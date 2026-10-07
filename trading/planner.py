"""Turns a strategy signal plus the current position into concrete actions.

The backtester and the live trader both use this, so they make identical
decisions; they only differ in how actions are executed.
"""
from __future__ import annotations

from dataclasses import dataclass

from trading.models import FLAT, LONG, Position, Signal


@dataclass(frozen=True)
class EntryIntent:
    strategy: str
    symbol: str
    direction: int
    stop_distance: float
    take_profit_distance: float | None
    reason: str


@dataclass(frozen=True)
class CloseIntent:
    position: Position
    reason: str


@dataclass(frozen=True)
class StopUpdate:
    position: Position
    stop_loss: float
    reason: str


Action = EntryIntent | CloseIntent | StopUpdate


def tightens(position: Position, stop_loss: float) -> bool:
    if position.stop_loss is None:
        return True
    if position.direction == LONG:
        return stop_loss > position.stop_loss
    return stop_loss < position.stop_loss


def plan_actions(strategy: str, symbol: str, signal: Signal | None, position: Position | None) -> list[Action]:
    if signal is None:
        return []
    if position is not None and signal.direction == position.direction:
        if signal.trailing_stop is not None and tightens(position, signal.trailing_stop):
            return [StopUpdate(position, signal.trailing_stop, signal.reason)]
        return []

    actions: list[Action] = []
    if position is not None:
        actions.append(CloseIntent(position, signal.reason))
    if signal.direction != FLAT:
        if signal.stop_distance is None or signal.stop_distance <= 0:
            raise ValueError(f"{strategy} {symbol}: entry signal without a protective stop")
        actions.append(
            EntryIntent(
                strategy=strategy,
                symbol=symbol,
                direction=signal.direction,
                stop_distance=signal.stop_distance,
                take_profit_distance=signal.take_profit_distance,
                reason=signal.reason,
            )
        )
    return actions
