"""Discovery enrichment adapters — DEXScreener, Birdeye, Meteora, Moonshot (T-301).

ARCHITECTURE CONSTRAINTS (data-ingestion-engineer charter + BUILD-DIRECTIVE-v3):
  - External services (RPC/Geyser/enrichment APIs) are NOT reachable at test time.
    All clients are INJECTABLE: the HTTP transport is passed at construction, and
    offline fixture responses are shipped alongside for tests.
  - Enrichment is SLOW PATH ONLY — never on the SNIPE/FAST critical path.  No HTTP
    call may block an event decode or a stream produce.  Results are cached and
    tagged with their staleness (seconds-stale, clearly labeled, never zero-lag).
  - Every result carries `event_time` (the slot/blockTime at which the enrichment
    datum was sampled), `ingest_time_ms` (wall-clock at receive), and
    `data_staleness_ms` = ingest_time_ms − event_time.block_time_ms.  Downstream
    consumers are NOT allowed to treat these as real-time data.
  - Money is integer base units / Decimal.  Volume and liquidity values from APIs
    are typically float USD — they are accepted as-is for enrichment metadata
    (not for trade sizing), clearly typed as `float` metadata.
  - No program-ID literals in this file (execution-venue.md §3.2 build guard).
    Venue IDs come from the ProgramRegistry.
  - No secrets in code.  API keys come from env (BIRDEYE_API_KEY, etc.).
  - This module is RAW DATA delivery only.  No feature math, no scoring, no
    trading decisions.  The feature-quant-engineer consumes the typed enrichment
    records and derives features.

Supported enrichment sources:
  1. DEXScreener — pool token pairs, 24h volume (USD float, metadata only),
     liquidity (USD float), recent trades count.
  2. Birdeye  — token overview: holder count, 24h volume, market cap, price
     history samples.
  3. Meteora  — DLMM pool metadata: bin step, fee rate bps, active bin range.
     (candidate venue; enrichment only until promoted to active via config).
  4. Moonshot — launch curve status, graduation threshold, circulating supply.
     (candidate venue; enrichment only until promoted to active via config).

Injectable HTTP transport:
  The `HttpTransport` protocol accepts a callable that returns a dict (for JSON
  responses).  Tests inject `OfflineFixtureTransport` with pre-baked responses.
  Production code wires `AiohttpTransport` (aiohttp-based, async).

Usage:
    # In production (slow loop, off the hot path):
    transport = AiohttpTransport(session=aiohttp.ClientSession())
    client = EnrichmentClient(
        dexscreener=DEXScreenerAdapter(transport=transport),
        birdeye=BirdeyeAdapter(transport=transport, api_key=os.environ["BIRDEYE_API_KEY"]),
        meteora=MeteoraAdapter(transport=transport),
        moonshot=MoonshotAdapter(transport=transport),
    )
    result = await client.enrich(mint="...", slot=300_000_000, block_time_ms=...)

    # In tests (fully offline):
    transport = OfflineFixtureTransport(fixtures=DEXSCREENER_FIXTURE)
    adapter = DEXScreenerAdapter(transport=transport)
"""

from __future__ import annotations

import copy
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------


def _now_ms() -> int:
    return int(time.time() * 1_000)


# ---------------------------------------------------------------------------
# HTTP transport protocol (injectable — no real network at test time)
# ---------------------------------------------------------------------------


class HttpTransport(Protocol):
    """Minimal HTTP GET protocol.

    `get(url, headers, params)` returns a dict (parsed JSON) or raises on error.
    The caller is responsible for error handling / retries at the adapter level.
    """

    async def get(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        params: dict[str, str] | None = None,
    ) -> dict[str, Any]: ...


# ---------------------------------------------------------------------------
# Offline fixture transport — deterministic, no network (for tests)
# ---------------------------------------------------------------------------


class OfflineFixtureTransport:
    """Returns pre-baked fixture responses keyed by (url_substring or exact url).

    Fixture key matching rules (first match wins):
      1. Exact URL match.
      2. Any key that is a substring of the requested URL.
      3. Fallback: raises OfflineFixtureNotFound.

    This lets tests inject per-mint or per-endpoint fixtures without needing
    the exact URL the adapter builds at runtime.

    Usage:
        transport = OfflineFixtureTransport({
            "dexscreener.com/latest/dex/tokens/MintABC": {...json fixture...},
            "overview/MintABC": {...birdeye fixture...},
        })
    """

    class OfflineFixtureNotFound(KeyError):
        pass

    def __init__(self, fixtures: dict[str, dict[str, Any]]) -> None:
        self._fixtures = fixtures

    async def get(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        params: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        # Exact match first
        if url in self._fixtures:
            return copy.deepcopy(self._fixtures[url])
        # Substring match
        for key, fixture in self._fixtures.items():
            if key in url:
                return copy.deepcopy(fixture)
        raise self.OfflineFixtureNotFound(
            f"OfflineFixtureTransport: no fixture for url={url!r}. "
            f"Available keys: {list(self._fixtures)}"
        )


# ---------------------------------------------------------------------------
# Typed enrichment result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EnrichmentMeta:
    """Common provenance fields carried on every enrichment result.

    data_staleness_ms: ingest_time_ms − event_time_block_time_ms (monitoring only).
    event_time_block_time_ms: the on-chain anchor at which this enrichment was
      requested (the LaunchEvent's event_time.block_time_ms).  This is the join
      key for point-in-time feature construction.

    IMPORTANT: enrichment data is seconds-stale by nature (HTTP poll latency +
    indexer lag).  Consumers MUST NOT treat it as sub-block fresh.
    """

    source: str  # "dexscreener" | "birdeye" | "meteora" | "moonshot"
    mint: str
    request_slot: int  # the LaunchEvent slot that triggered this enrichment
    event_time_block_time_ms: int  # on-chain anchor (join key, C-5)
    ingest_time_ms: int  # wall-clock at HTTP response receipt
    data_staleness_ms: int  # = ingest_time_ms - event_time_block_time_ms


@dataclass(frozen=True)
class DEXScreenerResult:
    """DEXScreener pool metadata for a token (enrichment only, slow path).

    All monetary/volume values are USD floats (DEXScreener API format) or
    integer lamports where available.  These are METADATA for enrichment;
    they must not be used for trade sizing (data-models.md §0 money rule).
    """

    meta: EnrichmentMeta
    # Pool info (may be None if token not yet indexed on DEXScreener)
    pool_address: str | None
    base_token_symbol: str | None
    quote_token_symbol: str | None
    price_usd: float | None          # USD, float OK for enrichment metadata
    price_usd_change_h1: float | None
    volume_usd_h24: float | None     # float USD — metadata only
    liquidity_usd: float | None      # float USD — metadata only
    txns_h24_buys: int | None
    txns_h24_sells: int | None
    fdv_usd: float | None            # fully diluted value USD
    pair_created_at_ms: int | None   # epoch ms from DEXScreener
    dex_id: str | None               # "raydium", "pumpswap", etc.


@dataclass(frozen=True)
class BirdeyeResult:
    """Birdeye token overview (enrichment only, slow path).

    holder_count is the key M1 field: used by feature-quant-engineer to compute
    holder_concentration and initial_holders enrichment for the LaunchEvent.
    """

    meta: EnrichmentMeta
    # Token overview
    holder_count: int | None        # integer — used for holder_concentration feature
    price_usd: float | None
    market_cap_usd: float | None    # float USD — metadata only
    volume_usd_h24: float | None    # float USD — metadata only
    liquidity_usd: float | None     # float USD — metadata only
    trade_count_h24: int | None
    # Price history: list of {timestamp_ms, price_usd} dicts (metadata only)
    price_history_samples: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class MeteoraResult:
    """Meteora DLMM pool metadata (candidate venue enrichment — T-202, T-301).

    Meteora is a CANDIDATE in the program registry (status=candidate in
    program-allowlist.json).  This adapter emits enrichment metadata only;
    it does NOT wire any execution path.  Execution is gated on registry
    promotion to `active` via config + deploy.
    """

    meta: EnrichmentMeta
    pool_address: str | None
    base_mint: str | None
    quote_mint: str | None
    bin_step: int | None            # DLMM bin step (price granularity)
    fee_rate_bps: int | None        # base fee in bps (integer — fee metadata)
    active_bin_id: int | None       # current active price bin
    total_liquidity_usd: float | None  # float USD — metadata only
    total_volume_usd_h24: float | None


@dataclass(frozen=True)
class MoonshotResult:
    """Moonshot launch curve metadata (candidate venue enrichment — T-202, T-301).

    Moonshot is a CANDIDATE in the program registry.  Enrichment metadata only;
    no execution path is wired until the registry promotes it to `active`.
    """

    meta: EnrichmentMeta
    curve_type: str | None          # "linear" | "exponential" | etc.
    curve_progress_pct: float | None  # 0.0–100.0
    graduation_threshold_usd: float | None
    circulating_supply: int | None  # integer base units
    total_supply: int | None        # integer base units
    creator_wallet: str | None


# ---------------------------------------------------------------------------
# DEXScreener adapter
# ---------------------------------------------------------------------------

# API base URL template (no credentials required for public endpoints)
_DEXSCREENER_BASE = "https://api.dexscreener.com/latest/dex/tokens"


class DEXScreenerAdapter:
    """Fetch DEXScreener pool metadata for a Solana token mint.

    DEXScreener does not require an API key for the public tokens endpoint.
    DEXSCREENER_API_KEY from env is reserved for future rate-limit bypass.

    The adapter is INJECTABLE: `transport` can be an OfflineFixtureTransport
    in tests or an AiohttpTransport in production.
    """

    def __init__(
        self,
        transport: HttpTransport,
        base_url: str = _DEXSCREENER_BASE,
    ) -> None:
        self._transport = transport
        self._base_url = base_url

    async def fetch(
        self,
        mint: str,
        request_slot: int,
        event_time_block_time_ms: int,
    ) -> DEXScreenerResult | None:
        """Fetch DEXScreener metadata for `mint`.

        Returns None if the token is not indexed or the request fails.
        Errors are logged at WARNING level (enrichment is best-effort, never
        on the critical path).
        """
        url = f"{self._base_url}/{mint}"
        try:
            data = await self._transport.get(url)
        except Exception as exc:
            logger.warning(
                "DEXScreener fetch failed for mint=%s: %s", mint, exc
            )
            return None

        ingest_time_ms = _now_ms()
        meta = EnrichmentMeta(
            source="dexscreener",
            mint=mint,
            request_slot=request_slot,
            event_time_block_time_ms=event_time_block_time_ms,
            ingest_time_ms=ingest_time_ms,
            data_staleness_ms=ingest_time_ms - event_time_block_time_ms,
        )

        # DEXScreener response: {"pairs": [{...}, ...]}
        pairs = data.get("pairs") or []
        if not pairs:
            logger.debug("DEXScreener: no pairs for mint=%s", mint)
            return DEXScreenerResult(
                meta=meta,
                pool_address=None,
                base_token_symbol=None,
                quote_token_symbol=None,
                price_usd=None,
                price_usd_change_h1=None,
                volume_usd_h24=None,
                liquidity_usd=None,
                txns_h24_buys=None,
                txns_h24_sells=None,
                fdv_usd=None,
                pair_created_at_ms=None,
                dex_id=None,
            )

        # Use the first pair (most liquid by DEXScreener ordering)
        pair = pairs[0]
        base_token = pair.get("baseToken") or {}
        quote_token = pair.get("quoteToken") or {}
        txns = pair.get("txns") or {}
        h24 = txns.get("h24") or {}
        volume = pair.get("volume") or {}
        liquidity = pair.get("liquidity") or {}
        price_change = pair.get("priceChange") or {}

        return DEXScreenerResult(
            meta=meta,
            pool_address=pair.get("pairAddress"),
            base_token_symbol=base_token.get("symbol"),
            quote_token_symbol=quote_token.get("symbol"),
            price_usd=_safe_float(pair.get("priceUsd")),
            price_usd_change_h1=_safe_float(price_change.get("h1")),
            volume_usd_h24=_safe_float(volume.get("h24")),
            liquidity_usd=_safe_float(liquidity.get("usd")),
            txns_h24_buys=_safe_int(h24.get("buys")),
            txns_h24_sells=_safe_int(h24.get("sells")),
            fdv_usd=_safe_float(pair.get("fdv")),
            pair_created_at_ms=_safe_int(pair.get("pairCreatedAt")),
            dex_id=pair.get("dexId"),
        )


# ---------------------------------------------------------------------------
# Birdeye adapter
# ---------------------------------------------------------------------------

_BIRDEYE_BASE = "https://public-api.birdeye.so/defi"
_BIRDEYE_OVERVIEW_PATH = "/token_overview"


class BirdeyeAdapter:
    """Fetch Birdeye token overview for a Solana token mint.

    Requires BIRDEYE_API_KEY from env (passed at construction — never hardcoded).
    """

    def __init__(
        self,
        transport: HttpTransport,
        api_key: str = "",
        base_url: str = _BIRDEYE_BASE,
    ) -> None:
        self._transport = transport
        self._api_key = api_key  # from env BIRDEYE_API_KEY
        self._base_url = base_url

    async def fetch(
        self,
        mint: str,
        request_slot: int,
        event_time_block_time_ms: int,
    ) -> BirdeyeResult | None:
        """Fetch Birdeye overview for `mint`.

        Returns None if the token is not indexed or the request fails.
        """
        url = f"{self._base_url}{_BIRDEYE_OVERVIEW_PATH}"
        headers: dict[str, str] = {}
        if self._api_key:
            headers["X-API-KEY"] = self._api_key
        params = {"address": mint}

        try:
            data = await self._transport.get(url, headers=headers, params=params)
        except Exception as exc:
            logger.warning("Birdeye fetch failed for mint=%s: %s", mint, exc)
            return None

        ingest_time_ms = _now_ms()
        meta = EnrichmentMeta(
            source="birdeye",
            mint=mint,
            request_slot=request_slot,
            event_time_block_time_ms=event_time_block_time_ms,
            ingest_time_ms=ingest_time_ms,
            data_staleness_ms=ingest_time_ms - event_time_block_time_ms,
        )

        # Birdeye response: {"data": {...}, "success": true}
        if not data.get("success", True):
            logger.debug("Birdeye: success=false for mint=%s", mint)
            return BirdeyeResult(
                meta=meta,
                holder_count=None,
                price_usd=None,
                market_cap_usd=None,
                volume_usd_h24=None,
                liquidity_usd=None,
                trade_count_h24=None,
            )

        token_data = data.get("data") or {}

        return BirdeyeResult(
            meta=meta,
            holder_count=_safe_int(token_data.get("holder")),
            price_usd=_safe_float(token_data.get("price")),
            market_cap_usd=_safe_float(token_data.get("mc")),
            volume_usd_h24=_safe_float(token_data.get("v24hUSD")),
            liquidity_usd=_safe_float(token_data.get("liquidity")),
            trade_count_h24=_safe_int(token_data.get("trade24h")),
            price_history_samples=_safe_list(token_data.get("priceHistory")),
        )


# ---------------------------------------------------------------------------
# Meteora adapter (candidate venue — enrichment only)
# ---------------------------------------------------------------------------

_METEORA_BASE = "https://dlmm-api.meteora.ag/pair"


class MeteoraAdapter:
    """Fetch Meteora DLMM pool metadata for a Solana token mint.

    CANDIDATE VENUE (T-202, program-allowlist.json status=candidate):
      This adapter emits ENRICHMENT METADATA ONLY.  No execution path is
      wired via this adapter.  Venue promotion to `active` requires a separate
      config + deploy change (registry.from_allowlist with filter_active_only).

    Meteora's public DLMM API is used to look up pool pairs by base/quote mint.
    No API key required for the public endpoint.
    """

    def __init__(
        self,
        transport: HttpTransport,
        base_url: str = _METEORA_BASE,
    ) -> None:
        self._transport = transport
        self._base_url = base_url

    async def fetch(
        self,
        mint: str,
        request_slot: int,
        event_time_block_time_ms: int,
    ) -> MeteoraResult | None:
        """Fetch Meteora DLMM pool metadata for `mint`.

        Queries the /pair/all_with_pagination endpoint filtered by token mint.
        Returns None if not found or request fails.
        """
        url = f"{self._base_url}/all_with_pagination"
        params = {"search_term": mint, "page": "0", "limit": "1"}

        try:
            data = await self._transport.get(url, params=params)
        except Exception as exc:
            logger.warning("Meteora fetch failed for mint=%s: %s", mint, exc)
            return None

        ingest_time_ms = _now_ms()
        meta = EnrichmentMeta(
            source="meteora",
            mint=mint,
            request_slot=request_slot,
            event_time_block_time_ms=event_time_block_time_ms,
            ingest_time_ms=ingest_time_ms,
            data_staleness_ms=ingest_time_ms - event_time_block_time_ms,
        )

        # Meteora response: {"data": [...pairs...], "total": N, "page": 0}
        pairs: list[dict[str, Any]] = []
        raw_pairs = data.get("data") or data.get("pairs") or []
        if isinstance(raw_pairs, list):
            pairs = raw_pairs

        if not pairs:
            logger.debug("Meteora: no DLMM pairs for mint=%s", mint)
            return MeteoraResult(
                meta=meta,
                pool_address=None,
                base_mint=None,
                quote_mint=None,
                bin_step=None,
                fee_rate_bps=None,
                active_bin_id=None,
                total_liquidity_usd=None,
                total_volume_usd_h24=None,
            )

        pair = pairs[0]
        return MeteoraResult(
            meta=meta,
            pool_address=pair.get("address"),
            base_mint=pair.get("mint_x") or pair.get("base_mint"),
            quote_mint=pair.get("mint_y") or pair.get("quote_mint"),
            bin_step=_safe_int(pair.get("bin_step")),
            # Meteora fee_rate is usually in bps already (integer hundredths of pct)
            fee_rate_bps=_safe_int(pair.get("base_fee_percentage") or pair.get("fee_rate_bps")),
            active_bin_id=_safe_int(pair.get("active_id") or pair.get("active_bin_id")),
            total_liquidity_usd=_safe_float(pair.get("liquidity") or pair.get("total_liquidity_usd")),
            total_volume_usd_h24=_safe_float(pair.get("trade_volume_usd") or pair.get("volume_usd_h24")),
        )


# ---------------------------------------------------------------------------
# Moonshot adapter (candidate venue — enrichment only)
# ---------------------------------------------------------------------------

_MOONSHOT_BASE = "https://api.moonshot.cc/token/v1/solana"


class MoonshotAdapter:
    """Fetch Moonshot launch curve metadata for a Solana token mint.

    CANDIDATE VENUE (T-202, program-allowlist.json status=candidate):
      This adapter emits ENRICHMENT METADATA ONLY.  No execution path is
      wired via this adapter.  Venue promotion to `active` requires a separate
      config + deploy change.

    Moonshot's public token API does not require an API key for basic queries.
    """

    def __init__(
        self,
        transport: HttpTransport,
        base_url: str = _MOONSHOT_BASE,
    ) -> None:
        self._transport = transport
        self._base_url = base_url

    async def fetch(
        self,
        mint: str,
        request_slot: int,
        event_time_block_time_ms: int,
    ) -> MoonshotResult | None:
        """Fetch Moonshot launch curve metadata for `mint`.

        Returns None if the token is not a Moonshot token or request fails.
        """
        url = f"{self._base_url}/{mint}"

        try:
            data = await self._transport.get(url)
        except Exception as exc:
            logger.warning("Moonshot fetch failed for mint=%s: %s", mint, exc)
            return None

        ingest_time_ms = _now_ms()
        meta = EnrichmentMeta(
            source="moonshot",
            mint=mint,
            request_slot=request_slot,
            event_time_block_time_ms=event_time_block_time_ms,
            ingest_time_ms=ingest_time_ms,
            data_staleness_ms=ingest_time_ms - event_time_block_time_ms,
        )

        # Moonshot response: {curveType, curveProgress, graduationThreshold, ...}
        # The exact shape varies; we extract what's available gracefully.
        curve_progress_raw = data.get("curveProgress") or data.get("curve_progress")
        graduation_raw = data.get("graduationThreshold") or data.get("graduation_threshold_usd")

        return MoonshotResult(
            meta=meta,
            curve_type=data.get("curveType") or data.get("curve_type"),
            curve_progress_pct=_safe_float(curve_progress_raw),
            graduation_threshold_usd=_safe_float(graduation_raw),
            # Supply values are returned as integers by Moonshot's API
            circulating_supply=_safe_int(
                data.get("circulatingSupply") or data.get("circulating_supply")
            ),
            total_supply=_safe_int(data.get("totalSupply") or data.get("total_supply")),
            creator_wallet=data.get("creatorAddress") or data.get("creator"),
        )


# ---------------------------------------------------------------------------
# Composite enrichment client
# ---------------------------------------------------------------------------


@dataclass
class EnrichmentBundle:
    """All enrichment results for a single mint, keyed by source.

    Any individual result may be None if the source had no data or failed.
    Consumers must check for None before using a specific result.
    """

    mint: str
    request_slot: int
    event_time_block_time_ms: int
    dexscreener: DEXScreenerResult | None = None
    birdeye: BirdeyeResult | None = None
    meteora: MeteoraResult | None = None
    moonshot: MoonshotResult | None = None

    @property
    def any_success(self) -> bool:
        """True if at least one enrichment source returned data."""
        return any(r is not None for r in (self.dexscreener, self.birdeye, self.meteora, self.moonshot))

    def holder_count(self) -> int | None:
        """Best available holder count (Birdeye is primary)."""
        if self.birdeye and self.birdeye.holder_count is not None:
            return self.birdeye.holder_count
        return None

    def liquidity_usd(self) -> float | None:
        """Best available liquidity (DEXScreener primary, Meteora fallback)."""
        if self.dexscreener and self.dexscreener.liquidity_usd is not None:
            return self.dexscreener.liquidity_usd
        if self.meteora and self.meteora.total_liquidity_usd is not None:
            return self.meteora.total_liquidity_usd
        return None

    def max_staleness_ms(self) -> int:
        """Max data_staleness_ms across all non-None results (monitoring)."""
        candidates = []
        for r in (self.dexscreener, self.birdeye, self.meteora, self.moonshot):
            if r is not None:
                candidates.append(r.meta.data_staleness_ms)
        return max(candidates) if candidates else -1


class EnrichmentClient:
    """Composite enrichment client — fetches from all enabled adapters in parallel.

    All adapters are OPTIONAL.  Pass None to skip an adapter entirely.
    Enrichment is SLOW PATH ONLY and must never block the SNIPE/FAST path.

    Errors from individual adapters are logged at WARNING and result in a None
    slot in the bundle — never propagated as exceptions to the caller.
    """

    def __init__(
        self,
        dexscreener: DEXScreenerAdapter | None = None,
        birdeye: BirdeyeAdapter | None = None,
        meteora: MeteoraAdapter | None = None,
        moonshot: MoonshotAdapter | None = None,
    ) -> None:
        self._dexscreener = dexscreener
        self._birdeye = birdeye
        self._meteora = meteora
        self._moonshot = moonshot

    async def enrich(
        self,
        mint: str,
        request_slot: int,
        event_time_block_time_ms: int,
    ) -> EnrichmentBundle:
        """Fetch enrichment from all configured adapters.

        Each adapter call is independent; a single adapter failure does not
        prevent the others from succeeding.  Results are bundled and returned
        together.

        NOT concurrent by default (single-threaded async sequential) — the
        caller may wrap in asyncio.gather if parallel latency matters, but
        enrichment is seconds-stale anyway, so sequential is usually fine.
        """
        bundle = EnrichmentBundle(
            mint=mint,
            request_slot=request_slot,
            event_time_block_time_ms=event_time_block_time_ms,
        )

        if self._dexscreener is not None:
            try:
                bundle.dexscreener = await self._dexscreener.fetch(
                    mint, request_slot, event_time_block_time_ms
                )
            except Exception as exc:
                logger.warning("EnrichmentClient: DEXScreener error mint=%s: %s", mint, exc)

        if self._birdeye is not None:
            try:
                bundle.birdeye = await self._birdeye.fetch(
                    mint, request_slot, event_time_block_time_ms
                )
            except Exception as exc:
                logger.warning("EnrichmentClient: Birdeye error mint=%s: %s", mint, exc)

        if self._meteora is not None:
            try:
                bundle.meteora = await self._meteora.fetch(
                    mint, request_slot, event_time_block_time_ms
                )
            except Exception as exc:
                logger.warning("EnrichmentClient: Meteora error mint=%s: %s", mint, exc)

        if self._moonshot is not None:
            try:
                bundle.moonshot = await self._moonshot.fetch(
                    mint, request_slot, event_time_block_time_ms
                )
            except Exception as exc:
                logger.warning("EnrichmentClient: Moonshot error mint=%s: %s", mint, exc)

        logger.debug(
            "EnrichmentClient: mint=%s slot=%d dex=%s birdeye=%s meteora=%s moonshot=%s",
            mint,
            request_slot,
            "ok" if bundle.dexscreener else "none",
            "ok" if bundle.birdeye else "none",
            "ok" if bundle.meteora else "none",
            "ok" if bundle.moonshot else "none",
        )
        return bundle


# ---------------------------------------------------------------------------
# Offline fixture data (deterministic, no network)
# ---------------------------------------------------------------------------

# DEXScreener fixture: a pump.fun token that has migrated to PumpSwap
DEXSCREENER_FIXTURE: dict[str, Any] = {
    "pairs": [
        {
            "chainId": "solana",
            "dexId": "pumpswap",
            "url": "https://dexscreener.com/solana/FixturePoolAddr1111111111111111111111111111",
            "pairAddress": "FixturePoolAddr1111111111111111111111111111",
            "baseToken": {
                "address": "FixtureMint1111111111111111111111111111111111",
                "name": "TestToken",
                "symbol": "TEST",
            },
            "quoteToken": {
                "address": "So11111111111111111111111111111111111111112",
                "name": "Wrapped SOL",
                "symbol": "SOL",
            },
            "priceUsd": "0.000042",
            "priceChange": {"h1": "12.5", "h24": "-3.1"},
            "volume": {"h24": "85000.0"},
            "liquidity": {"usd": "32000.0"},
            "txns": {
                "h24": {"buys": 312, "sells": 198}
            },
            "fdv": "42000.0",
            "pairCreatedAt": 1_718_700_011_000,
        }
    ]
}

# Birdeye fixture: token overview
BIRDEYE_FIXTURE: dict[str, Any] = {
    "success": True,
    "data": {
        "address": "FixtureMint1111111111111111111111111111111111",
        "symbol": "TEST",
        "name": "TestToken",
        "decimals": 6,
        "holder": 847,
        "price": 0.000042,
        "mc": 42000.0,
        "v24hUSD": 85000.0,
        "liquidity": 32000.0,
        "trade24h": 510,
        "priceHistory": [
            {"unixTime": 1_718_700_000, "value": 0.000038},
            {"unixTime": 1_718_700_060, "value": 0.000042},
        ],
    },
}

# Meteora fixture: DLMM pool metadata
METEORA_FIXTURE: dict[str, Any] = {
    "data": [
        {
            "address": "MeteoraPool111111111111111111111111111111111",
            "mint_x": "FixtureMint1111111111111111111111111111111111",
            "mint_y": "So11111111111111111111111111111111111111112",
            "bin_step": 100,
            "base_fee_percentage": 25,
            "active_id": 8388608,
            "liquidity": "28000.5",
            "trade_volume_usd": "12000.0",
        }
    ],
    "total": 1,
    "page": 0,
}

# Moonshot fixture: launch curve status
MOONSHOT_FIXTURE: dict[str, Any] = {
    "curveType": "linear",
    "curveProgress": 67.4,
    "graduationThreshold": 500.0,
    "circulatingSupply": 800_000_000,
    "totalSupply": 1_000_000_000,
    "creatorAddress": "MoonshotCreator11111111111111111111111111111",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _safe_float(val: Any) -> float | None:
    """Safely coerce a value to float; None on failure."""
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _safe_int(val: Any) -> int | None:
    """Safely coerce a value to int; None on failure."""
    if val is None:
        return None
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


def _safe_list(val: Any) -> list[Any]:
    """Return val if it is a list, else empty list."""
    if isinstance(val, list):
        return val
    return []


def build_offline_enrichment_client(
    mint: str | None = None,
) -> EnrichmentClient:
    """Build an EnrichmentClient wired to deterministic offline fixtures.

    Used in tests to verify adapter parsing without any network access.
    The `mint` parameter is ignored in fixture matching (fixtures respond to any URL
    containing the configured key substring); it is accepted for call-site clarity.
    """
    fixture_map: dict[str, dict[str, Any]] = {
        "dexscreener.com": DEXSCREENER_FIXTURE,
        "birdeye.so": BIRDEYE_FIXTURE,
        "meteora.ag": METEORA_FIXTURE,
        "moonshot.cc": MOONSHOT_FIXTURE,
    }
    transport = OfflineFixtureTransport(fixture_map)
    return EnrichmentClient(
        dexscreener=DEXScreenerAdapter(transport=transport),
        birdeye=BirdeyeAdapter(transport=transport, api_key="offline"),
        meteora=MeteoraAdapter(transport=transport),
        moonshot=MoonshotAdapter(transport=transport),
    )
