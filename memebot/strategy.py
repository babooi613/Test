"""Entry filters, scoring, and exit rules."""

from __future__ import annotations

from typing import Any

from .config import FilterConfig, StrategyConfig
from .models import PairSnapshot, Position


def screen(pair: PairSnapshot, cfg: FilterConfig, now: float) -> list[str]:
    """Return a list of reasons to reject this pair (empty list = passes)."""
    r: list[str] = []
    if pair.token_address in cfg.blacklist:
        r.append("blacklisted")
    if cfg.allowed_quote_symbols and pair.quote_symbol.upper() not in {
        q.upper() for q in cfg.allowed_quote_symbols
    }:
        r.append(f"quote {pair.quote_symbol} not allowed")
    if pair.liquidity_usd < cfg.min_liquidity_usd:
        r.append(f"liquidity ${pair.liquidity_usd:,.0f} < ${cfg.min_liquidity_usd:,.0f}")
    if pair.liquidity_usd > cfg.max_liquidity_usd:
        r.append(f"liquidity ${pair.liquidity_usd:,.0f} > ${cfg.max_liquidity_usd:,.0f}")
    fdv = pair.fdv or pair.market_cap
    if fdv < cfg.min_fdv_usd:
        r.append(f"fdv ${fdv:,.0f} < ${cfg.min_fdv_usd:,.0f}")
    if fdv > cfg.max_fdv_usd:
        r.append(f"fdv ${fdv:,.0f} > ${cfg.max_fdv_usd:,.0f}")
    age = pair.age_minutes(now)
    if age is None:
        r.append("unknown age")
    else:
        if age < cfg.min_age_minutes:
            r.append(f"too new ({age:.0f}m)")
        if age > cfg.max_age_hours * 60:
            r.append(f"too old ({age / 60:.1f}h)")
    if pair.volume_h1 < cfg.min_volume_h1_usd:
        r.append(f"h1 volume ${pair.volume_h1:,.0f} < ${cfg.min_volume_h1_usd:,.0f}")
    if pair.txns_h1 < cfg.min_txns_h1:
        r.append(f"h1 txns {pair.txns_h1} < {cfg.min_txns_h1}")
    if pair.buy_ratio_h1 < cfg.min_buy_ratio_h1:
        r.append(f"buy ratio {pair.buy_ratio_h1:.2f} < {cfg.min_buy_ratio_h1:.2f}")
    if pair.change_h1 < cfg.min_price_change_h1:
        r.append(f"h1 change {pair.change_h1:.1f}% < {cfg.min_price_change_h1:.1f}%")
    if pair.change_h1 > cfg.max_price_change_h1:
        r.append(f"h1 change {pair.change_h1:.1f}% > {cfg.max_price_change_h1:.1f}% (overextended)")
    if pair.change_m5 > cfg.max_price_change_m5:
        r.append(f"m5 change {pair.change_m5:.1f}% > {cfg.max_price_change_m5:.1f}% (overextended)")
    return r


def screen_rugcheck(report: dict[str, Any] | None, cfg: FilterConfig) -> list[str]:
    """Reject tokens RugCheck flags as dangerous. Missing report => reject (fail safe)."""
    if report is None:
        return ["rugcheck report unavailable"]
    r: list[str] = []
    score = report.get("score")
    if isinstance(score, (int, float)) and score > cfg.rugcheck_max_score:
        r.append(f"rugcheck score {score} > {cfg.rugcheck_max_score}")
    if cfg.rugcheck_reject_danger:
        for risk in report.get("risks") or []:
            if str(risk.get("level", "")).lower() == "danger":
                r.append(f"rugcheck danger: {risk.get('name', 'unknown')}")
    return r


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def score(pair: PairSnapshot, fcfg: FilterConfig) -> float:
    """Momentum score from 0-100.

    Components:
      * 35 pts  buy pressure (h1 buy ratio from 0.5 -> 0.8)
      * 25 pts  short-term buy pressure (m5 buy ratio from 0.5 -> 0.8)
      * 25 pts  turnover (h1 volume / liquidity, saturates at 2x)
      * 15 pts  healthy trend (h1 change, best around the middle of the allowed band)
    """
    buy_h1 = _clamp((pair.buy_ratio_h1 - 0.5) / 0.3)
    m5_total = pair.buys_m5 + pair.sells_m5
    buy_m5 = _clamp(((pair.buys_m5 / m5_total) - 0.5) / 0.3) if m5_total else 0.0
    turnover = _clamp((pair.volume_h1 / pair.liquidity_usd) / 2.0) if pair.liquidity_usd else 0.0
    lo, hi = max(fcfg.min_price_change_h1, 0.0), fcfg.max_price_change_h1
    if hi > lo and pair.change_h1 > 0:
        mid = (lo + hi) / 2
        trend = _clamp(1.0 - abs(pair.change_h1 - mid) / ((hi - lo) / 2))
    else:
        trend = 0.0
    return round(35 * buy_h1 + 25 * buy_m5 + 25 * turnover + 15 * trend, 2)


def exit_reason(
    pos: Position, pair: PairSnapshot | None, cfg: StrategyConfig, now: float
) -> str | None:
    """Decide whether to close a position. Updates pos.peak_price as a side effect."""
    held_min = (now - pos.opened_at) / 60.0
    if pair is None:
        # Pair disappeared from DexScreener; only time-based exit is possible.
        return "max_hold (no data)" if held_min >= cfg.max_hold_minutes else None

    price = pair.price_usd
    if price > pos.peak_price:
        pos.peak_price = price
    pnl = pos.pnl_pct(price)

    if pos.entry_liquidity_usd > 0:
        drop = (1 - pair.liquidity_usd / pos.entry_liquidity_usd) * 100
        if drop >= cfg.liquidity_drop_exit_pct:
            return f"liquidity_drop ({drop:.0f}%)"
    if pnl <= -cfg.stop_loss_pct:
        return f"stop_loss ({pnl:.1f}%)"
    if pnl >= cfg.take_profit_pct:
        return f"take_profit ({pnl:.1f}%)"
    peak_gain = pos.pnl_pct(pos.peak_price)
    if peak_gain >= cfg.trailing_activation_pct:
        from_peak = (1 - price / pos.peak_price) * 100
        if from_peak >= cfg.trailing_stop_pct:
            return f"trailing_stop ({from_peak:.1f}% off peak, pnl {pnl:.1f}%)"
    if held_min >= cfg.max_hold_minutes:
        return f"max_hold ({held_min:.0f}m, pnl {pnl:.1f}%)"
    return None
