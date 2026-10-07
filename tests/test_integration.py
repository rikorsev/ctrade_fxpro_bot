"""The whole live path: LiveTrader -> CTraderBroker -> (fake) CTraderClient, with real protobuf messages."""
from datetime import timedelta

from ctrader_open_api.messages.OpenApiMessages_pb2 import (
    ProtoOAExecutionEvent,
    ProtoOAGetPositionUnrealizedPnLRes,
    ProtoOAReconcileRes,
)
from ctrader_open_api.messages.OpenApiModelMessages_pb2 import (
    ProtoOAAsset,
    ProtoOALightSymbol,
    ProtoOAPosition,
    ProtoOASymbol,
    ProtoOATradeData,
    ProtoOATrader,
    ProtoOATrendbar,
)
from twisted.internet import defer

from ctrader_broker import CTraderBroker
from tests.helpers import Scripted, daily_bars
from trading.live import LiveConfig, LiveTrader
from trading.models import LONG, Signal
from trading.risk import RiskManager

BARS = daily_bars([1.1000, 1.1010, 1.1020, 1.1030, 1.1040])


class FakeCTraderClient:
    account_id = 1

    def __init__(self):
        self.orders, self.closes, self.amends = [], [], []
        self.open_positions = []

    def assets(self):
        return defer.succeed([ProtoOAAsset(assetId=1, name="EUR"), ProtoOAAsset(assetId=2, name="USD")])

    def trader(self):
        return defer.succeed(ProtoOATrader(ctidTraderAccountId=1, balance=10_000_000, depositAssetId=2,
                                           moneyDigits=2))

    def symbols(self):
        return defer.succeed([ProtoOALightSymbol(symbolId=7, symbolName="EURUSD", baseAssetId=1, quoteAssetId=2)])

    def symbol_details(self, ids):
        return defer.succeed([ProtoOASymbol(symbolId=7, digits=5, pipPosition=4, lotSize=10_000_000,
                                            minVolume=100_000, stepVolume=100_000, swapLong=-0.6,
                                            swapShort=0.1)])

    def trendbars(self, symbol_id, period, from_ms, to_ms, count=None):
        assert (symbol_id, period) == (7, "D1")
        raw = [ProtoOATrendbar(volume=1, low=round(b.low * 1e5), deltaOpen=round((b.open - b.low) * 1e5),
                               deltaHigh=round((b.high - b.low) * 1e5), deltaClose=round((b.close - b.low) * 1e5),
                               utcTimestampInMinutes=int(b.time.timestamp() // 60)) for b in BARS]
        return defer.succeed(raw[-count:] if count else raw)

    def reconcile(self):
        return defer.succeed(ProtoOAReconcileRes(ctidTraderAccountId=1, position=self.open_positions))

    def unrealized_pnl(self):
        return defer.succeed(ProtoOAGetPositionUnrealizedPnLRes(ctidTraderAccountId=1, moneyDigits=2))

    def market_order(self, **order):
        self.orders.append(order)
        trade = ProtoOATradeData(symbolId=7, volume=order["volume"], tradeSide=1 if order["buy"] else 2,
                                 openTimestamp=int(BARS[-1].time.timestamp() * 1000), label=order["label"])
        position = ProtoOAPosition(positionId=99, tradeData=trade, positionStatus=1, swap=0, price=1.10412,
                                   stopLoss=1.10412 - order["relative_stop_loss"] / 1e5)
        self.open_positions.append(position)
        return defer.succeed(ProtoOAExecutionEvent(ctidTraderAccountId=1, executionType=3, position=position))


def result_of(coroutine):
    out = []
    defer.ensureDeferred(coroutine).addCallbacks(out.append, lambda f: f.raiseException())
    return out[0]


def test_signal_to_order_through_the_cTrader_adapter(tmp_path):
    client = FakeCTraderClient()
    broker = CTraderBroker(client, label_prefix="fxbot")
    result_of(broker.load(["EURUSD"]))
    assert broker.account_currency == "USD"
    assert broker.symbol_spec("EURUSD").min_volume == 1_000

    strategy = Scripted({BARS[-1].time: Signal(LONG, stop_distance=0.01, reason="breakout")}, lookback=3)
    trader = LiveTrader(
        broker, [strategy], RiskManager(),
        LiveConfig(symbols=("EURUSD",), execute=True, state_path=tmp_path / "s.json"),
        clock=lambda: BARS[-1].time + timedelta(days=1, minutes=5),
    )
    assert result_of(trader.tick())
    (order,) = client.orders
    assert order["volume"] == 5_000_000  # 50,000 units: 0.5% of 100,000 at a 100-pip stop
    assert order["relative_stop_loss"] == 1000 and order["buy"] and order["label"] == "fxbot:scripted"
    (seen_time, window, position, _), = strategy.calls
    assert seen_time == BARS[-1].time and window == 3 and position is None

    # The new position is now reported back, mapped to the strategy that opened it.
    (p,) = result_of(broker._positions())
    assert (p.strategy, p.symbol, p.units, p.position_id) == ("scripted", "EURUSD", 50_000, 99)
