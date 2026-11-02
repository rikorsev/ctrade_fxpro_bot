"""Download the free historical data used by research/run_research.py.

* FRED H.10 daily noon buying rates (close-only), 1971 onwards. EURUSD before
  1999 is spliced from the Deutsche mark at the fixed 1.95583 DEM/EUR rate.
* FRED (OECD) monthly short-term interest rates, used to model swaps/carry.
* Yahoo Finance daily OHLC, December 2003 onwards.
* HistData.com one-minute bid bars (FX, gold and index CFDs), 2010 onwards,
  aggregated to five-minute bars for the intraday research. The zip files are
  cached in data/research/raw/histdata/.

Files land in data/research/ (gitignored). The data is for personal research;
check each provider's terms before redistributing it.

    python -m research.download
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import re
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trading.data import merge_weekend_bars, resample, write_bars  # noqa: E402
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


def get(url: str, tries: int = 6, *, html: bool = False, form: dict | None = None,
        headers: dict | None = None) -> bytes:
    data = urllib.parse.urlencode(form).encode() if form else None
    for attempt in range(tries):
        try:
            request = urllib.request.Request(url, data=data, headers={**UA, **(headers or {})})
            with urllib.request.urlopen(request, timeout=90) as r:
                body = r.read()
            if not html and body.lstrip()[:1] == b"<":
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


# HistData pair -> FxPro cTrader symbol name (upper-cased, as the broker adapter reports it).
HISTDATA = {
    "SPXUSD": "#USSPX500",
    "NSXUSD": "#USNDAQ100",
    "GRXEUR": "#GERMANY40",
    "UKXGBP": "#UK100",
    "XAUUSD": "XAUUSD",
    "EURUSD": "EURUSD",
    "GBPUSD": "GBPUSD",
    "USDJPY": "USDJPY",
}
HISTDATA_FIRST_YEAR = 2010
HISTDATA_PAGE = "https://www.histdata.com/download-free-forex-historical-data/?/ascii/1-minute-bar-quotes/{}"
HISTDATA_OFFSET = timedelta(hours=5)  # HistData stamps are EST all year round (no daylight saving)


def histdata_zip(pair: str, year: int, month: int | None = None) -> bytes | None:
    """One year (or, for the current year, one month) of HistData M1 bars as a zip, or None if not published."""
    period = f"{year}" if month is None else f"{year}{month:02d}"
    cache = OUT / "raw" / "histdata" / f"{pair}_{period}.zip"
    if cache.exists():
        return cache.read_bytes()
    page_url = HISTDATA_PAGE.format(f"{pair.lower()}/{year}" + ("" if month is None else f"/{month}"))
    page = get(page_url, html=True).decode(errors="replace")
    form = {}
    for name in ("tk", "date", "datemonth", "platform", "timeframe", "fxpair"):
        found = re.search(rf'name="{name}" id="{name}" value="([^"]*)"', page)
        form[name] = found.group(1) if found else ""
    if not form["tk"]:
        return None
    blob = get("https://www.histdata.com/get.php", form=form, headers={"Referer": page_url})
    if blob[:2] != b"PK":
        raise RuntimeError(f"HistData {pair} {period}: expected a zip file")
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_bytes(blob)
    time.sleep(1)
    return blob


def histdata_bars(blob: bytes) -> list[Bar]:
    """Parse a HistData ASCII M1 zip (``YYYYMMDD HHMMSS;open;high;low;close;volume``) into UTC bars."""
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        name = next(n for n in archive.namelist() if n.lower().endswith(".csv"))
        text = archive.read(name).decode()
    bars = []
    for line in text.splitlines():
        if not line.strip():
            continue
        stamp, o, h, low, c, *_ = line.split(";")
        est = datetime(int(stamp[0:4]), int(stamp[4:6]), int(stamp[6:8]), int(stamp[9:11]), int(stamp[11:13]),
                       tzinfo=timezone.utc)
        bars.append(Bar(est + HISTDATA_OFFSET, float(o), float(h), float(low), float(c)))
    return bars


def download_histdata(timeframe: str = "M5") -> None:
    today = datetime.now(timezone.utc).date()
    for pair, symbol in HISTDATA.items():
        by_time: dict[datetime, Bar] = {}
        for year in range(HISTDATA_FIRST_YEAR, today.year + 1):
            months = [None] if year < today.year else range(1, today.month + 1)
            for month in months:
                blob = histdata_zip(pair, year, month)
                if blob is None:
                    continue
                for bar in resample(histdata_bars(blob), timeframe):
                    by_time[bar.time] = bar
            print(f"HistData {pair} {year}: {len(by_time)} {timeframe} bars so far", flush=True)
        bars = [by_time[t] for t in sorted(by_time)]
        write_bars(OUT / "intraday" / f"{symbol}_{timeframe}.csv", bars)
        print(f"HistData {symbol}: {len(bars)} {timeframe} bars {bars[0].time:%Y-%m-%d} .. {bars[-1].time:%Y-%m-%d}",
              flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--only", choices=["fred", "rates", "yahoo", "histdata"], help="download one dataset")
    args = parser.parse_args()
    jobs = {"fred": download_fred_fx, "rates": download_rates, "yahoo": download_yahoo,
            "histdata": download_histdata}
    for name, job in jobs.items():
        if args.only in (None, name):
            job()


if __name__ == "__main__":
    main()
