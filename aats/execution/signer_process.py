"""aats-signer -- the isolated Python signer process (ADR-0009 topology, ADR-0015 language + contract).

Holds the wallet SECRET. Exposes ONLY a loopback Unix-domain socket. NO
inbound network beyond that socket, no untrusted-byte decoding beyond the one
tx it must parse to police it (ADR-0009 trust model). Runs
`signer_enforcer.Enforcer.enforce()` on EVERY sign request and REFUSES
(raises `SignerRefused` -> wire `{error,reason}`) rather than signing on any
violation -- there is NO "sign anyway" branch anywhere in this module.

Wire protocol (matches aats/execution/signer_client.py -- UNCHANGED by ADR-0015):
  request:  4-byte BE length + JSON {"wallet_id","tx_b64","intent_id","intent_kind","mint"}
  success:  JSON {"signed_b64","pubkey"}
  refusal:  JSON {"error","reason"}          # reason in the frozen SignerRefused set

  Fix round 1 additions (both additive, wire SHAPE unchanged -- see exceptions.py
  SignerRefused docstring "ADDITIVE reason codes"):
    - a request-frame length prefix over _MAX_FRAME_BYTES is refused
      (signer_frame_too_large) WITHOUT reading the body.
    - a request whose "wallet_id" does not match this process's provisioned
      SIGNER_WALLET_ID env var (default "wallet-0") is refused
      (signer_wallet_id_mismatch) before any tx parsing.

  Fix round 2 additions (wire SHAPE unchanged):
    - each accepted connection now gets a bounded recv timeout
      (_CONN_RECV_TIMEOUT_S) so a slow/stuck/slowloris peer cannot wedge the
      single-threaded accept loop indefinitely (see _serve_accepted_connection).
    - Enforcer._parse_and_price now also asserts the tx's payer (account 0)
      equals wallet_pubkey, refusing (signer_payer_mismatch) otherwise -- see
      signer_enforcer.py and exceptions.py's "ADDITIVE reason codes" for round 2.

Key custody (infrastructure.md §5.3, unchanged by ADR-0015):
  Vault short-lived token at boot -> exchange for the wallet secret -> hold in
  mlock-able memory -> zeroize on process exit. NEVER a static env var.
  `SoldersEd25519Signer` is the production signer; ed25519 signing itself is
  delegated to `solders` (a pinned project dependency) -- this module NEVER
  hand-rolls signing math, only the un-bypassable POLICY around it.

  `DevKeystoreSigner` is a CLEARLY TEST-ONLY fallback (double-gated: an
  explicit opt-in env var AND a refusal if AATS_ENV looks like a real
  deployment) for offline dev/tests where Vault is unreachable. It is NEVER
  a production key source.

Run as a standalone process: `python -m aats.execution.signer_process`.
"""
from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import socket
import struct
import time
from typing import Protocol

from aats.execution import solana_wire
from aats.execution.exceptions import SignerRefused
from aats.execution.signer_enforcer import Enforcer, VelocityLedger, load_signer_policy

logger = logging.getLogger(__name__)

_HEADER_FMT = "!I"
_HEADER_SIZE = struct.calcsize(_HEADER_FMT)
# Max accepted request-frame size (fix round 1 MINOR finding). A real request is a JSON
# envelope around one serialized versioned tx (protocol max ~1232 bytes) -- 64 KiB is
# generous headroom while bounding the allocation/read a peer's length prefix can force.
# Defense-in-depth: the socket is already 0o600 owner-only + peer-credential gated
# (T-352a), this is a second, cheap layer for the one process holding the wallet secret.
_MAX_FRAME_BYTES = 65_536
_WALLET_ID_ENV = "SIGNER_WALLET_ID"
_DEFAULT_WALLET_ID = "wallet-0"  # matches multi_wallet.py's N=1 default slot id

# ---------------------------------------------------------------------------
# Ed25519Signer -- the ONLY thing in this module that touches secret key
# material. Signing math is ALWAYS delegated to a real, audited library.
# ---------------------------------------------------------------------------


class Ed25519Signer(Protocol):
    @property
    def pubkey_b58(self) -> str: ...

    def sign_message(self, message: bytes) -> bytes:
        """Sign `message` (the tx's MESSAGE bytes, not the whole tx) and return
        the raw 64-byte ed25519 signature."""
        ...


class SoldersEd25519Signer:
    """Production signer -- real ed25519 signing via `solders` (lazy-imported).

    NEVER hand-rolls signing math (see module docstring). `secret_bytes` is the
    64-byte Solana CLI keypair encoding (32-byte seed || 32-byte pubkey); it is
    held only in this instance's memory for the process lifetime and is never
    logged, returned, or serialized anywhere in this module.
    """

    def __init__(self, secret_bytes: bytes) -> None:
        try:
            from solders.keypair import Keypair  # type: ignore
        except ImportError as exc:
            raise ImportError(
                "solders is required for real ed25519 signing (SoldersEd25519Signer). "
                "Install it: pip install solders. This module NEVER hand-rolls ed25519 "
                "signing math -- only the un-bypassable policy around it."
            ) from exc
        self._keypair = Keypair.from_bytes(secret_bytes)
        self._pubkey_b58: str = str(self._keypair.pubkey())

    @property
    def pubkey_b58(self) -> str:
        return self._pubkey_b58

    def sign_message(self, message: bytes) -> bytes:
        return bytes(self._keypair.sign_message(message))


class DevKeystoreSigner:
    """TEST-ONLY dev-keystore fallback. NEVER a production key source.

    Loads a keypair from a plaintext JSON file (the standard Solana CLI
    `keypair.json` format: a JSON array of 64 ints = 32-byte seed || 32-byte
    pubkey) purely for local/offline testing where Vault is unreachable.
    This is INSECURE BY DESIGN (a plaintext file on disk) -- it MUST NEVER
    hold a funded/real wallet's secret. Double-gated so it can never
    silently activate:
      1. AATS_SIGNER_ALLOW_DEV_KEYSTORE must be exactly "true".
      2. AATS_ENV must NOT look like a real deployment (mainnet-*).
    Delegates the actual signing math to the same `SoldersEd25519Signer`
    (or a caller-injected `_signer_ctor`, used by tests to avoid a solders
    dependency) -- this class only handles the (deliberately weak) key
    SOURCE, never the crypto.
    """

    _LIVE_ENV_PREFIXES = ("mainnet-",)

    def __init__(
        self,
        keystore_path: str,
        *,
        _signer_ctor: type[Ed25519Signer] | None = None,
    ) -> None:
        env = os.environ.get("AATS_ENV", "").strip().lower()
        if env.startswith(self._LIVE_ENV_PREFIXES):
            raise RuntimeError(
                f"DevKeystoreSigner refuses to load: AATS_ENV={env!r} looks like a real "
                "deployment. The dev keystore is TEST-ONLY (sim/devnet only) -- production "
                "custody is Vault (infrastructure.md §5.3)."
            )
        if os.environ.get("AATS_SIGNER_ALLOW_DEV_KEYSTORE", "").strip().lower() != "true":
            raise RuntimeError(
                "DevKeystoreSigner refuses to load: AATS_SIGNER_ALLOW_DEV_KEYSTORE is not "
                "'true'. This double-gate exists so a dev/test keystore can never silently "
                "become the production key source."
            )
        with open(keystore_path, encoding="utf-8") as fh:
            raw = json.load(fh)
        if not isinstance(raw, list) or len(raw) != 64:
            raise ValueError(
                "dev keystore must be a JSON array of 64 ints (Solana CLI keypair.json format)"
            )
        secret_bytes = bytes(raw)
        ctor = _signer_ctor or SoldersEd25519Signer
        self._impl: Ed25519Signer = ctor(secret_bytes)  # type: ignore[call-arg]

    @property
    def pubkey_b58(self) -> str:
        return self._impl.pubkey_b58

    def sign_message(self, message: bytes) -> bytes:
        return self._impl.sign_message(message)


# ---------------------------------------------------------------------------
# Transaction signature splicing -- pure byte manipulation, no crypto.
# ---------------------------------------------------------------------------


def sign_transaction(tx_bytes: bytes, signer: Ed25519Signer, *, signer_index: int = 0) -> bytes:
    """Sign the MESSAGE portion of `tx_bytes` and splice the 64-byte signature
    into signature slot `signer_index`. Works on any well-formed unsigned tx
    (real solders-built bytes OR the test-fixture bytes from
    `solana_wire.build_versioned_tx`) -- the signature-slot layout is the same
    wire format `solana_wire.parse_transaction` reads.
    """
    n_sigs, sigs_start = solana_wire.read_compact_u16(tx_bytes, 0)
    if signer_index < 0 or signer_index >= n_sigs:
        raise ValueError(
            f"signer_index {signer_index} out of range for a tx declaring {n_sigs} "
            "required signatures"
        )
    message_start = sigs_start + n_sigs * 64
    if message_start > len(tx_bytes):
        raise ValueError("truncated transaction: signatures section")
    message = bytes(tx_bytes[message_start:])

    signature = signer.sign_message(message)
    if len(signature) != 64:
        raise ValueError("ed25519 signature must be exactly 64 bytes")

    out = bytearray(tx_bytes)
    slot_start = sigs_start + signer_index * 64
    out[slot_start:slot_start + 64] = signature
    return bytes(out)


# ---------------------------------------------------------------------------
# getTipAccounts -- boot-time fetch (C2 fail-closed on failure)
# ---------------------------------------------------------------------------


def fetch_tip_accounts(jito_url: str | None = None, *, timeout_s: float = 3.0) -> frozenset[str]:
    """Fetch the live Jito tip-account set via getTipAccounts.

    Returns an EMPTY frozenset on ANY failure (no URL configured, network
    error, malformed response) -- ADR-0015 C2's fail-closed rule: "the signer
    never widens the pin to recover." An empty pin means every value-moving
    System transfer refuses (`signer_unpinned_transfer`), never that the pin
    silently falls back to a hardcoded/stale copy.
    """
    url = jito_url or os.environ.get("JITO_BLOCK_ENGINE", "")
    if not url:
        logger.warning("aats_signer.tip_accounts.no_url_configured")
        return frozenset()
    try:
        import httpx  # noqa: F401 (lazy -- signer's core policy path has no network dependency)

        body = {"jsonrpc": "2.0", "id": 1, "method": "getTipAccounts", "params": []}
        with httpx.Client(timeout=timeout_s) as client:
            resp = client.post(url, json=body)
            resp.raise_for_status()
            data = resp.json()
    except Exception:
        logger.exception("aats_signer.tip_accounts.fetch_failed")
        return frozenset()

    result = data.get("result") or []
    accounts = frozenset(str(a) for a in result if isinstance(a, str))
    if not accounts:
        logger.warning("aats_signer.tip_accounts.empty_response")
    return accounts


# ---------------------------------------------------------------------------
# Request handler -- the ADR-0015 "signer main loop", as a pure function.
# ---------------------------------------------------------------------------


def handle_sign_request(
    payload: dict,
    *,
    signer: Ed25519Signer,
    enforcer: Enforcer,
    ledger: VelocityLedger,
    now_s: float,
    expected_wallet_id: str | None = None,
) -> dict:
    """Implements the ADR-0015 signer main loop (steps 2-5).

    NEVER raises -- always returns a wire-shaped dict:
      success: {"signed_b64": str, "pubkey": str}
      refusal: {"error": str, "reason": <frozen SignerRefused reason code>}

    `expected_wallet_id` (fix round 1 MINOR finding): step 1 of the ADR-0015 main loop
    is "select key by wallet_id" -- this process provisions exactly ONE signer (N=1,
    `multi_wallet.py` wallet_ids=["wallet-0"] at the current deployment), so there is no
    actual key SELECTION to perform, but a caller that sends a wallet_id NOT matching
    this process's provisioned wallet is a footgun (would silently sign with the wrong
    process's key if N>1 is ever enabled). Defaults to None (check skipped) so existing
    callers/tests that use a placeholder wallet_id keep working unchanged; the real
    `serve_forever()` boot path ALWAYS supplies the provisioned wallet id.
    """
    if expected_wallet_id is not None and payload.get("wallet_id") != expected_wallet_id:
        logger.warning(
            "aats_signer.wallet_id_mismatch",
            extra={"intent_id": payload.get("intent_id")},
        )
        return {
            "error": (
                "request wallet_id does not match this signer process's provisioned "
                "wallet (single-wallet-per-process invariant)."
            ),
            "reason": "signer_wallet_id_mismatch",
        }

    tx_b64 = payload.get("tx_b64")
    if not isinstance(tx_b64, str):
        return {"error": "request missing 'tx_b64'", "reason": "signer_tx_unparseable"}
    try:
        tx_bytes = base64.b64decode(tx_b64, validate=True)
    except (binascii.Error, ValueError) as exc:
        return {"error": f"tx_b64 is not valid base64: {exc}", "reason": "signer_tx_unparseable"}

    # step 3: enforce() re-derives EVERY policy fact from tx_bytes; the
    # request's intent_id/intent_kind/mint are NEVER read for policy.
    try:
        enforcer.enforce(tx_bytes, signer.pubkey_b58, now_s)
    except SignerRefused as exc:
        logger.warning(
            "aats_signer.refused",
            extra={"reason": exc.reason, "intent_id": payload.get("intent_id")},
        )
        return {"error": exc.message, "reason": exc.reason}

    # step 4: sign, THEN record the ledger -- never the other way around.
    try:
        signed_bytes = sign_transaction(tx_bytes, signer)
    except ValueError as exc:
        logger.exception("aats_signer.sign_failed", extra={"intent_id": payload.get("intent_id")})
        return {"error": str(exc), "reason": "signer_tx_unparseable"}

    try:
        spend = enforcer.compute_spend_lamports(tx_bytes, signer.pubkey_b58)
    except SignerRefused:
        # Cannot happen in practice -- enforce() above already validated this exact
        # tx_bytes with the identical computation. Fail closed rather than skip the
        # ledger record if it somehow does (e.g. a future refactor introduces drift).
        logger.exception(
            "aats_signer.post_sign_spend_recompute_failed",
            extra={"intent_id": payload.get("intent_id")},
        )
        spend = enforcer.policy.per_tx_cap_lamports  # conservative fallback -- see comment above
    ledger.record(now_s, spend)

    # WARNING (not INFO/DEBUG): the process-wide log level is forced to WARNING
    # (see main()) so this operationally-important "a signature was issued" event
    # stays visible without ever requiring a more verbose level. No secret/signed
    # bytes are included -- only the caller's logging-hint intent_id.
    logger.warning("aats_signer.signed", extra={"intent_id": payload.get("intent_id")})
    return {"signed_b64": base64.b64encode(signed_bytes).decode(), "pubkey": signer.pubkey_b58}


# ---------------------------------------------------------------------------
# Boot -- builds the Enforcer + signer from config/Vault, fails loud on any
# missing/misconfigured piece (RED-1: no silent fallback to an unpoliced signer).
# ---------------------------------------------------------------------------


class SignerBootError(RuntimeError):
    """Raised when the signer process cannot boot safely. FAILS LOUD -- never
    starts a socket listener that would sign without a valid Enforcer."""


def _load_wallet_signer() -> Ed25519Signer:
    """Load the wallet signer: Vault in production, dev-keystore ONLY if explicitly
    opted in for tests. FAILS LOUD (raises SignerBootError) if neither is configured
    -- there is NO silent fallback to an unpoliced/mock signer (RED-1)."""
    vault_path = os.environ.get("WALLET_SECRET_VAULT_PATH", "")
    if vault_path:
        try:
            return _load_wallet_signer_from_vault(vault_path)
        except Exception as exc:  # noqa: BLE001 -- fail loud with full context, never fall through
            raise SignerBootError(
                f"failed to load wallet secret from Vault path {vault_path!r}: {exc}"
            ) from exc

    dev_keystore_path = os.environ.get("AATS_SIGNER_DEV_KEYSTORE_PATH", "")
    if dev_keystore_path:
        try:
            return DevKeystoreSigner(dev_keystore_path)
        except Exception as exc:  # noqa: BLE001
            raise SignerBootError(f"dev keystore load failed: {exc}") from exc

    raise SignerBootError(
        "no wallet key source configured: set WALLET_SECRET_VAULT_PATH (production) or "
        "AATS_SIGNER_DEV_KEYSTORE_PATH + AATS_SIGNER_ALLOW_DEV_KEYSTORE=true (test-only). "
        "Refusing to boot with an unpoliced/undefined key source (RED-1)."
    )


def _load_wallet_signer_from_vault(vault_path: str) -> Ed25519Signer:
    """Fetch the wallet secret from Vault (short-lived token -> mlock -> zeroize,
    infrastructure.md §5.3) and construct the production signer.

    Vault is NOT reachable in this offline dev/CI sandbox -- this function is
    exercised in a real deployment only; `latency-devops-engineer` wires the
    actual Vault client/mlock/zeroize integration (T-352a). Lazy-imports `hvac`
    so the signer's core policy path (Enforcer) has no Vault dependency at all.
    """
    try:
        import hvac  # type: ignore
    except ImportError as exc:
        raise ImportError(
            "hvac is required to fetch the wallet secret from Vault. Install it: "
            "pip install hvac. NEVER substitute a static env var for the Vault path "
            "(infrastructure.md §5.3 hard rule)."
        ) from exc

    vault_addr = os.environ.get("VAULT_ADDR", "")
    vault_token = os.environ.get("VAULT_TOKEN", "")
    if not vault_addr or not vault_token:
        raise SignerBootError("VAULT_ADDR / VAULT_TOKEN not set -- cannot fetch the wallet secret.")
    client = hvac.Client(url=vault_addr, token=vault_token)
    secret = client.secrets.kv.v2.read_secret_version(path=vault_path)
    raw = secret["data"]["data"]["keypair"]  # list[int], the 64-byte CLI keypair encoding
    secret_bytes = bytes(raw)
    try:
        return SoldersEd25519Signer(secret_bytes)
    finally:
        # Best-effort zeroize of the local reference to the raw secret bytes/list;
        # the SoldersEd25519Signer instance is the sole holder from here on.
        del secret_bytes, raw, secret


def boot_enforcer() -> tuple[Enforcer, VelocityLedger]:
    """Build the Enforcer from config/program-allowlist.json + config/signer-policy.json
    + a live getTipAccounts fetch. FAILS LOUD on any config/parse error."""
    tip_accounts = fetch_tip_accounts()
    try:
        policy = load_signer_policy(pinned_tip_accounts=tip_accounts)
    except Exception as exc:
        raise SignerBootError(f"failed to load signer policy: {exc}") from exc
    ledger = VelocityLedger(window_seconds=policy.window_seconds)
    return Enforcer(policy, ledger), ledger


# ---------------------------------------------------------------------------
# Unix-domain socket server
# ---------------------------------------------------------------------------

_SOCKET_PATH_ENV = "SIGNER_SOCKET_PATH"
_DEFAULT_SOCKET_PATH = "/run/aats/signer.sock"

# Fix round 2 MINOR finding: the server previously set NO per-connection recv
# timeout and accepted connections serially (single-threaded, `listen(8)`) -- a
# slow/stuck or slowloris peer holding a connection open inside `_recv_exact` would
# wedge signing for the ENTIRE process (every other request, including a
# time-critical exit, blocks behind it). Primary mitigation remains the socket being
# 0o600 owner-only + peer-credential gated (devops T-352a) so only a local trusted
# caller can ever connect -- but a hung/misbehaving local caller must still be
# dropped rather than block forever. Generous relative to real request latency
# (single-digit-to-tens of ms, see signer_client.py's timeout note) so it never
# fires on a legitimate slow-but-alive request; tight enough that a truly stuck
# peer is dropped well before it matters operationally.
_CONN_RECV_TIMEOUT_S = 5.0


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise OSError("peer closed connection prematurely")
        buf.extend(chunk)
    return bytes(buf)


def _serve_one_connection(conn: socket.socket, *, signer: Ed25519Signer, enforcer: Enforcer,
                           ledger: VelocityLedger, expected_wallet_id: str | None = None) -> None:
    header = _recv_exact(conn, _HEADER_SIZE)
    (length,) = struct.unpack(_HEADER_FMT, header)
    if length > _MAX_FRAME_BYTES:
        # Refuse WITHOUT reading the body -- never allocate/read attacker-controlled
        # bytes past the bound (fix round 1 MINOR finding).
        response = {
            "error": f"request frame ({length} bytes) exceeds max {_MAX_FRAME_BYTES} bytes.",
            "reason": "signer_frame_too_large",
        }
        resp_bytes = json.dumps(response).encode()
        conn.sendall(struct.pack(_HEADER_FMT, len(resp_bytes)) + resp_bytes)
        return
    body = _recv_exact(conn, length)
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        response = {"error": "malformed JSON request", "reason": "signer_tx_unparseable"}
    else:
        response = handle_sign_request(
            payload, signer=signer, enforcer=enforcer, ledger=ledger, now_s=time.monotonic(),
            expected_wallet_id=expected_wallet_id,
        )
    resp_bytes = json.dumps(response).encode()
    conn.sendall(struct.pack(_HEADER_FMT, len(resp_bytes)) + resp_bytes)


def _serve_accepted_connection(
    conn: socket.socket, *, signer: Ed25519Signer, enforcer: Enforcer, ledger: VelocityLedger,
    expected_wallet_id: str | None = None,
) -> None:
    """Serve ONE already-accepted connection, with the per-connection recv timeout
    applied (fix round 2 MINOR finding -- see `_CONN_RECV_TIMEOUT_S` above).

    Factored out of `serve_forever()`'s accept loop so it is independently testable
    with `socket.socketpair()` -- a real bound+accepted `AF_UNIX` connection is not
    exercisable in every dev/CI sandbox (e.g. `AF_UNIX` is unavailable on this
    Windows Python build), but a socketpair is a real connected socket pair the OS
    treats identically for `settimeout()`/`recv()` purposes.
    """
    conn.settimeout(_CONN_RECV_TIMEOUT_S)
    try:
        _serve_one_connection(
            conn, signer=signer, enforcer=enforcer, ledger=ledger,
            expected_wallet_id=expected_wallet_id,
        )
    except OSError:
        # Covers both a genuinely stuck/slowloris peer (socket.timeout, a subclass of
        # OSError as of Python 3.10) and any other connection-level failure -- the
        # process logs and moves on to the NEXT accept() rather than staying wedged.
        logger.exception("aats_signer.connection_error")


def serve_forever(socket_path: str | None = None) -> None:  # pragma: no cover -- process entrypoint
    """Run the aats-signer socket server. FAILS LOUD at boot (SignerBootError propagates
    and the process exits nonzero) rather than listening with an unpoliced signer."""
    path = socket_path or os.environ.get(_SOCKET_PATH_ENV, _DEFAULT_SOCKET_PATH)
    expected_wallet_id = os.environ.get(_WALLET_ID_ENV, _DEFAULT_WALLET_ID)
    signer = _load_wallet_signer()
    enforcer, ledger = boot_enforcer()
    # WARNING (not INFO): forced process log level is WARNING (see main()). The
    # pubkey is PUBLIC (never the secret) -- safe to log at any level, but kept
    # at WARNING for consistency with the rest of this module.
    logger.warning(
        "aats_signer.boot",
        extra={"pubkey": signer.pubkey_b58, "socket_path": path, "wallet_id": expected_wallet_id},
    )

    if os.path.exists(path):
        os.remove(path)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(path)
    os.chmod(path, 0o600)  # owner-only; peer-credential gating is devops' T-352a layer
    server.listen(8)
    try:
        while True:
            conn, _ = server.accept()
            with conn:
                _serve_accepted_connection(
                    conn, signer=signer, enforcer=enforcer, ledger=ledger,
                    expected_wallet_id=expected_wallet_id,
                )
    finally:
        server.close()
        if os.path.exists(path):
            os.remove(path)


def main() -> None:  # pragma: no cover -- process entrypoint
    # Force WARNING as the process-wide default (never DEBUG/INFO) -- defense in
    # depth so no future log call in this process (or a dependency's logger) can
    # leak key/signed-byte-adjacent detail at a verbose level. Individual events
    # here never carry secret material (only intent_id/pubkey/reason), but the
    # signer is the one process holding the wallet secret and the level is
    # deliberately conservative regardless (mirrors signer_client.py's
    # `_LOG_LEVEL = logging.WARNING` on the caller side).
    logging.basicConfig(level=logging.WARNING)
    logger.warning("aats_signer.starting")
    serve_forever()


if __name__ == "__main__":  # pragma: no cover
    main()
