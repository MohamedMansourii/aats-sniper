"""BACKTEST-QA adversarial leak audit for the reaction/front-run harness (QA-owned regression).

These are the skeptical validator's tests, independent of the strategy author's own suite. They go
BEYOND `test_reaction_harness.py` by (a) proving a signal's OWN forward can never reach its OWN
decision via a full-corpus mutation sweep, (b) proving a source's FUTURE huge-winner signal cannot
rescue an EARLIER decision (the subtle walk-forward leak), (c) NEUTERING each structural guard to
prove it is load-bearing (the leak passes silently once the guard is gone), (d) proving the anchor
is the on-chain block_time (scrambling wall-clock is inert), and (e) determinism / fail-closed.

Deterministic. No RPC, no LLM, no network, no keypair, no capital.
"""
from __future__ import annotations

import copy
import hashlib
from decimal import Decimal

import pytest

from aats.backtest import reaction_harness as rh
from aats.backtest.outcome_harness import LeakError, PITFeature
from aats.backtest.reaction_harness import (
    REACTION_PARAMS,
    ReactionSignal,
    ReputationInput,
    ReputationLeakError,
    assert_reputation_inputs_prior,
    build_reaction_outcomes,
    decide_reaction_entry,
    source_reputation,
)
from aats.contracts.risk import RiskConfig

_BASE = 1_700_000_000_000
_SPACING = 1_000_000  # >> max horizon (300s) so each prior outcome is observed before the next

_WIN = {15: ("0.0011", "6000", 80, 20), 30: ("0.0015", "7000", 70, 30),
        60: ("0.0025", "8000", 60, 40), 120: ("0.0030", "9000", 50, 50),
        300: ("0.0033", "9000", 50, 50)}
_LOSE = {15: ("0.0009", "3000", 20, 80), 30: ("0.0006", "1500", 10, 90),
         60: ("0.0004", "800", 5, 95), 120: (None, None, 0, 0), 300: (None, None, 0, 0)}
_MONSTER = {15: ("0.05", "90000", 99, 1), 30: ("0.10", "90000", 99, 1),
            60: ("0.20", "90000", 99, 1), 120: ("0.40", "90000", 99, 1),
            300: ("0.80", "90000", 99, 1)}


def _rec(mint, source, *, bt, slot, price="0.001", marks=_LOSE):
    forward = [{"horizon_s": h, "price_sol": m[0], "liquidity_usd": m[1],
                "txns_m5": {"buys": m[2], "sells": m[3]}, "vol_m5": None,
                "obs_wall_ms": bt + h * 1000, "price_usd": None}
               for h, m in sorted(marks.items())]
    return {"signal_type": "whale_buy", "source_id": source, "mint": mint, "signal_slot": slot,
            "signal_block_time_ms": bt, "signal_price_sol": price, "signal_size_sol": "5.0",
            "source_prior": None, "forward": forward}


def _series(source, *, n, marks, slot0, bt0):
    return [_rec(f"{source}_{i}", source, bt=bt0 + i * _SPACING, slot=slot0 + i, marks=marks)
            for i in range(n)]


def _signal(bt=_BASE, source="S"):
    return ReactionSignal("M", source, "whale_buy", 1, bt, Decimal("0.001"), Decimal("5"), None)


# ---------------------------------------------------------------------------
# BOUNDARY 1 — a signal's OWN forward can never reach its OWN decision
# ---------------------------------------------------------------------------
def test_b1_own_forward_never_reaches_own_decision_full_sweep():
    """For EVERY record, replacing ONLY that record's forward with a monster winner leaves that
    record's OWN model/baseline mask byte-identical: a decision cannot see its own outcome."""
    corpus = _series("BAD", n=6, marks=_LOSE, slot0=100, bt0=_BASE) + \
        _series("GOOD", n=6, marks=_WIN, slot0=200, bt0=_BASE + 50_000_000)
    base_mask = {o.mint: (o.model_selected, o.baseline_selected)
                 for o in build_reaction_outcomes(corpus)[0]}
    for i in range(len(corpus)):
        mutated = copy.deepcopy(corpus)
        mutated[i]["forward"] = _rec("x", "x", bt=mutated[i]["signal_block_time_ms"], slot=0,
                                     marks=_MONSTER)["forward"]
        mine = next(o for o in build_reaction_outcomes(mutated)[0]
                    if o.mint == corpus[i]["mint"])
        assert (mine.model_selected, mine.baseline_selected) == base_mask[corpus[i]["mint"]]


def test_b1_forward_feature_guard_is_load_bearing(monkeypatch):
    """A forward-derived decision feature RAISES LeakError; NEUTERING the guard lets it slip."""
    sig = _signal()
    leaked = PITFeature("LEAK_fwd@60s", 0.0025, sig.signal_block_time_ms + 60_000)
    with pytest.raises(LeakError, match="forward"):
        decide_reaction_entry(sig, reputation=None, n_prior_signals=0, reputation_audit_summary="x",
                              params=REACTION_PARAMS, risk_config=RiskConfig(),
                              extra_features=(leaked,))
    # NEUTER the guard -> the identical leak now passes silently (proves the guard catches it).
    monkeypatch.setattr(rh, "assert_features_leq_decision", lambda feats, cutoff: "NEUTERED")
    d = decide_reaction_entry(sig, reputation=None, n_prior_signals=0, reputation_audit_summary="x",
                              params=REACTION_PARAMS, risk_config=RiskConfig(),
                              extra_features=(leaked,))
    assert d.leak_audit_summary == "NEUTERED"  # leak slipped through the neutered guard


# ---------------------------------------------------------------------------
# BOUNDARY 2 — a source's FUTURE outcome can never inform an EARLIER decision
# ---------------------------------------------------------------------------
def test_b2_future_monster_winner_cannot_rescue_earlier_decision():
    """A proven-loser source's later signals are declined; making the LAST (future) signal a
    monster winner does NOT flip any earlier decision (no future outcome informs the past)."""
    loser_future = _series("S", n=5, marks=_LOSE, slot0=100, bt0=_BASE)
    mask_a = {o.mint: o.model_selected for o in build_reaction_outcomes(loser_future)[0]}

    monster_future = copy.deepcopy(loser_future)
    monster_future[4]["forward"] = _rec("x", "x", bt=monster_future[4]["signal_block_time_ms"],
                                        slot=0, marks=_MONSTER)["forward"]
    mask_b = {o.mint: o.model_selected for o in build_reaction_outcomes(monster_future)[0]}

    for i in range(4):  # earlier signals unchanged by the future monster winner
        assert mask_a[f"S_{i}"] == mask_b[f"S_{i}"]
    assert mask_a["S_3"] is False and mask_b["S_3"] is False  # declined in both


def test_b2_reputation_guard_is_load_bearing(monkeypatch):
    """The strictly-prior guard RAISES on a future outcome; NEUTERING it leaks a positive mean
    (a would-be decline flips to a follow), proving the guard is what prevents the leak."""
    prior = ReputationInput("S", _BASE - _SPACING, _BASE - 700_000, Decimal("-0.5"))  # observed loss
    future = ReputationInput("S", _BASE + _SPACING, _BASE + _SPACING + 300_000, Decimal("5.0"))
    with pytest.raises(ReputationLeakError):
        assert_reputation_inputs_prior([prior, future], _BASE)
    rep_clean, n_clean, _ = source_reputation([prior], _BASE)
    assert rep_clean == Decimal("-0.5") and n_clean == 1

    monkeypatch.setattr(rh, "assert_reputation_inputs_prior", lambda inputs, dbt: "NEUTERED")
    rep_leaky, _, _ = source_reputation([prior, future], _BASE)
    assert rep_leaky is not None and rep_leaky > 0  # future winner leaked the mean positive


def test_b2_same_block_time_tie_is_not_prior():
    """A same-source outcome at the IDENTICAL anchor is refused (strict-prior only, no same-instant
    leak)."""
    tie = ReputationInput("S", _BASE, _BASE - 10, Decimal("9.0"))
    with pytest.raises(ReputationLeakError):
        assert_reputation_inputs_prior([tie], _BASE)


# ---------------------------------------------------------------------------
# ANCHOR — on-chain block_time, never wall-clock
# ---------------------------------------------------------------------------
def test_anchor_is_block_time_not_wall_clock():
    """Scrambling every forward obs_wall_ms leaves net PnL AND selection masks byte-identical, and
    the build is input-order-independent (sorted by on-chain block_time/slot)."""
    corpus = _series("BAD", n=5, marks=_LOSE, slot0=100, bt0=_BASE) + \
        _series("GOOD", n=5, marks=_WIN, slot0=200, bt0=_BASE + 50_000_000)
    base_out = build_reaction_outcomes(corpus)[0]
    scrambled = copy.deepcopy(corpus)
    for r in scrambled:
        for f in r["forward"]:
            f["obs_wall_ms"] = 42
    scr_out = build_reaction_outcomes(scrambled)[0]
    assert [o.net_pnl_lamports for o in base_out] == [o.net_pnl_lamports for o in scr_out]
    assert [(o.model_selected, o.baseline_selected) for o in base_out] == \
        [(o.model_selected, o.baseline_selected) for o in scr_out]

    import random
    shuffled = corpus[:]
    random.Random(7).shuffle(shuffled)
    sh_out = build_reaction_outcomes(shuffled)[0]
    assert {o.mint: o.model_selected for o in base_out} == \
        {o.mint: o.model_selected for o in sh_out}


# ---------------------------------------------------------------------------
# DETERMINISM / MONEY / REALIZABLE-DEFAULT
# ---------------------------------------------------------------------------
def test_determinism_and_money_int_and_realizable_default():
    corpus = _series("GOOD", n=4, marks=_WIN, slot0=200, bt0=_BASE)
    real_out = build_reaction_outcomes(corpus)[0]  # default = realizable
    spot_out = build_reaction_outcomes(corpus, exit_model="spot")[0]
    assert sum(o.net_pnl_lamports for o in real_out) <= sum(o.net_pnl_lamports for o in spot_out)
    # Strict `type() is int` (NOT isinstance) on purpose: money must be a true int, never a bool
    # (bool is an int subclass) — the money-discipline invariant. noqa E721 keeps that strictness.
    assert all(type(o.net_pnl_lamports) is int and type(o.sol_at_risk_lamports) is int  # noqa: E721
               for o in real_out)
    o2 = build_reaction_outcomes(corpus)[0]
    assert [o.net_pnl_lamports for o in real_out] == [o.net_pnl_lamports for o in o2]


def test_entry_latency_haircut_reaches_outcome_and_is_monotone_in_prod_regime(monkeypatch):
    """The entry-latency haircut REACHES the OUTCOME via the MODULE constant, and from the frozen
    value UPWARD a larger haircut lowers realized PnL (the production-relevant regime).

    QA FINDINGS documented here (both filed against the strategy engineer):
      1. `build_reaction_outcomes(params=...)` does NOT route the OUTCOME entry-latency haircut —
         PASS-1 resolution (`_resolve_one`) reads the MODULE `REACTION_PARAMS`, so the `params`
         argument's `entry_latency_haircut_bps` is OUTCOME-INERT (only the decision path honors it).
         The author's `test_haircut_lowers_realized_pnl_vs_zero_haircut` passes `params=` and is
         therefore VACUOUS (compares two identical PnLs). This test patches the module constant that
         `_resolve_one` actually consumes.
      2. The documented invariant "the haircut can only LOWER realized PnL, never fabricate a GO" is
         NOT strictly true: the haircut uniformly SCALES the R-multiple path fed to a threshold-based
         exit engine, so PnL is NON-MONOTONIC (a 0bps->50bps step can RAISE PnL by de-aligning
         round-number marks from exit rungs). At the frozen 50bps, applied identically to model and
         baseline on real (non-round) prices, the effect is small and unbiased across cohorts — not a
         leak, not a GATE-B bias — but the invariant is overclaimed. This test asserts only the ROBUST
         production-regime property (monotone decreasing FROM the frozen value upward)."""
    import dataclasses

    corpus = _series("GOOD", n=4, marks=_WIN, slot0=200, bt0=_BASE)
    frozen = dataclasses.replace(REACTION_PARAMS, entry_latency_haircut_bps=50, params_hash="qa")
    heavy = dataclasses.replace(REACTION_PARAMS, entry_latency_haircut_bps=500, params_hash="qa")
    monkeypatch.setattr(rh, "REACTION_PARAMS", frozen)
    frozen_net = sum(o.net_pnl_lamports for o in build_reaction_outcomes(corpus)[0])
    monkeypatch.setattr(rh, "REACTION_PARAMS", heavy)
    heavy_net = sum(o.net_pnl_lamports for o in build_reaction_outcomes(corpus)[0])
    assert heavy_net < frozen_net  # 500bps < 50bps: the haircut reaches the outcome & is monotone up


def test_params_arg_haircut_now_reaches_outcome_after_fix():
    """FIX VERIFICATION of filed defect #1 (was: `test_params_arg_haircut_is_outcome_inert_...`).

    The engineer routed `params` through PASS-1 (`_resolve_one(..., params=p)`), so the OUTCOME
    entry-latency haircut and the DECISION now consume the SAME `params` object. Passing a heavier
    `entry_latency_haircut_bps` via the `params` argument therefore STRICTLY lowers realized PnL on a
    winner cohort — it is no longer outcome-inert. Pre-fix this asserted `arg_net == base_net` (a
    vacuous sensitivity that hid the params-threading defect); post-fix it must strictly decrease.
    """
    import dataclasses

    corpus = _series("GOOD", n=4, marks=_WIN, slot0=200, bt0=_BASE)
    base_net = sum(o.net_pnl_lamports for o in build_reaction_outcomes(corpus)[0])  # frozen 50 bps
    heavier = dataclasses.replace(REACTION_PARAMS, entry_latency_haircut_bps=500, params_hash="qa")
    arg_net = sum(o.net_pnl_lamports for o in build_reaction_outcomes(corpus, params=heavier)[0])
    assert arg_net < base_net  # FIXED: the params-arg haircut now bites the outcome path (500>50 bps)


def test_build_is_hash_seed_independent():
    """The sort key is (block_time, slot, mint) — deterministic regardless of PYTHONHASHSEED."""
    corpus = _series("BAD", n=5, marks=_LOSE, slot0=100, bt0=_BASE) + \
        _series("GOOD", n=5, marks=_WIN, slot0=200, bt0=_BASE + 50_000_000)
    out = build_reaction_outcomes(corpus)[0]
    digest = hashlib.sha256(
        str([(o.mint, o.net_pnl_lamports, o.model_selected) for o in out]).encode()).hexdigest()
    # Pinned digest, verified identical under PYTHONHASHSEED in {0,7,99} (dict-order / hash-seed
    # independent). A change here means the numeric output or ordering drifted (regression tripwire).
    assert digest[:16] == "aac6ae0652f97e10"
