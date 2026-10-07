import math

import pytest

from tests.helpers import bar
from trading import indicators as ind


def test_sma_and_ema():
    assert ind.sma([1, 2, 3, 4], 2) == 3.5
    assert ind.sma([1], 2) is None
    # seed = mean(1, 2, 3) = 2, alpha = 0.5: 4 -> 3, 5 -> 4
    assert ind.ema([1, 2, 3, 4, 5], 3) == pytest.approx(4.0)
    assert ind.ema([1, 2], 3) is None
    with pytest.raises(ValueError):
        ind.sma([1, 2], 0)


def test_true_range_and_wilder_atr():
    bars = [
        bar(0, 10, 11, 9, 10),
        bar(1, 10, 12, 10, 11),  # TR = 2
        bar(2, 11, 11.5, 8, 9),  # TR = 3.5
        bar(3, 15, 16, 15, 15.5),  # gap: TR = 16 - 9 = 7
    ]
    assert ind.true_ranges(bars) == [2, 3.5, 7]
    # seed (2 + 3.5) / 2 = 2.75, then 2.75 + (7 - 2.75) / 2
    assert ind.atr(bars, 2) == pytest.approx(4.875)
    assert ind.atr(bars[:2], 2) is None


def test_donchian_excludes_the_current_bar():
    bars = [bar(0, 1, 5, 1, 2), bar(1, 2, 3, 0.5, 2), bar(2, 2, 100, -100, 2)]
    assert ind.donchian(bars, 2) == (5, 0.5)
    assert ind.donchian(bars[:2], 2) is None


def test_rate_of_change_and_volatility():
    assert ind.rate_of_change([100, 50, 110], 2) == pytest.approx(0.1)
    assert ind.rate_of_change([100, 110], 2) is None
    expected = abs(math.log(1.1) - math.log(0.9)) / math.sqrt(2)
    assert ind.return_volatility([100, 110, 99], 2) == pytest.approx(expected)


def test_rsi_extremes_and_bollinger():
    assert ind.rsi([1, 2, 3, 4, 5], 3) == 100.0
    assert ind.rsi([5, 4, 3, 2, 1], 3) == pytest.approx(0.0)
    assert ind.rsi([1, 1, 1, 1], 3) == 50.0
    mid, upper, lower = ind.bollinger([1, 2, 3, 4, 5], 5, 2.0)
    assert mid == 3
    assert upper == pytest.approx(3 + 2 * math.sqrt(2))
    assert lower == pytest.approx(3 - 2 * math.sqrt(2))
