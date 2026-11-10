"""cTrader client and broker adapter, exercised with real protobuf messages and a fake transport."""
import json
from datetime import datetime, timezone

import pytest
from ctrader_open_api.messages.OpenApiCommonMessages_pb2 import ProtoMessage
from ctrader_open_api.messages.OpenApiMessages_pb2 import (
    ProtoOAAccountAuthRes,
    ProtoOAAccountsTokenInvalidatedEvent,
    ProtoOAApplicationAuthRes,
    ProtoOAErrorRes,
    ProtoOAExecutionEvent,
    ProtoOAGetAccountListByAccessTokenRes,
    ProtoOAReconcileRes,
    ProtoOARefreshTokenRes,
    ProtoOATraderRes,
)
from ctrader_open_api.messages.OpenApiModelMessages_pb2 import (
    ProtoOACtidTraderAccount,
    ProtoOALightSymbol,
    ProtoOAOrder,
    ProtoOAPosition,
    ProtoOASymbol,
    ProtoOATradeData,
    ProtoOATrendbar,
)
from twisted.internet import defer, task

from config import TokenStore
from ctrader import CTraderClient
from ctrader_broker import CTraderBroker, decode_trendbar, relative_price, spec_from_proto, to_volume
from trading.broker import OrderRequest
from trading.models import LONG, SHORT


def wrap(payload, client_msg_id=None) -> ProtoMessage:
    return ProtoMessage(payloadType=payload.payloadType, payload=payload.SerializeToString(),
                        clientMsgId=client_msg_id)


class FakeTransport:
    """Stands in for ctrader_open_api.Client: records requests, lets the test answer them."""

    running = True

    def __init__(self):
        self.sent = []
        self.restarts = 0

    def send(self, message, responseTimeoutInSeconds=5, **_):
        d = defer.Deferred()
        self.sent.append((message, d))
        return d

    def stopService(self):
        self.restarts += 1

    def startService(self):
        pass


def make_client(live=False, account_id=123, tokens="token", ready=True) -> tuple[CTraderClient, FakeTransport]:
    """A client wired to a fake transport; ``ready`` pretends the login has already happened."""
    client = CTraderClient("id", "secret", tokens, account_id, live, on_fatal=lambda reason: fatal.append(reason))
    fatal: list[str] = []
    client.fatal = fatal
    client.ready = ready
    client.clock = task.Clock()
    transport = FakeTransport()
    client.client = transport
    return client, transport


def account_list(scope: int) -> ProtoOAGetAccountListByAccessTokenRes:
    return ProtoOAGetAccountListByAccessTokenRes(accessToken="token", permissionScope=scope, ctidTraderAccount=[
        ProtoOACtidTraderAccount(ctidTraderAccountId=50, isLive=True),
        ProtoOACtidTraderAccount(ctidTraderAccountId=49, isLive=False),
    ])


def authenticate(client, transport, responses):
    """Run the auth flow, answering each request in turn; returns the flow's Deferred."""
    d = defer.ensureDeferred(client._authenticate())
    for i, response in enumerate(responses):
        transport.sent[i][1].callback(wrap(response))
    return d


def test_auth_picks_demo_account_and_detects_view_only_scope():
    client, transport = make_client(account_id=None, ready=False)
    ready = []
    client.on_ready(ready.append)
    authenticate(client, transport, [ProtoOAApplicationAuthRes(), account_list(scope=0),
                                     ProtoOAAccountAuthRes(ctidTraderAccountId=49)])
    assert client.account_id == 49 and client.can_trade is False and ready == [client]
    sent_before = len(transport.sent)
    failures = []
    client.market_order(symbol_id=1, buy=True, volume=100_000, relative_stop_loss=100).addErrback(failures.append)
    assert failures[0].value.code == "NO_TRADING_SCOPE" and len(transport.sent) == sent_before


def test_auth_rejects_an_account_the_token_does_not_cover():
    client, transport = make_client(account_id=50, ready=False)  # 50 is a live account; this client is on the demo host
    failures = []
    authenticate(client, transport, [ProtoOAApplicationAuthRes(), account_list(scope=1)]).addErrback(failures.append)
    assert failures[0].value.code == "NO_ACCOUNT" and client.can_trade is True


def failures_of(d):
    out = []
    d.addErrback(out.append)
    return out


def test_requests_fail_at_once_while_logged_out():
    client, transport = make_client(ready=False)
    assert failures_of(client.trader())[0].value.code == "NOT_READY"
    assert failures_of(client.market_order(symbol_id=1, buy=True, volume=100, relative_stop_loss=10))[0].value.code \
        == "NOT_READY"
    assert transport.sent == []  # nothing queued to go out ahead of the next login


def test_disconnect_fails_unanswered_requests_and_stale_logins_do_not_restart():
    client, transport = make_client()
    pending = failures_of(client.trader())
    client._on_connected(None)  # a reconnect starts a login on the new connection
    assert len(transport.sent) == 2
    client._on_disconnected(None, "connection lost")
    # Both fail now, not after the 30 s timeout, and the dead login does not schedule a restart.
    assert pending[0].value.code == "DISCONNECTED"
    assert client.clock.getDelayedCalls() == [] and client.fatal == [] and not client.ready
    # The next connection logs in afresh.
    client._on_connected(None)
    assert len(transport.sent) == 3 and transport.restarts == 0


def test_failed_login_reconnects_unless_already_reconnected():
    client, transport = make_client(ready=False)
    client._on_connected(None)
    transport.sent[0][1].errback(defer.TimeoutError(30, "Deferred"))
    client.clock.advance(10)
    assert transport.restarts == 1 and client.fatal == []

    client._on_connected(None)
    transport.sent[1][1].errback(defer.TimeoutError(30, "Deferred"))
    client._on_disconnected(None, "closed")  # the library reconnected meanwhile
    client.clock.advance(10)
    assert transport.restarts == 1


def test_failed_start_up_callback_reconnects_to_retry_it():
    client, transport = make_client(account_id=49, ready=False)
    client.on_ready(lambda c: defer.fail(defer.TimeoutError(30, "Deferred")))
    authenticate(client, transport, [ProtoOAApplicationAuthRes(), account_list(scope=1),
                                     ProtoOAAccountAuthRes(ctidTraderAccountId=49)])
    client.clock.advance(10)
    assert transport.restarts == 1


def expired():
    return ProtoOAErrorRes(errorCode="CH_ACCESS_TOKEN_INVALID", description="Access token expired")


def test_expired_access_token_is_renewed_and_saved(tmp_path):
    path = tmp_path / "token.json"
    client, transport = make_client(account_id=49, ready=False, tokens=TokenStore("old", "refresh-1", path))
    authenticate(client, transport, [
        ProtoOAApplicationAuthRes(), expired(),
        ProtoOARefreshTokenRes(accessToken="new", tokenType="bearer", expiresIn=2_628_000, refreshToken="refresh-2"),
        account_list(scope=1), ProtoOAAccountAuthRes(ctidTraderAccountId=49),
    ])
    sent = [m for m, _ in transport.sent]
    assert sent[2].refreshToken == "refresh-1"
    assert sent[3].accessToken == "new" and sent[4].accessToken == "new"
    assert client.ready and client.fatal == []

    assert path.stat().st_mode & 0o777 == 0o600
    assert json.loads(path.read_text())["refresh_token"] == "refresh-2"
    restarted = TokenStore("old", "refresh-1", path)  # the next run, with the same .env
    assert (restarted.access_token, restarted.refresh_token) == ("new", "refresh-2")
    assert restarted.expires_at is not None
    replaced = TokenStore("pasted-later", "refresh-x", path)  # a new token in .env wins over the saved one
    assert (replaced.access_token, replaced.refresh_token) == ("pasted-later", "refresh-x")


def test_token_renewed_by_another_run_is_picked_up(tmp_path):
    path = tmp_path / "token.json"
    tokens = TokenStore("old", "refresh-1", path)
    TokenStore("old", "refresh-1", path).save("new", "refresh-2", 3600)  # e.g. `fetch` renewed it meanwhile
    client, transport = make_client(account_id=49, ready=False, tokens=tokens)
    authenticate(client, transport, [ProtoOAApplicationAuthRes(), expired(), account_list(scope=1),
                                     ProtoOAAccountAuthRes(ctidTraderAccountId=49)])
    assert transport.sent[2][0].accessToken == "new" and client.ready  # no refresh request was needed


def test_expired_token_stops_the_bot_without_a_refresh_token():
    client, transport = make_client(ready=False)
    client._on_connected(None)
    for response in (ProtoOAApplicationAuthRes(), expired()):
        transport.sent[-1][1].callback(wrap(response))
    (reason,) = client.fatal
    assert "CTRADER_REFRESH_TOKEN" in reason and len(transport.sent) == 2


def test_rejected_refresh_token_stops_the_bot():
    client, transport = make_client(ready=False, tokens=TokenStore("old", "used-up"))
    client._on_connected(None)
    for response in (ProtoOAApplicationAuthRes(), expired(), ProtoOAErrorRes(errorCode="ACCESS_DENIED")):
        transport.sent[-1][1].callback(wrap(response))
    (reason,) = client.fatal
    assert "TOKEN_REFRESH_FAILED" in reason


def test_invalidated_token_logs_in_again_instead_of_stopping():
    client, transport = make_client()
    client._on_message(None, wrap(ProtoOAAccountsTokenInvalidatedEvent(ctidTraderAccountIds=[999], reason="other")))
    assert transport.restarts == 0  # another account
    client._on_message(None, wrap(ProtoOAAccountsTokenInvalidatedEvent(ctidTraderAccountIds=[123], reason="expired")))
    assert transport.restarts == 1 and client.fatal == [] and not client.ready


def test_decode_trendbar_and_unit_conversions():
    minutes = int(datetime(2024, 1, 2, tzinfo=timezone.utc).timestamp() // 60)
    tb = ProtoOATrendbar(volume=42, low=109_950, deltaOpen=60, deltaHigh=120, deltaClose=80,
                         utcTimestampInMinutes=minutes)
    bar = decode_trendbar(tb, digits=5)
    assert (bar.open, bar.high, bar.low, bar.close) == (1.1001, 1.1007, 1.0995, 1.1003)
    assert bar.time == datetime(2024, 1, 2, tzinfo=timezone.utc) and bar.volume == 42
    assert relative_price(0.012346, digits=5) == 1235
    assert relative_price(1.2367, digits=3) == 123_700  # multiples of 100 for 3-digit symbols
    assert relative_price(0.0, digits=5) == 1
    assert to_volume(50_000) == 5_000_000


def test_spec_from_proto():
    symbol = ProtoOASymbol(symbolId=1, digits=3, pipPosition=2, lotSize=10_000_000, minVolume=100_000,
                           stepVolume=100_000, maxVolume=1_000_000_000, swapLong=-1.2, swapShort=0.4,
                           swapRollover3Days=3)
    light = ProtoOALightSymbol(symbolId=1, symbolName="usdjpy", baseAssetId=10, quoteAssetId=20)
    spec = spec_from_proto(symbol, light, {10: "USD", 20: "JPY"})
    assert (spec.name, spec.base, spec.quote, spec.pip_size) == ("USDJPY", "USD", "JPY", 0.01)
    assert (spec.lot_size, spec.min_volume, spec.volume_step) == (100_000, 1_000, 1_000)
    assert spec.triple_swap_weekday == 2 and spec.swap_long == pytest.approx(-1.2)


def test_error_responses_become_exceptions():
    client, transport = make_client()
    d = client.trader()
    (_, pending), = transport.sent
    pending.callback(wrap(ProtoOAErrorRes(errorCode="NOT_ENOUGH_MONEY", description="margin")))
    failure = []
    d.addErrback(failure.append)
    assert failure[0].value.code == "NOT_ENOUGH_MONEY"


def test_successful_response_is_decoded():
    client, transport = make_client()
    d = client.trader()
    from ctrader_open_api.messages.OpenApiModelMessages_pb2 import ProtoOATrader
    res = ProtoOATraderRes(ctidTraderAccountId=123,
                           trader=ProtoOATrader(ctidTraderAccountId=123, balance=1_000_000, depositAssetId=1))
    transport.sent[0][1].callback(wrap(res))
    result = []
    d.addCallback(result.append)
    assert result[0].balance == 1_000_000


def test_live_client_refuses_to_trade():
    client, transport = make_client(live=True)
    failures = []
    client.market_order(symbol_id=1, buy=True, volume=100_000, relative_stop_loss=100).addErrback(failures.append)
    client.close_position(1, 100).addErrback(failures.append)
    client.amend_position_sltp(1, 1.1, None).addErrback(failures.append)
    assert [f.value.code for f in failures] == ["LIVE_TRADING_DISABLED"] * 3
    assert transport.sent == []


def trade_data(label="fxbot:donchian", side=1, volume=5_000_000):
    return ProtoOATradeData(symbolId=1, volume=volume, tradeSide=side, openTimestamp=1_700_000_000_000, label=label)


def execution(client_order_id, execution_type=3):
    order = ProtoOAOrder(orderId=9, tradeData=trade_data(), orderType=1, orderStatus=2,
                         clientOrderId=client_order_id)
    position = ProtoOAPosition(positionId=55, tradeData=trade_data(), positionStatus=1, swap=0,
                               price=1.1002, stopLoss=1.0902)
    return ProtoOAExecutionEvent(ctidTraderAccountId=123, executionType=execution_type, order=order,
                                 position=position)


def test_market_order_resolves_on_fill_event():
    client, transport = make_client()
    filled = []
    client.market_order(symbol_id=1, buy=True, volume=5_000_000, relative_stop_loss=1000,
                        label="fxbot:donchian").addCallback(filled.append)
    (request, ack), = transport.sent
    assert request.relativeStopLoss == 1000 and request.label == "fxbot:donchian"
    coid = request.clientOrderId
    client._on_message(None, wrap(execution(coid, execution_type=2)))  # ORDER_ACCEPTED
    assert filled == []
    client._on_message(None, wrap(execution(coid, execution_type=3)))  # ORDER_FILLED
    assert filled[0].position.positionId == 55
    assert client._pending_orders == {}


def test_rejected_order_fails():
    client, transport = make_client()
    failures = []
    client.market_order(symbol_id=1, buy=False, volume=100_000, relative_stop_loss=10).addErrback(failures.append)
    (request, ack), = transport.sent
    ack.callback(wrap(ProtoOAErrorRes(errorCode="TRADING_BAD_STOPS")))
    assert failures[0].value.code == "TRADING_BAD_STOPS"


class FakeClient:
    """CTraderClient stand-in for the broker adapter."""

    account_id = 123

    def __init__(self, reconcile=None):
        self._reconcile = reconcile
        self.orders = []

    def reconcile(self):
        return defer.succeed(self._reconcile)

    def symbol_details(self, ids):
        return defer.succeed([ProtoOASymbol(symbolId=i, digits=5, pipPosition=4) for i in ids])

    def market_order(self, **kwargs):
        self.orders.append(kwargs)
        return defer.succeed(execution("x"))


def broker_with(reconcile=None) -> CTraderBroker:
    broker = CTraderBroker(FakeClient(reconcile))
    light = ProtoOALightSymbol(symbolId=1, symbolName="EURUSD", baseAssetId=1, quoteAssetId=2)
    broker._light = {"EURUSD": light}
    broker._names = {1: "EURUSD"}
    broker._assets = {1: "EUR", 2: "USD"}
    return broker


def test_positions_only_include_this_bots_label():
    res = ProtoOAReconcileRes(ctidTraderAccountId=123, position=[
        ProtoOAPosition(positionId=1, tradeData=trade_data("fxbot:tsmom", side=2), positionStatus=1, swap=0,
                        price=1.1, stopLoss=1.12),
        ProtoOAPosition(positionId=2, tradeData=trade_data("manual trade"), positionStatus=1, swap=0, price=1.1),
    ])
    out = []
    broker_with(res).positions().addCallback(out.append)
    (p,) = out[0]
    assert (p.position_id, p.strategy, p.direction, p.units, p.stop_loss) == (1, "tsmom", SHORT, 50_000, 1.12)


def test_open_position_converts_units_and_stops():
    broker = broker_with()
    broker._specs["EURUSD"] = spec_from_proto(ProtoOASymbol(symbolId=1, digits=5, pipPosition=4),
                                              broker._light["EURUSD"], broker._assets)
    out = []
    broker.open_position(OrderRequest("EURUSD", "donchian", LONG, 50_000, 0.0123)).addCallback(out.append)
    (order,) = broker.client.orders
    assert order["volume"] == 5_000_000 and order["relative_stop_loss"] == 1230 and order["buy"]
    assert order["label"] == "fxbot:donchian"
    assert out[0].position_id == 55 and out[0].entry_price == 1.1002
