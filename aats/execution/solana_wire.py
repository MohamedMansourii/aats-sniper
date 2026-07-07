"""Minimal, dependency-free Solana transaction wire codec.

Why this exists (ADR-0015): the isolated Python signer (`aats-signer`) must
independently PARSE the exact tx bytes it is asked to sign, in EVERY
environment -- including this dev/CI sandbox where `solders` is a pinned
*production* dependency (pyproject.toml `solders>=0.21.0`) but happens not to
be installed. The signer is the one process where "the parser library might
be missing" is not an acceptable failure mode: if C0 (byte-level parse)
cannot run, the un-bypassable enforcement contract cannot run at all, and an
un-enforced signer is exactly the RED-1 gap ADR-0015 closes. This module has
ZERO third-party dependencies (stdlib only), so C0 works identically whether
or not `solders` is present -- and it is the byte-for-byte conformance
reference for the eventual Rust port (ADR-0015 "Consequences").

Scope: decodes exactly what ADR-0015 C0-C5 need to inspect -- the STATIC
account-keys array, the payer, every TOP-LEVEL instruction's program_id +
account indices + data, and whether the message declares any address-table
lookups (C0a). It is NOT a general Solana SDK: no CPI/inner-instruction
decoding (CPI effects are not visible pre-execution anyway -- ADR-0015 C1-C3
only ever operate on top-level instructions, so that is a completeness
match, not a shortcut), no legacy-vs-v0 execution semantics.

Wire format reference: https://docs.solana.com/developing/programming-model/transactions
(compact-u16 "shortvec" arrays; MessageHeader; account_keys; recent_blockhash;
instructions; v0 address_table_lookups).
"""
from __future__ import annotations

from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Base58 (Bitcoin/Solana alphabet) -- stdlib-only encode/decode.
# ---------------------------------------------------------------------------

_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_B58_INDEX = {ch: i for i, ch in enumerate(_B58_ALPHABET)}


def b58encode(data: bytes) -> str:
    """Encode bytes as a base58 string (Bitcoin/Solana alphabet)."""
    n = int.from_bytes(data, "big")
    out = bytearray()
    while n > 0:
        n, rem = divmod(n, 58)
        out.append(ord(_B58_ALPHABET[rem]))
    out.reverse()
    n_pad = len(data) - len(data.lstrip(b"\x00"))
    return "1" * n_pad + out.decode("ascii")


def b58decode(s: str) -> bytes:
    """Decode a base58 string to bytes. Raises ValueError on an invalid character."""
    n = 0
    for ch in s:
        idx = _B58_INDEX.get(ch)
        if idx is None:
            raise ValueError(f"invalid base58 character: {ch!r}")
        n = n * 58 + idx
    body = n.to_bytes((n.bit_length() + 7) // 8, "big") if n > 0 else b""
    n_pad = len(s) - len(s.lstrip("1"))
    return b"\x00" * n_pad + body


# ---------------------------------------------------------------------------
# Compact-u16 ("shortvec") -- Solana's variable-length array-length encoding.
# ---------------------------------------------------------------------------


def read_compact_u16(buf: bytes, offset: int) -> tuple[int, int]:
    """Read a compact-u16 from buf at offset. Returns (value, next_offset)."""
    value = 0
    shift = 0
    pos = offset
    for _ in range(3):  # a compact-u16 is at most 3 bytes
        if pos >= len(buf):
            raise ValueError("truncated compact-u16")
        b = buf[pos]
        pos += 1
        value |= (b & 0x7F) << shift
        if (b & 0x80) == 0:
            return value, pos
        shift += 7
    raise ValueError("compact-u16 overflow (more than 3 continuation bytes)")


def write_compact_u16(value: int) -> bytes:
    """Encode value as a compact-u16 (shortvec)."""
    if value < 0:
        raise ValueError("compact-u16 cannot encode a negative value")
    out = bytearray()
    v = value
    while True:
        b = v & 0x7F
        v >>= 7
        if v:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


# ---------------------------------------------------------------------------
# Parsed transaction types (C0 output)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ParsedInstruction:
    program_id: str  # base58, resolved from the STATIC account_keys ("" if ALT-region)
    program_id_index: int
    account_indices: tuple[int, ...]
    data: bytes


@dataclass(frozen=True)
class ParsedTx:
    account_keys: tuple[str, ...]  # STATIC keys only -- NEVER ALT-resolved
    num_required_signatures: int
    payer: str  # account_keys[0]
    recent_blockhash: str
    instructions: tuple[ParsedInstruction, ...]
    is_versioned: bool
    has_address_table_lookups: bool  # True => ADR-0015 C0a MUST refuse


class _AltRegionIndex(Exception):
    """Internal signal: an index resolves >= len(static keys) (the ALT-loaded region)."""


def _resolve_static(keys: tuple[str, ...], index: int) -> str:
    if index < 0:
        raise ValueError(f"negative account index: {index}")
    if index >= len(keys):
        raise _AltRegionIndex(index)
    return keys[index]


def parse_transaction(tx_bytes: bytes) -> ParsedTx:
    """Parse a serialized (legacy or v0) Solana transaction.

    Raises ValueError on malformed/truncated input (ADR-0015 C0 ->
    `signer_tx_unparseable`). Never raises on an ALT-referencing tx --
    that is surfaced via `ParsedTx.has_address_table_lookups` /
    `ParsedInstruction.program_id == ""` so the caller can apply the
    distinct C0a refusal (`signer_alt_unresolvable_security_account`).
    """
    pos = 0
    n_sigs, pos = read_compact_u16(tx_bytes, pos)
    pos += n_sigs * 64  # signature bytes (all-zero placeholders for an unsigned tx)
    if pos > len(tx_bytes):
        raise ValueError("truncated transaction: signatures section")

    if pos >= len(tx_bytes):
        raise ValueError("truncated transaction: no message bytes after signatures")

    is_versioned = bool(tx_bytes[pos] & 0x80)
    if is_versioned:
        version = tx_bytes[pos] & 0x7F
        if version != 0:
            raise ValueError(f"unsupported transaction message version: {version}")
        pos += 1

    if pos + 3 > len(tx_bytes):
        raise ValueError("truncated transaction: no message header")
    num_required_signatures = tx_bytes[pos]
    # num_readonly_signed_accounts / num_readonly_unsigned_accounts are not
    # needed by C0-C5, but the header is fixed-width so we still skip them.
    pos += 3

    n_keys, pos = read_compact_u16(tx_bytes, pos)
    keys: list[str] = []
    for _ in range(n_keys):
        if pos + 32 > len(tx_bytes):
            raise ValueError("truncated transaction: account key")
        keys.append(b58encode(tx_bytes[pos:pos + 32]))
        pos += 32
    account_keys = tuple(keys)
    if not account_keys:
        raise ValueError("transaction has no account keys (no payer)")

    if pos + 32 > len(tx_bytes):
        raise ValueError("truncated transaction: recent blockhash")
    recent_blockhash = b58encode(tx_bytes[pos:pos + 32])
    pos += 32

    n_ix, pos = read_compact_u16(tx_bytes, pos)
    alt_region_hit = False
    instructions: list[ParsedInstruction] = []
    for _ in range(n_ix):
        if pos >= len(tx_bytes):
            raise ValueError("truncated transaction: instruction program_id_index")
        program_id_index = tx_bytes[pos]
        pos += 1
        try:
            program_id = _resolve_static(account_keys, program_id_index)
        except _AltRegionIndex:
            program_id = ""
            alt_region_hit = True

        n_acc, pos = read_compact_u16(tx_bytes, pos)
        if pos + n_acc > len(tx_bytes):
            raise ValueError("truncated transaction: instruction accounts")
        acc_indices = tuple(tx_bytes[pos:pos + n_acc])
        pos += n_acc
        for idx in acc_indices:
            if idx >= len(account_keys):
                alt_region_hit = True

        n_data, pos = read_compact_u16(tx_bytes, pos)
        if pos + n_data > len(tx_bytes):
            raise ValueError("truncated transaction: instruction data")
        data = bytes(tx_bytes[pos:pos + n_data])
        pos += n_data

        instructions.append(
            ParsedInstruction(
                program_id=program_id,
                program_id_index=program_id_index,
                account_indices=acc_indices,
                data=data,
            )
        )

    has_alt = alt_region_hit
    if is_versioned and pos < len(tx_bytes):
        n_alt, pos = read_compact_u16(tx_bytes, pos)
        if n_alt > 0:
            has_alt = True
        for _ in range(n_alt):
            if pos + 32 > len(tx_bytes):
                raise ValueError("truncated transaction: ALT lookup account")
            pos += 32
            n_w, pos = read_compact_u16(tx_bytes, pos)
            if pos + n_w > len(tx_bytes):
                raise ValueError("truncated transaction: ALT writable indexes")
            pos += n_w
            n_r, pos = read_compact_u16(tx_bytes, pos)
            if pos + n_r > len(tx_bytes):
                raise ValueError("truncated transaction: ALT readonly indexes")
            pos += n_r

    return ParsedTx(
        account_keys=account_keys,
        num_required_signatures=num_required_signatures,
        payer=account_keys[0],
        recent_blockhash=recent_blockhash,
        instructions=tuple(instructions),
        is_versioned=is_versioned,
        has_address_table_lookups=has_alt,
    )


# ---------------------------------------------------------------------------
# Test-fixture builder -- constructs real wire-format bytes WITHOUT solders.
# Used by the signer_enforcer / signer_process refusal-test suite so C0-C5
# are proven against byte-accurate transactions in every environment.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RawInstruction:
    program_id: str = ""  # looked up in account_keys unless program_id_index_override is set
    accounts: tuple[int, ...] = ()
    data: bytes = b""
    program_id_index_override: int | None = None  # test-only: force an out-of-range index (C0a)


def build_versioned_tx(
    *,
    account_keys: list[str],
    num_required_signatures: int,
    recent_blockhash: str,
    instructions: list[RawInstruction],
    num_readonly_signed: int = 0,
    num_readonly_unsigned: int = 0,
    versioned: bool = True,
    n_address_table_lookups: int = 0,
) -> bytes:
    """Build a serialized transaction (unsigned placeholder signatures) for TEST fixtures.

    account_keys[0] is the payer. Emits `num_required_signatures` all-zero 64-byte
    signature slots so the byte layout matches a real unsigned tx (mirrors solders'
    `VersionedTransaction.unsigned()`). `n_address_table_lookups > 0` emits a v0
    ALT-lookups section with dummy (zero) entries purely to exercise the C0a
    "ALT present" refusal path in tests -- not a functioning ALT reference.
    """
    out = bytearray()
    out += write_compact_u16(num_required_signatures)
    out += b"\x00" * 64 * num_required_signatures

    if versioned:
        out.append(0x80)  # version 0

    out.append(num_required_signatures)
    out.append(num_readonly_signed)
    out.append(num_readonly_unsigned)

    out += write_compact_u16(len(account_keys))
    for key in account_keys:
        raw = b58decode(key)
        if len(raw) != 32:
            raise ValueError(f"account key {key!r} does not decode to 32 bytes")
        out += raw

    bh_raw = b58decode(recent_blockhash)
    if len(bh_raw) != 32:
        raise ValueError("recent_blockhash does not decode to 32 bytes")
    out += bh_raw

    out += write_compact_u16(len(instructions))
    for ix in instructions:
        if ix.program_id_index_override is not None:
            prog_index = ix.program_id_index_override
        else:
            try:
                prog_index = account_keys.index(ix.program_id)
            except ValueError as exc:
                raise ValueError(
                    f"instruction program_id {ix.program_id!r} not in account_keys"
                ) from exc
        if not (0 <= prog_index <= 255):
            raise ValueError("program_id_index must fit in a single byte")
        out.append(prog_index)
        out += write_compact_u16(len(ix.accounts))
        out += bytes(ix.accounts)
        out += write_compact_u16(len(ix.data))
        out += ix.data

    if versioned:
        out += write_compact_u16(n_address_table_lookups)
        for _ in range(n_address_table_lookups):
            out += b"\x00" * 32  # dummy ALT account
            out += write_compact_u16(0)
            out += write_compact_u16(0)

    return bytes(out)
