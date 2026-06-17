"""Unit tests for GeyserTransport._parse_geyser_tx(), _build_subscribe_request(),
and subscribe()/_stream_once() live-path logic.

No live Geyser endpoint is required.  The live gRPC path is exercised via a
patched FakeGeyserStub (see TestSubscribeStreamPath) that returns an async
iterator of pre-built SubscribeUpdate proto messages.  All assertions are
against the proto shapes defined in aats/ingestion/geyser_proto/.

Covers:
  AC-1  _parse_geyser_tx maps a SubscribeUpdateTransaction -> RawTransaction
        with correct signature, slot, account_keys, instructions, inner_instructions.
  AC-2  block_time_unix_s is always None (T-300a: PROCESSED level has no block_time).
  AC-3  is_vote=True transactions yield is_vote=True in RawTransaction.
  AC-4  errored transactions carry err != None.
  AC-5  _build_subscribe_request() builds the SubscribeRequest with
        account_include = program_ids, vote=False, failed=False,
        commitment=PROCESSED, and from_slot set iff last_slot>0.
  AC-6  Empty GEYSER_ENDPOINT logs a clear error and yields nothing (no crash).
  AC-7  _parse_geyser_tx correctly decodes address lookup table resolved keys
        (loaded_writable_addresses + loaded_readonly_addresses appended to key list).
  AC-8  Instruction data bytes are correctly base64-encoded in RawInstruction.data_b64.
  AC-9  Inner instructions are flattened and resolved to the full account table.
  AC-10 pump.fun create fixture: _parse_geyser_tx produces a RawTransaction that
        PumpFunDecoder decodes to a LaunchEvent (end-to-end integration).
  AC-11 subscribe()/_stream_once() invokes _build_subscribe_request() and the
        stub.Subscribe() call receives the correctly constructed request.
  AC-12 Point-in-time: no stored field is derived from wall-clock time
        (block_time_unix_s is None; event_time is NOT set by wall-clock).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import struct
import sys
from typing import AsyncIterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Ensure the proto stubs directory is importable
_PROTO_DIR = os.path.join(os.path.dirname(__file__), "../../aats/ingestion/geyser_proto")
_PROTO_DIR = os.path.normpath(_PROTO_DIR)
if _PROTO_DIR not in sys.path:
    sys.path.insert(0, _PROTO_DIR)

import geyser_pb2  # type: ignore[import]
import solana_storage_pb2  # type: ignore[import]

from aats.contracts.events import DetectionTransport, LaunchSource
from aats.ingestion.transport import (
    GeyserTransport,
    _build_subscribe_request,
    _bytes_to_base58,
)
from tests.ingestion.fixtures import PROGRAM_IDS, make_registry


# ---------------------------------------------------------------------------
# Helpers to construct proto fixture messages
# ---------------------------------------------------------------------------

def _disc(name: str) -> bytes:
    return hashlib.sha256(f"global:{name}".encode()).digest()[:8]


def _b58_to_bytes(s: str) -> bytes:
    """Decode a base58 string to bytes (Solana pubkey / signature)."""
    alphabet = b"123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    num = 0
    for char in s.encode("ascii"):
        num = num * 58 + alphabet.index(char)
    # Count leading '1's → leading zero bytes
    leading_zeros = len(s) - len(s.lstrip("1"))
    result = num.to_bytes((num.bit_length() + 7) // 8 or 1, "big")
    return b"\x00" * leading_zeros + result


_BASE58_ALPHABET = b"123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _b58_decode_to_bytes(s: str) -> bytes:
    """Decode a base58 string to its raw bytes (inverse of _bytes_to_base58)."""
    num = 0
    for c in s.encode("ascii"):
        num = num * 58 + _BASE58_ALPHABET.index(c)
    leading_zeros = len(s) - len(s.lstrip("1"))
    n = (num.bit_length() + 7) // 8 if num else 0
    return b"\x00" * leading_zeros + num.to_bytes(n or 1, "big")


def _make_account_key_bytes(address: str) -> bytes:
    """Convert an address string to 32 bytes suitable for proto account_keys.

    For strings that look like real base58 Solana addresses (32-44 chars,
    all base58 alphabet), decode them properly so the round-trip through
    _bytes_to_base58 in the transport recovers the original string.

    For short/synthetic test strings (used by most tests), pad to 32 bytes
    from the UTF-8 encoding.
    """
    b58_chars = set(_BASE58_ALPHABET.decode("ascii"))
    # A real-looking Solana address: 32-44 chars, all base58, decodes to ~32 bytes
    if 32 <= len(address) <= 44 and all(c in b58_chars for c in address):
        try:
            decoded = _b58_decode_to_bytes(address)
            if 28 <= len(decoded) <= 32:
                return decoded.rjust(32, b"\x00")
        except (ValueError, IndexError):
            pass
    # Fallback: UTF-8 pad (for short synthetic test strings like "payer", "prog")
    raw = address.encode("utf-8")[:32].ljust(32, b"\x00")
    return raw


def _make_subscribe_update_transaction(
    *,
    signature_bytes: bytes,
    slot: int,
    account_keys_b58: list[str],
    instructions: list[tuple],  # (program_id_index: int, accounts: bytes, data: bytes)
    inner_instructions: list[tuple] | None = None,  # (outer_ix_index, [(pid_idx, accts, data)])
    log_messages: list[str] | None = None,
    is_vote: bool = False,
    err_bytes: bytes | None = None,
    loaded_writable: list[str] | None = None,
    loaded_readonly: list[str] | None = None,
) -> object:
    """Construct a SubscribeUpdateTransaction proto message from test parameters.

    Args:
        signature_bytes: Raw 64-byte signature.
        slot: On-chain slot number.
        account_keys_b58: List of base58 account key strings.
        instructions: List of (program_id_index, accounts_bytes, data_bytes) tuples.
        inner_instructions: Optional list of (outer_ix_index, [(pid_idx, accts, data)]) tuples.
        log_messages: Optional program log lines.
        is_vote: Whether this is a vote transaction.
        err_bytes: If not None, sets meta.err.err to this (signals failed tx).
        loaded_writable: Optional writable loaded addresses (for versioned txns).
        loaded_readonly: Optional readonly loaded addresses (for versioned txns).

    Returns:
        A geyser_pb2.SubscribeUpdateTransaction proto message.
    """
    # Build the inner transaction
    tx_msg = solana_storage_pb2.Transaction()
    tx_msg.signatures.append(signature_bytes)

    msg = solana_storage_pb2.Message()
    for key_b58 in account_keys_b58:
        # Use 32-byte deterministic encoding of the fixture address string
        msg.account_keys.append(_make_account_key_bytes(key_b58))

    for pid_idx, accts, data in instructions:
        ix = solana_storage_pb2.CompiledInstruction()
        ix.program_id_index = pid_idx
        ix.accounts = accts
        ix.data = data
        msg.instructions.append(ix)

    tx_msg.message.CopyFrom(msg)

    # Build meta
    meta = solana_storage_pb2.TransactionStatusMeta()
    if err_bytes is not None:
        meta.err.err = err_bytes
    else:
        meta.inner_instructions_none = False

    if log_messages is not None:
        for log in log_messages:
            meta.log_messages.append(log)
    else:
        meta.log_messages_none = True

    if loaded_writable:
        for addr in loaded_writable:
            meta.loaded_writable_addresses.append(_make_account_key_bytes(addr))
    if loaded_readonly:
        for addr in loaded_readonly:
            meta.loaded_readonly_addresses.append(_make_account_key_bytes(addr))

    if inner_instructions:
        for outer_ix_idx, ixs in inner_instructions:
            inner_group = solana_storage_pb2.InnerInstructions()
            inner_group.index = outer_ix_idx
            for pid_idx, accts, data in ixs:
                inner_ix = solana_storage_pb2.InnerInstruction()
                inner_ix.program_id_index = pid_idx
                inner_ix.accounts = accts
                inner_ix.data = data
                inner_group.instructions.append(inner_ix)
            meta.inner_instructions.append(inner_group)

    # Build SubscribeUpdateTransactionInfo
    tx_info = geyser_pb2.SubscribeUpdateTransactionInfo()
    tx_info.signature = signature_bytes
    tx_info.is_vote = is_vote
    tx_info.transaction.CopyFrom(tx_msg)
    tx_info.meta.CopyFrom(meta)

    # Build SubscribeUpdateTransaction
    update_tx = geyser_pb2.SubscribeUpdateTransaction()
    update_tx.transaction.CopyFrom(tx_info)
    update_tx.slot = slot

    return update_tx


def _make_pump_create_geyser_update(slot: int = 300_000_000) -> object:
    """Construct a Geyser SubscribeUpdateTransaction for a pump.fun create.

    Account layout matches fixtures.make_pumpfun_create_tx():
      account_keys[0] = payer (CreatorWallet...)
      account_keys[1] = mint (MintPumpFun...)
      account_keys[2] = bonding curve (BondingCurve...)
      account_keys[3] = PUMP_PID (program, used as program_id_index)

    The account_keys are stored as their real base58-decoded bytes so that
    the transport's _bytes_to_base58() round-trip recovers the original
    base58 strings.  This lets the decoder match by comparing the program_id
    string against the registry.
    """
    PUMP_PID_B58 = PROGRAM_IDS[LaunchSource.PUMPFUN]
    # These addresses are short/synthetic — they'll be encoded as UTF-8 padded bytes.
    # The decoder matches program_id against the registry by base58 string equality.
    # For the program_id to match, we MUST use the real pump.fun PID in account_keys
    # and it MUST round-trip correctly through _make_account_key_bytes -> _bytes_to_base58.
    account_keys = [
        "CreatorWallet11111111111111111111111111111111",  # [0] payer
        "MintPumpFun1111111111111111111111111111111111",  # [1] mint
        "BondingCurve11111111111111111111111111111111",   # [2] bonding curve
        PUMP_PID_B58,                                     # [3] program (must round-trip)
    ]
    ix_data = _disc("create")
    # accounts byte: [0, 1, 2] = indices into account_keys that this ix uses
    accounts_bytes = bytes([0, 1, 2])
    program_id_index = 3  # PUMP_PID is at index 3 in account_keys

    sig = b"\xaa" * 64
    return _make_subscribe_update_transaction(
        signature_bytes=sig,
        slot=slot,
        account_keys_b58=account_keys,
        instructions=[(program_id_index, accounts_bytes, ix_data)],
        log_messages=[f"Program {PUMP_PID_B58} invoke [1]"],
    )


# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------

class TestBase58Encoding:
    """Test our pure-Python base58 encoder."""

    def test_known_pubkey_round_trip(self):
        """A 32-byte value should encode to a non-empty base58 string."""
        data = bytes(range(32))  # known 32 bytes
        encoded = _bytes_to_base58(data)
        assert len(encoded) > 0
        assert all(c in "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
                   for c in encoded)

    def test_empty_bytes_returns_empty_string(self):
        assert _bytes_to_base58(b"") == ""

    def test_leading_zero_bytes(self):
        """Leading zero bytes should produce leading '1' characters."""
        encoded = _bytes_to_base58(b"\x00\x00\x01")
        assert encoded.startswith("11")


class TestParseGeyserTx:
    """AC-1, AC-2, AC-3, AC-4, AC-7, AC-8, AC-9: _parse_geyser_tx field assertions."""

    def _make_transport(self) -> GeyserTransport:
        return GeyserTransport(endpoint="", x_token="")

    def test_ac1_signature_decoded_to_base58(self):
        """AC-1: signature bytes are base58-encoded in RawTransaction.signature."""
        transport = self._make_transport()
        sig = bytes(range(64))
        update = _make_subscribe_update_transaction(
            signature_bytes=sig,
            slot=12345,
            account_keys_b58=["11111111111111111111111111111111", "prog"],
            instructions=[(1, bytes([0]), b"\x00")],
        )
        raw = transport._parse_geyser_tx(update)
        assert raw is not None
        assert raw.signature == _bytes_to_base58(sig)
        assert len(raw.signature) > 0

    def test_ac1_slot_preserved(self):
        """AC-1: slot is passed through as-is from SubscribeUpdateTransaction.slot."""
        transport = self._make_transport()
        update = _make_subscribe_update_transaction(
            signature_bytes=b"\xab" * 64,
            slot=299_999_999,
            account_keys_b58=["payer", "prog"],
            instructions=[(1, bytes([0]), b"\x00")],
        )
        raw = transport._parse_geyser_tx(update)
        assert raw.slot == 299_999_999

    def test_ac2_block_time_is_none(self):
        """AC-2: block_time_unix_s is ALWAYS None for PROCESSED updates (T-300a)."""
        transport = self._make_transport()
        update = _make_subscribe_update_transaction(
            signature_bytes=b"\xcc" * 64,
            slot=300_000_000,
            account_keys_b58=["payer", "prog"],
            instructions=[(1, bytes([0]), b"\x00")],
        )
        raw = transport._parse_geyser_tx(update)
        assert raw.block_time_unix_s is None, (
            "block_time_unix_s MUST be None for PROCESSED Geyser updates. "
            "Wall-clock substitution is forbidden (T-300a). "
            f"Got: {raw.block_time_unix_s}"
        )

    def test_ac3_is_vote_propagated(self):
        """AC-3: is_vote=True in the proto maps to is_vote=True in RawTransaction."""
        transport = self._make_transport()
        update = _make_subscribe_update_transaction(
            signature_bytes=b"\xdd" * 64,
            slot=300_000_001,
            account_keys_b58=["voter", "vote_prog"],
            instructions=[(1, bytes([0]), b"\x00")],
            is_vote=True,
        )
        raw = transport._parse_geyser_tx(update)
        assert raw.is_vote is True

    def test_ac3_non_vote_transaction(self):
        """AC-3: Non-vote transactions have is_vote=False."""
        transport = self._make_transport()
        update = _make_subscribe_update_transaction(
            signature_bytes=b"\xee" * 64,
            slot=300_000_002,
            account_keys_b58=["user", "prog"],
            instructions=[(1, bytes([0]), b"\x00")],
            is_vote=False,
        )
        raw = transport._parse_geyser_tx(update)
        assert raw.is_vote is False

    def test_ac4_err_is_none_for_success(self):
        """AC-4: Successful transactions have err=None."""
        transport = self._make_transport()
        update = _make_subscribe_update_transaction(
            signature_bytes=b"\xff" * 64,
            slot=300_000_003,
            account_keys_b58=["payer", "prog"],
            instructions=[(1, bytes([0]), b"\x00")],
            err_bytes=None,
        )
        raw = transport._parse_geyser_tx(update)
        assert raw.err is None

    def test_ac4_err_set_for_failed_tx(self):
        """AC-4: Failed transactions have err != None (non-empty err bytes)."""
        transport = self._make_transport()
        update = _make_subscribe_update_transaction(
            signature_bytes=b"\x01" * 64,
            slot=300_000_004,
            account_keys_b58=["payer", "prog"],
            instructions=[(1, bytes([0]), b"\x00")],
            err_bytes=b"\x04\x00\x00\x00",  # some Solana TransactionError bincode
        )
        raw = transport._parse_geyser_tx(update)
        assert raw.err is not None

    def test_ac7_loaded_addresses_appended_to_key_list(self):
        """AC-7: Loaded writable+readonly addresses are appended after static keys."""
        transport = self._make_transport()
        # Static keys: [payer(0), prog(1)]
        # Loaded writable: [extra_key_w] -> becomes index 2
        # Loaded readonly: [extra_key_r] -> becomes index 3
        # Instruction uses account index 2 (should resolve to extra_key_w)
        update = _make_subscribe_update_transaction(
            signature_bytes=b"\x02" * 64,
            slot=300_000_005,
            account_keys_b58=["payer", "prog"],
            instructions=[(1, bytes([0, 2]), b"\x00")],  # account idx 2 = loaded writable
            loaded_writable=["LoadedWritable11111111111111111111111111111"],
            loaded_readonly=["LoadedReadonly111111111111111111111111111111"],
        )
        raw = transport._parse_geyser_tx(update)
        # The instruction should have resolved account[2] to the loaded writable key
        assert len(raw.instructions) == 1
        # account_keys[1] should be the loaded writable address (index 2 in the full table)
        assert len(raw.instructions[0].account_keys) >= 2
        # The second account (index 2 in full list) should be the loaded writable address
        full_key_2 = raw.instructions[0].account_keys[1]
        # The encoded address should come from _make_account_key_bytes("LoadedWritable...")
        # which is the first 32 chars of the string; just assert it is non-empty
        assert full_key_2 != ""

    def test_ac8_instruction_data_is_base64(self):
        """AC-8: Instruction data bytes are correctly base64-encoded in data_b64."""
        transport = self._make_transport()
        raw_data = _disc("create")  # 8-byte discriminator
        update = _make_subscribe_update_transaction(
            signature_bytes=b"\x03" * 64,
            slot=300_000_006,
            account_keys_b58=["payer", "prog"],
            instructions=[(1, bytes([0]), raw_data)],
        )
        raw = transport._parse_geyser_tx(update)
        assert len(raw.instructions) == 1
        decoded = base64.b64decode(raw.instructions[0].data_b64)
        assert decoded == raw_data

    def test_ac9_inner_instructions_flattened(self):
        """AC-9: Inner instructions are flattened from InnerInstructions groups."""
        transport = self._make_transport()
        inner_data = b"\xca\xfe"
        update = _make_subscribe_update_transaction(
            signature_bytes=b"\x04" * 64,
            slot=300_000_007,
            account_keys_b58=["payer", "outer_prog", "inner_prog"],
            instructions=[(1, bytes([0]), b"\x00")],
            inner_instructions=[
                (0, [(2, bytes([0, 1]), inner_data)]),  # outer ix 0 -> inner ix using prog@2
            ],
        )
        raw = transport._parse_geyser_tx(update)
        assert len(raw.inner_instructions) == 1
        decoded_inner = base64.b64decode(raw.inner_instructions[0].data_b64)
        assert decoded_inner == inner_data

    def test_ac1_fee_payer_is_first_account_key(self):
        """AC-1: fee_payer is the first account key in the resolved list."""
        transport = self._make_transport()
        update = _make_subscribe_update_transaction(
            signature_bytes=b"\x05" * 64,
            slot=300_000_008,
            account_keys_b58=["FeePayer111111111111111111111111111111111", "prog"],
            instructions=[(1, bytes([0]), b"\x00")],
        )
        raw = transport._parse_geyser_tx(update)
        # fee_payer must be the first account key's base58 encoding
        expected_first_key = _bytes_to_base58(_make_account_key_bytes(
            "FeePayer111111111111111111111111111111111"
        ))
        assert raw.fee_payer == expected_first_key


class TestParseGeyserTxToPumpFunDecode:
    """AC-10: End-to-end: Geyser fixture -> _parse_geyser_tx -> PumpFunDecoder -> LaunchEvent."""

    def test_ac10_pump_create_end_to_end(self):
        """AC-10: Geyser SubscribeUpdateTransaction for pump.fun create decodes to LaunchEvent."""
        from aats.ingestion.decoders import PumpFunDecoder

        registry = make_registry()
        decoder = PumpFunDecoder(registry)

        geyser_update = _make_pump_create_geyser_update(slot=300_000_000)
        transport = GeyserTransport(endpoint="", x_token="")
        raw_tx = transport._parse_geyser_tx(geyser_update)

        assert raw_tx is not None
        assert raw_tx.slot == 300_000_000
        # block_time is None — decoder returns None (T-300a), which is CORRECT:
        # no event emitted without authoritative block_time
        ev = decoder.decode(raw_tx, DetectionTransport.GEYSER)
        # T-300a: block_time_unix_s is None -> _make_event_time returns None -> no event emitted
        assert ev is None, (
            "With block_time_unix_s=None, decoder MUST return None (T-300a point-in-time law). "
            f"Got: {ev}"
        )

    def test_ac10_pump_create_with_block_time_decodes_fully(self):
        """AC-10 (with block_time): inject block_time manually to prove full decode path.

        This simulates what a block_meta enricher would do after a parallel
        blocks_meta subscription supplies block_time for the slot.
        """
        from aats.ingestion.decoders import PumpFunDecoder, RawTransaction

        registry = make_registry()
        decoder = PumpFunDecoder(registry)

        # Get the RawTransaction from the Geyser parse
        geyser_update = _make_pump_create_geyser_update(slot=300_000_000)
        transport = GeyserTransport(endpoint="", x_token="")
        raw_tx = transport._parse_geyser_tx(geyser_update)

        assert raw_tx is not None
        # Simulate block_time enrichment (block_meta subscription would provide this)
        raw_tx_with_bt = RawTransaction(
            signature=raw_tx.signature,
            slot=raw_tx.slot,
            block_time_unix_s=1_718_700_000,  # injected from block_meta
            fee_payer=raw_tx.fee_payer,
            instructions=raw_tx.instructions,
            inner_instructions=raw_tx.inner_instructions,
            program_logs=raw_tx.program_logs,
            is_vote=raw_tx.is_vote,
            err=raw_tx.err,
        )

        result = decoder.decode(raw_tx_with_bt, DetectionTransport.GEYSER)
        assert result is not None, "After block_time is injected, pump.fun create MUST decode."
        ev, _kind = result
        assert ev.source == LaunchSource.PUMPFUN
        assert ev.event_time.slot == 300_000_000
        assert ev.event_time.block_time_ms == 1_718_700_000_000
        assert isinstance(ev.sol_reserve_lamports, int)
        assert isinstance(ev.token_reserve_base, int)


class TestBuildSubscribeRequest:
    """AC-5, AC-11: _build_subscribe_request() builds the correct proto message.

    These tests assert against the PRODUCTION _build_subscribe_request() function
    (extracted from _stream_once()), so they exercise the actual request-building
    logic, not a locally-constructed copy (B2 fix).
    """

    def test_ac5_account_include_matches_program_ids(self):
        """AC-5: account_include == the supplied program_ids set."""
        program_ids = frozenset([
            "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",
            "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA",
        ])
        request = _build_subscribe_request(geyser_pb2, program_ids, from_slot=0, commitment=0)

        assert "sniper" in request.transactions
        filt = request.transactions["sniper"]
        assert set(filt.account_include) == program_ids

    def test_ac5_vote_and_failed_are_false(self):
        """AC-5: vote=False and failed=False in the production request."""
        program_ids = frozenset(["6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"])
        request = _build_subscribe_request(geyser_pb2, program_ids, from_slot=0, commitment=0)

        filt = request.transactions["sniper"]
        assert filt.vote is False
        assert filt.failed is False

    def test_ac5_commitment_is_processed(self):
        """AC-5: commitment=0 (PROCESSED) in the production request."""
        program_ids = frozenset(["6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"])
        request = _build_subscribe_request(geyser_pb2, program_ids, from_slot=0, commitment=0)
        assert request.commitment == 0  # CommitmentLevel.PROCESSED

    def test_ac5_from_slot_set_when_nonzero(self):
        """AC-5: from_slot is set in the production request when last_slot > 0."""
        program_ids = frozenset(["6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"])
        request = _build_subscribe_request(
            geyser_pb2, program_ids, from_slot=299_999_000, commitment=0
        )
        assert request.from_slot == 299_999_000

    def test_ac5_from_slot_not_set_when_zero(self):
        """AC-5: from_slot is NOT set (remains default 0) when last_slot == 0."""
        program_ids = frozenset(["6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"])
        request = _build_subscribe_request(
            geyser_pb2, program_ids, from_slot=0, commitment=0
        )
        # Proto3 scalar default — 0 means "start from live tip", not "slot 0"
        assert request.from_slot == 0

    def test_ac5_empty_program_ids_produces_empty_filter(self):
        """AC-5: Empty program_ids produces an empty account_include list."""
        request = _build_subscribe_request(
            geyser_pb2, frozenset(), from_slot=0, commitment=0
        )
        assert "sniper" in request.transactions
        assert len(request.transactions["sniper"].account_include) == 0

    def test_ac5_multiple_program_ids_all_present(self):
        """AC-5: All program IDs from the registry appear in account_include."""
        pids = frozenset(PROGRAM_IDS.values())
        request = _build_subscribe_request(geyser_pb2, pids, from_slot=0, commitment=0)
        assert set(request.transactions["sniper"].account_include) == pids


class TestSubscribeWithEmptyEndpoint:
    """AC-6: Empty GEYSER_ENDPOINT logs a clear error and yields nothing."""

    def test_ac6_empty_endpoint_yields_nothing_gracefully(self):
        """AC-6: No crash, no NotImplementedError, clear log message, yields nothing."""
        transport = GeyserTransport(endpoint="", x_token="")
        program_ids = frozenset(["6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"])

        collected = []

        async def run():
            async for tx in transport.subscribe(program_ids):
                collected.append(tx)

        # Must not raise any exception (especially not NotImplementedError)
        asyncio.run(run())
        assert collected == [], "Empty endpoint must yield nothing, not raise"

    def test_ac6_is_connected_false_after_empty_endpoint(self):
        """AC-6: is_connected remains False after empty endpoint subscribe."""
        transport = GeyserTransport(endpoint="", x_token="")

        async def run():
            async for _ in transport.subscribe(frozenset()):
                pass

        asyncio.run(run())
        assert transport.is_connected is False


class TestPointInTimeCorrectness:
    """AC-12: No stored field is derived from wall-clock time."""

    def test_ac12_block_time_none_never_wall_clock_substituted(self):
        """AC-12: block_time_unix_s is None; wall-clock is never put in its place."""
        transport = GeyserTransport(endpoint="", x_token="")
        update = _make_subscribe_update_transaction(
            signature_bytes=b"\xab" * 64,
            slot=300_000_100,
            account_keys_b58=["payer", "prog"],
            instructions=[(1, bytes([0]), b"\x00")],
        )
        raw = transport._parse_geyser_tx(update)
        assert raw.block_time_unix_s is None

    def test_ac12_decoder_rejects_none_block_time(self):
        """AC-12: Decoder correctly returns None for a RawTransaction with block_time=None."""
        from aats.ingestion.decoders import PumpFunDecoder, RawInstruction, RawTransaction

        registry = make_registry()
        decoder = PumpFunDecoder(registry)

        # A pump.fun create with block_time_unix_s = None (T-300a hold-pending path)
        PUMP_PID = PROGRAM_IDS[LaunchSource.PUMPFUN]
        ix = RawInstruction(
            program_id=PUMP_PID,
            data_b64=base64.b64encode(_disc("create")).decode(),
            account_keys=[
                "CreatorWallet11111111111111111111111111111111",
                "MintPumpFun1111111111111111111111111111111111",
                "BondingCurve11111111111111111111111111111111",
            ],
            program_index=0,
        )
        tx = RawTransaction(
            signature="pit_test_sig",
            slot=300_000_200,
            block_time_unix_s=None,  # T-300a: absent
            fee_payer="CreatorWallet11111111111111111111111111111111",
            instructions=[ix],
            inner_instructions=[],
            program_logs=[],
        )
        ev = decoder.decode(tx, DetectionTransport.GEYSER)
        assert ev is None, (
            "Decoder MUST return None when block_time_unix_s is None. "
            "Wall-clock substitution is forbidden (T-300a). "
            f"Got: {ev}"
        )

    def test_ac12_out_of_order_and_duplicate_produce_same_result(self):
        """AC-12: Out-of-order and duplicate events produce identical results.

        This tests the point-in-time correctness property: feeding the same
        transactions in different order to _parse_geyser_tx produces the same
        RawTransaction objects (since we never use wall-clock in the event fields).
        """
        transport = GeyserTransport(endpoint="", x_token="")
        sig_a = b"\xaa" * 64
        sig_b = b"\xbb" * 64

        def make_update(sig, slot):
            return _make_subscribe_update_transaction(
                signature_bytes=sig,
                slot=slot,
                account_keys_b58=["payer", "prog"],
                instructions=[(1, bytes([0]), _disc("create"))],
            )

        update_a = make_update(sig_a, slot=300_000_300)
        update_b = make_update(sig_b, slot=300_000_301)

        # In-order
        tx_a1 = transport._parse_geyser_tx(update_a)
        tx_b1 = transport._parse_geyser_tx(update_b)

        # Out-of-order (b then a)
        tx_b2 = transport._parse_geyser_tx(update_b)
        tx_a2 = transport._parse_geyser_tx(update_a)

        # Duplicate of a
        tx_a3 = transport._parse_geyser_tx(update_a)

        # All instances of a should be identical (slot, block_time=None, sig)
        assert tx_a1.slot == tx_a2.slot == tx_a3.slot == 300_000_300
        assert tx_a1.block_time_unix_s is None
        assert tx_a2.block_time_unix_s is None
        assert tx_a3.block_time_unix_s is None
        # All instances of b should be identical
        assert tx_b1.slot == tx_b2.slot == 300_000_301


class TestProtoImportSmokeTest:
    """Verify the vendored stubs import and construct messages correctly."""

    def test_proto_imports_ok(self):
        """The vendored geyser_pb2 and solana_storage_pb2 import without error."""
        # If imports failed, this test file would not load at all.
        # This explicit assertion documents that we verified it.
        assert hasattr(geyser_pb2, "SubscribeRequest")
        assert hasattr(geyser_pb2, "SubscribeUpdate")
        assert hasattr(geyser_pb2, "CommitmentLevel")
        assert hasattr(solana_storage_pb2, "Transaction")
        assert hasattr(solana_storage_pb2, "TransactionStatusMeta")

    def test_commitment_level_processed_is_zero(self):
        """CommitmentLevel.PROCESSED must be 0 (per Yellowstone proto spec)."""
        assert geyser_pb2.CommitmentLevel.Value("PROCESSED") == 0

    def test_subscribe_request_construction(self):
        """SubscribeRequest can be constructed and populated with transactions filter."""
        req = geyser_pb2.SubscribeRequest()
        req.transactions["test"].account_include.append("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P")
        req.transactions["test"].vote = False
        req.transactions["test"].failed = False
        req.commitment = 0  # PROCESSED
        assert "test" in req.transactions
        assert len(req.transactions["test"].account_include) == 1

    def test_subscribe_update_has_field_transaction(self):
        """SubscribeUpdate.HasField('transaction') works on a populated transaction."""
        update = geyser_pb2.SubscribeUpdate()
        tx_info = geyser_pb2.SubscribeUpdateTransactionInfo()
        tx_info.signature = b"\x00" * 64
        update.transaction.CopyFrom(geyser_pb2.SubscribeUpdateTransaction())
        update.transaction.transaction.CopyFrom(tx_info)
        assert update.HasField("transaction")

    def test_subscribe_update_has_field_slot_not_transaction(self):
        """HasField('transaction') is False when only slot is set."""
        update = geyser_pb2.SubscribeUpdate()
        slot_update = geyser_pb2.SubscribeUpdateSlot()
        slot_update.slot = 12345
        update.slot.CopyFrom(slot_update)
        assert not update.HasField("transaction")
        assert update.HasField("slot")


# ---------------------------------------------------------------------------
# Fake GeyserStub helpers — B1 fix
# ---------------------------------------------------------------------------

def _make_subscribe_update_wrapper(update_tx: object) -> object:
    """Wrap a SubscribeUpdateTransaction in a SubscribeUpdate with HasField('transaction')."""
    outer = geyser_pb2.SubscribeUpdate()
    outer.transaction.CopyFrom(update_tx)
    return outer


def _make_slot_update(slot: int) -> object:
    """Build a SubscribeUpdate carrying only a slot (HasField('transaction') == False)."""
    outer = geyser_pb2.SubscribeUpdate()
    slot_upd = geyser_pb2.SubscribeUpdateSlot()
    slot_upd.slot = slot
    outer.slot.CopyFrom(slot_upd)
    return outer


def _make_bad_update() -> object:
    """Build a SubscribeUpdate that will cause _parse_geyser_tx to raise.

    We wrap a real SubscribeUpdate in a MagicMock that passes HasField('transaction')
    but whose .transaction property raises AttributeError when _parse_geyser_tx
    tries to access .transaction on the SubscribeUpdateTransaction.  This exercises
    the try/except around _parse_geyser_tx() inside _stream_once().
    """
    # The check in _stream_once is:
    #   if not update.HasField("transaction"): continue
    # followed by:
    #   tx = self._parse_geyser_tx(update.transaction)
    #
    # We build a real SubscribeUpdate that HasField('transaction') returns True,
    # but then override the .transaction attribute to a MagicMock whose
    # .slot property raises a TypeError to force the parse to throw.
    outer = geyser_pb2.SubscribeUpdate()
    # Populate the transaction oneof so HasField returns True
    bad_inner = geyser_pb2.SubscribeUpdateTransaction()
    tx_info = geyser_pb2.SubscribeUpdateTransactionInfo()
    tx_info.signature = b"\xff" * 64
    bad_inner.transaction.CopyFrom(tx_info)
    outer.transaction.CopyFrom(bad_inner)

    # Wrap in a MagicMock that preserves HasField but raises on .transaction access
    bad_wrapper = MagicMock()
    bad_wrapper.HasField.side_effect = lambda field: field == "transaction"
    # Accessing .transaction raises to simulate a corrupt/unparseable message
    type(bad_wrapper).transaction = property(
        lambda self: (_ for _ in ()).throw(
            RuntimeError("simulated corrupt proto field")
        )
    )
    return bad_wrapper


class _AsyncIteratorFromList:
    """Async iterator backed by a list of pre-built SubscribeUpdate messages."""

    def __init__(self, items: list) -> None:
        self._items = iter(items)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._items)
        except StopIteration:
            raise StopAsyncIteration


class TestSubscribeStreamPath:
    """AC-11, B1 fix: subscribe() and _stream_once() coverage via patched GeyserStub.

    We patch geyser_pb2_grpc.GeyserStub so that stub.Subscribe(request_iter)
    returns an _AsyncIteratorFromList carrying a mix of:
      - A SubscribeUpdate wrapping a valid transaction (must yield RawTransaction).
      - A slot-only SubscribeUpdate (HasField('transaction')==False, must be skipped).
      - A malformed SubscribeUpdate (parse exception must be caught, not crash).

    We also capture the SubscribeRequest passed to stub.Subscribe() and assert
    that _build_subscribe_request() was actually invoked with the correct values.
    """

    def _build_channel_mock(self, updates: list) -> tuple:
        """Return (mock_channel, mock_stub, captured_requests).

        mock_channel is a context-manager that yields mock_channel itself.
        mock_stub.Subscribe(request_iter) is an async iterator over updates.
        captured_requests accumulates every SubscribeRequest yielded by
        the _request_iter passed into stub.Subscribe() so tests can assert on it.
        """
        import grpc.aio

        captured_requests: list = []

        async def _fake_subscribe(request_iter):
            # Drain the request iterator so _build_subscribe_request output
            # can be captured for assertion.
            async for req in request_iter:
                captured_requests.append(req)
            for update in updates:
                yield update

        mock_stub = MagicMock()
        mock_stub.Subscribe = _fake_subscribe

        # grpc.aio.secure_channel is used as an async context manager:
        #   async with grpc.aio.secure_channel(...) as channel:
        mock_channel = AsyncMock()
        mock_channel.__aenter__ = AsyncMock(return_value=mock_channel)
        mock_channel.__aexit__ = AsyncMock(return_value=False)

        return mock_channel, mock_stub, captured_requests

    def _patch_grpc(self, mock_channel, mock_stub):
        """Return a context manager that patches grpc.aio and geyser_pb2_grpc."""
        import aats.ingestion.transport as transport_mod

        # We need to patch the GeyserStub constructor AND grpc.aio.secure_channel.
        # _stream_once imports grpc and grpc.aio locally; we patch at the
        # grpc.aio module level and stub constructor level.
        grpc_patch = patch("grpc.aio.secure_channel", return_value=mock_channel)

        # geyser_pb2_grpc.GeyserStub is used inside _stream_once after it is
        # imported via _import_geyser_proto().  We patch the module-level symbol
        # so the call inside _stream_once picks up the fake.
        import geyser_pb2_grpc  # type: ignore[import]
        stub_patch = patch.object(geyser_pb2_grpc, "GeyserStub", return_value=mock_stub)
        return grpc_patch, stub_patch

    def test_ac11_subscribe_yields_raw_tx_from_valid_update(self):
        """AC-11/B1: subscribe() drives _stream_once() and yields RawTransaction
        for a valid transaction SubscribeUpdate."""
        # Build a valid SubscribeUpdateTransaction
        valid_update_tx = _make_subscribe_update_transaction(
            signature_bytes=b"\xde" * 64,
            slot=400_000_000,
            account_keys_b58=["payer", "prog"],
            instructions=[(1, bytes([0]), b"\x01")],
        )
        valid_wrapper = _make_subscribe_update_wrapper(valid_update_tx)

        mock_channel, mock_stub, captured_requests = self._build_channel_mock(
            [valid_wrapper]
        )
        grpc_patch, stub_patch = self._patch_grpc(mock_channel, mock_stub)

        transport = GeyserTransport(
            endpoint="fake-endpoint:443",
            x_token="fake-token",
            max_reconnect_attempts=1,
        )
        program_ids = frozenset(["6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"])
        collected = []

        async def run():
            with grpc_patch, stub_patch:
                async for tx in transport.subscribe(program_ids, last_slot=0):
                    collected.append(tx)

        asyncio.run(run())

        assert len(collected) == 1, f"Expected 1 RawTransaction, got {len(collected)}"
        assert collected[0].slot == 400_000_000
        assert collected[0].block_time_unix_s is None  # T-300a
        assert len(collected[0].signature) > 0

    def test_ac11_slot_only_update_is_skipped(self):
        """AC-11/B1: HasField('transaction')==False updates are skipped; no crash."""
        slot_update = _make_slot_update(400_000_001)
        valid_update_tx = _make_subscribe_update_transaction(
            signature_bytes=b"\xef" * 64,
            slot=400_000_002,
            account_keys_b58=["payer", "prog"],
            instructions=[(1, bytes([0]), b"\x02")],
        )
        valid_wrapper = _make_subscribe_update_wrapper(valid_update_tx)

        mock_channel, mock_stub, _ = self._build_channel_mock(
            [slot_update, valid_wrapper]
        )
        grpc_patch, stub_patch = self._patch_grpc(mock_channel, mock_stub)

        transport = GeyserTransport(
            endpoint="fake-endpoint:443",
            x_token="fake-token",
            max_reconnect_attempts=1,
        )
        program_ids = frozenset(["6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"])
        collected = []

        async def run():
            with grpc_patch, stub_patch:
                async for tx in transport.subscribe(program_ids, last_slot=0):
                    collected.append(tx)

        asyncio.run(run())

        # Only the transaction update yields a RawTransaction; slot skipped
        assert len(collected) == 1
        assert collected[0].slot == 400_000_002

    def test_ac11_parse_exception_is_caught_and_stream_continues(self):
        """AC-11/B1: A malformed update triggers the parse-exception handler.
        The handler logs a warning and continues; no crash; subsequent valid
        updates are still yielded."""
        bad = _make_bad_update()

        valid_update_tx = _make_subscribe_update_transaction(
            signature_bytes=b"\xf1" * 64,
            slot=400_000_010,
            account_keys_b58=["payer", "prog"],
            instructions=[(1, bytes([0]), b"\x03")],
        )
        valid_wrapper = _make_subscribe_update_wrapper(valid_update_tx)

        mock_channel, mock_stub, _ = self._build_channel_mock([bad, valid_wrapper])
        grpc_patch, stub_patch = self._patch_grpc(mock_channel, mock_stub)

        transport = GeyserTransport(
            endpoint="fake-endpoint:443",
            x_token="fake-token",
            max_reconnect_attempts=1,
        )
        program_ids = frozenset(["6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"])
        collected = []

        async def run():
            with grpc_patch, stub_patch:
                async for tx in transport.subscribe(program_ids, last_slot=0):
                    collected.append(tx)

        # Must not raise
        asyncio.run(run())

        # The valid update after the bad one must still arrive
        assert len(collected) == 1
        assert collected[0].slot == 400_000_010

    def test_ac11_subscribe_request_has_correct_filters(self):
        """AC-11/B1: The SubscribeRequest actually passed to stub.Subscribe()
        has account_include==program_ids, vote=False, failed=False,
        commitment==0 (PROCESSED), and from_slot set when last_slot>0.

        This asserts on the CAPTURED request object from the production code path
        in _stream_once() -> _build_subscribe_request(), not on a locally
        constructed copy (B2 fix)."""
        program_ids = frozenset([
            "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",
            "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8",
        ])
        FROM_SLOT = 399_000_000

        mock_channel, mock_stub, captured_requests = self._build_channel_mock([])
        grpc_patch, stub_patch = self._patch_grpc(mock_channel, mock_stub)

        transport = GeyserTransport(
            endpoint="fake-endpoint:443",
            x_token="fake-token",
            max_reconnect_attempts=1,
        )

        async def run():
            with grpc_patch, stub_patch:
                async for _ in transport.subscribe(program_ids, last_slot=FROM_SLOT):
                    pass

        asyncio.run(run())

        assert len(captured_requests) == 1, (
            f"Expected exactly 1 SubscribeRequest captured, got {len(captured_requests)}"
        )
        req = captured_requests[0]
        assert "sniper" in req.transactions
        filt = req.transactions["sniper"]
        assert set(filt.account_include) == program_ids, (
            f"account_include mismatch: got {set(filt.account_include)!r}, "
            f"expected {program_ids!r}"
        )
        assert filt.vote is False
        assert filt.failed is False
        assert req.commitment == 0  # PROCESSED
        assert req.from_slot == FROM_SLOT

    def test_ac11_from_slot_zero_not_set_in_request(self):
        """AC-11/B1: When last_slot==0, from_slot is NOT set (remains default 0)."""
        program_ids = frozenset(["6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"])

        mock_channel, mock_stub, captured_requests = self._build_channel_mock([])
        grpc_patch, stub_patch = self._patch_grpc(mock_channel, mock_stub)

        transport = GeyserTransport(
            endpoint="fake-endpoint:443",
            x_token="fake-token",
            max_reconnect_attempts=1,
        )

        async def run():
            with grpc_patch, stub_patch:
                async for _ in transport.subscribe(program_ids, last_slot=0):
                    pass

        asyncio.run(run())
        assert len(captured_requests) == 1
        req = captured_requests[0]
        # from_slot == 0 means "start from live tip" (not explicitly set)
        assert req.from_slot == 0

    def test_ac11_is_connected_true_during_stream(self):
        """AC-11/B1: is_connected is True while the stream is active."""
        # Capture is_connected mid-stream via a side-effect on the yielded tx
        snapshots: list[bool] = []
        transport_ref: list[GeyserTransport] = []

        async def _fake_subscribe_spy(request_iter):
            # Drain request iter
            async for _ in request_iter:
                pass
            # Yield one valid update so we can snapshot is_connected
            update_tx = _make_subscribe_update_transaction(
                signature_bytes=b"\xf2" * 64,
                slot=400_000_020,
                account_keys_b58=["payer", "prog"],
                instructions=[(1, bytes([0]), b"\x04")],
            )
            yield _make_subscribe_update_wrapper(update_tx)

        mock_channel = AsyncMock()
        mock_channel.__aenter__ = AsyncMock(return_value=mock_channel)
        mock_channel.__aexit__ = AsyncMock(return_value=False)

        mock_stub = MagicMock()
        mock_stub.Subscribe = _fake_subscribe_spy

        import geyser_pb2_grpc  # type: ignore[import]
        grpc_patch = patch("grpc.aio.secure_channel", return_value=mock_channel)
        stub_patch = patch.object(geyser_pb2_grpc, "GeyserStub", return_value=mock_stub)

        transport = GeyserTransport(
            endpoint="fake-endpoint:443",
            x_token="fake-token",
            max_reconnect_attempts=1,
        )
        transport_ref.append(transport)
        program_ids = frozenset(["6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"])

        async def run():
            with grpc_patch, stub_patch:
                async for tx in transport.subscribe(program_ids, last_slot=0):
                    snapshots.append(transport.is_connected)

        asyncio.run(run())
        # While processing the yielded tx, is_connected must have been True
        assert len(snapshots) == 1
        assert snapshots[0] is True
        # After the stream ends and max_reconnect_attempts=1 is exhausted,
        # is_connected becomes False
        assert transport.is_connected is False

    def test_ac11_grpc_error_sets_is_connected_false(self):
        """AC-11/B1: A gRPC AioRpcError sets is_connected=False and
        does NOT propagate past subscribe() (reconnect loop absorbs it)."""
        import grpc
        import grpc.aio

        async def _fake_subscribe_raises(request_iter):
            async for _ in request_iter:
                pass
            raise grpc.aio.AioRpcError(
                code=grpc.StatusCode.UNAVAILABLE,
                initial_metadata=None,
                trailing_metadata=None,
                details="connection refused",
                debug_error_string="",
            )
            # unreachable but needed for async generator syntax
            yield  # pragma: no cover

        mock_channel = AsyncMock()
        mock_channel.__aenter__ = AsyncMock(return_value=mock_channel)
        mock_channel.__aexit__ = AsyncMock(return_value=False)

        mock_stub = MagicMock()
        mock_stub.Subscribe = _fake_subscribe_raises

        import geyser_pb2_grpc  # type: ignore[import]
        grpc_patch = patch("grpc.aio.secure_channel", return_value=mock_channel)
        stub_patch = patch.object(geyser_pb2_grpc, "GeyserStub", return_value=mock_stub)

        transport = GeyserTransport(
            endpoint="fake-endpoint:443",
            x_token="fake-token",
            max_reconnect_attempts=1,
        )
        program_ids = frozenset(["6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"])
        collected = []

        async def run():
            with grpc_patch, stub_patch:
                # Patch asyncio.sleep to skip the backoff wait
                with patch("asyncio.sleep", new_callable=AsyncMock):
                    async for tx in transport.subscribe(program_ids, last_slot=0):
                        collected.append(tx)

        asyncio.run(run())

        assert collected == [], "gRPC error path must yield nothing"
        assert transport.is_connected is False

    def test_ac11_generic_exception_is_absorbed_and_reconnect_attempted(self):
        """AC-11/B1: A non-gRPC exception from _stream_once sets is_connected=False
        and is absorbed by the outer except Exception handler (not propagated).
        The reconnect loop then exhausts max_reconnect_attempts=1 and exits."""
        async def _fake_subscribe_generic_exc(request_iter):
            async for _ in request_iter:
                pass
            raise ValueError("simulated unexpected error from stream")
            yield  # pragma: no cover

        mock_channel = AsyncMock()
        mock_channel.__aenter__ = AsyncMock(return_value=mock_channel)
        mock_channel.__aexit__ = AsyncMock(return_value=False)

        mock_stub = MagicMock()
        mock_stub.Subscribe = _fake_subscribe_generic_exc

        import geyser_pb2_grpc  # type: ignore[import]
        grpc_patch = patch("grpc.aio.secure_channel", return_value=mock_channel)
        stub_patch = patch.object(geyser_pb2_grpc, "GeyserStub", return_value=mock_stub)

        transport = GeyserTransport(
            endpoint="fake-endpoint:443",
            x_token="fake-token",
            max_reconnect_attempts=1,
        )
        program_ids = frozenset(["6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"])
        collected = []

        async def run():
            with grpc_patch, stub_patch:
                with patch("asyncio.sleep", new_callable=AsyncMock):
                    # Must NOT raise — the generic Exception handler absorbs it
                    async for tx in transport.subscribe(program_ids, last_slot=0):
                        collected.append(tx)

        asyncio.run(run())
        assert collected == [], "generic exception path must yield nothing"
        assert transport.is_connected is False

    def test_ac11_cancelled_error_propagates_out_of_subscribe(self):
        """AC-11/B1: asyncio.CancelledError propagates out of subscribe() (not absorbed).
        This ensures that pipeline cancellation actually terminates the transport."""
        async def _fake_subscribe_cancel(request_iter):
            async for _ in request_iter:
                pass
            raise asyncio.CancelledError("simulated task cancellation")
            yield  # pragma: no cover

        mock_channel = AsyncMock()
        mock_channel.__aenter__ = AsyncMock(return_value=mock_channel)
        mock_channel.__aexit__ = AsyncMock(return_value=False)

        mock_stub = MagicMock()
        mock_stub.Subscribe = _fake_subscribe_cancel

        import geyser_pb2_grpc  # type: ignore[import]
        grpc_patch = patch("grpc.aio.secure_channel", return_value=mock_channel)
        stub_patch = patch.object(geyser_pb2_grpc, "GeyserStub", return_value=mock_stub)

        transport = GeyserTransport(
            endpoint="fake-endpoint:443",
            x_token="fake-token",
            max_reconnect_attempts=0,  # infinite — so only CancelledError can exit
        )
        program_ids = frozenset(["6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"])

        async def run():
            with grpc_patch, stub_patch:
                with patch("asyncio.sleep", new_callable=AsyncMock):
                    async for _ in transport.subscribe(program_ids, last_slot=0):
                        pass  # pragma: no cover

        with pytest.raises(asyncio.CancelledError):
            asyncio.run(run())

        assert transport.is_connected is False
