"""T-360 — Telegram ALERT channel configuration (env-only; NO real secrets).

The bot token + chat id are read from the environment using the schema declared
in `.env.example` (Alertmanager bot, custody-policy.md §7).  This module NEVER
contains a real token; `.env.example` carries placeholders only.

REDACTION: `redact_token` is the single chokepoint that guarantees a bot token
never reaches a log line, an exception message, or an offline-captured message.
Every log statement and every error in this package routes any token-bearing
string through `redact_token` first.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

# A Telegram bot token has the shape "<bot_id>:<35-char-secret>".  We redact the
# secret portion, keeping only the public bot id + a short fingerprint so an
# operator can tell *which* token is configured without the secret ever leaking.
_TOKEN_KEEP_PREFIX = 6  # keep at most this many chars of the leading bot-id


def redact_token(value: str | None) -> str:
    """Return a log-safe representation of a bot token (or any secret-ish string).

    A real bot token NEVER appears in a log, an exception, or a captured message.
    The redaction keeps only the public bot-id prefix (before the ':') so the
    operator can identify the configured bot, and masks everything after it.

    Examples:
        "123456:AAFooBarBaz..."  -> "123456:<redacted>"
        "placeholder-no-colon"   -> "<redacted>"
        ""                       -> "<unset>"
        None                     -> "<unset>"
    """
    if not value:
        return "<unset>"
    # Placeholder values from .env.example (angle-bracketed) are not secrets, but
    # we still never echo them verbatim into logs — treat as unset-shaped.
    if value.startswith("<") and value.endswith(">"):
        return "<placeholder>"
    if ":" in value:
        bot_id, _, _ = value.partition(":")
        bot_id = bot_id[:_TOKEN_KEEP_PREFIX]
        return f"{bot_id}:<redacted>"
    return "<redacted>"


def _looks_like_placeholder(value: str | None) -> bool:
    """True if the value is an unset/placeholder (angle-bracketed or empty)."""
    if not value:
        return True
    v = value.strip()
    return v == "" or (v.startswith("<") and v.endswith(">"))


@dataclass(frozen=True)
class TelegramAlertConfig:
    """Configuration for the Telegram ALERT channel (T-360).

    Loaded from the environment (`.env.example` schema) via `from_env`.  The
    `bot_token` is held only in memory for the lifetime of the process and is
    NEVER logged in the clear (use `redact_token`).  In tests, construct this
    directly with placeholder values and inject an OfflineTelegramClient — no
    real token is ever required to exercise the service.

    Fields:
      bot_token:   Alertmanager bot token (ALERTMANAGER_TELEGRAM_BOT_TOKEN).
                   May be a Vault ref / placeholder offline; redacted in logs.
      chat_id:     Destination chat id (ALERTMANAGER_TELEGRAM_CHAT_ID).
      dry_run_enabled: mirror of the hard DRY-RUN flag — surfaced in alert text,
                   never used to enable anything (read-only posture display).
      enabled:     False when token/chat are placeholders (offline/dev) — the
                   service then runs against the offline client and never tries
                   a real send.  This is what makes the package importable and
                   testable with no real secret present.
    """

    bot_token: str
    chat_id: str
    dry_run_enabled: bool = True
    enabled: bool = field(default=False)

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> TelegramAlertConfig:
        """Build config from the environment (ALERTMANAGER_TELEGRAM_* keys).

        `env` is injectable for testing (defaults to `os.environ`).  When the
        token or chat id is a placeholder/unset, `enabled` is False so the
        service uses the offline client — no real network, no real secret.
        """
        src = env if env is not None else dict(os.environ)
        token = src.get("ALERTMANAGER_TELEGRAM_BOT_TOKEN", "")
        chat_id = src.get("ALERTMANAGER_TELEGRAM_CHAT_ID", "")
        dry_run = src.get("DRY_RUN_ENABLED", "true").strip().lower() != "false"
        enabled = not (_looks_like_placeholder(token) or _looks_like_placeholder(chat_id))
        return cls(
            bot_token=token,
            chat_id=chat_id,
            dry_run_enabled=dry_run,
            enabled=enabled,
        )

    def safe_repr(self) -> str:
        """A log-safe one-line description (token redacted)."""
        return (
            f"TelegramAlertConfig(bot_token={redact_token(self.bot_token)}, "
            f"chat_id={self.chat_id!r}, dry_run_enabled={self.dry_run_enabled}, "
            f"enabled={self.enabled})"
        )
