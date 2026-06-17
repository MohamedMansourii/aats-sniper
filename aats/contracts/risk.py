"""RiskConfig and BreakerState.

Source of truth: data-models.md §8.

TIGHTEN-ONLY semantics (api-contracts.md §5, OQ-001/005):
  - per_trade_cap_lamports: floor 100_000_000 lamports (0.1 SOL) — a ceiling
    on the per-trade size.  The API may only DECREASE this.
  - max_aggregate_lamports: floor 500_000_000 lamports (0.5 SOL).
  - daily_loss_limit_pct: may only become more negative (tighter); never wider
    than -3.0% of tranche.
  - Widening any limit requires an explicit R3 config change (file + deploy),
    NOT via this API.

Money fields (data-models.md §0):
  All *_lamports fields → int (integer)
  daily_loss_limit_pct, jito_tip_cap_frac, kelly_fraction_cap → Decimal
  daily_net_pnl_lamports → int (integer)
"""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, model_validator


class RiskConfig(BaseModel):
    """Current risk configuration — hardcoded FLOORS (OQ-005).

    The API may only TIGHTEN below these; never widen (api-contracts §5).
    Widening requires an explicit config-file change + deploy.

    All lamport fields are int; all fractional limits are Decimal.
    NO float money fields (data-models.md §0).
    """

    # Hardcoded FLOORS — API can only tighten
    per_trade_cap_lamports: int = 100_000_000  # 0.1 SOL ceiling
    max_aggregate_lamports: int = 500_000_000  # 0.5 SOL ceiling
    daily_risk_tranche_lamports: int = 500_000_000  # 0.5 SOL ceiling
    # tighten-only; never widen beyond -3.0% (OQ-001)
    daily_loss_limit_pct: Decimal = Decimal("3.0")
    # absolute -0.30 SOL floor, independent (OQ-001)
    daily_loss_floor_lamports: int = 300_000_000
    max_slippage_bps: int = 300  # reasonable default
    snipe_threshold: float = 0.65  # raising it is de-risk; lowering rejected by API
    veto_threshold: float = 0.30
    # 0.30 × edge cap (tips.py invariant)
    jito_tip_cap_frac: Decimal = Decimal("0.30")
    # hard 1/4 Kelly (FR-032)
    kelly_fraction_cap: Decimal = Decimal("0.25")
    # R3 (OQ-010); multi-wallet built but N_max=1 until R4
    n_wallets_max: int = 1
    blast_radius_cap_lamports: int = 100_000_000  # = per_trade_cap at R3
    # 60 default, ENV-CONFIGURABLE not constant (OQ-006)
    t_dms_seconds: int = 60
    # UNCALIBRATED floor (OQ-007); widen-only pre-R3
    pre_calibration_haircut_bps: int = 150

    model_config = {"frozen": True}

    @model_validator(mode="before")
    @classmethod
    def _reject_float_money(cls, data: object) -> object:
        """Reject float values for integer lamport fields (data-models.md §0)."""
        if not isinstance(data, dict):
            return data
        _INT_MONEY_FIELDS = (
            "per_trade_cap_lamports",
            "max_aggregate_lamports",
            "daily_risk_tranche_lamports",
            "daily_loss_floor_lamports",
            "blast_radius_cap_lamports",
        )
        for field in _INT_MONEY_FIELDS:
            val = data.get(field)
            if val is not None and isinstance(val, float):
                raise ValueError(
                    f"RiskConfig field '{field}' must be int (lamports), "
                    f"got float {val!r}.  Use integer base units (data-models.md §0)."
                )
        return data

    @model_validator(mode="after")
    def _floor_invariants(self) -> RiskConfig:
        """Assert that no floor has been violated at construction time."""
        if self.per_trade_cap_lamports > 100_000_000:
            raise ValueError(
                f"per_trade_cap_lamports ({self.per_trade_cap_lamports}) "
                "exceeds the hardcoded floor of 100_000_000 lamports (0.1 SOL, OQ-005).  "
                "This is a ceiling, not a floor — the value must be <= 100_000_000."
            )
        if self.max_aggregate_lamports > 500_000_000:
            raise ValueError(
                f"max_aggregate_lamports ({self.max_aggregate_lamports}) "
                "exceeds the hardcoded floor of 500_000_000 lamports (0.5 SOL, OQ-005)."
            )
        if self.daily_loss_limit_pct > Decimal("3.0"):
            raise ValueError(
                f"daily_loss_limit_pct ({self.daily_loss_limit_pct}) "
                "exceeds the 3.0% floor (OQ-001).  May only be tightened."
            )
        if self.kelly_fraction_cap > Decimal("0.25"):
            raise ValueError(
                f"kelly_fraction_cap ({self.kelly_fraction_cap}) "
                "exceeds the 0.25 (1/4 Kelly) hard cap (FR-032)."
            )
        if self.jito_tip_cap_frac > Decimal("0.30"):
            raise ValueError(
                f"jito_tip_cap_frac ({self.jito_tip_cap_frac}) "
                "exceeds the 0.30 × edge cap (FR-027, tips.py invariant)."
            )
        if self.pre_calibration_haircut_bps < 150:
            raise ValueError(
                f"pre_calibration_haircut_bps ({self.pre_calibration_haircut_bps}) "
                "is below the 150 bps UNCALIBRATED floor (OQ-007, C-11)."
            )
        return self


class BreakerState(BaseModel):
    """Daily-loss circuit breaker state.

    Source: data-models.md §8 (ADR-0012 added `daily_net_pnl_day_utc`).
    Re-arm ONLY via POST /api/breaker/reset with operator auth (AC-029).
    LLM may TRIP the breaker, NEVER reset it.
    On restart, state is restored from Redis — it NEVER auto-resets (AC-029).

    POINT-IN-TIME (C-5; ADR-0012, B1 fix): `daily_net_pnl_lamports` is the net
    for ONE specific event-time UTC day, and `daily_net_pnl_day_utc` records
    WHICH day that net belongs to ('YYYY-MM-DD' derived from event-time
    block_time_ms, UTC).  Persisting the day key is what lets a restart seed the
    net ONLY into the day it was accumulated in.  Without it, a restart that
    straddles UTC midnight would either spuriously trip (negative carry into a
    fresh day) or — far worse — MASK a hard halt (positive carry hiding a real
    next-day loss).  `None` only when the net is exactly 0 (no day established
    yet), e.g. first boot or post-reset.
    """

    state: Literal["ARMED", "TRIPPED"]
    tripped_at_utc: str | None  # ISO-8601, None when ARMED
    daily_net_pnl_lamports: int  # integer lamports (NOT float) — net for ONE event-time UTC day
    daily_net_pnl_day_utc: str | None  # 'YYYY-MM-DD' (event-time UTC) the net above belongs to
    threshold_breached: str | None  # e.g. "-3.0% tranche" or "-0.30 SOL"

    model_config = {"frozen": True}

    @model_validator(mode="before")
    @classmethod
    def _reject_float_pnl(cls, data: object) -> object:
        """Reject float value for daily_net_pnl_lamports (data-models.md §0)."""
        if not isinstance(data, dict):
            return data
        val = data.get("daily_net_pnl_lamports")
        if val is not None and isinstance(val, float):
            raise ValueError(
                f"BreakerState.daily_net_pnl_lamports must be int (lamports), "
                f"got float {val!r}.  Use integer base units (data-models.md §0)."
            )
        return data

    @model_validator(mode="after")
    def _tripped_invariant(self) -> BreakerState:
        """A TRIPPED breaker must have tripped_at_utc and threshold_breached."""
        if self.state == "TRIPPED":
            if self.tripped_at_utc is None:
                raise ValueError("BreakerState.tripped_at_utc must be set when state='TRIPPED'.")
            if self.threshold_breached is None:
                raise ValueError(
                    "BreakerState.threshold_breached must be set when state='TRIPPED'."
                )
        return self

    @model_validator(mode="after")
    def _day_key_invariant(self) -> BreakerState:
        """Point-in-time guard (B1; ADR-0012): a non-zero day-net MUST carry the
        event-time UTC day it belongs to, so a restart can seed it into the
        correct day and never into a fresh, unrelated UTC day.  A zero net has no
        established day (None permitted: first boot / post-reset)."""
        if self.daily_net_pnl_lamports != 0 and self.daily_net_pnl_day_utc is None:
            raise ValueError(
                "BreakerState.daily_net_pnl_day_utc must be set whenever "
                "daily_net_pnl_lamports != 0 (point-in-time restart seeding, ADR-0012/B1)."
            )
        return self
