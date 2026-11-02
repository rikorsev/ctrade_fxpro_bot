"""Data types shared by strategies, the risk manager, the backtester and the live trader."""
from __future__ import annotations

import math
from collections.abc import Iterator, Sequence
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


class BarWindow(Sequence[Bar]):
    """A read-only view of ``bars[start:stop]`` that is free to create.

    The backtester hands strategies one of these on every bar instead of
    copying thousands of intraday bars into a new list each time. Slicing it
    returns a plain list.
    """

    __slots__ = ("_bars", "_start", "_stop")

    def __init__(self, bars: list[Bar], start: int, stop: int) -> None:
        self._bars = bars
        self._start = max(start, 0)
        self._stop = min(stop, len(bars))

    def __len__(self) -> int:
        return max(self._stop - self._start, 0)

    def __getitem__(self, index):
        if isinstance(index, slice):
            r = range(len(self))[index]
            if r.step == 1:
                return self._bars[self._start + r.start:self._start + r.stop]
            return [self._bars[self._start + i] for i in r]
        n = len(self)
        if index < 0:
            index += n
        if not 0 <= index < n:
            raise IndexError("bar window index out of range")
        return self._bars[self._start + index]

    def __iter__(self) -> Iterator[Bar]:
        return map(self._bars.__getitem__, range(self._start, self._stop))

    def __reversed__(self) -> Iterator[Bar]:
        return map(self._bars.__getitem__, range(self._stop - 1, self._start - 1, -1))


# Approximate FxPro cTrader contract details for common CFDs, so backtests on
# downloaded research data work before `fetch` has saved the real ones.
# Swaps are % of notional per year; intraday strategies never pay them.
KNOWN_CFDS = {
    "#USSPX500": dict(base="USSPX500", quote="USD", swap_long=-6.2, swap_short=1.1),
    "#USNDAQ100": dict(base="USNDAQ100", quote="USD", swap_long=-6.0, swap_short=1.1),
    "#US30": dict(base="US30", quote="USD", swap_long=-6.5, swap_short=1.1),
    "#GERMANY40": dict(base="GERMANY40", quote="EUR", swap_long=-5.0, swap_short=-0.4),
    "#UK100": dict(base="UK100", quote="GBP", swap_long=-6.5, swap_short=1.2),
    "XAUUSD": dict(base="XAU", quote="USD", swap_long=-6.1, swap_short=1.5, pip_position=1, lot_size=100.0,
                   min_volume=1.0, volume_step=1.0),
}


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
        """Default spec for a plain six-letter FX pair such as EURUSD, or for a CFD in ``KNOWN_CFDS``."""
        name = name.upper()
        if name in KNOWN_CFDS:
            fields = dict(digits=2, pip_position=0, lot_size=1.0, min_volume=0.01, volume_step=0.01,
                          swap_type="percent")
            fields.update(KNOWN_CFDS[name])
            return cls(name=name, **fields)
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
