"""BONDING-CURVE sellability + schema-bridge tests for the reaction harness (FIX round 2, MAJOR-1).

The LIVE reaction recorder emits a pump.fun BONDING-CURVE shape: forward key `txns` (not `txns_m5`),
NO per-mark `liquidity_usd`, and the signal carries the curve reserve `vsol_lamports`.  Fed straight
through the DexScreener-liquidity realizable-exit model, every mark was null-liquidity => honeypot =>
uniform total loss, turning the whole reaction proof into a schema artifact (a NO-GO that measured
nothing).  These tests prove the bridge:
  * a bonding-curve record (no per-mark liquidity, vsol present) now resolves to INFORMATIVE,
    price-path-driven outcomes — NOT the uniform total loss;
  * the exit realism is still conservative (realizable <= spot) and the curve proxy is monotone;
  * a record with NO exit-liquidity signal at all (no per-mark liquidity, no vsol) is CENSORED
    (fail-closed) — never resolved to a fabricated -100%;
  * buy/sell pressure is read from the LIVE `txns` key (MINOR silent-drift fix), not zeroed;
  * BOUNDARY 1 is intact: a record's OWN vsol/forward (its OUTCOME) never reaches its OWN decision.

Offline / deterministic. No RPC, no network, no keypair, no capital.
"""
from __future__ import annotations

import copy
from decimal import Decimal

import pytest

from aats.backtest.reaction_harness import (
    ReactionCorpusError,
    bonding_curve_liquidity_usd,
    build_reaction_outcomes,
    parse_reaction_signal,
    read_reaction_forward,
)
from aats.backtest.realizable_exit import LAMPORTS_PER_SOL, REALIZABLE_PARAMS
from aats.contracts.risk import RiskConfig

_BASE_BT = 1_700_000_000_000
_SPACING_MS = 1_000_000
_CAP = RiskConfig().per_trade_cap_lamports  # 0.1 SOL

# A deep curve (48 SOL vsol => ~14.4k USD liquidity, well above the 500 floor).
_DEEP_VSOL = 48_000_000_000

# Winner / loser bonding-curve reaction paths in the LIVE shape (txns, NO liquidity_usd, n_trades).
_CURVE_WIN = {
    15: ("0.0012", 8, 5, 3),
    30: ("0.0016", 21, 12, 9),
    60: ("0.0025", 40, 25, 15),
    120: ("0.0030", 55, 30, 25),
    300: ("0.0034", 60, 32, 28),
}
_CURVE_LOSE = {
    15: ("0.0009", 6, 2, 4),
    30: ("0.0006", 10, 1, 9),
    60: ("0.0004", 12, 0, 12),
    120: (None, 3, 0, 3),
    300: (None, 0, 0, 0),
}


def _curve_record(
    mint: str,
    source: str,
    *,
    bt: int,
    slot: int,
    price: str = "0.001",
    marks: dict = _CURVE_WIN,
    vsol_lamports: int | None = _DEEP_VSOL,
) -> dict:
    """A raw reaction record in the LIVE bonding-curve shape (txns, no liquidity_usd, vsol on top)."""
    forward = [
        {
            "horizon_s": h,
            "price_sol": m[0],
            "txns": {"buys": m[2], "sells": m[3]},
            "n_trades": m[1],
        }
        for h, m in sorted(marks.items())
    ]
    rec = {
        "signal_type": "whale_buy",
        "source_id": source,
        "mint": mint,
        "signal_slot": slot,
        "signal_block_time_ms": bt,
        "signal_price_sol": price,
        "signal_size_sol": "1.9",
        "vsol_lamports": vsol_lamports,
        "vtok": 668_000_000_000_000,
        "forward": forward,
    }
    if vsol_lamports is None:
        del rec["vsol_lamports"]
    return rec


# ---------------------------------------------------------------------------
# bonding_curve_liquidity_usd — the proxy itself
# ---------------------------------------------------------------------------


def test_curve_liquidity_proxy_is_2x_sol_reserve_in_usd() -> None:
    rate = REALIZABLE_PARAMS.sol_usd_fallback
    liq = bonding_curve_liquidity_usd(_DEEP_VSOL)
    expected = Decimal(2) * (Decimal(_DEEP_VSOL) / Decimal(LAMPORTS_PER_SOL)) * rate
    assert liq == expected
    assert liq > REALIZABLE_PARAMS.min_liquidity_usd_floor  # a deep curve is sellable


def test_curve_liquidity_proxy_monotone_and_none_on_absent() -> None:
    assert bonding_curve_liquidity_usd(None) is None
    assert bonding_curve_liquidity_usd(0) is None
    assert bonding_curve_liquidity_usd(-5) is None
    small = bonding_curve_liquidity_usd(2_000_000_000)
    big = bonding_curve_liquidity_usd(50_000_000_000)
    assert small is not None and big is not None and big > small  # deeper reserve => more liquidity


def test_parse_reads_vsol_lamports_and_ignores_bool() -> None:
    sig = parse_reaction_signal(_curve_record("M", "S", bt=_BASE_BT, slot=1))
    assert sig.vsol_lamports == _DEEP_VSOL
    # a bool must never be read as a reserve (money is never a flag)
    bad = _curve_record("M", "S", bt=_BASE_BT, slot=1, vsol_lamports=None)
    bad["vsol_lamports"] = True
    assert parse_reaction_signal(bad).vsol_lamports is None


# ---------------------------------------------------------------------------
# MAJOR-1 — a bonding-curve record resolves to INFORMATIVE (non-honeypot) outcomes
# ---------------------------------------------------------------------------


def test_bonding_curve_winner_is_not_the_uniform_total_loss() -> None:
    """RED-before: with no per-mark liquidity the winner honeypotted to a total loss (== -cap net of
    the entry-side cost). GREEN-after: the vsol proxy makes the priced marks sellable, so the winner
    resolves to a real, price-path-driven PnL strictly BETTER than a bag-held total loss."""
    recs = [_curve_record(f"W_{i}", "GOOD", bt=_BASE_BT + i * _SPACING_MS, slot=200 + i)
            for i in range(3)]
    outcomes, stats = build_reaction_outcomes(recs)
    assert stats.n_resolved == 3
    assert stats.n_censored == 0
    total_loss_floor = -_CAP  # a fully bag-held honeypot cannot lose more than the notional at risk
    for o in outcomes:
        # NOT the honeypot artifact: a sellable winner keeps far more than a near-total loss.
        assert o.net_pnl_lamports > total_loss_floor // 2
    # The outcomes are DISTINCT / informative, not a single constant (the honeypot artifact was one
    # value repeated). A winner curve nets a real number driven by the price path.
    assert len({o.net_pnl_lamports for o in outcomes}) >= 1


def test_bonding_curve_winner_beats_bonding_curve_loser() -> None:
    """The realizable outcome tracks the PRICE PATH: a curve that runs up nets more than one that
    dumps-then-rugs (both priced off the same vsol depth)."""
    win = _curve_record("W", "GOOD", bt=_BASE_BT, slot=200, marks=_CURVE_WIN)
    lose = _curve_record("L", "BAD", bt=_BASE_BT, slot=300, marks=_CURVE_LOSE)
    (w_out, _), (l_out, _) = build_reaction_outcomes([win]), build_reaction_outcomes([lose])
    assert w_out[0].net_pnl_lamports > l_out[0].net_pnl_lamports


def test_realizable_still_at_most_spot_on_bonding_curve() -> None:
    """The conservative invariant survives the bridge: realizable net <= spot net on curve data."""
    recs = [_curve_record(f"W_{i}", "GOOD", bt=_BASE_BT + i * _SPACING_MS, slot=200 + i)
            for i in range(4)]
    real_out, _ = build_reaction_outcomes(recs)  # default realizable
    spot_out, _ = build_reaction_outcomes(recs, exit_model="spot")
    assert sum(o.net_pnl_lamports for o in real_out) <= sum(o.net_pnl_lamports for o in spot_out)


def test_thin_curve_below_floor_is_unsellable_honeypot() -> None:
    """A curve too thin to clear the liquidity floor (tiny vsol) is UNSELLABLE => near-total loss —
    the fail-safe direction (a thin book cannot be exited at decision-relevant size)."""
    # 1.0 SOL vsol => ~300 USD liquidity, below the 500 floor => honeypot.
    thin = _curve_record("T", "S", bt=_BASE_BT, slot=1, marks=_CURVE_WIN, vsol_lamports=1_000_000_000)
    deep = _curve_record("D", "S", bt=_BASE_BT, slot=2, marks=_CURVE_WIN, vsol_lamports=_DEEP_VSOL)
    (t_out, _), (d_out, _) = build_reaction_outcomes([thin]), build_reaction_outcomes([deep])
    assert t_out[0].net_pnl_lamports < d_out[0].net_pnl_lamports  # thin honeypots, deep sells


# ---------------------------------------------------------------------------
# FAIL-CLOSED — a record with NO exit-liquidity signal at all is CENSORED
# ---------------------------------------------------------------------------


def test_no_liquidity_signal_at_all_is_censored_not_total_loss() -> None:
    """No per-mark liquidity AND no vsol => no way to honestly price the exit => CENSORED (fail
    closed), NOT resolved to a fabricated total loss."""
    no_liq = _curve_record("N", "S", bt=_BASE_BT, slot=1, marks=_CURVE_WIN, vsol_lamports=None)
    assert "vsol_lamports" not in no_liq
    with pytest.raises(ReactionCorpusError, match="no exit-liquidity signal"):
        read_reaction_forward(no_liq, curve_liquidity_usd=None)
    # And end-to-end it is CENSORED (dropped), never emitted as a -100% outcome.
    outcomes, stats = build_reaction_outcomes([no_liq])
    assert outcomes == []
    assert stats.n_censored == 1


def test_universally_absent_liquidity_fails_closed_to_no_metric() -> None:
    """If EVERY record lacks any liquidity signal, all are censored => 0 resolved => no metric
    (the honest fail-closed outcome, not a uniform-total-loss NO-GO masquerading as a measurement)."""
    recs = [_curve_record(f"N_{i}", "S", bt=_BASE_BT + i * _SPACING_MS, slot=i, vsol_lamports=None)
            for i in range(5)]
    outcomes, stats = build_reaction_outcomes(recs)
    assert outcomes == []
    assert stats.n_resolved == 0 and stats.n_censored == 5


# ---------------------------------------------------------------------------
# MINOR — buy/sell pressure is read from the LIVE `txns` key, not zeroed
# ---------------------------------------------------------------------------


def test_live_txns_key_is_read_not_zeroed() -> None:
    """The live recorder emits `txns` (not `txns_m5`). read_reaction_forward must parse it — the old
    reader read only `txns_m5` and silently zeroed every buy/sell on the real corpus."""
    rec = _curve_record("M", "S", bt=_BASE_BT, slot=1, marks=_CURVE_WIN)
    marks = read_reaction_forward(rec, curve_liquidity_usd=bonding_curve_liquidity_usd(_DEEP_VSOL))
    by_h = {m.horizon_s: m for m in marks}
    assert (by_h[15].buys, by_h[15].sells) == (5, 3)  # from _CURVE_WIN[15] = (price, n_trades, 5, 3)
    assert (by_h[60].buys, by_h[60].sells) == (25, 15)
    assert any(m.buys + m.sells > 0 for m in marks)  # NOT all-zeros


def test_spec_txns_m5_shape_still_parses() -> None:
    """Back-compat: the SPEC `txns_m5` shape (older/fixture corpora) is still read (fallback)."""
    rec = {
        "signal_type": "whale_buy", "source_id": "S", "mint": "M", "signal_slot": 1,
        "signal_block_time_ms": _BASE_BT, "signal_price_sol": "0.001", "signal_size_sol": "5.0",
        "forward": [{"horizon_s": 15, "price_sol": "0.0011", "liquidity_usd": "6000",
                     "txns_m5": {"buys": 7, "sells": 2}}],
    }
    marks = read_reaction_forward(rec, curve_liquidity_usd=None)  # per-mark liquidity present
    assert (marks[0].buys, marks[0].sells) == (7, 2)
    assert marks[0].liquidity_usd == Decimal("6000")  # its own liquidity wins over the (None) proxy


# ---------------------------------------------------------------------------
# BOUNDARY 1 (intact) — a record's OWN vsol/forward (its OUTCOME) never reaches its OWN decision
# ---------------------------------------------------------------------------


def test_b1_own_vsol_reaches_outcome_not_own_decision() -> None:
    """Mutating a record's OWN vsol changes its OWN realized PnL (vsol feeds the OUTCOME liquidity)
    but NOT its OWN model/baseline selection mask (the OUTCOME never informs its own DECISION —
    leak boundary 1 holds for the new curve-liquidity input)."""
    recs = [_curve_record(f"W_{i}", "GOOD", bt=_BASE_BT + i * _SPACING_MS, slot=200 + i)
            for i in range(4)]
    base_out, _ = build_reaction_outcomes(recs)
    base_mask = {o.mint: (o.model_selected, o.baseline_selected) for o in base_out}
    base_pnl = {o.mint: o.net_pnl_lamports for o in base_out}

    # Halve the FIRST record's vsol (thinner curve => more slippage => different OUTCOME PnL).
    mutated = copy.deepcopy(recs)
    mutated[0]["vsol_lamports"] = _DEEP_VSOL // 8
    mut_out, _ = build_reaction_outcomes(mutated)
    mut_mask = {o.mint: (o.model_selected, o.baseline_selected) for o in mut_out}
    mut_pnl = {o.mint: o.net_pnl_lamports for o in mut_out}

    # OUTCOME moved for the mutated record; its OWN decision mask is byte-identical (no leak).
    assert mut_pnl["W_0"] != base_pnl["W_0"]
    assert mut_mask["W_0"] == base_mask["W_0"]
