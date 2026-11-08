"""Command-line interface.

    python -m memebot run    [--config config.toml] [--once] [--live]
    python -m memebot scan   [--config config.toml] [--all]
    python -m memebot status [--config config.toml]
    python -m memebot snipe  [--config config.toml] [--live]   # real-time Mayhem sniper
    python -m memebot snipe-status [--config config.toml]
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


def cmd_snipe(args, cfg) -> int:
    import asyncio

    from .pumpportal import PumpPortalExecutor, PumpPortalFeed
    from .sniper import PaperSnipeExecutor, SnipeBook, SniperEngine

    s = cfg.sniper
    live = cfg.general.mode == "live"
    if live != bool(args.live):
        print("Live sniping needs BOTH general.mode = \"live\" in the config AND the --live flag.")
        return 2
    executor = PumpPortalExecutor(s, cfg.live) if live else PaperSnipeExecutor(s)
    book = SnipeBook.load(s.state_file, 0.0 if live else s.paper_starting_sol)
    checker = None
    if s.only_mayhem and s.mayhem_check_onchain:
        from .pumpchain import MayhemChecker

        checker = MayhemChecker(cfg.live.rpc_url)
    engine = SniperEngine(s, executor, book, paper=not live,
                          kill_switch_file=cfg.general.kill_switch_file, mayhem_checker=checker)
    feed = PumpPortalFeed(s.feed_url, engine)
    logging.getLogger(__name__).info(
        "Sniping %s launches in %s mode: %.4f SOL per trade, TP +%.0f%% / SL -%.0f%% / max hold %.0fs",
        "Mayhem" if s.only_mayhem else "all pump.fun", "LIVE" if live else "PAPER",
        s.buy_sol, s.take_profit_pct, s.stop_loss_pct, s.max_hold_sec,
    )
    try:
        asyncio.run(feed.run())
    except KeyboardInterrupt:
        book.save(s.state_file)
        print("\nStopped. State saved.")
    return 0


def cmd_snipe_status(args, cfg) -> int:
    from .sniper import SnipeBook

    s = cfg.sniper
    book = SnipeBook.load(s.state_file, s.paper_starting_sol)
    now = time.time()
    total = sum(t.pnl_sol for t in book.closed)
    wins = sum(1 for t in book.closed if t.pnl_sol > 0)
    print(f"SOL balance (paper) / net flow (live): {book.sol_balance:.4f}")
    print(f"PnL today: {book.pnl_today(now):+.4f} SOL   all-time: {total:+.4f} SOL")
    if book.closed:
        print(f"Trades: {len(book.closed)}  win rate: {wins / len(book.closed):.0%}")
    print("\nOpen positions:")
    for p in book.positions.values():
        print(f"  {p.symbol:<10} {p.cost_sol:.4f} SOL  held {now - p.opened_at:.0f}s  {p.mint}")
    if not book.positions:
        print("  (none)")
    print("\nLast 15 trades:")
    for t in book.closed[-15:]:
        print(f"  {t.symbol:<10} {t.pnl_sol:+.4f} SOL ({t.pnl_pct:+6.1f}%) {t.closed_at - t.opened_at:4.0f}s  {t.reason}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="memebot", description="Solana memecoin trading bot")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("run", "scan", "status", "snipe", "snipe-status"):
        sp = sub.add_parser(name)
        sp.add_argument("--config", "-c", default=None, help="Path to config TOML")
        if name in ("run", "snipe"):
            sp.add_argument("--live", action="store_true", help="Confirm live trading with real funds")
        if name == "run":
            sp.add_argument("--once", action="store_true", help="Run a single tick and exit")
        if name == "scan":
            sp.add_argument("--all", action="store_true", help="Also show rejected tokens")
    args = parser.parse_args(argv)
    cfg = load_config(args.config)
    _setup_logging(cfg.general.log_level)
    return {
        "run": cmd_run, "scan": cmd_scan, "status": cmd_status,
        "snipe": cmd_snipe, "snipe-status": cmd_snipe_status,
    }[args.command](args, cfg)


if __name__ == "__main__":
    sys.exit(main())
