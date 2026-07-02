"""EN2a — VelocitySignalComputer wired into MCSSentimentPipeline.score() as a sidecar.

Self-check items verified here:
  EN2a-A: pipeline.score() returns a 4-tuple (MCSScore, MCSEvidence, NewsSignal,
          VelocitySignal) — VelocitySignal is an ADDITIONAL sidecar output; the
          frozen MCSScore contract shape is unchanged.
  EN2a-B: MUTATION-MEANINGFUL — a coordinated bot-burst (COORDINATED_SHILL_POSTS,
          the same golden fixture used by the SC-4 adversarial test) produces a
          positive VelocitySignal.mcs_penalty, and pipeline.score()'s returned
          conviction is STRICTLY LOWER than, and exactly equal to the
          additive-clamped combination of, the pre-velocity (Tier-A + Tier-B
          only) conviction.  If the Stage 6/7 wiring in pipeline.score() is
          ever removed, this test goes RED (proven manually — see HANDOFF).
  EN2a-C: Same additive-clamped pattern as NewsSignal.mcs_delta — a fully
          organic, non-bursty post set inside the velocity window produces
          mcs_penalty == 0.0 and pipeline conviction EXACTLY equal to the
          pre-velocity conviction (no accidental effect on clean traffic).
  EN2a-D: Velocity penalty and news delta compose SEQUENTIALLY (news applied
          first, velocity applied second), both additive-clamped to [0, 1],
          matching the news-then-velocity order documented in pipeline.py.
  EN2a-E: No external data dependency — VelocityEvents are derived entirely
          from the RawPosts the pipeline already ingested; zero extra network
          or LLM calls (LLM call_count stays at exactly 1 per asset).
"""

from __future__ import annotations

import pytest

from aats.contracts.events import EventTime
from aats.sentiment.adapters import MockSocialAdapter
from aats.sentiment.pipeline import MCSSentimentPipeline
from aats.sentiment.tier_a import run_tier_a
from aats.sentiment.tier_b import MockLLMBackend, TierBScorer
from aats.sentiment.velocity import apply_velocity_penalty_to_conviction
from tests.sentiment.fixtures import (
    COORDINATED_SHILL_POSTS,
    DECISION_TIME_MS,
    T_MINUS_10MIN,
    make_post,
)
from tests.sentiment.news_fixtures import CREDIBLE_NEGATIVE_ARTICLES

_ASSET = "VelocityWiringM1ntTestXxxxxxxxxxxxxxxxx"
_KEYWORDS = ["solana", "shill", "gem"]
_REFERENCE_EVENT_TIME = EventTime(
    slot=4_296_260,
    block_time_ms=DECISION_TIME_MS,
    wall_clock_ms=DECISION_TIME_MS + 60,
)

# Recent, established, low-cadence posts INSIDE the velocity module's default
# 15-minute rolling window but with none of the bot/shill markers — used to
# prove the sidecar leaves organic traffic untouched (mcs_penalty == 0.0).
RECENT_ORGANIC_POSTS = [
    make_post(
        text=(
            "Solid fundamentals on this one — team doxxed, audit posted, "
            "liquidity locked for 6 months. Slow and steady."
        ),
        event_time_ms=T_MINUS_10MIN + i * 90_000,  # spread across ~7.5 minutes
        account_age_days=400.0 + i * 20.0,
        follower_count=8_000 + i * 500,
        following_count=600,
        like_count=30,
        reply_count=6,
        repost_count=2,
        is_verified=(i % 2 == 0),
        has_default_avatar=False,
        posting_cadence_per_day=4.0,
        author_id=f"organic_recent_author_{i}",
    )
    for i in range(5)
]


def _pre_velocity_conviction(posts: list, base_score: float = 0.7) -> float:
    """Reproduce Stages 1-3 of pipeline.score() (Tier-A + Tier-B, no news, no
    velocity) directly, so we have an independent ground truth for "what
    conviction would pipeline.score() return if the EN2a wiring did not exist".
    """
    digest = run_tier_a(posts, _ASSET, DECISION_TIME_MS)
    reference_scorer = TierBScorer(MockLLMBackend(base_score=base_score))
    mcs, _evidence = reference_scorer.score_asset(digest, _REFERENCE_EVENT_TIME)
    return mcs.conviction


# ---------------------------------------------------------------------------
# EN2a-A: sidecar return shape
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_score_returns_four_tuple_with_velocity_signal() -> None:
    """pipeline.score() returns (MCSScore, MCSEvidence, NewsSignal, VelocitySignal)."""
    adapter = MockSocialAdapter()
    adapter.load_posts(RECENT_ORGANIC_POSTS)
    pipe = MCSSentimentPipeline(adapters=[adapter], tier_b=TierBScorer(MockLLMBackend(base_score=0.6)))

    result = await pipe.score(_ASSET, _KEYWORDS, DECISION_TIME_MS)

    assert len(result) == 4, f"pipeline.score() must return a 4-tuple, got {len(result)}"
    mcs_score, evidence, news_signal, velocity_signal = result
    assert hasattr(velocity_signal, "mcs_penalty")
    assert hasattr(velocity_signal, "account_age_cohort_flag")
    assert hasattr(velocity_signal, "red_flags")
    # Frozen MCSScore contract fields are all still present (sidecar, not a replacement)
    for field in (
        "asset", "event_time", "conviction", "momentum", "novelty",
        "synchronicity", "account_age_median_days", "coordinated_shill_flag",
        "red_flags", "post_count", "reasoning",
    ):
        assert hasattr(mcs_score, field), f"MCSScore missing frozen field: {field}"
    assert hasattr(evidence, "coordinated_shill_penalty")
    assert hasattr(news_signal, "mcs_delta")


# ---------------------------------------------------------------------------
# EN2a-B: MUTATION-MEANINGFUL — velocity penalty flows through and LOWERS conviction
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_velocity_penalty_flows_through_score_and_lowers_conviction() -> None:
    """The coordinated-shill-burst velocity penalty must flow through score().

    Input fixture: COORDINATED_SHILL_POSTS (20 near-identical posts, 90s burst,
    brand-new accounts 0-1 days old, default avatars, 500 posts/day cadence —
    the SAME golden fixture the SC-4 adversarial test uses for Tier-A/Tier-B).

    Method: independently reproduce the PRE-velocity conviction (Tier-A +
    Tier-B only, identical digest/base_score, no news adapters configured so
    news_delta == 0.0), then run the SAME posts through the full pipeline
    (which additionally applies the EN2a velocity sidecar).

    ASSERTIONS (both must hold — this is what makes the test mutation-meaningful):
      1. velocity_signal.mcs_penalty > 0.0 (the fixture must actually trigger it).
      2. pipeline conviction is STRICTLY LOWER than the pre-velocity conviction.
      3. pipeline conviction EXACTLY equals apply_velocity_penalty_to_conviction(
         pre_velocity_conviction, velocity_signal) — the same additive-clamped
         helper used for the news delta, applied to the SAME digest's raw score.

    If the Stage 6/7 velocity-wiring block in pipeline.score() is ever deleted
    or neutered, pipeline conviction would EQUAL pre_velocity_conviction and
    assertions 2 and 3 above both go RED (manually verified — see HANDOFF).
    """
    pre_velocity_conviction = _pre_velocity_conviction(COORDINATED_SHILL_POSTS, base_score=0.7)

    adapter = MockSocialAdapter()
    adapter.load_posts(COORDINATED_SHILL_POSTS)
    pipe = MCSSentimentPipeline(adapters=[adapter], tier_b=TierBScorer(MockLLMBackend(base_score=0.7)))

    mcs_full, evidence, news_signal, velocity_signal = await pipe.score(
        _ASSET, _KEYWORDS, DECISION_TIME_MS
    )

    print(
        f"\n[EN2a-B] pre_velocity_conviction={pre_velocity_conviction:.4f}, "
        f"velocity_penalty={velocity_signal.mcs_penalty:.4f}, "
        f"pipeline_conviction={mcs_full.conviction:.4f}, "
        f"velocity_red_flags={velocity_signal.red_flags}"
    )

    assert news_signal.mcs_delta == 0.0, "no news adapters configured — isolate the velocity effect"

    assert velocity_signal.mcs_penalty > 0.0, (
        "FIXTURE INVALID: COORDINATED_SHILL_POSTS (bot-burst, brand-new accounts) "
        f"must trigger a positive velocity penalty. Got {velocity_signal.mcs_penalty:.4f}."
    )

    assert mcs_full.conviction < pre_velocity_conviction, (
        "EN2a WIRING NOT LIVE: pipeline conviction "
        f"({mcs_full.conviction:.4f}) must be strictly lower than the "
        f"pre-velocity conviction ({pre_velocity_conviction:.4f}). "
        "The velocity penalty did not flow through score()."
    )

    expected_conviction = apply_velocity_penalty_to_conviction(
        pre_velocity_conviction, velocity_signal
    )
    assert mcs_full.conviction == pytest.approx(expected_conviction, abs=1e-9), (
        f"pipeline conviction ({mcs_full.conviction:.6f}) does not match the "
        f"additive-clamped velocity formula applied to the pre-velocity score "
        f"({expected_conviction:.6f}). The wiring must use "
        "apply_velocity_penalty_to_conviction() exactly."
    )

    # Velocity red flags must be merged into the evidence + MCSScore audit trail.
    for flag in velocity_signal.red_flags:
        assert flag in mcs_full.red_flags, (
            f"Velocity red_flag '{flag}' not merged into MCSScore.red_flags: {mcs_full.red_flags}"
        )
    assert evidence.final_conviction == pytest.approx(mcs_full.conviction, abs=1e-9)


# ---------------------------------------------------------------------------
# EN2a-C: clean/organic traffic is untouched (penalty == 0.0, no side effect)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_organic_recent_traffic_has_zero_velocity_penalty_and_conviction_unchanged() -> None:
    """A clean, established, low-cadence post set inside the velocity window
    must produce mcs_penalty == 0.0 and leave conviction EXACTLY unchanged —
    proving the sidecar can only ever subtract, never alter clean traffic.
    """
    pre_velocity_conviction = _pre_velocity_conviction(RECENT_ORGANIC_POSTS, base_score=0.6)

    adapter = MockSocialAdapter()
    adapter.load_posts(RECENT_ORGANIC_POSTS)
    pipe = MCSSentimentPipeline(adapters=[adapter], tier_b=TierBScorer(MockLLMBackend(base_score=0.6)))

    mcs_full, _evidence, news_signal, velocity_signal = await pipe.score(
        _ASSET, _KEYWORDS, DECISION_TIME_MS
    )

    print(
        f"\n[EN2a-C] pre_velocity_conviction={pre_velocity_conviction:.4f}, "
        f"velocity_penalty={velocity_signal.mcs_penalty:.4f}, "
        f"pipeline_conviction={mcs_full.conviction:.4f}"
    )

    assert news_signal.mcs_delta == 0.0
    assert velocity_signal.mcs_penalty == 0.0, (
        f"Clean organic traffic must produce mcs_penalty == 0.0. "
        f"Got {velocity_signal.mcs_penalty:.4f}."
    )
    assert mcs_full.conviction == pytest.approx(pre_velocity_conviction, abs=1e-9), (
        "Zero velocity penalty must leave conviction EXACTLY unchanged."
    )


# ---------------------------------------------------------------------------
# EN2a-D: velocity and news penalties compose sequentially, both clamped
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_velocity_and_news_penalties_compose_sequentially() -> None:
    """Credible negative news AND a bot-velocity burst together must compose
    sequentially (news applied first, velocity applied second), matching the
    documented Stage 5 -> Stage 7 order in pipeline.py, both additive-clamped
    to [0, 1].
    """
    pre_velocity_conviction = _pre_velocity_conviction(COORDINATED_SHILL_POSTS, base_score=0.7)

    pipe = MCSSentimentPipeline.offline(
        preloaded_posts=COORDINATED_SHILL_POSTS,
        mock_llm_base_score=0.7,
        preloaded_news=CREDIBLE_NEGATIVE_ARTICLES,
    )
    mcs_full, _evidence, news_signal, velocity_signal = await pipe.score(
        _ASSET, _KEYWORDS, DECISION_TIME_MS
    )

    print(
        f"\n[EN2a-D] pre_velocity_conviction={pre_velocity_conviction:.4f}, "
        f"news_delta={news_signal.mcs_delta:.4f}, "
        f"velocity_penalty={velocity_signal.mcs_penalty:.4f}, "
        f"pipeline_conviction={mcs_full.conviction:.4f}"
    )

    assert news_signal.mcs_delta < 0.0, "fixture must trigger a credible negative news event"
    assert velocity_signal.mcs_penalty > 0.0, "fixture must trigger a positive velocity penalty"

    after_news = max(0.0, min(1.0, pre_velocity_conviction + news_signal.mcs_delta))
    expected_final = apply_velocity_penalty_to_conviction(after_news, velocity_signal)

    assert mcs_full.conviction == pytest.approx(expected_final, abs=1e-9), (
        f"Sequential composition mismatch: expected {expected_final:.6f} "
        f"(news then velocity), got {mcs_full.conviction:.6f}."
    )
    assert mcs_full.conviction <= pre_velocity_conviction + 1e-9, (
        "Combined news + velocity adjustment must never exceed the pre-adjustment conviction."
    )
    assert mcs_full.conviction >= 0.0


# ---------------------------------------------------------------------------
# EN2a-E: no external data dependency — zero extra network/LLM calls
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_velocity_sidecar_adds_zero_extra_llm_calls() -> None:
    """The velocity sidecar must add ZERO extra LLM calls (no-LLM-per-tweet,
    and no LLM call at all for velocity — it is pure arithmetic over the
    RawPosts already ingested).
    """
    llm = MockLLMBackend(base_score=0.7)
    scorer = TierBScorer(llm)
    adapter = MockSocialAdapter()
    adapter.load_posts(COORDINATED_SHILL_POSTS)  # 20 posts
    pipe = MCSSentimentPipeline(adapters=[adapter], tier_b=scorer)

    _mcs, _ev, _news, velocity_signal = await pipe.score(_ASSET, _KEYWORDS, DECISION_TIME_MS)

    assert llm.call_count == 1, (
        f"20 posts through the pipeline (with the EN2a velocity sidecar wired in) "
        f"produced {llm.call_count} LLM calls; expected exactly 1 (Tier-B batch call). "
        "The velocity sidecar must never call the LLM."
    )
    assert velocity_signal.total_events_in_window == len(COORDINATED_SHILL_POSTS), (
        "Velocity sidecar must see all 20 posts already ingested by the pipeline "
        "(no external data dependency, no additional fetch)."
    )
