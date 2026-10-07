"""Experiments behind docs/RESEARCH.md.

    python -m research.download          # once; writes data/research/
    python -m research.run_research      # writes data/research/results.md

The strategy defaults shipped in trading/ were fixed from the literature
before any of this was run. Experiments that vary them say so, and the
parameter grids report a Deflated Sharpe Ratio to account for the search.
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
import time
from collections import defaultdict
from collections.abc import Callable, Sequence
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from research.strategies import DollarCarry, RandomEntry, Rsi2MeanReversion  # noqa: E402
from trading.backtest import Backtester, BacktestResult  # noqa: E402
from trading.costs import DEFAULT_SPREADS_PIPS, CostModel, InterestRateSwaps  # noqa: E402
from trading.data import read_bars  # noqa: E402
from trading.metrics import (  # noqa: E402
    daily_equity,
    deflated_sharpe,
    max_drawdown,
    per_period_sharpe,
    returns,
    sharpe,
    summarize,
)
from trading.models import Bar  # noqa: E402
from trading.risk import RiskConfig  # noqa: E402
from trading.strategies import Strategy, create_strategy  # noqa: E402

DATA = Path("data/research")
YAHOO = ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "NZDUSD", "USDCAD", "USDCHF", "EURJPY", "EURGBP", "AUDJPY"]
FRED = ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "NZDUSD", "USDCAD", "USDCHF", "USDNOK", "USDSEK"]
COSTS = CostModel(spread_pips={**DEFAULT_SPREADS_PIPS, "USDNOK": 30.0, "USDSEK": 30.0})
START = datetime(2005, 1, 1, tzinfo=timezone.utc)  # Yahoo data starts Dec 2003: one year of warm-up
SUBPERIODS = [(2005, 2012), (2013, 2019), (2020, 2026)]
DECADES = [(1975, 1984), (1985, 1994), (1995, 2004), (2005, 2014), (2015, 2026)]


def utc(year: int, month: int = 1, day: int = 1) -> datetime:
    return datetime(year, month, day, tzinfo=timezone.utc)


def load(folder: str, symbols: Sequence[str]) -> dict[str, list[Bar]]:
    out = {}
    for s in symbols:
        path = DATA / folder / f"{s}_D1.csv"
        if path.exists():
            out[s] = read_bars(path)
    if not out:
        raise SystemExit(f"no data in {DATA / folder}; run: python -m research.download")
    return out


def load_swaps(markup: float) -> InterestRateSwaps:
    """Monthly average rates are only known when the month ends, so each applies from the next month."""
    raw = json.loads((DATA / "rates.json").read_text())

    def next_month(d: date) -> date:
        return date(d.year + d.month // 12, d.month % 12 + 1, 1)

    rates = {ccy: [(next_month(date.fromisoformat(d)), v) for d, v in obs] for ccy, obs in raw.items()}
    return InterestRateSwaps(rates, markup_pct=markup)


SWAPS = {}


def swaps(markup: float = 1.0) -> InterestRateSwaps:
    if markup not in SWAPS:
        SWAPS[markup] = load_swaps(markup)
    return SWAPS[markup]


def risk_for(n_strategies: int) -> RiskConfig:
    """Same risk per trade; portfolio caps scale with the number of strategies sharing the account.

    The drawdown kill switch is disabled: live, it blocks entries until a human
    resets it, which in a backtest would simply end the test. Section 1 reports
    when it would have fired instead.
    """
    base = RiskConfig()
    return RiskConfig(
        risk_per_trade=base.risk_per_trade,
        max_open_risk=base.max_open_risk * n_strategies,
        max_currency_risk=base.max_currency_risk * n_strategies,
        max_positions=base.max_positions * n_strategies,
        max_drawdown=0.99,
    )


def first_drawdown(result: BacktestResult, threshold: float = RiskConfig().max_drawdown) -> str:
    peak = result.initial_equity
    for t, value in daily_equity(result.equity_curve):
        peak = max(peak, value)
        if value <= peak * (1 - threshold):
            return f"{t:%Y-%m}"
    return "never"


def backtest(strategies: Sequence[Strategy], bars, *, costs=COSTS, markup=1.0, start=START, end=None,
             symbols=None) -> BacktestResult:
    return Backtester(
        strategies, bars, timeframe="D1", symbols=symbols, costs=costs, swaps=swaps(markup),
        risk=risk_for(len(strategies)), start=start, end=end,
    ).run()


def daily_returns(result: BacktestResult, start: datetime | None = None, end: datetime | None = None):
    points = [(t, v) for t, v in daily_equity(result.equity_curve)
              if (start is None or t >= start) and (end is None or t < end)]
    return points, returns([v for _, v in points])


def window_stats(result: BacktestResult, start: datetime, end: datetime) -> tuple[float, float, float]:
    points, rets = daily_returns(result, start, end)
    if len(points) < 20:
        return math.nan, math.nan, math.nan
    years = (points[-1][0] - points[0][0]).days / 365.25
    cagr = (points[-1][1] / points[0][1]) ** (1 / years) - 1
    return cagr, sharpe(rets), max_drawdown([v for _, v in points])


def pct(x: float, digits: int = 1) -> str:
    return "n/a" if x is None or math.isnan(x) else f"{x:.{digits}%}"


def num(x: float, digits: int = 2) -> str:
    return "n/a" if x is None or math.isnan(x) or math.isinf(x) else f"{x:.{digits}f}"


def table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    lines += ["| " + " | ".join(str(c) for c in row) + " |" for row in rows]
    return "\n".join(lines)


class Report:
    def __init__(self) -> None:
        self.parts: list[str] = []

    def add(self, text: str) -> None:
        print(text + "\n", flush=True)
        self.parts.append(text)

    def save(self, path: Path) -> None:
        path.write_text("\n\n".join(self.parts) + "\n")


def summary_row(name: str, result: BacktestResult) -> list[str]:
    s = summarize(result)
    return [name, pct(s.cagr), pct(s.volatility), num(s.sharpe), pct(s.max_drawdown), num(s.calmar),
            str(s.trades), pct(s.win_rate, 0), num(s.profit_factor), num(s.avg_r), f"{s.swap:+,.0f}",
            f"{s.commission:,.0f}", pct(s.psr, 0), first_drawdown(result)]


SUMMARY_HEADERS = ["Strategy", "CAGR", "Vol", "Sharpe", "Max DD", "Calmar", "Trades", "Win", "PF", "Avg R",
                   "Swap", "Commission", "P(SR>0)", "20% DD halt"]

COMBOS: dict[str, Callable[[], list[Strategy]]] = {
    "donchian": lambda: [create_strategy("donchian")],
    "tsmom": lambda: [create_strategy("tsmom")],
    "carry": lambda: [create_strategy("carry")],
    "donchian+carry": lambda: [create_strategy("donchian"), create_strategy("carry")],
    "donchian+tsmom+carry": lambda: [create_strategy(n) for n in ("donchian", "tsmom", "carry")],
}


def main_results(report: Report, yahoo) -> dict[str, BacktestResult]:
    results = {name: backtest(make(), yahoo) for name, make in COMBOS.items()}
    results["rsi2 (mean reversion)"] = backtest([Rsi2MeanReversion()], yahoo)
    report.add("## 1. Main results: Yahoo daily OHLC, 10 pairs, 2005-01 to 2026-10\n\n"
               "Default parameters, 0.5% risk per trade, FxPro-like costs, 1% p.a. swap markup, USD 100k account.\n\n"
               + table(SUMMARY_HEADERS, [summary_row(n, r) for n, r in results.items()]))

    rows = []
    for name, result in results.items():
        row = [name]
        for a, b in SUBPERIODS:
            cagr, sr, mdd = window_stats(result, utc(a), utc(b + 1))
            row += [pct(cagr), num(sr)]
        rows.append(row)
    headers = ["Strategy"] + [h for a, b in SUBPERIODS for h in (f"CAGR {a}-{b}", f"Sharpe {a}-{b}")]
    report.add("## 2. Sub-periods (same runs)\n\n" + table(headers, rows))

    names = ["donchian", "tsmom", "carry", "rsi2 (mean reversion)"]
    series = {}
    for n in names:
        points, rets = daily_returns(results[n])
        series[n] = dict(zip([t.date() for t, _ in points[1:]], rets))
    common = sorted(set.intersection(*(set(s) for s in series.values())))
    rows = []
    for a in names:
        rows.append([a] + [num(statistics.correlation([series[a][d] for d in common], [series[b][d] for d in common]))
                           for b in names])
    report.add("## 3. Correlation of daily returns\n\n" + table(["", *names], rows))

    rows = []
    for name in ("donchian", "tsmom", "carry"):
        by_symbol = defaultdict(float)
        for t in results[name].trades:
            by_symbol[t.symbol] += t.pnl
        rows.append([name] + [f"{by_symbol[s]:+,.0f}" for s in YAHOO])
    report.add("## 4. Net P&L by pair (USD, whole period)\n\n" + table(["Strategy", *YAHOO], rows))
    return results


def long_history(report: Report) -> None:
    fred = load("fred", FRED)
    rows = []
    runs = {name: (lambda n=name: [create_strategy(n)]) for name in ("donchian", "tsmom", "carry")}
    runs["dollar carry"] = lambda: [DollarCarry()]
    for name, make in runs.items():
        result = backtest(make(), fred, start=utc(1973))
        row = [name]
        for a, b in DECADES:
            cagr, sr, _ = window_stats(result, utc(a), utc(b + 1))
            row += [num(sr)]
        s = summarize(result)
        rows.append(row + [num(s.sharpe), pct(s.cagr), pct(s.max_drawdown)])
    headers = ["Strategy"] + [f"{a}-{b}" for a, b in DECADES] + ["Full Sharpe", "CAGR", "Max DD"]
    first = min(b[0].time for b in fred.values())
    report.add(f"## 5. Long history: FRED daily closes, {len(fred)} USD pairs, {first:%Y} to 2026 (Sharpe by decade)\n\n"
               "Close-only data: entries fill at the next day's close and stops are checked at closes. "
               "Today's cost assumptions are applied throughout, which flatters the early decades.\n\n"
               + table(headers, rows))


def textbook_replication(report: Report) -> None:
    """Moskowitz/Ooi/Pedersen-style monthly 12-month TSMOM computed directly from FRED month-end closes.

    Gross of costs and independent of the backtester, so it checks both the
    data and the engine: the engine's results should show the same pattern.
    """
    fred = load("fred", FRED)
    month_end = {s: {(b.time.year, b.time.month): b.close for b in bars} for s, bars in fred.items()}
    months = sorted(set().union(*month_end.values()))
    portfolio: dict[tuple[int, int], float] = {}
    for i in range(13, len(months) - 1):
        legs = []
        for closes in month_end.values():
            window = [closes.get(m) for m in months[i - 12:i + 2]]
            if any(c is None for c in window):
                continue
            history = [b / a - 1 for a, b in zip(window[:-1], window[1:-1])]
            vol = statistics.stdev(history) * math.sqrt(12)
            if vol > 0:
                signal = math.copysign(1.0, window[-2] / window[0] - 1)
                legs.append(signal * (window[-1] / window[-2] - 1) * 0.10 / vol)
        if legs:
            portfolio[months[i]] = statistics.fmean(legs)
    rows = []
    for a, b in [(1972, 1979), (1980, 1989), (1990, 1999), (2000, 2009), (2010, 2019), (2020, 2026), (1972, 2026)]:
        rets = [r for (y, _), r in portfolio.items() if a <= y <= b]
        sr = statistics.fmean(rets) / statistics.stdev(rets) * math.sqrt(12)
        rows.append([f"{a}-{b}", num(sr), pct(statistics.fmean(rets) * 12), str(len(rets))])
    report.add("## 0. Independent replication: textbook 12-month TSMOM on FRED month-end closes\n\n"
               "Each month, go long (short) every USD pair whose 12-month return is positive (negative), each leg "
               "scaled to 10% volatility; gross of costs and carry; computed without the backtester.\n\n"
               + table(["Period", "Sharpe", "Mean return / yr", "Months"], rows))


def random_control(report: Report, yahoo, main: dict[str, BacktestResult], seeds: int) -> None:
    donchian = main["donchian"]
    target = len(donchian.trades)
    probe = backtest([RandomEntry(seed=0)], yahoo)
    probability = 0.04 * target / max(len(probe.trades), 1)
    sharpes, cagrs = [], []
    for seed in range(seeds):
        result = backtest([RandomEntry(seed=seed, probability=probability)], yahoo)
        s = summarize(result)
        sharpes.append(s.sharpe)
        cagrs.append(s.cagr)
    actual = summarize(donchian).sharpe
    beaten = sum(1 for x in sharpes if x >= actual)
    report.add(
        "## 6. Random-entry control (same exits, stops and sizing as donchian)\n\n"
        + table(["", "Value"], [
            ["Donchian Sharpe", num(actual)],
            [f"Random entries: mean Sharpe over {seeds} seeds", num(statistics.fmean(sharpes))],
            ["Random entries: st. dev. of Sharpe", num(statistics.stdev(sharpes))],
            ["Random entries: best Sharpe", num(max(sharpes))],
            ["Random entries: mean CAGR", pct(statistics.fmean(cagrs))],
            ["Share of random runs >= Donchian", f"{beaten}/{seeds}"],
        ])
    )


def cost_sensitivity(report: Report, yahoo) -> None:
    rows = []
    for name in ("donchian", "tsmom", "carry", "donchian+tsmom+carry"):
        row = [name]
        for factor in (0.0, 1.0, 2.0, 4.0):
            s = summarize(backtest(COMBOS[name](), yahoo, costs=COSTS.scaled(factor)))
            row.append(f"{pct(s.cagr)} / {num(s.sharpe)}")
        for markup in (0.0, 0.5, 2.0):
            s = summarize(backtest(COMBOS[name](), yahoo, markup=markup))
            row.append(f"{pct(s.cagr)} / {num(s.sharpe)}")
        rows.append(row)
    headers = ["Strategy", "costs x0", "costs x1", "costs x2", "costs x4", "markup 0%", "markup 0.5%", "markup 2%"]
    report.add("## 7. Cost sensitivity (CAGR / Sharpe)\n\n"
               "Execution cost multiples scale spread, slippage and commission (swap markup 1%). "
               "Markup columns vary the swap markup per side (execution costs x1).\n\n" + table(headers, rows))


def parameter_grids(report: Report, yahoo) -> None:
    grids = {
        "donchian": [dict(entry=e, exit=x, stop_atr=k) for e in (20, 55, 100) for x in (10, 20, 40)
                     for k in (2.0, 3.0) if x < e],
        "tsmom": [dict(fast=f, medium=m, slow=s, stop_atr=k)
                  for f, m, s in ((21, 63, 126), (63, 126, 252), (21, 126, 252), (126, 189, 252))
                  for k in (3.0, 5.0)],
    }
    for name, grid in grids.items():
        rows, per_period, all_rets = [], [], []
        for params in grid:
            result = backtest([create_strategy(name, **params)], yahoo)
            _, rets = daily_returns(result)
            s = summarize(result)
            per_period.append(per_period_sharpe(rets))
            all_rets.append(rets)
            rows.append([", ".join(f"{k}={v:g}" for k, v in params.items()), pct(s.cagr), num(s.sharpe),
                         pct(s.max_drawdown), str(s.trades)])
        best = max(range(len(grid)), key=lambda i: per_period[i])
        dsr = deflated_sharpe(all_rets[best], per_period)
        default_label = ", ".join(f"{k}={v:g}" for k, v in grid[0].items())
        report.add(
            f"## 8. Parameter grid: {name} ({len(grid)} configurations)\n\n"
            + table(["Parameters", "CAGR", "Sharpe", "Max DD", "Trades"], rows)
            + f"\n\nBest: {rows[best][0]} (Sharpe {rows[best][2]}). Deflated Sharpe Ratio of the best "
              f"given {len(grid)} trials: {pct(dsr, 0)} (probability its true Sharpe is above zero after "
              f"accounting for the search; >95% would be convincing). First row: {default_label}."
        )


def specification_checks(report: Report, yahoo) -> None:
    fred = load("fred", FRED)
    variants = {
        "tsmom, rebalance daily": lambda: [create_strategy("tsmom", rebalance="daily")],
        "tsmom, rebalance weekly": lambda: [create_strategy("tsmom", rebalance="weekly")],
        "tsmom, rebalance monthly (default)": lambda: [create_strategy("tsmom")],
        "carry, no filters": lambda: [create_strategy("carry", trend=0, vol_cap=0)],
        "carry, trend filter only": lambda: [create_strategy("carry", vol_cap=0)],
        "carry, trend + volatility filters (default)": lambda: [create_strategy("carry")],
        "dollar carry": lambda: [DollarCarry()],
    }
    rows = []
    for name, make in variants.items():
        y = backtest(make(), yahoo)
        f = backtest(make(), fred, start=utc(1973))
        sy, sf = summarize(y), summarize(f)
        recent = window_stats(f, utc(2015), utc(2027))[1]
        rows.append([name, pct(sy.cagr), num(sy.sharpe), pct(sy.max_drawdown), str(sy.trades), num(sf.sharpe),
                     num(recent)])
    report.add("## 9. Specification checks\n\n"
               "Yahoo = 10 pairs 2005-2026 (OHLC); FRED = 9 USD pairs 1973-2026 (closes).\n\n"
               + table(["Variant", "Yahoo CAGR", "Yahoo Sharpe", "Yahoo Max DD", "Yahoo trades",
                        "FRED Sharpe 1973-2026", "FRED Sharpe 2015-2026"], rows))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--seeds", type=int, default=40, help="random-entry runs")
    parser.add_argument("--skip", nargs="*", default=[], help="sections to skip: long random costs grids checks")
    args = parser.parse_args()

    began = time.time()
    report = Report()
    report.add(f"# Research results (generated {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC)")
    textbook_replication(report)
    yahoo = load("yahoo", YAHOO)
    main_runs = main_results(report, yahoo)
    if "long" not in args.skip:
        long_history(report)
    if "random" not in args.skip:
        random_control(report, yahoo, main_runs, args.seeds)
    if "costs" not in args.skip:
        cost_sensitivity(report, yahoo)
    if "grids" not in args.skip:
        parameter_grids(report, yahoo)
    if "checks" not in args.skip:
        specification_checks(report, yahoo)
    report.add(f"_Runtime: {time.time() - began:.0f}s_")
    report.save(DATA / "results.md")


if __name__ == "__main__":
    main()
