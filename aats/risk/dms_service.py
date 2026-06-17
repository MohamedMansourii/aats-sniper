"""T-322 — the `aats-dms` SEPARATE-FAILURE-DOMAIN service (backs the breaker).

This is NOT the in-process `DeadMansSwitch` primitive (that is `deadman.py`,
T-321 Layer-3 inside the FAST loop's process).  This is the standalone watchdog
SERVICE that runs as its OWN compose unit (`aats-dms`) in its OWN failure domain
(infrastructure.md §8, ADR-0008 Layer-3).  Its whole reason to exist is to fire
when the FAST-loop process is FULLY DEAD or the host partitions — so it cannot
share memory, a thread, or a process with the loop it watches.

WHAT MAKES THIS A SEPARATE FAILURE DOMAIN
-----------------------------------------
  * The FAST loop (a DIFFERENT process — `aats-hotcore`) writes a heartbeat into
    a SHARED store (Redis KV in prod; `HeartbeatStore` seam here).  The watchdog
    READS that heartbeat across the process boundary.  Nothing in the loop has to
    run for the watchdog to act — that is the point.
  * The flatten transactions are PRE-SIGNED and HELD by the DMS (refreshed as
    positions open/close, produced via `aats-signer`, ADR-0009).  Because they are
    already signed, the DMS fires even if `aats-signer` is ALSO down at fire time
    — it submits the held signed bytes straight to the block engine.

NON-WAIVABLE (infrastructure.md §8; ADR-0008 §Decision; AC-046)
---------------------------------------------------------------
  * heartbeat age > T_DMS  ->  flatten EVERY open position.  Fail-closed.
  * T_DMS is ENV-CONFIGURABLE (T_DMS_SECONDS, default 60s; OQ-006 /
    RiskConfig.t_dms_seconds) — a config value, NEVER a hardcoded constant.
  * The DMS CANNOT be disarmed by an LLM output, a market event, or a risk
    update.  The ONLY things that keep it from firing are (a) a fresh, valid
    heartbeat in the shared store, or (b) an explicit operator stand-down
    (DmsStandDownToken — a capability the automated/LLM/market path cannot
    construct).  This is enforced BY TYPE (the token), not by comment.
  * Real capital is DISABLED by default: the submit path is behind the DRY_RUN
    gate.  In DRY_RUN the pre-signed flatten is built/held but NOT transmitted.
  * Money is integer lamports / Decimal.  NEVER float.

CLOCK NOTE (liveness vs point-in-time)
--------------------------------------
PnL / breaker / stop-breach math uses EVENT-TIME (C-5).  The DMS is the
deliberate, documented exception: it is a LIVENESS watchdog, so it measures the
AGE of the last heartbeat against a MONOTONIC clock.  There is no market data and
no PnL in the staleness measurement — only "how long since the loop last proved
it is alive".  A monotonic source (not wall-clock) is used so an NTP step cannot
spuriously fire or suppress the switch.  Because the heartbeat is written by a
DIFFERENT process, the shared clock cannot be `time.monotonic()` (monotonic
clocks are per-process and not comparable across processes); the heartbeat
carries the writer's wall-clock epoch-ms, and the watchdog measures age as
`now_epoch_ms - heartbeat.epoch_ms`.  To defend against an NTP step on EITHER
host, the watchdog ALSO tracks a per-watchdog monotonic age since it last
OBSERVED a FRESH heartbeat (advancing `seq`), and fires on the LARGER (more
conservative) of the two implied ages — so neither a stalled wall-clock nor a
stuck heartbeat can hide a dead loop.  See `DmsWatchdog._age_locked`.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from aats.risk.deadman import (
    DmsStandDownToken,
    EmergencyFlattenVenue,
)

# ---------------------------------------------------------------------------
# The shared heartbeat carried across the process boundary.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Heartbeat:
    """One heartbeat the FAST loop wrote into the shared store.

    `epoch_ms` is the writer's wall-clock at the moment it beat (UTC ms).  It is a
    LIVENESS timestamp, NOT an event-time / point-in-time market timestamp — using
    wall-clock here is correct and is NOT a compute-time leak (there is no market
    data or PnL in a liveness check).  `seq` is a monotonically increasing counter
    the writer bumps every beat, used to detect a STUCK heartbeat (same value
    re-read) independently of the clock.
    """

    epoch_ms: int
    seq: int

    def __post_init__(self) -> None:
        if isinstance(self.epoch_ms, float):
            raise TypeError("Heartbeat.epoch_ms must be int (epoch ms), not float.")
        if isinstance(self.seq, float):
            raise TypeError("Heartbeat.seq must be int, not float.")
        if self.epoch_ms < 0:
            raise ValueError("Heartbeat.epoch_ms must be non-negative.")
        if self.seq < 0:
            raise ValueError("Heartbeat.seq must be non-negative.")


@runtime_checkable
class HeartbeatWriter(Protocol):
    """The WRITE side of the cross-process heartbeat boundary.

    The FAST-loop process (aats-hotcore) calls write() every tick to prove
    liveness.  In prod this is satisfied by RedisHeartbeatWriter; in tests by
    InMemoryHeartbeatStore (which implements both sides).

    Declared separately from HeartbeatStore (the read-only watchdog view) so
    that FastLoopEnforcerWiring is typed against exactly the capability it uses
    (write-only) and mypy --strict proves the contract without suppression.
    A type that satisfies both Protocols (e.g. InMemoryHeartbeatStore) can be
    used wherever either is required.
    """

    def write(self, hb: Heartbeat) -> None: ...


@runtime_checkable
class HeartbeatStore(Protocol):
    """The SHARED store the FAST loop beats into and the watchdog reads.

    In prod this is Redis KV (a single key, e.g. `aats:dms:heartbeat`), so the
    `aats-hotcore` process writes it and the `aats-dms` process reads it across a
    real process / failure-domain boundary.  `read()` returns None when no
    heartbeat has ever been written, or when the store is unreachable — BOTH of
    which the watchdog treats as "not alive" (fail-closed): an unreachable
    heartbeat store is itself a liveness failure.
    """

    def read(self) -> Heartbeat | None: ...


class InMemoryHeartbeatStore:
    """Reference store for tests / sim, standing in for Redis KV.

    The KEY property under test: the watchdog reads through this seam and NEVER
    holds a reference to the writer's in-process state — so 'killing' the writer
    (ceasing to call write()) is indistinguishable from a dead process to the
    watchdog, exactly as Redis would be.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._hb: Heartbeat | None = None
        self._fail: bool = False

    def write(self, hb: Heartbeat) -> None:
        """Called by the (separate) FAST-loop process to prove liveness."""
        with self._lock:
            self._hb = hb

    def set_unreachable(self, unreachable: bool) -> None:
        """Test hook: simulate the store being unreachable (fail-closed path)."""
        with self._lock:
            self._fail = unreachable

    def read(self) -> Heartbeat | None:
        with self._lock:
            if self._fail:
                return None
            return self._hb


# ---------------------------------------------------------------------------
# Telemetry sink (Prometheus gauge in prod; no-op if absent).
# ---------------------------------------------------------------------------


@runtime_checkable
class DmsServiceTelemetry(Protocol):
    def set_heartbeat_age_seconds(self, age_seconds: float) -> None: ...
    def on_dms_fired(self, age_seconds: float, reason: str) -> None: ...


def _default_wall_epoch_ms() -> int:
    import time

    return int(time.time() * 1000)


def _default_monotonic() -> float:
    import time

    return time.monotonic()


# ---------------------------------------------------------------------------
# The watchdog — the heart of the separate-failure-domain service.
# ---------------------------------------------------------------------------


class DmsWatchdog:
    """Separate-failure-domain heartbeat watchdog backing the breaker.

    The FAST loop (a DIFFERENT process) writes `Heartbeat`s into the shared
    `HeartbeatStore`.  This watchdog, in its own process, calls `check()` on its
    own cadence.  When the heartbeat age exceeds T_DMS (or the heartbeat is
    missing / stuck / the store is unreachable), it submits the PRE-SIGNED
    emergency flatten exactly once and LATCHES FIRED until an explicit operator
    stand-down + re-arm.

    Liveness is measured two independent ways and the watchdog fires on EITHER
    crossing T_DMS (defense against an NTP step on either host):
      1. cross-process wall age   = now_epoch_ms - heartbeat.epoch_ms     (ms)
      2. observed monotonic age   = monotonic_now - monotonic_at_last_fresh_beat
    `_seq` deduplicates: a STUCK heartbeat (same seq re-read) does NOT refresh the
    observed-age anchor, so a frozen-but-present heartbeat still ages out.
    """

    def __init__(
        self,
        venue: EmergencyFlattenVenue,
        store: HeartbeatStore,
        *,
        t_dms_seconds: float,
        wall_epoch_ms: Callable[[], int] = _default_wall_epoch_ms,
        monotonic: Callable[[], float] = _default_monotonic,
        telemetry: DmsServiceTelemetry | None = None,
    ) -> None:
        if t_dms_seconds <= 0:
            raise ValueError("t_dms_seconds must be > 0 (ADR-0008 / OQ-006).")
        self._venue = venue
        self._store = store
        self._t_dms = float(t_dms_seconds)
        self._wall_epoch_ms = wall_epoch_ms
        self._monotonic = monotonic
        self._telemetry = telemetry
        self._lock = threading.RLock()

        self._fired: bool = False
        self._armed: bool = True
        # Anchor for the observed (monotonic) liveness age.  Seed to "now" so a
        # freshly-armed watchdog does not fire before the loop's first beat lands.
        self._last_fresh_mono: float = self._monotonic()
        self._last_seq: int | None = None

    # -- the separate failure domain watches ------------------------------

    @property
    def t_dms_seconds(self) -> float:
        return self._t_dms

    def is_fired(self) -> bool:
        with self._lock:
            return self._fired

    def is_armed(self) -> bool:
        with self._lock:
            return self._armed

    def heartbeat_age_seconds(self) -> float:
        """Best-known liveness age in seconds (the LARGER of the two measures).

        A missing / unreachable heartbeat reports an age of +inf (fail-closed):
        no heartbeat at all is the worst liveness state, never 'fresh'.
        """
        with self._lock:
            return self._age_locked()

    def _age_locked(self) -> float:
        hb = self._store.read()
        mono_now = self._monotonic()
        if hb is None:
            # No heartbeat (never written, or store unreachable).  Fail-closed:
            # the observed-age anchor still ages from the last FRESH beat, so a
            # store that goes dark ages out exactly like a dead loop.
            return mono_now - self._last_fresh_mono
        # 1) cross-process wall age.
        wall_age_s = max(0.0, (self._wall_epoch_ms() - hb.epoch_ms) / 1000.0)
        # 2) observed monotonic age — refreshed ONLY when seq advances (a stuck
        #    heartbeat with the same seq does NOT count as proof of life).
        if self._last_seq is None or hb.seq > self._last_seq:
            self._last_seq = hb.seq
            self._last_fresh_mono = mono_now
        observed_age_s = mono_now - self._last_fresh_mono
        # Fire on the LARGER implied age: if EITHER measure says we are past
        # T_DMS, we are not satisfied of liveness.
        return max(wall_age_s, observed_age_s)

    def check(self) -> bool:
        """Watchdog tick.  Fire the pre-signed flatten iff liveness age > T_DMS.

        Returns True iff this call fired (the ARMED->FIRED edge).  Fires EXACTLY
        ONCE then latches FIRED: a partition that recovers must still be
        operator-reviewed, so a late heartbeat does NOT un-fire.  FAIL-CLOSED: the
        latch is set BEFORE the venue submit, so a throwing/stalling flatten can
        never leave the watchdog un-latched (a retry or restart re-attempts the
        flatten and never silently leaves the book open).
        """
        with self._lock:
            age = self._age_locked()
            if self._telemetry is not None:
                self._telemetry.set_heartbeat_age_seconds(age)
            if not self._armed or self._fired:
                return False
            if age <= self._t_dms:
                return False
            # Heartbeat lost beyond T_DMS — FIRE.  Latch BEFORE the submit.
            self._fired = True
            reason = (
                f"aats_dms_service: heartbeat_age={age:.3f}s > "
                f"T_DMS={self._t_dms:.3f}s — flattening all open positions"
            )
            if self._telemetry is not None:
                self._telemetry.on_dms_fired(age, reason)
            # Submit the PRE-SIGNED flatten for every open position (DRY-RUN gated
            # in the venue).  The DMS holds the signed bytes, so this fires even if
            # aats-signer is also down (infrastructure.md §8).
            self._venue.emergency_flatten(reason=reason)
            return True

    # -- operator-only controls (LLM / market / risk path CANNOT reach these) --

    def stand_down(self, token: DmsStandDownToken) -> None:
        """Operator-only: disarm the watchdog (e.g. planned maintenance).

        Requires a genuine DmsStandDownToken — a capability NO automated, LLM,
        market, or risk-signal caller can mint (it has no reference to the minting
        function).  This is the type-level embodiment of "the DMS cannot be
        disarmed by an LLM/market/risk update" (AC-046).  Disarming does NOT
        un-fire a flatten that already happened.
        """
        if not isinstance(token, DmsStandDownToken):
            raise PermissionError(
                "stand_down() requires a genuine DmsStandDownToken minted by the "
                "authenticated operator path (AC-046).  Automated/LLM/market disarm "
                "is forbidden — the dead-man's switch is non-disarmable by signal."
            )
        with self._lock:
            self._armed = False

    def rearm(self, token: DmsStandDownToken) -> None:
        """Operator-only: re-arm after a stand-down (resets the liveness anchor)."""
        if not isinstance(token, DmsStandDownToken):
            raise PermissionError("rearm() requires a genuine DmsStandDownToken (AC-046).")
        with self._lock:
            self._armed = True
            self._fired = False
            self._last_fresh_mono = self._monotonic()
            self._last_seq = None


# ---------------------------------------------------------------------------
# The breaker linkage — the DMS BACKS the breaker.
# ---------------------------------------------------------------------------


@runtime_checkable
class BreakerView(Protocol):
    """Read-only view of the circuit breaker the DMS service consults.

    The DMS BACKS the breaker (TASKBOARD T-322 title): when the breaker is
    TRIPPED, open positions have been handed to the stops/flatten, but the DMS
    remains the backstop that holds when the PROCESS itself is gone.  The service
    reads `is_tripped()` for telemetry/observability ONLY — a TRIPPED breaker
    NEVER suppresses the DMS (a tripped breaker with a dead loop is precisely when
    the DMS must still fire).  The DMS has NO method to reset or widen the breaker
    — it can only de-risk.
    """

    def is_tripped(self) -> bool: ...


def build_heartbeat(
    *, seq: int, wall_epoch_ms: Callable[[], int] = _default_wall_epoch_ms
) -> Heartbeat:
    """Build the next heartbeat the FAST-loop side writes into the shared store.

    Kept here so the writer (separate `aats-hotcore` process) and the reader (this
    `aats-dms` process) agree on the exact `Heartbeat` shape.  The FAST loop bumps
    `seq` every beat and calls this each tick; the actual write into the shared
    store is the loop's responsibility (it never touches the watchdog directly —
    that is what keeps the failure domains separate).
    """
    return Heartbeat(epoch_ms=wall_epoch_ms(), seq=seq)
