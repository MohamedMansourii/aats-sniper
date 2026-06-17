"""First-60-second microstructure features for Solana meme-coin sniper (T-304).

These features represent where the real early-signal information lives for tokens
that are minutes-to-seconds old.  Unlike TA indicators (RSI/MACD/BB) which are
meaningless without many bars of history, these features are meaningful from the
first slot of a token's existence.

FEATURES PRODUCED
-----------------
lp_depth_lamports : int (lamports)
    Quote-side SOL/USDC depth of the liquidity pool at observation time.
    Lamports, integer.  Large LP depth = more liquidity, harder to rug.

lp_lock_status : str  ("burned" | "locked" | "unlocked")
    LP mint authority / burn status.  "burned" is safest (LP tokens sent to
    burn address — irreversible).  "locked" means LP is time-locked.
    "unlocked" = rug-ready: LP can be pulled any time.

lp_age_slots : int
    Pool age in slots at observation time.  = event_time.slot - pool_creation_slot.
    Only valid if pool_creation_slot is known; otherwise None.

mint_authority_renounced : bool
    True if the SPL Mint account's mint_authority == None.  Un-renounced mint
    authority means more tokens can be minted at any time (inflation attack).

freeze_authority_renounced : bool
    True if the SPL Mint account's freeze_authority == None.  Un-renounced freeze
    authority is a HARD RISK FLAG: the mint authority can freeze user token accounts,
    making tokens impossible to sell (classic honeypot mechanism).

dev_wallet_supply_pct_bps : int (basis points, 0..10000)
    Developer wallet supply percentage in basis points (0 = 0%, 10000 = 100%).
    Excludes LP vault and burn addresses.  High dev supply → dump risk.

bundling_detected : bool
    True if same-block / same-bundle correlated buys funded from a common ancestor
    wallet are detected (pump.fun launch-snipe bundling pattern).
    Bundling inflates initial buy pressure metrics — this flag de-risks them.

bundler_wallet_count : int
    Number of wallets in the detected bundler cluster (0 if no bundling).

holder_concentration_top10_bps : int (basis points, 0..10000)
    Share of token supply held by the top 10 holders, excluding LP vault and
    burn addresses.  High concentration → high dump-risk if any whale exits.

sniper_cluster_score : float [0.0 .. 1.0]
    Normalized score of sniper activity in the first N slots.  Combines:
      - count of wallets buying in first N slots
      - supply share of those wallets
      - funding-source synchronicity (common ancestor → higher score)
    0.0 = no detected snipers; 1.0 = highly concentrated sniper cluster.

sniper_wallet_count : int
    Raw count of wallets detected as snipers in the first N slots.

sniper_supply_share_bps : int (basis points)
    Supply share held by sniper wallets, in basis points.

effective_sell_tax_bps : int (basis points)
    Effective sell tax measured from realized in/out deltas.
    Not the declared tax (which can be anything), but the MEASURED tax:
      sell_tax_bps = round((1 - tokens_received / tokens_expected) * 10000)
    A high measured tax (e.g. > 1000 bps = 10%) is a honeypot signal.
    0 if insufficient sell events to measure.

buy_pressure_imbalance : float
    Ratio of buy volume to total volume in the first-K-slot window.
    = buy_volume_lamports / (buy_volume_lamports + sell_volume_lamports)
    Range [0.0, 1.0].  0.5 = balanced.  1.0 = all buys (suspicious if bundled).
    NaN (returned as None) if no volume observed.

unique_buyer_delta : int
    Change in unique buyer count per slot window (rolling delta).
    = current_unique_buyers - previous_unique_buyers
    Positive = new buyers entering.  Computed over the last slot window.
    Requires at least 2 slot windows of data.

time_since_pool_creation_slots : int
    Slots elapsed since pool creation = event_time.slot - pool_creation_slot.
    Zero at creation event.

time_since_first_trade_slots : int | None
    Slots elapsed since first trade = event_time.slot - first_trade_slot.
    None if no trade has occurred yet.

sell_reserve_trajectory : list[int]
    Ordered samples of the SOL reserve at each observed slot (lamports, int each).
    Used to detect reserve trajectories consistent with rug preparation.
    Empty list if no reserve samples observed.

POINT-IN-TIME CORRECTNESS (non-negotiable)
-------------------------------------------
Every feature value is a function ONLY of data with slot <= event_time.slot + first_k_slots.
There is no wall-clock, no compute-time, no future slot.

The accumulator pattern mirrors buy_pressure.py:
  - observe(event) raises MicrostructureWindowError if event.slot > cutoff.
  - The offline (batch) and online (incremental) paths use the same code path.

MONEY RULE (data-models.md §0)
-------------------------------
  - LP depth → lamports (int)
  - Supply percentages → basis points (int, 0..10000)
  - SOL amounts → lamports (int)
  - Decimal/float for ratios and scores (explicitly float, not money)
  - NO float for lamport/bps quantities

LOOP PLACEMENT
--------------
These features feed:
  - SNIPE loop (via pre-staged FeatureFrame in Redis KV)
  - SLOW loop (classifier features)
They are computed from LaunchEvent sequences — no pandas in the hot path.
The accumulator supports single-event .observe() calls for online use.

NO I/O: pure function module.  No sockets, no RPC, no Redis.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Literal

from aats.contracts.events import EventTime, LaunchEvent
from aats.contracts.features import FeatureProvenance, FeatureSourceWindow
from aats.contracts.provenance import check_feature_window_cutoff

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

FEATURE_SCHEMA_VERSION = "1.0.0"

# Lineage for all microstructure features derived from LaunchEvents
_LINEAGE_DATASET: Literal["launch_events"] = "launch_events"

# Burn addresses (well-known Solana burn / dead addresses)
BURN_ADDRESSES: frozenset[str] = frozenset({
    "11111111111111111111111111111111",           # system program (used as burn target)
    "1nc1nerator11111111111111111111111111111111", # pump.fun incinerator
})

# SOL mint address for filtering LP vaults
SOL_MINT = "So11111111111111111111111111111111111111112"

# Sniper detection: first N slots after pool creation
SNIPER_FIRST_N_SLOTS_DEFAULT = 3

# Minimum trades required to compute effective sell tax
MIN_SELL_EVENTS_FOR_TAX = 1

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class MicrostructureWindowError(ValueError):
    """Raised when an event with slot outside [creation_slot, cutoff_slot] is offered.

    Both lookahead (slot > cutoff) and pre-creation (slot < creation) are HARD
    FAILURES.  There is no silent discard — the violation must be loud and testable.
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
            msg = (
                f"POINT-IN-TIME VIOLATION (microstructure): event slot {event_slot} < "
                f"creation slot {creation_slot} for mint={mint!r}.  "
                "Pre-creation events must not contribute to microstructure features."
            )
        else:
            msg = (
                f"POINT-IN-TIME VIOLATION (microstructure): event slot {event_slot} > "
                f"cutoff slot {cutoff_slot} for mint={mint!r}.  "
                "Future data must not be offered to a microstructure accumulator.  "
                "This is a lookahead BLOCKER."
            )
        super().__init__(msg)


# ---------------------------------------------------------------------------
# Per-holder snapshot (point-in-time, used to compute concentration)
# ---------------------------------------------------------------------------


@dataclass
class HolderSnapshot:
    """Snapshot of a single holder's position at a given slot.

    Amounts are in token base units (int, not float).
    """

    wallet: str
    token_amount_base: int      # integer base units
    slot: int                   # slot at which this snapshot was observed
    is_lp_vault: bool = False   # exclude from concentration calculations
    is_burn_address: bool = False


# ---------------------------------------------------------------------------
# Sell event record for tax measurement
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SellRecord:
    """A single sell event, used to measure effective sell tax."""

    slot: int
    tokens_sold_base: int       # tokens offered to the pool (base units, int)
    sol_received_lamports: int  # SOL received (lamports, int)
    expected_sol_lamports: int  # SOL expected at fair price (lamports, int)
    # If expected > received: tax = (expected - received) / expected


# ---------------------------------------------------------------------------
# Typed result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MicrostructureFeatures:
    """All first-60s microstructure features for one mint at one event_time.

    Every integer money field is in lamports or bps (never float).
    Scores and ratios are float (explicitly statistical, not monetary).
    """

    # LP features
    lp_depth_lamports: int              # int, lamports
    lp_lock_status: str                 # "burned" | "locked" | "unlocked"
    lp_age_slots: int                   # slots since pool creation

    # Authority renounce state (from SPL Mint account)
    mint_authority_renounced: bool
    freeze_authority_renounced: bool    # UN-RENOUNCED = HARD RISK FLAG

    # Dev / bundling
    dev_wallet_supply_pct_bps: int      # bps, 0..10000
    bundling_detected: bool
    bundler_wallet_count: int

    # Holder concentration (top 10, excluding LP vault + burn)
    holder_concentration_top10_bps: int  # bps, 0..10000
    holder_count: int

    # Sniper cluster
    sniper_cluster_score: float         # [0.0, 1.0]
    sniper_wallet_count: int
    sniper_supply_share_bps: int        # bps, 0..10000

    # Effective sell tax (measured, not declared)
    effective_sell_tax_bps: int         # bps; 0 if insufficient data

    # Buy/sell pressure imbalance
    buy_pressure_imbalance: float | None  # [0.0, 1.0]; None if no volume

    # Unique buyer delta (rolling)
    unique_buyer_delta: int             # positive = new buyers entering

    # Timing (slots)
    time_since_pool_creation_slots: int
    time_since_first_trade_slots: int | None  # None if no trades yet

    # Reserve trajectory (for rug-trajectory detection)
    sell_reserve_trajectory: list[int]  # lamports, int each

    # Provenance (ADR-0010)
    provenance: FeatureProvenance

    # Metadata
    event_time: EventTime
    first_k_slots: int
    max_source_slot: int


# ---------------------------------------------------------------------------
# Incremental accumulator — the single code path for online and batch
# ---------------------------------------------------------------------------


@dataclass
class MicrostructureAccumulator:
    """Incremental first-60s microstructure feature accumulator.

    One event at a time via .observe(); materialize with .to_features().
    Offline batch and online SNIPE-loop paths share this single implementation
    to guarantee bit-identical outputs.

    Point-in-time guarantee:
        observe() RAISES MicrostructureWindowError on any event with
        slot > event_time_slot + first_k_slots  (lookahead upper bound)
        OR slot < event_time_slot               (pre-creation lower bound).
        There is no silent discard.
    """

    mint: str
    event_time_slot: int        # canonical decision slot (pool creation slot)
    first_k_slots: int          # K — window size in slots (= time budget * slots_per_s)

    # --- LP state (updated on every observe; last value wins) ---
    _lp_depth_lamports: int = field(default=0)
    _lp_lock_status: str = field(default="unlocked")
    _pool_creation_slot: int = field(default=0)

    # --- Authority state ---
    _mint_authority_renounced: bool = field(default=False)
    _freeze_authority_renounced: bool = field(default=False)

    # --- Dev / bundle detection ---
    # The creator_wallet from the pool creation event is the "dev wallet"
    _dev_wallet: str = field(default="")
    # All wallets from same-slot events (potential bundlers)
    _same_slot_wallets: dict[str, int] = field(default_factory=dict)  # wallet -> slot
    # Total token supply (base units) for supply% calculations
    _total_supply_base: int = field(default=0)
    # Dev wallet token balance (base units)
    _dev_token_balance_base: int = field(default=0)

    # --- Holder tracking ---
    # wallet -> most recent HolderSnapshot for that wallet
    _holders: dict[str, HolderSnapshot] = field(default_factory=dict)

    # --- Sniper detection ---
    # Wallets that bought in the first SNIPER_FIRST_N_SLOTS slots
    _sniper_wallets: set[str] = field(default_factory=set)
    # Funding source wallets per early buyer (creator_wallet is the "funder" in our model)
    _sniper_funders: dict[str, str] = field(default_factory=dict)  # buyer_wallet -> funder

    # --- Sell tax measurement ---
    _sell_records: list[SellRecord] = field(default_factory=list)

    # --- Buy/sell volume for pressure imbalance ---
    _buy_volume_lamports: int = field(default=0)
    _sell_volume_lamports: int = field(default=0)

    # --- Unique buyer tracking (for delta) ---
    _unique_buyers_by_slot: dict[int, set[str]] = field(default_factory=lambda: defaultdict(set))
    _all_unique_buyers: set[str] = field(default_factory=set)

    # --- First trade tracking ---
    _first_trade_slot: int | None = field(default=None)

    # --- Reserve trajectory ---
    _reserve_samples: list[int] = field(default_factory=list)  # (lamports)

    # --- Provenance tracking ---
    _max_source_slot: int = field(default=0)
    _event_count: int = field(default=0)

    @property
    def _cutoff_slot(self) -> int:
        return self.event_time_slot + self.first_k_slots

    def observe(
        self,
        event: LaunchEvent,
        *,
        is_buy: bool,
        # Authority state (from SPL Mint account at this slot)
        mint_authority_renounced: bool = False,
        freeze_authority_renounced: bool = False,
        # LP state
        lp_lock_status: str = "unlocked",
        # Token supply and holder balances (optional — used when available from enrichment)
        total_supply_base: int = 0,
        dev_token_balance_base: int = 0,
        holder_snapshot: HolderSnapshot | None = None,
        # Expected SOL for sell tax measurement (from AMM math; None to skip tax measurement)
        expected_sol_lamports: int | None = None,
        # Is this the pool creation event?
        is_pool_creation: bool = False,
        # Is this wallet a bundler (funded from same common ancestor)?
        is_bundler: bool = False,
    ) -> None:
        """Accumulate one event into the microstructure state.

        Parameters
        ----------
        event : LaunchEvent
            The raw event.  event.mint must match self.mint.
        is_buy : bool
            True if SOL flowed INTO the pool (buy); False if SOL flowed OUT (sell).
        mint_authority_renounced : bool
            Current mint authority renounce state from the SPL Mint account.
        freeze_authority_renounced : bool
            Current freeze authority renounce state.  False is a HARD RISK FLAG.
        lp_lock_status : str
            "burned" | "locked" | "unlocked" — from LP mint account state.
        total_supply_base : int
            Total token supply in base units at this slot.  0 = not yet known.
        dev_token_balance_base : int
            Dev wallet token balance in base units.  0 = not yet known.
        holder_snapshot : HolderSnapshot | None
            A single holder's position at this slot.  The accumulator maintains
            the latest snapshot per wallet.
        expected_sol_lamports : int | None
            For sell tax measurement: the SOL the seller expected at fair price
            (from AMM constant-product formula).  None = skip tax measurement.
        is_pool_creation : bool
            True if this is the pool/bonding-curve creation event.
        is_bundler : bool
            True if this wallet was detected as part of a same-bundle cluster.

        Raises
        ------
        MicrostructureWindowError
            If event.event_time.slot > cutoff (lookahead) or < creation slot.
        """
        slot = event.event_time.slot

        # Lower-bound guard: pre-creation events are causal violations
        if slot < self.event_time_slot:
            raise MicrostructureWindowError(
                event_slot=slot,
                cutoff_slot=self._cutoff_slot,
                mint=self.mint,
                creation_slot=self.event_time_slot,
            )

        # Upper-bound guard: lookahead is a BLOCKER
        if slot > self._cutoff_slot:
            raise MicrostructureWindowError(
                event_slot=slot,
                cutoff_slot=self._cutoff_slot,
                mint=self.mint,
            )

        # --- Update LP state ---
        self._lp_depth_lamports = event.sol_reserve_lamports
        self._lp_lock_status = lp_lock_status
        if is_pool_creation:
            self._pool_creation_slot = slot
            self._dev_wallet = event.creator_wallet

        # --- Update authority state ---
        self._mint_authority_renounced = mint_authority_renounced
        self._freeze_authority_renounced = freeze_authority_renounced

        # --- Dev / supply tracking ---
        if total_supply_base > 0:
            self._total_supply_base = total_supply_base
        if dev_token_balance_base > 0:
            self._dev_token_balance_base = dev_token_balance_base

        # --- Bundling detection ---
        # Track wallets buying in the first slot (same-slot correlated buys = bundling signal)
        if is_buy and slot == self.event_time_slot:
            self._same_slot_wallets[event.creator_wallet] = slot
        if is_bundler:
            self._same_slot_wallets[event.creator_wallet] = slot

        # --- Holder tracking ---
        if holder_snapshot is not None:
            self._holders[holder_snapshot.wallet] = holder_snapshot

        # --- Sniper detection ---
        if is_buy and slot <= self.event_time_slot + SNIPER_FIRST_N_SLOTS_DEFAULT:
            self._sniper_wallets.add(event.creator_wallet)
            self._sniper_funders[event.creator_wallet] = event.creator_wallet

        # --- Sell tax measurement ---
        if (
            not is_buy
            and expected_sol_lamports is not None
            and expected_sol_lamports > 0
            and event.sol_reserve_lamports > 0
        ):
            sell_rec = SellRecord(
                slot=slot,
                tokens_sold_base=event.token_reserve_base,
                sol_received_lamports=event.sol_reserve_lamports,
                expected_sol_lamports=expected_sol_lamports,
            )
            self._sell_records.append(sell_rec)

        # --- Buy/sell pressure ---
        # Pool creation events are NOT trades — do not count them toward pressure.
        # Only genuine buyer/seller transactions contribute to the imbalance metric.
        if not is_pool_creation:
            if is_buy:
                self._buy_volume_lamports += event.sol_reserve_lamports
            else:
                self._sell_volume_lamports += event.sol_reserve_lamports

        # --- Unique buyer tracking ---
        if is_buy:
            self._unique_buyers_by_slot[slot].add(event.creator_wallet)
            self._all_unique_buyers.add(event.creator_wallet)

        # --- First trade ---
        # Any buy or sell (is_buy is always True or False) is a "trade".
        # Pool creation events are excluded by the slot > event_time_slot guard.
        if self._first_trade_slot is None and slot > self.event_time_slot:
            self._first_trade_slot = slot

        # --- Reserve trajectory ---
        self._reserve_samples.append(event.sol_reserve_lamports)

        # --- Provenance ---
        if slot > self._max_source_slot:
            self._max_source_slot = slot
        self._event_count += 1

    def to_features(self, event_time: EventTime) -> MicrostructureFeatures:
        """Materialise the accumulated state into a typed MicrostructureFeatures.

        Provenance guards are run here (second defence line after observe()).
        """
        effective_max_slot = (
            self._max_source_slot if self._event_count > 0 else self.event_time_slot
        )

        # Compute lp_age_slots
        creation_slot = self._pool_creation_slot if self._pool_creation_slot else self.event_time_slot
        lp_age_slots = event_time.slot - creation_slot

        # Dev supply %
        dev_supply_bps = 0
        if self._total_supply_base > 0 and self._dev_token_balance_base > 0:
            dev_supply_bps = int(
                (self._dev_token_balance_base * 10000) // self._total_supply_base
            )
            dev_supply_bps = min(dev_supply_bps, 10000)

        # Bundling: if >= 3 wallets bought in the same slot from the creation slot
        bundler_count = len(self._same_slot_wallets)
        bundling_detected = bundler_count >= 3

        # Holder concentration (top 10, excluding LP + burn)
        active_holders = [
            h for h in self._holders.values()
            if not h.is_lp_vault and not h.is_burn_address
        ]
        total_active_supply = sum(h.token_amount_base for h in active_holders)
        holder_count = len(active_holders)
        concentration_bps = 0
        if total_active_supply > 0 and active_holders:
            sorted_holders = sorted(active_holders, key=lambda h: h.token_amount_base, reverse=True)
            top10 = sorted_holders[:10]
            top10_total = sum(h.token_amount_base for h in top10)
            concentration_bps = int((top10_total * 10000) // total_active_supply)
            concentration_bps = min(concentration_bps, 10000)

        # Sniper cluster score
        # Score = (sniper_count / total_early_traders) * synchronicity_factor
        # Where synchronicity_factor is elevated if snipers share a funding source.
        sniper_count = len(self._sniper_wallets)
        sniper_supply_bps = 0
        if self._total_supply_base > 0 and sniper_count > 0:
            sniper_total_base = sum(
                self._holders[w].token_amount_base
                for w in self._sniper_wallets
                if w in self._holders
            )
            sniper_supply_bps = int((sniper_total_base * 10000) // self._total_supply_base)
            sniper_supply_bps = min(sniper_supply_bps, 10000)

        # Synchronicity: how many snipers share funders
        funder_groups: dict[str, int] = defaultdict(int)
        for _, funder in self._sniper_funders.items():
            funder_groups[funder] += 1
        max_same_funder = max(funder_groups.values()) if funder_groups else 0
        synchronicity = max_same_funder / max(sniper_count, 1)

        # Score blends count_fraction and synchronicity
        total_early_traders = len(self._all_unique_buyers)
        count_fraction = sniper_count / max(total_early_traders, 1)
        sniper_cluster_score = float(min(1.0, 0.6 * count_fraction + 0.4 * synchronicity))

        # Effective sell tax (measured from sell records)
        effective_sell_tax_bps = 0
        if len(self._sell_records) >= MIN_SELL_EVENTS_FOR_TAX:
            total_expected = sum(r.expected_sol_lamports for r in self._sell_records)
            total_received = sum(r.sol_received_lamports for r in self._sell_records)
            if total_expected > 0:
                tax_fraction = 1.0 - (total_received / total_expected)
                effective_sell_tax_bps = int(max(0, min(10000, round(tax_fraction * 10000))))

        # Buy pressure imbalance
        total_volume = self._buy_volume_lamports + self._sell_volume_lamports
        buy_pressure_imbalance: float | None
        if total_volume > 0:
            buy_pressure_imbalance = self._buy_volume_lamports / total_volume
        else:
            buy_pressure_imbalance = None

        # Unique buyer delta: current unique buyers - previous window unique buyers
        # Use a simple slot-window approximation: count unique buyers across all slots
        # vs. count in the last slot_window
        slot_keys = sorted(self._unique_buyers_by_slot.keys())
        if len(slot_keys) >= 2:
            last_two_slots = slot_keys[-2:]
            current_window_buyers = self._unique_buyers_by_slot[last_two_slots[-1]]
            prev_window_buyers = self._unique_buyers_by_slot[last_two_slots[-2]]
            unique_buyer_delta = len(current_window_buyers) - len(prev_window_buyers)
        else:
            unique_buyer_delta = len(self._all_unique_buyers)

        # Time features
        time_since_pool_creation_slots = max(0, event_time.slot - creation_slot)
        time_since_first_trade_slots = (
            event_time.slot - self._first_trade_slot
            if self._first_trade_slot is not None
            else None
        )

        # Reserve trajectory (point-in-time: only slots <= cutoff, already enforced by observe)
        sell_reserve_trajectory = list(self._reserve_samples)

        # Build provenance windows (one per non-metadata feature field)
        feature_names = [
            "lp_depth_lamports",
            "lp_lock_status",
            "lp_age_slots",
            "mint_authority_renounced",
            "freeze_authority_renounced",
            "dev_wallet_supply_pct_bps",
            "bundling_detected",
            "bundler_wallet_count",
            "holder_concentration_top10_bps",
            "holder_count",
            "sniper_cluster_score",
            "sniper_wallet_count",
            "sniper_supply_share_bps",
            "effective_sell_tax_bps",
            "buy_pressure_imbalance",
            "unique_buyer_delta",
            "time_since_pool_creation_slots",
            "time_since_first_trade_slots",
            "sell_reserve_trajectory",
        ]

        windows = [
            FeatureSourceWindow(
                feature_name=name,
                max_source_slot=effective_max_slot,
                lineage_dataset=_LINEAGE_DATASET,
            )
            for name in feature_names
        ]

        # Run the build guard (data-models.md §3.3, guard 1) on every window
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

        return MicrostructureFeatures(
            lp_depth_lamports=self._lp_depth_lamports,
            lp_lock_status=self._lp_lock_status,
            lp_age_slots=lp_age_slots,
            mint_authority_renounced=self._mint_authority_renounced,
            freeze_authority_renounced=self._freeze_authority_renounced,
            dev_wallet_supply_pct_bps=dev_supply_bps,
            bundling_detected=bundling_detected,
            bundler_wallet_count=bundler_count,
            holder_concentration_top10_bps=concentration_bps,
            holder_count=holder_count,
            sniper_cluster_score=sniper_cluster_score,
            sniper_wallet_count=sniper_count,
            sniper_supply_share_bps=sniper_supply_bps,
            effective_sell_tax_bps=effective_sell_tax_bps,
            buy_pressure_imbalance=buy_pressure_imbalance,
            unique_buyer_delta=unique_buyer_delta,
            time_since_pool_creation_slots=time_since_pool_creation_slots,
            time_since_first_trade_slots=time_since_first_trade_slots,
            sell_reserve_trajectory=sell_reserve_trajectory,
            provenance=provenance,
            event_time=event_time,
            first_k_slots=self.first_k_slots,
            max_source_slot=effective_max_slot,
        )

    def reset(self) -> None:
        """Reset all accumulators (for per-mint reuse or test teardown)."""
        self._lp_depth_lamports = 0
        self._lp_lock_status = "unlocked"
        self._pool_creation_slot = 0
        self._mint_authority_renounced = False
        self._freeze_authority_renounced = False
        self._dev_wallet = ""
        self._same_slot_wallets = {}
        self._total_supply_base = 0
        self._dev_token_balance_base = 0
        self._holders = {}
        self._sniper_wallets = set()
        self._sniper_funders = {}
        self._sell_records = []
        self._buy_volume_lamports = 0
        self._sell_volume_lamports = 0
        self._unique_buyers_by_slot = defaultdict(set)
        self._all_unique_buyers = set()
        self._first_trade_slot = None
        self._reserve_samples = []
        self._max_source_slot = 0
        self._event_count = 0


# ---------------------------------------------------------------------------
# Batch helper (uses accumulator internally — ONE code path, not two)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ClassifiedEvent:
    """A LaunchEvent annotated with its direction and enrichment data.

    All enrichment data is point-in-time: it was known at the event's slot.
    """

    event: LaunchEvent
    is_buy: bool
    # Authority state (from SPL Mint account at event.event_time.slot)
    mint_authority_renounced: bool = False
    freeze_authority_renounced: bool = False
    # LP state
    lp_lock_status: str = "unlocked"
    # Supply / holder data (from enrichment; 0 = not available)
    total_supply_base: int = 0
    dev_token_balance_base: int = 0
    holder_snapshot: HolderSnapshot | None = None
    # Sell tax measurement
    expected_sol_lamports: int | None = None
    # Pool creation flag
    is_pool_creation: bool = False
    # Bundler flag
    is_bundler: bool = False


def build_microstructure_features(
    pool_creation_event: LaunchEvent,
    classified_events: list[ClassifiedEvent],
    first_k_slots: int,
) -> MicrostructureFeatures:
    """Compute first-60s microstructure features from pre-classified event list.

    This is the BATCH PATH.  It uses MicrostructureAccumulator internally,
    guaranteeing bit-identical outputs with the online path.

    Point-in-time guarantee:
        Any event with slot > pool_creation_event.event_time.slot + first_k_slots
        raises MicrostructureWindowError immediately (lookahead guard).
        Any event with slot < pool_creation_event.event_time.slot raises
        MicrostructureWindowError (pre-creation guard).

    Money discipline:
        All LP depth, supply amounts are int (lamports / base units).
        Ratios and scores are float.  Basis-point quantities are int.

    Parameters
    ----------
    pool_creation_event : LaunchEvent
        The pool creation event.  Sets the event_time anchor and creation slot.
    classified_events : list[ClassifiedEvent]
        Events with their buy/sell classification and enrichment data.
        All events must satisfy:
            pool_creation_event.event_time.slot <= event.slot <= cutoff
    first_k_slots : int
        K — the window size in slots.

    Returns
    -------
    MicrostructureFeatures
        Typed result with provenance manifest.

    Raises
    ------
    MicrostructureWindowError
        If any event has slot > cutoff (lookahead) or slot < creation_slot.
    """
    acc = MicrostructureAccumulator(
        mint=pool_creation_event.mint,
        event_time_slot=pool_creation_event.event_time.slot,
        first_k_slots=first_k_slots,
    )

    # Observe the pool creation event first
    acc.observe(
        pool_creation_event,
        is_buy=False,     # pool creation is not a trade
        is_pool_creation=True,
    )

    for ce in classified_events:
        acc.observe(
            ce.event,
            is_buy=ce.is_buy,
            mint_authority_renounced=ce.mint_authority_renounced,
            freeze_authority_renounced=ce.freeze_authority_renounced,
            lp_lock_status=ce.lp_lock_status,
            total_supply_base=ce.total_supply_base,
            dev_token_balance_base=ce.dev_token_balance_base,
            holder_snapshot=ce.holder_snapshot,
            expected_sol_lamports=ce.expected_sol_lamports,
            is_pool_creation=False,
            is_bundler=ce.is_bundler,
        )

    return acc.to_features(event_time=pool_creation_event.event_time)
