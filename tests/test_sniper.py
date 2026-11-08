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
