"""T-323 — point-in-time correctness + asymmetric-trust + sell-sim-seam proofs.

POINT-IN-TIME (C-5):
  * The gate reads ONLY as-of event_time facts; there is no field on SafetyInputs
    that admits event_time + H data, so a lookahead leak is not expressible.
  * The decision is invariant to wall-clock: the same inputs decided at two
    different compute times give the IDENTICAL verdict (no compute-time leak).
  * event_block_time_ms (the authoritative clock) is carried on the decision, and
    the verdict never depends on the harness's wall-clock.

ASYMMETRIC TRUST:
  * The gate consumes NO LLM / model / social signal — it is a pure on-chain-fact
    scanner.  There is no parameter through which an LLM could size up, widen, or
    override.  The gate can ONLY reject.

SELL-SIM SEAM (off hot path, stubbed for T-329):
  * No probe => refuse-by-default (NOT_SELLABLE).
  * A probe that says not-sellable => NOT_SELLABLE.
  * A probe that says sellable => no flag.
  * The hot path never invokes the probe.
"""

from __future__ import annotations

from aats.risk.pretrade_gate import (
    GateVerdict,
    PreTradeSafetyGate,
    RedFlag,
    SafetyInputs,
)

MINT = "So1111111111111111111111111111111111111111"


def _inputs(slot: int, block_time_ms: int, **ov) -> SafetyInputs:
    base = {
        "mint": MINT,
        "event_slot": slot,
        "event_block_time_ms": block_time_ms,
        "data_staleness_ms": 50,
        "freeze_authority": None,
        "mint_authority": None,
        "lp_locked_or_burned_bps": 10_000,
        "dev_bundle_cluster_bps": 400,
        "holder_concentration_top10_bps": 1_800,
        "holder_count": 50,  # E18: above the distinct-holder floor
        "sell_tax_bps": 0,
        "buy_tax_bps": 0,
        "authorities_decoded": True,
    }
    base.update(ov)
    return SafetyInputs(**base)


# ---------------------------------------------------------------------------
# Point-in-time: verdict is a pure function of as-of inputs (no compute-time leak).
# ---------------------------------------------------------------------------


def test_verdict_is_pure_function_of_inputs_no_wall_clock():
    """Deciding the SAME inputs at two different compute times => identical verdict.
    The gate has no wall-clock dependency, so there is no compute-time leak."""
    import time

    gate = PreTradeSafetyGate()
    inp = _inputs(slot=5000, block_time_ms=1_700_000_005_000)

    d1 = gate.evaluate_fast(inp)
    time.sleep(0.01)  # advance wall-clock
    d2 = gate.evaluate_fast(inp)

    assert d1.verdict == d2.verdict
    assert d1.red_flag_codes == d2.red_flag_codes
    # The decision carries the EVENT-TIME clock, never wall-clock.
    assert d1.event_block_time_ms == inp.event_block_time_ms
    assert d2.event_block_time_ms == inp.event_block_time_ms


def test_no_lookahead_field_on_inputs():
    """Structural: SafetyInputs exposes no field that could carry event_time+H data
    (no future-slot, no realized-outcome, no label).  A lookahead leak is not
    expressible on the input contract."""
    fields = set(SafetyInputs.__dataclass_fields__.keys())
    forbidden = {
        "future_slot",
        "realized_mult",
        "realized_mult_decimal",
        "label",
        "survived",
        "fwd_return",
        "outcome",
        "resolution_slot",
        "max_drawdown_bps",
        "rug_detectable_at_decision",
        # sim truth_* fields must never appear
        "truth_is_rug",
        "truth_max_multiple",
        "truth_rug_detectable",
    }
    assert fields.isdisjoint(forbidden), f"SafetyInputs carries a lookahead field: {fields}"


def test_decision_clock_is_event_time_not_wall_clock():
    gate = PreTradeSafetyGate()
    inp = _inputs(slot=42, block_time_ms=1_609_459_200_001)
    dec = gate.evaluate_fast(inp)
    assert dec.event_slot == 42
    assert dec.event_block_time_ms == 1_609_459_200_001


# ---------------------------------------------------------------------------
# Asymmetric trust: no LLM/model/social parameter; gate can ONLY reject.
# ---------------------------------------------------------------------------


def test_gate_has_no_llm_or_model_parameter():
    """The gate's inputs/constructor expose NO LLM/model/social signal — there is
    no parameter through which an LLM could size up, widen, or override.  The gate
    is a pure on-chain-fact scanner that can only de-risk."""
    input_fields = set(SafetyInputs.__dataclass_fields__.keys())
    for tainted in (
        "llm",
        "model",
        "probability",
        "p_calibrated",
        "conviction",
        "sentiment",
        "social",
        "mcs",
        "score",
    ):
        assert not any(
            tainted in f.lower() for f in input_fields
        ), f"SafetyInputs carries an LLM/model/social field matching {tainted!r}"


def test_gate_only_de_risks_never_passes_a_bad_token_on_any_signal():
    """No combination of inputs makes a freeze-authority honeypot PASS.  There is
    no 'override' that a high score / LLM could supply — the input has no such field."""
    gate = PreTradeSafetyGate()
    dec = gate.evaluate_fast(
        _inputs(slot=1, block_time_ms=1_700_000_000_001, freeze_authority="Fr33ze")
    )
    assert dec.verdict is GateVerdict.REJECT


# ---------------------------------------------------------------------------
# Sell-sim seam (off hot path, stubbed for T-329).
# ---------------------------------------------------------------------------


class _SellableProbe:
    def is_sellable(self, mint, event_slot):  # noqa: ANN001
        return True


class _HoneypotProbe:
    def is_sellable(self, mint, event_slot):  # noqa: ANN001
        return False


def test_sellsim_refuses_by_default_without_probe():
    """Pre-T-329: no probe wired => NOT_SELLABLE (refuse-by-default), never 'unknown==ok'."""
    gate = PreTradeSafetyGate()
    hit = gate.check_sellability(_inputs(1, 1_700_000_000_001), probe=None)
    assert hit is not None
    assert hit.flag is RedFlag.NOT_SELLABLE


def test_sellsim_honeypot_probe_flags_not_sellable():
    gate = PreTradeSafetyGate()
    hit = gate.check_sellability(_inputs(1, 1_700_000_000_001), probe=_HoneypotProbe())
    assert hit is not None
    assert hit.flag is RedFlag.NOT_SELLABLE


def test_sellsim_sellable_probe_returns_no_flag():
    gate = PreTradeSafetyGate()
    hit = gate.check_sellability(_inputs(1, 1_700_000_000_001), probe=_SellableProbe())
    assert hit is None


# ---------------------------------------------------------------------------
# Fail-closed: a disabled gate can never green-light a real entry.
# ---------------------------------------------------------------------------


def test_disabled_gate_cannot_greenlight_entry():
    """SHADOW/RECORD may run the gate disabled to observe red flags; but a disabled
    gate's PASS must NEVER green-light a live entry (fail-closed)."""
    enabled = PreTradeSafetyGate(enabled=True)
    disabled = PreTradeSafetyGate(enabled=False)
    clean = _inputs(1, 1_700_000_000_001)
    # Both can PRODUCE a pass decision (observe), but only the enabled gate may
    # green-light an entry.
    assert enabled.require_enabled_for_entry(enabled.evaluate_fast(clean)) is True
    assert disabled.require_enabled_for_entry(disabled.evaluate_fast(clean)) is False
