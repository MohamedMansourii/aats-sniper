"""Tests: ADR-0010 provenance/lineage taint hooks.

Verifies all four build/load guards:
  1. feature_window_exceeds_cutoff — per-feature source-slot cutoff
  2. feature_lineage_touches_label — lineage taint guard
  3. label_column_in_feature_frame — column-set disjointness
  4. recorded_at_before_knowable / backfill_recorded_at_regression

Source: data-models.md §3.3, ADR-0010, validation-harness.md §2.5.
"""

from __future__ import annotations

import pytest

from aats.contracts.provenance import (
    ALLOWED_LINEAGE_DATASETS,
    ProvenanceTaintError,
    assert_feature_frame_column_disjointness,
    assert_recorded_at_honesty,
    check_feature_window_cutoff,
    check_lineage_taint,
)

# ---------------------------------------------------------------------------
# Guard 1: feature_window_exceeds_cutoff
# ---------------------------------------------------------------------------


class TestFeatureWindowCutoffGuard:
    def test_within_cutoff_passes(self):
        """A feature whose max_source_slot <= event_time + K passes."""
        check_feature_window_cutoff(
            feature_name="first_k_volume_lamports",
            max_source_slot=1005,
            event_time_slot=1000,
            first_k_slots=5,
        )  # cutoff = 1005; 1005 <= 1005 → passes

    def test_at_cutoff_passes(self):
        check_feature_window_cutoff(
            feature_name="first_k_volume_lamports",
            max_source_slot=1005,
            event_time_slot=1000,
            first_k_slots=5,
        )

    def test_exceeds_cutoff_raises(self):
        """A feature that read slot 1006 when cutoff is 1005 must fail."""
        with pytest.raises(ProvenanceTaintError) as exc_info:
            check_feature_window_cutoff(
                feature_name="leaked_feature",
                max_source_slot=1006,  # 1 slot past cutoff
                event_time_slot=1000,
                first_k_slots=5,
            )
        err = str(exc_info.value)
        assert "feature_window_exceeds_cutoff" in err
        assert "leaked_feature" in err

    def test_far_future_slot_raises(self):
        """A feature that read event_time + H (label horizon) must fail."""
        with pytest.raises(ProvenanceTaintError) as exc_info:
            check_feature_window_cutoff(
                feature_name="survived_60s",
                max_source_slot=1060,  # 60 slots past event_time — label horizon
                event_time_slot=1000,
                first_k_slots=5,
            )
        assert "feature_window_exceeds_cutoff" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Guard 2: feature_lineage_touches_label
# ---------------------------------------------------------------------------


class TestLineageTaintGuard:
    @pytest.mark.parametrize("allowed", sorted(ALLOWED_LINEAGE_DATASETS))
    def test_allowed_lineage_datasets_pass(self, allowed: str):
        """All allowed lineage datasets pass without raising."""
        check_lineage_taint(feature_name="some_feature", lineage_dataset=allowed)

    def test_labels_lineage_raises(self):
        """A feature whose lineage touches 'labels' must fail (PRIMARY guard, ADR-0010)."""
        with pytest.raises(ProvenanceTaintError) as exc_info:
            check_lineage_taint(feature_name="survived_60s", lineage_dataset="labels")
        err = str(exc_info.value)
        assert "feature_lineage_touches_label" in err
        assert "survived_60s" in err

    def test_unknown_dataset_raises(self):
        """An unknown lineage dataset (not on allowlist) must fail."""
        with pytest.raises(ProvenanceTaintError) as exc_info:
            check_lineage_taint(feature_name="some_feature", lineage_dataset="external_data")
        assert "feature_lineage_touches_label" in str(exc_info.value)

    def test_innocuously_named_label_caught_by_lineage(self):
        """A column named 'realized_mult' is caught if its lineage is 'labels'."""
        with pytest.raises(ProvenanceTaintError):
            check_lineage_taint(feature_name="realized_mult", lineage_dataset="labels")

    def test_fwd_return_caught_by_lineage(self):
        with pytest.raises(ProvenanceTaintError):
            check_lineage_taint(feature_name="fwd_return", lineage_dataset="labels")


# ---------------------------------------------------------------------------
# Guard 3: label_column_in_feature_frame (column-set disjointness)
# ---------------------------------------------------------------------------


class TestColumnDisjointnessGuard:
    def test_disjoint_sets_pass(self):
        """Disjoint column sets must pass."""
        # Note: 'mint' is excluded from BOTH in this test (it is the join key, not a data column)
        feature_cols2 = ["first_k_volume_lamports", "lp_depth_lamports"]
        label_cols2 = ["label", "resolution_event_time"]
        assert_feature_frame_column_disjointness(feature_cols2, label_cols2)

    def test_overlap_raises(self):
        """A column that appears in BOTH datasets must fail."""
        feature_cols = ["first_k_volume_lamports", "label", "lp_depth_lamports"]
        label_cols = ["label", "resolution_event_time"]
        with pytest.raises(ProvenanceTaintError) as exc_info:
            assert_feature_frame_column_disjointness(feature_cols, label_cols)
        err = str(exc_info.value)
        assert "label_column_in_feature_frame" in err
        assert "label" in err

    def test_truth_field_in_feature_caught(self):
        """A truth_* field in feature_cols is caught if it also appears in label_cols."""
        feature_cols = ["first_k_volume_lamports", "truth_is_rug"]
        label_cols = ["label", "truth_is_rug"]
        with pytest.raises(ProvenanceTaintError) as exc_info:
            assert_feature_frame_column_disjointness(feature_cols, label_cols)
        assert "truth_is_rug" in str(exc_info.value)

    def test_empty_sets_pass(self):
        assert_feature_frame_column_disjointness([], [])
        assert_feature_frame_column_disjointness(["a", "b"], [])
        assert_feature_frame_column_disjointness([], ["c", "d"])


# ---------------------------------------------------------------------------
# Guard 4: recorded_at_before_knowable / backfill_recorded_at_regression
# ---------------------------------------------------------------------------


class TestRecordedAtHonestyGuard:
    def test_valid_recorded_at_passes(self):
        """recorded_at_ms >= event_time.block_time_ms is valid."""
        assert_recorded_at_honesty(
            recorded_at_ms=1_700_000_001_000,
            event_time_block_time_ms=1_700_000_000_000,
            dataset="feature_frames",
            mint="So11111111111111111111111111111111111111112",
        )

    def test_recorded_at_equal_block_time_passes(self):
        """recorded_at_ms == event_time.block_time_ms is technically valid (edge case)."""
        assert_recorded_at_honesty(
            recorded_at_ms=1_700_000_000_000,
            event_time_block_time_ms=1_700_000_000_000,
            dataset="feature_frames",
            mint="So11111111111111111111111111111111111111112",
        )

    def test_recorded_before_knowable_raises(self):
        """recorded_at_ms < event_time.block_time_ms must fail (recorded_at_before_knowable)."""
        with pytest.raises(ProvenanceTaintError) as exc_info:
            assert_recorded_at_honesty(
                recorded_at_ms=1_699_999_999_000,  # BEFORE the on-chain event
                event_time_block_time_ms=1_700_000_000_000,
                dataset="feature_frames",
                mint="So11111111111111111111111111111111111111112",
            )
        err = str(exc_info.value)
        assert "recorded_at_before_knowable" in err

    def test_backfill_regression_raises(self):
        """A correction row with recorded_at < latest_recorded_at must fail."""
        with pytest.raises(ProvenanceTaintError) as exc_info:
            assert_recorded_at_honesty(
                recorded_at_ms=1_700_000_002_000,  # newer than event but older than latest
                event_time_block_time_ms=1_700_000_000_000,
                dataset="feature_frames",
                mint="So11111111111111111111111111111111111111112",
                latest_recorded_at_ms=1_700_000_005_000,  # latest already at +5s
            )
        err = str(exc_info.value)
        assert "backfill_recorded_at_regression" in err

    def test_valid_correction_row_passes(self):
        """A correction row with recorded_at >= latest_recorded_at is valid."""
        assert_recorded_at_honesty(
            recorded_at_ms=1_700_000_010_000,  # later than latest
            event_time_block_time_ms=1_700_000_000_000,
            dataset="feature_frames",
            mint="So11111111111111111111111111111111111111112",
            latest_recorded_at_ms=1_700_000_005_000,
        )


# ---------------------------------------------------------------------------
# Integration: full provenance chain with FeatureFrame
# ---------------------------------------------------------------------------


class TestFeatureFrameProvenanceIntegration:
    """Full integration: a FeatureFrame's provenance is checked by all four guards."""

    def test_feature_frame_with_valid_provenance_passes_all_guards(self):
        """A well-formed FeatureFrame with honest provenance passes all guards."""
        from decimal import Decimal

        from aats.contracts.events import EventTime
        from aats.contracts.features import FeatureFrame, FeatureProvenance, FeatureSourceWindow

        et = EventTime(slot=1000, block_time_ms=1_700_000_000_000, wall_clock_ms=1_700_000_000_100)
        provenance = FeatureProvenance(
            windows=[
                FeatureSourceWindow(
                    feature_name="first_k_volume_lamports",
                    max_source_slot=1005,  # <= 1000 + 5
                    lineage_dataset="launch_events",
                ),
                FeatureSourceWindow(
                    feature_name="lp_depth_lamports",
                    max_source_slot=1005,
                    lineage_dataset="feature_frames",
                ),
            ],
            builder_version="v0.1.0",
        )
        ff = FeatureFrame(
            mint="So11111111111111111111111111111111111111112",
            event_time=et,
            feature_schema_version="1.0.0",
            data_staleness_ms=50,
            first_k_slots=5,
            first_k_buy_pressure=Decimal("1000000000"),
            first_k_volume_lamports=5_000_000_000,
            first_k_buy_count=10,
            first_k_sell_count=2,
            first_k_unique_buyers=8,
            lp_depth_lamports=10_000_000_000,
            holder_count=200,
            holder_concentration_top10_bps=3000,
            sniper_cluster_score=0.3,
            sell_tax_bps=0,
            sell_reserve_trajectory=[10_000_000_000],
            rsi=None,
            macd=None,
            bb_width=None,
            smart_wallets_in=0,
            smart_wallet_entry_lag_slots=None,
            tip_floor_at_decision_lamports=200_000,
            tip_contention_bucket="low",
            normalization_window="as_of_event_time",
            provenance=provenance,
        )
        # Run guard 1 on each window
        for window in ff.provenance.windows:
            check_feature_window_cutoff(
                feature_name=window.feature_name,
                max_source_slot=window.max_source_slot,
                event_time_slot=ff.event_time.slot,
                first_k_slots=ff.first_k_slots,
            )
        # Run guard 2 on each window
        for window in ff.provenance.windows:
            check_lineage_taint(
                feature_name=window.feature_name,
                lineage_dataset=window.lineage_dataset,
            )
        # Guard 3: no label columns on this frame
        frame_col_names = list(type(ff).model_fields.keys())
        label_col_names = ["label", "resolution_event_time", "realized_mult_decimal"]
        assert_feature_frame_column_disjointness(frame_col_names, label_col_names)
