from datetime import datetime, timezone

import pytest

from trading.models import FLAT, LONG, SHORT, Position, Signal
from trading.planner import CloseIntent, EntryIntent, StopUpdate, plan_actions

NOW = datetime(2025, 1, 1, tzinfo=timezone.utc)


def long_position(stop=1.0950):
    return Position("EURUSD", "s", LONG, 10_000, 1.1000, NOW, stop_loss=stop)


def test_no_signal_or_same_direction_does_nothing():
    assert plan_actions("s", "EURUSD", None, long_position()) == []
    assert plan_actions("s", "EURUSD", Signal(LONG), long_position()) == []
    assert plan_actions("s", "EURUSD", Signal(FLAT), None) == []


def test_trailing_stop_only_tightens():
    (update,) = plan_actions("s", "EURUSD", Signal(LONG, trailing_stop=1.0970), long_position())
    assert isinstance(update, StopUpdate) and update.stop_loss == 1.0970
    assert plan_actions("s", "EURUSD", Signal(LONG, trailing_stop=1.0900), long_position()) == []


def test_reversal_closes_then_enters():
    close, entry = plan_actions("s", "EURUSD", Signal(SHORT, stop_distance=0.01, reason="flip"), long_position())
    assert isinstance(close, CloseIntent) and isinstance(entry, EntryIntent)
    assert entry.direction == SHORT and entry.stop_distance == 0.01 and entry.reason == "flip"
    (only_close,) = plan_actions("s", "EURUSD", Signal(FLAT), long_position())
    assert isinstance(only_close, CloseIntent)


def test_entries_require_a_stop():
    with pytest.raises(ValueError, match="protective stop"):
        plan_actions("s", "EURUSD", Signal(LONG), None)
