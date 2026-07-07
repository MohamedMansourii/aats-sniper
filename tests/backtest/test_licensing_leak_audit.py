"""BACKTEST-QA adversarial leak/edge audit for the CAPITAL-LICENSING walk-forward + the
effective-sample floor (QA-owned regression, independent of the strategy author's suite).

These are the SKEPTICAL VALIDATOR's tests. They go beyond `test_licensing.py` /
`test_gate_b.py` by trying to DEFEAT the guards, on the four fronts the G4 leak/edge audit
must clear before a future real-data GO could license capital:

  (1) CROSS-FOLD LEAKAGE — on a corpus whose labels GENUINELY overlap adjacent folds
      (spacing << horizon), prove NO surviving (post purge+embargo) member of an earlier
      fold has a label horizon that reaches into a later fold's surviving members, AND prove
      the purge is LOAD-BEARING (a naive no-purge builder leaks on the same metadata).
  (2) EMBARGO IS REAL — the embargo actually drops boundary rows (default vs embargo=0 differ);
      it is not an inert constant.
  (3) EFFECTIVE-SAMPLE FLOOR — the n=497->4,187 reversal mechanism holds NOT ONLY in the
      i.i.d. gate (covered in test_gate_b.py) but ALSO in the CLUSTERED reaction gate and in
      the POOLED licensing statistic: a thin ~8-20 model!=baseline cohort diluted in thousands
      of both-take trades is WITHHELD; a sufficient cohort with the same edge licenses (proving
      the floor is a discriminator, not a blanket veto); the floor cannot be lowered by a caller.
  (4) FAIL-CLOSED ON EMPTY HOLDS — an all-purged (every fold empty) build, an empty outcome
      list, and an empty reaction corpus all fail closed (NO-GO / FAIL-CLOSED, pooled None),
      never a fabricated pass.

Plus a walk-forward REPUTATION adversarial: a source's FUTURE monster-winner outcome can never
raise an EARLIER same-source decision's reputation (no future informs the past), and a
same-block_time same-source tie is refused (strict-prior only).

Offline / deterministic. No RPC, no LLM, no network, no keypair, no capital. Small bootstrap
counts keep the clustered/pooled paths fast while preserving the pass/fail verdict.
"""

from __future__ import annotations

import json
import os
import tempfile

import pytest

from aats.backtest.licensing import (
    LICENSING_MIN_FOLDS,
    assert_purge_is_load_bearing,
    build_purged_embargoed_folds,
    make_iid_fold_scorer,
    make_reaction_fold_scorer,
    walk_forward_licensing,
)
from aats.backtest.reaction_gate import (
    build_reaction_clusterings,
    clustered_gate_b_delta,
)
from aats.backtest.reaction_harness import (
    EXIT_MODEL_REALIZABLE,
    REACTION_PARAMS,
    _resolve_one,
    _walk_forward_reputations,
    build_reaction_outcomes,
)
from aats.backtest.outcome_harness import build_round_trip_cost_stack
from aats.backtest.run_edge_proof import (
    EXIT_FAIL_CLOSED,
    STRATEGY_REACTION,
    run_edge_proof,
)
from aats.contracts.risk import RiskConfig
from aats.models.gate_b import TradeOutcome

SOL = 1_000_000_000

_BASE = 1_700_000_000_000
_SPACE = 1_000_000  # 1000s apart >> the 300s max horizon: each prior outcome observed in time

_WIN = {15: ("0.0011", "6000", 80, 20), 30: ("0.0015", "7000", 70, 30),
        60: ("0.0025", "8000", 60, 40), 120: ("0.0030", "9000", 50, 50),
        300: ("0.0033", "9000", 50, 50)}
_LOSE = {15: ("0.0009", "3000", 20, 80), 30: ("0.0006", "1500", 10, 90),
         60: ("0.0004", "800", 5, 95), 120: (None, None, 0, 0), 300: (None, None, 0, 0)}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _rec(mint: str, source: str, *, bt: int, slot: int, marks: dict) -> dict:
    fwd = [
        {"horizon_s": h, "price_sol": m[0], "liquidity_usd": m[1],
         "txns_m5": {"buys": m[2], "sells": m[3]}, "vol_m5": None,
         "obs_wall_ms": bt + h * 1000, "price_usd": None}
        for h, m in sorted(marks.items())
    ]
    return {"signal_type": "whale_buy", "source_id": source, "mint": mint, "signal_slot": slot,
            "signal_block_time_ms": bt, "signal_price_sol": "0.001", "signal_size_sol": "5.0",
            "source_prior": None, "forward": fwd}


def _interleaved_reaction_corpus(n_sources: int, n_each: int, *, win: bool, lose: bool) -> list[dict]:
    recs: list[dict] = []
    slot, t = 100, 0
    for i in range(n_each):
        for s in range(n_sources):
            if win:
                recs.append(_rec(f"W{s}_{i}", f"W{s}", bt=_BASE + t * _SPACE, slot=slot, marks=_WIN))
                slot += 1
                t += 1
            if lose:
                recs.append(_rec(f"L{s}_{i}", f"L{s}", bt=_BASE + t * _SPACE, slot=slot, marks=_LOSE))
                slot += 1
                t += 1
    return recs


def _thin_effective(n_both_take: int, n_effective: int) -> list[TradeOutcome]:
    """Many both-take trades (cancel in the delta) wrapped around a THIN model-declines-loser
    cohort that carries the ENTIRE positive delta — the exact dilution shape the total-n floor is
    blind to and the effective floor must catch (the n=497->4,187 reversal)."""
    out: list[TradeOutcome] = []
    slot = 0
    for i in range(n_both_take):
        out.append(TradeOutcome(f"both{i}", slot, True, True, SOL // 50, SOL // 10))
        slot += 1
    for i in range(n_effective):
        out.append(TradeOutcome(f"eff{i}", slot, False, True, -SOL // 2, SOL // 10))
        slot += 1
    return out


def _naive_no_purge_folds(event_times, n_folds):
    """The LEAKY reference: contiguous forward chunks with NO purge and NO embargo (keep every
    member). The purged builder must strictly beat this — the load-bearing proof."""
    n = len(event_times)
    order = sorted(range(n), key=lambda i: (event_times[i], i))
    base, rem = divmod(n, n_folds)
    out, start = [], 0
    for f in range(n_folds):
        size = base + (1 if f < rem else 0)
        out.append(order[start:start + size])
        start += size
    return out


class _NullResolver:
    def resolve(self, sig: str) -> object:  # pragma: no cover - reaction never resolves via RPC
        raise AssertionError("reaction licensing must not call a block_time resolver")


# ===========================================================================
# (1) CROSS-FOLD LEAKAGE — the #1 audit ask
# ===========================================================================

# A corpus whose LABELS GENUINELY OVERLAP adjacent folds: spacing 30, horizon 300 => every
# decision's label reaches ~10 decisions ahead, straddling several fold boundaries. On the
# interleaved reaction FIXTURES the spacing (1e6 ms) dwarfs the horizon (3e5 ms) so the purge
# there is nearly vacuous for cross-fold overlap; THIS synthetic exercises the real overlap.
_OVERLAP_EV = [i * 30 for i in range(120)]
_OVERLAP_HZ = [t + 300 for t in _OVERLAP_EV]


def test_cross_fold_no_kept_label_reaches_a_later_fold():
    """PURGED build: no SURVIVING member of an earlier fold has a label horizon that reaches the
    event-time of ANY surviving member of a later fold (the pooled OOS union is cross-fold
    decoupled — the property that makes the pooled bound an honest OOS statistic, not an
    in-sample echo)."""
    folds = build_purged_embargoed_folds(_OVERLAP_EV, _OVERLAP_HZ, n_folds=6)
    assert len(folds) == 6
    assert_purge_is_load_bearing(folds, _OVERLAP_HZ)  # every kept label <= its own fold end
    kept = [[(i, _OVERLAP_EV[i], _OVERLAP_HZ[i]) for i in f.test_idx] for f in folds]
    # every fold actually kept AND actually purged members (non-vacuous on a real-overlap corpus)
    assert all(len(k) > 0 for k in kept)
    assert all(f.n_purged > 0 for f in folds)
    for a in range(len(folds)):
        for b in range(a + 1, len(folds)):
            if not kept[a] or not kept[b]:
                continue
            max_label_a = max(h for _, _, h in kept[a])
            min_event_b = min(e for _, e, _ in kept[b])
            assert max_label_a < min_event_b, (
                f"CROSS-FOLD LEAK: fold {a} kept a label ending {max_label_a} that reaches "
                f"fold {b}'s earliest surviving decision at {min_event_b}"
            )


def test_purge_is_load_bearing_naive_builder_leaks_on_same_metadata():
    """NEGATIVE CONTROL: a naive no-purge/no-embargo builder LEAKS across folds on the SAME
    metadata the purged builder cleans — proving the purge (not the corpus) is what removes the
    overlap. If this ever stops leaking, the purge test above is vacuous."""
    naive = _naive_no_purge_folds(_OVERLAP_EV, 6)
    leaks = 0
    for a in range(len(naive)):
        for b in range(a + 1, len(naive)):
            max_label_a = max(_OVERLAP_HZ[i] for i in naive[a])
            min_event_b = min(_OVERLAP_EV[i] for i in naive[b])
            if max_label_a >= min_event_b:
                leaks += 1
    assert leaks > 0, "the no-purge negative control must leak, else the purge test is vacuous"
    # and the fold-0 tail that leaks is exactly what the purge removes
    f0_end = max(_OVERLAP_EV[i] for i in naive[0])
    assert sum(1 for i in naive[0] if _OVERLAP_HZ[i] > f0_end) > 0


def test_cross_fold_decoupled_on_real_reaction_metadata():
    """END-TO-END: on the REAL reaction harness metadata (on-chain ms anchors + real label
    horizons = signal_block_time + max_horizon*1000), the purge holds and the pooled OOS union is
    cross-fold decoupled. Uses the exact `outcome_event_times_ms` / `outcome_label_horizon_end_ms`
    the runner feeds the walk-forward."""
    recs = _interleaved_reaction_corpus(16, 6, win=True, lose=True)
    _outs, stats = build_reaction_outcomes(recs)
    ev = list(stats.outcome_event_times_ms)
    hz = list(stats.outcome_label_horizon_end_ms)
    # label horizon must strictly exceed the event anchor (a real forward window), else the purge
    # would be vacuous by construction.
    assert all(h > e for e, h in zip(ev, hz))
    folds = build_purged_embargoed_folds(ev, hz, n_folds=6)
    assert_purge_is_load_bearing(folds, hz)
    kept = [[(i, ev[i], hz[i]) for i in f.test_idx] for f in folds]
    for a in range(len(folds)):
        for b in range(a + 1, len(folds)):
            if not kept[a] or not kept[b]:
                continue
            assert max(h for _, _, h in kept[a]) < min(e for _, e, _ in kept[b])


# ===========================================================================
# (2) EMBARGO IS REAL — not an inert constant
# ===========================================================================


def test_embargo_is_active_not_inert():
    """The embargo genuinely drops boundary rows: the default (span-fraction) embargo removes
    strictly more than embargo=0, and the surviving sets differ. A no-op embargo would be a silent
    weakening of the decoupling."""
    default = build_purged_embargoed_folds(_OVERLAP_EV, _OVERLAP_HZ, n_folds=6)
    zero = build_purged_embargoed_folds(_OVERLAP_EV, _OVERLAP_HZ, n_folds=6, embargo=0)
    assert default[0].embargo > 0
    assert sum(f.n_embargoed for f in default) > 0
    assert sum(f.n_embargoed for f in zero) == 0
    assert [len(f.test_idx) for f in default] != [len(f.test_idx) for f in zero]


def test_embargo_boundary_tie_is_dropped_even_at_zero_width():
    """Adversarial tie: a later-fold decision at the EXACT prior-fold boundary time is embargoed
    even with embargo=0 (the condition is `event_time <= prev_end + embargo`, and prev_end is the
    prior fold's last event time). This closes the one measure-zero overlap the purge alone would
    leave on tied anchors."""
    # two decisions share the boundary anchor 50; horizon 5 so purge keeps most members.
    ev = [10, 20, 30, 40, 50, 50, 60, 70, 80, 90]
    hz = [t + 5 for t in ev]
    folds = build_purged_embargoed_folds(ev, hz, n_folds=2, embargo=0)
    # the fold-1 member sharing fold-0's boundary time (50) must not survive into fold 1.
    boundary = folds[0].test_end_time
    for i in folds[1].test_idx:
        assert ev[i] > boundary


# ===========================================================================
# (3) EFFECTIVE-SAMPLE FLOOR — clustered gate + pooled licensing (n=497->4,187)
# ===========================================================================


def test_effective_floor_holds_in_clustered_reaction_gate():
    """The thin-cohort floor is enforced in the CLUSTERED reaction GATE-B (not just the i.i.d.
    gate): 15 model!=baseline decisions diluted in 200 both-take trades is WITHHELD even though the
    point delta is positive."""
    outs = _thin_effective(n_both_take=200, n_effective=15)
    src = [f"S{i % 8}" for i in range(len(outs))]
    gb = clustered_gate_b_delta(outs, build_reaction_clusterings(outs, src), n_bootstrap=300)
    assert gb.n_effective == 15
    assert gb.delta > 0
    # LOAD-BEARING PROOF: the clustered bound itself is POSITIVE and the total-n guard is satisfied,
    # so the effective floor is the SOLE thing withholding the pass (not a redundant guard behind an
    # already-failing bound). This is the n=497->4,187 reversal: a thin de-risk cohort manufactures a
    # spurious lower95 > 0 that only the effective floor catches.
    assert gb.lower_95_bound > 0
    assert gb.min_sample_met is True
    assert gb.effective_sample_met is False
    assert gb.gate_b_pass is False


def test_effective_floor_holds_in_pooled_licensing_statistic():
    """The n=497->4,187 reversal at the POOLED level: a thin de-risk cohort diluted in thousands of
    both-take trades does NOT license, even though the pooled i.i.d. delta bound clears zero. The
    pooled GATE-B inherits the effective floor, so licensing is WITHHELD."""
    outs = _thin_effective(n_both_take=4000, n_effective=15)
    ev = [o.decision_slot for o in outs]
    hz = [o.decision_slot + 1 for o in outs]  # tiny horizon so folds stay populated
    res = walk_forward_licensing(
        outs, ev, hz, fold_scorer=make_iid_fold_scorer(outs, n_bootstrap=300), n_folds=6
    )
    assert res.n_folds_built >= LICENSING_MIN_FOLDS
    assert res.pooled_gate_b.n_effective < 21          # pooled effective cohort is thin
    # LOAD-BEARING: the pooled bound is positive and the total-n guard is met, so ONLY the effective
    # floor blocks the license — remove it and this thin cohort would license capital.
    assert res.pooled_gate_b.lower_95_bound > 0
    assert res.pooled_gate_b.min_sample_met is True
    assert res.pooled_gate_b.effective_sample_met is False
    assert res.licensing_go is False
    assert "GATE-B" in res.reason


def test_sufficient_effective_cohort_licenses_positive_control():
    """DISCRIMINATOR proof: the SAME edge on a SUFFICIENT effective cohort (48 model-declines-loser
    decisions) diluted in both-take winners DOES license — pooled GATE-A (absolute) and GATE-B
    (delta, effective-gated) both pass. Proves the floor withholds thin cohorts, it is not a
    blanket veto that would spuriously block genuine edge."""
    outs = _thin_effective(n_both_take=150, n_effective=48)
    ev = [o.decision_slot for o in outs]
    hz = [o.decision_slot + 1 for o in outs]
    res = walk_forward_licensing(
        outs, ev, hz, fold_scorer=make_iid_fold_scorer(outs, n_bootstrap=400), n_folds=6
    )
    assert res.pooled_gate_b.n_effective >= 21
    assert res.pooled_gate_b.effective_sample_met is True
    assert res.pooled_gate_a.gate_a_pass is True
    assert res.pooled_gate_b.gate_b_pass is True
    assert res.licensing_go is True


def test_pooled_licensing_floor_cannot_be_defeated_by_reaction_scorer_default():
    """The reaction fold scorer uses the DEFAULT effective floor (>= 21 after the hard clamp): a
    thin 12-effective clustered pooled cohort cannot license. The licensing path exposes no knob to
    lower the floor, and the underlying floor is clamp-protected."""
    outs = _thin_effective(n_both_take=300, n_effective=12)
    src = [f"S{i % 10}" for i in range(len(outs))]
    ev = [o.decision_slot for o in outs]
    hz = [o.decision_slot + 1 for o in outs]
    scorer = make_reaction_fold_scorer(outs, src, n_bootstrap=250)
    res = walk_forward_licensing(outs, ev, hz, fold_scorer=scorer, n_folds=6)
    assert res.pooled_gate_b.effective_min_sample >= 21
    assert res.pooled_gate_b.effective_sample_met is False
    assert res.licensing_go is False


# ===========================================================================
# (4) FAIL-CLOSED ON EMPTY HOLDS
# ===========================================================================


def test_all_folds_empty_after_purge_is_fail_closed_no_go():
    """If EVERY fold is emptied by the purge (a uniform horizon larger than every fold's own time
    span), the pooled union is empty -> pooled gates None -> NO-GO. No metric on no data; never a
    fabricated pass."""
    outs = _thin_effective(n_both_take=40, n_effective=20)  # 60 outcomes
    ev = list(range(60))
    hz = [t + 10_000 for t in ev]  # horizon >> corpus span -> every member's label spills its fold
    res = walk_forward_licensing(
        outs, ev, hz, fold_scorer=make_iid_fold_scorer(outs, n_bootstrap=200), n_folds=6
    )
    assert res.n_folds_built == 0
    assert res.pooled_n == 0
    assert res.pooled_gate_a is None and res.pooled_gate_b is None
    assert res.licensing_go is False


def test_empty_outcomes_licensing_is_fail_closed():
    """An empty outcome list fails closed with a NO-GO and pooled gates None (parity with the
    reaction empty-corpus path)."""
    res = walk_forward_licensing([], [], [], fold_scorer=make_iid_fold_scorer([]), n_folds=6)
    assert res.licensing_go is False
    assert res.pooled_gate_a is None and res.pooled_gate_b is None
    assert "no metric on no data" in res.reason.lower()


def test_reaction_empty_corpus_exits_fail_closed():
    """The runnable proof fails closed (EXIT_FAIL_CLOSED) on an empty reaction corpus — never a
    fabricated GO. This is the exact fail-closed the earlier real-data NO-GO relied on."""
    d = tempfile.mkdtemp()
    empty = os.path.join(d, "empty.jsonl")
    open(empty, "w").close()
    code, sb = run_edge_proof(empty, block_time_resolver=_NullResolver(), strategy=STRATEGY_REACTION)
    assert code == EXIT_FAIL_CLOSED
    assert sb["verdict"].startswith("FAIL-CLOSED")


def test_reaction_all_censored_corpus_exits_fail_closed(tmp_path):
    """A corpus of only UNUSABLE records (null anchor) resolves nothing -> fail closed, not a
    fabricated pass on zero data."""
    bad = _rec("A", "S", bt=_BASE, slot=1, marks=_WIN)
    bad["signal_block_time_ms"] = None
    corpus = tmp_path / "bad.jsonl"
    corpus.write_text(json.dumps(bad) + "\n", encoding="utf-8")
    code, sb = run_edge_proof(corpus, block_time_resolver=_NullResolver(), strategy=STRATEGY_REACTION)
    assert code == EXIT_FAIL_CLOSED
    assert sb["n_resolved"] == 0


# ===========================================================================
# WALK-FORWARD REPUTATION — no future informs the past (defeat attempts)
# ===========================================================================


def _resolve_group(recs: list[dict]):
    resolved = []
    for o in recs:
        resolved.append(
            _resolve_one(
                o,
                params=REACTION_PARAMS,
                cost_stack=build_round_trip_cost_stack(),
                risk_config=RiskConfig(),
                exit_model=EXIT_MODEL_REALIZABLE,
            )
        )
    resolved.sort(key=lambda r: (r.signal.signal_block_time_ms, r.signal.signal_slot, r.signal.mint))
    by_source: dict[str, list] = {}
    for r in resolved:
        by_source.setdefault(r.signal.source_id, []).append(r)
    return resolved, by_source


def test_future_monster_winner_cannot_raise_an_earlier_reputation():
    """A source that is a LOSER early and a MONSTER WINNER later: every EARLY decision's
    walk-forward reputation reflects ONLY the prior losses (<= 0), never the future win. Proven on
    the production accumulator (`_walk_forward_reputations`), decision by decision."""
    recs = [
        _rec(f"S_{i}", "S", bt=_BASE + i * _SPACE, slot=100 + i, marks=_LOSE if i < 4 else _WIN)
        for i in range(6)
    ]
    resolved, by_source = _resolve_group(recs)
    reps = _walk_forward_reputations(resolved, by_source)
    # decision 0 rides the neutral prior; decisions 1..4 see only the prior LOSERS (rep <= 0),
    # even decision 4 which is itself a winner — its reputation cannot see its own or future wins.
    assert reps[0][0] is None and reps[0][1] == 0
    for i in range(1, 5):
        rep, n_prior, _ = reps[i]
        assert rep is not None and rep <= 0, f"decision {i} leaked a future winner into reputation"
        assert n_prior == i  # exactly the strictly-prior, already-observed same-source outcomes


def test_same_block_time_same_source_tie_does_not_inform_a_decision():
    """Four same-source signals at the IDENTICAL on-chain anchor: strict-prior (`<`) refuses every
    same-instant sibling, so NONE informs another. All ride the neutral prior (no reputation gate
    fires, no decline) — no same-block leak."""
    recs = [_rec(f"T{i}", "S", bt=_BASE, slot=100 + i, marks=_LOSE) for i in range(4)]
    outs, stats = build_reaction_outcomes(recs)
    assert stats.n_resolved == 4
    assert stats.n_reputation_gated == 0
    assert stats.n_declined_by_model == 0
    assert all(o.model_selected for o in outs)
