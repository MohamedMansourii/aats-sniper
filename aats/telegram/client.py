"""T-360 — Injectable Telegram client (offline fake + real HTTP impl).

The Telegram HTTP API is an EXTERNAL service and is NOT reachable in this
environment.  Per the build directive it is INJECTABLE behind a small protocol:

  * `TelegramClient`        — the seam the service depends on.
  * `OfflineTelegramClient` — deterministic in-memory fake used by every test;
                              captures sent messages, NO network, NO token.
  * `HttpTelegramClient`    — the real adapter (httpx → api.telegram.org).  It is
                              constructed only at runtime with a real token; it
                              is never exercised in tests and never embeds a token.

SECRET DISCIPLINE: the bot token is held only inside the client instance and is
NEVER logged.  `OfflineTelegramClient` records the *text* and *chat_id* of each
send but NEVER the token.  `HttpTelegramClient` routes the token only into the
request URL at call time and redacts it from every log line via `redact_token`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from aats.telegram.config import redact_token

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SentMessage:
    """A message handed to the Telegram client (token never stored here)."""

    chat_id: str
    text: str


@runtime_checkable
class TelegramClient(Protocol):
    """The minimal outbound seam the alert service depends on.

    `send_message` is async and idempotent from the caller's view: a transport
    failure raises so the service can decide to drop or retry, but a successful
    send has no side effect beyond delivering the text.  Implementations MUST
    NOT log the bot token in the clear.
    """

    async def send_message(self, chat_id: str, text: str) -> None: ...


@dataclass
class OfflineTelegramClient:
    """Deterministic offline fake — the default in tests and DRY-RUN/dev.

    Captures every send in `sent` (an ordered list) so tests can assert that an
    alert fired and was formatted correctly.  NO network, NO token, NO secret.

    `fail_next` can be set to simulate a transient transport failure for the
    next send (the service's error handling is then exercised offline).
    """

    sent: list[SentMessage] = field(default_factory=list)
    fail_next: bool = False

    async def send_message(self, chat_id: str, text: str) -> None:
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("offline-telegram: simulated transient send failure")
        self.sent.append(SentMessage(chat_id=chat_id, text=text))
        # Log WITHOUT the token and WITHOUT the chat content secrets — just a count.
        log.debug("offline-telegram: captured message #%d (len=%d)", len(self.sent), len(text))

    @property
    def texts(self) -> list[str]:
        """Convenience: the ordered text bodies of every captured message."""
        return [m.text for m in self.sent]

    def reset(self) -> None:
        self.sent.clear()
        self.fail_next = False


class HttpTelegramClient:
    """Real Telegram Bot API adapter (httpx).  Constructed at runtime only.

    NOT exercised in tests (no network here).  The token lives only in this
    instance; it is placed into the request URL at call time and is redacted
    from every log line.  A bounded timeout is enforced on every call so a hung
    Telegram endpoint cannot stall the alert loop indefinitely.
    """

    _API_BASE = "https://api.telegram.org"

    def __init__(self, bot_token: str, *, timeout_s: float = 10.0) -> None:
        if not bot_token:
            raise ValueError("HttpTelegramClient requires a bot token (runtime-injected).")
        self._token = bot_token
        self._timeout_s = timeout_s
        # Import httpx lazily so the package imports offline without the dep
        # resolved into the hot path (tests never reach this branch).
        import httpx

        self._httpx = httpx
        self._client = httpx.AsyncClient(timeout=timeout_s)

    async def send_message(self, chat_id: str, text: str) -> None:
        url = f"{self._API_BASE}/bot{self._token}/sendMessage"
        try:
            resp = await self._client.post(
                url,
                json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
            )
            resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001 — re-raised after redaction
            # NEVER let the URL (which contains the token) reach a log/exception.
            raise RuntimeError(
                f"telegram send failed (bot {redact_token(self._token)}): "
                f"{type(exc).__name__}"
            ) from None

    async def aclose(self) -> None:
        await self._client.aclose()
