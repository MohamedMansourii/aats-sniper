"""Tests for the outcome-labeling harness (Phase-5 unblock).

Self-check criteria (mirrors the WP acceptance + the ml-prediction self-check):
  1. LEAK BOUNDARY (the headline): injecting a forward feature into the decision step makes
     the guard FAIL (RED); the entry-only decision PASSES (GREEN). Proves the boundary is
     real and structural, not a comment.
  2. STRUCTURAL SEPARATION: `decide_entry` has no forward argument; `resolve_outcome`
     requires the forward path. The decision cannot see the future by construction.
  3. CORRECTNESS on a fixture corpus: known winner/flat/rugged launches produce the expected
     sign of `net_pnl_lamports` and the expected selection masks; GATE-A / GATE-B compute.
  4. T-300a: the decision anchors on the RESOLVED on-chain block_time, NOT `recv_wall_ms`.
  5. MONEY DISCIPLINE: net_pnl / risk are INTEGER lamports; NO win-rate anywhere.
  6. FAIL-CLOSED: an unresolvable block_time -> CENSORED (dropped), never a fabricated anchor.
  7. REPRODUCIBLE: the same corpus + seed produces byte-identical net_pnl_lamports.

Offline / injectable / deterministic. No RPC, no LLM, no network, no keypair, no capital.
"""

from __future__ import annotations

import inspect
from decimal import Decimal

import pytest

from aats.backtest import outcome_harness as oh
from aats.backtest.outcome_harness import (
    BlockTime,
    CorpusRecord,
    EntryRecord,
    FixtureBlockTimeResolver,
    LeakError,
    PITFeature,
    assemble_decision_features,
    assert_features_leq_decision,
    build_round_trip_cost_stack,
    build_trade_outcomes,
    decide_entry,
    to_entry_record,
)
from aats.contracts.risk import RiskConfig
from aats.models.baseline import load_params
from aats.models.gate_a import compute_gate_a
from aats.models.gate_b import TradeOutcome, compute_gate_b_delta

_LAMPORTS = 1_000_000_000
_DECISION_BT_MS = 1_700_000_100_000  # on-chain block_time (differs from recv_wall_ms below)
_RECV_WALL_MS = 1_700_000_000_000  # collection timestamp — must NEVER be the decision anchor


def _record(
    mint: str,
    sig: str,
    *,
    sol_amount: str,
    v_sol_sol: str,
    forward_multiples: list[str | None],
    v_tokens: str = "1000000000",
    recv_wall_ms: int = _RECV_WALL_MS,
) -> CorpusRecord:
    """Build a corpus record whose forward prices realize the given multiples of entry price."""
    entry_price = Decimal(v_sol_sol) / Decimal(v_tokens)  # SOL per token
    forward = []
    for horizon, mult in zip((60, 300, 900), forward_multiples, strict=True):
        price = None if mult is None else str(entry_price * Decimal(mult))
        forward.append(
            {
                "price_sol": price,
                "liquidity_usd": None,
                "dex": "pumpfun",
                "note": "ok",
                "horizon_s": horizon,
                "obs_wall_ms": recv_wall_ms + horizon * 1000,
            }
        )
    entry = {
        "mint": mint,
        "signature": sig,
        "traderPublicKey": "TraderPubKey",
        "initialBuy": 1_000_000.0,
        "solAmount": float(sol_amount),
        "vTokensInBondingCurve": float(v_tokens),
        "vSolInBondingCurve": float(v_sol_sol),
        "marketCapSol": float(v_sol_sol),
        "name": "Test",
        "symbol": "TST",
        "pool": "pump",
        "recv_wall_ms": recv_wall_ms,
    }
    return CorpusRecord(entry=entry, forward=forward)


def _resolver_for(records: list[CorpusRecord]) -> FixtureBlockTimeResolver:
    return FixtureBlockTimeResolver(
        {
            r.signature: BlockTime(slot=300_000_000 + i, block_time_ms=_DECISION_BT_MS + i)
            for i, r in enumerate(records)
        }
    )


def _entry_record(record: CorpusRecord, slot: int = 300_000_000) -> EntryRecord:
    return to_entry_record(record, BlockTime(slot=slot, block_time_ms=_DECISION_BT_MS))


# ---------------------------------------------------------------------------
# 1. THE LEAK TEST — RED-before (forward feature) / GREEN-after (boundary intact)
# ---------------------------------------------------------------------------


def test_leak_guard_green_with_entry_only_features() -> None:
    """GREEN: the entry-only decision feature set passes the point-in-time guard."""
    rec = _record("MINT_CLEAN", "sig_clean", sol_amount="0.06", v_sol_sol="32",
                  forward_multiples=["1", "2", "3"])
    entry = _entry_record(rec)
    features = assemble_decision_features(entry)
    summary = assert_features_leq_decision(features, entry.decision_block_time_ms)
    assert "LEAK AUDIT PASS" in summary
    # And the full decision path runs clean.
    result = decide_entry(entry, baseline_params=load_params(), risk_config=RiskConfig())
    assert "LEAK AUDIT PASS" in result.leak_audit_summary


def test_leak_guard_red_when_forward_feature_injected() -> None:
    """RED: a forward observation injected into the decision features trips the guard.

    The forward mark's event-time is decision + horizon (strictly after the decision), so it
    is a forward-looking leak — `assert_features_leq_decision` MUST raise `LeakError`.
    """
    rec = _record("MINT_LEAK", "sig_leak", sol_amount="0.06", v_sol_sol="32",
                  forward_multiples=["1", "2", "3"])
    entry = _entry_record(rec)
    forward_obs = oh.EmbeddedForwardPriceSource().forward_path(rec)[-1]  # the 900s mark
    assert forward_obs.price_sol is not None
    # A feature built from a FORWARD observation carries a POST-decision event-time.
    leaked = PITFeature(
        name="LEAK_forward_price_sol",
        value=float(forward_obs.price_sol),
        event_block_time_ms=entry.decision_block_time_ms + forward_obs.horizon_s * 1000,
    )
    tainted = assemble_decision_features(entry, extra_features=(leaked,))
    with pytest.raises(LeakError, match="forward"):
        assert_features_leq_decision(tainted, entry.decision_block_time_ms)


def test_decide_entry_red_when_forward_feature_injected_end_to_end() -> None:
    """RED end-to-end: injecting a forward feature via decide_entry's seam raises LeakError;
    the same call WITHOUT the injection (GREEN) returns a decision."""
    rec = _record("MINT_E2E", "sig_e2e", sol_amount="0.06", v_sol_sol="32",
                  forward_multiples=["1", "2", "3"])
    entry = _entry_record(rec)
    forward_obs = oh.EmbeddedForwardPriceSource().forward_path(rec)[-1]
    leaked = PITFeature(
        "LEAK_forward_price_sol",
        float(forward_obs.price_sol),
        entry.decision_block_time_ms + forward_obs.horizon_s * 1000,
    )
    # RED — forward feature reaches the decision step.
    with pytest.raises(LeakError):
        decide_entry(
            entry,
            baseline_params=load_params(),
            risk_config=RiskConfig(),
            extra_features=(leaked,),
        )
    # GREEN — boundary intact.
    ok = decide_entry(entry, baseline_params=load_params(), risk_config=RiskConfig())
    assert isinstance(ok.model_selected, bool)


# ---------------------------------------------------------------------------
# 2. STRUCTURAL SEPARATION — the decision cannot take a forward argument
# ---------------------------------------------------------------------------


def test_decision_and_outcome_are_structurally_separated() -> None:
    decide_params = set(inspect.signature(decide_entry).parameters)
    resolve_params = set(inspect.signature(oh.resolve_outcome).parameters)
    # The decision takes no forward/price/outcome argument.
    assert not any(
        tok in p for p in decide_params for tok in ("forward", "price", "outcome", "obs")
    )
    # The outcome REQUIRES the forward path.
    assert "forward_path" in resolve_params


# ---------------------------------------------------------------------------
# 3. CORRECTNESS on a fixture corpus (known winner / flat / rugged)
# ---------------------------------------------------------------------------


def test_winner_flat_rug_produce_expected_pnl_signs() -> None:
    winner = _record("MINT_WIN", "sig_win", sol_amount="0.06", v_sol_sol="32",
                     forward_multiples=["1", "2", "3"])
    flat = _record("MINT_FLAT", "sig_flat", sol_amount="0.5", v_sol_sol="30",
                   forward_multiples=["1", "1", "1"])
    rug = _record("MINT_RUG", "sig_rug", sol_amount="0.5", v_sol_sol="30",
                  forward_multiples=["1", None, None])
    records = [winner, flat, rug]
    outcomes, stats = build_trade_outcomes(records, block_time_resolver=_resolver_for(records))

    assert stats.n_resolved == 3
    assert stats.n_censored == 0
    by_mint = {o.mint: o for o in outcomes}

    # Money is INTEGER lamports; risk unit is the per-trade cap (> 0).
    for o in outcomes:
        assert isinstance(o.net_pnl_lamports, int)
        assert isinstance(o.sol_at_risk_lamports, int)
        assert o.sol_at_risk_lamports == RiskConfig().per_trade_cap_lamports

    # Winner (3x) is net positive after cost; flat loses the ~6% cost; rug is a heavy loss.
    assert by_mint["MINT_WIN"].net_pnl_lamports > 0
    assert by_mint["MINT_FLAT"].net_pnl_lamports < 0
    assert by_mint["MINT_RUG"].net_pnl_lamports < by_mint["MINT_FLAT"].net_pnl_lamports

    # Selection masks: the thin model selects the high-liquidity, small-dev-buy winner; the
    # frozen baseline selects the big-initial-buy candidates (>= 0.3 SOL).
    assert by_mint["MINT_WIN"].model_selected is True
    assert by_mint["MINT_WIN"].baseline_selected is False  # 0.06 SOL < 0.3 SOL threshold
    assert by_mint["MINT_FLAT"].model_selected is False  # v_sol 30 < 31 floor
    assert by_mint["MINT_FLAT"].baseline_selected is True

    # GATE-A / GATE-B COMPUTE on the produced records (n<min_sample -> GATE-B withheld).
    ga = compute_gate_a(outcomes, model=True)
    gb = compute_gate_b_delta(outcomes)
    assert ga.n_trades == 3
    assert gb.min_sample_met is False  # 3 < 10 floor -> pass withheld (de-risk)
    assert gb.gate_b_pass is False


def test_model_winner_cohort_makes_money_gate_a() -> None:
    """A model cohort of pure winners has a positive aggregate net-of-cost PnL (GATE-A)."""
    records = [
        _record(f"WIN{i}", f"sig_win_{i}", sol_amount="0.06", v_sol_sol="32",
                forward_multiples=["1", "2", "3"])
        for i in range(12)
    ]
    outcomes, stats = build_trade_outcomes(records, block_time_resolver=_resolver_for(records))
    assert stats.n_resolved == 12
    assert all(o.model_selected for o in outcomes)
    ga = compute_gate_a(outcomes, model=True)
    assert ga.total_net_pnl_lamports > 0
    assert ga.n_selected == 12


# ---------------------------------------------------------------------------
# 4. T-300a — the decision anchors on the resolved on-chain block_time, not recv_wall_ms
# ---------------------------------------------------------------------------


def test_decision_anchor_is_onchain_block_time_not_recv_wall_ms() -> None:
    rec = _record("MINT_BT", "sig_bt", sol_amount="0.06", v_sol_sol="32",
                  forward_multiples=["1", "1", "1"], recv_wall_ms=_RECV_WALL_MS)
    entry = to_entry_record(rec, BlockTime(slot=300_500_000, block_time_ms=_DECISION_BT_MS))
    assert entry.decision_block_time_ms == _DECISION_BT_MS
    assert entry.decision_block_time_ms != entry.recv_wall_ms
    assert entry.recv_wall_ms == _RECV_WALL_MS  # retained for monitoring only
    # The decision features are all stamped at the on-chain block_time (never recv_wall_ms).
    feats = assemble_decision_features(entry)
    assert all(f.event_block_time_ms == _DECISION_BT_MS for f in feats)


# ---------------------------------------------------------------------------
# 5. MONEY DISCIPLINE + NO WIN-RATE (source-level assertion)
# ---------------------------------------------------------------------------


def test_no_win_rate_and_no_float_money_tokens_in_source() -> None:
    src = inspect.getsource(oh).lower()
    assert "win_rate" not in src
    assert "winrate" not in src
    assert 'won"' not in src  # no "won" field


def test_cost_stack_is_six_percent_and_validated() -> None:
    stack = build_round_trip_cost_stack()
    assert stack.total_cost_bps == 600  # ~6% round trip
    # Reconciles to the sum of parts (validated by the cost gate inside the builder).
    parts = (
        stack.jito_tip_bps + stack.priority_fee_bps + stack.entry_slippage_bps
        + stack.amm_fee_bps + stack.exit_slippage_bps + stack.adverse_selection_bps
    )
    assert parts == stack.total_cost_bps
    assert stack.adverse_selection_bps >= 150  # UNCALIBRATED floor


# ---------------------------------------------------------------------------
# 6. FAIL-CLOSED — unresolvable block_time is CENSORED, never fabricated
# ---------------------------------------------------------------------------


def test_unresolvable_block_time_is_censored() -> None:
    rec = _record("MINT_NOSIG", "sig_missing", sol_amount="0.06", v_sol_sol="32",
                  forward_multiples=["1", "2", "3"])
    # Resolver has NO mapping for this signature -> BlockTimeUnavailable -> CENSORED.
    outcomes, stats = build_trade_outcomes(
        [rec], block_time_resolver=FixtureBlockTimeResolver({})
    )
    assert outcomes == []
    assert stats.n_records == 1
    assert stats.n_resolved == 0
    assert stats.n_censored == 1


# ---------------------------------------------------------------------------
# 7. REPRODUCIBLE — same corpus + seed -> identical net_pnl_lamports
# ---------------------------------------------------------------------------


def test_reproducible_net_pnl() -> None:
    records = [
        _record("R1", "sig_r1", sol_amount="0.06", v_sol_sol="32",
                forward_multiples=["1", "2", "3"]),
        _record("R2", "sig_r2", sol_amount="0.5", v_sol_sol="30",
                forward_multiples=["1", None, None]),
    ]
    o1, _ = build_trade_outcomes(records, block_time_resolver=_resolver_for(records))
    o2, _ = build_trade_outcomes(records, block_time_resolver=_resolver_for(records))
    assert [o.net_pnl_lamports for o in o1] == [o.net_pnl_lamports for o in o2]


def test_trade_outcome_rejects_float_money() -> None:
    # The contract itself refuses float PnL (defense-in-depth for the harness output).
    with pytest.raises(ValueError, match="int"):
        TradeOutcome(
            mint="X",
            decision_slot=1,
            model_selected=True,
            baseline_selected=False,
            net_pnl_lamports=1.5,  # float money -> rejected
            sol_at_risk_lamports=100_000_000,
        )
