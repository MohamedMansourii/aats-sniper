"""Tests for the SLOW-loop chart-path / REGIME model retraining scaffold (M2-CP-08).

Self-check criteria (charter §6; ADR-0014; EDGE-VERDICT; BUILD-DIRECTIVE HARD RULES):
  1. DETERMINISM: re-training + re-reporting from the pinned seed reproduces the metrics dict
     bit-for-bit (run twice -> identical numbers on the bootstrap corpus).
  2. NO-LEAK: the leak audit asserts feature event-time <= decision-time on every row; the
     lineage-taint guard rejects a label column in the feature matrix.
  3. SLOW-LOOP ONLY: the regime model REFUSES to run on the FAST/SNIPE loop.
  4. OUTPUT CONTRACT: predict() emits a frozen RegimeSignal — a multiclass STATE + a genuine
     calibrated distribution + uncertainty; NO price / size / win-rate field.
  5. ACCUMULATION-INERT + DE-RISK-ONLY: ACCUMULATION/NEUTRAL -> NONE (provably inert);
     DISTRIBUTION -> REDUCE; RUG_IN_PROGRESS -> FORCE_EXIT; no risk-increase directive exists.
  6. NO WIN-RATE: no win-rate / success-rate / price field anywhere in the metrics or contract.
  7. BOOTSTRAP MARKER + NO CAPITAL LICENSE: artifact + signal stamped is_bootstrap_not_real;
     the card/metrics state no_capital_license.
  8. DEEP DEP LAZILY IMPORTED: the bootstrap path never imports the deep dep; it is declared.

Offline / injectable / deterministic: runs on the synthetic bootstrap corpus in-memory.  No
RPC, no LLM, no Geyser, no Redis, no network, no keypair.  Fixed seed.
"""

from __future__ import annotations

import json
import sys

import numpy as np
import pytest

from aats.contracts.models import (
    RegimeDeRiskDirective,
    RegimeSignal,
    RegimeState,
    regime_derisk_directive,
)
from aats.models.regime_labels import RegimeLabelLeakError
from aats.models.survivor import SurvivorLoopViolation
from aats.models.synthetic import generate_synthetic_corpus
from aats.models.train_regime import (
    _FORBIDDEN_METRIC_NAMES,
    DEEP_MODEL_DEP,
    N_REGIME_CLASSES,
    REGIME_CLASS_ORDER,
    _maybe_import_deep_model,
    build_regime_bootstrap_dataset,
    compute_regime_report,
    frozen_regime_baseline_probs,
    run_training,
    train_regime_model,
)
from aats.models.training import assert_event_time_leq_decision

SEED = 20260706
SMALL_N = 600


@pytest.fixture(scope="module")
def corpus():
    return generate_synthetic_corpus(n=SMALL_N, seed=SEED)


@pytest.fixture(scope="module")
def model(corpus):
    return train_regime_model(corpus, seed=SEED)


# ---------------------------------------------------------------------------
# 1. DETERMINISM — run twice -> identical numbers
# ---------------------------------------------------------------------------


def test_retraining_is_deterministic(corpus):
    m1 = train_regime_model(corpus, seed=SEED)
    r1 = compute_regime_report(m1)
    m2 = train_regime_model(corpus, seed=SEED)
    r2 = compute_regime_report(m2)
    assert json.dumps(r1, sort_keys=True) == json.dumps(r2, sort_keys=True)


def test_run_training_deterministic_without_writing():
    a = run_training(seed=SEED, corpus_n=SMALL_N, write=False)
    b = run_training(seed=SEED, corpus_n=SMALL_N, write=False)
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


# ---------------------------------------------------------------------------
# 2. NO-LEAK — the reused leak audit passes; a future outcome is rejected upstream
# ---------------------------------------------------------------------------


def test_leak_audit_passes_on_bootstrap_join(corpus):
    joined = build_regime_bootstrap_dataset(corpus, seed=SEED)
    summary = assert_event_time_leq_decision(joined)
    assert "LEAK AUDIT PASS" in summary
    # every row: feature event-time == decision, resolution strictly later (label looked forward)
    for ex in joined:
        assert ex.feature_event_time.slot == ex.decision_event_time.slot
        assert ex.resolution_event_time.slot > ex.decision_event_time.slot


def test_model_reports_leak_audit_pass(model):
    assert "LEAK AUDIT PASS" in model.leak_audit_summary


def test_a_future_outcome_sample_is_rejected(corpus):
    # Monkey-inject a sample at/before the decision slot via a hostile trajectory: the
    # M2-CP-02 forward-window gate must raise (the label may never read non-forward data).
    from aats.models.regime_labels import RegimeOutcomeSample, build_regime_outcome

    row = corpus[0]
    d = row.frame.event_time
    with pytest.raises(RegimeLabelLeakError):
        build_regime_outcome(
            mint=row.frame.mint,
            decision_event_time=d,
            resolution_event_time=row.resolution_event_time,
            label_horizon_h_slots=row.label_horizon_h_slots,
            decision_reference_price_ratio=1.0,
            decision_reference_holder_count=10,
            outcome_samples=[
                RegimeOutcomeSample(
                    outcome_slot=d.slot,  # NOT strictly forward -> leak
                    sol_reserve_lamports=1_000_000,
                    token_reserve_base=1_000_000,
                    holder_count=10,
                    spot_price_ratio=1.0,
                )
            ],
            decision_relevant_size_base=1000,
            max_slippage_bps=2000,
        )


# ---------------------------------------------------------------------------
# 3. SLOW-LOOP ONLY
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_loop", ["snipe", "fast", "hot", ""])
def test_predict_refuses_non_slow_loop(model, corpus, bad_loop):
    with pytest.raises(SurvivorLoopViolation):
        model.predict(corpus[0].frame, None, loop=bad_loop)


# ---------------------------------------------------------------------------
# 4. OUTPUT CONTRACT — a valid RegimeSignal, no price/size/win-rate field
# ---------------------------------------------------------------------------


def test_predict_emits_valid_regime_signal(model, corpus):
    sig = model.predict(corpus[0].frame, None)
    assert isinstance(sig, RegimeSignal)
    probs = [sig.p_accumulation, sig.p_neutral, sig.p_distribution, sig.p_rug_in_progress]
    assert abs(sum(probs) - 1.0) <= 1e-6          # a genuine distribution
    assert all(0.0 <= p <= 1.0 for p in probs)
    assert 0.0 <= sig.uncertainty <= 1.0
    assert sig.is_bootstrap_not_real is True
    # regime is the argmax class
    top_idx = int(np.argmax(probs))
    assert sig.regime == {
        REGIME_CLASS_ORDER[i]: state
        for i, state in enumerate(
            [
                RegimeState.ACCUMULATION,
                RegimeState.NEUTRAL,
                RegimeState.DISTRIBUTION,
                RegimeState.RUG_IN_PROGRESS,
            ]
        )
    }[REGIME_CLASS_ORDER[top_idx]]


def test_regime_signal_has_no_price_size_or_winrate_field():
    fields = set(RegimeSignal.model_fields.keys())
    for forbidden in _FORBIDDEN_METRIC_NAMES:
        assert not any(forbidden in f.lower() for f in fields), forbidden


# ---------------------------------------------------------------------------
# 5. ACCUMULATION-INERT + DE-RISK-ONLY (structural)
# ---------------------------------------------------------------------------


def test_accumulation_and_neutral_are_inert():
    assert regime_derisk_directive(RegimeState.ACCUMULATION) is RegimeDeRiskDirective.NONE
    assert regime_derisk_directive(RegimeState.NEUTRAL) is RegimeDeRiskDirective.NONE


def test_derisk_states_only_reduce_or_exit():
    assert regime_derisk_directive(RegimeState.DISTRIBUTION) is RegimeDeRiskDirective.REDUCE
    assert (
        regime_derisk_directive(RegimeState.RUG_IN_PROGRESS)
        is RegimeDeRiskDirective.FORCE_EXIT
    )


def test_no_risk_increase_directive_is_expressible():
    names = {m.name for m in RegimeDeRiskDirective}
    forbidden = {"SIZE_UP", "WIDEN_STOP", "RELAX_STOP", "DELAY_EXIT", "ADD_LEVERAGE", "OVERRIDE_HARD_STOP"}
    assert names & forbidden == set()


def test_predicted_signal_directive_is_derisk_or_inert(model, corpus):
    # Whatever the model predicts, the directive is de-risk-or-inert (never risk-up).
    legal = {
        RegimeDeRiskDirective.NONE,
        RegimeDeRiskDirective.REDUCE,
        RegimeDeRiskDirective.FORCE_EXIT,
        RegimeDeRiskDirective.VETO_ENTRY,
    }
    for row in corpus[:50]:
        sig = model.predict(row.frame, None)
        assert sig.derisk_directive in legal


# ---------------------------------------------------------------------------
# 6. NO WIN-RATE anywhere in the metrics
# ---------------------------------------------------------------------------


def test_metrics_carry_no_win_rate_or_price_field(model):
    report = compute_regime_report(model)
    blob = json.dumps(report).lower()
    for forbidden in ("win_rate", "winrate", "success_rate", "realized_mult"):
        assert forbidden not in blob


# ---------------------------------------------------------------------------
# 7. BOOTSTRAP MARKER + NO CAPITAL LICENSE
# ---------------------------------------------------------------------------


def test_bootstrap_and_no_capital_license(model):
    assert model.is_bootstrap_not_real is True
    report = compute_regime_report(model)
    assert report["is_bootstrap_not_real"] is True
    assert report["no_capital_license"] is True


def test_all_four_classes_present_in_bootstrap(model):
    # The bootstrap corpus must exercise all four regime STATES (non-degenerate labels).
    assert set(model.class_counts.keys()) == {s.value for s in REGIME_CLASS_ORDER}
    assert all(v > 0 for v in model.class_counts.values())


# ---------------------------------------------------------------------------
# 8. DEEP DEP declared + lazily imported (never on the bootstrap path)
# ---------------------------------------------------------------------------


def test_deep_dep_declared_and_not_imported_on_bootstrap():
    assert DEEP_MODEL_DEP == "pytorch-forecasting"
    # The bootstrap path returns None and never imports the heavy dep.
    assert _maybe_import_deep_model(False) is None
    assert "pytorch_forecasting" not in sys.modules


def test_frozen_baseline_is_a_distribution(model):
    base = frozen_regime_baseline_probs(model.test_x)
    assert base.shape[1] == N_REGIME_CLASSES
    row_sums = base.sum(axis=1)
    assert np.allclose(row_sums, 1.0, atol=1e-9)
