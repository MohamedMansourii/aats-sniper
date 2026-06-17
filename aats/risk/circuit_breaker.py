"""T-320 — Daily-loss circuit breaker / global kill switch (HARD HALT).

The single most important code in this system.  It is built and proven FIRST,
before any live-capable execution path exists (TASKBOARD §3 SAFETY-FIRST).

WHAT IT DOES
------------
Tracks realized + unrealized PnL in EVENT-TIME against a per-day risk tranche.
It TRIPS (HARD halt) when daily net PnL crosses EITHER:
  (a) a percentage of the day's tranche   (daily_loss_limit_pct, default -3.0%); OR
  (b) the hard absolute floor              (daily_loss_floor_lamports, -0.30 SOL).
whichever is breached first.  On trip it:

SOFT TIER (AUDIT-risktiers) — a REDUCE/PAUSE band STRICTLY BELOW the hard trip:
  When the day's net crosses the SOFT limit (daily_loss_limit_pct × DEFAULT_SOFT_RATIO,
  default -2.0% of tranche = -0.010 SOL on the 0.5 SOL tranche), the breaker enters a
  REDUCED posture: entries_allowed() returns False (new entries PAUSE) WITHOUT a
  hard flatten or latch.  It fires BEFORE the hard -3.0% / -0.30 SOL trip, is a
  pure DE-RISK early warning, is MONOTONE within a day (a same-day recovery never
  lifts the pause — lifting would re-increase risk), survives a restart, and is
  lifted ONLY by the same operator reset() that clears a hard trip.  It can ONLY
  reduce risk; it never widens, never increases exposure, never blocks the hard
  trip from firing.

  OWNED IN THIS RISK MODULE — the soft ratio is a module constant (DEFAULT_SOFT_RATIO,
  optionally overridden per-breaker) and the soft posture is tracked in-process and
  RE-DERIVED on restart from the already-persisted day net.  No RiskConfig or
  BreakerState contract field is added: the contracts package is NOT modified.

On a HARD trip it:
  1. STOPS all new entries (entries_allowed() → False).
  2. Hands open positions to the survivable stop / flatten (FlattenHandler).
  3. LATCHES (state=TRIPPED, persisted) — it does NOT auto-reset.
  4. Re-arm requires an explicit MANUAL operator action (reset()), gated on
     operator auth.  No automated path, no LLM, no market event may reset it.
  5. Emits breaker_tripped telemetry (Prometheus gauge → Telegram alert ≤10s).

ASYMMETRIC TRUST (BUILD-DIRECTIVE / AUTONOMY-DIRECTIVE non-waivable #4; AC-029)
------------------------------------------------------------------------------
An LLM / de-risk signal may TRIP the breaker (de-risk) but may NEVER reset it.
This is enforced BY TYPE, not by comment:
  - trip_from_llm() accepts an LLMDeRiskSignal and can only de-risk.
  - reset() requires an OperatorResetToken — a value the LLM path cannot
    construct (it has no reference to its constructor) — so the reset capability
    is structurally unreachable from any automated/LLM caller.

POINT-IN-TIME CORRECTNESS (C-5; AUTONOMY-DIRECTIVE non-waivable #5)
------------------------------------------------------------------
All PnL / tranche / day-boundary math uses EVENT-TIME (the on-chain
block_time_ms carried on every PnLEvent), never compute/wall-clock time.  A
breaker that "would have" tripped using future-arriving data is a backtest lie;
the same code runs live and in backtest.  The day key is derived from
event_time.block_time_ms (UTC), so the tranche resets at UTC midnight in
event-time and a replayed event lands in exactly the day it occurred.

MONEY (data-models.md §0; AUTONOMY-DIRECTIVE non-waivable #5)
------------------------------------------------------------
Every monetary quantity is integer lamports or Decimal.  NEVER float.

RESTART SAFETY (AC-029)
-----------------------
On process restart the breaker re-reads BreakerState from the store.  A TRIPPED
breaker comes back TRIPPED — it NEVER auto-resets on restart.

DRY-RUN (AUTONOMY-DIRECTIVE non-waivable #1)
--------------------------------------------
This module performs NO real-capital action itself.  It hands flatten/exit to a
FlattenHandler (the ExecutionVenue seam, T-327), which is itself behind the
DRY_RUN_ENABLED gate.  In paper/dry-run the handler builds-but-does-not-submit.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import ROUND_DOWN, Decimal
from typing import Protocol, runtime_checkable

from aats.contracts.risk import BreakerState, RiskConfig

LAMPORTS_PER_SOL = 1_000_000_000

# SOFT-TIER ratio (AUDIT-risktiers).  The soft REDUCE/PAUSE tier fires at
# `daily_loss_limit_pct × DEFAULT_SOFT_RATIO` of the day tranche — STRICTLY below
# the hard breaker.  At the default hard -3.0% the soft tier is -2.0% (-0.010 SOL
# on the 0.5 SOL tranche).  Owned in this RISK module (not in contracts): no new
# RiskConfig/BreakerState field is introduced.  The ratio is in (0,1) so the soft
# pct is always strictly below the hard pct, for any hard limit.
DEFAULT_SOFT_RATIO = Decimal("0.6667")


# ---------------------------------------------------------------------------
# Event-time PnL event — the ONLY input that moves the breaker's day PnL.
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class PnLEvent:
    """A point-in-time PnL delta in integer lamports, stamped in EVENT-TIME.

    `block_time_ms` is the on-chain block time (UTC ms) — the AUTHORITATIVE
    clock (C-5).  It is what selects the day-tranche and what the breaker
    compares against the loss limits.  `recorded_at_ms` (wall-clock at ingest)
    is carried for audit ONLY and is NEVER used for any join or threshold —
    using it would be a compute-time leak.

    `delta_lamports` is signed: negative = loss, positive = gain.  It may be a
    realized fill PnL or a mark-to-market unrealized revaluation; the breaker
    treats realized+unrealized identically against the day limit (a deep
    unrealized drawdown trips the halt just as a realized loss does).
    """

    mint: str
    block_time_ms: int  # event-time (UTC ms) — AUTHORITATIVE, selects the day & trips the limit
    delta_lamports: int  # signed integer lamports; NEVER float
    kind: str = "realized"  # "realized" | "unrealized" — audit label only
    recorded_at_ms: int | None = None  # compute-time — AUDIT ONLY, never a join/threshold key

    def __post_init__(self) -> None:
        if isinstance(self.delta_lamports, float):
            raise TypeError(
                f"PnLEvent.delta_lamports must be int lamports, got float "
                f"{self.delta_lamports!r} (data-models.md §0)."
            )
        if isinstance(self.block_time_ms, float):
            raise TypeError(
                f"PnLEvent.block_time_ms must be int (event-time ms), got float "
                f"{self.block_time_ms!r}."
            )
        if self.block_time_ms < 0:
            raise ValueError("PnLEvent.block_time_ms must be non-negative.")


# ---------------------------------------------------------------------------
# Asymmetric-trust types.  The LLM path may only DE-RISK; reset is unreachable.
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class LLMDeRiskSignal:
    """A de-risk-only signal the SLOW-loop reasoner can use to TRIP the breaker.

    This is the ONLY breaker capability the LLM / reasoning path is handed.  It
    carries no field and references no method that could reset, widen, or size
    up.  trip_from_llm() consumes it and can ONLY trip (de-risk).  There is no
    LLM-constructible path to reset() — that needs an OperatorResetToken the LLM
    code cannot create (ADR-0006; AC-029).
    """

    reason: str  # quoted untrusted rationale — NEVER executed as an instruction


class OperatorResetToken:
    """Capability token proving a human operator authorized a manual re-arm.

    reset() requires an instance of this.  It is minted ONLY by the
    authenticated control-plane handler for POST /api/breaker/reset after
    operator auth (api-contracts.md §5) via `mint_operator_reset_token()`.

    The LLM / automated path has no reference to the minting function and cannot
    construct this token, so the reset CAPABILITY is structurally unreachable
    from any non-operator caller.  This is the type-level embodiment of
    "the LLM may trip the breaker, never reset it" (AC-029).
    """

    __slots__ = ("_operator_id",)
    # A private sentinel only the minting function knows.  A naive
    # OperatorResetToken() call from arbitrary code raises — the token must be
    # minted through the audited operator path.
    _MINT_KEY = object()

    def __init__(self, operator_id: str, _mint_key: object = None) -> None:
        if _mint_key is not OperatorResetToken._MINT_KEY:
            raise PermissionError(
                "OperatorResetToken may only be minted via mint_operator_reset_token() "
                "from the authenticated operator path (AC-029).  Direct construction is "
                "forbidden so that no automated/LLM caller can forge a reset capability."
            )
        self._operator_id = operator_id

    @property
    def operator_id(self) -> str:
        return self._operator_id


def mint_operator_reset_token(operator_id: str) -> OperatorResetToken:
    """Mint a reset capability AFTER the control-plane has authenticated the operator.

    Call this ONLY from the authenticated POST /api/breaker/reset handler
    (operator auth verified upstream).  It is the single chokepoint that can
    produce a valid OperatorResetToken.  No LLM/automated module imports or calls
    it on the de-risk path.
    """
    if not operator_id or not isinstance(operator_id, str):
        raise ValueError("operator_id must be a non-empty string (audit trail, AC-029).")
    return OperatorResetToken(operator_id, OperatorResetToken._MINT_KEY)


# ---------------------------------------------------------------------------
# Persistence + flatten seams (Protocols — we INVOKE, we do not author RPC/IO).
# ---------------------------------------------------------------------------
@runtime_checkable
class BreakerStore(Protocol):
    """Durable store for BreakerState (Redis KV in prod; data-models.md §8).

    The breaker persists every state transition so a restart re-reads TRIPPED
    and never auto-resets (AC-029).
    """

    def load(self) -> BreakerState | None: ...
    def save(self, state: BreakerState) -> None: ...


@runtime_checkable
class FlattenHandler(Protocol):
    """The de-risk handoff to execution (the ExecutionVenue seam, T-327).

    The breaker INVOKES this to hand open positions to the survivable stop /
    flatten; it does NOT build, sign, or land transactions itself (boundary:
    solana-execution-engineer owns that).  In paper/dry-run the handler
    builds-but-does-not-submit.  It MUST be idempotent — repeating flatten on an
    already-tripped breaker is safe (api-contracts.md §1.5).
    """

    def emergency_flatten_all(self, reason: str) -> None: ...


class InMemoryBreakerStore:
    """Reference in-memory BreakerStore for tests / sim.

    Production uses a Redis-backed store with the SAME interface.  Crucially,
    constructing a fresh CircuitBreaker against a store that already holds a
    TRIPPED state reproduces "process restart" — and the breaker must come back
    TRIPPED (proven by test_restart_stays_tripped).
    """

    def __init__(self) -> None:
        self._state: BreakerState | None = None

    def load(self) -> BreakerState | None:
        return self._state

    def save(self, state: BreakerState) -> None:
        self._state = state


# ---------------------------------------------------------------------------
# Optional telemetry hook (Prometheus gauge in prod; no-op if absent).
# ---------------------------------------------------------------------------
@runtime_checkable
class BreakerTelemetry(Protocol):
    def set_tripped(self, tripped: bool) -> None: ...
    def set_daily_pnl_lamports(self, lamports: int) -> None: ...
    def set_daily_loss_limit_lamports(self, lamports: int) -> None: ...


@dataclass
class _TripDecision:
    tripped: bool
    threshold_breached: str | None = field(default=None)
    # SOFT-TIER (AUDIT-risktiers): True once the day's net crosses the soft -2.0%
    # tier, which sits STRICTLY BELOW the hard breaker.  When the hard tier trips,
    # `soft_breached` is also True (the harder state implies the softer one).  The
    # soft tier is a DE-RISK early warning: it pauses new entries (reduce posture)
    # WITHOUT a hard flatten/latch.
    soft_breached: bool = field(default=False)
    soft_threshold_breached: str | None = field(default=None)


class CircuitBreaker:
    """Daily-loss circuit breaker.  Trips on -X% tranche OR -0.30 SOL floor.

    Thread-safe (a single lock guards the day-PnL accumulator and the latch).
    PnL is accumulated in event-time per UTC day; the tranche resets at UTC
    midnight in event-time, so a new day re-arms the *limit* (but NEVER a
    latched TRIPPED state — only a manual reset() clears the latch).
    """

    def __init__(
        self,
        config: RiskConfig,
        store: BreakerStore,
        flatten_handler: FlattenHandler,
        *,
        telemetry: BreakerTelemetry | None = None,
        soft_ratio: Decimal | str | None = None,
    ) -> None:
        self._config = config
        self._store = store
        self._flatten = flatten_handler
        self._telemetry = telemetry
        self._lock = threading.RLock()

        # SOFT-TIER ratio (AUDIT-risktiers).  The soft REDUCE/PAUSE tier fires at
        # `daily_loss_limit_pct × soft_ratio` — STRICTLY below the hard tier for any
        # hard limit, so the two can never cross and tightening the hard limit auto-
        # tightens the soft one.  Owned here in the RISK module (the contracts
        # package is not modified): no new RiskConfig/BreakerState field is added —
        # the soft posture is tracked in-process and RE-DERIVED on restart from the
        # already-persisted day net.  Ratio must be in (0,1); de-risk only.
        ratio = DEFAULT_SOFT_RATIO if soft_ratio is None else Decimal(str(soft_ratio))
        if not (Decimal("0") < ratio < Decimal("1")):
            raise ValueError(
                f"soft_ratio ({ratio}) must be in (0, 1) — the soft REDUCE/PAUSE tier fires at "
                "hard_pct × ratio and must sit STRICTLY BELOW the hard breaker (AUDIT-risktiers)."
            )
        self._soft_ratio = ratio

        # Per-UTC-day net PnL accumulator, keyed by event-time day string.
        # {"YYYY-MM-DD": net_lamports_int}
        self._day_pnl: dict[str, int] = {}

        # SOFT-TIER (AUDIT-risktiers): the set of event-time UTC days whose net has
        # crossed the soft tier.  Posture is MONOTONE within a day: once a day is in
        # this set it STAYS paused for that day (a same-day partial recovery does NOT
        # lift the pause — lifting would re-increase risk).  A fresh UTC day is absent
        # from the set, so it starts NORMAL (the tranche reset).  Tracked in memory
        # and RE-DERIVED on restart from the persisted day net (below) — no extra
        # persisted field is needed, so the contracts package stays untouched.
        self._soft_reduced_days: set[str] = set()
        # The latest event-time UTC day the breaker has observed.  Tracked SEPARATELY
        # from daily_net_pnl_day_utc (which is None when net==0) so that a day which
        # crossed the soft tier and then recovered to EXACTLY zero net still reads
        # REDUCED (monotone within a day — a recovery must not silently lift the
        # pause).  None until the first event / restart with an established day.
        self._current_day: str | None = None

        # Restore latched state on construction — this is the restart path.
        restored = self._store.load()
        if restored is None:
            # First boot: ARMED, zero PnL, nothing breached, no day established.
            self._state = BreakerState(
                state="ARMED",
                tripped_at_utc=None,
                daily_net_pnl_lamports=0,
                daily_net_pnl_day_utc=None,
                threshold_breached=None,
            )
            self._store.save(self._state)
        else:
            # Restart: re-read whatever was persisted.  TRIPPED stays TRIPPED.
            self._state = restored
            # POINT-IN-TIME RESTART SEEDING (B1 fix; C-5).  The persisted net
            # belongs to a SPECIFIC event-time UTC day (daily_net_pnl_day_utc).
            # Seed the accumulator into THAT day — never into "whichever day the
            # first post-restart event happens to land in".  A first event on a
            # LATER UTC day therefore starts that day at 0 (a fresh tranche),
            # while a same-day event correctly continues from the persisted net.
            #
            # This closes both failure directions the old "carry until first
            # event" logic created:
            #   (a) negative carry spuriously tripping a fresh next day, and
            #   (b) the DANGEROUS one — positive carry MASKING a real next-day
            #       loss that must halt.
            persisted_day = self._state.daily_net_pnl_day_utc
            persisted_net = self._state.daily_net_pnl_lamports
            if persisted_net != 0:
                # Invariant guaranteed by BreakerState: a non-zero net carries a
                # day key.  Defensive check keeps a malformed store fail-closed.
                if persisted_day is None:
                    raise ValueError(
                        "Corrupt BreakerState: non-zero daily_net_pnl_lamports with no "
                        "daily_net_pnl_day_utc (point-in-time restart seeding, B1/ADR-0012)."
                    )
                self._day_pnl[persisted_day] = persisted_net
            # SOFT-TIER restart RE-DERIVATION (no extra persisted field; contracts
            # untouched).  The soft pause must SURVIVE a restart (de-risk must never
            # be silently re-enabled).  We reconstruct it from data already on
            # BreakerState: if the persisted day's net is already at/below the soft
            # threshold (the same event-time math the live path uses), that day was
            # soft-reduced — re-seed it.  A persisted TRIPPED state implies the soft
            # tier was crossed too (the harder state implies the softer), so its day
            # is re-seeded as well.  This is point-in-time correct: it uses the
            # persisted event-time net, never compute-time.
            if persisted_day is not None and (
                self._state.state == "TRIPPED" or self._evaluate(persisted_net).soft_breached
            ):
                self._soft_reduced_days.add(persisted_day)
            # Re-establish the current event-time day so the soft posture survives a
            # restart even after a same-day recovery to zero net (point-in-time).
            self._current_day = persisted_day

        self._emit_telemetry()

    # -- public API ---------------------------------------------------------

    @property
    def state(self) -> BreakerState:
        with self._lock:
            return self._state

    def is_tripped(self) -> bool:
        with self._lock:
            return self._state.state == "TRIPPED"

    def entries_allowed(self) -> bool:
        """FAIL CLOSED: new entries are allowed ONLY when ARMED **and** NORMAL.

        Two independent de-risk gates, BOTH must permit an entry:
          - the hard latch: state must be ARMED (a TRIPPED breaker blocks entries);
          - the SOFT tier:  posture must be NORMAL (a REDUCED/paused posture, set
            when the day's net crosses the soft tier, PAUSES new entries BEFORE the
            hard breaker trips — AUDIT-risktiers).
        This is the read the SNIPE/pre-trade gate consults before any EntryIntent.
        It can only ever REDUCE the set of permitted entries, never widen it.
        """
        with self._lock:
            return self._state.state == "ARMED" and self._posture() == "NORMAL"

    def risk_posture(self) -> str:
        """The current soft-tier posture: 'NORMAL' or 'REDUCED' (AUDIT-risktiers).

        Orthogonal to the hard ARMED/TRIPPED latch.  REDUCED means the soft tier
        paused new entries (a de-risk early warning) — distinct from a hard TRIPPED
        halt, which additionally flattens and latches.  A TRIPPED breaker is always
        at least REDUCED.
        """
        with self._lock:
            return self._posture()

    def is_soft_reduced(self) -> bool:
        """True when the soft reduce/pause tier is active but the hard breaker has
        NOT (yet) tripped — i.e. the early-warning band BELOW the hard halt."""
        with self._lock:
            return self._posture() == "REDUCED" and self._state.state == "ARMED"

    def _posture(self) -> str:
        """Derive the current soft posture from the breaker's OWN state (no contract
        field).  A hard TRIPPED state is always REDUCED (the harder state implies the
        softer).  Otherwise the posture is REDUCED iff the CURRENT event-time day —
        the day the persisted net belongs to — is in the soft-reduced set.  A fresh
        day (absent from the set) is NORMAL.  Caller holds the lock."""
        if self._state.state == "TRIPPED":
            return "REDUCED"
        if self._current_day is not None and self._current_day in self._soft_reduced_days:
            return "REDUCED"
        return "NORMAL"

    def record_pnl(self, event: PnLEvent) -> BreakerState:
        """Apply an event-time PnL delta and trip the breaker if a limit breaks.

        Returns the (possibly updated) BreakerState.  Idempotent on an already-
        TRIPPED breaker for the trip side: a further loss does not "double-trip",
        and entries stay blocked.  The flatten is fired exactly once, on the
        transition ARMED→TRIPPED.
        """
        with self._lock:
            day = self._utc_day_key(event.block_time_ms)
            # Track the latest event-time day (so a same-day recovery to zero net
            # keeps the soft pause — the posture key is the DAY, not the net sign).
            self._current_day = day

            # Accumulate into the event's OWN event-time UTC day.  A day that has
            # not been seen (or was seeded at restart for a DIFFERENT day) starts
            # fresh at 0 — the restart seed lives under its own persisted day key,
            # so it can never bleed into a later day's tranche (B1 fix; C-5).
            self._day_pnl[day] = self._day_pnl.get(day, 0) + int(event.delta_lamports)
            net = self._day_pnl[day]

            decision = self._evaluate(net)

            # SOFT-TIER (AUDIT-risktiers): mark this day soft-reduced the moment its
            # net crosses the soft tier.  MONOTONE within a day — once marked it stays
            # marked for that day (a same-day partial recovery does NOT lift it;
            # lifting would re-increase risk).  This is pure de-risk bookkeeping and
            # happens BEFORE any trip handling so a hard trip's day is marked too.
            if decision.soft_breached:
                self._soft_reduced_days.add(day)

            if decision.tripped and self._state.state == "ARMED":
                # Transition ARMED → TRIPPED.  Latch, persist, flatten, alert.  A hard
                # trip's day is, by the check above, already in the soft-reduced set,
                # so the derived posture is REDUCED (the harder state implies softer).
                self._trip(net, decision.threshold_breached, event.block_time_ms, day)
            else:
                # No hard trip: keep the persisted daily_net (and its day key) in sync
                # for audit/telemetry and correct restart seeding.  The soft posture
                # is DERIVED from _soft_reduced_days + _current_day (no contract field).
                was_reduced_before = self._posture()
                self._state = self._state.model_copy(
                    update={
                        "daily_net_pnl_lamports": net,
                        "daily_net_pnl_day_utc": day if net != 0 else None,
                    }
                )
                self._store.save(self._state)
                if self._posture() == "REDUCED" and was_reduced_before != "REDUCED":
                    # First crossing into the soft band this day → de-risk telemetry.
                    self._emit_soft_reduce(net, decision.soft_threshold_breached)

            self._emit_telemetry()
            return self._state

    def trip_from_llm(self, signal: LLMDeRiskSignal, *, block_time_ms: int) -> BreakerState:
        """The LLM / reasoning path may TRIP the breaker (de-risk) — never reset.

        This is the asymmetric-trust trip path.  It consumes an LLMDeRiskSignal
        (a de-risk-only capability) and forces TRIPPED.  It has NO ability to
        reset, widen, or size up — those need an OperatorResetToken the LLM path
        cannot mint (AC-029).
        """
        with self._lock:
            if self._state.state == "ARMED":
                # Preserve the persisted day key when the current net belongs to
                # the same event-time day; otherwise the day of this LLM-trip
                # block_time_ms governs (the net it carries is whatever was
                # accumulated, day-keyed for correct restart seeding, B1).
                net = self._state.daily_net_pnl_lamports
                day_utc = (
                    self._state.daily_net_pnl_day_utc
                    if net != 0
                    else self._utc_day_key(block_time_ms)
                )
                self._current_day = day_utc
                self._trip(
                    net,
                    f"llm_narrative_failure:{signal.reason[:80]}",
                    block_time_ms,
                    day_utc,
                )
                self._emit_telemetry()
            return self._state

    def reset(self, token: OperatorResetToken) -> BreakerState:
        """Manual re-arm — the ONLY path back to ARMED (AC-029).

        Requires a valid OperatorResetToken (operator auth verified upstream;
        minted only via mint_operator_reset_token()).  Returns 409-equivalent
        behavior by raising if the breaker is not currently TRIPPED
        (api-contracts.md §5: breaker_not_tripped).  Idempotency at the API
        layer is handled by the controller; here a non-TRIPPED reset is a
        conflict the caller must handle.
        """
        if not isinstance(token, OperatorResetToken):
            # Defense in depth: the type signature already requires it, but a
            # dynamically-typed caller (e.g. a forged duck-typed object) is
            # rejected here too.
            raise PermissionError(
                "reset() requires a genuine OperatorResetToken minted by the "
                "authenticated operator path (AC-029).  Automated/LLM reset is forbidden."
            )
        with self._lock:
            if self._state.state != "TRIPPED":
                raise BreakerNotTripped(
                    "breaker_not_tripped: reset is only valid when TRIPPED (api-contracts.md §5)."
                )
            # Re-arm: clear the latch, the day accumulator, AND the soft-reduce
            # band (a manual review has occurred; the operator owns the decision to
            # resume — re-arming to NORMAL is the ONLY path that lifts the soft
            # pause, and it requires the same operator token as the hard reset).
            self._day_pnl.clear()
            self._soft_reduced_days.clear()
            self._current_day = None
            self._state = BreakerState(
                state="ARMED",
                tripped_at_utc=None,
                daily_net_pnl_lamports=0,
                daily_net_pnl_day_utc=None,
                threshold_breached=None,
            )
            self._store.save(self._state)
            self._emit_telemetry()
            return self._state

    # -- internals ----------------------------------------------------------

    def _evaluate(self, net_lamports: int) -> _TripDecision:
        """Pure, event-time decision: does `net_lamports` breach a limit?

        Hard limit (a): percent of the day tranche.
            pct_limit_lamports = -(daily_risk_tranche_lamports * pct / 100)
            (computed in Decimal then floored to int lamports — no float).
        Hard limit (b): the absolute hard floor (-daily_loss_floor_lamports).
        The breaker TRIPS (hard halt) on the TIGHTER (less negative) of the two
        being crossed — i.e. whichever limit the net breaches first.

        SOFT tier (AUDIT-risktiers): the day net crossing the soft -2.0% tier —
        which sits STRICTLY BELOW the hard breaker — sets `soft_breached`.  Because
        the soft pct < the hard pct, the soft tier is ALWAYS crossed before (at a
        smaller loss than) the hard tier; when the hard tier trips, the soft is
        crossed too.  The soft tier is a DE-RISK reduce/pause early warning, NOT a
        hard halt — it never flattens or latches.
        """
        cfg = self._config

        # (a) percentage-of-tranche HARD limit, in lamports, as a negative integer.
        pct = cfg.daily_loss_limit_pct  # Decimal, e.g. 3.0
        tranche = cfg.daily_risk_tranche_lamports  # int lamports
        # -(tranche * pct / 100), exact Decimal then int (truncate toward zero is
        # fine — we compare <=; using int() floors magnitude which is the
        # conservative direction for a negative limit: a slightly less-negative
        # limit trips slightly EARLIER, never later).
        pct_limit_lamports = -int((Decimal(tranche) * pct) / Decimal(100))

        # (b) absolute HARD floor, negative integer lamports.
        floor_limit_lamports = -int(cfg.daily_loss_floor_lamports)

        # SOFT tier: percentage-of-tranche limit, STRICTLY tighter than (a).  Derived
        # from the hard pct × the soft ratio (owned in this module, not in contracts).
        soft_pct = self._soft_limit_pct()  # Decimal, e.g. 2.0 at the default
        soft_limit_lamports = -int((Decimal(tranche) * soft_pct) / Decimal(100))
        crossed_soft = net_lamports <= soft_limit_lamports
        soft_desc = self._fmt_soft() if crossed_soft else None

        breached: str | None = None
        # Whichever HARD threshold the net has crossed.  Report the FIRST (tighter)
        # one breached for the audit string; both are checked.
        crossed_pct = net_lamports <= pct_limit_lamports
        crossed_floor = net_lamports <= floor_limit_lamports

        if crossed_pct or crossed_floor:
            # Choose the description of the limit actually responsible.  The
            # tighter (less negative) limit is the one that fired first.
            if crossed_floor and (not crossed_pct or floor_limit_lamports >= pct_limit_lamports):
                # floor is the tighter/equal one that's breached
                breached = self._fmt_floor()
            else:
                breached = self._fmt_pct()
            # A hard trip ALWAYS implies the soft tier is crossed (soft_pct < pct,
            # and soft_pct% < floor magnitude on the default tranche): the harder
            # state implies the softer.  Assert that invariant defensively.
            assert crossed_soft, (
                "invariant: a hard breaker trip must also cross the soft tier "
                "(soft tier must sit strictly below the hard tier, AUDIT-risktiers)"
            )
            return _TripDecision(
                tripped=True,
                threshold_breached=breached,
                soft_breached=True,
                soft_threshold_breached=soft_desc,
            )
        # No hard trip — but the soft reduce/pause tier may still be crossed.
        return _TripDecision(
            tripped=False,
            soft_breached=crossed_soft,
            soft_threshold_breached=soft_desc,
        )

    def _fmt_pct(self) -> str:
        return f"-{self._config.daily_loss_limit_pct}% tranche"

    def _soft_limit_pct(self) -> Decimal:
        """The SOFT-tier percentage limit = hard pct × soft ratio (AUDIT-risktiers).

        Derived (never a contract field) so it is ALWAYS strictly below the hard
        limit.  Quantized DOWN to 1 dp (ROUND_DOWN, toward zero) so the default
        (3.0 x 0.6667 = 2.0001 -> 2.0) reads cleanly AND the value can only become a
        SMALLER-magnitude limit — the soft tier fires marginally EARLIER, never
        later, and can never round UP to meet the hard tier (de-risk rounding)."""
        exact = self._config.daily_loss_limit_pct * self._soft_ratio
        soft = exact.quantize(Decimal("0.1"), rounding=ROUND_DOWN)
        # Never return a non-positive limit (could happen rounding a tiny hard pct
        # down); fall back to the exact product, which the (0,1) ratio guarantees is
        # strictly between 0 and the hard pct.
        return soft if soft > 0 else exact

    def _fmt_soft(self) -> str:
        return f"-{self._soft_limit_pct()}% tranche (soft)"

    def _fmt_floor(self) -> str:
        sol = Decimal(self._config.daily_loss_floor_lamports) / Decimal(LAMPORTS_PER_SOL)
        return f"-{sol.normalize()} SOL"

    def _trip(
        self,
        net_lamports: int,
        threshold_breached: str | None,
        block_time_ms: int,
        day_utc: str | None,
    ) -> None:
        """Latch TRIPPED, persist, hand positions to flatten, alert.  ARMED→TRIPPED only."""
        tripped_at = self._utc_iso(block_time_ms)
        # A hard trip implies the soft reduce/pause (the harder state implies the
        # softer); mark the day soft-reduced so the DERIVED posture reads REDUCED
        # for this day (consistent across observability/restart).
        if day_utc is not None:
            self._soft_reduced_days.add(day_utc)
        self._state = BreakerState(
            state="TRIPPED",
            tripped_at_utc=tripped_at,
            daily_net_pnl_lamports=net_lamports,
            # The day this net belongs to, so a restart-while-TRIPPED seeds the
            # accumulator into the correct event-time day (point-in-time, B1).
            daily_net_pnl_day_utc=day_utc if net_lamports != 0 else None,
            threshold_breached=threshold_breached or "limit breached",
        )
        # Persist BEFORE flatten so a crash mid-flatten still restarts TRIPPED.
        self._store.save(self._state)
        # Hand open positions to the survivable stop / flatten (idempotent).
        # This is the de-risk handoff; any handler error must NOT un-latch the
        # breaker (fail closed) — we let it propagate after the latch is durable.
        self._flatten.emergency_flatten_all(reason=f"circuit_breaker_tripped:{threshold_breached}")

    def _emit_soft_reduce(self, net_lamports: int, threshold: str | None) -> None:
        """Soft-tier de-risk telemetry hook (best-effort; no-op if unsupported).

        The soft tier is a DE-RISK early warning distinct from the hard trip.  We
        emit it through the same optional telemetry sink if it exposes a
        `set_soft_reduced` method; otherwise we silently skip (the posture is still
        derived and read by entries_allowed())."""
        if self._telemetry is None:
            return
        setter = getattr(self._telemetry, "set_soft_reduced", None)
        if callable(setter):
            setter(True)

    def _emit_telemetry(self) -> None:
        if self._telemetry is None:
            return
        self._telemetry.set_tripped(self._state.state == "TRIPPED")
        self._telemetry.set_daily_pnl_lamports(self._state.daily_net_pnl_lamports)
        self._telemetry.set_daily_loss_limit_lamports(-int(self._config.daily_loss_floor_lamports))
        setter = getattr(self._telemetry, "set_soft_reduced", None)
        if callable(setter):
            setter(self._posture() == "REDUCED")

    @staticmethod
    def _utc_day_key(block_time_ms: int) -> str:
        """UTC day string ('YYYY-MM-DD') from EVENT-TIME block_time_ms.

        This is what makes the tranche reset at UTC midnight in EVENT-TIME, not
        compute-time (C-5).  A replayed historical event lands in the day it
        actually occurred.
        """
        dt = datetime.fromtimestamp(block_time_ms / 1000, tz=UTC)
        return dt.strftime("%Y-%m-%d")

    @staticmethod
    def _utc_iso(block_time_ms: int) -> str:
        dt = datetime.fromtimestamp(block_time_ms / 1000, tz=UTC)
        return dt.isoformat()


class BreakerNotTripped(Exception):
    """Raised when reset() is called on a breaker that is not TRIPPED (409)."""
