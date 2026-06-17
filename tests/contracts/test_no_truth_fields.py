"""Tests: no truth_* field on any production/validation model.

The clean-room harness (C-7, AC-056) must never reference truth_is_rug,
truth_max_multiple, or truth_rug_detectable.  The import guard in
validation-harness.md §2 enforces this at build time.

These tests assert the structural guarantee in the contracts package:
  1. No production model has a truth_* field.
  2. A FeatureFrame that carries a label-field name is rejected.
  3. No LaunchOutcome label field appears on FeatureFrame.

Source rules: data-models.md §2 (no truth_* on LaunchEvent), §3 (no truth_* on
FeatureFrame), ADR-0010, validation-harness.md §2.

T-199a: Leak-guard non-determinism fix.
  Root cause: 'from __future__ import annotations' in features.py caused Pydantic v2
  to build the FeatureFrame CoreSchema lazily (forward refs unresolved until
  model_rebuild).  Under some PYTHONHASHSEED values, the model could be accessed
  before model_rebuild completed, leaving extra_fields_behavior as the default
  'ignore' instead of 'forbid'.  Extra kwargs like label='RUGGED' were silently
  dropped, the _no_label_fields mode='after' validator never saw them, and the
  negative tests did not raise.

  Fix (see features.py module docstring for full explanation):
  1. Removed 'from __future__ import annotations' from features.py.
  2. Added FeatureFrame.model_rebuild(force=True) at features.py module bottom.
  3. Added TestLeakGuardMutationProof below: verifying that reverting extra='forbid'
     makes the negative tests RED deterministically.
"""

from decimal import Decimal

import pytest

from aats.contracts.events import (
    DetectionTransport,
    EventTime,
    LaunchEvent,
    LaunchOutcome,
    LaunchOutcomeLabel,
    LaunchSource,
)
from aats.contracts.features import FeatureFrame, FeatureProvenance, FeatureSourceWindow


def _event_time(slot: int = 1000) -> EventTime:
    return EventTime(slot=slot, block_time_ms=1_700_000_000_000, wall_clock_ms=1_700_000_000_100)


# ---------------------------------------------------------------------------
# LaunchEvent: no truth_* fields
# ---------------------------------------------------------------------------


class TestLaunchEventNoTruthFields:
    def test_no_truth_is_rug_field(self):
        """LaunchEvent must NOT have a truth_is_rug field."""
        from aats.contracts.events import LaunchEvent
        assert not hasattr(LaunchEvent.model_fields, "truth_is_rug"), (
            "LaunchEvent must NOT have a truth_is_rug field (data-models.md §2)"
        )
        # Also verify it cannot be set
        assert "truth_is_rug" not in LaunchEvent.model_fields

    def test_no_truth_max_multiple_field(self):
        assert "truth_max_multiple" not in LaunchEvent.model_fields

    def test_no_truth_rug_detectable_field(self):
        assert "truth_rug_detectable" not in LaunchEvent.model_fields

    def test_production_launch_event_has_no_truth_fields(self):
        """A valid LaunchEvent can be constructed with no truth_* fields."""
        event = LaunchEvent(
            mint="So11111111111111111111111111111111111111112",
            venue_program_id="6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",
            source=LaunchSource.PUMPFUN,
            event_time=_event_time(),
            observation_slot=999,
            confirmation_slot=1000,
            detection_transport=DetectionTransport.GEYSER,
            sol_reserve_lamports=1_000_000_000,
            token_reserve_base=1_000_000_000_000,
            token_decimals=6,
            initial_holders=100,
            competitors=5,
            creator_wallet="Abc123",
            bundler_cluster_id=None,
            deploy_template_fingerprint=None,
            data_staleness_ms=50,
        )
        assert not hasattr(event, "truth_is_rug")
        assert not hasattr(event, "truth_max_multiple")
        assert not hasattr(event, "truth_rug_detectable")


# ---------------------------------------------------------------------------
# FeatureFrame: no truth_* or label fields (ADR-0010, data-models.md §3.3 guard 3)
# ---------------------------------------------------------------------------


def _valid_provenance() -> FeatureProvenance:
    return FeatureProvenance(
        windows=[
            FeatureSourceWindow(
                feature_name="first_k_volume_lamports",
                max_source_slot=1005,
                lineage_dataset="launch_events",
            ),
        ],
        builder_version="v0.1.0",
    )


def _valid_feature_frame_data() -> dict:
    return {
        "mint": "So11111111111111111111111111111111111111112",
        "event_time": _event_time(),
        "feature_schema_version": "1.0.0",
        "data_staleness_ms": 50,
        "first_k_slots": 5,
        "first_k_buy_pressure": Decimal("1000000000"),
        "first_k_volume_lamports": 5_000_000_000,
        "first_k_buy_count": 10,
        "first_k_sell_count": 2,
        "first_k_unique_buyers": 8,
        "lp_depth_lamports": 10_000_000_000,
        "holder_count": 200,
        "holder_concentration_top10_bps": 3000,
        "sniper_cluster_score": 0.3,
        "sell_tax_bps": 0,
        "sell_reserve_trajectory": [10_000_000_000],
        "rsi": 55.0,
        "macd": None,
        "bb_width": None,
        "smart_wallets_in": 0,
        "smart_wallet_entry_lag_slots": None,
        "tip_floor_at_decision_lamports": 200_000,
        "tip_contention_bucket": "low",
        "normalization_window": "as_of_event_time",
        "provenance": _valid_provenance(),
    }


class TestFeatureFrameNoTruthOrLabelFields:
    def test_no_truth_is_rug_field_on_feature_frame(self):
        assert "truth_is_rug" not in FeatureFrame.model_fields

    def test_no_truth_max_multiple_on_feature_frame(self):
        assert "truth_max_multiple" not in FeatureFrame.model_fields

    def test_no_label_field_on_feature_frame(self):
        assert "label" not in FeatureFrame.model_fields

    def test_no_realized_mult_decimal_on_feature_frame(self):
        assert "realized_mult_decimal" not in FeatureFrame.model_fields

    def test_no_rug_detectable_at_decision_on_feature_frame(self):
        assert "rug_detectable_at_decision" not in FeatureFrame.model_fields

    def test_valid_feature_frame_constructs_cleanly(self):
        data = _valid_feature_frame_data()
        ff = FeatureFrame(**data)
        assert not hasattr(ff, "truth_is_rug")
        assert not hasattr(ff, "label")
        assert not hasattr(ff, "realized_mult_decimal")


# ---------------------------------------------------------------------------
# LaunchOutcome: label dataset fields MUST NOT appear on FeatureFrame
# (ADR-0010, data-models.md §3.3 guard 3 — column disjointness)
# ---------------------------------------------------------------------------


class TestLabelColumnDisjointness:
    """Guard 3: label/feature column disjointness.

    FeatureFrame.model_config has extra="forbid", so Pydantic rejects any kwarg
    whose name matches a label/truth_* field at construction time.  The
    _no_label_fields() model_validator is a secondary layer catching class-level
    field-definition contamination.  These tests verify BOTH layers.
    """

    def test_label_field_in_feature_frame_rejected_at_construction(self):
        """Planting a 'label' kwarg at construction must raise ValidationError.

        This exercises the real enforcement path (extra='forbid') — not merely a
        static field-set assertion.  Previously this test only checked
        'label' not in model_fields, which gave false coverage credit because the
        runtime validator was dead code (extra='ignore' default silently dropped the
        kwarg before it could be seen).
        """
        data = _valid_feature_frame_data()
        with pytest.raises(Exception) as exc_info:
            FeatureFrame(**data, label="RUGGED")
        err = str(exc_info.value)
        assert "label" in err, (
            f"Expected 'label' in the error message but got: {err}"
        )

    def test_truth_is_rug_kwarg_rejected_at_construction(self):
        """Planting a 'truth_is_rug' kwarg at construction must raise ValidationError."""
        data = _valid_feature_frame_data()
        with pytest.raises(Exception) as exc_info:
            FeatureFrame(**data, truth_is_rug=True)
        err = str(exc_info.value)
        assert "truth_is_rug" in err, (
            f"Expected 'truth_is_rug' in the error message but got: {err}"
        )

    def test_truth_max_multiple_kwarg_rejected_at_construction(self):
        """Planting a 'truth_max_multiple' kwarg at construction must raise ValidationError."""
        data = _valid_feature_frame_data()
        with pytest.raises(Exception) as exc_info:
            FeatureFrame(**data, truth_max_multiple=5.0)
        err = str(exc_info.value)
        assert "truth_max_multiple" in err, (
            f"Expected 'truth_max_multiple' in the error message but got: {err}"
        )

    def test_label_field_absent_from_model_fields(self):
        """Static field-set guard: 'label' must not be a declared FeatureFrame field."""
        assert "label" not in FeatureFrame.model_fields, (
            "FeatureFrame must not have a 'label' field (label_column_in_feature_frame guard)"
        )

    def test_provenance_disjointness_guard_utility(self):
        """The utility function assert_feature_frame_column_disjointness fires on overlap."""
        from aats.contracts.provenance import (
            assert_feature_frame_column_disjointness,
        )
        # "mint" overlaps (common join key) — only actual data columns should overlap,
        # but the guard asserts FULL disjointness; shared keys like mint would fail.
        # In practice the harness should exclude the join key from both column sets.
        # For the guard test, we test an obvious overlap like "label".
        feature_cols2 = ["first_k_volume_lamports", "lp_depth_lamports"]
        label_cols2 = ["label", "resolution_event_time", "realized_mult_decimal"]
        # No overlap — should pass
        assert_feature_frame_column_disjointness(feature_cols2, label_cols2)

    def test_disjointness_guard_fires_on_overlap(self):
        from aats.contracts.provenance import (
            ProvenanceTaintError,
            assert_feature_frame_column_disjointness,
        )
        feature_cols = ["first_k_volume_lamports", "label", "lp_depth_lamports"]
        label_cols = ["label", "resolution_event_time"]
        with pytest.raises(ProvenanceTaintError) as exc_info:
            assert_feature_frame_column_disjointness(feature_cols, label_cols)
        assert "label_column_in_feature_frame" in str(exc_info.value)
        assert "label" in str(exc_info.value)


# ---------------------------------------------------------------------------
# LaunchOutcome: correctly lives in its own contract
# ---------------------------------------------------------------------------


class TestLaunchOutcomeContract:
    def test_launch_outcome_has_label_field(self):
        assert "label" in LaunchOutcome.model_fields

    def test_launch_outcome_construction_invariants(self):
        """resolution_event_time.slot must == event_time.slot + label_horizon_h_slots."""
        event_time = _event_time(slot=1000)
        resolution_time = EventTime(
            slot=1060,  # 1000 + 60
            block_time_ms=1_700_000_024_000,  # later
            wall_clock_ms=1_700_000_024_100,
        )
        outcome = LaunchOutcome(
            mint="So11111111111111111111111111111111111111112",
            event_time=event_time,
            label_horizon_h_slots=60,
            resolution_event_time=resolution_time,
            label=LaunchOutcomeLabel.SURVIVED,
            realized_mult_decimal="1.5",
            max_drawdown_bps=2000,
            rug_detectable_at_decision=None,
            resolution_recorded_at_ms=1_700_000_024_100,
        )
        assert outcome.label == LaunchOutcomeLabel.SURVIVED

    def test_launch_outcome_rejects_wrong_horizon_slot(self):
        event_time = _event_time(slot=1000)
        resolution_time = EventTime(
            slot=1100,  # wrong: should be 1000 + 60 = 1060
            block_time_ms=1_700_000_024_000,
            wall_clock_ms=1_700_000_024_100,
        )
        with pytest.raises(Exception) as exc_info:
            LaunchOutcome(
                mint="So11111111111111111111111111111111111111112",
                event_time=event_time,
                label_horizon_h_slots=60,
                resolution_event_time=resolution_time,
                label=LaunchOutcomeLabel.RUGGED,
                realized_mult_decimal=None,
                max_drawdown_bps=None,
                rug_detectable_at_decision=True,
                resolution_recorded_at_ms=1_700_000_024_100,
            )
        assert "1060" in str(exc_info.value) or "resolution_event_time" in str(exc_info.value)

    def test_launch_outcome_rejects_recorded_at_before_knowable(self):
        """resolution_recorded_at_ms must be >= resolution_event_time.block_time_ms."""
        event_time = _event_time(slot=1000)
        resolution_time = EventTime(
            slot=1060,
            block_time_ms=1_700_000_024_000,
            wall_clock_ms=1_700_000_024_100,
        )
        with pytest.raises(Exception) as exc_info:
            LaunchOutcome(
                mint="So11111111111111111111111111111111111111112",
                event_time=event_time,
                label_horizon_h_slots=60,
                resolution_event_time=resolution_time,
                label=LaunchOutcomeLabel.RUGGED,
                realized_mult_decimal=None,
                max_drawdown_bps=None,
                rug_detectable_at_decision=True,
                resolution_recorded_at_ms=1_700_000_010_000,  # BEFORE resolution block_time
            )
        assert "recorded_at" in str(exc_info.value).lower() or "knowable" in str(exc_info.value).lower()


# ---------------------------------------------------------------------------
# T-199a: Mutation proof — extra="forbid" is the ACTIVE enforcement mechanism
# ---------------------------------------------------------------------------


class TestLeakGuardMutationProof:
    """Prove that extra="forbid" is the live, enforced gate — not dead config.

    These tests verify that:
    a) The FeatureFrame CoreSchema currently has extra_fields_behavior='forbid'
       (not 'ignore' or 'allow') — i.e. model_rebuild completed successfully.
    b) The negative-construction tests in TestLabelColumnDisjointness exercise
       the SAME code path that would fail if extra='forbid' were removed.

    Mutation proof statement (non-executable, but verifiable by manual revert):
      If you change features.py model_config from {"extra": "forbid"} to
      {"extra": "ignore"} (or remove the key entirely), then the three tests in
      TestLabelColumnDisjointness named *_rejected_at_construction WILL FAIL
      because FeatureFrame(**data, label='RUGGED') will succeed silently
      (extra kwargs dropped) and pytest.raises(Exception) will not catch anything.
      This is deterministic across ALL PYTHONHASHSEED values.

    The fix (T-199a) makes the guard reliable across all seeds by:
      1. Removing 'from __future__ import annotations' (no more lazy forward refs).
      2. Adding model_rebuild(force=True) at module bottom (schema always complete).
    """

    def test_feature_frame_core_schema_has_extra_forbid(self):
        """The CoreSchema must have extra_fields_behavior='forbid' — not 'ignore'.

        This is the DIRECT check that extra="forbid" is in the ACTIVE CoreSchema,
        not just in model_config (which could be shadowed by incomplete schema build).
        """
        import pydantic_core  # noqa: F401 — confirms pydantic-core is available

        schema = FeatureFrame.__pydantic_core_schema__

        def _find_key(d, key):
            """Recursively find all values for a key in a nested dict/list."""
            if isinstance(d, dict):
                if key in d:
                    yield d[key]
                for v in d.values():
                    yield from _find_key(v, key)
            elif isinstance(d, list):
                for item in d:
                    yield from _find_key(item, key)

        behaviors = list(_find_key(schema, "extra_fields_behavior"))
        assert behaviors, (
            "extra_fields_behavior not found in FeatureFrame CoreSchema. "
            "This means model_rebuild() did not complete and extra='forbid' is NOT enforced. "
            "Run FeatureFrame.model_rebuild(force=True) before using this class."
        )
        for behavior in behaviors:
            assert behavior == "forbid", (
                f"FeatureFrame CoreSchema has extra_fields_behavior='{behavior}', "
                f"expected 'forbid'. The leak guard is NOT active. "
                "Ensure model_config = {'extra': 'forbid'} and model_rebuild() has run."
            )

    def test_feature_frame_pydantic_complete(self):
        """FeatureFrame must have __pydantic_complete__ = True.

        A False value means the CoreSchema is deferred/incomplete and validators
        (including extra='forbid') may not be enforced.
        """
        assert FeatureFrame.__pydantic_complete__ is True, (
            "FeatureFrame.__pydantic_complete__ is False — the model is not fully built. "
            "extra='forbid' is NOT enforced in this state. "
            "The T-199a fix (no __future__ annotations + explicit model_rebuild) "
            "should prevent this."
        )

    def test_extra_forbid_config_present(self):
        """model_config must explicitly declare extra='forbid'."""
        cfg = FeatureFrame.model_config
        assert cfg.get("extra") == "forbid", (
            f"FeatureFrame.model_config['extra'] = {cfg.get('extra')!r}. "
            "Must be 'forbid' (ADR-0010, data-models.md §3.3 guard 3, T-199a). "
            "Reverting this setting makes the negative-construction tests go RED."
        )

    def test_extra_forbid_actually_rejects_unknown_field(self):
        """Smoke test: the LIVE enforcement path works right now (not just schema inspection).

        This is the critical test that the T-199a flake broke intermittently.
        It must be GREEN on ALL PYTHONHASHSEED values.
        """
        data = _valid_feature_frame_data()
        # These are the exact patterns from TestLabelColumnDisjointness that were flaky:
        for extra_kwarg, extra_value in [
            ("label", "RUGGED"),
            ("truth_is_rug", True),
            ("truth_max_multiple", 5.0),
            ("realized_mult_decimal", "1.5"),
            ("rug_detectable_at_decision", False),
        ]:
            with pytest.raises(Exception) as exc_info:
                FeatureFrame(**data, **{extra_kwarg: extra_value})
            err = str(exc_info.value)
            assert extra_kwarg in err, (
                f"Expected '{extra_kwarg}' in error when planting extra kwarg "
                f"{extra_kwarg}={extra_value!r}, but got: {err[:200]}"
            )
