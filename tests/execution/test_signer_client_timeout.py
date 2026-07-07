"""Tests for SocketSignerClient's socket-timeout resolution (fix round 2 MAJOR
finding) and a recorded latency benchmark for the Enforcer's hot own-ATA path that
justifies the new default (see aats/execution/signer_client.py module docstring).

Scope note: `SocketSignerClient.sign()` itself opens a real `socket.AF_UNIX` socket
-- `AF_UNIX` is NOT available on this Windows Python build (`hasattr(socket,
"AF_UNIX")` is False here), mirroring why `aats/execution/signer_process.py`'s
`serve_forever()` is already `pragma: no cover` in this sandbox. What IS fully
testable without a real Unix-domain socket, and is covered below: the timeout
RESOLUTION logic (env var / constructor arg precedence, malformed-value fallback)
that the code-review finding is actually about, plus the latency measurement that
sets the new default's justification.
"""
from __future__ import annotations

import hashlib
import struct
import time

import pytest

from aats.execution import solana_wire as sw
from aats.execution.signer_client import (
    _DEFAULT_SOCKET_TIMEOUT_S,
    SocketSignerClient,
    _resolve_default_timeout_s,
)
from aats.execution.signer_enforcer import Enforcer, SignerPolicy, VelocityLedger


def _pubkey(seed: str) -> str:
    return sw.b58encode(hashlib.sha256(f"test-pubkey-{seed}".encode()).digest())


# ---------------------------------------------------------------------------
# _resolve_default_timeout_s / constructor wiring
# ---------------------------------------------------------------------------


class TestResolveDefaultTimeout:
    def test_no_env_var_returns_the_documented_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("SIGNER_CLIENT_TIMEOUT_S", raising=False)
        assert _resolve_default_timeout_s() == _DEFAULT_SOCKET_TIMEOUT_S

    def test_default_is_no_longer_the_stale_3ms_rust_era_value(self) -> None:
        """The core of the MAJOR finding: 0.003s was budgeted for the deferred Rust
        signer and left unreconciled against the shipped Python signer. Assert the
        default has actually moved, not just that a new knob exists."""
        assert _DEFAULT_SOCKET_TIMEOUT_S != 0.003
        assert _DEFAULT_SOCKET_TIMEOUT_S > 0.003

    def test_env_var_overrides_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SIGNER_CLIENT_TIMEOUT_S", "0.25")
        assert _resolve_default_timeout_s() == 0.25

    def test_malformed_env_var_falls_back_to_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SIGNER_CLIENT_TIMEOUT_S", "not-a-number")
        assert _resolve_default_timeout_s() == _DEFAULT_SOCKET_TIMEOUT_S

    def test_non_positive_env_var_falls_back_to_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SIGNER_CLIENT_TIMEOUT_S", "0")
        assert _resolve_default_timeout_s() == _DEFAULT_SOCKET_TIMEOUT_S
        monkeypatch.setenv("SIGNER_CLIENT_TIMEOUT_S", "-1")
        assert _resolve_default_timeout_s() == _DEFAULT_SOCKET_TIMEOUT_S

    def test_constructor_arg_takes_precedence_over_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SIGNER_CLIENT_TIMEOUT_S", "0.25")
        client = SocketSignerClient(socket_path="/tmp/irrelevant.sock", timeout_s=0.011)
        assert client._timeout_s == 0.011

    def test_constructor_no_arg_reads_env_at_construction_time(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SIGNER_CLIENT_TIMEOUT_S", "0.33")
        client = SocketSignerClient(socket_path="/tmp/irrelevant.sock")
        assert client._timeout_s == 0.33

    def test_constructor_no_arg_no_env_uses_documented_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("SIGNER_CLIENT_TIMEOUT_S", raising=False)
        client = SocketSignerClient(socket_path="/tmp/irrelevant.sock")
        assert client._timeout_s == _DEFAULT_SOCKET_TIMEOUT_S


# ---------------------------------------------------------------------------
# Recorded latency benchmark -- the measurement the new default is based on
# (fix round 2 MAJOR finding: "measure the fixed signer's p99 sign latency and
# set the client timeout from it (with headroom), and record the measurement").
# ---------------------------------------------------------------------------


class TestMeasuredEnforcerLatencyIsRecordedForTheTimeoutDefault:
    """Reproduces, as a pytest test (not just an ad-hoc script), the benchmark
    signer_client.py's module docstring cites. Runs against the REAL Enforcer
    hot path (Enforcer.enforce()) on a realistic 17-key tx carrying an own-ATA
    transfer -- the worst case the MAJOR finding identified. In THIS sandbox
    `solders` is not installed (a pinned prod dependency that IS expected to be
    present in a real deployment), so this measures the pure-Python fallback
    path -- documented as the honest, dated number, not a stand-in for a real
    production measurement (see the module docstring's "REQUIRED PRECONDITION").

    This is a REGRESSION GUARD (generous bound, not a tight perf assertion) plus
    a printed record of the actual numbers for traceability -- run with
    `-s` to see them, or read them from CI log capture.
    """

    def _policy(self) -> SignerPolicy:
        wallet = _pubkey("bench-wallet")
        system = _pubkey("bench-system")
        compute_budget = _pubkey("bench-compute-budget")
        venue = _pubkey("bench-venue")
        ata_program = _pubkey("bench-ata-program")
        spl_token = _pubkey("bench-spl-token")
        tip_1 = _pubkey("bench-tip-1")

        class _ZeroSpendDecoder:
            def max_sol_lamports(self, ix_data: bytes) -> int:
                return 0

        self._wallet = wallet
        self._system = system
        self._compute_budget = compute_budget
        self._venue = venue
        self._ata_program = ata_program
        self._spl_token = spl_token
        self._tip_1 = tip_1
        return SignerPolicy(
            per_tx_cap_lamports=100_000_000,
            aggregate_cap_lamports=500_000_000,
            window_seconds=60,
            max_sign_count=1000,
            allowed_program_ids=frozenset({system, compute_budget, venue, ata_program, spl_token}),
            venue_program_ids=frozenset({venue}),
            pinned_tip_accounts=frozenset({tip_1}),
            ata_program_id=ata_program,
            spl_token_program_id=spl_token,
            venue_spend_decoders={venue: _ZeroSpendDecoder()},
            system_program_id=system,
            compute_budget_program_id=compute_budget,
        )

    def _build_own_ata_tx(self, *, mint: str, blockhash: str, ata: str, extra_key_count: int = 10) -> bytes:
        extra_keys = [_pubkey(f"bench-extra-{i}") for i in range(extra_key_count)]
        account_keys = [self._wallet, self._system, self._compute_budget, self._venue, ata, mint] + extra_keys
        create_account_data = struct.pack("<IQQ", 0, 2_039_280, 165) + b"\x00" * 32
        ix = sw.RawInstruction(program_id=self._system, accounts=(0, 4), data=create_account_data)
        return sw.build_versioned_tx(
            account_keys=account_keys,
            num_required_signatures=1,
            recent_blockhash=blockhash,
            instructions=[ix],
        )

    def test_measured_enforcer_latency_is_recorded_for_the_timeout_default(self) -> None:
        from aats.execution.signer_enforcer import derive_ata

        policy = self._policy()
        mint = _pubkey("bench-mint")
        ata = derive_ata(
            self._wallet, mint, ata_program_id=self._ata_program, spl_token_program_id=self._spl_token
        )

        # ---- Blockhash-retry pattern: SAME (wallet, mint), NEW blockhash each call
        # (the dominant real-world repeat pattern -- fix round 2 MAJOR finding). ----
        ledger = VelocityLedger(window_seconds=60)
        enforcer = Enforcer(policy, ledger)
        retry_times_ms: list[float] = []
        n_retries = 30
        for i in range(n_retries):
            bh = _pubkey(f"bench-retry-blockhash-{i}")
            tx = self._build_own_ata_tx(mint=mint, blockhash=bh, ata=ata)
            t0 = time.perf_counter()
            enforcer.enforce(tx, self._wallet, time.monotonic())
            t1 = time.perf_counter()
            retry_times_ms.append((t1 - t0) * 1000)
        retry_times_ms.sort()
        retry_p50 = retry_times_ms[len(retry_times_ms) // 2]
        retry_p99 = retry_times_ms[min(len(retry_times_ms) - 1, int(len(retry_times_ms) * 0.99))]

        # ---- Cold-cache first call (fresh Enforcer/process each time) -- the
        # absolute worst case: no memoization warmth at all. ----
        cold_times_ms: list[float] = []
        n_cold = 15
        for i in range(n_cold):
            fresh_ledger = VelocityLedger(window_seconds=60)
            fresh_enforcer = Enforcer(policy, fresh_ledger)
            bh = _pubkey(f"bench-cold-blockhash-{i}")
            tx = self._build_own_ata_tx(mint=mint, blockhash=bh, ata=ata)
            t0 = time.perf_counter()
            fresh_enforcer.enforce(tx, self._wallet, time.monotonic())
            t1 = time.perf_counter()
            cold_times_ms.append((t1 - t0) * 1000)
        cold_times_ms.sort()
        cold_p50 = cold_times_ms[len(cold_times_ms) // 2]
        cold_max = cold_times_ms[-1]

        print(
            "\n[signer latency benchmark -- pure-Python fallback, solders NOT installed "
            "in this sandbox]\n"
            f"  retry pattern  (warm ATA cache): p50={retry_p50:.3f}ms  p99={retry_p99:.3f}ms\n"
            f"  cold-cache first call          : p50={cold_p50:.3f}ms  max={cold_max:.3f}ms\n"
            "  See aats/execution/signer_client.py module docstring for how these feed "
            "SIGNER_CLIENT_TIMEOUT_S's default, and the REQUIRED precondition to re-measure "
            "against the real deployed (solders-enabled) signer before devnet/LIVE."
        )

        # Regression guard, deliberately generous (this is a correctness/traceability
        # record, not a tight perf budget -- CI hardware varies): if the warm-cache
        # retry path or the cold-cache path blow WAY past these, something has
        # regressed (e.g. the memoization or the native-path preference silently
        # stopped taking effect).
        assert retry_p50 < 50.0, "warm-cache retry p50 regressed far past the documented benchmark"
        assert cold_max < 500.0, "cold-cache worst case regressed far past the documented benchmark"
