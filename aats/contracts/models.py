"""Prediction (DecisionSignal), MCSScore, ReasoningVerdict, ReasoningAction.

Source of truth: data-models.md §4 (DecisionSignal), §5 (MCSScore), §6.1 (ReasoningVerdict).

CRITICAL invariant — ADR-0006 (asymmetric trust by type):
  ReasoningAction has EXACTLY four members: HOLD, VETO_ENTRY, REDUCE_SIZE, FORCE_EXIT.
  There is NO SIZE_UP, WIDEN_STOP, ADD_LEVERAGE, or OVERRIDE_HARD_STOP member.
  This is the PRIMARY defense — the type cannot express a risk-increase.
  The clamp (risk_increase_clamped=True) is the backstop for raw LLM strings.

NO win_rate field exists on any model in this file (HONESTY CLAUSE; AC-037).
NO point price field on DecisionSignal (locked decision 9).
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, field_validator

from aats.contracts.events import EventTime


class DecisionSignal(BaseModel):
    """Calibrated probability + uncertainty — the model's output.

    Source: data-models.md §4.
    Produced by the SLOW loop (classifier inference); pre-staged in Redis KV.
    The SNIPE loop reads p_calibrated + uncertainty from KV — it NEVER recomputes.

    NO point price field (locked decision 9).
    NO win_rate field (HONESTY CLAUSE, AC-037).
    High uncertainty → de-risk (¼-Kelly shrinks), NEVER size-up (FR-014/032).
    """

    mint: str
    event_time: EventTime
    model_version: str
    # Calibrated probability in [0,1] (FR-014; reliability-curve-checked)
    p_calibrated: float
    # Predictive uncertainty band — high uncertainty => de-risk only
    uncertainty: float
    # The frozen naive-momentum baseline's signal on the same candidate (C-4)
    baseline_p: float
    # Which hypothesis/edge thesis fired (EH-001 | EH-003 | ...)
    surface: str

    # PROOF: NO point price field — the model never emits a price target (locked decision 9)
    # PROOF: NO win_rate field — HONESTY CLAUSE (AC-037, api-contracts.md §4 metrics)

    model_config = {"frozen": True}

    @field_validator("p_calibrated", "uncertainty", "baseline_p")
    @classmethod
    def _probability_range(cls, v: float) -> float:
        if not (0.0 <= v <= 1.0):
            raise ValueError(f"Probability/uncertainty must be in [0, 1], got {v}")
        return v


class MCSScore(BaseModel):
    """Market Conviction Score — adversarial sentiment, contrarian by construction.

    Source: data-models.md §5.
    HIGH synchronicity LOWERS conviction (FR-008, AC-010).
    LOW account_age_median_days LOWERS conviction (adversarial shill signal).

    conviction may ONLY gate or de-risk an entry.
    A high MCS can NEVER trigger or size-up an entry (FR-008; AC-021).
    NO win_rate field (HONESTY CLAUSE).
    """

    asset: str
    event_time: EventTime
    conviction: float  # the score the SLOW loop consumes (de-risk/gate only)
    momentum: float
    novelty: float
    # HIGH synchronicity LOWERS conviction (adversarial: coordinated shill → contrarian)
    synchronicity: float
    # LOW age LOWERS conviction (adversarial shill signal)
    account_age_median_days: float
    coordinated_shill_flag: bool
    red_flags: list[str]
    post_count: int
    # Quoted untrusted text — NEVER executed as an instruction (BUILD-DIRECTIVE)
    reasoning: str

    model_config = {"frozen": True}

    @field_validator("conviction", "momentum", "novelty", "synchronicity")
    @classmethod
    def _score_range(cls, v: float) -> float:
        if not (0.0 <= v <= 1.0):
            raise ValueError(f"MCS score fields must be in [0, 1], got {v}")
        return v


class ReasoningAction(StrEnum):
    """The ONLY actions the LLM Reasoner may apply — ALL de-risk or no-op.

    PROOF (data-models.md §6.1, ADR-0006):
    This enum has EXACTLY four members.  There is NO SIZE_UP, WIDEN_STOP,
    ADD_LEVERAGE, or OVERRIDE_HARD_STOP member.  The type system makes a
    risk-increase inexpressible on the reasoning path.

    A raw LLM string outside this set is caught at parse time and forced to
    HOLD with risk_increase_clamped=True (FR-017, AC-019/054).
    """

    HOLD = "HOLD"  # no-op (the default / clamp target)
    VETO_ENTRY = "VETO_ENTRY"  # de-risk: cancel a pending entry
    REDUCE_SIZE = "REDUCE_SIZE"  # de-risk: shrink position
    FORCE_EXIT = "FORCE_EXIT"  # de-risk: exit the position immediately

    # NO SIZE_UP member — the type cannot express a size-increase.
    # NO WIDEN_STOP member — the type cannot express a stop loosening.
    # NO ADD_LEVERAGE member — the type cannot express leverage.
    # NO OVERRIDE_HARD_STOP member — the type cannot express a hard-stop override.


# ---------------------------------------------------------------------------
# Static proof that ReasoningAction contains no risk-increase member.
# This check runs at module import time — if someone adds a bad member the
# import fails immediately, not at test time.
# ---------------------------------------------------------------------------
_RISK_INCREASE_FORBIDDEN_NAMES = frozenset(
    {"SIZE_UP", "WIDEN_STOP", "ADD_LEVERAGE", "OVERRIDE_HARD_STOP"}
)
_ACTUAL_MEMBERS = frozenset(m.name for m in ReasoningAction)
_ILLEGAL = _ACTUAL_MEMBERS & _RISK_INCREASE_FORBIDDEN_NAMES
if _ILLEGAL:  # pragma: no cover — this block must never execute
    raise RuntimeError(
        f"ReasoningAction contains forbidden risk-increase member(s): {_ILLEGAL}.  "
        "ADR-0006 prohibits risk-increase actions on the reasoning path."
    )


class ReasoningVerdict(BaseModel):
    """The LLM Reasoner's ONLY output shape.

    Source: data-models.md §6.1.
    The LLM Reasoner emits ONLY a ReasoningVerdict.  The action enum has no
    risk-increase member.  If the raw LLM string is outside the enum, it is
    forced to HOLD and risk_increase_clamped is set True (AC-054).

    audit fields (AC-054): what the raw LLM tried vs what was applied.
    """

    mint: str
    event_time: EventTime
    action: ReasoningAction  # enum has NO risk-increase variant (proof above)
    reason: str  # quoted untrusted rationale — NEVER executed as an instruction
    confidence: float
    # Audit fields (AC-054)
    action_received_raw: str  # the unvalidated string the LLM emitted
    risk_increase_clamped: bool  # True if raw action was risk-increase → forced HOLD

    model_config = {"frozen": True}

    @field_validator("confidence")
    @classmethod
    def _confidence_range(cls, v: float) -> float:
        if not (0.0 <= v <= 1.0):
            raise ValueError(f"confidence must be in [0, 1], got {v}")
        return v
