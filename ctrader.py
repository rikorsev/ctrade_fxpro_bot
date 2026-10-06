from __future__ import annotations

from typing import Callable

from ctrader_open_api import Client, EndPoints, Protobuf, TcpProtocol
from ctrader_open_api.messages.OpenApiCommonMessages_pb2 import (
    ProtoHeartbeatEvent,
)
from ctrader_open_api.messages.OpenApiMessages_pb2 import (
    ProtoOAAccountAuthReq,
    ProtoOAAccountAuthRes,
    ProtoOAApplicationAuthReq,
    ProtoOAApplicationAuthRes,
    ProtoOAGetAccountListByAccessTokenReq,
    ProtoOAGetAccountListByAccessTokenRes,
    ProtoOASymbolsListReq,
    ProtoOASymbolsListRes,
    ProtoOASpotEvent,
    ProtoOASubscribeSpotsReq,
)
from twisted.internet import reactor


class CTraderClient:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        access_token: str,
        account_id: int | None,
        live: bool,
        symbol: str,
        on_quote: Callable[[str, float | None, float | None], None],
    ):
        self.client_id = client_id
        self.client_secret = client_secret
        self.access_token = access_token
        self.account_id = account_id
        self.live = live
        self.symbol_name = symbol
        self.on_quote = on_quote

        host = (
            EndPoints.PROTOBUF_LIVE_HOST
            if live
            else EndPoints.PROTOBUF_DEMO_HOST
        )
        self.client = Client(
            host,
            EndPoints.PROTOBUF_PORT,
            TcpProtocol,
        )
        self.symbol_id: int | None = None
        self.symbol_digits: int = 5
        self.symbol_name_by_id: dict[int, str] = {}

        self.client.setConnectedCallback(self._on_connected)
        self.client.setDisconnectedCallback(self._on_disconnected)
        self.client.setMessageReceivedCallback(self._on_message)

    def start(self) -> None:
        self.client.startService()

    def _on_connected(self, client) -> None:
        print("Connected to cTrader Open API")

        request = ProtoOAApplicationAuthReq()
        request.clientId = self.client_id
        request.clientSecret = self.client_secret

        d = self.client.send(request)
        d.addErrback(self._on_error)

    def _on_disconnected(self, client, reason) -> None:
        print(f"Disconnected: {reason}")

    def _on_error(self, failure):
        print(f"API error: {failure}")
        return failure

    def _on_message(self, client, message) -> None:
        payload_type = message.payloadType

        if payload_type == ProtoHeartbeatEvent().payloadType:
            return

        if payload_type == ProtoOAApplicationAuthRes().payloadType:
            print("Application authenticated")

            if self.account_id is not None:
                self._authenticate_account(self.account_id)
                return

            req = ProtoOAGetAccountListByAccessTokenReq()
            req.accessToken = self.access_token
            d = self.client.send(req)
            d.addErrback(self._on_error)
            return

        if payload_type == ProtoOAGetAccountListByAccessTokenRes().payloadType:
            response = Protobuf.extract(message)

            accounts = response.ctidTraderAccount
            if not accounts:
                raise RuntimeError("No trading accounts authorized for this token")

            self.account_id = int(accounts[0].ctidTraderAccountId)
            print(f"Selected account ID: {self.account_id}")

            self._authenticate_account(self.account_id)
            return

        if payload_type == ProtoOAAccountAuthRes().payloadType:
            response = Protobuf.extract(message)
            print(f"Trading account authenticated: {response.ctidTraderAccountId}")
            self._request_symbols()
            return

        if payload_type == ProtoOASymbolsListRes().payloadType:
            response = Protobuf.extract(message)

            for symbol in response.symbol:
                name = getattr(symbol, "symbolName", "")

                self.symbol_name_by_id[int(symbol.symbolId)] = name

                if name.upper() == self.symbol_name.upper():
                    self.symbol_id = int(symbol.symbolId)

                    print(
                        f"Found {name}: "
                        f"symbol_id={self.symbol_id}"
                    )

                    self._subscribe()
                    return

            print(f"Symbol {self.symbol_name!r} not found.")

        if payload_type == ProtoOASpotEvent().payloadType:
            event = Protobuf.extract(message)

            if int(event.symbolId) != self.symbol_id:
                return

            bid = (
                event.bid / 100000.0
                if event.HasField("bid")
                else None
            )
            ask = (
                event.ask / 100000.0
                if event.HasField("ask")
                else None
            )

            self.on_quote(self.symbol_name, bid, ask)
            return

    def _authenticate_account(self, account_id: int) -> None:
        request = ProtoOAAccountAuthReq()
        request.ctidTraderAccountId = account_id
        request.accessToken = self.access_token

        d = self.client.send(request)
        d.addErrback(self._on_error)

    def _request_symbols(self) -> None:
        request = ProtoOASymbolsListReq()
        request.ctidTraderAccountId = self.account_id

        d = self.client.send(request)
        d.addErrback(self._on_error)

    def _subscribe(self) -> None:
        if self.symbol_id is None:
            raise RuntimeError("Cannot subscribe: symbol ID is unknown")

        request = ProtoOASubscribeSpotsReq()
        request.ctidTraderAccountId = self.account_id
        request.symbolId.append(self.symbol_id)

        d = self.client.send(request)
        d.addErrback(self._on_error)

        print(f"Subscribed to {self.symbol_name}")
