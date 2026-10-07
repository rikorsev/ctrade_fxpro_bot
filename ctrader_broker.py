"""cTrader implementation of the ``trading.broker.Broker`` interface."""
from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta, timezone

from ctrader_open_api.messages.OpenApiModelMessages_pb2 import ProtoOATradeSide
from twisted.internet import defer

from ctrader import CTraderClient, CTraderError
from trading.broker import OrderRequest
from trading.costs import BrokerSwaps, CurrencyConverter, SwapCarry
from trading.models import LONG, SHORT, AccountState, Bar, Position, SymbolSpec, timeframe_delta

log = logging.getLogger(__name__)

PRICE_SCALE = 100_000  # cTrader sends prices as integers in 1/100000 units
VOLUME_SCALE = 100  # and volumes in hundredths of a unit
MAX_BARS_PER_REQUEST = 5_000  # the server caps responses at 14,000 bars; stay well below
RATE_CACHE = timedelta(minutes=10)
SWAP_TYPES = {0: "pips", 1: "percent", 2: "points"}
BUY = ProtoOATradeSide.Value("BUY")


def decode_trendbar(bar, digits: int) -> Bar:
    """ProtoOATrendbar -> Bar. Prices are ``low`` plus unsigned deltas, all in 1/100000 units."""
    low = bar.low
    return Bar(
        time=datetime.fromtimestamp(bar.utcTimestampInMinutes * 60, timezone.utc),
        open=round((low + bar.deltaOpen) / PRICE_SCALE, digits),
        high=round((low + bar.deltaHigh) / PRICE_SCALE, digits),
        low=round(low / PRICE_SCALE, digits),
        close=round((low + bar.deltaClose) / PRICE_SCALE, digits),
        volume=float(bar.volume),
    )


def relative_price(distance: float, digits: int) -> int:
    """A price distance as cTrader's relative SL/TP: 1/100000 units, rounded to the symbol's precision."""
    step = 10 ** max(0, 5 - digits)
    return max(int(round(distance * PRICE_SCALE / step)) * step, step)


def to_volume(units: float) -> int:
    return int(round(units * VOLUME_SCALE))


def spec_from_proto(symbol, light, assets: dict[int, str]) -> SymbolSpec:
    """Build a SymbolSpec from ProtoOASymbol + ProtoOALightSymbol."""
    name = light.symbolName.upper()
    triple = symbol.swapRollover3Days if symbol.HasField("swapRollover3Days") else 3
    return SymbolSpec(
        name=name,
        base=assets.get(light.baseAssetId, name[:3]),
        quote=assets.get(light.quoteAssetId, name[3:]),
        digits=symbol.digits,
        pip_position=symbol.pipPosition,
        lot_size=symbol.lotSize / VOLUME_SCALE if symbol.HasField("lotSize") else 100_000.0,
        min_volume=symbol.minVolume / VOLUME_SCALE if symbol.HasField("minVolume") else 1_000.0,
        volume_step=symbol.stepVolume / VOLUME_SCALE if symbol.HasField("stepVolume") else 1_000.0,
        max_volume=symbol.maxVolume / VOLUME_SCALE if symbol.HasField("maxVolume") else None,
        swap_long=symbol.swapLong,
        swap_short=symbol.swapShort,
        swap_type=SWAP_TYPES.get(symbol.swapCalculationType, "pips"),
        triple_swap_weekday=triple - 1 if 1 <= triple <= 7 else 2,  # protocol: MONDAY=1 .. SUNDAY=7
        symbol_id=int(symbol.symbolId),
    )


class CTraderBroker:
    """Broker adapter for a demo cTrader account.

    Positions are tagged with the label ``<label_prefix>:<strategy>``; only
    positions carrying this bot's prefix are reported or managed, so manual
    trades on the same account are left alone.
    """

    def __init__(self, client: CTraderClient, label_prefix: str = "fxbot") -> None:
        self.client = client
        self.label_prefix = label_prefix
        self.account_currency = ""
        self._money_digits = 2
        self._assets: dict[int, str] = {}
        self._light: dict[str, object] = {}
        self._names: dict[int, str] = {}
        self._specs: dict[str, SymbolSpec] = {}
        self._prices: dict[str, tuple[float, datetime]] = {}
        self._carry = SwapCarry(BrokerSwaps(), self._specs)

    # -- start-up -------------------------------------------------------------

    async def load(self, symbols: Iterable[str]) -> None:
        """Load account, asset and symbol metadata. Call after every (re)connect."""
        self._assets = {int(a.assetId): a.name for a in await self.client.assets()}
        trader = await self.client.trader()
        self.account_currency = self._assets.get(int(trader.depositAssetId), "USD")
        self._money_digits = trader.moneyDigits if trader.HasField("moneyDigits") else 2
        light = await self.client.symbols()
        self._light = {s.symbolName.upper(): s for s in light if s.symbolName}
        self._names = {int(s.symbolId): name for name, s in self._light.items()}
        names = [s.upper() for s in symbols]
        missing = [s for s in names if s not in self._light]
        if missing:
            raise CTraderError("SYMBOL_NOT_FOUND", f"not offered on this account: {', '.join(missing)}")
        await self._load_specs(names)
        log.info("Account %s: currency %s, balance %.2f, leverage 1:%g", self.client.account_id,
                 self.account_currency, trader.balance / 10 ** self._money_digits,
                 trader.leverageInCents / 100 if trader.HasField("leverageInCents") else 0)

    async def _load_specs(self, names: Sequence[str]) -> None:
        wanted = {int(self._light[n].symbolId): n for n in names if n not in self._specs and n in self._light}
        if not wanted:
            return
        for symbol in await self.client.symbol_details(wanted):
            name = wanted[int(symbol.symbolId)]
            self._specs[name] = spec_from_proto(symbol, self._light[name], self._assets)

    def symbol_spec(self, symbol: str) -> SymbolSpec:
        return self._specs[symbol.upper()]

    @property
    def specs(self) -> dict[str, SymbolSpec]:
        return dict(self._specs)

    # -- market data ----------------------------------------------------------

    def bars(self, symbol: str, timeframe: str, count: int) -> defer.Deferred:
        return defer.ensureDeferred(self._recent_bars(symbol, timeframe, count))

    async def _recent_bars(self, symbol: str, timeframe: str, count: int) -> list[Bar]:
        end = datetime.now(timezone.utc)
        # Enough calendar time for `count` bars across weekends and holidays.
        start = end - timeframe_delta(timeframe) * count * 2 - timedelta(days=7)
        return (await self._range(symbol, timeframe, start, end, count))[-count:]

    async def history(self, symbol: str, timeframe: str, start: datetime, end: datetime) -> list[Bar]:
        """All bars between ``start`` and ``end``, fetched in chunks below the server's per-request cap."""
        chunk = timeframe_delta(timeframe) * MAX_BARS_PER_REQUEST
        by_time: dict[datetime, Bar] = {}
        t = start
        while t < end:
            upper = min(t + chunk, end)
            for bar in await self._range(symbol, timeframe, t, upper):
                by_time[bar.time] = bar
            t = upper
        return [by_time[k] for k in sorted(by_time)]

    async def _range(self, symbol: str, timeframe: str, start: datetime, end: datetime,
                     count: int | None = None) -> list[Bar]:
        light = self._light[symbol.upper()]
        digits = self._specs[symbol.upper()].digits if symbol.upper() in self._specs else 5
        raw = await self.client.trendbars(int(light.symbolId), timeframe, int(start.timestamp() * 1000),
                                          int(end.timestamp() * 1000), count)
        return sorted((decode_trendbar(b, digits) for b in raw), key=lambda b: b.time)

    def conversion_rate(self, from_ccy: str, to_ccy: str) -> defer.Deferred:
        return defer.ensureDeferred(self._conversion_rate(from_ccy, to_ccy))

    async def _conversion_rate(self, from_ccy: str, to_ccy: str) -> float:
        if from_ccy == to_ccy:
            return 1.0
        pairs = [a + b for a, b in ((from_ccy, to_ccy), (from_ccy, "USD"), ("USD", to_ccy))]
        pairs += [b + a for a, b in ((from_ccy, to_ccy), (from_ccy, "USD"), ("USD", to_ccy))]
        now = datetime.now(timezone.utc)
        for pair in pairs:
            cached = self._prices.get(pair)
            if pair in self._light and pair != "USDUSD" and (cached is None or now - cached[1] > RATE_CACHE):
                bars = await self._recent_bars(pair, "H1", 2)
                if bars:
                    self._prices[pair] = (bars[-1].close, now)
        prices = {pair: price for pair, (price, _) in self._prices.items()}
        return CurrencyConverter(prices.get, prices).rate(from_ccy, to_ccy)

    def annual_carry(self, symbol: str, direction: int, price: float, time: datetime) -> float | None:
        return self._carry.annual_carry(symbol.upper(), direction, price, time)

    # -- account --------------------------------------------------------------

    def account(self) -> defer.Deferred:
        return defer.ensureDeferred(self._account())

    async def _account(self) -> AccountState:
        trader = await self.client.trader()
        digits = trader.moneyDigits if trader.HasField("moneyDigits") else self._money_digits
        balance = trader.balance / 10 ** digits
        equity = balance
        try:
            pnl = await self.client.unrealized_pnl()
            equity += sum(p.netUnrealizedPnL for p in pnl.positionUnrealizedPnL) / 10 ** pnl.moneyDigits
        except CTraderError as exc:
            log.warning("Unrealized P&L unavailable (%s); using balance as equity", exc)
        return AccountState(balance=balance, equity=equity, currency=self.account_currency)

    def positions(self) -> defer.Deferred:
        return defer.ensureDeferred(self._positions())

    async def _positions(self) -> list[Position]:
        response = await self.client.reconcile()
        prefix = self.label_prefix + ":"
        mine = [p for p in response.position if p.tradeData.label.startswith(prefix)]
        await self._load_specs([self._names[p.tradeData.symbolId] for p in mine if p.tradeData.symbolId in self._names])
        out = []
        for p in mine:
            name = self._names.get(p.tradeData.symbolId)
            if name is None:
                log.warning("Position %s is on an unknown symbol id %s", p.positionId, p.tradeData.symbolId)
                continue
            out.append(self._to_position(p, name))
        return out

    def _to_position(self, p, name: str, strategy: str | None = None) -> Position:
        return Position(
            symbol=name,
            strategy=strategy or p.tradeData.label.partition(":")[2],
            direction=LONG if p.tradeData.tradeSide == BUY else SHORT,
            units=p.tradeData.volume / VOLUME_SCALE,
            entry_price=p.price,
            entry_time=datetime.fromtimestamp(p.tradeData.openTimestamp / 1000, timezone.utc),
            stop_loss=p.stopLoss if p.HasField("stopLoss") else None,
            take_profit=p.takeProfit if p.HasField("takeProfit") else None,
            position_id=int(p.positionId),
        )

    # -- orders ---------------------------------------------------------------

    def open_position(self, order: OrderRequest) -> defer.Deferred:
        return defer.ensureDeferred(self._open(order))

    async def _open(self, order: OrderRequest) -> Position:
        spec = self.symbol_spec(order.symbol)
        event = await self.client.market_order(
            symbol_id=spec.symbol_id,
            buy=order.direction == LONG,
            volume=to_volume(order.units),
            relative_stop_loss=relative_price(order.stop_distance, spec.digits),
            relative_take_profit=(
                relative_price(order.take_profit_distance, spec.digits) if order.take_profit_distance else None
            ),
            label=f"{self.label_prefix}:{order.strategy}",
            comment=order.comment,
        )
        return self._to_position(event.position, spec.name, order.strategy)

    def close_position(self, position: Position) -> defer.Deferred:
        return self.client.close_position(position.position_id, to_volume(position.units))

    def amend_stop(self, position: Position, stop_loss: float) -> defer.Deferred:
        spec = self.symbol_spec(position.symbol)
        take_profit = spec.round_price(position.take_profit) if position.take_profit else None
        return self.client.amend_position_sltp(position.position_id, spec.round_price(stop_loss), take_profit)
