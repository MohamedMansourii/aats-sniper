"""Smart-money / copy-trade wallet stream (T-302).

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
    fixture source.  No real RPC is called in tests.
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
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Protocol

logger = logging.getLogger(__name__)

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
