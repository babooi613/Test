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

## Snipe mode: real-time pump.fun Mayhem coins

`python -m memebot snipe` is a second, much faster bot for the "Mayhem" tab:
coins that are seconds old, with market caps from a few hundred to a few thousand dollars.
It does not poll. It keeps one websocket open to PumpPortal, sees every new
pump.fun launch as it happens, streams every trade for the coins it watches, and
**re-checks its exits on every single trade**.

```bash
python -m memebot snipe -c config.toml          # paper (default, simulated SOL)
python -m memebot snipe-status -c config.toml   # PnL, win rate, recent trades
python -m memebot snipe -c config.toml --live   # real SOL (also needs mode = "live")
```

**Lifecycle of one coin**

1. **Launch.** A new token event arrives. If `only_mayhem = true`, coins
   without a Mayhem flag are ignored. Otherwise the bot starts watching the coin.
2. **Watch window** (`min_age_sec` to `max_age_sec`, default 3–45s). On each
   trade it updates the bonding-curve price, buy/sell counts, SOL volume,
   unique buyers, and how much of the buying comes from the single biggest wallet.
3. **Entry.** It buys `buy_sol` once all of these are true: ≥ 8 buys from
   ≥ 6 different wallets, ≥ 1.5 SOL of buy volume, ≥ 70% of volume is buys, no
   single wallet is > 40% of buying (bundles and self-pumps), the dev hasn't
   sold, and the price is not already +150% since launch. It gets **one shot
   per coin**.
4. **Exit**, checked on every trade:
   - `dev_sold`: the creator sold anything
   - `stop_loss`: −20%
   - `take_profit`: +35%
   - `trailing_stop`: after +20%, sell if it drops 12% from the peak
   - `max_hold`: 120 seconds (checked every second even with no trades)
5. Coins that never qualify are dropped after `watch_ttl_sec`.

Buys and sells run in background threads, so a slow transaction never stalls
the feed. Daily loss limit, max open positions and the `STOP` kill switch all
still apply.

**Paper mode is deliberately pessimistic.** Fills are calculated on the actual
bonding curve, with the pump.fun fee and an extra 3% `paper_latency_slippage_pct`
on each side. Professional snipers run co-located with Jito bundles and will
nearly always be in before you and out before you. If a config is only
profitable in paper with that haircut set to 0, it won't work live.

**Mayhem detection.** pump.fun records Mayhem mode on-chain. `create_v2` sets
`is_mayhem_mode` on the coin's bonding-curve account, and it never changes after
that (see the official IDL in
[pump-fun/pump-public-docs](https://github.com/pump-fun/pump-public-docs), `idl/pump.json`).
If the PumpPortal event already includes a flag (under any of the names in
`mayhem_keys`), the bot uses it. Otherwise it watches the coin while reading byte 81
of the bonding-curve account through `[live] rpc_url`. Coins that turn out not to be
Mayhem are dropped. Coins whose status can't be confirmed are **never bought**.
Each launch costs one `getAccountInfo` call, so on busy days use a paid RPC.
Set `only_mayhem = false` to trade every pump.fun launch with the same filters.

**PumpPortal API key required for trade data.** PumpPortal streams new launches
for free, but per-coin trade streams (`subscribeTokenTrade`), which the entry and
exit rules rely on, only work with an API key funded with at least 0.02 SOL.
Create one at pumpportal.fun and `export MEMEBOT_PUMPPORTAL_KEY=...`. Without
it, the bot logs an ERROR and never buys.

**Live execution** uses PumpPortal's `trade-local` API: PumpPortal builds the
transaction, your machine signs it, and it is sent through your own RPC. Your
key is never uploaded. Use a fast paid RPC, and raise `priority_fee_sol` if
your transactions land late.

### Graduated coins

With `[graduated] enabled = true`, the sniper also subscribes to PumpPortal's
migration feed. When a coin completes its bonding curve and moves to PumpSwap,
the bot watches its trades for `max_age_sec` (default 5 minutes) and buys only
on sustained, spread-out buying: at least 15 buys from at least 10 wallets, at
least 5 SOL of volume, at least 60% of volume from buys, and no wallet above 35%
of buying. Exits are tighter than for Mayhem coins (TP +30%, SL −15%, trail 10%
after +15%, max hold 5 min), because the drop right after migration is common.
Prices come from pool reserves when the feed includes them, otherwise from
market cap divided by the 1B supply. Both modes share the position cap and the
daily loss limit. Trades and `snipe-status` are labelled `mayhem` or `graduated`.

### Tuning with real data (record → backtest)

The sniper records every event it receives to `state/events/events-YYYYMMDD.jsonl`.
After it has run for a few hours:

```bash
python -m memebot backtest -c config.toml                        # replay with current settings
python -m memebot backtest -c config.toml --tune mayhem graduated # random-search settings
```

The replay runs the same engine and paper fills on a simulated clock. Tuning
searches on the first 60% of the data and scores each candidate on the last 40%,
which it never saw. A candidate is only worth using if it also makes money on
that unseen part. Anything that only wins on the training data is overfit.

**Bot wallets.** The Mayhem AI agent and sniper bots buy many coins at once.
Wallets that bought at least `bot_wallet_min_coins` different coins within
`bot_wallet_window_sec` are left out of the unique-buyer and top-buyer counts,
so those filters measure real demand.

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
  sniper.py     real-time launch sniper: entry/exit rules, paper curve fills, engine
  pumpportal.py PumpPortal websocket feed + live pump.fun executor
  pumpchain.py  on-chain pump.fun reads (Mayhem flag from the bonding curve)
  __main__.py   CLI (run / scan / status)
tests/          pytest suite (offline, uses fake market data)
```

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```
