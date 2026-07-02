"""Tests for the Telegram ingestion adapter (EN3 — real telethon implementation).

Acceptance criteria (EN3 task):
  - TelegramAdapter.fetch_posts is a REAL implementation (no unconditional
    NotImplementedError) restricted to a PRE-CONFIGURED call-channel allowlist
    (MCS_TELEGRAM_CHANNEL_IDS) — no crawl/search of arbitrary dialogs.
  - INJECTABLE exactly like aats/telegram/client.py: an Offline (fake) client
    is used in ALL tests; the Http/real (telethon-backed) client is
    constructed ONLY at runtime — so NO test ever opens a real MTProto session.
  - event_time_ms comes from message.date (T-300a), NEVER wall-clock.
  - api_id/api_hash/session are injected from Vault/.env ONLY, never
    hardcoded or logged.
  - Output is a RawPost feeding the existing adversarial Tier-A/Tier-B
    pipeline — creates no new buy-trigger or sizing path.

HARD RULES verified here:
  - No real MTProto session is ever opened by the test suite (telethon is
    never imported when a client is injected).
  - Point-in-time: future Telegram messages are excluded before scoring.
  - Allowlist enforcement: only pre-configured channel_ids are ever queried.
  - No secrets in source; credential injection is env-driven and redacted.
"""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
import sys

import pytest

from aats.sentiment.adapters import (
    HttpTelegramMTProtoClient,
    OfflineTelegramMTProtoClient,
    TelegramAdapter,
    TelegramMTProtoClient,
    TelegramRawMessage,
)
from aats.sentiment.pipeline import MCSSentimentPipeline
from aats.sentiment.tier_a import run_tier_a
from aats.sentiment.tier_b import MockLLMBackend, TierBScorer
from tests.sentiment.fixtures import (
    DECISION_TIME_MS,
    ONE_DAY_MS,
    ONE_HOUR_MS,
    T_MINUS_1HR,
    T_MINUS_2HR,
    T_MINUS_10MIN,
    T_MINUS_30MIN,
    T_PLUS_1HR,
    T_PLUS_5MIN,
)

_ASSET = "TelegramM1ntXxxtest"

# The PRE-CONFIGURED call-channel allowlist (mirrors MCS_TELEGRAM_CHANNEL_IDS).
_ALLOWED_CHANNEL_ID = 1_001_001_001
_OTHER_CHANNEL_ID = 2_002_002_002  # NOT on the allowlist — must never be queried

# ---------------------------------------------------------------------------
# Fixtures: TelegramRawMessage (wire-level, pre-adapter)
# ---------------------------------------------------------------------------

TELEGRAM_ORGANIC_MESSAGES: list[TelegramRawMessage] = [
    TelegramRawMessage(
        message_id="tg_msg_001",
        channel_id=_ALLOWED_CHANNEL_ID,
        sender_id="alpha_channel",
        text="Solana gem launch on raydium — dev wallet under 3%, LP locked 30d. Watching.",
        event_time_ms=T_MINUS_2HR,
    ),
    TelegramRawMessage(
        message_id="tg_msg_002",
        channel_id=_ALLOWED_CHANNEL_ID,
        sender_id="alpha_channel",
        text="Early volume on this Solana pump.fun migration looks organic, spread across wallets.",
        event_time_ms=T_MINUS_1HR,
    ),
    TelegramRawMessage(
        message_id="tg_msg_003",
        channel_id=_ALLOWED_CHANNEL_ID,
        sender_id="alpha_channel",
        text="Dev known in the solana degen community, no mint authority. Worth tracking.",
        event_time_ms=T_MINUS_30MIN,
    ),
]

TELEGRAM_FUTURE_MESSAGES: list[TelegramRawMessage] = [
    # NOTE (fix for EN3-QA-01): both texts deliberately contain "solana" so the
    # KEYWORD filter can never be the (incidental) reason these are excluded --
    # only the point-in-time guard(s) can exclude them.  See
    # test_adapter_defensively_re_filters_a_leaky_client below, whose
    # _LeakyClient ignores window bounds entirely: with a keyword-matching
    # text, that test is mutation-effective against adapters.py's defensive
    # re-filter (`if not (earliest_ms <= msg.event_time_ms <= decision_time_ms)`).
    TelegramRawMessage(
        message_id="tg_msg_future_001",
        channel_id=_ALLOWED_CHANNEL_ID,
        sender_id="alpha_channel",
        text="Solana gem from the future -- this message must be excluded.",
        event_time_ms=T_PLUS_5MIN,
    ),
    TelegramRawMessage(
        message_id="tg_msg_future_002",
        channel_id=_ALLOWED_CHANNEL_ID,
        sender_id="alpha_channel",
        text="Another solana gem from the future that must also be excluded.",
        event_time_ms=T_PLUS_1HR,
    ),
]

TELEGRAM_OTHER_CHANNEL_MESSAGES: list[TelegramRawMessage] = [
    TelegramRawMessage(
        message_id="tg_msg_other_001",
        channel_id=_OTHER_CHANNEL_ID,
        sender_id="random_channel",
        text="Solana pump gem — this channel is NOT on the allowlist.",
        event_time_ms=T_MINUS_30MIN,
    ),
]

# ---------------------------------------------------------------------------
# Fixture: ADVERSARIAL — organic (spread, distinct) vs coordinated-shill burst
#
# TelegramAdapter cannot observe per-account age/follower data via MTProto for
# broadcast channel content, so _telegram_message_to_raw_post() maps EVERY
# Telegram message to the same conservative (never-up-weighted) account
# defaults. The adversarial differentiator for THIS source is therefore
# synchronicity + cluster concentration alone: 8 distinct messages spread
# across ~20h->6h before decision_time (low sync, low concentration) vs 20
# near-identical messages in a 90-second burst (single dominant cluster).
# ---------------------------------------------------------------------------

_TELEGRAM_ORGANIC_SPREAD_TEXTS = [
    "Solana gem launch on raydium — dev wallet under 3%, LP locked 30d. Watching.",
    "Early volume on this Solana pump.fun migration looks organic, spread across wallets.",
    "Dev known in the solana degen community, no mint authority. Worth tracking.",
    "Solana holder count growing steadily, nothing alarming in the chart yet.",
    "This Solana token contract looks clean, no proxy, mint function renounced.",
    "Liquidity pool for this Solana pair seeded modestly, still early days.",
    "Solana community discussion pretty measured today, no major catalysts.",
    "Watching this Solana project, dev has shipped before — reasonable track record.",
]

TELEGRAM_ORGANIC_SPREAD_MESSAGES: list[TelegramRawMessage] = [
    TelegramRawMessage(
        message_id=f"tg_spread_{i:03d}",
        channel_id=_ALLOWED_CHANNEL_ID,
        sender_id="alpha_channel",
        text=text,
        event_time_ms=DECISION_TIME_MS - (20 - 2 * i) * ONE_HOUR_MS,  # 20h -> 6h before decision
    )
    for i, text in enumerate(_TELEGRAM_ORGANIC_SPREAD_TEXTS)
]

_TELEGRAM_SHILL_TEXT_BASE = (
    "100x GEM ALERT! solana pump.fun migrating to raydium, LP locked, only "
    "1000 holders! GET IN NOW before it moons!"
)

TELEGRAM_SHILL_BURST_MESSAGES: list[TelegramRawMessage] = [
    TelegramRawMessage(
        message_id=f"tg_shill_{i:03d}",
        channel_id=_ALLOWED_CHANNEL_ID,
        sender_id=f"shill_relay_{i}",
        text=_TELEGRAM_SHILL_TEXT_BASE + f" #{i}",  # minor variation — same cluster after dedup
        event_time_ms=T_MINUS_10MIN + (i * 4_500),  # 4.5s apart -> 90s total burst
    )
    for i in range(20)
]


def _make_adapter(
    *,
    channel_ids: list[int],
    client: TelegramMTProtoClient | None,
    api_id: int | None = None,
    api_hash: str | None = None,
    session_string: str | None = None,
) -> TelegramAdapter:
    return TelegramAdapter(
        api_id=api_id,
        api_hash=api_hash,
        session_string=session_string,
        channel_ids=channel_ids,
        client=client,
    )


# ---------------------------------------------------------------------------
# 1. Real implementation — no unconditional NotImplementedError
# ---------------------------------------------------------------------------


def test_fetch_posts_no_longer_raises_notimplementederror_unconditionally():
    """TelegramAdapter.fetch_posts must be a real body, not a stub raise."""
    source = inspect.getsource(TelegramAdapter.fetch_posts)
    assert "raise NotImplementedError" not in source, (
        "TelegramAdapter.fetch_posts still unconditionally raises NotImplementedError — "
        "EN3 requires a real telethon-backed implementation."
    )


# ---------------------------------------------------------------------------
# 2. Offline injectable client — no live session ever opened
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_offline_client_yields_raw_posts_with_platform_event_time():
    """Injected OfflineTelegramMTProtoClient produces RawPosts; event_time_ms
    equals the message.date-derived value, NOT any wall-clock stamp."""
    client = OfflineTelegramMTProtoClient(
        messages_by_channel={_ALLOWED_CHANNEL_ID: TELEGRAM_ORGANIC_MESSAGES}
    )
    adapter = _make_adapter(channel_ids=[_ALLOWED_CHANNEL_ID], client=client)

    posts = await adapter.fetch_posts(["solana"], DECISION_TIME_MS, 3 * ONE_HOUR_MS)

    assert len(posts) == 3
    by_id = {p.post_id: p for p in posts}
    expected_event_times = {m.message_id: m.event_time_ms for m in TELEGRAM_ORGANIC_MESSAGES}
    for post in posts:
        assert post.source == "telegram"
        # post_id is namespaced tg:<channel_id>:<message_id>; recover message_id
        message_id = post.post_id.split(":")[-1]
        assert post.event_time_ms == expected_event_times[message_id], (
            "RawPost.event_time_ms must equal the Telegram message.date value "
            "(platform time), never a fetch/wall-clock stamp."
        )
    assert by_id  # sanity: dict was populated


@pytest.mark.asyncio
async def test_no_telethon_import_when_offline_client_injected():
    """Injecting OfflineTelegramMTProtoClient must NEVER import telethon —
    proving no real MTProto session is opened by any test."""
    assert "telethon" not in sys.modules, (
        "telethon was already imported before this test ran; test isolation broken."
    )
    client = OfflineTelegramMTProtoClient(
        messages_by_channel={_ALLOWED_CHANNEL_ID: TELEGRAM_ORGANIC_MESSAGES}
    )
    adapter = _make_adapter(
        channel_ids=[_ALLOWED_CHANNEL_ID],
        client=client,
        api_id=123456,
        api_hash="fake-hash-not-real",
        session_string="fake-session-not-real",
    )
    posts = await adapter.fetch_posts(["solana"], DECISION_TIME_MS, 3 * ONE_HOUR_MS)
    assert len(posts) == 3
    assert "telethon" not in sys.modules, (
        "TelegramAdapter.fetch_posts imported telethon even though a client was "
        "injected — this means a real MTProto session could be opened. "
        "_build_real_client() must only run when self.client is None."
    )


def test_http_client_construction_requires_all_three_credentials():
    """HttpTelegramMTProtoClient (the REAL client) refuses partial credentials.

    This test constructs the object with an invalid arg set that raises before
    telethon is ever imported (ValueError guard fires first), so it stays
    import-safe even though telethon is not installed in this environment.
    """
    with pytest.raises(ValueError):
        HttpTelegramMTProtoClient(api_id=0, api_hash="", session_string="")


# ---------------------------------------------------------------------------
# 3. Allowlist enforcement — no crawl/search beyond MCS_TELEGRAM_CHANNEL_IDS
# ---------------------------------------------------------------------------


def test_adapter_only_queries_allowlisted_channels():
    """The adapter must NEVER request a channel outside its configured allowlist.

    HERMETIC BY CONSTRUCTION (fix for code-review F1 / STRIKE 1): this is a
    plain (non-`@pytest.mark.asyncio`) test that drives its own private event
    loop via `asyncio.run()` — the SAME pattern already used elsewhere in this
    file by `test_adapter_offline_mode_returns_empty_without_client_or_creds`
    and `test_from_env_placeholder_values_yield_offline_adapter`. This removes
    ANY dependency on pytest-asyncio's fixture/loop-scope machinery for this
    safety-critical allowlist assertion.

    The loop below also builds a BRAND NEW client + adapter object graph (zero
    fixture reuse, zero shared object) on every one of 5 independent
    iterations, turning a single sample into 5 independent, self-contained
    confirmations inside one pytest node — this is deliberate redundancy
    against exactly the kind of one-off nondeterminism the reviewer observed
    (n=2, ~1 event), without materially slowing the suite.
    """

    async def _run_once() -> tuple[list[int], list, int]:
        client = OfflineTelegramMTProtoClient(
            messages_by_channel={
                _ALLOWED_CHANNEL_ID: TELEGRAM_ORGANIC_MESSAGES,
                _OTHER_CHANNEL_ID: TELEGRAM_OTHER_CHANNEL_MESSAGES,
            }
        )
        adapter = _make_adapter(channel_ids=[_ALLOWED_CHANNEL_ID], client=client)
        posts = await adapter.fetch_posts(["solana"], DECISION_TIME_MS, 3 * ONE_HOUR_MS)
        return list(client.requested_channel_ids), posts, client.call_count

    for iteration in range(5):
        requested_channel_ids, posts, call_count = asyncio.run(_run_once())

        assert requested_channel_ids == [_ALLOWED_CHANNEL_ID], (
            f"[iteration {iteration}] Adapter queried channel(s) "
            f"{requested_channel_ids} but the allowlist is "
            f"[{_ALLOWED_CHANNEL_ID}] only — this is a crawl/search violation "
            "(MCS_TELEGRAM_CHANNEL_IDS is a pre-configured allowlist)."
        )
        assert call_count == 1, (
            f"[iteration {iteration}] Expected exactly one channel fetch call "
            f"(one allowlisted channel), got call_count={call_count}."
        )
        other_ids = {m.message_id for m in TELEGRAM_OTHER_CHANNEL_MESSAGES}
        returned_ids = {p.post_id.split(":")[-1] for p in posts}
        assert returned_ids.isdisjoint(other_ids), (
            f"[iteration {iteration}] Posts from a non-allowlisted channel "
            "leaked into the output."
        )


def test_empty_allowlist_returns_empty_even_with_client_and_creds():
    """An empty channel_ids allowlist must return [] immediately — no crawl target.

    Hermetic (F1): plain `def` + `asyncio.run()` — see
    test_adapter_only_queries_allowlisted_channels above for rationale.
    """
    client = OfflineTelegramMTProtoClient(
        messages_by_channel={_ALLOWED_CHANNEL_ID: TELEGRAM_ORGANIC_MESSAGES}
    )
    adapter = _make_adapter(
        channel_ids=[],
        client=client,
        api_id=123456,
        api_hash="fake",
        session_string="fake",
    )
    posts = asyncio.run(adapter.fetch_posts(["solana"], DECISION_TIME_MS, 3 * ONE_HOUR_MS))
    assert posts == []
    assert client.call_count == 0, (
        "The offline client must never be called when the allowlist is empty."
    )


def test_adapter_offline_mode_returns_empty_without_client_or_creds():
    """No client injected AND no credentials configured -> [] (offline/CI default)."""
    adapter = _make_adapter(channel_ids=[_ALLOWED_CHANNEL_ID], client=None)
    result = asyncio.run(adapter.fetch_posts(["solana"], DECISION_TIME_MS, ONE_HOUR_MS))
    assert result == []


# ---------------------------------------------------------------------------
# 4. Point-in-time filter — Telegram future messages excluded
# ---------------------------------------------------------------------------


def test_future_messages_excluded_by_offline_client_window():
    """The offline client itself enforces the point-in-time window.

    Hermetic (F1): plain `def` + `asyncio.run()`.
    """
    client = OfflineTelegramMTProtoClient(
        messages_by_channel={
            _ALLOWED_CHANNEL_ID: TELEGRAM_ORGANIC_MESSAGES + TELEGRAM_FUTURE_MESSAGES
        }
    )
    adapter = _make_adapter(channel_ids=[_ALLOWED_CHANNEL_ID], client=client)

    posts = asyncio.run(adapter.fetch_posts(["solana"], DECISION_TIME_MS, 3 * ONE_HOUR_MS))

    future_ids = {m.message_id for m in TELEGRAM_FUTURE_MESSAGES}
    returned_ids = {p.post_id.split(":")[-1] for p in posts}
    assert returned_ids.isdisjoint(future_ids), (
        "Telegram future messages leaked past the offline client's window filter."
    )


def test_adapter_defensively_re_filters_a_leaky_client():
    """Even if the injected client misbehaves and returns out-of-window
    messages, TelegramAdapter.fetch_posts must defensively drop them
    (belt-and-braces point-in-time enforcement, T-300a).

    MUTATION-EFFECTIVE (fix for EN3-QA-01): TELEGRAM_FUTURE_MESSAGES now
    contain the "solana" keyword, so the KEYWORD filter can no longer be the
    (incidental) reason these are excluded here. _LeakyClient deliberately
    ignores the window bounds, so the ONLY mechanism left that can exclude
    these keyword-matching future messages is adapters.py's defensive
    point-in-time re-filter
    (`if not (earliest_ms <= msg.event_time_ms <= decision_time_ms): continue`).
    Replacing that guard with a no-op now makes this test FAIL (verified by
    hand during this fix: reverting the guard to `if False: continue` breaks
    both assertions below).

    Hermetic (F1): plain `def` + `asyncio.run()`.
    """

    @dataclasses.dataclass
    class _LeakyClient:
        """A deliberately non-compliant fake that ignores the window bounds."""

        messages: list[TelegramRawMessage]

        async def iter_channel_messages(
            self, channel_id, min_event_time_ms, max_event_time_ms, limit
        ) -> list[TelegramRawMessage]:
            return self.messages  # WRONG: ignores min/max window entirely

    leaky = _LeakyClient(messages=TELEGRAM_ORGANIC_MESSAGES + TELEGRAM_FUTURE_MESSAGES)
    adapter = _make_adapter(channel_ids=[_ALLOWED_CHANNEL_ID], client=leaky)

    posts = asyncio.run(adapter.fetch_posts(["solana"], DECISION_TIME_MS, 3 * ONE_HOUR_MS))

    future_ids = {m.message_id for m in TELEGRAM_FUTURE_MESSAGES}
    returned_ids = {p.post_id.split(":")[-1] for p in posts}
    assert returned_ids.isdisjoint(future_ids), (
        "POINT-IN-TIME VIOLATION: adapter trusted a leaky client's future "
        "messages instead of defensively re-filtering by event_time_ms."
    )
    assert len(posts) == len(TELEGRAM_ORGANIC_MESSAGES)


def test_telegram_future_posts_do_not_change_tier_a_digest_count():
    """Adding Telegram future messages must not change the Tier-A post count
    (mirrors the Discord/X point-in-time invariant test).

    Hermetic (F1): plain `def` + `asyncio.run()`.
    """
    client_clean = OfflineTelegramMTProtoClient(
        messages_by_channel={_ALLOWED_CHANNEL_ID: TELEGRAM_ORGANIC_MESSAGES}
    )
    adapter_clean = _make_adapter(channel_ids=[_ALLOWED_CHANNEL_ID], client=client_clean)
    posts_clean = asyncio.run(
        adapter_clean.fetch_posts(["solana"], DECISION_TIME_MS, 3 * ONE_HOUR_MS)
    )
    digest_clean = run_tier_a(posts_clean, _ASSET, DECISION_TIME_MS)

    client_with_future = OfflineTelegramMTProtoClient(
        messages_by_channel={
            _ALLOWED_CHANNEL_ID: TELEGRAM_ORGANIC_MESSAGES + TELEGRAM_FUTURE_MESSAGES
        }
    )
    adapter_with_future = _make_adapter(
        channel_ids=[_ALLOWED_CHANNEL_ID], client=client_with_future
    )
    posts_with_future = asyncio.run(
        adapter_with_future.fetch_posts(["solana"], DECISION_TIME_MS, 3 * ONE_HOUR_MS)
    )
    digest_with_future = run_tier_a(posts_with_future, _ASSET, DECISION_TIME_MS)

    assert digest_clean.total_post_count == digest_with_future.total_post_count, (
        "POINT-IN-TIME VIOLATION: Telegram future messages changed the Tier-A "
        "post count."
    )


# ---------------------------------------------------------------------------
# 4b. ADVERSARIAL INVARIANT — Telegram coordinated shill burst LOWERS conviction
# ---------------------------------------------------------------------------


async def _score_via_telegram(
    messages: list[TelegramRawMessage], lookback_window_ms: int
) -> tuple:
    client = OfflineTelegramMTProtoClient(messages_by_channel={_ALLOWED_CHANNEL_ID: messages})
    adapter = _make_adapter(channel_ids=[_ALLOWED_CHANNEL_ID], client=client)
    posts = await adapter.fetch_posts(["solana"], DECISION_TIME_MS, lookback_window_ms)
    digest = run_tier_a(posts, _ASSET, DECISION_TIME_MS)
    scorer = TierBScorer(MockLLMBackend(base_score=0.5))
    from aats.contracts.events import EventTime

    event_time = EventTime(
        slot=max(1, DECISION_TIME_MS // 400),
        block_time_ms=DECISION_TIME_MS,
        wall_clock_ms=DECISION_TIME_MS + 75,
    )
    return scorer.score_asset(digest, event_time)


@pytest.mark.asyncio
async def test_telegram_shill_burst_produces_lower_conviction_than_organic():
    """ADVERSARIAL INVARIANT: a coordinated Telegram call-channel shill burst
    (20 near-identical messages, 90-second window, single dominant cluster)
    must produce LOWER conviction than organic, topically-distinct messages
    spread across ~14 hours -- even though TelegramAdapter cannot observe
    per-account age (every Telegram post gets the same conservative,
    never-up-weighted account defaults). The differentiator here is
    synchronicity + cluster concentration, exactly as designed.
    """
    mcs_organic, ev_organic = await _score_via_telegram(
        TELEGRAM_ORGANIC_SPREAD_MESSAGES, ONE_DAY_MS
    )
    mcs_shill, ev_shill = await _score_via_telegram(TELEGRAM_SHILL_BURST_MESSAGES, ONE_DAY_MS)

    print(
        f"\n[EN3-adversarial] organic: posts={mcs_organic.post_count} "
        f"sync={ev_organic.synchronicity:.3f} conc={ev_organic.cluster_concentration:.3f} "
        f"raw={ev_organic.raw_narrative_score:.3f} penalty={ev_organic.coordinated_shill_penalty:.3f} "
        f"conviction={mcs_organic.conviction:.3f} flag={mcs_organic.coordinated_shill_flag}\n"
        f"[EN3-adversarial] shill:   posts={mcs_shill.post_count} "
        f"sync={ev_shill.synchronicity:.3f} conc={ev_shill.cluster_concentration:.3f} "
        f"raw={ev_shill.raw_narrative_score:.3f} penalty={ev_shill.coordinated_shill_penalty:.3f} "
        f"conviction={mcs_shill.conviction:.3f} flag={mcs_shill.coordinated_shill_flag}"
    )

    assert mcs_shill.conviction < mcs_organic.conviction, (
        f"ADVERSARIAL INVARIANT VIOLATED: Telegram shill-burst conviction "
        f"{mcs_shill.conviction:.3f} must be LOWER than organic conviction "
        f"{mcs_organic.conviction:.3f}. Manufactured Telegram euphoria must "
        "never score higher than organic, spread-out discussion."
    )
    assert ev_shill.coordinated_shill_penalty > ev_organic.coordinated_shill_penalty, (
        "Shill burst must carry a strictly larger coordinated-shill penalty "
        "than the organic, spread-out fixture."
    )
    assert mcs_shill.coordinated_shill_flag is True, (
        "20 near-identical Telegram messages in a 90-second burst must raise "
        "coordinated_shill_flag."
    )


# ---------------------------------------------------------------------------
# 5. Keyword relevance filter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_keyword_filter_excludes_non_matching_messages():
    off_topic = TelegramRawMessage(
        message_id="tg_msg_offtopic",
        channel_id=_ALLOWED_CHANNEL_ID,
        sender_id="alpha_channel",
        text="Happy birthday to our mod, cake time in the lounge!",
        event_time_ms=T_MINUS_10MIN,
    )
    client = OfflineTelegramMTProtoClient(
        messages_by_channel={_ALLOWED_CHANNEL_ID: [*TELEGRAM_ORGANIC_MESSAGES, off_topic]}
    )
    adapter = _make_adapter(channel_ids=[_ALLOWED_CHANNEL_ID], client=client)

    posts = await adapter.fetch_posts(["solana"], DECISION_TIME_MS, 3 * ONE_HOUR_MS)

    returned_ids = {p.post_id.split(":")[-1] for p in posts}
    assert "tg_msg_offtopic" not in returned_ids
    assert len(posts) == len(TELEGRAM_ORGANIC_MESSAGES)


@pytest.mark.asyncio
async def test_empty_keywords_returns_all_messages_in_window():
    """No keyword filter (empty list) -> all in-window allowlisted messages pass."""
    client = OfflineTelegramMTProtoClient(
        messages_by_channel={_ALLOWED_CHANNEL_ID: TELEGRAM_ORGANIC_MESSAGES}
    )
    adapter = _make_adapter(channel_ids=[_ALLOWED_CHANNEL_ID], client=client)
    posts = await adapter.fetch_posts([], DECISION_TIME_MS, 3 * ONE_HOUR_MS)
    assert len(posts) == len(TELEGRAM_ORGANIC_MESSAGES)


# ---------------------------------------------------------------------------
# 6. Credential injection path (from_env)
# ---------------------------------------------------------------------------


def test_from_env_placeholder_values_yield_offline_adapter():
    """`.env.example`-style placeholders must resolve to None / [] — offline mode."""
    env = {
        "TELEGRAM_MTProto_API_ID": "<vault-ref: secret/aats/telegram-mtproto-api-id>",
        "TELEGRAM_MTProto_API_HASH": "<vault-ref: secret/aats/telegram-mtproto-api-hash>",
        "TELEGRAM_MTProto_SESSION": "<vault-ref: secret/aats/telegram-mtproto-session>",
        "MCS_TELEGRAM_CHANNEL_IDS": "",
    }
    adapter = TelegramAdapter.from_env(env)
    assert adapter.api_id is None
    assert adapter.api_hash is None
    assert adapter.session_string is None
    assert adapter.channel_ids == []
    assert adapter.client is None

    result = asyncio.run(adapter.fetch_posts(["solana"], DECISION_TIME_MS, ONE_HOUR_MS))
    assert result == []


def test_from_env_real_looking_values_are_parsed_correctly():
    """Real (test-fixture) credential-shaped values are parsed into the right types."""
    env = {
        "TELEGRAM_MTProto_API_ID": "987654",
        "TELEGRAM_MTProto_API_HASH": "0123456789abcdef0123456789abcdef",
        "TELEGRAM_MTProto_SESSION": "1BVtsOJ8BuFake...session...string",
        "MCS_TELEGRAM_CHANNEL_IDS": f"{_ALLOWED_CHANNEL_ID}, {_OTHER_CHANNEL_ID} ,",
    }
    adapter = TelegramAdapter.from_env(env)
    assert adapter.api_id == 987654
    assert adapter.api_hash == "0123456789abcdef0123456789abcdef"
    assert adapter.session_string == "1BVtsOJ8BuFake...session...string"
    assert adapter.channel_ids == [_ALLOWED_CHANNEL_ID, _OTHER_CHANNEL_ID]
    # Constructing from_env must NEVER open a live session (no client set).
    assert adapter.client is None


def test_from_env_malformed_channel_id_is_skipped_not_fatal():
    env = {
        "TELEGRAM_MTProto_API_ID": "1",
        "TELEGRAM_MTProto_API_HASH": "h",
        "TELEGRAM_MTProto_SESSION": "s",
        "MCS_TELEGRAM_CHANNEL_IDS": f"{_ALLOWED_CHANNEL_ID},not-an-int,{_OTHER_CHANNEL_ID}",
    }
    adapter = TelegramAdapter.from_env(env)
    assert adapter.channel_ids == [_ALLOWED_CHANNEL_ID, _OTHER_CHANNEL_ID]


def test_from_env_malformed_api_id_treated_as_unset():
    env = {
        "TELEGRAM_MTProto_API_ID": "not-an-int",
        "TELEGRAM_MTProto_API_HASH": "h",
        "TELEGRAM_MTProto_SESSION": "s",
        "MCS_TELEGRAM_CHANNEL_IDS": str(_ALLOWED_CHANNEL_ID),
    }
    adapter = TelegramAdapter.from_env(env)
    assert adapter.api_id is None


def test_adapter_repr_never_leaks_api_hash_or_session_string():
    """repr(adapter) must never contain the raw api_hash / session_string."""
    adapter = TelegramAdapter(
        api_id=42,
        api_hash="THIS-IS-A-SECRET-HASH-VALUE",
        session_string="THIS-IS-A-SECRET-SESSION-STRING",
        channel_ids=[_ALLOWED_CHANNEL_ID],
    )
    r = repr(adapter)
    assert "THIS-IS-A-SECRET-HASH-VALUE" not in r
    assert "THIS-IS-A-SECRET-SESSION-STRING" not in r


# ---------------------------------------------------------------------------
# 7. No secrets in adapter source
# ---------------------------------------------------------------------------


def test_telegram_adapter_source_has_no_hardcoded_secrets():
    import aats.sentiment.adapters as _adapters_mod

    source = inspect.getsource(_adapters_mod)
    banned = [
        "api_hash = \"",
        "api_id = 1",
        "session_string = \"1",
        "StringSession(\"1",
        "sk-",
    ]
    for pattern in banned:
        assert pattern not in source, (
            f"Potential hardcoded Telegram secret pattern '{pattern}' found in adapters.py."
        )


# ---------------------------------------------------------------------------
# 8. Pipeline integration — MCS is de-risk only, no per-post LLM call
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pipeline_ingests_telegram_posts_via_injected_offline_client():
    client = OfflineTelegramMTProtoClient(
        messages_by_channel={_ALLOWED_CHANNEL_ID: TELEGRAM_ORGANIC_MESSAGES}
    )
    adapter = _make_adapter(channel_ids=[_ALLOWED_CHANNEL_ID], client=client)
    llm = MockLLMBackend(base_score=0.5)
    scorer = TierBScorer(llm)
    pipe = MCSSentimentPipeline(
        adapters=[adapter], tier_b=scorer, lookback_window_ms=3 * ONE_HOUR_MS
    )

    mcs, ev, _news, _vel = await pipe.score(
        _ASSET, ["solana", "gem", "launch"], DECISION_TIME_MS
    )

    assert mcs.post_count == len(TELEGRAM_ORGANIC_MESSAGES)
    assert 0.0 <= mcs.conviction <= 1.0
    # No win_rate field anywhere (HONESTY CLAUSE).
    from aats.contracts.models import MCSScore

    assert "win_rate" not in MCSScore.model_fields


@pytest.mark.asyncio
async def test_pipeline_one_llm_call_for_telegram_posts():
    """N Telegram messages through the pipeline = exactly 1 LLM call."""
    client = OfflineTelegramMTProtoClient(
        messages_by_channel={_ALLOWED_CHANNEL_ID: TELEGRAM_ORGANIC_MESSAGES}
    )
    adapter = _make_adapter(channel_ids=[_ALLOWED_CHANNEL_ID], client=client)
    llm = MockLLMBackend(base_score=0.5)
    scorer = TierBScorer(llm)
    pipe = MCSSentimentPipeline(
        adapters=[adapter], tier_b=scorer, lookback_window_ms=3 * ONE_HOUR_MS
    )

    await pipe.score(_ASSET, ["solana", "gem", "launch"], DECISION_TIME_MS)

    assert llm.call_count == 1, (
        f"{len(TELEGRAM_ORGANIC_MESSAGES)} Telegram messages produced "
        f"{llm.call_count} LLM calls. Per-post LLM calls are FORBIDDEN."
    )


def test_pipeline_gather_does_not_cross_contaminate_concurrent_telegram_adapters():
    """Rule out a shared-client concurrency path in MCSSentimentPipeline.score()'s
    `asyncio.gather` over adapters (F1 follow-up requested by code review).

    Wires TWO independent TelegramAdapter instances -- distinct allowlists,
    distinct OfflineTelegramMTProtoClient instances -- into ONE pipeline.
    `pipeline.score()` awaits both adapters' `fetch_posts()` concurrently via
    `asyncio.gather` (see aats/sentiment/pipeline.py `score()` Stage 1). This
    proves concurrent scheduling never lets one adapter's client observe (or
    be queried for) the other adapter's channel_id, and that each channel's
    posts are attributed only to their own adapter/client.

    Hermetic (F1): plain `def` + `asyncio.run()`.
    """
    channel_a = _ALLOWED_CHANNEL_ID
    channel_b = 3_003_003_003  # a second, DISTINCT allowlisted channel

    messages_a = [
        TelegramRawMessage(
            message_id="tg_a_001",
            channel_id=channel_a,
            sender_id="alpha_channel",
            text="Solana channel A organic call, no hype language.",
            event_time_ms=T_MINUS_30MIN,
        )
    ]
    messages_b = [
        TelegramRawMessage(
            message_id="tg_b_001",
            channel_id=channel_b,
            sender_id="beta_channel",
            text="Solana channel B organic call, different desk entirely.",
            event_time_ms=T_MINUS_30MIN,
        )
    ]

    client_a = OfflineTelegramMTProtoClient(messages_by_channel={channel_a: messages_a})
    client_b = OfflineTelegramMTProtoClient(messages_by_channel={channel_b: messages_b})
    adapter_a = _make_adapter(channel_ids=[channel_a], client=client_a)
    adapter_b = _make_adapter(channel_ids=[channel_b], client=client_b)

    llm = MockLLMBackend(base_score=0.5)
    scorer = TierBScorer(llm)
    pipe = MCSSentimentPipeline(
        adapters=[adapter_a, adapter_b], tier_b=scorer, lookback_window_ms=3 * ONE_HOUR_MS
    )

    mcs, _ev, _news, _vel = asyncio.run(pipe.score(_ASSET, ["solana"], DECISION_TIME_MS))

    assert client_a.requested_channel_ids == [channel_a], (
        f"adapter_a's client was queried for {client_a.requested_channel_ids} -- "
        f"asyncio.gather concurrency must never leak channel_b={channel_b} "
        "into adapter_a's client."
    )
    assert client_b.requested_channel_ids == [channel_b], (
        f"adapter_b's client was queried for {client_b.requested_channel_ids} -- "
        f"asyncio.gather concurrency must never leak channel_a={channel_a} "
        "into adapter_b's client."
    )
    assert mcs.post_count == len(messages_a) + len(messages_b), (
        "Concurrent gather over two independent Telegram adapters must ingest "
        "both channels' posts without loss or duplication."
    )


def test_telegram_mcs_cannot_size_up():
    """High Telegram-sourced conviction must NEVER change position size upward."""
    from aats.sentiment.pipeline import assert_mcs_cannot_size_up

    original_size = 3.0
    for conviction in [0.0, 0.5, 0.99, 1.0]:
        result = assert_mcs_cannot_size_up(conviction, original_size)
        assert result == original_size, (
            f"Telegram-sourced MCS conviction={conviction} changed size to {result}. "
            "MCS may only DE-RISK — never size up."
        )
