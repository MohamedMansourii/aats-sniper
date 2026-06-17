"""Redis Streams message bus — producer and consumer (T-300).

ARCHITECTURE (BLUEPRINT.md §5, ADR-0001):
  - Redis Streams with consumer groups (XREADGROUP + XACK) for durable consumers.
  - MAXLEN ~ caps on every stream so a slow consumer can never OOM a producer.
  - The producer NEVER blocks on processing: XADD and return.
  - Stream names and key schemas from api-contracts.md and data-models.md §9.

Stream registry (api-contracts.md §5.1):
  STREAM_LAUNCH_EVENTS  = "launch.events"   MAXLEN ~100_000
  STREAM_FEATURE_FRAMES = "feature.frames"  MAXLEN ~50_000
  STREAM_DECISION       = "decision.signals" MAXLEN ~50_000
  STREAM_INTENTS        = "intents"          MAXLEN ~50_000
  STREAM_FILLS          = "fills"            MAXLEN ~50_000
  STREAM_OPS_FEED       = "ops.feed"         MAXLEN ~10_000

Consumer groups for launch.events:
  GROUP_SNIPE = "g_snipe"   (SNIPE loop)
  GROUP_SLOW  = "g_slow"    (SLOW loop / feature pipeline)

Point-in-time correctness:
  Every message carries event_time (on-chain) AND ingest_time (wall-clock at
  XADD).  The two timestamps are explicit and separate.  Downstream consumers
  JOIN on event_time only; ingest_time is for monitoring and lag detection.

Dedup:
  Producers stamp (signature, instruction_index) in the message.  Consumers
  track seen (sig, idx) pairs in a bounded Redis set and skip duplicates.
  Out-of-order delivery is handled by the point-in-time store (event-time keying).

Injectable Redis client:
  The BusProducer and BusConsumer accept a redis.asyncio.Redis instance at
  construction.  Tests inject a FakeRedis or a mock; live code passes the
  real client configured from env vars.

NO real capital: the bus is read-side only.  It carries events, not intents to submit.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from aats.contracts.events import LaunchEvent

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Stream registry constants (api-contracts.md §5.1)
# ---------------------------------------------------------------------------

STREAM_LAUNCH_EVENTS  = "launch.events"
STREAM_FEATURE_FRAMES = "feature.frames"
STREAM_DECISION       = "decision.signals"
STREAM_INTENTS        = "intents"
STREAM_FILLS          = "fills"
STREAM_OPS_FEED       = "ops.feed"

MAXLEN_LAUNCH_EVENTS  = 100_000
MAXLEN_FEATURE_FRAMES = 50_000
MAXLEN_DECISION       = 50_000
MAXLEN_INTENTS        = 50_000
MAXLEN_FILLS          = 50_000
MAXLEN_OPS_FEED       = 10_000

# Consumer groups for launch.events
GROUP_SNIPE = "g_snipe"
GROUP_SLOW  = "g_slow"


# ---------------------------------------------------------------------------
# Message envelope
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BusMessage:
    """A message as read from a Redis Stream entry.

    stream_id: the Redis entry ID (e.g., "1718700000000-0")
    payload: the decoded event (LaunchEvent or other typed model)
    ingest_time_ms: wall-clock at XADD (monitoring only)
    source_signature: on-chain tx signature (for dedup)
    instruction_index: instruction index in the tx (for dedup)
    """

    stream_id: str
    payload: Any               # typed event (LaunchEvent, etc.)
    ingest_time_ms: int
    source_signature: str
    instruction_index: int = 0


# ---------------------------------------------------------------------------
# BusProducer
# ---------------------------------------------------------------------------


class BusProducer:
    """Produces typed events to Redis Streams.

    Usage:
        producer = BusProducer(redis_client)
        await producer.publish_launch_event(event, sig="...", ix_index=0)

    The producer NEVER blocks on downstream processing.  XADD is fire-and-forget
    (aside from the network round-trip to Redis).

    MAXLEN ~ is approximate trimming (O(1) amortized) to avoid OOM on slow consumers.
    """

    def __init__(self, redis_client: Any) -> None:
        """Args:
            redis_client: an async Redis client (redis.asyncio.Redis or compatible mock).
        """
        self._r = redis_client

    async def publish_launch_event(
        self,
        event: LaunchEvent,
        signature: str,
        instruction_index: int = 0,
    ) -> str:
        """XADD a LaunchEvent to launch.events.

        Returns the Redis stream entry ID.
        Raises on Redis error (let it propagate — a silent dead bus is worse).
        """
        ingest_time_ms = int(time.time() * 1_000)
        payload = {
            # Dedup key
            "sig": signature,
            "ix_idx": str(instruction_index),
            # Timestamps — explicit separation
            "ingest_time_ms": str(ingest_time_ms),
            # Staleness (monitoring)
            "data_staleness_ms": str(event.data_staleness_ms),
            # Full event JSON
            "event": event.model_dump_json(),
        }
        entry_id: str = await self._r.xadd(
            STREAM_LAUNCH_EVENTS,
            payload,
            maxlen=MAXLEN_LAUNCH_EVENTS,
            approximate=True,   # MAXLEN ~ (O(1) amortized, not exact)
        )
        logger.debug(
            "bus.publish_launch_event mint=%s slot=%d entry=%s staleness=%dms",
            event.mint, event.event_time.slot, entry_id, event.data_staleness_ms,
        )
        return entry_id

    async def publish_ops_event(self, event_type: str, data: dict) -> str:
        """Publish a control-plane ops event to ops.feed (SSE source)."""
        ingest_time_ms = int(time.time() * 1_000)
        payload = {
            "type": event_type,
            "ingest_time_ms": str(ingest_time_ms),
            "data": json.dumps(data),
        }
        entry_id: str = await self._r.xadd(
            STREAM_OPS_FEED,
            payload,
            maxlen=MAXLEN_OPS_FEED,
            approximate=True,
        )
        return entry_id

    async def ensure_consumer_groups(self) -> None:
        """Create consumer groups for launch.events if they don't exist.

        Idempotent — safe to call on every startup.
        Uses MKSTREAM so the stream is created if not present.
        """
        for group in (GROUP_SNIPE, GROUP_SLOW):
            try:
                await self._r.xgroup_create(
                    STREAM_LAUNCH_EVENTS,
                    group,
                    id="$",       # start from new messages (not history)
                    mkstream=True,
                )
                logger.info("Created consumer group %s on %s", group, STREAM_LAUNCH_EVENTS)
            except Exception as exc:
                # BUSYGROUP is expected on repeated startup; log others
                if "BUSYGROUP" not in str(exc):
                    logger.warning(
                        "xgroup_create %s/%s: %s", STREAM_LAUNCH_EVENTS, group, exc
                    )


# ---------------------------------------------------------------------------
# BusConsumer
# ---------------------------------------------------------------------------


class BusConsumer:
    """Consumes LaunchEvents from Redis Streams (XREADGROUP + XACK).

    Usage:
        consumer = BusConsumer(redis_client, group=GROUP_SLOW, consumer="slow-1")
        async for msg in consumer.consume_launch_events():
            # process msg.payload (LaunchEvent)
            await consumer.ack(msg)

    Idempotency:
        The consumer tracks seen (signature, ix_idx) pairs in a bounded in-memory
        set.  Duplicates (from reconnect or re-delivery) are logged and skipped;
        the ack is still sent so the message is not re-delivered.
    """

    def __init__(
        self,
        redis_client: Any,
        group: str,
        consumer_name: str,
        dedup_capacity: int = 10_000,
        block_ms: int = 1_000,
    ) -> None:
        self._r = redis_client
        self._group = group
        self._consumer_name = consumer_name
        self._dedup_capacity = dedup_capacity
        self._block_ms = block_ms
        self._seen: set[tuple[str, int]] = set()

    async def consume_launch_events(
        self,
        batch_size: int = 100,
    ) -> AsyncIterator[BusMessage]:
        """Yield BusMessages from launch.events consumer group.

        Reads pending messages first (PEL — messages delivered but not acked),
        then new messages (">").  Handles re-delivery idempotently.
        """
        # First drain pending messages (PEL) from a previous consumer that crashed
        async for msg in self._drain_pending():
            yield msg

        # Then read new messages
        while True:
            try:
                entries = await self._r.xreadgroup(
                    self._group,
                    self._consumer_name,
                    {STREAM_LAUNCH_EVENTS: ">"},
                    count=batch_size,
                    block=self._block_ms,
                )
            except Exception as exc:
                logger.error("BusConsumer.xreadgroup error: %s", exc, exc_info=True)
                raise

            if not entries:
                continue

            for _stream_name, messages in entries:
                for entry_id, fields in messages:
                    parsed: BusMessage | None = self._parse_entry(entry_id, fields)
                    if parsed is None:
                        continue
                    dedup_key = (parsed.source_signature, parsed.instruction_index)
                    if dedup_key in self._seen:
                        logger.debug("bus.dedup skip sig=%s ix=%d", *dedup_key)
                        await self.ack(parsed)
                        continue
                    self._seen.add(dedup_key)
                    if len(self._seen) > self._dedup_capacity:
                        # Evict oldest (approximate — set doesn't track order)
                        to_remove = next(iter(self._seen))
                        self._seen.discard(to_remove)
                    yield parsed

    async def _drain_pending(self) -> AsyncIterator[BusMessage]:
        """Drain PEL (pending entries list) — messages delivered but not acked."""
        try:
            pending = await self._r.xpending_range(
                STREAM_LAUNCH_EVENTS,
                self._group,
                min="-",
                max="+",
                count=1000,
                consumername=self._consumer_name,
            )
        except Exception as exc:
            logger.warning("xpending_range: %s", exc)
            return

        if not pending:
            return

        ids = [p["message_id"] for p in pending]
        try:
            claimed = await self._r.xclaim(
                STREAM_LAUNCH_EVENTS,
                self._group,
                self._consumer_name,
                min_idle_time=0,
                message_ids=ids,
            )
        except Exception as exc:
            logger.warning("xclaim: %s", exc)
            return

        for entry_id, fields in claimed:
            msg = self._parse_entry(entry_id, fields)
            if msg:
                yield msg

    def _parse_entry(self, entry_id: str, fields: dict) -> BusMessage | None:
        """Parse a Redis Stream entry into a BusMessage."""
        try:
            event_json = fields.get(b"event") or fields.get("event")
            if event_json is None:
                return None
            if isinstance(event_json, bytes):
                event_json = event_json.decode()
            payload = LaunchEvent.model_validate_json(event_json)

            sig_raw = fields.get(b"sig") or fields.get("sig", b"")
            sig = sig_raw.decode() if isinstance(sig_raw, bytes) else str(sig_raw)

            ix_raw = fields.get(b"ix_idx") or fields.get("ix_idx", b"0")
            ix_idx = int(ix_raw.decode() if isinstance(ix_raw, bytes) else ix_raw)

            ingest_raw = fields.get(b"ingest_time_ms") or fields.get("ingest_time_ms", b"0")
            ingest_ms = int(ingest_raw.decode() if isinstance(ingest_raw, bytes) else ingest_raw)

            eid = entry_id.decode() if isinstance(entry_id, bytes) else entry_id

            return BusMessage(
                stream_id=eid,
                payload=payload,
                ingest_time_ms=ingest_ms,
                source_signature=sig,
                instruction_index=ix_idx,
            )
        except Exception as exc:
            logger.warning("BusConsumer._parse_entry error: %s", exc)
            return None

    async def ack(self, msg: BusMessage) -> None:
        """Acknowledge a processed message (XACK)."""
        try:
            await self._r.xack(STREAM_LAUNCH_EVENTS, self._group, msg.stream_id)
        except Exception as exc:
            logger.error("BusConsumer.ack error %s: %s", msg.stream_id, exc)
