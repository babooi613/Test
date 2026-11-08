"""The main trading loop."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config
from .execution import Executor
from .market import DexScreener, RugCheck, best_pair_per_token
from .models import PairSnapshot
from .portfolio import Portfolio
from .strategy import exit_reason, score, screen, screen_rugcheck

log = logging.getLogger(__name__)


@dataclass
class Candidate:
    pair: PairSnapshot
    score: float
    rejections: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.rejections


class TradingBot:
    def __init__(
        self,
        cfg: Config,
        executor: Executor,
        portfolio: Portfolio,
        dex: DexScreener | None = None,
        rugcheck: RugCheck | None = None,
        clock=time.time,
    ):
        self.cfg = cfg
        self.executor = executor
        self.portfolio = portfolio
        self.dex = dex or DexScreener()
        self.rugcheck = rugcheck if rugcheck is not None else (RugCheck() if cfg.filters.use_rugcheck else None)
        self.clock = clock
        self.last_prices: dict[str, float] = {}

    # ------------------------------------------------------------------
    def discover_tokens(self) -> list[str]:
        d = self.cfg.discovery
        tokens: list[str] = list(d.watchlist)
        for enabled, fetch in (
            (d.use_latest_boosts, self.dex.latest_boosts),
            (d.use_latest_profiles, self.dex.latest_profiles),
        ):
            if not enabled:
                continue
            try:
                tokens.extend(fetch(d.chain))
            except RuntimeError as exc:
                log.warning("Discovery feed failed: %s", exc)
        unique = list(dict.fromkeys(tokens))
        return unique[: d.max_candidates_per_tick]

    def evaluate(self, tokens: list[str], now: float) -> list[Candidate]:
        chain = self.cfg.discovery.chain
        pairs = best_pair_per_token(self.dex.pairs_for_tokens(chain, tokens), chain)
        out: list[Candidate] = []
        for token, pair in pairs.items():
            rej = screen(pair, self.cfg.filters, now)
            if token in self.portfolio.positions:
                rej.append("already held")
            if self.portfolio.in_cooldown(token, now):
                rej.append("in cooldown")
            s = score(pair, self.cfg.filters)
            if not rej and s < self.cfg.strategy.min_score:
                rej.append(f"score {s:.1f} < {self.cfg.strategy.min_score}")
            # Only hit RugCheck for tokens that passed everything else (rate limits).
            if not rej and self.rugcheck is not None and chain == "solana":
                rej.extend(screen_rugcheck(self.rugcheck.report(token), self.cfg.filters))
            out.append(Candidate(pair, s, rej))
        out.sort(key=lambda c: c.score, reverse=True)
        return out

    def scan(self) -> list[Candidate]:
        return self.evaluate(self.discover_tokens(), self.clock())

    # ------------------------------------------------------------------
    def kill_switch_active(self) -> bool:
        return Path(self.cfg.general.kill_switch_file).exists()

    def entry_block_reason(self, now: float) -> str | None:
        r = self.cfg.risk
        pf = self.portfolio
        if self.kill_switch_active():
            return "kill switch file present"
        pnl_today = pf.realized_pnl_today(now)
        if pnl_today <= -r.daily_loss_limit_usd:
            return f"daily loss limit hit (${pnl_today:,.2f})"
        if len(pf.positions) >= r.max_open_positions:
            return "max open positions"
        if pf.exposure_usd() + r.position_size_usd > r.max_total_exposure_usd:
            return "max exposure"
        if self.cfg.general.mode == "paper" and pf.cash_usd < r.position_size_usd:
            return "insufficient paper cash"
        return None

    def manage_exits(self, now: float) -> None:
        pf = self.portfolio
        if not pf.positions:
            return
        chain = self.cfg.discovery.chain
        pairs = {p.pair_address: p for p in self.dex.pairs_for_tokens(chain, list(pf.positions))}
        for token, pos in list(pf.positions.items()):
            pair = pairs.get(pos.pair_address)
            if pair is not None:
                self.last_prices[token] = pair.price_usd
            reason = exit_reason(pos, pair, self.cfg.strategy, now)
            if reason is None:
                if pair is not None:
                    log.debug("Holding %s at %+.1f%%", pos.symbol, pos.pnl_pct(pair.price_usd))
                continue
            fill = self.executor.sell(pos, pair)
            if fill is None:
                log.error("Exit for %s (%s) failed; will retry next tick", pos.symbol, reason)
                continue
            trade = pf.close(token, fill, reason, now, self.cfg.strategy.cooldown_minutes * 60)
            log.info(
                "SELL %s  reason=%s  pnl=$%+.2f (%+.1f%%)",
                trade.symbol, reason, trade.pnl_usd, trade.pnl_pct,
            )

    def manage_entries(self, now: float) -> None:
        block = self.entry_block_reason(now)
        if block:
            log.info("New entries paused: %s", block)
            return
        candidates = [c for c in self.evaluate(self.discover_tokens(), now) if c.ok]
        log.info("%d candidate(s) passed filters", len(candidates))
        entries = 0
        for cand in candidates:
            if entries >= self.cfg.risk.max_entries_per_tick or self.entry_block_reason(now):
                break
            fill = self.executor.buy(cand.pair, self.cfg.risk.position_size_usd)
            if fill is None:
                continue
            self.portfolio.open(cand.pair, fill, cand.score, now)
            self.last_prices[cand.pair.token_address] = cand.pair.price_usd
            entries += 1
            log.info(
                "BUY  %s  $%.2f @ $%.8f  score=%.1f  liq=$%,.0f  %s",
                cand.pair.symbol, fill.usd_value, fill.price_usd, cand.score,
                cand.pair.liquidity_usd, cand.pair.url,
            )

    def tick(self) -> None:
        now = self.clock()
        try:
            self.manage_exits(now)
        except Exception:  # noqa: BLE001
            log.exception("Error managing exits")
        try:
            self.manage_entries(now)
        except Exception:  # noqa: BLE001
            log.exception("Error managing entries")
        self.portfolio.save(self.cfg.general.state_file)

    def run(self, once: bool = False) -> None:
        log.info(
            "Starting memebot in %s mode on %s (poll every %ss)",
            self.cfg.general.mode.upper(), self.cfg.discovery.chain, self.cfg.general.poll_interval_sec,
        )
        while True:
            self.tick()
            if once:
                return
            if self.kill_switch_active() and not self.portfolio.positions:
                log.info("Kill switch present and no open positions; exiting.")
                return
            time.sleep(self.cfg.general.poll_interval_sec)
