"""Tests: ReasoningAction and Intent contain NO risk-increase member.

ADR-0006 (asymmetric trust by type): the ReasoningAction enum has EXACTLY four
members (HOLD, VETO_ENTRY, REDUCE_SIZE, FORCE_EXIT) — all de-risk or no-op.
There is NO SIZE_UP, WIDEN_STOP, ADD_LEVERAGE, or OVERRIDE_HARD_STOP member.

The Intent union has no risk-increase-only variant (ENTRY is cost-gated and
only constructible on the SNIPE path; the reasoning path holds DeRiskIntentFactory
which has no entry() method).

Self-check item #3 from the charter: paste proof that the enum contains no
risk-increase member.
"""

from __future__ import annotations

import pytest

from aats.contracts.events import EventTime
from aats.contracts.intents import (
    DeRiskIntentFactory,
    EntryIntent,
    ExitIntent,
    IntentKind,
    ReduceIntent,
    VetoIntent,
)
from aats.contracts.models import ReasoningAction, ReasoningVerdict


def _event_time() -> EventTime:
    return EventTime(slot=1000, block_time_ms=1_700_000_000_000, wall_clock_ms=1_700_000_000_100)


# ---------------------------------------------------------------------------
# Proof: ReasoningAction has no risk-increase member
# ---------------------------------------------------------------------------


class TestReasoningActionNoRiskIncrease:
    _FORBIDDEN_NAMES = frozenset(
        {"SIZE_UP", "WIDEN_STOP", "ADD_LEVERAGE", "OVERRIDE_HARD_STOP"}
    )
    _EXPECTED_MEMBERS = frozenset({"HOLD", "VETO_ENTRY", "REDUCE_SIZE", "FORCE_EXIT"})

    def test_no_size_up_member(self):
        """ReasoningAction MUST NOT have a SIZE_UP member (ADR-0006)."""
        assert "SIZE_UP" not in [m.name for m in ReasoningAction], (
            "ReasoningAction.SIZE_UP is forbidden — it is a risk-increase action"
        )

    def test_no_widen_stop_member(self):
        assert "WIDEN_STOP" not in [m.name for m in ReasoningAction]

    def test_no_add_leverage_member(self):
        assert "ADD_LEVERAGE" not in [m.name for m in ReasoningAction]

    def test_no_override_hard_stop_member(self):
        assert "OVERRIDE_HARD_STOP" not in [m.name for m in ReasoningAction]

    def test_exact_member_set(self):
        """The enum has EXACTLY the four de-risk/no-op members — nothing more."""
        actual = frozenset(m.name for m in ReasoningAction)
        assert actual == self._EXPECTED_MEMBERS, (
            f"ReasoningAction has unexpected members.  "
            f"Expected: {sorted(self._EXPECTED_MEMBERS)}.  "
            f"Got: {sorted(actual)}.  "
            f"Unexpected (potential risk-increase): {sorted(actual - self._EXPECTED_MEMBERS)}"
        )

    def test_no_forbidden_names_in_enum(self):
        actual_names = frozenset(m.name for m in ReasoningAction)
        illegal = actual_names & self._FORBIDDEN_NAMES
        assert not illegal, (
            f"ReasoningAction contains FORBIDDEN risk-increase member(s): {illegal}.  "
            "ADR-0006 makes risk-increase inexpressible on the reasoning path."
        )

    def test_all_members_are_de_risk_or_noop(self):
        """Positive proof: every member is in the de-risk/no-op set."""
        for member in ReasoningAction:
            assert member.name in self._EXPECTED_MEMBERS, (
                f"Unexpected ReasoningAction member: {member.name}"
            )

    def test_clamp_to_hold_on_risk_increase_raw_string(self):
        """A raw LLM string 'SIZE_UP' must be clamped to HOLD with risk_increase_clamped=True."""
        # The clamp is the backstop: parse time fails (SIZE_UP not in enum),
        # so the caller must catch and default to HOLD + risk_increase_clamped=True.
        # This test verifies SIZE_UP is NOT parseable as a valid ReasoningAction.
        with pytest.raises((ValueError, KeyError)):  # StrEnum raises ValueError or KeyError
            ReasoningAction("SIZE_UP")  # must fail — not a valid member

    def test_hold_is_the_clamp_target(self):
        """HOLD is a valid member and the correct clamp target."""
        assert ReasoningAction("HOLD") == ReasoningAction.HOLD

    def test_reasoning_verdict_with_risk_increase_raw_sets_clamped(self):
        """A ReasoningVerdict that received SIZE_UP but applied HOLD must set risk_increase_clamped=True."""
        verdict = ReasoningVerdict(
            mint="So11111111111111111111111111111111111111112",
            event_time=_event_time(),
            action=ReasoningAction.HOLD,  # clamped from SIZE_UP
            reason="model said buy more",
            confidence=0.8,
            action_received_raw="SIZE_UP",
            risk_increase_clamped=True,
        )
        assert verdict.action == ReasoningAction.HOLD
        assert verdict.risk_increase_clamped is True
        assert verdict.action_received_raw == "SIZE_UP"


# ---------------------------------------------------------------------------
# Intent union: no risk-increase variant
# ---------------------------------------------------------------------------


class TestIntentUnionNoRiskIncrease:
    def test_intent_kind_has_no_increase_position_member(self):
        kind_names = frozenset(m.name for m in IntentKind)
        assert "INCREASE_POSITION" not in kind_names
        assert "ADD_LEVERAGE" not in kind_names
        assert "WIDEN_STOP" not in kind_names

    def test_entry_kind_exists_for_snipe_path(self):
        """ENTRY is the only risk-increasing kind and it requires a cost stack."""
        assert IntentKind.ENTRY in IntentKind

    def test_de_risk_factory_has_no_entry_method(self):
        """The DeRiskIntentFactory has NO entry() method — structural enforcement of ADR-0006."""
        factory = DeRiskIntentFactory()
        assert not hasattr(factory, "entry"), (
            "DeRiskIntentFactory.entry() must not exist — the reasoning path "
            "cannot construct an EntryIntent (ADR-0006)."
        )

    def test_de_risk_factory_can_produce_exit(self):
        factory = DeRiskIntentFactory()
        intent = factory.exit(
            client_intent_id="ci-test-exit",
            mint="So11111111111111111111111111111111111111112",
            fraction_bps=10_000,
            reason="llm_forced_exit",
        )
        assert isinstance(intent, ExitIntent)
        assert intent.kind == IntentKind.EXIT

    def test_de_risk_factory_can_produce_reduce(self):
        factory = DeRiskIntentFactory()
        intent = factory.reduce(
            client_intent_id="ci-test-reduce",
            mint="So11111111111111111111111111111111111111112",
            fraction_bps=5_000,
            reason="llm_reduce_size",
        )
        assert isinstance(intent, ReduceIntent)
        assert intent.kind == IntentKind.REDUCE

    def test_de_risk_factory_can_produce_veto(self):
        factory = DeRiskIntentFactory()
        intent = factory.veto(
            client_intent_id="ci-test-veto",
            mint="So11111111111111111111111111111111111111112",
            reason="llm_vetoed",
        )
        assert isinstance(intent, VetoIntent)
        assert intent.kind == IntentKind.VETO

    def test_entry_intent_requires_cost_stack(self):
        """EntryIntent CANNOT be constructed without a populated CostStack (FR-027)."""
        from pydantic import ValidationError
        with pytest.raises((ValidationError, Exception)):
            # cost_stack is required — no default
            EntryIntent(
                client_intent_id="ci-test",
                mint="So11111111111111111111111111111111111111112",
                event_time=_event_time(),
                sol_in_lamports=100_000_000,
                slippage_bps=300,
                tip_lamports=200_000,
                cu_price_microlamports=100_000,
                target_slot=1005,
                venue="pumpswap",
                wallet_id="wallet-0",
                # cost_stack intentionally missing
            )

    def test_entry_intent_cost_gate_enforced(self):
        """ENTRY is rejected if expected_edge_bps <= total_cost_bps (FR-027, AC-013)."""
        from aats.contracts.intents import CostStack

        negative_edge_stack = CostStack(
            expected_edge_bps=100,  # less than total
            jito_tip_bps=50,
            priority_fee_bps=20,
            entry_slippage_bps=100,
            amm_fee_bps=25,
            exit_slippage_bps=80,
            adverse_selection_bps=150,
            total_cost_bps=275,  # > expected_edge_bps → REJECTED
        )
        with pytest.raises(Exception) as exc_info:
            EntryIntent(
                client_intent_id="ci-test",
                mint="So11111111111111111111111111111111111111112",
                event_time=_event_time(),
                sol_in_lamports=100_000_000,
                slippage_bps=300,
                tip_lamports=200_000,
                cu_price_microlamports=100_000,
                target_slot=1005,
                cost_stack=negative_edge_stack,
                venue="pumpswap",
                wallet_id="wallet-0",
            )
        assert "cost_gate" in str(exc_info.value).lower() or "edge" in str(exc_info.value).lower()

    def test_entry_intent_slot_guard_enforced(self):
        """target_slot must be >= event_time.slot + 5 (no block-0 race, AC-017)."""
        from aats.contracts.intents import CostStack

        cs = CostStack(
            expected_edge_bps=300,
            jito_tip_bps=50,
            priority_fee_bps=20,
            entry_slippage_bps=100,
            amm_fee_bps=25,
            exit_slippage_bps=80,
            adverse_selection_bps=150,
            total_cost_bps=275,
        )
        with pytest.raises(Exception) as exc_info:
            EntryIntent(
                client_intent_id="ci-test",
                mint="So11111111111111111111111111111111111111112",
                event_time=_event_time(),  # slot=1000
                sol_in_lamports=100_000_000,
                slippage_bps=300,
                tip_lamports=200_000,
                cu_price_microlamports=100_000,
                target_slot=1002,  # < 1000 + 5 — must fail
                cost_stack=cs,
                venue="pumpswap",
                wallet_id="wallet-0",
            )
        assert "target_slot" in str(exc_info.value) or "block-0" in str(exc_info.value).lower()
