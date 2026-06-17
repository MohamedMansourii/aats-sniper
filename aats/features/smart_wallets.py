"""Smart-wallet selectivity feature (T-304): smart_wallets_in + entry lag.

THIS FEATURE IS ADVERSARIAL — IT NEVER TRIGGERS ENTRY OR SIZES UP.

AC-008 (US-004): smart_wallets_in >= 1 when a tracked wallet buys within
the first-K-slot window (slot <= event_time.slot + K).

AC-009 (US-004 NEGATIVE): smart_wallets_in = 5 MUST produce sol_in <= sol_in
for smart_wallets_in = 0 (the signal may only reduce or hold size, never increase).
This is an invariant of the RISK module (T-324), not this module.  We compute
the COUNT; we do NOT determine position size.  A column named 'should_enter',
'position_size', or 'action' is OUT OF SCOPE for this module.

AC-020 (NEGATIVE): When a new smart_wallets_in observation arrives for an open
or pre-staged position, position size MUST NOT change.  This is also enforced
by the risk module.

ADVERSARIAL INTERPRETATION
---------------------------
High smart_wallets_in is NOT a buy signal.  Smart money often exits before
retail; a high count can indicate that:
  - The "smart" wallets have already captured the entry alpha
  - We are BEHIND them (smart_wallet_entry_lag_slots > 0)
  - They may exit soon (increasing dump risk for a latecomer)

The downstream model (T-310) weights this accordingly.  This module simply
computes the COUNT and LAG honestly — it does not decide what they mean.

POINT-IN-TIME CORRECTNESS
--------------------------
A wallet observation with slot > event_time.slot + first_k_slots is a
lookahead violation.  SmartWalletWindowError is raised immediately.
A wallet observation with slot < event_time.slot is a pre-creation violation
and is also rejected.

LOOP PLACEMENT
--------------
SNIPE loop + SLOW loop: these features are cheap (set membership + slot delta).
No pandas.  O(1) per observation.

MONEY RULE
----------
No monetary fields here.  smart_wallets_in is an int count.
smart_wallet_entry_lag_slots is an int slot delta.

NO I/O: pure function module.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Literal

from aats.contracts.events import EventTime, LaunchEvent
from aats.contracts.features import FeatureProvenance, FeatureSourceWindow
from aats.contracts.provenance import check_feature_window_cutoff

logger = logging.getLogger(__name__)

FEATURE_SCHEMA_VERSION = "1.0.0"
_LINEAGE_DATASET: Literal["launch_events"] = "launch_events"


# ---------------------------------------------------------------------------
# Error
# ---------------------------------------------------------------------------


class SmartWalletWindowError(ValueError):
    """Raised when a wallet observation slot is outside [creation_slot, cutoff_slot].

    Both lookahead (slot > cutoff) and pre-creation (slot < creation_slot) are
    HARD FAILURES.  The future observation must not contribute to the count.
    """

    def __init__(
        self,
        observation_slot: int,
        cutoff_slot: int,
        wallet: str,
        *,
        creation_slot: int | None = None,
    ) -> None:
        self.observation_slot = observation_slot
        self.cutoff_slot = cutoff_slot
        self.wallet = wallet
        self.creation_slot = creation_slot

        if creation_slot is not None and observation_slot < creation_slot:
            msg = (
                f"POINT-IN-TIME VIOLATION (smart_wallets): wallet={wallet!r} "
                f"observation_slot={observation_slot} < creation_slot={creation_slot}.  "
                "Pre-creation wallet observations must not contribute to smart_wallets_in."
            )
        else:
            msg = (
                f"POINT-IN-TIME VIOLATION (smart_wallets): wallet={wallet!r} "
                f"observation_slot={observation_slot} > cutoff_slot={cutoff_slot}.  "
                "Future wallet observations are a LOOKAHEAD BLOCKER.  "
                "smart_wallets_in may only count wallets that bought in slots <= cutoff."
            )
        super().__init__(msg)


# ---------------------------------------------------------------------------
# Typed result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SmartWalletFeatures:
    """smart_wallets_in count and entry lag.

    smart_wallets_in : int
        Count of tracked smart wallets that bought within the first-K-slot window.
        NEVER a buy trigger.  Adversarial selectivity feature only.
        Zero = no tracked wallets observed.

    smart_wallet_entry_lag_slots : int | None
        Our decision slot - earliest smart wallet fill slot.
        Positive = we are BEHIND (they got in first; we see a stale signal).
        None if no smart wallets bought in the window.

    earliest_smart_wallet_slot : int | None
        Slot of the first smart wallet observation in the window.
        Used to compute entry lag against our decision slot.
        None if no smart wallets observed.

    provenance : FeatureProvenance
        Per-feature source windows (ADR-0010).
    """

    smart_wallets_in: int            # int, count — NEVER a position-sizing signal
    smart_wallet_entry_lag_slots: int | None  # int (signed); positive = we are BEHIND

    # Supporting fields (for assembler; not in FeatureFrame directly)
    earliest_smart_wallet_slot: int | None

    # Provenance
    provenance: FeatureProvenance
    event_time: EventTime
    first_k_slots: int
    max_source_slot: int


# ---------------------------------------------------------------------------
# Accumulator
# ---------------------------------------------------------------------------


@dataclass
class SmartWalletAccumulator:
    """Incremental smart-wallet observation accumulator.

    Call .observe_wallet_buy(wallet, slot) for each tracked wallet buy.
    Call .to_features(event_time, our_decision_slot) to materialise results.

    Point-in-time guarantee:
        observe_wallet_buy() RAISES SmartWalletWindowError if
        slot > event_time_slot + first_k_slots  OR  slot < event_time_slot.
        There is no silent discard.

    CRITICAL: smart_wallets_in NEVER influences entry size (AC-009, AC-020).
    This accumulator only counts and records timing; the risk module decides
    what (if anything) to do with the count, and only in a de-risk direction.
    """

    mint: str
    event_time_slot: int      # the canonical decision slot (pool creation slot)
    first_k_slots: int        # K — window size in slots

    # The set of wallet addresses we are tracking (pre-loaded from T-302 stream)
    tracked_wallets: frozenset[str] = field(default_factory=frozenset)

    # Internal state
    _observed_wallets: set[str] = field(default_factory=set)
    _wallet_slots: dict[str, int] = field(default_factory=dict)  # wallet -> slot
    _max_source_slot: int = field(default=0)
    _event_count: int = field(default=0)

    @property
    def _cutoff_slot(self) -> int:
        return self.event_time_slot + self.first_k_slots

    def observe_wallet_buy(self, wallet: str, slot: int) -> None:
        """Record a buy event from a wallet at a given slot.

        Only tracked wallets are counted.  Untracked wallets are silently
        ignored (they are not counted for smart_wallets_in).

        Parameters
        ----------
        wallet : str
            The wallet address that performed the buy.
        slot : int
            The on-chain slot of the buy event.

        Raises
        ------
        SmartWalletWindowError
            If slot > cutoff (lookahead BLOCKER) or slot < event_time_slot
            (pre-creation violation).
        """
        # Lower-bound guard
        if slot < self.event_time_slot:
            raise SmartWalletWindowError(
                observation_slot=slot,
                cutoff_slot=self._cutoff_slot,
                wallet=wallet,
                creation_slot=self.event_time_slot,
            )

        # Upper-bound guard (LOOKAHEAD BLOCKER)
        if slot > self._cutoff_slot:
            raise SmartWalletWindowError(
                observation_slot=slot,
                cutoff_slot=self._cutoff_slot,
                wallet=wallet,
            )

        # Only count if this wallet is in our tracked set
        if wallet not in self.tracked_wallets:
            return

        # Record first observation for this wallet (first buy wins for lag calculation)
        if wallet not in self._observed_wallets:
            self._observed_wallets.add(wallet)
            self._wallet_slots[wallet] = slot

        # Track the highest slot seen (for provenance)
        if slot > self._max_source_slot:
            self._max_source_slot = slot

        self._event_count += 1

    def to_features(
        self,
        event_time: EventTime,
        our_decision_slot: int | None = None,
    ) -> SmartWalletFeatures:
        """Materialise the accumulated wallet observations.

        Parameters
        ----------
        event_time : EventTime
            The canonical decision anchor (C-5).
        our_decision_slot : int | None
            The slot at which we would enter.  Used to compute entry lag.
            If None, lag is computed as event_time.slot - earliest_smart_wallet_slot.

        Returns
        -------
        SmartWalletFeatures
            Typed result with provenance manifest.

        Note
        ----
        smart_wallets_in is a COUNT feature.  It MUST NOT be used to trigger
        or increase an entry.  The downstream model (T-310) and risk layer
        (T-324) enforce this; we emit the count honestly.
        """
        effective_max_slot = (
            self._max_source_slot if self._event_count > 0 else self.event_time_slot
        )

        smart_wallets_in = len(self._observed_wallets)

        # Entry lag: our_decision_slot - earliest smart wallet slot
        earliest_slot: int | None = None
        if self._wallet_slots:
            earliest_slot = min(self._wallet_slots.values())

        lag: int | None = None
        if earliest_slot is not None:
            decision_slot = our_decision_slot if our_decision_slot is not None else event_time.slot
            lag = decision_slot - earliest_slot
            # Positive lag = we are BEHIND.  Negative would mean we were ahead
            # (unusual; possible if our signal fired before their confirmed slot).

        # Build provenance windows
        windows = [
            FeatureSourceWindow(
                feature_name="smart_wallets_in",
                max_source_slot=effective_max_slot,
                lineage_dataset=_LINEAGE_DATASET,
            ),
            FeatureSourceWindow(
                feature_name="smart_wallet_entry_lag_slots",
                max_source_slot=effective_max_slot,
                lineage_dataset=_LINEAGE_DATASET,
            ),
        ]

        # Run the build guard (data-models.md §3.3, guard 1)
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

        return SmartWalletFeatures(
            smart_wallets_in=smart_wallets_in,
            smart_wallet_entry_lag_slots=lag,
            earliest_smart_wallet_slot=earliest_slot,
            provenance=provenance,
            event_time=event_time,
            first_k_slots=self.first_k_slots,
            max_source_slot=effective_max_slot,
        )

    def reset(self) -> None:
        """Reset all state (for per-mint reuse)."""
        self._observed_wallets = set()
        self._wallet_slots = {}
        self._max_source_slot = 0
        self._event_count = 0


# ---------------------------------------------------------------------------
# Batch helper
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WalletObservation:
    """A single tracked-wallet buy observation (from T-302 wallet stream)."""

    wallet: str
    slot: int


def build_smart_wallet_features(
    pool_creation_event: LaunchEvent,
    wallet_observations: list[WalletObservation],
    tracked_wallets: frozenset[str],
    first_k_slots: int,
    our_decision_slot: int | None = None,
) -> SmartWalletFeatures:
    """Compute smart_wallets_in and entry lag from observed wallet buys.

    THIS IS AN ADVERSARIAL SELECTIVITY FEATURE — NEVER A BUY TRIGGER.
    The feature count is meant to DE-RISK, never to trigger or size up.

    Point-in-time guarantee:
        Any observation with slot > cutoff or slot < creation_slot raises
        SmartWalletWindowError.  No silent filtering.

    Parameters
    ----------
    pool_creation_event : LaunchEvent
        The pool creation event.  Sets the event_time anchor.
    wallet_observations : list[WalletObservation]
        Observed wallet buy events, point-in-time (all slots <= cutoff).
    tracked_wallets : frozenset[str]
        The set of tracked "smart money" wallets (from T-302 stream).
    first_k_slots : int
        K — the window size in slots.
    our_decision_slot : int | None
        Our entry decision slot for lag calculation.

    Returns
    -------
    SmartWalletFeatures
        Typed result with provenance manifest.

    Raises
    ------
    SmartWalletWindowError
        If any observation has slot > cutoff or slot < creation_slot.
    """
    acc = SmartWalletAccumulator(
        mint=pool_creation_event.mint,
        event_time_slot=pool_creation_event.event_time.slot,
        first_k_slots=first_k_slots,
        tracked_wallets=tracked_wallets,
    )

    for obs in wallet_observations:
        acc.observe_wallet_buy(wallet=obs.wallet, slot=obs.slot)

    return acc.to_features(
        event_time=pool_creation_event.event_time,
        our_decision_slot=our_decision_slot,
    )
