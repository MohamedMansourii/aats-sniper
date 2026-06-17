"""Intent union — the ONLY thing that reaches execution.

Source of truth: data-models.md §6.2.  ADR-0006 (asymmetric trust by type).

CRITICAL invariant — the Intent union is de-risk safe:
  - The ONLY risk-increasing variant is EntryIntent (increases exposure).
  - EntryIntent is ONLY constructible by the cost-gated SNIPE path.
  - The reasoning/LLM/social/MCS path holds a factory that produces only
    ExitIntent, ReduceIntent, or VetoIntent.  It has NO access to
    EntryIntent's constructor.
  - EntryIntent CANNOT be constructed without a populated CostStack AND
    target_slot >= event_time.slot + 5.
  - There is NO "increase position" variant; the union is CLOSED.
    Adding a risk-increasing member requires an ADR + delta notice.

Money rule (data-models.md §0):
  sol_in_lamports   → int (lamports)
  tip_lamports      → int (lamports)
  All CostStack bps fields → int (basis points, exact integer)
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from aats.contracts.events import EventTime


class IntentKind(StrEnum):
    """The discriminator tag for the Intent union."""

    ENTRY = "ENTRY"  # increases exposure — ONLY constructible by cost-gated SNIPE path
    EXIT = "EXIT"  # de-risk
    REDUCE = "REDUCE"  # de-risk (partial exit)
    VETO = "VETO"  # de-risk (cancel a pending entry)

    # PROOF: NO risk-increase variant beyond ENTRY (which is cost-gated).
    # NO INCREASE_POSITION, NO WIDEN_STOP, NO ADD_LEVERAGE.


class CostStack(BaseModel):
    """An ENTRY Intent CANNOT be constructed without this (FR-027, cost-aware by construction).

    Source: data-models.md §6.2.
    Every field is int (bps or lamports) — NEVER float.
    ENTRY is rejected if expected_edge_bps <= total_cost_bps.
    """

    expected_edge_bps: int
    jito_tip_bps: int
    priority_fee_bps: int
    entry_slippage_bps: int
    amm_fee_bps: int
    exit_slippage_bps: int
    # FLOOR 150 bps pre-calibration, labeled UNCALIBRATED (OQ-007, C-11)
    adverse_selection_bps: int
    # = sum of all cost components
    total_cost_bps: int

    model_config = {"frozen": True}

    @model_validator(mode="before")
    @classmethod
    def _reject_float_bps(cls, data: object) -> object:
        """Reject float values for any bps/cost field (data-models.md §0)."""
        if not isinstance(data, dict):
            return data
        _BPS_FIELDS = (
            "expected_edge_bps",
            "jito_tip_bps",
            "priority_fee_bps",
            "entry_slippage_bps",
            "amm_fee_bps",
            "exit_slippage_bps",
            "adverse_selection_bps",
            "total_cost_bps",
        )
        for field in _BPS_FIELDS:
            val = data.get(field)
            if isinstance(val, float):
                raise ValueError(
                    f"CostStack field '{field}' must be int (bps), "
                    f"got float {val!r}.  Use integer basis points (data-models.md §0)."
                )
        return data

    @model_validator(mode="after")
    def _adverse_selection_floor(self) -> CostStack:
        """FLOOR 150 bps pre-calibration (OQ-007, C-11)."""
        if self.adverse_selection_bps < 150:
            raise ValueError(
                f"adverse_selection_bps must be >= 150 (UNCALIBRATED floor, OQ-007, C-11), "
                f"got {self.adverse_selection_bps}"
            )
        return self


class EntryIntent(BaseModel):
    """An intent to enter a new position — the ONLY risk-increasing intent.

    Source: data-models.md §6.2.
    ONLY constructible by the cost-gated SNIPE path.
    The reasoning/LLM/social/MCS path has NO access to this constructor.

    Cost gate: ENTRY is rejected if cost_stack.expected_edge_bps <= total_cost_bps
    (FR-027, AC-013).  This is enforced by the SNIPE loop before emitting.
    The CostStack is REQUIRED — there is no default that bypasses the cost gate.

    slot guard: target_slot MUST be >= event_time.slot + 5 (no block-0, AC-017).
    """

    kind: Literal[IntentKind.ENTRY] = IntentKind.ENTRY
    # Deterministic client_intent_id (mint + epoch) for idempotent replay (FR-024)
    client_intent_id: str
    mint: str
    event_time: EventTime
    # SOL in lamports (integer) — must be <= min(per_coin_cap, 0.25 × kelly)
    sol_in_lamports: int
    slippage_bps: int
    tip_lamports: int  # from LIVE tip cache, bounded by min(floor, 0.30 × edge) (FR-027)
    cu_price_microlamports: int
    # MUST be >= event_time.slot + 5 (no block-0, AC-017)
    target_slot: int
    # REQUIRED — no ENTRY without the full cost stack present (cost-aware by construction)
    cost_stack: CostStack
    venue: str  # registry venue id
    wallet_id: str  # multi-wallet (FR-036); single for R3 (OQ-010)

    model_config = {"frozen": True}

    @model_validator(mode="before")
    @classmethod
    def _reject_float_money(cls, data: object) -> object:
        """Reject float values for integer money fields (data-models.md §0)."""
        if not isinstance(data, dict):
            return data
        _INT_FIELDS = ("sol_in_lamports", "tip_lamports", "cu_price_microlamports")
        for field in _INT_FIELDS:
            val = data.get(field)
            if isinstance(val, float):
                raise ValueError(
                    f"EntryIntent field '{field}' must be int (lamports), "
                    f"got float {val!r}.  Use integer base units (data-models.md §0)."
                )
        return data

    @model_validator(mode="after")
    def _slot_guard(self) -> EntryIntent:
        """target_slot MUST be >= event_time.slot + 5 (AC-017)."""
        min_target = self.event_time.slot + 5
        if self.target_slot < min_target:
            raise ValueError(
                f"target_slot ({self.target_slot}) must be >= event_time.slot + 5 "
                f"= {min_target} (no block-0 race, AC-017)"
            )
        return self

    @model_validator(mode="after")
    def _cost_gate(self) -> EntryIntent:
        """ENTRY is rejected if expected_edge_bps <= total_cost_bps (FR-027, AC-013)."""
        if self.cost_stack.expected_edge_bps <= self.cost_stack.total_cost_bps:
            raise ValueError(
                f"ENTRY rejected: expected_edge_bps ({self.cost_stack.expected_edge_bps}) "
                f"<= total_cost_bps ({self.cost_stack.total_cost_bps}).  "
                "Cost gate: expected edge must exceed all costs (FR-027, AC-013)."
            )
        return self


class ExitIntent(BaseModel):
    """Intent to exit (fully or partially) an open position — de-risk only.

    Source: data-models.md §6.2.
    fraction_bps: 0..10000 of remaining position.
    The reasoning/LLM/social path MAY construct this.
    """

    kind: Literal[IntentKind.EXIT] = IntentKind.EXIT
    client_intent_id: str  # deterministic, for idempotent replay
    mint: str
    fraction_bps: int  # 0..10000 of remaining position
    exit_mode: Literal["secure", "fast"]  # default secure (OQ-008)
    reason: str

    model_config = {"frozen": True}

    @field_validator("fraction_bps")
    @classmethod
    def _fraction_range(cls, v: int) -> int:
        if not (0 <= v <= 10_000):
            raise ValueError(
                f"ExitIntent.fraction_bps must be in [0, 10000] (bps), got {v}"
            )
        return v


class ReduceIntent(BaseModel):
    """Intent to reduce (partial exit) an open position — de-risk only.

    Source: data-models.md §6.2.
    fraction_bps must be a REDUCTION; cannot exceed remaining.
    The reasoning/LLM/social path MAY construct this.
    """

    kind: Literal[IntentKind.REDUCE] = IntentKind.REDUCE
    client_intent_id: str  # deterministic, for idempotent replay
    mint: str
    fraction_bps: int  # must be a REDUCTION; cannot exceed remaining
    reason: str

    model_config = {"frozen": True}

    @field_validator("fraction_bps")
    @classmethod
    def _fraction_range(cls, v: int) -> int:
        if not (1 <= v <= 10_000):
            raise ValueError(
                f"ReduceIntent.fraction_bps must be in [1, 10000] (bps), got {v}.  "
                "A ReduceIntent must reduce the position (not zero or exceed remaining)."
            )
        return v


class VetoIntent(BaseModel):
    """Intent to cancel a pending entry — de-risk only.

    Source: data-models.md §6.2.
    The reasoning/LLM/social path MAY construct this.
    """

    kind: Literal[IntentKind.VETO] = IntentKind.VETO
    client_intent_id: str  # deterministic, for idempotent replay
    mint: str
    reason: str

    model_config = {"frozen": True}


# ---------------------------------------------------------------------------
# The discriminated union — the ONLY thing that reaches execution (M4).
# Discriminated on the `kind` field.
# ---------------------------------------------------------------------------
Intent = Annotated[
    EntryIntent | ExitIntent | ReduceIntent | VetoIntent,
    Field(discriminator="kind"),
]

# ---------------------------------------------------------------------------
# De-risk-only factory — the ONLY factory the reasoning/LLM/social/MCS path
# holds.  It has NO EntryIntent constructor.  This is the structural
# enforcement of ADR-0006 §"Consequences".
# ---------------------------------------------------------------------------


class DeRiskIntentFactory:
    """A factory that can ONLY produce de-risk variants of Intent.

    The reasoning/LLM/social/MCS path is handed an instance of this factory.
    It has no method to construct an EntryIntent.  The type system is the
    primary defense; the absence of an entry method is the structural guarantee.

    Usage:
        factory = DeRiskIntentFactory()
        # This is all the reasoning path can do:
        exit_intent = factory.exit(mint=..., fraction_bps=10000, exit_mode="secure", ...)
        reduce_intent = factory.reduce(mint=..., fraction_bps=5000, ...)
        veto_intent = factory.veto(mint=..., reason=...)
        # factory.entry(...)  ← does NOT exist — AttributeError if attempted
    """

    def exit(
        self,
        *,
        client_intent_id: str,
        mint: str,
        fraction_bps: int,
        exit_mode: Literal["secure", "fast"] = "secure",
        reason: str,
    ) -> ExitIntent:
        return ExitIntent(
            client_intent_id=client_intent_id,
            mint=mint,
            fraction_bps=fraction_bps,
            exit_mode=exit_mode,
            reason=reason,
        )

    def reduce(
        self,
        *,
        client_intent_id: str,
        mint: str,
        fraction_bps: int,
        reason: str,
    ) -> ReduceIntent:
        return ReduceIntent(
            client_intent_id=client_intent_id,
            mint=mint,
            fraction_bps=fraction_bps,
            reason=reason,
        )

    def veto(
        self,
        *,
        client_intent_id: str,
        mint: str,
        reason: str,
    ) -> VetoIntent:
        return VetoIntent(
            client_intent_id=client_intent_id,
            mint=mint,
            reason=reason,
        )

    # Explicitly NO `entry` method — this is the structural enforcement.
    # A future engineer who tries `factory.entry(...)` gets AttributeError,
    # not a silent bypass.  (ADR-0006 §Consequences)
