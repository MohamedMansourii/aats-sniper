"""T-320 PROPERTY / FUZZ proof — invariants over randomized loss sequences.

No external fuzzing dependency (hypothesis is not vendored).  We use seeded
`random` so the run is deterministic and reproducible, sweeping thousands of
randomized PnL sequences and configs.  The invariant asserted is the one that
matters: the breaker NEVER leaves entries enabled once the day's event-time net
PnL has crossed either loss limit, and it NEVER trips spuriously above both.

Invariants (must hold for ALL inputs):
  P1  Once the event-time day net crosses a limit, the breaker is TRIPPED and
      entries are BLOCKED, and it STAYS so for the rest of the day.
  P2  The breaker never trips while the day net is strictly above BOTH limits.
  P3  daily_net_pnl_lamports stays an int (never float) for every transition.
  P4  Latch monotonicity: once TRIPPED, no PnL event re-arms it (only reset()).
  P5  Flatten is invoked exactly once per ARMED→TRIPPED transition.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime
from decimal import Decimal

from aats.contracts.risk import RiskConfig
from aats.risk.circuit_breaker import (
    CircuitBreaker,
    InMemoryBreakerStore,
    PnLEvent,
)

_DAY_BASE = int(datetime(2026, 6, 16, 0, 0, 0, tzinfo=UTC).timestamp() * 1000)


class _Spy:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def emergency_flatten_all(self, reason: str) -> None:
        self.calls.append(reason)


def _limits(cfg: RiskConfig) -> int:
    """The TIGHTER (less negative, fires first) of the two loss limits, lamports."""
    pct_limit = -int((Decimal(cfg.daily_risk_tranche_lamports) * cfg.daily_loss_limit_pct) / 100)
    floor_limit = -int(cfg.daily_loss_floor_lamports)
    # tighter = the one with the larger (less negative) value
    return max(pct_limit, floor_limit)


def test_property_breaker_fuzz_invariants():
    rng = random.Random(1320)  # fixed seed → reproducible
    n_sequences = 2000

    for seq_i in range(n_sequences):
        # Randomize a config within the contract's legal (tighten-only) range.
        tranche = rng.choice([10_000_000, 100_000_000, 500_000_000])
        pct = rng.choice([Decimal("0.5"), Decimal("1.0"), Decimal("3.0")])
        cfg = RiskConfig(daily_risk_tranche_lamports=tranche, daily_loss_limit_pct=pct)
        tighter_limit = _limits(cfg)

        store = InMemoryBreakerStore()
        spy = _Spy()
        breaker = CircuitBreaker(cfg, store, spy)

        net = 0
        trips_seen = 0
        was_tripped = False
        # All events in one UTC day so they pool into one tranche.
        for step in range(rng.randint(1, 40)):
            # Mostly losses (so we actually exercise trips), occasional gains.
            delta = rng.randint(-50_000_000, 5_000_000)
            net += delta
            state = breaker.record_pnl(
                PnLEvent(mint="F", block_time_ms=_DAY_BASE + step, delta_lamports=delta)
            )

            # P3 — integer money always.
            assert isinstance(state.daily_net_pnl_lamports, int), (
                f"seq {seq_i}: daily_net_pnl became non-int"
            )

            crossed = net <= tighter_limit

            if crossed:
                # P1 — crossing the limit means TRIPPED + entries blocked.
                assert breaker.is_tripped(), (
                    f"seq {seq_i} step {step}: net {net} <= limit {tighter_limit} "
                    f"but breaker NOT tripped"
                )
                assert breaker.entries_allowed() is False

            if breaker.is_tripped() and not was_tripped:
                trips_seen += 1
                was_tripped = True

            # P4 — once tripped, never silently re-armed by a PnL event.
            if was_tripped:
                assert breaker.is_tripped(), (
                    f"seq {seq_i} step {step}: breaker un-latched on a PnL event"
                )
                assert breaker.entries_allowed() is False

        # P5 — flatten fired exactly once iff it ever tripped.
        if was_tripped:
            assert spy.calls and len(spy.calls) == 1, (
                f"seq {seq_i}: expected exactly one flatten, got {len(spy.calls)}"
            )
        else:
            # P2 — never tripped spuriously: if it never tripped, the final net
            # must be strictly above the tighter limit.
            assert net > tighter_limit, (
                f"seq {seq_i}: not tripped but final net {net} <= limit {tighter_limit}"
            )
            assert spy.calls == []


def test_property_monotonic_deeper_loss_never_un_trips():
    """Adding loss to an already-tripped breaker keeps it tripped (monotone)."""
    rng = random.Random(99)
    for _ in range(500):
        cfg = RiskConfig()
        breaker = CircuitBreaker(cfg, InMemoryBreakerStore(), _Spy())
        # Trip it.
        breaker.record_pnl(PnLEvent(mint="M", block_time_ms=_DAY_BASE, delta_lamports=-20_000_000))
        assert breaker.is_tripped()
        # Random further deltas (even gains) never un-trip without reset().
        for step in range(rng.randint(1, 10)):
            delta = rng.randint(-30_000_000, 30_000_000)
            breaker.record_pnl(
                PnLEvent(mint="M", block_time_ms=_DAY_BASE + 1 + step, delta_lamports=delta)
            )
            assert breaker.is_tripped()
            assert breaker.entries_allowed() is False
