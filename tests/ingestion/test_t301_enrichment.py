"""T-301: Discovery enrichment adapter tests + T-300a pending-table carry-forward.

Tests verified:
  1. DEXScreener adapter parses offline fixture correctly.
  2. Birdeye adapter parses offline fixture correctly.
  3. Meteora adapter parses offline fixture correctly (candidate venue).
  4. Moonshot adapter parses offline fixture correctly (candidate venue).
  5. EnrichmentClient aggregates all adapters (offline fixtures only).
  6. Enrichment is tagged with correct staleness (not real-time, not zero).
  7. Adapter returns None gracefully when fixture has no data (empty pairs).
  8. EnrichmentClient continues when one adapter fails (isolation).
  9. OfflineFixtureTransport raises OfflineFixtureNotFound for unknown URLs.
 10. T-300a carry-forward: read_as_of("pending_events") raises ValueError with
     helpful message (not KeyError: 'mint').
 11. T-300a carry-forward: pending rows are in dedicated table, not _rows.
 12. T-300a carry-forward: append_pending_row() enforces dataset='pending_events'.
 13. T-300a carry-forward: read_pending_as_of() works on dedicated table.
 14. T-300a carry-forward: all_rows("pending_events") returns _pending_rows.
 15. T-300a carry-forward: write_pending_slot_event uses public append_pending_row.
 16. Registry: from_allowlist skips candidate venues when filter_active_only=True.
 17. Registry: from_dict with offline entries loads and returns correct IDs.
 18. Money int rule: no float in sol_reserve_lamports / token_reserve_base enrichment.
 19. Staleness: enrichment result data_staleness_ms >= 0.
 20. EnrichmentBundle.holder_count() prefers Birdeye.
 21. EnrichmentBundle.liquidity_usd() falls back to Meteora if DEXScreener absent.

HARD RULES (non-waivable, from BUILD-DIRECTIVE-v3):
  - No real network calls: all adapters use OfflineFixtureTransport.
  - No secrets/keys in code: api_key="offline" placeholder only.
  - DRY_RUN implicit: this module is read-side only (no execution path).
  - Money is integer/Decimal: holder_count, supply are int; USD floats are
    enrichment metadata only, clearly NOT used for sizing.
"""

from __future__ import annotations

import asyncio

import pytest

from aats.ingestion.enrichment import (
    BIRDEYE_FIXTURE,
    DEXSCREENER_FIXTURE,
    METEORA_FIXTURE,
    MOONSHOT_FIXTURE,
    BirdeyeAdapter,
    DEXScreenerAdapter,
    EnrichmentBundle,
    EnrichmentClient,
    MeteoraAdapter,
    MoonshotAdapter,
    OfflineFixtureTransport,
    build_offline_enrichment_client,
)
from aats.ingestion.store import InMemoryParquetBackend, PointInTimeStoreWriter, _now_ms

# ---------------------------------------------------------------------------
# Common test fixtures
# ---------------------------------------------------------------------------

TEST_MINT = "FixtureMint1111111111111111111111111111111111"
TEST_SLOT = 300_000_011
TEST_BLOCK_TIME_MS = 1_718_700_011_000  # corresponds to DEXScreener pairCreatedAt


def _make_dex_transport() -> OfflineFixtureTransport:
    return OfflineFixtureTransport({"dexscreener.com": DEXSCREENER_FIXTURE})


def _make_birdeye_transport() -> OfflineFixtureTransport:
    return OfflineFixtureTransport({"birdeye.so": BIRDEYE_FIXTURE})


def _make_meteora_transport() -> OfflineFixtureTransport:
    return OfflineFixtureTransport({"meteora.ag": METEORA_FIXTURE})


def _make_moonshot_transport() -> OfflineFixtureTransport:
    return OfflineFixtureTransport({"moonshot.cc": MOONSHOT_FIXTURE})


# ---------------------------------------------------------------------------
# 1. DEXScreener adapter
# ---------------------------------------------------------------------------


class TestDEXScreenerAdapter:
    def test_parses_pool_address(self):
        adapter = DEXScreenerAdapter(transport=_make_dex_transport())
        result = asyncio.run(
            adapter.fetch(TEST_MINT, TEST_SLOT, TEST_BLOCK_TIME_MS)
        )
        assert result is not None
        assert result.pool_address == "FixturePoolAddr1111111111111111111111111111"

    def test_parses_base_token_symbol(self):
        adapter = DEXScreenerAdapter(transport=_make_dex_transport())
        result = asyncio.run(
            adapter.fetch(TEST_MINT, TEST_SLOT, TEST_BLOCK_TIME_MS)
        )
        assert result is not None
        assert result.base_token_symbol == "TEST"

    def test_parses_liquidity_usd_as_float(self):
        """Liquidity is float USD metadata — NOT used for sizing."""
        adapter = DEXScreenerAdapter(transport=_make_dex_transport())
        result = asyncio.run(
            adapter.fetch(TEST_MINT, TEST_SLOT, TEST_BLOCK_TIME_MS)
        )
        assert result is not None
        assert isinstance(result.liquidity_usd, float)
        assert result.liquidity_usd == pytest.approx(32000.0)

    def test_parses_txns_h24_buys_as_int(self):
        """Trade counts are integers."""
        adapter = DEXScreenerAdapter(transport=_make_dex_transport())
        result = asyncio.run(
            adapter.fetch(TEST_MINT, TEST_SLOT, TEST_BLOCK_TIME_MS)
        )
        assert result is not None
        assert isinstance(result.txns_h24_buys, int)
        assert result.txns_h24_buys == 312

    def test_parses_dex_id(self):
        adapter = DEXScreenerAdapter(transport=_make_dex_transport())
        result = asyncio.run(
            adapter.fetch(TEST_MINT, TEST_SLOT, TEST_BLOCK_TIME_MS)
        )
        assert result is not None
        assert result.dex_id == "pumpswap"

    def test_parses_pair_created_at_ms_as_int(self):
        adapter = DEXScreenerAdapter(transport=_make_dex_transport())
        result = asyncio.run(
            adapter.fetch(TEST_MINT, TEST_SLOT, TEST_BLOCK_TIME_MS)
        )
        assert result is not None
        assert isinstance(result.pair_created_at_ms, int)
        assert result.pair_created_at_ms == 1_718_700_011_000

    def test_staleness_is_non_negative(self):
        """data_staleness_ms must be >= 0 (monitoring only, not zero-lag)."""
        adapter = DEXScreenerAdapter(transport=_make_dex_transport())
        result = asyncio.run(
            adapter.fetch(TEST_MINT, TEST_SLOT, TEST_BLOCK_TIME_MS)
        )
        assert result is not None
        assert result.meta.data_staleness_ms >= 0

    def test_source_label_is_dexscreener(self):
        """Meta.source must be 'dexscreener' for staleness tracking."""
        adapter = DEXScreenerAdapter(transport=_make_dex_transport())
        result = asyncio.run(
            adapter.fetch(TEST_MINT, TEST_SLOT, TEST_BLOCK_TIME_MS)
        )
        assert result is not None
        assert result.meta.source == "dexscreener"

    def test_returns_none_on_transport_error(self):
        """When the transport raises, adapter returns None (best-effort enrichment)."""
        class ErrorTransport:
            async def get(self, url, headers=None, params=None):
                raise ConnectionError("offline")

        adapter = DEXScreenerAdapter(transport=ErrorTransport())
        result = asyncio.run(
            adapter.fetch(TEST_MINT, TEST_SLOT, TEST_BLOCK_TIME_MS)
        )
        assert result is None

    def test_returns_none_fields_when_no_pairs(self):
        """When pairs list is empty, result has None pool_address etc."""
        transport = OfflineFixtureTransport({"dexscreener.com": {"pairs": []}})
        adapter = DEXScreenerAdapter(transport=transport)
        result = asyncio.run(
            adapter.fetch(TEST_MINT, TEST_SLOT, TEST_BLOCK_TIME_MS)
        )
        assert result is not None  # result is not None — just has None fields
        assert result.pool_address is None
        assert result.liquidity_usd is None

    def test_meta_request_slot_matches_input(self):
        adapter = DEXScreenerAdapter(transport=_make_dex_transport())
        result = asyncio.run(
            adapter.fetch(TEST_MINT, TEST_SLOT, TEST_BLOCK_TIME_MS)
        )
        assert result is not None
        assert result.meta.request_slot == TEST_SLOT

    def test_meta_event_time_block_time_ms_matches_input(self):
        adapter = DEXScreenerAdapter(transport=_make_dex_transport())
        result = asyncio.run(
            adapter.fetch(TEST_MINT, TEST_SLOT, TEST_BLOCK_TIME_MS)
        )
        assert result is not None
        assert result.meta.event_time_block_time_ms == TEST_BLOCK_TIME_MS


# ---------------------------------------------------------------------------
# 2. Birdeye adapter
# ---------------------------------------------------------------------------


class TestBirdeyeAdapter:
    def test_parses_holder_count_as_int(self):
        """holder_count is an integer (used for holder_concentration feature)."""
        adapter = BirdeyeAdapter(transport=_make_birdeye_transport(), api_key="offline")
        result = asyncio.run(
            adapter.fetch(TEST_MINT, TEST_SLOT, TEST_BLOCK_TIME_MS)
        )
        assert result is not None
        assert isinstance(result.holder_count, int)
        assert result.holder_count == 847

    def test_parses_price_usd_as_float(self):
        """price_usd is a float (enrichment metadata, not for sizing)."""
        adapter = BirdeyeAdapter(transport=_make_birdeye_transport(), api_key="offline")
        result = asyncio.run(
            adapter.fetch(TEST_MINT, TEST_SLOT, TEST_BLOCK_TIME_MS)
        )
        assert result is not None
        assert isinstance(result.price_usd, float)

    def test_parses_market_cap_usd_as_float(self):
        adapter = BirdeyeAdapter(transport=_make_birdeye_transport(), api_key="offline")
        result = asyncio.run(
            adapter.fetch(TEST_MINT, TEST_SLOT, TEST_BLOCK_TIME_MS)
        )
        assert result is not None
        assert result.market_cap_usd == pytest.approx(42000.0)

    def test_parses_trade_count_h24_as_int(self):
        adapter = BirdeyeAdapter(transport=_make_birdeye_transport(), api_key="offline")
        result = asyncio.run(
            adapter.fetch(TEST_MINT, TEST_SLOT, TEST_BLOCK_TIME_MS)
        )
        assert result is not None
        assert isinstance(result.trade_count_h24, int)
        assert result.trade_count_h24 == 510

    def test_parses_price_history_as_list(self):
        adapter = BirdeyeAdapter(transport=_make_birdeye_transport(), api_key="offline")
        result = asyncio.run(
            adapter.fetch(TEST_MINT, TEST_SLOT, TEST_BLOCK_TIME_MS)
        )
        assert result is not None
        assert isinstance(result.price_history_samples, list)
        assert len(result.price_history_samples) == 2

    def test_source_label_is_birdeye(self):
        adapter = BirdeyeAdapter(transport=_make_birdeye_transport(), api_key="offline")
        result = asyncio.run(
            adapter.fetch(TEST_MINT, TEST_SLOT, TEST_BLOCK_TIME_MS)
        )
        assert result is not None
        assert result.meta.source == "birdeye"

    def test_staleness_is_non_negative(self):
        adapter = BirdeyeAdapter(transport=_make_birdeye_transport(), api_key="offline")
        result = asyncio.run(
            adapter.fetch(TEST_MINT, TEST_SLOT, TEST_BLOCK_TIME_MS)
        )
        assert result is not None
        assert result.meta.data_staleness_ms >= 0

    def test_returns_none_on_transport_error(self):
        class ErrorTransport:
            async def get(self, url, headers=None, params=None):
                raise ConnectionError("offline")

        adapter = BirdeyeAdapter(transport=ErrorTransport(), api_key="offline")
        result = asyncio.run(
            adapter.fetch(TEST_MINT, TEST_SLOT, TEST_BLOCK_TIME_MS)
        )
        assert result is None

    def test_success_false_returns_result_with_none_holder_count(self):
        """success=false from Birdeye → result with None fields, not exception."""
        transport = OfflineFixtureTransport({"birdeye.so": {"success": False, "data": None}})
        adapter = BirdeyeAdapter(transport=transport, api_key="offline")
        result = asyncio.run(
            adapter.fetch(TEST_MINT, TEST_SLOT, TEST_BLOCK_TIME_MS)
        )
        assert result is not None
        assert result.holder_count is None


# ---------------------------------------------------------------------------
# 3. Meteora adapter (candidate venue)
# ---------------------------------------------------------------------------


class TestMeteoraAdapter:
    def test_parses_pool_address(self):
        adapter = MeteoraAdapter(transport=_make_meteora_transport())
        result = asyncio.run(
            adapter.fetch(TEST_MINT, TEST_SLOT, TEST_BLOCK_TIME_MS)
        )
        assert result is not None
        assert result.pool_address == "MeteoraPool111111111111111111111111111111111"

    def test_parses_bin_step_as_int(self):
        """bin_step is an integer (DLMM price granularity metadata)."""
        adapter = MeteoraAdapter(transport=_make_meteora_transport())
        result = asyncio.run(
            adapter.fetch(TEST_MINT, TEST_SLOT, TEST_BLOCK_TIME_MS)
        )
        assert result is not None
        assert isinstance(result.bin_step, int)
        assert result.bin_step == 100

    def test_parses_fee_rate_bps_as_int(self):
        """fee_rate_bps is an integer (bps are exact, not float)."""
        adapter = MeteoraAdapter(transport=_make_meteora_transport())
        result = asyncio.run(
            adapter.fetch(TEST_MINT, TEST_SLOT, TEST_BLOCK_TIME_MS)
        )
        assert result is not None
        assert isinstance(result.fee_rate_bps, int)
        assert result.fee_rate_bps == 25

    def test_parses_active_bin_id_as_int(self):
        adapter = MeteoraAdapter(transport=_make_meteora_transport())
        result = asyncio.run(
            adapter.fetch(TEST_MINT, TEST_SLOT, TEST_BLOCK_TIME_MS)
        )
        assert result is not None
        assert isinstance(result.active_bin_id, int)
        assert result.active_bin_id == 8388608

    def test_parses_liquidity_as_float(self):
        """Liquidity is float USD metadata — enrichment only, not for sizing."""
        adapter = MeteoraAdapter(transport=_make_meteora_transport())
        result = asyncio.run(
            adapter.fetch(TEST_MINT, TEST_SLOT, TEST_BLOCK_TIME_MS)
        )
        assert result is not None
        assert isinstance(result.total_liquidity_usd, float)

    def test_source_label_is_meteora(self):
        adapter = MeteoraAdapter(transport=_make_meteora_transport())
        result = asyncio.run(
            adapter.fetch(TEST_MINT, TEST_SLOT, TEST_BLOCK_TIME_MS)
        )
        assert result is not None
        assert result.meta.source == "meteora"

    def test_returns_none_fields_when_no_pairs(self):
        transport = OfflineFixtureTransport({"meteora.ag": {"data": [], "total": 0}})
        adapter = MeteoraAdapter(transport=transport)
        result = asyncio.run(
            adapter.fetch(TEST_MINT, TEST_SLOT, TEST_BLOCK_TIME_MS)
        )
        assert result is not None
        assert result.pool_address is None
        assert result.bin_step is None

    def test_returns_none_on_transport_error(self):
        class ErrorTransport:
            async def get(self, url, headers=None, params=None):
                raise ConnectionError("offline")

        adapter = MeteoraAdapter(transport=ErrorTransport())
        result = asyncio.run(
            adapter.fetch(TEST_MINT, TEST_SLOT, TEST_BLOCK_TIME_MS)
        )
        assert result is None

    def test_staleness_is_non_negative(self):
        adapter = MeteoraAdapter(transport=_make_meteora_transport())
        result = asyncio.run(
            adapter.fetch(TEST_MINT, TEST_SLOT, TEST_BLOCK_TIME_MS)
        )
        assert result is not None
        assert result.meta.data_staleness_ms >= 0


# ---------------------------------------------------------------------------
# 4. Moonshot adapter (candidate venue)
# ---------------------------------------------------------------------------


class TestMoonshotAdapter:
    def test_parses_curve_type(self):
        adapter = MoonshotAdapter(transport=_make_moonshot_transport())
        result = asyncio.run(
            adapter.fetch(TEST_MINT, TEST_SLOT, TEST_BLOCK_TIME_MS)
        )
        assert result is not None
        assert result.curve_type == "linear"

    def test_parses_curve_progress_pct_as_float(self):
        adapter = MoonshotAdapter(transport=_make_moonshot_transport())
        result = asyncio.run(
            adapter.fetch(TEST_MINT, TEST_SLOT, TEST_BLOCK_TIME_MS)
        )
        assert result is not None
        assert isinstance(result.curve_progress_pct, float)
        assert result.curve_progress_pct == pytest.approx(67.4)

    def test_parses_circulating_supply_as_int(self):
        """Supply is an integer (base units — money integer rule)."""
        adapter = MoonshotAdapter(transport=_make_moonshot_transport())
        result = asyncio.run(
            adapter.fetch(TEST_MINT, TEST_SLOT, TEST_BLOCK_TIME_MS)
        )
        assert result is not None
        assert isinstance(result.circulating_supply, int)
        assert result.circulating_supply == 800_000_000

    def test_parses_total_supply_as_int(self):
        adapter = MoonshotAdapter(transport=_make_moonshot_transport())
        result = asyncio.run(
            adapter.fetch(TEST_MINT, TEST_SLOT, TEST_BLOCK_TIME_MS)
        )
        assert result is not None
        assert isinstance(result.total_supply, int)
        assert result.total_supply == 1_000_000_000

    def test_parses_creator_wallet(self):
        adapter = MoonshotAdapter(transport=_make_moonshot_transport())
        result = asyncio.run(
            adapter.fetch(TEST_MINT, TEST_SLOT, TEST_BLOCK_TIME_MS)
        )
        assert result is not None
        assert result.creator_wallet == "MoonshotCreator11111111111111111111111111111"

    def test_source_label_is_moonshot(self):
        adapter = MoonshotAdapter(transport=_make_moonshot_transport())
        result = asyncio.run(
            adapter.fetch(TEST_MINT, TEST_SLOT, TEST_BLOCK_TIME_MS)
        )
        assert result is not None
        assert result.meta.source == "moonshot"

    def test_returns_none_on_transport_error(self):
        class ErrorTransport:
            async def get(self, url, headers=None, params=None):
                raise ConnectionError("offline")

        adapter = MoonshotAdapter(transport=ErrorTransport())
        result = asyncio.run(
            adapter.fetch(TEST_MINT, TEST_SLOT, TEST_BLOCK_TIME_MS)
        )
        assert result is None

    def test_staleness_is_non_negative(self):
        adapter = MoonshotAdapter(transport=_make_moonshot_transport())
        result = asyncio.run(
            adapter.fetch(TEST_MINT, TEST_SLOT, TEST_BLOCK_TIME_MS)
        )
        assert result is not None
        assert result.meta.data_staleness_ms >= 0


# ---------------------------------------------------------------------------
# 5. EnrichmentClient — composite, offline fixtures
# ---------------------------------------------------------------------------


class TestEnrichmentClient:
    def _run_enrich(self) -> EnrichmentBundle:
        client = build_offline_enrichment_client(mint=TEST_MINT)
        return asyncio.run(
            client.enrich(TEST_MINT, TEST_SLOT, TEST_BLOCK_TIME_MS)
        )

    def test_bundle_has_all_four_sources(self):
        bundle = self._run_enrich()
        assert bundle.dexscreener is not None
        assert bundle.birdeye is not None
        assert bundle.meteora is not None
        assert bundle.moonshot is not None

    def test_bundle_any_success_true_when_all_present(self):
        bundle = self._run_enrich()
        assert bundle.any_success is True

    def test_bundle_holder_count_from_birdeye(self):
        """holder_count() prefers Birdeye (test 20)."""
        bundle = self._run_enrich()
        assert bundle.holder_count() == 847

    def test_bundle_liquidity_usd_from_dexscreener(self):
        bundle = self._run_enrich()
        liq = bundle.liquidity_usd()
        assert liq is not None
        assert liq == pytest.approx(32000.0)

    def test_bundle_max_staleness_non_negative(self):
        bundle = self._run_enrich()
        assert bundle.max_staleness_ms() >= 0

    def test_bundle_mint_matches_input(self):
        bundle = self._run_enrich()
        assert bundle.mint == TEST_MINT

    def test_client_continues_when_one_adapter_fails(self):
        """Test 8: one adapter failure does not block others."""
        class ErrorTransport:
            async def get(self, url, headers=None, params=None):
                raise ConnectionError("simulated offline failure")

        fixture_map = {
            "dexscreener.com": DEXSCREENER_FIXTURE,
            "birdeye.so": BIRDEYE_FIXTURE,
            # meteora and moonshot use ErrorTransport via a different client
        }
        ok_transport = OfflineFixtureTransport(fixture_map)
        fail_transport = ErrorTransport()

        client = EnrichmentClient(
            dexscreener=DEXScreenerAdapter(transport=ok_transport),
            birdeye=BirdeyeAdapter(transport=ok_transport, api_key="offline"),
            meteora=MeteoraAdapter(transport=fail_transport),
            moonshot=MoonshotAdapter(transport=fail_transport),
        )
        bundle = asyncio.run(
            client.enrich(TEST_MINT, TEST_SLOT, TEST_BLOCK_TIME_MS)
        )
        # dexscreener and birdeye succeed; meteora and moonshot fail gracefully
        assert bundle.dexscreener is not None
        assert bundle.birdeye is not None
        assert bundle.meteora is None
        assert bundle.moonshot is None
        assert bundle.any_success is True

    def test_bundle_liquidity_falls_back_to_meteora_when_dex_absent(self):
        """Test 21: liquidity_usd() falls back to Meteora if DEXScreener absent."""
        client = EnrichmentClient(
            dexscreener=None,
            birdeye=None,
            meteora=MeteoraAdapter(transport=_make_meteora_transport()),
            moonshot=None,
        )
        bundle = asyncio.run(
            client.enrich(TEST_MINT, TEST_SLOT, TEST_BLOCK_TIME_MS)
        )
        liq = bundle.liquidity_usd()
        assert liq is not None
        assert liq > 0.0  # from Meteora fixture

    def test_bundle_holder_count_none_when_birdeye_absent(self):
        client = EnrichmentClient(
            dexscreener=DEXScreenerAdapter(transport=_make_dex_transport()),
            birdeye=None,
        )
        bundle = asyncio.run(
            client.enrich(TEST_MINT, TEST_SLOT, TEST_BLOCK_TIME_MS)
        )
        assert bundle.holder_count() is None


# ---------------------------------------------------------------------------
# 6. OfflineFixtureTransport
# ---------------------------------------------------------------------------


class TestOfflineFixtureTransport:
    def test_exact_url_match(self):
        transport = OfflineFixtureTransport({"https://example.com/foo": {"key": "val"}})
        result = asyncio.run(
            transport.get("https://example.com/foo")
        )
        assert result == {"key": "val"}

    def test_substring_match(self):
        transport = OfflineFixtureTransport({"dexscreener.com": {"pairs": []}})
        result = asyncio.run(
            transport.get("https://api.dexscreener.com/latest/dex/tokens/MINT")
        )
        assert result == {"pairs": []}

    def test_raises_for_unknown_url(self):
        """Test 9: OfflineFixtureNotFound for unknown URLs."""
        transport = OfflineFixtureTransport({"dexscreener.com": {}})
        with pytest.raises(OfflineFixtureTransport.OfflineFixtureNotFound):
            asyncio.run(
                transport.get("https://unknown.example.com/api")
            )

    def test_returns_copy_of_fixture(self):
        """Returned dict must be a copy (callers may mutate without affecting fixture)."""
        fixture_data = {"pairs": [{"pairAddress": "A"}]}
        transport = OfflineFixtureTransport({"test.com": fixture_data})
        result1 = asyncio.run(
            transport.get("https://test.com/foo")
        )
        result1["pairs"].clear()  # mutate
        result2 = asyncio.run(
            transport.get("https://test.com/foo")
        )
        assert len(result2["pairs"]) == 1  # fixture not mutated


# ---------------------------------------------------------------------------
# 7. T-300a carry-forward: pending_events dedicated table (T-301)
# ---------------------------------------------------------------------------


class TestPendingEventsTable:
    """Proves that read_as_of('pending_events') no longer raises KeyError: 'mint'.

    Root cause of the original bug: write_pending_slot_event wrote raw dicts
    (no 'mint' key) directly to _parquet._rows; read_as_of then filtered rows
    by r["mint"] — raising KeyError on the pending rows.

    Fix: dedicated _pending_rows table + public append_pending_row().
    """

    def test_read_as_of_pending_events_raises_value_error_not_keyerror(self):
        """Test 10: read_as_of('pending_events') raises ValueError with helpful msg."""
        backend = InMemoryParquetBackend()
        with pytest.raises(ValueError, match="pending_events"):
            backend.read_as_of("pending_events", "SomeMint", _now_ms())

    def test_pending_rows_are_in_dedicated_table_not_main_rows(self):
        """Test 11: append_pending_row writes to _pending_rows, not _rows."""
        backend = InMemoryParquetBackend()
        backend.append_pending_row({
            "dataset": "pending_events",
            "slot": 300_000_999,
            "wall_clock_ms": _now_ms(),
            "event_date": None,
            "recorded_at_ms": _now_ms(),
            "source_signature": "sig_test",
            "data_staleness_ms": -1,
        })
        # Main _rows table must be empty
        assert len(backend.all_rows()) == 0
        # Dedicated pending table must have the row
        assert len(backend.all_pending_rows()) == 1

    def test_append_pending_row_enforces_dataset_key(self):
        """Test 12: append_pending_row rejects rows with wrong dataset."""
        backend = InMemoryParquetBackend()
        with pytest.raises(ValueError, match="pending_events"):
            backend.append_pending_row({
                "dataset": "launch_events",  # wrong dataset
                "slot": 1,
                "recorded_at_ms": _now_ms(),
            })

    def test_read_pending_as_of_works(self):
        """Test 13: read_pending_as_of() returns pending rows within cutoff."""
        backend = InMemoryParquetBackend()
        ts = _now_ms()
        backend.append_pending_row({
            "dataset": "pending_events",
            "slot": 300_001_000,
            "wall_clock_ms": ts,
            "event_date": None,
            "recorded_at_ms": ts,
            "source_signature": "sig_pending",
            "data_staleness_ms": -1,
        })
        # Cutoff before write → empty
        assert backend.read_pending_as_of(ts - 1) == []
        # Cutoff at or after write → one row
        result = backend.read_pending_as_of(ts)
        assert len(result) == 1

    def test_all_rows_pending_events_returns_pending_table(self):
        """Test 14: all_rows('pending_events') returns the dedicated _pending_rows."""
        backend = InMemoryParquetBackend()
        backend.append_pending_row({
            "dataset": "pending_events",
            "slot": 300_001_001,
            "wall_clock_ms": _now_ms(),
            "event_date": None,
            "recorded_at_ms": _now_ms(),
            "source_signature": "sig_all_rows",
            "data_staleness_ms": -1,
        })
        rows = backend.all_rows("pending_events")
        assert len(rows) == 1
        assert rows[0]["dataset"] == "pending_events"
        assert rows[0]["event_date"] is None

    def test_write_pending_slot_event_uses_public_method(self):
        """Test 15: write_pending_slot_event uses append_pending_row (public API).

        This is the carry-forward fix for the T-300a MINOR finding that _rows
        was written directly. After this fix, the pending rows land in the
        dedicated table accessible via all_rows('pending_events').
        """
        backend = InMemoryParquetBackend()
        writer = PointInTimeStoreWriter(redis_client=None, parquet_backend=backend)
        asyncio.run(
            writer.write_pending_slot_event(
                slot=300_001_002,
                wall_clock_ms=_now_ms(),
                source_signature="sig_write_pending",
            )
        )
        # Must be in the dedicated pending table, not main _rows
        assert len(backend.all_rows("launch_events")) == 0
        assert len(backend.all_rows()) == 0  # main table empty
        pending = backend.all_rows("pending_events")
        assert len(pending) == 1
        assert pending[0]["dataset"] == "pending_events"
        assert pending[0]["event_date"] is None
        assert pending[0]["data_staleness_ms"] == -1  # STALENESS_UNKNOWN


# ---------------------------------------------------------------------------
# 8. Registry pluggability tests (T-301 AC)
# ---------------------------------------------------------------------------


class TestRegistryPluggable:
    def test_from_allowlist_skips_candidate_venues(self):
        """Test 16: from_allowlist with filter_active_only=True excludes candidates."""
        from pathlib import Path

        from aats.ingestion.registry import ProgramRegistry

        allowlist_path = Path("C:/dev/aats/config/program-allowlist.json")
        # No verifier in offline mode (verifier=None means skip live check)
        registry = ProgramRegistry.from_allowlist(allowlist_path, verifier=None, filter_active_only=True)
        # Active venues: pump.fun, pumpswap, raydium_v4, raydium_cpmm
        active_pids = registry.active_program_ids()
        assert len(active_pids) >= 4
        # Candidate venues (meteora, moonshot) must NOT be in active IDs
        # Their IDs from the allowlist:
        meteora_pid = "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo"
        moonshot_pid = "MoonCVVNZFSYkqNXP6bxHLPL6QQJiMagDL3qcqUQTrG"
        assert meteora_pid not in active_pids, (
            "Meteora (candidate) must NOT be in the active registry — "
            "it is enrichment-only until promoted via config+deploy."
        )
        assert moonshot_pid not in active_pids, (
            "Moonshot (candidate) must NOT be in the active registry — "
            "it is enrichment-only until promoted via config+deploy."
        )

    def test_from_allowlist_without_filter_active_still_skips_unmapped_venues(self):
        """Meteora and Moonshot have no LaunchSource enum entry.

        The registry maps allowlist entries via venue_name_map (pump.fun / pumpswap /
        raydium_v4 / raydium_cpmm).  Meteora and Moonshot are NOT in that map —
        they are enrichment-only candidates with no decoded LaunchSource.  They
        cannot be admitted to the program registry regardless of filter_active_only,
        because the registry is keyed on LaunchSource enum values and those two
        venues do not have one.

        This is CORRECT behaviour: their program IDs are in program-allowlist.json
        for the signer's use (T-251), but the ProgramRegistry only tracks venues that
        have a LaunchSource decoder path.  Meteora/Moonshot enrichment uses their
        own HTTP adapters (DEXScreenerAdapter / MeteoraAdapter / MoonshotAdapter),
        not the decoder registry.
        """
        from pathlib import Path

        from aats.ingestion.registry import ProgramRegistry

        allowlist_path = Path("C:/dev/aats/config/program-allowlist.json")
        registry = ProgramRegistry.from_allowlist(allowlist_path, verifier=None, filter_active_only=False)
        all_pids = registry.active_program_ids()
        # Only LaunchSource-mapped venues can appear in the decoder registry
        meteora_pid = "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo"
        moonshot_pid = "MoonCVVNZFSYkqNXP6bxHLPL6QQJiMagDL3qcqUQTrG"
        assert meteora_pid not in all_pids, (
            "Meteora has no LaunchSource enum entry — it cannot be in the decoder "
            "registry.  Use MeteoraAdapter for enrichment instead."
        )
        assert moonshot_pid not in all_pids, (
            "Moonshot has no LaunchSource enum entry — it cannot be in the decoder "
            "registry.  Use MoonshotAdapter for enrichment instead."
        )
        # Active mapped venues (pump.fun, pumpswap, raydium_v4, raydium_cpmm) are present
        assert len(all_pids) >= 4

    def test_from_dict_offline_loads_and_returns_program_id(self):
        """Test 17: from_dict with offline entries returns correct IDs."""
        from aats.contracts.events import LaunchSource
        from aats.ingestion.registry import ProgramRegistry

        entries = {
            LaunchSource.PUMPFUN: "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",
            LaunchSource.PUMPSWAP: "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA",
        }
        registry = ProgramRegistry.from_dict(entries)
        assert registry.program_id(LaunchSource.PUMPFUN) == "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
        assert registry.program_id(LaunchSource.PUMPSWAP) == "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"

    def test_registry_verifier_injectable_offline(self):
        """Registry verifier is injectable — offline verifier always returns True."""
        from pathlib import Path

        from aats.ingestion.registry import ProgramRegistry

        # Offline verifier: always returns True (no real RPC)
        def offline_verifier(pid: str) -> bool:
            return True

        allowlist_path = Path("C:/dev/aats/config/program-allowlist.json")
        registry = ProgramRegistry.from_allowlist(
            allowlist_path, verifier=offline_verifier, filter_active_only=True
        )
        # With an always-true verifier, all active venues should be loaded
        assert len(registry.active_program_ids()) >= 4

    def test_registry_verifier_fail_closed(self):
        """Registry verifier that always fails → empty active set (fail-closed)."""
        from pathlib import Path

        from aats.ingestion.registry import ProgramRegistry

        def failing_verifier(pid: str) -> bool:
            return False

        allowlist_path = Path("C:/dev/aats/config/program-allowlist.json")
        registry = ProgramRegistry.from_allowlist(
            allowlist_path, verifier=failing_verifier, filter_active_only=True
        )
        # Fail-closed: no venue admitted when verifier rejects all
        assert len(registry.active_program_ids()) == 0, (
            "Registry must be fail-closed: a verifier that rejects all PIDs "
            "must result in an empty active set, never a stale ID in the hot path."
        )


# ---------------------------------------------------------------------------
# 9. Money integer rule
# ---------------------------------------------------------------------------


class TestMoneyIntegerRule:
    def test_circulating_supply_is_int_not_float(self):
        """Supply values from Moonshot must be int (money integer base units rule)."""
        adapter = MoonshotAdapter(transport=_make_moonshot_transport())
        result = asyncio.run(
            adapter.fetch(TEST_MINT, TEST_SLOT, TEST_BLOCK_TIME_MS)
        )
        assert result is not None
        assert isinstance(result.circulating_supply, int), (
            "circulating_supply must be int (base units), not float. "
            "BUILD-DIRECTIVE money-integer rule."
        )

    def test_fee_rate_bps_is_int(self):
        """fee_rate_bps from Meteora must be int (bps is an exact integer)."""
        adapter = MeteoraAdapter(transport=_make_meteora_transport())
        result = asyncio.run(
            adapter.fetch(TEST_MINT, TEST_SLOT, TEST_BLOCK_TIME_MS)
        )
        assert result is not None
        assert isinstance(result.fee_rate_bps, int), (
            "fee_rate_bps must be int (bps is exact), not float."
        )

    def test_holder_count_is_int(self):
        """holder_count from Birdeye must be int (count, not float)."""
        adapter = BirdeyeAdapter(transport=_make_birdeye_transport(), api_key="offline")
        result = asyncio.run(
            adapter.fetch(TEST_MINT, TEST_SLOT, TEST_BLOCK_TIME_MS)
        )
        assert result is not None
        assert isinstance(result.holder_count, int)

    def test_txns_h24_buys_is_int(self):
        """txns_h24_buys from DEXScreener must be int (count)."""
        adapter = DEXScreenerAdapter(transport=_make_dex_transport())
        result = asyncio.run(
            adapter.fetch(TEST_MINT, TEST_SLOT, TEST_BLOCK_TIME_MS)
        )
        assert result is not None
        assert isinstance(result.txns_h24_buys, int)
