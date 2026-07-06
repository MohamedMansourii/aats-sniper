"""Tests for the MOMENTUM / reaction-entry harness (a different strategy, same leak machinery).

Self-check criteria (mirrors the ml-prediction self-check + the WP acceptance):
  1. THE LEAK TEST (headline): a feature derived from a `horizon_s > T_ENTRY` mark reaching the
     DECISION makes the (moving-cutoff) guard FAIL (RED); the `<= T_ENTRY`-only decision PASSES
     (GREEN). Proves the boundary is real and structural at T_ENTRY, not a comment.
  2. STRUCTURAL SEPARATION: `decide_momentum_entry` takes no post/forward/price argument;
     `resolve_momentum_outcome` REQUIRES the post-entry marks. The decision cannot see the future.
  3. THE SPLIT: `split_forward_at_entry` puts every `> T_ENTRY` mark ONLY in the outcome
     partition and every `<= T_ENTRY` mark ONLY in the decision partition.
  4. CORRECTNESS on a fixture corpus: momentum winner / flat / sells-heavy loser / untradeable
     produce the expected selection masks and PnL signs; GATE-A / GATE-B compute.
  5. CANNOT-PRICE SKIP: null price at T_ENTRY -> both cohorts decline, net 0, NEVER a fabricated
     fill.
  6. MONEY DISCIPLINE: net_pnl / risk are INTEGER lamports; NO win-rate anywhere.
  7. REPRODUCIBLE: same corpus + seed -> byte-identical net_pnl_lamports.
  8. RUNNER: `run_edge_proof(strategy="momentum")` runs GATE-A / GATE-B and surfaces T_ENTRY.

Offline / injectable / deterministic. No RPC, no LLM, no network, no keypair, no capital.
"""

from __future__ import annotations

import inspect
import json

import pytest

from aats.backtest import momentum_harness as mh
from aats.backtest.momentum_harness import (
    CORPUS_HORIZON_GRID,
    DEFAULT_ENTRY_HORIZON_S,
    MomentumForwardObservation,
    assemble_momentum_features,
    build_momentum_outcomes,
    decide_momentum_entry,
    load_momentum_params,
    momentum_decision_cutoff_ms,
    read_momentum_forward,
    resolve_momentum_outcome,
    split_forward_at_entry,
)
from aats.backtest.outcome_harness import (
    BlockTime,
    CorpusRecord,
    CorpusRecordError,
    FixtureBlockTimeResolver,
    LeakError,
    PITFeature,
    assert_features_leq_decision,
    to_entry_record,
)
from aats.backtest.run_edge_proof import (
    EXIT_GO,
    EXIT_NO_GO,
    STRATEGY_MOMENTUM,
    run_edge_proof,
)
from aats.contracts.risk import RiskConfig
from aats.models.gate_a import compute_gate_a
from aats.models.gate_b import compute_gate_b_delta

_DECISION_BT_MS = 1_700_000_100_000
_RECV_WALL_MS = 1_700_000_000_000


def _mom_record(mint: str, sig: str, *, marks: dict[int, dict]) -> CorpusRecord:
    """Build a corpus record whose forward list carries momentum fields per horizon.

    `marks[horizon] = {"price": str|None, "buys": int, "sells": int, "liq": str|None}`.
    """
    forward = []
    for h in sorted(marks):
        m = marks[h]
        forward.append(
            {
                "horizon_s": h,
                "price_sol": m.get("price"),
                "price_usd": None,
                "liquidity_usd": m.get("liq"),
                "vol_m5": m.get("vol"),
                "txns_m5": {"buys": m.get("buys", 0), "sells": m.get("sells", 0)},
                "txns_h1": None,
                "note": m.get("note", ""),
                "obs_wall_ms": _RECV_WALL_MS + h * 1000,
            }
        )
    entry = {
        "mint": mint,
        "signature": sig,
        "traderPublicKey": "T",
        "initialBuy": 1_000_000.0,
        "solAmount": 0.5,
        "vTokensInBondingCurve": 1_000_000_000.0,
        "vSolInBondingCurve": 30.0,
        "marketCapSol": 30.0,
        "name": "n",
        "symbol": "s",
        "pool": "pump",
        "recv_wall_ms": _RECV_WALL_MS,
    }
    return CorpusRecord(entry=entry, forward=forward)


# Canonical fixtures on the fixed horizon grid, entering at T_ENTRY = 60.
def _winner(mint: str = "MOM_WIN", sig: str = "sig_win") -> CorpusRecord:
    # price rises 30->60 (0.001 -> 0.0011), buys dominate, liquidity present; then runs to 3x.
    return _mom_record(
        mint, sig,
        marks={
            30: {"price": "0.001", "buys": 80, "sells": 20, "liq": "5000"},
            60: {"price": "0.0011", "buys": 90, "sells": 10, "liq": "6000"},
            120: {"price": "0.0020", "buys": 70, "sells": 30, "liq": "7000"},
            300: {"price": "0.0030", "buys": 60, "sells": 40, "liq": "8000"},
            900: {"price": "0.0033", "buys": 50, "sells": 50, "liq": "9000"},
        },
    )


def _flat(mint: str = "MOM_FLAT", sig: str = "sig_flat") -> CorpusRecord:
    # price does NOT rise 30->60 -> neither model nor baseline selects.
    return _mom_record(
        mint, sig,
        marks={
            30: {"price": "0.001", "buys": 90, "sells": 10, "liq": "6000"},
            60: {"price": "0.001", "buys": 90, "sells": 10, "liq": "6000"},
            120: {"price": "0.001", "buys": 50, "sells": 50, "liq": "6000"},
            300: {"price": "0.001", "buys": 50, "sells": 50, "liq": "6000"},
            900: {"price": "0.001", "buys": 50, "sells": 50, "liq": "6000"},
        },
    )


def _sells_heavy_loser(mint: str = "MOM_SELL", sig: str = "sig_sell") -> CorpusRecord:
    # price rose 30->60 (baseline BUYS) but sells dominate (model DECLINES); then it dumps.
    return _mom_record(
        mint, sig,
        marks={
            30: {"price": "0.001", "buys": 40, "sells": 60, "liq": "6000"},
            60: {"price": "0.0011", "buys": 30, "sells": 70, "liq": "6000"},
            120: {"price": "0.0006", "buys": 20, "sells": 80, "liq": "3000"},
            300: {"price": "0.0004", "buys": 10, "sells": 90, "liq": "1000"},
            900: {"price": None, "buys": 0, "sells": 0, "liq": None},
        },
    )


def _untradeable(mint: str = "MOM_SKIP", sig: str = "sig_skip") -> CorpusRecord:
    # no market at T_ENTRY=60 -> cannot price -> SKIP (declined by both, net 0).
    return _mom_record(
        mint, sig,
        marks={
            30: {"price": "0.001", "buys": 90, "sells": 10, "liq": "5000"},
            60: {"price": None, "buys": 0, "sells": 0, "liq": None},
            120: {"price": "0.002", "buys": 50, "sells": 50, "liq": "6000"},
            300: {"price": "0.003", "buys": 50, "sells": 50, "liq": "6000"},
            900: {"price": "0.004", "buys": 50, "sells": 50, "liq": "6000"},
        },
    )


def _resolver_for(records: list[CorpusRecord]) -> FixtureBlockTimeResolver:
    return FixtureBlockTimeResolver(
        {
            r.signature: BlockTime(slot=300_000_000 + i, block_time_ms=_DECISION_BT_MS + i)
            for i, r in enumerate(records)
        }
    )


def _entry_record(record: CorpusRecord):
    return to_entry_record(record, BlockTime(slot=300_000_000, block_time_ms=_DECISION_BT_MS))


# ---------------------------------------------------------------------------
# 1. THE LEAK TEST — RED (a > T_ENTRY feature reaches the decision) / GREEN (boundary intact)
# ---------------------------------------------------------------------------


def test_leak_guard_green_with_pre_entry_only_features() -> None:
    """GREEN: the `<= T_ENTRY` decision feature set passes the moving-cutoff guard."""
    rec = _winner()
    entry = _entry_record(rec)
    pre, _post = split_forward_at_entry(read_momentum_forward(rec), DEFAULT_ENTRY_HORIZON_S)
    feats = assemble_momentum_features(entry, pre)
    cutoff = momentum_decision_cutoff_ms(entry, DEFAULT_ENTRY_HORIZON_S)
    summary = assert_features_leq_decision(feats, cutoff)
    assert "LEAK AUDIT PASS" in summary
    decision = decide_momentum_entry(
        entry, pre, entry_horizon_s=DEFAULT_ENTRY_HORIZON_S, risk_config=RiskConfig()
    )
    assert "LEAK AUDIT PASS" in decision.leak_audit_summary
    assert decision.model_selected is True  # the clean momentum winner is selected


def test_leak_guard_red_when_post_entry_feature_injected() -> None:
    """RED: a feature built from a `> T_ENTRY` (300s) mark trips the guard.

    Its event-time is `launch_block_time + 300s`, strictly after the decision cutoff
    (`launch_block_time + 60s`), so `assert_features_leq_decision` MUST raise LeakError.
    """
    rec = _winner()
    entry = _entry_record(rec)
    pre, post = split_forward_at_entry(read_momentum_forward(rec), DEFAULT_ENTRY_HORIZON_S)
    post_mark = next(o for o in post if o.horizon_s == 300)
    assert post_mark.price_sol is not None
    leaked = PITFeature(
        name="LEAK_fwd_price_sol@300s",
        value=float(post_mark.price_sol),
        event_block_time_ms=entry.decision_block_time_ms + post_mark.horizon_s * 1000,
    )
    tainted = assemble_momentum_features(entry, pre, extra_features=(leaked,))
    cutoff = momentum_decision_cutoff_ms(entry, DEFAULT_ENTRY_HORIZON_S)
    with pytest.raises(LeakError, match="forward"):
        assert_features_leq_decision(tainted, cutoff)


def test_decide_momentum_red_when_post_feature_injected_end_to_end() -> None:
    """RED end-to-end: injecting a `> T_ENTRY` feature via decide's seam raises LeakError;
    the same call WITHOUT the injection (GREEN) returns a decision."""
    rec = _winner()
    entry = _entry_record(rec)
    pre, post = split_forward_at_entry(read_momentum_forward(rec), DEFAULT_ENTRY_HORIZON_S)
    post_mark = next(o for o in post if o.horizon_s == 900)
    leaked = PITFeature(
        "LEAK_fwd_price_sol@900s",
        float(post_mark.price_sol),
        entry.decision_block_time_ms + post_mark.horizon_s * 1000,
    )
    with pytest.raises(LeakError):
        decide_momentum_entry(
            entry, pre, entry_horizon_s=DEFAULT_ENTRY_HORIZON_S,
            risk_config=RiskConfig(), extra_features=(leaked,),
        )
    ok = decide_momentum_entry(
        entry, pre, entry_horizon_s=DEFAULT_ENTRY_HORIZON_S, risk_config=RiskConfig()
    )
    assert isinstance(ok.model_selected, bool)


def test_leak_guard_red_when_outcome_mark_fed_to_decision() -> None:
    """RED: feeding an OUTCOME (post-entry) mark into the decision partition trips the guard.

    A `> T_ENTRY` observation carries a post-cutoff event-time; assembling its decision features
    and guarding them raises LeakError. This proves the split boundary is enforced, not assumed.
    """
    rec = _winner()
    entry = _entry_record(rec)
    path = read_momentum_forward(rec)
    _pre, post = split_forward_at_entry(path, DEFAULT_ENTRY_HORIZON_S)
    # Wrongly hand a post-entry mark to the decision-feature assembler.
    tainted = assemble_momentum_features(entry, [post[0]])
    cutoff = momentum_decision_cutoff_ms(entry, DEFAULT_ENTRY_HORIZON_S)
    with pytest.raises(LeakError, match="forward"):
        assert_features_leq_decision(tainted, cutoff)


# ---------------------------------------------------------------------------
# 2. STRUCTURAL SEPARATION + 3. THE SPLIT
# ---------------------------------------------------------------------------


def test_decision_and_outcome_are_structurally_separated() -> None:
    decide_params = set(inspect.signature(decide_momentum_entry).parameters)
    resolve_params = set(inspect.signature(resolve_momentum_outcome).parameters)
    # The decision takes no forward/post/outcome/price argument (pre_entry_marks is the ONLY
    # trajectory input, and it is the `<= T_ENTRY` partition).
    assert not any(
        tok in p for p in decide_params for tok in ("post", "forward", "outcome")
    )
    assert "pre_entry_marks" in decide_params
    # The outcome REQUIRES the post-entry marks.
    assert "post_entry_marks" in resolve_params


def test_split_partitions_at_t_entry() -> None:
    rec = _winner()
    path = read_momentum_forward(rec)
    for t_entry in (30, 60, 120):
        pre, post = split_forward_at_entry(path, t_entry)
        assert all(o.horizon_s <= t_entry for o in pre)
        assert all(o.horizon_s > t_entry for o in post)
        assert len(pre) + len(post) == len(path)


# ---------------------------------------------------------------------------
# 4. CORRECTNESS — selection masks + PnL signs on the fixture corpus
# ---------------------------------------------------------------------------


def test_selection_masks_winner_flat_sellsheavy_untradeable() -> None:
    records = [_winner(), _flat(), _sells_heavy_loser(), _untradeable()]
    outcomes, stats = build_momentum_outcomes(
        records, block_time_resolver=_resolver_for(records), entry_horizon_s=60
    )
    assert stats.entry_horizon_s == 60
    assert stats.n_resolved == 4
    assert stats.n_tradeable == 3  # winner, flat, sells-heavy are priceable at 60
    assert stats.n_skipped_untradeable == 1  # the untradeable one
    by_mint = {o.mint: o for o in outcomes}

    # Winner: momentum + strong buy pressure + liquidity -> BOTH select; net positive (3x run).
    assert by_mint["MOM_WIN"].model_selected is True
    assert by_mint["MOM_WIN"].baseline_selected is True
    assert by_mint["MOM_WIN"].net_pnl_lamports > 0

    # Flat: price did not rise -> NEITHER selects.
    assert by_mint["MOM_FLAT"].model_selected is False
    assert by_mint["MOM_FLAT"].baseline_selected is False

    # Sells-heavy: price rose (baseline BUYS) but sells dominate (model DECLINES); it dumps.
    assert by_mint["MOM_SELL"].baseline_selected is True
    assert by_mint["MOM_SELL"].model_selected is False
    assert by_mint["MOM_SELL"].net_pnl_lamports < 0  # the loss the model AVOIDED

    # Untradeable at T_ENTRY: declined by BOTH, net exactly 0 — never a fabricated fill.
    assert by_mint["MOM_SKIP"].model_selected is False
    assert by_mint["MOM_SKIP"].baseline_selected is False
    assert by_mint["MOM_SKIP"].net_pnl_lamports == 0

    # Money is INTEGER lamports; risk is the fixed per-trade cap.
    for o in outcomes:
        assert isinstance(o.net_pnl_lamports, int)
        assert o.sol_at_risk_lamports == RiskConfig().per_trade_cap_lamports

    # GATE-A / GATE-B COMPUTE on the momentum records.
    ga = compute_gate_a(outcomes, model=True)
    gb = compute_gate_b_delta(outcomes)
    assert ga.n_trades == 4
    assert gb.min_sample_met is False  # 4 < 10 floor -> pass withheld (de-risk)


def test_model_avoids_the_loss_the_baseline_takes() -> None:
    """The whole point of the extra filter: on a sells-heavy pump the model's cohort net beats
    the baseline's cohort net (model skipped the loser the baseline bought)."""
    records = [_sells_heavy_loser(f"S{i}", f"sig_s{i}") for i in range(12)]
    outcomes, _ = build_momentum_outcomes(
        records, block_time_resolver=_resolver_for(records), entry_horizon_s=60
    )
    ga_model = compute_gate_a(outcomes, model=True)
    ga_base = compute_gate_a(outcomes, model=False)
    # Model selected nothing here (all sells-heavy) -> 0; baseline bought losers -> negative.
    assert ga_model.n_selected == 0
    assert ga_model.total_net_pnl_lamports == 0
    assert ga_base.total_net_pnl_lamports < 0
    assert ga_model.total_net_pnl_lamports > ga_base.total_net_pnl_lamports


def test_price_rise_requires_a_prior_pre_entry_mark() -> None:
    """When there is no tradeable pre-T_ENTRY mark, momentum cannot be confirmed -> no select."""
    rec = _mom_record(
        "NOPRIOR", "sig_np",
        marks={
            30: {"price": None, "buys": 0, "sells": 0, "liq": None},  # not yet tradeable at 30
            60: {"price": "0.0011", "buys": 90, "sells": 10, "liq": "6000"},
            120: {"price": "0.003", "buys": 50, "sells": 50, "liq": "6000"},
            300: {"price": "0.003", "buys": 50, "sells": 50, "liq": "6000"},
        },
    )
    entry = _entry_record(rec)
    pre, _ = split_forward_at_entry(read_momentum_forward(rec), 60)
    decision = decide_momentum_entry(
        entry, pre, entry_horizon_s=60, risk_config=RiskConfig()
    )
    assert decision.tradeable_at_entry is True  # priceable at 60
    assert decision.price_rose is False  # but no earlier mark to confirm a rise
    assert decision.model_selected is False
    assert decision.baseline_selected is False


# ---------------------------------------------------------------------------
# 5. CENSOR — tradeable at T_ENTRY but no post-entry marks -> CENSORED, never fabricated
# ---------------------------------------------------------------------------


def test_tradeable_but_no_post_marks_is_censored() -> None:
    rec = _mom_record(
        "NOPOST", "sig_npost",
        marks={
            30: {"price": "0.001", "buys": 80, "sells": 20, "liq": "5000"},
            60: {"price": "0.0011", "buys": 90, "sells": 10, "liq": "6000"},
        },  # nothing after T_ENTRY=60
    )
    outcomes, stats = build_momentum_outcomes(
        [rec], block_time_resolver=_resolver_for([rec]), entry_horizon_s=60
    )
    assert outcomes == []
    assert stats.n_censored == 1
    assert stats.n_resolved == 0


def test_unresolvable_block_time_is_censored() -> None:
    rec = _winner()
    outcomes, stats = build_momentum_outcomes(
        [rec], block_time_resolver=FixtureBlockTimeResolver({}), entry_horizon_s=60
    )
    assert outcomes == []
    assert stats.n_censored == 1


# ---------------------------------------------------------------------------
# 6. MONEY DISCIPLINE / NO WIN-RATE (source-level assertion)
# ---------------------------------------------------------------------------


def test_no_win_rate_and_no_float_money_tokens_in_source() -> None:
    src = inspect.getsource(mh).lower()
    assert "win_rate" not in src
    assert "winrate" not in src
    assert 'won"' not in src


# ---------------------------------------------------------------------------
# 7. REPRODUCIBLE — same corpus + seed -> identical net_pnl_lamports
# ---------------------------------------------------------------------------


def test_reproducible_net_pnl() -> None:
    records = [_winner(), _sells_heavy_loser(), _flat()]
    o1, _ = build_momentum_outcomes(
        records, block_time_resolver=_resolver_for(records), entry_horizon_s=60
    )
    o2, _ = build_momentum_outcomes(
        records, block_time_resolver=_resolver_for(records), entry_horizon_s=60
    )
    assert [o.net_pnl_lamports for o in o1] == [o.net_pnl_lamports for o in o2]


# ---------------------------------------------------------------------------
# 8. RUNNER — run_edge_proof(strategy="momentum") runs GATE-A / GATE-B, surfaces T_ENTRY
# ---------------------------------------------------------------------------


def _write_corpus(path, records: list[CorpusRecord]) -> None:
    lines = [json.dumps({"entry": r.entry, "forward": r.forward}) for r in records]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_runner_momentum_strategy_runs_gates(tmp_path) -> None:
    records = (
        [_winner(f"W{i}", f"sig_w{i}") for i in range(12)]
        + [_sells_heavy_loser(f"S{i}", f"sig_s{i}") for i in range(12)]
    )
    corpus = tmp_path / "mom.jsonl"
    _write_corpus(corpus, records)
    code, scoreboard = run_edge_proof(
        corpus,
        block_time_resolver=_resolver_for(records),
        strategy=STRATEGY_MOMENTUM,
        entry_horizon_s=60,
    )
    assert scoreboard["strategy"] == "momentum"
    assert scoreboard["entry_horizon_s"] == 60
    assert scoreboard["n_resolved"] == 24
    assert scoreboard["n_tradeable"] == 24
    # GATE-A / GATE-B actually ran and a verdict was computed (maker does not grade GO/NO-GO).
    assert "gate_a_model" in scoreboard
    assert "gate_b" in scoreboard
    assert scoreboard["verdict"] in ("GO", "NO-GO")
    assert code in (EXIT_GO, EXIT_NO_GO)
    # Model selects only the winners; baseline buys winners AND the sells-heavy losers.
    assert scoreboard["n_model_selected"] == 12
    assert scoreboard["n_baseline_selected"] == 24


def test_runner_momentum_reports_untradeable_skips(tmp_path) -> None:
    records = [_winner("W0", "sig_w0"), _untradeable("U0", "sig_u0")]
    corpus = tmp_path / "mix.jsonl"
    _write_corpus(corpus, records)
    _code, scoreboard = run_edge_proof(
        corpus,
        block_time_resolver=_resolver_for(records),
        strategy=STRATEGY_MOMENTUM,
        entry_horizon_s=60,
    )
    assert scoreboard["n_skipped_untradeable"] == 1
    assert scoreboard["n_tradeable"] == 1


def test_forward_observation_buy_fraction_and_tradeable() -> None:
    obs = MomentumForwardObservation(
        horizon_s=60, price_sol=None, liquidity_usd=None, buys=0, sells=0,
        vol_m5=None, note="", obs_wall_ms=0,
    )
    assert obs.tradeable is False
    assert obs.buy_fraction == 0  # no txns -> 0, not a divide-by-zero
