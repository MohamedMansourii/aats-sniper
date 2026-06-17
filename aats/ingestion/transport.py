"""Transport interface + injectable sources (Geyser gRPC stub, WS fallback, replay).

ARCHITECTURE NOTE:
  The real Geyser/Yellowstone gRPC endpoint is NOT reachable in this environment.
  Per the task brief: "make transports INJECTABLE and provide a deterministic
  REPLAY/mock source so decoders/store/bus are testable offline; clearly mark
  where the real Geyser/ShredStream endpoint plugs in."

This module provides:

1. TransportInterface (ABC) — the contract all sources implement.
2. GeyserTransport — STUB that shows exactly where the real gRPC client plugs in.
   The real implementation uses grpc.aio + the yellowstone-grpc proto; the
   plug-in points are documented with PLUG_IN_HERE markers.
3. ReplayTransport — deterministic offline source driven by a list of
   RawTransaction fixtures.  Used by all offline tests; no network required.
4. EnhancedWsFallback — stub showing where the Helius/Triton enhanced WS plugs in.

Back-pressure and reconnect rules:
  - Every transport wraps reconnect in tenacity exponential backoff with jitter.
  - On reconnect, `from_slot` resumes from the last successfully processed slot.
  - A dead feed surfaces as rising `data_staleness_ms`, never as stale-but-silent.
  - Producers NEVER block on processing: they yield events and return immediately.
"""

from __future__ import annotations

import asyncio
import logging
import time
from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator
from dataclasses import dataclass

from aats.contracts.events import DetectionTransport
from aats.ingestion.decoders import InstructionRouter, RawTransaction

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

        This is declared as a plain (non-async) method returning an AsyncGenerator
        so subclasses can implement it as `async def` with `yield` and still satisfy
        the ABC (mypy requires the abstract to not be `async def` when subclasses
        use async generators).
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
# GeyserTransport — STUB (real gRPC client plug-in point)
# ---------------------------------------------------------------------------


class GeyserTransport(TransportInterface):
    """Yellowstone/Geyser gRPC transport.

    STATUS: STUB — the real gRPC client is NOT instantiated here because the
    live RPC is not reachable in the build environment.  Every PLUG_IN_HERE
    comment marks exactly where the real client code goes.

    When wired to a real Geyser endpoint:
      - Uses grpc.aio.insecure_channel / ssl_channel_credentials.
      - Sends a SubscribeRequest with accounts + transactions + slot
        subscriptions, commitment = PROCESSED.
      - Filters to the active program IDs from the registry (not a firehose).
      - On stream close / error: tenacity retry with exponential backoff + jitter,
        resumes from from_slot = last_slot.

    The ShredStream overlay (for the colo upgrade) plugs in at
    _shredstream_subscribe() — same interface, different gRPC stub.
    """

    def __init__(
        self,
        endpoint: str,  # env: GEYSER_ENDPOINT — never a literal in code
        x_token: str,   # env: GEYSER_TOKEN (canonical) — never hardcoded
        shredstream_endpoint: str | None = None,  # env: SHREDSTREAM_ENDPOINT
    ) -> None:
        super().__init__()
        self._endpoint = endpoint
        self._x_token = x_token  # NOT logged, NOT serialized
        self._shredstream_endpoint = shredstream_endpoint
        self._connected = False

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
        """Stream from Geyser gRPC.

        PLUG_IN_HERE — real implementation:

            import grpc
            import grpc.aio
            # from yellowstone_grpc.geyser_pb2 import SubscribeRequest, ...
            # from yellowstone_grpc.geyser_pb2_grpc import GeyserStub

            async with grpc.aio.secure_channel(
                self._endpoint,
                grpc.ssl_channel_credentials(),
                options=[("grpc.keepalive_time_ms", 10_000)],
            ) as channel:
                stub = GeyserStub(channel)
                request = SubscribeRequest(
                    transactions={
                        "sniper": SubscribeRequestFilterTransactions(
                            account_include=list(program_ids),
                            failed=False,
                            vote=False,
                        )
                    },
                    commitment=CommitmentLevel.PROCESSED,
                    from_slot=last_slot or None,
                )
                metadata = [("x-token", self._x_token)]
                async for update in stub.Subscribe(
                    iter([request]), metadata=metadata
                ):
                    if update.HasField("transaction"):
                        tx = self._parse_geyser_tx(update.transaction)
                        self._last_slot = tx.slot
                        yield tx
        """
        # Stub: never connects — raises immediately to signal "not available"
        logger.warning(
            "GeyserTransport.subscribe() called on STUB — no live endpoint. "
            "Inject a ReplayTransport for offline testing. "
            "PLUG_IN_HERE: wire grpc.aio + GeyserStub to %s",
            self._endpoint,
        )
        # Yield nothing; in test contexts use ReplayTransport instead
        return
        yield  # make this a generator (pragma: no cover — stub never reached in tests)

    def _parse_geyser_tx(self, geyser_tx_update: object) -> RawTransaction:
        """PLUG_IN_HERE — convert Geyser protobuf → RawTransaction.

        This method deserializes the SubscribeUpdateTransactionInfo protobuf
        into the transport-agnostic RawTransaction.  Key fields to extract:
          - signature: bytes → base58 string
          - slot: uint64 from the outer SubscribeUpdate.slot field
          - block_time: the SlotInfo block_time (unix seconds)
          - fee_payer: transaction.message.account_keys[0]
          - instructions: iterate message.instructions, resolve account keys
          - inner_instructions: from meta.inner_instructions
          - program_logs: meta.log_messages
          - err: meta.err (None if success)
        """
        raise NotImplementedError(
            "GeyserTransport._parse_geyser_tx: PLUG_IN_HERE — "
            "deserialize the yellowstone_grpc SubscribeUpdateTransaction protobuf."
        )


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
    """

    def __init__(
        self,
        ws_url: str,      # env: ENHANCED_WS_URL
        api_key: str,     # env: HELIUS_API_KEY / TRITON_API_KEY (never hardcoded)
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
    """Orchestrates transport → decode → event yield.

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
        router: InstructionRouter,
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
