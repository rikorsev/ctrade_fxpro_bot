from datetime import datetime, timedelta, timezone

import pytest

from trading.models import LONG, SHORT, Position, SymbolSpec
from trading.risk import Exposure, RiskConfig, RiskManager, position_exposure

EURUSD = SymbolSpec.infer("EURUSD")
GBPUSD = SymbolSpec.infer("GBPUSD")
USDJPY = SymbolSpec.infer("USDJPY")
NOW = datetime(2025, 3, 3, 12, tzinfo=timezone.utc)


def size(manager, spec=EURUSD, direction=LONG, price=1.10, stop=0.01, rate=1.0, exposures=(), equity=100_000):
    return manager.size(
        equity=equity,
        spec=spec,
        direction=direction,
        price=price,
        stop_distance=stop,
        quote_to_account=rate,
        exposures=list(exposures),
    )


def test_risk_per_trade_sizing_in_quote_and_converted_currency():
    rm = RiskManager()
    d = size(rm)  # 0.5% of 100k = 500 at a 100-pip stop
    assert d.units == 50_000 and d.risk == pytest.approx(500) and d.notional == pytest.approx(55_000)
    jpy = size(rm, spec=USDJPY, price=150.0, stop=1.5, rate=1 / 150)
    assert jpy.units == 50_000 and jpy.risk == pytest.approx(500)


def test_open_risk_cap_shrinks_the_position():
    used = [Exposure("AUD", "JPY", SHORT, 2_800, 50_000)]  # shares no currency with EURUSD
    d = size(RiskManager(), exposures=used)
    assert d.units == 20_000 and "open risk cap" in d.reason


def test_currency_concentration_cap():
    # Long EURUSD already puts 1,400 at risk on "short USD"; long GBPUSD would add more.
    used = [Exposure("EUR", "USD", LONG, 1_400, 50_000)]
    d = size(RiskManager(), spec=GBPUSD, price=1.25, exposures=used)
    assert d.units == 10_000 and "short USD" in d.reason
    # Short GBPUSD is long USD, so the short-USD bucket does not limit it.
    assert size(RiskManager(), spec=GBPUSD, direction=SHORT, price=1.25, exposures=used).units == 50_000


def test_leverage_cap_and_broker_minimum():
    used = [Exposure("AUD", "USD", LONG, 0, 990_000)]
    d = size(RiskManager(), exposures=used)
    assert d.units == 9_000 and "leverage" in d.reason
    tiny = size(RiskManager(), equity=1_000, stop=0.05)  # budget 5 -> 100 units < 1,000 minimum
    assert tiny.units == 0 and "minimum" in tiny.reason


def test_max_positions():
    rm = RiskManager(RiskConfig(max_positions=1))
    assert size(rm, exposures=[Exposure("AUD", "USD", LONG, 0, 0)]).units == 0


def test_daily_loss_halt_resets_next_day_but_drawdown_halt_is_sticky():
    rm = RiskManager(RiskConfig(max_daily_loss=0.03, max_drawdown=0.10))
    assert rm.update(100_000, NOW) is None
    assert "daily loss" in rm.update(96_900, NOW + timedelta(hours=1))
    assert rm.update(99_000, NOW + timedelta(hours=2)) is not None  # stays halted for the day
    assert size(rm).units == 0
    assert rm.update(99_000, NOW + timedelta(days=1)) is None
    assert "drawdown" in rm.update(89_000, NOW + timedelta(days=2))
    assert rm.update(95_000, NOW + timedelta(days=5)) is not None
    rm.reset_drawdown(95_000)
    assert rm.update(95_000, NOW + timedelta(days=6)) is None


def test_state_round_trip():
    rm = RiskManager()
    rm.update(100_000, NOW)
    rm.update(70_000, NOW)
    restored = RiskManager()
    restored.restore(rm.state())
    assert restored.peak_equity == 100_000 and restored.drawdown_halt and restored.halt_reason


def test_position_exposure_uses_current_stop():
    p = Position("EURUSD", "s", LONG, 10_000, 1.1000, NOW, stop_loss=1.0900)
    e = position_exposure(p, EURUSD, 1.1050, 1.0, risk_without_stop=999)
    assert e.risk == pytest.approx(100) and e.notional == pytest.approx(11_050)
    p.stop_loss = 1.1010  # stop moved past entry: no open risk left
    assert position_exposure(p, EURUSD, 1.1050, 1.0, 999).risk == 0
    p.stop_loss = None
    assert position_exposure(p, EURUSD, 1.1050, 1.0, 999).risk == 999


def test_config_validation():
    with pytest.raises(ValueError):
        RiskConfig(risk_per_trade=0.05, max_open_risk=0.03)
    with pytest.raises(ValueError):
        RiskConfig(max_drawdown=1.5)
