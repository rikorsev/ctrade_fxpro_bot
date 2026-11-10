# Trading strategies for this bot: research report

October 2026. Scope: systematic strategies that a retail bot can run on an FxPro cTrader account through the Open API.
- **Part I** covers swing trading FX majors on daily bars.
- **Part II** covers day trading index CFDs, gold and FX on five-minute bars, holding for minutes to hours and never overnight, with high risk on a small account.

## Bottom line

### Day trading (Part II)

1. **One of 24 intraday strategy and market combinations made money net of FxPro's costs from 2011 to 2026.** It is `noise_breakout`, an intraday momentum strategy (Zarattini, Aziz & Barbon 2024), on the Nasdaq 100 CFD:
   - 5.0% a year at 1% risk per trade, Sharpe ratio 0.40, maximum drawdown 24%;
   - about 250 trades a year, held for about two hours each and closed before every session ends, so no swap is ever paid.
2. **Its edge is real but small, and it has weakened since publication:**
   - Before costs it is statistically significant (t = 3.4), and every one of 16 parameter variants was profitable.
   - After costs it is +1.4 basis points per trade (t = 1.8), not statistically reliable. It breaks even at about twice the modelled costs.
   - Picked as the best of 24 combinations, it cannot be told apart from luck (Deflated Sharpe Ratio 0%). The case for the Nasdaq rests on its low cost relative to its movement, which was known before testing.
   - Since the paper appeared in May 2024 its Sharpe ratio is 0.13, with 2025 and 2026 both losing years.
3. **Everything else lost money net of costs:**
   - the opening range breakout (`orb`), whose direction is better than a coin flip but worth less than the spread;
   - last-half-hour momentum;
   - the same momentum strategy on the S&P 500, DAX, FTSE, gold and FX;
   - every combination of markets or strategies.

   The momentum exists before costs in most markets; FxPro's 1.2 to 2.5 basis-point round trip is larger than it.
4. **High risk does not buy more return beyond about 3% per trade.** On a USD 1,000 account the backtest gave:

   | Risk per trade | Return a year | Max drawdown |
   |---|---|---|
   | 2% (the `aggressive` profile) | 7.8% | 51% |
   | 3% | 8.4% | 67% |
   | 5% | 3.8% | 86% |

   At 5%, the account halved at some point in two simulated years out of five. At 2%, the account lost half its value between June 2014 and May 2017. With the `aggressive` profile's kill switches on, that is where the drawdown halt stopped the bot.
5. **Scalping is not feasible with this bot.** Its API throttling, bar-based decisions and retail spreads leave no room. The shortest horizon tested is five-minute bars.
6. **What the bot ships with for day trading:**
   - `noise_breakout` and `orb` on five-minute bars, with exchange sessions and daylight saving;
   - a `standard` (0.5% per trade) and an `aggressive` (2% per trade) risk profile;
   - the same demo-only execution path as before.

   Treat it as an experiment with a slight tilt in its favour, not as income. Almost all real day traders lose money (§7).

### Swing trading FX on daily bars (Part I)

1. **No strategy can be expected to make money trading FX majors at retail costs today.** That holds for every candidate in the academic literature and for every strategy I tested. The best-documented FX strategies are trend following, carry and value. Each earned a real premium for decades. In developed-market currencies those premia have decayed to roughly zero since about 2010, and retail financing costs turn "roughly zero" into a loss.
2. **This conclusion rests on independent evidence, not one backtest:**
   - published out-of-sample studies (Neely, Weller & Ulrich 2009; Hsu, Taylor & Wang 2016; Hurst, Ooi & Pedersen 2017);
   - a textbook replication I ran without the backtester (12-month FX momentum: Sharpe 0.36 to 0.71 in every decade from the 1970s to the 2000s, then −0.15 in both the 2010s and the 2020s);
   - the bot's own engine on two independent datasets, 55 years of FRED closes and 22 years of OHLC bars, plus a cross-check on FxPro's own cTrader history and swap rates;
   - a random-entry control that the main trend strategy could not beat.
3. **What the bot ships with:** three strategies with the strongest evidence, implemented carefully:
   - `donchian`: Turtle-style channel breakout;
   - `tsmom`: multi-horizon time-series momentum;
   - `carry`: carry with trend and volatility crash filters.

   Around them sit a strict risk manager, a conservative backtester and a demo-only execution path. Treat them as research tools, not income.
4. **If you trade anything, the least-bad option is `carry`.** Over 2005 to 2026 it had a small positive Sharpe ratio (0.15) and an 8% maximum drawdown, against −0.10 to −0.18 Sharpe and 33 to 50% drawdowns for trend following. That 0.15 is not statistically distinguishable from zero. Run it on demo at small risk, and compare live results with the backtest before believing anything.

### For both parts

**Avoid:**
- martingale and grid systems;
- more risk than the edge supports: for the best intraday result, more than about 2 to 3% per trade (§11);
- short-term indicator strategies on FX majors;
- any "best parameters" found by searching a backtest.

ESMA found that 74 to 89% of retail CFD accounts lose money; FxPro's own disclosure is about 77%.

The rest of this document explains why, and how each claim was checked.

---

# Part I: Swing trading FX on daily bars

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
  - swaps equal to the historical interest-rate differential minus a 1% per year broker markup, with a triple charge on Wednesday. FxPro's real swap rates imply a median markup of 1.2% (§4.7).
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
- Part II (§7 to §12) tests intraday strategies in depth, on index CFDs, gold and FX.

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

### 4.7 Cross-check on FxPro's own data (demo account, 7 October 2026)

The bot was run against a real FxPro cTrader demo account: quotes, `fetch` of D1 history and contract specs, backtests and a live dry run.

- **Backtests on cTrader's own D1 bars**, 10 pairs, 2008 to 2026, with today's FxPro swap rates applied to the whole history: donchian Sharpe −0.23 (58% max DD), tsmom −0.31 (44%), carry +0.29 (7.4%). This independent data source agrees with §4.1. Carry looks a little better here partly because today's swaps are applied to the past, which knows the current carry direction in advance.
- **Live spreads** at the time were 0.4 pip on EURUSD and 0.3 pip on USDJPY, below the 0.5 and 0.7 pips modelled. The cost model is conservative on spreads.
- **Real swap markup.** The account's `swapLong`/`swapShort`, compared with the latest short-term rate differentials, imply a markup of **1.2% per side (median; mean 1.5%, range 0.1 to 3.8%)**. It is asymmetric: the negative-carry side of high-differential pairs (short USDJPY, short USDCHF, short EURJPY) costs 3 to 4% a year more than the differential. The 1% markup used in this research is, if anything, slightly favourable to the strategies.
- **Pairs that earn real carry** after the markup, at today's rates: only long AUDJPY (2.5% a year), long USDCHF (1.8%) and long USDJPY (1.6%) exceed the strategy's 1% threshold. On the dry run, `carry` wanted long USDCHF; AUDJPY and USDJPY failed the trend filter.
- **Account size matters.** The minimum order is 1,000 units. With a daily ATR stop, the minimum position already risks about $14 to $19, so a $1,000 balance cannot follow a 0.5% risk-per-trade rule, and the risk manager skips every entry. Prudent sizing on majors needs roughly $10,000 or more.

---

## 5. Risk management the bot enforces

Every limit is shared by the backtester and the live trader (`trading/risk.py`). `--risk-profile` selects the `standard` (default) or `aggressive` preset, and each limit can also be set on its own on the command line. §11 tests the aggressive profile.

| Control | `standard` | `aggressive` | Why |
|---|---|---|---|
| Risk per trade | 0.5% of equity lost if the stop is hit | 2% | Fixed-fractional sizing with volatility-based stops is inverse-volatility sizing. The standard value is small because the Kelly analysis in §2.5 says edges are thin or absent; 2% is about the growth-optimal level for the best intraday backtest (§11). |
| Protective stop on every entry | Required by the planner; sent to the broker as a relative stop with the market order | same | The position stays protected if the bot or connection dies. |
| Total open risk | 3% of equity | 6% | Caps the loss if every stop is hit at once. |
| Currency concentration | 1.5% of equity per currency and direction | 6% | Long EURUSD, GBPUSD and AUDUSD is one big short-USD bet. |
| Leverage | 10× notional / equity | 20× | Inside ESMA's retail limits: 30:1 on FX majors, 20:1 on major indices. |
| Max positions | 10 | 6 | |
| Daily loss halt | 3% from the previous day's equity; no new entries until the next UTC day | 6% | |
| Drawdown halt | 20% from peak; no new entries until a human runs `trade --reset-drawdown` | 50% | At 0.5% risk, 20% would have stopped donchian in March 2011 and tsmom in October 2012. At 2% risk the best intraday backtest fell 51% (2014 to 2017), so a lower threshold would halt on routine losses. Even 50% fired, in May 2017 (§11). |
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

# Part II: Day trading

Part I looked for an edge in holding FX positions for weeks. Part II asks whether one exists in trading many times a week, holding for minutes to hours and never overnight, with high risk on a small account.

## 7. What day trading changes

**It removes the largest cost of Part I.** The broker's financing markup (§3) is charged on positions held at the daily rollover. A strategy that is flat before the close never pays it.

**It multiplies the cost that remains.** A strategy that trades every day pays the spread and slippage about 250 times a year, so the average trade must move further than the round trip. On FxPro that is 0.3 to 1.5 basis points of the price per round trip on index CFDs, plus commission on FX and gold. The average move inside a trade has to clear that by a clear margin.

**Scalping is out of reach for this bot.** Scalping means holding for seconds to a few minutes and living on order flow, queue position and latency, which is what high-frequency firms compete for. This bot cannot compete there:
- the Python Open API library sends at most 5 messages per second;
- the bot reacts to closed bars, and its market orders reach the server seconds after a bar closes;
- an index moves about 1 to 3 basis points in an average minute, roughly the retail round-trip cost.

The shortest horizon tested here is therefore five-minute bars, with trades lasting minutes to hours.

**Most day traders lose.** This is the base rate any day-trading strategy has to beat:
- Barber, Lee, Liu & Odean (2014) studied every day trader in Taiwan from 1992 to 2006. Fewer than 1% were able to earn positive abnormal returns net of fees predictably and reliably.
- Chague, De-Losso & Giovannetti (2019) followed everyone who started day trading Brazilian index futures in 2013 to 2015. Of those who kept at it for at least 300 days, 97% lost money, and only 1.1% earned more than the minimum wage.
- ESMA found that 74 to 89% of retail CFD accounts lose money (§2.6).

## 8. What the literature says about intraday strategies

### 8.1 Intraday momentum in equity indices: the strongest evidence

- **Gao, Han, Li & Zhou (2018)** showed that on the S&P 500 ETF from 1993 to 2013, the return from the previous close to 10:00 predicts the return of the last half hour of the day. The effect is stronger on volatile days and in recessions.
- **Baltussen, Da, Lammers & Martens (2021)** found the same pattern in more than 60 futures markets (equity indices, bonds, commodities, currencies) from 1974 to 2020. Their explanation is a mechanism, not a pattern search. Options dealers and leveraged ETFs that are short gamma must buy as prices rise and sell as they fall, mostly late in the day, which pushes prices further in the direction of the day's move. The effect partly reverses over the following days.
- **Zarattini, Aziz & Barbon (2024)** turned this into an all-day strategy on the S&P 500 ETF:
  - The "noise area" is the band within which prices usually move during the day.
  - A move outside it signals a demand/supply imbalance that tends to persist.
  - The authors report 19.6% a year net of costs and a Sharpe ratio of 1.33 for 2007 to early 2024, with leverage of up to 4×.
  - That is their own backtest. Its costs are ETF commissions and a small slippage allowance, a fraction of a CFD spread.

  This is the strategy implemented as `noise_breakout`.

### 8.2 Opening range breakout: a gross effect that costs eat

- **Zarattini & Aziz (2023)** tested a five-minute opening range breakout on the Nasdaq 100 ETF from 2016 to 2023. It trades in the direction of the first five minutes, with the stop at the other end of that range and a target of 10× the risk. They report a 33% annual alpha net of commissions, with up to 4× leverage, but no spread or slippage.
- An **independent replication on index CFDs** (2015 to 2026) reproduced the gross effect on the Nasdaq 100: +0.13 R per trade. Net of CFD spreads, though, no market was distinguishable from zero and four of five were negative. The replication also found at most +0.06 R on the Nasdaq after 2021. It is a single, non-peer-reviewed source; §10 tests the same rule on this data.

This strategy is implemented as `orb`.

### 8.3 FX intraday: patterns smaller than retail costs

- **Breedon & Ranaldo** show that currencies tend to weaken during their home trading hours.
- **Krohn, Mueller & Whelan (2024)** show that the dollar moves in a W shape around the Tokyo, Frankfurt and London fixes.
- **Elaut, Frömmel & Lampaert (2018)** found intraday momentum in the rouble, driven by liquidity providers who avoid holding overnight.

None of these documents an effect in the majors large enough to clear a retail round trip of about 1 basis point plus commission (§2.4).

### 8.4 Where to look

The best-supported candidate is intraday momentum in equity indices. It has a published mechanism, many markets and decades of data. Index CFDs are also where FxPro's costs are lowest relative to daily movement. On the demo account, the Nasdaq 100 CFD's spread was about 0.3 basis points against an average daily range of 1.8%. Among FxPro's cash index CFDs, that is the lowest cost relative to movement: the S&P 500 CFD's spread was 0.9 basis points against a 1.1% range.

## 9. How the intraday strategies were tested

**Data.** HistData.com publishes free one-minute bid bars, used here from November 2010 to October 2026 for eight markets:
- index CFDs: Nasdaq 100, S&P 500, DAX, FTSE 100;
- gold;
- EURUSD, GBPUSD and USDJPY.

The bars were aggregated to five minutes, and only bars inside each market's session were kept. That covers about 258 sessions a year. The checks found no malformed bars. The jumps above 2% between consecutive bars are overnight gaps in volatile periods such as August 2011 and March 2020. From March to July 2023 about half the bars are missing in every market; those sessions were kept as they are.

**Sessions.** All sessions are in local time, so daylight saving follows each exchange:

| Market | Session |
|---|---|
| US indices | 09:30–16:00 New York |
| DAX | 09:00–17:30 Frankfurt |
| FTSE | 08:00–16:30 London |
| FX and gold | 08:00–17:00 London |

**Execution.** The backtest uses the same strategy, planner, risk manager and fill model as the live bot:
- Signals are computed on a bar's close and filled at the next bar's open.
- Stops and targets trigger on the bar's high or low. Longs buy at the ask, and shorts exit at the ask.
- Every position is closed at the open of the last five-minute bar (15:55 for US indices), so none is held overnight.

**Costs.** These are measured on an FxPro cTrader demo account in October 2026 and padded:

| Market | Spread (bp of price) | Slippage per fill (bp) | Commission per side (bp) | Round trip (bp) |
|---|---|---|---|---|
| Nasdaq 100 | 0.6 (measured 0.3) | 0.3 | 0 | 1.2 |
| S&P 500 | 1.0 (measured 0.9) | 0.3 | 0 | 1.6 |
| DAX | 1.0 (measured 1.0) | 0.4 | 0 | 1.8 |
| FTSE 100 | 1.5 (measured 1.4) | 0.5 | 0 | 2.5 |
| Gold | 0.6 (measured 0.5) | 0.3 | 0.35 | 1.9 |
| EURUSD, GBPUSD, USDJPY | 0.5–0.9 pip (about 0.45–0.7 bp) | 0.1 pip (about 0.1 bp) | 0.35 | about 1.3–1.5 |

Slippage stands for the seconds between a bar's close and the bot's order reaching the server. A breakout strategy pays for the price drifting on in the breakout direction during that time.

**Sizing.** Each trade risks 1% of equity at a stop 0.5 daily standard deviations away. That reproduces the paper's exposure, a 2% daily volatility target, and leverage is capped at 4× as in the paper. The kill switches are off, so the tests measure the strategies (§11 turns them on).

**Guarding against self-deception.**
- The defaults come from the papers and were fixed before any test.
- The months after each paper appeared on SSRN are out of sample: from May 2024 for `noise_breakout` and from April 2023 for `orb`.
- A random-direction control tests what the opening range's direction is worth.
- A parameter grid reports a Deflated Sharpe Ratio.

**Deviations from the paper.**
- A CFD instead of the S&P 500 ETF.
- An equal-weighted session average price instead of VWAP, because CFD tick volume is not traded volume.
- A broker-side stop that the paper does not have. A bot needs one in case it disconnects.
- Exit at 15:55 instead of 16:00.
- The data starts in 2011, so it misses 2007 to 2010, which includes 2008, one of the paper's best years.

## 10. Intraday test results

Everything is reproducible with `python -m research.download --only histdata` followed by `python -m research.intraday`, which writes `data/research/intraday_results.md`. The tables below come from that file.

### 10.1 Each strategy on each market, 2011 to 2026

Sharpe ratio gross (no costs) → net (FxPro-like costs). One market per run, 1% risk per trade, leverage capped at 4×.

| Market | Round trip | `noise_breakout` | `orb` | `late_momentum` (research only) |
|---|---|---|---|---|
| Nasdaq 100 | 1.2 bp | 0.87 → **0.40** | 0.13 → −0.29 | 0.31 → −0.73 |
| S&P 500 | 1.6 bp | 0.59 → −0.17 | −0.09 → −0.75 | 0.35 → −1.26 |
| DAX | 1.8 bp | 0.19 → −0.50 | 0.43 → −0.24 | 0.32 → −0.91 |
| FTSE 100 | 2.5 bp | 0.28 → −0.93 | 0.45 → −0.53 | 0.24 → −1.75 |
| Gold | 1.9 bp | 0.76 → −0.26 | 0.77 → −0.54 | 0.21 → −1.78 |
| EURUSD | ~1.3 bp | −0.21 → −1.66 | 0.20 → −1.19 | −0.33 → −2.83 |
| GBPUSD | ~1.5 bp | 0.51 → −0.92 | 0.15 → −1.44 | 0.23 → −2.43 |
| USDJPY | ~1.3 bp | 0.68 → −0.83 | 0.05 → −1.58 | 0.56 → −2.57 |

`late_momentum` is the classic test from Gao et al. (2018): half an hour before the close, take the direction of the day's move so far and hold it to the close.

What the table shows:
- **Intraday momentum is real before costs.** 21 of the 24 combinations have a positive gross Sharpe ratio, the pattern the literature describes.
- **Costs are larger than the edge almost everywhere.** Before costs the average trade earns between −0.3 and +2.6 basis points, and under 1.3 in all but one case. A round trip costs 1.2 to 2.5. Net of costs, 23 of the 24 lose money.
- **The one survivor is `noise_breakout` on the Nasdaq 100,** the market with the lowest cost relative to its movement:

  | Measure | Value |
  |---|---|
  | Net return | 5.0% a year |
  | Sharpe ratio | 0.40 |
  | Max drawdown | 24% |
  | Trades | 246 a year, held about two hours on average |
  | Win rate | 36% (losses are cut at the trailing level, winners run to the close) |
  | Average trade | +2.6 bp gross (t = 3.4), +1.4 bp net (t = 1.8) |

  The gross edge is statistically significant. The net edge is not: a t-statistic of 1.8 is below the usual 2, and well below the 3 that Harvey, Liu & Zhu (2016) ask for.
- **Choosing the Nasdaq after seeing the table is itself a selection.** Its Deflated Sharpe Ratio among the 24 combinations is 0%: once picking the best of 24 is accounted for, the result cannot be told apart from luck.
  - The test is harsh here, because most of the spread between the 24 results comes from their different costs rather than from chance.
  - But it agrees with the t-statistic: the data alone cannot show that the net edge is real.
  - What favours the Nasdaq is the argument of §8.4, made before testing: it has the lowest cost relative to its movement.

### 10.2 Over time, and after publication

Net Sharpe ratio (CAGR) by period:

| Market | 2011–15 | 2016–20 | 2021 to May 2024 | After the paper (May 2024 to Oct 2026) |
|---|---|---|---|---|
| Nasdaq 100 | 0.21 (2.0%) | 0.54 (7.7%) | 0.63 (8.4%) | **0.13 (0.8%)** |
| S&P 500 | −0.35 (−5.4%) | 0.04 (−0.5%) | 0.25 (2.7%) | −0.94 (−13.1%) |
| DAX | −0.15 (−3.8%) | −0.50 (−9.0%) | −0.77 (−13.8%) | −0.88 (−14.8%) |
| FTSE 100 | −0.56 (−9.6%) | −0.46 (−8.3%) | −1.69 (−27.2%) | −1.44 (−25.9%) |

Calendar years, Nasdaq 100, net:

| 2011 | 2012 | 2013 | 2014 | 2015 | 2016 | 2017 | 2018 | 2019 | 2020 | 2021 | 2022 | 2023 | 2024 | 2025 | 2026 (to Oct) |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 5% | −3% | 6% | −10% | 12% | −3% | −10% | 36% | −4% | 25% | 7% | 26% | −9% | 34% | −13% | −5% |

- The strategy makes its money in turbulent years (2018, 2020, 2022, 2024) and bleeds in calm ones. Seven of the 16 years lost money.
- Out of sample it has been weak: Sharpe 0.13 since the paper appeared, with 2025 and 2026 both negative. McLean & Pontiff (2016) found that published stock-return predictors earn 58% less after publication, so some decay is the normal expectation, not bad luck.

### 10.3 Opening range breakout: the direction carries information, but not enough

Net Sharpe ratio of `orb`, against the same rule with a coin flip choosing the direction (20 seeds per market):

| Market | `orb` | Random: mean (st. dev.) | Random runs that did as well |
|---|---|---|---|
| Nasdaq 100 | −0.29 | −0.52 (0.19) | 3 of 20 |
| S&P 500 | −0.75 | −0.86 (0.16) | 3 of 20 |
| DAX | −0.24 | −0.74 (0.18) | 0 of 20 |

The first five minutes do predict the direction better than chance, as the independent replication also found. The prediction is worth less than the round trip, though. Net of costs, `orb` lost money in every market and every sub-period, except the DAX from 2021 to early 2023.

### 10.4 Parameter robustness

`noise_breakout` on the Nasdaq 100 was run with 16 configurations: band 0.75, 1, 1.25 or 1.5; checks every 30 or 60 minutes; stop at 0.5 or 1 daily sigma. Risk per trade was scaled with the stop, so exposure stayed the same.
- All 16 are positive net of costs, with Sharpe ratios from 0.29 to 0.56. The best has a Deflated Sharpe Ratio of 96%.
- Wider bands (1.25 and 1.5) did best because they trade less: 150 to 200 times a year instead of 246. That is consistent with costs being the binding constraint.
- The defaults stay at the paper's values. Picking the best of 16 on this data would be the selection §2.5 warns about.

### 10.5 More markets and more trades make it worse

| Portfolio | Costs ×0 | ×0.5 | ×1 | ×1.5 | ×2 | Max DD at ×1 | Trades/yr |
|---|---|---|---|---|---|---|---|
| noise: Nasdaq 100 | 0.87 | 0.62 | **0.40** | 0.17 | −0.04 | 24% | 246 |
| noise: Nasdaq 100 + S&P 500 | 0.81 | 0.47 | 0.13 | −0.21 | −0.57 | 63% | 492 |
| noise: 4 indices | 0.73 | 0.12 | −0.49 | −0.66 | −1.00 | 99% | 1,072 |
| noise + orb: Nasdaq 100 | 0.50 | 0.20 | −0.12 | −0.59 | −0.91 | 88% | 494 |

Each extra market or strategy adds more cost than edge. The Nasdaq strategy alone breaks even at about 1.9× the modelled costs: a round trip of about 2.3 basis points instead of 1.2. That margin is thin. Wider spreads at the open, slower fills or a broker change could remove it.

### 10.6 Comparison with the paper

On the S&P 500 CFD from 2011 to 2026 the strategy had a gross Sharpe ratio of 0.59, against the paper's 1.33 on the ETF from 2007 to 2024. Several things differ:
- the period: this data misses 2008, a very strong year for intraday momentum;
- the instrument: a CFD instead of the ETF;
- the average price: a session average instead of VWAP;
- the exits: this version adds a broker-side stop and closes at 15:55.

The larger difference is costs. Net of CFD costs, the S&P 500 version lost 3.3% a year.

## 11. High risk with a small account

Section 10 measured the strategy at 1% risk per trade. This section asks what happens on a USD 1,000 account at higher risk. The leverage cap is 20×, the EU retail limit for major index CFDs, and sizes are rounded to the 0.01-unit minimum.

The test uses `noise_breakout` on the Nasdaq 100, net of costs, from 2011 to 2026, with the kill switches off:

| Risk per trade | CAGR | Volatility | Max drawdown | Worst day | Worst month | $1,000 became |
|---|---|---|---|---|---|---|
| 0.5% (`standard`) | 2.6% | 7% | 14% | −2.7% | −4.7% | $1,504 |
| 1% | 5.0% | 15% | 27% | −5.3% | −9.4% | $2,148 |
| **2% (`aggressive`)** | 7.8% | 30% | **51%** | −10.9% | −18.3% | $3,254 |
| 3% | 8.4% | 45% | 67% | −16.4% | −26.4% | $3,575 |
| 5% | 3.8% | 73% | 86% | −27.4% | −40.4% | $1,796 |

One-year outcomes, from a block bootstrap of 2016 to 2026 daily returns (2,000 simulated years):

| Risk per trade | Median year | Bad year (5th percentile) | Good year (95th) | P(losing year) | P(drawdown ≥ 30%) | P(drawdown ≥ 50%) |
|---|---|---|---|---|---|---|
| 0.5% | +3.5% | −6% | +15% | 27% | 0% | 0% |
| 1% | +6.8% | −13% | +31% | 29% | 0% | 0% |
| 2% | +11.6% | −26% | +67% | 32% | 15% | 0% |
| 3% | +13.7% | −38% | +106% | 36% | 51% | 5% |
| 5% | +10.4% | −60% | +183% | 44% | 92% | 39% |

What this means:
- **Risk does not change the edge.** The Sharpe ratio is 0.39 at every level. More risk only scales the swings, up and down.
- **Beyond about 3% per trade, more risk means less money.** Growth peaks around 3% and then falls, because large losses compound against you (volatility drag).
  - This matches the Kelly calculation in §2.5. With a Sharpe ratio of 0.39, growth-optimal volatility is about 39% a year, which here is about 2.5% risk per trade.
  - If the true Sharpe is nearer the post-publication 0.13, the optimum falls to about 1% per trade, and 2% is already twice it.
- **2% per trade is the `aggressive` profile.**
  - In the backtest it made 7.8% a year, with a 51% peak-to-trough fall and an 18% worst month.
  - In a typical simulated year one outcome in three is a loss, and one in seven sees a 30% drawdown.
  - Leverage is about 3× at the Nasdaq's typical volatility and about 7× in calm markets.
- **5% per trade is gambling.** It earned less than 1% per trade's return with an 86% drawdown, and in two simulated years out of five the account halves at some point.
- **Small accounts work on index CFDs.** The Nasdaq 100 CFD trades in 0.01-unit steps, so a $1,000 account was never too small for an entry at any of these levels. That is not true of FX pairs, whose 1,000-unit minimum is about $1,100 of notional.

**Kill switches.** The tables above run with the kill switches off. With the `aggressive` profile's switches on, the same backtest went differently:
- The 6% daily-loss halt fired four times between 2011 and 2016, each time blocking new entries for the rest of that day.
- The account peaked at $1,355 in June 2014. A three-year losing stretch then took it down 50% by May 2017, and the sticky drawdown halt stopped all new entries.
- The account ended at $681, because the halt stayed on through the strategy's best years: 2018, 2020, 2022 and 2024.
- With the switches off, the same strategy fell to $668, got back above its 2014 peak only in December 2018, and ended at $3,254.

A sticky halt fires at the bottom by design, and a human then decides whether to continue. At 2% per trade, this strategy put the account through that decision after three years of losses and four and a half years below its peak. That is what high risk looks like with a Sharpe ratio of 0.4.

## 12. What the bot ships for day trading, and recommendations

**Shipped:**
- `noise_breakout`, the strategy of §8.1 and the only intraday result that survived costs.
- `orb`, for comparison; it lost money net of costs everywhere.
- `standard` (0.5% per trade) and `aggressive` (2% per trade) risk profiles. The aggressive profile's drawdown halt is at 50%; in the backtest it fired once, in May 2017.
- Sessions with daylight saving, flat before every close, and costs in basis points for CFDs.

**Recommendations:**
1. **Expect a small, uncertain edge, not income.**
   - Over 16 years the best intraday result made 5% a year at 1% risk.
   - Its net edge is not statistically reliable, and since publication it has roughly broken even.
   - The base rate (§7) is that almost all day traders lose.
2. **Trade only the Nasdaq 100 CFD with `noise_breakout`.** Adding the S&P 500, the DAX, the FTSE, gold or FX added costs, not edge (§10.5).
3. **Measure your real costs before believing anything.** The edge disappears at about 2.3 basis points per round trip. Compare each fill in `logs/journal.jsonl` with the bar's close, and watch the spread at 10:00 New York time, when most entries happen.
4. **If you want high risk, 2% per trade is the ceiling that makes sense.** Above about 3%, expected growth falls even if the backtest is right. Above 5% the account is likely to halve within a year.
5. **Run it on demo for months,** then compare the results with a backtest of the same period on FxPro's own five-minute bars (`python main.py fetch --symbols '#USNDAQ100' --timeframe M5`).

---

## Appendix: cTrader Open API facts the implementation relies on

All of these were verified against the installed `ctrader-open-api` 0.9.2 protobuf definitions and, where possible, against the demo server (it reported API version 102).

- **Rate limits:** 50 requests per second for non-historical data and 5 per second for historical data, per connection. The Python library throttles everything to 5 messages per second.
- **Trendbars:**
  - FxPro's D1 bars start at 21:00 UTC in summer (the New York close), so the bar labelled "Sunday 21:00" is Monday's full session. Weekend merging only folds genuine stubs (bars closing before Monday midday UTC), so these bars are kept intact.
  - `ProtoOAGetTrendbarsReq` needs `fromTimestamp` (required in this protocol version).
  - At most 14,000 bars come back per request, with no "has more" flag, so `fetch` requests chunks of 5,000 bars.
  - Prices are `low` plus unsigned deltas, in 1/100000 units.
  - `utcTimestampInMinutes` is the bar's open time. The newest bar can still be forming, so it is dropped.
- **Market orders:**
  - Market orders cannot carry an absolute stop. They take `relativeStopLoss` in 1/100000 price units, rounded to the symbol's digits (multiples of 100 for 3-digit JPY pairs).
  - Volumes are in hundredths of a unit: 100,000 units is `10,000,000`.
  - Fills arrive as `ProtoOAExecutionEvent` (`ORDER_ACCEPTED`, then `ORDER_FILLED`). Errors arrive as `ProtoOAOrderErrorEvent` or `ProtoOAErrorRes`.
- **Responses are matched to requests by `clientMsgId`**, which the library supports. The client resolves each request's Deferred and raises `CTraderError` on error responses. It gives every request a random ID: the library's default, the `id()` of the response Deferred, can be reused after a timeout, and a late answer would then reach the wrong request.
- **On a disconnect the library forgets unanswered requests without failing them**, so each would hang until its 30 s timeout. The client fails them at once. It also ignores login failures from a connection that has already closed: before, such a failure restarted the new, healthy connection, and those restarts kept repeating each other.
- **Access tokens expire after 30 days** (`expiresIn` 2,628,000 s). Refresh tokens do not expire until used. On expiry the server sends `ProtoOAAccountDisconnectEvent`, requests fail with `OA_AUTH_TOKEN_EXPIRED`, and the next login fails with `CH_ACCESS_TOKEN_INVALID` ("Access token expired"). The client then sends `ProtoOARefreshTokenReq` with `CTRADER_REFRESH_TOKEN`, saves the new pair in `state/ctrader_token.json` and logs in again. Without a refresh token it stops.
- **Token scope.** A token issued with the `accounts` scope can read quotes, history and positions, but the server rejects orders ("TRADE permission required"). `ProtoOAGetAccountListByAccessTokenRes.permissionScope` reports the scope; the client logs it at login and refuses orders, and `trade --execute` refuses to start, when it is view-only. Orders need a token issued with the `trading` scope.

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

Intraday momentum and day trading
- Gao, L., Han, Y., Li, S. Z. & Zhou, G. (2018). Market Intraday Momentum. *Journal of Financial Economics* 129(2). https://doi.org/10.1016/j.jfineco.2018.05.009
- Baltussen, G., Da, Z., Lammers, S. & Martens, M. (2021). Hedging Demand and Market Intraday Momentum. *Journal of Financial Economics* 142(1). https://www3.nd.edu/~zda/intramom.pdf
- Zarattini, C., Aziz, A. & Barbon, A. (2024). Beat the Market: An Effective Intraday Momentum Strategy for S&P500 ETF (SPY). Swiss Finance Institute Research Paper 24-97. https://ideas.repec.org/p/chf/rpseri/rp2497.html
- Zarattini, C. & Aziz, A. (2023). Can Day Trading Really Be Profitable? Evidence of Sustainable Long-term Profits from Opening Range Breakout (ORB) Day Trading Strategy. https://papers.ssrn.com/abstract=4416622
- The opening-range breakout paper, replicated on five indices (2026, non-peer-reviewed). https://www.mql5.com/en/blogs/post/776235
- Elaut, G., Frömmel, M. & Lampaert, K. (2018). Intraday Momentum in FX Markets: Disentangling Informed Trading from Liquidity Provision. *Journal of Financial Markets* 37. https://econpapers.repec.org/RePEc:eee:finmar:v:37:y:2018:i:c:p:35-51
- Barber, B., Lee, Y.-T., Liu, Y.-J. & Odean, T. (2014). The Cross-Section of Speculator Skill: Evidence from Day Trading. *Journal of Financial Markets* 18. https://econpapers.repec.org/RePEc:eee:finmar:v:18:y:2014:i:c:p:1-24
- Chague, F., De-Losso, R. & Giovannetti, B. (2019). Day Trading for a Living? https://papers.ssrn.com/abstract=3423101
- McLean, R. D. & Pontiff, J. (2016). Does Academic Research Destroy Stock Return Predictability? *Journal of Finance* 71(1). https://ivey.uwo.ca/media/3775549/pontiff.pdf

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
- HistData.com free one-minute bid bars (FX, gold and index CFDs), aggregated to five minutes as described in §9. https://www.histdata.com
