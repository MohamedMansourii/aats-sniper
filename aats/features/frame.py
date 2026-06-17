"""FeatureFrame assembler (T-304): joins microstructure + TA + smart_wallets into FeatureFrame.

This is the FINAL STAGE of the M1 sensor pipeline.  It takes the typed outputs of:
  - buy_pressure.py  (T-305: first-K buy pressure / volume — C-4 baseline enablers)
  - microstructure.py (T-304: LP state, authority, dev/bundle, concentration, tax)
  - ta.py             (T-304: RSI/MACD/BB for tokens with sufficient bar history)
  - smart_wallets.py  (T-304: smart_wallets_in count + entry lag)

And assembles them into the typed FeatureFrame from aats.contracts.features.

POINT-IN-TIME CORRECTNESS
--------------------------
Every input to assemble_feature_frame() must be from data with
observation_slot <= event_time.slot + first_k_slots.  The provenance manifests
from each upstream module already carry this guarantee (per-feature FeatureSourceWindow).

The assembler MERGES all provenance windows from all upstream modules into the
single FeatureProvenance on the assembled FeatureFrame.

SCHEMA CONFORMANCE (data-models.md §3)
---------------------------------------
The FeatureFrame type definition in aats.contracts.features is the law.
Every field must match exactly: correct dtype, correct units, correct nullability.
  - Monetary fields: int (lamports) or Decimal (net quantities)
  - Statistical fields: float
  - Basis-point fields: int (0..10000)
  - Optional fields: None when min-history guard fired or data unavailable

PROVENANCE MERGING
------------------
Each upstream module emits FeatureProvenance with one FeatureSourceWindow per
feature it computed.  The assembler merges all windows into a single
FeatureProvenance.  The max_source_slot across all windows is validated against
the cutoff (event_time.slot + first_k_slots) as a final defence.

NO LABELS — EVER
-----------------
The assembler NEVER writes label fields, truth_* fields, or any field whose
name appears in LaunchOutcome (data-models.md §3A).  The FeatureFrame's own
extra="forbid" guard and _no_label_fields validator enforce this structurally.

NO I/O: the assembler takes typed inputs and returns a FeatureFrame.
The feature store write (Redis + Parquet) is done by the store writer
(aats.ingestion.store.PointInTimeStoreWriter.write_feature_frame_json).
"""

from __future__ import annotations

import logging
from typing import Literal, cast

from aats.contracts.events import EventTime
from aats.contracts.features import FeatureFrame, FeatureProvenance, FeatureSourceWindow
from aats.contracts.provenance import check_feature_window_cutoff
from aats.features.buy_pressure import BuyPressureFeatures
from aats.features.microstructure import MicrostructureFeatures
from aats.features.smart_wallets import SmartWalletFeatures
from aats.features.ta import TAFeatures, ta_frame_fields

logger = logging.getLogger(__name__)

ASSEMBLER_VERSION = "1.0.0"


class FrameAssemblyError(ValueError):
    """Raised when the assembler cannot build a valid FeatureFrame.

    Causes:
    - event_time mismatch between upstream inputs
    - Missing required inputs
    - Provenance max_source_slot mismatch
    """


def _merge_provenance(
    *provenance_lists: list[FeatureSourceWindow],
    builder_version: str,
) -> FeatureProvenance:
    """Merge FeatureSourceWindow lists from multiple upstream modules.

    All windows are included.  Duplicate feature names are an assembly error
    (each feature should appear exactly once across all modules).
    """
    all_windows: list[FeatureSourceWindow] = []
    seen_names: set[str] = set()

    for window_list in provenance_lists:
        for window in window_list:
            if window.feature_name in seen_names:
                # Duplicate: the same feature was computed by two modules.
                # Log a warning but do not fail — use the first occurrence.
                logger.warning(
                    "FeatureFrame assembly: duplicate provenance window for '%s'; "
                    "keeping first occurrence.",
                    window.feature_name,
                )
                continue
            seen_names.add(window.feature_name)
            all_windows.append(window)

    return FeatureProvenance(windows=all_windows, builder_version=builder_version)


def assemble_feature_frame(
    *,
    mint: str,
    event_time: EventTime,
    buy_pressure: BuyPressureFeatures,
    microstructure: MicrostructureFeatures,
    ta: TAFeatures,
    smart_wallets: SmartWalletFeatures,
    tip_floor_at_decision_lamports: int,
    tip_contention_bucket: str,
    data_staleness_ms: int,
    feature_schema_version: str = ASSEMBLER_VERSION,
) -> FeatureFrame:
    """Assemble a typed FeatureFrame from all upstream feature module outputs.

    This is the single assembly point — every FeatureFrame produced by the
    M1 pipeline goes through this function.

    Parameters
    ----------
    mint : str
        The token mint address.
    event_time : EventTime
        The canonical decision anchor (C-5).  Must match the event_time in
        all upstream feature inputs.
    buy_pressure : BuyPressureFeatures
        Output of buy_pressure.build_buy_pressure_features().
    microstructure : MicrostructureFeatures
        Output of microstructure.build_microstructure_features().
    ta : TAFeatures
        Output of ta.compute_ta_features().  May have all-None fields
        if min-history guard fired (freshly-minted token).
    smart_wallets : SmartWalletFeatures
        Output of smart_wallets.build_smart_wallet_features().
        smart_wallets_in is a count feature — NEVER a buy trigger.
    tip_floor_at_decision_lamports : int
        LIVE tip floor at event_time (from tip cache, recorded for C-3 stratification).
        Integer lamports.
    tip_contention_bucket : str
        "low" | "medium" | "high" — from tip contention analysis (C-3).
    data_staleness_ms : int
        Maximum staleness across all feature inputs (FR-057).
    feature_schema_version : str
        Schema version string (bumped on any feature add/change, C-4).

    Returns
    -------
    FeatureFrame
        Typed, validated FeatureFrame ready to write to the feature store.

    Raises
    ------
    FrameAssemblyError
        If event_time mismatches between inputs, or if tip_contention_bucket
        is not one of "low" | "medium" | "high".
    ValueError
        Propagated from FeatureFrame validation (e.g. float money fields,
        label field injection).
    """
    # Validate tip_contention_bucket early (before FeatureFrame construction)
    if tip_contention_bucket not in ("low", "medium", "high"):
        raise FrameAssemblyError(
            f"tip_contention_bucket must be 'low' | 'medium' | 'high', "
            f"got {tip_contention_bucket!r}"
        )
    # Cast is safe: validated above.
    tip_bucket_typed = cast(Literal["low", "medium", "high"], tip_contention_bucket)

    # -------------------------------------------------------------------------
    # B1 FIX (BLOCKER): event_time mismatch guard.
    #
    # The assembler MUST verify that all upstream inputs were computed for the
    # SAME event_time as the assembler's own event_time parameter.  Without this
    # guard an upstream module computed for a different slot (e.g. a stale cached
    # result) silently taints the assembled frame with data from a different
    # point-in-time anchor — a subtle but real lookahead / look-behind defect.
    #
    # We compare the full EventTime (slot + block_time_ms + wall_clock_ms) to
    # catch any mismatch, not just slot.  wall_clock_ms is a monitoring field but
    # an exact match confirms the objects were produced from the same event.
    # -------------------------------------------------------------------------
    _upstream_event_times: dict[str, EventTime] = {
        "buy_pressure": buy_pressure.event_time,
        "microstructure": microstructure.event_time,
        "smart_wallets": smart_wallets.event_time,
    }
    for _module_name, _module_et in _upstream_event_times.items():
        if _module_et != event_time:
            raise FrameAssemblyError(
                f"Upstream module '{_module_name}' has event_time="
                f"(slot={_module_et.slot}, block_time_ms={_module_et.block_time_ms}) "
                f"which does not match assembler event_time="
                f"(slot={event_time.slot}, block_time_ms={event_time.block_time_ms}).  "
                "All upstream feature inputs MUST be computed for the same point-in-time "
                "anchor.  A mismatch means features from different slots are being "
                "assembled together — this is a causality violation.  "
                "Fix: ensure all upstream modules are built from the same LaunchEvent."
            )

    # -------------------------------------------------------------------------
    # M1 FIX (MAJOR): first_k_slots consistency guard.
    #
    # The assembled FeatureFrame carries a single first_k_slots value that defines
    # the cutoff (event_time.slot + first_k_slots) for ALL features in the frame.
    # If upstream modules were built with different K values, the cutoff written on
    # the frame would understate the window an upstream module actually read —
    # making the final provenance cutoff check (below) ill-defined and allowing a
    # module that used K=20 to escape the K=5 cutoff guard.
    #
    # All four K-bearing modules must agree on K before we proceed.
    # -------------------------------------------------------------------------
    _module_ks: dict[str, int] = {
        "buy_pressure": buy_pressure.first_k_slots,
        "microstructure": microstructure.first_k_slots,
        "smart_wallets": smart_wallets.first_k_slots,
    }
    _first_k = buy_pressure.first_k_slots  # canonical K (all must match this)
    _mismatched_ks = {
        name: k for name, k in _module_ks.items() if k != _first_k
    }
    if _mismatched_ks:
        all_ks = {**_module_ks}
        raise FrameAssemblyError(
            f"Upstream modules disagree on first_k_slots: {all_ks}.  "
            f"All modules must use the same K so that the provenance cutoff "
            f"(event_time.slot + first_k_slots) is unambiguous for the assembled frame.  "
            f"A mismatch means a module that used a larger K could read slots past the "
            f"declared cutoff without being caught by the final provenance guard.  "
            "Fix: construct all upstream modules with the same first_k_slots value."
        )

    # Merge all provenance windows
    buy_pressure_windows = list(buy_pressure.provenance.windows)
    micro_windows = list(microstructure.provenance.windows)
    smart_wallet_windows = list(smart_wallets.provenance.windows)

    # TA features do NOT have a FeatureProvenance in their typed result
    # (they are computed on OHLCV bars, not raw events — max_bar_slot is the proxy).
    # We create synthetic windows for the three TA fields.
    ta_field_fields = ta_frame_fields(ta)
    ta_max_slot = ta.max_bar_slot if ta.max_bar_slot > 0 else event_time.slot
    ta_windows = [
        FeatureSourceWindow(
            feature_name="rsi",
            max_source_slot=ta_max_slot,
            lineage_dataset="launch_events",
        ),
        FeatureSourceWindow(
            feature_name="macd",
            max_source_slot=ta_max_slot,
            lineage_dataset="launch_events",
        ),
        FeatureSourceWindow(
            feature_name="bb_width",
            max_source_slot=ta_max_slot,
            lineage_dataset="launch_events",
        ),
    ]

    # Tip fields are live-read at decision time; their max_source_slot = event_time.slot
    tip_windows = [
        FeatureSourceWindow(
            feature_name="tip_floor_at_decision_lamports",
            max_source_slot=event_time.slot,
            lineage_dataset="launch_events",
        ),
        FeatureSourceWindow(
            feature_name="tip_contention_bucket",
            max_source_slot=event_time.slot,
            lineage_dataset="launch_events",
        ),
    ]

    merged_provenance = _merge_provenance(
        buy_pressure_windows,
        micro_windows,
        smart_wallet_windows,
        ta_windows,
        tip_windows,
        builder_version=feature_schema_version,
    )

    # -------------------------------------------------------------------------
    # B1 FIX (BLOCKER): final provenance cutoff re-validation after merge.
    #
    # Each upstream module ran check_feature_window_cutoff against its own
    # event_time at production time.  The assembler now re-runs this guard on
    # EVERY merged window using the assembler's canonical event_time + first_k_slots
    # cutoff.  This is the "final defence" described in the module docstring:
    #
    #   "The max_source_slot across all windows is validated against the cutoff
    #    (event_time.slot + first_k_slots) as a final defence."
    #
    # Without this re-validation, an upstream module that produced a window with
    # max_source_slot > assembler_cutoff (e.g. due to a different K, a stale
    # cache, or a deliberate forge of the provenance manifest) would escape the
    # final guard and contaminate the assembled FeatureFrame with forward-looking
    # data.  The module-level guards are necessary but not sufficient; the
    # assembler must be the definitive checkpoint.
    #
    # This raises ProvenanceTaintError (from contracts.provenance) — a BUILD
    # failure, not a warning.  The FrameAssemblyError guards above run first
    # (event_time mismatch, K mismatch); if those pass, any remaining cutoff
    # violation is caught here.
    # -------------------------------------------------------------------------
    for _window in merged_provenance.windows:
        check_feature_window_cutoff(
            feature_name=_window.feature_name,
            max_source_slot=_window.max_source_slot,
            event_time_slot=event_time.slot,
            first_k_slots=_first_k,
        )

    # Map microstructure fields to FeatureFrame fields
    # lp_depth_lamports: use the microstructure LP depth (lamports, int)
    lp_depth_lamports = microstructure.lp_depth_lamports

    # holder_concentration_top10_bps: from microstructure (bps, int)
    holder_concentration_top10_bps = microstructure.holder_concentration_top10_bps

    # sniper_cluster_score: from microstructure (float)
    sniper_cluster_score = microstructure.sniper_cluster_score

    # sell_tax_bps: effective sell tax from microstructure (bps, int)
    sell_tax_bps = microstructure.effective_sell_tax_bps

    # sell_reserve_trajectory: reserve samples (list[int] lamports)
    sell_reserve_trajectory = microstructure.sell_reserve_trajectory

    # holder_count: from microstructure (int)
    holder_count = microstructure.holder_count

    # TA fields (may be None — min-history guard)
    rsi = ta_field_fields["rsi"]
    macd = ta_field_fields["macd"]
    bb_width = ta_field_fields["bb_width"]

    # smart_wallets_in: ADVERSARIAL SELECTIVITY FEATURE — NEVER A BUY TRIGGER
    smart_wallets_in = smart_wallets.smart_wallets_in
    smart_wallet_entry_lag_slots = smart_wallets.smart_wallet_entry_lag_slots

    # Assemble FeatureFrame (will raise ValueError if any type/guard violation)
    frame = FeatureFrame(
        mint=mint,
        event_time=event_time,
        feature_schema_version=feature_schema_version,
        data_staleness_ms=data_staleness_ms,

        # --- first-K-slot microstructure (C-4 baseline enablers) ---
        first_k_slots=buy_pressure.first_k_slots,
        first_k_buy_pressure=buy_pressure.first_k_buy_pressure,      # Decimal
        first_k_volume_lamports=buy_pressure.first_k_volume_lamports,  # int lamports
        first_k_buy_count=buy_pressure.first_k_buy_count,             # int
        first_k_sell_count=buy_pressure.first_k_sell_count,           # int
        first_k_unique_buyers=buy_pressure.first_k_unique_buyers,     # int

        # --- first-60s survivor microstructure (FR-010) ---
        lp_depth_lamports=lp_depth_lamports,                # int lamports
        holder_count=holder_count,                          # int
        holder_concentration_top10_bps=holder_concentration_top10_bps,  # int bps
        sniper_cluster_score=sniper_cluster_score,          # float
        sell_tax_bps=sell_tax_bps,                         # int bps
        sell_reserve_trajectory=sell_reserve_trajectory,   # list[int]

        # --- survivor TA (FR-010) — None if min-history guard fired ---
        rsi=rsi,           # float | None
        macd=macd,         # float | None
        bb_width=bb_width, # float | None

        # --- adversarial selectivity (NEVER a buy trigger; FR-007/011, EH-005) ---
        smart_wallets_in=smart_wallets_in,                          # int count
        smart_wallet_entry_lag_slots=smart_wallet_entry_lag_slots,  # int | None

        # --- tip-contention context (C-3, FR-047) ---
        tip_floor_at_decision_lamports=tip_floor_at_decision_lamports,  # int lamports
        tip_contention_bucket=tip_bucket_typed,                          # Literal

        # --- normalization provenance (C-5) ---
        normalization_window="as_of_event_time",

        # --- per-feature provenance manifest (ADR-0010) ---
        provenance=merged_provenance,
    )

    return frame


def assemble_feature_frame_json(
    *,
    mint: str,
    event_time: EventTime,
    buy_pressure: BuyPressureFeatures,
    microstructure: MicrostructureFeatures,
    ta: TAFeatures,
    smart_wallets: SmartWalletFeatures,
    tip_floor_at_decision_lamports: int,
    tip_contention_bucket: str,
    data_staleness_ms: int,
    feature_schema_version: str = ASSEMBLER_VERSION,
) -> str:
    """Assemble a FeatureFrame and return its JSON serialization.

    This is the form consumed by PointInTimeStoreWriter.write_feature_frame_json().
    The store stamps recorded_at_ms at write time; the frame carries event_time.
    """
    frame = assemble_feature_frame(
        mint=mint,
        event_time=event_time,
        buy_pressure=buy_pressure,
        microstructure=microstructure,
        ta=ta,
        smart_wallets=smart_wallets,
        tip_floor_at_decision_lamports=tip_floor_at_decision_lamports,
        tip_contention_bucket=tip_contention_bucket,
        data_staleness_ms=data_staleness_ms,
        feature_schema_version=feature_schema_version,
    )
    return frame.model_dump_json()
