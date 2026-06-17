"""T-321 — latency proof: the in-process breach DECISION is fast and IO-free.

ADR-0008 Layer 2 budget: the steady-state in-process enforcer must decide within
<=50ms p99 and NEVER await an LLM or RPC on the decision path.  Here we:
  * benchmark SecondaryStopEnforcer.decide() over many ticks and assert p50/p99
    are deep inside the 50ms budget (the decision is a pure Decimal compare);
  * prove the decision performs NO IO — a venue whose every method raises is
    handed in, and decide() still returns without touching it (only a real
    BREACH would call .exit(), which is the post-decision handoff, not the
    decision).
"""

from __future__ import annotations

import time
from decimal import Decimal

from aats.risk.secondary_enforcer import MarkTick, SecondaryStopEnforcer
from aats.risk.survivable_stop import StopState

MINT = "So1111111111111111111111111111111111111111"

# Layer-2 steady-state budget (ADR-0008): <= 50ms p99 for the decision.
DECISION_BUDGET_MS = 50.0


class _ExplodingVenue:
    """Any IO from the DECISION path detonates this — proving decide() is IO-free."""

    name = "exploding"

    def exit(self, intent, position):  # noqa: ANN001 - test stub
        raise AssertionError("decide() must NOT touch the venue — no IO on the decision path")

    def __getattr__(self, name):
        raise AssertionError(f"decide() touched venue.{name} — the decision path must be IO-free")


def _percentile(samples: list[float], pct: float) -> float:
    s = sorted(samples)
    if not s:
        return 0.0
    k = (len(s) - 1) * (pct / 100.0)
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def test_decision_is_io_free():
    enf = SecondaryStopEnforcer(_ExplodingVenue())  # type: ignore[arg-type]
    enf.register(StopState.at_entry(mint=MINT, entry_fill_price=100), position=object())
    # A non-breach tick: decide() must return False WITHOUT any venue access.
    tick = MarkTick(mint=MINT, mark_price=Decimal("90"), block_time_ms=1)
    assert enf.decide(tick) is False  # would AssertionError if it touched the venue


def test_decision_latency_within_budget():
    enf = SecondaryStopEnforcer(_ExplodingVenue())  # type: ignore[arg-type]
    enf.register(StopState.at_entry(mint=MINT, entry_fill_price=100), position=object())
    tick = MarkTick(mint=MINT, mark_price=Decimal("90"), block_time_ms=1)

    # warmup
    for _ in range(1000):
        enf.decide(tick)

    samples_ms: list[float] = []
    for _ in range(20_000):
        t0 = time.perf_counter()
        enf.decide(tick)
        samples_ms.append((time.perf_counter() - t0) * 1000.0)

    p50 = _percentile(samples_ms, 50)
    p99 = _percentile(samples_ms, 99)
    # Print for the SELF-CHECK paste.
    print(
        f"\n[T-321 latency] decide() p50={p50 * 1000:.2f}us  p99={p99 * 1000:.2f}us "
        f"(budget {DECISION_BUDGET_MS}ms p99)"
    )
    assert p99 < DECISION_BUDGET_MS, f"p99 {p99:.3f}ms exceeds {DECISION_BUDGET_MS}ms budget"
    # And it is in fact ORDERS of magnitude under budget (pure compare).
    assert p50 < 1.0


def test_breach_decision_also_fast():
    """A breaching tick's DECISION is just as fast (the .exit() is separate)."""

    # Use a venue that records (does not explode) since a breach DOES hand off.
    class _Rec:
        name = "rec"

        def exit(self, intent, position):
            from aats.contracts.venue import FillResult

            return FillResult(landed=True, reason="sim_exit")

    enf = SecondaryStopEnforcer(_Rec())  # type: ignore[arg-type]
    enf.register(StopState.at_entry(mint=MINT, entry_fill_price=100), position=object())
    breach = MarkTick(mint=MINT, mark_price=Decimal("10"), block_time_ms=1)
    t0 = time.perf_counter()
    decided = enf.decide(breach)
    dt_ms = (time.perf_counter() - t0) * 1000.0
    assert decided is True
    assert dt_ms < DECISION_BUDGET_MS
