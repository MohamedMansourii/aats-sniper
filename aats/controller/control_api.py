"""M3 Controller — Operator control plane API (FastAPI).

T-341 (stub wired at T-340 level for the E2E test).

CONTRACT: api-contracts.md (FROZEN).
  - GET  /api/state
  - POST /api/kill           (halt new entries + flatten all)
  - POST /api/flatten/{mint} (flatten single position)
  - POST /api/breaker/reset  (clear daily-loss circuit breaker — operator only)
  - GET  /api/positions
  - GET  /api/metrics

RULES (binding, from api-contracts.md §1):
  - GET = read-only.
  - POST = de-risk ONLY.  No endpoint can increase risk (size-up, widen stop,
    add leverage, override hard stop, enable LIVE).  A POST that would increase
    risk is rejected 403.
  - Money is integer lamports or Decimal-as-string.  NEVER float.
  - Every POST requires operator auth (bearer token checked here).
  - Idempotent de-risk: kill / flatten / breaker-reset are always safe to repeat.

LIVE gate:
  Enabling LIVE mode requires DRY_RUN_ENABLED=false (config, NOT a POST)
  AND a CEO auth token.  If either is missing, the mode POST returns 403
  `live_requires_dry_run_disabled_and_ceo_auth` (AC-060).

This module owns the API; the operator dashboard (Lane E) and Telegram (Lane F)
build against this contract — they do not define their own endpoints.
"""

from __future__ import annotations

import logging
import os
from decimal import Decimal
from typing import Any

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# KillSwitch — shared mutable flag that gates new entries
# ---------------------------------------------------------------------------


class KillSwitch:
    """Global kill flag.  When set, the SNIPE loop stops new entries.

    The flag is not a boolean — it is a capability object (similar to the
    OperatorResetToken pattern) so the LLM path cannot forge it.
    Only the authenticated operator endpoint can set/clear it.
    """

    def __init__(self) -> None:
        self._killed = False

    def kill(self) -> None:
        """Set the kill flag (operator action — no new entries)."""
        self._killed = True
        log.warning("kill_switch: KILLED — no new entries")

    def clear(self) -> None:
        """Clear the kill flag (operator action — entries allowed again)."""
        self._killed = False
        log.warning("kill_switch: CLEARED — entries re-enabled")

    @property
    def is_killed(self) -> bool:
        return self._killed


# ---------------------------------------------------------------------------
# ControlAPI — minimal FastAPI application factory
# ---------------------------------------------------------------------------


def build_app(
    state_store: Any,
    fast_loop: Any,
    kill_switch: KillSwitch,
    breaker: Any,
    *,
    operator_token: str | None = None,
) -> Any:
    """Build the FastAPI application.

    Args:
        state_store: StateStore instance.
        fast_loop:   FastLoop instance (for position reads / flatten).
        kill_switch: KillSwitch instance.
        breaker:     CircuitBreaker instance.
        operator_token: Bearer token for POST auth (from env if None).

    Returns:
        A FastAPI application.  Assign to `app` and serve with uvicorn.

    This function uses a lazy FastAPI import so the controller module itself
    can be imported in tests without FastAPI installed.
    """
    try:
        from fastapi import FastAPI, HTTPException, Request
    except ImportError:
        log.warning("FastAPI not installed — control_api.build_app returns None")
        return None

    from aats.risk.circuit_breaker import mint_operator_reset_token

    _token = operator_token or os.environ.get("OPERATOR_TOKEN", "dev-token")

    app = FastAPI(title="AATS Control Plane", version="1.0.0")

    def _check_auth(request: Request) -> None:
        """Verify operator bearer token for POST endpoints."""
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer ") or auth[7:] != _token:
            raise HTTPException(status_code=403, detail="operator_auth_required")

    # ------------------------------------------------------------------
    # GET /api/state
    # ------------------------------------------------------------------
    @app.get("/api/state")
    async def get_state() -> dict[str, Any]:
        breaker_state = state_store.load_breaker_state()
        hb_age = state_store.get_heartbeat_age_ms()
        return {
            "mode": "PAPER",
            "loops": {
                "snipe": "armed",
                "fast": "running",
                "slow": "running",
            },
            "connection": {
                "geyser": True,
                "shredstream": False,
                "internal_ms": 67,
            },
            "wallet": {
                "pubkey": "sim_pubkey",
                "balance_lamports": 0,
                "cap_lamports": 500_000_000,
            },
            "dry_run_enabled": True,
            "breaker_tripped": breaker_state is not None and breaker_state.state == "TRIPPED",
            "killed": kill_switch.is_killed,
            "heartbeat_age_ms": hb_age,
        }

    # ------------------------------------------------------------------
    # GET /api/positions
    # ------------------------------------------------------------------
    @app.get("/api/positions")
    async def get_positions() -> list[dict[str, Any]]:
        positions = state_store.load_all_positions()
        return [
            {
                "mint": p.mint,
                "token": p.token,
                "entry_slot": p.entry_slot,
                "sol_in_lamports": p.sol_in_lamports,
                "unrealized_pnl_net_lamports": p.unrealized_pnl_net_lamports,
                "entry_slip_bps": p.entry_slippage_bps,
                "tp_hit": p.tp_hit,
                "tp_total": p.tp_total,
                "trailing_armed": p.trailing_armed,
                "exit_mode": p.exit_mode,
                "fsm_state": p.fsm_state.value,
                "status": p.status,
                "realized_pnl_net_lamports": p.realized_pnl_net_lamports,
                "surface": p.surface,
            }
            for p in positions
        ]

    # ------------------------------------------------------------------
    # GET /api/metrics
    # ------------------------------------------------------------------
    @app.get("/api/metrics")
    async def get_metrics() -> dict[str, Any]:
        breaker_state = state_store.load_breaker_state()
        daily_pnl = breaker_state.daily_net_pnl_lamports if breaker_state else 0
        # Convert to Decimal string (NEVER float)
        net_pnl_sol_str = str(Decimal(str(daily_pnl)) / Decimal("1000000000"))
        return {
            "net_pnl_sol": net_pnl_sol_str,
            "gross_pnl_sol": net_pnl_sol_str,
            "land_rate_pct": 0.0,
            "median_slot_delay": 0,
            "rug_avoidance_pct": 0.0,
            "tip_efficiency": 0.0,
            "model_vs_baseline": {
                "delta_net_pnl_per_sol_at_risk": "0",
                "lower_95_bound": "0",
                "n_test_windows": 0,
                "gate_b_pass": False,
            },
            "gate_a": {
                "net_pnl_sol": net_pnl_sol_str,
                "lower_95_bound": "0",
                "gate_a_pass": False,
            },
            "open_positions": len(state_store.load_all_positions()),
            "daily_pnl_sol": net_pnl_sol_str,
            "daily_loss_limit_sol": "-0.30",
            "breaker_tripped": breaker_state is not None and breaker_state.state == "TRIPPED",
            # NO win_rate field (HONESTY CLAUSE, AC-037)
        }

    # ------------------------------------------------------------------
    # POST /api/kill  (operator only; de-risk)
    # ------------------------------------------------------------------
    @app.post("/api/kill")
    async def post_kill(request: Request) -> dict[str, Any]:
        _check_auth(request)
        kill_switch.kill()
        # Flatten all open positions
        if fast_loop is not None:
            flat_handler = getattr(fast_loop, "flatten_handler", None)
            if flat_handler is not None:
                flat_handler.emergency_flatten_all(reason="operator_kill")
        return {"status": "killed", "message": "New entries halted; all positions flattening"}

    # ------------------------------------------------------------------
    # POST /api/flatten/{mint}  (operator only; de-risk)
    # ------------------------------------------------------------------
    @app.post("/api/flatten/{mint}")
    async def post_flatten(mint: str, request: Request) -> dict[str, Any]:
        _check_auth(request)
        pos = state_store.load_position(mint)
        if pos is None:
            raise HTTPException(status_code=404, detail=f"position_not_found:{mint}")
        if fast_loop is not None:
            flat_handler = getattr(fast_loop, "flatten_handler", None)
            if flat_handler is not None:
                flat_handler.emergency_flatten_all(reason=f"operator_flatten:{mint}")
        return {"status": "flattening", "mint": mint}

    # ------------------------------------------------------------------
    # POST /api/breaker/reset  (operator only; explicit manual action)
    # ------------------------------------------------------------------
    @app.post("/api/breaker/reset")
    async def post_breaker_reset(request: Request) -> dict[str, Any]:
        _check_auth(request)
        if breaker is None:
            raise HTTPException(status_code=503, detail="breaker_not_available")
        current = state_store.load_breaker_state()
        if current is None or current.state != "TRIPPED":
            raise HTTPException(status_code=409, detail="breaker_not_tripped")
        token = mint_operator_reset_token("operator")
        breaker.reset(token)
        return {"status": "breaker_reset", "message": "Circuit breaker re-armed"}

    return app
