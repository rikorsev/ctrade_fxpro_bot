from __future__ import annotations

from config import load_config
from ctrader import CTraderClient
from twisted.internet import reactor


def on_quote(symbol: str, bid: float | None, ask: float | None) -> None:
    print(f"{symbol:8s} bid={bid!s:>12} ask={ask!s:>12}")


def main() -> None:
    config = load_config()

    mode = "LIVE" if config.live else "DEMO"
    print(f"Starting FxPro cTrader client ({mode})")

    if config.live:
        print("WARNING: live mode enabled")

    client = CTraderClient(
        client_id=config.client_id,
        client_secret=config.client_secret,
        access_token=config.access_token,
        account_id=config.account_id,
        live=config.live,
        symbol=config.symbol,
        on_quote=on_quote,
    )

    client.start()
    reactor.run()


if __name__ == "__main__":
    main()
