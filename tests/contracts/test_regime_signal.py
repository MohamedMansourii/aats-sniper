"""Tests: the frozen RegimeSignal contract (M2-CP-04, ADR-0014).

Proves the non-waivable laws of the SLOW-loop regime contract:
  - it is a multiclass STATE + calibrated probabilities (a distribution) + uncertainty;
  - NO price / size / win-rate / success-rate / realized-mult field anywhere;
  - the de-risk directive codomain is de-risk-or-inert ONLY (no risk-increase member);
  - ACCUMULATION / NEUTRAL map to NONE (provably INERT); DISTRIBUTION -> REDUCE;
    RUG_IN_PROGRESS -> FORCE_EXIT;
  - the contract-layer taxonomy matches the model-layer taxonomy (no drift);
  - event_time is carried (point-in-time).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from aats.contracts.events import EventTime
from aats.contracts.models import (
    RegimeDeRiskDirective,
    RegimeSignal,
    RegimeState,
    regime_derisk_directive,
)


def _event_time(slot: int = 1000) -> EventTime:
    return EventTime(slot=slot, block_time_ms=1_700_000_000_000, wall_clock_ms=1_700_000_000_100)


def _signal(
    *,
    regime: RegimeState = RegimeState.NEUTRAL,
    p_accumulation: float = 0.20,
    p_neutral: float = 0.50,
    p_distribution: float = 0.20,
    p_rug_in_progress: float = 0.10,
    uncertainty: float = 0.20,
    mint: str = "MINT_R",
) -> RegimeSignal:
    return RegimeSignal(
        mint=mint,
        event_time=_event_time(),
        model_version="regime-lgbm-v1.0.0-BOOTSTRAP",
        taxonomy_version="1.0.0",
        regime=regime,
        p_accumulation=p_accumulation,
        p_neutral=p_neutral,
        p_distribution=p_distribution,
        p_rug_in_progress=p_rug_in_progress,
        uncertainty=uncertainty,
        is_bootstrap_not_real=True,
    )


# ---------------------------------------------------------------------------
# Happy path + structure
# ---------------------------------------------------------------------------


class TestRegimeSignalConstruction:
    def test_valid_signal_constructs(self):
        sig = _signal()
        assert sig.regime is RegimeState.NEUTRAL
        assert sig.event_time.slot == 1000
        assert sig.is_bootstrap_not_real is True

    def test_signal_is_frozen(self):
        sig = _signal()
        with pytest.raises(ValidationError):
            sig.regime = RegimeState.RUG_IN_PROGRESS  # type: ignore[misc]

    def test_probabilities_must_sum_to_one(self):
        with pytest.raises(ValidationError) as exc:
            _signal(
                p_accumulation=0.5,
                p_neutral=0.5,
                p_distribution=0.5,
                p_rug_in_progress=0.5,
            )
        assert "distribution" in str(exc.value).lower() or "sum" in str(exc.value).lower()

    def test_probability_out_of_range_rejected(self):
        with pytest.raises(ValidationError):
            _signal(
                p_accumulation=1.5,
                p_neutral=0.0,
                p_distribution=-0.5,
                p_rug_in_progress=0.0,
            )

    def test_regime_must_be_argmax_class(self):
        """Declaring a STATE that is NOT the top-probability class is rejected."""
        with pytest.raises(ValidationError) as exc:
            _signal(
                regime=RegimeState.ACCUMULATION,  # but rug has the max prob below
                p_accumulation=0.10,
                p_neutral=0.10,
                p_distribution=0.10,
                p_rug_in_progress=0.70,
            )
        assert "argmax" in str(exc.value).lower() or "top-probability" in str(exc.value).lower()

    def test_regime_argmax_tie_allows_declared_top(self):
        """On a tie for the max, any tied class is a legal declared STATE."""
        sig = _signal(
            regime=RegimeState.RUG_IN_PROGRESS,
            p_accumulation=0.25,
            p_neutral=0.25,
            p_distribution=0.25,
            p_rug_in_progress=0.25,
        )
        assert sig.regime is RegimeState.RUG_IN_PROGRESS


# ---------------------------------------------------------------------------
# NO price / size / win-rate — HONESTY CLAUSE
# ---------------------------------------------------------------------------


class TestRegimeSignalNoForbiddenFields:
    _FORBIDDEN_SUBSTRINGS = (
        "price",
        "size",
        "win_rate",
        "winrate",
        "success_rate",
        "realized_mult",
        "pnl",
        "lamports",
        "target",
    )

    def test_no_price_size_or_winrate_field(self):
        for field_name in RegimeSignal.model_fields:
            lowered = field_name.lower()
            for forbidden in self._FORBIDDEN_SUBSTRINGS:
                assert forbidden not in lowered, (
                    f"RegimeSignal must carry NO {forbidden!r}-like field "
                    f"(found {field_name!r}). A regime is a STATE + calibrated prob + "
                    "uncertainty — never a price, a size, or a win-rate (AC-037)."
                )

    def test_fields_are_exactly_the_expected_set(self):
        expected = {
            "mint",
            "event_time",
            "model_version",
            "taxonomy_version",
            "regime",
            "p_accumulation",
            "p_neutral",
            "p_distribution",
            "p_rug_in_progress",
            "uncertainty",
            "is_bootstrap_not_real",
        }
        assert set(RegimeSignal.model_fields) == expected


# ---------------------------------------------------------------------------
# De-risk directive: de-risk-or-inert codomain ONLY (no risk-increase member)
# ---------------------------------------------------------------------------


class TestRegimeDeRiskDirectiveNoRiskIncrease:
    _FORBIDDEN_NAMES = frozenset(
        {"SIZE_UP", "WIDEN_STOP", "RELAX_STOP", "DELAY_EXIT", "ADD_LEVERAGE", "OVERRIDE_HARD_STOP"}
    )
    _EXPECTED = frozenset({"NONE", "REDUCE", "FORCE_EXIT", "VETO_ENTRY"})

    def test_no_risk_increase_member(self):
        names = frozenset(m.name for m in RegimeDeRiskDirective)
        assert not (names & self._FORBIDDEN_NAMES), (
            "RegimeDeRiskDirective must contain NO risk-increase member (ADR-0014)."
        )

    def test_exact_member_set(self):
        assert frozenset(m.name for m in RegimeDeRiskDirective) == self._EXPECTED

    def test_accumulation_and_neutral_are_inert(self):
        """The bullish/ranging classes map to NONE — provably INERT."""
        assert regime_derisk_directive(RegimeState.ACCUMULATION) is RegimeDeRiskDirective.NONE
        assert regime_derisk_directive(RegimeState.NEUTRAL) is RegimeDeRiskDirective.NONE

    def test_distribution_reduces_and_rug_force_exits(self):
        assert regime_derisk_directive(RegimeState.DISTRIBUTION) is RegimeDeRiskDirective.REDUCE
        assert (
            regime_derisk_directive(RegimeState.RUG_IN_PROGRESS)
            is RegimeDeRiskDirective.FORCE_EXIT
        )

    def test_signal_property_matches_mapping(self):
        for state in RegimeState:
            # argmax-consistent signal: put all mass on the declared state
            sig = RegimeSignal(
                mint="M",
                event_time=_event_time(),
                model_version="v",
                taxonomy_version="1.0.0",
                regime=state,
                p_accumulation=1.0 if state is RegimeState.ACCUMULATION else 0.0,
                p_neutral=1.0 if state is RegimeState.NEUTRAL else 0.0,
                p_distribution=1.0 if state is RegimeState.DISTRIBUTION else 0.0,
                p_rug_in_progress=1.0 if state is RegimeState.RUG_IN_PROGRESS else 0.0,
                uncertainty=0.1,
                is_bootstrap_not_real=True,
            )
            assert sig.derisk_directive is regime_derisk_directive(state)


# ---------------------------------------------------------------------------
# Taxonomy consistency: the contract layer must NOT drift from the model layer
# ---------------------------------------------------------------------------


class TestTaxonomyConsistencyWithModelLayer:
    def test_states_match_model_regime_labels(self):
        from aats.models.regime_labels import RegimeLabel

        assert {s.value for s in RegimeState} == {label.value for label in RegimeLabel}

    def test_directives_match_model_derisk_actions(self):
        from aats.models.regime_labels import RegimeDeRiskAction

        assert {d.value for d in RegimeDeRiskDirective} == {a.value for a in RegimeDeRiskAction}

    def test_state_directive_mapping_matches_model_layer(self):
        from aats.models.regime_labels import REGIME_DERISK_ACTION, RegimeLabel

        for state in RegimeState:
            model_action = REGIME_DERISK_ACTION[RegimeLabel(state.value)]
            assert regime_derisk_directive(state).value == model_action.value
