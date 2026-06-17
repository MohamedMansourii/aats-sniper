"""T-342 — FAST-loop Layer-2 survivable-stop ENFORCER + DMS heartbeat wiring.

ADR-0008, BLUEPRINT §9, FR-033, AC-026, AC-027.

This module wires all three survivable-stop layers into the FAST loop:

  Layer 1  venue-native resting order (VenueNativeStopPlacer) — armed at entry
           by the coordinator; fires off-box independent of this process.
  Layer 2  in-process FAST enforcer (SecondaryStopEnforcer) — this file runs it
           deterministically on every FAST tick (<=50ms p99, no LLM/RPC).
  Layer 3  dead-man's switch (separate failure domain) — this file emits the
           heartbeat into the HeartbeatStore; the aats-dms process reads it.

CRITICAL FAST-PATH INVARIANTS (non-waivable, BLUEPRINT §2.1):
  * NO `await`.  This code runs SYNCHRONOUSLY on the FAST loop tick.
  * NO LLM call, NO slow-model call, NO unbounded RPC.
  * The breach DECISION (is_breached) is a pure function; only the VENUE EXIT
    that follows involves IO, and that IO is bounded (DRY_RUN gated).
  * If the enforcer cannot reach the venue on a breach it still DEREGISTERS the
    mint (fail-closed: the position is handed to Layer 1 and Layer 3).
  * Heartbeat emission is a local write (HeartbeatStore is Redis or in-memory);
    a store-down condition is caught and logged, never raised — the FAST loop
    must not crash on a heartbeat write failure.

MONEY: integer lamports / Decimal.  NEVER float.
TIME:  block_time_ms (event-time, on-chain).  NEVER wall-clock for decisions.
       The DMS heartbeat is the documented wall-clock exception (liveness only).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from decimal import Decimal
from typing import Protocol, runtime_checkable

from aats.contracts.venue import FillResult
from aats.risk.dms_service import Heartbeat, HeartbeatWriter
from aats.risk.secondary_enforcer import MarkTick, SecondaryStopEnforcer
from aats.risk.survivable_stop import StopState, StopThresholds

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Telemetry sink (Prometheus in prod; no-op for tests)
# ---------------------------------------------------------------------------


@runtime_checkable
class EnforcerTelemetry(Protocol):
    """Telemetry sink the enforcer wiring emits into."""

    def on_stop_fired(self, mint: str, reason: str) -> None: ...

    def on_heartbeat_emitted(self, seq: int, age_ms: int) -> None: ...

    def on_heartbeat_store_error(self, error: str) -> None: ...


# ---------------------------------------------------------------------------
# FastLoopEnforcerWiring
# ---------------------------------------------------------------------------


class FastLoopEnforcerWiring:
    """Wires Layer-2 stop enforcement + DMS heartbeat into the FAST loop tick.

    Called once per tick from the FAST loop (or from the
    `SurvivableStopCoordinator`-aware extension of it).  It:

      1. Emits a heartbeat into the HeartbeatStore (cross-process; the aats-dms
         watchdog reads this from the other side of the process boundary).
      2. For every registered mint, runs the pure breach decision then fires an
         ExitIntent through the ExecutionVenue seam on breach.

    The HeartbeatStore write is the ONLY external IO here.  If it fails (Redis
    down) we log and continue — a missed heartbeat ages out T_DMS seconds later
    and Layer 3 fires, which is the correct fail-closed behaviour.  We do NOT
    raise and crash the FAST loop on a store error.

    PROOF NO-LLM (for self-check §5):
      The only method calls on the critical path are:
        self._hb_store.read/write      — synchronous KV op (bounded)
        enforcer.on_tick(tick)         — pure breach check + venue.exit() on hit
      There is no import of any LLM module, no `await`, no call to anything in
      aats.reasoning / aats.slow / aats.nlp.  The LLM verdict is a PRE-STAGED
      boolean in the StateStore (written by the SLOW loop); the enforcer never
      reads it — it compares mark vs trigger, period.
    """

    def __init__(
        self,
        enforcer: SecondaryStopEnforcer,
        hb_store: HeartbeatWriter,
        *,
        telemetry: EnforcerTelemetry | None = None,
        wall_epoch_ms: Callable[[], int] | None = None,
    ) -> None:
        """
        Args:
            enforcer:    the Layer-2 SecondaryStopEnforcer (shared singleton).
            hb_store:    the HeartbeatWriter the DMS watchdog reads.
                         In prod: a RedisHeartbeatWriter (writes aats:dms:heartbeat).
                         In tests: InMemoryHeartbeatStore (satisfies both
                         HeartbeatWriter and HeartbeatStore — offline, no network).
            telemetry:   optional Prometheus sink.
            wall_epoch_ms: injectable wall-clock for the heartbeat (liveness only,
                           not a point-in-time market clock).  Defaults to
                           time.time()*1000 as int.
        """
        self._enforcer = enforcer
        self._hb_store = hb_store
        self._telemetry = telemetry
        self._wall_epoch_ms = wall_epoch_ms or _default_wall_epoch_ms
        self._seq: int = 0
        # None until the FIRST successful write so we do not emit a garbage
        # inter-beat age of ~epoch-ms on the very first heartbeat (M1 fix).
        self._last_hb_wall_ms: int | None = None

    # ------------------------------------------------------------------
    # The FAST-loop hot call (synchronous, no await, no LLM)
    # ------------------------------------------------------------------

    def on_tick(
        self,
        mints_and_prices: list[tuple[str, Decimal, int]],
    ) -> list[FillResult]:
        """Run Layer-2 enforcement for all open positions this tick.

        Args:
            mints_and_prices: list of (mint, mark_price_decimal, block_time_ms).
                              The block_time_ms is the EVENT-TIME authoritative
                              clock — NEVER wall-clock (C-5, point-in-time).

        Returns:
            list of FillResult for any positions that had stops fire this tick
            (empty list is the normal case).

        CRITICAL PATH PROOF:
          * No `await`.
          * No import or call to any LLM, reasoning, or slow-model module.
          * No unbounded network IO (heartbeat write is a bounded KV op; on
            failure it is caught and logged, NEVER raises to the caller).
          * Breach decision = pure Decimal comparison (StopState.is_breached).
          * Venue.exit() is the only external call and it is post-decision,
            bounded by the venue's timeout (DRY_RUN is immediate).
        """
        # --- 1. Emit heartbeat (Layer-3 liveness proof) -----------------
        self._emit_heartbeat_safe()

        # --- 2. Run Layer-2 breach check for each position ---------------
        fired: list[FillResult] = []
        for mint, mark_price, block_time_ms in mints_and_prices:
            tick = MarkTick(
                mint=mint,
                mark_price=mark_price,
                block_time_ms=block_time_ms,
            )
            fill = self._enforcer.on_tick(tick)
            if fill is not None:
                fired.append(fill)
                log.warning(
                    "enforcer_wiring.stop_fired: mint=%s block_time_ms=%d mark=%s",
                    mint,
                    block_time_ms,
                    mark_price,
                )
                if self._telemetry is not None:
                    self._telemetry.on_stop_fired(
                        mint,
                        f"hard_stop_breach at block_time_ms={block_time_ms}",
                    )

        return fired

    # ------------------------------------------------------------------
    # Delegation to coordinator / enforcer
    # ------------------------------------------------------------------

    def arm(
        self,
        mint: str,
        entry_fill_price: Decimal | int | str,
        position: object,
        thresholds: StopThresholds | None = None,
    ) -> StopState:
        """Arm the Layer-2 stop for a freshly-filled position.

        Called by the FAST loop's fill-reconciliation path (after ENTERING→OPEN).
        Returns the armed StopState for audit / telemetry.
        """
        stop = StopState.at_entry(
            mint=mint,
            entry_fill_price=entry_fill_price,
            thresholds=thresholds,
        )
        self._enforcer.register(stop, position)
        log.info(
            "enforcer_wiring.armed: mint=%s trigger=%s",
            mint,
            stop.trigger_price,
        )
        return stop

    def deregister(self, mint: str) -> None:
        """Deregister a closed position from Layer-2 enforcement."""
        self._enforcer.deregister(mint)

    def tighten(
        self, mint: str, proposed_trigger: Decimal | int | str
    ) -> StopState:
        """TIGHTEN-ONLY: raise the stop trigger for a mint (de-risk).

        Delegates to SecondaryStopEnforcer.tighten() which enforces
        one-directionality (StopState.tighten() raises on any widen).
        """
        return self._enforcer.tighten(mint, proposed_trigger)

    def armed_mints(self) -> frozenset[str]:
        """Return the set of mints currently armed in the Layer-2 enforcer."""
        return self._enforcer.armed_mints()

    def stop_for(self, mint: str) -> StopState | None:
        return self._enforcer.stop_for(mint)

    # ------------------------------------------------------------------
    # Internal: heartbeat emission (fail-safe, never raises to caller)
    # ------------------------------------------------------------------

    def _emit_heartbeat_safe(self) -> None:
        """Write one heartbeat into the shared store (fail-closed, never raises).

        The DMS watchdog in the SEPARATE failure domain (aats-dms process) reads
        this.  If the store is down, we log the error and continue — a missed
        heartbeat ages out after T_DMS and Layer 3 fires, which is the correct
        fail-closed behaviour.  We NEVER crash the FAST loop on a heartbeat
        write failure.

        Metric semantic: `age_ms` reported to telemetry is the gap between the
        LAST SUCCESSFUL WRITE and the current attempt, measured in wall-clock ms.
        It measures successful write-to-write latency, NOT attempt-to-attempt
        latency.  After a store outage the next successful write will report the
        full outage duration — which is the correct inter-success gap and a useful
        signal for alert tuning.  The first ever beat emits no age sample
        (seq == 1, no prior successful write to measure from).
        """
        self._seq += 1
        now_ms = int(self._wall_epoch_ms())
        hb = Heartbeat(epoch_ms=now_ms, seq=self._seq)
        try:
            self._hb_store.write(hb)
            if self._telemetry is not None and self._last_hb_wall_ms is not None:
                # Only emit the inter-beat age once a prior successful write exists.
                # On seq==1 (first ever beat) there is no prior anchor, so we skip
                # the age sample rather than reporting a garbage value of ~epoch-ms.
                age_ms = now_ms - self._last_hb_wall_ms
                self._telemetry.on_heartbeat_emitted(self._seq, age_ms)
            elif self._telemetry is not None:
                # First beat: emit with age_ms=0 as a sentinel so the counter
                # increments (the gauge value is meaningless until seq >= 2).
                self._telemetry.on_heartbeat_emitted(self._seq, 0)
            self._last_hb_wall_ms = now_ms
        except Exception as exc:  # noqa: BLE001 — store down is non-fatal
            err_str = f"heartbeat_store_write_error: {exc}"
            log.error("enforcer_wiring.%s", err_str)
            if self._telemetry is not None:
                self._telemetry.on_heartbeat_store_error(err_str)

    @property
    def heartbeat_seq(self) -> int:
        """Current heartbeat sequence number (monotonically increasing)."""
        return self._seq


# ---------------------------------------------------------------------------
# Default wall-clock (injectable for tests)
# ---------------------------------------------------------------------------


def _default_wall_epoch_ms() -> int:
    return int(time.time() * 1000)


# ---------------------------------------------------------------------------
# RedisHeartbeatWriter — the FAST-loop side of the cross-process boundary
# (the DMS side uses RedisHeartbeatStore from dms_main.py to READ this key)
# ---------------------------------------------------------------------------


class RedisHeartbeatWriter:
    """Writes the FAST-loop heartbeat to Redis (the shared KV the DMS reads).

    This is the writer side of the `aats:dms:heartbeat` key.
    The aats-dms service uses `RedisHeartbeatStore` (from dms_main.py) to READ
    the same key from its own process.  The two sides are decoupled by the key
    — no shared memory, no shared thread — which is what makes the DMS a
    separate failure domain.

    The wire format (`epoch_ms:seq`) matches `RedisHeartbeatStore.serialize()`.
    """

    def __init__(
        self,
        redis_client: object,
        key: str = "aats:dms:heartbeat",
        ttl_seconds: int = 120,
    ) -> None:
        """
        Args:
            redis_client: a redis.Redis client (injected; not imported here so
                          the module loads without a redis install in tests).
            key:          the shared Redis key (must match DMS_HEARTBEAT_KEY env).
            ttl_seconds:  key TTL; a client that dies without writing the key
                          will see the key expire and the DMS will treat absence
                          as a liveness failure (fail-closed).
        """
        self._redis = redis_client
        self._key = key
        self._ttl = ttl_seconds

    def write(self, hb: Heartbeat) -> None:
        """Write the heartbeat to Redis (the DMS reads this across the boundary)."""
        value = f"{hb.epoch_ms}:{hb.seq}"
        self._redis.set(self._key, value, ex=self._ttl)  # type: ignore[attr-defined]
