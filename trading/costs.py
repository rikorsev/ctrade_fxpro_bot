"""Trading costs (spread, commission, swaps) and currency conversion."""
from __future__ import annotations

import bisect
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Protocol

from trading.models import LONG, SymbolSpec

# FxPro charges swaps at 21:59 UK time; 21:00 UTC is close enough for daily accounting.
ROLLOVER_HOUR_UTC = 21
ROLLOVERS_PER_YEAR = 365  # 5 charges a week, one of them triple

# Typical FxPro cTrader raw spreads in pips, padded upwards because spreads
# widen around rollover and news. Measure your own with `python main.py quotes`.
DEFAULT_SPREADS_PIPS = {
    "EURUSD": 0.5,
    "GBPUSD": 0.9,
    "USDJPY": 0.7,
    "AUDUSD": 0.7,
    "NZDUSD": 1.2,
    "USDCAD": 0.9,
    "USDCHF": 0.9,
    "EURJPY": 1.0,
    "EURGBP": 0.8,
    "EURCHF": 1.0,
    "AUDJPY": 1.2,
    "GBPJPY": 1.8,
}


@dataclass(frozen=True)
class CostModel:
    """Execution costs. The defaults approximate an FxPro cTrader account (see docs/RESEARCH.md)."""

    spread_pips: Mapping[str, float] = field(default_factory=lambda: dict(DEFAULT_SPREADS_PIPS))
    default_spread_pips: float = 1.5
    slippage_pips: float = 0.1
    commission_per_million: float = 35.0  # USD per 1M USD of notional, charged on open and on close

    def spread(self, spec: SymbolSpec) -> float:
        return self.spread_pips.get(spec.name, self.default_spread_pips) * spec.pip_size

    def slippage(self, spec: SymbolSpec) -> float:
        return self.slippage_pips * spec.pip_size

    def scaled(self, factor: float) -> CostModel:
        """The same model with spread, slippage and commission multiplied by ``factor``."""
        return CostModel(
            spread_pips={k: v * factor for k, v in self.spread_pips.items()},
            default_spread_pips=self.default_spread_pips * factor,
            slippage_pips=self.slippage_pips * factor,
            commission_per_million=self.commission_per_million * factor,
        )


def rollovers(start: datetime, end: datetime, triple_weekday: int = 2) -> int:
    """Number of daily swap charges in (start, end]. The triple-swap weekday counts three times."""
    if end <= start:
        return 0
    charge = datetime.combine(start.date(), time(ROLLOVER_HOUR_UTC), tzinfo=start.tzinfo)
    if charge <= start:
        charge += timedelta(days=1)
    count = 0
    while charge <= end:
        weekday = charge.weekday()
        if weekday < 5:
            count += 3 if weekday == triple_weekday else 1
        charge += timedelta(days=1)
    return count


class SwapModel(Protocol):
    def swap_per_unit(self, spec: SymbolSpec, direction: int, price: float, when: datetime) -> float | None:
        """Quote-currency amount credited (+) or charged (-) per unit of base for one rollover."""


class BrokerSwaps:
    """Swaps from the broker's symbol specs (``swapLong``/``swapShort`` in cTrader)."""

    def swap_per_unit(self, spec: SymbolSpec, direction: int, price: float, when: datetime) -> float | None:
        rate = spec.swap_long if direction == LONG else spec.swap_short
        if spec.swap_type == "pips":
            return rate * spec.pip_size
        if spec.swap_type == "points":
            return rate * spec.point_size
        if spec.swap_type == "percent":
            return rate / 100.0 * price / ROLLOVERS_PER_YEAR
        raise ValueError(f"{spec.name}: unknown swap type {spec.swap_type!r}")


class InterestRateSwaps:
    """Swaps implied by short-term interest rates minus a broker markup.

    Used to backtest carry over periods for which the broker's historical
    swap rates are not available. ``rates`` maps a currency to (date, % per
    year) observations; the latest observation on or before a date applies.
    """

    def __init__(
        self,
        rates: Mapping[str, Sequence[tuple[date, float]]],
        markup_pct: float = 1.0,
        day_count: int = 360,
    ) -> None:
        self.markup_pct = markup_pct
        self.day_count = day_count
        self._dates = {ccy: [d for d, _ in sorted(obs)] for ccy, obs in rates.items()}
        self._values = {ccy: [v for _, v in sorted(obs)] for ccy, obs in rates.items()}

    def rate(self, currency: str, when: datetime) -> float | None:
        dates = self._dates.get(currency)
        if not dates:
            return None
        i = bisect.bisect_right(dates, when.date()) - 1
        return self._values[currency][i] if i >= 0 else None

    def swap_per_unit(self, spec: SymbolSpec, direction: int, price: float, when: datetime) -> float | None:
        base, quote = self.rate(spec.base, when), self.rate(spec.quote, when)
        if base is None or quote is None:
            return None
        differential = base - quote if direction == LONG else quote - base
        return (differential - self.markup_pct) / 100.0 * price / self.day_count


class SwapCarry:
    """Carry source for strategies: the swap a position would earn per year, in % of notional."""

    def __init__(self, swaps: SwapModel, specs: Mapping[str, SymbolSpec]) -> None:
        self.swaps = swaps
        self.specs = specs

    def annual_carry(self, symbol: str, direction: int, price: float, time: datetime) -> float | None:
        spec = self.specs.get(symbol)
        if spec is None or price <= 0:
            return None
        per_unit = self.swaps.swap_per_unit(spec, direction, price, time)
        if per_unit is None:
            return None
        return per_unit * ROLLOVERS_PER_YEAR / price * 100.0


class CurrencyConverter:
    """Converts amounts between currencies using the latest price of a direct or USD-crossed pair."""

    def __init__(self, price_of: Callable[[str], float | None], symbols: Iterable[str]) -> None:
        self._price_of = price_of
        self._symbols = {s.upper() for s in symbols}

    def can_convert(self, from_ccy: str, to_ccy: str) -> bool:
        def direct(a: str, b: str) -> bool:
            return a == b or a + b in self._symbols or b + a in self._symbols

        return direct(from_ccy, to_ccy) or (direct(from_ccy, "USD") and direct("USD", to_ccy))

    def rate(self, from_ccy: str, to_ccy: str) -> float:
        """Multiply an amount in ``from_ccy`` by this to get ``to_ccy``."""
        direct = self._direct(from_ccy, to_ccy)
        if direct is not None:
            return direct
        via_from, via_to = self._direct(from_ccy, "USD"), self._direct("USD", to_ccy)
        if via_from is not None and via_to is not None:
            return via_from * via_to
        raise LookupError(
            f"No price to convert {from_ccy} to {to_ccy}: load a {from_ccy}{to_ccy}, "
            f"{to_ccy}{from_ccy} or USD pair for each currency"
        )

    def _direct(self, a: str, b: str) -> float | None:
        if a == b:
            return 1.0
        if a + b in self._symbols:
            price = self._price_of(a + b)
            if price:
                return price
        if b + a in self._symbols:
            price = self._price_of(b + a)
            if price:
                return 1.0 / price
        return None
