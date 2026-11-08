"""Core data models."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def _i(value: Any, default: int = 0) -> int:
    try:
        return int(value) if value is not None else default
    except (TypeError, ValueError):
        return default


@dataclass
class PairSnapshot:
    """A point-in-time view of a DEX trading pair (from DexScreener)."""

    chain_id: str
    dex_id: str
    pair_address: str
    token_address: str
    symbol: str
    name: str
    quote_symbol: str
    price_usd: float
    liquidity_usd: float
    fdv: float
    market_cap: float
    volume_m5: float
    volume_h1: float
    volume_h24: float
    change_m5: float
    change_h1: float
    change_h6: float
    change_h24: float
    buys_m5: int
    sells_m5: int
    buys_h1: int
    sells_h1: int
    created_at_ms: int | None
    url: str = ""

    @classmethod
    def from_api(cls, d: dict[str, Any]) -> "PairSnapshot":
        base = d.get("baseToken") or {}
        quote = d.get("quoteToken") or {}
        txns = d.get("txns") or {}
        vol = d.get("volume") or {}
        chg = d.get("priceChange") or {}
        liq = d.get("liquidity") or {}
        created = d.get("pairCreatedAt")
        return cls(
            chain_id=d.get("chainId", ""),
            dex_id=d.get("dexId", ""),
            pair_address=d.get("pairAddress", ""),
            token_address=base.get("address", ""),
            symbol=base.get("symbol", "?"),
            name=base.get("name", ""),
            quote_symbol=quote.get("symbol", ""),
            price_usd=_f(d.get("priceUsd")),
            liquidity_usd=_f(liq.get("usd")),
            fdv=_f(d.get("fdv")),
            market_cap=_f(d.get("marketCap")),
            volume_m5=_f(vol.get("m5")),
            volume_h1=_f(vol.get("h1")),
            volume_h24=_f(vol.get("h24")),
            change_m5=_f(chg.get("m5")),
            change_h1=_f(chg.get("h1")),
            change_h6=_f(chg.get("h6")),
            change_h24=_f(chg.get("h24")),
            buys_m5=_i((txns.get("m5") or {}).get("buys")),
            sells_m5=_i((txns.get("m5") or {}).get("sells")),
            buys_h1=_i((txns.get("h1") or {}).get("buys")),
            sells_h1=_i((txns.get("h1") or {}).get("sells")),
            created_at_ms=_i(created) if created else None,
            url=d.get("url", ""),
        )

    def age_minutes(self, now: float) -> float | None:
        if not self.created_at_ms:
            return None
        return max(0.0, (now - self.created_at_ms / 1000.0) / 60.0)

    @property
    def txns_h1(self) -> int:
        return self.buys_h1 + self.sells_h1

    @property
    def buy_ratio_h1(self) -> float:
        total = self.txns_h1
        return self.buys_h1 / total if total else 0.0


@dataclass
class Fill:
    """Result of an executed (or simulated) swap."""

    price_usd: float  # effective price per token
    quantity: float  # tokens bought/sold (UI units)
    usd_value: float  # USD spent (buy) or received (sell), after fees
    fee_usd: float = 0.0
    signature: str | None = None


@dataclass
class Position:
    token_address: str
    symbol: str
    pair_address: str
    entry_price: float
    quantity: float
    cost_usd: float
    opened_at: float
    peak_price: float
    entry_liquidity_usd: float
    entry_score: float = 0.0
    signature: str | None = None

    def pnl_pct(self, price: float) -> float:
        return (price / self.entry_price - 1.0) * 100.0 if self.entry_price else 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Position":
        return cls(**d)


@dataclass
class ClosedTrade:
    token_address: str
    symbol: str
    entry_price: float
    exit_price: float
    quantity: float
    cost_usd: float
    proceeds_usd: float
    opened_at: float
    closed_at: float
    reason: str
    signatures: list[str] = field(default_factory=list)

    @property
    def pnl_usd(self) -> float:
        return self.proceeds_usd - self.cost_usd

    @property
    def pnl_pct(self) -> float:
        return (self.proceeds_usd / self.cost_usd - 1.0) * 100.0 if self.cost_usd else 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClosedTrade":
        return cls(**d)
