"""Tests for the first-K-slot buy pressure / volume feature (T-305).

Self-check criteria (charter §6, M1 standards):
  1. Correct values on known fixtures (exact Decimal/int arithmetic).
  2. Lookahead-injection test PROVES the function refuses future data
     (FeatureWindowError raised; the feature value at t is unchanged).
  3. Provenance cutoff is correct (max_source_slot <= event_time.slot + K).
  4. Batch == streaming parity (BuyPressureAccumulator vs build_buy_pressure_features).
  5. Money fields are int/Decimal, NEVER float.
  6. Empty window returns zeros (no fabrication of non-observed data).
  7. Schema conformance: BuyPressureFeatures fields match FeatureFrame expectations.
  8. FeatureProvenance guard fires as second defence when max_source_slot > cutoff.
  9. [B-1 FIX] Real-decoder create reserve=0: sell correctly counted as sell.
     (With pool_creation sol_reserve_lamports=0 — the real pump.fun decoder value —
     a genuine sell MUST produce first_k_sell_count=1, first_k_buy_pressure < 0.)
  10. [B-1 FIX] classify_event_direction raises ClassificationUndefinedError when
      creation reserve == 0 (prevents silent all-BUY degeneration on real decoder
      output).
  11. [M-1 FIX] Pre-creation events (slot < creation slot) are REJECTED by observe()
      with FeatureWindowError.  A stray earlier-slot event must not silently pollute
      the accumulator.

Point-in-time correctness is the prime directive (charter M1 §Standards).
Any test that accepts a future-slot event silently is itself a bug.

API CHANGE (B-1 fix): build_buy_pressure_features now accepts
  classified_events: Sequence[tuple[LaunchEvent, bool]]
instead of Sequence[LaunchEvent].  Direction (is_buy) must be supplied by the
caller from the instruction discriminator — not inferred from sol_reserve_lamports.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from aats.contracts.events import DetectionTransport, EventTime, LaunchEvent, LaunchSource
from aats.contracts.provenance import ProvenanceTaintError
from aats.features.buy_pressure import (
    FEATURE_SCHEMA_VERSION,
    BuyPressureAccumulator,
    BuyPressureFeatures,
    ClassificationUndefinedError,
    FeatureWindowError,
    build_buy_pressure_features,
    classify_event_direction,
)

# ---------------------------------------------------------------------------
# Test fixtures — deterministic, no wall-clock, no network
# ---------------------------------------------------------------------------

BASE_SLOT = 300_000_000
BASE_BLOCK_TIME_MS = 1_718_700_000_000  # ~June 2024 in ms
LAMPORTS_PER_SOL = 1_000_000_000

FIRST_K_SLOTS = 10  # 10-slot window for these tests
MINT_A = "MintAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
WALLET_BUYER_1 = "BuyerWallet111111111111111111111111111111111"
WALLET_BUYER_2 = "BuyerWallet222222222222222222222222222222222"
WALLET_SELLER_1 = "SellerWallet11111111111111111111111111111111"


def _make_event_time(slot: int, offset_ms: int = 0) -> EventTime:
    """Create an EventTime for a given slot.  block_time_ms is authoritative (C-5)."""
    # ~400 ms per slot on Solana
    return EventTime(
        slot=slot,
        block_time_ms=BASE_BLOCK_TIME_MS + (slot - BASE_SLOT) * 400 + offset_ms,
        wall_clock_ms=BASE_BLOCK_TIME_MS + (slot - BASE_SLOT) * 400 + offset_ms + 50,
    )


def _make_launch_event(
    slot: int,
    sol_reserve_lamports: int,
    creator_wallet: str = WALLET_BUYER_1,
    mint: str = MINT_A,
) -> LaunchEvent:
    """Create a minimal LaunchEvent for testing."""
    return LaunchEvent(
        mint=mint,
        venue_program_id="6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",
        source=LaunchSource.PUMPFUN,
        event_time=_make_event_time(slot),
        observation_slot=slot,
        confirmation_slot=slot,
        detection_transport=DetectionTransport.GEYSER,
        sol_reserve_lamports=sol_reserve_lamports,
        token_reserve_base=1_000_000_000,
        token_decimals=6,
        initial_holders=0,
        competitors=0,
        creator_wallet=creator_wallet,
        bundler_cluster_id=None,
        deploy_template_fingerprint=None,
        data_staleness_ms=50,
    )


# Pool creation event — SYNTHETIC FIXTURE with non-zero reserve for tests that
# use classify_event_direction.  The REAL decoder emits sol_reserve_lamports=0
# for create events; those tests use POOL_CREATION_EVENT_REAL_DECODER below.
POOL_CREATION_EVENT = _make_launch_event(
    slot=BASE_SLOT,
    sol_reserve_lamports=30 * LAMPORTS_PER_SOL,  # synthetic: 30 SOL initial reserve
    creator_wallet=WALLET_BUYER_1,
)

# Real-decoder pool creation event — sol_reserve_lamports=0, as emitted by
# decoders.py _try_create (decoders.py:324).  This is what ships on the real
# ingestion path.  Tests that verify B-1 behaviour use this fixture.
POOL_CREATION_EVENT_REAL_DECODER = _make_launch_event(
    slot=BASE_SLOT,
    sol_reserve_lamports=0,  # REAL decoder value — create events emit 0
    creator_wallet=WALLET_BUYER_1,
)


# ---------------------------------------------------------------------------
# 1. Correct values on known fixtures
# ---------------------------------------------------------------------------


class TestCorrectValuesOnFixtures:
    """Verify exact arithmetic on deterministic fixtures.

    Note: these tests use pre-classified (event, is_buy) pairs — the fixed
    API that works correctly regardless of the creation reserve value.
    """

    def test_empty_window_returns_zeros(self):
        """Zero events → zero pressure, zero volume, zero counts."""
        result = build_buy_pressure_features(
            pool_creation_event=POOL_CREATION_EVENT,
            classified_events=[],
            first_k_slots=FIRST_K_SLOTS,
        )
        assert result.first_k_buy_pressure == Decimal(0)
        assert result.first_k_volume_lamports == 0
        assert result.first_k_buy_count == 0
        assert result.first_k_sell_count == 0
        assert result.first_k_unique_buyers == 0

    def test_single_buy_event_positive_pressure(self):
        """One buy event: pressure = buy_sol, volume = buy_sol."""
        buy_event = _make_launch_event(
            slot=BASE_SLOT + 1,
            sol_reserve_lamports=5 * LAMPORTS_PER_SOL,  # 5 SOL buy cost
            creator_wallet=WALLET_BUYER_1,
        )
        result = build_buy_pressure_features(
            pool_creation_event=POOL_CREATION_EVENT_REAL_DECODER,
            classified_events=[(buy_event, True)],  # is_buy=True from discriminator
            first_k_slots=FIRST_K_SLOTS,
        )
        assert result.first_k_buy_count == 1
        assert result.first_k_sell_count == 0
        assert result.first_k_buy_pressure == Decimal(5 * LAMPORTS_PER_SOL)
        assert result.first_k_volume_lamports == 5 * LAMPORTS_PER_SOL
        assert result.first_k_unique_buyers == 1

    def test_single_sell_event_negative_pressure(self):
        """One sell event: pressure = -sell_sol, volume = sell_sol."""
        sell_amount = 3 * LAMPORTS_PER_SOL
        sell_event = _make_launch_event(
            slot=BASE_SLOT + 2,
            sol_reserve_lamports=sell_amount,  # 3 SOL output
            creator_wallet=WALLET_SELLER_1,
        )
        result = build_buy_pressure_features(
            pool_creation_event=POOL_CREATION_EVENT_REAL_DECODER,
            classified_events=[(sell_event, False)],  # is_buy=False from discriminator
            first_k_slots=FIRST_K_SLOTS,
        )
        assert result.first_k_buy_count == 0
        assert result.first_k_sell_count == 1
        assert result.first_k_buy_pressure == Decimal(-sell_amount)
        assert result.first_k_volume_lamports == sell_amount

    def test_buy_and_sell_net_pressure(self):
        """Buy 5 SOL, sell 3 SOL → net pressure = +2 SOL, volume = 8 SOL."""
        buy_amount = 5 * LAMPORTS_PER_SOL
        sell_amount = 3 * LAMPORTS_PER_SOL

        buy_event = _make_launch_event(
            slot=BASE_SLOT + 1,
            sol_reserve_lamports=buy_amount,
            creator_wallet=WALLET_BUYER_1,
        )
        sell_event = _make_launch_event(
            slot=BASE_SLOT + 2,
            sol_reserve_lamports=sell_amount,
            creator_wallet=WALLET_SELLER_1,
        )

        result = build_buy_pressure_features(
            pool_creation_event=POOL_CREATION_EVENT_REAL_DECODER,
            classified_events=[(buy_event, True), (sell_event, False)],
            first_k_slots=FIRST_K_SLOTS,
        )
        assert result.first_k_buy_pressure == Decimal(buy_amount) - Decimal(sell_amount)
        assert result.first_k_buy_pressure == Decimal(2 * LAMPORTS_PER_SOL)
        assert result.first_k_volume_lamports == buy_amount + sell_amount
        assert result.first_k_volume_lamports == 8 * LAMPORTS_PER_SOL
        assert result.first_k_buy_count == 1
        assert result.first_k_sell_count == 1

    def test_multiple_buys_from_different_wallets(self):
        """Three buys from two distinct wallets → unique_buyers = 2."""
        events = [
            (_make_launch_event(
                slot=BASE_SLOT + 1,
                sol_reserve_lamports=3 * LAMPORTS_PER_SOL,
                creator_wallet=WALLET_BUYER_1,
            ), True),
            (_make_launch_event(
                slot=BASE_SLOT + 3,
                sol_reserve_lamports=2 * LAMPORTS_PER_SOL,
                creator_wallet=WALLET_BUYER_2,
            ), True),
            (_make_launch_event(
                slot=BASE_SLOT + 5,
                sol_reserve_lamports=1 * LAMPORTS_PER_SOL,
                creator_wallet=WALLET_BUYER_1,  # same wallet as first buy
            ), True),
        ]
        result = build_buy_pressure_features(
            pool_creation_event=POOL_CREATION_EVENT_REAL_DECODER,
            classified_events=events,
            first_k_slots=FIRST_K_SLOTS,
        )
        assert result.first_k_buy_count == 3
        assert result.first_k_unique_buyers == 2  # WALLET_BUYER_1 counted once
        assert result.first_k_sell_count == 0

    def test_event_at_exact_cutoff_slot_is_included(self):
        """An event at slot = event_time + K is WITHIN the window (inclusive cutoff)."""
        cutoff_event = _make_launch_event(
            slot=BASE_SLOT + FIRST_K_SLOTS,  # exactly at K — must be accepted
            sol_reserve_lamports=5 * LAMPORTS_PER_SOL,
        )
        # Must NOT raise
        result = build_buy_pressure_features(
            pool_creation_event=POOL_CREATION_EVENT_REAL_DECODER,
            classified_events=[(cutoff_event, True)],
            first_k_slots=FIRST_K_SLOTS,
        )
        assert result.first_k_buy_count == 1
        assert result.max_source_slot == BASE_SLOT + FIRST_K_SLOTS

    def test_event_count_zero_volume_is_zero_not_nan(self):
        """Empty window must produce 0, not NaN or None — no fabrication."""
        result = build_buy_pressure_features(
            pool_creation_event=POOL_CREATION_EVENT_REAL_DECODER,
            classified_events=[],
            first_k_slots=FIRST_K_SLOTS,
        )
        # Explicit type and value checks
        assert isinstance(result.first_k_buy_pressure, Decimal)
        assert isinstance(result.first_k_volume_lamports, int)
        assert result.first_k_buy_pressure == Decimal("0")
        assert result.first_k_volume_lamports == 0

    def test_exact_cancellation_buy_equals_sell(self):
        """A buy and a sell of equal size must net to EXACTLY zero (Decimal arithmetic)."""
        buy_amount = 40 * LAMPORTS_PER_SOL
        # Direct accumulator test for exact cancellation scenario
        acc = BuyPressureAccumulator(
            mint=MINT_A,
            event_time_slot=BASE_SLOT,
            first_k_slots=FIRST_K_SLOTS,
        )
        # Manually observe a buy and a sell of equal magnitude
        acc._net_buy_lamports += Decimal(buy_amount)
        acc._volume_lamports += buy_amount
        acc._buy_count += 1
        acc._buyer_wallets.add(WALLET_BUYER_1)
        acc._max_source_slot = BASE_SLOT + 1
        acc._event_count += 1

        acc._net_buy_lamports -= Decimal(buy_amount)  # exact cancellation
        acc._volume_lamports += buy_amount
        acc._sell_count += 1
        acc._max_source_slot = BASE_SLOT + 2
        acc._event_count += 1

        feat = acc.to_features(POOL_CREATION_EVENT.event_time)
        # With Decimal arithmetic, this MUST be exactly zero (no float rounding)
        assert feat.first_k_buy_pressure == Decimal(0)
        assert feat.first_k_buy_pressure != Decimal("1E-18")  # not a float artifact


# ---------------------------------------------------------------------------
# 9. [B-1 FIX] Real-decoder create reserve=0 correctness tests
# ---------------------------------------------------------------------------


class TestRealDecoderReserveZero:
    """BLOCKER B-1 regression tests.

    The real pump.fun decoder (decoders.py:324) emits sol_reserve_lamports=0 for
    create events.  These tests verify:
      - A genuine SELL with the real-decoder creation anchor (reserve=0) correctly
        produces first_k_sell_count=1 and negative net pressure.
      - A BUY+SELL pair yields correct net pressure (+2 SOL, not +8 SOL gross).
      - The pre-classified API works correctly with reserve=0.

    These tests MUST PASS.  If they fail, the C-4 GATE-B naive-momentum baseline
    is not constructible and the headline acceptance metric is corrupted.
    """

    def test_sell_with_real_decoder_create_reserve_is_counted_as_sell(self):
        """A genuine sell with create reserve=0 MUST produce sell_count=1, not 0.

        This is the exact repro from the review finding (B-1):
          - pool_creation sol_reserve_lamports=0  (real decoder value)
          - one BUY event at 5 SOL
          - one SELL event at 3 SOL
          Expected: buy_count=1, sell_count=1, net_pressure=+2 SOL
          Old code: buy_count=2, sell_count=0, net_pressure=+8 SOL (WRONG)
        """
        buy_event = _make_launch_event(
            slot=BASE_SLOT + 1,
            sol_reserve_lamports=5 * LAMPORTS_PER_SOL,
            creator_wallet=WALLET_BUYER_1,
        )
        sell_event = _make_launch_event(
            slot=BASE_SLOT + 2,
            sol_reserve_lamports=3 * LAMPORTS_PER_SOL,
            creator_wallet=WALLET_SELLER_1,
        )

        result = build_buy_pressure_features(
            pool_creation_event=POOL_CREATION_EVENT_REAL_DECODER,  # reserve=0
            classified_events=[
                (buy_event, True),   # direction from PUMP_DISC_BUY discriminator
                (sell_event, False),  # direction from PUMP_DISC_SELL discriminator
            ],
            first_k_slots=FIRST_K_SLOTS,
        )

        # C-4 assertion: net pressure MUST distinguish a net-buying from net-selling token.
        assert result.first_k_buy_count == 1, (
            f"Expected buy_count=1, got {result.first_k_buy_count}"
        )
        assert result.first_k_sell_count == 1, (
            f"Expected sell_count=1, got {result.first_k_sell_count}.  "
            "B-1 BLOCKER: sell classified as buy when creation reserve=0."
        )
        assert result.first_k_buy_pressure == Decimal(2 * LAMPORTS_PER_SOL), (
            f"Expected net pressure=+2 SOL ({2 * LAMPORTS_PER_SOL}), "
            f"got {result.first_k_buy_pressure}.  "
            "B-1 BLOCKER: pressure equals gross volume instead of net."
        )
        assert result.first_k_volume_lamports == 8 * LAMPORTS_PER_SOL

    def test_net_sell_pressure_is_negative_with_real_decoder_reserve(self):
        """When sells outweigh buys, net pressure MUST be negative."""
        buy_event = _make_launch_event(
            slot=BASE_SLOT + 1,
            sol_reserve_lamports=2 * LAMPORTS_PER_SOL,
            creator_wallet=WALLET_BUYER_1,
        )
        sell_event = _make_launch_event(
            slot=BASE_SLOT + 2,
            sol_reserve_lamports=5 * LAMPORTS_PER_SOL,
            creator_wallet=WALLET_SELLER_1,
        )

        result = build_buy_pressure_features(
            pool_creation_event=POOL_CREATION_EVENT_REAL_DECODER,
            classified_events=[(buy_event, True), (sell_event, False)],
            first_k_slots=FIRST_K_SLOTS,
        )

        assert result.first_k_buy_pressure < Decimal(0), (
            "Net pressure must be negative when sells outweigh buys."
        )
        assert result.first_k_buy_pressure == Decimal(-3 * LAMPORTS_PER_SOL)
        assert result.first_k_buy_count == 1
        assert result.first_k_sell_count == 1

    def test_pure_sell_scenario_with_real_decoder_reserve(self):
        """A token with only sells should have zero buy pressure and negative net."""
        sell_event = _make_launch_event(
            slot=BASE_SLOT + 1,
            sol_reserve_lamports=4 * LAMPORTS_PER_SOL,
            creator_wallet=WALLET_SELLER_1,
        )

        result = build_buy_pressure_features(
            pool_creation_event=POOL_CREATION_EVENT_REAL_DECODER,
            classified_events=[(sell_event, False)],
            first_k_slots=FIRST_K_SLOTS,
        )

        assert result.first_k_buy_count == 0
        assert result.first_k_sell_count == 1
        assert result.first_k_buy_pressure == Decimal(-4 * LAMPORTS_PER_SOL)
        assert result.first_k_volume_lamports == 4 * LAMPORTS_PER_SOL
        assert result.first_k_unique_buyers == 0

    def test_create_event_with_zero_reserve_does_not_fail_on_empty_window(self):
        """An empty window with create reserve=0 must return zero features (not error)."""
        result = build_buy_pressure_features(
            pool_creation_event=POOL_CREATION_EVENT_REAL_DECODER,
            classified_events=[],
            first_k_slots=FIRST_K_SLOTS,
        )
        assert result.first_k_buy_pressure == Decimal(0)
        assert result.first_k_volume_lamports == 0
        assert result.first_k_buy_count == 0
        assert result.first_k_sell_count == 0


# ---------------------------------------------------------------------------
# 10. [B-1 FIX] classify_event_direction raises on zero-reserve anchor
# ---------------------------------------------------------------------------


class TestClassifyEventDirectionGuard:
    """classify_event_direction must refuse to classify when creation reserve=0.

    This prevents the silent all-BUY degeneration that corrupted the C-4 baseline.
    """

    def test_classify_raises_when_creation_reserve_is_zero(self):
        """ClassificationUndefinedError raised when create event has reserve=0."""
        any_event = _make_launch_event(
            slot=BASE_SLOT + 1,
            sol_reserve_lamports=5 * LAMPORTS_PER_SOL,
        )

        with pytest.raises(ClassificationUndefinedError) as exc_info:
            classify_event_direction(any_event, POOL_CREATION_EVENT_REAL_DECODER)

        err = exc_info.value
        assert err.creation_sol_reserve == 0
        assert err.mint == POOL_CREATION_EVENT_REAL_DECODER.mint
        # Error message must name the root cause
        assert "sol_reserve_lamports == 0" in str(err)
        assert "PUMP_DISC_BUY" in str(err) or "discriminator" in str(err)

    def test_classify_works_with_nonzero_creation_reserve(self):
        """classify_event_direction is valid only when creation reserve > 0 (synthetic)."""
        # BUY: event reserve > creation reserve
        buy_event = _make_launch_event(
            slot=BASE_SLOT + 1,
            sol_reserve_lamports=35 * LAMPORTS_PER_SOL,
        )
        assert classify_event_direction(buy_event, POOL_CREATION_EVENT) is True

        # SELL: event reserve < creation reserve
        sell_event = _make_launch_event(
            slot=BASE_SLOT + 2,
            sol_reserve_lamports=25 * LAMPORTS_PER_SOL,
        )
        assert classify_event_direction(sell_event, POOL_CREATION_EVENT) is False

    def test_classify_equal_to_creation_reserve_is_buy(self):
        """Event reserve == creation reserve classifies as BUY (not sell)."""
        equal_event = _make_launch_event(
            slot=BASE_SLOT + 1,
            sol_reserve_lamports=30 * LAMPORTS_PER_SOL,  # same as POOL_CREATION_EVENT
        )
        assert classify_event_direction(equal_event, POOL_CREATION_EVENT) is True


# ---------------------------------------------------------------------------
# 2. LOOKAHEAD INJECTION TEST — the prime directive test
# ---------------------------------------------------------------------------


class TestLookaheadInjection:
    """MANDATORY: proves the function REFUSES future-slot data (causality test).

    These tests are the 'future-mutation invariance' tests required by the
    M1 self-check (charter §6, item 2).  A function that changes its output
    when future-slot data is injected is a LOOKAHEAD BLOCKER.

    The contract: injecting an event with slot > cutoff MUST raise FeatureWindowError.
    If the function silently accepts and incorporates future data, this test fails
    and the feature is a BUILD BLOCKER.
    """

    def test_future_slot_event_raises_feature_window_error(self):
        """An event one slot past the cutoff MUST raise FeatureWindowError."""
        future_slot = BASE_SLOT + FIRST_K_SLOTS + 1  # ONE slot past cutoff
        future_event = _make_launch_event(
            slot=future_slot,
            sol_reserve_lamports=50 * LAMPORTS_PER_SOL,  # large buy would inflate pressure
        )

        with pytest.raises(FeatureWindowError) as exc_info:
            build_buy_pressure_features(
                pool_creation_event=POOL_CREATION_EVENT_REAL_DECODER,
                classified_events=[(future_event, True)],
                first_k_slots=FIRST_K_SLOTS,
            )

        err = exc_info.value
        assert err.event_slot == future_slot
        assert err.cutoff_slot == BASE_SLOT + FIRST_K_SLOTS
        # The error message must be informative for debugging
        assert "POINT-IN-TIME VIOLATION" in str(err)
        assert str(future_slot) in str(err)

    def test_far_future_slot_raises_feature_window_error(self):
        """An event 100 slots past the cutoff (label horizon range) raises."""
        future_slot = BASE_SLOT + FIRST_K_SLOTS + 100  # far future
        future_event = _make_launch_event(
            slot=future_slot,
            sol_reserve_lamports=200 * LAMPORTS_PER_SOL,
        )
        with pytest.raises(FeatureWindowError):
            build_buy_pressure_features(
                pool_creation_event=POOL_CREATION_EVENT_REAL_DECODER,
                classified_events=[(future_event, True)],
                first_k_slots=FIRST_K_SLOTS,
            )

    def test_future_event_does_not_silently_change_feature_value(self):
        """Perturbing with a future event CHANGES the exception (not the feature value).

        This is the 'future-mutation invariance' test from the charter.
        The feature value at t must be unchanged by any future perturbation.
        Invariance is enforced by REJECTION, not by filtering — we verify that
        the future event causes an exception rather than a silent change.
        """
        # Compute the clean value (only within-window events)
        within_window_events = [
            (
                _make_launch_event(
                    slot=BASE_SLOT + 1,
                    sol_reserve_lamports=5 * LAMPORTS_PER_SOL,
                    creator_wallet=WALLET_BUYER_1,
                ),
                True,
            )
        ]
        clean_result = build_buy_pressure_features(
            pool_creation_event=POOL_CREATION_EVENT_REAL_DECODER,
            classified_events=within_window_events,
            first_k_slots=FIRST_K_SLOTS,
        )

        # Now try to inject a future event ALONGSIDE the within-window event.
        # The function MUST raise — not produce a modified result.
        future_event = _make_launch_event(
            slot=BASE_SLOT + FIRST_K_SLOTS + 5,  # future slot
            sol_reserve_lamports=1_000 * LAMPORTS_PER_SOL,  # huge buy to make leak visible
        )
        contaminated_events = within_window_events + [(future_event, True)]

        with pytest.raises(FeatureWindowError):
            # Must raise — the future data must not be incorporated
            build_buy_pressure_features(
                pool_creation_event=POOL_CREATION_EVENT_REAL_DECODER,
                classified_events=contaminated_events,
                first_k_slots=FIRST_K_SLOTS,
            )

        # Verify the clean result was not changed by the failed contaminated call
        # (the accumulator is constructed fresh each time — no shared state)
        clean_result_2 = build_buy_pressure_features(
            pool_creation_event=POOL_CREATION_EVENT_REAL_DECODER,
            classified_events=within_window_events,
            first_k_slots=FIRST_K_SLOTS,
        )
        assert clean_result.first_k_buy_pressure == clean_result_2.first_k_buy_pressure
        assert clean_result.first_k_volume_lamports == clean_result_2.first_k_volume_lamports

    def test_accumulator_observe_raises_on_future_slot(self):
        """The online accumulator (SNIPE-loop path) also raises FeatureWindowError."""
        acc = BuyPressureAccumulator(
            mint=MINT_A,
            event_time_slot=BASE_SLOT,
            first_k_slots=FIRST_K_SLOTS,
        )
        future_event = _make_launch_event(
            slot=BASE_SLOT + FIRST_K_SLOTS + 1,
            sol_reserve_lamports=50 * LAMPORTS_PER_SOL,
        )
        with pytest.raises(FeatureWindowError) as exc_info:
            acc.observe(future_event, is_buy=True)

        assert exc_info.value.event_slot == BASE_SLOT + FIRST_K_SLOTS + 1
        assert exc_info.value.cutoff_slot == BASE_SLOT + FIRST_K_SLOTS

    def test_accumulator_state_unchanged_after_future_slot_rejection(self):
        """After a FeatureWindowError, the accumulator state is unchanged.

        This proves the rejection is atomic: a future-slot event that fails
        must not partially mutate the accumulator before raising.
        """
        acc = BuyPressureAccumulator(
            mint=MINT_A,
            event_time_slot=BASE_SLOT,
            first_k_slots=FIRST_K_SLOTS,
        )
        # First, add a valid event
        valid_event = _make_launch_event(
            slot=BASE_SLOT + 1,
            sol_reserve_lamports=5 * LAMPORTS_PER_SOL,
            creator_wallet=WALLET_BUYER_1,
        )
        acc.observe(valid_event, is_buy=True)

        # Snapshot state BEFORE the bad event
        pressure_before = acc._net_buy_lamports
        volume_before = acc._volume_lamports
        buy_count_before = acc._buy_count

        # Now try to inject a future event
        future_event = _make_launch_event(
            slot=BASE_SLOT + FIRST_K_SLOTS + 2,
            sol_reserve_lamports=100 * LAMPORTS_PER_SOL,
        )
        with pytest.raises(FeatureWindowError):
            acc.observe(future_event, is_buy=True)

        # State must be UNCHANGED after the rejected future event
        assert acc._net_buy_lamports == pressure_before
        assert acc._volume_lamports == volume_before
        assert acc._buy_count == buy_count_before


# ---------------------------------------------------------------------------
# 11. [M-1 FIX] Pre-creation slot rejection tests
# ---------------------------------------------------------------------------


class TestPreCreationSlotRejection:
    """MAJOR M-1 regression tests.

    observe() must reject events with slot < event_time_slot (pre-creation).
    A stray earlier-slot event (mis-route, reorg, replay) MUST NOT silently
    contribute to first-K-slot features or corrupt max_source_slot.
    """

    def test_pre_creation_slot_raises_feature_window_error(self):
        """An event at slot < creation slot MUST raise FeatureWindowError."""
        # Use a creation slot of 1000 so we can create a pre-creation event at slot 990.
        # BASE_SLOT is too large; we need a small slot value so 990 < creation_slot is valid.
        creation_slot = 1000

        pre_creation_event = LaunchEvent(
            mint=MINT_A,
            venue_program_id="6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",
            source=LaunchSource.PUMPFUN,
            event_time=EventTime(
                slot=990,  # pre-creation: 990 < 1000
                block_time_ms=BASE_BLOCK_TIME_MS - 1000,
                wall_clock_ms=BASE_BLOCK_TIME_MS - 900,
            ),
            observation_slot=990,
            confirmation_slot=990,
            detection_transport=DetectionTransport.GEYSER,
            sol_reserve_lamports=5 * LAMPORTS_PER_SOL,
            token_reserve_base=0,
            token_decimals=6,
            initial_holders=0,
            competitors=0,
            creator_wallet=WALLET_BUYER_1,
            bundler_cluster_id=None,
            deploy_template_fingerprint=None,
            data_staleness_ms=50,
        )

        acc = BuyPressureAccumulator(
            mint=MINT_A,
            event_time_slot=creation_slot,
            first_k_slots=FIRST_K_SLOTS,
        )

        with pytest.raises(FeatureWindowError) as exc_info:
            acc.observe(pre_creation_event, is_buy=True)

        err = exc_info.value
        assert err.event_slot == 990
        assert err.creation_slot == creation_slot
        assert "POINT-IN-TIME VIOLATION" in str(err)
        assert "990" in str(err)
        assert "creation" in str(err).lower() or "pre-creation" in str(err).lower()

    def test_pre_creation_event_does_not_pollute_accumulator(self):
        """After a pre-creation FeatureWindowError, accumulator state is unchanged."""
        creation_slot = 1000
        acc = BuyPressureAccumulator(
            mint=MINT_A,
            event_time_slot=creation_slot,
            first_k_slots=FIRST_K_SLOTS,
        )

        # A valid event at creation slot + 1
        valid_event = LaunchEvent(
            mint=MINT_A,
            venue_program_id="6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",
            source=LaunchSource.PUMPFUN,
            event_time=EventTime(
                slot=creation_slot + 1,
                block_time_ms=BASE_BLOCK_TIME_MS + 400,
                wall_clock_ms=BASE_BLOCK_TIME_MS + 450,
            ),
            observation_slot=creation_slot + 1,
            confirmation_slot=creation_slot + 1,
            detection_transport=DetectionTransport.GEYSER,
            sol_reserve_lamports=3 * LAMPORTS_PER_SOL,
            token_reserve_base=0,
            token_decimals=6,
            initial_holders=0,
            competitors=0,
            creator_wallet=WALLET_BUYER_1,
            bundler_cluster_id=None,
            deploy_template_fingerprint=None,
            data_staleness_ms=50,
        )
        acc.observe(valid_event, is_buy=True)

        pressure_before = acc._net_buy_lamports
        volume_before = acc._volume_lamports
        buy_count_before = acc._buy_count

        # Pre-creation event
        pre_creation_event = LaunchEvent(
            mint=MINT_A,
            venue_program_id="6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",
            source=LaunchSource.PUMPFUN,
            event_time=EventTime(
                slot=990,  # pre-creation
                block_time_ms=BASE_BLOCK_TIME_MS - 1000,
                wall_clock_ms=BASE_BLOCK_TIME_MS - 900,
            ),
            observation_slot=990,
            confirmation_slot=990,
            detection_transport=DetectionTransport.GEYSER,
            sol_reserve_lamports=100 * LAMPORTS_PER_SOL,
            token_reserve_base=0,
            token_decimals=6,
            initial_holders=0,
            competitors=0,
            creator_wallet=WALLET_BUYER_1,
            bundler_cluster_id=None,
            deploy_template_fingerprint=None,
            data_staleness_ms=50,
        )

        with pytest.raises(FeatureWindowError):
            acc.observe(pre_creation_event, is_buy=True)

        # State must be UNCHANGED
        assert acc._net_buy_lamports == pressure_before
        assert acc._volume_lamports == volume_before
        assert acc._buy_count == buy_count_before

    def test_pre_creation_event_via_build_buy_pressure_features_raises(self):
        """build_buy_pressure_features also raises on pre-creation events."""
        creation_slot = 1000
        creation_event = LaunchEvent(
            mint=MINT_A,
            venue_program_id="6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",
            source=LaunchSource.PUMPFUN,
            event_time=EventTime(
                slot=creation_slot,
                block_time_ms=BASE_BLOCK_TIME_MS,
                wall_clock_ms=BASE_BLOCK_TIME_MS + 50,
            ),
            observation_slot=creation_slot,
            confirmation_slot=creation_slot,
            detection_transport=DetectionTransport.GEYSER,
            sol_reserve_lamports=0,
            token_reserve_base=0,
            token_decimals=6,
            initial_holders=0,
            competitors=0,
            creator_wallet=WALLET_BUYER_1,
            bundler_cluster_id=None,
            deploy_template_fingerprint=None,
            data_staleness_ms=50,
        )

        pre_creation_event = LaunchEvent(
            mint=MINT_A,
            venue_program_id="6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",
            source=LaunchSource.PUMPFUN,
            event_time=EventTime(
                slot=990,
                block_time_ms=BASE_BLOCK_TIME_MS - 1000,
                wall_clock_ms=BASE_BLOCK_TIME_MS - 900,
            ),
            observation_slot=990,
            confirmation_slot=990,
            detection_transport=DetectionTransport.GEYSER,
            sol_reserve_lamports=2 * LAMPORTS_PER_SOL,
            token_reserve_base=0,
            token_decimals=6,
            initial_holders=0,
            competitors=0,
            creator_wallet=WALLET_BUYER_1,
            bundler_cluster_id=None,
            deploy_template_fingerprint=None,
            data_staleness_ms=50,
        )

        with pytest.raises(FeatureWindowError) as exc_info:
            build_buy_pressure_features(
                pool_creation_event=creation_event,
                classified_events=[(pre_creation_event, True)],
                first_k_slots=FIRST_K_SLOTS,
            )

        err = exc_info.value
        assert err.event_slot == 990
        assert err.creation_slot == creation_slot

    def test_event_at_creation_slot_is_accepted(self):
        """An event at slot == creation_slot is WITHIN the window (inclusive lower bound)."""
        creation_slot = 1000
        creation_event = LaunchEvent(
            mint=MINT_A,
            venue_program_id="6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",
            source=LaunchSource.PUMPFUN,
            event_time=EventTime(
                slot=creation_slot,
                block_time_ms=BASE_BLOCK_TIME_MS,
                wall_clock_ms=BASE_BLOCK_TIME_MS + 50,
            ),
            observation_slot=creation_slot,
            confirmation_slot=creation_slot,
            detection_transport=DetectionTransport.GEYSER,
            sol_reserve_lamports=0,
            token_reserve_base=0,
            token_decimals=6,
            initial_holders=0,
            competitors=0,
            creator_wallet=WALLET_BUYER_1,
            bundler_cluster_id=None,
            deploy_template_fingerprint=None,
            data_staleness_ms=50,
        )

        same_slot_event = LaunchEvent(
            mint=MINT_A,
            venue_program_id="6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",
            source=LaunchSource.PUMPFUN,
            event_time=EventTime(
                slot=creation_slot,  # slot == creation_slot — valid lower bound
                block_time_ms=BASE_BLOCK_TIME_MS,
                wall_clock_ms=BASE_BLOCK_TIME_MS + 50,
            ),
            observation_slot=creation_slot,
            confirmation_slot=creation_slot,
            detection_transport=DetectionTransport.GEYSER,
            sol_reserve_lamports=5 * LAMPORTS_PER_SOL,
            token_reserve_base=0,
            token_decimals=6,
            initial_holders=0,
            competitors=0,
            creator_wallet=WALLET_BUYER_1,
            bundler_cluster_id=None,
            deploy_template_fingerprint=None,
            data_staleness_ms=50,
        )

        # Must NOT raise — slot == creation_slot is the inclusive lower bound
        result = build_buy_pressure_features(
            pool_creation_event=creation_event,
            classified_events=[(same_slot_event, True)],
            first_k_slots=FIRST_K_SLOTS,
        )
        assert result.first_k_buy_count == 1


# ---------------------------------------------------------------------------
# 3. Provenance cutoff correctness
# ---------------------------------------------------------------------------


class TestProvenanceCutoff:
    """Provenance manifest max_source_slot must be <= event_time.slot + K."""

    def test_provenance_max_source_slot_at_cutoff(self):
        """Events at the boundary slot are accepted and recorded in provenance."""
        boundary_event = _make_launch_event(
            slot=BASE_SLOT + FIRST_K_SLOTS,  # exactly at cutoff
            sol_reserve_lamports=5 * LAMPORTS_PER_SOL,
        )
        result = build_buy_pressure_features(
            pool_creation_event=POOL_CREATION_EVENT_REAL_DECODER,
            classified_events=[(boundary_event, True)],
            first_k_slots=FIRST_K_SLOTS,
        )
        assert result.max_source_slot == BASE_SLOT + FIRST_K_SLOTS
        for window in result.provenance.windows:
            assert window.max_source_slot <= BASE_SLOT + FIRST_K_SLOTS

    def test_provenance_lineage_is_launch_events(self):
        """All provenance windows declare lineage from launch_events."""
        result = build_buy_pressure_features(
            pool_creation_event=POOL_CREATION_EVENT_REAL_DECODER,
            classified_events=[],
            first_k_slots=FIRST_K_SLOTS,
        )
        for window in result.provenance.windows:
            assert window.lineage_dataset == "launch_events"

    def test_provenance_has_one_window_per_feature(self):
        """Provenance must have one window for each tracked feature."""
        result = build_buy_pressure_features(
            pool_creation_event=POOL_CREATION_EVENT_REAL_DECODER,
            classified_events=[],
            first_k_slots=FIRST_K_SLOTS,
        )
        expected_features = {
            "first_k_buy_pressure",
            "first_k_volume_lamports",
            "first_k_buy_count",
            "first_k_sell_count",
            "first_k_unique_buyers",
        }
        declared_features = {w.feature_name for w in result.provenance.windows}
        assert declared_features == expected_features

    def test_provenance_builder_version_matches_module_constant(self):
        """builder_version must match the module FEATURE_SCHEMA_VERSION constant."""
        result = build_buy_pressure_features(
            pool_creation_event=POOL_CREATION_EVENT_REAL_DECODER,
            classified_events=[],
            first_k_slots=FIRST_K_SLOTS,
        )
        assert result.provenance.builder_version == FEATURE_SCHEMA_VERSION

    def test_provenance_guard_fires_on_fabricated_future_max_slot(self):
        """The second-line provenance guard raises ProvenanceTaintError if
        max_source_slot > cutoff.  This tests the guard directly (bypassing the
        accumulator's FeatureWindowError guard to verify the second defence).

        Note: in normal usage, FeatureWindowError fires first.  This test
        reaches the provenance layer directly through check_feature_window_cutoff.
        """
        from aats.contracts.provenance import check_feature_window_cutoff

        with pytest.raises(ProvenanceTaintError) as exc_info:
            check_feature_window_cutoff(
                feature_name="first_k_buy_pressure",
                max_source_slot=BASE_SLOT + FIRST_K_SLOTS + 1,  # ONE past cutoff
                event_time_slot=BASE_SLOT,
                first_k_slots=FIRST_K_SLOTS,
            )
        assert "feature_window_exceeds_cutoff" in str(exc_info.value)
        assert "first_k_buy_pressure" in str(exc_info.value)

    def test_empty_window_provenance_max_slot_is_event_time_slot(self):
        """With zero events, max_source_slot must equal event_time.slot (not 0)."""
        result = build_buy_pressure_features(
            pool_creation_event=POOL_CREATION_EVENT_REAL_DECODER,
            classified_events=[],
            first_k_slots=FIRST_K_SLOTS,
        )
        # No events observed → max_source_slot = event_time_slot (the creation slot)
        assert result.max_source_slot == BASE_SLOT
        # This is within the cutoff (BASE_SLOT <= BASE_SLOT + K)
        for window in result.provenance.windows:
            assert window.max_source_slot <= BASE_SLOT + FIRST_K_SLOTS


# ---------------------------------------------------------------------------
# 4. Batch == streaming parity
# ---------------------------------------------------------------------------


class TestBatchStreamingParity:
    """The online accumulator and the batch helper must produce BIT-IDENTICAL outputs.

    This is the M1 requirement: offline backtest path and live SNIPE-loop path
    share the SAME code and produce bit-identical outputs.
    Divergence between batch and streaming is a defect.
    """

    def test_empty_events_parity(self):
        """No events: batch and streaming produce identical zeros."""
        acc = BuyPressureAccumulator(
            mint=MINT_A,
            event_time_slot=BASE_SLOT,
            first_k_slots=FIRST_K_SLOTS,
        )
        streaming = acc.to_features(POOL_CREATION_EVENT_REAL_DECODER.event_time)
        batch = build_buy_pressure_features(
            pool_creation_event=POOL_CREATION_EVENT_REAL_DECODER,
            classified_events=[],
            first_k_slots=FIRST_K_SLOTS,
        )
        _assert_features_equal(streaming, batch)

    def test_three_events_parity(self):
        """Three events: batch and streaming produce identical results."""
        classified = [
            (_make_launch_event(
                slot=BASE_SLOT + 1,
                sol_reserve_lamports=5 * LAMPORTS_PER_SOL,
                creator_wallet=WALLET_BUYER_1,
            ), True),
            (_make_launch_event(
                slot=BASE_SLOT + 3,
                sol_reserve_lamports=3 * LAMPORTS_PER_SOL,
                creator_wallet=WALLET_SELLER_1,
            ), False),
            (_make_launch_event(
                slot=BASE_SLOT + 7,
                sol_reserve_lamports=4 * LAMPORTS_PER_SOL,
                creator_wallet=WALLET_BUYER_2,
            ), True),
        ]
        # Online path
        acc = BuyPressureAccumulator(
            mint=MINT_A,
            event_time_slot=BASE_SLOT,
            first_k_slots=FIRST_K_SLOTS,
        )
        for ev, is_buy in classified:
            acc.observe(ev, is_buy=is_buy)
        streaming = acc.to_features(POOL_CREATION_EVENT_REAL_DECODER.event_time)

        # Batch path
        batch = build_buy_pressure_features(
            pool_creation_event=POOL_CREATION_EVENT_REAL_DECODER,
            classified_events=classified,
            first_k_slots=FIRST_K_SLOTS,
        )
        _assert_features_equal(streaming, batch)

    def test_single_event_parity(self):
        """Single event: batch and streaming match exactly."""
        event = _make_launch_event(
            slot=BASE_SLOT + 5,
            sol_reserve_lamports=5 * LAMPORTS_PER_SOL,
            creator_wallet=WALLET_BUYER_1,
        )
        acc = BuyPressureAccumulator(
            mint=MINT_A,
            event_time_slot=BASE_SLOT,
            first_k_slots=FIRST_K_SLOTS,
        )
        acc.observe(event, is_buy=True)
        streaming = acc.to_features(POOL_CREATION_EVENT_REAL_DECODER.event_time)

        batch = build_buy_pressure_features(
            pool_creation_event=POOL_CREATION_EVENT_REAL_DECODER,
            classified_events=[(event, True)],
            first_k_slots=FIRST_K_SLOTS,
        )
        _assert_features_equal(streaming, batch)

    def test_parity_with_K_equals_1(self):
        """K=1 edge case: only one slot post-creation is accepted."""
        event = _make_launch_event(
            slot=BASE_SLOT + 1,
            sol_reserve_lamports=3 * LAMPORTS_PER_SOL,
        )
        acc = BuyPressureAccumulator(
            mint=MINT_A,
            event_time_slot=BASE_SLOT,
            first_k_slots=1,
        )
        acc.observe(event, is_buy=True)
        streaming = acc.to_features(POOL_CREATION_EVENT_REAL_DECODER.event_time)

        batch = build_buy_pressure_features(
            pool_creation_event=POOL_CREATION_EVENT_REAL_DECODER,
            classified_events=[(event, True)],
            first_k_slots=1,
        )
        _assert_features_equal(streaming, batch)


def _assert_features_equal(a: BuyPressureFeatures, b: BuyPressureFeatures) -> None:
    """Assert that two BuyPressureFeatures are bit-identical."""
    assert a.first_k_buy_pressure == b.first_k_buy_pressure, (
        f"buy_pressure mismatch: {a.first_k_buy_pressure} != {b.first_k_buy_pressure}"
    )
    assert a.first_k_volume_lamports == b.first_k_volume_lamports, (
        f"volume mismatch: {a.first_k_volume_lamports} != {b.first_k_volume_lamports}"
    )
    assert a.first_k_buy_count == b.first_k_buy_count
    assert a.first_k_sell_count == b.first_k_sell_count
    assert a.first_k_unique_buyers == b.first_k_unique_buyers
    assert a.max_source_slot == b.max_source_slot
    assert a.first_k_slots == b.first_k_slots


# ---------------------------------------------------------------------------
# 5. Money fields are int/Decimal, NEVER float
# ---------------------------------------------------------------------------


class TestMoneyFieldTypes:
    """Money fields must be int or Decimal — NEVER float (data-models.md §0)."""

    def test_buy_pressure_is_decimal_not_float(self):
        result = build_buy_pressure_features(
            pool_creation_event=POOL_CREATION_EVENT_REAL_DECODER,
            classified_events=[],
            first_k_slots=FIRST_K_SLOTS,
        )
        assert isinstance(result.first_k_buy_pressure, Decimal), (
            f"first_k_buy_pressure must be Decimal, got {type(result.first_k_buy_pressure)}"
        )
        assert not isinstance(result.first_k_buy_pressure, float)

    def test_volume_lamports_is_int_not_float(self):
        result = build_buy_pressure_features(
            pool_creation_event=POOL_CREATION_EVENT_REAL_DECODER,
            classified_events=[],
            first_k_slots=FIRST_K_SLOTS,
        )
        assert isinstance(result.first_k_volume_lamports, int), (
            f"first_k_volume_lamports must be int, got {type(result.first_k_volume_lamports)}"
        )
        assert not isinstance(result.first_k_volume_lamports, float)

    def test_buy_count_is_int(self):
        result = build_buy_pressure_features(
            pool_creation_event=POOL_CREATION_EVENT_REAL_DECODER,
            classified_events=[],
            first_k_slots=FIRST_K_SLOTS,
        )
        assert isinstance(result.first_k_buy_count, int)
        assert isinstance(result.first_k_sell_count, int)
        assert isinstance(result.first_k_unique_buyers, int)

    def test_with_events_buy_pressure_is_decimal(self):
        buy_event = _make_launch_event(
            slot=BASE_SLOT + 1,
            sol_reserve_lamports=5 * LAMPORTS_PER_SOL,
        )
        result = build_buy_pressure_features(
            pool_creation_event=POOL_CREATION_EVENT_REAL_DECODER,
            classified_events=[(buy_event, True)],
            first_k_slots=FIRST_K_SLOTS,
        )
        assert isinstance(result.first_k_buy_pressure, Decimal)
        assert isinstance(result.first_k_volume_lamports, int)


# ---------------------------------------------------------------------------
# 6. Schema conformance with FeatureFrame
# ---------------------------------------------------------------------------


class TestSchemaConformance:
    """BuyPressureFeatures values must be compatible with FeatureFrame field types."""

    def test_features_populate_feature_frame(self):
        """Verify the computed features can populate FeatureFrame without type error."""
        from aats.contracts.features import FeatureFrame, FeatureProvenance, FeatureSourceWindow

        buy_event = _make_launch_event(
            slot=BASE_SLOT + 1,
            sol_reserve_lamports=5 * LAMPORTS_PER_SOL,
            creator_wallet=WALLET_BUYER_1,
        )
        result = build_buy_pressure_features(
            pool_creation_event=POOL_CREATION_EVENT_REAL_DECODER,
            classified_events=[(buy_event, True)],
            first_k_slots=FIRST_K_SLOTS,
        )

        # Build a full FeatureFrame using the computed buy-pressure features
        # (other fields are stubs — this test verifies the BuyPressureFeatures
        # fields are type-compatible with FeatureFrame)
        full_provenance = FeatureProvenance(
            windows=list(result.provenance.windows) + [
                FeatureSourceWindow(
                    feature_name="lp_depth_lamports",
                    max_source_slot=result.max_source_slot,
                    lineage_dataset="launch_events",
                ),
            ],
            builder_version=FEATURE_SCHEMA_VERSION,
        )
        frame = FeatureFrame(
            mint=POOL_CREATION_EVENT_REAL_DECODER.mint,
            event_time=result.event_time,
            feature_schema_version=FEATURE_SCHEMA_VERSION,
            data_staleness_ms=50,
            # --- first-K-slot microstructure (from BuyPressureFeatures) ---
            first_k_slots=result.first_k_slots,
            first_k_buy_pressure=result.first_k_buy_pressure,      # Decimal ✓
            first_k_volume_lamports=result.first_k_volume_lamports,  # int ✓
            first_k_buy_count=result.first_k_buy_count,             # int ✓
            first_k_sell_count=result.first_k_sell_count,           # int ✓
            first_k_unique_buyers=result.first_k_unique_buyers,     # int ✓
            # --- survivor microstructure stubs ---
            lp_depth_lamports=10_000_000_000,
            holder_count=100,
            holder_concentration_top10_bps=2500,
            sniper_cluster_score=0.1,
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
            provenance=full_provenance,
        )
        # The FeatureFrame must be constructable (no TypeError from type mismatches)
        assert frame.first_k_buy_pressure == result.first_k_buy_pressure
        assert frame.first_k_volume_lamports == result.first_k_volume_lamports
        assert isinstance(frame.first_k_buy_pressure, Decimal)
        assert isinstance(frame.first_k_volume_lamports, int)

    def test_feature_frame_rejects_float_buy_pressure(self):
        """FeatureFrame must REJECT float values for first_k_buy_pressure."""
        from aats.contracts.features import FeatureFrame, FeatureProvenance, FeatureSourceWindow

        provenance = FeatureProvenance(
            windows=[
                FeatureSourceWindow(
                    feature_name="first_k_buy_pressure",
                    max_source_slot=BASE_SLOT + FIRST_K_SLOTS,
                    lineage_dataset="launch_events",
                ),
            ],
            builder_version=FEATURE_SCHEMA_VERSION,
        )
        with pytest.raises(ValueError):  # Pydantic rejects float for Decimal field
            FeatureFrame(
                mint=MINT_A,
                event_time=POOL_CREATION_EVENT_REAL_DECODER.event_time,
                feature_schema_version=FEATURE_SCHEMA_VERSION,
                data_staleness_ms=0,
                first_k_slots=FIRST_K_SLOTS,
                first_k_buy_pressure=1.234,  # FLOAT — must be rejected
                first_k_volume_lamports=1_000_000_000,
                first_k_buy_count=1,
                first_k_sell_count=0,
                first_k_unique_buyers=1,
                lp_depth_lamports=10_000_000_000,
                holder_count=100,
                holder_concentration_top10_bps=2500,
                sniper_cluster_score=0.1,
                sell_tax_bps=0,
                sell_reserve_trajectory=[],
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


# ---------------------------------------------------------------------------
# 7. Accumulator reset test
# ---------------------------------------------------------------------------


class TestAccumulatorReset:
    """reset() must restore the accumulator to the same state as a fresh instance."""

    def test_reset_clears_all_state(self):
        acc = BuyPressureAccumulator(
            mint=MINT_A,
            event_time_slot=BASE_SLOT,
            first_k_slots=FIRST_K_SLOTS,
        )
        # Populate the accumulator
        event = _make_launch_event(
            slot=BASE_SLOT + 1,
            sol_reserve_lamports=5 * LAMPORTS_PER_SOL,
            creator_wallet=WALLET_BUYER_1,
        )
        acc.observe(event, is_buy=True)
        assert acc._buy_count == 1
        assert acc._volume_lamports > 0

        # Reset and verify
        acc.reset()
        assert acc._net_buy_lamports == Decimal(0)
        assert acc._volume_lamports == 0
        assert acc._buy_count == 0
        assert acc._sell_count == 0
        assert len(acc._buyer_wallets) == 0
        assert acc._max_source_slot == 0
        assert acc._event_count == 0

    def test_reset_then_reuse_gives_same_result(self):
        """After reset, re-observing the same events gives the same result."""
        acc = BuyPressureAccumulator(
            mint=MINT_A,
            event_time_slot=BASE_SLOT,
            first_k_slots=FIRST_K_SLOTS,
        )
        event = _make_launch_event(
            slot=BASE_SLOT + 1,
            sol_reserve_lamports=5 * LAMPORTS_PER_SOL,
            creator_wallet=WALLET_BUYER_1,
        )

        # First pass
        acc.observe(event, is_buy=True)
        first_result = acc.to_features(POOL_CREATION_EVENT_REAL_DECODER.event_time)

        # Reset and second pass
        acc.reset()
        acc.observe(event, is_buy=True)
        second_result = acc.to_features(POOL_CREATION_EVENT_REAL_DECODER.event_time)

        assert first_result.first_k_buy_pressure == second_result.first_k_buy_pressure
        assert first_result.first_k_volume_lamports == second_result.first_k_volume_lamports


# ---------------------------------------------------------------------------
# 8. Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Additional edge-case coverage."""

    def test_event_at_pool_creation_slot_is_within_window(self):
        """An event at slot == pool creation slot is within window (inclusive lower bound)."""
        same_slot_event = _make_launch_event(
            slot=BASE_SLOT,
            sol_reserve_lamports=5 * LAMPORTS_PER_SOL,
        )
        # Must NOT raise — slot == creation_slot is the inclusive lower bound
        result = build_buy_pressure_features(
            pool_creation_event=POOL_CREATION_EVENT_REAL_DECODER,
            classified_events=[(same_slot_event, True)],
            first_k_slots=FIRST_K_SLOTS,
        )
        assert result.first_k_buy_count + result.first_k_sell_count == 1

    def test_large_volume_integer_arithmetic(self):
        """Large lamport sums must not overflow int (Python ints are arbitrary precision)."""
        # 1000 SOL buy — tests big number handling
        large_event = _make_launch_event(
            slot=BASE_SLOT + 1,
            sol_reserve_lamports=1_000 * LAMPORTS_PER_SOL,  # 1000 SOL
        )
        result = build_buy_pressure_features(
            pool_creation_event=POOL_CREATION_EVENT_REAL_DECODER,
            classified_events=[(large_event, True)],
            first_k_slots=FIRST_K_SLOTS,
        )
        assert result.first_k_volume_lamports == 1_000 * LAMPORTS_PER_SOL
        assert isinstance(result.first_k_volume_lamports, int)
        # No float imprecision — these are exact integers
        assert result.first_k_volume_lamports == 1_000_000_000_000

    def test_k_equals_zero_rejects_all_subsequent_events(self):
        """K=0: the cutoff = event_time.slot, so ANY subsequent event is future."""
        # slot + 1 > slot + 0 = slot → future
        subsequent = _make_launch_event(
            slot=BASE_SLOT + 1,
            sol_reserve_lamports=5 * LAMPORTS_PER_SOL,
        )
        with pytest.raises(FeatureWindowError):
            build_buy_pressure_features(
                pool_creation_event=POOL_CREATION_EVENT_REAL_DECODER,
                classified_events=[(subsequent, True)],
                first_k_slots=0,
            )

    def test_k_equals_zero_with_no_events_is_valid(self):
        """K=0 with no events is valid (zero window, zero features)."""
        result = build_buy_pressure_features(
            pool_creation_event=POOL_CREATION_EVENT_REAL_DECODER,
            classified_events=[],
            first_k_slots=0,
        )
        assert result.first_k_buy_pressure == Decimal(0)
        assert result.first_k_volume_lamports == 0

    def test_event_time_is_anchored_to_pool_creation(self):
        """The event_time in the result must be copied from pool_creation_event (C-5)."""
        result = build_buy_pressure_features(
            pool_creation_event=POOL_CREATION_EVENT_REAL_DECODER,
            classified_events=[],
            first_k_slots=FIRST_K_SLOTS,
        )
        assert result.event_time == POOL_CREATION_EVENT_REAL_DECODER.event_time
        assert result.event_time.slot == BASE_SLOT

    def test_feature_window_error_mint_attribute(self):
        """FeatureWindowError carries the mint for diagnostics."""
        future = _make_launch_event(
            slot=BASE_SLOT + FIRST_K_SLOTS + 1,
            sol_reserve_lamports=1,
            mint=MINT_A,
        )
        with pytest.raises(FeatureWindowError) as exc_info:
            build_buy_pressure_features(
                pool_creation_event=POOL_CREATION_EVENT_REAL_DECODER,
                classified_events=[(future, True)],
                first_k_slots=FIRST_K_SLOTS,
            )
        assert exc_info.value.mint == MINT_A
