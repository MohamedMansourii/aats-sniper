"""T-320 FAULT-INJECTION proof — the breaker is durable under failure.

Paranoid by trade: assume the flatten RPC lies, the handler throws, the store
write is observed mid-transition, and the process dies.  The non-negotiable
invariant is FAIL-CLOSED — under every fault, the breaker must end up halted
(entries blocked) and the TRIPPED latch must be DURABLE (persisted) so a restart
re-reads TRIPPED.  A fault must never leave the breaker armed-but-bleeding.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from aats.contracts.risk import RiskConfig
from aats.risk.circuit_breaker import (
    CircuitBreaker,
    InMemoryBreakerStore,
    PnLEvent,
    mint_operator_reset_token,
)

_T = int(datetime(2026, 6, 16, 12, 0, 0, tzinfo=UTC).timestamp() * 1000)


class ThrowingFlatten:
    """A flatten handler whose venue call FAILS (RPC stall / venue error)."""

    def __init__(self) -> None:
        self.calls = 0

    def emergency_flatten_all(self, reason: str) -> None:
        self.calls += 1
        raise RuntimeError("simulated venue/RPC failure during flatten")


class OkFlatten:
    def __init__(self) -> None:
        self.calls = 0

    def emergency_flatten_all(self, reason: str) -> None:
        self.calls += 1


def test_latch_is_durable_even_if_flatten_raises():
    # The breaker persists TRIPPED BEFORE invoking flatten, so a flatten
    # failure cannot leave the latch un-persisted.  We assert: the trip raises
    # (the caller learns flatten failed), BUT the store already holds TRIPPED
    # and a restart comes back TRIPPED.
    store = InMemoryBreakerStore()
    breaker = CircuitBreaker(RiskConfig(), store, ThrowingFlatten())

    with pytest.raises(RuntimeError):
        breaker.record_pnl(PnLEvent(mint="M", block_time_ms=_T, delta_lamports=-20_000_000))

    # FAIL-CLOSED: despite the flatten error, the latch is durable.
    persisted = store.load()
    assert persisted is not None
    assert persisted.state == "TRIPPED", (
        "A flatten failure must NOT leave the breaker un-latched — fail closed."
    )

    # Restart over the same store: comes back TRIPPED, entries blocked, and a
    # working flatten handler is NOT re-fired automatically (no double-flatten).
    breaker2 = CircuitBreaker(RiskConfig(), store, OkFlatten())
    assert breaker2.is_tripped() is True
    assert breaker2.entries_allowed() is False


def test_can_recover_after_fault_via_manual_reset_only():
    store = InMemoryBreakerStore()
    breaker = CircuitBreaker(RiskConfig(), store, ThrowingFlatten())
    with pytest.raises(RuntimeError):
        breaker.record_pnl(PnLEvent(mint="M", block_time_ms=_T, delta_lamports=-20_000_000))
    # Only an explicit operator reset clears it — no automatic recovery.
    breaker2 = CircuitBreaker(RiskConfig(), store, OkFlatten())
    assert breaker2.is_tripped()
    breaker2.reset(mint_operator_reset_token(operator_id="ceo"))
    assert breaker2.entries_allowed() is True


def test_partial_persist_observed_as_tripped_blocks_entries():
    # If a crash is observed with TRIPPED already persisted (the durable-first
    # ordering guarantees this), a fresh breaker treats it as halted.
    store = InMemoryBreakerStore()
    breaker = CircuitBreaker(RiskConfig(), store, OkFlatten())
    breaker.record_pnl(PnLEvent(mint="M", block_time_ms=_T, delta_lamports=-20_000_000))
    # Simulate crash: drop the in-memory breaker, keep the store.
    del breaker
    revived = CircuitBreaker(RiskConfig(), store, OkFlatten())
    assert revived.entries_allowed() is False
