"""M2-CP-07 — tests for the creator/dev-wallet post-migration OUTFLOW-VELOCITY
feature (`aats.ingestion.creator_outflow_velocity`).

Covers (VERIFY criteria from the task brief):
  - the feature RISES with creator outflow (pure arithmetic + tracker level)
  - the monotone-decreasing (survivor-probability) constraint holds
    (structural pin here; the live sign-test-against-a-trained-model lives in
    tests/models/test_creator_outflow_survivor_covariate.py)
  - event-time honesty: event_slot/block_time_ms come from on-chain fields
    only, never wall-clock; unconfirmed block_time is held PENDING
  - refuse-by-default: an unregistered mint, a degenerate baseline, and
    undecodable account data are NEVER silently treated as "no outflow"
  - idempotent + order-tolerant: duplicated + out-of-order replay produces
    the IDENTICAL final feature snapshot as in-order, once-each delivery
  - the MigrationEvent (LaunchEvent) and RawAccountUpdate adapters
  - the bus publisher wire format
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from aats.contracts.events import DetectionTransport, EventTime, LaunchEvent, LaunchSource
from aats.ingestion.creator_outflow_velocity import (
    BPS_DENOM,
    MAXLEN_CREATOR_OUTFLOW,
    STREAM_CREATOR_OUTFLOW,
    VELOCITY_NORMALIZATION_SLOTS,
    CreatorOutflowBusPublisher,
    CreatorOutflowObservation,
    CreatorOutflowVelocityFeature,
    CreatorOutflowVelocityTracker,
    compute_outflow_velocity_bps,
    creator_outflow_observation_from_account_update,
    register_migration_from_launch_event,
)
from aats.ingestion.smart_money import RawAccountUpdate
from aats.ingestion.transport import _bytes_to_base58

MINT = _bytes_to_base58(bytes([1] * 32))
CREATOR = _bytes_to_base58(bytes([2] * 32))
OUTSIDER = _bytes_to_base58(bytes([3] * 32))
MIGRATION_SLOT = 1_000_000
BLOCK_TIME_MS = 1_718_700_000_000


def _obs(
    *,
    slot: int,
    balance: int,
    mint: str = MINT,
    wallet: str = CREATOR,
    block_time_ms: int | None = BLOCK_TIME_MS,
    write_version: int = 0,
) -> CreatorOutflowObservation:
    return CreatorOutflowObservation(
        mint=mint,
        creator_wallet=wallet,
        event_slot=slot,
        block_time_ms=block_time_ms,
        current_balance_base=balance,
        write_version=write_version,
    )


def _tracker(migration_slot: int = MIGRATION_SLOT) -> CreatorOutflowVelocityTracker:
    t = CreatorOutflowVelocityTracker()
    t.register_migration(MINT, CREATOR, migration_slot)
    return t


# ---------------------------------------------------------------------------
# 1. Pure velocity math -- "the feature rises with creator outflow"
# ---------------------------------------------------------------------------


class TestComputeOutflowVelocityBps:
    def test_exact_values(self):
        # 250_000 / 1_000_000 = 25% = 2500bps; normalized over 1000 slots at
        # slots_since_migration=1000 -> velocity == fraction exactly.
        fraction, velocity = compute_outflow_velocity_bps(
            cumulative_outflow_base=250_000,
            baseline_post_migration_base=1_000_000,
            slots_since_migration=VELOCITY_NORMALIZATION_SLOTS,
        )
        assert fraction == 2_500
        assert velocity == 2_500

    def test_faster_distribution_yields_higher_velocity_same_fraction(self):
        # Same 25% sold, but in HALF the slots -> velocity should be higher
        # (faster distribution == more dangerous == higher feature value).
        _, slow = compute_outflow_velocity_bps(
            cumulative_outflow_base=250_000,
            baseline_post_migration_base=1_000_000,
            slots_since_migration=VELOCITY_NORMALIZATION_SLOTS,
        )
        _, fast = compute_outflow_velocity_bps(
            cumulative_outflow_base=250_000,
            baseline_post_migration_base=1_000_000,
            slots_since_migration=VELOCITY_NORMALIZATION_SLOTS // 2,
        )
        assert fast > slow

    def test_monotone_non_decreasing_in_cumulative_outflow(self):
        """The feature RISES with creator outflow -- a direct arithmetic proof,
        no model required."""
        prior_velocity = -1
        prior_fraction = -1
        for cumulative in (0, 10_000, 100_000, 250_000, 500_000, 1_000_000, 2_000_000):
            fraction, velocity = compute_outflow_velocity_bps(
                cumulative_outflow_base=cumulative,
                baseline_post_migration_base=1_000_000,
                slots_since_migration=VELOCITY_NORMALIZATION_SLOTS,
            )
            assert fraction >= prior_fraction
            assert velocity >= prior_velocity
            prior_fraction, prior_velocity = fraction, velocity

    def test_clamps_at_bps_denom(self):
        fraction, velocity = compute_outflow_velocity_bps(
            cumulative_outflow_base=50_000_000,  # 50x the baseline
            baseline_post_migration_base=1_000_000,
            slots_since_migration=1,
        )
        assert fraction == BPS_DENOM
        assert velocity == BPS_DENOM

    def test_refuses_non_positive_baseline(self):
        with pytest.raises(ValueError, match="baseline_post_migration_base"):
            compute_outflow_velocity_bps(
                cumulative_outflow_base=100, baseline_post_migration_base=0, slots_since_migration=1
            )

    def test_refuses_non_positive_slots_since_migration(self):
        with pytest.raises(ValueError, match="slots_since_migration"):
            compute_outflow_velocity_bps(
                cumulative_outflow_base=100,
                baseline_post_migration_base=1_000,
                slots_since_migration=0,
            )

    def test_refuses_negative_cumulative(self):
        with pytest.raises(ValueError, match="cumulative_outflow_base"):
            compute_outflow_velocity_bps(
                cumulative_outflow_base=-1,
                baseline_post_migration_base=1_000,
                slots_since_migration=1,
            )


# ---------------------------------------------------------------------------
# 2. CreatorOutflowObservation -- money-rule / type guards
# ---------------------------------------------------------------------------


class TestObservationValidation:
    def test_rejects_float_balance(self):
        with pytest.raises(TypeError):
            CreatorOutflowObservation(
                mint=MINT,
                creator_wallet=CREATOR,
                event_slot=1,
                block_time_ms=1,
                current_balance_base=1.5,  # type: ignore[arg-type]
            )

    def test_rejects_negative_slot(self):
        with pytest.raises(ValueError):
            CreatorOutflowObservation(
                mint=MINT,
                creator_wallet=CREATOR,
                event_slot=-1,
                block_time_ms=1,
                current_balance_base=1,
            )

    def test_allows_none_block_time(self):
        obs = CreatorOutflowObservation(
            mint=MINT, creator_wallet=CREATOR, event_slot=1, block_time_ms=None,
            current_balance_base=1,
        )
        assert obs.block_time_ms is None


# ---------------------------------------------------------------------------
# 3. Refuse-by-default
# ---------------------------------------------------------------------------


class TestRefuseByDefault:
    def test_refuses_unregistered_mint(self):
        tracker = CreatorOutflowVelocityTracker()  # no register_migration call
        feature = tracker.observe_balance(_obs(slot=MIGRATION_SLOT + 1, balance=900_000))
        assert feature is None
        assert tracker.stats.refused_unregistered_mint == 1
        assert tracker.latest_feature(MINT) is None

    def test_exact_migration_slot_is_excluded_not_counted(self):
        """Boundary (mutation-sensitive): a reading AT the migration slot is
        pre-migration by convention (event_slot <= migration_slot) and is
        REFUSED, never treated as the baseline."""
        tracker = _tracker()
        feature = tracker.observe_balance(_obs(slot=MIGRATION_SLOT, balance=1_000_000))
        assert feature is None
        assert tracker.stats.pre_migration_skipped == 1

    def test_migration_slot_plus_one_is_accepted_as_baseline_seed(self):
        tracker = _tracker()
        # First post-migration reading seeds the baseline AND is itself an
        # honest (zero-outflow-so-far) feature reading -- not withheld.
        feature = tracker.observe_balance(_obs(slot=MIGRATION_SLOT + 1, balance=1_000_000))
        assert feature is not None
        assert tracker.stats.pre_migration_skipped == 0
        assert feature.baseline_post_migration_base == 1_000_000
        assert feature.cumulative_outflow_base == 0
        assert feature.outflow_velocity_bps == 0
        # A SUBSEQUENT decrease now measures against that seeded baseline.
        feature2 = tracker.observe_balance(_obs(slot=MIGRATION_SLOT + 2, balance=900_000))
        assert feature2 is not None
        assert feature2.baseline_post_migration_base == 1_000_000
        assert feature2.cumulative_outflow_base == 100_000

    def test_refuses_degenerate_zero_baseline(self):
        tracker = _tracker()
        feature = tracker.observe_balance(_obs(slot=MIGRATION_SLOT + 1, balance=0))
        assert feature is None
        assert tracker.stats.refused_degenerate_baseline == 1
        assert tracker.latest_feature(MINT) is None

    def test_untracked_wallet_is_noop_not_a_refusal(self):
        tracker = _tracker()
        feature = tracker.observe_balance(
            _obs(slot=MIGRATION_SLOT + 1, balance=500, wallet=OUTSIDER)
        )
        assert feature is None
        assert tracker.stats.untracked_wallet_skipped == 1

    def test_unconfirmed_block_time_held_pending(self):
        tracker = _tracker()
        feature = tracker.observe_balance(
            _obs(slot=MIGRATION_SLOT + 1, balance=1_000_000, block_time_ms=None)
        )
        assert feature is None
        assert tracker.stats.pending_unconfirmed_skipped == 1
        # And it must NOT have seeded a baseline from the unconfirmed reading.
        assert tracker.latest_feature(MINT) is None


# ---------------------------------------------------------------------------
# 4. Feature rises with creator outflow (tracker level, end-to-end)
# ---------------------------------------------------------------------------


class TestFeatureRisesWithOutflow:
    def test_velocity_increases_as_creator_sells_more(self):
        tracker = _tracker()
        tracker.observe_balance(_obs(slot=MIGRATION_SLOT + 1, balance=1_000_000))  # seed baseline

        prior_velocity = -1
        for i, balance in enumerate((900_000, 700_000, 400_000, 100_000), start=2):
            feature = tracker.observe_balance(_obs(slot=MIGRATION_SLOT + i, balance=balance))
            assert feature is not None
            assert feature.outflow_velocity_bps >= prior_velocity
            prior_velocity = feature.outflow_velocity_bps
        assert prior_velocity > 0  # genuinely rose, not a vacuous flat pass

    def test_balance_increase_never_decreases_cumulative_outflow(self):
        """A creator BUYING BACK (balance increase) must never reduce the
        already-accrued cumulative outflow (monotone non-decreasing ledger)."""
        tracker = _tracker()
        tracker.observe_balance(_obs(slot=MIGRATION_SLOT + 1, balance=1_000_000))
        f1 = tracker.observe_balance(_obs(slot=MIGRATION_SLOT + 2, balance=800_000))
        assert f1.cumulative_outflow_base == 200_000
        f2 = tracker.observe_balance(_obs(slot=MIGRATION_SLOT + 3, balance=950_000))  # buys back
        assert f2.cumulative_outflow_base == 200_000  # unchanged, never decreases
        f3 = tracker.observe_balance(_obs(slot=MIGRATION_SLOT + 4, balance=850_000))  # sells again
        assert f3.cumulative_outflow_base == 300_000  # 200_000 + (950_000-850_000)

    def test_no_outflow_yields_zero_velocity(self):
        tracker = _tracker()
        tracker.observe_balance(_obs(slot=MIGRATION_SLOT + 1, balance=1_000_000))
        feature = tracker.observe_balance(_obs(slot=MIGRATION_SLOT + 2, balance=1_000_000))
        assert feature is not None
        assert feature.cumulative_outflow_base == 0
        assert feature.outflow_velocity_bps == 0
        assert feature.fraction_sold_bps == 0


# ---------------------------------------------------------------------------
# 5. Event-time honesty (T-300a)
# ---------------------------------------------------------------------------


class TestEventTimeHonesty:
    def test_feature_stamped_by_slot_and_block_time_from_inputs_only(self):
        tracker = _tracker()
        tracker.observe_balance(_obs(slot=MIGRATION_SLOT + 1, balance=1_000_000))
        feature = tracker.observe_balance(
            _obs(slot=MIGRATION_SLOT + 5, balance=900_000, block_time_ms=BLOCK_TIME_MS + 5_000)
        )
        assert feature.event_slot == MIGRATION_SLOT + 5
        assert feature.block_time_ms == BLOCK_TIME_MS + 5_000
        assert feature.migration_slot == MIGRATION_SLOT
        assert feature.slots_since_migration == 5

    def test_computed_wall_ms_is_not_used_in_velocity_math(self):
        """computed_wall_ms is monitoring-only -- perturbing wall-clock (by
        constructing two features far apart in real time) must never change
        the velocity math, only the identically-computed on-chain fields."""
        tracker = _tracker()
        tracker.observe_balance(_obs(slot=MIGRATION_SLOT + 1, balance=1_000_000))
        f1 = tracker.observe_balance(_obs(slot=MIGRATION_SLOT + 2, balance=900_000))
        # A second, independent tracker replaying the identical on-chain facts
        # must compute the IDENTICAL velocity regardless of wall-clock at test
        # run time.
        tracker2 = _tracker()
        tracker2.observe_balance(_obs(slot=MIGRATION_SLOT + 1, balance=1_000_000))
        f2 = tracker2.observe_balance(_obs(slot=MIGRATION_SLOT + 2, balance=900_000))
        assert f1.outflow_velocity_bps == f2.outflow_velocity_bps
        assert f1.fraction_sold_bps == f2.fraction_sold_bps
        assert f1.cumulative_outflow_base == f2.cumulative_outflow_base


# ---------------------------------------------------------------------------
# 6. Idempotent / order-tolerant
# ---------------------------------------------------------------------------


class TestIdempotentOrderTolerant:
    def test_duplicate_observation_is_noop(self):
        tracker = _tracker()
        tracker.observe_balance(_obs(slot=MIGRATION_SLOT + 1, balance=1_000_000))
        f1 = tracker.observe_balance(_obs(slot=MIGRATION_SLOT + 2, balance=800_000))
        f2 = tracker.observe_balance(_obs(slot=MIGRATION_SLOT + 2, balance=800_000))  # replay
        assert f2 is None
        assert tracker.stats.duplicates_skipped == 1
        assert tracker.latest_feature(MINT) == f1

    def _final_state(self, order: list[tuple[int, int]]) -> CreatorOutflowVelocityFeature:
        tracker = _tracker()
        for slot, balance in order:
            tracker.observe_balance(_obs(slot=slot, balance=balance))
        return tracker.latest_feature(MINT)

    def test_out_of_order_replay_converges_to_identical_state(self):
        in_order = [
            (MIGRATION_SLOT + 1, 1_000_000),
            (MIGRATION_SLOT + 2, 900_000),
            (MIGRATION_SLOT + 3, 700_000),
            (MIGRATION_SLOT + 4, 650_000),
        ]
        scrambled = [in_order[2], in_order[0], in_order[3], in_order[1]]
        # duplicated too: replay slot 2 a second time out of order
        duplicated_and_scrambled = scrambled + [in_order[1]]

        f_in_order = self._final_state(in_order)
        f_scrambled = self._final_state(scrambled)
        f_dup = self._final_state(duplicated_and_scrambled)

        for other in (f_scrambled, f_dup):
            assert other.cumulative_outflow_base == f_in_order.cumulative_outflow_base
            assert other.baseline_post_migration_base == f_in_order.baseline_post_migration_base
            assert other.outflow_velocity_bps == f_in_order.outflow_velocity_bps
            assert other.fraction_sold_bps == f_in_order.fraction_sold_bps
            assert other.event_slot == f_in_order.event_slot
            assert other.slots_since_migration == f_in_order.slots_since_migration


# ---------------------------------------------------------------------------
# 6b. CP07-B1 regression: a late-arriving degenerate-baseline reading must
#     invalidate (not merely refuse to refresh) an already-served latest_feature.
# ---------------------------------------------------------------------------


class TestLateDegenerateReadingInvalidatesLatestFeature:
    def test_late_earlier_zero_reading_invalidates_latest_feature(self):
        tracker = _tracker()
        # Valid post-migration readings -- a real feature IS served.
        tracker.observe_balance(_obs(slot=MIGRATION_SLOT + 5, balance=1_000_000))
        f = tracker.observe_balance(_obs(slot=MIGRATION_SLOT + 6, balance=900_000))
        assert f is not None
        assert tracker.latest_feature(MINT) is not None

        # A late-arriving EARLIER reading (e.g. the ATA-creation write, which
        # the scrambled-replay contract explicitly supports) shows the TRUE
        # first post-migration balance was 0 -- the whole observation set is
        # now degenerate.  The caller-facing read surface must REFUSE, never
        # keep serving the superseded snapshot.
        result = tracker.observe_balance(_obs(slot=MIGRATION_SLOT + 1, balance=0))
        assert result is None
        assert tracker.latest_feature(MINT) is None

        # And it must STAY refused on every subsequent observation of this
        # same (now degenerate) set -- never resurrect the stale snapshot.
        again = tracker.observe_balance(_obs(slot=MIGRATION_SLOT + 7, balance=850_000))
        assert again is None
        assert tracker.latest_feature(MINT) is None

    def test_refused_degenerate_baseline_counted_once_not_per_recompute(self):
        """The refusal counter must reflect the TRANSITION into the degenerate
        state, not be re-inflated on every subsequent (still-degenerate)
        observation."""
        tracker = _tracker()
        tracker.observe_balance(_obs(slot=MIGRATION_SLOT + 1, balance=0))
        assert tracker.stats.refused_degenerate_baseline == 1
        tracker.observe_balance(_obs(slot=MIGRATION_SLOT + 2, balance=500))
        tracker.observe_balance(_obs(slot=MIGRATION_SLOT + 3, balance=400))
        assert tracker.stats.refused_degenerate_baseline == 1

    def test_in_order_vs_scrambled_delivery_converge_to_identical_none_state(self):
        def _final_state(order: list[tuple[int, int]]) -> CreatorOutflowVelocityFeature | None:
            tracker = _tracker()
            for slot, balance in order:
                tracker.observe_balance(_obs(slot=slot, balance=balance))
            return tracker.latest_feature(MINT)

        in_order = [
            (MIGRATION_SLOT + 1, 0),  # true (earliest) baseline reading: degenerate
            (MIGRATION_SLOT + 5, 1_000_000),
            (MIGRATION_SLOT + 6, 900_000),
        ]
        # Scrambled: the valid-looking readings arrive FIRST (serving a real
        # feature transiently), the degenerate baseline arrives LAST.
        scrambled = [in_order[1], in_order[2], in_order[0]]

        assert _final_state(in_order) is None
        assert _final_state(scrambled) is None


# ---------------------------------------------------------------------------
# 6c. CP07-B2 regression: same-slot DISTINCT writes are ordered by
#     write_version, never conflated as a duplicate or arrival-order-dependent.
# ---------------------------------------------------------------------------


class TestSameSlotWriteVersionArbitration:
    def test_distinct_same_slot_writes_converge_regardless_of_arrival_order(self):
        # Forward order: lower write_version (earlier write) arrives first,
        # higher write_version (the slot's true closing balance) arrives second.
        tracker_a = _tracker()
        tracker_a.observe_balance(_obs(slot=MIGRATION_SLOT + 1, balance=1_000_000))
        f1 = tracker_a.observe_balance(
            _obs(slot=MIGRATION_SLOT + 2, balance=800_000, write_version=1)
        )
        f2 = tracker_a.observe_balance(
            _obs(slot=MIGRATION_SLOT + 2, balance=600_000, write_version=2)
        )
        assert f1 is not None
        assert f2 is not None
        assert f2.cumulative_outflow_base == 400_000  # 1_000_000 - 600_000 (closing balance)
        assert tracker_a.stats.same_slot_superseded == 0
        assert tracker_a.stats.duplicates_skipped == 0

        # Reverse order: the higher write_version (closing balance) arrives
        # FIRST; the lower write_version arrives SECOND and must be treated as
        # a genuine-but-superseded reading, never a duplicate, and must never
        # corrupt the slot's closing balance.
        tracker_b = _tracker()
        tracker_b.observe_balance(_obs(slot=MIGRATION_SLOT + 1, balance=1_000_000))
        g1 = tracker_b.observe_balance(
            _obs(slot=MIGRATION_SLOT + 2, balance=600_000, write_version=2)
        )
        g2 = tracker_b.observe_balance(
            _obs(slot=MIGRATION_SLOT + 2, balance=800_000, write_version=1)
        )
        assert g1 is not None
        assert g2 is None
        assert tracker_b.stats.same_slot_superseded == 1
        assert tracker_b.stats.duplicates_skipped == 0

        final_a = tracker_a.latest_feature(MINT)
        final_b = tracker_b.latest_feature(MINT)
        assert final_a is not None and final_b is not None
        assert final_a.cumulative_outflow_base == final_b.cumulative_outflow_base
        assert final_a.baseline_post_migration_base == final_b.baseline_post_migration_base
        assert final_a.outflow_velocity_bps == final_b.outflow_velocity_bps
        assert final_a.fraction_sold_bps == final_b.fraction_sold_bps

    def test_true_duplicate_same_write_version_still_deduped(self):
        tracker = _tracker()
        tracker.observe_balance(_obs(slot=MIGRATION_SLOT + 1, balance=1_000_000))
        f1 = tracker.observe_balance(
            _obs(slot=MIGRATION_SLOT + 2, balance=800_000, write_version=1)
        )
        f2 = tracker.observe_balance(
            _obs(slot=MIGRATION_SLOT + 2, balance=800_000, write_version=1)  # exact replay
        )
        assert f1 is not None
        assert f2 is None
        assert tracker.stats.duplicates_skipped == 1
        assert tracker.stats.same_slot_superseded == 0


# ---------------------------------------------------------------------------
# 7. reset_mint
# ---------------------------------------------------------------------------


class TestResetMint:
    def test_reset_clears_all_state(self):
        tracker = _tracker()
        tracker.observe_balance(_obs(slot=MIGRATION_SLOT + 1, balance=1_000_000))
        tracker.observe_balance(_obs(slot=MIGRATION_SLOT + 2, balance=500_000))
        assert tracker.latest_feature(MINT) is not None
        tracker.reset_mint(MINT)
        assert tracker.latest_feature(MINT) is None
        # Re-registering + re-seeding must start a FRESH baseline, unaffected
        # by the previous trade's cumulative.
        tracker.register_migration(MINT, CREATOR, MIGRATION_SLOT + 100)
        feature = tracker.observe_balance(_obs(slot=MIGRATION_SLOT + 101, balance=2_000_000))
        assert feature is not None  # baseline seed -- honest zero-outflow reading
        assert feature.cumulative_outflow_base == 0
        feature2 = tracker.observe_balance(_obs(slot=MIGRATION_SLOT + 102, balance=1_900_000))
        assert feature2.cumulative_outflow_base == 100_000
        assert feature2.baseline_post_migration_base == 2_000_000


# ---------------------------------------------------------------------------
# 8. MigrationEvent adapter
# ---------------------------------------------------------------------------


def _migration_event(mint: str = MINT, creator: str = CREATOR, slot: int = MIGRATION_SLOT) -> LaunchEvent:
    return LaunchEvent(
        mint=mint,
        venue_program_id="6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",
        source=LaunchSource.MIGRATION,
        event_time=EventTime(slot=slot, block_time_ms=BLOCK_TIME_MS, wall_clock_ms=BLOCK_TIME_MS),
        observation_slot=slot,
        confirmation_slot=slot,
        detection_transport=DetectionTransport.GEYSER,
        sol_reserve_lamports=0,
        token_reserve_base=0,
        token_decimals=6,
        initial_holders=0,
        competitors=0,
        creator_wallet=creator,
        bundler_cluster_id=None,
        deploy_template_fingerprint=None,
        data_staleness_ms=0,
    )


class TestMigrationAdapter:
    def test_registers_from_migration_event(self):
        tracker = CreatorOutflowVelocityTracker()
        ok = register_migration_from_launch_event(tracker, _migration_event())
        assert ok is True
        feature = tracker.observe_balance(_obs(slot=MIGRATION_SLOT + 1, balance=1_000_000))
        assert feature is not None  # baseline seed -- honest zero-outflow reading
        feature2 = tracker.observe_balance(_obs(slot=MIGRATION_SLOT + 2, balance=900_000))
        assert feature2 is not None

    def test_ignores_non_migration_event(self):
        from aats.contracts.events import LaunchSource as _LS

        tracker = CreatorOutflowVelocityTracker()
        ev = _migration_event()
        non_migration = ev.model_copy(update={"source": _LS.PUMPFUN})
        ok = register_migration_from_launch_event(tracker, non_migration)
        assert ok is False
        # unregistered -> the tracker still refuses any observation for MINT
        feature = tracker.observe_balance(_obs(slot=MIGRATION_SLOT + 1, balance=1_000_000))
        assert feature is None
        assert tracker.stats.refused_unregistered_mint == 1


# ---------------------------------------------------------------------------
# 9. RawAccountUpdate adapter (reuses smart_money.py's canonical SPL decode)
# ---------------------------------------------------------------------------


def _token_account_data(mint_b: bytes, owner_b: bytes, amount: int) -> bytes:
    body = mint_b + owner_b + amount.to_bytes(8, "little")
    return body + b"\x00" * (165 - len(body))


class TestAccountUpdateAdapter:
    def test_adapts_decoded_token_account_write(self):
        mint_b = bytes([1] * 32)
        owner_b = bytes([2] * 32)
        update = RawAccountUpdate(
            pubkey="TokenAccountPubkey1111111111111111111111111",
            owner_program="TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
            slot=MIGRATION_SLOT + 1,
            lamports=1,
            data=_token_account_data(mint_b, owner_b, 750_000),
            write_version=1,
            is_startup=False,
        )
        obs = creator_outflow_observation_from_account_update(update, block_time_ms=BLOCK_TIME_MS)
        assert obs is not None
        assert obs.mint == _bytes_to_base58(mint_b)
        assert obs.creator_wallet == _bytes_to_base58(owner_b)
        assert obs.event_slot == MIGRATION_SLOT + 1
        assert obs.current_balance_base == 750_000
        assert obs.block_time_ms == BLOCK_TIME_MS
        assert obs.write_version == 1  # carried through from RawAccountUpdate, never dropped

    def test_returns_none_for_startup_snapshot(self):
        update = RawAccountUpdate(
            pubkey="x", owner_program="y", slot=1, lamports=1, data=b"", write_version=1,
            is_startup=True,
        )
        assert creator_outflow_observation_from_account_update(update, block_time_ms=1) is None

    def test_refuses_by_returning_none_for_undecodable_data(self):
        """REFUSE-BY-DEFAULT on undecoded outflow: a too-short / non-SPL-Token
        account write must NEVER be coerced into a (fabricated) observation."""
        update = RawAccountUpdate(
            pubkey="x", owner_program="y", slot=1, lamports=1, data=b"\x00" * 10,
            write_version=1, is_startup=False,
        )
        assert creator_outflow_observation_from_account_update(update, block_time_ms=1) is None

    def test_block_time_none_is_passed_through_never_substituted(self):
        mint_b = bytes([1] * 32)
        owner_b = bytes([2] * 32)
        update = RawAccountUpdate(
            pubkey="x", owner_program="TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
            slot=1, lamports=1, data=_token_account_data(mint_b, owner_b, 1), write_version=1,
            is_startup=False,
        )
        obs = creator_outflow_observation_from_account_update(update, block_time_ms=None)
        assert obs is not None
        assert obs.block_time_ms is None


# ---------------------------------------------------------------------------
# 10. CreatorOutflowVelocityFeature invariants
# ---------------------------------------------------------------------------


class TestFeatureInvariants:
    def test_rejects_inconsistent_slots_since_migration(self):
        with pytest.raises(ValueError):
            CreatorOutflowVelocityFeature(
                mint=MINT,
                creator_wallet=CREATOR,
                event_slot=100,
                block_time_ms=1,
                migration_slot=50,
                slots_since_migration=10,  # should be 50, not 10
                cumulative_outflow_base=0,
                baseline_post_migration_base=1,
                fraction_sold_bps=0,
                outflow_velocity_bps=0,
            )

    def test_rejects_out_of_range_bps(self):
        with pytest.raises(ValueError):
            CreatorOutflowVelocityFeature(
                mint=MINT,
                creator_wallet=CREATOR,
                event_slot=60,
                block_time_ms=1,
                migration_slot=50,
                slots_since_migration=10,
                cumulative_outflow_base=0,
                baseline_post_migration_base=1,
                fraction_sold_bps=10_001,
                outflow_velocity_bps=0,
            )


# ---------------------------------------------------------------------------
# 11. Bus publisher wire format
# ---------------------------------------------------------------------------


class TestBusPublisher:
    def test_publish_xadds_expected_payload(self):
        async def _run() -> None:
            redis = AsyncMock()
            redis.xadd = AsyncMock(return_value="1-0")
            publisher = CreatorOutflowBusPublisher(redis)
            feature = CreatorOutflowVelocityFeature(
                mint=MINT,
                creator_wallet=CREATOR,
                event_slot=MIGRATION_SLOT + 5,
                block_time_ms=BLOCK_TIME_MS,
                migration_slot=MIGRATION_SLOT,
                slots_since_migration=5,
                cumulative_outflow_base=100_000,
                baseline_post_migration_base=1_000_000,
                fraction_sold_bps=1_000,
                outflow_velocity_bps=1_000,
            )
            entry_id = await publisher.publish(feature)
            assert entry_id == "1-0"
            redis.xadd.assert_awaited_once()
            args, kwargs = redis.xadd.call_args
            assert args[0] == STREAM_CREATOR_OUTFLOW
            payload = args[1]
            assert payload["mint"] == MINT
            assert payload["outflow_velocity_bps"] == "1000"
            assert kwargs["maxlen"] == MAXLEN_CREATOR_OUTFLOW
            assert kwargs["approximate"] is True

        asyncio.run(_run())
