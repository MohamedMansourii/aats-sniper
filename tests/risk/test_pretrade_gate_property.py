"""T-323 PROPERTY / FUZZ proof — invariants over randomized candidate tokens.

No external fuzzing dependency (hypothesis is not vendored; matches the breaker /
survivable-stop property tests).  Seeded `random` makes the run deterministic.

Invariants (must hold for ALL inputs):
  P1  REJECT-ONLY: the verdict is always PASS or REJECT; the gate never produces a
      size, a stop, or an instruction.  A PASS is silent.
  P2  A tighter threshold rejects a SUPERSET (de-risk monotonicity): making any
      threshold stricter can only turn PASSes into REJECTs, never the reverse.
  P3  REFUSE-BY-DEFAULT: any None/undecoded required input always REJECTS.
  P4  DETERMINISM: the same inputs always yield the same verdict (no wall-clock,
      no RNG, no hidden state) — point-in-time purity.
  P5  scan_all is a superset of evaluate_fast: if evaluate_fast rejects, scan_all
      rejects (and reports >= as many flags).
  P6  Money discipline: the decision carries only int bps / int slots — never float.
"""

from __future__ import annotations

import random

from aats.risk.pretrade_gate import (
    GateVerdict,
    PreTradeSafetyGate,
    RedFlag,
    SafetyInputs,
    SafetyThresholds,
)

MINT = "So1111111111111111111111111111111111111111"


def _rand_inputs(rng: random.Random, *, force_complete: bool = True) -> SafetyInputs:
    slot = rng.randint(1, 5_000_000)
    return SafetyInputs(
        mint=MINT,
        event_slot=slot,
        event_block_time_ms=1_600_000_000_000 + slot,
        data_staleness_ms=rng.randint(0, 5_000),
        freeze_authority=None if rng.random() < 0.6 else "Fr33ze",
        mint_authority=None if rng.random() < 0.6 else "M1nt",
        lp_locked_or_burned_bps=rng.randint(0, 10_000),
        dev_bundle_cluster_bps=rng.randint(0, 10_000),
        holder_concentration_top10_bps=rng.randint(0, 10_000),
        sell_tax_bps=rng.randint(0, 10_000),
        buy_tax_bps=rng.randint(0, 10_000),
        authorities_decoded=True if force_complete else rng.random() < 0.8,
    )


def test_P1_verdict_is_always_pass_or_reject():
    rng = random.Random(20260616)
    gate = PreTradeSafetyGate()
    for _ in range(20_000):
        dec = gate.evaluate_fast(_rand_inputs(rng))
        assert dec.verdict in (GateVerdict.PASS, GateVerdict.REJECT)
        # A PASS is silent (no flags); a REJECT names >= 1 flag.
        if dec.passed:
            assert dec.red_flags == ()
        else:
            assert len(dec.red_flags) >= 1


def test_P2_tighter_threshold_rejects_a_superset():
    """De-risk monotonicity: tightening thresholds can only ADD rejects."""
    rng = random.Random(7)
    loose = SafetyThresholds(
        min_lp_locked_bps=5_000,
        max_holder_concentration_top10_bps=8_000,
        max_dev_bundle_cluster_bps=8_000,
        max_sell_tax_bps=5_000,
        max_buy_tax_bps=5_000,
        max_staleness_ms=10_000,
    )
    tight = SafetyThresholds(
        min_lp_locked_bps=9_500,
        max_holder_concentration_top10_bps=3_500,
        max_dev_bundle_cluster_bps=2_000,
        max_sell_tax_bps=1_000,
        max_buy_tax_bps=1_000,
        max_staleness_ms=2_000,
    )
    g_loose = PreTradeSafetyGate(thresholds=loose)
    g_tight = PreTradeSafetyGate(thresholds=tight)
    for _ in range(20_000):
        inp = _rand_inputs(rng)
        if g_loose.evaluate_fast(inp).passed:
            # Anything the LOOSE gate passes is not guaranteed to pass the tight gate,
            # but anything the loose gate REJECTS the tight gate must also reject.
            pass
        if not g_loose.evaluate_fast(inp).passed:
            assert not g_tight.evaluate_fast(
                inp
            ).passed, "tightening a threshold turned a REJECT into a PASS — that increases risk"


def test_P3_incomplete_input_always_rejects():
    rng = random.Random(99)
    gate = PreTradeSafetyGate()
    fields = [
        "lp_locked_or_burned_bps",
        "dev_bundle_cluster_bps",
        "holder_concentration_top10_bps",
        "sell_tax_bps",
        "buy_tax_bps",
    ]
    for _ in range(5_000):
        inp = _rand_inputs(rng)
        # Blank out one required numeric field => must reject.
        f = rng.choice(fields)
        kw = {k: getattr(inp, k) for k in inp.__dataclass_fields__}
        kw[f] = None
        holed = SafetyInputs(**kw)
        assert not gate.evaluate_fast(holed).passed
        # Undecoded authorities also always reject.
        kw2 = {k: getattr(inp, k) for k in inp.__dataclass_fields__}
        kw2["authorities_decoded"] = False
        assert not gate.evaluate_fast(SafetyInputs(**kw2)).passed


def test_P4_determinism():
    rng = random.Random(123456)
    gate = PreTradeSafetyGate()
    for _ in range(10_000):
        inp = _rand_inputs(rng)
        d1 = gate.evaluate_fast(inp)
        d2 = gate.evaluate_fast(inp)
        assert d1.verdict == d2.verdict
        assert d1.red_flag_codes == d2.red_flag_codes


def test_P5_scan_all_is_superset_of_fast():
    rng = random.Random(2024)
    gate = PreTradeSafetyGate()
    for _ in range(20_000):
        inp = _rand_inputs(rng)
        fast = gate.evaluate_fast(inp)
        full = gate.scan_all(inp)
        # Same verdict (any flag => reject in both).
        assert fast.passed == full.passed
        if not fast.passed:
            # scan_all reports at least as many flags as the short-circuiting fast path.
            assert len(full.red_flags) >= len(fast.red_flags)
            # The first fast flag is contained in the full set.
            assert fast.red_flags[0].flag in {h.flag for h in full.red_flags}


def test_P6_decision_carries_only_int_money():
    rng = random.Random(555)
    gate = PreTradeSafetyGate()
    for _ in range(5_000):
        dec = gate.evaluate_fast(_rand_inputs(rng))
        assert isinstance(dec.event_slot, int) and not isinstance(dec.event_slot, bool)
        assert isinstance(dec.event_block_time_ms, int)
        for hit in dec.red_flags:
            assert hit.measured is None or isinstance(hit.measured, int)
            assert hit.threshold is None or isinstance(hit.threshold, int)
            # NEVER float on a money/bps field.
            assert not isinstance(hit.measured, float)
            assert not isinstance(hit.threshold, float)


def test_P7_freeze_or_mint_authority_always_rejects():
    """Hard invariant: a set freeze OR mint authority ALWAYS rejects, regardless of
    every other field.  These are non-negotiable honeypot/rug signatures."""
    rng = random.Random(31337)
    gate = PreTradeSafetyGate()
    for _ in range(10_000):
        inp = _rand_inputs(rng)
        kw = {k: getattr(inp, k) for k in inp.__dataclass_fields__}
        kw["data_staleness_ms"] = 0  # fresh, so the authority check is reached
        if rng.random() < 0.5:
            kw["freeze_authority"] = "Fr33ze"
            expected = RedFlag.FREEZE_AUTHORITY_SET
        else:
            kw["freeze_authority"] = None
            kw["mint_authority"] = "M1nt"
            expected = RedFlag.MINT_AUTHORITY_NOT_RENOUNCED
        dec = gate.evaluate_fast(SafetyInputs(**kw))
        assert not dec.passed
        assert expected in {h.flag for h in dec.red_flags}
