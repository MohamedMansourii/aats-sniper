"""M2-CP-07 — Creator/dev-wallet post-migration OUTFLOW-VELOCITY feature.

WHAT THIS IS (and how it differs from insider_dump.py)
--------------------------------------------------------
``aats.ingestion.insider_dump`` (E14a) already covers PRE-migration
bonding-curve SELLs and sets a boolean FAST-loop de-risk FLAG the instant a
cumulative-sold threshold is crossed.  This module is the MODEL-SIDE analogue
of Pepe Boost's dev-sell trigger: a continuous, point-in-time, MONOTONE
covariate — how FAST is the creator/dev wallet distributing its
POST-migration holdings — fed to the SLOW-loop survivor model
(``aats.models.survivor``) as ``creator_outflow_velocity_bps`` and (once
trained; M2-CP-03 is currently DATA-BLOCKED) to the chart-path/regime model,
via the SAME pure velocity function (``compute_outflow_velocity_bps``).  It
NEVER writes a StateStore flag and NEVER touches the FAST/snipe hot path —
it is a SLOW-loop model INPUT, not a FAST-loop trigger.

WHY POST-MIGRATION, AND WHY BALANCE-DELTA (not SELL-instruction decode)
--------------------------------------------------------------------------
Post-migration, PumpSwap/Raydium swaps are NOT yet separately discriminated
buy-vs-sell (``EventKind.SWAP`` conflates both — see ``insider_dump.py``'s
module docstring on this exact scope limit).  Rather than guess a swap
direction, this module reuses the PROVEN SPL Token Account balance-delta
pattern already live in ``aats.ingestion.smart_money``
(``GeyserAccountSubscriptionSource`` / ``_decode_spl_token_account``): a
DECREASE in the creator wallet's OWN token balance for ``mint``, observed
strictly after the recorded migration slot, is an OUTFLOW by construction —
regardless of whether it lands as a swap, a raw SPL transfer, or an LP
withdrawal.  This sidesteps the swap-direction ambiguity entirely and is
honest about doing so (no new decoder/offset is invented here).

MONOTONE NON-INCREASING BY CONSTRUCTION (creator selling => P(survive) DOWN)
-------------------------------------------------------------------------------
``compute_outflow_velocity_bps`` is provably NON-DECREASING in cumulative
outflow (holding the observation window fixed) — "the feature rises with
creator outflow" is a pure-arithmetic fact, tested directly with no model
required (``tests/ingestion/test_creator_outflow_velocity.py``).  The value
is wired into ``aats.models.survivor.SURVIVOR_COVARIATE_COLUMNS`` as
``creator_outflow_velocity_bps`` with a HARD LightGBM ``monotone_constraints``
entry of -1 (``SURVIVOR_MONOTONE_NONINCREASING``): a higher velocity can
ONLY push P(survive) DOWN, verified by a live sign test against a TRAINED
booster, including a mutation guard proving a sign-flipped pin would be
caught
(``tests/models/test_creator_outflow_survivor_covariate.py``) — mirroring
``tests/models/test_survivor.py::TestDeRiskSign``.  This module NEVER
computes a probability itself; it only computes the raw, bounded,
point-in-time feature value.  There is no code path in this module or in
the survivor wiring that can widen a stop, size up, or delay an exit.

EVENT-TIME ONLY (T-300a)
--------------------------
Every observation and every derived feature is stamped by on-chain SLOT
(``event_slot``) and on-chain ``block_time_ms``.  ``computed_wall_ms`` is
carried for MONITORING ONLY (staleness), exactly like ``EventTime.
wall_clock_ms`` — it is NEVER compared against a slot, never a join key,
never part of the velocity arithmetic.  An observation whose on-chain
``block_time_ms`` is not yet confirmed (``None``) is held PENDING — wall-clock
is NEVER substituted for it.

REFUSE-BY-DEFAULT (never fabricate "no outflow")
----------------------------------------------------
If the creator wallet's post-migration BASELINE (its first observed
post-migration balance) is unknown, degenerate (<=0), or the mint was never
registered from a decoded migration event, the tracker REFUSES to compute a
velocity — ``observe_balance``/``latest_feature`` return ``None``, and the
survivor covariate extractor (``aats.models.survivor.
build_survivor_covariate_row``) emits the NEUTRAL sentinel (0.0) alongside a
``creator_outflow_velocity_present=0.0`` flag, so a tree model can split on
"no coverage" instead of silently reading a fabricated "0% sold".  Every
refusal is counted (``CreatorOutflowVelocityStats``), never silent.

IDEMPOTENT / ORDER-TOLERANT (dedup on (mint, wallet, slot, write_version))
----------------------------------------------------------------------------------
Dedup is on ``(mint, creator_wallet, event_slot, write_version)`` — ``slot``
ALONE is NOT a safe event-identity key for account writes: two DISTINCT
writes to the creator's own token account can land in the SAME slot, and
Geyser's ``write_version`` is precisely the field that orders them (see
``aats.ingestion.smart_money.RawAccountUpdate.write_version``).  Per slot,
the tracker keeps only the HIGHEST-``write_version`` reading — the slot's
closing balance — deterministically, REGARDLESS of arrival order; a
lower-``write_version`` reading that arrives after a higher one has already
been recorded for that slot is a genuine (non-duplicate) observation that is
simply superseded, counted distinctly (``same_slot_superseded``), never
folded into ``duplicates_skipped``.  Rather than apply balance deltas
sequentially in ARRIVAL order (which would corrupt the running total on
out-of-order delivery), every NEW (not-yet-seen, not-superseded) observation
triggers a full recompute of the mint's cumulative outflow from the COMPLETE
set of (slot -> closing balance) pairs seen so far, sorted by slot.  The
final state after replaying a duplicated + out-of-order + same-slot-distinct
observation sequence is therefore IDENTICAL, byte-for-byte, to in-order
once-each delivery — proven directly in
``tests/ingestion/test_creator_outflow_velocity.py``.

CAUSAL-FEED CONTRACT (mirrors every other stateful M1 tracker)
-------------------------------------------------------------------
A caller building a TRAINING/backtest row for decision time T must feed this
tracker every observation with ``event_slot <= T`` and ONLY those, then read
``latest_feature``/the return of ``observe_balance`` — exactly like
``insider_dump.InsiderDumpDetector`` and ``smart_money.SmartWalletDecoder``.
Feeding a post-decision observation before reading the decision-time feature
is a caller-side leak, not something this module can detect after the fact;
the recompute-on-insert design guarantees ARRIVAL-order independence for a
FIXED input set, not omniscience about a set the caller has not fed yet.

OFF THE HOT PATH
-------------------
Enrichment-tier (mirrors ``insider_dump.py`` / ``enrichment.py``): pure,
allocation-light accounting off the FAST/snipe path.  No RPC/HTTP/LLM call
inside the tracker itself.  No signing, no submission.  The SLOW loop reads
the covariate at inference time exactly like every other survivor covariate
(``aats.controller.regime_wiring`` -> ``aats.models.survivor``).

MONEY RULE
------------
Token balances / cumulative outflow are integer base units.  The derived
``fraction_sold_bps`` / ``outflow_velocity_bps`` are bounded ratios
([0, 10000], bps) — statistical feature quantities, not money (data-models
§0) — kept as int (bps convention, exact).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from aats.contracts.events import LaunchEvent, LaunchSource

if TYPE_CHECKING:
    from aats.ingestion.smart_money import RawAccountUpdate

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Bus stream registry (mirrors insider_dump.py's STREAM_INSIDER_DUMP pattern)
# ---------------------------------------------------------------------------

STREAM_CREATOR_OUTFLOW = "creator_outflow.events"
MAXLEN_CREATOR_OUTFLOW = 5_000  # MAXLEN ~ so a slow consumer never OOMs the producer

BPS_DENOM = 10_000

# Normalizes the RATE component: bps-of-baseline sold PER this many slots.
# ~400ms/slot => 1000 slots ~= 6.7 minutes.  A creator dumping their FULL
# post-migration baseline within this window reads a maxed-out 10000 bps
# velocity.  FIXED, NEVER data-fit (mirrors regime_labels.py's fixed-threshold
# discipline) -- versioned informally via this module's docstring/tests.
VELOCITY_NORMALIZATION_SLOTS = 1_000


# ---------------------------------------------------------------------------
# Type guards (money rule; mirrors insider_dump.py's boundary checks)
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
# The pure, deterministic velocity math (no model, no I/O, no state)
# ---------------------------------------------------------------------------


def compute_outflow_velocity_bps(
    *,
    cumulative_outflow_base: int,
    baseline_post_migration_base: int,
    slots_since_migration: int,
) -> tuple[int, int]:
    """Pure velocity math -> ``(fraction_sold_bps, outflow_velocity_bps)``.

    ``fraction_sold_bps``  = min(10000, cumulative_outflow_base * 10000 // baseline)
    ``outflow_velocity_bps`` = min(10000, fraction_sold_bps * VELOCITY_NORMALIZATION_SLOTS
                                          // slots_since_migration)

    MONOTONE NON-DECREASING in ``cumulative_outflow_base`` holding the other
    two arguments fixed — "the feature rises with creator outflow" is a pure
    integer-arithmetic fact (verified directly in tests, no model needed).

    Refuses (raises ``ValueError``) on a non-positive baseline or non-positive
    ``slots_since_migration`` -- a caller/programming error, never silently
    divided by zero or by a negative denominator.
    """
    if baseline_post_migration_base <= 0:
        raise ValueError(
            "compute_outflow_velocity_bps: baseline_post_migration_base must be > 0, "
            f"got {baseline_post_migration_base}. A non-positive baseline can never be a "
            "meaningful denominator -- refuse-by-default, never divide by it."
        )
    if slots_since_migration <= 0:
        raise ValueError(
            "compute_outflow_velocity_bps: slots_since_migration must be > 0 (this feature "
            f"is POST-migration only), got {slots_since_migration}."
        )
    if cumulative_outflow_base < 0:
        raise ValueError(
            "compute_outflow_velocity_bps: cumulative_outflow_base must be >= 0, got "
            f"{cumulative_outflow_base}."
        )
    fraction_bps = min(
        BPS_DENOM, (cumulative_outflow_base * BPS_DENOM) // baseline_post_migration_base
    )
    velocity_bps = min(
        BPS_DENOM, (fraction_bps * VELOCITY_NORMALIZATION_SLOTS) // slots_since_migration
    )
    return fraction_bps, velocity_bps


# ---------------------------------------------------------------------------
# CreatorOutflowObservation -- transport-agnostic decoded balance observation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CreatorOutflowObservation:
    """One decoded creator-wallet token-balance reading, transport-agnostic.

    ``current_balance_base`` is the wallet's CURRENT token balance for
    ``mint`` as of ``event_slot`` (a materialized SPL Token Account read, NOT
    a transfer amount) -- the tracker derives outflow from the DECREASE
    between successive readings, mirroring
    ``aats.ingestion.smart_money.GeyserSmartWalletBackend``'s balance-delta
    technique.

    ``block_time_ms=None`` means "not yet confirmed on-chain" -- the
    observation is held PENDING by the tracker; wall-clock is NEVER
    substituted (T-300a).

    ``write_version`` is Geyser's per-slot write-ordering counter (mirrors
    ``aats.ingestion.smart_money.RawAccountUpdate.write_version``).  Two
    DISTINCT writes to the creator's token account CAN land in the same
    ``event_slot`` -- ``write_version`` is what lets the tracker pick the
    slot's true CLOSING balance deterministically, regardless of arrival
    order (dedup key includes it; see module docstring).
    """

    mint: str
    creator_wallet: str
    event_slot: int  # on-chain slot of this balance reading (AUTHORITATIVE)
    block_time_ms: int | None  # on-chain block time; None = unconfirmed (held pending)
    current_balance_base: int  # integer base units (NEVER float) -- money rule
    write_version: int = 0  # Geyser per-slot write-ordering counter (event-identity, not money)
    observation_wall_ms: int = field(default_factory=lambda: int(time.time() * 1_000))
    source: str = "accounts_subscribe"  # "accounts_subscribe" | "replay"

    def __post_init__(self) -> None:
        _check_str("CreatorOutflowObservation.mint", self.mint)
        _check_str("CreatorOutflowObservation.creator_wallet", self.creator_wallet)
        _check_int("CreatorOutflowObservation.event_slot", self.event_slot, min_value=0)
        if self.block_time_ms is not None:
            _check_int(
                "CreatorOutflowObservation.block_time_ms", self.block_time_ms, min_value=1
            )
        _check_int(
            "CreatorOutflowObservation.current_balance_base",
            self.current_balance_base,
            min_value=0,
        )
        _check_int(
            "CreatorOutflowObservation.write_version", self.write_version, min_value=0
        )
        _check_int(
            "CreatorOutflowObservation.observation_wall_ms",
            self.observation_wall_ms,
            min_value=0,
        )


def register_migration_from_launch_event(
    tracker: CreatorOutflowVelocityTracker, event: LaunchEvent
) -> bool:
    """Register the migration anchor for ``event.mint`` from a decoded
    MigrationEvent (a ``LaunchEvent`` with ``source=LaunchSource.MIGRATION`` --
    ``aats.ingestion.decoders.PumpFunDecoder._try_withdraw``, the bonding-curve
    completion signal).

    Returns ``True`` if registered, ``False`` if ``event`` is not a genuine
    migration (wrong source) -- honest about the scope limit, mirrors
    ``insider_dump.sell_observation_from_launch_event``'s kind-check pattern.
    Never raises on a non-migration event; it is simply not this module's
    concern.
    """
    if event.source is not LaunchSource.MIGRATION:
        return False
    tracker.register_migration(
        mint=event.mint,
        creator_wallet=event.creator_wallet,
        migration_slot=event.event_time.slot,
    )
    return True


def creator_outflow_observation_from_account_update(
    update: RawAccountUpdate,
    *,
    block_time_ms: int | None,
) -> CreatorOutflowObservation | None:
    """Adapt a ``RawAccountUpdate`` (a raw SPL Token Account write for a
    TRACKED creator wallet) into a ``CreatorOutflowObservation``, reusing the
    CANONICAL SPL Token Account decode already proven in
    ``aats.ingestion.smart_money._decode_spl_token_account`` -- no new decoder,
    no new byte offset.

    ``block_time_ms`` MUST be resolved on-chain (e.g. via
    ``aats.ingestion.smart_money.make_rpc_block_time_resolver``) -- this
    adapter NEVER substitutes wall-clock; pass ``None`` when unresolved (the
    tracker holds the observation PENDING, T-300a).

    Returns ``None`` for a startup snapshot (``update.is_startup`` -- the
    pre-existing balance, not a live write; the tracker's own first-
    observation seeding handles the baseline) or an account write that does
    not decode as an SPL Token Account (defensive; never raises on a
    malformed/unrelated write).
    """
    if update.is_startup:
        return None
    from aats.ingestion.smart_money import _decode_spl_token_account

    decoded = _decode_spl_token_account(update.data)
    if decoded is None:
        return None
    mint, owner, amount = decoded
    return CreatorOutflowObservation(
        mint=mint,
        creator_wallet=owner,
        event_slot=update.slot,
        block_time_ms=block_time_ms,
        current_balance_base=amount,
        write_version=update.write_version,
        source="accounts_subscribe",
    )


# ---------------------------------------------------------------------------
# CreatorOutflowVelocityFeature -- the typed, point-in-time model INPUT
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CreatorOutflowVelocityFeature:
    """The point-in-time creator-outflow-velocity feature snapshot.

    ``event_slot``/``block_time_ms`` are the on-chain anchors of the LATEST
    balance reading folded into this snapshot (T-300a; AUTHORITATIVE).
    ``computed_wall_ms`` is wall-clock at computation -- MONITORING ONLY,
    never part of the velocity arithmetic, never a join key.

    This type is M1-owned (like ``InsiderDumpSignal`` / ``SmartMoneyEvent``),
    NOT part of ``data-models.md`` -- it is consumed as a plain float
    (``outflow_velocity_bps``) by ``aats.models.survivor.
    build_survivor_covariate_row``, never as a rich object crossing the
    ingestion/model layer boundary.
    """

    mint: str
    creator_wallet: str
    event_slot: int  # on-chain slot of the latest folded-in balance reading
    block_time_ms: int  # on-chain block time of that same reading
    migration_slot: int  # the post-migration anchor (T-300a decision anchor)
    slots_since_migration: int  # = event_slot - migration_slot, > 0
    cumulative_outflow_base: int  # monotone non-decreasing running total, base units
    baseline_post_migration_base: int  # denominator: first observed post-migration balance
    fraction_sold_bps: int  # [0, 10000] cumulative_outflow / baseline
    outflow_velocity_bps: int  # [0, 10000] rate-normalized -- THE model covariate
    computed_wall_ms: int = field(default_factory=lambda: int(time.time() * 1_000))

    def __post_init__(self) -> None:
        _check_str("CreatorOutflowVelocityFeature.mint", self.mint)
        _check_str("CreatorOutflowVelocityFeature.creator_wallet", self.creator_wallet)
        _check_int("CreatorOutflowVelocityFeature.event_slot", self.event_slot, min_value=0)
        _check_int("CreatorOutflowVelocityFeature.block_time_ms", self.block_time_ms, min_value=1)
        _check_int("CreatorOutflowVelocityFeature.migration_slot", self.migration_slot, min_value=0)
        _check_int(
            "CreatorOutflowVelocityFeature.slots_since_migration",
            self.slots_since_migration,
            min_value=1,
        )
        _check_int(
            "CreatorOutflowVelocityFeature.cumulative_outflow_base",
            self.cumulative_outflow_base,
            min_value=0,
        )
        _check_int(
            "CreatorOutflowVelocityFeature.baseline_post_migration_base",
            self.baseline_post_migration_base,
            min_value=1,
        )
        frac = _check_int(
            "CreatorOutflowVelocityFeature.fraction_sold_bps", self.fraction_sold_bps, min_value=0
        )
        if frac > BPS_DENOM:
            raise ValueError(f"fraction_sold_bps must be <= 10000, got {frac}.")
        vel = _check_int(
            "CreatorOutflowVelocityFeature.outflow_velocity_bps",
            self.outflow_velocity_bps,
            min_value=0,
        )
        if vel > BPS_DENOM:
            raise ValueError(f"outflow_velocity_bps must be <= 10000, got {vel}.")
        if self.event_slot != self.migration_slot + self.slots_since_migration:
            raise ValueError(
                "CreatorOutflowVelocityFeature invariant violated: event_slot must equal "
                f"migration_slot + slots_since_migration (got event_slot={self.event_slot}, "
                f"migration_slot={self.migration_slot}, "
                f"slots_since_migration={self.slots_since_migration})."
            )


# ---------------------------------------------------------------------------
# Stats (observability -- proves "refuse" is never silent)
# ---------------------------------------------------------------------------


@dataclass
class CreatorOutflowVelocityStats:
    """Runtime counters.  The ``refused_*``/``*_skipped`` fields prove a
    refusal or scope-exclusion was counted distinctly, never silently folded
    into "zero outflow"."""

    observations_seen: int = 0
    pending_unconfirmed_skipped: int = 0
    refused_unregistered_mint: int = 0
    untracked_wallet_skipped: int = 0
    pre_migration_skipped: int = 0
    duplicates_skipped: int = 0
    same_slot_superseded: int = 0  # genuine reading, but a higher write_version already won the slot
    refused_degenerate_baseline: int = 0
    features_emitted: int = 0


# ---------------------------------------------------------------------------
# CreatorOutflowVelocityTracker -- the main class
# ---------------------------------------------------------------------------


class CreatorOutflowVelocityTracker:
    """Stateful, deterministic, order-tolerant creator post-migration outflow
    velocity tracker.  One instance covers many (mint, creator wallet) pairs.

    USAGE (off the hot path -- the SLOW-loop / enrichment runner drives this):

        tracker = CreatorOutflowVelocityTracker()
        register_migration_from_launch_event(tracker, migration_event)  # from decoders.py
        # per decoded balance reading (accounts_subscribe or replay):
        obs = creator_outflow_observation_from_account_update(raw_update, block_time_ms=bt)
        if obs is not None:
            feature = tracker.observe_balance(obs)
            if feature is not None:
                await bus_publisher.publish(feature)  # audit trail

        # SLOW-loop inference time (point-in-time read, no new observation):
        feature = tracker.latest_feature(mint)
        velocity_bps = feature.outflow_velocity_bps if feature is not None else None
        row = build_survivor_covariate_row(frame, mcs, velocity_bps)

    De-risk-only: this tracker NEVER writes a StateStore flag and NEVER
    touches the FAST/snipe path -- it only ever produces a bounded, monotone
    covariate for the SLOW-loop survivor/regime models.  Idempotent /
    order-tolerant: dedup on (mint, creator_wallet, event_slot, write_version);
    a per-slot arbitration keeps only the highest-write_version (closing)
    balance per slot, deterministically regardless of arrival order.
    Replaying a duplicated, out-of-order, or same-slot-distinct-write
    observation set converges to the IDENTICAL final state as in-order,
    once-each delivery (recompute-from-scratch on every new slot -- see
    module docstring).
    """

    def __init__(self, seen_capacity: int = 50_000) -> None:
        self._migration_slot: dict[str, int] = {}
        self._creator_wallet: dict[str, str] = {}
        self._balances_by_slot: dict[str, dict[int, int]] = {}
        self._write_version_by_slot: dict[str, dict[int, int]] = {}
        self._block_time_by_slot: dict[str, dict[int, int]] = {}
        self._baseline_base: dict[str, int] = {}
        self._cumulative_outflow_base: dict[str, int] = {}
        self._latest: dict[str, CreatorOutflowVelocityFeature] = {}
        self._degenerate_mints: set[str] = set()
        self._seen: set[tuple[str, str, int, int]] = set()
        self._seen_capacity = seen_capacity
        self.stats = CreatorOutflowVelocityStats()

    # -- registration --------------------------------------------------- #

    def register_migration(self, mint: str, creator_wallet: str, migration_slot: int) -> None:
        """Record the migration anchor for ``mint`` (from a decoded
        MigrationEvent).  MUST be called before any observation for this mint
        is processed -- an observation for an unregistered mint is REFUSED
        (never assumed pre- or post-migration)."""
        _check_str("register_migration.mint", mint)
        _check_str("register_migration.creator_wallet", creator_wallet)
        _check_int("register_migration.migration_slot", migration_slot, min_value=0)
        self._migration_slot[mint] = migration_slot
        self._creator_wallet[mint] = creator_wallet

    def reset_mint(self, mint: str) -> None:
        """Clear all tracked state for ``mint`` (call on position CLOSE/re-entry
        so a stale cumulative from a prior trade never leaks into a new one)."""
        self._migration_slot.pop(mint, None)
        self._creator_wallet.pop(mint, None)
        self._balances_by_slot.pop(mint, None)
        self._write_version_by_slot.pop(mint, None)
        self._block_time_by_slot.pop(mint, None)
        self._baseline_base.pop(mint, None)
        self._cumulative_outflow_base.pop(mint, None)
        self._latest.pop(mint, None)
        self._degenerate_mints.discard(mint)

    # -- internal: order-tolerant recompute ------------------------------ #

    def _recompute_mint(self, mint: str) -> None:
        """Recompute ``mint``'s baseline + cumulative outflow from the COMPLETE
        set of (slot -> balance) readings seen so far, sorted by slot.  This is
        what makes the tracker provably order-tolerant: the result depends only
        on the SET of readings, never on the order they arrived in."""
        balances = self._balances_by_slot.get(mint)
        if not balances:
            return
        ordered_slots = sorted(balances)
        baseline = balances[ordered_slots[0]]
        if baseline <= 0:
            # Degenerate baseline (creator already held 0 at first post-migration
            # reading) -- REFUSE, never divide by it.  Un-register any prior
            # (now-stale) baseline/cumulative/FEATURE so a caller can't read a
            # fabricated number -- or a stale superseded snapshot -- left over
            # from a smaller observation set.  A late-arriving EARLIER 0-balance
            # reading (e.g. the ATA-creation write) must invalidate an
            # already-published `latest_feature`, not leave it serving forever.
            self._baseline_base.pop(mint, None)
            self._cumulative_outflow_base.pop(mint, None)
            self._latest.pop(mint, None)
            # Count the refusal once per transition into the degenerate state,
            # never re-inflated on every subsequent recompute while it remains
            # degenerate (each new/duplicate-free observation re-triggers this
            # branch as long as the earliest slot's balance stays <= 0).
            if mint not in self._degenerate_mints:
                self._degenerate_mints.add(mint)
                self.stats.refused_degenerate_baseline += 1
            return
        self._degenerate_mints.discard(mint)
        cumulative = 0
        prev_balance = baseline
        for slot in ordered_slots[1:]:
            bal = balances[slot]
            if bal < prev_balance:
                cumulative += prev_balance - bal
            prev_balance = bal
        self._baseline_base[mint] = baseline
        self._cumulative_outflow_base[mint] = cumulative

    # -- the enrichment-tier hot call (off the FAST/snipe path) ---------- #

    def observe_balance(
        self, obs: CreatorOutflowObservation
    ) -> CreatorOutflowVelocityFeature | None:
        """Process one decoded balance reading.  Returns the CURRENT
        point-in-time feature snapshot (folding in this + all prior readings
        for the mint), or ``None`` when the reading is held pending, refused,
        out of scope, or a duplicate."""
        self.stats.observations_seen += 1

        if obs.block_time_ms is None:
            self.stats.pending_unconfirmed_skipped += 1
            logger.debug(
                "CreatorOutflowVelocityTracker: mint=%s slot=%d block_time unconfirmed -- "
                "held PENDING (no wall-clock substitution, T-300a).",
                obs.mint,
                obs.event_slot,
            )
            return None

        mint = obs.mint
        if mint not in self._migration_slot:
            self.stats.refused_unregistered_mint += 1
            logger.debug(
                "CreatorOutflowVelocityTracker: REFUSING mint=%s -- no migration anchor "
                "registered yet (call register_migration first).",
                mint,
            )
            return None

        if obs.creator_wallet != self._creator_wallet[mint]:
            self.stats.untracked_wallet_skipped += 1
            return None  # a non-creator balance write is a normal no-op here

        migration_slot = self._migration_slot[mint]
        if obs.event_slot <= migration_slot:
            self.stats.pre_migration_skipped += 1
            return None  # out of scope: this feature is POST-migration only

        dedup_key = (mint, obs.creator_wallet, obs.event_slot, obs.write_version)
        if dedup_key in self._seen:
            self.stats.duplicates_skipped += 1
            logger.debug(
                "CreatorOutflowVelocityTracker: dedup skip mint=%s slot=%d write_version=%d",
                mint,
                obs.event_slot,
                obs.write_version,
            )
            return None
        self._seen.add(dedup_key)
        if len(self._seen) > self._seen_capacity:
            self._seen.discard(next(iter(self._seen)))  # approximate eviction

        # Same-slot arbitration: two DISTINCT writes to the creator's token
        # account can land in the SAME slot on Solana (write_version orders
        # them).  Keep only the highest-write_version reading per slot -- the
        # slot's closing balance -- deterministically, regardless of arrival
        # order.  A lower-write_version reading arriving after a higher one
        # has already been recorded is a genuine (non-duplicate) observation
        # that is simply superseded -- count it distinctly, never as a
        # duplicate, and never let it corrupt the slot's closing balance.
        existing_write_version = self._write_version_by_slot.get(mint, {}).get(obs.event_slot)
        if existing_write_version is not None and obs.write_version < existing_write_version:
            self.stats.same_slot_superseded += 1
            logger.debug(
                "CreatorOutflowVelocityTracker: same-slot superseded mint=%s slot=%d "
                "write_version=%d < existing_write_version=%d",
                mint,
                obs.event_slot,
                obs.write_version,
                existing_write_version,
            )
            return None

        self._balances_by_slot.setdefault(mint, {})[obs.event_slot] = obs.current_balance_base
        self._write_version_by_slot.setdefault(mint, {})[obs.event_slot] = obs.write_version
        self._block_time_by_slot.setdefault(mint, {})[obs.event_slot] = obs.block_time_ms
        self._recompute_mint(mint)

        baseline = self._baseline_base.get(mint)
        cumulative = self._cumulative_outflow_base.get(mint)
        if baseline is None or cumulative is None:
            return None  # degenerate baseline -- refusal already counted above

        latest_slot = max(self._balances_by_slot[mint])
        slots_since_migration = latest_slot - migration_slot
        latest_block_time_ms = self._block_time_by_slot[mint][latest_slot]

        fraction_bps, velocity_bps = compute_outflow_velocity_bps(
            cumulative_outflow_base=cumulative,
            baseline_post_migration_base=baseline,
            slots_since_migration=slots_since_migration,
        )

        feature = CreatorOutflowVelocityFeature(
            mint=mint,
            creator_wallet=obs.creator_wallet,
            event_slot=latest_slot,
            block_time_ms=latest_block_time_ms,
            migration_slot=migration_slot,
            slots_since_migration=slots_since_migration,
            cumulative_outflow_base=cumulative,
            baseline_post_migration_base=baseline,
            fraction_sold_bps=fraction_bps,
            outflow_velocity_bps=velocity_bps,
        )
        self._latest[mint] = feature
        self.stats.features_emitted += 1
        if velocity_bps > 0:
            logger.info(
                "CreatorOutflowVelocityTracker: mint=%s creator=%s fraction_sold_bps=%d "
                "outflow_velocity_bps=%d slots_since_migration=%d",
                mint,
                obs.creator_wallet,
                fraction_bps,
                velocity_bps,
                slots_since_migration,
            )
        return feature

    def latest_feature(self, mint: str) -> CreatorOutflowVelocityFeature | None:
        """Point-in-time read WITHOUT a new observation -- the survivor/regime
        SLOW-loop inference call.  Returns ``None`` (REFUSE) if no feature has
        been computed yet for this mint (never fabricated as "no outflow")."""
        return self._latest.get(mint)


# ---------------------------------------------------------------------------
# CreatorOutflowBusPublisher -- the "/ bus" half (audit trail, not the hot path)
# ---------------------------------------------------------------------------


class CreatorOutflowBusPublisher:
    """Publishes CreatorOutflowVelocityFeature snapshots to the Redis Streams
    bus (creator_outflow.events).  XADD and return -- never blocks on
    downstream processing.  This is the audit trail; the SLOW loop reads the
    covariate directly from the tracker (``latest_feature``), not this stream.
    """

    def __init__(self, redis_client: object) -> None:
        self._r = redis_client

    async def publish(self, feature: CreatorOutflowVelocityFeature) -> str:
        """XADD a CreatorOutflowVelocityFeature to creator_outflow.events.
        Raises on Redis error (a silent dead bus is worse than a raised
        exception)."""
        import json

        ingest_time_ms = int(time.time() * 1_000)
        payload = {
            "mint": feature.mint,
            "creator_wallet": feature.creator_wallet,
            "event_slot": str(feature.event_slot),
            "block_time_ms": str(feature.block_time_ms),
            "migration_slot": str(feature.migration_slot),
            "slots_since_migration": str(feature.slots_since_migration),
            "cumulative_outflow_base": str(feature.cumulative_outflow_base),
            "baseline_post_migration_base": str(feature.baseline_post_migration_base),
            "fraction_sold_bps": str(feature.fraction_sold_bps),
            "outflow_velocity_bps": str(feature.outflow_velocity_bps),
            "ingest_time_ms": str(ingest_time_ms),
            "data_staleness_ms": str(max(0, ingest_time_ms - feature.block_time_ms)),
            "event": json.dumps(
                {
                    "mint": feature.mint,
                    "creator_wallet": feature.creator_wallet,
                    "event_slot": feature.event_slot,
                    "block_time_ms": feature.block_time_ms,
                    "migration_slot": feature.migration_slot,
                    "slots_since_migration": feature.slots_since_migration,
                    "cumulative_outflow_base": feature.cumulative_outflow_base,
                    "baseline_post_migration_base": feature.baseline_post_migration_base,
                    "fraction_sold_bps": feature.fraction_sold_bps,
                    "outflow_velocity_bps": feature.outflow_velocity_bps,
                }
            ),
        }
        entry_id: str = await self._r.xadd(  # type: ignore[attr-defined]
            STREAM_CREATOR_OUTFLOW,
            payload,
            maxlen=MAXLEN_CREATOR_OUTFLOW,
            approximate=True,
        )
        logger.debug(
            "creator_outflow.publish mint=%s velocity_bps=%d entry=%s",
            feature.mint,
            feature.outflow_velocity_bps,
            entry_id,
        )
        return entry_id
