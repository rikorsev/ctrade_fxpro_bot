"""Live trading loop.

Runs the same strategies, planner and risk manager as the backtester against
a ``Broker``. Call ``tick()`` periodically (main.py does it every minute); it
only acts when a new bar has closed. Without ``execute`` it is a dry run: it
logs every decision and sends nothing.

Decisions, orders and errors are appended to a JSON-lines journal, and the
last processed bar plus the risk manager's kill-switch state are saved after
every cycle, so a restart neither repeats a bar nor forgets a halt.
"""
from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from trading.broker import Broker, OrderRequest
from trading.data import merge_weekend_bars
from trading.models import AccountState, Bar, Position, side_name, timeframe_delta
from trading.planner import CloseIntent, EntryIntent, StopUpdate, plan_actions
from trading.risk import Exposure, RiskManager, position_exposure
from trading.strategies import MarketContext, Strategy

log = logging.getLogger(__name__)

FIRST_RETRY = timedelta(minutes=1)
MAX_RETRY = timedelta(hours=1)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class LiveConfig:
    symbols: tuple[str, ...]
    timeframe: str = "D1"
    execute: bool = False
    settle: timedelta = timedelta(seconds=15)  # wait after a bar closes before asking for it
    state_path: Path | None = None
    journal_path: Path | None = None


class LiveTrader:
    def __init__(
        self,
        broker: Broker,
        strategies: Sequence[Strategy],
        risk: RiskManager,
        config: LiveConfig,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        names = [s.name for s in strategies]
        if not names or len(set(names)) != len(names):
            raise ValueError("need at least one strategy, with distinct names")
        self.broker = broker
        self.strategies = list(strategies)
        self.risk = risk
        self.config = config
        self.clock = clock
        self.bar_length = timeframe_delta(config.timeframe)
        self.lookback = max(s.lookback for s in strategies)
        self.last_bar: dict[str, datetime] = {}
        self._next_check: dict[str, datetime] = {}
        self._retry: dict[str, timedelta] = {}
        self._busy = False
        self._load_state()

    async def tick(self) -> bool:
        """Run a trading cycle if a new bar has closed on any symbol. Returns True if one ran."""
        if self._busy:
            return False
        self._busy = True
        try:
            now = self.clock()
            fresh = {}
            for symbol in self.config.symbols:
                due = self._next_check.get(symbol)
                if due is not None and now < due:
                    continue
                try:
                    bars, forming_close = await self._closed_bars(symbol, now)
                except Exception as exc:  # network or broker error: try again later
                    log.warning("%s: could not fetch bars: %s", symbol, exc)
                    bars, forming_close = [], None
                last = self.last_bar.get(symbol)
                if bars and (last is None or bars[-1].time > last):
                    fresh[symbol] = bars
                if symbol in fresh or (forming_close is not None and forming_close > now):
                    self._retry.pop(symbol, None)
                    upcoming = forming_close or bars[-1].time + 2 * self.bar_length
                    self._next_check[symbol] = upcoming + self.config.settle
                else:
                    delay = min(self._retry.get(symbol, FIRST_RETRY / 2) * 2, MAX_RETRY)
                    self._retry[symbol] = delay
                    self._next_check[symbol] = now + delay
            if not fresh:
                return False
            try:
                await self._cycle(fresh, now)
            except Exception:
                # Usually a dropped connection: retry these bars soon instead of waiting a whole bar.
                log.exception("Trading cycle failed; retrying in %s", FIRST_RETRY)
                for symbol in fresh:
                    self._next_check[symbol] = now + FIRST_RETRY
                return False
            return True
        finally:
            self._busy = False

    async def _closed_bars(self, symbol: str, now: datetime) -> tuple[list[Bar], datetime | None]:
        count = self.lookback + self.lookback // 4 + 10  # room for weekend stubs that get merged
        raw = await self.broker.bars(symbol, self.config.timeframe, count)
        closed = [b for b in raw if b.time + self.bar_length <= now]
        forming_close = None
        if raw and raw[-1].time + self.bar_length > now:
            forming_close = raw[-1].time + self.bar_length
        if self.config.timeframe == "D1":
            closed = merge_weekend_bars(closed)
        return closed, forming_close

    async def _cycle(self, fresh: dict[str, list[Bar]], now: datetime) -> None:
        account = await self.broker.account()
        positions = await self.broker.positions()
        halt = self.risk.update(account.equity, now)
        if halt:
            log.warning("Risk halt: %s", halt)
            self._journal("halt", reason=halt, equity=account.equity)
        held = self._index(positions)
        context = MarketContext(time=now, carry=self.broker)

        closes: list[CloseIntent] = []
        updates: list[StopUpdate] = []
        entries: list[EntryIntent] = []
        for symbol, bars in fresh.items():
            for strategy in self.strategies:
                window = bars[-strategy.lookback:]
                if len(window) < strategy.lookback:
                    log.warning("%s: %s needs %d bars, broker returned %d", symbol, strategy.name,
                                strategy.lookback, len(window))
                    continue
                position = held.get((strategy.name, symbol))
                signal = strategy.on_bar(symbol, window, position, context)
                actions = plan_actions(strategy.name, symbol, signal, position)
                log.info("%s %s bar %s close %g: %s%s", strategy.name, symbol, f"{window[-1].time:%Y-%m-%d %H:%M}",
                         window[-1].close, "no opinion" if signal is None else
                         f"{side_name(signal.direction)} ({signal.reason})",
                         "" if not actions else " -> " + ", ".join(type(a).__name__ for a in actions))
                self._journal("signal", strategy=strategy.name, symbol=symbol, bar=window[-1].time,
                              close=window[-1].close, direction=None if signal is None else signal.direction,
                              reason=None if signal is None else signal.reason,
                              actions=[type(a).__name__ for a in actions])
                for action in actions:
                    if isinstance(action, CloseIntent):
                        closes.append(action)
                    elif isinstance(action, StopUpdate):
                        updates.append(action)
                    else:
                        entries.append(action)

        for close in closes:
            await self._close(close)
        for update in updates:
            await self._amend(update)
        if entries:
            if closes and self.config.execute:
                account = await self.broker.account()
                positions = await self.broker.positions()
            closing = {id(c.position) for c in closes}
            positions = [p for p in positions if id(p) not in closing]
            prices = {s: b[-1].close for s, b in fresh.items()}
            for intent in entries:
                opened = await self._enter(intent, account, positions, prices)
                if opened is not None:
                    positions.append(opened)

        self.last_bar.update({s: b[-1].time for s, b in fresh.items()})
        self._save_state()

    async def _enter(
        self,
        intent: EntryIntent,
        account: AccountState,
        positions: list[Position],
        prices: dict[str, float],
    ) -> Position | None:
        spec = self.broker.symbol_spec(intent.symbol)
        price = prices[intent.symbol]
        try:
            rate = await self.broker.conversion_rate(spec.quote, account.currency)
            exposures = [await self._exposure(p, account, prices) for p in positions]
        except Exception as exc:
            log.error("%s %s: cannot size entry: %s", intent.strategy, intent.symbol, exc)
            self._journal("entry_failed", strategy=intent.strategy, symbol=intent.symbol, error=str(exc))
            return None
        decision = self.risk.size(
            equity=account.equity,
            spec=spec,
            direction=intent.direction,
            price=price,
            stop_distance=intent.stop_distance,
            quote_to_account=rate,
            exposures=exposures,
        )
        side = side_name(intent.direction)
        if decision.units <= 0:
            log.info("Skip %s %s %s: %s", intent.strategy, side, intent.symbol, decision.reason)
            self._journal("entry_skipped", strategy=intent.strategy, symbol=intent.symbol, side=side,
                          reason=decision.reason)
            return None

        order = OrderRequest(
            symbol=intent.symbol,
            strategy=intent.strategy,
            direction=intent.direction,
            units=decision.units,
            stop_distance=intent.stop_distance,
            take_profit_distance=intent.take_profit_distance,
            comment=intent.reason[:100],
        )
        details = dict(strategy=intent.strategy, symbol=intent.symbol, side=side, units=decision.units,
                       stop_distance=intent.stop_distance, risk=round(decision.risk, 2),
                       sizing=decision.reason, reason=intent.reason)
        if not self.config.execute:
            log.info("DRY RUN: would open %s %s %g units, stop %g away, risking %.2f %s (%s)", side,
                     intent.symbol, decision.units, intent.stop_distance, decision.risk, account.currency,
                     decision.reason)
            self._journal("dry_run_open", **details)
            return None
        try:
            position = await self.broker.open_position(order)
        except Exception as exc:
            log.error("Open %s %s failed: %s", side, intent.symbol, exc)
            self._journal("open_failed", error=str(exc), **details)
            return None
        log.info("Opened %s %s %g units at %g, stop %s (position %s)", side, intent.symbol, position.units,
                 position.entry_price, position.stop_loss, position.position_id)
        self._journal("opened", position_id=position.position_id, price=position.entry_price,
                      stop_loss=position.stop_loss, **details)
        return position

    async def _exposure(self, position: Position, account: AccountState, prices: dict[str, float]) -> Exposure:
        spec = self.broker.symbol_spec(position.symbol)
        rate = await self.broker.conversion_rate(spec.quote, account.currency)
        price = prices.get(position.symbol, position.entry_price)
        return position_exposure(position, spec, price, rate, self.risk.config.risk_per_trade * account.equity)

    async def _close(self, action: CloseIntent) -> None:
        p = action.position
        details = dict(strategy=p.strategy, symbol=p.symbol, side=side_name(p.direction), units=p.units,
                       position_id=p.position_id, reason=action.reason)
        if not self.config.execute:
            log.info("DRY RUN: would close %s %s position %s (%s)", side_name(p.direction), p.symbol,
                     p.position_id, action.reason)
            self._journal("dry_run_close", **details)
            return
        try:
            await self.broker.close_position(p)
        except Exception as exc:
            log.error("Close %s position %s failed: %s", p.symbol, p.position_id, exc)
            self._journal("close_failed", error=str(exc), **details)
            return
        log.info("Closed %s %s position %s (%s)", side_name(p.direction), p.symbol, p.position_id, action.reason)
        self._journal("closed", **details)

    async def _amend(self, action: StopUpdate) -> None:
        p = action.position
        stop = self.broker.symbol_spec(p.symbol).round_price(action.stop_loss)
        details = dict(strategy=p.strategy, symbol=p.symbol, position_id=p.position_id,
                       old_stop=p.stop_loss, new_stop=stop)
        if not self.config.execute:
            log.info("DRY RUN: would move %s stop from %s to %g", p.symbol, p.stop_loss, stop)
            self._journal("dry_run_stop", **details)
            return
        try:
            await self.broker.amend_stop(p, stop)
        except Exception as exc:
            log.error("Moving %s stop failed: %s", p.symbol, exc)
            self._journal("stop_failed", error=str(exc), **details)
            return
        log.info("Moved %s stop from %s to %g", p.symbol, p.stop_loss, stop)
        self._journal("stop_moved", **details)

    def _index(self, positions: Sequence[Position]) -> dict[tuple[str, str], Position]:
        known = {s.name for s in self.strategies}
        held: dict[tuple[str, str], Position] = {}
        for p in positions:
            key = (p.strategy, p.symbol)
            if p.strategy not in known:
                log.warning("Position %s on %s belongs to strategy %r, which is not running; only its broker-side "
                            "stop protects it", p.position_id, p.symbol, p.strategy)
            elif key in held:
                log.warning("Several %s positions on %s; managing %s only. Close the others manually.",
                            p.strategy, p.symbol, held[key].position_id)
            else:
                held[key] = p
        return held

    def _load_state(self) -> None:
        path = self.config.state_path
        if path is None or not path.exists():
            return
        state = json.loads(path.read_text())
        self.last_bar = {s: datetime.fromisoformat(t) for s, t in state.get("last_bar", {}).items()}
        self.risk.restore(state.get("risk", {}))
        log.info("Restored state from %s (last bars: %s)", path, ", ".join(
            f"{s} {t:%Y-%m-%d %H:%M}" for s, t in sorted(self.last_bar.items())) or "none")

    def _save_state(self) -> None:
        path = self.config.state_path
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        state = {"last_bar": {s: t.isoformat() for s, t in self.last_bar.items()}, "risk": self.risk.state()}
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(state, indent=2))
        os.replace(tmp, path)

    def _journal(self, event: str, **fields: Any) -> None:
        path = self.config.journal_path
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {"time": self.clock().isoformat(), "event": event,
                  "mode": "execute" if self.config.execute else "dry-run", **fields}
        with open(path, "a") as f:
            f.write(json.dumps(record, default=str) + "\n")
