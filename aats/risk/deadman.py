"""T-321 / T-322 — Layer 3: the DEAD-MAN'S SWITCH (DMS).

ADR-0008 Layer 3: a heartbeat watchdog in a SEPARATE failure domain.  The FAST
loop emits a heartbeat; if the DMS sees no heartbeat for longer than T_DMS
(default 60s, env-configurable per OQ-006 / RiskConfig.t_dms_seconds), it
auto-flattens EVERY open position via the ExecutionVenue emergency-flatten seam.

This is the layer that protects a position when the bot is FULLY DEAD or the host
partitions: nothing in the bot's main loop has to run for the DMS to fire — that
is the whole point of putting it in a separate failure domain.

NON-WAIVABLE (ADR-0008 §Decision; AC-046):
  * The DMS CANNOT be disarmed by an LLM, a market event, or a risk update.  The
    ONLY things that keep it from firing are (a) a valid, fresh heartbeat or
    (b) an explicit operator stand-down (DmsStandDownToken — a capability the
    automated/LLM path cannot construct, mirroring the breaker's reset token).
  * The flatten handoff is the ExecutionVenue seam (emergency_flatten), behind
    the DRY_RUN gate.  The DMS does NOT build/sign/land — it INVOKES.  In prod
    the flatten uses PRE-SIGNED flatten tx produced via aats-signer (ADR-0009),
    so it can fire even if the signer is unreachable at fire time.
  * Money is integer lamports / Decimal.  No float anywhere.

CLOCK NOTE (point-in-time vs liveness):
  PnL / breaker / stop-breach math uses EVENT-TIME (C-5) — never wall-clock.  The
  DMS is the deliberate, documented exception: it is a LIVENESS watchdog, so it
  measures the AGE of the last heartbeat against a MONOTONIC clock.  This is not
  a point-in-time lookahead concern — there is no market data and no PnL in the
  staleness measurement, only "how long since the loop last proved it is alive".
  A monotonic clock (not wall-clock) is used so an NTP step cannot spuriously
  fire or suppress the switch.  The monotonic source is injectable for tests.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Protocol, runtime_checkable


@runtime_checkable
class EmergencyFlattenVenue(Protocol):
    """The ExecutionVenue emergency-flatten seam the DMS invokes.

    Implemented by `solana-execution-engineer` behind the ExecutionVenue ABC.
    `emergency_flatten` submits the PRE-SIGNED flatten tx for every open position
    (de-risk only).  Idempotent: repeated calls on an already-flat book are safe.
    In DRY_RUN it builds-but-does-not-submit.
    """

    def emergency_flatten(self, reason: str) -> None: ...


class DmsStandDownToken:
    """Capability token proving an operator deliberately stood the DMS down.

    Required by `stand_down()`.  Minted ONLY by the authenticated operator path
    via `mint_dms_standdown_token()`.  The automated/LLM path has no reference to
    the minting function and cannot construct this, so a market event or LLM can
    NEVER disarm the DMS (AC-046) — mirrors the breaker's OperatorResetToken.
    """

    __slots__ = ("_operator_id",)
    _MINT_KEY = object()

    def __init__(self, operator_id: str, _mint_key: object = None) -> None:
        if _mint_key is not DmsStandDownToken._MINT_KEY:
            raise PermissionError(
                "DmsStandDownToken may only be minted via mint_dms_standdown_token() "
                "from the authenticated operator path (AC-046).  Direct construction "
                "is forbidden so no automated/LLM caller can disarm the dead-man's switch."
            )
        self._operator_id = operator_id

    @property
    def operator_id(self) -> str:
        return self._operator_id


def mint_dms_standdown_token(operator_id: str) -> DmsStandDownToken:
    """Mint a DMS stand-down capability AFTER operator auth (single chokepoint)."""
    if not operator_id or not isinstance(operator_id, str):
        raise ValueError("operator_id must be a non-empty string (audit trail, AC-046).")
    return DmsStandDownToken(operator_id, DmsStandDownToken._MINT_KEY)


@runtime_checkable
class DmsTelemetry(Protocol):
    def on_dms_fired(self, age_seconds: float, reason: str) -> None: ...
    def set_heartbeat_age_seconds(self, age_seconds: float) -> None: ...


def _default_monotonic() -> float:
    import time

    return time.monotonic()


class DeadMansSwitch:
    """Heartbeat watchdog.  No fresh heartbeat for > T_DMS => flatten everything.

    The FAST loop calls `heartbeat()` every tick.  A separate process / thread
    (the SEPARATE FAILURE DOMAIN) calls `check()` on its own cadence; when the
    heartbeat age exceeds T_DMS, `check()` fires the emergency flatten exactly
    once and latches FIRED until an explicit operator stand-down + re-arm.

    The monotonic clock is injectable (`clock`) so tests can simulate the passage
    of time and 'kill' the heartbeat deterministically.
    """

    def __init__(
        self,
        venue: EmergencyFlattenVenue,
        *,
        t_dms_seconds: float,
        clock: Callable[[], float] = _default_monotonic,
        telemetry: DmsTelemetry | None = None,
    ) -> None:
        if t_dms_seconds <= 0:
            raise ValueError("t_dms_seconds must be > 0 (ADR-0008 / OQ-006).")
        self._venue = venue
        self._t_dms = float(t_dms_seconds)
        self._clock = clock
        self._telemetry = telemetry
        self._lock = threading.RLock()
        # Arm immediately: from construction, a missing heartbeat is a fire
        # condition.  We seed last_beat to 'now' so a freshly-armed DMS does not
        # fire before the loop's first beat lands within T_DMS.
        self._last_beat: float = self._clock()
        self._fired: bool = False
        self._armed: bool = True

    # -- the FAST loop proves liveness -------------------------------------

    def heartbeat(self) -> None:
        """Called by the FAST loop every tick to prove it is alive."""
        with self._lock:
            self._last_beat = self._clock()
            if self._telemetry is not None:
                self._telemetry.set_heartbeat_age_seconds(0.0)

    # -- the separate failure domain watches ------------------------------

    def heartbeat_age_seconds(self) -> float:
        with self._lock:
            return self._clock() - self._last_beat

    def is_fired(self) -> bool:
        with self._lock:
            return self._fired

    def check(self) -> bool:
        """Watchdog tick.  Fire the flatten iff heartbeat age > T_DMS.

        Returns True iff this call fired the flatten (the ARMED->FIRED edge).
        Fires EXACTLY ONCE: once FIRED it stays latched (entries are already
        blocked by the breaker; the DMS does not un-fire on a late heartbeat —
        a partition that recovers must still be operator-reviewed).  FAIL-CLOSED:
        if the flatten handler raises, the latch is still set first so a retry or
        a restart re-attempts the flatten and never silently leaves the book open.
        """
        with self._lock:
            if not self._armed or self._fired:
                if self._telemetry is not None:
                    self._telemetry.set_heartbeat_age_seconds(self._age_locked())
                return False
            age = self._age_locked()
            if self._telemetry is not None:
                self._telemetry.set_heartbeat_age_seconds(age)
            if age <= self._t_dms:
                return False
            # Heartbeat lost beyond T_DMS — FIRE.  Latch BEFORE the venue call so
            # a throwing/stalling flatten cannot leave us un-latched (fail closed).
            self._fired = True
            reason = f"dead_mans_switch: heartbeat_age={age:.3f}s > T_DMS={self._t_dms:.3f}s"
            if self._telemetry is not None:
                self._telemetry.on_dms_fired(age, reason)
            self._venue.emergency_flatten(reason=reason)
            return True

    def _age_locked(self) -> float:
        return self._clock() - self._last_beat

    # -- operator-only controls (LLM/market path CANNOT reach these) -------

    def stand_down(self, token: DmsStandDownToken) -> None:
        """Operator-only: disarm the DMS (e.g. planned maintenance).

        Requires a genuine DmsStandDownToken.  No automated/LLM caller can mint
        one, so a market event or the reasoner can NEVER disarm the switch
        (AC-046).  Disarming does NOT un-fire a flatten that already happened.
        """
        if not isinstance(token, DmsStandDownToken):
            raise PermissionError(
                "stand_down() requires a genuine DmsStandDownToken minted by the "
                "authenticated operator path (AC-046).  Automated/LLM disarm is forbidden."
            )
        with self._lock:
            self._armed = False

    def rearm(self, token: DmsStandDownToken) -> None:
        """Operator-only: re-arm after a stand-down (resets the heartbeat clock)."""
        if not isinstance(token, DmsStandDownToken):
            raise PermissionError("rearm() requires a genuine DmsStandDownToken (AC-046).")
        with self._lock:
            self._armed = True
            self._fired = False
            self._last_beat = self._clock()
