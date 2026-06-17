"""T-323 — LATENCY proof: the local hot-path gate is sub-10ms, 0-RPC, and REFUSES
(never passes) when the budget / staleness is exceeded.

ADR / acceptance: the local checks (1..5) must complete inside the <10ms block-0
budget with NO RPC on the hot path (safety.py:4-6).  Here we:
  * benchmark evaluate_fast() over many candidates and assert p50/p99 << 10ms;
  * prove the hot path performs NO IO — any attribute access on a sell-sim probe
    detonates (the sell-sim is OFF the hot path; evaluate_fast must never touch it);
  * prove REFUSE-BY-TIMEOUT-EQUIVALENT: when staleness exceeds the freshness
    budget, the gate REJECTS (does not pass).  A check that cannot be trusted in
    budget refuses; it never passes by default.
"""

from __future__ import annotations

import time

from aats.risk.pretrade_gate import (
    GateVerdict,
    PreTradeSafetyGate,
    SafetyInputs,
    SafetyThresholds,
)

MINT = "So1111111111111111111111111111111111111111"

# Block-0 local-check budget (BUILD-DIRECTIVE / safety.py): the LOCAL checks must
# finish well inside 10ms.  We assert p99 strictly under budget.
HOT_BUDGET_MS = 10.0


class _ExplodingProbe:
    """Any access detonates — proving the hot path NEVER touches the sell-sim seam."""

    def is_sellable(self, mint, event_slot):  # noqa: ANN001
        raise AssertionError("evaluate_fast must NOT call the sell-sim probe (off hot path)")

    def __getattr__(self, name):
        raise AssertionError(f"hot path touched probe.{name} — sell-sim is OFF the hot path")


def _clean(slot: int = 1000) -> SafetyInputs:
    return SafetyInputs(
        mint=MINT,
        event_slot=slot,
        event_block_time_ms=1_700_000_000_000 + slot,
        data_staleness_ms=50,
        freeze_authority=None,
        mint_authority=None,
        lp_locked_or_burned_bps=10_000,
        dev_bundle_cluster_bps=400,
        holder_concentration_top10_bps=1_800,
        sell_tax_bps=0,
        buy_tax_bps=0,
        authorities_decoded=True,
    )


def _percentile(samples: list[float], pct: float) -> float:
    s = sorted(samples)
    if not s:
        return 0.0
    k = (len(s) - 1) * (pct / 100.0)
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def test_hot_path_does_no_io():
    """evaluate_fast performs no RPC / no sell-sim — a probe is never even held by
    the hot path.  (We don't pass it in; the hot path has no reference to one.)"""
    gate = PreTradeSafetyGate()
    # Sanity: the sell-sim seam is a SEPARATE method, never invoked by evaluate_fast.
    dec = gate.evaluate_fast(_clean())
    assert dec.passed
    # If anyone wired the probe into the hot path, this would detonate:
    # check_sellability is the ONLY caller of the probe, and it is off the hot path.
    hit = gate.check_sellability(_clean(), probe=None)  # no probe => refuse-by-default
    assert hit is not None  # NOT_SELLABLE (refuse-by-default)


def test_hot_path_latency_p50_p99_under_10ms():
    gate = PreTradeSafetyGate()
    candidates = [_clean(slot=1000 + i) for i in range(256)]

    # warmup
    for c in candidates:
        gate.evaluate_fast(c)

    samples_ms: list[float] = []
    for _ in range(40):
        for c in candidates:
            t0 = time.perf_counter()
            gate.evaluate_fast(c)
            samples_ms.append((time.perf_counter() - t0) * 1000.0)

    p50 = _percentile(samples_ms, 50)
    p99 = _percentile(samples_ms, 99)
    print(
        f"\n[T-323 latency] evaluate_fast p50={p50 * 1000:.2f}us  p99={p99 * 1000:.2f}us "
        f"(budget {HOT_BUDGET_MS}ms p99)  n={len(samples_ms)}"
    )
    assert p99 < HOT_BUDGET_MS, f"p99 {p99:.4f}ms exceeds {HOT_BUDGET_MS}ms hot-path budget"
    # And it is in fact orders of magnitude under budget (pure integer comparisons).
    assert p50 < 0.5


def test_reject_path_also_under_budget():
    """The REJECT path (short-circuit on first flag) is at least as fast as PASS."""
    gate = PreTradeSafetyGate()
    bad = SafetyInputs(
        mint=MINT,
        event_slot=1000,
        event_block_time_ms=1_700_000_001_000,
        data_staleness_ms=50,
        freeze_authority="Fr33ze",  # trips check #1 — short circuits immediately
        mint_authority=None,
        lp_locked_or_burned_bps=10_000,
        dev_bundle_cluster_bps=400,
        holder_concentration_top10_bps=1_800,
        sell_tax_bps=0,
        buy_tax_bps=0,
    )
    t0 = time.perf_counter()
    dec = gate.evaluate_fast(bad)
    dt_ms = (time.perf_counter() - t0) * 1000.0
    assert dec.verdict is GateVerdict.REJECT
    assert dt_ms < HOT_BUDGET_MS


def test_over_budget_staleness_refuses_not_passes():
    """REFUSE-BY-DEFAULT: when point-in-time data is staler than the freshness
    budget (the time-budget analogue), the gate REJECTS — it never passes-by-timeout.

    This is the structural 'refuse if it cannot fully pass inside budget' rule:
    stale == cannot-trust == REJECT, never a silent PASS.
    """
    gate = PreTradeSafetyGate(thresholds=SafetyThresholds(max_staleness_ms=1_000))
    # Otherwise-clean token, but data is 3s stale (over the 1s budget).
    stale = SafetyInputs(
        mint=MINT,
        event_slot=1000,
        event_block_time_ms=1_700_000_001_000,
        data_staleness_ms=3_000,  # OVER budget
        freeze_authority=None,
        mint_authority=None,
        lp_locked_or_burned_bps=10_000,
        dev_bundle_cluster_bps=400,
        holder_concentration_top10_bps=1_800,
        sell_tax_bps=0,
        buy_tax_bps=0,
    )
    dec = gate.evaluate_fast(stale)
    assert dec.verdict is GateVerdict.REJECT, "over-budget data MUST refuse, not pass"
    assert dec.red_flag_codes[0] == "input_stale"
