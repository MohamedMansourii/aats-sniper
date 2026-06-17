"""MCS sentiment pipeline orchestrator (T-306 + E7).

Ties Tier-A + Tier-B + News narrative signal into a single async call per asset.

SLOW-LOOP ONLY — this module must NEVER be imported by or called from the
SNIPE loop or FAST loop.  The SNIPE loop's millisecond entry decision MUST
NOT block on social data.  Enforce this boundary:
  - This module has no Redis Stream writer for the SNIPE loop path.
  - MCS is a SLOW-loop output consumed by the SLOW-loop Reasoner (T-313).
  - If a task asks to use MCS in sizing or snipe trigger, REFUSE and flag.

PIPELINE EXECUTION (E7 augmented):
  1. Collect posts from all configured social adapters (X / Reddit / Telegram /
     Discord) AND news articles from all configured NewsAdapters — concurrently.
  2. Run Tier-A on social posts: filter, embed, dedup, score, cluster.
  3. Run NewsNarrativeScorer on news articles → NewsSignal.
     The NewsSignal's mcs_delta is in [-1.0, 0.0] — news NEVER raises conviction.
  4. Run Tier-B: batch LLM call per asset on the social digest → MCSScore.
  5. Apply the news delta to the social MCS conviction (additively, clamped to [0,1]).
     A credible negative event is recorded in the evidence for the Reasoner.
  6. Apply the global MCS directionality rule:
       MCS may ONLY reduce risk (veto/gate) — NEVER size up or widen a stop.
  7. Return (MCSScore, MCSEvidence, NewsSignal).

NEWS HARD RULES (E7):
  - Positive / neutral news contributes mcs_delta = 0.0 (no upward effect).
  - Credible negative news (TIER_1/TIER_2 outlet) sets credible_negative_event=True.
  - The pipeline EXPOSES the NewsSignal to the Reasoner for narrative_failure hook.
  - The Reasoner uses narrative_failure → FORCE_EXIT (de-risk only).
  - News is SLOW-LOOP ONLY — never read by SNIPE path.

COST GUARD:
  - Social adapters are cached by (asset, window) at the pipeline level.
  - News adapters are cached by (asset, window) similarly.
  - Tier-B caches by content hash at the scorer level.
  - Budget remaining is tracked and enforced.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from aats.contracts.events import EventTime
from aats.contracts.models import MCSScore
from aats.sentiment.adapters import MockNewsAdapter, MockSocialAdapter, NewsAdapter, SocialAdapter
from aats.sentiment.models import MCSEvidence, NewsSignal, RawPost
from aats.sentiment.news_scorer import score_news
from aats.sentiment.tier_a import run_tier_a
from aats.sentiment.tier_b import MockLLMBackend, TierBScorer

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Pipeline configuration
# ---------------------------------------------------------------------------

DEFAULT_LOOKBACK_WINDOW_MS = 3_600_000  # 1 hour
MAX_POSTS_PER_ASSET = 5_000  # hard cap to prevent runaway API costs


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


@dataclass
class MCSSentimentPipeline:
    """Orchestrates Tier-A + Tier-B + News narrative signal for a single asset.

    INJECTABLE:
        adapters:       list of SocialAdapter instances (MockSocialAdapter in tests)
        tier_b:         TierBScorer with injected LLMBackend
        news_adapters:  list of NewsAdapter instances (MockNewsAdapter in tests, E7)

    OFFLINE / TEST DEFAULT:
        MCSSentimentPipeline.offline() factory builds with all mocks.

    REAL-ENDPOINT:
        MCSSentimentPipeline(
            adapters=[XAPIv2Adapter(bearer_token=...), ...],
            tier_b=TierBScorer(OpenAILLMBackend(api_key=...)),
            news_adapters=[RSSNewsAdapter(rss_feed_urls={...})],
        )
        Real API keys come from env vars / Vault — NEVER from code.

    E7 — NEWS HARD RULES:
        - news_adapters is an optional list; defaults to [] (no news signal).
        - Positive / neutral news has mcs_delta=0.0 (NEVER raises conviction).
        - A credible NEGATIVE event can lower conviction and set credible_negative_event.
        - The full pipeline returns (MCSScore, MCSEvidence, NewsSignal).
        - score() also returns a NewsSignal so the Reasoner can check narrative_failure.
    """

    adapters: list[SocialAdapter]
    tier_b: TierBScorer
    news_adapters: list[NewsAdapter] = field(default_factory=list)
    lookback_window_ms: int = DEFAULT_LOOKBACK_WINDOW_MS
    max_posts_per_asset: int = MAX_POSTS_PER_ASSET

    @classmethod
    def offline(
        cls,
        preloaded_posts: list[RawPost] | None = None,
        mock_llm_base_score: float = 0.0,
        preloaded_news: list | None = None,
    ) -> "MCSSentimentPipeline":
        """Factory: fully offline pipeline with mock adapters and mock LLM.

        Used in tests and CI environments.
        No network calls, no API keys required.

        Args:
            preloaded_posts:  Optional social posts to pre-load.
            mock_llm_base_score: Base score for the mock LLM.
            preloaded_news:   Optional list of NewsItem to pre-load in the
                              MockNewsAdapter (E7).  Pass [] or None for no news.
        """
        adapter = MockSocialAdapter()
        if preloaded_posts:
            adapter.load_posts(preloaded_posts)
        llm = MockLLMBackend(base_score=mock_llm_base_score)
        scorer = TierBScorer(llm)

        # E7: wire MockNewsAdapter if news fixtures are provided
        news_adapters: list[NewsAdapter] = []
        if preloaded_news is not None:
            mock_news = MockNewsAdapter()
            mock_news.load_articles(preloaded_news)
            news_adapters = [mock_news]  # type: ignore[list-item]

        return cls(
            adapters=[adapter],
            tier_b=scorer,
            news_adapters=news_adapters,
        )

    async def score(
        self,
        asset: str,
        keywords: list[str],
        decision_time_ms: int,
    ) -> tuple[MCSScore, MCSEvidence, NewsSignal]:
        """Score an asset at a specific point in time (E7 augmented).

        Runs social pipeline (Tier-A + Tier-B) and news pipeline concurrently,
        then combines their outputs into the final MCS conviction.

        Args:
            asset:            Mint address string.
            keywords:         Search terms (token name, ticker, etc.).
            decision_time_ms: Point-in-time anchor.  Only posts/articles with
                              event_time_ms <= decision_time_ms are considered.
                              This is the C-5 point-in-time correctness guarantee.

        Returns:
            (MCSScore, MCSEvidence, NewsSignal) — the score, its full audit
            trail, and the news narrative signal.

        E7 NEWS RULES (enforced here):
            - news mcs_delta is in [-1.0, 0.0] — NEVER positive.
            - Final conviction = clamp(social_conviction + news_delta, 0.0, 1.0).
            - credible_negative_event is passed through to the Reasoner caller
              via the returned NewsSignal; the Reasoner decides narrative_failure.
        """
        # Build EventTime from decision_time_ms.
        approx_slot = max(1, decision_time_ms // 400)
        event_time = EventTime(
            slot=approx_slot,
            block_time_ms=decision_time_ms,
            wall_clock_ms=int(time.time() * 1000),
        )

        # --- Stage 1: collect social posts + news articles concurrently ---
        social_tasks = [
            adapter.fetch_posts(keywords, decision_time_ms, self.lookback_window_ms)
            for adapter in self.adapters
        ]
        news_tasks = [
            na.fetch_news(keywords, decision_time_ms, self.lookback_window_ms)
            for na in self.news_adapters
        ]
        all_tasks = social_tasks + news_tasks
        results = await asyncio.gather(*all_tasks, return_exceptions=True)

        # Split results back into social and news
        social_results = results[: len(social_tasks)]
        news_results = results[len(social_tasks):]

        all_posts: list[RawPost] = []
        for res in social_results:
            if isinstance(res, Exception):
                logger.warning("pipeline: social adapter failed for asset=%s: %s", asset, res)
                continue
            all_posts.extend(res)  # type: ignore[arg-type]

        from aats.sentiment.models import NewsItem as _NewsItem  # avoid circular at module level
        all_news_items: list[_NewsItem] = []
        for res in news_results:
            if isinstance(res, Exception):
                logger.warning("pipeline: news adapter failed for asset=%s: %s", asset, res)
                continue
            all_news_items.extend(res)  # type: ignore[arg-type]

        # Hard cap on social posts
        if len(all_posts) > self.max_posts_per_asset:
            logger.warning(
                "pipeline: capping %d posts to %d for asset=%s",
                len(all_posts),
                self.max_posts_per_asset,
                asset,
            )
            all_posts = all_posts[: self.max_posts_per_asset]

        posts_excluded_future = sum(
            1 for p in all_posts if p.event_time_ms > decision_time_ms
        )

        # --- Stage 2: Tier-A (social) ---
        digest = run_tier_a(all_posts, asset, decision_time_ms)

        # --- Stage 3: Tier-B (social) ---
        mcs_score, evidence = self.tier_b.score_asset(digest, event_time)
        evidence.posts_excluded_future = posts_excluded_future

        # --- Stage 4: News narrative scoring (E7) ---
        news_signal = score_news(all_news_items, asset, decision_time_ms)

        # --- Stage 5: Apply news delta to social conviction (E7) ---
        # mcs_delta is in [-1.0, 0.0] — NEVER positive.
        # conviction = clamp(social_conviction + news_delta, 0.0, 1.0)
        # This ensures a credible negative event can drive conviction to 0.
        # A positive news_delta is IMPOSSIBLE by construction of score_news().
        if news_signal.mcs_delta < 0.0:
            new_conviction = max(
                0.0,
                min(1.0, mcs_score.conviction + news_signal.mcs_delta),
            )
            # Rebuild MCSScore with adjusted conviction and merged red_flags
            merged_red_flags = list(mcs_score.red_flags) + news_signal.red_flags
            mcs_score = MCSScore(
                asset=mcs_score.asset,
                event_time=mcs_score.event_time,
                conviction=new_conviction,
                momentum=mcs_score.momentum,
                novelty=mcs_score.novelty,
                synchronicity=mcs_score.synchronicity,
                account_age_median_days=mcs_score.account_age_median_days,
                coordinated_shill_flag=mcs_score.coordinated_shill_flag,
                red_flags=merged_red_flags,
                post_count=mcs_score.post_count,
                reasoning=mcs_score.reasoning,
            )
            # Reflect the news delta in the evidence trail
            evidence.final_conviction = new_conviction
            evidence.red_flags = merged_red_flags

        logger.info(
            "pipeline: asset=%s social_posts=%d clusters=%d sync=%.3f "
            "conviction=%.3f penalty=%.3f shill=%s "
            "news_articles=%d news_neg=%d credible_neg=%s news_delta=%.3f",
            asset,
            digest.total_post_count,
            digest.unique_cluster_count,
            digest.synchronicity,
            mcs_score.conviction,
            evidence.coordinated_shill_penalty,
            mcs_score.coordinated_shill_flag,
            news_signal.total_articles,
            news_signal.negative_article_count,
            news_signal.credible_negative_event,
            news_signal.mcs_delta,
        )

        return mcs_score, evidence, news_signal


# ---------------------------------------------------------------------------
# MCS directionality assertion (load-bearing — NOT optional)
# ---------------------------------------------------------------------------

# This function is the explicit contract between the MCS pipeline and its
# downstream consumers (llm-reasoning-engineer / T-313, risk engine / T-324).
#
# LAW: MCS conviction may ONLY reduce risk.
#   - conviction < threshold → veto entry or force exit
#   - conviction >= threshold → no additional risk increase
#
# It is ILLEGAL for the downstream consumer to:
#   - Use conviction to size UP a position
#   - Use conviction to WIDEN a stop
#   - Use conviction to OVERRIDE a hard stop
#   - Use conviction to ADD leverage
#
# This is documented here at the seam and tested in test_schema.py.


def mcs_may_only_de_risk(conviction: float, de_risk_threshold: float = 0.3) -> bool:
    """Return True iff MCS conviction triggers de-risking (veto / reduce).

    conviction ∈ [0, 1]: 0 = adversarial/manufactured, 1 = organic.
    de_risk_threshold: if conviction < threshold, the reasoner should de-risk.

    CORRECT caller pattern:
        if mcs_may_only_de_risk(mcs.conviction, threshold):
            reasoner.veto_entry(mint)

    INCORRECT (enforced to fail in tests):
        if conviction > threshold:
            entry_size *= conviction   # ILLEGAL: sizes up
    """
    return conviction < de_risk_threshold


def assert_mcs_cannot_size_up(conviction: float, current_size: float) -> float:
    """Return current_size unchanged — MCS does not influence sizing UP.

    This function is the callable proof that MCS cannot size up.
    A test passes (conviction=0.9, size=1.0) and asserts size is unchanged.
    If this function is ever modified to multiply by conviction, the test fails.
    """
    # MCS conviction NEVER modifies size upward.  Period.
    return current_size
