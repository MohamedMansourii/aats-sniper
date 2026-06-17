"""AUDIT-liquidity — pre-trade LIQUIDITY-SANITY gate tests.

Proves the audit requirement directly:
  * An entry whose 24h volume is < ~10x the intended position is REJECTED.
  * An entry whose SIMULATED entry slippage exceeds the threshold is REJECTED.
  * A deep, liquid candidate PASSes (and the pass is SILENT — sizes nothing).
  * Refuse-by-default: unknown volume / missing reserves / stale data => REJECT.
  * Point-in-time: the gate reads only event-time fields; no wall-clock.
  * Money is int/Decimal — float inputs/thresholds are refused at construction.
  * De-risk-only: tightening a threshold can only REJECT MORE.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from aats.risk.liquidity_sanity import (
    LiquidityInputs,
    LiquidityReject,
    LiquiditySanityGate,
    LiquidityThresholds,
    LiquidityVerdict,
    simulate_entry_slippage_bps,
)

MINT = "So1111111111111111111111111111111111111111"
SLOT = 1_000
BTIME = 1_700_000_000_000

# A position of 0.1 SOL (the per-trade cap floor).
POSITION = 100_000_000  # lamports

# A deep pool: 5_000 SOL reserve vs a token reserve.  A 0.1 SOL buy against this
# is ~0.004% of the pool => negligible slippage.
DEEP_SOL_RESERVE = 5_000 * 1_000_000_000  # 5_000 SOL
DEEP_TOK_RESERVE = 1_000_000_000_000_000  # arbitrary deep token reserve


def _inputs(**overrides) -> LiquidityInputs:
    """A deep, liquid, fresh candidate that should PASS by default."""
    base = {
        "mint": MINT,
        "event_slot": SLOT,
        "event_block_time_ms": BTIME,
        "data_staleness_ms": 50,
        "intended_position_lamports": POSITION,
        # 24h volume = 100x the position => comfortably above the 10x floor.
        "volume_24h_lamports": POSITION * 100,
        "sol_reserve_lamports": DEEP_SOL_RESERVE,
        "token_reserve_base": DEEP_TOK_RESERVE,
    }
    base.update(overrides)
    return LiquidityInputs(**base)


# ---------------------------------------------------------------------------
# Happy path — a deep, liquid candidate PASSes silently.
# ---------------------------------------------------------------------------


def test_deep_liquid_candidate_passes():
    gate = LiquiditySanityGate()
    dec = gate.evaluate(_inputs())
    assert dec.passed
    assert dec.verdict is LiquidityVerdict.PASS
    assert dec.rejects == ()


def test_pass_is_silent_no_sizing_fields():
    """The decision is a veto report — it exposes no size/stop/trigger field."""
    gate = LiquiditySanityGate()
    dec = gate.evaluate(_inputs())
    # Only verdict/reject reporting fields exist — nothing that could add risk.
    fields = set(vars(dec).keys())
    forbidden = {"size", "size_lamports", "stop", "trigger", "intent", "leverage"}
    assert fields.isdisjoint(forbidden)


# ---------------------------------------------------------------------------
# THE AUDIT REQUIREMENT (1): 24h volume < ~10x position => REJECT.
# ---------------------------------------------------------------------------


def test_volume_below_10x_position_is_rejected():
    gate = LiquiditySanityGate()  # default min_volume_multiple = 10x
    # Volume only 9x the position — below the 10x floor.
    dec = gate.evaluate(_inputs(volume_24h_lamports=POSITION * 9))
    assert not dec.passed
    assert LiquidityReject.VOLUME_BELOW_MULTIPLE.value in dec.reject_codes


def test_volume_exactly_10x_passes():
    """10x exactly meets the floor (>=, not >) — passes the volume check."""
    gate = LiquiditySanityGate()
    dec = gate.evaluate(_inputs(volume_24h_lamports=POSITION * 10))
    assert dec.passed


def test_volume_just_under_10x_is_rejected():
    gate = LiquiditySanityGate()
    dec = gate.evaluate(_inputs(volume_24h_lamports=POSITION * 10 - 1))
    assert not dec.passed
    assert LiquidityReject.VOLUME_BELOW_MULTIPLE.value in dec.reject_codes


def test_volume_reject_reports_measured_ratio():
    gate = LiquiditySanityGate()
    dec = gate.evaluate(_inputs(volume_24h_lamports=POSITION * 5))
    hit = next(h for h in dec.rejects if h.reason is LiquidityReject.VOLUME_BELOW_MULTIPLE)
    assert hit.measured_ratio == Decimal(5)


def test_volume_multiple_is_tighten_only_rejects_more():
    """A stricter multiple (20x) rejects a candidate the default (10x) passed."""
    deep = _inputs(volume_24h_lamports=POSITION * 15)
    assert LiquiditySanityGate().evaluate(deep).passed  # 15x passes default 10x
    strict = LiquiditySanityGate(thresholds=LiquidityThresholds(min_volume_multiple=Decimal(20)))
    assert not strict.evaluate(deep).passed  # 15x fails the tighter 20x floor


# ---------------------------------------------------------------------------
# THE AUDIT REQUIREMENT (2): simulated slippage over threshold => REJECT.
# ---------------------------------------------------------------------------


def test_thin_pool_excessive_sim_slippage_is_rejected():
    gate = LiquiditySanityGate()  # default max_sim_slippage_bps = 300 (3%)
    # A thin pool: 0.1 SOL reserve vs the SAME 0.1 SOL buy => ~50% price impact.
    thin = _inputs(
        sol_reserve_lamports=POSITION,  # pool == ticket size
        token_reserve_base=DEEP_TOK_RESERVE,
        volume_24h_lamports=POSITION * 100,  # volume check passes; isolate slippage
    )
    dec = gate.evaluate(thin)
    assert not dec.passed
    assert LiquidityReject.SIM_SLIPPAGE_TOO_HIGH.value in dec.reject_codes


def test_sim_slippage_within_threshold_passes():
    gate = LiquiditySanityGate()
    dec = gate.evaluate(_inputs())  # deep pool => negligible slippage
    assert dec.passed


def test_sim_slippage_threshold_is_tighten_only_rejects_more():
    """A moderately-deep pool passes a 300bps tolerance but fails a 1bps one."""
    # Pool ~= 50x the ticket => ~2% impact, under 300bps, over 1bps.
    moderate = _inputs(
        sol_reserve_lamports=POSITION * 50,
        token_reserve_base=DEEP_TOK_RESERVE,
        volume_24h_lamports=POSITION * 100,
    )
    assert LiquiditySanityGate().evaluate(moderate).passed
    strict = LiquiditySanityGate(thresholds=LiquidityThresholds(max_sim_slippage_bps=1))
    assert not strict.evaluate(moderate).passed


def test_sim_slippage_matches_helper():
    """The gate's slippage check uses the same pure helper a backtest would."""
    slip = simulate_entry_slippage_bps(POSITION, DEEP_TOK_RESERVE, POSITION)
    # 0.1 SOL into a 0.1 SOL pool => ~50% slippage => well over 300 bps.
    assert slip is not None and slip > 300


# ---------------------------------------------------------------------------
# Refuse-by-default — unknown/missing/stale inputs REJECT, never pass.
# ---------------------------------------------------------------------------


def test_unknown_volume_refuses():
    gate = LiquiditySanityGate()
    dec = gate.evaluate(_inputs(volume_24h_lamports=None))
    assert not dec.passed
    assert LiquidityReject.INPUT_INCOMPLETE.value in dec.reject_codes


def test_missing_sol_reserve_refuses():
    gate = LiquiditySanityGate()
    dec = gate.evaluate(_inputs(sol_reserve_lamports=None))
    assert not dec.passed
    assert LiquidityReject.INPUT_INCOMPLETE.value in dec.reject_codes


def test_missing_token_reserve_refuses():
    gate = LiquiditySanityGate()
    dec = gate.evaluate(_inputs(token_reserve_base=None))
    assert not dec.passed
    assert LiquidityReject.INPUT_INCOMPLETE.value in dec.reject_codes


def test_degenerate_pool_refuses():
    """A zero-reserve pool cannot form a sim => refuse-by-default."""
    gate = LiquiditySanityGate()
    dec = gate.evaluate(_inputs(sol_reserve_lamports=0, token_reserve_base=DEEP_TOK_RESERVE))
    assert not dec.passed
    assert LiquidityReject.INPUT_INCOMPLETE.value in dec.reject_codes


def test_stale_data_refuses_before_any_check():
    gate = LiquiditySanityGate()
    dec = gate.evaluate(_inputs(data_staleness_ms=10_000))  # over the 2_000ms budget
    assert not dec.passed
    assert dec.reject_codes == (LiquidityReject.INPUT_STALE.value,)


# ---------------------------------------------------------------------------
# scan_all reports every failure (dashboard) — both checks can fail at once.
# ---------------------------------------------------------------------------


def test_scan_all_reports_both_failures():
    gate = LiquiditySanityGate()
    bad = _inputs(
        volume_24h_lamports=POSITION,  # 1x — below 10x
        sol_reserve_lamports=POSITION,  # thin pool — high slippage
    )
    dec = gate.scan_all(bad)
    assert not dec.passed
    codes = set(dec.reject_codes)
    assert LiquidityReject.VOLUME_BELOW_MULTIPLE.value in codes
    assert LiquidityReject.SIM_SLIPPAGE_TOO_HIGH.value in codes


def test_evaluate_short_circuits_volume_before_slippage():
    """The hot path stops at the first failure (volume) for latency."""
    gate = LiquiditySanityGate()
    bad = _inputs(volume_24h_lamports=POSITION, sol_reserve_lamports=POSITION)
    dec = gate.evaluate(bad)
    assert dec.reject_codes == (LiquidityReject.VOLUME_BELOW_MULTIPLE.value,)


# ---------------------------------------------------------------------------
# Fail-closed entry guard — a disabled gate can never green-light an entry.
# ---------------------------------------------------------------------------


def test_disabled_gate_cannot_green_light_entry():
    gate = LiquiditySanityGate(enabled=False)
    dec = gate.evaluate(_inputs())  # would PASS on the merits
    assert dec.passed
    assert not gate.require_enabled_for_entry(dec)  # but disabled => not eligible


def test_enabled_pass_is_entry_eligible():
    gate = LiquiditySanityGate(enabled=True)
    dec = gate.evaluate(_inputs())
    assert gate.require_enabled_for_entry(dec)


def test_enabled_reject_is_not_entry_eligible():
    gate = LiquiditySanityGate(enabled=True)
    dec = gate.evaluate(_inputs(volume_24h_lamports=POSITION))
    assert not gate.require_enabled_for_entry(dec)


# ---------------------------------------------------------------------------
# Point-in-time — the gate reads only event-time fields (no wall-clock leak).
# Same inputs => same verdict regardless of when called.
# ---------------------------------------------------------------------------


def test_deterministic_no_wall_clock():
    gate = LiquiditySanityGate()
    inp = _inputs(volume_24h_lamports=POSITION * 7)
    first = gate.evaluate(inp)
    second = gate.evaluate(inp)
    assert first.verdict is second.verdict
    assert first.reject_codes == second.reject_codes
    # The decision echoes only the event-time anchors it was given.
    assert first.event_slot == SLOT
    assert first.event_block_time_ms == BTIME


# ---------------------------------------------------------------------------
# Money is int/Decimal — float money is refused at construction.
# ---------------------------------------------------------------------------


def test_float_position_refused():
    with pytest.raises(TypeError):
        _inputs(intended_position_lamports=1.0e8)


def test_float_volume_refused():
    with pytest.raises(TypeError):
        _inputs(volume_24h_lamports=1.0e9)


def test_float_reserve_refused():
    with pytest.raises(TypeError):
        _inputs(sol_reserve_lamports=5.0e12)


def test_float_threshold_multiple_refused():
    with pytest.raises(TypeError):
        LiquidityThresholds(min_volume_multiple=10.0)  # must be Decimal, not float


def test_float_slippage_threshold_refused():
    with pytest.raises(TypeError):
        LiquidityThresholds(max_sim_slippage_bps=300.0)


def test_multiple_below_one_refused():
    with pytest.raises(ValueError):
        LiquidityThresholds(min_volume_multiple=Decimal("0.5"))


def test_non_positive_position_refused():
    with pytest.raises(ValueError):
        _inputs(intended_position_lamports=0)


# ---------------------------------------------------------------------------
# The pure slippage helper — monotonic in size (bigger ticket => more impact).
# ---------------------------------------------------------------------------


def test_slippage_monotonic_in_size():
    small = simulate_entry_slippage_bps(DEEP_SOL_RESERVE, DEEP_TOK_RESERVE, POSITION)
    big = simulate_entry_slippage_bps(DEEP_SOL_RESERVE, DEEP_TOK_RESERVE, POSITION * 100)
    assert small is not None and big is not None
    assert big >= small


def test_slippage_helper_refuses_degenerate():
    assert simulate_entry_slippage_bps(0, DEEP_TOK_RESERVE, POSITION) is None
    assert simulate_entry_slippage_bps(DEEP_SOL_RESERVE, 0, POSITION) is None
    assert simulate_entry_slippage_bps(DEEP_SOL_RESERVE, DEEP_TOK_RESERVE, 0) is None
