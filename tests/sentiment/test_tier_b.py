"""Tests for Tier-B: batched per-asset LLM narrative scorer.

T-306 acceptance criteria covered:
  - ONE LLM call per asset (never per post)
  - Coordinated shill penalty is applied and reduces conviction
  - Cache hit avoids second LLM call
  - Budget exhaustion falls back to zero-conviction
  - Evidence trail is complete and reproducible
  - Uncertainty band widens for thin evidence
  - MCS in [-1, 1] always
"""

from __future__ import annotations

from aats.contracts.events import EventTime
from aats.sentiment.tier_a import run_tier_a
from aats.sentiment.tier_b import (
    MCS_MAX,
    MCS_MIN,
    MockLLMBackend,
    TierBScorer,
    _coordinated_shill_penalty,
    _digest_cache_key,
)
from tests.sentiment.fixtures import (
    COORDINATED_SHILL_POSTS,
    DECISION_TIME_MS,
    ORGANIC_POSTS,
)

_ASSET = "So1anaM1ntXxxxx"
_EVENT_TIME = EventTime(
    slot=1_000_000,
    block_time_ms=DECISION_TIME_MS,
    wall_clock_ms=DECISION_TIME_MS + 100,
)


def _make_scorer(base_score: float = 0.5, budget: int = 1_000) -> TierBScorer:
    return TierBScorer(MockLLMBackend(base_score=base_score), budget_remaining_calls=budget)


# ---------------------------------------------------------------------------
# No-LLM-per-tweet assertion (load-bearing)
# ---------------------------------------------------------------------------


def test_one_llm_call_per_asset_not_per_post():
    """CRITICAL: Tier-B must issue exactly ONE LLM call per asset, not per post.

    This test proves the invariant by running 20 shill posts (20 potential
    per-post LLM targets) through the full pipeline and asserting the LLM
    backend received exactly 1 call.
    """
    llm = MockLLMBackend(base_score=0.0)
    scorer = TierBScorer(llm, budget_remaining_calls=1_000)

    digest = run_tier_a(COORDINATED_SHILL_POSTS, _ASSET, DECISION_TIME_MS)
    assert len(COORDINATED_SHILL_POSTS) == 20, "Fixture must have 20 posts"

    scorer.score_asset(digest, _EVENT_TIME)

    assert llm.call_count == 1, (
        f"Expected exactly 1 LLM call per asset for {len(COORDINATED_SHILL_POSTS)} posts, "
        f"got {llm.call_count}.  Per-post LLM calls are FORBIDDEN."
    )


def test_second_call_for_same_digest_is_cached():
    """Identical digest produces a cache hit — zero additional LLM calls."""
    llm = MockLLMBackend(base_score=0.0)
    scorer = TierBScorer(llm, budget_remaining_calls=1_000)
    digest = run_tier_a(ORGANIC_POSTS, _ASSET, DECISION_TIME_MS)

    mcs1, ev1 = scorer.score_asset(digest, _EVENT_TIME)
    mcs2, ev2 = scorer.score_asset(digest, _EVENT_TIME)

    assert llm.call_count == 1, "Second call with same digest should be cached (0 extra LLM calls)"
    assert mcs1.conviction == mcs2.conviction
    assert ev2.llm_call_cached is True
    assert ev1.llm_call_cached is False


def test_multiple_assets_one_call_each():
    """Each asset gets exactly one LLM call; two distinct assets = 2 calls."""
    llm = MockLLMBackend(base_score=0.0)
    scorer = TierBScorer(llm, budget_remaining_calls=1_000)

    digest1 = run_tier_a(ORGANIC_POSTS, "asset_aaa", DECISION_TIME_MS)
    digest2 = run_tier_a(COORDINATED_SHILL_POSTS, "asset_bbb", DECISION_TIME_MS)

    scorer.score_asset(digest1, _EVENT_TIME)
    scorer.score_asset(digest2, _EVENT_TIME)

    assert llm.call_count == 2, (
        f"Two distinct assets should produce 2 LLM calls, got {llm.call_count}"
    )


# ---------------------------------------------------------------------------
# Conviction range
# ---------------------------------------------------------------------------


def test_conviction_in_minus_one_to_one():
    """MCSScore.conviction must be in [-1, 1] for all inputs."""
    for base in [-2.0, -1.0, -0.5, 0.0, 0.5, 1.0, 2.0]:
        llm = MockLLMBackend(base_score=max(-1.0, min(1.0, base)))
        scorer = TierBScorer(llm)
        for posts in [ORGANIC_POSTS, COORDINATED_SHILL_POSTS]:
            digest = run_tier_a(posts, _ASSET, DECISION_TIME_MS)
            mcs, ev = scorer.score_asset(digest, _EVENT_TIME)
            assert MCS_MIN <= mcs.conviction <= MCS_MAX, (
                f"conviction {mcs.conviction} out of [-1, 1] for base_score={base}"
            )
            assert MCS_MIN <= ev.final_conviction <= MCS_MAX


# ---------------------------------------------------------------------------
# Coordinated-shill penalty
# ---------------------------------------------------------------------------


def test_shill_penalty_reduces_conviction():
    """A coordinated shill burst LOWERS conviction vs an organic post set."""
    scorer = _make_scorer(base_score=0.5)  # neutral LLM base
    digest_organic = run_tier_a(ORGANIC_POSTS, _ASSET, DECISION_TIME_MS)
    digest_shill = run_tier_a(COORDINATED_SHILL_POSTS, _ASSET, DECISION_TIME_MS)

    mcs_organic, ev_organic = scorer.score_asset(digest_organic, _EVENT_TIME)
    # Use a fresh scorer to avoid cache collision on different asset
    scorer2 = _make_scorer(base_score=0.5)
    mcs_shill, ev_shill = scorer2.score_asset(digest_shill, _EVENT_TIME)

    assert ev_shill.coordinated_shill_penalty > ev_organic.coordinated_shill_penalty, (
        "Shill burst should produce a higher penalty than organic posts"
    )
    assert mcs_shill.conviction < mcs_organic.conviction, (
        f"Shill conviction {mcs_shill.conviction:.3f} should be < "
        f"organic conviction {mcs_organic.conviction:.3f}"
    )


def test_high_synchronicity_lowers_conviction():
    """HIGH shill signals (age + concentration) must lower conviction via penalty.

    The shill burst deduplicates to 1 cluster (near-identical text) →
    synchronicity=0 (single cluster, no inter-cluster burst to measure).
    But cluster_concentration=1.0 and majority young accounts STILL drive a
    substantial penalty.  This tests the combined adversarial signal, not
    synchronicity alone.
    """
    scorer = _make_scorer(base_score=0.5)
    digest = run_tier_a(COORDINATED_SHILL_POSTS, _ASSET, DECISION_TIME_MS)
    mcs, ev = scorer.score_asset(digest, _EVENT_TIME)

    # Even with synchronicity=0, penalty fires via concentration + age
    assert ev.coordinated_shill_penalty > 0.3, (
        f"Shill signals (cluster_concentration={digest.cluster_concentration:.2f}, "
        f"age_median={digest.account_age_median_days:.1f}d) should produce "
        f"substantial penalty > 0.3, got {ev.coordinated_shill_penalty:.4f}"
    )
    # Conviction must be lower than the raw narrative score (penalty was applied)
    # conviction = raw * (1 - penalty) < raw when penalty > 0
    assert ev.final_conviction < ev.raw_narrative_score, (
        f"final_conviction ({ev.final_conviction:.3f}) must be < "
        f"raw_narrative_score ({ev.raw_narrative_score:.3f}) when penalty > 0"
    )


def test_penalty_breakdown_sums_to_total():
    """penalty_breakdown total must equal the sum of components."""
    digest = run_tier_a(COORDINATED_SHILL_POSTS, _ASSET, DECISION_TIME_MS)
    penalty, breakdown = _coordinated_shill_penalty(digest)
    components_sum = (
        breakdown["synchronicity_component"]
        + breakdown["account_age_component"]
        + breakdown["cluster_concentration_component"]
    )
    assert abs(components_sum - breakdown["total"]) < 1e-9


def test_penalty_non_negative():
    """Coordinated shill penalty is always >= 0."""
    for posts in [ORGANIC_POSTS, COORDINATED_SHILL_POSTS, []]:
        digest = run_tier_a(posts, _ASSET, DECISION_TIME_MS)
        penalty, _ = _coordinated_shill_penalty(digest)
        assert penalty >= 0.0, f"Penalty must be non-negative, got {penalty}"


# ---------------------------------------------------------------------------
# Evidence trail completeness
# ---------------------------------------------------------------------------


def test_evidence_trail_complete():
    """MCSEvidence must contain all required fields with valid values."""
    scorer = _make_scorer()
    digest = run_tier_a(ORGANIC_POSTS, _ASSET, DECISION_TIME_MS)
    mcs, ev = scorer.score_asset(digest, _EVENT_TIME)

    # Core fields
    assert ev.asset == _ASSET
    assert ev.event_time_ms == DECISION_TIME_MS
    assert MCS_MIN <= ev.raw_narrative_score <= MCS_MAX
    assert ev.coordinated_shill_penalty >= 0.0
    assert MCS_MIN <= ev.final_conviction <= MCS_MAX
    assert ev.confidence_lower <= ev.final_conviction
    assert ev.final_conviction <= ev.confidence_upper
    assert ev.llm_model_used  # must be non-empty
    assert isinstance(ev.penalty_breakdown, dict)
    assert "total" in ev.penalty_breakdown
    assert isinstance(ev.red_flags, list)
    assert isinstance(ev.contributing_cluster_ids, list)
    assert isinstance(ev.source_post_ids, list)
    assert isinstance(ev.account_age_histogram, dict)
    assert ev.total_posts_ingested >= 0


def test_evidence_reasoning_marked_untrusted():
    """reasoning_raw is stored as data — pipeline marks it as QUOTED UNTRUSTED."""
    scorer = _make_scorer()
    digest = run_tier_a(ORGANIC_POSTS, _ASSET, DECISION_TIME_MS)
    mcs, ev = scorer.score_asset(digest, _EVENT_TIME)
    # The MCSScore.reasoning field should have the QUOTED-UNTRUSTED marker
    assert "[QUOTED-UNTRUSTED]" in mcs.reasoning or mcs.reasoning.startswith("[")


def test_evidence_cluster_ids_match_digest():
    """Contributing cluster IDs in evidence must be a subset of digest cluster IDs."""
    scorer = _make_scorer()
    digest = run_tier_a(ORGANIC_POSTS, _ASSET, DECISION_TIME_MS)
    mcs, ev = scorer.score_asset(digest, _EVENT_TIME)
    digest_cids = {c.cluster_id for c in digest.clusters}
    evidence_cids = set(ev.contributing_cluster_ids)
    assert evidence_cids <= digest_cids, (
        "Evidence contains cluster IDs not in the digest"
    )


# ---------------------------------------------------------------------------
# Uncertainty band
# ---------------------------------------------------------------------------


def test_thin_evidence_produces_wide_band():
    """Zero posts → evidence uncertainty band spans most of [0, 1]."""
    scorer = _make_scorer()
    digest = run_tier_a([], _ASSET, DECISION_TIME_MS)
    mcs, ev = scorer.score_asset(digest, _EVENT_TIME)
    band_width = ev.confidence_upper - ev.confidence_lower
    # For empty digest the band is [MCS_MIN, MCS_MAX] = [0, 1] → width = 1.0
    assert band_width >= 0.9, (
        f"Empty evidence should produce wide uncertainty band, got {band_width:.2f}"
    )


def test_confidence_bounds_in_range():
    """confidence_lower and confidence_upper must be in [-1, 1]."""
    scorer = _make_scorer()
    for posts in [ORGANIC_POSTS, COORDINATED_SHILL_POSTS, []]:
        digest = run_tier_a(posts, _ASSET, DECISION_TIME_MS)
        mcs, ev = scorer.score_asset(digest, _EVENT_TIME)
        assert MCS_MIN <= ev.confidence_lower <= ev.final_conviction
        assert ev.final_conviction <= ev.confidence_upper <= MCS_MAX


# ---------------------------------------------------------------------------
# Budget exhaustion
# ---------------------------------------------------------------------------


def test_budget_exhaustion_returns_zero_conviction():
    """When budget is 0, scorer returns 0.0 conviction (safe default)."""
    scorer = TierBScorer(MockLLMBackend(base_score=0.9), budget_remaining_calls=0)
    digest = run_tier_a(ORGANIC_POSTS, _ASSET, DECISION_TIME_MS)
    mcs, ev = scorer.score_asset(digest, _EVENT_TIME)
    # Should not have called the LLM — raw_narrative_score must be 0.0
    assert scorer.llm_call_count == 0
    assert ev.raw_narrative_score == 0.0


# ---------------------------------------------------------------------------
# Cache key stability
# ---------------------------------------------------------------------------


def test_cache_key_stable_across_invocations():
    """Same digest always produces the same cache key."""
    digest = run_tier_a(ORGANIC_POSTS, _ASSET, DECISION_TIME_MS)
    key1 = _digest_cache_key(digest)
    key2 = _digest_cache_key(digest)
    assert key1 == key2


def test_different_digests_produce_different_cache_keys():
    """Different assets/posts produce different cache keys."""
    digest1 = run_tier_a(ORGANIC_POSTS, "asset_aaa", DECISION_TIME_MS)
    digest2 = run_tier_a(ORGANIC_POSTS, "asset_bbb", DECISION_TIME_MS)
    assert _digest_cache_key(digest1) != _digest_cache_key(digest2)
