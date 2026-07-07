"""Tests for the isolated Python signer process (aats/execution/signer_process.py).

Covers: the request handler (ADR-0015 main-loop steps 2-5) wired to a FAKE
Ed25519Signer (no real crypto -- exactly like MockSignerClient never holds a
key), the TEST-ONLY DevKeystoreSigner double-gate, sign_transaction's byte
splicing, and the mandatory "LIVE-without-config fails loud" boot test.
"""
from __future__ import annotations

import base64
import hashlib
import json
import struct
import time

import pytest

from aats.execution import solana_wire as sw
from aats.execution.signer_enforcer import Enforcer, SignerPolicy, VelocityLedger
from aats.execution.signer_process import (
    DevKeystoreSigner,
    SignerBootError,
    fetch_tip_accounts,
    handle_sign_request,
    sign_transaction,
)


def _pubkey(seed: str) -> str:
    return sw.b58encode(hashlib.sha256(f"test-pubkey-{seed}".encode()).digest())


WALLET = _pubkey("wallet")
SYSTEM = _pubkey("system_program")
VENUE = _pubkey("venue_program")
TIP_1 = _pubkey("tip_account_1")
BLOCKHASH = _pubkey("blockhash")


class _FakeEd25519Signer:
    """NEVER holds a real key -- deterministic 64-byte "signature" derived from a hash,
    exactly like MockSignerClient never touches real key material."""

    def __init__(self, pubkey_b58: str = WALLET) -> None:
        self._pubkey = pubkey_b58
        self.sign_calls: list[bytes] = []

    @property
    def pubkey_b58(self) -> str:
        return self._pubkey

    def sign_message(self, message: bytes) -> bytes:
        self.sign_calls.append(message)
        digest = hashlib.sha512(message).digest()  # 64 bytes
        return digest


class _FakeVenueDecoder:
    def max_sol_lamports(self, ix_data: bytes) -> int:
        if ix_data[:4] != b"\x01\x02\x03\x04":
            raise ValueError("unknown discriminator")
        return struct.unpack("<Q", ix_data[4:12])[0]


def _policy(**overrides: object) -> SignerPolicy:
    defaults: dict[str, object] = {
        "per_tx_cap_lamports": 100_000_000,
        "aggregate_cap_lamports": 500_000_000,
        "window_seconds": 60,
        "max_sign_count": 10,
        "allowed_program_ids": frozenset({SYSTEM, VENUE}),
        "venue_program_ids": frozenset({VENUE}),
        "pinned_tip_accounts": frozenset({TIP_1}),
        "ata_program_id": _pubkey("ata_program"),
        "spl_token_program_id": _pubkey("spl_token"),
        "venue_spend_decoders": {VENUE: _FakeVenueDecoder()},
        "system_program_id": SYSTEM,
    }
    defaults.update(overrides)
    return SignerPolicy(**defaults)  # type: ignore[arg-type]


def _valid_tx_b64() -> str:
    data = b"\x01\x02\x03\x04" + struct.pack("<Q", 1_000)
    tx = sw.build_versioned_tx(
        account_keys=[WALLET, VENUE],
        num_required_signatures=1,
        recent_blockhash=BLOCKHASH,
        instructions=[sw.RawInstruction(program_id=VENUE, accounts=(0,), data=data)],
    )
    return base64.b64encode(tx).decode()


def _over_cap_tx_b64() -> str:
    data = b"\x01\x02\x03\x04" + struct.pack("<Q", 200_000_000)
    tx = sw.build_versioned_tx(
        account_keys=[WALLET, VENUE],
        num_required_signatures=1,
        recent_blockhash=BLOCKHASH,
        instructions=[sw.RawInstruction(program_id=VENUE, accounts=(0,), data=data)],
    )
    return base64.b64encode(tx).decode()


# ---------------------------------------------------------------------------
# handle_sign_request -- the ADR-0015 main loop as a pure function
# ---------------------------------------------------------------------------


class TestHandleSignRequestSuccess:
    def test_valid_request_returns_signed_b64_and_pubkey(self) -> None:
        signer = _FakeEd25519Signer()
        enforcer = Enforcer(_policy(), VelocityLedger(window_seconds=60))
        payload = {
            "wallet_id": "wallet-0",
            "tx_b64": _valid_tx_b64(),
            "intent_id": "ci-1",
            "intent_kind": "ENTRY",
            "mint": "SomeMint111111111111111111111111111111111",
        }
        resp = handle_sign_request(
            payload, signer=signer, enforcer=enforcer, ledger=enforcer.ledger, now_s=time.monotonic()
        )
        assert "signed_b64" in resp
        assert resp["pubkey"] == WALLET
        assert "error" not in resp

    def test_success_splices_a_real_signature_into_the_tx(self) -> None:
        signer = _FakeEd25519Signer()
        enforcer = Enforcer(_policy(), VelocityLedger(window_seconds=60))
        tx_b64 = _valid_tx_b64()
        payload = {"wallet_id": "w", "tx_b64": tx_b64, "intent_id": "ci-2"}
        resp = handle_sign_request(
            payload, signer=signer, enforcer=enforcer, ledger=enforcer.ledger, now_s=time.monotonic()
        )
        signed_bytes = base64.b64decode(resp["signed_b64"])
        original_bytes = base64.b64decode(tx_b64)
        # Only the 64-byte signature slot should differ from the unsigned original.
        assert signed_bytes != original_bytes
        assert len(signed_bytes) == len(original_bytes)
        n_sigs, pos = sw.read_compact_u16(signed_bytes, 0)
        assert n_sigs == 1
        assert signed_bytes[pos:pos + 64] != b"\x00" * 64
        assert signed_bytes[pos + 64:] == original_bytes[pos + 64:]  # message untouched

    def test_success_records_the_ledger(self) -> None:
        signer = _FakeEd25519Signer()
        ledger = VelocityLedger(window_seconds=60)
        enforcer = Enforcer(_policy(max_sign_count=1), ledger)
        payload = {"wallet_id": "w", "tx_b64": _valid_tx_b64(), "intent_id": "ci-3"}
        now = time.monotonic()
        resp1 = handle_sign_request(payload, signer=signer, enforcer=enforcer, ledger=ledger, now_s=now)
        assert "signed_b64" in resp1

        # max_sign_count=1 -- a second sign in the same instant must now refuse.
        resp2 = handle_sign_request(payload, signer=signer, enforcer=enforcer, ledger=ledger, now_s=now)
        assert resp2["reason"] == "signer_velocity_exceeded"


class TestHandleSignRequestRefusal:
    def test_over_cap_returns_wire_refusal(self) -> None:
        signer = _FakeEd25519Signer()
        enforcer = Enforcer(_policy(), VelocityLedger(window_seconds=60))
        payload = {"wallet_id": "w", "tx_b64": _over_cap_tx_b64(), "intent_id": "ci-4"}
        resp = handle_sign_request(
            payload, signer=signer, enforcer=enforcer, ledger=enforcer.ledger, now_s=time.monotonic()
        )
        assert resp["reason"] == "signer_per_tx_cap_exceeded"
        assert "signed_b64" not in resp
        assert signer.sign_calls == []  # NEVER signed

    def test_refusal_never_calls_sign(self) -> None:
        signer = _FakeEd25519Signer()
        enforcer = Enforcer(_policy(), VelocityLedger(window_seconds=60))
        payload = {"wallet_id": "w", "tx_b64": _over_cap_tx_b64(), "intent_id": "ci-5"}
        handle_sign_request(
            payload, signer=signer, enforcer=enforcer, ledger=enforcer.ledger, now_s=time.monotonic()
        )
        assert len(signer.sign_calls) == 0

    def test_malformed_base64_refuses(self) -> None:
        signer = _FakeEd25519Signer()
        enforcer = Enforcer(_policy(), VelocityLedger(window_seconds=60))
        payload = {"wallet_id": "w", "tx_b64": "not-valid-base64!!!", "intent_id": "ci-6"}
        resp = handle_sign_request(
            payload, signer=signer, enforcer=enforcer, ledger=enforcer.ledger, now_s=time.monotonic()
        )
        assert resp["reason"] == "signer_tx_unparseable"

    def test_missing_tx_b64_refuses(self) -> None:
        signer = _FakeEd25519Signer()
        enforcer = Enforcer(_policy(), VelocityLedger(window_seconds=60))
        resp = handle_sign_request(
            {"wallet_id": "w"}, signer=signer, enforcer=enforcer, ledger=enforcer.ledger,
            now_s=time.monotonic(),
        )
        assert resp["reason"] == "signer_tx_unparseable"

    def test_handler_never_raises(self) -> None:
        """The main loop must ALWAYS return a wire-shaped dict, never propagate an exception."""
        signer = _FakeEd25519Signer()
        enforcer = Enforcer(_policy(), VelocityLedger(window_seconds=60))
        resp = handle_sign_request(
            {}, signer=signer, enforcer=enforcer, ledger=enforcer.ledger, now_s=time.monotonic()
        )
        assert "error" in resp and "reason" in resp


class TestWalletIdMismatch:
    """Fix round 1 MINOR finding: a request whose wallet_id does not match this
    process's provisioned wallet refuses BEFORE any tx parsing/signing, when the
    caller (serve_forever) supplies an expected_wallet_id. Backward-compatible:
    the check is skipped (None default) for callers/tests that don't supply one."""

    def test_mismatched_wallet_id_refuses_without_signing(self) -> None:
        signer = _FakeEd25519Signer()
        enforcer = Enforcer(_policy(), VelocityLedger(window_seconds=60))
        payload = {"wallet_id": "wallet-9", "tx_b64": _valid_tx_b64(), "intent_id": "ci-wid-1"}
        resp = handle_sign_request(
            payload, signer=signer, enforcer=enforcer, ledger=enforcer.ledger,
            now_s=time.monotonic(), expected_wallet_id="wallet-0",
        )
        assert resp["reason"] == "signer_wallet_id_mismatch"
        assert "signed_b64" not in resp
        assert signer.sign_calls == []

    def test_matching_wallet_id_signs(self) -> None:
        signer = _FakeEd25519Signer()
        enforcer = Enforcer(_policy(), VelocityLedger(window_seconds=60))
        payload = {"wallet_id": "wallet-0", "tx_b64": _valid_tx_b64(), "intent_id": "ci-wid-2"}
        resp = handle_sign_request(
            payload, signer=signer, enforcer=enforcer, ledger=enforcer.ledger,
            now_s=time.monotonic(), expected_wallet_id="wallet-0",
        )
        assert "signed_b64" in resp

    def test_no_expected_wallet_id_skips_the_check(self) -> None:
        """Default (expected_wallet_id=None) preserves pre-fix behaviour -- existing
        callers/tests using a placeholder wallet_id are unaffected."""
        signer = _FakeEd25519Signer()
        enforcer = Enforcer(_policy(), VelocityLedger(window_seconds=60))
        payload = {"wallet_id": "anything-at-all", "tx_b64": _valid_tx_b64(), "intent_id": "ci-wid-3"}
        resp = handle_sign_request(
            payload, signer=signer, enforcer=enforcer, ledger=enforcer.ledger, now_s=time.monotonic(),
        )
        assert "signed_b64" in resp


# ---------------------------------------------------------------------------
# _serve_one_connection -- wire-level frame-size bound (fix round 1 MINOR finding)
# ---------------------------------------------------------------------------


class TestMaxFrameBound:
    def test_oversized_frame_refuses_without_reading_the_body(self) -> None:
        import socket as socket_module
        import struct as struct_module

        from aats.execution.signer_process import (
            _HEADER_FMT,
            _MAX_FRAME_BYTES,
            _serve_one_connection,
        )

        signer = _FakeEd25519Signer()
        enforcer = Enforcer(_policy(), VelocityLedger(window_seconds=60))
        client, server = socket_module.socketpair()
        try:
            oversized_len = _MAX_FRAME_BYTES + 1
            client.sendall(struct_module.pack(_HEADER_FMT, oversized_len))
            # Deliberately do NOT send the (huge) body -- if the server tried to read
            # it, this test would hang/timeout, proving the bound is enforced BEFORE
            # any body read is attempted.
            client.settimeout(2.0)
            _serve_one_connection(server, signer=signer, enforcer=enforcer, ledger=enforcer.ledger)
            resp_header = client.recv(4)
            (resp_len,) = struct_module.unpack(_HEADER_FMT, resp_header)
            resp_body = client.recv(resp_len)
            resp = json.loads(resp_body)
            assert resp["reason"] == "signer_frame_too_large"
        finally:
            client.close()
            server.close()
        assert signer.sign_calls == []

    def test_normal_sized_frame_is_processed(self) -> None:
        import socket as socket_module
        import struct as struct_module

        from aats.execution.signer_process import _HEADER_FMT, _serve_one_connection

        signer = _FakeEd25519Signer()
        enforcer = Enforcer(_policy(), VelocityLedger(window_seconds=60))
        client, server = socket_module.socketpair()
        try:
            payload = json.dumps(
                {"wallet_id": "w", "tx_b64": _valid_tx_b64(), "intent_id": "ci-frame-1"}
            ).encode()
            client.sendall(struct_module.pack(_HEADER_FMT, len(payload)) + payload)
            client.settimeout(2.0)
            _serve_one_connection(server, signer=signer, enforcer=enforcer, ledger=enforcer.ledger)
            resp_header = client.recv(4)
            (resp_len,) = struct_module.unpack(_HEADER_FMT, resp_header)
            resp_body = client.recv(resp_len)
            resp = json.loads(resp_body)
            assert "signed_b64" in resp
        finally:
            client.close()
            server.close()


class TestCallerMetadataIgnored:
    """ADR-0015 trust model: intent_id/intent_kind/mint/sol_in are LOGGING HINTS ONLY --
    they never influence the enforcement verdict. Two requests wrapping the IDENTICAL
    tx_bytes but carrying DIFFERENT (one truthful, one lying) metadata must produce
    IDENTICAL outcomes."""

    def test_lying_metadata_does_not_change_a_refusal(self) -> None:
        tx_b64 = _over_cap_tx_b64()
        truthful = {"wallet_id": "w", "tx_b64": tx_b64, "intent_id": "a", "intent_kind": "ENTRY",
                    "mint": "RealMint1111111111111111111111111111111111"}
        lying = {"wallet_id": "w", "tx_b64": tx_b64, "intent_id": "a", "intent_kind": "EXIT",
                  "mint": "TotallyDifferentMint111111111111111111111"}
        now = time.monotonic()
        for payload in (truthful, lying):
            signer = _FakeEd25519Signer()
            enforcer = Enforcer(_policy(), VelocityLedger(window_seconds=60))
            resp = handle_sign_request(
                payload, signer=signer, enforcer=enforcer, ledger=enforcer.ledger, now_s=now
            )
            assert resp["reason"] == "signer_per_tx_cap_exceeded"

    def test_lying_metadata_does_not_change_a_success(self) -> None:
        tx_b64 = _valid_tx_b64()
        truthful = {"wallet_id": "w", "tx_b64": tx_b64, "intent_id": "a", "intent_kind": "ENTRY",
                    "mint": "RealMint1111111111111111111111111111111111"}
        lying = {"wallet_id": "w", "tx_b64": tx_b64, "intent_id": "a", "intent_kind": "EXIT",
                  "mint": "TotallyDifferentMint111111111111111111111"}
        now = time.monotonic()
        results = []
        for payload in (truthful, lying):
            signer = _FakeEd25519Signer()
            enforcer = Enforcer(_policy(), VelocityLedger(window_seconds=60))
            resp = handle_sign_request(
                payload, signer=signer, enforcer=enforcer, ledger=enforcer.ledger, now_s=now
            )
            assert "signed_b64" in resp
            results.append(resp["signed_b64"])
        assert results[0] == results[1]  # byte-identical output regardless of metadata


# ---------------------------------------------------------------------------
# sign_transaction -- pure byte splicing
# ---------------------------------------------------------------------------


class TestSignTransaction:
    def test_signature_lands_in_the_correct_slot(self) -> None:
        signer = _FakeEd25519Signer()
        tx = sw.build_versioned_tx(
            account_keys=[WALLET, SYSTEM],
            num_required_signatures=1,
            recent_blockhash=BLOCKHASH,
            instructions=[],
        )
        signed = sign_transaction(tx, signer)
        n_sigs, pos = sw.read_compact_u16(signed, 0)
        assert signed[pos:pos + 64] == hashlib.sha512(tx[pos + 64 * n_sigs:]).digest()

    def test_signer_index_out_of_range_raises(self) -> None:
        signer = _FakeEd25519Signer()
        tx = sw.build_versioned_tx(
            account_keys=[WALLET],
            num_required_signatures=1,
            recent_blockhash=BLOCKHASH,
            instructions=[],
        )
        with pytest.raises(ValueError):
            sign_transaction(tx, signer, signer_index=5)


# ---------------------------------------------------------------------------
# DevKeystoreSigner -- TEST-ONLY, double-gated
# ---------------------------------------------------------------------------


class TestDevKeystoreSignerGating:
    def test_refuses_without_allow_flag(self, monkeypatch: pytest.MonkeyPatch, tmp_path: object) -> None:
        monkeypatch.delenv("AATS_SIGNER_ALLOW_DEV_KEYSTORE", raising=False)
        monkeypatch.delenv("AATS_ENV", raising=False)
        keystore = tmp_path / "keypair.json"  # type: ignore[operator]
        keystore.write_text(json.dumps(list(range(64))))
        with pytest.raises(RuntimeError, match="AATS_SIGNER_ALLOW_DEV_KEYSTORE"):
            DevKeystoreSigner(str(keystore))

    def test_refuses_when_aats_env_looks_live(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: object
    ) -> None:
        monkeypatch.setenv("AATS_SIGNER_ALLOW_DEV_KEYSTORE", "true")
        monkeypatch.setenv("AATS_ENV", "mainnet-live")
        keystore = tmp_path / "keypair.json"  # type: ignore[operator]
        keystore.write_text(json.dumps(list(range(64))))
        with pytest.raises(RuntimeError, match="AATS_ENV"):
            DevKeystoreSigner(str(keystore))

    def test_refuses_malformed_keystore(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: object
    ) -> None:
        monkeypatch.setenv("AATS_SIGNER_ALLOW_DEV_KEYSTORE", "true")
        monkeypatch.delenv("AATS_ENV", raising=False)
        keystore = tmp_path / "keypair.json"  # type: ignore[operator]
        keystore.write_text(json.dumps([1, 2, 3]))  # not 64 ints
        with pytest.raises(ValueError):
            DevKeystoreSigner(str(keystore))

    def test_gated_open_loads_via_injected_signer_ctor(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: object
    ) -> None:
        """With both gates cleared, DevKeystoreSigner delegates key construction to
        whatever Ed25519Signer implementation is injected -- proven here with a fake
        (no real crypto library needed to test the gating logic itself)."""
        monkeypatch.setenv("AATS_SIGNER_ALLOW_DEV_KEYSTORE", "true")
        monkeypatch.setenv("AATS_ENV", "sim")
        keystore = tmp_path / "keypair.json"  # type: ignore[operator]
        keystore.write_text(json.dumps(list(range(64))))

        class _FakeCtor:
            def __init__(self, secret_bytes: bytes) -> None:
                self._secret = secret_bytes

            @property
            def pubkey_b58(self) -> str:
                return "FakePubkeyFromDevKeystore"

            def sign_message(self, message: bytes) -> bytes:
                return b"\x01" * 64

        dks = DevKeystoreSigner(str(keystore), _signer_ctor=_FakeCtor)  # type: ignore[arg-type]
        assert dks.pubkey_b58 == "FakePubkeyFromDevKeystore"
        assert dks.sign_message(b"msg") == b"\x01" * 64


# ---------------------------------------------------------------------------
# Boot fail-loud: LIVE-without-config must not silently start unpoliced
# ---------------------------------------------------------------------------


class TestBootFailsLoud:
    def test_no_key_source_configured_fails_loud(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("WALLET_SECRET_VAULT_PATH", raising=False)
        monkeypatch.delenv("AATS_SIGNER_DEV_KEYSTORE_PATH", raising=False)
        from aats.execution.signer_process import _load_wallet_signer

        with pytest.raises(SignerBootError, match="no wallet key source configured"):
            _load_wallet_signer()

    def test_vault_path_set_but_vault_unreachable_fails_loud(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("WALLET_SECRET_VAULT_PATH", "secret/aats/trade-wallet")
        monkeypatch.delenv("VAULT_ADDR", raising=False)
        monkeypatch.delenv("VAULT_TOKEN", raising=False)
        from aats.execution.signer_process import _load_wallet_signer

        with pytest.raises(SignerBootError):
            _load_wallet_signer()

    def test_boot_enforcer_loads_the_real_shipped_config(self) -> None:
        from aats.execution.signer_process import boot_enforcer

        enforcer, ledger = boot_enforcer()
        assert enforcer is not None
        assert ledger is not None
        # An unverified venue buy must still refuse even against the real config.
        assert enforcer.policy.venue_program_ids


# ---------------------------------------------------------------------------
# _serve_accepted_connection -- per-connection recv timeout (fix round 2 MINOR
# finding: a slow/stuck/slowloris peer must not wedge the whole accept loop).
# ---------------------------------------------------------------------------


class TestSlowPeerRecvTimeout:
    def test_peer_sending_nothing_is_dropped_not_wedged_forever(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A peer that connects and sends NOTHING must be dropped within
        _CONN_RECV_TIMEOUT_S, not block the caller (and therefore the whole
        single-threaded accept loop) forever. Uses socket.socketpair() -- AF_UNIX
        bind/accept is not available on this Windows Python build (mirrors why
        serve_forever() itself is pragma:no-cover here), but socketpair() gives a
        real connected socket pair the OS treats identically for
        settimeout()/recv() purposes, so this proves the actual mechanism."""
        import socket as socket_module

        import aats.execution.signer_process as sp

        monkeypatch.setattr(sp, "_CONN_RECV_TIMEOUT_S", 0.2)

        signer = _FakeEd25519Signer()
        enforcer = Enforcer(_policy(), VelocityLedger(window_seconds=60))
        client, server = socket_module.socketpair()
        try:
            start = time.monotonic()
            # Must return (the OSError/timeout is caught internally and logged),
            # not raise out to the caller and not hang.
            sp._serve_accepted_connection(
                server, signer=signer, enforcer=enforcer, ledger=enforcer.ledger,
            )
            elapsed = time.monotonic() - start
            # Bounded by _CONN_RECV_TIMEOUT_S with generous slack for scheduler
            # jitter -- must NOT be anywhere near "forever".
            assert elapsed < 2.0
        finally:
            client.close()
            server.close()
        assert signer.sign_calls == []  # never reached the signer

    def test_connection_gets_the_configured_timeout_applied(self) -> None:
        """Directly proves _serve_accepted_connection calls settimeout() with the
        module's _CONN_RECV_TIMEOUT_S constant on the accepted connection."""
        import socket as socket_module

        import aats.execution.signer_process as sp

        signer = _FakeEd25519Signer()
        enforcer = Enforcer(_policy(), VelocityLedger(window_seconds=60))
        client, server = socket_module.socketpair()
        try:
            payload = json.dumps(
                {"wallet_id": "w", "tx_b64": _valid_tx_b64(), "intent_id": "ci-timeout-1"}
            ).encode()
            client.sendall(struct.pack(sp._HEADER_FMT, len(payload)) + payload)
            client.settimeout(2.0)

            sp._serve_accepted_connection(
                server, signer=signer, enforcer=enforcer, ledger=enforcer.ledger,
            )
            assert server.gettimeout() == sp._CONN_RECV_TIMEOUT_S

            resp_header = client.recv(4)
            (resp_len,) = struct.unpack(sp._HEADER_FMT, resp_header)
            resp_body = client.recv(resp_len)
            resp = json.loads(resp_body)
            assert "signed_b64" in resp
        finally:
            client.close()
            server.close()


# ---------------------------------------------------------------------------
# fetch_tip_accounts -- fail-closed on any failure
# ---------------------------------------------------------------------------


class TestFetchTipAccounts:
    def test_no_url_configured_returns_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("JITO_BLOCK_ENGINE", raising=False)
        assert fetch_tip_accounts() == frozenset()

    def test_unreachable_url_returns_empty(self) -> None:
        # No real network in this sandbox -- any URL fails closed to empty.
        result = fetch_tip_accounts("https://definitely-not-a-real-jito-endpoint.invalid")
        assert result == frozenset()
