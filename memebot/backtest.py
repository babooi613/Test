"""Record live feed events and replay them to tune the sniper.

Recording:  the live sniper writes every event it receives to
            <events_dir>/events-YYYYMMDD.jsonl as {"t": unix_time, **event}.
Replay:     events are fed through the *same* SniperEngine + PaperSnipeExecutor
            on a simulated clock, so results reflect the real decision logic.
Tuning:     random search over parameters on the earlier part of the data
            ("train"), then each candidate is scored on the later part ("test")
            that it never saw. Only settings that also hold up on test matter.
"""

from __future__ import annotations

import asyncio
import copy
import glob
import json
import logging
import os
import random
import tempfile
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .config import GraduatedConfig, SniperConfig
from .sniper import PaperSnipeExecutor, SnipeBook, SniperEngine, SnipeTrade


class EventRecorder:
    def __init__(self, directory: str | Path):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._day = None
        self._fh = None

    def __call__(self, ev: dict[str, Any], now: float) -> None:
        day = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y%m%d")
        if day != self._day:
            if self._fh:
                self._fh.close()
            self._fh = open(self.dir / f"events-{day}.jsonl", "a", buffering=1)
            self._day = day
        self._fh.write(json.dumps({"t": round(now, 3), **ev}, separators=(",", ":")) + "\n")

    def close(self) -> None:
        if self._fh:
            self._fh.close()


def load_events(patterns: Iterable[str]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for pattern in patterns:
        for path in sorted(glob.glob(pattern)):
            with open(path) as fh:
                for line in fh:
                    try:
                        ev = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(ev, dict) and "t" in ev:
                        events.append(ev)
    events.sort(key=lambda e: e["t"])
    return events


@dataclass
class Result:
    trades: list[SnipeTrade]
    final_open: int

    @property
    def n(self) -> int:
        return len(self.trades)

    @property
    def pnl(self) -> float:
        return sum(t.pnl_sol for t in self.trades)

    @property
    def win_rate(self) -> float:
        return sum(t.pnl_sol > 0 for t in self.trades) / self.n if self.n else 0.0

    @property
    def avg_pct(self) -> float:
        return sum(t.pnl_pct for t in self.trades) / self.n if self.n else 0.0

    def by_kind(self, kind: str) -> "Result":
        return Result([t for t in self.trades if t.kind == kind], 0)

    def summary(self) -> str:
        return (f"trades={self.n:3d} win={self.win_rate:4.0%} pnl={self.pnl:+.4f} SOL "
                f"avg={self.avg_pct:+6.1f}%/trade")


def simulate(events: list[dict[str, Any]], scfg: SniperConfig, gcfg: GraduatedConfig | None,
             start_sol: float = 5.0) -> Result:
    """Replay events through the engine. Positions still open at the end are
    force-closed after their max hold time using the last known price."""
    return asyncio.run(_simulate(events, scfg, gcfg, start_sol))


async def _simulate(events, scfg, gcfg, start_sol) -> Result:
    clock = [events[0]["t"] if events else 0.0]
    with tempfile.TemporaryDirectory() as tmp:
        s = replace(scfg, state_file=os.path.join(tmp, "s.json"), daily_loss_limit_sol=1e9)
        g = replace(gcfg) if gcfg is not None else None
        book = SnipeBook(start_sol)
        eng = SniperEngine(s, PaperSnipeExecutor(s, g), book, paper=True,
                           kill_switch_file=os.path.join(tmp, "STOP"), clock=lambda: clock[0],
                           graduated=g if (g is not None and g.enabled) else None)
        eng._save = lambda: None  # no disk writes during replay
        next_hk = clock[0] + 1
        for ev in events:
            t = ev["t"]
            while next_hk <= t:
                clock[0] = next_hk
                await eng.housekeeping()
                await eng.drain()
                next_hk += 1
            clock[0] = t
            body = {k: v for k, v in ev.items() if k != "t"}
            await eng.on_event(body)
            await eng.drain()
        # Let remaining positions run out their max hold on last known prices.
        end = clock[0] + max(s.max_hold_sec, g.max_hold_sec if g else 0) + 2
        while next_hk <= end and book.positions:
            clock[0] = next_hk
            await eng.housekeeping()
            await eng.drain()
            next_hk += 1
        return Result(list(book.closed), len(book.positions))


# ---------------------------------------------------------------- tuning
MAYHEM_SPACE = {
    "min_age_sec": [1, 3, 5, 10],
    "max_age_sec": [20, 45, 90, 180],
    "min_buys": [3, 5, 8, 12, 20],
    "min_unique_buyers": [1, 2, 3, 6, 10],
    "min_buy_volume_sol": [0.2, 0.5, 1.5, 3, 6],
    "min_buy_volume_ratio": [0.4, 0.5, 0.6, 0.7, 0.8],
    "max_top_buyer_share": [0.3, 0.4, 0.6, 0.8, 1.0],
    "max_gain_since_launch_pct": [30, 75, 150, 400],
    "take_profit_pct": [10, 20, 35, 60, 100],
    "stop_loss_pct": [8, 12, 20, 30],
    "trailing_activation_pct": [10, 20, 40],
    "trailing_stop_pct": [5, 8, 12, 20],
    "max_hold_sec": [20, 45, 90, 180, 300],
    "bot_wallet_min_coins": [0, 3, 4, 6],
}
GRAD_SPACE = {
    "min_age_sec": [0, 5, 15, 30],
    "max_age_sec": [30, 60, 120, 300],
    "min_buys": [5, 15, 30, 60],
    "min_unique_buyers": [3, 10, 25, 50],
    "min_buy_volume_sol": [1, 5, 10, 25],
    "min_buy_volume_ratio": [0.4, 0.5, 0.6, 0.7],
    "max_top_buyer_share": [0.2, 0.35, 0.5, 0.8, 1.0],
    "max_gain_since_launch_pct": [20, 50, 100, 300],
    "take_profit_pct": [8, 15, 30, 60],
    "stop_loss_pct": [5, 8, 12, 15, 25],
    "trailing_activation_pct": [5, 10, 15, 30],
    "trailing_stop_pct": [3, 5, 10, 15],
    "max_hold_sec": [30, 60, 120, 300, 600],
}


def _sample(space: dict[str, list], rng: random.Random) -> dict[str, Any]:
    p = {k: rng.choice(v) for k, v in space.items()}
    if p["max_age_sec"] <= p["min_age_sec"]:
        p["max_age_sec"] = p["min_age_sec"] + 30
    return p


def tune(events: list[dict[str, Any]], scfg: SniperConfig, gcfg: GraduatedConfig, kind: str,
         samples: int = 200, train_frac: float = 0.6, min_trades: int = 8, seed: int = 7,
         log=print) -> list[dict[str, Any]]:
    """Random-search one mode ("mayhem" or "graduated"). Returns candidates ranked by
    the WORSE of train/test PnL (robustness over a lucky fit)."""
    split = events[int(len(events) * train_frac)]["t"] if events else 0
    train = [e for e in events if e["t"] < split]
    test = [e for e in events if e["t"] >= split]
    rng = random.Random(seed)
    space = MAYHEM_SPACE if kind == "mayhem" else GRAD_SPACE

    def configs(params):
        s, g = copy.deepcopy(scfg), copy.deepcopy(gcfg)
        if kind == "mayhem":
            s = replace(s, enabled=True, **params)
            g = replace(g, enabled=False)
        else:
            bot = params.get("bot_wallet_min_coins")
            g = replace(g, enabled=True, **{k: v for k, v in params.items() if k != "bot_wallet_min_coins"})
            s = replace(s, enabled=False, **({"bot_wallet_min_coins": bot} if bot is not None else {}))
        s = replace(s, max_open_positions=10)
        return s, g

    base = {k: getattr(scfg if kind == "mayhem" else gcfg, k) for k in space}
    if kind == "graduated":
        base.pop("bot_wallet_min_coins", None)
    candidates = [base] + [_sample(space, rng) for _ in range(samples)]
    out = []
    for i, params in enumerate(candidates):
        s, g = configs(params)
        tr = simulate(train, s, g).by_kind(kind)
        if tr.n < min_trades or tr.pnl <= 0:
            if i == 0:
                out.append({"params": params, "train": tr, "test": simulate(test, s, g).by_kind(kind), "current": True})
            continue
        te = simulate(test, s, g).by_kind(kind)
        out.append({"params": params, "train": tr, "test": te, "current": i == 0})
        if (i + 1) % 25 == 0:
            log(f"  ...{i + 1}/{len(candidates)} tried, {len(out)} profitable on train")
    out.sort(key=lambda r: min(r["train"].pnl, r["test"].pnl), reverse=True)
    return out


def recorded_span(events: list[dict[str, Any]]) -> str:
    if not events:
        return "no events"
    hours = (events[-1]["t"] - events[0]["t"]) / 3600
    kinds: dict[str, int] = {}
    for e in events:
        kinds[e.get("txType", "?")] = kinds.get(e.get("txType", "?"), 0) + 1
    return f"{len(events)} events over {hours:.2f}h ({', '.join(f'{k}={v}' for k, v in sorted(kinds.items()))})"


logging.getLogger(__name__)
