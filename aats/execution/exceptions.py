"""Venue-specific exception taxonomy for M4 execution layer.

Source: execution-venue.md §1 (signer refusal taxonomy),
        api-contracts.md §3 (error codes), data-models.md §0.

These exceptions are the ONLY exceptions the loop core catches from the venue.
They carry a machine reason code so the FSM can transition to the correct state.
"""
from __future__ import annotations


class VenueError(Exception):
    """Base for all execution-venue errors."""

    def __init__(self, reason: str, message: str = "") -> None:
        self.reason = reason
        self.message = message or reason
        super().__init__(self.message)


class SignerRefused(VenueError):
    """aats-signer refused to sign the transaction.

    Per ADR-0009: a refusal aborts the snipe/exit, NEVER leaks the key.
    The hot-core venue holds only the PUBKEY; the signer holds the SECRET.
    Reason codes: signer_per_tx_cap_exceeded | signer_aggregate_cap_exceeded |
                  signer_velocity_exceeded | signer_program_not_allowlisted |
                  signer_unpinned_transfer | signer_unavailable.
    """

    pass


class SimulationReverted(VenueError):
    """simulateTransaction detected a revert before any submit.

    Reason: the transaction would fail on-chain. Never send after this.
    Reason codes: sim_revert | sim_cu_exceeded | sim_honeypot_suspected.
    """

    pass


class DryRunBlocked(VenueError):
    """land() was called in DRY_RUN mode — transmission was blocked.

    This is NOT an error condition; it is the expected outcome in DRY_RUN.
    Reason: dry_run (FR-039, AC-060).
    """

    pass


class QuoteStalenessError(VenueError):
    """The quote has expired; re-quote required before building.

    A stale quote is NEVER sent — re-quote instead (execution-venue.md §5).
    Reason: quote_stale.
    """

    pass


class AtomicityViolation(VenueError):
    """The transaction would leave a partial fill or unsellable token.

    Atomicity over optimism: a buy that cannot complete on intended terms
    reverts whole (execution-venue.md Standards §2).
    Reason: atomicity_violation | partial_fill_unsellable.
    """

    pass


class IdempotencyKeyConflict(VenueError):
    """A duplicate send was prevented by the idempotency key.

    A duplicate send under the same client_intent_id does NOT double-land.
    Reason: idempotency_key_conflict.
    """

    pass


class LiveSubmitBlocked(VenueError):
    """LIVE submit attempted without all three DRY-RUN gates cleared.

    Three independent gates (execution-venue.md §4):
      1. venue submit_mode == LIVE
      2. DRY_RUN_ENABLED=false (config, not absent)
      3. Funded isolated wallet configured
    Reason: live_requires_dry_run_disabled_and_ceo_auth.
    """

    pass


class DevnetSubmitBlocked(VenueError):
    """Devnet submit attempted without a devnet RPC URL configured.

    Devnet is a separate Solana cluster (worthless SOL).  It is gated by:
      1. SOLANA_CLUSTER=devnet
      2. RPC_DEVNET env var set to a non-empty devnet RPC endpoint URL

    Reason: devnet_rpc_not_configured.
    """

    pass
