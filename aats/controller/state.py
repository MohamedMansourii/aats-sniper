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
  insider_dump:{mint}  -> "1" (+ fraction_sold_bps/event_slot payload) if M1
                          ingestion (aats.ingestion.insider_dump, E14a) detected
                          creator/dev-wallet or tracked top-holder-cluster
                          distribution crossing the configured cumulative-sold
                          threshold on an OPEN position.  Mirrors narrative:{mint}
                          exactly (same TTL-based presence semantics); the FAST
                          exit branch (E14b) reads ONLY the bare bool via
                          get_insider_dump_flag() — never the payload.
  sellability_degraded:{mint}
                       -> "1" (+ reason/event_slot payload) if the SLOW-loop
                          sellability RE-PROBE (aats.risk.sellability_reprobe, E17)
                          re-ran the SHARED sell-sim on an OPEN position and got a
                          DEGRADED (delayed-honeypot / tax-flip) or INCONCLUSIVE
                          (refuse-by-default => degraded) result.  Mirrors
                          insider_dump:{mint} exactly (same TTL-based presence
                          semantics, no new storage pattern, no frozen contract);
                          the FAST exit branch (E17) reads ONLY the bare bool via
                          get_sellability_degraded_flag() — never the payload.
  lp_unlock_approaching:{mint}
                       -> "1" (+ reason/event_slot payload) if the SLOW-loop
                          LP-unlock watcher (aats.risk.lp_unlock_gate.
                          LpUnlockExitWatcher, E19) measured a TIME-LOCKED LP's
                          unlock-slot proximity on an OPEN position and found the
                          unlock APPROACHING (or the schedule undecodable —
                          refuse-by-default) within the configured exit horizon.
                          Mirrors sellability_degraded:{mint} exactly (same
                          TTL-based presence semantics, no new storage pattern,
                          no frozen contract); the FAST exit branch (E19) reads
                          ONLY the bare bool via get_lp_unlock_approaching_flag()
                          — never the payload.

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

    # --- Insider-dump flag (E14a: M1 ingestion writes, FAST reads bool) ---
    #
    # Mirrors the narrative_failure_flag mechanism EXACTLY (same TTL-based
    # presence semantics — no new frozen contract, no new storage pattern).
    # Detection lives OFF the hot path in aats.ingestion.insider_dump
    # (enrichment tier); this StateStore method is the ONLY place the
    # detection result is persisted.  The FAST exit branch (E14b,
    # risk-guardrails-engineer) reads get_insider_dump_flag() — a bare bool,
    # zero computation — exactly like get_narrative_failure_flag().

    def get_insider_dump_flag(self, mint: str) -> bool:
        """Return True iff an insider-dump flag is currently set (unexpired).

        PRE-SET boolean read for the FAST exit branch — same TTL-expiry
        semantics as get_narrative_failure_flag().  De-risk only: reading
        True can only cause a caller to de-risk; there is no path back to
        False except natural TTL expiry (mirrors narrative_failure — no
        explicit clear method either).
        """
        ...

    def set_insider_dump_flag(
        self,
        mint: str,
        *,
        fraction_sold_bps: int,
        event_slot: int,
        ttl_seconds: int = 120,
    ) -> None:
        """M1 (aats.ingestion.insider_dump.InsiderDumpDetector) sets the flag.

        Carries the detection PAYLOAD (fraction_sold_bps, event_slot) alongside
        the boolean presence flag so SLOW-loop / audit / dashboard consumers can
        read WHY it fired via get_insider_dump_detail().  The FAST exit branch
        NEVER reads the payload directly — only the bare bool.

        fraction_sold_bps: cumulative sold fraction of the wallet's/cluster's
            held-supply baseline, in bps [0, 10000], AT THE MOMENT the
            threshold was crossed (event-time — never wall-clock).
        event_slot: the on-chain slot of the sell that crossed the threshold
            (T-300a: event-time only).
        """
        ...

    def get_insider_dump_detail(self, mint: str) -> tuple[int, int] | None:
        """Return (fraction_sold_bps, event_slot) if the flag is set (unexpired).

        None if absent/expired.  SLOW-loop / audit / dashboard use ONLY — the
        FAST exit branch must never call this (its return type is not a bare
        bool and reading it must never enter the hot path).
        """
        ...

    # --- Sellability-degraded flag (E17: SLOW re-probe writes, FAST reads bool) ---
    #
    # Mirrors the insider_dump_flag mechanism EXACTLY (same TTL-based presence
    # semantics — no new frozen contract, no new storage pattern).  The delayed-
    # honeypot / tax-flip DETECTION lives OFF the hot path in
    # aats.risk.sellability_reprobe (SLOW / N+2 tier), which RE-RUNS the SHARED
    # sell-sim (aats.execution.sell_sim) on open positions; this StateStore method
    # is the ONLY place that re-probe result is persisted.  The FAST exit branch
    # (E17) reads get_sellability_degraded_flag() — a bare bool, zero computation,
    # no RPC, no sell-sim — exactly like get_insider_dump_flag().  De-risk only:
    # reading True can only cause a caller to de-risk (force-exit SECURE); there
    # is no path back to False except natural TTL expiry.

    def get_sellability_degraded_flag(self, mint: str) -> bool:
        """Return True iff a sellability-degraded flag is currently set (unexpired).

        PRE-SET boolean read for the FAST exit branch — same TTL-expiry semantics
        as get_insider_dump_flag().  De-risk only: reading True can only cause a
        caller to force-exit; there is no explicit clear (mirrors insider_dump).
        """
        ...

    def set_sellability_degraded_flag(
        self,
        mint: str,
        *,
        reason: str,
        event_slot: int,
        ttl_seconds: int = 120,
    ) -> None:
        """The SLOW re-probe (aats.risk.sellability_reprobe.SellabilityReprober)
        sets the flag when the shared sell-sim re-probe on an OPEN position is
        DEGRADED or INCONCLUSIVE (refuse-by-default => degraded).

        Carries the machine `reason` (a SellabilityReprobeReason value, e.g.
        "degraded_sim_revert") and the on-chain `event_slot` (T-300a: event-time
        only — never wall-clock) alongside the boolean presence flag so SLOW-loop /
        audit / dashboard consumers can read WHY it fired via
        get_sellability_degraded_detail().  The FAST exit branch NEVER reads the
        payload directly — only the bare bool.
        """
        ...

    def get_sellability_degraded_detail(self, mint: str) -> tuple[str, int] | None:
        """Return (reason, event_slot) if the flag is set (unexpired), else None.

        SLOW-loop / audit / dashboard use ONLY — the FAST exit branch must never
        call this (its return type is not a bare bool).
        """
        ...

    # --- LP-unlock-approaching flag (E19: SLOW watcher writes, FAST reads bool) ---
    #
    # Mirrors the sellability_degraded_flag mechanism EXACTLY (same TTL-based
    # presence semantics — no new frozen contract, no new storage pattern).  The
    # TIME-LOCKED LP unlock-proximity DETECTION lives OFF the hot path in
    # aats.risk.lp_unlock_gate.LpUnlockExitWatcher (SLOW / N+2 tier), which
    # measures the unlock-slot proximity in EVENT-TIME slots on open positions;
    # this StateStore method is the ONLY place that watcher's result is
    # persisted.  The FAST exit branch (E19) reads get_lp_unlock_approaching_flag()
    # — a bare bool, zero computation, no RPC, no schedule decode — exactly like
    # get_sellability_degraded_flag().  De-risk only: reading True can only cause
    # a caller to force-exit; there is no explicit clear (mirrors the flags above).

    def get_lp_unlock_approaching_flag(self, mint: str) -> bool:
        """Return True iff an lp_unlock_approaching flag is currently set (unexpired).

        PRE-SET boolean read for the FAST exit branch — same TTL-expiry semantics
        as get_sellability_degraded_flag().  De-risk only: reading True can only
        cause a caller to force-exit; there is no explicit clear (mirrors
        insider_dump / sellability_degraded — natural TTL expiry only).
        """
        ...

    def set_lp_unlock_approaching_flag(
        self,
        mint: str,
        *,
        reason: str,
        event_slot: int,
        ttl_seconds: int = 120,
    ) -> None:
        """The SLOW-loop LP-unlock watcher (aats.risk.lp_unlock_gate.
        LpUnlockExitWatcher) sets the flag when a TIME-LOCKED LP's unlock slot
        APPROACHES the current tick slot (or the schedule is undecodable —
        refuse-by-default).

        Carries the machine `reason` (an LpUnlockExitReason value, e.g.
        "lp_unlock_approaching" / "lp_unlock_schedule_unknown") and the on-chain
        `event_slot` (T-300a: event-time only — never wall-clock) alongside the
        boolean presence flag so SLOW-loop / audit / dashboard consumers can read
        WHY it fired via get_lp_unlock_approaching_detail().  The FAST exit
        branch NEVER reads the payload directly — only the bare bool.
        """
        ...

    def get_lp_unlock_approaching_detail(self, mint: str) -> tuple[str, int] | None:
        """Return (reason, event_slot) if the flag is set (unexpired), else None.

        SLOW-loop / audit / dashboard use ONLY — the FAST exit branch must never
        call this (its return type is not a bare bool).
        """
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
        # {mint: (fraction_sold_bps, event_slot, expiry_ms)}  (E14a)
        self._insider_dump_flags: dict[str, tuple[int, int, int]] = {}
        # {mint: (reason, event_slot, expiry_ms)}  (E17 sellability re-probe)
        self._sellability_degraded_flags: dict[str, tuple[str, int, int]] = {}
        # {mint: (reason, event_slot, expiry_ms)}  (E19 LP-unlock watcher)
        self._lp_unlock_approaching_flags: dict[str, tuple[str, int, int]] = {}
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

    # --- Insider-dump flag (E14a) ---

    def get_insider_dump_flag(self, mint: str) -> bool:
        with self._lock:
            entry = self._insider_dump_flags.get(mint)
            if entry is None:
                return False
            _frac, _slot, expiry = entry
            if self._is_expired(expiry):
                del self._insider_dump_flags[mint]
                return False
            return True

    def set_insider_dump_flag(
        self,
        mint: str,
        *,
        fraction_sold_bps: int,
        event_slot: int,
        ttl_seconds: int = 120,
    ) -> None:
        if isinstance(fraction_sold_bps, bool) or not isinstance(fraction_sold_bps, int):
            raise TypeError(
                "set_insider_dump_flag: fraction_sold_bps must be int (bps), not "
                f"{type(fraction_sold_bps).__name__}."
            )
        if not (0 <= fraction_sold_bps <= 10_000):
            raise ValueError(
                f"set_insider_dump_flag: fraction_sold_bps must be in [0, 10000], "
                f"got {fraction_sold_bps}."
            )
        if isinstance(event_slot, bool) or not isinstance(event_slot, int):
            raise TypeError(
                "set_insider_dump_flag: event_slot must be int (on-chain slot), not "
                f"{type(event_slot).__name__}."
            )
        if event_slot < 0:
            raise ValueError(f"set_insider_dump_flag: event_slot must be >= 0, got {event_slot}.")
        with self._lock:
            expiry = self._now() + ttl_seconds * 1_000
            self._insider_dump_flags[mint] = (fraction_sold_bps, event_slot, expiry)

    def get_insider_dump_detail(self, mint: str) -> tuple[int, int] | None:
        with self._lock:
            entry = self._insider_dump_flags.get(mint)
            if entry is None:
                return None
            frac, slot, expiry = entry
            if self._is_expired(expiry):
                del self._insider_dump_flags[mint]
                return None
            return (frac, slot)

    # --- Sellability-degraded flag (E17) ---

    def get_sellability_degraded_flag(self, mint: str) -> bool:
        with self._lock:
            entry = self._sellability_degraded_flags.get(mint)
            if entry is None:
                return False
            _reason, _slot, expiry = entry
            if self._is_expired(expiry):
                del self._sellability_degraded_flags[mint]
                return False
            return True

    def set_sellability_degraded_flag(
        self,
        mint: str,
        *,
        reason: str,
        event_slot: int,
        ttl_seconds: int = 120,
    ) -> None:
        if not isinstance(reason, str) or not reason:
            raise ValueError(
                "set_sellability_degraded_flag: reason must be a non-empty str "
                "(a machine SellabilityReprobeReason value), got "
                f"{type(reason).__name__}={reason!r}."
            )
        if isinstance(event_slot, bool) or not isinstance(event_slot, int):
            raise TypeError(
                "set_sellability_degraded_flag: event_slot must be int (on-chain slot), not "
                f"{type(event_slot).__name__}."
            )
        if event_slot < 0:
            raise ValueError(
                f"set_sellability_degraded_flag: event_slot must be >= 0, got {event_slot}."
            )
        with self._lock:
            expiry = self._now() + ttl_seconds * 1_000
            self._sellability_degraded_flags[mint] = (reason, event_slot, expiry)

    def get_sellability_degraded_detail(self, mint: str) -> tuple[str, int] | None:
        with self._lock:
            entry = self._sellability_degraded_flags.get(mint)
            if entry is None:
                return None
            reason, slot, expiry = entry
            if self._is_expired(expiry):
                del self._sellability_degraded_flags[mint]
                return None
            return (reason, slot)

    # --- LP-unlock-approaching flag (E19) ---

    def get_lp_unlock_approaching_flag(self, mint: str) -> bool:
        with self._lock:
            entry = self._lp_unlock_approaching_flags.get(mint)
            if entry is None:
                return False
            _reason, _slot, expiry = entry
            if self._is_expired(expiry):
                del self._lp_unlock_approaching_flags[mint]
                return False
            return True

    def set_lp_unlock_approaching_flag(
        self,
        mint: str,
        *,
        reason: str,
        event_slot: int,
        ttl_seconds: int = 120,
    ) -> None:
        if not isinstance(reason, str) or not reason:
            raise ValueError(
                "set_lp_unlock_approaching_flag: reason must be a non-empty str "
                "(a machine LpUnlockExitReason value), got "
                f"{type(reason).__name__}={reason!r}."
            )
        if isinstance(event_slot, bool) or not isinstance(event_slot, int):
            raise TypeError(
                "set_lp_unlock_approaching_flag: event_slot must be int (on-chain slot), "
                f"not {type(event_slot).__name__}."
            )
        if event_slot < 0:
            raise ValueError(
                f"set_lp_unlock_approaching_flag: event_slot must be >= 0, got {event_slot}."
            )
        with self._lock:
            expiry = self._now() + ttl_seconds * 1_000
            self._lp_unlock_approaching_flags[mint] = (reason, event_slot, expiry)

    def get_lp_unlock_approaching_detail(self, mint: str) -> tuple[str, int] | None:
        with self._lock:
            entry = self._lp_unlock_approaching_flags.get(mint)
            if entry is None:
                return None
            reason, slot, expiry = entry
            if self._is_expired(expiry):
                del self._lp_unlock_approaching_flags[mint]
                return None
            return (reason, slot)

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
