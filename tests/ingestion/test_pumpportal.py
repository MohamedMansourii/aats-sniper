"""Unit tests for PumpPortalTransport — no live network in any test.

Coverage (task T-PUMPPORTAL AC requirements):
  (a) A subscribeNewToken frame → exactly one (LaunchEvent, sig, EventKind.CREATE)
      keyed on the base mint; fields mapped correctly from message JSON.
  (b) A message with NO on-chain block_time → zero events yielded (T-300a);
      wall-clock is NEVER substituted into event_time.
  (c) Malformed / unexpected frames are skipped safely — no crash, logged.
  (d) Both subscribe handshake frames are sent on connect.
  Extra: migration message → (LaunchEvent, sig, EventKind.WITHDRAW) with
         source=MIGRATION.

DESIGN
------
All tests are async (pytest-asyncio asyncio_mode=auto) and fully offline.

WebSocket is mocked by patching ``aats.ingestion.transport._ws_import`` to
return a fake websockets module whose ``connect()`` yields scripted messages.

RPC enrichment (_get_slot_time) is mocked with patch.object so each test
can independently control whether block_time is available.

``max_reconnect_attempts=1`` bounds the retry loop so tests terminate after
one WS pass without an artificial timeout.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, patch

from aats.contracts.events import LaunchSource
from aats.ingestion.decoders import EventKind
from aats.ingestion.transport import (
    PUMPPORTAL_DEFAULT_WS_URL,
    PumpPortalTransport,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_PUMP_PID = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
_BASE_MINT = "Mint1111111111111111111111111111111111111111"
_CREATOR   = "Creator111111111111111111111111111111111111"
_SIG_1     = "5KtPn3DV3gFcnGHCymZ7bEXKNkDMqWrPQ7NpHPBV2UHzFyVxMsT3d7zMZNywWQ"

# RPC response values used when enrichment succeeds
_ENRICH_SLOT        = 300_000_001
_ENRICH_BLOCK_TIME  = 1_718_700_001        # seconds (unix)
_ENRICH_BLOCK_TIME_MS = _ENRICH_BLOCK_TIME * 1_000


# ---------------------------------------------------------------------------
# Message fixtures
# ---------------------------------------------------------------------------

def _new_token_msg(
    mint: str = _BASE_MINT,
    creator: str = _CREATOR,
    signature: str = _SIG_1,
    v_sol: float = 30.0,
    v_tokens: float = 1_073_000_191.0,
) -> dict:
    """Realistic-looking PumpPortal subscribeNewToken message."""
    return {
        "txType": "create",
        "signature": signature,
        "mint": mint,
        "traderPublicKey": creator,
        "bondingCurveKey": "BondingCurve11111111111111111111111111111111",
        "vSolInBondingCurve": v_sol,
        "vTokensInBondingCurve": v_tokens,
        "marketCapSol": 1.5,
        "name": "Test Token",
        "symbol": "TEST",
        "uri": "https://example.com/metadata.json",
        "initialBuy": 0,
        "pool": "pump",
    }


def _migration_msg(
    mint: str = _BASE_MINT,
    signature: str = _SIG_1,
) -> dict:
    """Realistic-looking PumpPortal subscribeMigration message."""
    return {
        "txType": "migrate",
        "signature": signature,
        "mint": mint,
        "traderPublicKey": _CREATOR,
        "vSolInBondingCurve": 85.0,
        "vTokensInBondingCurve": 206_900_000.0,
        "pool": "raydium",
    }


# ---------------------------------------------------------------------------
# Fake WebSocket infrastructure (same pattern as test_ws_fallback.py)
# ---------------------------------------------------------------------------

class _FakePumpPortalWS:
    """Fake websockets.connect context manager for PumpPortal tests.

    Records every frame sent via send() in self.sent.
    Yields messages from self._messages as JSON strings.
    """

    def __init__(
        self,
        messages: list[dict],
        fail_on_connect: bool = False,
    ) -> None:
        self._messages = messages
        self._fail_on_connect = fail_on_connect
        self.sent: list[dict] = []  # frames sent by the transport

    async def __aenter__(self) -> _FakePumpPortalWS:
        if self._fail_on_connect:
            raise OSError("Connection refused (fake)")
        return self

    async def __aexit__(self, *args: Any) -> None:
        pass

    async def send(self, data: str) -> None:  # called by transport
        try:
            self.sent.append(json.loads(data))
        except json.JSONDecodeError:
            self.sent.append({"_raw": data})

    def __aiter__(self) -> AsyncIterator[str]:
        return self._aiter()

    async def _aiter(self) -> AsyncIterator[str]:
        for msg in self._messages:
            yield json.dumps(msg)


class _FakePPWsModule:
    """Minimal stub mimicking the ``websockets`` module for PumpPortal tests."""

    def __init__(self, ws: _FakePumpPortalWS) -> None:
        self._ws = ws

    def connect(self, *args: Any, **kwargs: Any) -> _FakePumpPortalWS:
        return self._ws


def _patch_pp_ws(ws: _FakePumpPortalWS):
    """Patch ``_ws_import`` to return a fake websockets module backed by ``ws``."""
    return patch(
        "aats.ingestion.transport._ws_import",
        return_value=_FakePPWsModule(ws),
    )


def _patch_get_slot_time(slot: int | None, block_time_ms: int | None):
    """Patch PumpPortalTransport._get_slot_time to return fixed values."""
    return patch.object(
        PumpPortalTransport,
        "_get_slot_time",
        new=AsyncMock(return_value=(slot, block_time_ms)),
    )


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def _make_transport(
    enrich_via_rpc: bool = False,  # tests control slot/time via _get_slot_time mock
) -> PumpPortalTransport:
    return PumpPortalTransport(
        ws_url="wss://fake-pumpportal.example/api/data",
        rpc_url="https://fake-rpc.example/?api-key=FAKE",
        pump_program_id=_PUMP_PID,
        enrich_via_rpc=enrich_via_rpc,
        max_reconnect_attempts=1,  # terminate after one attempt in tests
    )


# ---------------------------------------------------------------------------
# (d) Subscribe handshake — sent on connect
# ---------------------------------------------------------------------------

class TestPumpPortalHandshake:
    """(d) Both subscribe frames must be sent on every new WS connection."""

    async def test_subscribe_frames_sent_on_connect(self):
        """The transport sends subscribeNewToken and subscribeMigration on connect."""
        fake_ws = _FakePumpPortalWS(messages=[])  # no messages → stream closes immediately

        transport = _make_transport()
        collected: list = []

        with _patch_pp_ws(fake_ws):
            gen = transport.events(last_slot=0)
            try:
                async for item in gen:
                    collected.append(item)
            finally:
                await gen.aclose()

        # No events expected (no messages)
        assert collected == []

        # Both subscribe frames must have been sent
        methods_sent = {f.get("method") for f in fake_ws.sent}
        assert "subscribeNewToken" in methods_sent, (
            f"subscribeNewToken frame not sent; frames sent: {fake_ws.sent}"
        )
        assert "subscribeMigration" in methods_sent, (
            f"subscribeMigration frame not sent; frames sent: {fake_ws.sent}"
        )

    async def test_subscribe_frame_order(self):
        """subscribeNewToken is sent before subscribeMigration (canonical order)."""
        fake_ws = _FakePumpPortalWS(messages=[])
        transport = _make_transport()

        with _patch_pp_ws(fake_ws):
            gen = transport.events(last_slot=0)
            try:
                async for _ in gen:
                    pass
            finally:
                await gen.aclose()

        methods = [f.get("method") for f in fake_ws.sent]
        assert methods[0] == "subscribeNewToken"
        assert methods[1] == "subscribeMigration"


# ---------------------------------------------------------------------------
# (a) subscribeNewToken → EventKind.CREATE
# ---------------------------------------------------------------------------

class TestNewTokenMapping:
    """(a) A subscribeNewToken message yields exactly one (LaunchEvent, sig, CREATE)."""

    async def test_new_token_yields_create_event(self):
        """subscribeNewToken → (LaunchEvent, signature, EventKind.CREATE)."""
        msg = _new_token_msg()
        fake_ws = _FakePumpPortalWS(messages=[msg])
        transport = _make_transport()
        collected: list = []

        with _patch_pp_ws(fake_ws), _patch_get_slot_time(
            _ENRICH_SLOT, _ENRICH_BLOCK_TIME_MS
        ):
            gen = transport.events(last_slot=0)
            try:
                async for item in gen:
                    collected.append(item)
                    break  # take first, don't wait for reconnect
            finally:
                await gen.aclose()

        assert len(collected) == 1, f"Expected 1 event, got {len(collected)}"
        event, sig, kind = collected[0]

        # (a) keyed on the BASE mint
        assert event.mint == _BASE_MINT, (
            f"event.mint must be the base mint {_BASE_MINT!r}, got {event.mint!r}"
        )
        assert kind == EventKind.CREATE, f"Expected EventKind.CREATE, got {kind!r}"
        assert sig == _SIG_1, f"Expected sig {_SIG_1!r}, got {sig!r}"

    async def test_new_token_source_is_pumpfun(self):
        """source must be LaunchSource.PUMPFUN for new tokens."""
        msg = _new_token_msg()
        fake_ws = _FakePumpPortalWS(messages=[msg])
        transport = _make_transport()

        with _patch_pp_ws(fake_ws), _patch_get_slot_time(
            _ENRICH_SLOT, _ENRICH_BLOCK_TIME_MS
        ):
            gen = transport.events(last_slot=0)
            try:
                async for event, _sig, _kind in gen:
                    assert event.source == LaunchSource.PUMPFUN
                    break
            finally:
                await gen.aclose()

    async def test_new_token_venue_program_id(self):
        """venue_program_id must be the pump.fun program ID from the registry."""
        msg = _new_token_msg()
        fake_ws = _FakePumpPortalWS(messages=[msg])
        transport = _make_transport()

        with _patch_pp_ws(fake_ws), _patch_get_slot_time(
            _ENRICH_SLOT, _ENRICH_BLOCK_TIME_MS
        ):
            gen = transport.events(last_slot=0)
            try:
                async for event, _sig, _kind in gen:
                    assert event.venue_program_id == _PUMP_PID
                    break
            finally:
                await gen.aclose()

    async def test_new_token_creator_wallet(self):
        """creator_wallet comes from message traderPublicKey."""
        msg = _new_token_msg(creator=_CREATOR)
        fake_ws = _FakePumpPortalWS(messages=[msg])
        transport = _make_transport()

        with _patch_pp_ws(fake_ws), _patch_get_slot_time(
            _ENRICH_SLOT, _ENRICH_BLOCK_TIME_MS
        ):
            gen = transport.events(last_slot=0)
            try:
                async for event, _sig, _kind in gen:
                    assert event.creator_wallet == _CREATOR
                    break
            finally:
                await gen.aclose()

    async def test_new_token_sol_reserve_is_integer_lamports(self):
        """sol_reserve_lamports must be int (money rule — no floats, data-models §0)."""
        # 30.0 SOL = 30_000_000_000 lamports
        msg = _new_token_msg(v_sol=30.0)
        fake_ws = _FakePumpPortalWS(messages=[msg])
        transport = _make_transport()

        with _patch_pp_ws(fake_ws), _patch_get_slot_time(
            _ENRICH_SLOT, _ENRICH_BLOCK_TIME_MS
        ):
            gen = transport.events(last_slot=0)
            try:
                async for event, _sig, _kind in gen:
                    assert isinstance(event.sol_reserve_lamports, int), (
                        "sol_reserve_lamports must be int (money rule)"
                    )
                    assert event.sol_reserve_lamports == 30_000_000_000
                    break
            finally:
                await gen.aclose()

    async def test_new_token_token_reserve_is_integer_base_units(self):
        """token_reserve_base must be int (money rule)."""
        msg = _new_token_msg(v_tokens=1_073_000_191.0)
        fake_ws = _FakePumpPortalWS(messages=[msg])
        transport = _make_transport()

        with _patch_pp_ws(fake_ws), _patch_get_slot_time(
            _ENRICH_SLOT, _ENRICH_BLOCK_TIME_MS
        ):
            gen = transport.events(last_slot=0)
            try:
                async for event, _sig, _kind in gen:
                    assert isinstance(event.token_reserve_base, int)
                    assert event.token_reserve_base == 1_073_000_191
                    break
            finally:
                await gen.aclose()

    async def test_new_token_event_time_uses_rpc_block_time(self):
        """event_time.block_time_ms must be the RPC-returned on-chain block_time."""
        msg = _new_token_msg()
        fake_ws = _FakePumpPortalWS(messages=[msg])
        transport = _make_transport()

        with _patch_pp_ws(fake_ws), _patch_get_slot_time(
            _ENRICH_SLOT, _ENRICH_BLOCK_TIME_MS
        ):
            gen = transport.events(last_slot=0)
            try:
                async for event, _sig, _kind in gen:
                    assert event.event_time.slot == _ENRICH_SLOT
                    assert event.event_time.block_time_ms == _ENRICH_BLOCK_TIME_MS
                    break
            finally:
                await gen.aclose()

    async def test_new_token_exactly_one_event_per_message(self):
        """One subscribeNewToken message produces exactly one event (no duplication)."""
        msg = _new_token_msg()
        fake_ws = _FakePumpPortalWS(messages=[msg])
        transport = _make_transport()
        collected: list = []

        with _patch_pp_ws(fake_ws), _patch_get_slot_time(
            _ENRICH_SLOT, _ENRICH_BLOCK_TIME_MS
        ):
            gen = transport.events(last_slot=0)
            try:
                async for item in gen:
                    collected.append(item)
            finally:
                await gen.aclose()

        assert len(collected) == 1, f"Expected 1 event, got {len(collected)}"


# ---------------------------------------------------------------------------
# (b) T-300a — absent block_time → zero events yielded, no wall-clock
# ---------------------------------------------------------------------------

class TestT300aBlockTimeHonesty:
    """(b) T-300a: no on-chain block_time → event_time None (not yielded)."""

    async def test_no_block_time_yields_no_event(self):
        """When _get_slot_time returns (None, None), no LaunchEvent is yielded.

        This is the T-300a compliance test: PumpPortal server timestamps are
        receive times, not on-chain block_times.  Without a real on-chain time,
        we must NOT emit a LaunchEvent.
        """
        msg = _new_token_msg()
        fake_ws = _FakePumpPortalWS(messages=[msg])
        transport = _make_transport()
        collected: list = []

        # _get_slot_time returns (None, None) → no block_time available
        with _patch_pp_ws(fake_ws), _patch_get_slot_time(None, None):
            gen = transport.events(last_slot=0)
            try:
                async for item in gen:
                    collected.append(item)
            finally:
                await gen.aclose()

        assert collected == [], (
            "T-300a violation: an event was yielded despite absent block_time.  "
            "event_time must NEVER be wall-clock-substituted."
        )

    async def test_no_block_time_increments_skipped_counter(self):
        """Pending events (no block_time) are counted in stats.events_skipped."""
        msg = _new_token_msg()
        fake_ws = _FakePumpPortalWS(messages=[msg])
        transport = _make_transport()

        with _patch_pp_ws(fake_ws), _patch_get_slot_time(None, None):
            gen = transport.events(last_slot=0)
            try:
                async for _ in gen:
                    pass
            finally:
                await gen.aclose()

        assert transport.stats.events_skipped > 0, (
            "A pending event (no block_time) must increment events_skipped."
        )
        assert transport._pending_no_block_time > 0, (
            "_pending_no_block_time counter must be non-zero."
        )

    async def test_enrich_disabled_yields_no_event(self):
        """When enrich_via_rpc=False, _get_slot_time returns (None, None) → no events."""
        # Do NOT mock _get_slot_time here; let the real method run with
        # enrich_via_rpc=False so it immediately returns (None, None).
        transport = PumpPortalTransport(
            ws_url="wss://fake-pumpportal.example/api/data",
            rpc_url="",           # no RPC URL — enrichment disabled
            pump_program_id=_PUMP_PID,
            enrich_via_rpc=False, # explicit disable
            max_reconnect_attempts=1,
        )
        msg = _new_token_msg()
        fake_ws = _FakePumpPortalWS(messages=[msg])
        collected: list = []

        with _patch_pp_ws(fake_ws):
            gen = transport.events(last_slot=0)
            try:
                async for item in gen:
                    collected.append(item)
            finally:
                await gen.aclose()

        assert collected == [], (
            "T-300a: with enrich_via_rpc=False no event should be yielded "
            "(wall-clock must NEVER be substituted for block_time_ms)."
        )

    async def test_wall_clock_not_used_as_event_time(self):
        """Verify that wall-clock time does not appear in event_time.block_time_ms.

        This test patches _pp_now_ms to return a distinctive value (999_999_999)
        and verifies that when a real block_time IS available, event_time.block_time_ms
        is the on-chain time (not the wall-clock value).  Confirms the separation.
        """
        _WALL_CLOCK_MS = 999_999_999_000  # distinctive wall-clock sentinel
        msg = _new_token_msg()
        fake_ws = _FakePumpPortalWS(messages=[msg])
        transport = _make_transport()

        with (
            _patch_pp_ws(fake_ws),
            _patch_get_slot_time(_ENRICH_SLOT, _ENRICH_BLOCK_TIME_MS),
            patch("aats.ingestion.transport._pp_now_ms", return_value=_WALL_CLOCK_MS),
        ):
            gen = transport.events(last_slot=0)
            try:
                async for event, _sig, _kind in gen:
                    # block_time_ms must be the on-chain value
                    assert event.event_time.block_time_ms == _ENRICH_BLOCK_TIME_MS, (
                        "T-300a: event_time.block_time_ms is the on-chain value, "
                        f"not the wall-clock {_WALL_CLOCK_MS}"
                    )
                    # wall_clock_ms may be the wall-clock sentinel (monitoring only)
                    assert event.event_time.block_time_ms != _WALL_CLOCK_MS, (
                        "T-300a violation: block_time_ms equals wall-clock sentinel"
                    )
                    break
            finally:
                await gen.aclose()


# ---------------------------------------------------------------------------
# (c) Malformed / unexpected frames — no crash
# ---------------------------------------------------------------------------

class TestMalformedFrames:
    """(c) Malformed and unexpected frames are skipped without crashing."""

    async def _run_with_messages(self, messages: list[Any]) -> tuple[list, PumpPortalTransport]:
        """Run transport with given raw string/json messages, return (events, transport)."""
        # _FakePumpPortalWS expects dict messages (it json.dumps them).
        # For raw string injection, we override _aiter.
        class _RawWS(_FakePumpPortalWS):
            def __init__(self, raw_messages: list[Any]) -> None:
                super().__init__(messages=[])
                self._raw_messages = raw_messages

            async def _aiter(self) -> AsyncIterator[str]:  # type: ignore[override]
                for m in self._raw_messages:
                    if isinstance(m, str):
                        yield m
                    else:
                        yield json.dumps(m)

        transport = _make_transport()
        collected: list = []
        fake_ws = _RawWS(messages)

        with _patch_pp_ws(fake_ws), _patch_get_slot_time(
            _ENRICH_SLOT, _ENRICH_BLOCK_TIME_MS
        ):
            gen = transport.events(last_slot=0)
            try:
                async for item in gen:
                    collected.append(item)
            finally:
                await gen.aclose()

        return collected, transport

    async def test_malformed_json_is_skipped(self):
        """Invalid JSON is skipped without raising."""
        collected, transport = await self._run_with_messages(["not-json{{{"])
        assert collected == []
        assert transport.stats.events_skipped > 0

    async def test_non_dict_json_is_skipped(self):
        """A valid JSON array/string (not a dict) is skipped."""
        collected, transport = await self._run_with_messages(['["array", "not", "dict"]'])
        assert collected == []
        assert transport.stats.events_skipped > 0

    async def test_missing_mint_field_is_skipped(self):
        """A message without a 'mint' field is skipped."""
        msg = {"txType": "create", "signature": _SIG_1, "traderPublicKey": _CREATOR}
        collected, _ = await self._run_with_messages([msg])
        assert collected == []

    async def test_unknown_tx_type_is_skipped(self):
        """An unrecognised txType is skipped without crash."""
        msg = {"txType": "unknownXYZ", "mint": _BASE_MINT, "signature": _SIG_1}
        collected, _ = await self._run_with_messages([msg])
        assert collected == []

    async def test_trade_message_is_skipped(self):
        """A buy/sell trade message (subscribeTokenTrade) is skipped silently."""
        for tx_type in ("buy", "sell", "swap"):
            msg = {
                "txType": tx_type,
                "mint": _BASE_MINT,
                "signature": _SIG_1,
                "traderPublicKey": _CREATOR,
                "tokenAmount": 1_000_000_000,
                "solAmount": 0.1,
            }
            collected, _ = await self._run_with_messages([msg])
            assert collected == [], f"txType={tx_type!r} should be skipped"

    async def test_empty_string_message_is_skipped(self):
        """An empty string is skipped without crash."""
        collected, _ = await self._run_with_messages([""])
        assert collected == []

    async def test_null_json_is_skipped(self):
        """JSON null is skipped without crash (not a dict)."""
        collected, _ = await self._run_with_messages(["null"])
        assert collected == []

    async def test_mix_of_bad_and_good_frames(self):
        """Bad frames are skipped; a following good frame is still decoded."""
        bad_msgs = [
            "not-json",
            '["array"]',
            {"txType": "unknownXYZ", "mint": _BASE_MINT, "signature": _SIG_1},
        ]
        good_msg = _new_token_msg()
        all_msgs = bad_msgs + [good_msg]

        collected, transport = await self._run_with_messages(all_msgs)
        # Exactly one event from the good message
        assert len(collected) == 1
        _event, _sig, kind = collected[0]
        assert kind == EventKind.CREATE
        # Three bad frames were skipped
        assert transport.stats.events_skipped >= 3


# ---------------------------------------------------------------------------
# Migration mapping
# ---------------------------------------------------------------------------

class TestMigrationMapping:
    """subscribeMigration → (LaunchEvent, sig, EventKind.WITHDRAW)."""

    async def test_migration_yields_withdraw_event(self):
        """A migration message yields EventKind.WITHDRAW."""
        msg = _migration_msg()
        fake_ws = _FakePumpPortalWS(messages=[msg])
        transport = _make_transport()
        collected: list = []

        with _patch_pp_ws(fake_ws), _patch_get_slot_time(
            _ENRICH_SLOT, _ENRICH_BLOCK_TIME_MS
        ):
            gen = transport.events(last_slot=0)
            try:
                async for item in gen:
                    collected.append(item)
                    break
            finally:
                await gen.aclose()

        assert len(collected) == 1
        event, sig, kind = collected[0]
        assert kind == EventKind.WITHDRAW, f"Expected EventKind.WITHDRAW, got {kind!r}"
        assert event.mint == _BASE_MINT
        assert event.source == LaunchSource.MIGRATION
        assert sig == _SIG_1

    async def test_migration_no_block_time_yields_nothing(self):
        """T-300a: migration without block_time → no yield."""
        msg = _migration_msg()
        fake_ws = _FakePumpPortalWS(messages=[msg])
        transport = _make_transport()
        collected: list = []

        with _patch_pp_ws(fake_ws), _patch_get_slot_time(None, None):
            gen = transport.events(last_slot=0)
            try:
                async for item in gen:
                    collected.append(item)
            finally:
                await gen.aclose()

        assert collected == []


# ---------------------------------------------------------------------------
# Stats tracking
# ---------------------------------------------------------------------------

class TestStats:
    """Transport-level stats are updated correctly."""

    async def test_decoded_event_increments_events_decoded(self):
        """A successfully decoded event increments stats.events_decoded."""
        msg = _new_token_msg()
        fake_ws = _FakePumpPortalWS(messages=[msg])
        transport = _make_transport()

        with _patch_pp_ws(fake_ws), _patch_get_slot_time(
            _ENRICH_SLOT, _ENRICH_BLOCK_TIME_MS
        ):
            gen = transport.events(last_slot=0)
            try:
                async for _ in gen:
                    pass
            finally:
                await gen.aclose()

        assert transport.stats.events_decoded == 1

    async def test_decoded_event_updates_last_slot(self):
        """A decoded event updates stats.last_slot to the on-chain slot."""
        msg = _new_token_msg()
        fake_ws = _FakePumpPortalWS(messages=[msg])
        transport = _make_transport()

        with _patch_pp_ws(fake_ws), _patch_get_slot_time(
            _ENRICH_SLOT, _ENRICH_BLOCK_TIME_MS
        ):
            gen = transport.events(last_slot=0)
            try:
                async for _ in gen:
                    pass
            finally:
                await gen.aclose()

        assert transport.stats.last_slot == _ENRICH_SLOT


# ---------------------------------------------------------------------------
# Constants / defaults
# ---------------------------------------------------------------------------

class TestDefaults:
    """Module-level constants and defaults are correct."""

    def test_default_ws_url_is_pumpportal(self):
        assert PUMPPORTAL_DEFAULT_WS_URL == "wss://pumpportal.fun/api/data"

    def test_subscribe_frames_contain_new_token_and_migration(self):
        methods = {f["method"] for f in PumpPortalTransport._SUBSCRIBE_FRAMES}
        assert "subscribeNewToken" in methods
        assert "subscribeMigration" in methods

    def test_empty_ws_url_yields_nothing(self):
        """Transport with no WS URL logs an error and yields nothing (graceful)."""
        transport = PumpPortalTransport(
            ws_url="",
            rpc_url="",
            pump_program_id=_PUMP_PID,
            max_reconnect_attempts=1,
        )

        async def _collect() -> list:
            out = []
            async for item in transport.events(last_slot=0):
                out.append(item)
            return out

        import asyncio
        result = asyncio.run(_collect())
        assert result == []
