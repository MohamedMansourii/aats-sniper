"""End-to-end Reasoner tests (self-check items 4, 5, 8 + de-risk-only Intent).

Proves:
  - the full pipeline emits a schema-valid, de-risk-only ReasoningVerdict;
  - narrative_failure=true fires the M4 catastrophic-exit trigger END-TO-END (FORCE_EXIT +
    a full ExitIntent built via the de-risk-only factory);
  - agreement (bullish quant + benign sentiment + non-veto LLM) passes through to HOLD with
    NO de-risk Intent and NO clamp;
  - every conflict cell resolves toward lower risk through the full Reasoner;
  - point-in-time: a DecisionSignal/MCS event_time mismatch is REFUSED (no lookahead);
  - the Reasoner can NEVER emit an EntryIntent — it has no constructor for one.
"""

from __future__ import annotations

import pytest

from aats.contracts.intents import ExitIntent, ReduceIntent, VetoIntent
from aats.contracts.models import ReasoningAction, ReasoningVerdict
from aats.reasoning.reasoner import DeRiskReasoner
from aats.reasoning.router import LLMRouter, MockReasonerBackend
from aats.reasoning.schema import DecisionSignalLabel, LLMRawVerdict
from tests.reasoning.fixtures import (
    COST_NO_EDGE,
    COST_WITH_EDGE,
    EVENT_TIME_OTHER_SLOT,
    make_mcs,
    make_shill_mcs,
    make_signal,
)


def _reasoner_with(raw: LLMRawVerdict) -> DeRiskReasoner:
    """A Reasoner whose backend always returns a fixed raw verdict (deterministic)."""
    backend = MockReasonerBackend(force=raw)
    return DeRiskReasoner(router=LLMRouter(local_backend=backend))


_HOLD_RAW = LLMRawVerdict(
    decision_signal=DecisionSignalLabel.HOLD,
    confidence=0.7,
    veto=False,
    narrative_failure=False,
    rationale="no concerns",
)


# ---------------------------------------------------------------------------
# Output is always a schema-valid, de-risk-only verdict
# ---------------------------------------------------------------------------


def test_output_is_schema_valid_reasoning_verdict():
    reasoner = _reasoner_with(_HOLD_RAW)
    out = reasoner.adjudicate(
        decision_signal=make_signal(),
        mcs=make_mcs(),
        **COST_WITH_EDGE,
        position_is_open=False,
    )
    assert isinstance(out.verdict, ReasoningVerdict)
    assert out.verdict.action in {
        ReasoningAction.HOLD,
        ReasoningAction.VETO_ENTRY,
        ReasoningAction.REDUCE_SIZE,
        ReasoningAction.FORCE_EXIT,
    }
    # 0 <= confidence <= 1 enforced by the contract validator.
    assert 0.0 <= out.verdict.confidence <= 1.0


def test_agreement_passes_through_to_hold_with_no_intent():
    """Bullish quant + benign sentiment + non-veto LLM => HOLD, no de-risk Intent."""
    reasoner = _reasoner_with(_HOLD_RAW)
    out = reasoner.adjudicate(
        decision_signal=make_signal(p_calibrated=0.8, uncertainty=0.1),
        mcs=make_mcs(conviction=0.7, synchronicity=0.1, account_age_median_days=200.0),
        **COST_WITH_EDGE,
        position_is_open=False,
    )
    assert out.verdict.action is ReasoningAction.HOLD
    assert out.intent is None  # HOLD adds no de-risk Intent
    assert out.verdict.risk_increase_clamped is False


# ---------------------------------------------------------------------------
# narrative_failure => M4 catastrophic FORCE_EXIT, end-to-end
# ---------------------------------------------------------------------------


_NARRATIVE_FAILURE_RAW = LLMRawVerdict(
    decision_signal=DecisionSignalLabel.STRONG_SELL,
    confidence=0.95,
    veto=True,
    narrative_failure=True,
    rationale="the dev wallet dumped; the thesis is dead",
)


def test_narrative_failure_open_position_fires_force_exit_intent():
    """narrative_failure on an OPEN position => FORCE_EXIT + full-exit ExitIntent (M4)."""
    reasoner = _reasoner_with(_NARRATIVE_FAILURE_RAW)
    out = reasoner.adjudicate(
        decision_signal=make_signal(p_calibrated=0.3, uncertainty=0.4),
        mcs=make_mcs(conviction=0.1),
        **COST_WITH_EDGE,
        position_is_open=True,  # there is a live position to flatten
    )
    assert out.verdict.action is ReasoningAction.FORCE_EXIT
    # The M4 catastrophic-exit trigger: a FULL exit Intent, secure mode.
    assert isinstance(out.intent, ExitIntent)
    assert out.intent.fraction_bps == 10_000  # full exit
    assert out.intent.exit_mode == "secure"
    # And the audit trail records the catastrophic narrative failure.
    assert "applied_narrative_failure=True" in out.verdict.reason


def test_narrative_failure_pre_entry_vetoes_with_veto_intent():
    """narrative_failure pre-entry => VETO_ENTRY + VetoIntent (block the pending entry)."""
    reasoner = _reasoner_with(_NARRATIVE_FAILURE_RAW)
    out = reasoner.adjudicate(
        decision_signal=make_signal(),
        mcs=make_mcs(),
        **COST_WITH_EDGE,
        position_is_open=False,
    )
    assert out.verdict.action is ReasoningAction.VETO_ENTRY
    assert isinstance(out.intent, VetoIntent)


# ---------------------------------------------------------------------------
# Conflict resolves toward lower risk through the full Reasoner
# ---------------------------------------------------------------------------


def test_manufactured_sentiment_de_risks_even_when_llm_bullish():
    """A bullish LLM raw verdict + coordinated-shill MCS => still de-risks (never a buy)."""
    bullish_raw = LLMRawVerdict(
        decision_signal=DecisionSignalLabel.STRONG_BUY,
        confidence=0.9,
        veto=False,
        narrative_failure=False,
        rationale="looks great",
    )
    reasoner = _reasoner_with(bullish_raw)
    out = reasoner.adjudicate(
        decision_signal=make_signal(p_calibrated=0.85, uncertainty=0.1),
        mcs=make_shill_mcs(),  # MANUFACTURED
        **COST_WITH_EDGE,
        position_is_open=False,
    )
    # Even with a bullish quant AND a bullish LLM, manufactured sentiment vetoes.
    assert out.verdict.action is ReasoningAction.VETO_ENTRY
    assert isinstance(out.intent, VetoIntent)


def test_marginal_edge_de_risks():
    """No net edge after costs => the Reasoner de-risks (cost-aware).

    Uses the REAL offline mock backend (not a forced raw), so the mock reads the safe cost
    view from the prompt and vetoes a candidate with no edge net of costs.
    """
    reasoner = DeRiskReasoner()  # default offline mock backend (cost-aware heuristic)
    out = reasoner.adjudicate(
        decision_signal=make_signal(p_calibrated=0.8, uncertainty=0.1),
        mcs=make_mcs(),
        **COST_NO_EDGE,  # cost > edge => net_edge_bps_after_costs <= 0
        position_is_open=False,
    )
    # The mock backend reads the safe cost view and vetoes a no-edge candidate.
    assert out.verdict.action is ReasoningAction.VETO_ENTRY


# ---------------------------------------------------------------------------
# Point-in-time: event_time mismatch is refused (no lookahead)
# ---------------------------------------------------------------------------


def test_event_time_mismatch_is_refused():
    """DecisionSignal and MCS at DIFFERENT slots => the prompt builder refuses (leak)."""
    reasoner = _reasoner_with(_HOLD_RAW)
    with pytest.raises(ValueError, match="point-in-time violation"):
        reasoner.adjudicate(
            decision_signal=make_signal(),  # EVENT_TIME
            mcs=make_mcs(event_time=EVENT_TIME_OTHER_SLOT),  # different slot
            **COST_WITH_EDGE,
            position_is_open=False,
        )


# ---------------------------------------------------------------------------
# The Reasoner cannot emit an EntryIntent (de-risk-only by type)
# ---------------------------------------------------------------------------


def test_reasoner_holds_only_a_de_risk_factory():
    """The reasoner's factory has NO entry method — an EntryIntent is unreachable."""
    reasoner = DeRiskReasoner()
    factory = reasoner._factory  # the de-risk-only factory
    assert not hasattr(factory, "entry")
    # It CAN do exit/reduce/veto; it has no path to an EntryIntent.
    assert hasattr(factory, "exit")
    assert hasattr(factory, "reduce")
    assert hasattr(factory, "veto")


def test_every_possible_intent_from_reasoner_is_de_risk():
    """Across forced raw verdicts, the produced Intent is ALWAYS a de-risk variant."""
    de_risk_types = (ExitIntent, ReduceIntent, VetoIntent, type(None))
    raws = [
        _HOLD_RAW,
        _NARRATIVE_FAILURE_RAW,
        LLMRawVerdict(
            decision_signal=DecisionSignalLabel.STRONG_BUY,
            confidence=0.9,
            veto=False,
            narrative_failure=False,
            rationale="bull",
        ),
        LLMRawVerdict(
            decision_signal=DecisionSignalLabel.SELL,
            confidence=0.8,
            veto=True,
            narrative_failure=False,
            rationale="bear",
        ),
    ]
    for raw in raws:
        for is_open in (False, True):
            reasoner = _reasoner_with(raw)
            out = reasoner.adjudicate(
                decision_signal=make_signal(),
                mcs=make_mcs(),
                **COST_WITH_EDGE,
                position_is_open=is_open,
            )
            assert isinstance(out.intent, de_risk_types)
