import json
from datetime import timedelta

import pytest

from tests.helpers import MONDAY, Scripted, daily_bars
from trading.broker import ClosedTrade
from trading.live import REPORT_ATTEMPTS, LiveConfig, LiveTrader
from trading.models import FLAT, LONG, SHORT, AccountState, Bar, Position, Signal, SymbolSpec
from trading.risk import RiskConfig, RiskManager


def run(coro):
    """Drive a coroutine that never actually suspends (the fake broker answers immediately)."""
    try:
        coro.send(None)
    except StopIteration as stop:
        return stop.value
    raise AssertionError("coroutine suspended unexpectedly")


class FakeBroker:
    def __init__(self, bars, equity=100_000.0):
        self.data = bars
        self.equity = equity
        self.open_positions: list[Position] = []
        self.opened, self.closed, self.amended = [], [], []
        self.fail_open = False
        self.results: dict[int, ClosedTrade] = {}  # what closed_trade reports, by position id

    def symbol_spec(self, symbol):
        return SymbolSpec.infer(symbol)

    async def bars(self, symbol, timeframe, count):
        return self.data[symbol][-count:]

    async def account(self):
        return AccountState(self.equity, self.equity, "USD")

    async def positions(self):
        return list(self.open_positions)

    async def conversion_rate(self, from_ccy, to_ccy):
        return 1.0

    def annual_carry(self, symbol, direction, price, time):
        return None

    async def open_position(self, order):
        if self.fail_open:
            raise RuntimeError("TRADING_BAD_VOLUME")
        self.opened.append(order)
        price = self.data[order.symbol][-1].close
        p = Position(order.symbol, order.strategy, order.direction, order.units, price, MONDAY,
                     stop_loss=price - order.direction * order.stop_distance, position_id=len(self.opened))
        self.open_positions.append(p)
        return p

    async def close_position(self, position):
        self.closed.append(position)
        self.open_positions.remove(position)

    async def amend_stop(self, position, stop_loss):
        self.amended.append((position.position_id, stop_loss))

    async def closed_trade(self, position):
        return self.results.get(position.position_id)


BARS = daily_bars([1.10, 1.11, 1.12, 1.13, 1.14])  # Mon..Fri of the first week


def trader(tmp_path, broker, strategy, execute=True, now=None, risk=None):
    clock = (lambda: now) if now else (lambda: BARS[-1].time + timedelta(days=1, minutes=1))
    config = LiveConfig(symbols=("EURUSD",), timeframe="D1", execute=execute,
                        state_path=tmp_path / "state.json", journal_path=tmp_path / "journal.jsonl")
    return LiveTrader(broker, [strategy], RiskManager(risk), config, clock=clock)


def journal(tmp_path):
    return [json.loads(line) for line in (tmp_path / "journal.jsonl").read_text().splitlines()]


def test_dry_run_logs_but_sends_nothing(tmp_path):
    broker = FakeBroker({"EURUSD": BARS})
    s = Scripted({BARS[-1].time: Signal(LONG, stop_distance=0.01, reason="test")}, lookback=3)
    assert run(trader(tmp_path, broker, s, execute=False).tick())
    assert broker.opened == []
    events = [e["event"] for e in journal(tmp_path)]
    assert events == ["signal", "dry_run_open"]
    state = json.loads((tmp_path / "state.json").read_text())
    assert state["last_bar"]["EURUSD"] == BARS[-1].time.isoformat()


def test_execute_sizes_by_risk_and_does_not_repeat_a_bar(tmp_path):
    broker = FakeBroker({"EURUSD": BARS})
    s = Scripted({BARS[-1].time: Signal(LONG, stop_distance=0.01)}, lookback=3)
    t = trader(tmp_path, broker, s)
    assert run(t.tick())
    (order,) = broker.opened
    assert order.units == 50_000 and order.direction == LONG and order.stop_distance == 0.01
    t._next_check.clear()  # force a re-check of the same bar
    assert not run(t.tick())
    # A restarted bot must not act on the bar it already processed either.
    restarted = trader(tmp_path, broker, s)
    assert not run(restarted.tick())
    assert len(broker.opened) == 1


def test_forming_bar_is_ignored(tmp_path):
    forming = BARS + daily_bars([1.14, 1.20], start=BARS[-1].time + timedelta(days=3))[1:]
    broker = FakeBroker({"EURUSD": forming})
    s = Scripted({}, lookback=3)
    # The Monday bar opened 12 hours ago: still forming.
    now = forming[-1].time + timedelta(hours=12)
    run(trader(tmp_path, broker, s, now=now).tick())
    (seen_time, _, _, _), = s.calls
    assert seen_time == BARS[-1].time


def test_reversal_closes_first_and_sizes_without_the_old_position(tmp_path):
    broker = FakeBroker({"EURUSD": BARS})
    old = Position("EURUSD", "scripted", LONG, 300_000, 1.10, MONDAY, stop_loss=1.09, position_id=7)
    broker.open_positions.append(old)
    s = Scripted({BARS[-1].time: Signal(SHORT, stop_distance=0.01)}, lookback=3)
    run(trader(tmp_path, broker, s).tick())
    assert broker.closed == [old]
    (order,) = broker.opened
    assert order.direction == SHORT and order.units == 50_000


def test_trailing_stop_and_flat_signal(tmp_path):
    broker = FakeBroker({"EURUSD": BARS})
    held = Position("EURUSD", "scripted", LONG, 10_000, 1.10, MONDAY, stop_loss=1.09, position_id=3)
    broker.open_positions.append(held)
    s = Scripted({BARS[-1].time: Signal(LONG, trailing_stop=1.123456)}, lookback=3)
    run(trader(tmp_path, broker, s).tick())
    assert broker.amended == [(3, 1.12346)]  # rounded to the symbol's 5 digits

    broker2 = FakeBroker({"EURUSD": BARS})
    broker2.open_positions.append(held)
    s2 = Scripted({BARS[-1].time: Signal(FLAT)}, lookback=3)
    run(trader(tmp_path / "b", broker2, s2).tick())
    assert broker2.closed == [held]


def test_risk_halt_blocks_entries(tmp_path):
    broker = FakeBroker({"EURUSD": BARS}, equity=90_000)
    s = Scripted({BARS[-1].time: Signal(LONG, stop_distance=0.01)}, lookback=3)
    t = trader(tmp_path, broker, s, risk=RiskConfig(max_daily_loss=0.05))
    t.risk.update(100_000, BARS[-1].time + timedelta(hours=23))  # yesterday's equity
    run(t.tick())
    assert broker.opened == []
    events = [e["event"] for e in journal(tmp_path)]
    assert "halt" in events and "entry_skipped" in events


def test_failed_order_is_journaled_not_raised(tmp_path):
    broker = FakeBroker({"EURUSD": BARS})
    broker.fail_open = True
    s = Scripted({BARS[-1].time: Signal(LONG, stop_distance=0.01)}, lookback=3)
    assert run(trader(tmp_path, broker, s).tick())
    assert journal(tmp_path)[-1]["event"] == "open_failed"


def test_failed_cycle_is_retried_soon(tmp_path):
    broker = FakeBroker({"EURUSD": BARS})
    s = Scripted({BARS[-1].time: Signal(LONG, stop_distance=0.01)}, lookback=3)
    t = trader(tmp_path, broker, s)
    original = broker.account

    async def disconnected():
        raise ConnectionError("lost connection")

    broker.account = disconnected
    assert not run(t.tick())
    assert "EURUSD" not in t.last_bar and broker.opened == []
    broker.account = original
    t.clock = lambda: BARS[-1].time + timedelta(days=1, minutes=3)  # two minutes later
    assert run(t.tick())
    assert len(broker.opened) == 1


class FailsOnGbp(Scripted):
    def on_bar(self, symbol, bars, position, context):
        if symbol == "GBPUSD":
            raise ZeroDivisionError("bad bar")
        return super().on_bar(symbol, bars, position, context)


def test_a_failing_or_unavailable_symbol_does_not_stop_the_others(tmp_path):
    broker = FakeBroker({"EURUSD": BARS, "GBPUSD": BARS})
    s = FailsOnGbp({BARS[-1].time: Signal(LONG, stop_distance=0.01)}, lookback=3)
    config = LiveConfig(symbols=("EURUSD", "GBPUSD", "USDJPY"), timeframe="D1", execute=True,
                        state_path=tmp_path / "state.json", journal_path=tmp_path / "journal.jsonl")
    t = LiveTrader(broker, [s], RiskManager(), config, clock=lambda: BARS[-1].time + timedelta(days=1, minutes=1))
    t.ignore("usdjpy", "not offered on this account")
    assert t.symbols == ["EURUSD", "GBPUSD"]
    assert run(t.tick())
    assert [o.symbol for o in broker.opened] == ["EURUSD"]
    events = [(e["event"], e["symbol"]) for e in journal(tmp_path)]
    assert events == [("symbol_ignored", "USDJPY"), ("signal", "EURUSD"), ("strategy_failed", "GBPUSD"),
                      ("opened", "EURUSD")]
    assert set(t.last_bar) == {"EURUSD", "GBPUSD"}  # the failed bar is not retried


def test_strategy_names_must_be_distinct(tmp_path):
    with pytest.raises(ValueError):
        LiveTrader(FakeBroker({}), [Scripted({}), Scripted({})], RiskManager(), LiveConfig(symbols=("EURUSD",)))


def test_intraday_tick_on_five_minute_bars(tmp_path):
    from datetime import date

    from tests.helpers import quiet_sessions, session_bars, session_days
    from trading.strategies import NoiseBreakout

    days = session_days(date(2024, 1, 2), 32)
    history = quiet_sessions(days[:31])
    opening = history[-1].close
    rising = session_bars(days[31], opening, [opening * (1 + 0.001 * (i + 1)) for i in range(6)])
    broker = FakeBroker({"#USSPX500": history + rising})
    config = LiveConfig(symbols=("#USSPX500",), timeframe="M5", execute=True,
                        state_path=tmp_path / "state.json", journal_path=tmp_path / "journal.jsonl")
    with pytest.raises(ValueError):
        LiveTrader(broker, [NoiseBreakout(days=3)], RiskManager(), LiveConfig(symbols=("#USSPX500",)))
    # A symbol without a known session is ignored; the trader only refuses to start if nothing is left.
    mixed = LiveTrader(broker, [NoiseBreakout(days=3)], RiskManager(),
                       LiveConfig(symbols=("#USSPX500", "#JP225_Z26"), timeframe="M5"))
    assert mixed.symbols == ["#USSPX500"]
    with pytest.raises(ValueError, match="#JP225_Z26"):
        LiveTrader(broker, [NoiseBreakout(days=3)], RiskManager(), LiveConfig(symbols=("#JP225_Z26",), timeframe="M5"))

    # 25 minutes into the session: between checkpoints, so nothing is journaled.
    broker.data["#USSPX500"] = history + rising[:5]
    clock = {"now": rising[4].time + timedelta(minutes=5, seconds=10)}
    t = LiveTrader(broker, [NoiseBreakout(days=3)], RiskManager(), config, clock=lambda: clock["now"])
    assert run(t.tick())
    assert broker.opened == [] and not (tmp_path / "journal.jsonl").exists()

    broker.data["#USSPX500"] = history + rising
    clock["now"] = rising[5].time + timedelta(minutes=5, seconds=20)  # after the settle delay
    assert run(t.tick())
    (order,) = broker.opened
    assert order.direction == LONG and order.strategy == "noise_breakout"
    assert [e["event"] for e in journal(tmp_path)] == ["signal", "opened"]


def test_a_bar_that_closed_too_long_ago_is_not_acted_on(tmp_path):
    start = MONDAY + timedelta(hours=14)
    bars = [Bar(start + timedelta(minutes=5 * i), 1.1, 1.1, 1.1, 1.1) for i in range(6)]  # last closes 14:30
    broker = FakeBroker({"EURUSD": bars})
    s = Scripted({b.time: Signal(LONG, stop_distance=0.01) for b in bars}, lookback=3)
    config = LiveConfig(symbols=("EURUSD",), timeframe="M5", execute=True,
                        state_path=tmp_path / "state.json", journal_path=tmp_path / "journal.jsonl")
    clock = {"now": bars[-1].time + timedelta(hours=2)}  # restarted two hours after the market stopped
    t = LiveTrader(broker, [s], RiskManager(), config, clock=lambda: clock["now"])
    assert not run(t.tick())
    assert s.calls == [] and broker.opened == []
    (event,) = journal(tmp_path)
    assert (event["event"], event["symbol"], event["minutes_old"]) == ("stale_bar", "EURUSD", 115)
    # Journaled once, also across a restart.
    t._next_check.clear()
    assert not run(t.tick())
    assert not run(LiveTrader(broker, [s], RiskManager(), config, clock=lambda: clock["now"]).tick())
    assert len(journal(tmp_path)) == 1
    # The next bar that closes is traded as usual.
    bars.append(Bar(bars[-1].time + timedelta(hours=2), 1.1, 1.1, 1.1, 1.1))
    s.signals[bars[-1].time] = Signal(LONG, stop_distance=0.01)
    clock["now"] = bars[-1].time + timedelta(minutes=5, seconds=20)
    t._next_check.clear()
    assert run(t.tick())
    assert len(broker.opened) == 1


class ClosesGbp(Scripted):
    def on_bar(self, symbol, bars, position, context):
        super().on_bar(symbol, bars, position, context)
        return Signal(FLAT, reason="exit rule") if symbol == "GBPUSD" else None


def test_closed_positions_are_journaled_with_the_brokers_result(tmp_path):
    monday = daily_bars([1.14, 1.15], start=BARS[-1].time + timedelta(days=3))[1:]
    broker = FakeBroker({"EURUSD": BARS, "GBPUSD": BARS})
    stopped = Position("EURUSD", "scripted", LONG, 10_000, 1.10, MONDAY, stop_loss=1.09, position_id=1)
    exited = Position("GBPUSD", "scripted", LONG, 10_000, 1.10, MONDAY, stop_loss=1.09, position_id=2)
    broker.open_positions += [stopped, exited]
    config = LiveConfig(symbols=("EURUSD", "GBPUSD"), timeframe="D1", execute=True,
                        state_path=tmp_path / "state.json", journal_path=tmp_path / "journal.jsonl")
    s = ClosesGbp({}, lookback=3)
    friday = LiveTrader(broker, [s], RiskManager(), config, clock=lambda: BARS[-1].time + timedelta(days=1, minutes=1))
    assert run(friday.tick())
    assert broker.closed == [exited]
    assert "trade_result" not in [e["event"] for e in journal(tmp_path)]  # reported once it is gone

    # Over the weekend the EURUSD stop is hit. A restarted bot still knows both positions.
    broker.open_positions.remove(stopped)
    exit_time = BARS[-1].time + timedelta(days=1)
    broker.results = {1: ClosedTrade(1.09, exit_time, -100.0, -0.5, -0.7, -101.2),
                      2: ClosedTrade(1.141, exit_time, 410.0, 0.0, -0.7, 409.3)}
    broker.data = {"EURUSD": BARS + monday, "GBPUSD": BARS + monday}
    tuesday = LiveTrader(broker, [s], RiskManager(), config, clock=lambda: monday[-1].time + timedelta(days=1, minutes=1))
    assert run(tuesday.tick())
    results = {e["symbol"]: e for e in journal(tmp_path) if e["event"] == "trade_result"}
    assert (results["EURUSD"]["closed_by"], results["EURUSD"]["net"], results["EURUSD"]["exit_price"]) == \
        ("broker", -101.2, 1.09)
    assert (results["GBPUSD"]["closed_by"], results["GBPUSD"]["reason"], results["GBPUSD"]["net"]) == \
        ("strategy", "exit rule", 409.3)
    assert tuesday._tracked == {}


def test_a_closed_position_without_deals_is_journaled_after_a_few_cycles(tmp_path):
    broker = FakeBroker({"EURUSD": BARS})
    t = trader(tmp_path, broker, Scripted({}, lookback=3))
    t._tracked[9] = Position("EURUSD", "scripted", SHORT, 10_000, 1.10, MONDAY, stop_loss=1.11, position_id=9)
    for _ in range(REPORT_ATTEMPTS - 1):
        run(t._report_closed([]))
    assert not (tmp_path / "journal.jsonl").exists()
    run(t._report_closed([]))
    (event,) = journal(tmp_path)
    assert event["event"] == "trade_result" and event["position_id"] == 9 and "net" not in event
    assert event["error"] == "the broker has no closing deal for it" and t._tracked == {}
