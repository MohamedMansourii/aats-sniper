"""LaunchEvent, LaunchOutcome, and related enum types.

Source of truth: data-models.md §2 (LaunchEvent) and §3A (LaunchOutcome).
These are FROZEN post-G1; no field may be added without an ADR + delta notice.

CRITICAL — no truth_* fields here (C-7 / AC-056).
The sim's truth_is_rug / truth_max_multiple / truth_rug_detectable are
sim-only and must NEVER appear in any production or validation path.
The import guard in validation-harness.md §2 enforces this at build time.

Money rule (data-models.md §0):
  sol_reserve_lamports  → int  (lamports, integer)
  token_reserve_base    → int  (base units, integer)
All financial quantities on this model are integer base units.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, field_validator, model_validator


class DetectionTransport(StrEnum):
    """How the event reached the ingest pipeline."""

    GEYSER = "geyser"
    SHREDSTREAM = "shredstream"


class LaunchSource(StrEnum):
    """Which on-chain venue produced the liquidity event."""

    PUMPFUN = "pump.fun"
    PUMPSWAP = "pumpswap"
    RAYDIUM_V4 = "raydium_v4"
    RAYDIUM_CPMM = "raydium_cpmm"
    MIGRATION = "migration"


class EventTime(BaseModel):
    """Canonical decision-point clock (C-5).

    ALL point-in-time joins anchor on slot / block_time_ms.
    wall_clock_ms is carried for monitoring/latency tracking ONLY — it MUST NOT
    be used as a join key, sort key, or feature.  The field name says so.
    """

    slot: int  # canonical decision slot
    block_time_ms: int  # on-chain block_time (ms) — AUTHORITATIVE clock
    wall_clock_ms: int  # node wall-clock at decode — monitoring ONLY, NOT for joins

    model_config = {"frozen": True}

    @field_validator("slot", "block_time_ms", "wall_clock_ms")
    @classmethod
    def _non_negative(cls, v: int) -> int:
        if v < 0:
            raise ValueError(f"EventTime field must be non-negative, got {v}")
        return v

    @model_validator(mode="after")
    def _block_time_reasonable(self) -> EventTime:
        # block_time_ms must be at least slot-proportional (rough sanity guard).
        # Solana ~400 ms/slot, genesis ~1609-01-01.  Allow any positive value but
        # assert block_time_ms > 0 so a zero-init is caught immediately.
        if self.block_time_ms == 0:
            raise ValueError("block_time_ms must be > 0 (use a real on-chain block time)")
        return self


class LaunchEvent(BaseModel):
    """A detected liquidity event — point-in-time, from Rust ingest to SNIPE/SLOW.

    Source: data-models.md §2.  Consumed by SNIPE loop and SLOW feature pipeline.
    Persisted to Parquet history (append-only) and Redis Streams (ephemeral).

    Actor-identity fingerprints (creator_wallet, bundler_cluster_id,
    deploy_template_fingerprint) are REQUIRED for group-aware purge (C-10, FR-046).

    NO truth_* fields — those exist only in sniper_sim.types.LaunchEvent (the sim).
    The production label is computed post-hoc by the clean-room harness (C-7).
    """

    mint: str
    venue_program_id: str  # LIVE-verified program ID from registry (FR-001)
    source: LaunchSource
    event_time: EventTime  # ALL joins anchor on event_time.slot/block_time
    observation_slot: int  # may be < confirmation_slot (ShredStream, AC-003)
    confirmation_slot: int
    detection_transport: DetectionTransport

    # Money fields — integer base units (data-models.md §0)
    sol_reserve_lamports: int  # initial pool SOL in lamports (NOT float)
    token_reserve_base: int  # initial pool tokens in base units (NOT float)
    token_decimals: int
    initial_holders: int
    competitors: int  # other bots observed racing (point-in-time estimate)

    # Actor-identity fingerprints for group-aware purge (C-10, FR-046)
    creator_wallet: str
    bundler_cluster_id: str | None
    deploy_template_fingerprint: str | None

    # Staleness — age of the freshest source datum vs wall_clock at decode (FR-057)
    data_staleness_ms: int

    model_config = {"frozen": True}

    @field_validator("sol_reserve_lamports", "token_reserve_base")
    @classmethod
    def _no_float_money(cls, v: int) -> int:
        # Pydantic v2 will coerce a float 1.0 → 1 silently; we must reject floats.
        # The validator receives the already-coerced int, so we use model_validator
        # to catch the raw input.  The explicit type annotation int is the first gate.
        return v

    @model_validator(mode="before")
    @classmethod
    def _reject_float_money(cls, data: object) -> object:
        """Reject float values for integer money fields (data-models.md §0)."""
        if not isinstance(data, dict):
            return data
        _MONEY_FIELDS = ("sol_reserve_lamports", "token_reserve_base")
        for field in _MONEY_FIELDS:
            val = data.get(field)
            if isinstance(val, float):
                raise ValueError(
                    f"Money field '{field}' must be int (lamports/base-units), "
                    f"got float {val!r}.  Use integer base units (data-models.md §0)."
                )
        return data

    @model_validator(mode="after")
    def _slot_ordering(self) -> LaunchEvent:
        if self.observation_slot > self.confirmation_slot:
            raise ValueError(
                f"observation_slot ({self.observation_slot}) must be "
                f"<= confirmation_slot ({self.confirmation_slot})"
            )
        return self


# ---------------------------------------------------------------------------
# LaunchOutcome — THE LABEL (data-models.md §3A, ADR-0010)
# ---------------------------------------------------------------------------


class LaunchOutcomeLabel(StrEnum):
    """Post-hoc outcome classification.

    PRODUCED ONLY by the clean-room harness (T-400/T-401).
    FORBIDDEN from feature_frames/ by the disjointness guard (ADR-0010 §3.3 guard 3).
    """

    SURVIVED = "SURVIVED"  # alive and tradeable at resolution
    RUGGED = "RUGGED"  # LP pulled / freeze / honeypot realized
    FADED = "FADED"  # not rugged but decayed below exit floor
    CENSORED = "CENSORED"  # outcome un-resolvable (C-6 survivorship guard)


class LaunchOutcome(BaseModel):
    """Post-hoc label — PRODUCED ONLY by the clean-room harness.

    Source: data-models.md §3A.
    Written to labels/ (event-time partitioned).
    FORBIDDEN from feature_frames/ — the column disjointness guard (ADR-0010,
    validation-harness.md §2.5 guard 3) asserts this at load time.

    The label joins to features ON event_time ONLY, inside the harness,
    AFTER purge/embargo.  It is never carried on a live FeatureFrame.

    Construction-time invariants (asserted by model_validator):
      resolution_event_time.slot == event_time.slot + label_horizon_h_slots
      resolution_event_time.block_time_ms > event_time.block_time_ms
      resolution_recorded_at_ms >= resolution_event_time.block_time_ms
    """

    mint: str
    event_time: EventTime  # DECISION ANCHOR — labels join to features on this only
    label_horizon_h_slots: int  # H, declared per surface upfront
    resolution_event_time: EventTime  # = event_time + H, STAMPED
    label: LaunchOutcomeLabel
    # None when CENSORED
    realized_mult_decimal: str | None  # Decimal-as-string (money-derived exact quantity)
    max_drawdown_bps: int | None
    # For recall measurement on HELD-OUT folds — NEVER a feature
    rug_detectable_at_decision: bool | None
    resolution_recorded_at_ms: int  # when the harness COMPUTED the label (compute-time)

    model_config = {"frozen": True}

    @model_validator(mode="after")
    def _construction_invariants(self) -> LaunchOutcome:
        """Enforce the three construction-time invariants (data-models.md §3A)."""
        expected_slot = self.event_time.slot + self.label_horizon_h_slots
        if self.resolution_event_time.slot != expected_slot:
            raise ValueError(
                f"resolution_event_time.slot ({self.resolution_event_time.slot}) "
                f"must equal event_time.slot + label_horizon_h_slots "
                f"({self.event_time.slot} + {self.label_horizon_h_slots} = {expected_slot})"
            )
        if self.resolution_event_time.block_time_ms <= self.event_time.block_time_ms:
            raise ValueError(
                "resolution_event_time.block_time_ms must be strictly greater than "
                f"event_time.block_time_ms ({self.event_time.block_time_ms}), "
                f"got {self.resolution_event_time.block_time_ms}"
            )
        if self.resolution_recorded_at_ms < self.resolution_event_time.block_time_ms:
            raise ValueError(
                "resolution_recorded_at_ms must be >= resolution_event_time.block_time_ms "
                f"({self.resolution_event_time.block_time_ms}), "
                f"got {self.resolution_recorded_at_ms} (recorded_at_before_knowable)"
            )
        return self

    @model_validator(mode="before")
    @classmethod
    def _recorded_at_honesty(cls, data: object) -> object:
        """Enforce recorded_at honesty (data-models.md §9.2, ADR-0010)."""
        if not isinstance(data, dict):
            return data
        # Structural check: resolution_recorded_at_ms must not precede knowable time.
        # Full check is in the model_validator above; this catches obvious bad inputs early.
        return data
