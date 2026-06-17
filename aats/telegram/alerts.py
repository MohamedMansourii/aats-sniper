"""T-360 — Alert classification + formatting (pure, deterministic, no I/O).

This module turns a control-plane `SnipeEvent` (api-contracts.md §6) or a
breaker-trip signal into a human-readable `AlertMessage`.  It is PURE: no
network, no clock dependence beyond what the event carries, no secrets.

MONEY RULE (NFR-009): every monetary quantity is an INTEGER number of base
units (lamports).  Display formatting goes through `format_sol_from_lamports`,
which uses `Decimal` exclusively — there is NO float arithmetic on money in this
module.  A `float` passed where lamports are expected raises immediately.

HONESTY CLAUSE (AC-037): there is NO win-rate / outcome-rate field, target, or
symbol anywhere in these messages.  A `pnl_pct` carried on a SnipeEvent is a
*per-event* realized percentage on a single position (the contract's field), not
an aggregate hit-rate — and it is surfaced as such, labelled "pnl".  We do not
compute, store, or display any rate-of-wins quantity.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal
from enum import StrEnum
from typing import Any

# Lamports per SOL — the only conversion constant.  Integer; used only as a
# Decimal divisor so the result is exact decimal arithmetic, never binary float.
_LAMPORTS_PER_SOL = Decimal(1_000_000_000)
# Display precision for SOL amounts (9 dp = full lamport resolution; we trim to a
# readable 6 dp for alerts, which is still far finer than any tradeable tick).
_SOL_DISPLAY_QUANTUM = Decimal("0.000001")


class AlertCategory(StrEnum):
    """The three alert classes the operator subscribes to (BUILD-DIRECTIVE)."""

    FILL = "FILL"
    RUG_AVOIDED = "RUG_AVOIDED"
    BREAKER_TRIP = "BREAKER_TRIP"


@dataclass(frozen=True)
class AlertMessage:
    """A formatted alert ready for the Telegram client.

    `text` is the human-readable body.  `category` lets the service route /
    rate-limit per class.  `dedupe_key` lets the service suppress duplicate
    alerts for the same underlying event (idempotency — the feed may redeliver).
    """

    category: AlertCategory
    text: str
    dedupe_key: str


def format_sol_from_lamports(lamports: int) -> str:
    """Format integer lamports as a signed SOL string using Decimal ONLY.

    Raises TypeError on a float (money is never float — NFR-009).  A bool is
    rejected too (bool is an int subclass but never a valid lamport quantity).
    The result is quantized to 6 dp with banker's rounding for stable display.
    """
    if isinstance(lamports, bool) or not isinstance(lamports, int):
        raise TypeError(
            f"lamports must be int (integer base units), got {type(lamports).__name__} "
            f"{lamports!r}. Money is NEVER float (NFR-009)."
        )
    sol = (Decimal(lamports) / _LAMPORTS_PER_SOL).quantize(
        _SOL_DISPLAY_QUANTUM, rounding=ROUND_HALF_EVEN
    )
    # Force an explicit sign for non-negative values so a +/- is always visible.
    sign = "+" if sol >= 0 else ""
    return f"{sign}{sol} SOL"


def _event_slot(event: dict[str, Any]) -> int:
    """Extract the canonical decision slot (event_time.slot), falling back to slot."""
    et = event.get("event_time")
    if isinstance(et, dict) and isinstance(et.get("slot"), int):
        return et["slot"]
    slot = event.get("slot")
    return slot if isinstance(slot, int) else 0


def _short_mint(mint: str) -> str:
    """Abbreviate a mint/pubkey for readability (head…tail). Never a secret."""
    if len(mint) <= 12:
        return mint
    return f"{mint[:6]}…{mint[-4:]}"


def classify_event(event: dict[str, Any]) -> AlertCategory | None:
    """Classify a SnipeEvent dict into an alert category, or None to skip.

    Mapping (api-contracts.md §6 SnipeEvent semantics):
      action == "sniped"                       -> FILL
      action in {"vetoed", "skipped"} AND
        (red_flags non-empty OR veto_source set
         that is not the literal string "null") -> RUG_AVOIDED
      anything else                            -> None (no alert)

    A plain "skipped" with no red flags and no veto source is routine
    non-action noise and is intentionally NOT alerted (operator signal hygiene).
    """
    action = event.get("action")
    if action == "sniped":
        return AlertCategory.FILL
    if action in ("vetoed", "skipped"):
        red_flags = event.get("red_flags") or []
        veto_source = event.get("veto_source")
        has_veto = bool(veto_source) and veto_source != "null"
        if red_flags or has_veto:
            return AlertCategory.RUG_AVOIDED
    return None


def _money_lamports(event: dict[str, Any], key: str) -> int | None:
    """Read an integer-lamport field from an event; reject float money."""
    val = event.get(key)
    if val is None:
        return None
    if isinstance(val, bool) or not isinstance(val, int):
        raise TypeError(
            f"SnipeEvent.{key} must be int lamports, got {type(val).__name__} {val!r} "
            f"(money is never float — NFR-009)."
        )
    return val


def format_fill(event: dict[str, Any]) -> AlertMessage:
    """Format a FILL alert from a SnipeEvent (action == 'sniped').

    Money fields (when present) are integer lamports formatted via Decimal.
    The contract's `sol_in_lamports` may ride on a feed event; if absent we omit
    the size line rather than guess.  `pnl_pct` is the per-event realized
    percentage on close (contract field) — surfaced labelled "pnl", NOT a
    win-rate.
    """
    token = str(event.get("token", "?"))
    mint = _short_mint(str(event.get("mint", "?")))
    slot = _event_slot(event)
    source = str(event.get("source", "?"))
    lines = [
        "✅ FILL",
        f"token: {token}  mint: {mint}",
        f"source: {source}  slot: {slot}",
    ]

    sol_in = _money_lamports(event, "sol_in_lamports")
    if sol_in is not None:
        lines.append(f"size: {format_sol_from_lamports(sol_in)}")

    realized = _money_lamports(event, "realized_pnl_net_lamports")
    if realized is not None:
        lines.append(f"net pnl: {format_sol_from_lamports(realized)}")

    pnl_pct = event.get("pnl_pct")
    if isinstance(pnl_pct, int | float) and not isinstance(pnl_pct, bool):
        # Per-event realized percentage (contract field) — labelled "pnl", not a
        # win-rate.  Format with a fixed precision via Decimal-from-string.
        pct = Decimal(str(pnl_pct)).quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN)
        lines.append(f"pnl: {pct}%")

    return AlertMessage(
        category=AlertCategory.FILL,
        text="\n".join(lines),
        dedupe_key=f"FILL:{event.get('id', mint + ':' + str(slot))}",
    )


def format_rug_avoided(event: dict[str, Any]) -> AlertMessage:
    """Format a RUG-AVOIDED alert (vetoed/skipped on red flags or a veto gate)."""
    token = str(event.get("token", "?"))
    mint = _short_mint(str(event.get("mint", "?")))
    slot = _event_slot(event)
    red_flags = event.get("red_flags") or []
    veto_source = event.get("veto_source")
    gate_reasons = event.get("gate_reasons") or []

    lines = [
        "🛡️ RUG AVOIDED",
        f"token: {token}  mint: {mint}",
        f"slot: {slot}",
    ]
    if veto_source and veto_source != "null":
        lines.append(f"vetoed by: {veto_source}")
    if red_flags:
        lines.append("red flags: " + ", ".join(str(f) for f in red_flags))
    elif gate_reasons:
        # No explicit red flags but a gate reason explains the skip.
        reasons = [r for r in gate_reasons if r != "passed"]
        if reasons:
            lines.append("gate: " + ", ".join(str(r) for r in reasons))

    return AlertMessage(
        category=AlertCategory.RUG_AVOIDED,
        text="\n".join(lines),
        dedupe_key=f"RUG_AVOIDED:{event.get('id', mint + ':' + str(slot))}",
    )


def format_breaker_trip(
    *,
    daily_net_pnl_lamports: int,
    daily_loss_limit_sol: str,
    tripped_at_utc: str | None,
    dry_run_enabled: bool,
) -> AlertMessage:
    """Format a BREAKER-TRIP alert from breaker state.

    `daily_net_pnl_lamports` is integer lamports (rejects float).
    `daily_loss_limit_sol` is the contract's decimal-string floor (e.g. "-0.30").
    The alert states the DRY-RUN posture verbatim — it never changes it; this
    channel cannot enable live capital.  The dedupe key is the trip timestamp so
    a single trip alerts ONCE even if the state is polled repeatedly.
    """
    pnl = format_sol_from_lamports(daily_net_pnl_lamports)
    posture = "DRY-RUN (no real capital)" if dry_run_enabled else "LIVE"
    lines = [
        "⛔ CIRCUIT BREAKER TRIPPED",
        "all new entries HALTED; open positions handed to ExitEngine.",
        f"daily net pnl: {pnl}",
        f"daily loss limit: {daily_loss_limit_sol} SOL",
        f"posture: {posture}",
        "manual review required to re-arm (operator-only; never automated).",
    ]
    if tripped_at_utc:
        lines.append(f"tripped at: {tripped_at_utc}")

    return AlertMessage(
        category=AlertCategory.BREAKER_TRIP,
        text="\n".join(lines),
        dedupe_key=f"BREAKER_TRIP:{tripped_at_utc or 'unknown'}",
    )
