"""Trend-following strategies: the family with the strongest long-run evidence in FX.

See docs/RESEARCH.md for the evidence and the reasoning behind the defaults.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from trading.indicators import donchian, rate_of_change
from trading.models import FLAT, LONG, SHORT, Bar, Position, Signal
from trading.strategies.base import MarketContext, Strategy, recent_atr


@dataclass(frozen=True)
class DonchianParams:
    entry: int = 55  # breakout channel (bars)
    exit: int = 20  # trailing exit channel (bars)
    atr: int = 20
    stop_atr: float = 2.0  # initial stop distance in ATRs


class DonchianTrend(Strategy):
    """Channel breakout in the style of the Turtles' "System 2".

    Enters when the close breaks the previous ``entry``-bar high (long) or low
    (short). The initial stop is ``stop_atr`` ATRs from the fill; afterwards
    the stop trails the opposite ``exit``-bar channel and is only ever
    tightened. Every exit is therefore a broker-side stop, which keeps the
    position protected even if the bot is offline.
    """

    name = "donchian"
    Params = DonchianParams

    def validate(self) -> None:
        p = self.params
        if not 1 <= p.exit < p.entry:
            raise ValueError("donchian: need 1 <= exit < entry")
        if p.atr < 1 or p.stop_atr <= 0:
            raise ValueError("donchian: atr must be >= 1 and stop_atr > 0")

    @property
    def lookback(self) -> int:
        return max(self.params.entry, 3 * self.params.atr) + 1

    def on_bar(
        self,
        symbol: str,
        bars: Sequence[Bar],
        position: Position | None,
        context: MarketContext,
    ) -> Signal | None:
        p = self.params
        entry_channel = donchian(bars, p.entry)
        exit_channel = donchian(bars, p.exit)
        average_range = recent_atr(bars, p.atr)
        if entry_channel is None or exit_channel is None or average_range is None:
            return None

        close = bars[-1].close
        entry_high, entry_low = entry_channel
        exit_high, exit_low = exit_channel
        stop = p.stop_atr * average_range
        long_breakout = Signal(LONG, stop_distance=stop, reason=f"close {close:g} > {p.entry}-bar high {entry_high:g}")
        short_breakout = Signal(SHORT, stop_distance=stop, reason=f"close {close:g} < {p.entry}-bar low {entry_low:g}")

        if position is None:
            if close > entry_high:
                return long_breakout
            if close < entry_low:
                return short_breakout
            return Signal(FLAT, reason="no breakout")

        if position.direction == LONG:
            if close < entry_low:
                return short_breakout
            if close < exit_low:
                return Signal(FLAT, reason=f"close {close:g} < {p.exit}-bar low {exit_low:g}")
            return Signal(LONG, trailing_stop=exit_low, reason=f"trail stop to {p.exit}-bar low {exit_low:g}")

        if close > entry_high:
            return long_breakout
        if close > exit_high:
            return Signal(FLAT, reason=f"close {close:g} > {p.exit}-bar high {exit_high:g}")
        return Signal(SHORT, trailing_stop=exit_high, reason=f"trail stop to {p.exit}-bar high {exit_high:g}")


@dataclass(frozen=True)
class TsmomParams:
    # Lookbacks in bars; the defaults are ~3, 6 and 12 months of daily bars.
    fast: int = 63
    medium: int = 126
    slow: int = 252
    atr: int = 20
    stop_atr: float = 3.0
    rebalance: str = "monthly"  # when the vote may change the position: daily, weekly or monthly


class TimeSeriesMomentum(Strategy):
    """Multi-horizon time-series momentum (Moskowitz, Ooi & Pedersen 2012).

    Each lookback votes with the sign of its return and the strategy holds
    the majority direction, so it is always in the market once warmed up.
    As in the literature, the vote is only acted on at the first bar of each
    month by default; re-voting daily whipsaws whenever one horizon hovers
    around zero. The 1-month signal is deliberately left out: Hurst, Ooi &
    Pedersen (2017) show it decayed to a ~0 Sharpe ratio after 2010 while the
    3- and 12-month signals held up. The ATR stop is a disaster stop and sizes
    the position; after a stop-out the position is re-entered at the next
    rebalance if the vote still agrees.
    """

    name = "tsmom"
    Params = TsmomParams

    def validate(self) -> None:
        p = self.params
        if not 1 <= p.fast < p.medium < p.slow:
            raise ValueError("tsmom: need 1 <= fast < medium < slow")
        if p.atr < 1 or p.stop_atr <= 0:
            raise ValueError("tsmom: atr must be >= 1 and stop_atr > 0")
        if p.rebalance not in ("daily", "weekly", "monthly"):
            raise ValueError("tsmom: rebalance must be daily, weekly or monthly")

    def _rebalance_bar(self, bars: Sequence[Bar]) -> bool:
        """True on the first bar of a new period (always true for daily rebalancing)."""
        if self.params.rebalance == "daily" or len(bars) < 2:
            return True
        now, before = bars[-1].time, bars[-2].time
        if self.params.rebalance == "weekly":
            return now.isocalendar()[:2] != before.isocalendar()[:2]
        return (now.year, now.month) != (before.year, before.month)

    @property
    def lookback(self) -> int:
        return max(self.params.slow, 3 * self.params.atr) + 1

    def on_bar(
        self,
        symbol: str,
        bars: Sequence[Bar],
        position: Position | None,
        context: MarketContext,
    ) -> Signal | None:
        p = self.params
        closes = [b.close for b in bars]
        returns = [rate_of_change(closes, n) for n in (p.fast, p.medium, p.slow)]
        average_range = recent_atr(bars, p.atr)
        if average_range is None or any(r is None for r in returns):
            return None

        if not self._rebalance_bar(bars):
            if position is None:
                return Signal(FLAT, reason=f"waiting for the {p.rebalance} rebalance")
            return Signal(position.direction, reason=f"holding until the {p.rebalance} rebalance")

        score = sum((r > 0) - (r < 0) for r in returns)
        direction = LONG if score > 0 else SHORT if score < 0 else FLAT
        reason = "momentum vote {:+d} ({})".format(
            score, ", ".join(f"{n} bars {r:+.1%}" for n, r in zip((p.fast, p.medium, p.slow), returns))
        )
        if direction == FLAT or (position is not None and position.direction == direction):
            return Signal(direction, reason=reason)
        return Signal(direction, stop_distance=p.stop_atr * average_range, reason=reason)
