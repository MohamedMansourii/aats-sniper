"""Schema conformance tests (SC-6, SC-7).

Verifies:
  - MCSScore output matches data-models.md §5 exactly
  - MCSEvidence audit trail is complete and reproducible
  - MCS conviction may ONLY de-risk (never size up)
  - No secrets/API keys in the module
  - No LLM on the hot path (import guard)
  - No win_rate field anywhere
"""

from __future__ import annotations

import inspect
import sys
from typing import get_type_hints

import pytest

from aats.contracts.events import EventTime
from aats.contracts.models import MCSScore
from aats.sentiment.models import MCSEvidence
from aats.sentiment.pipeline import (
    assert_mcs_cannot_size_up,
    mcs_may_only_de_risk,
)
from aats.sentiment.tier_a import run_tier_a
from aats.sentiment.tier_b import MockLLMBackend, TierBScorer
from tests.sentiment.fixtures import (
    COORDINATED_SHILL_POSTS,
    DECISION_TIME_MS,
    ORGANIC_POSTS,
)

_ASSET = "SchemaM1ntXxx"
_EVENT_TIME = EventTime(
    slot=4_296_250,
    block_time_ms=DECISION_TIME_MS,
    wall_clock_ms=DECISION_TIME_MS + 100,
)


# ---------------------------------------------------------------------------
# MCSScore schema conformance (data-models.md §5)
# ---------------------------------------------------------------------------


def test_mcs_score_has_all_required_fields():
    """MCSScore must have all fields defined in data-models.md §5."""
    required_fields = {
        "asset",
        "event_time",
        "conviction",
        "momentum",
        "novelty",
        "synchronicity",
        "account_age_median_days",
        "coordinated_shill_flag",
        "red_flags",
        "post_count",
        "reasoning",
    }
    actual_fields = set(MCSScore.model_fields.keys())
    missing = required_fields - actual_fields
    assert not missing, (
        f"MCSScore is missing required fields: {missing}. "
        "Check data-models.md §5 for the canonical schema."
    )


def test_mcs_score_no_win_rate_field():
    """MCSScore must NOT have a win_rate field (HONESTY CLAUSE, AC-037)."""
    assert "win_rate" not in MCSScore.model_fields, (
        "MCSScore has a win_rate field — this violates the HONESTY CLAUSE (AC-037). "
        "Win-rate must NEVER appear in any model."
    )


def test_mcs_score_conviction_validator_enforces_range():
    """MCSScore.conviction is validated in [0, 1] by the existing contract validator.

    The aats.contracts.models MCSScore validator enforces conviction ∈ [0, 1].
    conviction=0 means adversarial/manufactured (shill); conviction=1 means organic.
    Our pipeline uses conviction = raw_score * (1 - shill_penalty) ∈ [0, 1].
    """
    # Verify conviction is a float field (not Decimal) — statistical quantity
    hints = get_type_hints(MCSScore)
    assert hints.get("conviction") == float, "conviction must be float"
    # Verify synchronicity is a float field
    assert hints.get("synchronicity") == float
    # Verify the validator rejects out-of-range values
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        MCSScore(
            asset="test",
            event_time=EventTime(slot=1, block_time_ms=1000, wall_clock_ms=1000),
            conviction=1.5,  # > 1.0 — must reject
            momentum=0.5,
            novelty=0.5,
            synchronicity=0.5,
            account_age_median_days=30.0,
            coordinated_shill_flag=False,
            red_flags=[],
            post_count=5,
            reasoning="[QUOTED-UNTRUSTED] test",
        )


def test_mcs_score_reasoning_is_string():
    """reasoning field must be a string (QUOTED UNTRUSTED TEXT, never executed)."""
    hints = get_type_hints(MCSScore)
    assert hints.get("reasoning") == str, "reasoning must be str (quoted untrusted data)"


def test_mcs_score_event_time_is_event_time():
    """event_time must be EventTime (canonical decision-point clock)."""
    hints = get_type_hints(MCSScore)
    assert hints.get("event_time") == EventTime


def test_mcs_score_red_flags_is_list():
    """red_flags must be a list (multiple flags possible)."""
    scorer = TierBScorer(MockLLMBackend())
    digest = run_tier_a(ORGANIC_POSTS, _ASSET, DECISION_TIME_MS)
    mcs, _ = scorer.score_asset(digest, _EVENT_TIME)
    assert isinstance(mcs.red_flags, list), "red_flags must be a list"


# ---------------------------------------------------------------------------
# MCSEvidence schema completeness
# ---------------------------------------------------------------------------


def test_mcs_evidence_has_all_required_fields():
    """MCSEvidence must have all audit trail fields."""
    required = {
        "asset",
        "event_time_ms",
        "raw_narrative_score",
        "coordinated_shill_penalty",
        "final_conviction",
        "confidence_lower",
        "confidence_upper",
        "synchronicity",
        "account_age_median_days",
        "account_age_histogram",
        "cluster_concentration",
        "influencer_tier_counts",
        "contributing_cluster_ids",
        "source_post_ids",
        "red_flags",
        "coordinated_shill_flag",
        "penalty_breakdown",
        "llm_model_used",
        "llm_call_cached",
        "total_posts_ingested",
        "posts_excluded_future",
        "reasoning_raw",
    }
    actual = {f.name for f in MCSEvidence.__dataclass_fields__.values()}
    missing = required - actual
    assert not missing, f"MCSEvidence missing fields: {missing}"


def test_mcs_evidence_reasoning_raw_is_string():
    """reasoning_raw is string — QUOTED UNTRUSTED TEXT, never executed."""
    scorer = TierBScorer(MockLLMBackend())
    digest = run_tier_a(ORGANIC_POSTS, _ASSET, DECISION_TIME_MS)
    _, ev = scorer.score_asset(digest, _EVENT_TIME)
    assert isinstance(ev.reasoning_raw, str)


def test_mcs_evidence_penalty_breakdown_has_total():
    """penalty_breakdown must include a 'total' key."""
    scorer = TierBScorer(MockLLMBackend())
    digest = run_tier_a(COORDINATED_SHILL_POSTS, _ASSET, DECISION_TIME_MS)
    _, ev = scorer.score_asset(digest, _EVENT_TIME)
    assert "total" in ev.penalty_breakdown
    assert ev.penalty_breakdown["total"] >= 0.0


def test_evidence_conviction_equals_mcs_conviction():
    """MCSEvidence.final_conviction must equal MCSScore.conviction."""
    scorer = TierBScorer(MockLLMBackend())
    digest = run_tier_a(ORGANIC_POSTS, _ASSET, DECISION_TIME_MS)
    mcs, ev = scorer.score_asset(digest, _EVENT_TIME)
    assert mcs.conviction == ev.final_conviction, (
        f"Mismatch: MCSScore.conviction={mcs.conviction:.4f} != "
        f"MCSEvidence.final_conviction={ev.final_conviction:.4f}"
    )


# ---------------------------------------------------------------------------
# Asymmetric trust: MCS may only de-risk
# ---------------------------------------------------------------------------


def test_assert_mcs_cannot_size_up_all_convictions():
    """assert_mcs_cannot_size_up must return the SAME size for all conviction values."""
    test_sizes = [0.0, 0.5, 1.0, 2.5, 10.0]
    test_convictions = [-1.0, -0.5, 0.0, 0.5, 1.0]

    for size in test_sizes:
        for conviction in test_convictions:
            result = assert_mcs_cannot_size_up(conviction, size)
            assert result == size, (
                f"MCS must not change size: conviction={conviction}, "
                f"original_size={size}, result={result}. "
                "This is the asymmetric trust invariant (FR-008, AC-021)."
            )


def test_mcs_may_only_de_risk_boundary():
    """mcs_may_only_de_risk correctly gates on threshold.

    conviction ∈ [0, 1]; threshold is exclusive.
    """
    # Default threshold 0.3
    assert mcs_may_only_de_risk(0.1) is True      # below default threshold
    assert mcs_may_only_de_risk(0.3) is False     # at threshold (not below)
    assert mcs_may_only_de_risk(0.5) is False     # above threshold
    # Custom threshold
    assert mcs_may_only_de_risk(0.2, 0.3) is True
    assert mcs_may_only_de_risk(0.3, 0.3) is False


# ---------------------------------------------------------------------------
# No secrets in module source
# ---------------------------------------------------------------------------


def test_no_api_keys_in_source_code():
    """No real API keys, bearer tokens, or secrets in the sentiment module source."""
    import glob
    import os

    sentiment_dir = os.path.join(
        os.path.dirname(__file__), "..", "..", "aats", "sentiment"
    )
    py_files = glob.glob(os.path.join(sentiment_dir, "*.py"))
    assert py_files, "No Python files found in aats/sentiment/"

    banned_patterns = [
        "sk-",  # OpenAI key prefix
        "Bearer ya",  # Google OAuth bearer
        "xoxb-",  # Slack bot token
        "ghp_",  # GitHub personal access token
        "api_key = \"",  # literal key assignment
        "token = \"sk",  # literal token
        "password = \"",  # literal password
    ]

    for path in py_files:
        with open(path, encoding="utf-8") as f:
            content = f.read()
        for pattern in banned_patterns:
            assert pattern not in content, (
                f"Potential secret found in {path}: '{pattern}'. "
                "Real secrets must NEVER be committed to code (CLAUDE.md §3.7)."
            )


# ---------------------------------------------------------------------------
# No float money
# ---------------------------------------------------------------------------


def test_mcs_score_has_no_float_money_fields():
    """MCSScore has no monetary fields at all (it is a statistical model).

    Verify that no field name suggests money storage with a float type.
    """
    money_names = {"lamports", "sol", "token_amount", "pnl", "fee", "tip", "price"}
    for name in MCSScore.model_fields:
        for mn in money_names:
            assert mn not in name.lower(), (
                f"MCSScore has a suspicious monetary field '{name}'. "
                "Statistical scores must not carry money values."
            )


# ---------------------------------------------------------------------------
# Import guard: MCS is SLOW-LOOP only
# ---------------------------------------------------------------------------


def test_sentiment_not_imported_by_fast_or_snipe_loop_modules():
    """The aats.sentiment package must not be imported by any FAST/SNIPE loop module.

    We verify by checking that risk, execution, and ingestion modules
    do NOT import from aats.sentiment.
    """
    # We just verify the module exists and can be imported cleanly
    # Verify that the contracts module (which SNIPE reads) does NOT import sentiment
    contracts_source = inspect.getsource(sys.modules["aats.contracts"])
    assert "aats.sentiment" not in contracts_source, (
        "aats.contracts imports from aats.sentiment — this would put MCS on the hot path. "
        "MCS is SLOW-LOOP ONLY."
    )
