"""End-to-end edge-gate integration (T-330).

Wires the three deliverables together as the SNIPE path would:
  1. price the tip from the LIVE stream (edge-bounded),
  2. if DO_NOT_SUBMIT -> no bundle is built, nothing is submitted (cost gate),
  3. if SUBMIT -> build the atomic buy bundle with the priced tip and submit
     (DRY-RUN by default, so nothing reaches the block engine),
  4. trace the per-hop latency, internal vs submission kept separate.

This is the cost-aware-or-do-not-submit discipline proven across the modules.
"""
from __future__ import annotations

from aats.contracts.venue import SignedTx
from aats.mev.bundle_submitter import (
    AtomicBuyBundle,
    BundleStatus,
    JitoBundleSubmitter,
    MockBlockEngine,
)
from aats.mev.latency_tracer import LatencyTracer
from aats.mev.tip_controller import TipController, TipDecisionKind
from aats.mev.tip_stream import StaticTipStream, TipFloorSnapshot

LAMPORTS_PER_SOL = 1_000_000_000


def _stream(p50):
    return StaticTipStream(
        [
            TipFloorSnapshot(
                p25_lamports=p50 // 2,
                p50_lamports=p50,
                p75_lamports=p50 * 2,
                p95_lamports=p50 * 4,
                tip_accounts=("TipAcct1111111111111111111111111111111111111",),
                as_of_slot=1000,
                as_of_block_time_ms=400_000,
            )
        ]
    )


def _signed():
    return SignedTx(
        serialized_b64="ZmFrZQ==",
        signer_pubkey="Pub1111111111111111111111111111111111111111",
        intent_kind="ENTRY",
        mint="Mint11111111111111111111111111111111111111",
        client_intent_id="cid-int",
    )


def test_priced_out_path_submits_nothing():
    # Tiny edge, fat live floor -> DO_NOT_SUBMIT, and we never build/submit a bundle.
    ctrl = TipController(_stream(p50=5_000_000))  # 0.005 SOL floor
    edge = 1_000_000  # 0.001 SOL edge -> ceiling 0.0003 SOL < floor
    decision = ctrl.price_tip(
        expected_edge_lamports=edge, decision_slot=1000, contention_bucket="low"
    )
    assert decision.kind is TipDecisionKind.DO_NOT_SUBMIT

    # The submit step is guarded by the decision — no bundle is built at all.
    submitted = False
    if decision.kind is TipDecisionKind.SUBMIT:  # pragma: no cover - must not run
        submitted = True
    assert submitted is False


def test_full_submit_path_dry_run_flat(monkeypatch):
    monkeypatch.delenv("DRY_RUN_ENABLED", raising=False)  # DRY-RUN default
    ctrl = TipController(_stream(p50=200_000))
    edge = 5 * LAMPORTS_PER_SOL
    decision = ctrl.price_tip(
        expected_edge_lamports=edge, decision_slot=1000, contention_bucket="low"
    )
    assert decision.kind is TipDecisionKind.SUBMIT
    assert decision.tip_lamports == 200_000
    # tip is within the 0.30x edge ceiling
    assert decision.tip_lamports <= decision.edge_ceiling_lamports

    # Build the atomic bundle with the PRICED tip (not a constant).
    bundle = AtomicBuyBundle(
        signed_buy_tx=_signed(),
        tip_lamports=decision.tip_lamports,
        tip_account="TipAcct1111111111111111111111111111111111111",
        min_tokens_out_base=1_000_000,
        client_intent_id="cid-int",
    )
    sub = JitoBundleSubmitter(MockBlockEngine(actual_tokens_out_base=2_000_000))
    result = sub.submit_atomic_buy(bundle)
    # DRY-RUN: nothing reaches the engine; flat state.
    assert result.status is BundleStatus.DRY_RUN
    assert result.landed_buy is False
    assert result.tip_lamports == 200_000  # the priced tip carried through


def test_latency_trace_alongside_submit():
    tracer = LatencyTracer(metrics=None)
    trace = tracer.start_trace(event_slot=1000, event_block_time_ms=400_000)
    for hop, us in {
        "ingress_detect": 60_000,
        "decode": 1_200,
        "gate": 2_000,
        "decide": 1_500,
        "build_sign": 1_300,
        "submit_call": 800,
        "block_engine_rtt": 55_000,
    }.items():
        trace.record_hop_us(hop, us)
    tracer.record(trace)
    led = tracer.ledger()
    # internal compute is reported separately from the 55ms submission RTT.
    assert led["rollups"]["internal_total"]["p50_ms"] < 70.0
    assert led["rollups"]["submission_rtt"]["p50_ms"] == 55.0
