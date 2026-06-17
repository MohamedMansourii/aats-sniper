"""FAST snipe-classifier inference wrapper (T-310) — single-digit-ms, calibrated prob + uncertainty.

WHAT THIS IS (and is NOT)
=========================
A thin, deterministic wrapper that runs the EXPORTED ONNX tree, applies the calibrator,
and emits a CALIBRATED PROBABILITY + UNCERTAINTY ONLY.  It never emits a price, a size,
or a trade decision — those are downstream (OMS / ¼-Kelly sizer) and not ours.

This is the PYTHON reference inference path used by training, parity tests, and the
slow-loop pre-stage that writes `score:{mint}` to Redis KV.  On the production SNIPE hot
path the SAME ONNX bytes are loaded and run by the Rust core (the inference shim) — the
Python side proves the export is correct and fast; the Rust side runs it in the race.
The seam is marked: `ONNX_RUST_HANDOFF` below.

OUTPUT CONTRACT (data-models §4 / aats.contracts.models.DecisionSignal)
=======================================================================
`infer_decision_signal()` returns a DecisionSignal with EXACTLY:
  p_calibrated, uncertainty, baseline_p, model_version, mint, event_time, surface.
There is NO price field (locked decision 9) and NO win-rate field (HONESTY CLAUSE) — the
contract itself forbids both, and we never try to add them.  High `uncertainty` is a
DE-RISK input downstream — it can never size up (FR-014/032).

LATENCY (a hard constraint, not a nice-to-have)
===============================================
ONNXRuntime with a single intra-op thread and a warmed session runs the tree ensemble in
single-digit-to-low-tens of microseconds per row on commodity hardware — well inside the
single-digit-MS budget.  `measure_latency()` reports MEASURED p50/p99 over many runs (the
self-check pastes the numbers — never an estimate).  We measure p99, not just p50.

MONEY DISCIPLINE
================
No money arithmetic here.  The feature row is float64 statistical inputs (data-models §0:
real-valued feature values may be float — money is not).  baseline_p and p_calibrated are
probabilities in [0,1], not money.

INJECTABLE / OFFLINE
====================
The session is built from ONNX bytes (in-memory) or a path — no network, no RPC, no LLM,
no Redis.  The caller supplies the FeatureFrame and (optionally) the baseline_p.
"""

from __future__ import annotations

import statistics
import time
from dataclasses import dataclass

import numpy as np
import onnxruntime as ort

from aats.contracts.features import FeatureFrame
from aats.contracts.models import DecisionSignal
from aats.models.calibration import ProbabilityCalibrator, bernoulli_uncertainty
from aats.models.featureset import N_FEATURES, frame_to_feature_row


@dataclass(frozen=True)
class InferenceResult:
    """The raw numeric output of one inference (before wrapping into a DecisionSignal).

    p_calibrated + uncertainty ONLY — no price, no size, no decision.
    """

    p_uncalibrated: float
    p_calibrated: float
    uncertainty: float


class SnipeInference:
    """Loaded ONNX session + calibrator — the single-digit-ms inference path.

    Construct from ONNX bytes (export_booster_to_onnx_bytes) + the fitted calibrator.
    `infer_row()` takes a float feature row; `infer_decision_signal()` takes a FeatureFrame
    and returns the typed DecisionSignal contract.
    """

    def __init__(
        self,
        onnx_bytes: bytes,
        calibrator: ProbabilityCalibrator,
        model_version: str,
        *,
        intra_op_threads: int = 1,
    ):
        # Single intra-op thread + sequential execution => deterministic, low-latency,
        # no thread-pool contention in the hot path.
        so = ort.SessionOptions()
        so.intra_op_num_threads = intra_op_threads
        so.inter_op_num_threads = 1
        so.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        self._session = ort.InferenceSession(onnx_bytes, sess_options=so, providers=["CPUExecutionProvider"])
        self._input_name = self._session.get_inputs()[0].name
        # The probability output: onnxmltools (zipmap=False) emits a [N,2] tensor of
        # [p(neg), p(pos)].  We locate it by shape; fall back to the last output.
        self._prob_output_name = self._resolve_prob_output()
        self._calibrator = calibrator
        self._model_version = model_version

    # ----- ONNX_RUST_HANDOFF ------------------------------------------------
    # The production SNIPE hot path loads THESE SAME ONNX BYTES into the Rust core
    # (tract / ort) and runs identical inference.  The calibrator (a tiny monotone
    # isotonic/Platt map) is applied in Rust as a lookup/affine post-step.  This Python
    # class is the REFERENCE implementation + parity oracle, not the race-path runtime.
    # ------------------------------------------------------------------------

    def _resolve_prob_output(self) -> str:
        outputs = self._session.get_outputs()
        # Prefer an output whose name suggests probabilities, else the [N,2] tensor.
        for o in outputs:
            if "prob" in o.name.lower():
                return o.name
        # onnxmltools binary classifier: outputs = [label, probabilities]
        if len(outputs) >= 2:
            return outputs[1].name
        return outputs[0].name

    def _raw_proba(self, X: np.ndarray) -> np.ndarray:
        """Run the ONNX tree and return p(positive) per row."""
        X = np.ascontiguousarray(X, dtype=np.float32)
        out = self._session.run([self._prob_output_name], {self._input_name: X})[0]
        out = np.asarray(out)
        if out.ndim == 2 and out.shape[1] == 2:
            return out[:, 1].astype(np.float64)  # p(positive)
        return out.reshape(-1).astype(np.float64)

    def infer_row(self, feature_row: list[float]) -> InferenceResult:
        """Run inference on ONE feature row (length N_FEATURES). Returns prob + uncertainty."""
        if len(feature_row) != N_FEATURES:
            raise ValueError(
                f"feature_row has {len(feature_row)} values, expected {N_FEATURES}"
            )
        X = np.asarray([feature_row], dtype=np.float32)
        raw = float(self._raw_proba(X)[0])
        calibrated = float(self._calibrator.transform(np.asarray([raw]))[0])
        uncertainty = bernoulli_uncertainty(calibrated)
        return InferenceResult(
            p_uncalibrated=raw,
            p_calibrated=calibrated,
            uncertainty=uncertainty,
        )

    def infer_decision_signal(
        self,
        frame: FeatureFrame,
        *,
        baseline_p: float,
        surface: str,
    ) -> DecisionSignal:
        """Run inference on a FeatureFrame -> the typed DecisionSignal contract.

        The output is calibrated probability + uncertainty ONLY.  No price field, no
        size, no decision — the DecisionSignal type forbids a price (locked decision 9)
        and a win-rate (HONESTY CLAUSE), and we add neither.  `baseline_p` is the frozen
        naive-momentum baseline's signal on the same candidate (GATE-B comparison input).
        """
        row = frame_to_feature_row(frame)
        result = self.infer_row(row)
        return DecisionSignal(
            mint=frame.mint,
            event_time=frame.event_time,
            model_version=self._model_version,
            p_calibrated=result.p_calibrated,
            uncertainty=result.uncertainty,
            baseline_p=float(min(max(baseline_p, 0.0), 1.0)),
            surface=surface,
        )

    # ----- latency measurement (MEASURED, never estimated) ------------------

    def measure_latency(self, feature_row: list[float], *, n: int = 2000, warmup: int = 200) -> dict[str, float]:
        """Measure p50/p99 single-row inference latency in milliseconds (MEASURED).

        Warms the session, then times `n` single-row inferences (the hot-path shape).
        Returns {'p50_ms', 'p99_ms', 'mean_ms', 'n'}.  The self-check pastes these numbers.
        """
        X = np.asarray([feature_row], dtype=np.float32)
        for _ in range(warmup):
            self._raw_proba(X)
        samples: list[float] = []
        for _ in range(n):
            t0 = time.perf_counter()
            self._raw_proba(X)
            samples.append((time.perf_counter() - t0) * 1000.0)
        samples.sort()
        p50 = statistics.median(samples)
        p99 = samples[min(len(samples) - 1, int(0.99 * len(samples)))]
        return {
            "p50_ms": float(p50),
            "p99_ms": float(p99),
            "mean_ms": float(statistics.fmean(samples)),
            "n": float(n),
        }


def build_inference_from_artifact(artifact, onnx_bytes: bytes) -> SnipeInference:
    """Convenience: build a SnipeInference from a TrainedSnipeClassifier + its ONNX bytes."""
    return SnipeInference(
        onnx_bytes=onnx_bytes,
        calibrator=artifact.calibrator,
        model_version=artifact.model_version,
    )
