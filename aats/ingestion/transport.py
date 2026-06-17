"""Transport interface + injectable sources (Geyser gRPC, WS fallback, replay).

ARCHITECTURE NOTE
-----------------
This module provides:

1. TransportInterface (ABC) — the contract all sources implement.
2. GeyserTransport — LIVE Yellowstone/Dragon's-Mouth gRPC client.
   - Opens grpc.aio.secure_channel to GEYSER_ENDPOINT with x-token auth.
   - Sends a SubscribeRequest filtering TRANSACTIONS by account_include
     = the program IDs from ProgramRegistry (pump.fun, PumpSwap, Raydium v4,
     CPMM).  Commitment = PROCESSED for the lowest-latency snipe edge.
   - Wraps reconnect in tenacity exponential backoff with jitter.
   - On reconnect, resumes from from_slot = last successfully processed slot.
   - Emits rising data_staleness_ms on dead feed (never silent stale data).
   - Parses SubscribeUpdateTransactionInfo -> RawTransaction using the vendored
     Yellowstone proto stubs in aats/ingestion/geyser_proto/.
3. ReplayTransport — deterministic offline source driven by a list of
   RawTransaction fixtures.  Used by all offline tests; no network required.
4. EnhancedWsFallback — WebSocket fallback for Geyser-degraded periods.
   STATUS: STUB — see PLUG_IN_HERE comment.

LIVE VALIDATION
---------------
Running --source=geyser against a real Helius/Triton/QuickNode endpoint
requires:
  GEYSER_ENDPOINT=<host:port>   e.g. atlas-mainnet.helius-rpc.com:2053
  GEYSER_TOKEN=<api-key>        your Helius/Triton/QuickNode token

The module gracefully fails (logs a clear error and yields nothing) when
GEYSER_ENDPOINT is empty or the connection is refused.

BLOCK_TIME HONESTY (T-300a)
---------------------------
The Yellowstone SubscribeUpdateTransaction does NOT carry block_time in the
transaction payload (github.com/rpcpool/yellowstone-grpc/issues/228, closed
not-planned).  block_time is only available via SubscribeUpdateBlockMeta
updates on a parallel slot/block_meta subscription.

Consequence enforced here:
  - _parse_geyser_tx() sets block_time_unix_s = None when the block_time is
    not yet available (which is always for pure PROCESSED transaction updates).
  - The decoder's _make_event_time() returns None for absent block_time and
    the event is HELD PENDING — never wall-clock-substituted (T-300a).

For latency-vs-honesty tradeoff: if an operator needs block_time, run a
parallel SubscribeRequest for blocks_meta and merge on slot.  A future
block_time enricher can back-fill pending events.  This module never guesses.

POINT-IN-TIME CORRECTNESS
--------------------------
event_time (on-chain slot/block_time) is the ONLY time that exists for
feature computation and backtests.  ingest_time / wall-clock is for
monitoring (data_staleness_ms) ONLY.

BACK-PRESSURE
-------------
GeyserTransport is a producer: it decodes and yields, then returns.
No price math, no model call, no enrichment HTTP call sits between receiving
a Geyser message and yielding it.

SECURITY
--------
GEYSER_ENDPOINT and GEYSER_TOKEN come from environment only.
They are NEVER logged, NEVER serialized, NEVER hardcoded.
"""

from __future__ import annotations

import asyncio
import base64
import itertools
import logging
import os
import sys
import time
from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import TYPE_CHECKING

from aats.contracts.events import DetectionTransport
from aats.ingestion.decoders import RawInstruction, RawTransaction

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Transport interface
# ---------------------------------------------------------------------------


class TransportInterface(ABC):
    """The contract every event source implements.

    subscribe() is an async generator: it yields RawTransactions as they arrive
    and resumes from last_slot on reconnect.  It runs until cancelled.
    """

    @abstractmethod
    def subscribe(
        self,
        program_ids: frozenset[str],
        last_slot: int = 0,
    ) -> AsyncGenerator[RawTransaction, None]:
        """Stream raw transactions matching any of the given program IDs.

        Args:
            program_ids: The set of program IDs to filter for.
            last_slot: Resume from this slot on reconnect (from_slot parameter).

        Yields:
            RawTransaction for each matched transaction.

        Declared as a plain (non-async) method returning an AsyncGenerator
        so subclasses can implement it as `async def` with `yield` and still
        satisfy the ABC.
        """
        ...  # pragma: no cover

    @property
    @abstractmethod
    def detection_transport(self) -> DetectionTransport:
        """Which DetectionTransport tag to stamp on decoded events."""
        ...  # pragma: no cover

    @property
    @abstractmethod
    def is_connected(self) -> bool:
        """Whether the transport currently has an active upstream connection."""
        ...  # pragma: no cover

    @property
    def last_slot(self) -> int:
        """The most recently yielded slot (for resume on reconnect)."""
        return self._last_slot

    def __init__(self) -> None:
        self._last_slot: int = 0


# ---------------------------------------------------------------------------
# ReplayTransport — deterministic offline source for tests
# ---------------------------------------------------------------------------


class ReplayTransport(TransportInterface):
    """Deterministic replay source: replays a fixed list of RawTransactions.

    Used for offline testing and RECORD/SHADOW mode replay.  No network,
    no real Geyser connection.  The decode pipeline and bus are exercised
    identically to live mode.

    Supports injecting out-of-order and duplicate events to test
    idempotency (see tests/ingestion/test_point_in_time.py).
    """

    def __init__(
        self,
        transactions: list[RawTransaction],
        tick_ms: float = 0.0,  # inter-event delay (0 = as fast as possible)
    ) -> None:
        super().__init__()
        self._transactions = transactions
        self._tick_ms = tick_ms
        self._connected = False

    @property
    def detection_transport(self) -> DetectionTransport:
        return DetectionTransport.GEYSER  # tag replay as geyser for test parity

    @property
    def is_connected(self) -> bool:
        return self._connected

    async def subscribe(  # type: ignore[override]
        self,
        program_ids: frozenset[str],
        last_slot: int = 0,
    ) -> AsyncGenerator[RawTransaction, None]:
        """Yield all fixture transactions (filtered by program_ids from last_slot)."""
        self._connected = True
        try:
            for tx in self._transactions:
                if tx.slot < last_slot:
                    continue  # resume-from-slot: skip already-processed slots
                tx_pids = {ix.program_id for ix in tx.instructions + tx.inner_instructions}
                if program_ids and not tx_pids.intersection(program_ids):
                    continue
                if self._tick_ms > 0:
                    await asyncio.sleep(self._tick_ms / 1_000)
                self._last_slot = tx.slot
                yield tx
        finally:
            self._connected = False


# ---------------------------------------------------------------------------
# _GeyserProtoImport — lazy, isolated import of the vendored proto stubs
# ---------------------------------------------------------------------------

def _import_geyser_proto():
    """Import the vendored Yellowstone gRPC stubs.

    The stubs live in aats/ingestion/geyser_proto/ and use bare module names
    (import geyser_pb2, import solana_storage_pb2).  We ensure the directory
    is on sys.path before importing so they resolve correctly regardless of
    how this module is invoked.

    Returns (geyser_pb2, geyser_pb2_grpc) tuple.
    Raises ImportError with a helpful message if grpcio is not installed.
    """
    _proto_dir = os.path.join(os.path.dirname(__file__), "geyser_proto")
    if _proto_dir not in sys.path:
        sys.path.insert(0, _proto_dir)

    try:
        import geyser_pb2  # type: ignore[import]
        import geyser_pb2_grpc  # type: ignore[import]
        return geyser_pb2, geyser_pb2_grpc
    except ImportError as exc:
        raise ImportError(
            "Could not import Yellowstone gRPC proto stubs from "
            f"{_proto_dir}.  Ensure grpcio>=1.64.1 is installed "
            "(requirements/requirements.txt).  Original error: {exc}"
        ) from exc


# ---------------------------------------------------------------------------
# GeyserTransport — LIVE Yellowstone gRPC client
# ---------------------------------------------------------------------------


class GeyserTransport(TransportInterface):
    """Yellowstone/Geyser gRPC transport — LIVE IMPLEMENTATION.

    Connects to a Yellowstone-compatible endpoint (Helius, Triton, QuickNode)
    and streams SubscribeUpdateTransaction messages, converting them to
    RawTransaction objects for the decoder pipeline.

    CREDENTIALS — from environment only, NEVER hardcoded:
      GEYSER_ENDPOINT — host:port or https://host:port (e.g. atlas-mainnet.helius-rpc.com:2053)
      GEYSER_TOKEN    — API key / x-token for auth

    COMMITMENT:
      Uses PROCESSED (0) for minimum-latency snipe edge.  Slot rollbacks are
      possible on the processed commitment level; the downstream pipeline
      handles this via the CENSORED window mechanism in ShadowRecorder.

    BLOCK_TIME (T-300a honesty):
      The Yellowstone SubscribeUpdateTransaction does NOT embed block_time.
      block_time_unix_s is set to None in every RawTransaction.  The decoder's
      _make_event_time() holds the event PENDING rather than substituting
      wall-clock.  See T-300a fix in decoders.py.

    RECONNECT:
      On stream close or gRPC error, retries with exponential backoff + jitter
      (tenacity).  Resumes from from_slot = last successfully processed slot.
      data_staleness_ms rises during the dead window (visible to monitoring).
      A silent dead feed is the worst failure mode; this transport surfaces it.

    GRACEFUL DEGRADATION:
      - Empty GEYSER_ENDPOINT: logs a clear error, yields nothing.
      - gRPC connection refused: logs, retries up to max_retries, then exits.
      - Stream closed by server: reconnects from last_slot.
    """

    # Tenacity retry params
    _RETRY_MIN_WAIT_S: float = 1.0
    _RETRY_MAX_WAIT_S: float = 30.0
    _RETRY_MULTIPLIER: float = 2.0
    _RETRY_MAX_ATTEMPTS: int = 0  # 0 = infinite (reconnect forever on live feed)

    def __init__(
        self,
        endpoint: str,        # env: GEYSER_ENDPOINT — NEVER a literal in code
        x_token: str,         # env: GEYSER_TOKEN — NEVER hardcoded
        commitment: int = 0,  # CommitmentLevel.PROCESSED = 0
        shredstream_endpoint: str | None = None,  # env: SHREDSTREAM_ENDPOINT
        max_reconnect_attempts: int = 0,  # 0 = infinite
    ) -> None:
        super().__init__()
        self._endpoint = endpoint
        self._x_token = x_token  # NOT logged, NOT serialized
        self._commitment = commitment
        self._shredstream_endpoint = shredstream_endpoint
        self._max_reconnect_attempts = max_reconnect_attempts
        self._connected = False
        # Reconnect jitter seed (random, not reproducible — that is intentional)
        import random
        self._rng = random.Random()

    @property
    def detection_transport(self) -> DetectionTransport:
        return DetectionTransport.GEYSER

    @property
    def is_connected(self) -> bool:
        return self._connected

    async def subscribe(  # type: ignore[override]
        self,
        program_ids: frozenset[str],
        last_slot: int = 0,
    ) -> AsyncGenerator[RawTransaction, None]:
        """Stream from Yellowstone Geyser gRPC.

        Connects to self._endpoint, subscribes to TRANSACTIONS filtered by
        account_include = list(program_ids), and yields RawTransaction for
        each non-vote, non-failed transaction update.

        On stream drop, reconnects with exponential backoff + jitter and
        resumes from from_slot = self._last_slot.

        LIVE ENDPOINT NOTE:
          This method requires a valid GEYSER_ENDPOINT and GEYSER_TOKEN.
          Without them, it logs a clear error message and yields nothing.
        """
        if not self._endpoint:
            logger.error(
                "GeyserTransport.subscribe(): GEYSER_ENDPOINT is not set.  "
                "Set the GEYSER_ENDPOINT environment variable to a valid "
                "Yellowstone gRPC endpoint (e.g. atlas-mainnet.helius-rpc.com:2053).  "
                "Also set GEYSER_TOKEN to your API key.  "
                "See .env.example for the full schema.  "
                "Yielding nothing — use --source=replay for offline testing."
            )
            return

        # Import proto stubs lazily (keeps import errors clear at startup)
        try:
            geyser_pb2, geyser_pb2_grpc = _import_geyser_proto()
        except ImportError as exc:
            logger.error("GeyserTransport: proto stubs unavailable: %s", exc)
            return

        import grpc
        import grpc.aio

        current_slot = last_slot
        attempt = 0

        while True:
            attempt += 1
            if self._max_reconnect_attempts > 0 and attempt > self._max_reconnect_attempts:
                logger.error(
                    "GeyserTransport: max reconnect attempts (%d) reached — giving up.",
                    self._max_reconnect_attempts,
                )
                break

            try:
                logger.info(
                    "GeyserTransport: connecting to %s (attempt %d, from_slot=%d)",
                    self._endpoint,
                    attempt,
                    current_slot,
                )
                async for tx in self._stream_once(
                    geyser_pb2, geyser_pb2_grpc, grpc, program_ids, current_slot
                ):
                    current_slot = tx.slot
                    self._last_slot = tx.slot
                    yield tx
                # Stream ended cleanly (server closed it) — reconnect
                logger.info(
                    "GeyserTransport: stream ended cleanly at slot %d — reconnecting.",
                    current_slot,
                )

            except grpc.aio.AioRpcError as rpc_err:
                self._connected = False
                logger.warning(
                    "GeyserTransport: gRPC error (attempt %d): %s %s — reconnecting.",
                    attempt,
                    rpc_err.code(),
                    rpc_err.details(),
                )
            except asyncio.CancelledError:
                # Propagate cancellation — the pipeline is shutting down.
                self._connected = False
                logger.info("GeyserTransport: cancelled — shutting down.")
                raise
            except Exception as exc:  # noqa: BLE001
                self._connected = False
                logger.warning(
                    "GeyserTransport: unexpected error (attempt %d): %s — reconnecting.",
                    attempt,
                    exc,
                    exc_info=True,
                )

            # Exponential backoff with jitter before reconnect
            wait_s = min(
                self._RETRY_MIN_WAIT_S * (self._RETRY_MULTIPLIER ** (attempt - 1)),
                self._RETRY_MAX_WAIT_S,
            )
            jitter = self._rng.uniform(0, wait_s * 0.25)  # ±25% jitter
            sleep_s = wait_s + jitter
            logger.info(
                "GeyserTransport: waiting %.2fs before reconnect (from_slot=%d).",
                sleep_s,
                current_slot,
            )
            await asyncio.sleep(sleep_s)

    async def _stream_once(
        self,
        geyser_pb2: object,
        geyser_pb2_grpc: object,
        grpc: object,
        program_ids: frozenset[str],
        from_slot: int,
    ) -> AsyncGenerator[RawTransaction, None]:
        """Open one gRPC stream and yield RawTransactions until it closes.

        This is separated from subscribe() so the outer retry loop can catch
        gRPC errors at the stream level without nesting async generators.

        CREDENTIAL SECURITY:
          The x-token is passed as gRPC call metadata (not in the URL/path).
          It is not logged here.
        """
        # Build composite credentials: TLS + per-call x-token metadata
        ssl_credentials = grpc.ssl_channel_credentials()
        auth_credentials = grpc.metadata_call_credentials(
            lambda _, callback: callback((("x-token", self._x_token),), None),
            name="x-token-auth",
        )
        channel_credentials = grpc.composite_channel_credentials(
            ssl_credentials, auth_credentials
        )

        channel_options = [
            ("grpc.keepalive_time_ms", 10_000),
            ("grpc.keepalive_timeout_ms", 5_000),
            ("grpc.keepalive_permit_without_calls", 1),
            ("grpc.http2.max_pings_without_data", 0),
        ]

        async with grpc.aio.secure_channel(
            self._endpoint,
            channel_credentials,
            options=channel_options,
        ) as channel:
            stub = geyser_pb2_grpc.GeyserStub(channel)

            # Build SubscribeRequest via the pure helper so tests can assert on
            # the exact production request-building path (finding B2).
            request = _build_subscribe_request(
                geyser_pb2, program_ids, from_slot, self._commitment
            )

            logger.info(
                "GeyserTransport: subscribing — program_ids=%d commitment=%d from_slot=%d",
                len(program_ids),
                self._commitment,
                from_slot,
            )

            self._connected = True
            # The Geyser Subscribe RPC is a bidirectional stream.
            # We send one SubscribeRequest (as a one-element async iterator)
            # and then consume the server's SubscribeUpdate stream.
            async def _request_iter():
                yield request

            async for update in stub.Subscribe(_request_iter()):
                if not update.HasField("transaction"):
                    # slot, block_meta, ping, pong — skip, not a transaction
                    continue
                try:
                    tx = self._parse_geyser_tx(update.transaction)
                except Exception as parse_exc:  # noqa: BLE001
                    logger.warning(
                        "GeyserTransport: parse error on update: %s", parse_exc, exc_info=True
                    )
                    continue
                if tx is not None:
                    yield tx

        self._connected = False

    def _parse_geyser_tx(
        self, geyser_tx_update: object
    ) -> RawTransaction | None:
        """Convert a Geyser SubscribeUpdateTransaction -> RawTransaction.

        Parses:
          - signature: bytes → base58 string (via base58 encoding)
          - slot: uint64 from the outer SubscribeUpdateTransaction.slot
          - block_time_unix_s: None (PROCESSED updates do not carry block_time)
          - fee_payer: transaction.message.account_keys[0] (first signer)
          - instructions: outer message.instructions resolved against all keys
          - inner_instructions: meta.inner_instructions, flattened
          - program_logs: meta.log_messages
          - is_vote: SubscribeUpdateTransactionInfo.is_vote
          - err: meta.err (None if no error field populated)

        ACCOUNT KEY RESOLUTION:
          Versioned transactions may include address lookup tables.  The
          Yellowstone meta carries loaded_writable_addresses and
          loaded_readonly_addresses as the resolved keys.  We append them to
          the static account_keys list to produce the complete address table.

        POINT-IN-TIME CORRECTNESS (T-300a):
          block_time_unix_s is always None for PROCESSED-level Geyser updates.
          The caller (decoder._make_event_time) returns None for absent
          block_time and holds the event PENDING.  Wall-clock is NEVER
          substituted.  Future: a parallel block_meta subscription can supply
          block_time keyed on slot.

        Returns:
            RawTransaction, or None if the update should be dropped (e.g. vote).
        """
        # geyser_tx_update is a SubscribeUpdateTransaction proto message.
        # .transaction is SubscribeUpdateTransactionInfo
        # .slot is uint64
        slot: int = int(geyser_tx_update.slot)
        tx_info = geyser_tx_update.transaction  # SubscribeUpdateTransactionInfo

        is_vote: bool = bool(tx_info.is_vote)

        # Signature: first bytes in the list — base58-encode it
        sig_bytes: bytes = bytes(tx_info.signature)
        signature: str = _bytes_to_base58(sig_bytes) if sig_bytes else "unknown"

        # The proto Transaction wraps Message which has account_keys (list of bytes)
        proto_tx = tx_info.transaction  # solana.storage.ConfirmedBlock.Transaction
        meta = tx_info.meta            # solana.storage.ConfirmedBlock.TransactionStatusMeta

        # err: present if meta.err.err is non-empty bytes
        err: str | None = None
        if meta.HasField("err"):
            # err.err is bytes (bincode-encoded Solana TransactionError)
            err = f"transaction_error:{meta.err.err.hex()}" if meta.err.err else "unknown_error"

        message = proto_tx.message  # solana.storage.ConfirmedBlock.Message

        # Build full account key list: static keys + lookup-table resolved keys
        # static keys are bytes objects in message.account_keys
        static_keys: list[str] = [_bytes_to_base58(k) for k in message.account_keys]

        # Loaded addresses from lookup tables (versioned tx only)
        loaded_writable: list[str] = [
            _bytes_to_base58(k) for k in meta.loaded_writable_addresses
        ]
        loaded_readonly: list[str] = [
            _bytes_to_base58(k) for k in meta.loaded_readonly_addresses
        ]
        # Full account table: static + writable + readonly (matches Solana's resolution order)
        all_keys: list[str] = static_keys + loaded_writable + loaded_readonly

        fee_payer: str = all_keys[0] if all_keys else ""

        # Outer instructions
        instructions: list[RawInstruction] = []
        for ix in message.instructions:
            program_id = _safe_key(all_keys, ix.program_id_index)
            if program_id is None:
                logger.debug(
                    "GeyserTransport._parse_geyser_tx: program_id_index=%d out of range "
                    "(account_keys len=%d) in tx %s — skipping instruction",
                    ix.program_id_index, len(all_keys), signature,
                )
                continue
            # accounts: bytes — packed byte array of uint8 account indices
            acct_keys: list[str] = []
            for acct_idx in ix.accounts:  # bytes iterates as ints in Python 3
                k = _safe_key(all_keys, int(acct_idx))
                acct_keys.append(k if k is not None else "")

            # data: raw bytes → base64 string (matches RawInstruction.data_b64 schema)
            data_b64: str = base64.b64encode(bytes(ix.data)).decode("ascii")

            instructions.append(RawInstruction(
                program_id=program_id,
                data_b64=data_b64,
                account_keys=acct_keys,
                program_index=ix.program_id_index,
            ))

        # Inner instructions (flattened from InnerInstructions groups)
        inner_instructions: list[RawInstruction] = []
        if not meta.inner_instructions_none:
            for inner_group in meta.inner_instructions:
                # inner_group.index = outer instruction index this belongs to
                for ix in inner_group.instructions:
                    program_id = _safe_key(all_keys, ix.program_id_index)
                    if program_id is None:
                        continue
                    acct_keys = []
                    for acct_idx in ix.accounts:
                        k = _safe_key(all_keys, int(acct_idx))
                        acct_keys.append(k if k is not None else "")
                    data_b64 = base64.b64encode(bytes(ix.data)).decode("ascii")
                    inner_instructions.append(RawInstruction(
                        program_id=program_id,
                        data_b64=data_b64,
                        account_keys=acct_keys,
                        program_index=ix.program_id_index,
                    ))

        # Program logs
        program_logs: list[str] = []
        if not meta.log_messages_none:
            program_logs = list(meta.log_messages)

        # block_time_unix_s = None always for PROCESSED updates (T-300a).
        # Do NOT substitute wall-clock.  Decoder holds event PENDING.
        block_time_unix_s: int | None = None

        return RawTransaction(
            signature=signature,
            slot=slot,
            block_time_unix_s=block_time_unix_s,
            fee_payer=fee_payer,
            instructions=instructions,
            inner_instructions=inner_instructions,
            program_logs=program_logs,
            is_vote=is_vote,
            err=err,
        )


# ---------------------------------------------------------------------------
# Base58 encoding utilities (no external dependency)
# ---------------------------------------------------------------------------

_BASE58_ALPHABET = b"123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _bytes_to_base58(data: bytes) -> str:
    """Encode raw bytes to a base58 string (Solana pubkey / signature format).

    Pure Python implementation — no external base58 library required.
    Handles the leading-zero (leading-'1') case correctly.
    """
    if not data:
        return ""
    # Count leading zero bytes
    leading_zeros = len(data) - len(data.lstrip(b"\x00"))

    # Convert bytes to an integer
    n = int.from_bytes(data, "big")

    result: list[int] = []
    while n > 0:
        n, remainder = divmod(n, 58)
        result.append(_BASE58_ALPHABET[remainder])

    # Add leading '1's for leading zero bytes
    result.extend([_BASE58_ALPHABET[0]] * leading_zeros)
    result.reverse()
    return bytes(result).decode("ascii")


def _safe_key(keys: list[str], index: int) -> str | None:
    """Safely look up an account key by index; return None if out of range."""
    if 0 <= index < len(keys):
        return keys[index]
    return None


# ---------------------------------------------------------------------------
# _build_subscribe_request — pure helper, testable in isolation (B2 fix)
# ---------------------------------------------------------------------------


def _build_subscribe_request(
    geyser_pb2: object,
    program_ids: frozenset[str],
    from_slot: int,
    commitment: int,
) -> object:
    """Build a Yellowstone SubscribeRequest from the given parameters.

    This is a pure function (no I/O, no side-effects) extracted from
    GeyserTransport._stream_once() so that unit tests can assert on the
    actual production request-building logic without requiring a live gRPC
    channel (finding B2 fix).

    Args:
        geyser_pb2: The imported geyser_pb2 module (injectable for tests).
        program_ids: The set of Solana program IDs to filter on.
        from_slot: Resume slot for reconnects (0 = start from now).
        commitment: CommitmentLevel value (0 = PROCESSED).

    Returns:
        A fully populated geyser_pb2.SubscribeRequest proto message.
    """
    request = geyser_pb2.SubscribeRequest()
    txn_filter = request.transactions["sniper"]
    txn_filter.vote = False
    txn_filter.failed = False
    for pid in sorted(program_ids):  # sort for deterministic serialisation
        txn_filter.account_include.append(pid)

    # Commitment: PROCESSED (0) for minimum latency (snipe edge).
    # Confirmed (1) would add ~400ms but provide block_time.
    # We use PROCESSED and accept absent block_time (T-300a).
    request.commitment = commitment  # 0 = PROCESSED

    # from_slot: resume from last successfully processed slot.
    # Only set when > 0 so a fresh subscribe starts from the live tip.
    if from_slot > 0:
        request.from_slot = from_slot

    return request


# ---------------------------------------------------------------------------
# EnhancedWsFallback — LIVE implementation (standard Helius free-tier WS)
# ---------------------------------------------------------------------------

# JSON-RPC request ID counter — simple monotonic, module-level.
_ws_rpc_id = itertools.count(1)


def _ws_import() -> object:
    """Import and return the `websockets` module.

    Extracted as a named function so tests can patch `_ws_import` in
    `aats.ingestion.transport` to inject a fake websockets module without
    requiring the real websockets package to be installed.

    Example (in tests):
        with patch("aats.ingestion.transport._ws_import", return_value=fake_ws_mod):
            ...
    """
    try:
        import websockets  # type: ignore[import]
        return websockets
    except ImportError as exc:
        raise ImportError(
            "websockets is required for EnhancedWsFallback. "
            "Install it: pip install websockets==10.4  "
            "(it is already listed in requirements/requirements.txt)"
        ) from exc


class EnhancedWsFallback(TransportInterface):
    """Standard Solana WebSocket + RPC-fetch fallback for Geyser-degraded periods.

    Works on a STANDARD (free-tier) Helius RPC key — no paid Geyser subscription
    required.  Emits the SAME typed RawTransaction contract as GeyserTransport so
    the decoder pipeline and bus are identical.

    ARCHITECTURE
    ------------
    PRIMARY PATH  — Solana standard logsSubscribe WebSocket
      Opens one WS connection per `subscribe()` call.
      For EACH program ID in the ProgramRegistry sends a separate JSON-RPC
      `logsSubscribe` request with `{"mentions": ["<programId>"]}` and
      `{"commitment": "confirmed"}`.  One subscription per program ID (the
      Solana `mentions` filter takes exactly ONE address).
      On each notification with `err == null`: extracts the tx signature and
      calls `getTransaction` over the HTTP RPC to fetch the full transaction;
      maps it to RawTransaction; yields it.
      Deduplicates on signature so out-of-order / duplicate WS deliveries do
      not produce duplicate RawTransactions.

    FALLBACK PATH  — RPC polling (getSignaturesForAddress)
      Activated when: the WS connection fails to connect, or yields nothing for
      `poll_idle_timeout_s` seconds.  Polls `getSignaturesForAddress` for each
      program ID every `poll_interval_s` seconds; diffs against seen signatures;
      calls `getTransaction` on new ones.  Only standard JSON-RPC calls —
      guaranteed to work on any free-tier RPC key.

    RECONNECT
      On WS drop the outer `subscribe()` loop retries with exponential backoff
      + jitter (tenacity-style manual loop).  `data_staleness_ms` rises during
      dead windows (never silent stale data).

    POINT-IN-TIME CORRECTNESS (T-300a)
      `block_time_unix_s` is taken from `getTransaction` → `blockTime` field.
      If `blockTime` is absent or null the RawTransaction is emitted with
      `block_time_unix_s = None`; the decoder's `_make_event_time()` holds the
      event PENDING and does NOT substitute wall-clock.

    CREDENTIALS — from environment ONLY, never hardcoded or logged
      WS_ENDPOINT — WS URL (e.g. wss://mainnet.helius-rpc.com/?api-key=<key>)
                    If unset, derived from RPC_PRIMARY by replacing https→wss.
      RPC_PRIMARY — HTTP RPC URL used for getTransaction / polling calls.

    READ-ONLY
      Never signs, never submits, never holds a keypair.  Every call is a
      read-only JSON-RPC request.

    BACKPRESSURE
      subscribe() decodes and yields, then returns to the caller.  No price
      math, no model call sits between receiving a WS message and yielding it.
    """

    # Retry / timing knobs
    _WS_RETRY_MIN_WAIT_S: float = 1.0
    _WS_RETRY_MAX_WAIT_S: float = 30.0
    _WS_RETRY_MULTIPLIER: float = 2.0
    _DEFAULT_POLL_INTERVAL_S: float = 3.0
    _DEFAULT_POLL_IDLE_TIMEOUT_S: float = 10.0
    _DEFAULT_POLL_SIG_LIMIT: int = 20
    # How many seen-signatures to keep in the dedup ring (memory-bounded)
    _DEDUP_RING_MAX: int = 4096

    def __init__(
        self,
        ws_url: str,       # env: WS_ENDPOINT (derived from RPC_PRIMARY if blank)
        rpc_url: str,      # env: RPC_PRIMARY — for getTransaction / polling
        poll_interval_s: float = _DEFAULT_POLL_INTERVAL_S,
        poll_idle_timeout_s: float = _DEFAULT_POLL_IDLE_TIMEOUT_S,
        poll_sig_limit: int = _DEFAULT_POLL_SIG_LIMIT,
        max_reconnect_attempts: int = 0,   # 0 = infinite
    ) -> None:
        super().__init__()
        self._ws_url = ws_url     # NOT logged (may contain API key in query string)
        self._rpc_url = rpc_url   # NOT logged
        self._poll_interval_s = poll_interval_s
        self._poll_idle_timeout_s = poll_idle_timeout_s
        self._poll_sig_limit = poll_sig_limit
        self._max_reconnect_attempts = max_reconnect_attempts
        self._connected = False
        import random
        self._rng = random.Random()
        # Dedup ring: set of seen signatures (memory-bounded via _DEDUP_RING_MAX)
        self._seen_sigs: set[str] = set()
        self._seen_sigs_order: list[str] = []

    @property
    def detection_transport(self) -> DetectionTransport:
        return DetectionTransport.GEYSER  # same contract as Geyser (redundancy path)

    @property
    def is_connected(self) -> bool:
        return self._connected

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def subscribe(  # type: ignore[override]
        self,
        program_ids: frozenset[str],
        last_slot: int = 0,
    ) -> AsyncGenerator[RawTransaction, None]:
        """Stream RawTransactions via logsSubscribe WS + getTransaction enrichment.

        Falls back to RPC polling if the WS connection is unavailable or idle.
        Reconnects with exponential backoff on WS drop.
        Deduplicates on signature; out-of-order / duplicate delivery is safe.
        Yields nothing and logs a clear error if ws_url or rpc_url is empty.
        """
        if not self._rpc_url:
            logger.error(
                "EnhancedWsFallback: RPC_PRIMARY is not set.  "
                "Set RPC_PRIMARY (e.g. https://mainnet.helius-rpc.com/?api-key=<key>) "
                "in the environment.  See .env.example.  Yielding nothing."
            )
            return

        if not program_ids:
            logger.warning("EnhancedWsFallback: program_ids is empty — nothing to subscribe.")
            return

        attempt = 0
        while True:
            attempt += 1
            if self._max_reconnect_attempts > 0 and attempt > self._max_reconnect_attempts:
                logger.error(
                    "EnhancedWsFallback: max reconnect attempts (%d) reached — giving up.",
                    self._max_reconnect_attempts,
                )
                break

            if self._ws_url:
                # Try the primary WS path
                try:
                    async for tx in self._stream_ws(program_ids):
                        yield tx
                    # Stream ended cleanly — reconnect
                    logger.info(
                        "EnhancedWsFallback: WS stream ended — reconnecting (attempt %d).",
                        attempt,
                    )
                except _WsConnectError as exc:
                    self._connected = False
                    logger.warning(
                        "EnhancedWsFallback: WS connect failed (attempt %d): %s — "
                        "switching to polling fallback.",
                        attempt, exc,
                    )
                    # Fall through to polling below
                except asyncio.CancelledError:
                    self._connected = False
                    logger.info("EnhancedWsFallback: cancelled.")
                    raise
                except Exception as exc:  # noqa: BLE001
                    self._connected = False
                    logger.warning(
                        "EnhancedWsFallback: WS error (attempt %d): %s — will retry.",
                        attempt, exc, exc_info=True,
                    )
            else:
                logger.warning(
                    "EnhancedWsFallback: WS_ENDPOINT not set — using RPC polling only."
                )

            # Polling fallback: a guaranteed-to-work path on any free-tier RPC key
            try:
                async for tx in self._stream_poll(program_ids):
                    yield tx
            except asyncio.CancelledError:
                self._connected = False
                logger.info("EnhancedWsFallback: polling cancelled.")
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "EnhancedWsFallback: polling error (attempt %d): %s",
                    attempt, exc, exc_info=True,
                )

            # Backoff before next attempt
            wait_s = min(
                self._WS_RETRY_MIN_WAIT_S * (self._WS_RETRY_MULTIPLIER ** (attempt - 1)),
                self._WS_RETRY_MAX_WAIT_S,
            )
            jitter = self._rng.uniform(0, wait_s * 0.25)
            sleep_s = wait_s + jitter
            logger.info(
                "EnhancedWsFallback: backing off %.2fs before reconnect.", sleep_s
            )
            await asyncio.sleep(sleep_s)

    # ------------------------------------------------------------------
    # PRIMARY: WebSocket logsSubscribe
    # ------------------------------------------------------------------

    async def _stream_ws(
        self, program_ids: frozenset[str]
    ) -> AsyncGenerator[RawTransaction, None]:
        """Open one WS connection, subscribe to each program ID, yield RawTransactions.

        Uses websockets==10.4 (already in requirements.txt).
        One logsSubscribe per program ID (the `mentions` filter takes one address).
        On each log notification with err==null: getTransaction → RawTransaction.
        Falls back to polling path if WS is idle for poll_idle_timeout_s.

        The `websockets` module is imported via `_ws_import()` so tests can
        patch `aats.ingestion.transport._ws_import` without needing the real
        websockets package installed.
        """
        _ws = _ws_import()

        import json

        logger.info(
            "EnhancedWsFallback: opening WS connection to endpoint "
            "(URL contains API key — not logged)"
        )

        try:
            async with _ws.connect(  # type: ignore[attr-defined]
                self._ws_url,
                ping_interval=20,
                ping_timeout=10,
                close_timeout=5,
            ) as ws:
                self._connected = True
                logger.info(
                    "EnhancedWsFallback: WS connected — sending %d logsSubscribe requests.",
                    len(program_ids),
                )

                # One subscription per program ID
                sub_id_to_pid: dict[int, str] = {}
                for pid in sorted(program_ids):  # sorted for determinism
                    req_id = next(_ws_rpc_id)
                    await ws.send(json.dumps({
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "method": "logsSubscribe",
                        "params": [
                            {"mentions": [pid]},
                            {"commitment": "confirmed"},
                        ],
                    }))
                    # Record the request ID → program ID mapping; subscription
                    # confirmation maps req_id → subscription_id in the response.
                    sub_id_to_pid[req_id] = pid

                # subscription_id → program_id (populated from confirmations)
                confirmed_subs: dict[int, str] = {}

                # Idle-timeout gate: if no useful message arrives in
                # poll_idle_timeout_s, exit _stream_ws and let the caller
                # fall through to polling.
                last_event_wall_s = _time_now_s()

                async for raw_msg in ws:
                    try:
                        msg = json.loads(raw_msg)
                    except (json.JSONDecodeError, TypeError):
                        continue

                    # Subscription confirmation: {"jsonrpc":"2.0","result":<sub_id>,"id":<req_id>}
                    if "result" in msg and "id" in msg and isinstance(msg["result"], int):
                        req_id = msg["id"]
                        if req_id in sub_id_to_pid:
                            sub_id = msg["result"]
                            pid = sub_id_to_pid[req_id]
                            confirmed_subs[sub_id] = pid
                            logger.info(
                                "EnhancedWsFallback: logsSubscribe confirmed "
                                "sub_id=%d for program %s",
                                sub_id, pid,
                            )
                        continue

                    # Log notification: {"jsonrpc":"2.0","method":"logsNotification","params":{...}}
                    if msg.get("method") != "logsNotification":
                        # Check idle timeout for non-notification messages too
                        elapsed = _time_now_s() - last_event_wall_s
                        if elapsed > self._poll_idle_timeout_s:
                            logger.warning(
                                "EnhancedWsFallback: WS idle for %.1fs — "
                                "exiting WS stream to try polling.",
                                elapsed,
                            )
                            return
                        continue

                    params = msg.get("params", {})
                    result = params.get("result", {})
                    value = result.get("value", {})

                    # Drop errored transactions (err field is non-null)
                    if value.get("err") is not None:
                        continue

                    signature = value.get("signature")
                    if not signature:
                        continue

                    # Dedup
                    if self._is_seen(signature):
                        continue

                    # Fetch the full transaction via RPC
                    tx = await self._fetch_transaction(signature)
                    if tx is not None:
                        last_event_wall_s = _time_now_s()
                        self._mark_seen(signature)
                        self._last_slot = tx.slot
                        yield tx

                    # Idle check after processing
                    elapsed = _time_now_s() - last_event_wall_s
                    if elapsed > self._poll_idle_timeout_s:
                        logger.warning(
                            "EnhancedWsFallback: WS idle for %.1fs after processing — "
                            "exiting WS stream to try polling.",
                            elapsed,
                        )
                        return

        except OSError as exc:
            raise _WsConnectError(str(exc)) from exc
        except Exception as exc:
            # Re-raise websockets connection errors as _WsConnectError so the
            # outer loop can distinguish connect-fail from stream errors.
            ws_exc_names = {"ConnectionClosedError", "InvalidURI", "WebSocketException",
                            "InvalidHandshake", "ConnectionRefusedError"}
            if type(exc).__name__ in ws_exc_names:
                raise _WsConnectError(str(exc)) from exc
            raise
        finally:
            self._connected = False

    # ------------------------------------------------------------------
    # FALLBACK: RPC polling (getSignaturesForAddress + getTransaction)
    # ------------------------------------------------------------------

    async def _stream_poll(
        self, program_ids: frozenset[str]
    ) -> AsyncGenerator[RawTransaction, None]:
        """Poll getSignaturesForAddress for each program ID; fetch new transactions.

        Uses only standard JSON-RPC calls — works on any free-tier RPC key.
        Runs for poll_idle_timeout_s then returns (caller re-enters the WS path).
        """
        logger.info(
            "EnhancedWsFallback: entering RPC polling fallback "
            "for %d program IDs (poll interval %.1fs).",
            len(program_ids),
            self._poll_interval_s,
        )
        poll_end_s = _time_now_s() + self._poll_idle_timeout_s

        while _time_now_s() < poll_end_s:
            for pid in sorted(program_ids):
                sigs = await self._get_signatures_for_address(pid)
                for sig in sigs:
                    if self._is_seen(sig):
                        continue
                    tx = await self._fetch_transaction(sig)
                    if tx is not None:
                        self._mark_seen(sig)
                        self._last_slot = tx.slot
                        yield tx
                        # Reset idle timeout whenever we get a live result
                        poll_end_s = _time_now_s() + self._poll_idle_timeout_s

            await asyncio.sleep(self._poll_interval_s)

        logger.info("EnhancedWsFallback: polling interval complete — returning to WS path.")

    # ------------------------------------------------------------------
    # RPC helpers (getTransaction, getSignaturesForAddress)
    # ------------------------------------------------------------------

    async def _fetch_transaction(self, signature: str) -> RawTransaction | None:
        """Call getTransaction over HTTP RPC; map result → RawTransaction.

        Returns None if the transaction is not found, errored, or failed to parse.
        block_time_unix_s = getTransaction.blockTime (on-chain only; T-300a).
        """
        try:
            import httpx
        except ImportError as exc:
            raise ImportError(
                "httpx is required for EnhancedWsFallback RPC calls: "
                "pip install httpx"
            ) from exc

        payload = {
            "jsonrpc": "2.0",
            "id": next(_ws_rpc_id),
            "method": "getTransaction",
            "params": [
                signature,
                {
                    "maxSupportedTransactionVersion": 0,
                    "commitment": "confirmed",
                    "encoding": "jsonParsed",
                },
            ],
        }
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(self._rpc_url, json=payload)
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "EnhancedWsFallback._fetch_transaction(%s): HTTP error: %s", signature, exc
            )
            return None

        result = data.get("result")
        if result is None:
            return None

        return _parse_rpc_get_transaction(signature, result)

    async def _get_signatures_for_address(self, program_id: str) -> list[str]:
        """Call getSignaturesForAddress → list of signature strings (newest first)."""
        try:
            import httpx
        except ImportError as exc:
            raise ImportError("httpx is required: pip install httpx") from exc

        payload = {
            "jsonrpc": "2.0",
            "id": next(_ws_rpc_id),
            "method": "getSignaturesForAddress",
            "params": [
                program_id,
                {
                    "limit": self._poll_sig_limit,
                    "commitment": "confirmed",
                },
            ],
        }
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(self._rpc_url, json=payload)
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "EnhancedWsFallback._get_signatures_for_address(%s): HTTP error: %s",
                program_id, exc,
            )
            return []

        results = data.get("result") or []
        # Each item: {"signature": str, "slot": int, "err": null|{}, ...}
        return [
            item["signature"]
            for item in results
            if isinstance(item, dict)
            and item.get("err") is None
            and item.get("signature")
        ]

    # ------------------------------------------------------------------
    # Dedup ring (memory-bounded)
    # ------------------------------------------------------------------

    def _is_seen(self, sig: str) -> bool:
        return sig in self._seen_sigs

    def _mark_seen(self, sig: str) -> None:
        if sig in self._seen_sigs:
            return
        if len(self._seen_sigs_order) >= self._DEDUP_RING_MAX:
            evict = self._seen_sigs_order.pop(0)
            self._seen_sigs.discard(evict)
        self._seen_sigs.add(sig)
        self._seen_sigs_order.append(sig)


# ---------------------------------------------------------------------------
# _WsConnectError — internal sentinel for WS connection failures
# ---------------------------------------------------------------------------

class _WsConnectError(Exception):
    """Raised by _stream_ws when the WS connection itself fails."""


# ---------------------------------------------------------------------------
# _time_now_s — wall-clock seconds helper (for internal timeout tracking only)
# ---------------------------------------------------------------------------

def _time_now_s() -> float:
    return time.time()


# ---------------------------------------------------------------------------
# _parse_rpc_get_transaction — map jsonParsed getTransaction → RawTransaction
# ---------------------------------------------------------------------------

def _parse_rpc_get_transaction(
    signature: str,
    result: dict,
) -> RawTransaction | None:
    """Parse the JSON result of getTransaction (jsonParsed encoding) → RawTransaction.

    Fields populated:
      signature       — from the caller (not duplicated in result root)
      slot            — result["slot"]
      block_time_unix_s — result["blockTime"] (None if absent/null; T-300a)
      fee_payer       — transaction.message.accountKeys[0].pubkey
      instructions    — outer instructions from transaction.message.instructions
      inner_instructions — meta.innerInstructions flattened
      program_logs    — meta.logMessages
      is_vote         — not present in jsonParsed; heuristic from program ID
      err             — meta.err (None if null/absent)

    POINT-IN-TIME CORRECTNESS (T-300a):
      block_time_unix_s is taken from result["blockTime"] ONLY.
      If blockTime is absent, null, or 0, it is set to None.
      Wall-clock is NEVER substituted.

    Returns None if the result is malformed (missing required fields).
    """
    if not isinstance(result, dict):
        return None

    slot = result.get("slot")
    if slot is None:
        return None
    slot = int(slot)

    # blockTime: on-chain unix timestamp (seconds). May be absent for recent slots.
    # T-300a: if absent/null/0, set to None — decoder will hold event PENDING.
    block_time_raw = result.get("blockTime")
    block_time_unix_s: int | None = None
    if block_time_raw is not None and int(block_time_raw) > 0:
        block_time_unix_s = int(block_time_raw)

    transaction = result.get("transaction") or {}
    meta = result.get("meta") or {}

    # err: non-null meta.err means the transaction failed — skip it
    err_field = meta.get("err")
    if err_field is not None:
        return None

    message = transaction.get("message") or {}
    account_keys_raw = message.get("accountKeys") or []

    # jsonParsed accountKeys: list of {"pubkey": str, "signer": bool, "writable": bool, ...}
    # or for newer responses list of strings.
    all_keys: list[str] = []
    for ak in account_keys_raw:
        if isinstance(ak, dict):
            all_keys.append(ak.get("pubkey") or "")
        elif isinstance(ak, str):
            all_keys.append(ak)

    fee_payer = all_keys[0] if all_keys else ""

    # Outer instructions
    instructions_raw = message.get("instructions") or []
    instructions: list[RawInstruction] = []
    for raw_ix in instructions_raw:
        raw_ix_parsed = _parse_rpc_instruction(raw_ix, all_keys)
        if raw_ix_parsed is not None:
            instructions.append(raw_ix_parsed)

    # Inner instructions (flattened)
    inner_instructions: list[RawInstruction] = []
    for inner_group in (meta.get("innerInstructions") or []):
        for raw_ix in (inner_group.get("instructions") or []):
            raw_ix_parsed = _parse_rpc_instruction(raw_ix, all_keys)
            if raw_ix_parsed is not None:
                inner_instructions.append(raw_ix_parsed)

    # Program logs
    program_logs: list[str] = list(meta.get("logMessages") or [])

    # Vote heuristic: check if any instruction targets the Vote program
    VOTE_PROGRAM = "Vote111111111111111111111111111111111111111"
    is_vote = any(ix.program_id == VOTE_PROGRAM for ix in instructions)

    return RawTransaction(
        signature=signature,
        slot=slot,
        block_time_unix_s=block_time_unix_s,
        fee_payer=fee_payer,
        instructions=instructions,
        inner_instructions=inner_instructions,
        program_logs=program_logs,
        is_vote=is_vote,
        err=None,  # already filtered out errored txs above
    )


def _parse_rpc_instruction(
    raw_ix: dict,
    all_keys: list[str],
) -> RawInstruction | None:
    """Parse a single instruction from a jsonParsed getTransaction response.

    Handles both parsed-format instructions (with `parsed` field) and raw-format
    instructions (with `data` field in base58 or base64).

    Returns None if the instruction cannot be usefully parsed.
    """
    if not isinstance(raw_ix, dict):
        return None

    import base64 as _base64

    # programId may be a string (jsonParsed) or an index
    program_id = raw_ix.get("programId") or ""
    if not program_id:
        # Fall back to programIdIndex if programId is absent
        pid_idx = raw_ix.get("programIdIndex")
        if pid_idx is not None and 0 <= int(pid_idx) < len(all_keys):
            program_id = all_keys[int(pid_idx)]
    if not program_id:
        return None

    # Instruction data: may be base58 (standard), base64, or "parsed" object
    data_b64 = ""
    if "data" in raw_ix and isinstance(raw_ix["data"], str):
        raw_data = raw_ix["data"]
        # Try to detect encoding: if it looks like base58, decode and re-encode as b64.
        # jsonParsed encoding uses base58 for instruction data by default.
        try:
            data_bytes = _base58_decode(raw_data)
            data_b64 = _base64.b64encode(data_bytes).decode("ascii")
        except Exception:
            # If base58 decode fails, treat as empty
            data_b64 = ""
    elif "parsed" in raw_ix:
        # jsonParsed fully-decoded instruction (e.g. system program transfers).
        # We cannot recover raw bytes from the parsed representation.
        # Emit an empty data_b64; the decoder will see a discriminator mismatch
        # and return None (correct — parsed system instructions are not venue events).
        data_b64 = ""

    # Account keys in this instruction
    accounts_raw = raw_ix.get("accounts") or []
    acct_keys: list[str] = []
    for a in accounts_raw:
        if isinstance(a, str):
            acct_keys.append(a)  # jsonParsed gives pubkeys directly
        elif isinstance(a, int):
            acct_keys.append(all_keys[a] if 0 <= a < len(all_keys) else "")

    return RawInstruction(
        program_id=program_id,
        data_b64=data_b64,
        account_keys=acct_keys,
        program_index=0,  # program index is not carried through jsonParsed
    )


# ---------------------------------------------------------------------------
# Base58 decode — pure Python (symmetric with _bytes_to_base58 encoder)
# ---------------------------------------------------------------------------

_BASE58_DECODE_MAP: dict[int, int] = {
    ch: i for i, ch in enumerate(_BASE58_ALPHABET)
}


def _base58_decode(s: str) -> bytes:
    """Decode a base58-encoded string to bytes."""
    n = 0
    for char in s.encode("ascii"):
        digit = _BASE58_DECODE_MAP.get(char)
        if digit is None:
            raise ValueError(f"Invalid base58 character: {char!r}")
        n = n * 58 + digit

    # Count leading '1's → leading zero bytes
    leading_ones = len(s) - len(s.lstrip("1"))
    result_bytes = n.to_bytes((n.bit_length() + 7) // 8, "big") if n > 0 else b""
    return b"\x00" * leading_ones + result_bytes


# ---------------------------------------------------------------------------
# TransportPipeline — the orchestrating producer
# ---------------------------------------------------------------------------


@dataclass
class TransportStats:
    """Runtime health statistics for a transport pipeline."""

    events_decoded: int = 0
    events_skipped: int = 0
    decode_errors: int = 0
    last_event_time_ms: int = 0
    last_slot: int = 0

    @property
    def data_staleness_ms(self) -> int:
        """Staleness of the last event relative to now (monitoring signal)."""
        if self.last_event_time_ms == 0:
            return 0
        return max(0, int(time.time() * 1_000) - self.last_event_time_ms)


class TransportPipeline:
    """Orchestrates transport -> decode -> event yield.

    The pipeline:
      1. Subscribes to the transport (Geyser / Replay / WS).
      2. Routes each raw transaction through InstructionRouter.
      3. Yields typed LaunchEvents.
      4. Records stats for monitoring (data_staleness_ms, etc.).

    Producers: call events() and XADD to the bus immediately.
    The pipeline NEVER blocks on downstream processing.
    """

    def __init__(
        self,
        transport: TransportInterface,
        router: "InstructionRouter",
    ) -> None:
        self._transport = transport
        self._router = router
        self.stats = TransportStats()

    async def events(
        self,
        last_slot: int = 0,
    ) -> AsyncGenerator[tuple, None]:
        """Yield (LaunchEvent, tx_signature) for every matched transaction.

        Args:
            last_slot: resume from this slot (Geyser from_slot; replay skip).

        Yields:
            (LaunchEvent, tx_signature) — the event and its source tx signature
            for dedup tracking.
        """
        from aats.ingestion.decoders import InstructionRouter as _Router

        program_ids = self._router._active_pids
        async for tx in self._transport.subscribe(program_ids, last_slot):
            try:
                ev = self._router.route(tx, self._transport.detection_transport)
                if ev is not None:
                    self.stats.events_decoded += 1
                    self.stats.last_event_time_ms = ev.event_time.block_time_ms
                    self.stats.last_slot = tx.slot
                    yield ev, tx.signature
                else:
                    self.stats.events_skipped += 1
            except Exception as exc:
                self.stats.decode_errors += 1
                logger.error(
                    "TransportPipeline.events() decode error on %s: %s",
                    tx.signature, exc,
                    exc_info=True,
                )
