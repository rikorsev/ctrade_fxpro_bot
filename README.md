# FxPro cTrader Open API bot

Linux/Python bot for an FxPro cTrader account. It can:
- stream live bid/ask quotes and spreads;
- download history and contract specs (digits, volumes, swap rates) from cTrader;
- backtest evidence-based strategies with realistic costs, swaps and risk limits:
  - **day trading** on five-minute bars of index CFDs, flat before every close (`noise_breakout`, `orb`);
  - swing trading on daily bars of FX pairs (`donchian`, `tsmom`, `carry`);
- run the same strategies against a **demo** account, as a dry run or sending orders, with a `standard` or an `aggressive` risk profile.

> **Read [docs/RESEARCH.md](docs/RESEARCH.md) before trading anything.** The research reviews the academic evidence and tests every strategy net of FxPro's costs:
> - **Day trading.** Of 24 intraday strategy and market combinations tested on 2011–2026 five-minute data, only `noise_breakout` (intraday momentum) on the Nasdaq 100 CFD made money after costs: 5% a year at 1% risk per trade, Sharpe 0.40, 24% maximum drawdown. Its edge is small: +1.4 basis points per trade, not statistically reliable, and indistinguishable from luck once picking the best of 24 is accounted for. It breaks even at twice the modelled costs, and it has roughly broken even since the paper behind it appeared in May 2024.
> - **Swing trading.** No daily strategy can be expected to make money on FX majors at retail costs today.
>
> Use the bot on demo as an experiment, not as a source of income.

Order sending is **demo-only**. The `trade` command refuses to start with `CTRADER_LIVE=1`, and the client rejects order requests on the live host.

## 1. Create a cTrader account

You need an FxPro cTrader account. Use a demo account: the bot only trades demo.

## 2. Register a cTrader Open API application

Register an application in the cTrader Open API portal and obtain:
- client ID
- client secret

For a real OAuth application, configure a redirect URI and obtain an access token.
For initial personal testing, cTrader also provides a Playground from the Applications page.

Keep the **refresh token** that comes with the access token. Access tokens expire after 30 days. With `CTRADER_REFRESH_TOKEN` set, the bot renews an expired token when it logs in, also in the middle of a run, and saves the new pair in `state/ctrader_token.json`. Restarts and the other commands then use the saved pair, as long as `.env` still holds the token it was renewed from. Put a fresh token in `.env` and the saved pair is ignored. Without a refresh token the bot stops when the token expires, and you need to generate a new one.

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
CTRADER_REFRESH_TOKEN=your-refresh-token
CTRADER_ACCOUNT_ID=
CTRADER_SYMBOL=EURUSD
CTRADER_SYMBOLS=#USNDAQ100
CTRADER_LIVE=0
```

FxPro names its index CFDs with a leading `#` (`#USNDAQ100`, `#USSPX500`, `#Germany40`, `#UK100`); symbol names are case-insensitive. `.env` reads `#USNDAQ100` fine, but on the command line quote it: `--symbols '#USNDAQ100'`.

| Variable | Required | Default | Description |
|---|---|---|---|
| `CTRADER_CLIENT_ID` | yes | | Open API application client ID |
| `CTRADER_CLIENT_SECRET` | yes | | Open API application client secret |
| `CTRADER_ACCESS_TOKEN` | yes | | OAuth access token |
| `CTRADER_REFRESH_TOKEN` | no (recommended) | | OAuth refresh token, used to renew the access token when it expires |
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
python main.py fetch --symbols '#USNDAQ100' --timeframe M5 --years 2    # writes data/#USNDAQ100_M5.csv
python main.py fetch --timeframe D1 --years 15      # writes data/<SYMBOL>_D1.csv and data/symbols.json
```

`symbols.json` holds each symbol's digits, volume limits and the account's current swap rates. Without it, backtests fall back to approximate FxPro specs for FX pairs and the common index CFDs and gold (`KNOWN_CFDS` in `trading/models.py`).

### Backtest

```bash
python main.py backtest --strategy noise_breakout --symbols '#USNDAQ100'
python main.py backtest --strategy noise_breakout --symbols '#USNDAQ100' --risk-profile aggressive --equity 1000
python main.py backtest --strategy orb --symbols '#USNDAQ100,#USSPX500' --data data/research/intraday
python main.py backtest --strategy carry
python main.py backtest --strategy donchian --param entry=100 --param exit=20 --cost-multiplier 2
```

The timeframe defaults to the strategy's own: M5 for the intraday strategies, D1 for the daily ones. Signals fill at the next bar's open. Costs include spread, slippage and commission:
- FX pairs: spreads in pips, plus $35 per $1M per side.
- Index CFDs and gold: spreads and slippage in basis points of price, measured on an FxPro demo account and padded. Index CFDs pay no commission.

With `--swaps broker`, today's swap rates from `symbols.json` are applied to the whole history; intraday strategies never hold overnight, so they pay none. Crosses such as EURGBP, and the DAX and FTSE CFDs, need a conversion pair (for example EURUSD or GBPUSD) in the data directory.

`data/research/intraday/` (from `python -m research.download --only histdata`) holds 2010–2026 five-minute bars for the Nasdaq 100, S&P 500, DAX, FTSE, gold, EURUSD, GBPUSD and USDJPY. They cover the whole day, so a backtest over all of it takes a few minutes.

### Parameter sweep

```bash
python main.py sweep --strategy noise_breakout --symbols '#USNDAQ100' --grid band=0.75,1,1.25 --grid check=30,60
python main.py sweep --strategy donchian --grid entry=20,55,100 --grid exit=10,20
```

The sweep prints every configuration and the **Deflated Sharpe Ratio** of the best one: the probability that its edge is real once you account for how many configurations were tried.

### Trade on the demo account

```bash
python main.py trade --strategy noise_breakout --symbols '#USNDAQ100'            # dry run: logs decisions, sends nothing
python main.py trade --strategy noise_breakout --symbols '#USNDAQ100' --risk-profile aggressive --execute
python main.py trade --strategy carry --execute                                   # daily strategy
```

Every entry is a market order with a broker-side stop. Decisions, orders and errors go to `logs/journal.jsonl`.

A symbol that cannot be traded does not stop the others. The bot logs a warning, journals `symbol_ignored` and carries on with the rest when a symbol is not offered on the account or a strategy has no trading session for it. It exits only if no symbol is left. If a strategy raises an error on one symbol, that bar is skipped and journaled as `strategy_failed`.

**Day trading.** With an intraday strategy the bot works on five-minute bars:
- It checks every 5 seconds and acts about 3 seconds after each bar closes.
- It trades only during the market's cash session: 09:30 to 16:00 New York time for US indices, 09:00 to 17:30 Frankfurt time for the DAX, 08:00 to 16:30 London time for the FTSE, and 08:00 to 17:00 London time for FX and gold. Daylight saving follows each exchange.
- `noise_breakout` decides every 30 minutes (10:00, 10:30, … New York time), so most bars produce no log line.
- Positions are closed 5 minutes before the session ends, so no swap is ever charged. A position that is still open in a later session (after a half-day holiday or a restart) is closed at once.
- Leave the bot running through the session. If it stops, open positions keep their broker-side stop but miss the session-close exit.

**Daily strategies.** The bot checks for a newly closed bar every minute and runs the strategies once per bar. FxPro's daily bars close at 21:00 UTC in summer (the New York close). `tsmom` only changes positions on the first daily bar of a month, so after a mid-month start it waits for the next month.

**Small accounts.** Position size comes from the risk per trade and is rounded down to the broker's volume step:
- Index CFDs trade in steps of 0.01 units (about $310 of Nasdaq 100 notional at October 2026 prices), so a $500 to $1,000 balance can follow the risk rules.
- FX pairs start at 1,000 units. With daily stops that minimum already risks about $15 to $20, so on a $1,000 balance at 0.5% risk every daily-strategy entry is skipped as "size below broker minimum".

Its other behaviour:
- Progress and kill-switch state are kept in `state/live_state.json`, so a restart neither repeats a bar nor forgets a halt.
- The bot only manages positions labelled `fxbot:<strategy>`; manual trades on the account are left alone.
- If a drawdown halt fires, clear it with `--reset-drawdown` once you have decided to continue.

## Strategies

Intraday (M5 bars, flat before the close):

| Name | Idea | Evidence (see docs/RESEARCH.md) | Defaults |
|---|---|---|---|
| `noise_breakout` | Intraday momentum. The "noise area" spans the average absolute move from the open at each time of day over 14 sessions. Every 30 minutes, go long above it or short below it; hold while price stays beyond both the area and the session's average price; flat 5 minutes before the close. Broker stop 0.5 daily standard deviations away | Zarattini, Aziz & Barbon 2024; Baltussen et al. 2021; Gao et al. 2018 | `days=14 band=1 check=30 stop_sigma=0.5` |
| `orb` | Opening range breakout: trade the direction of the first 5 minutes, stop at the other end of that range, target 10× the risk, otherwise flat at the close | Zarattini & Aziz 2023; an independent CFD replication found no net edge | `range_minutes=5 target_r=10` |

Daily (D1 bars):

| Name | Idea | Evidence (see docs/RESEARCH.md) | Defaults |
|---|---|---|---|
| `donchian` | Turtle-style breakout: enter on a close beyond the 55-bar high/low; initial stop 2 ATR, then trail the 20-bar channel | Hurst, Ooi & Pedersen 2017; Turtle rules; decayed in FX since the 1990s | `entry=55 exit=20 atr=20 stop_atr=2` |
| `tsmom` | Time-series momentum: majority vote of the 3, 6 and 12-month return signs, acted on monthly; 3-ATR disaster stop | Moskowitz, Ooi & Pedersen 2012; short lookbacks decayed most | `fast=63 medium=126 slow=252 stop_atr=3 rebalance=monthly` |
| `carry` | Hold the side that earns positive swap (net of the broker's markup) only while price is above/below its 100-bar SMA and volatility is not spiking | Lustig et al. 2011; Brunnermeier et al. 2008; Menkhoff et al. 2012 | `min_carry=1.0 trend=100 vol_cap=1.5 stop_atr=3` |

Results net of FxPro-like costs (docs/RESEARCH.md §10 and §4):
- `noise_breakout` on the Nasdaq 100 CFD, 2011–2026: 5.0% a year at 1% risk per trade, Sharpe 0.40, max drawdown 24%, about 250 trades a year. On the S&P 500, DAX, FTSE, gold and FX it lost money.
- `orb` lost money on all eight markets tested.
- Daily strategies on 10 FX pairs, 2005–2026: `donchian` Sharpe −0.18, `tsmom` −0.10, `carry` +0.15 (8% max drawdown).

## Risk controls

These limits are enforced identically in backtests and live (`trading/risk.py`). `--risk-profile` picks a preset, and each limit can also be set on its own (for example `--risk-per-trade 0.01`):

| Limit | `standard` (default) | `aggressive` |
|---|---|---|
| Equity lost if a new position's stop is hit | 0.5% | 2% |
| Total open risk | 3% | 6% |
| Risk per currency and direction | 1.5% | 6% |
| Open positions | 10 | 6 |
| Leverage (notional / equity) | 10× | 20× |
| Daily-loss halt (no new entries until the next UTC day) | 3% | 6% |
| Sticky drawdown halt (until `--reset-drawdown`) | 20% | 50% |

Every position also carries a broker-side protective stop. Choose the risk per trade knowingly. On the best intraday backtest (docs/RESEARCH.md §11):

| Risk per trade | Return a year | Max drawdown |
|---|---|---|
| 2% (`aggressive`) | 7.8% | 51% |
| 3% | 8.4% | 67% |
| 5% | 3.8% | 86% |

Above about 3% per trade, more risk means less money. At 2% the account lost half its value from June 2014 to May 2017. With the aggressive profile, that is where the sticky drawdown halt would have stopped the bot until you reset it.

## Research

```bash
python -m research.download          # free data from FRED, Yahoo and HistData into data/research/
python -m research.run_research      # daily strategies: regenerates data/research/results.md (about 5 minutes)
python -m research.intraday          # intraday strategies: regenerates data/research/intraday_results.md (30-60 minutes)
```

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest
```

The tests cover indicators, sessions and daylight saving, daily and intraday strategies, risk limits and profiles, costs and swaps, the backtester's fills and stops, metrics, data handling, the live trader on daily and five-minute bars (against a fake broker), and the cTrader client and adapter (against real protobuf messages with a fake transport).

## Project layout

```text
main.py              CLI: quotes (default), fetch, backtest, sweep, trade
config.py            reads and validates environment variables
ctrader.py           CTraderClient: connection, auth, typed Deferred-based requests, demo-only order methods
ctrader_broker.py    CTraderBroker: implements trading.broker.Broker on top of CTraderClient
trading/
  models.py          Bar, BarWindow, SymbolSpec (with approximate CFD specs), Position, Signal, AccountState
  sessions.py        exchange sessions in local time (US, EU, UK, FX), daylight saving included
  indicators.py      SMA, EMA, ATR, Donchian channel, RSI, Bollinger, volatility
  strategies/        noise_breakout, orb (intraday.py); donchian, tsmom (trend.py); carry (carry.py); base class and registry
  planner.py         signal + current position -> close / entry / stop-update actions
  risk.py            sizing, exposure caps, daily-loss and drawdown kill switches, risk profiles
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

1. Run `noise_breakout` on demo for a few months and compare the journal with a backtest of the same period on FxPro's own bars (`fetch --timeframe M5`). Real fills, spreads and latency are the biggest unknowns.
2. OAuth authorization-code flow with a local callback, so that a first token pair does not have to be copied from the Playground.
3. An exchange holiday calendar, so that half-day sessions close positions at the early close.
