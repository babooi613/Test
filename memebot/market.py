"""Market data sources: DexScreener (discovery + prices) and RugCheck (safety)."""

from __future__ import annotations

import logging
from typing import Any, Iterable

from .http import HttpClient
from .models import PairSnapshot

log = logging.getLogger(__name__)

DEXSCREENER = "https://api.dexscreener.com"
RUGCHECK = "https://api.rugcheck.xyz/v1"


class DexScreener:
    def __init__(self, http: HttpClient | None = None, base_url: str = DEXSCREENER):
        self.http = http or HttpClient()
        self.base_url = base_url

    def _token_feed(self, path: str, chain: str) -> list[str]:
        data = self.http.get(f"{self.base_url}{path}")
        items = data if isinstance(data, list) else [data]
        out: list[str] = []
        for item in items:
            if isinstance(item, dict) and item.get("chainId") == chain and item.get("tokenAddress"):
                out.append(item["tokenAddress"])
        return out

    def latest_profiles(self, chain: str) -> list[str]:
        return self._token_feed("/token-profiles/latest/v1", chain)

    def latest_boosts(self, chain: str) -> list[str]:
        return self._token_feed("/token-boosts/latest/v1", chain)

    def pairs_for_tokens(self, chain: str, addresses: Iterable[str]) -> list[PairSnapshot]:
        """Fetch all pairs for up to N tokens (batched by 30, the API limit)."""
        addrs = list(dict.fromkeys(a for a in addresses if a))
        pairs: list[PairSnapshot] = []
        for i in range(0, len(addrs), 30):
            chunk = ",".join(addrs[i : i + 30])
            try:
                data = self.http.get(f"{self.base_url}/tokens/v1/{chain}/{chunk}")
            except RuntimeError as exc:
                log.warning("DexScreener pair lookup failed: %s", exc)
                continue
            if isinstance(data, dict):  # older response shape
                data = data.get("pairs") or []
            for raw in data or []:
                try:
                    pairs.append(PairSnapshot.from_api(raw))
                except Exception as exc:  # noqa: BLE001 - skip malformed rows
                    log.debug("Skipping malformed pair: %s", exc)
        return pairs


def best_pair_per_token(pairs: Iterable[PairSnapshot], chain: str) -> dict[str, PairSnapshot]:
    """Pick the most liquid pair for each token (that's where we'd trade)."""
    best: dict[str, PairSnapshot] = {}
    for p in pairs:
        if p.chain_id != chain or p.price_usd <= 0:
            continue
        cur = best.get(p.token_address)
        if cur is None or p.liquidity_usd > cur.liquidity_usd:
            best[p.token_address] = p
    return best


class RugCheck:
    def __init__(self, http: HttpClient | None = None, base_url: str = RUGCHECK):
        self.http = http or HttpClient(retries=2)
        self.base_url = base_url
        self._cache: dict[str, dict[str, Any] | None] = {}

    def report(self, mint: str) -> dict[str, Any] | None:
        if mint in self._cache:
            return self._cache[mint]
        try:
            data = self.http.get(f"{self.base_url}/tokens/{mint}/report/summary")
        except RuntimeError as exc:
            log.warning("RugCheck lookup failed for %s: %s", mint, exc)
            data = None
        self._cache[mint] = data
        return data
