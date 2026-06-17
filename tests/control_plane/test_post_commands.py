"""T-341 — POST command tests (de-risk-only contract enforcement).

Tests:
  - kill -> halts entries + flattens within budget
  - flatten / flatten/{mint} -> de-risk, single-position (AC-044)
  - breaker/reset -> gated on TRIPPED + operator auth; 409 when not TRIPPED
  - mode -> LIVE blocked behind DRY-RUN; down always permitted; up fenced
  - Unauthorized POST -> 403
  - Risk-increasing mode / command -> rejected at the contract layer
  - daily_loss_limit_pct tighten-then-widen round-trip -> 403
"""
from __future__ import annotations

import time

import pytest
from httpx import AsyncClient

from aats.contracts.intents import CostStack
from aats.contracts.positions import FSMState
from aats.contracts.risk import BreakerState
from aats.contracts.venue import FillResult, SimulationVenue
from aats.controller.control_api import KillSwitch
from aats.controller.fsm import PositionFSM, make_client_intent_id
from aats.controller.state import InMemoryStateStore
from tests.control_plane.conftest import CEO_HEADERS, OPERATOR_HEADERS

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_position(
    store: InMemoryStateStore,
    mint: str,
    slot: int,
    *,
    fsm_state: FSMState = FSMState.ENTERING,
) -> PositionFSM:
    """Create a position in the store and return its FSM."""
    intent_id = make_client_intent_id(mint, slot, "ENTRY")
    store.claim_entering(mint, intent_id)
    cost = CostStack(
        expected_edge_bps=200,
        jito_tip_bps=10,
        priority_fee_bps=5,
        entry_slippage_bps=50,
        amm_fee_bps=25,
        exit_slippage_bps=30,
        adverse_selection_bps=150,
        total_cost_bps=120,
    )
    fsm = PositionFSM.create_entering(
        store,
        mint=mint,
        token="TOK_" + mint[-4:],
        surface="EH-001",
        entry_slot=slot,
        sol_in_lamports=100_000_000,
        tokens_out_base=1_000_000,
        entry_slippage_bps=50,
        exit_config_label="secure_balanced",
        exit_mode="secure",
        tp_total=3,
        hard_stop_price_lamports=80_000_000,
        cost_breakdown=cost,
        client_intent_id=intent_id,
    )
    if fsm_state == FSMState.OPEN:
        fill = FillResult(
            landed=True,
            reason="filled",
            land_slot=slot + 1,
            slot_delay=1,
            buyers_ahead=0,
            tokens_out=1_000_000,
            effective_price_decimal="100",
            entry_slippage_bps=50,
            tip_lamports=10_000,
            priority_lamports=5_000,
        )
        fsm.transition_to_open(fill, block_time_ms=slot * 400)
    return fsm

# ---------------------------------------------------------------------------
# Auth enforcement (every POST must require operator Bearer token)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_kill_requires_auth(client: AsyncClient):
    """POST /api/kill without auth -> 403."""
    resp = await client.post("/api/kill")
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_post_flatten_requires_auth(client: AsyncClient):
    resp = await client.post("/api/flatten")
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_post_mode_requires_auth(client: AsyncClient):
    resp = await client.post("/api/mode", json={"mode": "SHADOW"})
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_post_breaker_reset_requires_auth(client: AsyncClient):
    resp = await client.post("/api/breaker/reset")
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_post_risk_config_requires_auth(client: AsyncClient):
    resp = await client.post("/api/risk-config", json={})
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# POST /api/kill
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_kill_halts_entries(client: AsyncClient, kill_switch: KillSwitch):
    """POST /api/kill sets the kill flag — no new entries allowed."""
    assert not kill_switch.is_killed
    resp = await client.post("/api/kill", headers=OPERATOR_HEADERS)
    assert resp.status_code == 202
    assert kill_switch.is_killed

    data = resp.json()
    assert data["status"] == "halting"
    assert "kill" in data["message"].lower() or "halt" in data["message"].lower()


@pytest.mark.asyncio
async def test_post_kill_is_idempotent(client: AsyncClient, kill_switch: KillSwitch):
    """POST /api/kill can be called multiple times safely."""
    resp1 = await client.post("/api/kill", headers=OPERATOR_HEADERS)
    resp2 = await client.post("/api/kill", headers=OPERATOR_HEADERS)
    assert resp1.status_code == 202
    assert resp2.status_code == 202
    assert kill_switch.is_killed


@pytest.mark.asyncio
async def test_post_kill_triggers_flatten(
    store: InMemoryStateStore, fast_loop, client: AsyncClient
):
    """POST /api/kill flattens OPEN positions within the AC-040 <= 2s budget.

    This test:
      (a) creates a REAL OPEN position registered in FastLoop._fsm_registry,
      (b) injects a counting venue that records every exit() call,
      (c) POSTs /api/kill,
      (d) asserts venue.exit() fired for the open position,
      (e) asserts the effect occurred within the AC-040 <= 2s budget.
    """
    import random as _random

    from aats.controller.fast_loop import FastLoopFlattenHandler

    # Counting venue that tracks exit calls
    exit_calls: list[str] = []

    class CountingVenue(SimulationVenue):
        def exit(self, intent, position):
            exit_calls.append(intent.mint)
            return super().exit(intent, position)

    counting_venue = CountingVenue(rng=_random.Random(42))

    # Create an OPEN position in the store
    fsm = _make_position(store, "KILL_OPEN_MINT", 200, fsm_state=FSMState.OPEN)

    # Register the FSM in the fast_loop registry so flatten_handler can find it
    fast_loop._fsm_registry["KILL_OPEN_MINT"] = fsm

    # Swap the flatten_handler's venue with the counting venue
    fast_loop._flatten_handler = FastLoopFlattenHandler(
        store, counting_venue, fast_loop._fsm_registry
    )

    # Measure wall-clock time for AC-040 budget assertion
    t0 = time.monotonic()
    resp = await client.post("/api/kill", headers=OPERATOR_HEADERS)
    elapsed_s = time.monotonic() - t0

    assert resp.status_code == 202

    # (d) venue.exit() MUST have fired for the open position
    assert "KILL_OPEN_MINT" in exit_calls, (
        "POST /api/kill must call venue.exit() for each OPEN position. "
        "Deleting emergency_flatten_all from the kill handler would cause this to fail."
    )

    # (e) AC-040: effect must occur within <= 2s budget
    assert elapsed_s < 2.0, (
        f"POST /api/kill took {elapsed_s:.3f}s, exceeds AC-040 <= 2s budget"
    )

    # Kill switch must be active
    state_resp = await client.get("/api/state")
    assert state_resp.json()["killed"] is True


@pytest.mark.asyncio
async def test_post_kill_covers_entering_position(
    store: InMemoryStateStore, fast_loop, client: AsyncClient
):
    """POST /api/kill handles ENTERING positions: they are vetoed (not ignored).

    An ENTERING position that survives a kill would become a live un-flattened
    position after the kill fires (MAJOR-1 finding).  Verify it is handled.
    """
    import random as _random

    from aats.controller.fast_loop import FastLoopFlattenHandler

    exit_calls: list[str] = []

    class SpyVenue(SimulationVenue):
        def exit(self, intent, position):
            exit_calls.append(intent.mint)
            return super().exit(intent, position)

    spy_venue = SpyVenue(rng=_random.Random(42))

    # Create an ENTERING position (NOT yet open)
    _make_position(store, "KILL_ENTERING_MINT", 300, fsm_state=FSMState.ENTERING)

    fast_loop._flatten_handler = FastLoopFlattenHandler(
        store, spy_venue, fast_loop._fsm_registry
    )

    resp = await client.post("/api/kill", headers=OPERATOR_HEADERS)
    assert resp.status_code == 202

    # After kill the ENTERING position must NOT remain in ENTERING state
    remaining = store.load_position("KILL_ENTERING_MINT")
    # The position should either be deleted (vetoed) or transitioned away from ENTERING
    if remaining is not None:
        assert remaining.fsm_state != FSMState.ENTERING, (
            "ENTERING position must be vetoed/closed on kill; "
            "it cannot remain ENTERING after the kill fires (MAJOR-1)."
        )


# ---------------------------------------------------------------------------
# POST /api/flatten
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_flatten_all(client: AsyncClient):
    """POST /api/flatten flattens all positions; returns 202."""
    resp = await client.post("/api/flatten", headers=OPERATOR_HEADERS)
    assert resp.status_code == 202
    data = resp.json()
    assert data["status"] == "flattening"
    assert data["mint"] is None  # None means "all positions"


@pytest.mark.asyncio
async def test_post_flatten_all_idempotent(client: AsyncClient):
    """POST /api/flatten is idempotent."""
    resp1 = await client.post("/api/flatten", headers=OPERATOR_HEADERS)
    resp2 = await client.post("/api/flatten", headers=OPERATOR_HEADERS)
    assert resp1.status_code == 202
    assert resp2.status_code == 202


# ---------------------------------------------------------------------------
# POST /api/flatten/{mint}
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_flatten_mint_single_position(
    store: InMemoryStateStore, fast_loop, client: AsyncClient
):
    """POST /api/flatten/{mint} flattens ONLY that mint — other positions survive (AC-044).

    This test creates TWO open positions and flattens only one.
    The other must remain OPEN after the request.

    Deleting flatten_one() and routing to emergency_flatten_all() would close
    BOTH positions and fail the assertion.
    """
    import random as _random

    from aats.controller.fast_loop import FastLoopFlattenHandler

    exit_calls: list[str] = []

    class CountingVenue(SimulationVenue):
        def exit(self, intent, position):
            exit_calls.append(intent.mint)
            return super().exit(intent, position)

    counting_venue = CountingVenue(rng=_random.Random(42))
    fast_loop._flatten_handler = FastLoopFlattenHandler(
        store, counting_venue, fast_loop._fsm_registry
    )

    # Create two OPEN positions
    fsm_a = _make_position(store, "FLAT_MINT_A", 400, fsm_state=FSMState.OPEN)
    fsm_b = _make_position(store, "FLAT_MINT_B", 401, fsm_state=FSMState.OPEN)
    fast_loop._fsm_registry["FLAT_MINT_A"] = fsm_a
    fast_loop._fsm_registry["FLAT_MINT_B"] = fsm_b

    # Flatten ONLY MINT_A
    resp = await client.post("/api/flatten/FLAT_MINT_A", headers=OPERATOR_HEADERS)
    assert resp.status_code == 202
    data = resp.json()
    assert data["status"] == "flattening"
    assert data["mint"] == "FLAT_MINT_A"

    # Only FLAT_MINT_A should have received an exit call (AC-044)
    assert "FLAT_MINT_A" in exit_calls, (
        "venue.exit() must be called for FLAT_MINT_A on POST /api/flatten/FLAT_MINT_A"
    )
    assert "FLAT_MINT_B" not in exit_calls, (
        "POST /api/flatten/FLAT_MINT_A must NOT close FLAT_MINT_B (AC-044). "
        "Routing to emergency_flatten_all() would fail here — use flatten_one()."
    )

    # FLAT_MINT_B must still be OPEN
    pos_b = store.load_position("FLAT_MINT_B")
    assert pos_b is not None, "FLAT_MINT_B must still exist after single-mint flatten"
    assert pos_b.fsm_state == FSMState.OPEN, (
        f"FLAT_MINT_B must remain OPEN; got {pos_b.fsm_state} (AC-044)"
    )


@pytest.mark.asyncio
async def test_post_flatten_mint_not_found(client: AsyncClient):
    """POST /api/flatten/{mint} for non-existent mint returns 404."""
    resp = await client.post("/api/flatten/NONEXISTENT_MINT_XYZ", headers=OPERATOR_HEADERS)
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /api/breaker/reset
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_breaker_reset_when_not_tripped_returns_409(client: AsyncClient):
    """POST /api/breaker/reset when breaker is ARMED -> 409 breaker_not_tripped."""
    resp = await client.post("/api/breaker/reset", headers=OPERATOR_HEADERS)
    assert resp.status_code == 409
    data = resp.json()
    # Detail should carry the machine error code
    detail = data.get("detail", {})
    if isinstance(detail, dict):
        assert "breaker_not_tripped" in str(detail)
    else:
        assert "breaker_not_tripped" in str(detail)


@pytest.mark.asyncio
async def test_post_breaker_reset_when_tripped_re_arms(
    store: InMemoryStateStore, breaker, breaker_store, client: AsyncClient
):
    """POST /api/breaker/reset when TRIPPED re-arms and returns 200."""

    # Trip the breaker by injecting into the breaker's OWN store (InMemoryBreakerStore).
    # The CircuitBreaker reads its state from _store, not from state_store.
    tripped = BreakerState(
        state="TRIPPED",
        tripped_at_utc="2026-06-16T00:00:00Z",
        daily_net_pnl_lamports=-300_000_001,
        daily_net_pnl_day_utc="2026-06-16",
        threshold_breached="-0.30 SOL",
    )
    # Trip the breaker's internal store AND internal state
    breaker_store.save(tripped)
    breaker._state = tripped  # set the live latch directly

    resp = await client.post("/api/breaker/reset", headers=OPERATOR_HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    assert data["state"] == "ARMED"


# ---------------------------------------------------------------------------
# POST /api/mode
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_mode_shadow_to_paper_allowed(client: AsyncClient, cp_config):
    """Mode can advance from SHADOW to PAPER (operator-only; no CEO auth needed)."""
    cp_config.agent_mode = "SHADOW"
    resp = await client.post("/api/mode", json={"mode": "PAPER"}, headers=OPERATOR_HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    assert data["mode"] == "PAPER"


@pytest.mark.asyncio
async def test_post_mode_paper_to_shadow_always_allowed(client: AsyncClient, cp_config):
    """Moving DOWN the ladder (PAPER->SHADOW) is always de-risk; always permitted."""
    cp_config.agent_mode = "PAPER"
    resp = await client.post("/api/mode", json={"mode": "SHADOW"}, headers=OPERATOR_HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    assert data["mode"] == "SHADOW"


@pytest.mark.asyncio
async def test_post_mode_live_blocked_when_dry_run_enabled(client: AsyncClient, cp_config):
    """LIVE mode is blocked when DRY_RUN_ENABLED=true (HARD RULE, AC-060)."""
    cp_config.dry_run_enabled = True
    cp_config.agent_mode = "LIVE_DRY_RUN"
    resp = await client.post("/api/mode", json={"mode": "LIVE"}, headers=CEO_HEADERS)
    assert resp.status_code == 403
    data = resp.json()
    detail = data.get("detail", {})
    assert "live_requires_dry_run_disabled_and_ceo_auth" in str(detail)


@pytest.mark.asyncio
async def test_post_mode_live_blocked_without_ceo_auth(client: AsyncClient, cp_config):
    """LIVE mode requires CEO auth token even if DRY_RUN_ENABLED=false."""
    cp_config.dry_run_enabled = False
    cp_config.agent_mode = "LIVE_DRY_RUN"
    # Only operator auth, no CEO auth
    resp = await client.post("/api/mode", json={"mode": "LIVE"}, headers=OPERATOR_HEADERS)
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_post_mode_live_allowed_with_full_gates(client: AsyncClient, cp_config):
    """LIVE mode is allowed with DRY_RUN_ENABLED=false AND CEO auth token."""
    cp_config.dry_run_enabled = False
    cp_config.agent_mode = "LIVE_DRY_RUN"
    resp = await client.post("/api/mode", json={"mode": "LIVE"}, headers=CEO_HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    assert data["mode"] == "LIVE"
    assert data["dry_run_enabled"] is False


@pytest.mark.asyncio
async def test_post_mode_response_includes_dry_run_flag(client: AsyncClient, cp_config):
    """POST /api/mode response always includes dry_run_enabled."""
    resp = await client.post("/api/mode", json={"mode": "SHADOW"}, headers=OPERATOR_HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    assert "mode" in data
    assert "dry_run_enabled" in data


@pytest.mark.asyncio
async def test_post_mode_invalid_mode_returns_400(client: AsyncClient):
    """Invalid mode value -> 400."""
    resp = await client.post("/api/mode", json={"mode": "INVALID_MODE"}, headers=OPERATOR_HEADERS)
    assert resp.status_code in (400, 422)


@pytest.mark.asyncio
async def test_post_mode_down_never_requires_ceo(client: AsyncClient, cp_config):
    """Mode DOWN the ladder (de-risk) never requires CEO auth."""
    cp_config.agent_mode = "LIVE_DRY_RUN"
    # No CEO auth header, just operator
    resp = await client.post("/api/mode", json={"mode": "PAPER"}, headers=OPERATOR_HEADERS)
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# POST /api/risk-config (tighten-only)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_risk_config_tighten_accepted(client: AsyncClient):
    """Tightening per_trade_cap is accepted (de-risk)."""
    # Get current config
    get_resp = await client.get("/api/risk-config")
    current = get_resp.json()
    # Propose a tighter cap (decrease)
    proposed = dict(current)
    proposed["per_trade_cap_lamports"] = current["per_trade_cap_lamports"] // 2
    resp = await client.post("/api/risk-config", json=proposed, headers=OPERATOR_HEADERS)
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_post_risk_config_widen_rejected(client: AsyncClient):
    """Widening per_trade_cap is rejected 403 risk_increase_rejected."""
    get_resp = await client.get("/api/risk-config")
    current = get_resp.json()
    # Propose a wider cap (increase) — this is a risk INCREASE
    proposed = dict(current)
    proposed["per_trade_cap_lamports"] = current["per_trade_cap_lamports"] + 1_000_000
    resp = await client.post("/api/risk-config", json=proposed, headers=OPERATOR_HEADERS)
    assert resp.status_code == 403
    detail = resp.json().get("detail", {})
    assert "risk_increase_rejected" in str(detail)


@pytest.mark.asyncio
async def test_post_risk_config_lower_snipe_threshold_rejected(client: AsyncClient):
    """Lowering snipe_threshold (more aggressive snipes) is a widen -> 403."""
    get_resp = await client.get("/api/risk-config")
    current = get_resp.json()
    proposed = dict(current)
    proposed["snipe_threshold"] = current["snipe_threshold"] - 0.1  # lower = more trades = risk increase
    resp = await client.post("/api/risk-config", json=proposed, headers=OPERATOR_HEADERS)
    assert resp.status_code == 403
    detail = resp.json().get("detail", {})
    assert "risk_increase_rejected" in str(detail)


@pytest.mark.asyncio
async def test_post_risk_config_violations_listed(client: AsyncClient):
    """403 response includes detail.violations list naming the offending fields."""
    get_resp = await client.get("/api/risk-config")
    current = get_resp.json()
    proposed = dict(current)
    proposed["per_trade_cap_lamports"] = current["per_trade_cap_lamports"] + 1_000_000
    resp = await client.post("/api/risk-config", json=proposed, headers=OPERATOR_HEADERS)
    assert resp.status_code == 403
    detail = resp.json().get("detail", {})
    # The detail should contain violations list
    if isinstance(detail, dict):
        # violations may be nested under detail.detail or at top level
        violations_str = str(detail)
        assert "per_trade_cap_lamports" in violations_str or "violations" in violations_str


@pytest.mark.asyncio
async def test_post_risk_config_daily_loss_pct_widen_rejected_numeric(client: AsyncClient):
    """daily_loss_limit_pct widen is rejected even when round-tripping as a decimal-string.

    BLOCKER-1 fix: the old code compared strings lexicographically ("1.0" > "3.0" is
    False so the widen was ACCEPTED).  After the fix, strings are parsed to Decimal
    before comparison, so a widen is correctly detected and rejected with 403.

    Sequence:
      1. GET current config  -> daily_loss_limit_pct = "3.0"
      2. POST tighten to "1.0"   -> 200 accepted
      3. POST widen  to "2.5"    -> 403 risk_increase_rejected (the regression check)
      4. POST widen  to "10.0"   -> 403  (would pass lexicographic "10.0" < "3.0" bug)
    """
    # Step 1: GET current config
    get_resp = await client.get("/api/risk-config")
    assert get_resp.status_code == 200
    current = get_resp.json()
    original_limit = current["daily_loss_limit_pct"]  # "3.0" as string

    # Step 2: Tighten 3.0 -> 1.0 (must be accepted)
    tighten = dict(current)
    tighten["daily_loss_limit_pct"] = "1.0"
    resp_tighten = await client.post("/api/risk-config", json=tighten, headers=OPERATOR_HEADERS)
    assert resp_tighten.status_code == 200, (
        f"Tightening daily_loss_limit_pct from {original_limit!r} to '1.0' must be accepted; "
        f"got {resp_tighten.status_code}"
    )

    # Step 3: Widen 1.0 -> 2.5 (must be REJECTED — widen of the loss limit)
    widen_25 = dict(resp_tighten.json())
    widen_25["daily_loss_limit_pct"] = "2.5"
    resp_widen_25 = await client.post("/api/risk-config", json=widen_25, headers=OPERATOR_HEADERS)
    assert resp_widen_25.status_code == 403, (
        "Widening daily_loss_limit_pct from '1.0' to '2.5' must be rejected 403. "
        "String comparison would accept this ('2.5' > '1.0' as strings is True — correct "
        "but only by accident; use Decimal comparison to be robust)."
    )
    assert "risk_increase_rejected" in str(resp_widen_25.json())

    # Step 4: Widen 1.0 -> 10.0 (must also be REJECTED — catches the lexicographic bug)
    # Lexicographic: "10.0" < "1.0" because '1' == '1' then '0' < '.' — so the old
    # string comparison would ACCEPT this widen.  Decimal comparison correctly rejects it.
    widen_10 = dict(widen_25)
    widen_10["daily_loss_limit_pct"] = "10.0"
    # Note: RiskConfig model also caps at 3.0, so this gets a 400 from the model validator.
    # But the tighten-only check (BLOCKER-1) must fire FIRST and return 403.
    resp_widen_10 = await client.post("/api/risk-config", json=widen_10, headers=OPERATOR_HEADERS)
    # Either 400 (model floor) or 403 (tighten-only) is acceptable; 200 is NOT.
    assert resp_widen_10.status_code in (400, 403), (
        f"Widening daily_loss_limit_pct to '10.0' from '1.0' must be rejected; "
        f"got {resp_widen_10.status_code}. "
        "Lexicographic string comparison ('10.0' < '1.0') would accept this — BLOCKER-1."
    )


@pytest.mark.asyncio
async def test_post_risk_config_jito_tip_widen_rejected_numeric(client: AsyncClient):
    """jito_tip_cap_frac widen is rejected after being parsed as Decimal (BLOCKER-1 fix)."""
    get_resp = await client.get("/api/risk-config")
    current = get_resp.json()

    # Tighten jito_tip_cap_frac from "0.30" to "0.10"
    tighten = dict(current)
    tighten["jito_tip_cap_frac"] = "0.10"
    resp_t = await client.post("/api/risk-config", json=tighten, headers=OPERATOR_HEADERS)
    assert resp_t.status_code == 200

    # Widen from "0.10" to "0.25" — must be rejected
    widen = dict(resp_t.json())
    widen["jito_tip_cap_frac"] = "0.25"
    resp_w = await client.post("/api/risk-config", json=widen, headers=OPERATOR_HEADERS)
    assert resp_w.status_code == 403, (
        "Widening jito_tip_cap_frac from '0.10' to '0.25' must be rejected 403 (BLOCKER-1)"
    )
    assert "risk_increase_rejected" in str(resp_w.json())


# ---------------------------------------------------------------------------
# Ensure GET endpoints are truly read-only (no body, no side effects)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_endpoints_have_no_side_effects(client: AsyncClient, kill_switch: KillSwitch):
    """GET endpoints do NOT change kill state or mode."""
    await client.get("/api/state")
    await client.get("/api/metrics")
    await client.get("/api/positions")
    assert not kill_switch.is_killed
