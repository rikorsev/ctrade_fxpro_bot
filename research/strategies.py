"""Research-only strategies: a random-entry control and a popular mean-reversion rule.

They are not registered for live trading; they exist to test the main
strategies against (see docs/RESEARCH.md).
"""
from __future__ import annotations

import random
from dataclasses import dataclass

from trading.indicators import donchian, rsi, sma
from trading.models import FLAT, LONG, SHORT, Signal
from trading.strategies.base import Strategy, recent_atr


@dataclass(frozen=True)
class RandomParams:
    probability: float = 0.04  # chance of entering on any bar while flat
    seed: int = 0
    exit: int = 20
    atr: int = 20
    stop_atr: float = 2.0


class RandomEntry(Strategy):
    """Coin-flip entries with exactly the Donchian strategy's stops, trailing exits and sizing.

    If Donchian cannot beat this, its breakout entries add nothing beyond the
    exit and risk management that both share.
    """

    name = "random"
    Params = RandomParams

    def __init__(self, params=None, **overrides) -> None:
        super().__init__(params, **overrides)
        self.rng = random.Random(self.params.seed)

    @property
    def lookback(self) -> int:
        return max(self.params.exit, 3 * self.params.atr) + 1

    def on_bar(self, symbol, bars, position, context):
        p = self.params
        channel = donchian(bars, p.exit)
        average_range = recent_atr(bars, p.atr)
        if channel is None or average_range is None:
            return None
        close = bars[-1].close
        high, low = channel
        if position is None:
            if self.rng.random() < p.probability:
                return Signal(self.rng.choice((LONG, SHORT)), stop_distance=p.stop_atr * average_range)
            return Signal(FLAT)
        if position.direction == LONG:
            return Signal(FLAT) if close < low else Signal(LONG, trailing_stop=low)
        return Signal(FLAT) if close > high else Signal(SHORT, trailing_stop=high)


@dataclass(frozen=True)
class DollarCarryParams:
    min_carry: float = 0.0  # net % per year the basket must earn to hold it
    atr: int = 20
    stop_atr: float = 3.0


DOLLAR_BASKET = ("EURUSD", "GBPUSD", "AUDUSD", "NZDUSD", "USDJPY", "USDCHF", "USDCAD", "USDNOK", "USDSEK")


class DollarCarry(Strategy):
    """Dollar carry (Lustig, Roussanov & Verdelhan 2014): long every foreign currency against the
    dollar while the basket's average net carry is positive, short them all while it is negative.

    Re-evaluated at the first bar of each month. Only meaningful for USD pairs.
    """

    name = "dollar_carry"
    Params = DollarCarryParams

    @property
    def lookback(self) -> int:
        return 3 * self.params.atr + 2

    def on_bar(self, symbol, bars, position, context):
        p = self.params
        if context.carry is None or "USD" not in (symbol[:3], symbol[3:]):
            return None
        new_month = (bars[-1].time.year, bars[-1].time.month) != (bars[-2].time.year, bars[-2].time.month)
        if not new_month:
            return Signal(position.direction if position else FLAT)
        long_foreign, short_foreign = [], []
        for pair in DOLLAR_BASKET:
            foreign_long = LONG if pair.endswith("USD") else SHORT
            a = context.carry.annual_carry(pair, foreign_long, 1.0, context.time)
            b = context.carry.annual_carry(pair, -foreign_long, 1.0, context.time)
            if a is not None and b is not None:
                long_foreign.append(a)
                short_foreign.append(b)
        average_range = recent_atr(bars, p.atr)
        if not long_foreign or average_range is None:
            return None
        avg_long, avg_short = sum(long_foreign) / len(long_foreign), sum(short_foreign) / len(short_foreign)
        foreign = LONG if avg_long > p.min_carry else SHORT if avg_short > p.min_carry else FLAT
        direction = foreign * (LONG if symbol.endswith("USD") else SHORT)
        if direction == FLAT or (position is not None and position.direction == direction):
            return Signal(direction)
        return Signal(direction, stop_distance=p.stop_atr * average_range,
                      reason=f"basket carry long {avg_long:+.2f}% / short {avg_short:+.2f}%")


@dataclass(frozen=True)
class Rsi2Params:
    rsi: int = 2
    lower: float = 10.0
    upper: float = 90.0
    trend: int = 200
    max_hold: int = 5
    atr: int = 20
    stop_atr: float = 2.0


class Rsi2MeanReversion(Strategy):
    """Connors-style RSI(2) pullback: buy short-term dips in an uptrend, sell rallies in a downtrend."""

    name = "rsi2"
    Params = Rsi2Params

    @property
    def lookback(self) -> int:
        return max(self.params.trend, 3 * self.params.atr + 1)

    def on_bar(self, symbol, bars, position, context):
        p = self.params
        closes = [b.close for b in bars]
        strength = rsi(closes[-50:], p.rsi)
        trend = sma(closes, p.trend)
        average_range = recent_atr(bars, p.atr)
        if strength is None or trend is None or average_range is None:
            return None
        close = closes[-1]
        if position is None:
            if strength < p.lower and close > trend:
                return Signal(LONG, stop_distance=p.stop_atr * average_range, reason=f"RSI {strength:.0f}")
            if strength > p.upper and close < trend:
                return Signal(SHORT, stop_distance=p.stop_atr * average_range, reason=f"RSI {strength:.0f}")
            return Signal(FLAT)
        held = sum(1 for b in bars if b.time >= position.entry_time)
        done = held >= p.max_hold or (strength > 50 if position.direction == LONG else strength < 50)
        return Signal(FLAT if done else position.direction)
