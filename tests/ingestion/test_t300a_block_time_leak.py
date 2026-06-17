"""T-300a regression tests — _make_event_time wall-clock compute-time leak fix.

These tests prove that:
1. When block_time_unix_s is None, _make_event_time returns None (not a wall-clock
   dated EventTime).
2. When block_time_unix_s is 0, _make_event_time returns None.
3. All decoders return None for transactions with None block_time (never emit a
   wall-clock-dated LaunchEvent).
4. data_staleness_ms is STALENESS_UNKNOWN (-1) when block_time is absent.
5. data_staleness_ms is honest (positive, >= wall-decode lag) when block_time is
   confirmed.
6. Events with None block_time are excluded from event_date partitioning — they are
   NEVER written to the point-in-time Parquet store with a wall-clock-derived date.
7. The store's write_pending_slot_event carries event_date=None explicitly.
8. Regression proof: the old code path (wall-clock substitution) cannot execute.

Gates C-5 (clock audit, T-400).

HARD RULES (non-waivable):
  - No wall-clock value must ever appear in event_time.block_time_ms.
  - STALENESS_UNKNOWN must propagate to data_staleness_ms when block_time absent.
  - event_date must never be computed from wall_clock_ms.
"""

from __future__ import annotations

import asyncio
import time

from aats.contracts.events import DetectionTransport, LaunchSource
from aats.ingestion.decoders import (
    STALENESS_UNKNOWN,
    InstructionRouter,
    PumpFunDecoder,
    PumpSwapDecoder,
    RawTransaction,
    RaydiumCpmmDecoder,
    RaydiumV4Decoder,
    _make_event_time,
    _staleness_ms,
)
from aats.ingestion.store import InMemoryParquetBackend, PointInTimeStoreWriter, _now_ms
from tests.ingestion.fixtures import (
    make_pumpfun_buy_tx,
    make_pumpfun_create_tx,
    make_pumpfun_sell_tx,
    make_pumpfun_withdraw_tx,
    make_pumpswap_create_pool_tx,
    make_raydium_cpmm_init_tx,
    make_raydium_cpmm_swap_tx,
    make_raydium_v4_init2_tx,
    make_registry,
)

# ---------------------------------------------------------------------------
# Helpers to build None-block_time versions of fixtures
# ---------------------------------------------------------------------------


def _with_no_block_time(tx: RawTransaction) -> RawTransaction:
    """Return a copy of tx with block_time_unix_s=None."""
    return RawTransaction(
        signature=tx.signature,
        slot=tx.slot,
        block_time_unix_s=None,  # <-- the ShredStream / pre-confirmation case
        fee_payer=tx.fee_payer,
        instructions=tx.instructions,
        inner_instructions=tx.inner_instructions,
        program_logs=tx.program_logs,
        is_vote=tx.is_vote,
        err=tx.err,
    )


def _with_zero_block_time(tx: RawTransaction) -> RawTransaction:
    """Return a copy of tx with block_time_unix_s=0 (also absent)."""
    return RawTransaction(
        signature=tx.signature,
        slot=tx.slot,
        block_time_unix_s=0,  # 0 is treated as absent (T-300a)
        fee_payer=tx.fee_payer,
        instructions=tx.instructions,
        inner_instructions=tx.inner_instructions,
        program_logs=tx.program_logs,
        is_vote=tx.is_vote,
        err=tx.err,
    )


# ---------------------------------------------------------------------------
# 1. _make_event_time returns None for absent block_time (T-300a)
# ---------------------------------------------------------------------------


class TestMakeEventTimeReturnsNoneWhenBlockTimeAbsent:
    """Core guard: _make_event_time must NEVER substitute wall-clock."""

    def test_none_block_time_returns_none(self):
        """block_time_unix_s=None → _make_event_time must return None, not a wall-clock EventTime."""
        result = _make_event_time(slot=300_000_000, block_time_unix_s=None)
        assert result is None, (
            "REGRESSION: _make_event_time returned an EventTime with wall-clock "
            "substituted into block_time_ms when block_time_unix_s was None. "
            "This is the T-300a compute-time leak."
        )

    def test_zero_block_time_returns_none(self):
        """block_time_unix_s=0 → _make_event_time must return None (0 means absent)."""
        result = _make_event_time(slot=300_000_000, block_time_unix_s=0)
        assert result is None, (
            "REGRESSION: _make_event_time returned an EventTime with wall-clock "
            "substituted when block_time_unix_s=0. "
            "0 means 'not yet confirmed' on the ShredStream path. T-300a."
        )

    def test_negative_block_time_returns_none(self):
        """block_time_unix_s < 0 is treated as absent."""
        result = _make_event_time(slot=300_000_000, block_time_unix_s=-1)
        assert result is None

    def test_valid_block_time_returns_event_time(self):
        """block_time_unix_s > 0 → returns EventTime with correct block_time_ms."""
        result = _make_event_time(slot=300_000_000, block_time_unix_s=1_718_700_000)
        assert result is not None
        assert result.block_time_ms == 1_718_700_000 * 1_000
        assert result.slot == 300_000_000

    def test_block_time_ms_is_not_wall_clock_when_confirmed(self):
        """When confirmed, block_time_ms is on-chain value, wall_clock_ms is separate."""
        before_ms = _now_ms()
        result = _make_event_time(slot=300_000_001, block_time_unix_s=1_718_700_001)
        after_ms = _now_ms()
        assert result is not None
        # block_time_ms must be the on-chain value — definitely not wall_clock range
        assert result.block_time_ms == 1_718_700_001 * 1_000
        # wall_clock_ms must be near current time (it's stamped at decode)
        assert before_ms <= result.wall_clock_ms <= after_ms + 100

    def test_block_time_ms_never_equals_wall_clock_for_old_events(self):
        """Staleness is genuine: block_time_ms (old event) != wall_clock_ms (now)."""
        result = _make_event_time(slot=100, block_time_unix_s=1_000_000)  # Jan 2001
        assert result is not None
        # block_time_ms is 2001-era, wall_clock_ms is now; they must differ by >> 0
        assert result.wall_clock_ms - result.block_time_ms > 1_000  # at least 1 second


# ---------------------------------------------------------------------------
# 2. _staleness_ms with None event_time returns STALENESS_UNKNOWN (-1)
# ---------------------------------------------------------------------------


class TestStalenessUnknown:
    def test_staleness_unknown_sentinel_value(self):
        """STALENESS_UNKNOWN must equal -1."""
        assert STALENESS_UNKNOWN == -1

    def test_staleness_ms_returns_unknown_for_none_event_time(self):
        """_staleness_ms(None) must return STALENESS_UNKNOWN, not 0."""
        result = _staleness_ms(None)
        assert result == STALENESS_UNKNOWN, (
            "REGRESSION: _staleness_ms returned 0 (or any non-sentinel) for None "
            "event_time.  This collapses data_staleness_ms to 0 and hides true "
            "staleness.  T-300a requires STALENESS_UNKNOWN = -1."
        )

    def test_staleness_ms_non_negative_for_valid_event_time(self):
        """When block_time_ms is old, staleness is a positive integer."""
        from aats.contracts.events import EventTime

        et = EventTime(
            slot=300_000_000,
            block_time_ms=1_718_700_000_000,  # 2024 era
            wall_clock_ms=1_718_700_001_000,
        )
        result = _staleness_ms(et)
        # wall_clock at decode is now (2026), block_time is 2024 — huge staleness
        assert result > 0, "Staleness for a 2024 event must be > 0 in 2026"

    def test_staleness_ms_never_negative_for_valid_event_time(self):
        """Staleness is clamped to 0 minimum for valid event times (max(0, ...))."""
        from aats.contracts.events import EventTime

        et = EventTime(
            slot=300_000_000,
            block_time_ms=int(time.time() * 1_000) + 60_000,  # 1 min in future (test clock)
            wall_clock_ms=int(time.time() * 1_000),
        )
        # block_time is in the future (edge case for test clocks) — clamped to 0
        result = _staleness_ms(et)
        assert result >= 0


# ---------------------------------------------------------------------------
# 3. All decoders return None for None-block_time transactions (T-300a)
# ---------------------------------------------------------------------------


class TestDecoderReturnsNoneForMissingBlockTime:
    """Every decoder path must return None when block_time_unix_s is None.

    If any of these tests fail, the leak is still present:
    the decoder is emitting a wall-clock-dated LaunchEvent.
    """

    # --- pump.fun ---

    def test_pumpfun_create_none_block_time(self):
        registry = make_registry()
        decoder = PumpFunDecoder(registry)
        tx = _with_no_block_time(make_pumpfun_create_tx())
        result = decoder.decode(tx, DetectionTransport.GEYSER)
        assert result is None, "pump.fun create must not emit event when block_time=None"

    def test_pumpfun_create_zero_block_time(self):
        registry = make_registry()
        decoder = PumpFunDecoder(registry)
        tx = _with_zero_block_time(make_pumpfun_create_tx())
        result = decoder.decode(tx, DetectionTransport.GEYSER)
        assert result is None, "pump.fun create must not emit event when block_time=0"

    def test_pumpfun_buy_none_block_time(self):
        registry = make_registry()
        decoder = PumpFunDecoder(registry)
        tx = _with_no_block_time(make_pumpfun_buy_tx())
        result = decoder.decode(tx, DetectionTransport.GEYSER)
        assert result is None, "pump.fun buy must not emit event when block_time=None"

    def test_pumpfun_sell_none_block_time(self):
        registry = make_registry()
        decoder = PumpFunDecoder(registry)
        tx = _with_no_block_time(make_pumpfun_sell_tx())
        result = decoder.decode(tx, DetectionTransport.GEYSER)
        assert result is None, "pump.fun sell must not emit event when block_time=None"

    def test_pumpfun_migrate_none_block_time(self):
        """Migration (highest-value signal) must also hold pending — no wall-clock date."""
        registry = make_registry()
        decoder = PumpFunDecoder(registry)
        tx = _with_no_block_time(make_pumpfun_withdraw_tx())
        result = decoder.decode(tx, DetectionTransport.GEYSER)
        assert result is None, (
            "pump.fun MIGRATION must not emit event when block_time=None. "
            "Even the highest-value signal cannot have a wall-clock-dated block_time_ms."
        )

    # --- PumpSwap ---

    def test_pumpswap_create_pool_none_block_time(self):
        registry = make_registry()
        decoder = PumpSwapDecoder(registry)
        tx = _with_no_block_time(make_pumpswap_create_pool_tx())
        result = decoder.decode(tx, DetectionTransport.GEYSER)
        assert result is None, "PumpSwap create_pool must not emit event when block_time=None"

    # --- Raydium AMM v4 ---

    def test_raydium_v4_init2_none_block_time(self):
        registry = make_registry()
        decoder = RaydiumV4Decoder(registry)
        tx = _with_no_block_time(make_raydium_v4_init2_tx())
        result = decoder.decode(tx, DetectionTransport.GEYSER)
        assert result is None, "Raydium v4 init2 must not emit event when block_time=None"

    # --- Raydium CPMM ---

    def test_raydium_cpmm_init_none_block_time(self):
        registry = make_registry()
        decoder = RaydiumCpmmDecoder(registry)
        tx = _with_no_block_time(make_raydium_cpmm_init_tx())
        result = decoder.decode(tx, DetectionTransport.GEYSER)
        assert result is None, "Raydium CPMM init must not emit event when block_time=None"

    def test_raydium_cpmm_swap_none_block_time(self):
        registry = make_registry()
        decoder = RaydiumCpmmDecoder(registry)
        tx = _with_no_block_time(make_raydium_cpmm_swap_tx())
        result = decoder.decode(tx, DetectionTransport.GEYSER)
        assert result is None, "Raydium CPMM swap must not emit event when block_time=None"

    # --- InstructionRouter ---

    def test_router_returns_none_for_all_none_block_time(self):
        """The router must propagate None from all decoders when block_time is absent."""
        registry = make_registry()
        router = InstructionRouter(registry)
        txs = [
            make_pumpfun_create_tx(),
            make_pumpfun_buy_tx(),
            make_pumpfun_sell_tx(),
            make_pumpfun_withdraw_tx(),
            make_pumpswap_create_pool_tx(),
            make_raydium_v4_init2_tx(),
            make_raydium_cpmm_init_tx(),
            make_raydium_cpmm_swap_tx(),
        ]
        for tx in txs:
            no_bt_tx = _with_no_block_time(tx)
            result = router.route(no_bt_tx, DetectionTransport.GEYSER)
            assert result is None, (
                f"Router returned a LaunchEvent for {tx.signature} with None block_time. "
                "This is the T-300a wall-clock leak. Router must return None."
            )


# ---------------------------------------------------------------------------
# 4. Confirmed block_time events are decoded normally (no regression on happy path)
# ---------------------------------------------------------------------------


class TestConfirmedBlockTimeDecodesNormally:
    """Ensure the fix did not break normal (confirmed block_time) decoding."""

    def test_pumpfun_create_with_block_time_decoded(self):
        registry = make_registry()
        decoder = PumpFunDecoder(registry)
        tx = make_pumpfun_create_tx()  # has block_time_unix_s=1_718_700_000
        result = decoder.decode(tx, DetectionTransport.GEYSER)
        assert result is not None
        assert result.event_time.block_time_ms == 1_718_700_000 * 1_000

    def test_pumpfun_buy_block_time_ms_equals_on_chain(self):
        """event_time.block_time_ms must equal block_time_unix_s * 1000 (C-5)."""
        registry = make_registry()
        decoder = PumpFunDecoder(registry)
        tx = make_pumpfun_buy_tx()  # block_time_unix_s=1_718_700_001
        result = decoder.decode(tx, DetectionTransport.GEYSER)
        assert result is not None
        assert result.event_time.block_time_ms == tx.block_time_unix_s * 1_000

    def test_staleness_is_positive_for_old_event(self):
        """data_staleness_ms must be > 0 for an on-chain event from 2024."""
        registry = make_registry()
        decoder = PumpFunDecoder(registry)
        tx = make_pumpfun_create_tx()
        result = decoder.decode(tx, DetectionTransport.GEYSER)
        assert result is not None
        # In 2026, a 2024 event has high staleness
        assert result.data_staleness_ms > 0
        # And it must NOT be STALENESS_UNKNOWN
        assert result.data_staleness_ms != STALENESS_UNKNOWN

    def test_all_decoders_decode_confirmed_events(self):
        """Regression: confirm the happy path still works for all decoders."""
        registry = make_registry()
        router = InstructionRouter(registry)
        txs = [
            (make_pumpfun_create_tx(), LaunchSource.PUMPFUN),
            (make_pumpfun_buy_tx(), LaunchSource.PUMPFUN),
            (make_pumpfun_sell_tx(), LaunchSource.PUMPFUN),
            (make_pumpfun_withdraw_tx(), LaunchSource.MIGRATION),
            (make_pumpswap_create_pool_tx(), LaunchSource.PUMPSWAP),
            (make_raydium_v4_init2_tx(), LaunchSource.RAYDIUM_V4),
            (make_raydium_cpmm_init_tx(), LaunchSource.RAYDIUM_CPMM),
            (make_raydium_cpmm_swap_tx(), LaunchSource.RAYDIUM_CPMM),
        ]
        for tx, expected_source in txs:
            result = router.route(tx, DetectionTransport.GEYSER)
            assert result is not None, f"Confirmed-block_time tx {tx.signature} returned None"
            assert result.source == expected_source
            assert result.event_time.block_time_ms > 0
            assert (
                result.event_time.block_time_ms != result.event_time.wall_clock_ms
                or result.event_time.block_time_ms < result.event_time.wall_clock_ms
            ), "block_time_ms must be the on-chain value, not wall_clock_ms"


# ---------------------------------------------------------------------------
# 5. Store: None-block_time events are excluded from event_date partitioning
# ---------------------------------------------------------------------------


class TestStoreExcludesNoneBlockTimeFromPartitioning:
    """Events with None block_time must NEVER enter the main Parquet store.

    write_pending_slot_event() records slot + wall_clock but carries
    event_date=None — no date partition computed from wall_clock.
    """

    def test_pending_event_has_no_event_date(self):
        """write_pending_slot_event must store event_date=None (T-300a).

        T-301 carry-forward: pending rows now live in the DEDICATED pending_events
        table (backend.all_rows('pending_events') / backend.all_pending_rows()).
        read_as_of('pending_events', mint, cutoff) no longer raises KeyError: 'mint'.
        """
        backend = InMemoryParquetBackend()
        writer = PointInTimeStoreWriter(redis_client=None, parquet_backend=backend)
        asyncio.run(
            writer.write_pending_slot_event(
                slot=300_000_999,
                wall_clock_ms=_now_ms(),
                source_signature="no_block_time_sig_aabbcc",
            )
        )
        # Pending rows are in the DEDICATED table (not mixed into _rows).
        rows = backend.all_rows("pending_events")
        assert len(rows) == 1
        row = rows[0]
        assert row["event_date"] is None, (
            "REGRESSION: pending event has a non-None event_date. "
            "A None-block_time event must NEVER be dated by wall_clock. T-300a."
        )

    def test_pending_event_staleness_is_unknown_sentinel(self):
        """Pending event's data_staleness_ms must be STALENESS_UNKNOWN (-1)."""
        backend = InMemoryParquetBackend()
        writer = PointInTimeStoreWriter(redis_client=None, parquet_backend=backend)
        asyncio.run(
            writer.write_pending_slot_event(
                slot=300_001_000,
                wall_clock_ms=_now_ms(),
                source_signature="no_block_time_staleness_sig",
            )
        )
        # Use the dedicated pending_events table (T-301 carry-forward).
        rows = backend.all_rows("pending_events")
        assert rows[0]["data_staleness_ms"] == STALENESS_UNKNOWN

    def test_pending_event_does_not_pollute_launch_events_dataset(self):
        """Pending events must go to 'pending_events' dataset, not 'launch_events'."""
        backend = InMemoryParquetBackend()
        writer = PointInTimeStoreWriter(redis_client=None, parquet_backend=backend)
        asyncio.run(
            writer.write_pending_slot_event(
                slot=300_001_001,
                wall_clock_ms=_now_ms(),
                source_signature="no_block_time_isolation_sig",
            )
        )
        launch_rows = backend.all_rows("launch_events")
        assert len(launch_rows) == 0, (
            "A pending (no block_time) event ended up in launch_events. "
            "It must be quarantined in 'pending_events' with no event_date. T-300a."
        )

    def test_confirmed_launch_event_has_event_date_not_none(self):
        """Contrast: a confirmed LaunchEvent DOES have an event_date."""
        from aats.contracts.events import (
            EventTime,
            LaunchEvent,
            LaunchSource,
        )

        backend = InMemoryParquetBackend()
        writer = PointInTimeStoreWriter(redis_client=None, parquet_backend=backend)
        ev = LaunchEvent(
            mint="ConfirmedMint111111111111111111111111111111",
            venue_program_id="6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",
            source=LaunchSource.PUMPFUN,
            event_time=EventTime(
                slot=300_000_000,
                block_time_ms=1_718_700_000_000,  # 2024-06-18
                wall_clock_ms=1_718_700_001_000,
            ),
            observation_slot=300_000_000,
            confirmation_slot=300_000_000,
            detection_transport=DetectionTransport.GEYSER,
            sol_reserve_lamports=5_000_000_000,
            token_reserve_base=1_000_000_000_000,
            token_decimals=6,
            initial_holders=0,
            competitors=0,
            creator_wallet="Creator111111111111111111111111111111111111",
            bundler_cluster_id=None,
            deploy_template_fingerprint=None,
            data_staleness_ms=1000,
        )
        asyncio.run(
            writer.write_launch_event(ev, "confirmed_sig_aabb")
        )
        rows = backend.all_rows("launch_events")
        assert len(rows) == 1
        assert rows[0]["event_date"] == "2024-06-18"
        assert rows[0]["event_date"] is not None


# ---------------------------------------------------------------------------
# 6. Regression proof: prove the old code path (wall-clock substitution) is closed
# ---------------------------------------------------------------------------


class TestLeakRegressionClosed:
    """Mutation-meaningful regression: prove the wall-clock leak cannot silently return.

    These tests directly probe the condition that caused the bug.  If a future
    change re-introduces `block_time_ms = wall_ms` when block_time is absent,
    at least one of these tests will fail.
    """

    def test_none_block_time_does_not_produce_event_time_close_to_now(self):
        """No wall-clock EventTime is created when block_time_unix_s=None.

        If the old code were still active, _make_event_time(slot, None) would
        return an EventTime with block_time_ms ≈ wall_clock_ms ≈ now.
        This test asserts that path is DEAD.
        """
        before_ms = _now_ms()
        result = _make_event_time(slot=300_000_000, block_time_unix_s=None)
        after_ms = _now_ms()

        # The only acceptable result is None.  If result is not None, it means
        # wall-clock was substituted — that is the bug.
        assert result is None, (
            f"LEAK DETECTED: _make_event_time returned EventTime with "
            f"block_time_ms={result.block_time_ms if result else 'N/A'} "
            f"(wall range: {before_ms}..{after_ms}) for None block_time_unix_s. "
            "The T-300a fix has been bypassed or reverted."
        )

    def test_zero_block_time_does_not_produce_event_time_close_to_now(self):
        """Same regression probe for block_time_unix_s=0."""
        before_ms = _now_ms()
        result = _make_event_time(slot=300_000_000, block_time_unix_s=0)
        after_ms = _now_ms()

        assert result is None, (
            f"LEAK DETECTED: _make_event_time returned EventTime with "
            f"block_time_ms={result.block_time_ms if result else 'N/A'} "
            f"(wall range: {before_ms}..{after_ms}) for block_time_unix_s=0. "
            "The T-300a fix has been bypassed or reverted."
        )

    def test_pumpfun_migrate_none_block_time_does_not_produce_event_with_wall_clock_date(self):
        """Migration (highest-value) event must not be wall-clock dated.

        The original bug was worst on migration events because they are the
        most time-sensitive signal.  Prove this specific path is clean.
        """
        registry = make_registry()
        decoder = PumpFunDecoder(registry)
        tx = _with_no_block_time(make_pumpfun_withdraw_tx())

        before_ms = _now_ms()
        result = decoder.decode(tx, DetectionTransport.GEYSER)
        after_ms = _now_ms()

        assert result is None, (
            f"LEAK DETECTED on MIGRATION path: decoder returned LaunchEvent with "
            f"block_time_ms={result.event_time.block_time_ms if result else 'N/A'} "
            f"(wall range: {before_ms}..{after_ms}) for None block_time. "
            "This is the worst-case T-300a leak on the migration sniper path."
        )

    def test_router_none_block_time_not_substituted_with_wall_clock(self):
        """End-to-end proof: router must never emit wall-clock-dated event."""
        registry = make_registry()
        router = InstructionRouter(registry)

        # Use a migration event — most valuable, most tempting to fake a time for
        tx = _with_no_block_time(make_pumpfun_withdraw_tx())
        before_ms = _now_ms()
        result = router.route(tx, DetectionTransport.GEYSER)
        after_ms = _now_ms()

        assert result is None, (
            f"REGRESSION: router emitted a wall-clock-dated LaunchEvent for "
            f"migration with None block_time. "
            f"block_time_ms would have been ≈{before_ms} to {after_ms}. "
            "T-300a leak not fully closed."
        )

    def test_staleness_is_not_zero_for_none_block_time(self):
        """data_staleness_ms must NOT be 0 when block_time is absent.

        The original bug collapsed staleness to ~0 by setting
        block_time_ms = wall_ms (so wall_ms - block_time_ms ≈ 0).
        Prove the sentinel is -1, not 0.
        """
        staleness = _staleness_ms(None)
        assert staleness != 0, (
            "REGRESSION: _staleness_ms(None) returned 0. "
            "This is the staleness-collapse symptom of the T-300a wall-clock leak. "
            "Expected STALENESS_UNKNOWN = -1."
        )
        assert (
            staleness == STALENESS_UNKNOWN
        ), f"Expected STALENESS_UNKNOWN = {STALENESS_UNKNOWN}, got {staleness}"
