"""FeatureFrame, FeatureProvenance, and FeatureSourceWindow.

Source of truth: data-models.md §3 (FeatureFrame) and §3.3 (build-time guards).
These are FROZEN post-G1; no field may be added without an ADR + delta notice.

Leak-prevention invariants enforced BY CONSTRUCTION:
  1. event_time is the ONLY allowed join anchor (wall_clock_ms is monitoring-only).
  2. normalization_window must be "as_of_event_time" — no global-stat normalization.
  3. provenance (FeatureProvenance) is REQUIRED — no feature escapes the window check.
  4. No truth_* field exists on this model (C-7 guard).
  5. No LaunchOutcome / labels field exists on this model (ADR-0010 §3.3 guard 3).

Money fields (data-models.md §0):
  first_k_volume_lamports  → int  (lamports)
  tip_floor_at_decision_lamports → int  (lamports)
  sell_reserve_trajectory  → list[int]  (each entry: lamports/base-units)
  lp_depth_lamports        → int  (lamports)
  first_k_buy_pressure     → Decimal-as-string (net SOL pressure, exact quantity)

Statistical / feature quantities that are genuinely real-valued may be float
(sniper_cluster_score, rsi, macd, bb_width, etc.) — the money rule does not
apply to them.

T-199a FIX — WHY 'from __future__ import annotations' IS REMOVED:
  PEP 563 (from __future__ import annotations) causes ALL annotations in this
  module to become lazy strings.  Pydantic v2 must call model_rebuild() to
  resolve them and materialise the CoreSchema.  If features.py is first accessed
  during pytest collection (before the module completes its own model_rebuild()
  call), the model can be left with __pydantic_complete__ = False.  In that
  partial-build state the CoreSchema lacks extra_fields_behavior='forbid', so
  FeatureFrame(**data, label='RUGGED') silently drops the extra kwarg instead of
  raising — the negative tests do not raise (the flake).

  The fix is two layers:
    1. Remove 'from __future__ import annotations' so ALL annotations are
       evaluated eagerly at class-definition time.  The self-referential return
       type '-> FeatureFrame' in _no_label_fields is replaced with no return
       annotation (Pydantic does not require one for mode='after' validators).
       This eliminates the forward-ref that triggers lazy schema building.
    2. Call FeatureFrame.model_rebuild(force=True) at module bottom as a
       belt-and-suspenders guarantee: even if Pydantic's internal model_rebuild
       was somehow skipped, the explicit call forces the complete CoreSchema
       (with extra_fields_behavior='forbid') to be in place before any test
       or import can reach the class.

  Mutation proof: reverting extra="forbid" to extra="ignore" (or removing
  model_config entirely) makes the negative-construction tests in
  tests/contracts/test_no_truth_fields.py::TestLabelColumnDisjointness
  go RED deterministically across all PYTHONHASHSEED values.
"""

from decimal import Decimal
from typing import Literal, Optional

from pydantic import BaseModel, field_validator, model_validator

from aats.contracts.events import EventTime


class FeatureSourceWindow(BaseModel):
    """Per-feature declaration of the LATEST source slot the feature is allowed to read.

    The producing pipeline MUST stamp one of these per non-metadata feature field.
    A missing window is provenance_window_missing — also a build failure.

    Source: data-models.md §3.3 (build-time guard 1 and 2).
    """

    feature_name: str
    # The highest slot any datum feeding this feature came from.
    max_source_slot: int
    # Allowed lineage datasets — 'labels' is NOT on this list (ADR-0010 guard 2).
    lineage_dataset: Literal[
        "launch_events",
        "feature_frames",
        "mcs_scores",
        "fills_pre_event",
    ]

    model_config = {"frozen": True}


class FeatureProvenance(BaseModel):
    """Provenance manifest for a FeatureFrame (per-feature source windows).

    Source: data-models.md §3.3.
    The build guard (validation-harness.md §2.5) FAILs if any window's
    max_source_slot > event_time.slot + first_k_slots, or if any window's
    lineage_dataset touches 'labels/'.
    """

    windows: list[FeatureSourceWindow]  # one per non-metadata feature field
    builder_version: str  # the feature-pipeline build that produced these windows

    model_config = {"frozen": True}


class FeatureFrame(BaseModel):
    """Event-time-stamped sensor output — the most leak-sensitive contract.

    Source: data-models.md §3.  ALL values reconstructable from data with
    observation-time <= event_time (walk-forward §1; FR-005/010/012).

    The frame carries its own event_time so a Parquet row can never be joined
    on compute-time.  The provenance manifest makes the leak guard structural,
    not conventional.

    Money fields are int (lamports) or Decimal-as-string.
    Statistical / feature float fields are explicitly float.
    NO truth_* fields — those are sim-only (C-7).
    NO LaunchOutcome/label fields — disjointness guard (ADR-0010 §3.3 guard 3).
    """

    mint: str
    event_time: EventTime  # anchor — copied from LaunchEvent (C-5)
    feature_schema_version: str  # bump on any feature add/change (C-4 frozen-baseline)
    data_staleness_ms: int  # max staleness across features in this frame (FR-057)

    # --- first-K-slot microstructure (slots <= event_time + K only) ---
    first_k_slots: int  # K (declared per surface)
    # C-4 BASELINE ENABLER: net buy SOL pressure over first K slots (Decimal-string)
    first_k_buy_pressure: Decimal  # exact quantity — Decimal, not float
    # C-4 BASELINE ENABLER: total volume over first K slots (lamports, integer)
    first_k_volume_lamports: int
    first_k_buy_count: int
    first_k_sell_count: int
    first_k_unique_buyers: int

    # --- first-60s survivor microstructure (FR-010) ---
    lp_depth_lamports: int  # lamports, integer
    holder_count: int
    holder_concentration_top10_bps: int  # share held by top 10, in bps
    sniper_cluster_score: float  # statistical, float is correct
    sell_tax_bps: int
    sell_reserve_trajectory: list[int]  # reserve samples (lamports/base-units, integer each)

    # --- survivor TA (FR-010) ---
    rsi: Optional[float]
    macd: Optional[float]
    bb_width: Optional[float]

    # --- adversarial selectivity (NEVER a buy trigger; FR-007/011, EH-005) ---
    smart_wallets_in: int  # count of tracked wallets buying in first K slots
    smart_wallet_entry_lag_slots: Optional[int]  # our entry slot - their fill slot (we are BEHIND)

    # --- tip-contention context (C-3, FR-047) ---
    tip_floor_at_decision_lamports: int  # LIVE tip floor at event_time (integer lamports)
    tip_contention_bucket: Literal["low", "medium", "high"]

    # --- normalization provenance (C-5 / walk-forward §3) ---
    # "as_of_event_time" is the ONLY legal value — no global-stat normalization
    normalization_window: Literal["as_of_event_time"]

    # --- per-feature provenance manifest (ADR-0010, data-models.md §3.3) ---
    # REQUIRED: build FAILS if any feature's window extends past event_time + K
    # or if any lineage touches 'labels/'.
    provenance: FeatureProvenance

    # extra="forbid" is REQUIRED: it makes Pydantic reject any kwarg whose name
    # matches a label/truth_* field at construction time, before any validator runs.
    # Without it, Pydantic's default extra="ignore" would silently drop those kwargs
    # and the _no_label_fields model_validator would never see them (dead code).
    # Together: extra="forbid" rejects unknown-field injections; _no_label_fields is a
    # defense-in-depth guard for fields that could be added to the model definition itself.
    #
    # T-199a: extra="forbid" MUST be present and ENFORCED.  The module-bottom
    # model_rebuild(force=True) call guarantees the CoreSchema is materialised with
    # extra_fields_behavior='forbid' before any external code can reach this class,
    # regardless of import order or PYTHONHASHSEED.
    model_config = {"frozen": True, "extra": "forbid"}

    @model_validator(mode="before")
    @classmethod
    def _reject_float_money(cls, data: object) -> object:
        """Reject float values for integer money fields (data-models.md §0)."""
        if not isinstance(data, dict):
            return data
        _INT_MONEY_FIELDS = (
            "first_k_volume_lamports",
            "lp_depth_lamports",
            "tip_floor_at_decision_lamports",
            "first_k_buy_count",
            "first_k_sell_count",
            "first_k_unique_buyers",
            "holder_count",
            "sell_tax_bps",
        )
        for field in _INT_MONEY_FIELDS:
            val = data.get(field)
            if isinstance(val, float):
                raise ValueError(
                    f"Money field '{field}' must be int (lamports/base-units), "
                    f"got float {val!r}.  Use integer base units (data-models.md §0)."
                )
        # sell_reserve_trajectory entries must be int
        traj = data.get("sell_reserve_trajectory")
        if isinstance(traj, list):
            for i, entry in enumerate(traj):
                if isinstance(entry, float):
                    raise ValueError(
                        f"sell_reserve_trajectory[{i}] must be int, got float {entry!r}."
                    )
        return data

    @field_validator("first_k_buy_pressure", mode="before")
    @classmethod
    def _parse_decimal_pressure(cls, v: object) -> Decimal:
        """Accept Decimal, int, or decimal string — reject float."""
        if isinstance(v, float):
            raise ValueError(
                "first_k_buy_pressure must be Decimal or decimal string, not float.  "
                "Use Decimal('...') or a string representation (data-models.md §0)."
            )
        if isinstance(v, Decimal):
            return v
        if isinstance(v, int):
            return Decimal(v)
        if isinstance(v, str):
            try:
                return Decimal(v)
            except Exception as exc:
                raise ValueError(
                    f"Cannot parse first_k_buy_pressure from string {v!r}: {exc}"
                ) from exc
        raise ValueError(
            f"first_k_buy_pressure must be Decimal, int, or string, got {type(v).__name__}"
        )

    @model_validator(mode="after")
    def _no_label_fields(self):
        # Return type annotation intentionally omitted: with PEP 563 removed from this
        # module, there is no forward-ref risk, but we also do not annotate the return
        # as 'FeatureFrame' to avoid any future re-introduction of the self-reference
        # that was the root cause of the T-199a flake.
        """Defense-in-depth guard: no LaunchOutcome field names on a FeatureFrame.

        Primary defense: model_config extra="forbid" (above) rejects any unknown kwarg
        — including planted label/truth_* kwargs — before this validator is reached.

        This validator is the secondary layer: it fires when someone adds a field to the
        FeatureFrame class definition whose name collides with a label-dataset field name,
        catching accidental schema contamination that extra="forbid" cannot prevent (since
        a declared field is not "extra").

        Together the two layers enforce ADR-0010, data-models.md §3.3 guard 3, and
        validation-harness.md §2.5 guard 7 at Python construction time.
        """
        # The canonical label field names from LaunchOutcome (data-models.md §3A).
        _LABEL_FIELD_NAMES = frozenset(
            {
                "label",
                "label_horizon_h_slots",
                "resolution_event_time",
                "realized_mult_decimal",
                "max_drawdown_bps",
                "rug_detectable_at_decision",
                "resolution_recorded_at_ms",
                # Sim truth_* fields — never on production models
                "truth_is_rug",
                "truth_max_multiple",
                "truth_rug_detectable",
            }
        )
        frame_fields = set(self.model_fields_set) | set(type(self).model_fields.keys())
        overlap = frame_fields & _LABEL_FIELD_NAMES
        if overlap:
            raise ValueError(
                f"FeatureFrame contains label-dataset field(s): {sorted(overlap)}.  "
                "Label fields must live in labels/ only (ADR-0010, label_column_in_feature_frame)."
            )
        return self


# ---------------------------------------------------------------------------
# T-199a: Belt-and-suspenders model_rebuild call.
#
# This MUST remain at module bottom, after ALL class definitions in this file.
# It forces Pydantic to resolve all annotations (including EventTime from
# aats.contracts.events) and materialise the CoreSchema with
# extra_fields_behavior='forbid' BEFORE any external code can reach FeatureFrame.
#
# Why this matters: even without 'from __future__ import annotations', Pydantic v2
# may defer CoreSchema construction until first use in some edge cases (e.g.,
# partially-initialised module state during circular imports).  The explicit
# model_rebuild(force=True) eliminates that window.
#
# Mutation proof: if you remove model_config's extra="forbid" and run
#   pytest tests/contracts/test_no_truth_fields.py::TestLabelColumnDisjointness
# the three *_rejected_at_construction tests go RED deterministically (no exception
# is raised), because Pydantic silently drops the extra kwargs.
# ---------------------------------------------------------------------------
FeatureFrame.model_rebuild(force=True)
FeatureProvenance.model_rebuild(force=True)
FeatureSourceWindow.model_rebuild(force=True)
