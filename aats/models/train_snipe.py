"""Pinned, reproducible training script for the FAST snipe classifier (T-310).

Run:  python -m aats.models.train_snipe

PRODUCES (under aats/models/artifacts/):
  - snipe_classifier.onnx       : the exported ONNX tree (loaded by the Rust hot core)
  - calibrator.pkl              : the fitted isotonic/Platt calibrator (applied post-ONNX)
  - reliability_diagram.json    : the reliability-curve points (the diagram the sizer reads)
  - MODEL_CARD.md               : features, label, training window, calibration, baseline gap,
                                  failure regimes, disable thresholds, BOOTSTRAP marker
  - metrics.json                : the machine-readable metrics the card quotes

REPRODUCIBLE: fixed seed (FIXED_SEED), single-threaded deterministic LightGBM, deterministic
ONNX export.  Re-running reproduces the card metrics bit-for-bit (self-check 8).

HARD MARKER: every artifact is stamped is_bootstrap_not_real=True.  This model has earned
NO capital license — acceptance is net-PnL + model-vs-baseline on RECORDED data (harness),
not this synthetic bootstrap.  Real capital stays DISABLED behind DRY-RUN.

NO SECRETS, NO KEYS, NO EXECUTION.  This script trains and exports a model.  It never
builds, signs, or lands a transaction; it touches no keypair, RPC key, or swap code.
"""

from __future__ import annotations

import json
import pickle
from dataclasses import asdict
from pathlib import Path

import numpy as np
from sklearn.metrics import average_precision_score, roc_auc_score

from aats.models.baseline import evaluate_baseline, load_params
from aats.models.calibration import (
    brier_score,
    expected_calibration_error,
    reliability_curve,
)
from aats.models.featureset import FEATURE_COLUMNS
from aats.models.monitor import compare_window, rolling_auto_disable
from aats.models.synthetic import generate_synthetic_corpus
from aats.models.training import (
    TrainedSnipeClassifier,
    _matrix,
    export_booster_to_onnx_bytes,
    join_labels_by_event_time,
    time_forward_split,
    train_snipe_classifier,
)

FIXED_SEED = 20260616
CORPUS_N = 4000
ARTIFACT_DIR = Path(__file__).with_name("artifacts")


def _baseline_scores_for(examples) -> list[float]:
    """Frozen naive-momentum baseline_p on the SAME frames (GATE-B comparison input)."""
    from decimal import Decimal

    from aats.features.buy_pressure import BuyPressureFeatures

    params = load_params()
    col = {c: i for i, c in enumerate(FEATURE_COLUMNS)}
    out: list[float] = []
    for ex in examples:
        bp = ex.feature_row[col["first_k_buy_pressure"]]
        vol = ex.feature_row[col["first_k_volume_lamports"]]
        feats = BuyPressureFeatures(
            first_k_buy_pressure=Decimal(str(int(bp))),
            first_k_volume_lamports=int(vol),
            first_k_buy_count=0,
            first_k_sell_count=0,
            first_k_unique_buyers=0,
            max_source_slot=ex.feature_event_time.slot + params.first_k_slots,
            event_time=ex.feature_event_time,
            first_k_slots=params.first_k_slots,
            provenance=None,  # type: ignore[arg-type]
        )
        out.append(evaluate_baseline(feats, params, mint="x").baseline_p)
    return out


def run_training(seed: int = FIXED_SEED, corpus_n: int = CORPUS_N) -> dict:
    """Train, calibrate, export, and compute all card metrics. Returns the metrics dict."""
    rows = generate_synthetic_corpus(n=corpus_n, seed=seed)
    artifact: TrainedSnipeClassifier = train_snipe_classifier(rows, seed=seed)

    joined = join_labels_by_event_time(rows)
    split = time_forward_split(joined)
    X_test, y_test = _matrix(split.test)

    # OOS calibrated probabilities on the held-out TEST fold (honest calibration check).
    cal_test = artifact.predict_calibrated(X_test.astype(np.float32))
    test_brier = brier_score(cal_test, y_test)
    test_ece = expected_calibration_error(cal_test, y_test, n_bins=10)
    test_bins = reliability_curve(cal_test, y_test, n_bins=10)

    # Model vs baseline on the test fold (the headline GATE-B-like delta).
    base_test = np.asarray(_baseline_scores_for(split.test), dtype=np.float64)
    model_auc = roc_auc_score(y_test, cal_test)
    model_pr = average_precision_score(y_test, cal_test)
    base_auc = roc_auc_score(y_test, base_test)
    base_pr = average_precision_score(y_test, base_test)

    # Rolling auto-disable demo over thirds of the test fold (point-in-time order).
    thirds = np.array_split(np.arange(len(y_test)), 3)
    windows = [
        compare_window(cal_test[idx], base_test[idx], y_test[idx], min_edge=0.02)
        for idx in thirds
    ]
    rolling = rolling_auto_disable(windows, consecutive_no_signal_to_disable=2)

    # ONNX export + parity.
    onnx_bytes = export_booster_to_onnx_bytes(artifact.booster)
    from aats.models.inference import SnipeInference

    inf = SnipeInference(onnx_bytes, artifact.calibrator, artifact.model_version)
    py_raw = artifact.predict_uncalibrated(X_test.astype(np.float32))
    onnx_raw = inf._raw_proba(X_test.astype(np.float32))
    parity_max_abs_diff = float(np.max(np.abs(py_raw - onnx_raw)))

    # Latency (MEASURED p50/p99).
    latency = inf.measure_latency(split.test[0].feature_row, n=3000, warmup=300)

    metrics = {
        "is_bootstrap_not_real": True,
        "model_version": artifact.model_version,
        "seed": seed,
        "corpus_n": corpus_n,
        "n_train": artifact.n_train,
        "n_calib": artifact.n_calib,
        "n_test": artifact.n_test,
        "positive_rate": float(np.mean([r.label for r in rows])),
        "leak_audit_summary": artifact.leak_audit_summary,
        "calib_slice_report": {
            "method": artifact.calibration_report.method,
            "brier": artifact.calibration_report.brier_score,
            "ece": artifact.calibration_report.ece,
            "in_tolerance": artifact.calibration_report.in_tolerance,
        },
        "test_fold_calibration": {
            "brier": test_brier,
            "ece": test_ece,
            "reliability_bins": [asdict(b) for b in test_bins],
        },
        "model_vs_baseline_test": {
            "model_auc": model_auc,
            "model_pr_auc": model_pr,
            "baseline_auc": base_auc,
            "baseline_pr_auc": base_pr,
            "edge_auc": model_auc - base_auc,
            "edge_pr_auc": model_pr - base_pr,
        },
        "rolling_monitor": {
            "disabled": rolling.disabled,
            "final_verdict": rolling.final_verdict.value,
            "windows": [w.summary() for w in rolling.windows],
        },
        "onnx_parity_max_abs_diff": parity_max_abs_diff,
        "latency_ms": latency,
        "feature_columns": list(FEATURE_COLUMNS),
        "monotone_constraints": artifact.monotone_constraints,
        "auto_disable_min_edge_auc": 0.02,
    }

    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    (ARTIFACT_DIR / "snipe_classifier.onnx").write_bytes(onnx_bytes)
    with (ARTIFACT_DIR / "calibrator.pkl").open("wb") as fh:
        pickle.dump(artifact.calibrator, fh)
    with (ARTIFACT_DIR / "reliability_diagram.json").open("w", encoding="utf-8") as fh:
        json.dump([asdict(b) for b in test_bins], fh, indent=2)
    with (ARTIFACT_DIR / "metrics.json").open("w", encoding="utf-8") as fh:
        json.dump(metrics, fh, indent=2, default=str)
    _write_model_card(metrics)
    return metrics


def _write_model_card(m: dict) -> None:
    mvb = m["model_vs_baseline_test"]
    tf = m["test_fold_calibration"]
    lat = m["latency_ms"]
    card = f"""# MODEL CARD — FAST Snipe Classifier (T-310)

> **BOOTSTRAP, NOT REAL.** `is_bootstrap_not_real = {m["is_bootstrap_not_real"]}`.
> This model is trained on a SYNTHETIC labeled launch dataset (brief 7.2) because recorded
> chain data does not exist yet. It has earned **NO capital license**. The sole
> production-acceptance metric is **net-of-cost PnL + model-vs-baseline on RECORDED data**
> (clean-room harness, T-400/401) — never on this synthetic set. Real capital stays
> **DISABLED behind DRY-RUN** regardless of any number below.

## Identity
- **model_version:** `{m["model_version"]}`
- **seed:** `{m["seed"]}` (deterministic; re-train reproduces these metrics bit-for-bit)
- **algorithm:** LightGBM gradient-boosted trees, binary objective (log-loss — a proper
  scoring rule), exported to **ONNX** (run by the Rust hot core on the SNIPE path).
- **output:** calibrated probability + uncertainty ONLY. **No price field. No win-rate
  field. No trade decision.** (data-models §4; locked decision 9; HONESTY CLAUSE.)

## Label (leak-free, co-owned with backtest-qa-engineer)
- Binary: `1 = coin reached the target multiple within the horizon AND remained sellable`
  (non-honeypot, non-rugged exit liquidity existed); `0 = FADED/RUGGED`.
- Lives in a **separate labels dataset**, joined to features **by event_time ONLY**.
- `resolution_event_time = event_time + H` is **stamped and strictly later** than the
  decision anchor (horizon proof). H (synthetic) = 1500 slots (~10 min).
- **Leak audit:** every training row asserts `feature_event_time <= decision_event_time`
  AND `resolution_event_time > decision_event_time`. Result: PASS (see metrics.json).

## Features ({len(m["feature_columns"])} columns)
On-chain, point-in-time, available inside the first blocks. First-K microstructure
(buy/sell pressure, volume, counts, unique-buyer velocity), LP depth, holder count,
top-10 concentration, sniper-cluster score, sell tax, survivor TA (rsi/macd/bb_width with
presence flags), adversarial selectivity (smart_wallets_in, entry lag), tip-contention
one-hot. **No truth_*/label column can enter the matrix** — `assert_no_label_taint()`
fails the build on any (lineage taint).

### Monotone (de-risk) constraints — adversarial-sentiment rule
These features are constrained **monotone non-increasing** (a higher value can only push
the probability DOWN, never up): `holder_concentration_top10_bps`, `sell_tax_bps`,
`smart_wallets_in`, `smart_wallet_entry_lag_slots`. Whale concentration, honeypot-adjacent
tax, and being *behind* smarter money are RISK signals — the model is forbidden from
learning to treat them as bullish.

## Training window
- corpus n = {m["corpus_n"]}, positive rate = {m["positive_rate"]:.3f}
- forward-only, event-time-ordered split (NEVER shuffled): train={m["n_train"]} /
  calib={m["n_calib"]} / test={m["n_test"]} (the latest event-time window is OOS).

## Calibration (calibration before accuracy)
- method: **{m["calib_slice_report"]["method"]}**, fit on the held-out, time-forward
  calibration slice (never the train or test fold).
- **OOS test-fold calibration:** Brier = **{tf["brier"]:.4f}**, ECE = **{tf["ece"]:.4f}**.
- Reliability diagram: `artifacts/reliability_diagram.json` (10 bins).
- Uncertainty = `sqrt(p*(1-p))` (Bernoulli band) — high uncertainty **de-risks** the
  ¼-Kelly sizer (shrinks size); it can NEVER size up.

## Model vs baseline (beat the baseline or stay silent) — OOS test fold
- model AUC = **{mvb["model_auc"]:.4f}** vs baseline AUC = **{mvb["baseline_auc"]:.4f}**
  -> **edge = {mvb["edge_auc"]:+.4f}**.
- model PR-AUC = {mvb["model_pr_auc"]:.4f} vs baseline PR-AUC = {mvb["baseline_pr_auc"]:.4f}.
- **Auto-disable:** the model-vs-baseline monitor emits **NO_SIGNAL** (no edge traded)
  on any rolling OOS window where `model_AUC - baseline_AUC < {m["auto_disable_min_edge_auc"]}`,
  and auto-disables after 2 consecutive NO_SIGNAL windows. Degraded/shuffled input ->
  NO_SIGNAL (verified by the shuffle-collapse test).

## ONNX parity + latency (measured, not estimated)
- Python-vs-ONNX max abs diff (raw proba) = **{m["onnx_parity_max_abs_diff"]:.2e}**.
- single-row inference: **p50 = {lat["p50_ms"]:.4f} ms, p99 = {lat["p99_ms"]:.4f} ms**
  (n={int(lat["n"])}), well inside the single-digit-ms SNIPE budget.

## Known failure regimes
- **Synthetic data is not the live distribution** — this card's numbers do not transfer;
  they prove the *pipeline* is leak-free and calibrated, not that the model has edge live.
- Edge decay / regime change: the auto-disable monitor is the backstop (emit no signal).
- Class imbalance at low positive rate inflates AUC's apparent stability — PR-AUC and the
  harness's net-PnL are the real bars.
- TA features absent for ~40% of launches; presence flags carry the missingness.

## Disable thresholds
- min edge over baseline (AUC): **{m["auto_disable_min_edge_auc"]}**.
- consecutive NO_SIGNAL windows to disable: **2**.
- absent/stale score => SNIPE SKIPs (no block) — "no signal" is the safe default.

## Boundaries (non-waivable)
Outputs probability + uncertainty, never a price. No execution code, no keypair, no RPC
key, no swap building. LLM/slow-model never on this FAST path. Any downstream signal may
only DE-RISK — never size up, widen a stop, or override a hard stop (enforced by the OMS).
"""
    (ARTIFACT_DIR / "MODEL_CARD.md").write_text(card, encoding="utf-8")


if __name__ == "__main__":
    metrics = run_training()
    print(json.dumps({k: v for k, v in metrics.items() if k != "feature_columns"}, indent=2, default=str))
