"""First-K-slot buy pressure and volume features (T-305).

These are the two C-4 GATE-B BASELINE ENABLERS:
  - first_k_buy_pressure  : Decimal  — net buy SOL pressure over first K slots
                            (sum of buy lamports - sum of sell lamports)
  - first_k_volume_lamports : int    — total traded volume over first K slots
                              (buy lamports + sell lamports, integer)

POINT-IN-TIME CORRECTNESS — the prime directive.
  Every datum consumed here MUST have slot <= event_time.slot + first_k_slots (the
  cutoff), AND slot >= event_time.slot (no pre-creation events).
  The function raises FeatureWindowError if any event violates either bound.
  This is NOT a polite warning — it is a hard build/compute failure identical to
  the ProvenanceTaintError contract guard.  The naive-momentum baseline is only
  trustworthy if the features it reads are causally clean.

DIRECTION CLASSIFICATION — why the caller must supply it (B-1 fix).
  The real pump.fun decoder (decoders.py) emits sol_reserve_lamports=0 for create
  events.  That means the old reserve-comparison heuristic
    (event.sol_reserve_lamports >= create.sol_reserve_lamports)
  degenerates to (positive_amount >= 0) = True for EVERY subsequent event —
  buys AND sells both classify as BUY.  This is a structural correctness bug:
  first_k_sell_count would always be 0 and first_k_buy_pressure would equal
  gross volume, destroying the C-4 baseline.

  The only correct fix is to carry the decoded instruction discriminator through
  to the feature layer.  Pending a full data-contract delta (post-G1 ADR), the
  self-contained solution is: the CALLER supplies pre-classified (event, is_buy)
  pairs to build_buy_pressure_features.  This is the same API the online
  accumulator (BuyPressureAccumulator.observe) already uses — direction is always
  known at decode time (PUMP_DISC_BUY vs PUMP_DISC_SELL, PumpSwap buy vs sell
  discriminators, etc.).

  classify_event_direction is RETAINED but is now EXPLICITLY UNSAFE: it raises
  ClassificationUndefinedError when the creation anchor reserve is 0 (real decoder
  output), making the defect loud and testable instead of silent.  It may only be
  used in controlled tests with synthetic non-zero creation reserves, or in the
  enrichment layer (T-301) once real cumulative reserves are available.

LOOP PLACEMENT:
  These features feed the SNIPE loop (microstructure, first-K-slot window) and the
  SLOW loop (as inputs to the classifier and the frozen GATE-B naive-momentum
  baseline).  They are pure scalar accumulators — no pandas, no rolling windows
  over unseen data, no global normalization.  The incremental path (online
  accumulator) and the batch path (list of pre-classified pairs replayed) produce
  BIT-IDENTICAL outputs.

MONEY RULES (data-models.md §0, non-negotiable):
  - sol amounts → int (lamports).  NEVER float.
  - first_k_buy_pressure → Decimal (net quantity, exact).  NEVER float.
  - first_k_volume_lamports → int (lamports, non-negative).
  Decimal arithmetic is used for the accumulator to preserve exact cancellation
  when buys and sells are close in magnitude.

PROVENANCE MANIFEST (ADR-0010, data-models.md §3.3):
  The function emits a list[FeatureSourceWindow], one per feature, declaring the
  HIGHEST slot observed across all contributing events.  The build guard
  (check_feature_window_cutoff) is called inside build_buy_pressure_features()
  and will raise ProvenanceTaintError if the declared max_source_slot exceeds
  the cutoff.  There is no code path through which a future-data event can
  silently contribute to the output — either it is rejected up front
  (FeatureWindowError) or it triggers the provenance guard.

INCREMENTAL ACCUMULATOR:
  BuyPressureAccumulator supports .observe(event, is_buy) for the online (SNIPE-loop)
  path.  .to_features() materialises the same values as the batch path given the same
  event sequence.  The batch helper build_buy_pressure_features() calls the accumulator
  internally — batch and streaming share one code path.

NO I/O: this module is a pure function module.  No sockets, no RPC, no Redis.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal

from aats.contracts.events import EventTime, LaunchEvent
from aats.contracts.features import FeatureProvenance, FeatureSourceWindow
from aats.contracts.provenance import check_feature_window_cutoff

# ---------------------------------------------------------------------------
# Feature schema version — bump when the algorithm changes (C-4)
# ---------------------------------------------------------------------------

FEATURE_SCHEMA_VERSION = "1.0.0"

# Lineage dataset for all first-K-slot features computed from LaunchEvents.
# Declared as Literal so mypy can verify it satisfies FeatureSourceWindow.lineage_dataset.
_LINEAGE_DATASET: Literal["launch_events"] = "launch_events"

# ---------------------------------------------------------------------------
# Custom exceptions for point-in-time violations at compute time
# (distinct from ProvenanceTaintError which fires at the build/guard layer)
# ---------------------------------------------------------------------------


class FeatureWindowError(ValueError):
    """Raised when an event with slot outside [event_time_slot, cutoff_slot] is observed.

    This covers both lookahead (slot > cutoff) and pre-creation (slot < creation)
    violations.  Both are HARD FAILURES, not silent filters.

    For lookahead: the caller must never offer future-slot data to a point-in-time
    feature computation.  Swallowing future events silently would be a lookahead
    leak — exactly the bug we are designed to prevent.

    For pre-creation: an event with slot < event_time_slot pre-dates pool creation
    and has no business contributing to first-K-slot features.  A stray earlier-slot
    event (mis-route, reorg, replay) would silently lower max_source_slot below the
    creation slot, producing a provenance manifest that UNDERSTATES the window —
    which is also incorrect.

    Raising here makes the violation loud and testable.
    """

    def __init__(
        self,
        event_slot: int,
        cutoff_slot: int,
        mint: str,
        *,
        creation_slot: int | None = None,
    ) -> None:
        self.event_slot = event_slot
        self.cutoff_slot = cutoff_slot
        self.mint = mint
        self.creation_slot = creation_slot

        if creation_slot is not None and event_slot < creation_slot:
            # Pre-creation lower-bound violation (M-1 fix)
            msg = (
                f"POINT-IN-TIME VIOLATION: event slot {event_slot} < creation slot "
                f"{creation_slot} for mint={mint!r}.  Pre-creation events must not "
                "contribute to first-K-slot features.  This is a lower-bound causality "
                "BLOCKER — fix the caller to filter events to slot >= creation_slot."
            )
        else:
            # Lookahead upper-bound violation
            msg = (
                f"POINT-IN-TIME VIOLATION: event slot {event_slot} > cutoff slot "
                f"{cutoff_slot} for mint={mint!r}.  Future data was offered to a "
                "first-K-slot accumulator.  This is a lookahead BLOCKER — fix the "
                "caller to filter by slot <= cutoff before calling observe() or "
                "build_buy_pressure_features()."
            )
        super().__init__(msg)


class ClassificationUndefinedError(ValueError):
    """Raised when classify_event_direction cannot produce a reliable result.

    This error fires when pool_creation_event.sol_reserve_lamports == 0, which is
    the value emitted by the real pump.fun decoder (decoders.py:324).  With a zero
    anchor, the comparison (event.sol_reserve >= 0) is True for every event —
    buys AND sells both classify as BUY.  The error makes this defect LOUD rather
    than silently corrupting the GATE-B baseline.

    FIX: supply pre-classified (event, is_buy: bool) pairs to
    build_buy_pressure_features, deriving direction from the instruction
    discriminator (PUMP_DISC_BUY vs PUMP_DISC_SELL) at decode time.
    """

    def __init__(self, creation_sol_reserve: int, mint: str) -> None:
        self.creation_sol_reserve = creation_sol_reserve
        self.mint = mint
        super().__init__(
            f"classify_event_direction: pool_creation_event.sol_reserve_lamports == 0 "
            f"for mint={mint!r}.  The real pump.fun decoder emits reserve=0 on create "
            "events; comparing (trade_amount >= 0) classifies EVERY event as BUY, which "
            "is incorrect.  Use the instruction discriminator (PUMP_DISC_BUY vs "
            "PUMP_DISC_SELL) at decode time and supply pre-classified (event, is_buy: bool) "
            "pairs to build_buy_pressure_features instead of relying on this function.  "
            "See aats/features/buy_pressure.py module docstring §DIRECTION CLASSIFICATION."
        )


# ---------------------------------------------------------------------------
# Typed result of the feature computation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BuyPressureFeatures:
    """Typed output of the first-K-slot buy-pressure feature computation.

    All money fields are exact (Decimal or int, never float).
    Provenance manifest is REQUIRED by ADR-0010 and validated at creation.

    Fields
    ------
    first_k_buy_pressure : Decimal
        Net buy SOL pressure over the first K slots.
        = sum(sol_lamports for buy events) - sum(sol_lamports for sell events)
        Positive = net buying pressure; negative = net selling pressure.
        Units: lamports (Decimal for exact arithmetic).
        NEVER float (data-models.md §0).

    first_k_volume_lamports : int
        Total traded volume over the first K slots.
        = sum(sol_lamports for all events regardless of direction)
        Units: lamports, non-negative integer.
        NEVER float.

    first_k_buy_count : int
        Number of buy events observed in the window.

    first_k_sell_count : int
        Number of sell events observed in the window.

    first_k_unique_buyers : int
        Count of distinct buyer wallet addresses in the window.
        (Excludes sell-only wallets.)

    max_source_slot : int
        The highest slot of any event that contributed to this computation.
        Stored here so the provenance manifest can be assembled by the caller
        without a second pass through the events.

    event_time : EventTime
        The decision anchor (copied from the triggering LaunchEvent, C-5).
        ALL joins must use this field, never wall_clock_ms.

    first_k_slots : int
        K — the window size in slots.

    provenance : FeatureProvenance
        Per-feature source windows (ADR-0010, data-models.md §3.3).
        The build guard has ALREADY been run on these windows by the time
        this object is constructed — a BuyPressureFeatures instance with
        a future-slot max_source_slot cannot exist.
    """

    first_k_buy_pressure: Decimal
    first_k_volume_lamports: int
    first_k_buy_count: int
    first_k_sell_count: int
    first_k_unique_buyers: int
    max_source_slot: int
    event_time: EventTime
    first_k_slots: int
    provenance: FeatureProvenance


# ---------------------------------------------------------------------------
# Incremental online accumulator (SNIPE loop path)
# ---------------------------------------------------------------------------


@dataclass
class BuyPressureAccumulator:
    """Incremental, event-by-event accumulator for first-K-slot buy pressure.

    Designed for the SNIPE loop: one event arrives at a time, the accumulator
    is updated in O(1), and .to_features() materialises the result on demand.

    The batch helper build_buy_pressure_features() uses this internally so
    that the online and offline code paths are IDENTICAL — bit-exact outputs
    are guaranteed because there is only one implementation.

    Point-in-time guarantee:
        observe() RAISES FeatureWindowError on any event with
        slot > event_time_slot + first_k_slots  (lookahead upper bound)
        OR slot < event_time_slot               (pre-creation lower bound).
        There is no silent discard.

    Money discipline:
        _net_buy_lamports accumulates in Decimal to prevent floating-point
        imprecision on large lamport sums.  Volume is accumulated in int.
        These are then emitted as Decimal and int respectively.
    """

    mint: str
    event_time_slot: int          # the canonical decision slot (C-5)
    first_k_slots: int            # K — the window size in slots

    # Running totals (Decimal arithmetic for exact lamport sums)
    _net_buy_lamports: Decimal = field(default_factory=lambda: Decimal(0))
    _volume_lamports: int = field(default=0)
    _buy_count: int = field(default=0)
    _sell_count: int = field(default=0)
    _buyer_wallets: set[str] = field(default_factory=set)
    _max_source_slot: int = field(default=0)
    _event_count: int = field(default=0)

    @property
    def _cutoff_slot(self) -> int:
        """The inclusive upper bound on source slots (no future data beyond this)."""
        return self.event_time_slot + self.first_k_slots

    def observe(self, event: LaunchEvent, is_buy: bool) -> None:
        """Accumulate one event into the running totals.

        Parameters
        ----------
        event : LaunchEvent
            The raw event from the ingestion pipeline.  Must have
            event.mint == self.mint (not enforced — caller must route correctly).
        is_buy : bool
            True if this event is a buy (sol flows INTO the pool → buying pressure).
            False if this event is a sell (sol flows OUT of pool → selling pressure).
            The CALLER must supply this from the decoded instruction discriminator
            (PUMP_DISC_BUY / PUMP_DISC_SELL or the PumpSwap/Raydium equivalent).
            Do NOT derive this from sol_reserve_lamports when the creation reserve
            is 0 — see ClassificationUndefinedError and module docstring §DIRECTION.

        Raises
        ------
        FeatureWindowError
            If event.event_time.slot > self._cutoff_slot (lookahead guard).
            If event.event_time.slot < self.event_time_slot (pre-creation guard, M-1).
            A future or pre-creation slot MUST NOT silently contribute to the accumulator.
        """
        event_slot = event.event_time.slot

        # LOWER-BOUND GUARD (M-1 fix): reject pre-creation events.
        # A slot before pool creation cannot logically contribute to first-K-slot
        # features.  A stray earlier-slot event (mis-route, reorg, replay) would
        # lower max_source_slot below the creation slot and corrupt the provenance
        # manifest.  This is a hard failure, not a silent filter.
        if event_slot < self.event_time_slot:
            raise FeatureWindowError(
                event_slot=event_slot,
                cutoff_slot=self._cutoff_slot,
                mint=self.mint,
                creation_slot=self.event_time_slot,
            )

        # UPPER-BOUND GUARD: any future slot raises immediately.
        # This is the primary runtime enforcement of the causality contract.
        if event_slot > self._cutoff_slot:
            raise FeatureWindowError(
                event_slot=event_slot,
                cutoff_slot=self._cutoff_slot,
                mint=self.mint,
            )

        sol = event.sol_reserve_lamports  # int lamports (data-models.md §0)

        # Accumulate in Decimal to avoid float imprecision on large lamport sums.
        # Exact arithmetic is critical: a buy and a sell of equal size must net to 0.
        sol_decimal = Decimal(sol)

        if is_buy:
            self._net_buy_lamports += sol_decimal
            self._volume_lamports += sol
            self._buy_count += 1
            self._buyer_wallets.add(event.creator_wallet)
        else:
            # Sell: subtract from net pressure, add to volume
            self._net_buy_lamports -= sol_decimal
            self._volume_lamports += sol
            self._sell_count += 1

        # Track the highest slot we have consumed
        if event_slot > self._max_source_slot:
            self._max_source_slot = event_slot

        self._event_count += 1

    def to_features(self, event_time: EventTime) -> BuyPressureFeatures:
        """Materialise the accumulated totals into a typed BuyPressureFeatures.

        Parameters
        ----------
        event_time : EventTime
            The canonical decision anchor for this computation (C-5).
            This is the event_time of the pool-creation LaunchEvent, not the
            wall-clock at which to_features() is called.  It is the ONLY time
            that may be used for downstream joins.

        Returns
        -------
        BuyPressureFeatures
            Typed result with provenance manifest.  The per-feature cutoff guard
            (check_feature_window_cutoff) is run here — if somehow a future-slot
            event slipped through observe() (which is impossible given the guard
            above), this raises ProvenanceTaintError as the second defence line.
        """
        # Use 0 as max_source_slot if no events were observed (empty window).
        # An empty window is a valid state: a token with zero trades in K slots
        # yields zero pressure and zero volume — both correct, not a fabrication.
        effective_max_slot = self._max_source_slot if self._event_count > 0 else self.event_time_slot

        # Build per-feature provenance windows.
        # All three C-4 features share the same max_source_slot and lineage.
        def _window(name: str) -> FeatureSourceWindow:
            return FeatureSourceWindow(
                feature_name=name,
                max_source_slot=effective_max_slot,
                lineage_dataset=_LINEAGE_DATASET,
            )

        windows = [
            _window("first_k_buy_pressure"),
            _window("first_k_volume_lamports"),
            _window("first_k_buy_count"),
            _window("first_k_sell_count"),
            _window("first_k_unique_buyers"),
        ]

        # Run the build guard (data-models.md §3.3, guard 1) on every window.
        # This is the second line of defence after the observe() guard above.
        # If max_source_slot > cutoff, ProvenanceTaintError is raised here.
        for window in windows:
            check_feature_window_cutoff(
                feature_name=window.feature_name,
                max_source_slot=window.max_source_slot,
                event_time_slot=event_time.slot,
                first_k_slots=self.first_k_slots,
            )

        provenance = FeatureProvenance(
            windows=windows,
            builder_version=FEATURE_SCHEMA_VERSION,
        )

        return BuyPressureFeatures(
            first_k_buy_pressure=self._net_buy_lamports,
            first_k_volume_lamports=self._volume_lamports,
            first_k_buy_count=self._buy_count,
            first_k_sell_count=self._sell_count,
            first_k_unique_buyers=len(self._buyer_wallets),
            max_source_slot=effective_max_slot,
            event_time=event_time,
            first_k_slots=self.first_k_slots,
            provenance=provenance,
        )

    def reset(self) -> None:
        """Reset all accumulators (e.g., on session start or per-mint reuse)."""
        self._net_buy_lamports = Decimal(0)
        self._volume_lamports = 0
        self._buy_count = 0
        self._sell_count = 0
        self._buyer_wallets = set()
        self._max_source_slot = 0
        self._event_count = 0


# ---------------------------------------------------------------------------
# Batch helper — uses the accumulator internally (one code path, not two)
# ---------------------------------------------------------------------------


def classify_event_direction(event: LaunchEvent, pool_creation_event: LaunchEvent) -> bool:
    """Classify a subsequent event as a buy or sell relative to the pool creation.

    WARNING — LIMITED APPLICABILITY (B-1 fix).
    This function is ONLY valid when pool_creation_event.sol_reserve_lamports > 0,
    i.e., when the creation event carries a real cumulative pool reserve.

    The real pump.fun decoder (decoders.py) emits sol_reserve_lamports=0 for create
    events.  In that case, EVERY subsequent event satisfies
        event.sol_reserve_lamports >= 0
    and is classified as BUY, regardless of the actual direction.  To prevent
    silent corruption of the GATE-B baseline, this function raises
    ClassificationUndefinedError when called with a zero-reserve anchor.

    Use build_buy_pressure_features with pre-classified (event, is_buy) pairs instead.
    Pre-classification is done at decode time using the instruction discriminator
    (PUMP_DISC_BUY vs PUMP_DISC_SELL, PumpSwap buy vs sell discriminators, etc.),
    which is the only reliable source of direction on real decoder output.

    This function remains available for:
    - Controlled unit tests with synthetic non-zero creation reserves.
    - The enrichment layer (T-301) once real cumulative reserves are available.
    - Documentation of what the heuristic WOULD do with reliable reserves.

    Heuristic (valid only when creation_reserve > 0):
    - Event is a BUY if sol_reserve_lamports >= pool_creation_event.sol_reserve_lamports
      (reserve grew or held — SOL flowed INTO the pool).
    - Event is a SELL if sol_reserve_lamports < pool_creation_event.sol_reserve_lamports
      (reserve shrank — SOL flowed OUT of the pool).

    Raises
    ------
    ClassificationUndefinedError
        If pool_creation_event.sol_reserve_lamports == 0.  This is the real pump.fun
        decoder output for create events; the comparison becomes degenerate.
    """
    if pool_creation_event.sol_reserve_lamports == 0:
        raise ClassificationUndefinedError(
            creation_sol_reserve=pool_creation_event.sol_reserve_lamports,
            mint=pool_creation_event.mint,
        )
    # Reserve-delta heuristic: valid only when creation reserve > 0 (synthetic/enriched data).
    # A BUY increases the reserve; a SELL decreases it.
    return event.sol_reserve_lamports >= pool_creation_event.sol_reserve_lamports


def build_buy_pressure_features(
    pool_creation_event: LaunchEvent,
    classified_events: Sequence[tuple[LaunchEvent, bool]],
    first_k_slots: int,
) -> BuyPressureFeatures:
    """Compute first-K-slot buy pressure and volume from pre-classified event pairs.

    This is the BATCH PATH that is bit-identical to the online (accumulator) path.
    It uses BuyPressureAccumulator internally — there is only one accumulator
    implementation, so batch and streaming outputs are GUARANTEED to be identical
    given the same event sequence.

    WHY PRE-CLASSIFIED PAIRS (B-1 fix):
        The old signature accepted Sequence[LaunchEvent] and called
        classify_event_direction internally.  That classifier compared
        event.sol_reserve_lamports >= pool_creation_event.sol_reserve_lamports.
        Since the real pump.fun decoder emits sol_reserve_lamports=0 for create
        events, every buy AND sell produced (positive_amount >= 0) = True → BUY.
        first_k_sell_count was always 0; first_k_buy_pressure always equalled
        gross volume.  The C-4 GATE-B baseline was not constructible.

        The fix: direction is known at decode time from the instruction discriminator
        and must be passed in by the caller.  The feature layer does not re-derive
        what the decoder already knows.

    POINT-IN-TIME GUARANTEE:
        Any event with slot > pool_creation_event.event_time.slot + first_k_slots
        raises FeatureWindowError IMMEDIATELY (lookahead guard).
        Any event with slot < pool_creation_event.event_time.slot raises
        FeatureWindowError IMMEDIATELY (pre-creation guard, M-1 fix).
        There is no silent filtering — the caller must supply only events within
        [creation_slot, creation_slot + K].

    MONEY DISCIPLINE:
        - sol_reserve_lamports: int (from LaunchEvent, always integer lamports)
        - first_k_buy_pressure: Decimal (net, exact arithmetic)
        - first_k_volume_lamports: int (sum, non-negative)

    Parameters
    ----------
    pool_creation_event : LaunchEvent
        The pool/bonding-curve creation event.  Establishes:
          - event_time (the canonical decision anchor, C-5)
          - event_time.slot (the start of the K-slot window and the lower bound)

    classified_events : Sequence[tuple[LaunchEvent, bool]]
        Sequence of (event, is_buy) pairs where is_buy was determined at decode
        time from the instruction discriminator.  All events must satisfy:
            pool_creation_event.event_time.slot
            <= event.event_time.slot
            <= pool_creation_event.event_time.slot + first_k_slots
        FeatureWindowError is raised on any violation.

    first_k_slots : int
        K — the number of slots after pool creation over which to accumulate.
        The window is [creation_slot, creation_slot + K] (both inclusive).

    Returns
    -------
    BuyPressureFeatures
        Typed result with provenance manifest.  The provenance guard has been
        run; a future-slot or pre-creation contribution is impossible in the
        returned object.

    Raises
    ------
    FeatureWindowError
        If any event has slot > cutoff (lookahead) or slot < creation_slot
        (pre-creation, M-1 fix).
    ProvenanceTaintError
        If the accumulated max_source_slot somehow exceeds the cutoff
        (second-line defence; should not be reachable if FeatureWindowError is raised).
    """
    accumulator = BuyPressureAccumulator(
        mint=pool_creation_event.mint,
        event_time_slot=pool_creation_event.event_time.slot,
        first_k_slots=first_k_slots,
    )

    for event, is_buy in classified_events:
        accumulator.observe(event, is_buy=is_buy)

    return accumulator.to_features(event_time=pool_creation_event.event_time)
