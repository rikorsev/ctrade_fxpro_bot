import os
from dataclasses import dataclass

from dotenv import load_dotenv


@dataclass(frozen=True)
class Config:
    client_id: str
    client_secret: str
    access_token: str
    account_id: int | None
    live: bool
    symbol: str
    symbols: tuple[str, ...]


def load_config() -> Config:
    load_dotenv()

    client_id = os.getenv("CTRADER_CLIENT_ID", "").strip()
    client_secret = os.getenv("CTRADER_CLIENT_SECRET", "").strip()
    access_token = os.getenv("CTRADER_ACCESS_TOKEN", "").strip()
    account_id_raw = os.getenv("CTRADER_ACCOUNT_ID", "").strip()
    live = os.getenv("CTRADER_LIVE", "0").strip() == "1"
    symbol = os.getenv("CTRADER_SYMBOL", "EURUSD").strip()

    missing = [
        name for name, value in (
            ("CTRADER_CLIENT_ID", client_id),
            ("CTRADER_CLIENT_SECRET", client_secret),
            ("CTRADER_ACCESS_TOKEN", access_token),
        )
        if not value
    ]
    if missing:
        raise RuntimeError(
            "Missing environment variables: " + ", ".join(missing)
        )

    account_id = int(account_id_raw) if account_id_raw else None

    return Config(
        client_id=client_id,
        client_secret=client_secret,
        access_token=access_token,
        account_id=account_id,
        live=live,
        symbol=symbol,
        symbols=default_symbols(),
    )


def default_symbols() -> tuple[str, ...]:
    """CTRADER_SYMBOLS (comma-separated), falling back to CTRADER_SYMBOL."""
    load_dotenv()
    raw = os.getenv("CTRADER_SYMBOLS", "").strip() or os.getenv("CTRADER_SYMBOL", "EURUSD").strip()
    return tuple(s.strip().upper() for s in raw.split(",") if s.strip())
