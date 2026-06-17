"""T-321 — Layer 2: in-process FAST-loop survivable-stop ENFORCER.

The steady-state primary stop layer (ADR-0008 Layer 2, <=50ms p99).  It is the
deterministic decision the FAST loop runs every tick:

    for each open position with a registered StopState:
        if stop.is_breached(point_in_time_mark_price):
            fire an ExitIntent through the ExecutionVenue seam (de-risk only)

NON-WAIVABLE on this path:
  * NO LLM await.  NO RPC await on the DECISION.  The breach decision is a pure
    comparison (StopState.is_breached) over a point-in-time mark already on the
    tick.  The only IO is the exit handoff AFTER the decision, via the
    ExecutionVenue seam (which `solana-execution-engineer` owns and which is
    behind the DRY_RUN gate).  The decision never blocks on the venue.
  * Point-in-time: the mark price carries its event-time block_time_ms; the
    decision uses that price, never wall-clock and never a future tick.
  * Money is integer lamports / Decimal.  NEVER float.
  * De-risk only: the enforcer can ONLY emit ExitIntent (full flatten of the
    breached position).  It holds the DeRiskIntentFactory — it has no
    EntryIntent constructor, so it is structurally incapable of adding risk.

This is "secondary" in the ADR-0008 sense: it fires the stop INDEPENDENTLY of
the main OMS/snipe path, so a stall in the OMS does not stall the stop.  It runs
on the FAST loop driven by `agent-orchestration-engineer` (T-342); this module
provides the deterministic mechanism it drives.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol, runtime_checkable

from aats.contracts.intents import DeRiskIntentFactory, ExitIntent
from aats.contracts.venue import ExecutionVenue, FillResult
from aats.risk.survivable_stop import StopState

# Full flatten of the breached position (de-risk).  Hard-stop exits are
# fraction 100% — we are exiting the whole thing on a breach.
_FULL_EXIT_BPS = 10_000


@dataclass(frozen=True)
class MarkTick:
    """A point-in-time mark for one mint, stamped in EVENT-TIME (C-5).

    `mark_price` is Decimal SOL-lamports per token base unit (money-derived
    exact; never float).  `block_time_ms` is the authoritative event-time clock
    — the enforcer uses the price as-of this event-time, never wall-clock.
    """

    mint: str
    mark_price: Decimal
    block_time_ms: int  # event-time (UTC ms) — AUTHORITATIVE, never wall-clock

    def __post_init__(self) -> None:
        if isinstance(self.mark_price, float):
            raise TypeError(
                f"MarkTick.mark_price must be Decimal, got float {self.mark_price!r} "
                "(data-models.md §0)."
            )
        if isinstance(self.block_time_ms, float):
            raise TypeError("MarkTick.block_time_ms must be int (event-time ms), not float.")
        if self.block_time_ms < 0:
            raise ValueError("MarkTick.block_time_ms must be non-negative.")


@runtime_checkable
class StopFireTelemetry(Protocol):
    """Optional telemetry sink for stop fires (Prometheus counter in prod)."""

    def on_stop_fired(self, mint: str, reason: str) -> None: ...


@dataclass
class _Registered:
    stop: StopState
    position: object  # opaque Position handle passed back to venue.exit()


class SecondaryStopEnforcer:
    """In-process Layer-2 enforcer.  Deterministic; no LLM/RPC await on decide().

    register()      — arm a fixed StopState for an open position.
    deregister()    — remove a position (closed / handed off).
    tighten()       — TIGHTEN-ONLY update of a registered stop (de-risk).
    on_tick()       — the FAST-loop hot call: decide (pure) then fire exits.

    The factory is a DeRiskIntentFactory: the enforcer can ONLY build ExitIntent
    — it has no path to construct an EntryIntent (structural, ADR-0006).
    """

    def __init__(
        self,
        venue: ExecutionVenue,
        *,
        factory: DeRiskIntentFactory | None = None,
        telemetry: StopFireTelemetry | None = None,
    ) -> None:
        # The loop core imports ONLY the ExecutionVenue ABC, never a concrete
        # class (execution-venue.md invariant 2).
        self._venue = venue
        self._factory = factory or DeRiskIntentFactory()
        self._telemetry = telemetry
        self._stops: dict[str, _Registered] = {}

    # -- registration -------------------------------------------------------

    def register(self, stop: StopState, position: object) -> None:
        """Arm the fixed hard stop for an open position (idempotent per mint)."""
        self._stops[stop.mint] = _Registered(stop=stop, position=position)

    def deregister(self, mint: str) -> None:
        """Remove a position from enforcement (closed or fully handed off)."""
        self._stops.pop(mint, None)

    def tighten(self, mint: str, proposed_trigger: Decimal | int | str) -> StopState:
        """TIGHTEN-ONLY update.  Raises if the proposal would widen the stop.

        This is the only mutation a de-risk signal can apply to an armed stop,
        and StopState.tighten() enforces one-directionality (no widen, ever).
        """
        reg = self._stops.get(mint)
        if reg is None:
            raise KeyError(f"no armed stop registered for mint {mint!r}")
        tightened = reg.stop.tighten(proposed_trigger)
        self._stops[mint] = _Registered(stop=tightened, position=reg.position)
        return tightened

    def armed_mints(self) -> frozenset[str]:
        return frozenset(self._stops.keys())

    def stop_for(self, mint: str) -> StopState | None:
        reg = self._stops.get(mint)
        return reg.stop if reg is not None else None

    # -- the FAST-loop hot path --------------------------------------------

    def decide(self, tick: MarkTick) -> bool:
        """PURE breach decision for a single tick.  No IO, no LLM, no wall-clock.

        Returns True iff a stop is armed for this mint AND the point-in-time mark
        breaches its fixed trigger.  Separated from on_tick() so the decision is
        unit-testable as a pure function and so the FAST loop can measure the
        decision latency in isolation from the (async, venue-side) exit.
        """
        reg = self._stops.get(tick.mint)
        if reg is None:
            return False
        return reg.stop.is_breached(tick.mark_price)

    def on_tick(self, tick: MarkTick) -> FillResult | None:
        """FAST-loop call: decide (pure), and on breach fire the exit (de-risk).

        Order is intentional: the breach DECISION happens first and is pure; only
        if it fires do we touch the venue.  The venue handoff is the only IO and
        it is behind the DRY_RUN gate.  Returns the FillResult of the exit, or
        None if no stop fired.

        On a breach we DEREGISTER the mint after handing it to the venue so the
        same stop does not re-fire on the next tick (idempotency on the enforcer
        side; the venue exit is itself idempotent on client_intent_id).
        """
        if not self.decide(tick):
            return None

        reg = self._stops[tick.mint]
        # Deterministic client_intent_id for idempotent replay (FR-024): mint +
        # event-time of the breach tick.  A replay of the same breach produces
        # the same id, so the exit cannot be double-submitted.
        client_intent_id = f"stop_exit:{tick.mint}:{tick.block_time_ms}"
        intent: ExitIntent = self._factory.exit(
            client_intent_id=client_intent_id,
            mint=tick.mint,
            fraction_bps=_FULL_EXIT_BPS,
            exit_mode="secure",  # private routing on a stop (sandwich-safe, OQ-008)
            reason=(
                f"hard_stop_breach: mark<=trigger ({tick.mark_price}<={reg.stop.trigger_price})"
            ),
        )
        fill = self._venue.exit(intent, reg.position)
        if self._telemetry is not None:
            self._telemetry.on_stop_fired(tick.mint, intent.reason)
        # Hand-off complete on the enforcer side; stop enforcement for this mint
        # is now the venue/exit path's job.  Remove from the armed set so we do
        # not re-fire.  (If the exit did NOT land, the breaker/DMS layers and the
        # venue-native resting order remain as backstops — Layer-2 is not the
        # only defense, by design.)
        self._stops.pop(tick.mint, None)
        return fill
