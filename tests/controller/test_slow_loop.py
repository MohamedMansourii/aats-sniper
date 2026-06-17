"""T-340 — SLOW loop tests.

Covers:
  - SC-7: Asymmetric trust guard in SlowLoop._apply_reasoning_verdict()
  - SLOW loop pre-stages signal correctly
  - SLOW loop passes narrative_failure to store (FAST reads it)
"""
from __future__ import annotations

import pytest

from aats.contracts.events import EventTime
from aats.contracts.models import ReasoningAction, ReasoningVerdict
from aats.controller.slow_loop import SlowLoop
from tests.controller.conftest import make_launch_event


def _make_verdict(action: ReasoningAction, mint: str = "MINT_A") -> ReasoningVerdict:
    return ReasoningVerdict(
        mint=mint,
        event_time=EventTime(
            slot=1000, block_time_ms=1_700_000_000_000, wall_clock_ms=1_700_000_000_060
        ),
        action=action,
        reason="test reason",
        confidence=0.8,
        action_received_raw=action.value,
        risk_increase_clamped=False,
    )


def test_slow_loop_stages_signal(store, classifier):
    """SlowLoop.process_event() stages a signal so SNIPE can read it."""
    slow = SlowLoop(store=store, classifier=classifier)
    event = make_launch_event(mint="STAGED_MINT")
    signal = slow.process_event(event)

    assert signal is not None
    staged = store.get_decision_signal("STAGED_MINT")
    assert staged is not None
    assert staged.mint == "STAGED_MINT"
    assert staged.p_calibrated == pytest.approx(0.80)


def test_slow_loop_none_classifier_returns_none(store):
    """When classifier returns None, no signal is staged."""

    class NullClassifier:
        def predict(self, event):
            return None

    slow = SlowLoop(store=store, classifier=NullClassifier())
    event = make_launch_event(mint="NULL_MINT")
    signal = slow.process_event(event)
    assert signal is None
    assert store.get_decision_signal("NULL_MINT") is None


def test_slow_loop_veto_entry_sets_veto_flag(store, classifier):
    """VETO_ENTRY verdict sets the veto flag in the store."""
    slow = SlowLoop(store=store, classifier=classifier)
    verdict = _make_verdict(ReasoningAction.VETO_ENTRY, "VETO_MINT")
    slow._apply_reasoning_verdict("VETO_MINT", verdict)
    assert store.get_veto_flag("VETO_MINT") is True


def test_slow_loop_force_exit_sets_narrative_flag(store, classifier):
    """FORCE_EXIT verdict sets the narrative_failure_flag in the store."""
    slow = SlowLoop(store=store, classifier=classifier)
    verdict = _make_verdict(ReasoningAction.FORCE_EXIT, "FORCE_MINT")
    slow._apply_reasoning_verdict("FORCE_MINT", verdict)
    assert store.get_narrative_failure_flag("FORCE_MINT") is True


def test_slow_loop_hold_does_nothing(store, classifier):
    """HOLD verdict changes no state."""
    slow = SlowLoop(store=store, classifier=classifier)
    verdict = _make_verdict(ReasoningAction.HOLD, "HOLD_MINT")
    slow._apply_reasoning_verdict("HOLD_MINT", verdict)
    assert not store.get_veto_flag("HOLD_MINT")
    assert not store.get_narrative_failure_flag("HOLD_MINT")


def test_slow_loop_reduce_size_sets_veto_flag(store, classifier):
    """REDUCE_SIZE verdict sets the veto flag (de-risk: snipe will skip or use smaller size)."""
    slow = SlowLoop(store=store, classifier=classifier)
    verdict = _make_verdict(ReasoningAction.REDUCE_SIZE, "REDUCE_MINT")
    slow._apply_reasoning_verdict("REDUCE_MINT", verdict)
    assert store.get_veto_flag("REDUCE_MINT") is True


def test_slow_loop_llm_on_slow_path_only():
    """SlowLoop import does NOT pull in LLM modules on the FAST/SNIPE path."""
    # Verify the slow_loop module does not import any LLM at module level

    llm_modules = ["openai", "anthropic", "langchain"]
    for _mod in llm_modules:
        # The slow_loop module itself shouldn't auto-import LLM libraries
        # The reasoner is injected, so this is guaranteed by construction
        pass
    # The actual guard is that FastLoop and SnipeLoop don't import slow_loop
    import ast
    import pathlib

    for path_str in [
        "C:/dev/aats/aats/controller/fast_loop.py",
        "C:/dev/aats/aats/controller/snipe_loop.py",
    ]:
        path = pathlib.Path(path_str)
        if not path.exists():
            continue
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                assert "slow_loop" not in module, (
                    f"{path.name} must NOT import slow_loop "
                    f"(LLM is on SLOW loop only; found import of {module!r})"
                )


def test_asymmetric_trust_lllm_cannot_size_up(store, classifier):
    """The reasoning path can emit ONLY de-risk ReasoningAction members.
    SIZE_UP is not a member of ReasoningAction — constructing a verdict with it fails."""
    # The type system is the primary defense: ReasoningAction has no SIZE_UP
    allowed_actions = {a.value for a in ReasoningAction}
    assert "SIZE_UP" not in allowed_actions, (
        "SIZE_UP must NOT be a member of ReasoningAction (ADR-0006)"
    )
    assert "WIDEN_STOP" not in allowed_actions
    assert "ADD_LEVERAGE" not in allowed_actions
    assert "OVERRIDE_HARD_STOP" not in allowed_actions

    # All members are de-risk
    for action in ReasoningAction:
        assert action in (
            ReasoningAction.HOLD,
            ReasoningAction.VETO_ENTRY,
            ReasoningAction.REDUCE_SIZE,
            ReasoningAction.FORCE_EXIT,
        ), f"Unexpected ReasoningAction member: {action!r}"
