"""T-360 — Telegram operator ALERT channel (Lane F, backend-engineer).

This package is the **outbound, read-only alert channel** for the AATS operator.
It consumes the FROZEN control-plane feed (`/api/feed` SnipeEvent stream,
api-contracts.md §6) plus the breaker/state signal and emits human-readable
Telegram alerts for three event classes:

  * FILL          — a position was entered/closed (SnipeEvent action="sniped",
                    or a SnipeEvent carrying realized pnl_pct on close).
  * RUG-AVOIDED   — a launch was vetoed/skipped on red flags or a failed
                    safety/cost gate (SnipeEvent action in {"vetoed","skipped"}
                    with red_flags or a veto_source).
  * BREAKER-TRIP  — the daily-loss circuit breaker latched ARMED -> TRIPPED.

CHANNEL SEPARATION (custody-policy.md §7): this ALERT bot is the SEPARATE
Alertmanager bot (`ALERTMANAGER_TELEGRAM_BOT_TOKEN` /
`ALERTMANAGER_TELEGRAM_CHAT_ID`), NOT the operator *command* bot
(`TELEGRAM_BOT_TOKEN_VAULT_REF`).  T-360 is alerts only.  The command/de-risk
surface (`/status /kill /flatten /pause`) is T-361 and is built separately.

HARD RULES honoured here (non-waivable):
  * This channel is OUTBOUND ONLY — it sends no command and exposes no control.
    It therefore CANNOT increase risk; it can only inform the operator (who may
    then de-risk via the T-361 command channel).  De-risk-only by construction.
  * Real capital DISABLED by default — this service never touches the venue, the
    signer, or the block engine.  It only reads the feed.  DRY-RUN posture is
    surfaced verbatim in the alert text, never overridden.
  * Money is INTEGER lamports / Decimal — formatted to a SOL string for display
    via Decimal, NEVER float arithmetic (NFR-009).
  * NO win-rate / outcome-rate field, target, or symbol anywhere (AC-037).
  * The LLM / slow model is NEVER on this path — this is a formatting + I/O loop.
  * NO secrets in code/logs/images — the bot token is read from the environment
    (.env.example placeholders only) and is REDACTED from every log line and
    every captured/offline message.  A real token never appears in this package.
  * External services (Telegram HTTP API) are INJECTABLE — the TelegramClient is
    a protocol; tests use the deterministic OfflineTelegramClient fake (no
    network, no token required).
"""

from aats.telegram.alerts import (
    AlertCategory,
    AlertMessage,
    format_breaker_trip,
    format_fill,
    format_rug_avoided,
)
from aats.telegram.client import (
    HttpTelegramClient,
    OfflineTelegramClient,
    SentMessage,
    TelegramClient,
)
from aats.telegram.command_config import TelegramCommandConfig
from aats.telegram.commands import (
    CommandReply,
    TelegramCommandHandler,
)
from aats.telegram.config import TelegramAlertConfig, redact_token
from aats.telegram.control_plane_client import (
    PAUSE_TARGET_MODE,
    CommandOutcome,
    ControlPlaneClient,
    HttpControlPlaneClient,
    OfflineControlPlaneClient,
    StatusSnapshot,
)
from aats.telegram.service import (
    BreakerSignal,
    FeedSource,
    TelegramAlertService,
)

__all__ = [
    # T-360 — alert channel
    "AlertCategory",
    "AlertMessage",
    "BreakerSignal",
    "FeedSource",
    "HttpTelegramClient",
    "OfflineTelegramClient",
    "SentMessage",
    "TelegramAlertConfig",
    "TelegramAlertService",
    "TelegramClient",
    "format_breaker_trip",
    "format_fill",
    "format_rug_avoided",
    "redact_token",
    # T-361 — de-risk-only command channel
    "PAUSE_TARGET_MODE",
    "CommandOutcome",
    "CommandReply",
    "ControlPlaneClient",
    "HttpControlPlaneClient",
    "OfflineControlPlaneClient",
    "StatusSnapshot",
    "TelegramCommandConfig",
    "TelegramCommandHandler",
]
