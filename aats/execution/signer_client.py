"""Signer client — the thin bridge from venue.sign() to the aats-signer process.

Architecture: ADR-0009 — sign() CROSSES A PROCESS BOUNDARY.
The hot-core venue holds ONLY the PUBKEY. The key lives in aats-signer.

Protocol:
  1. Serialize the UnsignedTx bytes as a length-prefixed message.
  2. Send over a local Unix-domain socket (SIGNER_SOCKET_PATH env var).
  3. Receive signed bytes (or a SignerRefused error payload).
  4. Reconstruct SignedTx; never log the raw bytes.

On a live mainnet, this is a loopback socket call budgeted at ≤ 1.5 ms p99
(latency-budget.md hop 5). The socket is file-permission and peer-credential
gated (T-352a / latency-devops-engineer).

In DRY_RUN, we still call through sign() for latency measurement — that is
intentional per execution-venue.md §1: "land() performs everything up to
and including sign() (for latency measurement)."

INJECTABLE: the RPC / signer path is injected so offline tests can use the
MockSignerClient (below) without needing a live socket.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import socket
import struct
import time
from typing import Protocol

from aats.contracts.venue import SignedTx, UnsignedTx
from aats.execution.exceptions import SignerRefused

logger = logging.getLogger(__name__)

# Never log tx bytes or pubkeys at DEBUG level in production — set to WARNING.
_LOG_LEVEL = logging.WARNING


class SignerClientProtocol(Protocol):
    """The signer client interface — injectable for offline/dry-run testing."""

    def sign(self, unsigned_tx: UnsignedTx, wallet_id: str) -> SignedTx:
        """Call aats-signer; returns signed bytes or raises SignerRefused."""
        ...

    @property
    def signer_pubkey(self) -> str:
        """The wallet pubkey this signer controls (NOT the secret key)."""
        ...


# ---------------------------------------------------------------------------
# Mock signer — deterministic, offline, never holds a real key
# ---------------------------------------------------------------------------

class MockSignerClient:
    """Deterministic offline signer for tests and DRY_RUN simulations.

    NEVER holds a private key. Signs with a predictable mock pubkey so tests
    can assert on the signer_pubkey without touching a real Solana keypair.

    The mock returns a base64-encoded deterministic signature that includes
    the client_intent_id so tests can verify per-intent idempotency.
    """

    def __init__(self, wallet_pubkey: str = "MockPubkey11111111111111111111111111111111") -> None:
        # PUBKEY only — the secret never enters this process.
        self._pubkey = wallet_pubkey

    @property
    def signer_pubkey(self) -> str:
        return self._pubkey

    def sign(self, unsigned_tx: UnsignedTx, wallet_id: str) -> SignedTx:
        """Return a mock-signed tx. No key, no network, no signer process call.

        The signed_b64 encodes both the intent_id AND the unsigned tx bytes so
        that a rebuild with a different blockhash (different unsigned_tx.serialized_b64)
        produces genuinely different signed bytes.  This lets tests assert that a
        blockhash-expiry retry actually rebuilt the tx (not re-sent byte-identical bytes).
        """
        # Include unsigned_tx.serialized_b64 so signed output differs when the
        # underlying tx bytes change (e.g. different blockhash after retry rebuild).
        mock_sig_payload = (
            f"mock_sig:{unsigned_tx.client_intent_id}:{wallet_id}:"
            f"{unsigned_tx.serialized_b64}"
        )
        signed_b64 = base64.b64encode(mock_sig_payload.encode()).decode()

        logger.debug(
            "mock_signer.sign",
            extra={"intent_id": unsigned_tx.client_intent_id, "wallet_id": wallet_id},
        )
        return SignedTx(
            serialized_b64=signed_b64,
            signer_pubkey=self._pubkey,
            intent_kind=unsigned_tx.intent_kind,
            mint=unsigned_tx.mint,
            client_intent_id=unsigned_tx.client_intent_id,
        )


class MockSignerClientRefusing(MockSignerClient):
    """A mock signer that always refuses — used in pre-send simulation tests."""

    def __init__(
        self,
        reason: str = "signer_per_tx_cap_exceeded",
        wallet_pubkey: str = "MockPubkey11111111111111111111111111111111",
    ) -> None:
        super().__init__(wallet_pubkey=wallet_pubkey)
        self._reason = reason

    def sign(self, unsigned_tx: UnsignedTx, wallet_id: str) -> SignedTx:
        raise SignerRefused(
            reason=self._reason,
            message=f"Signer refused (test mock): {self._reason} for intent {unsigned_tx.client_intent_id}",
        )


# ---------------------------------------------------------------------------
# Real signer client — calls aats-signer over a Unix-domain socket
# ---------------------------------------------------------------------------

_SOCKET_PATH_ENV = "SIGNER_SOCKET_PATH"
_DEFAULT_SOCKET_PATH = "/run/aats/signer.sock"

# Wire protocol:
#   Request:  4-byte big-endian length prefix + JSON payload
#   Response: 4-byte big-endian length prefix + JSON payload
#   JSON request: {"wallet_id": str, "tx_b64": str, "intent_id": str}
#   JSON response on success: {"signed_b64": str, "pubkey": str}
#   JSON response on refusal: {"error": str, "reason": str}
_HEADER_FMT = "!I"  # 4-byte unsigned int, big-endian
_HEADER_SIZE = struct.calcsize(_HEADER_FMT)

# Fix round 2 MAJOR finding: 3ms was budgeted for the DEFERRED Rust signer
# (ADR-0009's 1.5ms p99 hop budget). ADR-0015 (the go-live Python signer) is explicit
# that the Python signer adds "a few ms, not sub-ms" -- and this file shipped
# unchanged, still hard-coded to the Rust-era 3ms, which the code review flagged as
# an unreconciled cross-lane mismatch: every LIVE sign would fail closed
# (signer_unavailable) against the ACTUAL Python signer's latency.
#
# Measured (this sandbox, `solders` NOT installed -- see signer_enforcer.py's
# `_try_derive_ata`; pure-Python PDA-search fallback path, the WORST case) on a
# realistic 17-key tx carrying an own-ATA transfer, order of magnitude across
# repeated runs (exact numbers vary with host load -- see the live test output):
#   first call on a cold Enforcer (no ATA-cache warmth yet): low tens of ms typical,
#     occasional spikes to ~70ms.
#   repeat calls with the SAME wallet/mint but a NEW blockhash (the blockhash-expiry
#   retry pattern -- ADR-0015 memoizes per (wallet, mint), see signer_enforcer.py fix
#   round 2 MAJOR finding note): sub-ms to a few ms typical, occasional double-digit-ms
#     outlier (scheduler/GC jitter, not a systematic cost).
# (see tests/execution/test_signer_client_timeout.py::test_measured_enforcer_latency_
# is_recorded_for_the_timeout_default, which reproduces this benchmark on every run,
# prints the CURRENT numbers, and asserts a generous regression bound). `solders` is a
# PINNED production dependency (pyproject.toml) whose native PDA
# search is expected to keep production latency in ADR-0015's stated "a few ms" range
# even on a cold cache -- but `solders` is not installed in this offline dev sandbox
# (ModuleNotFoundError), so that expectation is NOT independently verified here.
#
# The default below is deliberately conservative given that gap: generous headroom
# over the WARM-cache pure-Python numbers above, but explicitly documented as NOT a
# substitute for a real measurement against the deployed signer (solders installed,
# real hardware). REQUIRED PRECONDITION before any devnet/LIVE wiring (T-327/T-352a):
# measure the deployed signer's actual p99 sign latency and set SIGNER_CLIENT_TIMEOUT_S
# from it (with headroom), not from this sandbox's number.
_DEFAULT_SOCKET_TIMEOUT_S = 0.05  # 50 ms
_SOCKET_TIMEOUT_ENV = "SIGNER_CLIENT_TIMEOUT_S"

# Retained for backward compatibility with any code/tests referencing the old name;
# now equal to the new documented default (see _DEFAULT_SOCKET_TIMEOUT_S above).
_SOCKET_TIMEOUT_S = _DEFAULT_SOCKET_TIMEOUT_S


def _resolve_default_timeout_s() -> float:
    """Read SIGNER_CLIENT_TIMEOUT_S at call time (not at import time) so a deployment
    can set it from its OWN measured signer p99 without a code change. Falls back to
    the conservative default on any missing/malformed value."""
    raw = os.environ.get(_SOCKET_TIMEOUT_ENV)
    if raw is None:
        return _DEFAULT_SOCKET_TIMEOUT_S
    try:
        value = float(raw)
    except ValueError:
        logger.warning(
            "signer_client.invalid_timeout_env",
            extra={"raw_value": raw, "falling_back_to": _DEFAULT_SOCKET_TIMEOUT_S},
        )
        return _DEFAULT_SOCKET_TIMEOUT_S
    if value <= 0:
        logger.warning(
            "signer_client.non_positive_timeout_env",
            extra={"raw_value": raw, "falling_back_to": _DEFAULT_SOCKET_TIMEOUT_S},
        )
        return _DEFAULT_SOCKET_TIMEOUT_S
    return value


class SocketSignerClient:
    """Production signer client — calls aats-signer over a Unix-domain socket.

    Mainnet is NOT reachable in this environment; this class is injectable
    and is replaced by MockSignerClient in offline/test contexts.

    The socket path is read from SIGNER_SOCKET_PATH env var at construction time.
    The socket timeout is read from SIGNER_CLIENT_TIMEOUT_S env var (or the
    `timeout_s` constructor arg, which takes precedence) — see the module-level
    "Fix round 2 MAJOR finding" note above for why this must be set from a REAL
    measured signer p99 before any devnet/LIVE wiring.
    """

    def __init__(
        self,
        socket_path: str | None = None,
        timeout_s: float | None = None,
    ) -> None:
        self._socket_path = socket_path or os.environ.get(
            _SOCKET_PATH_ENV, _DEFAULT_SOCKET_PATH
        )
        self._timeout_s = timeout_s if timeout_s is not None else _resolve_default_timeout_s()
        # Cached pubkey — fetched on first sign() call or at construction if reachable.
        self._pubkey: str | None = None

    @property
    def signer_pubkey(self) -> str:
        if self._pubkey is None:
            # Fallback: read from env var (set during deployment alongside socket path).
            pub = os.environ.get("WALLET_PUBKEY", "")
            if not pub:
                raise RuntimeError(
                    "WALLET_PUBKEY env var not set and signer pubkey not yet fetched. "
                    "The hot core holds ONLY the pubkey (ADR-0009)."
                )
            self._pubkey = pub
        return self._pubkey

    def sign(self, unsigned_tx: UnsignedTx, wallet_id: str) -> SignedTx:
        """Call aats-signer over the Unix-domain socket. Raises SignerRefused on policy violation."""
        payload = json.dumps(
            {
                "wallet_id": wallet_id,
                "tx_b64": unsigned_tx.serialized_b64,
                "intent_id": unsigned_tx.client_intent_id,
                "intent_kind": unsigned_tx.intent_kind,
                "mint": unsigned_tx.mint,
            }
        ).encode()

        t0 = time.monotonic()
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(self._timeout_s)
                sock.connect(self._socket_path)

                # Send length-prefixed payload.
                header = struct.pack(_HEADER_FMT, len(payload))
                sock.sendall(header + payload)

                # Read response header.
                resp_header = self._recv_exact(sock, _HEADER_SIZE)
                (resp_len,) = struct.unpack(_HEADER_FMT, resp_header)
                resp_body = self._recv_exact(sock, resp_len)

        except OSError as exc:
            # Socket not available — signer process not running.
            raise SignerRefused(
                reason="signer_unavailable",
                message=f"aats-signer socket unreachable: {exc}",
            ) from exc

        elapsed_ms = (time.monotonic() - t0) * 1000
        logger.log(
            _LOG_LEVEL,
            "signer_call",
            extra={"elapsed_ms": round(elapsed_ms, 2), "intent_id": unsigned_tx.client_intent_id},
        )

        response = json.loads(resp_body)
        if "error" in response:
            raise SignerRefused(
                reason=response.get("reason", "signer_error"),
                message=response["error"],
            )

        # Cache the pubkey from the first successful response.
        if self._pubkey is None and "pubkey" in response:
            self._pubkey = response["pubkey"]

        return SignedTx(
            serialized_b64=response["signed_b64"],
            signer_pubkey=response.get("pubkey", self.signer_pubkey),
            intent_kind=unsigned_tx.intent_kind,
            mint=unsigned_tx.mint,
            client_intent_id=unsigned_tx.client_intent_id,
        )

    @staticmethod
    def _recv_exact(sock: socket.socket, n: int) -> bytes:
        """Read exactly n bytes from the socket."""
        buf = bytearray()
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                raise OSError("aats-signer closed connection prematurely")
            buf.extend(chunk)
        return bytes(buf)
