from __future__ import annotations

import dataclasses
from typing import Any

from trading.strategies.base import CarrySource, MarketContext, Strategy
from trading.strategies.carry import CarryTrend
from trading.strategies.intraday import IntradayStrategy, NoiseBreakout, OpeningRangeBreakout
from trading.strategies.trend import DonchianTrend, TimeSeriesMomentum

STRATEGIES: dict[str, type[Strategy]] = {
    cls.name: cls for cls in (NoiseBreakout, OpeningRangeBreakout, DonchianTrend, TimeSeriesMomentum, CarryTrend)
}


def create_strategy(name: str, **overrides: Any) -> Strategy:
    try:
        cls = STRATEGIES[name]
    except KeyError:
        raise ValueError(f"Unknown strategy {name!r}; available: {', '.join(STRATEGIES)}") from None
    return cls(**overrides)


def with_timeframe(name: str, overrides: dict[str, Any], timeframe: str) -> dict[str, Any]:
    """Add ``timeframe`` to the overrides of a strategy that has a timeframe parameter, unless set already."""
    fields = {f.name for f in dataclasses.fields(STRATEGIES[name].Params)} if name in STRATEGIES else set()
    if "timeframe" in fields and "timeframe" not in overrides:
        return {**overrides, "timeframe": timeframe}
    return overrides


__all__ = [
    "STRATEGIES",
    "CarrySource",
    "CarryTrend",
    "DonchianTrend",
    "IntradayStrategy",
    "MarketContext",
    "NoiseBreakout",
    "OpeningRangeBreakout",
    "Strategy",
    "TimeSeriesMomentum",
    "create_strategy",
    "with_timeframe",
]
