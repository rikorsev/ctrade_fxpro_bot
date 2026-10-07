"""Performance statistics, including selection-bias-aware Sharpe ratio tests."""
from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from statistics import NormalDist

from trading.backtest import BacktestResult, Trade

TRADING_DAYS = 252
EULER_GAMMA = 0.5772156649015329
_NORMAL = NormalDist()


def daily_equity(curve: Sequence[tuple[datetime, float]]) -> list[tuple[datetime, float]]:
    """Last equity value of each UTC calendar day."""
    by_day: dict = {}
    for t, value in curve:
        by_day[t.date()] = (t, value)
    return [by_day[d] for d in sorted(by_day)]


def returns(values: Sequence[float]) -> list[float]:
    return [b / a - 1.0 for a, b in zip(values, values[1:]) if a > 0]


def max_drawdown(values: Sequence[float]) -> float:
    """Largest peak-to-trough decline as a positive fraction."""
    peak, worst = -math.inf, 0.0
    for v in values:
        peak = max(peak, v)
        if peak > 0:
            worst = max(worst, 1.0 - v / peak)
    return worst


def longest_drawdown_days(points: Sequence[tuple[datetime, float]]) -> int:
    """Longest time from a peak to the recovery of that peak (or to the end, if never recovered)."""
    peak_value, peak_time, longest, underwater = -math.inf, None, 0, False
    for t, v in points:
        if v >= peak_value:
            if underwater:
                longest = max(longest, (t - peak_time).days)
            peak_value, peak_time, underwater = v, t, False
        else:
            underwater = True
    if underwater:
        longest = max(longest, (points[-1][0] - peak_time).days)
    return longest


def sharpe(rets: Sequence[float], periods_per_year: int = TRADING_DAYS) -> float:
    if len(rets) < 2:
        return math.nan
    sd = statistics.stdev(rets)
    return statistics.fmean(rets) / sd * math.sqrt(periods_per_year) if sd > 0 else math.nan


def sortino(rets: Sequence[float], periods_per_year: int = TRADING_DAYS) -> float:
    if len(rets) < 2:
        return math.nan
    downside = math.sqrt(math.fsum(min(r, 0.0) ** 2 for r in rets) / len(rets))
    return statistics.fmean(rets) / downside * math.sqrt(periods_per_year) if downside > 0 else math.nan


def _moments(rets: Sequence[float]) -> tuple[float, float]:
    """Skewness and (non-excess) kurtosis."""
    n = len(rets)
    mean = statistics.fmean(rets)
    m2 = math.fsum((r - mean) ** 2 for r in rets) / n
    if m2 == 0:
        return 0.0, 3.0
    m3 = math.fsum((r - mean) ** 3 for r in rets) / n
    m4 = math.fsum((r - mean) ** 4 for r in rets) / n
    return m3 / m2**1.5, m4 / m2**2


def probabilistic_sharpe(rets: Sequence[float], benchmark_sr: float = 0.0) -> float:
    """P(true per-period Sharpe > benchmark) given sample length, skew and fat tails (Bailey & Lopez de Prado 2012)."""
    n = len(rets)
    if n < 3:
        return math.nan
    sd = statistics.stdev(rets)
    if sd == 0:
        return math.nan
    sr = statistics.fmean(rets) / sd
    skew, kurt = _moments(rets)
    variance = 1 - skew * sr + (kurt - 1) / 4 * sr**2
    if variance <= 0:
        return math.nan
    return _NORMAL.cdf((sr - benchmark_sr) * math.sqrt(n - 1) / math.sqrt(variance))


def expected_max_sharpe(trial_sharpes: Sequence[float]) -> float:
    """Expected best per-period Sharpe among N unskilled trials with the observed dispersion."""
    n = len(trial_sharpes)
    if n < 2:
        return 0.0
    sd = statistics.stdev(trial_sharpes)
    return sd * (
        (1 - EULER_GAMMA) * _NORMAL.inv_cdf(1 - 1 / n) + EULER_GAMMA * _NORMAL.inv_cdf(1 - 1 / (n * math.e))
    )


def deflated_sharpe(rets: Sequence[float], trial_sharpes: Sequence[float]) -> float:
    """Deflated Sharpe Ratio (Bailey & Lopez de Prado 2014).

    ``trial_sharpes`` are the per-period (not annualised) Sharpe ratios of
    every configuration that was tried, including the selected one. Values
    below ~0.95 mean the result is plausibly a product of the search.
    """
    return probabilistic_sharpe(rets, expected_max_sharpe(trial_sharpes))


def per_period_sharpe(rets: Sequence[float]) -> float:
    if len(rets) < 2:
        return math.nan
    sd = statistics.stdev(rets)
    return statistics.fmean(rets) / sd if sd > 0 else math.nan


@dataclass(frozen=True)
class Summary:
    start: datetime
    end: datetime
    initial_equity: float
    final_equity: float
    cagr: float
    volatility: float
    sharpe: float
    sortino: float
    max_drawdown: float
    longest_drawdown_days: int
    calmar: float
    psr: float
    trades: int
    win_rate: float
    profit_factor: float
    avg_r: float
    avg_holding_days: float
    commission: float
    swap: float
    halts: int
    rejected_entries: int

    def format(self) -> str:
        rows = [
            ("Period", f"{self.start:%Y-%m-%d} .. {self.end:%Y-%m-%d}"),
            ("Equity", f"{self.initial_equity:,.0f} -> {self.final_equity:,.0f}"),
            ("CAGR", f"{self.cagr:.2%}"),
            ("Volatility (ann.)", f"{self.volatility:.2%}"),
            ("Sharpe", f"{self.sharpe:.2f}"),
            ("Sortino", f"{self.sortino:.2f}"),
            ("Max drawdown", f"{self.max_drawdown:.2%}"),
            ("Longest drawdown", f"{self.longest_drawdown_days} days"),
            ("Calmar", f"{self.calmar:.2f}"),
            ("P(Sharpe > 0)", f"{self.psr:.1%}"),
            ("Trades", f"{self.trades}"),
            ("Win rate", f"{self.win_rate:.1%}"),
            ("Profit factor", f"{self.profit_factor:.2f}"),
            ("Avg R per trade", f"{self.avg_r:+.2f}"),
            ("Avg holding", f"{self.avg_holding_days:.1f} days"),
            ("Commission paid", f"{self.commission:,.0f}"),
            ("Swap earned", f"{self.swap:+,.0f}"),
            ("Risk halts", f"{self.halts}"),
            ("Entries rejected by risk", f"{self.rejected_entries}"),
        ]
        width = max(len(k) for k, _ in rows)
        return "\n".join(f"{k:<{width}}  {v}" for k, v in rows)


def summarize(result: BacktestResult) -> Summary:
    points = daily_equity(result.equity_curve)
    if len(points) < 2:
        raise ValueError("backtest produced fewer than two days of equity; check data range and warm-up")
    values = [result.initial_equity] + [v for _, v in points]
    rets = returns(values)
    start, end = points[0][0], points[-1][0]
    years = max((end - start).days / 365.25, 1 / 365.25)
    final = values[-1]
    cagr = (final / result.initial_equity) ** (1 / years) - 1 if final > 0 else -1.0
    vol = statistics.stdev(rets) * math.sqrt(TRADING_DAYS) if len(rets) > 1 else math.nan
    mdd = max_drawdown(values)
    trades = result.trades
    return Summary(
        start=start,
        end=end,
        initial_equity=result.initial_equity,
        final_equity=final,
        cagr=cagr,
        volatility=vol,
        sharpe=sharpe(rets),
        sortino=sortino(rets),
        max_drawdown=mdd,
        longest_drawdown_days=longest_drawdown_days(points),
        calmar=cagr / mdd if mdd > 0 else math.nan,
        psr=probabilistic_sharpe(rets),
        **trade_stats(trades),
        halts=len(result.halts),
        rejected_entries=result.rejected_entries,
    )


def trade_stats(trades: Sequence[Trade]) -> dict[str, float]:
    if not trades:
        return dict(trades=0, win_rate=math.nan, profit_factor=math.nan, avg_r=math.nan,
                    avg_holding_days=math.nan, commission=0.0, swap=0.0)
    wins = [t.pnl for t in trades if t.pnl > 0]
    losses = [-t.pnl for t in trades if t.pnl <= 0]
    rs = [t.r_multiple for t in trades if not math.isnan(t.r_multiple)]
    return dict(
        trades=len(trades),
        win_rate=len(wins) / len(trades),
        profit_factor=sum(wins) / sum(losses) if sum(losses) > 0 else math.inf,
        avg_r=statistics.fmean(rs) if rs else math.nan,
        avg_holding_days=statistics.fmean((t.exit_time - t.entry_time).total_seconds() / 86400 for t in trades),
        commission=math.fsum(t.commission for t in trades),
        swap=math.fsum(t.swap for t in trades),
    )


def yearly_returns(result: BacktestResult) -> list[tuple[int, float]]:
    out = []
    previous = result.initial_equity
    by_year: dict[int, float] = {}
    for t, value in daily_equity(result.equity_curve):
        by_year[t.year] = value
    for year in sorted(by_year):
        out.append((year, by_year[year] / previous - 1.0))
        previous = by_year[year]
    return out
