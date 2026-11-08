"""Trade execution: a paper simulator and a live Jupiter (Solana) executor."""

from __future__ import annotations

import base64
import json
import logging
import os
import time
from pathlib import Path
from typing import Protocol

from .config import LiveConfig, PaperConfig
from .http import HttpClient
from .models import Fill, PairSnapshot, Position

log = logging.getLogger(__name__)

SOL_MINT = "So11111111111111111111111111111111111111112"
LAMPORTS_PER_SOL = 1_000_000_000


class Executor(Protocol):
    def buy(self, pair: PairSnapshot, usd_amount: float) -> Fill | None: ...

    def sell(self, position: Position, pair: PairSnapshot | None) -> Fill | None: ...


# --------------------------------------------------------------------------
# Paper trading
# --------------------------------------------------------------------------
class PaperExecutor:
    """Simulates fills at the current DexScreener price with slippage and fees."""

    def __init__(self, cfg: PaperConfig):
        self.cfg = cfg

    def buy(self, pair: PairSnapshot, usd_amount: float) -> Fill | None:
        if pair.price_usd <= 0:
            return None
        fill_price = pair.price_usd * (1 + self.cfg.slippage_pct / 100)
        fee = usd_amount * self.cfg.fee_pct / 100
        qty = (usd_amount - fee) / fill_price
        return Fill(price_usd=fill_price, quantity=qty, usd_value=usd_amount, fee_usd=fee)

    def sell(self, position: Position, pair: PairSnapshot | None) -> Fill | None:
        if pair is None or pair.price_usd <= 0:
            return None
        fill_price = pair.price_usd * (1 - self.cfg.slippage_pct / 100)
        gross = position.quantity * fill_price
        fee = gross * self.cfg.fee_pct / 100
        return Fill(price_usd=fill_price, quantity=position.quantity, usd_value=gross - fee, fee_usd=fee)


# --------------------------------------------------------------------------
# Live trading via Jupiter aggregator
# --------------------------------------------------------------------------
def load_keypair(secret: str):
    """Load a Solana keypair from a base58 string or a path to a JSON byte array (id.json)."""
    from solders.keypair import Keypair  # imported lazily: only needed for live trading

    secret = secret.strip()
    path = Path(os.path.expanduser(secret))
    if path.suffix == ".json" and path.exists():
        return Keypair.from_bytes(bytes(json.loads(path.read_text())))
    if secret.startswith("["):
        return Keypair.from_bytes(bytes(json.loads(secret)))
    return Keypair.from_base58_string(secret)


class SolanaRpc:
    def __init__(self, url: str, http: HttpClient):
        self.url = url
        self.http = http
        self._id = 0

    def call(self, method: str, params: list) -> object:
        self._id += 1
        resp = self.http.post(
            self.url, json={"jsonrpc": "2.0", "id": self._id, "method": method, "params": params}
        )
        if "error" in resp:
            raise RuntimeError(f"RPC {method} error: {resp['error']}")
        return resp.get("result")

    def sol_balance(self, owner: str) -> float:
        res = self.call("getBalance", [owner, {"commitment": "confirmed"}])
        return res["value"] / LAMPORTS_PER_SOL  # type: ignore[index]

    def token_balance(self, owner: str, mint: str) -> tuple[int, float]:
        """Return (raw amount, UI amount) summed over all of owner's accounts for mint."""
        res = self.call(
            "getTokenAccountsByOwner",
            [owner, {"mint": mint}, {"encoding": "jsonParsed", "commitment": "confirmed"}],
        )
        raw, ui = 0, 0.0
        for acc in res["value"]:  # type: ignore[index]
            amt = acc["account"]["data"]["parsed"]["info"]["tokenAmount"]
            raw += int(amt["amount"])
            ui += float(amt.get("uiAmount") or 0)
        return raw, ui

    def send_raw(self, tx_bytes: bytes) -> str:
        b64 = base64.b64encode(tx_bytes).decode()
        return self.call(  # type: ignore[return-value]
            "sendTransaction",
            [b64, {"encoding": "base64", "skipPreflight": True, "maxRetries": 3}],
        )

    def confirm(self, sig: str, timeout: float) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            res = self.call("getSignatureStatuses", [[sig], {"searchTransactionHistory": True}])
            status = (res or {}).get("value", [None])[0]  # type: ignore[union-attr]
            if status:
                if status.get("err"):
                    raise RuntimeError(f"Transaction {sig} failed: {status['err']}")
                if status.get("confirmationStatus") in ("confirmed", "finalized"):
                    return True
            time.sleep(2)
        return False


class JupiterExecutor:
    """Swaps SOL <-> token through Jupiter, signs locally, sends via your RPC.

    All amounts are sized in USD and converted to SOL using Jupiter's price API.
    """

    def __init__(self, cfg: LiveConfig, http: HttpClient | None = None, keypair=None):
        self.cfg = cfg
        self.http = http or HttpClient(timeout=20)
        self.rpc = SolanaRpc(cfg.rpc_url, self.http)
        if keypair is None:
            secret = os.environ.get(cfg.private_key_env)
            if not secret:
                raise RuntimeError(
                    f"Live mode requires a wallet: set ${cfg.private_key_env} to a base58 "
                    "secret key or a path to a Solana id.json file."
                )
            keypair = load_keypair(secret)
        self.keypair = keypair
        self.owner = str(keypair.pubkey())
        log.info("Live executor ready for wallet %s", self.owner)

    # ---- Jupiter API ---------------------------------------------------
    def sol_price_usd(self) -> float:
        data = self.http.get(f"{self.cfg.jupiter_base_url}/price/v3", params={"ids": SOL_MINT})
        price = float((data.get(SOL_MINT) or {}).get("usdPrice") or 0)
        if price <= 0:
            raise RuntimeError("Could not fetch SOL price from Jupiter")
        return price

    def _quote(self, input_mint: str, output_mint: str, amount: int) -> dict:
        quote = self.http.get(
            f"{self.cfg.jupiter_base_url}/swap/v1/quote",
            params={
                "inputMint": input_mint,
                "outputMint": output_mint,
                "amount": str(amount),
                "slippageBps": self.cfg.slippage_bps,
            },
        )
        impact = float(quote.get("priceImpactPct") or 0) * 100
        if impact > self.cfg.max_price_impact_pct:
            raise RuntimeError(f"Price impact {impact:.2f}% exceeds max {self.cfg.max_price_impact_pct}%")
        return quote

    def _swap(self, quote: dict) -> str:
        from solders.transaction import VersionedTransaction

        body = {
            "quoteResponse": quote,
            "userPublicKey": self.owner,
            "wrapAndUnwrapSol": True,
            "dynamicComputeUnitLimit": True,
            "prioritizationFeeLamports": self.cfg.priority_fee_lamports,
        }
        resp = self.http.post(f"{self.cfg.jupiter_base_url}/swap/v1/swap", json=body)
        raw = base64.b64decode(resp["swapTransaction"])
        unsigned = VersionedTransaction.from_bytes(raw)
        signed = VersionedTransaction(unsigned.message, [self.keypair])
        sig = self.rpc.send_raw(bytes(signed))
        log.info("Sent swap tx %s", sig)
        if not self.rpc.confirm(sig, self.cfg.confirm_timeout_sec):
            raise RuntimeError(f"Transaction {sig} not confirmed within {self.cfg.confirm_timeout_sec}s")
        return sig

    def _wait_balance_change(self, mint: str, before_raw: int, expect_increase: bool) -> tuple[int, float]:
        for _ in range(10):
            raw, ui = self.rpc.token_balance(self.owner, mint)
            if (raw > before_raw) if expect_increase else (raw < before_raw):
                return raw, ui
            time.sleep(1.5)
        return self.rpc.token_balance(self.owner, mint)

    # ---- Executor interface -------------------------------------------
    def buy(self, pair: PairSnapshot, usd_amount: float) -> Fill | None:
        try:
            sol_usd = self.sol_price_usd()
            sol_needed = usd_amount / sol_usd
            balance = self.rpc.sol_balance(self.owner)
            if balance - sol_needed < self.cfg.min_sol_reserve:
                log.warning("Insufficient SOL: have %.4f, need %.4f + reserve", balance, sol_needed)
                return None
            before_raw, before_ui = self.rpc.token_balance(self.owner, pair.token_address)
            lamports = int(sol_needed * LAMPORTS_PER_SOL)
            quote = self._quote(SOL_MINT, pair.token_address, lamports)
            sig = self._swap(quote)
            _, after_ui = self._wait_balance_change(pair.token_address, before_raw, True)
            qty = after_ui - before_ui
            if qty <= 0:
                log.error("Buy %s confirmed but token balance did not increase", sig)
                return None
            return Fill(price_usd=usd_amount / qty, quantity=qty, usd_value=usd_amount, signature=sig)
        except Exception as exc:  # noqa: BLE001
            log.error("Live buy of %s failed: %s", pair.symbol, exc)
            return None

    def sell(self, position: Position, pair: PairSnapshot | None) -> Fill | None:
        try:
            raw, ui = self.rpc.token_balance(self.owner, position.token_address)
            if raw <= 0:
                log.warning("No %s balance to sell; marking closed at zero", position.symbol)
                return Fill(price_usd=0.0, quantity=0.0, usd_value=0.0)
            quote = self._quote(position.token_address, SOL_MINT, raw)
            sig = self._swap(quote)
            sol_out = int(quote["outAmount"]) / LAMPORTS_PER_SOL
            usd = sol_out * self.sol_price_usd()
            return Fill(price_usd=usd / ui if ui else 0.0, quantity=ui, usd_value=usd, signature=sig)
        except Exception as exc:  # noqa: BLE001
            log.error("Live sell of %s failed: %s", position.symbol, exc)
            return None
