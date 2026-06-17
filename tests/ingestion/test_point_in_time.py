"""Point-in-time store correctness tests (self-check item 4).

Tests:
  1. Store is event-time correct (recorded_at >= event_time.block_time_ms).
  2. Rejects future-dated recorded_at (store cannot backdate a record).
  3. Rejects backfill recorded_at regression.
  4. Out-of-order + duplicated event replay produces identical store state.
  5. No stored field is derived from wall-clock time (join anchor = event_time only).
  6. ShadowRecorder captures first-K-slot snapshots correctly.
  7. CENSORED completeness status on reconnect (C-6).
"""

import asyncio
import json
import time

import pytest

from aats.contracts.events import DetectionTransport, EventTime, LaunchEvent, LaunchSource
from aats.contracts.provenance import ProvenanceTaintError
from aats.ingestion.store import (
    InMemoryParquetBackend,
    PointInTimeStoreWriter,
    ShadowRecorder,
    _now_ms,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_event(
    mint: str = "TestMint111111111111111111111111111111111",
    slot: int = 300_000_000,
    block_time_ms: int = 1_718_700_000_000,
    staleness_ms: int = 10,
) -> LaunchEvent:
    return LaunchEvent(
        mint=mint,
        venue_program_id="6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",
        source=LaunchSource.PUMPFUN,
        event_time=EventTime(
            slot=slot,
            block_time_ms=block_time_ms,
            wall_clock_ms=block_time_ms + staleness_ms,
        ),
        observation_slot=slot,
        confirmation_slot=slot,
        detection_transport=DetectionTransport.GEYSER,
        sol_reserve_lamports=5_000_000_000,
        token_reserve_base=1_000_000_000_000,
        token_decimals=6,
        initial_holders=0,
        competitors=0,
        creator_wallet="Creator111111111111111111111111111111111111",
        bundler_cluster_id=None,
        deploy_template_fingerprint=None,
        data_staleness_ms=staleness_ms,
    )


def _make_store(redis_client=None):
    backend = InMemoryParquetBackend()
    writer = PointInTimeStoreWriter(redis_client=redis_client, parquet_backend=backend)
    return writer, backend


# ---------------------------------------------------------------------------
# Test 1: Store is event-time correct
# ---------------------------------------------------------------------------

class TestEventTimeCorrectness:
    def test_write_stores_event_time_slot(self):
        writer, backend = _make_store()
        ev = _make_event(slot=300_000_050, block_time_ms=1_718_700_050_000)
        asyncio.run(
            writer.write_launch_event(ev, "sig_aaa")
        )
        rows = backend.all_rows("launch_events")
        assert len(rows) == 1
        assert rows[0]["event_slot"] == 300_000_050

    def test_write_stores_event_block_time_ms(self):
        writer, backend = _make_store()
        ev = _make_event(block_time_ms=1_718_700_050_000)
        asyncio.run(
            writer.write_launch_event(ev, "sig_bbb")
        )
        rows = backend.all_rows("launch_events")
        assert rows[0]["event_block_time_ms"] == 1_718_700_050_000

    def test_event_date_is_from_block_time_not_wall_clock(self):
        """Partition key = date(event_time.block_time_ms), NOT wall_clock_ms (C-5)."""
        writer, backend = _make_store()
        # block_time_ms corresponds to 2024-06-18
        ev = _make_event(block_time_ms=1_718_700_000_000)  # 2024-06-18
        asyncio.run(
            writer.write_launch_event(ev, "sig_ccc")
        )
        rows = backend.all_rows("launch_events")
        # event_date must be derived from block_time_ms, not current time
        assert rows[0]["event_date"] == "2024-06-18"

    def test_recorded_at_ms_non_negative(self):
        writer, backend = _make_store()
        ev = _make_event()
        asyncio.run(
            writer.write_launch_event(ev, "sig_ddd")
        )
        rows = backend.all_rows("launch_events")
        assert rows[0]["recorded_at_ms"] > 0

    def test_recorded_at_ms_is_wall_clock_not_event_time(self):
        """recorded_at_ms is stamped at write time (monitoring), not event_time."""
        writer, backend = _make_store()
        ev = _make_event(block_time_ms=1_718_700_000_000)  # ancient event time
        before_ms = _now_ms()
        asyncio.run(
            writer.write_launch_event(ev, "sig_eee")
        )
        after_ms = _now_ms()
        rows = backend.all_rows("launch_events")
        recorded = rows[0]["recorded_at_ms"]
        assert before_ms <= recorded <= after_ms + 100  # within the call window

    def test_recorded_at_ms_ge_event_block_time_ms(self):
        """HARD INVARIANT: recorded_at_ms >= event_time.block_time_ms (§9.2)."""
        writer, backend = _make_store()
        ev = _make_event(block_time_ms=1_718_700_000_000)  # past event
        asyncio.run(
            writer.write_launch_event(ev, "sig_fff")
        )
        rows = backend.all_rows("launch_events")
        assert rows[0]["recorded_at_ms"] >= rows[0]["event_block_time_ms"]


# ---------------------------------------------------------------------------
# Test 2: Rejects future-dated recorded_at (honesty constraint)
# ---------------------------------------------------------------------------

class TestRecordedAtHonesty:
    def test_would_violate_honesty_returns_true_for_future_block_time(self):
        """A block_time_ms in the future would violate the honesty constraint."""
        writer, _ = _make_store()
        future_block_time_ms = int(time.time() * 1_000) + 60_000  # 60s in future
        assert writer.would_violate_honesty(
            event_time_block_time_ms=future_block_time_ms,
            recorded_at_ms=int(time.time() * 1_000),
        )

    def test_write_rejects_future_block_time(self):
        """Writing an event with block_time_ms in the future must raise."""
        writer, _ = _make_store()
        future_ms = int(time.time() * 1_000) + 300_000  # 5 min in future
        ev = LaunchEvent(
            mint="FutureMint1111111111111111111111111111111111",
            venue_program_id="6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",
            source=LaunchSource.PUMPFUN,
            event_time=EventTime(
                slot=999_999_999,
                block_time_ms=future_ms,
                wall_clock_ms=future_ms,
            ),
            observation_slot=999_999_999,
            confirmation_slot=999_999_999,
            detection_transport=DetectionTransport.GEYSER,
            sol_reserve_lamports=1_000_000,
            token_reserve_base=1_000_000,
            token_decimals=6,
            initial_holders=0,
            competitors=0,
            creator_wallet="Creator",
            bundler_cluster_id=None,
            deploy_template_fingerprint=None,
            data_staleness_ms=0,
        )
        # recorded_at_ms at write time will be less than future block_time_ms
        with pytest.raises(ProvenanceTaintError, match="recorded_at_before_knowable"):
            asyncio.run(
                writer.write_launch_event(ev, "sig_future")
            )

    def test_backfill_recorded_at_regression_rejected(self):
        """A second write with recorded_at <= the first write's recorded_at is rejected."""
        writer, backend = _make_store()
        ev = _make_event(block_time_ms=1_718_700_000_000)
        asyncio.run(
            writer.write_launch_event(ev, "sig_first")
        )
        rows = backend.all_rows("launch_events")
        first_recorded_at = rows[0]["recorded_at_ms"]

        # Simulate a backfill that tries to set recorded_at to BEFORE the first write.
        # The honesty guard should catch this because latest_recorded_at_for will find
        # the first record and compare.
        from aats.contracts.provenance import ProvenanceTaintError, assert_recorded_at_honesty
        with pytest.raises(ProvenanceTaintError, match="backfill_recorded_at_regression"):
            assert_recorded_at_honesty(
                recorded_at_ms=first_recorded_at - 1,  # before the first write
                event_time_block_time_ms=1_718_700_000_000,
                dataset="launch_events",
                mint=ev.mint,
                latest_recorded_at_ms=first_recorded_at,
            )


# ---------------------------------------------------------------------------
# Test 3: Out-of-order + duplicated event replay (self-check item 4)
# ---------------------------------------------------------------------------

class TestOutOfOrderAndDuplicates:
    def test_out_of_order_delivery_produces_same_rows(self):
        """Delivering events A, B, C in order C, A, B produces the same stored rows as A, B, C."""
        mint = "OOOTestMint11111111111111111111111111111111"

        events = [
            _make_event(mint=mint, slot=300_000_100, block_time_ms=1_718_700_100_000),
            _make_event(mint=mint, slot=300_000_101, block_time_ms=1_718_700_101_000),
            _make_event(mint=mint, slot=300_000_102, block_time_ms=1_718_700_102_000),
        ]

        # In-order store
        writer1, backend1 = _make_store()
        for i, ev in enumerate(events):
            asyncio.run(
                writer1.write_launch_event(ev, f"sig_order_{i}")
            )

        # Out-of-order store (C, A, B)
        writer2, backend2 = _make_store()
        for i, ev in enumerate([events[2], events[0], events[1]]):
            asyncio.run(
                writer2.write_launch_event(ev, f"sig_ooo_{i}")
            )

        rows1 = sorted(backend1.all_rows("launch_events"), key=lambda r: r["event_slot"])
        rows2 = sorted(backend2.all_rows("launch_events"), key=lambda r: r["event_slot"])

        # Both stores should have the same events (same slots, mints, block_times)
        assert len(rows1) == len(rows2) == 3
        for r1, r2 in zip(rows1, rows2, strict=False):
            assert r1["event_slot"] == r2["event_slot"]
            assert r1["event_block_time_ms"] == r2["event_block_time_ms"]
            assert r1["mint"] == r2["mint"]
            # event_date (partition key) must match — derived from event_time, not recorded_at
            assert r1["event_date"] == r2["event_date"]

    def test_duplicate_writes_do_not_change_event_time_fields(self):
        """Writing the same event twice: both rows stored; event_time fields identical."""
        ev = _make_event(slot=300_000_200, block_time_ms=1_718_700_200_000)
        writer, backend = _make_store()
        asyncio.run(
            writer.write_launch_event(ev, "sig_dup")
        )
        asyncio.run(
            writer.write_launch_event(ev, "sig_dup")  # same sig, second write
        )
        rows = backend.all_rows("launch_events")
        assert len(rows) == 2
        for r in rows:
            assert r["event_slot"] == 300_000_200
            assert r["event_block_time_ms"] == 1_718_700_200_000
            assert r["event_date"] == "2024-06-18"

    def test_no_wall_clock_leak_in_stored_event_time(self):
        """event_slot and event_block_time_ms must never be derived from wall-clock."""
        ev = _make_event(slot=300_000_300, block_time_ms=1_000_000_000_000)  # Jan 2001 date
        writer, backend = _make_store()
        asyncio.run(
            writer.write_launch_event(ev, "sig_noleak")
        )
        rows = backend.all_rows("launch_events")
        assert rows[0]["event_slot"] == 300_000_300
        # block_time_ms is from fixture, NOT from time.time()
        assert rows[0]["event_block_time_ms"] == 1_000_000_000_000


# ---------------------------------------------------------------------------
# Test 4: Bus round-trip (offline via InMemory store)
# ---------------------------------------------------------------------------

class TestStoreRoundTrip:
    def test_write_and_read_as_of(self):
        """Written events are readable via as-of query."""
        writer, backend = _make_store()
        ev = _make_event(mint="RoundTripMint1111111111111111111111111111111")
        asyncio.run(
            writer.write_launch_event(ev, "sig_rt")
        )
        cutoff = _now_ms() + 1_000
        results = writer.read_launch_events_as_of("RoundTripMint1111111111111111111111111111111", cutoff)
        assert len(results) == 1
        assert results[0]["event_slot"] == ev.event_time.slot

    def test_as_of_query_excludes_future_recorded_at(self):
        """as-of with early cutoff excludes events recorded later."""
        writer, backend = _make_store()
        ev = _make_event(mint="AsOfMint1111111111111111111111111111111111111")
        asyncio.run(
            writer.write_launch_event(ev, "sig_asof")
        )
        rows = backend.all_rows("launch_events")
        recorded = rows[0]["recorded_at_ms"]

        # Cutoff BEFORE the write: should return nothing
        results_early = writer.read_launch_events_as_of(
            "AsOfMint1111111111111111111111111111111111111",
            recorded - 1,
        )
        assert len(results_early) == 0

        # Cutoff AT the write: should return the row
        results_at = writer.read_launch_events_as_of(
            "AsOfMint1111111111111111111111111111111111111",
            recorded,
        )
        assert len(results_at) == 1


# ---------------------------------------------------------------------------
# Test 5: ShadowRecorder / RECORD mode
# ---------------------------------------------------------------------------

class TestShadowRecorder:
    def test_captures_events_within_k_slots(self):
        backend = InMemoryParquetBackend()
        recorder = ShadowRecorder(backend, first_k_slots=5)

        mint = "ShadowMint11111111111111111111111111111111111"
        for slot_offset in range(6):  # slots 0..5 (inclusive = within K)
            ev = _make_event(
                mint=mint,
                slot=300_000_000 + slot_offset,
                block_time_ms=1_718_700_000_000 + slot_offset * 400,
            )
            recorder.observe(ev)

        # Should still be in open window (slot 5 = event_time_slot + K)
        assert recorder.open_window_count == 1

    def test_flushes_window_past_k_slots(self):
        backend = InMemoryParquetBackend()
        recorder = ShadowRecorder(backend, first_k_slots=5)

        mint = "FlushMint111111111111111111111111111111111111"
        # Open with slot 0
        recorder.observe(_make_event(mint=mint, slot=300_000_000, block_time_ms=1_718_700_000_000))
        # Push past K
        recorder.observe(_make_event(mint=mint, slot=300_000_006, block_time_ms=1_718_700_006_000))
        # Window should be flushed
        assert recorder.open_window_count == 0

    def test_flushed_snapshot_in_backend(self):
        backend = InMemoryParquetBackend()
        recorder = ShadowRecorder(backend, first_k_slots=3)

        mint = "SnapMint11111111111111111111111111111111111111"
        for i in range(4):
            recorder.observe(_make_event(mint=mint, slot=300_000_000 + i, block_time_ms=1_718_700_000_000 + i * 400))
        # Slot 3 is within K=3: still open. Push to 4 to flush.
        recorder.observe(_make_event(mint=mint, slot=300_000_004, block_time_ms=1_718_700_004_000))

        snaps = recorder.completed_snapshots()
        assert len(snaps) >= 1
        snap_row = snaps[0]
        snap_data = json.loads(snap_row["payload_json"])
        assert snap_data["mint"] == mint

    def test_censored_on_reconnect(self):
        """All open windows marked CENSORED on transport reconnect (C-6)."""
        backend = InMemoryParquetBackend()
        recorder = ShadowRecorder(backend, first_k_slots=10)

        for i in range(3):
            mint = f"Mint{i}1111111111111111111111111111111111111111"
            recorder.observe(_make_event(mint=mint, slot=300_000_000 + i, block_time_ms=1_718_700_000_000 + i * 400))

        assert recorder.open_window_count == 3
        recorder.on_reconnect()
        assert recorder.open_window_count == 0

        snaps = recorder.completed_snapshots()
        assert len(snaps) == 3
        for snap in snaps:
            payload = json.loads(snap["payload_json"])
            assert payload["completeness_status"] == "CENSORED"

    def test_completeness_status_complete_on_normal_flush(self):
        backend = InMemoryParquetBackend()
        recorder = ShadowRecorder(backend, first_k_slots=2)

        mint = "CompleteMint11111111111111111111111111111111"
        # Observe within K, then past K
        for i in range(3):
            recorder.observe(_make_event(mint=mint, slot=300_000_000 + i, block_time_ms=1_718_700_000_000 + i * 400))
        recorder.observe(_make_event(mint=mint, slot=300_000_003, block_time_ms=1_718_700_003_000))

        snaps = recorder.completed_snapshots()
        assert len(snaps) == 1
        payload = json.loads(snaps[0]["payload_json"])
        assert payload["completeness_status"] == "complete"

    def test_flush_all_writes_complete_status(self):
        backend = InMemoryParquetBackend()
        recorder = ShadowRecorder(backend, first_k_slots=5)

        mint = "FlushAllMint111111111111111111111111111111111"
        recorder.observe(_make_event(mint=mint, slot=300_000_000, block_time_ms=1_718_700_000_000))
        recorder.flush_all(status="complete")

        snaps = recorder.completed_snapshots()
        assert len(snaps) == 1
        payload = json.loads(snaps[0]["payload_json"])
        assert payload["completeness_status"] == "complete"

    def test_snapshot_event_time_from_on_chain_not_wall_clock(self):
        """event_slot in the snapshot must come from the event's on-chain slot."""
        backend = InMemoryParquetBackend()
        recorder = ShadowRecorder(backend, first_k_slots=0)

        ev = _make_event(slot=300_000_777, block_time_ms=1_718_700_000_000)
        recorder.observe(ev)
        # K=0 means the very next event past slot 0 flushes; let's push with slot +1
        recorder.observe(_make_event(slot=300_000_778, block_time_ms=1_718_700_001_000))

        snaps = recorder.completed_snapshots()
        assert len(snaps) >= 1
        payload = json.loads(snaps[0]["payload_json"])
        # event_slot must be the on-chain slot, not time.time()
        assert payload["event_slot"] == 300_000_777
