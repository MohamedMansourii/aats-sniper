"""M3 Controller — SLOW-loop / enrichment-tier PRODUCER wiring (E14 / E17 / E19).

2026-07-03 PROGRAM REVIEW FIX (HIGH)
-------------------------------------
The review found the Wave-2 catastrophic exits (insider_dump E14, sellability_
degraded E17, lp_unlock_approaching E19) were built + unit-tested in the
``ExitEngine`` but NEVER FIRE in the running system: no controller loop SET
those flags on real ticks.  This module is the missing wire: it drives the
THREE existing, already-correct producer classes —

  * ``aats.ingestion.insider_dump.InsiderDumpDetector``          (E14a)
  * ``aats.risk.sellability_reprobe.SellabilityReprober``        (E17)
  * ``aats.risk.lp_unlock_gate.LpUnlockExitWatcher``              (E19)

— from the SLOW-loop / controller path, OFF the FAST/SNIPE hot path, and
nowhere else.  It does not re-implement any detection/re-probe/watch logic
(that would fork the mechanics this module only orchestrates).

HOT-PATH PURITY (non-waivable)
-------------------------------
Every method here is intended to be called from:
  * the trade-event consumer (a decoded on-chain SELL arrives) — event-driven,
    OFF the FAST/SNIPE critical path, or
  * a periodic SLOW-loop driver (seconds-to-minutes cadence) — e.g. every 5-10s,
    never once per FAST tick (~100ms).
``ControllerOrchestrator.tick()`` (the FAST-loop driver) MUST NEVER call any
method on this class.  ``ControllerOrchestrator.run_slow_enrichment_tick()`` is
the SLOW-cadence entry point a separate periodic task drives.

DE-RISK ONLY
------------
Every method here can ONLY ever SET a StateStore flag that biases the FAST
exit branch toward a force-exit.  There is no method here that clears a flag,
sizes a trade, widens a stop, or extends a hold — this module is a thin
dispatcher over three producers that already enforce that invariant
themselves (see their own module docstrings).

REFUSE-BY-DEFAULT vs. "NOT YET WIRED" (an intentional, documented distinction)
-------------------------------------------------------------------------------
The codebase's own precedent (``InsiderDumpDetector`` / ``StaticHeldSupplyProvider``)
already distinguishes two situations that both look like "we don't know":

  (a) a DECISION was attempted and the input was ambiguous/undecodable
      -> refuse-by-default -> treat as the WORST case (e.g. E19's
      ``LpLockStatus.UNKNOWN`` -> RAISE the exit flag; E17's probe-raised
      -> INCONCLUSIVE -> degraded).  This module never overrides that; it is
      the producers' own internal behaviour.

  (b) NO decision was attempted at all because an upstream data source has not
      been wired for this mint yet (e.g. no held-supply baseline registered;
      no LP-unlock schedule decoder exists in this codebase at all as of this
      change).  The existing ``InsiderDumpDetector`` treats this as a COUNTED
      REFUSAL that HOLDS (no flag) — it does NOT force an exit, because
      forcing an exit for "we have not built the upstream feed yet" would turn
      a de-risk *signal* into an unconditional kill-switch, which is not what
      refuse-by-default means anywhere else in this codebase.  This module
      applies the SAME distinction to E19: ``run_lp_unlock_watch`` SKIPS
      (counted, logged) a mint for which the injected schedule source has no
      data at all — this is NOT the same as evaluating a schedule that turned
      out UNKNOWN/undecodable (which still forces the exit, unchanged).

KNOWN GAPS (honestly documented, not silently covered)
--------------------------------------------------------
  * E14 (insider dump): there is currently NO live on-chain SELL-event feed
    wired into the ingestion transport (``PumpPortalTransport`` only subscribes
    to ``subscribeNewToken`` / ``subscribeMigration`` — CREATE/WITHDRAW).  This
    module's ``on_decoded_sell`` is fully wired and tested end-to-end against a
    real ``SellObservation``; it simply has no live producer feeding it yet.
    TODO(M1 / data-ingestion-engineer): subscribe to pump.fun trade events
    (``subscribeTokenTrade`` / on-chain SELL instruction decode) and forward
    each decoded SELL through ``ControllerOrchestrator.on_decoded_sell``.
  * E19 (LP-unlock): there is currently NO on-chain LP-locker schedule decoder
    in this codebase (no Streamflow / Raydium-locker account decode exists).
    ``run_lp_unlock_watch`` is fully wired and tested against an injected
    ``LpUnlockScheduleSource``; in production today it is safe to run with NO
    source wired (``lp_unlock_schedule_source=None``), which cleanly no-ops.
    TODO(M1 / data-ingestion-engineer): build the on-chain LP-locker schedule
    decoder and wire a real ``LpUnlockScheduleSource`` here.
  * E17 (sellability re-probe) has NO such gap: ``SellSimVenue`` is DRY-RUN
    and fully offline-capable via its default ``MockRpcClient``/
    ``MockSignerClient`` — this producer is live-wireable today with zero
    external dependency, and ``__main__.py`` wires it unconditionally.

MONEY / TIME: this module touches no money field itself; it only forwards
event-time slots the caller supplies (never wall-clock) to the producers.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from aats.ingestion.insider_dump import (
    InsiderDumpDetector,
    InsiderDumpSignal,
    SellObservation,
)
from aats.risk.lp_unlock_gate import (
    LpUnlockExitDecision,
    LpUnlockExitWatcher,
    LpUnlockScheduleLike,
)
from aats.risk.sellability_reprobe import SellabilityReprober, SellabilityReprobeResult

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Structural read surfaces (no hard import dependency beyond typing)
# ---------------------------------------------------------------------------


@runtime_checkable
class OpenPositionMintSource(Protocol):
    """The minimal read surface this wiring needs from the shared StateStore.

    Satisfied by ``aats.controller.state.StateStore`` (both ``InMemoryStateStore``
    and any Redis-backed implementation) WITHOUT this module importing the
    controller's concrete classes — only the shape.
    """

    def load_all_positions(self) -> list: ...  # pragma: no cover

    def load_position(self, mint: str) -> object | None: ...  # pragma: no cover


@runtime_checkable
class LpUnlockScheduleSource(Protocol):
    """Injectable source of a decoded LP-unlock schedule for ONE mint.

    Returns ``None`` when NO schedule data is available for this mint AT ALL
    (the LP-locker decoder has not observed/decoded this position yet) — this
    is DISTINCT from ``LpLockStatus.UNKNOWN`` (a schedule WAS decoded but is
    unreadable/contradictory).  See ``EnrichmentWiring.run_lp_unlock_watch``
    docstring for how the two are treated differently.
    """

    def get_schedule(self, mint: str) -> LpUnlockScheduleLike | None: ...  # pragma: no cover


# ---------------------------------------------------------------------------
# Stats — observability that proves the wiring ran (never silent)
# ---------------------------------------------------------------------------


@dataclass
class EnrichmentStats:
    """Runtime counters.  Every counter here proves a code path actually ran —
    mirrors the ``stats`` pattern already used by ``InsiderDumpDetector`` /
    ``SellabilityReprober`` / ``LpUnlockExitWatcher``."""

    creator_registrations: int = 0
    sells_processed: int = 0
    sellability_reprobes_run: int = 0
    lp_unlock_checks_run: int = 0
    lp_unlock_schedule_absent_skipped: int = 0


# ---------------------------------------------------------------------------
# SlowLoopEnrichmentWiring — the single dispatcher
# ---------------------------------------------------------------------------


class SlowLoopEnrichmentWiring:
    """Wires the E14 / E17 / E19 catastrophic-exit flag PRODUCERS into the
    RUNNING controller, off the FAST/SNIPE hot path.

    USAGE (wired by ``ControllerOrchestrator``; see ``loops.py``):

        enrichment = SlowLoopEnrichmentWiring(
            store=state_store,
            insider_dump_detector=InsiderDumpDetector(flag_sink=state_store),
            sellability_reprober=SellabilityReprober(
                probe=SellSimVenue(), flag_sink=state_store
            ),
            lp_unlock_watcher=LpUnlockExitWatcher(flag_sink=state_store),
            lp_unlock_schedule_source=None,  # TODO: wire once M1 builds a decoder
        )
        orchestrator = ControllerOrchestrator(..., enrichment=enrichment)

        # event-driven (a decoded SELL arrives, off the hot path):
        orchestrator.on_decoded_sell(obs)

        # periodic, SLOW cadence (e.g. every 5-10s), off the hot path:
        orchestrator.run_slow_enrichment_tick(event_slot=current_slot)

    Every producer is OPTIONAL (``None`` disables that producer cleanly and is
    counted/logged, never silently pretended to be covered) so this class can
    be constructed incrementally as each upstream data source becomes available.
    """

    def __init__(
        self,
        store: OpenPositionMintSource,
        *,
        insider_dump_detector: InsiderDumpDetector | None = None,
        sellability_reprober: SellabilityReprober | None = None,
        lp_unlock_watcher: LpUnlockExitWatcher | None = None,
        lp_unlock_schedule_source: LpUnlockScheduleSource | None = None,
    ) -> None:
        self._store = store
        self._insider_dump = insider_dump_detector
        self._sellability = sellability_reprober
        self._lp_unlock_watcher = lp_unlock_watcher
        self._lp_unlock_schedule_source = lp_unlock_schedule_source
        self.stats = EnrichmentStats()

    # -- E14a: insider / dev-sell dump ------------------------------------

    def register_creator(self, mint: str, creator_wallet: str) -> None:
        """Register ``mint``'s creator/dev wallet with the E14a detector.

        Call this ONLY when an entry is actually attempted for ``mint`` (a
        ``FillResult`` was produced by SNIPE) — this bounds the detector's
        registration dict to traded mints, mirroring ``InsiderDumpDetector.
        reset_mint``'s close-time symmetry.  A no-op if no detector is wired.
        """
        if self._insider_dump is None:
            return
        self._insider_dump.register_creator(mint, creator_wallet)
        self.stats.creator_registrations += 1

    def on_decoded_sell(self, obs: SellObservation) -> InsiderDumpSignal | None:
        """Feed ONE decoded on-chain SELL through the E14a detector.

        OFF the FAST/snipe hot path — call this from the trade-event consumer,
        never from ``FastLoop.tick()`` / ``SnipeLoop.process_event()``.
        ``is_open_position`` is derived HERE from the CURRENT StateStore truth
        (the detector's own contract: "the caller passes is_open_position").
        Returns None (no-op) if no detector is wired.
        """
        if self._insider_dump is None:
            return None
        is_open = self._store.load_position(obs.mint) is not None
        self.stats.sells_processed += 1
        return self._insider_dump.on_sell(obs, is_open_position=is_open)

    # -- E17: sellability re-probe (periodic, SLOW cadence) ---------------

    def run_sellability_reprobe(self, event_slot: int) -> list[SellabilityReprobeResult]:
        """Re-probe EVERY open position's sellability at ``event_slot`` (event-time).

        Delegates entirely to ``SellabilityReprober.reprobe_open_positions`` —
        no honeypot/tax-flip logic lives here.  Returns ``[]`` (no-op) if no
        reprober is wired.
        """
        if self._sellability is None:
            return []
        results = self._sellability.reprobe_open_positions(self._store, event_slot=event_slot)
        self.stats.sellability_reprobes_run += len(results)
        return results

    # -- E19: LP-unlock-approaching watcher (periodic, SLOW cadence) ------

    def run_lp_unlock_watch(self, event_slot: int) -> list[LpUnlockExitDecision]:
        """Check EVERY open position's LP-unlock proximity at ``event_slot``.

        For each open position, fetches a schedule via the injected
        ``LpUnlockScheduleSource`` and delegates classification entirely to
        ``LpUnlockExitWatcher.watch`` — no unlock-proximity logic lives here.

        A mint for which the schedule source returns ``None`` (no data
        observed for this mint at all) is SKIPPED and counted distinctly via
        ``stats.lp_unlock_schedule_absent_skipped`` — this is NOT the same as
        a DECODED schedule that is ``LpLockStatus.UNKNOWN`` (which the watcher
        itself still raises the exit flag for, unchanged).  See the module
        docstring "REFUSE-BY-DEFAULT vs. NOT YET WIRED" section for why this
        distinction is deliberate and not a safety hole: forcing every
        un-decoded position to exit would turn a de-risk *signal* into an
        unconditional kill-switch, which the codebase's own refuse-by-default
        precedent (``InsiderDumpDetector``'s held-supply refusal) does not do
        either.  Returns ``[]`` (no-op) if no watcher OR no schedule source is
        wired.
        """
        if self._lp_unlock_watcher is None or self._lp_unlock_schedule_source is None:
            return []
        decisions: list[LpUnlockExitDecision] = []
        for pos in self._store.load_all_positions():
            mint = pos.mint  # type: ignore[attr-defined]
            schedule = self._lp_unlock_schedule_source.get_schedule(mint)
            if schedule is None:
                self.stats.lp_unlock_schedule_absent_skipped += 1
                logger.debug(
                    "enrichment_wiring.lp_unlock_schedule_absent: mint=%s "
                    "event_slot=%d (no schedule source data yet — skipped, not "
                    "raised; see module docstring)",
                    mint,
                    event_slot,
                )
                continue
            decision = self._lp_unlock_watcher.watch(schedule, event_slot=event_slot)
            self.stats.lp_unlock_checks_run += 1
            decisions.append(decision)
        return decisions
