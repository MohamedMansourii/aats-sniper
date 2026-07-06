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

from pydantic import BaseModel, field_validator, model_validator

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


# ---------------------------------------------------------------------------
# RegimeSignal — the SLOW-loop chart-path / regime model output contract
# (ADR-0014).  Added by M2-CP-04 as a FROZEN, ADDITIVE message contract.
#
# WHY IT LIVES HERE, SELF-CONTAINED:
#   The taxonomy/label half of the regime model lives in
#   `aats.models.regime_labels` (M2-CP-02), which transitively imports the heavy
#   ML stack (lightgbm/numpy via `aats.models.survivor`).  The frozen contract
#   layer MUST stay lightweight (pydantic only) and free of circular imports
#   (`aats.models.regime_labels` -> `aats.models.survivor` -> `aats.contracts.models`).
#   So the STATE enum + de-risk directive + total mapping are RE-DECLARED here as
#   the contract-layer law, with the SAME string values as the model layer.  A
#   consistency test (`tests/contracts/test_regime_signal.py`) fails the build if
#   the two ever drift, so there is one taxonomy with two lightweight/heavy homes,
#   never two taxonomies.
#
# NON-WAIVABLE LAWS (asserted in tests):
#   - A regime is a multiclass STATE + calibrated probabilities + uncertainty.
#     There is NO price, NO size, NO win-rate / success-rate / realized-mult field.
#   - The de-risk directive codomain is de-risk-or-inert ONLY
#     ({NONE, REDUCE, FORCE_EXIT, VETO_ENTRY}); a risk-INCREASING directive is
#     INEXPRESSIBLE BY TYPE (no size-up / relax-stop / delay-exit / leverage member).
#   - ACCUMULATION and NEUTRAL are pinned INERT (-> NONE): a bullish/ranging
#     prediction carries ZERO control authority — it can never delay an exit,
#     relax a stop, or size up.
#   - event_time is carried (point-in-time); wall-clock is never a decision field.
# ---------------------------------------------------------------------------


class RegimeState(StrEnum):
    """The four regime STATES over the survivor/exit horizon — a multiclass STATE.

    String values MUST match `aats.models.regime_labels.RegimeLabel` (consistency
    test enforces it).  A STATE is never a rate, a success-rate, or a price.
    """

    ACCUMULATION = "ACCUMULATION"      # still sellable; healthy base-building (INERT/bullish)
    DISTRIBUTION = "DISTRIBUTION"      # still sellable; topping / markdown (DE-RISK)
    RUG_IN_PROGRESS = "RUG_IN_PROGRESS"  # sellability collapsing (DE-RISK-MAXIMAL)
    NEUTRAL = "NEUTRAL"                 # still sellable; ranging / undetermined (INERT)


class RegimeDeRiskDirective(StrEnum):
    """The ONLY directives a regime STATE may authorize — de-risk or inert, NEVER risk-up.

    PROOF (mirrors ReasoningAction / ADR-0006): the codomain contains NO size-up,
    relax-stop, delay-exit, add-leverage, or hard-stop-override member.  A
    risk-INCREASING directive is INEXPRESSIBLE BY TYPE.  String values MUST match
    `aats.models.regime_labels.RegimeDeRiskAction` (consistency test enforces it).
    """

    NONE = "NONE"                # inert: authorizes NOTHING (ACCUMULATION, NEUTRAL)
    REDUCE = "REDUCE"            # de-risk: shrink / lean-exit (DISTRIBUTION)
    FORCE_EXIT = "FORCE_EXIT"    # de-risk-maximal: exit the position (RUG_IN_PROGRESS)
    VETO_ENTRY = "VETO_ENTRY"    # de-risk: veto a NEW entry (never affects an open stop)


# The TOTAL STATE -> directive mapping.  ACCUMULATION and NEUTRAL are pinned to
# NONE (inert).  MUST match `aats.models.regime_labels.REGIME_DERISK_ACTION`.
_REGIME_STATE_DIRECTIVE: dict[RegimeState, RegimeDeRiskDirective] = {
    RegimeState.ACCUMULATION: RegimeDeRiskDirective.NONE,
    RegimeState.NEUTRAL: RegimeDeRiskDirective.NONE,
    RegimeState.DISTRIBUTION: RegimeDeRiskDirective.REDUCE,
    RegimeState.RUG_IN_PROGRESS: RegimeDeRiskDirective.FORCE_EXIT,
}


# Static proof that RegimeDeRiskDirective contains no risk-increase member.
# Runs at import time — a bad member fails the import immediately (mirrors the
# ReasoningAction guard above).
_REGIME_FORBIDDEN_DIRECTIVE_NAMES = frozenset(
    {"SIZE_UP", "WIDEN_STOP", "RELAX_STOP", "DELAY_EXIT", "ADD_LEVERAGE", "OVERRIDE_HARD_STOP"}
)
_REGIME_ACTUAL_DIRECTIVES = frozenset(m.name for m in RegimeDeRiskDirective)
_REGIME_ILLEGAL = _REGIME_ACTUAL_DIRECTIVES & _REGIME_FORBIDDEN_DIRECTIVE_NAMES
if _REGIME_ILLEGAL:  # pragma: no cover — this block must never execute
    raise RuntimeError(
        f"RegimeDeRiskDirective contains forbidden risk-increase member(s): {_REGIME_ILLEGAL}. "
        "ADR-0014 prohibits risk-increase directives on the regime path."
    )


def regime_derisk_directive(state: RegimeState) -> RegimeDeRiskDirective:
    """The (total) de-risk directive a regime STATE authorizes.  Bullish/ranging -> NONE."""
    return _REGIME_STATE_DIRECTIVE[state]


class RegimeSignal(BaseModel):
    """The SLOW-loop chart-path / regime model output — STATE + calibrated probs + uncertainty.

    Source: ADR-0014 (M2-CP-04).  Produced by the SLOW loop only (the chart-path /
    regime model is SLOW-loop only — no ONNX/Rust shim, never FAST/SNIPE).  The SNIPE
    and FAST loops NEVER consume this contract; the SLOW loop translates its de-risk
    directive into the EXISTING pre-set scalar flags (narrative_failure / veto) that
    exit_engine / rule_engine already read.

    `regime` is the argmax STATE; `p_*` are the per-class CALIBRATED probabilities of a
    genuine distribution (sum to 1); `uncertainty` is a predictive band.  High
    uncertainty / a de-risk STATE is a DE-RISK input ONLY — it can never size up, widen
    a stop, or override a hard stop.

    NO price field.  NO size field.  NO win_rate / success_rate / realized_mult field
    (HONESTY CLAUSE, AC-037).  `is_bootstrap_not_real=True` until the R1 corpus exists —
    no regime signal has earned any capital license.
    """

    mint: str
    event_time: EventTime
    model_version: str
    taxonomy_version: str
    # The argmax multiclass STATE (must be one of the top-probability classes).
    regime: RegimeState
    # Per-class CALIBRATED probabilities in [0,1]; a genuine distribution (sum == 1).
    p_accumulation: float
    p_neutral: float
    p_distribution: float
    p_rug_in_progress: float
    # Predictive uncertainty band — high uncertainty => de-risk only, never size-up.
    uncertainty: float
    is_bootstrap_not_real: bool

    # PROOF: NO price / size field — the regime model never emits a price or a trade size.
    # PROOF: NO win_rate / success_rate / realized_mult field — HONESTY CLAUSE (AC-037).

    model_config = {"frozen": True}

    @field_validator(
        "p_accumulation",
        "p_neutral",
        "p_distribution",
        "p_rug_in_progress",
        "uncertainty",
    )
    @classmethod
    def _probability_range(cls, v: float) -> float:
        if not (0.0 <= v <= 1.0):
            raise ValueError(f"Regime probability/uncertainty must be in [0, 1], got {v}")
        return v

    @model_validator(mode="after")
    def _distribution_and_argmax(self) -> RegimeSignal:
        probs = {
            RegimeState.ACCUMULATION: self.p_accumulation,
            RegimeState.NEUTRAL: self.p_neutral,
            RegimeState.DISTRIBUTION: self.p_distribution,
            RegimeState.RUG_IN_PROGRESS: self.p_rug_in_progress,
        }
        total = sum(probs.values())
        if abs(total - 1.0) > 1e-6:
            raise ValueError(
                f"Regime class probabilities must form a distribution summing to 1 "
                f"(got sum={total}). Normalize before constructing a RegimeSignal."
            )
        top = max(probs.values())
        # `regime` MUST be (one of) the argmax class(es) — the declared STATE is a
        # genuine top-probability class, not an arbitrary label decoupled from the probs.
        if probs[self.regime] < top - 1e-9:
            raise ValueError(
                f"regime={self.regime.value!r} is not an argmax class "
                f"(its prob {probs[self.regime]} < max {top}). The declared STATE must "
                "match the top-probability class."
            )
        return self

    @property
    def derisk_directive(self) -> RegimeDeRiskDirective:
        """The de-risk directive this STATE authorizes (bullish/ranging -> NONE, provably inert)."""
        return _REGIME_STATE_DIRECTIVE[self.regime]
