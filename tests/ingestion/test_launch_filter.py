"""T-LAUNCH-FILTER unit tests — ShadowRecorder only opens windows on genuine launches.

Requirements tested (task T-LAUNCH-FILTER ACs):

(a) create event opens exactly one window keyed on the base mint.
(b) buy/sell with no prior create opens NO window and is counted as an orphan.
(c) buy/sell arriving AFTER a create is attributed to that window.
(d) create where the quote side is USDC/SOL still keys on the BASE mint
    (requirement 3: window key = new-base-token mint, never the quote mint).
(e) T-300a preservation: create/buy with absent block_time returns None from
    decoder; ShadowRecorder is never called (no wall-clock pollution).

Point-in-time correctness law: no stored field is derived from wall-clock time.
These tests run fully offline — no network, no Redis, no real Parquet I/O.
"""

from __future__ import annotations

from aats.contracts.events import DetectionTransport, EventTime, LaunchEvent, LaunchSource
from aats.ingestion.decoders import (
    WINDOW_OPENING_KINDS,
    EventKind,
    PumpFunDecoder,
    RawTransaction,
)
from aats.ingestion.store import InMemoryParquetBackend, ShadowRecorder
from tests.ingestion.fixtures import (
    PROGRAM_IDS,
    make_pumpfun_buy_tx,
    make_pumpfun_create_tx,
    make_pumpfun_sell_tx,
    make_registry,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
WSOL_MINT = "So11111111111111111111111111111111111111112"
BASE_MINT  = "MintPumpFun1111111111111111111111111111111111"  # from fixtures


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event(
    mint: str,
    slot: int,
    source: LaunchSource = LaunchSource.PUMPFUN,
) -> LaunchEvent:
    """Build a minimal valid LaunchEvent for ShadowRecorder tests.

    block_time_ms = slot * 400 (Solana ~400 ms/slot, deterministic for tests).
    wall_clock_ms = block_time_ms (exact — tests do NOT rely on this for anything
    except satisfying the EventTime validator; it is NEVER used as a join key).
    """
    block_time_ms = slot * 400  # deterministic, no wall-clock call
    return LaunchEvent(
        mint=mint,
        venue_program_id=PROGRAM_IDS[source],
        source=source,
        event_time=EventTime(
            slot=slot,
            block_time_ms=block_time_ms,
            wall_clock_ms=block_time_ms,
        ),
        observation_slot=slot,
        confirmation_slot=slot,
        detection_transport=DetectionTransport.GEYSER,
        sol_reserve_lamports=1_000_000_000,
        token_reserve_base=1_000_000_000_000,
        token_decimals=6,
        initial_holders=1,
        competitors=0,
        creator_wallet="CreatorWallet11111111111111111111111111111111",
        bundler_cluster_id=None,
        deploy_template_fingerprint=None,
        data_staleness_ms=50,
    )


def _make_recorder() -> ShadowRecorder:
    return ShadowRecorder(InMemoryParquetBackend(), first_k_slots=10, max_open_windows=100)


# ---------------------------------------------------------------------------
# (a) create event opens exactly one window keyed on base mint
# ---------------------------------------------------------------------------


class TestWindowOpenOnCreate:
    def test_create_opens_exactly_one_window(self):
        """A single CREATE event for a mint opens exactly one snapshot window."""
        recorder = _make_recorder()
        ev = _make_event(mint=BASE_MINT, slot=300_000_000)

        recorder.observe(ev, EventKind.CREATE)

        # Exactly one window open
        assert len(recorder._open) == 1, "Expected exactly one open window after create"

    def test_window_keyed_on_base_mint(self):
        """The window dict key equals the base mint from the create event."""
        recorder = _make_recorder()
        ev = _make_event(mint=BASE_MINT, slot=300_000_000)

        recorder.observe(ev, EventKind.CREATE)

        assert BASE_MINT in recorder._open, (
            f"Window should be keyed on base mint {BASE_MINT!r}"
        )

    def test_create_event_recorded_in_window(self):
        """The create event itself is appended as the first entry in the window."""
        recorder = _make_recorder()
        ev = _make_event(mint=BASE_MINT, slot=300_000_000)

        recorder.observe(ev, EventKind.CREATE)

        snap = recorder._open[BASE_MINT]
        assert len(snap.events) == 1
        assert snap.events[0]["mint"] == BASE_MINT

    def test_second_create_for_same_mint_does_not_duplicate_window(self):
        """Two create events for the same mint reuse the existing window (idempotent open)."""
        recorder = _make_recorder()
        ev1 = _make_event(mint=BASE_MINT, slot=300_000_000)
        ev2 = _make_event(mint=BASE_MINT, slot=300_000_001)

        recorder.observe(ev1, EventKind.CREATE)
        recorder.observe(ev2, EventKind.CREATE)

        # Still only one window
        assert len(recorder._open) == 1
        # Both events appended
        snap = recorder._open[BASE_MINT]
        assert len(snap.events) == 2

    def test_init_kind_also_opens_window(self):
        """EventKind.INIT (Raydium/PumpSwap) also opens a window (window-opening set)."""
        recorder = _make_recorder()
        ev = _make_event(mint="RaydiumNewMint1111111111111111111111111111", slot=300_000_100)

        recorder.observe(ev, EventKind.INIT)

        assert len(recorder._open) == 1

    def test_withdraw_kind_also_opens_window(self):
        """EventKind.WITHDRAW (pump.fun migration) also opens a window."""
        recorder = _make_recorder()
        ev = _make_event(mint="MigrationMint111111111111111111111111111111", slot=300_000_200)

        recorder.observe(ev, EventKind.WITHDRAW)

        assert len(recorder._open) == 1

    def test_window_opening_kinds_set(self):
        """WINDOW_OPENING_KINDS must contain CREATE, INIT, WITHDRAW and nothing else."""
        assert EventKind.CREATE in WINDOW_OPENING_KINDS
        assert EventKind.INIT in WINDOW_OPENING_KINDS
        assert EventKind.WITHDRAW in WINDOW_OPENING_KINDS
        # Confirm the non-opening kinds are excluded
        assert EventKind.BUY not in WINDOW_OPENING_KINDS
        assert EventKind.SELL not in WINDOW_OPENING_KINDS
        assert EventKind.SWAP not in WINDOW_OPENING_KINDS


# ---------------------------------------------------------------------------
# (b) buy/sell with no prior create opens NO window (orphan-counted)
# ---------------------------------------------------------------------------


class TestOrphanBuySell:
    def test_buy_before_create_opens_no_window(self):
        """A BUY arriving before any CREATE for that mint must NOT open a window."""
        recorder = _make_recorder()
        ev = _make_event(mint=BASE_MINT, slot=300_000_001)

        recorder.observe(ev, EventKind.BUY)

        assert len(recorder._open) == 0, (
            "BUY event with no prior CREATE must not open a window"
        )

    def test_sell_before_create_opens_no_window(self):
        """A SELL arriving before any CREATE for that mint must NOT open a window."""
        recorder = _make_recorder()
        ev = _make_event(mint=BASE_MINT, slot=300_000_002)

        recorder.observe(ev, EventKind.SELL)

        assert len(recorder._open) == 0, (
            "SELL event with no prior CREATE must not open a window"
        )

    def test_swap_before_create_opens_no_window(self):
        """A SWAP arriving before any CREATE must NOT open a window."""
        recorder = _make_recorder()
        ev = _make_event(mint=BASE_MINT, slot=300_000_003)

        recorder.observe(ev, EventKind.SWAP)

        assert len(recorder._open) == 0

    def test_buy_before_create_increments_orphan_counter(self):
        """orphan_events_total must increment for each trade event with no window."""
        recorder = _make_recorder()

        recorder.observe(_make_event(mint=BASE_MINT, slot=300_000_001), EventKind.BUY)
        recorder.observe(_make_event(mint=BASE_MINT, slot=300_000_002), EventKind.SELL)
        recorder.observe(_make_event(mint="OtherMint111111111111111111111111111111111", slot=300_000_003), EventKind.BUY)

        assert recorder.orphan_events_total == 3

    def test_orphan_counter_starts_at_zero(self):
        recorder = _make_recorder()
        assert recorder.orphan_events_total == 0

    def test_create_does_not_increment_orphan_counter(self):
        """CREATE events must NOT increment the orphan counter."""
        recorder = _make_recorder()
        recorder.observe(_make_event(mint=BASE_MINT, slot=300_000_000), EventKind.CREATE)

        assert recorder.orphan_events_total == 0


# ---------------------------------------------------------------------------
# (c) buy/sell AFTER create is attributed to that window
# ---------------------------------------------------------------------------


class TestAttributionAfterCreate:
    def test_buy_after_create_appended_to_window(self):
        """A BUY arriving after a CREATE for the same mint is appended to the window."""
        recorder = _make_recorder()
        create_ev = _make_event(mint=BASE_MINT, slot=300_000_000)
        buy_ev    = _make_event(mint=BASE_MINT, slot=300_000_002)

        recorder.observe(create_ev, EventKind.CREATE)
        recorder.observe(buy_ev,    EventKind.BUY)

        snap = recorder._open[BASE_MINT]
        assert len(snap.events) == 2, "CREATE + BUY must both be in the window"

    def test_sell_after_create_appended_to_window(self):
        """A SELL arriving after a CREATE is appended to the window."""
        recorder = _make_recorder()
        create_ev = _make_event(mint=BASE_MINT, slot=300_000_000)
        sell_ev   = _make_event(mint=BASE_MINT, slot=300_000_003)

        recorder.observe(create_ev, EventKind.CREATE)
        recorder.observe(sell_ev,   EventKind.SELL)

        snap = recorder._open[BASE_MINT]
        assert len(snap.events) == 2

    def test_buy_after_create_does_not_increment_orphan_counter(self):
        """A BUY that is attributed to a window must NOT touch the orphan counter."""
        recorder = _make_recorder()
        recorder.observe(_make_event(mint=BASE_MINT, slot=300_000_000), EventKind.CREATE)
        recorder.observe(_make_event(mint=BASE_MINT, slot=300_000_002), EventKind.BUY)

        assert recorder.orphan_events_total == 0

    def test_buy_outside_k_slot_window_flushes(self):
        """A BUY arriving past slot + first_k_slots triggers a flush (window complete)."""
        recorder = _make_recorder()
        first_k_slots = 10
        recorder = ShadowRecorder(
            InMemoryParquetBackend(), first_k_slots=first_k_slots, max_open_windows=100
        )
        create_slot = 300_000_000
        recorder.observe(_make_event(mint=BASE_MINT, slot=create_slot), EventKind.CREATE)

        # BUY at slot + K + 1 — beyond the window
        late_buy = _make_event(mint=BASE_MINT, slot=create_slot + first_k_slots + 1)
        recorder.observe(late_buy, EventKind.BUY)

        # Window should be flushed (closed) — not still open
        assert BASE_MINT not in recorder._open, (
            "Window should have been flushed when a late event arrived past K slots"
        )

    def test_multiple_mints_independent_windows(self):
        """CREATE events for distinct mints create independent windows."""
        recorder = _make_recorder()
        mint_a = "MintAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        mint_b = "MintBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"

        recorder.observe(_make_event(mint=mint_a, slot=300_000_000), EventKind.CREATE)
        recorder.observe(_make_event(mint=mint_b, slot=300_000_001), EventKind.CREATE)
        recorder.observe(_make_event(mint=mint_a, slot=300_000_002), EventKind.BUY)

        assert len(recorder._open) == 2
        assert len(recorder._open[mint_a].events) == 2  # create + buy
        assert len(recorder._open[mint_b].events) == 1  # create only


# ---------------------------------------------------------------------------
# (d) quote side is USDC/SOL — window keyed on BASE mint, not quote
# ---------------------------------------------------------------------------


class TestBaseMintNotQuote:
    """Verify that the decoder extracts the base mint, not the USDC/SOL quote mint.

    We test this at the decoder level (PumpSwap create_pool) and at the
    ShadowRecorder level (the key is never a known quote mint).
    """

    def test_pumpswap_create_pool_mint_is_not_usdc(self):
        """PumpSwapDecoder must key the event on the BASE mint, not USDC."""
        from aats.ingestion.decoders import PumpSwapDecoder
        from tests.ingestion.fixtures import make_pumpswap_create_pool_tx

        registry = make_registry()
        decoder = PumpSwapDecoder(registry)
        tx = make_pumpswap_create_pool_tx()
        result = decoder.decode(tx, DetectionTransport.GEYSER)
        assert result is not None
        ev, kind = result

        # Base mint is at accounts[3] in fixture; USDC is not a real key there
        assert ev.mint != USDC_MINT, (
            f"Window mint must be the BASE mint, not USDC ({USDC_MINT})"
        )
        assert ev.mint != WSOL_MINT, (
            f"Window mint must be the BASE mint, not WSOL ({WSOL_MINT})"
        )
        # Verify the exact fixture base mint
        assert ev.mint == "BaseMintPumpSwap11111111111111111111111111111"

    def test_pumpswap_create_pool_returns_init_kind(self):
        """PumpSwapDecoder.decode() must return EventKind.INIT for create_pool."""
        from aats.ingestion.decoders import PumpSwapDecoder
        from tests.ingestion.fixtures import make_pumpswap_create_pool_tx

        registry = make_registry()
        decoder = PumpSwapDecoder(registry)
        tx = make_pumpswap_create_pool_tx()
        result = decoder.decode(tx, DetectionTransport.GEYSER)
        assert result is not None
        _, kind = result
        assert kind == EventKind.INIT

    def test_shadow_recorder_window_key_is_base_mint(self):
        """When a create_pool event is observed, the window key is the BASE mint."""
        from aats.ingestion.decoders import PumpSwapDecoder
        from tests.ingestion.fixtures import make_pumpswap_create_pool_tx

        registry = make_registry()
        decoder = PumpSwapDecoder(registry)
        tx = make_pumpswap_create_pool_tx()
        result = decoder.decode(tx, DetectionTransport.GEYSER)
        assert result is not None
        ev, kind = result

        recorder = _make_recorder()
        recorder.observe(ev, kind)

        # The window key must be the base mint (accounts[3] in the PumpSwap fixture)
        assert ev.mint in recorder._open
        assert USDC_MINT not in recorder._open
        assert WSOL_MINT not in recorder._open

    def test_raydium_v4_window_key_is_not_sol(self):
        """Raydium v4 initialize2 decoder picks the NON-SOL mint as the base."""
        from aats.ingestion.decoders import RaydiumV4Decoder
        from tests.ingestion.fixtures import make_raydium_v4_init2_tx

        registry = make_registry()
        decoder = RaydiumV4Decoder(registry)
        tx = make_raydium_v4_init2_tx()
        result = decoder.decode(tx, DetectionTransport.GEYSER)
        assert result is not None
        ev, kind = result

        assert ev.mint != WSOL_MINT, (
            "Raydium v4 decoder must select the non-SOL mint as the base"
        )
        assert kind == EventKind.INIT


# ---------------------------------------------------------------------------
# (e) T-300a: absent block_time → decoder returns None (no wall-clock pollution)
# ---------------------------------------------------------------------------


class TestT300aAbsentBlockTime:
    """T-300a point-in-time correctness preservation.

    When block_time_unix_s is None, the decoder must return None.
    The ShadowRecorder must never be called with a wall-clock-derived event.
    """

    def _no_block_time(self, tx: RawTransaction) -> RawTransaction:
        """Return a copy of tx with block_time_unix_s=None."""
        return RawTransaction(
            signature=tx.signature + "_nobt",
            slot=tx.slot,
            block_time_unix_s=None,
            fee_payer=tx.fee_payer,
            instructions=tx.instructions,
            inner_instructions=tx.inner_instructions,
            program_logs=tx.program_logs,
        )

    def test_create_with_no_block_time_returns_none(self):
        """PumpFunDecoder must return None for a create tx with absent block_time."""
        registry = make_registry()
        decoder = PumpFunDecoder(registry)
        tx = self._no_block_time(make_pumpfun_create_tx())

        result = decoder.decode(tx, DetectionTransport.GEYSER)

        assert result is None, (
            "Decoder must return None when block_time_unix_s is absent (T-300a)"
        )

    def test_buy_with_no_block_time_returns_none(self):
        """PumpFunDecoder must return None for a buy tx with absent block_time."""
        registry = make_registry()
        decoder = PumpFunDecoder(registry)
        tx = self._no_block_time(make_pumpfun_buy_tx())

        result = decoder.decode(tx, DetectionTransport.GEYSER)

        assert result is None, (
            "Decoder must return None when block_time_unix_s is absent (T-300a)"
        )

    def test_sell_with_no_block_time_returns_none(self):
        """PumpFunDecoder must return None for a sell tx with absent block_time."""
        registry = make_registry()
        decoder = PumpFunDecoder(registry)
        tx = self._no_block_time(make_pumpfun_sell_tx())

        result = decoder.decode(tx, DetectionTransport.GEYSER)

        assert result is None

    def test_no_window_opened_when_block_time_absent(self):
        """If the decoder returns None (absent block_time), no window must be opened.

        Callers in the transport pipeline skip observe() when decode() returns None,
        so the ShadowRecorder is never polluted with wall-clock-derived data.
        """
        registry = make_registry()
        decoder = PumpFunDecoder(registry)
        recorder = _make_recorder()

        tx = self._no_block_time(make_pumpfun_create_tx())
        result = decoder.decode(tx, DetectionTransport.GEYSER)

        # Pipeline guard: only call observe when result is not None
        if result is not None:
            ev, kind = result
            recorder.observe(ev, kind)

        assert len(recorder._open) == 0, (
            "No window must be opened when block_time is absent (T-300a guard)"
        )
        assert recorder.orphan_events_total == 0, (
            "Absent block_time events must not be counted as orphans either"
        )

    def test_block_time_zero_treated_as_absent(self):
        """block_time_unix_s=0 is treated identically to None (T-300a)."""
        registry = make_registry()
        decoder = PumpFunDecoder(registry)

        tx = make_pumpfun_create_tx()
        tx_zero = RawTransaction(
            signature=tx.signature + "_zerbt",
            slot=tx.slot,
            block_time_unix_s=0,
            fee_payer=tx.fee_payer,
            instructions=tx.instructions,
            inner_instructions=tx.inner_instructions,
            program_logs=tx.program_logs,
        )

        result = decoder.decode(tx_zero, DetectionTransport.GEYSER)

        assert result is None, (
            "block_time_unix_s=0 must be treated as absent (T-300a, not wall-clock)"
        )


# ---------------------------------------------------------------------------
# (bonus) EventKind discriminator propagation end-to-end
# ---------------------------------------------------------------------------


class TestEventKindPropagation:
    """Verify EventKind is correctly assigned by each decoder path."""

    def test_pumpfun_create_returns_create_kind(self):
        registry = make_registry()
        decoder = PumpFunDecoder(registry)
        result = decoder.decode(make_pumpfun_create_tx(), DetectionTransport.GEYSER)
        assert result is not None
        _, kind = result
        assert kind == EventKind.CREATE

    def test_pumpfun_buy_returns_buy_kind(self):
        registry = make_registry()
        decoder = PumpFunDecoder(registry)
        result = decoder.decode(make_pumpfun_buy_tx(), DetectionTransport.GEYSER)
        assert result is not None
        _, kind = result
        assert kind == EventKind.BUY

    def test_pumpfun_sell_returns_sell_kind(self):
        registry = make_registry()
        decoder = PumpFunDecoder(registry)
        result = decoder.decode(make_pumpfun_sell_tx(), DetectionTransport.GEYSER)
        assert result is not None
        _, kind = result
        assert kind == EventKind.SELL

    def test_buy_kind_is_not_window_opening(self):
        """EventKind.BUY must NOT be in WINDOW_OPENING_KINDS."""
        assert EventKind.BUY not in WINDOW_OPENING_KINDS

    def test_sell_kind_is_not_window_opening(self):
        """EventKind.SELL must NOT be in WINDOW_OPENING_KINDS."""
        assert EventKind.SELL not in WINDOW_OPENING_KINDS
