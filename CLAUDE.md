# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Python bot for an FxPro account via the Spotware cTrader Open API. It streams quotes, downloads history, backtests strategies and runs them against a **demo** account.

Order sending is **demo-only by design**, enforced in two places:
- `main.py trade` exits when `CTRADER_LIVE=1`;
- `CTraderClient` refuses `market_order`, `close_position` and `amend_position_sltp` on the live host.

Keep both guards. Don't add a live-trading path unless the user explicitly asks.

`docs/RESEARCH.md` is the research report behind the strategies. Its conclusion is that none of them has a reliable edge on FX majors at retail costs today. Keep the code, README and report consistent with that. Don't present strategies as profitable.

## Commands

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt        # requirements.txt + pytest
python -m pytest                           # all tests; no broker connection needed
python main.py                             # quote stream (default command)
python main.py fetch|backtest|sweep|trade --help
python -m research.download && python -m research.run_research   # regenerate data/research/results.md
```

There is no linter or build step.

Configuration comes from environment variables or a `.env` file (gitignored; loaded by `python-dotenv` in `config.py`):
- Required: `CTRADER_CLIENT_ID`, `CTRADER_CLIENT_SECRET`, `CTRADER_ACCESS_TOKEN`
- Optional:
  - `CTRADER_ACCOUNT_ID`: if empty, the first account whose live/demo flag matches the host is used.
  - `CTRADER_SYMBOL`: default `EURUSD`.
  - `CTRADER_SYMBOLS`: comma-separated; defaults to `CTRADER_SYMBOL`.
  - `CTRADER_LIVE`: `1` selects the live host; anything else selects demo.

Backtests and sweeps need no credentials.

## Architecture

The layering is strategy → planner/risk → broker interface → cTrader client:
- `trading/` is pure Python and never imports Twisted or `ctrader_open_api`.
- `ctrader.py` and `ctrader_broker.py` are the only modules that talk to the API.
- `main.py` wires the two together.

### `trading/`

- `models.py`: `Bar` (open time, UTC), `SymbolSpec` (volumes in units), `Position`, `Signal`, `AccountState`.
- `strategies/`:
  - `Strategy.on_bar(symbol, bars, position, context) -> Signal | None`. It receives exactly `lookback` *closed* bars.
  - Parameters are frozen dataclasses; CLI overrides are coerced by type.
  - Register new strategies in `STRATEGIES` in `strategies/__init__.py`.
- `planner.plan_actions`: turns a signal plus the current position into `CloseIntent`, `StopUpdate` or `EntryIntent`.
  - Every entry must carry a `stop_distance`.
  - Trailing stops only ever tighten.
- `risk.RiskManager`:
  - sizes positions as risk ÷ stop distance;
  - enforces open-risk, per-currency, leverage and position caps;
  - holds the daily-loss and sticky drawdown kill switches (`state()`/`restore()` persist them).
- `backtest.Backtester`:
  - Signals at a bar's close fill at the next bar's open. Bars are treated as bid.
  - Stops are checked on the high/low, gaps fill at the open, and swaps accrue per rollover (triple Wednesday).
  - Positions still open at the end are closed so they appear in the trade list.
- `live.LiveTrader`: `tick()` is an `async` method.
  - It fetches bars only when a new bar should have closed, drops the forming bar, and merges D1 weekend stubs.
  - It runs strategies, then executes closes, then stop updates, then entries (sized with fresh equity).
  - It journals to JSON lines and saves `last_bar` and risk state after each cycle.
  - Without `execute` it is a dry run.
- `broker.Broker`: the protocol `LiveTrader` uses. Methods return awaitables; `CTraderBroker` returns Deferreds.

### API layer

- `ctrader.py`: `CTraderClient`.
  - Authentication runs as an async flow on every (re)connect, then the `on_ready` callbacks run.
  - `request()` returns a Deferred of the decoded payload. Responses are matched by `clientMsgId`, which the library handles. Error payloads raise `CTraderError`.
  - `_on_message` handles only unsolicited events: spots, execution events, disconnects.
  - `market_order()` resolves when the matching `ORDER_FILLED` execution event arrives, keyed by `clientOrderId`.
- `ctrader_broker.py`: `CTraderBroker`.
  - Converts units, prices and stops (cTrader volumes are hundredths of a unit; prices and relative SL/TP are in 1/100000).
  - Builds `SymbolSpec` from `ProtoOASymbol`, fetches history in chunks below the 14,000-bar cap, and computes currency conversion rates.
  - Only reports positions labelled `<prefix>:<strategy>`.

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

`research/` holds the downloaders (FRED, Yahoo), research-only strategies (`RandomEntry`, `Rsi2MeanReversion`, `DollarCarry`) and `run_research.py`.

Downloaded data and results go to `data/research/`, which is gitignored. If you change strategy defaults, re-run the research and update the tables in `docs/RESEARCH.md`.
