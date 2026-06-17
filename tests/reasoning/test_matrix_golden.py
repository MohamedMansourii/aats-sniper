"""Conflict/agreement matrix golden-case tests (self-check item 4).

Every cell of the (quant × LLM signal × sentiment) matrix has a golden case and resolves
toward the LOWER-RISK side.  Agreement passes through (HOLD = no de-risk added); conflict
resolves to a de-risk floor.

We also fuzz the full cross-product and assert the matrix floor is ALWAYS a de-risk-only
action and that no cell ever LOWERS de-risk because sentiment is hot or the LLM is bullish.
"""

from __future__ import annotations

import itertools

from aats.contracts.models import ReasoningAction
from aats.reasoning.clamp import derisk_strength
from aats.reasoning.matrix import combine_clamp_and_matrix, evaluate_matrix
from aats.reasoning.schema import (
    DecisionSignalLabel,
    QuantBucket,
    SentimentRisk,
)

_DE_RISK_ACTIONS = {
    ReasoningAction.HOLD,
    ReasoningAction.VETO_ENTRY,
    ReasoningAction.REDUCE_SIZE,
    ReasoningAction.FORCE_EXIT,
}


# ---------------------------------------------------------------------------
# Golden cells (the doc table in matrix.py is the law)
# ---------------------------------------------------------------------------


def _floor(quant, signal, veto, narr, sentiment, is_open):
    return evaluate_matrix(
        quant=quant,
        signal=signal,
        veto=veto,
        narrative_failure=narr,
        sentiment=sentiment,
        position_is_open=is_open,
    ).floor_action


def test_agreement_bullish_benign_passes_through():
    """quant BULLISH + bull/hold LLM + BENIGN sentiment => HOLD (no de-risk added)."""
    floor = _floor(
        QuantBucket.BULLISH, DecisionSignalLabel.WEAK_BUY, False, False, SentimentRisk.BENIGN, False
    )
    assert floor is ReasoningAction.HOLD


def test_conflict_bullish_quant_llm_veto_no_entry():
    """quant BULLISH + LLM veto => VETO_ENTRY (LLM sees a fire the quant missed)."""
    floor = _floor(
        QuantBucket.BULLISH, DecisionSignalLabel.SELL, True, False, SentimentRisk.BENIGN, False
    )
    assert floor is ReasoningAction.VETO_ENTRY


def test_conflict_bullish_quant_manufactured_sentiment_de_risk():
    """quant BULLISH + MANUFACTURED sentiment => VETO_ENTRY (manufactured euphoria)."""
    floor = _floor(
        QuantBucket.BULLISH,
        DecisionSignalLabel.WEAK_BUY,
        False,
        False,
        SentimentRisk.MANUFACTURED,
        False,
    )
    assert floor is ReasoningAction.VETO_ENTRY


def test_neutral_quant_manufactured_sentiment_de_risk():
    """quant NEUTRAL + MANUFACTURED sentiment => VETO_ENTRY (no edge + shill)."""
    floor = _floor(
        QuantBucket.NEUTRAL,
        DecisionSignalLabel.HOLD,
        False,
        False,
        SentimentRisk.MANUFACTURED,
        False,
    )
    assert floor is ReasoningAction.VETO_ENTRY


def test_neutral_quant_benign_is_hold():
    floor = _floor(
        QuantBucket.NEUTRAL, DecisionSignalLabel.HOLD, False, False, SentimentRisk.BENIGN, False
    )
    assert floor is ReasoningAction.HOLD


def test_neutral_quant_elevated_is_hold_not_veto():
    """ELEVATED (not manufactured) sentiment alone does not veto — sizing handles it."""
    floor = _floor(
        QuantBucket.NEUTRAL, DecisionSignalLabel.HOLD, False, False, SentimentRisk.ELEVATED, False
    )
    assert floor is ReasoningAction.HOLD


def test_bearish_quant_any_llm_vetoes():
    """quant BEARISH => VETO_ENTRY regardless of LLM/sentiment (quant already de-risks)."""
    for signal in DecisionSignalLabel:
        for sentiment in SentimentRisk:
            floor = _floor(QuantBucket.BEARISH, signal, False, False, sentiment, False)
            assert floor is ReasoningAction.VETO_ENTRY


def test_bearish_quant_open_position_reduces():
    floor = _floor(
        QuantBucket.BEARISH, DecisionSignalLabel.HOLD, False, False, SentimentRisk.BENIGN, True
    )
    assert floor is ReasoningAction.REDUCE_SIZE


def test_narrative_failure_pre_entry_vetoes():
    floor = _floor(
        QuantBucket.BULLISH, DecisionSignalLabel.STRONG_BUY, False, True, SentimentRisk.BENIGN, False
    )
    assert floor is ReasoningAction.VETO_ENTRY


def test_narrative_failure_open_position_force_exits():
    """narrative_failure on an OPEN position => FORCE_EXIT (M4 catastrophic exit)."""
    floor = _floor(
        QuantBucket.BULLISH, DecisionSignalLabel.STRONG_BUY, False, True, SentimentRisk.BENIGN, True
    )
    assert floor is ReasoningAction.FORCE_EXIT


def test_llm_sell_against_bullish_quant_de_risks():
    """quant BULLISH + LLM Sell (no explicit veto) => VETO_ENTRY (conflict → low risk)."""
    floor = _floor(
        QuantBucket.BULLISH, DecisionSignalLabel.SELL, False, False, SentimentRisk.BENIGN, False
    )
    assert floor is ReasoningAction.VETO_ENTRY


# ---------------------------------------------------------------------------
# Full cross-product fuzz: every cell is de-risk-only; manufactured never lowers de-risk
# ---------------------------------------------------------------------------


def test_every_matrix_cell_is_de_risk_only():
    n = 0
    for quant, signal, veto, narr, sentiment, is_open in itertools.product(
        QuantBucket, DecisionSignalLabel, [False, True], [False, True], SentimentRisk, [False, True]
    ):
        floor = _floor(quant, signal, veto, narr, sentiment, is_open)
        assert floor in _DE_RISK_ACTIONS
        n += 1
    print(f"\n[matrix-golden] checked {n} matrix cells; all de-risk-only")


def test_manufactured_sentiment_never_lowers_de_risk():
    """For fixed (quant, signal, veto, narr, is_open), MANUFACTURED >= BENIGN in de-risk.

    Sentiment can only ever RAISE the de-risk floor — hot/manufactured sentiment is never
    a reason to be LESS cautious.
    """
    for quant, signal, veto, narr, is_open in itertools.product(
        QuantBucket, DecisionSignalLabel, [False, True], [False, True], [False, True]
    ):
        benign = derisk_strength(_floor(quant, signal, veto, narr, SentimentRisk.BENIGN, is_open))
        manufactured = derisk_strength(
            _floor(quant, signal, veto, narr, SentimentRisk.MANUFACTURED, is_open)
        )
        assert manufactured >= benign, (quant, signal, veto, narr, is_open)


def test_combine_takes_the_stronger_de_risk():
    """combine_clamp_and_matrix returns the more-de-risk of the two inputs."""
    assert (
        combine_clamp_and_matrix(ReasoningAction.HOLD, ReasoningAction.VETO_ENTRY)
        is ReasoningAction.VETO_ENTRY
    )
    assert (
        combine_clamp_and_matrix(ReasoningAction.FORCE_EXIT, ReasoningAction.HOLD)
        is ReasoningAction.FORCE_EXIT
    )
    assert (
        combine_clamp_and_matrix(ReasoningAction.REDUCE_SIZE, ReasoningAction.VETO_ENTRY)
        is ReasoningAction.REDUCE_SIZE
    )
