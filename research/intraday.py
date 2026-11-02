"""Intraday experiments behind docs/RESEARCH.md, part II.

    python -m research.download --only histdata   # once; writes data/research/intraday/
    python -m research.intraday                   # writes data/research/intraday_results.md

The intraday strategy defaults follow the published papers and were fixed
before any of this ran. The parameter grid reports a Deflated Sharpe Ratio
to account for the search, and the months after each paper appeared serve
as an out-of-sample check.
"""
from __future__ import annotations

import argparse
import math
import os
import pickle
import random
import statistics
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from research.run_research import Report, num, pct, table  # noqa: E402
from research.strategies import LateDayMomentum, RandomOrb  # noqa: E402
from trading.backtest import Backtester, BacktestResult, Trade  # noqa: E402
from trading.costs import CostModel  # noqa: E402
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
from trading.models import Bar, SymbolSpec  # noqa: E402
from trading.risk import RISK_PROFILES, RiskConfig  # noqa: E402
from trading.sessions import session_for  # noqa: E402
from trading.strategies import STRATEGIES, Strategy  # noqa: E402

DATA = Path("data/research/intraday")
OUT = Path("data/research/intraday_results.md")
INDICES = ["#USNDAQ100", "#USSPX500", "#GERMANY40", "#UK100"]
OTHERS = ["XAUUSD", "EURUSD", "GBPUSD", "USDJPY"]
MARKETS = INDICES + OTHERS
CONVERSION = {"EUR": "EURUSD", "GBP": "GBPUSD", "JPY": "USDJPY"}
START = datetime(2011, 1, 1, tzinfo=timezone.utc)  # HistData starts mid-November 2010
# When each strategy's paper first appeared (SSRN); later data is out of sample.
PUBLISHED = {
    "noise_breakout": datetime(2024, 5, 1, tzinfo=timezone.utc),
    "orb": datetime(2023, 4, 1, tzinfo=timezone.utc),
    "late_momentum": datetime(2015, 1, 1, tzinfo=timezone.utc),
}
STRATEGY_CLASSES: dict[str, type[Strategy]] = {**STRATEGIES, "random_orb": RandomOrb, "late_momentum": LateDayMomentum}


def research_risk(n_markets: int = 1, risk: float = 0.01, leverage: float = 4.0) -> RiskConfig:
    """1% risk per trade, which with the default 0.5-sigma stop is the paper's 2% daily volatility target.

    The paper caps leverage at 4x; the cap scales with the number of markets.
    The daily-loss and drawdown kill switches are off so the tests measure the
    strategies, not the switches (section 7 turns them back on).
    """
    return RiskConfig(
        risk_per_trade=risk,
        max_open_risk=min(0.95, risk * 3 * n_markets),
        max_currency_risk=min(0.95, risk * 3 * n_markets),
        max_positions=10 * n_markets,
        max_leverage=leverage * n_markets,
        max_daily_loss=0.99,
        max_drawdown=0.99,
    )


# -- data ----------------------------------------------------------------------


_CACHE: dict[str, list[Bar]] = {}


def session_bars(symbol: str) -> list[Bar]:
    """Bars inside the symbol's trading session. The strategies are flat outside it, so nothing else matters."""
    if symbol in _CACHE:
        return _CACHE[symbol]
    source = DATA / f"{symbol}_M5.csv"
    if not source.exists():
        raise SystemExit(f"{source} not found; run: python -m research.download --only histdata")
    cache = DATA / ".cache" / f"{symbol}_session.pkl"
    if cache.exists() and cache.stat().st_mtime > source.stat().st_mtime:
        bars = pickle.loads(cache.read_bytes())
    else:
        session = session_for(symbol)
        bars = []
        for b in read_bars(source):
            start, end = session.bounds(session.day_of(b.time))
            if start <= b.time < end:
                bars.append(b)
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_bytes(pickle.dumps(bars, protocol=pickle.HIGHEST_PROTOCOL))
    _CACHE[symbol] = bars
    return bars


def hourly_bars(symbol: str) -> list[Bar]:
    """All of a pair's bars on the hour: enough to convert P&L into USD."""
    key = symbol + ":hourly"
    if key not in _CACHE:
        _CACHE[key] = [b for b in read_bars(DATA / f"{symbol}_M5.csv") if b.time.minute == 0]
    return _CACHE[key]


# -- running backtests in worker processes ---------------------------------------------


@dataclass(frozen=True)
class Leg:
    strategy: str
    params: tuple[tuple[str, object], ...] = ()


@dataclass(frozen=True)
class Job:
    label: str
    legs: tuple[Leg, ...]
    symbols: tuple[str, ...]
    cost: float = 1.0
    risk: RiskConfig = field(default_factory=research_risk)
    equity: float = 100_000.0


@dataclass
class Outcome:
    job: Job
    result: BacktestResult
    seconds: float


def run_job(job: Job) -> Outcome:
    started = time.time()
    pairs = {CONVERSION.get(SymbolSpec.infer(s).quote) for s in job.symbols} - {None} - set(job.symbols)
    for key in [k for k in _CACHE if k not in job.symbols and k.split(":")[0] not in pairs]:
        del _CACHE[key]  # keep worker memory to one job's markets
    bars = {s: session_bars(s) for s in job.symbols}
    for pair in pairs:
        bars[pair] = hourly_bars(pair)
    strategies = []
    for i, leg in enumerate(job.legs):
        strategy = STRATEGY_CLASSES[leg.strategy](**dict(leg.params))
        if sum(other.strategy == leg.strategy for other in job.legs) > 1:
            strategy.name = f"{leg.strategy}#{i}"
        strategies.append(strategy)
    result = Backtester(
        strategies, bars, timeframe="M5", symbols=list(job.symbols), costs=CostModel().scaled(job.cost),
        risk=job.risk, initial_equity=job.equity, start=START,
    ).run()
    # Ship back daily equity only: the full intraday curve is large and every statistic here is daily.
    result.equity_curve = daily_equity(result.equity_curve)
    return Outcome(job, result, time.time() - started)


def run_all(jobs: list[Job], workers: int) -> dict[str, Outcome]:
    out = {}
    with ProcessPoolExecutor(workers) as pool:
        for outcome in pool.map(run_job, jobs):
            out[outcome.job.label] = outcome
            s = summarize(outcome.result)
            print(f"  {outcome.job.label:55s} Sharpe {s.sharpe:5.2f} CAGR {s.cagr:7.2%} "
                  f"({outcome.seconds:.0f}s)", flush=True)
    return out


def leg(strategy: str, **params) -> Leg:
    return Leg(strategy, tuple(sorted(params.items())))


# -- statistics -----------------------------------------------------------------


def trade_bp(t: Trade, cost: float = 1.0) -> float:
    """Net return of one trade in basis points of its entry price (spread, slippage and commission included)."""
    commission = 0.0 if t.symbol.startswith("#") else 2 * CostModel().commission_per_million / 100 * cost
    return (t.exit_price / t.entry_price - 1.0) * t.direction * 10_000 - commission


def t_stat(values: list[float]) -> float:
    if len(values) < 3:
        return math.nan
    sd = statistics.stdev(values)
    return statistics.fmean(values) / sd * math.sqrt(len(values)) if sd > 0 else math.nan


def period_stats(result: BacktestResult, start: datetime | None, end: datetime | None) -> tuple[float, float, float]:
    points = [(t, v) for t, v in result.equity_curve if (start is None or t >= start) and (end is None or t < end)]
    if len(points) < 40:
        return math.nan, math.nan, math.nan
    years = (points[-1][0] - points[0][0]).days / 365.25
    values = [v for _, v in points]
    cagr = (values[-1] / values[0]) ** (1 / years) - 1 if values[-1] > 0 and years > 0 else math.nan
    return cagr, sharpe(returns(values)), max_drawdown(values)


def trades_per_year(result: BacktestResult) -> float:
    if not result.equity_curve:
        return math.nan
    years = (result.equity_curve[-1][0] - result.equity_curve[0][0]).days / 365.25
    return len(result.trades) / years if years > 0 else math.nan


def bootstrap_year(daily: list[float], draws: int = 2000, block: int = 20, seed: int = 1) -> dict[str, float]:
    """One-year outcomes from a block bootstrap of daily returns (keeps volatility clustering)."""
    rng = random.Random(seed)
    finals, drawdowns = [], []
    n = len(daily)
    for _ in range(draws):
        value, peak, worst = 1.0, 1.0, 0.0
        days = 0
        while days < 252:
            i = rng.randrange(0, n - block)
            for r in daily[i:i + block]:
                value *= 1 + r
                peak = max(peak, value)
                worst = max(worst, 1 - value / peak)
            days += block
        finals.append(value - 1)
        drawdowns.append(worst)
    finals.sort()
    return {
        "median": finals[len(finals) // 2],
        "p05": finals[len(finals) // 20],
        "p95": finals[-len(finals) // 20],
        "loss": sum(f < 0 for f in finals) / draws,
        "half": sum(f <= -0.5 for f in finals) / draws,
        "dd30": sum(d >= 0.3 for d in drawdowns) / draws,
        "dd50": sum(d >= 0.5 for d in drawdowns) / draws,
    }


# -- experiments ------------------------------------------------------------------


def strategy_market_jobs() -> list[Job]:
    jobs = []
    for strategy in ("noise_breakout", "orb", "late_momentum"):
        for market in MARKETS:
            for cost in (0.0, 1.0):
                jobs.append(Job(f"{strategy} {market} x{cost:g}", (leg(strategy),), (market,), cost))
    return jobs


def section_markets(report: Report, results: dict[str, Outcome]) -> None:
    rows, sharpes, series, labels = [], [], [], []
    for strategy in ("noise_breakout", "orb", "late_momentum"):
        for market in MARKETS:
            gross = results[f"{strategy} {market} x0"].result
            net = results[f"{strategy} {market} x1"].result
            s, g = summarize(net), summarize(gross)
            rets = returns([v for _, v in net.equity_curve])
            sharpes.append(per_period_sharpe(rets))
            series.append(rets)
            labels.append(f"{strategy} on {market}")
            gross_bp = [trade_bp(t, 0.0) for t in gross.trades]
            net_bp = [trade_bp(t) for t in net.trades]
            rows.append([strategy, market, num(g.sharpe), num(s.sharpe), pct(s.cagr), pct(s.max_drawdown),
                         f"{trades_per_year(net):.0f}", pct(s.win_rate, 0),
                         f"{statistics.fmean(gross_bp):+.2f} ({t_stat(gross_bp):+.1f})" if gross_bp else "n/a",
                         f"{statistics.fmean(net_bp):+.2f} ({t_stat(net_bp):+.1f})" if net_bp else "n/a"])
    best = max(range(len(sharpes)), key=lambda i: sharpes[i])
    report.add(
        "## 1. Each strategy on each market, 2011-01 to 2026-10\n\n"
        "One market per run, USD 100k, 1% risk per trade (the papers' exposure), leverage capped at 4x, "
        "kill switches off. Gross = no spread, slippage or commission; net = FxPro-like costs. "
        "bp/trade is the average trade's return in basis points of the entry price, with its t-statistic.\n\n"
        + table(["Strategy", "Market", "Gross Sharpe", "Net Sharpe", "Net CAGR", "Max DD", "Trades/yr", "Win",
                 "Gross bp/trade (t)", "Net bp/trade (t)"], rows)
        + f"\n\nDeflated Sharpe Ratio of the best net result ({labels[best]}) among these {len(rows)} "
        f"strategy-market combinations: **{deflated_sharpe(series[best], sharpes):.0%}**.")


def section_periods(report: Report, results: dict[str, Outcome]) -> None:
    cuts = [(datetime(2011, 1, 1, tzinfo=timezone.utc), datetime(2016, 1, 1, tzinfo=timezone.utc), "2011-15"),
            (datetime(2016, 1, 1, tzinfo=timezone.utc), datetime(2021, 1, 1, tzinfo=timezone.utc), "2016-20")]
    rows = []
    for strategy in ("noise_breakout", "orb"):
        published = PUBLISHED[strategy]
        periods = cuts + [(datetime(2021, 1, 1, tzinfo=timezone.utc), published, f"2021-{published:%y.%m}"),
                          (published, None, f"after {published:%Y-%m}")]
        for market in INDICES:
            net = results[f"{strategy} {market} x1"].result
            row = [strategy, market]
            for a, b, _ in periods:
                cagr, sr, _ = period_stats(net, a, b)
                row.append(f"{num(sr)} ({pct(cagr)})")
            rows.append(row)
    report.add(
        "## 2. Sub-periods, net of costs: Sharpe (CAGR)\n\n"
        "The last column starts when the paper appeared on SSRN, so it is out of sample "
        "(noise_breakout: May 2024; orb: April 2023).\n\n"
        + table(["Strategy", "Market", "2011-15", "2016-20", "2021 to paper", "After paper"], rows))

    years = sorted({t.year for t, _ in results["noise_breakout #USNDAQ100 x1"].result.equity_curve})
    rows = []
    for market in INDICES:
        curve = results[f"noise_breakout {market} x1"].result.equity_curve
        by_year: dict[int, float] = {}
        previous = curve[0][1]
        last: dict[int, float] = {}
        for t, v in curve:
            last[t.year] = v
        for year in years:
            if year in last:
                by_year[year] = last[year] / previous - 1
                previous = last[year]
        rows.append([market] + [pct(by_year.get(y, math.nan), 0) for y in years])
    report.add("## 3. noise_breakout, calendar-year returns net of costs\n\n"
               + table(["Market"] + [str(y) for y in years], rows))


def control_jobs(seeds: int) -> list[Job]:
    jobs = []
    for market in INDICES[:3]:
        for seed in range(seeds):
            jobs.append(Job(f"random_orb {market} seed {seed}", (leg("random_orb", seed=seed),), (market,)))
    return jobs


def section_control(report: Report, results: dict[str, Outcome], seeds: int) -> None:
    rows = []
    for market in INDICES[:3]:
        orb = summarize(results[f"orb {market} x1"].result).sharpe
        randoms = [summarize(results[f"random_orb {market} seed {s}"].result).sharpe for s in range(seeds)]
        beat = sum(r >= orb for r in randoms)
        rows.append([market, num(orb), num(statistics.fmean(randoms)), num(statistics.stdev(randoms)),
                     num(max(randoms)), f"{beat} of {seeds}"])
    report.add(
        "## 4. Random-direction control for the opening range breakout (net of costs)\n\n"
        "Same entry time, stop, target, sizing and costs as `orb`, but a coin flip picks the direction.\n\n"
        + table(["Market", "orb Sharpe", "Random mean", "Random st.dev.", "Random best", "Random >= orb"], rows))


GRID = [dict(band=b, check=c, stop_sigma=s) for b in (0.75, 1.0, 1.25, 1.5) for c in (30, 60) for s in (0.5, 1.0)]


def grid_jobs(market: str) -> list[Job]:
    return [Job(f"grid {market} " + " ".join(f"{k}={v}" for k, v in g.items()),
                (leg("noise_breakout", **g),), (market,),
                risk=research_risk(risk=0.01 * g["stop_sigma"] / 0.5)) for g in GRID]


def section_grid(report: Report, results: dict[str, Outcome], market: str) -> None:
    rows, sharpes, series = [], [], []
    for g in GRID:
        label = f"grid {market} " + " ".join(f"{k}={v}" for k, v in g.items())
        res = results[label].result
        s = summarize(res)
        rets = returns([v for _, v in res.equity_curve])
        rows.append((s.sharpe, [str(g["band"]), str(g["check"]), str(g["stop_sigma"]), num(s.sharpe), pct(s.cagr),
                                pct(s.max_drawdown), f"{trades_per_year(res):.0f}"]))
        sharpes.append(per_period_sharpe(rets))
        series.append(rets)
    best = max(range(len(GRID)), key=lambda i: rows[i][0])
    dsr = deflated_sharpe(series[best], sharpes)
    rows.sort(key=lambda r: -r[0])
    report.add(
        f"## 5. noise_breakout parameter grid on {market}, net of costs\n\n"
        "Risk per trade scales with the stop so exposure stays the same. "
        f"Deflated Sharpe Ratio of the best of {len(GRID)} configurations: **{dsr:.0%}** "
        "(the probability its true Sharpe is above zero once the search is accounted for).\n\n"
        + table(["band", "check (min)", "stop (sigma)", "Sharpe", "CAGR", "Max DD", "Trades/yr"], [r for _, r in rows]))


PORTFOLIOS = {
    "noise: Nasdaq 100": ((leg("noise_breakout"),), ("#USNDAQ100",)),
    "noise: Nasdaq 100 + S&P 500": ((leg("noise_breakout"),), ("#USNDAQ100", "#USSPX500")),
    "noise: 4 indices": ((leg("noise_breakout"),), tuple(INDICES)),
    "noise + orb: Nasdaq 100": ((leg("noise_breakout"), leg("orb")), ("#USNDAQ100",)),
}
COSTS = (0.0, 0.5, 1.0, 1.5, 2.0)


def portfolio_jobs() -> list[Job]:
    jobs = []
    for name, (legs, symbols) in PORTFOLIOS.items():
        for cost in COSTS:
            jobs.append(Job(f"{name} x{cost:g}", legs, symbols, cost,
                            risk=research_risk(n_markets=len(symbols) * len(legs))))
    return jobs


def section_portfolios(report: Report, results: dict[str, Outcome]) -> None:
    rows = []
    for name in PORTFOLIOS:
        row = [name]
        for cost in COSTS:
            s = summarize(results[f"{name} x{cost:g}"].result)
            row.append(f"{num(s.sharpe)} ({pct(s.cagr)})")
        net = results[f"{name} x1"].result
        row.append(pct(summarize(net).max_drawdown))
        row.append(f"{trades_per_year(net):.0f}")
        rows.append(row)
    report.add(
        "## 6. Portfolios and cost sensitivity: Sharpe (CAGR)\n\n"
        "Costs x1 is the FxPro-like model; x2 doubles spread, slippage and commission. "
        "Max DD and trades per year are at x1.\n\n"
        + table(["Portfolio"] + [f"Costs x{c:g}" for c in COSTS] + ["Max DD", "Trades/yr"], rows))


RISK_LEVELS = (0.005, 0.01, 0.02, 0.03, 0.05)
SMALL_ACCOUNT = 1_000.0
CANDIDATE = "noise: Nasdaq 100"


def risk_jobs() -> list[Job]:
    legs, symbols = PORTFOLIOS[CANDIDATE]
    jobs = []
    for risk in RISK_LEVELS:
        config = RiskConfig(risk_per_trade=risk, max_open_risk=min(0.95, 3 * risk), max_currency_risk=min(0.95, 3 * risk),
                            max_positions=6, max_leverage=20.0, max_daily_loss=0.99, max_drawdown=0.99)
        jobs.append(Job(f"risk {risk:g}", legs, symbols, risk=config, equity=SMALL_ACCOUNT))
    aggressive = RISK_PROFILES["aggressive"]
    jobs.append(Job("risk aggressive profile", legs, symbols, risk=aggressive, equity=SMALL_ACCOUNT))
    return jobs


def section_risk(report: Report, results: dict[str, Outcome]) -> None:
    rows, boot_rows = [], []
    for risk in RISK_LEVELS:
        res = results[f"risk {risk:g}"].result
        s = summarize(res)
        values = [res.initial_equity] + [v for _, v in res.equity_curve]
        daily = returns(values)
        monthly: dict[tuple[int, int], float] = {}
        for t, v in res.equity_curve:
            monthly[(t.year, t.month)] = v
        month_values = [res.initial_equity] + [monthly[k] for k in sorted(monthly)]
        month_rets = returns(month_values)
        rows.append([f"{risk:.1%}", pct(s.cagr), pct(s.volatility), num(s.sharpe), pct(s.max_drawdown),
                     pct(min(daily)), pct(min(month_rets)), pct(sum(r < 0 for r in month_rets) / len(month_rets), 0),
                     f"{s.final_equity:,.0f}", str(s.rejected_entries)])
        recent = [r for (t, _), r in zip(res.equity_curve, daily) if t >= datetime(2016, 1, 1, tzinfo=timezone.utc)]
        b = bootstrap_year(recent)
        boot_rows.append([f"{risk:.1%}", pct(b["median"]), pct(b["p05"]), pct(b["p95"]), pct(b["loss"], 0),
                          pct(b["dd30"], 0), pct(b["dd50"], 0), pct(b["half"], 0)])
    report.add(
        f"## 7. Risk per trade on a USD {SMALL_ACCOUNT:,.0f} account: {CANDIDATE}, net of costs\n\n"
        "Leverage capped at 20x (the EU retail limit for major indices), kill switches off. "
        "Position sizes are rounded to the 0.01 minimum. Rejected = entries too small for the minimum.\n\n"
        + table(["Risk/trade", "CAGR", "Vol", "Sharpe", "Max DD", "Worst day", "Worst month", "Losing months",
                 "Final equity", "Rejected"], rows))
    report.add(
        "### 7b. One-year outcomes, block bootstrap of 2016-2026 daily returns (2,000 draws)\n\n"
        + table(["Risk/trade", "Median year", "5th pct", "95th pct", "P(loss)", "P(DD >= 30%)", "P(DD >= 50%)",
                 "P(lose half)"], boot_rows))
    res = results["risk aggressive profile"].result
    s = summarize(res)
    halts = "; ".join(f"{t:%Y-%m-%d}: {reason.split(';')[0]}" for t, reason in res.halts[:6]) or "none"
    report.add(
        "### 7c. The `aggressive` risk profile with its kill switches on\n\n"
        f"CAGR {pct(s.cagr)}, Sharpe {num(s.sharpe)}, max drawdown {pct(s.max_drawdown)}, "
        f"final equity {s.final_equity:,.0f}, {len(res.halts)} halts. First halts: {halts}.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--workers", type=int, default=min(4, os.cpu_count() or 1))
    parser.add_argument("--seeds", type=int, default=20, help="random-direction control runs per market")
    args = parser.parse_args()
    started = time.time()
    for symbol in MARKETS:  # build the on-disk session caches once, before the workers need them
        session_bars(symbol)
        _CACHE.clear()
    jobs = (strategy_market_jobs() + control_jobs(args.seeds) + grid_jobs("#USNDAQ100") + portfolio_jobs()
            + risk_jobs())
    print(f"{len(jobs)} backtests on {args.workers} workers", flush=True)
    results = run_all(jobs, args.workers)

    report = Report()
    report.add("# Intraday research results\n\n"
               f"Generated by `python -m research.intraday` on {datetime.now(timezone.utc):%Y-%m-%d}. "
               "Data: HistData.com one-minute bid bars aggregated to five minutes, "
               "trading-session bars only, 2011-01 to 2026-10.")
    section_markets(report, results)
    section_periods(report, results)
    section_control(report, results, args.seeds)
    section_grid(report, results, "#USNDAQ100")
    section_portfolios(report, results)
    section_risk(report, results)
    report.save(OUT)
    print(f"Wrote {OUT} in {(time.time() - started) / 60:.0f} minutes")


if __name__ == "__main__":
    main()
