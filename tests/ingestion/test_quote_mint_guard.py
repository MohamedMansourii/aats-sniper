"""T-QUOTE-GUARD unit tests — quote/settlement mints are never recorded as launches.

Root cause addressed
--------------------
In a live run, 1/40 launch events was keyed on the USDC mint
(EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v) tagged source=pump.fun.
Investigation showed PumpPortal's ``subscribeMigration`` feed sometimes places
the Raydium pool's QUOTE side (USDC or WSOL) in the message's ``mint`` field
instead of the graduated pump.fun base token.  The same bug can occur in
``subscribeNewToken`` frames for any edge-case where PumpPortal mis-identifies
the token mint.

Fix layers tested here
----------------------
PRIMARY  — PumpPortalTransport._map_new_token / _map_migration:
           If mint is in QUOTE_MINTS the event is dropped (returns None) and
           stats.events_skipped is incremented.

SECONDARY — ShadowRecorder.observe() (defense-in-depth):
            If event.mint is in QUOTE_MINTS the event is dropped and
            orphan_events_total is incremented.  This guards any upstream caller
            that bypasses the transport-layer guard.

Mutation-meaningful test design
--------------------------------
Removing the ``if mint in QUOTE_MINTS`` guard from transport.py or store.py
causes tests in class TestPumpPortalQuoteMintGuard or
TestShadowRecorderQuoteMintGuard (respectively) to fail.

All tests are fully offline — no network, no Redis, no real Parquet I/O.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, patch

from aats.contracts.events import DetectionTransport, EventTime, LaunchEvent, LaunchSource
from aats.ingestion.decoders import EventKind
from aats.ingestion.store import InMemoryParquetBackend, ShadowRecorder
from aats.ingestion.transport import (
    QUOTE_MINTS,
    PumpPortalTransport,
)

# ---------------------------------------------------------------------------
# Constants — well-known mints used across tests
# ---------------------------------------------------------------------------

_PUMP_PID = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"

# The three quote mints the guard covers.
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT_MINT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
WSOL_MINT = "So11111111111111111111111111111111111111112"

# A legitimate (non-quote) pump.fun base mint for regression tests.
REAL_MINT = "RealPumpToken11111111111111111111111111111111"

_SIG = "5KtPn3DV3gFcnGHCymZ7bEXKNkDMqWrPQ7NpHPBV2UHzFyVxMsT3d7zMZNywWQ"

_ENRICH_SLOT = 300_000_001
_ENRICH_BLOCK_TIME_MS = 1_718_700_001_000  # seconds * 1000


# ---------------------------------------------------------------------------
# PumpPortal WS / RPC mock helpers (reused from test_pumpportal.py pattern)
# ---------------------------------------------------------------------------

class _FakeWS:
    """Fake websockets.connect context manager."""

    def __init__(self, messages: list[dict]) -> None:
        self._messages = messages
        self.sent: list[dict] = []

    async def __aenter__(self) -> _FakeWS:
        return self

    async def __aexit__(self, *args: Any) -> None:
        pass

    async def send(self, data: str) -> None:
        try:
            self.sent.append(json.loads(data))
        except json.JSONDecodeError:
            self.sent.append({"_raw": data})

    def __aiter__(self) -> AsyncIterator[str]:
        return self._aiter()

    async def _aiter(self) -> AsyncIterator[str]:
        for msg in self._messages:
            yield json.dumps(msg)


class _FakeWsModule:
    def __init__(self, ws: _FakeWS) -> None:
        self._ws = ws

    def connect(self, *args: Any, **kwargs: Any) -> _FakeWS:
        return self._ws


def _patch_ws(ws: _FakeWS):
    return patch("aats.ingestion.transport._ws_import", return_value=_FakeWsModule(ws))


def _patch_slot_time(slot: int | None, block_time_ms: int | None):
    return patch.object(
        PumpPortalTransport,
        "_get_slot_time",
        new=AsyncMock(return_value=(slot, block_time_ms)),
    )


def _make_transport() -> PumpPortalTransport:
    return PumpPortalTransport(
        ws_url="wss://fake-pumpportal.example/api/data",
        rpc_url="https://fake-rpc.example/",
        pump_program_id=_PUMP_PID,
        enrich_via_rpc=True,
        max_reconnect_attempts=1,
    )


def _create_msg(mint: str) -> dict:
    """Minimal subscribeNewToken message with the given mint."""
    return {
        "txType": "create",
        "signature": _SIG,
        "mint": mint,
        "traderPublicKey": "Creator111111111111111111111111111111111111",
        "vSolInBondingCurve": 30.0,
        "vTokensInBondingCurve": 1_073_000_191.0,
    }


def _migration_msg(mint: str) -> dict:
    """Minimal subscribeMigration message with the given mint."""
    return {
        "txType": "migrate",
        "signature": _SIG,
        "mint": mint,
        "traderPublicKey": "Creator111111111111111111111111111111111111",
        "vSolInBondingCurve": 85.0,
        "vTokensInBondingCurve": 206_900_000.0,
        "pool": "raydium",
    }


async def _run(messages: list[dict]) -> tuple[list, PumpPortalTransport]:
    """Drive the transport with scripted messages; return (events, transport)."""
    fake_ws = _FakeWS(messages)
    transport = _make_transport()
    collected: list = []
    with _patch_ws(fake_ws), _patch_slot_time(_ENRICH_SLOT, _ENRICH_BLOCK_TIME_MS):
        gen = transport.events(last_slot=0)
        try:
            async for item in gen:
                collected.append(item)
        finally:
            await gen.aclose()
    return collected, transport


# ---------------------------------------------------------------------------
# QUOTE_MINTS constant tests (sanity / contract)
# ---------------------------------------------------------------------------

class TestQuoteMintsConstant:
    """QUOTE_MINTS must contain the three canonical settlement mints and nothing else."""

    def test_usdc_in_quote_mints(self):
        assert USDC_MINT in QUOTE_MINTS, "USDC must be in QUOTE_MINTS"

    def test_usdt_in_quote_mints(self):
        assert USDT_MINT in QUOTE_MINTS, "USDT must be in QUOTE_MINTS"

    def test_wsol_in_quote_mints(self):
        assert WSOL_MINT in QUOTE_MINTS, "WSOL/SOL must be in QUOTE_MINTS"

    def test_real_mint_not_in_quote_mints(self):
        """A normal pump.fun token mint must NOT be in QUOTE_MINTS."""
        assert REAL_MINT not in QUOTE_MINTS

    def test_quote_mints_is_frozenset(self):
        assert isinstance(QUOTE_MINTS, frozenset)

    def test_quote_mints_has_exactly_three_entries(self):
        assert len(QUOTE_MINTS) == 3


# ---------------------------------------------------------------------------
# PRIMARY guard: PumpPortalTransport._map_new_token / _map_migration
# ---------------------------------------------------------------------------

class TestPumpPortalQuoteMintGuard:
    """
    (a) A new-token/create or migration event with mint=USDC (or WSOL) must be
        DROPPED — not recorded — and stats.events_skipped must increment.

    Mutation-meaningful: removing the ``if mint in QUOTE_MINTS`` guard from
    _map_new_token / _map_migration makes these tests fail.
    """

    # -- create / subscribeNewToken path --

    async def test_create_with_usdc_mint_is_dropped(self):
        """subscribeNewToken with mint=USDC yields zero events."""
        collected, _ = await _run([_create_msg(USDC_MINT)])
        assert collected == [], (
            "create event with mint=USDC must be dropped (T-QUOTE-GUARD)"
        )

    async def test_create_with_usdc_mint_increments_skip_counter(self):
        """stats.events_skipped increments when create mint=USDC is dropped."""
        _, transport = await _run([_create_msg(USDC_MINT)])
        assert transport.stats.events_skipped >= 1, (
            "events_skipped must increment for a dropped quote-mint create"
        )

    async def test_create_with_wsol_mint_is_dropped(self):
        """subscribeNewToken with mint=WSOL yields zero events."""
        collected, _ = await _run([_create_msg(WSOL_MINT)])
        assert collected == [], (
            "create event with mint=WSOL must be dropped (T-QUOTE-GUARD)"
        )

    async def test_create_with_wsol_mint_increments_skip_counter(self):
        """stats.events_skipped increments when create mint=WSOL is dropped."""
        _, transport = await _run([_create_msg(WSOL_MINT)])
        assert transport.stats.events_skipped >= 1

    async def test_create_with_usdt_mint_is_dropped(self):
        """subscribeNewToken with mint=USDT yields zero events."""
        collected, _ = await _run([_create_msg(USDT_MINT)])
        assert collected == [], (
            "create event with mint=USDT must be dropped (T-QUOTE-GUARD)"
        )

    # -- migrate / subscribeMigration path --

    async def test_migration_with_usdc_mint_is_dropped(self):
        """subscribeMigration with mint=USDC yields zero events.

        This is the confirmed live-run failure mode: PumpPortal sent the
        Raydium pool's quote side (USDC) in the 'mint' field of the migration
        message instead of the graduated pump.fun base token.
        """
        collected, _ = await _run([_migration_msg(USDC_MINT)])
        assert collected == [], (
            "migration event with mint=USDC must be dropped (T-QUOTE-GUARD); "
            "this is the confirmed live-run bug: 1/40 launches keyed on USDC"
        )

    async def test_migration_with_usdc_mint_increments_skip_counter(self):
        """stats.events_skipped increments when migration mint=USDC is dropped."""
        _, transport = await _run([_migration_msg(USDC_MINT)])
        assert transport.stats.events_skipped >= 1, (
            "events_skipped must increment for a dropped quote-mint migration"
        )

    async def test_migration_with_wsol_mint_is_dropped(self):
        """subscribeMigration with mint=WSOL yields zero events."""
        collected, _ = await _run([_migration_msg(WSOL_MINT)])
        assert collected == []

    async def test_migration_with_wsol_mint_increments_skip_counter(self):
        """stats.events_skipped increments when migration mint=WSOL is dropped."""
        _, transport = await _run([_migration_msg(WSOL_MINT)])
        assert transport.stats.events_skipped >= 1

    # -- no regression on normal launches --

    async def test_create_with_real_mint_is_recorded(self):
        """(b) A normal pump.fun create event with a real base mint is NOT dropped.

        Regression guard: removing the quote-mint check must not affect this
        path (it passes today because REAL_MINT is not in QUOTE_MINTS).
        """
        collected, _ = await _run([_create_msg(REAL_MINT)])
        assert len(collected) == 1, (
            "A normal create event must still be recorded; no regression"
        )
        event, sig, kind = collected[0]
        assert event.mint == REAL_MINT
        assert kind == EventKind.CREATE

    async def test_create_with_real_mint_events_skipped_zero(self):
        """A normal create event must NOT increment events_skipped."""
        _, transport = await _run([_create_msg(REAL_MINT)])
        # events_decoded should be 1; events_skipped must be 0
        assert transport.stats.events_decoded == 1
        assert transport.stats.events_skipped == 0

    async def test_migration_with_real_mint_is_recorded(self):
        """A normal migration event with a real base mint is NOT dropped."""
        collected, _ = await _run([_migration_msg(REAL_MINT)])
        assert len(collected) == 1, (
            "A normal migration must still be recorded; no regression"
        )
        event, sig, kind = collected[0]
        assert event.mint == REAL_MINT
        assert kind == EventKind.WITHDRAW

    async def test_quote_mint_followed_by_real_mint_only_records_real(self):
        """USDC drop does not block a subsequent real-mint message in the same stream."""
        messages = [_create_msg(USDC_MINT), _create_msg(REAL_MINT)]
        collected, transport = await _run(messages)
        assert len(collected) == 1, (
            "Only the real-mint event should be recorded; USDC must be dropped"
        )
        assert collected[0][0].mint == REAL_MINT
        assert transport.stats.events_skipped == 1
        assert transport.stats.events_decoded == 1


# ---------------------------------------------------------------------------
# SECONDARY guard: ShadowRecorder.observe() (defense-in-depth in store.py)
# ---------------------------------------------------------------------------

def _make_event(mint: str, slot: int = 300_000_000) -> LaunchEvent:
    """Build a minimal valid LaunchEvent for ShadowRecorder tests."""
    block_time_ms = slot * 400
    return LaunchEvent(
        mint=mint,
        venue_program_id=_PUMP_PID,
        source=LaunchSource.PUMPFUN,
        event_time=EventTime(
            slot=slot,
            block_time_ms=block_time_ms,
            wall_clock_ms=block_time_ms,
        ),
        observation_slot=slot,
        confirmation_slot=slot,
        detection_transport=DetectionTransport.GEYSER,
        sol_reserve_lamports=30_000_000_000,
        token_reserve_base=1_073_000_191,
        token_decimals=6,
        initial_holders=1,
        competitors=0,
        creator_wallet="Creator111111111111111111111111111111111111",
        bundler_cluster_id=None,
        deploy_template_fingerprint=None,
        data_staleness_ms=50,
    )


def _make_recorder() -> ShadowRecorder:
    return ShadowRecorder(InMemoryParquetBackend(), first_k_slots=10, max_open_windows=100)


class TestShadowRecorderQuoteMintGuard:
    """
    ShadowRecorder.observe() must drop events whose mint is in QUOTE_MINTS
    (defense-in-depth T-QUOTE-GUARD).

    Mutation-meaningful: removing the ``if mint in QUOTE_MINTS`` guard from
    ShadowRecorder.observe() makes these tests fail.
    """

    def test_usdc_mint_opens_no_window(self):
        """observe() with mint=USDC must NOT open a snapshot window."""
        recorder = _make_recorder()
        recorder.observe(_make_event(USDC_MINT), EventKind.CREATE)
        assert len(recorder._open) == 0, (
            "ShadowRecorder must not open a window for a quote mint (USDC)"
        )

    def test_usdc_mint_increments_orphan_counter(self):
        """observe() with mint=USDC increments orphan_events_total (observable metric)."""
        recorder = _make_recorder()
        recorder.observe(_make_event(USDC_MINT), EventKind.CREATE)
        assert recorder.orphan_events_total == 1, (
            "orphan_events_total must increment for a quote-mint event "
            "(T-QUOTE-GUARD defense-in-depth)"
        )

    def test_wsol_mint_opens_no_window(self):
        """observe() with mint=WSOL must NOT open a snapshot window."""
        recorder = _make_recorder()
        recorder.observe(_make_event(WSOL_MINT), EventKind.CREATE)
        assert len(recorder._open) == 0

    def test_wsol_mint_increments_orphan_counter(self):
        """observe() with mint=WSOL increments orphan_events_total."""
        recorder = _make_recorder()
        recorder.observe(_make_event(WSOL_MINT), EventKind.WITHDRAW)
        assert recorder.orphan_events_total == 1

    def test_usdt_mint_opens_no_window(self):
        """observe() with mint=USDT must NOT open a snapshot window."""
        recorder = _make_recorder()
        recorder.observe(_make_event(USDT_MINT), EventKind.INIT)
        assert len(recorder._open) == 0

    def test_real_mint_still_opens_window(self):
        """A normal (non-quote) mint must still open a window — no regression."""
        recorder = _make_recorder()
        recorder.observe(_make_event(REAL_MINT), EventKind.CREATE)
        assert len(recorder._open) == 1, (
            "Normal mint must still open a window (no regression from quote-mint guard)"
        )
        assert recorder.orphan_events_total == 0

    def test_quote_mint_not_recorded_in_snapshot_store(self):
        """A quote-mint event must not appear in the snapshot store after flush."""
        recorder = _make_recorder()
        recorder.observe(_make_event(USDC_MINT), EventKind.CREATE)
        recorder.flush_all(status="complete")
        snapshots = recorder.completed_snapshots()
        mints_recorded = [s.get("mint") for s in snapshots]
        assert USDC_MINT not in mints_recorded, (
            "USDC must never appear in the recorded snapshot corpus"
        )

    def test_multiple_quote_mints_all_dropped(self):
        """All three quote mints are dropped; orphan counter reflects the total."""
        recorder = _make_recorder()
        for mint in (USDC_MINT, USDT_MINT, WSOL_MINT):
            recorder.observe(_make_event(mint), EventKind.CREATE)
        assert len(recorder._open) == 0
        assert recorder.orphan_events_total == 3

    def test_quote_mint_guard_fires_before_window_open_check(self):
        """Quote-mint guard drops the event even for INIT and WITHDRAW kinds."""
        recorder = _make_recorder()
        recorder.observe(_make_event(USDC_MINT), EventKind.INIT)
        recorder.observe(_make_event(WSOL_MINT), EventKind.WITHDRAW)
        assert len(recorder._open) == 0
        assert recorder.orphan_events_total == 2
