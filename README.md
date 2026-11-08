# memebot — Solana memecoin trading bot

A momentum trading bot for Solana memecoins. It finds new tokens on DexScreener,
screens out obvious rugs, ranks the rest by on-chain momentum, and manages each
position with a stop-loss, take-profit, trailing stop, max-hold timer and a
liquidity-pull ("rug") detector.

**It runs in paper-trading mode by default.** Live trading through Jupiter has
to be turned on explicitly, in two places.

> ⚠️ **Read this first.** Memecoins are extremely risky. Most go to zero, and many
> are built to scam buyers (honeypots, freeze authority, dev dumps, bundled
> launches). No filter catches them all. This software comes with no warranty
> and does not promise any profit. Paper trade first, and only trade live with
> money you can afford to lose completely. Use a separate hot wallet, never your
> main one.

## How it works

Each tick (every `poll_interval_sec`):

1. **Exits.** Refresh prices for open positions and sell when one of these fires:
   - `liquidity_drop`: pool liquidity has fallen ≥ 50% since entry (likely rug)
   - `stop_loss`: down ≥ 25%
   - `take_profit`: up ≥ 100%
   - `trailing_stop`: after a gain of +40%, sell if the price falls 20% from its peak
   - `max_hold`: held longer than 240 minutes
2. **Risk gate.** No new buys if the kill switch is on, the daily loss limit
   is hit, the bot holds its maximum number of positions, or total exposure
   is at its cap.
3. **Discovery.** Pull the latest boosted tokens and token profiles from
   DexScreener, plus your watchlist.
4. **Screening.** Use the most liquid pair for each token and reject any that
   fail on liquidity, FDV, age, volume, transaction count, buy/sell ratio, or
   a price that has already gone vertical. Tokens that pass are checked on
   **RugCheck.xyz**: anything marked "danger" (mint or freeze authority still
   set, and so on) or with a high risk score is rejected. If the report can't
   be fetched, the token is rejected too.
5. **Scoring (0–100).** h1 buy pressure (35), m5 buy pressure (25),
   turnover = volume/liquidity (25), and a trend score that is highest in the
   middle of the allowed h1 price-change band (15).
6. **Entry.** Buy the top-scoring candidates at a fixed USD size.

State (cash, positions, trade history, cooldowns) is saved atomically to
`state/state.json`, so the bot picks up where it left off after a restart.

## Install

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt        # requests + solders (solders only needed for live)
cp config.example.toml config.toml     # then edit
```

Requires Python 3.11+.

## Usage

```bash
# See what passes the filters right now (no trading)
python -m memebot scan -c config.toml
python -m memebot scan -c config.toml --all   # include rejected tokens and reasons

# Paper trade (default)
python -m memebot run -c config.toml
python -m memebot run -c config.toml --once   # single tick

# Portfolio, open positions and recent trades
python -m memebot status -c config.toml

# Stop opening new positions (the bot exits once all positions are closed)
touch STOP
```

## Live trading

1. Create a **new, dedicated** wallet and fund it with a small amount of SOL.
2. In `config.toml` set `mode = "live"` under `[general]`, and set a reliable
   `rpc_url` (the public RPC is rate-limited; Helius, Triton, QuickNode and
   similar providers work better).
3. Export the key, either as base58 or as a path to a Solana CLI `id.json`:
   ```bash
   export MEMEBOT_PRIVATE_KEY=~/.config/solana/memebot.json
   ```
4. Run with the confirmation flag:
   ```bash
   python -m memebot run -c config.toml --live
   ```

Live buys swap SOL → token via Jupiter, and sells swap the full token balance
back to SOL. Transactions are signed on your machine and sent through your RPC;
the key never leaves your computer. A swap is refused if Jupiter's quoted price
impact is above `max_price_impact_pct`, and `min_sol_reserve` SOL is always kept
back for fees.

## Tuning tips

- Start with `scan --all` to see why tokens are rejected, then loosen or
  tighten the filters.
- `min_age_minutes` skips the sniper/bundle chaos right after launch. Lowering
  it means more upside and much more rug risk.
- Paper fills assume `slippage_pct` + `fee_pct` on each side. Real memecoin
  slippage is often worse, so don't trust paper results that only work with
  tiny slippage.
- Keep `position_size_usd` small compared with pool liquidity, or you will
  move the price yourself.

## Project layout

```
memebot/
  config.py     TOML config -> dataclasses (unknown keys are rejected)
  models.py     PairSnapshot, Position, ClosedTrade, Fill
  market.py     DexScreener + RugCheck clients
  strategy.py   screening filters, momentum score, exit rules
  portfolio.py  positions, PnL, cooldowns, persistence
  execution.py  PaperExecutor, JupiterExecutor (Solana live trading)
  bot.py        main loop + risk management
  __main__.py   CLI (run / scan / status)
tests/          pytest suite (offline, uses fake market data)
```

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```
