"""E14a — insider / dev-sell distribution DETECTION (Pepe-Boost "dev-sell trigger",
detection half; risk-guardrails-engineer's E14b wires the FAST-exit read).

WHAT THIS MODULE DOES
----------------------
For an OPEN-position mint, watch on-chain SELL events by (a) the creator/dev
wallet and (b) a tracked top-holder cluster.  When the CUMULATIVE sold fraction
of that wallet's/cluster's held-supply BASELINE crosses a configured threshold
(event-time, on-chain slot), emit an `InsiderDumpSignal` and PRE-SET a de-risk
flag in the shared StateStore using the SAME mechanism `narrative_failure`
already uses (`aats.controller.state.StateStore.set_insider_dump_flag` /
`get_insider_dump_flag`, mirrored key-for-key — see that module's docstring).

HARD RULES (non-waivable, enforced by construction / asserted in tests)
-------------------------------------------------------------------------
  * DE-RISK ONLY.  This module can only ever SET a flag (a signal that biases
    an exit).  There is no method here that clears a flag, sizes a trade,
    widens a stop, or extends a hold.  The emitted flag is consumed by the
    FAST exit branch as a PRE-SET boolean (E14b) — this module never touches
    the FAST/snipe hot path itself.
  * EVENT-TIME ONLY (T-300a).  `fraction_sold_bps` and `event_slot` are pure
    functions of on-chain data (slot, decoded sell amounts).  Nothing here
    reads `time.time()` into a stored/emitted decision field; wall-clock
    (`detected_wall_ms` on InsiderDumpSignal) is carried for MONITORING ONLY,
    exactly like `EventTime.wall_clock_ms` — never a join key, never part of
    the threshold math.
  * OFF THE HOT PATH.  This is an ENRICHMENT-TIER producer (same posture as
    `enrichment.py`'s Birdeye/DexScreener pollers): it does its (cheap, pure)
    accounting off the FAST/snipe path and writes a flag the FAST loop reads
    as a plain bool.  It performs no signing, no submission, no LLM call.
  * REFUSE-BY-DEFAULT.  If a sold wallet's held-supply baseline is unknown
    (undecodable / never observed), the detector REFUSES to compute a
    fraction — it does NOT treat "unknown" as "0% sold" (which would silently
    suppress a real dump).  The refusal is counted (`stats.
    refused_undecodable_supply`) and logged, never swallowed silently.  The
    default `HeldSupplyProvider` (`StaticHeldSupplyProvider` with an empty
    map) refuses EVERYTHING until a caller explicitly registers a baseline —
    refuse-by-default is the out-of-the-box posture, not an opt-in.
  * IDEMPOTENT / ORDER-TOLERANT (dedup on (signature, instruction_index)).
    Cumulative-sold is a simple non-decreasing sum over deduplicated sells, so
    replaying the same sells out of order or twice yields the IDENTICAL final
    state as in-order, once-each delivery.
  * MONEY RULE.  All token amounts are integer base units.  Float/bool are
    rejected at every boundary (dataclass __post_init__ and the StateStore
    setter).

WHAT THIS MODULE DOES NOT DO (owner boundaries)
------------------------------------------------
  * It does not decide WHEN a position is open — the caller passes
    `is_open_position` (typically `store.load_position(mint) is not None`,
    M3's job).  This keeps the detector framework-agnostic and directly
    testable.
  * It does not build the FAST-loop read path or add insider_dump_flag to
    RuleInputs/ExitConfig — that is E14b (risk-guardrails-engineer).  See the
    module docstring on `aats.controller.state` for the exact flag shape
    E14b reads: `StateStore.get_insider_dump_flag(mint) -> bool`.
  * It does not fetch top-holder lists from Birdeye/DexScreener itself — the
    tracked cluster is injected via `register_cluster()` (operator-configured
    watchlist or an enrichment-derived set built elsewhere); this keeps
    detection a pure, offline-testable function of (creator/cluster
    registration, held-supply baseline, decoded sell stream).
  * It does not touch `aats/contracts/` — no frozen contract is edited.  The
    `InsiderDumpSignal` type here is M1-owned (like `SmartMoneyEvent` in
    `smart_money.py`), not part of `data-models.md`.

SOURCE OF SELL EVENTS
----------------------
`sell_observation_from_launch_event()` adapts the EXISTING decoded feed:
`aats.ingestion.decoders.PumpFunDecoder._try_sell()` already discriminates a
genuine SELL (via `EventKind.SELL`) from a BUY, and reuses `LaunchEvent`
(`creator_wallet` = the seller, `token_reserve_base` = tokens sold,
`event_time.slot`/`event_time.block_time_ms` = on-chain anchors) — see
`decoders.py` module docstring.  PumpSwap/Raydium post-migration swaps are
NOT yet separately discriminated buy-vs-sell (`EventKind.SWAP` conflates
both) — this adapter only accepts `EventKind.SELL` and is honest about that
scope limit rather than guessing.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from aats.contracts.events import LaunchEvent
    from aats.ingestion.decoders import EventKind

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Bus stream registry (mirrors smart_money.py's STREAM_SMART_MONEY pattern)
# ---------------------------------------------------------------------------

STREAM_INSIDER_DUMP = "insider_dump.events"
MAXLEN_INSIDER_DUMP = 5_000  # MAXLEN ~ so a slow consumer never OOMs the producer

_BPS_DENOM = 10_000

# Defensive cap on tracked cluster size per mint (mirrors smart_money.py's
# MAX_TRACKED_WALLETS convention — an unbounded cluster is a firehose, not a
# targeted watchlist).
MAX_CLUSTER_WALLETS = 50


# ---------------------------------------------------------------------------
# Wallet role
# ---------------------------------------------------------------------------


class WalletRole(StrEnum):
    """Which tracked role a selling wallet occupies for a given mint."""

    CREATOR = "creator"  # the pump.fun create-tx payer / dev wallet
    CLUSTER = "cluster"  # a tracked top-holder-cluster member (operator/enrichment-fed)


# ---------------------------------------------------------------------------
# Type guards (money rule; mirrors exit_engine.py's boundary checks)
# ---------------------------------------------------------------------------


def _check_int(name: str, value: object, *, min_value: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be int, not {type(value).__name__}={value!r}.")
    if min_value is not None and value < min_value:
        raise ValueError(f"{name} must be >= {min_value}, got {value}.")
    return value


def _check_str(name: str, value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty str, got {value!r}.")
    return value


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InsiderDumpConfig:
    """Detector configuration.  De-risk-shaped by construction.

    threshold_bps: the cumulative-sold-of-held-supply fraction (bps, [1, 10000])
        that triggers the flag.  Lower == MORE sensitive (more de-risk-prone);
        there is no "raise the threshold to hold longer" lever exposed on the
        hot path — this is a detection-config constant set by the operator.
    flag_ttl_seconds: TTL for the StateStore flag — mirrors
        set_narrative_failure_flag's default (120s).
    """

    threshold_bps: int = 2_000  # 20% of held-supply baseline sold, cumulative
    flag_ttl_seconds: int = 120

    def __post_init__(self) -> None:
        thr = _check_int("InsiderDumpConfig.threshold_bps", self.threshold_bps, min_value=1)
        if thr > _BPS_DENOM:
            raise ValueError(
                f"InsiderDumpConfig.threshold_bps must be <= 10000 (a fraction), got {thr}."
            )
        _check_int("InsiderDumpConfig.flag_ttl_seconds", self.flag_ttl_seconds, min_value=1)


# ---------------------------------------------------------------------------
# SellObservation — transport-agnostic decoded SELL event input
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SellObservation:
    """A single decoded on-chain SELL, transport-agnostic (mirrors RawWalletUpdate).

    mint                    — the token mint sold.
    wallet                  — the seller (on-chain signer/authority).
    token_amount_sold_base  — tokens sold, integer base units (NEVER float).
    event_slot              — on-chain slot of the sell (AUTHORITATIVE, T-300a).
    block_time_ms           — on-chain block time (ms) of the sell.
    signature                — tx signature (dedup key component).
    instruction_index        — instruction index within the tx (dedup key component).
    source                    — provenance tag ("decoder" | "replay").
    """

    mint: str
    wallet: str
    token_amount_sold_base: int
    event_slot: int
    block_time_ms: int
    signature: str
    instruction_index: int = 0
    source: str = "decoder"

    def __post_init__(self) -> None:
        _check_str("SellObservation.mint", self.mint)
        _check_str("SellObservation.wallet", self.wallet)
        _check_int(
            "SellObservation.token_amount_sold_base", self.token_amount_sold_base, min_value=0
        )
        _check_int("SellObservation.event_slot", self.event_slot, min_value=0)
        _check_int("SellObservation.block_time_ms", self.block_time_ms, min_value=0)
        _check_int("SellObservation.instruction_index", self.instruction_index, min_value=0)
        _check_str("SellObservation.signature", self.signature)

    @property
    def dedupe_key(self) -> tuple[str, int]:
        """Idempotency key — (signature, instruction_index), per module standard."""
        return (self.signature, self.instruction_index)


def sell_observation_from_launch_event(
    *,
    signature: str,
    event: LaunchEvent,
    kind: EventKind,
    instruction_index: int = 0,
) -> SellObservation | None:
    """Adapt the EXISTING decoded feed (`decoders.PumpFunDecoder._try_sell`)
    into a `SellObservation`.

    Returns None for anything that is not a genuine, decoded SELL (`kind` must
    be `EventKind.SELL`) — this adapter is honest about only covering the
    pump.fun bonding-curve sell path, where buy/sell are already cleanly
    discriminated (see module docstring).  Requires a CONFIRMED event_time
    (the decoder itself never emits a LaunchEvent with block_time absent —
    T-300a — so this is a defensive re-check, not a new gate).
    """
    from aats.ingestion.decoders import EventKind as _EventKind

    if kind is not _EventKind.SELL:
        return None
    if event.event_time is None:  # defensive; decoders.py never emits this
        return None
    return SellObservation(
        mint=event.mint,
        wallet=event.creator_wallet,  # decoders.py reuses this field to carry the seller
        token_amount_sold_base=event.token_reserve_base,
        event_slot=event.event_time.slot,
        block_time_ms=event.event_time.block_time_ms,
        signature=signature,
        instruction_index=instruction_index,
        source="decoder",
    )


# ---------------------------------------------------------------------------
# Held-supply baseline provider (injectable — REFUSE-BY-DEFAULT)
# ---------------------------------------------------------------------------


class HeldSupplyProvider(Protocol):
    """Resolves a wallet's held-supply BASELINE for a mint, in base units.

    Returns None for UNKNOWN / UNDECODABLE — the caller (InsiderDumpDetector)
    MUST treat None as a refusal to compute a fraction, never as "0% sold".
    """

    def held_supply_base(self, mint: str, wallet: str) -> int | None: ...  # pragma: no cover


class StaticHeldSupplyProvider:
    """Operator/enrichment-seeded held-supply baselines (REFUSE-BY-DEFAULT).

    Starts EMPTY: every lookup returns None (refuse) until a baseline is
    explicitly registered via `set_held_supply()`.  This is the deliberate
    default posture — the detector never silently assumes a baseline.

    A baseline is typically captured once, at position-OPEN time, from the
    best available on-chain/enrichment read of the wallet's current token
    balance for the mint (e.g. an SPL Token Account snapshot, mirroring the
    balance-tracking pattern already proven in `smart_money.py`).  Sourcing
    that snapshot live is a deliberate extension point left to the caller
    (e.g. wiring this to `smart_money.py`'s `AccountSubscriptionSource` /
    `_decode_spl_token_account` machinery) — this module only requires the
    Protocol above be satisfied.
    """

    def __init__(self) -> None:
        self._baselines: dict[tuple[str, str], int] = {}

    def set_held_supply(self, mint: str, wallet: str, amount_base: int) -> None:
        """Register a held-supply baseline.  Refuses a degenerate (<=0) baseline
        (a zero/negative denominator can never yield a meaningful fraction —
        registering one would be worse than refusing, so it is rejected here
        rather than silently accepted and later mis-divided)."""
        _check_str("StaticHeldSupplyProvider.mint", mint)
        _check_str("StaticHeldSupplyProvider.wallet", wallet)
        amt = _check_int("StaticHeldSupplyProvider.amount_base", amount_base, min_value=1)
        self._baselines[(mint, wallet)] = amt

    def held_supply_base(self, mint: str, wallet: str) -> int | None:
        return self._baselines.get((mint, wallet))


# ---------------------------------------------------------------------------
# InsiderDumpSignal — the M1-owned typed detection event (bus payload)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InsiderDumpSignal:
    """The detection result the instant the cumulative-sold threshold is crossed.

    `event_slot` is the on-chain slot of the sell that CROSSED the threshold
    (event-time, T-300a).  `detected_wall_ms` is wall-clock at decode — carried
    for MONITORING ONLY (never a join key, mirrors EventTime.wall_clock_ms).
    """

    mint: str
    wallet: str
    role: WalletRole
    fraction_sold_bps: int  # [0, 10000] — cumulative sold / held-supply baseline
    event_slot: int  # on-chain slot — AUTHORITATIVE (T-300a)
    block_time_ms: int  # on-chain block time of the crossing sell
    cumulative_sold_base: int  # running total sold by this wallet, this mint
    held_supply_base: int  # the baseline denominator used
    detected_wall_ms: int = field(default_factory=lambda: int(time.time() * 1_000))

    def __post_init__(self) -> None:
        _check_str("InsiderDumpSignal.mint", self.mint)
        _check_str("InsiderDumpSignal.wallet", self.wallet)
        frac = _check_int(
            "InsiderDumpSignal.fraction_sold_bps", self.fraction_sold_bps, min_value=0
        )
        if frac > _BPS_DENOM:
            raise ValueError(f"InsiderDumpSignal.fraction_sold_bps must be <= 10000, got {frac}.")
        _check_int("InsiderDumpSignal.event_slot", self.event_slot, min_value=0)
        _check_int("InsiderDumpSignal.block_time_ms", self.block_time_ms, min_value=0)
        _check_int("InsiderDumpSignal.cumulative_sold_base", self.cumulative_sold_base, min_value=0)
        _check_int("InsiderDumpSignal.held_supply_base", self.held_supply_base, min_value=1)


# ---------------------------------------------------------------------------
# StateStore write surface — structural Protocol (no M1->M3 import dependency)
# ---------------------------------------------------------------------------


class InsiderDumpFlagSink(Protocol):
    """The minimal write surface this detector needs from the shared StateStore.

    Structural (duck-typed): `aats.controller.state.StateStore` (both
    `InMemoryStateStore` and any future Redis-backed implementation) satisfies
    this WITHOUT M1 importing M3's controller package — M1 emits, M3 owns the
    shared state layer (module boundary).
    """

    def set_insider_dump_flag(
        self,
        mint: str,
        *,
        fraction_sold_bps: int,
        event_slot: int,
        ttl_seconds: int = 120,
    ) -> None: ...  # pragma: no cover


# ---------------------------------------------------------------------------
# Stats (observability — proves "refuse" is never silent)
# ---------------------------------------------------------------------------


@dataclass
class InsiderDumpStats:
    """Runtime counters.  `refused_undecodable_supply` is the load-bearing one:
    it PROVES an unknown-supply sell was counted distinctly from a genuine
    below-threshold no-op, never silently folded into "no dump"."""

    sells_observed: int = 0
    duplicates_skipped: int = 0
    not_open_position_skipped: int = 0
    untracked_sells: int = 0
    refused_undecodable_supply: int = 0
    signals_emitted: int = 0
    last_event_slot: int = 0


# ---------------------------------------------------------------------------
# InsiderDumpDetector — the main class
# ---------------------------------------------------------------------------


class InsiderDumpDetector:
    """Stateful, deterministic, de-risk-only insider/dev-sell dump detector.

    USAGE (off the hot path — the SLOW-loop / enrichment runner drives this):

        detector = InsiderDumpDetector(
            config=InsiderDumpConfig(threshold_bps=2_000),
            held_supply=held_supply_provider,   # REFUSES until seeded
            flag_sink=state_store,              # satisfies InsiderDumpFlagSink
        )
        detector.register_creator(mint, creator_wallet)      # from the CREATE event
        detector.register_cluster(mint, {"WalletA...", ...})  # operator/enrichment-fed
        held_supply_provider.set_held_supply(mint, creator_wallet, baseline_amount)

        # per decoded sell:
        obs = sell_observation_from_launch_event(signature=tx.signature, event=ev, kind=kind)
        if obs is not None:
            signal = detector.on_sell(obs, is_open_position=store.load_position(mint) is not None)
            if signal is not None:
                await bus_publisher.publish(signal)   # audit trail (the "/ bus" half)
                # the StateStore flag is ALREADY set by on_sell() via flag_sink

    De-risk-only: `on_sell` can only ever SET the flag (via flag_sink) — there
    is no method here that clears it, and cumulative-sold is monotone
    non-decreasing per (mint, wallet), so `fraction_sold_bps` can never fall
    within a position's life.  Idempotent: dedup on (signature,
    instruction_index) makes replay of out-of-order/duplicated sells produce
    the IDENTICAL cumulative state as in-order, once-each delivery.
    """

    def __init__(
        self,
        *,
        config: InsiderDumpConfig | None = None,
        held_supply: HeldSupplyProvider | None = None,
        flag_sink: InsiderDumpFlagSink | None = None,
        seen_capacity: int = 50_000,
    ) -> None:
        self._config = config or InsiderDumpConfig()
        # REFUSE-BY-DEFAULT: an empty StaticHeldSupplyProvider refuses every
        # lookup until the caller explicitly seeds a baseline.
        self._held_supply = held_supply or StaticHeldSupplyProvider()
        self._flag_sink = flag_sink
        self.stats = InsiderDumpStats()

        self._creator_wallet: dict[str, str] = {}
        self._cluster_wallets: dict[str, frozenset[str]] = {}
        self._cumulative_sold: dict[tuple[str, str], int] = {}
        self._seen: set[tuple[str, int]] = set()
        self._seen_capacity = seen_capacity

    @property
    def config(self) -> InsiderDumpConfig:
        return self._config

    # -- registration --------------------------------------------------- #

    def register_creator(self, mint: str, wallet: str) -> None:
        """Record the creator/dev wallet for `mint` (from the decoded CREATE event)."""
        _check_str("register_creator.mint", mint)
        _check_str("register_creator.wallet", wallet)
        self._creator_wallet[mint] = wallet

    def register_cluster(self, mint: str, wallets: frozenset[str] | set[str] | list[str]) -> None:
        """Record the tracked top-holder cluster for `mint`.

        Capped at MAX_CLUSTER_WALLETS (truncated with a WARNING) — an
        unbounded cluster is a firehose, not a targeted watchlist, mirroring
        `smart_money.py`'s MAX_TRACKED_WALLETS convention.
        """
        _check_str("register_cluster.mint", mint)
        normalized = frozenset(w for w in wallets if isinstance(w, str) and w)
        if len(normalized) > MAX_CLUSTER_WALLETS:
            logger.warning(
                "InsiderDumpDetector.register_cluster: mint=%s %d wallets provided but "
                "max is %d — truncating.",
                mint,
                len(normalized),
                MAX_CLUSTER_WALLETS,
            )
            normalized = frozenset(sorted(normalized)[:MAX_CLUSTER_WALLETS])
        self._cluster_wallets[mint] = normalized

    def reset_mint(self, mint: str) -> None:
        """Clear all tracked state for `mint` (call on position CLOSE/re-entry
        so a stale cumulative from a prior trade never leaks into a new one)."""
        self._creator_wallet.pop(mint, None)
        self._cluster_wallets.pop(mint, None)
        stale_keys = [k for k in self._cumulative_sold if k[0] == mint]
        for k in stale_keys:
            del self._cumulative_sold[k]

    def _role_of(self, mint: str, wallet: str) -> WalletRole | None:
        if self._creator_wallet.get(mint) == wallet:
            return WalletRole.CREATOR
        if wallet in self._cluster_wallets.get(mint, frozenset()):
            return WalletRole.CLUSTER
        return None

    # -- the enrichment-tier hot call (off the FAST/snipe path) ---------- #

    def on_sell(self, obs: SellObservation, *, is_open_position: bool) -> InsiderDumpSignal | None:
        """Process one decoded SELL.  Returns a signal iff the threshold was
        JUST crossed by this sell; otherwise None (hold, refuse, or duplicate).

        PURE except for the deterministic flag_sink.set_insider_dump_flag()
        call and stats bookkeeping — no HTTP, no LLM, no wall-clock in the
        threshold math.  Safe to call from a SLOW-loop / enrichment worker;
        never call this from the FAST/snipe hot path (heavy-ish dict/loop
        work, explicitly off that path per the module charter).
        """
        if not is_open_position:
            self.stats.not_open_position_skipped += 1
            return None

        self.stats.sells_observed += 1
        self.stats.last_event_slot = max(self.stats.last_event_slot, obs.event_slot)

        if obs.dedupe_key in self._seen:
            self.stats.duplicates_skipped += 1
            logger.debug(
                "InsiderDumpDetector: dedup skip mint=%s wallet=%s sig=%s ix=%d",
                obs.mint,
                obs.wallet,
                obs.signature,
                obs.instruction_index,
            )
            return None
        self._seen.add(obs.dedupe_key)
        if len(self._seen) > self._seen_capacity:
            self._seen.discard(next(iter(self._seen)))  # approximate eviction

        role = self._role_of(obs.mint, obs.wallet)
        if role is None:
            self.stats.untracked_sells += 1
            return None  # a non-creator/non-cluster sell is a normal no-op

        held = self._held_supply.held_supply_base(obs.mint, obs.wallet)
        if held is None or held <= 0:
            # REFUSE-BY-DEFAULT: unknown/undecodable held supply is NEVER
            # treated as "0% sold" — count it distinctly and hold (no flag).
            self.stats.refused_undecodable_supply += 1
            logger.warning(
                "InsiderDumpDetector: REFUSING mint=%s wallet=%s role=%s — held-supply "
                "baseline is unknown/undecodable.  This sell is NOT counted as "
                "'no dump'; it is excluded from the fraction until a baseline is "
                "registered (refuse-by-default).",
                obs.mint,
                obs.wallet,
                role.value,
            )
            return None

        key = (obs.mint, obs.wallet)
        prior_cumulative = self._cumulative_sold.get(key, 0)
        new_cumulative = prior_cumulative + obs.token_amount_sold_base
        self._cumulative_sold[key] = new_cumulative  # monotone non-decreasing

        fraction_bps = min(_BPS_DENOM, (new_cumulative * _BPS_DENOM) // held)

        if fraction_bps < self._config.threshold_bps:
            return None  # below threshold — hold, no flag

        signal = InsiderDumpSignal(
            mint=obs.mint,
            wallet=obs.wallet,
            role=role,
            fraction_sold_bps=fraction_bps,
            event_slot=obs.event_slot,
            block_time_ms=obs.block_time_ms,
            cumulative_sold_base=new_cumulative,
            held_supply_base=held,
        )

        if self._flag_sink is not None:
            self._flag_sink.set_insider_dump_flag(
                obs.mint,
                fraction_sold_bps=fraction_bps,
                event_slot=obs.event_slot,
                ttl_seconds=self._config.flag_ttl_seconds,
            )

        self.stats.signals_emitted += 1
        logger.warning(
            "InsiderDumpDetector: INSIDER DUMP mint=%s wallet=%s role=%s "
            "fraction_sold_bps=%d event_slot=%d — de-risk flag SET.",
            obs.mint,
            obs.wallet,
            role.value,
            fraction_bps,
            obs.event_slot,
        )
        return signal


# ---------------------------------------------------------------------------
# InsiderDumpBusPublisher — the "/ bus" half (audit trail, not the hot path)
# ---------------------------------------------------------------------------


class InsiderDumpBusPublisher:
    """Publishes InsiderDumpSignals to the Redis Streams bus (insider_dump.events).

    Mirrors `smart_money.py`'s `SmartWalletBusPublisher` exactly.  XADD and
    return — never blocks on downstream processing.  This is the audit trail;
    the StateStore flag (set synchronously by `InsiderDumpDetector.on_sell`)
    is the ONLY thing the FAST exit branch reads.
    """

    def __init__(self, redis_client: object) -> None:
        self._r = redis_client

    async def publish(self, signal: InsiderDumpSignal) -> str:
        """XADD an InsiderDumpSignal to insider_dump.events.  Raises on Redis
        error (a silent dead bus is worse than a raised exception)."""
        import json

        ingest_time_ms = int(time.time() * 1_000)
        payload = {
            "mint": signal.mint,
            "wallet": signal.wallet,
            "role": signal.role.value,
            "fraction_sold_bps": str(signal.fraction_sold_bps),
            "event_slot": str(signal.event_slot),
            "block_time_ms": str(signal.block_time_ms),
            "cumulative_sold_base": str(signal.cumulative_sold_base),
            "held_supply_base": str(signal.held_supply_base),
            "ingest_time_ms": str(ingest_time_ms),
            "data_staleness_ms": str(max(0, ingest_time_ms - signal.block_time_ms)),
            "event": json.dumps(
                {
                    "mint": signal.mint,
                    "wallet": signal.wallet,
                    "role": signal.role.value,
                    "fraction_sold_bps": signal.fraction_sold_bps,
                    "event_slot": signal.event_slot,
                    "block_time_ms": signal.block_time_ms,
                    "cumulative_sold_base": signal.cumulative_sold_base,
                    "held_supply_base": signal.held_supply_base,
                }
            ),
        }
        entry_id: str = await self._r.xadd(  # type: ignore[attr-defined]
            STREAM_INSIDER_DUMP,
            payload,
            maxlen=MAXLEN_INSIDER_DUMP,
            approximate=True,
        )
        logger.debug(
            "insider_dump.publish mint=%s wallet=%s fraction_bps=%d entry=%s",
            signal.mint,
            signal.wallet,
            signal.fraction_sold_bps,
            entry_id,
        )
        return entry_id
