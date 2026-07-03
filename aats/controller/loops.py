"""M3 Controller — ControllerOrchestrator (wires the three loops together).

T-340: The orchestrator integrates:
  - SnipeLoop: processes new LaunchEvents (called from the event bus consumer)
  - FastLoop:  ticks deterministically at ~100ms intervals
  - SlowLoop:  processes events at a slower cadence (seconds-to-minutes)
  - Reconciler: startup + continuous reconciliation
  - SlowLoopEnrichmentWiring (optional): drives the E14/E17/E19 catastrophic-
    exit flag PRODUCERS (see aats.controller.enrichment_wiring) off the FAST
    hot path — event-driven (on_decoded_sell) or SLOW-cadence periodic
    (run_slow_enrichment_tick).

This module provides the ControllerOrchestrator that:
  1. On startup: runs the reconciler before any loop acts.
  2. On each new LaunchEvent: dispatches to SLOW first (pre-stage score),
     then to SNIPE (entry decision).
  3. On each tick: runs FAST loop (stop/TP/OMS).
  4. On a decoded SELL (event-driven) or a periodic SLOW-cadence timer: drives
     the enrichment-tier flag producers — NEVER from tick().

DRY-RUN FIRST: the venue is injected; default = SimulationVenue.
"""

from __future__ import annotations

import logging
from typing import Any

from aats.contracts.events import LaunchEvent
from aats.contracts.venue import ExecutionVenue, FillResult
from aats.controller.control_api import KillSwitch
from aats.controller.enrichment_wiring import SlowLoopEnrichmentWiring
from aats.controller.fast_loop import FastLoop
from aats.controller.reconcile import Reconciler
from aats.controller.slow_loop import SlowLoop
from aats.controller.snipe_loop import SnipeLoop
from aats.controller.state import StateStore
from aats.ingestion.insider_dump import InsiderDumpSignal, SellObservation
from aats.risk.lp_unlock_gate import LpUnlockExitDecision
from aats.risk.sellability_reprobe import SellabilityReprobeResult

log = logging.getLogger(__name__)


class ControllerOrchestrator:
    """Wires the three loops + reconciler into a single runnable unit.

    This is the entry point for the E2E test and for the Python process that
    runs the SLOW loop + control plane (BLUEPRINT §4.2).

    In production (BLUEPRINT §4.1), the SNIPE + FAST loops run in Rust; only
    the SLOW loop runs here.  For the DRY-RUN / SimulationVenue paper run
    (T-340 deliverable), all three loops run in Python so the E2E test can
    exercise the full stack without a Rust process.
    """

    def __init__(
        self,
        store: StateStore,
        venue: ExecutionVenue,
        snipe_loop: SnipeLoop,
        fast_loop: FastLoop,
        slow_loop: SlowLoop,
        reconciler: Reconciler,
        kill_switch: KillSwitch,
        *,
        enrichment: SlowLoopEnrichmentWiring | None = None,
    ) -> None:
        """
        Args:
            enrichment: Optional SlowLoopEnrichmentWiring (2026-07-03 program-review
                        fix).  Drives the E14 insider-dump / E17 sellability-reprobe
                        / E19 lp-unlock-approaching flag PRODUCERS from the SLOW-loop
                        / event-driven path — OFF the FAST/SNIPE hot path.  When None
                        (legacy / partial integration), those three catastrophic-exit
                        flags simply never get SET (the FAST branch that reads them
                        is unaffected either way — it always treats absence as HOLD,
                        never as risk).  Production SHOULD inject this so all four
                        pre-set exit flags (narrative, insider_dump,
                        sellability_degraded, lp_unlock_approaching) are live.
        """
        self._store = store
        self._venue = venue
        self._snipe = snipe_loop
        self._fast = fast_loop
        self._slow = slow_loop
        self._reconciler = reconciler
        self._kill = kill_switch
        self._enrichment = enrichment
        self._started = False

    def startup(self, block_time_ms: int) -> list[str]:
        """Run startup reconciliation.  MUST be called before any loop runs."""
        actions = self._reconciler.startup_reconcile(self._fast, block_time_ms)
        self._started = True
        log.info("orchestrator.startup: reconciliation complete; %d actions", len(actions))
        return actions

    def on_launch_event(self, event: LaunchEvent) -> dict[str, Any]:
        """Process a new LaunchEvent through SLOW then SNIPE.

        Called by the event bus consumer for each new pool event.
        Returns a dict with the results of the slow and snipe steps.
        """
        if not self._started:
            raise RuntimeError("ControllerOrchestrator.startup() must be called first")

        result: dict[str, Any] = {"mint": event.mint}

        # --- SLOW loop: classify + pre-stage + reason (may call LLM) ---
        signal = self._slow.process_event(event)
        result["signal"] = signal

        # --- SNIPE loop: entry decision (no LLM; reads pre-staged KV) ---
        if self._kill.is_killed:
            result["snipe"] = "kill_switch_active"
        else:
            snipe_result = self._snipe.process_event(event)
            result["snipe"] = snipe_result

            # E14a: register the creator/dev wallet with the insider-dump detector
            # ONLY when SNIPE actually attempted an entry (a FillResult means
            # ENTERING was claimed and the venue was called — landed or not).
            # This bounds the detector's registration dict to traded mints only
            # (mirrors InsiderDumpDetector.reset_mint's close-time symmetry) and
            # runs off the FAST/SNIPE hot path (a single dict write, no IO).
            if self._enrichment is not None and isinstance(snipe_result, FillResult):
                self._enrichment.register_creator(event.mint, event.creator_wallet)

        return result

    def tick(self, block_time_ms: int, current_slot: int) -> dict[str, Any]:
        """Run one FAST-loop tick + continuous reconciliation.

        Called periodically (target: every 100ms in production).
        Returns a dict with tick actions and any reconcile actions.
        """
        if not self._started:
            raise RuntimeError("ControllerOrchestrator.startup() must be called first")

        fast_actions = self._fast.tick(block_time_ms, current_slot)
        recon_actions = self._reconciler.continuous_reconcile_tick(
            self._fast, block_time_ms, current_slot
        )
        return {
            "block_time_ms": block_time_ms,
            "slot": current_slot,
            "fast_actions": fast_actions,
            "reconcile_actions": recon_actions,
        }

    def on_fill(
        self, mint: str, fill: FillResult, block_time_ms: int
    ) -> bool:
        """Called by the fills-stream consumer to reconcile a fill."""
        return self._fast.reconcile_fill(mint, fill, block_time_ms)

    def on_decoded_sell(self, obs: SellObservation) -> InsiderDumpSignal | None:
        """Feed one decoded on-chain SELL through the E14a insider-dump detector.

        Called by the trade-event consumer — EVENT-DRIVEN, OFF the FAST/SNIPE
        hot path.  MUST NEVER be called from tick() or the SNIPE path.  A no-op
        (returns None) if no enrichment wiring is injected.
        """
        if self._enrichment is None:
            return None
        return self._enrichment.on_decoded_sell(obs)

    def run_slow_enrichment_tick(
        self, event_slot: int
    ) -> dict[str, list[SellabilityReprobeResult] | list[LpUnlockExitDecision]]:
        """Run the E17 sellability re-probe + E19 LP-unlock watch over ALL open
        positions, at the SLOW-loop cadence (seconds-to-minutes).

        Call this from a PERIODIC SLOW-loop driver task (e.g. every 5-10s) —
        NEVER from tick() (the FAST-loop driver).  The sellability re-probe
        re-runs a ~30-100ms sell-sim per open position and the LP-unlock watch
        may perform a schedule lookup per position; neither is bounded to the
        FAST loop's <=100ms tick budget, which is exactly why this is a
        SEPARATE method the caller must schedule off the hot path.

        A no-op (returns empty lists) if no enrichment wiring is injected.
        """
        if self._enrichment is None:
            return {"sellability": [], "lp_unlock": []}
        sellability = self._enrichment.run_sellability_reprobe(event_slot)
        lp_unlock = self._enrichment.run_lp_unlock_watch(event_slot)
        return {"sellability": sellability, "lp_unlock": lp_unlock}

    @property
    def enrichment(self) -> SlowLoopEnrichmentWiring | None:
        return self._enrichment

    @property
    def kill_switch(self) -> KillSwitch:
        return self._kill

    @property
    def fast_loop(self) -> FastLoop:
        return self._fast

    @property
    def slow_loop(self) -> SlowLoop:
        return self._slow

    @property
    def snipe_loop(self) -> SnipeLoop:
        return self._snipe
