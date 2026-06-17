"""T-324 — ASYMMETRIC-TRUST proof (self-check item 5).

The non-waivable law: an LLM / de-risk / sentiment signal may ONLY reduce risk.
It may NEVER size up, widen or move a stop, add leverage, override a hard stop,
or lift a cap.  This test feeds risk-INCREASING signals into every T-324 surface
and asserts they are rejected or STRUCTURALLY IMPOSSIBLE.

Surfaces:
  * sizing.py        — a 'signal' > 1.0 (a size-up) is rejected at construction.
  * rule_engine.py   — the LLM input is a bool that can only force an EXIT; there
                       is no input by which it could size up / widen a stop.
  * cost_model.py    — no LLM/sentiment input exists at all (decoded facts only).
  * survivable_stop  — the stop can only be TIGHTENED, never widened.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from aats.contracts.risk import RiskConfig
from aats.risk.cost_model import CostAwareEntryGate
from aats.risk.rule_engine import HierarchicalRiskRuleEngine, RuleInputs, RuleOutcomeKind
from aats.risk.sizing import DeRiskSignals, FractionalKellySizer
from aats.risk.survivable_stop import StopState, StopThresholds

# --------------------------------------------------------------------------- #
# Sizing — a signal that tries to GROW size is structurally impossible.
# --------------------------------------------------------------------------- #


def test_sizing_signal_cannot_size_up():
    # A de-risk factor is bounded to (0,1]; > 1.0 (a size-up) is rejected.
    with pytest.raises(ValueError):
        DeRiskSignals(sentiment_factor=1.01)
    with pytest.raises(ValueError):
        DeRiskSignals(confidence_factor=5.0)


def test_sizing_caps_cannot_be_lifted_by_any_input():
    """No probability/uncertainty/signal/config combination breaches the caps."""
    sizer = FractionalKellySizer(RiskConfig())
    # Most aggressive possible legal inputs: perfect prob, zero uncertainty,
    # no de-risk.  Size MUST still respect the per-trade cap.
    d = sizer.size(p_calibrated=1.0, uncertainty=0.0, current_aggregate_lamports=0)
    assert d.size_lamports <= RiskConfig().per_trade_cap_lamports


def test_riskconfig_refuses_widened_caps():
    """The caps are tighten-only: a config that WIDENS a cap is refused."""
    with pytest.raises(ValueError):
        RiskConfig(per_trade_cap_lamports=200_000_000)  # > 0.1 SOL floor
    with pytest.raises(ValueError):
        RiskConfig(max_aggregate_lamports=1_000_000_000)  # > 0.5 SOL floor
    with pytest.raises(ValueError):
        RiskConfig(kelly_fraction_cap=Decimal("0.50"))  # > 1/4 Kelly


# --------------------------------------------------------------------------- #
# Rule engine — the LLM input can only force an EXIT.
# --------------------------------------------------------------------------- #


def test_rule_engine_llm_input_is_de_risk_only():
    eng = HierarchicalRiskRuleEngine()
    stop = StopState.at_entry(mint="M", entry_fill_price=Decimal("100"))
    # The LLM flag True forces a FULL EXIT (de-risk).
    out = eng.evaluate(
        RuleInputs(
            mint="M",
            block_time_ms=1_700_000_000_000,
            mark_price=Decimal("100"),
            stop=stop,
            narrative_failure_flag=True,
        )
    )
    assert out.kind == RuleOutcomeKind.FULL_EXIT  # de-risk
    # There is NO RuleInputs field that could express a risk-increase.
    fields = set(RuleInputs.__dataclass_fields__.keys())
    forbidden = {"size_up", "widen_stop", "add_leverage", "new_stop", "leverage", "increase"}
    assert fields.isdisjoint(forbidden)


def test_rule_engine_factory_has_no_entry_path():
    eng = HierarchicalRiskRuleEngine()
    assert not hasattr(eng._factory, "entry")  # cannot construct an EntryIntent


# --------------------------------------------------------------------------- #
# Cost gate — NO LLM/sentiment input exists.
# --------------------------------------------------------------------------- #


def test_cost_gate_consumes_only_decoded_costs():
    gate = CostAwareEntryGate(RiskConfig())
    # evaluate() takes a CostStack only — there is no LLM/sentiment parameter by
    # which a narrative could relax the cost gate.
    import inspect

    params = set(inspect.signature(gate.evaluate).parameters.keys())
    assert params == {"cost_stack"}, f"cost gate must take only a CostStack, got {params}"


# --------------------------------------------------------------------------- #
# Survivable stop — TIGHTEN-only; a widen is refused (used by the rule engine).
# --------------------------------------------------------------------------- #


def test_stop_cannot_be_widened():
    stop = StopState.at_entry(
        mint="M",
        entry_fill_price=Decimal("100"),
        thresholds=StopThresholds(hard_stop_drawdown_bps=4000),
    )
    # trigger = 60.  Tighten (raise) to 70 is allowed.
    tightened = stop.tighten(Decimal("70"))
    assert tightened.trigger_price == Decimal("70")
    # Widen (lower) to 50 is REFUSED — a stop never gives more room to lose.
    with pytest.raises(ValueError):
        stop.tighten(Decimal("50"))
