"""The conflict / agreement decision matrix (T-313).

(quant probability bucket) × (LLM signal) × (sentiment risk) → de-risk action.

THE RULE: AGREEMENT PASSES THROUGH; CONFLICT RESOLVES TOWARD THE LOWER-RISK SIDE.
================================================================================
This module maps the discretized market state onto a *floor* of de-risk strength that
the matrix demands.  It is COMBINED with the LLM clamp (clamp.py): the applied action
is the STRONGER (more de-risk) of the clamp result and this matrix floor.  Because both
inputs can only ever ADD de-risk, the combination can never increase risk.

Worked rules (the doc table below is the law; the code mirrors it cell-for-cell):
  - quant BULLISH + LLM veto                  => NO ENTRY (veto)         [conflict → low-risk]
  - quant NEUTRAL + MANUFACTURED sentiment     => de-risk (veto/reduce)   [contrarian]
  - MANUFACTURED sentiment, ANY quant          => at least de-risk; never a buy trigger
  - quant BEARISH, ANY LLM, ANY sentiment      => veto/exit               [quant already de-risking]
  - agreement (quant BULLISH + LLM not-veto + benign sentiment) => HOLD (no de-risk added)

ADVERSARIAL SENTIMENT IS NEVER A BUY SIGNAL.  A MANUFACTURED bucket can only raise the
de-risk floor; there is no cell in this matrix that lowers de-risk because sentiment is
"hot".  (BUILD-DIRECTIVE adversarial-sentiment rule; FR-008, AC-021.)

THE FULL MATRIX (doc table) — value is the MINIMUM de-risk action the matrix imposes
====================================================================================
Legend: H=HOLD (no de-risk added), V=VETO_ENTRY, R=REDUCE_SIZE, X=FORCE_EXIT.
"applied" = stronger(clamp, matrix-floor); position-open promotes V→R, X stays X.

| quant   | LLM signal      | sentiment    | matrix floor | rationale                         |
|---------|-----------------|--------------|--------------|-----------------------------------|
| BULLISH | not-veto/bull   | BENIGN       | H            | AGREEMENT — pass through          |
| BULLISH | not-veto/bull   | ELEVATED     | H            | mild; clamp/MCS shrink size elsewhere |
| BULLISH | not-veto/bull   | MANUFACTURED | V            | CONFLICT — manufactured euphoria → de-risk |
| BULLISH | veto/bear       | any          | V            | CONFLICT — LLM sees a fire → no entry |
| NEUTRAL | not-veto        | BENIGN       | H            | neutral, nothing adversarial      |
| NEUTRAL | not-veto        | ELEVATED     | H            | mild — handled by sizing, not veto |
| NEUTRAL | not-veto        | MANUFACTURED | V            | CONFLICT — manufactured + no edge → de-risk |
| NEUTRAL | veto/bear       | any          | V            | de-risk toward the cautious side  |
| BEARISH | any             | any          | V            | quant already de-risking → veto   |
| any     | narrative_fail  | any          | V (X if open)| thesis dead → block / catastrophic exit |

The matrix returns a FLOOR; the final applied action is stronger(clamp, floor) and is
promoted for an open position (V→R, the position must be reduced not merely un-entered).
"""

from __future__ import annotations

from dataclasses import dataclass

from aats.contracts.models import ReasoningAction
from aats.reasoning.clamp import derisk_strength
from aats.reasoning.schema import (
    DecisionSignalLabel,
    QuantBucket,
    SentimentRisk,
    signal_risk_rank,
)


@dataclass(frozen=True)
class MatrixDecision:
    """The de-risk floor the matrix imposes, plus which cell fired (for the audit log)."""

    floor_action: ReasoningAction
    cell: str  # human-readable cell id, e.g. "BULLISH×veto×MANUFACTURED"
    is_conflict: bool  # True when quant and LLM/sentiment disagree on direction


# Hold-or-bullish signals — these express NO de-risk intent on the LLM's part.
_BULLISH_OR_HOLD = frozenset(
    {
        DecisionSignalLabel.HOLD,
        DecisionSignalLabel.WEAK_BUY,
        DecisionSignalLabel.STRONG_BUY,
    }
)


def _llm_is_bearish_or_veto(signal: DecisionSignalLabel, veto: bool) -> bool:
    """True if the LLM is signalling de-risk (a sell/strong-sell or an explicit veto)."""
    return veto or signal in (DecisionSignalLabel.SELL, DecisionSignalLabel.STRONG_SELL)


def evaluate_matrix(
    *,
    quant: QuantBucket,
    signal: DecisionSignalLabel,
    veto: bool,
    narrative_failure: bool,
    sentiment: SentimentRisk,
    position_is_open: bool,
) -> MatrixDecision:
    """Compute the de-risk FLOOR the matrix imposes for this market state.

    The returned floor is combined (by the Reasoner) with the clamp via the STRONGER-of
    rule.  The matrix can only ever RAISE the de-risk floor; no cell lowers it because
    sentiment is hot or because the LLM is bullish.

    Conflict resolves toward the lower-risk (more de-risk) side:
      - quant-bullish + LLM-veto      => VETO_ENTRY (no entry)
      - quant-neutral + manufactured  => VETO_ENTRY (de-risk)
      - quant-bearish (any)           => VETO_ENTRY
      - narrative_failure (any)       => VETO_ENTRY pre-entry, FORCE_EXIT if open
    """
    llm_derisk = _llm_is_bearish_or_veto(signal, veto)

    # 1) narrative_failure dominates everything: the story is dead.
    if narrative_failure:
        if position_is_open:
            return MatrixDecision(
                floor_action=ReasoningAction.FORCE_EXIT,
                cell=f"{quant}×narrative_failure×{sentiment}[open]",
                is_conflict=True,
            )
        return MatrixDecision(
            floor_action=ReasoningAction.VETO_ENTRY,
            cell=f"{quant}×narrative_failure×{sentiment}",
            is_conflict=True,
        )

    # 2) quant BEARISH: the quant is already de-risking; the floor is at least a veto.
    if quant is QuantBucket.BEARISH:
        floor = (
            ReasoningAction.REDUCE_SIZE if position_is_open else ReasoningAction.VETO_ENTRY
        )
        return MatrixDecision(
            floor_action=floor,
            cell=f"BEARISH×{signal}×{sentiment}",
            # Conflict iff the LLM is bullish against a bearish quant.
            is_conflict=signal in _BULLISH_OR_HOLD and not llm_derisk
            and signal_risk_rank(signal) > signal_risk_rank(DecisionSignalLabel.HOLD),
        )

    # 3) MANUFACTURED sentiment, any quant: coordinated shill → de-risk, never a buy.
    if sentiment is SentimentRisk.MANUFACTURED:
        floor = (
            ReasoningAction.REDUCE_SIZE if position_is_open else ReasoningAction.VETO_ENTRY
        )
        return MatrixDecision(
            floor_action=floor,
            cell=f"{quant}×{signal}×MANUFACTURED",
            is_conflict=quant is QuantBucket.BULLISH or signal in _BULLISH_OR_HOLD,
        )

    # 4) LLM is de-risking (sell/veto) against a non-bearish quant => CONFLICT → veto.
    if llm_derisk:
        floor = (
            ReasoningAction.REDUCE_SIZE if position_is_open else ReasoningAction.VETO_ENTRY
        )
        return MatrixDecision(
            floor_action=floor,
            cell=f"{quant}×llm_derisk×{sentiment}",
            is_conflict=quant is QuantBucket.BULLISH,  # bullish quant vs de-risk LLM
        )

    # 5) AGREEMENT: quant non-bearish, LLM not de-risking, sentiment not manufactured.
    #    The matrix adds NO de-risk (HOLD).  Size/entry is decided by the cost-gated
    #    SNIPE path — the reasoning floor here is "I do not veto."
    return MatrixDecision(
        floor_action=ReasoningAction.HOLD,
        cell=f"{quant}×{signal}×{sentiment}[agreement]",
        is_conflict=False,
    )


def combine_clamp_and_matrix(
    clamp_action: ReasoningAction,
    matrix_floor: ReasoningAction,
) -> ReasoningAction:
    """Apply the STRONGER (more de-risk) of the clamp action and the matrix floor.

    Both inputs are de-risk-only.  The combination is de-risk-only and is >= each input
    in de-risk strength — so the result can never be more bullish than either side.
    Conflict therefore ALWAYS resolves toward the lower-risk side.
    """
    return (
        clamp_action
        if derisk_strength(clamp_action) >= derisk_strength(matrix_floor)
        else matrix_floor
    )
