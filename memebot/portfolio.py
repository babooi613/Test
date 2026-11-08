"""Portfolio state: open positions, trade history, cash (paper), persistence."""

from __future__ import annotations

import json
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from .models import ClosedTrade, Fill, PairSnapshot, Position


class Portfolio:
    def __init__(self, cash_usd: float = 0.0):
        self.cash_usd = cash_usd
        self.positions: dict[str, Position] = {}
        self.closed: list[ClosedTrade] = []
        self.cooldowns: dict[str, float] = {}  # token -> unix time until re-entry allowed

    # ---- queries -------------------------------------------------------
    def exposure_usd(self) -> float:
        return sum(p.cost_usd for p in self.positions.values())

    def realized_pnl_since(self, since_ts: float) -> float:
        return sum(t.pnl_usd for t in self.closed if t.closed_at >= since_ts)

    def realized_pnl_today(self, now: float | None = None) -> float:
        now = now if now is not None else time.time()
        day = datetime.fromtimestamp(now, tz=timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        return self.realized_pnl_since(day.timestamp())

    def in_cooldown(self, token: str, now: float) -> bool:
        return self.cooldowns.get(token, 0) > now

    def equity_usd(self, prices: dict[str, float]) -> float:
        value = sum(
            p.quantity * prices.get(p.token_address, p.entry_price) for p in self.positions.values()
        )
        return self.cash_usd + value

    # ---- mutations -----------------------------------------------------
    def open(self, pair: PairSnapshot, fill: Fill, score: float, now: float) -> Position:
        pos = Position(
            token_address=pair.token_address,
            symbol=pair.symbol,
            pair_address=pair.pair_address,
            entry_price=fill.price_usd,
            quantity=fill.quantity,
            cost_usd=fill.usd_value,
            opened_at=now,
            peak_price=fill.price_usd,
            entry_liquidity_usd=pair.liquidity_usd,
            entry_score=score,
            signature=fill.signature,
        )
        self.positions[pair.token_address] = pos
        self.cash_usd -= fill.usd_value
        return pos

    def close(self, token: str, fill: Fill, reason: str, now: float, cooldown_sec: float) -> ClosedTrade:
        pos = self.positions.pop(token)
        trade = ClosedTrade(
            token_address=token,
            symbol=pos.symbol,
            entry_price=pos.entry_price,
            exit_price=fill.price_usd,
            quantity=pos.quantity,
            cost_usd=pos.cost_usd,
            proceeds_usd=fill.usd_value,
            opened_at=pos.opened_at,
            closed_at=now,
            reason=reason,
            signatures=[s for s in (pos.signature, fill.signature) if s],
        )
        self.closed.append(trade)
        self.cash_usd += fill.usd_value
        self.cooldowns[token] = now + cooldown_sec
        return trade

    # ---- persistence ---------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "cash_usd": self.cash_usd,
            "positions": [p.to_dict() for p in self.positions.values()],
            "closed": [t.to_dict() for t in self.closed],
            "cooldowns": self.cooldowns,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Portfolio":
        pf = cls(cash_usd=d.get("cash_usd", 0.0))
        for p in d.get("positions", []):
            pos = Position.from_dict(p)
            pf.positions[pos.token_address] = pos
        pf.closed = [ClosedTrade.from_dict(t) for t in d.get("closed", [])]
        pf.cooldowns = {k: float(v) for k, v in d.get("cooldowns", {}).items()}
        return pf

    def save(self, path: str | Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".state-", suffix=".json")
        with os.fdopen(fd, "w") as fh:
            json.dump(self.to_dict(), fh, indent=2)
        os.replace(tmp, path)  # atomic

    @classmethod
    def load(cls, path: str | Path, default_cash: float) -> "Portfolio":
        path = Path(path)
        if not path.exists():
            return cls(cash_usd=default_cash)
        with open(path) as fh:
            return cls.from_dict(json.load(fh))
