"""M3 Controller — ControllerOrchestrator (wires the three loops together).

T-340: The orchestrator integrates:
  - SnipeLoop: processes new LaunchEvents (called from the event bus consumer)
  - FastLoop:  ticks deterministically at ~100ms intervals
  - SlowLoop:  processes events at a slower cadence (seconds-to-minutes)
  - Reconciler: startup + continuous reconciliation

This module provides the ControllerOrchestrator that:
  1. On startup: runs the reconciler before any loop acts.
  2. On each new LaunchEvent: dispatches to SLOW first (pre-stage score),
     then to SNIPE (entry decision).
  3. On each tick: runs FAST loop (stop/TP/OMS).

DRY-RUN FIRST: the venue is injected; default = SimulationVenue.
"""

from __future__ import annotations

import logging
from typing import Any

from aats.contracts.events import LaunchEvent
from aats.contracts.venue import ExecutionVenue, FillResult
from aats.controller.control_api import KillSwitch
from aats.controller.fast_loop import FastLoop
from aats.controller.reconcile import Reconciler
from aats.controller.slow_loop import SlowLoop
from aats.controller.snipe_loop import SnipeLoop
from aats.controller.state import StateStore

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
    ) -> None:
        self._store = store
        self._venue = venue
        self._snipe = snipe_loop
        self._fast = fast_loop
        self._slow = slow_loop
        self._reconciler = reconciler
        self._kill = kill_switch
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
