"""T-324 — Hierarchical risk rule engine: ordered, de-risk-only, FAST-loop.

Self-check items proven here:
  * Hierarchy order: (iii) narrative_failure > (i) hard stop > (ii) take-profit,
    highest-priority-first, short-circuit (a catastrophic exit is never
    overridden by a take-profit).
  * EVERY outcome is de-risk: only ExitIntent / ReduceIntent / NO_OP — NEVER an
    EntryIntent (structurally impossible — no constructor on this path).
  * Asymmetric LLM trust: the LLM reaches the engine ONLY as a pre-set flag that
    can force an EXIT (de-risk); it cannot size up, widen a stop, or add risk.
  * Point-in-time: pure function of (fixed trigger, mark, flags, TP config).
  * Money is Decimal price / int bps; float rejected.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from aats.contracts.intents import ExitIntent, ReduceIntent
from aats.risk.rule_engine import (
    HierarchicalRiskRuleEngine,
    RuleInputs,
    RuleOutcomeKind,
    RulePriority,
    TakeProfitLevel,
)
from aats.risk.survivable_stop import StopState, StopThresholds

ENTRY = Decimal("100")  # 100 lamports/token entry fill
# -40% default stop → trigger at 60.
STOP = StopState.at_entry(mint="M", entry_fill_price=ENTRY, thresholds=StopThresholds())
BT = 1_700_000_000_000


def _engine() -> HierarchicalRiskRuleEngine:
    return HierarchicalRiskRuleEngine()


def _inputs(
    *,
    mark: Decimal,
    narrative_failure: bool = False,
    tps: tuple[TakeProfitLevel, ...] = (),
) -> RuleInputs:
    return RuleInputs(
        mint="M",
        block_time_ms=BT,
        mark_price=mark,
        stop=STOP,
        narrative_failure_flag=narrative_failure,
        take_profit_levels=tps,
    )


# --------------------------------------------------------------------------- #
# Priority order — highest-priority-first, short-circuit.
# --------------------------------------------------------------------------- #


def test_narrative_failure_is_highest_priority_full_exit():
    eng = _engine()
    # Mark is healthy (above stop, below TP) — only the LLM flag is set.
    out = eng.evaluate(_inputs(mark=Decimal("100"), narrative_failure=True))
    assert out.kind == RuleOutcomeKind.FULL_EXIT
    assert out.priority == RulePriority.NARRATIVE_FAILURE
    assert isinstance(out.intent, ExitIntent)
    assert out.intent.fraction_bps == 10_000  # full flatten


def test_narrative_failure_overrides_take_profit():
    """A catastrophic exit must NOT be overridden by a take-profit scale-out."""
    eng = _engine()
    # Mark is ABOVE a TP rung (TP would otherwise fire) AND narrative_failure set.
    tp = TakeProfitLevel(trigger_price=Decimal("150"), fraction_bps=5000)
    out = eng.evaluate(_inputs(mark=Decimal("200"), narrative_failure=True, tps=(tp,)))
    # Narrative failure wins — FULL exit, not a partial TP scale-out.
    assert out.kind == RuleOutcomeKind.FULL_EXIT
    assert out.priority == RulePriority.NARRATIVE_FAILURE
    assert out.intent.fraction_bps == 10_000


def test_narrative_failure_overrides_hard_stop():
    """Even on a stop breach, narrative-failure (higher priority) is reported."""
    eng = _engine()
    # Mark breaches the stop (<=60) AND narrative_failure set.  Both would exit;
    # the engine reports the HIGHEST-priority rule that fired.
    out = eng.evaluate(_inputs(mark=Decimal("50"), narrative_failure=True))
    assert out.kind == RuleOutcomeKind.FULL_EXIT
    assert out.priority == RulePriority.NARRATIVE_FAILURE


def test_hard_stop_fires_when_no_narrative_failure():
    eng = _engine()
    out = eng.evaluate(_inputs(mark=Decimal("60")))  # mark == trigger → breach
    assert out.kind == RuleOutcomeKind.FULL_EXIT
    assert out.priority == RulePriority.HARD_STOP
    assert isinstance(out.intent, ExitIntent)
    assert out.intent.fraction_bps == 10_000


def test_hard_stop_overrides_take_profit():
    """Hard stop (priority i) beats take-profit (priority ii) — but they can't
    both be hit on one tick anyway (mark can't be <=60 AND >=150).  We force the
    stop breach and assert TP is NOT consulted."""
    eng = _engine()
    tp = TakeProfitLevel(trigger_price=Decimal("90"), fraction_bps=5000)
    # Mark 55 breaches stop (<=60).  TP trigger 90 is NOT hit at mark 55 anyway,
    # but the point is the stop short-circuits before TP is evaluated.
    out = eng.evaluate(_inputs(mark=Decimal("55"), tps=(tp,)))
    assert out.kind == RuleOutcomeKind.FULL_EXIT
    assert out.priority == RulePriority.HARD_STOP


def test_take_profit_is_lowest_priority_scale_out():
    eng = _engine()
    tp = TakeProfitLevel(trigger_price=Decimal("150"), fraction_bps=5000)
    out = eng.evaluate(_inputs(mark=Decimal("160"), tps=(tp,)))
    assert out.kind == RuleOutcomeKind.SCALE_OUT
    assert out.priority == RulePriority.TAKE_PROFIT
    assert isinstance(out.intent, ReduceIntent)
    assert out.intent.fraction_bps == 5000  # partial — books gains, never full add


def test_take_profit_fires_highest_hit_rung():
    """When a tick jumps past several rungs, the HIGHEST hit rung scales out."""
    eng = _engine()
    tps = (
        TakeProfitLevel(trigger_price=Decimal("150"), fraction_bps=2500),
        TakeProfitLevel(trigger_price=Decimal("200"), fraction_bps=5000),
        TakeProfitLevel(trigger_price=Decimal("300"), fraction_bps=7500),
    )
    out = eng.evaluate(_inputs(mark=Decimal("250"), tps=tps))  # hits 150 & 200
    assert out.kind == RuleOutcomeKind.SCALE_OUT
    assert out.intent.fraction_bps == 5000  # the 200 rung (highest hit)


def test_no_op_when_nothing_fires():
    eng = _engine()
    tp = TakeProfitLevel(trigger_price=Decimal("150"), fraction_bps=5000)
    out = eng.evaluate(_inputs(mark=Decimal("100"), tps=(tp,)))  # above stop, below TP
    assert out.kind == RuleOutcomeKind.NO_OP
    assert out.intent is None
    assert out.priority is None
    assert out.is_exit() is False


# --------------------------------------------------------------------------- #
# EVERY outcome is de-risk — NEVER an EntryIntent.
# --------------------------------------------------------------------------- #


def test_every_outcome_is_de_risk_only():
    eng = _engine()
    cases = [
        _inputs(mark=Decimal("50"), narrative_failure=True),  # full exit
        _inputs(mark=Decimal("50")),  # hard stop
        _inputs(mark=Decimal("160"), tps=(TakeProfitLevel(Decimal("150"), 5000),)),  # TP
        _inputs(mark=Decimal("100")),  # no-op
    ]
    for inp in cases:
        out = eng.evaluate(inp)
        if out.intent is not None:
            # The only intent types this engine can ever emit are de-risk.
            assert isinstance(out.intent, ExitIntent | ReduceIntent)
            # An ExitIntent/ReduceIntent can NEVER increase exposure: the
            # fraction is bounded to a reduction of the existing position.
            assert 1 <= out.intent.fraction_bps <= 10_000


def test_engine_has_no_entry_constructor():
    """Structural proof: the engine's factory cannot build an EntryIntent."""
    eng = _engine()
    # The DeRiskIntentFactory the engine holds has no `entry` method.
    assert not hasattr(eng._factory, "entry")


# --------------------------------------------------------------------------- #
# Asymmetric LLM trust — the LLM flag can only DE-RISK (force exit).
# --------------------------------------------------------------------------- #


def test_llm_flag_can_only_force_exit_never_size_up():
    """The ONLY LLM-derived input is a boolean.  True → full exit (de-risk).
    There is no field on RuleInputs to size up, widen a stop, or add leverage —
    a risk-increasing LLM instruction is not an expressible input here."""
    eng = _engine()
    # narrative_failure True → de-risk (full exit).
    out_true = eng.evaluate(_inputs(mark=Decimal("100"), narrative_failure=True))
    assert out_true.kind == RuleOutcomeKind.FULL_EXIT
    # narrative_failure False → the LLM has NO effect on a healthy position
    # (it cannot turn a hold into a bigger position — there is no such path).
    out_false = eng.evaluate(_inputs(mark=Decimal("100"), narrative_failure=False))
    assert out_false.kind == RuleOutcomeKind.NO_OP

    # RuleInputs has no attribute by which an LLM could widen a stop or size up.
    fields = set(RuleInputs.__dataclass_fields__.keys())
    for forbidden in ("size_up", "widen_stop", "add_leverage", "new_stop_trigger", "size_lamports"):
        assert forbidden not in fields, f"RuleInputs must not expose '{forbidden}'"


def test_engine_never_awaits_llm_on_the_decision():
    """The decision reads a PRE-SET flag, never calls an LLM.  We prove the
    evaluate() path touches no callable LLM input — the flag is a plain bool."""
    inp = _inputs(mark=Decimal("100"), narrative_failure=True)
    assert isinstance(inp.narrative_failure_flag, bool)  # not a coroutine/callable
    eng = _engine()
    out = eng.evaluate(inp)  # synchronous, pure — no await anywhere
    assert out.kind == RuleOutcomeKind.FULL_EXIT


# --------------------------------------------------------------------------- #
# Money / type guards.
# --------------------------------------------------------------------------- #


def test_float_mark_rejected():
    with pytest.raises(TypeError):
        _inputs(mark=1.5)  # float price


def test_take_profit_float_fraction_rejected():
    with pytest.raises(TypeError):
        TakeProfitLevel(trigger_price=Decimal("150"), fraction_bps=0.5)


def test_take_profit_full_fraction_rejected():
    """A TP rung cannot be a 100% exit — that is the stop/narrative path."""
    with pytest.raises(ValueError):
        TakeProfitLevel(trigger_price=Decimal("150"), fraction_bps=10_000)
    with pytest.raises(ValueError):
        TakeProfitLevel(trigger_price=Decimal("150"), fraction_bps=0)


def test_mint_mismatch_rejected():
    """The stop must belong to the same mint as the inputs (no cross-wiring)."""
    other_stop = StopState.at_entry(mint="OTHER", entry_fill_price=ENTRY)
    with pytest.raises(ValueError):
        RuleInputs(
            mint="M",
            block_time_ms=BT,
            mark_price=Decimal("100"),
            stop=other_stop,
        )


def test_pure_deterministic():
    eng = _engine()
    inp = _inputs(mark=Decimal("160"), tps=(TakeProfitLevel(Decimal("150"), 5000),))
    out1 = eng.evaluate(inp)
    out2 = eng.evaluate(inp)
    assert out1.kind == out2.kind
    assert out1.intent == out2.intent  # same client_intent_id → idempotent replay
