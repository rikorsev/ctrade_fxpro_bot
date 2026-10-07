"""Event-driven bar-by-bar backtester.

It drives the same strategies, planner and risk manager as the live trader.
Execution model, chosen to be conservative:

* Bars are bid prices (cTrader trendbars are). Longs fill at the ask
  (bid + spread), shorts close at the ask; slippage is added to every market
  fill. Commission is charged on both sides.
* A signal computed at a bar's close is filled at the *next* bar's open, so
  there is no look-ahead. Position sizes are fixed at signal time, as in live
  trading.
* Stops and take-profits are checked against each bar's high/low. If a bar
  touches both, the stop wins. A gap through the stop fills at the open.
* Swaps accrue at every rollover (triple on the broker's triple-swap day) and
  are realised when the position closes.
"""
from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from trading.costs import BrokerSwaps, CostModel, CurrencyConverter, SwapCarry, SwapModel, rollovers
from trading.models import LONG, Bar, Position, SymbolSpec, timeframe_delta
from trading.planner import CloseIntent, EntryIntent, StopUpdate, plan_actions
from trading.risk import Exposure, RiskConfig, RiskManager, position_exposure
from trading.strategies import MarketContext, Strategy
from trading.strategies.base import CarrySource


@dataclass(frozen=True)
class Trade:
    strategy: str
    symbol: str
    direction: int
    units: float
    entry_time: datetime
    entry_price: float
    exit_time: datetime
    exit_price: float
    gross_pnl: float  # account currency, before costs below
    commission: float
    swap: float  # positive = earned
    initial_risk: float  # account currency at the initial stop
    exit_reason: str

    @property
    def pnl(self) -> float:
        return self.gross_pnl + self.swap - self.commission

    @property
    def r_multiple(self) -> float:
        return self.pnl / self.initial_risk if self.initial_risk > 0 else math.nan


@dataclass
class BacktestResult:
    initial_equity: float
    account_currency: str
    equity_curve: list[tuple[datetime, float]] = field(default_factory=list)
    trades: list[Trade] = field(default_factory=list)
    halts: list[tuple[datetime, str]] = field(default_factory=list)
    rejected_entries: int = 0

    @property
    def final_equity(self) -> float:
        return self.equity_curve[-1][1] if self.equity_curve else self.initial_equity


@dataclass
class _Open:
    position: Position
    spec: SymbolSpec
    initial_risk: float
    commission: float
    swap: float = 0.0
    last_accrual: datetime | None = None


@dataclass
class _PendingEntry:
    intent: EntryIntent
    units: float
    risk: float
    exposure: Exposure


class Backtester:
    def __init__(
        self,
        strategies: Sequence[Strategy],
        bars: Mapping[str, Sequence[Bar]],
        *,
        timeframe: str,
        symbols: Sequence[str] | None = None,
        specs: Mapping[str, SymbolSpec] | None = None,
        costs: CostModel | None = None,
        risk: RiskConfig | None = None,
        swaps: SwapModel | None = None,
        carry: CarrySource | None = None,
        initial_equity: float = 100_000.0,
        account_currency: str = "USD",
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> None:
        if not strategies:
            raise ValueError("need at least one strategy")
        names = [s.name for s in strategies]
        if len(set(names)) != len(names):
            raise ValueError("strategies must have distinct names")
        self.strategies = list(strategies)
        self.bars = {s: list(b) for s, b in bars.items()}
        self.timeframe = timeframe
        self.bar_length = timeframe_delta(timeframe)
        self.symbols = list(symbols) if symbols is not None else list(self.bars)
        missing = [s for s in self.symbols if s not in self.bars]
        if missing:
            raise ValueError(f"no bars for {', '.join(missing)}")
        specs = dict(specs or {})
        self.specs = {s: specs.get(s) or SymbolSpec.infer(s) for s in self.symbols}
        self.costs = costs or CostModel()
        self.risk = RiskManager(risk)
        self.swaps = swaps if swaps is not None else BrokerSwaps()
        self.carry = carry if carry is not None else SwapCarry(self.swaps, self.specs)
        self.initial_equity = initial_equity
        self.account_currency = account_currency
        self.start = start
        self.end = end

        self._last_close: dict[str, float] = {}
        self.converter = CurrencyConverter(self._last_close.get, self.bars)
        for spec in self.specs.values():
            for source, target in ((spec.quote, account_currency), (spec.quote, "USD"), ("USD", account_currency)):
                if not self.converter.can_convert(source, target):
                    raise ValueError(
                        f"{spec.name}: cannot convert {source} to {target}; add bars for a conversion pair"
                    )

    def run(self) -> BacktestResult:
        result = BacktestResult(self.initial_equity, self.account_currency)
        self._result = result
        self._cash = self.initial_equity
        self._open: dict[tuple[str, str], _Open] = {}
        self._pending_close: dict[tuple[str, str], CloseIntent] = {}
        self._pending_entry: dict[tuple[str, str], _PendingEntry] = {}
        self._last_close.clear()
        traded = set(self.symbols)

        events: dict[datetime, list[tuple[str, int]]] = defaultdict(list)
        for symbol, series in self.bars.items():
            for i, bar in enumerate(series):
                if self.end is None or bar.time < self.end:
                    events[bar.time].append((symbol, i))

        halted: str | None = None
        for bar_time in sorted(events):
            entries = events[bar_time]
            close_time = bar_time + self.bar_length
            active = self.start is None or bar_time >= self.start
            for symbol, i in entries:
                if symbol in traded:
                    bar = self.bars[symbol][i]
                    self._fill_pending(symbol, bar)
                    self._check_exits(symbol, bar)
            for symbol, i in entries:
                self._last_close[symbol] = self.bars[symbol][i].close
            for symbol, i in entries:
                if symbol in traded:
                    self._accrue_swaps(symbol, close_time)
            if not active:
                continue

            equity = self._equity()
            reason = self.risk.update(equity, close_time)
            if reason and reason != halted:
                result.halts.append((close_time, reason))
            halted = reason
            result.equity_curve.append((close_time, equity))
            for symbol, i in entries:
                if symbol in traded:
                    self._decide(symbol, i, close_time, equity)

        self._close_all_at_end()
        return result

    # -- decisions ---------------------------------------------------------

    def _decide(self, symbol: str, i: int, now: datetime, equity: float) -> None:
        series = self.bars[symbol]
        context = MarketContext(time=now, carry=self.carry)
        for strategy in self.strategies:
            if i + 1 < strategy.lookback:
                continue
            key = (strategy.name, symbol)
            held = self._open.get(key)
            position = held.position if held else None
            window = series[i + 1 - strategy.lookback:i + 1]
            signal = strategy.on_bar(symbol, window, position, context)
            for action in plan_actions(strategy.name, symbol, signal, position):
                if isinstance(action, StopUpdate):
                    position.stop_loss = self.specs[symbol].round_price(action.stop_loss)
                elif isinstance(action, CloseIntent):
                    self._pending_close[key] = action
                elif isinstance(action, EntryIntent):
                    self._queue_entry(key, action, equity)

    def _queue_entry(self, key: tuple[str, str], intent: EntryIntent, equity: float) -> None:
        spec = self.specs[intent.symbol]
        price = self._last_close[intent.symbol]
        try:
            quote_to_account = self.converter.rate(spec.quote, self.account_currency)
        except LookupError:
            self._result.rejected_entries += 1
            return
        decision = self.risk.size(
            equity=equity,
            spec=spec,
            direction=intent.direction,
            price=price,
            stop_distance=intent.stop_distance,
            quote_to_account=quote_to_account,
            exposures=self._exposures(exclude=key),
        )
        if decision.units <= 0:
            self._result.rejected_entries += 1
            return
        exposure = Exposure(spec.base, spec.quote, intent.direction, decision.risk, decision.notional)
        self._pending_entry[key] = _PendingEntry(intent, decision.units, decision.risk, exposure)

    def _exposures(self, exclude: tuple[str, str]) -> list[Exposure]:
        exposures = []
        for key, held in self._open.items():
            if key == exclude or key in self._pending_close:
                continue
            spec = held.spec
            rate = self.converter.rate(spec.quote, self.account_currency)
            exposures.append(
                position_exposure(held.position, spec, self._last_close[spec.name], rate, held.initial_risk)
            )
        exposures.extend(p.exposure for key, p in self._pending_entry.items() if key != exclude)
        return exposures

    # -- execution ---------------------------------------------------------

    def _fill_pending(self, symbol: str, bar: Bar) -> None:
        for key in [k for k in self._pending_close if k[1] == symbol]:
            intent = self._pending_close.pop(key)
            held = self._open.get(key)
            if held is not None:
                price = self._exit_price(held, bar.open)
                self._close(key, price, bar.time, intent.reason or "signal")
        for key in [k for k in self._pending_entry if k[1] == symbol]:
            pending = self._pending_entry.pop(key)
            self._open_position(key, pending, bar)

    def _open_position(self, key: tuple[str, str], pending: _PendingEntry, bar: Bar) -> None:
        intent = pending.intent
        spec = self.specs[intent.symbol]
        spread, slip = self.costs.spread(spec), self.costs.slippage(spec)
        price = bar.open + spread + slip if intent.direction == LONG else bar.open - slip
        stop = spec.round_price(price - intent.direction * intent.stop_distance)
        take_profit = None
        if intent.take_profit_distance:
            take_profit = spec.round_price(price + intent.direction * intent.take_profit_distance)
        position = Position(
            symbol=intent.symbol,
            strategy=intent.strategy,
            direction=intent.direction,
            units=pending.units,
            entry_price=price,
            entry_time=bar.time,
            stop_loss=stop,
            take_profit=take_profit,
        )
        commission = self._commission(spec, pending.units, price)
        self._cash -= commission
        self._open[key] = _Open(position, spec, pending.risk, commission, last_accrual=bar.time)

    def _check_exits(self, symbol: str, bar: Bar) -> None:
        for key in [k for k in self._open if k[1] == symbol]:
            held = self._open[key]
            p = held.position
            spread, slip = self.costs.spread(held.spec), self.costs.slippage(held.spec)
            if p.direction == LONG:
                if p.stop_loss is not None and bar.low <= p.stop_loss:
                    self._close(key, min(bar.open, p.stop_loss) - slip, bar.time, "stop")
                elif p.take_profit is not None and bar.high >= p.take_profit:
                    self._close(key, max(bar.open, p.take_profit), bar.time, "take profit")
            else:
                ask_open, ask_high, ask_low = bar.open + spread, bar.high + spread, bar.low + spread
                if p.stop_loss is not None and ask_high >= p.stop_loss:
                    self._close(key, max(ask_open, p.stop_loss) + slip, bar.time, "stop")
                elif p.take_profit is not None and ask_low <= p.take_profit:
                    self._close(key, min(ask_open, p.take_profit), bar.time, "take profit")

    def _exit_price(self, held: _Open, bid: float) -> float:
        spread, slip = self.costs.spread(held.spec), self.costs.slippage(held.spec)
        return bid - slip if held.position.direction == LONG else bid + spread + slip

    def _close(self, key: tuple[str, str], price: float, when: datetime, reason: str) -> None:
        held = self._open.pop(key)
        p, spec = held.position, held.spec
        self._accrue_swaps(spec.name, when, held)
        rate = self.converter.rate(spec.quote, self.account_currency)
        gross = (price - p.entry_price) * p.direction * p.units * rate
        commission = self._commission(spec, p.units, price)
        self._cash += gross + held.swap - commission
        self._result.trades.append(
            Trade(
                strategy=p.strategy,
                symbol=p.symbol,
                direction=p.direction,
                units=p.units,
                entry_time=p.entry_time,
                entry_price=p.entry_price,
                exit_time=when,
                exit_price=price,
                gross_pnl=gross,
                commission=held.commission + commission,
                swap=held.swap,
                initial_risk=held.initial_risk,
                exit_reason=reason,
            )
        )

    def _close_all_at_end(self) -> None:
        """Close what is still open at the last close so every position shows up in the trade list."""
        if not self._open or not self._result.equity_curve:
            return
        end = self._result.equity_curve[-1][0]
        for key in list(self._open):
            held = self._open[key]
            self._close(key, self._exit_price(held, self._last_close[held.spec.name]), end, "end of data")
        self._result.equity_curve[-1] = (end, self._equity())

    def _commission(self, spec: SymbolSpec, units: float, price: float) -> float:
        usd_notional = units * price * self.converter.rate(spec.quote, "USD")
        usd = usd_notional * self.costs.commission_per_million / 1_000_000
        return usd * self.converter.rate("USD", self.account_currency)

    def _accrue_swaps(self, symbol: str, until: datetime, only: _Open | None = None) -> None:
        targets = [only] if only is not None else [h for k, h in self._open.items() if k[1] == symbol]
        for held in targets:
            p, spec = held.position, held.spec
            count = rollovers(held.last_accrual, until, spec.triple_swap_weekday)
            held.last_accrual = max(held.last_accrual, until)
            if not count:
                continue
            price = self._last_close.get(symbol, p.entry_price)
            per_unit = self.swaps.swap_per_unit(spec, p.direction, price, until)
            if per_unit:
                rate = self.converter.rate(spec.quote, self.account_currency)
                held.swap += count * per_unit * p.units * rate

    def _equity(self) -> float:
        equity = self._cash
        for held in self._open.values():
            p, spec = held.position, held.spec
            bid = self._last_close[spec.name]
            mark = bid if p.direction == LONG else bid + self.costs.spread(spec)
            rate = self.converter.rate(spec.quote, self.account_currency)
            equity += (mark - p.entry_price) * p.direction * p.units * rate + held.swap
        return equity
