from datetime import date, datetime, timedelta, timezone

import pytest

from trading.costs import BrokerSwaps, CostModel, CurrencyConverter, InterestRateSwaps, SwapCarry, rollovers
from trading.models import LONG, SHORT, SymbolSpec

MON = datetime(2024, 1, 1, tzinfo=timezone.utc)
DAY = timedelta(days=1)


def test_rollovers_count_triple_wednesday_and_skip_weekends():
    assert rollovers(MON, MON + DAY) == 1  # Monday 21:00
    assert rollovers(MON + 2 * DAY, MON + 3 * DAY) == 3  # Wednesday
    assert rollovers(MON + 4 * DAY, MON + 7 * DAY) == 1  # Friday only
    assert rollovers(MON + 5 * DAY, MON + 6 * DAY) == 0  # Saturday
    assert rollovers(MON, MON + 7 * DAY) == 7
    assert rollovers(MON + DAY, MON) == 0
    # boundaries: a charge exactly at `start` is excluded, at `end` included
    charge = MON.replace(hour=21)
    assert rollovers(charge, charge + timedelta(hours=1)) == 0
    assert rollovers(charge - timedelta(hours=1), charge) == 1


def test_broker_swaps_by_type():
    swaps = BrokerSwaps()
    pips = SymbolSpec("EURUSD", "EUR", "USD", swap_long=-6.5, swap_short=2.0)
    assert swaps.swap_per_unit(pips, LONG, 1.1, MON) == pytest.approx(-0.00065)
    assert swaps.swap_per_unit(pips, SHORT, 1.1, MON) == pytest.approx(0.0002)
    points = SymbolSpec("USDJPY", "USD", "JPY", digits=3, pip_position=2, swap_long=150, swap_type="points")
    assert swaps.swap_per_unit(points, LONG, 150.0, MON) == pytest.approx(0.15)
    pct = SymbolSpec("XAUUSD", "XAU", "USD", digits=2, pip_position=1, swap_long=-3.65, swap_type="percent")
    assert swaps.swap_per_unit(pct, LONG, 2000.0, MON) == pytest.approx(-0.2)


def test_interest_rate_swaps_and_carry():
    rates = {"AUD": [(date(2020, 1, 1), 1.0), (date(2023, 1, 1), 4.0)], "USD": [(date(2020, 1, 1), 2.0)]}
    swaps = InterestRateSwaps(rates, markup_pct=0.5, day_count=360)
    aud = SymbolSpec.infer("AUDUSD")
    when = datetime(2024, 6, 1, tzinfo=timezone.utc)
    assert swaps.swap_per_unit(aud, LONG, 0.66, when) == pytest.approx((4 - 2 - 0.5) / 100 * 0.66 / 360)
    assert swaps.swap_per_unit(aud, SHORT, 0.66, when) == pytest.approx((2 - 4 - 0.5) / 100 * 0.66 / 360)
    assert swaps.swap_per_unit(aud, LONG, 0.66, datetime(2019, 1, 1, tzinfo=timezone.utc)) is None
    carry = SwapCarry(swaps, {"AUDUSD": aud})
    assert carry.annual_carry("AUDUSD", LONG, 0.66, when) == pytest.approx(1.5 * 365 / 360)
    assert carry.annual_carry("NZDUSD", LONG, 0.6, when) is None


def test_currency_converter():
    prices = {"EURUSD": 1.10, "USDJPY": 150.0, "GBPUSD": 1.25}
    conv = CurrencyConverter(prices.get, prices)
    assert conv.rate("USD", "USD") == 1.0
    assert conv.rate("EUR", "USD") == pytest.approx(1.10)
    assert conv.rate("JPY", "USD") == pytest.approx(1 / 150)
    assert conv.rate("GBP", "JPY") == pytest.approx(1.25 * 150)  # via USD
    assert conv.can_convert("EUR", "JPY") and not conv.can_convert("CHF", "USD")
    with pytest.raises(LookupError):
        conv.rate("CHF", "USD")


def test_cost_model():
    costs = CostModel(spread_pips={"EURUSD": 0.4}, default_spread_pips=2.0, slippage_pips=0.1)
    assert costs.spread(SymbolSpec.infer("EURUSD")) == pytest.approx(0.00004)
    assert costs.spread(SymbolSpec.infer("USDJPY")) == pytest.approx(0.02)
    double = costs.scaled(2)
    assert double.spread(SymbolSpec.infer("EURUSD")) == pytest.approx(0.00008)
    assert double.commission_per_million == 70.0
