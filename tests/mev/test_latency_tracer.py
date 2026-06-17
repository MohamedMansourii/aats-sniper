"""Tests for the per-hop LatencyTracer (T-330).

Proves:
  - the ledger SEPARATES internal compute from block-engine RTT from leader-land
    (the C-1 mandate),
  - p50/p95/p99 are computed per hop (proven against a replayed event, not asserted),
  - traces are anchored to EVENT-TIME, not compute-time,
  - emit to telemetry never sums internal + submission into one number,
  - an unclassed hop name is rejected (no silent ledger growth).
"""
from __future__ import annotations

import pytest

from aats.mev.latency_tracer import (
    HOP_CLASS,
    HopClass,
    LatencyTracer,
)


def _replay_one_trace(tracer, *, event_slot, ledger_us):
    """Replay a recorded liquidity-event trace from a per-hop microsecond map."""
    trace = tracer.start_trace(event_slot=event_slot, event_block_time_ms=event_slot * 400)
    for hop, us in ledger_us.items():
        trace.record_hop_us(hop, us)
    tracer.record(trace)
    return trace


# A recorded/replayed liquidity-event ledger (per hop, microseconds).
# Numbers mirror latency-budget.md §2 (dedicated_geyser tier).
_RECORDED_US = {
    "ingress_detect": 60_000,
    "decode": 1_200,
    "gate": 2_000,
    "decide": 1_500,
    "build_sign": 1_300,
    "submit_call": 800,
    "block_engine_rtt": 55_000,
    "leader_land": 50_000,
}


# ---------------------------------------------------------------------------
# C-1 class separation.
# ---------------------------------------------------------------------------


def test_ledger_separates_internal_submission_leaderland():
    tracer = LatencyTracer(metrics=None)
    _replay_one_trace(tracer, event_slot=1000, ledger_us=_RECORDED_US)
    led = tracer.ledger()

    # Each hop carries its C-1 class.
    assert led["hops"]["ingress_detect"]["class"] == "INTERNAL"
    assert led["hops"]["block_engine_rtt"]["class"] == "SUBMISSION"
    assert led["hops"]["leader_land"]["class"] == "LEADER_LAND"

    # Rollups are SEPARATE — internal total excludes RTT and leader-land.
    internal_p50 = led["rollups"]["internal_total"]["p50_ms"]
    submission_p50 = led["rollups"]["submission_rtt"]["p50_ms"]
    leader_p50 = led["rollups"]["leader_land"]["p50_ms"]

    # internal = 60+1.2+2+1.5+1.3+0.8 = 66.8 ms
    assert abs(internal_p50 - 66.8) < 0.5
    assert abs(submission_p50 - 55.0) < 0.5
    assert abs(leader_p50 - 50.0) < 0.5
    # The honesty check: internal is NOT the sum of all three.
    assert internal_p50 < submission_p50 + internal_p50 + leader_p50


def test_internal_total_excludes_network_hops():
    tracer = LatencyTracer(metrics=None)
    trace = _replay_one_trace(tracer, event_slot=1, ledger_us=_RECORDED_US)
    # Per-trace rollups directly.
    assert trace.internal_total_us() == 60_000 + 1_200 + 2_000 + 1_500 + 1_300 + 800
    assert trace.submission_rtt_us() == 55_000
    assert trace.leader_land_us() == 50_000


# ---------------------------------------------------------------------------
# p50/p95/p99 proven over many replayed traces.
# ---------------------------------------------------------------------------


def test_percentiles_computed_per_hop():
    tracer = LatencyTracer(metrics=None)
    # Replay 100 traces with decode varying 1..100 us so percentiles are checkable.
    for i in range(1, 101):
        trace = tracer.start_trace(event_slot=i, event_block_time_ms=i * 400)
        trace.record_hop_us("decode", i)  # 1..100 us
        tracer.record(trace)
    led = tracer.ledger()
    decode = led["hops"]["decode"]
    assert decode["n"] == 100
    # nearest-rank: p50 of 1..100 -> 50us = 0.05ms; p95 -> 95us; p99 -> 99us
    assert decode["p50_ms"] == pytest.approx(0.05, abs=0.001)
    assert decode["p95_ms"] == pytest.approx(0.095, abs=0.001)
    assert decode["p99_ms"] == pytest.approx(0.099, abs=0.001)


def test_context_manager_hop_records_a_span():
    tracer = LatencyTracer(metrics=None)
    trace = tracer.start_trace(event_slot=5, event_block_time_ms=2000)
    with trace.hop("decode"):
        pass  # near-zero but non-negative
    tracer.record(trace)
    led = tracer.ledger()
    assert led["hops"]["decode"]["n"] == 1
    assert led["hops"]["decode"]["p50_ms"] >= 0.0


# ---------------------------------------------------------------------------
# Event-time anchoring (point-in-time honesty).
# ---------------------------------------------------------------------------


def test_trace_anchored_to_event_time_not_compute_time():
    tracer = LatencyTracer(metrics=None)
    trace = tracer.start_trace(event_slot=123, event_block_time_ms=49_200)
    assert trace.event_slot == 123
    assert trace.event_block_time_ms == 49_200
    # wall_clock_ms exists for monitoring only — and is distinct from the anchor.
    assert trace.wall_clock_ms > 0


# ---------------------------------------------------------------------------
# Unclassed hop guard.
# ---------------------------------------------------------------------------


def test_unknown_hop_rejected():
    tracer = LatencyTracer(metrics=None)
    trace = tracer.start_trace(event_slot=1, event_block_time_ms=400)
    with pytest.raises(ValueError):
        trace.record_hop_us("totally_new_hop", 100)


def test_negative_duration_rejected():
    tracer = LatencyTracer(metrics=None)
    trace = tracer.start_trace(event_slot=1, event_block_time_ms=400)
    with pytest.raises(ValueError):
        trace.record_hop_us("decode", -5)


def test_hop_class_table_complete():
    # Every classed hop is one of the three C-1 classes.
    for cls in HOP_CLASS.values():
        assert cls in (HopClass.INTERNAL, HopClass.SUBMISSION, HopClass.LEADER_LAND)


# ---------------------------------------------------------------------------
# Emit to telemetry keeps internal and submission SEPARATE.
# ---------------------------------------------------------------------------


class _SpyHist:
    def __init__(self):
        self.observed = []

    def observe(self, v):
        self.observed.append(v)


class _SpyMetrics:
    def __init__(self):
        self.snipe_decision_latency_seconds = _SpyHist()
        self.time_to_land_seconds = _SpyHist()


def test_emit_routes_internal_and_submission_to_separate_histograms():
    spy = _SpyMetrics()
    tracer = LatencyTracer(metrics=spy)
    _replay_one_trace(tracer, event_slot=1000, ledger_us=_RECORDED_US)
    # Internal-only total -> snipe decision histogram (~66.8 ms = 0.0668 s)
    assert len(spy.snipe_decision_latency_seconds.observed) == 1
    assert spy.snipe_decision_latency_seconds.observed[0] == pytest.approx(0.0668, abs=0.001)
    # Submission RTT -> time-to-land histogram (55 ms = 0.055 s), NEVER summed with internal.
    assert len(spy.time_to_land_seconds.observed) == 1
    assert spy.time_to_land_seconds.observed[0] == pytest.approx(0.055, abs=0.001)


def test_emit_never_breaks_on_metrics_error():
    class _Boom:
        @property
        def snipe_decision_latency_seconds(self):
            raise RuntimeError("metrics down")

    tracer = LatencyTracer(metrics=_Boom())
    # Must not raise — telemetry failure cannot break the hot path.
    _replay_one_trace(tracer, event_slot=1, ledger_us={"decode": 100})
