# FxPro cTrader Open API starter bot

Linux/Python starter application for an FxPro cTrader account.

This first version is intentionally READ-ONLY:
- connects to cTrader Open API
- authenticates the application
- authenticates a cTrader trading account
- retrieves symbols
- finds a configured symbol
- subscribes to live bid/ask quotes
- prints quotes

No order-placement code is included yet.

## 1. Create a cTrader account

You need an FxPro cTrader account (demo is recommended for development).

## 2. Register a cTrader Open API application

Register an application in the cTrader Open API portal and obtain:
- client ID
- client secret

For a real OAuth application, configure a redirect URI and obtain an access token.
For initial personal testing, cTrader also provides a Playground from the Applications page.

## 3. Create the virtual environment

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 4. Configure

```bash
cp .env.example .env
```

Set `CTRADER_CLIENT_ID`, `CTRADER_CLIENT_SECRET`, and `CTRADER_ACCESS_TOKEN`.

Keep `CTRADER_LIVE=0`.

If `CTRADER_ACCOUNT_ID` is empty, the application selects the first account returned for the access token.

## 5. Run

```bash
PYTHONPATH=src python -m fxpro_bot
```

You should see account information followed by live EURUSD bid/ask updates.

## Architecture

```text
strategy
   |
broker interface
   |
cTrader client
   |
Spotware cTrader Open API
   |
FxPro cTrader account
```

The broker/API layer is intentionally isolated so strategy code can later be tested without connecting to the broker.

## Next steps

1. OAuth authorization-code flow with a local callback.
2. Token persistence and refresh.
3. Historical bars.
4. Position/order read API.
5. Demo-only order execution.
6. Risk manager.
7. Strategy/backtesting layer.
