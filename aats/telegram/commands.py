"""T-361 — Telegram operator COMMAND handlers (de-risk ONLY).

This is the command surface bound to the FROZEN control-plane contract
(api-contracts.md §7 / §10, custody-policy.md §7).  It exposes EXACTLY:

    /status            — read agent state + metrics (no confirm)
    /kill              — halt all entries + flatten ALL (PER-COMMAND CONFIRM)
    /flatten <mint>    — flatten one position       (PER-COMMAND CONFIRM)
    /pause             — step agent mode DOWN to SHADOW (no confirm; already de-risk)

THE FOUR NON-WAIVABLE INVARIANTS THIS MODULE ENFORCES:

  1. AUTHZ — operator-ID-only (custody-policy.md §7, AC-043).  ONLY a sender whose
     Telegram user-ID is in the configured allowlist may issue a command.  An
     unlisted sender gets NO control-plane call: the update is DROPPED and the
     attempt is LOGGED (without echoing the body).  The user-ID check is the
     FIRST gate on every command — it runs before any routing, confirm, or call.

  2. PER-COMMAND CONFIRM on /kill and /flatten (custody-policy.md §7, AC-042).
     The first invocation does NOT fire the API; it returns a confirm prompt
     carrying a short-lived, single-use nonce bound to the SAME operator user-ID
     and the SAME command.  Only a matching confirm (from that user-ID, that
     nonce, unexpired) fires the control-plane call.  A leaked chat_id alone
     cannot fire a de-risk command unattended.

  3. DE-RISK ONLY — structurally.  The command REGISTRY is a closed, fixed map of
     four commands; there is NO size-up, stop-widen, leverage, breaker-reset,
     risk-config, or "go live" command, and none can be added at runtime.  Each
     command routes ONLY to a de-risk/read method on the ControlPlaneClient seam
     (which itself exposes only de-risk/read — control_plane_client.py).  `/pause`
     posts a hard-coded downward mode; no command takes a "direction" or "amount"
     that could widen risk.  A risk-increasing command is not "rejected" — it
     DOES NOT EXIST.

  4. MONEY is integer lamports / decimal-string formatted via Decimal, NEVER
     float (NFR-009); and there is NO win-rate / outcome-rate field anywhere in
     the /status output (AC-037).

INJECTION: the control-plane client + a monotonic clock are injected seams with
deterministic offline fakes (control_plane_client.py).  No Redis/RPC/Telegram is
reachable here; the bot is driven by `handle_update(update)` with plain dicts.
"""

from __future__ import annotations

import logging
import secrets
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from aats.telegram.alerts import format_sol_from_lamports
from aats.telegram.command_config import TelegramCommandConfig
from aats.telegram.control_plane_client import (
    ControlPlaneClient,
    StatusSnapshot,
)

log = logging.getLogger(__name__)

# How long a pending confirm nonce stays valid (seconds).  Short-lived so a
# stale/leaked confirm token cannot fire a de-risk command long after the fact.
_CONFIRM_TTL_S = 60.0
# Bound the pending-confirm table so a flood of /kill prompts cannot grow memory
# unboundedly (one operator, but defense-in-depth against a hostile-but-allowed
# sender spamming confirm prompts).
_MAX_PENDING_CONFIRMS = 64


# ---------------------------------------------------------------------------
# The de-risk-only command set (a CLOSED enum — nothing can be added at runtime)
# ---------------------------------------------------------------------------

# Commands that fire a destructive de-risk action and therefore REQUIRE a
# per-command operator confirm (custody-policy.md §7, AC-042).
_CONFIRM_REQUIRED = frozenset({"kill", "flatten"})

# The complete, fixed set of command verbs.  Anything not here is unknown and is
# silently ignored (no error surface to a hostile probe).  There is, by design,
# NO command verb that increases risk.
_KNOWN_COMMANDS = frozenset({"status", "kill", "flatten", "pause"})


@dataclass(frozen=True)
class _PendingConfirm:
    """A short-lived, single-use confirm token bound to operator + command."""

    nonce: str
    user_id: int
    command: str
    arg: str | None
    created_at: float


@dataclass(frozen=True)
class CommandReply:
    """The bot's reply to one update.

    `text` is the operator-facing message (secret-free).  `fired` is True iff a
    control-plane de-risk/read call was actually made (tests assert this).
    `confirm_nonce` is set when a confirm prompt was issued (the operator echoes
    it back via `/confirm <nonce>`).  `authorized` records whether the sender
    passed the operator-ID gate — False means NO control-plane call was made.
    """

    text: str
    fired: bool
    authorized: bool
    confirm_nonce: str | None = None


# A monotonic clock seam (injectable for deterministic TTL tests).
Clock = Callable[[], float]


class TelegramCommandHandler:
    """The de-risk-only operator command dispatcher.

    Construct with an injected ControlPlaneClient (offline fake in tests) and the
    command config (operator allowlist).  Drive it with `handle_update(update)` —
    a plain Telegram-update-shaped dict; no real Telegram client is needed.
    """

    def __init__(
        self,
        *,
        config: TelegramCommandConfig,
        client: ControlPlaneClient,
        clock: Clock = time.monotonic,
        confirm_ttl_s: float = _CONFIRM_TTL_S,
    ) -> None:
        self._config = config
        self._client = client
        self._clock = clock
        self._confirm_ttl_s = confirm_ttl_s
        # Pending confirms keyed by nonce (bounded, FIFO-evicted, single-use).
        self._pending: OrderedDict[str, _PendingConfirm] = OrderedDict()

    # ------------------------------------------------------------------
    # Update entry point
    # ------------------------------------------------------------------

    async def handle_update(self, update: dict[str, Any]) -> CommandReply | None:
        """Process one Telegram update; return a reply or None (ignored update).

        AUTHZ is the FIRST thing checked: the sender's user-ID must be in the
        operator allowlist, else the update is dropped (no control-plane call)
        and the rejection is logged WITHOUT echoing the message body.
        """
        message = update.get("message") if isinstance(update, dict) else None
        if not isinstance(message, dict):
            return None
        text = message.get("text")
        if not isinstance(text, str) or not text.strip():
            return None

        user_id = self._extract_user_id(message)

        # GATE 1 — operator-ID allowlist (necessary, runs before everything).
        if not self._config.is_operator(user_id):
            # Drop silently; log the attempt WITHOUT the body (no info leak, no
            # control-plane call).  A non-operator can never reach a command.
            log.warning(
                "telegram-command: REJECTED unauthorized sender user_id=%s (dropped, no API call)",
                _safe_user_id(user_id),
            )
            return CommandReply(
                text="",  # nothing is sent back to a non-operator
                fired=False,
                authorized=False,
            )

        # Authorized operator from here on.
        verb, arg = _parse_command(text)
        if verb is None:
            return None  # not a command; ignore

        if verb == "confirm":
            return await self._handle_confirm(user_id, arg)  # type: ignore[arg-type]

        if verb not in _KNOWN_COMMANDS:
            # Unknown command from an operator: a quiet, de-risk-irrelevant reply.
            return CommandReply(
                text="unknown command. available: /status /kill /flatten <mint> /pause",
                fired=False,
                authorized=True,
            )

        return await self._dispatch(user_id, verb, arg)  # type: ignore[arg-type]

    # ------------------------------------------------------------------
    # Dispatch a known command
    # ------------------------------------------------------------------

    async def _dispatch(self, user_id: int, verb: str, arg: str | None) -> CommandReply:
        if verb == "status":
            return await self._do_status()

        if verb == "pause":
            # Already de-risk (mode steps DOWN only) — no confirm required.
            return await self._do_pause()

        if verb in _CONFIRM_REQUIRED:
            # /kill and /flatten require a per-command confirm: the FIRST call
            # issues a prompt and does NOT fire the API.
            if verb == "flatten" and not arg:
                return CommandReply(
                    text="usage: /flatten <mint> — a mint is required (per-mint de-risk).",
                    fired=False,
                    authorized=True,
                )
            return self._issue_confirm(user_id, verb, arg)

        # Unreachable (verb is in _KNOWN_COMMANDS), but fail-closed.
        return CommandReply(text="unsupported command.", fired=False, authorized=True)

    # ------------------------------------------------------------------
    # Per-command confirm (kill / flatten)
    # ------------------------------------------------------------------

    def _issue_confirm(self, user_id: int, command: str, arg: str | None) -> CommandReply:
        """Create a single-use confirm nonce bound to (operator, command, arg).

        Does NOT fire the control-plane call — that only happens on a matching
        `/confirm <nonce>` from the SAME operator within the TTL.
        """
        self._evict_expired()
        nonce = secrets.token_hex(8)
        pending = _PendingConfirm(
            nonce=nonce,
            user_id=user_id,
            command=command,
            arg=arg,
            created_at=self._clock(),
        )
        self._pending[nonce] = pending
        # Bound the table (FIFO-evict oldest) — defense against confirm-spam.
        while len(self._pending) > _MAX_PENDING_CONFIRMS:
            self._pending.popitem(last=False)

        target = command if arg is None else f"{command} {arg}"
        log.info("telegram-command: confirm issued for %s (nonce live %.0fs)", command, self._confirm_ttl_s)
        return CommandReply(
            text=(
                f"⚠️ CONFIRM REQUIRED — this will DE-RISK: {target}.\n"
                f"reply `/confirm {nonce}` within {int(self._confirm_ttl_s)}s to proceed.\n"
                f"this command can only de-risk; it never increases exposure."
            ),
            fired=False,
            authorized=True,
            confirm_nonce=nonce,
        )

    async def _handle_confirm(self, user_id: int, nonce: str | None) -> CommandReply:
        """Validate a confirm nonce and, if valid, fire the bound de-risk call."""
        self._evict_expired()
        if not nonce:
            return CommandReply(
                text="usage: /confirm <nonce> (from the confirm prompt).",
                fired=False,
                authorized=True,
            )
        pending = self._pending.get(nonce)
        if pending is None:
            return CommandReply(
                text="confirm token invalid or expired. re-issue the command.",
                fired=False,
                authorized=True,
            )
        # Bind the confirm to the SAME operator who requested it.  A different
        # (even allowlisted) user cannot consume another's confirm token.
        if pending.user_id != user_id:
            log.warning("telegram-command: confirm user mismatch (dropped)")
            return CommandReply(
                text="confirm token does not belong to you.",
                fired=False,
                authorized=True,
            )
        if self._clock() - pending.created_at > self._confirm_ttl_s:
            self._pending.pop(nonce, None)
            return CommandReply(
                text="confirm token expired. re-issue the command.",
                fired=False,
                authorized=True,
            )

        # Single-use: consume the nonce BEFORE firing so a replay cannot re-fire.
        self._pending.pop(nonce, None)

        if pending.command == "kill":
            return await self._do_kill()
        if pending.command == "flatten":
            return await self._do_flatten(pending.arg or "")
        # Closed set — nothing else is confirmable.
        return CommandReply(text="nothing to confirm.", fired=False, authorized=True)

    # ------------------------------------------------------------------
    # The four de-risk/read actions (each routes to exactly one client method)
    # ------------------------------------------------------------------

    async def _do_status(self) -> CommandReply:
        try:
            snap = await self._client.get_status()
        except Exception as exc:  # noqa: BLE001 — never leak internals to the operator
            log.warning("telegram-command: status read failed: %s", type(exc).__name__)
            return CommandReply(
                text="status unavailable (control plane unreachable).",
                fired=True,
                authorized=True,
            )
        return CommandReply(text=_format_status(snap), fired=True, authorized=True)

    async def _do_kill(self) -> CommandReply:
        outcome = await self._client.kill()
        verb = "✅" if outcome.ok else "⚠️"
        return CommandReply(
            text=f"{verb} KILL — {outcome.detail}", fired=True, authorized=True
        )

    async def _do_flatten(self, mint: str) -> CommandReply:
        outcome = await self._client.flatten_mint(mint)
        verb = "✅" if outcome.ok else "⚠️"
        return CommandReply(
            text=f"{verb} FLATTEN — {outcome.detail}", fired=True, authorized=True
        )

    async def _do_pause(self) -> CommandReply:
        outcome = await self._client.pause()
        verb = "✅" if outcome.ok else "⚠️"
        return CommandReply(
            text=f"{verb} PAUSE — {outcome.detail}", fired=True, authorized=True
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _extract_user_id(self, message: dict[str, Any]) -> int | None:
        frm = message.get("from")
        if not isinstance(frm, dict):
            return None
        uid = frm.get("id")
        if isinstance(uid, bool) or not isinstance(uid, int):
            return None
        return uid

    def _evict_expired(self) -> None:
        now = self._clock()
        expired = [
            n for n, p in self._pending.items() if now - p.created_at > self._confirm_ttl_s
        ]
        for n in expired:
            self._pending.pop(n, None)

    @property
    def pending_confirm_count(self) -> int:
        """Number of live (un-expired) pending confirms (tests / telemetry)."""
        self._evict_expired()
        return len(self._pending)


# ---------------------------------------------------------------------------
# Module-level pure helpers (no I/O, no secrets)
# ---------------------------------------------------------------------------


def _parse_command(text: str) -> tuple[str | None, str | None]:
    """Parse a Telegram command line into (verb, arg).

    Handles `/cmd`, `/cmd arg`, and `/cmd@botname arg` (Telegram appends the bot
    username in group chats).  Returns (None, None) for non-command text.
    """
    stripped = text.strip()
    if not stripped.startswith("/"):
        return None, None
    parts = stripped[1:].split(maxsplit=1)
    if not parts:
        return None, None
    verb = parts[0].lower()
    # Strip a `@botname` suffix if present.
    if "@" in verb:
        verb = verb.split("@", 1)[0]
    arg = parts[1].strip() if len(parts) > 1 else None
    return verb, (arg or None)


def _safe_user_id(user_id: int | None) -> str:
    """A log-safe user-ID rendering.

    A Telegram user ID is a low-grade secret (guessable, but we do not splash it
    into logs in full).  We keep only a short fingerprint so an operator can
    correlate repeated rejections without publishing the full ID.
    """
    if user_id is None:
        return "<none>"
    s = str(user_id)
    if len(s) <= 4:
        return "…" + s[-2:]
    return "…" + s[-4:]


def _format_status(snap: StatusSnapshot) -> str:
    """Format a /status reply.  Money via Decimal (NEVER float).  No win-rate.

    `wallet_balance_lamports` is integer lamports rendered through the shared
    Decimal formatter; the decimal-string PnL fields are shown VERBATIM off the
    wire.  There is deliberately NO win-rate / outcome-rate line (AC-037).
    """
    posture = "DRY-RUN (no real capital)" if snap.dry_run_enabled else "LIVE"
    halted = []
    if snap.killed:
        halted.append("KILLED")
    if snap.breaker_tripped:
        halted.append("BREAKER-TRIPPED")
    halt_line = ", ".join(halted) if halted else "running"
    bal = format_sol_from_lamports(snap.wallet_balance_lamports)
    lines = [
        "📊 STATUS",
        f"mode: {snap.mode}  posture: {posture}",
        f"state: {halt_line}",
        f"open positions: {snap.open_positions}",
        f"net pnl: {snap.net_pnl_sol} SOL",
        f"daily pnl: {snap.daily_pnl_sol} SOL  (limit {snap.daily_loss_limit_sol} SOL)",
        f"wallet: {bal}",
        f"geyser: {'up' if snap.geyser_connected else 'down'}",
    ]
    return "\n".join(lines)
