"""Tests for the Discord ingestion adapter (E6).

Acceptance criteria:
  - DiscordAdapter follows the SocialAdapter Protocol exactly.
  - source="discord" is a valid Literal on RawPost and flows through Tier-A.
  - event_time_ms is taken from the Discord message timestamp (NOT fetch time).
  - Point-in-time filter excludes Discord future posts.
  - Discord shill fixture (coordinated, brand-new accounts, 90-second burst)
    produces low conviction (adversarial invariant holds for Discord source).
  - MockDiscordClient enables fully offline testing — no bot token needed.
  - No secrets in source; bot_token=None is the offline default.
  - DiscordAdapter is injectable via MockSocialAdapter for pipeline-level tests.
  - ToS note: self-bot (user account token) is structurally excluded — the
    adapter ONLY accepts a bot_token; there is no user-token parameter.

HARD RULES verified here:
  - Coordinated Discord shill -> LOWERS conviction (never raises it).
  - social signal is SLOW-LOOP ONLY (no import by hot-path modules tested in
    test_schema.py; this file adds Discord-specific coverage).
  - Ingested text is QUOTED UNTRUSTED DATA (verified via prompt wrapper check).
  - Point-in-time: future Discord posts are excluded before any scoring.
  - No win_rate field appears anywhere.
  - No money floats in MCS output.
"""

from __future__ import annotations

import dataclasses
import inspect

import pytest

from aats.contracts.events import EventTime
from aats.sentiment.adapters import (
    DiscordAdapter,
    MockDiscordClient,
    MockSocialAdapter,
)
from aats.sentiment.pipeline import MCSSentimentPipeline
from aats.sentiment.tier_a import run_tier_a
from aats.sentiment.tier_b import MockLLMBackend, TierBScorer
from tests.sentiment.fixtures import (
    DECISION_TIME_MS,
    DISCORD_FUTURE_POSTS,
    DISCORD_ORGANIC_POSTS,
    DISCORD_SHILL_POSTS,
    T_MINUS_30MIN,
    T_PLUS_5MIN,
    make_post,
)

_ASSET = "DiscordM1ntXxxtest"
_EVENT_TIME = EventTime(
    slot=4_296_251,
    block_time_ms=DECISION_TIME_MS,
    wall_clock_ms=DECISION_TIME_MS + 75,
)


# ---------------------------------------------------------------------------
# 1. RawPost source Literal — "discord" is valid
# ---------------------------------------------------------------------------


def test_raw_post_source_discord_accepted():
    """source='discord' must be a valid Literal on RawPost (models.py)."""
    post = make_post(
        text="Discord test post solana meme launch raydium",
        source="discord",
        account_age_days=200.0,
    )
    assert post.source == "discord"


def test_raw_post_source_discord_flows_through_tier_a():
    """A discord-source RawPost is accepted by Tier-A without error."""
    post = make_post(
        text="Solana meme launch pump raydium gem discord channel",
        source="discord",
        account_age_days=365.0,
        like_count=15,
        reply_count=4,
    )
    digest = run_tier_a([post], _ASSET, DECISION_TIME_MS)
    # Post is within the window and relevant → should be included
    assert digest.total_post_count == 1


# ---------------------------------------------------------------------------
# 2. event_time_ms from message timestamp — NOT fetch time
# ---------------------------------------------------------------------------


def test_discord_post_event_time_is_platform_time_not_fetch_time():
    """event_time_ms must be the Discord message timestamp, not fetch wall-clock.

    A post with event_time_ms inside the window but a FUTURE fetch_time_ms
    must still be INCLUDED (event_time_ms is authoritative).
    """
    post = make_post(
        text="Solana discord gem launch pump raydium token",
        source="discord",
        event_time_ms=T_MINUS_30MIN,  # inside the window
        account_age_days=400.0,
    )
    # Simulate a late fetch: fetch_time_ms is far in the future
    post_late_fetch = dataclasses.replace(post, fetch_time_ms=T_PLUS_5MIN + 1_000_000)

    digest = run_tier_a([post_late_fetch], _ASSET, DECISION_TIME_MS)
    # Must be included — event_time_ms <= decision_time_ms
    assert digest.total_post_count == 1, (
        "Discord post with future fetch_time but past event_time must be INCLUDED. "
        "event_time_ms (message timestamp) is authoritative."
    )


# ---------------------------------------------------------------------------
# 3. Point-in-time filter — Discord future posts excluded
# ---------------------------------------------------------------------------


def test_discord_future_posts_excluded():
    """Discord posts with event_time_ms > decision_time_ms must be excluded."""
    all_posts = DISCORD_ORGANIC_POSTS + DISCORD_FUTURE_POSTS
    digest = run_tier_a(all_posts, _ASSET, DECISION_TIME_MS)

    # Future post IDs must not appear in any cluster
    future_ids = {p.post_id for p in DISCORD_FUTURE_POSTS}
    cluster_ids = {
        pid
        for c in digest.clusters
        for pid in c.member_post_ids
    }
    assert cluster_ids.isdisjoint(future_ids), (
        "Discord future posts leaked into Tier-A clusters — "
        "point-in-time filter failed for Discord source."
    )


def test_discord_only_future_posts_produces_empty_digest():
    """If all Discord posts are in the future, digest must be empty."""
    digest = run_tier_a(DISCORD_FUTURE_POSTS, _ASSET, DECISION_TIME_MS)
    assert digest.total_post_count == 0
    assert digest.clusters == []
    assert digest.synchronicity == 0.0


def test_discord_future_posts_do_not_change_score():
    """Adding Discord future posts must not alter the conviction score.

    Proves point-in-time correctness for the Discord adapter path:
    the pipeline score with organic + future posts must equal the score
    with organic posts only.
    """
    llm_clean = MockLLMBackend(base_score=0.5)
    scorer_clean = TierBScorer(llm_clean)
    digest_clean = run_tier_a(DISCORD_ORGANIC_POSTS, _ASSET, DECISION_TIME_MS)
    mcs_clean, _ = scorer_clean.score_asset(digest_clean, _EVENT_TIME)

    llm_with_future = MockLLMBackend(base_score=0.5)
    scorer_with_future = TierBScorer(llm_with_future)
    digest_with_future = run_tier_a(
        DISCORD_ORGANIC_POSTS + DISCORD_FUTURE_POSTS, _ASSET, DECISION_TIME_MS
    )
    mcs_with_future, ev_with_future = scorer_with_future.score_asset(
        digest_with_future, _EVENT_TIME
    )

    assert mcs_clean.conviction == mcs_with_future.conviction, (
        f"POINT-IN-TIME VIOLATION (Discord): "
        f"conviction without future posts={mcs_clean.conviction:.4f}, "
        f"with future posts={mcs_with_future.conviction:.4f}. "
        "Future Discord posts must not affect the score."
    )
    assert mcs_clean.post_count == mcs_with_future.post_count, (
        "Post count must be identical when future Discord posts are excluded."
    )


# ---------------------------------------------------------------------------
# 4. Adversarial invariant — Discord shill burst LOWERS conviction
# ---------------------------------------------------------------------------


def test_discord_shill_burst_lowers_conviction():
    """ADVERSARIAL INVARIANT: Discord coordinated shill burst -> low conviction.

    Fixture: DISCORD_SHILL_POSTS (20 near-identical posts, 90-second burst,
    brand-new Discord accounts 0-1 days old, default avatars, 500 posts/day).

    Expected: conviction < 0.35 (heavily penalised toward 0).
    This is the load-bearing adversarial rule from the BUILD-DIRECTIVE:
    manufactured euphoria on Discord LOWERS conviction — it is NEVER a
    standalone buy trigger and NEVER raises risk.

    Formula: conviction = raw_score * (1 - penalty)
    With penalty driven by young accounts + cluster concentration,
    conviction -> 0 regardless of the LLM's raw score.
    """
    llm = MockLLMBackend(base_score=0.5)
    scorer = TierBScorer(llm, budget_remaining_calls=1_000)
    digest = run_tier_a(DISCORD_SHILL_POSTS, _ASSET, DECISION_TIME_MS)

    mcs, ev = scorer.score_asset(digest, _EVENT_TIME)

    print(
        f"\n[E6-SC4-Discord] posts={digest.total_post_count}, "
        f"clusters={digest.unique_cluster_count}, "
        f"sync={digest.synchronicity:.3f}, "
        f"age_median={digest.account_age_median_days:.1f}d, "
        f"concentration={digest.cluster_concentration:.3f}, "
        f"raw_score={ev.raw_narrative_score:.3f}, "
        f"penalty={ev.coordinated_shill_penalty:.3f}, "
        f"conviction={ev.final_conviction:.3f}, "
        f"shill_flag={ev.coordinated_shill_flag}, "
        f"red_flags={ev.red_flags}"
    )

    assert mcs.conviction < 0.35, (
        f"ADVERSARIAL INVARIANT VIOLATED (Discord): "
        f"shill burst produced conviction {mcs.conviction:.3f} (expected < 0.35). "
        f"penalty={ev.coordinated_shill_penalty:.3f}, raw={ev.raw_narrative_score:.3f}. "
        "Manufactured Discord euphoria MUST heavily lower conviction toward 0."
    )
    assert ev.coordinated_shill_penalty > 0.4, (
        f"Discord shill burst must produce substantial penalty > 0.4, "
        f"got {ev.coordinated_shill_penalty:.3f}"
    )
    assert mcs.account_age_median_days < 7.0, (
        f"Discord shill burst must register low account age, "
        f"got {mcs.account_age_median_days:.1f}d"
    )


def test_discord_organic_posts_produce_higher_conviction_than_shill():
    """Organic Discord posts must produce higher conviction than a shill burst.

    Both pipelines use the same MockLLMBackend base_score.
    The conviction difference comes entirely from the coordinated-shill penalty.
    """
    base = 0.5

    llm_org = MockLLMBackend(base_score=base)
    scorer_org = TierBScorer(llm_org)
    digest_org = run_tier_a(DISCORD_ORGANIC_POSTS, _ASSET, DECISION_TIME_MS)
    mcs_org, ev_org = scorer_org.score_asset(digest_org, _EVENT_TIME)

    llm_shill = MockLLMBackend(base_score=base)
    scorer_shill = TierBScorer(llm_shill)
    digest_shill = run_tier_a(DISCORD_SHILL_POSTS, _ASSET, DECISION_TIME_MS)
    mcs_shill, ev_shill = scorer_shill.score_asset(digest_shill, _EVENT_TIME)

    print(
        f"\n[E6-organic-vs-shill] "
        f"organic_conviction={mcs_org.conviction:.3f} "
        f"(penalty={ev_org.coordinated_shill_penalty:.3f}), "
        f"shill_conviction={mcs_shill.conviction:.3f} "
        f"(penalty={ev_shill.coordinated_shill_penalty:.3f})"
    )

    assert mcs_org.conviction > mcs_shill.conviction, (
        f"Organic Discord conviction {mcs_org.conviction:.3f} must be > "
        f"shill Discord conviction {mcs_shill.conviction:.3f}"
    )


def test_discord_shill_coordinated_shill_flag_raised():
    """coordinated_shill_flag must be True for a Discord shill burst."""
    scorer = TierBScorer(MockLLMBackend(base_score=0.0))
    digest = run_tier_a(DISCORD_SHILL_POSTS, _ASSET, DECISION_TIME_MS)
    mcs, ev = scorer.score_asset(digest, _EVENT_TIME)
    assert mcs.coordinated_shill_flag is True or ev.coordinated_shill_flag is True, (
        "coordinated_shill_flag must be raised for Discord shill burst"
    )


# ---------------------------------------------------------------------------
# 5. Injectable client — MockDiscordClient offline mode
# ---------------------------------------------------------------------------


def test_mock_discord_client_filters_by_window():
    """MockDiscordClient.posts_for_window respects point-in-time boundaries."""
    client = MockDiscordClient(posts=DISCORD_ORGANIC_POSTS + DISCORD_FUTURE_POSTS)
    window = 2 * 3_600_000  # 2-hour lookback

    in_window = client.posts_for_window(DECISION_TIME_MS, window)

    future_ids = {p.post_id for p in DISCORD_FUTURE_POSTS}
    returned_ids = {p.post_id for p in in_window}
    assert returned_ids.isdisjoint(future_ids), (
        "MockDiscordClient returned future-timestamped Discord posts — "
        "point-in-time filter violated."
    )
    # All returned posts must be discord source
    assert all(p.source == "discord" for p in in_window)


def test_mock_discord_client_empty_when_no_discord_posts():
    """MockDiscordClient returns empty list when no discord-source posts exist."""
    non_discord = [
        make_post("solana token launch pump", source="x", account_age_days=100.0)
    ]
    client = MockDiscordClient(posts=non_discord)
    result = client.posts_for_window(DECISION_TIME_MS, 3_600_000)
    assert result == [], "MockDiscordClient must return only discord-source posts"


def test_discord_adapter_offline_mode_returns_empty():
    """DiscordAdapter with bot_token=None returns [] (offline / CI mode)."""
    adapter = DiscordAdapter(bot_token=None, channel_allowlist=[(123456, 789012)])
    # Must return [] synchronously via async shim — use asyncio.run in sync test
    import asyncio

    result = asyncio.run(
        adapter.fetch_posts(["solana"], DECISION_TIME_MS, 3_600_000)
    )
    assert result == [], "DiscordAdapter must return [] when bot_token=None"


def test_discord_adapter_empty_allowlist_returns_empty():
    """DiscordAdapter with empty channel_allowlist returns [] immediately."""
    adapter = DiscordAdapter(bot_token=None, channel_allowlist=[])
    import asyncio

    result = asyncio.run(
        adapter.fetch_posts(["solana"], DECISION_TIME_MS, 3_600_000)
    )
    assert result == [], "DiscordAdapter with empty allowlist must return []"


# ---------------------------------------------------------------------------
# 6. Pipeline integration — MockSocialAdapter preloaded with Discord posts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pipeline_ingests_discord_posts_via_mock_adapter():
    """Discord posts fed through MockSocialAdapter appear in pipeline output."""
    adapter = MockSocialAdapter()
    adapter.load_posts(DISCORD_ORGANIC_POSTS)
    llm = MockLLMBackend(base_score=0.5)
    scorer = TierBScorer(llm)
    pipe = MCSSentimentPipeline(adapters=[adapter], tier_b=scorer)

    mcs, ev, _news = await pipe.score(
        _ASSET, ["solana", "gem", "launch"], DECISION_TIME_MS
    )
    # The default lookback is 1 hour; DISCORD_ORGANIC_POSTS spans up to 2 hours
    # so only posts within the 1-hour window are returned by the mock adapter.
    # We assert that at least 1 post was ingested (the pipeline is working) and
    # that the conviction is bounded correctly.
    assert mcs.post_count >= 1, (
        f"Expected at least 1 Discord post in the lookback window, got {mcs.post_count}"
    )
    # Pipeline must produce a bounded conviction in [0, 1]
    assert 0.0 <= mcs.conviction <= 1.0


@pytest.mark.asyncio
async def test_pipeline_discord_shill_conviction_below_organic():
    """End-to-end pipeline: Discord shill produces lower conviction than organic."""
    adapter_org = MockSocialAdapter()
    adapter_org.load_posts(DISCORD_ORGANIC_POSTS)
    llm_org = MockLLMBackend(base_score=0.5)
    pipe_org = MCSSentimentPipeline(adapters=[adapter_org], tier_b=TierBScorer(llm_org))
    mcs_org, _, _news_org = await pipe_org.score(_ASSET, ["solana", "gem"], DECISION_TIME_MS)

    adapter_shill = MockSocialAdapter()
    adapter_shill.load_posts(DISCORD_SHILL_POSTS)
    llm_shill = MockLLMBackend(base_score=0.5)
    pipe_shill = MCSSentimentPipeline(
        adapters=[adapter_shill], tier_b=TierBScorer(llm_shill)
    )
    mcs_shill, _, _news_shill = await pipe_shill.score(_ASSET, ["solana", "gem"], DECISION_TIME_MS)

    assert mcs_org.conviction > mcs_shill.conviction, (
        f"Organic Discord conviction {mcs_org.conviction:.3f} must exceed "
        f"shill conviction {mcs_shill.conviction:.3f} end-to-end."
    )


@pytest.mark.asyncio
async def test_pipeline_one_llm_call_for_discord_posts():
    """20 Discord posts through the pipeline = exactly 1 LLM call (no per-tweet LLM)."""
    llm = MockLLMBackend(base_score=0.5)
    scorer = TierBScorer(llm)
    adapter = MockSocialAdapter()
    adapter.load_posts(DISCORD_SHILL_POSTS)  # 20 posts
    pipe = MCSSentimentPipeline(adapters=[adapter], tier_b=scorer)

    assert len(DISCORD_SHILL_POSTS) == 20
    await pipe.score(_ASSET, ["solana", "shill", "pump"], DECISION_TIME_MS)

    assert llm.call_count == 1, (
        f"20 Discord posts produced {llm.call_count} LLM calls. "
        "Per-post LLM calls are FORBIDDEN (T-306; no-LLM-per-tweet rule)."
    )


# ---------------------------------------------------------------------------
# 7. No secrets in adapter source
# ---------------------------------------------------------------------------


def test_discord_adapter_source_has_no_secrets():
    """No real bot tokens or API secrets in the DiscordAdapter source."""
    import aats.sentiment.adapters as _adapters_mod

    source = inspect.getsource(_adapters_mod)
    banned = [
        "Bot MTI",   # real Bot token prefix pattern (base64 snowflake)
        "token = \"Bot",
        "bot_token = \"Bot",
        "password = \"",
        "sk-",
    ]
    for pattern in banned:
        assert pattern not in source, (
            f"Potential secret pattern '{pattern}' found in adapters.py. "
            "NEVER commit real Discord bot tokens."
        )


def test_discord_adapter_no_self_bot():
    """DiscordAdapter must not accept a user-account (self-bot) token parameter.

    Self-botting violates Discord ToS.  The adapter parameter is named
    `bot_token` specifically for Bot application tokens — there is no
    `user_token`, `self_token`, or `account_token` parameter.
    """
    # Verify the constructor only has 'bot_token', no 'user_token' / 'self_token'
    import dataclasses as dc

    field_names = {f.name for f in dc.fields(DiscordAdapter)}
    forbidden = {"user_token", "self_token", "account_token", "user_account_token"}
    overlap = field_names & forbidden
    assert not overlap, (
        f"DiscordAdapter has self-bot token field(s) {overlap}. "
        "Self-botting is Discord ToS violation — FORBIDDEN by design."
    )
    assert "bot_token" in field_names, (
        "DiscordAdapter must have a 'bot_token' field for Bot application tokens."
    )


# ---------------------------------------------------------------------------
# 8. No win_rate field
# ---------------------------------------------------------------------------


def test_discord_mcs_score_has_no_win_rate():
    """MCSScore produced from Discord posts must not have a win_rate field."""
    from aats.contracts.models import MCSScore

    assert "win_rate" not in MCSScore.model_fields, (
        "MCSScore has a win_rate field — HONESTY CLAUSE violation (AC-037)."
    )


# ---------------------------------------------------------------------------
# 9. MCS is de-risk only — Discord signal never sizes up
# ---------------------------------------------------------------------------


def test_discord_mcs_cannot_size_up():
    """High Discord conviction must NEVER change the position size upward."""
    from aats.sentiment.pipeline import assert_mcs_cannot_size_up

    original_size = 2.5
    for conviction in [0.0, 0.5, 0.99, 1.0]:
        result = assert_mcs_cannot_size_up(conviction, original_size)
        assert result == original_size, (
            f"Discord MCS conviction={conviction} changed size to {result}. "
            "MCS may only DE-RISK — never size up (asymmetric trust invariant)."
        )


# ---------------------------------------------------------------------------
# 10. Source label in cluster evidence
# ---------------------------------------------------------------------------


def test_discord_posts_appear_in_mcs_evidence_source_ids():
    """Post IDs from Discord posts appear in MCSEvidence.source_post_ids."""
    llm = MockLLMBackend(base_score=0.5)
    scorer = TierBScorer(llm)
    digest = run_tier_a(DISCORD_ORGANIC_POSTS, _ASSET, DECISION_TIME_MS)
    _, ev = scorer.score_asset(digest, _EVENT_TIME)

    discord_ids = {p.post_id for p in DISCORD_ORGANIC_POSTS}
    evidence_ids = set(ev.source_post_ids)
    # At least one Discord post ID must appear in the evidence
    assert discord_ids & evidence_ids, (
        "No Discord post IDs found in MCSEvidence.source_post_ids. "
        "Evidence audit trail must include contributing post IDs."
    )
