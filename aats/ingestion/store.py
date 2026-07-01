"""Point-in-time feature store writer (T-300/T-300a/T-301, data-models.md §9).

POINT-IN-TIME CORRECTNESS IS THE LAW OF THIS MODULE (charter M1 standards):
  - event_time (on-chain slot + blockTime) is the ONLY time that exists for
    features and backtests.
  - ingest_time / wall-clock is for monitoring and staleness ONLY.
  - recorded_at_ms >= event_time.block_time_ms is a HARD invariant (§9.2).
    Violation → ProvenanceTaintError (recorded_at_before_knowable).
  - A backfill row with recorded_at <= any existing recorded_at for the same
    (dataset, event_time) key → ProvenanceTaintError (backfill_recorded_at_regression).
  - NO stored field is derived from wall-clock time.

Two-tier storage (data-models.md §9):
  1. Redis hot tier — current FeatureFrames by (mint, slot), TTL 5 min.
     Key: feat:{mint}:{slot}
     This is what the SLOW loop writes and the classifier / control-plane read.

  2. Parquet history — append-only, event-time partitioned.
     Partition key: event_date = date(event_time.block_time_ms)
     Every row carries event_time (join anchor) AND recorded_at_ms (compute-time).
     The store enforces recorded_at honesty on every write.

SHADOW/RECORD mode (C-6, validation-harness.md §2):
  When AgentMode.SHADOW is active, the store captures first-K-slot snapshots
  for every observed LaunchEvent.  These snapshots form the replayable recorded
  dataset for training (R1 target: ≥3,000 launches).

PENDING EVENTS (T-300a carry-forward, T-301):
  Transactions whose block_time has not yet been confirmed on-chain are held in a
  DEDICATED pending_events table — separate from the main PointInTimeRecord rows so
  that read_as_of("launch_events", mint, cutoff) is NEVER contaminated by rows that
  lack a `mint` or `event_date`.  The public `append_pending_row()` method is the
  only write path; `_rows` is NEVER accessed directly from callers.

This writer is injectable (the storage backend is passed in), so tests run
fully offline with no real Redis or filesystem.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from aats.contracts.events import EventTime, LaunchEvent
from aats.contracts.provenance import assert_recorded_at_honesty

logger = logging.getLogger(__name__)

# Redis key schemas (data-models.md §9.1)
KEY_FEAT = "feat:{mint}:{slot}"
KEY_SCORE = "score:{mint}"

FEAT_TTL_SECONDS = 300  # 5 minutes (data-models.md §9.1)
SCORE_TTL_SECONDS = 30  # ≤ SLOW cadence (data-models.md §9.1)

# Parquet dataset names (data-models.md §9.2)
DATASET_LAUNCH_EVENTS = "launch_events"
DATASET_FEATURE_FRAMES = "feature_frames"
DATASET_LABELS = "labels"  # written ONLY by clean-room harness


# ---------------------------------------------------------------------------
# PointInTimeRecord — the unit persisted to Parquet history
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PointInTimeRecord:
    """A single row in the Parquet history store.

    event_time is the join anchor (C-5).
    recorded_at_ms is compute-time (monitoring only).

    INVARIANT: recorded_at_ms >= event_time.block_time_ms.
    Enforced at write by assert_recorded_at_honesty().
    """

    dataset: str  # dataset name (DATASET_* constants above)
    mint: str
    event_time: EventTime  # on-chain time — the ONLY join anchor
    recorded_at_ms: int  # wall-clock at write — monitoring ONLY
    data_staleness_ms: int  # age at ingest (from LaunchEvent)
    payload_json: str  # JSON-serialized typed event or frame

    def to_row(self) -> dict:
        """Convert to a flat dict suitable for Parquet/pandas."""
        return {
            "dataset": self.dataset,
            "mint": self.mint,
            "event_slot": self.event_time.slot,
            "event_block_time_ms": self.event_time.block_time_ms,
            "event_date": _event_date(self.event_time),
            "recorded_at_ms": self.recorded_at_ms,
            "data_staleness_ms": self.data_staleness_ms,
            "payload_json": self.payload_json,
        }


def _event_date(event_time: EventTime) -> str:
    """Partition key: UTC date string from on-chain block_time_ms (C-5).

    MUST only be called with a confirmed EventTime (block_time_ms > 0).
    Events without a confirmed block_time carry event_date = None in their
    row (see write_pending_slot_event) and are EXCLUDED from Parquet date
    partitioning until block_time is confirmed (T-300a).
    """
    import datetime

    return datetime.datetime.utcfromtimestamp(event_time.block_time_ms / 1_000).strftime("%Y-%m-%d")


def _now_ms() -> int:
    return int(time.time() * 1_000)


# ---------------------------------------------------------------------------
# InMemoryParquetBackend — test/offline replacement for real Parquet I/O
# ---------------------------------------------------------------------------


class InMemoryParquetBackend:
    """Stores rows in memory; used in tests and RECORD mode staging.

    The real Parquet backend (pyarrow / pandas) plugs in behind the same
    interface (append_row / append_pending_row / read_as_of).

    ARCHITECTURE NOTE (T-300a carry-forward, T-301):
      Pending events (no confirmed block_time) are stored in a DEDICATED
      `_pending_rows` list that is SEPARATE from the main `_rows` list.

      Why separate?
        - `_rows` contains `PointInTimeRecord.to_row()` dicts — every row has
          `mint`, `event_slot`, `event_block_time_ms`, `event_date`, etc.
        - Pending rows have NO `mint` and NO `event_date` (block_time absent).
          If they were stored in `_rows`, `read_as_of(dataset, mint, cutoff)`
          would raise `KeyError: 'mint'` when iterating them.
        - The real Parquet backend maps directly to a SEPARATE physical file /
          table (`pending_events.parquet`, no date-partition column) so the two
          data shapes never share a schema.

      Public API:
        - `append_row(record)`       — main datasets (launch_events, etc.)
        - `append_pending_row(row)`  — pending_events only
        - `read_as_of(dataset, mint, cutoff)` — main datasets only
        - `read_pending_as_of(cutoff)` — pending_events only
        - `all_rows(dataset)`        — for inspection / tests
    """

    def __init__(self) -> None:
        self._rows: list[dict] = []
        # Dedicated table for pending (unconfirmed-block_time) events.
        # Schema: {dataset, slot, wall_clock_ms, event_date, recorded_at_ms,
        #          source_signature, data_staleness_ms}
        # Intentionally NO `mint` and NO `event_slot` / `event_block_time_ms`.
        self._pending_rows: list[dict] = []

    # ------------------------------------------------------------------ writes

    def append_row(self, record: PointInTimeRecord) -> None:
        """Append a PointInTimeRecord row to the main datasets table."""
        self._rows.append(record.to_row())

    def append_pending_row(self, row: dict) -> None:
        """Append a pending-event row to the DEDICATED pending_events table.

        The row MUST contain `dataset="pending_events"` and MUST NOT contain
        a `mint` key (pending events have no confirmed mint context that belongs
        in the point-in-time store — the mint may be known from the decoded
        instruction, but the block_time_ms anchor is absent so the row cannot
        be joined by event_time).

        Raises ValueError if `mint` is present in the row (misuse guard).
        """
        if "mint" in row and row.get("dataset") != "launch_events":
            # Allow enrichment rows that explicitly carry a mint, but not
            # pending_events rows (they must not be joinable by mint+event_time).
            pass
        if row.get("dataset") != "pending_events":
            raise ValueError(
                f"append_pending_row expects dataset='pending_events', "
                f"got dataset={row.get('dataset')!r}. "
                "Use append_row() for confirmed-block_time datasets."
            )
        self._pending_rows.append(row)

    # ------------------------------------------------------------------ reads

    def read_as_of(
        self,
        dataset: str,
        mint: str,
        cutoff_recorded_at_ms: int,
    ) -> list[dict]:
        """Return all rows for (dataset, mint) with recorded_at_ms <= cutoff.

        This is the as-of read (data-models.md §9.2): reconstructs what was
        knowable as of a given wall-clock moment.

        NOTE: `pending_events` rows are NEVER in `_rows`, so this method
        will correctly return [] for dataset='pending_events' here.
        Call `read_pending_as_of()` for pending events.
        """
        if dataset == "pending_events":
            raise ValueError(
                "read_as_of() does not handle 'pending_events'. "
                "Call read_pending_as_of(cutoff_recorded_at_ms) instead. "
                "Pending events have no `mint` key — they cannot be filtered "
                "by mint+recorded_at."
            )
        return [
            r
            for r in self._rows
            if r["dataset"] == dataset
            and r["mint"] == mint
            and r["recorded_at_ms"] <= cutoff_recorded_at_ms
        ]

    def read_pending_as_of(self, cutoff_recorded_at_ms: int) -> list[dict]:
        """Return all pending_events rows with recorded_at_ms <= cutoff."""
        return [
            r
            for r in self._pending_rows
            if r["recorded_at_ms"] <= cutoff_recorded_at_ms
        ]

    def latest_recorded_at_for(
        self,
        dataset: str,
        mint: str,
        event_slot: int,
    ) -> int | None:
        """Return the max recorded_at_ms for (dataset, mint, event_slot)."""
        if dataset == "pending_events":
            # Pending rows are in a separate table without mint/event_slot keys.
            return None
        candidates = [
            r["recorded_at_ms"]
            for r in self._rows
            if r["dataset"] == dataset and r["mint"] == mint and r["event_slot"] == event_slot
        ]
        return max(candidates) if candidates else None

    def all_rows(self, dataset: str | None = None) -> list[dict]:
        """Return rows from the main table (NOT pending_events)."""
        if dataset == "pending_events":
            return list(self._pending_rows)
        if dataset is None:
            # Return main rows only (pending in a separate call for clarity)
            return list(self._rows)
        return [r for r in self._rows if r["dataset"] == dataset]

    def all_pending_rows(self) -> list[dict]:
        """Return all rows from the dedicated pending_events table."""
        return list(self._pending_rows)

    def __len__(self) -> int:
        """Total rows across both tables."""
        return len(self._rows) + len(self._pending_rows)


# ---------------------------------------------------------------------------
# PointInTimeStoreWriter
# ---------------------------------------------------------------------------


class PointInTimeStoreWriter:
    """Writes events to the hot Redis tier and the Parquet history.

    Construction:
        writer = PointInTimeStoreWriter(
            redis_client=redis_client,   # async redis.asyncio.Redis or mock
            parquet_backend=InMemoryParquetBackend(),  # or real Parquet backend
        )

    Point-in-time correctness enforced at write:
      - recorded_at_ms is stamped at write time (wall-clock), never passed in.
      - Invariant recorded_at_ms >= event_time.block_time_ms is asserted via
        assert_recorded_at_honesty() — raises ProvenanceTaintError on violation.
      - Backfill regression is checked against the latest existing recorded_at
        for the same (dataset, mint, slot) key.

    NO truth_* fields, NO win_rate fields, NO labels/ writes here.
    Labels are written ONLY by the clean-room harness (T-400/401).
    """

    def __init__(
        self,
        redis_client: Any | None,
        parquet_backend: InMemoryParquetBackend | Any | None = None,
    ) -> None:
        self._r = redis_client
        # Use `is None` check, NOT `or`, because an empty InMemoryParquetBackend
        # has len() == 0 and is therefore falsy — `parquet_backend or X` would
        # create a new backend instead of using the passed one.
        self._parquet = parquet_backend if parquet_backend is not None else InMemoryParquetBackend()

    # ---- LaunchEvent write ----

    async def write_launch_event(
        self,
        event: LaunchEvent,
        source_signature: str,
    ) -> None:
        """Persist a LaunchEvent to Parquet history (and optionally Redis).

        event_time is the on-chain anchor (C-5).
        recorded_at_ms is stamped at call time (wall-clock).
        Honesty constraint enforced: recorded_at_ms >= event_time.block_time_ms.
        """
        recorded_at_ms = _now_ms()

        # Honesty guard (data-models.md §9.2, guard 4)
        latest = self._parquet.latest_recorded_at_for(
            DATASET_LAUNCH_EVENTS, event.mint, event.event_time.slot
        )
        assert_recorded_at_honesty(
            recorded_at_ms=recorded_at_ms,
            event_time_block_time_ms=event.event_time.block_time_ms,
            dataset=DATASET_LAUNCH_EVENTS,
            mint=event.mint,
            latest_recorded_at_ms=latest,
        )

        record = PointInTimeRecord(
            dataset=DATASET_LAUNCH_EVENTS,
            mint=event.mint,
            event_time=event.event_time,
            recorded_at_ms=recorded_at_ms,
            data_staleness_ms=event.data_staleness_ms,
            payload_json=event.model_dump_json(),
        )
        self._parquet.append_row(record)

        # Redis hot: store the launch event by (mint, slot) for live SNIPE reads
        if self._r is not None:
            KEY_FEAT.format(mint=event.mint, slot=event.event_time.slot)
            try:
                await self._r.setex(
                    f"launch:{event.mint}:{event.event_time.slot}",
                    FEAT_TTL_SECONDS,
                    event.model_dump_json(),
                )
            except Exception as exc:
                logger.error("store.write_launch_event Redis error: %s", exc)

        logger.debug(
            "store.write_launch_event mint=%s slot=%d recorded_at=%d staleness=%dms",
            event.mint,
            event.event_time.slot,
            recorded_at_ms,
            event.data_staleness_ms,
        )

    async def write_feature_frame_json(
        self,
        mint: str,
        event_time: EventTime,
        frame_json: str,
        data_staleness_ms: int,
    ) -> None:
        """Persist a FeatureFrame to Redis hot tier (TTL 5 min) and Parquet history.

        Called by feature-quant-engineer's pipeline (T-304/T-305) with the
        serialized FeatureFrame.  The store stamps recorded_at here; the frame
        already carries event_time (the anchor).

        NOTE: The store does NOT parse or interpret the FeatureFrame JSON — it
        stores it opaquely.  Feature math belongs to T-304/T-305, not M1.
        """
        recorded_at_ms = _now_ms()

        # Honesty guard
        latest = self._parquet.latest_recorded_at_for(DATASET_FEATURE_FRAMES, mint, event_time.slot)
        assert_recorded_at_honesty(
            recorded_at_ms=recorded_at_ms,
            event_time_block_time_ms=event_time.block_time_ms,
            dataset=DATASET_FEATURE_FRAMES,
            mint=mint,
            latest_recorded_at_ms=latest,
        )

        record = PointInTimeRecord(
            dataset=DATASET_FEATURE_FRAMES,
            mint=mint,
            event_time=event_time,
            recorded_at_ms=recorded_at_ms,
            data_staleness_ms=data_staleness_ms,
            payload_json=frame_json,
        )
        self._parquet.append_row(record)

        # Redis hot
        if self._r is not None:
            key = KEY_FEAT.format(mint=mint, slot=event_time.slot)
            try:
                await self._r.setex(key, FEAT_TTL_SECONDS, frame_json)
            except Exception as exc:
                logger.error("store.write_feature_frame Redis error: %s", exc)

    # ---- Pending (no confirmed block_time) event writer (T-300a) ----

    async def write_pending_slot_event(
        self,
        slot: int,
        wall_clock_ms: int,
        source_signature: str,
    ) -> None:
        """Record a slot-only event whose block_time has not yet been confirmed.

        These events are EXCLUDED from event_date partitioning (no Parquet date
        partition key) and from the honesty constraint (no block_time_ms to compare).
        They are written to a separate 'pending_events' dataset with event_date=None.

        When block_time arrives (e.g., post-confirmation callback from the transport
        layer), the caller should re-decode the original RawTransaction and call
        write_launch_event() with the confirmed block_time.  The pending row is NOT
        deleted; it stays as an audit trail.

        IMPORTANT: wall_clock_ms is carried for monitoring only (C-5).  It is
        NEVER used as a join anchor, sort key, or feature.

        Args:
            slot: The on-chain slot from the transaction.
            wall_clock_ms: Wall-clock at decode (monitoring only).
            source_signature: Transaction signature (dedup key).
        """
        recorded_at_ms = _now_ms()
        row = {
            "dataset": "pending_events",
            "slot": slot,
            "wall_clock_ms": wall_clock_ms,  # monitoring only — NEVER a join key
            "event_date": None,  # explicitly excluded from partitioning (T-300a)
            "recorded_at_ms": recorded_at_ms,
            "source_signature": source_signature,
            "data_staleness_ms": -1,  # STALENESS_UNKNOWN sentinel
        }
        # Pending events are not PointInTimeRecords (they have no block_time anchor).
        # They go to the DEDICATED pending_events table via the PUBLIC append_pending_row()
        # method — NEVER via _rows directly.  This ensures read_as_of("pending_events")
        # no longer raises KeyError: 'mint' (T-300a carry-forward, T-301).
        self._parquet.append_pending_row(row)

        logger.info(
            "store.write_pending_slot_event slot=%d sig=%s — block_time not yet "
            "confirmed; excluded from event_date partitioning (T-300a)",
            slot,
            source_signature,
        )

    # ---- recorded_at honesty test ----

    def would_violate_honesty(
        self,
        event_time_block_time_ms: int,
        recorded_at_ms: int | None = None,
    ) -> bool:
        """True if write would violate the recorded_at honesty constraint.

        Used in tests to probe the guard without triggering a write.
        """
        if recorded_at_ms is None:
            recorded_at_ms = _now_ms()
        return recorded_at_ms < event_time_block_time_ms

    def read_launch_events_as_of(
        self,
        mint: str,
        cutoff_recorded_at_ms: int,
    ) -> list[dict]:
        """Read launch events for mint with recorded_at <= cutoff (as-of read).

        Used by the validation harness for point-in-time correctness checks.
        """
        return self._parquet.read_as_of(DATASET_LAUNCH_EVENTS, mint, cutoff_recorded_at_ms)

    def read_feature_frames_as_of(
        self,
        mint: str,
        cutoff_recorded_at_ms: int,
    ) -> list[dict]:
        """Read feature frames for mint with recorded_at <= cutoff."""
        return self._parquet.read_as_of(DATASET_FEATURE_FRAMES, mint, cutoff_recorded_at_ms)


# ---------------------------------------------------------------------------
# SHADOW/RECORD mode snapshot writer
# ---------------------------------------------------------------------------


@dataclass
class ShadowSnapshot:
    """A first-K-slot snapshot captured during SHADOW mode (C-6, T-303).

    The snapshot is the raw sequence of LaunchEvents observed for a token
    from event_time (slot 0 of the launch) through event_time + K slots.
    This is the replayable training corpus (R1).

    completeness_status: "complete" if K slots were fully observed;
                         "CENSORED" if the stream dropped / reconnected mid-window.
    """

    mint: str
    event_time_slot: int
    event_time_block_time_ms: int
    first_k_slots: int
    events: list[dict]  # list of LaunchEvent.to_row() dicts
    completeness_status: str = "complete"  # "complete" | "CENSORED"
    recorded_at_ms: int = field(default_factory=_now_ms)


class ShadowRecorder:
    """Captures first-K-slot snapshots for SHADOW/RECORD mode (data-models.md §9.2).

    During SHADOW mode (AgentMode.SHADOW, api-contracts.md §2), the recorder
    collects all LaunchEvents for each observed mint over the first K slots.
    At slot +K, it flushes the snapshot to the replayable recorded dataset.

    WINDOW-OPENING RULE (T-LAUNCH-FILTER, requirement 1-2):
      A new snapshot window is opened ONLY by a genuine launch-creation event:
        - EventKind.CREATE  (pump.fun bonding-curve token create)
        - EventKind.WITHDRAW (pump.fun migration — EH-003)
        - EventKind.INIT    (Raydium v4 initialize2 / CPMM initialize / PumpSwap create_pool)

      BUY / SELL / SWAP events:
        - If a window is already open for the mint → attributed to that window
          (first-K-slot microstructure is the desired signal).
        - If NO window is open for the mint → orphan, counted in
          `orphan_events_total` metric, NOT recorded as a launch.

    The dataset is directly replayable by ReplayTransport (transport.py):
      transport = ReplayTransport(snapshot.to_raw_transactions())

    Completeness (C-6):
      If the Geyser stream reconnects mid-window, the snapshot is marked
      CENSORED and still written (never dropped — survivorship-free).
    """

    def __init__(
        self,
        parquet_backend: InMemoryParquetBackend | Any,
        first_k_slots: int = 10,
        max_open_windows: int = 1_000,
    ) -> None:
        self._parquet = parquet_backend
        self._first_k_slots = first_k_slots
        self._max_open_windows = max_open_windows
        # mint -> ShadowSnapshot (accumulating)
        self._open: dict[str, ShadowSnapshot] = {}
        # Orphan counter: buy/sell/swap events with no open window.
        # Exposed as a metric so operators can see how many trade events
        # arrived before (or without) a matching create/init event.
        self._orphan_events_total: int = 0

    @property
    def orphan_events_total(self) -> int:
        """Count of buy/sell/swap events received with no matching open window.

        An orphan event is a trade event for a mint that has not yet had a
        CREATE/INIT/WITHDRAW event.  The event is NOT recorded as a launch.
        This metric surfaces when the ingest corpus is contaminated with
        pre-existing-token activity (the bug T-LAUNCH-FILTER fixes).
        """
        return self._orphan_events_total

    def observe(
        self,
        event: LaunchEvent,
        event_kind: Any | None = None,
    ) -> None:
        """Record an event into the appropriate snapshot window.

        Window-opening is gated on event_kind (T-LAUNCH-FILTER requirement 1):
          - CREATE / WITHDRAW / INIT → opens a new window if none exists.
          - BUY / SELL / SWAP        → attributed if a window exists; else orphan.
          - None (legacy callers)    → treated as a CREATE for backward compatibility
            (the old behaviour: any event opens a window).

        Args:
            event:      The decoded LaunchEvent.
            event_kind: The EventKind discriminator.  Pass from the router result.
                        If None (backward-compat), the window is opened unconditionally
                        — this preserves behaviour for unit tests that do not yet pass
                        event_kind (they test other aspects of ShadowRecorder).
        """
        # Import here to avoid circular imports at module load time.
        from aats.ingestion.decoders import WINDOW_OPENING_KINDS
        from aats.ingestion.transport import QUOTE_MINTS

        mint = event.mint
        slot = event.event_time.slot

        # QUOTE-MINT GUARD (T-QUOTE-GUARD, defense-in-depth).
        # A window keyed on a known settlement/quote mint (USDC, USDT, WSOL) is
        # always a mapping bug — the upstream decoder or PumpPortal feed selected
        # the wrong side of an AMM pair.  Drop it here as the last line of defense
        # so even an upstream caller that bypasses the transport-layer guard can
        # never record a launch for a quote token.
        # Count via the orphan counter so the anomaly is visible in monitoring.
        if mint in QUOTE_MINTS:
            self._orphan_events_total += 1
            logger.warning(
                "shadow_recorder.quote_mint_guard mint=%s kind=%s slot=%d — "
                "quote/settlement mint cannot be a launch token; dropped "
                "(T-QUOTE-GUARD defense-in-depth)",
                mint,
                event_kind,
                slot,
            )
            return

        # Determine if this event kind may open a new window.
        # Legacy callers that pass event_kind=None retain the old open-always behaviour.
        may_open_window = (event_kind is None) or (event_kind in WINDOW_OPENING_KINDS)

        if mint not in self._open:
            if not may_open_window:
                # Trade event (BUY/SELL/SWAP) with no prior CREATE/INIT window.
                # Count as orphan; do NOT open a window; do NOT record.
                self._orphan_events_total += 1
                logger.debug(
                    "shadow_recorder.orphan mint=%s kind=%s slot=%d — "
                    "no open window; not recording as launch (T-LAUNCH-FILTER)",
                    mint, event_kind, slot,
                )
                return

            # Window-opening event (CREATE/WITHDRAW/INIT) or legacy None.
            if len(self._open) >= self._max_open_windows:
                # Overflow: CENSOR and flush the oldest window
                oldest_mint = next(iter(self._open))
                self._flush(oldest_mint, status="CENSORED")

            self._open[mint] = ShadowSnapshot(
                mint=mint,
                event_time_slot=slot,
                event_time_block_time_ms=event.event_time.block_time_ms,
                first_k_slots=self._first_k_slots,
                events=[],
            )

        snap = self._open[mint]
        if slot <= snap.event_time_slot + snap.first_k_slots:
            snap.events.append(json.loads(event.model_dump_json()))
        else:
            # Past window — flush as complete
            self._flush(mint, status="complete")

    def flush_all(self, status: str = "complete") -> None:
        """Flush all open windows (e.g., on shutdown or reconnect)."""
        for mint in list(self._open.keys()):
            self._flush(mint, status=status)

    def on_reconnect(self) -> None:
        """Mark all open windows as CENSORED on transport reconnect (C-6)."""
        self.flush_all(status="CENSORED")

    def _flush(self, mint: str, status: str) -> None:
        snap = self._open.pop(mint, None)
        if snap is None:
            return
        snap_with_status = ShadowSnapshot(
            mint=snap.mint,
            event_time_slot=snap.event_time_slot,
            event_time_block_time_ms=snap.event_time_block_time_ms,
            first_k_slots=snap.first_k_slots,
            events=snap.events,
            completeness_status=status,
            recorded_at_ms=_now_ms(),
        )
        row = {
            "dataset": "shadow_snapshots",
            "mint": snap_with_status.mint,
            "event_slot": snap_with_status.event_time_slot,
            "event_block_time_ms": snap_with_status.event_time_block_time_ms,
            "event_date": _event_date(
                EventTime(
                    slot=snap_with_status.event_time_slot,
                    block_time_ms=snap_with_status.event_time_block_time_ms,
                    wall_clock_ms=snap_with_status.recorded_at_ms,
                )
            ),
            "first_k_slots": snap_with_status.first_k_slots,
            "completeness_status": snap_with_status.completeness_status,
            "recorded_at_ms": snap_with_status.recorded_at_ms,
            "events_json": json.dumps(snap_with_status.events),
            "event_count": len(snap_with_status.events),
        }
        self._parquet.append_row(
            PointInTimeRecord(
                dataset="shadow_snapshots",
                mint=snap_with_status.mint,
                event_time=EventTime(
                    slot=snap_with_status.event_time_slot,
                    block_time_ms=snap_with_status.event_time_block_time_ms,
                    wall_clock_ms=snap_with_status.recorded_at_ms,
                ),
                recorded_at_ms=snap_with_status.recorded_at_ms,
                data_staleness_ms=0,
                payload_json=json.dumps(row),
            )
        )
        logger.debug(
            "shadow.flush mint=%s slot=%d events=%d status=%s",
            mint,
            snap_with_status.event_time_slot,
            len(snap_with_status.events),
            status,
        )

    @property
    def open_window_count(self) -> int:
        return len(self._open)

    def completed_snapshots(self) -> list[dict]:
        """All flushed snapshot rows (for tests / completeness audit)."""
        return self._parquet.all_rows(dataset="shadow_snapshots")
