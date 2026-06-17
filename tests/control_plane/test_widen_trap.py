"""T-341 att.3 — Mutation-meaningful widen-rejection regression tests.

QA-MAJOR-1 FIX: the previous widen-rejection tests used value pairs that agree
under BOTH lexicographic AND numeric ordering, making them mutation-vacuous.
Deleting the _coerce Decimal branch left 96/96 GREEN because string comparison
happened to produce the same result.

These tests use value pairs where lexicographic ordering and numeric ordering
DISAGREE — so a mutation that removes _coerce will cause them to go RED.

The canonical lex vs numeric trap:
  "10.0" < "2.5"   (lexicographic: '1' < '2')
  10.0   > 2.5     (numeric: ten is greater than two-point-five)

A string comparison would MISS the widen "2.5" -> "10.0" (because "10.0" < "2.5"
lex, so `new > cur` is False). Decimal comparison CATCHES it (10.0 > 2.5 = True).

These are DIRECT UNIT TESTS on _validate_risk_config_tighten_only (not the API
endpoint) so model-level field constraints (e.g. daily_loss_limit_pct <= 3.0)
do not apply. The function tests the comparison logic only.

MUTATION PROOF (required per task directive):
  1. Run these tests -> GREEN.
  2. Temporarily delete the `_coerce` Decimal branch in server.py.
  3. Run again -> the widen-trap tests go RED.
  4. Restore `_coerce`.

FIX 2 (R-3 nit): POST status-code contract conformance tests added below.
  Per api-contracts.md §3 and §5:
    kill / flatten / flatten/{mint} -> 202 (async de-risk)
    breaker/reset -> 200 (sync result) | 409 (not tripped)
    mode -> 200 | 403
    risk-config -> 200 (sync, returns persisted config)
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from httpx import AsyncClient

from aats.control_plane.server import _validate_risk_config_tighten_only
from tests.control_plane.conftest import OPERATOR_HEADERS

# ---------------------------------------------------------------------------
# FIX 1 — Direct unit tests on _validate_risk_config_tighten_only
# These MUST go RED when the _coerce Decimal branch is removed.
# ---------------------------------------------------------------------------


class TestWidenTrapDecimalVsLexicographic:
    """Unit tests that catch the lexicographic ordering trap.

    The trap: Python string comparison orders "10.0" BEFORE "2.5" because
    character '1' < '2'. So without Decimal coercion, the check
    `new > cur` evaluates as `"10.0" > "2.5"` which is FALSE (lex), meaning
    the widen from 2.5 to 10.0 would be silently ACCEPTED.

    These tests MUST be green with the _coerce branch intact and go RED if the
    _coerce branch is deleted or disabled.
    """

    def test_daily_loss_limit_pct_lex_numeric_trap(self):
        """daily_loss_limit_pct: "2.5" -> "10.0" is a WIDEN that must be caught.

        Lexicographic:  "10.0" < "2.5"  (because '1' < '2')
            => string `new > cur` is FALSE => widen NOT detected by lex comparison.
        Numeric:        10.0  > 2.5
            => Decimal `new > cur` is TRUE  => widen DETECTED.

        Without _coerce: test goes RED (no violation detected).
        With    _coerce: test stays GREEN (violation correctly detected).
        """
        current = {"daily_loss_limit_pct": "2.5"}
        proposed = {"daily_loss_limit_pct": "10.0"}

        violations = _validate_risk_config_tighten_only(current, proposed)

        assert violations == ["daily_loss_limit_pct"], (
            f"Widen from '2.5' to '10.0' must be flagged as a violation. "
            f"Got violations={violations!r}. "
            "Lexicographic string comparison ('10.0' < '2.5' lex) would NOT catch "
            "this widen — Decimal comparison is required. "
            "MUTATION: delete _coerce and this test goes RED."
        )

    def test_jito_tip_cap_frac_lex_numeric_trap(self):
        """jito_tip_cap_frac: "2.5" -> "10.0" is a WIDEN that must be caught.

        jito_tip_cap_frac is a Decimal-string field (in _DECIMAL_STRING_FIELDS).
        The same lex vs numeric ordering trap applies to it.

        Lexicographic:  "10.0" < "2.5"  (because '1' < '2')
            => string `new > cur` is FALSE => widen NOT detected by lex comparison.
        Numeric:        10.0  > 2.5
            => Decimal `new > cur` is TRUE  => widen DETECTED.

        The function tests the comparison logic only; model-level field constraints
        (jito_tip_cap_frac <= 0.30) do not apply to this direct unit test — we are
        verifying that _coerce correctly handles ANY decimal-string pair for this
        field, including values that would be rejected by the API model.

        Without _coerce: test goes RED (no violation detected).
        With    _coerce: test stays GREEN (violation correctly detected).
        """
        current = {"jito_tip_cap_frac": "2.5"}
        proposed = {"jito_tip_cap_frac": "10.0"}

        violations = _validate_risk_config_tighten_only(current, proposed)

        assert violations == ["jito_tip_cap_frac"], (
            f"Widen from '2.5' to '10.0' for jito_tip_cap_frac must be flagged. "
            f"Got violations={violations!r}. "
            "Lexicographic string comparison ('10.0' < '2.5' lex) would NOT catch "
            "this widen — Decimal comparison is required (_DECIMAL_STRING_FIELDS). "
            "MUTATION: delete _coerce and this test goes RED."
        )

    def test_daily_loss_limit_pct_confirm_lex_ordering_disagrees(self):
        """Explicitly confirm that Python lex ordering gives the WRONG answer for the trap.

        This test documents WHY the _coerce branch is load-bearing: without it,
        the widen "2.5" -> "10.0" would be SILENTLY ACCEPTED.
        """
        # Python string comparison: "10.0" < "2.5" because '1' < '2'
        assert "10.0" < "2.5", (
            "Python lex: '10.0' < '2.5' (char '1' < '2'). "
            "This is why we must use Decimal, not raw string comparison."
        )
        # Numeric comparison (correct): 10.0 > 2.5
        assert Decimal("10.0") > Decimal("2.5"), (
            "Decimal: 10.0 > 2.5. This is the correct answer."
        )
        # Without _coerce: `new > cur` as strings => "10.0" > "2.5" is FALSE => no violation
        raw_new = "10.0"
        raw_cur = "2.5"
        assert not (raw_new > raw_cur), (
            "String comparison 'new > cur' returns False for '10.0' > '2.5'. "
            "A mutation that removes _coerce would produce zero violations for this widen."
        )
        # With _coerce: Decimal("10.0") > Decimal("2.5") is TRUE => violation detected
        assert Decimal(raw_new) > Decimal(raw_cur), (
            "Decimal comparison correctly returns True: 10.0 > 2.5."
        )

    def test_no_false_positive_on_tighten(self):
        """A tighten must NOT be flagged as a violation.

        "10.0" -> "2.5" is a TIGHTEN (smaller loss limit = tighter).
        Even though "10.0" > "2.5" lex is TRUE (wrong direction for our check),
        the Decimal comparison correctly says 2.5 < 10.0 so no violation.
        """
        current = {"daily_loss_limit_pct": "10.0"}
        proposed = {"daily_loss_limit_pct": "2.5"}

        violations = _validate_risk_config_tighten_only(current, proposed)

        assert "daily_loss_limit_pct" not in violations, (
            f"Tightening from '10.0' to '2.5' must NOT be flagged. Got {violations!r}."
        )

    def test_equal_value_not_flagged(self):
        """Equal current and proposed must NOT trigger a violation."""
        current = {"daily_loss_limit_pct": "3.0"}
        proposed = {"daily_loss_limit_pct": "3.0"}

        violations = _validate_risk_config_tighten_only(current, proposed)

        assert "daily_loss_limit_pct" not in violations, (
            f"Equal value '3.0' -> '3.0' must not be flagged. Got {violations!r}."
        )

    def test_multiple_violations_all_reported(self):
        """When both daily_loss_limit_pct and jito_tip_cap_frac widen, both reported."""
        current = {
            "daily_loss_limit_pct": "2.5",
            "jito_tip_cap_frac": "2.5",
        }
        proposed = {
            "daily_loss_limit_pct": "10.0",
            "jito_tip_cap_frac": "10.0",
        }

        violations = _validate_risk_config_tighten_only(current, proposed)

        assert "daily_loss_limit_pct" in violations, (
            f"daily_loss_limit_pct widen must be in violations. Got {violations!r}."
        )
        assert "jito_tip_cap_frac" in violations, (
            f"jito_tip_cap_frac widen must be in violations. Got {violations!r}."
        )

    def test_missing_field_not_flagged(self):
        """If proposed does not include the field, no violation (no change = fine)."""
        current = {"daily_loss_limit_pct": "2.5"}
        proposed = {}

        violations = _validate_risk_config_tighten_only(current, proposed)

        assert "daily_loss_limit_pct" not in violations, (
            f"Missing field should not produce a violation. Got {violations!r}."
        )


# ---------------------------------------------------------------------------
# FIX 2 (R-3 nit) — POST status-code contract conformance
# api-contracts.md §3 and §5 define the expected HTTP status codes.
# ---------------------------------------------------------------------------


class TestPostStatusCodeContractConformance:
    """Verify POST command status codes match the frozen api-contracts.md contract.

    Per api-contracts.md §3:
      200 ok (sync success)
      202 accepted (async de-risk initiated)
      400 bad request
      403 unauthorized or risk-increase rejected
      409 conflict

    Per api-contracts.md §5:
      POST /api/kill           -> 202  (async: "halt entries, hand to ExitEngine")
      POST /api/flatten        -> 202  (async)
      POST /api/flatten/{mint} -> 202  (async, single position)
      POST /api/breaker/reset  -> 200  (sync, returns persisted state) | 409 not tripped
      POST /api/mode           -> 200  (sync, returns new mode) | 403 gated
      POST /api/risk-config    -> 200  (sync, returns persisted config) | 403 widen
    """

    @pytest.mark.asyncio
    async def test_post_kill_returns_202(self, client: AsyncClient):
        """POST /api/kill -> 202 (async de-risk, api-contracts.md §5)."""
        resp = await client.post("/api/kill", headers=OPERATOR_HEADERS)
        assert resp.status_code == 202, (
            f"POST /api/kill must return 202 per api-contracts.md §5; got {resp.status_code}"
        )

    @pytest.mark.asyncio
    async def test_post_flatten_returns_202(self, client: AsyncClient):
        """POST /api/flatten -> 202 (async de-risk, api-contracts.md §5)."""
        resp = await client.post("/api/flatten", headers=OPERATOR_HEADERS)
        assert resp.status_code == 202, (
            f"POST /api/flatten must return 202 per api-contracts.md §5; got {resp.status_code}"
        )

    @pytest.mark.asyncio
    async def test_post_flatten_mint_returns_202(
        self, client: AsyncClient, store
    ):
        """POST /api/flatten/{mint} -> 202 (async de-risk, api-contracts.md §5)."""
        from aats.contracts.intents import CostStack
        from aats.controller.fsm import PositionFSM, make_client_intent_id

        mint = "STATUS_TEST_MINT"
        intent_id = make_client_intent_id(mint, 999, "ENTRY")
        store.claim_entering(mint, intent_id)
        cost = CostStack(
            expected_edge_bps=200, jito_tip_bps=10, priority_fee_bps=5,
            entry_slippage_bps=50, amm_fee_bps=25, exit_slippage_bps=30,
            adverse_selection_bps=150, total_cost_bps=120,
        )
        PositionFSM.create_entering(
            store, mint=mint, token="STOK", surface="EH-001",
            entry_slot=999, sol_in_lamports=100_000_000, tokens_out_base=1_000_000,
            entry_slippage_bps=50, exit_config_label="secure_balanced",
            exit_mode="secure", tp_total=3, hard_stop_price_lamports=80_000_000,
            cost_breakdown=cost, client_intent_id=intent_id,
        )
        resp = await client.post(f"/api/flatten/{mint}", headers=OPERATOR_HEADERS)
        assert resp.status_code == 202, (
            f"POST /api/flatten/{{mint}} must return 202 per api-contracts.md §5; "
            f"got {resp.status_code}"
        )

    @pytest.mark.asyncio
    async def test_post_breaker_reset_returns_200_when_tripped(
        self, client: AsyncClient, breaker, breaker_store
    ):
        """POST /api/breaker/reset -> 200 when TRIPPED (api-contracts.md §5)."""
        from aats.contracts.risk import BreakerState

        tripped = BreakerState(
            state="TRIPPED",
            tripped_at_utc="2026-06-16T00:00:00Z",
            daily_net_pnl_lamports=-300_000_001,
            daily_net_pnl_day_utc="2026-06-16",
            threshold_breached="-0.30 SOL",
        )
        breaker_store.save(tripped)
        breaker._state = tripped

        resp = await client.post("/api/breaker/reset", headers=OPERATOR_HEADERS)
        assert resp.status_code == 200, (
            f"POST /api/breaker/reset when TRIPPED must return 200 per api-contracts.md §5; "
            f"got {resp.status_code}"
        )

    @pytest.mark.asyncio
    async def test_post_breaker_reset_returns_409_when_armed(self, client: AsyncClient):
        """POST /api/breaker/reset -> 409 when ARMED (api-contracts.md §5)."""
        resp = await client.post("/api/breaker/reset", headers=OPERATOR_HEADERS)
        assert resp.status_code == 409, (
            f"POST /api/breaker/reset when ARMED must return 409 per api-contracts.md §5; "
            f"got {resp.status_code}"
        )

    @pytest.mark.asyncio
    async def test_post_mode_returns_200_on_success(self, client: AsyncClient, cp_config):
        """POST /api/mode -> 200 on success (api-contracts.md §5)."""
        cp_config.agent_mode = "SHADOW"
        resp = await client.post(
            "/api/mode", json={"mode": "PAPER"}, headers=OPERATOR_HEADERS
        )
        assert resp.status_code == 200, (
            f"POST /api/mode (valid mode change) must return 200 per api-contracts.md §5; "
            f"got {resp.status_code}"
        )

    @pytest.mark.asyncio
    async def test_post_mode_returns_403_on_live_without_gates(
        self, client: AsyncClient, cp_config
    ):
        """POST /api/mode LIVE without gates -> 403 (api-contracts.md §5)."""
        cp_config.dry_run_enabled = True
        resp = await client.post(
            "/api/mode", json={"mode": "LIVE"}, headers=OPERATOR_HEADERS
        )
        assert resp.status_code == 403, (
            f"POST /api/mode LIVE without DRY_RUN_ENABLED=false must return 403; "
            f"got {resp.status_code}"
        )

    @pytest.mark.asyncio
    async def test_post_risk_config_returns_200_on_tighten(self, client: AsyncClient):
        """POST /api/risk-config (tighten) -> 200 (api-contracts.md §5, sync response)."""
        get_resp = await client.get("/api/risk-config")
        current = get_resp.json()
        proposed = dict(current)
        proposed["per_trade_cap_lamports"] = current["per_trade_cap_lamports"] // 2
        resp = await client.post("/api/risk-config", json=proposed, headers=OPERATOR_HEADERS)
        assert resp.status_code == 200, (
            f"POST /api/risk-config (tighten) must return 200 per api-contracts.md §5; "
            f"got {resp.status_code}"
        )

    @pytest.mark.asyncio
    async def test_post_risk_config_returns_403_on_widen(self, client: AsyncClient):
        """POST /api/risk-config (widen) -> 403 (api-contracts.md §5)."""
        get_resp = await client.get("/api/risk-config")
        current = get_resp.json()
        proposed = dict(current)
        proposed["per_trade_cap_lamports"] = current["per_trade_cap_lamports"] + 1_000_000
        resp = await client.post("/api/risk-config", json=proposed, headers=OPERATOR_HEADERS)
        assert resp.status_code == 403, (
            f"POST /api/risk-config (widen) must return 403 per api-contracts.md §5; "
            f"got {resp.status_code}"
        )
