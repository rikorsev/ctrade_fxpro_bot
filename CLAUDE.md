# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Python starter bot for an FxPro account via the Spotware cTrader Open API. It is **read-only by design**: it authenticates, finds one symbol, subscribes to live spot quotes and prints bid/ask. There is no order-placement code yet. Don't add any unless asked, and keep it demo-only (`CTRADER_LIVE=0`) when you do.

## Commands

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python main.py
```

There is no test suite, linter or build step configured.

Configuration comes from environment variables or a `.env` file (loaded by `python-dotenv` in `config.py`):
- Required: `CTRADER_CLIENT_ID`, `CTRADER_CLIENT_SECRET`, `CTRADER_ACCESS_TOKEN`
- Optional: `CTRADER_ACCOUNT_ID` (if empty, the first account for the token is used), `CTRADER_SYMBOL` (default `EURUSD`), `CTRADER_LIVE` (`1` selects the live host; anything else selects demo)

## Architecture

- `config.py`: builds a frozen `Config` dataclass from env vars and fails fast if a required one is missing.
- `ctrader.py`: `CTraderClient` wraps `ctrader_open_api.Client` (Twisted, TCP/protobuf). It picks the demo or live host from `live` and passes quotes to an injected `on_quote(symbol, bid, ask)` callback.
- `main.py`: loads the config, wires up `on_quote`, calls `client.start()`, then blocks on `reactor.run()`.

The client is an **event-driven state machine** inside the single `_on_message` dispatcher, keyed on `message.payloadType`. Each response triggers the next request:

```
connect → ApplicationAuthReq → ApplicationAuthRes
  → (no account_id) GetAccountListByAccessTokenReq → Res → pick first account
  → AccountAuthReq → AccountAuthRes
  → SymbolsListReq → SymbolsListRes → match CTRADER_SYMBOL → SubscribeSpotsReq
  → ProtoOASpotEvent (repeated) → on_quote
```

To add a new API interaction:
1. Send the request from the handler of the response it depends on.
2. Add a `payloadType` branch to `_on_message`, using `Protobuf.extract(message)` to decode it.
3. Attach `_on_error` to the returned Deferred.

Everything runs on the Twisted reactor thread, so don't block in handlers.

Spot prices arrive as integers scaled by 100000 (`event.bid / 100000.0`). `symbol_digits` is stored but not used yet.

The README's intended layering is strategy → broker interface → cTrader client. It keeps the broker/API layer isolated so strategy code can be tested without a broker connection. Only the cTrader client layer exists so far. The README's roadmap is: OAuth code flow with token refresh, then historical bars, position/order reads, demo-only execution, a risk manager, and strategy/backtesting.
