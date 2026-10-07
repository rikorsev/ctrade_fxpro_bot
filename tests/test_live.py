import json
from datetime import timedelta

import pytest

from tests.helpers import MONDAY, Scripted, daily_bars
from trading.live import LiveConfig, LiveTrader
from trading.models import FLAT, LONG, SHORT, AccountState, Position, Signal, SymbolSpec
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


def test_strategy_names_must_be_distinct(tmp_path):
    with pytest.raises(ValueError):
        LiveTrader(FakeBroker({}), [Scripted({}), Scripted({})], RiskManager(), LiveConfig(symbols=("EURUSD",)))
