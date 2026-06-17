"""Provenance / lineage taint hooks (ADR-0010, data-models.md §3.3).

This module provides the build-time and load-time guards that make
forward-looking label leakage structurally impossible.

Guards (validation-harness.md §2.5):
  1. feature_window_exceeds_cutoff  — per-feature source-slot cutoff
  2. feature_lineage_touches_label  — lineage taint against labels/
  3. label_column_in_feature_frame  — column-set disjointness
  4. recorded_at_before_knowable    — recorded_at honesty

All four guards raise ProvenanceTaintError (a build/load failure, not a warning).

The column-disjointness guard (guard 3) is a scaffold here:
  assert_feature_frame_column_disjointness(feature_columns, label_columns)
This is called at Parquet load time by the harness (T-400/T-401).  The
contracts package provides the guard; the harness owns the Parquet I/O.
"""

from __future__ import annotations

from collections.abc import Collection

# ---------------------------------------------------------------------------
# Allowlist of legal lineage datasets (data-models.md §3.3)
# 'labels' is NOT on this list — it is the taint target.
# ---------------------------------------------------------------------------

ALLOWED_LINEAGE_DATASETS: frozenset[str] = frozenset(
    {
        "launch_events",
        "feature_frames",
        "mcs_scores",
        "fills_pre_event",
    }
)

# The label dataset name — tainted; any feature whose lineage touches this fails.
_LABEL_DATASET = "labels"

# Canonical label field names from LaunchOutcome (data-models.md §3A).
# Used by the column-disjointness guard.
LABEL_FIELD_NAMES: frozenset[str] = frozenset(
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
        # Innocuously-named forward-looking columns caught by name
        # (belt-and-suspenders; the primary guard is lineage-based)
        "survived_60s",
        "realized_mult",
        "fwd_return",
        "rug_label",
        "outcome_label",
    }
)


class ProvenanceTaintError(Exception):
    """Raised when a provenance/lineage guard fires.

    This is a BUILD / LOAD failure, not a runtime warning.
    The guard makes the leak inexpressible, not merely policed.
    """

    def __init__(self, guard_name: str, detail: str) -> None:
        self.guard_name = guard_name
        self.detail = detail
        super().__init__(f"[{guard_name}] {detail}")


# ---------------------------------------------------------------------------
# Guard 1: per-feature cutoff (feature_window_exceeds_cutoff)
# ---------------------------------------------------------------------------


def check_feature_window_cutoff(
    feature_name: str,
    max_source_slot: int,
    event_time_slot: int,
    first_k_slots: int,
) -> None:
    """Guard 1: per-feature source-slot cutoff (data-models.md §3.3, guard 1).

    Fails if max_source_slot > event_time_slot + first_k_slots.
    A feature that read a slot after the decision point is rejected.

    Args:
        feature_name: The feature being checked.
        max_source_slot: The highest slot any datum feeding this feature came from.
        event_time_slot: The canonical decision slot.
        first_k_slots: K (the window size declared per surface).

    Raises:
        ProvenanceTaintError: If the feature's source window exceeds the cutoff.
    """
    cutoff = event_time_slot + first_k_slots
    if max_source_slot > cutoff:
        raise ProvenanceTaintError(
            guard_name="feature_window_exceeds_cutoff",
            detail=(
                f"Feature '{feature_name}' has max_source_slot={max_source_slot} "
                f"which exceeds the cutoff event_time.slot + first_k_slots = "
                f"{event_time_slot} + {first_k_slots} = {cutoff}.  "
                "This feature read data from AFTER the decision point — "
                "forward-looking leak detected.  "
                "Fix: ensure the feature pipeline only reads slots <= cutoff."
            ),
        )


# ---------------------------------------------------------------------------
# Guard 2: lineage taint (feature_lineage_touches_label)
# ---------------------------------------------------------------------------


def check_lineage_taint(feature_name: str, lineage_dataset: str) -> None:
    """Guard 2: lineage taint against labels/ (data-models.md §3.3, guard 2).

    This is the PRIMARY anti-label-leak defense (ADR-0010).
    Fails if lineage_dataset is not on the allowlist OR touches 'labels'.

    The taint check is structural, not name-based: an innocuously-named
    leaked label (survived_60s, realized_mult, fwd_return) is caught because
    its LINEAGE is tainted, not because its name matched truth_*.

    Args:
        feature_name: The feature being checked.
        lineage_dataset: The declared lineage dataset for this feature.

    Raises:
        ProvenanceTaintError: If the lineage touches a forbidden dataset.
    """
    if lineage_dataset == _LABEL_DATASET or lineage_dataset not in ALLOWED_LINEAGE_DATASETS:
        raise ProvenanceTaintError(
            guard_name="feature_lineage_touches_label",
            detail=(
                f"Feature '{feature_name}' has lineage_dataset='{lineage_dataset}' "
                f"which is not on the allowlist {sorted(ALLOWED_LINEAGE_DATASETS)}.  "
                f"The 'labels' dataset is the taint target — any feature whose "
                f"computation lineage touches labels/ FAILS, regardless of column name.  "
                "Fix: the feature must be computed solely from "
                f"{sorted(ALLOWED_LINEAGE_DATASETS)}."
            ),
        )


# ---------------------------------------------------------------------------
# Guard 3: label/feature column disjointness (label_column_in_feature_frame)
# ---------------------------------------------------------------------------


def assert_feature_frame_column_disjointness(
    feature_columns: Collection[str],
    label_columns: Collection[str],
) -> None:
    """Guard 3: column-set disjointness (data-models.md §3.3, guard 3, ADR-0010).

    At Parquet load time, asserts that feature_frames/ and labels/ column sets
    are DISJOINT.  Any overlap is a load failure.

    Called by the harness (T-400/T-401) at load time — not a runtime check.
    The contracts package provides this guard; the harness owns the Parquet I/O.

    Args:
        feature_columns: Column names from the feature_frames/ dataset.
        label_columns: Column names from the labels/ dataset.

    Raises:
        ProvenanceTaintError: If any column appears in both sets.
    """
    feature_set = frozenset(feature_columns)
    label_set = frozenset(label_columns)
    overlap = feature_set & label_set
    if overlap:
        raise ProvenanceTaintError(
            guard_name="label_column_in_feature_frame",
            detail=(
                f"Column(s) {sorted(overlap)} appear in BOTH feature_frames/ and labels/.  "
                "The column sets must be disjoint (data-models.md §3.3 guard 3, ADR-0010).  "
                "Labels live ONLY in labels/ and are joined to features by event_time only, "
                "NEVER merged into feature_frames/.  "
                "Fix: remove the overlapping column(s) from the feature pipeline."
            ),
        )


# ---------------------------------------------------------------------------
# Guard 4: recorded_at honesty (recorded_at_before_knowable)
# ---------------------------------------------------------------------------


def assert_recorded_at_honesty(
    recorded_at_ms: int,
    event_time_block_time_ms: int,
    dataset: str,
    mint: str,
    latest_recorded_at_ms: int | None = None,
) -> None:
    """Guard 4: recorded_at honesty (data-models.md §9.2, ADR-0010, guard 4).

    Enforces two invariants:
      a) recorded_at_ms >= event_time.block_time_ms
         (a datum cannot be recorded before its on-chain event existed)
      b) If latest_recorded_at_ms is provided (for a correction/backfill row),
         recorded_at_ms >= latest_recorded_at_ms
         (a backfill cannot travel backward in compute-time)

    Args:
        recorded_at_ms: When the datum was computed/recorded.
        event_time_block_time_ms: On-chain block time of the event (authoritative).
        dataset: Dataset name (for error context).
        mint: Mint address (for error context).
        latest_recorded_at_ms: For correction rows: the latest recorded_at already
            present for this (dataset, event_time) key.  None for original rows.

    Raises:
        ProvenanceTaintError: If either invariant is violated.
    """
    if recorded_at_ms < event_time_block_time_ms:
        raise ProvenanceTaintError(
            guard_name="recorded_at_before_knowable",
            detail=(
                f"dataset='{dataset}' mint='{mint}': "
                f"recorded_at_ms={recorded_at_ms} < "
                f"event_time.block_time_ms={event_time_block_time_ms}.  "
                "A datum cannot have been recorded before the on-chain event it describes "
                "existed.  This is the live-backfill lookahead vector (data-models.md §9.2).  "
                "Fix: ensure recorded_at_ms is stamped at compute-time, not back-dated."
            ),
        )
    if latest_recorded_at_ms is not None and recorded_at_ms < latest_recorded_at_ms:
        raise ProvenanceTaintError(
            guard_name="backfill_recorded_at_regression",
            detail=(
                f"dataset='{dataset}' mint='{mint}': "
                f"correction row has recorded_at_ms={recorded_at_ms} which predates "
                f"the latest_recorded_at_ms={latest_recorded_at_ms} already present.  "
                "A backfill row that travels backward in compute-time is a lookahead "
                "vector (data-models.md §9.2, ADR-0010).  "
                "Fix: correction rows must use a recorded_at_ms >= any existing row's "
                "recorded_at_ms for the same (dataset, event_time) key."
            ),
        )
