from datetime import date, datetime, timezone

import pytest

from trading.sessions import SESSIONS, session_for

UTC = timezone.utc


def test_sessions_follow_each_exchanges_daylight_saving():
    us, eu = SESSIONS["us"], SESSIONS["eu"]
    assert us.bounds(date(2024, 1, 2)) == (datetime(2024, 1, 2, 14, 30, tzinfo=UTC), datetime(2024, 1, 2, 21, tzinfo=UTC))
    assert us.bounds(date(2024, 7, 1)) == (datetime(2024, 7, 1, 13, 30, tzinfo=UTC), datetime(2024, 7, 1, 20, tzinfo=UTC))
    # In March 2024 the US moved its clocks on the 10th, Europe on the 31st.
    assert us.bounds(date(2024, 3, 18))[0] == datetime(2024, 3, 18, 13, 30, tzinfo=UTC)
    assert eu.bounds(date(2024, 3, 18))[0] == datetime(2024, 3, 18, 8, tzinfo=UTC)
    assert eu.bounds(date(2024, 4, 2))[0] == datetime(2024, 4, 2, 7, tzinfo=UTC)
    assert us.minutes() == 390


def test_day_of_uses_local_time():
    assert SESSIONS["us"].day_of(datetime(2024, 1, 3, 2, 0, tzinfo=UTC)) == date(2024, 1, 2)
    assert SESSIONS["eu"].day_of(datetime(2024, 1, 2, 23, 30, tzinfo=UTC)) == date(2024, 1, 3)


def test_session_inferred_from_the_symbol():
    assert session_for("#USNDAQ100").name == "us"
    assert session_for("#USSPX500").name == "us"
    assert session_for("#GERMANY40").name == "eu"
    assert session_for("#UK100").name == "uk"
    assert session_for("EURUSD").name == "fx"
    assert session_for("XAUUSD").name == "fx"
    assert session_for("#JAPAN225", "us").name == "us"
    with pytest.raises(ValueError):
        session_for("#JAPAN225")
    with pytest.raises(ValueError):
        session_for("EURUSD", "tokyo")
