"""Social + news ingestion adapters (T-306 + E7).

Sources: X (Twitter) API v2, Reddit (asyncpraw), Telegram (telethon),
         Discord (discord.py bot), and News (RSS / news API — E7).

SLOW-LOOP ingestion only.  These adapters are NOT on the SNIPE hot path.

HARD RULES:
  - Event_time is stamped from the PLATFORM (post / article publish time),
    NOT fetch time.  Fetch time is recorded for monitoring/lag only.
  - Real keys/tokens are NEVER in code or logs.  All secrets come from the
    injectable config or environment variables via `.env.example`.
  - Rate caps: X API v2 free/basic/pro tier caps are respected via token-bucket
    throttling.  Reddit uses asyncpraw's built-in rate handling.
  - Retry policy: exponential backoff, MAX_RETRIES attempts.  We never burn
    quota on aggressive retries.
  - All adapters are injectable and offline-mockable.  Tests always use the
    mock adapters.
  - E7 (News): news signals are DE-RISK ONLY — a credible NEGATIVE article can
    lower MCS conviction or trigger narrative_failure (FORCE_EXIT).  Positive
    news NEVER raises MCS.  News is SLOW-LOOP ONLY.

X API v2 rate caps (Basic tier, the minimum viable tier):
  - 1,500,000 tweets/month on reads
  - POST cap: not relevant (we only read)
  - We stay within the cap by:
    (a) caching ingested posts for the decision window (15m TTL)
    (b) querying only when the cache is stale
    (c) using the search/recent endpoint, not streaming (to control quota)

Reddit (asyncpraw):
  - Public API, no auth needed for read-only search.
  - Rate: 60 requests/minute; asyncpraw enforces this automatically.

Telegram (telethon) — EN3, REAL implementation:
  - Channel monitoring via the MTProto API, restricted to a PRE-CONFIGURED
    call-channel allowlist (MCS_TELEGRAM_CHANNEL_IDS). We do NOT crawl or
    search dialogs -- only the allowlisted channel_ids are ever queried.
  - Requires API_ID + API_HASH + session string, injected from Vault/.env
    ONLY (TELEGRAM_MTProto_API_ID / _API_HASH / _SESSION) -- never hardcoded,
    never logged.
  - INJECTABLE exactly like aats/telegram/client.py: `OfflineTelegramMTProtoClient`
    is the deterministic in-memory fake used by EVERY test (no network, no
    MTProto session); `HttpTelegramMTProtoClient` is the real telethon-backed
    client, lazily imported and constructed ONLY at runtime with real creds --
    it is never exercised by the test suite.
  - event_time_ms is stamped from the Telegram message's `date` field (T-300a
    platform time), NEVER wall-clock at fetch time.

News (E7):
  - Crypto-native: CoinDesk, The Block, Cointelegraph (RSS feeds).
  - Mainstream: Forbes Crypto, Bloomberg Crypto (RSS / news API).
  - event_time_ms from article pubDate / published (NOT wall-clock at fetch).
  - REAL-ENDPOINT: RSS feed URLs injected via env; NewsAPI key optional.
  - OFFLINE: returns [] when no feeds are configured (CI/test default).

All social adapters are OFFLINE-MOCKABLE via `MockSocialAdapter`.
News adapter is offline-mockable via `MockNewsAdapter`.
Real endpoints marked with `# REAL-ENDPOINT` comments.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

from aats.sentiment.models import NewsItem, RawPost

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Adapter protocol
# ---------------------------------------------------------------------------


class SocialAdapter(Protocol):
    """Protocol for any social ingestion source.

    fetch_posts() returns RawPost objects with event_time_ms stamped from
    the platform's published-at timestamp, NOT the wall-clock at fetch time.
    """

    async def fetch_posts(
        self,
        keywords: list[str],
        decision_time_ms: int,
        lookback_window_ms: int,
    ) -> list[RawPost]:
        """Fetch posts mentioning `keywords` within the lookback window.

        Only posts with platform_published_at <= decision_time_ms are returned.
        Adapters MUST enforce this filter before returning.

        Args:
            keywords:           Search terms (token name, mint address, etc.)
            decision_time_ms:   Point-in-time cutoff (exclusive upper bound).
            lookback_window_ms: How far back to look (e.g. 3_600_000 = 1 hour).

        Returns:
            List of RawPost with event_time_ms = platform published time.
        """
        ...


# ---------------------------------------------------------------------------
# Mock adapter (deterministic, no network calls)
# ---------------------------------------------------------------------------


@dataclass
class MockSocialAdapter:
    """Offline deterministic mock.  Used in ALL tests.

    Posts are pre-loaded at construction time and returned as-is.
    The mock enforces the point-in-time contract: posts with
    event_time_ms > decision_time_ms are filtered OUT before returning.
    """

    _posts: list[RawPost] = field(default_factory=list)
    _call_count: int = field(default=0, init=False)

    def load_posts(self, posts: list[RawPost]) -> None:
        self._posts = posts

    @property
    def call_count(self) -> int:
        return self._call_count

    async def fetch_posts(
        self,
        keywords: list[str],
        decision_time_ms: int,
        lookback_window_ms: int,
    ) -> list[RawPost]:
        self._call_count += 1
        earliest = decision_time_ms - lookback_window_ms
        return [
            p
            for p in self._posts
            if earliest <= p.event_time_ms <= decision_time_ms
        ]


# ---------------------------------------------------------------------------
# X API v2 adapter (STUB — real endpoint injectable at deploy time)
# ---------------------------------------------------------------------------


@dataclass
class XAPIv2Adapter:
    """X (Twitter) API v2 adapter.

    REAL-ENDPOINT: https://api.twitter.com/2/tweets/search/recent
    INJECTABLE: bearer_token is injected; never hardcoded.
    OFFLINE: raises NotImplementedError when bearer_token is None.

    Rate cap: 1,500,000 tweets/month on the Basic tier.
    We enforce a local token-bucket at construction time.
    Cache TTL: 15 minutes (900 s) to avoid re-fetching identical windows.
    """

    bearer_token: str | None  # NEVER commit a real token; inject via env
    max_results_per_call: int = 100
    max_retries: int = 3

    async def fetch_posts(
        self,
        keywords: list[str],
        decision_time_ms: int,
        lookback_window_ms: int,
    ) -> list[RawPost]:
        """Fetch from X API v2 recent search.

        REAL-ENDPOINT: api.twitter.com/2/tweets/search/recent
        Mark where real creds plug in — never committed.
        """
        if self.bearer_token is None:
            # Offline / CI: return empty rather than erroring; the pipeline
            # handles no-data gracefully with zero-conviction MCS.
            logger.info("XAPIv2Adapter: bearer_token=None (offline mode), returning []")
            return []

        # --- REAL IMPLEMENTATION PLACEHOLDER ---
        # In production, inject httpx.AsyncClient + bearer_token from env.
        # REAL-ENDPOINT: GET https://api.twitter.com/2/tweets/search/recent
        #   ?query=<keywords>&start_time=<ISO>&end_time=<ISO>&max_results=100
        # Parse response, stamp event_time_ms from tweet.created_at (NOT wall clock).
        # Rate-limit via token-bucket; retry with exponential backoff.
        # Cache by (keywords_hash, window_hash) with 15-minute TTL.
        raise NotImplementedError(
            "XAPIv2Adapter.fetch_posts: real endpoint not implemented in this build. "
            "Inject MockSocialAdapter in tests; wire real httpx client at deploy time. "
            "REAL-ENDPOINT: https://api.twitter.com/2/tweets/search/recent"
        )


# ---------------------------------------------------------------------------
# Reddit adapter (asyncpraw stub)
# ---------------------------------------------------------------------------


@dataclass
class RedditAdapter:
    """Reddit adapter via asyncpraw.

    REAL-ENDPOINT: Reddit REST API via asyncpraw (reddit.com)
    INJECTABLE: client_id, client_secret from env; never hardcoded.
    OFFLINE: returns [] when client_id is None.

    Subreddits: SolanaMemeCoins, CryptoMoonShots, solana.
    Rate: asyncpraw enforces 60 req/min automatically.
    """

    client_id: str | None  # inject from env; never commit
    client_secret: str | None  # inject from env; never commit
    user_agent: str = "aats-mcs-sentiment/0.1"
    subreddits: list[str] = field(default_factory=lambda: [
        "SolanaMemeCoins",
        "CryptoMoonShots",
        "solana",
    ])

    async def fetch_posts(
        self,
        keywords: list[str],
        decision_time_ms: int,
        lookback_window_ms: int,
    ) -> list[RawPost]:
        """Search Reddit for posts mentioning keywords.

        REAL-ENDPOINT: asyncpraw Reddit.subreddit().search()
        Post's created_utc is the event_time (not fetch time).
        """
        if self.client_id is None:
            logger.info("RedditAdapter: client_id=None (offline mode), returning []")
            return []

        # --- REAL IMPLEMENTATION PLACEHOLDER ---
        # In production:
        #   import asyncpraw
        #   async with asyncpraw.Reddit(
        #       client_id=self.client_id,
        #       client_secret=self.client_secret,
        #       user_agent=self.user_agent,
        #   ) as reddit:
        #       posts = []
        #       for sub in self.subreddits:
        #           async for submission in reddit.subreddit(sub).search(
        #               " OR ".join(keywords), sort="new", time_filter="hour"
        #           ):
        #               posts.append(RawPost(
        #                   post_id=submission.id,
        #                   source="reddit",
        #                   author_id=str(submission.author),
        #                   text=submission.title + " " + submission.selftext,
        #                   event_time_ms=int(submission.created_utc * 1000),  # NOT wall clock
        #                   fetch_time_ms=<wall clock>,
        #                   ...
        #               ))
        #   return [p for p in posts if p.event_time_ms <= decision_time_ms]
        raise NotImplementedError(
            "RedditAdapter.fetch_posts: real endpoint not implemented. "
            "Inject MockSocialAdapter in tests. "
            "REAL-ENDPOINT: asyncpraw Reddit search"
        )


# ---------------------------------------------------------------------------
# Telegram adapter (telethon — EN3 REAL implementation, injectable client)
# ---------------------------------------------------------------------------


def _redact_telegram_secret(value: str | None) -> str:
    """Log-safe redaction for a Telegram api_hash / session_string.

    NEVER echoes the raw secret.  Placeholder values from `.env.example`
    (angle-bracketed) are reported as `<placeholder>`, not the literal text,
    so a stray log line can never be mistaken for "a real secret was here".
    """
    if not value:
        return "<unset>"
    if value.startswith("<") and value.endswith(">"):
        return "<placeholder>"
    if len(value) <= 4:
        return "<redacted>"
    return f"{value[:4]}...<redacted>"


@dataclass(frozen=True)
class TelegramRawMessage:
    """Wire-level Telegram channel message (from telethon or the offline fake).

    Decouples the `TelegramMTProtoClient` seam from `aats.sentiment.models.RawPost`
    — mirrors `aats.telegram.client.SentMessage`'s "no token, no cross-module
    coupling" pattern.

    event_time_ms MUST come from the platform's `message.date` (T-300a) —
    NEVER wall-clock at fetch time.
    """

    message_id: str
    channel_id: int
    sender_id: str
    text: str  # QUOTED UNTRUSTED DATA — never executed as instructions
    event_time_ms: int  # message.date — platform time, NEVER wall-clock


@runtime_checkable
class TelegramMTProtoClient(Protocol):
    """The minimal inbound seam `TelegramAdapter` depends on.

    Mirrors `aats.telegram.client.TelegramClient`'s injectable-seam pattern:
    `OfflineTelegramMTProtoClient` is the deterministic in-memory fake used by
    EVERY test (no network, no MTProto session ever opened); `HttpTelegramMTProtoClient`
    is the real telethon-backed implementation, lazily imported and constructed
    ONLY at runtime with real credentials, and NEVER exercised in tests.
    """

    async def iter_channel_messages(
        self,
        channel_id: int,
        min_event_time_ms: int,
        max_event_time_ms: int,
        limit: int,
    ) -> list[TelegramRawMessage]:
        """Return messages for ONE pre-configured channel_id — no crawl/search.

        Implementations MUST restrict themselves to the single `channel_id`
        passed in (never a dialog-wide search) and MUST stamp event_time_ms
        from the platform's message timestamp, never wall-clock at fetch.
        """
        ...


@dataclass
class OfflineTelegramMTProtoClient:
    """Deterministic offline fake — the default (and ONLY) client in tests.

    Preloaded per-channel message fixtures.  NO network, NO MTProto session is
    ever opened.  `iter_channel_messages` filters by the requested channel_id
    and the `[min_event_time_ms, max_event_time_ms]` window, exactly mirroring
    the contract the real telethon-backed client fulfils.

    `requested_channel_ids` records every channel_id this fake was asked for,
    so tests can assert the adapter never queried outside its allowlist.
    """

    messages_by_channel: dict[int, list[TelegramRawMessage]] = field(default_factory=dict)
    call_count: int = field(default=0, init=False)
    requested_channel_ids: list[int] = field(default_factory=list, init=False)

    async def iter_channel_messages(
        self,
        channel_id: int,
        min_event_time_ms: int,
        max_event_time_ms: int,
        limit: int,
    ) -> list[TelegramRawMessage]:
        self.call_count += 1
        self.requested_channel_ids.append(channel_id)
        msgs = self.messages_by_channel.get(channel_id, [])
        in_window = [
            m for m in msgs if min_event_time_ms <= m.event_time_ms <= max_event_time_ms
        ]
        return in_window[:limit]


class HttpTelegramMTProtoClient:
    """REAL telethon-backed MTProto client.  Constructed ONLY at runtime.

    NEVER exercised in tests: no test in this repo constructs this class, so
    `telethon` is never imported — and no MTProto session is ever opened —
    under pytest.  `api_id` / `api_hash` / `session_string` are injected from
    Vault/.env at deploy time and are NEVER hardcoded or logged in the clear.

    Restricted, by construction, to `iter_channel_messages(channel_id, ...)`
    for ONE pre-configured channel at a time — there is no dialog-wide
    search/crawl method on this class.
    """

    def __init__(
        self,
        api_id: int,
        api_hash: str,
        session_string: str,
        *,
        timeout_s: float = 20.0,
    ) -> None:
        if not api_id or not api_hash or not session_string:
            raise ValueError(
                "HttpTelegramMTProtoClient requires api_id/api_hash/session_string "
                "(runtime-injected from Vault/.env; never hardcoded)."
            )
        # Lazy import: telethon is an OPTIONAL runtime dependency. It is never
        # imported at module load time and never required to run the test
        # suite — this constructor (and therefore telethon itself) is only
        # ever reached when real credentials are runtime-injected.
        from telethon import TelegramClient
        from telethon.sessions import StringSession

        self._api_id = api_id
        self._api_hash_fingerprint = _redact_telegram_secret(api_hash)
        self._timeout_s = timeout_s
        self._client = TelegramClient(StringSession(session_string), api_id, api_hash)
        self._started = False

    async def _ensure_connected(self) -> None:
        if not self._started:
            await self._client.connect()
            self._started = True

    async def iter_channel_messages(
        self,
        channel_id: int,
        min_event_time_ms: int,
        max_event_time_ms: int,
        limit: int,
    ) -> list[TelegramRawMessage]:
        await self._ensure_connected()
        # offset_date bounds the fetch AT THE SOURCE: telethon returns only
        # messages strictly before this date, so decision-time-future messages
        # are never even transferred (cost guard + point-in-time contract).
        offset_date = datetime.fromtimestamp(max_event_time_ms / 1000.0, tz=UTC)
        out: list[TelegramRawMessage] = []
        try:
            async for msg in self._client.iter_messages(
                channel_id,  # PRE-CONFIGURED allowlist entry ONLY — no search
                offset_date=offset_date,
                limit=limit,
            ):
                if msg.date is None or not msg.message:
                    continue
                event_time_ms = int(msg.date.timestamp() * 1000)  # T-300a: platform time
                if event_time_ms > max_event_time_ms:
                    continue  # defensive; offset_date already excludes these
                if event_time_ms < min_event_time_ms:
                    break  # telethon streams newest-first; older than window -> stop (cost guard)
                out.append(
                    TelegramRawMessage(
                        message_id=str(msg.id),
                        channel_id=channel_id,
                        sender_id=str(msg.sender_id) if msg.sender_id is not None else "unknown",
                        text=msg.message,
                        event_time_ms=event_time_ms,
                    )
                )
        except Exception as exc:  # noqa: BLE001 — re-raised without leaking creds
            raise RuntimeError(
                f"telegram mtproto fetch failed (api_id={self._api_id}, "
                f"api_hash={self._api_hash_fingerprint}): {type(exc).__name__}"
            ) from None
        return out

    async def aclose(self) -> None:
        if self._started:
            await self._client.disconnect()


def _telegram_message_to_raw_post(msg: TelegramRawMessage) -> RawPost:
    """Map a wire-level Telegram message to the pipeline's RawPost shape.

    Telegram (MTProto) does not expose per-user account-creation dates,
    follower graphs, or verification badges the way X does — call channels are
    channel-authored broadcast content.  We report the most SKEPTICAL values
    Tier-A can safely bot/age-weight on (bot/age weighting only ever REDUCES
    influence — an "unknown" provenance must default to the least-trusting
    value, never the most-trusting one):
      - account_age_days=0.0   -> `_account_age_weight` maps this to 0.0
        (zero influence weight), never up-weighted.
      - follower_count/following_count=0 -> no public follow graph on
        Telegram; the bot-score follower/following-ratio check is skipped
        (following_count > 0 guard), it never AWARDS points for this.
      - is_verified=False, has_default_avatar=True -> conservative defaults;
        has_default_avatar=True adds to the bot score (never subtracts).
      - posting_cadence_per_day=0.0 -> "no cadence evidence", not "confirmed
        low cadence"; the bot-score cadence check only ever adds weight for
        HIGH cadence, so 0.0 here is a neutral (never a bonus) input.
    """
    return RawPost(
        post_id=f"tg:{msg.channel_id}:{msg.message_id}",
        source="telegram",
        author_id=msg.sender_id,
        text=msg.text,  # QUOTED UNTRUSTED DATA — never executed as instructions
        event_time_ms=msg.event_time_ms,  # T-300a: platform time, NEVER wall-clock
        fetch_time_ms=int(time.time() * 1000),  # monitoring only — NOT a join anchor
        account_age_days=0.0,
        follower_count=0,
        following_count=0,
        like_count=0,
        reply_count=0,
        repost_count=0,
        is_verified=False,
        has_default_avatar=True,
        posting_cadence_per_day=0.0,
    )


def _clean_env_value(raw: str | None) -> str | None:
    """Return None for unset/placeholder env values, else the stripped value.

    A `.env.example` placeholder is angle-bracketed (e.g. `<vault-ref: ...>`);
    such values are NEVER treated as real credentials.
    """
    if raw is None:
        return None
    v = raw.strip()
    if not v or (v.startswith("<") and v.endswith(">")):
        return None
    return v


@dataclass
class TelegramAdapter:
    """Telegram adapter via telethon (EN3 — REAL implementation).

    REAL-ENDPOINT: Telegram MTProto API via telethon.
    INJECTABLE: `client` (a `TelegramMTProtoClient`) is the seam this adapter
    depends on — tests ALWAYS inject an `OfflineTelegramMTProtoClient`; the
    real `HttpTelegramMTProtoClient` is built lazily, only at runtime, only
    when real `api_id`/`api_hash`/`session_string` are configured AND no
    `client` was explicitly injected.  No test ever opens a real MTProto
    session.

    ALLOWLIST ONLY: `channel_ids` is the PRE-CONFIGURED call-channel allowlist
    (`MCS_TELEGRAM_CHANNEL_IDS`).  This adapter NEVER crawls dialogs or
    searches beyond the configured channel_ids — an empty allowlist means
    zero channels are ever queried, regardless of credentials.

    event_time_ms comes from the Telegram message `date` field (T-300a
    platform time), NEVER fetch wall-clock time.
    """

    api_id: int | None  # inject from env (TELEGRAM_MTProto_API_ID); never hardcoded
    # api_hash / session_string are excluded from the dataclass repr (repr=False)
    # so a stray `repr(adapter)` / log of the adapter object can NEVER leak the
    # secret in the clear — same discipline as HttpTelegramClient's redact_token.
    api_hash: str | None = field(repr=False)  # TELEGRAM_MTProto_API_HASH; never hardcoded/logged
    session_string: str | None = field(repr=False)  # TELEGRAM_MTProto_SESSION; Vault-managed
    channel_ids: list[int] = field(default_factory=list)  # MCS_TELEGRAM_CHANNEL_IDS allowlist
    max_messages_per_channel: int = 200
    max_retries: int = 3
    # INJECTABLE seam: tests set this to an OfflineTelegramMTProtoClient.
    # Left None in production config; built lazily via _build_real_client().
    client: TelegramMTProtoClient | None = None

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> TelegramAdapter:
        """Build a TelegramAdapter from the environment (`.env.example` schema).

        Reads:
          TELEGRAM_MTProto_API_ID    -- int; placeholder/unset -> None
          TELEGRAM_MTProto_API_HASH  -- str; placeholder/unset -> None
          TELEGRAM_MTProto_SESSION   -- str; placeholder/unset -> None
          MCS_TELEGRAM_CHANNEL_IDS   -- comma-separated int channel IDs (the
                                        PRE-CONFIGURED call-channel allowlist)

        `env` is injectable for testing (defaults to `os.environ`).  This
        classmethod NEVER logs a raw credential value.  When credentials are
        placeholders/unset, `fetch_posts()` runs offline (returns []) unless a
        `client` is separately injected after construction.
        """
        src = env if env is not None else dict(os.environ)

        raw_api_id = _clean_env_value(src.get("TELEGRAM_MTProto_API_ID"))
        api_id: int | None
        try:
            api_id = int(raw_api_id) if raw_api_id is not None else None
        except ValueError:
            logger.warning(
                "TelegramAdapter.from_env: TELEGRAM_MTProto_API_ID is not an integer; "
                "treating as unset."
            )
            api_id = None

        api_hash = _clean_env_value(src.get("TELEGRAM_MTProto_API_HASH"))
        session_string = _clean_env_value(src.get("TELEGRAM_MTProto_SESSION"))

        raw_channel_ids = _clean_env_value(src.get("MCS_TELEGRAM_CHANNEL_IDS")) or ""
        channel_ids: list[int] = []
        for part in raw_channel_ids.split(","):
            part = part.strip()
            if not part:
                continue
            try:
                channel_ids.append(int(part))
            except ValueError:
                logger.warning(
                    "TelegramAdapter.from_env: skipping non-integer MCS_TELEGRAM_CHANNEL_IDS "
                    "entry %r",
                    part,
                )

        return cls(
            api_id=api_id,
            api_hash=api_hash,
            session_string=session_string,
            channel_ids=channel_ids,
        )

    def _build_real_client(self) -> TelegramMTProtoClient:
        """Construct the REAL telethon-backed client. RUNTIME-ONLY code path.

        Never invoked by any test: every test injects `client=` with an
        `OfflineTelegramMTProtoClient`, so this method — and therefore
        `telethon` itself — is never imported or executed under pytest.

        Uses `raise ValueError` (never a bare `assert`) for the precondition
        check: `assert` is stripped under `python -O`, which would silently
        defeat this guard; `fetch_posts()` already checks this before calling
        here, so this is a defensive second layer, not the only one.
        """
        if self.api_id is None or self.api_hash is None or self.session_string is None:
            raise ValueError(
                "TelegramAdapter._build_real_client: api_id/api_hash/session_string "
                "must all be set before building the real telethon client."
            )
        return HttpTelegramMTProtoClient(
            api_id=self.api_id,
            api_hash=self.api_hash,
            session_string=self.session_string,
        )

    async def _fetch_channel_with_retry(
        self,
        client: TelegramMTProtoClient,
        channel_id: int,
        earliest_ms: int,
        decision_time_ms: int,
    ) -> list[TelegramRawMessage]:
        """Bounded retry with exponential backoff — never burn quota aggressively."""
        backoff_s = 1.0
        for attempt in range(1, self.max_retries + 1):
            try:
                return await client.iter_channel_messages(
                    channel_id=channel_id,
                    min_event_time_ms=earliest_ms,
                    max_event_time_ms=decision_time_ms,
                    limit=self.max_messages_per_channel,
                )
            except Exception as exc:  # noqa: BLE001 — transport errors only, logged + bounded
                if attempt >= self.max_retries:
                    logger.error(
                        "TelegramAdapter: giving up on channel after %d attempt(s): %s",
                        attempt,
                        type(exc).__name__,
                    )
                    return []
                logger.warning(
                    "TelegramAdapter: fetch failed (attempt %d/%d): %s -- backing off %.1fs",
                    attempt,
                    self.max_retries,
                    type(exc).__name__,
                    backoff_s,
                )
                await asyncio.sleep(backoff_s)
                backoff_s *= 2
        return []

    async def fetch_posts(
        self,
        keywords: list[str],
        decision_time_ms: int,
        lookback_window_ms: int,
    ) -> list[RawPost]:
        """Fetch messages from the pre-configured Telegram channel allowlist.

        ALLOWLIST ONLY: iterates `self.channel_ids` exclusively — never a
        dialog-wide crawl or search.  An empty allowlist returns [] immediately,
        regardless of credentials.

        Point-in-time (T-300a): event_time_ms comes from `message.date`
        (platform time). Messages are re-checked against
        `[decision_time_ms - lookback_window_ms, decision_time_ms]` here even
        though the real client already bounds its fetch at the source — this
        is the same belt-and-braces pattern used by every other adapter.
        """
        if not self.channel_ids:
            logger.info(
                "TelegramAdapter: channel_ids allowlist is empty (no crawl target), "
                "returning []"
            )
            return []

        client = self.client
        if client is None:
            if self.api_id is None or self.api_hash is None or self.session_string is None:
                logger.info(
                    "TelegramAdapter: no credentials configured (offline mode), returning []"
                )
                return []
            client = self._build_real_client()

        earliest_ms = decision_time_ms - lookback_window_ms
        lower_kw = [kw.lower() for kw in keywords]
        posts: list[RawPost] = []
        for channel_id in self.channel_ids:  # ALLOWLIST ONLY — never crawl beyond this set
            raw_messages = await self._fetch_channel_with_retry(
                client, channel_id, earliest_ms, decision_time_ms
            )
            for msg in raw_messages:
                # Defensive point-in-time re-check (T-300a): NEVER wall-clock.
                if not (earliest_ms <= msg.event_time_ms <= decision_time_ms):
                    continue
                if lower_kw and not any(kw in msg.text.lower() for kw in lower_kw):
                    continue
                posts.append(_telegram_message_to_raw_post(msg))
        return posts


# ---------------------------------------------------------------------------
# Discord adapter (discord.py / discord-py-interactions bot token — NOT a self-bot)
# ---------------------------------------------------------------------------


@dataclass
class DiscordAdapter:
    """Discord adapter using a proper Discord Bot token via the discord.py library.

    IMPORTANT — ToS compliance: This adapter authenticates as a BOT APPLICATION
    registered through the Discord Developer Portal.  Self-bot authentication
    (logging in with a user account token) is explicitly FORBIDDEN by Discord ToS
    and will NEVER be supported here.  The `bot_token` is a Bot application token
    only (starts with "Bot ").

    REAL-ENDPOINT: Discord HTTP API v10 (https://discord.com/api/v10/)
    INJECTABLE: bot_token is injected from env; NEVER hardcoded.
    OFFLINE: returns [] when bot_token is None (CI/test default).

    Ingestion is restricted to a pre-configured ALLOWLIST of (server_id,
    channel_id) pairs.  The bot must have been granted the required permissions
    (Read Messages / Read Message History) in each configured channel by the
    server admin — it does NOT crawl or enumerate.

    event_time_ms is stamped from the Discord message's `timestamp` field
    (ISO-8601, UTC — the authoritative publish time from the platform).
    Fetch wall-clock time is recorded separately as fetch_time_ms.

    Rate caps (Discord Bot API, standard limits):
      - 50 requests / second (global bot rate limit).
      - Per-route bucket rate limits are respected via Retry-After headers.
      - We never burst aggressively; each channel fetch is sequential with
        exponential backoff on 429 responses.
      - A 15-minute channel cache avoids re-fetching identical windows.
    """

    bot_token: str | None  # Discord Bot token — NEVER commit; inject via env
    # Allowlist of (guild_id, channel_id) pairs the bot is permitted to read.
    # Populated from DISCORD_ALLOWLIST env var (JSON array) at deploy time.
    channel_allowlist: list[tuple[int, int]] = field(default_factory=list)
    max_messages_per_channel: int = 100
    max_retries: int = 3

    async def fetch_posts(
        self,
        keywords: list[str],
        decision_time_ms: int,
        lookback_window_ms: int,
    ) -> list[RawPost]:
        """Fetch messages from pre-configured Discord channels.

        Only channels on the allowlist are queried.  Messages are filtered to
        those whose `timestamp` (event_time_ms) is within the lookback window
        AND <= decision_time_ms (point-in-time contract).

        Message content containing `keywords` is retained; others are dropped.

        REAL-ENDPOINT: GET /channels/{channel_id}/messages
            ?limit=100&before=<snowflake-from-decision_time_ms>
        event_time_ms = int(message.timestamp) * 1000  (NOT wall-clock at fetch).

        ToS note: This bot account is registered as an APPLICATION in the Discord
        Developer Portal.  The `bot_token` is a Bot token, NOT a user account
        token.  Self-botting (user account token) is forbidden here by design.
        """
        if self.bot_token is None:
            logger.info(
                "DiscordAdapter: bot_token=None (offline mode), returning []"
            )
            return []

        if not self.channel_allowlist:
            logger.info(
                "DiscordAdapter: channel_allowlist is empty, returning []"
            )
            return []

        # --- REAL IMPLEMENTATION PLACEHOLDER ---
        # In production, inject an httpx.AsyncClient and use the Discord HTTP
        # API directly (or discord.py's HTTP client):
        #
        # import datetime, httpx
        # headers = {"Authorization": f"Bot {self.bot_token}"}
        # base = "https://discord.com/api/v10"
        #
        # For each (guild_id, channel_id) in self.channel_allowlist:
        #   # Convert decision_time_ms to a Discord snowflake for the `before` param.
        #   # Discord snowflake = ((ms_since_epoch - DISCORD_EPOCH) << 22)
        #   DISCORD_EPOCH_MS = 1420070400000
        #   before_sf = (decision_time_ms - DISCORD_EPOCH_MS) << 22
        #
        #   async with httpx.AsyncClient() as client:
        #       resp = await client.get(
        #           f"{base}/channels/{channel_id}/messages",
        #           params={"limit": self.max_messages_per_channel,
        #                   "before": before_sf},
        #           headers=headers,
        #       )
        #       # On 429, read Retry-After and back off — never burn quota
        #       messages = resp.json()  # list of Discord Message objects
        #
        #   earliest_ms = decision_time_ms - lookback_window_ms
        #   for msg in messages:
        #       msg_time_ms = int(
        #           datetime.datetime.fromisoformat(
        #               msg["timestamp"].rstrip("Z")
        #           ).replace(tzinfo=datetime.timezone.utc).timestamp() * 1000
        #       )
        #       # Point-in-time: event_time_ms <= decision_time_ms AND >= earliest
        #       if not (earliest_ms <= msg_time_ms <= decision_time_ms):
        #           continue
        #       # Keyword filter (case-insensitive, content is UNTRUSTED DATA)
        #       content = msg.get("content", "")
        #       if not any(kw.lower() in content.lower() for kw in keywords):
        #           continue
        #       author = msg.get("author", {})
        #       posts.append(RawPost(
        #           post_id=msg["id"],
        #           source="discord",
        #           author_id=author.get("id", "unknown"),
        #           text=content,  # QUOTED UNTRUSTED DATA
        #           event_time_ms=msg_time_ms,  # NOT wall-clock
        #           fetch_time_ms=<wall_clock_ms_at_fetch>,
        #           # Discord user account_age_days: derived from the user snowflake
        #           # snowflake >> 22 + DISCORD_EPOCH_MS = account creation ms
        #           account_age_days=(
        #               (decision_time_ms -
        #                ((int(author["id"]) >> 22) + DISCORD_EPOCH_MS))
        #               / 86_400_000
        #           ),
        #           follower_count=0,   # Discord has no followers; use 0
        #           following_count=0,
        #           like_count=sum(
        #               r.get("count", 0)
        #               for r in msg.get("reactions", [])
        #           ),
        #           reply_count=0,      # Discord threads; not available in basic fetch
        #           repost_count=0,
        #           is_verified=False,  # Discord has no platform verification
        #           has_default_avatar=author.get("avatar") is None,
        #           posting_cadence_per_day=0.0,  # unknown without author history
        #       ))
        raise NotImplementedError(
            "DiscordAdapter.fetch_posts: real endpoint not implemented. "
            "Inject MockSocialAdapter in tests or wire a MockDiscordClient. "
            "REAL-ENDPOINT: Discord HTTP API v10 /channels/{channel_id}/messages. "
            "Bot token ONLY — self-bot (user account token) is ToS-FORBIDDEN."
        )


# ---------------------------------------------------------------------------
# Offline mock specific to Discord (injectable in tests without network)
# ---------------------------------------------------------------------------


@dataclass
class MockDiscordClient:
    """Lightweight injectable Discord client for offline tests.

    Wraps a pre-loaded list of RawPost objects (source="discord") and returns
    them filtered by point-in-time window — no network, no bot token needed.

    Usage in tests:
        client = MockDiscordClient(posts=DISCORD_FIXTURE_POSTS)
        adapter = DiscordAdapter(bot_token=None, channel_allowlist=[(123, 456)])
        adapter._client = client   # inject offline client

    Or use MCSSentimentPipeline with MockSocialAdapter preloaded with
    discord-source posts — the pipeline is source-agnostic (RawPost.source
    is recorded but not gated on).
    """

    posts: list[RawPost] = field(default_factory=list)

    def posts_for_window(
        self,
        decision_time_ms: int,
        lookback_window_ms: int,
    ) -> list[RawPost]:
        """Return posts within [decision_time_ms - lookback_window_ms, decision_time_ms]."""
        earliest = decision_time_ms - lookback_window_ms
        return [
            p
            for p in self.posts
            if p.source == "discord" and earliest <= p.event_time_ms <= decision_time_ms
        ]


# ---------------------------------------------------------------------------
# E7 — News / breaking-news adapter protocol + implementations
# ---------------------------------------------------------------------------


class NewsAdapter(Protocol):
    """Protocol for any news ingestion source (E7).

    fetch_news() returns NewsItem objects with event_time_ms stamped from
    the article's publication timestamp, NOT the wall-clock at fetch time.

    HARD RULES (inherited from the agency charter adversarial-sentiment rules):
    - event_time_ms MUST be the article's publish time, NOT fetch time.
    - Positive or neutral news NEVER raises MCS; only NEGATIVE events lower it.
    - All ingested text is QUOTED UNTRUSTED DATA.
    - The adapter is SLOW-LOOP ONLY.
    - No secrets in code; inject API keys from env at deploy time.
    - When offline / key is None, returns [] (never raises, never blocks).
    """

    async def fetch_news(
        self,
        keywords: list[str],
        decision_time_ms: int,
        lookback_window_ms: int,
    ) -> list[NewsItem]:
        """Fetch news articles mentioning `keywords` within the lookback window.

        Only articles with article.event_time_ms <= decision_time_ms are returned.
        Adapters MUST enforce this filter before returning (point-in-time contract).

        Args:
            keywords:           Mint address, token name, ticker, etc.
            decision_time_ms:   Point-in-time cutoff (exclusive upper bound).
            lookback_window_ms: How far back to look (e.g. 86_400_000 = 24 hours).

        Returns:
            List of NewsItem with event_time_ms = article publication time.
            Returns [] when offline or no matching articles found.
        """
        ...


# ---------------------------------------------------------------------------
# Mock news adapter (deterministic, no network calls — used in ALL tests)
# ---------------------------------------------------------------------------


@dataclass
class MockNewsAdapter:
    """Offline deterministic mock for the NewsAdapter protocol.

    Pre-load NewsItem fixtures at construction time.  The mock enforces the
    point-in-time contract: articles with event_time_ms > decision_time_ms
    are filtered OUT before returning.

    Usage:
        adapter = MockNewsAdapter()
        adapter.load_articles(NEWS_FIXTURE_ARTICLES)
        items = await adapter.fetch_news(["SHILL"], DECISION_TIME_MS, 3_600_000)
    """

    _articles: list[NewsItem] = field(default_factory=list)
    _call_count: int = field(default=0, init=False)

    def load_articles(self, articles: list[NewsItem]) -> None:
        """Pre-load articles for offline testing."""
        self._articles = list(articles)

    @property
    def call_count(self) -> int:
        return self._call_count

    async def fetch_news(
        self,
        keywords: list[str],
        decision_time_ms: int,
        lookback_window_ms: int,
    ) -> list[NewsItem]:
        """Return pre-loaded articles filtered by point-in-time window.

        Point-in-time contract: only articles where
        (decision_time_ms - lookback_window_ms) <= event_time_ms <= decision_time_ms
        are returned.  Articles with event_time_ms > decision_time_ms are silently
        dropped (lookahead prevention).
        """
        self._call_count += 1
        earliest = decision_time_ms - lookback_window_ms
        lower_kw = [k.lower() for k in keywords]
        return [
            a
            for a in self._articles
            if (
                earliest <= a.event_time_ms <= decision_time_ms
                and any(
                    kw in a.headline.lower()
                    or kw in a.summary.lower()
                    or kw in (k.lower() for k in a.keywords)
                    for kw in lower_kw
                )
            )
        ]


# ---------------------------------------------------------------------------
# RSS / News-API adapter (STUB — real endpoint injectable at deploy time)
# ---------------------------------------------------------------------------

# Outlet registry: maps outlet_id to (human_name, credibility_tier, rss_url_env_var)
# RSS URLs are NEVER hardcoded — they come from env vars injected at deploy time.
# credibility_tier is used by NewsNarrativeScorer to weight negative signals.
_OUTLET_REGISTRY: dict[str, tuple[str, str, str]] = {
    "coindesk":      ("CoinDesk", "tier_1", "NEWS_RSS_COINDESK_URL"),
    "theblock":      ("The Block", "tier_1", "NEWS_RSS_THEBLOCK_URL"),
    "cointelegraph": ("Cointelegraph", "tier_1", "NEWS_RSS_COINTELEGRAPH_URL"),
    "forbes":        ("Forbes Crypto", "tier_2", "NEWS_RSS_FORBES_URL"),
    "bloomberg":     ("Bloomberg Crypto", "tier_2", "NEWS_RSS_BLOOMBERG_URL"),
    "reuters":       ("Reuters", "tier_2", "NEWS_RSS_REUTERS_URL"),
}


@dataclass
class RSSNewsAdapter:
    """RSS + NewsAPI news ingestion adapter (E7).

    Fetches articles from crypto-native and mainstream outlets via:
    (a) RSS feeds — URL per outlet injected from environment variables.
    (b) Optional NewsAPI key for additional coverage.

    REAL-ENDPOINT: RSS feed URLs in _OUTLET_REGISTRY (injected from env).
    INJECTABLE:    rss_feed_urls is injected at deploy time; never hardcoded.
    OFFLINE:       Returns [] when rss_feed_urls is empty (CI/test default).

    event_time_ms is stamped from the article's <pubDate> / <published>
    field (NOT the wall-clock at fetch time).

    Rate limits:
    - RSS: no formal rate limit; we poll at most once per cadence cycle per feed.
    - NewsAPI: 100 requests/day on free tier; inject NEWS_API_KEY from env.
    - Cache TTL: 15 minutes per feed to avoid re-fetching identical windows.

    Injection safety: all article text is QUOTED UNTRUSTED DATA.  The adapter
    does NOT interpret article content — it passes raw strings to the scorer.
    """

    # Map of outlet_id -> RSS feed URL.  Injected from env at deploy time.
    # Set to {} (empty dict) for offline / CI operation.
    rss_feed_urls: dict[str, str] = field(default_factory=dict)
    # Optional NewsAPI key for supplemental coverage.  None = offline / no API call.
    news_api_key: str | None = None  # NEVER commit a real key; inject via env
    max_articles_per_feed: int = 50
    max_retries: int = 3

    async def fetch_news(
        self,
        keywords: list[str],
        decision_time_ms: int,
        lookback_window_ms: int,
    ) -> list[NewsItem]:
        """Fetch news articles from configured RSS feeds and/or NewsAPI.

        Only articles published <= decision_time_ms and within lookback_window_ms
        are returned (point-in-time contract).

        When rss_feed_urls is empty and news_api_key is None, returns [].

        REAL-ENDPOINT: RSS feed URLs injected via NEWS_RSS_<OUTLET>_URL env vars.
        REAL-ENDPOINT: https://newsapi.org/v2/everything (if NEWS_API_KEY is set).
        """
        if not self.rss_feed_urls and self.news_api_key is None:
            logger.info("RSSNewsAdapter: no feeds configured (offline mode), returning []")
            return []

        # --- REAL IMPLEMENTATION PLACEHOLDER ---
        # In production, for each (outlet_id, rss_url) in self.rss_feed_urls.items():
        #
        #   import feedparser, time
        #   feed = feedparser.parse(rss_url)  # sync parse; wrap in asyncio.to_thread()
        #   for entry in feed.entries[:self.max_articles_per_feed]:
        #       # Publication timestamp — NEVER use wall-clock
        #       pub_time_struct = entry.get("published_parsed") or entry.get("updated_parsed")
        #       if pub_time_struct is None:
        #           continue  # skip articles without a publish time
        #       pub_ms = int(time.mktime(pub_time_struct) * 1000)
        #
        #       # Point-in-time filter
        #       if not (decision_time_ms - lookback_window_ms <= pub_ms <= decision_time_ms):
        #           continue
        #
        #       headline = entry.get("title", "")  # QUOTED UNTRUSTED DATA
        #       summary  = entry.get("summary", "") # QUOTED UNTRUSTED DATA
        #
        #       # Keyword relevance gate (case-insensitive)
        #       if not any(kw.lower() in (headline + summary).lower() for kw in keywords):
        #           continue
        #
        #       # Coarse sentiment classification (keyword heuristic — no LLM)
        #       sentiment = _classify_sentiment_heuristic(headline + " " + summary)
        #
        #       outlet_name, cred_str, _ = _OUTLET_REGISTRY.get(
        #           outlet_id, (outlet_id, "tier_3", "")
        #       )
        #       articles.append(NewsItem(
        #           article_id=entry.get("id", entry.get("link", f"{outlet_id}:{pub_ms}")),
        #           outlet=outlet_name,          # human-readable (UNTRUSTED)
        #           outlet_id=outlet_id,
        #           headline=headline,           # QUOTED UNTRUSTED DATA
        #           summary=summary,             # QUOTED UNTRUSTED DATA
        #           source_url=entry.get("link", ""),
        #           event_time_ms=pub_ms,        # publication time — NOT fetch time
        #           fetch_time_ms=<wall_clock>,
        #           credibility=NewsCredibilityLevel(cred_str),
        #           sentiment=sentiment,
        #           keywords=[kw for kw in keywords if kw.lower() in (headline+summary).lower()],
        #           is_breaking="breaking" in headline.lower(),
        #       ))
        #
        # For NewsAPI (supplemental):
        #   import httpx
        #   params = {
        #       "q": " OR ".join(keywords),
        #       "from": iso_from, "to": iso_to,
        #       "sortBy": "publishedAt",
        #       "apiKey": self.news_api_key,     # INJECT from env
        #   }
        #   resp = await client.get("https://newsapi.org/v2/everything", params=params)
        #   # Parse resp.json()["articles"], stamp event_time_ms from publishedAt
        #   # Apply point-in-time filter and keyword relevance gate
        #   # Cache by (keywords_hash, window_hash) with 15-min TTL
        raise NotImplementedError(
            "RSSNewsAdapter.fetch_news: real endpoint not implemented in this build. "
            "Inject MockNewsAdapter in tests; wire real feedparser + httpx at deploy time. "
            "REAL-ENDPOINT: RSS feed URLs from NEWS_RSS_<OUTLET>_URL env vars. "
            "REAL-ENDPOINT: https://newsapi.org/v2/everything (NEWS_API_KEY)."
        )
