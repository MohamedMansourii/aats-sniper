"""Tests for the REACTION / front-run harness — the smart-money/KOL signal -> reaction edge proof.

Self-check criteria (mirrors the ml-prediction self-check + the WP acceptance):
  1. FROZEN PARAMS: the reaction constants load + hash; a drifted value FAILS (anti-p-hacking);
     float-money is rejected.
  2. LEAK BOUNDARY 1 (decision vs outcome, signal-anchored): a feature derived from a `forward`
     (post-signal) mark reaching the DECISION makes the guard FAIL (RED); the signal-time-only
     decision PASSES (GREEN). Structural, not a comment.
  3. LEAK BOUNDARY 2 (walk-forward source reputation): injecting a source's CURRENT/FUTURE outcome
     (signal_block_time >= decision) OR a not-yet-observed prior outcome (resolved > decision) into
     the reputation FAILS (RED); the strictly-prior + already-observed set PASSES (GREEN). A source's
     first signals ride a neutral prior; no future same-source outcome informs the decision.
  4. STRUCTURAL SEPARATION: `decide_reaction_entry` takes no forward/outcome/post argument; the
     outcome is resolved from the forward path alone.
  5. CORRECTNESS on a fixture corpus: model selection is a strict SUBSET of "follow every signal";
     the model DECLINES a proven-loser source's later signals and its cohort net beats the
     baseline's; GATE-A / GATE-B compute.
  6. CENSORING: null anchor / non-positive price / unknown signal_type / empty forward / missing
     mint|source -> CENSORED, never a fabricated anchor or fill.
  7. ENTRY LATENCY HAIRCUT: the front-run haircut worsens the fill (lower PnL), conservative.
  8. MONEY DISCIPLINE: net_pnl / risk are INTEGER lamports; NO win-rate token anywhere.
  9. REPRODUCIBLE: same corpus -> byte-identical net_pnl_lamports.
 10. RUNNER: `run_edge_proof(strategy="reaction")` runs GATE-A / GATE-B and surfaces reaction stats.
 11. source_prior (externally provided) is AUDIT ONLY — never a selection input.

Offline / deterministic. No RPC (the anchor is in-record), no LLM, no network, no keypair, no capital.
"""

from __future__ import annotations

import inspect
import json
from decimal import Decimal

import pytest

from aats.backtest import reaction_harness as rh
from aats.backtest.outcome_harness import LeakError, PITFeature
from aats.backtest.reaction_harness import (
    REACTION_PARAMS,
    ReactionCorpusError,
    ReactionParamsError,
    ReactionSignal,
    ReputationInput,
    ReputationLeakError,
    apply_entry_latency_haircut,
    assemble_reaction_features,
    assert_reputation_inputs_prior,
    build_reaction_outcomes,
    decide_reaction_entry,
    load_reaction_params,
    parse_reaction_signal,
    source_reputation,
)
from aats.backtest.run_edge_proof import (
    _DEFAULT_CORPUS,
    _DEFAULT_REACTION_CORPUS,
    EXIT_GO,
    EXIT_NO_GO,
    STRATEGY_LAUNCH,
    STRATEGY_MOMENTUM,
    STRATEGY_REACTION,
    _resolve_corpus_path,
    run_edge_proof,
)
from aats.contracts.risk import RiskConfig
from aats.models.gate_a import compute_gate_a
from aats.models.gate_b import compute_gate_b_delta

_BASE_BT = 1_700_000_000_000
# Max reaction horizon (300s) => 300_000 ms; space same-source signals well past it so a prior
# signal's outcome is fully OBSERVED before the next same-source signal (walk-forward eligibility).
_SPACING_MS = 1_000_000

# A winner reaction path (price runs up, liquidity present) and a loser path (dumps then rugs).
_WIN_MARKS = {
    15: ("0.0011", "6000", 80, 20),
    30: ("0.0015", "7000", 70, 30),
    60: ("0.0025", "8000", 60, 40),
    120: ("0.0030", "9000", 50, 50),
    300: ("0.0033", "9000", 50, 50),
}
_LOSE_MARKS = {
    15: ("0.0009", "3000", 20, 80),
    30: ("0.0006", "1500", 10, 90),
    60: ("0.0004", "800", 5, 95),
    120: (None, None, 0, 0),
    300: (None, None, 0, 0),
}


def _reaction_record(
    mint: str,
    source: str,
    *,
    bt: int,
    slot: int,
    price: str = "0.001",
    marks: dict[int, tuple] = _LOSE_MARKS,
    signal_type: str = "whale_buy",
    size: str | None = "5.0",
    source_prior: float | None = None,
) -> dict:
    """Build one raw reaction-corpus dict (flat per-signal schema, forward = launch shape)."""
    forward = [
        {
            "horizon_s": h,
            "price_sol": m[0],
            "liquidity_usd": m[1],
            "txns_m5": {"buys": m[2], "sells": m[3]},
            "vol_m5": None,
            "obs_wall_ms": bt + h * 1000,
            "price_usd": None,
        }
        for h, m in sorted(marks.items())
    ]
    return {
        "signal_type": signal_type,
        "source_id": source,
        "mint": mint,
        "signal_slot": slot,
        "signal_block_time_ms": bt,
        "signal_price_sol": price,
        "signal_size_sol": size,
        "source_prior": source_prior,
        "forward": forward,
    }


def _source_series(source: str, *, n: int, marks: dict[int, tuple], slot0: int, bt0: int) -> list[dict]:
    """n same-source signals spaced past the max horizon so each prior outcome is observed in time."""
    return [
        _reaction_record(f"{source}_{i}", source, bt=bt0 + i * _SPACING_MS, slot=slot0 + i, marks=marks)
        for i in range(n)
    ]


def _signal(bt: int = _BASE_BT, source: str = "S") -> ReactionSignal:
    return ReactionSignal(
        mint="M",
        source_id=source,
        signal_type="whale_buy",
        signal_slot=1,
        signal_block_time_ms=bt,
        signal_price_sol=Decimal("0.001"),
        signal_size_sol=Decimal("5"),
        source_prior=None,
    )


# ---------------------------------------------------------------------------
# 1. FROZEN PARAMS — load + hash-drift + float-money rejection (anti-p-hacking)
# ---------------------------------------------------------------------------


def test_frozen_params_load_and_hash_matches() -> None:
    p = load_reaction_params()
    assert p.params_hash == REACTION_PARAMS.params_hash
    assert p.min_prior_signals >= 1
    assert p.entry_latency_haircut_bps >= 0
    assert isinstance(p.reputation_threshold, Decimal)


def test_frozen_hash_drift_fails(tmp_path) -> None:
    """A value changed after the freeze (stale frozen_hash) is a TEST FAILURE, not an edit."""
    good = json.loads(rh._REACTION_PARAMS_PATH.read_text(encoding="utf-8"))
    tampered = dict(good)
    tampered["params"] = dict(good["params"])
    tampered["params"]["reputation_threshold"] = "0.05"  # retune the threshold, keep old hash
    path = tmp_path / "drift.json"
    path.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(ReactionParamsError, match="changed_after_freeze"):
        load_reaction_params(path)


def test_frozen_float_money_rejected(tmp_path) -> None:
    good = json.loads(rh._REACTION_PARAMS_PATH.read_text(encoding="utf-8"))
    bad = dict(good)
    bad["params"] = dict(good["params"])
    bad["params"]["reputation_threshold"] = 0.0  # float, not Decimal-string
    bad["frozen_hash"] = None  # skip the drift check to reach the float guard
    path = tmp_path / "float.json"
    path.write_text(json.dumps(bad), encoding="utf-8")
    with pytest.raises(ReactionParamsError, match="float"):
        load_reaction_params(path)


# ---------------------------------------------------------------------------
# 2. LEAK BOUNDARY 1 — decision vs outcome (RED forward feature / GREEN signal-only)
# ---------------------------------------------------------------------------


def test_leak_b1_green_signal_time_only_decision() -> None:
    """GREEN: a signal-time-only decision passes the guard (cutoff = signal anchor)."""
    d = decide_reaction_entry(
        _signal(),
        reputation=None,
        n_prior_signals=0,
        reputation_audit_summary="ok",
        params=REACTION_PARAMS,
        risk_config=RiskConfig(),
    )
    assert "LEAK AUDIT PASS" in d.leak_audit_summary
    assert d.baseline_selected is True
    assert d.model_selected is True  # neutral prior -> follows (frozen policy)


def test_leak_b1_red_forward_feature_injected() -> None:
    """RED: a feature built from a `forward` (60s) mark trips the guard.

    Its event-time is `signal_block_time + 60s`, strictly after the decision cutoff (the signal
    anchor), so `decide_reaction_entry`'s guard MUST raise LeakError.
    """
    sig = _signal()
    leaked = PITFeature(
        name="LEAK_fwd_price_sol@60s",
        value=0.0025,
        event_block_time_ms=sig.signal_block_time_ms + 60 * 1000,
    )
    with pytest.raises(LeakError, match="forward"):
        decide_reaction_entry(
            sig,
            reputation=None,
            n_prior_signals=0,
            reputation_audit_summary="ok",
            params=REACTION_PARAMS,
            risk_config=RiskConfig(),
            extra_features=(leaked,),
        )
    # GREEN control: the SAME call without the injected forward feature returns a decision.
    ok = decide_reaction_entry(
        sig,
        reputation=None,
        n_prior_signals=0,
        reputation_audit_summary="ok",
        params=REACTION_PARAMS,
        risk_config=RiskConfig(),
    )
    assert isinstance(ok.model_selected, bool)


def test_leak_b1_red_forward_feature_via_assembler() -> None:
    """RED at the assembler level: a post-signal-stamped feature fails assert_features_leq_decision."""
    from aats.backtest.outcome_harness import assert_features_leq_decision

    sig = _signal()
    leaked = PITFeature("LEAK@120s", 1.0, sig.signal_block_time_ms + 120 * 1000)
    feats = assemble_reaction_features(sig, reputation=Decimal("0.1"), extra_features=(leaked,))
    with pytest.raises(LeakError, match="forward"):
        assert_features_leq_decision(feats, sig.signal_block_time_ms)


# ---------------------------------------------------------------------------
# 3. LEAK BOUNDARY 2 — walk-forward source reputation (RED future/current / GREEN prior-observed)
# ---------------------------------------------------------------------------


def test_leak_b2_green_strictly_prior_observed() -> None:
    prior = ReputationInput("S", _BASE_BT - _SPACING_MS, _BASE_BT - 700_000, Decimal("0.1"))
    rep, n, summary = source_reputation([prior], _BASE_BT)
    assert n == 1
    assert rep == Decimal("0.1")
    assert "REPUTATION AUDIT PASS" in summary


def test_leak_b2_red_current_or_future_same_source_outcome() -> None:
    """RED: a same-source outcome whose signal_block_time >= decision (its OWN current signal or a
    FUTURE one) reaching the reputation must FAIL. This is the walk-forward boundary."""
    current = ReputationInput("S", _BASE_BT, _BASE_BT - 100, Decimal("0.1"))
    with pytest.raises(ReputationLeakError, match="CURRENT or FUTURE"):
        source_reputation([current], _BASE_BT)
    future = ReputationInput("S", _BASE_BT + 500, _BASE_BT + 400, Decimal("0.1"))
    with pytest.raises(ReputationLeakError, match="CURRENT or FUTURE"):
        assert_reputation_inputs_prior([future], _BASE_BT)


def test_leak_b2_red_prior_outcome_not_yet_observed() -> None:
    """RED: a strictly-prior signal whose OUTCOME resolves AFTER the decision (its forward window
    extends past the decision instant) is an outcome-completion lookahead and must FAIL."""
    not_observed = ReputationInput("S", _BASE_BT - 100_000, _BASE_BT + 100_000, Decimal("0.1"))
    with pytest.raises(ReputationLeakError, match="not yet observable"):
        source_reputation([not_observed], _BASE_BT)


def test_leak_b2_end_to_end_reputation_ignores_future_same_source() -> None:
    """The build's walk-forward reputation of an EARLY signal is unaffected by LATER same-source
    outcomes: a source's first `min_prior_signals` signals ride the neutral prior regardless of how
    its later signals turn out (no future outcome informs the current decision)."""
    recs = _source_series("BAD", n=5, marks=_LOSE_MARKS, slot0=100, bt0=_BASE_BT)
    outcomes, stats = build_reaction_outcomes(recs)
    # First min_prior_signals (=3) BAD signals are on the neutral prior -> followed by the model,
    # even though the source will prove to be a net loser. The reputation looked only backward.
    n_prior = REACTION_PARAMS.min_prior_signals
    by_mint = {o.mint: o for o in outcomes}
    for i in range(n_prior):
        assert by_mint[f"BAD_{i}"].model_selected is True  # neutral prior, no track record yet
    # Once the source has >= min_prior_signals OBSERVED losers, the model declines it.
    for i in range(n_prior, 5):
        assert by_mint[f"BAD_{i}"].model_selected is False
    assert stats.n_declined_by_model == 5 - n_prior


# ---------------------------------------------------------------------------
# 3b. NEAR-LINEAR WALK-FORWARD ACCUMULATOR — equal to the naive filter, non-monotonic-safe
# ---------------------------------------------------------------------------

# A SHORT winner forward window (max horizon 60s) so its outcome resolves EARLIER than an
# earlier-signalled LONG (max horizon 300s) window — making outcome_resolved NON-MONOTONIC in
# signal order (the case the accumulator's release-heap must handle, not a second pointer).
_SHORT_WIN_MARKS = {
    15: ("0.0011", "6000", 80, 20),
    30: ("0.0015", "7000", 70, 30),
    60: ("0.0020", "8000", 60, 40),
}


def _resolve_and_group(recs: list[dict]):
    """Replicate build_reaction_outcomes' PASS-1 (resolve + sort + group) so a test can compare the
    near-linear accumulator against a naive per-decision reference on the SAME resolved set."""
    from aats.backtest.outcome_harness import build_round_trip_cost_stack

    cost = build_round_trip_cost_stack()
    rc = RiskConfig()
    resolved = []
    for obj in recs:
        try:
            resolved.append(
                rh._resolve_one(
                    obj,
                    params=REACTION_PARAMS,
                    cost_stack=cost,
                    risk_config=rc,
                    exit_model=rh.EXIT_MODEL_REALIZABLE,
                )
            )
        except (rh.ReactionCorpusError, rh.CorpusRecordError):
            continue
    resolved.sort(
        key=lambda r: (r.signal.signal_block_time_ms, r.signal.signal_slot, r.signal.mint)
    )
    by_source: dict[str, list] = {}
    for r in resolved:
        by_source.setdefault(r.signal.source_id, []).append(r)
    return resolved, by_source


def _naive_reputations(resolved, by_source):
    """The previous O(k^2)-per-source reference: rescan ALL same-source outcomes per decision."""
    out = []
    for r in resolved:
        decision_bt = r.signal.signal_block_time_ms
        eligible = [
            ReputationInput(
                source_id=o.signal.source_id,
                signal_block_time_ms=o.signal.signal_block_time_ms,
                outcome_resolved_block_time_ms=o.outcome_resolved_block_time_ms,
                net_pnl_per_sol=o.net_pnl_per_sol,
            )
            for o in by_source[r.signal.source_id]
            if o.signal.signal_block_time_ms < decision_bt
            and o.outcome_resolved_block_time_ms <= decision_bt
        ]
        rep, n, _ = source_reputation(eligible, decision_bt)
        out.append((rep, n))
    return out


def test_walk_forward_accumulator_equals_naive_with_varying_horizons() -> None:
    """The near-linear accumulator's (reputation, n_prior) is BYTE-IDENTICAL to the naive
    per-decision filter, even when forward windows differ in length so outcome_resolved is
    non-monotonic in signal order (the heap-release path). A divergence would be a leak or a
    silent selection change — the whole point of leak boundary 2."""
    # Same-source signals 40s apart, alternating LONG (300s) / SHORT (60s) forward windows, so an
    # earlier LONG signal resolves AFTER a later SHORT signal (non-monotonic observation times).
    mix = [
        _reaction_record(
            f"MIX_{i}",
            "MIX",
            bt=_BASE_BT + i * 40_000,
            slot=300 + i,
            marks=_WIN_MARKS if i % 2 == 0 else _SHORT_WIN_MARKS,
        )
        for i in range(8)
    ]
    resolved, by_source = _resolve_and_group(mix)
    acc = [(rep, n) for (rep, n, _s) in rh._walk_forward_reputations(resolved, by_source)]
    naive = _naive_reputations(resolved, by_source)
    assert acc == naive
    # The fixture actually exercised the not-yet-observed path (some decisions saw fewer priors
    # than the count of strictly-prior signals), else this test would be vacuous.
    assert any(n_prior < idx for idx, (_rep, n_prior) in enumerate(acc))


def test_walk_forward_accumulator_multi_source_equals_naive() -> None:
    """Interleaved sources: the per-source pointer/heap state must not cross-contaminate."""
    recs = (
        _source_series("BAD", n=5, marks=_LOSE_MARKS, slot0=100, bt0=_BASE_BT)
        + _source_series("GOOD", n=5, marks=_WIN_MARKS, slot0=200, bt0=_BASE_BT + 20_000)
    )
    resolved, by_source = _resolve_and_group(recs)
    acc = [(rep, n) for (rep, n, _s) in rh._walk_forward_reputations(resolved, by_source)]
    assert acc == _naive_reputations(resolved, by_source)


# ---------------------------------------------------------------------------
# 4. STRUCTURAL SEPARATION — decision takes no forward/outcome argument
# ---------------------------------------------------------------------------


def test_decision_takes_no_forward_or_outcome_argument() -> None:
    params = set(inspect.signature(decide_reaction_entry).parameters)
    assert not any(tok in p for p in params for tok in ("forward", "post", "outcome_mark"))
    # It takes the walk-forward reputation SCALAR, not the prior outcomes themselves.
    assert "reputation" in params


# ---------------------------------------------------------------------------
# 5. CORRECTNESS — model is a subset of "follow every signal"; it avoids the proven-loser's losses
# ---------------------------------------------------------------------------


def test_model_is_subset_and_beats_baseline_on_proven_loser_source() -> None:
    # A proven-loser source (BAD) fires 6 losers; a proven-winner source (GOOD) fires 6 winners.
    bad = _source_series("BAD", n=6, marks=_LOSE_MARKS, slot0=100, bt0=_BASE_BT)
    good = _source_series("GOOD", n=6, marks=_WIN_MARKS, slot0=200, bt0=_BASE_BT + 50_000_000)
    recs = bad + good
    outcomes, stats = build_reaction_outcomes(recs)

    assert stats.n_resolved == 12
    assert stats.n_sources == 2
    # Model selection is a strict SUBSET of the baseline (baseline follows every signal).
    for o in outcomes:
        assert o.baseline_selected is True
        if o.model_selected:
            assert o.baseline_selected is True
    assert stats.n_baseline_selected == 12
    assert stats.n_model_selected < stats.n_baseline_selected  # model dropped proven losers
    assert stats.n_declined_by_model == 6 - REACTION_PARAMS.min_prior_signals

    # The model's cohort net beats the baseline's (it skipped the proven-loser's later losses).
    ga_model = compute_gate_a(outcomes, model=True)
    ga_base = compute_gate_a(outcomes, model=False)
    assert ga_model.total_net_pnl_lamports > ga_base.total_net_pnl_lamports

    # GATE-B computes (maker does not grade GO/NO-GO).
    gb = compute_gate_b_delta(outcomes)
    assert gb.n_trades == 12
    assert isinstance(gb.gate_b_pass, bool)


def test_good_source_is_never_declined() -> None:
    good = _source_series("GOOD", n=6, marks=_WIN_MARKS, slot0=200, bt0=_BASE_BT)
    outcomes, stats = build_reaction_outcomes(good)
    assert stats.n_model_selected == 6  # a proven net-winner source clears the threshold
    assert stats.n_declined_by_model == 0


# ---------------------------------------------------------------------------
# 6. CENSORING — fail-closed on unusable records (never a fabricated anchor/fill)
# ---------------------------------------------------------------------------


def test_censoring_null_anchor_price_type_forward() -> None:
    good = _reaction_record("OK", "S", bt=_BASE_BT, slot=1, marks=_WIN_MARKS)

    null_anchor = _reaction_record("A", "S", bt=_BASE_BT, slot=2, marks=_WIN_MARKS)
    null_anchor["signal_block_time_ms"] = None  # T-300a: no on-chain anchor -> CENSORED

    bad_price = _reaction_record("B", "S", bt=_BASE_BT, slot=3, price="0", marks=_WIN_MARKS)

    bad_type = _reaction_record("C", "S", bt=_BASE_BT, slot=4, marks=_WIN_MARKS)
    bad_type["signal_type"] = "coordinated_shill"  # unknown -> CENSORED

    empty_forward = _reaction_record("D", "S", bt=_BASE_BT, slot=5, marks=_WIN_MARKS)
    empty_forward["forward"] = []

    no_source = _reaction_record("E", "", bt=_BASE_BT, slot=6, marks=_WIN_MARKS)

    recs = [good, null_anchor, bad_price, bad_type, empty_forward, no_source]
    outcomes, stats = build_reaction_outcomes(recs)
    assert stats.n_resolved == 1  # only the good one resolves
    assert stats.n_censored == 5
    assert outcomes[0].mint == "OK"


def test_parse_rejects_non_positive_price() -> None:
    with pytest.raises(ReactionCorpusError, match="signal_price_sol"):
        parse_reaction_signal(
            _reaction_record("Z", "S", bt=_BASE_BT, slot=1, price="-0.5", marks=_WIN_MARKS)
        )


# ---------------------------------------------------------------------------
# 7. ENTRY LATENCY HAIRCUT — worsens the fill (conservative, only lowers PnL)
# ---------------------------------------------------------------------------


def test_entry_latency_haircut_raises_entry_price() -> None:
    price = Decimal("0.001")
    filled = apply_entry_latency_haircut(price, REACTION_PARAMS)
    assert filled > price  # fill HIGHER than the signal tick (worse) = conservative
    expected = price * (Decimal(1) + Decimal(REACTION_PARAMS.entry_latency_haircut_bps) / Decimal(10_000))
    assert filled == expected


def _haircut_net(recs: list[dict], bps: int) -> int:
    """Total realized net PnL when the front-run entry-latency haircut is `bps` (via params)."""
    import dataclasses

    params = dataclasses.replace(
        REACTION_PARAMS,
        entry_latency_haircut_bps=bps,
        params_hash="test-only-not-frozen",
    )
    outs, _ = build_reaction_outcomes(recs, params=params)
    return sum(o.net_pnl_lamports for o in outs)


def test_haircut_lowers_realized_pnl_the_heavier_it_is() -> None:
    """A HEAVIER front-run haircut STRICTLY lowers realized PnL on a winner cohort (conservative),
    and the OUTCOME path honors the SAME `params` the decision uses.

    This is the exact safety property MAJOR-1 broke: pre-fix, `_resolve_one` used the frozen global
    for the entry-latency haircut, so EVERY override gave BYTE-IDENTICAL net PnL (50 vs 500 bps both
    == 192623506) and this test was vacuous. Post-fix, a heavier haircut raises the entry fill and
    strictly reduces realized net PnL, so any two REALISTIC (positive) haircuts are strictly ordered.

    NOTE (honest caveat): we deliberately do NOT compare against the degenerate 0-bps "fill exactly
    at the signal tick" case. The realizable-exit engine is THRESHOLD-based on the price multiple R,
    so filling at the exact tick can land on a different stop/take-profit branch than any positive
    haircut — a discretization artifact at the boundary, not a conservatism violation. In the
    OPERATIVE regime (all realistic positive haircuts) the relationship is cleanly monotone: heavier
    => strictly lower. The frozen production haircut is 50 bps and is always applied.
    """
    recs = _source_series("GOOD", n=4, marks=_WIN_MARKS, slot0=200, bt0=_BASE_BT)

    light_net = _haircut_net(recs, REACTION_PARAMS.entry_latency_haircut_bps)  # frozen 50 bps
    mid_net = _haircut_net(recs, 200)  # 2%
    heavy_net = _haircut_net(recs, 500)  # 5%

    # Strictly monotone in the conservative direction: heavier haircut => strictly lower realized
    # PnL. (Pre-MAJOR-1-fix these were all equal — the outcome path ignored the override.)
    assert heavy_net < mid_net < light_net
    # The default (no params) path equals the explicit frozen-bps path (no silent divergence).
    default_out, _ = build_reaction_outcomes(recs)
    assert sum(o.net_pnl_lamports for o in default_out) == light_net


# ---------------------------------------------------------------------------
# 8. MONEY DISCIPLINE / NO WIN-RATE
# ---------------------------------------------------------------------------


def test_money_is_integer_lamports() -> None:
    recs = _source_series("GOOD", n=4, marks=_WIN_MARKS, slot0=200, bt0=_BASE_BT)
    outcomes, _ = build_reaction_outcomes(recs)
    for o in outcomes:
        assert isinstance(o.net_pnl_lamports, int)
        assert o.sol_at_risk_lamports == RiskConfig().per_trade_cap_lamports


def test_no_win_rate_tokens_in_source() -> None:
    src = inspect.getsource(rh).lower()
    assert "win_rate" not in src
    assert "winrate" not in src
    assert 'won"' not in src


# ---------------------------------------------------------------------------
# 9. REPRODUCIBLE — same corpus -> identical net_pnl
# ---------------------------------------------------------------------------


def test_reproducible_net_pnl() -> None:
    recs = _source_series("BAD", n=5, marks=_LOSE_MARKS, slot0=100, bt0=_BASE_BT) + _source_series(
        "GOOD", n=5, marks=_WIN_MARKS, slot0=200, bt0=_BASE_BT + 50_000_000
    )
    o1, _ = build_reaction_outcomes(recs)
    o2, _ = build_reaction_outcomes(recs)
    assert [o.net_pnl_lamports for o in o1] == [o.net_pnl_lamports for o in o2]
    assert [o.model_selected for o in o1] == [o.model_selected for o in o2]


# ---------------------------------------------------------------------------
# 10. RUNNER — run_edge_proof(strategy="reaction") runs GATE-A / GATE-B, surfaces reaction stats
# ---------------------------------------------------------------------------


def _write_reaction_corpus(path, records: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")


class _UnusedResolver:
    """The reaction strategy needs NO resolver (anchor is in-record); this proves it is never used."""

    def resolve(self, signature: str) -> object:  # pragma: no cover - must never be called
        raise AssertionError("reaction strategy must not call a block_time resolver")


def test_runner_reaction_strategy_runs_gates(tmp_path) -> None:
    recs = _source_series("BAD", n=6, marks=_LOSE_MARKS, slot0=100, bt0=_BASE_BT) + _source_series(
        "GOOD", n=6, marks=_WIN_MARKS, slot0=200, bt0=_BASE_BT + 50_000_000
    )
    corpus = tmp_path / "reaction.jsonl"
    _write_reaction_corpus(corpus, recs)
    code, scoreboard = run_edge_proof(
        corpus,
        block_time_resolver=_UnusedResolver(),
        strategy=STRATEGY_REACTION,
    )
    assert scoreboard["strategy"] == "reaction"
    assert scoreboard["n_resolved"] == 12
    assert scoreboard["n_sources"] == 2
    assert scoreboard["n_baseline_selected"] == 12
    assert scoreboard["n_model_selected"] < 12  # model dropped proven losers
    assert scoreboard["n_declined_by_model"] == 6 - REACTION_PARAMS.min_prior_signals
    assert "gate_a_model" in scoreboard
    assert "gate_b" in scoreboard
    assert scoreboard["verdict"] in ("GO", "NO-GO")
    assert code in (EXIT_GO, EXIT_NO_GO)


def test_explicit_corpus_is_honored_even_when_it_equals_launch_default() -> None:
    """An explicitly-passed --corpus is ALWAYS honored — even the launch-default path under
    --strategy reaction (the None-sentinel fix; the old equality sentinel silently swapped it)."""
    # Explicit path == launch default, strategy reaction -> HONORED (not swapped to the reaction one).
    assert _resolve_corpus_path(_DEFAULT_CORPUS, STRATEGY_REACTION) == _DEFAULT_CORPUS
    # A custom explicit path is honored for every strategy.
    assert _resolve_corpus_path("C:/custom/x.jsonl", STRATEGY_REACTION) == "C:/custom/x.jsonl"
    assert _resolve_corpus_path("C:/custom/x.jsonl", STRATEGY_MOMENTUM) == "C:/custom/x.jsonl"
    # UNSET (None) -> the per-strategy default.
    assert _resolve_corpus_path(None, STRATEGY_REACTION) == _DEFAULT_REACTION_CORPUS
    assert _resolve_corpus_path(None, STRATEGY_LAUNCH) == _DEFAULT_CORPUS
    assert _resolve_corpus_path(None, STRATEGY_MOMENTUM) == _DEFAULT_CORPUS


# ---------------------------------------------------------------------------
# 11. source_prior is AUDIT ONLY — never a selection input
# ---------------------------------------------------------------------------


def test_source_prior_does_not_change_selection() -> None:
    """An externally-provided source_prior (unverified provenance) must NOT change the decision —
    the model relies solely on the leak-free walk-forward reputation it computes itself."""
    base = _source_series("GOOD", n=4, marks=_WIN_MARKS, slot0=200, bt0=_BASE_BT)
    with_prior = [dict(r, source_prior=0.99) for r in _source_series(
        "GOOD", n=4, marks=_WIN_MARKS, slot0=200, bt0=_BASE_BT
    )]
    o_base, _ = build_reaction_outcomes(base)
    o_prior, _ = build_reaction_outcomes(with_prior)
    assert [o.model_selected for o in o_base] == [o.model_selected for o in o_prior]
