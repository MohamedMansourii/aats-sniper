"""T-321 — THE survivability proof: the stop holds with the bot process DEAD.

This is the test the whole module exists for (ADR-0008).  We arm all three
layers, then SIMULATE PROCESS DEATH by ceasing to call the in-process FAST loop
(no more on_tick / no more heartbeat) and prove the position STILL gets flattened
by the layers that live in a different liveness/failure domain:

  * Layer 1 (venue-native resting order): an OFF-BOX keeper fires the SELL
    trigger on the on-chain price breach while our loop is dead.
  * Layer 3 (dead-man's switch): with no heartbeat for > T_DMS, a SEPARATE
    failure domain auto-flattens the whole book.

We also prove the steady-state (process ALIVE) Layer-2 path fires, so the test
covers both "alive" and "dead" worlds against the SAME fixed trigger.
"""

from __future__ import annotations

from decimal import Decimal

from aats.contracts.venue import FillResult, SubmitMode
from aats.risk.deadman import DeadMansSwitch
from aats.risk.secondary_enforcer import MarkTick, SecondaryStopEnforcer
from aats.risk.survivable_stop import StopState
from aats.risk.survivable_stop_coordinator import SurvivableStopCoordinator
from aats.risk.venue_native_stop import (
    SimulationRestingStopVenue,
    VenueNativeStopPlacer,
)

MINT = "So1111111111111111111111111111111111111111"


# A controllable monotonic clock so we can 'kill' the heartbeat deterministically.
class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, secs: float) -> None:
        self.t += secs


class _ExitSpyVenue:
    """Records in-process Layer-2 exits (no network)."""

    name = "exit-spy"

    def __init__(self) -> None:
        self.exits: list[object] = []

    def exit(self, intent, position) -> FillResult:  # noqa: ANN001 - test stub
        self.exits.append(intent)
        return FillResult(landed=True, reason="sim_exit")


class _EmergencyFlattenVenue:
    """Records DMS book-wide emergency flattens (no network; DRY-RUN safe)."""

    def __init__(self) -> None:
        self.flattens: list[str] = []

    def emergency_flatten(self, reason: str) -> None:
        self.flattens.append(reason)


def _build(clock: _Clock, t_dms: float = 60.0):
    exit_venue = _ExitSpyVenue()
    enforcer = SecondaryStopEnforcer(exit_venue)  # type: ignore[arg-type]
    resting_venue = SimulationRestingStopVenue()
    placer = VenueNativeStopPlacer(resting_venue)
    flatten_venue = _EmergencyFlattenVenue()
    dms = DeadMansSwitch(flatten_venue, t_dms_seconds=t_dms, clock=clock)
    coord = SurvivableStopCoordinator(enforcer=enforcer, venue_native=placer, deadman=dms)
    return coord, exit_venue, resting_venue, flatten_venue, dms


# ---------------------------------------------------------------------------
# Layer 1 — venue-native resting order survives a DEAD process
# ---------------------------------------------------------------------------
def test_venue_native_resting_stop_fires_with_process_dead():
    clock = _Clock()
    coord, exit_venue, resting_venue, _flatten, _dms = _build(clock)

    arm = coord.arm_at_entry(mint=MINT, entry_fill_price=100, position=object())
    # The off-box order rests at the FIXED trigger (60 = -40% of 100).
    assert arm.resting_order.trigger_price == Decimal("60")
    assert arm.resting_order.order_id is not None  # placed off-box
    assert resting_venue.submit_mode == SubmitMode.SIMULATION  # no network, ever

    # --- SIMULATE PROCESS DEATH: we stop calling coord.on_tick() entirely. ---
    # No FAST loop, no heartbeat, no in-process enforcer running.

    # The OFF-BOX keeper (a different process) observes the chain price breach.
    fill = resting_venue.keeper_tick(MINT, mark_price=Decimal("55"))

    # The position was flattened by the off-box order WITH OUR LOOP DEAD.
    assert fill is not None
    assert fill.landed is True
    assert fill.reason == "venue_native_resting_stop_filled"
    assert len(resting_venue.fired_exits) == 1
    # And the in-process enforcer never ran (no Layer-2 exit recorded).
    assert exit_venue.exits == []


# ---------------------------------------------------------------------------
# Layer 3 — dead-man's switch flattens the book when the heartbeat dies
# ---------------------------------------------------------------------------
def test_dead_mans_switch_flattens_when_heartbeat_lost():
    clock = _Clock()
    coord, _exit_venue, _resting, flatten_venue, dms = _build(clock, t_dms=60.0)
    coord.arm_at_entry(mint=MINT, entry_fill_price=100, position=object())

    # Steady state: the FAST loop beats and the DMS stays quiet.
    coord.on_tick(MarkTick(mint=MINT, mark_price=Decimal("90"), block_time_ms=1))
    assert dms.check() is False
    assert flatten_venue.flattens == []

    # --- SIMULATE PROCESS DEATH: the FAST loop stops; no more heartbeats. ---
    clock.advance(61.0)  # > T_DMS with no beat

    # The SEPARATE failure domain (watchdog) checks and fires the flatten.
    fired = dms.check()
    assert fired is True
    assert len(flatten_venue.flattens) == 1
    assert "dead_mans_switch" in flatten_venue.flattens[0]

    # Fires EXACTLY once and latches.
    clock.advance(120.0)
    assert dms.check() is False
    assert len(flatten_venue.flattens) == 1
    assert dms.is_fired() is True


# ---------------------------------------------------------------------------
# Steady-state (process ALIVE) Layer-2 still fires on a breach
# ---------------------------------------------------------------------------
def test_in_process_enforcer_fires_when_alive():
    clock = _Clock()
    coord, exit_venue, _resting, flatten_venue, dms = _build(clock, t_dms=60.0)
    coord.arm_at_entry(mint=MINT, entry_fill_price=100, position=object())

    # Loop is alive: on_tick beats the DMS and runs the breach decision.
    fill = coord.on_tick(MarkTick(mint=MINT, mark_price=Decimal("59"), block_time_ms=1))
    assert fill is not None and fill.landed
    assert len(exit_venue.exits) == 1  # Layer 2 fired
    # DMS never needed to fire because the loop is alive and beating.
    assert dms.check() is False
    assert flatten_venue.flattens == []


# ---------------------------------------------------------------------------
# Belt-and-braces: ALL THREE agree on the SAME fixed trigger
# ---------------------------------------------------------------------------
def test_all_layers_share_the_same_fixed_trigger():
    clock = _Clock()
    coord, _exit_venue, resting_venue, _flatten, _dms = _build(clock)
    arm = coord.arm_at_entry(mint=MINT, entry_fill_price=Decimal("0.001"), position=object())
    enforcer_stop: StopState = coord._enforcer.stop_for(MINT)  # type: ignore[assignment]
    placed = resting_venue._orders[MINT].order  # off-box order
    # Layer 1 and Layer 2 enforce the identical fixed price.
    assert enforcer_stop.trigger_price == arm.stop.trigger_price
    assert placed.trigger_price == arm.stop.trigger_price
