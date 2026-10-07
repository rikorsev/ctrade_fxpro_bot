from __future__ import annotations

from typing import Any

from trading.strategies.base import CarrySource, MarketContext, Strategy
from trading.strategies.carry import CarryTrend
from trading.strategies.trend import DonchianTrend, TimeSeriesMomentum

STRATEGIES: dict[str, type[Strategy]] = {
    cls.name: cls for cls in (DonchianTrend, TimeSeriesMomentum, CarryTrend)
}


def create_strategy(name: str, **overrides: Any) -> Strategy:
    try:
        cls = STRATEGIES[name]
    except KeyError:
        raise ValueError(f"Unknown strategy {name!r}; available: {', '.join(STRATEGIES)}") from None
    return cls(**overrides)


__all__ = [
    "STRATEGIES",
    "CarrySource",
    "CarryTrend",
    "DonchianTrend",
    "MarketContext",
    "Strategy",
    "TimeSeriesMomentum",
    "create_strategy",
]
