"""RPC client abstraction — injectable for offline testing.

MAINNET AND DEVNET ARE NOT REACHABLE IN THIS ENVIRONMENT.
The RPC / Jito clients are INJECTABLE so:
  - build/sign/simulate/dry-run is fully testable offline.
  - A deterministic MockRpcClient replaces the live RPC in tests.
  - The real SolanaRpcClient calls the live RPC only in LIVE mode.
  - The real DevnetRpcClient calls the Solana DEVNET for E1 validation mode.
  - MockDevnetRpcClient drives the full submit→confirm→reconcile path offline.

This module provides:
  - RpcClientProtocol        — the injectable interface
  - MockRpcClient            — deterministic offline mock (no network)
  - MockRpcClientRevert      — mock that forces simulate to return revert
  - MockRpcClientBlockhashExpiry — mock that fails first attempt (blockhash expiry)
  - MockDevnetRpcClient      — offline mock of devnet: submit→confirm→reconcile path
  - DevnetRpcClient          — real httpx-backed client for Solana DEVNET (RPC_DEVNET)
  - SolanaRpcClient          — real httpx-backed client (mainnet; not called in DRY_RUN)

All money in simulate results is integer lamports / CU counts (int), never float.
"""
from __future__ import annotations

import base64
import logging
import os
from decimal import Decimal
from typing import Any, Protocol

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Types returned by the RPC client
# ---------------------------------------------------------------------------


class SimulateResult:
    """Result of simulateTransaction.

    Attributes:
        success:       True if the tx would not revert on-chain.
        cu_consumed:   Compute units consumed (int); 0 if simulation failed.
        revert_reason: Human-readable revert reason or None on success.
        logs:          Program log lines (list of strings).
    """

    __slots__ = ("success", "cu_consumed", "revert_reason", "logs")

    def __init__(
        self,
        *,
        success: bool,
        cu_consumed: int,
        revert_reason: str | None = None,
        logs: list[str] | None = None,
    ) -> None:
        self.success = success
        self.cu_consumed = cu_consumed  # int, never float
        self.revert_reason = revert_reason
        self.logs: list[str] = logs or []


class QuoteResult:
    """A raw quote from the AMM or Jupiter.

    amount_in_base and amount_out_base are integer base units (NOT float).
    price is a Decimal-as-string.
    valid_until_slot is the last slot this quote is valid for.
    route_label describes the swap path (for logging and route freshness checks).
    price_impact_pct is a Decimal-as-string (surfaced to caller per mandate).
    """

    __slots__ = (
        "amount_in_base",
        "amount_out_base",
        "price",
        "valid_until_slot",
        "route_label",
        "price_impact_pct",
    )

    def __init__(
        self,
        *,
        amount_in_base: int,
        amount_out_base: int,
        price: str,
        valid_until_slot: int,
        route_label: str = "direct",
        price_impact_pct: str = "0",
    ) -> None:
        # Guard: no float money fields.
        if isinstance(amount_in_base, float) or isinstance(amount_out_base, float):
            raise TypeError(
                "QuoteResult.amount_in_base and amount_out_base must be int (base units), not float."
            )
        self.amount_in_base: int = amount_in_base
        self.amount_out_base: int = amount_out_base
        self.price: str = price  # Decimal-as-string
        self.valid_until_slot: int = valid_until_slot
        self.route_label: str = route_label
        self.price_impact_pct: str = price_impact_pct  # Decimal-as-string

    @property
    def price_impact(self) -> Decimal:
        return Decimal(self.price_impact_pct)


class SignatureStatus:
    """Result of a single signature's getSignatureStatuses lookup.

    found:     True if the RPC has ever seen this signature (processed/confirmed/
               finalized OR failed on-chain). False means "unknown to this node" --
               NOT proof the tx never landed (it may still be in flight), but the
               retry/phantom-land recheck (jito_jupiter_venue.py) treats an unfound
               signature as safe to retry.
    confirmed: True if confirmationStatus is "confirmed" or "finalized" AND err is None.
    slot:      the slot the tx landed in, or None.
    err:       the on-chain error object (None on success), or None if not found.
    """

    __slots__ = ("signature", "found", "confirmed", "slot", "err")

    def __init__(
        self,
        *,
        signature: str,
        found: bool,
        confirmed: bool,
        slot: int | None = None,
        err: Any = None,
    ) -> None:
        self.signature = signature
        self.found = found
        self.confirmed = confirmed
        self.slot = slot
        self.err = err


def extract_signature_b58(serialized_b64: str) -> str:
    """Best-effort LOCAL extraction of a signed tx's own (first) signature.

    A signed Solana transaction's first signature is fully determined by its
    own bytes -- no RPC round-trip is needed to know what signature a landed
    tx WOULD have. This lets the retry/resend path check "did the ORIGINAL
    attempt actually land?" via getSignatureStatuses even when send_transaction()
    itself failed/timed out client-side (so no signature ever came back in the
    RPC response) -- see jito_jupiter_venue.py's phantom-land recheck.

    When `solders` is available, decodes the real VersionedTransaction and
    returns the base58-encoded first signature. When solders is absent (as in
    this offline dev/test environment -- see tx_builder.py `_HAS_SOLDERS`),
    falls back to a deterministic (non-cryptographic) surrogate derived from a
    hash of the serialized bytes: STABLE for byte-identical resends, DIFFERENT
    after a fresh-blockhash rebuild. This surrogate is sufficient to dedupe
    "did I already send exactly this content" in an offline/test context; it
    is NOT a real Solana signature and MUST NOT be treated as one elsewhere.
    """
    try:
        import solders  # noqa: F401
        from solders.transaction import VersionedTransaction  # type: ignore

        raw = base64.b64decode(serialized_b64)
        tx = VersionedTransaction.from_bytes(raw)
        if tx.signatures:
            return str(tx.signatures[0])
    except Exception:
        pass
    import hashlib

    digest = hashlib.sha256(serialized_b64.encode()).hexdigest()
    return f"local-sig-{digest[:44]}"


def _get_signature_statuses_via_rpc(
    rpc_call: Any, signatures: list[str]
) -> list[SignatureStatus | None]:
    """Shared getSignatureStatuses implementation for SolanaRpcClient / DevnetRpcClient.

    `rpc_call` is each client's own `_rpc(method, params)` bound method.
    """
    if not signatures:
        return []
    result = rpc_call("getSignatureStatuses", [signatures, {"searchTransactionHistory": True}])
    values = result.get("value", []) if result else []
    out: list[SignatureStatus | None] = []
    for i, sig in enumerate(signatures):
        val = values[i] if i < len(values) else None
        if val is None:
            out.append(None)
            continue
        confirmation_status = val.get("confirmationStatus")
        confirmed = confirmation_status in ("confirmed", "finalized") and val.get("err") is None
        slot = val.get("slot")
        out.append(
            SignatureStatus(
                signature=sig,
                found=True,
                confirmed=confirmed,
                slot=int(slot) if slot is not None else None,
                err=val.get("err"),
            )
        )
    return out


class LandAttemptResult:
    """Result of submitting a transaction to the block engine.

    signature: the transaction signature string (base58) if landed, else None.
    submitted: True if the tx was actually sent to the network.
    land_slot: the slot in which the tx landed, or None.
    reason:    "landed" | "blockhash_expired" | "dry_run" | "node_lag" | ...
    """

    __slots__ = ("submitted", "signature", "land_slot", "reason")

    def __init__(
        self,
        *,
        submitted: bool,
        signature: str | None = None,
        land_slot: int | None = None,
        reason: str = "landed",
    ) -> None:
        self.submitted = submitted
        self.signature = signature
        self.land_slot = land_slot
        self.reason = reason


# ---------------------------------------------------------------------------
# Injectable interface protocol
# ---------------------------------------------------------------------------


class RpcClientProtocol(Protocol):
    """Injectable RPC interface — the only surface the venue talks to."""

    def get_latest_blockhash(self) -> str:
        """Return the latest blockhash as a base58 string."""
        ...

    def get_current_slot(self) -> int:
        """Return the current cluster slot (int)."""
        ...

    def simulate_transaction(self, signed_tx_b64: str) -> SimulateResult:
        """Simulate the transaction. Returns SimulateResult (never raises on revert)."""
        ...

    def send_transaction(self, signed_tx_b64: str) -> LandAttemptResult:
        """Broadcast the transaction. LIVE path only — DRY_RUN must never call this."""
        ...

    def get_account_info(self, pubkey: str) -> dict[str, Any] | None:
        """Return account info dict or None if account does not exist."""
        ...

    def get_signature_statuses(self, signatures: list[str]) -> list[SignatureStatus | None]:
        """Poll getSignatureStatuses for the given signatures (batch, order-preserving).

        One entry per input signature; None = the RPC has never seen this
        signature. Used to RE-CHECK the ORIGINAL signature's on-chain status
        before any resend, so a transient client-side send failure never
        causes a double-land of a tx that actually landed (the "phantom
        landed" guard — jito_jupiter_venue.py `_recheck_signature_before_resend`).
        """
        ...


# ---------------------------------------------------------------------------
# Mock RPC client — deterministic, no network, for offline tests
# ---------------------------------------------------------------------------

_MOCK_SLOT_BASE = 300_000_000  # realistic-looking slot number


class MockRpcClient:
    """Deterministic offline mock of the Solana RPC.

    All amounts are integer. simulate always succeeds with a fixed CU count.
    send_transaction raises if called (confirms no real submit in DRY_RUN tests).
    """

    def __init__(
        self,
        current_slot: int = _MOCK_SLOT_BASE,
        cu_consumed: int = 180_000,
        blockhash: str = "MockBlockhash11111111111111111111111111111111",
    ) -> None:
        self._slot = current_slot
        self._cu_consumed = cu_consumed
        self._blockhash = blockhash
        # Track how many times simulate/send were called (for test assertions).
        self.simulate_calls: int = 0
        self.send_calls: int = 0
        self.signature_status_calls: int = 0
        # signature -> land_slot, populated by mark_signature_landed() (test-only
        # helper) to simulate "this signature actually landed on-chain".
        self._landed_signatures: dict[str, int] = {}

    def get_latest_blockhash(self) -> str:
        return self._blockhash

    def get_current_slot(self) -> int:
        return self._slot

    def mark_signature_landed(self, signature: str, *, slot: int | None = None) -> None:
        """TEST-ONLY: simulate that `signature` actually landed on-chain, so a
        subsequent get_signature_statuses([signature]) call reports it confirmed.
        Used to construct "phantom landed" scenarios (a client-perceived send
        failure whose tx actually landed anyway)."""
        self._landed_signatures[signature] = slot if slot is not None else self._slot

    def get_signature_statuses(self, signatures: list[str]) -> list[SignatureStatus | None]:
        self.signature_status_calls += 1
        out: list[SignatureStatus | None] = []
        for sig in signatures:
            if sig in self._landed_signatures:
                out.append(
                    SignatureStatus(
                        signature=sig,
                        found=True,
                        confirmed=True,
                        slot=self._landed_signatures[sig],
                        err=None,
                    )
                )
            else:
                out.append(None)
        return out

    def simulate_transaction(self, signed_tx_b64: str) -> SimulateResult:
        self.simulate_calls += 1
        return SimulateResult(
            success=True,
            cu_consumed=self._cu_consumed,
            revert_reason=None,
            logs=["Program log: mock_simulate_success"],
        )

    def send_transaction(self, signed_tx_b64: str) -> LandAttemptResult:
        # In tests, this should NEVER be called when DRY_RUN_ENABLED=true.
        # If it is called, we record it (the test then asserts send_calls == 0).
        self.send_calls += 1
        # Return a plausible-looking signature so calling code doesn't crash,
        # but the test can assert send_calls == 0.
        return LandAttemptResult(
            submitted=True,
            signature="MockSig1111111111111111111111111111111111111111111111111",
            land_slot=self._slot + 2,
            reason="landed",
        )

    def get_account_info(self, pubkey: str) -> dict[str, Any] | None:
        return {"executable": True, "owner": "BPFLoader", "lamports": 1_000_000}


class MockRpcClientRevert(MockRpcClient):
    """Mock RPC that forces simulate to return a revert.

    Used to prove that a reverting tx is caught BEFORE any submit is attempted.
    """

    def __init__(self, revert_reason: str = "mock_program_error", **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._revert_reason = revert_reason

    def simulate_transaction(self, signed_tx_b64: str) -> SimulateResult:
        self.simulate_calls += 1
        return SimulateResult(
            success=False,
            cu_consumed=0,
            revert_reason=self._revert_reason,
            logs=[f"Program log: Error: {self._revert_reason}"],
        )

    def send_transaction(self, signed_tx_b64: str) -> LandAttemptResult:
        # MUST NOT be called after a simulate revert.
        self.send_calls += 1
        raise AssertionError(
            "send_transaction() called after simulate returned a revert — "
            "this is a bug: pre-send simulation MUST prevent any submit on revert."
        )


class MockRpcClientBlockhashExpiry(MockRpcClient):
    """Mock RPC that simulates blockhash expiry on the first send, then succeeds.

    Used to prove the retry/re-quote path: blockhash expiry triggers a fresh
    blockhash fetch, rebuild, re-sign, and re-simulate before re-sending.

    The blockhash returned by get_latest_blockhash() changes on each call so
    tests can assert the retry used a DIFFERENT blockhash (not byte-identical bytes).
    The sent_bytes list records every serialized_b64 passed to send_transaction()
    so tests can assert the two sends carried different content.
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._attempt = 0
        self._blockhash_call_count = 0
        self.blockhash_expiry_count = 0
        # Track every serialized_b64 passed to send_transaction for byte-equality checks.
        self.sent_bytes: list[str] = []
        # Track every blockhash returned by get_latest_blockhash.
        self.blockhashes_issued: list[str] = []

    def get_latest_blockhash(self) -> str:
        """Return a unique blockhash per call so retry bytes differ from attempt 1."""
        self._blockhash_call_count += 1
        bh = f"FreshBlockhash{self._blockhash_call_count:04d}11111111111111111111111111111"
        self.blockhashes_issued.append(bh)
        return bh

    def send_transaction(self, signed_tx_b64: str) -> LandAttemptResult:
        self.send_calls += 1
        self._attempt += 1
        self.sent_bytes.append(signed_tx_b64)
        if self._attempt == 1:
            self.blockhash_expiry_count += 1
            return LandAttemptResult(
                submitted=False,
                signature=None,
                land_slot=None,
                reason="blockhash_expired",
            )
        # Second attempt succeeds.
        return LandAttemptResult(
            submitted=True,
            signature="RetryLandedSig111111111111111111111111111111111111111",
            land_slot=self._slot + 3,
            reason="landed",
        )


class MockRpcClientPhantomLand(MockRpcClient):
    """Mock RPC that reports a client-side send FAILURE whose tx ACTUALLY landed anyway.

    Models the real-world "phantom landed" hazard: sendTransaction times out /
    errors on the client side, but the cluster processed the tx regardless.
    Proves the retry loop's getSignatureStatuses recheck (`extract_signature_b58`
    + `get_signature_statuses`) catches this BEFORE blindly rebuilding-and-resending
    with a fresh blockhash (which would risk landing BOTH the original and the
    retry — a double-land). A correct caller must see send_calls == 1: the
    recheck must short-circuit the retry, not merely detect the phantom land
    after also resending.
    """

    def __init__(self, *, land_slot: int | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._phantom_land_slot = land_slot if land_slot is not None else self._slot + 1

    def send_transaction(self, signed_tx_b64: str) -> LandAttemptResult:
        self.send_calls += 1
        # Simulate the cluster actually processing this exact tx despite the
        # client reporting a transient failure below: register its local
        # signature as landed BEFORE returning the (client-perceived) failure.
        local_sig = extract_signature_b58(signed_tx_b64)
        self.mark_signature_landed(local_sig, slot=self._phantom_land_slot)
        return LandAttemptResult(
            submitted=False,
            signature=None,
            land_slot=None,
            reason="node_lag",
        )


# ---------------------------------------------------------------------------
# MockDevnetRpcClient — offline mock of the devnet submit→confirm→reconcile path
# ---------------------------------------------------------------------------


class MockDevnetRpcClient(MockRpcClient):
    """Offline mock of the Solana DEVNET RPC for E1 validation testing.

    Exercises the FULL submit→land→confirm→reconcile path without any network
    call.  Proves that the devnet code path (send_transaction → poll
    getSignatureStatuses → reconcile fill) works end-to-end before a real
    devnet endpoint is plugged in.

    Behaviour:
      - simulate_transaction: succeeds (or reverts if forced).
      - send_transaction:      returns a mock devnet signature on success.
      - confirm_transaction:   polls until the mock confirms the tx (1 poll).
      - All money fields remain int; no float.

    Where the real devnet endpoint plugs in:
      Replace this mock with DevnetRpcClient(rpc_url=os.environ["RPC_DEVNET"])
      and set SOLANA_CLUSTER=devnet in the environment.  The JitoJupiterVenue
      constructor reads SOLANA_CLUSTER and selects the devnet client.
    """

    DEVNET_MOCK_SIGNATURE_PREFIX = "DevnetMockSig"

    def __init__(
        self,
        *,
        current_slot: int = _MOCK_SLOT_BASE,
        cu_consumed: int = 180_000,
        blockhash: str = "DevnetBlockhash1111111111111111111111111111",
        confirm_after_polls: int = 1,
        land_succeeds: bool = True,
        fail_reason: str = "blockhash_expired",
    ) -> None:
        """
        Args:
            confirm_after_polls: number of getSignatureStatuses polls before confirming.
            land_succeeds:       if False, send_transaction fails (simulates failed-land).
            fail_reason:         reason returned when land_succeeds=False.
        """
        super().__init__(
            current_slot=current_slot,
            cu_consumed=cu_consumed,
            blockhash=blockhash,
        )
        self._confirm_after_polls = confirm_after_polls
        self._land_succeeds = land_succeeds
        self._fail_reason = fail_reason
        self._poll_count: dict[str, int] = {}  # sig → poll calls so far
        # Records of every submitted signature (for test assertions).
        self.submitted_signatures: list[str] = []
        self.confirm_calls: int = 0

    def send_transaction(self, signed_tx_b64: str) -> LandAttemptResult:
        """Mock devnet send.  Returns a devnet signature on success."""
        self.send_calls += 1
        if not self._land_succeeds:
            return LandAttemptResult(
                submitted=False,
                signature=None,
                land_slot=None,
                reason=self._fail_reason,
            )
        sig = f"{self.DEVNET_MOCK_SIGNATURE_PREFIX}{self.send_calls:04d}1111111111111111111111111111111111111111"
        self.submitted_signatures.append(sig)
        self._poll_count[sig] = 0
        return LandAttemptResult(
            submitted=True,
            signature=sig,
            land_slot=None,  # not yet confirmed; use confirm_transaction to get slot
            reason="submitted_pending_confirm",
        )

    def confirm_transaction(
        self,
        signature: str,
        *,
        max_polls: int = 30,
        poll_interval_s: float = 0.5,
    ) -> ConfirmResult:
        """Poll for confirmation.  Returns ConfirmResult after confirm_after_polls polls.

        In production this maps to repeated getSignatureStatuses calls until
        the tx reaches 'confirmed' or 'finalized' commitment.  The mock resolves
        after confirm_after_polls calls so tests can prove the polling loop fires.

        max_polls and poll_interval_s are accepted (matching DevnetRpcClient's signature)
        but ignored — the mock uses self._confirm_after_polls, not wall-clock time.
        """
        self.confirm_calls += 1
        if signature not in self._poll_count:
            return ConfirmResult(
                confirmed=False,
                land_slot=None,
                error="signature_not_found",
                polls=0,
            )
        self._poll_count[signature] += 1
        confirmed = self._poll_count[signature] >= self._confirm_after_polls
        return ConfirmResult(
            confirmed=confirmed,
            land_slot=self._slot + 2 if confirmed else None,
            error=None,
            polls=self._poll_count[signature],
        )

    def get_account_info(self, pubkey: str) -> dict[str, Any] | None:
        return {"executable": True, "owner": "BPFLoader", "lamports": 1_000_000}


class ConfirmResult:
    """Result of a confirmation poll (getSignatureStatuses).

    confirmed:  True if the tx has reached 'confirmed' commitment.
    land_slot:  The slot the tx landed in (set when confirmed=True).
    error:      Error string or None.
    polls:      How many times confirm_transaction was called for this sig.
    """

    __slots__ = ("confirmed", "land_slot", "error", "polls")

    def __init__(
        self,
        *,
        confirmed: bool,
        land_slot: int | None = None,
        error: str | None = None,
        polls: int = 0,
    ) -> None:
        self.confirmed = confirmed
        self.land_slot = land_slot
        self.error = error
        self.polls = polls


# ---------------------------------------------------------------------------
# Real DevnetRpcClient — Solana DEVNET, NOT mainnet (E1 validation mode)
# ---------------------------------------------------------------------------


class DevnetRpcClient:
    """Production Solana JSON-RPC client wired to DEVNET (E1 — worthless SOL).

    Devnet is a separate Solana cluster from mainnet.  It is used for
    VALIDATING the real submit→land→confirm→reconcile code path without
    touching real capital.

    DEVNET IS NOT REACHABLE IN THIS OFFLINE ENVIRONMENT.  To wire the real
    devnet endpoint, set RPC_DEVNET in the environment and inject this client:

        from aats.execution.rpc_client import DevnetRpcClient
        client = DevnetRpcClient(rpc_url=os.environ["RPC_DEVNET"])

    Then pass it to JitoJupiterVenue via the rpc_client argument (or let the
    venue select it automatically when SOLANA_CLUSTER=devnet).

    ALL money fields remain integer (lamports / base units) — never float.
    """

    #: The public devnet RPC endpoint (no API key required for rate-limited use).
    #: Replace with a premium devnet endpoint for production E1 validation.
    DEVNET_PUBLIC_RPC = "https://api.devnet.solana.com"

    def __init__(self, rpc_url: str | None = None) -> None:
        self._url = rpc_url or os.environ.get("RPC_DEVNET", "")
        if not self._url:
            raise ValueError(
                "DevnetRpcClient: RPC_DEVNET env var not set. "
                "Provide an rpc_url or set RPC_DEVNET to a Solana devnet endpoint. "
                f"Public fallback (rate-limited): {self.DEVNET_PUBLIC_RPC}"
            )
        try:
            import httpx  # noqa: F401
        except ImportError as exc:
            raise ImportError(
                "httpx is required for DevnetRpcClient. Install it: pip install httpx"
            ) from exc
        self._http = None  # lazy-initialized

    def _client(self):  # type: ignore[return]
        import httpx

        if self._http is None:
            self._http = httpx.Client(timeout=10.0)
        return self._http

    def _rpc(self, method: str, params: list[Any]) -> Any:
        import httpx

        body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        try:
            r = self._client().post(self._url, json=body)
            r.raise_for_status()
            data = r.json()
        except httpx.HTTPError as exc:
            raise OSError(f"Devnet RPC call {method} failed: {exc}") from exc
        if "error" in data:
            raise OSError(f"Devnet RPC error {method}: {data['error']}")
        return data.get("result")

    def get_latest_blockhash(self) -> str:
        result = self._rpc("getLatestBlockhash", [{"commitment": "confirmed"}])
        return result["value"]["blockhash"]

    def get_current_slot(self) -> int:
        return int(self._rpc("getSlot", [{"commitment": "confirmed"}]))

    def simulate_transaction(self, signed_tx_b64: str) -> SimulateResult:
        params = [
            signed_tx_b64,
            {
                "encoding": "base64",
                "commitment": "confirmed",
                "sigVerify": False,
                "replaceRecentBlockhash": True,
            },
        ]
        result = self._rpc("simulateTransaction", params)
        value = result["value"]
        success = value.get("err") is None
        cu = int(value.get("unitsConsumed", 0))
        logs = value.get("logs") or []
        revert_reason: str | None = None
        if not success:
            err = value.get("err")
            revert_reason = str(err) if err else "unknown_revert"
        return SimulateResult(
            success=success,
            cu_consumed=cu,
            revert_reason=revert_reason,
            logs=logs,
        )

    def send_transaction(self, signed_tx_b64: str) -> LandAttemptResult:
        """Broadcast to DEVNET.  Uses 'confirmed' preflight — devnet only.

        NOTE: Devnet is a separate cluster.  This NEVER touches mainnet or
        real capital.  The worthless SOL on devnet has no monetary value.
        """
        params = [
            signed_tx_b64,
            {
                "encoding": "base64",
                "preflightCommitment": "confirmed",
                # skipPreflight=False: devnet preflight is fine (not latency-sensitive)
                "skipPreflight": False,
            },
        ]
        try:
            sig = self._rpc("sendTransaction", params)
            return LandAttemptResult(
                submitted=True,
                signature=str(sig),
                land_slot=None,  # poll via confirm_transaction
                reason="submitted_pending_confirm",
            )
        except OSError as exc:
            reason = "blockhash_expired" if "Blockhash not found" in str(exc) else "node_lag"
            return LandAttemptResult(
                submitted=False,
                signature=None,
                land_slot=None,
                reason=reason,
            )

    def confirm_transaction(
        self,
        signature: str,
        *,
        commitment: str = "confirmed",
        max_polls: int = 30,
        poll_interval_s: float = 0.5,
    ) -> ConfirmResult:
        """Poll getSignatureStatuses until confirmed or max_polls exhausted.

        In production E1 validation, this drives the confirm loop.  The mock
        (MockDevnetRpcClient) replaces this for offline testing.
        """
        import time

        for poll in range(1, max_polls + 1):
            result = self._rpc("getSignatureStatuses", [[signature], {"searchTransactionHistory": True}])
            statuses = result.get("value", [])
            status = statuses[0] if statuses else None
            if status is not None and status.get("confirmationStatus") in (commitment, "finalized"):
                slot = status.get("slot")
                return ConfirmResult(
                    confirmed=True,
                    land_slot=int(slot) if slot is not None else None,
                    error=status.get("err"),
                    polls=poll,
                )
            time.sleep(poll_interval_s)
        return ConfirmResult(
            confirmed=False,
            land_slot=None,
            error="confirmation_timeout",
            polls=max_polls,
        )

    def get_account_info(self, pubkey: str) -> dict[str, Any] | None:
        result = self._rpc("getAccountInfo", [pubkey, {"encoding": "base58"}])
        if result is None or result.get("value") is None:
            return None
        return dict(result["value"])

    def get_signature_statuses(self, signatures: list[str]) -> list[SignatureStatus | None]:
        """Batch getSignatureStatuses -- the phantom-land / original-signature recheck primitive."""
        return _get_signature_statuses_via_rpc(self._rpc, signatures)


# ---------------------------------------------------------------------------
# Real SolanaRpcClient — mainnet-bound, NOT called in DRY_RUN
# ---------------------------------------------------------------------------


class SolanaRpcClient:
    """Production Solana JSON-RPC client backed by httpx.

    Mainnet is NOT reachable in this test environment — this class is injected
    only when DRY_RUN_ENABLED=false AND the loop is in LIVE mode.

    ALL money fields remain integer (lamports / base units) — the JSON-RPC
    returns them as integers and we never cast to float.
    """

    def __init__(self, rpc_url: str | None = None) -> None:
        self._url = rpc_url or os.environ.get("RPC_PRIMARY", "")
        if not self._url:
            raise ValueError(
                "SolanaRpcClient: RPC_PRIMARY env var not set. "
                "Provide an rpc_url or set RPC_PRIMARY."
            )
        # Import httpx lazily so offline tests don't require it on the hot path.
        try:
            import httpx  # noqa: F401
        except ImportError as exc:
            raise ImportError(
                "httpx is required for SolanaRpcClient. Install it: pip install httpx"
            ) from exc
        self._http = None  # lazy-initialized

    def _client(self):  # type: ignore[return]
        import httpx

        if self._http is None:
            self._http = httpx.Client(timeout=5.0)
        return self._http

    def _rpc(self, method: str, params: list[Any]) -> Any:
        import httpx

        body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        try:
            r = self._client().post(self._url, json=body)
            r.raise_for_status()
            data = r.json()
        except httpx.HTTPError as exc:
            raise OSError(f"RPC call {method} failed: {exc}") from exc
        if "error" in data:
            raise OSError(f"RPC error {method}: {data['error']}")
        return data.get("result")

    def get_latest_blockhash(self) -> str:
        result = self._rpc("getLatestBlockhash", [{"commitment": "confirmed"}])
        return result["value"]["blockhash"]

    def get_current_slot(self) -> int:
        return int(self._rpc("getSlot", [{"commitment": "confirmed"}]))

    def simulate_transaction(self, signed_tx_b64: str) -> SimulateResult:
        params = [
            signed_tx_b64,
            {
                "encoding": "base64",
                "commitment": "confirmed",
                "sigVerify": False,  # Skip sig verify — tx not yet signed for real
                "replaceRecentBlockhash": True,
            },
        ]
        result = self._rpc("simulateTransaction", params)
        value = result["value"]
        success = value.get("err") is None
        cu = int(value.get("unitsConsumed", 0))
        logs = value.get("logs") or []
        revert_reason: str | None = None
        if not success:
            err = value.get("err")
            revert_reason = str(err) if err else "unknown_revert"
        return SimulateResult(
            success=success,
            cu_consumed=cu,
            revert_reason=revert_reason,
            logs=logs,
        )

    def send_transaction(self, signed_tx_b64: str) -> LandAttemptResult:
        params = [
            signed_tx_b64,
            {"encoding": "base64", "preflightCommitment": "confirmed"},
        ]
        try:
            sig = self._rpc("sendTransaction", params)
            return LandAttemptResult(
                submitted=True,
                signature=str(sig),
                land_slot=None,  # Confirmed async via getSignatureStatuses
                reason="landed",
            )
        except OSError as exc:
            reason = "blockhash_expired" if "Blockhash not found" in str(exc) else "node_lag"
            return LandAttemptResult(
                submitted=False,
                signature=None,
                land_slot=None,
                reason=reason,
            )

    def get_account_info(self, pubkey: str) -> dict[str, Any] | None:
        result = self._rpc("getAccountInfo", [pubkey, {"encoding": "base58"}])
        if result is None or result.get("value") is None:
            return None
        return dict(result["value"])

    def get_signature_statuses(self, signatures: list[str]) -> list[SignatureStatus | None]:
        """Batch getSignatureStatuses -- the phantom-land / original-signature recheck primitive
        used by jito_jupiter_venue.py before any blockhash-expiry retry resend."""
        return _get_signature_statuses_via_rpc(self._rpc, signatures)
