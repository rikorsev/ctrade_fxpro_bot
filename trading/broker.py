"""The interface the live trader uses to reach a broker.

Methods that touch the network return awaitables (Twisted Deferreds for the
cTrader implementation), so the live trader can be driven by any event loop
and tested against an in-memory fake.
"""
from __future__ import annotations

from collections.abc import Awaitable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from trading.models import AccountState, Bar, Position, SymbolSpec


@dataclass(frozen=True)
class OrderRequest:
    """A market order with a protective stop placed relative to the fill price."""

    symbol: str
    strategy: str
    direction: int
    units: float
    stop_distance: float
    take_profit_distance: float | None = None
    comment: str = ""


class Broker(Protocol):
    def symbol_spec(self, symbol: str) -> SymbolSpec:
        """Contract details for a symbol loaded at start-up."""

    def bars(self, symbol: str, timeframe: str, count: int) -> Awaitable[list[Bar]]:
        """The latest ``count`` bars, oldest first. The last one may still be forming."""

    def account(self) -> Awaitable[AccountState]: ...

    def positions(self) -> Awaitable[list[Position]]:
        """Open positions opened by this bot (other positions on the account are ignored)."""

    def conversion_rate(self, from_ccy: str, to_ccy: str) -> Awaitable[float]: ...

    def annual_carry(self, symbol: str, direction: int, price: float, time: datetime) -> float | None:
        """Net swap carry in % of notional per year (see trading.strategies.CarrySource)."""

    def open_position(self, order: OrderRequest) -> Awaitable[Position]: ...

    def close_position(self, position: Position) -> Awaitable[None]: ...

    def amend_stop(self, position: Position, stop_loss: float) -> Awaitable[None]: ...
