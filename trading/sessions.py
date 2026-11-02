"""Trading sessions in exchange-local time.

Intraday strategies trade the cash session of the market behind a CFD, even
though the CFD itself quotes nearly around the clock. Sessions are defined in
local time, so their UTC hours follow each exchange's daylight-saving switch
(the US and Europe change clocks on different dates).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class Session:
    name: str
    tz: str
    open: time
    close: time

    def bounds(self, day: date) -> tuple[datetime, datetime]:
        """UTC open and close of the session on the local date ``day``."""
        return _bounds(self, day)

    def day_of(self, when: datetime) -> date:
        """The local date of a UTC time."""
        return when.astimezone(_zone(self.tz)).date()

    def minutes(self) -> int:
        return (datetime.combine(date.min, self.close) - datetime.combine(date.min, self.open)).seconds // 60


SESSIONS = {
    "us": Session("us", "America/New_York", time(9, 30), time(16, 0)),  # NYSE / Nasdaq cash session
    "eu": Session("eu", "Europe/Berlin", time(9, 0), time(17, 30)),  # Xetra (DAX), Euronext
    "uk": Session("uk", "Europe/London", time(8, 0), time(16, 30)),  # London Stock Exchange
    "fx": Session("fx", "Europe/London", time(8, 0), time(17, 0)),  # London open to the London/New York overlap
}

_US = ("SPX", "US500", "NDAQ", "US100", "NAS100", "USTEC", "US30", "DJ30", "US2000")
_EU = ("GER", "DE40", "DAX", "EUR50", "EURO50", "STOXX", "FRANCE", "FRA40", "SPAIN", "HOLLAND", "NETH")
_UK = ("UK100", "FTSE")


def session_for(symbol: str, name: str = "auto") -> Session:
    """The session to trade ``symbol`` in: ``name`` if given, otherwise inferred from the symbol."""
    if name != "auto":
        try:
            return SESSIONS[name]
        except KeyError:
            raise ValueError(f"Unknown session {name!r}; use auto or one of {', '.join(SESSIONS)}") from None
    upper = symbol.upper()
    for key, markers in (("us", _US), ("eu", _EU), ("uk", _UK)):
        if any(marker in upper for marker in markers):
            return SESSIONS[key]
    plain = upper.lstrip("#")
    if len(plain) == 6 and plain.isalpha():  # FX pairs and metals such as XAUUSD
        return SESSIONS["fx"]
    raise ValueError(
        f"no known trading session for {symbol!r}. Sessions are known for US, German and UK index CFDs, "
        f"FX pairs and metals; trade one of those or pass --param session=" + "|".join(SESSIONS)
    )


@lru_cache(maxsize=None)
def _zone(name: str) -> ZoneInfo:
    return ZoneInfo(name)


@lru_cache(maxsize=65536)
def _bounds(session: Session, day: date) -> tuple[datetime, datetime]:
    zone = _zone(session.tz)
    start = datetime.combine(day, session.open, zone).astimezone(timezone.utc)
    end = datetime.combine(day, session.close, zone).astimezone(timezone.utc)
    if end <= start:
        end += timedelta(days=1)
    return start, end
