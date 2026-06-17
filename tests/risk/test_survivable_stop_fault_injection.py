"""T-321 FAULT-INJECTION proof — the stop survives lying/throwing layers.

Paranoid by trade: assume the in-process exit RPC fails, the DMS flatten handler
throws, and the venue stalls.  The non-negotiable invariant is that NO single
layer failing leaves the position unprotected — a different independent layer
still flattens it, and the DMS latch is durable under a throwing handler
(fail-closed).
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from aats.contracts.venue import FillResult
from aats.risk.deadman import DeadMansSwitch
from aats.risk.secondary_enforcer import MarkTick, SecondaryStopEnforcer
from aats.risk.survivable_stop_coordinator import SurvivableStopCoordinator
from aats.risk.venue_native_stop import (
    SimulationRestingStopVenue,
    VenueNativeStopPlacer,
)

MINT = "So1111111111111111111111111111111111111111"


class _Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def advance(self, s: float) -> None:
        self.t += s


class _ThrowingExitVenue:
    """In-process Layer-2 exit RPC FAILS (venue/RPC error)."""

    name = "throwing-exit"

    def __init__(self) -> None:
        self.calls = 0

    def exit(self, intent, position) -> FillResult:  # noqa: ANN001
        self.calls += 1
        raise RuntimeError("simulated exit RPC failure")


class _ThrowingFlattenVenue:
    """DMS emergency_flatten FAILS."""

    def __init__(self) -> None:
        self.calls = 0

    def emergency_flatten(self, reason: str) -> None:
        self.calls += 1
        raise RuntimeError("simulated flatten RPC failure")


def test_layer1_protects_when_layer2_exit_throws():
    """Layer-2 in-process exit throws, but the OFF-BOX resting order still fires."""
    enforcer = SecondaryStopEnforcer(_ThrowingExitVenue())  # type: ignore[arg-type]
    resting_venue = SimulationRestingStopVenue()
    placer = VenueNativeStopPlacer(resting_venue)
    dms = DeadMansSwitch(_ThrowingFlattenVenue(), t_dms_seconds=60.0, clock=_Clock())
    coord = SurvivableStopCoordinator(enforcer=enforcer, venue_native=placer, deadman=dms)
    coord.arm_at_entry(mint=MINT, entry_fill_price=100, position=object())

    # Layer-2 fires on breach but its venue throws.
    with pytest.raises(RuntimeError):
        coord.on_tick(MarkTick(mint=MINT, mark_price=Decimal("50"), block_time_ms=1))

    # Layer-1 off-box keeper still flattens the position independently.
    fill = resting_venue.keeper_tick(MINT, Decimal("50"))
    assert fill is not None and fill.landed


def test_dms_latches_even_when_flatten_throws():
    """FAIL-CLOSED: a throwing flatten still latches FIRED (no re-fire loop)."""
    clock = _Clock()
    flatten = _ThrowingFlattenVenue()
    dms = DeadMansSwitch(flatten, t_dms_seconds=60.0, clock=clock)
    clock.advance(61.0)  # heartbeat lost
    with pytest.raises(RuntimeError):
        dms.check()  # fires, handler throws AFTER the latch is set
    assert dms.is_fired() is True  # latched despite the throw (fail-closed)
    # A subsequent check does NOT re-invoke the handler (latched).
    flatten.calls = 0
    assert dms.check() is False
    assert flatten.calls == 0


def test_dms_does_not_fire_with_fresh_heartbeats():
    clock = _Clock()
    flatten = _ThrowingFlattenVenue()
    dms = DeadMansSwitch(flatten, t_dms_seconds=60.0, clock=clock)
    for _ in range(10):
        clock.advance(30.0)
        dms.heartbeat()  # loop alive: beat every 30s, under the 60s budget
        assert dms.check() is False
    assert dms.is_fired() is False
    assert flatten.calls == 0
