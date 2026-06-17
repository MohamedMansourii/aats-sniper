"""T-321 — survivable-stop COORDINATOR: arm all three layers at entry.

ADR-0008 says the stop must survive (a) a slow tick, (b) a dead process, (c) a
host/network partition.  No single layer covers all three, so on every fill we
arm ALL THREE independent layers at the SAME fixed trigger:

  Layer 1  venue-native resting order  (VenueNativeStopPlacer)  — off-box keeper
  Layer 2  in-process FAST enforcer    (SecondaryStopEnforcer)  — steady-state
  Layer 3  dead-man's switch heartbeat (DeadMansSwitch)         — separate domain

This module is the RISK-side facade the orchestration layer (T-342) drives: it
exposes `arm_at_entry`, `on_tick`, `tighten`, and `on_exit` and fans them out to
the three layers, keeping the trigger consistent across all of them.

NON-WAIVABLE invariants are enforced in the underlying layers (tighten-only,
de-risk-only, integer money, point-in-time, no-LLM-on-decision); this coordinator
adds no capability that could relax them — it only fans out.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from decimal import Decimal

from aats.contracts.venue import FillResult
from aats.risk.deadman import DeadMansSwitch
from aats.risk.secondary_enforcer import MarkTick, SecondaryStopEnforcer
from aats.risk.survivable_stop import StopState, StopThresholds
from aats.risk.venue_native_stop import RestingStopOrder, VenueNativeStopPlacer


@dataclass(frozen=True)
class ArmResult:
    """What got armed at entry, for audit / telemetry / operator surfaces."""

    stop: StopState
    resting_order: RestingStopOrder


class SurvivableStopCoordinator:
    """Arms + drives the three independent stop layers at a single fixed trigger.

    The DMS is shared across all positions (it flattens the whole book on
    heartbeat loss), so it is constructed once and passed in.  The enforcer and
    the venue-native placer are also shared singletons; arm_at_entry registers a
    new position with each.
    """

    def __init__(
        self,
        *,
        enforcer: SecondaryStopEnforcer,
        venue_native: VenueNativeStopPlacer,
        deadman: DeadMansSwitch,
    ) -> None:
        self._enforcer = enforcer
        self._venue_native = venue_native
        self._dms = deadman

    def arm_at_entry(
        self,
        *,
        mint: str,
        entry_fill_price: Decimal | int | str,
        position: object,
        thresholds: StopThresholds | None = None,
        fraction_bps: int = 10_000,
    ) -> ArmResult:
        """Arm all three layers for a freshly-filled position at the fixed trigger."""
        stop = StopState.at_entry(
            mint=mint,
            entry_fill_price=entry_fill_price,
            thresholds=thresholds,
        )
        # Layer 2: in-process enforcer.
        self._enforcer.register(stop, position)
        # Layer 1: off-box venue-native resting order at the same fixed trigger.
        resting = self._venue_native.place_for(stop, fraction_bps=fraction_bps)
        # Layer 3 (DMS) is book-wide and already armed; nothing per-position to do
        # except keep its heartbeat fresh from the FAST loop (orchestration owns
        # that wiring, T-342).
        return ArmResult(stop=stop, resting_order=resting)

    def on_tick(self, tick: MarkTick) -> FillResult | None:
        """FAST-loop tick: prove liveness to the DMS, then run the Layer-2 stop.

        The heartbeat is sent FIRST so that as long as the FAST loop is calling
        on_tick the DMS stays satisfied; the moment the loop stops calling, the
        heartbeat ages out and Layer 3 takes over.
        """
        self._dms.heartbeat()
        return self._enforcer.on_tick(tick)

    def tighten(self, mint: str, new_trigger: Decimal | int | str) -> StopState:
        """TIGHTEN-ONLY, fanned out to enforcer AND venue-native (de-risk).

        Both layers refuse a widen independently; we tighten the enforcer first
        (in-memory, cannot fail on a valid tighten) then the off-box order.
        """
        tightened = self._enforcer.tighten(mint, new_trigger)
        # Keep Layer 1 in sync at the same tighter trigger.  No off-box order
        # placed (e.g. a venue without native triggers) is not fatal — the
        # in-process tighten still applies and Layers 2/3 remain.
        with contextlib.suppress(KeyError):
            self._venue_native.tighten(mint, tightened.trigger_price)
        return tightened

    def on_exit(self, mint: str) -> None:
        """Position closed: deregister Layer 2 and cancel the off-box order."""
        self._enforcer.deregister(mint)
        with contextlib.suppress(KeyError):
            self._venue_native.cancel(mint)
