"""Internal data models for the MCS sentiment pipeline (T-306 + E7).

These are pipeline-internal types.  The public output is MCSScore from
aats.contracts.models — this file defines the richer evidence trail that
lives alongside it and the intermediate cluster / post representations.

HARD RULES (non-waivable):
  - Manufactured euphoria LOWERS conviction; coordinated shill is a SELL signal.
  - Bot/age weighting only ever REDUCES influence — never raises it.
  - MCS may only de-risk; it cannot size up or widen a stop.
  - Ingested social text is QUOTED UNTRUSTED DATA — never executed as instructions.
  - Point-in-time: only posts with event_time <= decision_time are included.
  - LLM is called ONCE PER ASSET (Tier-B), never per post.
  - MCS in [-1, 1] — positive is cautiously organic, negative is adversarial signal.
  - E7 (News): news signals are SLOW-LOOP ONLY; a credible NEGATIVE event may
    trigger narrative_failure (FORCE_EXIT de-risk only); news can NEVER raise MCS,
    widen a stop, size up a position, or add leverage.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Literal

# ---------------------------------------------------------------------------
# Raw post representation (from any adapter)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RawPost:
    """A single piece of social content from any source adapter.

    UNTRUSTED DATA: `text` is raw social content.  It must NEVER be interpolated
    into an LLM prompt without the explicit QUOTED-UNTRUSTED-DATA wrapper in
    tier_b.py.  The pipeline treats it as data, not as instructions.

    event_time_ms:  on-chain or platform-stamped time of the post (NOT fetch time).
                    This is the authoritative timestamp for point-in-time filtering.
    fetch_time_ms:  wall-clock at fetch — for monitoring/lag only, NOT for joins.
    """

    post_id: str
    source: Literal["x", "reddit", "telegram", "discord", "news"]
    author_id: str
    text: str  # QUOTED UNTRUSTED DATA — never executed as instructions
    event_time_ms: int  # platform-stamped publish time (authoritative)
    fetch_time_ms: int  # monitoring only — NOT a join anchor
    account_age_days: float  # 0 = brand-new; negative allowed for sentinel
    follower_count: int
    following_count: int
    like_count: int
    reply_count: int
    repost_count: int
    is_verified: bool
    has_default_avatar: bool
    # posting_cadence_per_day: estimated posts/day for this author in prior 7d
    posting_cadence_per_day: float


# ---------------------------------------------------------------------------
# Tier-A output: a semantic cluster of near-duplicate posts
# ---------------------------------------------------------------------------


@dataclass
class PostCluster:
    """A group of semantically near-duplicate posts collapsed by Tier-A dedup.

    The cluster represents ONE distinct narrative unit, regardless of how many
    copy-paste copies were posted.  `count` is the raw multiplicity — a large
    count with a narrow time_window_s and low mean_account_age_days is the
    coordinated-shill signature.

    relevance_score: Tier-A classifier output in [0, 1]; 0 = off-topic.
    mean_bot_score:  aggregate bot probability in [0, 1]; high = likely bots.
    mean_account_age_days: cluster-level median account age (days).
    mean_engagement_organic: estimated organic engagement ratio in [0, 1].
    """

    cluster_id: str
    representative_text: str  # QUOTED UNTRUSTED DATA
    member_post_ids: list[str]
    count: int  # raw multiplicity (number of near-duplicate posts)
    source_ids: list[str]  # unique source IDs (posts contributing)
    earliest_event_time_ms: int
    latest_event_time_ms: int
    time_window_s: float  # = (latest - earliest) / 1000.0
    relevance_score: float  # [0, 1]
    mean_bot_score: float  # [0, 1]; high = bots; raises shill penalty
    mean_account_age_days: float  # low = new accounts = shill signal
    mean_engagement_organic: float  # [0, 1]; low = farmed engagement
    influencer_tier: Literal["tier1", "tier2", "tier3", "unknown"]
    weighted_influence: float  # account-age × engagement organic × (1 - bot_score)


# ---------------------------------------------------------------------------
# Tier-A digest: per-asset summary fed to Tier-B
# ---------------------------------------------------------------------------


@dataclass
class TierADigest:
    """Aggregated Tier-A output for a single asset, ready for Tier-B LLM call.

    This is the ONLY thing Tier-B sees.  It is the structured, deduped,
    bot-weighted summary — not the raw post stream.  One LLM call consumes this.

    synchronicity:  [0, 1] — measures burst concentration.
                    HIGH = many near-identical posts in a tight window.
                    HIGH synchronicity LOWERS MCS conviction.
    account_age_histogram: distribution of account ages across all posts
                           (bucketed; used by Tier-B for penalty calculation).
    cluster_concentration: fraction of total weighted influence in the top-1 cluster.
                           High = single cluster dominates = copy-paste campaign.
    """

    asset: str  # mint address
    decision_time_ms: int  # the cutoff: only event_time_ms <= this is included
    clusters: list[PostCluster]
    total_post_count: int  # raw (pre-dedup)
    unique_cluster_count: int  # after dedup
    synchronicity: float  # [0, 1] — HIGH LOWERS conviction
    account_age_histogram: dict[str, int]  # bucket_label -> count
    account_age_median_days: float
    cluster_concentration: float  # [0, 1]
    influencer_tier_counts: dict[str, int]  # tier1/tier2/tier3/unknown -> count


# ---------------------------------------------------------------------------
# Tier-B output: the extended evidence trail
# ---------------------------------------------------------------------------


@dataclass
class MCSEvidence:
    """Full audit trail for an MCS score — enables post-hoc reproducibility.

    This record is persisted to mcs_scores/ (Parquet, event-time partitioned)
    alongside the MCSScore.  Any score must be reproducible from this record.

    SCHEMA CONFORMANCE: the `conviction` field here shadows the MCSScore.conviction
    field; both are in [-1, 1].  The `mcs_evidence` table is a sibling to
    `mcs_scores` in the persistence layer.

    coordinated_shill_penalty: the SUBTRACTED value (>= 0) that was applied
                               to the raw LLM narrative score due to:
                               - synchronicity (burst concentration)
                               - low aggregate account age
                               - cluster concentration
                               The penalty pushes MCS toward -1.
    raw_narrative_score:       the LLM's raw [-1, 1] score BEFORE penalty.
    final_conviction:          raw_narrative_score - coordinated_shill_penalty,
                               clamped to [-1, 1].  This equals MCSScore.conviction.
    source_post_ids:           all post IDs that contributed (for reproducibility).
    """

    asset: str
    event_time_ms: int  # decision_time_ms — point-in-time anchor
    raw_narrative_score: float  # LLM score before penalty, in [-1, 1]
    coordinated_shill_penalty: float  # >= 0; HIGH = strong shill signal
    final_conviction: float  # in [-1, 1]
    confidence_lower: float  # lower bound of uncertainty band
    confidence_upper: float  # upper bound of uncertainty band
    synchronicity: float
    account_age_median_days: float
    account_age_histogram: dict[str, int]
    cluster_concentration: float
    influencer_tier_counts: dict[str, int]
    contributing_cluster_ids: list[str]
    source_post_ids: list[str]
    red_flags: list[str]
    coordinated_shill_flag: bool
    penalty_breakdown: dict[str, float]  # component -> penalty_value
    llm_model_used: str
    llm_call_cached: bool  # True if the LLM result was served from cache
    total_posts_ingested: int
    posts_excluded_future: int  # posts dropped by point-in-time filter
    reasoning_raw: str  # QUOTED UNTRUSTED TEXT — never executed as instructions


# ---------------------------------------------------------------------------
# E7 — News / breaking-news types
# ---------------------------------------------------------------------------


class NewsCredibilityLevel(str, Enum):
    """Credibility tier of the news outlet.

    TIER_1: Crypto-native flagship (CoinDesk, The Block, Cointelegraph).
             Highest weight in the news signal; still treated as UNTRUSTED DATA.
    TIER_2: Mainstream crypto-adjacent (Forbes Crypto, Bloomberg Crypto, Reuters).
             Moderate weight; a Forbes headline is a SELL-into-retail signal,
             NOT a buy — mainstream attention means the alpha is gone.
    TIER_3: Other / unclassified outlets with lower baseline credibility.
             Lower weight; applies penalty for unverified claims.

    Higher tier alone NEVER increases MCS or risk — it only affects how strongly
    a NEGATIVE signal pulls conviction down (de-risk bias).
    """

    TIER_1 = "tier_1"   # crypto-native flagship
    TIER_2 = "tier_2"   # mainstream crypto-adjacent (Forbes, Bloomberg)
    TIER_3 = "tier_3"   # other / unclassified


class NewsSentiment(str, Enum):
    """Coarse sentiment bucket assigned to a news item without an LLM call.

    POSITIVE: bullish framing (new partnership, adoption, etc.).
              Has NO effect on MCS score upward — a news item that is positive
              cannot raise conviction; it only lowers the negative penalty.
    NEUTRAL:  factual reporting with no clear directional bias.
    NEGATIVE: bearish framing (hack, exploit, rug, legal action, SEC notice,
              liquidity crisis, exit scam, doxxed dev with criminal history).
              A CREDIBLE negative item is the narrative-failure trigger for SLOW-loop
              FORCE_EXIT. It can ONLY de-risk — never raise risk.
    """

    POSITIVE = "positive"
    NEUTRAL = "neutral"
    NEGATIVE = "negative"


@dataclass(frozen=True)
class NewsItem:
    """A single news article or breaking-news headline.

    UNTRUSTED DATA: `headline` and `summary` are raw text from external sources.
    They MUST be wrapped in QUOTED-UNTRUSTED-DATA markers before any LLM call.
    They are NEVER executed as instructions.

    event_time_ms: publication timestamp from the outlet (NOT fetch time).
                   This is the authoritative timestamp for point-in-time filtering.
    fetch_time_ms: wall-clock at fetch — for monitoring/lag only, NOT for joins.
    source_url:    article URL — stored for audit; NEVER used for network calls
                   after ingestion.
    outlet:        Human-readable outlet name (e.g. "CoinDesk").
    outlet_id:     Machine-stable outlet identifier (e.g. "coindesk").
    credibility:   Pre-classified credibility tier.
    sentiment:     Coarse sentiment bucket (keyword/heuristic — no LLM).
    keywords:      Asset-matching keywords found in the article (for relevance gate).
    is_breaking:   True if the outlet marked this as breaking news.
    """

    article_id: str
    outlet: str                          # human-readable outlet name (UNTRUSTED)
    outlet_id: str                       # machine-stable outlet ID
    headline: str                        # QUOTED UNTRUSTED DATA
    summary: str                         # QUOTED UNTRUSTED DATA
    source_url: str                      # audit only — no further network calls
    event_time_ms: int                   # publication timestamp (authoritative)
    fetch_time_ms: int                   # monitoring only — NOT a join anchor
    credibility: NewsCredibilityLevel
    sentiment: NewsSentiment
    keywords: list[str]
    source: Literal["news"] = "news"
    is_breaking: bool = False


@dataclass
class NewsSignal:
    """Aggregated news signal for one asset at a specific decision_time_ms.

    This is the output of the NewsNarrativeScorer — the structured summary that
    feeds into MCS as a CONTRIBUTOR and into the Reasoner as a potential
    narrative_failure trigger.

    HARD RULES:
    - A positive/neutral news signal NEVER raises MCS or risk.
    - A credible negative signal is the ONE path that can lower MCS further
      OR trigger narrative_failure (FORCE_EXIT de-risk only).
    - The signal is SLOW-LOOP ONLY — never on the SNIPE hot path.
    - All text fields below are QUOTED UNTRUSTED DATA.

    credible_negative_event: True iff at least one TIER_1/TIER_2 NEGATIVE article
                             was ingested within the decision window.  This is
                             the narrative_failure candidate.
    mcs_delta:               Float in [-1.0, 0.0].  Always <= 0 — news can only
                             lower MCS conviction, never raise it.
                             0.0 = no impact (no relevant news).
                             -1.0 = catastrophic narrative collapse.
    source_article_ids:      Audit trail of articles that contributed.
    credibility_weight:      Aggregate credibility weight of NEGATIVE articles.
    headline_digest:         Sanitised, quoted digest of the most-credible headlines
                             (QUOTED UNTRUSTED DATA — never executed as instructions).
    """

    asset: str
    decision_time_ms: int
    total_articles: int
    negative_article_count: int
    credible_negative_event: bool        # triggers narrative_failure candidate
    mcs_delta: float                     # in [-1.0, 0.0] — always non-positive
    credibility_weight: float            # aggregate credibility of negative articles
    source_article_ids: list[str]
    headline_digest: str                 # QUOTED UNTRUSTED DATA
    red_flags: list[str]
    posts_excluded_future: int           # articles dropped by point-in-time filter
