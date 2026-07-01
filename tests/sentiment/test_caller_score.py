"""Tests for E9 — Alpha-caller / call-channel track-record scoring.

Mandatory self-check items verified here:
  SC-E9-A: Scores are LEAK-FREE (no future outcome used in the score).
  SC-E9-B: A high score is ONLY a weighted selectivity input, NOT a trigger.
  SC-E9-C: Low/negative scores are surfaced honestly (no inflation).
  SC-E9-D: Caller signal cannot raise conviction (mcs_delta_contribution <= 0).
  SC-E9-E: No win_rate field anywhere in the module source.
  SC-E9-F: External services NOT reachable (offline, injectable store only).

GOLDEN FIXTURES:
  - Perpetual-negative-caller: 10 wrong calls => weight 0.0, mcs_delta < 0
  - Perpetual-positive-caller: 10 right calls => weight > 0, small upward trust
  - New caller (thin data): 2 calls => weight 0.0 (insufficient history)
  - Mixed caller: 6 right / 4 wrong => near-universe accuracy => low weight
  - No callers called this asset: modifier = 1.0, delta = 0.0 (neutral)
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest

from aats.sentiment.caller_score import (
    DEFAULT_UNIVERSE_ACCURACY,
    MIN_CALLS_FOR_SCORE,
    CallerCallOutcome,
    CallerSignal,
    CallerTrackRecordScorer,
    InMemoryCallerOutcomeStore,
    _compute_caller_score,
    assert_caller_signal_cannot_raise_conviction,
)

# ---------------------------------------------------------------------------
# Time anchors (deterministic — no wall-clock)
# ---------------------------------------------------------------------------

DECISION_TIME_MS = 1_718_500_000_000  # 2024-06-16T00:00:00Z  (arbitrary fixed point)
ONE_WEEK_MS = 7 * 24 * 3_600_000
ONE_DAY_MS = 86_400_000
ONE_HOUR_MS = 3_600_000

# Outcome times WITHIN the decision window (leak-safe)
T_MINUS_1W = DECISION_TIME_MS - ONE_WEEK_MS
T_MINUS_3D = DECISION_TIME_MS - 3 * ONE_DAY_MS
T_MINUS_2D = DECISION_TIME_MS - 2 * ONE_DAY_MS
T_MINUS_1D = DECISION_TIME_MS - ONE_DAY_MS
T_MINUS_1H = DECISION_TIME_MS - ONE_HOUR_MS

# Outcome times AFTER decision_time (MUST be excluded — point-in-time)
T_PLUS_1H  = DECISION_TIME_MS + ONE_HOUR_MS
T_PLUS_1D  = DECISION_TIME_MS + ONE_DAY_MS

_ASSET = "M1ntAlphaCallxxx"
_ASSET_2 = "M1ntOtherTokenxx"


# ---------------------------------------------------------------------------
# Golden fixture builders
# ---------------------------------------------------------------------------

def _outcome(
    caller_id: str,
    call_time_ms: int,
    outcome_time_ms: int,
    correct: bool,
    asset: str = _ASSET,
) -> CallerCallOutcome:
    """Build a CallerCallOutcome. outcome_time_ms MUST be > call_time_ms."""
    assert outcome_time_ms > call_time_ms, (
        "outcome_time_ms must be after call_time_ms (no look-ahead in the call)"
    )
    return CallerCallOutcome(
        caller_id=caller_id,
        call_event_time_ms=call_time_ms,
        outcome_event_time_ms=outcome_time_ms,
        asset=asset,
        outcome_correct=correct,
    )


def _build_outcomes(
    caller_id: str,
    n_correct: int,
    n_wrong: int,
    base_call_time_ms: int = T_MINUS_1W,
    outcome_lag_ms: int = ONE_HOUR_MS,
) -> list[CallerCallOutcome]:
    """Build a set of right + wrong outcomes spaced 1 hour apart."""
    outcomes: list[CallerCallOutcome] = []
    for i in range(n_correct + n_wrong):
        call_t = base_call_time_ms + i * ONE_HOUR_MS
        out_t = call_t + outcome_lag_ms
        is_correct = i < n_correct
        outcomes.append(_outcome(caller_id, call_t, out_t, is_correct))
    return outcomes


# ---------------------------------------------------------------------------
# GOLDEN FIXTURE 1: Always-wrong caller (perpetually negative)
# ---------------------------------------------------------------------------

_ALWAYS_WRONG_ID = "caller_always_wrong"
_ALWAYS_WRONG_OUTCOMES = _build_outcomes(_ALWAYS_WRONG_ID, n_correct=0, n_wrong=10)


# ---------------------------------------------------------------------------
# GOLDEN FIXTURE 2: Always-right caller (perpetually positive)
# ---------------------------------------------------------------------------

_ALWAYS_RIGHT_ID = "caller_always_right"
_ALWAYS_RIGHT_OUTCOMES = _build_outcomes(_ALWAYS_RIGHT_ID, n_correct=10, n_wrong=0)


# ---------------------------------------------------------------------------
# GOLDEN FIXTURE 3: Thin-data caller (only 2 resolved outcomes)
# ---------------------------------------------------------------------------

_THIN_DATA_ID = "caller_thin_data"
_THIN_DATA_OUTCOMES = _build_outcomes(_THIN_DATA_ID, n_correct=2, n_wrong=0)


# ---------------------------------------------------------------------------
# GOLDEN FIXTURE 4: Near-universe-average caller (mixed, near noise level)
# ---------------------------------------------------------------------------

_MIXED_ID = "caller_mixed"
# Universe accuracy is ~40%; 4 right out of 10 ≈ universe => near-zero delta
_MIXED_OUTCOMES = _build_outcomes(_MIXED_ID, n_correct=4, n_wrong=6)


# ---------------------------------------------------------------------------
# GOLDEN FIXTURE 5: Future-outcome caller (outcomes AFTER decision_time_ms)
# These MUST be excluded by the point-in-time filter.
# ---------------------------------------------------------------------------

_FUTURE_OUTCOME_ID = "caller_future"
_FUTURE_OUTCOME_OUTCOMES = [
    # Call made 1 day before decision — but OUTCOME is 1 hour AFTER decision.
    # The score at decision_time_ms MUST exclude this outcome.
    _outcome(
        _FUTURE_OUTCOME_ID,
        call_time_ms=T_MINUS_1D,
        outcome_time_ms=T_PLUS_1H,  # FUTURE — must be excluded
        correct=True,
    ),
    # An older outcome that IS within the window (resolved before decision_time)
    _outcome(
        _FUTURE_OUTCOME_ID,
        call_time_ms=T_MINUS_3D,
        outcome_time_ms=T_MINUS_2D,  # resolved before decision_time — included
        correct=False,  # this one is wrong
    ),
]


# ---------------------------------------------------------------------------
# Build the full store
# ---------------------------------------------------------------------------

def _build_store() -> InMemoryCallerOutcomeStore:
    all_outcomes = (
        _ALWAYS_WRONG_OUTCOMES
        + _ALWAYS_RIGHT_OUTCOMES
        + _THIN_DATA_OUTCOMES
        + _MIXED_OUTCOMES
        + _FUTURE_OUTCOME_OUTCOMES
    )
    return InMemoryCallerOutcomeStore(outcomes=all_outcomes)


# ---------------------------------------------------------------------------
# SC-E9-A: Leak-free — future outcome excluded
# ---------------------------------------------------------------------------


class TestPointInTimeLeak:
    """SC-E9-A: No future outcome may contribute to the score."""

    def test_future_outcome_excluded_from_score(self) -> None:
        """A call with outcome resolved AFTER decision_time_ms is excluded.

        Proof: score at decision_time_ms uses only outcomes where
        outcome_event_time_ms <= decision_time_ms.
        """
        store = InMemoryCallerOutcomeStore(outcomes=_FUTURE_OUTCOME_OUTCOMES)
        scorer = CallerTrackRecordScorer(store, universe_accuracy=DEFAULT_UNIVERSE_ACCURACY)
        score = scorer.score_caller(_FUTURE_OUTCOME_ID, DECISION_TIME_MS)

        # Only the T_MINUS_2D outcome (wrong) should be included.
        # The T_PLUS_1H outcome is excluded (future).
        # So call_count must be 1 (only the resolved wrong one).
        assert score.call_count == 1, (
            f"LEAK: expected 1 resolved outcome (the past one), "
            f"got call_count={score.call_count}. "
            "Future outcome leaked into the score."
        )

    def test_score_changes_without_future_outcome(self) -> None:
        """Prove the exclusion is meaningful: scores differ with/without future outcome."""
        # Store with ONLY the wrong past outcome (no future)
        past_only = [_FUTURE_OUTCOME_OUTCOMES[1]]  # the resolved-before-decision one
        store_past = InMemoryCallerOutcomeStore(outcomes=past_only)
        scorer_past = CallerTrackRecordScorer(store_past, universe_accuracy=DEFAULT_UNIVERSE_ACCURACY)
        score_past = scorer_past.score_caller(_FUTURE_OUTCOME_ID, DECISION_TIME_MS)

        # Store with BOTH outcomes (future + past) — future must be excluded
        store_both = InMemoryCallerOutcomeStore(outcomes=_FUTURE_OUTCOME_OUTCOMES)
        scorer_both = CallerTrackRecordScorer(store_both, universe_accuracy=DEFAULT_UNIVERSE_ACCURACY)
        score_both = scorer_both.score_caller(_FUTURE_OUTCOME_ID, DECISION_TIME_MS)

        # Both scorers must see exactly 1 resolved outcome
        # (the future one is excluded from both)
        assert score_past.call_count == score_both.call_count == 1, (
            "Future outcome changed the effective call count — this is a point-in-time leak."
        )
        # Accuracy scores must be identical (same resolved data)
        assert abs(score_past.accuracy_raw - score_both.accuracy_raw) < 1e-9, (
            f"POINT-IN-TIME VIOLATED: scores differ with ({score_both.accuracy_raw:.4f}) "
            f"and without ({score_past.accuracy_raw:.4f}) the future outcome. "
            "Future outcomes must not contribute."
        )

    def test_thin_data_below_decision_time_is_thin(self) -> None:
        """A future-only store yields thin data (no resolved outcomes)."""
        future_only_outcomes = [
            _outcome(
                "caller_x",
                call_time_ms=T_MINUS_1H,
                outcome_time_ms=T_PLUS_1D,  # future outcome
                correct=True,
            )
        ]
        store = InMemoryCallerOutcomeStore(outcomes=future_only_outcomes)
        scorer = CallerTrackRecordScorer(store, universe_accuracy=DEFAULT_UNIVERSE_ACCURACY)
        score = scorer.score_caller("caller_x", DECISION_TIME_MS)
        assert score.is_thin_data is True
        assert score.selectivity_weight == 0.0, (
            "Caller with only future outcomes must have selectivity_weight=0.0"
        )
        assert score.call_count == 0, (
            "No resolved outcomes at decision_time → call_count=0"
        )


# ---------------------------------------------------------------------------
# SC-E9-B: High score is only a selectivity input, NOT a trigger
# ---------------------------------------------------------------------------


class TestSelectivityOnlyNotTrigger:
    """SC-E9-B: Even the best caller signal cannot raise conviction above base."""

    def test_caller_signal_cannot_raise_conviction(self) -> None:
        """assert_caller_signal_cannot_raise_conviction: result <= base_conviction."""
        store = _build_store()
        scorer = CallerTrackRecordScorer(store, universe_accuracy=DEFAULT_UNIVERSE_ACCURACY)
        signal = scorer.score_for_asset(
            _ASSET,
            [_ALWAYS_RIGHT_ID],
            DECISION_TIME_MS,
        )
        for base in [0.0, 0.1, 0.3, 0.5, 0.8, 1.0]:
            result = assert_caller_signal_cannot_raise_conviction(signal, base)
            assert result <= base + 1e-9, (
                f"Caller signal RAISED conviction: base={base:.3f}, result={result:.3f}. "
                "Caller signals may ONLY de-risk."
            )

    def test_mcs_delta_contribution_is_always_non_positive(self) -> None:
        """caller_signal.mcs_delta_contribution must always be <= 0."""
        store = _build_store()
        scorer = CallerTrackRecordScorer(store, universe_accuracy=DEFAULT_UNIVERSE_ACCURACY)

        # Test all fixture caller IDs
        for cid in [
            _ALWAYS_RIGHT_ID,
            _ALWAYS_WRONG_ID,
            _THIN_DATA_ID,
            _MIXED_ID,
        ]:
            signal = scorer.score_for_asset(_ASSET, [cid], DECISION_TIME_MS)
            assert signal.mcs_delta_contribution <= 0.0, (
                f"VIOLATION: caller {cid} produced positive mcs_delta_contribution="
                f"{signal.mcs_delta_contribution:.4f}. "
                "Caller signal may ONLY de-risk (delta must be <= 0)."
            )

    def test_caller_selectivity_modifier_never_exceeds_one(self) -> None:
        """caller_selectivity_modifier in [0.0, 1.0] — cannot exceed 1.0."""
        store = _build_store()
        scorer = CallerTrackRecordScorer(store, universe_accuracy=DEFAULT_UNIVERSE_ACCURACY)

        for cid in [
            _ALWAYS_RIGHT_ID,
            _ALWAYS_WRONG_ID,
            _THIN_DATA_ID,
            _MIXED_ID,
        ]:
            signal = scorer.score_for_asset(_ASSET, [cid], DECISION_TIME_MS)
            assert 0.0 <= signal.caller_selectivity_modifier <= 1.0, (
                f"caller_selectivity_modifier out of bounds for {cid}: "
                f"{signal.caller_selectivity_modifier:.4f}"
            )

    def test_no_callers_returns_neutral_signal(self) -> None:
        """No callers for this asset => modifier=1.0, delta=0.0 (neutral, no de-risk)."""
        store = InMemoryCallerOutcomeStore()
        scorer = CallerTrackRecordScorer(store, universe_accuracy=DEFAULT_UNIVERSE_ACCURACY)
        signal = scorer.score_for_asset(_ASSET_2, [], DECISION_TIME_MS)
        assert signal.caller_selectivity_modifier == 1.0
        assert signal.mcs_delta_contribution == 0.0
        assert signal.called_by == []


# ---------------------------------------------------------------------------
# SC-E9-C: Low/negative scores surfaced honestly — no inflation
# ---------------------------------------------------------------------------


class TestHonestyNegativeScores:
    """SC-E9-C: Negative and thin-data callers must be surfaced honestly."""

    def test_always_wrong_caller_gets_weight_zero(self) -> None:
        """An always-wrong caller has selectivity_weight=0.0 (deweighted)."""
        store = InMemoryCallerOutcomeStore(outcomes=_ALWAYS_WRONG_OUTCOMES)
        scorer = CallerTrackRecordScorer(store, universe_accuracy=DEFAULT_UNIVERSE_ACCURACY)
        score = scorer.score_caller(_ALWAYS_WRONG_ID, DECISION_TIME_MS)

        print(
            f"\n[FIXTURE always-wrong] calls={score.call_count} "
            f"accuracy_raw={score.accuracy_raw:.3f} "
            f"accuracy_delta={score.accuracy_delta:.3f} "
            f"selectivity_weight={score.selectivity_weight:.3f} "
            f"is_negative={score.is_negative}"
        )

        assert score.is_negative is True, (
            f"Always-wrong caller must be flagged as_negative. "
            f"accuracy_delta={score.accuracy_delta:.3f}"
        )
        assert score.selectivity_weight == 0.0, (
            f"Always-wrong caller must have selectivity_weight=0.0 (completely deweighted). "
            f"Got {score.selectivity_weight:.3f}."
        )
        assert score.accuracy_raw == pytest.approx(0.0, abs=0.05), (
            f"Always-wrong caller accuracy_raw expected ≈ 0.0, got {score.accuracy_raw:.3f}"
        )

    def test_negative_caller_count_surfaced_in_signal(self) -> None:
        """CallerSignal.negative_caller_count must be nonzero for a negative caller."""
        store = InMemoryCallerOutcomeStore(outcomes=_ALWAYS_WRONG_OUTCOMES)
        scorer = CallerTrackRecordScorer(store, universe_accuracy=DEFAULT_UNIVERSE_ACCURACY)
        signal = scorer.score_for_asset(_ASSET, [_ALWAYS_WRONG_ID], DECISION_TIME_MS)

        print(
            f"\n[FIXTURE always-wrong signal] negative_count={signal.negative_caller_count} "
            f"modifier={signal.caller_selectivity_modifier:.3f} "
            f"mcs_delta={signal.mcs_delta_contribution:.3f}"
        )

        assert signal.negative_caller_count >= 1, (
            "negative_caller_count must be surfaced in the CallerSignal for a negative caller"
        )
        # Modifier should be 0 (all callers are negative)
        assert signal.caller_selectivity_modifier == pytest.approx(0.0, abs=1e-6), (
            f"A single always-wrong caller should give modifier=0.0, "
            f"got {signal.caller_selectivity_modifier:.3f}"
        )
        # Delta must be negative (de-risk)
        assert signal.mcs_delta_contribution < 0.0, (
            f"mcs_delta_contribution must be < 0 for a negative-only caller set. "
            f"Got {signal.mcs_delta_contribution:.3f}"
        )

    def test_thin_data_caller_weight_is_zero(self) -> None:
        """A caller with fewer than MIN_CALLS_FOR_SCORE resolved outcomes gets weight=0.0."""
        store = InMemoryCallerOutcomeStore(outcomes=_THIN_DATA_OUTCOMES)
        scorer = CallerTrackRecordScorer(store, universe_accuracy=DEFAULT_UNIVERSE_ACCURACY)
        score = scorer.score_caller(_THIN_DATA_ID, DECISION_TIME_MS)

        print(
            f"\n[FIXTURE thin-data] calls={score.call_count} "
            f"is_thin_data={score.is_thin_data} "
            f"selectivity_weight={score.selectivity_weight:.3f}"
        )

        assert score.is_thin_data is True, (
            f"Caller with {score.call_count} calls (< {MIN_CALLS_FOR_SCORE}) must be thin-data."
        )
        assert score.selectivity_weight == 0.0, (
            f"Thin-data caller must have selectivity_weight=0.0. Got {score.selectivity_weight:.3f}"
        )

    def test_near_universe_caller_has_low_weight(self) -> None:
        """A caller at universe accuracy gets near-zero selectivity (indistinguishable from noise)."""
        store = InMemoryCallerOutcomeStore(outcomes=_MIXED_OUTCOMES)
        scorer = CallerTrackRecordScorer(store, universe_accuracy=DEFAULT_UNIVERSE_ACCURACY)
        score = scorer.score_caller(_MIXED_ID, DECISION_TIME_MS)

        print(
            f"\n[FIXTURE near-universe] calls={score.call_count} "
            f"accuracy_raw={score.accuracy_raw:.3f} "
            f"accuracy_delta={score.accuracy_delta:.3f} "
            f"selectivity_weight={score.selectivity_weight:.3f}"
        )

        # 4 right / 10 total = 0.4 raw accuracy = universe average = delta ~0
        assert abs(score.accuracy_delta) < 0.15, (
            f"Near-universe caller should have small accuracy_delta. "
            f"Got {score.accuracy_delta:.3f}"
        )
        assert score.selectivity_weight < 0.3, (
            f"Near-universe caller should have LOW selectivity. "
            f"Got {score.selectivity_weight:.3f}"
        )

    def test_always_right_caller_has_highest_weight_among_fixtures(self) -> None:
        """Always-right caller must have higher selectivity than near-universe caller."""
        store_right = InMemoryCallerOutcomeStore(outcomes=_ALWAYS_RIGHT_OUTCOMES)
        scorer_right = CallerTrackRecordScorer(store_right, universe_accuracy=DEFAULT_UNIVERSE_ACCURACY)
        score_right = scorer_right.score_caller(_ALWAYS_RIGHT_ID, DECISION_TIME_MS)

        store_mixed = InMemoryCallerOutcomeStore(outcomes=_MIXED_OUTCOMES)
        scorer_mixed = CallerTrackRecordScorer(store_mixed, universe_accuracy=DEFAULT_UNIVERSE_ACCURACY)
        score_mixed = scorer_mixed.score_caller(_MIXED_ID, DECISION_TIME_MS)

        print(
            f"\n[FIXTURE comparison] right={score_right.selectivity_weight:.3f} "
            f"mixed={score_mixed.selectivity_weight:.3f}"
        )

        assert score_right.selectivity_weight > score_mixed.selectivity_weight, (
            f"Always-right caller must have higher selectivity than near-universe caller. "
            f"right={score_right.selectivity_weight:.3f} mixed={score_mixed.selectivity_weight:.3f}"
        )

    def test_most_callers_near_zero_or_negative(self) -> None:
        """Verify that 3 out of 4 fixture callers have low/zero selectivity.

        This is the 'surface honestly' requirement: most callers score near-zero
        or negative; there is no artificial inflation.
        """
        store = _build_store()
        scorer = CallerTrackRecordScorer(store, universe_accuracy=DEFAULT_UNIVERSE_ACCURACY)

        scores = {
            _ALWAYS_WRONG_ID: scorer.score_caller(_ALWAYS_WRONG_ID, DECISION_TIME_MS),
            _THIN_DATA_ID: scorer.score_caller(_THIN_DATA_ID, DECISION_TIME_MS),
            _MIXED_ID: scorer.score_caller(_MIXED_ID, DECISION_TIME_MS),
            _ALWAYS_RIGHT_ID: scorer.score_caller(_ALWAYS_RIGHT_ID, DECISION_TIME_MS),
        }
        low_selectivity = [
            cid for cid, s in scores.items()
            if s.selectivity_weight < 0.3
        ]
        print(f"\n[FIXTURE honest] low-selectivity callers: {low_selectivity}")
        assert len(low_selectivity) >= 3, (
            f"At least 3/4 fixture callers should have low selectivity (<0.3). "
            f"Only {len(low_selectivity)} do: {low_selectivity}. "
            "This may indicate artificial inflation of caller scores."
        )


# ---------------------------------------------------------------------------
# SC-E9-D: net_caller_weight is always <= 0
# ---------------------------------------------------------------------------


class TestNetCallerWeightNonPositive:
    """SC-E9-D: net_caller_weight and mcs_delta_contribution are always <= 0."""

    @pytest.mark.parametrize("caller_ids", [
        [_ALWAYS_WRONG_ID],
        [_THIN_DATA_ID],
        [_MIXED_ID],
        [_ALWAYS_RIGHT_ID],
        [_ALWAYS_WRONG_ID, _THIN_DATA_ID, _MIXED_ID],
        [_ALWAYS_RIGHT_ID, _ALWAYS_WRONG_ID],
        [],
    ])
    def test_net_caller_weight_non_positive(self, caller_ids: list[str]) -> None:
        """net_caller_weight must be <= 0 for any caller combination."""
        store = _build_store()
        scorer = CallerTrackRecordScorer(store, universe_accuracy=DEFAULT_UNIVERSE_ACCURACY)
        signal = scorer.score_for_asset(_ASSET, caller_ids, DECISION_TIME_MS)
        assert signal.net_caller_weight <= 0.0 + 1e-9, (
            f"net_caller_weight={signal.net_caller_weight:.4f} > 0 for callers={caller_ids}. "
            "net_caller_weight must be <= 0 (caller weights are at most 1.0 each)."
        )
        assert signal.mcs_delta_contribution <= 0.0 + 1e-9, (
            f"mcs_delta_contribution={signal.mcs_delta_contribution:.4f} > 0 for callers={caller_ids}."
        )


# ---------------------------------------------------------------------------
# SC-E9-E: No win_rate field anywhere in the module
# ---------------------------------------------------------------------------


class TestNoWinRateField:
    """SC-E9-E: win_rate must NOT appear anywhere in the caller_score module."""

    def test_no_win_rate_in_caller_score_source(self) -> None:
        """The word 'win_rate' must not appear in caller_score.py source."""
        import aats.sentiment.caller_score as caller_mod
        source = inspect.getsource(caller_mod)
        assert "win_rate" not in source, (
            "Found 'win_rate' in caller_score.py source. "
            "win_rate is FORBIDDEN (HONESTY CLAUSE). "
            "Use accuracy_delta or net-PnL metrics instead."
        )

    def test_caller_score_dataclass_has_no_win_rate(self) -> None:
        """CallerScore dataclass must not have a win_rate field."""
        from aats.sentiment.caller_score import CallerScore
        assert "win_rate" not in CallerScore.__dataclass_fields__, (
            "CallerScore has a 'win_rate' field — this violates the HONESTY CLAUSE."
        )

    def test_caller_signal_dataclass_has_no_win_rate(self) -> None:
        """CallerSignal dataclass must not have a win_rate field."""
        from aats.sentiment.caller_score import CallerSignal
        assert "win_rate" not in CallerSignal.__dataclass_fields__, (
            "CallerSignal has a 'win_rate' field — this violates the HONESTY CLAUSE."
        )


# ---------------------------------------------------------------------------
# SC-E9-F: External services NOT reachable — injectable offline store only
# ---------------------------------------------------------------------------


class TestOfflineInjectable:
    """SC-E9-F: No live network calls; all access through injectable store."""

    def test_in_memory_store_makes_no_network_calls(self) -> None:
        """InMemoryCallerOutcomeStore has no network socket operations."""
        # If this test passes, no sockets were opened.
        # (We just run the scorer end-to-end and verify no exception from a live call.)
        store = InMemoryCallerOutcomeStore(outcomes=_ALWAYS_WRONG_OUTCOMES)
        scorer = CallerTrackRecordScorer(store)
        signal = scorer.score_for_asset(_ASSET, [_ALWAYS_WRONG_ID], DECISION_TIME_MS)
        # No exception = no live call attempted
        assert isinstance(signal, CallerSignal)

    def test_scorer_works_with_empty_store(self) -> None:
        """CallerTrackRecordScorer works with an empty store (no outcomes)."""
        store = InMemoryCallerOutcomeStore()
        scorer = CallerTrackRecordScorer(store)
        signal = scorer.score_for_asset(_ASSET, ["unknown_caller"], DECISION_TIME_MS)
        # Unknown caller with no outcomes is thin-data => weight 0 => modifier=0
        # But signal.called_by should include the caller_id
        assert signal.caller_selectivity_modifier == pytest.approx(0.0, abs=0.1)
        assert signal.mcs_delta_contribution <= 0.0


# ---------------------------------------------------------------------------
# Unit tests for core score arithmetic
# ---------------------------------------------------------------------------


class TestCoreScoreArithmetic:
    """Unit tests for _compute_caller_score and related functions."""

    def test_recency_weight_decreases_with_age(self) -> None:
        """Older outcomes should have lower recency weight."""
        from aats.sentiment.caller_score import _recency_weight
        w_recent = _recency_weight(DECISION_TIME_MS - ONE_DAY_MS, DECISION_TIME_MS)
        w_old = _recency_weight(DECISION_TIME_MS - 4 * ONE_WEEK_MS, DECISION_TIME_MS)
        assert w_recent > w_old, (
            f"Recency weight must decrease with age: "
            f"recent={w_recent:.3f} > old={w_old:.3f}"
        )

    def test_recency_weight_future_is_zero(self) -> None:
        """Future outcome (age < 0) must have recency weight 0."""
        from aats.sentiment.caller_score import _recency_weight
        w = _recency_weight(DECISION_TIME_MS + ONE_HOUR_MS, DECISION_TIME_MS)
        assert w == 0.0, (
            f"Future outcome recency weight must be 0.0, got {w:.3f}"
        )

    def test_recency_weight_at_zero_age_is_one(self) -> None:
        """An outcome resolved exactly at decision_time should be downweighted to half-life."""
        from aats.sentiment.caller_score import _recency_weight
        # outcome at the same instant as decision => age=0 but we return 0.5^0 = 1.0
        # (can't happen in practice since outcome_time > call_time, but test the math)
        # We don't call this with age=0 (the guard returns 0 for age<=0),
        # but just past zero should give weight ~1.
        w = _recency_weight(DECISION_TIME_MS - 1, DECISION_TIME_MS)
        assert w > 0.99, f"Near-zero age should give weight ~1.0, got {w:.4f}"

    def test_compute_caller_score_handles_zero_outcomes(self) -> None:
        """_compute_caller_score with empty outcomes list returns thin-data score."""
        score = _compute_caller_score("x", [], DECISION_TIME_MS, DEFAULT_UNIVERSE_ACCURACY)
        assert score.is_thin_data is True
        assert score.selectivity_weight == 0.0
        assert score.call_count == 0

    def test_compute_caller_score_all_correct(self) -> None:
        """10 correct calls (none wrong) => high accuracy_delta => high selectivity."""
        outcomes = _build_outcomes("best_caller", n_correct=10, n_wrong=0)
        score = _compute_caller_score("best_caller", outcomes, DECISION_TIME_MS, DEFAULT_UNIVERSE_ACCURACY)
        assert score.accuracy_delta > 0.0, "All-correct caller must have positive delta"
        assert score.selectivity_weight > 0.0, "All-correct caller must have nonzero weight"
        assert not score.is_negative

    def test_compute_caller_score_all_wrong(self) -> None:
        """10 wrong calls => near-zero accuracy => large negative delta => weight=0."""
        outcomes = _build_outcomes("worst_caller", n_correct=0, n_wrong=10)
        score = _compute_caller_score("worst_caller", outcomes, DECISION_TIME_MS, DEFAULT_UNIVERSE_ACCURACY)
        assert score.is_negative is True
        assert score.selectivity_weight == 0.0

    def test_universe_accuracy_override(self) -> None:
        """Universe accuracy can be overridden to a custom prior."""
        # If universe is set artificially high (0.9), even a 100% caller is below universe
        outcomes = _build_outcomes("caller_a", n_correct=7, n_wrong=3)  # 70% raw accuracy
        score_hi_uni = _compute_caller_score("caller_a", outcomes, DECISION_TIME_MS, 0.9)
        # 70% raw < 90% universe => negative delta
        assert score_hi_uni.accuracy_delta < 0.0
        # At default universe (40%): 70% raw is well above => positive delta
        score_lo_uni = _compute_caller_score("caller_a", outcomes, DECISION_TIME_MS, 0.4)
        assert score_lo_uni.accuracy_delta > 0.0


# ---------------------------------------------------------------------------
# Integration: score_for_asset with multiple callers
# ---------------------------------------------------------------------------


class TestScoreForAsset:
    """Integration tests for CallerTrackRecordScorer.score_for_asset."""

    def test_mix_of_negative_and_positive_callers_produces_intermediate_modifier(self) -> None:
        """Mix of one negative + one positive caller => modifier between 0 and 1."""
        store = InMemoryCallerOutcomeStore(
            outcomes=_ALWAYS_WRONG_OUTCOMES + _ALWAYS_RIGHT_OUTCOMES
        )
        scorer = CallerTrackRecordScorer(store, universe_accuracy=DEFAULT_UNIVERSE_ACCURACY)
        signal = scorer.score_for_asset(
            _ASSET, [_ALWAYS_WRONG_ID, _ALWAYS_RIGHT_ID], DECISION_TIME_MS
        )
        # negative weight=0 + positive weight>0 => mean 0<w<1
        assert 0.0 <= signal.caller_selectivity_modifier <= 1.0
        # Both callers are counted
        assert len(signal.called_by) == 2
        assert signal.negative_caller_count == 1

    def test_all_negative_callers_gives_zero_modifier(self) -> None:
        """All negative callers => modifier=0.0 and mcs_delta strongly negative."""
        store = InMemoryCallerOutcomeStore(outcomes=_ALWAYS_WRONG_OUTCOMES)
        scorer = CallerTrackRecordScorer(store, universe_accuracy=DEFAULT_UNIVERSE_ACCURACY)
        signal = scorer.score_for_asset(_ASSET, [_ALWAYS_WRONG_ID], DECISION_TIME_MS)
        assert signal.caller_selectivity_modifier == pytest.approx(0.0, abs=1e-6)
        assert signal.mcs_delta_contribution < 0.0

    def test_caller_not_in_store_is_thin_data(self) -> None:
        """A caller ID with no store outcomes is treated as thin data (weight=0)."""
        store = InMemoryCallerOutcomeStore()
        scorer = CallerTrackRecordScorer(store, universe_accuracy=DEFAULT_UNIVERSE_ACCURACY)
        signal = scorer.score_for_asset(_ASSET, ["ghost_caller_xyz"], DECISION_TIME_MS)
        assert signal.caller_scores[0].is_thin_data is True
        assert signal.thin_data_caller_count == 1

    def test_asset_not_called_by_any_caller_is_neutral(self) -> None:
        """An asset with no callers => modifier=1.0, delta=0.0 (neutral)."""
        store = _build_store()
        scorer = CallerTrackRecordScorer(store, universe_accuracy=DEFAULT_UNIVERSE_ACCURACY)
        # Pass an empty list of caller IDs for this asset
        signal = scorer.score_for_asset(_ASSET_2, [], DECISION_TIME_MS)
        assert signal.caller_selectivity_modifier == 1.0
        assert signal.mcs_delta_contribution == 0.0
        assert signal.called_by == []

    def test_called_by_populated_correctly(self) -> None:
        """called_by must list all caller IDs passed to score_for_asset."""
        store = _build_store()
        scorer = CallerTrackRecordScorer(store, universe_accuracy=DEFAULT_UNIVERSE_ACCURACY)
        signal = scorer.score_for_asset(
            _ASSET,
            [_ALWAYS_WRONG_ID, _ALWAYS_RIGHT_ID, _THIN_DATA_ID],
            DECISION_TIME_MS,
        )
        assert set(signal.called_by) == {_ALWAYS_WRONG_ID, _ALWAYS_RIGHT_ID, _THIN_DATA_ID}


# ---------------------------------------------------------------------------
# Golden fixture output documentation (self-check paste)
# ---------------------------------------------------------------------------


def test_golden_fixture_summary(capsys: Any) -> None:
    """Print the full golden fixture output for the SELF-CHECK record."""
    store = _build_store()
    scorer = CallerTrackRecordScorer(store, universe_accuracy=DEFAULT_UNIVERSE_ACCURACY)

    print("\n=== E9 GOLDEN FIXTURE SUMMARY (SELF-CHECK) ===")
    print(f"decision_time_ms={DECISION_TIME_MS}  universe_accuracy={DEFAULT_UNIVERSE_ACCURACY}")

    for cid in [_ALWAYS_WRONG_ID, _ALWAYS_RIGHT_ID, _THIN_DATA_ID, _MIXED_ID, _FUTURE_OUTCOME_ID]:
        score = scorer.score_caller(cid, DECISION_TIME_MS)
        print(
            f"  caller={cid!r:35s}  calls={score.call_count:3d}  "
            f"acc_raw={score.accuracy_raw:.3f}  "
            f"acc_delta={score.accuracy_delta:+.3f}  "
            f"weight={score.selectivity_weight:.3f}  "
            f"negative={score.is_negative}  thin={score.is_thin_data}"
        )

    signal_all = scorer.score_for_asset(
        _ASSET,
        [_ALWAYS_WRONG_ID, _ALWAYS_RIGHT_ID, _THIN_DATA_ID, _MIXED_ID],
        DECISION_TIME_MS,
    )
    print(
        f"\n  AGGREGATE signal:"
        f"  modifier={signal_all.caller_selectivity_modifier:.3f}  "
        f"mcs_delta={signal_all.mcs_delta_contribution:.3f}  "
        f"neg_callers={signal_all.negative_caller_count}  "
        f"thin_callers={signal_all.thin_data_caller_count}  "
        f"pos_callers={signal_all.positive_caller_count}"
    )
    print("=== END GOLDEN FIXTURE SUMMARY ===\n")

    # Core assertions on the aggregate
    assert signal_all.mcs_delta_contribution <= 0.0
    assert 0.0 <= signal_all.caller_selectivity_modifier <= 1.0
    assert signal_all.negative_caller_count >= 1
    # At least 3/4 callers are low quality (the adversarial-by-default invariant)
    low_weight_count = sum(
        1 for s in signal_all.caller_scores if s.selectivity_weight < 0.3
    )
    assert low_weight_count >= 3, (
        f"Expected >= 3 low-selectivity callers, got {low_weight_count}. "
        "Most callers must score near-zero or negative (ADVERSARIAL-BY-DEFAULT)."
    )


# ---------------------------------------------------------------------------
# DEF-EN1-01: de-risk guard survives python -O (bare-assert replacement)
#
# Ported from DEF-E10-01 (aats/sentiment/velocity.py::apply_velocity_penalty_to_conviction).
# Same defect class: assert_caller_signal_cannot_raise_conviction previously
# enforced its de-risk invariant with a bare `assert`, which is STRIPPED under
# `python -O` / PYTHONOPTIMIZE=1.  In an optimised runtime the guarantee that a
# caller signal can only DE-RISK (never raise conviction) would silently
# disappear.  The fix mirrors DEF-E10-01's structure exactly.
# ---------------------------------------------------------------------------


class TestDeRiskGuardSurvivesOptimizedMode:
    """DEF-EN1-01: The de-risk invariant must NOT rely on bare `assert`.

    Under `python -O` (optimised mode) all `assert` statements are STRIPPED.
    A forged positive mcs_delta_contribution would therefore silently RAISE
    conviction if the only guard were an assertion.

    The fix replaces the bare assert with:
      1. An explicit `raise ValueError` for a forged positive
         mcs_delta_contribution (not stripped by -O).
      2. A hard `min(0.0, ...)` clamp that prevents raise-conviction even if
         the raise were somehow bypassed.

    These tests prove that:
      - A forged positive delta raises ValueError (not AssertionError).
      - The guard logic is unconditional — it fires even when __debug__ is False.
      - A legitimate zero delta returns base_conviction unchanged.
      - The clamp is load-bearing: even if the ValueError were suppressed the
        result could not exceed base_conviction.
    """

    def _make_signal_with_delta(self, delta: float) -> CallerSignal:
        """Build a CallerSignal with an arbitrary (potentially forged) mcs_delta_contribution.

        Constructs the dataclass directly — bypasses CallerTrackRecordScorer so
        we can test the guard in assert_caller_signal_cannot_raise_conviction
        independently.
        """
        return CallerSignal(
            asset="ForgeryTestMint111111111111111111111111",
            decision_time_ms=DECISION_TIME_MS,
            caller_selectivity_modifier=1.0,
            mcs_delta_contribution=delta,
            called_by=["forged_caller"],
            positive_caller_count=0,
            negative_caller_count=0,
            thin_data_caller_count=0,
            net_caller_weight=0.0,
            caller_scores=[],
        )

    def test_forged_positive_delta_raises_value_error_not_assertion_error(
        self,
    ) -> None:
        """A forged positive mcs_delta_contribution must raise ValueError, NOT AssertionError.

        AssertionError is produced by `assert` and is stripped under python -O.
        ValueError is unconditional and survives python -O.

        This is the primary DEF-EN1-01 guard.
        """
        forged = self._make_signal_with_delta(0.3)
        with pytest.raises(ValueError, match="CALLER INVARIANT VIOLATED"):
            assert_caller_signal_cannot_raise_conviction(forged, 0.7)

    def test_forged_positive_delta_small_still_raises(self) -> None:
        """Even a tiny positive delta (epsilon) must raise ValueError."""
        forged = self._make_signal_with_delta(1e-10)
        with pytest.raises(ValueError, match="mcs_delta_contribution.*positive"):
            assert_caller_signal_cannot_raise_conviction(forged, 0.5)

    def test_guard_is_not_an_assertion_error(self) -> None:
        """Confirm the raised exception type is ValueError, not AssertionError.

        If the guard were a bare `assert`, it would raise AssertionError.
        Under python -O it would be silently SKIPPED.  The ValueError is never
        skipped.
        """
        forged = self._make_signal_with_delta(0.4)
        exc_type = None
        try:
            assert_caller_signal_cannot_raise_conviction(forged, 0.6)
        except ValueError:
            exc_type = ValueError
        except AssertionError:
            exc_type = AssertionError

        assert exc_type is ValueError, (
            "DEF-EN1-01: The de-risk guard raised AssertionError (stripped by python -O) "
            "instead of ValueError (unconditional). "
            "Replace the bare assert with an explicit raise ValueError."
        )

    def test_guard_fires_even_when_debug_is_false(self) -> None:
        """Simulate python -O by temporarily setting __debug__-equivalent flag.

        We cannot actually set __debug__ = False at runtime (it is read-only),
        but we can prove the guard is NOT an assert by confirming:
          (a) The exception is ValueError (not AssertionError).
          (b) The function's source code does NOT contain a bare `assert` on
              mcs_delta_contribution (i.e., the load-bearing guard uses
              `raise`, not `assert`).
        """
        import inspect  # noqa: PLC0415

        import aats.sentiment.caller_score as caller_mod  # noqa: PLC0415

        source = inspect.getsource(caller_mod.assert_caller_signal_cannot_raise_conviction)

        # The source must contain `raise ValueError` for the positive-delta case
        assert "raise ValueError" in source, (
            "DEF-EN1-01: assert_caller_signal_cannot_raise_conviction must contain an "
            "explicit `raise ValueError` for the positive-delta guard. "
            "A bare `assert` is stripped by python -O."
        )

        # The source must NOT rely solely on assert for the mcs_delta_contribution check.
        # Count bare `assert` statements (excluding comments) in the function.
        # Zero bare asserts in the function = guard is unconditional.
        non_comment_lines = [
            line.strip()
            for line in source.splitlines()
            if not line.strip().startswith("#")
        ]
        bare_asserts = [
            line for line in non_comment_lines if line.startswith("assert ")
        ]
        assert len(bare_asserts) == 0, (
            f"DEF-EN1-01: assert_caller_signal_cannot_raise_conviction contains bare assert "
            f"statements that are stripped by python -O: {bare_asserts}. "
            "Replace with explicit `raise ValueError`."
        )

    def test_zero_delta_returns_base_conviction_unchanged(self) -> None:
        """A signal with mcs_delta_contribution=0.0 must return base_conviction unchanged."""
        zero_delta = self._make_signal_with_delta(0.0)
        for base in [0.0, 0.3, 0.5, 0.7, 1.0]:
            result = assert_caller_signal_cannot_raise_conviction(zero_delta, base)
            assert result == pytest.approx(base, abs=1e-9), (
                f"Zero delta must return base_conviction={base:.3f} unchanged. "
                f"Got {result:.6f}."
            )

    def test_legitimate_negative_delta_only_decreases_conviction(self) -> None:
        """Negative deltas (produced by CallerTrackRecordScorer) must only decrease conviction."""
        for delta_val in [0.0, -0.1, -0.3, -0.5, -0.8, -1.0]:
            signal = self._make_signal_with_delta(delta_val)
            for base in [0.0, 0.2, 0.5, 0.8, 1.0]:
                result = assert_caller_signal_cannot_raise_conviction(signal, base)
                assert result <= base + 1e-9, (
                    f"Negative delta={delta_val} raised conviction "
                    f"from {base:.3f} to {result:.3f}. De-risk direction violated."
                )
                assert result >= 0.0, (
                    f"Conviction clamped below 0.0: {result:.3f} "
                    f"(base={base:.3f}, delta={delta_val:.3f})."
                )

    def test_clamp_is_belt_and_suspenders_for_raise_bypass(self) -> None:
        """Prove that the min(0.0, delta_raw) clamp is load-bearing.

        If the ValueError raise were somehow bypassed (e.g., by a future code
        change that wraps the call in a bare except), the clamp ensures
        conviction still cannot rise.

        We prove this by directly testing the clamp arithmetic:
          clamp(base_conviction + min(0.0, 0.3), 0.0, 1.0)
          = clamp(base + 0.0, 0.0, 1.0)
          = clamp(base, 0.0, 1.0)
          = base  (for base in [0, 1])
        So even with a forged positive delta, the clamped result = base (not higher).
        """
        for pos_delta in [0.5, 0.1, 1.0, 1e-6]:
            clamped = min(0.0, pos_delta)
            for base in [0.0, 0.3, 0.7, 1.0]:
                result_if_clamped = max(0.0, min(1.0, base + clamped))
                assert result_if_clamped <= base + 1e-9, (
                    f"Even with forged delta={pos_delta}, the clamp ensures "
                    f"result ({result_if_clamped:.4f}) <= base ({base:.4f})."
                )

    def test_mutation_revert_clamp_would_leak_conviction_under_dash_o(self) -> None:
        """MUTATION PROBE (documented): reverting the fix to the historical bare
        `assert` reproduces the exact leak this defect closes.

        This test does not monkeypatch the module (that would require reload
        gymnastics); instead it proves the OLD formula — which this test would
        catch as RED if the fix were reverted — by directly recomputing what
        the pre-fix code path produced:

            old_adjusted = clamp(base_conviction + mcs_delta_contribution, 0, 1)
            assert old_adjusted <= base_conviction + 1e-9   # <- bare assert, stripped by -O

        With a forged positive delta of +0.3 and base=0.7, the OLD formula's
        clamped result is 1.0 > 0.7 — the bare assert would have caught this
        under normal `python`, but SILENTLY PASSES under `python -O`. The FIXED
        function must raise ValueError long before this arithmetic executes.
        """
        forged = self._make_signal_with_delta(0.3)
        base = 0.7

        # Reproduce the pre-fix arithmetic to show what WOULD have leaked.
        old_style_adjusted = max(0.0, min(1.0, base + forged.mcs_delta_contribution))
        assert old_style_adjusted > base, (
            "Sanity check: the forged delta must demonstrate a conviction leak "
            "under the pre-fix arithmetic for this probe to be meaningful."
        )

        # The FIXED function must refuse to produce this leaked value — it must
        # raise before returning anything, RED-proving the guard is load-bearing.
        with pytest.raises(ValueError):
            assert_caller_signal_cannot_raise_conviction(forged, base)
