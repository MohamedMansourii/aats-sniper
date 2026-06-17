"""T-331 — MEV protection: private split exits + FAST/SECURE modes + sandwich
avoidance + tip-contention logging (C-3).

This module is the EXIT-SIDE complement to T-330 (atomic buy-with-revert).  The
exit is where a sniper gets sandwiched and killed (EDGE-VERDICT §1B / EH-002): a
single large sell on a thin curve broadcasts intent to the public mempool, a
front-runner buys ahead and dumps after, and an insider/LP-puller can dump into
the same/adjacent slot.  This module is the discipline that keeps the desk alive
on the way *out*:

  1. PRIVATE ROUTING — the exit rides a Jito bundle (private orderflow), not the
     public mempool.  No exposed pending sell to front-run.
  2. SPLIT SIZING — on a thin pool one large sell both moves the curve against us
     (own constant-product impact) and presents a fat sandwich target.  We split
     the remainder into bounded legs so each leg's price impact and sandwich
     surface is smaller; the legs ride the SAME private bundle (atomic, ordered),
     so a partial public-mempool race cannot insert between our own legs.
  3. BOUNDED PER-LEG SLIPPAGE — every leg carries a tight `min_out` so a leg that
     would fill worse than the bound REVERTS rather than getting filled into a
     sandwich.

ASYMMETRY (non-waivable, HARD RULE):
  The mode/route choice may ONLY REDUCE sandwich exposure.  SECURE is the default
  (OQ-008) and is private/lower-sandwich.  FAST is the public-ish, faster, higher-
  sandwich routing.  A planner is NEVER allowed to produce an exit plan whose
  modeled sandwich exposure EXCEEDS the Fast baseline for the same exit — the
  routing is a de-risk knob, never a risk-up knob (mirrors ADR-0006 asymmetric
  trust: signals/modes may only shed risk).  This is asserted structurally
  (`ExitProtectionPlanner.plan` clamps to <= Fast exposure) and in tests.

C-3 (tip-cohort-bias) — the load-bearing audit hook:
  At DECISION TIME, for every exit candidate (and recorded alongside every entry
  candidate), we stamp the LIVE tip floor read point-in-time from the injectable
  tip stream, plus the contention bucket it implies.  `GATE-A` (backtest-qa,
  T-401) stratifies net-of-cost PnL by that bucket.  If the only profitable
  cohort is the low-contention bucket, that is negative-selection residual (the
  desk is winning only the launches the co-located staked bots declined), NOT
  edge — and scale-up (R4) is BLOCKED.  This module's job is to make that
  stratification *constructible*: it emits a `TipContentionStamp` per candidate,
  point-in-time correct, never a compute-time read.

SEAM / OWNERSHIP (AATS-ROSTER §4):
  * THIS module (`mev-latency-engineer`) owns the SPEED + EDGE + ROUTE: how to
    split/route an exit to minimize sandwich exposure, and the tip-contention
    audit stamp.  It plans the exit legs and tags routing; it authors NO
    transaction-building or RPC code.
  * `risk-guardrails-engineer`'s `ExitEngine` (T-325) DECIDES *whether/how much*
    to exit (which rung, the hard stop, the trailing exit) and emits the de-risk
    `ExitIntent`/`ReduceIntent`.  This module takes that decision's
    fraction + mode and turns it into a sandwich-minimizing private SPLIT PLAN.
  * `solana-execution-engineer`'s venue builds/signs/lands the legs behind the
    `ExecutionVenue` seam, DRY-RUN by default.  This module never submits.

INJECTABLE + OFFLINE-SAFE: the live tip stream is injected (`TipStreamProtocol`,
the same seam T-330 uses); `StaticTipStream` is the deterministic offline mock.
There is NO network call here.

Money: every quantity is int base units / int bps / Decimal price — NEVER float.
Point-in-time: the tip stamp is read from a snapshot stamped at-or-before the
decision slot; a future snapshot is refused by the stream (C-5).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal
from enum import StrEnum

from aats.mev.tip_stream import (
    StaleTipStreamError,
    TipFloorSnapshot,
    TipStreamProtocol,
)
from aats.risk.exit_engine import (
    FAST_SLIPPAGE,
    SECURE_SLIPPAGE,
    MevExitMode,
    MevSlippageModel,
)

logger = logging.getLogger(__name__)

_FULL_EXIT_BPS = 10_000
_BPS_DENOM = Decimal(10_000)


# --------------------------------------------------------------------------- #
# Routing modes — re-exported alias of the risk lane's MevExitMode so this
# module and the ExitEngine share ONE definition of fast/secure (no fork).
# --------------------------------------------------------------------------- #

# SECURE is the production default (OQ-008): private routing, lower sandwich.
DEFAULT_EXIT_MODE = MevExitMode.SECURE


def slippage_model_for(mode: MevExitMode) -> MevSlippageModel:
    """The modeled per-mode sandwich/slippage profile (shared with the risk lane).

    SECURE has strictly LOWER modeled sandwich probability than FAST (it routes
    private).  This is the structural fact the asymmetry guard relies on.
    """
    return FAST_SLIPPAGE if mode is MevExitMode.FAST else SECURE_SLIPPAGE


# --------------------------------------------------------------------------- #
# C-3 — the tip-contention stamp recorded per candidate at DECISION time.
# This is what makes GATE-A's tip-contention stratification constructible.
# --------------------------------------------------------------------------- #


class ContentionBucket(StrEnum):
    """The tip-contention cohort a candidate falls into (C-3 stratification key).

    Derived from where THIS candidate's expected edge ceiling sits relative to
    the LIVE tip-floor ladder at decision time:

      * LOW    — the live floor is comfortably below our edge ceiling; few
                 competitors bidding.  The cohort the EDGE-VERDICT warns may be
                 residual/adverse (the launches the pros declined).
      * MEDIUM — the floor is rising toward our ceiling; real contention.
      * HIGH   — the floor is at/above the p75..p95 of recent landed tips; a
                 contested launch dominated by co-located staked bots.
      * PRICED_OUT — the floor exceeds our edge ceiling; the TipController would
                 refuse to submit.  Recorded so GATE-A sees the auctions we
                 declined (the cohort we were priced OUT of, not just into).

    GATE-A stratifies net PnL by this bucket; LOW-only profit => flag
    negative-selection residual => BLOCK scale-up (C-3).
    """

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    PRICED_OUT = "priced_out"


@dataclass(frozen=True)
class TipContentionStamp:
    """A point-in-time stamp of tip contention for ONE candidate (C-3).

    Recorded at DECISION TIME for every entry AND exit candidate.  Every field is
    integer lamports / integer slot — NEVER float.  `as_of_slot` is the EVENT-TIME
    slot of the live snapshot used; it MUST be <= `decision_slot` (point-in-time).

    `bucket` is the stratification key GATE-A groups net PnL by.  The raw ladder
    (`live_p50_lamports`..`live_p95_lamports`) is carried too so GATE-A can
    re-bucket with a different boundary if its analysis demands, without re-reading
    a (now-unavailable) live feed.
    """

    candidate_id: str  # the client_intent_id / mint+slot this stamp is for
    mint: str
    decision_slot: int
    as_of_slot: int  # event-time slot of the snapshot used (<= decision_slot)
    as_of_block_time_ms: int
    # the live tip-floor ladder at decision time (integer lamports)
    live_p50_lamports: int
    live_p75_lamports: int
    live_p95_lamports: int
    # the floor we would actually have to clear for THIS candidate's bucket
    live_floor_lamports: int
    # the edge-derived ceiling (0.30x edge) the TipController would cap at
    edge_ceiling_lamports: int
    bucket: ContentionBucket
    # whether the candidate was priced OUT (floor > ceiling) at decision time
    priced_out: bool

    def __post_init__(self) -> None:
        # Money/slot fields are integers — reject float (data-models.md §0).
        for name in (
            "decision_slot",
            "as_of_slot",
            "as_of_block_time_ms",
            "live_p50_lamports",
            "live_p75_lamports",
            "live_p95_lamports",
            "live_floor_lamports",
            "edge_ceiling_lamports",
        ):
            v = getattr(self, name)
            if isinstance(v, bool | float):
                raise TypeError(
                    f"TipContentionStamp.{name} must be int (lamports/slot), not float/bool."
                )
        # POINT-IN-TIME: never stamp a candidate with a FUTURE tip snapshot (C-5).
        if self.as_of_slot > self.decision_slot:
            raise ValueError(
                f"TipContentionStamp.as_of_slot ({self.as_of_slot}) must be <= "
                f"decision_slot ({self.decision_slot}) — a future tip snapshot is a "
                "lookahead leak (C-5)."
            )
        if self.priced_out and self.bucket is not ContentionBucket.PRICED_OUT:
            raise ValueError(
                "priced_out=True must carry bucket=PRICED_OUT (C-3 stratification key)."
            )
        if self.bucket is ContentionBucket.PRICED_OUT and not self.priced_out:
            raise ValueError("bucket=PRICED_OUT must carry priced_out=True.")


class TipContentionRecorder:
    """Stamps the LIVE tip floor + contention bucket per candidate at decision time.

    This is the C-3 instrument: it reads the injectable tip stream POINT-IN-TIME
    and turns it into a `TipContentionStamp` GATE-A can stratify by.  It does NOT
    decide whether to trade and does NOT price the tip to pay (that is the
    TipController, T-330) — it only RECORDS the contention context so the cohort
    bias is auditable.  Read-only over the stream; no money is moved here.

    INJECTABLE: the same `TipStreamProtocol` seam the TipController uses.  Offline
    tests pass `StaticTipStream`.

    Bucket boundaries are derived from the LIVE ladder (never hardcoded lamport
    constants): a candidate is LOW if its required floor sits at/below the live
    p50, MEDIUM up to p75, HIGH up to p95, and (independently) PRICED_OUT if the
    floor it must clear exceeds the candidate's 0.30x edge ceiling.
    """

    # The edge ceiling fraction mirrors the TipController's cap (0.30x edge).
    # Stored as Decimal so the ceiling math never touches float.
    def __init__(
        self,
        tip_stream: TipStreamProtocol,
        *,
        edge_cap_frac: Decimal | str = Decimal("0.30"),
        sink: object | None = None,
    ) -> None:
        self._stream = tip_stream
        if isinstance(edge_cap_frac, float):
            raise TypeError(
                "edge_cap_frac must be Decimal or str, not float (exact-money rule)."
            )
        cap = Decimal(edge_cap_frac)
        if not (Decimal("0") < cap <= Decimal("1")):
            raise ValueError(f"edge_cap_frac must be in (0, 1], got {cap}")
        self._edge_cap_frac = cap
        # An optional append-only sink (e.g. a list / Redis stream writer) GATE-A
        # reads to stratify.  When None, the stamp is only logged + returned.
        self._sink = sink

    def stamp(
        self,
        *,
        candidate_id: str,
        mint: str,
        expected_edge_lamports: int,
        decision_slot: int,
        required_percentile: int = 50,
    ) -> TipContentionStamp | None:
        """Record the tip-contention context for ONE candidate, point-in-time.

        Args:
            candidate_id: the client_intent_id (entry) or exit candidate id.
            mint: the token mint.
            expected_edge_lamports: this candidate's expected NET edge (lamports,
                integer).  Used ONLY to derive the 0.30x ceiling for the
                priced-out flag — never to size anything here.
            decision_slot: the event-time slot of the decision.  The snapshot used
                MUST be valid at-or-before this slot (point-in-time, C-5).
            required_percentile: which ladder percentile this candidate must clear
                to land (the contention it actually faces).  Default p50.

        Returns:
            The `TipContentionStamp`, or None if no point-in-time tip snapshot is
            available (no live floor => we cannot stamp contention; we record the
            absence loudly rather than guessing a constant — same discipline as
            the TipController's do-not-submit).
        """
        if isinstance(expected_edge_lamports, bool | float):
            raise TypeError(
                "expected_edge_lamports must be int (lamports), not float/bool."
            )
        try:
            snap: TipFloorSnapshot = self._stream.current(decision_slot=decision_slot)
        except StaleTipStreamError as exc:
            logger.warning(
                "tip_contention.no_live_floor",
                extra={
                    "candidate_id": candidate_id,
                    "mint": mint,
                    "decision_slot": decision_slot,
                    "reason": str(exc),
                },
            )
            return None

        live_floor = snap.percentile(required_percentile)
        edge_ceiling = self._edge_ceiling(expected_edge_lamports)
        priced_out = live_floor > edge_ceiling
        bucket = self._bucket(
            live_floor=live_floor,
            snap=snap,
            priced_out=priced_out,
        )
        stamp = TipContentionStamp(
            candidate_id=candidate_id,
            mint=mint,
            decision_slot=decision_slot,
            as_of_slot=snap.as_of_slot,
            as_of_block_time_ms=snap.as_of_block_time_ms,
            live_p50_lamports=snap.p50_lamports,
            live_p75_lamports=snap.p75_lamports,
            live_p95_lamports=snap.p95_lamports,
            live_floor_lamports=live_floor,
            edge_ceiling_lamports=edge_ceiling,
            bucket=bucket,
            priced_out=priced_out,
        )
        self._emit(stamp)
        return stamp

    # -- internal ---------------------------------------------------------- #

    def _edge_ceiling(self, expected_edge_lamports: int) -> int:
        """floor(0.30 * edge) in integer lamports (exact Decimal, never float).

        Non-positive edge => a zero ceiling (everything is priced out — there is
        no edge to spend a tip on; mirrors the TipController's no-edge refusal).
        """
        if expected_edge_lamports <= 0:
            return 0
        return int(
            (self._edge_cap_frac * Decimal(expected_edge_lamports)).to_integral_value(
                rounding=ROUND_FLOOR
            )
        )

    @staticmethod
    def _bucket(
        *,
        live_floor: int,
        snap: TipFloorSnapshot,
        priced_out: bool,
    ) -> ContentionBucket:
        """Derive the contention bucket from the LIVE ladder (no hardcoded lamports).

        PRICED_OUT dominates: if the candidate's required floor exceeds its edge
        ceiling it is recorded as priced out (the cohort we DECLINED), regardless
        of where the floor sits on the ladder.  Otherwise the bucket is where the
        required floor sits on the live percentile ladder.
        """
        if priced_out:
            return ContentionBucket.PRICED_OUT
        if live_floor <= snap.p50_lamports:
            return ContentionBucket.LOW
        if live_floor <= snap.p75_lamports:
            return ContentionBucket.MEDIUM
        return ContentionBucket.HIGH

    def _emit(self, stamp: TipContentionStamp) -> None:
        logger.info(
            "tip_contention.stamp",
            extra={
                "candidate_id": stamp.candidate_id,
                "mint": stamp.mint,
                "decision_slot": stamp.decision_slot,
                "as_of_slot": stamp.as_of_slot,
                "bucket": stamp.bucket.value,
                "priced_out": stamp.priced_out,
                "live_floor_lamports": stamp.live_floor_lamports,
                "edge_ceiling_lamports": stamp.edge_ceiling_lamports,
            },
        )
        # Append-only sink GATE-A reads (list.append / stream writer duck-type).
        sink = self._sink
        if sink is not None:
            append = getattr(sink, "append", None)
            if append is not None:
                try:
                    append(stamp)
                except Exception:  # noqa: BLE001 — telemetry must never break the path
                    logger.exception("tip_contention.sink_append_failed")


# --------------------------------------------------------------------------- #
# Private split exit — the sandwich-avoidance plan.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ExitLeg:
    """One leg of a split exit (a fraction of the remaining position).

    `fraction_bps` is a share OF THE REMAINING position at exit time (1..10000);
    the legs sum to exactly the requested exit fraction.  `min_out_base` is the
    integer base-unit floor below which this leg REVERTS (the bounded per-leg
    slippage — a leg filled into a sandwich worse than this is dropped, not
    filled).  `private` is whether the leg rides a private bundle (always True for
    SECURE; True for FAST too here — both route private, FAST just bids/competes
    differently — but the flag is explicit so a future public-FAST variant cannot
    silently expose intent without flipping it).
    """

    leg_index: int
    fraction_bps: int  # share of the requested exit fraction (sums to total)
    min_out_base: int  # integer base-unit revert floor for this leg
    private: bool

    def __post_init__(self) -> None:
        for name in ("leg_index", "fraction_bps", "min_out_base"):
            v = getattr(self, name)
            if isinstance(v, bool | float):
                raise TypeError(f"ExitLeg.{name} must be int, not float/bool.")
        if self.leg_index < 0:
            raise ValueError("ExitLeg.leg_index must be >= 0.")
        if not (1 <= self.fraction_bps <= _FULL_EXIT_BPS):
            raise ValueError(
                f"ExitLeg.fraction_bps must be in [1, 10000], got {self.fraction_bps}."
            )
        if self.min_out_base < 0:
            raise ValueError(
                f"ExitLeg.min_out_base must be >= 0 base units, got {self.min_out_base}."
            )


@dataclass(frozen=True)
class SplitExitPlan:
    """A sandwich-minimizing private split-exit plan for ONE exit decision.

    Produced by `ExitProtectionPlanner.plan` from a risk-lane exit decision
    (fraction + mode).  The legs ride a private bundle in order; each carries a
    bounded `min_out_base`.  `modeled_sandwich_p_bps` is the planner's modeled
    probability of being sandwiched on this plan (bps) — used ONLY for the
    asymmetry guard + telemetry, NOT as live economics (live numbers come from the
    venue).

    INVARIANTS (asserted in __post_init__ AND by the planner):
      * legs' fraction_bps sum to exactly `total_fraction_bps`,
      * every leg is `private` (no public-mempool exposure on any leg),
      * `modeled_sandwich_p_bps` <= the FAST single-shot baseline for the same
        exit (the asymmetry: a plan may only REDUCE sandwich exposure).
    """

    mint: str
    mode: MevExitMode
    total_fraction_bps: int  # the requested exit fraction (of remaining)
    legs: tuple[ExitLeg, ...]
    modeled_sandwich_p_bps: int  # modeled P(sandwiched) for this plan (bps)
    fast_baseline_sandwich_p_bps: int  # the FAST single-shot baseline (the cap)
    reason: str

    def __post_init__(self) -> None:
        if isinstance(self.total_fraction_bps, bool | float):
            raise TypeError("total_fraction_bps must be int (bps), not float/bool.")
        if not (1 <= self.total_fraction_bps <= _FULL_EXIT_BPS):
            raise ValueError(
                f"total_fraction_bps must be in [1, 10000], got {self.total_fraction_bps}."
            )
        if not self.legs:
            raise ValueError("SplitExitPlan must have at least one leg.")
        leg_sum = sum(leg.fraction_bps for leg in self.legs)
        if leg_sum != self.total_fraction_bps:
            raise ValueError(
                f"SplitExitPlan legs must sum to total_fraction_bps "
                f"({self.total_fraction_bps}); got {leg_sum}."
            )
        if not all(leg.private for leg in self.legs):
            raise ValueError(
                "Every leg of a SplitExitPlan must be private (no public-mempool "
                "exposure of an exit intent — sandwich avoidance)."
            )
        for name in ("modeled_sandwich_p_bps", "fast_baseline_sandwich_p_bps"):
            v = getattr(self, name)
            if isinstance(v, bool | float):
                raise TypeError(f"SplitExitPlan.{name} must be int (bps), not float/bool.")
            if not (0 <= v <= _FULL_EXIT_BPS):
                raise ValueError(f"SplitExitPlan.{name} must be in [0, 10000] bps, got {v}.")
        # THE ASYMMETRY INVARIANT: a plan may only REDUCE sandwich exposure.
        if self.modeled_sandwich_p_bps > self.fast_baseline_sandwich_p_bps:
            raise ValueError(
                "SplitExitPlan refused: modeled_sandwich_p_bps "
                f"({self.modeled_sandwich_p_bps}) > FAST baseline "
                f"({self.fast_baseline_sandwich_p_bps}). A routing/split plan may "
                "ONLY reduce sandwich exposure, never increase it (asymmetric, "
                "ADR-0006 / HARD RULE)."
            )

    @property
    def n_legs(self) -> int:
        return len(self.legs)


@dataclass(frozen=True)
class PoolLiquidity:
    """The point-in-time pool depth the planner sizes legs against (integer base
    units).  Thin pools get MORE legs (smaller per-leg impact + sandwich surface);
    deep pools may need only one leg.

    `quote_reserve_base` is the curve's quote-side reserve (e.g. SOL lamports in
    the pool) at decision time — the larger it is relative to our sell, the less a
    single leg moves the curve and the smaller the sandwich target.
    """

    quote_reserve_base: int
    # the integer base-unit size we intend to sell (cost-basis notional)
    sell_notional_base: int

    def __post_init__(self) -> None:
        for name in ("quote_reserve_base", "sell_notional_base"):
            v = getattr(self, name)
            if isinstance(v, bool | float):
                raise TypeError(f"PoolLiquidity.{name} must be int (base units), not float/bool.")
            if v < 0:
                raise ValueError(f"PoolLiquidity.{name} must be >= 0, got {v}.")

    def sell_fraction_of_pool_bps(self) -> int:
        """How big our sell is relative to the pool, in bps (integer).

        0 when the pool reserve is 0 (degenerate — treated as maximally thin =>
        max splitting).  Capped at 10000 (a sell >= the pool is 100%+).
        """
        if self.quote_reserve_base <= 0:
            return _FULL_EXIT_BPS
        frac = (self.sell_notional_base * _FULL_EXIT_BPS) // self.quote_reserve_base
        return min(frac, _FULL_EXIT_BPS)


class ExitProtectionPlanner:
    """Turns a risk-lane exit decision into a sandwich-minimizing private split.

    The planner does NOT decide whether or how much to exit — that is the
    ExitEngine (T-325).  It takes the exit fraction + mode and produces a
    `SplitExitPlan`:

      * how many legs (more on thin pools, fewer on deep pools / small sells),
      * each leg's bounded `min_out_base` (the per-leg slippage floor),
      * every leg private (no public-mempool exposure),
      * a modeled sandwich probability that is CLAMPED never to exceed the FAST
        single-shot baseline (the asymmetry: a plan may only reduce exposure).

    The leg-count heuristic is deterministic and point-in-time (a pure function of
    the pool depth + mode + exit size).  Production landing economics come from the
    live venue; the modeled sandwich probability here exists for the asymmetry
    guard + telemetry + the offline sandwich-attempt sim.
    """

    def __init__(
        self,
        *,
        max_legs: int = 4,
        # split when our sell is >= this fraction of the pool (bps); thin-pool guard
        split_threshold_bps: int = 200,  # 2% of pool
    ) -> None:
        if isinstance(max_legs, bool) or not isinstance(max_legs, int):
            raise TypeError("max_legs must be int.")
        if max_legs < 1:
            raise ValueError("max_legs must be >= 1.")
        if isinstance(split_threshold_bps, bool) or not isinstance(split_threshold_bps, int):
            raise TypeError("split_threshold_bps must be int (bps).")
        if not (1 <= split_threshold_bps <= _FULL_EXIT_BPS):
            raise ValueError("split_threshold_bps must be in [1, 10000] bps.")
        self._max_legs = max_legs
        self._split_threshold_bps = split_threshold_bps

    def plan(
        self,
        *,
        mint: str,
        fraction_bps: int,
        mode: MevExitMode,
        pool: PoolLiquidity,
        per_leg_slippage_bps: int,
        expected_tokens_out_base: int,
    ) -> SplitExitPlan:
        """Plan a private split exit for one exit decision.

        Args:
            mint: the token mint.
            fraction_bps: the exit fraction (of remaining) the ExitEngine decided
                (1..10000).  Integer bps.
            mode: the routing mode (SECURE default / FAST).  The plan's modeled
                sandwich exposure is clamped to <= the FAST baseline regardless.
            pool: point-in-time pool depth (thin pools => more legs).
            per_leg_slippage_bps: the tight per-leg slippage bound (integer bps);
                each leg's `min_out_base` is set from it so a leg filling worse
                reverts rather than getting sandwiched.
            expected_tokens_out_base: the integer base-unit amount we expect to
                receive for the WHOLE exit at the current mark (used to derive each
                leg's bounded min_out).

        Returns:
            A `SplitExitPlan` whose modeled sandwich exposure NEVER exceeds the
            FAST single-shot baseline (asymmetry guaranteed structurally).
        """
        self._validate_exit_inputs(
            fraction_bps=fraction_bps,
            per_leg_slippage_bps=per_leg_slippage_bps,
            expected_tokens_out_base=expected_tokens_out_base,
        )

        # 1. Decide the leg count from pool thinness + exit size (deterministic).
        n_legs = self._leg_count(pool)

        # 2. Split the fraction into n_legs near-equal integer-bps legs (remainder
        #    to the first leg so they sum EXACTLY to fraction_bps — no float).
        leg_fracs = _split_bps_evenly(fraction_bps, n_legs)

        # 3. The FAST single-shot baseline sandwich probability is the cap (the
        #    most-exposed any routing is allowed to be) — the asymmetry anchor.
        fast_baseline_p = FAST_SLIPPAGE.sandwich_p_bps

        # 4. Model THIS plan's sandwich probability.  Private routing (both modes
        #    here route private) starts from the mode's base; splitting into k
        #    legs further shrinks each leg's surface, so per-plan exposure is the
        #    mode base scaled down by the split, then CLAMPED to <= the FAST
        #    baseline (the asymmetry — never above the public-ish worst case).
        mode_base_p = slippage_model_for(mode).sandwich_p_bps
        modeled_p = _modeled_split_sandwich_p_bps(mode_base_p, n_legs)
        modeled_p = min(modeled_p, fast_baseline_p)  # asymmetry clamp (de-risk only)

        # 5. Per-leg bounded min_out: each leg expects its share of the tokens-out,
        #    minus the per-leg slippage bound; below it the leg reverts.
        legs = self._build_legs(
            leg_fracs=leg_fracs,
            fraction_bps=fraction_bps,
            expected_tokens_out_base=expected_tokens_out_base,
            per_leg_slippage_bps=per_leg_slippage_bps,
        )

        plan = SplitExitPlan(
            mint=mint,
            mode=mode,
            total_fraction_bps=fraction_bps,
            legs=tuple(legs),
            modeled_sandwich_p_bps=modeled_p,
            fast_baseline_sandwich_p_bps=fast_baseline_p,
            reason=(
                f"{mode.value} private split: {n_legs} leg(s); "
                f"pool_fill={pool.sell_fraction_of_pool_bps()}bps; "
                f"per_leg_slip={per_leg_slippage_bps}bps; "
                f"modeled_sandwich_p={modeled_p}bps <= fast_baseline={fast_baseline_p}bps"
            ),
        )
        logger.info(
            "split_exit.plan",
            extra={
                "mint": mint,
                "mode": mode.value,
                "total_fraction_bps": fraction_bps,
                "n_legs": plan.n_legs,
                "modeled_sandwich_p_bps": modeled_p,
                "fast_baseline_sandwich_p_bps": fast_baseline_p,
                "pool_fill_bps": pool.sell_fraction_of_pool_bps(),
            },
        )
        return plan

    # -- internal ---------------------------------------------------------- #

    def _validate_exit_inputs(
        self,
        *,
        fraction_bps: int,
        per_leg_slippage_bps: int,
        expected_tokens_out_base: int,
    ) -> None:
        for name, val in (
            ("fraction_bps", fraction_bps),
            ("per_leg_slippage_bps", per_leg_slippage_bps),
            ("expected_tokens_out_base", expected_tokens_out_base),
        ):
            if isinstance(val, bool | float):
                raise TypeError(f"{name} must be int, not float/bool.")
        if not (1 <= fraction_bps <= _FULL_EXIT_BPS):
            raise ValueError("fraction_bps must be in [1, 10000].")
        if not (0 <= per_leg_slippage_bps < _FULL_EXIT_BPS):
            raise ValueError("per_leg_slippage_bps must be in [0, 10000).")
        if expected_tokens_out_base <= 0:
            raise ValueError("expected_tokens_out_base must be > 0 (a real fill).")

    def _leg_count(self, pool: PoolLiquidity) -> int:
        """Deterministic leg count: 1 leg when the sell is small vs the pool;
        scale up to max_legs as the sell grows past the split threshold.

        The thinner the pool relative to our sell (the bigger our footprint), the
        more legs — smaller per-leg curve impact and a smaller sandwich target.
        """
        fill_bps = pool.sell_fraction_of_pool_bps()
        if fill_bps < self._split_threshold_bps:
            return 1
        # Above the threshold, add a leg for every additional multiple of the
        # threshold our footprint occupies, capped at max_legs.
        multiples = fill_bps // self._split_threshold_bps
        return max(1, min(self._max_legs, int(multiples)))

    def _build_legs(
        self,
        *,
        leg_fracs: list[int],
        fraction_bps: int,
        expected_tokens_out_base: int,
        per_leg_slippage_bps: int,
    ) -> list[ExitLeg]:
        keep = (_BPS_DENOM - Decimal(per_leg_slippage_bps)) / _BPS_DENOM
        legs: list[ExitLeg] = []
        for idx, lf in enumerate(leg_fracs):
            # This leg's share of the WHOLE expected tokens-out (integer base).
            leg_tokens = (expected_tokens_out_base * lf) // fraction_bps
            # bounded min_out: leg_tokens * (1 - per_leg_slip), floored to integer.
            min_out = int((Decimal(leg_tokens) * keep).to_integral_value(rounding=ROUND_FLOOR))
            legs.append(
                ExitLeg(
                    leg_index=idx,
                    fraction_bps=lf,
                    min_out_base=max(min_out, 0),
                    private=True,  # every leg private — no public-mempool exposure
                )
            )
        return legs


# --------------------------------------------------------------------------- #
# Helpers — integer-exact split + the modeled sandwich-probability shrink.
# --------------------------------------------------------------------------- #


def _split_bps_evenly(total_bps: int, n: int) -> list[int]:
    """Split `total_bps` into `n` near-equal integer parts summing EXACTLY to it.

    The remainder is handed to the FIRST legs (one extra bp each) so the parts sum
    to exactly `total_bps` — no float, no rounding drift.  n is clamped to
    [1, total_bps] (can't make more non-zero legs than there are bps).
    """
    if n < 1:
        raise ValueError("n must be >= 1.")
    n = min(n, total_bps)  # at most one bp per leg
    base = total_bps // n
    rem = total_bps - base * n
    parts = [base + (1 if i < rem else 0) for i in range(n)]
    assert sum(parts) == total_bps  # integer-exact split invariant
    return parts


def _modeled_split_sandwich_p_bps(mode_base_p_bps: int, n_legs: int) -> int:
    """Model the sandwich probability of a private k-leg split (integer bps).

    Splitting into k private legs reduces each leg's surface; the modeled plan
    probability scales down with the leg count (integer division — floor).  This
    is a deterministic monotone-non-increasing function of n_legs (more legs =>
    never-higher modeled exposure), which is what the asymmetry guard needs.  It
    is NOT live economics; the venue measures the real number.
    """
    if n_legs < 1:
        raise ValueError("n_legs must be >= 1.")
    # k legs -> base / k (floor).  Monotone non-increasing in n_legs.
    return mode_base_p_bps // n_legs


__all__ = [
    "DEFAULT_EXIT_MODE",
    "MevExitMode",
    "slippage_model_for",
    "ContentionBucket",
    "TipContentionStamp",
    "TipContentionRecorder",
    "ExitLeg",
    "SplitExitPlan",
    "PoolLiquidity",
    "ExitProtectionPlanner",
]
