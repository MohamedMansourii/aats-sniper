"""CAPITAL-LICENSING purged + embargoed WALK-FORWARD over the REAL recorded corpus.

WHY THIS EXISTS (the in-sample blocker the verification flagged)
===============================================================
`run_edge_proof` scores the REAL corpus with a single bootstrap over the WHOLE record set —
i.i.d. for launch/momentum, source/time-block CLUSTERED for reaction.  A bootstrap resamples the
SAME in-sample window; it never scores the model on data it did not already see in aggregate.  The
charter is explicit ("in-sample edge is no edge"): a bootstrap `lower95 > 0` on the whole corpus is
a fast PRE-CHECK, NOT a capital license.  A license requires the model to clear the bar on
OUT-OF-SAMPLE, forward-in-time folds with the label-overlap leakage purged and a serial-correlation
embargo — the standard purged/embargoed walk-forward (López de Prado, walk-forward-methodology §2-3),
now run on the REAL corpus instead of only the synthetic `IS_BOOTSTRAP_NOT_REAL` bootstrap.

This module is the engine.  It is PURE and OFFLINE: it consumes the harness's `TradeOutcome` list
plus two ALIGNED metadata arrays (each decision's on-chain `event_time` anchor and its
`label_horizon_end`), builds forward-only folds, PURGES and EMBARGOES the boundaries, and scores each
fold — and a pooled out-of-sample union — with an injected fold scorer.  It NEVER sizes, signs, or
lands a transaction; it only measures.  Real capital stays DISABLED behind DRY-RUN regardless.

THE FOLD DISCIPLINE (walk-forward-methodology §2-3)
==================================================
  * Decisions are ordered STRICTLY by `event_time` (the on-chain anchor), never file order.
  * The ordered corpus is cut into `n_folds` contiguous, forward-in-time TEST folds by count.
  * PURGE — a decision is dropped from its fold if its LABEL horizon resolves AFTER the fold's end
    (`label_horizon_end > fold_test_end`): its realized outcome depends on price action that occurs
    in the NEXT fold's window, so scoring it here couples the two folds (the label-overlap leak).
    The purge is load-bearing: the trailing tail of every fold (whose horizon spills past the
    boundary) is removed, so folds are decoupled evidence rather than one serially-correlated block.
  * EMBARGO — after a fold's end, a gap (`embargo` in event-time units) is skipped: any decision in
    the NEXT fold whose `event_time` lands inside the gap is dropped, breaking the residual serial
    correlation of a decision that begins reacting to the same burst the prior fold's tail resolved
    into.  Together purge + embargo make the pooled out-of-sample union free of cross-fold label
    overlap, so the pooled bound is an honest OOS statistic, not an in-sample echo.

THE LICENSING VERDICT (conservative / fail-safe)
================================================
  * Each fold is scored OUT-OF-SAMPLE by the injected scorer (for the reaction path the scorer is the
    SOURCE + TIME-BLOCK clustered GATE-A / GATE-B — the same clustered resampling kept INSIDE every
    fold, so the in-fold CI still honours the coupling channels).
  * The PRIMARY statistic is the POOLED out-of-sample union of all purged/embargoed folds, scored
    once (combinatorial-purged-CV style): its GATE-A model bound AND GATE-B delta bound must both
    pass.  Because the pooled GATE-B inherits the EFFECTIVE-sample floor, the pool must carry a real
    de-risk cohort, not a thin one; and because the pooled CI is CLUSTERED by SOURCE and TIME-BLOCK
    (reaction path), a pooled pass concentrated in one source or one time burst is already withheld
    (that cluster resamples in/out, widening the bound) — so no separate single-fold veto is needed.
  * A GO additionally requires at least `LICENSING_MIN_FOLDS` (>= 5) non-empty forward folds.  The
    per-fold results and the count of positive-delta folds are REPORTED as diagnostics (fold
    consistency), not gated on — the reputation gate legitimately concentrates a reaction model's
    action in LATER folds, so a strict per-fold majority would spuriously withhold genuine edge; the
    clustered pooled bound is the statistically sound guard.  Every clause can only WITHHOLD a GO.

MONEY / DETERMINISTIC / REPRODUCIBLE
====================================
Money magnitudes stay INTEGER lamports inside the gates.  Pure function of the outcomes + metadata +
a fixed seed; the only floats are the dimensionless bootstrap bounds.  Same inputs + seed reproduce
the verdict byte-for-byte.  No RPC, no keypair, no OMS, no capital.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from aats.backtest.reaction_gate import (
    build_reaction_clusterings,
    clustered_gate_a,
    clustered_gate_b_delta,
)
from aats.models.gate_a import GateAResult, compute_gate_a
from aats.models.gate_b import GateBResult, TradeOutcome, compute_gate_b_delta

# A capital-licensing walk-forward needs at least this many non-empty forward folds (methodology
# §7.1).  Fewer folds cannot evidence out-of-sample stability, so a GO is withheld below this.
LICENSING_MIN_FOLDS = 5
# Build a small margin above the floor so purge/embargo attrition (which trims each fold's boundary)
# still leaves >= LICENSING_MIN_FOLDS non-empty folds on a healthy corpus.
DEFAULT_N_FOLDS = 6
# EMBARGO width as a fraction of the corpus event-time span.  A methodology constant (like the gates'
# n_bootstrap / seed), NOT a selection knob: it only widens/narrows the decoupling gap and the
# fail-safe direction is conservative (a larger embargo drops MORE boundary rows, never fewer).
DEFAULT_EMBARGO_FRAC = 0.02


class LicensingError(ValueError):
    """Raised when the walk-forward inputs are structurally unusable (misaligned metadata)."""


# A fold scorer maps GLOBAL outcome indices (into the full `outcomes` list) to the pair of
# (GATE-A model result, GATE-B delta result) for that subset.  Injected so the reaction path can
# pass the CLUSTERED gates and the launch/momentum path the i.i.d. gates, with the engine unchanged.
FoldScorer = Callable[[Sequence[int]], tuple[GateAResult, GateBResult]]


@dataclass(frozen=True)
class WalkForwardFold:
    """One forward-only, purged + embargoed test fold.

    `test_idx` are GLOBAL indices into the ordered-by-event-time corpus that SURVIVED purge +
    embargo (the fold's out-of-sample scoring set).  `n_purged` / `n_embargoed` record how many of
    the fold's raw members were dropped by each guard (so the purge's non-vacuity is auditable).
    """

    window_id: int
    test_idx: list[int]
    test_start_time: int
    test_end_time: int
    n_raw: int
    n_purged: int
    n_embargoed: int
    embargo: int


def build_purged_embargoed_folds(
    event_times: Sequence[int],
    label_horizon_ends: Sequence[int],
    *,
    n_folds: int = DEFAULT_N_FOLDS,
    embargo: int | None = None,
    embargo_frac: float = DEFAULT_EMBARGO_FRAC,
) -> list[WalkForwardFold]:
    """Build forward-only, event-time-ordered, PURGED + EMBARGOED walk-forward test folds.

    `event_times[i]` is decision i's on-chain anchor; `label_horizon_ends[i]` is when decision i's
    outcome is fully realized (anchor + max horizon).  Both are ALIGNED with the corpus order the
    caller will score against (the harness emits them aligned with `outcomes`).

    The ordered corpus is cut into `n_folds` contiguous forward chunks by COUNT; each chunk's
    [t_lo, t_hi] time span defines a test fold.  PURGE drops a member whose `label_horizon_end > t_hi`
    (label resolves past the fold boundary); EMBARGO drops a member whose `event_time` falls within
    `embargo` of the PRIOR fold's end.  `embargo` defaults to `embargo_frac * (t_max - t_min)`.

    Returns the folds in forward order (some may be empty after purge/embargo — the licensing logic
    counts only non-empty folds against the `LICENSING_MIN_FOLDS` floor).
    """
    n = len(event_times)
    if len(label_horizon_ends) != n:
        raise LicensingError(
            f"event_times ({n}) and label_horizon_ends ({len(label_horizon_ends)}) must align."
        )
    if n_folds < 1:
        raise LicensingError(f"n_folds must be >= 1 (got {n_folds}).")
    if n == 0:
        return []

    order = sorted(range(n), key=lambda i: (event_times[i], i))
    if embargo is None:
        span = event_times[order[-1]] - event_times[order[0]]
        embargo = int(span * embargo_frac)
    embargo = max(0, int(embargo))

    base, rem = divmod(n, n_folds)
    folds: list[WalkForwardFold] = []
    start = 0
    prev_test_end: int | None = None
    wid = 0
    for f in range(n_folds):
        size = base + (1 if f < rem else 0)
        if size == 0:
            continue
        chunk = order[start : start + size]
        start += size
        t_lo = event_times[chunk[0]]
        t_hi = event_times[chunk[-1]]

        kept: list[int] = []
        n_purged = 0
        n_embargoed = 0
        for i in chunk:
            # EMBARGO: a decision inside the gap after the prior fold's end is serially correlated
            # with that fold's boundary — drop it before it can be scored here.
            if prev_test_end is not None and event_times[i] <= prev_test_end + embargo:
                n_embargoed += 1
                continue
            # PURGE: a decision whose label resolves past this fold's end leaks into the next fold.
            if label_horizon_ends[i] > t_hi:
                n_purged += 1
                continue
            kept.append(i)

        folds.append(
            WalkForwardFold(
                window_id=wid,
                test_idx=kept,
                test_start_time=t_lo,
                test_end_time=t_hi,
                n_raw=size,
                n_purged=n_purged,
                n_embargoed=n_embargoed,
                embargo=embargo,
            )
        )
        prev_test_end = t_hi
        wid += 1

    return folds


def assert_purge_is_load_bearing(
    folds: Sequence[WalkForwardFold],
    label_horizon_ends: Sequence[int],
) -> None:
    """Prove the purge held: NO surviving fold member's label horizon reaches past its fold end.

    Raises AssertionError on a leak.  This is the non-vacuity/soundness check the purge exists for —
    a member whose `label_horizon_end > test_end_time` is exactly the cross-fold label overlap the
    purge must remove.
    """
    for fold in folds:
        for i in fold.test_idx:
            assert label_horizon_ends[i] <= fold.test_end_time, (
                f"PURGE LEAK: fold {fold.window_id} kept decision {i} whose label horizon "
                f"{label_horizon_ends[i]} > fold end {fold.test_end_time} — its outcome resolves in "
                "the next fold's window (the overlap the purge must remove)."
            )


@dataclass(frozen=True)
class FoldScore:
    """One fold's out-of-sample GATE-A / GATE-B result (or a degenerate marker on an empty fold)."""

    window_id: int
    n_test: int
    n_purged: int
    n_embargoed: int
    delta: float
    gate_a_pass: bool
    gate_b_pass: bool
    gate_a_summary: str
    gate_b_summary: str


@dataclass(frozen=True)
class LicensingResult:
    """The purged/embargoed walk-forward licensing verdict on the REAL corpus.

    `licensing_go` is the ONLY capital-licensing verdict.  It is True iff there are >=
    `LICENSING_MIN_FOLDS` non-empty folds AND the POOLED out-of-sample GATE-A (model) AND GATE-B both
    pass.  `n_folds_positive_delta` / `majority_consistent` are REPORTED fold-consistency diagnostics
    (not gated on — see the module docstring).  Every gated clause can only WITHHOLD a GO (fail-safe).
    """

    n_outcomes: int
    n_folds_requested: int
    n_folds_built: int
    min_folds: int
    folds_sufficient: bool
    embargo: int
    per_fold: list[FoldScore]
    pooled_n: int
    pooled_gate_a: GateAResult | None
    pooled_gate_b: GateBResult | None
    n_folds_positive_delta: int
    majority_consistent: bool
    licensing_go: bool
    reason: str

    def summary(self) -> str:
        head = (
            f"WALK-FORWARD LICENSE n={self.n_outcomes} folds={self.n_folds_built}"
            f"/{self.n_folds_requested} (min {self.min_folds}, embargo={self.embargo}): "
            f"{'GO' if self.licensing_go else 'NO-GO'}"
        )
        if self.pooled_gate_a is not None and self.pooled_gate_b is not None:
            head += (
                f"\n  pooled OOS n={self.pooled_n}: "
                f"{self.pooled_gate_a.summary()} | {self.pooled_gate_b.summary()}"
            )
        head += (
            f"\n  folds positive-delta={self.n_folds_positive_delta}/{self.n_folds_built} "
            f"(majority={'yes' if self.majority_consistent else 'no'})"
        )
        if not self.licensing_go:
            head += f"\n  reason: {self.reason}"
        return head


def walk_forward_licensing(
    outcomes: list[TradeOutcome],
    event_times: Sequence[int],
    label_horizon_ends: Sequence[int],
    *,
    fold_scorer: FoldScorer,
    n_folds: int = DEFAULT_N_FOLDS,
    embargo: int | None = None,
    embargo_frac: float = DEFAULT_EMBARGO_FRAC,
    min_folds: int = LICENSING_MIN_FOLDS,
) -> LicensingResult:
    """Score the REAL corpus with a purged/embargoed forward walk-forward and return the verdict.

    Builds forward folds (`build_purged_embargoed_folds`), scores each non-empty fold and the POOLED
    out-of-sample union with `fold_scorer`, and combines them into the conservative `licensing_go`
    verdict.  FAIL-CLOSED: an empty corpus yields a NO-GO with `pooled_*` None and a reason — never a
    fabricated pass.  Pure / deterministic given the scorer's seed.
    """
    n = len(outcomes)
    if not (len(event_times) == len(label_horizon_ends) == n):
        raise LicensingError(
            "outcomes, event_times, and label_horizon_ends must all align "
            f"(got {n}, {len(event_times)}, {len(label_horizon_ends)})."
        )

    folds = build_purged_embargoed_folds(
        event_times,
        label_horizon_ends,
        n_folds=n_folds,
        embargo=embargo,
        embargo_frac=embargo_frac,
    )
    # Soundness: prove the purge removed every cross-fold label overlap (never a silent leak).
    assert_purge_is_load_bearing(folds, label_horizon_ends)

    non_empty = [fold for fold in folds if fold.test_idx]
    per_fold: list[FoldScore] = []
    pooled_idx: list[int] = []
    n_positive = 0
    for fold in non_empty:
        pooled_idx.extend(fold.test_idx)
        ga, gb = fold_scorer(fold.test_idx)
        if gb.delta > 0:
            n_positive += 1
        per_fold.append(
            FoldScore(
                window_id=fold.window_id,
                n_test=len(fold.test_idx),
                n_purged=fold.n_purged,
                n_embargoed=fold.n_embargoed,
                delta=float(gb.delta),
                gate_a_pass=bool(ga.gate_a_pass),
                gate_b_pass=bool(gb.gate_b_pass),
                gate_a_summary=ga.summary(),
                gate_b_summary=gb.summary(),
            )
        )

    n_folds_built = len(non_empty)
    folds_sufficient = n_folds_built >= min_folds
    embargo_used = folds[0].embargo if folds else 0

    pooled_ga: GateAResult | None = None
    pooled_gb: GateBResult | None = None
    if pooled_idx:
        pooled_ga, pooled_gb = fold_scorer(pooled_idx)

    majority_consistent = n_folds_built > 0 and (n_positive * 2 > n_folds_built)
    pooled_go = (
        pooled_ga is not None
        and pooled_gb is not None
        and bool(pooled_ga.gate_a_pass)
        and bool(pooled_gb.gate_b_pass)
    )
    licensing_go = bool(folds_sufficient and pooled_go)

    reason = _licensing_reason(
        n_outcomes=n,
        folds_sufficient=folds_sufficient,
        n_folds_built=n_folds_built,
        min_folds=min_folds,
        pooled_ga=pooled_ga,
        pooled_gb=pooled_gb,
    )

    return LicensingResult(
        n_outcomes=n,
        n_folds_requested=n_folds,
        n_folds_built=n_folds_built,
        min_folds=min_folds,
        folds_sufficient=folds_sufficient,
        embargo=int(embargo_used),
        per_fold=per_fold,
        pooled_n=len(pooled_idx),
        pooled_gate_a=pooled_ga,
        pooled_gate_b=pooled_gb,
        n_folds_positive_delta=n_positive,
        majority_consistent=majority_consistent,
        licensing_go=licensing_go,
        reason=reason,
    )


def _licensing_reason(
    *,
    n_outcomes: int,
    folds_sufficient: bool,
    n_folds_built: int,
    min_folds: int,
    pooled_ga: GateAResult | None,
    pooled_gb: GateBResult | None,
) -> str:
    if n_outcomes == 0:
        return "FAIL-CLOSED: no resolved outcomes — no metric on no data."
    reasons: list[str] = []
    if not folds_sufficient:
        reasons.append(
            f"only {n_folds_built} non-empty walk-forward fold(s) < {min_folds} required"
        )
    if pooled_ga is not None and not pooled_ga.gate_a_pass:
        reasons.append("pooled OOS GATE-A (model net-of-cost) did not pass")
    if pooled_gb is not None and not pooled_gb.gate_b_pass:
        reasons.append("pooled OOS GATE-B (model-vs-baseline delta) did not pass")
    if not reasons:
        return "GO: pooled out-of-sample GATE-A and GATE-B pass across the forward folds."
    return "NO-GO: " + "; ".join(reasons)


def make_reaction_fold_scorer(
    outcomes: list[TradeOutcome],
    source_ids: Sequence[str],
    *,
    n_bootstrap: int = 2000,
    seed: int = 20260616,
) -> FoldScorer:
    """A fold scorer that keeps the reaction path's SOURCE + TIME-BLOCK clustered resampling.

    Inside each fold (and the pooled union) the GATE-A / GATE-B lower-95 bounds are the CLUSTERED
    bounds (`aats.backtest.reaction_gate`), so the in-fold CI still honours the two coupling channels
    (time bursts + per-source reputation) — an i.i.d. resample inside a fold would re-open exactly
    the variance-understatement the clustered bootstrap closes.  The scorer receives GLOBAL indices
    and slices the aligned outcomes + source_ids to the fold subset.
    """
    if len(source_ids) != len(outcomes):
        raise LicensingError(
            f"source_ids ({len(source_ids)}) must align with outcomes ({len(outcomes)})."
        )

    def _score(idx: Sequence[int]) -> tuple[GateAResult, GateBResult]:
        sub = [outcomes[i] for i in idx]
        sub_src = [source_ids[i] for i in idx]
        clusterings = build_reaction_clusterings(sub, sub_src)
        ga = clustered_gate_a(
            sub, clusterings, model=True, n_bootstrap=n_bootstrap, seed=seed
        )
        gb = clustered_gate_b_delta(
            sub, clusterings, n_bootstrap=n_bootstrap, seed=seed
        )
        return ga, gb

    return _score


def make_iid_fold_scorer(
    outcomes: list[TradeOutcome],
    *,
    n_bootstrap: int = 2000,
    seed: int = 20260616,
) -> FoldScorer:
    """A fold scorer using the shared i.i.d. gates (launch/momentum path — no source coupling).

    The launch/momentum corpora are not same-source-coupled, so the i.i.d. bound is defensible
    (methodology).  The scorer slices the outcomes to the fold subset and runs the shared
    `compute_gate_a` / `compute_gate_b_delta` (which carry the min-sample AND effective-sample floors).
    """

    def _score(idx: Sequence[int]) -> tuple[GateAResult, GateBResult]:
        sub = [outcomes[i] for i in idx]
        ga = compute_gate_a(sub, model=True, n_bootstrap=n_bootstrap, seed=seed)
        gb = compute_gate_b_delta(sub, n_bootstrap=n_bootstrap, seed=seed)
        return ga, gb

    return _score


__all__ = [
    "LICENSING_MIN_FOLDS",
    "DEFAULT_N_FOLDS",
    "DEFAULT_EMBARGO_FRAC",
    "LicensingError",
    "FoldScorer",
    "WalkForwardFold",
    "FoldScore",
    "LicensingResult",
    "build_purged_embargoed_folds",
    "assert_purge_is_load_bearing",
    "walk_forward_licensing",
    "make_reaction_fold_scorer",
    "make_iid_fold_scorer",
]
