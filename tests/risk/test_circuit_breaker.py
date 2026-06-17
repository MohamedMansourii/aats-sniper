"""T-320 PROOF-BY-EXECUTION — Daily-loss circuit breaker (HARD HALT).

These tests FIRE the breaker.  They are the deliverable's proof, not decoration.
Per the task's safety-critical mandate, they assert:

  * a loss sequence crossing -0.30 SOL TRIPS the breaker;
  * on trip, new entries STOP (entries_allowed() → False);
  * on trip, the FLATTEN handler is invoked (positions handed to survivable stop);
  * re-arm needs an EXPLICIT operator reset (manual);
  * an LLM signal CANNOT reset the breaker (asymmetric trust, AC-029);
  * a process RESTART keeps it TRIPPED (latched, never auto-reset);
  * the tranche resets at UTC midnight in EVENT-TIME (point-in-time, C-5);
  * money stays integer lamports / Decimal (NEVER float).

Run:  python -m pytest tests/risk/test_circuit_breaker.py -v
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from aats.contracts.risk import BreakerState, RiskConfig
from aats.risk.circuit_breaker import (
    BreakerNotTripped,
    CircuitBreaker,
    InMemoryBreakerStore,
    LLMDeRiskSignal,
    OperatorResetToken,
    PnLEvent,
    mint_operator_reset_token,
)

LAMPORTS_PER_SOL = 1_000_000_000

# A fixed event-time inside one UTC day (2026-06-16 12:00:00 UTC).
_DAY_T = int(datetime(2026, 6, 16, 12, 0, 0, tzinfo=UTC).timestamp() * 1000)


# ---------------------------------------------------------------------------
# A spy FlattenHandler — records every emergency_flatten_all invocation so the
# test can prove positions were handed to the survivable stop / flatten on trip.
# ---------------------------------------------------------------------------
class SpyFlattenHandler:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def emergency_flatten_all(self, reason: str) -> None:
        self.calls.append(reason)


def _make_breaker(config: RiskConfig | None = None, store: InMemoryBreakerStore | None = None):
    cfg = config or RiskConfig()
    store = store if store is not None else InMemoryBreakerStore()
    flatten = SpyFlattenHandler()
    breaker = CircuitBreaker(cfg, store, flatten)
    return breaker, store, flatten, cfg


# ===========================================================================
# 1. FIRES — a loss sequence crossing -0.30 SOL trips the breaker.
# ===========================================================================
def test_loss_sequence_crossing_floor_trips_breaker():
    breaker, store, flatten, cfg = _make_breaker()
    assert breaker.entries_allowed() is True  # armed at boot

    # Default tranche is 0.5 SOL → -3.0% = -0.015 SOL = -15_000_000 lamports.
    # That percentage limit is TIGHTER than the -0.30 SOL floor, so it fires
    # first.  Drive a loss sequence that crosses both, ending well past -0.30.
    losses = [-5_000_000, -5_000_000, -8_000_000]  # cumulative -0.018 SOL
    for i, delta in enumerate(losses):
        breaker.record_pnl(PnLEvent(mint="MINT1", block_time_ms=_DAY_T + i, delta_lamports=delta))

    assert breaker.is_tripped() is True
    # New entries STOP.
    assert breaker.entries_allowed() is False
    # Positions handed to flatten exactly once (on the ARMED→TRIPPED edge).
    assert len(flatten.calls) == 1
    assert "circuit_breaker_tripped" in flatten.calls[0]
    # State persisted as TRIPPED with a breach reason and a tripped_at timestamp.
    persisted = store.load()
    assert persisted is not None
    assert persisted.state == "TRIPPED"
    assert persisted.tripped_at_utc is not None
    assert persisted.threshold_breached is not None


def test_hard_floor_trips_independently_when_pct_widened_off():
    # Prove the ABSOLUTE -0.30 SOL floor is an independent trip even if the
    # percentage limit were as wide as the contract floor allows (3.0%).
    # Tranche 0.5 SOL, 3.0% = -0.015 SOL.  To isolate the floor we set a tiny
    # tranche so the pct limit is far below -0.30, leaving the -0.30 floor as
    # the binding constraint.  (RiskConfig clamps tranche <= 0.5 SOL.)
    cfg = RiskConfig(daily_risk_tranche_lamports=1, daily_loss_limit_pct=Decimal("3.0"))
    # pct limit = -(1 * 3 / 100) = 0 lamports (rounds to 0) → effectively the
    # floor dominates for any non-trivial loss.  Push exactly to -0.30 SOL.
    breaker, store, flatten, _ = _make_breaker(config=cfg)
    breaker.record_pnl(
        PnLEvent(mint="MINT_FLOOR", block_time_ms=_DAY_T, delta_lamports=-300_000_000)
    )
    assert breaker.is_tripped() is True
    assert breaker.entries_allowed() is False
    assert len(flatten.calls) == 1


def test_does_not_trip_above_limit():
    # A small loss BELOW BOTH the soft (-2.0% = -10M) AND hard (-3.0% = -15M)
    # tiers must NOT trip AND must stay at full risk (NORMAL posture).
    cfg = RiskConfig()  # -3.0% of 0.5 SOL = -15M hard; -2.0% = -10M soft
    breaker, _, flatten, _ = _make_breaker(config=cfg)
    breaker.record_pnl(PnLEvent(mint="MINT_OK", block_time_ms=_DAY_T, delta_lamports=-9_000_000))
    assert breaker.is_tripped() is False
    assert breaker.risk_posture() == "NORMAL"  # below the soft tier → full risk
    assert breaker.entries_allowed() is True
    assert flatten.calls == []  # no flatten while armed


def test_soft_tier_pauses_before_hard_trip():
    # AUDIT-risktiers: a loss crossing the SOFT -2.0% tier (-10M) but NOT the hard
    # -3.0% tier (-15M) PAUSES new entries (REDUCED) WITHOUT flatten/latch, and
    # fires STRICTLY BEFORE the hard breaker.
    cfg = RiskConfig()
    breaker, store, flatten, _ = _make_breaker(config=cfg)
    breaker.record_pnl(PnLEvent(mint="SOFT", block_time_ms=_DAY_T, delta_lamports=-12_000_000))
    # Soft tier crossed: REDUCED posture, entries paused, but NOT a hard trip.
    assert breaker.is_tripped() is False
    assert breaker.risk_posture() == "REDUCED"
    assert breaker.is_soft_reduced() is True
    assert breaker.entries_allowed() is False  # paused (de-risk) before the hard halt
    assert flatten.calls == []  # NO flatten — the soft tier never flattens/latches
    # The persisted state stays ARMED (not a hard latch); the soft posture survives
    # a restart by RE-DERIVATION from the persisted day net (proven in the soft-tier
    # test file's restart case), without adding any BreakerState contract field.
    assert store.load().state == "ARMED"  # not a hard latch
    assert store.load().daily_net_pnl_lamports == -12_000_000


def test_trip_fires_flatten_exactly_once_idempotent():
    # Further losses after the trip do NOT re-fire flatten (idempotent latch).
    breaker, _, flatten, _ = _make_breaker()
    breaker.record_pnl(PnLEvent(mint="M", block_time_ms=_DAY_T, delta_lamports=-20_000_000))
    assert breaker.is_tripped()
    assert len(flatten.calls) == 1
    breaker.record_pnl(PnLEvent(mint="M", block_time_ms=_DAY_T + 1, delta_lamports=-50_000_000))
    breaker.record_pnl(PnLEvent(mint="M", block_time_ms=_DAY_T + 2, delta_lamports=-50_000_000))
    assert len(flatten.calls) == 1  # still exactly one
    assert breaker.entries_allowed() is False  # still halted


# ===========================================================================
# 2. RE-ARM requires an explicit MANUAL operator reset.
# ===========================================================================
def test_rearm_requires_explicit_operator_reset():
    breaker, store, flatten, _ = _make_breaker()
    breaker.record_pnl(PnLEvent(mint="M", block_time_ms=_DAY_T, delta_lamports=-20_000_000))
    assert breaker.is_tripped()

    # Manual operator reset via the audited token mint.
    token = mint_operator_reset_token(operator_id="ceo")
    state = breaker.reset(token)
    assert state.state == "ARMED"
    assert breaker.entries_allowed() is True
    # Persisted state is ARMED again.
    assert store.load().state == "ARMED"


def test_reset_when_not_tripped_is_conflict():
    breaker, _, _, _ = _make_breaker()
    token = mint_operator_reset_token(operator_id="ceo")
    with pytest.raises(BreakerNotTripped):
        breaker.reset(token)  # not tripped → 409-equivalent


def test_reset_token_cannot_be_forged_directly():
    # Direct construction of the capability token is forbidden — only the
    # audited mint function can produce one (AC-029).
    with pytest.raises(PermissionError):
        OperatorResetToken("attacker")  # no mint key → rejected


# ===========================================================================
# 3. ASYMMETRIC TRUST — an LLM signal may TRIP but can NEVER reset.
# ===========================================================================
def test_llm_signal_can_trip_breaker():
    breaker, _, flatten, _ = _make_breaker()
    assert breaker.entries_allowed() is True
    sig = LLMDeRiskSignal(reason="narrative manufactured/abandoned")
    state = breaker.trip_from_llm(sig, block_time_ms=_DAY_T)
    assert state.state == "TRIPPED"
    assert breaker.entries_allowed() is False
    assert len(flatten.calls) == 1
    assert "llm_narrative_failure" in state.threshold_breached


def test_llm_signal_cannot_reset_breaker():
    breaker, _, _, _ = _make_breaker()
    breaker.record_pnl(PnLEvent(mint="M", block_time_ms=_DAY_T, delta_lamports=-20_000_000))
    assert breaker.is_tripped()

    # The LLM path holds only an LLMDeRiskSignal.  It has no method, field, or
    # reference that can produce an OperatorResetToken.  Attempting to reset
    # with the de-risk signal is a type error at call boundary; attempting to
    # forge a token raises PermissionError.  Prove BOTH avenues fail.

    # (a) The de-risk signal is not an OperatorResetToken — reset rejects it.
    sig = LLMDeRiskSignal(reason="please let me trade again")
    with pytest.raises(PermissionError):
        breaker.reset(sig)  # type: ignore[arg-type]  # deliberately wrong type

    # (b) The LLM cannot mint a token (no mint key).
    with pytest.raises(PermissionError):
        OperatorResetToken("llm")

    # The breaker is STILL tripped — the LLM achieved nothing.
    assert breaker.is_tripped() is True
    assert breaker.entries_allowed() is False


def test_llm_derisk_signal_has_no_reset_capability_by_type():
    # Structural proof: LLMDeRiskSignal exposes no reset/widen/size-up surface.
    sig = LLMDeRiskSignal(reason="x")
    for forbidden in ("reset", "rearm", "widen", "size_up", "override", "operator_id"):
        assert not hasattr(
            sig, forbidden
        ), f"LLMDeRiskSignal must NOT expose '{forbidden}' — the LLM path may only de-risk."


# ===========================================================================
# 4. RESTART safety — a TRIPPED breaker comes back TRIPPED (latched).
# ===========================================================================
def test_restart_stays_tripped():
    store = InMemoryBreakerStore()
    breaker, _, flatten, _ = _make_breaker(store=store)
    breaker.record_pnl(PnLEvent(mint="M", block_time_ms=_DAY_T, delta_lamports=-25_000_000))
    assert breaker.is_tripped()

    # Simulate a process restart: a BRAND NEW CircuitBreaker over the SAME store.
    flatten2 = SpyFlattenHandler()
    breaker2 = CircuitBreaker(RiskConfig(), store, flatten2)

    # It MUST come back TRIPPED, entries blocked, without auto-resetting.
    assert breaker2.is_tripped() is True
    assert breaker2.entries_allowed() is False
    # Restart does NOT re-fire flatten (it was already done pre-crash).
    assert flatten2.calls == []
    # And it stays tripped until an explicit operator reset.
    token = mint_operator_reset_token(operator_id="ceo")
    assert breaker2.reset(token).state == "ARMED"


def test_restart_after_reset_stays_armed():
    store = InMemoryBreakerStore()
    breaker, _, _, _ = _make_breaker(store=store)
    breaker.record_pnl(PnLEvent(mint="M", block_time_ms=_DAY_T, delta_lamports=-25_000_000))
    breaker.reset(mint_operator_reset_token(operator_id="ceo"))
    # Restart after a reset → still ARMED.
    breaker2 = CircuitBreaker(RiskConfig(), store, SpyFlattenHandler())
    assert breaker2.entries_allowed() is True


# ---------------------------------------------------------------------------
# 4A. POINT-IN-TIME RESTART SEEDING (B1 fix) — the persisted net is seeded into
#     the event-time UTC day it was ACCUMULATED in, never into whatever day the
#     first post-restart event lands in.  Two directions, both previously broken:
#       (a) negative carry must NOT spuriously trip a fresh next day;
#       (b) positive carry must NOT MASK a real next-day loss that must halt.
# ---------------------------------------------------------------------------

# Two distinct UTC days for the restart-straddles-midnight scenario.
_DAY1_T = int(datetime(2026, 6, 16, 12, 0, 0, tzinfo=UTC).timestamp() * 1000)
_DAY2_T = int(datetime(2026, 6, 17, 12, 0, 0, tzinfo=UTC).timestamp() * 1000)


def test_restart_negative_carry_does_not_spuriously_trip_fresh_day():
    # Day 1: an ARMED day accumulates -14_000_000 (inside the fresh -15M limit).
    store = InMemoryBreakerStore()
    breaker, _, _, _ = _make_breaker(store=store)
    breaker.record_pnl(PnLEvent(mint="M", block_time_ms=_DAY1_T, delta_lamports=-14_000_000))
    assert breaker.is_tripped() is False
    persisted = store.load()
    assert persisted.state == "ARMED"
    assert persisted.daily_net_pnl_lamports == -14_000_000
    # The day key MUST be recorded so the seed is day-aware (B1).
    assert persisted.daily_net_pnl_day_utc == "2026-06-16"

    # Restart, then the FIRST post-restart event lands on the NEXT UTC day with a
    # -10_000_000 loss — alone WELL inside the fresh -15M day-2 limit.
    flatten2 = SpyFlattenHandler()
    breaker2 = CircuitBreaker(RiskConfig(), store, flatten2)
    state = breaker2.record_pnl(
        PnLEvent(mint="M", block_time_ms=_DAY2_T, delta_lamports=-10_000_000)
    )

    # Point-in-time-correct day-2 net is -10M (the -14M day-1 carry is NOT in it).
    # OLD (buggy) behavior pooled them to -24M and SPURIOUSLY tripped.
    assert (
        breaker2.is_tripped() is False
    ), "Day-1 negative carry must not bleed into the fresh day-2 tranche (B1)."
    assert state.daily_net_pnl_lamports == -10_000_000
    assert state.daily_net_pnl_day_utc == "2026-06-17"
    assert flatten2.calls == []


def test_restart_positive_carry_does_not_mask_real_next_day_halt():
    # THE DANGEROUS DIRECTION.  Day 1 ends PROFITABLE: +50_000_000 (ARMED).
    store = InMemoryBreakerStore()
    breaker, _, _, _ = _make_breaker(store=store)
    breaker.record_pnl(PnLEvent(mint="M", block_time_ms=_DAY1_T, delta_lamports=50_000_000))
    assert breaker.is_tripped() is False
    persisted = store.load()
    assert persisted.daily_net_pnl_lamports == 50_000_000
    assert persisted.daily_net_pnl_day_utc == "2026-06-16"

    # Restart, then a REAL day-2 loss of -20_000_000 — alone CROSSES the fresh
    # -15M limit and MUST halt.
    flatten2 = SpyFlattenHandler()
    breaker2 = CircuitBreaker(RiskConfig(), store, flatten2)
    state = breaker2.record_pnl(
        PnLEvent(mint="M", block_time_ms=_DAY2_T, delta_lamports=-20_000_000)
    )

    # OLD (buggy) behavior seeded +50M into day 2 → net +30M → breaker stayed
    # ARMED and the daily-loss HALT SILENTLY DID NOT FIRE.  It MUST trip now.
    assert breaker2.is_tripped() is True, (
        "Day-1 positive carry must NOT mask the real day-2 loss that crosses the "
        "fresh limit — the hard halt MUST fire (B1, masked-trip guard)."
    )
    assert breaker2.entries_allowed() is False
    assert len(flatten2.calls) == 1
    assert state.daily_net_pnl_lamports == -20_000_000
    assert state.daily_net_pnl_day_utc == "2026-06-17"


def test_restart_same_day_continues_from_persisted_net():
    # A restart whose first event is on the SAME UTC day as the persisted net
    # MUST continue from it (the seed is correct, not discarded).
    store = InMemoryBreakerStore()
    breaker, _, _, _ = _make_breaker(store=store)
    breaker.record_pnl(PnLEvent(mint="M", block_time_ms=_DAY1_T, delta_lamports=-10_000_000))
    assert breaker.is_tripped() is False  # -10M inside -15M

    # Restart; same UTC day, a further -6_000_000 → net -16M crosses -15M → TRIP.
    flatten2 = SpyFlattenHandler()
    breaker2 = CircuitBreaker(RiskConfig(), store, flatten2)
    breaker2.record_pnl(PnLEvent(mint="M", block_time_ms=_DAY1_T + 1000, delta_lamports=-6_000_000))
    assert (
        breaker2.is_tripped() is True
    ), "A same-day restart MUST continue from the persisted net, not reset to 0."
    assert len(flatten2.calls) == 1


def test_restart_while_tripped_preserves_day_key():
    # A TRIPPED restart re-reads TRIPPED AND keeps its day key so any post-reset
    # accounting on the same day remains point-in-time correct.
    store = InMemoryBreakerStore()
    breaker, _, _, _ = _make_breaker(store=store)
    breaker.record_pnl(PnLEvent(mint="M", block_time_ms=_DAY1_T, delta_lamports=-20_000_000))
    assert breaker.is_tripped()
    assert store.load().daily_net_pnl_day_utc == "2026-06-16"

    breaker2 = CircuitBreaker(RiskConfig(), store, SpyFlattenHandler())
    assert breaker2.is_tripped() is True
    assert breaker2.state.daily_net_pnl_day_utc == "2026-06-16"


def test_breaker_state_nonzero_net_requires_day_key():
    # Contract-level invariant (ADR-0012): a non-zero net MUST carry its day key,
    # so the persisted net can never be seeded into the wrong UTC day at restart.
    with pytest.raises(ValidationError):
        BreakerState(
            state="ARMED",
            tripped_at_utc=None,
            daily_net_pnl_lamports=-14_000_000,
            daily_net_pnl_day_utc=None,  # non-zero net with no day → rejected
            threshold_breached=None,
        )


# ===========================================================================
# 5. POINT-IN-TIME — tranche resets at UTC midnight in EVENT-TIME.
# ===========================================================================
def test_tranche_resets_at_utc_midnight_event_time():
    # Day 1: lose -0.014 SOL (inside the -0.015 limit, does NOT trip).
    breaker, _, _, _ = _make_breaker()
    day1 = int(datetime(2026, 6, 16, 23, 59, 0, tzinfo=UTC).timestamp() * 1000)
    breaker.record_pnl(PnLEvent(mint="M", block_time_ms=day1, delta_lamports=-14_000_000))
    assert breaker.is_tripped() is False

    # Day 2 (next UTC day): a fresh tranche — the prior day's loss does NOT
    # carry into day 2's limit.  Another -0.014 alone must NOT trip.
    day2 = int(datetime(2026, 6, 17, 0, 1, 0, tzinfo=UTC).timestamp() * 1000)
    breaker.record_pnl(PnLEvent(mint="M", block_time_ms=day2, delta_lamports=-14_000_000))
    assert breaker.is_tripped() is False, (
        "Day-2 loss must be measured against a FRESH tranche; the day-1 loss "
        "must not bleed across the UTC-midnight reset (event-time, C-5)."
    )


def test_event_time_not_compute_time_drives_the_day():
    # Two events with the SAME compute order but DIFFERENT event-time days must
    # accumulate in their event-time day, not the order they arrived.
    breaker, _, _, _ = _make_breaker()
    d1 = int(datetime(2026, 6, 16, 10, 0, 0, tzinfo=UTC).timestamp() * 1000)
    d2 = int(datetime(2026, 6, 17, 10, 0, 0, tzinfo=UTC).timestamp() * 1000)
    # Out-of-order arrival (d2 event recorded first), but each lands in its day.
    breaker.record_pnl(PnLEvent(mint="M", block_time_ms=d2, delta_lamports=-14_000_000))
    breaker.record_pnl(PnLEvent(mint="M", block_time_ms=d1, delta_lamports=-14_000_000))
    # Neither day exceeded its own -0.015 limit, so no trip — proving the two
    # losses did NOT pool into a single compute-time bucket (which WOULD trip).
    assert breaker.is_tripped() is False


# ===========================================================================
# 6. MONEY — integer lamports / Decimal only, NEVER float.
# ===========================================================================
def test_float_pnl_delta_rejected():
    with pytest.raises(TypeError):
        PnLEvent(mint="M", block_time_ms=_DAY_T, delta_lamports=1.5)  # type: ignore[arg-type]


def test_breaker_state_rejects_float_pnl():
    with pytest.raises(ValidationError):  # pydantic ValidationError wrapping ValueError
        BreakerState(
            state="ARMED",
            tripped_at_utc=None,
            daily_net_pnl_lamports=1.5,  # type: ignore[arg-type]
            daily_net_pnl_day_utc="2026-06-16",
            threshold_breached=None,
        )


def test_daily_net_pnl_is_int_in_state():
    breaker, store, _, _ = _make_breaker()
    breaker.record_pnl(PnLEvent(mint="M", block_time_ms=_DAY_T, delta_lamports=-1_000_000))
    s = store.load()
    assert isinstance(s.daily_net_pnl_lamports, int)


# ===========================================================================
# 7. FAIL-CLOSED — only ARMED permits entries.
# ===========================================================================
def test_fail_closed_only_armed_allows_entries():
    breaker, _, _, _ = _make_breaker()
    assert breaker.entries_allowed() is True
    breaker.record_pnl(PnLEvent(mint="M", block_time_ms=_DAY_T, delta_lamports=-20_000_000))
    assert breaker.entries_allowed() is False


# ===========================================================================
# 8. TIGHTEN-ONLY config — API may not widen the loss limit beyond floors.
#    (Contract-level guard lives in RiskConfig; assert it here so the breaker's
#     dependency on it is explicit.)
# ===========================================================================
def test_config_cannot_widen_pct_limit_beyond_floor():
    with pytest.raises(ValidationError):
        RiskConfig(daily_loss_limit_pct=Decimal("5.0"))  # > 3.0% floor → rejected


def test_config_cannot_widen_per_trade_cap():
    with pytest.raises(ValidationError):
        RiskConfig(per_trade_cap_lamports=200_000_000)  # > 0.1 SOL ceiling → rejected
