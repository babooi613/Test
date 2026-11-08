"""Command-line interface.

    python -m memebot run    [--config config.toml] [--once] [--live]
    python -m memebot scan   [--config config.toml] [--all]
    python -m memebot status [--config config.toml]
"""

from __future__ import annotations

import argparse
import logging
import sys
import time

from .bot import TradingBot
from .config import load_config
from .execution import JupiterExecutor, PaperExecutor
from .portfolio import Portfolio


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )


def cmd_run(args, cfg) -> int:
    if cfg.general.mode == "live":
        if not args.live:
            print("Config is in live mode; pass --live to confirm you want to trade real funds.")
            return 2
        executor = JupiterExecutor(cfg.live)
        default_cash = 0.0
    else:
        if args.live:
            print("--live given but general.mode is 'paper'. Set mode = \"live\" in the config.")
            return 2
        executor = PaperExecutor(cfg.paper)
        default_cash = cfg.paper.starting_cash_usd
    portfolio = Portfolio.load(cfg.general.state_file, default_cash)
    bot = TradingBot(cfg, executor, portfolio)
    try:
        bot.run(once=args.once)
    except KeyboardInterrupt:
        portfolio.save(cfg.general.state_file)
        print("\nStopped. State saved.")
    return 0


def cmd_scan(args, cfg) -> int:
    bot = TradingBot(cfg, PaperExecutor(cfg.paper), Portfolio())
    cands = bot.scan()
    shown = cands if args.all else [c for c in cands if c.ok]
    if not shown:
        print(f"No candidates passed filters ({len(cands)} evaluated). Use --all to see rejections.")
    for c in shown:
        p = c.pair
        status = "PASS" if c.ok else "skip"
        print(
            f"[{status}] {p.symbol:<10} score={c.score:5.1f} price=${p.price_usd:<12.8g} "
            f"liq=${p.liquidity_usd:>11,.0f} fdv=${p.fdv:>13,.0f} h1={p.change_h1:+7.1f}% "
            f"buys={p.buy_ratio_h1:.2f}  {p.token_address}"
        )
        if c.rejections:
            print("         - " + "; ".join(c.rejections))
    return 0


def cmd_status(args, cfg) -> int:
    pf = Portfolio.load(cfg.general.state_file, cfg.paper.starting_cash_usd)
    now = time.time()
    label = "Cash" if cfg.general.mode == "paper" else "Net cash flow"
    print(f"Mode: {cfg.general.mode}   {label}: ${pf.cash_usd:,.2f}   Exposure: ${pf.exposure_usd():,.2f}")
    print(f"Realized PnL today: ${pf.realized_pnl_today(now):+,.2f}   all-time: ${pf.realized_pnl_since(0):+,.2f}")
    wins = [t for t in pf.closed if t.pnl_usd > 0]
    if pf.closed:
        print(f"Closed trades: {len(pf.closed)}  win rate: {len(wins) / len(pf.closed) * 100:.0f}%")
    print("\nOpen positions:")
    if not pf.positions:
        print("  (none)")
    for p in pf.positions.values():
        held = (now - p.opened_at) / 60
        print(f"  {p.symbol:<10} cost=${p.cost_usd:,.2f} entry=${p.entry_price:.8g} held={held:.0f}m  {p.token_address}")
    print("\nLast 10 trades:")
    for t in pf.closed[-10:]:
        print(f"  {t.symbol:<10} {t.pnl_usd:+9.2f} ({t.pnl_pct:+6.1f}%)  {t.reason}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="memebot", description="Solana memecoin trading bot")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("run", "scan", "status"):
        sp = sub.add_parser(name)
        sp.add_argument("--config", "-c", default=None, help="Path to config TOML")
        if name == "run":
            sp.add_argument("--once", action="store_true", help="Run a single tick and exit")
            sp.add_argument("--live", action="store_true", help="Confirm live trading with real funds")
        if name == "scan":
            sp.add_argument("--all", action="store_true", help="Also show rejected tokens")
    args = parser.parse_args(argv)
    cfg = load_config(args.config)
    _setup_logging(cfg.general.log_level)
    return {"run": cmd_run, "scan": cmd_scan, "status": cmd_status}[args.command](args, cfg)


if __name__ == "__main__":
    sys.exit(main())
