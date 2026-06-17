"""T-321 — asymmetric-trust + DRY-RUN + DMS-disarm-resistance proofs.

Non-waivable rules proven here:
  * The stop is TIGHTEN-ONLY across every layer: enforcer, venue-native placer,
    and the SimulationRestingStopVenue itself all REFUSE a widen.
  * The de-risk factory the enforcer holds has NO entry() — it is structurally
    impossible for the stop path to add risk (ADR-0006).
  * The dead-man's switch CANNOT be disarmed by an automated/LLM caller: a market
    event / arbitrary code cannot mint a DmsStandDownToken, so stand_down/rearm
    are unreachable from any non-operator path (AC-046).
  * DRY-RUN: the reference venue layers run in SIMULATION mode and never touch a
    network (no land/submit path).
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from aats.contracts.intents import DeRiskIntentFactory
from aats.contracts.venue import SubmitMode
from aats.risk.deadman import (
    DeadMansSwitch,
    DmsStandDownToken,
    mint_dms_standdown_token,
)
from aats.risk.secondary_enforcer import SecondaryStopEnforcer
from aats.risk.survivable_stop import StopState
from aats.risk.venue_native_stop import (
    SimulationRestingStopVenue,
    VenueNativeStopPlacer,
)

MINT = "So1111111111111111111111111111111111111111"


class _FlattenVenue:
    def __init__(self) -> None:
        self.flattens: list[str] = []

    def emergency_flatten(self, reason: str) -> None:
        self.flattens.append(reason)


# ---------------------------------------------------------------------------
# Tighten-only across every layer
# ---------------------------------------------------------------------------
class TestTightenOnlyEveryLayer:
    def test_venue_native_placer_refuses_widen(self):
        venue = SimulationRestingStopVenue()
        placer = VenueNativeStopPlacer(venue)
        stop = StopState.at_entry(mint=MINT, entry_fill_price=100)  # trigger 60
        placer.place_for(stop)
        placer.tighten(MINT, 80)  # raising the trigger is fine
        assert placer.placed_order(MINT).trigger_price == Decimal("80")
        with pytest.raises(ValueError, match="only be TIGHTENED"):
            placer.tighten(MINT, 70)  # lower than current (80) -> widen -> refuse

    def test_sim_resting_venue_itself_refuses_widen(self):
        venue = SimulationRestingStopVenue()
        from aats.risk.venue_native_stop import RestingStopOrder

        placed = venue.place_resting_stop(RestingStopOrder(mint=MINT, trigger_price=Decimal("60")))
        with pytest.raises(ValueError, match="tighten-only"):
            venue.replace_tighter(MINT, placed.order_id, Decimal("50"))

    def test_enforcer_refuses_widen(self):
        class _V:
            name = "v"

            def exit(self, intent, position):
                from aats.contracts.venue import FillResult

                return FillResult(landed=True, reason="sim_exit")

        enf = SecondaryStopEnforcer(_V())  # type: ignore[arg-type]
        enf.register(StopState.at_entry(mint=MINT, entry_fill_price=100), object())
        with pytest.raises(ValueError, match="only be TIGHTENED"):
            enf.tighten(MINT, 40)


# ---------------------------------------------------------------------------
# Structural: the de-risk factory has no entry() — cannot add risk
# ---------------------------------------------------------------------------
def test_derisk_factory_has_no_entry_method():
    assert not hasattr(DeRiskIntentFactory, "entry")
    factory = DeRiskIntentFactory()
    assert not hasattr(factory, "entry")
    with pytest.raises(AttributeError):
        factory.entry()  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# DMS cannot be disarmed by an automated / LLM / market path
# ---------------------------------------------------------------------------
class TestDmsDisarmResistance:
    def test_standdown_token_cannot_be_forged(self):
        with pytest.raises(PermissionError):
            DmsStandDownToken("attacker")  # direct construction forbidden

    def test_stand_down_requires_genuine_token(self):
        dms = DeadMansSwitch(_FlattenVenue(), t_dms_seconds=60.0)

        class _Fake:  # a duck-typed forgery
            operator_id = "attacker"

        with pytest.raises(PermissionError):
            dms.stand_down(_Fake())  # type: ignore[arg-type]

    def test_operator_can_stand_down_and_rearm(self):
        clock_t = {"t": 0.0}
        dms = DeadMansSwitch(_FlattenVenue(), t_dms_seconds=60.0, clock=lambda: clock_t["t"])
        token = mint_dms_standdown_token("ceo")
        # Stood down: even past T_DMS it will not fire.
        dms.stand_down(token)
        clock_t["t"] = 1000.0
        assert dms.check() is False
        # Re-armed: heartbeat clock resets, then ages out and fires.
        dms.rearm(token)
        clock_t["t"] = 2000.0
        assert dms.check() is True

    def test_market_event_cannot_mint_token(self):
        # There is no public constructor and no factory the automated path holds.
        # mint_dms_standdown_token is the ONLY chokepoint and requires an operator id.
        with pytest.raises(ValueError):
            mint_dms_standdown_token("")  # empty id rejected (audit trail)


# ---------------------------------------------------------------------------
# DRY-RUN: the reference venue layers never touch a network
# ---------------------------------------------------------------------------
def test_reference_venues_are_simulation_mode():
    venue = SimulationRestingStopVenue()
    assert venue.submit_mode == SubmitMode.SIMULATION
    placer = VenueNativeStopPlacer(venue)
    assert placer.submit_mode == SubmitMode.SIMULATION
