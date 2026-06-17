"""Tests for smart_wallets_in feature (T-304).

Self-check criteria:
  1. Correct count on known fixtures.
  2. Lookahead injection REFUSED (SmartWalletWindowError).
  3. Pre-creation slot rejected.
  4. smart_wallets_in is a count, NEVER a position-sizing signal (AC-009 type check).
  5. Untracked wallets do not count.
  6. Entry lag computation (we are BEHIND smart wallets).
  7. Batch == streaming parity.
  8. Provenance cutoff correct.
  9. AC-020 negative: smart_wallets_in=0 vs =5 must not affect sol_in.
     (We verify the feature is a COUNT with no position-size field — the
      downstream invariant is enforced by T-324, not here; we verify type contract.)
"""

from __future__ import annotations

import pytest

from aats.contracts.events import DetectionTransport, EventTime, LaunchEvent, LaunchSource
from aats.features.smart_wallets import (
    SmartWalletAccumulator,
    SmartWalletWindowError,
    WalletObservation,
    build_smart_wallet_features,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

BASE_SLOT = 300_000_000
BASE_BLOCK_TIME_MS = 1_718_700_000_000
LAMPORTS_PER_SOL = 1_000_000_000
FIRST_K_SLOTS = 10
MINT_A = "MintAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"

PROGRAM_ID = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"

SMART_WALLET_1 = "SmartWallet111111111111111111111111111111111"
SMART_WALLET_2 = "SmartWallet222222222222222222222222222222222"
SMART_WALLET_3 = "SmartWallet333333333333333333333333333333333"
UNTRACKED_WALLET = "UntrackedWallet1111111111111111111111111111"

TRACKED_WALLETS = frozenset({SMART_WALLET_1, SMART_WALLET_2, SMART_WALLET_3})


def _make_event_time(slot: int) -> EventTime:
    return EventTime(
        slot=slot,
        block_time_ms=BASE_BLOCK_TIME_MS + (slot - BASE_SLOT) * 400,
        wall_clock_ms=BASE_BLOCK_TIME_MS + (slot - BASE_SLOT) * 400 + 50,
    )


def _make_pool_creation(slot: int = BASE_SLOT) -> LaunchEvent:
    return LaunchEvent(
        mint=MINT_A,
        venue_program_id=PROGRAM_ID,
        source=LaunchSource.PUMPFUN,
        event_time=_make_event_time(slot),
        observation_slot=slot,
        confirmation_slot=slot,
        detection_transport=DetectionTransport.GEYSER,
        sol_reserve_lamports=30 * LAMPORTS_PER_SOL,
        token_reserve_base=1_000_000_000,
        token_decimals=6,
        initial_holders=0,
        competitors=0,
        creator_wallet="DevWallet",
        bundler_cluster_id=None,
        deploy_template_fingerprint=None,
        data_staleness_ms=50,
    )


POOL_CREATION = _make_pool_creation()


# ---------------------------------------------------------------------------
# 1. Correct count on known fixtures
# ---------------------------------------------------------------------------


class TestCorrectCount:

    def test_zero_observations_zero_count(self):
        """No wallet observations → smart_wallets_in = 0."""
        result = build_smart_wallet_features(
            pool_creation_event=POOL_CREATION,
            wallet_observations=[],
            tracked_wallets=TRACKED_WALLETS,
            first_k_slots=FIRST_K_SLOTS,
        )
        assert result.smart_wallets_in == 0
        assert result.smart_wallet_entry_lag_slots is None
        assert result.earliest_smart_wallet_slot is None

    def test_one_tracked_wallet_count_one(self):
        """One tracked wallet buy → count = 1."""
        result = build_smart_wallet_features(
            pool_creation_event=POOL_CREATION,
            wallet_observations=[WalletObservation(wallet=SMART_WALLET_1, slot=BASE_SLOT + 2)],
            tracked_wallets=TRACKED_WALLETS,
            first_k_slots=FIRST_K_SLOTS,
        )
        assert result.smart_wallets_in == 1

    def test_three_tracked_wallets_count_three(self):
        """Three distinct tracked wallets → count = 3."""
        result = build_smart_wallet_features(
            pool_creation_event=POOL_CREATION,
            wallet_observations=[
                WalletObservation(wallet=SMART_WALLET_1, slot=BASE_SLOT + 1),
                WalletObservation(wallet=SMART_WALLET_2, slot=BASE_SLOT + 2),
                WalletObservation(wallet=SMART_WALLET_3, slot=BASE_SLOT + 3),
            ],
            tracked_wallets=TRACKED_WALLETS,
            first_k_slots=FIRST_K_SLOTS,
        )
        assert result.smart_wallets_in == 3

    def test_same_wallet_multiple_buys_counts_once(self):
        """Same wallet buying twice still counts as 1 (deduplicated)."""
        result = build_smart_wallet_features(
            pool_creation_event=POOL_CREATION,
            wallet_observations=[
                WalletObservation(wallet=SMART_WALLET_1, slot=BASE_SLOT + 1),
                WalletObservation(wallet=SMART_WALLET_1, slot=BASE_SLOT + 3),  # same wallet
            ],
            tracked_wallets=TRACKED_WALLETS,
            first_k_slots=FIRST_K_SLOTS,
        )
        assert result.smart_wallets_in == 1

    def test_untracked_wallet_not_counted(self):
        """Untracked wallets are silently ignored — only tracked set counts."""
        result = build_smart_wallet_features(
            pool_creation_event=POOL_CREATION,
            wallet_observations=[
                WalletObservation(wallet=UNTRACKED_WALLET, slot=BASE_SLOT + 1),
                WalletObservation(wallet=SMART_WALLET_1, slot=BASE_SLOT + 2),
            ],
            tracked_wallets=TRACKED_WALLETS,
            first_k_slots=FIRST_K_SLOTS,
        )
        assert result.smart_wallets_in == 1  # only SMART_WALLET_1

    def test_all_untracked_wallets_zero_count(self):
        """Only untracked wallets → count = 0."""
        result = build_smart_wallet_features(
            pool_creation_event=POOL_CREATION,
            wallet_observations=[
                WalletObservation(wallet=UNTRACKED_WALLET, slot=BASE_SLOT + 1),
            ],
            tracked_wallets=TRACKED_WALLETS,
            first_k_slots=FIRST_K_SLOTS,
        )
        assert result.smart_wallets_in == 0


# ---------------------------------------------------------------------------
# 2. Lookahead injection test (prime directive)
# ---------------------------------------------------------------------------


class TestLookaheadInjection:
    """MANDATORY: future-slot wallet observations MUST raise SmartWalletWindowError."""

    def test_future_slot_raises(self):
        """An observation one slot past the cutoff MUST raise SmartWalletWindowError."""
        future_slot = BASE_SLOT + FIRST_K_SLOTS + 1

        with pytest.raises(SmartWalletWindowError) as exc_info:
            build_smart_wallet_features(
                pool_creation_event=POOL_CREATION,
                wallet_observations=[
                    WalletObservation(wallet=SMART_WALLET_1, slot=future_slot)
                ],
                tracked_wallets=TRACKED_WALLETS,
                first_k_slots=FIRST_K_SLOTS,
            )

        err = exc_info.value
        assert err.observation_slot == future_slot
        assert err.cutoff_slot == BASE_SLOT + FIRST_K_SLOTS
        assert "POINT-IN-TIME VIOLATION" in str(err)

    def test_future_slot_does_not_inflate_count(self):
        """Future slot MUST NOT silently inflate the smart_wallets_in count."""
        within_window = [WalletObservation(wallet=SMART_WALLET_1, slot=BASE_SLOT + 1)]
        clean = build_smart_wallet_features(
            pool_creation_event=POOL_CREATION,
            wallet_observations=within_window,
            tracked_wallets=TRACKED_WALLETS,
            first_k_slots=FIRST_K_SLOTS,
        )
        assert clean.smart_wallets_in == 1

        # Adding a future observation for a DIFFERENT tracked wallet must raise
        with pytest.raises(SmartWalletWindowError):
            build_smart_wallet_features(
                pool_creation_event=POOL_CREATION,
                wallet_observations=within_window + [
                    WalletObservation(wallet=SMART_WALLET_2, slot=BASE_SLOT + FIRST_K_SLOTS + 5)
                ],
                tracked_wallets=TRACKED_WALLETS,
                first_k_slots=FIRST_K_SLOTS,
            )

    def test_accumulator_raises_on_future_slot(self):
        """The online accumulator raises SmartWalletWindowError on future slot."""
        acc = SmartWalletAccumulator(
            mint=MINT_A,
            event_time_slot=BASE_SLOT,
            first_k_slots=FIRST_K_SLOTS,
            tracked_wallets=TRACKED_WALLETS,
        )
        with pytest.raises(SmartWalletWindowError):
            acc.observe_wallet_buy(
                wallet=SMART_WALLET_1,
                slot=BASE_SLOT + FIRST_K_SLOTS + 1,
            )

    def test_accumulator_state_unchanged_after_future_rejection(self):
        """Rejected future observation does not mutate accumulator state."""
        acc = SmartWalletAccumulator(
            mint=MINT_A,
            event_time_slot=BASE_SLOT,
            first_k_slots=FIRST_K_SLOTS,
            tracked_wallets=TRACKED_WALLETS,
        )
        acc.observe_wallet_buy(wallet=SMART_WALLET_1, slot=BASE_SLOT + 1)
        count_before = len(acc._observed_wallets)

        with pytest.raises(SmartWalletWindowError):
            acc.observe_wallet_buy(
                wallet=SMART_WALLET_2,
                slot=BASE_SLOT + FIRST_K_SLOTS + 1,
            )

        assert len(acc._observed_wallets) == count_before


# ---------------------------------------------------------------------------
# 3. Pre-creation slot rejection
# ---------------------------------------------------------------------------


class TestPreCreationSlotRejection:

    def test_pre_creation_slot_raises(self):
        """slot < creation_slot raises SmartWalletWindowError."""
        with pytest.raises(SmartWalletWindowError) as exc_info:
            build_smart_wallet_features(
                pool_creation_event=POOL_CREATION,
                wallet_observations=[
                    WalletObservation(wallet=SMART_WALLET_1, slot=BASE_SLOT - 1)
                ],
                tracked_wallets=TRACKED_WALLETS,
                first_k_slots=FIRST_K_SLOTS,
            )

        err = exc_info.value
        assert err.observation_slot == BASE_SLOT - 1
        assert err.creation_slot == BASE_SLOT
        assert "POINT-IN-TIME VIOLATION" in str(err)
        assert "pre-creation" in str(err).lower()

    def test_observation_at_creation_slot_accepted(self):
        """slot == creation_slot is the inclusive lower bound (valid)."""
        result = build_smart_wallet_features(
            pool_creation_event=POOL_CREATION,
            wallet_observations=[
                WalletObservation(wallet=SMART_WALLET_1, slot=BASE_SLOT)
            ],
            tracked_wallets=TRACKED_WALLETS,
            first_k_slots=FIRST_K_SLOTS,
        )
        assert result.smart_wallets_in == 1


# ---------------------------------------------------------------------------
# 4. Entry lag computation
# ---------------------------------------------------------------------------


class TestEntryLag:
    """Entry lag: our decision slot - earliest smart wallet fill slot."""

    def test_lag_is_positive_when_we_are_behind(self):
        """Positive lag when tracked wallet bought before us."""
        # Smart wallet bought at slot +1; our decision is slot +3
        result = build_smart_wallet_features(
            pool_creation_event=POOL_CREATION,
            wallet_observations=[
                WalletObservation(wallet=SMART_WALLET_1, slot=BASE_SLOT + 1),
            ],
            tracked_wallets=TRACKED_WALLETS,
            first_k_slots=FIRST_K_SLOTS,
            our_decision_slot=BASE_SLOT + 3,
        )
        assert result.smart_wallet_entry_lag_slots == 2  # 3 - 1 = 2 slots BEHIND

    def test_lag_is_earliest_fill(self):
        """Lag uses the EARLIEST smart wallet fill, not the latest."""
        # SW1 bought at slot +3, SW2 bought at slot +1 (earlier)
        result = build_smart_wallet_features(
            pool_creation_event=POOL_CREATION,
            wallet_observations=[
                WalletObservation(wallet=SMART_WALLET_1, slot=BASE_SLOT + 3),
                WalletObservation(wallet=SMART_WALLET_2, slot=BASE_SLOT + 1),
            ],
            tracked_wallets=TRACKED_WALLETS,
            first_k_slots=FIRST_K_SLOTS,
            our_decision_slot=BASE_SLOT + 5,
        )
        # Earliest is BASE_SLOT + 1, lag = 5 - 1 = 4
        assert result.smart_wallet_entry_lag_slots == 4
        assert result.earliest_smart_wallet_slot == BASE_SLOT + 1

    def test_lag_none_when_no_smart_wallets(self):
        """Lag is None when no smart wallets observed."""
        result = build_smart_wallet_features(
            pool_creation_event=POOL_CREATION,
            wallet_observations=[],
            tracked_wallets=TRACKED_WALLETS,
            first_k_slots=FIRST_K_SLOTS,
            our_decision_slot=BASE_SLOT + 5,
        )
        assert result.smart_wallet_entry_lag_slots is None

    def test_lag_uses_event_time_slot_when_decision_slot_none(self):
        """When our_decision_slot is None, lag uses event_time.slot."""
        result = build_smart_wallet_features(
            pool_creation_event=POOL_CREATION,
            wallet_observations=[
                WalletObservation(wallet=SMART_WALLET_1, slot=BASE_SLOT + 2),
            ],
            tracked_wallets=TRACKED_WALLETS,
            first_k_slots=FIRST_K_SLOTS,
            our_decision_slot=None,  # defaults to event_time.slot = BASE_SLOT
        )
        # lag = BASE_SLOT - (BASE_SLOT + 2) = -2 (we would be AHEAD, but that's unusual)
        assert result.smart_wallet_entry_lag_slots == -2


# ---------------------------------------------------------------------------
# 5. smart_wallets_in is a count feature — type contract check
# ---------------------------------------------------------------------------


class TestAdversarialContract:
    """smart_wallets_in is a COUNT. It has no 'sol_in', 'should_enter', or action fields.

    AC-009, AC-020: the downstream risk module (T-324) enforces that this count
    can only reduce or hold size, never increase it.  This test verifies the
    type contract: SmartWalletFeatures has NO position-size or action field.
    """

    def test_smart_wallet_features_has_no_position_size_field(self):
        """SmartWalletFeatures must have no sol_in, position_size, or action field."""
        result = build_smart_wallet_features(
            pool_creation_event=POOL_CREATION,
            wallet_observations=[],
            tracked_wallets=TRACKED_WALLETS,
            first_k_slots=FIRST_K_SLOTS,
        )
        # These fields must NOT exist on the result
        assert not hasattr(result, "sol_in")
        assert not hasattr(result, "position_size")
        assert not hasattr(result, "should_enter")
        assert not hasattr(result, "action")
        assert not hasattr(result, "entry_signal")

    def test_smart_wallets_in_is_int_count(self):
        """smart_wallets_in must be an int (count, not a ratio or float)."""
        result = build_smart_wallet_features(
            pool_creation_event=POOL_CREATION,
            wallet_observations=[
                WalletObservation(wallet=SMART_WALLET_1, slot=BASE_SLOT + 1),
            ],
            tracked_wallets=TRACKED_WALLETS,
            first_k_slots=FIRST_K_SLOTS,
        )
        assert isinstance(result.smart_wallets_in, int)
        assert not isinstance(result.smart_wallets_in, float)

    def test_no_label_or_outcome_fields(self):
        """SmartWalletFeatures must have no label, truth_*, or outcome fields."""
        result = build_smart_wallet_features(
            pool_creation_event=POOL_CREATION,
            wallet_observations=[],
            tracked_wallets=TRACKED_WALLETS,
            first_k_slots=FIRST_K_SLOTS,
        )
        forbidden = {
            "label", "truth_is_rug", "truth_max_multiple", "survived_60s",
            "realized_mult", "fwd_return", "rug_label", "outcome_label"
        }
        for field_name in forbidden:
            assert not hasattr(result, field_name), (
                f"SmartWalletFeatures must not have field {field_name!r}"
            )


# ---------------------------------------------------------------------------
# 6. Batch == streaming parity
# ---------------------------------------------------------------------------


class TestBatchStreamingParity:

    def test_empty_parity(self):
        """No events: batch and streaming produce identical results."""
        event_time = POOL_CREATION.event_time

        acc = SmartWalletAccumulator(
            mint=MINT_A,
            event_time_slot=BASE_SLOT,
            first_k_slots=FIRST_K_SLOTS,
            tracked_wallets=TRACKED_WALLETS,
        )
        streaming = acc.to_features(event_time)

        batch = build_smart_wallet_features(
            pool_creation_event=POOL_CREATION,
            wallet_observations=[],
            tracked_wallets=TRACKED_WALLETS,
            first_k_slots=FIRST_K_SLOTS,
        )

        assert streaming.smart_wallets_in == batch.smart_wallets_in
        assert streaming.smart_wallet_entry_lag_slots == batch.smart_wallet_entry_lag_slots

    def test_three_wallets_parity(self):
        """Three wallet observations: batch and streaming are identical."""
        observations = [
            WalletObservation(wallet=SMART_WALLET_1, slot=BASE_SLOT + 1),
            WalletObservation(wallet=SMART_WALLET_2, slot=BASE_SLOT + 2),
            WalletObservation(wallet=SMART_WALLET_3, slot=BASE_SLOT + 4),
        ]

        # Online
        acc = SmartWalletAccumulator(
            mint=MINT_A,
            event_time_slot=BASE_SLOT,
            first_k_slots=FIRST_K_SLOTS,
            tracked_wallets=TRACKED_WALLETS,
        )
        for obs in observations:
            acc.observe_wallet_buy(wallet=obs.wallet, slot=obs.slot)
        streaming = acc.to_features(POOL_CREATION.event_time, our_decision_slot=BASE_SLOT + 5)

        # Batch
        batch = build_smart_wallet_features(
            pool_creation_event=POOL_CREATION,
            wallet_observations=observations,
            tracked_wallets=TRACKED_WALLETS,
            first_k_slots=FIRST_K_SLOTS,
            our_decision_slot=BASE_SLOT + 5,
        )

        assert streaming.smart_wallets_in == batch.smart_wallets_in == 3
        assert streaming.smart_wallet_entry_lag_slots == batch.smart_wallet_entry_lag_slots
        assert streaming.earliest_smart_wallet_slot == batch.earliest_smart_wallet_slot


# ---------------------------------------------------------------------------
# 7. Provenance cutoff
# ---------------------------------------------------------------------------


class TestProvenanceCutoff:

    def test_provenance_windows_within_cutoff(self):
        result = build_smart_wallet_features(
            pool_creation_event=POOL_CREATION,
            wallet_observations=[
                WalletObservation(wallet=SMART_WALLET_1, slot=BASE_SLOT + FIRST_K_SLOTS)
            ],
            tracked_wallets=TRACKED_WALLETS,
            first_k_slots=FIRST_K_SLOTS,
        )
        cutoff = BASE_SLOT + FIRST_K_SLOTS
        for window in result.provenance.windows:
            assert window.max_source_slot <= cutoff

    def test_provenance_feature_names(self):
        result = build_smart_wallet_features(
            pool_creation_event=POOL_CREATION,
            wallet_observations=[],
            tracked_wallets=TRACKED_WALLETS,
            first_k_slots=FIRST_K_SLOTS,
        )
        feature_names = {w.feature_name for w in result.provenance.windows}
        assert "smart_wallets_in" in feature_names
        assert "smart_wallet_entry_lag_slots" in feature_names

    def test_provenance_lineage_is_launch_events(self):
        result = build_smart_wallet_features(
            pool_creation_event=POOL_CREATION,
            wallet_observations=[],
            tracked_wallets=TRACKED_WALLETS,
            first_k_slots=FIRST_K_SLOTS,
        )
        for window in result.provenance.windows:
            assert window.lineage_dataset == "launch_events"


# ---------------------------------------------------------------------------
# 8. Reset
# ---------------------------------------------------------------------------


class TestReset:

    def test_reset_clears_all_state(self):
        acc = SmartWalletAccumulator(
            mint=MINT_A,
            event_time_slot=BASE_SLOT,
            first_k_slots=FIRST_K_SLOTS,
            tracked_wallets=TRACKED_WALLETS,
        )
        acc.observe_wallet_buy(wallet=SMART_WALLET_1, slot=BASE_SLOT + 1)
        assert len(acc._observed_wallets) == 1

        acc.reset()
        assert len(acc._observed_wallets) == 0
        assert acc._max_source_slot == 0
        assert acc._event_count == 0
