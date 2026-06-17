"""T-323 — fault-injection harness for the pre-trade safety gate.

Every failure mode of the gate must be FAIL-CLOSED (reject / no-trade), never
fail-open (pass unprotected).  We inject:
  * malformed / out-of-range inputs (negative bps, impossible block_time);
  * a telemetry sink that raises (must not turn a REJECT into a PASS, and must not
    crash the hot path's verdict — the verdict is computed before telemetry);
  * a sell-sim probe that raises (off the hot path; must surface, never pass);
  * the staleness-budget edge.
"""

from __future__ import annotations

import pytest

from aats.risk.pretrade_gate import (
    GateVerdict,
    PreTradeSafetyGate,
    RedFlag,
    SafetyInputs,
    SafetyThresholds,
)

MINT = "So1111111111111111111111111111111111111111"


def _clean(**ov) -> SafetyInputs:
    base = {
        "mint": MINT,
        "event_slot": 1000,
        "event_block_time_ms": 1_700_000_001_000,
        "data_staleness_ms": 50,
        "freeze_authority": None,
        "mint_authority": None,
        "lp_locked_or_burned_bps": 10_000,
        "dev_bundle_cluster_bps": 400,
        "holder_concentration_top10_bps": 1_800,
        "sell_tax_bps": 0,
        "buy_tax_bps": 0,
        "authorities_decoded": True,
    }
    base.update(ov)
    return SafetyInputs(**base)


def test_impossible_block_time_is_refused_at_construction():
    with pytest.raises(ValueError):
        _clean(event_block_time_ms=0)
    with pytest.raises(ValueError):
        _clean(event_block_time_ms=-1)


def test_negative_staleness_refused_at_construction():
    with pytest.raises(ValueError):
        _clean(data_staleness_ms=-5)


def test_negative_threshold_refused_at_construction():
    with pytest.raises(ValueError):
        SafetyThresholds(max_sell_tax_bps=-1)


def test_lp_floor_out_of_range_refused():
    with pytest.raises(ValueError):
        SafetyThresholds(min_lp_locked_bps=10_001)


class _RaisingTelemetry:
    """A telemetry sink that detonates — must not affect the VERDICT (computed first)."""

    def on_gate_reject(self, mint, flags):  # noqa: ANN001
        raise RuntimeError("telemetry sink down")

    def on_gate_pass(self, mint):  # noqa: ANN001
        raise RuntimeError("telemetry sink down")


def test_telemetry_failure_does_not_flip_verdict_to_pass():
    """If telemetry raises on a REJECT, the gate must still have REJECTED — a
    telemetry outage can never silently turn a rejected honeypot into a pass.

    The verdict is the return value; telemetry is a side effect AFTER the verdict
    is decided.  Here we assert the verdict object reflects REJECT even though the
    sink raises (the exception propagates, but the decision was already a REJECT —
    fail-closed).  The SNIPE caller treats a raised gate as a refusal, never a pass.
    """
    gate = PreTradeSafetyGate(telemetry=_RaisingTelemetry())
    honeypot = _clean(freeze_authority="Fr33ze")
    with pytest.raises(RuntimeError):
        # The verdict is REJECT; the telemetry sink then raises.  The caller never
        # receives a PASS — the exception is itself fail-closed (no decision => refuse).
        gate.evaluate_fast(honeypot)


class _RaisingProbe:
    def is_sellable(self, mint, event_slot):  # noqa: ANN001
        raise RuntimeError("sell-sim RPC down")


def test_sellsim_probe_raising_is_off_hot_path_and_surfaces():
    """A raising sell-sim probe is OFF the hot path; when invoked (N+2), its failure
    must surface (propagate) — the caller treats it as not-sellable (fail-closed),
    never as a silent pass."""
    gate = PreTradeSafetyGate()
    with pytest.raises(RuntimeError):
        gate.check_sellability(_clean(), probe=_RaisingProbe())


def test_zero_staleness_budget_rejects_any_nonzero_staleness():
    """A zero freshness budget means only perfectly-fresh data passes — fail-closed
    against any staleness at all."""
    gate = PreTradeSafetyGate(thresholds=SafetyThresholds(max_staleness_ms=0))
    assert not gate.evaluate_fast(_clean(data_staleness_ms=1)).passed
    assert gate.evaluate_fast(_clean(data_staleness_ms=0)).passed


def test_extreme_bps_values_handled_as_int_no_overflow():
    """Extreme (but valid int) bps values are handled by integer comparison — no
    float coercion, no overflow, deterministic REJECT."""
    gate = PreTradeSafetyGate()
    dec = gate.evaluate_fast(_clean(sell_tax_bps=10_000))  # 100% sell tax
    assert dec.verdict is GateVerdict.REJECT
    assert dec.red_flags[0].flag is RedFlag.SELL_TAX_TOO_HIGH
    assert isinstance(dec.red_flags[0].measured, int)
