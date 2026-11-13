import argparse

import pytest

import main
from trading.models import BarWindow
from trading.risk import RISK_PROFILES


def namespace(**kwargs):
    defaults = dict(risk_profile="standard")
    defaults.update({name: None for name in main.RISK_FIELDS})
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


def test_timeframe_defaults_to_the_strategys_own():
    assert main.resolve_timeframe(None, ["noise_breakout"]) == "M5"
    assert main.resolve_timeframe(None, ["carry"]) == "D1"
    assert main.resolve_timeframe("H4", ["donchian"]) == "H4"
    with pytest.raises(ValueError):
        main.resolve_timeframe(None, ["noise_breakout", "carry"])


def test_build_strategies_passes_the_timeframe_to_intraday_strategies():
    (s,) = main.build_strategies(["noise_breakout"], ["band=1.2"], "M15")
    assert s.params.timeframe == "M15" and s.params.band == 1.2
    with pytest.raises(ValueError):
        main.build_strategies(["orb"], None, "D1")


def test_risk_profile_with_overrides():
    assert main.risk_config(namespace()) == RISK_PROFILES["standard"]
    aggressive = main.risk_config(namespace(risk_profile="aggressive", max_daily_loss=0.1))
    assert aggressive.risk_per_trade == RISK_PROFILES["aggressive"].risk_per_trade
    assert aggressive.max_daily_loss == 0.1


def test_bar_window_is_a_view():
    data = list(range(10))
    w = BarWindow(data, 3, 8)
    assert len(w) == 5 and w[0] == 3 and w[-1] == 7
    assert w[1:3] == [4, 5] and w[-2:] == [6, 7] and w[::2] == [3, 5, 7]
    assert list(w) == [3, 4, 5, 6, 7] and list(reversed(w)) == [7, 6, 5, 4, 3]
    with pytest.raises(IndexError):
        w[5]


def test_live_trading_needs_both_ctrader_live_and_allow_live():
    main.check_live_trading(False, False)
    main.check_live_trading(True, True)
    with pytest.raises(SystemExit):
        main.check_live_trading(True, False)
    with pytest.raises(SystemExit):
        main.check_live_trading(False, True)


def test_live_and_demo_keep_separate_state_files():
    args = main.build_parser().parse_args(["trade", "--strategy", "carry"])
    assert not args.allow_live
    assert [str(p) for p in main.trade_paths(args, False)] == ["state/live_state.json", "logs/journal.jsonl"]
    assert [str(p) for p in main.trade_paths(args, True)] == ["state/real_state.json", "logs/real_journal.jsonl"]
    args = main.build_parser().parse_args(["trade", "--strategy", "carry", "--state", "s.json", "--journal", "j.jsonl"])
    assert [str(p) for p in main.trade_paths(args, True)] == ["s.json", "j.jsonl"]
