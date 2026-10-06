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

Create a `.env` file in the repository root (it is loaded automatically), or export the variables in your shell:

```bash
CTRADER_CLIENT_ID=your-client-id
CTRADER_CLIENT_SECRET=your-client-secret
CTRADER_ACCESS_TOKEN=your-access-token
CTRADER_ACCOUNT_ID=
CTRADER_SYMBOL=EURUSD
CTRADER_LIVE=0
```

| Variable | Required | Default | Description |
|---|---|---|---|
| `CTRADER_CLIENT_ID` | yes | | Open API application client ID |
| `CTRADER_CLIENT_SECRET` | yes | | Open API application client secret |
| `CTRADER_ACCESS_TOKEN` | yes | | OAuth access token |
| `CTRADER_ACCOUNT_ID` | no | first account for the token | cTID trader account ID |
| `CTRADER_SYMBOL` | no | `EURUSD` | Symbol to subscribe to (case-insensitive) |
| `CTRADER_LIVE` | no | `0` | `1` connects to the live host; anything else uses demo |

Keep `CTRADER_LIVE=0`.

## 5. Run

```bash
python main.py
```

You should see the authentication steps and the selected account, followed by live bid/ask updates for the configured symbol. Stop with `Ctrl+C`.

## Project layout

```text
main.py       entry point: loads config, prints quotes, runs the Twisted reactor
config.py     reads and validates environment variables
ctrader.py    CTraderClient: Open API connection, auth flow, symbol lookup, spot subscription
```

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

Currently only the cTrader client layer exists; the strategy and broker interface layers are planned.

## Next steps

1. OAuth authorization-code flow with a local callback.
2. Token persistence and refresh.
3. Historical bars.
4. Position/order read API.
5. Demo-only order execution.
6. Risk manager.
7. Strategy/backtesting layer.
