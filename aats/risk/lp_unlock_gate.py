"""E19 (risk) — LP-unlock-approaching de-risk awareness for TIME-LOCKED LPs.

A meme-coin LP can be BURNED (tokens sent to a burn address — permanently locked,
never pullable) or merely TIME-LOCKED in a locker that RELEASES at a scheduled
on-chain slot.  A time-locked LP is safe only until its unlock slot arrives: the
instant the lock releases, the deployer can PULL the liquidity and rug.  This
module makes the firm AWARE of that unlock window and de-risks around it — on both
sides of a position:

  (a) AT ENTRY.  ``LpUnlockGate.evaluate(schedule)`` is a pure THRESHOLD on the
      unlock-slot proximity (in EVENT-TIME slots):
        * a time-locked LP whose unlock falls within ``reject_within_slots`` of the
          decision slot (imminent — including an already-released lock) is an instant
          pre-gate REJECT — a ``RedFlagHit(RedFlag.LP_UNLOCK_IMMINENT)`` that
          short-circuits BEFORE the safety gate runs, exactly like the E2 denylist /
          E15 serial-deployer / E16 fresh-funding rejects;
        * a time-locked LP whose unlock falls in the wider
          ``(reject_within_slots, downweight_within_slots]`` band DOWN-WEIGHTS
          conviction — a ``conviction_multiplier`` clamped hard to ``[0, 1)``, the
          same shape as E13 anti-FOMO / E15 / E16;
        * a BURNED LP (no unlock ever) or a time-locked LP whose unlock is beyond
          ``downweight_within_slots`` (far off) is SILENT — ``PASS`` with
          ``conviction_multiplier == 1``.  A burned/far-off LP is NEVER upgraded into
          a buy trigger or a size-up;
        * an UNKNOWN / undecodable schedule (or a time-lock carrying no readable
          unlock slot) is an instant pre-gate REJECT — a DISTINCT
          ``RedFlagHit(RedFlag.LP_UNLOCK_SCHEDULE_UNKNOWN)`` so the dashboard never
          conflates "we proved the unlock is imminent" with "we simply could not
          read the schedule" (refuse-by-default: unknown is NEVER "safe/far-off").

  (b) FOR AN OPEN POSITION.  ``LpUnlockExitWatcher`` (SLOW-loop, OFF the hot path)
      measures the SAME unlock-slot proximity against the CURRENT tick event-slot and,
      when the unlock APPROACHES within ``exit_raise_within_slots`` (or the schedule
      is undecodable — refuse-by-default => raise), PRE-SETS the
      ``lp_unlock_approaching`` flag in the shared StateStore (same mechanism
      ``insider_dump`` / ``sellability_degraded`` use).  The FAST exit branch
      (``exit_engine.ExitEngine.on_tick``) reads ONLY that bare bool and force-exits
      SECURE while an exit is still possible.  A burned or far-off LP raises NO flag.

ASYMMETRIC / DE-RISK-ONLY (E19 hard rule, non-waivable)
---------------------------------------------------------
  There is no code path here that constructs an entry, sizes a trade, widens a stop,
  raises conviction, adds leverage, or resets anything.  ``conviction_multiplier`` is
  STRUCTURALLY clamped to ``[0, 1]`` (``LpUnlockDecision.__post_init__`` raises on any
  attempt to violate this — not an ``assert``, so it survives ``python -O``, matching
  the DEF-E10-01 / E15 / E16 precedent).  The exit watcher can ONLY ever SET the flag
  (a signal that FORCES an exit); it can never clear it, size a trade, or hold longer.

POINT-IN-TIME (T-300a)
-----------------------
  Proximity is ``unlock_slot - event_slot`` — a difference of two on-chain EVENT-TIME
  slots.  ``event_slot`` is the decision slot (entry) or the current tick slot (open
  position); the module NEVER reads a wall-clock and never reaches forward past the
  supplied slot.  The SAME schedule yields a tighter verdict as ``event_slot`` advances
  toward the fixed ``unlock_slot`` — that monotone slot arithmetic is the whole point.

REFUSE-BY-DEFAULT / FAIL-CLOSED
--------------------------------
  UNKNOWN / undecodable => REJECT (entry) and RAISE the exit flag (open position).
  A time-lock carrying no readable unlock slot is treated as UNKNOWN, never as
  far-off/safe.  Malformed schedules (a burned LP carrying an unlock slot, etc.)
  RAISE rather than being silently trusted.

MONEY
------
  ``conviction_multiplier`` is ``Decimal`` (de-risk multiplier).  Slots and horizons
  are ``int`` (event-time slot counts).  NO float anywhere.

SEAM / OWNERSHIP
-----------------
  This module DECODES nothing on-chain — the LP-locker schedule decode is a SLOW-loop
  ingestion concern.  This module consumes a decoded, point-in-time ``schedule`` (any
  object matching ``LpUnlockScheduleLike``) and CLASSIFIES it into a de-risk decision.
  It touches no ``aats/contracts/`` frozen contract: the two new pre-gate ``RedFlag``
  codes are additive (mirroring E15/E16/E18), and the ``lp_unlock_approaching`` flag is
  written via the existing StateStore flag mechanism through a structural sink.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Protocol, runtime_checkable

from aats.risk.pretrade_gate import RedFlag, RedFlagHit

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# LP lock status — the three cases this module distinguishes.
# ---------------------------------------------------------------------------


class LpLockStatus(StrEnum):
    """The decoded LP-lock posture of a candidate/open token.

    BURNED      — LP tokens burned (permanently locked, no unlock schedule).  SAFE
                  from a pull: never flagged by this module.
    TIME_LOCKED — LP tokens held in a locker that releases at ``unlock_slot``.  Safe
                  only until the unlock slot approaches.
    UNKNOWN     — the schedule could not be conclusively decoded.  Refuse-by-default:
                  treated as REJECT (entry) / RAISE (open position), never as safe.
    """

    BURNED = "burned"
    TIME_LOCKED = "time_locked"
    UNKNOWN = "unknown"


def _status_value(status: object) -> str:
    """Coerce a status (an ``LpLockStatus`` or any ``.value``-bearing enum) to str."""
    return str(getattr(status, "value", status))


# ---------------------------------------------------------------------------
# Structural view of the decoded schedule — decoupled by design (no hard import
# dependency from risk/ onto ingestion/; any object with this shape works).
# ---------------------------------------------------------------------------


@runtime_checkable
class LpUnlockScheduleLike(Protocol):
    mint: str
    as_of_event_slot: int  # event-time anchor the schedule was decoded as-of
    status: object  # LpLockStatus — compared via .value
    unlock_slot: int | None  # event-time slot the time-lock releases (TIME_LOCKED)


@dataclass(frozen=True)
class LpUnlockSchedule:
    """A decoded, point-in-time LP-unlock schedule fact.

    ``unlock_slot`` is an on-chain EVENT-TIME slot (the slot the time-lock releases),
    or ``None`` when there is no unlock (BURNED) or it could not be decoded (UNKNOWN,
    or a TIME_LOCKED lock whose release slot is unreadable).  ``as_of_event_slot`` is
    the decision/observation slot the schedule was decoded as-of (used as the entry
    proximity anchor).  NO float; slots are int.
    """

    mint: str
    as_of_event_slot: int
    status: LpLockStatus
    unlock_slot: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.mint, str) or not self.mint:
            raise ValueError("LpUnlockSchedule.mint must be a non-empty str.")
        for name in ("as_of_event_slot",):
            val = getattr(self, name)
            if isinstance(val, bool) or not isinstance(val, int):
                raise TypeError(f"LpUnlockSchedule.{name} must be int (event-time slot).")
            if val < 0:
                raise ValueError(f"LpUnlockSchedule.{name} must be >= 0 (event-time slot).")
        if self.unlock_slot is not None:
            if isinstance(self.unlock_slot, bool) or not isinstance(self.unlock_slot, int):
                raise TypeError(
                    "LpUnlockSchedule.unlock_slot must be int (event-time slot) or None."
                )
            if self.unlock_slot < 0:
                raise ValueError("LpUnlockSchedule.unlock_slot must be >= 0 or None.")
        status = _status_value(self.status)
        # Refuse-by-default consistency: a BURNED or UNKNOWN schedule cannot carry an
        # unlock slot (a burned LP has no unlock; an unknown one has no readable one).
        # A contradictory input is a producer bug — RAISE, never silently trust it.
        if (
            status in (LpLockStatus.BURNED.value, LpLockStatus.UNKNOWN.value)
            and self.unlock_slot is not None
        ):
            raise ValueError(
                f"LpUnlockSchedule: status {status!r} must carry unlock_slot=None "
                f"(a burned/unknown LP has no readable unlock slot); got {self.unlock_slot}."
            )


# ---------------------------------------------------------------------------
# Thresholds — tighten-only de-risk knobs (all in EVENT-TIME slots).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LpUnlockThresholds:
    """Tunable E19 horizons.  Every knob is DE-RISK / SELECTIVITY ONLY.

    All horizons are counts of EVENT-TIME slots (~400ms/slot on Solana, so the
    defaults below are ~1 day reject / ~3 days downweight / ~1 day exit-raise).
    WIDENING a horizon (raising the slot count) only REJECTS / DOWN-WEIGHTS / RAISES
    the exit flag on MORE launches — always safe.  ``downweight_multiplier`` LOWERING
    (more haircut) is likewise always safe; it can never be raised to >= 1.
    """

    # Unlock within this many slots of the decision slot => hard REJECT at entry
    # (imminent / already-released pullable window).  Default ~1 day.
    reject_within_slots: int = 216_000
    # Unlock within this many slots (but beyond reject) => DOWN-WEIGHT at entry.
    # Default ~3 days.
    downweight_within_slots: int = 648_000
    # Conviction MULTIPLIER applied in the DOWN_WEIGHT band, in [0, 1).
    downweight_multiplier: Decimal = Decimal("0.25")
    # For an OPEN position: unlock within this many slots of the current tick slot
    # => RAISE the pre-set exit flag (force-exit SECURE).  Default ~1 day.
    exit_raise_within_slots: int = 216_000

    def __post_init__(self) -> None:
        if isinstance(self.downweight_multiplier, bool) or not isinstance(
            self.downweight_multiplier, Decimal | int | str | float
        ):
            raise TypeError("downweight_multiplier must be Decimal/int/float/str, not bool")
        if not isinstance(self.downweight_multiplier, Decimal):
            object.__setattr__(
                self, "downweight_multiplier", Decimal(str(self.downweight_multiplier))
            )
        for name in (
            "reject_within_slots",
            "downweight_within_slots",
            "exit_raise_within_slots",
        ):
            val = getattr(self, name)
            if not isinstance(val, int) or isinstance(val, bool):
                raise TypeError(f"{name} must be int (event-time slots)")
            if val < 0:
                raise ValueError(f"{name} must be >= 0")
        if self.downweight_within_slots < self.reject_within_slots:
            raise ValueError(
                f"downweight_within_slots ({self.downweight_within_slots}) must be >= "
                f"reject_within_slots ({self.reject_within_slots})"
            )
        if not (Decimal(0) <= self.downweight_multiplier < Decimal(1)):
            raise ValueError("downweight_multiplier must be a fraction in [0, 1) — de-risk only")

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> LpUnlockThresholds:
        """Build thresholds from environment (all optional; defaults if unset).

        A malformed value raises (fail-closed on config).
        """
        import os

        e = env if env is not None else os.environ

        def _int(key: str, default: int) -> int:
            raw = e.get(key)
            return int(raw) if raw is not None and raw != "" else default

        def _dec(key: str, default: Decimal) -> Decimal:
            raw = e.get(key)
            return Decimal(str(raw)) if raw is not None and raw != "" else default

        return cls(
            reject_within_slots=_int("LP_UNLOCK_REJECT_WITHIN_SLOTS", cls.reject_within_slots),
            downweight_within_slots=_int(
                "LP_UNLOCK_DOWNWEIGHT_WITHIN_SLOTS", cls.downweight_within_slots
            ),
            downweight_multiplier=_dec(
                "LP_UNLOCK_DOWNWEIGHT_MULTIPLIER", cls.downweight_multiplier
            ),
            exit_raise_within_slots=_int(
                "LP_UNLOCK_EXIT_RAISE_WITHIN_SLOTS", cls.exit_raise_within_slots
            ),
        )


# ---------------------------------------------------------------------------
# Reason taxonomy — dashboard / reasoner-facing vocabulary.  Stable codes.
# ---------------------------------------------------------------------------


class LpUnlockReason(StrEnum):
    """Stable E19 reason codes (dashboard + SLOW-loop reasoner contract)."""

    LP_UNLOCK_IMMINENT = "lp_unlock_imminent"  # REJECT band (nearest)
    LP_UNLOCK_APPROACHING = "lp_unlock_approaching"  # DOWN_WEIGHT band (wider)
    LP_UNLOCK_SCHEDULE_UNKNOWN = "lp_unlock_schedule_unknown"  # REJECT: refuse-by-default
    LP_BURNED_NO_UNLOCK = "lp_burned_no_unlock"  # info only: burned, no unlock ever
    LP_UNLOCK_FAR_OFF = "lp_unlock_far_off"  # info only: unlock well beyond the horizon


class LpUnlockVerdict(StrEnum):
    PASS = "PASS"  # burned or far-off — SILENT (never a buy trigger)
    DOWN_WEIGHT = "DOWN_WEIGHT"  # unlock approaching — conviction HAIRCUT (< 1)
    REJECT = "REJECT"  # unlock imminent OR schedule unknown — EXCLUDED


# ---------------------------------------------------------------------------
# Decision — REJECT / DOWN_WEIGHT / PASS only.  Never sizes / buys.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LpUnlockDecision:
    """The E19 entry verdict.  Structurally incapable of raising conviction/size.

    ``conviction_multiplier`` is hard-clamped to ``[0, 1]`` by the invariants below
    — NOT an ``assert`` (survives ``python -O`` / ``PYTHONOPTIMIZE=1``).  ``red_flag_hit``
    is populated ONLY on REJECT so a pre-gate composition (mirroring E15/E16) can
    short-circuit BEFORE the safety gate runs.  ``slots_to_unlock`` is the measured
    event-time proximity (``unlock_slot - as_of_event_slot``), or ``None`` for a
    burned/unknown schedule.
    """

    mint: str
    verdict: LpUnlockVerdict
    conviction_multiplier: Decimal
    reason_codes: tuple[str, ...]
    slots_to_unlock: int | None
    unlock_slot: int | None
    as_of_event_slot: int
    event_block_time_ms: int
    red_flag_hit: RedFlagHit | None

    _REJECT_FLAGS = (RedFlag.LP_UNLOCK_IMMINENT, RedFlag.LP_UNLOCK_SCHEDULE_UNKNOWN)

    def __post_init__(self) -> None:
        if not (Decimal(0) <= self.conviction_multiplier <= Decimal(1)):
            raise ValueError(
                "LpUnlockDecision.conviction_multiplier must be in [0, 1] "
                "(E19 is DE-RISK ONLY; it can never raise conviction)."
            )
        if self.verdict is LpUnlockVerdict.REJECT:
            if self.conviction_multiplier != Decimal(0):
                raise ValueError("a REJECT must carry conviction_multiplier == 0")
            if self.red_flag_hit is None:
                raise ValueError("a REJECT must carry a red_flag_hit (pre-gate veto contract)")
            if self.red_flag_hit.flag not in self._REJECT_FLAGS:
                raise ValueError(
                    "a REJECT's red_flag_hit must be LP_UNLOCK_IMMINENT or "
                    "LP_UNLOCK_SCHEDULE_UNKNOWN"
                )
        if self.verdict is LpUnlockVerdict.PASS:
            if self.conviction_multiplier != Decimal(1):
                raise ValueError("a PASS must carry conviction_multiplier == 1 (no change)")
            if self.red_flag_hit is not None:
                raise ValueError("a PASS must not carry a red_flag_hit")
        if self.verdict is LpUnlockVerdict.DOWN_WEIGHT:
            if not (Decimal(0) <= self.conviction_multiplier < Decimal(1)):
                raise ValueError("a DOWN_WEIGHT must carry conviction_multiplier in [0, 1)")
            if self.red_flag_hit is not None:
                raise ValueError("a DOWN_WEIGHT must not carry a red_flag_hit (it does not reject)")

    @property
    def passed(self) -> bool:
        """True iff NOT rejected.  NOTE: DOWN_WEIGHT is 'passed but de-rated'."""
        return self.verdict is not LpUnlockVerdict.REJECT

    @property
    def rejected(self) -> bool:
        return self.verdict is LpUnlockVerdict.REJECT

    @property
    def down_weighted(self) -> bool:
        return self.verdict is LpUnlockVerdict.DOWN_WEIGHT

    @property
    def refused_unknown(self) -> bool:
        """True iff the REJECT was caused by an UNKNOWN/undecodable schedule
        (distinct from a CONFIRMED imminent unlock — honest tagging)."""
        return (
            self.verdict is LpUnlockVerdict.REJECT
            and self.red_flag_hit is not None
            and self.red_flag_hit.flag is RedFlag.LP_UNLOCK_SCHEDULE_UNKNOWN
        )

    def apply(self, capped_size: int) -> int:
        """Apply the conviction haircut to an ALREADY-CAPPED integer size (lamports).

        DE-RISK ONLY: ``floor(capped_size * conviction_multiplier) <= capped_size``
        always (the multiplier is clamped to <= 1).  A REJECT yields 0.
        """
        if not isinstance(capped_size, int) or isinstance(capped_size, bool):
            raise TypeError("capped_size must be int lamports (money rule)")
        if capped_size < 0:
            raise ValueError("capped_size must be non-negative lamports")
        scaled = (Decimal(capped_size) * self.conviction_multiplier).to_integral_value(
            rounding="ROUND_FLOOR"
        )
        out = int(scaled)
        return min(out, capped_size)  # defence-in-depth: never exceed the input


# ---------------------------------------------------------------------------
# Telemetry seam — we INVOKE a sink; we do not author Prometheus here.
# ---------------------------------------------------------------------------


@runtime_checkable
class LpUnlockTelemetry(Protocol):
    def on_lp_unlock_reject(self, mint: str, slots_to_unlock: int | None) -> None: ...

    def on_lp_unlock_downweight(self, mint: str, slots_to_unlock: int, multiplier: str) -> None: ...


# ---------------------------------------------------------------------------
# The entry gate.
# ---------------------------------------------------------------------------


@dataclass
class LpUnlockGate:
    """Pure threshold evaluator over an ``LpUnlockSchedule``-shaped input.

    ``evaluate(schedule)`` may ONLY:
      * REJECT — schedule UNKNOWN/undecodable (refuse-by-default) OR unlock within
        ``reject_within_slots`` (imminent) — a ``RedFlagHit`` usable the same way as
        the E2 denylist's pre-gate veto, or
      * DOWN_WEIGHT — unlock in ``(reject_within_slots, downweight_within_slots]`` —
        a conviction MULTIPLIER < 1, or
      * PASS — BURNED (no unlock) or unlock beyond ``downweight_within_slots`` (far
        off) — SILENT, multiplier == 1, never a buy trigger.
    """

    thresholds: LpUnlockThresholds = field(default_factory=LpUnlockThresholds)
    telemetry: LpUnlockTelemetry | None = None

    def evaluate(
        self,
        schedule: LpUnlockScheduleLike,
        *,
        event_block_time_ms: int = 0,
    ) -> LpUnlockDecision:
        status = _status_value(schedule.status)
        as_of = schedule.as_of_event_slot
        unlock_slot = schedule.unlock_slot

        # BURNED — permanently locked, no unlock ever.  SILENT PASS (never a buy
        # trigger): a burned LP carries no unlock-window risk.
        if status == LpLockStatus.BURNED.value:
            return self._pass(
                schedule,
                event_block_time_ms=event_block_time_ms,
                slots_to_unlock=None,
            )

        # Refuse-by-default: an UNKNOWN schedule — or a TIME_LOCKED lock carrying no
        # readable unlock slot — is NEVER trusted as far-off/safe.  Instant pre-gate
        # REJECT, tagged DISTINCTLY from a confirmed imminent unlock (honest tagging).
        if status == LpLockStatus.UNKNOWN.value or (
            status == LpLockStatus.TIME_LOCKED.value and unlock_slot is None
        ):
            decision = self._reject(
                schedule,
                event_block_time_ms=event_block_time_ms,
                reason=LpUnlockReason.LP_UNLOCK_SCHEDULE_UNKNOWN,
                red_flag=RedFlag.LP_UNLOCK_SCHEDULE_UNKNOWN,
                slots_to_unlock=None,
                measured=None,
                threshold=None,
            )
            if self.telemetry is not None:
                self.telemetry.on_lp_unlock_reject(schedule.mint, None)
            return decision

        if status != LpLockStatus.TIME_LOCKED.value:
            # Unknown/unsupported status literal — refuse-by-default, never trust it.
            raise ValueError(
                f"LpUnlockGate: unsupported LP lock status {status!r} for {schedule.mint!r} "
                "(expected one of burned/time_locked/unknown)."
            )

        # TIME_LOCKED with a known unlock slot.  Proximity is a pure EVENT-TIME slot
        # difference (never wall-clock).  A non-positive value means the lock has
        # ALREADY released (a pullable window is open now) — it is <= reject horizon
        # and so falls into the imminent REJECT band naturally.
        assert unlock_slot is not None  # narrowed by the guards above  # noqa: S101
        slots_to_unlock = unlock_slot - as_of

        if slots_to_unlock <= self.thresholds.reject_within_slots:
            decision = self._reject(
                schedule,
                event_block_time_ms=event_block_time_ms,
                reason=LpUnlockReason.LP_UNLOCK_IMMINENT,
                red_flag=RedFlag.LP_UNLOCK_IMMINENT,
                slots_to_unlock=slots_to_unlock,
                measured=slots_to_unlock,
                threshold=self.thresholds.reject_within_slots,
            )
            if self.telemetry is not None:
                self.telemetry.on_lp_unlock_reject(schedule.mint, slots_to_unlock)
            return decision

        if slots_to_unlock <= self.thresholds.downweight_within_slots:
            decision = LpUnlockDecision(
                mint=schedule.mint,
                verdict=LpUnlockVerdict.DOWN_WEIGHT,
                conviction_multiplier=self.thresholds.downweight_multiplier,
                reason_codes=(LpUnlockReason.LP_UNLOCK_APPROACHING.value,),
                slots_to_unlock=slots_to_unlock,
                unlock_slot=unlock_slot,
                as_of_event_slot=as_of,
                event_block_time_ms=event_block_time_ms,
                red_flag_hit=None,
            )
            if self.telemetry is not None:
                self.telemetry.on_lp_unlock_downweight(
                    schedule.mint, slots_to_unlock, str(self.thresholds.downweight_multiplier)
                )
            return decision

        # Far off — SILENT PASS.
        return self._pass(
            schedule,
            event_block_time_ms=event_block_time_ms,
            slots_to_unlock=slots_to_unlock,
        )

    def _pass(
        self,
        schedule: LpUnlockScheduleLike,
        *,
        event_block_time_ms: int,
        slots_to_unlock: int | None,
    ) -> LpUnlockDecision:
        return LpUnlockDecision(
            mint=schedule.mint,
            verdict=LpUnlockVerdict.PASS,
            conviction_multiplier=Decimal(1),
            reason_codes=(),
            slots_to_unlock=slots_to_unlock,
            unlock_slot=schedule.unlock_slot,
            as_of_event_slot=schedule.as_of_event_slot,
            event_block_time_ms=event_block_time_ms,
            red_flag_hit=None,
        )

    def _reject(
        self,
        schedule: LpUnlockScheduleLike,
        *,
        event_block_time_ms: int,
        reason: LpUnlockReason,
        red_flag: RedFlag,
        slots_to_unlock: int | None,
        measured: int | None,
        threshold: int | None,
    ) -> LpUnlockDecision:
        return LpUnlockDecision(
            mint=schedule.mint,
            verdict=LpUnlockVerdict.REJECT,
            conviction_multiplier=Decimal(0),
            reason_codes=(reason.value,),
            slots_to_unlock=slots_to_unlock,
            unlock_slot=schedule.unlock_slot,
            as_of_event_slot=schedule.as_of_event_slot,
            event_block_time_ms=event_block_time_ms,
            red_flag_hit=RedFlagHit(red_flag, measured, threshold),
        )


# ---------------------------------------------------------------------------
# Composed pre-gate veto — mirrors blacklist.py's evaluate_with_denylist.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LpUnlockPreGateResult:
    """Result of composing the E19 entry gate BEFORE a downstream step.

    ``next_result`` is ``None`` iff the E19 gate REJECTED and short-circuited BEFORE
    the downstream step ran — proving the downstream step never green-lit a token
    whose LP unlock is imminent (or whose schedule is undecodable).
    """

    passed: bool
    decision: LpUnlockDecision
    next_result: object | None

    @property
    def rejected_by_lp_unlock(self) -> bool:
        return self.decision.rejected


def evaluate_with_lp_unlock(
    *,
    gate: LpUnlockGate,
    schedule: LpUnlockScheduleLike,
    next_step,
    event_block_time_ms: int = 0,
) -> LpUnlockPreGateResult:
    """Run the E19 LP-unlock gate, THEN ``next_step()`` — REJECT-only short-circuit.

    ``next_step`` is a zero-arg callable invoked ONLY when the gate does not REJECT.
    This makes "skip the pre-gate veto because the LP is locked right now" structurally
    inexpressible: there is no branch that returns ``passed=True`` on a REJECT without
    ``next_step`` ever running (``next_result is None`` proves it never ran).

    A DOWN_WEIGHT verdict does NOT short-circuit — ``next_step`` still runs; the caller
    applies ``decision.conviction_multiplier`` to sizing separately (same non-blocking
    shape as anti_fomo / E15 / E16).
    """
    decision = gate.evaluate(schedule, event_block_time_ms=event_block_time_ms)
    if decision.rejected:
        return LpUnlockPreGateResult(passed=False, decision=decision, next_result=None)

    next_result = next_step()
    passed = bool(getattr(next_result, "passed", True))
    return LpUnlockPreGateResult(passed=passed, decision=decision, next_result=next_result)


# ---------------------------------------------------------------------------
# (b) OPEN-POSITION exit watcher — SLOW-loop producer of the pre-set exit flag.
# Mirrors sellability_reprobe.py: it can ONLY ever SET the flag (force an exit).
# ---------------------------------------------------------------------------

# Mirrors set_sellability_degraded_flag's default TTL (120s) — same presence semantics.
DEFAULT_FLAG_TTL_SECONDS = 120


class LpUnlockExitReason(StrEnum):
    """Classification of an open position's LP-unlock posture at one tick.

    HEALTHY is the ONLY non-raising outcome; every other outcome raises the
    ``lp_unlock_approaching`` flag the FAST exit branch reads.
    """

    HEALTHY = "healthy"  # burned or far-off unlock — no flag
    APPROACHING = "lp_unlock_approaching"  # unlock within the exit horizon — raise
    SCHEDULE_UNKNOWN = "lp_unlock_schedule_unknown"  # refuse-by-default — raise

    @property
    def should_raise(self) -> bool:
        """True for every outcome except HEALTHY — i.e. every outcome that must
        raise the exit flag.  Refuse-by-default: SCHEDULE_UNKNOWN raises."""
        return self is not LpUnlockExitReason.HEALTHY


@dataclass(frozen=True)
class LpUnlockExitDecision:
    """The outcome of ONE unlock-proximity check on ONE OPEN position.

    ``raise_flag`` is the de-risk verdict (True => the ``lp_unlock_approaching`` flag
    should be / was raised for the FAST exit branch).  ``flag_set`` records whether the
    flag was actually written this call (False when no ``flag_sink`` is wired).
    ``slots_to_unlock`` is the measured EVENT-TIME proximity (``unlock_slot -
    event_slot``), or ``None`` when burned/unknown.
    """

    mint: str
    event_slot: int
    reason: LpUnlockExitReason
    raise_flag: bool
    flag_set: bool
    slots_to_unlock: int | None
    detail: str = ""


def classify_open_position_unlock(
    schedule: LpUnlockScheduleLike,
    *,
    event_slot: int,
    thresholds: LpUnlockThresholds,
) -> tuple[LpUnlockExitReason, int | None, str]:
    """Classify an OPEN position's LP-unlock posture at ``event_slot`` (pure).

    Returns ``(reason, slots_to_unlock, detail)``.  Refuse-by-default: an UNKNOWN
    schedule (or a time-lock with no readable unlock slot) => SCHEDULE_UNKNOWN (raise).
    A BURNED or far-off unlock => HEALTHY (no flag).  Proximity is a pure EVENT-TIME
    slot difference (never wall-clock).
    """
    if isinstance(event_slot, bool) or not isinstance(event_slot, int):
        raise TypeError("classify_open_position_unlock.event_slot must be int (event-time slot).")
    if event_slot < 0:
        raise ValueError("classify_open_position_unlock.event_slot must be >= 0.")

    status = _status_value(schedule.status)
    unlock_slot = schedule.unlock_slot

    if status == LpLockStatus.BURNED.value:
        return LpUnlockExitReason.HEALTHY, None, "burned LP: no unlock window, no flag"

    if status == LpLockStatus.UNKNOWN.value or (
        status == LpLockStatus.TIME_LOCKED.value and unlock_slot is None
    ):
        return (
            LpUnlockExitReason.SCHEDULE_UNKNOWN,
            None,
            "undecodable LP unlock schedule (refuse-by-default => raise exit flag)",
        )

    if status != LpLockStatus.TIME_LOCKED.value:
        raise ValueError(
            f"classify_open_position_unlock: unsupported LP lock status {status!r} "
            f"for {schedule.mint!r} (expected burned/time_locked/unknown)."
        )

    assert unlock_slot is not None  # narrowed above  # noqa: S101
    slots_to_unlock = unlock_slot - event_slot
    if slots_to_unlock <= thresholds.exit_raise_within_slots:
        return (
            LpUnlockExitReason.APPROACHING,
            slots_to_unlock,
            f"unlock in {slots_to_unlock} slots (<= {thresholds.exit_raise_within_slots}); "
            "pullable-LP window is a rising rug risk",
        )
    return (
        LpUnlockExitReason.HEALTHY,
        slots_to_unlock,
        f"unlock in {slots_to_unlock} slots (> {thresholds.exit_raise_within_slots}); far off",
    )


class LpUnlockApproachingFlagSink(Protocol):
    """The minimal write surface the watcher needs from the shared StateStore.

    Structural (duck-typed): the controller StateStore satisfies this WITHOUT importing
    the controller package.  Mirrors ``set_sellability_degraded_flag`` key-for-key.
    """

    def set_lp_unlock_approaching_flag(
        self,
        mint: str,
        *,
        reason: str,
        event_slot: int,
        ttl_seconds: int = 120,
    ) -> None: ...  # pragma: no cover


@dataclass
class LpUnlockExitWatcher:
    """SLOW-loop, de-risk-only producer of the ``lp_unlock_approaching`` exit flag.

    USAGE (off the hot path — the SLOW-loop / enrichment runner drives this):

        watcher = LpUnlockExitWatcher(flag_sink=state_store)
        # periodically, per open position, with the current on-chain slot (event-time):
        decision = watcher.watch(schedule, event_slot=current_slot)
        # if decision.raise_flag, the flag the FAST exit branch reads via
        # StateStore.get_lp_unlock_approaching_flag(mint) has ALREADY been pre-set.

    De-risk-only: ``watch`` can ONLY ever SET the flag (via ``flag_sink``); there is no
    method here that clears it, sizes a trade, widens a stop, or holds longer.  It runs
    no schedule decode / RPC of its own — it consumes a decoded ``schedule``.
    """

    thresholds: LpUnlockThresholds = field(default_factory=LpUnlockThresholds)
    flag_sink: LpUnlockApproachingFlagSink | None = None
    flag_ttl_seconds: int = DEFAULT_FLAG_TTL_SECONDS

    def __post_init__(self) -> None:
        if isinstance(self.flag_ttl_seconds, bool) or not isinstance(self.flag_ttl_seconds, int):
            raise TypeError("LpUnlockExitWatcher.flag_ttl_seconds must be int (seconds).")
        if self.flag_ttl_seconds < 1:
            raise ValueError("LpUnlockExitWatcher.flag_ttl_seconds must be >= 1.")

    def watch(
        self,
        schedule: LpUnlockScheduleLike,
        *,
        event_slot: int,
    ) -> LpUnlockExitDecision:
        """Evaluate ONE open position's unlock proximity and, on an approaching/unknown
        schedule, PRE-SET the flag via ``flag_sink``.  Returns the classification.

        Off the hot path: point-in-time (``event_slot`` is the current on-chain tick
        slot; proximity is a pure slot difference, no wall-clock).  De-risk only: the
        sole side effect is SETTING the flag (never clearing it).
        """
        reason, slots_to_unlock, detail = classify_open_position_unlock(
            schedule, event_slot=event_slot, thresholds=self.thresholds
        )
        raise_flag = reason.should_raise
        flag_set = False
        if raise_flag and self.flag_sink is not None:
            self.flag_sink.set_lp_unlock_approaching_flag(
                schedule.mint,
                reason=reason.value,
                event_slot=event_slot,
                ttl_seconds=self.flag_ttl_seconds,
            )
            flag_set = True
            logger.warning(
                "LpUnlockExitWatcher: RAISE mint=%s reason=%s slots_to_unlock=%s "
                "event_slot=%d — lp_unlock_approaching flag SET (de-risk force-exit).",
                schedule.mint,
                reason.value,
                slots_to_unlock,
                event_slot,
            )
        return LpUnlockExitDecision(
            mint=schedule.mint,
            event_slot=event_slot,
            reason=reason,
            raise_flag=raise_flag,
            flag_set=flag_set,
            slots_to_unlock=slots_to_unlock,
            detail=detail,
        )
