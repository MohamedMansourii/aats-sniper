"""CLUSTERED / BLOCK-bootstrap GATE-A / GATE-B tests for the reaction strategy (FIX round 2, MAJOR-2).

Reaction signals cluster in time and are same-source-coupled via reputation, so the shared gates'
i.i.d. trade-resample bootstrap understates the delta's variance and can manufacture a lower95>0 on
correlated noise (the in-sample capital blocker).  `aats.backtest.reaction_gate` replaces the i.i.d.
resample with a CLUSTER bootstrap (resample whole clusters) and certifies a pass only under the MOST
CONSERVATIVE of a SOURCE and a TIME-BLOCK clustering.  These tests prove:
  * POINT-ESTIMATE PARITY — the aggregate PnL / delta come straight from the shared gates;
  * i.i.d.-EQUIVALENCE — a per-trade-unique clustering reproduces the i.i.d. bound byte-for-byte;
  * CONSERVATISM — on correlated data where i.i.d. PASSES, the source-cluster bootstrap WITHHOLDS;
  * MIN-OVER-CLUSTERINGS — adding the source clustering to an otherwise-passing set flips it to fail;
  * SINGLE-CLUSTER REFUSAL — a clustering with < 2 clusters cannot certify a pass.

Offline / deterministic. No network, no keypair, no capital.
"""
from __future__ import annotations

import pytest

from aats.backtest.reaction_gate import (
    build_reaction_clusterings,
    clustered_gate_a,
    clustered_gate_b_delta,
)
from aats.models.gate_a import compute_gate_a
from aats.models.gate_b import TradeOutcome, compute_gate_b_delta

_CAP = 100_000_000  # 0.1 SOL at risk per trade


def _o(mint, slot, *, model, base, net) -> TradeOutcome:
    return TradeOutcome(
        mint=mint,
        decision_slot=slot,
        model_selected=model,
        baseline_selected=base,
        net_pnl_lamports=net,
        sol_at_risk_lamports=_CAP,
    )


def _concentrated_delta_corpus():
    """150 trades / 5 equal sources. The model's entire edge is CONCENTRATED in ONE source (A): the
    model declines A's 30 losers (delta +0.5 each); B..E are delta-0 (both cohorts take them). The
    positive delta is perfectly correlated WITHIN cluster A — exactly the structure an i.i.d.
    bootstrap misreads as many independent positive draws.

    A's 30 declined losers are the EFFECTIVE (model!=baseline) cohort — above the GATE-B effective
    floor, so the ONLY thing withholding the pass here is the CLUSTER structure (the point this file
    proves), not the thin-cohort guard (which has its own coverage in test_gate_b.py)."""
    outs: list[TradeOutcome] = []
    labels: list[str] = []
    slot = 0
    for i in range(30):  # source A — proven loser the model avoids (the effective de-risk cohort)
        outs.append(_o(f"A_{i}", slot, model=False, base=True, net=-50_000_000))
        labels.append("A")
        slot += 1
    for s in ("B", "C", "D", "E"):  # delta-0 sources (both cohorts take them, net 0)
        for i in range(30):
            outs.append(_o(f"{s}_{i}", slot, model=True, base=True, net=0))
            labels.append(s)
            slot += 1
    return outs, labels


def _per_trade_unique(n: int) -> list[str]:
    return [f"u{i}" for i in range(n)]


# ---------------------------------------------------------------------------
# POINT-ESTIMATE PARITY + i.i.d. EQUIVALENCE
# ---------------------------------------------------------------------------


def test_gate_b_point_delta_matches_shared_gate() -> None:
    outs, _labels = _concentrated_delta_corpus()
    iid = compute_gate_b_delta(outs)
    clus = clustered_gate_b_delta(outs, [("u", _per_trade_unique(len(outs)))])
    # The delta / cohort figures are the SHARED gate's (audit parity) — only the CI changes.
    assert clus.delta == iid.delta
    assert clus.model_net_pnl_per_risk == iid.model_net_pnl_per_risk
    assert clus.baseline_net_pnl_per_risk == iid.baseline_net_pnl_per_risk


def test_per_trade_unique_clustering_reproduces_iid_bound() -> None:
    """A clustering of one-trade-per-cluster IS the i.i.d. bootstrap: same seed => byte-identical
    lower bound and verdict. This anchors the clustered machinery against the shared gate."""
    outs, _labels = _concentrated_delta_corpus()
    iid = compute_gate_b_delta(outs)
    clus = clustered_gate_b_delta(outs, [("u", _per_trade_unique(len(outs)))])
    assert clus.lower_95_bound == iid.lower_95_bound
    assert clus.gate_b_pass == iid.gate_b_pass

    iid_a = compute_gate_a(outs, model=False)
    clus_a = clustered_gate_a(outs, [("u", _per_trade_unique(len(outs)))], model=False)
    assert clus_a.total_net_pnl_lamports == iid_a.total_net_pnl_lamports  # point estimate reused
    assert clus_a.lower_95_bound_lamports == iid_a.lower_95_bound_lamports  # iid-equivalent CI


# ---------------------------------------------------------------------------
# CONSERVATISM — i.i.d. PASSES on correlated noise; the SOURCE-cluster bootstrap WITHHOLDS
# ---------------------------------------------------------------------------


def test_source_cluster_withholds_where_iid_manufactures_a_pass() -> None:
    """THE headline fix: on data whose positive delta is concentrated in ONE source cluster, the
    i.i.d. bootstrap certifies a PASS (spuriously), while the source-cluster bootstrap does NOT —
    proving the clustered CI cannot be fooled by correlated noise into licensing capital."""
    outs, source_labels = _concentrated_delta_corpus()

    iid = compute_gate_b_delta(outs)
    assert iid.delta > 0
    assert iid.gate_b_pass is True  # i.i.d. would license the edge (the danger)

    clustered = clustered_gate_b_delta(outs, [("source", source_labels)])
    assert clustered.delta == iid.delta  # same point estimate
    assert clustered.lower_95_bound <= iid.lower_95_bound  # strictly-not-narrower CI
    assert clustered.gate_b_pass is False  # WITHHELD — no capital on a one-cluster artifact


def test_min_over_clusterings_flips_a_passing_set_to_fail() -> None:
    """MIN-OVER-CLUSTERINGS: an i.i.d.-like (per-trade) clustering alone PASSES; ADDING the source
    clustering makes the certified bound the MIN of the two, which withholds. A pass must clear zero
    under BOTH the source AND the (here iid-like) channel."""
    outs, source_labels = _concentrated_delta_corpus()
    iid_like = ("u", _per_trade_unique(len(outs)))

    passes = clustered_gate_b_delta(outs, [iid_like])
    assert passes.gate_b_pass is True

    withheld = clustered_gate_b_delta(outs, [iid_like, ("source", source_labels)])
    assert withheld.gate_b_pass is False
    assert withheld.lower_95_bound <= passes.lower_95_bound


# ---------------------------------------------------------------------------
# SINGLE-CLUSTER REFUSAL — a degenerate clustering cannot certify a pass
# ---------------------------------------------------------------------------


def test_single_cluster_cannot_certify_pass() -> None:
    """A clustering with ONE cluster has no resampleable structure (its bootstrap is degenerate at
    the point estimate). Even with a strongly positive delta it must NOT certify a pass."""
    # Strong, uniform positive delta: model declines every proven-loser trade across 40 trades.
    outs = [_o(f"X_{i}", i, model=False, base=True, net=-50_000_000) for i in range(40)]
    one_cluster = ("all", ["c"] * len(outs))
    res = clustered_gate_b_delta(outs, [one_cluster])
    assert res.delta > 0
    assert res.gate_b_pass is False  # refused: < 2 clusters cannot bound anything

    res_a = clustered_gate_a(outs, [one_cluster], model=False)
    assert res_a.gate_a_pass is False


def test_strong_uniform_edge_passes_under_both_clusterings() -> None:
    """Sanity (the positive control): a strong edge spread across MANY independent sources AND time
    blocks clears the conservative min of both clusterings -> PASS. The clustered gate is not a
    blanket veto; it withholds only when the structure is degenerate/correlated."""
    # 40 sources x 1 trade, each a proven-loser the model avoids; slots spread across many blocks.
    outs, labels = [], []
    for i in range(40):
        outs.append(_o(f"S{i}", i * 1000, model=False, base=True, net=-50_000_000))
        labels.append(f"S{i}")
    clusterings = build_reaction_clusterings(outs, labels)
    res = clustered_gate_b_delta(outs, clusterings)
    assert res.delta > 0
    assert res.gate_b_pass is True


# ---------------------------------------------------------------------------
# build_reaction_clusterings — alignment + channels
# ---------------------------------------------------------------------------


def test_build_clusterings_requires_aligned_source_ids() -> None:
    outs = [_o(f"X_{i}", i, model=True, base=True, net=0) for i in range(3)]
    with pytest.raises(ValueError, match="align"):
        build_reaction_clusterings(outs, ["only-one"])
    names = [name for name, _labels in build_reaction_clusterings(outs, ["a", "b", "c"])]
    assert names == ["source", "time_block"]
