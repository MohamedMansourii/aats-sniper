"""M3 Controller — shared Redis state layer (injectable for offline tests).

T-340 ownership: this module defines all Redis key conventions from data-models.md
and provides two implementations:
  - InMemoryStateStore: deterministic offline fake; used in ALL tests.
  - RedisStateStore:    real Redis; injectable at runtime.

Key conventions (data-models.md §9):
  pos:{mint}           -> JSON-serialized Position (open only)
  score:{mint}         -> JSON pre-staged DecisionSignal (TTL = slow cadence)
  veto:{mint}          -> "1" if LLM veto is active (TTL = slow cadence)
  cooldown:{mint}      -> "1" if mint is in post-exit cooldown (TTL configurable)
  fsm_lock:{mint}      -> per-mint FSM lock for atomic IDLE->ENTERING CAS
  intent:{id}          -> deduplicated intent record (TTL = intent window)
  heartbeat:fast_loop  -> last FAST-loop heartbeat timestamp (ms, event-time)
  breaker:state        -> JSON-serialized BreakerState
  narrative:{mint}     -> "1" if SLOW-loop narrative_failure flag is set

ATOMIC SNIPE->FAST HANDOFF (ADR-0007):
  The fsm_lock:{mint} key is written atomically (SETNX / NX flag) with the
  ENTERING claim.  Only ONE concurrent snipe on a given mint can set it; all
  others receive fsm_state_conflict and are dropped.  The FAST loop transitions
  the state machine from ENTERING to subsequent states and removes the lock when
  the position closes.

FAIL-CLOSED POSTURE:
  A Redis-down condition is detected by the store; callers treat absence of a
  critical key the same as a risk-off condition (BLUEPRINT §2.1 back-pressure).

MONEY: all values stored as strings; integer lamports or Decimal strings (never float).
"""

from __future__ import annotations

import threading
import time
from typing import Any, Protocol, runtime_checkable

from aats.contracts.models import DecisionSignal
from aats.contracts.positions import Position
from aats.contracts.risk import BreakerState

# ---------------------------------------------------------------------------
# StateStore Protocol — the interface both impls satisfy.
# ---------------------------------------------------------------------------


@runtime_checkable
class StateStore(Protocol):
    """Shared state store interface (Redis in prod; in-memory for tests).

    All methods are SYNCHRONOUS (the FAST loop must not await anything on the
    critical path — BLUEPRINT §2.1).  The real Redis impl uses a connection
    pool with socket-timeout bounded to < 10ms; a timeout raises RedisDown.
    """

    # --- FSM / position ---

    def claim_entering(self, mint: str, intent_id: str, ttl_seconds: int = 30) -> bool:
        """Atomic IDLE->ENTERING claim (the snipe->fast handoff CAS).

        Returns True if the claim was acquired (mint was IDLE/not-present).
        Returns False if the mint already has a claim (ENTERING or OPEN) —
        the caller MUST treat False as fsm_state_conflict and abort the snipe.

        This is the critical section (ADR-0007): exactly ONE concurrent claim
        wins per mint.  A second call while the claim is held returns False.
        """
        ...

    def release_entering(self, mint: str) -> None:
        """Release the per-mint FSM lock (called by FAST on ENTERING->OPEN|VETOED)."""
        ...

    def save_position(self, position: Position) -> None:
        """Write-ahead: persist FSM state BEFORE emitting side effect."""
        ...

    def load_position(self, mint: str) -> Position | None:
        """Load a position by mint; None if not found."""
        ...

    def delete_position(self, mint: str) -> None:
        """Remove position from live store (called on CLOSED/VETOED)."""
        ...

    def load_all_positions(self) -> list[Position]:
        """Load all currently open positions (used by reconciler on startup)."""
        ...

    # --- Pre-staged score (SNIPE reads; SLOW writes) ---

    def get_decision_signal(self, mint: str) -> DecisionSignal | None:
        """Read pre-staged classifier score+veto; None if absent/stale."""
        ...

    def set_decision_signal(self, signal: DecisionSignal, ttl_seconds: int = 30) -> None:
        """SLOW loop writes the pre-staged score. TTL = SLOW cadence."""
        ...

    # --- Veto flag (LLM de-risk, SLOW writes, FAST reads) ---

    def get_veto_flag(self, mint: str) -> bool:
        """Return True if an LLM/reasoning veto is active for this mint."""
        ...

    def set_veto_flag(self, mint: str, ttl_seconds: int = 60) -> None:
        """SLOW loop sets a veto flag; it self-expires after TTL."""
        ...

    def clear_veto_flag(self, mint: str) -> None:
        """Explicit clear (e.g. after position closes)."""
        ...

    # --- Narrative failure flag (SLOW writes, FAST reads) ---

    def get_narrative_failure_flag(self, mint: str) -> bool:
        """Return True if SLOW loop has flagged a narrative failure for this mint."""
        ...

    def set_narrative_failure_flag(self, mint: str, ttl_seconds: int = 120) -> None:
        """SLOW loop sets the catastrophic-exit flag."""
        ...

    # --- Per-mint cooldown ---

    def set_cooldown(self, mint: str, ttl_seconds: int) -> None:
        """Set a post-exit cooldown for a mint (no re-entry until TTL expires)."""
        ...

    def in_cooldown(self, mint: str) -> bool:
        """Return True if the mint is in a post-exit cooldown window."""
        ...

    # --- Intent deduplication ---

    def mark_intent_seen(self, intent_id: str, ttl_seconds: int = 120) -> bool:
        """Idempotent dedup: returns True if this is the FIRST time we see intent_id.

        Returns False if we have already processed this intent_id (duplicate/retry).
        This is an atomic SETNX equivalent — exactly one caller wins per intent_id.
        """
        ...

    # --- Breaker state ---

    def load_breaker_state(self) -> BreakerState | None:
        """Load persisted BreakerState; None on first boot."""
        ...

    def save_breaker_state(self, state: BreakerState) -> None:
        """Persist BreakerState (write-ahead before acting on a trip)."""
        ...

    # --- FAST-loop heartbeat (DMS monitors this) ---

    def emit_heartbeat(self, block_time_ms: int) -> None:
        """FAST loop calls this each tick to keep the DMS watchdog satisfied."""
        ...

    def get_heartbeat_age_ms(self) -> int:
        """Return wall-clock age (ms) since the last heartbeat; 2^31-1 if never set."""
        ...

    # --- Aggregate open exposure (for blast-radius cap) ---

    def get_aggregate_open_lamports(self) -> int:
        """Sum of sol_in_lamports across all open positions."""
        ...


# ---------------------------------------------------------------------------
# InMemoryStateStore — deterministic offline fake for tests
# ---------------------------------------------------------------------------


class InMemoryStateStore:
    """Thread-safe in-memory StateStore for tests and paper-run.

    All methods are synchronous and blocking (no async).
    Redis key semantics are reproduced exactly:
      - claim_entering: SETNX semantics (atomic in a single-process test via RLock)
      - TTLs: tracked as wall-clock expiry (test can freeze time by injecting a clock)
    """

    def __init__(self, clock: Any = None) -> None:
        """
        Args:
            clock: optional callable returning current wall-clock ms.
                   Defaults to time.monotonic_ns // 1_000_000.
                   Inject a fake clock for deterministic TTL tests.
        """
        self._clock = clock or (lambda: time.monotonic_ns() // 1_000_000)
        self._lock = threading.RLock()

        # {mint: (intent_id, expiry_ms)}
        self._fsm_locks: dict[str, tuple[str, int]] = {}
        # {mint: Position}
        self._positions: dict[str, Position] = {}
        # {mint: (DecisionSignal, expiry_ms)}
        self._signals: dict[str, tuple[DecisionSignal, int]] = {}
        # {mint: expiry_ms}
        self._veto_flags: dict[str, int] = {}
        # {mint: expiry_ms}
        self._narrative_flags: dict[str, int] = {}
        # {mint: expiry_ms}
        self._cooldowns: dict[str, int] = {}
        # {intent_id: expiry_ms}
        self._seen_intents: dict[str, int] = {}
        # breaker state
        self._breaker: BreakerState | None = None
        # heartbeat
        self._last_heartbeat_ms: int | None = None
        self._last_heartbeat_wall_ms: int | None = None

    def _now(self) -> int:
        return self._clock()

    def _is_expired(self, expiry_ms: int) -> bool:
        return self._now() >= expiry_ms

    # --- FSM / position ---

    def claim_entering(self, mint: str, intent_id: str, ttl_seconds: int = 30) -> bool:
        """Atomic SETNX: returns True if claim acquired, False if already held."""
        with self._lock:
            now = self._now()
            # Purge expired lock first (crash-recovery: stale ENTERING re-enterable)
            if mint in self._fsm_locks:
                _, expiry = self._fsm_locks[mint]
                if self._is_expired(expiry):
                    del self._fsm_locks[mint]
            if mint in self._fsm_locks:
                return False  # fsm_state_conflict
            expiry_ms = now + ttl_seconds * 1_000
            self._fsm_locks[mint] = (intent_id, expiry_ms)
            return True

    def release_entering(self, mint: str) -> None:
        with self._lock:
            self._fsm_locks.pop(mint, None)

    def save_position(self, position: Position) -> None:
        with self._lock:
            self._positions[position.mint] = position

    def load_position(self, mint: str) -> Position | None:
        with self._lock:
            return self._positions.get(mint)

    def delete_position(self, mint: str) -> None:
        with self._lock:
            self._positions.pop(mint, None)

    def load_all_positions(self) -> list[Position]:
        with self._lock:
            return list(self._positions.values())

    # --- Pre-staged score ---

    def get_decision_signal(self, mint: str) -> DecisionSignal | None:
        with self._lock:
            entry = self._signals.get(mint)
            if entry is None:
                return None
            signal, expiry = entry
            if self._is_expired(expiry):
                del self._signals[mint]
                return None
            return signal

    def set_decision_signal(self, signal: DecisionSignal, ttl_seconds: int = 30) -> None:
        with self._lock:
            expiry = self._now() + ttl_seconds * 1_000
            self._signals[signal.mint] = (signal, expiry)

    # --- Veto flag ---

    def get_veto_flag(self, mint: str) -> bool:
        with self._lock:
            expiry = self._veto_flags.get(mint)
            if expiry is None:
                return False
            if self._is_expired(expiry):
                del self._veto_flags[mint]
                return False
            return True

    def set_veto_flag(self, mint: str, ttl_seconds: int = 60) -> None:
        with self._lock:
            self._veto_flags[mint] = self._now() + ttl_seconds * 1_000

    def clear_veto_flag(self, mint: str) -> None:
        with self._lock:
            self._veto_flags.pop(mint, None)

    # --- Narrative failure flag ---

    def get_narrative_failure_flag(self, mint: str) -> bool:
        with self._lock:
            expiry = self._narrative_flags.get(mint)
            if expiry is None:
                return False
            if self._is_expired(expiry):
                del self._narrative_flags[mint]
                return False
            return True

    def set_narrative_failure_flag(self, mint: str, ttl_seconds: int = 120) -> None:
        with self._lock:
            self._narrative_flags[mint] = self._now() + ttl_seconds * 1_000

    # --- Per-mint cooldown ---

    def set_cooldown(self, mint: str, ttl_seconds: int) -> None:
        with self._lock:
            self._cooldowns[mint] = self._now() + ttl_seconds * 1_000

    def in_cooldown(self, mint: str) -> bool:
        with self._lock:
            expiry = self._cooldowns.get(mint)
            if expiry is None:
                return False
            if self._is_expired(expiry):
                del self._cooldowns[mint]
                return False
            return True

    # --- Intent deduplication ---

    def mark_intent_seen(self, intent_id: str, ttl_seconds: int = 120) -> bool:
        with self._lock:
            entry = self._seen_intents.get(intent_id)
            if entry is not None and not self._is_expired(entry):
                return False  # already seen
            expiry = self._now() + ttl_seconds * 1_000
            self._seen_intents[intent_id] = expiry
            return True  # first time

    # --- Breaker state ---

    def load_breaker_state(self) -> BreakerState | None:
        with self._lock:
            return self._breaker

    def save_breaker_state(self, state: BreakerState) -> None:
        with self._lock:
            self._breaker = state

    # --- FAST-loop heartbeat ---

    def emit_heartbeat(self, block_time_ms: int) -> None:
        with self._lock:
            self._last_heartbeat_ms = block_time_ms
            self._last_heartbeat_wall_ms = self._now()

    def get_heartbeat_age_ms(self) -> int:
        with self._lock:
            if self._last_heartbeat_wall_ms is None:
                return 2**31 - 1
            return self._now() - self._last_heartbeat_wall_ms

    # --- Aggregate open exposure ---

    def get_aggregate_open_lamports(self) -> int:
        with self._lock:
            return sum(p.sol_in_lamports for p in self._positions.values())

    # --- Test helpers ---

    def get_fsm_lock(self, mint: str) -> str | None:
        """Test helper: return the intent_id held in the per-mint lock, or None."""
        with self._lock:
            entry = self._fsm_locks.get(mint)
            if entry is None:
                return None
            intent_id, expiry = entry
            if self._is_expired(expiry):
                return None
            return intent_id
