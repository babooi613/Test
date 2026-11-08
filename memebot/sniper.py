"""Real-time pump.fun launch sniper (targets Mayhem-mode coins).

Flow per launch:
  new token event -> watch it (stream every trade) -> if early order flow passes
  the entry rules inside [min_age_sec, max_age_sec], buy -> re-check exits on
  every subsequent trade (TP / SL / trailing / dev-sell / max hold) -> sell.

All accounting is in SOL. Decisions are pure functions so they can be tested
offline; execution runs in worker threads so the feed is never blocked.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from .config import SniperConfig

log = logging.getLogger(__name__)


def _f(v: Any) -> float:
    try:
        return float(v) if v is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def is_mayhem(event: dict[str, Any], keys: list[str]) -> bool:
    for k in keys:
        v = event.get(k)
        if isinstance(v, bool):
            return v
        if isinstance(v, str) and v.lower() in ("true", "1", "yes"):
            return True
        if isinstance(v, (int, float)) and v:
            return True
    return False


# --------------------------------------------------------------------------
# Per-token live state
# --------------------------------------------------------------------------
@dataclass
class TokenWatch:
    mint: str
    symbol: str
    name: str
    creator: str
    created_at: float
    mayhem: bool
    v_sol: float = 0.0
    v_tokens: float = 0.0
    mcap_sol: float = 0.0
    launch_price: float = 0.0
    buys: int = 0
    sells: int = 0
    buy_sol: float = 0.0
    sell_sol: float = 0.0
    buyer_sol: dict[str, float] = field(default_factory=dict)
    dev_sold: bool = False
    last_trade_at: float = 0.0

    @property
    def price(self) -> float:
        """Price in SOL per token from the bonding curve reserves."""
        return self.v_sol / self.v_tokens if self.v_tokens else 0.0

    def age(self, now: float) -> float:
        return now - self.created_at

    @property
    def unique_buyers(self) -> int:
        return len(self.buyer_sol)

    @property
    def buy_volume_ratio(self) -> float:
        total = self.buy_sol + self.sell_sol
        return self.buy_sol / total if total else 0.0

    @property
    def top_buyer_share(self) -> float:
        return max(self.buyer_sol.values()) / self.buy_sol if self.buyer_sol and self.buy_sol else 0.0

    def _update_curve(self, ev: dict[str, Any]) -> None:
        vs, vt = _f(ev.get("vSolInBondingCurve")), _f(ev.get("vTokensInBondingCurve"))
        if vs > 0 and vt > 0:
            self.v_sol, self.v_tokens = vs, vt
        mc = _f(ev.get("marketCapSol"))
        if mc > 0:
            self.mcap_sol = mc

    @classmethod
    def from_create(cls, ev: dict[str, Any], now: float, mayhem_keys: list[str]) -> "TokenWatch":
        w = cls(
            mint=ev.get("mint", ""),
            symbol=str(ev.get("symbol", "?"))[:16],
            name=str(ev.get("name", ""))[:64],
            creator=ev.get("traderPublicKey", ""),
            created_at=now,
            mayhem=is_mayhem(ev, mayhem_keys),
            last_trade_at=now,
        )
        w._update_curve(ev)
        w.launch_price = w.price
        return w

    def apply_trade(self, ev: dict[str, Any], now: float) -> None:
        side = ev.get("txType")
        trader = ev.get("traderPublicKey", "")
        sol = _f(ev.get("solAmount"))
        prev_v_sol = self.v_sol
        self._update_curve(ev)
        if sol <= 0 and prev_v_sol:  # some feeds omit solAmount: infer from reserves
            sol = abs(self.v_sol - prev_v_sol)
        if side == "buy":
            self.buys += 1
            self.buy_sol += sol
            if trader and trader != self.creator:
                self.buyer_sol[trader] = self.buyer_sol.get(trader, 0.0) + sol
        elif side == "sell":
            self.sells += 1
            self.sell_sol += sol
            if trader and trader == self.creator:
                self.dev_sold = True
        self.last_trade_at = now


def entry_rejections(w: TokenWatch, cfg: SniperConfig, now: float) -> list[str]:
    r: list[str] = []
    age = w.age(now)
    if age < cfg.min_age_sec:
        r.append(f"too early ({age:.1f}s)")
    if age > cfg.max_age_sec:
        r.append(f"window closed ({age:.0f}s)")
    if cfg.only_mayhem and not w.mayhem:
        r.append("not mayhem")
    if w.creator in cfg.creator_blacklist:
        r.append("blacklisted creator")
    if cfg.require_dev_holding and w.dev_sold:
        r.append("dev sold")
    if w.buys < cfg.min_buys:
        r.append(f"buys {w.buys} < {cfg.min_buys}")
    if w.unique_buyers < cfg.min_unique_buyers:
        r.append(f"unique buyers {w.unique_buyers} < {cfg.min_unique_buyers}")
    if w.buy_sol < cfg.min_buy_volume_sol:
        r.append(f"buy volume {w.buy_sol:.2f} < {cfg.min_buy_volume_sol} SOL")
    if w.buy_volume_ratio < cfg.min_buy_volume_ratio:
        r.append(f"buy ratio {w.buy_volume_ratio:.2f} < {cfg.min_buy_volume_ratio}")
    if w.top_buyer_share > cfg.max_top_buyer_share:
        r.append(f"top buyer {w.top_buyer_share:.0%} of volume")
    if not (cfg.min_mcap_sol <= w.mcap_sol <= cfg.max_mcap_sol):
        r.append(f"mcap {w.mcap_sol:.1f} SOL out of range")
    if w.launch_price and w.price:
        gain = (w.price / w.launch_price - 1) * 100
        if gain > cfg.max_gain_since_launch_pct:
            r.append(f"already +{gain:.0f}% since launch")
    if w.price <= 0:
        r.append("no price")
    return r


# --------------------------------------------------------------------------
# Positions & book
# --------------------------------------------------------------------------
@dataclass
class SnipePosition:
    mint: str
    symbol: str
    entry_price: float  # SOL per token
    tokens: float
    cost_sol: float
    opened_at: float
    peak_price: float
    signature: str | None = None

    def pnl_pct(self, price: float) -> float:
        return (price / self.entry_price - 1) * 100 if self.entry_price else 0.0


@dataclass
class SnipeTrade:
    mint: str
    symbol: str
    cost_sol: float
    proceeds_sol: float
    opened_at: float
    closed_at: float
    reason: str
    signatures: list[str] = field(default_factory=list)

    @property
    def pnl_sol(self) -> float:
        return self.proceeds_sol - self.cost_sol

    @property
    def pnl_pct(self) -> float:
        return (self.proceeds_sol / self.cost_sol - 1) * 100 if self.cost_sol else 0.0


def exit_reason(pos: SnipePosition, w: TokenWatch | None, cfg: SniperConfig, now: float) -> str | None:
    held = now - pos.opened_at
    if w is None or w.price <= 0:
        return f"max_hold ({held:.0f}s, no data)" if held >= cfg.max_hold_sec else None
    price = w.price
    pos.peak_price = max(pos.peak_price, price)
    pnl = pos.pnl_pct(price)
    if cfg.exit_on_dev_sell and w.dev_sold:
        return f"dev_sold ({pnl:+.1f}%)"
    if pnl <= -cfg.stop_loss_pct:
        return f"stop_loss ({pnl:+.1f}%)"
    if pnl >= cfg.take_profit_pct:
        return f"take_profit ({pnl:+.1f}%)"
    if pos.pnl_pct(pos.peak_price) >= cfg.trailing_activation_pct:
        off_peak = (1 - price / pos.peak_price) * 100
        if off_peak >= cfg.trailing_stop_pct:
            return f"trailing_stop ({off_peak:.1f}% off peak, {pnl:+.1f}%)"
    if held >= cfg.max_hold_sec:
        return f"max_hold ({held:.0f}s, {pnl:+.1f}%)"
    return None


class SnipeBook:
    def __init__(self, sol_balance: float = 0.0):
        self.sol_balance = sol_balance  # paper balance (live: net flow)
        self.positions: dict[str, SnipePosition] = {}
        self.closed: list[SnipeTrade] = []
        self.attempted: set[str] = set()  # one shot per mint

    def pnl_today(self, now: float) -> float:
        day = datetime.fromtimestamp(now, tz=timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        return sum(t.pnl_sol for t in self.closed if t.closed_at >= day.timestamp())

    def to_dict(self) -> dict:
        return {
            "sol_balance": self.sol_balance,
            "positions": [asdict(p) for p in self.positions.values()],
            "closed": [asdict(t) for t in self.closed],
            "attempted": sorted(self.attempted)[-5000:],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "SnipeBook":
        b = cls(d.get("sol_balance", 0.0))
        for p in d.get("positions", []):
            b.positions[p["mint"]] = SnipePosition(**p)
        b.closed = [SnipeTrade(**t) for t in d.get("closed", [])]
        b.attempted = set(d.get("attempted", []))
        return b

    def save(self, path: str | Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".sniper-", suffix=".json")
        with os.fdopen(fd, "w") as fh:
            json.dump(self.to_dict(), fh, indent=2)
        os.replace(tmp, path)

    @classmethod
    def load(cls, path: str | Path, default_sol: float) -> "SnipeBook":
        path = Path(path)
        if not path.exists():
            return cls(default_sol)
        return cls.from_dict(json.loads(path.read_text()))


# --------------------------------------------------------------------------
# Execution interface
# --------------------------------------------------------------------------
@dataclass
class SnipeFill:
    tokens: float
    sol: float  # SOL spent (buy) or received (sell), after fees
    signature: str | None = None


class SnipeExecutor(Protocol):
    def buy(self, w: TokenWatch, sol_amount: float) -> SnipeFill | None: ...

    def sell(self, pos: SnipePosition, w: TokenWatch | None) -> SnipeFill | None: ...


class PaperSnipeExecutor:
    """Simulates fills against the constant-product bonding curve, plus fees and
    an extra latency haircut (real bots will usually get in/out before you)."""

    def __init__(self, cfg: SniperConfig):
        self.cfg = cfg

    def buy(self, w: TokenWatch, sol_amount: float) -> SnipeFill | None:
        if w.v_sol <= 0 or w.v_tokens <= 0:
            return None
        net = sol_amount * (1 - self.cfg.paper_fee_pct / 100)
        k = w.v_sol * w.v_tokens
        tokens = w.v_tokens - k / (w.v_sol + net)
        tokens *= 1 - self.cfg.paper_latency_slippage_pct / 100
        return SnipeFill(tokens=tokens, sol=sol_amount)

    def sell(self, pos: SnipePosition, w: TokenWatch | None) -> SnipeFill | None:
        if w is None or w.v_sol <= 0 or w.v_tokens <= 0:
            return None
        k = w.v_sol * w.v_tokens
        sol_out = w.v_sol - k / (w.v_tokens + pos.tokens)
        sol_out *= (1 - self.cfg.paper_fee_pct / 100) * (1 - self.cfg.paper_latency_slippage_pct / 100)
        return SnipeFill(tokens=pos.tokens, sol=max(sol_out, 0.0))


# --------------------------------------------------------------------------
# Engine
# --------------------------------------------------------------------------
class SniperEngine:
    """Consumes feed events and decides/executes trades.

    `subscribe`/`unsubscribe` are async callbacks into the feed so the engine
    can stream trades for the tokens it is watching.
    """

    def __init__(self, cfg: SniperConfig, executor: SnipeExecutor, book: SnipeBook,
                 paper: bool = True, kill_switch_file: str = "STOP", clock=time.time):
        self.cfg = cfg
        self.executor = executor
        self.book = book
        self.paper = paper
        self.kill_switch_file = kill_switch_file
        self.clock = clock
        self.watches: dict[str, TokenWatch] = {}
        self.pending: set[str] = set()  # mints with an in-flight order
        self.tasks: set[asyncio.Task] = set()
        self.subscribe = None  # set by feed: async fn(list[str])
        self.unsubscribe = None
        self.stats = {"launches": 0, "mayhem": 0, "buys": 0, "sells": 0}
        self._warned_no_mayhem = False

    # ---- feed callbacks ------------------------------------------------
    async def on_event(self, ev: dict[str, Any]) -> None:
        tx = ev.get("txType")
        if tx == "create":
            await self.on_create(ev)
        elif tx in ("buy", "sell") and ev.get("mint") in self.watches:
            self.on_trade(ev)

    async def on_create(self, ev: dict[str, Any]) -> None:
        now = self.clock()
        w = TokenWatch.from_create(ev, now, self.cfg.mayhem_keys)
        if not w.mint:
            return
        self.stats["launches"] += 1
        if w.mayhem:
            self.stats["mayhem"] += 1
        elif (self.cfg.only_mayhem and not self._warned_no_mayhem
              and self.stats["launches"] >= 200 and self.stats["mayhem"] == 0):
            self._warned_no_mayhem = True
            log.warning(
                "Seen %d launches but none flagged as Mayhem via keys %s. The feed may not expose "
                "the flag under these names - adjust sniper.mayhem_keys or set only_mayhem=false.",
                self.stats["launches"], self.cfg.mayhem_keys,
            )
        if self.cfg.only_mayhem and not w.mayhem:
            return
        if w.creator in self.cfg.creator_blacklist:
            return
        self.watches[w.mint] = w
        log.debug("Watching %s (%s) mcap=%.1f SOL", w.symbol, w.mint, w.mcap_sol)
        if self.subscribe:
            await self.subscribe([w.mint])

    def on_trade(self, ev: dict[str, Any]) -> None:
        now = self.clock()
        w = self.watches[ev["mint"]]
        w.apply_trade(ev, now)
        if w.mint in self.book.positions:
            self._check_exit(w.mint, now)
        else:
            self._check_entry(w, now)

    async def housekeeping(self) -> None:
        """Time-based exits and pruning stale watches. Call ~once a second."""
        now = self.clock()
        for mint in list(self.book.positions):
            self._check_exit(mint, now)
        stale = [
            m for m, w in self.watches.items()
            if m not in self.book.positions and m not in self.pending
            and w.age(now) > max(self.cfg.watch_ttl_sec, self.cfg.max_age_sec)
        ]
        for m in stale:
            del self.watches[m]
        if stale and self.unsubscribe:
            await self.unsubscribe(stale)

    # ---- decisions -----------------------------------------------------
    def entry_block_reason(self, now: float) -> str | None:
        if Path(self.kill_switch_file).exists():
            return "kill switch"
        if self.book.pnl_today(now) <= -self.cfg.daily_loss_limit_sol:
            return "daily loss limit"
        open_count = len(self.book.positions) + len(self.pending - set(self.book.positions))
        if open_count >= self.cfg.max_open_positions:
            return "max positions"
        if self.paper and self.book.sol_balance < self.cfg.buy_sol:
            return "insufficient paper SOL"
        return None

    def _check_entry(self, w: TokenWatch, now: float) -> None:
        if w.mint in self.book.attempted or w.mint in self.pending:
            return
        if entry_rejections(w, self.cfg, now) or self.entry_block_reason(now):
            return
        self.book.attempted.add(w.mint)
        self.pending.add(w.mint)
        self._spawn(self._do_buy(w))

    def _check_exit(self, mint: str, now: float) -> None:
        if mint in self.pending:
            return
        pos = self.book.positions[mint]
        reason = exit_reason(pos, self.watches.get(mint), self.cfg, now)
        if reason:
            self.pending.add(mint)
            self._spawn(self._do_sell(mint, reason))

    # ---- execution -----------------------------------------------------
    def _spawn(self, coro) -> None:
        task = asyncio.ensure_future(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def drain(self) -> None:
        while self.tasks:
            await asyncio.gather(*list(self.tasks), return_exceptions=True)

    async def _do_buy(self, w: TokenWatch) -> None:
        try:
            fill = await asyncio.to_thread(self.executor.buy, w, self.cfg.buy_sol)
            if fill is None or fill.tokens <= 0:
                return
            now = self.clock()
            entry = fill.sol / fill.tokens
            self.book.positions[w.mint] = SnipePosition(
                w.mint, w.symbol, entry, fill.tokens, fill.sol, now, entry, fill.signature
            )
            self.book.sol_balance -= fill.sol
            self.stats["buys"] += 1
            log.info(
                "BUY  %-10s %.4f SOL  age=%.1fs buys=%d buyers=%d vol=%.2f SOL mcap=%.1f SOL  %s",
                w.symbol, fill.sol, w.age(now), w.buys, w.unique_buyers, w.buy_sol, w.mcap_sol, w.mint,
            )
            self._save()
        except Exception:  # noqa: BLE001
            log.exception("Buy task failed for %s", w.mint)
        finally:
            self.pending.discard(w.mint)

    async def _do_sell(self, mint: str, reason: str) -> None:
        try:
            pos = self.book.positions.get(mint)
            if pos is None:
                return
            fill = await asyncio.to_thread(self.executor.sell, pos, self.watches.get(mint))
            if fill is None:
                log.error("Sell of %s failed (%s); retrying on next tick", pos.symbol, reason)
                return
            now = self.clock()
            del self.book.positions[mint]
            trade = SnipeTrade(
                mint, pos.symbol, pos.cost_sol, fill.sol, pos.opened_at, now, reason,
                [s for s in (pos.signature, fill.signature) if s],
            )
            self.book.closed.append(trade)
            self.book.sol_balance += fill.sol
            self.stats["sells"] += 1
            log.info(
                "SELL %-10s %+.4f SOL (%+.1f%%) held=%.0fs  %s",
                pos.symbol, trade.pnl_sol, trade.pnl_pct, now - pos.opened_at, reason,
            )
            self._save()
        except Exception:  # noqa: BLE001
            log.exception("Sell task failed for %s", mint)
        finally:
            self.pending.discard(mint)

    def _save(self) -> None:
        try:
            self.book.save(self.cfg.state_file)
        except OSError as exc:
            log.warning("Could not save sniper state: %s", exc)
