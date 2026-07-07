"""Tests for the REALIZABLE-EXIT outcome model (perf/realism — outcome-only, never the decision).

Self-check criteria (mirrors the ml-prediction self-check + the task's HARD CONSTRAINTS):
  1. CONSERVATIVE-VS-SPOT INVARIANT (the headline): for the SAME path, realizable net_pnl <=
     spot net_pnl ALWAYS (realism can only LOWER PnL, never inflate it) — winner / flat / rug /
     thin / honeypot paths, both the launch harness and the momentum harness.
  2. HONEYPOT -> LOSS: a path priced at spot but with null / sub-floor liquidity (never sellable)
     is a near-total LOSS under realizable (you cannot exit at a price you could not realize),
     even though spot books it as a gain.
  3. SLIPPAGE MONOTONE: the slippage fraction is non-decreasing in position notional and
     non-increasing in pool liquidity (and capped).
  4. FROZEN-HASH: the frozen realism constants have not drifted (anti-p-hacking); a mutated
     `params` FAILS the canonical-hash check.
  5. DECISION / LEAK BOUNDARY UNCHANGED: the exit_model does not alter selection masks; the
     realism layer reads only OUTCOME marks.
  6. TOGGLE: spot mode reproduces the pre-realism (optimistic) fill; realizable is the default.
  7. MONEY DISCIPLINE: net_pnl / deduction are INTEGER lamports; constants are Decimal.

Offline / deterministic. No RPC, no LLM, no network, no keypair, no capital.
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from aats.backtest import realizable_exit as re
from aats.backtest.momentum_harness import build_momentum_outcomes
from aats.backtest.outcome_harness import (
    BlockTime,
    CorpusRecord,
    FixtureBlockTimeResolver,
    build_trade_outcomes,
)
from aats.backtest.realizable_exit import (
    EXIT_MODEL_REALIZABLE,
    EXIT_MODEL_SPOT,
    REALIZABLE_PARAMS,
    RealizableExitError,
    _canonical_realizable_hash,
    compute_realizable_adjustment,
    load_realizable_params,
    realizable_gross_pnl,
    slippage_fraction,
)
from aats.contracts.risk import RiskConfig

_DECISION_BT_MS = 1_700_000_100_000
_RECV_WALL_MS = 1_700_000_000_000
_LAMPORTS = 1_000_000_000


# ---------------------------------------------------------------------------
# Corpus fixture builder (launch harness): forward marks with price + liquidity + USD
# ---------------------------------------------------------------------------


def _record(
    mint: str,
    sig: str,
    *,
    sol_amount: str = "0.06",
    v_sol_sol: str = "32",
    v_tokens: str = "1000000000",
    forward_multiples: list[str | None],
    liquidity_usd: str | None = "5000",
    sol_usd: str = "150",
) -> CorpusRecord:
    entry_price = Decimal(v_sol_sol) / Decimal(v_tokens)
    forward = []
    for horizon, mult in zip((60, 300, 900), forward_multiples, strict=True):
        if mult is None:
            price = None
            price_usd = None
        else:
            price = str(entry_price * Decimal(mult))
            price_usd = str(entry_price * Decimal(mult) * Decimal(sol_usd))
        forward.append(
            {
                "price_sol": price,
                "price_usd": price_usd,
                "liquidity_usd": liquidity_usd,
                "dex": "pumpfun",
                "note": "ok",
                "horizon_s": horizon,
                "obs_wall_ms": _RECV_WALL_MS + horizon * 1000,
            }
        )
    entry = {
        "mint": mint,
        "signature": sig,
        "traderPublicKey": "T",
        "initialBuy": 1_000_000.0,
        "solAmount": float(sol_amount),
        "vTokensInBondingCurve": float(v_tokens),
        "vSolInBondingCurve": float(v_sol_sol),
        "marketCapSol": float(v_sol_sol),
        "name": "n",
        "symbol": "s",
        "pool": "pump",
        "recv_wall_ms": _RECV_WALL_MS,
    }
    return CorpusRecord(entry=entry, forward=forward)


def _resolver_for(records: list[CorpusRecord]) -> FixtureBlockTimeResolver:
    return FixtureBlockTimeResolver(
        {
            r.signature: BlockTime(slot=300_000_000 + i, block_time_ms=_DECISION_BT_MS + i)
            for i, r in enumerate(records)
        }
    )


def _by_mint(records: list[CorpusRecord], *, exit_model: str) -> dict:
    outs, _ = build_trade_outcomes(
        records, block_time_resolver=_resolver_for(records), exit_model=exit_model
    )
    return {o.mint: o for o in outs}


# ---------------------------------------------------------------------------
# 1. THE CONSERVATIVE INVARIANT — realizable net_pnl <= spot net_pnl (same path)
# ---------------------------------------------------------------------------


def test_realizable_never_exceeds_spot_launch_harness() -> None:
    """For EVERY path shape, realizable net_pnl <= spot net_pnl (realism only lowers PnL)."""
    records = [
        _record("WIN", "s_win", forward_multiples=["1", "2", "3"]),          # winner
        _record("FLAT", "s_flat", forward_multiples=["1", "1", "1"]),        # flat
        _record("RUG", "s_rug", forward_multiples=["1", None, None]),        # rug (null marks)
        _record("THIN", "s_thin", forward_multiples=["1", "5", "9"], liquidity_usd="600"),  # thin
        _record("HONEY", "s_honey", forward_multiples=["1", "5", "9"], liquidity_usd=None),  # honeypot
        _record("SUBFLOOR", "s_sub", forward_multiples=["1", "5", "9"], liquidity_usd="100"),  # < floor
    ]
    spot = _by_mint(records, exit_model=EXIT_MODEL_SPOT)
    real = _by_mint(records, exit_model=EXIT_MODEL_REALIZABLE)
    for mint in spot:
        assert real[mint].net_pnl_lamports <= spot[mint].net_pnl_lamports, (
            f"{mint}: realizable {real[mint].net_pnl_lamports} > spot "
            f"{spot[mint].net_pnl_lamports} — realism INFLATED PnL (invariant breach)"
        )


def test_realizable_strictly_lower_on_thin_and_honeypot() -> None:
    """On thin / honeypot paths that spot over-values, realizable is STRICTLY lower (non-vacuous)."""
    records = [
        _record("THIN", "s_thin", forward_multiples=["1", "5", "9"], liquidity_usd="600"),
        _record("HONEY", "s_honey", forward_multiples=["1", "5", "9"], liquidity_usd=None),
    ]
    spot = _by_mint(records, exit_model=EXIT_MODEL_SPOT)
    real = _by_mint(records, exit_model=EXIT_MODEL_REALIZABLE)
    # THIN: above-floor but thin liquidity -> a real (non-zero) slippage haircut.
    assert real["THIN"].net_pnl_lamports < spot["THIN"].net_pnl_lamports
    # HONEY: null liquidity -> honeypot near-total loss, far below the optimistic spot gain.
    assert real["HONEY"].net_pnl_lamports < spot["HONEY"].net_pnl_lamports


def test_realizable_never_exceeds_spot_momentum_harness() -> None:
    """Same invariant on the MOMENTUM harness (post-T_ENTRY outcome marks)."""

    def _mom(mint, sig, marks):
        forward = []
        for h in sorted(marks):
            m = marks[h]
            forward.append(
                {
                    "horizon_s": h,
                    "price_sol": m.get("price"),
                    "price_usd": m.get("price_usd"),
                    "liquidity_usd": m.get("liq"),
                    "vol_m5": None,
                    "txns_m5": {"buys": m.get("buys", 0), "sells": m.get("sells", 0)},
                    "txns_h1": None,
                    "note": "",
                    "obs_wall_ms": _RECV_WALL_MS + h * 1000,
                }
            )
        entry = {
            "mint": mint, "signature": sig, "traderPublicKey": "T", "initialBuy": 1e6,
            "solAmount": 0.5, "vTokensInBondingCurve": 1e9, "vSolInBondingCurve": 30.0,
            "marketCapSol": 30.0, "name": "n", "symbol": "s", "pool": "pump",
            "recv_wall_ms": _RECV_WALL_MS,
        }
        return CorpusRecord(entry=entry, forward=forward)

    records = [
        _mom("MWIN", "m_win", {
            30: {"price": "0.001", "buys": 80, "sells": 20, "liq": "5000"},
            60: {"price": "0.0011", "buys": 90, "sells": 10, "liq": "6000"},
            120: {"price": "0.0020", "buys": 70, "sells": 30, "liq": "7000"},
            300: {"price": "0.0030", "buys": 60, "sells": 40, "liq": "8000"},
        }),
        _mom("MHONEY", "m_honey", {
            30: {"price": "0.001", "buys": 80, "sells": 20, "liq": "5000"},
            60: {"price": "0.0011", "buys": 90, "sells": 10, "liq": "6000"},
            120: {"price": "0.0050", "buys": 70, "sells": 30, "liq": None},  # priced but no liq
            300: {"price": "0.0090", "buys": 60, "sells": 40, "liq": None},
        }),
    ]
    resolver = _resolver_for(records)
    spot, _ = build_momentum_outcomes(
        records, block_time_resolver=resolver, entry_horizon_s=60, exit_model=EXIT_MODEL_SPOT
    )
    real, _ = build_momentum_outcomes(
        records, block_time_resolver=resolver, entry_horizon_s=60, exit_model=EXIT_MODEL_REALIZABLE
    )
    spot_m = {o.mint: o for o in spot}
    real_m = {o.mint: o for o in real}
    for mint in spot_m:
        assert real_m[mint].net_pnl_lamports <= spot_m[mint].net_pnl_lamports
    # The post-T_ENTRY honeypot (all post marks unpriced-liquidity) is a near-total loss.
    assert real_m["MHONEY"].net_pnl_lamports < 0
    assert real_m["MHONEY"].net_pnl_lamports < spot_m["MHONEY"].net_pnl_lamports


# ---------------------------------------------------------------------------
# 2. HONEYPOT -> LOSS (near-total; spot over-values it)
# ---------------------------------------------------------------------------


def test_honeypot_path_is_near_total_loss() -> None:
    """A path spot books as a big winner but whose marks are ALL unsellable -> near-total loss."""
    rec = _record("HONEY", "s_h", forward_multiples=["2", "5", "10"], liquidity_usd=None)
    spot = _by_mint([rec], exit_model=EXIT_MODEL_SPOT)["HONEY"]
    real = _by_mint([rec], exit_model=EXIT_MODEL_REALIZABLE)["HONEY"]
    risk = RiskConfig().per_trade_cap_lamports
    # Spot sees a large gain (10x); realizable sees a near-total loss (bag-held, never sellable).
    assert spot.net_pnl_lamports > 0
    assert real.net_pnl_lamports < 0
    # Near-total: with honeypot_residual_bps == 0 you lose the whole notional plus entry-side cost.
    assert real.net_pnl_lamports <= -risk


def test_honeypot_flag_and_deduction_surface_in_outcome_result() -> None:
    """The realizable adjustment reports sellable=False + a positive deduction for a honeypot."""
    from aats.backtest.outcome_harness import (
        EmbeddedForwardPriceSource,
        build_round_trip_cost_stack,
        resolve_outcome,
        to_entry_record,
    )

    rec = _record("HONEY", "s_h", forward_multiples=["2", "5", "10"], liquidity_usd=None)
    entry = to_entry_record(rec, BlockTime(slot=1, block_time_ms=_DECISION_BT_MS))
    fwd = EmbeddedForwardPriceSource().forward_path(rec)
    res = resolve_outcome(
        entry, fwd, cost_stack=build_round_trip_cost_stack(),
        sol_at_risk_lamports=RiskConfig().per_trade_cap_lamports,
        exit_model=EXIT_MODEL_REALIZABLE,
    )
    assert res.sellable_market_existed is False
    assert res.realizable_deduction_lamports > 0
    # Realizable gross <= the spot gross recorded alongside it.
    assert res.gross_pnl_lamports <= res.spot_gross_pnl_lamports


# ---------------------------------------------------------------------------
# 3. SLIPPAGE MONOTONE in position notional and pool liquidity (capped)
# ---------------------------------------------------------------------------


def test_slippage_monotone_increasing_in_notional() -> None:
    p = REALIZABLE_PARAMS
    liq = Decimal("10000")
    prev = Decimal(-1)
    for notional in (Decimal("10"), Decimal("100"), Decimal("1000"), Decimal("5000")):
        s = slippage_fraction(notional, liq, p)
        assert s >= prev  # non-decreasing in position size
        assert s <= p.max_slippage_fraction  # capped
        prev = s


def test_slippage_monotone_decreasing_in_liquidity() -> None:
    p = REALIZABLE_PARAMS
    notional = Decimal("100")
    prev = Decimal("2")  # above the cap
    for liq in (Decimal("100"), Decimal("1000"), Decimal("10000"), Decimal("100000")):
        s = slippage_fraction(notional, liq, p)
        assert s <= prev  # non-increasing as liquidity deepens
        prev = s


def test_slippage_hits_cap_when_notional_dominates_pool() -> None:
    p = REALIZABLE_PARAMS
    # notional >> liquidity -> the linear impact would exceed 1; the cap binds.
    s = slippage_fraction(Decimal("100000"), Decimal("100"), p)
    assert s == p.max_slippage_fraction


def test_adjustment_deduction_grows_as_liquidity_thins() -> None:
    """The realizable deduction on identical spot proceeds grows as the sellable book thins."""
    notional = RiskConfig().per_trade_cap_lamports
    proceeds = 200_000_000  # 0.2 SOL of spot proceeds

    def _mark(liq: str | None):
        # Minimal duck-typed mark with the three fields the layer reads.
        class _M:
            price_sol = Decimal("0.001")
            price_usd = Decimal("0.15")  # -> sol_usd = 150
            liquidity_usd = None if liq is None else Decimal(liq)
        return _M()

    deep = compute_realizable_adjustment([_mark("100000")], notional, proceeds, REALIZABLE_PARAMS)
    thin = compute_realizable_adjustment([_mark("600")], notional, proceeds, REALIZABLE_PARAMS)
    honey = compute_realizable_adjustment([_mark(None)], notional, proceeds, REALIZABLE_PARAMS)
    assert deep.deduction_lamports <= thin.deduction_lamports < honey.deduction_lamports
    assert honey.sellable_market_existed is False
    assert deep.sellable_market_existed is True


# ---------------------------------------------------------------------------
# 4. FROZEN-HASH drift guard (anti-p-hacking)
# ---------------------------------------------------------------------------


def test_frozen_params_load_and_hash_matches() -> None:
    p = load_realizable_params()
    # The live canonical hash equals the stored frozen_hash (no drift).
    raw = json.loads(re._REALIZABLE_PARAMS_PATH.read_text(encoding="utf-8"))
    assert p.params_hash == raw["frozen_hash"]
    assert p.params_hash == _canonical_realizable_hash(raw["params"])


def test_frozen_hash_fails_on_drift(tmp_path) -> None:
    """Mutating a frozen realism constant makes load FAIL (p-hacking the outcome is caught)."""
    raw = json.loads(re._REALIZABLE_PARAMS_PATH.read_text(encoding="utf-8"))
    raw["params"]["linear_impact_coeff"] = "0.5"  # retune -> hash no longer matches frozen_hash
    drifted = tmp_path / "drifted.json"
    drifted.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(RealizableExitError, match="changed_after_freeze"):
        load_realizable_params(drifted)


def test_frozen_params_reject_float_money(tmp_path) -> None:
    raw = json.loads(re._REALIZABLE_PARAMS_PATH.read_text(encoding="utf-8"))
    raw["params"]["max_slippage_fraction"] = 0.9  # float, not a Decimal-string
    raw["frozen_hash"] = None  # skip the drift branch to reach the float check
    bad = tmp_path / "float.json"
    bad.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(RealizableExitError, match="float"):
        load_realizable_params(bad)


# ---------------------------------------------------------------------------
# 5. DECISION / LEAK BOUNDARY UNCHANGED — exit_model does not touch selection
# ---------------------------------------------------------------------------


def test_exit_model_does_not_change_selection_masks() -> None:
    """The selection (model/baseline) is identical under spot and realizable — outcome-only."""
    records = [
        _record("WIN", "s_win", forward_multiples=["1", "2", "3"]),
        _record("FLAT", "s_flat", sol_amount="0.5", v_sol_sol="30", forward_multiples=["1", "1", "1"]),
        _record("HONEY", "s_h", forward_multiples=["2", "5", "10"], liquidity_usd=None),
    ]
    spot = _by_mint(records, exit_model=EXIT_MODEL_SPOT)
    real = _by_mint(records, exit_model=EXIT_MODEL_REALIZABLE)
    for mint in spot:
        assert spot[mint].model_selected == real[mint].model_selected
        assert spot[mint].baseline_selected == real[mint].baseline_selected


# ---------------------------------------------------------------------------
# 6. TOGGLE — spot reproduces the optimistic fill; unknown mode fails loud
# ---------------------------------------------------------------------------


def test_spot_mode_is_optimistic_no_deduction() -> None:
    """Spot mode returns the walk's spot gross unchanged (no realism deduction)."""
    gross, adj = realizable_gross_pnl(
        spot_gross_lamports=123_456,
        spot_proceeds_lamports=1_000_000,
        marks=[],
        notional_lamports=RiskConfig().per_trade_cap_lamports,
        exit_model=EXIT_MODEL_SPOT,
    )
    assert gross == 123_456
    assert adj is None


def test_unknown_exit_model_fails_loud() -> None:
    with pytest.raises(ValueError, match="unknown exit_model"):
        realizable_gross_pnl(
            spot_gross_lamports=0, spot_proceeds_lamports=0, marks=[],
            notional_lamports=1, exit_model="optimistic_lie",
        )


def test_realizable_is_the_default_mode() -> None:
    """The build path defaults to realizable (trustworthy), not spot."""
    rec = _record("HONEY", "s_h", forward_multiples=["2", "5", "10"], liquidity_usd=None)
    default = _by_mint([rec], exit_model=EXIT_MODEL_REALIZABLE)["HONEY"]
    outs, _ = build_trade_outcomes([rec], block_time_resolver=_resolver_for([rec]))  # no exit_model
    assert outs[0].net_pnl_lamports == default.net_pnl_lamports  # default == realizable


# ---------------------------------------------------------------------------
# 7. MONEY DISCIPLINE — integer lamports, Decimal constants, no float / win-rate
# ---------------------------------------------------------------------------


def test_deduction_and_pnl_are_integer_lamports() -> None:
    rec = _record("WIN", "s_win", forward_multiples=["1", "2", "3"], liquidity_usd="600")
    out = _by_mint([rec], exit_model=EXIT_MODEL_REALIZABLE)["WIN"]
    assert isinstance(out.net_pnl_lamports, int)

    # A thin sellable book on 0.5 SOL of spot proceeds -> an integer-lamport deduction < proceeds.
    def _mark():
        class _M:
            price_sol = Decimal("0.001")
            price_usd = Decimal("0.15")
            liquidity_usd = Decimal("600")
        return _M()

    gross, adj = realizable_gross_pnl(
        spot_gross_lamports=400_000_000,
        spot_proceeds_lamports=500_000_000,
        marks=[_mark()],
        notional_lamports=RiskConfig().per_trade_cap_lamports,
        exit_model=EXIT_MODEL_REALIZABLE,
    )
    assert isinstance(gross, int)
    assert adj is not None and isinstance(adj.deduction_lamports, int)
    assert 0 < adj.deduction_lamports < 500_000_000  # a real, bounded haircut


def test_frozen_constants_are_decimal_not_float() -> None:
    p = REALIZABLE_PARAMS
    for val in (
        p.linear_impact_coeff, p.max_slippage_fraction, p.min_liquidity_usd_floor,
        p.honeypot_residual_bps, p.sol_usd_fallback,
    ):
        assert isinstance(val, Decimal)


def test_no_win_rate_in_source() -> None:
    import inspect

    src = inspect.getsource(re).lower()
    assert "win_rate" not in src
    assert "winrate" not in src


def test_reproducible_realizable_pnl() -> None:
    records = [
        _record("A", "sa", forward_multiples=["1", "2", "3"], liquidity_usd="600"),
        _record("B", "sb", forward_multiples=["1", None, None]),
    ]
    o1 = _by_mint(records, exit_model=EXIT_MODEL_REALIZABLE)
    o2 = _by_mint(records, exit_model=EXIT_MODEL_REALIZABLE)
    assert {m: o.net_pnl_lamports for m, o in o1.items()} == {
        m: o.net_pnl_lamports for m, o in o2.items()
    }
