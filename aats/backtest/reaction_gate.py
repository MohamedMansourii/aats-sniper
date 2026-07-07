"""CLUSTERED / BLOCK-bootstrap GATE-A / GATE-B for the reaction (front-run) strategy.

WHY A CLUSTERED BOOTSTRAP (the in-sample-CI blocker this closes)
===============================================================
`aats.models.gate_a.compute_gate_a` and `aats.models.gate_b.compute_gate_b_delta` resample
TRADES i.i.d. (`rng.integers(0, n, size=n)`).  I.i.d. resampling assumes every recorded trade is
an INDEPENDENT draw.  For the LAUNCH / MOMENTUM corpora that is defensible.  For the REACTION
corpus it is NOT, on two coupling channels the reviewers flagged as ACUTE:

  1. TIME CLUSTERING — reaction signals fire in bursts; many tokens co-move inside the same
     market-wide pump window, so trades near in time are positively correlated.
  2. SOURCE COUPLING — the model's ONLY selection lever is per-source reputation, computed from a
     source's own prior outcomes.  All of a source's decisions are therefore a coupled block.

Under positive within-group correlation, an i.i.d. bootstrap UNDERSTATES the delta's variance and
can manufacture a `lower95 > 0` on correlated noise — i.e. certify an edge that is a clustering
artifact.  Per the charter ("in-sample edge is no edge") no reaction GATE-B PASS may license
capital on the i.i.d. bound.  This module replaces the i.i.d. resample with a CLUSTER bootstrap
(resample whole clusters with replacement — the textbook fix for grouped/serially-correlated data)
and, to honour BOTH coupling channels, certifies a pass ONLY when the lower-95 bound clears zero
under the MOST CONSERVATIVE (minimum) of a SOURCE clustering AND a TIME-BLOCK clustering.  That is
strictly harder than either alone and strictly harder than i.i.d. — it can only WITHHOLD a pass,
never manufacture one (fail-safe).

WHAT IS AND IS NOT CHANGED
==========================
* The POINT ESTIMATES (aggregate net PnL, the model-vs-baseline delta, the per-unit-risk cohort
  figures) are computed by the SHARED `compute_gate_a` / `compute_gate_b_delta` verbatim, so they
  are byte-identical to the launch/momentum path (audit parity).  ONLY the lower-95 bound and the
  pass verdict are recomputed under the cluster bootstrap.
* The per-trade cohort arrays are the SHARED gate helpers (`_cohort_net_pnl_per_trade`,
  `_cohort_per_trade_ratios`, `_per_unit_risk`), reused so the clustered resample aggregates EXACTLY
  what the gates aggregate — no divergent statistic.
* The MINIMUM-SAMPLE guard (GATE-B) is inherited unchanged from `compute_gate_b_delta`.

A clustering with FEWER THAN TWO distinct clusters carries no resampleable structure (its bootstrap
is degenerate at the point estimate), so it cannot CERTIFY a pass: a pass additionally requires at
least one clustering with >= 2 clusters.  This blocks a spurious "pass" on a single-cluster corpus.

DETERMINISTIC / OFFLINE / NO CAPITAL
====================================
Pure function of the recorded TradeOutcome list + the cluster labels + a seed.  No network, no
clock, no keypair.  Same seed reproduces the bound.  This module only MEASURES — it never sizes,
never overrides a stop, never builds/signs a transaction (the gate boundaries, non-waivable).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

import numpy as np

from aats.models.gate_a import GateAResult, _cohort_net_pnl_per_trade, compute_gate_a
from aats.models.gate_b import (
    DEFAULT_EFFECTIVE_MIN_SAMPLE,
    DEFAULT_MIN_SAMPLE,
    GateBResult,
    TradeOutcome,
    UnitOfRisk,
    _cohort_per_trade_ratios,
    _per_unit_risk,
    compute_gate_b_delta,
)

# TIME-BLOCK width for the block bootstrap, in on-chain SLOTS.  Solana runs ~2.5 slots/s, so ~750
# slots ~= 5 minutes — a block wide enough to hold a market-wide reaction burst together (the unit
# of temporal correlation) yet fine enough to leave many independent blocks over a real corpus.
# A methodology constant (like the gates' n_bootstrap / seed), NOT a selection constant: it only
# widens/narrows the CI, never the point estimate, and the fail-safe direction is conservative.
GATE_CLUSTER_SLOTS_PER_BLOCK = 750

# A clustering needs at least this many distinct clusters to certify a pass (below it the cluster
# bootstrap is degenerate — a single independent unit cannot bound anything).
_MIN_CLUSTERS_TO_CERTIFY = 2

_LOWER_PCT = 2.5  # 2.5th percentile => the lower 95% bound (matches gate_a / gate_b)


def _cluster_index(cluster_labels: Sequence[object]) -> list[np.ndarray]:
    """Group row indices [0, n) by their cluster label, in FIRST-SEEN label order (deterministic).

    Returns one index array per distinct cluster.  Deterministic regardless of label hashing /
    PYTHONHASHSEED (insertion-ordered dict, integer indices) so the bootstrap is reproducible.
    """
    groups: dict[object, list[int]] = {}
    for i, lab in enumerate(cluster_labels):
        groups.setdefault(lab, []).append(i)
    return [np.asarray(idxs, dtype=np.int64) for idxs in groups.values()]


def _clustered_lower95(
    stat_of_idx,
    clusters: list[np.ndarray],
    *,
    n_bootstrap: int,
    seed: int,
) -> float:
    """Lower-95 bound of `stat_of_idx` under a CLUSTER bootstrap (resample whole clusters).

    Each iteration draws `len(clusters)` clusters WITH REPLACEMENT and concatenates their member row
    indices, then evaluates `stat_of_idx(concatenated_idx)`.  Returns the 2.5th percentile of the
    bootstrap distribution.  With a single cluster the distribution is degenerate at the point
    statistic (the caller refuses to certify on < 2 clusters).
    """
    k = len(clusters)
    if k == 0:
        return 0.0
    rng = np.random.default_rng(seed)
    boot = np.empty(n_bootstrap, dtype=np.float64)
    for b in range(n_bootstrap):
        chosen = rng.integers(0, k, size=k)
        idx = np.concatenate([clusters[c] for c in chosen])
        boot[b] = stat_of_idx(idx)
    return float(np.percentile(boot, _LOWER_PCT))


def _time_block_labels(outcomes: list[TradeOutcome]) -> list[int]:
    """TIME-BLOCK cluster labels from each outcome's on-chain `decision_slot` (the anchor)."""
    return [o.decision_slot // GATE_CLUSTER_SLOTS_PER_BLOCK for o in outcomes]


def build_reaction_clusterings(
    outcomes: list[TradeOutcome], source_ids: Sequence[str]
) -> list[tuple[str, list[object]]]:
    """The named clusterings a reaction GATE certifies under: SOURCE and TIME-BLOCK.

    `source_ids` MUST be aligned with `outcomes` (the harness surfaces
    `ReactionHarnessStats.outcome_source_ids`).  Both channels are returned so the runner can report
    which one bound the verdict, and the gate takes the MOST CONSERVATIVE (min lower-95) of the two.
    """
    if len(source_ids) != len(outcomes):
        raise ValueError(
            "source_ids must align with outcomes "
            f"(got {len(source_ids)} ids for {len(outcomes)} outcomes)"
        )
    return [
        ("source", list(source_ids)),
        ("time_block", list(_time_block_labels(outcomes))),
    ]


def clustered_gate_a(
    outcomes: list[TradeOutcome],
    clusterings: list[tuple[str, list[object]]],
    *,
    model: bool = True,
    n_bootstrap: int = 2000,
    seed: int = 20260616,
) -> GateAResult:
    """GATE-A with a CLUSTER-bootstrap lower-95 bound (point estimate = shared `compute_gate_a`).

    Recomputes ONLY the lower-95 bound + the pass verdict under the cluster bootstrap; the aggregate
    net-PnL, mean, and per-SOL figures come straight from `compute_gate_a` (audit parity).  The bound
    is the MINIMUM across the supplied clusterings (source AND time-block => the conservative one),
    and a pass requires at least one clustering with >= 2 distinct clusters.
    """
    base = compute_gate_a(outcomes, model=model, n_bootstrap=n_bootstrap, seed=seed)
    per_trade = _cohort_net_pnl_per_trade(outcomes, model=model)

    def _mean_stat(idx: np.ndarray) -> float:
        return float(np.mean(per_trade[idx]))

    lower, enough = _min_lower_over_clusterings(
        _mean_stat, clusterings, n_bootstrap=n_bootstrap, seed=seed
    )
    return replace(
        base,
        lower_95_bound_lamports=lower,
        gate_a_pass=bool(lower > 0.0) and enough,
    )


def clustered_gate_b_delta(
    outcomes: list[TradeOutcome],
    clusterings: list[tuple[str, list[object]]],
    *,
    unit_of_risk: UnitOfRisk = UnitOfRisk.NET_PNL_PER_SOL,
    n_bootstrap: int = 2000,
    seed: int = 20260616,
    min_sample: int = DEFAULT_MIN_SAMPLE,
    effective_min_sample: int = DEFAULT_EFFECTIVE_MIN_SAMPLE,
) -> GateBResult:
    """GATE-B delta with a CLUSTER-bootstrap lower-95 (point delta = shared `compute_gate_b_delta`).

    Recomputes ONLY the lower-95 bound + the pass verdict under the cluster bootstrap; the model /
    baseline per-unit-risk figures and the delta come straight from `compute_gate_b_delta` (audit
    parity).  The bound is the MINIMUM across the supplied clusterings (source AND time-block), and a
    pass requires ALL of: the inherited minimum-sample guard, the inherited EFFECTIVE-sample guard
    (>= `effective_min_sample` model!=baseline decisions), AND at least one clustering with >= 2
    clusters — strictly harder than the i.i.d. bound, so it can only withhold a pass, never fabricate
    one.
    """
    base = compute_gate_b_delta(
        outcomes,
        unit_of_risk=unit_of_risk,
        n_bootstrap=n_bootstrap,
        seed=seed,
        min_sample=min_sample,
        effective_min_sample=effective_min_sample,
    )
    model_ratios = _cohort_per_trade_ratios(outcomes, model=True)
    base_ratios = _cohort_per_trade_ratios(outcomes, model=False)

    def _delta_stat(idx: np.ndarray) -> float:
        m = _per_unit_risk(model_ratios[idx], unit_of_risk)
        bl = _per_unit_risk(base_ratios[idx], unit_of_risk)
        return m - bl

    lower, enough = _min_lower_over_clusterings(
        _delta_stat, clusterings, n_bootstrap=n_bootstrap, seed=seed
    )
    return replace(
        base,
        lower_95_bound=lower,
        gate_b_pass=(
            bool(lower > 0.0)
            and base.min_sample_met
            and base.effective_sample_met
            and enough
        ),
    )


def _min_lower_over_clusterings(
    stat_of_idx,
    clusterings: list[tuple[str, list[object]]],
    *,
    n_bootstrap: int,
    seed: int,
) -> tuple[float, bool]:
    """Return (min lower-95 across clusterings, whether >=1 clustering has >= 2 clusters).

    A clustering with < 2 clusters is still evaluated (its degenerate bound == the point statistic,
    so it can still LOWER the min if the point estimate is negative), but it does NOT count toward
    the "enough clusters to certify" flag — a pass needs a genuinely resampleable clustering.
    """
    lowers: list[float] = []
    enough = False
    for _name, labels in clusterings:
        clusters = _cluster_index(labels)
        if len(clusters) >= _MIN_CLUSTERS_TO_CERTIFY:
            enough = True
        lowers.append(
            _clustered_lower95(stat_of_idx, clusters, n_bootstrap=n_bootstrap, seed=seed)
        )
    lower = min(lowers) if lowers else float("-inf")
    return lower, enough


__all__ = [
    "GATE_CLUSTER_SLOTS_PER_BLOCK",
    "build_reaction_clusterings",
    "clustered_gate_a",
    "clustered_gate_b_delta",
]
