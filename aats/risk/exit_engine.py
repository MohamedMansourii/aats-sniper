"""T-325 — Productionized ExitEngine: TP-ladder + trailing + hard-stop + timeout.

Photon priority #1 — the biggest PROVEN lever (the sim's staged ladder beats the
naive "0.5x-of-peak" exit by ~+24% on the colo A/B, sniper_sim/demo.py).  This
module promotes that sim `run_exit` (`sniper_sim/exits.py`) into the production
risk lane, BEHIND the typed contracts and the survivable stop (T-321), with:

  * integer/Decimal money ONLY — never float (data-models.md §0);
  * de-risk-only outputs — every exit decision is an ExitIntent / ReduceIntent
    built from the DeRiskIntentFactory (ADR-0006).  There is NO add path here;
  * Fast vs Secure MEV exit modes (Photon's two routings, modeled);
  * "auto-strat" presets selectable per surface / aggressiveness;
  * point-in-time correctness — every decision is a pure function of the FIXED
    config, the FIXED-at-entry stop, and the point-in-time mark.  No wall-clock,
    no lookahead.  The SAME code runs live and in backtest.

SEAM / OWNERSHIP (AATS-ROSTER §4)
---------------------------------
  * THIS module (`risk-guardrails-engineer`) DECIDES the exit (which rung, when
    to trail-out, when the hard stop / timeout fires) and emits the de-risk
    Intent.  It is the STATEFUL strategy layer.
  * The per-tick ORDERED hierarchy (narrative_failure > hard-stop > take-profit)
    is `rule_engine.py` (T-324); the ExitEngine REUSES the hard-stop primitive
    (`survivable_stop.StopState`, T-321) so the ladder and the survivable stop
    enforce the SAME fixed line.  A hard stop fired here is identical to the one
    the Layer-2 enforcer / Layer-1 resting order fire — they never disagree.
  * `solana-execution-engineer` builds/signs/lands the emitted Intent behind the
    `ExecutionVenue` seam (behind the DRY_RUN gate).  This module authors NO
    transaction-building or RPC code and performs NO real-capital action.

RELATION TO T-324 rule_engine
-----------------------------
`rule_engine.HierarchicalRiskRuleEngine` is the STATELESS single-tick evaluator
(it has no memory of which rungs fired).  The ExitEngine is the STATEFUL wrapper
that remembers, across a position's lifetime: which TP rungs already fired,
whether the trailing stop is armed, the running peak, and how many steps the
position has been held (for the timeout).  It still defers the priority ordering
to the same three rules and never lets a lower rule override a higher one.

NON-WAIVABLE LAWS (encoded as types/structure, asserted in tests)
-----------------------------------------------------------------
  * Every outcome is de-risk: ExitIntent (full flatten) / ReduceIntent
    (scale-out) / NO-OP.  Never an EntryIntent (not constructible on this path).
  * TRAILING TIGHTENS, NEVER WIDENS.  The trailing exit floor only ratchets UP
    with the peak; a lower mark can fire it but can never lower the armed floor.
  * PROFIT-LOCK RATCHET TIGHTENS, NEVER WIDENS.  A discrete stepped floor
    (breakeven at 2x, lock 2x at 3x, rungs below): once the peak crosses a rung the
    locked floor is RAISED to a fixed multiple of entry and can never fall again.
    It is the tighter complement of the proportional trail — it only adds
    protection; a transition lowering `locked_floor_r` is rejected at the boundary.
  * The HARD STOP is FIXED at entry and never widened (it is the T-321 StopState
    trigger, reused verbatim).
  * The TP ladder REDUCES only (each rung 1..9999 bps of remaining); a full exit
    is the stop / narrative / timeout path, never a TP rung.
  * The LLM is NEVER on this path; a catastrophic exit arrives ONLY as a pre-set
    `narrative_failure` flag the engine reads (it never calls an LLM).
  * The insider/dev-sell DUMP exit (E14b) arrives the SAME asymmetric, off-hot-path
    way: a pre-set `insider_dump` flag produced by E14a (on-chain creator/top-holder
    distribution detection in `aats.ingestion.insider_dump`, persisted via
    `StateStore.set_insider_dump_flag`).  The FAST branch only READS the bool — no
    detection, RPC, or LLM here — and it can ONLY force an exit (de-risk); it can
    never hold longer, widen/relax a stop, or size up.
  * The DELAYED-HONEYPOT / TAX-FLIP sellability exit (E17) arrives the SAME
    asymmetric, off-hot-path way: a pre-set `sellability_degraded` flag produced by
    the SLOW-loop re-probe (`aats.risk.sellability_reprobe`), which RE-RUNS the
    SHARED sell-sim (`aats.execution.sell_sim`) on open positions and persists a
    DEGRADED / INCONCLUSIVE (refuse-by-default) result via
    `StateStore.set_sellability_degraded_flag`.  The FAST branch only READS the bool
    — no sell-sim, RPC, or detection here — and it can ONLY force an exit (de-risk);
    it can never hold longer, widen/relax a stop, or size up.
  * Money is integer lamports / Decimal price.  Float is rejected at the boundary.
  * DRY-RUN: this module decides only; the venue handoff is behind the DRY_RUN
    gate.  The `paper_walk` simulator below NEVER touches a network.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum

from aats.contracts.intents import (
    DeRiskIntentFactory,
    ExitIntent,
    ReduceIntent,
)
from aats.risk.survivable_stop import StopState, StopThresholds

# Full flatten = 100% of remaining (bps).  A scale-out rung is strictly < this.
_FULL_EXIT_BPS = 10_000
_BPS_DENOM = Decimal(10_000)


# --------------------------------------------------------------------------- #
# Money / type guards
# --------------------------------------------------------------------------- #


def _to_decimal_price(value: object, field_name: str) -> Decimal:
    """Coerce a price/return input to Decimal, REJECTING float (data-models.md §0).

    int and str (and Decimal) are accepted; a raw float is refused so a lossy
    binary price can never enter the exit math.
    """
    if isinstance(value, bool):  # bool is an int subclass — refuse explicitly
        raise TypeError(f"{field_name} must be Decimal/int/str, got bool {value!r}.")
    if isinstance(value, float):
        raise TypeError(
            f"{field_name} must be Decimal/int/str (money-derived exact), "
            f"got float {value!r}.  Float money is forbidden (data-models.md §0)."
        )
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int | str):
        return Decimal(value)
    raise TypeError(f"{field_name} must be Decimal/int/str, got {type(value).__name__}.")


# --------------------------------------------------------------------------- #
# MEV exit modes (Photon's Fast vs Secure, modeled).  These are routing modes —
# they DO NOT change the trigger logic, only the exit slippage / sandwich model
# that the paper-walk applies and the `exit_mode` tag the emitted Intent carries.
# --------------------------------------------------------------------------- #


class MevExitMode(StrEnum):
    """Photon's two exit routings.

    FAST   — speed / public-ish landing.  More sandwich risk, lower base haircut.
    SECURE — private routing (default, OQ-008).  Sandwich-safe, slightly worse
             base price.  Forced/stop/narrative exits ALWAYS go SECURE so a
             defensive exit is never sandwiched on the way out.
    """

    FAST = "fast"
    SECURE = "secure"


@dataclass(frozen=True)
class MevSlippageModel:
    """The slippage / sandwich model for a routing mode (paper-walk only).

    All quantities are integer BASIS POINTS (exact, not float).  `sandwich_p_bps`
    is the probability of being sandwiched on a sell, in bps (e.g. 3000 = 30%);
    the paper-walk uses a seeded RNG to apply it.  Production landing economics
    come from the live venue — this model exists only to reproduce the sim's A/B
    lift deterministically and to stratify Fast vs Secure.
    """

    exit_slippage_bps: int  # per-sell base haircut (bps)
    sandwich_p_bps: int  # P(sandwiched on a sell), in bps
    sandwich_loss_bps: int  # extra haircut if sandwiched (bps)

    def __post_init__(self) -> None:
        for name in ("exit_slippage_bps", "sandwich_p_bps", "sandwich_loss_bps"):
            v = getattr(self, name)
            if isinstance(v, bool | float):
                raise TypeError(f"MevSlippageModel.{name} must be int (bps), not float/bool.")
            if v < 0:
                raise ValueError(f"MevSlippageModel.{name} must be >= 0 bps, got {v}.")
        if self.sandwich_p_bps > _FULL_EXIT_BPS:
            raise ValueError("sandwich_p_bps must be <= 10000 (a probability in bps).")


# Modeled from sniper_sim FAST_EXIT / SECURE_EXIT, converted to integer bps.
#   FAST:   exit_slippage 0.015 -> 150 bps; sandwich_p 0.30 -> 3000; loss 0.12 -> 1200
#   SECURE: exit_slippage 0.025 -> 250 bps; sandwich_p 0.08 ->  800; loss 0.04 ->  400
FAST_SLIPPAGE = MevSlippageModel(exit_slippage_bps=150, sandwich_p_bps=3000, sandwich_loss_bps=1200)
SECURE_SLIPPAGE = MevSlippageModel(exit_slippage_bps=250, sandwich_p_bps=800, sandwich_loss_bps=400)


def slippage_model_for(mode: MevExitMode) -> MevSlippageModel:
    return FAST_SLIPPAGE if mode is MevExitMode.FAST else SECURE_SLIPPAGE


# --------------------------------------------------------------------------- #
# ExitConfig — the FROZEN-at-entry strategy (productionized sim ExitConfig).
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class TpRung:
    """One TP ladder rung: at/above `trigger_r`, scale out `fraction_bps`.

    `trigger_r` is the realized return on ENTRY (mark / entry_fill_price), a
    Decimal multiple (e.g. Decimal("1.5") == +50%).  `fraction_bps` is a
    REDUCTION of the remaining position (1..9999) — a partial scale-out; a 100%
    exit is the hard-stop / narrative / timeout path, never a TP rung.
    """

    trigger_r: Decimal
    fraction_bps: int

    def __post_init__(self) -> None:
        trig = _to_decimal_price(self.trigger_r, "TpRung.trigger_r")
        object.__setattr__(self, "trigger_r", trig)
        if trig <= 0:
            raise ValueError("TpRung.trigger_r must be > 0.")
        if isinstance(self.fraction_bps, bool | float):
            raise TypeError("TpRung.fraction_bps must be int (bps), not float/bool.")
        if not (1 <= self.fraction_bps <= 9_999):
            raise ValueError(
                "TpRung.fraction_bps must be in [1, 9999] (a partial scale-out; a "
                f"full exit is the stop/narrative/timeout path), got {self.fraction_bps}."
            )


@dataclass(frozen=True)
class RatchetRung:
    """One PROFIT-LOCK RATCHET rung: once peak R reaches `at_r`, the locked exit
    floor RATCHETS UP to `lock_r` and never falls again.

    This is the discrete stepped profit-lock the audit demands (e.g. breakeven at
    2x, lock 2x at 3x, with rungs below).  It is distinct from the proportional
    `trail_drawdown_bps` trail: the ratchet locks a FIXED multiple of entry the
    instant the peak crosses a rung, where the proportional trail tracks a band
    below the live peak.  The engine enforces the TIGHTER (higher) of the two
    floors — the ratchet can only ever ADD protection, never remove it.

    `at_r`   — the peak-R threshold that ARMS this rung (a Decimal multiple > 1;
               you only lock profit once you have run, so a rung never arms at or
               below entry).
    `lock_r` — the floor the position is guaranteed once `at_r` is reached, as a
               Decimal multiple of entry.  MUST be in (0, at_r): you cannot lock a
               floor at or above the very price that armed it (that would force an
               instant exit), and `lock_r > 0` keeps the floor a real price.  A
               `lock_r` of exactly 1.0 is "breakeven"; > 1.0 locks in profit.

    De-risk only: a rung can ONLY raise the floor.  There is no field that lowers
    it, widens it, or sizes up.  Rungs are validated strictly ascending in BOTH
    `at_r` and `lock_r` so the ratchet is monotone by construction.
    """

    at_r: Decimal
    lock_r: Decimal

    def __post_init__(self) -> None:
        at = _to_decimal_price(self.at_r, "RatchetRung.at_r")
        lock = _to_decimal_price(self.lock_r, "RatchetRung.lock_r")
        object.__setattr__(self, "at_r", at)
        object.__setattr__(self, "lock_r", lock)
        if at <= 1:
            raise ValueError(
                f"RatchetRung.at_r must be > 1 (a ratchet rung only arms once in "
                f"profit); got {at}."
            )
        if lock <= 0:
            raise ValueError(f"RatchetRung.lock_r must be > 0 (a real floor price); got {lock}.")
        if lock >= at:
            raise ValueError(
                f"RatchetRung.lock_r ({lock}) must be < at_r ({at}): you cannot lock a "
                "floor at or above the price that armed the rung (that would force an "
                "instant exit the moment it arms)."
            )


@dataclass(frozen=True)
class ExitConfig:
    """The FROZEN-at-entry exit strategy for one position (AC-033).

    Promoted from `sniper_sim/exits.py` ExitConfig.  ALL thresholds are integer
    bps or Decimal multiples — NEVER float.  Triggers are evaluated on
    R = mark / entry_fill_price (realized return on the actual fill), exactly as
    the sim does, so a "1.5x TP" means 1.5x your fill, not 1.5x the launch spot.

    Preset changes NEVER retro-modify an open position — the position carries its
    `exit_config_label` and the engine evaluates the config frozen at entry.
    """

    label: str
    # TP ladder — ascending trigger_r rungs, each a partial scale-out.
    tp_ladder: tuple[TpRung, ...]
    # Hard stop: full exit when R <= hard_stop_r.  This MUST equal the T-321
    # StopState trigger ratio so the ladder and the survivable stop agree.
    # e.g. Decimal("0.60") == -40% (sim hard_stop=0.60).
    hard_stop_r: Decimal
    # Trailing: arm once R >= trail_arm_r; after armed, exit the remainder if
    # R falls to/below peakR * (1 - trail_drawdown_bps/10000).
    trail_arm_r: Decimal
    trail_drawdown_bps: int
    # PROFIT-LOCK RATCHET — the discrete stepped floor (breakeven at 2x, lock 2x
    # at 3x, rungs below).  Each rung, once the PEAK reaches `at_r`, ratchets the
    # locked exit floor up to `lock_r` (a fixed multiple of entry).  The locked
    # floor only ever RISES; the remainder is force-exited if R falls to/below it.
    # This is enforced alongside (and as the TIGHTER of) the proportional trail —
    # the ratchet can only ADD protection, never remove it.  Rungs MUST be strictly
    # ascending in both at_r and lock_r.  Empty tuple == ratchet disabled (the
    # engine then falls back to the proportional trail alone — never less safe).
    # Timeout: force-exit the remainder after this many event-time steps held.
    max_hold_steps: int
    # PROFIT-LOCK RATCHET rungs (see field doc above).  Empty == disabled.
    ratchet_ladder: tuple[RatchetRung, ...] = ()
    # E12 — Time-stop / stale-narrative EXIT (SLOW-loop, de-risk only).
    #
    # If a position has been FLAT (mark stayed inside `time_stop_flat_band_bps` of
    # entry) for at least `time_stop_hours` of EVENT-TIME *and* its SLOW-loop
    # narrative score has cooled to/below `time_stop_narrative_cooled_at`, the
    # remainder is force-exited.  This is a stale-bag de-risk: a coin that neither
    # ran nor rugged but whose social story has gone cold is dead money.
    #
    #   * `time_stop_hours = None` DISABLES the branch (fail-safe default — never
    #     more aggressive than the existing engine unless explicitly enabled).
    #   * Hours are EVENT-TIME (block_time_ms deltas), never wall-clock.
    #   * The narrative score is a PRE-COMPUTED SLOW-loop point-in-time value passed
    #     into on_tick — the engine NEVER calls an LLM/social source on the hot path.
    #   * De-risk only: the sole outcome is a full exit of the remainder.  It cannot
    #     widen a stop, size up, or hold longer than the existing rules already do.
    time_stop_hours: int | None = None
    # Price is "flat" when |mark/entry - 1| <= this band (bps).  A mark outside the
    # band (either direction) is a "move" that resets the flat clock.
    time_stop_flat_band_bps: int = 1_500  # +/-15% around entry == flat
    # Narrative is "cooled" when the SLOW-loop score is <= this threshold.  Decimal
    # in [0, 1]; lower == colder.  The default 0 means "only an ice-cold (0) story
    # qualifies" — callers tune this up to make the time-stop more selective.
    time_stop_narrative_cooled_at: Decimal = Decimal(0)
    # Default routing for the gain-booking scale-outs (forced exits go SECURE).
    default_exit_mode: MevExitMode = MevExitMode.SECURE

    def __post_init__(self) -> None:
        if not self.label or not isinstance(self.label, str):
            raise ValueError("ExitConfig.label must be a non-empty str.")
        hs = _to_decimal_price(self.hard_stop_r, "ExitConfig.hard_stop_r")
        ta = _to_decimal_price(self.trail_arm_r, "ExitConfig.trail_arm_r")
        object.__setattr__(self, "hard_stop_r", hs)
        object.__setattr__(self, "trail_arm_r", ta)
        if not (0 < hs < 1):
            raise ValueError(
                f"ExitConfig.hard_stop_r must be in (0, 1) — a loss multiple "
                f"(e.g. 0.60 = -40%); got {hs}."
            )
        if ta <= 1:
            raise ValueError(
                f"ExitConfig.trail_arm_r must be > 1 (arm only once in profit); got {ta}."
            )
        if isinstance(self.trail_drawdown_bps, bool | float):
            raise TypeError("ExitConfig.trail_drawdown_bps must be int (bps), not float/bool.")
        if not (0 < self.trail_drawdown_bps < _FULL_EXIT_BPS):
            raise ValueError(
                "ExitConfig.trail_drawdown_bps must be in (0, 10000) bps; "
                f"got {self.trail_drawdown_bps}."
            )
        if isinstance(self.max_hold_steps, bool | float):
            raise TypeError("ExitConfig.max_hold_steps must be int, not float/bool.")
        if self.max_hold_steps <= 0:
            raise ValueError("ExitConfig.max_hold_steps must be > 0.")
        # E12 — time-stop / stale-narrative validation.  Disabled (None) by default.
        if self.time_stop_hours is not None:
            if isinstance(self.time_stop_hours, bool | float):
                raise TypeError(
                    "ExitConfig.time_stop_hours must be int hours or None, not float/bool."
                )
            if self.time_stop_hours <= 0:
                raise ValueError(
                    "ExitConfig.time_stop_hours must be > 0 (a positive event-time horizon) "
                    f"or None to disable; got {self.time_stop_hours}."
                )
        if isinstance(self.time_stop_flat_band_bps, bool | float):
            raise TypeError("ExitConfig.time_stop_flat_band_bps must be int (bps), not float/bool.")
        if not (0 < self.time_stop_flat_band_bps <= _FULL_EXIT_BPS):
            raise ValueError(
                "ExitConfig.time_stop_flat_band_bps must be in (0, 10000] bps; "
                f"got {self.time_stop_flat_band_bps}."
            )
        cooled = _to_decimal_price(
            self.time_stop_narrative_cooled_at, "ExitConfig.time_stop_narrative_cooled_at"
        )
        object.__setattr__(self, "time_stop_narrative_cooled_at", cooled)
        if not (Decimal(0) <= cooled <= Decimal(1)):
            raise ValueError(
                "ExitConfig.time_stop_narrative_cooled_at must be a Decimal in [0, 1] "
                f"(a normalized narrative score); got {cooled}."
            )
        if not isinstance(self.tp_ladder, tuple) or len(self.tp_ladder) == 0:
            raise ValueError("ExitConfig.tp_ladder must be a non-empty tuple of TpRung.")
        # Rungs must be strictly ascending in trigger_r (a ladder, not a heap),
        # and cumulative scale-out must not exceed 100% of the position.
        prev = Decimal(0)
        cumulative = 0
        for rung in self.tp_ladder:
            if not isinstance(rung, TpRung):
                raise TypeError("ExitConfig.tp_ladder entries must be TpRung instances.")
            if rung.trigger_r <= prev:
                raise ValueError(
                    "ExitConfig.tp_ladder rungs must be strictly ascending in trigger_r; "
                    f"{rung.trigger_r} follows {prev}."
                )
            prev = rung.trigger_r
            cumulative += rung.fraction_bps
        if cumulative > _FULL_EXIT_BPS:
            raise ValueError(
                "ExitConfig.tp_ladder cumulative fraction_bps must be <= 10000 (cannot "
                f"scale out more than 100% of the position); got {cumulative}."
            )
        # PROFIT-LOCK RATCHET validation — strictly ascending in BOTH at_r and
        # lock_r (a monotone ratchet, never a heap that could de-tighten).
        if not isinstance(self.ratchet_ladder, tuple):
            raise TypeError("ExitConfig.ratchet_ladder must be a tuple of RatchetRung.")
        prev_at = Decimal(0)
        prev_lock = Decimal(0)
        for rrung in self.ratchet_ladder:
            if not isinstance(rrung, RatchetRung):
                raise TypeError("ExitConfig.ratchet_ladder entries must be RatchetRung instances.")
            if rrung.at_r <= prev_at:
                raise ValueError(
                    "ExitConfig.ratchet_ladder rungs must be strictly ascending in at_r; "
                    f"{rrung.at_r} follows {prev_at}."
                )
            if rrung.lock_r <= prev_lock:
                raise ValueError(
                    "ExitConfig.ratchet_ladder rungs must be strictly ascending in lock_r "
                    f"(a higher rung must lock a HIGHER floor); {rrung.lock_r} follows {prev_lock}."
                )
            prev_at = rrung.at_r
            prev_lock = rrung.lock_r

    def stop_thresholds(self) -> StopThresholds:
        """Derive the T-321 StopThresholds from hard_stop_r so the survivable stop
        and the ladder enforce the SAME fixed line.

        hard_stop_r = 0.60 means -40% means a 4000-bps drawdown stop.
        """
        drawdown_bps = int((Decimal(1) - self.hard_stop_r) * _BPS_DENOM)
        return StopThresholds(hard_stop_drawdown_bps=drawdown_bps)


# --------------------------------------------------------------------------- #
# Auto-strat presets — selectable per surface / aggressiveness (Photon-style).
# Every preset is de-risk-shaped by construction (validated by ExitConfig).
# --------------------------------------------------------------------------- #

# Modeled from sniper_sim FAST_EXIT / SECURE_EXIT ladders, converted to integer
# bps / Decimal multiples.  The two differ ONLY in default routing — the trigger
# geometry is identical, matching the sim (the MEV mode is a routing choice).
_BALANCED_LADDER = (
    TpRung(trigger_r=Decimal("1.5"), fraction_bps=3000),
    TpRung(trigger_r=Decimal("3.0"), fraction_bps=3000),
    TpRung(trigger_r=Decimal("6.0"), fraction_bps=2000),
)

# The PROFIT-LOCK RATCHET (the audit's canonical shape):
#   * a rung BELOW breakeven that locks the early give-back tighter than the -40%
#     hard stop once the position has shown a real run (1.5x -> lock 0.9x);
#   * BREAKEVEN at 2x (the trail arm) — once you double, you cannot give it all
#     back: the floor is locked at entry (1.0x);
#   * LOCK 2x at 3x — once you triple, two-bag profit is locked in (floor 2.0x);
#   * and a higher rung (6x -> lock 4x) so a big runner keeps ratcheting.
# Strictly ascending in both at_r and lock_r; de-risk only (the floor only rises).
_BALANCED_RATCHET = (
    RatchetRung(at_r=Decimal("1.5"), lock_r=Decimal("0.9")),
    RatchetRung(at_r=Decimal("2.0"), lock_r=Decimal("1.0")),  # breakeven at 2x
    RatchetRung(at_r=Decimal("3.0"), lock_r=Decimal("2.0")),  # lock 2x at 3x
    RatchetRung(at_r=Decimal("6.0"), lock_r=Decimal("4.0")),
)

AUTO_STRAT_PRESETS: dict[str, ExitConfig] = {
    # The production default (Secure routing, OQ-008) — mirrors sim SECURE_EXIT.
    "secure_balanced": ExitConfig(
        label="secure_balanced",
        tp_ladder=_BALANCED_LADDER,
        hard_stop_r=Decimal("0.60"),  # -40%
        trail_arm_r=Decimal("2.0"),
        trail_drawdown_bps=3000,  # 30%
        max_hold_steps=24,
        ratchet_ladder=_BALANCED_RATCHET,  # breakeven@2x, lock 2x@3x, rungs below
        default_exit_mode=MevExitMode.SECURE,
    ),
    # Fast routing (public-ish, lower base haircut, more sandwich) — sim FAST_EXIT.
    "fast_balanced": ExitConfig(
        label="fast_balanced",
        tp_ladder=_BALANCED_LADDER,
        hard_stop_r=Decimal("0.60"),
        trail_arm_r=Decimal("2.0"),
        trail_drawdown_bps=3000,
        max_hold_steps=24,
        ratchet_ladder=_BALANCED_RATCHET,
        default_exit_mode=MevExitMode.FAST,
    ),
    # Conservative: books gains earlier, tighter trail, tighter timeout.  Still
    # Secure.  De-risk-shaped: more of the position is taken off sooner.
    "secure_conservative": ExitConfig(
        label="secure_conservative",
        tp_ladder=(
            TpRung(trigger_r=Decimal("1.3"), fraction_bps=4000),
            TpRung(trigger_r=Decimal("2.0"), fraction_bps=3500),
            TpRung(trigger_r=Decimal("4.0"), fraction_bps=2000),
        ),
        hard_stop_r=Decimal("0.70"),  # -30% (tighter stop)
        trail_arm_r=Decimal("1.5"),
        trail_drawdown_bps=2000,  # 20% (tighter trail)
        max_hold_steps=16,
        # Conservative ratchet: breakeven earlier (at 1.5x), then lock profit fast.
        ratchet_ladder=(
            RatchetRung(at_r=Decimal("1.3"), lock_r=Decimal("0.95")),
            RatchetRung(at_r=Decimal("1.5"), lock_r=Decimal("1.0")),  # breakeven@1.5x
            RatchetRung(at_r=Decimal("2.0"), lock_r=Decimal("1.4")),
            RatchetRung(at_r=Decimal("4.0"), lock_r=Decimal("3.0")),
        ),
        default_exit_mode=MevExitMode.SECURE,
    ),
    # E12 — Secure balanced WITH the stale-narrative time-stop enabled.  Identical
    # trigger geometry to secure_balanced (so the A/B lift is unchanged on hot
    # paths), plus a SLOW-loop de-risk: a position flat (+/-15% of entry) for >= 6
    # event-time hours whose narrative score has cooled to <= 0.2 is force-exited.
    # De-risk only — this preset can only EXIT sooner than secure_balanced, never
    # hold longer or size up.
    "secure_balanced_timestop": ExitConfig(
        label="secure_balanced_timestop",
        tp_ladder=_BALANCED_LADDER,
        hard_stop_r=Decimal("0.60"),
        trail_arm_r=Decimal("2.0"),
        trail_drawdown_bps=3000,
        max_hold_steps=24,
        ratchet_ladder=_BALANCED_RATCHET,
        time_stop_hours=6,
        time_stop_flat_band_bps=1_500,  # +/-15% == flat
        time_stop_narrative_cooled_at=Decimal("0.2"),
        default_exit_mode=MevExitMode.SECURE,
    ),
    # Runner: lets winners breathe — smaller early take, wider trail, longer hold.
    # Still Secure, still de-risk-shaped (every branch only sheds risk).
    "secure_runner": ExitConfig(
        label="secure_runner",
        tp_ladder=(
            TpRung(trigger_r=Decimal("2.0"), fraction_bps=2000),
            TpRung(trigger_r=Decimal("5.0"), fraction_bps=2500),
            TpRung(trigger_r=Decimal("10.0"), fraction_bps=2500),
        ),
        hard_stop_r=Decimal("0.55"),  # -45%
        trail_arm_r=Decimal("3.0"),
        trail_drawdown_bps=3500,  # 35%
        max_hold_steps=30,
        # Runner ratchet: lets it breathe — breakeven only at 3x, then lock wide.
        ratchet_ladder=(
            RatchetRung(at_r=Decimal("2.0"), lock_r=Decimal("0.9")),
            RatchetRung(at_r=Decimal("3.0"), lock_r=Decimal("1.0")),  # breakeven@3x
            RatchetRung(at_r=Decimal("5.0"), lock_r=Decimal("2.5")),
            RatchetRung(at_r=Decimal("10.0"), lock_r=Decimal("6.0")),
        ),
        default_exit_mode=MevExitMode.SECURE,
    ),
}

# The DEFAULT preset — Secure (OQ-008, AUTONOMY-DIRECTIVE: most conservative
# default).  Used when no preset is named.
DEFAULT_PRESET_LABEL = "secure_balanced"


def preset(label: str | None = None) -> ExitConfig:
    """Look up an auto-strat preset by label.  Defaults to the Secure balanced
    strategy (OQ-008).  An unknown label raises (fail-closed — no silent fallback
    to a riskier config)."""
    if label is None:
        label = DEFAULT_PRESET_LABEL
    if label not in AUTO_STRAT_PRESETS:
        raise KeyError(
            f"unknown auto-strat preset {label!r}; known presets: "
            + ", ".join(sorted(AUTO_STRAT_PRESETS))
        )
    return AUTO_STRAT_PRESETS[label]


# --------------------------------------------------------------------------- #
# Per-position lifecycle state (the STATEFUL part the rule_engine lacks).
# Immutable: every transition returns a NEW state.  This makes the walk a pure
# fold over the price path — identical live and in backtest (point-in-time).
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ExitState:
    """Mutating-by-replacement lifecycle state for ONE open position.

    `remaining_bps` is the share of the ORIGINAL position still held (10000 =
    full, 0 = flat).  `fired_rungs` records which ladder indices already scaled
    out (a rung fires at most once).  `peak_r` is the running max realized return
    (the trailing floor ratchets up with it — TIGHTEN-ONLY).  `armed` is whether
    the trailing stop has been armed.  `steps_held` counts event-time steps for
    the timeout.

    There is NO field that can grow the position — `remaining_bps` only ever
    decreases.  A transition that would increase it is rejected.
    """

    mint: str
    entry_fill_price: Decimal
    stop: StopState  # the FIXED-at-entry T-321 hard stop (shared line)
    remaining_bps: int = _FULL_EXIT_BPS
    fired_rungs: frozenset[int] = field(default_factory=frozenset)
    peak_r: Decimal = Decimal(1)
    armed: bool = False
    steps_held: int = 0
    # PROFIT-LOCK RATCHET floor (multiple of entry).  Starts at 0 (no lock yet);
    # each ratchet rung the PEAK crosses raises it to that rung's lock_r.  It is
    # MONOTONE NON-DECREASING — a transition that lowers it is rejected in
    # `_replace` (the ratchet can only tighten, never widen).  The remainder is
    # force-exited when R falls to/below this floor (once it is > 0).
    locked_floor_r: Decimal = Decimal(0)
    # The indices of ratchet rungs already engaged (each arms at most once).
    fired_ratchet_rungs: frozenset[int] = field(default_factory=frozenset)
    # E12 — EVENT-TIME anchors for the time-stop / stale-narrative branch.  Both are
    # on-chain block_time_ms (the authoritative clock, EventTime §C-5) — NEVER
    # wall-clock.  `last_active_block_time_ms` is the event-time of the most recent
    # tick whose mark left the flat band (a real price move); "flat for N hours"
    # is measured as (now - last_active).  -1 == not yet anchored (set at first tick).
    entry_block_time_ms: int = -1
    last_active_block_time_ms: int = -1

    def __post_init__(self) -> None:
        if isinstance(self.entry_fill_price, float):
            raise TypeError("ExitState.entry_fill_price must be Decimal, not float.")
        if self.entry_fill_price <= 0:
            raise ValueError("ExitState.entry_fill_price must be > 0.")
        if isinstance(self.remaining_bps, bool | float):
            raise TypeError("ExitState.remaining_bps must be int (bps), not float/bool.")
        if not (0 <= self.remaining_bps <= _FULL_EXIT_BPS):
            raise ValueError("ExitState.remaining_bps must be in [0, 10000].")
        if self.stop.mint != self.mint:
            raise ValueError(
                f"ExitState.stop.mint ({self.stop.mint!r}) must match mint ({self.mint!r})."
            )
        if self.steps_held < 0:
            raise ValueError("ExitState.steps_held must be >= 0.")
        if isinstance(self.locked_floor_r, float):
            raise TypeError("ExitState.locked_floor_r must be Decimal, not float.")
        if self.locked_floor_r < 0:
            raise ValueError("ExitState.locked_floor_r must be >= 0 (0 = no lock yet).")
        for name in ("entry_block_time_ms", "last_active_block_time_ms"):
            v = getattr(self, name)
            if isinstance(v, bool | float):
                raise TypeError(f"ExitState.{name} must be int (event-time ms), not float/bool.")
            if v < -1:
                raise ValueError(f"ExitState.{name} must be >= -1 (event-time ms; -1 = unset).")

    @classmethod
    def at_entry(
        cls,
        *,
        mint: str,
        entry_fill_price: Decimal | int | str,
        config: ExitConfig,
    ) -> ExitState:
        """Construct the lifecycle state at entry, deriving the FIXED hard stop
        from the config so the ladder and the survivable stop share one line."""
        entry = _to_decimal_price(entry_fill_price, "entry_fill_price")
        stop = StopState.at_entry(
            mint=mint,
            entry_fill_price=entry,
            thresholds=config.stop_thresholds(),
        )
        return cls(mint=mint, entry_fill_price=entry, stop=stop)

    def realized_r(self, mark_price: Decimal | int | str) -> Decimal:
        """R = mark / entry_fill_price — the realized return on the actual fill."""
        mark = _to_decimal_price(mark_price, "mark_price")
        return mark / self.entry_fill_price

    def _replace(self, **changes: object) -> ExitState:
        # Defense-in-depth: a replacement may NEVER increase remaining_bps.
        new_remaining = changes.get("remaining_bps", self.remaining_bps)
        if isinstance(new_remaining, int) and new_remaining > self.remaining_bps:
            raise ValueError(
                "ExitState transition refused: remaining_bps may only DECREASE "
                f"(de-risk only); {self.remaining_bps} -> {new_remaining}."
            )
        # Defense-in-depth: the ratchet floor may NEVER decrease (tighten-only).
        new_floor = changes.get("locked_floor_r", self.locked_floor_r)
        if isinstance(new_floor, Decimal) and new_floor < self.locked_floor_r:
            raise ValueError(
                "ExitState transition refused: locked_floor_r may only RISE "
                f"(ratchet tighten-only, never widen); {self.locked_floor_r} -> {new_floor}."
            )
        merged: dict[str, object] = {
            "mint": self.mint,
            "entry_fill_price": self.entry_fill_price,
            "stop": self.stop,
            "remaining_bps": self.remaining_bps,
            "fired_rungs": self.fired_rungs,
            "peak_r": self.peak_r,
            "armed": self.armed,
            "steps_held": self.steps_held,
            "locked_floor_r": self.locked_floor_r,
            "fired_ratchet_rungs": self.fired_ratchet_rungs,
            "entry_block_time_ms": self.entry_block_time_ms,
            "last_active_block_time_ms": self.last_active_block_time_ms,
        }
        merged.update(changes)
        return ExitState(**merged)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Exit decision — the de-risk Intent (or no-op) the engine emits for one tick.
# --------------------------------------------------------------------------- #


class ExitReason(StrEnum):
    """Why an exit fired (audit / telemetry).  ALL de-risk."""

    NONE = "none"  # hold
    NARRATIVE_FAILURE = "narrative_failure"  # (iii) LLM catastrophic exit (highest)
    INSIDER_DUMP = "insider_dump"  # (iii') insider/dev-sell dump — catastrophic exit
    SELLABILITY_DEGRADED = "sellability_degraded"  # (iii'') delayed-honeypot/tax-flip re-probe
    HARD_STOP = "hard_stop"  # (i)  fixed-at-entry stop breach
    RATCHET_STOP = "ratchet_stop"  # profit-lock ratchet floor breach (de-risk)
    TRAILING_STOP = "trailing_stop"  # trailing floor breach (de-risk, full remainder)
    TIMEOUT = "timeout"  # max-hold reached — force-exit remainder
    # E12 — flat N event-time hours AND SLOW-loop narrative cooled — force-exit
    # the remainder.  De-risk only; SLOW-loop signal, never on the snipe hot path.
    STALE_NARRATIVE_TIME_STOP = "stale_narrative_time_stop"
    TAKE_PROFIT = "take_profit"  # (ii) TP ladder scale-out (only gain branch)


@dataclass(frozen=True)
class ExitDecision:
    """The engine's decision for ONE tick, plus the resulting state.

    `intent` is None for a hold; otherwise an ExitIntent (full flatten of the
    remainder) or a ReduceIntent (partial scale-out).  NEVER an EntryIntent.
    `next_state` is the lifecycle state AFTER applying this decision (the caller
    threads it to the next tick).  `reason` records which branch fired.
    """

    reason: ExitReason
    intent: ExitIntent | ReduceIntent | None
    next_state: ExitState
    fraction_bps: int  # share of REMAINING shed this tick (0 = hold)
    exit_mode: MevExitMode | None  # routing of the emitted intent (None on hold)

    def is_exit(self) -> bool:
        return self.intent is not None

    def is_full_exit(self) -> bool:
        return isinstance(self.intent, ExitIntent)


# --------------------------------------------------------------------------- #
# The ExitEngine — stateful, deterministic, de-risk-only.
# --------------------------------------------------------------------------- #


class ExitEngine:
    """Productionized TP-ladder + trailing + hard-stop + timeout exit engine.

    USAGE (the orchestrator drives this on the FAST loop):

        engine = ExitEngine(config=preset("secure_balanced"))
        state  = ExitState.at_entry(mint="M", entry_fill_price=Decimal("100"),
                                    config=engine.config)
        # each event-time tick:
        decision = engine.on_tick(state, mark_price=mark, block_time_ms=bt,
                                  narrative_failure_flag=flag)
        state = decision.next_state
        if decision.intent is not None:
            venue.exit(decision.intent, position)   # behind the DRY_RUN gate

    The engine holds a DeRiskIntentFactory — it can ONLY build ExitIntent /
    ReduceIntent.  There is no method here that constructs an EntryIntent, sizes
    a trade, widens a stop, or resets a breaker.  Every branch sheds risk.

    PRIORITY (same as rule_engine, highest-first, short-circuit):
        (iii) narrative_failure       -> FULL exit of remainder (SECURE)     [highest]
        (iii')insider/dev-sell dump   -> FULL exit of remainder (SECURE) [rug-in-progress]
        (iii'')sellability degraded   -> FULL exit of remainder (SECURE) [delayed honeypot]
        (i)   hard stop breach        -> FULL exit of remainder (SECURE)
        (i'') profit-lock ratchet     -> FULL exit of remainder (SECURE) [locked floor]
        (i')  trailing breach         -> FULL exit of remainder (SECURE) [de-risk runner]
        (ii)  timeout                 -> FULL exit of remainder (SECURE)
        (E12) stale-narrative time-stop -> FULL exit of remainder (SECURE)
        (ii)  TP ladder rung          -> PARTIAL scale-out (config routing)  [lowest]

    The profit-lock RATCHET (i'') is the discrete stepped floor (breakeven at 2x,
    lock 2x at 3x, rungs below): once the PEAK crosses a rung the locked floor
    ratchets UP to a fixed multiple of entry and never falls.  It is enforced as
    the TIGHTER complement of the proportional trail — it can only ADD protection.

    The E12 stale-narrative time-stop sits below the unconditional max-hold timeout
    (which is a hard cap) and above the gain-booking TP ladder.  It is a SLOW-loop
    de-risk: a position flat for N EVENT-TIME hours whose narrative has cooled is a
    dead bag and is force-exited (SECURE).  Disabled unless the config opts in.

    The insider/dev-sell DUMP exit (iii', E14b) sits in the top catastrophic tier,
    just BELOW narrative_failure and ABOVE the mechanical hard stop: an on-chain
    creator/top-holder distribution past threshold is a rug-in-progress that must
    pre-empt the price-based stop (you want out on the dev-dump signal before the
    price fully collapses).  It reads a PRE-SET bool produced OFF the hot path by
    E14a — the FAST branch runs no detection, RPC, or LLM — and can ONLY force a
    full SECURE exit; it can never hold longer, widen/relax a stop, or size up.

    The DELAYED-HONEYPOT / TAX-FLIP sellability exit (iii'', E17) sits just BELOW
    the insider-dump signal and ABOVE the mechanical hard stop: a token that passed
    the pre-trade sellability gate at entry can be flipped into a honeypot afterward
    (freeze-authority abuse, transfer-fee/tax spike, LP pull, account-close trap).
    The SLOW-loop re-probe re-runs the SHARED sell-sim on open positions and, on a
    degraded/inconclusive result, pre-sets the bool this branch reads.  Like the
    other catastrophic flags the FAST branch runs no sell-sim/RPC/detection and can
    ONLY force a full SECURE exit — exit while an exit is still possible.

    A forced/defensive exit (narrative / insider-dump / hard-stop / trailing /
    timeout) ALWAYS routes SECURE (private) so a defensive exit is never
    sandwiched.  Only the gain-booking TP scale-out uses the config's routing.
    """

    def __init__(self, *, config: ExitConfig, factory: DeRiskIntentFactory | None = None) -> None:
        self.config = config
        self._factory = factory or DeRiskIntentFactory()

    # -- the FAST-loop hot call ------------------------------------------- #

    def on_tick(
        self,
        state: ExitState,
        *,
        mark_price: Decimal | int | str,
        block_time_ms: int,
        narrative_failure_flag: bool = False,
        insider_dump_flag: bool = False,
        sellability_degraded_flag: bool = False,
        narrative_score: Decimal | int | str | None = None,
    ) -> ExitDecision:
        """Evaluate the ordered hierarchy for one open position at one tick.

        PURE except for the deterministic factory call — no IO, no LLM, no
        wall-clock.  `block_time_ms` is EVENT-TIME — used to build a deterministic
        client_intent_id AND (E12) to measure how long the position has been flat
        (block_time_ms deltas only; never compared to wall-clock).  Returns an
        ExitDecision carrying the de-risk Intent (or None) and the next state.

        `narrative_score` (E12) is a PRE-COMPUTED SLOW-loop point-in-time value in
        [0, 1] (higher == hotter story), passed in by the SLOW reasoner — the FAST
        loop NEVER computes it or awaits an LLM here.  None == "no narrative read
        this tick"; the time-stop then cannot fire (fail-safe: a missing cool
        signal never forces an exit, it just holds).  The score is used ONLY to
        DE-RISK (force a stale-bag exit); it can never size up or extend a hold.

        `insider_dump_flag` (E14b) is a PRE-SET boolean produced OFF the hot path by
        E14a's on-chain insider/dev-sell distribution detector
        (`aats.ingestion.insider_dump`), read from the shared StateStore by the
        caller (`StateStore.get_insider_dump_flag(mint)`) exactly as
        `narrative_failure_flag` is.  The FAST branch only READS the bool — it runs
        NO detection, RPC, or LLM here.  Like narrative_failure it can ONLY force a
        full SECURE exit (de-risk); there is no path by which it holds longer,
        widens/relaxes a stop, or sizes up.

        `sellability_degraded_flag` (E17) is a PRE-SET boolean produced OFF the hot
        path by the SLOW-loop sellability RE-PROBE
        (`aats.risk.sellability_reprobe.SellabilityReprober`), which periodically
        RE-RUNS the SHARED sell-sim (`aats.execution.sell_sim`) on OPEN positions
        and, on a DEGRADED (sim-revert / zero-output / high sell-tax / account-close
        trap) or INCONCLUSIVE (refuse-by-default => degraded) result, writes
        `StateStore.set_sellability_degraded_flag`.  The caller reads
        `StateStore.get_sellability_degraded_flag(mint)` — a bare bool — exactly like
        the two flags above.  The FAST branch only READS the bool; it runs NO
        sell-sim, RPC, or detection here.  A delayed honeypot / tax-flip means the
        token may have become unsellable AFTER entry: like the other catastrophic
        flags it can ONLY force a full SECURE exit (exit while an exit is still
        possible) — never hold longer, widen/relax a stop, or size up.
        """
        if isinstance(block_time_ms, bool | float):
            raise TypeError("block_time_ms must be int (event-time ms), not float/bool.")
        if block_time_ms < 0:
            raise ValueError("block_time_ms must be non-negative (event-time).")
        if not isinstance(narrative_failure_flag, bool):
            raise TypeError("narrative_failure_flag must be a plain bool (a pre-set de-risk flag).")
        if not isinstance(insider_dump_flag, bool):
            raise TypeError("insider_dump_flag must be a plain bool (a pre-set de-risk flag).")
        if not isinstance(sellability_degraded_flag, bool):
            raise TypeError(
                "sellability_degraded_flag must be a plain bool (a pre-set de-risk flag)."
            )
        narr_score = self._coerce_narrative_score(narrative_score)

        mark = _to_decimal_price(mark_price, "mark_price")

        # Already flat — nothing to do (idempotent: a closed position holds).
        if state.remaining_bps <= 0:
            return self._hold(state)

        r = state.realized_r(mark)
        # Update the running peak (the trailing floor ratchets UP with it; this is
        # the TIGHTEN-ONLY mechanism — the floor never falls).
        new_peak = r if r > state.peak_r else state.peak_r
        # Advance the held-step counter (event-time step; timeout uses it).
        stepped = state._replace(peak_r=new_peak, steps_held=state.steps_held + 1)
        # Arm the trailing stop once R crosses trail_arm_r (one-directional: once
        # armed it stays armed — disarming would WIDEN protection, forbidden).
        if not stepped.armed and r >= self.config.trail_arm_r:
            stepped = stepped._replace(armed=True)

        # PROFIT-LOCK RATCHET — advance the locked floor as the PEAK crosses rungs.
        # This RAISES locked_floor_r to the highest engaged rung's lock_r (monotone
        # by construction: rungs ascend in lock_r and _replace forbids a fall).  It
        # uses peak_r (not the live mark) so a wick down after a rung crossed never
        # un-locks the floor — once you've run, the profit stays locked.
        stepped = self._advance_ratchet(stepped)

        # E12 — maintain the EVENT-TIME flat clock.  Anchor entry/last-active on the
        # first tick; thereafter, a mark that LEAVES the flat band is a real move
        # that resets last_active (so "flat for N hours" measures only quiet runs).
        stepped = self._advance_flat_clock(stepped, r, block_time_ms)

        # (iii) HIGHEST: LLM narrative-failure catastrophic exit.  Reads a PRE-SET
        # flag — NEVER calls the LLM.  Full flatten of remainder, short-circuit.
        if narrative_failure_flag:
            return self._full_exit(
                stepped,
                block_time_ms=block_time_ms,
                reason=ExitReason.NARRATIVE_FAILURE,
                detail="SLOW-loop reasoner forced a catastrophic exit "
                "(manufactured/abandoned sentiment)",
            )

        # (iii') INSIDER / DEV-SELL DUMP catastrophic exit (E14b).  Reads a PRE-SET
        # flag produced OFF the hot path by E14a (`aats.ingestion.insider_dump` ->
        # `StateStore.set_insider_dump_flag`); the FAST branch NEVER runs detection,
        # an RPC, or an LLM here — it reads a plain bool exactly like the narrative
        # flag above.  A creator/top-holder-cluster wallet dumping its held-supply
        # baseline past the configured threshold is a rug-in-progress: force a FULL
        # flatten of the remainder (SECURE) and short-circuit.  Placed just below
        # narrative_failure and ABOVE the mechanical hard stop so a dev-dump exits
        # ahead of the price collapse.  De-risk only — like narrative_failure it can
        # ONLY force an exit; it can never hold longer, widen/relax a stop, or size up.
        if insider_dump_flag:
            return self._full_exit(
                stepped,
                block_time_ms=block_time_ms,
                reason=ExitReason.INSIDER_DUMP,
                detail="insider/dev-sell dump flag pre-set off-hot-path "
                "(creator/top-holder distribution crossed threshold); rug-in-progress",
            )

        # (iii'') DELAYED-HONEYPOT / TAX-FLIP SELLABILITY DEGRADATION (E17).  Reads
        # a PRE-SET flag produced OFF the hot path by the SLOW-loop sellability
        # re-probe (`aats.risk.sellability_reprobe` -> `StateStore.
        # set_sellability_degraded_flag`), which periodically RE-RUNS the SHARED
        # sell-sim on open positions; the FAST branch NEVER runs a sell-sim, an RPC,
        # or detection here — it reads a plain bool exactly like the two flags above.
        # A degraded re-probe (sim-revert / zero-output / high sell-tax / account-
        # close trap) OR an inconclusive one (refuse-by-default => degraded) means
        # the token may have flipped into a honeypot AFTER entry: force a FULL
        # flatten of the remainder (SECURE) while an exit is still possible, and
        # short-circuit.  Placed in the catastrophic tier just below the insider-dump
        # rug-in-progress signal and ABOVE the mechanical hard stop (a freshly-locked
        # honeypot must be exited on the sellability signal, not after the price
        # collapse).  De-risk only — like the flags above it can ONLY force an exit;
        # it can never hold longer, widen/relax a stop, or size up.
        if sellability_degraded_flag:
            return self._full_exit(
                stepped,
                block_time_ms=block_time_ms,
                reason=ExitReason.SELLABILITY_DEGRADED,
                detail="sellability_degraded flag pre-set off-hot-path (SLOW-loop "
                "sell-sim re-probe: delayed honeypot / tax-flip / inconclusive-refuse); "
                "exit while an exit is still possible",
            )

        # (i) MANDATORY HARD STOP — the FIXED-at-entry T-321 trigger.  We reuse the
        # StopState breach test so the ladder and the survivable stop NEVER
        # disagree on the line.
        if stepped.stop.is_breached(mark):
            return self._full_exit(
                stepped,
                block_time_ms=block_time_ms,
                reason=ExitReason.HARD_STOP,
                detail=f"mark ({mark}) <= fixed hard-stop trigger ({stepped.stop.trigger_price})",
            )

        # (i'') PROFIT-LOCK RATCHET STOP — de-risk.  Once a rung has engaged, the
        # locked floor is a FIXED multiple of entry that only ever rises.  If R has
        # fallen to/below the locked floor, force-exit the remainder (SECURE).  This
        # sits just above the proportional trail and is the TIGHTER of the two: at
        # 3x peak the ratchet locks 2.0x where the 30% trail would only protect 2.1x
        # — but after a give-back to (say) 2.05x the trail would already have fired;
        # the ratchet's job is to GUARANTEE the discrete floor regardless of the
        # proportional band.  Fires independently of `armed` (a locked floor is a
        # commitment that stands on its own).
        if stepped.locked_floor_r > 0 and r <= stepped.locked_floor_r:
            return self._full_exit(
                stepped,
                block_time_ms=block_time_ms,
                reason=ExitReason.RATCHET_STOP,
                detail=(
                    f"R ({r}) <= locked profit floor ({stepped.locked_floor_r}x of entry); "
                    "profit-lock ratchet force-exited the remainder"
                ),
            )

        # (i') TRAILING STOP on the runner remainder — de-risk.  Only after armed.
        # Floor = peakR * (1 - trail_drawdown).  TIGHTENS with peak, never widens.
        if stepped.armed:
            trail_floor_r = (
                stepped.peak_r * (_BPS_DENOM - self.config.trail_drawdown_bps) / _BPS_DENOM
            )
            if r <= trail_floor_r:
                return self._full_exit(
                    stepped,
                    block_time_ms=block_time_ms,
                    reason=ExitReason.TRAILING_STOP,
                    detail=(
                        f"R ({r}) <= trailing floor ({trail_floor_r}) = peakR "
                        f"({stepped.peak_r}) * (1 - {self.config.trail_drawdown_bps}bps)"
                    ),
                )

        # (ii) TIMEOUT — force-exit the remainder once max_hold_steps reached.
        if stepped.steps_held >= self.config.max_hold_steps:
            return self._full_exit(
                stepped,
                block_time_ms=block_time_ms,
                reason=ExitReason.TIMEOUT,
                detail=f"max_hold_steps ({self.config.max_hold_steps}) reached",
            )

        # (E12) STALE-NARRATIVE TIME-STOP — SLOW-loop de-risk.  Force-exit the
        # remainder when the position has been FLAT for >= time_stop_hours of
        # EVENT-TIME *and* the SLOW-loop narrative score has cooled to/below the
        # configured threshold.  Both conditions are required (a flat-but-still-hot
        # bag holds; a cooled-but-moving bag holds).  Disabled when the config opts
        # out or no narrative read is supplied (fail-safe: never forces on missing
        # data).  Full SECURE exit — a stale defensive exit, never sandwiched.
        ts_detail = self._time_stop_detail(stepped, narr_score, block_time_ms)
        if ts_detail is not None:
            return self._full_exit(
                stepped,
                block_time_ms=block_time_ms,
                reason=ExitReason.STALE_NARRATIVE_TIME_STOP,
                detail=ts_detail,
            )

        # (ii) TP LADDER — the LOWEST-priority, only gain-booking branch.  Fire the
        # HIGHEST un-fired rung the mark has reached (a tick that jumps past several
        # rungs books the most advanced scale-out).  Still a partial REDUCE.
        hit_idx = self._highest_hit_rung(stepped, r)
        if hit_idx is not None:
            return self._scale_out(stepped, block_time_ms=block_time_ms, rung_idx=hit_idx)

        # Nothing fired — hold (with the updated peak / armed / step state).
        return self._hold(stepped)

    # -- rule helpers ------------------------------------------------------ #

    def _advance_ratchet(self, state: ExitState) -> ExitState:
        """Raise the locked profit floor to the highest ratchet rung the PEAK has
        crossed (pure; returns a new state).

        Uses `state.peak_r` (already updated this tick) so a rung that armed on an
        earlier high stays engaged even after a give-back — the lock never reverts.
        The new floor is the MAX of the current floor and every engaged rung's
        lock_r; since rungs ascend in lock_r and _replace forbids a decrease, the
        floor is monotone by construction.  No-op (returns the same state) when the
        ratchet is disabled or no new rung engaged.
        """
        ladder = self.config.ratchet_ladder
        if not ladder:
            return state
        new_floor = state.locked_floor_r
        engaged = set(state.fired_ratchet_rungs)
        for idx, rrung in enumerate(ladder):
            if state.peak_r >= rrung.at_r:
                engaged.add(idx)
                if rrung.lock_r > new_floor:
                    new_floor = rrung.lock_r
        if new_floor == state.locked_floor_r and engaged == set(state.fired_ratchet_rungs):
            return state  # nothing newly engaged
        return state._replace(
            locked_floor_r=new_floor,
            fired_ratchet_rungs=frozenset(engaged),
        )

    def _highest_hit_rung(self, state: ExitState, r: Decimal) -> int | None:
        """Index of the highest-trigger un-fired rung whose trigger R has been
        reached, or None.  Firing the highest hit rung means a single big tick
        books the most advanced scale-out (never re-firing a lower rung later)."""
        best: int | None = None
        best_trigger = Decimal(0)
        for idx, rung in enumerate(self.config.tp_ladder):
            if idx in state.fired_rungs:
                continue
            if r >= rung.trigger_r and rung.trigger_r > best_trigger:
                best = idx
                best_trigger = rung.trigger_r
        return best

    # -- E12: time-stop / stale-narrative helpers -------------------------- #

    @staticmethod
    def _coerce_narrative_score(value: Decimal | int | str | None) -> Decimal | None:
        """Coerce a SLOW-loop narrative score to Decimal, rejecting float, or None.

        None == no narrative read this tick (the time-stop then cannot fire — a
        missing cool signal never forces an exit).  A float is refused like all
        money-adjacent inputs.  Range is checked at use against the config band.
        """
        if value is None:
            return None
        if isinstance(value, bool):
            raise TypeError("narrative_score must be Decimal/int/str or None, got bool.")
        if isinstance(value, float):
            raise TypeError(
                "narrative_score must be Decimal/int/str or None (exact), got float. "
                "Pass an exact normalized score (data-models.md §0)."
            )
        if isinstance(value, Decimal):
            score = value
        elif isinstance(value, int | str):
            score = Decimal(value)
        else:
            raise TypeError(
                f"narrative_score must be Decimal/int/str or None, got {type(value).__name__}."
            )
        if not (Decimal(0) <= score <= Decimal(1)):
            raise ValueError(f"narrative_score must be a normalized value in [0, 1]; got {score}.")
        return score

    def _advance_flat_clock(self, state: ExitState, r: Decimal, block_time_ms: int) -> ExitState:
        """Maintain the EVENT-TIME flat clock (pure; returns a new state).

        On the FIRST tick of the position's life, anchor entry/last-active to this
        block_time_ms.  Thereafter, if the mark has LEFT the flat band (a real move
        in either direction), reset last_active to now — only quiet, in-band runs
        accumulate flat time.  This is the ONLY place the flat anchors move, and it
        moves them by event-time (block_time_ms), never wall-clock.
        """
        # First tick — anchor both clocks (no prior event-time to compare to).
        if state.entry_block_time_ms < 0:
            return state._replace(
                entry_block_time_ms=block_time_ms,
                last_active_block_time_ms=block_time_ms,
            )
        # |R - 1| in bps; outside the flat band == a real price move (resets clock).
        deviation_bps = abs(r - Decimal(1)) * _BPS_DENOM
        if deviation_bps > Decimal(self.config.time_stop_flat_band_bps):
            return state._replace(last_active_block_time_ms=block_time_ms)
        # In-band (flat) — the flat clock keeps running (last_active unchanged).
        return state

    def _time_stop_detail(
        self, state: ExitState, narrative_score: Decimal | None, block_time_ms: int
    ) -> str | None:
        """Return a detail string when the E12 time-stop should fire, else None.

        Fires only when ALL hold: the branch is enabled (time_stop_hours set), a
        narrative score was supplied AND has cooled to/below the threshold, and the
        position has been FLAT for >= time_stop_hours of EVENT-TIME.  Pure: a
        function of (config, state, block_time_ms, narrative_score) only.
        """
        hours = self.config.time_stop_hours
        if hours is None:  # branch disabled — fail-safe default
            return None
        if narrative_score is None:  # no SLOW-loop read => never force an exit
            return None
        if narrative_score > self.config.time_stop_narrative_cooled_at:
            return None  # story still warm — hold
        if state.last_active_block_time_ms < 0:  # not yet anchored
            return None
        flat_ms = block_time_ms - state.last_active_block_time_ms
        if flat_ms < 0:  # out-of-order event-time tick — never fire on a negative span
            return None
        horizon_ms = hours * 3_600_000  # event-time hours -> ms (exact integer)
        if flat_ms < horizon_ms:
            return None
        flat_hours = Decimal(flat_ms) / Decimal(3_600_000)
        return (
            f"flat {flat_hours}h (>= {hours}h) within +/-{self.config.time_stop_flat_band_bps}bps "
            f"of entry AND narrative cooled (score {narrative_score} <= "
            f"{self.config.time_stop_narrative_cooled_at}); stale bag force-exited"
        )

    # -- de-risk Intent construction (factory has NO EntryIntent path) ----- #

    def _hold(self, state: ExitState) -> ExitDecision:
        return ExitDecision(
            reason=ExitReason.NONE,
            intent=None,
            next_state=state,
            fraction_bps=0,
            exit_mode=None,
        )

    def _full_exit(
        self,
        state: ExitState,
        *,
        block_time_ms: int,
        reason: ExitReason,
        detail: str,
    ) -> ExitDecision:
        """Full flatten of the REMAINING position (de-risk).  Forced/defensive
        exits ALWAYS route SECURE (private) — never sandwiched on the way out."""
        client_intent_id = f"exit_eng:{reason.value}:{state.mint}:{block_time_ms}"
        intent: ExitIntent = self._factory.exit(
            client_intent_id=client_intent_id,
            mint=state.mint,
            fraction_bps=_FULL_EXIT_BPS,  # 100% of REMAINING
            exit_mode=MevExitMode.SECURE.value,  # defensive exits are always Secure
            reason=f"{reason.value}: {detail}",
        )
        next_state = state._replace(remaining_bps=0)
        return ExitDecision(
            reason=reason,
            intent=intent,
            next_state=next_state,
            fraction_bps=_FULL_EXIT_BPS,
            exit_mode=MevExitMode.SECURE,
        )

    def _scale_out(self, state: ExitState, *, block_time_ms: int, rung_idx: int) -> ExitDecision:
        """Partial REDUCE (TP scale-out) — the only gain branch.

        The rung's fraction is a share of the ORIGINAL position; we clamp it to
        the remaining share so a late ladder never tries to sell more than is
        held.  A ReduceIntent of < 100% of remaining keeps the runner; a rung that
        would consume the whole remainder is still emitted as a partial-by-rung,
        but the ReduceIntent fraction is bounded to the remaining and never grows.
        """
        rung = self.config.tp_ladder[rung_idx]
        # Fraction of REMAINING to sell, clamped so we never oversell.
        frac_of_remaining = min(rung.fraction_bps, state.remaining_bps)
        # Express the reduce as bps OF REMAINING (the ReduceIntent contract is
        # fraction of the remaining position).
        reduce_bps = frac_of_remaining
        mode = self.config.default_exit_mode
        client_intent_id = f"exit_eng:tp:{state.mint}:{block_time_ms}:{rung.trigger_r}"
        reason = (
            f"take_profit: rung@{rung.trigger_r}x hit; scaling out {reduce_bps} bps of remaining "
            f"({mode.value} routing)"
        )

        # New remaining (of the ORIGINAL position).  remaining shrinks by the
        # same bps we just sold relative to the original — exactly, in integers.
        sold_of_original = int(state.remaining_bps * reduce_bps / _FULL_EXIT_BPS)
        new_remaining = state.remaining_bps - sold_of_original
        next_state = state._replace(
            remaining_bps=new_remaining,
            fired_rungs=state.fired_rungs | {rung_idx},
        )

        # If this scale-out happened to consume the entire remainder, emit a full
        # ExitIntent instead of a ReduceIntent (the contract forbids a 100%
        # ReduceIntent path being confused with an add; a full flatten is an EXIT).
        if new_remaining <= 0:
            intent: ExitIntent = self._factory.exit(
                client_intent_id=client_intent_id,
                mint=state.mint,
                fraction_bps=_FULL_EXIT_BPS,
                exit_mode=mode.value,
                reason=reason + " [consumed remainder -> full exit]",
            )
            return ExitDecision(
                reason=ExitReason.TAKE_PROFIT,
                intent=intent,
                next_state=state._replace(
                    remaining_bps=0, fired_rungs=state.fired_rungs | {rung_idx}
                ),
                fraction_bps=_FULL_EXIT_BPS,
                exit_mode=mode,
            )

        reduce_intent: ReduceIntent = self._factory.reduce(
            client_intent_id=client_intent_id,
            mint=state.mint,
            fraction_bps=reduce_bps,
            reason=reason,
        )
        return ExitDecision(
            reason=ExitReason.TAKE_PROFIT,
            intent=reduce_intent,
            next_state=next_state,
            fraction_bps=reduce_bps,
            exit_mode=mode,
        )
