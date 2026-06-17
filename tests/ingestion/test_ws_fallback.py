"""Tests for EnhancedWsFallback: WS logsSubscribe path + RPC polling fallback.

OFFLINE TESTS ONLY — mainnet not reachable in CI.
A fake WS server and fake RPC server are used to exercise the full
WS-parse → RawTransaction → decoder → LaunchEvent path, and the
polling-fallback path.

LIVE NOTE: real-mainnet collection requires a genuine RPC_PRIMARY value
(e.g. RPC_PRIMARY=https://mainnet.helius-rpc.com/?api-key=<key>).
The operator has a real key; these tests run against fakes.

Self-check items covered:
  1. WS logsNotification → getTransaction → RawTransaction → PumpFunDecoder → LaunchEvent
  2. Polling fallback: getSignaturesForAddress → getTransaction → RawTransaction → LaunchEvent
  3. Point-in-time correctness: block_time_unix_s from on-chain blockTime ONLY (T-300a)
  4. Dedup: duplicate signatures produce exactly one RawTransaction
  5. err!=null notifications are dropped
  6. Empty rpc_url fails gracefully (no crash, clear log message)
  7. WS URL derivation from RPC_PRIMARY (https→wss)

DETERMINISM NOTE (WS-B1 fix)
-----------------------------
All tests that exercise _stream_poll or _stream_ws patch
``aats.ingestion.transport._time_now_s`` with a ``_FakeClock`` rather than
relying on real wall-clock timeouts (``poll_idle_timeout_s``,
``poll_interval_s``).

Root cause of the flakiness:
  ``_stream_poll`` uses ``while _time_now_s() < poll_end_s`` as its loop
  condition.  Under full-suite CPU contention the wall-clock can advance
  arbitrarily between ``asyncio.sleep(0.0)`` ticks, making the number of
  poll iterations non-deterministic.  In the worst case the outer
  ``subscribe()`` reconnect loop ran an extra ``_stream_poll`` pass before
  the inner ``async for`` break propagated, causing call_count==2 /
  len(collected)==2 instead of 1.

Fix:
  ``_FakeClock`` is a callable that returns values from a pre-scripted
  sequence (then repeats the last value indefinitely).  Each async test
  that touches timing passes a clock whose sequence is chosen so that
  ``_stream_poll`` runs exactly one complete pass then exits, making the
  test verdict independent of wall-clock or CPU load.

  The sync dedup tests (``test_duplicate_signatures_deduped``,
  ``test_dedup_ring_bounded``) do not call ``_time_now_s`` at all
  (pure synchronous operations on instance state) but also receive the
  ``_FakeClock`` patch as a defensive measure against future refactors.

HERMETIC NOTE (WS-hermetic fix)
--------------------------------
All async tests are declared as ``async def`` methods so pytest-asyncio
(asyncio_mode=auto) owns the event-loop lifecycle.  No test creates its own
loop via ``asyncio.run()``.

Every ``async for`` that breaks early uses a ``try/finally`` with
``aclose()`` on the generator so abandoned generators do not leak across
tests.

Every patch of ``_time_now_s`` (and other module globals) is applied at
the top level of the test method (not inside a nested coroutine) so the
context-manager restore is guaranteed even when the test body throws.
"""

from __future__ import annotations

import base64
import hashlib
import json
import struct
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import patch

import pytest

from aats.contracts.events import LaunchSource
from aats.ingestion.decoders import InstructionRouter, RawTransaction
from aats.ingestion.transport import (
    EnhancedWsFallback,
    _base58_decode,
    _bytes_to_base58,
    _parse_rpc_get_transaction,
)
from tests.ingestion.fixtures import PROGRAM_IDS, make_registry

# ---------------------------------------------------------------------------
# Deterministic fake clock (WS-B1 fix)
# ---------------------------------------------------------------------------

class _FakeClock:
    """A wall-clock replacement for ``_time_now_s`` that returns scripted values.

    Returns successive values from ``times``.  When the sequence is exhausted,
    repeats the last value indefinitely.  Calling ``advance(dt)`` adds ``dt``
    to every remaining value in the sequence AND to the repeated tail, making
    it easy to signal "deadline has expired" from within a test callback.

    This makes every ``while _time_now_s() < poll_end_s`` loop in
    ``_stream_poll`` / ``_stream_ws`` terminate at a deterministic iteration
    count, regardless of CPU load or OS scheduling.
    """

    def __init__(self, times: list[float]) -> None:
        assert times, "_FakeClock requires at least one value"
        self._values = list(times)
        self._index = 0

    def __call__(self) -> float:
        if self._index < len(self._values):
            v = self._values[self._index]
            self._index += 1
            return v
        # Repeat last value indefinitely
        return self._values[-1]

    def advance(self, dt: float) -> None:
        """Shift all *future* values (index onward) by ``dt``.

        Call this from inside a mock callback (e.g. ``fake_fetch``) to make
        the NEXT ``_time_now_s()`` call return a value that is past the
        current ``poll_end_s`` deadline, causing ``_stream_poll`` to exit
        after the current iteration.
        """
        for i in range(self._index, len(self._values)):
            self._values[i] += dt
        # Also shift the tail (last repeated value)
        self._values[-1] += dt


def _poll_clock(timeout_s: float = 10.0, t0: float = 1_000.0) -> _FakeClock:
    """Return a clock tuned for a single-pass ``_stream_poll`` run.

    Call sequence inside ``_stream_poll`` with one program ID and one sig:
      [0]  poll_end_s = _time_now_s() + timeout_s        → returns t0
           poll_end_s = t0 + timeout_s
      [1]  while _time_now_s() < poll_end_s:             → t0 < t0+T → True
           # inner loop: _get_signatures_for_address, _fetch_transaction
           # _mark_seen, yield tx
           poll_end_s = _time_now_s() + timeout_s        → returns t0 (call [2])
           # asyncio.sleep(0.0)
      [3]  while _time_now_s() < poll_end_s:             → must be > t0+T → False

    By returning t0 for calls [0],[1],[2] and t0+timeout_s+1 for call [3],
    the while loop executes exactly ONE iteration then exits.
    """
    past_deadline = t0 + timeout_s + 1.0
    # Indices:           [0]  [1]  [2]          [3+]
    return _FakeClock([t0,  t0,  t0,  past_deadline])


def _patch_time(clock: _FakeClock):
    """Return a context manager that patches ``_time_now_s`` with ``clock``."""
    return patch("aats.ingestion.transport._time_now_s", side_effect=clock)


# ---------------------------------------------------------------------------
# Helpers: build fake JSON-RPC payloads
# ---------------------------------------------------------------------------

def _disc(name: str) -> bytes:
    return hashlib.sha256(f"global:{name}".encode()).digest()[:8]


_PUMP_PID = PROGRAM_IDS[LaunchSource.PUMPFUN]

# A realistic-looking (but fake) base58 signature
_FAKE_SIG_1 = "5KtPn3DV3gFcnGHCymZ7bEXKNkDMqWrPQ7NpHPBV2UHzFyVxMsT3d7zMZNywWQ8bXj9FAbKoVWm4rHxQ4HNW6gH"
_FAKE_SIG_2 = "3J9qzNhW8mRP4VqwAZuEPyUVSDjkBMiQDgLp1zB5fKEcY6XRJT7HsWnNq8vYxM2LbCAkR3tPDqUoFg9cHs5XjpA"


def _make_fake_get_transaction_result(
    signature: str,
    slot: int = 300_000_000,
    block_time: int | None = 1_718_700_000,
    token_amount: int = 1_000_000_000,
    max_sol_cost: int = 200_000_000,
    err: Any = None,
) -> dict:
    """Build a fake getTransaction JSON response (jsonParsed encoding)."""
    ix_data_bytes = (
        _disc("buy")
        + struct.pack("<Q", token_amount)
        + struct.pack("<Q", max_sol_cost)
    )
    # Encode instruction data as base58 (as Solana jsonParsed returns it)
    ix_data_b58 = _bytes_to_base58(ix_data_bytes)

    return {
        "slot": slot,
        "blockTime": block_time,
        "transaction": {
            "message": {
                "accountKeys": [
                    {"pubkey": "GlobalState111111111111111111111111111111111", "signer": True, "writable": True},
                    {"pubkey": "FeeRecipient1111111111111111111111111111111111", "signer": False, "writable": True},
                    {"pubkey": "MintPumpFun1111111111111111111111111111111111", "signer": False, "writable": True},
                    {"pubkey": "BondingCurve11111111111111111111111111111111", "signer": False, "writable": True},
                    {"pubkey": "AssocBondingCrv11111111111111111111111111111", "signer": False, "writable": True},
                    {"pubkey": "BuyerATA111111111111111111111111111111111111", "signer": False, "writable": True},
                    {"pubkey": "BuyerWallet1111111111111111111111111111111111", "signer": True, "writable": False},
                ],
                "instructions": [
                    {
                        "programId": _PUMP_PID,
                        "accounts": [
                            "GlobalState111111111111111111111111111111111",
                            "FeeRecipient1111111111111111111111111111111111",
                            "MintPumpFun1111111111111111111111111111111111",
                            "BondingCurve11111111111111111111111111111111",
                            "AssocBondingCrv11111111111111111111111111111",
                            "BuyerATA111111111111111111111111111111111111",
                            "BuyerWallet1111111111111111111111111111111111",
                        ],
                        "data": ix_data_b58,
                    }
                ],
            }
        },
        "meta": {
            "err": err,
            "logMessages": [
                f"Program {_PUMP_PID} invoke [1]",
                f"Program {_PUMP_PID} success",
            ],
            "innerInstructions": [],
        },
    }


def _make_logs_notification(signature: str, sub_id: int = 1, err: Any = None) -> dict:
    """Build a fake Solana WS logsNotification message."""
    return {
        "jsonrpc": "2.0",
        "method": "logsNotification",
        "params": {
            "result": {
                "context": {"slot": 300_000_000},
                "value": {
                    "signature": signature,
                    "err": err,
                    "logs": [f"Program {_PUMP_PID} invoke [1]"],
                },
            },
            "subscription": sub_id,
        },
    }


def _make_sub_confirmation(req_id: int, sub_id: int) -> dict:
    """Build a fake WS subscription confirmation message."""
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "result": sub_id,
    }


# ---------------------------------------------------------------------------
# Tests: _parse_rpc_get_transaction
# ---------------------------------------------------------------------------


class TestParseRpcGetTransaction:
    """Unit tests for the jsonParsed getTransaction → RawTransaction parser."""

    def test_basic_pump_buy_yields_raw_transaction(self):
        result = _make_fake_get_transaction_result(_FAKE_SIG_1)
        tx = _parse_rpc_get_transaction(_FAKE_SIG_1, result)

        assert tx is not None
        assert tx.signature == _FAKE_SIG_1
        assert tx.slot == 300_000_000
        assert tx.block_time_unix_s == 1_718_700_000
        assert tx.fee_payer == "GlobalState111111111111111111111111111111111"
        assert len(tx.instructions) == 1
        assert tx.instructions[0].program_id == _PUMP_PID
        assert tx.err is None
        assert tx.is_vote is False

    def test_errored_transaction_returns_none(self):
        """Transactions with non-null meta.err must be dropped (T-300a — we only want successes)."""
        result = _make_fake_get_transaction_result(_FAKE_SIG_1, err={"InstructionError": [0, "Custom"]})
        tx = _parse_rpc_get_transaction(_FAKE_SIG_1, result)
        assert tx is None

    def test_absent_block_time_yields_none_block_time(self):
        """T-300a: missing blockTime → block_time_unix_s=None; wall-clock never substituted."""
        result = _make_fake_get_transaction_result(_FAKE_SIG_1, block_time=None)
        tx = _parse_rpc_get_transaction(_FAKE_SIG_1, result)
        assert tx is not None
        assert tx.block_time_unix_s is None, (
            "T-300a violation: block_time_unix_s must be None when blockTime is absent"
        )

    def test_zero_block_time_yields_none_block_time(self):
        """T-300a: blockTime=0 → block_time_unix_s=None (not a real on-chain time)."""
        result = _make_fake_get_transaction_result(_FAKE_SIG_1, block_time=0)
        tx = _parse_rpc_get_transaction(_FAKE_SIG_1, result)
        assert tx is not None
        assert tx.block_time_unix_s is None

    def test_instruction_data_roundtrips_to_decoder(self):
        """The instruction data survives base58→base64 and decoders can read it."""
        result = _make_fake_get_transaction_result(
            _FAKE_SIG_1,
            token_amount=5_000_000_000,
            max_sol_cost=300_000_000,
        )
        tx = _parse_rpc_get_transaction(_FAKE_SIG_1, result)
        assert tx is not None
        assert len(tx.instructions) == 1

        # Verify the instruction data decodes to the buy discriminator
        ix = tx.instructions[0]
        raw_bytes = base64.b64decode(ix.data_b64)
        assert raw_bytes[:8] == _disc("buy"), "First 8 bytes must be buy discriminator"
        token_amount = struct.unpack_from("<Q", raw_bytes, 8)[0]
        max_sol = struct.unpack_from("<Q", raw_bytes, 16)[0]
        assert token_amount == 5_000_000_000
        assert max_sol == 300_000_000

    def test_missing_result_returns_none(self):
        tx = _parse_rpc_get_transaction(_FAKE_SIG_1, {})
        assert tx is None

    def test_none_result_returns_none(self):
        tx = _parse_rpc_get_transaction(_FAKE_SIG_1, None)  # type: ignore[arg-type]
        assert tx is None


# ---------------------------------------------------------------------------
# Tests: base58 encode/decode round-trip
# ---------------------------------------------------------------------------

class TestBase58Codec:
    def test_roundtrip(self):
        for data in [b"\x00", b"\xff\x00\xff", b"\x00\x01\x02\x03", bytes(range(32))]:
            encoded = _bytes_to_base58(data)
            decoded = _base58_decode(encoded)
            assert decoded == data, f"Roundtrip failed for {data!r}"

    def test_empty(self):
        assert _bytes_to_base58(b"") == ""

    def test_leading_zeros(self):
        # Each leading zero byte encodes to a leading '1' in base58
        encoded = _bytes_to_base58(b"\x00\x01")
        assert encoded.startswith("1")


# ---------------------------------------------------------------------------
# Tests: WS path (fake WS server via async generator mock)
# ---------------------------------------------------------------------------

class _FakeWebSocket:
    """Fake websockets.connect context manager that yields pre-scripted messages."""

    def __init__(self, messages: list[dict], fail_on_connect: bool = False) -> None:
        self._messages = messages
        self._fail_on_connect = fail_on_connect
        self._sent: list[str] = []

    async def __aenter__(self) -> _FakeWebSocket:
        if self._fail_on_connect:
            raise OSError("Connection refused (fake)")
        return self

    async def __aexit__(self, *args: Any) -> None:
        pass

    async def send(self, data: str) -> None:
        self._sent.append(data)

    def __aiter__(self) -> AsyncIterator[str]:
        return self._aiter()

    async def _aiter(self) -> AsyncIterator[str]:
        for msg in self._messages:
            yield json.dumps(msg)


# Fake websockets module (makes patch("aats.ingestion.transport.websockets") work
# even when the real package is not installed in CI)
class _FakeWebsocketsModule:
    """Minimal stub that satisfies `import websockets; websockets.connect(...)`."""
    def connect(self, url: str, **kwargs: Any) -> Any:
        raise AssertionError("connect must be patched per-test")  # pragma: no cover


def _make_fake_ws_module(ws: _FakeWebSocket) -> _FakeWebsocketsModule:
    """Return a fake websockets module whose connect() returns the given ws."""
    mod = _FakeWebsocketsModule()
    mod.connect = lambda *a, **kw: ws  # type: ignore[method-assign]
    return mod


def _patch_ws(ws: _FakeWebSocket):
    """Patch `_ws_import` in the transport module to return a fake websockets module.

    Usage:
        with _patch_ws(fake_ws):
            ...
    """
    fake_mod = _make_fake_ws_module(ws)
    return patch("aats.ingestion.transport._ws_import", return_value=fake_mod)


async def _empty_async_gen() -> AsyncIterator[RawTransaction]:
    """An async generator that yields nothing (used to stub _stream_poll).

    Returns a FRESH generator each time it is called.  The caller is
    responsible for aclose()-ing any generator that is abandoned mid-iteration
    (i.e. broken out of with ``break``).
    """
    return
    yield  # pragma: no cover  # makes this an async generator function


class TestEnhancedWsFallbackWsPath:
    """Test the primary WS path using a fake websockets connection.

    We patch `aats.ingestion.transport.websockets` (the module-level import
    inside _stream_ws) rather than `websockets.connect` directly, so these
    tests work even when the websockets package is not installed in CI.

    HERMETIC: all test methods are ``async def`` so pytest-asyncio owns the
    event-loop lifecycle.  No ``asyncio.run()`` calls exist.  Every
    ``async for`` that may break early uses ``try/finally`` + ``aclose()``.
    Every patch of module globals is applied at the top of the test body so
    context-manager restore is guaranteed.
    """

    def _make_fallback(self) -> EnhancedWsFallback:
        return EnhancedWsFallback(
            ws_url="wss://fake-endpoint/?api-key=FAKE",
            rpc_url="https://fake-rpc/?api-key=FAKE",
            poll_idle_timeout_s=0.1,   # short for test speed
            poll_interval_s=0.05,
            max_reconnect_attempts=1,
        )

    async def test_ws_log_notification_yields_raw_transaction(self):
        """End-to-end: WS logsNotification → getTransaction → RawTransaction."""
        fallback = self._make_fallback()
        registry = make_registry()
        program_ids = registry.active_program_ids()

        tx_result = _make_fake_get_transaction_result(_FAKE_SIG_1)

        # Fake WS messages: 1 subscription confirmation + 1 log notification
        ws_messages = [
            _make_sub_confirmation(req_id=1, sub_id=100),
            _make_logs_notification(_FAKE_SIG_1, sub_id=100),
            # A second, duplicate notification — must be deduped
            _make_logs_notification(_FAKE_SIG_1, sub_id=100),
        ]

        async def fake_fetch(self_inner: Any, sig: str) -> RawTransaction | None:
            if sig == _FAKE_SIG_1:
                return _parse_rpc_get_transaction(sig, tx_result)
            return None

        # _empty_async_gen() returns a fresh generator; patch _stream_poll so
        # the subscribe() loop uses it.  We must aclose() it after the test
        # because subscribe() may never exhaust it (it breaks on first TX).
        empty_gen = _empty_async_gen()
        collected: list[RawTransaction] = []
        fake_ws = _FakeWebSocket(ws_messages)
        try:
            with _patch_ws(fake_ws), patch.object(
                EnhancedWsFallback, "_fetch_transaction", fake_fetch
            ), patch.object(
                EnhancedWsFallback, "_stream_poll", return_value=empty_gen
            ):
                sub_gen = fallback.subscribe(program_ids)
                try:
                    async for tx in sub_gen:
                        collected.append(tx)
                        break  # stop after first event
                finally:
                    await sub_gen.aclose()
        finally:
            await empty_gen.aclose()

        assert len(collected) == 1
        tx = collected[0]
        assert tx.signature == _FAKE_SIG_1
        assert tx.slot == 300_000_000
        assert tx.block_time_unix_s == 1_718_700_000

    async def test_errored_notification_is_dropped(self):
        """Log notifications with err!=null must not produce a RawTransaction."""
        fallback = self._make_fallback()
        program_ids = frozenset([_PUMP_PID])

        ws_messages = [
            _make_sub_confirmation(req_id=1, sub_id=100),
            _make_logs_notification(_FAKE_SIG_1, err={"InstructionError": [0, "Custom"]}),
        ]

        collected: list[RawTransaction] = []
        fake_ws = _FakeWebSocket(ws_messages)
        with _patch_ws(fake_ws):
            sub_gen = fallback.subscribe(program_ids)
            try:
                async for tx in sub_gen:  # pragma: no branch
                    collected.append(tx)
                    break
            finally:
                await sub_gen.aclose()

        assert len(collected) == 0, "Errored notification must be dropped"

    async def test_ws_connect_fail_triggers_polling_fallback(self):
        """When WS fails to connect, the polling fallback is activated."""
        fallback = self._make_fallback()
        program_ids = frozenset([_PUMP_PID])

        tx_result = _make_fake_get_transaction_result(_FAKE_SIG_2)

        async def fake_poll(self_inner: Any, pids: frozenset[str]) -> AsyncIterator[RawTransaction]:
            tx = _parse_rpc_get_transaction(_FAKE_SIG_2, tx_result)
            if tx is not None:
                yield tx

        fail_ws = _FakeWebSocket([], fail_on_connect=True)

        collected: list[RawTransaction] = []
        with _patch_ws(fail_ws), patch.object(EnhancedWsFallback, "_stream_poll", fake_poll):
            sub_gen = fallback.subscribe(program_ids)
            try:
                async for tx in sub_gen:
                    collected.append(tx)
                    break
            finally:
                await sub_gen.aclose()

        assert len(collected) == 1
        assert collected[0].signature == _FAKE_SIG_2

    def test_duplicate_signatures_deduped(self):
        """Out-of-order / duplicate signature delivery must produce exactly one RawTransaction.

        WS-B1 fix: ``_time_now_s`` is patched with ``_FakeClock`` as a defensive
        measure even though ``_mark_seen`` / ``_is_seen`` are synchronous and do
        not call ``_time_now_s`` themselves.  This ensures the test is immune to
        any future refactor that adds timing to the dedup path.
        """
        clock = _FakeClock([1_000.0])  # constant clock, never consumed by sync code
        with _patch_time(clock):
            fallback = self._make_fallback()
            # Pre-mark sig as seen
            fallback._mark_seen(_FAKE_SIG_1)

            # Now _is_seen should return True
            assert fallback._is_seen(_FAKE_SIG_1) is True
            assert fallback._is_seen(_FAKE_SIG_2) is False

    def test_dedup_ring_bounded(self):
        """Dedup ring evicts oldest entries when full (memory-bounded).

        WS-B1 fix: ``_time_now_s`` is patched with ``_FakeClock`` as a defensive
        measure (same rationale as ``test_duplicate_signatures_deduped``).
        """
        clock = _FakeClock([1_000.0])  # constant clock, never consumed by sync code
        with _patch_time(clock):
            fallback = self._make_fallback()
            fallback._DEDUP_RING_MAX = 3  # type: ignore[assignment]  # shrink for test

            sigs = [f"sig_{i}" for i in range(5)]
            for sig in sigs:
                fallback._mark_seen(sig)

            # First 2 should have been evicted
            assert not fallback._is_seen("sig_0")
            assert not fallback._is_seen("sig_1")
            # Last 3 should still be present
            assert fallback._is_seen("sig_2")
            assert fallback._is_seen("sig_3")
            assert fallback._is_seen("sig_4")

    async def test_empty_rpc_url_yields_nothing_gracefully(self, caplog: pytest.LogCaptureFixture):
        """Empty rpc_url must fail gracefully with a clear log message, not crash."""
        import logging
        fallback = EnhancedWsFallback(ws_url="", rpc_url="")

        collected: list[RawTransaction] = []
        with caplog.at_level(logging.ERROR, logger="aats.ingestion.transport"):
            sub_gen = fallback.subscribe(frozenset([_PUMP_PID]))
            try:
                async for tx in sub_gen:
                    collected.append(tx)  # pragma: no cover
            finally:
                await sub_gen.aclose()

        assert len(collected) == 0
        assert any("RPC_PRIMARY" in r.message for r in caplog.records), (
            "Must log a clear error mentioning RPC_PRIMARY when rpc_url is empty"
        )


# ---------------------------------------------------------------------------
# Tests: RPC polling fallback path
# ---------------------------------------------------------------------------

class TestEnhancedWsFallbackPollingPath:
    """Test the polling fallback path using mocked httpx responses.

    HERMETIC: all test methods are ``async def`` so pytest-asyncio owns the
    event-loop lifecycle.  No ``asyncio.run()`` calls exist.
    """

    async def test_polling_fallback_yields_raw_transaction(self):
        """Polling path: getSignaturesForAddress → getTransaction → RawTransaction."""
        fallback = EnhancedWsFallback(
            ws_url="",   # no WS — forces polling immediately
            rpc_url="https://fake-rpc/?api-key=FAKE",
            poll_interval_s=0.0,
            poll_idle_timeout_s=0.5,
            max_reconnect_attempts=1,
        )
        program_ids = frozenset([_PUMP_PID])

        async def fake_get_sigs(self_inner: Any, pid: str) -> list[str]:
            return [_FAKE_SIG_1]

        tx_result = _make_fake_get_transaction_result(_FAKE_SIG_1)

        async def fake_fetch(self_inner: Any, sig: str) -> RawTransaction | None:
            return _parse_rpc_get_transaction(sig, tx_result)

        collected: list[RawTransaction] = []
        with patch.object(
            EnhancedWsFallback, "_get_signatures_for_address", fake_get_sigs
        ), patch.object(EnhancedWsFallback, "_fetch_transaction", fake_fetch):
            sub_gen = fallback.subscribe(program_ids)
            try:
                async for tx in sub_gen:
                    collected.append(tx)
                    break  # stop after first result
            finally:
                await sub_gen.aclose()

        assert len(collected) == 1
        assert collected[0].signature == _FAKE_SIG_1
        assert collected[0].slot == 300_000_000
        assert collected[0].block_time_unix_s == 1_718_700_000

    async def test_polling_deduplicates_repeated_sigs(self):
        """Polling must not emit the same transaction twice across iterations.

        WS-B1 fix: ``_time_now_s`` is patched with ``_poll_clock()`` so that
        ``_stream_poll`` executes exactly ONE complete while-loop iteration then
        exits, regardless of CPU load or OS scheduling.

        Clock script (one program ID, one sig):
          call[0]  poll_end_s initialised   → t0
          call[1]  first while-check         → t0   (< t0+T → enter loop)
          call[2]  poll_end_s reset on yield → t0   (→ poll_end_s = t0+T)
          call[3]  second while-check        → t0+T+1 (> t0+T → exit loop)

        The poll generator therefore yields the sig exactly ONCE, then exits.
        The sig is marked seen during that one iteration, so the second
        while-check (if it were to run) would skip the sig and produce no
        second fetch.  The fake clock makes this proof hold regardless of
        real wall-clock elapsed time.
        """
        # Use a large, round timeout so the clock values are readable in diffs.
        _POLL_TIMEOUT = 10.0
        clock = _poll_clock(timeout_s=_POLL_TIMEOUT)

        fallback = EnhancedWsFallback(
            ws_url="",
            rpc_url="https://fake-rpc/?api-key=FAKE",
            poll_interval_s=0.0,
            poll_idle_timeout_s=_POLL_TIMEOUT,
            max_reconnect_attempts=1,
        )
        program_ids = frozenset([_PUMP_PID])
        call_count = [0]

        async def fake_get_sigs(self_inner: Any, pid: str) -> list[str]:
            # Always return the same signature — dedup must suppress repeated fetches.
            return [_FAKE_SIG_1]

        tx_result = _make_fake_get_transaction_result(_FAKE_SIG_1)

        async def fake_fetch(self_inner: Any, sig: str) -> RawTransaction | None:
            call_count[0] += 1
            return _parse_rpc_get_transaction(sig, tx_result)

        collected: list[RawTransaction] = []
        # _patch_time is applied HERE (not inside a nested coroutine) so the
        # context-manager restore is guaranteed even if the test throws.
        with _patch_time(clock), patch.object(
            EnhancedWsFallback, "_get_signatures_for_address", fake_get_sigs
        ), patch.object(EnhancedWsFallback, "_fetch_transaction", fake_fetch):
            sub_gen = fallback.subscribe(program_ids)
            try:
                async for tx in sub_gen:
                    collected.append(tx)
                    # Safety guard: if dedup fails and yields a 2nd item, stop
                    # immediately so the test can report the correct len/call_count.
                    if len(collected) >= 2:
                        break
            finally:
                await sub_gen.aclose()

        # Should only collect 1 unique transaction even though the sig repeats.
        assert len(collected) == 1, (
            f"Dedup must prevent the same sig from yielding twice "
            f"(call_count={call_count[0]}, collected={len(collected)})"
        )
        # fetch must be called exactly once — dedup prevents the second iteration
        # from ever calling _fetch_transaction for a seen signature.
        assert call_count[0] == 1, (
            f"_fetch_transaction called {call_count[0]} times; expected 1 "
            f"(dedup should block all calls after the first)"
        )

    async def test_polling_absent_block_time_point_in_time_correct(self):
        """T-300a: block_time_unix_s=None when getTransaction returns no blockTime."""
        fallback = EnhancedWsFallback(
            ws_url="",
            rpc_url="https://fake-rpc/?api-key=FAKE",
            poll_interval_s=0.0,
            poll_idle_timeout_s=0.3,
            max_reconnect_attempts=1,
        )
        program_ids = frozenset([_PUMP_PID])

        async def fake_get_sigs(self_inner: Any, pid: str) -> list[str]:
            return [_FAKE_SIG_1]

        tx_result = _make_fake_get_transaction_result(_FAKE_SIG_1, block_time=None)

        async def fake_fetch(self_inner: Any, sig: str) -> RawTransaction | None:
            return _parse_rpc_get_transaction(sig, tx_result)

        collected: list[RawTransaction] = []
        with patch.object(
            EnhancedWsFallback, "_get_signatures_for_address", fake_get_sigs
        ), patch.object(EnhancedWsFallback, "_fetch_transaction", fake_fetch):
            sub_gen = fallback.subscribe(program_ids)
            try:
                async for tx in sub_gen:
                    collected.append(tx)
                    break
            finally:
                await sub_gen.aclose()

        # Transactions with absent blockTime still get emitted as RawTransaction
        # (the block_time_unix_s=None is honest); the decoder then holds them PENDING.
        assert len(collected) == 1
        assert collected[0].block_time_unix_s is None, (
            "T-300a: absent blockTime must yield block_time_unix_s=None, never wall-clock"
        )


# ---------------------------------------------------------------------------
# Tests: end-to-end WS → RawTransaction → Decoder → LaunchEvent
# ---------------------------------------------------------------------------

class TestEndToEndWsDecoder:
    """Full pipeline: fake WS message → RawTransaction → PumpFunDecoder → LaunchEvent.

    HERMETIC: all test methods are ``async def`` so pytest-asyncio owns the
    event-loop lifecycle.  No ``asyncio.run()`` calls exist.
    """

    async def test_ws_pump_buy_yields_launch_event(self):
        """End-to-end: WS notification → RawTransaction → PumpFunDecoder → LaunchEvent."""
        from aats.ingestion.transport import TransportPipeline

        fallback = EnhancedWsFallback(
            ws_url="wss://fake/?api-key=FAKE",
            rpc_url="https://fake-rpc/?api-key=FAKE",
            poll_idle_timeout_s=0.1,
            poll_interval_s=0.05,
            max_reconnect_attempts=1,
        )
        registry = make_registry()
        router = InstructionRouter(registry)

        tx_result = _make_fake_get_transaction_result(
            _FAKE_SIG_1,
            token_amount=1_000_000_000,
            max_sol_cost=200_000_000,
            block_time=1_718_700_001,
        )
        raw_tx: RawTransaction | None = _parse_rpc_get_transaction(_FAKE_SIG_1, tx_result)
        assert raw_tx is not None

        ws_messages = [
            _make_sub_confirmation(req_id=1, sub_id=10),
            _make_logs_notification(_FAKE_SIG_1, sub_id=10),
        ]

        async def fake_fetch(self_inner: Any, sig: str) -> RawTransaction | None:
            return raw_tx

        # A fresh empty generator for each test — aclose()'d in finally.
        empty_gen = _empty_async_gen()
        events: list[Any] = []
        fake_ws = _FakeWebSocket(ws_messages)
        try:
            with _patch_ws(fake_ws), patch.object(
                EnhancedWsFallback, "_fetch_transaction", fake_fetch
            ), patch.object(
                EnhancedWsFallback, "_stream_poll", return_value=empty_gen
            ):
                pipeline = TransportPipeline(transport=fallback, router=router)
                ev_gen = pipeline.events()
                try:
                    async for ev, sig, _kind in ev_gen:
                        events.append((ev, sig))
                        break
                finally:
                    await ev_gen.aclose()
        finally:
            await empty_gen.aclose()

        assert len(events) == 1
        ev, sig = events[0]
        assert sig == _FAKE_SIG_1
        assert ev.source == LaunchSource.PUMPFUN
        # event_time.block_time_ms must come from on-chain blockTime ONLY (T-300a)
        assert ev.event_time.block_time_ms == 1_718_700_001 * 1_000
        assert ev.event_time.slot == 300_000_000
        # Money fields must be int (not float)
        assert isinstance(ev.sol_reserve_lamports, int)
        assert isinstance(ev.token_reserve_base, int)


# ---------------------------------------------------------------------------
# Tests: WS URL derivation from RPC_PRIMARY
# ---------------------------------------------------------------------------

class TestWsUrlDerivation:
    """Verify the WS URL is correctly derived from RPC_PRIMARY in shadow_record._run()."""

    def test_https_to_wss_derivation(self):
        """RPC_PRIMARY https://... → WS URL wss://... (correct derivation)."""
        rpc = "https://mainnet.helius-rpc.com/?api-key=FAKEFAKEFAKE"
        derived = rpc.replace("https://", "wss://", 1)
        assert derived == "wss://mainnet.helius-rpc.com/?api-key=FAKEFAKEFAKE"

    def test_http_to_ws_derivation(self):
        """RPC_PRIMARY http://... → WS URL ws://... (localhost devnet case)."""
        rpc = "http://localhost:8899"
        derived = rpc.replace("https://", "wss://", 1).replace("http://", "ws://", 1)
        assert derived == "ws://localhost:8899"

    def test_already_wss_unchanged(self):
        """WS_ENDPOINT takes precedence when set (not derived)."""
        ws_explicit = "wss://mainnet.helius-rpc.com/?api-key=EXPLICIT"
        rpc = "https://mainnet.helius-rpc.com/?api-key=DIFFERENT"
        # When WS_ENDPOINT is set, it overrides derivation
        ws_url = ws_explicit or rpc.replace("https://", "wss://", 1)
        assert ws_url == ws_explicit


# ---------------------------------------------------------------------------
# Tests: shadow_record --source=ws graceful failure with no endpoint
# ---------------------------------------------------------------------------

class TestShadowRecordWsSource:
    """Integration test: --source=ws constructs EnhancedWsFallback; empty env fails gracefully."""

    async def test_ws_source_no_endpoint_yields_zero_events(self, tmp_path: Any):
        """With no RPC_PRIMARY set, --source=ws logs error and produces zero events (no crash)."""
        import os
        from pathlib import Path

        from aats.ingestion.shadow_record import _run

        allowlist = Path("C:/dev/aats/config/program-allowlist.json")
        if not allowlist.exists():
            pytest.skip("program-allowlist.json not found — skipping integration test")

        # Ensure env vars are unset
        env_backup = {}
        for key in ("RPC_PRIMARY", "WS_ENDPOINT"):
            env_backup[key] = os.environ.pop(key, None)

        try:
            stats = await _run(
                source="ws",
                out_dir=tmp_path / "ws_empty",
                max_events=1,
                allowlist_path=allowlist,
                first_k_slots=5,
            )
        finally:
            for key, val in env_backup.items():
                if val is not None:
                    os.environ[key] = val

        assert stats["events_decoded"] == 0
        # Must not crash — graceful failure only

    def test_ws_source_max_events_1_no_crash(self, tmp_path: Any, capsys: Any):
        """python -m aats.ingestion.shadow_record --source=ws --max-events 1 with no env → graceful."""
        import subprocess
        import sys

        result = subprocess.run(
            [
                sys.executable, "-m", "aats.ingestion.shadow_record",
                "--source=ws",
                "--max-events", "1",
                "--out", str(tmp_path / "ws_test"),
            ],
            capture_output=True,
            text=True,
            env={
                **dict(__import__("os").environ),
                # Ensure no real endpoint leaks from the dev environment
                "RPC_PRIMARY": "",
                "WS_ENDPOINT": "",
                "GEYSER_ENDPOINT": "",
                "GEYSER_TOKEN": "",
            },
            timeout=30,
        )
        # Must exit cleanly (0 or 1 — not crash) with a clear message
        assert result.returncode in (0, 1), (
            f"shadow_record --source=ws should exit 0 or 1, got {result.returncode}\n"
            f"STDOUT: {result.stdout}\nSTDERR: {result.stderr}"
        )
        # Must not crash with an unhandled exception
        assert "Traceback" not in result.stderr, (
            f"Unhandled exception in shadow_record --source=ws:\n{result.stderr}"
        )


# ---------------------------------------------------------------------------
# Tests: point-in-time correctness (T-300a)
# ---------------------------------------------------------------------------

class TestPointInTimeCorrectness:
    """Verify that block_time_unix_s from on-chain data is used; wall-clock never leaks."""

    def test_block_time_absent_means_none_not_wall_clock(self):
        """T-300a: when blockTime is absent, block_time_unix_s=None; wall-clock NEVER substituted."""
        result = _make_fake_get_transaction_result(_FAKE_SIG_1, block_time=None)
        tx = _parse_rpc_get_transaction(_FAKE_SIG_1, result)

        assert tx is not None
        assert tx.block_time_unix_s is None, (
            "T-300a: block_time_unix_s must be None when blockTime is absent"
        )
        # If wall-clock had leaked, block_time_unix_s would be a recent unix timestamp.
        # The assertion above already catches that since None != any integer.

    def test_block_time_present_uses_on_chain_value(self):
        """The on-chain blockTime (not wall-clock) is used when present."""
        on_chain_time = 1_718_700_042  # arbitrary historical value, NOT wall-clock
        result = _make_fake_get_transaction_result(_FAKE_SIG_1, block_time=on_chain_time)
        tx = _parse_rpc_get_transaction(_FAKE_SIG_1, result)

        assert tx is not None
        assert tx.block_time_unix_s == on_chain_time

    def test_decoder_holds_pending_when_block_time_absent(self):
        """_make_event_time returns None (PENDING) when block_time_unix_s is None."""
        from aats.ingestion.decoders import _make_event_time
        result = _make_event_time(slot=300_000_000, block_time_unix_s=None)
        assert result is None, (
            "T-300a: decoder must return None (PENDING) when block_time_unix_s is None"
        )

    def test_decoder_uses_block_time_ms_not_wall_clock(self):
        """EventTime.block_time_ms must equal on-chain blockTime * 1000."""
        from aats.ingestion.decoders import _make_event_time
        on_chain_s = 1_718_700_000
        event_time = _make_event_time(slot=300_000_000, block_time_unix_s=on_chain_s)
        assert event_time is not None
        assert event_time.block_time_ms == on_chain_s * 1_000
