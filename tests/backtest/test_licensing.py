"""Tests for the CAPITAL-LICENSING purged/embargoed WALK-FORWARD (aats.backtest.licensing).

What these prove (the capital-licensing hardening the verification demanded):
  1. FOLD DISCIPLINE — folds are forward-only, event-time-ordered, and cut into >= 5 folds; the
     PURGE is LOAD-BEARING (drops every decision whose label horizon spills past its fold boundary,
     non-vacuous) and the EMBARGO drops the boundary of each subsequent fold.
  2. PURGE SOUNDNESS — `assert_purge_is_load_bearing` RAISES on a planted cross-fold overlap (RED)
     and passes on a clean build (GREEN).
  3. FAIL-CLOSED — an empty corpus yields NO folds and a NO-GO with pooled gates None (no metric on
     no data), and misaligned metadata raises `LicensingError`.
  4. INSUFFICIENT FOLDS — fewer than LICENSING_MIN_FOLDS non-empty folds is a NO-GO (cannot license).
  5. REACTION CONTROLS — a genuine, time-spread OOS edge (winners the model follows + proven-loser
     sources it declines) LICENSES (GO); a pure-loser corpus does NOT (pooled GATE-A absolute-edge
     veto), even though the model beats its terrible baseline.
  6. CLUSTERED INSIDE EVERY FOLD — the pooled OOS statistic is the SOURCE+TIME-BLOCK clustered gate
     (parity with `reaction_gate`), not the i.i.d. gate.
  7. WIRING — `run_edge_proof(..., licensing=True)` runs the walk-forward, surfaces the pooled OOS +
     per-fold scoreboard, and a GO requires BOTH the in-sample pre-check AND the walk-forward.

Offline / deterministic. No RPC, no LLM, no network, no keypair, no capital.
"""

from __future__ import annotations

import json

import pytest

from aats.backtest.licensing import (
    DEFAULT_N_FOLDS,
    LICENSING_MIN_FOLDS,
    LicensingError,
    assert_purge_is_load_bearing,
    build_purged_embargoed_folds,
    make_iid_fold_scorer,
    make_reaction_fold_scorer,
    walk_forward_licensing,
)
from aats.backtest.reaction_gate import build_reaction_clusterings, clustered_gate_b_delta
from aats.backtest.reaction_harness import build_reaction_outcomes
from aats.backtest.run_edge_proof import (
    EXIT_GO,
    EXIT_NO_GO,
    STRATEGY_REACTION,
    run_edge_proof,
)
from aats.models.gate_b import TradeOutcome

_BASE = 1_700_000_000_000
_SPACE = 1_000_000  # 1000s apart >> the 300s max horizon: each prior outcome is observed in time

_WIN = {15: ("0.0011", "6000", 80, 20), 30: ("0.0015", "7000", 70, 30),
        60: ("0.0025", "8000", 60, 40), 120: ("0.0030", "9000", 50, 50),
        300: ("0.0033", "9000", 50, 50)}
_LOSE = {15: ("0.0009", "3000", 20, 80), 30: ("0.0006", "1500", 10, 90),
         60: ("0.0004", "800", 5, 95), 120: (None, None, 0, 0), 300: (None, None, 0, 0)}


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


def _interleaved_corpus(n_sources: int, n_each: int, *, win: bool, lose: bool) -> list[dict]:
    """Sources firing INTERLEAVED in time so every fold mixes sources.  `win`/`lose` pick which
    source families exist (both -> half winners, half losers)."""
    recs: list[dict] = []
    slot = 100
    t = 0
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


class _NullResolver:
    def resolve(self, sig: str) -> object:  # pragma: no cover - reaction never resolves via RPC
        raise AssertionError("reaction licensing must not call a block_time resolver")


# ---------------------------------------------------------------------------
# 1 + 2. FOLD DISCIPLINE + PURGE SOUNDNESS
# ---------------------------------------------------------------------------


def test_folds_are_forward_only_and_event_ordered():
    ev = list(range(0, 600, 10))          # 60 decisions at t=0,10,...,590
    hz = [t + 25 for t in ev]              # each label resolves ~2.5 decisions later
    folds = build_purged_embargoed_folds(ev, hz, n_folds=6)
    assert len(folds) == 6
    prev_end = None
    for f in folds:
        assert f.test_start_time <= f.test_end_time
        if prev_end is not None:
            assert f.test_start_time > prev_end  # strictly forward in event-time
        prev_end = f.test_end_time
        # every KEPT member's label horizon ends at/before this fold's end (purge held)
        for i in f.test_idx:
            assert hz[i] <= f.test_end_time


def test_purge_is_load_bearing_and_nonvacuous():
    ev = list(range(0, 600, 10))
    hz = [t + 25 for t in ev]
    folds = build_purged_embargoed_folds(ev, hz, n_folds=6)
    assert_purge_is_load_bearing(folds, hz)  # GREEN on a clean build
    # NON-VACUITY: the purge actually removed the trailing tail of every fold.
    assert sum(f.n_purged for f in folds) > 0
    for f in folds:
        assert f.n_purged >= 1  # the fold's last member always spills past its own end


def test_embargo_drops_the_boundary_of_later_folds():
    ev = list(range(0, 600, 10))
    hz = [t + 25 for t in ev]
    folds = build_purged_embargoed_folds(ev, hz, n_folds=6)
    assert folds[0].n_embargoed == 0  # no prior fold to embargo against
    assert sum(f.n_embargoed for f in folds[1:]) > 0  # later folds drop their embargoed head


def test_assert_purge_is_load_bearing_raises_on_planted_overlap():
    """RED: a fold that kept a member whose label horizon crosses its end is a PURGE LEAK."""
    ev = list(range(0, 100, 10))
    hz = [t + 5 for t in ev]
    folds = build_purged_embargoed_folds(ev, hz, n_folds=2)
    # Plant a leak: force a fold to keep a member whose horizon ends AFTER the fold end.
    bad = folds[0]
    tampered = type(bad)(
        window_id=bad.window_id,
        test_idx=list(bad.test_idx) + [len(ev) - 1],  # a late-horizon decision from a later fold
        test_start_time=bad.test_start_time,
        test_end_time=bad.test_end_time,
        n_raw=bad.n_raw,
        n_purged=bad.n_purged,
        n_embargoed=bad.n_embargoed,
        embargo=bad.embargo,
    )
    with pytest.raises(AssertionError, match="PURGE LEAK"):
        assert_purge_is_load_bearing([tampered], hz)


# ---------------------------------------------------------------------------
# 3. FAIL-CLOSED + input validation
# ---------------------------------------------------------------------------


def test_empty_corpus_is_fail_closed_no_go():
    res = walk_forward_licensing(
        [], [], [], fold_scorer=make_iid_fold_scorer([]), n_folds=6
    )
    assert res.licensing_go is False
    assert res.n_folds_built == 0
    assert res.pooled_gate_a is None and res.pooled_gate_b is None
    assert "no metric on no data" in res.reason.lower()


def test_misaligned_metadata_raises():
    with pytest.raises(LicensingError, match="align"):
        build_purged_embargoed_folds([1, 2, 3], [1, 2], n_folds=2)


def test_build_purged_embargoed_folds_rejects_zero_folds():
    with pytest.raises(LicensingError, match="n_folds"):
        build_purged_embargoed_folds([1, 2, 3], [2, 3, 4], n_folds=0)


# ---------------------------------------------------------------------------
# 4. INSUFFICIENT FOLDS -> NO-GO (cannot license out-of-sample stability)
# ---------------------------------------------------------------------------


def test_insufficient_folds_is_no_go():
    # 60 identical-delta model-wins outcomes, but only 3 folds requested -> below the >=5 floor.
    outs = [
        TradeOutcome(mint=f"m{i}", decision_slot=i, model_selected=False, baseline_selected=True,
                     net_pnl_lamports=-50_000_000, sol_at_risk_lamports=100_000_000)
        for i in range(60)
    ]
    ev = [o.decision_slot for o in outs]
    hz = [o.decision_slot + 1 for o in outs]  # tiny horizon (little purge) so folds stay populated
    res = walk_forward_licensing(
        outs, ev, hz, fold_scorer=make_iid_fold_scorer(outs, n_bootstrap=300), n_folds=3
    )
    assert res.n_folds_built < LICENSING_MIN_FOLDS
    assert res.folds_sufficient is False
    assert res.licensing_go is False
    assert "walk-forward fold" in res.reason


# ---------------------------------------------------------------------------
# 5. REACTION CONTROLS — genuine spread OOS edge GOes; pure losers do NOT
# ---------------------------------------------------------------------------


def _reaction_licensing(recs: list[dict], *, n_folds: int = 6, n_bootstrap: int = 400):
    outs, stats = build_reaction_outcomes(recs)
    scorer = make_reaction_fold_scorer(outs, stats.outcome_source_ids, n_bootstrap=n_bootstrap)
    res = walk_forward_licensing(
        outs,
        stats.outcome_event_times_ms,
        stats.outcome_label_horizon_end_ms,
        fold_scorer=scorer,
        n_folds=n_folds,
    )
    return outs, stats, res


def test_reaction_genuine_spread_edge_licenses_go():
    """POSITIVE CONTROL: winners the model follows + proven-loser sources it declines, spread across
    time so every fold has data. The pooled OOS GATE-A (absolute) AND GATE-B (delta) both pass across
    >= 5 forward folds -> the walk-forward LICENSES (GO). Proves the gate is not a blanket veto."""
    recs = _interleaved_corpus(16, 6, win=True, lose=True)  # 16 winner + 16 loser sources
    _outs, _stats, res = _reaction_licensing(recs)
    assert res.n_folds_built >= LICENSING_MIN_FOLDS
    assert res.pooled_gate_a.gate_a_pass is True   # model's selected cohort is net-of-cost positive
    assert res.pooled_gate_b.gate_b_pass is True   # and beats the baseline, effective-sample-gated
    assert res.pooled_gate_b.n_effective >= res.pooled_gate_b.effective_min_sample
    assert res.licensing_go is True


def test_reaction_pure_loser_corpus_does_not_license():
    """NEGATIVE CONTROL: a corpus of pure losers. The model beats its (terrible) baseline by declining
    proven losers (GATE-B may pass), but its OWN selected cohort still loses money net of cost, so the
    pooled GATE-A absolute-edge veto WITHHOLDS the license -> NO-GO. Absolute edge must hold too."""
    recs = _interleaved_corpus(30, 6, win=False, lose=True)
    _outs, _stats, res = _reaction_licensing(recs)
    assert res.n_folds_built >= LICENSING_MIN_FOLDS
    assert res.pooled_gate_a.gate_a_pass is False  # absolute net-of-cost edge is negative
    assert res.licensing_go is False
    assert "GATE-A" in res.reason


# ---------------------------------------------------------------------------
# 6. CLUSTERED INSIDE EVERY FOLD — pooled statistic is the source+time-block clustered gate
# ---------------------------------------------------------------------------


def test_pooled_statistic_uses_the_clustered_gate_not_iid():
    """The pooled OOS GATE-B lower bound is BYTE-IDENTICAL to calling the SOURCE+TIME-BLOCK clustered
    gate on the exact pooled subset — proving the walk-forward keeps the reaction clustered resampling
    inside the pooled statistic (an i.i.d. bound would differ)."""
    recs = _interleaved_corpus(16, 6, win=True, lose=True)
    outs, stats, res = _reaction_licensing(recs)
    # Reconstruct the pooled index set in the engine's fold order.
    pooled_idx: list[int] = []
    folds = build_purged_embargoed_folds(
        stats.outcome_event_times_ms, stats.outcome_label_horizon_end_ms, n_folds=DEFAULT_N_FOLDS
    )
    for f in folds:
        pooled_idx.extend(f.test_idx)
    sub = [outs[i] for i in pooled_idx]
    sub_src = [stats.outcome_source_ids[i] for i in pooled_idx]
    clus = clustered_gate_b_delta(sub, build_reaction_clusterings(sub, sub_src), n_bootstrap=400)
    assert res.pooled_gate_b.lower_95_bound == clus.lower_95_bound
    assert res.pooled_gate_b.delta == clus.delta


# ---------------------------------------------------------------------------
# 7. WIRING — run_edge_proof(..., licensing=True) runs the walk-forward, pre-check can veto
# ---------------------------------------------------------------------------


def _write(path, recs: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(r) for r in recs) + "\n", encoding="utf-8")


def test_run_edge_proof_licensing_go_wiring(tmp_path):
    recs = _interleaved_corpus(16, 6, win=True, lose=True)
    corpus = tmp_path / "reaction.jsonl"
    _write(corpus, recs)
    code, sb = run_edge_proof(
        corpus, block_time_resolver=_NullResolver(), strategy=STRATEGY_REACTION, licensing=True
    )
    assert sb["mode"].startswith("capital-licensing")
    assert sb["licensing_pre_check_pass"] is True   # the in-sample bootstrap pre-check passed...
    assert sb["licensing_go"] is True               # ...and the walk-forward licenses it
    assert sb["wf_n_folds_built"] >= LICENSING_MIN_FOLDS
    assert sb["wf_pooled_gate_a_pass"] is True
    assert sb["wf_pooled_gate_b_pass"] is True
    assert len(sb["wf_per_fold"]) == sb["wf_n_folds_built"]
    assert sb["verdict"] == "GO"
    assert code == EXIT_GO


def test_run_edge_proof_licensing_no_go_on_pure_losers(tmp_path):
    recs = _interleaved_corpus(30, 6, win=False, lose=True)
    corpus = tmp_path / "reaction.jsonl"
    _write(corpus, recs)
    code, sb = run_edge_proof(
        corpus, block_time_resolver=_NullResolver(), strategy=STRATEGY_REACTION, licensing=True
    )
    assert sb["licensing_go"] is False
    assert sb["verdict"] == "NO-GO"
    assert code == EXIT_NO_GO


def test_default_path_is_pre_check_only_not_a_capital_license(tmp_path):
    """The DEFAULT (no --licensing) path is the in-sample bootstrap PRE-CHECK ONLY — it never emits a
    licensing verdict, so it cannot license capital on its own. A GO in --licensing mode (proved in
    `test_run_edge_proof_licensing_go_wiring`) additionally requires the purged/embargoed walk-forward,
    so the licensing verdict can only ADD conditions to the pre-check, never loosen it.

    (This runs only the CHEAP in-sample path — the expensive walk-forward is exercised by the GO/NO-GO
    wiring tests above.)"""
    recs = _interleaved_corpus(16, 6, win=True, lose=True)
    corpus = tmp_path / "reaction.jsonl"
    _write(corpus, recs)
    _code, pre = run_edge_proof(
        corpus, block_time_resolver=_NullResolver(), strategy=STRATEGY_REACTION, licensing=False
    )
    assert pre["mode"].startswith("in-sample")
    assert "licensing_go" not in pre  # the default path does NOT license capital
    assert "wf_pooled_gate_b" not in pre  # ...and never ran the walk-forward
