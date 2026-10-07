"""Position sizing, exposure limits and kill switches.

Shared by the backtester and the live trader so that a backtest exercises
exactly the limits the bot will enforce on the account.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import date, datetime
from typing import Any

from trading.models import LONG, Position, SymbolSpec


@dataclass(frozen=True)
class RiskConfig:
    risk_per_trade: float = 0.005  # equity fraction lost if a new position's stop is hit
    max_open_risk: float = 0.03  # cap on the summed stop risk of all open positions
    max_currency_risk: float = 0.015  # cap on stop risk stacked on one currency in one direction
    max_positions: int = 10
    max_leverage: float = 10.0  # total notional / equity
    max_daily_loss: float = 0.03  # block entries for the rest of the UTC day after this loss
    max_drawdown: float = 0.20  # block entries after this drawdown from peak, until reset

    def __post_init__(self) -> None:
        for name in ("risk_per_trade", "max_open_risk", "max_currency_risk", "max_daily_loss", "max_drawdown"):
            value = getattr(self, name)
            if not 0 < value < 1:
                raise ValueError(f"{name} must be between 0 and 1, got {value}")
        if self.risk_per_trade > self.max_open_risk:
            raise ValueError("risk_per_trade cannot exceed max_open_risk")
        if self.max_positions < 1 or self.max_leverage <= 0:
            raise ValueError("max_positions must be >= 1 and max_leverage > 0")


@dataclass(frozen=True)
class Exposure:
    """An open (or about to be opened) position as the risk manager sees it, in account currency."""

    base: str
    quote: str
    direction: int
    risk: float  # loss if the stop is hit, never negative
    notional: float

    def loads(self, currency: str, direction: int) -> bool:
        """True if this position is long (direction=1) or short (-1) ``currency``."""
        return (self.base == currency and self.direction == direction) or (
            self.quote == currency and self.direction == -direction
        )


def position_exposure(
    position: Position,
    spec: SymbolSpec,
    price: float,
    quote_to_account: float,
    risk_without_stop: float,
) -> Exposure:
    """Exposure of an open position; its risk is the distance from entry to the current stop."""
    if position.stop_loss is None:
        risk = risk_without_stop
    else:
        distance = (position.entry_price - position.stop_loss) * position.direction
        risk = max(distance, 0.0) * position.units * quote_to_account
    return Exposure(
        base=spec.base,
        quote=spec.quote,
        direction=position.direction,
        risk=risk,
        notional=position.units * price * quote_to_account,
    )


@dataclass(frozen=True)
class SizeDecision:
    units: float
    risk: float = 0.0
    notional: float = 0.0
    reason: str = ""


class RiskManager:
    def __init__(self, config: RiskConfig | None = None) -> None:
        self.config = config or RiskConfig()
        self.peak_equity: float | None = None
        self.last_equity: float | None = None
        self.day: date | None = None
        self.day_start_equity: float | None = None
        self.daily_halt = False
        self.drawdown_halt = False

    def update(self, equity: float, now: datetime) -> str | None:
        """Record current equity. Returns why new entries are blocked, or None.

        The day's loss is measured from the last equity seen before the UTC day
        began, so it also works when updates only happen once a day (daily bars).
        """
        if self.day != now.date():
            self.day = now.date()
            self.day_start_equity = self.last_equity if self.last_equity is not None else equity
            self.daily_halt = False
        self.last_equity = equity
        if self.peak_equity is None or equity > self.peak_equity:
            self.peak_equity = equity
        if equity <= self.peak_equity * (1 - self.config.max_drawdown):
            self.drawdown_halt = True
        if equity <= self.day_start_equity * (1 - self.config.max_daily_loss):
            self.daily_halt = True
        return self.halt_reason

    @property
    def halt_reason(self) -> str | None:
        if self.drawdown_halt:
            return (
                f"max drawdown {self.config.max_drawdown:.0%} from peak {self.peak_equity:,.2f} reached; "
                "new entries blocked until the risk state is reset"
            )
        if self.daily_halt:
            return (
                f"daily loss limit {self.config.max_daily_loss:.0%} from {self.day_start_equity:,.2f} reached; "
                "new entries blocked until tomorrow (UTC)"
            )
        return None

    def reset_drawdown(self, equity: float | None = None) -> None:
        """Clear a drawdown halt; the peak restarts from ``equity`` (or the next update)."""
        self.drawdown_halt = False
        self.peak_equity = equity

    def size(
        self,
        *,
        equity: float,
        spec: SymbolSpec,
        direction: int,
        price: float,
        stop_distance: float,
        quote_to_account: float,
        exposures: Sequence[Exposure],
    ) -> SizeDecision:
        """Units for a new position risking ``risk_per_trade`` of equity at its stop, within all limits."""
        c = self.config
        if self.halt_reason:
            return SizeDecision(0.0, reason=self.halt_reason)
        if equity <= 0 or price <= 0 or stop_distance <= 0 or quote_to_account <= 0:
            return SizeDecision(0.0, reason="invalid sizing inputs")
        if len(exposures) >= c.max_positions:
            return SizeDecision(0.0, reason=f"max positions ({c.max_positions}) reached")

        budget = c.risk_per_trade * equity
        limits = [f"risk/trade {c.risk_per_trade:.2%}"]
        open_room = c.max_open_risk * equity - sum(e.risk for e in exposures)
        if open_room < budget:
            budget, limits = open_room, [f"open risk cap {c.max_open_risk:.1%}"]
        for currency, sign in ((spec.base, direction), (spec.quote, -direction)):
            used = sum(e.risk for e in exposures if e.loads(currency, sign))
            room = c.max_currency_risk * equity - used
            if room < budget:
                side = "long" if sign == LONG else "short"
                budget, limits = room, [f"{side} {currency} risk cap {c.max_currency_risk:.1%}"]
        if budget <= 0:
            return SizeDecision(0.0, reason=f"no risk budget left ({limits[0]})")

        units = budget / (stop_distance * quote_to_account)
        notional_per_unit = price * quote_to_account
        leverage_room = c.max_leverage * equity - sum(e.notional for e in exposures)
        if leverage_room < units * notional_per_unit:
            units = max(leverage_room, 0.0) / notional_per_unit
            limits = [f"leverage cap {c.max_leverage:g}x"]

        units = spec.normalize_units(units)
        if units <= 0:
            return SizeDecision(
                0.0, reason=f"size below broker minimum of {spec.min_volume:g} units ({limits[0]})"
            )
        return SizeDecision(
            units=units,
            risk=units * stop_distance * quote_to_account,
            notional=units * notional_per_unit,
            reason=f"sized by {limits[0]}",
        )

    def state(self) -> dict[str, Any]:
        """Serializable state, so kill switches survive a restart of the live bot."""
        return {
            "config": asdict(self.config),
            "peak_equity": self.peak_equity,
            "last_equity": self.last_equity,
            "day": self.day.isoformat() if self.day else None,
            "day_start_equity": self.day_start_equity,
            "daily_halt": self.daily_halt,
            "drawdown_halt": self.drawdown_halt,
        }

    def restore(self, state: dict[str, Any]) -> None:
        self.peak_equity = state.get("peak_equity")
        self.last_equity = state.get("last_equity")
        self.day = date.fromisoformat(state["day"]) if state.get("day") else None
        self.day_start_equity = state.get("day_start_equity")
        self.daily_halt = bool(state.get("daily_halt"))
        self.drawdown_halt = bool(state.get("drawdown_halt"))
