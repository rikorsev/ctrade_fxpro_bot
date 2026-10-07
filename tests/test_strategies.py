from datetime import datetime, timezone

import pytest

from tests.helpers import FixedCarry, daily_bars
from trading.models import FLAT, LONG, SHORT, Position
from trading.strategies import CarryTrend, DonchianTrend, MarketContext, TimeSeriesMomentum, create_strategy

NOW = datetime(2025, 1, 1, tzinfo=timezone.utc)
CTX = MarketContext(time=NOW)

# Entry channel (5 bars before the last) spans 0.97..1.01, exit channel (3 bars) spans 0.99..1.01.
RANGE = [1.00, 0.97, 1.00, 1.01, 0.99, 1.00]


def position(direction, stop=None):
    return Position("EURUSD", "test", direction, 10_000, 1.0, NOW, stop_loss=stop)


def small():
    return DonchianTrend(entry=5, exit=3, atr=2, stop_atr=2.0)


def last(close):
    return daily_bars(RANGE + [close])


def test_donchian_breakouts():
    s = small()
    assert s.lookback == 7
    assert s.on_bar("EURUSD", last(1.00), None, CTX).direction == FLAT
    long_ = s.on_bar("EURUSD", last(1.05), None, CTX)
    assert long_.direction == LONG and long_.stop_distance > 0
    assert s.on_bar("EURUSD", last(0.95), None, CTX).direction == SHORT


def test_donchian_trails_exits_and_reverses():
    s = small()
    hold = s.on_bar("EURUSD", last(1.00), position(LONG), CTX)
    assert hold.direction == LONG and hold.trailing_stop == pytest.approx(0.99)
    assert s.on_bar("EURUSD", last(0.985), position(LONG), CTX).direction == FLAT
    reverse = s.on_bar("EURUSD", last(0.95), position(LONG), CTX)
    assert reverse.direction == SHORT and reverse.stop_distance > 0
    hold_short = s.on_bar("EURUSD", last(1.00), position(SHORT), CTX)
    assert hold_short.direction == SHORT and hold_short.trailing_stop == pytest.approx(1.01)


def test_donchian_needs_full_window_and_valid_params():
    assert small().on_bar("EURUSD", daily_bars([1.0] * 3), None, CTX) is None
    with pytest.raises(ValueError):
        DonchianTrend(entry=20, exit=20)


def test_tsmom_votes_and_holds():
    s = TimeSeriesMomentum(fast=2, medium=3, slow=4, atr=1, stop_atr=3.0, rebalance="daily")
    rising = daily_bars([1.0, 1.01, 1.02, 1.03, 1.04, 1.05])
    entry = s.on_bar("EURUSD", rising, None, CTX)
    assert entry.direction == LONG and entry.stop_distance > 0
    held = s.on_bar("EURUSD", rising, position(LONG), CTX)
    assert held.direction == LONG and held.stop_distance is None
    falling = daily_bars([1.05, 1.04, 1.03, 1.02, 1.01, 1.0])
    assert s.on_bar("EURUSD", falling, position(LONG), CTX).direction == SHORT
    # 2-of-3 vote: the short horizon turned down but the longer ones are still up
    mixed = daily_bars([1.0, 1.1, 1.2, 1.25, 1.24, 1.23])
    assert s.on_bar("EURUSD", mixed, None, CTX).direction == LONG


def test_tsmom_only_changes_position_at_the_monthly_rebalance():
    s = TimeSeriesMomentum(fast=2, medium=3, slow=4, atr=1)
    assert s.params.rebalance == "monthly"
    january = daily_bars([1.0, 1.01, 1.02, 1.03, 1.04, 1.05], start=datetime(2024, 1, 22, tzinfo=timezone.utc))
    assert [b.time.month for b in january] == [1, 1, 1, 1, 1, 1]
    assert s.on_bar("EURUSD", january, None, CTX).direction == FLAT  # mid-month: wait
    falling = daily_bars([1.05, 1.04, 1.03, 1.02, 1.01, 1.0], start=datetime(2024, 1, 22, tzinfo=timezone.utc))
    assert s.on_bar("EURUSD", falling, position(LONG), CTX).direction == LONG  # hold until month end
    february = daily_bars([1.0, 1.01, 1.02, 1.03, 1.04, 1.05], start=datetime(2024, 1, 25, tzinfo=timezone.utc))
    assert february[-1].time.month == 2 and february[-2].time.month == 1
    assert s.on_bar("EURUSD", february, None, CTX).direction == LONG
    with pytest.raises(ValueError):
        TimeSeriesMomentum(rebalance="hourly")


def carry_strategy():
    return CarryTrend(min_carry=1.0, trend=3, vol_fast=2, vol_slow=8, vol_cap=1.5, atr=1)


AUD_CARRY = FixedCarry({("AUDUSD", LONG): 2.5, ("AUDUSD", SHORT): -4.0})


def test_carry_follows_positive_carry_with_trend():
    s = carry_strategy()
    up = daily_bars([1.00 + 0.01 * i for i in range(10)])
    signal = s.on_bar("AUDUSD", up, None, MarketContext(NOW, AUD_CARRY))
    assert signal.direction == LONG and signal.stop_distance > 0
    down = daily_bars([1.09 - 0.01 * i for i in range(10)])
    assert s.on_bar("AUDUSD", down, None, MarketContext(NOW, AUD_CARRY)).direction == FLAT


def test_carry_stays_flat_without_enough_carry_or_in_volatility_spikes():
    s = carry_strategy()
    up = daily_bars([1.00 + 0.01 * i for i in range(10)])
    thin = MarketContext(NOW, FixedCarry({("EURUSD", LONG): -0.5, ("EURUSD", SHORT): 0.4}))
    assert s.on_bar("EURUSD", up, None, thin).direction == FLAT

    spike = daily_bars([1.000 + 0.001 * i for i in range(9)] + [1.10])
    flat = s.on_bar("AUDUSD", spike, None, MarketContext(NOW, AUD_CARRY))
    assert flat.direction == FLAT and "volatility" in flat.reason

    assert s.on_bar("AUDUSD", up, None, CTX) is None  # no carry source


def test_carry_filters_can_be_disabled():
    raw = CarryTrend(min_carry=1.0, trend=0, vol_cap=0, atr=1)
    assert raw.lookback == 4
    down = daily_bars([1.09 - 0.01 * i for i in range(10)])
    assert raw.on_bar("AUDUSD", down, None, MarketContext(NOW, AUD_CARRY)).direction == LONG


def test_params_are_coerced_and_validated():
    s = create_strategy("donchian", entry="100", stop_atr="2.5")
    assert s.params.entry == 100 and s.params.stop_atr == 2.5
    with pytest.raises(ValueError, match="Unknown parameter"):
        create_strategy("donchian", nope=1)
    with pytest.raises(ValueError, match="expects int"):
        create_strategy("donchian", entry="2.5")
    with pytest.raises(ValueError, match="Unknown strategy"):
        create_strategy("martingale")
