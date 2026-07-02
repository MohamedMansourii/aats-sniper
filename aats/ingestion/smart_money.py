"""Smart-money / copy-trade wallet stream (T-302, E-M1-04).

E-M1-04 (Wave 1 "go-smart" alpha engine): wires a LIVE SmartWalletBackend
(`GeyserSmartWalletBackend`, backed by a Yellowstone `accounts` subscription
built by `_build_account_subscribe_request()`) behind the SAME
`SmartWalletBackend` Protocol `ReplaySmartWalletBackend` already satisfied.
`SmartWalletStream` is UNCHANGED -- every safety property below is enforced
there exactly as it was for T-302; the live backend only supplies
RawWalletUpdate records, identically to the replay fixture.  The stream stays
DISABLED unless an operator explicitly sets `SmartWalletConfig.enabled=True`
AND wires a backend -- neither of which this change does automatically.

OPERATIONAL CONTRACT (BUILD-DIRECTIVE-v3, AUTONOMY-DIRECTIVE OQ-002, EH-005):
  - DISABLED BY DEFAULT.  The stream is a no-op unless
    `SmartWalletConfig.enabled = True` is explicitly set by the operator.
  - MAXIMUM 20 tracked wallets (0-20).  Any list longer than 20 is truncated
    and a warning is emitted.
  - SELECTIVITY FILTER ONLY.  `smart_wallets_in` is a COUNT (int) — the number
    of tracked wallets that entered a token in the first-K slots.  It is a
    selectivity feature passed to the FeatureFrame.  It is NEVER a standalone
    buy trigger and NEVER causes a blind mirror of a tracked wallet's trade.
  - HONEST LAG ACCOUNTING.  We are behind their fill.  Every SmartMoneyEvent
    carries:
      their_fill_slot    — the on-chain slot at which the tracked wallet filled
      our_event_slot     — the slot at which WE observed the event (always >= their fill)
      entry_lag_slots    — our_event_slot - their_fill_slot  (always >= 0)
      their_fill_block_time_ms  — on-chain block time of THEIR fill (event_time anchor)
      our_observation_ms        — wall-clock at which WE decoded this event
      observation_lag_ms        — our_observation_ms - their_fill_block_time_ms
    These fields are used by feature-quant-engineer to populate
    FeatureFrame.smart_wallet_entry_lag_slots (data-models.md §3).
  - INJECTABLE AND OFFLINE-MOCKABLE.  The stream backend is passed at
    construction.  `ReplaySmartWalletBackend` is the deterministic offline
    fixture source.  `GeyserSmartWalletBackend` is the LIVE production
    implementation (E-M1-04); it is itself built on an injectable
    `AccountSubscriptionSource` (`MockAccountSubscriptionSource` in tests,
    `GeyserAccountSubscriptionSource` live).  No real RPC/gRPC/WS connection
    is ever opened in tests.
  - EXPERIMENTAL / EXPECTED-ZERO (EH-005).  The lift of smart_wallets_in is
    expected to be ~0 given we are always behind their fill slot.  The feature
    is included to measure that expected-zero honestly, not to trade on a
    phantom edge.
  - COPY-TRADE = SELECTIVITY, NEVER A MIRROR.  A non-zero smart_wallets_in
    may increment a selectivity score; it does NOT issue a trade instruction.
    The SNIPE loop makes its own cost-gated, risk-gated decision.
  - MONEY INTEGERS.  sol_amount_lamports is int (lamports), NEVER float.
  - NO SECRETS.  Tracked wallet addresses come from config / env; they are
    public on-chain addresses.  No private keys, no API secrets here.

Point-in-time correctness:
  their_fill_block_time_ms is the AUTHORITATIVE event_time anchor for the
  feature store.  our_observation_ms / wall-clock is for monitoring and lag
  accounting only.  No stored feature field is derived from wall-clock time.

Architecture boundaries (M1 owns; M2/M3/M4 consume):
  - This module produces SmartMoneyEvent and publishes it to the bus stream
    "smart_money.events" (MAXLEN ~10_000).
  - Feature-quant-engineer (T-304) READS smart_money.events and computes
    the smart_wallets_in count feature for the FeatureFrame.  The count math
    belongs to T-304, NOT here.
  - This module NEVER constructs or submits a trade of any kind.  It NEVER issues or
    suggests a trade.  It is READ-SIDE ONLY.
"""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Universal SPL system-program constants (NOT venue IDs — no ProgramRegistry
# governance needed; same convention as the hardcoded VOTE_PROGRAM literal in
# transport.py).  These are canonical, public Solana program IDs, unchanged
# since program inception.
# ---------------------------------------------------------------------------

TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM_ID = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"

# spl_token::state::Account (solana-program-library/token/program/src/state.rs)
# is a STABLE, public binary layout (unchanged since the Token program shipped):
#   offset  0..32  mint    (pubkey)
#   offset 32..64  owner   (pubkey)
#   offset 64..72  amount  (u64, little-endian)
#   ... (delegate / state / is_native / delegated_amount / close_authority)
# Token-2022 accounts share this identical base layout; extensions are
# appended after byte 165 and are irrelevant to balance-delta decoding here.
# This is a universal system-level constant, not a per-tx instruction offset
# guessed from an unconfirmed signature (the M1 "confirm on a real signature"
# guard governs venue-specific instruction decoding, not this public,
# ecosystem-wide account-state layout used by every wallet/explorer/RPC).
SPL_TOKEN_ACCOUNT_MIN_LEN = 72  # enough bytes for mint + owner + amount


# BlockTimeResolver: an injectable async callable that resolves the on-chain
# block_time (ms) for a given slot -- e.g. a getBlockTime RPC call.  Returns
# None if the block time is not yet available.  NEVER wall-clock (T-300a).
BlockTimeResolver = Callable[[int], Awaitable["int | None"]]

# ---------------------------------------------------------------------------
# Stream registry constant (new stream for smart-money events)
# ---------------------------------------------------------------------------

STREAM_SMART_MONEY = "smart_money.events"
MAXLEN_SMART_MONEY = 10_000  # MAXLEN ~ so a slow consumer never OOMs the producer

# ---------------------------------------------------------------------------
# Hard limits (EH-005 / OQ-002 constraints)
# ---------------------------------------------------------------------------

MAX_TRACKED_WALLETS = 20  # 0-20 inclusive; > 20 is a config error, truncated
DISABLED_BY_DEFAULT = True  # explicit operator opt-in required


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass
class SmartWalletConfig:
    """Operator configuration for the smart-money wallet stream.

    DISABLED BY DEFAULT (AUTONOMY-DIRECTIVE OQ-002, EH-005):
      enabled must be explicitly set to True.  When False (the default),
      SmartWalletStream.subscribe() is a no-op and emits no events.

    wallet_addresses: list of 0-20 base58 pubkeys to track.  Wallets beyond
      the 20th are silently truncated (with a WARNING log).  An empty list
      with enabled=True is legal (stream runs but never emits events).

    first_k_slots: the observation window (slots) within which a wallet entry
      counts as "smart_wallets_in" for a given token launch.  Must match the
      K used by feature-quant-engineer (T-304) — they share the same K.
    """

    enabled: bool = False  # DISABLED BY DEFAULT — must be explicitly set True
    wallet_addresses: list[str] = field(default_factory=list)
    first_k_slots: int = 10  # window size; must match FeatureFrame.first_k_slots

    def __post_init__(self) -> None:
        if len(self.wallet_addresses) > MAX_TRACKED_WALLETS:
            logger.warning(
                "SmartWalletConfig: %d wallets provided but max is %d — "
                "truncating to %d (OQ-002)",
                len(self.wallet_addresses),
                MAX_TRACKED_WALLETS,
                MAX_TRACKED_WALLETS,
            )
            self.wallet_addresses = self.wallet_addresses[:MAX_TRACKED_WALLETS]
        if len(self.wallet_addresses) > 0 and not self.enabled:
            logger.info(
                "SmartWalletConfig: %d wallets configured but stream is DISABLED "
                "(set enabled=True to activate — OQ-002 / EH-005)",
                len(self.wallet_addresses),
            )

    @property
    def wallet_set(self) -> frozenset[str]:
        """Immutable set of tracked wallet addresses for O(1) lookup."""
        return frozenset(self.wallet_addresses)


# ---------------------------------------------------------------------------
# SmartMoneyEvent — the typed event this module emits
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SmartMoneyEvent:
    """A detected entry or exit by a tracked smart-money wallet.

    HONEST LAG ACCOUNTING (the core purpose of this type):
      We are BEHIND their fill.  The lag fields below prove it and quantify it.
      entry_lag_slots and observation_lag_ms are ALWAYS >= 0.

    Fields:
      wallet          — the tracked wallet's public key (base58)
      mint            — the token mint address the wallet traded
      their_fill_slot — on-chain slot of the tracked wallet's fill (AUTHORITATIVE)
      our_event_slot  — the slot at which WE decoded/observed this (>= their_fill_slot)
      entry_lag_slots — our_event_slot - their_fill_slot (>= 0; measures our latency)
      their_fill_block_time_ms — on-chain block time of THEIR fill (ms, event_time anchor)
      our_observation_ms       — wall-clock at which WE decoded this (monitoring only)
      observation_lag_ms       — our_observation_ms - their_fill_block_time_ms (>= 0)
      is_buy          — True = entry (buy); False = exit (sell)
      sol_amount_lamports — trade SOL amount in lamports (integer, NEVER float)
      token_amount_base   — trade token amount in base units (integer, NEVER float)
      data_staleness_ms   — same as observation_lag_ms (alias for bus monitoring field)
      source              — "accounts_subscribe" | "replay" (for provenance tagging)

    Point-in-time anchor:
      their_fill_block_time_ms is the ONLY join key for feature construction.
      our_observation_ms is monitoring only (never a feature, never a join key).
    """

    wallet: str
    mint: str
    their_fill_slot: int               # on-chain slot of THEIR fill — AUTHORITATIVE
    our_event_slot: int                # slot at which WE observed this (>= their_fill_slot)
    entry_lag_slots: int               # = our_event_slot - their_fill_slot  (>= 0)
    their_fill_block_time_ms: int      # on-chain block time of THEIR fill (event_time anchor)
    our_observation_ms: int            # wall-clock at decode (monitoring only, NOT a join key)
    observation_lag_ms: int            # = our_observation_ms - their_fill_block_time_ms  (>= 0)
    is_buy: bool                       # True = entry (buy), False = exit (sell)
    sol_amount_lamports: int           # integer lamports (NEVER float) — money rule
    token_amount_base: int             # integer base units (NEVER float) — money rule
    data_staleness_ms: int             # = observation_lag_ms (bus monitoring field, FR-057)
    source: str                        # "accounts_subscribe" | "replay"

    def __post_init__(self) -> None:
        # Enforce lag invariants: we are BEHIND their fill, always
        if self.our_event_slot < self.their_fill_slot:
            raise ValueError(
                f"SmartMoneyEvent invariant violated: our_event_slot={self.our_event_slot} < "
                f"their_fill_slot={self.their_fill_slot}. We are behind their fill, always."
            )
        if self.entry_lag_slots != self.our_event_slot - self.their_fill_slot:
            raise ValueError(
                f"SmartMoneyEvent invariant violated: entry_lag_slots={self.entry_lag_slots} != "
                f"our_event_slot - their_fill_slot = "
                f"{self.our_event_slot - self.their_fill_slot}."
            )
        if self.observation_lag_ms < 0:
            raise ValueError(
                f"SmartMoneyEvent invariant violated: observation_lag_ms={self.observation_lag_ms} < 0."
            )
        if self.sol_amount_lamports < 0:
            raise ValueError(
                f"SmartMoneyEvent: sol_amount_lamports must be >= 0, got {self.sol_amount_lamports}"
            )
        if self.token_amount_base < 0:
            raise ValueError(
                f"SmartMoneyEvent: token_amount_base must be >= 0, got {self.token_amount_base}"
            )


# ---------------------------------------------------------------------------
# Raw wallet account update (transport-agnostic input)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RawWalletUpdate:
    """A raw account update for a tracked wallet, transport-agnostic.

    This is the input type to SmartWalletDecoder.  The transport backend
    (accounts_subscribe or replay) constructs these; the decoder consumes them.

    wallet              — the wallet pubkey being observed
    mint                — the token mint this update relates to (may be inferred
                          from token account owner resolution)
    fill_slot           — on-chain slot of the change (AUTHORITATIVE event time)
    fill_block_time_ms  — on-chain block time in ms (None if pre-confirmation)
    observation_slot    — slot at which WE observed this update (>= fill_slot)
    observation_wall_ms — wall-clock at decode (monitoring only)
    is_buy              — True = token balance increased (buy); False = decreased (sell)
    sol_delta_lamports  — SOL change in lamports (absolute value, integer >= 0)
    token_delta_base    — token balance change in base units (absolute value, integer >= 0)
    source              — "accounts_subscribe" | "replay"
    """

    wallet: str
    mint: str
    fill_slot: int
    fill_block_time_ms: int | None     # None = pre-confirmation (hold pending)
    observation_slot: int
    observation_wall_ms: int
    is_buy: bool
    sol_delta_lamports: int            # integer lamports
    token_delta_base: int              # integer base units
    source: str = "accounts_subscribe"


# ---------------------------------------------------------------------------
# Backend protocol (injectable — no real RPC in tests)
# ---------------------------------------------------------------------------


class SmartWalletBackend(Protocol):
    """Protocol for the accounts_subscribe stream backend.

    subscribe() is an async generator that yields RawWalletUpdates for the
    given set of tracked wallet addresses.

    The real backend (production) uses Geyser accountSubscribe or a Helius
    enhanced WebSocket accountSubscribe RPC call.  The replay backend is the
    deterministic offline fixture source for tests.

    NOTE: subscribe() is declared as a plain (non-async) method returning
    AsyncIterator so subclasses can implement it as `async def` with `yield`
    and still satisfy the Protocol (same pattern as TransportInterface in
    transport.py — mypy requires the abstract to not be `async def` when
    subclasses use async generators).
    """

    def subscribe(
        self,
        wallet_addresses: frozenset[str],
        last_slot: int = 0,
    ) -> AsyncIterator[RawWalletUpdate]:
        """Yield RawWalletUpdates for tracked wallets from last_slot onward.

        Implementations must:
          - Filter to wallet_addresses only.
          - Resume from last_slot on reconnect.
          - Be idempotent (the stream manager handles dedup at the event level).
        """
        ...  # pragma: no cover


# ---------------------------------------------------------------------------
# ReplaySmartWalletBackend — deterministic offline source for tests
# ---------------------------------------------------------------------------


class ReplaySmartWalletBackend:
    """Deterministic offline replay source for smart-money wallet tests.

    Yields a fixed list of RawWalletUpdates filtered to the given wallet set
    and from last_slot onward.  No network connection required.

    Usage in tests:
        updates = [
            RawWalletUpdate(wallet="WalletA...", mint="MintX...", ...),
        ]
        backend = ReplaySmartWalletBackend(updates)
        stream = SmartWalletStream(config=SmartWalletConfig(enabled=True, wallet_addresses=["WalletA..."]),
                                   backend=backend)
        events = [ev async for ev in stream.subscribe()]
    """

    def __init__(self, updates: list[RawWalletUpdate]) -> None:
        self._updates = updates
        self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected

    async def subscribe(  # type: ignore[override]
        self,
        wallet_addresses: frozenset[str],
        last_slot: int = 0,
    ) -> AsyncIterator[RawWalletUpdate]:
        """Yield fixture updates filtered to wallet_addresses and from last_slot."""
        self._connected = True
        try:
            for update in self._updates:
                if update.observation_slot < last_slot:
                    continue  # resume-from-slot: skip already-processed slots
                if wallet_addresses and update.wallet not in wallet_addresses:
                    continue  # not a tracked wallet
                yield update
        finally:
            self._connected = False


# ---------------------------------------------------------------------------
# RawAccountUpdate — transport-agnostic raw account write (pre-decode input)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RawAccountUpdate:
    """A single raw on-chain account write, transport-agnostic.

    This is the INJECTABLE seam one layer BELOW SmartWalletBackend: an
    AccountSubscriptionSource (live Geyser gRPC, or MockAccountSubscriptionSource
    in tests) yields these.  GeyserSmartWalletBackend turns a stream of
    RawAccountUpdates into RawWalletUpdates (balance deltas) that feed the
    existing SmartWalletDecoder unchanged.

    Fields:
      pubkey        — the account address that was written.  Either a tracked
                      wallet's own (System-Program-owned) account, OR an SPL
                      token account whose `owner` field (decoded from `data`)
                      is a tracked wallet.
      owner_program — the program that owns `pubkey` on-chain (System Program
                      for a wallet's native account; Token / Token-2022
                      program for an SPL token account).
      slot          — on-chain slot of this write (AUTHORITATIVE; T-300a).
      lamports      — native SOL lamports currently held by `pubkey`.
      data          — raw account data bytes.  Empty for a plain system
                      account; >=165 bytes (SPL Token Account layout) for a
                      token account.
      write_version — Geyser per-slot write-ordering counter (monitoring only).
      is_startup    — True for the INITIAL snapshot delivered right after
                      subscribing (the pre-existing balance — NOT a "fill").
                      Used only to seed the baseline; never turned into a
                      SmartMoneyEvent.
    """

    pubkey: str
    owner_program: str
    slot: int
    lamports: int
    data: bytes
    write_version: int
    is_startup: bool = False


# ---------------------------------------------------------------------------
# AccountSubscriptionSource — the injectable raw transport (Protocol)
# ---------------------------------------------------------------------------


class AccountSubscriptionSource(Protocol):
    """Protocol for the raw accountSubscribe transport.

    Same injectable pattern as SmartWalletBackend / TransportInterface: the
    LIVE implementation (GeyserAccountSubscriptionSource) opens a real
    Yellowstone gRPC channel; MockAccountSubscriptionSource is the
    deterministic, no-network test double used by every test in this module.
    """

    def subscribe(
        self,
        wallet_addresses: frozenset[str],
        last_slot: int = 0,
    ) -> AsyncIterator[RawAccountUpdate]:
        """Yield RawAccountUpdates for tracked wallets from last_slot onward."""
        ...  # pragma: no cover


# ---------------------------------------------------------------------------
# MockAccountSubscriptionSource — deterministic offline test double
# ---------------------------------------------------------------------------


class MockAccountSubscriptionSource:
    """Deterministic, no-network test double for AccountSubscriptionSource.

    NOTE: this is NOT ReplaySmartWalletBackend (which replays already-decoded
    RawWalletUpdate objects).  MockAccountSubscriptionSource replays RAW,
    pre-decode account writes -- it exercises GeyserSmartWalletBackend's SPL
    Token Account decode + balance-delta logic end-to-end, exactly the way
    the live Geyser accountSubscribe path does.  No real gRPC/WS connection
    is ever opened by this class; it is for tests only.
    """

    def __init__(self, updates: list[RawAccountUpdate]) -> None:
        self._updates = updates
        self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected

    async def subscribe(  # type: ignore[override]
        self,
        wallet_addresses: frozenset[str],
        last_slot: int = 0,
    ) -> AsyncIterator[RawAccountUpdate]:
        self._connected = True
        try:
            for update in self._updates:
                if update.slot < last_slot:
                    continue  # resume-from-slot: skip already-processed slots
                yield update
        finally:
            self._connected = False


# ---------------------------------------------------------------------------
# SPL Token Account decode helper (pure function)
# ---------------------------------------------------------------------------


def _decode_spl_token_account(data: bytes) -> tuple[str, str, int] | None:
    """Decode (mint, owner, amount) from the canonical SPL Token Account layout.

    See the SPL_TOKEN_ACCOUNT_MIN_LEN module comment for the layout citation.
    Returns None (never raises) if `data` is too short to hold the base
    fields -- a live feed can legitimately deliver an unrelated or partial
    account write; a decode failure must never crash the stream.
    """
    if len(data) < SPL_TOKEN_ACCOUNT_MIN_LEN:
        return None
    from aats.ingestion.transport import _bytes_to_base58

    mint = _bytes_to_base58(bytes(data[0:32]))
    owner = _bytes_to_base58(bytes(data[32:64]))
    amount = int.from_bytes(bytes(data[64:72]), "little")
    return (mint, owner, amount)


# ---------------------------------------------------------------------------
# _build_account_subscribe_request — pure helper (testable without a live channel)
# ---------------------------------------------------------------------------


def _build_account_subscribe_request(
    geyser_pb2: object,
    wallet_addresses: frozenset[str],
    from_slot: int,
    commitment: int,
) -> object:
    """Build a Yellowstone SubscribeRequest scoped to tracked wallets ONLY.

    Mirrors the pure-helper pattern of `_build_subscribe_request()` in
    transport.py (finding B2): no I/O, fully unit-testable without a live
    gRPC channel.

    FILTER SCOPING (never a firehose):
      - "native": one shared `accounts` filter group with
        `account = list(wallet_addresses)` -- tracks the wallets' OWN native
        SOL balance writes.
      - "tok_<i>" (one group PER wallet): `owner = [TOKEN_PROGRAM_ID,
        TOKEN_2022_PROGRAM_ID]` AND `filters = [memcmp(offset=32,
        bytes=wallet_pubkey_bytes)]` -- tracks ONLY SPL token accounts whose
        `owner` field (byte offset 32 in the SPL Token Account layout) is
        THIS specific wallet.  A separate group per wallet is required
        because Yellowstone ANDs all filters within one group; an
        owner-only filter with no memcmp would match every token account
        on-chain -- exactly the firehose this module must avoid.

    Args:
        geyser_pb2: the imported geyser_pb2 module (injectable for tests).
        wallet_addresses: tracked wallet pubkeys (already capped to <= 20).
        from_slot: resume slot for reconnects (0 = start from live tip).
        commitment: CommitmentLevel value (0 = PROCESSED).
    """
    from aats.ingestion.transport import _base58_decode

    request = geyser_pb2.SubscribeRequest()

    sorted_wallets = sorted(wallet_addresses)

    if sorted_wallets:
        native_group = request.accounts["native"]
        for w in sorted_wallets:
            native_group.account.append(w)

    for i, wallet in enumerate(sorted_wallets):
        tok_group = request.accounts[f"tok_{i}"]
        tok_group.owner.append(TOKEN_PROGRAM_ID)
        tok_group.owner.append(TOKEN_2022_PROGRAM_ID)
        memcmp_filter = tok_group.filters.add()
        memcmp_filter.memcmp.offset = 32
        memcmp_filter.memcmp.bytes = _base58_decode(wallet)

    request.commitment = commitment  # 0 = PROCESSED
    if from_slot > 0:
        request.from_slot = from_slot

    return request


# ---------------------------------------------------------------------------
# GeyserAccountSubscriptionSource — LIVE Yellowstone accountSubscribe transport
# ---------------------------------------------------------------------------


class GeyserAccountSubscriptionSource:
    """LIVE Yellowstone/Geyser gRPC accountSubscribe transport.

    Subscribes to the `accounts` filters built by
    `_build_account_subscribe_request()` (scoped to tracked wallets only --
    never a firehose) and yields RawAccountUpdate for every account write.

    CREDENTIALS -- from environment only, NEVER hardcoded or logged:
      GEYSER_ENDPOINT, GEYSER_TOKEN (same variables GeyserTransport uses).

    RECONNECT:
      Exponential backoff + jitter (mirrors GeyserTransport exactly).
      Resumes `from_slot` = the last slot successfully yielded.  A dead feed
      never fails silently -- it surfaces via SmartWalletStreamStats.data_staleness_ms
      rising in the caller (SmartWalletStream) while this class retries.

    GRACEFUL DEGRADATION:
      - Empty GEYSER_ENDPOINT or empty wallet_addresses: logs a clear error
        and yields nothing (no crash).
      - gRPC error mid-stream: reconnects from last_slot.
    """

    _RETRY_MIN_WAIT_S: float = 1.0
    _RETRY_MAX_WAIT_S: float = 30.0
    _RETRY_MULTIPLIER: float = 2.0

    def __init__(
        self,
        endpoint: str,       # env: GEYSER_ENDPOINT -- NEVER a literal in code
        x_token: str,        # env: GEYSER_TOKEN -- NEVER hardcoded, NEVER logged
        commitment: int = 0,  # CommitmentLevel.PROCESSED = 0
        max_reconnect_attempts: int = 0,  # 0 = infinite
    ) -> None:
        self._endpoint = endpoint
        self._x_token = x_token  # NOT logged, NOT serialized
        self._commitment = commitment
        self._max_reconnect_attempts = max_reconnect_attempts
        self._connected = False
        import random
        self._rng = random.Random()

    @property
    def is_connected(self) -> bool:
        return self._connected

    async def subscribe(  # type: ignore[override]
        self,
        wallet_addresses: frozenset[str],
        last_slot: int = 0,
    ) -> AsyncIterator[RawAccountUpdate]:
        """Stream RawAccountUpdates from Yellowstone Geyser accountSubscribe."""
        if not self._endpoint:
            logger.error(
                "GeyserAccountSubscriptionSource.subscribe(): GEYSER_ENDPOINT is not "
                "set.  Set GEYSER_ENDPOINT + GEYSER_TOKEN (see .env.example).  "
                "Yielding nothing -- use MockAccountSubscriptionSource for offline testing."
            )
            return

        if not wallet_addresses:
            logger.info(
                "GeyserAccountSubscriptionSource.subscribe(): wallet_addresses is "
                "empty -- nothing to subscribe to."
            )
            return

        from aats.ingestion.transport import _import_geyser_proto

        try:
            geyser_pb2, geyser_pb2_grpc = _import_geyser_proto()
        except ImportError as exc:
            logger.error("GeyserAccountSubscriptionSource: proto stubs unavailable: %s", exc)
            return

        import asyncio

        import grpc
        import grpc.aio

        current_slot = last_slot
        attempt = 0

        while True:
            attempt += 1
            if self._max_reconnect_attempts > 0 and attempt > self._max_reconnect_attempts:
                logger.error(
                    "GeyserAccountSubscriptionSource: max reconnect attempts (%d) "
                    "reached -- giving up.",
                    self._max_reconnect_attempts,
                )
                break

            try:
                logger.info(
                    "GeyserAccountSubscriptionSource: connecting to %s "
                    "(attempt %d, %d wallets, from_slot=%d)",
                    self._endpoint, attempt, len(wallet_addresses), current_slot,
                )
                async for upd in self._stream_once(
                    geyser_pb2, geyser_pb2_grpc, grpc, wallet_addresses, current_slot
                ):
                    current_slot = upd.slot
                    yield upd
                logger.info(
                    "GeyserAccountSubscriptionSource: stream ended cleanly at "
                    "slot %d -- reconnecting.",
                    current_slot,
                )
            except grpc.aio.AioRpcError as rpc_err:
                self._connected = False
                logger.warning(
                    "GeyserAccountSubscriptionSource: gRPC error (attempt %d): %s %s "
                    "-- reconnecting.",
                    attempt, rpc_err.code(), rpc_err.details(),
                )
            except asyncio.CancelledError:
                self._connected = False
                logger.info("GeyserAccountSubscriptionSource: cancelled -- shutting down.")
                raise
            except Exception as exc:  # noqa: BLE001
                self._connected = False
                logger.warning(
                    "GeyserAccountSubscriptionSource: unexpected error (attempt %d): "
                    "%s -- reconnecting.",
                    attempt, exc, exc_info=True,
                )

            wait_s = min(
                self._RETRY_MIN_WAIT_S * (self._RETRY_MULTIPLIER ** (attempt - 1)),
                self._RETRY_MAX_WAIT_S,
            )
            jitter = self._rng.uniform(0, wait_s * 0.25)
            sleep_s = wait_s + jitter
            logger.info(
                "GeyserAccountSubscriptionSource: waiting %.2fs before reconnect "
                "(from_slot=%d).",
                sleep_s, current_slot,
            )
            await asyncio.sleep(sleep_s)

    async def _stream_once(
        self,
        geyser_pb2: object,
        geyser_pb2_grpc: object,
        grpc: object,
        wallet_addresses: frozenset[str],
        from_slot: int,
    ) -> AsyncIterator[RawAccountUpdate]:
        """Open one gRPC stream and yield RawAccountUpdates until it closes."""
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
            self._endpoint, channel_credentials, options=channel_options,
        ) as channel:
            stub = geyser_pb2_grpc.GeyserStub(channel)
            request = _build_account_subscribe_request(
                geyser_pb2, wallet_addresses, from_slot, self._commitment
            )

            logger.info(
                "GeyserAccountSubscriptionSource: subscribing -- %d wallet(s), "
                "commitment=%d, from_slot=%d",
                len(wallet_addresses), self._commitment, from_slot,
            )

            self._connected = True

            async def _request_iter():
                yield request

            async for update in stub.Subscribe(_request_iter()):
                if not update.HasField("account"):
                    continue  # slot, block_meta, ping, pong -- not an account write
                acc_upd = update.account       # SubscribeUpdateAccount
                info = acc_upd.account          # SubscribeUpdateAccountInfo

                from aats.ingestion.transport import _bytes_to_base58

                yield RawAccountUpdate(
                    pubkey=_bytes_to_base58(bytes(info.pubkey)),
                    owner_program=_bytes_to_base58(bytes(info.owner)),
                    slot=int(acc_upd.slot),
                    lamports=int(info.lamports),
                    data=bytes(info.data),
                    write_version=int(info.write_version),
                    is_startup=bool(acc_upd.is_startup),
                )

        self._connected = False


# ---------------------------------------------------------------------------
# make_rpc_block_time_resolver — LIVE on-chain block-time resolver (getBlockTime)
# ---------------------------------------------------------------------------


def make_rpc_block_time_resolver(rpc_url: str) -> BlockTimeResolver:
    """Build a BlockTimeResolver backed by a standard JSON-RPC getBlockTime call.

    Purely on-chain -- NEVER substitutes wall-clock (T-300a).  Returns None
    (event held pending downstream) if the RPC call fails or the slot has no
    confirmed block time yet.

    Args:
        rpc_url: RPC_PRIMARY from the environment (never hardcoded, never logged).
    """

    async def _resolve(slot: int) -> int | None:
        try:
            import httpx
        except ImportError:
            logger.error(
                "make_rpc_block_time_resolver: httpx not installed -- "
                "cannot resolve block_time for slot %d.",
                slot,
            )
            return None

        # httpx/httpcore emit their OWN "HTTP Request: POST <url> ..." log
        # line at INFO/DEBUG via the standard logging module -- and that
        # line embeds the full URL, api-key included.  Silencing these
        # library loggers is required (not optional) whenever rpc_url may
        # carry a credential in its query string; a permissive root-logger
        # config elsewhere in the app must never be able to leak it.
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)

        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getBlockTime",
            "params": [slot],
        }
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.post(rpc_url, json=payload)
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPStatusError as exc:
            # exc / str(exc) embeds the full request URL -- rpc_url may carry
            # an api-key query param (e.g. Helius).  Log ONLY the status code,
            # NEVER the exception object or the URL.
            logger.debug(
                "getBlockTime(%d) HTTP %s", slot, exc.response.status_code
            )
            return None
        except Exception as exc:  # noqa: BLE001
            # Some httpx exceptions (e.g. ConnectError, ReadTimeout) also
            # stringify the request URL -- log only the exception type.
            logger.debug("getBlockTime(%d) failed (%s)", slot, type(exc).__name__)
            return None

        result = data.get("result")
        if result is None:
            return None
        return int(result) * 1_000  # on-chain unix seconds -> ms

    return _resolve


# ---------------------------------------------------------------------------
# GeyserSmartWalletBackend — LIVE SmartWalletBackend (satisfies the Protocol)
# ---------------------------------------------------------------------------


class GeyserSmartWalletBackend:
    """LIVE SmartWalletBackend: Geyser accountSubscribe -> RawWalletUpdate.

    This is the production implementation of the `SmartWalletBackend`
    Protocol.  It wraps an injectable `AccountSubscriptionSource` (live:
    GeyserAccountSubscriptionSource; tests: MockAccountSubscriptionSource)
    and decodes raw account writes into balance-delta RawWalletUpdates that
    feed the EXISTING, UNCHANGED SmartWalletDecoder / SmartWalletStream --
    every safety property downstream (disabled-by-default, 20-wallet cap,
    dedup, honest lag, no-wall-clock-substitution, count-only selectivity)
    is enforced there and is NOT re-implemented or bypassed here.

    HONEST BLOCK-TIME RESOLUTION (T-300a):
      Geyser account updates carry `slot` but NEVER `block_time`.
      `fill_block_time_ms` is resolved via the injected
      `block_time_resolver(slot)` callable (e.g. `make_rpc_block_time_resolver`,
      an on-chain getBlockTime RPC call).  If the resolver returns None (or
      is not configured, or raises), the RawWalletUpdate carries
      `fill_block_time_ms=None` and the existing SmartWalletDecoder holds the
      event PENDING.  Wall-clock is NEVER substituted.

    HONEST LAG ACCOUNTING:
      `observation_slot` is set equal to `fill_slot`.  Yellowstone's `slot`
      field IS both "the slot this account was written in" and the slot
      carried by the notification -- we do not run an independent
      validator/slot-clock, so we never invent a finer-grained observation
      slot.  `entry_lag_slots` will therefore typically read 0 on this live
      path.  The genuinely non-zero, honestly measured latency is
      `observation_lag_ms` (wall-clock at decode minus the RESOLVED on-chain
      block_time) -- that is where real lag surfaces.  This is a documented
      property of the live path, not a fabricated zero.

    SOL LEG (documented limitation, non-blocking):
      A single SPL Token Account write reveals only the TOKEN balance delta.
      The paired SOL leg of the trade is NOT derivable from that write alone
      (it would require correlating a same-slot native-account lamports
      delta, which is noisy -- fees / rent / other same-slot activity
      confound it).  `sol_amount_lamports` is therefore emitted as `0` on
      this live path.  It is NEVER fabricated or estimated.
      `count_smart_wallets_in()` does not read `sol_amount_lamports`, so
      this has NO safety-critical consequence -- it only affects an
      informational field on the bus.

    STARTUP SNAPSHOT:
      The first observation per (wallet, mint) -- or per wallet for the
      native balance -- is the PRE-EXISTING balance (Yellowstone marks it
      `is_startup=True`).  It seeds the baseline WITHOUT emitting a
      RawWalletUpdate: it is not a "fill", it is the balance that existed
      before we subscribed.

    NEVER A TRADE PATHWAY:
      This class only ever produces a RawWalletUpdate (a data record).  It
      holds no keypair, calls no send/sign/submit path, and is READ-SIDE ONLY.
    """

    def __init__(
        self,
        source: AccountSubscriptionSource,
        block_time_resolver: BlockTimeResolver | None = None,
    ) -> None:
        self._source = source
        self._resolver = block_time_resolver
        # Baselines seeded on the first (startup) observation per key, then
        # updated on every subsequent write so deltas are always relative to
        # the last known balance.
        self._token_baseline: dict[tuple[str, str], int] = {}  # (wallet, mint) -> amount
        self._native_baseline: dict[str, int] = {}              # wallet -> lamports

    @property
    def is_connected(self) -> bool:
        source_is_connected = getattr(self._source, "is_connected", None)
        return bool(source_is_connected) if source_is_connected is not None else False

    async def subscribe(  # type: ignore[override]
        self,
        wallet_addresses: frozenset[str],
        last_slot: int = 0,
    ) -> AsyncIterator[RawWalletUpdate]:
        """Yield RawWalletUpdates decoded from live Geyser account writes."""
        async for acc in self._source.subscribe(wallet_addresses, last_slot):
            update = await self._to_wallet_update(acc, wallet_addresses)
            if update is not None:
                yield update

    async def _to_wallet_update(
        self,
        acc: RawAccountUpdate,
        wallet_addresses: frozenset[str],
    ) -> RawWalletUpdate | None:
        """Turn one RawAccountUpdate into a RawWalletUpdate, or None.

        Returns None for: the wallets' own native-account writes (they carry
        no mint and cannot alone become a SmartMoneyEvent), startup snapshots
        (baseline-only), non-token-account data, untracked owners (defensive
        fail-closed), and zero-delta writes (no balance change).
        """
        # Case 1: the wallet's own native (System-Program-owned) account.
        if acc.pubkey in wallet_addresses:
            self._native_baseline[acc.pubkey] = acc.lamports
            # A native SOL write alone carries no mint and cannot become a
            # SmartMoneyEvent by itself -- seed-only, never emitted.
            return None

        # Case 2: a candidate SPL token account -- decode (mint, owner, amount).
        decoded = _decode_spl_token_account(acc.data)
        if decoded is None:
            return None
        mint, owner, amount = decoded

        if owner not in wallet_addresses:
            # Defensive fail-closed: the source's own memcmp filter should
            # already scope this to tracked wallets, but never trust the
            # upstream transport blindly.
            logger.debug(
                "GeyserSmartWalletBackend: token account owner=%s is not a "
                "tracked wallet -- dropping (defensive filter).",
                owner,
            )
            return None

        key = (owner, mint)
        prior_amount = self._token_baseline.get(key)
        self._token_baseline[key] = amount

        if acc.is_startup or prior_amount is None:
            # Seed the baseline with the pre-existing balance; not a fill.
            return None

        delta = amount - prior_amount
        if delta == 0:
            return None  # no balance change -- not a fill

        is_buy = delta > 0
        token_delta_base = abs(delta)

        fill_block_time_ms: int | None = None
        if self._resolver is not None:
            try:
                fill_block_time_ms = await self._resolver(acc.slot)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "GeyserSmartWalletBackend: block_time_resolver(%d) failed: %s "
                    "-- event will be held PENDING (T-300a: no wall-clock substitution).",
                    acc.slot, exc,
                )
                fill_block_time_ms = None

        return RawWalletUpdate(
            wallet=owner,
            mint=mint,
            fill_slot=acc.slot,
            fill_block_time_ms=fill_block_time_ms,
            observation_slot=acc.slot,  # see class docstring: no independent slot clock
            observation_wall_ms=int(time.time() * 1_000),
            is_buy=is_buy,
            sol_delta_lamports=0,  # see class docstring: SOL leg not derivable from this write
            token_delta_base=token_delta_base,
            source="accounts_subscribe",
        )


def build_geyser_smart_wallet_backend_from_env() -> GeyserSmartWalletBackend:
    """Factory: build a production GeyserSmartWalletBackend from environment.

    Reads GEYSER_ENDPOINT / GEYSER_TOKEN (same variables GeyserTransport
    uses) and RPC_PRIMARY (for the on-chain getBlockTime resolver).  Never
    logs credential values.  Safe to call even with missing env vars: the
    resulting backend degrades gracefully (logs a clear error, yields
    nothing) exactly like GeyserTransport / EnhancedWsFallback do.

    This factory is NOT invoked by any hot-path module by default -- wiring
    it into a runner is a deliberate, explicit operator action (SmartWalletStream
    itself stays DISABLED unless SmartWalletConfig.enabled=True is also set).
    """
    import os

    endpoint = os.environ.get("GEYSER_ENDPOINT", "")
    x_token = os.environ.get("GEYSER_TOKEN") or os.environ.get("GEYSER_X_TOKEN", "")
    rpc_url = os.environ.get("RPC_PRIMARY", "")

    source = GeyserAccountSubscriptionSource(endpoint=endpoint, x_token=x_token)
    resolver = make_rpc_block_time_resolver(rpc_url) if rpc_url else None
    return GeyserSmartWalletBackend(source=source, block_time_resolver=resolver)


# ---------------------------------------------------------------------------
# SmartWalletDecoder — RawWalletUpdate -> SmartMoneyEvent
# ---------------------------------------------------------------------------


class SmartWalletDecoder:
    """Decode a RawWalletUpdate into a SmartMoneyEvent with honest lag accounting.

    This is a PURE FUNCTION (no state).  It stamps all lag fields and enforces
    the "we are behind their fill" invariant.

    If fill_block_time_ms is None (pre-confirmation), the decoder returns None
    (event held pending, consistent with decoders.py _make_event_time behavior).
    Wall-clock is NEVER substituted for fill_block_time_ms.
    """

    def decode(self, update: RawWalletUpdate) -> SmartMoneyEvent | None:
        """Decode a RawWalletUpdate into a SmartMoneyEvent.

        Returns None if fill_block_time_ms is absent (block time not confirmed).
        Wall-clock NEVER substituted for on-chain block time (T-300a law).
        """
        if update.fill_block_time_ms is None or update.fill_block_time_ms <= 0:
            logger.debug(
                "SmartWalletDecoder: wallet=%s mint=%s fill_slot=%d — "
                "fill_block_time_ms absent, event HELD PENDING (no wall-clock substitution)",
                update.wallet,
                update.mint,
                update.fill_slot,
            )
            return None

        # Lag fields — honest accounting: we observe AFTER their fill
        our_event_slot = update.observation_slot
        their_fill_slot = update.fill_slot
        entry_lag_slots = max(0, our_event_slot - their_fill_slot)

        our_obs_ms = update.observation_wall_ms
        their_fill_ms = update.fill_block_time_ms
        observation_lag_ms = max(0, our_obs_ms - their_fill_ms)

        # Money rule enforcement: lamports and token amounts MUST be int (data-models.md §0).
        # Python dataclasses don't enforce type annotations at runtime; reject floats here.
        sol_lam = update.sol_delta_lamports
        tok_base = update.token_delta_base
        if not isinstance(sol_lam, int) or isinstance(sol_lam, bool):
            raise TypeError(
                f"SmartWalletDecoder: sol_delta_lamports must be int (lamports), "
                f"got {type(sol_lam).__name__}={sol_lam!r}. "
                "Money rule violation — floats are forbidden (data-models.md §0)."
            )
        if not isinstance(tok_base, int) or isinstance(tok_base, bool):
            raise TypeError(
                f"SmartWalletDecoder: token_delta_base must be int (base units), "
                f"got {type(tok_base).__name__}={tok_base!r}. "
                "Money rule violation — floats are forbidden (data-models.md §0)."
            )

        return SmartMoneyEvent(
            wallet=update.wallet,
            mint=update.mint,
            their_fill_slot=their_fill_slot,
            our_event_slot=our_event_slot,
            entry_lag_slots=entry_lag_slots,
            their_fill_block_time_ms=their_fill_ms,
            our_observation_ms=our_obs_ms,
            observation_lag_ms=observation_lag_ms,
            is_buy=update.is_buy,
            sol_amount_lamports=sol_lam,
            token_amount_base=tok_base,
            data_staleness_ms=observation_lag_ms,  # alias for FR-057 monitoring
            source=update.source,
        )


# ---------------------------------------------------------------------------
# SmartWalletStream — the main orchestrator
# ---------------------------------------------------------------------------


@dataclass
class SmartWalletStreamStats:
    """Runtime health statistics for the smart-money wallet stream."""

    events_decoded: int = 0
    events_skipped: int = 0   # pre-confirmation or filtered
    decode_errors: int = 0
    last_event_time_ms: int = 0
    last_slot: int = 0

    @property
    def data_staleness_ms(self) -> int:
        """Staleness of the last observed event relative to now (monitoring signal)."""
        if self.last_event_time_ms == 0:
            return 0
        return max(0, int(time.time() * 1_000) - self.last_event_time_ms)


class SmartWalletStream:
    """Smart-money / copy-trade wallet stream (T-302).

    DISABLED BY DEFAULT (config.enabled must be True to emit any events).

    When enabled:
      1. Subscribes to the backend (accounts_subscribe or replay).
      2. Decodes RawWalletUpdates into SmartMoneyEvents with honest lag.
      3. Yields SmartMoneyEvents to the caller (for publishing to the bus).

    The caller (ingestion pipeline) publishes SmartMoneyEvents to
    STREAM_SMART_MONEY via the bus.  The feature-quant-engineer then reads
    the stream and counts how many tracked wallets entered a given token
    within the first-K slots — producing smart_wallets_in (T-304).

    COPY-TRADE CONTRACT (non-waivable):
      This stream is a SELECTIVITY SIGNAL SOURCE.  It answers the question
      "how many proven wallets are in this token?"  It NEVER:
        - Issues or suggests a trade of any kind (no Buy, Exit, Reduce, or Veto).
        - Mirrors a trade blindly (we are always behind; lag is documented).
        - Acts as a standalone buy trigger.
      The SNIPE loop makes its own cost-gated, risk-gated, safety-gated decision.

    Injectable:
      The backend is passed at construction.  Use ReplaySmartWalletBackend for
      offline tests.  The production Geyser accountSubscribe backend plugs in
      via the same interface.
    """

    def __init__(
        self,
        config: SmartWalletConfig,
        backend: SmartWalletBackend | None = None,
    ) -> None:
        self._config = config
        self._backend = backend
        self._decoder = SmartWalletDecoder()
        self.stats = SmartWalletStreamStats()
        # Dedup: (wallet, mint, fill_slot) seen set
        self._seen: set[tuple[str, str, int]] = set()
        self._seen_capacity = 50_000  # bounded dedup cache

    async def subscribe(
        self,
        last_slot: int = 0,
    ) -> AsyncIterator[SmartMoneyEvent]:
        """Yield SmartMoneyEvents for tracked wallets.

        DISABLED CHECK:
          If config.enabled is False, this generator yields nothing immediately
          (no events, no blocking, no error).  This is the safe default.

        Args:
            last_slot: resume from this slot on reconnect (backend passes through).

        Yields:
            SmartMoneyEvent for each decoded wallet entry/exit.
        """
        if not self._config.enabled:
            logger.info(
                "SmartWalletStream.subscribe(): stream DISABLED (config.enabled=False). "
                "No smart-money events will be emitted (OQ-002 / EH-005 default). "
                "Set SmartWalletConfig(enabled=True) to activate."
            )
            return  # DISABLED BY DEFAULT — yield nothing

        if self._backend is None:
            logger.warning(
                "SmartWalletStream.subscribe(): enabled=True but no backend configured. "
                "Inject a SmartWalletBackend (or ReplaySmartWalletBackend for tests). "
                "PLUG_IN_HERE: wire Geyser accountSubscribe or enhanced-WS."
            )
            return

        wallet_set = self._config.wallet_set
        if not wallet_set:
            logger.info(
                "SmartWalletStream.subscribe(): enabled=True but wallet_addresses is empty — "
                "no wallets to track. Yielding no events."
            )
            return

        logger.info(
            "SmartWalletStream.subscribe(): tracking %d wallet(s), first_k_slots=%d, "
            "from_slot=%d (EH-005 EXPERIMENTAL — expected lift ~0; we are behind their fill)",
            len(wallet_set),
            self._config.first_k_slots,
            last_slot,
        )

        async for update in self._backend.subscribe(wallet_set, last_slot):
            try:
                # Dedup on (wallet, mint, fill_slot) — idempotent by construction
                dedup_key = (update.wallet, update.mint, update.fill_slot)
                if dedup_key in self._seen:
                    logger.debug(
                        "SmartWalletStream: dedup skip wallet=%s mint=%s fill_slot=%d",
                        update.wallet, update.mint, update.fill_slot,
                    )
                    continue

                ev = self._decoder.decode(update)
                if ev is None:
                    self.stats.events_skipped += 1
                    continue  # pre-confirmation — held pending

                # Register in dedup cache
                self._seen.add(dedup_key)
                if len(self._seen) > self._seen_capacity:
                    # Evict an arbitrary entry (set has no ordering; approximate LRU)
                    self._seen.discard(next(iter(self._seen)))

                self.stats.events_decoded += 1
                self.stats.last_event_time_ms = ev.their_fill_block_time_ms
                self.stats.last_slot = ev.our_event_slot

                yield ev

            except Exception as exc:
                self.stats.decode_errors += 1
                logger.error(
                    "SmartWalletStream: decode error wallet=%s mint=%s: %s",
                    update.wallet,
                    update.mint,
                    exc,
                    exc_info=True,
                )


# ---------------------------------------------------------------------------
# SmartWalletBusPublisher — wraps the bus to publish SmartMoneyEvents
# ---------------------------------------------------------------------------


class SmartWalletBusPublisher:
    """Publishes SmartMoneyEvents to the Redis Streams bus.

    Wraps BusProducer.xadd to the smart_money.events stream.
    The publisher NEVER blocks on processing — XADD and return.

    Usage:
        publisher = SmartWalletBusPublisher(redis_client)
        stream = SmartWalletStream(config=..., backend=...)
        async for ev in stream.subscribe():
            await publisher.publish(ev)
    """

    def __init__(self, redis_client: Any) -> None:
        self._r = redis_client

    async def publish(self, ev: SmartMoneyEvent) -> str:
        """XADD a SmartMoneyEvent to smart_money.events.

        Returns the Redis stream entry ID.
        Raises on Redis error (a silent dead bus is the worst failure mode).
        """
        import json

        ingest_time_ms = int(time.time() * 1_000)
        payload = {
            # Dedup / provenance
            "wallet": ev.wallet,
            "mint": ev.mint,
            "fill_slot": str(ev.their_fill_slot),
            # Timestamps (explicit separation: event vs ingest)
            "their_fill_block_time_ms": str(ev.their_fill_block_time_ms),
            "our_observation_ms": str(ev.our_observation_ms),
            "ingest_time_ms": str(ingest_time_ms),
            # LAG ACCOUNTING (the core T-302 requirement)
            "entry_lag_slots": str(ev.entry_lag_slots),
            "observation_lag_ms": str(ev.observation_lag_ms),
            # Staleness (monitoring, FR-057)
            "data_staleness_ms": str(ev.data_staleness_ms),
            # Trade direction + amounts (integer lamports / base units only)
            "is_buy": "1" if ev.is_buy else "0",
            "sol_amount_lamports": str(ev.sol_amount_lamports),
            "token_amount_base": str(ev.token_amount_base),
            "source": ev.source,
            # Full event JSON for consumers who want the whole shape
            "event": json.dumps({
                "wallet": ev.wallet,
                "mint": ev.mint,
                "their_fill_slot": ev.their_fill_slot,
                "our_event_slot": ev.our_event_slot,
                "entry_lag_slots": ev.entry_lag_slots,
                "their_fill_block_time_ms": ev.their_fill_block_time_ms,
                "our_observation_ms": ev.our_observation_ms,
                "observation_lag_ms": ev.observation_lag_ms,
                "is_buy": ev.is_buy,
                "sol_amount_lamports": ev.sol_amount_lamports,
                "token_amount_base": ev.token_amount_base,
                "data_staleness_ms": ev.data_staleness_ms,
                "source": ev.source,
            }),
        }
        entry_id: str = await self._r.xadd(
            STREAM_SMART_MONEY,
            payload,
            maxlen=MAXLEN_SMART_MONEY,
            approximate=True,
        )
        logger.debug(
            "smart_money.publish wallet=%s mint=%s lag_slots=%d lag_ms=%d entry=%s",
            ev.wallet, ev.mint, ev.entry_lag_slots, ev.observation_lag_ms, entry_id,
        )
        return entry_id


# ---------------------------------------------------------------------------
# SmartWalletsInCounter — computes the smart_wallets_in count for a token
# ---------------------------------------------------------------------------
# NOTE: This counter lives in M1 as a pure helper that aggregates observed
# SmartMoneyEvents into a count for a given (mint, launch_slot, first_k_slots)
# window.  The full FeatureFrame assembly (with provenance, all other features)
# remains in feature-quant-engineer (T-304).  This helper is M1-owned because
# it needs to know the observation window and the config's first_k_slots.
#
# The counter is STATELESS per call — it re-scans a list of SmartMoneyEvents.
# Tests can drive it directly without any async machinery.


def count_smart_wallets_in(
    events: list[SmartMoneyEvent],
    mint: str,
    launch_slot: int,
    first_k_slots: int,
) -> int:
    """Count unique tracked wallets that entered `mint` within the first K slots.

    This is the smart_wallets_in feature value (data-models.md §3 FeatureFrame).

    SELECTIVITY SEMANTICS:
      - Counts wallets that placed a BUY (is_buy=True) for `mint`.
      - The buy must have occurred within [launch_slot, launch_slot + first_k_slots].
      - Each wallet is counted at most once (unique count).
      - Sells in the same window do NOT decrement the count (we count entries).

    Args:
        events: list of SmartMoneyEvents for any wallet/mint (will be filtered).
        mint: the token mint to count for.
        launch_slot: the on-chain slot of the LaunchEvent (inclusive lower bound).
        first_k_slots: the K-slot window (inclusive upper bound = launch_slot + K).

    Returns:
        int — number of unique tracked wallets that entered `mint` in first K slots.
        NEVER a float. NEVER a trigger. A count only.

    Point-in-time correctness:
        The filter uses their_fill_slot (on-chain, AUTHORITATIVE), not
        our_event_slot or any wall-clock field.  This ensures the count reflects
        what happened on-chain in the K-slot window, regardless of when WE
        observed it.
    """
    upper_slot = launch_slot + first_k_slots
    unique_wallets: set[str] = set()
    for ev in events:
        if ev.mint != mint:
            continue
        if not ev.is_buy:
            continue  # only count entries, not exits
        # Use their_fill_slot (on-chain, point-in-time) as the window filter
        if launch_slot <= ev.their_fill_slot <= upper_slot:
            unique_wallets.add(ev.wallet)
    return len(unique_wallets)


def count_smart_wallet_entry_lag_slots(
    events: list[SmartMoneyEvent],
    mint: str,
    launch_slot: int,
    first_k_slots: int,
    our_decision_slot: int,
) -> int | None:
    """Return our decision slot minus the EARLIEST tracked-wallet fill slot.

    This is the smart_wallet_entry_lag_slots feature (data-models.md §3):
      "our entry slot - their fill slot (we are BEHIND)"

    Returns None if no tracked wallet entered this mint in the window.
    Returns an int >= 0 otherwise (we are always at or behind their fill).

    Args:
        events: list of SmartMoneyEvents (any wallet/mint; will be filtered).
        mint: token mint to compute for.
        launch_slot: inclusive lower bound of the K-slot window.
        first_k_slots: window size.
        our_decision_slot: the slot at which we decide to enter (>= earliest fill).

    Point-in-time correctness:
        Uses their_fill_slot (on-chain AUTHORITATIVE), not observation_slot.
    """
    upper_slot = launch_slot + first_k_slots
    earliest_fill: int | None = None
    for ev in events:
        if ev.mint != mint:
            continue
        if not ev.is_buy:
            continue
        if launch_slot <= ev.their_fill_slot <= upper_slot and (
            earliest_fill is None or ev.their_fill_slot < earliest_fill
        ):
            earliest_fill = ev.their_fill_slot

    if earliest_fill is None:
        return None  # no tracked wallet in window
    # We are BEHIND; lag >= 0 always
    return max(0, our_decision_slot - earliest_fill)
