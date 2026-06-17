"""Snipe-classifier feature set — the model's input vector (T-310).

WHAT THIS IS
============
The single, frozen, ordered list of columns the FAST snipe classifier consumes,
extracted from a point-in-time `FeatureFrame` (aats.contracts.features).  The model
NEVER reads raw chain data — it reads only the already-provenance-guarded FeatureFrame
fields.  Every column here is reconstructable from data with observation-time
<= event_time + K (the FeatureFrame already proved this at construction).

THE LINEAGE-TAINT BUILD GUARD (the heart of T-310's no-leak requirement)
========================================================================
`assert_no_label_taint()` is the build guard the brief demands: it FAILS the build
(ProvenanceTaintError) if ANY truth_*/label-named column tries to enter the feature
matrix.  It is the model-side mirror of the contract guards in
aats.contracts.provenance:
  - it rejects any column whose name is in LABEL_FIELD_NAMES (data-models §3A,
    e.g. label, realized_mult_decimal, survived_60s, fwd_return, truth_is_rug);
  - it rejects any column with a `truth_` prefix (sim-only, C-7);
  - it asserts the model feature columns are DISJOINT from the label columns
    (guard 3, label_column_in_feature_frame).
The training pipeline calls this on the assembled feature matrix BEFORE fitting, so a
label column that somehow reached the matrix taints the lineage and fails the build —
not a reviewer's eye.

WHY THESE COLUMNS (and not others)
==================================
Available INSIDE the first blocks of a launch (FeatureFrame §3 / data-models §3):
  - first-K microstructure: buy/sell pressure, volume, counts, unique-buyer velocity
  - LP / survivor microstructure: depth, holder count, top-10 concentration, sell tax,
    sniper-cluster score
  - survivor TA (rsi/macd/bb_width) — nullable; imputed with a sentinel + a presence flag
  - adversarial selectivity: smart_wallets_in, entry-lag (NEVER a buy trigger; a high
    value pushes risk DOWN — we are behind smarter money — never up)
  - tip-contention context: tip_floor_at_decision, contention bucket (one-hot)

EXCLUDED ON PURPOSE
===================
  - event_time fields (slot / block_time / wall_clock): identifiers, never features
    (using them as features would let the model memorise the calendar — leak-adjacent).
  - mint, feature_schema_version, normalization_window, provenance: metadata.
  - ANY label / truth_* / forward-looking column: rejected by assert_no_label_taint.

NO MONEY-AS-FLOAT LEAK INTO MODEL MATH
======================================
Lamport quantities are large integers.  For the model matrix they are cast to float64
(a statistical feature value, NOT a money field per data-models §0 — the rule is
explicit that "feature values that are genuinely real-valued may be float").  We do
NOT do money arithmetic in float here — we read already-final integer/Decimal values
and present them as model inputs.  No PnL, fee, tip, or size is ever computed in this
module.

OUTPUT BOUNDARY
===============
This module produces a FEATURE VECTOR ONLY.  It never produces a probability, a price,
or a decision.  Calibrated probability + uncertainty is the inference wrapper's job
(inference.py); the trade decision is downstream (OMS/sizer) and not ours.
"""

from __future__ import annotations

from decimal import Decimal

from aats.contracts.features import FeatureFrame
from aats.contracts.provenance import LABEL_FIELD_NAMES, ProvenanceTaintError

# ---------------------------------------------------------------------------
# Feature schema version — bump on any change to FEATURE_COLUMNS (C-4 frozen-baseline
# safety: a model trained on a different feature set has a different model_version).
# ---------------------------------------------------------------------------

MODEL_FEATURE_SCHEMA_VERSION = "1.0.0"

# Sentinel used when a nullable TA feature (rsi/macd/bb_width) is absent.  A companion
# *_present flag carries the missingness so the tree can split on "TA unavailable"
# without confusing the sentinel for a real value.
_NULL_SENTINEL = 0.0

# The ORDERED, FROZEN feature column list.  ONNX inference reads the row in EXACTLY
# this order — the order is part of the model contract.  Adding/removing a column is a
# schema change (bump MODEL_FEATURE_SCHEMA_VERSION) and produces a new model_version.
FEATURE_COLUMNS: tuple[str, ...] = (
    # --- first-K microstructure (point-in-time, slots <= event_time + K) ---
    "first_k_buy_pressure",          # Decimal net SOL pressure -> float64 model input
    "first_k_volume_lamports",       # int lamports -> float64 model input
    "first_k_buy_count",
    "first_k_sell_count",
    "first_k_unique_buyers",
    "first_k_buy_sell_ratio",        # derived: buys / (buys + sells + 1)  (bounded, PIT)
    "first_k_unique_buyer_velocity", # derived: unique_buyers / (K + 1)    (bounded, PIT)
    # --- LP / survivor microstructure ---
    "lp_depth_lamports",
    "holder_count",
    "holder_concentration_top10_bps",
    "sniper_cluster_score",
    "sell_tax_bps",
    # --- survivor TA (nullable -> sentinel + presence flag) ---
    "rsi",
    "rsi_present",
    "macd",
    "macd_present",
    "bb_width",
    "bb_width_present",
    # --- adversarial selectivity (de-risk-only; high => behind smart money => risk DOWN) ---
    "smart_wallets_in",
    "smart_wallet_entry_lag_slots",  # nullable -> sentinel + presence flag
    "smart_wallet_entry_lag_present",
    # --- tip-contention context (one-hot of the bucket) ---
    "tip_floor_at_decision_lamports",
    "tip_contention_low",
    "tip_contention_medium",
    "tip_contention_high",
)

N_FEATURES = len(FEATURE_COLUMNS)

# Columns whose learned effect on the model output MUST be monotone non-increasing
# (a higher value can only push the probability DOWN, never up).  These are the
# adversarial / contrarian-risk features: being behind smart money, high holder
# concentration, and high sell tax are RISK signals — the model must not learn to
# treat them as bullish (BUILD-DIRECTIVE adversarial-sentiment / de-risk rule).
# Enforced as LightGBM monotone constraints at fit time (training.py) and asserted by
# the sign test (tests/models/test_snipe_classifier.py).
MONOTONE_NONINCREASING_FEATURES: frozenset[str] = frozenset(
    {
        "holder_concentration_top10_bps",  # whale concentration => rug risk
        "sell_tax_bps",                     # honeypot-adjacent tax => risk
        "smart_wallets_in",                 # we are BEHIND smarter money => de-risk
        "smart_wallet_entry_lag_slots",     # larger lag => more behind => de-risk
    }
)


def monotone_constraint_vector() -> list[int]:
    """Per-feature monotone constraint for LightGBM (1, 0, or -1), in FEATURE_COLUMNS order.

    -1 => monotone non-increasing (the de-risk features above).
     0 => unconstrained.
    We never use +1 (forced-increasing) — there is no feature we *require* to be bullish.
    """
    return [
        -1 if col in MONOTONE_NONINCREASING_FEATURES else 0 for col in FEATURE_COLUMNS
    ]


# ---------------------------------------------------------------------------
# The lineage-taint build guard (brief: "a build guard must ensure NO
# truth_*/label field leaks into the feature matrix (lineage taint)").
# ---------------------------------------------------------------------------


def assert_no_label_taint(columns: list[str] | tuple[str, ...]) -> None:
    """FAIL the build if any truth_*/label-named column is present in the feature matrix.

    This is the model-side lineage-taint guard (ADR-0010 guard 2/3 mirror).  It is a
    BUILD failure (ProvenanceTaintError), not a warning.

    Three checks:
      1. truth_ prefix scan (sim-only fields, C-7 belt-and-suspenders).
      2. exact membership in LABEL_FIELD_NAMES (the LaunchOutcome label columns and the
         innocuously-named forward-looking columns — survived_60s, realized_mult,
         fwd_return — caught by the contract's name set).
      3. disjointness of the model feature columns from LABEL_FIELD_NAMES.

    Args:
        columns: the column names about to be used as the model feature matrix.

    Raises:
        ProvenanceTaintError: if any column is a label/truth_* column.
    """
    cols = list(columns)
    # 1. truth_ prefix scan.
    truth_hits = [c for c in cols if c.startswith("truth_")]
    if truth_hits:
        raise ProvenanceTaintError(
            guard_name="feature_lineage_touches_label",
            detail=(
                f"truth_* column(s) {sorted(truth_hits)} reached the snipe-classifier "
                "feature matrix.  truth_* fields are sim-only ground truth (C-7) and a "
                "forward-looking leak.  A model trained on them prints money on paper and "
                "loses it live.  Fix: remove them from the feature pipeline."
            ),
        )
    # 2 + 3. exact label-name membership / disjointness.
    overlap = sorted(set(cols) & LABEL_FIELD_NAMES)
    if overlap:
        raise ProvenanceTaintError(
            guard_name="label_column_in_feature_frame",
            detail=(
                f"Label/forward-looking column(s) {overlap} reached the snipe-classifier "
                "feature matrix.  These are LaunchOutcome label columns (data-models §3A) "
                "or innocuously-named forward-looking columns.  They must live ONLY in "
                "labels/ and join to features by event_time — NEVER enter the feature "
                "matrix.  Fix: drop them before building the matrix."
            ),
        )


# ---------------------------------------------------------------------------
# Feature extraction from a FeatureFrame -> the model row (FEATURE_COLUMNS order)
# ---------------------------------------------------------------------------


def _f(value: float | int | Decimal | None) -> float:
    """Cast a frame value to a model float input. None -> NULL sentinel.

    Decimal/int are read as already-final values and presented as float64 model
    inputs (statistical feature values, data-models §0 — NOT money arithmetic).
    """
    if value is None:
        return _NULL_SENTINEL
    return float(value)


def frame_to_feature_row(frame: FeatureFrame) -> list[float]:
    """Extract the model feature row from a FeatureFrame, in FEATURE_COLUMNS order.

    NO label, truth_*, event_time, or metadata field is read.  The derived ratios are
    point-in-time (functions of first-K counts only) and bounded in [0, 1].

    Returns a list[float] of length N_FEATURES.  The caller (training/inference) stacks
    these into the matrix and the lineage-taint guard runs on FEATURE_COLUMNS first.
    """
    buys = frame.first_k_buy_count
    sells = frame.first_k_sell_count
    k = frame.first_k_slots

    buy_sell_ratio = buys / (buys + sells + 1)  # bounded (0,1), PIT (first-K counts only)
    unique_buyer_velocity = frame.first_k_unique_buyers / (k + 1) if k >= 0 else 0.0

    row_by_name: dict[str, float] = {
        "first_k_buy_pressure": _f(frame.first_k_buy_pressure),
        "first_k_volume_lamports": _f(frame.first_k_volume_lamports),
        "first_k_buy_count": _f(buys),
        "first_k_sell_count": _f(sells),
        "first_k_unique_buyers": _f(frame.first_k_unique_buyers),
        "first_k_buy_sell_ratio": float(buy_sell_ratio),
        "first_k_unique_buyer_velocity": float(unique_buyer_velocity),
        "lp_depth_lamports": _f(frame.lp_depth_lamports),
        "holder_count": _f(frame.holder_count),
        "holder_concentration_top10_bps": _f(frame.holder_concentration_top10_bps),
        "sniper_cluster_score": _f(frame.sniper_cluster_score),
        "sell_tax_bps": _f(frame.sell_tax_bps),
        "rsi": _f(frame.rsi),
        "rsi_present": 1.0 if frame.rsi is not None else 0.0,
        "macd": _f(frame.macd),
        "macd_present": 1.0 if frame.macd is not None else 0.0,
        "bb_width": _f(frame.bb_width),
        "bb_width_present": 1.0 if frame.bb_width is not None else 0.0,
        "smart_wallets_in": _f(frame.smart_wallets_in),
        "smart_wallet_entry_lag_slots": _f(frame.smart_wallet_entry_lag_slots),
        "smart_wallet_entry_lag_present": (
            1.0 if frame.smart_wallet_entry_lag_slots is not None else 0.0
        ),
        "tip_floor_at_decision_lamports": _f(frame.tip_floor_at_decision_lamports),
        "tip_contention_low": 1.0 if frame.tip_contention_bucket == "low" else 0.0,
        "tip_contention_medium": 1.0 if frame.tip_contention_bucket == "medium" else 0.0,
        "tip_contention_high": 1.0 if frame.tip_contention_bucket == "high" else 0.0,
    }

    # Assert we produced exactly the declared columns (no silent drift).
    if set(row_by_name.keys()) != set(FEATURE_COLUMNS):
        missing = set(FEATURE_COLUMNS) - set(row_by_name.keys())
        extra = set(row_by_name.keys()) - set(FEATURE_COLUMNS)
        raise ValueError(
            f"frame_to_feature_row produced a column set != FEATURE_COLUMNS. "
            f"missing={sorted(missing)} extra={sorted(extra)}"
        )

    return [row_by_name[col] for col in FEATURE_COLUMNS]
