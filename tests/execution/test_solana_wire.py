"""Tests for the dependency-free Solana wire codec (aats/execution/solana_wire.py).

This codec is the byte-level parser the signer_enforcer (ADR-0015 C0) relies
on -- it must be correct in every environment, with or without `solders`.
"""
from __future__ import annotations

import pytest

from aats.execution import solana_wire as sw

_WALLET = "11111111111111111111111111111112"  # 32 zero-ish bytes, valid base58 length
_PROGRAM = "ComputeBudget111111111111111111111111111111"
_SYSTEM = "11111111111111111111111111111111"
_BLOCKHASH = "SysvarC1ock11111111111111111111111111111111"


class TestBase58RoundTrip:
    @pytest.mark.parametrize(
        "value",
        [
            "11111111111111111111111111111111",
            "ComputeBudget111111111111111111111111111111",
            "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
            "9n3d1K5YD2vECAbRFhFFGYNNjiXtHXJWn9F31t89vsAV",
        ],
    )
    def test_encode_decode_round_trip(self, value: str) -> None:
        raw = sw.b58decode(value)
        assert sw.b58encode(raw) == value

    def test_decode_rejects_invalid_character(self) -> None:
        with pytest.raises(ValueError):
            sw.b58decode("not-valid-base58-0OIl")

    def test_all_zero_bytes_round_trip(self) -> None:
        raw = b"\x00" * 32
        encoded = sw.b58encode(raw)
        assert sw.b58decode(encoded) == raw
        assert len(encoded) >= 32  # leading zero bytes -> leading '1's, not dropped


class TestCompactU16:
    @pytest.mark.parametrize("value", [0, 1, 127, 128, 200, 16383, 16384, 65535, 70000])
    def test_round_trip(self, value: int) -> None:
        encoded = sw.write_compact_u16(value)
        decoded, offset = sw.read_compact_u16(encoded, 0)
        assert decoded == value
        assert offset == len(encoded)

    def test_negative_rejected(self) -> None:
        with pytest.raises(ValueError):
            sw.write_compact_u16(-1)

    def test_truncated_buffer_raises(self) -> None:
        with pytest.raises(ValueError):
            sw.read_compact_u16(b"\x80", 0)  # continuation bit set, nothing follows


class TestParseTransaction:
    def test_parses_simple_two_instruction_tx(self) -> None:
        keys = [_WALLET, _PROGRAM, _SYSTEM]
        tx = sw.build_versioned_tx(
            account_keys=keys,
            num_required_signatures=1,
            recent_blockhash=_BLOCKHASH,
            instructions=[
                sw.RawInstruction(program_id=_PROGRAM, accounts=(), data=b"\x02" + b"\x00" * 4),
                sw.RawInstruction(program_id=_SYSTEM, accounts=(0, 1), data=b"hello"),
            ],
        )
        parsed = sw.parse_transaction(tx)
        assert parsed.payer == _WALLET
        assert parsed.account_keys == tuple(keys)
        assert parsed.num_required_signatures == 1
        assert parsed.recent_blockhash == _BLOCKHASH
        assert len(parsed.instructions) == 2
        assert parsed.instructions[0].program_id == _PROGRAM
        assert parsed.instructions[1].program_id == _SYSTEM
        assert parsed.instructions[1].account_indices == (0, 1)
        assert parsed.instructions[1].data == b"hello"
        assert parsed.has_address_table_lookups is False
        assert parsed.is_versioned is True

    def test_unsigned_tx_has_zero_signature_bytes(self) -> None:
        tx = sw.build_versioned_tx(
            account_keys=[_WALLET, _SYSTEM],
            num_required_signatures=1,
            recent_blockhash=_BLOCKHASH,
            instructions=[],
        )
        n_sigs, pos = sw.read_compact_u16(tx, 0)
        assert n_sigs == 1
        assert tx[pos:pos + 64] == b"\x00" * 64

    def test_legacy_non_versioned_tx_parses(self) -> None:
        tx = sw.build_versioned_tx(
            account_keys=[_WALLET, _SYSTEM],
            num_required_signatures=1,
            recent_blockhash=_BLOCKHASH,
            instructions=[sw.RawInstruction(program_id=_SYSTEM, accounts=(0,), data=b"\x00")],
            versioned=False,
        )
        parsed = sw.parse_transaction(tx)
        assert parsed.is_versioned is False
        assert parsed.payer == _WALLET

    def test_empty_bytes_unparseable(self) -> None:
        with pytest.raises(ValueError):
            sw.parse_transaction(b"")

    def test_truncated_tx_unparseable(self) -> None:
        tx = sw.build_versioned_tx(
            account_keys=[_WALLET, _SYSTEM],
            num_required_signatures=1,
            recent_blockhash=_BLOCKHASH,
            instructions=[sw.RawInstruction(program_id=_SYSTEM, accounts=(0,), data=b"\x00\x00")],
        )
        with pytest.raises(ValueError):
            sw.parse_transaction(tx[:-3])  # chop off the tail of the last instruction's data

    def test_alt_lookups_present_flagged(self) -> None:
        tx = sw.build_versioned_tx(
            account_keys=[_WALLET, _SYSTEM],
            num_required_signatures=1,
            recent_blockhash=_BLOCKHASH,
            instructions=[],
            n_address_table_lookups=1,
        )
        parsed = sw.parse_transaction(tx)
        assert parsed.has_address_table_lookups is True

    def test_out_of_range_program_id_index_flags_alt_region(self) -> None:
        """A program_id_index beyond the static key array is the C0a signal --
        the instruction's program_id resolves to '' and has_address_table_lookups=True."""
        tx = sw.build_versioned_tx(
            account_keys=[_WALLET, _SYSTEM],
            num_required_signatures=1,
            recent_blockhash=_BLOCKHASH,
            instructions=[
                sw.RawInstruction(program_id_index_override=99, accounts=(), data=b"")
            ],
        )
        parsed = sw.parse_transaction(tx)
        assert parsed.has_address_table_lookups is True
        assert parsed.instructions[0].program_id == ""

    def test_out_of_range_account_index_flags_alt_region(self) -> None:
        tx = sw.build_versioned_tx(
            account_keys=[_WALLET, _SYSTEM],
            num_required_signatures=1,
            recent_blockhash=_BLOCKHASH,
            instructions=[
                sw.RawInstruction(program_id=_SYSTEM, accounts=(0, 250), data=b"")
            ],
        )
        parsed = sw.parse_transaction(tx)
        assert parsed.has_address_table_lookups is True

    def test_no_account_keys_unparseable(self) -> None:
        tx = sw.write_compact_u16(0)  # 0 signatures
        tx += bytes([0x80, 1, 0, 0])  # version 0 + header
        tx += sw.write_compact_u16(0)  # 0 account keys
        with pytest.raises(ValueError):
            sw.parse_transaction(tx)


class TestBuildVersionedTxValidation:
    def test_unknown_program_id_raises(self) -> None:
        with pytest.raises(ValueError):
            sw.build_versioned_tx(
                account_keys=[_WALLET],
                num_required_signatures=1,
                recent_blockhash=_BLOCKHASH,
                instructions=[sw.RawInstruction(program_id=_SYSTEM, accounts=(), data=b"")],
            )

    def test_bad_length_key_raises(self) -> None:
        with pytest.raises(ValueError):
            sw.build_versioned_tx(
                account_keys=["short"],
                num_required_signatures=1,
                recent_blockhash=_BLOCKHASH,
                instructions=[],
            )
