"""MCS sentiment pipeline orchestrator (T-306 + E7).

Ties Tier-A + Tier-B + News narrative signal into a single async call per asset.

SLOW-LOOP ONLY — this module must NEVER be imported by or called from the
SNIPE loop or FAST loop.  The SNIPE loop's millisecond entry decision MUST
NOT block on social data.  Enforce this boundary:
  - This module has no Redis Stream writer for the SNIPE loop path.
  - MCS is a SLOW-loop output consumed by the SLOW-loop Reasoner (T-313).
  - If a task asks to use MCS in sizing or snipe trigger, REFUSE and flag.

PIPELINE EXECUTION (E7 + EN2a augmented):
  1. Collect posts from all configured social adapters (X / Reddit / Telegram /
     Discord) AND news articles from all configured NewsAdapters — concurrently.
  2. Run Tier-A on social posts: filter, embed, dedup, score, cluster.
  3. Run NewsNarrativeScorer on news articles → NewsSignal.
     The NewsSignal's mcs_delta is in [-1.0, 0.0] — news NEVER raises conviction.
  4. Run Tier-B: batch LLM call per asset on the social digest → MCSScore.
  5. Apply the news delta to the social MCS conviction (additively, clamped to [0,1]).
     A credible negative event is recorded in the evidence for the Reasoner.
  6. Run VelocitySignalComputer (E10) on the SAME RawPosts already ingested in
     step 1 — NO external data dependency, no new adapter, no network call.
     Apply signal.mcs_penalty to conviction the SAME additive-clamped way the
     news delta is applied in step 5 (subtractive here since mcs_penalty >= 0).
  7. Apply the global MCS directionality rule:
       MCS may ONLY reduce risk (veto/gate) — NEVER size up or widen a stop.
  8. Return (MCSScore, MCSEvidence, NewsSignal, VelocitySignal).

NEWS HARD RULES (E7):
  - Positive / neutral news contributes mcs_delta = 0.0 (no upward effect).
  - Credible negative news (TIER_1/TIER_2 outlet) sets credible_negative_event=True.
  - The pipeline EXPOSES the NewsSignal to the Reasoner for narrative_failure hook.
  - The Reasoner uses narrative_failure → FORCE_EXIT (de-risk only).
  - News is SLOW-LOOP ONLY — never read by SNIPE path.

VELOCITY SIDECAR HARD RULES (EN2a — wires the previously-dormant E10 module,
now backed by the EN5 `SocialAdapterVelocitySource` production source):
  - VelocitySignal.mcs_penalty is ALWAYS >= 0.0 (proven in tests/sentiment/test_velocity.py);
    wiring it in only ever SUBTRACTS from conviction, never adds.
  - Built entirely from RawPosts this pipeline call already fetched, via
    `SocialAdapterVelocitySource(posts=all_posts)` — the single source of truth
    for the RawPost -> VelocityEvent mapping (aats/sentiment/velocity.py). No
    recorded outcomes, no extra adapter, no extra network/LLM call.
  - apply_velocity_penalty_to_conviction() (the DEF-E10-01 hardened helper) is
    reused as-is: it raises ValueError on a forged negative penalty and clamps
    defensively, so this wiring cannot accidentally raise conviction.
  - VelocitySignal does NOT change the frozen MCSScore contract shape — it is a
    sidecar return value, exactly like NewsSignal.

COST GUARD:
  - Social adapters are cached by (asset, window) at the pipeline level.
  - News adapters are cached by (asset, window) similarly.
  - Tier-B caches by content hash at the scorer level.
  - Budget remaining is tracked and enforced.
  - The velocity sidecar issues ZERO additional network/LLM calls (posts reused).
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
from aats.sentiment.velocity import (
    SocialAdapterVelocitySource,
    VelocitySignal,
    VelocitySignalComputer,
    apply_velocity_penalty_to_conviction,
)

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
    ) -> MCSSentimentPipeline:
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
    ) -> tuple[MCSScore, MCSEvidence, NewsSignal, VelocitySignal]:
        """Score an asset at a specific point in time (E7 + EN2a augmented).

        Runs social pipeline (Tier-A + Tier-B), news pipeline, and the velocity
        sidecar (over the same ingested posts) then combines their outputs into
        the final MCS conviction.

        Args:
            asset:            Mint address string.
            keywords:         Search terms (token name, ticker, etc.).
            decision_time_ms: Point-in-time anchor.  Only posts/articles with
                              event_time_ms <= decision_time_ms are considered.
                              This is the C-5 point-in-time correctness guarantee.

        Returns:
            (MCSScore, MCSEvidence, NewsSignal, VelocitySignal) — the score, its
            full audit trail, the news narrative signal, and the social-velocity
            / bot-ratio sidecar signal (EN2a). VelocitySignal is an ADDITIONAL
            sidecar output; it does not alter the frozen MCSScore contract shape.

        E7 NEWS RULES (enforced here):
            - news mcs_delta is in [-1.0, 0.0] — NEVER positive.
            - Final conviction = clamp(social_conviction + news_delta, 0.0, 1.0).
            - credible_negative_event is passed through to the Reasoner caller
              via the returned NewsSignal; the Reasoner decides narrative_failure.

        EN2a VELOCITY RULES (enforced here):
            - velocity.mcs_penalty is in [0.0, 1.0] — NEVER negative.
            - Applied AFTER the news delta, via the SAME additive-clamped pattern:
              new_conviction = clamp(conviction - velocity.mcs_penalty, 0.0, 1.0),
              using the hardened apply_velocity_penalty_to_conviction() helper.
            - No external data dependency: built from the RawPosts already
              fetched in Stage 1 above — zero extra network/LLM calls.
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

        # --- Stage 6: Velocity sidecar (EN2a, backed by the EN5 production source) ---
        # NO EXTERNAL DATA DEPENDENCY: SocialAdapterVelocitySource reshapes `all_posts`,
        # the SAME RawPosts already fetched in Stage 1 / scored by Tier-A in Stage 2 —
        # it performs zero additional fetch/network/LLM call of its own (see
        # aats/sentiment/velocity.py). This is the single source of truth for the
        # RawPost -> VelocityEvent mapping (including is_bot_scored via the Tier-A
        # bot scorer) — the pipeline no longer keeps its own inline copy of it.
        velocity_source = SocialAdapterVelocitySource(posts=all_posts)
        velocity_signal = VelocitySignalComputer(source=velocity_source).compute(
            asset, decision_time_ms
        )

        # --- Stage 7: Apply velocity penalty to social conviction (EN2a) ---
        # SAME additive-clamped pattern as the news delta in Stage 5 above:
        #   new_conviction = clamp(conviction - velocity.mcs_penalty, 0.0, 1.0)
        # (subtractive here because VelocitySignal.mcs_penalty >= 0 by construction,
        # whereas NewsSignal.mcs_delta <= 0 by construction — both stages can only
        # LOWER conviction, never raise it).
        # apply_velocity_penalty_to_conviction() is the DEF-E10-01 hardened helper:
        # it raises ValueError on a forged negative penalty and clamps defensively,
        # so this wiring cannot accidentally raise conviction even under misuse.
        if velocity_signal.mcs_penalty > 0.0:
            new_conviction = apply_velocity_penalty_to_conviction(
                mcs_score.conviction, velocity_signal
            )
            # Rebuild MCSScore with adjusted conviction and merged red_flags
            merged_red_flags = list(mcs_score.red_flags) + velocity_signal.red_flags
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
            # Reflect the velocity penalty in the evidence trail
            evidence.final_conviction = new_conviction
            evidence.red_flags = merged_red_flags

        logger.info(
            "pipeline: asset=%s social_posts=%d clusters=%d sync=%.3f "
            "conviction=%.3f penalty=%.3f shill=%s "
            "news_articles=%d news_neg=%d credible_neg=%s news_delta=%.3f "
            "velocity_penalty=%.3f velocity_bot_frac=%.2f velocity_cohort=%s",
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
            velocity_signal.mcs_penalty,
            velocity_signal.bot_account_fraction,
            velocity_signal.account_age_cohort_flag,
        )

        return mcs_score, evidence, news_signal, velocity_signal


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
