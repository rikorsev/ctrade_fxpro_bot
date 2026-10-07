from __future__ import annotations

import inspect
import logging
import uuid
from collections.abc import Callable, Iterable
from typing import Any

from ctrader_open_api import Client, EndPoints, Protobuf, TcpProtocol
from ctrader_open_api.messages.OpenApiCommonMessages_pb2 import ProtoErrorRes, ProtoHeartbeatEvent
from ctrader_open_api.messages.OpenApiMessages_pb2 import (
    ProtoOAAccountAuthReq,
    ProtoOAAccountDisconnectEvent,
    ProtoOAAccountsTokenInvalidatedEvent,
    ProtoOAAmendPositionSLTPReq,
    ProtoOAApplicationAuthReq,
    ProtoOAAssetListReq,
    ProtoOAClientDisconnectEvent,
    ProtoOAClosePositionReq,
    ProtoOAErrorRes,
    ProtoOAExecutionEvent,
    ProtoOAGetAccountListByAccessTokenReq,
    ProtoOAGetPositionUnrealizedPnLReq,
    ProtoOAGetTrendbarsReq,
    ProtoOANewOrderReq,
    ProtoOAOrderErrorEvent,
    ProtoOAReconcileReq,
    ProtoOASpotEvent,
    ProtoOASubscribeSpotsReq,
    ProtoOASymbolByIdReq,
    ProtoOASymbolsListReq,
    ProtoOATraderReq,
)
from ctrader_open_api.messages.OpenApiModelMessages_pb2 import (
    ProtoOAClientPermissionScope,
    ProtoOAExecutionType,
    ProtoOAOrderType,
    ProtoOATradeSide,
    ProtoOATrendbarPeriod,
)
from twisted.internet import defer, reactor

log = logging.getLogger(__name__)

# The library sends at most 5 queued messages per second, so a request can
# wait in the queue for a while before it even reaches the server.
REQUEST_TIMEOUT = 30
FILL_TIMEOUT = 60
FATAL_ERRORS = {"CH_CLIENT_AUTH_FAILURE", "CH_ACCESS_TOKEN_INVALID", "OA_AUTH_TOKEN_EXPIRED", "NO_ACCOUNT"}

_ERROR_TYPES = {
    ProtoErrorRes().payloadType,
    ProtoOAErrorRes().payloadType,
    ProtoOAOrderErrorEvent().payloadType,
}
_HEARTBEAT = ProtoHeartbeatEvent().payloadType
_SPOT = ProtoOASpotEvent().payloadType
_EXECUTION = ProtoOAExecutionEvent().payloadType
_ORDER_ERROR = ProtoOAOrderErrorEvent().payloadType
_ACCOUNT_DISCONNECT = ProtoOAAccountDisconnectEvent().payloadType
_CLIENT_DISCONNECT = ProtoOAClientDisconnectEvent().payloadType
_TOKEN_INVALIDATED = ProtoOAAccountsTokenInvalidatedEvent().payloadType
_FILLED = {ProtoOAExecutionType.Value("ORDER_FILLED"), ProtoOAExecutionType.Value("ORDER_PARTIAL_FILL")}
_DEAD = {
    ProtoOAExecutionType.Value("ORDER_REJECTED"),
    ProtoOAExecutionType.Value("ORDER_CANCELLED"),
    ProtoOAExecutionType.Value("ORDER_EXPIRED"),
}
_SCOPE_TRADE = ProtoOAClientPermissionScope.Value("SCOPE_TRADE")


class CTraderError(Exception):
    def __init__(self, code: str, description: str = "") -> None:
        super().__init__(f"{code}: {description}" if description else code)
        self.code = code
        self.description = description


class CTraderClient:
    """Connection, authentication and typed requests for the cTrader Open API.

    Requests return Deferreds that fire with the decoded response payload or
    fail with ``CTraderError`` when the server answers with an error. Run code
    that needs an authenticated account from an ``on_ready`` callback: it runs
    after the first connection and again after every reconnect.

    Order-related methods refuse to run when connected to the live host.
    """

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        access_token: str,
        account_id: int | None,
        live: bool,
        on_spot: Callable[[int, float | None, float | None], None] | None = None,
        on_fatal: Callable[[str], None] | None = None,
    ) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.access_token = access_token
        self.account_id = account_id
        self.live = live
        self.on_spot = on_spot
        self.on_fatal = on_fatal
        self.ready = False
        self.can_trade: bool | None = None  # from the access token's scope; None until known

        host = EndPoints.PROTOBUF_LIVE_HOST if live else EndPoints.PROTOBUF_DEMO_HOST
        self.client = Client(host, EndPoints.PROTOBUF_PORT, TcpProtocol)
        self._ready_callbacks: list[Callable[[CTraderClient], Any]] = []
        self._pending_orders: dict[str, defer.Deferred] = {}

        self.client.setConnectedCallback(self._on_connected)
        self.client.setDisconnectedCallback(self._on_disconnected)
        self.client.setMessageReceivedCallback(self._on_message)

    def on_ready(self, callback: Callable[[CTraderClient], Any]) -> None:
        """Run ``callback(client)`` after every successful (re)authentication. It may return a Deferred or coroutine."""
        self._ready_callbacks.append(callback)

    def start(self) -> None:
        self.client.startService()

    def stop(self) -> None:
        self.client.stopService()

    # -- connection and authentication ------------------------------------

    def _on_connected(self, client) -> None:
        log.info("Connected to cTrader Open API (%s host)", "LIVE" if self.live else "demo")
        d = defer.ensureDeferred(self._authenticate())
        d.addErrback(self._on_auth_failure)

    async def _authenticate(self) -> None:
        await self.request(ProtoOAApplicationAuthReq(clientId=self.client_id, clientSecret=self.client_secret))
        log.info("Application authenticated")

        response = await self.request(ProtoOAGetAccountListByAccessTokenReq(accessToken=self.access_token))
        if response.HasField("permissionScope"):
            self.can_trade = response.permissionScope == _SCOPE_TRADE
            if self.can_trade:
                log.info("Access token scope: trading")
            else:
                log.warning("Access token scope: view only. Quotes and data work, but orders will be refused; "
                            "generate a token with the 'trading' scope to trade")
        # Demo accounts can only be used on the demo host and live ones on the live host.
        kind = "live" if self.live else "demo"
        accounts = [int(a.ctidTraderAccountId) for a in response.ctidTraderAccount if bool(a.isLive) == self.live]
        if self.account_id is None:
            if not accounts:
                raise CTraderError("NO_ACCOUNT", f"no {kind} trading account is authorized for this access token")
            self.account_id = accounts[0]
            log.info("Selected account ID: %s", self.account_id)
        elif self.account_id not in accounts:
            raise CTraderError("NO_ACCOUNT", f"account {self.account_id} is not a {kind} account authorized for this "
                                             "access token")

        await self.request(
            ProtoOAAccountAuthReq(ctidTraderAccountId=self.account_id, accessToken=self.access_token)
        )
        log.info("Trading account authenticated: %s", self.account_id)
        self.ready = True
        for callback in self._ready_callbacks:
            self._run_callback(callback)

    def _run_callback(self, callback: Callable[[CTraderClient], Any]) -> None:
        try:
            result = callback(self)
            if inspect.iscoroutine(result):
                result = defer.ensureDeferred(result)
            if isinstance(result, defer.Deferred):
                result.addErrback(lambda f: log.error("Start-up callback failed: %s", f.getTraceback()))
        except Exception:
            log.exception("Start-up callback failed")

    def _on_auth_failure(self, failure) -> None:
        error = failure.value
        code = getattr(error, "code", "")
        log.error("Authentication failed: %s", error)
        if code in FATAL_ERRORS:
            self._fatal(f"authentication failed ({error}); check the client ID, secret and access token")
        else:
            # Usually a timeout during a reconnect storm: start over on a fresh connection.
            reactor.callLater(10, self._restart)

    def _restart(self) -> None:
        d = defer.maybeDeferred(self.client.stopService)
        d.addBoth(lambda _: self.client.startService())

    def _fatal(self, reason: str) -> None:
        self.stop()
        if self.on_fatal is not None:
            self.on_fatal(reason)

    def _on_disconnected(self, client, reason) -> None:
        self.ready = False
        message = reason.getErrorMessage() if hasattr(reason, "getErrorMessage") else reason
        if self.client.running:
            log.warning("Disconnected: %s (reconnecting automatically)", message)
        else:
            log.info("Disconnected: %s", message)
        for pending in list(self._pending_orders.values()):
            if not pending.called:
                pending.errback(CTraderError("DISCONNECTED", "connection lost before the order was confirmed"))

    # -- incoming messages --------------------------------------------------

    def _on_message(self, client, message) -> None:
        payload_type = message.payloadType
        if payload_type == _HEARTBEAT:
            return
        if payload_type == _SPOT:
            event = Protobuf.extract(message)
            if self.on_spot is not None:
                bid = event.bid / 100000.0 if event.HasField("bid") else None
                ask = event.ask / 100000.0 if event.HasField("ask") else None
                self.on_spot(int(event.symbolId), bid, ask)
            return
        if payload_type == _EXECUTION:
            self._on_execution(Protobuf.extract(message))
            return
        if payload_type == _ORDER_ERROR:
            event = Protobuf.extract(message)
            log.warning("Order error %s: %s (order %s, position %s)", event.errorCode, event.description,
                        event.orderId, event.positionId)
            return
        if payload_type == _ACCOUNT_DISCONNECT:
            log.warning("Server disconnected trading account %s; re-authenticating", self.account_id)
            self.ready = False
            self._restart()
            return
        if payload_type == _CLIENT_DISCONNECT:
            log.warning("Server is closing the connection: %s", Protobuf.extract(message).reason)
            return
        if payload_type == _TOKEN_INVALIDATED:
            self._fatal(f"access token invalidated: {Protobuf.extract(message).reason}")
            return

    def _on_execution(self, event) -> None:
        if not event.HasField("order"):
            return
        pending = self._pending_orders.get(event.order.clientOrderId)
        if pending is None or pending.called:
            return
        if event.executionType in _FILLED:
            pending.callback(event)
        elif event.executionType in _DEAD:
            kind = ProtoOAExecutionType.Name(event.executionType)
            pending.errback(CTraderError(event.errorCode or kind, f"order {event.order.orderId} {kind}"))

    # -- requests -------------------------------------------------------------

    def request(self, message, timeout: int = REQUEST_TIMEOUT) -> defer.Deferred:
        d = self.client.send(message, responseTimeoutInSeconds=timeout)
        d.addCallback(_unwrap)
        return d

    def _account_request(self, message_type, **fields) -> defer.Deferred:
        if self.account_id is None:
            return defer.fail(CTraderError("NOT_READY", "trading account not authenticated yet"))
        return self.request(message_type(ctidTraderAccountId=self.account_id, **fields))

    def symbols(self) -> defer.Deferred:
        """All symbols as ProtoOALightSymbol (name, id, base/quote asset ids)."""
        return self._account_request(ProtoOASymbolsListReq).addCallback(lambda r: list(r.symbol))

    def symbol_details(self, symbol_ids: Iterable[int]) -> defer.Deferred:
        """Full ProtoOASymbol entities (digits, volumes, swaps...)."""
        return self._account_request(ProtoOASymbolByIdReq, symbolId=list(symbol_ids)).addCallback(
            lambda r: list(r.symbol)
        )

    def assets(self) -> defer.Deferred:
        return self._account_request(ProtoOAAssetListReq).addCallback(lambda r: list(r.asset))

    def trader(self) -> defer.Deferred:
        return self._account_request(ProtoOATraderReq).addCallback(lambda r: r.trader)

    def trendbars(self, symbol_id: int, period: str, from_ms: int, to_ms: int, count: int | None = None):
        """ProtoOATrendbar list. At most 14,000 bars come back per request."""
        fields = dict(
            symbolId=symbol_id,
            period=ProtoOATrendbarPeriod.Value(period),
            fromTimestamp=from_ms,
            toTimestamp=to_ms,
        )
        if count is not None:
            fields["count"] = count
        return self._account_request(ProtoOAGetTrendbarsReq, **fields).addCallback(lambda r: list(r.trendbar))

    def reconcile(self) -> defer.Deferred:
        """Open positions and pending orders (ProtoOAReconcileRes)."""
        return self._account_request(ProtoOAReconcileReq)

    def unrealized_pnl(self) -> defer.Deferred:
        return self._account_request(ProtoOAGetPositionUnrealizedPnLReq)

    def subscribe_spots(self, symbol_ids: Iterable[int]) -> defer.Deferred:
        return self._account_request(ProtoOASubscribeSpotsReq, symbolId=list(symbol_ids))

    # -- trading (demo accounts only) ---------------------------------------

    def _trading_blocked(self) -> defer.Deferred | None:
        if self.live:
            return defer.fail(
                CTraderError("LIVE_TRADING_DISABLED", "this bot only sends orders to demo accounts (CTRADER_LIVE=0)")
            )
        if self.can_trade is False:
            return defer.fail(
                CTraderError("NO_TRADING_SCOPE", "the access token was issued without the 'trading' scope")
            )
        return None

    def market_order(
        self,
        *,
        symbol_id: int,
        buy: bool,
        volume: int,
        relative_stop_loss: int,
        relative_take_profit: int | None = None,
        label: str = "",
        comment: str = "",
    ) -> defer.Deferred:
        """Send a market order and fire with the ORDER_FILLED execution event.

        ``volume`` is in hundredths of a unit; relative stops are in 1/100000
        of a price unit, as the protocol requires for market orders.
        """
        refused = self._trading_blocked()
        if refused is not None:
            return refused
        client_order_id = uuid.uuid4().hex[:24]
        request = ProtoOANewOrderReq(
            ctidTraderAccountId=self.account_id,
            symbolId=symbol_id,
            orderType=ProtoOAOrderType.Value("MARKET"),
            tradeSide=ProtoOATradeSide.Value("BUY" if buy else "SELL"),
            volume=volume,
            relativeStopLoss=relative_stop_loss,
            label=label[:100],
            comment=comment[:100],
            clientOrderId=client_order_id,
        )
        if relative_take_profit:
            request.relativeTakeProfit = relative_take_profit

        filled: defer.Deferred = defer.Deferred()
        self._pending_orders[client_order_id] = filled
        filled.addTimeout(FILL_TIMEOUT, reactor)
        filled.addBoth(self._forget_order, client_order_id)

        acknowledgement = self.request(request)
        acknowledgement.addErrback(self._order_rejected, client_order_id)
        return filled

    def _order_rejected(self, failure, client_order_id: str) -> None:
        pending = self._pending_orders.get(client_order_id)
        # A slow acknowledgement is not a failure; the fill timeout decides.
        if pending is not None and not pending.called and failure.check(CTraderError):
            pending.errback(failure)

    def _forget_order(self, result, client_order_id: str):
        self._pending_orders.pop(client_order_id, None)
        return result

    def close_position(self, position_id: int, volume: int) -> defer.Deferred:
        refused = self._trading_blocked()
        if refused is not None:
            return refused
        return self._account_request(ProtoOAClosePositionReq, positionId=position_id, volume=volume)

    def amend_position_sltp(self, position_id: int, stop_loss: float, take_profit: float | None) -> defer.Deferred:
        refused = self._trading_blocked()
        if refused is not None:
            return refused
        fields: dict[str, Any] = dict(positionId=position_id, stopLoss=stop_loss)
        if take_profit is not None:
            fields["takeProfit"] = take_profit  # omitting it would remove an existing take profit
        return self._account_request(ProtoOAAmendPositionSLTPReq, **fields)


def _unwrap(message):
    payload = Protobuf.extract(message)
    if message.payloadType in _ERROR_TYPES:
        raise CTraderError(payload.errorCode, getattr(payload, "description", ""))
    return payload
