"""Bar storage (CSV), symbol specs (JSON) and bar transformations."""
from __future__ import annotations

import csv
import dataclasses
import json
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path

from trading.models import Bar, SymbolSpec, timeframe_delta

CSV_HEADER = ["time", "open", "high", "low", "close", "volume"]
SPECS_FILE = "symbols.json"


def bars_path(data_dir: str | Path, symbol: str, timeframe: str) -> Path:
    return Path(data_dir) / f"{symbol.upper()}_{timeframe}.csv"


def parse_time(text: str) -> datetime:
    text = text.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    value = datetime.fromisoformat(text)
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def format_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_bars(path: str | Path) -> list[Bar]:
    """Read bars from CSV.

    Accepts ``time,open,high,low,close[,volume]`` (what ``fetch`` writes) or a
    two-column ``date,close`` file such as a FRED download, which becomes
    bars with open=high=low=close. Missing values ("" or ".") are skipped.
    """
    with open(path, newline="") as f:
        reader = csv.reader(f)
        header = [h.strip().lower() for h in next(reader)]
        rows = [row for row in reader if row and any(cell.strip() for cell in row)]

    bars = []
    if {"time", "open", "high", "low", "close"} <= set(header):
        col = {name: header.index(name) for name in CSV_HEADER if name in header}
        for row in rows:
            volume = float(row[col["volume"]]) if "volume" in col and row[col["volume"]] else 0.0
            bars.append(
                Bar(
                    parse_time(row[col["time"]]),
                    float(row[col["open"]]),
                    float(row[col["high"]]),
                    float(row[col["low"]]),
                    float(row[col["close"]]),
                    volume,
                )
            )
    elif len(header) == 2:
        for when, value in rows:
            if value.strip() in ("", "."):
                continue
            close = float(value)
            bars.append(Bar(parse_time(when), close, close, close, close))
    else:
        raise ValueError(f"{path}: expected columns {','.join(CSV_HEADER)} or a two-column date,close file")

    bars.sort(key=lambda b: b.time)
    for a, b in zip(bars, bars[1:]):
        if a.time == b.time:
            raise ValueError(f"{path}: duplicate bar at {format_time(a.time)}")
    return bars


def write_bars(path: str | Path, bars: Iterable[Bar]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(CSV_HEADER)
        for b in bars:
            writer.writerow([format_time(b.time)] + [format(x, ".10g") for x in (b.open, b.high, b.low, b.close, b.volume)])


def load_bars(data_dir: str | Path, symbols: Sequence[str], timeframe: str) -> dict[str, list[Bar]]:
    out = {}
    for symbol in symbols:
        path = bars_path(data_dir, symbol, timeframe)
        if not path.exists():
            raise FileNotFoundError(
                f"{path} not found; download it with: python main.py fetch --symbols {symbol} --timeframe {timeframe}"
            )
        out[symbol.upper()] = read_bars(path)
    return out


def save_specs(path: str | Path, specs: Mapping[str, SymbolSpec]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = load_specs(path) if path.exists() else {}
    existing.update(specs)
    payload = {name: dataclasses.asdict(spec) for name, spec in sorted(existing.items())}
    path.write_text(json.dumps(payload, indent=2) + "\n")


def load_specs(path: str | Path) -> dict[str, SymbolSpec]:
    path = Path(path)
    if not path.exists():
        return {}
    raw = json.loads(path.read_text())
    return {name: SymbolSpec(**fields) for name, fields in raw.items()}


def merge_weekend_bars(bars: Sequence[Bar]) -> list[Bar]:
    """Fold weekend stub bars into the following weekday bar.

    Daily bars cut at UTC midnight include a stub for the Sunday-evening
    open; merging it keeps daily indicators consistent across data sources.
    A bar only counts as a stub if it closes before Monday midday UTC: brokers
    that cut days at the New York close label the full Monday session
    "Sunday 21:00", and that bar must stay as it is. A trailing stub is held
    back until the bar that absorbs it exists.
    """
    out: list[Bar] = []
    pending: Bar | None = None
    for bar in bars:
        if _is_weekend_stub(bar):
            pending = bar if pending is None else _join(pending, bar, pending.time)
            continue
        if pending is not None:
            bar = _join(pending, bar, bar.time)
            pending = None
        out.append(bar)
    return out


def resample(bars: Sequence[Bar], timeframe: str) -> list[Bar]:
    """Aggregate intraday bars into ``timeframe`` buckets aligned to UTC multiples of its length."""
    seconds = int(timeframe_delta(timeframe).total_seconds())
    if seconds >= 86400:
        raise ValueError("resample() is for intraday targets; daily bars have broker-specific session cuts")
    out: list[Bar] = []
    for bar in bars:
        bucket = datetime.fromtimestamp(int(bar.time.timestamp()) // seconds * seconds, timezone.utc)
        if out and out[-1].time == bucket:
            out[-1] = _join(out[-1], bar, bucket)
        else:
            out.append(Bar(bucket, bar.open, bar.high, bar.low, bar.close, bar.volume))
    return out


def _is_weekend_stub(bar: Bar) -> bool:
    if bar.time.weekday() < 5:
        return False
    closes = bar.time + timedelta(days=1)
    return closes.weekday() >= 5 or (closes.weekday() == 0 and closes.hour < 12)


def _join(first: Bar, second: Bar, time: datetime) -> Bar:
    return Bar(
        time,
        first.open,
        max(first.high, second.high),
        min(first.low, second.low),
        second.close,
        first.volume + second.volume,
    )
