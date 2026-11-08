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
class Config:
    general: GeneralConfig = field(default_factory=GeneralConfig)
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
