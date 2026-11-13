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
    ProtoOADealListByPositionIdReq,
    ProtoOAErrorRes,
    ProtoOAExecutionEvent,
    ProtoOAGetAccountListByAccessTokenReq,
    ProtoOAGetPositionUnrealizedPnLReq,
    ProtoOAGetTrendbarsReq,
    ProtoOANewOrderReq,
    ProtoOAOrderErrorEvent,
    ProtoOAReconcileReq,
    ProtoOARefreshTokenReq,
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
from twisted.python.failure import Failure

from config import TokenStore

log = logging.getLogger(__name__)

# The library sends at most 5 queued messages per second, so a request can
# wait in the queue for a while before it even reaches the server.
REQUEST_TIMEOUT = 30
FILL_TIMEOUT = 60
RETRY_DELAY = 10  # seconds before reconnecting after a failed login or start-up callback
TOKEN_ERRORS = {"CH_ACCESS_TOKEN_INVALID", "OA_AUTH_TOKEN_EXPIRED"}  # expired or revoked access token
FATAL_ERRORS = {"CH_CLIENT_AUTH_FAILURE", "NO_ACCOUNT", "TOKEN_REFRESH_FAILED"} | TOKEN_ERRORS

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

    Account requests fail at once with ``NOT_READY`` while the client is not
    logged in, and requests still waiting for an answer fail with
    ``DISCONNECTED`` when the connection drops.

    When the server rejects the access token as expired, the login renews it
    with the refresh token and saves the new pair in the ``TokenStore``.

    Order-related methods refuse to run when connected to the live host,
    unless ``allow_live_orders`` is set.
    """

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        tokens: TokenStore | str,
        account_id: int | None,
        live: bool,
        on_spot: Callable[[int, float | None, float | None], None] | None = None,
        on_fatal: Callable[[str], None] | None = None,
        allow_live_orders: bool = False,
    ) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.tokens = tokens if isinstance(tokens, TokenStore) else TokenStore(tokens)
        self.account_id = account_id
        self.live = live
        self.allow_live_orders = allow_live_orders
        self.on_spot = on_spot
        self.on_fatal = on_fatal
        self.ready = False
        self.can_trade: bool | None = None  # from the access token's scope; None until known
        self.clock = reactor

        host = EndPoints.PROTOBUF_LIVE_HOST if live else EndPoints.PROTOBUF_DEMO_HOST
        self.client = Client(host, EndPoints.PROTOBUF_PORT, TcpProtocol)
        self._ready_callbacks: list[Callable[[CTraderClient], Any]] = []
        self._pending_orders: dict[str, defer.Deferred] = {}
        self._in_flight: set[defer.Deferred] = set()
        # Bumped on every connect and disconnect. A login or start-up callback that began in an
        # older session belongs to a closed connection, and its failure must not restart the new one.
        self._session = 0

        self.client.setConnectedCallback(self._on_connected)
        self.client.setDisconnectedCallback(self._on_disconnected)
        self.client.setMessageReceivedCallback(self._on_message)

    def on_ready(self, callback: Callable[[CTraderClient], Any]) -> None:
        """Run ``callback(client)`` after every successful (re)authentication. It may return a Deferred or coroutine.

        If it fails while the connection is still up, the client reconnects after ``RETRY_DELAY``
        seconds, so the callback runs again.
        """
        self._ready_callbacks.append(callback)

    def start(self) -> None:
        self.client.startService()

    def stop(self) -> None:
        self.client.stopService()

    # -- connection and authentication ------------------------------------

    def _on_connected(self, client) -> None:
        log.info("Connected to cTrader Open API (%s host)", "LIVE" if self.live else "demo")
        self._session += 1
        session = self._session
        d = defer.ensureDeferred(self._authenticate(session))
        d.addErrback(self._on_auth_failure, session)

    async def _authenticate(self, session: int | None = None) -> None:
        session = self._session if session is None else session
        await self._login_request(
            session, ProtoOAApplicationAuthReq(clientId=self.client_id, clientSecret=self.client_secret)
        )
        log.info("Application authenticated")
        try:
            await self._authorize_account(session)
        except CTraderError as exc:
            if exc.code not in TOKEN_ERRORS:
                raise
            await self._renew_token(session, exc)
            await self._authorize_account(session)
        self._check_session(session)
        self.ready = True
        for callback in self._ready_callbacks:
            self._run_callback(callback, session)

    async def _authorize_account(self, session: int) -> None:
        response = await self._login_request(
            session, ProtoOAGetAccountListByAccessTokenReq(accessToken=self.tokens.access_token)
        )
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

        await self._login_request(
            session, ProtoOAAccountAuthReq(ctidTraderAccountId=self.account_id, accessToken=self.tokens.access_token)
        )
        log.info("Trading account authenticated: %s", self.account_id)

    async def _renew_token(self, session: int, error: CTraderError) -> None:
        """Replace a rejected access token, or raise if that is impossible."""
        if self.tokens.reload():
            log.warning("Access token rejected (%s); using the token another run has already renewed", error)
            return
        if not self.tokens.refresh_token:
            raise CTraderError(error.code, f"{error.description or error.code}. Set CTRADER_REFRESH_TOKEN so the bot "
                                           "can renew expired tokens, or generate a new access token")
        log.warning("Access token rejected (%s); renewing it with the refresh token", error)
        try:
            response = await self._login_request(session, ProtoOARefreshTokenReq(refreshToken=self.tokens.refresh_token))
        except CTraderError as exc:
            if exc.code == "DISCONNECTED":
                raise
            raise CTraderError("TOKEN_REFRESH_FAILED", f"the refresh token was rejected ({exc}); "
                                                       "generate a new access and refresh token") from exc
        self.tokens.save(response.accessToken, response.refreshToken, response.expiresIn)
        log.info("Access token renewed; valid until %s UTC", f"{self.tokens.expires_at:%Y-%m-%d %H:%M}")

    async def _login_request(self, session: int, message):
        self._check_session(session)
        response = await self.request(message)
        self._check_session(session)
        return response

    def _check_session(self, session: int) -> None:
        if session != self._session:
            raise CTraderError("DISCONNECTED", "the connection closed during the login")

    def _run_callback(self, callback: Callable[[CTraderClient], Any], session: int) -> None:
        try:
            result = callback(self)
            if inspect.iscoroutine(result):
                result = defer.ensureDeferred(result)
            if isinstance(result, defer.Deferred):
                result.addErrback(self._on_callback_failure, session)
        except Exception:
            self._on_callback_failure(Failure(), session)

    def _on_callback_failure(self, failure: Failure, session: int) -> None:
        if session != self._session:
            log.warning("Start-up callback interrupted by a disconnect (%s); it runs again after the next login",
                        _describe(failure.value))
            return
        log.error("Start-up callback failed; reconnecting in %d s to retry it: %s", RETRY_DELAY,
                  failure.getTraceback())
        self._restart_later(session)

    def _on_auth_failure(self, failure: Failure, session: int) -> None:
        if session != self._session:
            # That connection is gone and the next one logs in afresh. Restarting now would drop the new one.
            log.debug("Ignoring a login failure from a closed connection: %s", _describe(failure.value))
            return
        error = failure.value
        log.error("Authentication failed: %s", _describe(error))
        if getattr(error, "code", "") in FATAL_ERRORS:
            self._fatal(f"authentication failed ({error}); check the client ID, secret and tokens")
        else:
            log.info("Reconnecting in %d s", RETRY_DELAY)
            self._restart_later(session)

    def _restart_later(self, session: int) -> None:
        def restart() -> None:
            if session == self._session:  # skip if a reconnect has already replaced that connection
                self._restart()

        self.clock.callLater(RETRY_DELAY, restart)

    def _restart(self) -> None:
        d = defer.maybeDeferred(self.client.stopService)
        d.addBoth(lambda _: self.client.startService())

    def _fatal(self, reason: str) -> None:
        self.stop()
        if self.on_fatal is not None:
            self.on_fatal(reason)

    def _on_disconnected(self, client, reason) -> None:
        self.ready = False
        self._session += 1
        message = reason.getErrorMessage() if hasattr(reason, "getErrorMessage") else reason
        if self.client.running:
            log.warning("Disconnected: %s (reconnecting automatically)", message)
        else:
            log.info("Disconnected: %s", message)
        # The library forgets unanswered requests on a disconnect without failing them, so each
        # one would hang until its timeout. Fail them now.
        in_flight, self._in_flight = self._in_flight, set()
        for d in in_flight:
            if not d.called:
                d.errback(CTraderError("DISCONNECTED", "the connection closed before the server answered"))
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
            log.warning("Server disconnected trading account %s; logging in again", self.account_id)
            self.ready = False
            self._restart()
            return
        if payload_type == _CLIENT_DISCONNECT:
            log.warning("Server is closing the connection: %s", Protobuf.extract(message).reason)
            return
        if payload_type == _TOKEN_INVALIDATED:
            event = Protobuf.extract(message)
            if event.ctidTraderAccountIds and self.account_id not in event.ctidTraderAccountIds:
                return
            # Expired, revoked, or renewed by another run. The next login renews it or stops the bot.
            log.warning("Server invalidated the access token (%s); logging in again", event.reason or "no reason given")
            self.ready = False
            self._restart()
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
        # A unique message ID per request. The library's default, id() of the response Deferred, can be
        # reused after a request times out, and a late answer would then go to the wrong request.
        d = self.client.send(message, clientMsgId=uuid.uuid4().hex, responseTimeoutInSeconds=timeout)
        self._in_flight.add(d)
        d.addBoth(self._answered, d)
        d.addCallback(_unwrap)
        return d

    def _answered(self, result, d: defer.Deferred):
        self._in_flight.discard(d)
        return result

    def _not_ready(self) -> defer.Deferred | None:
        # Sent while logged out, a request would go out on the next connection ahead of the login.
        if not self.ready or self.account_id is None:
            return defer.fail(CTraderError("NOT_READY", "not logged in to the trading account (reconnecting)"))
        return None

    def _account_request(self, message_type, **fields) -> defer.Deferred:
        return self._not_ready() or self.request(message_type(ctidTraderAccountId=self.account_id, **fields))

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

    def position_deals(self, position_id: int, from_ms: int, to_ms: int) -> defer.Deferred:
        """ProtoOADeal list of one position, executed between the two times. ``to_ms`` must not be in the future."""
        return self._account_request(ProtoOADealListByPositionIdReq, positionId=position_id,
                                     fromTimestamp=from_ms, toTimestamp=to_ms).addCallback(lambda r: list(r.deal))

    def subscribe_spots(self, symbol_ids: Iterable[int]) -> defer.Deferred:
        return self._account_request(ProtoOASubscribeSpotsReq, symbolId=list(symbol_ids))

    # -- trading (demo accounts; live only with allow_live_orders) ----------

    def _trading_blocked(self) -> defer.Deferred | None:
        if self.live and not self.allow_live_orders:
            return defer.fail(
                CTraderError("LIVE_TRADING_DISABLED", "orders to live accounts need allow_live_orders (trade --allow-live)")
            )
        if self.can_trade is False:
            return defer.fail(
                CTraderError("NO_TRADING_SCOPE", "the access token was issued without the 'trading' scope")
            )
        return self._not_ready()

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
        filled.addTimeout(FILL_TIMEOUT, self.clock)
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


def _describe(error: BaseException) -> str:
    if isinstance(error, defer.TimeoutError):
        return "the server did not answer in time"
    return str(error) or type(error).__name__


def _unwrap(message):
    payload = Protobuf.extract(message)
    if message.payloadType in _ERROR_TYPES:
        raise CTraderError(payload.errorCode, getattr(payload, "description", ""))
    return payload
