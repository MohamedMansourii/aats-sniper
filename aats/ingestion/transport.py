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
# EnhancedWsFallback — STUB (enhanced WebSocket fallback plug-in point)
# ---------------------------------------------------------------------------


class EnhancedWsFallback(TransportInterface):
    """Enhanced WebSocket fallback (Helius / Triton / QuickNode enhanced logsSubscribe).

    STATUS: STUB — real WebSocket client is NOT instantiated here.
    The fallback is a REDUNDANCY PATH for when Geyser is degraded, not a
    feature fork.  It emits the SAME typed RawTransaction that Geyser emits,
    so the decoder pipeline and bus are identical.

    When wired:
      - Uses websockets / httpx-ws for logsSubscribe and accountSubscribe.
      - Filters on `mentions` to the active program IDs.
      - On disconnect: tenacity retry with exponential backoff + jitter.
      - On reconnect: subscribes from the head of the stream (no from_slot for WS)
        and emits `data_staleness_ms` until caught up.

    PLUG_IN_HERE: replace the stub body of subscribe() with a real websockets
    connection, logsSubscribe request, and message parser that maps to RawTransaction.
    """

    def __init__(
        self,
        ws_url: str,    # env: ENHANCED_WS_URL
        api_key: str,   # env: HELIUS_API_KEY / TRITON_API_KEY (never hardcoded)
    ) -> None:
        super().__init__()
        self._ws_url = ws_url
        self._api_key = api_key  # NOT logged
        self._connected = False

    @property
    def detection_transport(self) -> DetectionTransport:
        return DetectionTransport.GEYSER  # WS fallback uses same tag (same event contract)

    @property
    def is_connected(self) -> bool:
        return self._connected

    async def subscribe(  # type: ignore[override]
        self,
        program_ids: frozenset[str],
        last_slot: int = 0,
    ) -> AsyncGenerator[RawTransaction, None]:
        """PLUG_IN_HERE — enhanced WS logsSubscribe / accountSubscribe.

        Real implementation sketch:
            async with websockets.connect(self._ws_url) as ws:
                await ws.send(json.dumps({
                    "jsonrpc": "2.0", "id": 1,
                    "method": "logsSubscribe",
                    "params": [{"mentions": list(program_ids)}, {"commitment": "processed"}]
                }))
                async for msg in ws:
                    data = json.loads(msg)
                    tx = self._parse_ws_log(data)
                    if tx:
                        self._last_slot = tx.slot
                        yield tx
        """
        logger.warning(
            "EnhancedWsFallback.subscribe() called on STUB. "
            "PLUG_IN_HERE: wire websockets + logsSubscribe to %s",
            self._ws_url,
        )
        return
        yield  # pragma: no cover


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
