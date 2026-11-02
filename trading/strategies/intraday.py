"""Intraday (day-trading) strategies for index CFDs, gold and FX.

Both strategies trade one exchange session (see trading/sessions.py) and are
flat a few minutes before it closes. Holding nothing overnight means paying no
swap, which the daily research found to be the largest cost of holding CFDs.
See docs/RESEARCH.md, part II, for the evidence and the test results.
"""
from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from trading.models import FLAT, LONG, SHORT, TIMEFRAME_MINUTES, Bar, Position, Signal, timeframe_delta
from trading.sessions import Session, session_for
from trading.strategies.base import MarketContext, Strategy

INTRADAY_TIMEFRAMES = ("M1", "M5", "M15", "M30")


@dataclass(frozen=True)
class _Day:
    """A completed session: its open and close, and the absolute move from the open at each bar close."""

    day: date
    open: float
    close: float
    moves: dict[int, float]  # minutes after the open -> |close / open - 1|


@dataclass(frozen=True)
class _Today:
    start: datetime  # session open (UTC)
    end: datetime  # session close (UTC)
    minutes: int  # minutes from the open to the close of the latest bar
    bars: list[Bar]  # this session's bars so far
    closing: bool  # inside the flat window before the close


class IntradayStrategy(Strategy):
    """Session bookkeeping shared by the intraday strategies.

    Params must define ``session``, ``timeframe`` and ``flat_minutes``.
    """

    default_timeframe = "M5"
    history_days = 0  # completed sessions the strategy needs before today

    def __init__(self, params=None, **overrides) -> None:
        super().__init__(params, **overrides)
        self._bar = timeframe_delta(self.params.timeframe)
        self._sessions: dict[str, Session] = {}
        self._history: dict[str, tuple[date, list[_Day]]] = {}

    def validate(self) -> None:
        p = self.params
        if p.timeframe not in INTRADAY_TIMEFRAMES:
            raise ValueError(f"{self.name}: timeframe must be one of {', '.join(INTRADAY_TIMEFRAMES)}")
        if p.flat_minutes < 0:
            raise ValueError(f"{self.name}: flat_minutes must be >= 0")
        if p.session != "auto":
            session_for("", p.session)  # raises for unknown names

    def check_timeframe(self, timeframe: str) -> None:
        if timeframe != self.params.timeframe:
            raise ValueError(
                f"{self.name} is set up for {self.params.timeframe} bars but runs on {timeframe}; "
                f"pass --param timeframe={timeframe} or --timeframe {self.params.timeframe}"
            )

    def check_symbol(self, symbol: str) -> None:
        self.session(symbol)

    @property
    def lookback(self) -> int:
        # CFDs quote almost around the clock; leave room for holidays with no cash session.
        bars_per_day = 1440 // TIMEFRAME_MINUTES[self.params.timeframe]
        return (self.history_days + 5) * bars_per_day if self.history_days else bars_per_day + 2

    def session(self, symbol: str) -> Session:
        if symbol not in self._sessions:
            self._sessions[symbol] = session_for(symbol, self.params.session)
        return self._sessions[symbol]

    def _today(self, symbol: str, bars: Sequence[Bar]) -> _Today | None:
        """This session's bars if the latest bar closed inside the session, else None."""
        session = self.session(symbol)
        last = bars[-1]
        start, end = session.bounds(session.day_of(last.time))
        closes_at = last.time + self._bar
        if last.time < start or closes_at > end:
            return None
        i = len(bars) - 1
        while i > 0 and bars[i - 1].time >= start:
            i -= 1
        closing = closes_at >= end - timedelta(minutes=self.params.flat_minutes)
        minutes = int((closes_at - start).total_seconds()) // 60
        return _Today(start, end, minutes, list(bars[i:]), closing)

    def _completed(self, symbol: str, bars: Sequence[Bar], today: _Today) -> list[_Day]:
        """The last ``history_days + 1`` completed sessions before today (cached for the day)."""
        session = self.session(symbol)
        day = session.day_of(today.start)
        cached = self._history.get(symbol)
        if cached is not None and cached[0] == day:
            return cached[1]
        wanted = self.history_days + 1
        groups: list[list[Bar]] = []
        current: list[Bar] = []
        candidate = day - timedelta(days=1)
        start, end = session.bounds(candidate)
        for i in range(len(bars) - len(today.bars) - 1, -1, -1):
            b = bars[i]
            while b.time < start:
                if current:
                    groups.append(current)
                    current = []
                    if len(groups) == wanted:
                        break
                candidate -= timedelta(days=1)
                start, end = session.bounds(candidate)
            if len(groups) == wanted:
                break
            if b.time + self._bar <= end:
                current.append(b)
        if current and len(groups) < wanted:
            groups.append(current)
        days = []
        for group in reversed(groups):
            group.reverse()
            first = group[0]
            open_time = session.bounds(session.day_of(first.time))[0]
            moves = {
                int((b.time + self._bar - open_time).total_seconds()) // 60: abs(b.close / first.open - 1.0)
                for b in group
            }
            days.append(_Day(session.day_of(first.time), first.open, group[-1].close, moves))
        self._history[symbol] = (day, days)
        return days

    def _housekeeping(self, symbol: str, bars: Sequence[Bar], position: Position | None) -> tuple[_Today | None, Signal | None]:
        """Session checks every intraday strategy makes first. Returns (today, signal to return now or None)."""
        today = self._today(symbol, bars)
        if today is None:
            return None, Signal(FLAT, reason="outside the trading session") if position is not None else None
        if position is not None and position.entry_time < today.start:
            return None, Signal(FLAT, reason="position is left over from an earlier session")
        if today.closing:
            return None, Signal(FLAT, reason="session close")
        return today, None


@dataclass(frozen=True)
class NoiseParams:
    days: int = 14  # sessions averaged into the noise area
    band: float = 1.0  # noise-area width multiplier
    check: int = 30  # minutes between decisions (HH:00 and HH:30 for sessions opening on the half hour)
    stop_sigma: float = 0.5  # protective stop in daily standard deviations; sizes the position
    flat_minutes: int = 5  # be flat this long before the session close
    session: str = "auto"  # us, eu, uk, fx or auto (from the symbol)
    timeframe: str = "M5"


class NoiseBreakout(IntradayStrategy):
    """Intraday momentum: trade breakouts from the "noise area" (Zarattini, Aziz & Barbon 2024).

    For every time of day, the noise area spans the average absolute move
    from the session open over the last ``days`` sessions. Its upper bound
    starts from the higher of today's open and yesterday's close, its lower
    bound from the lower of the two, so an overnight gap does not count as a
    breakout. Every ``check`` minutes the strategy goes long above the upper
    bound and short below the lower bound. A long is held while the price
    stays above both the upper bound and the session's average price, a short
    while it stays below both; otherwise it is closed at that checkpoint.
    Everything is closed before the session ends.

    The broker-side stop sits ``stop_sigma`` daily standard deviations away.
    It protects against moves between checkpoints and sizes the position:
    at 1% risk per trade, 0.5 sigma gives the paper's exposure (2% daily
    volatility target). The average price is an equal-weighted average of
    each bar's typical price, because CFD tick volume is not traded volume.
    """

    name = "noise_breakout"
    Params = NoiseParams

    def validate(self) -> None:
        super().validate()
        p = self.params
        if p.days < 2 or p.band <= 0 or p.stop_sigma <= 0:
            raise ValueError("noise_breakout: need days >= 2, band > 0 and stop_sigma > 0")
        if p.check < TIMEFRAME_MINUTES[p.timeframe] or p.check % TIMEFRAME_MINUTES[p.timeframe]:
            raise ValueError("noise_breakout: check must be a multiple of the bar length")

    @property
    def history_days(self) -> int:  # type: ignore[override]
        return self.params.days

    def on_bar(
        self,
        symbol: str,
        bars: Sequence[Bar],
        position: Position | None,
        context: MarketContext,
    ) -> Signal | None:
        p = self.params
        today, early = self._housekeeping(symbol, bars, position)
        if today is None:
            return early
        if today.minutes % p.check:
            return None
        history = self._completed(symbol, bars, today)
        if len(history) < p.days + 1:
            return None
        recent = history[-p.days:]
        moves = [d.moves[today.minutes] for d in recent if today.minutes in d.moves]
        if len(moves) < p.days // 2:
            return None
        noise = statistics.fmean(moves)
        returns = [b.close / a.close - 1.0 for a, b in zip(history, history[1:])]
        sigma = statistics.stdev(returns)
        if not math.isfinite(sigma) or sigma <= 0:
            return None

        session_open, previous_close = today.bars[0].open, history[-1].close
        upper = max(session_open, previous_close) * (1 + p.band * noise)
        lower = min(session_open, previous_close) * (1 - p.band * noise)
        average = statistics.fmean((b.high + b.low + b.close) / 3 for b in today.bars)
        close = bars[-1].close
        stop = p.stop_sigma * sigma * close
        if close > upper:
            breakout = Signal(LONG, stop_distance=stop, reason=f"close {close:g} above noise area {upper:.5g}")
        elif close < lower:
            breakout = Signal(SHORT, stop_distance=stop, reason=f"close {close:g} below noise area {lower:.5g}")
        else:
            breakout = None

        if position is None:
            return breakout or Signal(FLAT, reason=f"inside noise area {lower:.5g}..{upper:.5g}")
        if breakout is not None and breakout.direction != position.direction:
            return breakout
        if position.direction == LONG:
            trail = max(upper, average)
            if close > trail:
                return Signal(LONG, reason=f"close {close:g} above trailing level {trail:.5g}")
            return Signal(FLAT, reason=f"close {close:g} fell below trailing level {trail:.5g}")
        trail = min(lower, average)
        if close < trail:
            return Signal(SHORT, reason=f"close {close:g} below trailing level {trail:.5g}")
        return Signal(FLAT, reason=f"close {close:g} rose above trailing level {trail:.5g}")


@dataclass(frozen=True)
class OrbParams:
    range_minutes: int = 5  # opening range length
    target_r: float = 10.0  # take profit at this multiple of the stop distance (0 = none)
    flat_minutes: int = 5
    session: str = "auto"
    timeframe: str = "M5"


class OpeningRangeBreakout(IntradayStrategy):
    """Opening range breakout (Zarattini & Aziz 2023).

    Trades in the direction of the session's first ``range_minutes``: long
    if the range closed above its open, short if below, nothing if flat. The
    stop goes at the other end of the range, the target at ``target_r`` times
    the stop distance, and an untouched position is closed before the
    session ends. One trade per session at most.
    """

    name = "orb"
    Params = OrbParams

    def validate(self) -> None:
        super().validate()
        p = self.params
        bar = TIMEFRAME_MINUTES[p.timeframe]
        if p.range_minutes < bar or p.range_minutes % bar:
            raise ValueError("orb: range_minutes must be a multiple of the bar length")
        if p.target_r < 0:
            raise ValueError("orb: target_r must be >= 0")

    def direction(self, range_open: float, range_close: float) -> int:
        return LONG if range_close > range_open else SHORT if range_close < range_open else FLAT

    def on_bar(
        self,
        symbol: str,
        bars: Sequence[Bar],
        position: Position | None,
        context: MarketContext,
    ) -> Signal | None:
        p = self.params
        today, early = self._housekeeping(symbol, bars, position)
        if today is None:
            return early
        if today.minutes != p.range_minutes or position is not None:
            return None
        first = today.bars[0]
        if first.time != today.start:
            return Signal(FLAT, reason="no bar at the session open")
        high = max(b.high for b in today.bars)
        low = min(b.low for b in today.bars)
        close = today.bars[-1].close
        direction = self.direction(first.open, close)
        if direction == FLAT:
            return Signal(FLAT, reason="opening range closed unchanged")
        stop = close - low if direction == LONG else high - close
        if stop <= 0:
            return Signal(FLAT, reason="opening range has no room for a stop")
        side = "up" if direction == LONG else "down"
        return Signal(direction, stop_distance=stop, take_profit_distance=p.target_r * stop or None,
                      reason=f"first {p.range_minutes} minutes closed {side} ({first.open:g} -> {close:g})")
