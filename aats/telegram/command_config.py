"""T-361 — Telegram operator COMMAND bot configuration (env-only; NO real secrets).

This is the configuration for the **command / de-risk** bot — a SEPARATE bot
from the T-360 alert bot (custody-policy.md §7).  It carries:

  * the operator user-ID allowlist (`TELEGRAM_OPERATOR_USER_IDS`, OQ-004, AC-043),
  * the Vault *reference* to the command bot token
    (`TELEGRAM_BOT_TOKEN_VAULT_REF`) — NEVER the token itself in the env schema,
  * the operator Bearer token the bot presents to the control plane's POST
    endpoints (`OPERATOR_API_TOKEN`, a Vault ref offline), and
  * the DRY-RUN posture mirror (read-only display; never used to enable anything).

SECRET DISCIPLINE (non-waivable):
  * The env schema for the command bot is a **Vault reference**, not key material
    (custody-policy.md §7 / §4).  `.env.example` carries placeholders only.
  * `redact_token` (shared from `config.py`) is the single chokepoint so a token
    can never reach a log line, an exception, or a captured/offline message.
  * No real token, user-ID secret, or API token ever appears in this module.

AUTHZ POLICY (custody-policy.md §7):
  * ONLY a sender whose Telegram user-ID is in the allowlist may issue a command.
    An unlisted sender gets NO control-plane call (the update is dropped + logged).
  * The user-ID check is NECESSARY, NOT SUFFICIENT — `/kill` and `/flatten` add a
    per-command confirm bound to the SAME operator user-ID.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from aats.telegram.config import _looks_like_placeholder, redact_token


def _parse_operator_ids(raw: str | None) -> frozenset[int]:
    """Parse a comma/space-separated operator user-ID list into ints.

    Placeholders (angle-bracketed) and empty entries yield an EMPTY allowlist —
    which is the safe default: with no configured operator, EVERY command is
    rejected (fail-closed).  Non-numeric entries are ignored (never crash on a
    malformed env), keeping only valid integer user IDs.
    """
    if not raw:
        return frozenset()
    out: set[int] = set()
    # Accept commas and/or whitespace as separators.
    for tok in raw.replace(",", " ").split():
        tok = tok.strip()
        if not tok or _looks_like_placeholder(tok):
            continue
        try:
            out.add(int(tok))
        except ValueError:
            # A malformed user-ID is dropped rather than crashing config load.
            # An unparseable allowlist must never accidentally widen access.
            continue
    return frozenset(out)


@dataclass(frozen=True)
class TelegramCommandConfig:
    """Configuration for the Telegram operator COMMAND bot (T-361).

    Loaded from the environment (`.env.example` schema) via `from_env`.  All
    secret-shaped fields are Vault references offline; the real token is injected
    only at runtime and is NEVER logged in the clear.  In tests, construct this
    directly with placeholder refs + an explicit operator-ID set and inject an
    offline control-plane client — no real secret is ever required.

    Fields:
      operator_user_ids: the SINGLE operator allowlist (AC-043).  Empty => no
                         sender is authorized (fail-closed default).
      bot_token_vault_ref: Vault reference to the command bot token (NOT the
                         token).  Redacted in logs.
      operator_api_token: the Bearer token the bot presents to control-plane
                         POSTs (Vault ref offline).  Redacted in logs.
      control_plane_url: base URL of the control plane the commands route to.
      dry_run_enabled:   read-only mirror of the hard DRY-RUN flag — surfaced in
                         /status text; NEVER used to enable anything.
      enabled:           False when the token ref / API token are placeholders
                         (offline/dev) — the bot then runs against the offline
                         control-plane client and never reaches a real endpoint.
    """

    operator_user_ids: frozenset[int]
    bot_token_vault_ref: str
    operator_api_token: str
    control_plane_url: str
    dry_run_enabled: bool = True
    enabled: bool = field(default=False)

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> TelegramCommandConfig:
        """Build config from the environment (TELEGRAM_* / OPERATOR_API_TOKEN keys).

        `env` is injectable for testing (defaults to `os.environ`).  When the bot
        token ref or operator API token is a placeholder/unset, `enabled` is
        False so the bot uses the offline control-plane client — no real network,
        no real secret.  DRY-RUN defaults True (real capital disabled by default).
        """
        src = env if env is not None else dict(os.environ)
        operator_ids = _parse_operator_ids(src.get("TELEGRAM_OPERATOR_USER_IDS"))
        token_ref = src.get("TELEGRAM_BOT_TOKEN_VAULT_REF", "")
        api_token = src.get("OPERATOR_API_TOKEN", "")
        control_plane_url = src.get("CONTROL_PLANE_URL", "http://localhost:8787")
        dry_run = src.get("DRY_RUN_ENABLED", "true").strip().lower() != "false"
        enabled = not (
            _looks_like_placeholder(token_ref) or _looks_like_placeholder(api_token)
        )
        return cls(
            operator_user_ids=operator_ids,
            bot_token_vault_ref=token_ref,
            operator_api_token=api_token,
            control_plane_url=control_plane_url,
            dry_run_enabled=dry_run,
            enabled=enabled,
        )

    def is_operator(self, user_id: int | None) -> bool:
        """True iff `user_id` is the configured operator (allowlist membership).

        A None / non-int user-ID is NEVER an operator.  With an empty allowlist,
        this is always False (fail-closed: no operator configured => no command).
        """
        if user_id is None or isinstance(user_id, bool) or not isinstance(user_id, int):
            return False
        return user_id in self.operator_user_ids

    def safe_repr(self) -> str:
        """A log-safe one-line description (token refs redacted, IDs counted only).

        The operator user IDs themselves are NOT echoed — a user ID is a low-grade
        secret (guessable but not for logging), so only the count is surfaced.
        """
        return (
            f"TelegramCommandConfig(operator_count={len(self.operator_user_ids)}, "
            f"bot_token_vault_ref={redact_token(self.bot_token_vault_ref)}, "
            f"operator_api_token={redact_token(self.operator_api_token)}, "
            f"control_plane_url={self.control_plane_url!r}, "
            f"dry_run_enabled={self.dry_run_enabled}, enabled={self.enabled})"
        )
