"""PumpPortal integration: real-time pump.fun websocket feed + live trade execution.

Feed:   wss://pumpportal.fun/api/data  (one shared connection; subscribe per mint)
Trades: https://pumpportal.fun/api/trade-local  (returns an unsigned tx; we sign
        locally so the private key never leaves this machine)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time

import requests

from .config import LiveConfig, SniperConfig
from .execution import SolanaRpc, load_keypair
from .http import HttpClient
from .sniper import SniperEngine, SnipeFill, SnipePosition, TokenWatch

log = logging.getLogger(__name__)


class PumpPortalFeed:
    def __init__(self, url: str, engine: SniperEngine, api_key: str | None = None):
        if api_key:
            url += ("&" if "?" in url else "?") + "api-key=" + api_key
        self.url = url
        self.engine = engine
        self.ws = None
        self.subscribed: set[str] = set()
        engine.subscribe = self.subscribe
        engine.unsubscribe = self.unsubscribe

    async def _send(self, payload: dict) -> None:
        if self.ws is not None:
            await self.ws.send(json.dumps(payload))

    async def subscribe(self, mints: list[str]) -> None:
        new = [m for m in mints if m not in self.subscribed]
        if new:
            self.subscribed.update(new)
            await self._send({"method": "subscribeTokenTrade", "keys": new})

    async def unsubscribe(self, mints: list[str]) -> None:
        gone = [m for m in mints if m in self.subscribed]
        if gone:
            self.subscribed.difference_update(gone)
            await self._send({"method": "unsubscribeTokenTrade", "keys": gone})

    async def _housekeeping_loop(self) -> None:
        last_status = time.time()
        while True:
            await asyncio.sleep(1)
            try:
                await self.engine.housekeeping()
            except Exception:  # noqa: BLE001
                log.exception("Housekeeping error")
            if time.time() - last_status > 60:
                last_status = time.time()
                s, b = self.engine.stats, self.engine.book
                log.info(
                    "launches=%d mayhem=%d graduated=%d watching=%d open=%d buys=%d sells=%d pnl_today=%+.4f SOL",
                    s["launches"], s["mayhem"], s["graduated"], len(self.engine.watches), len(b.positions),
                    s["buys"], s["sells"], b.pnl_today(time.time()),
                )

    async def run(self) -> None:
        import websockets  # imported lazily

        hk = asyncio.create_task(self._housekeeping_loop())
        backoff = 1.0
        try:
            while True:
                try:
                    async with websockets.connect(self.url, ping_interval=20, max_size=2**22) as ws:
                        self.ws = ws
                        log.info("Connected to %s", self.url.split("?")[0])
                        backoff = 1.0
                        await self._send({"method": "subscribeNewToken"})
                        if self.engine.grad_cfg is not None and getattr(self.engine.grad_cfg, "enabled", True):
                            await self._send({"method": "subscribeMigration"})
                        if self.subscribed:  # restore after reconnect
                            await self._send({"method": "subscribeTokenTrade", "keys": sorted(self.subscribed)})
                        async for raw in ws:
                            try:
                                ev = json.loads(raw)
                            except ValueError:
                                continue
                            if isinstance(ev, dict) and ev.get("mint"):
                                await self.engine.on_event(ev)
                            elif isinstance(ev, dict) and ev.get("message"):
                                msg = str(ev["message"])
                                if "only available" in msg or "api key" in msg.lower():
                                    log.error(
                                        "PumpPortal refused the per-coin trade stream: %s  -> Without trade "
                                        "data the sniper cannot buy. Set $MEMEBOT_PUMPPORTAL_KEY to a funded "
                                        "PumpPortal API key.", msg,
                                    )
                                else:
                                    log.debug("Feed: %s", msg)
                except Exception as exc:  # noqa: BLE001 - CancelledError still propagates
                    log.warning("Feed disconnected (%s); reconnecting in %.0fs", exc, backoff)
                finally:
                    self.ws = None
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)
        finally:
            hk.cancel()
            await self.engine.drain()


class PumpPortalExecutor:
    """Live buys/sells on pump.fun via PumpPortal's local-transaction API."""

    def __init__(self, cfg: SniperConfig, live: LiveConfig, keypair=None):
        from solders.transaction import VersionedTransaction  # noqa: F401 - fail fast if missing

        self.cfg = cfg
        self.live = live
        self.http = HttpClient(timeout=10, retries=2, backoff=0.3)
        self.rpc = SolanaRpc(live.rpc_url, self.http)
        if keypair is None:
            secret = os.environ.get(live.private_key_env)
            if not secret:
                raise RuntimeError(f"Set ${live.private_key_env} to your wallet key (base58 or id.json path)")
            keypair = load_keypair(secret)
        self.keypair = keypair
        self.owner = str(keypair.pubkey())
        log.info("Sniper live executor ready for wallet %s", self.owner)

    def _trade(self, action: str, mint: str, amount, denominated_in_sol: bool) -> str:
        from solders.transaction import VersionedTransaction

        resp = requests.post(
            self.cfg.trade_api_url,
            data={
                "publicKey": self.owner,
                "action": action,
                "mint": mint,
                "amount": amount,
                "denominatedInSol": "true" if denominated_in_sol else "false",
                "slippage": self.cfg.slippage_pct,
                "priorityFee": self.cfg.priority_fee_sol,
                "pool": self.cfg.pool,
            },
            timeout=10,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"PumpPortal {action} failed: HTTP {resp.status_code} {resp.text[:200]}")
        tx = VersionedTransaction.from_bytes(resp.content)
        signed = VersionedTransaction(tx.message, [self.keypair])
        sig = self.rpc.send_raw(bytes(signed))
        log.info("%s tx sent: %s", action.upper(), sig)
        if not self.rpc.confirm(sig, self.live.confirm_timeout_sec):
            raise RuntimeError(f"{action} tx {sig} not confirmed")
        return sig

    def buy(self, w: TokenWatch, sol_amount: float) -> SnipeFill | None:
        try:
            bal = self.rpc.sol_balance(self.owner)
            if bal - sol_amount < self.live.min_sol_reserve:
                log.warning("Insufficient SOL (%.4f) for %.4f buy + reserve", bal, sol_amount)
                return None
            before_raw, before_ui = self.rpc.token_balance(self.owner, w.mint)
            sig = self._trade("buy", w.mint, sol_amount, True)
            ui = before_ui
            for _ in range(10):
                raw, ui = self.rpc.token_balance(self.owner, w.mint)
                if raw > before_raw:
                    break
                time.sleep(0.5)
            spent = bal - self.rpc.sol_balance(self.owner)  # includes fees / rent
            tokens = ui - before_ui
            if tokens <= 0:
                log.error("Buy %s confirmed but no tokens received", sig)
                return None
            return SnipeFill(tokens=tokens, sol=max(spent, sol_amount), signature=sig)
        except Exception as exc:  # noqa: BLE001
            log.error("Live buy %s failed: %s", w.symbol, exc)
            return None

    def sell(self, pos: SnipePosition, w: TokenWatch | None) -> SnipeFill | None:
        try:
            raw, ui = self.rpc.token_balance(self.owner, pos.mint)
            if raw <= 0:
                return SnipeFill(tokens=0.0, sol=0.0)
            before = self.rpc.sol_balance(self.owner)
            sig = self._trade("sell", pos.mint, "100%", False)
            time.sleep(0.5)
            received = self.rpc.sol_balance(self.owner) - before
            return SnipeFill(tokens=ui, sol=max(received, 0.0), signature=sig)
        except Exception as exc:  # noqa: BLE001
            log.error("Live sell %s failed: %s", pos.symbol, exc)
            return None

