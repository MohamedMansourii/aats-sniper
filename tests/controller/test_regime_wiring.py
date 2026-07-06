"""Tests: SlowLoopRegimeWiring — SLOW-loop regime/survivor de-risk wiring (M2-CP-04).

Proves the non-waivable laws of wiring the chart-path/regime model and the
previously-orphaned survivor model into the control plane:

  - the wiring maps a regime STATE / survival probability ONLY to de-risk flags
    (FORCE_EXIT -> narrative_failure; REDUCE/VETO -> veto) — it can structurally
    NOT size up (no EntryIntent, no size method, no risk-increase branch);
  - the bullish/ranging regimes (ACCUMULATION / NEUTRAL) and a healthy survival
    read are INERT — they set NO flag (provably cannot delay an exit, relax a
    stop, or size up);
  - the SNIPE / FAST loops read a PRE-COMPUTED scalar flag (get_veto_flag /
    get_narrative_failure_flag), never the model;
  - the wiring is SLOW-loop only (assert_slow_loop_only) — a fast/snipe loop raises;
  - point-in-time: decisions use event-time slot only, never wall-clock.
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest

from aats.contracts.events import EventTime
from aats.contracts.models import (
    RegimeDeRiskDirective,
    RegimeSignal,
    RegimeState,
)
from aats.controller import regime_wiring as regime_wiring_module
from aats.controller.regime_wiring import SlowLoopRegimeWiring
from aats.controller.state import InMemoryStateStore
from aats.models.survivor import SurvivorLoopViolation


def _event_time(slot: int = 1000) -> EventTime:
    return EventTime(slot=slot, block_time_ms=1_700_000_000_000, wall_clock_ms=1_700_000_000_100)


def _regime_signal(regime: RegimeState, mint: str = "MINT_R") -> RegimeSignal:
    """A confident signal: all mass on `regime` (argmax-consistent)."""
    return RegimeSignal(
        mint=mint,
        event_time=_event_time(),
        model_version="regime-v1-BOOTSTRAP",
        taxonomy_version="1.0.0",
        regime=regime,
        p_accumulation=1.0 if regime is RegimeState.ACCUMULATION else 0.0,
        p_neutral=1.0 if regime is RegimeState.NEUTRAL else 0.0,
        p_distribution=1.0 if regime is RegimeState.DISTRIBUTION else 0.0,
        p_rug_in_progress=1.0 if regime is RegimeState.RUG_IN_PROGRESS else 0.0,
        uncertainty=0.1,
        is_bootstrap_not_real=True,
    )


def _survivor_pred(p_calibrated: float, uncertainty: float, mint: str = "MINT_S"):
    """A duck-typed survivor prediction (matches SurvivorPrediction's read surface)."""
    return SimpleNamespace(
        mint=mint,
        event_time=_event_time(),
        p_calibrated=p_calibrated,
        uncertainty=uncertainty,
    )


@pytest.fixture
def store() -> InMemoryStateStore:
    return InMemoryStateStore(clock=lambda: 0)


# ---------------------------------------------------------------------------
# Regime -> existing de-risk flags
# ---------------------------------------------------------------------------


class TestRegimeSignalWiring:
    def test_rug_in_progress_sets_narrative_failure_flag(self, store):
        wiring = SlowLoopRegimeWiring(store)
        directive = wiring.apply_regime_signal(_regime_signal(RegimeState.RUG_IN_PROGRESS, "RUG"))
        assert directive is RegimeDeRiskDirective.FORCE_EXIT
        # FAST reads a pre-computed scalar — the narrative_failure flag it already reads.
        assert store.get_narrative_failure_flag("RUG") is True
        assert store.get_veto_flag("RUG") is False

    def test_distribution_sets_veto_flag(self, store):
        wiring = SlowLoopRegimeWiring(store)
        directive = wiring.apply_regime_signal(_regime_signal(RegimeState.DISTRIBUTION, "DIST"))
        assert directive is RegimeDeRiskDirective.REDUCE
        assert store.get_veto_flag("DIST") is True
        assert store.get_narrative_failure_flag("DIST") is False

    def test_accumulation_is_inert(self, store):
        """ACCUMULATION (bullish) sets NO flag — provably cannot size up or delay an exit."""
        wiring = SlowLoopRegimeWiring(store)
        directive = wiring.apply_regime_signal(_regime_signal(RegimeState.ACCUMULATION, "ACC"))
        assert directive is RegimeDeRiskDirective.NONE
        assert store.get_veto_flag("ACC") is False
        assert store.get_narrative_failure_flag("ACC") is False

    def test_neutral_is_inert(self, store):
        wiring = SlowLoopRegimeWiring(store)
        directive = wiring.apply_regime_signal(_regime_signal(RegimeState.NEUTRAL, "NEU"))
        assert directive is RegimeDeRiskDirective.NONE
        assert store.get_veto_flag("NEU") is False
        assert store.get_narrative_failure_flag("NEU") is False

    def test_regime_signal_slow_loop_only(self, store):
        wiring = SlowLoopRegimeWiring(store)
        for bad_loop in ("fast", "snipe"):
            with pytest.raises(SurvivorLoopViolation):
                wiring.apply_regime_signal(
                    _regime_signal(RegimeState.RUG_IN_PROGRESS), loop=bad_loop
                )
        # And it did NOT set any flag on the refused call.
        assert store.get_narrative_failure_flag("MINT_R") is False


# ---------------------------------------------------------------------------
# Survivor -> existing de-risk flags (finally wiring the orphaned model)
# ---------------------------------------------------------------------------


class TestSurvivorWiring:
    def test_low_survival_forces_exit(self, store):
        wiring = SlowLoopRegimeWiring(store)
        directive = wiring.apply_survivor_prediction(
            _survivor_pred(p_calibrated=0.05, uncertainty=0.5, mint="LOW")
        )
        assert directive is RegimeDeRiskDirective.FORCE_EXIT
        assert store.get_narrative_failure_flag("LOW") is True

    def test_weak_survival_reduces(self, store):
        wiring = SlowLoopRegimeWiring(store)
        directive = wiring.apply_survivor_prediction(
            _survivor_pred(p_calibrated=0.30, uncertainty=0.3, mint="WEAK")
        )
        assert directive is RegimeDeRiskDirective.REDUCE
        assert store.get_veto_flag("WEAK") is True

    def test_high_uncertainty_reduces_even_when_survival_high(self, store):
        wiring = SlowLoopRegimeWiring(store)
        directive = wiring.apply_survivor_prediction(
            _survivor_pred(p_calibrated=0.90, uncertainty=0.95, mint="UNC")
        )
        assert directive is RegimeDeRiskDirective.REDUCE
        assert store.get_veto_flag("UNC") is True

    def test_healthy_survival_is_inert(self, store):
        """High P(survive) + low uncertainty sets NO flag — cannot size up."""
        wiring = SlowLoopRegimeWiring(store)
        directive = wiring.apply_survivor_prediction(
            _survivor_pred(p_calibrated=0.85, uncertainty=0.10, mint="OK")
        )
        assert directive is RegimeDeRiskDirective.NONE
        assert store.get_veto_flag("OK") is False
        assert store.get_narrative_failure_flag("OK") is False

    def test_survivor_directive_monotone_non_increasing_in_survival(self, store):
        """Higher P(survive) can only move toward NONE — never toward more control."""
        wiring = SlowLoopRegimeWiring(store)
        severity = {
            RegimeDeRiskDirective.NONE: 0,
            RegimeDeRiskDirective.REDUCE: 1,
            RegimeDeRiskDirective.FORCE_EXIT: 2,
        }
        prev = None
        for p in [0.02, 0.10, 0.20, 0.34, 0.50, 0.80, 0.99]:
            d = wiring.survivor_directive(_survivor_pred(p_calibrated=p, uncertainty=0.1))
            sev = severity[d]
            if prev is not None:
                assert sev <= prev, f"de-risk severity rose as survival rose (p={p})"
            prev = sev

    def test_survivor_prediction_slow_loop_only(self, store):
        wiring = SlowLoopRegimeWiring(store)
        with pytest.raises(SurvivorLoopViolation):
            wiring.apply_survivor_prediction(
                _survivor_pred(p_calibrated=0.05, uncertainty=0.5), loop="fast"
            )

    def test_wiring_matches_real_survivor_prediction_shape(self, store):
        """The duck-typed contract matches the REAL SurvivorPrediction object."""
        from aats.models.survivor import SurvivorPrediction

        pred = SurvivorPrediction(
            mint="REAL",
            event_time=_event_time(),
            model_version="survivor-v1-BOOTSTRAP",
            p_calibrated=0.05,
            uncertainty=0.4,
            p10=0.0,
            p50=0.05,
            p90=0.2,
            vol_estimate=0.3,
            is_bootstrap_not_real=True,
        )
        wiring = SlowLoopRegimeWiring(store)
        assert wiring.survivor_directive(pred) is RegimeDeRiskDirective.FORCE_EXIT


# ---------------------------------------------------------------------------
# run_survivor_derisk over open positions (SLOW cadence)
# ---------------------------------------------------------------------------


class _RecordingFlagSink:
    """A structural StateStore surface that records de-risk calls + serves positions."""

    def __init__(self, mints: list[str]) -> None:
        self._positions = [SimpleNamespace(mint=m) for m in mints]
        self.veto: list[str] = []
        self.narrative: list[str] = []

    def set_veto_flag(self, mint: str, ttl_seconds: int = 60) -> None:
        self.veto.append(mint)

    def set_narrative_failure_flag(self, mint: str, ttl_seconds: int = 120) -> None:
        self.narrative.append(mint)

    def load_all_positions(self) -> list:
        return list(self._positions)


class _FakeSurvivorModel:
    def __init__(self, p: float, u: float) -> None:
        self._p = p
        self._u = u

    def predict(self, frame, mcs, *, loop: str = "slow"):
        return _survivor_pred(p_calibrated=self._p, uncertainty=self._u, mint=frame["mint"])


class _FakeFeatureSource:
    def __init__(self, covered: set[str]) -> None:
        self._covered = covered

    def get_survivor_inputs(self, mint: str):
        if mint not in self._covered:
            return None
        return ({"mint": mint}, None)


class TestRunSurvivorDerisk:
    def test_noop_when_unwired(self, store):
        wiring = SlowLoopRegimeWiring(store)  # no model / feature source
        assert wiring.run_survivor_derisk(event_slot=1234) == []

    def test_applies_over_open_positions_and_skips_absent(self):
        sink = _RecordingFlagSink(["A", "B", "C"])
        wiring = SlowLoopRegimeWiring(
            sink,
            survivor_model=_FakeSurvivorModel(p=0.05, u=0.4),  # -> FORCE_EXIT
            survivor_feature_source=_FakeFeatureSource(covered={"A", "C"}),  # B absent
        )
        directives = wiring.run_survivor_derisk(event_slot=999)
        # A + C acted (FORCE_EXIT -> narrative), B skipped/counted.
        assert directives == [RegimeDeRiskDirective.FORCE_EXIT, RegimeDeRiskDirective.FORCE_EXIT]
        assert set(sink.narrative) == {"A", "C"}
        assert sink.veto == []
        assert wiring.stats.survivor_features_absent_skipped == 1

    def test_run_survivor_derisk_slow_loop_only(self, store):
        wiring = SlowLoopRegimeWiring(
            store,
            survivor_model=_FakeSurvivorModel(p=0.05, u=0.4),
            survivor_feature_source=_FakeFeatureSource(covered=set()),
        )
        with pytest.raises(SurvivorLoopViolation):
            wiring.run_survivor_derisk(event_slot=1, loop="snipe")


# ---------------------------------------------------------------------------
# Structural de-risk proof + point-in-time
# ---------------------------------------------------------------------------


class TestStructuralDeRiskProof:
    def test_wiring_has_no_entry_or_size_method(self):
        wiring = SlowLoopRegimeWiring(InMemoryStateStore(clock=lambda: 0))
        for forbidden in ("entry", "size_up", "increase", "widen_stop", "add_leverage"):
            assert not hasattr(wiring, forbidden), (
                f"SlowLoopRegimeWiring must have no {forbidden!r} method — it can only de-risk."
            )

    def test_module_never_imports_entry_intent(self):
        src = inspect.getsource(regime_wiring_module)
        assert "EntryIntent" not in src, (
            "regime_wiring.py must never import/construct EntryIntent — de-risk only."
        )

    def test_module_uses_no_wall_clock_for_decisions(self):
        """Decisions use event-time slot only; no wall-clock (point-in-time)."""
        src = inspect.getsource(regime_wiring_module)
        for banned in ("import time", "datetime", "time.time(", "time.monotonic"):
            assert banned not in src, (
                f"regime_wiring.py must not use {banned!r} — decisions are event-time only."
            )

    def test_only_de_risk_flag_setters_are_called(self):
        """Every acting directive routes through a de-risk StateStore setter — nothing else."""
        sink = _RecordingFlagSink([])
        wiring = SlowLoopRegimeWiring(sink)
        # Exercise every acting directive; the recording sink only exposes de-risk setters.
        wiring.apply_regime_signal(_regime_signal(RegimeState.RUG_IN_PROGRESS, "X"))
        wiring.apply_regime_signal(_regime_signal(RegimeState.DISTRIBUTION, "Y"))
        wiring.apply_regime_signal(_regime_signal(RegimeState.ACCUMULATION, "Z"))
        assert sink.narrative == ["X"]
        assert sink.veto == ["Y"]  # ACCUMULATION set nothing (inert)


# ---------------------------------------------------------------------------
# Control-plane wiring via ControllerOrchestrator (M2-CP-04)
# ---------------------------------------------------------------------------


class TestOrchestratorRegimeWiring:
    def test_passthrough_noop_when_unwired(self, orchestrator):
        """The default orchestrator injects no regime wiring — every hook is a safe no-op."""
        orchestrator.startup(block_time_ms=1)
        assert orchestrator.regime_wiring is None
        assert orchestrator.apply_regime_signal(_regime_signal(RegimeState.RUG_IN_PROGRESS)) is None
        assert (
            orchestrator.apply_survivor_prediction(_survivor_pred(p_calibrated=0.05, uncertainty=0.5))
            is None
        )
        assert orchestrator.run_slow_regime_tick(event_slot=1) == []

    def test_wired_orchestrator_applies_de_risk_flag(
        self, store, venue, snipe_loop, fast_loop, slow_loop, reconciler, kill_switch
    ):
        """A wired orchestrator routes a RUG regime signal to the narrative_failure flag."""
        from aats.controller.loops import ControllerOrchestrator

        wiring = SlowLoopRegimeWiring(store)
        orch = ControllerOrchestrator(
            store=store,
            venue=venue,
            snipe_loop=snipe_loop,
            fast_loop=fast_loop,
            slow_loop=slow_loop,
            reconciler=reconciler,
            kill_switch=kill_switch,
            regime_wiring=wiring,
        )
        orch.startup(block_time_ms=1)
        directive = orch.apply_regime_signal(_regime_signal(RegimeState.RUG_IN_PROGRESS, "WIRED"))
        assert directive is RegimeDeRiskDirective.FORCE_EXIT
        assert store.get_narrative_failure_flag("WIRED") is True
