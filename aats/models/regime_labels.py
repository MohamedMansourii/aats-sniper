"""Chart-path REGIME label taxonomy + remains-sellable gate (M2-CP-02).

WHAT THIS IS (and is NOT)
=========================
This module defines the LEAK-FREE, EVENT-TIME-JOINED, VERSIONED *label* taxonomy for the
SLOW-loop chart-path / regime model:

    ACCUMULATION | DISTRIBUTION | RUG_IN_PROGRESS | NEUTRAL

plus the "remains-sellable at decision-relevant size" gate that decides whether real,
non-honeypot, non-rugged exit liquidity existed at decision-relevant size throughout the
survivor/exit horizon.  It is the LABEL half of the regime model (the input tensor is
M2-CP-01 `aats.features.ta.PricePathTensor`; the trained model + contract wiring are the
DATA-BLOCKED M2-CP-03/CP-04 items).  It ships NO trained model, NO ONNX export, NO Rust
shim — by design, so the regime STATE can never be dropped into the FAST/SNIPE race core.

A regime is a multiclass STATE + (later) a calibrated probability + uncertainty.  It is
NEVER a success-rate, NEVER a win-rate, NEVER a price.  There is no win-rate / success-rate
/ realized-mult / price / size field anywhere in this module (HONESTY CLAUSE, AC-037).

THE ASYMMETRIC-TRUST LAW (de-risk only; accumulation/bullish class provably INERT)
=================================================================================
The regime STATE maps to a `RegimeDeRiskAction` whose codomain contains ONLY de-risk or
inert values:  {NONE, REDUCE, FORCE_EXIT, VETO_ENTRY}.  There is NO "size up", "relax stop",
"delay exit", or "add leverage" member — a risk-INCREASING action is INEXPRESSIBLE BY TYPE.
The bullish/ranging classes are pinned INERT:

    ACCUMULATION  -> NONE        (can NEVER delay an exit, relax a stop, or size up)
    NEUTRAL       -> NONE
    DISTRIBUTION  -> REDUCE      (de-risk: shrink / lean-exit)
    RUG_IN_PROGRESS -> FORCE_EXIT  (de-risk-maximal: exit / veto)

So a confident ACCUMULATION prediction is worth exactly zero control authority: it cannot
move capital in the risk-increasing direction because no such move is representable.  The
DOWNSTREAM enforcement (wiring the scalar de-risk flag into exit_engine/rule_engine) is
M2-CP-04's job (a solana-systems-architect ADR); THIS module makes the inertness a property
of the taxonomy itself.

POINT-IN-TIME LEAK-FREEDOM (the whole game — T-300a)
====================================================
A LABEL is allowed to look FORWARD; a FEATURE never is.  This module enforces that
asymmetry by construction:

  - Labels live in their OWN dataset and join to features BY EVENT-TIME ONLY (slot +
    block_time), reusing `event_time_key` and the frozen `JoinedExample` shape.
  - `resolution_event_time` is STAMPED and STRICTLY LATER than the decision anchor
    (asserted in `RegimeOutcome.__post_init__`, mirroring the frozen `LaunchOutcome`
    §3A invariant) — the horizon proof that the label genuinely resolved forward.
  - Every outcome sample the label reads is gated at `decision.slot < outcome_slot <=
    resolution.slot` (`build_regime_outcome`) — the label reads ONLY the strictly-forward
    outcome window, never a decision-time or beyond-horizon bar, and never forward-fills.
  - The leak audit REUSES the frozen `assert_event_time_leq_decision` (feature event-time
    <= decision, resolution strictly > decision) and the taint guard REUSES the frozen
    `assert_no_label_taint` — a regime-label column that reaches a feature matrix fails the
    build (`assert_no_regime_label_taint`).
  - NO wall-clock anywhere: joins/gates use slot + block_time only (`wall_clock_ms` is
    monitoring-only per data-models §3.1).

LOOP PLACEMENT: SLOW loop only.  `build_regime_outcome`/`classify_regime` take an explicit
`loop` kwarg and REUSE the canonical `aats.models.survivor.assert_slow_loop_only` guard —
the chart-path/regime model is SLOW-loop only (BLUEPRINT triple-loop boundary), never
FAST/SNIPE, no ONNX/Rust shim.

MONEY DISCIPLINE (data-models §0)
=================================
Pool reserves, exit liquidity, and decision-relevant size are INTEGER lamports / base
units.  Slippage/thresholds are integer bps.  PRICES ARE RATIOS (spot = sol-per-token) and
may be float per data-models §0.  No PnL/fee/tip/size is ever COMPUTED here — the
sellability gate is a pure constant-product (x*y=k) depth probe reusing the same integer
math shape as `aats.risk.liquidity_sanity.simulate_entry_slippage_bps`.

REPRODUCIBLE / VERSIONED
========================
`REGIME_LABEL_TAXONOMY_VERSION` versions the taxonomy + the (fixed, NEVER data-fit)
classification thresholds; every `RegimeOutcome` carries it.  `is_bootstrap_not_real=True`
until the R1 recorded corpus exists — no regime label has earned any capital license.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from aats.contracts.events import EventTime
from aats.contracts.provenance import ProvenanceTaintError
from aats.models.featureset import assert_no_label_taint
from aats.models.survivor import assert_slow_loop_only
from aats.models.training import JoinedExample, event_time_key

# ---------------------------------------------------------------------------
# Versioning — bump on ANY change to the taxonomy OR the fixed thresholds below
# (a different taxonomy version is a different label, and a model trained on it is a
# different model_version).
# ---------------------------------------------------------------------------

REGIME_LABEL_TAXONOMY_VERSION = "1.0.0"

# SLOW-loop marker (reuses the canonical assert_slow_loop_only guard, which checks
# loop == "slow"); declared here so the regime STATE is never wired onto FAST/SNIPE.
REGIME_LABELS_LOOP = "slow"

BPS_DENOM = 10_000


# ---------------------------------------------------------------------------
# The taxonomy — EXHAUSTIVE + MUTUALLY EXCLUSIVE multiclass STATE (never a rate/price)
# ---------------------------------------------------------------------------


class RegimeLabel(StrEnum):
    """The four regime STATES over the survivor/exit horizon.

    A StrEnum is mutually exclusive by construction (a label is exactly one member).
    `classify_regime` is EXHAUSTIVE (a priority-ordered decision with a final NEUTRAL
    fall-through) — every possible outcome maps to exactly one member.
    """

    ACCUMULATION = "ACCUMULATION"          # still sellable; healthy base-building (INERT/bullish)
    DISTRIBUTION = "DISTRIBUTION"          # still sellable; topping / markdown (DE-RISK)
    RUG_IN_PROGRESS = "RUG_IN_PROGRESS"    # sellability collapsing at size (DE-RISK-MAXIMAL)
    NEUTRAL = "NEUTRAL"                     # still sellable; ranging / undetermined (INERT)


class RegimeDeRiskAction(StrEnum):
    """The ONLY actions a regime STATE may authorize — de-risk or inert, NEVER risk-up.

    The codomain deliberately contains NO size-up / relax-stop / delay-exit / leverage
    member: a risk-INCREASING action is INEXPRESSIBLE BY TYPE.  This is the type-level
    proof that the accumulation/bullish class is provably INERT.
    """

    NONE = "NONE"                # inert: authorizes NOTHING (ACCUMULATION, NEUTRAL)
    REDUCE = "REDUCE"            # de-risk: shrink / lean-exit (DISTRIBUTION)
    FORCE_EXIT = "FORCE_EXIT"    # de-risk-maximal: exit the position (RUG_IN_PROGRESS)
    VETO_ENTRY = "VETO_ENTRY"    # de-risk: veto a NEW entry (never affects an open stop)


# The TOTAL mapping from STATE -> de-risk action.  ACCUMULATION and NEUTRAL are pinned
# to NONE (inert) — they can never delay an exit, relax a stop, or size up.
REGIME_DERISK_ACTION: dict[RegimeLabel, RegimeDeRiskAction] = {
    RegimeLabel.ACCUMULATION: RegimeDeRiskAction.NONE,
    RegimeLabel.NEUTRAL: RegimeDeRiskAction.NONE,
    RegimeLabel.DISTRIBUTION: RegimeDeRiskAction.REDUCE,
    RegimeLabel.RUG_IN_PROGRESS: RegimeDeRiskAction.FORCE_EXIT,
}

# Integer codes for the frozen JoinedExample.label int field (event-time leak audit reuse).
# Ordered by de-risk severity (0 = inert .. 3 = maximal); the codes are an ENCODING, NOT a
# rank the model optimizes and NEVER a success rate.
REGIME_LABEL_CODES: dict[RegimeLabel, int] = {
    RegimeLabel.ACCUMULATION: 0,
    RegimeLabel.NEUTRAL: 1,
    RegimeLabel.DISTRIBUTION: 2,
    RegimeLabel.RUG_IN_PROGRESS: 3,
}


def derisk_action(label: RegimeLabel) -> RegimeDeRiskAction:
    """The (total) de-risk action a regime STATE authorizes.  Bullish class -> NONE."""
    return REGIME_DERISK_ACTION[label]


def is_inert(label: RegimeLabel) -> bool:
    """True iff the STATE authorizes NOTHING (ACCUMULATION / NEUTRAL) — provably de-risk-safe."""
    return REGIME_DERISK_ACTION[label] is RegimeDeRiskAction.NONE


# ---------------------------------------------------------------------------
# Fixed (NEVER data-fit) classification thresholds — versioned with the taxonomy.
# All are RATIOS (dimensionless), never money.  Documented so the label is auditable.
# ---------------------------------------------------------------------------

# DISTRIBUTION (topping / markdown while still sellable) fires if ANY of:
#   - the horizon ended materially DOWN from the decision anchor, or
#   - a deep, unrecovered drawdown-from-peak occurred while net return stayed negative, or
#   - the holder base shrank materially (smart money / whales distributing out).
DISTRIBUTION_NET_RETURN_MAX_RATIO = -0.25    # ended <= -25% vs the decision anchor
DISTRIBUTION_DRAWDOWN_MIN_RATIO = 0.50       # >= 50% drawdown from the horizon peak ...
DISTRIBUTION_HOLDER_SHRINK_MAX_RATIO = -0.20  # holder base shrank >= 20%

# ACCUMULATION (healthy base-building) requires ALL of: net-positive, holders growing,
# and a CONTROLLED (shallow) drawdown — otherwise it is not accumulation.
ACCUMULATION_NET_RETURN_MIN_RATIO = 0.20     # ended >= +20% vs the decision anchor
ACCUMULATION_HOLDER_GROWTH_MIN_RATIO = 0.10  # holder base grew >= 10%
ACCUMULATION_MAX_DRAWDOWN_RATIO = 0.40       # drawdown-from-peak stayed <= 40%


# ---------------------------------------------------------------------------
# The remains-sellable-at-size gate — pure constant-product (x*y=k) exit depth probe.
# ---------------------------------------------------------------------------


def simulate_exit_slippage_bps(
    sol_reserve_lamports: int,
    token_reserve_base: int,
    tokens_in_base: int,
) -> int | None:
    """Dry-run a constant-product (x*y=k) SELL and return exit slippage in bps.

    The exit-side mirror of `aats.risk.liquidity_sanity.simulate_entry_slippage_bps`:
    selling `tokens_in_base` back into the pool moves the price DOWN; the returned slippage
    is how far the average realized price falls BELOW spot, in bps.  As the sell-side
    reserve drains toward a rug, slippage -> BPS_DENOM (10000) and the position becomes
    unsellable at size.  Integer/Decimal math throughout — NO float money.

    Returns slippage (>= 0) in bps, or None when the sim cannot be formed (degenerate
    reserves / non-positive fill) — the caller treats None as refuse-by-default (NOT
    sellable), never as a guess.
    """
    if sol_reserve_lamports <= 0 or token_reserve_base <= 0 or tokens_in_base <= 0:
        return None
    k = sol_reserve_lamports * token_reserve_base
    new_reserve_tok = token_reserve_base + tokens_in_base
    new_reserve_sol = k // new_reserve_tok
    sol_out = sol_reserve_lamports - new_reserve_sol
    if sol_out <= 0:
        return None
    spot_price = Decimal(sol_reserve_lamports) / Decimal(token_reserve_base)
    eff_price = Decimal(sol_out) / Decimal(tokens_in_base)
    if spot_price <= 0:
        return None
    # A well-formed constant-product SELL ALWAYS realizes strictly below spot.  If the sim
    # reports eff_price >= spot_price, the pool state is degenerate / drained (an LP-pulled
    # rug reserve too thin for the integer model to be meaningful) — refuse-by-default (NOT
    # sellable), never report a rosy 0-bps exit out of a rugged pool.
    if eff_price >= spot_price:
        return None
    # Selling realizes LESS than spot; slippage is the downward fraction from spot.
    slip_fraction = (spot_price - eff_price) / spot_price
    slip_bps = int(slip_fraction * BPS_DENOM)
    return max(0, slip_bps)


@dataclass(frozen=True)
class RegimeOutcomeSample:
    """ONE strictly-forward outcome-window observation feeding the LABEL (never a feature).

    Every sample is stamped at its on-chain `outcome_slot`; `build_regime_outcome` asserts
    `decision.slot < outcome_slot <= resolution.slot` so the label reads ONLY the forward
    horizon.  Reserves/size are INTEGER lamports/base units; `spot_price_ratio` is a ratio
    (sol-per-token) and may be float per data-models §0.
    """

    outcome_slot: int
    sol_reserve_lamports: int
    token_reserve_base: int
    holder_count: int
    spot_price_ratio: float


def remains_sellable_at_size(
    samples: list[RegimeOutcomeSample],
    *,
    decision_relevant_size_base: int,
    max_slippage_bps: int,
) -> tuple[bool, int | None]:
    """The "remains-sellable at decision-relevant size" gate.

    Returns `(remained_sellable, first_unsellable_slot)`.  The coin REMAINS SELLABLE iff,
    at EVERY forward outcome step, exiting a decision-relevant position (`decision_relevant_
    size_base` tokens) incurs simulated exit slippage <= `max_slippage_bps` AND the sim is
    well-formed (not None).  The FIRST step that fails (thin/collapsed reserve, degenerate
    sim) sets `first_unsellable_slot` and short-circuits — that is a sellability collapse,
    the signature of a rug-in-progress.

    An EMPTY trajectory is NOT sellable (refuse-by-default: we cannot prove exit liquidity
    existed).  No float money — reserves/size are ints, slippage is int bps.
    """
    if decision_relevant_size_base <= 0:
        raise ValueError(
            f"decision_relevant_size_base must be > 0 (a real exit size), got "
            f"{decision_relevant_size_base}"
        )
    if not samples:
        return (False, None)
    for s in samples:
        slip = simulate_exit_slippage_bps(
            s.sol_reserve_lamports, s.token_reserve_base, decision_relevant_size_base
        )
        if slip is None or slip > max_slippage_bps:
            return (False, s.outcome_slot)
    return (True, None)


# ---------------------------------------------------------------------------
# The pure classifier — EXHAUSTIVE + MUTUALLY EXCLUSIVE, priority-ordered.
# ---------------------------------------------------------------------------


def classify_regime(
    *,
    remained_sellable: bool,
    net_return_ratio: float,
    max_drawdown_ratio: float,
    holder_delta_ratio: float,
    loop: str = REGIME_LABELS_LOOP,
) -> RegimeLabel:
    """Classify the survivor/exit-horizon outcome into exactly one regime STATE.

    Inputs are RATIO summaries of the STRICTLY-FORWARD outcome window (measured against the
    decision anchor):
      - remained_sellable  : the remains-sellable-at-size gate result (bool)
      - net_return_ratio   : resolution price / decision-anchor price - 1  (ratio)
      - max_drawdown_ratio : worst drawdown-from-running-peak over the horizon, in [0,1]
      - holder_delta_ratio : (resolution holders - decision holders) / decision holders

    Priority order (guarantees MUTUAL EXCLUSIVITY and EXHAUSTIVENESS):
      1. NOT sellable                      -> RUG_IN_PROGRESS   (sellability dominates all)
      2. distribution signature            -> DISTRIBUTION
      3. accumulation signature            -> ACCUMULATION
      4. otherwise                         -> NEUTRAL           (total fall-through)

    SLOW-loop only: reuses the canonical `assert_slow_loop_only` guard.
    """
    assert_slow_loop_only(loop)

    # 1. Sellability collapse dominates: an un-exitable position is a rug-in-progress
    #    REGARDLESS of any price/holder reading (a "green" candle you cannot sell out of
    #    at size is not accumulation — it is a trap).
    if not remained_sellable:
        return RegimeLabel.RUG_IN_PROGRESS

    # 2. DISTRIBUTION: topping / markdown while still (barely) sellable.
    if (
        net_return_ratio <= DISTRIBUTION_NET_RETURN_MAX_RATIO
        or (max_drawdown_ratio >= DISTRIBUTION_DRAWDOWN_MIN_RATIO and net_return_ratio < 0.0)
        or holder_delta_ratio <= DISTRIBUTION_HOLDER_SHRINK_MAX_RATIO
    ):
        return RegimeLabel.DISTRIBUTION

    # 3. ACCUMULATION: net-positive AND holders growing AND drawdown controlled.
    if (
        net_return_ratio >= ACCUMULATION_NET_RETURN_MIN_RATIO
        and holder_delta_ratio >= ACCUMULATION_HOLDER_GROWTH_MIN_RATIO
        and max_drawdown_ratio <= ACCUMULATION_MAX_DRAWDOWN_RATIO
    ):
        return RegimeLabel.ACCUMULATION

    # 4. Everything else: ranging / undetermined.
    return RegimeLabel.NEUTRAL


# ---------------------------------------------------------------------------
# The label row — lives in its OWN dataset, joins to features by event_time ONLY.
# ---------------------------------------------------------------------------


class RegimeLabelLeakError(AssertionError):
    """Raised when a regime label reads outside its strictly-forward outcome window.

    A label may read the DECISION ANCHOR (its reference point) and the STRICTLY-FORWARD
    outcome window `(decision.slot, resolution.slot]`.  A sample at or before the decision
    slot, or beyond the resolution slot, or out of increasing-slot order, is a leak — the
    label would be conditioning on non-forward or beyond-horizon data.  Loud failure.
    """


@dataclass(frozen=True)
class RegimeOutcome:
    """The post-hoc regime LABEL row (its OWN dataset; joins to features by event_time ONLY).

    Mirrors the frozen `LaunchOutcome` §3A discipline: a stamped, strictly-later
    `resolution_event_time` (the horizon proof) and a declared horizon.  It carries the
    STATE, the remains-sellable gate result, the derived de-risk action, and the taxonomy
    version — and NOTHING that is a success rate, win-rate, price, size, or realized
    multiple (HONESTY CLAUSE + asymmetric-trust law).  FORBIDDEN from any feature matrix by
    `assert_no_regime_label_taint`.
    """

    mint: str
    event_time: EventTime               # the DECISION ANCHOR — labels join to features on this ONLY
    label_horizon_h_slots: int          # H, declared per surface up front
    resolution_event_time: EventTime    # = event_time + H, STAMPED and strictly later
    taxonomy_version: str
    label: RegimeLabel
    remained_sellable: bool
    first_unsellable_slot: int | None   # the slot sellability collapsed (None if never)
    is_bootstrap_not_real: bool

    def __post_init__(self) -> None:
        # Horizon proof: resolution STRICTLY later than the decision anchor (slot + block
        # time), and the declared horizon must match — mirrors the LaunchOutcome invariant.
        d_slot = self.event_time.slot
        r_slot = self.resolution_event_time.slot
        d_bt = self.event_time.block_time_ms
        r_bt = self.resolution_event_time.block_time_ms
        if self.label_horizon_h_slots <= 0:
            raise ValueError(
                f"label_horizon_h_slots must be > 0 (a real forward horizon), got "
                f"{self.label_horizon_h_slots}"
            )
        if r_slot != d_slot + self.label_horizon_h_slots:
            raise RegimeLabelLeakError(
                f"resolution slot {r_slot} != decision slot {d_slot} + horizon "
                f"{self.label_horizon_h_slots}. The stamped horizon does not match the "
                "declared one — the label's forward-resolution proof is inconsistent."
            )
        if r_slot <= d_slot or r_bt <= d_bt:
            raise RegimeLabelLeakError(
                f"resolution_event_time (slot={r_slot}, bt={r_bt}) is NOT strictly after "
                f"the decision anchor (slot={d_slot}, bt={d_bt}). A regime label MUST "
                "resolve forward; a non-forward resolution is a leak."
            )
        if self.first_unsellable_slot is None and not self.remained_sellable:
            raise ValueError(
                "remained_sellable=False requires a first_unsellable_slot (the slot it "
                "collapsed); got None."
            )
        if self.first_unsellable_slot is not None and self.remained_sellable:
            raise ValueError(
                "remained_sellable=True is inconsistent with a first_unsellable_slot; "
                f"got {self.first_unsellable_slot}."
            )

    @property
    def derisk_action(self) -> RegimeDeRiskAction:
        """The de-risk action this STATE authorizes (bullish/ranging -> NONE, provably inert)."""
        return REGIME_DERISK_ACTION[self.label]


def build_regime_outcome(
    *,
    mint: str,
    decision_event_time: EventTime,
    resolution_event_time: EventTime,
    label_horizon_h_slots: int,
    decision_reference_price_ratio: float,
    decision_reference_holder_count: int,
    outcome_samples: list[RegimeOutcomeSample],
    decision_relevant_size_base: int,
    max_slippage_bps: int,
    loop: str = REGIME_LABELS_LOOP,
    is_bootstrap_not_real: bool = True,
) -> RegimeOutcome:
    """Build a leak-free `RegimeOutcome` from the STRICTLY-FORWARD outcome window.

    The label MAY read the DECISION ANCHOR references (a decision-time price ratio + holder
    count — the point it measures forward FROM) and the strictly-forward outcome samples.
    It may NEVER read a bar at/before the decision slot or beyond the resolution slot: every
    `outcome_sample.outcome_slot` is asserted in `(decision.slot, resolution.slot]` and in
    strictly increasing order, else `RegimeLabelLeakError`.  This is the point-in-time gate
    (T-300a): outcome bars are gated at `outcome_slot` vs event-time, no wall-clock, no
    forward-fill.

    Steps: forward-window gate -> remains-sellable-at-size gate -> path-summary ratios
    (net return / drawdown-from-peak / holder delta, all vs the anchor) -> classify.
    Returns the `RegimeOutcome` label row.  SLOW-loop only (reuses assert_slow_loop_only).
    """
    assert_slow_loop_only(loop)

    if decision_reference_price_ratio <= 0.0:
        raise ValueError(
            f"decision_reference_price_ratio must be > 0, got {decision_reference_price_ratio}"
        )
    if decision_reference_holder_count <= 0:
        raise ValueError(
            f"decision_reference_holder_count must be > 0, got {decision_reference_holder_count}"
        )
    if not outcome_samples:
        raise RegimeLabelLeakError(
            "outcome_samples is empty — a regime label needs a strictly-forward outcome "
            "window to resolve; refuse-by-default (never fabricate one)."
        )

    d_slot = decision_event_time.slot
    r_slot = resolution_event_time.slot

    # Forward-window gate: every sample strictly after the decision, no later than the
    # resolution, in strictly increasing slot order (no dup, no forward-fill from a future
    # bar re-writing an earlier step).
    prev_slot: int | None = None
    for s in outcome_samples:
        if s.outcome_slot <= d_slot:
            raise RegimeLabelLeakError(
                f"outcome sample slot {s.outcome_slot} is <= the decision slot {d_slot}. "
                "The label may only read the STRICTLY-forward outcome window; a sample at/"
                "before the decision is a leak."
            )
        if s.outcome_slot > r_slot:
            raise RegimeLabelLeakError(
                f"outcome sample slot {s.outcome_slot} is beyond the resolution slot "
                f"{r_slot}. The label may not read past its declared horizon."
            )
        if prev_slot is not None and s.outcome_slot <= prev_slot:
            raise RegimeLabelLeakError(
                f"outcome samples out of order: slot {s.outcome_slot} <= previous "
                f"{prev_slot}. Forward samples must be strictly increasing in slot."
            )
        prev_slot = s.outcome_slot

    # Remains-sellable-at-size gate.
    remained_sellable, first_unsellable_slot = remains_sellable_at_size(
        outcome_samples,
        decision_relevant_size_base=decision_relevant_size_base,
        max_slippage_bps=max_slippage_bps,
    )

    # Path-summary ratios vs the decision anchor (dimensionless — never money).
    last = outcome_samples[-1]
    net_return_ratio = last.spot_price_ratio / decision_reference_price_ratio - 1.0
    holder_delta_ratio = (
        last.holder_count - decision_reference_holder_count
    ) / decision_reference_holder_count

    # Drawdown-from-running-peak across the anchor + forward path (causal running max).
    running_peak = decision_reference_price_ratio
    max_drawdown_ratio = 0.0
    for s in outcome_samples:
        if s.spot_price_ratio > running_peak:
            running_peak = s.spot_price_ratio
        if running_peak > 0.0:
            dd = (running_peak - s.spot_price_ratio) / running_peak
            if dd > max_drawdown_ratio:
                max_drawdown_ratio = dd

    label = classify_regime(
        remained_sellable=remained_sellable,
        net_return_ratio=net_return_ratio,
        max_drawdown_ratio=max_drawdown_ratio,
        holder_delta_ratio=holder_delta_ratio,
        loop=loop,
    )

    return RegimeOutcome(
        mint=mint,
        event_time=decision_event_time,
        label_horizon_h_slots=label_horizon_h_slots,
        resolution_event_time=resolution_event_time,
        taxonomy_version=REGIME_LABEL_TAXONOMY_VERSION,
        label=label,
        remained_sellable=remained_sellable,
        first_unsellable_slot=first_unsellable_slot,
        is_bootstrap_not_real=is_bootstrap_not_real,
    )


# ---------------------------------------------------------------------------
# Lineage-taint guard — REUSES the frozen assert_no_label_taint + regime-label names.
# ---------------------------------------------------------------------------

# Regime-label column names that must NEVER appear in a feature matrix (the model-side
# analogue of LABEL_FIELD_NAMES; kept HERE, not merged into the frozen provenance set,
# because that set is a frozen contract — CP-04's ADR owns any contract addition).
REGIME_LABEL_FIELD_NAMES: frozenset[str] = frozenset(
    {
        "regime_label",
        "regime",
        "taxonomy_version",
        "remained_sellable",
        "first_unsellable_slot",
        "regime_derisk_action",
        "regime_resolution_event_time",
    }
)


def assert_no_regime_label_taint(columns: list[str] | tuple[str, ...]) -> None:
    """FAIL the build if any regime-label OR frozen-label column reaches the feature matrix.

    Layers on top of the frozen `assert_no_label_taint` (truth_*/LaunchOutcome names) with
    the regime-specific label names.  A regime STATE column that somehow reached a feature
    matrix taints the lineage and fails the build (ProvenanceTaintError) — not a reviewer's
    eye.
    """
    cols = list(columns)
    # 1. Reuse the frozen guard (truth_* prefix + LaunchOutcome label names).
    assert_no_label_taint(cols)
    # 2. Regime-specific label names.
    overlap = sorted(set(cols) & REGIME_LABEL_FIELD_NAMES)
    if overlap:
        raise ProvenanceTaintError(
            guard_name="regime_label_column_in_feature_frame",
            detail=(
                f"Regime-label column(s) {overlap} reached the regime-model feature matrix. "
                "Regime STATE labels live ONLY in the regime labels dataset and join to "
                "features by event_time — they must NEVER enter a feature matrix. "
                "Fix: drop them before building the matrix."
            ),
        )


# ---------------------------------------------------------------------------
# Leak-free join (by event_time ONLY) -> frozen JoinedExample, for the reused leak audit.
# ---------------------------------------------------------------------------


def regime_label_to_joined_example(
    *,
    feature_row: list[float],
    feature_event_time: EventTime,
    outcome: RegimeOutcome,
) -> JoinedExample:
    """Adapt a (feature_row, RegimeOutcome) pair to the frozen JoinedExample shape.

    The join is BY EVENT-TIME ONLY: the feature's event_time must key-match the label's
    decision anchor (`event_time_key` equality), else there is no legal join.  The returned
    JoinedExample carries the label as its integer code (`REGIME_LABEL_CODES`) and the
    stamped resolution — so `assert_event_time_leq_decision` (REUSED unchanged) can run the
    feature<=decision<resolution leak audit on the regime dataset.
    """
    if event_time_key(feature_event_time) != event_time_key(outcome.event_time):
        raise RegimeLabelLeakError(
            f"regime label join key mismatch: feature event_time "
            f"{event_time_key(feature_event_time)} != label decision anchor "
            f"{event_time_key(outcome.event_time)}. Labels join to features by event_time "
            "ONLY."
        )
    return JoinedExample(
        feature_row=feature_row,
        label=REGIME_LABEL_CODES[outcome.label],
        feature_event_time=feature_event_time,
        decision_event_time=outcome.event_time,
        resolution_event_time=outcome.resolution_event_time,
    )


def join_regime_labels_by_event_time(
    feature_rows: list[tuple[EventTime, list[float]]],
    outcomes: list[RegimeOutcome],
) -> list[JoinedExample]:
    """Join a regime labels dataset to feature rows BY EVENT-TIME ONLY.

    `feature_rows` is a list of (event_time, feature_row) — the decision-time feature side.
    Labels are indexed by `event_time_key` and joined to each feature by that key ONLY.  A
    feature with no matching label is DROPPED (CENSORED-equivalent), never defaulted.  The
    result is a list of frozen `JoinedExample` ready for the REUSED leak audit
    (`assert_event_time_leq_decision`).
    """
    label_index: dict[tuple[int, int], RegimeOutcome] = {
        event_time_key(o.event_time): o for o in outcomes
    }
    joined: list[JoinedExample] = []
    for feat_et, row in feature_rows:
        key = event_time_key(feat_et)
        outcome = label_index.get(key)
        if outcome is None:
            continue  # no label -> CENSORED-equivalent, dropped (never defaulted)
        joined.append(
            regime_label_to_joined_example(
                feature_row=row, feature_event_time=feat_et, outcome=outcome
            )
        )
    return joined
