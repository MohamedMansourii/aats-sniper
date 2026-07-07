"""The un-bypassable signer-side enforcement contract (ADR-0015, delta to ADR-0009).

Resolves M3-audit RED-1: `rust/aats-signer` was a health-check-only scaffold
with no enforcer at all. This module IS the enforcer -- it is the contract
`aats-signer` (aats/execution/signer_process.py) runs on EVERY sign request,
BEFORE producing a single signature byte.

Trust model (ADR-0015): the signer holds the SECRET; the bot holds only the
pubkey. `enforce()` trusts NO caller-supplied field for policy -- it
deserializes the transaction bytes itself (via `solana_wire`, dependency-free
-- see that module's docstring) and re-derives every fact from them. Checks
run in order C0 -> C0a -> C1 -> C2 -> C3 -> C4 -> C5; the FIRST failure
refuses (raises `SignerRefused`) and NOTHING is signed, NOTHING is recorded
to the velocity ledger. The signer signs only if ALL checks pass.

Signatures + ordering below are LAW (ADR-0015 "The exact enforcement
interface for the execution engineer") -- transcribed verbatim; bodies are
this engineer's. `SignerPolicy` carries two ADDITIVE fields beyond the ADR's
listed set:
  - `system_program_id` -- C2/C3 require recognizing which allowlisted program
    IS the System program to decode transfers -- the ADR's field list did not
    include it and there is no way to derive "which allowlist entry is System"
    other than naming it explicitly.
  - `compute_budget_program_id` -- fix round 1 (code-review MAJOR finding):
    C3's `spend(tx)` formula as literally written in ADR-0015 ("Σ System
    transfers sourced from the wallet + Σ decoded venue native-SOL debit")
    does NOT price the ComputeBudget `SetComputeUnitPrice` priority fee
    (CU limit x CU price = real wallet SOL outflow, burned to the block
    producer). Because ComputeBudget is a `core`-category allowlisted program
    (never a `venue` entry), that outflow was priced at 0 by the ADR's literal
    formula -- a compromised hot core could emit
    [SetComputeUnitLimit(huge), SetComputeUnitPrice(huge)] and drain the
    wallet in ONE signature while C1-C4 all reported clean. This is a pure
    TIGHTENING of C3 (a superset of what the literal formula refused; nothing
    it previously refused now signs) -- consistent with "execution narrows
    risk, never widens it" -- so it ships now rather than waiting on an ADR
    text edit. It is called out here, in the C3 body below, and in the
    SELF-CHECK as a contract-conformance note / blueprint-change escalation
    for `solana-systems-architect` to fold into an ADR-0015 delta, not a
    silent deviation.
Both are called out as a contract-conformance note in the SELF-CHECK, not a
silent deviation.

Fix round 2 (code-review MAJOR + MINOR findings):
  - **C0 payer check.** ADR-0015 C0 states "the payer (account 0 = the
    wallet)" but the round-1 body never asserted it -- `sign_transaction()`
    always splices into signature slot 0 regardless, so a tx whose
    `account_keys[0]` is not `wallet_pubkey` would previously fall through to
    an on-chain signature-verification reject (fail-closed, no fund loss) but
    silently skipped the C0 invariant. `_parse_and_price` now refuses
    (`signer_payer_mismatch`) explicitly, as defense-in-depth and a clearer
    failure than an on-chain reject. ADDITIVE reason code (see
    exceptions.py's `SignerRefused` docstring).
  - **Own-ATA derivation latency (MAJOR).** `_is_own_ata` previously ran the
    pure-Python PDA search (`derive_ata` -> `_find_program_address` ->
    up to 256 SHA-256 + on-curve checks) once per candidate account key in
    the tx -- measured ~30-50ms for a realistic 16-key own-ATA-carrying tx,
    ~10x the ADR-0015 "a few ms" budget and ~13x the (round-1) 3ms client
    socket timeout. Two fixes, both applied:
      1. `_try_derive_ata` now prefers `solders`' native (Rust)
         `Pubkey.find_program_address` when solders is importable (it is a
         pinned prod dependency, `pyproject.toml`) -- sub-millisecond per
         call. The pure-Python `derive_ata`/`_find_program_address` path is
         RETAINED, unchanged, as the dependency-free offline/test fallback
         and the Rust-signer-port conformance reference (never removed).
      2. `Enforcer` now memoizes `(wallet_pubkey, candidate_mint) -> ata`
         across calls for the process lifetime (`self._ata_cache`), not just
         within one `enforce()` call. This directly targets the dominant
         real-world repeat pattern -- a blockhash-expiry retry re-enforces a
         REBUILT tx (new blockhash) carrying the SAME wallet/mint pair -- so
         a retried sign pays the derivation cost at most once, not once per
         attempt. Bounded (see `_ATA_CACHE_MAX_ENTRIES`) so a process that
         lives for a very long time cannot grow this cache unboundedly; a
         cache reset is a pure perf event, never a policy event.
Both are called out as a contract-conformance note in the SELF-CHECK, not a
silent deviation.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Protocol

from aats.execution import ed25519_field, solana_wire
from aats.execution.exceptions import SignerRefused

# ---------------------------------------------------------------------------
# SpendDecoder -- C3 per-venue native-SOL spend pricing (frozen Protocol)
# ---------------------------------------------------------------------------


class SpendDecoder(Protocol):
    def max_sol_lamports(self, ix_data: bytes) -> int:
        """Upper bound (lamports) of NATIVE SOL this venue instruction can debit from the wallet.
        Return 0 for pure wSOL-swap instructions (their SOL is a counted System wrap transfer).
        RAISE (KeyError/ValueError) if the discriminator/layout is unknown -> caller REFUSES with
        signer_spend_undecodable. NEVER guess a bound."""
        ...


class _UnverifiedSpendDecoder:
    """Fail-closed decoder for a venue whose discriminator table is still VERIFY_AT_BUILD.

    config/signer-policy.json ships every venue_spend_decoders entry as an
    UNVERIFIED placeholder (`discriminator_hex: "VERIFY_AT_BUILD"`) -- the
    architect's own recorded risk (ADR-0015 delta notice) is that these must
    be transcribed from live IDLs and CI-verified before a venue's buys may
    sign. Fabricating discriminator bytes from memory here would be WORSE
    than refusing: a wrong byte offset silently under- or over-prices spend,
    which is exactly the custody failure this whole contract exists to
    prevent. This decoder ALWAYS raises -- every instruction targeting an
    unverified venue refuses with `signer_spend_undecodable`, safely, until
    a future build transcribes the real IDL bytes into signer-policy.json
    and this loader is extended to construct a real decoder for it.
    """

    def __init__(self, allowlist_name: str) -> None:
        self._name = allowlist_name

    def max_sol_lamports(self, ix_data: bytes) -> int:
        raise ValueError(
            f"venue_spend_decoders['{self._name}'] is UNVERIFIED (VERIFY_AT_BUILD placeholder "
            "in config/signer-policy.json) -- discriminator not transcribed from the live IDL. "
            "Fail-closed: this venue's buys refuse until re-verified (ADR-0015 C3)."
        )


# ---------------------------------------------------------------------------
# SignerPolicy -- loaded ONCE at boot, immutable for the process lifetime.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SignerPolicy:
    """Loaded ONCE at boot from config/signer-policy.json + config/program-allowlist.json.
    Immutable for the process lifetime. NEVER constructed from aats.contracts.risk."""

    per_tx_cap_lamports: int  # 100_000_000  (0.1 SOL, OQ-005)
    aggregate_cap_lamports: int  # 500_000_000  (0.5 SOL, OQ-005)
    window_seconds: int  # C4 rolling window width (burst window)
    max_sign_count: int  # C4 velocity: max issued sigs per window
    allowed_program_ids: frozenset[str]  # C1 live set (executable-verified at boot)
    venue_program_ids: frozenset[str]  # subset of allowed that is category=venue
    pinned_tip_accounts: frozenset[str]  # C2 getTipAccounts@boot (empty => fail-closed)
    ata_program_id: str  # C2 own-ATA derivation
    spl_token_program_id: str  # C2 own-ATA derivation
    venue_spend_decoders: dict[str, SpendDecoder]  # program_id -> decoder (missing => refuse)
    # ADDITIVE (not in the ADR-0015 field list -- see module docstring): needed to
    # recognize which allowlisted program IS the System program for C2/C3.
    system_program_id: str = field(default="")
    # ADDITIVE (fix round 1 MAJOR finding -- see module docstring): needed to recognize
    # which allowlisted program IS ComputeBudget so C3 can price the SetComputeUnitPrice
    # priority fee as real wallet SOL outflow.
    compute_budget_program_id: str = field(default="")


# ---------------------------------------------------------------------------
# VelocityLedger -- C4 rolling aggregate + velocity cap
# ---------------------------------------------------------------------------


class VelocityLedger:
    """In-memory, per signer process. monotonic-clock (t, spend_lamports) of ISSUED signatures,
    pruned to window_seconds. NOT persisted across restart (fresh window on boot, by design)."""

    def __init__(self, window_seconds: int) -> None:
        if window_seconds <= 0:
            raise ValueError("window_seconds must be positive")
        self._window_seconds = window_seconds
        self._entries: list[tuple[float, int]] = []  # [(t, spend_lamports), ...], ascending t

    def _prune(self, now_s: float) -> list[tuple[float, int]]:
        cutoff = now_s - self._window_seconds
        self._entries = [(t, s) for (t, s) in self._entries if t > cutoff]
        return self._entries

    def would_exceed(
        self,
        now_s: float,
        spend_lamports: int,
        aggregate_cap_lamports: int,
        max_sign_count: int,
    ) -> str | None:
        """Return 'signer_aggregate_cap_exceeded' | 'signer_velocity_exceeded' if adding this sign
        would breach either cap in-window; else None. Does NOT record (read-only check)."""
        in_window = self._prune(now_s)
        window_spend = sum(s for _t, s in in_window)
        if window_spend + spend_lamports > aggregate_cap_lamports:
            return "signer_aggregate_cap_exceeded"
        if len(in_window) + 1 > max_sign_count:
            return "signer_velocity_exceeded"
        return None

    def record(self, now_s: float, spend_lamports: int) -> None:
        """Call ONLY after a signature is actually issued (post C0-C5 pass)."""
        self._prune(now_s)
        self._entries.append((now_s, spend_lamports))


# ---------------------------------------------------------------------------
# System-program / SPL-Token instruction decoding (C2, C3, C5 bodies)
# ---------------------------------------------------------------------------

_SYSTEM_CREATE_ACCOUNT = 0
_SYSTEM_TRANSFER = 2
_SYSTEM_TRANSFER_WITH_SEED = 11

_TOKEN_APPROVE = 4
_TOKEN_APPROVE_CHECKED = 13
# "Unbounded/large" per ADR-0015 C5 -- half of u64::MAX catches both the
# canonical infinite-approval sentinel (u64::MAX) and any suspiciously large amount.
_UNBOUNDED_APPROVAL_THRESHOLD = 2**63

# Bound on Enforcer._ata_cache (fix round 2 MAJOR finding perf cache) -- a generous
# ceiling for a process whose realistic working set is "the wSOL mint + however many
# distinct token mints this wallet has traded this process lifetime" (low hundreds at
# most). Exceeding it just means we reset and recompute -- a perf event, never a
# policy event.
_ATA_CACHE_MAX_ENTRIES = 4096

# ComputeBudgetInstruction discriminants (single-byte enum tag, NOT the 4-byte
# little-endian tag System-program instructions use). Source: solana-program's
# `ComputeBudgetInstruction` enum -- 0=deprecated RequestUnits, 1=RequestHeapFrame,
# 2=SetComputeUnitLimit(u32), 3=SetComputeUnitPrice(u64), 4=SetLoadedAccountsDataSizeLimit.
# Only 2 and 3 carry a priced quantity relevant to C3 (see module docstring "priority-fee
# outflow" -- fix round 1 MAJOR finding).
_COMPUTE_BUDGET_SET_LIMIT_TAG = 2
_COMPUTE_BUDGET_SET_PRICE_TAG = 3
# Solana's protocol-enforced per-transaction compute-unit ceiling. Used as the
# CONSERVATIVE fallback CU limit when a tx sets a priority-fee CU price but omits an
# explicit SetComputeUnitLimit instruction: the runtime default in that case is smaller
# (200_000 CU x instruction count, capped at this ceiling), but the signer must NEVER
# under-price a possible priority-fee outflow -- it prices the worst case the protocol
# allows, never the common case (ADR-0015 "execution narrows risk, never widens it").
_PROTOCOL_MAX_CU_LIMIT = 1_400_000
_MICROLAMPORTS_PER_LAMPORT = 1_000_000


def _resolve(account_keys: tuple[str, ...], index: int) -> str:
    if index < 0 or index >= len(account_keys):
        raise ValueError(f"account index {index} out of range")
    return account_keys[index]


def _decode_system_transfer(
    ix: solana_wire.ParsedInstruction, account_keys: tuple[str, ...]
) -> tuple[str, str, int] | None:
    """Decode a System-program instruction as a value transfer, if it is one.

    Returns (source_pubkey, recipient_pubkey, lamports) for Transfer /
    CreateAccount-with-lamports / TransferWithSeed. Returns None for any
    OTHER System-program instruction (Allocate, Assign, Nonce, ...) -- those
    move no lamports and are out of scope for C2/C3. Raises ValueError on a
    RECOGNIZED-but-malformed transfer -- the caller must fail-closed, never
    silently price a malformed instruction at zero.
    """
    if len(ix.data) < 4:
        return None
    discriminant = int.from_bytes(ix.data[0:4], "little")

    if discriminant == _SYSTEM_TRANSFER:
        if len(ix.data) < 12 or len(ix.account_indices) < 2:
            raise ValueError("malformed System::Transfer instruction")
        lamports = int.from_bytes(ix.data[4:12], "little")
        return (
            _resolve(account_keys, ix.account_indices[0]),
            _resolve(account_keys, ix.account_indices[1]),
            lamports,
        )

    if discriminant == _SYSTEM_CREATE_ACCOUNT:
        if len(ix.data) < 12 or len(ix.account_indices) < 2:
            raise ValueError("malformed System::CreateAccount instruction")
        lamports = int.from_bytes(ix.data[4:12], "little")
        return (
            _resolve(account_keys, ix.account_indices[0]),
            _resolve(account_keys, ix.account_indices[1]),
            lamports,
        )

    if discriminant == _SYSTEM_TRANSFER_WITH_SEED:
        # layout: discriminant(4) + lamports(8) + seed(borsh str) + owner(32)
        # accounts = [from, base, to] -- recipient is the 3rd account.
        if len(ix.data) < 12 or len(ix.account_indices) < 3:
            raise ValueError("malformed System::TransferWithSeed instruction")
        lamports = int.from_bytes(ix.data[4:12], "little")
        return (
            _resolve(account_keys, ix.account_indices[0]),
            _resolve(account_keys, ix.account_indices[2]),
            lamports,
        )

    return None


def _check_approval_hygiene(
    ix: solana_wire.ParsedInstruction,
    account_keys: tuple[str, ...],
    policy: SignerPolicy,
) -> None:
    """C5: refuse an unbounded/large Approve to a delegate outside the venue allowlist."""
    if len(ix.data) < 1:
        return
    tag = ix.data[0]
    if tag not in (_TOKEN_APPROVE, _TOKEN_APPROVE_CHECKED):
        return
    if len(ix.data) < 9:
        raise SignerRefused(
            reason="signer_tx_unparseable",
            message="malformed SPL Token Approve/ApproveChecked instruction data.",
        )
    amount = int.from_bytes(ix.data[1:9], "little")
    # Approve accounts:        [source, delegate, owner, ...signers]
    # ApproveChecked accounts: [source, mint, delegate, owner, ...signers]
    delegate_slot = 1 if tag == _TOKEN_APPROVE else 2
    if len(ix.account_indices) <= delegate_slot:
        raise SignerRefused(
            reason="signer_tx_unparseable",
            message="Approve/ApproveChecked instruction is missing the delegate account.",
        )
    try:
        delegate = _resolve(account_keys, ix.account_indices[delegate_slot])
    except ValueError as exc:
        raise SignerRefused(reason="signer_tx_unparseable", message=str(exc)) from exc

    if amount >= _UNBOUNDED_APPROVAL_THRESHOLD and delegate not in policy.venue_program_ids:
        raise SignerRefused(
            reason="signer_unbounded_approval",
            message=(
                f"Approve amount {amount} to delegate {delegate!r} is unbounded/large and the "
                "delegate is not a known venue program (ADR-0015 C5)."
            ),
        )


def _decode_compute_budget_ix(ix: solana_wire.ParsedInstruction) -> tuple[str, int] | None:
    """Decode a ComputeBudget-program instruction as a priority-fee-relevant field, if it is one.

    Returns ('limit', cu_limit) for SetComputeUnitLimit, ('price', cu_price_microlamports)
    for SetComputeUnitPrice. Returns None for any OTHER ComputeBudget instruction
    (RequestHeapFrame, SetLoadedAccountsDataSizeLimit, the deprecated RequestUnits) --
    those carry no priced wallet-SOL-outflow field. Raises ValueError on a
    RECOGNIZED-but-malformed instruction -- the caller must fail-closed, mirrors
    `_decode_system_transfer`.
    """
    if len(ix.data) < 1:
        return None
    tag = ix.data[0]
    if tag == _COMPUTE_BUDGET_SET_LIMIT_TAG:
        if len(ix.data) < 5:
            raise ValueError("malformed ComputeBudget::SetComputeUnitLimit instruction")
        return ("limit", int.from_bytes(ix.data[1:5], "little"))
    if tag == _COMPUTE_BUDGET_SET_PRICE_TAG:
        if len(ix.data) < 9:
            raise ValueError("malformed ComputeBudget::SetComputeUnitPrice instruction")
        return ("price", int.from_bytes(ix.data[1:9], "little"))
    return None


# ---------------------------------------------------------------------------
# Enforcer -- the un-bypassable control
# ---------------------------------------------------------------------------


class Enforcer:
    """The un-bypassable control. Constructed once with the boot-loaded policy + ledger.
    enforce() re-derives EVERY fact from tx_bytes; caller-supplied metadata is untrusted."""

    def __init__(self, policy: SignerPolicy, ledger: VelocityLedger) -> None:
        self._policy = policy
        self._ledger = ledger
        # Perf-only cache (fix round 1 MINOR finding), NEVER a policy input: the signer
        # main loop calls enforce() (ADR-0015 step 3) then compute_spend_lamports()
        # (step 4) back-to-back for the IDENTICAL tx_bytes/wallet_pubkey -- without this,
        # `_parse_and_price` (including the C2 own-ATA PDA search, the most expensive
        # part) runs TWICE per request. Keyed by VALUE equality (not id()/identity) so a
        # stale/aliased pointer can never produce a false hit. Only ever populated with a
        # value `_parse_and_price` already fully validated in this call -- a cache hit
        # returns exactly what a fresh computation would, never a shortcut around C0-C3.
        self._price_cache: tuple[bytes, str, solana_wire.ParsedTx, int] | None = None
        # Perf-only cache (fix round 2 MAJOR finding), NEVER a policy input: memoizes
        # (wallet_pubkey, candidate_mint) -> derived ATA (or None if candidate_mint is
        # not a valid 32-byte pubkey) across DIFFERENT enforce() calls -- unlike
        # `_price_cache` above (single-tx only), this persists for the process
        # lifetime. Targets the dominant real-world repeat pattern: a blockhash-expiry
        # retry re-enforces a REBUILT tx (new blockhash -> different tx_bytes, so
        # `_price_cache` cannot help) carrying the SAME wallet/mint pair. A cache hit
        # returns exactly the same value a fresh `_try_derive_ata` call would (the PDA
        # derivation is a pure function of its inputs) -- never a shortcut around any
        # check. Bounded: reset entirely (not evicted piecewise) once it would exceed
        # `_ATA_CACHE_MAX_ENTRIES`, trading a one-time recompute for a hard memory bound.
        self._ata_cache: dict[tuple[str, str], str | None] = {}

    @property
    def policy(self) -> SignerPolicy:
        """Read-only access to the boot-loaded policy (e.g. for a caller's own
        conservative fallback accounting) -- never mutated after construction."""
        return self._policy

    @property
    def ledger(self) -> VelocityLedger:
        """Read-only access to the C4 velocity ledger this Enforcer was constructed
        with (e.g. so a caller that only holds the Enforcer can still call
        ledger.record() after signing, per the ADR-0015 main-loop step 4)."""
        return self._ledger

    def enforce(self, tx_bytes: bytes, wallet_pubkey: str, now_s: float) -> None:
        """Run C0..C5 IN ORDER. Raise SignerRefused(reason=<code>, message=...) on the FIRST
        violation and return before any signing. Return None iff the tx MAY be signed. MUST NOT
        sign and MUST NOT mutate the ledger on refusal. The signer records the ledger AFTER it has
        produced the signature."""
        parsed, total_spend = self._parse_and_price(tx_bytes, wallet_pubkey)

        # --- C4: rolling aggregate + velocity cap ---
        violation = self._ledger.would_exceed(
            now_s, total_spend, self._policy.aggregate_cap_lamports, self._policy.max_sign_count
        )
        if violation is not None:
            raise SignerRefused(reason=violation, message=f"C4 breach: {violation}")

        # --- C5: approval hygiene ---
        for ix in parsed.instructions:
            if ix.program_id == self._policy.spl_token_program_id:
                _check_approval_hygiene(ix, parsed.account_keys, self._policy)

        return None

    def compute_spend_lamports(self, tx_bytes: bytes, wallet_pubkey: str) -> int:
        """Re-derive the C0/C0a/C1/C2/C3-validated lamport spend for tx_bytes.

        NOT a bypass -- this is the SAME parse+price computation `enforce()` performs
        (single source of truth, see `_parse_and_price`) and still raises SignerRefused
        on any C0-C3 violation. Exposed so aats-signer's main loop can call
        `ledger.record(now, spend)` AFTER producing a signature (ADR-0015 main-loop step
        4) with a value guaranteed IDENTICAL to what `enforce()` validated for this tx.
        """
        _parsed, total_spend = self._parse_and_price(tx_bytes, wallet_pubkey)
        return total_spend

    def _parse_and_price(self, tx_bytes: bytes, wallet_pubkey: str) -> tuple[solana_wire.ParsedTx, int]:
        """C0, C0a, C1, C2, C3 -- shared by enforce() and compute_spend_lamports()."""
        cached = self._price_cache
        if cached is not None and cached[0] == tx_bytes and cached[1] == wallet_pubkey:
            return cached[2], cached[3]

        # --- C0: parse ---
        try:
            parsed = solana_wire.parse_transaction(tx_bytes)
        except ValueError as exc:
            raise SignerRefused(reason="signer_tx_unparseable", message=str(exc)) from exc

        # --- C0 (fix round 2 MINOR finding): payer == the wallet this signer controls.
        # ADR-0015 C0 states "the payer (account 0 = the wallet)" as an invariant of what
        # this signer is willing to sign, but the round-1 body never asserted it --
        # sign_transaction() always splices into signature slot 0 regardless of who
        # account_keys[0] actually is. A tx whose payer is NOT this wallet would
        # previously fall through every other check and only fail at on-chain signature
        # verification (fail-closed, no fund loss -- the produced signature is invalid
        # for a payer it doesn't match) -- but that silently skipped the C0 invariant and
        # wasted a sign. Refuse explicitly, as defense-in-depth and a clearer failure.
        if parsed.payer != wallet_pubkey:
            raise SignerRefused(
                reason="signer_payer_mismatch",
                message=(
                    f"tx payer (account 0) {parsed.payer!r} does not match the wallet "
                    f"this signer controls {wallet_pubkey!r} (ADR-0015 C0)."
                ),
            )

        # --- C0a: ALT security-account rule (fail-closed, v1 NON-goal) ---
        if parsed.has_address_table_lookups:
            raise SignerRefused(
                reason="signer_alt_unresolvable_security_account",
                message=(
                    "tx references an address-lookup-table-resolved account; "
                    "security-relevant accounts (program IDs, System-transfer accounts) "
                    "must be static so the signer can validate them with no network (ADR-0015 C0a)."
                ),
            )

        # --- C1: program-ID allowlist (default-deny) ---
        for ix in parsed.instructions:
            if ix.program_id not in self._policy.allowed_program_ids:
                raise SignerRefused(
                    reason="signer_program_not_allowlisted",
                    message=f"program_id {ix.program_id!r} is not in the live allowlist.",
                )

        # --- C2: value-transfer recipient pin (+ accumulate System-transfer spend) ---
        mint_independent_pins = frozenset(self._policy.pinned_tip_accounts) | {wallet_pubkey}
        system_spend = 0
        for ix in parsed.instructions:
            if ix.program_id != self._policy.system_program_id:
                continue
            try:
                transfer = _decode_system_transfer(ix, parsed.account_keys)
            except ValueError as exc:
                raise SignerRefused(reason="signer_tx_unparseable", message=str(exc)) from exc
            if transfer is None:
                continue
            source, recipient, lamports = transfer
            if source != wallet_pubkey:
                continue  # not sourced from the wallet -- out of scope for C2/C3
            if recipient not in mint_independent_pins and not self._is_own_ata(
                recipient, wallet_pubkey, parsed.account_keys
            ):
                raise SignerRefused(
                    reason="signer_unpinned_transfer",
                    message=(
                        f"System transfer recipient {recipient!r} is not pinned "
                        "(must be a live Jito tip account, the wallet's own ATA, or self)."
                    ),
                )
            system_spend += lamports

        # --- C3: per-venue spend decode + per-tx cap (fail-closed on unknown venue ix) ---
        venue_spend = 0
        for ix in parsed.instructions:
            if ix.program_id not in self._policy.venue_program_ids:
                continue
            decoder = self._policy.venue_spend_decoders.get(ix.program_id)
            if decoder is None:
                raise SignerRefused(
                    reason="signer_spend_undecodable",
                    message=f"no spend decoder registered for venue program {ix.program_id!r}.",
                )
            try:
                venue_spend += decoder.max_sol_lamports(ix.data)
            except (KeyError, ValueError) as exc:
                raise SignerRefused(
                    reason="signer_spend_undecodable",
                    message=f"venue program {ix.program_id!r}: {exc}",
                ) from exc

        # --- C3 (fix round 1 MAJOR finding): ComputeBudget priority-fee outflow.
        # SetComputeUnitPrice x SetComputeUnitLimit is real wallet SOL outflow (burned to
        # the block-producing validator) that the ADR-0015-literal spend(tx) formula does
        # NOT price (ComputeBudget is `core`, never `venue`). Fold it in here so it is
        # bounded by the same per-tx/aggregate caps as any other spend -- see module
        # docstring "priority-fee outflow" for why this ships as a body-level tightening
        # rather than waiting on an ADR text edit. Multiple occurrences of either
        # instruction take the MAX seen (conservative; the runtime would either use one or
        # reject the tx as a duplicate instruction -- taking the max never under-prices).
        cu_limit_seen: int | None = None
        cu_price_microlamports = 0
        for ix in parsed.instructions:
            if ix.program_id != self._policy.compute_budget_program_id:
                continue
            try:
                decoded = _decode_compute_budget_ix(ix)
            except ValueError as exc:
                raise SignerRefused(reason="signer_tx_unparseable", message=str(exc)) from exc
            if decoded is None:
                continue
            kind, value = decoded
            if kind == "limit":
                cu_limit_seen = value if cu_limit_seen is None else max(cu_limit_seen, value)
            else:
                cu_price_microlamports = max(cu_price_microlamports, value)

        priority_fee_lamports = 0
        if cu_price_microlamports > 0:
            # No explicit SetComputeUnitLimit -> price the protocol MAX, never the
            # (smaller) runtime default -- never under-price a possible outflow.
            effective_cu_limit = (
                cu_limit_seen if cu_limit_seen is not None else _PROTOCOL_MAX_CU_LIMIT
            )
            # Ceiling division (matches the runtime's own fee rounding) -- rounding DOWN
            # would under-price a fractional-lamport remainder.
            numerator = effective_cu_limit * cu_price_microlamports
            priority_fee_lamports = -(-numerator // _MICROLAMPORTS_PER_LAMPORT)

        total_spend = system_spend + venue_spend + priority_fee_lamports
        if total_spend > self._policy.per_tx_cap_lamports:
            raise SignerRefused(
                reason="signer_per_tx_cap_exceeded",
                message=(
                    f"tx spend {total_spend} lamports (system={system_spend} "
                    f"venue={venue_spend} priority_fee={priority_fee_lamports}) exceeds "
                    f"per-tx cap {self._policy.per_tx_cap_lamports}."
                ),
            )

        self._price_cache = (tx_bytes, wallet_pubkey, parsed, total_spend)
        return parsed, total_spend

    def _is_own_ata(
        self, recipient: str, wallet_pubkey: str, account_keys: tuple[str, ...]
    ) -> bool:
        """C2 rule 2: is `recipient` the wallet's OWN Associated Token Account?

        The ATA is mint-specific (`find_program_address([wallet, spl_token, mint],
        ata_program)`) and the signer receives no trusted "mint" field (request
        metadata is a logging hint only, never a policy input -- ADR-0015 trust
        model). Instead, it re-derives the ATA against EVERY account key already
        present in this SAME transaction's static key array (the real mint MUST
        be one of them for a legitimate wrap/rent instruction -- e.g. the AMM
        swap instruction's own account list) and accepts a recipient match
        against ANY of them. An attacker cannot forge this: the match is
        cryptographic (SHA-256 + off-curve PDA search), not a caller claim.

        Perf (fix round 2 MAJOR finding): derivation is memoized per
        `(wallet_pubkey, candidate_mint)` for the Enforcer's lifetime (see
        `self._ata_cache`) and prefers `solders`' native PDA search over the
        pure-Python one when available (see `_try_derive_ata`) -- a cache hit
        or a native derivation returns the IDENTICAL value the pure-Python
        `derive_ata()` would for the same inputs, never a shortcut around
        the recipient-match check itself.
        """
        if len(self._ata_cache) >= _ATA_CACHE_MAX_ENTRIES:
            # Bounded, not evicted piecewise -- a full reset is a pure perf event
            # (the next lookups just recompute), never a policy event.
            self._ata_cache.clear()
        for candidate_mint in account_keys:
            cache_key = (wallet_pubkey, candidate_mint)
            if cache_key in self._ata_cache:
                ata = self._ata_cache[cache_key]
            else:
                ata = _try_derive_ata(
                    wallet_pubkey,
                    candidate_mint,
                    ata_program_id=self._policy.ata_program_id,
                    spl_token_program_id=self._policy.spl_token_program_id,
                )
                self._ata_cache[cache_key] = ata
            if ata is not None and ata == recipient:
                return True
        return False


# ---------------------------------------------------------------------------
# ATA (Associated Token Account) derivation -- ADR-0015 C2 rule 2
# ---------------------------------------------------------------------------

_PDA_MARKER = b"ProgramDerivedAddress"


def _create_program_address(seeds: list[bytes], program_id: bytes) -> bytes:
    import hashlib

    if any(len(s) > 32 for s in seeds):
        raise ValueError("PDA seed exceeds 32 bytes")
    h = hashlib.sha256()
    for s in seeds:
        h.update(s)
    h.update(program_id)
    h.update(_PDA_MARKER)
    candidate = h.digest()
    if ed25519_field.is_on_curve(candidate):
        raise ValueError("candidate address is on-curve")
    return candidate


def _find_program_address(seeds: list[bytes], program_id: bytes) -> tuple[bytes, int]:
    for bump in range(255, -1, -1):
        try:
            return _create_program_address([*seeds, bytes([bump])], program_id), bump
        except ValueError:
            continue
    raise ValueError("unable to find a valid program address (exhausted all 256 bump seeds)")


def derive_ata(wallet_pubkey: str, mint: str, *, ata_program_id: str, spl_token_program_id: str) -> str:
    """Recompute the wallet's Associated Token Account for `mint` (ADR-0015 C2 rule 2).

    derivation = find_program_address([wallet, spl_token, mint], ata_program).
    Pure Python, dependency-free (see ed25519_field.py docstring) -- correct in
    every environment, including one where `solders` is not installed.
    """
    wallet_b = solana_wire.b58decode(wallet_pubkey)
    token_b = solana_wire.b58decode(spl_token_program_id)
    mint_b = solana_wire.b58decode(mint)
    program_b = solana_wire.b58decode(ata_program_id)
    for b in (wallet_b, token_b, mint_b, program_b):
        if len(b) != 32:
            raise ValueError("PDA inputs must each decode to 32 bytes")
    addr, _bump = _find_program_address([wallet_b, token_b, mint_b], program_b)
    return solana_wire.b58encode(addr)


# ---------------------------------------------------------------------------
# Fast-path ATA derivation via solders (fix round 2 MAJOR finding)
# ---------------------------------------------------------------------------
#
# `derive_ata()`/`_find_program_address()` above are the dependency-free
# pure-Python reference: correct in every environment (no `solders` needed)
# and kept UNCHANGED as the offline/test fallback and the eventual Rust-port
# conformance spec. But run cold on the hot own-ATA-candidate-search path
# (`Enforcer._is_own_ata`, up to 256 SHA-256 + on-curve checks per candidate
# account key), it measured ~30-50ms for a realistic 16-key tx -- ~10x the
# ADR-0015 "a few ms" signer latency budget. `solders` is a pinned production
# dependency (`pyproject.toml`) whose `Pubkey.find_program_address` is the
# identical, standard algorithm implemented in Rust -- sub-millisecond per
# call. `_try_derive_ata` prefers it when importable and ONLY falls back to
# the pure-Python path when it is not (e.g. this offline dev sandbox, where
# `solders` is declared but not installed) -- so the enforcement VERDICT is
# byte-identical either way; only which implementation computes it changes.

_SOLDERS_PUBKEY_CLS: object | None = None
_SOLDERS_PROBED: bool = False


def _solders_pubkey_cls() -> object | None:
    """Lazy, cached probe for `solders.pubkey.Pubkey`. Returns the class, or None
    if `solders` is not importable. Only `ImportError` is swallowed here -- any
    OTHER error surfaces normally (this is an availability probe, not a
    correctness shortcut)."""
    global _SOLDERS_PUBKEY_CLS, _SOLDERS_PROBED
    if _SOLDERS_PROBED:
        return _SOLDERS_PUBKEY_CLS
    _SOLDERS_PROBED = True
    try:
        from solders.pubkey import Pubkey  # type: ignore
    except ImportError:
        _SOLDERS_PUBKEY_CLS = None
        return None
    _SOLDERS_PUBKEY_CLS = Pubkey
    return Pubkey


def _derive_ata_native(
    pubkey_cls: object, wallet_pubkey: str, mint: str, *, ata_program_id: str, spl_token_program_id: str
) -> str | None:
    """Derive the ATA via solders' native `Pubkey.find_program_address`.

    Returns None (never raises) if any input is not a valid base58-encoded
    32-byte pubkey -- mirrors `derive_ata()`'s ValueError-means-skip contract
    so `_try_derive_ata` can treat both backends identically.
    """
    try:
        wallet_pk = pubkey_cls.from_string(wallet_pubkey)  # type: ignore[attr-defined]
        token_pk = pubkey_cls.from_string(spl_token_program_id)  # type: ignore[attr-defined]
        mint_pk = pubkey_cls.from_string(mint)  # type: ignore[attr-defined]
        program_pk = pubkey_cls.from_string(ata_program_id)  # type: ignore[attr-defined]
    except Exception:
        return None
    ata_pk, _bump = pubkey_cls.find_program_address(  # type: ignore[attr-defined]
        [bytes(wallet_pk), bytes(token_pk), bytes(mint_pk)], program_pk
    )
    return str(ata_pk)


def _try_derive_ata(
    wallet_pubkey: str, mint: str, *, ata_program_id: str, spl_token_program_id: str
) -> str | None:
    """Derive the wallet's ATA for `mint`, or None if `mint` is not a valid 32-byte
    pubkey candidate. Prefers solders' native derivation (sub-ms); falls back to the
    pure-Python `derive_ata()` reference when solders is not importable. Both
    backends implement the IDENTICAL standard algorithm
    (`find_program_address([wallet, spl_token, mint], ata_program)`) so the
    returned value is the same regardless of which one ran.
    """
    pubkey_cls = _solders_pubkey_cls()
    if pubkey_cls is not None:
        return _derive_ata_native(
            pubkey_cls, wallet_pubkey, mint,
            ata_program_id=ata_program_id, spl_token_program_id=spl_token_program_id,
        )
    try:
        return derive_ata(
            wallet_pubkey, mint,
            ata_program_id=ata_program_id, spl_token_program_id=spl_token_program_id,
        )
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Boot-time policy loader -- config/signer-policy.json + config/program-allowlist.json
# ---------------------------------------------------------------------------

_DEFAULT_ALLOWLIST_PATH = "config/program-allowlist.json"
_DEFAULT_POLICY_PATH = "config/signer-policy.json"


def _resolve_repo_path(path: str) -> str:
    if os.path.isabs(path):
        return path
    repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(repo_root, path)


def load_signer_policy(
    *,
    allowlist_path: str | None = None,
    policy_path: str | None = None,
    pinned_tip_accounts: frozenset[str],
) -> SignerPolicy:
    """Boot-time loader: builds a SignerPolicy from the two independent config files.

    `pinned_tip_accounts` MUST be supplied by the caller (signer_process.py),
    sourced from a live `getTipAccounts` fetch at boot -- an EMPTY frozenset
    on fetch failure, per ADR-0015 C2 fail-closed rule ("the signer never
    widens the pin to recover"). This loader does NOT fetch network itself
    (no network access inside the enforcement module -- ADR-0015 trust model:
    the signer has no inbound network and this keeps it with no OUTBOUND
    network dependency in its core policy path either).

    NEVER imports aats.contracts.risk (ADR-0015 "independent_of_riskconfig").
    """
    allowlist_p = _resolve_repo_path(allowlist_path or os.environ.get(
        "SIGNER_ALLOWLIST_PATH", _DEFAULT_ALLOWLIST_PATH
    ))
    policy_p = _resolve_repo_path(policy_path or os.environ.get(
        "SIGNER_POLICY_PATH", _DEFAULT_POLICY_PATH
    ))

    with open(allowlist_p, encoding="utf-8") as fh:
        allowlist_doc = json.load(fh)
    with open(policy_p, encoding="utf-8") as fh:
        policy_doc = json.load(fh)

    live_statuses = set(allowlist_doc["live_set_policy"]["admit_only_status"])
    allowed: set[str] = set()
    venue: set[str] = set()
    by_name: dict[str, str] = {}
    system_program_id = ""
    ata_program_id = ""
    spl_token_program_id = ""
    compute_budget_program_id = ""
    for entry in allowlist_doc["allowed_programs"]:
        name = entry["name"]
        pid = entry["program_id"]
        by_name[name] = pid
        category = entry.get("category")
        status = entry.get("status")
        is_candidate_flag = bool(entry.get("candidate", False))
        if status is not None:
            # venue/router entries are gated by admit_only_status (active only).
            is_live = status in live_statuses
        elif is_candidate_flag:
            # core entries CAN be marked candidate-only too (e.g. spl_token_2022_program) --
            # least privilege: NOT admitted until the venue path actually emits it.
            is_live = False
        else:
            # core entries with neither "status" nor "candidate" are always live.
            is_live = True
        if not is_live:
            continue
        allowed.add(pid)
        if category == "venue":
            venue.add(pid)
        if name == "system_program":
            system_program_id = pid
        elif name == "associated_token_account_program":
            ata_program_id = pid
        elif name == "spl_token_program":
            spl_token_program_id = pid
        elif name == "compute_budget_program":
            compute_budget_program_id = pid

    if not system_program_id or not ata_program_id or not spl_token_program_id or not compute_budget_program_id:
        raise ValueError(
            "config/program-allowlist.json is missing a required core program "
            "(system_program / associated_token_account_program / spl_token_program / "
            "compute_budget_program) -- compute_budget_program_id is REQUIRED so C3 can "
            "price the SetComputeUnitPrice priority fee (fix round 1 MAJOR finding)."
        )

    decoders: dict[str, SpendDecoder] = {}
    for dec in policy_doc["venue_spend_decoders"]["decoders"]:
        allowlist_name = dec["allowlist_name"]
        pid = by_name.get(allowlist_name)
        if pid is None:
            # Unresolvable name -- nothing in venue_program_ids will ever match this
            # entry, so it is inert. Surfaced at CI build-verification time, not here
            # (fail-closed by omission: any venue ix for this name simply has no
            # decoder registered under its real program_id and refuses).
            continue
        if dec.get("status") == "unverified" or dec.get("discriminator_hex") == "VERIFY_AT_BUILD":
            decoders[pid] = _UnverifiedSpendDecoder(allowlist_name)
        else:
            # A future build transcribes real IDL bytes and extends this loader
            # with a real decoder implementation for `allowlist_name`.
            raise NotImplementedError(
                f"venue_spend_decoders['{allowlist_name}'] is marked verified but no real "
                "decoder implementation is registered in load_signer_policy() yet."
            )

    per_tx_cap = policy_doc["per_tx_cap"]["per_tx_cap_lamports"]
    agg = policy_doc["rolling_aggregate_velocity"]
    if per_tx_cap > 100_000_000:
        raise ValueError("per_tx_cap_lamports exceeds the OQ-005 floor of 100_000_000 (0.1 SOL).")
    if agg["aggregate_cap_lamports"] > 500_000_000:
        raise ValueError("aggregate_cap_lamports exceeds the OQ-005 floor of 500_000_000 (0.5 SOL).")

    return SignerPolicy(
        per_tx_cap_lamports=per_tx_cap,
        aggregate_cap_lamports=agg["aggregate_cap_lamports"],
        window_seconds=agg["window_seconds"],
        max_sign_count=agg["max_sign_count"],
        allowed_program_ids=frozenset(allowed),
        venue_program_ids=frozenset(venue),
        pinned_tip_accounts=frozenset(pinned_tip_accounts),
        ata_program_id=ata_program_id,
        spl_token_program_id=spl_token_program_id,
        venue_spend_decoders=decoders,
        system_program_id=system_program_id,
        compute_budget_program_id=compute_budget_program_id,
    )
