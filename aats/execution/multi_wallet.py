"""Multi-wallet / bundle execution + partial fills + anti-cluster (T-328).

Architecture:
  - MultiWalletOrchestrator: dispatches entry intents across N wallets, where
    N_max is read from config (default 1 at R3; multi-wallet BUILT but NOT ACTIVATED
    until R4 per OQ-010 / AUTONOMY-DIRECTIVE.md).
  - Anti-cluster cap: total SOL allocated across ALL wallets to a single mint
    MUST NOT exceed C = PER_TRADE_CAP_LAMPORTS (custody-policy.md §1 blast-radius).
  - Partial-fill handling: each wallet's FillResult is individually reconciled;
    aggregate fill quantity and cost are tracked and returned.
  - DRY-RUN: the orchestrator calls venue.execute() on each wallet slot; the
    venue enforces DRY-RUN — no submit path here.
  - Idempotency: per-wallet intent IDs are derived deterministically from the
    base client_intent_id + wallet_slot so a retry cannot double-land.

Hard rules (non-waivable, AUTONOMY-DIRECTIVE.md §"What this directive does NOT waive"):
  - Real capital DISABLED by default behind DRY-RUN (venue enforces this).
  - No win-rate field, symbol, or target anywhere in this module.
  - All money fields are int (lamports / base units) or Decimal-as-string. NEVER float.
  - No secrets / private keys — keys are held exclusively by aats-signer (ADR-0009).
  - LLM / slow model NEVER called from this module.

Blast-radius constraint (custody-policy.md §1):
  C = PER_TRADE_CAP_LAMPORTS (per-trade cap, loaded from env, default 0.1 SOL)
  Aggregate SOL allocated to any single mint across all wallet slots MUST NOT exceed C.
  The orchestrator REFUSES (raises AntiClusterCapExceeded) if adding the current
  intent's sol_in_lamports would push the per-mint total above C.

Multi-wallet is built + tested but DEFAULT N_max = 1 until R4.
  - N_WALLETS_MAX_DEFAULT = 1
  - Env var: N_WALLETS_MAX (override; capped at N_WALLETS_MAX_HARD_CEILING = 5)
  - If N_WALLETS_MAX_ENABLED=false (or not set), the orchestrator runs single-wallet only.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from decimal import Decimal

from aats.contracts.events import LaunchEvent
from aats.contracts.intents import CostStack, EntryIntent
from aats.contracts.venue import ExecutionVenue, FillResult

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Default maximum wallet slots (R3 OQ-010 — 1 until R4).
N_WALLETS_MAX_DEFAULT: int = 1

# Hard ceiling: never allow more than this many wallets regardless of config.
# Prevents a misconfigured N_WALLETS_MAX from blowing up blast-radius.
N_WALLETS_MAX_HARD_CEILING: int = 5

# Env vars
_N_WALLETS_MAX_ENV = "N_WALLETS_MAX"
_N_WALLETS_MAX_ENABLED_ENV = "N_WALLETS_MAX_ENABLED"

# Anti-cluster cap: per-mint aggregate SOL cap across all wallets.
# Source: custody-policy.md §1 (C = per_trade_cap).
_PER_TRADE_CAP_ENV = "PER_TRADE_CAP_LAMPORTS"
_PER_TRADE_CAP_DEFAULT_LAMPORTS: int = 100_000_000  # 0.1 SOL


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class AntiClusterCapExceeded(Exception):
    """Raised when the per-mint aggregate SOL cap would be exceeded.

    custody-policy.md §1: "total SOL allocated across wallets to a mint
    MUST NOT exceed C = per_trade_cap."
    This is a HARD REFUSAL — the trade is not executed.
    """

    def __init__(self, mint: str, requested: int, current: int, cap: int) -> None:
        self.mint = mint
        self.requested_lamports = requested
        self.current_lamports = current
        self.cap_lamports = cap
        super().__init__(
            f"Anti-cluster cap exceeded for mint {mint}: "
            f"current={current} + requested={requested} > cap={cap} lamports. "
            "Refusing to execute — blast-radius constraint (custody-policy.md §1)."
        )


class MultiWalletConfigError(Exception):
    """Raised when multi-wallet configuration is invalid."""

    pass


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WalletSlotFill:
    """Fill result for a single wallet slot.

    All money fields are int (lamports / base units). Never float.
    """

    wallet_id: str
    intent_id: str  # per-wallet deterministic intent ID
    fill: FillResult
    sol_allocated_lamports: int  # int lamports — NOT float


@dataclass(frozen=True)
class BundleFillResult:
    """Aggregate fill result across all wallet slots in a bundle.

    Partial fills: individual wallet slots may land or fail independently.
    The aggregate captures total_tokens_out and total_sol_spent_lamports.

    All money fields are int (lamports / base units). Never float.
    Any_landed is True if at least one wallet slot landed.
    """

    wallet_fills: tuple[WalletSlotFill, ...]
    any_landed: bool
    total_tokens_out: int  # int base units — NOT float
    total_sol_spent_lamports: int  # int lamports — NOT float
    total_tip_lamports: int  # int lamports — NOT float
    total_priority_lamports: int  # int lamports — NOT float
    partial_fill: bool  # True if some slots landed and some did not
    # Effective average price (Decimal-as-string, NOT float).
    # "0" if nothing landed.
    aggregate_effective_price_decimal: str

    @property
    def aggregate_effective_price(self) -> Decimal:
        """Parse aggregate_effective_price_decimal as Decimal (never float)."""
        return Decimal(self.aggregate_effective_price_decimal)


# ---------------------------------------------------------------------------
# Per-mint exposure ledger (in-process, per-orchestrator-instance)
# ---------------------------------------------------------------------------


class MintExposureLedger:
    """Tracks aggregate SOL allocated to each mint across all wallet slots.

    This is the anti-cluster enforcement: no single mint can receive more than
    C = PER_TRADE_CAP_LAMPORTS across all concurrent wallet allocations.

    Thread-safety: NOT thread-safe by design. The SNIPE loop is single-writer
    per-position (T-340). If parallelism is ever added, wrap with a lock.
    """

    def __init__(self, cap_lamports: int) -> None:
        if isinstance(cap_lamports, float):
            raise TypeError(
                "MintExposureLedger: cap_lamports must be int (lamports), not float. "
                "(data-models.md §0)"
            )
        if cap_lamports <= 0:
            raise MultiWalletConfigError(
                f"MintExposureLedger: cap_lamports must be > 0, got {cap_lamports}."
            )
        self._cap: int = cap_lamports
        # mint → total SOL allocated (int lamports)
        self._exposure: dict[str, int] = {}

    @property
    def cap_lamports(self) -> int:
        """The per-mint aggregate cap in lamports (int, never float)."""
        return self._cap

    def check_and_reserve(self, mint: str, sol_lamports: int) -> None:
        """Reserve sol_lamports for a mint, raising AntiClusterCapExceeded if over cap.

        Called BEFORE executing the trade. If this raises, the trade is NOT executed.
        Call release() if the trade ultimately fails or is reverted.
        """
        if isinstance(sol_lamports, float):
            raise TypeError(
                "MintExposureLedger.check_and_reserve: sol_lamports must be int, not float."
            )
        current = self._exposure.get(mint, 0)
        proposed = current + sol_lamports
        if proposed > self._cap:
            raise AntiClusterCapExceeded(
                mint=mint,
                requested=sol_lamports,
                current=current,
                cap=self._cap,
            )
        self._exposure[mint] = proposed
        logger.info(
            "multi_wallet.ledger.reserved",
            extra={
                "mint": mint,
                "reserved_lamports": sol_lamports,
                "total_after_reserve_lamports": proposed,
                "cap_lamports": self._cap,
            },
        )

    def release(self, mint: str, sol_lamports: int) -> None:
        """Release a previously reserved amount for a mint.

        Called when a trade fails after reservation, to restore availability.
        """
        if isinstance(sol_lamports, float):
            raise TypeError(
                "MintExposureLedger.release: sol_lamports must be int, not float."
            )
        current = self._exposure.get(mint, 0)
        self._exposure[mint] = max(0, current - sol_lamports)
        logger.info(
            "multi_wallet.ledger.released",
            extra={
                "mint": mint,
                "released_lamports": sol_lamports,
                "total_after_release_lamports": self._exposure[mint],
            },
        )

    def get_exposure(self, mint: str) -> int:
        """Return current aggregate SOL allocated to mint (int lamports)."""
        return self._exposure.get(mint, 0)

    def reset_mint(self, mint: str) -> None:
        """Reset the exposure for a specific mint (e.g., after position is closed)."""
        self._exposure.pop(mint, None)
        logger.debug("multi_wallet.ledger.reset_mint", extra={"mint": mint})


# ---------------------------------------------------------------------------
# Multi-wallet orchestrator
# ---------------------------------------------------------------------------


class MultiWalletOrchestrator:
    """Orchestrates entry execution across N wallet slots.

    N_max is read from config (default 1 at R3; multi-wallet BUILT but NOT
    ACTIVATED until R4 per OQ-010 / AUTONOMY-DIRECTIVE.md).

    Usage:
        orchestrator = MultiWalletOrchestrator(
            venue=dry_run_venue,
            wallet_ids=["wallet-0"],        # N=1 at R3
            ledger=MintExposureLedger(cap_lamports=100_000_000),
        )
        result = orchestrator.execute_bundle(intent, event)
        # result is a BundleFillResult with per-wallet fills, partial-fill flag, etc.

    Anti-cluster: if total SOL for a mint across all wallets would exceed
    PER_TRADE_CAP_LAMPORTS, the orchestrator refuses BEFORE any execute() call.

    Partial-fill handling: if a wallet slot fails (signer refused, sim revert,
    dry-run blocked), the other slots still complete. The BundleFillResult
    carries partial_fill=True and the per-slot fills for reconciliation.

    DRY-RUN: all execution is delegated to venue.execute() which enforces DRY-RUN
    at the venue level. This orchestrator has NO submit path.
    """

    def __init__(
        self,
        *,
        venue: ExecutionVenue,
        wallet_ids: list[str],
        ledger: MintExposureLedger,
        n_max: int | None = None,
    ) -> None:
        """Construct the orchestrator.

        Args:
            venue:       The ExecutionVenue implementation. May be DRY_RUN or SIMULATION.
                         The orchestrator does NOT know or care about the submit_mode —
                         it delegates to venue.execute() which enforces the gate.
            wallet_ids:  List of wallet slot IDs. Length must be <= effective_n_max.
                         At R3, exactly 1 wallet ID is expected (OQ-010).
            ledger:      The per-mint exposure ledger (anti-cluster cap enforcement).
            n_max:       Override N_max. If None, reads from env (default 1).
        """
        effective_n_max = _resolve_n_max(n_max)

        if not wallet_ids:
            raise MultiWalletConfigError(
                "MultiWalletOrchestrator: wallet_ids must not be empty."
            )
        if len(wallet_ids) > effective_n_max:
            raise MultiWalletConfigError(
                f"MultiWalletOrchestrator: len(wallet_ids)={len(wallet_ids)} > "
                f"effective N_max={effective_n_max}. "
                "At R3, N_max=1 (OQ-010, custody-policy.md §1). "
                "Multi-wallet is built+tested but NOT activated until R4."
            )
        # Check for duplicate wallet IDs — each signing wallet must be distinct.
        if len(set(wallet_ids)) != len(wallet_ids):
            raise MultiWalletConfigError(
                "MultiWalletOrchestrator: wallet_ids must be unique — "
                "duplicate wallet IDs are not allowed (blast-radius per-wallet)."
            )

        self._venue = venue
        self._wallet_ids = list(wallet_ids)
        self._ledger = ledger
        self._effective_n_max = effective_n_max

        logger.info(
            "multi_wallet.orchestrator.init",
            extra={
                "n_wallets": len(wallet_ids),
                "n_max": effective_n_max,
                "ledger_cap_lamports": ledger.cap_lamports,
                "venue_name": getattr(venue, "name", "unknown"),
            },
        )

    @property
    def n_wallets(self) -> int:
        """Number of active wallet slots (int, <= N_max)."""
        return len(self._wallet_ids)

    @property
    def effective_n_max(self) -> int:
        """The effective N_max for this orchestrator instance."""
        return self._effective_n_max

    def execute_bundle(
        self,
        intent: EntryIntent,
        event: LaunchEvent,
    ) -> BundleFillResult:
        """Execute an entry intent across all wallet slots.

        Each wallet slot gets a derived intent with:
          - A unique per-wallet client_intent_id (intent.client_intent_id + ":w<slot>")
          - The per-wallet wallet_id
          - A proportional sol_in_lamports (intent.sol_in_lamports // n_wallets)

        Anti-cluster: reserves the TOTAL sol before executing any wallet.
        If the cap would be exceeded, raises AntiClusterCapExceeded before any
        venue.execute() call.

        Partial fills: each wallet slot is independently executed. A slot that
        fails (signer refused, sim revert, dry-run blocked) does NOT abort the
        other slots. partial_fill=True if at least one slot failed.

        Returns BundleFillResult with per-slot fills and aggregate totals.
        """
        mint = intent.mint
        total_sol = intent.sol_in_lamports
        n = self.n_wallets

        if isinstance(total_sol, float):
            raise TypeError(
                "execute_bundle: intent.sol_in_lamports must be int (lamports), not float."
            )

        # ---- Anti-cluster cap check (BEFORE any execution) ----
        # The entire bundle's SOL must fit within the cap for this mint.
        self._ledger.check_and_reserve(mint, total_sol)

        try:
            # ---- Distribute sol proportionally across wallets ----
            # Integer division: slots 0..n-2 get base_sol; slot n-1 gets the remainder.
            base_sol = total_sol // n
            remainder = total_sol - base_sol * n  # all int arithmetic

            slot_sols: list[int] = []
            for i in range(n):
                if i == n - 1:
                    slot_sol = base_sol + remainder
                else:
                    slot_sol = base_sol
                slot_sols.append(slot_sol)

            # Verify the sum is exact (int arithmetic invariant).
            assert sum(slot_sols) == total_sol, (
                f"Slot SOL distribution sum={sum(slot_sols)} != total={total_sol}. "
                "This is a bug in integer distribution."
            )

            logger.info(
                "multi_wallet.execute_bundle.start",
                extra={
                    "intent_id": intent.client_intent_id,
                    "mint": mint,
                    "n_wallets": n,
                    "total_sol_lamports": total_sol,
                    "slot_sols": slot_sols,
                    "venue_name": getattr(self._venue, "name", "unknown"),
                },
            )

            # ---- Execute each wallet slot ----
            slot_fills: list[WalletSlotFill] = []
            for i, (wallet_id, slot_sol) in enumerate(zip(self._wallet_ids, slot_sols)):
                slot_intent_id = f"{intent.client_intent_id}:w{i}"
                slot_intent = _derive_slot_intent(
                    base_intent=intent,
                    wallet_id=wallet_id,
                    slot_sol_lamports=slot_sol,
                    slot_intent_id=slot_intent_id,
                )

                logger.info(
                    "multi_wallet.execute_bundle.slot_start",
                    extra={
                        "parent_intent_id": intent.client_intent_id,
                        "slot_intent_id": slot_intent_id,
                        "wallet_id": wallet_id,
                        "slot_sol_lamports": slot_sol,
                        "slot_index": i,
                    },
                )

                try:
                    fill = self._venue.execute(slot_intent, event)
                except Exception as exc:
                    # Unexpected exceptions from venue are caught per-slot so
                    # other slots still run. Record as a failed fill.
                    logger.exception(
                        "multi_wallet.execute_bundle.slot_exception",
                        extra={
                            "slot_intent_id": slot_intent_id,
                            "wallet_id": wallet_id,
                            "error": str(exc),
                        },
                    )
                    fill = FillResult(
                        landed=False,
                        reason=f"slot_exception:{type(exc).__name__}",
                        tip_lamports=0,
                        priority_lamports=0,
                    )

                logger.info(
                    "multi_wallet.execute_bundle.slot_result",
                    extra={
                        "slot_intent_id": slot_intent_id,
                        "wallet_id": wallet_id,
                        "landed": fill.landed,
                        "reason": fill.reason,
                        "tokens_out": fill.tokens_out,
                        "tip_lamports": fill.tip_lamports,
                        "priority_lamports": fill.priority_lamports,
                    },
                )

                slot_fills.append(
                    WalletSlotFill(
                        wallet_id=wallet_id,
                        intent_id=slot_intent_id,
                        fill=fill,
                        sol_allocated_lamports=slot_sol,
                    )
                )

            # ---- Aggregate results ----
            return _aggregate_fills(slot_fills, mint)

        except AntiClusterCapExceeded:
            # Anti-cluster cap raised before any reservation is valid — re-release
            # is safe here because check_and_reserve raised (no partial reservation).
            # Actually check_and_reserve raises BEFORE reservation succeeds if over cap,
            # so we do NOT call release here. Re-raise directly.
            raise

        except Exception:
            # If anything unexpected fails AFTER reservation, release the reserved amount.
            self._ledger.release(mint, total_sol)
            raise


# ---------------------------------------------------------------------------
# PartialFillReconciler — reconciles partial fills back to the OMS
# ---------------------------------------------------------------------------


class PartialFillReconciler:
    """Reconciles BundleFillResult back to an OMS-compatible fill record.

    A partial fill occurs when:
      - Some wallet slots land and some do not.
      - Individual slot tokens_out < expected_tokens_out.

    The reconciler computes:
      - net SOL actually spent (sum of landed slot sols) — int lamports
      - total tokens received (sum of landed slot tokens_out) — int base units
      - partial_fill_ratio_bps: 0..10000 (int bps, never float)
    """

    @staticmethod
    def reconcile(
        bundle: BundleFillResult,
        expected_tokens_out: int,
    ) -> "PartialFillSummary":
        """Reconcile a bundle fill result into a partial fill summary.

        Args:
            bundle:               The aggregate bundle fill result.
            expected_tokens_out:  What we expected to receive (from the quote).
                                  Must be int (base units), not float.
        """
        if isinstance(expected_tokens_out, float):
            raise TypeError(
                "PartialFillReconciler.reconcile: expected_tokens_out must be int, not float."
            )

        landed_count = sum(1 for s in bundle.wallet_fills if s.fill.landed)
        total_count = len(bundle.wallet_fills)
        net_sol_spent = sum(
            s.sol_allocated_lamports for s in bundle.wallet_fills if s.fill.landed
        )  # int lamports

        tokens_received = bundle.total_tokens_out  # int base units

        # partial_fill_ratio_bps: (tokens_received / expected_tokens_out) * 10000 as int bps.
        # Guard against zero expected.
        if expected_tokens_out > 0 and tokens_received >= 0:
            partial_fill_ratio_bps = int(
                (tokens_received * 10_000) // expected_tokens_out
            )
            # Clamp to [0, 10000] (never negative, never over 100%).
            partial_fill_ratio_bps = max(0, min(10_000, partial_fill_ratio_bps))
        else:
            partial_fill_ratio_bps = 0

        return PartialFillSummary(
            landed_slots=landed_count,
            total_slots=total_count,
            net_sol_spent_lamports=net_sol_spent,
            tokens_received=tokens_received,
            partial_fill_ratio_bps=partial_fill_ratio_bps,
            total_tip_lamports=bundle.total_tip_lamports,
            total_priority_lamports=bundle.total_priority_lamports,
            aggregate_effective_price_decimal=bundle.aggregate_effective_price_decimal,
            is_partial_fill=bundle.partial_fill,
        )


@dataclass(frozen=True)
class PartialFillSummary:
    """Summary of a potentially partial fill for OMS reconciliation.

    All money fields are int (lamports / base units). Never float.
    """

    landed_slots: int
    total_slots: int
    net_sol_spent_lamports: int
    tokens_received: int
    # 0..10000 bps (int, never float): what fraction of expected was filled.
    partial_fill_ratio_bps: int
    total_tip_lamports: int
    total_priority_lamports: int
    aggregate_effective_price_decimal: str  # Decimal-as-string, not float
    is_partial_fill: bool

    @property
    def aggregate_effective_price(self) -> Decimal:
        """Parse aggregate_effective_price_decimal as Decimal (never float)."""
        return Decimal(self.aggregate_effective_price_decimal)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _resolve_n_max(override: int | None) -> int:
    """Resolve the effective N_max from override / env / default.

    At R3: N_max = 1 (OQ-010). Multi-wallet is built+tested but NOT activated.
    N_WALLETS_MAX_ENABLED env must be set to "true" to allow N > 1.
    Hard ceiling: N_WALLETS_MAX_HARD_CEILING = 5.
    """
    if override is not None:
        if isinstance(override, float):
            raise TypeError("_resolve_n_max: override must be int, not float.")
        n = int(override)
    else:
        # Multi-wallet activation gate: NOT enabled unless explicitly set.
        # This is the built+tested-but-not-activated-until-R4 gate.
        enabled = os.environ.get(_N_WALLETS_MAX_ENABLED_ENV, "false").strip().lower()
        if enabled != "true":
            # Multi-wallet not enabled — default N=1.
            return N_WALLETS_MAX_DEFAULT
        # Read from env if activation gate is open.
        raw = os.environ.get(_N_WALLETS_MAX_ENV, str(N_WALLETS_MAX_DEFAULT))
        try:
            n = int(raw)
        except ValueError as exc:
            raise MultiWalletConfigError(
                f"N_WALLETS_MAX env var must be an integer, got '{raw}'."
            ) from exc

    if n < 1:
        raise MultiWalletConfigError(
            f"Effective N_max must be >= 1, got {n}."
        )
    if n > N_WALLETS_MAX_HARD_CEILING:
        raise MultiWalletConfigError(
            f"Effective N_max={n} exceeds hard ceiling {N_WALLETS_MAX_HARD_CEILING}. "
            "Reduce N_WALLETS_MAX or update the ceiling in a future ADR."
        )
    return n


def _derive_slot_intent(
    *,
    base_intent: EntryIntent,
    wallet_id: str,
    slot_sol_lamports: int,
    slot_intent_id: str,
) -> EntryIntent:
    """Derive a per-wallet EntryIntent from the base intent.

    The per-slot intent has:
      - A unique client_intent_id (slot_intent_id = base_id + ":w<slot>")
      - The slot's wallet_id
      - sol_in_lamports = slot_sol_lamports (the slot's proportional allocation)
      - A proportionally scaled CostStack (bps are invariant; only sol amounts differ)
      - All other fields copied from base_intent

    All money fields are int. Never float.
    """
    if isinstance(slot_sol_lamports, float):
        raise TypeError(
            "_derive_slot_intent: slot_sol_lamports must be int (lamports), not float."
        )

    # CostStack bps fields are in basis points and are SIZE-INDEPENDENT —
    # they describe percentages, not amounts. They are copied unchanged.
    slot_cost_stack = CostStack(
        expected_edge_bps=base_intent.cost_stack.expected_edge_bps,
        jito_tip_bps=base_intent.cost_stack.jito_tip_bps,
        priority_fee_bps=base_intent.cost_stack.priority_fee_bps,
        entry_slippage_bps=base_intent.cost_stack.entry_slippage_bps,
        amm_fee_bps=base_intent.cost_stack.amm_fee_bps,
        exit_slippage_bps=base_intent.cost_stack.exit_slippage_bps,
        adverse_selection_bps=base_intent.cost_stack.adverse_selection_bps,
        total_cost_bps=base_intent.cost_stack.total_cost_bps,
    )

    # Tip and CU price are per-tx (not proportional to slot_sol).
    # The MEV engineer owns tip sizing; we pass through the base intent's values.
    slot_intent = EntryIntent(
        client_intent_id=slot_intent_id,
        mint=base_intent.mint,
        event_time=base_intent.event_time,
        sol_in_lamports=slot_sol_lamports,
        slippage_bps=base_intent.slippage_bps,
        tip_lamports=base_intent.tip_lamports,
        cu_price_microlamports=base_intent.cu_price_microlamports,
        target_slot=base_intent.target_slot,
        cost_stack=slot_cost_stack,
        venue=base_intent.venue,
        wallet_id=wallet_id,
    )
    return slot_intent


def _aggregate_fills(
    slot_fills: list[WalletSlotFill],
    mint: str,
) -> BundleFillResult:
    """Aggregate per-slot fills into a BundleFillResult.

    Partial fill: True if at least one slot landed AND at least one did not.
    All money fields are int. Effective price is Decimal-as-string.
    """
    any_landed = any(s.fill.landed for s in slot_fills)
    total_tokens_out: int = 0
    total_sol_spent: int = 0
    total_tip: int = 0
    total_priority: int = 0
    landed_count = 0

    for s in slot_fills:
        if s.fill.landed:
            landed_count += 1
            # All must be int (the FillResult validator enforces this).
            total_tokens_out += s.fill.tokens_out
            total_sol_spent += s.sol_allocated_lamports
            total_tip += s.fill.tip_lamports
            total_priority += s.fill.priority_lamports

    partial_fill = any_landed and (landed_count < len(slot_fills))

    # Aggregate effective price: total_sol_spent / total_tokens_out (Decimal, not float).
    # If nothing landed or no tokens received, price is "0".
    if total_tokens_out > 0 and total_sol_spent > 0:
        agg_price = str(Decimal(total_sol_spent) / Decimal(total_tokens_out))
    else:
        agg_price = "0"

    result = BundleFillResult(
        wallet_fills=tuple(slot_fills),
        any_landed=any_landed,
        total_tokens_out=total_tokens_out,
        total_sol_spent_lamports=total_sol_spent,
        total_tip_lamports=total_tip,
        total_priority_lamports=total_priority,
        partial_fill=partial_fill,
        aggregate_effective_price_decimal=agg_price,
    )

    logger.info(
        "multi_wallet.aggregate_fills",
        extra={
            "mint": mint,
            "n_slots": len(slot_fills),
            "landed_slots": landed_count,
            "any_landed": any_landed,
            "partial_fill": partial_fill,
            "total_tokens_out": total_tokens_out,
            "total_sol_spent_lamports": total_sol_spent,
            "total_tip_lamports": total_tip,
            "total_priority_lamports": total_priority,
            "aggregate_effective_price": agg_price,
        },
    )

    return result


# ---------------------------------------------------------------------------
# Factory helper for the default R3 single-wallet orchestrator
# ---------------------------------------------------------------------------


def make_single_wallet_orchestrator(
    venue: ExecutionVenue,
    wallet_id: str = "wallet-0",
    per_trade_cap_lamports: int | None = None,
) -> MultiWalletOrchestrator:
    """Construct the default R3 single-wallet orchestrator.

    N=1 per OQ-010. Multi-wallet NOT enabled until R4.
    per_trade_cap_lamports defaults to PER_TRADE_CAP_LAMPORTS env var
    (default 0.1 SOL = 100_000_000 lamports) per custody-policy.md §1.

    All amounts are int. Never float.
    """
    if isinstance(per_trade_cap_lamports, float):
        raise TypeError(
            "make_single_wallet_orchestrator: per_trade_cap_lamports must be int, not float."
        )

    cap: int
    if per_trade_cap_lamports is not None:
        cap = per_trade_cap_lamports
    else:
        raw_cap = os.environ.get(_PER_TRADE_CAP_ENV, str(_PER_TRADE_CAP_DEFAULT_LAMPORTS))
        try:
            cap = int(raw_cap)
        except ValueError as exc:
            raise MultiWalletConfigError(
                f"PER_TRADE_CAP_LAMPORTS env var must be an integer, got '{raw_cap}'."
            ) from exc

    ledger = MintExposureLedger(cap_lamports=cap)
    return MultiWalletOrchestrator(
        venue=venue,
        wallet_ids=[wallet_id],
        ledger=ledger,
        n_max=N_WALLETS_MAX_DEFAULT,
    )
