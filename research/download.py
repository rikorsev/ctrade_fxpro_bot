"""Download the free historical data used by research/run_research.py.

* FRED H.10 daily noon buying rates (close-only), 1971 onwards. EURUSD before
  1999 is spliced from the Deutsche mark at the fixed 1.95583 DEM/EUR rate.
* FRED (OECD) monthly short-term interest rates, used to model swaps/carry.
* Yahoo Finance daily OHLC, December 2003 onwards.

Files land in data/research/ (gitignored). The data is for personal research;
check each provider's terms before redistributing it.

    python -m research.download
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import statistics
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trading.data import merge_weekend_bars, write_bars  # noqa: E402
from trading.models import Bar  # noqa: E402

OUT = Path("data/research")
UA = {"User-Agent": "Mozilla/5.0 (research download)"}

# FRED series already quoted the way the pair is named (USD per EUR = EURUSD, JPY per USD = USDJPY).
FRED_FX = {
    "EURUSD": "DEXUSEU",
    "GBPUSD": "DEXUSUK",
    "AUDUSD": "DEXUSAL",
    "NZDUSD": "DEXUSNZ",
    "USDJPY": "DEXJPUS",
    "USDCHF": "DEXSZUS",
    "USDCAD": "DEXCAUS",
    "USDNOK": "DEXNOUS",
    "USDSEK": "DEXSDUS",
}
DEM_PER_EUR = 1.95583

# Overnight (call money) rates where available, 3-month interbank rates to fill gaps.
RATE_COUNTRY = {"USD": "US", "EUR": "EZ", "GBP": "GB", "JPY": "JP", "AUD": "AU",
                "NZD": "NZ", "CAD": "CA", "CHF": "CH", "NOK": "NO", "SEK": "SE"}

YAHOO = {
    "EURUSD": "EURUSD=X",
    "GBPUSD": "GBPUSD=X",
    "AUDUSD": "AUDUSD=X",
    "NZDUSD": "NZDUSD=X",
    "USDJPY": "JPY=X",
    "USDCHF": "CHF=X",
    "USDCAD": "CAD=X",
    "EURJPY": "EURJPY=X",
    "EURGBP": "EURGBP=X",
    "AUDJPY": "AUDJPY=X",
}


def get(url: str, tries: int = 6) -> bytes:
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=90) as r:
                body = r.read()
            if body.lstrip()[:1] == b"<":
                raise ValueError("got an HTML page instead of data")
            return body
        except (urllib.error.URLError, ValueError, TimeoutError) as exc:
            wait = 5 * (attempt + 1)
            print(f"  retry in {wait}s ({exc})", flush=True)
            time.sleep(wait)
    raise RuntimeError(f"giving up on {url}")


def fred(series: str, tries: int = 6) -> list[tuple[date, float]]:
    """A FRED series, cached in data/research/raw/ (FRED throttles repeated bulk downloads)."""
    cache = OUT / "raw" / f"{series}.csv"
    if cache.exists():
        text = cache.read_text()
    else:
        text = get(f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}", tries).decode()
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(text)
    rows = list(csv.reader(io.StringIO(text)))[1:]
    return [(date.fromisoformat(d), float(v)) for d, v in rows if v not in ("", ".")]


def close_bars(points: list[tuple[date, float]]) -> list[Bar]:
    return [Bar(datetime(d.year, d.month, d.day, tzinfo=timezone.utc), v, v, v, v) for d, v in points]


def download_fred_fx() -> None:
    for symbol, series in FRED_FX.items():
        points = fred(series)
        if symbol == "EURUSD":
            try:
                dem = [(d, DEM_PER_EUR / v) for d, v in fred("DEXGEUS", tries=2) if d < date(1999, 1, 4)]
                points = dem + points
            except RuntimeError:
                print("  DEM series unavailable; EURUSD starts in 1999")
        write_bars(OUT / "fred" / f"{symbol}_D1.csv", close_bars(points))
        print(f"FRED {symbol}: {len(points)} days from {points[0][0]}", flush=True)


def download_rates() -> None:
    rates = {}
    for currency, country in RATE_COUNTRY.items():
        overnight = dict(fred(f"IRSTCI01{country}M156N"))
        three_month = dict(fred(f"IR3TIB01{country}M156N"))
        merged = {**three_month, **overnight}
        rates[currency] = sorted(merged.items())
        first, last = rates[currency][0][0], rates[currency][-1][0]
        print(f"rates {currency}: {len(merged)} months {first} .. {last}", flush=True)
    path = OUT / "rates.json"
    path.write_text(json.dumps({c: [(d.isoformat(), v) for d, v in obs] for c, obs in rates.items()}, indent=0))


def yahoo_daily(ticker: str, today: date) -> list[Bar]:
    start = int(datetime(2003, 12, 1, tzinfo=timezone.utc).timestamp())
    end = int(datetime.now(timezone.utc).timestamp())
    raw = json.loads(get(f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
                         f"?period1={start}&period2={end}&interval=1d"))
    result = raw["chart"]["result"][0]
    quote = result["indicators"]["quote"][0]
    london = ZoneInfo("Europe/London")
    by_day: dict[date, Bar] = {}
    for i, ts in enumerate(result["timestamp"]):
        o, h, low, c = (quote[k][i] for k in ("open", "high", "low", "close"))
        if None in (o, h, low, c) or min(o, h, low, c) <= 0:
            continue
        day = datetime.fromtimestamp(ts, london).date()  # Yahoo FX days start at London midnight
        if day >= today:
            continue  # today's bar is still forming
        by_day[day] = Bar(datetime(day.year, day.month, day.day, tzinfo=timezone.utc), o, h, low, c)
    return clean(merge_weekend_bars([by_day[d] for d in sorted(by_day)]))


def clean(bars: list[Bar]) -> list[Bar]:
    """Repair off-market ticks, a known problem in free FX data.

    Uses the neighbouring prices (previous close and next open): when they
    agree with each other, a bar lying entirely 3%+ away from them, or a
    single price far outside the usual daily range, is a bad tick. Genuine
    shocks (SNB 2015, the October 2008 yen crash) either persist into the next
    bar or trade through the prior level, so they are kept as they are.
    """
    out = list(bars)
    for i in range(1, len(bars) - 1):
        prev_close, next_open = out[i - 1].close, bars[i + 1].open
        ref = (prev_close + next_open) / 2
        gap = abs(prev_close - next_open) / ref
        b = bars[i]
        if gap < 0.03 and min(abs(x - ref) for x in (b.open, b.high, b.low, b.close)) / ref > 0.03:
            out[i] = Bar(b.time, prev_close, max(prev_close, next_open), min(prev_close, next_open), next_open, b.volume)
            continue
        if gap > 0.02:
            continue
        window = out[max(0, i - 20):i]
        limit = max(8 * statistics.median((x.high - x.low) / x.close for x in window), 0.04)
        bad = {name: abs(getattr(b, name) - ref) / ref > limit for name in ("open", "high", "low", "close")}
        o = prev_close if bad["open"] else b.open
        c = next_open if bad["close"] else b.close
        hi = max(o, c) if bad["high"] else max(b.high, o, c)
        lo = min(o, c) if bad["low"] else min(b.low, o, c)
        out[i] = Bar(b.time, o, hi, lo, c, b.volume)
    return out


def download_yahoo() -> None:
    today = datetime.now(timezone.utc).date()
    for symbol, ticker in YAHOO.items():
        bars = yahoo_daily(ticker, today)
        write_bars(OUT / "yahoo" / f"{symbol}_D1.csv", bars)
        print(f"Yahoo {symbol}: {len(bars)} bars {bars[0].time:%Y-%m-%d} .. {bars[-1].time:%Y-%m-%d}", flush=True)
        time.sleep(1)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--only", choices=["fred", "rates", "yahoo"], help="download one dataset")
    args = parser.parse_args()
    jobs = {"fred": download_fred_fx, "rates": download_rates, "yahoo": download_yahoo}
    for name, job in jobs.items():
        if args.only in (None, name):
            job()


if __name__ == "__main__":
    main()
