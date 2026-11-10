import hashlib
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

log = logging.getLogger(__name__)

# Renewed access/refresh tokens are saved here (state/ is gitignored).
TOKEN_FILE = Path(__file__).resolve().parent / "state" / "ctrader_token.json"


@dataclass(frozen=True)
class Config:
    client_id: str
    client_secret: str
    access_token: str
    refresh_token: str  # "" when not configured
    account_id: int | None
    live: bool
    symbol: str
    symbols: tuple[str, ...]

    def tokens(self, path: Path | None = TOKEN_FILE) -> "TokenStore":
        return TokenStore(self.access_token, self.refresh_token, path)


def load_config() -> Config:
    load_dotenv()

    client_id = os.getenv("CTRADER_CLIENT_ID", "").strip()
    client_secret = os.getenv("CTRADER_CLIENT_SECRET", "").strip()
    access_token = os.getenv("CTRADER_ACCESS_TOKEN", "").strip()
    refresh_token = os.getenv("CTRADER_REFRESH_TOKEN", "").strip()
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
        refresh_token=refresh_token,
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


class TokenStore:
    """The access/refresh token pair in use.

    cTrader access tokens expire after about 30 days. ``CTraderClient`` renews an
    expired one with the refresh token and calls ``save``, which writes the new
    pair to ``path`` so that restarts (and other commands) keep using it. The
    saved pair is used only while CTRADER_ACCESS_TOKEN is still the token it was
    renewed from: once a new token is put in .env, the saved pair is ignored.
    """

    def __init__(self, access_token: str, refresh_token: str = "", path: Path | None = None) -> None:
        self.path = path
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.expires_at: datetime | None = None
        self._origin = hashlib.sha256(access_token.encode()).hexdigest()[:16]
        self.reload()

    def reload(self) -> bool:
        """Adopt the pair saved by this or another process. Returns True if the access token changed."""
        if self.path is None or not self.path.exists():
            return False
        try:
            saved = json.loads(self.path.read_text())
            if saved.get("origin") != self._origin:
                return False
            access, refresh = str(saved["access_token"]), str(saved["refresh_token"])
            expires_at = datetime.fromisoformat(saved["expires_at"]) if saved.get("expires_at") else None
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            log.warning("Ignoring the saved token in %s: %s", self.path, exc)
            return False
        changed = access != self.access_token
        self.access_token, self.refresh_token, self.expires_at = access, refresh, expires_at
        return changed

    def save(self, access_token: str, refresh_token: str, expires_in: int) -> None:
        self.access_token, self.refresh_token = access_token, refresh_token
        self.expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
        if self.path is None:
            return
        data = {"origin": self._origin, "access_token": access_token, "refresh_token": refresh_token,
                "expires_at": self.expires_at.isoformat(timespec="seconds")}
        tmp = self.path.with_name(self.path.name + ".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            os.fchmod(fd, 0o600)  # also when a leftover file existed
            with os.fdopen(fd, "w") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp, self.path)
        except OSError as exc:
            # The old refresh token is already used up: without this file a restart needs a new token pair.
            log.error("Could not save the renewed access token to %s (%s). This run keeps working, but after a "
                      "restart you will need to generate a new token.", self.path, exc)
