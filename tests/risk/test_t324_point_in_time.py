"""T-324 — POINT-IN-TIME proof (self-check item 8).

Every T-324 decision uses EVENT-TIME inputs only — no wall-clock, no lookahead,
no compute-time leak.  The live and backtest code paths are the SAME code: a
decision is a pure function of its event-time-stamped inputs, so replaying a
historical tick produces the identical decision regardless of WHEN it is
computed.

We prove:
  * The rule engine's decision is invariant to compute-time (same inputs, run at
    two different wall-clock instants → identical outcome incl. client_intent_id,
    which is keyed on EVENT-time block_time_ms, never wall-clock).
  * The sizer and cost gate are pure functions (no time input at all).
  * No module reads the system clock on its decision path.
"""

from __future__ import annotations

import time
from decimal import Decimal

from aats.contracts.intents import CostStack
from aats.contracts.risk import RiskConfig
from aats.risk.cost_model import CostAwareEntryGate
from aats.risk.rule_engine import HierarchicalRiskRuleEngine, RuleInputs, TakeProfitLevel
from aats.risk.sizing import FractionalKellySizer
from aats.risk.survivable_stop import StopState

EVENT_BT = 1_700_000_000_000  # a FIXED event-time block_time_ms


def test_rule_engine_decision_keyed_on_event_time_not_wall_clock():
    eng = HierarchicalRiskRuleEngine()
    stop = StopState.at_entry(mint="M", entry_fill_price=Decimal("100"))
    inp = RuleInputs(
        mint="M",
        block_time_ms=EVENT_BT,  # event-time
        mark_price=Decimal("60"),  # breaches stop
        stop=stop,
    )
    out1 = eng.evaluate(inp)
    time.sleep(0.01)  # advance WALL-CLOCK between the two evaluations
    out2 = eng.evaluate(inp)
    # Identical outcome AND identical client_intent_id — the id is keyed on the
    # EVENT-time block_time_ms, so it does not drift with compute-time.
    assert out1.intent == out2.intent
    assert str(EVENT_BT) in out1.intent.client_intent_id  # event-time in the key
    # A replay of the SAME event-time tick is idempotent (same id), which is what
    # makes the live and backtest paths produce the same decision.


def test_rule_engine_different_event_times_give_distinct_idempotency_keys():
    """Two ticks at DIFFERENT event-times → different client_intent_ids (so a
    later real tick is not deduped against an earlier one)."""
    eng = HierarchicalRiskRuleEngine()
    stop = StopState.at_entry(mint="M", entry_fill_price=Decimal("100"))
    out_a = eng.evaluate(
        RuleInputs(mint="M", block_time_ms=EVENT_BT, mark_price=Decimal("60"), stop=stop)
    )
    out_b = eng.evaluate(
        RuleInputs(mint="M", block_time_ms=EVENT_BT + 400, mark_price=Decimal("60"), stop=stop)
    )
    assert out_a.intent.client_intent_id != out_b.intent.client_intent_id


def test_take_profit_idempotency_key_uses_event_time():
    eng = HierarchicalRiskRuleEngine()
    stop = StopState.at_entry(mint="M", entry_fill_price=Decimal("100"))
    tp = TakeProfitLevel(trigger_price=Decimal("150"), fraction_bps=5000)
    out = eng.evaluate(
        RuleInputs(
            mint="M",
            block_time_ms=EVENT_BT,
            mark_price=Decimal("160"),
            stop=stop,
            take_profit_levels=(tp,),
        )
    )
    assert str(EVENT_BT) in out.intent.client_intent_id


def test_sizer_is_pure_no_time_input():
    sizer = FractionalKellySizer(RiskConfig())
    a = sizer.size(p_calibrated=0.8, uncertainty=0.2, current_aggregate_lamports=0)
    time.sleep(0.01)
    b = sizer.size(p_calibrated=0.8, uncertainty=0.2, current_aggregate_lamports=0)
    assert a == b  # identical → no wall-clock dependence


def test_cost_gate_is_pure_no_time_input():
    gate = CostAwareEntryGate(RiskConfig())
    cs = CostStack(
        expected_edge_bps=1000,
        jito_tip_bps=50,
        priority_fee_bps=30,
        entry_slippage_bps=40,
        amm_fee_bps=30,
        exit_slippage_bps=40,
        adverse_selection_bps=150,
        total_cost_bps=340,
    )
    a = gate.evaluate(cs)
    time.sleep(0.01)
    b = gate.evaluate(cs)
    assert a == b


def test_no_module_imports_wall_clock_on_decision_path():
    """Grep-style structural check: the decision modules do not call
    time.time()/datetime.now()/utcnow() on their decision path.  (The rule
    engine uses ONLY the event-time block_time_ms it is handed.)"""
    import pathlib

    risk_dir = pathlib.Path(__file__).resolve().parents[2] / "aats" / "risk"
    for name in ("sizing.py", "cost_model.py", "rule_engine.py"):
        text = (risk_dir / name).read_text(encoding="utf-8")
        # These modules must not read the system clock at all on the hot path.
        assert "time.time(" not in text, f"{name} reads wall-clock time.time()"
        assert "datetime.now(" not in text, f"{name} reads wall-clock datetime.now()"
        assert ".utcnow(" not in text, f"{name} reads wall-clock utcnow()"
