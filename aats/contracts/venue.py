"""ExecutionVenue ABC + SimulationVenue + supporting venue types.

Source of truth: execution-venue.md (version 1.1.0, FROZEN post-G1-red-team).

KEY INVARIANTS:
  1. execute(intent, event) -> FillResult arity PRESERVED from the sim seam
     (code-reviewer R-02).  Intent type is promoted SwapIntent → EntryIntent
     with integer/Decimal money fields; the arity/return-type is unchanged.
  2. The loop core imports ONLY ExecutionVenue (the ABC) — never a concrete class.
  3. SimulationVenue implements all 8 ABC members sim-native (code-reviewer R-01):
     submit_mode=SIMULATION, no-network land(), key-less sign() — never calls
     aats-signer, no key held.
  4. sign() in the real venue crosses to aats-signer over a Unix-domain socket
     (ADR-0009).  The hot-core venue holds ONLY the PUBKEY.
  5. DRY_RUN is a first-class SubmitMode, NOT a flag buried in code (FR-039).
     In DRY_RUN, land() refuses to transmit — there is no code path to the network.
  6. The venue holds NO private keys (ADR-0009, crypto-security-engineer's domain).

Money fields (data-models.md §0):
  FillResult.tip_lamports        → int (lamports)
  FillResult.priority_lamports   → int (lamports)
  FillResult.tokens_out          → int (base units)
  FillResult.effective_price_*   → Decimal-as-string (exact, money-derived)
  Quote.price                    → Decimal-as-string
  Quote.amount_*                 → int (base units / lamports)

The sim's float reserves are sim-only (data-models.md §0 note).
"""

from __future__ import annotations

import math
import random
from abc import ABC, abstractmethod
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, model_validator

from aats.contracts.events import LaunchEvent, LaunchSource
from aats.contracts.intents import EntryIntent, ExitIntent, ReduceIntent

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class SubmitMode(StrEnum):
    """Structural enforcement of the DRY-RUN rule (FR-039, AC-060).

    SIMULATION — SimulationVenue — no network ever.
    DRY_RUN    — real venue: quote+build+sign; land() refuses to transmit.
    DEVNET     — REAL submit to Solana DEVNET only (worthless SOL, separate cluster).
                 NOT mainnet.  Exercises the full submit→land→confirm→reconcile path
                 against a real RPC without touching real capital.
                 Gated by SOLANA_CLUSTER=devnet + a devnet RPC URL (RPC_DEVNET env var).
                 DRY_RUN_ENABLED is IRRELEVANT here; devnet is its own cluster.
    LIVE       — real submit to mainnet — ONLY when DRY_RUN_ENABLED=false + CEO auth.
    """

    SIMULATION = "SIMULATION"
    DRY_RUN = "DRY_RUN"
    DEVNET = "DEVNET"
    LIVE = "LIVE"


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


# ---------------------------------------------------------------------------
# Wire types returned by venue primitives
# ---------------------------------------------------------------------------


class Quote(BaseModel):
    """A price quote from the AMM.

    price: Decimal-as-string (SOL per token, money-derived exact quantity).
    amount_in and amount_out: integer base units (NOT float).
    """

    mint: str
    side: Side
    price: str  # Decimal-as-string — parse with Decimal(quote.price)
    amount_in_base: int  # integer base units
    amount_out_base: int  # integer base units
    valid_until_slot: int

    model_config = {"frozen": True}

    @model_validator(mode="before")
    @classmethod
    def _reject_float_amounts(cls, data: object) -> object:
        if not isinstance(data, dict):
            return data
        for field in ("amount_in_base", "amount_out_base"):
            val = data.get(field)
            if isinstance(val, float):
                raise ValueError(
                    f"Quote.{field} must be int (base units), got float {val!r}."
                )
        return data


class UnsignedTx(BaseModel):
    """Serialized unsigned transaction bytes (base64) + metadata."""

    serialized_b64: str  # base64-encoded serialized transaction
    intent_kind: str  # "ENTRY" | "EXIT" | "REDUCE"
    mint: str
    client_intent_id: str

    model_config = {"frozen": True}


class SignedTx(BaseModel):
    """Serialized signed transaction bytes (base64) + signing metadata.

    The venue holds only the PUBKEY.  The key lives in aats-signer (ADR-0009).
    """

    serialized_b64: str  # base64-encoded signed transaction
    signer_pubkey: str  # the PUBKEY that signed (not the private key)
    intent_kind: str
    mint: str
    client_intent_id: str

    model_config = {"frozen": True}


class SimResult(BaseModel):
    """Result of simulateTransaction (CU estimate + revert status)."""

    success: bool
    cu_consumed: int
    revert_reason: str | None

    model_config = {"frozen": True}


class LandResult(BaseModel):
    """Result of land() — includes dry-run short-circuit."""

    submitted: bool
    reason: str  # "landed" | "dry_run" | "signer_refused" | ...
    signature: str | None  # tx signature if submitted, else None
    land_slot: int | None

    model_config = {"frozen": True}


class FillResult(BaseModel):
    """What the venue returns after execute() or exit().

    landed=False means no position was created (reverted, too-late, dry-run).
    Money fields are integer (lamports/base-units) or Decimal-as-string.
    The sim's float fields are NOT promoted to production (data-models.md §0).
    """

    landed: bool
    reason: str = ""
    land_slot: int | None = None
    slot_delay: int | None = None  # slots behind the LP-add event
    buyers_ahead: int = 0  # co-buyers who moved price before you (C-1)
    # Tokens received — integer base units (NOT float)
    tokens_out: int = 0
    # SOL per token actually paid — Decimal-as-string (NOT float)
    effective_price_decimal: str = "0"
    # Entry slippage in bps — integer (NOT float)
    entry_slippage_bps: int = 0
    # Tip and priority — integer lamports (NOT float)
    tip_lamports: int = 0
    priority_lamports: int = 0

    model_config = {"frozen": True}

    @model_validator(mode="before")
    @classmethod
    def _reject_float_money(cls, data: object) -> object:
        """Reject float values for integer money fields (data-models.md §0)."""
        if not isinstance(data, dict):
            return data
        _INT_FIELDS = ("tokens_out", "entry_slippage_bps", "tip_lamports", "priority_lamports")
        for field in _INT_FIELDS:
            val = data.get(field)
            if val is not None and isinstance(val, float):
                raise ValueError(
                    f"FillResult.{field} must be int, got float {val!r}.  "
                    "Use integer base units (data-models.md §0)."
                )
        return data

    @property
    def effective_price(self) -> Decimal:
        """Parse effective_price_decimal as a Decimal (never returns float)."""
        return Decimal(self.effective_price_decimal)


# ---------------------------------------------------------------------------
# Venue registry entry
# ---------------------------------------------------------------------------


class VenueRegistryEntry(BaseModel):
    """An entry in the live-verified venue/program-ID registry.

    Source: execution-venue.md §3.
    Program IDs are DATA loaded and verified at startup — never literals
    in a decoder (AC-002, build guard §3.2).
    """

    venue: LaunchSource
    program_id: str  # the on-chain program ID (NOT hardcoded in any decoder)
    decoder: str  # decoder module name
    amm_fee_bps: int  # PumpSwap 25 / Raydium 25 (A-005/006) — fee is registry data
    migration_target: LaunchSource | None  # pump.fun -> pumpswap (A-001)
    status: Literal["active", "candidate", "deprecated"]
    last_verified_slot: int  # set by the startup live-verify probe (FR-001)

    model_config = {"frozen": True}


# ---------------------------------------------------------------------------
# ExecutionVenue ABC — the seam the loop core imports
# ---------------------------------------------------------------------------


class ExecutionVenue(ABC):
    """The pluggable execution venue interface.

    Source: execution-venue.md §1.
    The loop core imports ONLY this ABC — never a concrete class.

    execute(intent, event) -> FillResult: arity PRESERVED from the sim seam
    (execution-venue.md §6, code-reviewer R-02).  Tip/CU/slippage are
    first-class fields on the intent (not hidden).

    sign() MUST NOT hold a private key.  In the real venue it crosses to
    aats-signer over a local Unix-domain socket (ADR-0009).  In the sim it
    returns a sim signature with no network call and no key.
    """

    name: str  # registry id

    # --- SNIPE hot path (unchanged arity from the sim seam) ---
    @abstractmethod
    def execute(self, intent: EntryIntent, event: LaunchEvent) -> FillResult:
        """Build + (simulate) + land in one call for the snipe hot path.

        Tip/CU/slippage are on the intent — first-class.
        Returns FillResult (landed/slot_delay/buyers_ahead/etc.).
        """
        ...

    # --- FAST path (exits/survivors) ---
    @abstractmethod
    def exit(self, intent: ExitIntent | ReduceIntent, position: object) -> FillResult:
        """Exit or reduce an open position via the FAST path."""
        ...

    # --- lifecycle primitives (used by execute/exit; exposed for testing) ---
    @abstractmethod
    def quote(self, mint: str, side: Side, amount_base: int) -> Quote:
        """Fetch a price quote from the AMM."""
        ...

    @abstractmethod
    def build(self, intent: EntryIntent | ExitIntent | ReduceIntent) -> UnsignedTx:
        """Build an unsigned transaction from an intent."""
        ...

    @abstractmethod
    def sign(self, tx: UnsignedTx, wallet_id: str) -> SignedTx:
        """Sign a transaction.

        CROSSES A PROCESS BOUNDARY (ADR-0009): in the real venue, this
        serializes the UnsignedTx and calls aats-signer over a local
        Unix-domain socket.  The venue holds ONLY the PUBKEY.

        aats-signer independently re-validates (SOL-spend cap, program-ID
        allowlist, Jito-tip-account-pinned transfers) and may REFUSE
        (raises SignerRefused).  A refusal aborts the snipe/exit.

        In SimulationVenue, this returns a sim signature — NO network,
        NO key, NO call to aats-signer.
        """
        ...

    @abstractmethod
    def simulate(self, tx: SignedTx) -> SimResult:
        """simulateTransaction (CU estimate, revert detection)."""
        ...

    @abstractmethod
    def land(self, tx: SignedTx, tip_lamports: int, cu_price: int) -> LandResult:
        """Broadcast the transaction.

        In DRY_RUN: performs everything up to and including sign() for latency
        measurement, then REFUSES to transmit — returns LandResult(submitted=False,
        reason="dry_run").  There is no code path in DRY_RUN that reaches the
        block engine (FR-039).
        """
        ...

    @abstractmethod
    def reconcile(self, land: LandResult) -> FillResult:
        """Reconcile a land attempt into a canonical FillResult."""
        ...

    @property
    @abstractmethod
    def submit_mode(self) -> SubmitMode:
        """DRY-RUN is a first-class state, NOT a flag check buried in code (FR-039)."""
        ...


# ---------------------------------------------------------------------------
# SimulationVenue — paper / replay; retained from the sim (R-01)
# ---------------------------------------------------------------------------

SLOT_MS = 400  # Solana target slot time (ms)


class SimulationVenue(ExecutionVenue):
    """Models the landing race + slippage for paper / replay.

    Source: execution-venue.md §2, code-reviewer R-01.
    submit_mode = SIMULATION — no network, ever.
    sign() returns a sim signature — no key, no call to aats-signer.
    land() returns a simulated LandResult — no network.

    The sim's _competitor_delay and ahead_size constants are used here
    for simulation fidelity ONLY.  They MUST NOT bleed into the recorded
    cost stack (C-2; validation-harness.md §2 import guard).

    The execute() semantics (landing race + slippage + assert_min_out revert)
    are preserved from sniper_sim/venue.py.  The intent type is promoted to
    EntryIntent (integer money fields) — float reserves (sim-only) are used
    internally but never exposed on the public contract types.
    """

    name: str = "simulation"

    def __init__(
        self,
        rng: random.Random,
        slot_capacity: int = 3,
        max_slots: int = 3,
        market_tip_lamports: int = 200_000,
        ahead_size_sol_lamports: int = 1_200_000_000,  # 1.2 SOL in lamports
    ) -> None:
        self.rng = rng
        self.slot_capacity = slot_capacity
        self.max_slots = max_slots
        self.market_tip = market_tip_lamports
        # ahead_size in lamports (integer, not float SOL)
        self.ahead_size_lamports = ahead_size_sol_lamports

    # ---- internal simulation helpers ----

    def _poisson(self, lam: float) -> int:
        """Knuth's algorithm for Poisson draws (small lambda)."""
        L, k, p = math.exp(-lam), 0, 1.0
        while True:
            k += 1
            p *= self.rng.random()
            if p <= L:
                return k - 1

    def _competitor_tip(self) -> int:
        return int(self.market_tip * self.rng.lognormvariate(0.0, 0.6))

    def _competitor_delay(self) -> int:
        """Sim-only: competitor latency distribution (sim/internal only, C-2)."""
        r = self.rng.random()
        if r < 0.15:
            return 0  # insider/co-bundle
        if r < 0.75:
            return 1
        if r < 0.95:
            return 2
        return 3

    def _simulate_amm_entry(
        self,
        sol_reserve_lamports: int,
        token_reserve_base: int,
        sol_in_lamports: int,
        buyers_ahead: int,
    ) -> tuple[int, str, int]:
        """Sim AMM fill: returns (tokens_out, effective_price_decimal_str, entry_slippage_bps).

        Uses integer arithmetic throughout — no float money fields.
        """
        # Each buyer ahead adds ahead_size_lamports worth of SOL to the pool first.
        effective_reserve_sol = sol_reserve_lamports + buyers_ahead * self.ahead_size_lamports
        effective_reserve_tok = token_reserve_base
        # Constant-product AMM: k = x * y
        k = effective_reserve_sol * effective_reserve_tok
        new_reserve_sol = effective_reserve_sol + sol_in_lamports
        if new_reserve_sol == 0:
            return 0, "0", 0
        new_reserve_tok = k // new_reserve_sol
        tokens_out = effective_reserve_tok - new_reserve_tok
        if tokens_out <= 0:
            return 0, "0", 0

        # Spot price before our trade (SOL lamports per token base unit)
        spot_price = Decimal(effective_reserve_sol) / Decimal(effective_reserve_tok)
        # Our effective price
        eff_price = Decimal(sol_in_lamports) / Decimal(tokens_out)
        # Slippage in bps
        if spot_price > 0:
            slip_fraction = (eff_price - spot_price) / spot_price
            entry_slippage_bps = int(slip_fraction * 10_000)
        else:
            entry_slippage_bps = 0

        return tokens_out, str(eff_price), max(0, entry_slippage_bps)

    def _attempt_land(
        self, intent: EntryIntent, event: LaunchEvent
    ) -> tuple[bool, int | None, int, int]:
        """Return (landed, land_slot, slot_delay, buyers_ahead)."""
        # Use a fixed internal latency for sim (no tier injection needed for paper)
        ingress_ms = 60.0  # dedicated_geyser default
        jitter_ms = 25.0
        build_ms = 6.0
        rng = self.rng if hasattr(self.rng, "gauss") else None
        if rng:
            internal_ms = max(1.0, rng.gauss(ingress_ms, jitter_ms)) + build_ms
        else:
            internal_ms = ingress_ms + build_ms
        my_delay = max(1, math.ceil(internal_ms / SLOT_MS))
        if my_delay > self.max_slots:
            return False, None, my_delay, event.competitors
        buyers_ahead = 0
        for _ in range(event.competitors):
            cd = self._competitor_delay()
            if cd < my_delay or cd == my_delay and self._competitor_tip() > intent.tip_lamports:
                buyers_ahead += 1
        return True, event.event_time.slot + my_delay, my_delay, buyers_ahead

    # ---- ABC implementations ----

    @property
    def submit_mode(self) -> SubmitMode:
        return SubmitMode.SIMULATION

    def execute(self, intent: EntryIntent, event: LaunchEvent) -> FillResult:
        """Sim landing race + slippage; preserves sim execute() semantics (R-02)."""
        # priority_lamports: cu_price_microlamports * CU_BUDGET / 1_000_000
        # Using a fixed CU budget of 200_000 as a sim constant.
        _CU_BUDGET = 200_000
        priority_lamports = (intent.cu_price_microlamports * _CU_BUDGET) // 1_000_000

        landed, land_slot, slot_delay, buyers_ahead = self._attempt_land(intent, event)
        if not landed:
            return FillResult(
                landed=False,
                reason="too_late_or_outbid",
                slot_delay=slot_delay,
                buyers_ahead=buyers_ahead,
                tip_lamports=0,
                priority_lamports=0,
            )

        tokens_out, eff_price_str, slip_bps = self._simulate_amm_entry(
            event.sol_reserve_lamports,
            event.token_reserve_base,
            intent.sol_in_lamports,
            buyers_ahead,
        )

        # assert_min_out: if slippage blew past tolerance, bundle reverts.
        if slip_bps > intent.slippage_bps:
            return FillResult(
                landed=False,
                reason="reverted_min_out",
                land_slot=land_slot,
                slot_delay=slot_delay,
                buyers_ahead=buyers_ahead,
                entry_slippage_bps=slip_bps,
                tip_lamports=0,
                priority_lamports=0,
            )

        return FillResult(
            landed=True,
            reason="filled",
            land_slot=land_slot,
            slot_delay=slot_delay,
            buyers_ahead=buyers_ahead,
            tokens_out=tokens_out,
            effective_price_decimal=eff_price_str,
            entry_slippage_bps=slip_bps,
            tip_lamports=intent.tip_lamports,
            priority_lamports=priority_lamports,
        )

    def exit(self, intent: ExitIntent | ReduceIntent, position: object) -> FillResult:
        """Sim exit: immediate fill at current price (paper mode simplification)."""
        return FillResult(
            landed=True,
            reason="sim_exit",
            land_slot=None,
            slot_delay=0,
            buyers_ahead=0,
            tokens_out=0,
            effective_price_decimal="0",
            entry_slippage_bps=0,
            tip_lamports=0,
            priority_lamports=0,
        )

    def quote(self, mint: str, side: Side, amount_base: int) -> Quote:
        return Quote(
            mint=mint,
            side=side,
            price="0",
            amount_in_base=amount_base,
            amount_out_base=0,
            valid_until_slot=0,
        )

    def build(self, intent: EntryIntent | ExitIntent | ReduceIntent) -> UnsignedTx:
        return UnsignedTx(
            serialized_b64="",
            intent_kind=str(intent.kind.value),
            mint=intent.mint,
            client_intent_id=intent.client_intent_id,
        )

    def sign(self, tx: UnsignedTx, wallet_id: str) -> SignedTx:
        """Sim signature — NO key, NO network, NO call to aats-signer (R-01)."""
        return SignedTx(
            serialized_b64=tx.serialized_b64,
            signer_pubkey=f"sim_pubkey_{wallet_id}",
            intent_kind=tx.intent_kind,
            mint=tx.mint,
            client_intent_id=tx.client_intent_id,
        )

    def simulate(self, tx: SignedTx) -> SimResult:
        return SimResult(success=True, cu_consumed=0, revert_reason=None)

    def land(self, tx: SignedTx, tip_lamports: int, cu_price: int) -> LandResult:
        """Sim land — no network, ever (submit_mode=SIMULATION)."""
        return LandResult(
            submitted=False,
            reason="simulation",
            signature=None,
            land_slot=None,
        )

    def reconcile(self, land: LandResult) -> FillResult:
        return FillResult(landed=False, reason=land.reason)
