"""aats.reasoning — M2 schema-enforced DE-RISK-ONLY LLM Reasoner (T-313).

Implemented by: llm-reasoning-engineer (T-313).  Deps: T-310 (quant prob), T-306 (MCS).

WHAT THIS PACKAGE DOES
======================
Judges AGREEMENT / CONFLICT between the calibrated quant probability (DecisionSignal,
T-310) and the Market Conviction Score (MCSScore, T-306) and emits a structured,
schema-enforced, DE-RISK-ONLY verdict (ReasoningVerdict) over the bus, plus an optional
de-risk Intent (veto / reduce / force-exit) — NEVER an entry.

CRITICAL constraints (enforced by the type system + CI, not by comment):
  - The LLM is SLOW-loop ONLY; it NEVER touches the FAST or SNIPE critical path.
  - The ReasoningVerdict.action enum has NO SIZE_UP / WIDEN_STOP / ADD_LEVERAGE /
    OVERRIDE_HARD_STOP member — a risk-increase is INEXPRESSIBLE by construction
    (asymmetric trust, ADR-0006).
  - The reasoning path holds ONLY a DeRiskIntentFactory — it has no EntryIntent
    constructor, so it cannot size up, widen a stop, or add leverage.
  - The asymmetric-trust CLAMP is a pure, property-tested, monotone-toward-safety
    function: it can only ever NARROW risk relative to the quant decision.
  - Ingested narrative is QUOTED UNTRUSTED DATA — judged, never obeyed (injection-safe).
  - The output is SCHEMA-VALIDATED (pydantic); a malformed object fails safe to the most
    conservative action (veto/hold).  No regex on model output.
  - Point-in-time: the prompt mixes only data at the SAME event_time; lookahead is refused.
  - Sub-200ms LOCAL veto fast path; router timeout/error returns the conservative action.
  - LLM backends are INJECTABLE; the default is a deterministic OFFLINE mock (no network).

Public surface:
  DeRiskReasoner, ReasonerOutput        — the Reasoner and its output
  LLMRouter, RouteTier, SignalBucketKey — the LLM router + signal-bucket cache key
  ReasonerBackend, MockReasonerBackend  — the injectable backend protocol + offline mock
  conservative_fallback_verdict, is_dry_run
  build_ollama_backend, build_frontier_backend  — live plug-in points (offline = mock)
  LLMRawVerdict, DecisionSignalLabel    — the schema-enforced raw output
  QuantBucket, SentimentRisk, bucket_quant, bucket_sentiment
  clamp_to_derisk_action, ClampResult, classify_raw_is_risk_increase, derisk_strength
  evaluate_matrix, MatrixDecision, combine_clamp_and_matrix
  build_reasoner_prompt, PromptInputs, SYSTEM_PROMPT, assert_point_in_time
  run_calibration_check, CalibrationReport, brier_score, reliability_curve,
    expected_calibration_error
"""

from aats.reasoning.calibration import (
    CalibrationBin,
    CalibrationReport,
    brier_score,
    expected_calibration_error,
    reliability_curve,
    run_calibration_check,
)
from aats.reasoning.clamp import (
    ClampResult,
    clamp_to_derisk_action,
    classify_raw_is_risk_increase,
    derisk_strength,
    quant_to_action_ceiling,
)
from aats.reasoning.matrix import (
    MatrixDecision,
    combine_clamp_and_matrix,
    evaluate_matrix,
)
from aats.reasoning.prompt import (
    SYSTEM_PROMPT,
    PromptInputs,
    assert_point_in_time,
    build_reasoner_prompt,
)
from aats.reasoning.reasoner import (
    DeRiskReasoner,
    ReasonerOutput,
)
from aats.reasoning.router import (
    DEFAULT_FAST_PATH_BUDGET_MS,
    LLMRouter,
    MockReasonerBackend,
    ReasonerBackend,
    RoutedVerdict,
    RouteTier,
    SignalBucketKey,
    build_frontier_backend,
    build_ollama_backend,
    conservative_fallback_verdict,
    is_dry_run,
)
from aats.reasoning.schema import (
    DecisionSignalLabel,
    LLMRawVerdict,
    QuantBucket,
    SentimentRisk,
    bucket_quant,
    bucket_sentiment,
    signal_risk_rank,
)

__all__ = [
    # schema
    "DecisionSignalLabel",
    "LLMRawVerdict",
    "QuantBucket",
    "SentimentRisk",
    "bucket_quant",
    "bucket_sentiment",
    "signal_risk_rank",
    # clamp
    "ClampResult",
    "clamp_to_derisk_action",
    "classify_raw_is_risk_increase",
    "derisk_strength",
    "quant_to_action_ceiling",
    # matrix
    "MatrixDecision",
    "combine_clamp_and_matrix",
    "evaluate_matrix",
    # prompt
    "SYSTEM_PROMPT",
    "PromptInputs",
    "assert_point_in_time",
    "build_reasoner_prompt",
    # router
    "DEFAULT_FAST_PATH_BUDGET_MS",
    "LLMRouter",
    "MockReasonerBackend",
    "ReasonerBackend",
    "RoutedVerdict",
    "RouteTier",
    "SignalBucketKey",
    "build_frontier_backend",
    "build_ollama_backend",
    "conservative_fallback_verdict",
    "is_dry_run",
    # reasoner
    "DeRiskReasoner",
    "ReasonerOutput",
    # calibration
    "CalibrationBin",
    "CalibrationReport",
    "brier_score",
    "expected_calibration_error",
    "reliability_curve",
    "run_calibration_check",
]
