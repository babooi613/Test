import asyncio
import json

import pytest

from memebot.config import SniperConfig
from memebot.sniper import (
    PaperSnipeExecutor,
    SnipeBook,
    SniperEngine,
    SnipePosition,
    TokenWatch,
    entry_rejections,
    exit_reason,
    is_mayhem,
)

T0 = 1_800_000_000.0
CREATOR = "DEV"


class Curve:
    """Constant-product pump.fun-style bonding curve for generating events."""

    def __init__(self, mint="MINT1", v_sol=30.0, v_tokens=1_073_000_000.0):
        self.mint, self.v_sol, self.v_tokens = mint, v_sol, v_tokens
        self.k = v_sol * v_tokens

    def _state(self):
        return {"vSolInBondingCurve": self.v_sol, "vTokensInBondingCurve": self.v_tokens,
                "marketCapSol": self.v_sol / self.v_tokens * 1e9}

    def create(self, mayhem=True):
        return {"txType": "create", "mint": self.mint, "symbol": "MAY", "name": "Mayhem",
                "traderPublicKey": CREATOR, "isMayhemMode": mayhem, **self._state()}

    def trade(self, side, sol, trader):
        if side == "buy":
            self.v_sol += sol
        else:
            self.v_sol -= sol
        self.v_tokens = self.k / self.v_sol
        return {"txType": side, "mint": self.mint, "traderPublicKey": trader, "solAmount": sol, **self._state()}


def make_engine(tmp_path, clock, **cfg_kw):
    cfg = SniperConfig(state_file=str(tmp_path / "sniper.json"), **cfg_kw)
    book = SnipeBook(5.0)
    eng = SniperEngine(cfg, PaperSnipeExecutor(cfg), book, paper=True,
                       kill_switch_file=str(tmp_path / "STOP"), clock=lambda: clock[0])
    return cfg, book, eng


async def organic_pump(eng, curve, clock, n=10, sol=0.3):
    for i in range(n):
        clock[0] += 0.5
        await eng.on_event(curve.trade("buy", sol, f"buyer{i}"))
    await eng.drain()


def test_mayhem_detection():
    keys = SniperConfig().mayhem_keys
    assert is_mayhem({"isMayhemMode": True}, keys)
    assert is_mayhem({"is_mayhem_mode": "true"}, keys)
    assert not is_mayhem({"isMayhemMode": False}, keys)
    assert not is_mayhem({}, keys)


def test_entry_rules():
    cfg = SniperConfig()
    c = Curve()
    w = TokenWatch.from_create(c.create(), T0, cfg.mayhem_keys)
    assert any("too early" in r for r in entry_rejections(w, cfg, T0))
    for i in range(10):
        w.apply_trade(c.trade("buy", 0.3, f"b{i}"), T0 + i)
    assert entry_rejections(w, cfg, T0 + 10) == []
    assert any("window closed" in r for r in entry_rejections(w, cfg, T0 + 999))
    w.apply_trade(c.trade("sell", 0.01, CREATOR), T0 + 11)
    assert "dev sold" in entry_rejections(w, cfg, T0 + 11)


def test_whale_concentration_rejected():
    cfg = SniperConfig()
    c = Curve()
    w = TokenWatch.from_create(c.create(), T0, cfg.mayhem_keys)
    for i in range(9):
        w.apply_trade(c.trade("buy", 0.05, f"b{i}"), T0 + 5)
    w.apply_trade(c.trade("buy", 3.0, "whale"), T0 + 5)
    assert any("top buyer" in r for r in entry_rejections(w, cfg, T0 + 5))


def test_exit_rules():
    cfg = SniperConfig()
    c = Curve()
    w = TokenWatch.from_create(c.create(), T0, cfg.mayhem_keys)
    pos = SnipePosition("MINT1", "MAY", w.price, 1000, 0.05, T0, w.price)
    assert exit_reason(pos, w, cfg, T0 + 1) is None
    w.v_sol *= 1.5  # price +50%
    assert exit_reason(pos, w, cfg, T0 + 1).startswith("take_profit")
    w.v_sol = c.v_sol * 0.75
    assert exit_reason(pos, w, cfg, T0 + 1).startswith("stop_loss")
    w.v_sol = c.v_sol
    pos.peak_price = pos.entry_price  # reset peak so only the timer applies
    assert exit_reason(pos, w, cfg, T0 + 500).startswith("max_hold")
    w.dev_sold = True
    assert exit_reason(pos, w, cfg, T0 + 1).startswith("dev_sold")


def test_paper_curve_fills_are_realistic():
    cfg = SniperConfig()
    c = Curve()
    w = TokenWatch.from_create(c.create(), T0, cfg.mayhem_keys)
    ex = PaperSnipeExecutor(cfg)
    fill = ex.buy(w, 0.05)
    assert fill.tokens < 0.05 / w.price  # fees + slippage mean fewer tokens than spot
    pos = SnipePosition(w.mint, "MAY", fill.sol / fill.tokens, fill.tokens, fill.sol, T0, 0)
    back = ex.sell(pos, w)
    assert back.sol < 0.05  # immediate round trip loses money (fees/slippage)


def test_engine_full_cycle_take_profit(tmp_path):
    async def go():
        clock = [T0]
        cfg, book, eng = make_engine(tmp_path, clock)
        c = Curve()
        await eng.on_event(c.create())
        assert c.mint in eng.watches
        await organic_pump(eng, c, clock)
        assert c.mint in book.positions
        assert book.sol_balance == pytest.approx(5.0 - cfg.buy_sol)

        # Strong follow-through -> take profit fires on the trade tick.
        for i in range(6):
            clock[0] += 0.5
            await eng.on_event(c.trade("buy", 2.0, f"late{i}"))
        await eng.drain()
        assert book.positions == {}
        assert book.closed[0].reason.startswith("take_profit")
        assert book.closed[0].pnl_sol > 0

        # One shot per mint: no re-entry.
        await organic_pump(eng, c, clock)
        assert book.positions == {}
        assert SnipeBook.load(cfg.state_file, 0).closed[0].mint == c.mint
    asyncio.run(go())


def test_engine_dev_dump_exits(tmp_path):
    async def go():
        clock = [T0]
        cfg, book, eng = make_engine(tmp_path, clock)
        c = Curve()
        await eng.on_event(c.create())
        await organic_pump(eng, c, clock)
        assert c.mint in book.positions
        clock[0] += 1
        await eng.on_event(c.trade("sell", 0.5, CREATOR))
        await eng.drain()
        assert book.closed and book.closed[0].reason.startswith("dev_sold")
    asyncio.run(go())


def test_engine_ignores_non_mayhem_and_respects_limits(tmp_path):
    async def go():
        clock = [T0]
        cfg, book, eng = make_engine(tmp_path, clock, max_open_positions=1)
        await eng.on_event(Curve("NORMAL").create(mayhem=False))
        assert "NORMAL" not in eng.watches

        a, b = Curve("A"), Curve("B")
        await eng.on_event(a.create())
        await eng.on_event(b.create())
        await organic_pump(eng, a, clock)
        await organic_pump(eng, b, clock)
        assert list(book.positions) == ["A"]  # max_open_positions = 1

        (tmp_path / "STOP").touch()
        assert eng.entry_block_reason(clock[0]) == "kill switch"
    asyncio.run(go())


def test_housekeeping_max_hold_and_pruning(tmp_path):
    async def go():
        clock = [T0]
        unsubscribed = []
        cfg, book, eng = make_engine(tmp_path, clock)

        async def unsub(m):
            unsubscribed.extend(m)
        eng.unsubscribe = unsub

        c, idle = Curve("HELD"), Curve("IDLE")
        await eng.on_event(c.create())
        await eng.on_event(idle.create())
        await organic_pump(eng, c, clock)
        clock[0] += cfg.max_hold_sec + 1
        await eng.housekeeping()
        await eng.drain()
        assert book.closed[0].reason.startswith("max_hold")
        await eng.housekeeping()
        assert "IDLE" in unsubscribed and "IDLE" not in eng.watches
    asyncio.run(go())


def test_feed_end_to_end_with_local_websocket(tmp_path):
    """Spin up a fake PumpPortal websocket and run the real feed client against it."""
    websockets = pytest.importorskip("websockets")
    from memebot.pumpportal import PumpPortalFeed

    async def go():
        clock = [T0]
        cfg, book, eng = make_engine(tmp_path, clock)
        c = Curve()
        received = []

        async def handler(ws):
            msg = json.loads(await ws.recv())
            received.append(msg)
            assert msg == {"method": "subscribeNewToken"}
            await ws.send(json.dumps({"message": "Successfully subscribed"}))
            await ws.send(json.dumps(c.create()))
            sub = json.loads(await ws.recv())
            received.append(sub)
            for i in range(10):
                clock[0] += 0.5
                await ws.send(json.dumps(c.trade("buy", 0.3, f"b{i}")))
            await asyncio.sleep(0.2)
            for i in range(6):
                clock[0] += 0.5
                await ws.send(json.dumps(c.trade("buy", 2.0, f"late{i}")))
            await asyncio.sleep(0.3)

        async with websockets.serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            feed = PumpPortalFeed(f"ws://127.0.0.1:{port}", eng)
            task = asyncio.create_task(feed.run())
            for _ in range(50):
                await asyncio.sleep(0.05)
                if book.closed:
                    break
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        assert received[1] == {"method": "subscribeTokenTrade", "keys": [c.mint]}
        assert book.closed and book.closed[0].reason.startswith("take_profit")
    asyncio.run(go())


# ---------------------------------------------------------------- on-chain Mayhem flag
def _bonding_curve_bytes(mayhem: bool) -> bytes:
    import struct
    from memebot.pumpchain import BONDING_CURVE_DISCRIMINATOR
    return (BONDING_CURVE_DISCRIMINATOR
            + struct.pack("<5Q", 1_073_000_000_000_000, 30_000_000_000, 793_100_000_000_000, 0, 10**15)
            + b"\x00"                      # complete
            + bytes(range(32))             # creator pubkey
            + (b"\x01" if mayhem else b"\x00")  # is_mayhem_mode
            + b"\x00" * 120)               # remaining fields


def test_parse_is_mayhem_from_account_bytes():
    from memebot.pumpchain import parse_is_mayhem
    assert parse_is_mayhem(_bonding_curve_bytes(True)) is True
    assert parse_is_mayhem(_bonding_curve_bytes(False)) is False
    assert parse_is_mayhem(b"\x00" * 200) is None  # wrong discriminator
    assert parse_is_mayhem(_bonding_curve_bytes(True)[:60]) is None  # truncated


def test_mayhem_checker_via_rpc():
    import base64
    from memebot.pumpchain import MayhemChecker

    class FakeHttp:
        def __init__(self):
            self.calls = 0

        def post(self, url, json):
            self.calls += 1
            data = base64.b64encode(_bonding_curve_bytes(True)).decode()
            return {"result": {"value": {"data": [data, "base64"]}}}

    http = FakeHttp()
    chk = MayhemChecker("http://rpc", http=http)
    assert chk.is_mayhem("M", "CURVE") is True
    assert chk.is_mayhem("M", "CURVE") is True and http.calls == 1  # cached


def test_bonding_curve_pda_derivation():
    pytest.importorskip("solders")
    from memebot.pumpchain import bonding_curve_address
    a = bonding_curve_address("So11111111111111111111111111111111111111112")
    assert a == bonding_curve_address("So11111111111111111111111111111111111111112") and len(a) >= 32


class FakeChecker:
    def __init__(self, answers):
        self.answers = answers

    def is_mayhem(self, mint, bonding_curve=""):
        return self.answers.get(mint)


def test_engine_confirms_mayhem_onchain_when_feed_has_no_flag(tmp_path):
    async def go():
        clock = [T0]
        cfg, book, eng = make_engine(tmp_path, clock)
        eng.mayhem_checker = FakeChecker({"YES": True, "NO": False})
        yes, no = Curve("YES"), Curve("NO")
        ev_yes, ev_no = yes.create(), no.create()
        del ev_yes["isMayhemMode"], ev_no["isMayhemMode"]  # feed without the flag
        await eng.on_event(ev_yes)
        await eng.on_event(ev_no)
        await eng.drain()
        assert eng.watches["YES"].mayhem is True
        assert "NO" not in eng.watches  # confirmed non-Mayhem -> dropped
        await organic_pump(eng, yes, clock)
        assert "YES" in book.positions
    asyncio.run(go())


def test_engine_never_buys_unconfirmed(tmp_path):
    async def go():
        clock = [T0]
        cfg, book, eng = make_engine(tmp_path, clock)
        eng.mayhem_checker = FakeChecker({})  # RPC can't tell
        c = Curve("UNK")
        ev = c.create()
        del ev["isMayhemMode"]
        await eng.on_event(ev)
        await eng.drain()
        await organic_pump(eng, c, clock)
        assert book.positions == {}
    asyncio.run(go())


# ---------------------------------------------------------------- graduated mode
def make_grad_engine(tmp_path, clock):
    from memebot.config import GraduatedConfig
    cfg = SniperConfig(state_file=str(tmp_path / "sniper.json"))
    g = GraduatedConfig()
    book = SnipeBook(5.0)
    eng = SniperEngine(cfg, PaperSnipeExecutor(cfg, g), book, paper=True,
                       kill_switch_file=str(tmp_path / "STOP"), clock=lambda: clock[0], graduated=g)
    return g, book, eng


def _grad_trade(mint, side, sol, trader, mcap, reserves=None):
    ev = {"txType": side, "mint": mint, "traderPublicKey": trader, "solAmount": sol,
          "marketCapSol": mcap, "pool": "pump-amm"}
    if reserves:
        ev["vSolInBondingCurve"], ev["vTokensInBondingCurve"] = reserves
    return ev


@pytest.mark.parametrize("with_reserves", [False, True])
def test_graduated_cycle(tmp_path, with_reserves):
    async def go():
        clock = [T0]
        g, book, eng = make_grad_engine(tmp_path, clock)
        await eng.on_event({"txType": "migrate", "mint": "GRAD", "pool": "pump-amm"})
        assert eng.watches["GRAD"].kind == "graduated"
        mcap = 420.0
        for i in range(16):  # steady organic buying after migration
            clock[0] += 1
            mcap *= 1.005
            res = (85 * mcap / 420, 206e6 * 420 / mcap) if with_reserves else None
            await eng.on_event(_grad_trade("GRAD", "buy", 0.5, f"b{i}", mcap, res))
        await eng.drain()
        assert "GRAD" in book.positions and book.positions["GRAD"].kind == "graduated"
        for i in range(5):  # price runs +40%
            clock[0] += 1
            mcap *= 1.08
            res = (85 * mcap / 420, 206e6 * 420 / mcap) if with_reserves else None
            await eng.on_event(_grad_trade("GRAD", "buy", 3.0, f"x{i}", mcap, res))
        await eng.drain()
        t = book.closed[0]
        assert t.kind == "graduated" and t.reason.startswith("take_profit") and t.pnl_sol > 0
    asyncio.run(go())


def test_graduated_disabled_ignores_migrations(tmp_path):
    async def go():
        clock = [T0]
        cfg, book, eng = make_engine(tmp_path, clock)  # no graduated config
        await eng.on_event({"txType": "migrate", "mint": "GRAD", "pool": "pump-amm"})
        assert "GRAD" not in eng.watches
    asyncio.run(go())


def test_graduated_stop_loss(tmp_path):
    async def go():
        clock = [T0]
        g, book, eng = make_grad_engine(tmp_path, clock)
        await eng.on_event({"txType": "migrate", "mint": "G2", "pool": "pump-amm"})
        mcap = 420.0
        for i in range(16):
            clock[0] += 1
            await eng.on_event(_grad_trade("G2", "buy", 0.5, f"b{i}", mcap))
        await eng.drain()
        assert "G2" in book.positions
        clock[0] += 1
        await eng.on_event(_grad_trade("G2", "sell", 20, "dumper", mcap * 0.8))
        await eng.drain()
        assert book.closed[0].reason.startswith("stop_loss") and book.closed[0].pnl_sol < 0
    asyncio.run(go())


# ---------------------------------------------------------------- bots, recording, backtest
def test_bot_wallets_excluded_from_buyer_stats(tmp_path):
    async def go():
        clock = [T0]
        cfg, book, eng = make_engine(tmp_path, clock, bot_wallet_min_coins=3)
        coins = [Curve(f"C{i}") for i in range(3)]
        for c in coins:
            await eng.on_event(c.create())
        # The same "agent" wallet buys big in all three coins; a few small organic buyers too.
        for c in coins:
            for i in range(10):
                clock[0] += 0.3
                await eng.on_event(c.trade("buy", 2.0, "AGENT"))
        await eng.drain()
        w = eng.watches["C2"]
        assert w.top_buyer_share == pytest.approx(1.0)
        assert eng.bot_wallets_in(w) == {"AGENT"}
        assert w.buyer_stats(eng.bot_wallets_in(w)) == (0, 0.0)
        assert book.positions == {}  # agent-only flow never qualifies
    asyncio.run(go())


def test_record_and_replay_matches_live(tmp_path):
    from memebot.backtest import EventRecorder, load_events, simulate

    async def live():
        clock = [T0]
        cfg, book, eng = make_engine(tmp_path, clock)
        eng.recorder = EventRecorder(tmp_path / "events")
        c = Curve()
        await eng.on_event(c.create())
        for i in range(10):
            clock[0] += 0.5
            await eng.on_event(c.trade("buy", 0.3, f"buyer{i}"))
            await eng.drain()  # like the real feed loop: order tasks run between messages
        for i in range(6):
            clock[0] += 0.5
            await eng.on_event(c.trade("buy", 2.0, f"late{i}"))
            await eng.drain()
        eng.recorder.close()
        return cfg, book

    cfg, book = asyncio.run(live())
    events = load_events([str(tmp_path / "events" / "*.jsonl")])
    assert events and events[0]["txType"] == "create"
    res = simulate(events, cfg, None)
    assert res.n == 1 and res.trades[0].reason.startswith("take_profit")
    assert res.pnl == pytest.approx(book.closed[0].pnl_sol, rel=1e-6)


def test_tune_finds_profitable_params_on_synthetic_data(tmp_path):
    from memebot.backtest import tune
    from memebot.config import GraduatedConfig

    # Build 30 synthetic Mayhem launches: organic pump then +60% follow-through.
    events, t = [], T0
    for k in range(30):
        c = Curve(f"M{k}")
        events.append({"t": t, **c.create()})
        for i in range(10):
            t += 0.5
            events.append({"t": t, **c.trade("buy", 0.3, f"b{k}_{i}")})
        for i in range(6):
            t += 0.5
            events.append({"t": t, **c.trade("buy", 2.0, f"x{k}_{i}")})
        t += 200
    ranked = tune(events, SniperConfig(), GraduatedConfig(), "mayhem", samples=30, min_trades=3, log=lambda *_: None)
    best = [r for r in ranked if not r.get("current")][0]
    assert best["train"].pnl > 0 and best["test"].pnl > 0
