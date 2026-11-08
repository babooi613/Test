import time

import pytest

from memebot.bot import TradingBot
from memebot.config import Config, load_config
from memebot.execution import PaperExecutor
from memebot.models import PairSnapshot, Position
from memebot.portfolio import Portfolio
from memebot.strategy import exit_reason, score, screen, screen_rugcheck

NOW = 1_800_000_000.0


def raw_pair(token="TOKEN1", price="0.001", liq=50_000, fdv=500_000, age_min=120,
             h1_change=30, m5_change=5, buys=300, sells=150, vol_h1=60_000, quote="SOL",
             pair="PAIR1"):
    return {
        "chainId": "solana",
        "dexId": "raydium",
        "pairAddress": pair,
        "url": f"https://dexscreener.com/solana/{pair}",
        "baseToken": {"address": token, "name": token.title(), "symbol": token[:4]},
        "quoteToken": {"address": "So11111111111111111111111111111111111111112", "symbol": quote},
        "priceUsd": price,
        "txns": {"m5": {"buys": 40, "sells": 15}, "h1": {"buys": buys, "sells": sells}},
        "volume": {"m5": 5_000, "h1": vol_h1, "h24": 400_000},
        "priceChange": {"m5": m5_change, "h1": h1_change, "h6": 50, "h24": 120},
        "liquidity": {"usd": liq},
        "fdv": fdv,
        "marketCap": fdv,
        "pairCreatedAt": int((NOW - age_min * 60) * 1000),
    }


def make_pair(**kw) -> PairSnapshot:
    return PairSnapshot.from_api(raw_pair(**kw))


class FakeDex:
    def __init__(self, pairs):
        self.pairs = {p["baseToken"]["address"]: p for p in pairs}

    def latest_boosts(self, chain):
        return list(self.pairs)

    def latest_profiles(self, chain):
        return []

    def pairs_for_tokens(self, chain, addresses):
        return [PairSnapshot.from_api(self.pairs[a]) for a in addresses if a in self.pairs]


class FakeRug:
    def __init__(self, report):
        self._report = report

    def report(self, mint):
        return self._report


# ---------------------------------------------------------------- filters
def test_good_pair_passes_screen():
    assert screen(make_pair(), Config().filters, NOW) == []


@pytest.mark.parametrize(
    "kw,needle",
    [
        ({"liq": 1_000}, "liquidity"),
        ({"age_min": 2}, "too new"),
        ({"age_min": 60 * 100}, "too old"),
        ({"buys": 100, "sells": 200}, "buy ratio"),
        ({"h1_change": 900}, "overextended"),
        ({"quote": "WEIRD"}, "quote"),
        ({"fdv": 1_000}, "fdv"),
    ],
)
def test_screen_rejections(kw, needle):
    reasons = screen(make_pair(**kw), Config().filters, NOW)
    assert any(needle in r for r in reasons), reasons


def test_rugcheck_screen():
    cfg = Config().filters
    assert screen_rugcheck({"score": 100, "risks": []}, cfg) == []
    assert screen_rugcheck(None, cfg)  # fail safe
    assert screen_rugcheck({"score": 50_000, "risks": []}, cfg)
    assert screen_rugcheck({"score": 1, "risks": [{"name": "Mint authority", "level": "danger"}]}, cfg)


def test_score_range_and_ordering():
    f = Config().filters
    strong = score(make_pair(buys=400, sells=100, vol_h1=100_000, h1_change=150), f)
    weak = score(make_pair(buys=160, sells=140, vol_h1=5_000, h1_change=2), f)
    assert 0 <= weak < strong <= 100


# ---------------------------------------------------------------- exits
def _pos(entry=1.0, liq=50_000):
    return Position("T", "T", "PAIR1", entry, 10, 10, NOW, entry, liq)


def test_exit_rules():
    s = Config().strategy
    assert exit_reason(_pos(), make_pair(price="1.0"), s, NOW + 60) is None
    assert exit_reason(_pos(), make_pair(price="0.7"), s, NOW + 60).startswith("stop_loss")
    assert exit_reason(_pos(), make_pair(price="2.1"), s, NOW + 60).startswith("take_profit")
    assert exit_reason(_pos(), make_pair(price="1.0", liq=10_000), s, NOW).startswith("liquidity_drop")
    assert exit_reason(_pos(), make_pair(price="1.0"), s, NOW + 999 * 60).startswith("max_hold")


def test_trailing_stop():
    s = Config().strategy
    pos = _pos()
    assert exit_reason(pos, make_pair(price="1.8"), s, NOW) is None  # +80%, arms trailing
    assert pos.peak_price == pytest.approx(1.8)
    assert exit_reason(pos, make_pair(price="1.6"), s, NOW) is None  # 11% off peak
    assert exit_reason(pos, make_pair(price="1.4"), s, NOW).startswith("trailing_stop")


# ---------------------------------------------------------------- end-to-end paper
def test_paper_round_trip(tmp_path):
    cfg = Config()
    cfg.general.state_file = str(tmp_path / "state.json")
    cfg.general.kill_switch_file = str(tmp_path / "STOP")
    clock = [NOW]
    dex = FakeDex([raw_pair(token="GOOD", pair="P1"), raw_pair(token="BAD", pair="P2", liq=500)])
    pf = Portfolio(cash_usd=1000)
    bot = TradingBot(cfg, PaperExecutor(cfg.paper), pf, dex=dex,
                     rugcheck=FakeRug({"score": 10, "risks": []}), clock=lambda: clock[0])

    bot.tick()
    assert list(pf.positions) == ["GOOD"]
    assert pf.cash_usd == pytest.approx(1000 - cfg.risk.position_size_usd)

    # Price doubles+ -> take profit.
    dex.pairs["GOOD"]["priceUsd"] = "0.0025"
    clock[0] += 300
    bot.tick()
    assert pf.positions == {}
    assert len(pf.closed) == 1 and pf.closed[0].pnl_usd > 0
    assert pf.closed[0].reason.startswith("take_profit")

    # Cooldown prevents immediate re-entry.
    clock[0] += 60
    bot.tick()
    assert pf.positions == {}

    # State persists.
    loaded = Portfolio.load(cfg.general.state_file, 0)
    assert len(loaded.closed) == 1 and loaded.cash_usd == pytest.approx(pf.cash_usd)


def test_risk_limits_block_entries(tmp_path):
    cfg = Config()
    cfg.general.state_file = str(tmp_path / "state.json")
    cfg.general.kill_switch_file = str(tmp_path / "STOP")
    cfg.risk.max_entries_per_tick = 10
    cfg.risk.max_open_positions = 2
    dex = FakeDex([raw_pair(token=f"T{i}", pair=f"P{i}") for i in range(5)])
    pf = Portfolio(cash_usd=1000)
    bot = TradingBot(cfg, PaperExecutor(cfg.paper), pf, dex=dex,
                     rugcheck=FakeRug({"score": 1, "risks": []}), clock=lambda: NOW)
    bot.tick()
    assert len(pf.positions) == 2

    (tmp_path / "STOP").touch()
    assert bot.entry_block_reason(NOW) == "kill switch file present"


def test_daily_loss_limit(tmp_path):
    cfg = Config()
    cfg.general.kill_switch_file = str(tmp_path / "STOP")
    pf = Portfolio(cash_usd=1000)
    from memebot.models import ClosedTrade
    pf.closed.append(ClosedTrade("X", "X", 1, 0.1, 1, 100, 10, NOW - 100, NOW - 10, "stop_loss"))
    bot = TradingBot(cfg, PaperExecutor(cfg.paper), pf, dex=FakeDex([]), rugcheck=FakeRug(None),
                     clock=lambda: NOW)
    assert "daily loss" in bot.entry_block_reason(NOW)


def test_example_config_loads():
    cfg = load_config("config.example.toml")
    assert cfg.general.mode == "paper"
    assert cfg.risk.position_size_usd == 25.0


def test_unknown_config_key_rejected(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text("[risk]\nposition_sise_usd = 5\n")
    with pytest.raises(ValueError, match="Unknown config key"):
        load_config(p)
