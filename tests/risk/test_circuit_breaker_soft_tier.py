"""AUDIT-risktiers — SOFT daily-loss halt tier proof-by-execution.

The soft -2.0% (default) tier sits STRICTLY BELOW the hard -3.0% / -0.30 SOL
breaker.  These tests FIRE it and assert, by execution:

  * the soft tier triggers BEFORE the hard breaker (at a smaller loss);
  * crossing the soft tier PAUSES new entries (REDUCED) WITHOUT a hard
    flatten/latch (de-risk only — never increases risk, never flattens);
  * the posture is MONOTONE within a day (a same-day partial recovery does NOT
    lift the pause — lifting would re-increase risk);
  * the soft posture SURVIVES a process restart;
  * a hard trip implies the soft posture (REDUCED) too;
  * only the operator reset() lifts the soft pause;
  * a fresh UTC day starts NORMAL (the tranche resets);
  * the soft tier NEVER blocks the hard breaker from tripping;
  * money stays integer lamports / Decimal (NEVER float).

Run:  python -m pytest tests/risk/test_circuit_breaker_soft_tier.py -v
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from aats.contracts.risk import RiskConfig
from aats.risk.circuit_breaker import (
    DEFAULT_SOFT_RATIO,
    CircuitBreaker,
    InMemoryBreakerStore,
    LLMDeRiskSignal,
    PnLEvent,
    mint_operator_reset_token,
)

LAMPORTS_PER_SOL = 1_000_000_000

# Two fixed event-times in DIFFERENT UTC days.
_DAY1 = int(datetime(2026, 6, 16, 12, 0, 0, tzinfo=UTC).timestamp() * 1000)
_DAY2 = int(datetime(2026, 6, 17, 12, 0, 0, tzinfo=UTC).timestamp() * 1000)


class _SpyFlatten:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def emergency_flatten_all(self, reason: str) -> None:
        self.calls.append(reason)


def _make(config: RiskConfig | None = None, store: InMemoryBreakerStore | None = None):
    cfg = config or RiskConfig()
    store = store if store is not None else InMemoryBreakerStore()
    flatten = _SpyFlatten()
    return CircuitBreaker(cfg, store, flatten), store, flatten, cfg


# ---------------------------------------------------------------------------
# Threshold geometry: on the 0.5 SOL tranche, soft = -2.0% = -10M, hard = -15M.
# The soft tier is OWNED IN THE RISK MODULE (DEFAULT_SOFT_RATIO), not in contracts.
# ---------------------------------------------------------------------------
def test_soft_threshold_is_strictly_below_hard():
    cfg = RiskConfig()
    breaker, _, _, _ = _make(config=cfg)
    soft_pct = breaker._soft_limit_pct()  # derived: hard pct × DEFAULT_SOFT_RATIO
    assert soft_pct == Decimal("2.0")  # 3.0 × 0.6667 → 2.0001 → quantized DOWN to 2.0
    assert soft_pct < cfg.daily_loss_limit_pct  # strictly below the hard tier
    soft_lamports = int(cfg.daily_risk_tranche_lamports * soft_pct / 100)
    hard_lamports = int(cfg.daily_risk_tranche_lamports * cfg.daily_loss_limit_pct / 100)
    assert soft_lamports == 10_000_000  # -0.010 SOL
    assert hard_lamports == 15_000_000  # -0.015 SOL
    assert soft_lamports < hard_lamports


# ---------------------------------------------------------------------------
# 1. The soft tier triggers BEFORE the hard breaker, de-risk only, no flatten.
# ---------------------------------------------------------------------------
def test_soft_triggers_before_hard():
    breaker, store, flatten, _ = _make()
    assert breaker.entries_allowed() is True
    assert breaker.risk_posture() == "NORMAL"

    # Step 1: a loss of -8M is BELOW the soft tier (-10M) → still NORMAL.
    breaker.record_pnl(PnLEvent(mint="M", block_time_ms=_DAY1, delta_lamports=-8_000_000))
    assert breaker.risk_posture() == "NORMAL"
    assert breaker.entries_allowed() is True
    assert breaker.is_tripped() is False

    # Step 2: another -4M → cumulative -12M crosses the SOFT tier (-10M) but NOT
    # the hard tier (-15M).  Entries PAUSE; NO flatten; NOT a hard latch.
    breaker.record_pnl(PnLEvent(mint="M", block_time_ms=_DAY1 + 1, delta_lamports=-4_000_000))
    assert breaker.is_tripped() is False  # hard breaker has NOT tripped yet
    assert breaker.risk_posture() == "REDUCED"
    assert breaker.is_soft_reduced() is True
    assert breaker.entries_allowed() is False  # paused BEFORE the hard halt
    assert flatten.calls == []  # the soft tier NEVER flattens

    # Step 3: another -4M → cumulative -16M crosses the HARD tier → trip + flatten.
    breaker.record_pnl(PnLEvent(mint="M", block_time_ms=_DAY1 + 2, delta_lamports=-4_000_000))
    assert breaker.is_tripped() is True
    assert breaker.risk_posture() == "REDUCED"  # hard trip implies soft posture
    assert breaker.is_soft_reduced() is False  # no longer the soft-only band
    assert breaker.entries_allowed() is False
    assert len(flatten.calls) == 1  # flatten fired exactly once, on the hard edge


# ---------------------------------------------------------------------------
# 2. Soft pause is MONOTONE within a day (a recovery does NOT lift it).
# ---------------------------------------------------------------------------
def test_soft_pause_does_not_lift_on_same_day_recovery():
    breaker, _, flatten, _ = _make()
    # Cross the soft tier.
    breaker.record_pnl(PnLEvent(mint="M", block_time_ms=_DAY1, delta_lamports=-12_000_000))
    assert breaker.risk_posture() == "REDUCED"
    # A same-day GAIN brings the net back above the soft tier...
    breaker.record_pnl(PnLEvent(mint="M", block_time_ms=_DAY1 + 1, delta_lamports=+8_000_000))
    # ...but the pause STAYS (lifting it would re-increase risk — de-risk only).
    assert breaker.risk_posture() == "REDUCED"
    assert breaker.entries_allowed() is False
    assert flatten.calls == []  # still no flatten; soft tier never flattens


# ---------------------------------------------------------------------------
# 3. The soft posture SURVIVES a process restart.
# ---------------------------------------------------------------------------
def test_soft_posture_survives_restart():
    store = InMemoryBreakerStore()
    breaker, _, _, _ = _make(store=store)
    breaker.record_pnl(PnLEvent(mint="M", block_time_ms=_DAY1, delta_lamports=-12_000_000))
    assert breaker.risk_posture() == "REDUCED"

    # Reconstruct against the SAME store (== process restart).
    breaker2, _, _, _ = _make(store=store)
    assert breaker2.risk_posture() == "REDUCED"  # pause survived the restart
    assert breaker2.entries_allowed() is False
    assert breaker2.is_tripped() is False  # still a soft pause, not a hard latch

    # And a further same-day event keeps it REDUCED (re-seeded soft day).
    breaker2.record_pnl(PnLEvent(mint="M", block_time_ms=_DAY1 + 5, delta_lamports=+2_000_000))
    assert breaker2.risk_posture() == "REDUCED"


# ---------------------------------------------------------------------------
# 4. A fresh UTC day starts NORMAL (the tranche resets) — soft pause is per-day.
# ---------------------------------------------------------------------------
def test_fresh_utc_day_starts_normal():
    breaker, _, _, _ = _make()
    breaker.record_pnl(PnLEvent(mint="M", block_time_ms=_DAY1, delta_lamports=-12_000_000))
    assert breaker.risk_posture() == "REDUCED"
    # First event on the NEXT UTC day: a small loss below the soft tier → NORMAL.
    breaker.record_pnl(PnLEvent(mint="M", block_time_ms=_DAY2, delta_lamports=-5_000_000))
    assert breaker.risk_posture() == "NORMAL"
    assert breaker.entries_allowed() is True


# ---------------------------------------------------------------------------
# 5. Only the operator reset() lifts the soft pause.
# ---------------------------------------------------------------------------
def test_only_operator_reset_lifts_soft_pause_after_hard_trip():
    breaker, _, _, _ = _make()
    # Drive past the hard tier (which implies REDUCED).
    breaker.record_pnl(PnLEvent(mint="M", block_time_ms=_DAY1, delta_lamports=-20_000_000))
    assert breaker.is_tripped() is True
    assert breaker.risk_posture() == "REDUCED"
    # Operator reset clears BOTH the hard latch and the soft posture.
    breaker.reset(mint_operator_reset_token("ceo-1"))
    assert breaker.is_tripped() is False
    assert breaker.risk_posture() == "NORMAL"
    assert breaker.entries_allowed() is True


# ---------------------------------------------------------------------------
# 6. The soft tier NEVER blocks the hard breaker from tripping.
# ---------------------------------------------------------------------------
def test_soft_tier_never_blocks_hard_trip():
    breaker, _, flatten, _ = _make()
    # A single event straight past the hard tier: soft is crossed too, and the
    # hard trip STILL fires (the soft tier is not a gate on the hard halt).
    breaker.record_pnl(PnLEvent(mint="M", block_time_ms=_DAY1, delta_lamports=-300_000_000))
    assert breaker.is_tripped() is True
    assert len(flatten.calls) == 1


# ---------------------------------------------------------------------------
# 7. An LLM signal can TRIP (hard) → REDUCED; it can never lift the soft pause.
# ---------------------------------------------------------------------------
def test_llm_trip_implies_reduced_and_cannot_lift():
    breaker, _, _, _ = _make()
    breaker.record_pnl(PnLEvent(mint="M", block_time_ms=_DAY1, delta_lamports=-12_000_000))
    assert breaker.risk_posture() == "REDUCED"
    # The LLM de-risk path can only HARDEN (trip), never lift to NORMAL.
    breaker.trip_from_llm(LLMDeRiskSignal(reason="narrative abandoned"), block_time_ms=_DAY1 + 1)
    assert breaker.is_tripped() is True
    assert breaker.risk_posture() == "REDUCED"
    # There is NO LLM API that returns the posture to NORMAL.
    assert not hasattr(breaker, "lift_soft_pause")
    assert not hasattr(breaker, "set_posture")


# ---------------------------------------------------------------------------
# 8. Money discipline — the soft pct is Decimal, thresholds are integer lamports.
# ---------------------------------------------------------------------------
def test_soft_money_is_decimal_and_int():
    cfg = RiskConfig()
    assert isinstance(DEFAULT_SOFT_RATIO, Decimal)
    breaker, store, _, _ = _make(config=cfg)
    assert isinstance(breaker._soft_limit_pct(), Decimal)  # soft pct is Decimal
    state = breaker.record_pnl(PnLEvent(mint="M", block_time_ms=_DAY1, delta_lamports=-12_000_000))
    assert isinstance(state.daily_net_pnl_lamports, int)  # money stays integer lamports
    assert breaker.risk_posture() in ("NORMAL", "REDUCED")


# ---------------------------------------------------------------------------
# 9. The soft ratio can NEVER be configured at or above the hard tier.
#    (Owned in the risk module via the CircuitBreaker soft_ratio param — the
#     contracts package is NOT modified.)
# ---------------------------------------------------------------------------
def test_soft_ratio_cannot_reach_hard_tier():
    store = InMemoryBreakerStore()
    flatten = _SpyFlatten()
    cfg = RiskConfig()
    # A ratio >= 1 would put the soft tier at/above the hard halt — rejected.
    with pytest.raises(ValueError):
        CircuitBreaker(cfg, store, flatten, soft_ratio=Decimal("1.0"))
    with pytest.raises(ValueError):
        CircuitBreaker(cfg, store, flatten, soft_ratio=Decimal("1.5"))
    with pytest.raises(ValueError):
        CircuitBreaker(cfg, store, flatten, soft_ratio=Decimal("0"))
    # A tightened hard limit auto-keeps the soft strictly below it (no crossing):
    # the soft pct is derived from the hard pct × ratio, so it tracks down with it.
    tighter = RiskConfig(daily_loss_limit_pct=Decimal("1.0"))
    breaker, _, _, _ = _make(config=tighter)
    assert breaker._soft_limit_pct() < tighter.daily_loss_limit_pct
    assert breaker._soft_limit_pct() > 0


def test_custom_soft_ratio_overrides_default():
    # A per-breaker tighter ratio fires the soft pause earlier (de-risk).
    cfg = RiskConfig()
    breaker, _, _, _ = _make(config=cfg)
    tight, _, _, _ = (
        CircuitBreaker(cfg, InMemoryBreakerStore(), _SpyFlatten(), soft_ratio=Decimal("0.3")),
        None,
        None,
        None,
    )
    assert tight._soft_limit_pct() < breaker._soft_limit_pct()
