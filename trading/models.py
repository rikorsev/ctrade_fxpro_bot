"""Data types shared by strategies, the risk manager, the backtester and the live trader."""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta

LONG = 1
SHORT = -1
FLAT = 0

TIMEFRAME_MINUTES = {
    "M1": 1,
    "M5": 5,
    "M15": 15,
    "M30": 30,
    "H1": 60,
    "H4": 240,
    "H12": 720,
    "D1": 1440,
    "W1": 10080,
}


def timeframe_delta(timeframe: str) -> timedelta:
    try:
        return timedelta(minutes=TIMEFRAME_MINUTES[timeframe])
    except KeyError:
        raise ValueError(
            f"Unsupported timeframe {timeframe!r}; use one of {', '.join(TIMEFRAME_MINUTES)}"
        ) from None


def side_name(direction: int) -> str:
    return {LONG: "long", SHORT: "short", FLAT: "flat"}[direction]


@dataclass(frozen=True, slots=True)
class Bar:
    """One OHLC bar. ``time`` is the bar's *open* time in UTC (as cTrader reports it)."""

    time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


@dataclass(frozen=True)
class SymbolSpec:
    """Contract details needed for sizing, rounding and cost calculations.

    Volumes are in units of the base currency (cTrader's protocol uses
    hundredths of a unit; the broker adapter converts).
    """

    name: str
    base: str
    quote: str
    digits: int = 5
    pip_position: int = 4
    lot_size: float = 100_000.0
    min_volume: float = 1_000.0
    volume_step: float = 1_000.0
    max_volume: float | None = None
    # Broker swap rates per rollover. ``swap_type`` is "pips", "points" or
    # "percent" (annual %, like cTrader's PERCENTAGE swap calculation type).
    swap_long: float = 0.0
    swap_short: float = 0.0
    swap_type: str = "pips"
    triple_swap_weekday: int = 2  # Python weekday (Monday=0): Wednesday
    symbol_id: int | None = None

    @property
    def pip_size(self) -> float:
        return 10.0 ** -self.pip_position

    @property
    def point_size(self) -> float:
        return 10.0 ** -self.digits

    def round_price(self, price: float) -> float:
        return round(price, self.digits)

    def normalize_units(self, units: float) -> float:
        """Round down to the volume step and clamp to broker limits (0 if below the minimum)."""
        if units <= 0:
            return 0.0
        step = self.volume_step
        units = math.floor(units / step + 1e-9) * step
        if self.max_volume is not None:
            units = min(units, math.floor(self.max_volume / step + 1e-9) * step)
        return units if units >= self.min_volume else 0.0

    @classmethod
    def infer(cls, name: str) -> SymbolSpec:
        """Default spec for a plain six-letter FX pair such as EURUSD or USDJPY."""
        name = name.upper()
        if len(name) != 6 or not name.isalpha():
            raise ValueError(
                f"Cannot infer contract details for {name!r}; fetch symbol specs from cTrader first"
            )
        base, quote = name[:3], name[3:]
        pip_position = 2 if quote == "JPY" else 4
        return cls(name=name, base=base, quote=quote, digits=pip_position + 1, pip_position=pip_position)


@dataclass
class Position:
    """An open position managed by one strategy on one symbol."""

    symbol: str
    strategy: str
    direction: int
    units: float
    entry_price: float
    entry_time: datetime
    stop_loss: float | None = None
    take_profit: float | None = None
    position_id: int | None = None


@dataclass(frozen=True)
class Signal:
    """A strategy's view after a bar closes.

    ``direction`` is the position the strategy wants (LONG, SHORT or FLAT).
    New entries need ``stop_distance`` (price distance from the fill to the
    protective stop). ``trailing_stop`` is an absolute level for an existing
    position's stop; it is only ever applied when it tightens the stop.
    """

    direction: int
    stop_distance: float | None = None
    take_profit_distance: float | None = None
    trailing_stop: float | None = None
    reason: str = ""


@dataclass(frozen=True)
class AccountState:
    balance: float
    equity: float
    currency: str
