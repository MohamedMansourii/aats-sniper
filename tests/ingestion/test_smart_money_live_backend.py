"""Tests for the LIVE SmartWalletBackend (E-M1-04 / Wave 1 "go-smart" alpha engine).

TASK: wire a LIVE SmartWalletBackend (Geyser accountSubscribe) behind the
existing SmartWalletBackend Protocol.  T-302's ReplaySmartWalletBackend stays
the deterministic offline fixture; GeyserSmartWalletBackend is the new
production implementation.  Every T-302 safety property is PRESERVED, not
re-implemented:

  1. DISABLED BY DEFAULT           -- enforced entirely by SmartWalletStream /
                                       SmartWalletConfig (unchanged); verified
                                       here with the LIVE backend wired in.
  2. 20-wallet cap                 -- enforced by SmartWalletConfig (unchanged);
                                       verified the live backend only ever
                                       receives the capped wallet_set.
  3. COUNT-ONLY selectivity        -- count_smart_wallets_in() is untouched;
                                       this backend produces RawWalletUpdate
                                       only (a data record), never a trigger.
  4. Honest, non-negative lag      -- their_fill_slot vs our_event_slot; the
                                       live backend never fabricates a
                                       negative or reordered relationship.
  5. (wallet, mint, fill_slot) dedup -- enforced by SmartWalletStream
                                       (unchanged); verified end-to-end here.
  6. NO live gRPC/WS in tests      -- every test below uses
                                       MockAccountSubscriptionSource (raw,
                                       pre-decode account writes) or a patched
                                       fake gRPC stub -- never a real network
                                       call.
  7. event_time from on-chain slot -- fill_block_time_ms is resolved via an
                                       INJECTED async BlockTimeResolver
                                       (on-chain getBlockTime); wall-clock is
                                       NEVER substituted (T-300a).  A resolver
                                       returning None (or raising) holds the
                                       event PENDING via the existing,
                                       unchanged SmartWalletDecoder.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Ensure the vendored Yellowstone proto stubs are importable (same pattern as
# tests/ingestion/test_geyser_transport.py).
_PROTO_DIR = os.path.join(os.path.dirname(__file__), "../../aats/ingestion/geyser_proto")
_PROTO_DIR = os.path.normpath(_PROTO_DIR)
if _PROTO_DIR not in sys.path:
    sys.path.insert(0, _PROTO_DIR)

import geyser_pb2  # type: ignore[import]  # noqa: E402

from aats.ingestion.smart_money import (  # noqa: E402
    MAX_TRACKED_WALLETS,
    TOKEN_2022_PROGRAM_ID,
    TOKEN_PROGRAM_ID,
    GeyserAccountSubscriptionSource,
    GeyserSmartWalletBackend,
    MockAccountSubscriptionSource,
    RawAccountUpdate,
    SmartWalletConfig,
    SmartWalletStream,
    _build_account_subscribe_request,
    _decode_spl_token_account,
    build_geyser_smart_wallet_backend_from_env,
    count_smart_wallets_in,
    make_rpc_block_time_resolver,
)
from aats.ingestion.transport import _bytes_to_base58  # noqa: E402

# ---------------------------------------------------------------------------
# Deterministic fixture pubkeys (byte-seeded, then base58-encoded --
# avoids leading-zero base58-decode ambiguity in test fixtures).
# ---------------------------------------------------------------------------

_MINT_X_BYTES = bytes(range(1, 33))
_MINT_Y_BYTES = bytes(range(2, 34))
_WALLET_A_BYTES = bytes([9] * 32)
_WALLET_B_BYTES = bytes([10] * 32)
_UNTRACKED_BYTES = bytes([11] * 32)

MINT_X = _bytes_to_base58(_MINT_X_BYTES)
MINT_Y = _bytes_to_base58(_MINT_Y_BYTES)
WALLET_A = _bytes_to_base58(_WALLET_A_BYTES)
WALLET_B = _bytes_to_base58(_WALLET_B_BYTES)
UNTRACKED_WALLET = _bytes_to_base58(_UNTRACKED_BYTES)

LAUNCH_SLOT = 300_000_000
BLOCK_TIME_MS = 1_718_700_000_000


def _make_token_account_data(mint: bytes, owner: bytes, amount: int) -> bytes:
    """Build a (real-layout) 165-byte SPL Token Account blob.

    mint(32) + owner(32) + amount(u64 LE, 8) + zero-padded remainder, matching
    spl_token::state::Account::LEN=165.  Only the first 72 bytes are load-
    bearing for decode; the rest is realistic zero-padding.
    """
    body = mint + owner + amount.to_bytes(8, "little")
    return body + b"\x00" * (165 - len(body))


def _account_update(
    *,
    pubkey: str,
    owner_program: str,
    slot: int,
    data: bytes = b"",
    lamports: int = 1_000_000,
    write_version: int = 1,
    is_startup: bool = False,
) -> RawAccountUpdate:
    return RawAccountUpdate(
        pubkey=pubkey,
        owner_program=owner_program,
        slot=slot,
        lamports=lamports,
        data=data,
        write_version=write_version,
        is_startup=is_startup,
    )


def _run(coro_factory):
    return asyncio.run(coro_factory())


def _fixed_resolver(block_time_ms: int):
    async def _resolve(slot: int) -> int | None:
        return block_time_ms
    return _resolve


def _none_resolver():
    async def _resolve(slot: int) -> int | None:
        return None
    return _resolve


def _raising_resolver():
    async def _resolve(slot: int) -> int | None:
        raise RuntimeError("boom: simulated RPC failure")
    return _resolve


# ---------------------------------------------------------------------------
# 1. SPL Token Account decode helper
# ---------------------------------------------------------------------------


class TestDecodeSplTokenAccount:
    def test_decodes_mint_owner_amount(self) -> None:
        data = _make_token_account_data(_MINT_X_BYTES, _WALLET_A_BYTES, 1_000_000_000)
        decoded = _decode_spl_token_account(data)
        assert decoded is not None
        mint, owner, amount = decoded
        assert mint == MINT_X
        assert owner == WALLET_A
        assert amount == 1_000_000_000

    def test_amount_is_little_endian(self) -> None:
        # 0x0100000000000000 little-endian == 1 (sanity: not accidentally big-endian)
        data = _make_token_account_data(_MINT_X_BYTES, _WALLET_A_BYTES, 1)
        decoded = _decode_spl_token_account(data)
        assert decoded is not None
        assert decoded[2] == 1

    def test_too_short_data_returns_none(self) -> None:
        assert _decode_spl_token_account(b"\x00" * 10) is None

    def test_empty_data_returns_none(self) -> None:
        assert _decode_spl_token_account(b"") is None

    def test_exact_min_length_decodes(self) -> None:
        # Exactly 72 bytes (no extension padding) must still decode.
        data = _MINT_X_BYTES + _WALLET_A_BYTES + (500).to_bytes(8, "little")
        assert len(data) == 72
        decoded = _decode_spl_token_account(data)
        assert decoded == (MINT_X, WALLET_A, 500)


# ---------------------------------------------------------------------------
# 2. _build_account_subscribe_request — pure helper
# ---------------------------------------------------------------------------


class TestBuildAccountSubscribeRequest:
    def test_native_group_lists_all_wallets(self) -> None:
        wallets = frozenset([WALLET_A, WALLET_B])
        req = _build_account_subscribe_request(geyser_pb2, wallets, from_slot=0, commitment=0)
        assert "native" in req.accounts
        assert set(req.accounts["native"].account) == wallets

    def test_one_token_group_per_wallet(self) -> None:
        wallets = frozenset([WALLET_A, WALLET_B])
        req = _build_account_subscribe_request(geyser_pb2, wallets, from_slot=0, commitment=0)
        tok_groups = [k for k in req.accounts if k.startswith("tok_")]
        assert len(tok_groups) == 2

    def test_token_group_owner_is_token_programs(self) -> None:
        wallets = frozenset([WALLET_A])
        req = _build_account_subscribe_request(geyser_pb2, wallets, from_slot=0, commitment=0)
        group = req.accounts["tok_0"]
        assert TOKEN_PROGRAM_ID in group.owner
        assert TOKEN_2022_PROGRAM_ID in group.owner

    def test_token_group_memcmp_targets_wallet_at_offset_32(self) -> None:
        wallets = frozenset([WALLET_A])
        req = _build_account_subscribe_request(geyser_pb2, wallets, from_slot=0, commitment=0)
        group = req.accounts["tok_0"]
        assert len(group.filters) == 1
        memcmp = group.filters[0].memcmp
        assert memcmp.offset == 32
        assert memcmp.bytes == _WALLET_A_BYTES

    def test_commitment_processed(self) -> None:
        req = _build_account_subscribe_request(
            geyser_pb2, frozenset([WALLET_A]), from_slot=0, commitment=0
        )
        assert req.commitment == 0

    def test_from_slot_set_when_nonzero(self) -> None:
        req = _build_account_subscribe_request(
            geyser_pb2, frozenset([WALLET_A]), from_slot=299_999_000, commitment=0
        )
        assert req.from_slot == 299_999_000

    def test_from_slot_not_set_when_zero(self) -> None:
        req = _build_account_subscribe_request(
            geyser_pb2, frozenset([WALLET_A]), from_slot=0, commitment=0
        )
        assert req.from_slot == 0

    def test_empty_wallets_produces_no_groups(self) -> None:
        req = _build_account_subscribe_request(geyser_pb2, frozenset(), from_slot=0, commitment=0)
        assert len(req.accounts) == 0

    def test_never_a_firehose_no_bare_owner_filter(self) -> None:
        """Every token group MUST carry a memcmp filter -- an owner-only filter
        with no memcmp would match every SPL token account on-chain."""
        wallets = frozenset([WALLET_A, WALLET_B])
        req = _build_account_subscribe_request(geyser_pb2, wallets, from_slot=0, commitment=0)
        for key, group in req.accounts.items():
            if key.startswith("tok_"):
                assert len(group.filters) >= 1, (
                    f"group {key} has owner={list(group.owner)} but no memcmp filter "
                    "-- this would match EVERY token account on-chain (a firehose)."
                )

    def test_twenty_wallets_produce_twenty_token_groups(self) -> None:
        wallets = frozenset(_bytes_to_base58(bytes([i] * 32)) for i in range(20, 40))
        req = _build_account_subscribe_request(geyser_pb2, wallets, from_slot=0, commitment=0)
        tok_groups = [k for k in req.accounts if k.startswith("tok_")]
        assert len(tok_groups) == MAX_TRACKED_WALLETS == 20


# ---------------------------------------------------------------------------
# 3. GeyserSmartWalletBackend + MockAccountSubscriptionSource
# ---------------------------------------------------------------------------


class TestGeyserSmartWalletBackendDecode:
    def test_startup_snapshot_seeds_baseline_no_emit(self) -> None:
        """The first (is_startup=True) observation is the pre-existing balance
        -- it must NOT be emitted as a RawWalletUpdate."""
        data = _make_token_account_data(_MINT_X_BYTES, _WALLET_A_BYTES, 500_000_000)
        source = MockAccountSubscriptionSource([
            _account_update(
                pubkey="TokAcct1", owner_program=TOKEN_PROGRAM_ID,
                slot=LAUNCH_SLOT, data=data, is_startup=True,
            ),
        ])
        backend = GeyserSmartWalletBackend(source=source, block_time_resolver=_fixed_resolver(BLOCK_TIME_MS))

        async def collect():
            return [u async for u in backend.subscribe(frozenset([WALLET_A]))]

        updates = _run(collect)
        assert updates == []

    def test_buy_after_startup_emits_is_buy_true(self) -> None:
        baseline_data = _make_token_account_data(_MINT_X_BYTES, _WALLET_A_BYTES, 0)
        buy_data = _make_token_account_data(_MINT_X_BYTES, _WALLET_A_BYTES, 1_000_000_000)
        source = MockAccountSubscriptionSource([
            _account_update(pubkey="TokAcct1", owner_program=TOKEN_PROGRAM_ID,
                             slot=LAUNCH_SLOT, data=baseline_data, is_startup=True),
            _account_update(pubkey="TokAcct1", owner_program=TOKEN_PROGRAM_ID,
                             slot=LAUNCH_SLOT + 1, data=buy_data, is_startup=False),
        ])
        backend = GeyserSmartWalletBackend(source=source, block_time_resolver=_fixed_resolver(BLOCK_TIME_MS))

        async def collect():
            return [u async for u in backend.subscribe(frozenset([WALLET_A]))]

        updates = _run(collect)
        assert len(updates) == 1
        u = updates[0]
        assert u.wallet == WALLET_A
        assert u.mint == MINT_X
        assert u.is_buy is True
        assert u.token_delta_base == 1_000_000_000
        assert u.fill_slot == LAUNCH_SLOT + 1
        assert u.fill_block_time_ms == BLOCK_TIME_MS

    def test_sell_after_buy_emits_is_buy_false(self) -> None:
        baseline_data = _make_token_account_data(_MINT_X_BYTES, _WALLET_A_BYTES, 1_000_000_000)
        sell_data = _make_token_account_data(_MINT_X_BYTES, _WALLET_A_BYTES, 400_000_000)
        source = MockAccountSubscriptionSource([
            _account_update(pubkey="TokAcct1", owner_program=TOKEN_PROGRAM_ID,
                             slot=LAUNCH_SLOT, data=baseline_data, is_startup=True),
            _account_update(pubkey="TokAcct1", owner_program=TOKEN_PROGRAM_ID,
                             slot=LAUNCH_SLOT + 2, data=sell_data, is_startup=False),
        ])
        backend = GeyserSmartWalletBackend(source=source, block_time_resolver=_fixed_resolver(BLOCK_TIME_MS))

        async def collect():
            return [u async for u in backend.subscribe(frozenset([WALLET_A]))]

        updates = _run(collect)
        assert len(updates) == 1
        u = updates[0]
        assert u.is_buy is False
        assert u.token_delta_base == 600_000_000

    def test_zero_delta_write_does_not_emit(self) -> None:
        data = _make_token_account_data(_MINT_X_BYTES, _WALLET_A_BYTES, 1_000_000_000)
        source = MockAccountSubscriptionSource([
            _account_update(pubkey="TokAcct1", owner_program=TOKEN_PROGRAM_ID,
                             slot=LAUNCH_SLOT, data=data, is_startup=True),
            _account_update(pubkey="TokAcct1", owner_program=TOKEN_PROGRAM_ID,
                             slot=LAUNCH_SLOT + 1, data=data, is_startup=False),  # unchanged
        ])
        backend = GeyserSmartWalletBackend(source=source, block_time_resolver=_fixed_resolver(BLOCK_TIME_MS))

        async def collect():
            return [u async for u in backend.subscribe(frozenset([WALLET_A]))]

        assert _run(collect) == []

    def test_untracked_owner_defensively_dropped(self) -> None:
        """Even if a raw update slips through for an untracked wallet, the
        backend fail-closed drops it (defense in depth beyond the source's
        own memcmp filter)."""
        baseline = _make_token_account_data(_MINT_X_BYTES, _UNTRACKED_BYTES, 0)
        buy = _make_token_account_data(_MINT_X_BYTES, _UNTRACKED_BYTES, 1_000_000)
        source = MockAccountSubscriptionSource([
            _account_update(pubkey="TokAcctU", owner_program=TOKEN_PROGRAM_ID,
                             slot=LAUNCH_SLOT, data=baseline, is_startup=True),
            _account_update(pubkey="TokAcctU", owner_program=TOKEN_PROGRAM_ID,
                             slot=LAUNCH_SLOT + 1, data=buy, is_startup=False),
        ])
        backend = GeyserSmartWalletBackend(source=source, block_time_resolver=_fixed_resolver(BLOCK_TIME_MS))

        async def collect():
            return [u async for u in backend.subscribe(frozenset([WALLET_A]))]  # untracked not in set

        assert _run(collect) == []

    def test_native_account_write_never_emits(self) -> None:
        """A write to the wallet's OWN native (System-owned) account (pubkey ==
        the tracked wallet address itself) must never become a
        RawWalletUpdate -- it carries no mint."""
        source = MockAccountSubscriptionSource([
            _account_update(pubkey=WALLET_A, owner_program="11111111111111111111111111111111",
                             slot=LAUNCH_SLOT, data=b"", lamports=5_000_000_000, is_startup=True),
            _account_update(pubkey=WALLET_A, owner_program="11111111111111111111111111111111",
                             slot=LAUNCH_SLOT + 1, data=b"", lamports=4_000_000_000, is_startup=False),
        ])
        backend = GeyserSmartWalletBackend(source=source, block_time_resolver=_fixed_resolver(BLOCK_TIME_MS))

        async def collect():
            return [u async for u in backend.subscribe(frozenset([WALLET_A]))]

        assert _run(collect) == []

    def test_resolver_none_yields_none_block_time(self) -> None:
        """A resolver returning None (block time not yet available) must
        surface as fill_block_time_ms=None -- never wall-clock (T-300a)."""
        baseline = _make_token_account_data(_MINT_X_BYTES, _WALLET_A_BYTES, 0)
        buy = _make_token_account_data(_MINT_X_BYTES, _WALLET_A_BYTES, 1_000_000)
        source = MockAccountSubscriptionSource([
            _account_update(pubkey="TokAcct1", owner_program=TOKEN_PROGRAM_ID,
                             slot=LAUNCH_SLOT, data=baseline, is_startup=True),
            _account_update(pubkey="TokAcct1", owner_program=TOKEN_PROGRAM_ID,
                             slot=LAUNCH_SLOT + 1, data=buy, is_startup=False),
        ])
        backend = GeyserSmartWalletBackend(source=source, block_time_resolver=_none_resolver())

        async def collect():
            return [u async for u in backend.subscribe(frozenset([WALLET_A]))]

        updates = _run(collect)
        assert len(updates) == 1
        assert updates[0].fill_block_time_ms is None

    def test_resolver_raising_is_caught_treated_as_none(self) -> None:
        """A resolver exception (e.g. RPC timeout) must be caught -- never
        crash the stream -- and degrade to fill_block_time_ms=None."""
        baseline = _make_token_account_data(_MINT_X_BYTES, _WALLET_A_BYTES, 0)
        buy = _make_token_account_data(_MINT_X_BYTES, _WALLET_A_BYTES, 1_000_000)
        source = MockAccountSubscriptionSource([
            _account_update(pubkey="TokAcct1", owner_program=TOKEN_PROGRAM_ID,
                             slot=LAUNCH_SLOT, data=baseline, is_startup=True),
            _account_update(pubkey="TokAcct1", owner_program=TOKEN_PROGRAM_ID,
                             slot=LAUNCH_SLOT + 1, data=buy, is_startup=False),
        ])
        backend = GeyserSmartWalletBackend(source=source, block_time_resolver=_raising_resolver())

        async def collect():
            return [u async for u in backend.subscribe(frozenset([WALLET_A]))]

        updates = _run(collect)
        assert len(updates) == 1
        assert updates[0].fill_block_time_ms is None

    def test_no_resolver_configured_yields_none_block_time(self) -> None:
        baseline = _make_token_account_data(_MINT_X_BYTES, _WALLET_A_BYTES, 0)
        buy = _make_token_account_data(_MINT_X_BYTES, _WALLET_A_BYTES, 1_000_000)
        source = MockAccountSubscriptionSource([
            _account_update(pubkey="TokAcct1", owner_program=TOKEN_PROGRAM_ID,
                             slot=LAUNCH_SLOT, data=baseline, is_startup=True),
            _account_update(pubkey="TokAcct1", owner_program=TOKEN_PROGRAM_ID,
                             slot=LAUNCH_SLOT + 1, data=buy, is_startup=False),
        ])
        backend = GeyserSmartWalletBackend(source=source)  # no resolver injected

        async def collect():
            return [u async for u in backend.subscribe(frozenset([WALLET_A]))]

        updates = _run(collect)
        assert len(updates) == 1
        assert updates[0].fill_block_time_ms is None

    def test_sol_amount_lamports_is_zero_never_fabricated(self) -> None:
        """The SOL leg cannot be derived from a single SPL Token Account
        write; it must be honestly 0, never guessed."""
        baseline = _make_token_account_data(_MINT_X_BYTES, _WALLET_A_BYTES, 0)
        buy = _make_token_account_data(_MINT_X_BYTES, _WALLET_A_BYTES, 1_000_000)
        source = MockAccountSubscriptionSource([
            _account_update(pubkey="TokAcct1", owner_program=TOKEN_PROGRAM_ID,
                             slot=LAUNCH_SLOT, data=baseline, is_startup=True),
            _account_update(pubkey="TokAcct1", owner_program=TOKEN_PROGRAM_ID,
                             slot=LAUNCH_SLOT + 1, data=buy, is_startup=False),
        ])
        backend = GeyserSmartWalletBackend(source=source, block_time_resolver=_fixed_resolver(BLOCK_TIME_MS))

        async def collect():
            return [u async for u in backend.subscribe(frozenset([WALLET_A]))]

        updates = _run(collect)
        assert updates[0].sol_delta_lamports == 0
        assert isinstance(updates[0].sol_delta_lamports, int)

    def test_observation_slot_equals_fill_slot_honest_no_independent_clock(self) -> None:
        """No independent slot clock exists on this live path; observation_slot
        is honestly set equal to fill_slot (see class docstring)."""
        baseline = _make_token_account_data(_MINT_X_BYTES, _WALLET_A_BYTES, 0)
        buy = _make_token_account_data(_MINT_X_BYTES, _WALLET_A_BYTES, 1_000_000)
        source = MockAccountSubscriptionSource([
            _account_update(pubkey="TokAcct1", owner_program=TOKEN_PROGRAM_ID,
                             slot=LAUNCH_SLOT, data=baseline, is_startup=True),
            _account_update(pubkey="TokAcct1", owner_program=TOKEN_PROGRAM_ID,
                             slot=LAUNCH_SLOT + 7, data=buy, is_startup=False),
        ])
        backend = GeyserSmartWalletBackend(source=source, block_time_resolver=_fixed_resolver(BLOCK_TIME_MS))

        async def collect():
            return [u async for u in backend.subscribe(frozenset([WALLET_A]))]

        updates = _run(collect)
        assert updates[0].observation_slot == updates[0].fill_slot == LAUNCH_SLOT + 7

    def test_is_connected_proxies_to_source(self) -> None:
        source = MockAccountSubscriptionSource([])
        backend = GeyserSmartWalletBackend(source=source)
        assert backend.is_connected is False

        async def drain():
            async for _ in backend.subscribe(frozenset([WALLET_A])):
                assert backend.is_connected is True

        _run(drain)
        assert backend.is_connected is False

    def test_multiple_mints_tracked_independently(self) -> None:
        """Two different mints for the same wallet are tracked with
        independent baselines (no cross-mint contamination)."""
        base_x = _make_token_account_data(_MINT_X_BYTES, _WALLET_A_BYTES, 0)
        buy_x = _make_token_account_data(_MINT_X_BYTES, _WALLET_A_BYTES, 500)
        base_y = _make_token_account_data(_MINT_Y_BYTES, _WALLET_A_BYTES, 0)
        buy_y = _make_token_account_data(_MINT_Y_BYTES, _WALLET_A_BYTES, 700)
        source = MockAccountSubscriptionSource([
            _account_update(pubkey="TokX", owner_program=TOKEN_PROGRAM_ID,
                             slot=LAUNCH_SLOT, data=base_x, is_startup=True),
            _account_update(pubkey="TokY", owner_program=TOKEN_PROGRAM_ID,
                             slot=LAUNCH_SLOT, data=base_y, is_startup=True),
            _account_update(pubkey="TokX", owner_program=TOKEN_PROGRAM_ID,
                             slot=LAUNCH_SLOT + 1, data=buy_x, is_startup=False),
            _account_update(pubkey="TokY", owner_program=TOKEN_PROGRAM_ID,
                             slot=LAUNCH_SLOT + 1, data=buy_y, is_startup=False),
        ])
        backend = GeyserSmartWalletBackend(source=source, block_time_resolver=_fixed_resolver(BLOCK_TIME_MS))

        async def collect():
            return [u async for u in backend.subscribe(frozenset([WALLET_A]))]

        updates = _run(collect)
        assert len(updates) == 2
        by_mint = {u.mint: u.token_delta_base for u in updates}
        assert by_mint[MINT_X] == 500
        assert by_mint[MINT_Y] == 700


# ---------------------------------------------------------------------------
# 4. Full pipeline: SmartWalletStream + GeyserSmartWalletBackend
# ---------------------------------------------------------------------------


class TestFullPipelineWithLiveBackend:
    """AC: every T-302 safety property holds with the LIVE backend wired in."""

    def test_disabled_by_default_backend_never_invoked(self) -> None:
        """With config.enabled=False, SmartWalletStream never even calls
        backend.subscribe() -- the live source's is_connected stays False."""
        data = _make_token_account_data(_MINT_X_BYTES, _WALLET_A_BYTES, 1_000_000_000)
        source = MockAccountSubscriptionSource([
            _account_update(pubkey="Tok1", owner_program=TOKEN_PROGRAM_ID,
                             slot=LAUNCH_SLOT, data=data, is_startup=True),
        ])
        backend = GeyserSmartWalletBackend(source=source, block_time_resolver=_fixed_resolver(BLOCK_TIME_MS))
        cfg = SmartWalletConfig(enabled=False, wallet_addresses=[WALLET_A])
        stream = SmartWalletStream(config=cfg, backend=backend)

        async def collect():
            return [ev async for ev in stream.subscribe()]

        events = _run(collect)
        assert events == []
        assert source.is_connected is False, (
            "A disabled stream must never touch the live subscription source."
        )

    def test_enabled_emits_correctly_lagged_events(self) -> None:
        baseline = _make_token_account_data(_MINT_X_BYTES, _WALLET_A_BYTES, 0)
        buy = _make_token_account_data(_MINT_X_BYTES, _WALLET_A_BYTES, 2_000_000_000)
        source = MockAccountSubscriptionSource([
            _account_update(pubkey="Tok1", owner_program=TOKEN_PROGRAM_ID,
                             slot=LAUNCH_SLOT, data=baseline, is_startup=True),
            _account_update(pubkey="Tok1", owner_program=TOKEN_PROGRAM_ID,
                             slot=LAUNCH_SLOT + 4, data=buy, is_startup=False),
        ])
        backend = GeyserSmartWalletBackend(source=source, block_time_resolver=_fixed_resolver(BLOCK_TIME_MS))
        cfg = SmartWalletConfig(enabled=True, wallet_addresses=[WALLET_A])
        stream = SmartWalletStream(config=cfg, backend=backend)

        async def collect():
            return [ev async for ev in stream.subscribe()]

        events = _run(collect)
        assert len(events) == 1
        ev = events[0]
        assert ev.wallet == WALLET_A
        assert ev.mint == MINT_X
        assert ev.is_buy is True
        assert ev.their_fill_slot == LAUNCH_SLOT + 4
        # Honest lag invariants (enforced by SmartMoneyEvent.__post_init__ too)
        assert ev.our_event_slot >= ev.their_fill_slot
        assert ev.entry_lag_slots >= 0
        assert ev.observation_lag_ms >= 0
        assert ev.entry_lag_slots == ev.our_event_slot - ev.their_fill_slot

    def test_wallet_cap_enforced_with_live_backend(self) -> None:
        """SmartWalletConfig truncates to 20 wallets; the live backend only
        ever sees the truncated wallet_set (SmartWalletStream passes
        config.wallet_set, already capped)."""
        many_wallets = [_bytes_to_base58(bytes([i] * 32)) for i in range(50, 75)]  # 25 wallets
        cfg = SmartWalletConfig(enabled=True, wallet_addresses=many_wallets)
        assert len(cfg.wallet_addresses) == MAX_TRACKED_WALLETS

        seen_wallet_sets: list[frozenset[str]] = []

        class _SpySource(MockAccountSubscriptionSource):
            async def subscribe(self, wallet_addresses, last_slot=0):  # type: ignore[override]
                seen_wallet_sets.append(wallet_addresses)
                async for u in super().subscribe(wallet_addresses, last_slot):
                    yield u

        source = _SpySource([])
        backend = GeyserSmartWalletBackend(source=source)
        stream = SmartWalletStream(config=cfg, backend=backend)

        async def collect():
            return [ev async for ev in stream.subscribe()]

        _run(collect)
        assert len(seen_wallet_sets) == 1
        assert len(seen_wallet_sets[0]) == MAX_TRACKED_WALLETS

    def test_dedup_on_wallet_mint_fill_slot_with_live_backend(self) -> None:
        """The SAME (wallet, mint, fill_slot) delivered twice by the raw
        account source (e.g. a re-notify at the same slot) must dedup to one
        SmartMoneyEvent -- enforced by SmartWalletStream, unchanged."""
        baseline = _make_token_account_data(_MINT_X_BYTES, _WALLET_A_BYTES, 0)
        buy = _make_token_account_data(_MINT_X_BYTES, _WALLET_A_BYTES, 1_000_000_000)
        source = MockAccountSubscriptionSource([
            _account_update(pubkey="Tok1", owner_program=TOKEN_PROGRAM_ID,
                             slot=LAUNCH_SLOT, data=baseline, is_startup=True),
            _account_update(pubkey="Tok1", owner_program=TOKEN_PROGRAM_ID,
                             slot=LAUNCH_SLOT + 1, data=buy, is_startup=False,
                             write_version=1),
            _account_update(pubkey="Tok1", owner_program=TOKEN_PROGRAM_ID,
                             slot=LAUNCH_SLOT + 1, data=buy, is_startup=False,
                             write_version=2),  # re-notify, same balance, same slot
        ])
        backend = GeyserSmartWalletBackend(source=source, block_time_resolver=_fixed_resolver(BLOCK_TIME_MS))
        cfg = SmartWalletConfig(enabled=True, wallet_addresses=[WALLET_A])
        stream = SmartWalletStream(config=cfg, backend=backend)

        async def collect():
            return [ev async for ev in stream.subscribe()]

        events = _run(collect)
        # The second write has delta==0 relative to the first (same amount),
        # so the backend itself already suppresses it as a no-op write.
        assert len(events) == 1

    def test_stream_level_dedup_fires_on_key_collision_from_live_backend(self) -> None:
        """SmartWalletStream's (wallet, mint, fill_slot) dedup must fire even
        when the LIVE backend legitimately produces two distinct
        RawWalletUpdates that collide on that key (e.g. two separate token
        accounts for the same wallet+mint both changing in the same slot --
        an edge case, but the dedup contract must hold regardless of source)."""
        base1 = _make_token_account_data(_MINT_X_BYTES, _WALLET_A_BYTES, 0)
        buy1 = _make_token_account_data(_MINT_X_BYTES, _WALLET_A_BYTES, 1_000_000_000)
        base2 = _make_token_account_data(_MINT_X_BYTES, _WALLET_A_BYTES, 0)
        buy2 = _make_token_account_data(_MINT_X_BYTES, _WALLET_A_BYTES, 500_000_000)
        source = MockAccountSubscriptionSource([
            # Token account #1 for (WALLET_A, MINT_X)
            _account_update(pubkey="TokAcctOne", owner_program=TOKEN_PROGRAM_ID,
                             slot=LAUNCH_SLOT, data=base1, is_startup=True),
            # Token account #2 for (WALLET_A, MINT_X) -- distinct pubkey, same mint+wallet
            _account_update(pubkey="TokAcctTwo", owner_program=TOKEN_PROGRAM_ID,
                             slot=LAUNCH_SLOT, data=base2, is_startup=True),
            _account_update(pubkey="TokAcctOne", owner_program=TOKEN_PROGRAM_ID,
                             slot=LAUNCH_SLOT + 1, data=buy1, is_startup=False),
            _account_update(pubkey="TokAcctTwo", owner_program=TOKEN_PROGRAM_ID,
                             slot=LAUNCH_SLOT + 1, data=buy2, is_startup=False),
        ])
        backend = GeyserSmartWalletBackend(source=source, block_time_resolver=_fixed_resolver(BLOCK_TIME_MS))
        cfg = SmartWalletConfig(enabled=True, wallet_addresses=[WALLET_A])
        stream = SmartWalletStream(config=cfg, backend=backend)

        async def collect():
            return [ev async for ev in stream.subscribe()]

        events = _run(collect)
        # Both RawWalletUpdates share (WALLET_A, MINT_X, LAUNCH_SLOT+1) -- the
        # stream's dedup keeps only the FIRST and drops the second.
        assert len(events) == 1
        assert events[0].their_fill_slot == LAUNCH_SLOT + 1

    def test_never_a_buy_trigger_only_a_count(self) -> None:
        """Verify the live-backend-produced events feed count_smart_wallets_in
        (a plain int) -- no trade pathway is exercised."""
        baseline = _make_token_account_data(_MINT_X_BYTES, _WALLET_A_BYTES, 0)
        buy = _make_token_account_data(_MINT_X_BYTES, _WALLET_A_BYTES, 1_000_000_000)
        source = MockAccountSubscriptionSource([
            _account_update(pubkey="Tok1", owner_program=TOKEN_PROGRAM_ID,
                             slot=LAUNCH_SLOT, data=baseline, is_startup=True),
            _account_update(pubkey="Tok1", owner_program=TOKEN_PROGRAM_ID,
                             slot=LAUNCH_SLOT + 1, data=buy, is_startup=False),
        ])
        backend = GeyserSmartWalletBackend(source=source, block_time_resolver=_fixed_resolver(BLOCK_TIME_MS))
        cfg = SmartWalletConfig(enabled=True, wallet_addresses=[WALLET_A])
        stream = SmartWalletStream(config=cfg, backend=backend)

        async def collect():
            return [ev async for ev in stream.subscribe()]

        events = _run(collect)
        count = count_smart_wallets_in(events, MINT_X, LAUNCH_SLOT, first_k_slots=10)
        assert isinstance(count, int)
        assert count == 1


# ---------------------------------------------------------------------------
# 5. make_rpc_block_time_resolver / factory
# ---------------------------------------------------------------------------


class TestBlockTimeResolverAndFactory:
    def test_rpc_resolver_returns_none_on_http_error(self) -> None:
        """No network in tests: point at an invalid URL and confirm graceful
        None (never raises, never wall-clock-substitutes)."""
        resolver = make_rpc_block_time_resolver("http://127.0.0.1:1/does-not-exist")

        async def run():
            return await resolver(LAUNCH_SLOT)

        result = asyncio.run(run())
        assert result is None

    def test_rpc_resolver_never_logs_api_key_on_http_status_error(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """B1 regression test.

        An HTTP-status error (e.g. 401/403/429 from an expired or
        rate-limited key) must NEVER leak the api-key embedded in
        RPC_PRIMARY's query string into logs.  httpx.HTTPStatusError's
        str()/repr() embeds the full request URL, so the resolver must log
        only the exception type / status code -- never the exception object
        or the URL itself.
        """
        import httpx

        secret = "SUPER_SECRET_KEY"  # pragma: allowlist secret
        secret_url = f"https://mainnet.helius-rpc.com/?api-key={secret}"

        def _handler(request: httpx.Request) -> httpx.Response:
            # Simulate an expired/rate-limited key: 401 Unauthorized.
            return httpx.Response(401, request=request, json={"error": "unauthorized"})

        mock_transport = httpx.MockTransport(_handler)
        real_async_client = httpx.AsyncClient

        def _patched_async_client(*args, **kwargs):
            kwargs["transport"] = mock_transport
            return real_async_client(*args, **kwargs)

        resolver = make_rpc_block_time_resolver(secret_url)

        async def run():
            return await resolver(LAUNCH_SLOT)

        with (
            caplog.at_level(logging.DEBUG),
            patch("httpx.AsyncClient", _patched_async_client),
        ):
            result = asyncio.run(run())

        assert result is None, "HTTP-status error must resolve to None, never raise."
        assert secret not in caplog.text, (
            "The api-key MUST NEVER appear in logs on the HTTP-error path."
        )
        assert "api-key" not in caplog.text, (
            "The RPC URL's query string MUST NEVER appear in logs on the "
            "HTTP-error path."
        )
        # The status code IS useful for operators and safe to log.
        assert "401" in caplog.text

    def test_factory_builds_backend_without_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """build_geyser_smart_wallet_backend_from_env() must not raise even
        with no environment configured (graceful degradation, like
        GeyserTransport / EnhancedWsFallback)."""
        monkeypatch.delenv("GEYSER_ENDPOINT", raising=False)
        monkeypatch.delenv("GEYSER_TOKEN", raising=False)
        monkeypatch.delenv("RPC_PRIMARY", raising=False)
        backend = build_geyser_smart_wallet_backend_from_env()
        assert isinstance(backend, GeyserSmartWalletBackend)

        async def collect():
            return [u async for u in backend.subscribe(frozenset([WALLET_A]))]

        # Empty GEYSER_ENDPOINT -> underlying source logs and yields nothing.
        assert _run(collect) == []

    def test_no_secrets_logged_in_factory(self, monkeypatch: pytest.MonkeyPatch, caplog) -> None:
        monkeypatch.setenv("GEYSER_ENDPOINT", "fake-endpoint:443")
        monkeypatch.setenv("GEYSER_TOKEN", "super-secret-token-value")
        monkeypatch.setenv("RPC_PRIMARY", "https://example.invalid/?api-key=super-secret-rpc-key")
        backend = build_geyser_smart_wallet_backend_from_env()
        assert isinstance(backend, GeyserSmartWalletBackend)
        # The secret values must never appear in the module source (grepped
        # separately in CI) and must not leak via repr()/str() of the backend.
        assert "super-secret-token-value" not in repr(backend)
        assert "super-secret-rpc-key" not in repr(backend)


# ---------------------------------------------------------------------------
# 6. GeyserAccountSubscriptionSource — LIVE gRPC path (mocked stub, no network)
# ---------------------------------------------------------------------------


def _make_subscribe_update_account(
    *,
    pubkey_bytes: bytes,
    owner_bytes: bytes,
    slot: int,
    data: bytes = b"",
    lamports: int = 1_000_000,
    write_version: int = 1,
    is_startup: bool = False,
) -> object:
    info = geyser_pb2.SubscribeUpdateAccountInfo()
    info.pubkey = pubkey_bytes
    info.owner = owner_bytes
    info.lamports = lamports
    info.data = data
    info.write_version = write_version

    acc_upd = geyser_pb2.SubscribeUpdateAccount()
    acc_upd.account.CopyFrom(info)
    acc_upd.slot = slot
    acc_upd.is_startup = is_startup

    outer = geyser_pb2.SubscribeUpdate()
    outer.account.CopyFrom(acc_upd)
    return outer


def _make_slot_only_update(slot: int) -> object:
    outer = geyser_pb2.SubscribeUpdate()
    slot_upd = geyser_pb2.SubscribeUpdateSlot()
    slot_upd.slot = slot
    outer.slot.CopyFrom(slot_upd)
    return outer


class _FakeChannelStub:
    """Builds (mock_channel, mock_stub, captured_requests) for patched gRPC."""

    def __init__(self, updates_by_call: list[list]) -> None:
        # One list of updates per successive stub.Subscribe() call (to test reconnect).
        self._updates_by_call = updates_by_call
        self._call_index = 0
        self.captured_requests: list = []

    def build(self):
        async def _fake_subscribe(request_iter):
            async for req in request_iter:
                self.captured_requests.append(req)
            idx = min(self._call_index, len(self._updates_by_call) - 1)
            updates = self._updates_by_call[idx]
            self._call_index += 1
            for u in updates:
                if isinstance(u, Exception):
                    raise u
                yield u

        mock_stub = MagicMock()
        mock_stub.Subscribe = _fake_subscribe

        mock_channel = AsyncMock()
        mock_channel.__aenter__ = AsyncMock(return_value=mock_channel)
        mock_channel.__aexit__ = AsyncMock(return_value=False)
        return mock_channel, mock_stub


def _patch_grpc(mock_channel, mock_stub):
    import geyser_pb2_grpc  # type: ignore[import]
    grpc_patch = patch("grpc.aio.secure_channel", return_value=mock_channel)
    stub_patch = patch.object(geyser_pb2_grpc, "GeyserStub", return_value=mock_stub)
    return grpc_patch, stub_patch


class TestGeyserAccountSubscriptionSourceGracefulDegradation:
    def test_empty_endpoint_yields_nothing(self) -> None:
        source = GeyserAccountSubscriptionSource(endpoint="", x_token="tok")

        async def collect():
            return [u async for u in source.subscribe(frozenset([WALLET_A]))]

        assert _run(collect) == []
        assert source.is_connected is False

    def test_empty_wallets_yields_nothing(self) -> None:
        source = GeyserAccountSubscriptionSource(endpoint="fake:443", x_token="tok")

        async def collect():
            return [u async for u in source.subscribe(frozenset())]

        assert _run(collect) == []


class TestGeyserAccountSubscriptionSourceStreamPath:
    def test_valid_account_update_decoded(self) -> None:
        token_data = _make_token_account_data(_MINT_X_BYTES, _WALLET_A_BYTES, 777)
        update = _make_subscribe_update_account(
            pubkey_bytes=b"\x01" * 32, owner_bytes=_b58_decode(TOKEN_PROGRAM_ID),
            slot=LAUNCH_SLOT, data=token_data, lamports=2_039_280, write_version=5,
        )
        fake = _FakeChannelStub([[update]])
        mock_channel, mock_stub = fake.build()
        grpc_patch, stub_patch = _patch_grpc(mock_channel, mock_stub)

        source = GeyserAccountSubscriptionSource(
            endpoint="fake-endpoint:443", x_token="fake-token", max_reconnect_attempts=1,
        )

        async def run():
            with grpc_patch, stub_patch:
                return [u async for u in source.subscribe(frozenset([WALLET_A]))]

        collected = asyncio.run(run())
        assert len(collected) == 1
        raw = collected[0]
        assert raw.slot == LAUNCH_SLOT
        assert raw.lamports == 2_039_280
        assert raw.write_version == 5
        assert raw.is_startup is False
        decoded = _decode_spl_token_account(raw.data)
        assert decoded == (MINT_X, WALLET_A, 777)

    def test_slot_only_update_skipped(self) -> None:
        slot_update = _make_slot_only_update(LAUNCH_SLOT)
        token_data = _make_token_account_data(_MINT_X_BYTES, _WALLET_A_BYTES, 1)
        acct_update = _make_subscribe_update_account(
            pubkey_bytes=b"\x02" * 32, owner_bytes=_b58_decode(TOKEN_PROGRAM_ID),
            slot=LAUNCH_SLOT + 1, data=token_data,
        )
        fake = _FakeChannelStub([[slot_update, acct_update]])
        mock_channel, mock_stub = fake.build()
        grpc_patch, stub_patch = _patch_grpc(mock_channel, mock_stub)

        source = GeyserAccountSubscriptionSource(
            endpoint="fake-endpoint:443", x_token="fake-token", max_reconnect_attempts=1,
        )

        async def run():
            with grpc_patch, stub_patch:
                return [u async for u in source.subscribe(frozenset([WALLET_A]))]

        collected = asyncio.run(run())
        assert len(collected) == 1
        assert collected[0].slot == LAUNCH_SLOT + 1

    def test_captured_request_has_correct_filters(self) -> None:
        fake = _FakeChannelStub([[]])
        mock_channel, mock_stub = fake.build()
        grpc_patch, stub_patch = _patch_grpc(mock_channel, mock_stub)

        source = GeyserAccountSubscriptionSource(
            endpoint="fake-endpoint:443", x_token="fake-token", max_reconnect_attempts=1,
        )

        async def run():
            with grpc_patch, stub_patch:
                async for _ in source.subscribe(frozenset([WALLET_A, WALLET_B]), last_slot=299_000_000):
                    pass

        asyncio.run(run())
        assert len(fake.captured_requests) == 1
        req = fake.captured_requests[0]
        assert set(req.accounts["native"].account) == {WALLET_A, WALLET_B}
        assert req.from_slot == 299_000_000
        assert req.commitment == 0

    def test_is_connected_true_during_stream(self) -> None:
        token_data = _make_token_account_data(_MINT_X_BYTES, _WALLET_A_BYTES, 1)
        update = _make_subscribe_update_account(
            pubkey_bytes=b"\x03" * 32, owner_bytes=_b58_decode(TOKEN_PROGRAM_ID),
            slot=LAUNCH_SLOT, data=token_data,
        )
        fake = _FakeChannelStub([[update]])
        mock_channel, mock_stub = fake.build()
        grpc_patch, stub_patch = _patch_grpc(mock_channel, mock_stub)

        source = GeyserAccountSubscriptionSource(
            endpoint="fake-endpoint:443", x_token="fake-token", max_reconnect_attempts=1,
        )
        snapshots = []

        async def run():
            with grpc_patch, stub_patch:
                async for _ in source.subscribe(frozenset([WALLET_A])):
                    snapshots.append(source.is_connected)

        asyncio.run(run())
        assert snapshots == [True]
        assert source.is_connected is False


class TestGeyserAccountSubscriptionSourceResilience:
    """Self-check #5: kill and restore mid-stream; resume-from-slot; no dup."""

    def test_reconnect_after_grpc_error_resumes_from_last_slot(self) -> None:
        import grpc.aio

        first_update = _make_subscribe_update_account(
            pubkey_bytes=b"\x04" * 32, owner_bytes=_b58_decode(TOKEN_PROGRAM_ID),
            slot=LAUNCH_SLOT, data=_make_token_account_data(_MINT_X_BYTES, _WALLET_A_BYTES, 1),
        )
        # Fake a gRPC-level connection drop after the first update, mirroring
        # a real 'kill the connection mid-stream' event.
        rpc_error = grpc.aio.AioRpcError(
            grpc.StatusCode.UNAVAILABLE, None, None, "simulated connection drop",
        )
        second_update = _make_subscribe_update_account(
            pubkey_bytes=b"\x05" * 32, owner_bytes=_b58_decode(TOKEN_PROGRAM_ID),
            slot=LAUNCH_SLOT + 9, data=_make_token_account_data(_MINT_X_BYTES, _WALLET_A_BYTES, 2),
        )
        fake = _FakeChannelStub([[first_update, rpc_error], [second_update]])
        mock_channel, mock_stub = fake.build()
        grpc_patch, stub_patch = _patch_grpc(mock_channel, mock_stub)

        source = GeyserAccountSubscriptionSource(
            endpoint="fake-endpoint:443", x_token="fake-token", max_reconnect_attempts=2,
        )

        async def run():
            with grpc_patch, stub_patch, patch("asyncio.sleep", new=AsyncMock()):
                return [u async for u in source.subscribe(frozenset([WALLET_A]), last_slot=0)]

        collected = asyncio.run(run())

        # Both updates yielded exactly once each (no duplicate re-delivery).
        assert len(collected) == 2
        assert collected[0].slot == LAUNCH_SLOT
        assert collected[1].slot == LAUNCH_SLOT + 9

        # Two connect attempts: request captured for each; the SECOND request
        # resumes from_slot == the slot of the last update yielded before the
        # drop (LAUNCH_SLOT), proving resume-from-slot on reconnect.
        assert len(fake.captured_requests) == 2
        assert fake.captured_requests[0].from_slot == 0
        assert fake.captured_requests[1].from_slot == LAUNCH_SLOT

    def test_is_connected_false_after_error_true_after_recovery(self) -> None:
        import grpc.aio

        rpc_error = grpc.aio.AioRpcError(
            grpc.StatusCode.UNAVAILABLE, None, None, "simulated drop",
        )
        recovered_update = _make_subscribe_update_account(
            pubkey_bytes=b"\x06" * 32, owner_bytes=_b58_decode(TOKEN_PROGRAM_ID),
            slot=LAUNCH_SLOT + 3, data=_make_token_account_data(_MINT_X_BYTES, _WALLET_A_BYTES, 1),
        )
        fake = _FakeChannelStub([[rpc_error], [recovered_update]])
        mock_channel, mock_stub = fake.build()
        grpc_patch, stub_patch = _patch_grpc(mock_channel, mock_stub)

        source = GeyserAccountSubscriptionSource(
            endpoint="fake-endpoint:443", x_token="fake-token", max_reconnect_attempts=2,
        )
        connected_snapshots = []

        async def run():
            with grpc_patch, stub_patch, patch("asyncio.sleep", new=AsyncMock()):
                async for _ in source.subscribe(frozenset([WALLET_A])):
                    connected_snapshots.append(source.is_connected)

        asyncio.run(run())
        # Recovers to connected=True once the second stream yields data.
        assert connected_snapshots == [True]
        # And disconnects cleanly at the very end.
        assert source.is_connected is False

    def test_max_reconnect_attempts_gives_up_gracefully(self) -> None:
        import grpc.aio

        rpc_error = grpc.aio.AioRpcError(
            grpc.StatusCode.UNAVAILABLE, None, None, "always down",
        )
        fake = _FakeChannelStub([[rpc_error], [rpc_error], [rpc_error]])
        mock_channel, mock_stub = fake.build()
        grpc_patch, stub_patch = _patch_grpc(mock_channel, mock_stub)

        source = GeyserAccountSubscriptionSource(
            endpoint="fake-endpoint:443", x_token="fake-token", max_reconnect_attempts=2,
        )

        async def run():
            with grpc_patch, stub_patch, patch("asyncio.sleep", new=AsyncMock()):
                return [u async for u in source.subscribe(frozenset([WALLET_A]))]

        # Must not raise / hang -- gives up after max_reconnect_attempts.
        collected = asyncio.run(run())
        assert collected == []


def _b58_decode(s: str) -> bytes:
    """Local base58 decoder for test fixture construction (mirrors
    aats.ingestion.transport._base58_decode)."""
    from aats.ingestion.transport import _base58_decode as _real_decode
    return _real_decode(s)
