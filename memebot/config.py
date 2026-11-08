"""Configuration loading (TOML -> typed dataclasses)."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any


@dataclass
class GeneralConfig:
    mode: str = "paper"  # "paper" or "live"
    poll_interval_sec: int = 30
    state_file: str = "state/state.json"
    log_level: str = "INFO"
    kill_switch_file: str = "STOP"  # create this file to halt new entries and exit


@dataclass
class DiscoveryConfig:
    chain: str = "solana"
    # DexScreener feeds used to find fresh tokens.
    use_latest_profiles: bool = True
    use_latest_boosts: bool = True
    # Extra token addresses to always consider (watchlist).
    watchlist: list[str] = field(default_factory=list)
    max_candidates_per_tick: int = 60


@dataclass
class FilterConfig:
    min_liquidity_usd: float = 20_000
    max_liquidity_usd: float = 5_000_000
    min_fdv_usd: float = 50_000
    max_fdv_usd: float = 20_000_000
    min_age_minutes: float = 15
    max_age_hours: float = 72
    min_volume_h1_usd: float = 10_000
    min_txns_h1: int = 100
    min_buy_ratio_h1: float = 0.55  # buys / (buys + sells)
    min_price_change_h1: float = 0.0  # percent
    max_price_change_h1: float = 300.0  # avoid buying the top of a vertical candle
    max_price_change_m5: float = 40.0
    allowed_quote_symbols: list[str] = field(default_factory=lambda: ["SOL", "USDC"])
    blacklist: list[str] = field(default_factory=list)
    # RugCheck.xyz (Solana). Higher score = riskier.
    use_rugcheck: bool = True
    rugcheck_max_score: int = 1000
    rugcheck_reject_danger: bool = True


@dataclass
class StrategyConfig:
    min_score: float = 55.0
    take_profit_pct: float = 100.0  # +100% => 2x
    stop_loss_pct: float = 25.0  # -25%
    trailing_activation_pct: float = 40.0  # trailing stop arms after +40%
    trailing_stop_pct: float = 20.0  # sell if price drops 20% from peak once armed
    max_hold_minutes: float = 240
    liquidity_drop_exit_pct: float = 50.0  # likely rug if liquidity collapses
    cooldown_minutes: float = 120  # don't re-buy a token soon after exiting


@dataclass
class RiskConfig:
    position_size_usd: float = 25.0
    max_open_positions: int = 4
    max_total_exposure_usd: float = 100.0
    daily_loss_limit_usd: float = 50.0
    max_entries_per_tick: int = 1


@dataclass
class PaperConfig:
    starting_cash_usd: float = 1_000.0
    slippage_pct: float = 1.5
    fee_pct: float = 0.5


@dataclass
class LiveConfig:
    rpc_url: str = "https://api.mainnet-beta.solana.com"
    jupiter_base_url: str = "https://lite-api.jup.ag"
    slippage_bps: int = 300
    max_price_impact_pct: float = 3.0
    priority_fee_lamports: int | str = "auto"
    private_key_env: str = "MEMEBOT_PRIVATE_KEY"  # base58 secret or path to id.json
    min_sol_reserve: float = 0.02  # always keep this much SOL for fees
    confirm_timeout_sec: int = 60


@dataclass
class SniperConfig:
    """Real-time pump.fun launch sniper (Mayhem coins). All amounts in SOL."""

    feed_url: str = "wss://pumpportal.fun/api/data"
    trade_api_url: str = "https://pumpportal.fun/api/trade-local"
    state_file: str = "state/sniper.json"
    only_mayhem: bool = True
    # Field names checked on the new-token event to detect Mayhem mode.
    mayhem_keys: list[str] = field(
        default_factory=lambda: ["isMayhemMode", "is_mayhem_mode", "mayhemMode", "mayhem"]
    )
    creator_blacklist: list[str] = field(default_factory=list)

    # --- entry: watch a fresh launch, buy only if early flow looks organic ---
    min_age_sec: float = 3.0
    max_age_sec: float = 45.0
    min_buys: int = 8
    min_unique_buyers: int = 6
    min_buy_volume_sol: float = 1.5
    min_buy_volume_ratio: float = 0.70  # buy SOL / (buy SOL + sell SOL)
    max_top_buyer_share: float = 0.40  # one wallet (excl. dev) of total buy SOL
    min_mcap_sol: float = 0.0
    max_mcap_sol: float = 150.0
    max_gain_since_launch_pct: float = 150.0  # don't chase vertical candles
    require_dev_holding: bool = True  # skip if creator has sold anything

    # --- exits (checked on every trade tick) ---
    take_profit_pct: float = 35.0
    stop_loss_pct: float = 20.0
    trailing_activation_pct: float = 20.0
    trailing_stop_pct: float = 12.0
    max_hold_sec: float = 120.0
    exit_on_dev_sell: bool = True

    # --- sizing & risk ---
    buy_sol: float = 0.05
    max_open_positions: int = 3
    daily_loss_limit_sol: float = 0.5
    watch_ttl_sec: float = 90.0  # stop tracking unbought launches after this

    # --- paper simulation ---
    paper_starting_sol: float = 5.0
    paper_fee_pct: float = 1.25  # pump.fun trading fee (approx.)
    paper_latency_slippage_pct: float = 3.0  # you are not the fastest bot

    # --- live execution (PumpPortal local-signing API) ---
    slippage_pct: int = 15
    priority_fee_sol: float = 0.0005
    pool: str = "auto"


@dataclass
class Config:
    general: GeneralConfig = field(default_factory=GeneralConfig)
    sniper: SniperConfig = field(default_factory=SniperConfig)
    discovery: DiscoveryConfig = field(default_factory=DiscoveryConfig)
    filters: FilterConfig = field(default_factory=FilterConfig)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    paper: PaperConfig = field(default_factory=PaperConfig)
    live: LiveConfig = field(default_factory=LiveConfig)


def _build(cls: type, data: dict[str, Any], path: str) -> Any:
    known = {f.name: f for f in fields(cls)}
    kwargs: dict[str, Any] = {}
    for key, value in data.items():
        if key not in known:
            raise ValueError(f"Unknown config key: {path}{key}")
        default = known[key].default_factory() if callable(known[key].default_factory) else known[key].default  # type: ignore[misc]
        if is_dataclass(default):
            if not isinstance(value, dict):
                raise ValueError(f"Config section {path}{key} must be a table")
            kwargs[key] = _build(type(default), value, f"{path}{key}.")
        else:
            kwargs[key] = value
    return cls(**kwargs)


def load_config(path: str | Path | None) -> Config:
    if path is None:
        return Config()
    with open(path, "rb") as fh:
        data = tomllib.load(fh)
    cfg: Config = _build(Config, data, "")
    validate(cfg)
    return cfg


def validate(cfg: Config) -> None:
    if cfg.general.mode not in ("paper", "live"):
        raise ValueError("general.mode must be 'paper' or 'live'")
    if cfg.risk.position_size_usd <= 0:
        raise ValueError("risk.position_size_usd must be > 0")
    if cfg.risk.max_total_exposure_usd < cfg.risk.position_size_usd:
        raise ValueError("risk.max_total_exposure_usd must be >= position_size_usd")
    if not 0 < cfg.strategy.stop_loss_pct < 100:
        raise ValueError("strategy.stop_loss_pct must be between 0 and 100")
