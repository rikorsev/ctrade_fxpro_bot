import statistics
from datetime import date, datetime, timedelta, timezone

import pytest

from tests.helpers import quiet_sessions, session_bars, session_days
from trading.backtest import Backtester
from trading.costs import CostModel
from trading.models import FLAT, LONG, SHORT, Position
from trading.strategies import MarketContext, NoiseBreakout, OpeningRangeBreakout, with_timeframe

SYMBOL = "#USSPX500"
DAYS = session_days(date(2024, 1, 2), 16)
HISTORY = quiet_sessions(DAYS[:15])  # 15 sessions zigzagging 0.1% around their open
OPEN = HISTORY[-1].close
CTX = MarketContext(time=datetime(2024, 1, 1, tzinfo=timezone.utc))
FREE = CostModel(spread_pips={}, default_spread_pips=0.0, slippage_pips=0.0, commission_per_million=0.0,
                 spread_bp={}, slippage_bp={})


def today(path):
    return session_bars(DAYS[15], OPEN, [OPEN * (1 + x) for x in path])


def held(direction, when=None):
    return Position(SYMBOL, "noise_breakout", direction, 1.0, OPEN, when or today([0])[0].time)


def history_sigma():
    closes = [b.close for i, b in enumerate(HISTORY) if (i + 1) % 78 == 0]
    return statistics.stdev(b / a - 1 for a, b in zip(closes, closes[1:]))


RISE = [0.001 * (i + 1) for i in range(6)]  # +0.6% after 30 minutes; the noise area is +-0.1%


def test_noise_breakout_enters_at_a_checkpoint_above_the_noise_area():
    s = NoiseBreakout()
    signal = s.on_bar(SYMBOL, HISTORY + today(RISE), None, CTX)
    assert signal.direction == LONG
    close = OPEN * 1.006
    assert signal.stop_distance == pytest.approx(0.5 * history_sigma() * close)
    short = s.on_bar(SYMBOL, HISTORY + today([-x for x in RISE]), None, CTX)
    assert short.direction == SHORT and short.stop_distance > 0
    inside = s.on_bar(SYMBOL, HISTORY + today([0.0005] * 6), None, CTX)
    assert inside.direction == FLAT and inside.stop_distance is None


def test_noise_breakout_only_decides_at_checkpoints():
    s = NoiseBreakout()
    assert s.on_bar(SYMBOL, HISTORY + today(RISE[:5]), None, CTX) is None  # 25 minutes in
    assert s.on_bar(SYMBOL, HISTORY + today(RISE[:5]), held(LONG), CTX) is None


def test_noise_breakout_trails_and_reverses():
    s = NoiseBreakout()
    still_up = RISE + [0.006] * 6
    hold = s.on_bar(SYMBOL, HISTORY + today(still_up), held(LONG), CTX)
    assert hold.direction == LONG and hold.stop_distance is None
    # Above the noise area (+0.1%) but below the session's average price: the trailing exit.
    faded = RISE + [0.006] * 5 + [0.0015]
    exit_ = s.on_bar(SYMBOL, HISTORY + today(faded), held(LONG), CTX)
    assert exit_.direction == FLAT and "trailing" in exit_.reason
    crashed = RISE + [0.006] * 5 + [-0.004]
    reverse = s.on_bar(SYMBOL, HISTORY + today(crashed), held(LONG), CTX)
    assert reverse.direction == SHORT and reverse.stop_distance > 0


def test_noise_breakout_is_flat_by_the_close_and_outside_the_session():
    s = NoiseBreakout()
    full_day = today([0.006] * 77)  # last bar closes at 15:55 New York time
    close = s.on_bar(SYMBOL, HISTORY + full_day, held(LONG), CTX)
    assert close.direction == FLAT and close.reason == "session close"
    late = full_day + [type(full_day[0])(full_day[-1].time + timedelta(minutes=10), 1, 1, 1, 1)]  # 16:00
    assert s.on_bar(SYMBOL, HISTORY + late, None, CTX) is None  # after the close
    assert s.on_bar(SYMBOL, HISTORY + late, held(LONG), CTX).direction == FLAT
    yesterday = held(LONG, when=HISTORY[-1].time)
    stale = s.on_bar(SYMBOL, HISTORY + today(RISE[:2]), yesterday, CTX)
    assert stale.direction == FLAT and "earlier session" in stale.reason


def test_noise_breakout_needs_history_and_valid_params():
    assert NoiseBreakout().on_bar(SYMBOL, HISTORY[-78 * 5:] + today(RISE), None, CTX) is None
    with pytest.raises(ValueError):
        NoiseBreakout(timeframe="H1")
    with pytest.raises(ValueError):
        NoiseBreakout(check=7)
    with pytest.raises(ValueError):
        NoiseBreakout(session="tokyo")


def test_orb_trades_the_first_candle_with_its_range_as_stop():
    s = OpeningRangeBreakout()
    up = session_bars(DAYS[0], 100.0, [100.5])
    long_ = s.on_bar("#USNDAQ100", up, None, CTX)
    assert long_.direction == LONG
    assert long_.stop_distance == pytest.approx(0.5) and long_.take_profit_distance == pytest.approx(5.0)
    down = session_bars(DAYS[0], 100.0, [99.0])
    short = s.on_bar("#USNDAQ100", down, None, CTX)
    assert short.direction == SHORT and short.stop_distance == pytest.approx(1.0)
    assert s.on_bar("#USNDAQ100", session_bars(DAYS[0], 100.0, [100.0]), None, CTX).direction == FLAT
    assert s.on_bar("#USNDAQ100", session_bars(DAYS[0], 100.0, [100.5, 101.0]), None, CTX) is None
    no_target = OpeningRangeBreakout(target_r=0).on_bar("#USNDAQ100", up, None, CTX)
    assert no_target.take_profit_distance is None


def test_intraday_strategies_check_the_timeframe():
    NoiseBreakout().check_timeframe("M5")
    with pytest.raises(ValueError):
        NoiseBreakout().check_timeframe("M15")
    with pytest.raises(ValueError):
        Backtester([OpeningRangeBreakout()], {SYMBOL: HISTORY}, timeframe="M15")
    assert with_timeframe("noise_breakout", {}, "M15") == {"timeframe": "M15"}
    assert with_timeframe("noise_breakout", {"timeframe": "M1"}, "M15") == {"timeframe": "M1"}
    assert with_timeframe("donchian", {}, "H1") == {}


def test_intraday_strategies_refuse_symbols_without_a_session():
    NoiseBreakout().check_symbol("#USNDAQ100")
    with pytest.raises(ValueError, match="no known trading session for '#JP225_Z26'"):
        NoiseBreakout().check_symbol("#JP225_Z26")
    NoiseBreakout(session="us").check_symbol("#JP225_Z26")
    with pytest.raises(ValueError, match="noise_breakout cannot trade #JP225_Z26"):
        Backtester([NoiseBreakout()], {"#JP225_Z26": HISTORY}, timeframe="M5")


def test_backtest_closes_intraday_trades_before_the_session_ends():
    days = session_days(date(2024, 1, 2), 32)
    bars = quiet_sessions(days[:31])
    opening = bars[-1].close
    bars += session_bars(days[31], opening, [opening * (1 + 0.0005 * (i + 1)) for i in range(78)])
    result = Backtester([NoiseBreakout(days=3)], {SYMBOL: bars}, timeframe="M5", costs=FREE).run()
    (trade,) = result.trades
    assert trade.direction == LONG and trade.exit_reason == "session close"
    assert trade.entry_time.date() == trade.exit_time.date() == days[31]
    assert trade.exit_time.hour == 20 and trade.exit_time.minute == 55  # 15:55 in New York
    assert trade.swap == 0 and trade.pnl > 0
