"""On-chain instruction + log decoders for pump.fun, PumpSwap, Raydium v4, and CPMM.

Each decoder is a PURE FUNCTION over instruction data.  It receives raw bytes /
log lines and a ProgramRegistry reference, and returns a typed LaunchEvent or
None if the instruction is not relevant.

NO program-ID literals in this file (execution-venue.md §3.2 build guard).
All program IDs come from the ProgramRegistry injected at construction.

Supported decode paths:
  pump.fun:
    - create (new bonding-curve token)
    - buy (bonding-curve buy)
    - sell (bonding-curve sell)
    - withdraw / migrate (bonding-curve completion → PumpSwap)   ← highest value

  PumpSwap:
    - pool_create (post-migration AMM pool init)
    - swap (buy / sell on the PumpSwap AMM)

  Raydium AMM v4:
    - initialize2 (new pool creation with initial liquidity)
    - swap (swap_base_in / swap_base_out)

  Raydium CPMM:
    - initialize (pool init)
    - swap_base_input / swap_base_output

IMPORTANT — offset / layout sourcing:
  Layouts below are derived from the anchor-idl / discriminator bytes verified
  against real transaction signatures as documented in the fixture comments.
  Any offset marked ANCHOR_DISC uses the standard 8-byte Anchor discriminator
  prefix.  Any offset marked MANUAL_BORSH is from hand-verified Borsh layout.
  DO NOT change an offset without referencing a real transaction signature.

Point-in-time correctness:
  Every decoder stamps the returned LaunchEvent with event_time.slot from the
  TRANSACTION slot (on-chain, authoritative).  The wall_clock_ms is stamped
  at the moment of decode call (monitoring only).  data_staleness_ms =
  wall_clock_ms - event_time.block_time_ms.
"""

from __future__ import annotations

import hashlib
import logging
import struct
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol
from urllib.parse import parse_qsl, urlparse

from aats.contracts.events import (
    DetectionTransport,
    EventTime,
    LaunchEvent,
    LaunchSource,
)
from aats.ingestion.registry import ProgramRegistry

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# EventKind — discriminates create/init events from trade events.
#
# This enum lives in M1 (decoders) and is NOT part of the frozen LaunchEvent
# contract (data-models.md §2).  It is M1-internal metadata that the
# ShadowRecorder uses to gate window-opening.  Downstream consumers (feature-
# quant, NLP) receive the LaunchEvent unchanged; EventKind is a sidecar.
#
# CONTRACT LAW: LaunchEvent is frozen (data-models.md §2, ADR-only changes).
# EventKind adds discriminator semantics WITHOUT touching LaunchEvent fields.
# ---------------------------------------------------------------------------


class EventKind(StrEnum):
    """Discriminates the lifecycle phase of a decoded on-chain event.

    CREATE / INIT — a genuine new-token launch.  These are the ONLY events
        that may open a new ShadowRecorder window.
    BUY / SELL / SWAP — trade events.  If a window is already open for the
        mint, these are attributed to it.  If no window is open, they are
        orphans: counted by metric but no window is opened.
    WITHDRAW — bonding-curve completion (pump.fun migration trigger).
        Highest-value signal.  Opens a window (it IS a creation event for
        the migration sniper thesis EH-003).
    UNKNOWN — fallback; treated as non-create (no window opened).
    """

    CREATE = "create"       # pump.fun bonding-curve create
    BUY = "buy"             # pump.fun / PumpSwap buy
    SELL = "sell"           # pump.fun / PumpSwap sell
    SWAP = "swap"           # PumpSwap / Raydium swap (generic)
    WITHDRAW = "withdraw"   # pump.fun bonding-curve completion → migration
    INIT = "init"           # Raydium v4 initialize2 / CPMM initialize / PumpSwap create_pool
    UNKNOWN = "unknown"     # could not determine


# The set of EventKinds that are allowed to OPEN a new snapshot window.
# A buy/sell/swap/unknown arriving before a CREATE/INIT for that mint
# is an orphan — it is counted but does NOT open a window.
WINDOW_OPENING_KINDS: frozenset[EventKind] = frozenset({
    EventKind.CREATE,
    EventKind.WITHDRAW,
    EventKind.INIT,
})

# ---------------------------------------------------------------------------
# Raw transaction structure (transport-agnostic input to decoders)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RawInstruction:
    """A single instruction from a transaction, pre-parsed from the wire."""

    program_id: str  # base58 program pubkey
    data_b64: str  # base64-encoded instruction data bytes
    account_keys: list[str]  # account metas in index order (base58 pubkeys)
    program_index: int  # index into the outer accounts list


@dataclass(frozen=True)
class RawTransaction:
    """Minimal decoded transaction, transport-agnostic (Geyser or WS fallback).

    This is the input type to all decoders.  The transport layer (geyser.py /
    ws_fallback.py) constructs this; decoders consume it.

    slot and block_time_unix_s come from the Geyser SubscribeUpdateTransaction
    context or from the enhanced-WS logsSubscribe response.
    """

    signature: str
    slot: int
    block_time_unix_s: int | None  # on-chain blockTime (seconds); None if not yet
    fee_payer: str
    instructions: list[RawInstruction]
    inner_instructions: list[RawInstruction]  # all inner ixs, flattened
    program_logs: list[str]  # Program log: ... lines from meta.logMessages
    is_vote: bool = False
    err: str | None = None  # None = success


# ---------------------------------------------------------------------------
# Discriminator constants (Anchor 8-byte prefix = sha256("global:<ix_name>")[:8])
# Verified against on-chain transaction fixtures (see tests/ingestion/fixtures/).
# ---------------------------------------------------------------------------


def _disc(name: str) -> bytes:
    """Compute the 8-byte Anchor discriminator for an instruction name."""
    return hashlib.sha256(f"global:{name}".encode()).digest()[:8]


# pump.fun discriminators — verified against fixture transactions
PUMP_DISC_CREATE = _disc("create")
PUMP_DISC_BUY = _disc("buy")
PUMP_DISC_SELL = _disc("sell")
PUMP_DISC_WITHDRAW = _disc("withdraw")

# PumpSwap discriminators — verified against fixture transactions
PUMPSWAP_DISC_CREATE_POOL = _disc("create_pool")
PUMPSWAP_DISC_BUY = _disc("buy")
PUMPSWAP_DISC_SELL = _disc("sell")

# Raydium AMM v4 (non-Anchor, manual Borsh layout) — one-byte discriminator
# initialize2: instruction index 1 in Raydium AMM v4 IDL
RAYDIUM_V4_DISC_INIT2 = bytes([1])  # MANUAL_BORSH: first byte of data
RAYDIUM_V4_DISC_SWAP = bytes([9])  # MANUAL_BORSH: SwapBaseIn (9), SwapBaseOut (10)
RAYDIUM_V4_DISC_SWAP2 = bytes([10])

# Raydium CPMM discriminators (Anchor)
CPMM_DISC_INITIALIZE = _disc("initialize")
CPMM_DISC_SWAP_INPUT = _disc("swap_base_input")
CPMM_DISC_SWAP_OUTPUT = _disc("swap_base_output")


# ---------------------------------------------------------------------------
# Account index constants for pump.fun initialize instruction
# These are verified against real pump.fun create transactions.
# Account layout: [payer, mint, bondingCurve, associatedBondingCurve,
#                  global, mplTokenMetadata, metadata, systemProgram, ...]
# ---------------------------------------------------------------------------

PUMP_CREATE_ACCOUNT_IDX_MINT = 1
PUMP_CREATE_ACCOUNT_IDX_BONDING_CURVE = 2
PUMP_CREATE_ACCOUNT_IDX_PAYER = 0
# mplTokenMetadata PROGRAM account (index 5) — used to identify (by account
# equality, never a hardcoded literal) which inner instruction is the CPI into
# mpl-token-metadata's create_metadata_account, so we can pull the URI it was
# given (E-M1-02).  index 6 ("metadata") is the resulting PDA — not needed here.
PUMP_CREATE_ACCOUNT_IDX_MPL_METADATA_PROGRAM = 5

PUMP_BUY_ACCOUNT_IDX_MINT = 2
PUMP_BUY_ACCOUNT_IDX_BONDING_CURVE = 3
PUMP_BUY_ACCOUNT_IDX_USER = 6

PUMP_SELL_ACCOUNT_IDX_MINT = 2
PUMP_SELL_ACCOUNT_IDX_BONDING_CURVE = 3
PUMP_SELL_ACCOUNT_IDX_USER = 6

# pump.fun withdraw (migration trigger) account layout
PUMP_WITHDRAW_ACCOUNT_IDX_MINT = 2

# PumpSwap create_pool account layout (verified from PumpSwap IDL)
PSWAP_CREATE_ACCOUNT_IDX_POOL = 0
PSWAP_CREATE_ACCOUNT_IDX_BASE_MINT = 3
PSWAP_CREATE_ACCOUNT_IDX_QUOTE_MINT = 4
PSWAP_CREATE_ACCOUNT_IDX_CREATOR = 1

# Raydium AMM v4 initialize2 account layout (verified from Raydium IDL)
RAY_V4_INIT_ACCOUNT_IDX_AMM_ID = 4
RAY_V4_INIT_ACCOUNT_IDX_COIN_MINT = 8  # coin (base) mint
RAY_V4_INIT_ACCOUNT_IDX_PC_MINT = 9  # pc (quote) mint
RAY_V4_INIT_ACCOUNT_IDX_COIN_VAULT = 10
RAY_V4_INIT_ACCOUNT_IDX_PC_VAULT = 11

# Raydium CPMM initialize account layout (verified from CPMM IDL)
CPMM_INIT_ACCOUNT_IDX_POOL = 0
CPMM_INIT_ACCOUNT_IDX_CREATOR = 1
CPMM_INIT_ACCOUNT_IDX_TOKEN0 = 4
CPMM_INIT_ACCOUNT_IDX_TOKEN1 = 5


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------


def _b64_decode(s: str) -> bytes:
    import base64

    return base64.b64decode(s)


def _safe_get_account(accounts: list[str], idx: int) -> str | None:
    if idx < len(accounts):
        return accounts[idx]
    return None


def _now_ms() -> int:
    return int(time.time() * 1_000)


# Sentinel for data_staleness_ms when block_time is unavailable (T-300a).
# A value of -1 means "staleness unknown — block_time not yet confirmed on-chain."
# Consumers MUST NOT treat -1 as zero or as a real latency measurement.
STALENESS_UNKNOWN: int = -1


def _make_event_time(slot: int, block_time_unix_s: int | None) -> EventTime | None:
    """Construct EventTime with on-chain block_time_ms as the AUTHORITATIVE clock (C-5).

    POINT-IN-TIME CORRECTNESS LAW (T-300a fix):
      If block_time_unix_s is None or 0 (pre-confirmation / ShredStream path),
      this function returns None.  The caller MUST NOT emit a LaunchEvent in this
      case.  Wall-clock is NEVER substituted into block_time_ms.

    Why: block_time_ms is the AUTHORITATIVE join anchor for every feature and
    backtest.  Substituting wall-clock collapses data_staleness_ms to ~0 (hiding
    real staleness) and introduces a compute-time leak that silently inflates
    every backtest metric.  The correct behaviour is to carry the event as
    "pending" (unconfirmed block_time) and exclude it from the point-in-time
    store until a confirmed block_time arrives.

    Returns:
        EventTime with the on-chain block_time_ms, OR None if block_time is
        absent.  None means "do not emit a LaunchEvent for this transaction yet."
    """
    wall_ms = _now_ms()
    if block_time_unix_s is None or block_time_unix_s <= 0:
        # Block time not yet confirmed.  Log the skip so operators can see
        # how many pre-confirmation events are being held pending.
        logger.debug(
            "decoder._make_event_time: slot=%d block_time_unix_s=%r absent — "
            "event HELD PENDING (no wall-clock substitution); T-300a",
            slot,
            block_time_unix_s,
        )
        return None
    block_time_ms = block_time_unix_s * 1_000
    return EventTime(
        slot=slot,
        block_time_ms=block_time_ms,
        wall_clock_ms=wall_ms,
    )


def _staleness_ms(event_time: EventTime | None) -> int:
    """Compute staleness at the moment of decode (monitoring only).

    Returns STALENESS_UNKNOWN (-1) when event_time is None (block_time absent).
    Consumers must check for STALENESS_UNKNOWN and treat it as a HIGH-staleness
    signal, NOT as zero (T-300a).
    """
    if event_time is None:
        return STALENESS_UNKNOWN
    return max(0, _now_ms() - event_time.block_time_ms)


def _fingerprint_by_creator_mint(creator: str, mint: str) -> str:
    """Per-mint identity proxy — NOT template-invariant.

    Used only on decode paths (withdraw/migration, PumpSwap create_pool,
    Raydium v4/CPMM init) where no create-time metadata URI is reachable
    (the mint already exists; nothing re-runs mpl-token-metadata's
    create_metadata_account CPI here).  Changes every launch even for a
    repeat creator/template — it is a fallback identity tag, not the
    template-fingerprint semantics used on the pump.fun `create` path
    (see `_deploy_template_fingerprint_from_uri`).
    """
    raw = f"{creator}:{mint[:8]}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Template-invariant deploy fingerprint (E-M1-02)
#
# Fingerprint on the metadata-URI SHAPE (scheme + host-identity + normalized
# path pattern with the per-mint hash/CID collapsed), not on creator+mint.
# A rug-factory that reuses the same off-chain metadata template — same
# hosting service, same path/filename pattern — from a NEW wallet on a NEW
# mint yields the SAME fingerprint.  A genuinely different template (a
# different host, or a different path shape) yields a different fingerprint.
# Undecodable/absent metadata yields None (refuse-by-default — never
# fabricate a fingerprint from partial data).
# ---------------------------------------------------------------------------

_ALLOWED_METADATA_URI_SCHEMES = frozenset({"http", "https", "ipfs", "ar"})


def _looks_variable(segment: str) -> bool:
    """Heuristic: does this URL component look like a per-mint hash/ID?

    Long strings (CIDs, arweave tx ids, uuids) or shorter mixed alnum
    strings are treated as the variable, per-launch component and are
    collapsed out of the template shape.  Short literal path words
    ("ipfs", "metadata", "api", "v1", ...) are left untouched.
    """
    if not segment:
        return False
    if len(segment) >= 16:
        return True
    has_digit = any(ch.isdigit() for ch in segment)
    has_alpha = any(ch.isalpha() for ch in segment)
    return len(segment) >= 8 and has_digit and has_alpha


def _normalize_path_segment(segment: str) -> str:
    if "." in segment:
        stem, _, ext = segment.rpartition(".")
    else:
        stem, ext = segment, ""
    normalized_stem = "*" if _looks_variable(stem) else stem
    return f"{normalized_stem}.{ext.lower()}" if ext else normalized_stem


def _uri_shape(uri: str) -> str | None:
    """Reduce a metadata URI to a template-stable shape string.

    Returns None (refuse-by-default) for anything that cannot be confidently
    parsed as one of the recognised metadata-hosting URI schemes.
    """
    if not uri:
        return None
    try:
        parsed = urlparse(uri.strip())
    except ValueError:
        return None
    scheme = parsed.scheme.lower()
    if scheme not in _ALLOWED_METADATA_URI_SCHEMES:
        return None
    netloc = parsed.netloc
    if not netloc:
        return None
    if scheme in ("http", "https"):
        # A real hosting domain is part of the template's identity — keep it.
        host_identity = netloc.lower()
    else:
        # ipfs:// / ar:// — the netloc IS the content-addressed hash (unique
        # per upload), not a hosting identity.  Collapse it like a path hash.
        host_identity = "*" if _looks_variable(netloc) else netloc.lower()

    segments = [s for s in parsed.path.split("/") if s]
    normalized_path = "/".join(_normalize_path_segment(s) for s in segments)
    query_keys = sorted({k for k, _ in parse_qsl(parsed.query, keep_blank_values=True)})

    shape = f"{scheme}://{host_identity}/{normalized_path}"
    if query_keys:
        shape += "?" + "&".join(query_keys)
    return shape


def _deploy_template_fingerprint_from_uri(uri: str | None) -> str | None:
    """sha256 of the metadata-URI shape, truncated to 16 hex chars.

    None in, None out (no metadata / undecodable metadata / malformed URI —
    refuse-by-default rather than guess).
    """
    shape = _uri_shape(uri) if uri else None
    if shape is None:
        return None
    return hashlib.sha256(shape.encode()).hexdigest()[:16]


def _read_borsh_string(data: bytes, offset: int) -> tuple[str, int] | None:
    """Read one Borsh-encoded string (u32 LE length prefix + utf-8 bytes).

    Returns (value, next_offset), or None if the buffer is too short or the
    bytes are not valid utf-8 — malformed input is refused, never guessed.
    """
    if offset + 4 > len(data):
        return None
    (length,) = struct.unpack_from("<I", data, offset)
    start = offset + 4
    end = start + length
    if length > len(data) or end > len(data):
        return None
    try:
        value = data[start:end].decode("utf-8")
    except UnicodeDecodeError:
        return None
    return value, end


def _parse_mpl_create_metadata_uri(data: bytes) -> str | None:
    """Parse the `uri` field out of an mpl-token-metadata
    CreateMetadataAccount(V2/V3) CPI instruction payload.

    Layout (Borsh): 1-byte instruction discriminant, then DataV2 { name,
    symbol, uri, ... } — name/symbol/uri are each a u32-length-prefixed
    UTF-8 string, in that fixed order.  We only need to walk past name and
    symbol to reach uri; the remaining DataV2/Option fields are irrelevant
    to the fingerprint and are never parsed.
    """
    if len(data) < 1:
        return None
    offset = 1  # skip the 1-byte instruction discriminant
    name_result = _read_borsh_string(data, offset)
    if name_result is None:
        return None
    _, offset = name_result
    symbol_result = _read_borsh_string(data, offset)
    if symbol_result is None:
        return None
    _, offset = symbol_result
    uri_result = _read_borsh_string(data, offset)
    if uri_result is None:
        return None
    uri, _ = uri_result
    return uri or None


def _find_mpl_metadata_uri(
    inner_instructions: list[RawInstruction], mpl_metadata_program_id: str | None
) -> str | None:
    """Scan a create tx's inner instructions for the CPI into mpl-token-metadata
    and parse the URI it was given.

    `mpl_metadata_program_id` is resolved by the caller from the outer create
    instruction's OWN account_keys (index-referenced) — never a hardcoded
    program-ID literal in this module.  Returns None if the CPI is absent or
    its payload is malformed (refuse-by-default).
    """
    if not mpl_metadata_program_id:
        return None
    for inner_ix in inner_instructions:
        if inner_ix.program_id != mpl_metadata_program_id:
            continue
        try:
            data = _b64_decode(inner_ix.data_b64)
        except Exception:
            continue
        uri = _parse_mpl_create_metadata_uri(data)
        if uri is not None:
            return uri
    return None


# ---------------------------------------------------------------------------
# Decoder protocol (structural typing — no ABC overhead on hot path)
# ---------------------------------------------------------------------------


class _DecoderProtocol(Protocol):
    def decode(
        self,
        tx: RawTransaction,
        transport: DetectionTransport,
    ) -> tuple[LaunchEvent, EventKind] | None: ...


# ---------------------------------------------------------------------------
# pump.fun decoder
# ---------------------------------------------------------------------------


class PumpFunDecoder:
    """Decode pump.fun bonding-curve instructions into LaunchEvent.

    Program ID comes from the registry — never hardcoded here.
    """

    def __init__(self, registry: ProgramRegistry) -> None:
        self._registry = registry

    def _program_id(self) -> str | None:
        try:
            return self._registry.program_id(LaunchSource.PUMPFUN)
        except KeyError:
            return None

    def decode(
        self,
        tx: RawTransaction,
        transport: DetectionTransport,
    ) -> tuple[LaunchEvent, EventKind] | None:
        """Attempt to decode the transaction as a pump.fun event.

        Returns (LaunchEvent, EventKind) for the most significant event in the
        transaction (prefer MIGRATION over BUY/SELL; prefer BUY over SELL).
        Returns None if not a pump.fun tx.
        """
        pid = self._program_id()
        if pid is None:
            return None

        pump_ixs = [ix for ix in (tx.instructions + tx.inner_instructions) if ix.program_id == pid]
        if not pump_ixs:
            return None

        # Check withdraw first (migration / bonding-curve completion — highest value)
        for ix in pump_ixs:
            ev = self._try_withdraw(ix, tx, transport)
            if ev is not None:
                return ev, EventKind.WITHDRAW

        # Then create
        for ix in pump_ixs:
            ev = self._try_create(ix, tx, transport)
            if ev is not None:
                return ev, EventKind.CREATE

        # Then buy
        for ix in pump_ixs:
            ev = self._try_buy(ix, tx, transport)
            if ev is not None:
                return ev, EventKind.BUY

        # Then sell
        for ix in pump_ixs:
            ev = self._try_sell(ix, tx, transport)
            if ev is not None:
                return ev, EventKind.SELL

        return None

    def _try_create(
        self, ix: RawInstruction, tx: RawTransaction, transport: DetectionTransport
    ) -> LaunchEvent | None:
        try:
            data = _b64_decode(ix.data_b64)
        except Exception:
            return None
        if len(data) < 8 or data[:8] != PUMP_DISC_CREATE:
            return None

        mint = _safe_get_account(ix.account_keys, PUMP_CREATE_ACCOUNT_IDX_MINT)
        creator = _safe_get_account(ix.account_keys, PUMP_CREATE_ACCOUNT_IDX_PAYER)
        if mint is None or creator is None:
            return None

        # E-M1-02: fingerprint on the mplTokenMetadata CPI's URI shape (template-
        # invariant), not on creator+mint (which changes every launch).  The mpl
        # metadata program account is referenced BY INDEX from this create ix's
        # own account_keys — never a hardcoded program-ID literal.
        mpl_metadata_program_id = _safe_get_account(
            ix.account_keys, PUMP_CREATE_ACCOUNT_IDX_MPL_METADATA_PROGRAM
        )
        metadata_uri = _find_mpl_metadata_uri(tx.inner_instructions, mpl_metadata_program_id)

        event_time = _make_event_time(tx.slot, tx.block_time_unix_s)
        if event_time is None:
            return None  # block_time absent — hold pending, never wall-clock date (T-300a)
        return LaunchEvent(
            mint=mint,
            venue_program_id=self._program_id() or "",
            source=LaunchSource.PUMPFUN,
            event_time=event_time,
            observation_slot=tx.slot,
            confirmation_slot=tx.slot,
            detection_transport=transport,
            sol_reserve_lamports=0,  # Not embedded in create; enriched later
            token_reserve_base=0,
            token_decimals=6,  # pump.fun always 6 decimals
            initial_holders=0,
            competitors=0,
            creator_wallet=creator,
            bundler_cluster_id=None,
            deploy_template_fingerprint=_deploy_template_fingerprint_from_uri(metadata_uri),
            data_staleness_ms=_staleness_ms(event_time),
        )

    def _try_buy(
        self, ix: RawInstruction, tx: RawTransaction, transport: DetectionTransport
    ) -> LaunchEvent | None:
        try:
            data = _b64_decode(ix.data_b64)
        except Exception:
            return None
        if len(data) < 8 or data[:8] != PUMP_DISC_BUY:
            return None

        # pump.fun buy layout (after 8-byte disc):
        # u64 token_amount (little-endian) @ offset 8
        # u64 max_sol_cost (little-endian) @ offset 16
        if len(data) < 24:
            return None
        token_amount = struct.unpack_from("<Q", data, 8)[0]
        max_sol_cost_lamports = struct.unpack_from("<Q", data, 16)[0]

        mint = _safe_get_account(ix.account_keys, PUMP_BUY_ACCOUNT_IDX_MINT)
        creator = _safe_get_account(ix.account_keys, PUMP_BUY_ACCOUNT_IDX_USER) or ""
        if mint is None:
            return None

        event_time = _make_event_time(tx.slot, tx.block_time_unix_s)
        if event_time is None:
            return None  # block_time absent — hold pending, never wall-clock date (T-300a)
        return LaunchEvent(
            mint=mint,
            venue_program_id=self._program_id() or "",
            source=LaunchSource.PUMPFUN,
            event_time=event_time,
            observation_slot=tx.slot,
            confirmation_slot=tx.slot,
            detection_transport=transport,
            sol_reserve_lamports=max_sol_cost_lamports,
            token_reserve_base=token_amount,
            token_decimals=6,
            initial_holders=0,
            competitors=0,
            creator_wallet=creator,
            bundler_cluster_id=None,
            deploy_template_fingerprint=None,
            data_staleness_ms=_staleness_ms(event_time),
        )

    def _try_sell(
        self, ix: RawInstruction, tx: RawTransaction, transport: DetectionTransport
    ) -> LaunchEvent | None:
        try:
            data = _b64_decode(ix.data_b64)
        except Exception:
            return None
        if len(data) < 8 or data[:8] != PUMP_DISC_SELL:
            return None

        # pump.fun sell layout (after 8-byte disc):
        # u64 token_amount @ offset 8
        # u64 min_sol_output @ offset 16
        if len(data) < 24:
            return None
        token_amount = struct.unpack_from("<Q", data, 8)[0]
        min_sol_output = struct.unpack_from("<Q", data, 16)[0]

        mint = _safe_get_account(ix.account_keys, PUMP_SELL_ACCOUNT_IDX_MINT)
        seller = _safe_get_account(ix.account_keys, PUMP_SELL_ACCOUNT_IDX_USER) or ""
        if mint is None:
            return None

        event_time = _make_event_time(tx.slot, tx.block_time_unix_s)
        if event_time is None:
            return None  # block_time absent — hold pending, never wall-clock date (T-300a)
        return LaunchEvent(
            mint=mint,
            venue_program_id=self._program_id() or "",
            source=LaunchSource.PUMPFUN,
            event_time=event_time,
            observation_slot=tx.slot,
            confirmation_slot=tx.slot,
            detection_transport=transport,
            sol_reserve_lamports=min_sol_output,
            token_reserve_base=token_amount,
            token_decimals=6,
            initial_holders=0,
            competitors=0,
            creator_wallet=seller,
            bundler_cluster_id=None,
            deploy_template_fingerprint=None,
            data_staleness_ms=_staleness_ms(event_time),
        )

    def _try_withdraw(
        self, ix: RawInstruction, tx: RawTransaction, transport: DetectionTransport
    ) -> LaunchEvent | None:
        """Withdraw = bonding-curve completion → emit MigrationEvent early.

        The withdraw instruction fires when the bonding curve is fully filled
        and pump.fun moves liquidity to PumpSwap.  This is the highest-value
        signal (migration sniper trigger, EH-003).
        """
        try:
            data = _b64_decode(ix.data_b64)
        except Exception:
            return None
        if len(data) < 8 or data[:8] != PUMP_DISC_WITHDRAW:
            return None

        mint = _safe_get_account(ix.account_keys, PUMP_WITHDRAW_ACCOUNT_IDX_MINT)
        creator = tx.fee_payer
        if mint is None:
            return None

        event_time = _make_event_time(tx.slot, tx.block_time_unix_s)
        if event_time is None:
            return None  # block_time absent — hold pending, never wall-clock date (T-300a)
        return LaunchEvent(
            mint=mint,
            venue_program_id=self._program_id() or "",
            source=LaunchSource.MIGRATION,  # migration event — highest priority
            event_time=event_time,
            observation_slot=tx.slot,
            confirmation_slot=tx.slot,
            detection_transport=transport,
            sol_reserve_lamports=0,  # Reserve read from logs / account delta if available
            token_reserve_base=0,
            token_decimals=6,
            initial_holders=0,
            competitors=0,
            creator_wallet=creator,
            bundler_cluster_id=None,
            deploy_template_fingerprint=_fingerprint_by_creator_mint(creator, mint),
            data_staleness_ms=_staleness_ms(event_time),
        )


# ---------------------------------------------------------------------------
# PumpSwap decoder
# ---------------------------------------------------------------------------


class PumpSwapDecoder:
    """Decode PumpSwap AMM events (post-migration pool and swaps).

    PumpSwap is pump.fun's primary migration target (A-001).  Its program ID
    comes from the registry — NOT hardcoded here.
    """

    def __init__(self, registry: ProgramRegistry) -> None:
        self._registry = registry

    def _program_id(self) -> str | None:
        try:
            return self._registry.program_id(LaunchSource.PUMPSWAP)
        except KeyError:
            return None

    def decode(
        self,
        tx: RawTransaction,
        transport: DetectionTransport,
    ) -> tuple[LaunchEvent, EventKind] | None:
        pid = self._program_id()
        if pid is None:
            return None

        ixs = [ix for ix in (tx.instructions + tx.inner_instructions) if ix.program_id == pid]
        if not ixs:
            return None

        # Pool creation has priority over swaps (INIT opens a window)
        for ix in ixs:
            ev = self._try_create_pool(ix, tx, transport)
            if ev is not None:
                return ev, EventKind.INIT

        for ix in ixs:
            ev = self._try_swap(ix, tx, transport)
            if ev is not None:
                return ev, EventKind.SWAP

        return None

    def _try_create_pool(
        self, ix: RawInstruction, tx: RawTransaction, transport: DetectionTransport
    ) -> LaunchEvent | None:
        try:
            data = _b64_decode(ix.data_b64)
        except Exception:
            return None
        if len(data) < 8 or data[:8] != PUMPSWAP_DISC_CREATE_POOL:
            return None

        # PumpSwap create_pool layout (after 8-byte disc):
        # u64 base_amount_in @ offset 8
        # u64 quote_amount_in @ offset 16
        if len(data) < 24:
            return None
        base_amount = struct.unpack_from("<Q", data, 8)[0]
        quote_amount = struct.unpack_from("<Q", data, 16)[0]

        base_mint = _safe_get_account(ix.account_keys, PSWAP_CREATE_ACCOUNT_IDX_BASE_MINT)
        creator = (
            _safe_get_account(ix.account_keys, PSWAP_CREATE_ACCOUNT_IDX_CREATOR) or tx.fee_payer
        )
        if base_mint is None:
            return None

        event_time = _make_event_time(tx.slot, tx.block_time_unix_s)
        if event_time is None:
            return None  # block_time absent — hold pending, never wall-clock date (T-300a)
        return LaunchEvent(
            mint=base_mint,
            venue_program_id=self._program_id() or "",
            source=LaunchSource.PUMPSWAP,
            event_time=event_time,
            observation_slot=tx.slot,
            confirmation_slot=tx.slot,
            detection_transport=transport,
            sol_reserve_lamports=quote_amount,
            token_reserve_base=base_amount,
            token_decimals=6,
            initial_holders=0,
            competitors=0,
            creator_wallet=creator,
            bundler_cluster_id=None,
            deploy_template_fingerprint=_fingerprint_by_creator_mint(creator, base_mint),
            data_staleness_ms=_staleness_ms(event_time),
        )

    def _try_swap(
        self, ix: RawInstruction, tx: RawTransaction, transport: DetectionTransport
    ) -> LaunchEvent | None:
        try:
            data = _b64_decode(ix.data_b64)
        except Exception:
            return None
        is_buy = len(data) >= 8 and data[:8] == PUMPSWAP_DISC_BUY
        is_sell = len(data) >= 8 and data[:8] == PUMPSWAP_DISC_SELL
        if not (is_buy or is_sell):
            return None

        # PumpSwap swap layout (after 8-byte disc):
        # u64 base_amount @ offset 8
        # u64 quote_amount @ offset 16
        if len(data) < 24:
            return None
        base_amount = struct.unpack_from("<Q", data, 8)[0]
        quote_amount = struct.unpack_from("<Q", data, 16)[0]

        # Account layout: [pool, user, base_mint, quote_mint, ...]
        base_mint = _safe_get_account(ix.account_keys, 2)
        user = _safe_get_account(ix.account_keys, 1) or tx.fee_payer
        if base_mint is None:
            return None

        event_time = _make_event_time(tx.slot, tx.block_time_unix_s)
        if event_time is None:
            return None  # block_time absent — hold pending, never wall-clock date (T-300a)
        return LaunchEvent(
            mint=base_mint,
            venue_program_id=self._program_id() or "",
            source=LaunchSource.PUMPSWAP,
            event_time=event_time,
            observation_slot=tx.slot,
            confirmation_slot=tx.slot,
            detection_transport=transport,
            sol_reserve_lamports=quote_amount,
            token_reserve_base=base_amount,
            token_decimals=6,
            initial_holders=0,
            competitors=0,
            creator_wallet=user,
            bundler_cluster_id=None,
            deploy_template_fingerprint=None,
            data_staleness_ms=_staleness_ms(event_time),
        )


# ---------------------------------------------------------------------------
# Raydium AMM v4 decoder
# ---------------------------------------------------------------------------


class RaydiumV4Decoder:
    """Decode Raydium AMM v4 pool initialization and swaps.

    Layout is MANUAL_BORSH (non-Anchor IDL).  Offsets are verified against
    real Raydium v4 transactions (see fixture comments in tests/).
    """

    def __init__(self, registry: ProgramRegistry) -> None:
        self._registry = registry

    def _program_id(self) -> str | None:
        try:
            return self._registry.program_id(LaunchSource.RAYDIUM_V4)
        except KeyError:
            return None

    def decode(
        self,
        tx: RawTransaction,
        transport: DetectionTransport,
    ) -> tuple[LaunchEvent, EventKind] | None:
        pid = self._program_id()
        if pid is None:
            return None

        ixs = [ix for ix in (tx.instructions + tx.inner_instructions) if ix.program_id == pid]
        if not ixs:
            return None

        for ix in ixs:
            ev = self._try_init2(ix, tx, transport)
            if ev is not None:
                return ev, EventKind.INIT

        for ix in ixs:
            ev = self._try_swap(ix, tx, transport)
            if ev is not None:
                return ev, EventKind.SWAP

        return None

    def _try_init2(
        self, ix: RawInstruction, tx: RawTransaction, transport: DetectionTransport
    ) -> LaunchEvent | None:
        """Raydium AMM v4 initialize2 — new pool with initial liquidity.

        initialize2 layout (MANUAL_BORSH — verified against live txs):
          byte[0]   = instruction index (1 = initialize2)
          u64 LE    = nonce @ offset 1 (1 byte for nonce, not 8)
          Actually: the first byte is the discriminator (1 byte, value 1 for init2)
          then the Borsh-encoded InitializeInstruction2 struct follows.
          Minimum viable decode: just check byte[0] and extract accounts.
        """
        try:
            data = _b64_decode(ix.data_b64)
        except Exception:
            return None
        if len(data) < 1 or data[:1] != RAYDIUM_V4_DISC_INIT2:
            return None

        coin_mint = _safe_get_account(ix.account_keys, RAY_V4_INIT_ACCOUNT_IDX_COIN_MINT)
        pc_mint = _safe_get_account(ix.account_keys, RAY_V4_INIT_ACCOUNT_IDX_PC_MINT)
        creator = tx.fee_payer
        if coin_mint is None:
            return None

        # Use the non-SOL mint as the tracked token
        # SOL mint = "So11111111111111111111111111111111111111112"
        SOL_MINT = "So11111111111111111111111111111111111111112"
        mint = pc_mint if coin_mint == SOL_MINT else coin_mint
        sol_mint_is_coin = coin_mint == SOL_MINT

        # Raydium v4 init2 data layout after disc byte:
        # We need init_pc_amount (u64, SOL side) and init_coin_amount (u64, token side)
        # Raydium v4 initialize2 Borsh struct:
        # { nonce: u8, open_time: u64, init_pc_amount: u64, init_coin_amount: u64 }
        # total: 1 + 8 + 8 + 8 = 25 bytes after disc
        if len(data) >= 26:
            # nonce (1 byte) + open_time (8 bytes) + init_pc_amount (8) + init_coin_amount (8)
            init_pc_amount = struct.unpack_from("<Q", data, 10)[0]  # offset 1+1+8 = 10
            init_coin_amount = struct.unpack_from("<Q", data, 18)[0]  # offset 1+1+8+8 = 18
            if sol_mint_is_coin:
                sol_lamports = init_coin_amount
                token_base = init_pc_amount
            else:
                sol_lamports = init_pc_amount
                token_base = init_coin_amount
        else:
            sol_lamports = 0
            token_base = 0

        event_time = _make_event_time(tx.slot, tx.block_time_unix_s)
        if event_time is None:
            return None  # block_time absent — hold pending, never wall-clock date (T-300a)
        return LaunchEvent(
            mint=mint or coin_mint or "",
            venue_program_id=self._program_id() or "",
            source=LaunchSource.RAYDIUM_V4,
            event_time=event_time,
            observation_slot=tx.slot,
            confirmation_slot=tx.slot,
            detection_transport=transport,
            sol_reserve_lamports=sol_lamports,
            token_reserve_base=token_base,
            token_decimals=9,  # most Raydium tokens; enrichment corrects this
            initial_holders=0,
            competitors=0,
            creator_wallet=creator,
            bundler_cluster_id=None,
            deploy_template_fingerprint=_fingerprint_by_creator_mint(creator, mint or coin_mint or ""),
            data_staleness_ms=_staleness_ms(event_time),
        )

    def _try_swap(
        self, ix: RawInstruction, tx: RawTransaction, transport: DetectionTransport
    ) -> LaunchEvent | None:
        try:
            data = _b64_decode(ix.data_b64)
        except Exception:
            return None
        if len(data) < 1:
            return None
        disc = data[:1]
        if disc not in (RAYDIUM_V4_DISC_SWAP, RAYDIUM_V4_DISC_SWAP2):
            return None

        # SwapBaseIn layout (after 1-byte disc):
        # { amount_in: u64, minimum_amount_out: u64 }
        if len(data) < 17:
            return None
        amount_in = struct.unpack_from("<Q", data, 1)[0]
        min_out = struct.unpack_from("<Q", data, 9)[0]

        # Raydium swap accounts: [amm, authority, openOrders, targetOrders,
        #   poolCoinVault, poolPcVault, serumProg, serumMkt, ..., userSource, userDest, userOwner]
        # Coin vault is at index 4; the coin mint is recoverable from vault account
        # For M1 purposes, we emit a buy/sell event with the trade amounts
        _safe_get_account(ix.account_keys, 4)
        user = _safe_get_account(ix.account_keys, 15) or tx.fee_payer
        # The mint is not directly in swap accounts; we use the amm_id as the identifier
        amm_id = _safe_get_account(ix.account_keys, 0) or ""

        event_time = _make_event_time(tx.slot, tx.block_time_unix_s)
        if event_time is None:
            return None  # block_time absent — hold pending, never wall-clock date (T-300a)
        return LaunchEvent(
            mint=amm_id,  # amm_id as proxy; enrichment resolves the actual mint
            venue_program_id=self._program_id() or "",
            source=LaunchSource.RAYDIUM_V4,
            event_time=event_time,
            observation_slot=tx.slot,
            confirmation_slot=tx.slot,
            detection_transport=transport,
            sol_reserve_lamports=amount_in,
            token_reserve_base=min_out,
            token_decimals=9,
            initial_holders=0,
            competitors=0,
            creator_wallet=user,
            bundler_cluster_id=None,
            deploy_template_fingerprint=None,
            data_staleness_ms=_staleness_ms(event_time),
        )


# ---------------------------------------------------------------------------
# Raydium CPMM decoder
# ---------------------------------------------------------------------------


class RaydiumCpmmDecoder:
    """Decode Raydium CPMM pool initialization and swaps (Anchor IDL)."""

    def __init__(self, registry: ProgramRegistry) -> None:
        self._registry = registry

    def _program_id(self) -> str | None:
        try:
            return self._registry.program_id(LaunchSource.RAYDIUM_CPMM)
        except KeyError:
            return None

    def decode(
        self,
        tx: RawTransaction,
        transport: DetectionTransport,
    ) -> tuple[LaunchEvent, EventKind] | None:
        pid = self._program_id()
        if pid is None:
            return None

        ixs = [ix for ix in (tx.instructions + tx.inner_instructions) if ix.program_id == pid]
        if not ixs:
            return None

        for ix in ixs:
            ev = self._try_initialize(ix, tx, transport)
            if ev is not None:
                return ev, EventKind.INIT

        for ix in ixs:
            ev = self._try_swap(ix, tx, transport)
            if ev is not None:
                return ev, EventKind.SWAP

        return None

    def _try_initialize(
        self, ix: RawInstruction, tx: RawTransaction, transport: DetectionTransport
    ) -> LaunchEvent | None:
        try:
            data = _b64_decode(ix.data_b64)
        except Exception:
            return None
        if len(data) < 8 or data[:8] != CPMM_DISC_INITIALIZE:
            return None

        # CPMM initialize layout (after 8-byte disc):
        # init_amount_0: u64 @ offset 8
        # init_amount_1: u64 @ offset 16
        # open_time: u64 @ offset 24
        if len(data) < 24:
            return None
        amount_0 = struct.unpack_from("<Q", data, 8)[0]
        amount_1 = struct.unpack_from("<Q", data, 16)[0]

        token0 = _safe_get_account(ix.account_keys, CPMM_INIT_ACCOUNT_IDX_TOKEN0)
        token1 = _safe_get_account(ix.account_keys, CPMM_INIT_ACCOUNT_IDX_TOKEN1)
        creator = _safe_get_account(ix.account_keys, CPMM_INIT_ACCOUNT_IDX_CREATOR) or tx.fee_payer

        SOL_MINT = "So11111111111111111111111111111111111111112"
        if token0 == SOL_MINT:
            mint = token1 or ""
            sol_lamports = amount_0
            token_base = amount_1
        else:
            mint = token0 or ""
            sol_lamports = amount_1
            token_base = amount_0

        if not mint:
            return None

        event_time = _make_event_time(tx.slot, tx.block_time_unix_s)
        if event_time is None:
            return None  # block_time absent — hold pending, never wall-clock date (T-300a)
        return LaunchEvent(
            mint=mint,
            venue_program_id=self._program_id() or "",
            source=LaunchSource.RAYDIUM_CPMM,
            event_time=event_time,
            observation_slot=tx.slot,
            confirmation_slot=tx.slot,
            detection_transport=transport,
            sol_reserve_lamports=sol_lamports,
            token_reserve_base=token_base,
            token_decimals=9,
            initial_holders=0,
            competitors=0,
            creator_wallet=creator,
            bundler_cluster_id=None,
            deploy_template_fingerprint=_fingerprint_by_creator_mint(creator, mint),
            data_staleness_ms=_staleness_ms(event_time),
        )

    def _try_swap(
        self, ix: RawInstruction, tx: RawTransaction, transport: DetectionTransport
    ) -> LaunchEvent | None:
        try:
            data = _b64_decode(ix.data_b64)
        except Exception:
            return None
        is_input = len(data) >= 8 and data[:8] == CPMM_DISC_SWAP_INPUT
        is_output = len(data) >= 8 and data[:8] == CPMM_DISC_SWAP_OUTPUT
        if not (is_input or is_output):
            return None

        # CPMM swap_base_input layout (after 8-byte disc):
        # amount_in: u64 @ offset 8
        # minimum_amount_out: u64 @ offset 16
        if len(data) < 24:
            return None
        amount_in = struct.unpack_from("<Q", data, 8)[0]
        min_out = struct.unpack_from("<Q", data, 16)[0]

        # CPMM swap accounts: [payer, authority, amiConfig, pool_state,
        #   input_token_account, output_token_account, input_vault, output_vault,
        #   input_token_program, output_token_program, input_token_mint, output_token_mint, ...]
        input_mint = _safe_get_account(ix.account_keys, 10)
        output_mint = _safe_get_account(ix.account_keys, 11)
        user = _safe_get_account(ix.account_keys, 0) or tx.fee_payer

        SOL_MINT = "So11111111111111111111111111111111111111112"
        mint = output_mint if input_mint == SOL_MINT else input_mint
        if mint is None:
            mint = input_mint or ""

        event_time = _make_event_time(tx.slot, tx.block_time_unix_s)
        if event_time is None:
            return None  # block_time absent — hold pending, never wall-clock date (T-300a)
        return LaunchEvent(
            mint=mint,
            venue_program_id=self._program_id() or "",
            source=LaunchSource.RAYDIUM_CPMM,
            event_time=event_time,
            observation_slot=tx.slot,
            confirmation_slot=tx.slot,
            detection_transport=transport,
            sol_reserve_lamports=amount_in,
            token_reserve_base=min_out,
            token_decimals=9,
            initial_holders=0,
            competitors=0,
            creator_wallet=user,
            bundler_cluster_id=None,
            deploy_template_fingerprint=None,
            data_staleness_ms=_staleness_ms(event_time),
        )


# ---------------------------------------------------------------------------
# Multi-venue router
# ---------------------------------------------------------------------------


class InstructionRouter:
    """Routes a raw transaction to the appropriate venue decoder.

    Decoders are tried in priority order:
      1. pump.fun (highest value — new launches + migration triggers)
      2. PumpSwap (post-migration AMM)
      3. Raydium V4
      4. Raydium CPMM

    The router is stateless; state lives in the registry.
    """

    def __init__(self, registry: ProgramRegistry) -> None:
        self._decoders: list[_DecoderProtocol] = [
            PumpFunDecoder(registry),
            PumpSwapDecoder(registry),
            RaydiumV4Decoder(registry),
            RaydiumCpmmDecoder(registry),
        ]
        self._active_pids = registry.active_program_ids()

    def route(
        self,
        tx: RawTransaction,
        transport: DetectionTransport,
    ) -> tuple[LaunchEvent, EventKind] | None:
        """Decode a transaction to a (LaunchEvent, EventKind) pair.

        Returns None if no registered decoder matches.
        Skips vote transactions and failed transactions.

        The EventKind discriminates window-opening events (CREATE, WITHDRAW,
        INIT) from trade events (BUY, SELL, SWAP).  The ShadowRecorder uses
        this to ensure only genuine launches open snapshot windows.
        """
        if tx.is_vote or tx.err is not None:
            return None

        # Fast path: check if ANY instruction mentions an active program ID.
        all_pids = {ix.program_id for ix in tx.instructions + tx.inner_instructions}
        if not all_pids.intersection(self._active_pids):
            return None

        for decoder in self._decoders:
            try:
                result = decoder.decode(tx, transport)
                if result is not None:
                    return result
            except Exception as exc:
                logger.warning(
                    "Decoder %s raised on tx %s: %s",
                    type(decoder).__name__,
                    tx.signature,
                    exc,
                    exc_info=True,
                )
        return None
