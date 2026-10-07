"""Carry with trend and volatility filters.

See docs/RESEARCH.md for the evidence and the reasoning behind the defaults.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from trading.indicators import return_volatility, sma
from trading.models import FLAT, LONG, SHORT, Bar, Position, Signal, side_name
from trading.strategies.base import MarketContext, Strategy, recent_atr


@dataclass(frozen=True)
class CarryParams:
    min_carry: float = 1.0  # minimum net carry to hold, % of notional per year
    trend: int = 100  # SMA length; hold only while price is on the carry side of it (0 = no trend filter)
    vol_fast: int = 20
    vol_slow: int = 250
    vol_cap: float = 1.5  # stand aside while vol_fast > vol_cap * vol_slow (0 = no volatility filter)
    atr: int = 20
    stop_atr: float = 3.0


class CarryTrend(Strategy):
    """Hold the side of a pair that earns swap, but only while trend and volatility agree.

    Carry is measured from the broker's own swap rates, so it is net of the
    broker's financing markup: on most pairs neither side earns anything and
    the strategy stays flat. Carry earns a premium on average but crashes in
    risk-off episodes (Brunnermeier, Nagel & Pedersen 2008; Menkhoff et al.
    2012). The trend filter (price above/below its SMA) and the volatility
    filter (stand aside while short-term volatility spikes) aim to step aside
    before the worst of those episodes.
    """

    name = "carry"
    Params = CarryParams

    def validate(self) -> None:
        p = self.params
        if p.trend < 0 or p.vol_fast < 2 or p.vol_slow <= p.vol_fast:
            raise ValueError("carry: need trend >= 0 and 2 <= vol_fast < vol_slow")
        if p.vol_cap < 0 or p.atr < 1 or p.stop_atr <= 0:
            raise ValueError("carry: need vol_cap >= 0, atr >= 1 and stop_atr > 0")

    @property
    def lookback(self) -> int:
        p = self.params
        return max(p.trend, p.vol_slow + 1 if p.vol_cap else 0, 3 * p.atr + 1)

    def on_bar(
        self,
        symbol: str,
        bars: Sequence[Bar],
        position: Position | None,
        context: MarketContext,
    ) -> Signal | None:
        if context.carry is None:
            return None
        p = self.params
        closes = [b.close for b in bars]
        close = closes[-1]
        long_carry = context.carry.annual_carry(symbol, LONG, close, context.time)
        short_carry = context.carry.annual_carry(symbol, SHORT, close, context.time)
        average_range = recent_atr(bars, p.atr)
        if long_carry is None or short_carry is None or average_range is None or len(closes) < self.lookback:
            return None

        direction, carry = (LONG, long_carry) if long_carry >= short_carry else (SHORT, short_carry)
        side = side_name(direction)
        if carry < p.min_carry:
            return Signal(FLAT, reason=f"best carry {carry:.2f}%/yr ({side}) < {p.min_carry}%")
        if p.trend and direction * (close - sma(closes, p.trend)) <= 0:
            return Signal(FLAT, reason=f"{side} carry {carry:.2f}%/yr but price on wrong side of SMA{p.trend}")
        if p.vol_cap:
            vol_fast = return_volatility(closes, p.vol_fast)
            vol_slow = return_volatility(closes, p.vol_slow)
            if vol_fast > p.vol_cap * vol_slow:
                return Signal(FLAT, reason=f"volatility spike ({vol_fast / vol_slow:.2f}x normal)")

        reason = f"{side} carry {carry:.2f}%/yr with trend"
        if position is not None and position.direction == direction:
            return Signal(direction, reason=reason)
        return Signal(direction, stop_distance=p.stop_atr * average_range, reason=reason)
