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

    Per ADR-0009 (topology) / ADR-0015 (byte-level enforcement contract, the
    go-live isolated signer is a separate PYTHON process — see
    aats/execution/signer_enforcer.py): a refusal aborts the snipe/exit,
    NEVER leaks the key, NEVER signs. The hot-core venue holds only the
    PUBKEY; the signer holds the SECRET. C0-C5 are re-validated on EVERY
    sign request; the FIRST violation refuses and nothing is signed.

    FROZEN reason-code set (ADR-0015 "Refusal exception + config source" —
    the architect owns this taxonomy; the engineer transcribes it verbatim):
        signer_tx_unparseable                    — C0: tx bytes did not parse.
        signer_alt_unresolvable_security_account — C0a: a security-relevant
            account resolves via an address-lookup-table (v1 NON-goal — the
            signer has no network to resolve ALTs; fail-closed).
        signer_program_not_allowlisted           — C1: an instruction's
            program_id is not in the live (verified) allowlist.
        signer_unpinned_transfer                 — C2: a System-program SOL
            transfer sourced from the wallet has a recipient outside the
            pinned set (live Jito tip accounts + the wallet's own ATA + self).
        signer_spend_undecodable                 — C3: a venue-program
            instruction's native-SOL debit cannot be priced from bytes (no
            verified spend decoder for its discriminator/layout) — refused
            rather than guessed.
        signer_per_tx_cap_exceeded               — C3: total tx spend exceeds
            the per-tx cap (0.1 SOL, OQ-005).
        signer_aggregate_cap_exceeded            — C4: rolling-window
            cumulative spend would exceed the burst-window cap (0.5 SOL).
        signer_velocity_exceeded                 — C4: rolling-window issued-
            signature count would exceed max_sign_count.
        signer_unbounded_approval                — C5: an SPL Token
            Approve/ApproveChecked grants an unbounded/large amount to a
            delegate outside the venue allowlist.
        signer_unavailable                       — the signer socket/process
            is unreachable. The ONLY non-refuse-not-sign reason: it means no
            enforcement ran at all, so the venue MUST abort, never fall back
            to a mock signer (RED-1).

    ADDITIVE reason codes (fix round 1, code-review MINOR findings) -- NOT part
    of the ADR-0015 C0-C5 enforcement contract above (that set stays frozen
    verbatim); these are wire/transport-layer refusals `signer_process.py`
    returns BEFORE (or independently of) `Enforcer.enforce()`. Same
    refuse-not-sign discipline: no signature bytes, no ledger mutation.
        signer_wallet_id_mismatch                — the request's `wallet_id`
            does not match this process's provisioned wallet (single-wallet-
            per-process invariant). Only enforced when the signer process is
            booted with an expected wallet id configured.
        signer_frame_too_large                   — the wire request's length
            prefix exceeds the signer's max-frame bound (DoS defense-in-depth
            for the one process holding the wallet secret).

    ADDITIVE reason codes (fix round 2, code-review MINOR finding):
        signer_payer_mismatch                    — C0: the tx's account_keys[0]
            (the payer) is not the wallet_pubkey this signer controls. Defense-
            in-depth for the ADR-0015 C0 invariant "the payer (account 0 = the
            wallet)"; without it such a tx would only fail later, at on-chain
            signature verification (still fail-closed, no fund loss, but a
            less clear failure and a wasted sign).
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
