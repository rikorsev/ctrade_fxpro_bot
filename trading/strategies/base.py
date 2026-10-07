from __future__ import annotations

import dataclasses
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, ClassVar, Protocol

from trading.indicators import atr
from trading.models import Bar, Position, Signal


class CarrySource(Protocol):
    def annual_carry(self, symbol: str, direction: int, price: float, time: datetime) -> float | None:
        """Net carry in % of notional per year for holding ``direction``, after the broker's swap markup."""


@dataclass(frozen=True)
class MarketContext:
    time: datetime  # close time of the bar being evaluated
    carry: CarrySource | None = None


class Strategy(ABC):
    """Turns a window of closed bars into a ``Signal``.

    Strategies are pure: no I/O, no clock, no broker. The same instance is
    driven bar by bar by the backtester and by the live trader, and both pass
    exactly ``lookback`` closed bars (oldest first).
    """

    name: ClassVar[str]
    Params: ClassVar[type]  # frozen dataclass whose fields all have defaults

    def __init__(self, params: Any = None, **overrides: Any) -> None:
        params = params if params is not None else self.Params()
        if overrides:
            params = dataclasses.replace(params, **coerce_params(self.Params, overrides))
        self.params = params
        self.validate()

    def validate(self) -> None:
        """Raise ValueError for inconsistent parameters."""

    @property
    @abstractmethod
    def lookback(self) -> int:
        """Number of closed bars ``on_bar`` needs."""

    @abstractmethod
    def on_bar(
        self,
        symbol: str,
        bars: Sequence[Bar],
        position: Position | None,
        context: MarketContext,
    ) -> Signal | None:
        """Return the desired position after ``bars[-1]`` closed, or None for "no opinion"."""

    def describe(self) -> str:
        values = ", ".join(f"{f.name}={getattr(self.params, f.name)}" for f in dataclasses.fields(self.params))
        return f"{self.name}({values})"


def recent_atr(bars: Sequence[Bar], period: int) -> float | None:
    """ATR over a fixed window of 3x the period, so results don't depend on how much history is passed in."""
    return atr(bars[-(3 * period + 1):], period)


def coerce_params(params_cls: type, overrides: Mapping[str, Any]) -> dict[str, Any]:
    """Convert overrides (often strings from the command line) to the types of the dataclass defaults."""
    fields = {f.name: f for f in dataclasses.fields(params_cls)}
    coerced = {}
    for key, raw in overrides.items():
        if key not in fields:
            raise ValueError(
                f"Unknown parameter {key!r} for {params_cls.__name__}; valid: {', '.join(fields)}"
            )
        kind = type(fields[key].default)
        if kind is bool:
            coerced[key] = raw if isinstance(raw, bool) else str(raw).strip().lower() in ("1", "true", "yes", "on")
        else:
            try:
                coerced[key] = kind(raw)
            except (TypeError, ValueError):
                raise ValueError(f"Parameter {key!r} expects {kind.__name__}, got {raw!r}") from None
    return coerced
