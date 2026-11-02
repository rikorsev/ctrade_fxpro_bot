from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from trading.models import Bar, Position, Signal
from trading.sessions import SESSIONS
from trading.strategies import MarketContext, Strategy

UTC = timezone.utc
MONDAY = datetime(2024, 1, 1, tzinfo=UTC)  # 2024-01-01 was a Monday


def daily_bars(closes: Sequence[float], start: datetime = MONDAY, wick: float = 0.0) -> list[Bar]:
    """Weekday daily bars; each opens at the previous close, with ``wick`` added beyond the body."""
    bars, t, previous = [], start, closes[0]
    for close in closes:
        while t.weekday() >= 5:
            t += timedelta(days=1)
        bars.append(Bar(t, previous, max(previous, close) + wick, min(previous, close) - wick, close))
        previous = close
        t += timedelta(days=1)
    return bars


def bar(day: int, o: float, h: float, low: float, c: float, start: datetime = MONDAY) -> Bar:
    return Bar(start + timedelta(days=day), o, h, low, c)


@dataclass(frozen=True)
class _NoParams:
    pass


class Scripted(Strategy):
    """Returns pre-programmed signals keyed by bar time and records what it was shown."""

    name = "scripted"
    Params = _NoParams

    def __init__(self, signals: dict[datetime, Signal], lookback: int = 1, name: str = "scripted") -> None:
        super().__init__()
        self.signals = signals
        self._lookback = lookback
        self.name = name
        self.calls: list[tuple[datetime, int, Position | None, datetime]] = []

    @property
    def lookback(self) -> int:
        return self._lookback

    def on_bar(self, symbol, bars, position, context: MarketContext):
        self.calls.append((bars[-1].time, len(bars), position, context.time))
        return self.signals.get(bars[-1].time)


class FixedCarry:
    """Carry source returning fixed % per year by (symbol, direction)."""

    def __init__(self, table: dict[tuple[str, int], float | None]) -> None:
        self.table = table

    def annual_carry(self, symbol, direction, price, time):
        return self.table.get((symbol, direction))


def session_days(start: date, count: int) -> list[date]:
    """``count`` consecutive weekdays from ``start``."""
    days, d = [], start
    while len(days) < count:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    return days


def session_bars(day: date, opening: float, closes: Sequence[float], session: str = "us",
                 minutes: int = 5) -> list[Bar]:
    """Bars from the session open on ``day``; each opens at the previous close."""
    start, _ = SESSIONS[session].bounds(day)
    bars, previous = [], opening
    for i, close in enumerate(closes):
        bars.append(Bar(start + timedelta(minutes=minutes * i), previous, max(previous, close),
                        min(previous, close), close))
        previous = close
    return bars


def quiet_sessions(days: Sequence[date], opening: float = 100.0, wiggle: float = 0.001,
                   session: str = "us") -> list[Bar]:
    """Full sessions that zigzag ``wiggle`` around their open and close alternately up and down a little."""
    bars_per_session = SESSIONS[session].minutes() // 5
    bars, price = [], opening
    for n, day in enumerate(days):
        sign = 1 if n % 2 == 0 else -1
        closes = [price * (1 + wiggle * (1 if i % 2 == 0 else -1)) for i in range(bars_per_session - 1)]
        closes.append(price * (1 + sign * wiggle))
        bars += session_bars(day, price, closes, session)
        price = closes[-1]
    return bars
