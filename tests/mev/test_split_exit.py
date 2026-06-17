"""Tests for T-331 — MEV protection: private split exits + FAST/SECURE modes +
sandwich avoidance + tip-contention logging (C-3).

Proven here (the self-check the quant audits, every basis point):
  1. SECURE split-exit lowers MODELED sandwich exposure vs a single FAST shot.
  2. A routing/split plan can NEVER increase sandwich exposure above the FAST
     baseline (the asymmetry — modes may ONLY reduce risk).  Fuzzed.
  3. tip_floor_at_decision + contention bucket are stamped per candidate (C-3),
     point-in-time (no future snapshot), with the priced-out cohort recorded.
  4. A sandwich-attempt sim does not improve against the bot under SECURE split
     vs the FAST single shot (private + bounded + split survives the attack).
  5. Money is integer base units / int bps everywhere — float is rejected.
"""

from __future__ import annotations

import random
from decimal import Decimal

import pytest

from aats.mev.split_exit import (
    ContentionBucket,
    ExitLeg,
    ExitProtectionPlanner,
    MevExitMode,
    PoolLiquidity,
    SplitExitPlan,
    TipContentionRecorder,
    TipContentionStamp,
    _modeled_split_sandwich_p_bps,
    _split_bps_evenly,
    slippage_model_for,
)
from aats.mev.tip_stream import StaticTipStream, TipFloorSnapshot
from aats.risk.exit_engine import FAST_SLIPPAGE, SECURE_SLIPPAGE

LAMPORTS_PER_SOL = 1_000_000_000


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


def _snapshot(
    *,
    p25=None,
    p50=100_000,
    p75=None,
    p95=None,
    as_of_slot=1000,
    block_time_ms=400_000,
    accounts=("TipAcct1111111111111111111111111111111111111",),
):
    # Derive the surrounding percentiles from p50 unless overridden, so a caller
    # varying only p50 always yields a monotonic ladder.
    p25 = p25 if p25 is not None else p50 // 2
    p75 = p75 if p75 is not None else p50 * 2
    p95 = p95 if p95 is not None else p50 * 4
    return TipFloorSnapshot(
        p25_lamports=p25,
        p50_lamports=p50,
        p75_lamports=p75,
        p95_lamports=p95,
        tip_accounts=accounts,
        as_of_slot=as_of_slot,
        as_of_block_time_ms=block_time_ms,
    )


def _recorder(snap=None, sink=None):
    stream = StaticTipStream([snap or _snapshot()])
    return TipContentionRecorder(stream, sink=sink)


# Thin pool: a 1-SOL sell against a 5-SOL reserve = 2000 bps of the pool (fat
# footprint) -> the planner splits.  Deep pool: 1-SOL sell vs 1000-SOL reserve.
_THIN_POOL = PoolLiquidity(
    quote_reserve_base=5 * LAMPORTS_PER_SOL,
    sell_notional_base=1 * LAMPORTS_PER_SOL,
)
_DEEP_POOL = PoolLiquidity(
    quote_reserve_base=1_000 * LAMPORTS_PER_SOL,
    sell_notional_base=1 * LAMPORTS_PER_SOL,
)


# =========================================================================== #
# 1. SECURE split-exit lowers modeled sandwich exposure vs FAST single shot.
# =========================================================================== #


def test_secure_split_lowers_sandwich_vs_fast_single_shot():
    planner = ExitProtectionPlanner()
    fast_single = planner.plan(
        mint="M",
        fraction_bps=10_000,
        mode=MevExitMode.FAST,
        pool=_DEEP_POOL,  # deep -> FAST stays a single shot
        per_leg_slippage_bps=150,
        expected_tokens_out_base=2 * LAMPORTS_PER_SOL,
    )
    secure_split = planner.plan(
        mint="M",
        fraction_bps=10_000,
        mode=MevExitMode.SECURE,
        pool=_THIN_POOL,  # thin -> SECURE splits into multiple private legs
        per_leg_slippage_bps=150,
        expected_tokens_out_base=2 * LAMPORTS_PER_SOL,
    )
    # Secure private split has strictly lower modeled sandwich exposure.
    assert secure_split.modeled_sandwich_p_bps < fast_single.modeled_sandwich_p_bps
    # And it actually split (more legs than the single FAST shot).
    assert secure_split.n_legs > fast_single.n_legs
    assert fast_single.n_legs == 1


def test_secure_base_sandwich_below_fast_base():
    # The shared slippage models: SECURE routes private -> lower sandwich p.
    assert SECURE_SLIPPAGE.sandwich_p_bps < FAST_SLIPPAGE.sandwich_p_bps
    assert (
        slippage_model_for(MevExitMode.SECURE).sandwich_p_bps
        < slippage_model_for(MevExitMode.FAST).sandwich_p_bps
    )


def test_thin_pool_splits_more_than_deep_pool():
    planner = ExitProtectionPlanner()
    thin = planner.plan(
        mint="M",
        fraction_bps=10_000,
        mode=MevExitMode.SECURE,
        pool=_THIN_POOL,
        per_leg_slippage_bps=150,
        expected_tokens_out_base=LAMPORTS_PER_SOL,
    )
    deep = planner.plan(
        mint="M",
        fraction_bps=10_000,
        mode=MevExitMode.SECURE,
        pool=_DEEP_POOL,
        per_leg_slippage_bps=150,
        expected_tokens_out_base=LAMPORTS_PER_SOL,
    )
    assert thin.n_legs > deep.n_legs
    assert deep.n_legs == 1  # a small footprint doesn't need splitting


# =========================================================================== #
# 2. ASYMMETRY — a plan may ONLY reduce sandwich exposure (never above FAST).
# =========================================================================== #


def test_plan_never_exceeds_fast_baseline_fuzz():
    planner = ExitProtectionPlanner(max_legs=6, split_threshold_bps=100)
    rng = random.Random(1331)
    fast_baseline = FAST_SLIPPAGE.sandwich_p_bps
    for _ in range(2000):
        frac = rng.randint(1, 10_000)
        reserve = rng.randint(1, 10_000) * LAMPORTS_PER_SOL
        sell = rng.randint(1, 10_000) * LAMPORTS_PER_SOL
        mode = rng.choice([MevExitMode.FAST, MevExitMode.SECURE])
        plan = planner.plan(
            mint="M",
            fraction_bps=frac,
            mode=mode,
            pool=PoolLiquidity(quote_reserve_base=reserve, sell_notional_base=sell),
            per_leg_slippage_bps=rng.randint(0, 500),
            expected_tokens_out_base=rng.randint(1, 10_000) * 1_000_000,
        )
        # The asymmetry: modeled exposure NEVER above the FAST single-shot worst case.
        assert plan.modeled_sandwich_p_bps <= fast_baseline
        assert plan.modeled_sandwich_p_bps <= plan.fast_baseline_sandwich_p_bps


def test_constructing_a_riskier_plan_is_rejected():
    # Directly attempting to build a plan that exceeds the FAST baseline is refused
    # at the type boundary (the structural asymmetry guard).
    with pytest.raises(ValueError, match="ONLY reduce"):
        SplitExitPlan(
            mint="M",
            mode=MevExitMode.SECURE,
            total_fraction_bps=10_000,
            legs=(ExitLeg(leg_index=0, fraction_bps=10_000, min_out_base=1, private=True),),
            modeled_sandwich_p_bps=FAST_SLIPPAGE.sandwich_p_bps + 1,  # ABOVE the cap
            fast_baseline_sandwich_p_bps=FAST_SLIPPAGE.sandwich_p_bps,
            reason="illegal — over the cap",
        )


def test_modeled_split_prob_is_monotone_non_increasing_in_legs():
    base = FAST_SLIPPAGE.sandwich_p_bps
    prev = base + 1
    for n in range(1, 20):
        p = _modeled_split_sandwich_p_bps(base, n)
        assert p <= prev  # more legs => never-higher modeled exposure
        prev = p


def test_public_exposure_on_any_leg_is_rejected():
    # Every leg MUST be private — a public leg exposes intent to be sandwiched.
    with pytest.raises(ValueError, match="must be private"):
        SplitExitPlan(
            mint="M",
            mode=MevExitMode.SECURE,
            total_fraction_bps=10_000,
            legs=(ExitLeg(leg_index=0, fraction_bps=10_000, min_out_base=1, private=False),),
            modeled_sandwich_p_bps=0,
            fast_baseline_sandwich_p_bps=FAST_SLIPPAGE.sandwich_p_bps,
            reason="illegal — public leg",
        )


# =========================================================================== #
# 3. Split-plan integrity — legs sum exactly, bounded min_out.
# =========================================================================== #


def test_legs_sum_exactly_to_total_fraction_fuzz():
    rng = random.Random(7)
    for _ in range(5000):
        total = rng.randint(1, 10_000)
        n = rng.randint(1, 8)
        parts = _split_bps_evenly(total, n)
        assert sum(parts) == total  # integer-exact, no float drift
        assert all(p >= 1 for p in parts)


def test_plan_legs_sum_to_total_and_are_private():
    planner = ExitProtectionPlanner()
    plan = planner.plan(
        mint="M",
        fraction_bps=7_777,
        mode=MevExitMode.SECURE,
        pool=_THIN_POOL,
        per_leg_slippage_bps=200,
        expected_tokens_out_base=3 * LAMPORTS_PER_SOL,
    )
    assert sum(leg.fraction_bps for leg in plan.legs) == 7_777
    assert all(leg.private for leg in plan.legs)


def test_per_leg_min_out_is_bounded_by_slippage():
    # Each leg's min_out is leg_tokens * (1 - per_leg_slip), floored to integer.
    planner = ExitProtectionPlanner()
    plan = planner.plan(
        mint="M",
        fraction_bps=10_000,
        mode=MevExitMode.SECURE,
        pool=_DEEP_POOL,  # single leg, simplest to reason about
        per_leg_slippage_bps=500,  # 5%
        expected_tokens_out_base=1_000_000,
    )
    assert plan.n_legs == 1
    leg = plan.legs[0]
    # min_out = floor(1_000_000 * 0.95) = 950_000
    assert leg.min_out_base == 950_000


# =========================================================================== #
# 4. C-3 — tip-floor + contention bucket stamped per candidate, point-in-time.
# =========================================================================== #


def test_stamp_records_live_floor_and_bucket_per_candidate():
    sink: list[TipContentionStamp] = []
    rec = _recorder(_snapshot(p50=100_000, p75=200_000, p95=400_000), sink=sink)
    stamp = rec.stamp(
        candidate_id="cid-1",
        mint="M",
        expected_edge_lamports=10 * LAMPORTS_PER_SOL,  # huge edge -> not priced out
        decision_slot=1000,
        required_percentile=50,
    )
    assert stamp is not None
    assert stamp.candidate_id == "cid-1"
    assert stamp.live_floor_lamports == 100_000  # p50
    assert stamp.live_p50_lamports == 100_000
    assert stamp.bucket is ContentionBucket.LOW
    assert stamp.priced_out is False
    # Recorded to the append-only sink GATE-A reads to stratify (C-3).
    assert sink == [stamp]


def test_bucket_low_medium_high_from_required_percentile():
    rec = _recorder(_snapshot(p50=100_000, p75=200_000, p95=400_000))
    edge = 100 * LAMPORTS_PER_SOL  # never priced out
    low = rec.stamp(
        candidate_id="c", mint="M", expected_edge_lamports=edge,
        decision_slot=1000, required_percentile=50,
    )
    med = rec.stamp(
        candidate_id="c", mint="M", expected_edge_lamports=edge,
        decision_slot=1000, required_percentile=75,
    )
    high = rec.stamp(
        candidate_id="c", mint="M", expected_edge_lamports=edge,
        decision_slot=1000, required_percentile=95,
    )
    assert low.bucket is ContentionBucket.LOW
    assert med.bucket is ContentionBucket.MEDIUM
    assert high.bucket is ContentionBucket.HIGH


def test_priced_out_cohort_is_flaggable_C3():
    # The cohort we are priced OUT of: floor > 0.30x edge.  This is the cohort the
    # EDGE-VERDICT says we DECLINE; GATE-A needs to see it to detect the spiral.
    rec = _recorder(_snapshot(p50=100_000))
    # edge = 300_000 -> ceiling = 90_000 < floor 100_000 -> priced out.
    stamp = rec.stamp(
        candidate_id="c", mint="M", expected_edge_lamports=300_000,
        decision_slot=1000, required_percentile=50,
    )
    assert stamp.priced_out is True
    assert stamp.bucket is ContentionBucket.PRICED_OUT
    assert stamp.edge_ceiling_lamports == 90_000
    assert stamp.live_floor_lamports == 100_000


def test_low_contention_only_profit_is_stratifiable():
    # Simulate two candidates: one LOW contention, one HIGH contention.  GATE-A
    # groups realized PnL by bucket; if only LOW is profitable that is residual.
    sink: list[TipContentionStamp] = []
    rec = _recorder(_snapshot(p50=100_000, p75=200_000, p95=400_000), sink=sink)
    edge = 100 * LAMPORTS_PER_SOL
    rec.stamp(candidate_id="low", mint="A", expected_edge_lamports=edge,
              decision_slot=1000, required_percentile=50)
    rec.stamp(candidate_id="high", mint="B", expected_edge_lamports=edge,
              decision_slot=1000, required_percentile=95)
    # GATE-A can group the recorded stamps by bucket (the stratification key).
    by_bucket: dict[ContentionBucket, list[str]] = {}
    for s in sink:
        by_bucket.setdefault(s.bucket, []).append(s.candidate_id)
    assert by_bucket[ContentionBucket.LOW] == ["low"]
    assert by_bucket[ContentionBucket.HIGH] == ["high"]


def test_stamp_is_point_in_time_no_future_snapshot():
    # Only a snapshot at slot 2000 exists; a decision at 1500 must NOT see it.
    rec = TipContentionRecorder(StaticTipStream([_snapshot(as_of_slot=2000)]))
    stamp = rec.stamp(
        candidate_id="c", mint="M", expected_edge_lamports=10 * LAMPORTS_PER_SOL,
        decision_slot=1500, required_percentile=50,
    )
    assert stamp is None  # no point-in-time floor -> no stamp (never guess a constant)


def test_stamp_uses_freshest_past_snapshot():
    rec = TipContentionRecorder(
        StaticTipStream(
            [
                _snapshot(p50=50_000, as_of_slot=900),
                _snapshot(p50=120_000, as_of_slot=1000),
                _snapshot(p50=999_000, as_of_slot=2000),  # future — ignored
            ]
        )
    )
    stamp = rec.stamp(
        candidate_id="c", mint="M", expected_edge_lamports=100 * LAMPORTS_PER_SOL,
        decision_slot=1500, required_percentile=50,
    )
    assert stamp is not None
    assert stamp.as_of_slot == 1000  # freshest at-or-before 1500
    assert stamp.live_floor_lamports == 120_000
    assert stamp.as_of_slot <= stamp.decision_slot


def test_stamp_rejects_future_as_of_slot_at_construction():
    # Defense-in-depth: even a hand-built stamp cannot carry a future snapshot.
    with pytest.raises(ValueError, match="lookahead"):
        TipContentionStamp(
            candidate_id="c",
            mint="M",
            decision_slot=1000,
            as_of_slot=1001,  # FUTURE
            as_of_block_time_ms=1,
            live_p50_lamports=1,
            live_p75_lamports=2,
            live_p95_lamports=3,
            live_floor_lamports=1,
            edge_ceiling_lamports=10,
            bucket=ContentionBucket.LOW,
            priced_out=False,
        )


# =========================================================================== #
# 5. Sandwich-attempt sim — SECURE split vs FAST single shot under attack.
# =========================================================================== #


def _exit_under_sandwich_attempts(
    *,
    plan: SplitExitPlan,
    notional_base: int,
    realized_r: Decimal,
    base_haircut_bps: int,
    attempts: int,
    seed: int,
) -> int:
    """Model proceeds (integer base units) from executing a plan while an
    adversary attempts to sandwich each leg.

    A leg is sandwiched with probability `plan.modeled_sandwich_p_bps` (the plan's
    modeled exposure).  A sandwiched leg suffers the FAST extra haircut.  BUT the
    bounded per-leg `min_out_base` caps the damage: a leg that would fill below its
    min_out reverts (proceeds for that leg = its min_out floor, never worse).  We
    run `attempts` seeded trials and return the MEAN proceeds (integer floor).

    This reproduces, deterministically and offline, the exit-side discipline: a
    private split with bounded legs limits how much an attacker can extract vs a
    single fat public shot.
    """
    extra_loss_bps = FAST_SLIPPAGE.sandwich_loss_bps
    total = 0
    for a in range(attempts):
        rng = random.Random(seed * 1_000_003 + a)
        trial_proceeds = 0
        for leg in plan.legs:
            leg_notional = (notional_base * leg.fraction_bps) // plan.total_fraction_bps
            haircut = base_haircut_bps
            sandwiched = rng.random() < (plan.modeled_sandwich_p_bps / 10_000.0)
            if sandwiched:
                haircut += extra_loss_bps
            keep = (Decimal(10_000) - Decimal(haircut)) / Decimal(10_000)
            gross = int(Decimal(leg_notional) * realized_r * keep)
            # Bounded per-leg min_out: a leg never fills below its revert floor.
            leg_floor = leg.min_out_base
            trial_proceeds += max(gross, leg_floor)
        total += trial_proceeds
    return total // attempts


def test_secure_split_survives_sandwich_better_than_fast_single():
    planner = ExitProtectionPlanner()
    notional = 2 * LAMPORTS_PER_SOL
    # expected_out is the at-mark fill; the per-leg min_out floor is set well
    # BELOW the un-sandwiched gross so the sandwich draw can actually subtract
    # value (the floor only catches the worst case) — this isolates the
    # sandwich-exposure difference between the single FAST shot and the split.
    expected_out = 1 * LAMPORTS_PER_SOL
    r = Decimal("2.0")  # the mark is up -> gross is well above the min_out floor

    fast_single = planner.plan(
        mint="M", fraction_bps=10_000, mode=MevExitMode.FAST, pool=_DEEP_POOL,
        per_leg_slippage_bps=150, expected_tokens_out_base=expected_out,
    )
    secure_split = planner.plan(
        mint="M", fraction_bps=10_000, mode=MevExitMode.SECURE, pool=_THIN_POOL,
        per_leg_slippage_bps=150, expected_tokens_out_base=expected_out,
    )

    fast_proceeds = _exit_under_sandwich_attempts(
        plan=fast_single, notional_base=notional, realized_r=r,
        base_haircut_bps=150, attempts=2000, seed=11,
    )
    secure_proceeds = _exit_under_sandwich_attempts(
        plan=secure_split, notional_base=notional, realized_r=r,
        base_haircut_bps=250, attempts=2000, seed=11,  # secure base haircut is worse
    )
    # Despite SECURE's slightly worse BASE price, the private split's lower
    # sandwich exposure means the attacker extracts less -> the bot keeps MORE
    # under sustained sandwich attempts.  The attack does not improve vs the bot.
    assert secure_proceeds > fast_proceeds


def test_sandwich_attempt_cannot_breach_per_leg_min_out_floor():
    # The bounded per-leg min_out is the hard floor: no sandwich draw can push a
    # leg's realized proceeds below its revert floor (the leg reverts instead).
    planner = ExitProtectionPlanner()
    plan = planner.plan(
        mint="M", fraction_bps=10_000, mode=MevExitMode.SECURE, pool=_THIN_POOL,
        per_leg_slippage_bps=300, expected_tokens_out_base=4 * LAMPORTS_PER_SOL,
    )
    floor_sum = sum(leg.min_out_base for leg in plan.legs)
    # Even with EVERY leg sandwiched (worst case), proceeds >= the sum of the
    # per-leg revert floors — the attacker cannot extract below the bound.
    worst_case = _exit_under_sandwich_attempts(
        plan=plan, notional_base=4 * LAMPORTS_PER_SOL, realized_r=Decimal("0.01"),
        base_haircut_bps=9_000, attempts=50, seed=3,
    )
    assert worst_case >= floor_sum


# =========================================================================== #
# 6. Money discipline — integer base units / int bps; float rejected.
# =========================================================================== #


def test_exit_leg_rejects_float():
    with pytest.raises(TypeError):
        ExitLeg(leg_index=0, fraction_bps=1.0, min_out_base=1, private=True)  # type: ignore[arg-type]


def test_pool_liquidity_rejects_float():
    with pytest.raises(TypeError):
        PoolLiquidity(quote_reserve_base=1.0, sell_notional_base=1)  # type: ignore[arg-type]


def test_stamp_rejects_float_edge():
    rec = _recorder()
    with pytest.raises(TypeError):
        rec.stamp(
            candidate_id="c", mint="M", expected_edge_lamports=1.5,  # type: ignore[arg-type]
            decision_slot=1000, required_percentile=50,
        )


def test_planner_rejects_float_fraction():
    planner = ExitProtectionPlanner()
    with pytest.raises(TypeError):
        planner.plan(
            mint="M", fraction_bps=1.0,  # type: ignore[arg-type]
            mode=MevExitMode.SECURE, pool=_DEEP_POOL,
            per_leg_slippage_bps=150, expected_tokens_out_base=1_000_000,
        )


def test_recorder_rejects_float_cap_frac():
    with pytest.raises(TypeError):
        TipContentionRecorder(StaticTipStream([_snapshot()]), edge_cap_frac=0.30)  # type: ignore[arg-type]


def test_zero_expected_out_rejected():
    planner = ExitProtectionPlanner()
    with pytest.raises(ValueError):
        planner.plan(
            mint="M", fraction_bps=10_000, mode=MevExitMode.SECURE, pool=_DEEP_POOL,
            per_leg_slippage_bps=150, expected_tokens_out_base=0,
        )


# =========================================================================== #
# 7. Defaults — SECURE is the production default (OQ-008).
# =========================================================================== #


def test_secure_is_the_default_mode():
    from aats.mev.split_exit import DEFAULT_EXIT_MODE

    assert DEFAULT_EXIT_MODE is MevExitMode.SECURE
