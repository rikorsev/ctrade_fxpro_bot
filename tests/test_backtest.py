from datetime import timedelta

import pytest

from tests.helpers import Scripted, bar, daily_bars
from trading.backtest import Backtester
from trading.costs import CostModel
from trading.models import FLAT, LONG, SHORT, Signal, SymbolSpec
from trading.risk import RiskConfig

FREE = CostModel(spread_pips={}, default_spread_pips=0.0, slippage_pips=0.0, commission_per_million=0.0)
EURUSD = SymbolSpec.infer("EURUSD")


def run(strategy, bars, costs=FREE, **kwargs):
    return Backtester([strategy], {"EURUSD": bars}, timeframe="D1", costs=costs, **kwargs).run()


def test_signal_fills_at_next_open_and_strategy_never_sees_the_future():
    bars = [bar(i, 1.10 + i * 0.001, 1.10 + i * 0.001 + 0.0005, 1.10 + i * 0.001 - 0.0005, 1.10 + i * 0.001 + 0.0002)
            for i in range(6)]
    s = Scripted({
        bars[1].time: Signal(LONG, stop_distance=0.01),
        bars[3].time: Signal(FLAT),
    }, lookback=2)
    result = run(s, bars)
    (trade,) = result.trades
    assert trade.entry_time == bars[2].time and trade.entry_price == pytest.approx(bars[2].open)
    assert trade.exit_time == bars[4].time and trade.exit_price == pytest.approx(bars[4].open)
    assert trade.units == 50_000  # 0.5% of 100k / 0.01
    assert trade.pnl == pytest.approx((bars[4].open - bars[2].open) * 50_000)
    for seen_time, window, _, decided_at in s.calls:
        assert window == 2 and decided_at == seen_time + timedelta(days=1)
    assert result.final_equity == pytest.approx(100_000 + trade.pnl)


def test_costs_spread_slippage_and_commission():
    costs = CostModel(spread_pips={"EURUSD": 1.0}, slippage_pips=0.5, commission_per_million=35.0)
    bars = daily_bars([1.1000] * 6)
    s = Scripted({bars[0].time: Signal(LONG, stop_distance=0.01), bars[2].time: Signal(FLAT)})
    (trade,) = run(s, bars, costs=costs).trades
    assert trade.entry_price == pytest.approx(1.1000 + 0.0001 + 0.00005)  # ask + slippage
    assert trade.exit_price == pytest.approx(1.1000 - 0.00005)  # bid - slippage
    notional_in = 50_000 * trade.entry_price
    notional_out = 50_000 * trade.exit_price
    assert trade.commission == pytest.approx((notional_in + notional_out) * 35 / 1e6)
    assert trade.gross_pnl == pytest.approx(-0.0002 * 50_000)


def test_long_stop_hit_intrabar_and_on_a_gap():
    bars = [bar(0, 1.10, 1.101, 1.099, 1.10), bar(1, 1.10, 1.101, 1.099, 1.10),
            bar(2, 1.10, 1.101, 1.085, 1.09), bar(3, 1.09, 1.09, 1.09, 1.09)]
    s = Scripted({bars[0].time: Signal(LONG, stop_distance=0.01)})
    (trade,) = run(s, bars).trades
    assert trade.exit_reason == "stop" and trade.exit_price == pytest.approx(1.09)
    assert trade.r_multiple == pytest.approx(-1.0)

    gap = [bar(0, 1.10, 1.101, 1.099, 1.10), bar(1, 1.10, 1.101, 1.099, 1.10), bar(2, 1.07, 1.08, 1.06, 1.075)]
    (gapped,) = run(Scripted({gap[0].time: Signal(LONG, stop_distance=0.01)}), gap).trades
    assert gapped.exit_price == pytest.approx(1.07)  # filled at the open, below the stop


def test_short_stop_triggers_on_the_ask():
    costs = CostModel(spread_pips={"EURUSD": 2.0}, slippage_pips=0.0, commission_per_million=0.0)
    # stop = 1.10 + 0.0050; bid high 1.1049 + spread 0.0002 crosses it
    bars = [bar(0, 1.10, 1.10, 1.10, 1.10), bar(1, 1.10, 1.1049, 1.099, 1.10)]
    (trade,) = run(Scripted({bars[0].time: Signal(SHORT, stop_distance=0.005)}), bars, costs=costs).trades
    assert trade.exit_reason == "stop" and trade.exit_price == pytest.approx(1.1050)


def test_trailing_stop_is_applied_from_the_next_bar():
    bars = [bar(0, 1.10, 1.10, 1.10, 1.10), bar(1, 1.10, 1.11, 1.10, 1.11),
            bar(2, 1.11, 1.12, 1.105, 1.12), bar(3, 1.12, 1.12, 1.104, 1.105)]
    s = Scripted({
        bars[0].time: Signal(LONG, stop_distance=0.05),
        bars[1].time: Signal(LONG, trailing_stop=1.104),  # bar 2's low (1.105) does not reach it
        bars[2].time: Signal(LONG, trailing_stop=1.106),
    })
    (trade,) = run(s, bars).trades
    assert trade.exit_time == bars[3].time and trade.exit_price == pytest.approx(1.106)


def test_swaps_accrue_with_triple_wednesday():
    spec = SymbolSpec("EURUSD", "EUR", "USD", swap_long=-1.0)  # -1 pip per unit per rollover
    bars = daily_bars([1.10] * 8)  # Mon .. Wed of the following week
    s = Scripted({bars[0].time: Signal(LONG, stop_distance=0.01), bars[6].time: Signal(FLAT)})
    (trade,) = run(s, bars, specs={"EURUSD": spec}).trades
    # held from Tuesday's open to the next Wednesday's open: Tue, Wed(x3), Thu, Fri, Mon, Tue = 8 charges
    assert trade.swap == pytest.approx(-8 * 0.0001 * 50_000)


def test_start_date_is_warm_up_only():
    bars = daily_bars([1.10] * 10)
    s = Scripted({b.time: Signal(LONG, stop_distance=0.01) for b in bars})
    result = run(s, bars, start=bars[5].time)
    assert result.trades == [] or all(t.entry_time > bars[5].time for t in result.trades)
    assert result.equity_curve[0][0] == bars[5].time + timedelta(days=1)


def test_daily_loss_halt_blocks_new_entries():
    # Long 40,000 from 1.10; bar 2 closes at 1.07: -1,200 (1.2%) against yesterday's close.
    bars = [bar(0, 1.10, 1.10, 1.10, 1.10), bar(1, 1.10, 1.10, 1.10, 1.10),
            bar(2, 1.10, 1.10, 1.07, 1.07), bar(3, 1.07, 1.07, 1.07, 1.07)]
    risk = RiskConfig(risk_per_trade=0.02, max_open_risk=0.05, max_currency_risk=0.05, max_daily_loss=0.01)
    s = Scripted({bars[0].time: Signal(LONG, stop_distance=0.05), bars[2].time: Signal(SHORT, stop_distance=0.05)})
    result = run(s, bars, risk=risk)
    assert result.halts and "daily loss" in result.halts[0][1]
    assert result.rejected_entries == 1  # the reversal closed the long but could not open the short
    (trade,) = result.trades
    assert trade.direction == LONG and trade.exit_time == bars[3].time


def test_conversion_pairs_are_required_for_crosses():
    bars = daily_bars([0.85] * 5)
    with pytest.raises(ValueError, match="cannot convert GBP"):
        Backtester([Scripted({})], {"EURGBP": bars}, timeframe="D1")
    gbp = daily_bars([1.25] * 5)
    s = Scripted({bars[0].time: Signal(LONG, stop_distance=0.005)})
    result = Backtester([s], {"EURGBP": bars, "GBPUSD": gbp}, symbols=["EURGBP"], timeframe="D1", costs=FREE).run()
    (trade,) = result.trades
    # 500 USD risk = units * 0.005 GBP * 1.25 USD/GBP
    assert trade.units == 80_000
