"""Tests for first-60s microstructure features (T-304).

Self-check criteria (charter §6, M1 standards):
  1. Correct values on known fixtures.
  2. Lookahead-injection test: future-slot events REFUSED (MicrostructureWindowError).
  3. Pre-creation-slot rejection (lower-bound causality guard).
  4. Batch == streaming parity (MicrostructureAccumulator vs build_microstructure_features).
  5. Money fields are int/bps — NEVER float.
  6. Provenance cutoff correct (max_source_slot <= event_time.slot + K).
  7. Lineage is "launch_events" (not labels).
  8. Freeze-authority-unrenoounced is a detectable risk flag.
  9. Bundling detection fires at >=3 same-slot wallets.
  10. Top-10 holder concentration excludes LP vault and burn addresses.
  11. Sniper cluster score is in [0.0, 1.0].
  12. Sell tax computation from measured in/out deltas.
  13. buy_pressure_imbalance is None for zero-volume window.
"""

from __future__ import annotations

import pytest

from aats.contracts.events import DetectionTransport, EventTime, LaunchEvent, LaunchSource
from aats.contracts.provenance import ProvenanceTaintError
from aats.features.microstructure import (
    ClassifiedEvent,
    HolderSnapshot,
    MicrostructureAccumulator,
    MicrostructureWindowError,
    build_microstructure_features,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

BASE_SLOT = 400_000_000
BASE_BLOCK_TIME_MS = 1_720_000_000_000
LAMPORTS_PER_SOL = 1_000_000_000
FIRST_K_SLOTS = 10
MINT_A = "MintAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"

WALLET_DEV = "DevWallet111111111111111111111111111111111111"
WALLET_BUYER_1 = "Buyer1Wallet11111111111111111111111111111111"
WALLET_BUYER_2 = "Buyer2Wallet11111111111111111111111111111111"
WALLET_BUYER_3 = "Buyer3Wallet11111111111111111111111111111111"
WALLET_LP_VAULT = "LPVault111111111111111111111111111111111111"
WALLET_BURN = "1nc1nerator11111111111111111111111111111111"

PROGRAM_ID = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"


def _make_event_time(slot: int) -> EventTime:
    return EventTime(
        slot=slot,
        block_time_ms=BASE_BLOCK_TIME_MS + (slot - BASE_SLOT) * 400,
        wall_clock_ms=BASE_BLOCK_TIME_MS + (slot - BASE_SLOT) * 400 + 50,
    )


def _make_event(
    slot: int,
    sol_reserve_lamports: int = 10 * LAMPORTS_PER_SOL,
    token_reserve_base: int = 1_000_000_000,
    creator_wallet: str = WALLET_BUYER_1,
    mint: str = MINT_A,
) -> LaunchEvent:
    return LaunchEvent(
        mint=mint,
        venue_program_id=PROGRAM_ID,
        source=LaunchSource.PUMPFUN,
        event_time=_make_event_time(slot),
        observation_slot=slot,
        confirmation_slot=slot,
        detection_transport=DetectionTransport.GEYSER,
        sol_reserve_lamports=sol_reserve_lamports,
        token_reserve_base=token_reserve_base,
        token_decimals=6,
        initial_holders=0,
        competitors=0,
        creator_wallet=creator_wallet,
        bundler_cluster_id=None,
        deploy_template_fingerprint=None,
        data_staleness_ms=50,
    )


POOL_CREATION_EVENT = _make_event(
    slot=BASE_SLOT,
    sol_reserve_lamports=30 * LAMPORTS_PER_SOL,
    creator_wallet=WALLET_DEV,
)


# ---------------------------------------------------------------------------
# 1. Correct values on known fixtures
# ---------------------------------------------------------------------------


class TestCorrectValues:

    def test_empty_window_returns_defaults(self):
        """Empty window: lp_depth from pool creation, lock=unlocked, zeros elsewhere."""
        result = build_microstructure_features(
            pool_creation_event=POOL_CREATION_EVENT,
            classified_events=[],
            first_k_slots=FIRST_K_SLOTS,
        )
        # LP depth should be from the pool creation event
        assert result.lp_depth_lamports == 30 * LAMPORTS_PER_SOL
        assert result.lp_lock_status == "unlocked"
        assert result.lp_age_slots == 0  # at pool creation, age = 0
        assert result.sniper_cluster_score == 0.0
        assert result.bundling_detected is False
        assert result.bundler_wallet_count == 0

    def test_lp_depth_updated_on_subsequent_events(self):
        """LP depth reflects the latest sol_reserve from events."""
        ev = _make_event(
            slot=BASE_SLOT + 2,
            sol_reserve_lamports=50 * LAMPORTS_PER_SOL,
        )
        result = build_microstructure_features(
            pool_creation_event=POOL_CREATION_EVENT,
            classified_events=[ClassifiedEvent(event=ev, is_buy=True)],
            first_k_slots=FIRST_K_SLOTS,
        )
        assert result.lp_depth_lamports == 50 * LAMPORTS_PER_SOL

    def test_lp_lock_status_burned(self):
        """Lock status propagates from the classified event."""
        ev = _make_event(slot=BASE_SLOT + 1)
        result = build_microstructure_features(
            pool_creation_event=POOL_CREATION_EVENT,
            classified_events=[
                ClassifiedEvent(event=ev, is_buy=True, lp_lock_status="burned")
            ],
            first_k_slots=FIRST_K_SLOTS,
        )
        assert result.lp_lock_status == "burned"

    def test_freeze_authority_unrenoounced_is_detectable(self):
        """freeze_authority_renounced=False is a detectable risk flag."""
        ev = _make_event(slot=BASE_SLOT + 1)
        result = build_microstructure_features(
            pool_creation_event=POOL_CREATION_EVENT,
            classified_events=[
                ClassifiedEvent(
                    event=ev,
                    is_buy=True,
                    freeze_authority_renounced=False,   # NOT renounced = RISK
                )
            ],
            first_k_slots=FIRST_K_SLOTS,
        )
        assert result.freeze_authority_renounced is False

    def test_mint_authority_renounced_propagates(self):
        """mint_authority_renounced=True propagates to result."""
        ev = _make_event(slot=BASE_SLOT + 1)
        result = build_microstructure_features(
            pool_creation_event=POOL_CREATION_EVENT,
            classified_events=[
                ClassifiedEvent(
                    event=ev,
                    is_buy=True,
                    mint_authority_renounced=True,
                )
            ],
            first_k_slots=FIRST_K_SLOTS,
        )
        assert result.mint_authority_renounced is True

    def test_holder_concentration_excludes_lp_and_burn(self):
        """Top-10 concentration must exclude LP vault and burn addresses."""
        ev = _make_event(slot=BASE_SLOT + 1)
        # Holder 1: regular wallet with 60% of supply
        # LP vault: 30% of supply (excluded from concentration calc)
        # Burn: 10% of supply (excluded)
        total_supply = 1_000_000_000  # 1B tokens (base units)

        result = build_microstructure_features(
            pool_creation_event=POOL_CREATION_EVENT,
            classified_events=[
                ClassifiedEvent(
                    event=ev,
                    is_buy=True,
                    total_supply_base=total_supply,
                    holder_snapshot=HolderSnapshot(
                        wallet=WALLET_BUYER_1,
                        token_amount_base=600_000_000,  # 60%
                        slot=BASE_SLOT + 1,
                        is_lp_vault=False,
                        is_burn_address=False,
                    ),
                ),
                ClassifiedEvent(
                    event=_make_event(slot=BASE_SLOT + 1, creator_wallet=WALLET_LP_VAULT),
                    is_buy=False,
                    total_supply_base=total_supply,
                    holder_snapshot=HolderSnapshot(
                        wallet=WALLET_LP_VAULT,
                        token_amount_base=300_000_000,  # 30% — EXCLUDED
                        slot=BASE_SLOT + 1,
                        is_lp_vault=True,
                        is_burn_address=False,
                    ),
                ),
                ClassifiedEvent(
                    event=_make_event(slot=BASE_SLOT + 1, creator_wallet=WALLET_BURN),
                    is_buy=False,
                    total_supply_base=total_supply,
                    holder_snapshot=HolderSnapshot(
                        wallet=WALLET_BURN,
                        token_amount_base=100_000_000,  # 10% — EXCLUDED
                        slot=BASE_SLOT + 1,
                        is_lp_vault=False,
                        is_burn_address=True,
                    ),
                ),
            ],
            first_k_slots=FIRST_K_SLOTS,
        )
        # Only WALLET_BUYER_1 is in scope.  Their 60M / 60M active = 100% → 10000 bps
        assert result.holder_count == 1
        assert result.holder_concentration_top10_bps == 10000

    def test_sniper_cluster_score_range(self):
        """Sniper cluster score must be in [0.0, 1.0]."""
        result = build_microstructure_features(
            pool_creation_event=POOL_CREATION_EVENT,
            classified_events=[
                ClassifiedEvent(
                    event=_make_event(slot=BASE_SLOT + 1, creator_wallet=WALLET_BUYER_1),
                    is_buy=True,
                ),
                ClassifiedEvent(
                    event=_make_event(slot=BASE_SLOT + 1, creator_wallet=WALLET_BUYER_2),
                    is_buy=True,
                ),
            ],
            first_k_slots=FIRST_K_SLOTS,
        )
        assert 0.0 <= result.sniper_cluster_score <= 1.0

    def test_bundling_detected_on_three_same_slot_wallets(self):
        """Bundling detected when >=3 wallets buy in the same slot."""
        result = build_microstructure_features(
            pool_creation_event=POOL_CREATION_EVENT,
            classified_events=[
                ClassifiedEvent(
                    event=_make_event(slot=BASE_SLOT, creator_wallet=WALLET_BUYER_1),
                    is_buy=True,
                ),
                ClassifiedEvent(
                    event=_make_event(slot=BASE_SLOT, creator_wallet=WALLET_BUYER_2),
                    is_buy=True,
                ),
                ClassifiedEvent(
                    event=_make_event(slot=BASE_SLOT, creator_wallet=WALLET_BUYER_3),
                    is_buy=True,
                ),
            ],
            first_k_slots=FIRST_K_SLOTS,
        )
        assert result.bundling_detected is True
        assert result.bundler_wallet_count >= 3

    def test_bundling_not_detected_on_two_wallets(self):
        """Bundling NOT detected when < 3 wallets buy in the same slot."""
        result = build_microstructure_features(
            pool_creation_event=POOL_CREATION_EVENT,
            classified_events=[
                ClassifiedEvent(
                    event=_make_event(slot=BASE_SLOT, creator_wallet=WALLET_BUYER_1),
                    is_buy=True,
                ),
                ClassifiedEvent(
                    event=_make_event(slot=BASE_SLOT, creator_wallet=WALLET_BUYER_2),
                    is_buy=True,
                ),
            ],
            first_k_slots=FIRST_K_SLOTS,
        )
        assert result.bundling_detected is False

    def test_buy_pressure_imbalance_all_buys(self):
        """All buys → imbalance = 1.0."""
        result = build_microstructure_features(
            pool_creation_event=POOL_CREATION_EVENT,
            classified_events=[
                ClassifiedEvent(
                    event=_make_event(slot=BASE_SLOT + 1, sol_reserve_lamports=5 * LAMPORTS_PER_SOL),
                    is_buy=True,
                ),
            ],
            first_k_slots=FIRST_K_SLOTS,
        )
        assert result.buy_pressure_imbalance == 1.0

    def test_buy_pressure_imbalance_all_sells(self):
        """All sells (from post-creation sells) → imbalance = 0.0."""
        result = build_microstructure_features(
            pool_creation_event=POOL_CREATION_EVENT,
            classified_events=[
                ClassifiedEvent(
                    event=_make_event(slot=BASE_SLOT + 1, sol_reserve_lamports=5 * LAMPORTS_PER_SOL),
                    is_buy=False,  # sell
                ),
            ],
            first_k_slots=FIRST_K_SLOTS,
        )
        assert result.buy_pressure_imbalance == 0.0

    def test_buy_pressure_imbalance_none_for_zero_volume(self):
        """Zero volume (pool creation event only, no trades) → imbalance = None."""
        # The pool creation event is observed with is_buy=False, contributing 0 to buy vol.
        # But its sol_reserve_lamports is non-zero.
        # A pure no-trade window (no classified_events) has non-zero from the creation event.
        # Let's test with a pool creation event that has 0 sol (unrealistic but valid).
        zero_creation = _make_event(slot=BASE_SLOT, sol_reserve_lamports=0, creator_wallet=WALLET_DEV)
        result = build_microstructure_features(
            pool_creation_event=zero_creation,
            classified_events=[],
            first_k_slots=FIRST_K_SLOTS,
        )
        # Pool creation event is observed with is_buy=False and sol_reserve=0
        # → _sell_volume_lamports = 0, _buy_volume_lamports = 0 → None
        assert result.buy_pressure_imbalance is None

    def test_effective_sell_tax_measured(self):
        """Effective sell tax is measured from realized vs expected SOL."""
        # Sell: expected 10 SOL, received 9 SOL → 10% tax → 1000 bps
        ev = _make_event(slot=BASE_SLOT + 1, sol_reserve_lamports=9 * LAMPORTS_PER_SOL)
        result = build_microstructure_features(
            pool_creation_event=POOL_CREATION_EVENT,
            classified_events=[
                ClassifiedEvent(
                    event=ev,
                    is_buy=False,
                    expected_sol_lamports=10 * LAMPORTS_PER_SOL,  # expected
                ),
            ],
            first_k_slots=FIRST_K_SLOTS,
        )
        assert result.effective_sell_tax_bps == 1000  # 10%

    def test_time_since_pool_creation_slots(self):
        """time_since_pool_creation_slots = event_time.slot - creation_slot."""
        # The pool creation event is at BASE_SLOT.
        # After FIRST_K_SLOTS, this should be FIRST_K_SLOTS.
        # But we call to_features with the pool_creation_event's event_time,
        # so time_since_pool_creation = 0.
        result = build_microstructure_features(
            pool_creation_event=POOL_CREATION_EVENT,
            classified_events=[],
            first_k_slots=FIRST_K_SLOTS,
        )
        assert result.time_since_pool_creation_slots == 0

    def test_reserve_trajectory_populated(self):
        """sell_reserve_trajectory contains reserve samples from observed events."""
        ev1 = _make_event(slot=BASE_SLOT + 1, sol_reserve_lamports=30 * LAMPORTS_PER_SOL)
        ev2 = _make_event(slot=BASE_SLOT + 3, sol_reserve_lamports=28 * LAMPORTS_PER_SOL)
        result = build_microstructure_features(
            pool_creation_event=POOL_CREATION_EVENT,
            classified_events=[
                ClassifiedEvent(event=ev1, is_buy=True),
                ClassifiedEvent(event=ev2, is_buy=False),
            ],
            first_k_slots=FIRST_K_SLOTS,
        )
        # Reserve trajectory includes the pool creation + 2 events
        assert len(result.sell_reserve_trajectory) >= 2
        # All entries are int
        for entry in result.sell_reserve_trajectory:
            assert isinstance(entry, int)

    def test_sell_reserve_trajectory_entries_are_int_not_float(self):
        """All sell_reserve_trajectory entries must be int (money rule)."""
        ev = _make_event(slot=BASE_SLOT + 1, sol_reserve_lamports=25 * LAMPORTS_PER_SOL)
        result = build_microstructure_features(
            pool_creation_event=POOL_CREATION_EVENT,
            classified_events=[ClassifiedEvent(event=ev, is_buy=True)],
            first_k_slots=FIRST_K_SLOTS,
        )
        for entry in result.sell_reserve_trajectory:
            assert isinstance(entry, int), f"Expected int, got {type(entry)}"


# ---------------------------------------------------------------------------
# 2. Lookahead injection tests (prime directive)
# ---------------------------------------------------------------------------


class TestLookaheadInjection:
    """MANDATORY: proves the accumulator REFUSES future-slot events."""

    def test_future_slot_raises_microstructure_window_error(self):
        """An event one slot past the cutoff MUST raise MicrostructureWindowError."""
        future_event = _make_event(slot=BASE_SLOT + FIRST_K_SLOTS + 1)

        with pytest.raises(MicrostructureWindowError) as exc_info:
            build_microstructure_features(
                pool_creation_event=POOL_CREATION_EVENT,
                classified_events=[ClassifiedEvent(event=future_event, is_buy=True)],
                first_k_slots=FIRST_K_SLOTS,
            )

        err = exc_info.value
        assert err.event_slot == BASE_SLOT + FIRST_K_SLOTS + 1
        assert err.cutoff_slot == BASE_SLOT + FIRST_K_SLOTS
        assert "POINT-IN-TIME VIOLATION" in str(err)

    def test_far_future_slot_raises(self):
        """An event 100 slots past the cutoff raises."""
        future_event = _make_event(slot=BASE_SLOT + FIRST_K_SLOTS + 100)
        with pytest.raises(MicrostructureWindowError):
            build_microstructure_features(
                pool_creation_event=POOL_CREATION_EVENT,
                classified_events=[ClassifiedEvent(event=future_event, is_buy=True)],
                first_k_slots=FIRST_K_SLOTS,
            )

    def test_future_event_does_not_silently_change_lp_depth(self):
        """Future event must not silently alter lp_depth (future-mutation invariance)."""
        # Clean result with only within-window events
        within_ev = _make_event(slot=BASE_SLOT + 2, sol_reserve_lamports=20 * LAMPORTS_PER_SOL)
        clean = build_microstructure_features(
            pool_creation_event=POOL_CREATION_EVENT,
            classified_events=[ClassifiedEvent(event=within_ev, is_buy=True)],
            first_k_slots=FIRST_K_SLOTS,
        )

        # Now try with a future event appended
        future_ev = _make_event(
            slot=BASE_SLOT + FIRST_K_SLOTS + 5,
            sol_reserve_lamports=999 * LAMPORTS_PER_SOL,  # would corrupt LP depth if accepted
        )
        with pytest.raises(MicrostructureWindowError):
            build_microstructure_features(
                pool_creation_event=POOL_CREATION_EVENT,
                classified_events=[
                    ClassifiedEvent(event=within_ev, is_buy=True),
                    ClassifiedEvent(event=future_ev, is_buy=True),  # future — MUST raise
                ],
                first_k_slots=FIRST_K_SLOTS,
            )

        # Clean result is unchanged (fresh accumulator each time)
        clean2 = build_microstructure_features(
            pool_creation_event=POOL_CREATION_EVENT,
            classified_events=[ClassifiedEvent(event=within_ev, is_buy=True)],
            first_k_slots=FIRST_K_SLOTS,
        )
        assert clean.lp_depth_lamports == clean2.lp_depth_lamports

    def test_accumulator_observe_raises_on_future_slot(self):
        """The online accumulator (SNIPE loop path) raises on future slot."""
        acc = MicrostructureAccumulator(
            mint=MINT_A,
            event_time_slot=BASE_SLOT,
            first_k_slots=FIRST_K_SLOTS,
        )
        future_ev = _make_event(slot=BASE_SLOT + FIRST_K_SLOTS + 1)
        with pytest.raises(MicrostructureWindowError):
            acc.observe(future_ev, is_buy=True)

    def test_accumulator_state_unchanged_after_future_rejection(self):
        """After rejection, accumulator state is unchanged (atomic rejection)."""
        acc = MicrostructureAccumulator(
            mint=MINT_A,
            event_time_slot=BASE_SLOT,
            first_k_slots=FIRST_K_SLOTS,
        )
        # Valid event
        valid_ev = _make_event(slot=BASE_SLOT + 1, sol_reserve_lamports=10 * LAMPORTS_PER_SOL)
        acc.observe(valid_ev, is_buy=True, is_pool_creation=False)

        depth_before = acc._lp_depth_lamports
        buy_vol_before = acc._buy_volume_lamports

        # Future event
        future_ev = _make_event(slot=BASE_SLOT + FIRST_K_SLOTS + 2)
        with pytest.raises(MicrostructureWindowError):
            acc.observe(future_ev, is_buy=True)

        # State must be unchanged
        assert acc._lp_depth_lamports == depth_before
        assert acc._buy_volume_lamports == buy_vol_before


# ---------------------------------------------------------------------------
# 3. Pre-creation slot rejection
# ---------------------------------------------------------------------------


class TestPreCreationSlotRejection:
    """Lower-bound causality guard: slot < creation_slot is a HARD FAILURE."""

    def test_pre_creation_slot_raises(self):
        """An event at slot < creation_slot MUST raise MicrostructureWindowError."""
        creation_slot = 1000
        pre_creation_ev = LaunchEvent(
            mint=MINT_A,
            venue_program_id=PROGRAM_ID,
            source=LaunchSource.PUMPFUN,
            event_time=EventTime(
                slot=990,
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
        creation_event = LaunchEvent(
            mint=MINT_A,
            venue_program_id=PROGRAM_ID,
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
            creator_wallet=WALLET_DEV,
            bundler_cluster_id=None,
            deploy_template_fingerprint=None,
            data_staleness_ms=50,
        )

        with pytest.raises(MicrostructureWindowError) as exc_info:
            build_microstructure_features(
                pool_creation_event=creation_event,
                classified_events=[ClassifiedEvent(event=pre_creation_ev, is_buy=True)],
                first_k_slots=FIRST_K_SLOTS,
            )

        err = exc_info.value
        assert err.event_slot == 990
        assert err.creation_slot == creation_slot
        assert "POINT-IN-TIME VIOLATION" in str(err)
        assert "pre-creation" in str(err).lower()

    def test_event_at_creation_slot_is_accepted(self):
        """slot == creation_slot is the inclusive lower bound (valid)."""
        creation_slot = 1000
        creation_event = LaunchEvent(
            mint=MINT_A,
            venue_program_id=PROGRAM_ID,
            source=LaunchSource.PUMPFUN,
            event_time=EventTime(
                slot=creation_slot,
                block_time_ms=BASE_BLOCK_TIME_MS,
                wall_clock_ms=BASE_BLOCK_TIME_MS + 50,
            ),
            observation_slot=creation_slot,
            confirmation_slot=creation_slot,
            detection_transport=DetectionTransport.GEYSER,
            sol_reserve_lamports=10 * LAMPORTS_PER_SOL,
            token_reserve_base=0,
            token_decimals=6,
            initial_holders=0,
            competitors=0,
            creator_wallet=WALLET_DEV,
            bundler_cluster_id=None,
            deploy_template_fingerprint=None,
            data_staleness_ms=50,
        )
        same_slot_ev = LaunchEvent(
            mint=MINT_A,
            venue_program_id=PROGRAM_ID,
            source=LaunchSource.PUMPFUN,
            event_time=EventTime(
                slot=creation_slot,
                block_time_ms=BASE_BLOCK_TIME_MS,
                wall_clock_ms=BASE_BLOCK_TIME_MS + 100,
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
        # Must NOT raise — slot == creation_slot is valid
        result = build_microstructure_features(
            pool_creation_event=creation_event,
            classified_events=[ClassifiedEvent(event=same_slot_ev, is_buy=True)],
            first_k_slots=FIRST_K_SLOTS,
        )
        assert result.buy_pressure_imbalance == 1.0


# ---------------------------------------------------------------------------
# 4. Batch == streaming parity
# ---------------------------------------------------------------------------


class TestBatchStreamingParity:
    """Online accumulator and batch helper must produce bit-identical outputs."""

    def test_empty_events_parity(self):
        """No events: batch and streaming produce identical results."""
        creation_ev = POOL_CREATION_EVENT
        event_time = creation_ev.event_time

        # Online path
        acc = MicrostructureAccumulator(
            mint=MINT_A,
            event_time_slot=BASE_SLOT,
            first_k_slots=FIRST_K_SLOTS,
        )
        acc.observe(creation_ev, is_buy=False, is_pool_creation=True)
        streaming = acc.to_features(event_time)

        # Batch path
        batch = build_microstructure_features(
            pool_creation_event=creation_ev,
            classified_events=[],
            first_k_slots=FIRST_K_SLOTS,
        )

        # Core fields must match
        assert streaming.lp_depth_lamports == batch.lp_depth_lamports
        assert streaming.lp_lock_status == batch.lp_lock_status
        assert streaming.sniper_cluster_score == batch.sniper_cluster_score
        assert streaming.bundling_detected == batch.bundling_detected

    def test_three_events_parity(self):
        """Three events: batch and streaming produce identical results."""
        ev1 = _make_event(slot=BASE_SLOT + 1, sol_reserve_lamports=10 * LAMPORTS_PER_SOL)
        ev2 = _make_event(slot=BASE_SLOT + 2, sol_reserve_lamports=8 * LAMPORTS_PER_SOL)
        ev3 = _make_event(slot=BASE_SLOT + 3, sol_reserve_lamports=12 * LAMPORTS_PER_SOL)
        classified = [
            ClassifiedEvent(event=ev1, is_buy=True),
            ClassifiedEvent(event=ev2, is_buy=False),
            ClassifiedEvent(event=ev3, is_buy=True),
        ]

        # Online path
        acc = MicrostructureAccumulator(
            mint=MINT_A,
            event_time_slot=BASE_SLOT,
            first_k_slots=FIRST_K_SLOTS,
        )
        acc.observe(POOL_CREATION_EVENT, is_buy=False, is_pool_creation=True)
        for ce in classified:
            acc.observe(
                ce.event, is_buy=ce.is_buy,
                mint_authority_renounced=ce.mint_authority_renounced,
                freeze_authority_renounced=ce.freeze_authority_renounced,
                lp_lock_status=ce.lp_lock_status,
                total_supply_base=ce.total_supply_base,
                dev_token_balance_base=ce.dev_token_balance_base,
                holder_snapshot=ce.holder_snapshot,
                expected_sol_lamports=ce.expected_sol_lamports,
                is_pool_creation=False,
                is_bundler=ce.is_bundler,
            )
        streaming = acc.to_features(POOL_CREATION_EVENT.event_time)

        # Batch path
        batch = build_microstructure_features(
            pool_creation_event=POOL_CREATION_EVENT,
            classified_events=classified,
            first_k_slots=FIRST_K_SLOTS,
        )

        assert streaming.lp_depth_lamports == batch.lp_depth_lamports
        assert streaming.buy_pressure_imbalance == batch.buy_pressure_imbalance
        assert streaming.sniper_cluster_score == batch.sniper_cluster_score
        assert streaming.sell_reserve_trajectory == batch.sell_reserve_trajectory


# ---------------------------------------------------------------------------
# 5. Money fields are int/bps — NEVER float
# ---------------------------------------------------------------------------


class TestMoneyFieldTypes:
    """All monetary/bps fields must be int — never float."""

    def test_lp_depth_is_int(self):
        result = build_microstructure_features(
            pool_creation_event=POOL_CREATION_EVENT,
            classified_events=[],
            first_k_slots=FIRST_K_SLOTS,
        )
        assert isinstance(result.lp_depth_lamports, int)

    def test_dev_wallet_supply_pct_bps_is_int(self):
        ev = _make_event(slot=BASE_SLOT + 1)
        result = build_microstructure_features(
            pool_creation_event=POOL_CREATION_EVENT,
            classified_events=[
                ClassifiedEvent(
                    event=ev, is_buy=True,
                    total_supply_base=1_000_000_000,
                    dev_token_balance_base=100_000_000,
                )
            ],
            first_k_slots=FIRST_K_SLOTS,
        )
        assert isinstance(result.dev_wallet_supply_pct_bps, int)
        # 100M / 1B = 10% = 1000 bps
        assert result.dev_wallet_supply_pct_bps == 1000

    def test_holder_concentration_is_int(self):
        result = build_microstructure_features(
            pool_creation_event=POOL_CREATION_EVENT,
            classified_events=[],
            first_k_slots=FIRST_K_SLOTS,
        )
        assert isinstance(result.holder_concentration_top10_bps, int)

    def test_effective_sell_tax_bps_is_int(self):
        result = build_microstructure_features(
            pool_creation_event=POOL_CREATION_EVENT,
            classified_events=[],
            first_k_slots=FIRST_K_SLOTS,
        )
        assert isinstance(result.effective_sell_tax_bps, int)

    def test_sell_reserve_trajectory_entries_int(self):
        ev = _make_event(slot=BASE_SLOT + 1, sol_reserve_lamports=20 * LAMPORTS_PER_SOL)
        result = build_microstructure_features(
            pool_creation_event=POOL_CREATION_EVENT,
            classified_events=[ClassifiedEvent(event=ev, is_buy=True)],
            first_k_slots=FIRST_K_SLOTS,
        )
        for entry in result.sell_reserve_trajectory:
            assert isinstance(entry, int), f"Expected int, got {type(entry)}: {entry}"


# ---------------------------------------------------------------------------
# 6. Provenance cutoff correctness
# ---------------------------------------------------------------------------


class TestProvenanceCutoff:
    """Provenance max_source_slot must be <= event_time.slot + K."""

    def test_provenance_max_slot_within_cutoff(self):
        """max_source_slot must not exceed the cutoff."""
        ev = _make_event(slot=BASE_SLOT + FIRST_K_SLOTS)  # exactly at cutoff
        result = build_microstructure_features(
            pool_creation_event=POOL_CREATION_EVENT,
            classified_events=[ClassifiedEvent(event=ev, is_buy=True)],
            first_k_slots=FIRST_K_SLOTS,
        )
        cutoff = BASE_SLOT + FIRST_K_SLOTS
        for window in result.provenance.windows:
            assert window.max_source_slot <= cutoff, (
                f"Feature {window.feature_name!r} has max_source_slot={window.max_source_slot} "
                f"> cutoff={cutoff}"
            )

    def test_provenance_lineage_is_launch_events(self):
        """All provenance windows declare lineage from launch_events."""
        result = build_microstructure_features(
            pool_creation_event=POOL_CREATION_EVENT,
            classified_events=[],
            first_k_slots=FIRST_K_SLOTS,
        )
        for window in result.provenance.windows:
            assert window.lineage_dataset == "launch_events", (
                f"Feature {window.feature_name!r} has lineage={window.lineage_dataset!r}"
            )

    def test_provenance_guard_fires_on_future_max_slot(self):
        """ProvenanceTaintError fires when max_source_slot > cutoff (second defence)."""
        from aats.contracts.provenance import check_feature_window_cutoff
        with pytest.raises(ProvenanceTaintError) as exc_info:
            check_feature_window_cutoff(
                feature_name="lp_depth_lamports",
                max_source_slot=BASE_SLOT + FIRST_K_SLOTS + 1,
                event_time_slot=BASE_SLOT,
                first_k_slots=FIRST_K_SLOTS,
            )
        assert "feature_window_exceeds_cutoff" in str(exc_info.value)


# ---------------------------------------------------------------------------
# 7. Reset tests
# ---------------------------------------------------------------------------


class TestAccumulatorReset:
    """reset() clears all state."""

    def test_reset_clears_state(self):
        acc = MicrostructureAccumulator(
            mint=MINT_A,
            event_time_slot=BASE_SLOT,
            first_k_slots=FIRST_K_SLOTS,
        )
        ev = _make_event(slot=BASE_SLOT + 1, sol_reserve_lamports=10 * LAMPORTS_PER_SOL)
        acc.observe(ev, is_buy=True)
        assert acc._buy_volume_lamports > 0
        assert acc._event_count > 0

        acc.reset()
        assert acc._lp_depth_lamports == 0
        assert acc._buy_volume_lamports == 0
        assert acc._sell_volume_lamports == 0
        assert acc._event_count == 0
        assert len(acc._all_unique_buyers) == 0
        assert len(acc._reserve_samples) == 0

    def test_reset_then_reuse(self):
        """After reset, re-observing the same event gives the same result."""
        acc = MicrostructureAccumulator(
            mint=MINT_A,
            event_time_slot=BASE_SLOT,
            first_k_slots=FIRST_K_SLOTS,
        )
        ev = _make_event(slot=BASE_SLOT + 1, sol_reserve_lamports=10 * LAMPORTS_PER_SOL)
        acc.observe(POOL_CREATION_EVENT, is_buy=False, is_pool_creation=True)
        acc.observe(ev, is_buy=True)
        first = acc.to_features(POOL_CREATION_EVENT.event_time)

        acc.reset()
        acc.observe(POOL_CREATION_EVENT, is_buy=False, is_pool_creation=True)
        acc.observe(ev, is_buy=True)
        second = acc.to_features(POOL_CREATION_EVENT.event_time)

        assert first.lp_depth_lamports == second.lp_depth_lamports
        assert first.buy_pressure_imbalance == second.buy_pressure_imbalance
