from datetime import datetime, timedelta, timezone

import pytest

from tests.helpers import daily_bars
from trading.data import load_specs, merge_weekend_bars, read_bars, resample, save_specs, write_bars
from trading.models import Bar, SymbolSpec

UTC = timezone.utc


def test_csv_round_trip(tmp_path):
    bars = daily_bars([1.1, 1.2, 1.15])
    path = tmp_path / "EURUSD_D1.csv"
    write_bars(path, bars)
    assert read_bars(path) == bars


def test_two_column_close_files_and_missing_values(tmp_path):
    path = tmp_path / "fred.csv"
    path.write_text("observation_date,DEXUSEU\n2024-01-02,1.09\n2024-01-03,.\n2024-01-04,1.10\n")
    bars = read_bars(path)
    assert [b.close for b in bars] == [1.09, 1.10]
    assert bars[0].open == bars[0].high == bars[0].low == 1.09
    assert bars[0].time == datetime(2024, 1, 2, tzinfo=UTC)


def test_duplicates_are_rejected(tmp_path):
    path = tmp_path / "dup.csv"
    path.write_text("time,open,high,low,close\n2024-01-02,1,1,1,1\n2024-01-02,1,1,1,1\n")
    with pytest.raises(ValueError, match="duplicate"):
        read_bars(path)


def test_weekend_stub_merges_into_monday():
    fri = Bar(datetime(2024, 1, 5, tzinfo=UTC), 1.0, 1.1, 0.9, 1.05, 10)
    sun = Bar(datetime(2024, 1, 7, tzinfo=UTC), 1.06, 1.20, 1.04, 1.07, 1)
    mon = Bar(datetime(2024, 1, 8, tzinfo=UTC), 1.07, 1.08, 1.00, 1.02, 20)
    merged = merge_weekend_bars([fri, sun, mon])
    assert merged == [fri, Bar(mon.time, 1.06, 1.20, 1.00, 1.02, 21)]
    assert merge_weekend_bars([fri, sun]) == [fri]  # Monday not closed yet


def test_new_york_close_daily_bars_are_not_merged():
    # Brokers cutting days at 21:00 UTC label the whole Monday session "Sunday 21:00".
    thu = Bar(datetime(2024, 1, 4, 21, tzinfo=UTC), 1.0, 1.1, 0.9, 1.05)
    sun = Bar(datetime(2024, 1, 7, 21, tzinfo=UTC), 1.05, 1.2, 1.0, 1.1)
    mon = Bar(datetime(2024, 1, 8, 21, tzinfo=UTC), 1.1, 1.15, 1.05, 1.12)
    assert merge_weekend_bars([thu, sun, mon]) == [thu, sun, mon]


def test_resample_hours_to_h4():
    start = datetime(2024, 1, 2, 0, tzinfo=UTC)
    hourly = [Bar(start + timedelta(hours=h), 1 + h, 1.5 + h, 0.5 + h, 1.2 + h, 1) for h in range(6)]
    h4 = resample(hourly, "H4")
    assert [b.time.hour for b in h4] == [0, 4]
    assert h4[0] == Bar(start, 1, 4.5, 0.5, 4.2, 4)
    with pytest.raises(ValueError):
        resample(hourly, "D1")


def test_specs_round_trip_and_merge(tmp_path):
    path = tmp_path / "symbols.json"
    save_specs(path, {"EURUSD": SymbolSpec.infer("EURUSD")})
    save_specs(path, {"USDJPY": SymbolSpec.infer("USDJPY")})
    specs = load_specs(path)
    assert set(specs) == {"EURUSD", "USDJPY"} and specs["USDJPY"].pip_size == 0.01
