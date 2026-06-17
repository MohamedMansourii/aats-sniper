"""T-326 — Durable resting LIMIT + DCA orders that FIRE even when the operator is OFFLINE.

THE FEATURE (BUILD-DIRECTIVE-v3 §"Competitive feature target")
--------------------------------------------------------------
"Limit + DCA resting orders that fire even when the operator is offline."  A
resting order is a STANDING instruction parked in a DURABLE store (and, for a
SELL/EXIT, on a venue-native off-box seam) so it fires on a price/schedule trigger
WITHOUT the bot's main loop being alive.  This is the same off-box survivability
principle as the Layer-1 venue-native stop (ADR-0008 / `venue_native_stop.py`):
a protection that depends on this process being alive is not a protection.

THE NON-WAIVABLE DISCIPLINE — DE-RISK *DIRECTION* (BUILD-DIRECTIVE HARD RULES)
-----------------------------------------------------------------------------
Every order, signal, and command may only DE-RISK.  A resting order has a
DIRECTION, and the direction governs what it is allowed to do at fire time:

  * A resting SELL / EXIT / REDUCE is ALWAYS allowed.  It only sheds exposure.
    It fires straight through the de-risk factory (ExitIntent / ReduceIntent) —
    no entry gate, because it adds no risk.

  * A resting BUY is a sized ENTRY that is DEFERRED, not exempt.  At FIRE TIME it
    MUST still pass the FULL entry stack exactly as a live snipe would:
      1. the pre-trade SAFETY gate (T-323 PreTradeSafetyGate) — refuse-by-default;
      2. the circuit breaker (entries_allowed()) — a TRIPPED breaker blocks it;
      3. fractional-Kelly SIZING with the per-trade + aggregate caps (T-324);
      4. the COST gate (T-324 CostAwareEntryGate) — edge must exceed round-trip cost.
    A resting BUY is NEVER a risk-increase bypass.  Parking an order in advance
    must NOT let it skip a single gate that a just-in-time entry would face.  The
    EntryIntent is the ONLY risk-increasing intent and it remains constructible
    ONLY through the cost-gated path (contracts/intents.py / ADR-0006); this module
    holds NO EntryIntent constructor of its own — it INVOKES the gated builder the
    SNIPE lane owns and refuses to fire the buy if ANY gate says no.

STRUCTURAL ENFORCEMENT (types, not comments)
--------------------------------------------
  * `RestingSellOrder` carries a DeRiskIntentFactory and can ONLY emit
    ExitIntent / ReduceIntent.  There is no buy method on it.
  * `RestingBuyOrder` carries NO factory and NO EntryIntent constructor.  It can
    only produce a *request to evaluate* (a `BuyFireContext`); turning that into an
    actual EntryIntent requires the injected `EntryGateChain`, which runs all four
    gates and refuses-by-default.  A BUY that cannot pass every gate fires NOTHING.
  * The two are distinct types in a CLOSED union (`RestingOrder`); a future
    "increase position on a schedule with no gate" is not an expressible value.

OFFLINE / DURABILITY (fires with the operator OFFLINE)
------------------------------------------------------
  * `RestingOrderStore` (Protocol) is the DURABLE seam — Redis/Parquet in prod,
    in-memory reference here.  Orders SURVIVE a restart: the store is re-read and
    the standing orders are still armed.  A SELL/EXIT additionally rests on the
    venue-native off-box seam so it fires even with the whole bot dead (the same
    `RestingStopVenue` keeper used by the survivable stop).
  * `RestingOrderBook.evaluate_tick(...)` is the deterministic, point-in-time
    evaluator the FAST loop (or an off-box keeper) runs each tick.  It is pure
    w.r.t. its inputs (no wall-clock, no lookahead): a trigger is a comparison of
    the FIXED order trigger against the point-in-time mark / event-time step.

POINT-IN-TIME (C-5)
-------------------
Every trigger decision is a pure function of (fixed order spec, point-in-time mark
price, event-time step counter).  No wall-clock; no future data.  The SAME code
runs live and in backtest.  Each fire stamps the deterministic client_intent_id
with the event-time block_time_ms (idempotent replay, FR-024).

MONEY (data-models.md §0)
-------------------------
All prices are Decimal (SOL-lamports per token base unit); all sizes are integer
lamports/base-units.  Float money is rejected at every boundary.

DRY-RUN (BUILD-DIRECTIVE non-waivable #1)
-----------------------------------------
This module DECIDES and emits Intents.  It never builds/signs/lands — the venue
handoff is the ExecutionVenue seam (owned by solana-execution-engineer), behind
the DRY_RUN gate.  The reference in-memory store/keeper below NEVER touch a
network.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field, replace
from decimal import Decimal
from enum import StrEnum
from typing import Protocol, runtime_checkable

from aats.contracts.intents import (
    DeRiskIntentFactory,
    EntryIntent,
    ExitIntent,
    ReduceIntent,
)

_FULL_EXIT_BPS = 10_000


# --------------------------------------------------------------------------- #
# Money / type guards (consistent with the rest of the risk lane).
# --------------------------------------------------------------------------- #


def _to_decimal_price(value: object, field_name: str) -> Decimal:
    """Coerce a price/multiple to Decimal, REJECTING float (data-models.md §0)."""
    if isinstance(value, bool):  # bool is an int subclass — refuse explicitly
        raise TypeError(f"{field_name} must be Decimal/int/str, got bool {value!r}.")
    if isinstance(value, float):
        raise TypeError(
            f"{field_name} must be Decimal/int/str (money-derived exact), got float "
            f"{value!r}.  Float money is forbidden (data-models.md §0)."
        )
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int | str):
        return Decimal(value)
    raise TypeError(f"{field_name} must be Decimal/int/str, got {type(value).__name__}.")


def _reject_float_int(value: object, field_name: str) -> None:
    if isinstance(value, bool):
        raise TypeError(f"{field_name} must be a plain int, got bool {value!r}.")
    if isinstance(value, float):
        raise TypeError(
            f"{field_name} must be int (not float) — money is integer base units "
            f"(data-models.md §0); got {value!r}."
        )


# --------------------------------------------------------------------------- #
# Order direction + trigger semantics.
# --------------------------------------------------------------------------- #


class RestingSide(StrEnum):
    """The direction of a resting order — governs which gates apply at fire time.

    SELL / EXIT / REDUCE  — DE-RISK; always allowed; no entry gate.
    BUY                   — a DEFERRED sized ENTRY; MUST pass the full entry stack
                            (safety + breaker + sizing caps + cost) at fire time.
    """

    SELL = "SELL"  # de-risk: full exit (a resting take-profit / stop-limit / panic SELL)
    REDUCE = "REDUCE"  # de-risk: partial scale-out
    BUY = "BUY"  # deferred sized entry — gated at fire time, NEVER a bypass


class TriggerDirection(StrEnum):
    """Which way the mark must cross the trigger for the order to be eligible.

    AT_OR_BELOW — fire when mark <= trigger (a limit BUY dip; a stop-limit SELL).
    AT_OR_ABOVE — fire when mark >= trigger (a take-profit SELL; a breakout BUY).
    """

    AT_OR_BELOW = "AT_OR_BELOW"
    AT_OR_ABOVE = "AT_OR_ABOVE"


def _trigger_met(direction: TriggerDirection, mark: Decimal, trigger: Decimal) -> bool:
    """Pure point-in-time trigger test.  No wall-clock, no lookahead."""
    if direction is TriggerDirection.AT_OR_BELOW:
        return mark <= trigger
    return mark >= trigger


# --------------------------------------------------------------------------- #
# The buy-fire gate chain — the structural proof a resting BUY cannot bypass.
# We INVOKE the SNIPE lane's gates; we do not re-implement or weaken them.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class BuyFireContext:
    """The point-in-time facts a resting BUY needs to be RE-EVALUATED at fire time.

    A resting BUY does NOT carry a pre-baked EntryIntent (that would be a stale,
    un-gated entry).  Instead it carries — and at fire time the producer supplies —
    the CURRENT inputs to every gate, so the buy is judged against the market as it
    is WHEN IT FIRES, not as it was when parked.  Every field is point-in-time.

    `safety_inputs`/`cost_inputs` are opaque to this module (typed where they are
    produced); we pass them through to the injected gate chain, which owns the
    concrete types (PreTradeSafetyGate.SafetyInputs, CostStack, model p/uncertainty).
    """

    mint: str
    block_time_ms: int  # event-time of the fire tick — AUTHORITATIVE clock
    # The inputs each gate needs, supplied fresh at fire time (point-in-time):
    safety_inputs: object  # -> PreTradeSafetyGate.evaluate_fast
    p_calibrated: float  # -> FractionalKellySizer.size
    uncertainty: float  # -> FractionalKellySizer.size (higher shrinks)
    current_aggregate_lamports: int  # -> aggregate-exposure cap
    cost_stack: object  # -> CostAwareEntryGate.evaluate (a contracts.intents.CostStack)
    # Creator wallet of the candidate token (point-in-time decoded fact), carried so
    # the DENYLIST pre-filter (E2) can veto a known-scam CREATOR wallet at fire time,
    # not just a known-scam mint.  None => no creator known => the denylist simply has
    # no creator opinion (the mint check + safety gate still run).  Membership is a
    # property of the key string, never of any future price (no compute-time leak).
    creator: str | None = None
    # Everything else needed to actually BUILD the (gated) EntryIntent lives behind
    # the injected builder; this module never constructs EntryIntent itself.
    entry_build_params: object = None

    def __post_init__(self) -> None:
        _reject_float_int(self.block_time_ms, "BuyFireContext.block_time_ms")
        _reject_float_int(
            self.current_aggregate_lamports, "BuyFireContext.current_aggregate_lamports"
        )
        if self.block_time_ms < 0:
            raise ValueError("BuyFireContext.block_time_ms must be non-negative (event-time).")
        if self.current_aggregate_lamports < 0:
            raise ValueError("BuyFireContext.current_aggregate_lamports must be >= 0.")


class BuyFireRejection(StrEnum):
    """Why a resting BUY was REFUSED at fire time (auditable; refuse-by-default)."""

    DENYLISTED = "denylisted"  # E2 pre-filter: creator/mint on the scam denylist => veto
    SAFETY_GATE = "safety_gate_reject"  # T-323 pre-trade gate rejected (honeypot/flags/stale)
    BREAKER_TRIPPED = "breaker_tripped"  # circuit breaker is TRIPPED — no new entries
    ZERO_SIZE = "zero_size"  # sizing returned 0 (no edge / caps exhausted) — no buy
    COST_GATE = "cost_gate_reject"  # T-324 cost gate rejected (edge <= round-trip cost)
    BUILD_REFUSED = "build_refused"  # the gated EntryIntent builder itself refused
    NO_GATE_CHAIN = "no_gate_chain"  # no chain wired => refuse-by-default (cannot prove safe)


@dataclass(frozen=True)
class BuyFireResult:
    """The outcome of attempting to fire a resting BUY.

    EXACTLY ONE of (`intent`, `rejection`) is set.  `intent` is a fully-gated
    EntryIntent — it exists ONLY because every gate passed.  `rejection` is the
    refuse-by-default reason when any gate said no (or no chain was wired).  There
    is no third "fired without gates" outcome — that state is unrepresentable.
    """

    mint: str
    fired: bool
    intent: EntryIntent | None
    rejection: BuyFireRejection | None
    size_lamports: int  # the gated size (0 on any rejection)
    detail: str

    def __post_init__(self) -> None:
        _reject_float_int(self.size_lamports, "BuyFireResult.size_lamports")
        if self.fired:
            if self.intent is None or self.rejection is not None:
                raise ValueError("A fired BuyFireResult must carry an intent and no rejection.")
        else:
            if self.intent is not None or self.rejection is None:
                raise ValueError("A non-fired BuyFireResult must carry a rejection and no intent.")


@runtime_checkable
class EntryGateChain(Protocol):
    """The FULL entry stack a resting BUY must pass at fire time.

    This is the SAME stack a live snipe runs — we INVOKE it, we never weaken it.
    The concrete implementation (`GatedEntryFireChain` below) wires the real
    T-323 safety gate, the circuit breaker, the T-324 sizer + cost gate, and the
    SNIPE lane's gated EntryIntent builder.  Refuse-by-default: anything it cannot
    fully pass returns a non-fired BuyFireResult.

    A resting BUY holds a reference to this Protocol — NOT to an EntryIntent
    constructor — so the only way a parked buy becomes real exposure is by passing
    every gate here.
    """

    def evaluate_buy(self, ctx: BuyFireContext) -> BuyFireResult: ...


# --------------------------------------------------------------------------- #
# Resting order specs.  Frozen, validated, de-risk-direction-typed.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class RestingSellOrder:
    """A standing SELL / REDUCE that fires on a price trigger — ALWAYS de-risk.

    Holds a DeRiskIntentFactory: it can ONLY emit ExitIntent (full flatten) or
    ReduceIntent (scale-out).  There is no buy/entry path on this type — a resting
    SELL is structurally incapable of adding exposure.  It needs no entry gate
    because it only sheds risk; it fires straight through.

    `fraction_bps` of remaining: SELL is a full flatten (10000) or any partial;
    REDUCE is a strict partial (1..9999).  `trigger_price` is the FIXED level.
    """

    order_id: str
    mint: str
    side: RestingSide  # SELL or REDUCE only (validated)
    trigger_price: Decimal
    trigger_direction: TriggerDirection
    fraction_bps: int = _FULL_EXIT_BPS
    exit_mode: str = "secure"  # forced/defensive exits route secure (sandwich-safe)
    reason: str = "resting_sell"
    _factory: DeRiskIntentFactory = field(default_factory=DeRiskIntentFactory, repr=False)

    def __post_init__(self) -> None:
        if self.side not in (RestingSide.SELL, RestingSide.REDUCE):
            raise ValueError(
                f"RestingSellOrder.side must be SELL or REDUCE (de-risk), got {self.side}."
            )
        object.__setattr__(
            self, "trigger_price", _to_decimal_price(self.trigger_price, "trigger_price")
        )
        if self.trigger_price <= 0:
            raise ValueError("RestingSellOrder.trigger_price must be > 0.")
        _reject_float_int(self.fraction_bps, "RestingSellOrder.fraction_bps")
        if self.side is RestingSide.SELL:
            if not (1 <= self.fraction_bps <= _FULL_EXIT_BPS):
                raise ValueError("SELL fraction_bps must be in [1, 10000].")
        else:  # REDUCE — strict partial
            if not (1 <= self.fraction_bps <= _FULL_EXIT_BPS - 1):
                raise ValueError(
                    "REDUCE fraction_bps must be in [1, 9999] (a partial; a full exit is a SELL)."
                )
        if self.exit_mode not in ("secure", "fast"):
            raise ValueError("RestingSellOrder.exit_mode must be 'secure' or 'fast'.")

    def is_triggered(self, mark_price: Decimal | int | str) -> bool:
        """Pure point-in-time trigger test."""
        mark = _to_decimal_price(mark_price, "mark_price")
        return _trigger_met(self.trigger_direction, mark, self.trigger_price)

    def fire(self, *, block_time_ms: int) -> ExitIntent | ReduceIntent:
        """Emit the de-risk Intent for this resting SELL/REDUCE.  Always allowed.

        Deterministic client_intent_id (mint + event-time) for idempotent replay.
        NEVER constructs an EntryIntent — the factory has no such method.
        """
        _reject_float_int(block_time_ms, "block_time_ms")
        cid = f"resting:{self.side.value.lower()}:{self.mint}:{self.order_id}:{block_time_ms}"
        if self.side is RestingSide.SELL:
            return self._factory.exit(
                client_intent_id=cid,
                mint=self.mint,
                fraction_bps=self.fraction_bps,
                exit_mode=self.exit_mode,  # type: ignore[arg-type]
                reason=f"resting_sell_triggered: {self.reason}",
            )
        return self._factory.reduce(
            client_intent_id=cid,
            mint=self.mint,
            fraction_bps=self.fraction_bps,
            reason=f"resting_reduce_triggered: {self.reason}",
        )


@dataclass(frozen=True)
class RestingBuyOrder:
    """A standing BUY (limit dip / breakout / DCA tranche) — a DEFERRED sized ENTRY.

    A resting BUY is NOT exempt from the entry stack; it is the entry stack run
    LATER.  It holds NO factory and NO EntryIntent constructor.  Firing it produces
    a `BuyFireContext` that the injected `EntryGateChain` must turn into a gated
    EntryIntent — and only if the safety gate, breaker, sizing caps, AND cost gate
    all pass at fire time.  Anything less fires nothing (refuse-by-default).

    `trigger_price` is the FIXED limit level; `trigger_direction` AT_OR_BELOW for a
    dip-buy, AT_OR_ABOVE for a breakout-buy.  There is no size field here — the
    size is DECIDED by fractional-Kelly at fire time against the live caps, never
    pre-committed (a pre-committed size would bypass the aggregate-exposure cap).
    """

    order_id: str
    mint: str
    trigger_price: Decimal
    trigger_direction: TriggerDirection
    reason: str = "resting_buy"
    side: RestingSide = RestingSide.BUY
    # Event-time step at/after which a DCA tranche may fire (None = no schedule;
    # a plain limit BUY fires purely on its price trigger).  Point-in-time: this is
    # an event-time step counter, never a wall-clock time.
    scheduled_step: int | None = None

    def __post_init__(self) -> None:
        if self.side is not RestingSide.BUY:
            raise ValueError("RestingBuyOrder.side must be BUY.")
        object.__setattr__(
            self, "trigger_price", _to_decimal_price(self.trigger_price, "trigger_price")
        )
        if self.trigger_price <= 0:
            raise ValueError("RestingBuyOrder.trigger_price must be > 0.")
        if self.scheduled_step is not None:
            _reject_float_int(self.scheduled_step, "RestingBuyOrder.scheduled_step")
            if self.scheduled_step < 0:
                raise ValueError("RestingBuyOrder.scheduled_step must be >= 0.")

    def is_triggered(self, mark_price: Decimal | int | str) -> bool:
        """Pure point-in-time trigger test."""
        mark = _to_decimal_price(mark_price, "mark_price")
        return _trigger_met(self.trigger_direction, mark, self.trigger_price)

    def fire(self, ctx: BuyFireContext, chain: EntryGateChain | None) -> BuyFireResult:
        """Attempt to fire the BUY THROUGH the full entry gate chain.

        REFUSE-BY-DEFAULT: with no `chain` wired, the buy cannot be proven safe and
        is REFUSED — a resting BUY must NEVER fire un-gated.  Otherwise we delegate
        to the chain, which runs safety + breaker + sizing caps + cost and returns a
        gated EntryIntent or a rejection.  This type contributes NOTHING beyond
        forwarding to the gate stack — it has no power to construct an EntryIntent
        on its own.
        """
        if ctx.mint != self.mint:
            raise ValueError(
                f"BuyFireContext.mint ({ctx.mint!r}) must match order mint ({self.mint!r})."
            )
        if chain is None:
            return BuyFireResult(
                mint=self.mint,
                fired=False,
                intent=None,
                rejection=BuyFireRejection.NO_GATE_CHAIN,
                size_lamports=0,
                detail="refuse_by_default: no entry gate chain wired; a resting BUY may "
                "NEVER fire un-gated (safety+breaker+sizing+cost must all pass).",
            )
        return chain.evaluate_buy(ctx)


# The CLOSED union of resting-order types.  A SELL can only de-risk; a BUY is
# always gated.  There is no third "ungated increase" member — adding one would
# require an ADR + a new type here, not a code-path tweak.
RestingOrder = RestingSellOrder | RestingBuyOrder


# --------------------------------------------------------------------------- #
# The GATED entry-fire chain — wires the REAL T-323 + breaker + T-324 gates.
# This is where the "resting BUY cannot bypass" rule is realized concretely.
# --------------------------------------------------------------------------- #


@runtime_checkable
class _DenylistLike(Protocol):
    """Structural view of the E2 DENYLIST pre-filter (``DenyAllowWatcher.check_keys``).

    De-risk-only by construction: ``check_keys`` returns a truthy hit object when the
    creator OR mint is denylisted (=> REJECT) and ``None`` otherwise (no opinion).  It
    has no method that sizes, triggers, widens a stop, or lifts a cap — a denylist can
    only VETO.  The entry chain calls this O(1) membership test as STEP 0, before the
    safety gate; curation/hot-reload of the underlying file is a SLOW-loop job and is
    NOT on this fast path.  Membership is a property of the key string only — the same
    test runs live and in backtest (no compute-time leak).
    """

    def check_keys(
        self,
        *,
        creator: str | None,
        mint: str | None,
        event_slot: int = ...,
        event_block_time_ms: int = ...,
    ) -> object | None: ...


@runtime_checkable
class _SafetyGateLike(Protocol):
    """Structural view of T-323 PreTradeSafetyGate (evaluate_fast + enabled-for-entry)."""

    def evaluate_fast(self, inputs: object) -> object: ...
    def require_enabled_for_entry(self, decision: object) -> bool: ...


@runtime_checkable
class _BreakerLike(Protocol):
    """Structural view of the circuit breaker's entry gate (T-320)."""

    def entries_allowed(self) -> bool: ...


@runtime_checkable
class _SizerLike(Protocol):
    """Structural view of T-324 FractionalKellySizer.size(...)."""

    def size(
        self,
        *,
        p_calibrated: float,
        uncertainty: float,
        current_aggregate_lamports: int,
        **kwargs: object,
    ) -> object: ...


@runtime_checkable
class _CostGateLike(Protocol):
    """Structural view of T-324 CostAwareEntryGate.allows_entry(cost_stack)."""

    def allows_entry(self, cost_stack: object) -> bool: ...


@runtime_checkable
class GatedEntryBuilder(Protocol):
    """Builds the FINAL EntryIntent from a sized, fully-gated buy.

    Owned by the SNIPE / execution lane (it is the ONLY place an EntryIntent is
    constructed — ADR-0006).  This module INVOKES it ONLY after every gate passed
    and with the Kelly-decided size.  It may itself refuse (e.g. slot guard, signer
    re-validation) by raising or returning None — that is the BUILD_REFUSED branch.
    """

    def build_entry(
        self,
        *,
        mint: str,
        size_lamports: int,
        block_time_ms: int,
        cost_stack: object,
        entry_build_params: object,
    ) -> EntryIntent | None: ...


class GatedEntryFireChain:
    """The concrete `EntryGateChain` — a resting BUY's fire path, gate by gate.

    Runs, IN ORDER, refuse-by-default at every step:
        0. DENYLIST pre-filter (E2): a denylisted CREATOR wallet or token MINT is an
           instant, pre-gate REJECT — it never even reaches the safety gate.  This is
           the SAME single-function veto the just-in-time SNIPE path runs first
           (``blacklist.evaluate_with_denylist``); wiring it HERE closes the gap where
           a parked BUY could fire on a denylisted token the live path would refuse.
        1. SAFETY gate (T-323): evaluate_fast + require_enabled_for_entry.  A
           disabled gate or any red flag => REJECT.
        2. CIRCUIT BREAKER (T-320): entries_allowed() — a TRIPPED breaker blocks
           ALL new entries, parked or live.
        3. SIZING (T-324): fractional-Kelly with per-trade + aggregate caps.  A
           zero size (no edge / caps exhausted) => no buy.
        4. COST gate (T-324): edge must STRICTLY exceed round-trip cost.
        5. BUILD: hand the gated size to the SNIPE lane's EntryIntent builder.

    A resting BUY that clears ALL SIX becomes a real, fully-gated EntryIntent —
    identical to what a just-in-time snipe would produce.  A failure at ANY step is
    a non-fired result.  There is no path here that emits an EntryIntent without
    every step passing.

    The ``denylist`` seam is DE-RISK-ONLY: it can only veto (REJECT) or stay silent;
    it never sizes, triggers, or widens anything.  It is OPTIONAL only so existing
    construction sites stay valid, but the LIVE/SNIPE wiring MUST inject it — a chain
    with no denylist simply skips step 0 (no extra exclusion); it never widens risk.
    """

    def __init__(
        self,
        *,
        safety_gate: _SafetyGateLike,
        breaker: _BreakerLike,
        sizer: _SizerLike,
        cost_gate: _CostGateLike,
        entry_builder: GatedEntryBuilder,
        denylist: _DenylistLike | None = None,
    ) -> None:
        self._safety = safety_gate
        self._breaker = breaker
        self._sizer = sizer
        self._cost = cost_gate
        self._builder = entry_builder
        self._denylist = denylist

    def evaluate_buy(self, ctx: BuyFireContext) -> BuyFireResult:
        # 0. DENYLIST pre-filter (E2) — runs BEFORE the safety gate so a denylisted
        #    creator/mint short-circuits to REJECT and never reaches a single gate.
        #    De-risk-only: ``check_keys`` can only return a hit (=> veto) or None (no
        #    opinion).  Point-in-time: membership is a pure property of the key string
        #    (event-time is passed only to stamp the reject, never to decide it).
        if self._denylist is not None:
            hit = self._denylist.check_keys(
                creator=ctx.creator,
                mint=ctx.mint,
                event_slot=getattr(ctx.safety_inputs, "event_slot", 0) or 0,
                event_block_time_ms=ctx.block_time_ms,
            )
            if hit is not None:
                flag = getattr(getattr(hit, "flag", None), "value", "denylisted")
                return _reject(
                    ctx.mint,
                    BuyFireRejection.DENYLISTED,
                    f"denylist pre-filter rejected: {flag} "
                    f"(creator/mint on the scam denylist) — refused before the safety gate",
                )

        # 0b. REFUSE-BY-DEFAULT on a missing safety input.  A BUY can only be proven
        #    safe against FRESH decoded facts; absent them there is nothing to gate,
        #    so we refuse (we never invent a passing safety decision).  This also
        #    fail-closes if a malformed ctx reaches a gate that would otherwise raise.
        if ctx.safety_inputs is None:
            return _reject(
                ctx.mint,
                BuyFireRejection.SAFETY_GATE,
                "refuse_by_default: no fresh safety inputs at fire time — a resting BUY "
                "cannot be proven safe and is refused (never fires un-gated).",
            )

        # 1. SAFETY gate — refuse-by-default; a disabled gate can NEVER green-light.
        decision = self._safety.evaluate_fast(ctx.safety_inputs)
        if not self._safety.require_enabled_for_entry(decision):
            return _reject(ctx.mint, BuyFireRejection.SAFETY_GATE, "pre-trade safety gate rejected")

        # 2. CIRCUIT BREAKER — a TRIPPED breaker blocks every new entry.
        if not self._breaker.entries_allowed():
            return _reject(
                ctx.mint,
                BuyFireRejection.BREAKER_TRIPPED,
                "circuit breaker TRIPPED; entries halted",
            )

        # 3. SIZING — fractional-Kelly, hard-capped per-trade + aggregate.  This is
        #    the ONLY place the buy's size is decided; it is NEVER pre-committed.
        sizing = self._sizer.size(
            p_calibrated=ctx.p_calibrated,
            uncertainty=ctx.uncertainty,
            current_aggregate_lamports=ctx.current_aggregate_lamports,
        )
        size_lamports = int(sizing.size_lamports)  # type: ignore[attr-defined]
        if size_lamports <= 0:
            return _reject(
                ctx.mint,
                BuyFireRejection.ZERO_SIZE,
                f"sizing returned {size_lamports} lamports "
                f"(no edge / aggregate cap exhausted) — no buy",
            )

        # 4. COST gate — edge must STRICTLY exceed round-trip cost (incl. tip/fees/
        #    slippage/adverse selection).  The dominant failure mode dies here.
        if not self._cost.allows_entry(ctx.cost_stack):
            return _reject(
                ctx.mint,
                BuyFireRejection.COST_GATE,
                "cost gate rejected: expected edge <= total round-trip cost",
            )

        # 5. BUILD the gated EntryIntent via the SNIPE lane's builder (the ONLY
        #    EntryIntent constructor; ADR-0006).  It may itself refuse.
        try:
            intent = self._builder.build_entry(
                mint=ctx.mint,
                size_lamports=size_lamports,
                block_time_ms=ctx.block_time_ms,
                cost_stack=ctx.cost_stack,
                entry_build_params=ctx.entry_build_params,
            )
        except Exception as exc:  # noqa: BLE001 — a builder refusal is a non-fire, not a crash
            return _reject(
                ctx.mint,
                BuyFireRejection.BUILD_REFUSED,
                f"gated EntryIntent builder refused: {exc}",
            )
        if intent is None:
            return _reject(
                ctx.mint,
                BuyFireRejection.BUILD_REFUSED,
                "gated EntryIntent builder returned None (slot guard / signer re-validation)",
            )

        return BuyFireResult(
            mint=ctx.mint,
            fired=True,
            intent=intent,
            rejection=None,
            size_lamports=size_lamports,
            detail="resting BUY fired through the FULL entry stack "
            "(safety + breaker + sizing caps + cost gate)",
        )


def _reject(mint: str, why: BuyFireRejection, detail: str) -> BuyFireResult:
    return BuyFireResult(
        mint=mint,
        fired=False,
        intent=None,
        rejection=why,
        size_lamports=0,
        detail=detail,
    )


# --------------------------------------------------------------------------- #
# DCA plan — a schedule of resting BUY tranches.  Each tranche is INDIVIDUALLY
# gated at its own fire time (no tranche bypasses; the aggregate cap is enforced
# tranche-by-tranche because each re-reads current_aggregate_lamports).
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class DcaPlan:
    """A dollar-cost-average schedule: N resting BUY tranches at event-time steps.

    Each tranche is a SEPARATE deferred entry, fired on its scheduled event-time
    step, and EACH is gated independently at fire time.  There is no pre-committed
    total size: every tranche is sized by fractional-Kelly against the LIVE
    aggregate exposure when it fires, so the aggregate-exposure cap bounds the whole
    plan automatically — a later tranche is shrunk (or refused) once earlier
    tranches have consumed the headroom.  Crucially, this means a DCA plan can NEVER
    grow total exposure past the cap, no matter how many tranches it schedules.

    `step_interval` is in EVENT-TIME steps (not wall-clock), so a replay fires the
    same tranches on the same steps — point-in-time correct.  Triggers are optional:
    if `trigger_price` is None a tranche fires purely on its schedule; otherwise it
    must ALSO meet the price trigger (schedule AND price).
    """

    plan_id: str
    mint: str
    tranches: int
    step_interval: int  # event-time steps between tranches
    first_step: int = 0
    trigger_price: Decimal | None = None
    trigger_direction: TriggerDirection = TriggerDirection.AT_OR_BELOW
    reason: str = "dca_plan"

    def __post_init__(self) -> None:
        _reject_float_int(self.tranches, "DcaPlan.tranches")
        _reject_float_int(self.step_interval, "DcaPlan.step_interval")
        _reject_float_int(self.first_step, "DcaPlan.first_step")
        if self.tranches <= 0:
            raise ValueError("DcaPlan.tranches must be > 0.")
        if self.step_interval <= 0:
            raise ValueError("DcaPlan.step_interval must be > 0 (event-time steps).")
        if self.first_step < 0:
            raise ValueError("DcaPlan.first_step must be >= 0.")
        if self.trigger_price is not None:
            object.__setattr__(
                self, "trigger_price", _to_decimal_price(self.trigger_price, "trigger_price")
            )
            if self.trigger_price <= 0:
                raise ValueError("DcaPlan.trigger_price must be > 0 when set.")

    def tranche_steps(self) -> tuple[int, ...]:
        """The event-time steps at which each tranche is scheduled to fire."""
        return tuple(self.first_step + i * self.step_interval for i in range(self.tranches))

    def buy_order_for_tranche(self, idx: int) -> RestingBuyOrder:
        """The deferred (gated-at-fire) resting BUY for tranche `idx`.

        A schedule-only tranche (no price trigger) is modeled as a BUY whose price
        condition is trivially met by passing a mark at/within the trigger; we make
        the trigger explicit so the same `fire()` gate path is used — there is no
        separate, less-gated DCA fire path.
        """
        if not (0 <= idx < self.tranches):
            raise IndexError(f"tranche idx {idx} out of range [0, {self.tranches}).")
        # A schedule-only tranche uses a permissive price trigger (AT_OR_ABOVE 0+),
        # so the price test always passes and the SCHEDULE governs; the gate chain
        # still runs in full at fire time.
        trig = self.trigger_price if self.trigger_price is not None else Decimal(1)
        direction = (
            self.trigger_direction
            if self.trigger_price is not None
            else TriggerDirection.AT_OR_ABOVE
        )
        return RestingBuyOrder(
            order_id=f"{self.plan_id}:tranche:{idx}",
            mint=self.mint,
            trigger_price=trig,
            trigger_direction=direction,
            reason=f"{self.reason}:tranche={idx}/{self.tranches}",
            scheduled_step=self.first_step + idx * self.step_interval,
        )


# --------------------------------------------------------------------------- #
# Durable store seam — orders SURVIVE a restart (fire when operator OFFLINE).
# --------------------------------------------------------------------------- #


@runtime_checkable
class RestingOrderStore(Protocol):
    """Durable store for standing resting orders (Redis/Parquet in prod).

    The store is what makes resting orders fire while the operator is OFFLINE: the
    orders persist across a process restart, are re-read on boot, and remain armed.
    A SELL/EXIT additionally rests on the venue-native off-box seam so it can fire
    with the WHOLE bot dead; the store is the in-bot durable record both for SELLs
    and for BUYs (a BUY cannot live off-box because it must be re-gated at fire).
    """

    def upsert(self, order: RestingOrder) -> None: ...
    def remove(self, order_id: str) -> None: ...
    def load_all(self) -> tuple[RestingOrder, ...]: ...


class InMemoryRestingOrderStore:
    """Reference in-memory durable store for tests / sim (no network).

    Reconstructing a fresh RestingOrderBook against a store that already holds
    orders reproduces "process restart" — the standing orders come back armed,
    proving they fire after the bot was offline.
    """

    def __init__(self) -> None:
        self._orders: dict[str, RestingOrder] = {}
        self._lock = threading.RLock()

    def upsert(self, order: RestingOrder) -> None:
        with self._lock:
            self._orders[order.order_id] = order

    def remove(self, order_id: str) -> None:
        with self._lock:
            self._orders.pop(order_id, None)

    def load_all(self) -> tuple[RestingOrder, ...]:
        with self._lock:
            return tuple(self._orders.values())


# --------------------------------------------------------------------------- #
# Venue-native off-box SELL seam — a resting SELL fires with the bot fully dead.
# Reuses the survivable-stop keeper contract; a take-profit/stop-limit SELL can
# rest off-box exactly like a hard stop.  (A BUY can NEVER rest off-box — it must
# be re-gated at fire time, which an off-box keeper cannot do.)
# --------------------------------------------------------------------------- #


@runtime_checkable
class OffBoxSellKeeper(Protocol):
    """The off-box keeper seam for resting SELLs (owned by solana-execution-engineer).

    Mirrors `venue_native_stop.RestingStopVenue`: place a SELL trigger off-box,
    cancel it, and (off-box) fire it on a chain price breach WITHOUT our loop.  A
    resting take-profit/stop-limit SELL placed here survives a fully-dead bot.
    """

    def place_resting_sell(
        self, mint: str, order_id: str, trigger_price: Decimal, fraction_bps: int
    ) -> str: ...

    def cancel_resting_sell(self, mint: str, venue_order_id: str) -> None: ...


# --------------------------------------------------------------------------- #
# The order book — the deterministic, point-in-time evaluator the loop / keeper
# runs each tick.  De-risk SELLs fire straight through; BUYs go through the chain.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class TickFire:
    """One order that fired on a tick, plus its emitted artifact.

    For a SELL/REDUCE, `sell_intent` is the de-risk Intent (always present when
    fired).  For a BUY, `buy_result` is the gate-chain outcome — which may be a
    fired EntryIntent OR a refuse-by-default rejection (a BUY 'fires' its
    EVALUATION on trigger; whether it BUYS depends on the gates).
    """

    order_id: str
    side: RestingSide
    sell_intent: ExitIntent | ReduceIntent | None = None
    buy_result: BuyFireResult | None = None


class RestingOrderBook:
    """Standing resting LIMIT + DCA orders, evaluated point-in-time each tick.

    Loads its standing orders from a DURABLE store on construction (restart-safe:
    orders parked before the bot went offline come back armed).  Each tick:

      * a triggered SELL/REDUCE fires its de-risk Intent immediately (always
        allowed) and is removed from the book + store;
      * a triggered BUY is RE-EVALUATED through the full entry gate chain via a
        caller-supplied `BuyFireContext` (point-in-time inputs).  A BUY that passes
        every gate yields a gated EntryIntent; otherwise it is refused.  A refused
        BUY STAYS armed (it may pass on a later tick when conditions change) unless
        the caller cancels it.

    The evaluator is PURE w.r.t. its inputs (no wall-clock, no lookahead).  It
    DECIDES and emits Intents; the ExecutionVenue seam (DRY_RUN-gated) lands them.
    """

    def __init__(
        self,
        store: RestingOrderStore,
        *,
        gate_chain: EntryGateChain | None = None,
        off_box_keeper: OffBoxSellKeeper | None = None,
    ) -> None:
        self._store = store
        self._chain = gate_chain
        self._keeper = off_box_keeper
        self._lock = threading.RLock()
        # Re-read durable orders on boot — THIS is the offline-survival path.
        self._orders: dict[str, RestingOrder] = {o.order_id: o for o in store.load_all()}
        self._keeper_handles: dict[str, str] = {}

    # -- registration -------------------------------------------------------

    def place(self, order: RestingOrder) -> RestingOrder:
        """Arm a standing resting order; persist it durably (survives restart).

        A SELL/REDUCE is ALSO placed on the off-box keeper (if wired) so it fires
        with the whole bot dead.  A BUY is durable but NOT off-box: it must be
        re-gated at fire time, which only the in-bot chain can do.
        """
        with self._lock:
            self._orders[order.order_id] = order
            self._store.upsert(order)
            if isinstance(order, RestingSellOrder) and self._keeper is not None:
                handle = self._keeper.place_resting_sell(
                    mint=order.mint,
                    order_id=order.order_id,
                    trigger_price=order.trigger_price,
                    fraction_bps=order.fraction_bps,
                )
                self._keeper_handles[order.order_id] = handle
            return order

    def place_dca(self, plan: DcaPlan) -> tuple[RestingBuyOrder, ...]:
        """Arm every tranche of a DCA plan as an individually-gated resting BUY."""
        orders = tuple(plan.buy_order_for_tranche(i) for i in range(plan.tranches))
        for o in orders:
            self.place(o)
        return orders

    def cancel(self, order_id: str) -> None:
        """Cancel + de-persist a standing order (idempotent).  De-risk: removing an
        order can only REDUCE pending activity, never add it."""
        with self._lock:
            order = self._orders.pop(order_id, None)
            self._store.remove(order_id)
            if (
                order is not None
                and isinstance(order, RestingSellOrder)
                and self._keeper is not None
            ):
                handle = self._keeper_handles.pop(order_id, None)
                if handle is not None:
                    self._keeper.cancel_resting_sell(order.mint, handle)

    def open_order_ids(self) -> frozenset[str]:
        with self._lock:
            return frozenset(self._orders.keys())

    def order(self, order_id: str) -> RestingOrder | None:
        with self._lock:
            return self._orders.get(order_id)

    # -- the point-in-time evaluator ---------------------------------------

    def evaluate_tick(
        self,
        *,
        mint: str,
        mark_price: Decimal | int | str,
        block_time_ms: int,
        event_step: int | None = None,
        buy_context: BuyFireContext | None = None,
    ) -> tuple[TickFire, ...]:
        """Evaluate every standing order for `mint` at one point-in-time tick.

        Args:
            mint: the token whose orders to evaluate.
            mark_price: the point-in-time mark (Decimal SOL-lamports/token base unit).
            block_time_ms: event-time of the tick — AUTHORITATIVE clock (stamps ids).
            event_step: event-time step counter (for DCA schedule triggers); None
                disables schedule-gating (price-only).
            buy_context: the fresh point-in-time inputs a triggered BUY needs to be
                RE-GATED.  REFUSE-BY-DEFAULT: a triggered BUY with no context (or no
                gate chain) does NOT fire a buy — it cannot be proven safe.

        Returns the tuple of TickFire records (one per order that triggered).
        """
        _reject_float_int(block_time_ms, "block_time_ms")
        if block_time_ms < 0:
            raise ValueError("block_time_ms must be non-negative (event-time).")
        if event_step is not None:
            _reject_float_int(event_step, "event_step")
        mark = _to_decimal_price(mark_price, "mark_price")

        with self._lock:
            candidates = [o for o in self._orders.values() if o.mint == mint]
            # Determinism: evaluate BUY tranches in a stable order so the
            # within-tick aggregate accounting (below) is reproducible in replay.
            # SELLs are order-insensitive (each de-risks independently); BUYs are
            # NOT — a later BUY must see the headroom the earlier ones consumed, so
            # we sort the whole candidate set by order_id for a deterministic fire
            # sequence (point-in-time: pure function of the order specs).
            candidates.sort(key=lambda o: o.order_id)

        fires: list[TickFire] = []
        # WITHIN-TICK AGGREGATE ACCOUNTING (B1 fix).  Multiple resting BUYs of the
        # same mint can become eligible in a SINGLE tick (a DCA catch-up at a late
        # event_step makes every earlier-due tranche eligible at once).  Each is
        # sized against the aggregate-exposure cap via the supplied
        # current_aggregate_lamports — but the supplied figure is the aggregate as
        # of the START of the tick and does NOT yet reflect sizes we fire HERE.  If
        # we passed the same starting aggregate to every BUY, each would be sized to
        # the FULL remaining headroom and the SUM could blow through the hard cap
        # (proven: 30 catch-up tranches committing 0.75 SOL against a 0.5 SOL cap).
        # We therefore thread a running total of lamports COMMITTED so far this tick
        # and add it to each subsequent BUY's current_aggregate_lamports, so the
        # sizer's aggregate clamp sees the TRUE remaining headroom.  Once the
        # headroom is exhausted the sizer returns 0 and the BUY is refused — the
        # firm-wide aggregate ceiling holds even for a same-tick catch-up.
        committed_this_tick = 0
        for order in candidates:
            if isinstance(order, RestingSellOrder):
                if order.is_triggered(mark):
                    intent = order.fire(block_time_ms=block_time_ms)
                    fires.append(
                        TickFire(order_id=order.order_id, side=order.side, sell_intent=intent)
                    )
                    # A SELL fires once then is removed (idempotent on the book side).
                    self.cancel(order.order_id)
            else:  # RestingBuyOrder — deferred, gated entry
                if not self._buy_step_ready(order, event_step):
                    continue
                if not order.is_triggered(mark):
                    continue
                ctx = self._buy_ctx_for(order, buy_context, block_time_ms, committed_this_tick)
                result = order.fire(ctx, self._chain)
                fires.append(TickFire(order_id=order.order_id, side=order.side, buy_result=result))
                # A BUY is removed ONLY if it actually fired a gated entry; a
                # refused BUY stays armed for a later tick (de-risk: never auto-buys).
                if result.fired:
                    # Reserve the lamports it committed so the NEXT BUY this tick is
                    # sized against the shrunken remaining aggregate headroom.
                    committed_this_tick += result.size_lamports
                    self.cancel(order.order_id)
        return tuple(fires)

    # -- helpers ------------------------------------------------------------

    @staticmethod
    def _buy_step_ready(order: RestingBuyOrder, event_step: int | None) -> bool:
        """DCA schedule gate (point-in-time, event-time steps).

        A DCA tranche encodes its scheduled event-time step in `scheduled_step`; a
        plain limit BUY leaves it None and is always step-ready (price governs).
        When the caller supplies an `event_step`, a scheduled tranche is ready ONLY
        once that step has been reached — never before.  Firing before the schedule
        would be an early, un-planned entry; we refuse it (the order stays armed).
        A None `event_step` disables schedule-gating (price-only evaluation)."""
        scheduled = order.scheduled_step
        if scheduled is None or event_step is None:
            return True
        return event_step >= scheduled

    def _buy_ctx_for(
        self,
        order: RestingBuyOrder,
        supplied: BuyFireContext | None,
        block_time_ms: int,
        committed_this_tick: int,
    ) -> BuyFireContext:
        """The point-in-time gate inputs for a triggered BUY.

        REFUSE-BY-DEFAULT: if the caller supplied no context, we synthesize a
        context that the gate chain will REJECT (a None safety input / zero-edge
        cost stack would be refused by the real gates).  We never invent passing
        inputs — a BUY with no fresh inputs cannot fire.

        WITHIN-TICK AGGREGATE (B1 fix): `committed_this_tick` is the sum of sizes
        ALREADY fired this tick.  We add it to the supplied current_aggregate_lamports
        so the sizer's aggregate-exposure clamp sees the TRUE remaining headroom for
        THIS buy — never the start-of-tick figure that ignores same-tick fires.
        This is what stops a same-tick DCA catch-up from breaching the hard cap.
        """
        _reject_float_int(committed_this_tick, "committed_this_tick")
        if committed_this_tick < 0:
            raise ValueError("committed_this_tick must be >= 0 (lamports reserved this tick).")
        if supplied is not None:
            if supplied.mint != order.mint:
                raise ValueError("supplied BuyFireContext.mint must match the BUY order's mint.")
            # Re-stamp the event-time to the tick we are evaluating (point-in-time)
            # AND advance the aggregate by what earlier same-tick BUYs committed, so
            # this buy is sized against the real remaining headroom (never double-
            # spending the cap).
            return replace(
                supplied,
                block_time_ms=block_time_ms,
                current_aggregate_lamports=supplied.current_aggregate_lamports
                + committed_this_tick,
            )
        # No fresh inputs => a context that cannot pass: None safety inputs make the
        # safety gate refuse-by-default, and there is no chain to invent a pass.
        return BuyFireContext(
            mint=order.mint,
            block_time_ms=block_time_ms,
            safety_inputs=None,
            p_calibrated=0.0,
            uncertainty=1.0,
            current_aggregate_lamports=committed_this_tick,
            cost_stack=None,
        )
