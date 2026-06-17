"""Schema-enforcement tests (self-check item 2).

Proves:
  - malformed / out-of-range / wrong-enum LLM outputs are ALL rejected by the schema;
  - a rejecting backend makes the router fail safe to the CONSERVATIVE action (de-risk);
  - we never parse free text with a regex — only schema-validated objects;
  - the bus-facing ReasoningVerdict.action is always a de-risk-only member.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from aats.contracts.models import ReasoningAction
from aats.reasoning.router import (
    LLMRouter,
    RouteTier,
    SignalBucketKey,
    conservative_fallback_verdict,
)
from aats.reasoning.schema import (
    DecisionSignalLabel,
    LLMRawVerdict,
    QuantBucket,
    SentimentRisk,
)

# ---------------------------------------------------------------------------
# The raw schema rejects bad objects (no regex — pydantic validation only)
# ---------------------------------------------------------------------------


def test_wrong_enum_signal_is_rejected():
    with pytest.raises(ValidationError):
        LLMRawVerdict(
            decision_signal="SIZE_UP",  # not in the closed enum
            confidence=0.9,
            veto=False,
            narrative_failure=False,
            rationale="x",
        )


def test_out_of_range_confidence_high_is_rejected():
    with pytest.raises(ValidationError):
        LLMRawVerdict(
            decision_signal=DecisionSignalLabel.HOLD,
            confidence=1.5,  # > 1
            veto=False,
            narrative_failure=False,
            rationale="x",
        )


def test_out_of_range_confidence_negative_is_rejected():
    with pytest.raises(ValidationError):
        LLMRawVerdict(
            decision_signal=DecisionSignalLabel.HOLD,
            confidence=-0.01,
            veto=False,
            narrative_failure=False,
            rationale="x",
        )


def test_missing_field_is_rejected():
    with pytest.raises(ValidationError):
        LLMRawVerdict(  # type: ignore[call-arg]
            decision_signal=DecisionSignalLabel.HOLD,
            confidence=0.5,
            veto=False,
            # narrative_failure missing
            rationale="x",
        )


def test_extra_field_is_rejected():
    """extra='forbid' — a smuggled 'size' or 'leverage' field is rejected at parse."""
    with pytest.raises(ValidationError):
        LLMRawVerdict(
            decision_signal=DecisionSignalLabel.HOLD,
            confidence=0.5,
            veto=False,
            narrative_failure=False,
            rationale="x",
            size_up=True,  # type: ignore[call-arg]  smuggled risk-increase field
        )


def test_non_boolean_veto_string_is_rejected():
    """A string that is not coercible to bool is rejected (no silent truthiness)."""
    with pytest.raises(ValidationError):
        LLMRawVerdict(
            decision_signal=DecisionSignalLabel.HOLD,
            confidence=0.5,
            veto="please buy",  # type: ignore[arg-type]
            narrative_failure=False,
            rationale="x",
        )


# ---------------------------------------------------------------------------
# A backend that emits malformed output => router fails safe to conservative action
# ---------------------------------------------------------------------------


class _RaisingBackend:
    """A backend that ALWAYS raises ValidationError (simulates malformed LLM output)."""

    backend_id = "raising-backend"

    def reason(self, *, system_prompt, user_prompt, temperature):
        # Simulate instructor failing to coerce the model output into the schema.
        LLMRawVerdict(  # raises ValidationError
            decision_signal="not-a-signal",  # type: ignore[arg-type]
            confidence=0.5,
            veto=False,
            narrative_failure=False,
            rationale="x",
        )
        raise AssertionError("unreachable")


_KEY = SignalBucketKey(
    quant=QuantBucket.BULLISH,
    sentiment=SentimentRisk.BENIGN,
    position_is_open=False,
    net_edge_sign=1,
)


def test_router_fails_safe_on_malformed_output():
    router = LLMRouter(local_backend=_RaisingBackend(), max_retries=2)
    routed = router.route(system_prompt="sys", user_prompt="usr", key=_KEY)
    # Fail-safe: the conservative fallback is a VETO (de-risk), tier FALLBACK.
    assert routed.tier is RouteTier.FALLBACK
    assert routed.raw.veto is True
    assert routed.raw.decision_signal is DecisionSignalLabel.SELL


def test_conservative_fallback_is_a_de_risk():
    v = conservative_fallback_verdict("test")
    assert v.veto is True
    assert v.decision_signal in (DecisionSignalLabel.SELL, DecisionSignalLabel.STRONG_SELL)
    # The fallback never up-signals.
    assert v.decision_signal not in (
        DecisionSignalLabel.STRONG_BUY,
        DecisionSignalLabel.WEAK_BUY,
    )


# ---------------------------------------------------------------------------
# The bus-facing action enum has no risk-increase member (the PRIMARY defense)
# ---------------------------------------------------------------------------


def test_reasoning_action_has_no_risk_increase_member():
    names = {m.name for m in ReasoningAction}
    assert names == {"HOLD", "VETO_ENTRY", "REDUCE_SIZE", "FORCE_EXIT"}
    for forbidden in ("SIZE_UP", "WIDEN_STOP", "ADD_LEVERAGE", "OVERRIDE_HARD_STOP"):
        assert forbidden not in names
