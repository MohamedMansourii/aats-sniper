"""Tests for T-302: smart-money / copy-trade wallet stream.

VERIFICATION REQUIREMENTS (from task brief):
  1. The stream records lag HONESTLY (entry_lag_slots, observation_lag_ms >= 0;
     we are always behind their fill).
  2. smart_wallets_in is a COUNT, not a trigger (it is an integer; the module
     never calls an EntryIntent factory or anything that could submit a trade).
  3. DISABLED BY DEFAULT (SmartWalletConfig.enabled defaults to False; the
     stream yields nothing when disabled).
  4. NEVER a blind mirror (copy-trade = selectivity filter only; no trade issued).
  5. Point-in-time correctness: their_fill_block_time_ms is the event_time anchor;
     wall-clock never substituted for a missing fill_block_time.
  6. Money integers: sol_amount_lamports and token_amount_base are int (never float).
  7. Idempotent / order-tolerant: duplicate and out-of-order events produce the
     same smart_wallets_in count as in-order delivery.
  8. Max 20 wallets: configs with > 20 wallets are silently truncated.
  9. Injectable / offline: tests run with ReplaySmartWalletBackend, no real RPC.
 10. Bus publisher encodes money as strings (integer base units on the wire).
"""

from __future__ import annotations

import asyncio
import time
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock

import pytest

from aats.ingestion.smart_money import (
    MAX_TRACKED_WALLETS,
    STREAM_SMART_MONEY,
    RawWalletUpdate,
    ReplaySmartWalletBackend,
    SmartMoneyEvent,
    SmartWalletBusPublisher,
    SmartWalletConfig,
    SmartWalletDecoder,
    SmartWalletStream,
    count_smart_wallet_entry_lag_slots,
    count_smart_wallets_in,
)

# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

WALLET_A = "SmartWalletAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
WALLET_B = "SmartWalletBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"
WALLET_C = "SmartWalletCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC"
MINT_X = "MintXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX"
MINT_Y = "MintYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYYY"

LAUNCH_SLOT = 300_000_000
BLOCK_TIME_MS = 1_718_700_000_000  # on-chain block time (ms)


def make_raw_update(
    wallet: str = WALLET_A,
    mint: str = MINT_X,
    fill_slot: int = LAUNCH_SLOT,
    fill_block_time_ms: int | None = BLOCK_TIME_MS,
    observation_slot: int | None = None,
    observation_wall_ms: int | None = None,
    is_buy: bool = True,
    sol_delta_lamports: int = 100_000_000,
    token_delta_base: int = 1_000_000_000,
    source: str = "replay",
) -> RawWalletUpdate:
    """Helper: create a RawWalletUpdate with sensible defaults."""
    if observation_slot is None:
        observation_slot = fill_slot  # same slot (no lag — degenerate case)
    if observation_wall_ms is None:
        observation_wall_ms = fill_block_time_ms or (int(time.time() * 1000))
    return RawWalletUpdate(
        wallet=wallet,
        mint=mint,
        fill_slot=fill_slot,
        fill_block_time_ms=fill_block_time_ms,
        observation_slot=observation_slot,
        observation_wall_ms=observation_wall_ms,
        is_buy=is_buy,
        sol_delta_lamports=sol_delta_lamports,
        token_delta_base=token_delta_base,
        source=source,
    )


def collect_events(
    stream: SmartWalletStream,
    last_slot: int = 0,
) -> list[SmartMoneyEvent]:
    """Run the async stream synchronously in a test event loop."""
    async def _collect() -> list[SmartMoneyEvent]:
        evs: list[SmartMoneyEvent] = []
        async for ev in stream.subscribe(last_slot=last_slot):
            evs.append(ev)
        return evs
    return asyncio.run(_collect())


# ---------------------------------------------------------------------------
# 1. Config: disabled by default
# ---------------------------------------------------------------------------

class TestConfigDisabledByDefault:
    """AC: stream is DISABLED by default (OQ-002 / EH-005)."""

    def test_default_config_disabled(self) -> None:
        cfg = SmartWalletConfig()
        assert cfg.enabled is False, "SmartWalletConfig must default to enabled=False"

    def test_disabled_stream_yields_nothing(self) -> None:
        """A disabled stream yields no events, even with wallets and updates."""
        updates = [make_raw_update()]
        backend = ReplaySmartWalletBackend(updates)
        cfg = SmartWalletConfig(
            enabled=False,
            wallet_addresses=[WALLET_A],
        )
        stream = SmartWalletStream(config=cfg, backend=backend)
        events = collect_events(stream)
        assert events == [], (
            "Disabled stream must yield ZERO events. "
            "Operator must explicitly set enabled=True to receive events."
        )

    def test_disabled_stream_yields_nothing_no_backend(self) -> None:
        """Disabled stream with no backend: still yields nothing, no exception."""
        cfg = SmartWalletConfig(enabled=False, wallet_addresses=[WALLET_A])
        stream = SmartWalletStream(config=cfg, backend=None)
        events = collect_events(stream)
        assert events == []

    def test_enabled_with_no_backend_yields_nothing(self) -> None:
        """Enabled stream with no backend: yields nothing, no exception."""
        cfg = SmartWalletConfig(enabled=True, wallet_addresses=[WALLET_A])
        stream = SmartWalletStream(config=cfg, backend=None)
        events = collect_events(stream)
        assert events == []

    def test_enabled_with_empty_wallet_list_yields_nothing(self) -> None:
        """Enabled stream with zero wallets: yields nothing."""
        updates = [make_raw_update()]
        backend = ReplaySmartWalletBackend(updates)
        cfg = SmartWalletConfig(enabled=True, wallet_addresses=[])
        stream = SmartWalletStream(config=cfg, backend=backend)
        events = collect_events(stream)
        assert events == []


# ---------------------------------------------------------------------------
# 2. Config: max 20 wallets
# ---------------------------------------------------------------------------

class TestMaxWallets:
    """AC: configuration truncates lists > 20 wallets (OQ-002)."""

    def test_exactly_20_wallets_allowed(self) -> None:
        wallets = [f"Wallet{i:04d}{'X' * 38}" for i in range(20)]
        cfg = SmartWalletConfig(enabled=True, wallet_addresses=wallets)
        assert len(cfg.wallet_addresses) == 20

    def test_21_wallets_truncated_to_20(self) -> None:
        wallets = [f"Wallet{i:04d}{'X' * 38}" for i in range(21)]
        cfg = SmartWalletConfig(enabled=True, wallet_addresses=wallets)
        assert len(cfg.wallet_addresses) == MAX_TRACKED_WALLETS, (
            f"Expected {MAX_TRACKED_WALLETS} wallets after truncation, "
            f"got {len(cfg.wallet_addresses)}"
        )

    def test_30_wallets_truncated_to_20(self) -> None:
        wallets = [f"Wallet{i:04d}{'X' * 38}" for i in range(30)]
        cfg = SmartWalletConfig(enabled=True, wallet_addresses=wallets)
        assert len(cfg.wallet_addresses) == MAX_TRACKED_WALLETS

    def test_zero_wallets_is_valid(self) -> None:
        cfg = SmartWalletConfig(enabled=True, wallet_addresses=[])
        assert cfg.wallet_addresses == []

    def test_wallet_set_is_frozenset(self) -> None:
        cfg = SmartWalletConfig(enabled=True, wallet_addresses=[WALLET_A, WALLET_B])
        assert isinstance(cfg.wallet_set, frozenset)
        assert WALLET_A in cfg.wallet_set
        assert WALLET_B in cfg.wallet_set

    def test_max_wallets_constant(self) -> None:
        assert MAX_TRACKED_WALLETS == 20, "Hard limit must be exactly 20 (OQ-002)"


# ---------------------------------------------------------------------------
# 3. Lag accounting — honest
# ---------------------------------------------------------------------------

class TestLagAccounting:
    """AC: the stream records lag honestly; we are always behind their fill."""

    def test_zero_lag_when_same_slot(self) -> None:
        """When our observation slot == their fill slot, lag == 0."""
        update = make_raw_update(
            fill_slot=LAUNCH_SLOT,
            observation_slot=LAUNCH_SLOT,
            fill_block_time_ms=BLOCK_TIME_MS,
            observation_wall_ms=BLOCK_TIME_MS,
        )
        decoder = SmartWalletDecoder()
        ev = decoder.decode(update)
        assert ev is not None
        assert ev.entry_lag_slots == 0
        assert ev.observation_lag_ms == 0

    def test_positive_slot_lag(self) -> None:
        """We observed the update 5 slots after the tracked wallet filled."""
        update = make_raw_update(
            fill_slot=LAUNCH_SLOT,
            observation_slot=LAUNCH_SLOT + 5,
            fill_block_time_ms=BLOCK_TIME_MS,
            observation_wall_ms=BLOCK_TIME_MS + 2_000,  # 2s later
        )
        decoder = SmartWalletDecoder()
        ev = decoder.decode(update)
        assert ev is not None
        assert ev.entry_lag_slots == 5, (
            f"entry_lag_slots should be 5 (our slot - their slot), got {ev.entry_lag_slots}"
        )
        assert ev.observation_lag_ms == 2_000, (
            f"observation_lag_ms should be 2000ms, got {ev.observation_lag_ms}"
        )

    def test_entry_lag_slots_never_negative(self) -> None:
        """entry_lag_slots is always >= 0."""
        # Degenerate case: our_event_slot == their_fill_slot (same slot)
        update = make_raw_update(
            fill_slot=LAUNCH_SLOT,
            observation_slot=LAUNCH_SLOT,
        )
        decoder = SmartWalletDecoder()
        ev = decoder.decode(update)
        assert ev is not None
        assert ev.entry_lag_slots >= 0

    def test_lag_fields_identity(self) -> None:
        """entry_lag_slots == our_event_slot - their_fill_slot."""
        fill_slot = 300_000_000
        obs_slot = 300_000_007
        update = make_raw_update(
            fill_slot=fill_slot,
            observation_slot=obs_slot,
            fill_block_time_ms=BLOCK_TIME_MS,
            observation_wall_ms=BLOCK_TIME_MS + 2_800,
        )
        decoder = SmartWalletDecoder()
        ev = decoder.decode(update)
        assert ev is not None
        assert ev.entry_lag_slots == obs_slot - fill_slot
        assert ev.our_event_slot == obs_slot
        assert ev.their_fill_slot == fill_slot

    def test_observation_lag_ms_is_data_staleness_alias(self) -> None:
        """data_staleness_ms == observation_lag_ms (FR-057 monitoring field)."""
        update = make_raw_update(
            fill_block_time_ms=BLOCK_TIME_MS,
            observation_wall_ms=BLOCK_TIME_MS + 1_500,
        )
        decoder = SmartWalletDecoder()
        ev = decoder.decode(update)
        assert ev is not None
        assert ev.data_staleness_ms == ev.observation_lag_ms

    def test_honest_lag_in_stream(self) -> None:
        """Full stream path: lag fields are present and correct on emitted events."""
        fill_slot = LAUNCH_SLOT
        obs_slot = LAUNCH_SLOT + 3
        fill_ms = BLOCK_TIME_MS
        obs_ms = BLOCK_TIME_MS + 1_200

        updates = [make_raw_update(
            fill_slot=fill_slot,
            observation_slot=obs_slot,
            fill_block_time_ms=fill_ms,
            observation_wall_ms=obs_ms,
        )]
        backend = ReplaySmartWalletBackend(updates)
        cfg = SmartWalletConfig(enabled=True, wallet_addresses=[WALLET_A])
        stream = SmartWalletStream(config=cfg, backend=backend)
        events = collect_events(stream)

        assert len(events) == 1
        ev = events[0]
        assert ev.entry_lag_slots == 3
        assert ev.observation_lag_ms == 1_200
        assert ev.their_fill_slot == fill_slot
        assert ev.our_event_slot == obs_slot


# ---------------------------------------------------------------------------
# 4. SmartMoneyEvent invariants
# ---------------------------------------------------------------------------

class TestSmartMoneyEventInvariants:
    """AC: SmartMoneyEvent enforces invariants at construction."""

    def test_our_slot_less_than_their_slot_raises(self) -> None:
        """Constructing an event where our slot < their slot must raise."""
        with pytest.raises(ValueError, match="our_event_slot=.*their_fill_slot"):
            SmartMoneyEvent(
                wallet=WALLET_A,
                mint=MINT_X,
                their_fill_slot=300_000_010,
                our_event_slot=300_000_005,  # < their fill — impossible
                entry_lag_slots=5,
                their_fill_block_time_ms=BLOCK_TIME_MS,
                our_observation_ms=BLOCK_TIME_MS + 2_000,
                observation_lag_ms=2_000,
                is_buy=True,
                sol_amount_lamports=100_000_000,
                token_amount_base=1_000_000_000,
                data_staleness_ms=2_000,
                source="replay",
            )

    def test_wrong_entry_lag_slots_raises(self) -> None:
        """entry_lag_slots must equal our_event_slot - their_fill_slot."""
        with pytest.raises(ValueError, match="entry_lag_slots"):
            SmartMoneyEvent(
                wallet=WALLET_A,
                mint=MINT_X,
                their_fill_slot=LAUNCH_SLOT,
                our_event_slot=LAUNCH_SLOT + 5,
                entry_lag_slots=99,  # wrong: should be 5
                their_fill_block_time_ms=BLOCK_TIME_MS,
                our_observation_ms=BLOCK_TIME_MS + 2_000,
                observation_lag_ms=2_000,
                is_buy=True,
                sol_amount_lamports=100_000_000,
                token_amount_base=1_000_000_000,
                data_staleness_ms=2_000,
                source="replay",
            )

    def test_negative_observation_lag_raises(self) -> None:
        with pytest.raises(ValueError, match="observation_lag_ms"):
            SmartMoneyEvent(
                wallet=WALLET_A,
                mint=MINT_X,
                their_fill_slot=LAUNCH_SLOT,
                our_event_slot=LAUNCH_SLOT,
                entry_lag_slots=0,
                their_fill_block_time_ms=BLOCK_TIME_MS,
                our_observation_ms=BLOCK_TIME_MS,
                observation_lag_ms=-1,  # invalid
                is_buy=True,
                sol_amount_lamports=100_000_000,
                token_amount_base=1_000_000_000,
                data_staleness_ms=0,
                source="replay",
            )

    def test_negative_sol_amount_raises(self) -> None:
        with pytest.raises(ValueError, match="sol_amount_lamports"):
            SmartMoneyEvent(
                wallet=WALLET_A,
                mint=MINT_X,
                their_fill_slot=LAUNCH_SLOT,
                our_event_slot=LAUNCH_SLOT,
                entry_lag_slots=0,
                their_fill_block_time_ms=BLOCK_TIME_MS,
                our_observation_ms=BLOCK_TIME_MS,
                observation_lag_ms=0,
                is_buy=True,
                sol_amount_lamports=-1,  # invalid
                token_amount_base=1_000_000_000,
                data_staleness_ms=0,
                source="replay",
            )

    def test_valid_event_construction(self) -> None:
        """Valid SmartMoneyEvent constructs without raising."""
        ev = SmartMoneyEvent(
            wallet=WALLET_A,
            mint=MINT_X,
            their_fill_slot=LAUNCH_SLOT,
            our_event_slot=LAUNCH_SLOT + 3,
            entry_lag_slots=3,
            their_fill_block_time_ms=BLOCK_TIME_MS,
            our_observation_ms=BLOCK_TIME_MS + 1_200,
            observation_lag_ms=1_200,
            is_buy=True,
            sol_amount_lamports=100_000_000,
            token_amount_base=1_000_000_000,
            data_staleness_ms=1_200,
            source="replay",
        )
        assert ev.entry_lag_slots == 3
        assert ev.sol_amount_lamports == 100_000_000


# ---------------------------------------------------------------------------
# 5. Decoder: no wall-clock substitution (point-in-time law)
# ---------------------------------------------------------------------------

class TestDecoderPointInTime:
    """AC: decoder never substitutes wall-clock for missing fill_block_time_ms."""

    def test_none_block_time_returns_none(self) -> None:
        """Pre-confirmation update (no block_time) must return None, not an event."""
        update = make_raw_update(fill_block_time_ms=None)
        decoder = SmartWalletDecoder()
        ev = decoder.decode(update)
        assert ev is None, (
            "Decoder must return None when fill_block_time_ms is None. "
            "Wall-clock must NEVER be substituted for the on-chain block time."
        )

    def test_zero_block_time_returns_none(self) -> None:
        """Zero block_time (pre-confirmation) must return None."""
        update = make_raw_update(fill_block_time_ms=0)
        decoder = SmartWalletDecoder()
        ev = decoder.decode(update)
        assert ev is None, (
            "Decoder must return None when fill_block_time_ms == 0."
        )

    def test_valid_block_time_decodes(self) -> None:
        """Valid block_time produces a SmartMoneyEvent."""
        update = make_raw_update(fill_block_time_ms=BLOCK_TIME_MS)
        decoder = SmartWalletDecoder()
        ev = decoder.decode(update)
        assert ev is not None
        assert ev.their_fill_block_time_ms == BLOCK_TIME_MS

    def test_event_time_anchor_is_their_fill_block_time(self) -> None:
        """The on-chain block time anchors the event, not the wall-clock."""
        fill_ms = 1_718_700_000_000
        update = make_raw_update(
            fill_block_time_ms=fill_ms,
            observation_wall_ms=fill_ms + 9_999,  # 10s later
        )
        decoder = SmartWalletDecoder()
        ev = decoder.decode(update)
        assert ev is not None
        # event_time anchor = their fill block time
        assert ev.their_fill_block_time_ms == fill_ms
        # wall-clock is for monitoring only
        assert ev.our_observation_ms == fill_ms + 9_999

    def test_stream_skips_pending_events(self) -> None:
        """Stream skips events with no block_time (pending) and counts them as skipped."""
        updates = [
            make_raw_update(fill_block_time_ms=None, mint=MINT_X),    # pending — skip
            make_raw_update(fill_block_time_ms=BLOCK_TIME_MS, mint=MINT_Y),  # confirmed — emit
        ]
        backend = ReplaySmartWalletBackend(updates)
        cfg = SmartWalletConfig(enabled=True, wallet_addresses=[WALLET_A])
        stream = SmartWalletStream(config=cfg, backend=backend)
        events = collect_events(stream)

        # Only the confirmed event should be emitted
        assert len(events) == 1
        assert events[0].mint == MINT_Y
        assert stream.stats.events_skipped == 1
        assert stream.stats.events_decoded == 1


# ---------------------------------------------------------------------------
# 6. Money type safety
# ---------------------------------------------------------------------------

class TestMoneyTypes:
    """AC: sol_amount_lamports and token_amount_base are int, never float."""

    def test_sol_amount_is_int(self) -> None:
        update = make_raw_update(sol_delta_lamports=200_000_000)
        decoder = SmartWalletDecoder()
        ev = decoder.decode(update)
        assert ev is not None
        assert isinstance(ev.sol_amount_lamports, int), (
            f"sol_amount_lamports must be int, got {type(ev.sol_amount_lamports)}"
        )
        assert ev.sol_amount_lamports == 200_000_000

    def test_token_amount_is_int(self) -> None:
        update = make_raw_update(token_delta_base=5_000_000_000)
        decoder = SmartWalletDecoder()
        ev = decoder.decode(update)
        assert ev is not None
        assert isinstance(ev.token_amount_base, int), (
            f"token_amount_base must be int, got {type(ev.token_amount_base)}"
        )
        assert ev.token_amount_base == 5_000_000_000

    def test_float_sol_amount_in_raw_update_is_rejected(self) -> None:
        """RawWalletUpdate with a float sol_delta_lamports must be rejected by the decoder.

        Python dataclasses do not enforce type annotations at runtime, so the
        SmartWalletDecoder contains an explicit isinstance guard that raises
        TypeError when a non-int (e.g. float) slips through.
        """
        update = RawWalletUpdate(
            wallet=WALLET_A,
            mint=MINT_X,
            fill_slot=LAUNCH_SLOT,
            fill_block_time_ms=BLOCK_TIME_MS,
            observation_slot=LAUNCH_SLOT,
            observation_wall_ms=BLOCK_TIME_MS,
            is_buy=True,
            sol_delta_lamports=0.1,  # type: ignore[arg-type] — should be int
            token_delta_base=1_000_000_000,
        )
        decoder = SmartWalletDecoder()
        with pytest.raises(TypeError, match="sol_delta_lamports must be int"):
            decoder.decode(update)

    def test_bus_publisher_encodes_lamports_as_string(self) -> None:
        """Bus publisher encodes lamports as strings on the wire, never floats."""
        ev = SmartMoneyEvent(
            wallet=WALLET_A,
            mint=MINT_X,
            their_fill_slot=LAUNCH_SLOT,
            our_event_slot=LAUNCH_SLOT + 2,
            entry_lag_slots=2,
            their_fill_block_time_ms=BLOCK_TIME_MS,
            our_observation_ms=BLOCK_TIME_MS + 800,
            observation_lag_ms=800,
            is_buy=True,
            sol_amount_lamports=150_000_000,
            token_amount_base=2_000_000_000,
            data_staleness_ms=800,
            source="replay",
        )

        # Capture what the publisher sends to Redis
        captured: list[dict[str, Any]] = []
        redis_mock = AsyncMock()
        async def fake_xadd(stream: str, payload: dict, **kwargs: Any) -> str:
            captured.append(payload)
            return "1234567890-0"
        redis_mock.xadd.side_effect = fake_xadd

        publisher = SmartWalletBusPublisher(redis_mock)
        asyncio.run(publisher.publish(ev))

        assert len(captured) == 1
        payload = captured[0]

        # Money fields must be strings on the wire (api-contracts.md §1 money rule)
        assert payload["sol_amount_lamports"] == "150000000", (
            "sol_amount_lamports must be encoded as string on the wire"
        )
        assert payload["token_amount_base"] == "2000000000", (
            "token_amount_base must be encoded as string on the wire"
        )
        # Verify the stream name
        stream_name = redis_mock.xadd.call_args[0][0]
        assert stream_name == STREAM_SMART_MONEY


# ---------------------------------------------------------------------------
# 7. smart_wallets_in is a count, not a trigger
# ---------------------------------------------------------------------------

class TestSmartWalletsInCount:
    """AC: smart_wallets_in is a count (int), never a trigger or trade signal."""

    def _make_events(
        self,
        wallets_and_slots: list[tuple[str, int, bool]],
        mint: str = MINT_X,
    ) -> list[SmartMoneyEvent]:
        """Helper: build SmartMoneyEvents from (wallet, fill_slot, is_buy) tuples."""
        events = []
        for wallet, fill_slot, is_buy in wallets_and_slots:
            events.append(SmartMoneyEvent(
                wallet=wallet,
                mint=mint,
                their_fill_slot=fill_slot,
                our_event_slot=fill_slot + 2,
                entry_lag_slots=2,
                their_fill_block_time_ms=BLOCK_TIME_MS + (fill_slot - LAUNCH_SLOT) * 400,
                our_observation_ms=BLOCK_TIME_MS + (fill_slot - LAUNCH_SLOT) * 400 + 800,
                observation_lag_ms=800,
                is_buy=is_buy,
                sol_amount_lamports=100_000_000,
                token_amount_base=1_000_000_000,
                data_staleness_ms=800,
                source="replay",
            ))
        return events

    def test_count_returns_int(self) -> None:
        events = self._make_events([(WALLET_A, LAUNCH_SLOT, True)])
        count = count_smart_wallets_in(events, MINT_X, LAUNCH_SLOT, first_k_slots=10)
        assert isinstance(count, int), f"smart_wallets_in must be int, got {type(count)}"

    def test_zero_when_no_buys(self) -> None:
        events = self._make_events([(WALLET_A, LAUNCH_SLOT, False)])  # sell, not buy
        count = count_smart_wallets_in(events, MINT_X, LAUNCH_SLOT, first_k_slots=10)
        assert count == 0, "Sells must not count toward smart_wallets_in"

    def test_zero_when_empty_events(self) -> None:
        count = count_smart_wallets_in([], MINT_X, LAUNCH_SLOT, first_k_slots=10)
        assert count == 0

    def test_counts_unique_wallets(self) -> None:
        """Same wallet buying twice counts as 1, not 2."""
        events = self._make_events([
            (WALLET_A, LAUNCH_SLOT, True),
            (WALLET_A, LAUNCH_SLOT + 2, True),  # same wallet, second buy
        ])
        count = count_smart_wallets_in(events, MINT_X, LAUNCH_SLOT, first_k_slots=10)
        assert count == 1, f"Duplicate wallet should count as 1, got {count}"

    def test_counts_multiple_wallets(self) -> None:
        events = self._make_events([
            (WALLET_A, LAUNCH_SLOT + 1, True),
            (WALLET_B, LAUNCH_SLOT + 3, True),
            (WALLET_C, LAUNCH_SLOT + 5, True),
        ])
        count = count_smart_wallets_in(events, MINT_X, LAUNCH_SLOT, first_k_slots=10)
        assert count == 3

    def test_filters_by_mint(self) -> None:
        """Events for MINT_Y must not count toward MINT_X."""
        events = self._make_events([(WALLET_A, LAUNCH_SLOT, True)], mint=MINT_Y)
        count = count_smart_wallets_in(events, MINT_X, LAUNCH_SLOT, first_k_slots=10)
        assert count == 0, "Events for a different mint must not be counted"

    def test_filters_by_k_slot_window(self) -> None:
        """Buys outside the [launch_slot, launch_slot + K] window are excluded."""
        events = self._make_events([
            (WALLET_A, LAUNCH_SLOT - 1, True),        # BEFORE launch — excluded
            (WALLET_B, LAUNCH_SLOT + 5, True),        # within window — included
            (WALLET_C, LAUNCH_SLOT + 11, True),       # AFTER window (K=10) — excluded
        ])
        count = count_smart_wallets_in(events, MINT_X, LAUNCH_SLOT, first_k_slots=10)
        assert count == 1, (
            f"Only WALLET_B (slot within window) should count; got {count}"
        )

    def test_boundary_slots_inclusive(self) -> None:
        """Buys at exactly launch_slot and launch_slot + K are INCLUDED."""
        events = self._make_events([
            (WALLET_A, LAUNCH_SLOT, True),              # exactly launch_slot — included
            (WALLET_B, LAUNCH_SLOT + 10, True),         # exactly launch_slot + K — included
        ])
        count = count_smart_wallets_in(events, MINT_X, LAUNCH_SLOT, first_k_slots=10)
        assert count == 2, f"Boundary slots must be inclusive, got {count}"

    def test_count_is_never_a_float(self) -> None:
        """The count function returns int, never Decimal, never float."""
        events = self._make_events([(WALLET_A, LAUNCH_SLOT, True)])
        count = count_smart_wallets_in(events, MINT_X, LAUNCH_SLOT, first_k_slots=10)
        assert isinstance(count, int)
        assert not isinstance(count, float)
        assert not isinstance(count, Decimal)

    def test_entry_lag_slots_none_when_no_wallets(self) -> None:
        """count_smart_wallet_entry_lag_slots returns None when no wallets entered."""
        result = count_smart_wallet_entry_lag_slots(
            [], MINT_X, LAUNCH_SLOT, first_k_slots=10, our_decision_slot=LAUNCH_SLOT + 5
        )
        assert result is None

    def test_entry_lag_slots_is_int_when_wallets_present(self) -> None:
        events = self._make_events([(WALLET_A, LAUNCH_SLOT + 2, True)])
        result = count_smart_wallet_entry_lag_slots(
            events, MINT_X, LAUNCH_SLOT, first_k_slots=10,
            our_decision_slot=LAUNCH_SLOT + 7,
        )
        assert isinstance(result, int), f"entry_lag_slots must be int, got {type(result)}"
        assert result == 5, (
            f"our_decision_slot={LAUNCH_SLOT+7} - earliest_fill_slot={LAUNCH_SLOT+2} = 5, "
            f"got {result}"
        )

    def test_entry_lag_slots_never_negative(self) -> None:
        """Lag must be >= 0 even if decision slot == fill slot."""
        events = self._make_events([(WALLET_A, LAUNCH_SLOT + 3, True)])
        result = count_smart_wallet_entry_lag_slots(
            events, MINT_X, LAUNCH_SLOT, first_k_slots=10,
            our_decision_slot=LAUNCH_SLOT + 3,  # same slot as fill
        )
        assert result is not None
        assert result >= 0

    def test_uses_earliest_fill_slot(self) -> None:
        """Lag is our slot - EARLIEST fill slot (not the latest)."""
        events = self._make_events([
            (WALLET_A, LAUNCH_SLOT + 5, True),  # later
            (WALLET_B, LAUNCH_SLOT + 2, True),  # EARLIEST
        ])
        result = count_smart_wallet_entry_lag_slots(
            events, MINT_X, LAUNCH_SLOT, first_k_slots=10,
            our_decision_slot=LAUNCH_SLOT + 8,
        )
        # Should be 8 - 2 = 6 (using earliest slot 2, not 5)
        assert result == 6, f"Expected lag=6 (using earliest fill slot), got {result}"


# ---------------------------------------------------------------------------
# 8. Idempotency and order-tolerance
# ---------------------------------------------------------------------------

class TestIdempotencyAndOrderTolerance:
    """AC: duplicate and out-of-order events produce identical smart_wallets_in count."""

    def _run_stream(self, updates: list[RawWalletUpdate]) -> list[SmartMoneyEvent]:
        backend = ReplaySmartWalletBackend(updates)
        cfg = SmartWalletConfig(
            enabled=True,
            wallet_addresses=[WALLET_A, WALLET_B, WALLET_C],
        )
        stream = SmartWalletStream(config=cfg, backend=backend)
        return collect_events(stream)

    def test_duplicate_events_produce_same_count(self) -> None:
        """Sending the same update twice yields one SmartMoneyEvent (dedup)."""
        update = make_raw_update(wallet=WALLET_A, fill_slot=LAUNCH_SLOT, is_buy=True)
        events = self._run_stream([update, update])  # duplicate

        assert len(events) == 1, (
            f"Duplicate events should be deduped to 1, got {len(events)}"
        )
        count = count_smart_wallets_in(events, MINT_X, LAUNCH_SLOT, first_k_slots=10)
        assert count == 1

    def test_out_of_order_events_same_count_as_in_order(self) -> None:
        """Out-of-order delivery must produce the same smart_wallets_in as in-order."""
        updates_in_order = [
            make_raw_update(wallet=WALLET_A, fill_slot=LAUNCH_SLOT + 1, is_buy=True),
            make_raw_update(wallet=WALLET_B, fill_slot=LAUNCH_SLOT + 3, is_buy=True),
        ]
        updates_out_of_order = [
            make_raw_update(wallet=WALLET_B, fill_slot=LAUNCH_SLOT + 3, is_buy=True),
            make_raw_update(wallet=WALLET_A, fill_slot=LAUNCH_SLOT + 1, is_buy=True),
        ]

        events_in_order = self._run_stream(updates_in_order)
        events_out_of_order = self._run_stream(updates_out_of_order)

        count_in = count_smart_wallets_in(events_in_order, MINT_X, LAUNCH_SLOT, 10)
        count_oo = count_smart_wallets_in(events_out_of_order, MINT_X, LAUNCH_SLOT, 10)
        assert count_in == count_oo == 2, (
            f"In-order count={count_in}, out-of-order count={count_oo}; must both be 2"
        )

    def test_three_duplicates_of_same_event_deduped(self) -> None:
        update = make_raw_update(wallet=WALLET_A, fill_slot=LAUNCH_SLOT + 2, is_buy=True)
        events = self._run_stream([update, update, update])
        assert len(events) == 1, "Three identical events must dedup to 1"

    def test_same_slot_different_wallets_not_deduped(self) -> None:
        """Different wallets buying the same slot should each yield an event."""
        updates = [
            make_raw_update(wallet=WALLET_A, fill_slot=LAUNCH_SLOT, is_buy=True),
            make_raw_update(wallet=WALLET_B, fill_slot=LAUNCH_SLOT, is_buy=True),
        ]
        events = self._run_stream(updates)
        assert len(events) == 2, (
            "Two different wallets at the same slot must each emit one event"
        )

    def test_replay_from_slot_skips_earlier_slots(self) -> None:
        """resume-from-slot: events before last_slot are skipped."""
        updates = [
            make_raw_update(wallet=WALLET_A, fill_slot=LAUNCH_SLOT - 1),  # before resume slot
            make_raw_update(wallet=WALLET_B, fill_slot=LAUNCH_SLOT + 5),  # after
        ]
        backend = ReplaySmartWalletBackend(updates)
        cfg = SmartWalletConfig(enabled=True, wallet_addresses=[WALLET_A, WALLET_B])
        stream = SmartWalletStream(config=cfg, backend=backend)
        events = collect_events(stream, last_slot=LAUNCH_SLOT)

        # Only WALLET_B (slot LAUNCH_SLOT + 5 >= LAUNCH_SLOT) should be emitted
        # Note: ReplaySmartWalletBackend uses observation_slot for resume filtering,
        # and the default observation_slot = fill_slot in make_raw_update.
        mints = [ev.wallet for ev in events]
        assert WALLET_B in mints
        # WALLET_A at LAUNCH_SLOT - 1 should be skipped
        assert WALLET_A not in mints, (
            "Events from slots before last_slot must be skipped on resume"
        )


# ---------------------------------------------------------------------------
# 9. Selectivity: not a blind mirror, not a standalone trigger
# ---------------------------------------------------------------------------

class TestCopyTradeSelectivityOnly:
    """AC: smart-money is a selectivity filter only, never a blind mirror or trigger."""

    def test_smart_money_module_has_no_trade_pathway(self) -> None:
        """The smart_money module must not import or reference any trade-execution symbols.

        Verified symbols: things that would imply a trade pathway such as
        'send_transaction', 'sign(', 'land(', 'JitoJupiter', 'submit'.
        """
        import aats.ingestion.smart_money as sm_module
        source_path = sm_module.__file__
        assert source_path is not None
        with open(source_path) as f:
            source = f.read()
        # These symbols would imply a trade pathway
        forbidden_symbols = [
            "send_transaction",
            "sign(",
            "land(",
            "JitoJupiter",
            "submit_transaction",
        ]
        for sym in forbidden_symbols:
            assert sym not in source, (
                f"smart_money.py must not reference '{sym}' — "
                "copy-trade is SELECTIVITY ONLY, never a trade trigger"
            )

    def test_smart_wallets_in_is_count_not_signal_object(self) -> None:
        """The count function returns a plain int, not any kind of signal/intent object."""
        from aats.ingestion.smart_money import count_smart_wallets_in
        result = count_smart_wallets_in([], MINT_X, LAUNCH_SLOT, 10)
        assert isinstance(result, int), (
            f"smart_wallets_in count must be plain int, not {type(result)}"
        )

    def test_no_win_rate_field_in_module(self) -> None:
        """No win_rate field or symbol should appear in the module."""
        import aats.ingestion.smart_money as sm_module
        source_path = sm_module.__file__
        assert source_path is not None
        with open(source_path) as f:
            source = f.read()
        assert "win_rate" not in source, "smart_money.py must contain no win_rate field or symbol"

    def test_sell_events_do_not_increment_count(self) -> None:
        """A tracked wallet selling does NOT count as a buy signal."""
        ev = SmartMoneyEvent(
            wallet=WALLET_A,
            mint=MINT_X,
            their_fill_slot=LAUNCH_SLOT + 2,
            our_event_slot=LAUNCH_SLOT + 4,
            entry_lag_slots=2,
            their_fill_block_time_ms=BLOCK_TIME_MS,
            our_observation_ms=BLOCK_TIME_MS + 800,
            observation_lag_ms=800,
            is_buy=False,  # SELL
            sol_amount_lamports=90_000_000,
            token_amount_base=1_000_000_000,
            data_staleness_ms=800,
            source="replay",
        )
        count = count_smart_wallets_in([ev], MINT_X, LAUNCH_SLOT, first_k_slots=10)
        assert count == 0, "A sell by a tracked wallet must NOT count toward smart_wallets_in"


# ---------------------------------------------------------------------------
# 10. Bus publisher
# ---------------------------------------------------------------------------

class TestBusPublisher:
    """AC: bus publisher uses STREAM_SMART_MONEY and respects MAXLEN ~."""

    def test_publishes_to_correct_stream(self) -> None:
        ev = SmartMoneyEvent(
            wallet=WALLET_A, mint=MINT_X,
            their_fill_slot=LAUNCH_SLOT, our_event_slot=LAUNCH_SLOT + 1,
            entry_lag_slots=1, their_fill_block_time_ms=BLOCK_TIME_MS,
            our_observation_ms=BLOCK_TIME_MS + 400, observation_lag_ms=400,
            is_buy=True, sol_amount_lamports=100_000_000,
            token_amount_base=1_000_000_000, data_staleness_ms=400,
            source="replay",
        )
        redis_mock = AsyncMock()
        redis_mock.xadd = AsyncMock(return_value="1234567890-0")
        publisher = SmartWalletBusPublisher(redis_mock)
        asyncio.run(publisher.publish(ev))
        stream_name = redis_mock.xadd.call_args[0][0]
        assert stream_name == "smart_money.events"

    def test_publishes_with_approximate_maxlen(self) -> None:
        ev = SmartMoneyEvent(
            wallet=WALLET_A, mint=MINT_X,
            their_fill_slot=LAUNCH_SLOT, our_event_slot=LAUNCH_SLOT,
            entry_lag_slots=0, their_fill_block_time_ms=BLOCK_TIME_MS,
            our_observation_ms=BLOCK_TIME_MS, observation_lag_ms=0,
            is_buy=True, sol_amount_lamports=100_000_000,
            token_amount_base=1_000_000_000, data_staleness_ms=0,
            source="replay",
        )
        redis_mock = AsyncMock()
        redis_mock.xadd = AsyncMock(return_value="0-0")
        publisher = SmartWalletBusPublisher(redis_mock)
        asyncio.run(publisher.publish(ev))
        kwargs = redis_mock.xadd.call_args[1]
        assert kwargs.get("approximate") is True, "MAXLEN must use approximate=True (O(1) amortized)"
        assert kwargs.get("maxlen", 0) > 0, "maxlen must be set"

    def test_publish_includes_lag_fields(self) -> None:
        """Bus message must carry the lag fields (the core T-302 requirement)."""
        ev = SmartMoneyEvent(
            wallet=WALLET_A, mint=MINT_X,
            their_fill_slot=LAUNCH_SLOT, our_event_slot=LAUNCH_SLOT + 4,
            entry_lag_slots=4, their_fill_block_time_ms=BLOCK_TIME_MS,
            our_observation_ms=BLOCK_TIME_MS + 1_600, observation_lag_ms=1_600,
            is_buy=True, sol_amount_lamports=100_000_000,
            token_amount_base=1_000_000_000, data_staleness_ms=1_600,
            source="replay",
        )
        captured: list[dict] = []
        redis_mock = AsyncMock()
        async def fake_xadd(stream: str, payload: dict, **kwargs: Any) -> str:
            captured.append(payload)
            return "1-0"
        redis_mock.xadd.side_effect = fake_xadd
        publisher = SmartWalletBusPublisher(redis_mock)
        asyncio.run(publisher.publish(ev))

        assert len(captured) == 1
        payload = captured[0]
        assert "entry_lag_slots" in payload, "entry_lag_slots missing from bus payload"
        assert "observation_lag_ms" in payload, "observation_lag_ms missing from bus payload"
        assert payload["entry_lag_slots"] == "4"
        assert payload["observation_lag_ms"] == "1600"


# ---------------------------------------------------------------------------
# 11. Stats tracking
# ---------------------------------------------------------------------------

class TestStreamStats:
    """AC: stream tracks decoded/skipped/error counts for monitoring."""

    def test_stats_decoded_count(self) -> None:
        updates = [
            make_raw_update(wallet=WALLET_A, fill_block_time_ms=BLOCK_TIME_MS),
            make_raw_update(wallet=WALLET_B, fill_block_time_ms=BLOCK_TIME_MS + 400),
        ]
        backend = ReplaySmartWalletBackend(updates)
        cfg = SmartWalletConfig(enabled=True, wallet_addresses=[WALLET_A, WALLET_B])
        stream = SmartWalletStream(config=cfg, backend=backend)
        collect_events(stream)
        assert stream.stats.events_decoded == 2

    def test_stats_skipped_count(self) -> None:
        updates = [
            make_raw_update(fill_block_time_ms=None),    # skipped (pre-confirmation)
            make_raw_update(fill_block_time_ms=BLOCK_TIME_MS),  # decoded
        ]
        backend = ReplaySmartWalletBackend(updates)
        cfg = SmartWalletConfig(enabled=True, wallet_addresses=[WALLET_A])
        stream = SmartWalletStream(config=cfg, backend=backend)
        collect_events(stream)
        assert stream.stats.events_decoded == 1
        assert stream.stats.events_skipped == 1

    def test_last_event_time_ms_set(self) -> None:
        updates = [make_raw_update(fill_block_time_ms=BLOCK_TIME_MS)]
        backend = ReplaySmartWalletBackend(updates)
        cfg = SmartWalletConfig(enabled=True, wallet_addresses=[WALLET_A])
        stream = SmartWalletStream(config=cfg, backend=backend)
        collect_events(stream)
        assert stream.stats.last_event_time_ms == BLOCK_TIME_MS


# ---------------------------------------------------------------------------
# 12. Wallet filtering
# ---------------------------------------------------------------------------

class TestWalletFiltering:
    """AC: only tracked wallets produce events; untracked wallets are ignored."""

    def test_untracked_wallet_produces_no_event(self) -> None:
        """An update for a wallet NOT in config.wallet_addresses is filtered out."""
        updates = [
            make_raw_update(wallet="UntrackedWalletXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX"),
        ]
        backend = ReplaySmartWalletBackend(updates)
        cfg = SmartWalletConfig(enabled=True, wallet_addresses=[WALLET_A])  # only WALLET_A
        stream = SmartWalletStream(config=cfg, backend=backend)
        events = collect_events(stream)
        assert events == [], "Untracked wallet must produce no SmartMoneyEvent"

    def test_tracked_wallet_produces_event(self) -> None:
        updates = [make_raw_update(wallet=WALLET_A)]
        backend = ReplaySmartWalletBackend(updates)
        cfg = SmartWalletConfig(enabled=True, wallet_addresses=[WALLET_A])
        stream = SmartWalletStream(config=cfg, backend=backend)
        events = collect_events(stream)
        assert len(events) == 1
        assert events[0].wallet == WALLET_A

    def test_mixed_tracked_and_untracked(self) -> None:
        updates = [
            make_raw_update(wallet=WALLET_A),  # tracked
            make_raw_update(wallet="UntrackedXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX"),  # untracked
            make_raw_update(wallet=WALLET_B),  # tracked
        ]
        backend = ReplaySmartWalletBackend(updates)
        cfg = SmartWalletConfig(enabled=True, wallet_addresses=[WALLET_A, WALLET_B])
        stream = SmartWalletStream(config=cfg, backend=backend)
        events = collect_events(stream)
        wallets = {ev.wallet for ev in events}
        assert wallets == {WALLET_A, WALLET_B}
        assert "UntrackedXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX" not in wallets
