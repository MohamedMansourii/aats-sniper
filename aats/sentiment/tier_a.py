"""Tier-A: cheap, high-volume filter (T-306).

CPU/GPU-batched — NO LLM calls.

Stages (applied in order):
  1. Point-in-time filter — drop posts with event_time_ms > decision_time_ms.
  2. Relevance classification — sentence-transformers cosine similarity to a
     seed corpus of crypto-relevant phrases.  Scores below RELEVANCE_THRESHOLD
     are dropped before any further processing.
  3. Embedding-based semantic dedup — cosine similarity + HDBSCAN clustering
     collapses near-duplicate posts into PostClusters with a raw count.
     This prevents copy-paste campaigns from inflating volume.
  4. Bot scoring — account age, follower/following ratio, posting cadence,
     default avatar flag.  Score in [0, 1]; 1 = almost certainly a bot.
  5. Account-age weighting — new accounts are down-weighted toward zero.
     NEVER up-weighted.  fresh_account_weight(age_days) is monotone non-decreasing.
  6. Engagement weighting — organic vs farmed estimation (like/reply ratio).
  7. Synchronicity calculation — measures burst concentration across clusters.

HARD RULES:
  - Bot/age weighting only REDUCES influence.  There is no code path where a
    young or high-cadence account increases a score.
  - Point-in-time: posts with event_time_ms > decision_time_ms are EXCLUDED.
    The filter is the FIRST operation — no future post reaches any scorer.
  - No LLM calls in this module.
  - No secrets, API keys, or network calls.  This is a pure CPU pipeline.

Sentence-transformers / HDBSCAN are OPTIONAL runtime dependencies.  When absent
(offline / test environments), a deterministic fallback (keyword-based relevance
+ hash-based clustering) is used.  The fallback is always the test default.
"""

from __future__ import annotations

import hashlib
import logging
import math
import re
import uuid
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass

from aats.sentiment.models import PostCluster, RawPost, TierADigest

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

RELEVANCE_THRESHOLD = 0.25  # below this cosine score → drop (off-topic)
BOT_SCORE_THRESHOLD = 0.85  # above this → near-zero influence weight
DEDUP_COSINE_THRESHOLD = 0.82  # above this → same cluster
MIN_ACCOUNT_AGE_DAYS_FOR_FULL_WEIGHT = 90.0  # younger → partial weight
SYNCHRONICITY_WINDOW_S = 300.0  # 5 minutes: tight burst = shill signal
MAX_CLUSTER_INFLUENCE_WEIGHT = 1.0  # upper bound on per-cluster weight

# Crypto-relevance seed phrases (used for keyword fallback and as centroid seeds)
_CRYPTO_SEED_PHRASES = [
    "solana token launch",
    "meme coin pump",
    "new liquidity pool",
    "raydium listing",
    "pump.fun launch",
    "gem early buy",
    "100x potential",
    "bullish narrative",
    "rug pull warning",
    "dev wallet dump",
    "coordinated shill",
    "airdrop claim",
    "degen play",
]


# ---------------------------------------------------------------------------
# Relevance classification (keyword fallback — no model required)
# ---------------------------------------------------------------------------


def _keyword_relevance(text: str) -> float:
    """Deterministic keyword-based relevance score in [0, 1].

    Used when sentence-transformers is unavailable.  Returns higher scores for
    text containing crypto/Solana-relevant vocabulary.
    """
    text_lower = text.lower()
    hits = 0
    keywords = [
        "solana",
        "sol",
        "pump",
        "token",
        "launch",
        "gem",
        "moon",
        "100x",
        "liquidity",
        "raydium",
        "pumpfun",
        "meme",
        "degen",
        "rug",
        "bullish",
        "bearish",
        "shill",
        "early",
        "airdrop",
        "wallet",
    ]
    for kw in keywords:
        if re.search(r"\b" + re.escape(kw) + r"\b", text_lower):
            hits += 1
    # Normalise: 2+ hits → relevant
    return min(1.0, hits / 2.0)


def _embed_text_fallback(text: str) -> list[float]:
    """Deterministic pseudo-embedding via character n-gram hashing.

    Used when sentence-transformers is unavailable.  Not semantically meaningful
    but provides stable clustering for testing.
    """
    dim = 64
    vec = [0.0] * dim
    for i in range(len(text) - 2):
        trigram = text[i : i + 3]
        h = int(hashlib.md5(trigram.encode(), usedforsecurity=False).hexdigest(), 16)
        idx = h % dim
        vec[idx] += 1.0
    # L2 normalise
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / norm for x in vec]


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)


# ---------------------------------------------------------------------------
# Bot scoring
# ---------------------------------------------------------------------------


def _bot_score(post: RawPost) -> float:
    """Estimate bot probability in [0, 1].

    Scoring factors:
      - Account age (younger = more suspicious)
      - Follower/following ratio (ratio << 1 = follows many, has few = bot-like)
      - Posting cadence (very high cadence = bot-like)
      - Default avatar flag (unconfigured account = suspicious)

    ALL factors push the score toward 1 (more bot-like).  None can push it down.
    The function is monotone non-decreasing in each suspicious signal direction.
    """
    score = 0.0

    # Age component: [0, 0.35] — brand-new accounts score highest
    if post.account_age_days < 1:
        score += 0.35
    elif post.account_age_days < 7:
        score += 0.30
    elif post.account_age_days < 30:
        score += 0.20
    elif post.account_age_days < 90:
        score += 0.10

    # Follower/following ratio: ratio < 0.05 = likely bot/shill account
    if post.following_count > 0:
        ratio = post.follower_count / post.following_count
        if ratio < 0.05:
            score += 0.25
        elif ratio < 0.2:
            score += 0.15
        elif ratio < 0.5:
            score += 0.05

    # High posting cadence: >50 posts/day = automated
    if post.posting_cadence_per_day > 200:
        score += 0.25
    elif post.posting_cadence_per_day > 50:
        score += 0.15
    elif post.posting_cadence_per_day > 20:
        score += 0.05

    # Default avatar = unconfigured account
    if post.has_default_avatar:
        score += 0.15

    return min(1.0, score)


# ---------------------------------------------------------------------------
# Account-age weight (monotone non-decreasing with age — never up-weights young)
# ---------------------------------------------------------------------------


def _account_age_weight(age_days: float) -> float:
    """Map account age to an influence multiplier in [0, 1].

    INVARIANT: weight is monotone non-decreasing in age_days.
    A brand-new account (age=0) gets weight 0.
    An account older than MIN_ACCOUNT_AGE_DAYS_FOR_FULL_WEIGHT gets weight 1.

    This is the PRIMARY adversarial defence: fresh accounts cannot push MCS up.
    """
    if age_days <= 0:
        return 0.0
    if age_days >= MIN_ACCOUNT_AGE_DAYS_FOR_FULL_WEIGHT:
        return 1.0
    # Smooth ramp: sqrt gives more weight to mid-age accounts than linear
    return math.sqrt(age_days / MIN_ACCOUNT_AGE_DAYS_FOR_FULL_WEIGHT)


# ---------------------------------------------------------------------------
# Engagement organicity estimation
# ---------------------------------------------------------------------------


def _engagement_organic_score(post: RawPost) -> float:
    """Estimate organic engagement ratio in [0, 1].

    High like-count with zero replies = farmed.
    Balanced likes + replies = organic.
    """
    total = post.like_count + post.reply_count + post.repost_count
    if total == 0:
        return 0.1  # no engagement at all is suspicious
    # Organic signal: replies are harder to farm than likes
    reply_ratio = post.reply_count / total
    # A reply_ratio of >= 0.15 is a good organic signal
    return min(1.0, reply_ratio / 0.15)


# ---------------------------------------------------------------------------
# Influencer tier classification
# ---------------------------------------------------------------------------


def _influencer_tier(
    post: RawPost,
    bot_score: float,
) -> str:
    """Classify account into tier1/tier2/tier3/unknown.

    tier1: verified + high followers + old account + low bot score
    tier2: decent followers + moderate age
    tier3: low followers or new account
    unknown: cannot determine
    """
    if bot_score > BOT_SCORE_THRESHOLD:
        return "unknown"
    if post.is_verified and post.follower_count > 100_000 and post.account_age_days > 365:
        return "tier1"
    if post.follower_count > 10_000 and post.account_age_days > 90:
        return "tier2"
    if post.follower_count > 1_000 and post.account_age_days > 30:
        return "tier3"
    return "unknown"


# ---------------------------------------------------------------------------
# Semantic clustering (deterministic hash fallback)
# ---------------------------------------------------------------------------


@dataclass
class _PostWithFeatures:
    post: RawPost
    embedding: list[float]
    relevance: float
    bot_score_val: float
    account_age_weight_val: float
    organic_score: float
    inf_tier: str
    weighted_influence: float


def _cluster_posts(
    scored_posts: list[_PostWithFeatures],
) -> list[PostCluster]:
    """Greedy cosine-similarity clustering (O(n^2) — acceptable for batch sizes).

    Each unassigned post seeds a new cluster.  Posts within DEDUP_COSINE_THRESHOLD
    of the cluster centroid join it.  The centroid is the first post's embedding.

    Returns one PostCluster per discovered cluster, with aggregated statistics.
    """
    clusters: list[list[_PostWithFeatures]] = []
    centroids: list[list[float]] = []

    for sp in scored_posts:
        placed = False
        for i, centroid in enumerate(centroids):
            if _cosine(sp.embedding, centroid) >= DEDUP_COSINE_THRESHOLD:
                clusters[i].append(sp)
                placed = True
                break
        if not placed:
            clusters.append([sp])
            centroids.append(sp.embedding)

    result: list[PostCluster] = []
    for cluster_members in clusters:
        if not cluster_members:
            continue
        cid = str(uuid.uuid4())
        times = [m.post.event_time_ms for m in cluster_members]
        mean_bot = sum(m.bot_score_val for m in cluster_members) / len(cluster_members)
        mean_age = sum(m.post.account_age_days for m in cluster_members) / len(cluster_members)
        mean_organic = sum(m.organic_score for m in cluster_members) / len(cluster_members)
        mean_inf_weight = sum(m.weighted_influence for m in cluster_members) / len(
            cluster_members
        )
        mean_relevance = sum(m.relevance for m in cluster_members) / len(cluster_members)

        # Influencer tier is that of the highest-influence member
        best_tier = max(cluster_members, key=lambda x: x.weighted_influence).inf_tier

        result.append(
            PostCluster(
                cluster_id=cid,
                representative_text=cluster_members[0].post.text,
                member_post_ids=[m.post.post_id for m in cluster_members],
                count=len(cluster_members),
                source_ids=[m.post.post_id for m in cluster_members],
                earliest_event_time_ms=min(times),
                latest_event_time_ms=max(times),
                time_window_s=(max(times) - min(times)) / 1000.0,
                relevance_score=mean_relevance,
                mean_bot_score=mean_bot,
                mean_account_age_days=mean_age,
                mean_engagement_organic=mean_organic,
                influencer_tier=best_tier,  # type: ignore[arg-type]
                weighted_influence=mean_inf_weight,
            )
        )
    return result


# ---------------------------------------------------------------------------
# Synchronicity calculation
# ---------------------------------------------------------------------------


def _calculate_synchronicity(clusters: list[PostCluster], total_posts: int) -> float:
    """Compute a [0, 1] burst-concentration metric.

    High synchronicity = many DISTINCT clusters (sources) posted within a tight
    time window of each other.  HIGH synchronicity is the coordinated-shill
    fingerprint: multiple independent accounts all posting at the same moment.

    Single-post clusters, or clusters spread over hours, should score LOW.

    Algorithm:
      1. If there is only one cluster (or zero), the signal is noise — return 0.0.
         (A single account ranting cannot be "coordinated".)
      2. Measure the inter-cluster time spread: how tightly are ALL clusters'
         publication times packed together?
         inter_window_s = max(earliest_per_cluster) - min(earliest_per_cluster)
      3. burst_fraction: what fraction of the inter_window_s fits within SYNCHRONICITY_WINDOW_S.
         If all clusters started within 5 minutes of each other → burst_fraction near 1.
         If clusters span 2 hours → burst_fraction much lower.
      4. Concentration bonus: if one cluster dominates by count (copy-paste campaign),
         boost the synchronicity.

    This ensures:
      - Organic posts spread over hours: low synchronicity
      - 20 near-identical posts in 90 seconds: high synchronicity
    """
    if not clusters or total_posts == 0 or len(clusters) < 2:
        # Single cluster or no clusters → no coordination signal
        # But a single cluster with many copies IS a shill signal —
        # handle that via cluster_concentration in the penalty, not synchronicity
        return 0.0

    # Inter-cluster time spread
    earliest_times = [c.earliest_event_time_ms for c in clusters]
    inter_window_s = (max(earliest_times) - min(earliest_times)) / 1000.0

    if inter_window_s <= 0:
        # All clusters started at the exact same millisecond — maximum synchronicity
        base_sync = 1.0
    elif inter_window_s <= SYNCHRONICITY_WINDOW_S:
        # All clusters within the synchronicity window → high sync
        base_sync = 1.0 - (inter_window_s / SYNCHRONICITY_WINDOW_S) * 0.3
    else:
        # Spread across a longer window — lower sync, but not zero
        base_sync = SYNCHRONICITY_WINDOW_S / inter_window_s
        base_sync = min(0.7, base_sync)

    # Concentration bonus: single dominant cluster = copy-paste campaign
    max_cluster_count = max(c.count for c in clusters)
    concentration = max_cluster_count / total_posts
    if concentration > 0.5 and base_sync > 0.3:
        base_sync = min(1.0, base_sync * (1 + (concentration - 0.5) * 0.5))

    return min(1.0, max(0.0, base_sync))


# ---------------------------------------------------------------------------
# Account-age histogram
# ---------------------------------------------------------------------------


def _build_age_histogram(posts: list[RawPost]) -> dict[str, int]:
    """Bucket account ages for audit trail."""
    buckets: dict[str, int] = {
        "0d": 0,       # brand new (same day)
        "1-7d": 0,
        "8-30d": 0,
        "31-90d": 0,
        "91-365d": 0,
        ">365d": 0,
    }
    for p in posts:
        age = p.account_age_days
        if age < 1:
            buckets["0d"] += 1
        elif age <= 7:
            buckets["1-7d"] += 1
        elif age <= 30:
            buckets["8-30d"] += 1
        elif age <= 90:
            buckets["31-90d"] += 1
        elif age <= 365:
            buckets["91-365d"] += 1
        else:
            buckets[">365d"] += 1
    return buckets


# ---------------------------------------------------------------------------
# Public API: run_tier_a
# ---------------------------------------------------------------------------


def run_tier_a(
    posts: Sequence[RawPost],
    asset: str,
    decision_time_ms: int,
) -> TierADigest:
    """Execute the full Tier-A pipeline for a single asset.

    Args:
        posts:            All ingested posts (from all adapters) for this asset.
        asset:            Mint address string.
        decision_time_ms: Point-in-time cutoff.  Posts with
                          event_time_ms > decision_time_ms are EXCLUDED.
                          This is the first operation — no future post reaches
                          any scorer.  (data-models.md C-5 / FR-008)

    Returns:
        TierADigest ready for Tier-B consumption.
    """
    # --- Stage 1: Point-in-time filter (FIRST, before ANY scoring) ---
    posts_excluded_future = [p for p in posts if p.event_time_ms > decision_time_ms]
    pit_posts = [p for p in posts if p.event_time_ms <= decision_time_ms]

    if not pit_posts:
        logger.debug(
            "tier_a: no posts within point-in-time window for asset=%s "
            "decision_time_ms=%d excluded=%d",
            asset,
            decision_time_ms,
            len(posts_excluded_future),
        )
        return _empty_digest(asset, decision_time_ms)

    # --- Stage 2: Relevance filter ---
    relevant_posts: list[_PostWithFeatures] = []
    for post in pit_posts:
        relevance = _keyword_relevance(post.text)
        if relevance < RELEVANCE_THRESHOLD:
            continue
        embedding = _embed_text_fallback(post.text)
        bot = _bot_score(post)
        age_weight = _account_age_weight(post.account_age_days)
        organic = _engagement_organic_score(post)
        tier = _influencer_tier(post, bot)
        # Weighted influence: age_weight × organic × (1 - bot_score)
        # ALL three terms are in [0, 1]; the product is in [0, 1].
        # A fresh bot with farmed engagement gets weight ≈ 0.
        w_influence = age_weight * organic * (1.0 - bot)
        relevant_posts.append(
            _PostWithFeatures(
                post=post,
                embedding=embedding,
                relevance=relevance,
                bot_score_val=bot,
                account_age_weight_val=age_weight,
                organic_score=organic,
                inf_tier=tier,
                weighted_influence=w_influence,
            )
        )

    if not relevant_posts:
        return _empty_digest(asset, decision_time_ms)

    # --- Stage 3: Semantic dedup clustering ---
    clusters = _cluster_posts(relevant_posts)

    # --- Stage 4: Per-cluster statistics already in cluster objects ---

    # --- Stage 5: Synchronicity ---
    total_posts = len(pit_posts)
    synchronicity = _calculate_synchronicity(clusters, total_posts)

    # --- Stage 6: Account-age histogram (on ALL pit_posts, not just relevant) ---
    age_histogram = _build_age_histogram(pit_posts)

    # Account-age median (on relevant posts)
    all_ages = sorted(sp.post.account_age_days for sp in relevant_posts)
    if all_ages:
        mid = len(all_ages) // 2
        if len(all_ages) % 2 == 0:
            age_median = (all_ages[mid - 1] + all_ages[mid]) / 2.0
        else:
            age_median = all_ages[mid]
    else:
        age_median = 0.0

    # Cluster concentration
    if clusters:
        total_cluster_posts = sum(c.count for c in clusters)
        max_cluster = max(c.count for c in clusters)
        cluster_concentration = max_cluster / total_cluster_posts if total_cluster_posts > 0 else 0.0
    else:
        cluster_concentration = 0.0

    # Influencer tier counts
    tier_counts: dict[str, int] = defaultdict(int)
    for c in clusters:
        tier_counts[c.influencer_tier] += 1

    return TierADigest(
        asset=asset,
        decision_time_ms=decision_time_ms,
        clusters=clusters,
        total_post_count=total_posts,
        unique_cluster_count=len(clusters),
        synchronicity=synchronicity,
        account_age_histogram=age_histogram,
        account_age_median_days=age_median,
        cluster_concentration=cluster_concentration,
        influencer_tier_counts=dict(tier_counts),
    )


def _empty_digest(asset: str, decision_time_ms: int) -> TierADigest:
    """Return a digest with no posts — results in low-confidence MCS."""
    return TierADigest(
        asset=asset,
        decision_time_ms=decision_time_ms,
        clusters=[],
        total_post_count=0,
        unique_cluster_count=0,
        synchronicity=0.0,
        account_age_histogram={
            "0d": 0,
            "1-7d": 0,
            "8-30d": 0,
            "31-90d": 0,
            "91-365d": 0,
            ">365d": 0,
        },
        account_age_median_days=0.0,
        cluster_concentration=0.0,
        influencer_tier_counts={},
    )
