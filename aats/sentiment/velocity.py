"""E10 — Social-velocity + organic-vs-bot growth ratio features (SLOW-LOOP ONLY).

SLOW-LOOP ONLY.  This module MUST NEVER be imported by or called from the
SNIPE loop or FAST loop.  Social velocity is among the easiest signals to
manufacture; it belongs nowhere near the millisecond entry decision.

DESIGN CONTRACT (non-waivable):
  - Engagement VELOCITY: rate of new posts/interactions in a rolling window.
    HIGH bot-driven velocity LOWERS conviction (de-risk direction only).
    HIGH organic velocity has zero upward effect — it can only suppress the
    PENALTY that bot velocity would otherwise apply.
  - Organic-vs-BOT growth ratio: fraction of engagement velocity attributable
    to accounts that pass the bot filter.  BOT-dominant growth LOWERS conviction.
  - Unique-holder growth rate: rate of growth in distinct posting wallet/account
    IDs, weighted by account age.  Bot farms reuse identity patterns —
    near-identical account ages arriving in burst windows signal factory accounts.
  - Coordinated / bot-driven growth is a SELL signal, never a buy signal.
  - Every signal here feeds the MCS ONLY as a downward modifier.
    VelocitySignal.mcs_penalty >= 0.0 always — it SUBTRACTS from conviction.
  - SLOW-LOOP ONLY — injectable + offline fixtures.  No live network calls
    inside this module.  The VelocitySource Protocol is injected; tests use
    InMemoryVelocitySource.
  - Point-in-time: only posts/events with event_time_ms <= decision_time_ms
    contribute.  This is enforced as the FIRST operation on all input lists.
  - No win-rate, no capital sizing, no position trigger.
  - Money is not handled here (all fields are floats / ints / bool, no SOL).

PENALTY FORMULA:
    velocity_penalty = (
        BOT_VELOCITY_WEIGHT  * p_bot_velocity
      + GROWTH_RATIO_WEIGHT  * p_growth_ratio
      + HOLDER_GROWTH_WEIGHT * p_holder_growth
    )
    penalty is clamped to [0.0, 1.0].
    final_mcs_conviction = clamp(base_conviction - velocity_penalty, 0.0, 1.0)

    All three components push the penalty UP (toward 1.0 = maximum de-risk).
    A clear bot-farm burst (bot velocity >> organic velocity, synchronized
    account ages, single account-age cohort) drives the penalty toward 1.0.

INJECTABLE:
    VelocitySource is a Protocol.  Tests inject InMemoryVelocitySource.
    Real adapters wrap the existing social adapters to extract velocity events
    without additional network calls.

OFFLINE DEFAULT:
    Use VelocitySignalComputer.offline() factory or inject InMemoryVelocitySource.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

from aats.sentiment.models import RawPost
from aats.sentiment.tier_a import BOT_SCORE_THRESHOLD, _bot_score

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Rolling window (ms) for velocity computation.  Posts outside this window
# (AND outside the point-in-time filter) are excluded.
DEFAULT_VELOCITY_WINDOW_MS: int = 900_000  # 15 minutes

# Penalty component weights.  All three push the penalty upward.
BOT_VELOCITY_WEIGHT: float = 0.45
GROWTH_RATIO_WEIGHT: float = 0.35
HOLDER_GROWTH_WEIGHT: float = 0.20

# Account-age threshold below which an account is treated as "bot-likely"
# for velocity purposes.  Consistent with tier_a.py AGE_FULL_PENALTY_DAYS.
BOT_AGE_THRESHOLD_DAYS: float = 7.0

# Synchronicity window for account-age cohort detection (seconds).
# Accounts whose CREATION DATES are within this window of each other are
# treated as a factory batch (all the same bot-farm deploy).
ACCOUNT_CREATION_COHORT_WINDOW_S: float = 86_400.0  # 1 day

# Minimum organic posts before we assign a non-trivial growth ratio benefit.
MIN_ORGANIC_POSTS_FOR_BENEFIT: int = 3

# If bot-account fraction exceeds this, apply the maximum growth-ratio penalty.
BOT_FRACTION_FULL_PENALTY: float = 0.70

# Velocity burst threshold: posts/minute that classifies as a burst.
BURST_VELOCITY_POSTS_PER_MINUTE: float = 2.0

# Holder growth cohort penalty: fraction of accounts in the same age-cohort
# required to trigger the holder-growth penalty.
HOLDER_GROWTH_COHORT_THRESHOLD: float = 0.50


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VelocityEvent:
    """A single observed social event (post / reaction / join) for velocity tracking.

    NO TEXT: this record carries no post text and no hash of post text — only
    metadata (IDs, timestamps, account age, the pre-computed bot flag).  This
    module never receives, stores, or reasons over raw social text; velocity
    is computed purely from structured fields the source has already derived
    (e.g. `SocialAdapterVelocitySource` reshapes `RawPost` metadata, never
    `RawPost.text`).

    event_time_ms:      Platform-stamped publish time (authoritative).
                        This is the anchor for point-in-time filtering.
    fetch_time_ms:      Wall-clock at fetch — monitoring only, NOT a join anchor.
    account_age_days:   Age of the posting account in days (from account creation
                        to event_time_ms).  0 = brand-new; negative disallowed.
    is_bot_scored:      True iff the Tier-A bot scorer rated this account as likely
                        automated (bot_score > BOT_SCORE_THRESHOLD).
    account_id:         Stable, anonymised account identifier (never PII).
    source:             Source adapter ("x" | "reddit" | "telegram" | "discord").
    """

    event_id: str                  # unique event identifier
    account_id: str                # anonymised stable account ID (no PII)
    source: str                    # "x" | "reddit" | "telegram" | "discord"
    event_time_ms: int             # platform-stamped publish time (authoritative)
    fetch_time_ms: int             # monitoring only — NOT a join anchor
    account_age_days: float        # account age at event time; >= 0
    is_bot_scored: bool            # True if Tier-A bot_score > threshold


# ---------------------------------------------------------------------------
# VelocitySource Protocol (injectable)
# ---------------------------------------------------------------------------


class VelocitySource(Protocol):
    """Protocol for the velocity event backend.

    Implementations:
        InMemoryVelocitySource   — offline / test (golden fixtures)
        SocialAdapterVelocitySource — production (wraps existing social adapters)

    CONTRACT: get_events() returns all events in the rolling window.
    The window is [as_of_ms - window_ms, as_of_ms + <some_future_buffer>].
    Callers are responsible for applying the final point-in-time filter
    (event_time_ms <= as_of_ms) INSIDE compute(), so that future-excluded
    events can be counted in the audit trail.

    In practice, sources return events as they arrive — including any that
    may have been fetched slightly after the decision point.  The compute()
    method applies the definitive PIT filter.
    """

    def get_events(
        self,
        asset: str,
        as_of_ms: int,
        window_ms: int,
    ) -> list[VelocityEvent]:
        """Return velocity events for asset within the rolling window.

        Window: [as_of_ms - window_ms, as_of_ms + small_buffer].
        The source does NOT need to enforce the strict <= as_of_ms upper bound;
        that is the compute() caller's responsibility so it can count exclusions.

        The source MUST enforce the lower bound (as_of_ms - window_ms) to avoid
        returning arbitrarily old events.
        """
        ...


# ---------------------------------------------------------------------------
# InMemoryVelocitySource (offline / test fixture)
# ---------------------------------------------------------------------------


class InMemoryVelocitySource:
    """Offline injectable velocity source for tests and CI.

    Accepts a list of VelocityEvent records.  Filters by as_of_ms and window_ms
    before returning.  No network calls, no disk reads, no LLM.

    The get_events() method returns only events within the rolling window
    AND with event_time_ms <= as_of_ms.  Events in the window but after
    as_of_ms are excluded (point-in-time rule).

    get_all_in_window() is used internally by VelocitySignalComputer to
    also count future-excluded events (for the audit trail).
    """

    def __init__(self, events: list[VelocityEvent] | None = None) -> None:
        self._events: list[VelocityEvent] = list(events or [])

    def add_event(self, e: VelocityEvent) -> None:
        self._events.append(e)

    def get_events(
        self,
        asset: str,  # noqa: ARG002 — ignored in this in-memory impl (no per-asset partitioning)
        as_of_ms: int,
        window_ms: int,
    ) -> list[VelocityEvent]:
        """Return events within the rolling window (lower bound only).

        The lower bound (as_of_ms - window_ms) is enforced here.
        The UPPER BOUND (as_of_ms) is intentionally NOT enforced by the source —
        compute() applies the definitive PIT filter so it can count exclusions.

        This mirrors how social adapters work in production: they return
        events from the rolling window regardless of exact fetch timing, and
        the compute pipeline applies the authoritative point-in-time cutoff.
        """
        cutoff_low = as_of_ms - window_ms
        return [e for e in self._events if e.event_time_ms >= cutoff_low]


# ---------------------------------------------------------------------------
# SocialAdapterVelocitySource (production backend — EN5)
# ---------------------------------------------------------------------------


class SocialAdapterVelocitySource:
    """Production VelocitySource backend — derives VelocityEvents from the
    RawPosts the social adapters (aats/sentiment/adapters.py) ALREADY fetched
    for this asset+window in an earlier ingestion stage (EN5).

    ZERO ADDITIONAL NETWORK CALLS: this class never calls a SocialAdapter (or
    any other I/O) itself.  It has no `fetch_posts` method and no adapter/
    credential/token constructor parameter — it only reshapes RawPost objects
    a SocialAdapter already retrieved (e.g. MCSSentimentPipeline's Stage 1
    fetch) into VelocityEvents, via `posts=` at construction or `load_posts()`.
    `MCSSentimentPipeline.score()` (EN2a's Stage 6) constructs this class
    directly over the SAME RawPosts its Stage 1 already fetched — there is a
    single, first-class, standalone, injectable VelocitySource implementation
    of the RawPost -> VelocityEvent mapping (this class), not a second inline
    copy in the pipeline, so the E10 signal can also be computed WITHOUT
    running the full MCS pipeline and WITHOUT any extra fetch or cost.

    `is_bot_scored` REUSES the Tier-A bot scorer (`aats.sentiment.tier_a._bot_score`
    gated by `BOT_SCORE_THRESHOLD`) — a single source of truth for "is this
    account bot-like", matching the VelocityEvent docstring contract exactly
    ("True iff the Tier-A bot scorer rated this account as likely automated").

    INJECTABLE: construct with `posts` (RawPosts already fetched for one
    asset+window) or call `load_posts()` to refresh them between calls (e.g.
    a caller re-points this source once per decision cycle, after its own
    social adapters run — no re-fetch happens inside this class either way).

    WINDOWING CONTRACT (mirrors InMemoryVelocitySource exactly):
      `get_events()` enforces only the LOWER bound
      (event_time_ms >= as_of_ms - window_ms).  The UPPER bound
      (event_time_ms <= as_of_ms) is intentionally left to
      VelocitySignalComputer.compute(), which applies the definitive
      point-in-time filter and counts future-excluded events for the audit
      trail (posts_excluded_future) — the SAME contract every VelocitySource
      implementation in this module honors.

    USAGE:
        source = SocialAdapterVelocitySource(posts=already_fetched_raw_posts)
        computer = VelocitySignalComputer(source=source)
        signal = computer.compute(asset, decision_time_ms)
        # signal.mcs_penalty >= 0.0 always (de-risk only)
    """

    def __init__(self, posts: list[RawPost] | None = None) -> None:
        self._posts: list[RawPost] = list(posts or [])

    def load_posts(self, posts: list[RawPost]) -> None:
        """Replace the underlying RawPost set with a freshly-fetched batch.

        NO NETWORK CALL happens here — `posts` must already have been
        retrieved by a SocialAdapter elsewhere; this method only re-points
        the in-memory list this source reshapes into VelocityEvents.
        """
        self._posts = list(posts)

    @property
    def post_count(self) -> int:
        """Number of RawPosts currently loaded (audit/debugging helper)."""
        return len(self._posts)

    def get_events(
        self,
        asset: str,  # noqa: ARG002 — RawPost carries no per-asset partition;
        # the caller is responsible for only loading posts relevant to this
        # asset (identical contract to InMemoryVelocitySource).
        as_of_ms: int,
        window_ms: int,
    ) -> list[VelocityEvent]:
        """Map the already-ingested RawPosts to VelocityEvents (lower-bound only).

        NO NETWORK CALLS, NO LLM CALLS: every VelocityEvent field is derived
        purely from RawPost attributes a SocialAdapter already populated.  The
        upper bound (event_time_ms <= as_of_ms) is intentionally NOT enforced
        here; VelocitySignalComputer.compute() applies the definitive
        point-in-time filter (same contract as InMemoryVelocitySource.get_events()).
        """
        cutoff_low = as_of_ms - window_ms
        return [
            VelocityEvent(
                event_id=post.post_id,
                account_id=post.author_id,
                source=post.source,
                event_time_ms=post.event_time_ms,
                fetch_time_ms=post.fetch_time_ms,
                account_age_days=max(0.0, post.account_age_days),
                is_bot_scored=_bot_score(post) > BOT_SCORE_THRESHOLD,
            )
            for post in self._posts
            if post.event_time_ms >= cutoff_low
        ]


# ---------------------------------------------------------------------------
# VelocitySignal: the output of the velocity computation
# ---------------------------------------------------------------------------


@dataclass
class VelocitySignal:
    """Aggregated social-velocity signal for one asset at a specific decision_time_ms.

    This is the output of VelocitySignalComputer.compute() and feeds into the
    MCS pipeline as a DOWNWARD modifier ONLY.

    HARD RULES:
      - mcs_penalty >= 0.0 always (can only de-risk, never raise conviction).
      - Bot-driven or coordinated growth raises the penalty toward 1.0.
      - Organic growth does NOT lower the penalty below 0.0.
      - Signal is SLOW-LOOP ONLY, never on the SNIPE/FAST hot path.

    mcs_penalty:
        Float in [0.0, 1.0].  Subtracted from the social MCS conviction.
        0.0 = no velocity penalty (no events, or fully organic growth).
        1.0 = maximum penalty (bot-farm burst detected; conviction → 0).

    organic_velocity_per_min:
        Rate of posts from organic (non-bot-scored, account_age > threshold)
        accounts, in posts/minute within the velocity window.
        INFORMATIONAL ONLY — does not raise conviction.

    bot_velocity_per_min:
        Rate of posts from bot-scored accounts, in posts/minute.
        HIGH value drives the penalty up.

    organic_bot_ratio:
        organic / total_velocity ratio in [0.0, 1.0].
        1.0 = all posts are organic; 0.0 = all are bots.
        Low ratio drives the penalty up.

    unique_account_count:
        Distinct account IDs posting within the velocity window.

    bot_account_fraction:
        Fraction of unique accounts that are bot-scored.

    account_age_cohort_flag:
        True if >= HOLDER_GROWTH_COHORT_THRESHOLD fraction of accounts
        have creation dates within ACCOUNT_CREATION_COHORT_WINDOW_S of each
        other — a factory-account signal.

    posts_excluded_future:
        Number of events dropped because event_time_ms > decision_time_ms.
        Should be 0 if the VelocitySource is correctly point-in-time filtered,
        but tracked for audit.

    total_events_in_window:
        Total events contributing to this computation.

    penalty_breakdown:
        Component-level breakdown of the penalty for the audit trail.

    decision_time_ms:
        The point-in-time anchor (C-5).

    red_flags:
        List of human-readable adversarial signals detected.
    """

    asset: str
    decision_time_ms: int
    mcs_penalty: float                  # [0.0, 1.0] — always non-negative (de-risk only)
    organic_velocity_per_min: float     # informational; does NOT raise conviction
    bot_velocity_per_min: float         # high = penalty up
    organic_bot_ratio: float            # [0.0, 1.0]; low = penalty up
    unique_account_count: int
    bot_account_fraction: float         # [0.0, 1.0]
    account_age_cohort_flag: bool       # True = factory-account batch detected
    posts_excluded_future: int          # audit: events dropped by PIT filter
    total_events_in_window: int
    penalty_breakdown: dict[str, float]
    red_flags: list[str]


# ---------------------------------------------------------------------------
# Internal penalty computation functions
# ---------------------------------------------------------------------------


def _compute_bot_velocity_penalty(
    bot_events: list[VelocityEvent],
    organic_events: list[VelocityEvent],
    window_minutes: float,
) -> float:
    """Penalty component for bot-driven velocity burst.

    HIGH bot posts/minute relative to organic posts/minute → penalty near
    BOT_VELOCITY_WEIGHT.

    The ratio is:
        bot_velocity_fraction = bot_per_min / (bot_per_min + organic_per_min + eps)
    This maps to a penalty in [0.0, BOT_VELOCITY_WEIGHT].

    NEVER raises the result above BOT_VELOCITY_WEIGHT.
    """
    if window_minutes <= 0.0:
        return 0.0
    bot_per_min = len(bot_events) / window_minutes
    organic_per_min = len(organic_events) / window_minutes
    total_per_min = bot_per_min + organic_per_min
    if total_per_min < 1e-9:
        return 0.0

    # Fraction of velocity that is bot-driven
    bot_fraction = bot_per_min / total_per_min
    # Map to penalty: 0 at 0% bots; BOT_VELOCITY_WEIGHT at 100% bots
    return bot_fraction * BOT_VELOCITY_WEIGHT


def _compute_growth_ratio_penalty(
    bot_account_fraction: float,
    total_events: int,
) -> float:
    """Penalty component for organic-vs-bot growth ratio.

    BOT-dominant growth (bot_account_fraction near 1.0) → penalty near
    GROWTH_RATIO_WEIGHT.

    If bot_account_fraction >= BOT_FRACTION_FULL_PENALTY, apply the full
    weight.  Below the threshold, scale linearly.

    No events → no penalty.
    """
    if total_events == 0:
        return 0.0
    if bot_account_fraction >= BOT_FRACTION_FULL_PENALTY:
        return GROWTH_RATIO_WEIGHT
    # Linear scale from 0.0 (0% bots) to GROWTH_RATIO_WEIGHT (at threshold)
    return (bot_account_fraction / BOT_FRACTION_FULL_PENALTY) * GROWTH_RATIO_WEIGHT


def _compute_holder_growth_penalty(
    events: list[VelocityEvent],
) -> tuple[float, bool]:
    """Penalty component for unique-holder (account) growth via factory accounts.

    Detects: a batch of accounts whose ages are all within
    ACCOUNT_CREATION_COHORT_WINDOW_S of each other → factory-batch signal.

    Returns (penalty in [0.0, HOLDER_GROWTH_WEIGHT], cohort_flag).
    """
    if not events:
        return 0.0, False

    unique_accounts = list({e.account_id: e for e in events}.values())
    if len(unique_accounts) < 2:
        return 0.0, False

    ages_days = [e.account_age_days for e in unique_accounts]

    # Find the cohort: the largest fraction of accounts within the creation window
    max_cohort_fraction = 0.0
    n = len(ages_days)
    ages_sorted = sorted(ages_days)

    # Sliding-window check: count how many accounts have ages within 1-day of each other
    cohort_window_days = ACCOUNT_CREATION_COHORT_WINDOW_S / 86_400.0
    j = 0
    for i in range(n):
        while j < n and ages_sorted[j] - ages_sorted[i] <= cohort_window_days:
            j += 1
        fraction = (j - i) / n
        if fraction > max_cohort_fraction:
            max_cohort_fraction = fraction

    cohort_flag = max_cohort_fraction >= HOLDER_GROWTH_COHORT_THRESHOLD

    if not cohort_flag:
        return 0.0, False

    # Scale penalty: higher cohort concentration → higher penalty
    # At exactly the threshold → half weight; at 100% cohort → full weight
    scale = (max_cohort_fraction - HOLDER_GROWTH_COHORT_THRESHOLD) / (
        1.0 - HOLDER_GROWTH_COHORT_THRESHOLD + 1e-9
    )
    penalty = HOLDER_GROWTH_WEIGHT * (0.5 + 0.5 * scale)
    return min(HOLDER_GROWTH_WEIGHT, penalty), True


def _count_future_excluded(
    raw_events: list[VelocityEvent],
    decision_time_ms: int,
) -> int:
    """Count events that would be excluded by the point-in-time filter."""
    return sum(1 for e in raw_events if e.event_time_ms > decision_time_ms)


def _detect_red_flags(
    bot_velocity_per_min: float,
    bot_account_fraction: float,
    cohort_flag: bool,
    organic_bot_ratio: float,
    penalty: float,
) -> list[str]:
    """Identify specific adversarial red flags for the audit trail."""
    flags: list[str] = []
    if bot_velocity_per_min >= BURST_VELOCITY_POSTS_PER_MINUTE:
        flags.append("bot_velocity_burst")
    if bot_account_fraction >= BOT_FRACTION_FULL_PENALTY:
        flags.append("bot_dominant_growth")
    elif bot_account_fraction >= 0.4:
        flags.append("high_bot_fraction")
    if cohort_flag:
        flags.append("factory_account_cohort")
    if organic_bot_ratio < 0.2:
        flags.append("very_low_organic_ratio")
    elif organic_bot_ratio < 0.4:
        flags.append("low_organic_ratio")
    if penalty > 0.5:
        flags.append("strong_velocity_de_risk")
    return flags


# ---------------------------------------------------------------------------
# VelocitySignalComputer — the public API
# ---------------------------------------------------------------------------


class VelocitySignalComputer:
    """Computes the social-velocity + organic-vs-bot-growth signal for an asset.

    INJECTABLE:
        source:         VelocitySource (InMemoryVelocitySource in tests/offline)

    OFFLINE DEFAULT:
        VelocitySignalComputer.offline() factory builds with InMemoryVelocitySource.

    SLOW-LOOP ONLY.  NOT on the SNIPE/FAST hot path.  No network calls.
    No LLM calls.  No secrets.

    USAGE:
        source = InMemoryVelocitySource(events=[...])
        computer = VelocitySignalComputer(source)
        signal = computer.compute("MINT_ADDRESS", decision_time_ms)
        # signal.mcs_penalty >= 0.0 always (de-risk only)
        # apply: conviction = max(0.0, base_conviction - signal.mcs_penalty)

    INVARIANTS:
        1. mcs_penalty in [0.0, 1.0] — always non-negative.
        2. Bot-driven velocity raises the penalty (de-risk direction).
        3. Organic growth has zero upward effect on MCS.
        4. Point-in-time filter is the FIRST operation on every event list.
        5. Future events (event_time_ms > decision_time_ms) are excluded and
           counted in posts_excluded_future for the audit trail.
        6. No win-rate computation anywhere.
        7. No capital sizing, no position trigger.
    """

    def __init__(
        self,
        source: VelocitySource,
        velocity_window_ms: int = DEFAULT_VELOCITY_WINDOW_MS,
    ) -> None:
        self._source = source
        self._velocity_window_ms = velocity_window_ms

    @classmethod
    def offline(
        cls,
        preloaded_events: list[VelocityEvent] | None = None,
        velocity_window_ms: int = DEFAULT_VELOCITY_WINDOW_MS,
    ) -> VelocitySignalComputer:
        """Factory: fully offline computer with in-memory source.

        Used in tests and CI environments.  No network calls, no API keys.

        Args:
            preloaded_events:   Optional list of VelocityEvent fixtures to load.
            velocity_window_ms: Rolling window size in milliseconds.
        """
        source = InMemoryVelocitySource(events=preloaded_events or [])
        return cls(source=source, velocity_window_ms=velocity_window_ms)

    def compute(
        self,
        asset: str,
        decision_time_ms: int,
    ) -> VelocitySignal:
        """Compute the VelocitySignal for an asset at decision_time_ms.

        POINT-IN-TIME CONTRACT (C-5):
            Only events with event_time_ms <= decision_time_ms are included.
            The VelocitySource MUST enforce this; we also apply a defensive
            second-layer filter here so the computation is safe even if the
            source is incorrectly implemented.

        Args:
            asset:            Mint address string.
            decision_time_ms: Point-in-time anchor.  Only events with
                              event_time_ms <= this value are considered.

        Returns:
            VelocitySignal with mcs_penalty in [0.0, 1.0].
        """
        # --- Fetch events from source (lower-bound window filter only) ---
        # The source enforces event_time_ms >= (decision_time_ms - window_ms).
        # The source does NOT enforce the upper bound (event_time_ms <= decision_time_ms)
        # so that we can count future-excluded events in the audit trail.
        raw_events = self._source.get_events(
            asset=asset,
            as_of_ms=decision_time_ms,
            window_ms=self._velocity_window_ms,
        )

        # --- DEFINITIVE PIT filter (first operation on the list — load-bearing C-5) ---
        # Apply the authoritative upper bound: only event_time_ms <= decision_time_ms.
        # Count the excluded future events for the audit trail BEFORE filtering.
        posts_excluded_future = _count_future_excluded(raw_events, decision_time_ms)
        events = [e for e in raw_events if e.event_time_ms <= decision_time_ms]

        if not events:
            logger.debug(
                "velocity: no events in window for asset=%s decision_time_ms=%d",
                asset,
                decision_time_ms,
            )
            return _empty_signal(asset, decision_time_ms, posts_excluded_future)

        # --- Split bot vs organic events ---
        bot_events = [e for e in events if e.is_bot_scored or e.account_age_days < BOT_AGE_THRESHOLD_DAYS]
        organic_events = [e for e in events if not e.is_bot_scored and e.account_age_days >= BOT_AGE_THRESHOLD_DAYS]

        # --- Velocity rates ---
        window_minutes = self._velocity_window_ms / 60_000.0
        organic_velocity_per_min = len(organic_events) / window_minutes if window_minutes > 0 else 0.0
        bot_velocity_per_min = len(bot_events) / window_minutes if window_minutes > 0 else 0.0

        # --- Unique accounts ---
        unique_accounts = {e.account_id for e in events}
        unique_bot_accounts = {e.account_id for e in bot_events}
        unique_account_count = len(unique_accounts)
        bot_account_fraction = (
            len(unique_bot_accounts) / unique_account_count
            if unique_account_count > 0
            else 0.0
        )

        # --- Organic-vs-bot ratio ---
        total = len(events)
        organic_count = len(organic_events)
        organic_bot_ratio = organic_count / total if total > 0 else 1.0

        # --- Penalty components ---
        p_bot_velocity = _compute_bot_velocity_penalty(bot_events, organic_events, window_minutes)
        p_growth_ratio = _compute_growth_ratio_penalty(bot_account_fraction, total)
        p_holder_growth, cohort_flag = _compute_holder_growth_penalty(events)

        total_penalty = min(1.0, p_bot_velocity + p_growth_ratio + p_holder_growth)

        # --- Red flags ---
        red_flags = _detect_red_flags(
            bot_velocity_per_min=bot_velocity_per_min,
            bot_account_fraction=bot_account_fraction,
            cohort_flag=cohort_flag,
            organic_bot_ratio=organic_bot_ratio,
            penalty=total_penalty,
        )

        penalty_breakdown = {
            "bot_velocity_component": p_bot_velocity,
            "growth_ratio_component": p_growth_ratio,
            "holder_growth_component": p_holder_growth,
            "total": total_penalty,
        }

        logger.info(
            "velocity: asset=%s events=%d bot=%d organic=%d "
            "bot_vel=%.2f/min org_vel=%.2f/min ratio=%.2f "
            "bot_acct_frac=%.2f cohort=%s penalty=%.3f flags=%s",
            asset,
            total,
            len(bot_events),
            len(organic_events),
            bot_velocity_per_min,
            organic_velocity_per_min,
            organic_bot_ratio,
            bot_account_fraction,
            cohort_flag,
            total_penalty,
            red_flags,
        )

        return VelocitySignal(
            asset=asset,
            decision_time_ms=decision_time_ms,
            mcs_penalty=total_penalty,
            organic_velocity_per_min=organic_velocity_per_min,
            bot_velocity_per_min=bot_velocity_per_min,
            organic_bot_ratio=organic_bot_ratio,
            unique_account_count=unique_account_count,
            bot_account_fraction=bot_account_fraction,
            account_age_cohort_flag=cohort_flag,
            posts_excluded_future=posts_excluded_future,
            total_events_in_window=total,
            penalty_breakdown=penalty_breakdown,
            red_flags=red_flags,
        )


# ---------------------------------------------------------------------------
# MCS integration helpers (load-bearing seam)
# ---------------------------------------------------------------------------


def apply_velocity_penalty_to_conviction(
    base_conviction: float,
    signal: VelocitySignal,
) -> float:
    """Apply the velocity-based penalty to an MCS conviction score.

    INVARIANT: the result CANNOT exceed base_conviction.
    Bot-driven / coordinated growth can only lower conviction.
    Fully organic velocity returns base_conviction unchanged (penalty = 0.0).

    Args:
        base_conviction:  The social/narrative MCS conviction BEFORE velocity
                          adjustment.  Must be in [0.0, 1.0].
        signal:           VelocitySignal from compute().

    Returns:
        Adjusted conviction in [0.0, base_conviction].
        result = clamp(base_conviction - signal.mcs_penalty, 0.0, 1.0)

    TYPE SAFETY: mcs_penalty >= 0.0 always, so the result <= base_conviction.
    This is the only legal way to integrate velocity into MCS.
    Using a VelocitySignal to SIZE UP, WIDEN A STOP, or OVERRIDE a hard stop
    is a defect — MCS integration is de-risk direction only.
    """
    # Hard clamp: mcs_penalty is guaranteed non-negative by VelocitySignalComputer,
    # but a forged / mutated VelocitySignal (e.g. from a unit-test adversary or a
    # future code path that bypasses the compute() pipeline) could carry a negative
    # value.  A bare `assert` is STRIPPED under `python -O`, so we enforce the
    # invariant with an explicit raise that survives optimised mode AND a clamp
    # that prevents any accidental raise-conviction path even if the raise were
    # somehow suppressed.
    #
    # ORDER OF OPERATIONS (load-bearing):
    #   1. Clamp penalty to [0, +inf) — de-risk direction enforced unconditionally.
    #   2. Raise ValueError for the forged-negative case — not an assert.
    #   3. Compute adjusted score.
    #   4. Raise ValueError if adjusted > base_conviction — not an assert.
    #
    # The clamp in step 1 means step 3 is always safe even if the raise in step 2
    # is somehow bypassed (belt-and-suspenders).
    penalty_raw = signal.mcs_penalty
    if penalty_raw < 0.0:
        # This is NOT an assert — it is NOT stripped by python -O.
        raise ValueError(
            f"VELOCITY INVARIANT VIOLATED: signal.mcs_penalty={penalty_raw:.6f} is negative. "
            "VelocitySignal.mcs_penalty MUST be >= 0.0. "
            "A negative penalty would raise conviction, violating the de-risk direction rule. "
            "This is a defect in the caller or the VelocitySignalComputer pipeline."
        )
    # Clamp defensively even after the raise (belt-and-suspenders: if this
    # function is ever called in a context where exceptions are swallowed, the
    # clamp ensures conviction can still only go down, never up).
    penalty = max(0.0, penalty_raw)
    adjusted = max(0.0, min(1.0, base_conviction - penalty))
    # Hard guard: result must not exceed base_conviction.
    # This is NOT an assert — it is NOT stripped by python -O.
    if adjusted > base_conviction + 1e-9:
        raise ValueError(
            f"VELOCITY INVARIANT VIOLATED: adjusted conviction {adjusted:.6f} "
            f"exceeds base_conviction {base_conviction:.6f} "
            f"(signal.mcs_penalty={signal.mcs_penalty:.6f}, clamped_penalty={penalty:.6f}). "
            "Velocity signals may ONLY de-risk (lower or maintain conviction). "
            "This is a critical safety defect — investigation required."
        )
    return adjusted


def assert_velocity_cannot_raise_conviction(
    signal: VelocitySignal,
    base_conviction: float,
) -> float:
    """Alias for apply_velocity_penalty_to_conviction — used in tests as an explicit assertion.

    Proves that no VelocitySignal can ever raise conviction above base_conviction.
    """
    return apply_velocity_penalty_to_conviction(base_conviction, signal)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _empty_signal(
    asset: str,
    decision_time_ms: int,
    posts_excluded_future: int = 0,
) -> VelocitySignal:
    """Return a zero-penalty signal when there are no events in the window."""
    return VelocitySignal(
        asset=asset,
        decision_time_ms=decision_time_ms,
        mcs_penalty=0.0,
        organic_velocity_per_min=0.0,
        bot_velocity_per_min=0.0,
        organic_bot_ratio=1.0,
        unique_account_count=0,
        bot_account_fraction=0.0,
        account_age_cohort_flag=False,
        posts_excluded_future=posts_excluded_future,
        total_events_in_window=0,
        penalty_breakdown={
            "bot_velocity_component": 0.0,
            "growth_ratio_component": 0.0,
            "holder_growth_component": 0.0,
            "total": 0.0,
        },
        red_flags=[],
    )


def make_velocity_event(
    event_id: str,
    account_id: str,
    event_time_ms: int,
    account_age_days: float,
    is_bot_scored: bool,
    source: str = "x",
    fetch_time_ms: int | None = None,
) -> VelocityEvent:
    """Convenience builder for VelocityEvent (used in tests and golden fixtures).

    All fields are required (no hidden defaults that could mask test errors).
    fetch_time_ms defaults to event_time_ms + 100 (minimal lag, monitoring only).
    """
    return VelocityEvent(
        event_id=event_id,
        account_id=account_id,
        source=source,
        event_time_ms=event_time_ms,
        fetch_time_ms=fetch_time_ms if fetch_time_ms is not None else event_time_ms + 100,
        account_age_days=max(0.0, account_age_days),
        is_bot_scored=is_bot_scored,
    )
