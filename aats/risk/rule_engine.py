"""T-324 — Hierarchical risk RULE ENGINE (deterministic, ordered, de-risk-only).

THE TOPOLOGY
------------
A single deterministic evaluator that runs in the FAST loop (<100ms), evaluated
HIGHEST-PRIORITY-FIRST.  EVERY branch may ONLY exit or de-risk — NEVER add
exposure.  The three rules, in strict priority order:

  (iii) LLM `narrative_failure` catastrophic exit  — HIGHEST priority.
        The SLOW-loop reasoner, when it has exposed sentiment as
        manufactured/abandoned, can force a FULL FLATTEN.  The FAST loop NEVER
        awaits the LLM: this rule reads a PRE-SET flag (a de-risk capability the
        SLOW loop wrote), it does not call the LLM.  If set, it short-circuits to
        a full exit and nothing lower is consulted.

  (i)   MANDATORY HARD STOP  — non-negotiable.  Distance fixed at entry, NEVER
        widened.  Fires a full exit when the point-in-time mark breaches the
        fixed trigger (StopState.is_breached).  Owned by survivable_stop.py; this
        engine sequences it at priority 2.

  (ii)  TAKE-PROFIT / SCALE-OUT  — LOWEST priority.  The ONLY branch that books
        gains.  It emits a partial REDUCE (a scale-out), never an add.

NON-WAIVABLE LAWS (encoded as types/structure, not comments)
------------------------------------------------------------
  * EVERY outcome is de-risk.  The engine returns ONLY a de-risk Intent
    (ExitIntent / ReduceIntent / VetoIntent) or NO-OP.  It holds a
    DeRiskIntentFactory — it has NO EntryIntent constructor, so a branch that
    adds risk is not an expressible value here (ADR-0006).  The entry decision
    lives elsewhere (the cost-gated SNIPE path); this engine only ever sheds
    risk on an OPEN position.
  * Asymmetric LLM trust.  The LLM reaches this engine ONLY as a pre-set
    de-risk flag (`narrative_failure`).  It cannot size up, widen/move a stop,
    add leverage, override the hard stop, or reset anything — none of those are
    inputs the engine accepts, and the only LLM-derived input is a boolean that
    can FORCE AN EXIT (de-risk).  The FAST loop never blocks on an LLM call.
  * Highest-priority-first, short-circuit.  A higher rule firing means the lower
    rules are not consulted for that tick — a catastrophic exit is never
    overridden by a take-profit scale-out.
  * Point-in-time.  Every decision is a pure function of (fixed stop trigger,
    point-in-time mark price, pre-set flags, TP config).  No wall-clock, no
    lookahead.  Same code live and in backtest.
  * Money is integer lamports / Decimal.  NEVER float.

THIS IS THE FAST LOOP
---------------------
The evaluator NEVER awaits an LLM and NEVER awaits an RPC on the DECISION.  The
decision is pure; the only IO is the exit handoff AFTER the decision, via the
ExecutionVenue seam (owned by solana-execution-engineer, behind the DRY_RUN
gate).  This module decides; it does not build/sign/land.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import IntEnum, StrEnum

from aats.contracts.intents import (
    DeRiskIntentFactory,
    ExitIntent,
    ReduceIntent,
)
from aats.risk.survivable_stop import StopState

# Full flatten = 100% of remaining (bps).  A partial scale-out is < this.
_FULL_EXIT_BPS = 10_000


class RulePriority(IntEnum):
    """Strict priority order — LOWER number = HIGHER priority (evaluated first).

    There is NO rule that adds exposure.  The enum is the complete, ordered set
    of de-risk rules; the engine walks them in ascending priority value.
    """

    NARRATIVE_FAILURE = 1  # (iii) LLM catastrophic exit — HIGHEST
    HARD_STOP = 2  # (i)   mandatory hard stop — non-negotiable
    TAKE_PROFIT = 3  # (ii)  scale-out — LOWEST, the only gain-booking branch


class RuleOutcomeKind(StrEnum):
    """What the engine decided for one tick.  ALL are de-risk or no-op."""

    NO_OP = "no_op"  # nothing fired — hold (the default)
    FULL_EXIT = "full_exit"  # narrative-failure OR hard-stop → flatten 100%
    SCALE_OUT = "scale_out"  # take-profit partial reduce (< 100%)


def _to_decimal_price(value: object, field: str) -> Decimal:
    """Coerce a price to Decimal, REJECTING float (data-models.md §0)."""
    if isinstance(value, float):
        raise TypeError(
            f"{field} must be Decimal/int/str (SOL-lamports per token base unit), "
            f"got float {value!r} (data-models.md §0)."
        )
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int | str):
        return Decimal(value)
    raise TypeError(f"{field} must be Decimal/int/str, got {type(value).__name__}.")


@dataclass(frozen=True)
class TakeProfitLevel:
    """ONE take-profit rung: at/above `trigger_price`, scale out `fraction_bps`.

    `fraction_bps` is a REDUCTION of the remaining position (1..9999 — a partial
    scale-out; a full exit is the hard-stop/narrative path, not a TP rung).  This
    is the ONLY branch that books gains, and it can only ever REDUCE — there is
    no field that increases a position.
    """

    trigger_price: Decimal
    fraction_bps: int

    def __post_init__(self) -> None:
        if isinstance(self.trigger_price, float):
            raise TypeError("TakeProfitLevel.trigger_price must be Decimal, not float.")
        if self.trigger_price <= 0:
            raise ValueError("TakeProfitLevel.trigger_price must be > 0.")
        if isinstance(self.fraction_bps, float):
            raise TypeError("TakeProfitLevel.fraction_bps must be int (bps), not float.")
        if not (1 <= self.fraction_bps <= 9_999):
            raise ValueError(
                "TakeProfitLevel.fraction_bps must be in [1, 9999] (a partial scale-out; "
                f"a full exit is the hard-stop/narrative path), got {self.fraction_bps}."
            )

    def is_hit(self, mark_price: Decimal | int | str) -> bool:
        """Pure, point-in-time: True iff mark has risen TO OR ABOVE the trigger."""
        return _to_decimal_price(mark_price, "mark_price") >= self.trigger_price


@dataclass(frozen=True)
class RuleInputs:
    """Everything the engine evaluates for ONE open position at ONE tick.

    EVERY field is either a fixed-at-entry value, a point-in-time mark, or a
    PRE-SET de-risk flag.  There is NO field that can size up, widen a stop, or
    add exposure — the inputs themselves cannot express a risk-increase.

    narrative_failure_flag:  the LLM's catastrophic-exit signal, PRE-SET by the
        SLOW loop (the FAST loop reads it, never calls the LLM).  True ⇒ force a
        full flatten (priority iii).  This is the ONLY LLM-derived input and it
        can only DE-RISK.
    stop:  the FIXED-at-entry hard stop (priority i).  Its trigger is immutable
        here — the engine never widens it; tightening is a separate de-risk op on
        the StopState itself.
    mark_price:  the point-in-time mark (Decimal SOL-lamports/token base unit).
    take_profit_levels:  the TP ladder (priority ii); the lowest-priority rule.
    """

    mint: str
    block_time_ms: int  # event-time (UTC ms) — AUTHORITATIVE, never wall-clock
    mark_price: Decimal
    stop: StopState
    narrative_failure_flag: bool = False
    take_profit_levels: tuple[TakeProfitLevel, ...] = ()

    def __post_init__(self) -> None:
        if isinstance(self.mark_price, float):
            raise TypeError("RuleInputs.mark_price must be Decimal, not float (data-models.md §0).")
        if isinstance(self.block_time_ms, float):
            raise TypeError("RuleInputs.block_time_ms must be int (event-time ms), not float.")
        if self.block_time_ms < 0:
            raise ValueError("RuleInputs.block_time_ms must be non-negative.")
        if self.stop.mint != self.mint:
            raise ValueError(
                f"RuleInputs.stop.mint ({self.stop.mint!r}) must match mint ({self.mint!r})."
            )


@dataclass(frozen=True)
class RuleOutcome:
    """The engine's decision for one tick — a de-risk Intent or a no-op.

    `intent` is None for NO_OP; otherwise it is an ExitIntent (full flatten) or a
    ReduceIntent (scale-out).  It is NEVER an EntryIntent — that type is not
    constructible on this path.  `priority` records which rule fired (for audit /
    telemetry), `reason` is a human-readable audit string.
    """

    kind: RuleOutcomeKind
    intent: ExitIntent | ReduceIntent | None
    priority: RulePriority | None
    reason: str

    def is_exit(self) -> bool:
        return self.kind in (RuleOutcomeKind.FULL_EXIT, RuleOutcomeKind.SCALE_OUT)


class HierarchicalRiskRuleEngine:
    """Ordered, deterministic, de-risk-only rule evaluator (FAST loop, <100ms).

    Evaluates HIGHEST-PRIORITY-FIRST and SHORT-CIRCUITS on the first rule that
    fires.  Holds a DeRiskIntentFactory — it can ONLY produce ExitIntent /
    ReduceIntent.  There is no method here that constructs an EntryIntent, sizes
    a trade, widens a stop, or resets a breaker.  The engine sheds risk; it never
    adds it.

    The evaluation is a PURE function of RuleInputs (no IO, no LLM call, no
    wall-clock).  `agent-orchestration-engineer` drives it on the FAST loop and
    routes the returned de-risk Intent to the ExecutionVenue seam; THIS module
    only decides.
    """

    def __init__(self, *, factory: DeRiskIntentFactory | None = None) -> None:
        self._factory = factory or DeRiskIntentFactory()

    def evaluate(self, inputs: RuleInputs) -> RuleOutcome:
        """Run the ordered hierarchy for one open position at one tick.

        Returns the de-risk RuleOutcome of the HIGHEST-priority rule that fired,
        or NO_OP if none did.  Short-circuits: once a rule fires, lower rules are
        not consulted (a catastrophic exit is never overridden by a take-profit).
        """
        # (iii) HIGHEST: LLM narrative-failure catastrophic exit.  Reads a PRE-SET
        # flag — NEVER calls the LLM.  Full flatten, short-circuit.
        if inputs.narrative_failure_flag:
            return self._full_exit(
                inputs,
                priority=RulePriority.NARRATIVE_FAILURE,
                reason="narrative_failure: SLOW-loop reasoner forced a catastrophic exit "
                "(manufactured/abandoned sentiment)",
            )

        # (i) MANDATORY HARD STOP — non-negotiable, fixed-at-entry trigger.
        if inputs.stop.is_breached(inputs.mark_price):
            return self._full_exit(
                inputs,
                priority=RulePriority.HARD_STOP,
                reason=(
                    f"hard_stop_breach: mark ({inputs.mark_price}) <= fixed trigger "
                    f"({inputs.stop.trigger_price})"
                ),
            )

        # (ii) LOWEST: TAKE-PROFIT / SCALE-OUT — the only gain-booking branch.
        # Fire the HIGHEST hit rung (largest trigger that the mark has reached),
        # so a single tick that jumps past several rungs books the most advanced
        # scale-out (still a partial reduce, never an add).
        hit = [lvl for lvl in inputs.take_profit_levels if lvl.is_hit(inputs.mark_price)]
        if hit:
            level = max(hit, key=lambda lvl: lvl.trigger_price)
            return self._scale_out(inputs, level)

        # Nothing fired — hold.
        return RuleOutcome(
            kind=RuleOutcomeKind.NO_OP,
            intent=None,
            priority=None,
            reason="no_rule_fired: holding (no narrative failure, no stop breach, no TP hit)",
        )

    # -- de-risk Intent construction (factory has NO EntryIntent path) ---------

    def _full_exit(self, inputs: RuleInputs, *, priority: RulePriority, reason: str) -> RuleOutcome:
        """Build a FULL-flatten ExitIntent (de-risk).  100% of remaining."""
        client_intent_id = f"rule_exit:{priority.name.lower()}:{inputs.mint}:{inputs.block_time_ms}"
        intent: ExitIntent = self._factory.exit(
            client_intent_id=client_intent_id,
            mint=inputs.mint,
            fraction_bps=_FULL_EXIT_BPS,
            # Secure (private) routing on a forced/stop exit — sandwich-safe.
            exit_mode="secure",
            reason=reason,
        )
        return RuleOutcome(
            kind=RuleOutcomeKind.FULL_EXIT,
            intent=intent,
            priority=priority,
            reason=reason,
        )

    def _scale_out(self, inputs: RuleInputs, level: TakeProfitLevel) -> RuleOutcome:
        """Build a partial REDUCE (take-profit scale-out) — the only gain branch.

        This is a REDUCTION (1..9999 bps of remaining); the ReduceIntent contract
        refuses anything outside [1, 10000].  It can NEVER add to the position.
        """
        client_intent_id = f"rule_tp:{inputs.mint}:{inputs.block_time_ms}:{level.trigger_price}"
        reason = (
            f"take_profit_hit: mark ({inputs.mark_price}) >= TP trigger "
            f"({level.trigger_price}); scaling out {level.fraction_bps} bps"
        )
        intent: ReduceIntent = self._factory.reduce(
            client_intent_id=client_intent_id,
            mint=inputs.mint,
            fraction_bps=level.fraction_bps,
            reason=reason,
        )
        return RuleOutcome(
            kind=RuleOutcomeKind.SCALE_OUT,
            intent=intent,
            priority=RulePriority.TAKE_PROFIT,
            reason=reason,
        )
