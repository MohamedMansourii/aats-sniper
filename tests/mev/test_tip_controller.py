"""Tests for the edge-bounded TipController (T-330).

Proves the tip math the way the quant audits it (every basis point):
  - tip NEVER exceeds 0.30x expected edge (the hard ceiling),
  - tip is READ from the injectable LIVE stream, never hardcoded,
  - below the cost threshold (floor > ceiling) -> DO_NOT_SUBMIT,
  - tip scales with the live percentile / contention bucket,
  - point-in-time: a future snapshot is never used to price,
  - tip-as-%-of-PnL is logged, and is integer bps (no float money).
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from aats.mev.tip_controller import (
    DEFAULT_EDGE_CAP_FRAC,
    TipController,
    TipDecisionKind,
)
from aats.mev.tip_stream import (
    StaleTipStreamError,
    StaticTipStream,
    TipFloorSnapshot,
)

LAMPORTS_PER_SOL = 1_000_000_000


def _snapshot(
    *,
    p25=None,
    p50=200_000,
    p75=None,
    p95=None,
    as_of_slot=1000,
    block_time_ms=1_000_000,
):
    # Keep the ladder monotonic when a caller varies only p50: derive the
    # surrounding percentiles from p50 unless explicitly overridden.
    p25 = p25 if p25 is not None else p50 // 2
    p75 = p75 if p75 is not None else p50 * 2
    p95 = p95 if p95 is not None else p50 * 4
    return TipFloorSnapshot(
        p25_lamports=p25,
        p50_lamports=p50,
        p75_lamports=p75,
        p95_lamports=p95,
        tip_accounts=("TipAcct1111111111111111111111111111111111111",),
        as_of_slot=as_of_slot,
        as_of_block_time_ms=block_time_ms,
    )


def _controller(snap=None, cap=DEFAULT_EDGE_CAP_FRAC):
    stream = StaticTipStream([snap or _snapshot()])
    return TipController(stream, edge_cap_frac=cap)


# ---------------------------------------------------------------------------
# Tip NEVER exceeds 0.30x edge (the load-bearing invariant).
# ---------------------------------------------------------------------------


def test_tip_never_exceeds_edge_cap_when_submitting():
    # Edge = 2 SOL; ceiling = 0.30 * 2 SOL = 0.6 SOL.  Floor (p50) = 0.0002 SOL.
    ctrl = _controller(_snapshot(p50=200_000))
    edge = 2 * LAMPORTS_PER_SOL
    d = ctrl.price_tip(
        expected_edge_lamports=edge, decision_slot=1000, contention_bucket="low"
    )
    assert d.kind is TipDecisionKind.SUBMIT
    ceiling = int(Decimal("0.30") * Decimal(edge))
    assert d.edge_ceiling_lamports == ceiling
    assert d.tip_lamports <= d.edge_ceiling_lamports
    # bid is exactly the live floor (never a bp over)
    assert d.tip_lamports == 200_000


def test_tip_capped_property_across_many_edges():
    # For ANY edge that submits, tip <= 0.30 * edge.  Sweep edges and percentiles.
    for edge_sol in (1, 2, 5, 10):
        for floor in (50_000, 200_000, 1_000_000, 5_000_000):
            snap = _snapshot(p25=floor, p50=floor, p75=floor, p95=floor)
            ctrl = _controller(snap)
            edge = edge_sol * LAMPORTS_PER_SOL
            d = ctrl.price_tip(
                expected_edge_lamports=edge,
                decision_slot=1000,
                contention_bucket="low",
            )
            ceiling = int(Decimal("0.30") * Decimal(edge))
            if d.kind is TipDecisionKind.SUBMIT:
                assert d.tip_lamports <= ceiling, (
                    f"edge={edge} floor={floor}: tip {d.tip_lamports} > ceiling {ceiling}"
                )


def test_tip_at_exactly_the_ceiling_submits_not_over():
    # Floor == ceiling exactly -> still submits (<=), tip == ceiling, never over.
    edge = 1_000_000  # 0.001 SOL edge
    ceiling = int(Decimal("0.30") * Decimal(edge))  # 300_000
    ctrl = _controller(_snapshot(p25=ceiling, p50=ceiling, p75=ceiling, p95=ceiling))
    d = ctrl.price_tip(
        expected_edge_lamports=edge, decision_slot=1000, contention_bucket="low"
    )
    assert d.kind is TipDecisionKind.SUBMIT
    assert d.tip_lamports == ceiling
    assert d.tip_lamports <= d.edge_ceiling_lamports


# ---------------------------------------------------------------------------
# Priced out -> DO_NOT_SUBMIT (the decisive niche rule).
# ---------------------------------------------------------------------------


def test_priced_out_when_floor_exceeds_ceiling():
    # Live floor (p50) one lamport above the ceiling -> refuse, never bid up.
    edge = 1_000_000  # ceiling = 300_000
    ctrl = _controller(_snapshot(p50=300_001))
    d = ctrl.price_tip(
        expected_edge_lamports=edge, decision_slot=1000, contention_bucket="low"
    )
    assert d.kind is TipDecisionKind.DO_NOT_SUBMIT
    assert d.tip_lamports == 0
    assert "priced_out" in d.reason


def test_no_edge_refuses_before_touching_stream():
    ctrl = _controller(_snapshot())
    d = ctrl.price_tip(
        expected_edge_lamports=0, decision_slot=1000, contention_bucket="low"
    )
    assert d.kind is TipDecisionKind.DO_NOT_SUBMIT
    assert d.tip_lamports == 0
    assert d.reason == "no_expected_edge"


def test_negative_edge_refuses():
    ctrl = _controller(_snapshot())
    d = ctrl.price_tip(
        expected_edge_lamports=-5_000, decision_slot=1000, contention_bucket="low"
    )
    assert d.kind is TipDecisionKind.DO_NOT_SUBMIT
    assert d.tip_lamports == 0


# ---------------------------------------------------------------------------
# Read from the LIVE injectable stream (never hardcoded).
# ---------------------------------------------------------------------------


def test_tip_reads_from_injected_stream_not_constant():
    # Two different injected streams -> two different priced tips for the SAME edge.
    edge = 5 * LAMPORTS_PER_SOL  # large enough that neither is priced out
    ctrl_a = _controller(_snapshot(p50=150_000))
    ctrl_b = _controller(_snapshot(p50=900_000))
    da = ctrl_a.price_tip(
        expected_edge_lamports=edge, decision_slot=1000, contention_bucket="low"
    )
    db = ctrl_b.price_tip(
        expected_edge_lamports=edge, decision_slot=1000, contention_bucket="low"
    )
    assert da.kind is TipDecisionKind.SUBMIT and db.kind is TipDecisionKind.SUBMIT
    assert da.tip_lamports == 150_000
    assert db.tip_lamports == 900_000
    assert da.tip_lamports != db.tip_lamports  # NOT a hardcoded constant


def test_tip_scales_with_contention_bucket_percentile():
    # Same stream, same edge — higher contention reads a higher percentile.
    edge = 10 * LAMPORTS_PER_SOL
    ctrl = _controller(_snapshot(p50=200_000, p75=400_000, p95=800_000))
    low = ctrl.price_tip(
        expected_edge_lamports=edge, decision_slot=1000, contention_bucket="low"
    )
    med = ctrl.price_tip(
        expected_edge_lamports=edge, decision_slot=1000, contention_bucket="medium"
    )
    high = ctrl.price_tip(
        expected_edge_lamports=edge, decision_slot=1000, contention_bucket="high"
    )
    assert low.tip_lamports == 200_000  # p50
    assert med.tip_lamports == 400_000  # p75
    assert high.tip_lamports == 800_000  # p95
    assert low.tip_lamports < med.tip_lamports < high.tip_lamports
    assert (low.percentile_used, med.percentile_used, high.percentile_used) == (50, 75, 95)


# ---------------------------------------------------------------------------
# Point-in-time correctness (no lookahead).
# ---------------------------------------------------------------------------


def test_future_snapshot_never_used():
    # Only a snapshot at slot 2000 exists; decision at slot 1500 must NOT see it.
    stream = StaticTipStream([_snapshot(as_of_slot=2000)])
    ctrl = TipController(stream)
    d = ctrl.price_tip(
        expected_edge_lamports=5 * LAMPORTS_PER_SOL,
        decision_slot=1500,
        contention_bucket="low",
    )
    assert d.kind is TipDecisionKind.DO_NOT_SUBMIT
    assert d.reason == "no_live_tip_floor"


def test_freshest_past_snapshot_is_used():
    stream = StaticTipStream(
        [
            _snapshot(p50=100_000, as_of_slot=900),
            _snapshot(p50=300_000, as_of_slot=1000),
            _snapshot(p50=999_000, as_of_slot=2000),  # future — must be ignored
        ]
    )
    ctrl = TipController(stream)
    d = ctrl.price_tip(
        expected_edge_lamports=10 * LAMPORTS_PER_SOL,
        decision_slot=1500,
        contention_bucket="low",
    )
    assert d.kind is TipDecisionKind.SUBMIT
    assert d.snapshot_slot == 1000  # freshest at-or-before 1500
    assert d.tip_lamports == 300_000


def test_stream_raises_when_no_valid_snapshot():
    stream = StaticTipStream([_snapshot(as_of_slot=5000)])
    with pytest.raises(StaleTipStreamError):
        stream.current(decision_slot=4000)


# ---------------------------------------------------------------------------
# tip-as-%-of-PnL logging + exact-money guards.
# ---------------------------------------------------------------------------


def test_tip_pct_of_edge_is_integer_bps():
    edge = 1_000_000
    ctrl = _controller(_snapshot(p50=30_000))  # 3% of edge -> 300 bps
    d = ctrl.price_tip(
        expected_edge_lamports=edge, decision_slot=1000, contention_bucket="low"
    )
    assert d.kind is TipDecisionKind.SUBMIT
    assert d.tip_pct_of_edge_bps == 300  # 30_000 / 1_000_000 * 10000
    assert isinstance(d.tip_pct_of_edge_bps, int)


def test_log_tip_pct_of_pnl_positive():
    bps = TipController.log_tip_pct_of_pnl(
        client_intent_id="cid-1", tip_lamports=50_000, realized_pnl_lamports=1_000_000
    )
    assert bps == 500  # 5%


def test_log_tip_pct_of_pnl_non_positive_sentinel():
    bps = TipController.log_tip_pct_of_pnl(
        client_intent_id="cid-2", tip_lamports=50_000, realized_pnl_lamports=0
    )
    assert bps == -1  # pure-cost sentinel, never a divide-by-zero


def test_float_edge_rejected():
    ctrl = _controller(_snapshot())
    with pytest.raises(TypeError):
        ctrl.price_tip(
            expected_edge_lamports=1.5, decision_slot=1000, contention_bucket="low"  # type: ignore[arg-type]
        )


def test_float_cap_frac_rejected():
    stream = StaticTipStream([_snapshot()])
    with pytest.raises(TypeError):
        TipController(stream, edge_cap_frac=0.30)  # type: ignore[arg-type]


def test_cap_frac_out_of_range_rejected():
    stream = StaticTipStream([_snapshot()])
    with pytest.raises(ValueError):
        TipController(stream, edge_cap_frac=Decimal("1.5"))


def test_default_cap_is_thirty_percent():
    assert Decimal("0.30") == DEFAULT_EDGE_CAP_FRAC


def test_log_tip_pct_of_pnl_rejects_float():
    with pytest.raises(TypeError):
        TipController.log_tip_pct_of_pnl(
            client_intent_id="x", tip_lamports=1.0, realized_pnl_lamports=100  # type: ignore[arg-type]
        )


def test_snapshot_rejects_float_lamports():
    with pytest.raises(ValueError):
        TipFloorSnapshot(
            p25_lamports=1.0,  # type: ignore[arg-type]
            p50_lamports=2,
            p75_lamports=3,
            p95_lamports=4,
            tip_accounts=("a",),
            as_of_slot=1,
            as_of_block_time_ms=1,
        )


def test_snapshot_rejects_non_monotonic_ladder():
    with pytest.raises(ValueError):
        TipFloorSnapshot(
            p25_lamports=500,
            p50_lamports=200,  # decreasing -> reject
            p75_lamports=300,
            p95_lamports=400,
            tip_accounts=("a",),
            as_of_slot=1,
            as_of_block_time_ms=1,
        )
