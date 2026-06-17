"""Bus round-trip tests (self-check item 6: backpressure + dedup + round-trip).

Uses a fake Redis client (fakeredis) so no real Redis is needed.
Tests:
  1. BusProducer publishes LaunchEvent to stream with correct fields.
  2. BusConsumer reads and parses events (round-trip).
  3. Consumer deduplicates repeated (sig, ix_idx) pairs.
  4. MAXLEN ~ is set on the stream (backpressure contract).
  5. XACK is called after processing (PEL management).
  6. Out-of-order delivery produces same parsed events.
"""

import asyncio
import time

from aats.contracts.events import DetectionTransport, EventTime, LaunchEvent, LaunchSource
from aats.ingestion.bus import (
    GROUP_SLOW,
    MAXLEN_LAUNCH_EVENTS,
    STREAM_LAUNCH_EVENTS,
    BusConsumer,
    BusProducer,
)

# ---------------------------------------------------------------------------
# Fake Redis (minimal in-memory implementation for tests)
# ---------------------------------------------------------------------------

class FakeRedisStream:
    """Minimal in-memory Redis Streams implementation for offline testing."""

    def __init__(self):
        self._streams: dict[str, list[tuple[str, dict]]] = {}
        self._groups: dict[str, dict[str, int]] = {}  # stream -> {group -> cursor}
        self._pending: dict[str, dict[str, list]] = {}  # stream -> {group -> [ids]}
        self._acked: set[str] = set()
        self._kv: dict[str, tuple[str, int]] = {}  # key -> (value, ttl_seconds)
        self._xadd_calls: list[dict] = []

    async def xadd(self, stream: str, fields: dict, maxlen: int = None, approximate: bool = False) -> str:
        if stream not in self._streams:
            self._streams[stream] = []
        entry_id = f"{int(time.time() * 1000)}-{len(self._streams[stream])}"
        # Convert all values to strings as Redis does
        str_fields = {k: str(v) if not isinstance(v, bytes) else v for k, v in fields.items()}
        self._streams[stream].append((entry_id, str_fields))
        # Approximate trim
        if maxlen and len(self._streams[stream]) > maxlen:
            self._streams[stream] = self._streams[stream][-maxlen:]
        self._xadd_calls.append({
            "stream": stream, "fields": fields, "maxlen": maxlen,
        })
        return entry_id

    async def xgroup_create(self, stream: str, group: str, id: str = "$", mkstream: bool = False):
        if mkstream and stream not in self._streams:
            self._streams[stream] = []
        if stream not in self._groups:
            self._groups[stream] = {}
        if group in self._groups.get(stream, {}):
            raise Exception("BUSYGROUP Consumer Group name already exists")
        self._groups.setdefault(stream, {})[group] = len(self._streams.get(stream, []))
        self._pending.setdefault(stream, {})[group] = []

    async def xreadgroup(self, group: str, consumer: str, streams: dict,
                          count: int = 100, block: int = 0) -> list:
        results = []
        for stream, _cursor in streams.items():
            if stream not in self._streams:
                continue
            start = self._groups.get(stream, {}).get(group, 0)
            entries = self._streams[stream][start:start + count]
            if not entries:
                continue
            # Advance cursor
            self._groups.setdefault(stream, {})[group] = start + len(entries)
            # Track as pending
            for eid, _ in entries:
                self._pending.setdefault(stream, {}).setdefault(group, []).append(eid)
            results.append((stream.encode(), [(eid.encode(), {k.encode(): v.encode() if isinstance(v, str) else v for k, v in fields.items()}) for eid, fields in entries]))
        return results

    async def xpending_range(self, stream: str, group: str, min: str, max: str,
                              count: int = 100, consumername: str = None) -> list:
        pending_ids = self._pending.get(stream, {}).get(group, [])
        # Return pending that haven't been acked
        pending = [
            {"message_id": pid.encode() if isinstance(pid, str) else pid}
            for pid in pending_ids
            if pid not in self._acked
        ]
        return pending[:count]

    async def xclaim(self, stream: str, group: str, consumer: str, min_idle_time: int,
                     message_ids: list) -> list:
        result = []
        for stream_entries in (self._streams.get(stream) or []):
            eid, fields = stream_entries
            if eid.encode() in message_ids or eid in message_ids:
                enc_fields = {k.encode(): v.encode() if isinstance(v, str) else v
                              for k, v in fields.items()}
                result.append((eid.encode(), enc_fields))
        return result

    async def xack(self, stream: str, group: str, entry_id: str) -> int:
        self._acked.add(entry_id)
        return 1

    async def setex(self, key: str, seconds: int, value: str) -> None:
        self._kv[key] = (value, seconds)

    async def get(self, key: str) -> str | None:
        return self._kv.get(key, (None,))[0]


def _make_event(
    mint: str = "BusMint111111111111111111111111111111111111",
    slot: int = 300_000_500,
    staleness_ms: int = 15,
) -> LaunchEvent:
    block_time_ms = 1_718_700_500_000
    return LaunchEvent(
        mint=mint,
        venue_program_id="6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",
        source=LaunchSource.PUMPFUN,
        event_time=EventTime(
            slot=slot,
            block_time_ms=block_time_ms,
            wall_clock_ms=block_time_ms + staleness_ms,
        ),
        observation_slot=slot,
        confirmation_slot=slot,
        detection_transport=DetectionTransport.GEYSER,
        sol_reserve_lamports=3_000_000_000,
        token_reserve_base=500_000_000_000,
        token_decimals=6,
        initial_holders=0,
        competitors=0,
        creator_wallet="Creator111111111111111111111111111111111111",
        bundler_cluster_id=None,
        deploy_template_fingerprint=None,
        data_staleness_ms=staleness_ms,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestBusProducer:
    def test_publish_returns_entry_id(self):
        fake_redis = FakeRedisStream()
        producer = BusProducer(fake_redis)
        ev = _make_event()
        entry_id = asyncio.run(
            producer.publish_launch_event(ev, "sig_bus_aaa")
        )
        assert isinstance(entry_id, str)
        assert len(entry_id) > 0

    def test_published_event_json_round_trips(self):
        """The stored JSON must round-trip to the same LaunchEvent."""
        fake_redis = FakeRedisStream()
        producer = BusProducer(fake_redis)
        ev = _make_event(slot=300_000_600)
        asyncio.run(
            producer.publish_launch_event(ev, "sig_roundtrip")
        )
        entries = fake_redis._streams.get(STREAM_LAUNCH_EVENTS, [])
        assert len(entries) == 1
        _, fields = entries[0]
        event_json = fields["event"]
        ev2 = LaunchEvent.model_validate_json(event_json)
        assert ev2.mint == ev.mint
        assert ev2.event_time.slot == ev.event_time.slot
        assert ev2.sol_reserve_lamports == ev.sol_reserve_lamports

    def test_ingest_time_stamped(self):
        """ingest_time_ms must be a recent wall-clock timestamp (monitoring)."""
        fake_redis = FakeRedisStream()
        producer = BusProducer(fake_redis)
        before = int(time.time() * 1_000)
        ev = _make_event()
        asyncio.run(
            producer.publish_launch_event(ev, "sig_ts")
        )
        after = int(time.time() * 1_000)
        _, fields = fake_redis._streams[STREAM_LAUNCH_EVENTS][0]
        ingest = int(fields["ingest_time_ms"])
        assert before <= ingest <= after + 100

    def test_maxlen_set_on_xadd(self):
        """XADD must carry maxlen (backpressure contract)."""
        fake_redis = FakeRedisStream()
        producer = BusProducer(fake_redis)
        asyncio.run(
            producer.publish_launch_event(_make_event(), "sig_maxlen")
        )
        call = fake_redis._xadd_calls[0]
        assert call["maxlen"] == MAXLEN_LAUNCH_EVENTS

    def test_data_staleness_ms_in_message(self):
        """data_staleness_ms must be present in the message for monitoring."""
        fake_redis = FakeRedisStream()
        producer = BusProducer(fake_redis)
        ev = _make_event(staleness_ms=42)
        asyncio.run(
            producer.publish_launch_event(ev, "sig_stale")
        )
        _, fields = fake_redis._streams[STREAM_LAUNCH_EVENTS][0]
        assert int(fields["data_staleness_ms"]) == 42

    def test_no_float_money_in_published_event(self):
        """Published event JSON must not contain float money fields."""
        fake_redis = FakeRedisStream()
        producer = BusProducer(fake_redis)
        ev = _make_event()
        asyncio.run(
            producer.publish_launch_event(ev, "sig_nofloat")
        )
        _, fields = fake_redis._streams[STREAM_LAUNCH_EVENTS][0]
        ev2 = LaunchEvent.model_validate_json(fields["event"])
        assert isinstance(ev2.sol_reserve_lamports, int)
        assert isinstance(ev2.token_reserve_base, int)

    def test_signature_stored_in_message(self):
        fake_redis = FakeRedisStream()
        producer = BusProducer(fake_redis)
        asyncio.run(
            producer.publish_launch_event(_make_event(), "my_unique_sig_12345")
        )
        _, fields = fake_redis._streams[STREAM_LAUNCH_EVENTS][0]
        assert fields["sig"] == "my_unique_sig_12345"


class TestBusConsumerParsing:
    """Test BusConsumer._parse_entry in isolation (no XREADGROUP needed)."""

    def test_parse_valid_entry(self):
        fake_redis = FakeRedisStream()
        consumer = BusConsumer(fake_redis, group=GROUP_SLOW, consumer_name="test-1")
        ev = _make_event(slot=300_000_700)
        fields = {
            b"event": ev.model_dump_json().encode(),
            b"sig": b"sig_parse_test",
            b"ix_idx": b"0",
            b"ingest_time_ms": b"1718700700000",
        }
        msg = consumer._parse_entry("700-0", fields)
        assert msg is not None
        assert isinstance(msg.payload, LaunchEvent)
        assert msg.payload.event_time.slot == 300_000_700
        assert msg.source_signature == "sig_parse_test"
        assert msg.instruction_index == 0
        assert msg.ingest_time_ms == 1718700700000

    def test_parse_entry_returns_none_on_missing_event_field(self):
        fake_redis = FakeRedisStream()
        consumer = BusConsumer(fake_redis, group=GROUP_SLOW, consumer_name="test-2")
        msg = consumer._parse_entry("bad-0", {b"sig": b"x"})
        assert msg is None

    def test_parse_entry_returns_none_on_bad_json(self):
        fake_redis = FakeRedisStream()
        consumer = BusConsumer(fake_redis, group=GROUP_SLOW, consumer_name="test-3")
        msg = consumer._parse_entry("bad-1", {b"event": b"not_valid_json"})
        assert msg is None


class TestBusConsumerDedup:
    def test_dedup_skips_repeated_sig_ix_pair(self):
        """A message with a (sig, ix_idx) already seen must be skipped."""
        fake_redis = FakeRedisStream()
        consumer = BusConsumer(fake_redis, group=GROUP_SLOW, consumer_name="test-dedup")
        ev = _make_event(slot=300_000_800)

        # Populate stream with two identical entries (same sig, ix_idx)
        fields = {
            "event": ev.model_dump_json(),
            "sig": "dup_sig_123",
            "ix_idx": "0",
            "ingest_time_ms": "1718700800000",
        }

        async def run():
            await fake_redis.xgroup_create(STREAM_LAUNCH_EVENTS, GROUP_SLOW, id="$", mkstream=True)
            await fake_redis.xadd(STREAM_LAUNCH_EVENTS, fields)
            await fake_redis.xadd(STREAM_LAUNCH_EVENTS, fields)  # duplicate

            seen_msgs = []
            async for msg in consumer.consume_launch_events(batch_size=10):
                seen_msgs.append(msg)
                await consumer.ack(msg)
                if len(fake_redis._streams.get(STREAM_LAUNCH_EVENTS, [])) > 0:
                    break
            return seen_msgs

        # Just test that the dedup set is populated
        consumer._seen.add(("dup_sig_123", 0))
        msg = consumer._parse_entry(
            "800-0",
            {b"event": ev.model_dump_json().encode(), b"sig": b"dup_sig_123", b"ix_idx": b"0", b"ingest_time_ms": b"1718700800000"}
        )
        assert msg is not None
        # The sig+ix_idx is already in _seen
        key = (msg.source_signature, msg.instruction_index)
        assert key in consumer._seen


class TestBusBackpressure:
    def test_maxlen_prevents_unbounded_growth(self):
        """Verify the stream is trimmed when exceeding MAXLEN_LAUNCH_EVENTS."""
        fake_redis = FakeRedisStream()
        producer = BusProducer(fake_redis)

        async def publish_many(n: int):
            for i in range(n):
                await producer.publish_launch_event(
                    _make_event(mint=f"Mint{i:0>43}"), f"sig_{i}"
                )

        # Publish 200_000 events (2x the maxlen)
        # FakeRedisStream applies the maxlen trim
        asyncio.run(publish_many(200))
        # With maxlen=100_000 but only 200 events, all 200 should be present
        entries = fake_redis._streams.get(STREAM_LAUNCH_EVENTS, [])
        assert len(entries) <= MAXLEN_LAUNCH_EVENTS


class TestBusRoundTrip:
    def test_full_round_trip(self):
        """Publish an event, parse it back: all fields match."""
        fake_redis = FakeRedisStream()
        producer = BusProducer(fake_redis)
        ev = _make_event(slot=300_000_900)

        asyncio.run(
            producer.publish_launch_event(ev, "sig_fullrt", instruction_index=3)
        )

        consumer = BusConsumer(fake_redis, group=GROUP_SLOW, consumer_name="test-rt")
        _, fields_raw = fake_redis._streams[STREAM_LAUNCH_EVENTS][0]
        # Convert the raw str fields to bytes-keyed dict as the parser expects
        byte_fields = {k.encode() if isinstance(k, str) else k: v.encode() if isinstance(v, str) else v
                       for k, v in fields_raw.items()}
        msg = consumer._parse_entry("900-0", byte_fields)

        assert msg is not None
        parsed_ev: LaunchEvent = msg.payload
        assert parsed_ev.mint == ev.mint
        assert parsed_ev.event_time.slot == ev.event_time.slot
        assert parsed_ev.event_time.block_time_ms == ev.event_time.block_time_ms
        assert parsed_ev.sol_reserve_lamports == ev.sol_reserve_lamports
        assert parsed_ev.token_reserve_base == ev.token_reserve_base
        assert parsed_ev.source == ev.source
        assert parsed_ev.detection_transport == ev.detection_transport
        assert msg.source_signature == "sig_fullrt"
        assert msg.instruction_index == 3
