"""Tests: FSM transition guards (data-models.md §7, ADR-0007).

Verifies:
  1. All legal FSM transitions are accepted.
  2. All illegal transitions raise ValueError.
  3. No transition silently no-ops.
  4. The FSM exactly covers the FLAT→...→FLAT cycle.
"""

from __future__ import annotations

import pytest

from aats.contracts.positions import FSM_LEGAL_TRANSITIONS, FSMState, validate_fsm_transition

# ---------------------------------------------------------------------------
# Legal transitions
# ---------------------------------------------------------------------------


class TestLegalFSMTransitions:
    @pytest.mark.parametrize(
        "from_state, to_state",
        [
            (FSMState.IDLE, FSMState.ENTERING),
            (FSMState.ENTERING, FSMState.OPEN),
            (FSMState.ENTERING, FSMState.VETOED),
            (FSMState.OPEN, FSMState.CLOSING),
            (FSMState.CLOSING, FSMState.CLOSED),
        ],
    )
    def test_legal_transition_does_not_raise(self, from_state: FSMState, to_state: FSMState):
        """All legal transitions must pass without raising."""
        validate_fsm_transition(from_state, to_state)  # no exception

    def test_full_happy_path(self):
        """Verify the full IDLE → ENTERING → OPEN → CLOSING → CLOSED path."""
        path = [
            (FSMState.IDLE, FSMState.ENTERING),
            (FSMState.ENTERING, FSMState.OPEN),
            (FSMState.OPEN, FSMState.CLOSING),
            (FSMState.CLOSING, FSMState.CLOSED),
        ]
        for from_s, to_s in path:
            validate_fsm_transition(from_s, to_s)

    def test_veto_path(self):
        """ENTERING → VETOED is legal (LLM/gate veto)."""
        validate_fsm_transition(FSMState.ENTERING, FSMState.VETOED)


# ---------------------------------------------------------------------------
# Illegal transitions — ALL must raise, never silently no-op
# ---------------------------------------------------------------------------


class TestIllegalFSMTransitions:
    @pytest.mark.parametrize(
        "from_state, to_state, description",
        [
            # Double-entry: AC-012 — the most critical guard
            (FSMState.ENTERING, FSMState.ENTERING, "double-entering"),
            (FSMState.OPEN, FSMState.ENTERING, "re-entering open position"),
            (FSMState.OPEN, FSMState.IDLE, "stepping backward"),
            (FSMState.CLOSED, FSMState.IDLE, "recycling closed position"),
            (FSMState.CLOSED, FSMState.ENTERING, "entering from closed"),
            (FSMState.CLOSED, FSMState.OPEN, "re-opening closed position"),
            (FSMState.VETOED, FSMState.ENTERING, "re-entering vetoed"),
            (FSMState.VETOED, FSMState.OPEN, "opening vetoed"),
            (FSMState.IDLE, FSMState.OPEN, "skipping entering"),
            (FSMState.IDLE, FSMState.CLOSING, "skipping to closing"),
            (FSMState.IDLE, FSMState.CLOSED, "skipping to closed"),
            (FSMState.ENTERING, FSMState.CLOSING, "skipping open"),
            (FSMState.ENTERING, FSMState.CLOSED, "skipping to closed directly"),
            (FSMState.CLOSING, FSMState.OPEN, "stepping backward"),
            (FSMState.CLOSING, FSMState.ENTERING, "backward to entering"),
        ],
    )
    def test_illegal_transition_raises(
        self, from_state: FSMState, to_state: FSMState, description: str
    ):
        """Every illegal FSM transition must raise ValueError — never silently pass."""
        with pytest.raises(ValueError) as exc_info:
            validate_fsm_transition(from_state, to_state)
        # The error message must identify it as an illegal transition
        err = str(exc_info.value).lower()
        assert (
            "illegal" in err
            or "not a legal" in err
            or "fsm" in err
        ), (
            f"Expected a clear error message for illegal transition "
            f"{from_state.value}→{to_state.value} ({description}), "
            f"got: {exc_info.value}"
        )

    def test_same_state_transition_is_illegal(self):
        """No self-loop transitions are legal."""
        for state in FSMState:
            with pytest.raises(ValueError):
                validate_fsm_transition(state, state)


# ---------------------------------------------------------------------------
# Completeness: legal set is exactly what the blueprint defines
# ---------------------------------------------------------------------------


class TestFSMLegalSetCompleteness:
    def test_legal_transitions_count(self):
        """The legal transition set has exactly 5 transitions (data-models.md §7)."""
        assert len(FSM_LEGAL_TRANSITIONS) == 5, (
            f"Expected 5 legal FSM transitions, got {len(FSM_LEGAL_TRANSITIONS)}: "
            f"{sorted((a.value, b.value) for a, b in FSM_LEGAL_TRANSITIONS)}"
        )

    def test_idle_enters_only_entering(self):
        """From IDLE, the only legal next state is ENTERING."""
        reachable_from_idle = {b for a, b in FSM_LEGAL_TRANSITIONS if a == FSMState.IDLE}
        assert reachable_from_idle == {FSMState.ENTERING}

    def test_closed_is_terminal(self):
        """CLOSED has no outgoing legal transitions."""
        reachable_from_closed = {b for a, b in FSM_LEGAL_TRANSITIONS if a == FSMState.CLOSED}
        assert reachable_from_closed == set()

    def test_vetoed_is_terminal(self):
        """VETOED has no outgoing legal transitions."""
        reachable_from_vetoed = {b for a, b in FSM_LEGAL_TRANSITIONS if a == FSMState.VETOED}
        assert reachable_from_vetoed == set()
