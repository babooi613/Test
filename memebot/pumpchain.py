"""On-chain pump.fun helpers: read the Mayhem flag from a coin's bonding curve.

Layout from the official IDL (github.com/pump-fun/pump-public-docs, idl/pump.json):

    BondingCurve (Anchor account, borsh, no padding)
      [0..8)    discriminator  = 17 b7 f8 37 60 d8 ac 60
      [8..48)   virtual_token_reserves, virtual_quote_reserves,
                real_token_reserves, real_quote_reserves, token_total_supply  (5 x u64)
      [48]      complete        bool
      [49..81)  creator         pubkey
      [81]      is_mayhem_mode  bool     <- set once by create_v2, never changes
      ...
"""

from __future__ import annotations

import base64
import logging
import time

from .http import HttpClient

log = logging.getLogger(__name__)

PUMP_PROGRAM_ID = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
BONDING_CURVE_DISCRIMINATOR = bytes([23, 183, 248, 55, 96, 216, 172, 96])
MAYHEM_OFFSET = 81


def parse_is_mayhem(data: bytes) -> bool | None:
    """Return the is_mayhem_mode flag from raw BondingCurve account data."""
    if len(data) <= MAYHEM_OFFSET or data[:8] != BONDING_CURVE_DISCRIMINATOR:
        return None
    return data[MAYHEM_OFFSET] == 1


def bonding_curve_address(mint: str) -> str:
    from solders.pubkey import Pubkey

    pda, _ = Pubkey.find_program_address(
        [b"bonding-curve", bytes(Pubkey.from_string(mint))], Pubkey.from_string(PUMP_PROGRAM_ID)
    )
    return str(pda)


class MayhemChecker:
    """Looks up is_mayhem_mode via RPC getAccountInfo, with a small cache."""

    def __init__(self, rpc_url: str, http: HttpClient | None = None, attempts: int = 4, delay: float = 0.4):
        self.rpc_url = rpc_url
        self.http = http or HttpClient(timeout=5, retries=2, backoff=0.2)
        self.attempts = attempts
        self.delay = delay
        self.cache: dict[str, bool] = {}

    def _account_data(self, address: str) -> bytes | None:
        resp = self.http.post(self.rpc_url, json={
            "jsonrpc": "2.0", "id": 1, "method": "getAccountInfo",
            "params": [address, {"encoding": "base64", "commitment": "processed"}],
        })
        value = (resp.get("result") or {}).get("value")
        if not value:
            return None
        return base64.b64decode(value["data"][0])

    def is_mayhem(self, mint: str, bonding_curve: str = "") -> bool | None:
        if mint in self.cache:
            return self.cache[mint]
        try:
            address = bonding_curve or bonding_curve_address(mint)
        except Exception as exc:  # noqa: BLE001
            log.debug("Cannot derive bonding curve for %s: %s", mint, exc)
            return None
        for attempt in range(self.attempts):
            try:
                data = self._account_data(address)
            except RuntimeError as exc:
                log.debug("getAccountInfo failed for %s: %s", address, exc)
                data = None
            if data is not None:
                result = parse_is_mayhem(data)
                if result is not None:
                    self.cache[mint] = result
                return result
            time.sleep(self.delay * (attempt + 1))  # brand-new account may not be visible yet
        return None
