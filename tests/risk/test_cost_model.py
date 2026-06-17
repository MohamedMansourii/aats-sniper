"""T-324 — Cost-aware entry gate: REJECT iff expected_edge_bps <= total_cost_bps.

Self-check items proven here:
  * Cost gate REJECTS sub-cost trades (edge <= total cost) — including ties.
  * The 150bps UNCALIBRATED haircut floor (OQ-007, C-11) is enforced.
  * Fail-closed on negative lines, total mismatch, non-positive edge.
  * Money is integer bps; the gate never sizes/widens/adds.
"""

from __future__ import annotations

import pytest

from aats.contracts.intents import CostStack
from aats.contracts.risk import RiskConfig
from aats.risk.cost_model import (
    REASON_EDGE_BELOW_COST,
    REASON_HAIRCUT_BELOW_FLOOR,
    REASON_NEGATIVE_EDGE,
    REASON_NEGATIVE_LINE,
    REASON_TOTAL_MISMATCH,
    CostAwareEntryGate,
)


def _cost_stack(
    *,
    edge: int,
    jito: int = 50,
    prio: int = 30,
    entry_slip: int = 40,
    amm: int = 30,
    exit_slip: int = 40,
    adverse: int = 150,
    total: int | None = None,
) -> CostStack:
    parts = jito + prio + entry_slip + amm + exit_slip + adverse
    return CostStack(
        expected_edge_bps=edge,
        jito_tip_bps=jito,
        priority_fee_bps=prio,
        entry_slippage_bps=entry_slip,
        amm_fee_bps=amm,
        exit_slippage_bps=exit_slip,
        adverse_selection_bps=adverse,
        total_cost_bps=total if total is not None else parts,
    )


def test_edge_strictly_above_cost_passes():
    gate = CostAwareEntryGate(RiskConfig())
    cs = _cost_stack(edge=1000)  # total = 340; edge 1000 > 340
    d = gate.evaluate(cs)
    assert d.passed is True
    assert d.reason is None
    assert d.margin_bps == 1000 - cs.total_cost_bps
    assert gate.allows_entry(cs) is True


def test_edge_below_cost_rejected():
    gate = CostAwareEntryGate(RiskConfig())
    cs = _cost_stack(edge=100)  # total = 340; edge 100 < 340
    d = gate.evaluate(cs)
    assert d.passed is False
    assert d.reason == REASON_EDGE_BELOW_COST
    assert gate.allows_entry(cs) is False


def test_edge_equal_to_cost_rejected_no_free_trades():
    """Equality is a REJECT — ties go to 'no trade' (FR-027, AC-013)."""
    gate = CostAwareEntryGate(RiskConfig())
    # Build a stack whose total exactly equals the edge.
    cs = _cost_stack(edge=340)  # total = 340 exactly
    assert cs.total_cost_bps == 340
    d = gate.evaluate(cs)
    assert d.passed is False
    assert d.reason == REASON_EDGE_BELOW_COST
    assert d.margin_bps == 0


def test_edge_one_bps_above_cost_passes():
    gate = CostAwareEntryGate(RiskConfig())
    cs = _cost_stack(edge=341)  # total = 340; 1 bps net edge
    d = gate.evaluate(cs)
    assert d.passed is True
    assert d.margin_bps == 1


# --------------------------------------------------------------------------- #
# The 150bps UNCALIBRATED haircut floor (OQ-007, C-11).
# --------------------------------------------------------------------------- #


def test_haircut_floor_is_150_default():
    gate = CostAwareEntryGate(RiskConfig())
    assert gate.haircut_floor_bps == 150


def test_sub_floor_haircut_rejected_at_contract_level():
    """The CostStack itself refuses adverse_selection_bps < 150 (defense #1)."""
    with pytest.raises(ValueError):
        _cost_stack(edge=1000, adverse=100)  # CostStack validator raises


def test_gate_honors_widened_haircut_floor():
    """A config that WIDENS the floor (e.g. 200) rejects a 150bps haircut."""
    cfg = RiskConfig(pre_calibration_haircut_bps=200)
    gate = CostAwareEntryGate(cfg)
    assert gate.haircut_floor_bps == 200
    # A stack with a 150bps haircut (legal at contract level) is below the
    # WIDENED floor → reject.
    cs = _cost_stack(edge=10_000, adverse=150)
    d = gate.evaluate(cs)
    assert d.passed is False
    assert d.reason == REASON_HAIRCUT_BELOW_FLOOR


def test_gate_floor_cannot_be_lowered_below_150():
    """RiskConfig refuses a sub-150 haircut config; the floor stays >= 150."""
    with pytest.raises(ValueError):
        RiskConfig(pre_calibration_haircut_bps=100)


# --------------------------------------------------------------------------- #
# Fail-closed checks.
# --------------------------------------------------------------------------- #


def test_total_mismatch_rejected():
    """An understated total that hides a real cost is a sub-cost trade — reject."""
    gate = CostAwareEntryGate(RiskConfig())
    # Declared total 100 but parts sum to 340 — understated.
    cs = _cost_stack(edge=1000, total=100)
    d = gate.evaluate(cs)
    assert d.passed is False
    assert d.reason == REASON_TOTAL_MISMATCH


def test_non_positive_edge_rejected():
    gate = CostAwareEntryGate(RiskConfig())
    cs = _cost_stack(edge=0)
    d = gate.evaluate(cs)
    assert d.passed is False
    # edge<=0 caught by either negative-edge or edge-below-cost; both are rejects.
    assert d.reason in (REASON_NEGATIVE_EDGE, REASON_EDGE_BELOW_COST)


def test_negative_edge_rejected():
    gate = CostAwareEntryGate(RiskConfig())
    cs = _cost_stack(edge=-50)
    d = gate.evaluate(cs)
    assert d.passed is False


def test_negative_cost_line_rejected():
    """A negative cost line (optimistic 'cost') is a lie — reject."""
    gate = CostAwareEntryGate(RiskConfig())
    # jito_tip_bps negative; rebuild a stack whose total matches the parts so the
    # ONLY failing check is the negative line.
    cs = CostStack(
        expected_edge_bps=1000,
        jito_tip_bps=-10,
        priority_fee_bps=30,
        entry_slippage_bps=40,
        amm_fee_bps=30,
        exit_slippage_bps=40,
        adverse_selection_bps=150,
        total_cost_bps=-10 + 30 + 40 + 30 + 40 + 150,
    )
    d = gate.evaluate(cs)
    assert d.passed is False
    assert d.reason == REASON_NEGATIVE_LINE


def test_gate_default_config_floor_is_150():
    """No config supplied → still uses the 150bps floor (fail-closed default)."""
    gate = CostAwareEntryGate()
    assert gate.haircut_floor_bps == 150


# --------------------------------------------------------------------------- #
# Point-in-time / purity: same inputs → same verdict (no wall-clock).
# --------------------------------------------------------------------------- #


def test_gate_is_pure_deterministic():
    gate = CostAwareEntryGate(RiskConfig())
    cs = _cost_stack(edge=500)
    d1 = gate.evaluate(cs)
    d2 = gate.evaluate(cs)
    assert d1 == d2  # pure function — no wall-clock, no hidden state


def test_gate_matches_entry_intent_contract_cost_gate():
    """The pre-Intent gate and the EntryIntent contract gate agree on the rule.

    Both REJECT iff edge <= total.  The pre-Intent gate is the earlier check so
    we never attempt to construct a cost-failing Intent.
    """
    from aats.contracts.events import EventTime
    from aats.contracts.intents import EntryIntent

    gate = CostAwareEntryGate(RiskConfig())
    ev = EventTime(slot=100, block_time_ms=1_700_000_000_000, wall_clock_ms=1_700_000_000_050)

    # Passing stack: building the EntryIntent must SUCCEED.
    passing = _cost_stack(edge=1000)
    assert gate.allows_entry(passing) is True
    EntryIntent(
        client_intent_id="ok",
        mint="M",
        event_time=ev,
        sol_in_lamports=50_000_000,
        slippage_bps=300,
        tip_lamports=10_000,
        cu_price_microlamports=1000,
        target_slot=ev.slot + 5,
        cost_stack=passing,
        venue="v",
        wallet_id="w",
    )

    # Failing stack: the gate rejects AND the EntryIntent contract refuses too.
    failing = _cost_stack(edge=100)
    assert gate.allows_entry(failing) is False
    with pytest.raises(ValueError):
        EntryIntent(
            client_intent_id="bad",
            mint="M",
            event_time=ev,
            sol_in_lamports=50_000_000,
            slippage_bps=300,
            tip_lamports=10_000,
            cu_price_microlamports=1000,
            target_slot=ev.slot + 5,
            cost_stack=failing,
            venue="v",
            wallet_id="w",
        )
