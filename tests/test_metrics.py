import math
import random
from datetime import datetime, timedelta, timezone

import pytest

from trading import metrics
from trading.backtest import BacktestResult

T0 = datetime(2020, 1, 1, tzinfo=timezone.utc)


def test_drawdown_and_returns():
    assert metrics.max_drawdown([100, 120, 90, 130]) == pytest.approx(0.25)
    assert metrics.returns([100, 110, 99]) == pytest.approx([0.1, -0.1])
    points = [(T0 + timedelta(days=d), v) for d, v in enumerate([100, 120, 90, 110, 121])]
    assert metrics.longest_drawdown_days(points) == 3


def test_sharpe_family():
    assert math.isnan(metrics.sharpe([0.01, 0.01, 0.01]))
    rnd = random.Random(7)
    good = [rnd.gauss(0.001, 0.01) for _ in range(1000)]
    assert metrics.sharpe(good) > 0
    psr = metrics.probabilistic_sharpe(good)
    assert 0.5 < psr <= 1
    # Selecting the best of many trials raises the bar the result must clear.
    trials = [metrics.per_period_sharpe([rnd.gauss(0, 0.01) for _ in range(1000)]) for _ in range(50)]
    assert metrics.expected_max_sharpe(trials) > 0
    assert metrics.deflated_sharpe(good, trials) < psr


def test_summarize_and_yearly_returns():
    curve = [(T0 + timedelta(days=d), 100_000 * (1.0005 ** d)) for d in range(1, 800)]
    result = BacktestResult(100_000, "USD", equity_curve=curve)
    s = metrics.summarize(result)
    assert s.max_drawdown == 0 and s.cagr > 0 and s.trades == 0
    assert "CAGR" in s.format()
    years = metrics.yearly_returns(result)
    assert [y for y, _ in years] == [2020, 2021, 2022]
    assert all(r > 0 for _, r in years)
