"""E13 — Anti-FOMO / already-pumped EXCLUSION filter (de-risk / selectivity only).

THE FOMO BRAKE.  A SLOW-LOOP, point-in-time, REJECT-or-DOWN-WEIGHT-ONLY filter that
EXCLUDES (or, sub-threshold, DOWN-WEIGHTS the conviction of) a candidate that has
ALREADY RUN — and treats a mainstream / CEX-listing / Forbes-tier mention as an
EXCLUSION signal, never a buy signal.  It surfaces structured CANDIDATE REJECT
REASONS for the operator dashboard and the SLOW-loop reasoner.  It is the
contrarian counterweight to hype: "everyone is talking about it / it already 10x'd"
is exactly when this firm does NOT chase.

WHAT THIS MODULE IS (and is emphatically NOT)
---------------------------------------------
  * It is a SLOW-LOOP filter.  Price/pump history and mainstream-mention facts are
    seconds-to-minutes-stale aggregates; using them on the snipe hot path would be a
    correctness AND a latency violation.  This filter runs on the SLOW loop only,
    against an event-time anchor — never inline on the FAST/snipe path.
  * It is SELECTIVITY / DE-RISK ONLY.  Every branch can ONLY:
      - EXCLUDE a candidate (REJECT — it already ran past the configured ceiling, or
        a hard-exclusion mainstream/CEX/Forbes mention is present), or
      - DOWN-WEIGHT it (a recorded conviction haircut in [0, 1], applied as a
        MULTIPLIER <= 1 — it can only SHRINK conviction, never grow it), or
      - leave it untouched (no opinion).
    There is NO code path here that constructs an entry, sizes a trade, sets/widens a
    stop, lifts a cap, or emits a "buy".  A clean pass is SILENT: it means "not
    excluded / not de-rated by the FOMO filter" — never "buy".  The conviction
    multiplier is hard-clamped to <= 1, so this filter is STRUCTURALLY incapable of
    raising conviction (and thus size).
  * It is ADVERSARIAL-BY-DEFAULT / CONTRARIAN.  A mainstream-media / CEX-listing /
    Forbes-tier mention LOWERS conviction (or excludes) — it is treated as
    late-cycle distribution risk, NOT as validation.  A big recent pump LOWERS
    conviction — buying after the move is buying the bag.  Neither is ever a trigger.
  * It is REFUSE-BY-DEFAULT on missing data ONLY where absence is suspicious.  We do
    NOT reject merely because pump history is absent (a brand-new token legitimately
    has no run-up — that is the snipe niche, handled by other gates).  Absence of a
    pump signal is "no FOMO opinion", not an exclusion.  But a STALE bundle (older
    than the freshness budget) is EXCLUDED refuse-by-default: we will not pass a
    "looks un-pumped" verdict computed on stale price history.

THE TWO EXCLUSION FAMILIES
--------------------------
  1. ALREADY-PUMPED (price run-up).  Computed point-in-time from a price/pump-history
     window: the ratio of the current price to the window's anchor (e.g. the window
     low, or an open).  If that run-up >= a configurable hard ceiling (default 300%,
     i.e. a 4x = ratio 4.0), the candidate is REJECTED (FOMO_ALREADY_PUMPED).  In a
     softer band below the hard ceiling but above a "warm" floor, the candidate is
     DOWN-WEIGHTED (FOMO_PUMP_DOWNWEIGHT) — conviction is haircut proportionally, the
     candidate is not killed.  TIGHTENING (lowering the ceiling) only EXCLUDES MORE.
  2. MAINSTREAM / CEX-LISTING / FORBES-TIER MENTION.  A point-in-time set of mention
     tiers attached to the candidate.  A tier in the HARD-EXCLUSION set (e.g. a major
     CEX listing announcement, a Forbes/Bloomberg/mainstream-press feature) is an
     instant REJECT (FOMO_MAINSTREAM_MENTION).  A tier in the SOFT set (e.g. a large
     generalist crypto-influencer shout) is a DOWN-WEIGHT.  This inverts the naive
     reading: a Forbes headline is a SELL-into-retail signal here, never a buy.

ASYMMETRIC TRUST / NO STANDALONE BUY
------------------------------------
  Social / news / screener / caller signals are SLOW-LOOP ONLY and may only de-risk.
  This filter is exactly such a signal: it can REJECT a candidate, DOWN-WEIGHT it, or
  pass it through UNCHANGED to the EXISTING safety gate + classifier + asymmetric-trust
  pipeline — it can never, by itself, cause a buy, and its conviction multiplier is
  clamped to <= 1 so it can never size anything up.

POINT-IN-TIME (C-5) — NO COMPUTE-TIME LEAK
------------------------------------------
  The filter reads ONLY as-of values carried on its inputs and the event-time anchor
  (``event_slot`` / ``event_block_time_ms``).  The price/pump window is a list of
  as-of samples whose slots are <= the anchor; there is no field that admits a later
  slot, so a lookahead leak is not expressible.  It never reads wall-clock.  The
  staleness budget is enforced against the recorded ``data_staleness_ms``; over budget
  => EXCLUDE (refuse-by-default).  The SAME code path runs in live and in backtest.

MONEY (data-models.md §0)
-------------------------
  Price-history samples are integer LAMPORTS (the money rule: prices are int lamports,
  not float).  The pump RATIO is a pure dimensionless ``Decimal`` formed by exact
  integer division coerced to Decimal — no float, no binary drift.  This filter does
  NO trade sizing; it emits a conviction MULTIPLIER (a Decimal in [0, 1]) that a
  downstream consumer may apply to ALREADY-CAPPED size — it never produces a size.

CAPITAL SAFETY
--------------
  This module touches NO capital, NO signer, NO RPC.  It is pure over already-fetched
  metadata.  DRY-RUN / mainnet hard-gate are unaffected — a pass never enables a buy,
  so it cannot lift any safety primitive (breaker / survivable-stop / DMS are
  untouched and still fire).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Mention tiers — the mainstream / CEX / Forbes-tier exclusion vocabulary.
# A tier is a STRING label attached to a candidate (sourced SLOW-loop from the
# news / sentiment layer).  Tier membership is the whole decision; we never read
# the price implied by a mention.  HARD tiers REJECT; SOFT tiers DOWN-WEIGHT.
# ---------------------------------------------------------------------------


class MentionTier(StrEnum):
    """Mainstream-attention tiers, ordered roughly by 'how late-cycle is this'.

    These are EXCLUSION / DE-RISK signals.  A mainstream or CEX-listing mention means
    the move is likely already distributed to retail — this firm does NOT chase it.

    HARD (instant REJECT):
      * MAINSTREAM_PRESS — Forbes / Bloomberg / WSJ / mainstream-press feature.  By
        the time a meme-coin reaches Forbes, the alpha is gone and retail is the exit
        liquidity.  Hard exclusion.
      * MAJOR_CEX_LISTING — a top-tier CEX (Coinbase/Binance-class) listing
        announcement.  Famous "sell the listing" dynamic; hard exclusion for a sniper.

    SOFT (DOWN-WEIGHT conviction, do not kill):
      * MINOR_CEX_LISTING — a smaller/second-tier exchange listing.  Less terminal but
        still a distribution signal; haircut conviction.
      * MAINSTREAM_INFLUENCER — a large generalist (non-crypto-native) influencer shout.
        Coordinated/late hype; haircut conviction.
    """

    # -- hard exclusion tiers (REJECT) --
    MAINSTREAM_PRESS = "mainstream_press"
    MAJOR_CEX_LISTING = "major_cex_listing"
    # -- soft tiers (DOWN-WEIGHT) --
    MINOR_CEX_LISTING = "minor_cex_listing"
    MAINSTREAM_INFLUENCER = "mainstream_influencer"


# Tiers that, when present, mean the candidate is hard-EXCLUDED (REJECT).
_HARD_MENTION_TIERS: frozenset[MentionTier] = frozenset(
    {MentionTier.MAINSTREAM_PRESS, MentionTier.MAJOR_CEX_LISTING}
)
# Tiers that DOWN-WEIGHT (haircut conviction) but do not reject on their own.
_SOFT_MENTION_TIERS: frozenset[MentionTier] = frozenset(
    {MentionTier.MINOR_CEX_LISTING, MentionTier.MAINSTREAM_INFLUENCER}
)


def parse_mention_tier(raw: str) -> MentionTier | None:
    """Coerce a raw mention-tier string to a ``MentionTier``; unknown => None.

    Unknown tiers are IGNORED (no opinion) rather than treated as an exclusion — a
    typo in an upstream label must not silently widen OR drop the FOMO filter.  We log
    a warning so a mislabelled source is visible to the operator.
    """
    s = raw.strip().lower()
    try:
        return MentionTier(s)
    except ValueError:
        logger.warning("anti_fomo: ignoring unknown mention tier %r", raw)
        return None


# ---------------------------------------------------------------------------
# Reason taxonomy — the dashboard / reasoner-facing vocabulary.  Stable codes.
# ---------------------------------------------------------------------------


class FomoReason(StrEnum):
    """Stable anti-FOMO reason codes (dashboard + SLOW-loop reasoner contract).

    EXCLUSION reasons (a hit => candidate is REJECTED):
      * FOMO_ALREADY_PUMPED — recent run-up >= the hard ceiling (default >=300%).
      * FOMO_MAINSTREAM_MENTION — a HARD mention tier (mainstream press / major CEX).
      * INPUT_STALE — pump/mention bundle older than the freshness budget
        (refuse-by-default; we will not pass a verdict on stale price history).

    DOWN-WEIGHT reasons (a hit => conviction is HAIRCUT, candidate NOT killed):
      * FOMO_PUMP_DOWNWEIGHT — run-up in the warm band (>= warm floor, < hard ceiling).
      * FOMO_SOFT_MENTION — a SOFT mention tier (minor CEX / mainstream influencer).

    INFO reason (recorded; never excludes or down-weights on its own):
      * NO_PUMP_HISTORY — there was no usable price window (e.g. brand-new token).
        Absence of a run-up is NOT a buy and NOT an exclusion; it is "no FOMO opinion".
    """

    # -- exclusion reasons (REJECT) --
    FOMO_ALREADY_PUMPED = "fomo_already_pumped"
    FOMO_MAINSTREAM_MENTION = "fomo_mainstream_mention"
    INPUT_STALE = "input_stale"
    # -- down-weight reasons (haircut conviction) --
    FOMO_PUMP_DOWNWEIGHT = "fomo_pump_downweight"
    FOMO_SOFT_MENTION = "fomo_soft_mention"
    # -- info reasons --
    NO_PUMP_HISTORY = "no_pump_history"


_EXCLUSION_REASONS: frozenset[FomoReason] = frozenset(
    {
        FomoReason.FOMO_ALREADY_PUMPED,
        FomoReason.FOMO_MAINSTREAM_MENTION,
        FomoReason.INPUT_STALE,
    }
)
_DOWNWEIGHT_REASONS: frozenset[FomoReason] = frozenset(
    {FomoReason.FOMO_PUMP_DOWNWEIGHT, FomoReason.FOMO_SOFT_MENTION}
)


# ---------------------------------------------------------------------------
# Thresholds — tunable, de-risk-only selectivity knobs.  All Decimal/int.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AntiFomoThresholds:
    """Tunable anti-FOMO thresholds.  Every knob is DE-RISK / SELECTIVITY ONLY.

    Pump ratios are dimensionless ``Decimal`` multiples of the window anchor price:
      * ratio 1.0  = no change
      * ratio 2.0  = +100% (2x)
      * ratio 4.0  = +300% (4x)  <- the default HARD ceiling (the directive's >300%)

    LOWERING ``hard_pump_ratio`` (tightening) only EXCLUDES MORE — always safe.
    The warm band [warm_pump_ratio, hard_pump_ratio) DOWN-WEIGHTS rather than rejects.

    The soft mention down-weight multiplier and the maximum pump haircut both live in
    [0, 1] and are applied as MULTIPLIERS to conviction — they can only SHRINK it.
    """

    # HARD pump ceiling (ratio of current price to window anchor).  Default 4.0 = +300%.
    # A run-up at/above this is a REJECT.
    hard_pump_ratio: Decimal = Decimal("4.0")
    # WARM pump floor (ratio).  Run-up in [warm, hard) is DOWN-WEIGHTED, not rejected.
    # Default 2.0 = +100% (already doubled => start de-rating conviction).
    warm_pump_ratio: Decimal = Decimal("2.0")
    # The MAXIMUM conviction haircut applied at the top of the warm band (just below
    # the hard ceiling).  A fraction in [0, 1]; the haircut scales linearly from 0 at
    # the warm floor to this value just below the hard ceiling.  Default 0.50 => at
    # worst the warm band halves conviction.  This is a MULTIPLIER reduction, never a boost.
    max_warm_pump_haircut: Decimal = Decimal("0.50")
    # Conviction MULTIPLIER for a SOFT mention tier (minor CEX / mainstream influencer).
    # In [0, 1]; default 0.50 => a soft mainstream mention halves conviction.
    soft_mention_multiplier: Decimal = Decimal("0.50")
    # Freshness budget (ms): pump/mention bundles older than this are EXCLUDED
    # refuse-by-default.  We never pass an "un-pumped" verdict on stale price history.
    max_staleness_ms: int = 60_000
    # Minimum number of price samples required to form a run-up ratio.  Fewer than this
    # => NO_PUMP_HISTORY (no opinion), NOT an exclusion.  Default 2 (need >= an anchor
    # and a current sample).
    min_price_samples: int = 2

    def __post_init__(self) -> None:
        for name in (
            "hard_pump_ratio",
            "warm_pump_ratio",
            "max_warm_pump_haircut",
            "soft_mention_multiplier",
        ):
            val = getattr(self, name)
            if isinstance(val, bool):  # bool is an int subclass — reject explicitly
                raise TypeError(f"AntiFomoThresholds.{name} must be Decimal/int/str, not bool")
            if isinstance(val, Decimal):
                continue
            if isinstance(val, int | str | float):
                object.__setattr__(self, name, Decimal(str(val)))
            else:
                raise TypeError(
                    f"AntiFomoThresholds.{name} must be Decimal/int/float/str, "
                    f"got {type(val).__name__}"
                )
        # Range / ordering validation.
        if self.warm_pump_ratio < Decimal(1):
            raise ValueError("warm_pump_ratio must be >= 1.0 (a pump is a non-decrease)")
        if self.hard_pump_ratio < self.warm_pump_ratio:
            raise ValueError(
                f"hard_pump_ratio ({self.hard_pump_ratio}) must be >= "
                f"warm_pump_ratio ({self.warm_pump_ratio})"
            )
        if not (Decimal(0) <= self.max_warm_pump_haircut <= Decimal(1)):
            raise ValueError("max_warm_pump_haircut must be a fraction in [0, 1]")
        if not (Decimal(0) <= self.soft_mention_multiplier <= Decimal(1)):
            raise ValueError("soft_mention_multiplier must be a fraction in [0, 1]")
        if not isinstance(self.max_staleness_ms, int) or isinstance(self.max_staleness_ms, bool):
            raise TypeError("max_staleness_ms must be int (ms)")
        if self.max_staleness_ms < 0:
            raise ValueError("max_staleness_ms must be non-negative")
        if not isinstance(self.min_price_samples, int) or isinstance(self.min_price_samples, bool):
            raise TypeError("min_price_samples must be int")
        if self.min_price_samples < 2:
            raise ValueError("min_price_samples must be >= 2 (need an anchor and a current sample)")

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> AntiFomoThresholds:
        """Build thresholds from environment (all optional; defaults if unset).

        Env keys (see .env.example E13 block).  A malformed value raises (fail-closed
        on config — we do not silently widen the FOMO band).
        """
        e = env if env is not None else os.environ

        def _dec(key: str, default: Decimal) -> Decimal:
            raw = e.get(key)
            return Decimal(str(raw)) if raw is not None and raw != "" else default

        def _int(key: str, default: int) -> int:
            raw = e.get(key)
            return int(raw) if raw is not None and raw != "" else default

        return cls(
            hard_pump_ratio=_dec("ANTI_FOMO_HARD_PUMP_RATIO", cls.hard_pump_ratio),
            warm_pump_ratio=_dec("ANTI_FOMO_WARM_PUMP_RATIO", cls.warm_pump_ratio),
            max_warm_pump_haircut=_dec(
                "ANTI_FOMO_MAX_WARM_PUMP_HAIRCUT", cls.max_warm_pump_haircut
            ),
            soft_mention_multiplier=_dec(
                "ANTI_FOMO_SOFT_MENTION_MULTIPLIER", cls.soft_mention_multiplier
            ),
            max_staleness_ms=_int("ANTI_FOMO_MAX_STALENESS_MS", cls.max_staleness_ms),
            min_price_samples=_int("ANTI_FOMO_MIN_PRICE_SAMPLES", cls.min_price_samples),
        )


# ---------------------------------------------------------------------------
# Inputs — the point-in-time pump/mention facts, as-of the event-time anchor.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AntiFomoInputs:
    """Point-in-time anti-FOMO facts about a candidate, as-of the event-time anchor.

    ``price_history_lamports`` is a list of integer-lamport price samples whose slots
    are all <= the anchor (point-in-time; no field admits a later-slot sample).  The
    run-up is measured from the window's anchor (the MINIMUM sample — the lowest price
    the token traded at in the window, the most adverse 'you missed the bottom' read)
    to the CURRENT (last) sample.  This is conservative for an anti-FOMO filter: it
    measures the LARGEST plausible run-up in the window.

    ``mention_tiers`` is the set of mainstream/CEX/Forbes-tier mention labels attached
    to the candidate by the SLOW-loop news/sentiment layer, as-of the anchor.

    All money is integer LAMPORTS (money rule).  None / empty history => no pump
    opinion (NOT an exclusion — a brand-new token has no run-up).
    """

    mint: str
    event_slot: int  # the as-of anchor (no forward reads)
    event_block_time_ms: int  # event_time.block_time_ms — AUTHORITATIVE clock
    data_staleness_ms: int  # recorded staleness of the pump/mention bundle

    # Point-in-time price samples in LAMPORTS (int), oldest->newest, slots <= anchor.
    price_history_lamports: tuple[int, ...] = ()
    # Mainstream / CEX / Forbes-tier mention labels (raw strings, parsed leniently).
    mention_tiers: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.event_slot, int) or isinstance(self.event_slot, bool):
            raise TypeError("AntiFomoInputs.event_slot must be int")
        if not isinstance(self.event_block_time_ms, int) or isinstance(
            self.event_block_time_ms, bool
        ):
            raise TypeError("AntiFomoInputs.event_block_time_ms must be int")
        if self.event_block_time_ms <= 0:
            raise ValueError("AntiFomoInputs.event_block_time_ms must be > 0 (real on-chain time)")
        if self.data_staleness_ms < 0:
            raise ValueError("AntiFomoInputs.data_staleness_ms must be non-negative")
        for px in self.price_history_lamports:
            if not isinstance(px, int) or isinstance(px, bool):
                raise TypeError(
                    "price_history_lamports must be int lamports (money rule), "
                    f"got {type(px).__name__}"
                )
            if px < 0:
                raise ValueError("price_history_lamports must be non-negative lamports")


# ---------------------------------------------------------------------------
# Verdict + reason hit.  REJECT / DOWN-WEIGHT / PASS only.  Never sizes / buys.
# ---------------------------------------------------------------------------


class FomoVerdict(StrEnum):
    PASS = "PASS"  # no FOMO opinion — SILENT (does not buy/size/trigger)
    DOWN_WEIGHT = "DOWN_WEIGHT"  # warm / soft — conviction HAIRCUT (multiplier < 1)
    REJECT = "REJECT"  # already-pumped or hard mainstream mention => EXCLUDED


@dataclass(frozen=True)
class FomoReasonHit:
    """One anti-FOMO reason with the measured value vs the threshold (UI/reasoner).

    ``measured`` / ``threshold`` are Decimal for the pump-ratio comparisons (exact), or
    None for membership/info reasons.  ``detail`` carries an optional human string
    (e.g. the mention tier name).
    """

    reason: FomoReason
    measured: Decimal | None
    threshold: Decimal | None
    detail: str = ""

    @property
    def is_exclusion(self) -> bool:
        return self.reason in _EXCLUSION_REASONS

    @property
    def is_downweight(self) -> bool:
        return self.reason in _DOWNWEIGHT_REASONS


@dataclass(frozen=True)
class FomoDecision:
    """The anti-FOMO verdict + the full reason report + the conviction multiplier.

    ``verdict``:
      * REJECT iff ANY exclusion reason tripped (already-pumped / hard mainstream
        mention / stale).  ``conviction_multiplier`` is ``Decimal(0)`` on REJECT.
      * else DOWN_WEIGHT iff any down-weight reason tripped — and then
        ``conviction_multiplier`` is the PRODUCT of every applicable haircut multiplier,
        in [0, 1).
      * else PASS — ``conviction_multiplier`` is ``Decimal(1)`` (no change; silent).

    THE MULTIPLIER IS HARD-CLAMPED TO [0, 1].  It is a DE-RISK multiplier: a downstream
    consumer multiplies ALREADY-CAPPED size by it.  Because it can never exceed 1, this
    object is structurally incapable of increasing size / conviction.  There is no field
    that sizes a trade, sets a stop, or instructs execution: this is a veto/haircut
    report, never a buy.
    """

    mint: str
    verdict: FomoVerdict
    conviction_multiplier: Decimal
    reasons: tuple[FomoReasonHit, ...]
    event_slot: int
    event_block_time_ms: int

    def __post_init__(self) -> None:
        # Structural guard: the conviction multiplier can NEVER exceed 1.0 (de-risk
        # only) and can never be negative.  This makes "size up via the FOMO filter"
        # unrepresentable, not merely discouraged.
        if not (Decimal(0) <= self.conviction_multiplier <= Decimal(1)):
            raise ValueError(
                "conviction_multiplier must be in [0, 1] (anti-FOMO is DE-RISK ONLY; "
                "it can never raise conviction)"
            )
        if self.verdict is FomoVerdict.REJECT and self.conviction_multiplier != Decimal(0):
            raise ValueError("a REJECT must carry conviction_multiplier == 0")
        if self.verdict is FomoVerdict.PASS and self.conviction_multiplier != Decimal(1):
            raise ValueError("a PASS must carry conviction_multiplier == 1 (no change)")
        if self.verdict is FomoVerdict.DOWN_WEIGHT and not (
            Decimal(0) <= self.conviction_multiplier < Decimal(1)
        ):
            raise ValueError("a DOWN_WEIGHT must carry conviction_multiplier in [0, 1)")

    @property
    def passed(self) -> bool:
        """True iff NOT excluded.  NOTE: a DOWN_WEIGHT is 'passed but de-rated'."""
        return self.verdict is not FomoVerdict.REJECT

    @property
    def excluded(self) -> bool:
        return self.verdict is FomoVerdict.REJECT

    @property
    def down_weighted(self) -> bool:
        return self.verdict is FomoVerdict.DOWN_WEIGHT

    @property
    def reason_codes(self) -> tuple[str, ...]:
        """Stable string codes for the dashboard / reasoner (all reasons, in order)."""
        return tuple(hit.reason.value for hit in self.reasons)

    @property
    def exclusion_codes(self) -> tuple[str, ...]:
        """Only the codes that caused exclusion (empty unless REJECT)."""
        return tuple(hit.reason.value for hit in self.reasons if hit.is_exclusion)

    def apply(self, capped_size: int) -> int:
        """Apply the conviction haircut to an ALREADY-CAPPED integer size (lamports).

        DE-RISK ONLY: the result is ``floor(capped_size * conviction_multiplier)`` and
        is therefore ALWAYS <= ``capped_size`` (the multiplier is clamped to <= 1).
        This filter can SHRINK a size that other code already sized + capped; it can
        NEVER grow one.  A REJECT yields 0 (no position).  Uses integer/Decimal math
        only (money rule) — no float.
        """
        if not isinstance(capped_size, int) or isinstance(capped_size, bool):
            raise TypeError("capped_size must be int lamports (money rule)")
        if capped_size < 0:
            raise ValueError("capped_size must be non-negative lamports")
        # Decimal multiply then floor to int lamports.  Result <= capped_size always.
        scaled = (Decimal(capped_size) * self.conviction_multiplier).to_integral_value(
            rounding="ROUND_FLOOR"
        )
        out = int(scaled)
        # Defence-in-depth: never exceed the input even under a pathological multiplier.
        return min(out, capped_size)


# ---------------------------------------------------------------------------
# Telemetry seam — we INVOKE a sink; we do not author Prometheus here.
# ---------------------------------------------------------------------------


@runtime_checkable
class AntiFomoTelemetry(Protocol):
    """Optional telemetry sink for anti-FOMO excludes / down-weights."""

    def on_fomo_reject(self, mint: str, reasons: tuple[str, ...]) -> None: ...

    def on_fomo_downweight(self, mint: str, multiplier: str, reasons: tuple[str, ...]) -> None: ...


# ---------------------------------------------------------------------------
# The filter.
# ---------------------------------------------------------------------------


@dataclass
class AntiFomoFilter:
    """SLOW-LOOP anti-FOMO / already-pumped EXCLUSION filter.  De-risk only.

    ``evaluate(inputs)`` measures the recent price run-up and inspects the
    mainstream/CEX/Forbes-tier mentions, returning a ``FomoDecision``.  It may ONLY:
      * REJECT (already ran past the ceiling, or a hard mainstream/CEX mention), or
      * DOWN_WEIGHT (warm pump band, or a soft mention) — a conviction MULTIPLIER < 1, or
      * PASS (no FOMO opinion) — multiplier == 1, silent.

    It constructs no Intent, sizes nothing, sets no stop, and holds no reference to any
    entry/sizing path.  A mainstream mention is treated as an EXCLUSION signal, never a
    buy trigger — the contrarian rule of the firm.  SLOW-LOOP ONLY; never on the
    FAST/snipe hot path.
    """

    thresholds: AntiFomoThresholds = field(default_factory=AntiFomoThresholds)
    telemetry: AntiFomoTelemetry | None = None

    def evaluate(self, inputs: AntiFomoInputs) -> FomoDecision:
        """Evaluate the anti-FOMO band.  Refuse-by-default on stale data only.

        All checks are evaluated (no short-circuit on the non-stale path) so the
        dashboard/reasoner sees every reason; the verdict is REJECT iff any exclusion
        reason tripped, else DOWN_WEIGHT iff any down-weight reason tripped, else PASS.
        """
        reasons: list[FomoReasonHit] = []

        # -- refuse-by-default gate: staleness -------------------------------
        if inputs.data_staleness_ms > self.thresholds.max_staleness_ms:
            reasons.append(
                FomoReasonHit(
                    FomoReason.INPUT_STALE,
                    Decimal(inputs.data_staleness_ms),
                    Decimal(self.thresholds.max_staleness_ms),
                )
            )
            # Stale price/mention data is untrusted — exclude immediately.  We will not
            # emit a "looks un-pumped" pass computed on stale history.
            return self._finalize(inputs, reasons)

        # -- already-pumped (price run-up) -----------------------------------
        self._eval_pump(inputs, reasons)

        # -- mainstream / CEX / Forbes-tier mentions -------------------------
        self._eval_mentions(inputs, reasons)

        return self._finalize(inputs, reasons)

    # -- pump band -----------------------------------------------------------

    def _eval_pump(self, inputs: AntiFomoInputs, reasons: list[FomoReasonHit]) -> None:
        """Measure the recent run-up and append the matching reason (if any).

        Anchor = the MINIMUM sample in the window (most adverse 'missed the bottom');
        current = the LAST sample.  ratio = current / anchor, exact Decimal.  Absence
        of usable history => NO_PUMP_HISTORY (info, no opinion) — NOT an exclusion.
        """
        history = inputs.price_history_lamports
        if len(history) < self.thresholds.min_price_samples:
            reasons.append(FomoReasonHit(FomoReason.NO_PUMP_HISTORY, None, None))
            return

        anchor = min(history)
        current = history[-1]
        if anchor <= 0:
            # A zero/degenerate anchor cannot form a ratio.  No opinion (info), not an
            # exclusion — we do not reject merely because the floor price is unknown.
            reasons.append(FomoReasonHit(FomoReason.NO_PUMP_HISTORY, None, None))
            return

        # Exact dimensionless ratio via Decimal integer division — no float drift.
        ratio = Decimal(current) / Decimal(anchor)

        if ratio >= self.thresholds.hard_pump_ratio:
            reasons.append(
                FomoReasonHit(
                    FomoReason.FOMO_ALREADY_PUMPED,
                    ratio,
                    self.thresholds.hard_pump_ratio,
                )
            )
        elif ratio >= self.thresholds.warm_pump_ratio:
            reasons.append(
                FomoReasonHit(
                    FomoReason.FOMO_PUMP_DOWNWEIGHT,
                    ratio,
                    self.thresholds.warm_pump_ratio,
                )
            )
        # else: below the warm floor => no FOMO opinion from the pump dimension.

    # -- mention band --------------------------------------------------------

    def _eval_mentions(self, inputs: AntiFomoInputs, reasons: list[FomoReasonHit]) -> None:
        """Inspect mainstream/CEX/Forbes-tier mentions.  HARD => REJECT; SOFT => haircut.

        A mainstream / major-CEX / Forbes-tier mention is an EXCLUSION signal — never a
        buy.  We record one reason per DISTINCT tier present so the dashboard shows
        exactly which mention drove the verdict.
        """
        seen: set[MentionTier] = set()
        for raw in inputs.mention_tiers:
            tier = parse_mention_tier(raw)
            if tier is None or tier in seen:
                continue
            seen.add(tier)
            if tier in _HARD_MENTION_TIERS:
                reasons.append(
                    FomoReasonHit(FomoReason.FOMO_MAINSTREAM_MENTION, None, None, detail=tier.value)
                )
            elif tier in _SOFT_MENTION_TIERS:
                reasons.append(
                    FomoReasonHit(FomoReason.FOMO_SOFT_MENTION, None, None, detail=tier.value)
                )

    # -- finalize ------------------------------------------------------------

    def _finalize(self, inputs: AntiFomoInputs, reasons: list[FomoReasonHit]) -> FomoDecision:
        """Compute the verdict + the clamped conviction multiplier from the reasons."""
        any_exclusion = any(hit.is_exclusion for hit in reasons)

        if any_exclusion:
            verdict = FomoVerdict.REJECT
            multiplier = Decimal(0)
        else:
            multiplier = self._compute_multiplier(reasons)
            if multiplier < Decimal(1):
                verdict = FomoVerdict.DOWN_WEIGHT
            else:
                verdict = FomoVerdict.PASS
                multiplier = Decimal(1)  # silent pass — exactly 1, no change

        decision = FomoDecision(
            mint=inputs.mint,
            verdict=verdict,
            conviction_multiplier=multiplier,
            reasons=tuple(reasons),
            event_slot=inputs.event_slot,
            event_block_time_ms=inputs.event_block_time_ms,
        )

        if self.telemetry is not None:
            if decision.excluded:
                self.telemetry.on_fomo_reject(inputs.mint, decision.exclusion_codes)
            elif decision.down_weighted:
                self.telemetry.on_fomo_downweight(
                    inputs.mint, str(multiplier), decision.reason_codes
                )

        return decision

    def _compute_multiplier(self, reasons: list[FomoReasonHit]) -> Decimal:
        """Product of every applicable DE-RISK haircut multiplier; clamped to [0, 1].

        Each down-weight reason contributes a multiplier <= 1; we multiply them so
        multiple distribution signals compound DOWNWARD (never upward).  The pump
        haircut scales linearly across the warm band (0 haircut at the warm floor,
        ``max_warm_pump_haircut`` just below the hard ceiling).
        """
        multiplier = Decimal(1)
        for hit in reasons:
            if hit.reason is FomoReason.FOMO_SOFT_MENTION:
                multiplier *= self.thresholds.soft_mention_multiplier
            elif hit.reason is FomoReason.FOMO_PUMP_DOWNWEIGHT and hit.measured is not None:
                haircut = self._warm_pump_haircut(hit.measured)
                multiplier *= Decimal(1) - haircut
        # Clamp defensively into [0, 1] (each factor is already in [0, 1]; this is
        # belt-and-suspenders so the multiplier is STRUCTURALLY never > 1).
        if multiplier < Decimal(0):
            return Decimal(0)
        if multiplier > Decimal(1):
            return Decimal(1)
        return multiplier

    def _warm_pump_haircut(self, ratio: Decimal) -> Decimal:
        """Linear haircut across the warm band: 0 at warm floor, max just below hard.

        Returns a haircut fraction in [0, max_warm_pump_haircut].  The conviction
        multiplier contribution is ``1 - haircut`` (so a larger run-up => smaller
        multiplier => less conviction).  Clamped so it can never imply a multiplier > 1.
        """
        warm = self.thresholds.warm_pump_ratio
        hard = self.thresholds.hard_pump_ratio
        span = hard - warm
        if span <= 0:
            # Degenerate band (warm == hard): apply the full configured haircut.
            return self.thresholds.max_warm_pump_haircut
        # Clamp ratio into [warm, hard) for the interpolation.
        clamped = ratio
        if clamped < warm:
            clamped = warm
        if clamped > hard:
            clamped = hard
        frac = (clamped - warm) / span  # in [0, 1]
        haircut = frac * self.thresholds.max_warm_pump_haircut
        if haircut < Decimal(0):
            return Decimal(0)
        if haircut > self.thresholds.max_warm_pump_haircut:
            return self.thresholds.max_warm_pump_haircut
        return haircut
