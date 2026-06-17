"""The asymmetric-trust CLAMP — the enforcement boundary (T-313, ADR-0006).

ASYMMETRIC TRUST IS LAW
=======================
The LLM is a fast, fallible juror with exactly one vote, and that vote can only ever
SPARE risk, never SPEND it.  This module is the pure, unit-tested function every LLM
output passes through.  It can only NARROW risk relative to the quant decision:

  - it may DOWNGRADE a signal (toward de-risk),
  - it may flip `veto` true,
  - it may flip `narrative_failure` true (→ M4 catastrophic FORCE_EXIT).

It can NEVER:
  - upgrade a signal,
  - raise size,
  - widen or move a stop,
  - add leverage,
  - override a hard stop,
  - contradict the quant decision in the risk-increasing direction.

THE TYPE IS THE PRIMARY DEFENSE; THE CLAMP IS THE BACKSTOP
=========================================================
`ReasoningAction` (aats.contracts.models) has NO risk-increase member — SIZE_UP,
WIDEN_STOP, ADD_LEVERAGE, OVERRIDE_HARD_STOP are not expressible.  So even a clamp bug
cannot emit a risk-increase: the worst a defect could do is fail to de-risk, never
up-risk.  The clamp's job is to (a) drop any raw LLM string outside the de-risk space
onto HOLD with an audit flag, and (b) prove monotonicity toward safety under fuzzing.

PURE FUNCTION
=============
`clamp_to_derisk_action(...)` has no I/O, no clock, no randomness.  Identical inputs
always produce identical outputs.  Property tests fuzz it across the whole input space
and assert the applied action is NEVER more bullish than the quant decision.
"""

from __future__ import annotations

from dataclasses import dataclass

from aats.contracts.models import ReasoningAction
from aats.reasoning.schema import (
    DecisionSignalLabel,
    QuantBucket,
)

# The set of raw strings that REQUEST a risk increase.  If the LLM emits any of these
# (or anything that is not a valid de-risk signal), the clamp drops it to HOLD and sets
# risk_increase_clamped=True.  This list is for AUDIT classification only — the type
# system already makes these inexpressible as a ReasoningAction.
_RISK_INCREASE_TOKENS = frozenset(
    {
        "SIZE_UP",
        "BUY",
        "STRONG_BUY",
        "INCREASE",
        "INCREASE_SIZE",
        "ADD",
        "ADD_SIZE",
        "WIDEN_STOP",
        "MOVE_STOP",
        "ADD_LEVERAGE",
        "LEVERAGE",
        "OVERRIDE_HARD_STOP",
        "OVERRIDE_STOP",
        "DISABLE_STOP",
        "SCALE_IN",
        "MARTINGALE",
    }
)


@dataclass(frozen=True)
class ClampResult:
    """The outcome of clamping a raw LLM signal to a de-risk-only action.

    `action` is the de-risk-only action that will be APPLIED (one of HOLD, VETO_ENTRY,
    REDUCE_SIZE, FORCE_EXIT).  `risk_increase_clamped` is True iff the raw signal
    requested a risk increase that was dropped.  `action_received_raw` records the raw
    string the LLM emitted, for the audit trail (AC-054).
    """

    action: ReasoningAction
    risk_increase_clamped: bool
    action_received_raw: str


def classify_raw_is_risk_increase(action_received_raw: str) -> bool:
    """True if the raw LLM string is REQUESTING a risk increase.

    Used for AUDIT classification.  We do NOT regex-parse free text to extract a
    decision; we only check membership against a closed token set after normalizing
    case/whitespace.  A risk-increasing request is recorded and then dropped to HOLD.
    """
    token = action_received_raw.strip().upper().replace(" ", "_").replace("-", "_")
    return token in _RISK_INCREASE_TOKENS


def quant_to_action_ceiling(quant: QuantBucket) -> ReasoningAction:
    """The MOST-bullish (least de-risk) action the LLM is allowed to leave in place.

    The clamp can de-risk BELOW this ceiling but never above it.  The quant decision is
    the reference; the LLM may only narrow risk relative to it.

    - quant BULLISH  -> ceiling HOLD     (LLM cannot turn a quant-bullish into a buy;
                                          the LLM cannot up-signal AT ALL.  HOLD = no-op =
                                          "let the cost-gated SNIPE path proceed").
    - quant NEUTRAL  -> ceiling HOLD
    - quant BEARISH  -> ceiling VETO_ENTRY (the quant itself is already de-risking; the
                                          LLM cannot make it LESS de-risk).

    NOTE: HOLD is a no-op on the reasoning path.  It does NOT authorize an entry — the
    entry is authorized (or not) by the cost-gated SNIPE path.  The reasoning path can
    only ever subtract risk; HOLD means "I add no veto."
    """
    if quant is QuantBucket.BEARISH:
        # Quant is already vetoing-direction; the LLM may not relax it.
        return ReasoningAction.VETO_ENTRY
    return ReasoningAction.HOLD


# Order de-risk actions by how much risk they REMOVE.  HOLD removes the least (none);
# FORCE_EXIT removes the most.  The clamp may move DOWN this scale (toward HOLD) only
# up to the quant ceiling, and may move UP it freely (more de-risk is always allowed).
_DERISK_STRENGTH: dict[ReasoningAction, int] = {
    ReasoningAction.HOLD: 0,
    ReasoningAction.VETO_ENTRY: 1,
    ReasoningAction.REDUCE_SIZE: 2,
    ReasoningAction.FORCE_EXIT: 3,
}


def derisk_strength(action: ReasoningAction) -> int:
    """How much risk an action removes (0 = none/HOLD, 3 = FORCE_EXIT)."""
    return _DERISK_STRENGTH[action]


def _llm_requested_derisk(
    signal: DecisionSignalLabel,
    veto: bool,
    narrative_failure: bool,
    *,
    position_is_open: bool,
) -> ReasoningAction:
    """Translate the LLM's de-risk intent into the strongest de-risk action it asks for.

    narrative_failure dominates: a dead thesis => FORCE_EXIT (catastrophic, M4) when a
    position exists, else VETO_ENTRY (block the pending entry).  A bearish signal or an
    explicit veto requests VETO_ENTRY (pre-entry) / REDUCE_SIZE (open position).  A
    bullish or hold signal requests no de-risk (HOLD) — and crucially, even a 'Strong
    Buy' is treated as "the LLM wants no de-risk", NEVER as a size-up request.
    """
    if narrative_failure:
        return ReasoningAction.FORCE_EXIT if position_is_open else ReasoningAction.VETO_ENTRY

    if veto:
        # An explicit veto blocks a pending entry; on an open position it reduces.
        return ReasoningAction.REDUCE_SIZE if position_is_open else ReasoningAction.VETO_ENTRY

    # Map the opinion onto de-risk intent.  Bullish/hold => no de-risk (HOLD).
    if signal in (DecisionSignalLabel.STRONG_SELL, DecisionSignalLabel.SELL):
        return ReasoningAction.FORCE_EXIT if position_is_open else ReasoningAction.VETO_ENTRY

    # HOLD / WEAK_BUY / STRONG_BUY all collapse to HOLD — the LLM may not up-signal.
    return ReasoningAction.HOLD


def clamp_to_derisk_action(
    *,
    quant: QuantBucket,
    signal: DecisionSignalLabel,
    veto: bool,
    narrative_failure: bool,
    action_received_raw: str,
    position_is_open: bool,
) -> ClampResult:
    """The pure asymmetric-trust clamp.  Monotone toward safety, by construction.

    Returns a de-risk-only ReasoningAction.  The result is NEVER more bullish than the
    quant ceiling: the applied action's de-risk strength is >= the ceiling's de-risk
    strength.  More de-risk than the LLM asked for is allowed only when the quant
    itself demands it (quant BEARISH forces at least VETO_ENTRY).

    Args:
        quant:               the quant probability bucket (the reference).
        signal:              the LLM's raw human-vocabulary signal.
        veto:                the LLM's veto bit.
        narrative_failure:   the LLM's narrative-failure bit.
        action_received_raw: the literal raw string the LLM emitted (for audit).
        position_is_open:    whether a position currently exists (entry vs exit path).

    Invariants (property-tested):
      1. The result is one of {HOLD, VETO_ENTRY, REDUCE_SIZE, FORCE_EXIT} — never a
         risk-increase (the enum cannot express one).
      2. The result is NEVER less de-risk than the quant ceiling.
      3. A raw risk-increase request sets risk_increase_clamped=True and never increases
         risk.  The risk-increase token is DROPPED — it contributes ZERO de-risk-relaxing
         power — but any coexisting de-risk demand (explicit veto, narrative_failure, a
         bearish signal) is STILL honored.  So a model that both says "Strong Buy" AND
         flags narrative_failure on an open position still FORCE_EXITs; the bullish demand
         is merely ignored, never allowed to weaken the de-risk.
    """
    raw_is_risk_increase = classify_raw_is_risk_increase(action_received_raw)

    # The strongest de-risk the LLM actually asks for.  A risk-increase RAW string carries
    # NO de-risk weight of its own — but the booleans (veto / narrative_failure) and a
    # bearish signal still do.  We therefore compute the requested de-risk from the booleans
    # and the (clamped) signal: a bullish/risk-increase signal collapses to HOLD here, which
    # is the weakest de-risk, and can never relax a stronger demand.
    requested = _llm_requested_derisk(
        signal, veto, narrative_failure, position_is_open=position_is_open
    )

    # The ceiling: the LEAST-de-risk action permitted given the quant decision.
    ceiling = quant_to_action_ceiling(quant)

    # Apply the STRONGER (more de-risk) of {requested, ceiling}.  This holds whether or not
    # the raw was a risk-increase: the raw bullish demand never appears as a de-risk-relaxing
    # term, so it cannot pull the result below either the ceiling or the LLM's own de-risk
    # demand.  A risk-increase is thus a strict no-op on the risk direction.
    applied = (
        requested if derisk_strength(requested) >= derisk_strength(ceiling) else ceiling
    )

    return ClampResult(
        action=applied,
        risk_increase_clamped=raw_is_risk_increase,
        action_received_raw=action_received_raw,
    )
