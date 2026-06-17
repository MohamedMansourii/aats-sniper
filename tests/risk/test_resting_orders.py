"""T-326 — Durable resting LIMIT + DCA orders: fire OFFLINE; de-risk-direction.

VERIFY (from the task):
  * resting orders FIRE even when the operator/bot loop is OFFLINE (durable store);
  * a resting SELL/EXIT is ALWAYS allowed (de-risk);
  * a resting BUY still passes the FULL safety (T-323) + cost + sizing (T-324)
    gates AT FIRE TIME — never a bypass;
  * NO resting order can bypass the safety gate;
  * money is integer/Decimal, never float; point-in-time (no wall-clock).

These tests use the REAL T-323 PreTradeSafetyGate, the REAL T-324 sizer + cost
gate, and the REAL T-320 circuit breaker, wired through GatedEntryFireChain, so a
"resting BUY passes" is a buy that cleared the actual production gates.
"""

from __future__ import annotations

import decimal
import itertools
from decimal import Decimal

import pytest

from aats.contracts.events import EventTime
from aats.contracts.intents import CostStack, EntryIntent, ExitIntent, ReduceIntent
from aats.contracts.risk import RiskConfig
from aats.risk.blacklist import DenyAllowWatcher
from aats.risk.circuit_breaker import (
    CircuitBreaker,
    InMemoryBreakerStore,
    LLMDeRiskSignal,
    PnLEvent,
)
from aats.risk.cost_model import CostAwareEntryGate
from aats.risk.pretrade_gate import PreTradeSafetyGate, RedFlag, SafetyInputs
from aats.risk.resting_orders import (
    BuyFireContext,
    BuyFireRejection,
    DcaPlan,
    GatedEntryFireChain,
    InMemoryRestingOrderStore,
    RestingBuyOrder,
    RestingOrderBook,
    RestingSellOrder,
    RestingSide,
    TriggerDirection,
)
from aats.risk.sizing import FractionalKellySizer

MINT = "So1111111111111111111111111111111111111111"


# --------------------------------------------------------------------------- #
# TEST ISOLATION (T-326 re-spin) — DIAGNOSED ROOT CAUSE + DEFENCE IN DEPTH.
#
# The within-tick aggregate-cap regression tests below exercise the REAL
# production sizing path (sizing.py).  The PRIOR review correctly rejected the
# earlier story (a "leaked decimal context from a sibling") on two counts that we
# CONFIRMED:
#   (a) the hard aggregate clamp is pure INTEGER arithmetic, immune to the decimal
#       context — no context can drive the captured 750M cap breach through it; and
#   (b) NO test or production module in this codebase mutates the global decimal
#       context, so there was nothing to "isolate from" — a sibling-leak fixture
#       guards a leak that does not exist.
#
# The TRUE fragility we found and FIXED is in production: the *fractional* Kelly
# steps that FEED the integer clamps (`_clamp_unit`, `full_kelly_fraction`, the
# 1/4-cap product, the `Decimal(per_trade_cap) * f_applied` floor) ran under the
# ambient process-global decimal context.  Under a hostile low-precision /
# rounding-UP context the applied Kelly fraction can round PAST the 1/4 cap and
# trip sizing.py's own invariant assert (a hard ERROR), and in general the lamport
# size is not reproducible if anything ever leaves a mutated context installed —
# which is NOT point-in-time correct (live != backtest != test).  The fix pins a
# FIXED local context (prec=28, ROUND_HALF_EVEN) around the size math in sizing.py,
# so the size is now byte-identical under ANY global context (proven exhaustively
# by `test_sizing_hermetic_under_hostile_decimal_context` below).
#
# This autouse fixture remains as DEFENCE IN DEPTH: it pins a known-good context
# for every test in this module so a hostile context installed by a future sibling
# cannot make THESE tests' setup non-deterministic either.  It is pure isolation —
# it changes NO assertion and weakens NO guard — and it is no longer the load-
# bearing fix (the production pin is).  The hostile-context test below DELIBERATELY
# opts out of it to prove the production path stands on its own.
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _hermetic_decimal_context(request):
    # Tests that explicitly probe a hostile global context opt out via this marker
    # so they can install their own context and prove production immunity directly.
    if "no_hermetic_decimal" in request.keywords:
        yield
        return
    saved = decimal.getcontext()
    # A fresh copy of the canonical default — never the (possibly polluted) live one.
    decimal.setcontext(decimal.DefaultContext.copy())
    try:
        yield
    finally:
        decimal.setcontext(saved)


# --------------------------------------------------------------------------- #
# Test doubles for the SNIPE-lane seams (we INVOKE; we do not author).
# --------------------------------------------------------------------------- #


class _NoopFlatten:
    def emergency_flatten_all(self, reason: str) -> None:  # pragma: no cover - trivial
        pass


class _BuilderSpy:
    """A stand-in for the SNIPE lane's gated EntryIntent builder.

    It builds a real EntryIntent (with a populated CostStack and a valid slot
    guard), so the ONLY way it produces one is when the chain hands it a gated
    size.  It records every call for assertion.
    """

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def build_entry(
        self,
        *,
        mint: str,
        size_lamports: int,
        block_time_ms: int,
        cost_stack: object,
        entry_build_params: object,
    ) -> EntryIntent:
        self.calls.append({"mint": mint, "size_lamports": size_lamports})
        event_time = EventTime(slot=100, block_time_ms=block_time_ms or 1, wall_clock_ms=1)
        assert isinstance(cost_stack, CostStack)
        return EntryIntent(
            client_intent_id=f"entry:{mint}:{block_time_ms}",
            mint=mint,
            event_time=event_time,
            sol_in_lamports=size_lamports,
            slippage_bps=300,
            tip_lamports=10_000,
            cu_price_microlamports=1_000,
            target_slot=event_time.slot + 5,
            cost_stack=cost_stack,
            venue="pumpswap",
            wallet_id="w0",
        )


def _good_cost_stack(edge: int = 1_000) -> CostStack:
    """A cost stack whose edge STRICTLY exceeds round-trip cost (passes the gate)."""
    parts = 50 + 30 + 40 + 30 + 40 + 150  # = 340
    return CostStack(
        expected_edge_bps=edge,
        jito_tip_bps=50,
        priority_fee_bps=30,
        entry_slippage_bps=40,
        amm_fee_bps=30,
        exit_slippage_bps=40,
        adverse_selection_bps=150,
        total_cost_bps=parts,
    )


def _bad_cost_stack() -> CostStack:
    """A cost stack whose edge does NOT exceed cost (the cost gate must reject)."""
    parts = 50 + 30 + 40 + 30 + 40 + 150  # = 340
    return CostStack(
        expected_edge_bps=300,  # < 340 total
        jito_tip_bps=50,
        priority_fee_bps=30,
        entry_slippage_bps=40,
        amm_fee_bps=30,
        exit_slippage_bps=40,
        adverse_selection_bps=150,
        total_cost_bps=parts,
    )


def _clean_safety_inputs() -> SafetyInputs:
    """Decoded on-chain facts that PASS the pre-trade safety gate."""
    return SafetyInputs(
        mint=MINT,
        event_slot=100,
        event_block_time_ms=1_700_000_000_000,
        data_staleness_ms=100,
        freeze_authority=None,  # renounced
        mint_authority=None,  # renounced
        lp_locked_or_burned_bps=10_000,  # fully locked
        dev_bundle_cluster_bps=500,  # 5% — under cap
        holder_concentration_top10_bps=1_500,  # 15% — under cap
        sell_tax_bps=0,
        buy_tax_bps=0,
        authorities_decoded=True,
    )


def _honeypot_safety_inputs() -> SafetyInputs:
    """A honeypot: freeze authority set + 99% sell tax — the gate MUST reject."""
    return SafetyInputs(
        mint=MINT,
        event_slot=100,
        event_block_time_ms=1_700_000_000_000,
        data_staleness_ms=100,
        freeze_authority="EvilFreezeAuth1111111111111111111111111111",
        mint_authority="EvilMintAuth111111111111111111111111111111",
        lp_locked_or_burned_bps=0,
        dev_bundle_cluster_bps=9_000,
        holder_concentration_top10_bps=9_500,
        sell_tax_bps=9_900,  # 99% sell tax — honeypot
        buy_tax_bps=0,
        authorities_decoded=True,
    )


def _make_chain(
    *,
    breaker: CircuitBreaker | None = None,
    builder: _BuilderSpy | None = None,
    config: RiskConfig | None = None,
    denylist: object | None = None,
) -> tuple[GatedEntryFireChain, _BuilderSpy, CircuitBreaker]:
    cfg = config or RiskConfig()
    brk = breaker or CircuitBreaker(cfg, InMemoryBreakerStore(), _NoopFlatten())
    bld = builder or _BuilderSpy()
    chain = GatedEntryFireChain(
        safety_gate=PreTradeSafetyGate(),
        breaker=brk,
        sizer=FractionalKellySizer(cfg),
        cost_gate=CostAwareEntryGate(cfg),
        entry_builder=bld,
        denylist=denylist,  # type: ignore[arg-type]
    )
    return chain, bld, brk


def _buy_ctx(
    *,
    safety: SafetyInputs | None = None,
    cost: CostStack | None = None,
    p: float = 0.9,
    unc: float = 0.1,
    aggregate: int = 0,
    mint: str = MINT,
    creator: str | None = None,
) -> BuyFireContext:
    return BuyFireContext(
        mint=mint,
        block_time_ms=1_700_000_000_000,
        safety_inputs=safety if safety is not None else _clean_safety_inputs(),
        p_calibrated=p,
        uncertainty=unc,
        current_aggregate_lamports=aggregate,
        cost_stack=cost if cost is not None else _good_cost_stack(),
        creator=creator,
    )


def _simulate_within_tick(
    cfg: RiskConfig,
    *,
    n_buys: int,
    p: float,
    unc: float,
    start_aggregate: int,
) -> tuple[int, int, int]:
    """HERMETIC ground truth for the within-tick aggregate accounting.

    Re-derives — from a FRESH local FractionalKellySizer over the SAME RiskConfig,
    with NO global, no book, no chain — what evaluate_tick MUST produce when
    `n_buys` identical resting BUYs of one mint fire in a single tick starting from
    `start_aggregate` lamports already at risk.  It threads `committed_this_tick`
    exactly as the production B1 fix does: each successive buy is sized against the
    aggregate shrunk by everything reserved earlier this tick.

    Returns (expected_fired_count, expected_refused_count, expected_total_committed).
    The B1 tests assert the REAL evaluate_tick matches this tuple — so the test
    carries its own oracle and depends on no boundary constant.  Reverting the
    production `+ committed_this_tick` threading makes evaluate_tick size every buy
    to the FULL start-of-tick headroom (total ~= n_buys * per_buy >> cap), which
    cannot equal this threaded oracle: the mutation still drives the tests RED.
    """
    sizer = FractionalKellySizer(cfg)
    committed = 0
    fired = 0
    refused = 0
    for _ in range(n_buys):
        size = sizer.size(
            p_calibrated=p,
            uncertainty=unc,
            current_aggregate_lamports=start_aggregate + committed,
        ).size_lamports
        if size > 0:
            fired += 1
            committed += size
        else:
            refused += 1
    return fired, refused, committed


# =========================================================================== #
# 1. RESTING SELL / EXIT — ALWAYS ALLOWED (de-risk).  No gate, fires straight.
# =========================================================================== #


def test_resting_sell_full_exit_always_allowed():
    sell = RestingSellOrder(
        order_id="tp-1",
        mint=MINT,
        side=RestingSide.SELL,
        trigger_price=Decimal("3.0"),
        trigger_direction=TriggerDirection.AT_OR_ABOVE,  # take-profit
    )
    assert sell.is_triggered(Decimal("3.5")) is True
    intent = sell.fire(block_time_ms=42)
    assert isinstance(intent, ExitIntent)
    assert intent.fraction_bps == 10_000
    assert intent.exit_mode == "secure"  # defensive routing


def test_resting_reduce_partial_scaleout_always_allowed():
    red = RestingSellOrder(
        order_id="scale-1",
        mint=MINT,
        side=RestingSide.REDUCE,
        trigger_price=Decimal("2.0"),
        trigger_direction=TriggerDirection.AT_OR_ABOVE,
        fraction_bps=5_000,
    )
    intent = red.fire(block_time_ms=7)
    assert isinstance(intent, ReduceIntent)
    assert intent.fraction_bps == 5_000


def test_resting_sell_has_no_entry_capability():
    """Structural: a resting SELL cannot construct an EntryIntent — no such method."""
    sell = RestingSellOrder(
        order_id="s",
        mint=MINT,
        side=RestingSide.SELL,
        trigger_price=Decimal("1"),
        trigger_direction=TriggerDirection.AT_OR_BELOW,
    )
    assert not hasattr(sell, "entry")
    assert not hasattr(sell, "build_entry")
    # The factory it holds is de-risk only.
    assert not hasattr(sell._factory, "entry")


# =========================================================================== #
# 2. OFFLINE SURVIVAL — orders persist + fire after a "restart" (operator OFFLINE).
# =========================================================================== #


def test_resting_sell_survives_restart_and_fires_offline():
    store = InMemoryRestingOrderStore()
    # Operator places a resting take-profit, then the bot goes OFFLINE.
    book1 = RestingOrderBook(store)
    book1.place(
        RestingSellOrder(
            order_id="tp-offline",
            mint=MINT,
            side=RestingSide.SELL,
            trigger_price=Decimal("5.0"),
            trigger_direction=TriggerDirection.AT_OR_ABOVE,
        )
    )
    del book1  # bot process dies / restarts

    # A FRESH book re-reads the DURABLE store — the order is still armed.
    book2 = RestingOrderBook(store)
    assert "tp-offline" in book2.open_order_ids()

    # The price hits the trigger; the re-armed order fires.
    fires = book2.evaluate_tick(
        mint=MINT, mark_price=Decimal("5.5"), block_time_ms=1_700_000_000_123
    )
    assert len(fires) == 1
    assert fires[0].side is RestingSide.SELL
    assert isinstance(fires[0].sell_intent, ExitIntent)
    # Fired once and removed from the book + store (idempotent).
    assert "tp-offline" not in book2.open_order_ids()
    assert store.load_all() == ()


def test_off_box_keeper_fires_sell_with_whole_bot_dead():
    """A resting SELL placed off-box fires on a chain breach with NO loop running."""

    class _Keeper:
        def __init__(self) -> None:
            self.placed: dict[str, tuple] = {}
            self.fired: list[str] = []
            self._seq = 0

        def place_resting_sell(self, mint, order_id, trigger_price, fraction_bps) -> str:
            self._seq += 1
            handle = f"venue-{self._seq}"
            self.placed[handle] = (mint, order_id, trigger_price, fraction_bps)
            return handle

        def cancel_resting_sell(self, mint, venue_order_id) -> None:
            self.placed.pop(venue_order_id, None)

        def keeper_tick(self, mark: Decimal) -> None:
            # OFF-BOX: observe the chain and fire on breach — no bot loop involved.
            for handle, (_m, oid, trig, _f) in list(self.placed.items()):
                if mark <= trig:
                    self.fired.append(oid)
                    self.placed.pop(handle, None)

    keeper = _Keeper()
    store = InMemoryRestingOrderStore()
    book = RestingOrderBook(store, off_box_keeper=keeper)
    book.place(
        RestingSellOrder(
            order_id="stop-limit",
            mint=MINT,
            side=RestingSide.SELL,
            trigger_price=Decimal("60"),
            trigger_direction=TriggerDirection.AT_OR_BELOW,
        )
    )
    assert len(keeper.placed) == 1  # placed off-box

    # --- SIMULATE A FULLY DEAD BOT: we never call book.evaluate_tick(). ---
    keeper.keeper_tick(Decimal("55"))  # off-box keeper sees the breach
    assert keeper.fired == ["stop-limit"]  # fired with the bot dead


# =========================================================================== #
# 3. RESTING BUY — passes the FULL safety + sizing + cost stack AT FIRE TIME.
# =========================================================================== #


def test_resting_buy_fires_only_through_full_gate_stack():
    chain, builder, _brk = _make_chain()
    store = InMemoryRestingOrderStore()
    book = RestingOrderBook(store, gate_chain=chain)
    book.place(
        RestingBuyOrder(
            order_id="dip-buy",
            mint=MINT,
            trigger_price=Decimal("0.5"),
            trigger_direction=TriggerDirection.AT_OR_BELOW,  # limit dip-buy
        )
    )
    fires = book.evaluate_tick(
        mint=MINT,
        mark_price=Decimal("0.45"),  # dipped to/below the limit
        block_time_ms=1_700_000_000_000,
        buy_context=_buy_ctx(),
    )
    assert len(fires) == 1
    result = fires[0].buy_result
    assert result is not None
    assert result.fired is True
    assert isinstance(result.intent, EntryIntent)
    assert result.size_lamports > 0
    # The builder (the ONLY EntryIntent constructor) ran exactly once.
    assert len(builder.calls) == 1
    # A fired BUY is removed; a real entry is now live.
    assert "dip-buy" not in book.open_order_ids()


def test_resting_buy_blocked_by_safety_gate_is_refused():
    """A honeypot at fire time => safety gate rejects => NO buy (refuse-by-default)."""
    chain, builder, _brk = _make_chain()
    order = RestingBuyOrder(
        order_id="b",
        mint=MINT,
        trigger_price=Decimal("1"),
        trigger_direction=TriggerDirection.AT_OR_BELOW,
    )
    result = order.fire(_buy_ctx(safety=_honeypot_safety_inputs()), chain)
    assert result.fired is False
    assert result.rejection is BuyFireRejection.SAFETY_GATE
    assert result.intent is None
    assert builder.calls == []  # builder never invoked


def test_resting_buy_blocked_by_cost_gate_is_refused():
    """Edge below round-trip cost at fire time => cost gate rejects => NO buy."""
    chain, builder, _brk = _make_chain()
    order = RestingBuyOrder(
        order_id="b",
        mint=MINT,
        trigger_price=Decimal("1"),
        trigger_direction=TriggerDirection.AT_OR_BELOW,
    )
    result = order.fire(_buy_ctx(cost=_bad_cost_stack()), chain)
    assert result.fired is False
    assert result.rejection is BuyFireRejection.COST_GATE
    assert builder.calls == []


def test_resting_buy_blocked_by_breaker_is_refused():
    """A TRIPPED circuit breaker blocks ALL new entries — including parked buys."""
    cfg = RiskConfig()
    brk = CircuitBreaker(cfg, InMemoryBreakerStore(), _NoopFlatten())
    # Trip the breaker via the LLM de-risk path (it may trip, never reset).
    brk.trip_from_llm(LLMDeRiskSignal(reason="manufactured sentiment"), block_time_ms=1)
    assert brk.entries_allowed() is False

    chain, builder, _ = _make_chain(breaker=brk)
    order = RestingBuyOrder(
        order_id="b",
        mint=MINT,
        trigger_price=Decimal("1"),
        trigger_direction=TriggerDirection.AT_OR_BELOW,
    )
    result = order.fire(_buy_ctx(), chain)
    assert result.fired is False
    assert result.rejection is BuyFireRejection.BREAKER_TRIPPED
    assert builder.calls == []


def test_resting_buy_zero_size_is_refused():
    """No edge (p below break-even) => fractional-Kelly returns 0 => no buy."""
    chain, builder, _brk = _make_chain()
    order = RestingBuyOrder(
        order_id="b",
        mint=MINT,
        trigger_price=Decimal("1"),
        trigger_direction=TriggerDirection.AT_OR_BELOW,
    )
    # p=0.4 with default even-money odds => negative Kelly => size 0.
    result = order.fire(_buy_ctx(p=0.4, unc=0.1), chain)
    assert result.fired is False
    assert result.rejection is BuyFireRejection.ZERO_SIZE
    assert builder.calls == []


def test_resting_buy_with_no_chain_refuses_by_default():
    """The headline rule: a resting BUY may NEVER fire un-gated.  No chain => refuse."""
    order = RestingBuyOrder(
        order_id="b",
        mint=MINT,
        trigger_price=Decimal("1"),
        trigger_direction=TriggerDirection.AT_OR_BELOW,
    )
    result = order.fire(_buy_ctx(), chain=None)
    assert result.fired is False
    assert result.rejection is BuyFireRejection.NO_GATE_CHAIN
    assert result.intent is None


# --------------------------------------------------------------------------- #
# E2 — DENYLIST pre-filter is WIRED into the live entry chain (review fix).
#
# Regression for the FAILED-review finding E2-B1: a denylisted creator/mint must be
# EXCLUDED on the live BUY entry path, not merely in blacklist.py.  These drive the
# REAL GatedEntryFireChain with the REAL DenyAllowWatcher and assert the denylisted
# token is REJECTED at STEP 0 — before the safety gate, breaker, sizer, or cost gate.
# --------------------------------------------------------------------------- #

# These mirror the seed config keys (config/creator-token-denylist.json) so the
# integration is proven against the SAME shape the operator curates in production.
_DENY_MINT = "Scam1111111111111111111111111111111111111111"
_DENY_CREATOR = "Rug1111111111111111111111111111111111111111"


def _denylist_file(tmp_path, *, mints=None, creators=None) -> DenyAllowWatcher:
    import json

    p = tmp_path / "creator-token-denylist.json"
    p.write_text(
        json.dumps(
            {
                "version": "test",
                "denylist": {
                    "creators": [{"wallet": c} for c in (creators or [])],
                    "mints": [{"mint": m} for m in (mints or [])],
                },
                "allowlist": {"creators": [], "mints": []},
            }
        )
    )
    return DenyAllowWatcher(path=p, reload_throttle_s=0.0)


def test_denylisted_mint_rejected_pre_gate_on_live_entry_chain(tmp_path):
    """E2-B1 FIX: a denylisted MINT fed through the REAL entry chain is REJECTED at
    step 0 — before the safety gate ever runs.  Without the wiring this token PASSED
    the safety gate (it is otherwise clean) and would have fired a buy."""
    watcher = _denylist_file(tmp_path, mints=[_DENY_MINT])
    chain, builder, _brk = _make_chain(denylist=watcher)

    # A clean safety profile for the denylisted mint — proves the REJECT is the
    # denylist's doing, not a safety flag (the safety gate would otherwise PASS).
    clean = SafetyInputs(
        mint=_DENY_MINT,
        event_slot=100,
        event_block_time_ms=1_700_000_000_000,
        data_staleness_ms=100,
        freeze_authority=None,
        mint_authority=None,
        lp_locked_or_burned_bps=10_000,
        dev_bundle_cluster_bps=0,
        holder_concentration_top10_bps=0,
        sell_tax_bps=0,
        buy_tax_bps=0,
    )
    # Sanity: the safety gate ALONE passes this token (the gap the finding exploited).
    assert PreTradeSafetyGate().evaluate_fast(clean).passed is True

    order = RestingBuyOrder(
        order_id="b",
        mint=_DENY_MINT,
        trigger_price=Decimal("1"),
        trigger_direction=TriggerDirection.AT_OR_BELOW,
    )
    result = order.fire(_buy_ctx(mint=_DENY_MINT, safety=clean), chain)

    assert result.fired is False
    assert result.rejection is BuyFireRejection.DENYLISTED
    assert result.intent is None
    assert result.size_lamports == 0
    assert "denylist" in result.detail.lower()
    # Builder (the ONLY EntryIntent constructor) was NEVER reached — short-circuited.
    assert builder.calls == []


def test_denylisted_creator_rejected_pre_gate_on_live_entry_chain(tmp_path):
    """A denylisted CREATOR wallet (carried point-in-time on BuyFireContext) is also a
    pre-gate REJECT on the live chain — the veto is not mint-only."""
    watcher = _denylist_file(tmp_path, creators=[_DENY_CREATOR])
    chain, builder, _brk = _make_chain(denylist=watcher)
    order = RestingBuyOrder(
        order_id="b",
        mint=MINT,
        trigger_price=Decimal("1"),
        trigger_direction=TriggerDirection.AT_OR_BELOW,
    )
    result = order.fire(_buy_ctx(creator=_DENY_CREATOR), chain)
    assert result.fired is False
    assert result.rejection is BuyFireRejection.DENYLISTED
    assert builder.calls == []


def test_denylist_wired_through_order_book_evaluate_tick(tmp_path):
    """End-to-end through the BOOK: a triggered denylisted BUY on evaluate_tick is
    refused (DENYLISTED) and STAYS armed (a denied token never auto-buys)."""
    watcher = _denylist_file(tmp_path, mints=[_DENY_MINT])
    chain, builder, _brk = _make_chain(denylist=watcher)
    store = InMemoryRestingOrderStore()
    book = RestingOrderBook(store, gate_chain=chain)
    book.place(
        RestingBuyOrder(
            order_id="dip",
            mint=_DENY_MINT,
            trigger_price=Decimal("0.5"),
            trigger_direction=TriggerDirection.AT_OR_BELOW,
        )
    )
    clean = SafetyInputs(
        mint=_DENY_MINT,
        event_slot=100,
        event_block_time_ms=1_700_000_000_000,
        data_staleness_ms=100,
        freeze_authority=None,
        mint_authority=None,
        lp_locked_or_burned_bps=10_000,
        dev_bundle_cluster_bps=0,
        holder_concentration_top10_bps=0,
        sell_tax_bps=0,
        buy_tax_bps=0,
    )
    fires = book.evaluate_tick(
        mint=_DENY_MINT,
        mark_price=Decimal("0.45"),
        block_time_ms=1_700_000_000_000,
        buy_context=_buy_ctx(mint=_DENY_MINT, safety=clean),
    )
    assert len(fires) == 1
    res = fires[0].buy_result
    assert res is not None and res.fired is False
    assert res.rejection is BuyFireRejection.DENYLISTED
    assert builder.calls == []
    # A refused BUY stays armed (de-risk: never auto-buys, but not silently dropped).
    assert "dip" in book.open_order_ids()


def test_non_denylisted_buy_still_fires_through_full_stack_with_denylist_wired(tmp_path):
    """The denylist is SELECTIVITY-ONLY: a CLEAN, non-denylisted token still fires
    through the full gate stack when a (non-matching) denylist is wired.  The pre-
    filter must not block what it does not deny."""
    watcher = _denylist_file(tmp_path, mints=[_DENY_MINT], creators=[_DENY_CREATOR])
    chain, builder, _brk = _make_chain(denylist=watcher)
    order = RestingBuyOrder(
        order_id="ok",
        mint=MINT,  # not denylisted
        trigger_price=Decimal("1"),
        trigger_direction=TriggerDirection.AT_OR_BELOW,
    )
    result = order.fire(_buy_ctx(creator="GoodDev11111111111111111111111111111111111111"), chain)
    assert result.fired is True
    assert result.rejection is None
    assert isinstance(result.intent, EntryIntent)
    assert len(builder.calls) == 1


def test_denylist_membership_is_point_in_time_on_entry_chain(tmp_path):
    """Point-in-time: the denylist decision is a property of the KEY string, not of
    event-time.  Two fires of the same denylisted mint at wildly different block
    times yield the SAME reject (no compute-time leak; same code live + backtest)."""
    watcher = _denylist_file(tmp_path, mints=[_DENY_MINT])
    chain, _builder, _brk = _make_chain(denylist=watcher)
    order = RestingBuyOrder(
        order_id="b",
        mint=_DENY_MINT,
        trigger_price=Decimal("1"),
        trigger_direction=TriggerDirection.AT_OR_BELOW,
    )
    clean = SafetyInputs(
        mint=_DENY_MINT,
        event_slot=1,
        event_block_time_ms=1,
        data_staleness_ms=0,
        freeze_authority=None,
        mint_authority=None,
        lp_locked_or_burned_bps=10_000,
        dev_bundle_cluster_bps=0,
        holder_concentration_top10_bps=0,
        sell_tax_bps=0,
        buy_tax_bps=0,
    )
    ctx_early = BuyFireContext(
        mint=_DENY_MINT,
        block_time_ms=1,
        safety_inputs=clean,
        p_calibrated=0.9,
        uncertainty=0.1,
        current_aggregate_lamports=0,
        cost_stack=_good_cost_stack(),
    )
    ctx_late = BuyFireContext(
        mint=_DENY_MINT,
        block_time_ms=10**15,
        safety_inputs=clean,
        p_calibrated=0.9,
        uncertainty=0.1,
        current_aggregate_lamports=0,
        cost_stack=_good_cost_stack(),
    )
    r_early = order.fire(ctx_early, chain)
    r_late = order.fire(ctx_late, chain)
    assert r_early.rejection is BuyFireRejection.DENYLISTED
    assert r_late.rejection is BuyFireRejection.DENYLISTED
    # The red-flag vocabulary the dashboard renders is shared with the pre-gate path.
    assert RedFlag.MINT_DENYLISTED.value == "mint_denylisted"


def test_denylist_seam_is_de_risk_only_no_size_method():
    """STRUCTURAL: the denylist seam the chain holds can only veto.  The injected
    object's contract (check_keys -> hit|None) has no method that sizes/triggers a
    buy — a denylist cannot raise risk by construction."""
    from aats.risk.resting_orders import _DenylistLike

    # The Protocol exposes exactly one decision method and it returns a veto-or-None.
    assert hasattr(_DenylistLike, "check_keys")
    # No risk-increasing method names are part of the seam.
    forbidden = {"size", "buy", "trigger", "widen", "lift_cap", "allow_entry"}
    assert forbidden & set(dir(_DenylistLike)) == set()


def test_triggered_buy_with_no_context_does_not_buy():
    """A BUY that triggers on price but has NO fresh gate inputs must NOT fire a buy."""
    chain, builder, _brk = _make_chain()
    store = InMemoryRestingOrderStore()
    book = RestingOrderBook(store, gate_chain=chain)
    book.place(
        RestingBuyOrder(
            order_id="b",
            mint=MINT,
            trigger_price=Decimal("1"),
            trigger_direction=TriggerDirection.AT_OR_BELOW,
        )
    )
    # Price triggers, but the caller supplies NO BuyFireContext.
    fires = book.evaluate_tick(
        mint=MINT, mark_price=Decimal("0.5"), block_time_ms=1, buy_context=None
    )
    assert len(fires) == 1
    assert fires[0].buy_result is not None
    assert fires[0].buy_result.fired is False  # refuse-by-default
    # The order STAYS armed (it never auto-buys; it may pass with inputs later).
    assert "b" in book.open_order_ids()


def test_refused_buy_stays_armed_and_can_fire_later():
    """A buy refused now (honeypot) can still pass a later tick once it is clean."""
    chain, builder, _brk = _make_chain()
    store = InMemoryRestingOrderStore()
    book = RestingOrderBook(store, gate_chain=chain)
    book.place(
        RestingBuyOrder(
            order_id="b",
            mint=MINT,
            trigger_price=Decimal("1"),
            trigger_direction=TriggerDirection.AT_OR_BELOW,
        )
    )
    # Tick 1: honeypot => refused, stays armed.
    f1 = book.evaluate_tick(
        mint=MINT,
        mark_price=Decimal("0.5"),
        block_time_ms=1,
        buy_context=_buy_ctx(safety=_honeypot_safety_inputs()),
    )
    assert f1[0].buy_result.fired is False
    assert "b" in book.open_order_ids()
    # Tick 2: clean inputs => fires.
    f2 = book.evaluate_tick(
        mint=MINT, mark_price=Decimal("0.5"), block_time_ms=2, buy_context=_buy_ctx()
    )
    assert f2[0].buy_result.fired is True
    assert "b" not in book.open_order_ids()


# =========================================================================== #
# 4. NO RESTING ORDER CAN BYPASS THE SAFETY GATE (the headline non-waivable).
# =========================================================================== #


def test_no_buy_path_constructs_entry_without_all_gates():
    """Structural + behavioral: the ONLY EntryIntent comes from the builder, and the
    builder is reached ONLY after safety+breaker+sizing+cost all pass."""
    # RestingBuyOrder holds no factory and no entry constructor.
    order = RestingBuyOrder(
        order_id="b",
        mint=MINT,
        trigger_price=Decimal("1"),
        trigger_direction=TriggerDirection.AT_OR_BELOW,
    )
    assert not hasattr(order, "build_entry")
    assert not hasattr(order, "_factory")

    chain, builder, _brk = _make_chain()
    # Each single-gate failure prevents the builder from ever being called.
    for ctx in (
        _buy_ctx(safety=_honeypot_safety_inputs()),  # safety fails
        _buy_ctx(cost=_bad_cost_stack()),  # cost fails
        _buy_ctx(p=0.3),  # sizing -> 0
    ):
        res = order.fire(ctx, chain)
        assert res.fired is False
    assert builder.calls == []  # builder NEVER reached on any failing gate


def test_aggregate_cap_bounds_a_resting_buy_size():
    """A parked buy cannot exceed the aggregate-exposure cap — sized at fire time."""
    cfg = RiskConfig()  # aggregate cap 0.5 SOL
    chain, builder, _brk = _make_chain(config=cfg)
    order = RestingBuyOrder(
        order_id="b",
        mint=MINT,
        trigger_price=Decimal("1"),
        trigger_direction=TriggerDirection.AT_OR_BELOW,
    )
    # Already at the aggregate cap => remaining headroom 0 => sizing returns 0.
    res = order.fire(_buy_ctx(aggregate=cfg.max_aggregate_lamports), chain)
    assert res.fired is False
    assert res.rejection is BuyFireRejection.ZERO_SIZE


# =========================================================================== #
# 5. DCA — each tranche is individually gated; the plan can't exceed the cap.
# =========================================================================== #


def test_dca_plan_builds_gated_buy_tranches():
    plan = DcaPlan(plan_id="dca-1", mint=MINT, tranches=3, step_interval=10)
    orders = tuple(plan.buy_order_for_tranche(i) for i in range(3))
    assert len(orders) == 3
    assert all(isinstance(o, RestingBuyOrder) for o in orders)
    assert all(o.side is RestingSide.BUY for o in orders)
    assert plan.tranche_steps() == (0, 10, 20)


def test_dca_tranches_each_run_the_full_gate_stack():
    """Every DCA tranche is a deferred gated entry — no tranche bypasses."""
    chain, builder, _brk = _make_chain()
    store = InMemoryRestingOrderStore()
    book = RestingOrderBook(store, gate_chain=chain)
    plan = DcaPlan(plan_id="dca", mint=MINT, tranches=2, step_interval=5)
    placed = book.place_dca(plan)
    assert len(placed) == 2
    assert len(book.open_order_ids()) == 2

    # A honeypot tranche fire is refused (does not bypass safety).
    f = book.evaluate_tick(
        mint=MINT,
        mark_price=Decimal("1"),
        block_time_ms=1,
        event_step=0,
        buy_context=_buy_ctx(safety=_honeypot_safety_inputs()),
    )
    assert all(tf.buy_result is not None and tf.buy_result.fired is False for tf in f)
    assert builder.calls == []


def test_dca_tranche_defers_until_its_scheduled_event_step():
    """A scheduled tranche does NOT fire before its event-time step (point-in-time)."""
    chain, builder, _brk = _make_chain()
    store = InMemoryRestingOrderStore()
    book = RestingOrderBook(store, gate_chain=chain)
    plan = DcaPlan(plan_id="dca", mint=MINT, tranches=1, step_interval=10, first_step=10)
    book.place_dca(plan)  # tranche scheduled for step 10

    # Step 5: price triggers but the schedule has NOT been reached -> no fire, armed.
    f_early = book.evaluate_tick(
        mint=MINT, mark_price=Decimal("1"), block_time_ms=1, event_step=5, buy_context=_buy_ctx()
    )
    assert f_early == ()  # the tranche is not even evaluated before its step
    assert len(book.open_order_ids()) == 1
    assert builder.calls == []

    # Step 10: schedule reached -> the tranche fires through the full gate stack.
    f_due = book.evaluate_tick(
        mint=MINT, mark_price=Decimal("1"), block_time_ms=2, event_step=10, buy_context=_buy_ctx()
    )
    assert len(f_due) == 1
    assert f_due[0].buy_result is not None and f_due[0].buy_result.fired is True
    assert len(builder.calls) == 1


def test_dca_aggregate_cap_shrinks_or_refuses_later_tranches():
    """The plan total can NEVER exceed the aggregate cap: a later tranche is refused
    once earlier tranches have consumed the headroom (sized at fire time)."""
    cfg = RiskConfig()
    chain, builder, _brk = _make_chain(config=cfg)
    order = RestingBuyOrder(
        order_id="t1",
        mint=MINT,
        trigger_price=Decimal("1"),
        trigger_direction=TriggerDirection.AT_OR_BELOW,
    )
    # Headroom available -> a tranche fires with a positive size.
    r1 = order.fire(_buy_ctx(aggregate=0), chain)
    assert r1.fired is True and r1.size_lamports > 0
    # After earlier tranches filled the book to the cap, the next is refused.
    r2 = order.fire(_buy_ctx(aggregate=cfg.max_aggregate_lamports), chain)
    assert r2.fired is False and r2.rejection is BuyFireRejection.ZERO_SIZE


def test_dca_catchup_within_one_tick_never_breaches_aggregate_cap():
    """B1 regression: MANY DCA tranches becoming due in ONE evaluate_tick (a late
    event_step catch-up) must NOT, in aggregate, breach the hard aggregate-exposure
    cap.  Each fired BUY this tick reserves its committed lamports so the next is
    sized against the shrunken headroom; once headroom is exhausted, the rest refuse.

    Before the fix, 30 tranches at p~1 in one tick committed 0.75 SOL against the
    0.5 SOL cap.  Now the SUM of fired sizes must be <= max_aggregate_lamports."""
    cfg = RiskConfig()  # per-trade 0.1 SOL, aggregate cap 0.5 SOL
    chain, builder, _brk = _make_chain(config=cfg)
    store = InMemoryRestingOrderStore()
    book = RestingOrderBook(store, gate_chain=chain)

    n_tranches = 30  # >= 22; far more than fit under the 0.5 SOL cap
    plan = DcaPlan(
        plan_id="catchup",
        mint=MINT,
        tranches=n_tranches,
        step_interval=1,
        first_step=0,
        # schedule-only (no price trigger) so a single late event_step makes EVERY
        # earlier-due tranche eligible at once — the catch-up vector.
    )
    book.place_dca(plan)
    assert len(book.open_order_ids()) == n_tranches

    # ONE tick at a late event_step (>= the last tranche's step) with p~1, unc~0,
    # starting from a clean book (aggregate 0): every tranche is step-ready AND
    # price-ready, so all 30 are evaluated in this single tick.
    fires = book.evaluate_tick(
        mint=MINT,
        mark_price=Decimal("1"),
        block_time_ms=1_700_000_000_000,
        event_step=n_tranches,  # past the last scheduled step -> all due
        buy_context=_buy_ctx(p=1.0, unc=0.0, aggregate=0),
    )

    assert len(fires) == n_tranches  # every tranche was evaluated this tick
    fired = [f.buy_result for f in fires if f.buy_result is not None and f.buy_result.fired]
    refused = [f.buy_result for f in fires if f.buy_result is not None and not f.buy_result.fired]
    total_committed = sum(r.size_lamports for r in fired)

    # THE INVARIANT: the firm's total NEW open SOL this tick is bounded by the cap.
    assert total_committed <= cfg.max_aggregate_lamports, (
        f"within-tick aggregate breach: committed {total_committed} lamports "
        f"> cap {cfg.max_aggregate_lamports}"
    )

    # HERMETIC ORACLE (T-326 re-spin): re-derive what the within-tick accounting
    # MUST yield from a fresh local sizer over the SAME config, threading the
    # commitment exactly as the B1 fix does — no global, no boundary constant.
    exp_fired, exp_refused, exp_total = _simulate_within_tick(
        cfg, n_buys=n_tranches, p=1.0, unc=0.0, start_aggregate=0
    )
    # The real evaluate_tick matches the threaded oracle to the lamport.  Reverting
    # the production `+ committed_this_tick` sizes every BUY to the full headroom
    # (total ~= n * per_buy >> cap), which cannot equal exp_total: still RED.
    assert total_committed == exp_total
    assert len(fired) == exp_fired
    assert len(refused) == exp_refused
    # The cap actually BOUND: it took fewer than all tranches to exhaust the
    # headroom (some fired, some refused) — proven by the oracle, not by chance.
    assert exp_fired > 0 and exp_refused > 0, (
        "test premise: the local sizer must fit SOME but not ALL tranches under the "
        f"cap (fired={exp_fired}, refused={exp_refused}); the within-tick cap is then bound."
    )
    # The refused tranches stay armed (de-risk: a refused BUY never auto-buys).
    assert len(book.open_order_ids()) == len(refused)
    # The builder was invoked exactly once per FIRED tranche (no un-gated entries).
    assert len(builder.calls) == len(fired)


def test_multiple_distinct_resting_buys_same_mint_one_tick_respect_aggregate_cap():
    """Same B1 vector via DISTINCT resting BUY orders (not a DCA plan) of one mint
    all triggering in a single tick: their combined committed size must still honor
    the aggregate cap (within-tick accounting is not DCA-specific)."""
    cfg = RiskConfig()
    chain, builder, _brk = _make_chain(config=cfg)
    store = InMemoryRestingOrderStore()
    book = RestingOrderBook(store, gate_chain=chain)

    n = 25
    for i in range(n):
        book.place(
            RestingBuyOrder(
                order_id=f"limit-{i:02d}",
                mint=MINT,
                trigger_price=Decimal("1"),
                trigger_direction=TriggerDirection.AT_OR_BELOW,  # all trigger at mark<=1
            )
        )

    fires = book.evaluate_tick(
        mint=MINT,
        mark_price=Decimal("0.5"),  # below every trigger -> all eligible at once
        block_time_ms=1_700_000_000_000,
        buy_context=_buy_ctx(p=1.0, unc=0.0, aggregate=0),
    )
    fired = [f.buy_result for f in fires if f.buy_result is not None and f.buy_result.fired]
    total = sum(r.size_lamports for r in fired)
    # The hard cap bounds the combined within-tick commitment.
    assert total <= cfg.max_aggregate_lamports
    # And it matches the threaded oracle to the lamport (hermetic, no global):
    # reverting the within-tick accounting blows total past the cap, failing both.
    exp_fired, _exp_refused, exp_total = _simulate_within_tick(
        cfg, n_buys=n, p=1.0, unc=0.0, start_aggregate=0
    )
    assert total == exp_total
    assert len(fired) == exp_fired


def test_within_tick_accounting_respects_preexisting_aggregate():
    """The start-of-tick aggregate is NOT ignored: if the book already holds open
    exposure, the within-tick fires plus the pre-existing exposure together stay
    under the cap (committed_this_tick is ADDED to the supplied aggregate)."""
    cfg = RiskConfig()
    chain, builder, _brk = _make_chain(config=cfg)
    store = InMemoryRestingOrderStore()
    book = RestingOrderBook(store, gate_chain=chain)

    n = 30
    for i in range(n):
        book.place(
            RestingBuyOrder(
                order_id=f"l-{i:02d}",
                mint=MINT,
                trigger_price=Decimal("1"),
                trigger_direction=TriggerDirection.AT_OR_BELOW,
            )
        )

    # Already half-full: 0.25 SOL of the 0.5 SOL cap is committed coming in.
    preexisting = cfg.max_aggregate_lamports // 2
    fires = book.evaluate_tick(
        mint=MINT,
        mark_price=Decimal("0.5"),
        block_time_ms=1_700_000_000_000,
        buy_context=_buy_ctx(p=1.0, unc=0.0, aggregate=preexisting),
    )
    fired = [f.buy_result for f in fires if f.buy_result is not None and f.buy_result.fired]
    new_committed = sum(r.size_lamports for r in fired)
    # Pre-existing + everything fired this tick must not exceed the cap.
    assert preexisting + new_committed <= cfg.max_aggregate_lamports
    # Hermetic oracle: the start-of-tick aggregate is honored AND threaded — the
    # within-tick fires equal what a fresh local sizer yields starting from
    # `preexisting`.  Reverting either the pre-existing add or the within-tick
    # threading breaks this equality (and the cap bound above).
    exp_fired, _exp_refused, exp_new = _simulate_within_tick(
        cfg, n_buys=n, p=1.0, unc=0.0, start_aggregate=preexisting
    )
    assert new_committed == exp_new
    assert len(fired) == exp_fired
    # The pre-existing exposure genuinely shrank the headroom (fewer fit than would
    # from an empty book) — proving the supplied aggregate was not ignored.
    exp_fired_empty, _r, _t = _simulate_within_tick(
        cfg, n_buys=n, p=1.0, unc=0.0, start_aggregate=0
    )
    assert exp_fired < exp_fired_empty


def _run_within_tick_catchup(cfg: RiskConfig) -> tuple[int, int, int]:
    """Drive the REAL within-tick DCA catch-up path once and return
    (n_fired, n_refused, total_committed_lamports).  No fixture, no global pin —
    the caller controls the ambient decimal context."""
    chain, _builder, _brk = _make_chain(config=cfg)
    store = InMemoryRestingOrderStore()
    book = RestingOrderBook(store, gate_chain=chain)
    n = 30
    book.place_dca(DcaPlan(plan_id="hostile", mint=MINT, tranches=n, step_interval=1, first_step=0))
    fires = book.evaluate_tick(
        mint=MINT,
        mark_price=Decimal("1"),
        block_time_ms=1_700_000_000_000,
        event_step=n,
        buy_context=_buy_ctx(p=1.0, unc=0.0, aggregate=0),
    )
    fired = [f.buy_result for f in fires if f.buy_result is not None and f.buy_result.fired]
    refused = [f.buy_result for f in fires if f.buy_result is not None and not f.buy_result.fired]
    return len(fired), len(refused), sum(r.size_lamports for r in fired)


@pytest.mark.no_hermetic_decimal
def test_sizing_hermetic_under_hostile_decimal_context():
    """ROOT-CAUSE REGRESSION (T-326): the within-tick aggregate-cap path is now
    immune to the PROCESS-GLOBAL decimal context.

    This test OPTS OUT of the autouse context pin and itself installs every kind of
    hostile global context — low precision, every rounding mode — that a misbehaving
    sibling or dependency could leave behind.  Before the production fix (pinning a
    fixed local context inside FractionalKellySizer.size), a low-precision rounding-UP
    context drove the applied Kelly fraction past the 1/4 cap and tripped sizing.py's
    invariant assert (a hard ERROR), and the fractional steps were not reproducible.
    After the fix the result MUST be byte-identical to the standard context AND the
    hard aggregate cap MUST hold — under EVERY context.  This is the diagnosed flake,
    converted into a pinned invariant: no ambient context can perturb the size."""
    cfg = RiskConfig()
    saved = decimal.getcontext()
    try:
        # Reference under the canonical production context.
        decimal.setcontext(decimal.DefaultContext.copy())
        ref_fired, ref_refused, ref_total = _run_within_tick_catchup(cfg)
        # The reference itself must obey the hard cap and actually BIND it.
        assert ref_total <= cfg.max_aggregate_lamports
        assert ref_fired > 0 and ref_refused > 0

        precs = (1, 2, 3, 5, 6, 7, 9, 15, 28)
        roundings = (
            decimal.ROUND_HALF_EVEN,
            decimal.ROUND_DOWN,
            decimal.ROUND_UP,
            decimal.ROUND_CEILING,
            decimal.ROUND_FLOOR,
            decimal.ROUND_05UP,
            decimal.ROUND_HALF_UP,
        )
        for prec, rnd in itertools.product(precs, roundings):
            decimal.setcontext(decimal.Context(prec=prec, rounding=rnd))
            fired, refused, total = _run_within_tick_catchup(cfg)
            ctx_desc = f"prec={prec} rounding={rnd}"
            # HARD CAP holds under EVERY hostile context (the SAFETY invariant).
            assert total <= cfg.max_aggregate_lamports, (
                f"within-tick aggregate breach under {ctx_desc}: "
                f"committed {total} > cap {cfg.max_aggregate_lamports}"
            )
            # And the result is BYTE-IDENTICAL to the standard context — proving the
            # production path is hermetic (point-in-time reproducible), not merely
            # "happens to stay under the cap" for this context.
            assert (fired, refused, total) == (ref_fired, ref_refused, ref_total), (
                f"within-tick result diverged under {ctx_desc}: "
                f"{(fired, refused, total)} != ref {(ref_fired, ref_refused, ref_total)} "
                "— the sizing path is not hermetic w.r.t. the global decimal context"
            )
    finally:
        decimal.setcontext(saved)


# =========================================================================== #
# 6. MONEY / TYPE DISCIPLINE — float money rejected; integer/Decimal only.
# =========================================================================== #


def test_float_trigger_price_rejected_sell():
    with pytest.raises(TypeError):
        RestingSellOrder(
            order_id="s",
            mint=MINT,
            side=RestingSide.SELL,
            trigger_price=1.5,  # float money — forbidden
            trigger_direction=TriggerDirection.AT_OR_ABOVE,
        )


def test_float_trigger_price_rejected_buy():
    with pytest.raises(TypeError):
        RestingBuyOrder(
            order_id="b",
            mint=MINT,
            trigger_price=0.5,  # float money — forbidden
            trigger_direction=TriggerDirection.AT_OR_BELOW,
        )


def test_float_block_time_rejected_on_evaluate():
    store = InMemoryRestingOrderStore()
    book = RestingOrderBook(store)
    with pytest.raises(TypeError):
        book.evaluate_tick(mint=MINT, mark_price=Decimal("1"), block_time_ms=1.0)


def test_sell_side_rejects_buy_value():
    with pytest.raises(ValueError):
        RestingSellOrder(
            order_id="x",
            mint=MINT,
            side=RestingSide.BUY,  # a SELL order cannot be a BUY
            trigger_price=Decimal("1"),
            trigger_direction=TriggerDirection.AT_OR_BELOW,
        )


# =========================================================================== #
# 7. POINT-IN-TIME — the evaluator is pure; event-time stamps ids; no wall-clock.
# =========================================================================== #


def test_sell_intent_id_is_event_time_deterministic():
    sell = RestingSellOrder(
        order_id="o",
        mint=MINT,
        side=RestingSide.SELL,
        trigger_price=Decimal("1"),
        trigger_direction=TriggerDirection.AT_OR_BELOW,
    )
    i1 = sell.fire(block_time_ms=123)
    i2 = sell.fire(block_time_ms=123)
    # Same event-time => same client_intent_id (idempotent replay; no wall-clock).
    assert i1.client_intent_id == i2.client_intent_id
    i3 = sell.fire(block_time_ms=124)
    assert i3.client_intent_id != i1.client_intent_id


def test_trigger_is_pure_comparison_no_lookahead():
    buy = RestingBuyOrder(
        order_id="b",
        mint=MINT,
        trigger_price=Decimal("100"),
        trigger_direction=TriggerDirection.AT_OR_BELOW,
    )
    assert buy.is_triggered(Decimal("100")) is True  # at
    assert buy.is_triggered(Decimal("99")) is True  # below
    assert buy.is_triggered(Decimal("101")) is False  # above
    sell = RestingSellOrder(
        order_id="s",
        mint=MINT,
        side=RestingSide.SELL,
        trigger_price=Decimal("100"),
        trigger_direction=TriggerDirection.AT_OR_ABOVE,
    )
    assert sell.is_triggered(Decimal("100")) is True
    assert sell.is_triggered(Decimal("101")) is True
    assert sell.is_triggered(Decimal("99")) is False


def test_breaker_recorded_pnl_blocks_buy_via_real_path():
    """End-to-end: a real loss past the daily floor trips the breaker; a parked buy
    is then blocked — the breaker and resting-order paths agree."""
    cfg = RiskConfig()
    brk = CircuitBreaker(cfg, InMemoryBreakerStore(), _NoopFlatten())
    # Drive a loss past the -0.30 SOL floor in event-time.
    brk.record_pnl(
        PnLEvent(mint=MINT, block_time_ms=1_700_000_000_000, delta_lamports=-400_000_000)
    )
    assert brk.is_tripped() is True

    chain, builder, _ = _make_chain(breaker=brk)
    order = RestingBuyOrder(
        order_id="b",
        mint=MINT,
        trigger_price=Decimal("1"),
        trigger_direction=TriggerDirection.AT_OR_BELOW,
    )
    res = order.fire(_buy_ctx(), chain)
    assert res.fired is False
    assert res.rejection is BuyFireRejection.BREAKER_TRIPPED
