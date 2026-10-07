# FxPro cTrader Open API bot

Linux/Python bot for an FxPro cTrader account. It can:
- stream live bid/ask quotes and spreads;
- download history and contract specs (digits, volumes, swap rates) from cTrader;
- backtest evidence-based FX strategies with realistic costs, swaps and risk limits;
- run the same strategies against a **demo** account, as a dry run or sending orders.

> **Read [docs/RESEARCH.md](docs/RESEARCH.md) before trading anything.** The research reviews the academic evidence and tests every strategy on 22 to 55 years of data. Its conclusion: no strategy here can be expected to make money on FX majors at retail costs today. Trend following and carry worked for decades, but their edge in developed currencies has decayed to about zero since around 2010, and broker financing costs turn that into a loss. Use the bot as a research and learning tool on demo.

Order sending is **demo-only**. The `trade` command refuses to start with `CTRADER_LIVE=1`, and the client rejects order requests on the live host.

## 1. Create a cTrader account

You need an FxPro cTrader account. Use a demo account: the bot only trades demo.

## 2. Register a cTrader Open API application

Register an application in the cTrader Open API portal and obtain:
- client ID
- client secret

For a real OAuth application, configure a redirect URI and obtain an access token.
For initial personal testing, cTrader also provides a Playground from the Applications page.
Access tokens expire after 30 days; generate a new one when the bot reports an authentication error.

Choose the **trading** scope when you generate the token (with the OAuth URL, `scope=trading`). A token with the `accounts` (view) scope works for quotes, `fetch`, backtests and dry runs. The server rejects its orders, though, so `trade --execute` refuses to start with one. The bot logs the token's scope at every login.

## 3. Create the virtual environment

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt        # or requirements-dev.txt to also install pytest
```

## 4. Configure

Copy `.env.example` to `.env` in the repository root (it is loaded automatically and is gitignored), or export the variables in your shell:

```bash
CTRADER_CLIENT_ID=your-client-id
CTRADER_CLIENT_SECRET=your-client-secret
CTRADER_ACCESS_TOKEN=your-access-token
CTRADER_ACCOUNT_ID=
CTRADER_SYMBOL=EURUSD
CTRADER_SYMBOLS=EURUSD,GBPUSD,USDJPY,AUDUSD,NZDUSD,USDCAD,USDCHF
CTRADER_LIVE=0
```

| Variable | Required | Default | Description |
|---|---|---|---|
| `CTRADER_CLIENT_ID` | yes | | Open API application client ID |
| `CTRADER_CLIENT_SECRET` | yes | | Open API application client secret |
| `CTRADER_ACCESS_TOKEN` | yes | | OAuth access token |
| `CTRADER_ACCOUNT_ID` | no | first demo account for the token | cTID trader account ID |
| `CTRADER_SYMBOL` | no | `EURUSD` | Symbol for the quote stream (case-insensitive) |
| `CTRADER_SYMBOLS` | no | `CTRADER_SYMBOL` | Comma-separated default symbols for `fetch`, `backtest`, `sweep` and `trade` |
| `CTRADER_LIVE` | no | `0` | `1` connects to the live host (quotes and data only; trading refuses to run) |

Keep `CTRADER_LIVE=0`.

## 5. Run

```bash
python main.py                       # stream quotes for CTRADER_SYMBOL, with the spread in pips
python main.py quotes --symbol GBPUSD
```

### Download data

```bash
python main.py fetch --timeframe D1 --years 15      # writes data/<SYMBOL>_D1.csv and data/symbols.json
```

`symbols.json` holds each symbol's digits, volume limits and the account's current swap rates.

### Backtest

```bash
python main.py backtest --strategy carry
python main.py backtest --strategy donchian --strategy carry --start 2015-01-01 --trades-csv trades.csv
python main.py backtest --strategy donchian --param entry=100 --param exit=20 --cost-multiplier 2
```

Signals fill at the next bar's open, and costs include spread, slippage and commission ($35 per $1M per side). With `--swaps broker`, today's swap rates from `symbols.json` are applied to the whole history, which is only a rough guide for carry. Crosses such as EURGBP need a conversion pair (for example GBPUSD) in the data directory.

### Parameter sweep

```bash
python main.py sweep --strategy donchian --grid entry=20,55,100 --grid exit=10,20
```

The sweep prints every configuration and the **Deflated Sharpe Ratio** of the best one: the probability that its edge is real once you account for how many configurations were tried.

### Trade on the demo account

```bash
python main.py trade --strategy carry                 # dry run: logs decisions, sends nothing
python main.py trade --strategy carry --execute       # sends orders to the demo account
```

The bot checks for a newly closed bar every minute and runs the strategies once per bar. FxPro's daily bars close at 21:00 UTC in summer (the New York close). Every entry is a market order with a broker-side stop.

Position size comes from the risk per trade, and the smallest order is 1,000 units. With daily stops that minimum already risks about $15 to $20, so on a $1,000 demo balance at 0.5% risk every entry is skipped as "size below broker minimum". Use a demo balance of about $10,000 or more.

`tsmom` only changes positions on the first daily bar of a month, so after a mid-month start it waits for the next month.

Its other behaviour:
- Decisions, orders and errors go to `logs/journal.jsonl`.
- Progress and kill-switch state are kept in `state/live_state.json`, so a restart neither repeats a bar nor forgets a halt.
- The bot only manages positions labelled `fxbot:<strategy>`; manual trades on the account are left alone.
- If a drawdown halt fires, clear it with `--reset-drawdown` once you have decided to continue.

## Strategies

| Name | Idea | Evidence (see docs/RESEARCH.md) | Defaults |
|---|---|---|---|
| `donchian` | Turtle-style breakout: enter on a close beyond the 55-bar high/low; initial stop 2 ATR, then trail the 20-bar channel | Hurst, Ooi & Pedersen 2017; Turtle rules; decayed in FX since the 1990s | `entry=55 exit=20 atr=20 stop_atr=2` |
| `tsmom` | Time-series momentum: majority vote of the 3, 6 and 12-month return signs, acted on monthly; 3-ATR disaster stop | Moskowitz, Ooi & Pedersen 2012; short lookbacks decayed most | `fast=63 medium=126 slow=252 stop_atr=3 rebalance=monthly` |
| `carry` | Hold the side that earns positive swap (net of the broker's markup) only while price is above/below its 100-bar SMA and volatility is not spiking | Lustig et al. 2011; Brunnermeier et al. 2008; Menkhoff et al. 2012 | `min_carry=1.0 trend=100 vol_cap=1.5 stop_atr=3` |

Results on 10 pairs over 2005 to 2026, with costs: donchian Sharpe −0.18, tsmom −0.10, carry +0.15 (8% max drawdown). Full tables are in the research report.

## Risk controls

These limits are enforced identically in backtests and live (`trading/risk.py`), and each can be set on the command line:
- 0.5% of equity at risk per trade (position size = risk / stop distance);
- at most 3% total open risk;
- at most 1.5% risk per currency and direction;
- 10× leverage cap;
- daily-loss halt at 3%;
- sticky drawdown halt at 20%;
- a protective stop on every position.

## Research

```bash
python -m research.download          # free data from FRED and Yahoo into data/research/
python -m research.run_research      # regenerates data/research/results.md (about 5 minutes)
```

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest
```

The tests cover indicators, strategies, risk limits, costs and swaps, the backtester's fills and stops, metrics, data handling, the live trader (against a fake broker), and the cTrader client and adapter (against real protobuf messages with a fake transport).

## Project layout

```text
main.py              CLI: quotes (default), fetch, backtest, sweep, trade
config.py            reads and validates environment variables
ctrader.py           CTraderClient: connection, auth, typed Deferred-based requests, demo-only order methods
ctrader_broker.py    CTraderBroker: implements trading.broker.Broker on top of CTraderClient
trading/
  models.py          Bar, SymbolSpec, Position, Signal, AccountState
  indicators.py      SMA, EMA, ATR, Donchian channel, RSI, Bollinger, volatility
  strategies/        donchian, tsmom (trend.py), carry (carry.py); base class and registry
  planner.py         signal + current position -> close / entry / stop-update actions
  risk.py            sizing, exposure caps, daily-loss and drawdown kill switches
  costs.py           spreads, commission, swaps, rollover counting, currency conversion
  backtest.py        event-driven backtester (shares strategies, planner and risk with live)
  metrics.py         CAGR, Sharpe, drawdowns, trade stats, probabilistic and deflated Sharpe
  data.py            CSV bars, symbols.json, weekend-bar merging, resampling
  broker.py          Broker protocol the live trader depends on
  live.py            LiveTrader: polls for closed bars, decides, sizes, executes or dry-runs, journals
research/            data download, research-only strategies, experiment runner
docs/RESEARCH.md     the research report
tests/               pytest suite
```

## Architecture

```text
strategy (trading/strategies)  ->  planner + risk manager  ->  broker interface (trading/broker.py)
                                                                   |                      |
                                                     backtester (simulated fills)   CTraderBroker
                                                                                          |
                                                                                    CTraderClient
                                                                                          |
                                                                               Spotware cTrader Open API
                                                                                          |
                                                                               FxPro cTrader demo account
```

Strategies, the planner and the risk manager are pure Python. The backtester and the live trader drive the same code, so a backtest exercises exactly the decisions and limits the bot applies live.

## Next steps

1. OAuth authorization-code flow with a local callback, plus token persistence and refresh. Access tokens currently expire after 30 days.
2. Multi-asset research (indices, metals, energies), where the trend-following evidence is stronger than in FX alone.
3. Compare months of demo results against backtests of the same period before considering anything else.
