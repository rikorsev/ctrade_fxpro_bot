"""Indicators over plain sequences.

Every function returns the value for the *last* element of its input (or None
while there is not enough data). Strategies receive a fixed-length window of
closed bars, so the backtester and the live trader feed these functions
identical inputs and get identical results.
"""
from __future__ import annotations

import math
from collections.abc import Sequence

from trading.models import Bar


def _check_period(period: int) -> None:
    if period < 1:
        raise ValueError(f"period must be >= 1, got {period}")


def sma(values: Sequence[float], period: int) -> float | None:
    _check_period(period)
    if len(values) < period:
        return None
    return math.fsum(values[-period:]) / period


def ema(values: Sequence[float], period: int) -> float | None:
    """Exponential moving average seeded with the SMA of the first ``period`` values."""
    _check_period(period)
    if len(values) < period:
        return None
    alpha = 2.0 / (period + 1)
    value = math.fsum(values[:period]) / period
    for x in values[period:]:
        value += alpha * (x - value)
    return value


def true_ranges(bars: Sequence[Bar]) -> list[float]:
    """True range of every bar after the first (the first has no previous close)."""
    return [
        max(cur.high - cur.low, abs(cur.high - prev.close), abs(cur.low - prev.close))
        for prev, cur in zip(bars, bars[1:])
    ]


def atr(bars: Sequence[Bar], period: int) -> float | None:
    """Wilder's average true range over the whole window."""
    _check_period(period)
    trs = true_ranges(bars)
    if len(trs) < period:
        return None
    value = math.fsum(trs[:period]) / period
    for tr in trs[period:]:
        value += (tr - value) / period
    return value


def donchian(bars: Sequence[Bar], period: int) -> tuple[float, float] | None:
    """Highest high and lowest low of the ``period`` bars *before* the last bar."""
    _check_period(period)
    if len(bars) < period + 1:
        return None
    window = bars[-period - 1:-1]
    return max(b.high for b in window), min(b.low for b in window)


def rate_of_change(values: Sequence[float], period: int) -> float | None:
    _check_period(period)
    if len(values) < period + 1:
        return None
    return values[-1] / values[-1 - period] - 1.0


def return_volatility(values: Sequence[float], period: int) -> float | None:
    """Sample standard deviation of the last ``period`` log returns (per bar)."""
    if period < 2:
        raise ValueError(f"period must be >= 2, got {period}")
    if len(values) < period + 1:
        return None
    window = values[-period - 1:]
    returns = [math.log(b / a) for a, b in zip(window, window[1:])]
    mean = math.fsum(returns) / period
    return math.sqrt(math.fsum((r - mean) ** 2 for r in returns) / (period - 1))


def rsi(values: Sequence[float], period: int = 14) -> float | None:
    """Wilder's relative strength index."""
    _check_period(period)
    if len(values) < period + 1:
        return None
    changes = [b - a for a, b in zip(values, values[1:])]
    gain = math.fsum(max(c, 0.0) for c in changes[:period]) / period
    loss = math.fsum(max(-c, 0.0) for c in changes[:period]) / period
    for c in changes[period:]:
        gain += (max(c, 0.0) - gain) / period
        loss += (max(-c, 0.0) - loss) / period
    if loss == 0:
        return 100.0 if gain > 0 else 50.0
    return 100.0 - 100.0 / (1.0 + gain / loss)


def bollinger(values: Sequence[float], period: int = 20, width: float = 2.0) -> tuple[float, float, float] | None:
    """(middle, upper, lower) bands using the population standard deviation."""
    if period < 2:
        raise ValueError(f"period must be >= 2, got {period}")
    if len(values) < period:
        return None
    window = values[-period:]
    mid = math.fsum(window) / period
    dev = math.sqrt(math.fsum((x - mid) ** 2 for x in window) / period)
    return mid, mid + width * dev, mid - width * dev
