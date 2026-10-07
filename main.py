from __future__ import annotations

import argparse
import csv
import itertools
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from config import default_symbols, load_config
from trading.risk import RiskConfig

log = logging.getLogger("fxbot")


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if getattr(args, "verbose", False) else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    handler = getattr(args, "handler", None) or cmd_quotes
    try:
        handler(args)
    except (ValueError, FileNotFoundError, RuntimeError) as exc:
        sys.exit(f"error: {exc}")


# -- live connection commands ------------------------------------------------


def cmd_quotes(args: argparse.Namespace) -> None:
    """Stream bid/ask for one symbol (the original behaviour of this bot)."""
    from twisted.internet import reactor

    from ctrader import CTraderClient

    config = load_config()
    symbol = (getattr(args, "symbol", None) or config.symbol).upper()
    print(f"Starting FxPro cTrader client ({'LIVE' if config.live else 'DEMO'})")
    if config.live:
        print("WARNING: live mode enabled")

    state: dict = {"id": None, "pip": None, "bid": None, "ask": None}

    def on_spot(symbol_id: int, bid: float | None, ask: float | None) -> None:
        if symbol_id != state["id"]:
            return
        state["bid"] = bid if bid is not None else state["bid"]
        state["ask"] = ask if ask is not None else state["ask"]
        spread = ""
        if state["bid"] is not None and state["ask"] is not None and state["pip"]:
            spread = f" spread={(state['ask'] - state['bid']) / state['pip']:.1f} pips"
        print(f"{symbol:8s} bid={bid!s:>12} ask={ask!s:>12}{spread}")

    client = CTraderClient(config.client_id, config.client_secret, config.access_token, config.account_id,
                           config.live, on_spot=on_spot, on_fatal=_stop_reactor)

    async def subscribe(c: CTraderClient) -> None:
        match = next((s for s in await c.symbols() if s.symbolName.upper() == symbol), None)
        if match is None:
            log.error("Symbol %r not found.", symbol)
            return
        state["id"] = int(match.symbolId)
        (details,) = await c.symbol_details([state["id"]])
        state["pip"] = 10.0 ** -details.pipPosition
        await c.subscribe_spots([state["id"]])
        log.info("Subscribed to %s (symbol_id=%s)", symbol, state["id"])

    client.on_ready(subscribe)
    client.start()
    reactor.run()


def cmd_fetch(args: argparse.Namespace) -> None:
    """Download history and contract specs from cTrader for backtesting."""
    from twisted.internet import reactor

    from ctrader import CTraderClient
    from ctrader_broker import CTraderBroker
    from trading.data import SPECS_FILE, bars_path, save_specs, write_bars
    from trading.models import timeframe_delta

    config = load_config()
    symbols = _symbols(args)
    end = datetime.now(timezone.utc)
    start = _parse_date(args.start) if args.start else end - timedelta(days=round(365.25 * args.years))
    client = CTraderClient(config.client_id, config.client_secret, config.access_token, config.account_id,
                           config.live, on_fatal=_stop_reactor)
    broker = CTraderBroker(client)
    done = {"started": False}

    async def run(_: CTraderClient) -> None:
        if done["started"]:
            return
        done["started"] = True
        try:
            await broker.load(symbols)
            for symbol in symbols:
                bars = await broker.history(symbol, args.timeframe, start, end)
                bars = [b for b in bars if b.time + timeframe_delta(args.timeframe) <= end]
                path = bars_path(args.data, symbol, args.timeframe)
                write_bars(path, bars)
                first = f"{bars[0].time:%Y-%m-%d}" if bars else "-"
                log.info("%s: %d %s bars from %s -> %s", symbol, len(bars), args.timeframe, first, path)
            save_specs(Path(args.data) / SPECS_FILE, {s: broker.symbol_spec(s) for s in symbols})
            log.info("Contract specs (digits, volumes, swaps) saved to %s", Path(args.data) / SPECS_FILE)
        except Exception:
            log.exception("Fetch failed")
        finally:
            _stop_reactor()

    client.on_ready(run)
    client.start()
    reactor.run()


def cmd_trade(args: argparse.Namespace) -> None:
    """Run strategies against the demo account (dry run unless --execute)."""
    from twisted.internet import defer, reactor, task

    from ctrader import CTraderClient
    from ctrader_broker import CTraderBroker
    from trading.live import LiveConfig, LiveTrader
    from trading.risk import RiskManager

    config = load_config()
    if config.live:
        sys.exit("Refusing to run: CTRADER_LIVE=1. This bot only trades demo accounts.")
    symbols = _symbols(args)
    strategies = build_strategies(args.strategy, args.param)
    risk = RiskManager(risk_config(args))
    client = CTraderClient(config.client_id, config.client_secret, config.access_token, config.account_id,
                           config.live, on_fatal=_stop_reactor)
    broker = CTraderBroker(client, label_prefix=args.label)
    live_config = LiveConfig(
        symbols=tuple(symbols),
        timeframe=args.timeframe,
        execute=args.execute,
        state_path=Path(args.state),
        journal_path=Path(args.journal),
    )
    trader = LiveTrader(broker, strategies, risk, live_config)
    if args.reset_drawdown:
        risk.reset_drawdown()
        log.warning("Drawdown halt cleared; the peak equity restarts from the next equity reading")

    log.info("Mode: %s on demo account. Strategies: %s. Symbols: %s. Timeframe: %s.",
             "EXECUTE" if args.execute else "DRY RUN (no orders)", ", ".join(s.describe() for s in strategies),
             ", ".join(symbols), args.timeframe)
    log.info("Risk: %s", risk.config)

    loop = task.LoopingCall(lambda: defer.ensureDeferred(trader.tick()).addErrback(
        lambda f: log.error("Trading cycle failed: %s", f.getTraceback())))

    async def on_ready(_: CTraderClient) -> None:
        await broker.load(symbols)
        if not loop.running:
            loop.start(args.poll, now=True)

    client.on_ready(on_ready)
    client.start()
    reactor.run()


def _stop_reactor(reason: str | None = None) -> None:
    from twisted.internet import reactor

    if reason:
        log.error(reason)
    if reactor.running:
        reactor.stop()


# -- offline commands ----------------------------------------------------------


def cmd_backtest(args: argparse.Namespace) -> None:
    from trading.metrics import summarize, yearly_returns

    strategies = build_strategies(args.strategy, args.param)
    result = _run_backtest(args, strategies)
    summary = summarize(result)
    print(f"\nStrategies: {', '.join(s.describe() for s in strategies)}")
    print(f"Symbols: {', '.join(_symbols(args))} ({args.timeframe}), costs x{args.cost_multiplier:g}, "
          f"swaps: {args.swaps}\n")
    print(summary.format())
    print("\nYear   Return")
    for year, ret in yearly_returns(result):
        print(f"{year}  {ret:+7.2%}")
    for when, reason in result.halts:
        print(f"Risk halt {when:%Y-%m-%d}: {reason}")
    if args.trades_csv:
        _write_trades(Path(args.trades_csv), result.trades)
        print(f"\nTrades written to {args.trades_csv}")


def cmd_sweep(args: argparse.Namespace) -> None:
    from trading.metrics import daily_equity, deflated_sharpe, per_period_sharpe, returns, summarize
    from trading.strategies import create_strategy

    grid = {}
    for item in args.grid:
        key, sep, values = item.partition("=")
        if not sep or not values:
            raise ValueError(f"--grid expects key=v1,v2,..., got {item!r}")
        grid[key] = values.split(",")
    combos = [dict(zip(grid, values)) for values in itertools.product(*grid.values())]
    rows, sharpes, series = [], [], []
    for combo in combos:
        try:
            strategy = create_strategy(args.strategy, **combo)
        except ValueError as exc:
            log.info("Skipping %s: %s", combo, exc)
            continue
        result = _run_backtest(args, [strategy])
        rets = returns([v for _, v in daily_equity(result.equity_curve)])
        s = summarize(result)
        rows.append((combo, s))
        sharpes.append(per_period_sharpe(rets))
        series.append(rets)
    if not rows:
        raise ValueError("no valid parameter combinations")
    print(f"\n{'parameters':40s} {'CAGR':>8s} {'Sharpe':>7s} {'MaxDD':>7s} {'Trades':>7s}")
    order = sorted(range(len(rows)), key=lambda i: -rows[i][1].sharpe)
    for i in order:
        combo, s = rows[i]
        label = ", ".join(f"{k}={v}" for k, v in combo.items())
        print(f"{label:40s} {s.cagr:8.2%} {s.sharpe:7.2f} {s.max_drawdown:7.2%} {s.trades:7d}")
    best = order[0]
    dsr = deflated_sharpe(series[best], sharpes)
    print(f"\nDeflated Sharpe Ratio of the best of {len(rows)} configurations: {dsr:.0%}")
    print("(the probability its true Sharpe is above zero once the search is accounted for; "
          "below ~95% the 'best' result is plausibly luck)")


def _run_backtest(args: argparse.Namespace, strategies):
    from trading.backtest import Backtester
    from trading.costs import BrokerSwaps, CostModel
    from trading.data import SPECS_FILE, bars_path, load_bars, load_specs
    from trading.models import SymbolSpec

    symbols = _symbols(args)
    specs = load_specs(Path(args.data) / SPECS_FILE)
    specs = {s: specs.get(s) or SymbolSpec.infer(s) for s in symbols}
    helpers = set()
    for spec in specs.values():
        for a, b in ((spec.quote, args.currency), (spec.quote, "USD"), ("USD", args.currency)):
            for pair in (a + b, b + a):
                if a != b and pair not in symbols and bars_path(args.data, pair, args.timeframe).exists():
                    helpers.add(pair)
    bars = load_bars(args.data, list(symbols) + sorted(helpers), args.timeframe)
    if args.timeframe == "D1":
        from trading.data import merge_weekend_bars

        bars = {s: merge_weekend_bars(b) for s, b in bars.items()}
    costs = CostModel()
    if args.spread:
        overrides = dict(item.upper().split("=") for item in args.spread)
        costs = CostModel(spread_pips={**costs.spread_pips, **{k: float(v) for k, v in overrides.items()}})
    if args.commission is not None:
        costs = CostModel(costs.spread_pips, costs.default_spread_pips, costs.slippage_pips, args.commission)
    if args.swaps == "broker" and not any(s.swap_long or s.swap_short for s in specs.values()):
        log.warning("No swap rates in %s (run `fetch` first); backtesting without swaps", Path(args.data) / SPECS_FILE)
    swaps = BrokerSwaps() if args.swaps == "broker" else _NoSwaps()
    return Backtester(
        strategies,
        bars,
        timeframe=args.timeframe,
        symbols=symbols,
        specs=specs,
        costs=costs.scaled(args.cost_multiplier),
        risk=risk_config(args),
        swaps=swaps,
        initial_equity=args.equity,
        account_currency=args.currency,
        start=_parse_date(args.start) if args.start else None,
        end=_parse_date(args.end) if args.end else None,
    ).run()


class _NoSwaps:
    def swap_per_unit(self, spec, direction, price, when):
        return 0.0


def _write_trades(path: Path, trades) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["strategy", "symbol", "side", "units", "entry_time", "entry_price", "exit_time",
                         "exit_price", "gross_pnl", "commission", "swap", "pnl", "r_multiple", "exit_reason"])
        for t in trades:
            writer.writerow([t.strategy, t.symbol, "long" if t.direction > 0 else "short", t.units,
                             t.entry_time.isoformat(), t.entry_price, t.exit_time.isoformat(), t.exit_price,
                             round(t.gross_pnl, 2), round(t.commission, 2), round(t.swap, 2), round(t.pnl, 2),
                             round(t.r_multiple, 3), t.exit_reason])


# -- argument helpers ----------------------------------------------------------


def build_strategies(names: list[str], params: list[str] | None):
    from trading.strategies import create_strategy

    overrides: dict[str, dict[str, str]] = {n: {} for n in names}
    for item in params or []:
        key, sep, value = item.partition("=")
        if not sep:
            raise ValueError(f"--param expects key=value, got {item!r}")
        strategy, dot, field = key.partition(".")
        if dot:
            if strategy not in overrides:
                raise ValueError(f"--param {item}: strategy {strategy!r} is not selected")
            overrides[strategy][field] = value
        elif len(names) == 1:
            overrides[names[0]][key] = value
        else:
            raise ValueError(f"--param {item}: prefix it with the strategy, e.g. {names[0]}.{key}={value}")
    return [create_strategy(n, **overrides[n]) for n in names]


def risk_config(args: argparse.Namespace) -> RiskConfig:
    return RiskConfig(
        risk_per_trade=args.risk_per_trade,
        max_open_risk=args.max_open_risk,
        max_currency_risk=args.max_currency_risk,
        max_positions=args.max_positions,
        max_leverage=args.max_leverage,
        max_daily_loss=args.max_daily_loss,
        max_drawdown=args.max_drawdown,
    )


def _symbols(args: argparse.Namespace) -> list[str]:
    raw = args.symbols or ",".join(default_symbols())
    return [s.strip().upper() for s in raw.split(",") if s.strip()]


def _parse_date(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=timezone.utc)


def build_parser() -> argparse.ArgumentParser:
    from trading.strategies import STRATEGIES

    parser = argparse.ArgumentParser(description="FxPro cTrader Open API bot (demo-only trading).")
    sub = parser.add_subparsers(title="commands")

    quotes = sub.add_parser("quotes", help="stream bid/ask quotes (default command)")
    quotes.add_argument("--symbol", help="symbol to stream (default: CTRADER_SYMBOL)")
    quotes.set_defaults(handler=cmd_quotes)

    def data_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("--symbols", help="comma-separated symbols (default: CTRADER_SYMBOLS or CTRADER_SYMBOL)")
        p.add_argument("--timeframe", default="D1", help="M1 M5 M15 M30 H1 H4 H12 D1 W1 (default: D1)")
        p.add_argument("--data", default="data", help="directory for CSV bars and symbols.json (default: data)")

    def strategy_args(p: argparse.ArgumentParser) -> None:
        names = ", ".join(STRATEGIES)
        p.add_argument("--strategy", action="append", choices=list(STRATEGIES), required=True,
                       help=f"strategy to run, repeatable ({names})")
        p.add_argument("--param", action="append",
                       help="override a parameter: key=value, or strategy.key=value with several strategies")

    def risk_args(p: argparse.ArgumentParser) -> None:
        d = RiskConfig()
        p.add_argument("--risk-per-trade", type=float, default=d.risk_per_trade,
                       help=f"equity fraction lost if a stop is hit (default: {d.risk_per_trade})")
        p.add_argument("--max-open-risk", type=float, default=d.max_open_risk)
        p.add_argument("--max-currency-risk", type=float, default=d.max_currency_risk)
        p.add_argument("--max-positions", type=int, default=d.max_positions)
        p.add_argument("--max-leverage", type=float, default=d.max_leverage)
        p.add_argument("--max-daily-loss", type=float, default=d.max_daily_loss)
        p.add_argument("--max-drawdown", type=float, default=d.max_drawdown)

    def backtest_args(p: argparse.ArgumentParser) -> None:
        data_args(p)
        risk_args(p)
        p.add_argument("--start", help="first date to trade (earlier bars are warm-up), YYYY-MM-DD")
        p.add_argument("--end", help="last date (exclusive), YYYY-MM-DD")
        p.add_argument("--equity", type=float, default=100_000.0, help="starting equity (default: 100000)")
        p.add_argument("--currency", default="USD", help="account currency (default: USD)")
        p.add_argument("--cost-multiplier", type=float, default=1.0, help="scale spread/slippage/commission")
        p.add_argument("--spread", action="append", help="override a spread in pips: SYMBOL=PIPS")
        p.add_argument("--commission", type=float, help="USD per 1M notional per side (default: 35)")
        p.add_argument("--swaps", choices=["broker", "none"], default="broker",
                       help="'broker' applies today's swap rates from symbols.json to all history")

    fetch = sub.add_parser("fetch", help="download history + contract specs from cTrader")
    data_args(fetch)
    fetch.add_argument("--years", type=float, default=10.0, help="history length (default: 10)")
    fetch.add_argument("--start", help="start date YYYY-MM-DD (overrides --years)")
    fetch.set_defaults(handler=cmd_fetch)

    backtest = sub.add_parser("backtest", help="backtest strategies on downloaded bars")
    strategy_args(backtest)
    backtest_args(backtest)
    backtest.add_argument("--trades-csv", help="write the trade list to this CSV")
    backtest.set_defaults(handler=cmd_backtest)

    sweep = sub.add_parser("sweep", help="parameter grid with a Deflated Sharpe Ratio for the winner")
    sweep.add_argument("--strategy", required=True, choices=list(STRATEGIES))
    sweep.add_argument("--grid", action="append", required=True, help="key=v1,v2,... (repeatable)")
    backtest_args(sweep)
    sweep.set_defaults(handler=cmd_sweep)

    trade = sub.add_parser("trade", help="run strategies on the demo account (dry run unless --execute)")
    strategy_args(trade)
    data_args(trade)
    risk_args(trade)
    trade.add_argument("--execute", action="store_true", help="send orders (demo accounts only)")
    trade.add_argument("--poll", type=float, default=60.0, help="seconds between checks for new bars")
    trade.add_argument("--label", default="fxbot", help="position label prefix used to recognise bot trades")
    trade.add_argument("--state", default="state/live_state.json", help="risk and progress state file")
    trade.add_argument("--journal", default="logs/journal.jsonl", help="JSON-lines decision/order journal")
    trade.add_argument("--reset-drawdown", action="store_true", help="clear a max-drawdown halt")
    trade.add_argument("-v", "--verbose", action="store_true")
    trade.set_defaults(handler=cmd_trade)
    return parser


if __name__ == "__main__":
    main()
