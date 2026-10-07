# FX trading strategies for this bot: research report

October 2026. Scope: systematic strategies that a retail bot can run on an FxPro cTrader account through the Open API, mainly on FX majors.

## Bottom line

1. **No strategy can be expected to make money trading FX majors at retail costs today.** That holds for every candidate in the academic literature and for every strategy I tested. The best-documented FX strategies are trend following, carry and value. Each earned a real premium for decades. In developed-market currencies those premia have decayed to roughly zero since about 2010, and retail financing costs turn "roughly zero" into a loss.
2. **This conclusion rests on independent evidence, not one backtest:**
   - published out-of-sample studies (Neely, Weller & Ulrich 2009; Hsu, Taylor & Wang 2016; Hurst, Ooi & Pedersen 2017);
   - a textbook replication I ran without the backtester (12-month FX momentum: Sharpe 0.36 to 0.71 in every decade from the 1970s to the 2000s, then −0.15 in both the 2010s and the 2020s);
   - the bot's own engine on two independent datasets, 55 years of FRED closes and 22 years of OHLC bars;
   - a random-entry control that the main trend strategy could not beat.
3. **What the bot ships with:** three strategies with the strongest evidence, implemented carefully:
   - `donchian`: Turtle-style channel breakout;
   - `tsmom`: multi-horizon time-series momentum;
   - `carry`: carry with trend and volatility crash filters.

   Around them sit a strict risk manager, a conservative backtester and a demo-only execution path. Treat them as research tools, not income.
4. **If you trade anything, the least-bad option is `carry`.** Over 2005 to 2026 it had a small positive Sharpe ratio (0.15) and an 8% maximum drawdown, against −0.10 to −0.18 Sharpe and 33 to 50% drawdowns for trend following. That 0.15 is not statistically distinguishable from zero. Run it on demo at small risk, and compare live results with the backtest before believing anything.
5. **Avoid:** martingale and grid systems, high leverage, short-term indicator strategies on majors, and any "best parameters" found by searching a backtest. ESMA found that 74 to 89% of retail CFD accounts lose money; FxPro's own disclosure is about 77%.

The rest of this document explains why, and how each claim was checked.

---

## 1. Method

**Literature.** The academic evidence on FX strategies covers trend and time-series momentum, carry, value, cross-sectional momentum, technical trading rules, intraday patterns, volatility targeting, stop-losses and backtest overfitting. All sources are listed in [References](#references).

**Own tests.** Everything is reproducible with `python -m research.download` followed by `python -m research.run_research`, which writes `data/research/results.md`. The tests use:

| Dataset | Content | Used for |
|---|---|---|
| Yahoo Finance daily OHLC | 10 pairs (EURUSD, GBPUSD, USDJPY, AUDUSD, NZDUSD, USDCAD, USDCHF, EURJPY, EURGBP, AUDJPY), Dec 2003 to Oct 2026 | Main results with real highs and lows (stops, ATR) |
| FRED H.10 daily noon rates | 9 USD pairs incl. NOK and SEK, 1971 to 2026 (EUR from 1999) | Decade-by-decade history |
| FRED/OECD short-term rates | Overnight rates, with 3-month interbank rates filling gaps, 10 currencies | Historical swaps for carry and financing |

The free Yahoo data contains off-market ticks: EURUSD at 1.49 instead of 1.29 on 2008-12-08, for example. The downloader repairs such ticks by checking each bar against its neighbours, and the cleaned closes were cross-checked against FRED. Genuine shocks such as the 2015 SNB move are kept.

**Backtest model.** It is the same code that runs live, so a backtest exercises exactly what the bot would do.

- No look-ahead. Signals are computed on a bar's close and filled at the next bar's open. Sizes are fixed at signal time.
- Bars are treated as bid prices: longs buy at the ask, and short stops trigger on the ask. A gap through a stop fills at the open. If a bar touches both stop and target, the stop wins.
- FxPro-like costs:
  - commission of $35 per $1M per side;
  - padded raw spreads (EURUSD 0.5 pip, GBPUSD 0.9, USDJPY 0.7, others 0.7 to 1.8);
  - 0.1 pip slippage;
  - swaps equal to the historical interest-rate differential minus a 1% per year broker markup, with a triple charge on Wednesday.
- A USD 100,000 account risking 0.5% of equity per trade, with all the portfolio limits described in §5.
- The sticky 20% drawdown kill switch is disabled in research runs (it would simply end the test). The tables report when it would have fired.

**Guarding against self-deception.** The strategy defaults were fixed from the literature before any test was run. One change came after seeing data: TSMOM now rebalances monthly, as in the literature, instead of daily. It is flagged in §4.6, and both versions are reported. Parameter grids report a Deflated Sharpe Ratio (Bailey & López de Prado 2014), which corrects the best result for the number of configurations tried. A random-entry control tests whether the entry signal adds anything at all.

**How noisy is a Sharpe ratio?** Over 21 years the standard error of an annual Sharpe estimate is roughly √(1/21) ≈ 0.22. Differences of ±0.1 between variants are noise. Only large, consistent patterns mean anything.

---

## 2. What the literature says

### 2.1 Trend following and time-series momentum: strongest long-run evidence, decayed in FX

- **Moskowitz, Ooi & Pedersen (2012)** studied 58 futures and forwards across equities, bonds, currencies and commodities. They found a security's past 12-month excess return predicts its return over the next month, for *every* instrument. The effect persists for about a year and then partially reverses.
- **Hurst, Ooi & Pedersen (2017)** extended the test to 67 markets from 1880. Their strategy is an equal-weighted mix of 1, 3 and 12-month signals with volatility targeting. It returned 7.3% a year after simulated fees and costs, with a Sharpe ratio of 0.76, and was positive in every decade. But the decay is visible in their own tables:
  - In 2010 to 2016, the net Sharpe fell to 0.41.
  - Gross Sharpe by signal in 2010 to 2016 was **1-month 0.06, 3-month 0.30, 12-month 0.73.** Short lookbacks have stopped working.
  - Currency pairs had some of the lowest Sharpe ratios of any asset class over the full sample, around 0.1 to 0.6 per pair.
  - They assume one-way currency costs of 0.03% for 2003 to 2016.
- **Levine & Pedersen (2016)** show that moving-average crossovers, time-series momentum and other linear trend filters are mathematically near-equivalent. Choosing among trend indicators matters far less than people think.
- **Baltas & Kosowski** show that better volatility estimators and continuous signals in place of ±1 cut turnover by more than a third without hurting performance.

FX-specific evidence is more pessimistic:

- **Neely, Weller & Ulrich (2009)** ran true out-of-sample tests of the trading rules published in the 1970s to 1990s. The excess returns of the 1970s and 1980s were genuine, not data mining. But the profits **had disappeared by the early 1990s** for filter and moving-average rules. This is the "adaptive markets" pattern: published edges get traded away.
- **Hsu, Taylor & Wang (2016)** tested 21,195 technical rules on 30 currencies over 45 years, with a stepwise test against data snooping. For developed currencies: "only a few reliable rules in the 1980s, and none since the 1992–1996 subperiod". In their 2012 to 2015 out-of-sample test, three developed currencies had Sharpe ratios of 0.03, 0.29 and 0.54. Emerging currencies did better, but those are not practical at retail spreads.

### 2.2 Carry: a real risk premium with crash risk

- **Lustig, Roussanov & Verdelhan (2011)** show that high-rate currencies earn excess returns that compensate for exposure to a global risk factor.
- **Brunnermeier, Nagel & Pedersen (2008)** show carry returns are negatively skewed. They crash when risk appetite and funding liquidity dry up, as positions unwind together.
- **Menkhoff, Sarno, Schmeling & Schrimpf (2012b)** find that high-rate currencies lose when global FX volatility jumps. Volatility risk explains more than 90% of carry returns across portfolios. This is why the bot's `carry` strategy steps aside when short-term volatility spikes.
- **Daniel, Hodrick & Lu (2017)** split carry into two parts:
  - dollar-neutral carry, which has insignificant abnormal returns, is highly negatively skewed and carries real downside risk;
  - "dollar carry" (long or short the dollar against a basket, depending on average rate differentials), which has higher Sharpe and minimal skew.
- Carry did badly for the decade after 2008, while near-zero rates compressed differentials. It recovered in 2022. The most recent crash was the yen carry unwind of August 2024. The BIS estimated about ¥40 trillion (about $250bn) of FX carry positions going into it, and describes "volatility exacerbated by procyclical deleveraging".

**The retail catch:** a CFD account earns carry only through the broker's swap rates, and those include a financing markup on both sides. On many pairs neither side earns positive swap. The `carry` strategy therefore measures carry from the broker's own `swapLong`/`swapShort`, so it only trades carry that actually exists after the markup.

### 2.3 Value, cross-sectional momentum and style combinations: not implemented

- **Value** (PPP and real exchange rates; Menkhoff et al. 2017) predicts currency returns over multi-year horizons. It needs macro data the bot does not have, and holding a position for years on a CFD costs years of financing markup.
- **Cross-sectional momentum** (Menkhoff et al. 2012a) shows spreads of up to 10% a year between past winner and loser currencies. But the authors stress "very effective limits to arbitrage": the returns sit in currencies with high costs and idiosyncratic risk, which is not a retail majors trade.
- **Combining styles** (carry, momentum and value) adds real diversification for an international portfolio (Kroencke, Schindler & Schrimpf 2014). In my tests the bot's trend and carry strategies have daily-return correlations of about 0.3, which helps only if each has a positive edge. Recently neither did.

### 2.4 Short-term and intraday strategies: real patterns, too small for retail costs

- Intraday FX returns have documented patterns:
  - currencies tend to weaken during their home trading hours (Breedon & Ranaldo);
  - the US dollar traces a W-shaped path around the Tokyo, Frankfurt and London fixes, driven by dealer hedging (Krohn, Mueller & Whelan 2024).

  These are statistically robust but measured in basis points, roughly the size of a retail round trip (about 1 to 1.3 pips all-in on EURUSD at FxPro).
- Support/resistance breakouts were profitable after costs in data from the 1980s (Curcio & Goodhart 1992). The decay evidence in §2.1 applies to them too.
- A typical retail pullback rule, Connors-style RSI(2) with a 200-day trend filter, is tested in §4: Sharpe −0.20.

### 2.5 Risk management evidence

- **Stop-losses** lower expected returns when prices follow a random walk, but can add value when returns have momentum (Kaminski & Lo 2014). This favours stops for trend strategies. The bot still puts a broker-side stop on *every* position, because a disconnected bot with no stop is the larger risk.
- **Volatility targeting** improves Sharpe ratios for equities and credit but makes little difference for currencies, bonds and commodities. It does reduce extreme losses in all of them (Harvey et al. 2018). Sizing every position by an ATR-based stop is an implicit form of volatility targeting.
- **Leverage and Kelly.** For a strategy with annual Sharpe ratio S, the growth-optimal (Kelly) volatility is about S per year. With S ≈ 0.15 that means roughly 15% volatility, and half that is prudent, since S is barely known. With S ≤ 0 the optimal bet is zero. The bot's default of 0.5% risk per trade gave 2.5 to 8% annual volatility in the tests.
- **Backtest overfitting.** Try enough variants and one will look good by luck. Harvey, Liu & Zhu (2016) argue that new factors need t-statistics above 3, not 2. Bailey et al. show how quickly the probability of an overfit backtest rises with the number of trials. Use `python main.py sweep`: it reports the Deflated Sharpe Ratio of the best configuration.

### 2.6 Strategies to avoid outright

- **Martingale and grid** systems (adding to losers) turn many small wins into an eventual account-ending loss. They have no edge, only a hidden risk of ruin.
- **High leverage.** EU retail CFDs are capped at 30:1 on majors for a reason. ESMA's product intervention analysis found 74 to 89% of retail CFD accounts lose money.
- **Searching for the best parameters on a backtest and trading them** (§2.5).

---

## 3. Costs at FxPro cTrader, and why financing matters most

| Cost | FxPro cTrader | Effect in tests |
|---|---|---|
| Commission | $35 per $1M notional, charged on open and close (≈ $3.50 per side per 100k) | Small: 0.6 to 1.4k USD over 21 years per strategy |
| Spread | Raw, EURUSD about 0.2 to 0.4 pip on average (modelled 0.5) | Small for daily strategies |
| Swap | Interest differential ± markup, charged at 21:59 UK time, triple on Wednesday | **Largest:** −17k to −19k USD over 21 years for trend strategies |

Cost sensitivity, 2005 to 2026, CAGR / Sharpe:

| Strategy | Costs ×0 | Costs ×1 | Costs ×2 | Costs ×4 | Swap markup 0% | 0.5% | 2% |
|---|---|---|---|---|---|---|---|
| donchian | −1.7% / −0.16 | −1.9% / −0.18 | −2.0% / −0.20 | −2.6% / −0.27 | −1.0% / −0.07 | −1.5% / −0.13 | −2.8% / −0.29 |
| tsmom | −1.2% / −0.17 | −0.8% / −0.10 | −1.0% / −0.14 | −1.4% / −0.20 | −0.0% / 0.02 | −0.7% / −0.09 | −2.1% / −0.33 |
| carry | 0.4% / 0.18 | 0.3% / 0.15 | 0.3% / 0.12 | 0.1% / 0.07 | 0.4% / 0.13 | 0.3% / 0.13 | 0.1% / 0.06 |

What the table shows:

- Even with **zero** execution costs and **zero** swap markup, trend following was flat to negative in this period. Costs are not the reason it fails; the missing edge is.
- The financing markup alone moves trend results by 1 to 2% a year. Any strategy that holds positions for weeks pays it.
- Small inconsistencies such as tsmom at ×0 versus ×1 are path-dependence noise. Different fills change which stops trigger.

---

## 4. Test results

### 4.1 Main results: Yahoo OHLC, 10 pairs, 2005-01 to 2026-10

| Strategy | CAGR | Vol | Sharpe | Max DD | Trades | Win | Profit factor | Swap (USD) | P(Sharpe>0) | 20% DD halt |
|---|---|---|---|---|---|---|---|---|---|---|
| donchian | −1.9% | 8.3% | −0.18 | 49.6% | 814 | 28% | 0.84 | −16,630 | 20% | Mar 2011 |
| tsmom | −0.8% | 5.8% | −0.10 | 32.8% | 564 | 24% | 0.88 | −18,563 | 32% | Oct 2012 |
| carry | **0.3%** | 2.5% | **0.15** | **8.4%** | 460 | 33% | 1.16 | +14,925 | 75% | never |
| donchian + carry | −2.1% | 10.2% | −0.15 | 55.7% | 1,295 | 30% | 0.87 | −7,244 | 24% | Mar 2011 |
| donchian + tsmom + carry | −4.4% | 16.2% | −0.19 | 76.8% | 1,934 | 28% | 0.86 | −35,200 | 18% | Jan 2006 |
| rsi2 mean reversion (research only) | −0.8% | 3.7% | −0.20 | 24.2% | 2,616 | 55% | 0.93 | −5,938 | 17% | May 2018 |

Sub-periods (Sharpe): the decline continues into the most recent years.

| Strategy | 2005–2012 | 2013–2019 | 2020–2026 |
|---|---|---|---|
| donchian | 0.04 | −0.02 | −0.80 |
| tsmom | 0.08 | 0.08 | −0.58 |
| carry | 0.34 | −0.23 | 0.15 |
| rsi2 | −0.07 | −0.29 | −0.27 |

Combining strategies did not help, because each component lacked an edge. The combinations simply stacked more risk on the same losses. Daily-return correlations were 0.57 between donchian and tsmom and about 0.3 between trend and carry.

### 4.2 Long history: FRED closes, 9 USD pairs, Sharpe by decade

| Strategy | 1975–84 | 1985–94 | 1995–04 | 2005–14 | 2015–26 | 1973–2026 |
|---|---|---|---|---|---|---|
| donchian | 1.41 | 1.02 | 0.49 | 0.12 | **−0.64** | 0.60 |
| tsmom | 1.32 | 0.58 | 0.36 | 0.06 | **−0.39** | 0.50 |
| carry | 1.04 | 1.13 | 1.27 | 0.18 | **−0.15** | 0.49 |
| dollar carry (research only) | 0.37 | 1.01 | 0.81 | −0.07 | 0.09 | 0.46 |

The same engine and cost model that loses money today made money in the 1970s to 1990s. The engine is not biased against these strategies; the market changed. These figures flatter the early decades, because they use today's costs, while real costs then were several times higher. Close-only data also means fills and stops happen at closes.

### 4.3 Independent replication (no backtester)

Textbook 12-month TSMOM on FRED month-end closes: each leg is scaled to 10% volatility, gross of costs.

| 1972–79 | 1980–89 | 1990–99 | 2000–09 | 2010–19 | 2020–26 |
|---|---|---|---|---|---|
| 0.56 | 0.71 | 0.36 | 0.59 | **−0.15** | **−0.15** |

This matches the engine's results and the published decay. That rules out a bug as the explanation.

### 4.4 Random-entry control

Random entries were given exactly the donchian strategy's stops, trailing exits, sizing and costs, at a similar trade frequency, over 40 seeds. They averaged a Sharpe of −0.24 (st. dev. 0.15, best 0.07). Donchian scored −0.18, and **13 of 40 random runs did as well or better.** In 2005 to 2026 its breakout entries were statistically indistinguishable from coin flips. Random entries lost 1.6% a year on average, which is about the cost drag of being in the market.

### 4.5 Parameter robustness

- **donchian**, 14 configurations (entry 20/55/100 × exit 10/20/40 × stop 2/3 ATR): every one is negative, with Sharpe between −0.13 and −0.26. The best has a Deflated Sharpe Ratio of 18%.
- **tsmom**, 8 configurations (four lookback sets × stop 3/5 ATR): every one is negative, with Sharpe between −0.10 and −0.32. The best has a Deflated Sharpe Ratio of 14%.

No parameter choice rescues trend following on FX majors in this period, so there is nothing to optimise.

### 4.6 Specification checks

| Variant | Yahoo Sharpe 2005–26 | Yahoo Max DD | Trades | FRED Sharpe 1973–2026 | FRED Sharpe 2015–26 |
|---|---|---|---|---|---|
| tsmom, daily rebalance (first version) | −0.10 | 32.1% | 2,634 | 0.39 | −0.67 |
| tsmom, weekly | −0.15 | 36.9% | 1,188 | 0.46 | −0.40 |
| **tsmom, monthly (default, as in the literature)** | −0.10 | 32.8% | 564 | 0.50 | −0.39 |
| carry, no filters | 0.07 | 22.9% | 91 | 0.11 | 0.03 |
| carry, trend filter only | 0.10 | 9.1% | 469 | 0.48 | −0.15 |
| **carry, trend + volatility filters (default)** | 0.15 | 8.4% | 460 | 0.49 | −0.15 |
| dollar carry (research only) | −0.02 | 8.6% | 83 | 0.46 | 0.09 |

- Monthly rebalancing cuts TSMOM turnover by 4.7× for the same result, so it is the default.
- The carry filters cut the maximum drawdown from 22.9% to 8.4% and lifted the long-run Sharpe from 0.11 to 0.49. That is what the crash-risk literature predicts, and it is why they are on by default.

---

## 5. Risk management the bot enforces

Every limit is shared by the backtester and the live trader (`trading/risk.py`), and each can be set on the command line.

| Control | Default | Why |
|---|---|---|
| Risk per trade | 0.5% of equity lost if the stop is hit | Fixed-fractional sizing with ATR stops is inverse-volatility sizing. Small because the Kelly analysis in §2.5 says edges are thin or absent. |
| Protective stop on every entry | Required by the planner; sent to the broker as a relative stop with the market order | The position stays protected if the bot or connection dies. |
| Total open risk | 3% of equity | Caps the loss if every stop is hit at once. |
| Currency concentration | 1.5% of equity per currency and direction | Long EURUSD, GBPUSD and AUDUSD is one big short-USD bet. |
| Leverage | 10× notional / equity | Well inside ESMA's 30:1 retail margin limit. |
| Max positions | 10 | |
| Daily loss halt | 3% from the previous day's equity; no new entries until the next UTC day | |
| Drawdown halt | 20% from peak; no new entries until a human runs `trade --reset-drawdown` | Over 2005 to 2026 it would have stopped donchian in March 2011 and tsmom in October 2012. |
| Demo only | `trade` exits if `CTRADER_LIVE=1`; the client refuses order requests on the live host | Required by this project. |
| Only own positions | Bot positions carry the label `fxbot:<strategy>`; anything else on the account is ignored | Manual trades are never touched. |

---

## 6. Recommendations

1. **Do not expect income from these strategies on FX majors.** Use the bot to learn, test ideas honestly, and build a live track record on demo.
2. **Start with a dry run**, `python main.py trade --strategy carry`, then `--execute` on the demo account. Compare the journal (`logs/journal.jsonl`) against a backtest over the same period. Real fills, spreads and swaps differ from the model.
3. **Use the broker's own data:** `python main.py fetch` saves cTrader bars and the account's actual swap rates. Two caveats:
   - A backtest with `--swaps broker` applies *today's* swap rates to all history. It is a rough guide for carry, and wrong in sign for periods when rates were different.
   - Measure real spreads with `python main.py` (quotes now show the spread in pips) and pass them to `--spread`.
4. **If you research further**, the literature points to where an edge is more plausible:
   - Multi-asset trend following across indices, metals, energies and bonds. Hurst et al.'s strongest results come from diversifying across 67 markets, not from FX alone. The framework handles any cTrader symbol: fetch it and backtest.
   - Slower signals.
   - Lower-cost execution.

   Test every new idea against the random-entry control. Report the Deflated Sharpe Ratio. Keep the last few years untouched as a holdout.
5. **Never move to a live account** on the strength of a backtest. This project keeps live trading disabled. Changing that should require months of demo results consistent with the backtest, and a deliberate decision about money you can afford to lose.

---

## Appendix: cTrader Open API facts the implementation relies on

All of these were verified against the installed `ctrader-open-api` 0.9.2 protobuf definitions and, where possible, against the demo server (it reported API version 102).

- **Rate limits:** 50 requests per second for non-historical data and 5 per second for historical data, per connection. The Python library throttles everything to 5 messages per second.
- **Trendbars:**
  - `ProtoOAGetTrendbarsReq` needs `fromTimestamp` (required in this protocol version).
  - At most 14,000 bars come back per request, with no "has more" flag, so `fetch` requests chunks of 5,000 bars.
  - Prices are `low` plus unsigned deltas, in 1/100000 units.
  - `utcTimestampInMinutes` is the bar's open time. The newest bar can still be forming, so it is dropped.
- **Market orders:**
  - Market orders cannot carry an absolute stop. They take `relativeStopLoss` in 1/100000 price units, rounded to the symbol's digits (multiples of 100 for 3-digit JPY pairs).
  - Volumes are in hundredths of a unit: 100,000 units is `10,000,000`.
  - Fills arrive as `ProtoOAExecutionEvent` (`ORDER_ACCEPTED`, then `ORDER_FILLED`). Errors arrive as `ProtoOAOrderErrorEvent` or `ProtoOAErrorRes`.
- **Responses are matched to requests by `clientMsgId`**, which the library supports. The client resolves each request's Deferred and raises `CTraderError` on error responses.
- **Access tokens expire after 30 days** (`expiresIn` 2,628,000 s). Refresh tokens do not expire until used. Token refresh is not implemented yet; when the token expires the bot logs a fatal authentication error and stops.

---

## References

Trend following and technical rules
- Moskowitz, T., Ooi, Y.H. & Pedersen, L.H. (2012). Time Series Momentum. *Journal of Financial Economics* 104(2). https://papers.ssrn.com/abstract=2089463
- Hurst, B., Ooi, Y.H. & Pedersen, L.H. (2017). A Century of Evidence on Trend-Following Investing. *Journal of Portfolio Management* 44(1). https://www.aqr.com/Insights/Research/Journal-Article/A-Century-of-Evidence-on-Trend-Following-Investing
- Levine, A. & Pedersen, L.H. (2016). Which Trend Is Your Friend? *Financial Analysts Journal* 72(3). https://rpc.cfainstitute.org/research/financial-analysts-journal/2016/which-trend-is-your-friend
- Baltas, A.N. & Kosowski, R. Demystifying Time-Series Momentum Strategies: Volatility Estimators, Trading Rules and Pairwise Correlations. https://papers.ssrn.com/abstract=2140091
- Neely, C., Weller, P. & Ulrich, J. (2009). The Adaptive Markets Hypothesis: Evidence from the Foreign Exchange Market. *Journal of Financial and Quantitative Analysis* 44(2). https://econpapers.repec.org/paper/fipfedlwp/2006-046.htm
- Hsu, P.-H., Taylor, M. & Wang, Z. (2016). Technical Trading: Is It Still Beating the Foreign Exchange Market? *Journal of International Economics* 102. https://cepr.org/publications/DP10018 (summary: https://cxoadvisory.com/technical-trading/updated-comprehensive-long-term-test-of-technical-currency-trading)
- Curcio, R. & Goodhart, C. (1992). When Support/Resistance Levels Are Broken, Can Profits Be Made? LSE FMG DP 142. https://ideas.repec.org/p/fmg/fmgdps/dp142.html
- Faith, C. *The Original Turtle Trading Rules* (System 1: 20-day breakout; System 2: 55-day breakout; 2N stops; N-based sizing).

Carry, value and currency styles
- Lustig, H., Roussanov, N. & Verdelhan, A. (2011). Common Risk Factors in Currency Markets. *Review of Financial Studies* 24(11). https://www.nber.org/papers/w14082
- Brunnermeier, M., Nagel, S. & Pedersen, L.H. (2008). Carry Trades and Currency Crashes. *NBER Macroeconomics Annual*. https://www.nber.org/papers/w14473
- Menkhoff, L., Sarno, L., Schmeling, M. & Schrimpf, A. (2012a). Currency Momentum Strategies. *Journal of Financial Economics* 106(3). https://www.bis.org/publ/work366.pdf
- Menkhoff, L., Sarno, L., Schmeling, M. & Schrimpf, A. (2012b). Carry Trades and Global Foreign Exchange Volatility. *Journal of Finance* 67(2). https://openaccess.city.ac.uk/id/eprint/3391/
- Menkhoff, L., Sarno, L., Schmeling, M. & Schrimpf, A. (2017). Currency Value. *Review of Financial Studies* 30(2). https://ideas.repec.org/p/cpr/ceprdp/11324.html
- Burnside, C., Eichenbaum, M. & Rebelo, S. (2011). Carry Trade and Momentum in Currency Markets. *Annual Review of Financial Economics* 3. https://www.nber.org/papers/w16942
- Daniel, K., Hodrick, R. & Lu, Z. (2017). The Carry Trade: Risks and Drawdowns. *Critical Finance Review* 6(2). https://www.nber.org/papers/w20433
- Kroencke, T., Schindler, F. & Schrimpf, A. (2014). International Diversification Benefits with Foreign Exchange Investment Styles. *Review of Finance* 18(5). https://madoc.bib.uni-mannheim.de/35565/
- BIS (2024). The market turbulence and carry trade unwind of August 2024. BIS Bulletin 90. https://www.bis.org/publ/bisbull90.pdf

Intraday patterns
- Breedon, F. & Ranaldo, A. Intraday Patterns in FX Returns and Order Flow. SNB Working Paper 2011-04. https://www.snb.ch/en/publications/research/working-papers/2011/working_paper_2011_04
- Krohn, I., Mueller, P. & Whelan, P. (2024). Foreign Exchange Fixings and Returns Around the Clock. *Journal of Finance* 79(1). https://www.bankofcanada.ca/wp-content/uploads/2021/10/swp2021-48.pdf

Risk management and statistics
- Kaminski, K. & Lo, A. (2014). When Do Stop-Loss Rules Stop Losses? *Journal of Financial Markets* 18. https://dspace.mit.edu/handle/1721.1/114876
- Harvey, C., Hoyle, E., Korgaonkar, R., Rattray, S., Sargaison, M. & Van Hemert, O. (2018). The Impact of Volatility Targeting. *Journal of Portfolio Management*. https://people.duke.edu/~charvey/Research/Published_Papers/P135_The_impact_of.pdf
- Bailey, D. & López de Prado, M. (2014). The Deflated Sharpe Ratio. *Journal of Portfolio Management* 40(5). https://papers.ssrn.com/abstract=2460551
- Bailey, D., Borwein, J., López de Prado, M. & Zhu, Q. The Probability of Backtest Overfitting. *Journal of Computational Finance*. https://papers.ssrn.com/abstract=2326253
- Harvey, C., Liu, Y. & Zhu, H. (2016). … and the Cross-Section of Expected Returns. *Review of Financial Studies* 29(1). https://www.nber.org/papers/w20592
- Kelly, J.L. (1956). A New Interpretation of Information Rate. *Bell System Technical Journal* 35(4).

Retail trading, broker and API
- ESMA (2018). ESMA agrees to prohibit binary options and restrict CFDs to protect retail investors. https://www.esma.europa.eu/node/84933
- FxPro: cTrader commission. https://www.fxpro.com/help-section/faq/ctrader-platform/how-are-ctrader-commission-charges-calculated; swaps and charges: https://www.fxpro.com/commissions-swap-charges
- FxPro retail CFD loss disclosure (about 77%), via BrokerChooser. https://brokerchooser.com/broker-reviews/fxpro-review/fxpro-commission-and-other-fees
- cTrader Open API documentation. https://help.ctrader.com/open-api/ ; trendbar limits: https://community.ctrader.com/forum/connect-api-support/24731/ ; tokens: https://help.ctrader.com/open-api/account-authentication/

Data
- FRED, Federal Reserve Bank of St. Louis: H.10 daily exchange rates (DEXUSEU and others); OECD short-term interest rates (IRSTCI01*, IR3TIB01*). https://fred.stlouisfed.org
- Yahoo Finance daily FX bars (via the public chart endpoint), cleaned as described in §1.
