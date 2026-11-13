# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Python bot for an FxPro account via the Spotware cTrader Open API. It streams quotes, downloads history, backtests strategies and runs them against a **demo** account (a live account only with an explicit opt-in). It has two families of strategies: intraday (day trading on five-minute bars, flat before each session closes) and daily (swing trading on D1 bars).

Order sending is **demo by default**. Live trading needs `CTRADER_LIVE=1` *and* `trade --allow-live` (the user asked for this opt-in), enforced in two places:
- `main.py trade` exits when `CTRADER_LIVE=1` without `--allow-live`, or `--allow-live` without `CTRADER_LIVE=1` (`check_live_trading`);
- `CTraderClient` refuses `market_order`, `close_position` and `amend_position_sltp` on the live host unless built with `allow_live_orders=True`, which only `main.py trade --allow-live` does.

Live runs default to their own state and journal files (`trade_paths`: `state/real_state.json`, `logs/real_journal.jsonl`) so demo risk state never mixes with the real account.

The client also refuses orders when the access token's `permissionScope` is view-only, and `trade --execute` then exits with status 1. Orders need a token issued with the `trading` scope.

Keep both guards and keep the opt-in explicit. Don't make live trading a default or add other ways around the guards.

`docs/RESEARCH.md` is the research report behind the strategies. Part I (daily) concludes that none of the daily strategies has a reliable edge on FX majors at retail costs today. Part II (intraday) finds a real but small intraday momentum effect that survives FxPro's costs only with `noise_breakout` on the Nasdaq 100 CFD. That net result is not statistically reliable and has been weak since the paper was published. Keep the code, README and report consistent with that. Don't present strategies as profitable.

## Commands

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt        # requirements.txt + pytest
python -m pytest                           # all tests; no broker connection needed
python main.py                             # quote stream (default command)
python main.py fetch|backtest|sweep|trade --help
python -m research.download && python -m research.run_research   # regenerate data/research/results.md
python -m research.intraday                # regenerate data/research/intraday_results.md (about 30 min)
```

There is no linter or build step.

Configuration comes from environment variables or a `.env` file (gitignored; loaded by `python-dotenv` in `config.py`):
- Required: `CTRADER_CLIENT_ID`, `CTRADER_CLIENT_SECRET`, `CTRADER_ACCESS_TOKEN`
- Optional:
  - `CTRADER_REFRESH_TOKEN`: lets the client renew an expired access token. `config.TokenStore` saves the renewed pair in `state/ctrader_token.json` (gitignored, mode 0600). The saved pair is used only while `CTRADER_ACCESS_TOKEN` is still the token it was renewed from.
  - `CTRADER_ACCOUNT_ID`: if empty, the first account whose live/demo flag matches the host is used.
  - `CTRADER_SYMBOL`: default `EURUSD` (`.env.example` uses `#USNDAQ100`; FxPro index CFD names start with `#`).
  - `CTRADER_SYMBOLS`: comma-separated; defaults to `CTRADER_SYMBOL`.
  - `CTRADER_LIVE`: `1` selects the live host; anything else selects demo.

Backtests and sweeps need no credentials. `--timeframe` defaults to the strategy's own (`default_timeframe`: M5 for intraday strategies, D1 for the others), and `--risk-profile standard|aggressive` picks a `RISK_PROFILES` preset whose single limits can still be overridden.

## Architecture

The layering is strategy → planner/risk → broker interface → cTrader client:
- `trading/` is pure Python and never imports Twisted or `ctrader_open_api`.
- `ctrader.py` and `ctrader_broker.py` are the only modules that talk to the API.
- `main.py` wires the two together.

### `trading/`

- `models.py`: `Bar` (open time, UTC), `BarWindow` (zero-copy view the backtester passes to strategies), `SymbolSpec` (volumes in units; `infer` knows FX pairs and the CFDs in `KNOWN_CFDS`), `Position`, `Signal`, `AccountState`.
- `sessions.py`: exchange sessions (`us`, `eu`, `uk`, `fx`) in local time via `zoneinfo`, so their UTC hours follow each exchange's daylight saving. `session_for(symbol)` infers the session from the symbol name.
- `strategies/`:
  - `Strategy.on_bar(symbol, bars, position, context) -> Signal | None`. It receives exactly `lookback` *closed* bars.
  - Parameters are frozen dataclasses; CLI overrides are coerced by type.
  - Register new strategies in `STRATEGIES` in `strategies/__init__.py`.
  - `intraday.py`: `IntradayStrategy` base (session bookkeeping, a per-day cache of completed sessions, flat before the close, closes positions left over from an earlier session), `NoiseBreakout` (`noise_breakout`) and `OpeningRangeBreakout` (`orb`). Their Params carry `timeframe`; `check_timeframe` makes the backtester and live trader refuse a mismatch, and `main.py` injects `--timeframe` via `with_timeframe`. `check_symbol` raises for a symbol with no known session: the backtester refuses it, the live trader ignores it.
- `planner.plan_actions`: turns a signal plus the current position into `CloseIntent`, `StopUpdate` or `EntryIntent`.
  - Every entry must carry a `stop_distance`.
  - Trailing stops only ever tighten.
- `risk.RISK_PROFILES`: `standard` (0.5% per trade) and `aggressive` (2% per trade) presets.
- `risk.RiskManager`:
  - sizes positions as risk ÷ stop distance;
  - enforces open-risk, per-currency, leverage and position caps;
  - holds the daily-loss and sticky drawdown kill switches (`state()`/`restore()` persist them).
- `backtest.Backtester`:
  - Signals at a bar's close fill at the next bar's open. Bars are treated as bid.
  - Stops are checked on the high/low, gaps fill at the open, and swaps accrue per rollover (triple Wednesday).
  - `CostModel` costs FX in pips and the CFDs in `spread_bp`/`slippage_bp` in basis points of price; symbols starting with `#` pay no commission (as at FxPro).
  - Positions still open at the end are closed so they appear in the trade list.
- `live.LiveTrader`: `tick()` is an `async` method.
  - It fetches bars only when a new bar should have closed, drops the forming bar, and merges D1 weekend stubs.
  - A new bar that closed more than `max_bar_age` ago is journaled as `stale_bar` and not acted on (`default_max_bar_age`: one bar for intraday timeframes; for D1 and longer, one bar plus three days, so a weekend does not count). This covers restarts and markets that have stopped trading, such as expired futures.
  - It runs strategies, then executes closes, then stop updates, then entries (sized with fresh equity).
  - It tracks the bot's open positions by id. When one is gone, whoever closed it, the end of the cycle journals `trade_result` with the realized P&L from `Broker.closed_trade`, and `closed_by` set to `strategy` or `broker` (stop, take profit, stop-out or manual close). If no closing deal turns up after `REPORT_ATTEMPTS` cycles, it journals the trade with an `error` instead.
  - It journals to JSON lines and saves `last_bar`, risk state and the tracked positions after each cycle. Bars where a strategy returns `None` (no opinion) are logged at DEBUG and not journaled.
  - One bad symbol never stops the others. Symbols a strategy cannot trade (`check_symbol`) are dropped at construction, and `ignore()` drops those the account does not offer (`CTraderBroker.load` returns them). Both are journaled as `symbol_ignored`. An exception from `on_bar` or the planner skips that strategy/symbol for the bar (`strategy_failed`). A failed bar fetch backs off per symbol. Only account-level failures retry the whole cycle.
  - Without `execute` it is a dry run.
- `broker.Broker`: the protocol `LiveTrader` uses. Methods return awaitables; `CTraderBroker` returns Deferreds.

### API layer

- `ctrader.py`: `CTraderClient`.
  - Authentication runs as an async flow on every (re)connect, then the `on_ready` callbacks run.
  - The flow always fetches the token's account list. This records `can_trade` from `permissionScope`, and checks that a configured `CTRADER_ACCOUNT_ID` belongs to the token and to the host's live/demo kind.
  - If the token is rejected (`TOKEN_ERRORS`), the flow first adopts a pair another run has saved, else sends `ProtoOARefreshTokenReq`, saves the result and retries once. Without a refresh token, or when the refresh fails, the error is fatal.
  - `_session` is bumped on every connect and disconnect. Login and `on_ready` failures from an older session are ignored, because restarting would drop the new connection. Failures in the current session reconnect after `RETRY_DELAY`.
  - `request()` returns a Deferred of the decoded payload. Each request gets a random `clientMsgId`. Error payloads raise `CTraderError`.
  - On a disconnect, unanswered requests fail at once with `DISCONNECTED`. The library forgets them silently. Account requests and orders sent while not logged in fail with `NOT_READY` and are never queued. `main.py trade` skips polls while `client.ready` is false.
  - `_on_message` handles only unsolicited events: spots, execution events, disconnects.
  - `market_order()` resolves when the matching `ORDER_FILLED` execution event arrives, keyed by `clientOrderId`.
- `ctrader_broker.py`: `CTraderBroker`.
  - Converts units, prices and stops (cTrader volumes are hundredths of a unit; prices and relative SL/TP are in 1/100000).
  - Builds `SymbolSpec` from `ProtoOASymbol`, fetches history in chunks below the 14,000-bar cap, and computes currency conversion rates.
  - Only reports positions labelled `<prefix>:<strategy>`.
  - The fill event arrives before the server attaches the relative stop, so `open_position` fills in the stop the order set.
  - `closed_trade` sums a position's closing deals (`ProtoOAClosePositionDetail`). The server rejects a deal search that ends in the future (`INCORRECT_BOUNDARIES`), so the search stops a few seconds before now.

To add a new API interaction, add a typed method to `CTraderClient` that sends the request with `self._account_request(...)` (or `self.request(...)`) and decodes the response in a callback. If the strategy layer needs it, expose it through `CTraderBroker` and the `Broker` protocol.

Everything runs on the Twisted reactor thread. `LiveTrader.tick()` is driven by a `LoopingCall` via `defer.ensureDeferred`. Don't block in handlers or strategies.

Library quirks:
- `ctrader_open_api` sends at most 5 queued messages per second, so request timeouts are generous (30 s).
- `ProtoOAGetTrendbarsReq.fromTimestamp` is required in this protocol version.

## Tests

The tests live in `tests/`. Helpers are in `tests/helpers.py`:
- `daily_bars`: weekday bars from a close series;
- `Scripted`: a strategy that returns pre-programmed signals keyed by bar time;
- `FixedCarry`.

Live-trader tests drive `tick()` against an in-memory `FakeBroker`. cTrader tests use real protobuf messages with a fake transport. Keep them network-free.

## Research

`research/` holds the downloaders (FRED, Yahoo, HistData one-minute bars aggregated to M5), research-only strategies (`RandomEntry`, `Rsi2MeanReversion`, `DollarCarry`, `RandomOrb`, `LateDayMomentum`), `run_research.py` (daily) and `intraday.py` (intraday; runs backtests in worker processes on session-only bars).

Downloaded data and results go to `data/research/`, which is gitignored. If you change strategy defaults, re-run the research and update the tables in `docs/RESEARCH.md`. The intraday defaults follow the published papers; don't tune them on the backtest.
