"""EN5 — SocialAdapterVelocitySource: production VelocitySource backend.

Self-check items verified here:
  EN5-V-A: SocialAdapterVelocitySource never performs a network call — no
           adapter/token/credential constructor parameter, no `fetch_posts`
           method, no network-library reference anywhere in its source.
  EN5-V-B: It derives VelocityEvents purely from RawPosts already loaded
           (constructor / load_posts()) — zero additional fetch.
  EN5-V-C: `is_bot_scored` matches the Tier-A bot scorer exactly (same
           BOT_SCORE_THRESHOLD gate as `aats.sentiment.tier_a._bot_score`).
  EN5-V-D: Windowing contract mirrors InMemoryVelocitySource exactly — only
           the LOWER bound is enforced by get_events(); the UPPER bound
           (point-in-time) is enforced by VelocitySignalComputer.compute(),
           which also counts future-excluded posts for the audit trail.
  EN5-V-E: A coordinated bot-burst RawPost set produces mcs_penalty > 0 when
           run end-to-end through VelocitySignalComputer (adversarial fixture,
           mutation-meaningful).
  EN5-V-F: A clean organic RawPost set produces mcs_penalty == 0.0.
  EN5-V-G: Parity — SocialAdapterVelocitySource and a manually-built
           InMemoryVelocitySource (using the identical RawPost -> VelocityEvent
           mapping) produce IDENTICAL VelocitySignal output for the same posts.
  EN5-V-H: No win_rate field/computation anywhere in velocity.py (grep-assert).

GOLDEN FIXTURES (deterministic, no wall-clock):
  BOT_BURST_RAW_POSTS: 20 near-identical bot-profile RawPosts in a 4-minute
                       burst — brand-new accounts, default avatar, high
                       cadence, thin follower/following ratio.
  ORGANIC_RAW_POSTS:   5 organic-profile RawPosts spread across ~13 minutes —
                       established accounts, real engagement, low cadence.
  FUTURE_RAW_POSTS:    RawPosts timestamped AFTER decision_time_ms.
"""

from __future__ import annotations

import inspect
import time

import pytest

from aats.sentiment.models import RawPost
from aats.sentiment.tier_a import BOT_SCORE_THRESHOLD, _bot_score
from aats.sentiment.velocity import (
    InMemoryVelocitySource,
    SocialAdapterVelocitySource,
    VelocityEvent,
    VelocitySignalComputer,
)

# ---------------------------------------------------------------------------
# Time anchors (deterministic — no wall-clock)
# ---------------------------------------------------------------------------

DECISION_TIME_MS: int = 1_718_500_000_000  # 2024-06-16T00:00:00Z
ONE_MIN_MS = 60_000
WINDOW_MS = 900_000  # 15 minutes (VelocitySignalComputer default)

T_MINUS_14MIN = DECISION_TIME_MS - 14 * ONE_MIN_MS
T_MINUS_5MIN = DECISION_TIME_MS - 5 * ONE_MIN_MS
T_PLUS_5MIN = DECISION_TIME_MS + 5 * ONE_MIN_MS

ASSET = "SocialVelocitySourceTestMint111111111111"


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _bot_like_post(post_id: str, author_id: str, event_time_ms: int, account_age_days: float) -> RawPost:
    """A RawPost whose fields push `_bot_score` comfortably above BOT_SCORE_THRESHOLD."""
    return RawPost(
        post_id=post_id,
        source="x",
        author_id=author_id,
        text="gm degen this is the next 100x pump.fun gem",
        event_time_ms=event_time_ms,
        fetch_time_ms=event_time_ms + 50,
        account_age_days=account_age_days,  # brand new
        follower_count=5,
        following_count=1_000,  # ratio << 0.05
        like_count=0,
        reply_count=0,
        repost_count=0,
        is_verified=False,
        has_default_avatar=True,
        posting_cadence_per_day=500.0,  # bot-level cadence
    )


def _organic_post(post_id: str, author_id: str, event_time_ms: int, account_age_days: float) -> RawPost:
    """A RawPost whose fields keep `_bot_score` well below BOT_SCORE_THRESHOLD."""
    return RawPost(
        post_id=post_id,
        source="x",
        author_id=author_id,
        text="reviewed the contract, dev wallet is clean, LP locked for 6 months",
        event_time_ms=event_time_ms,
        fetch_time_ms=event_time_ms + 50,
        account_age_days=account_age_days,  # well-established
        follower_count=8_000,
        following_count=600,  # healthy ratio
        like_count=30,
        reply_count=6,
        repost_count=2,
        is_verified=True,
        has_default_avatar=False,
        posting_cadence_per_day=4.0,  # low cadence
    )


# Sanity: confirm the fixture builders actually straddle BOT_SCORE_THRESHOLD
# (proves the fixtures are meaningful, not accidentally both-bot or both-organic).
_SANITY_BOT_POST = _bot_like_post("sanity_bot", "sanity_bot_acct", T_MINUS_5MIN, 0.5)
_SANITY_ORGANIC_POST = _organic_post("sanity_org", "sanity_org_acct", T_MINUS_5MIN, 400.0)
assert _bot_score(_SANITY_BOT_POST) > BOT_SCORE_THRESHOLD
assert _bot_score(_SANITY_ORGANIC_POST) <= BOT_SCORE_THRESHOLD

# 20 bot-profile posts in a ~4-minute burst (12s apart)
BOT_BURST_RAW_POSTS: list[RawPost] = [
    _bot_like_post(
        f"bot_post_{i:03d}",
        f"bot_author_{i:03d}",
        T_MINUS_5MIN + i * 12_000,
        account_age_days=float(i % 2),  # 0 or 1 days
    )
    for i in range(20)
]

# 5 organic posts spread across ~13 minutes (well inside the 15-min window)
ORGANIC_RAW_POSTS: list[RawPost] = [
    _organic_post(
        f"organic_post_{i:03d}",
        f"organic_author_{i:03d}",
        T_MINUS_14MIN + i * 3 * ONE_MIN_MS,
        account_age_days=365.0 + i * 10.0,
    )
    for i in range(5)
]

# Posts timestamped AFTER decision_time_ms (must be excluded by compute()'s PIT filter)
FUTURE_RAW_POSTS: list[RawPost] = [
    _organic_post(
        f"future_post_{i:03d}",
        f"future_author_{i:03d}",
        T_PLUS_5MIN + i * ONE_MIN_MS,
        account_age_days=200.0,
    )
    for i in range(3)
]


def _reference_velocity_events(posts: list[RawPost]) -> list[VelocityEvent]:
    """Independently reproduce the RawPost -> VelocityEvent mapping.

    This is a SEPARATE implementation of the same transformation
    SocialAdapterVelocitySource performs, used ONLY as an independent ground
    truth for the parity tests (EN5-V-G) — not imported from velocity.py.
    """
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
        for post in posts
    ]


# ---------------------------------------------------------------------------
# EN5-V-A / EN5-V-B: no network calls, no adapter dependency
# ---------------------------------------------------------------------------


class TestNoNetworkCalls:
    """EN5-V-A/B: SocialAdapterVelocitySource never performs I/O of any kind."""

    def test_no_network_or_async_in_source(self) -> None:
        import aats.sentiment.velocity as vel_mod

        source = inspect.getsource(vel_mod.SocialAdapterVelocitySource)
        forbidden_tokens = (
            "httpx",
            "requests.",
            "asyncpraw",
            "telethon",
            "discord",
            "def fetch_posts",
            "async def",
            "await ",
            "socket.",
            "aiohttp",
        )
        for token in forbidden_tokens:
            assert token not in source, (
                f"SocialAdapterVelocitySource source contains forbidden token {token!r} — "
                "this class must derive events from already-fetched RawPosts with ZERO "
                "additional network/adapter calls."
            )

    def test_constructor_takes_only_preloaded_posts(self) -> None:
        sig = inspect.signature(SocialAdapterVelocitySource.__init__)
        params = list(sig.parameters)
        assert params == ["self", "posts"], (
            f"SocialAdapterVelocitySource.__init__ must accept only a preloaded "
            f"`posts` list (no adapter/token/credential params). Got: {params}"
        )

    def test_get_events_is_synchronous(self) -> None:
        """get_events must be a plain (non-async) method — no I/O wait possible."""
        assert not inspect.iscoroutinefunction(SocialAdapterVelocitySource.get_events), (
            "SocialAdapterVelocitySource.get_events must be synchronous — an async "
            "method would imply it performs its own I/O/fetch."
        )

    def test_no_network_calls_end_to_end(self) -> None:
        """Constructing + querying the source must complete instantly with no I/O.

        If this class attempted a network call, the absence of any mocked
        transport/credentials would cause it to hang or raise; completing
        cleanly and quickly is itself evidence of zero I/O.
        """
        start = time.monotonic()
        source = SocialAdapterVelocitySource(posts=BOT_BURST_RAW_POSTS)
        events = source.get_events(ASSET, DECISION_TIME_MS, WINDOW_MS)
        elapsed_s = time.monotonic() - start
        assert len(events) == len(BOT_BURST_RAW_POSTS)
        assert elapsed_s < 1.0, f"get_events took {elapsed_s:.3f}s — suspiciously slow for pure arithmetic."


# ---------------------------------------------------------------------------
# EN5-V-B / EN5-V-C: correct RawPost -> VelocityEvent derivation
# ---------------------------------------------------------------------------


class TestDerivesEventsFromRawPosts:
    """EN5-V-B/C: events are derived correctly, including is_bot_scored."""

    def test_bot_burst_posts_are_marked_bot_scored(self) -> None:
        source = SocialAdapterVelocitySource(posts=BOT_BURST_RAW_POSTS)
        events = source.get_events(ASSET, DECISION_TIME_MS, WINDOW_MS)
        assert len(events) == len(BOT_BURST_RAW_POSTS)
        assert all(e.is_bot_scored for e in events), (
            "All bot-profile RawPosts must be classified is_bot_scored=True, "
            "matching the Tier-A bot scorer threshold."
        )

    def test_organic_posts_are_not_bot_scored(self) -> None:
        source = SocialAdapterVelocitySource(posts=ORGANIC_RAW_POSTS)
        events = source.get_events(ASSET, DECISION_TIME_MS, WINDOW_MS)
        assert len(events) == len(ORGANIC_RAW_POSTS)
        assert not any(e.is_bot_scored for e in events), (
            "Organic RawPosts must be classified is_bot_scored=False."
        )

    def test_event_fields_map_correctly(self) -> None:
        post = ORGANIC_RAW_POSTS[0]
        source = SocialAdapterVelocitySource(posts=[post])
        events = source.get_events(ASSET, DECISION_TIME_MS, WINDOW_MS)
        assert len(events) == 1
        ev = events[0]
        assert ev.event_id == post.post_id
        assert ev.account_id == post.author_id
        assert ev.source == post.source
        assert ev.event_time_ms == post.event_time_ms
        assert ev.fetch_time_ms == post.fetch_time_ms
        assert ev.account_age_days == pytest.approx(post.account_age_days)

    def test_load_posts_replaces_underlying_set(self) -> None:
        source = SocialAdapterVelocitySource(posts=ORGANIC_RAW_POSTS)
        assert source.post_count == len(ORGANIC_RAW_POSTS)
        source.load_posts(BOT_BURST_RAW_POSTS)
        assert source.post_count == len(BOT_BURST_RAW_POSTS)
        events = source.get_events(ASSET, DECISION_TIME_MS, WINDOW_MS)
        assert len(events) == len(BOT_BURST_RAW_POSTS)

    def test_empty_posts_yields_no_events(self) -> None:
        source = SocialAdapterVelocitySource()
        events = source.get_events(ASSET, DECISION_TIME_MS, WINDOW_MS)
        assert events == []


# ---------------------------------------------------------------------------
# EN5-V-D: windowing contract mirrors InMemoryVelocitySource
# ---------------------------------------------------------------------------


class TestWindowingContract:
    """EN5-V-D: only the lower bound is enforced by get_events(); the upper
    bound (point-in-time) is enforced by VelocitySignalComputer.compute()."""

    def test_lower_bound_enforced_by_get_events(self) -> None:
        too_old_post = _organic_post(
            "too_old", "too_old_author", DECISION_TIME_MS - WINDOW_MS - ONE_MIN_MS, 400.0
        )
        in_window_post = ORGANIC_RAW_POSTS[0]
        source = SocialAdapterVelocitySource(posts=[too_old_post, in_window_post])
        events = source.get_events(ASSET, DECISION_TIME_MS, WINDOW_MS)
        assert len(events) == 1, "Posts older than the rolling window must be excluded."
        assert events[0].event_id == in_window_post.post_id

    def test_upper_bound_not_enforced_by_get_events_but_by_compute(self) -> None:
        """get_events() returns future posts raw; compute() applies the PIT cutoff."""
        source = SocialAdapterVelocitySource(posts=FUTURE_RAW_POSTS)
        raw_events = source.get_events(ASSET, DECISION_TIME_MS, WINDOW_MS)
        assert len(raw_events) == len(FUTURE_RAW_POSTS), (
            "get_events() must NOT enforce the upper (point-in-time) bound itself — "
            "that is compute()'s job so future-excluded events can be counted."
        )

        computer = VelocitySignalComputer(source=source, velocity_window_ms=WINDOW_MS)
        signal = computer.compute(ASSET, DECISION_TIME_MS)
        assert signal.total_events_in_window == 0, (
            "compute() must exclude ALL future posts from the actual signal."
        )
        assert signal.posts_excluded_future == len(FUTURE_RAW_POSTS), (
            "compute() must count every future-excluded post in the audit trail."
        )
        assert signal.mcs_penalty == 0.0


# ---------------------------------------------------------------------------
# EN5-V-E / EN5-V-F: adversarial vs. organic end-to-end behaviour
# ---------------------------------------------------------------------------


class TestAdversarialVsOrganicEndToEnd:
    """EN5-V-E/F: bot burst -> penalty > 0; clean organic -> penalty == 0."""

    def test_bot_burst_produces_positive_penalty(self) -> None:
        source = SocialAdapterVelocitySource(posts=BOT_BURST_RAW_POSTS)
        computer = VelocitySignalComputer(source=source, velocity_window_ms=WINDOW_MS)
        signal = computer.compute(ASSET, DECISION_TIME_MS)

        print(
            f"\n[EN5-V-E] bot_burst: events={signal.total_events_in_window} "
            f"bot_frac={signal.bot_account_fraction:.2f} penalty={signal.mcs_penalty:.4f} "
            f"flags={signal.red_flags}"
        )

        assert signal.mcs_penalty > 0.0, (
            "ADVERSARIAL INVARIANT VIOLATED: a coordinated bot-burst RawPost set "
            f"produced mcs_penalty=0.0 through SocialAdapterVelocitySource. Got "
            f"{signal.mcs_penalty:.4f}."
        )
        assert signal.mcs_penalty >= 0.3, (
            f"Bot burst (20 events, 4-min window) must produce a substantial penalty. "
            f"Got {signal.mcs_penalty:.4f}."
        )

    def test_organic_posts_produce_zero_penalty(self) -> None:
        source = SocialAdapterVelocitySource(posts=ORGANIC_RAW_POSTS)
        computer = VelocitySignalComputer(source=source, velocity_window_ms=WINDOW_MS)
        signal = computer.compute(ASSET, DECISION_TIME_MS)

        print(
            f"\n[EN5-V-F] organic: events={signal.total_events_in_window} "
            f"bot_frac={signal.bot_account_fraction:.2f} penalty={signal.mcs_penalty:.4f}"
        )

        assert signal.bot_account_fraction == 0.0
        assert signal.mcs_penalty == 0.0, (
            f"Clean organic RawPosts must produce mcs_penalty=0.0. Got {signal.mcs_penalty:.4f}."
        )


# ---------------------------------------------------------------------------
# EN5-V-G: parity with a manually-built InMemoryVelocitySource
# ---------------------------------------------------------------------------


class TestParityWithInMemorySource:
    """EN5-V-G: SocialAdapterVelocitySource and an independently-constructed
    InMemoryVelocitySource of the SAME reference VelocityEvents produce
    IDENTICAL VelocitySignal output."""

    @pytest.mark.parametrize(
        "posts,label",
        [
            (BOT_BURST_RAW_POSTS, "bot_burst"),
            (ORGANIC_RAW_POSTS, "organic"),
            (BOT_BURST_RAW_POSTS + ORGANIC_RAW_POSTS, "mixed"),
        ],
    )
    def test_signal_parity(self, posts: list[RawPost], label: str) -> None:
        adapter_source = SocialAdapterVelocitySource(posts=posts)
        adapter_computer = VelocitySignalComputer(source=adapter_source, velocity_window_ms=WINDOW_MS)
        adapter_signal = adapter_computer.compute(ASSET, DECISION_TIME_MS)

        reference_events = _reference_velocity_events(posts)
        reference_source = InMemoryVelocitySource(events=reference_events)
        reference_computer = VelocitySignalComputer(source=reference_source, velocity_window_ms=WINDOW_MS)
        reference_signal = reference_computer.compute(ASSET, DECISION_TIME_MS)

        print(
            f"\n[EN5-V-G:{label}] adapter_penalty={adapter_signal.mcs_penalty:.6f} "
            f"reference_penalty={reference_signal.mcs_penalty:.6f}"
        )

        assert adapter_signal.mcs_penalty == pytest.approx(reference_signal.mcs_penalty, abs=1e-9)
        assert adapter_signal.total_events_in_window == reference_signal.total_events_in_window
        assert adapter_signal.bot_account_fraction == pytest.approx(
            reference_signal.bot_account_fraction, abs=1e-9
        )
        assert adapter_signal.account_age_cohort_flag == reference_signal.account_age_cohort_flag
        assert set(adapter_signal.red_flags) == set(reference_signal.red_flags)


# ---------------------------------------------------------------------------
# EN5-V-H: No win_rate field/computation anywhere (grep-assert)
# ---------------------------------------------------------------------------


class TestNoWinRate:
    """EN5-V-H: 'win_rate' must not appear anywhere in velocity.py source."""

    def test_no_win_rate_in_module_source(self) -> None:
        import aats.sentiment.velocity as velocity_module

        source = inspect.getsource(velocity_module)
        assert "win_rate" not in source.lower(), (
            "HARD RULE VIOLATED: 'win_rate' found in aats.sentiment.velocity source. "
            "No win-rate computation is permitted anywhere."
        )
