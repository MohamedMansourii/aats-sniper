"""E1 — Devnet live-send validation mode tests.

Enhancement directive E1: add a DEVNET execution target that ACTUALLY SUBMITS
and confirms on Solana DEVNET (worthless SOL), exercising the real
submit→land→confirm→reconcile path behind SOLANA_CLUSTER=devnet|mainnet.

ALL tests here run OFFLINE using MockDevnetRpcClient.  The real devnet RPC
endpoint is NEVER reached in this environment.  The MockDevnetRpcClient proves
the full code path (send→confirm→reconcile) works before a real endpoint
(RPC_DEVNET) is plugged in.

Where the real devnet endpoint plugs in:
    Replace MockDevnetRpcClient with DevnetRpcClient(rpc_url=os.environ["RPC_DEVNET"])
    and set SOLANA_CLUSTER=devnet + RPC_DEVNET in the environment.

Hard rules verified here:
  - DRY_RUN is still the DEFAULT: absent SOLANA_CLUSTER → DRY_RUN, no submit.
  - MAINNET LIVE still gated: SOLANA_CLUSTER=mainnet + live_submit_enabled=False → DRY_RUN.
  - DEVNET is a separate cluster from mainnet: SubmitMode.DEVNET != SubmitMode.LIVE.
  - DevnetSubmitBlocked raised if SOLANA_CLUSTER=devnet but no devnet RPC injected.
  - The safety primitives (circuit breaker / survivable stop / DMS) are NOT modified here:
    the venue contract is unchanged; the safety layer sits above M4 and is unaffected.
  - SimulationVenue still satisfies the ABC (SubmitMode.SIMULATION unchanged).
  - No float money fields anywhere in the devnet path.
  - No secrets/private keys in code or logs.
"""
from __future__ import annotations

import os
from decimal import Decimal

import pytest

from aats.contracts.events import LaunchEvent
from aats.contracts.intents import EntryIntent
from aats.contracts.venue import (
    ExecutionVenue,
    FillResult,
    LandResult,
    SignedTx,
    SubmitMode,
)
from aats.execution import (
    ConfirmResult,
    DevnetSubmitBlocked,
    JitoJupiterVenue,
    MockDevnetRpcClient,
    MockRpcClient,
    MockSignerClient,
    MockSignerClientRefusing,
)
from aats.execution.rpc_client import MockRpcClientRevert
from tests.execution.conftest import (
    _MOCK_MINT,
    _MOCK_PUBKEY,
    _SLOT,
    make_entry_intent,
    make_launch_event,
)


# ===========================================================================
# Fixtures
# ===========================================================================


@pytest.fixture()
def devnet_rpc() -> MockDevnetRpcClient:
    """Offline mock of the Solana devnet RPC."""
    return MockDevnetRpcClient(
        current_slot=_SLOT,
        cu_consumed=180_000,
        confirm_after_polls=1,
        land_succeeds=True,
    )


@pytest.fixture()
def devnet_venue(devnet_rpc: MockDevnetRpcClient) -> JitoJupiterVenue:
    """JitoJupiterVenue wired for devnet mode (offline — MockDevnetRpcClient)."""
    return JitoJupiterVenue(
        wallet_pubkey=_MOCK_PUBKEY,
        rpc_client=devnet_rpc,
        signer_client=MockSignerClient(wallet_pubkey=_MOCK_PUBKEY),
        cluster="devnet",
        # live_submit_enabled is irrelevant for devnet; kept False (default)
        live_submit_enabled=False,
    )


@pytest.fixture()
def entry_intent() -> EntryIntent:
    return make_entry_intent(intent_id="ci-devnet-e1")


@pytest.fixture()
def launch_event() -> LaunchEvent:
    return make_launch_event()


# ===========================================================================
# 1. SubmitMode.DEVNET is a distinct cluster from LIVE and DRY_RUN
# ===========================================================================


class TestDevnetSubmitModeDistinctFromMainnet:
    """E1: DEVNET is a separate cluster — NOT mainnet, NOT DRY_RUN."""

    def test_devnet_venue_has_submit_mode_devnet(
        self, devnet_venue: JitoJupiterVenue
    ) -> None:
        """SOLANA_CLUSTER=devnet → submit_mode == DEVNET (not DRY_RUN, not LIVE)."""
        assert devnet_venue.submit_mode == SubmitMode.DEVNET, (
            f"Expected SubmitMode.DEVNET, got {devnet_venue.submit_mode!r}."
        )

    def test_devnet_submit_mode_is_not_live(
        self, devnet_venue: JitoJupiterVenue
    ) -> None:
        """Devnet is NOT LIVE — it is a separate cluster (worthless SOL)."""
        assert devnet_venue.submit_mode != SubmitMode.LIVE, (
            "SubmitMode.DEVNET must not equal SubmitMode.LIVE. "
            "Devnet uses worthless SOL on a separate cluster; mainnet LIVE is hard-gated."
        )

    def test_devnet_submit_mode_is_not_dry_run(
        self, devnet_venue: JitoJupiterVenue
    ) -> None:
        """Devnet mode DOES submit — it is not DRY_RUN."""
        assert devnet_venue.submit_mode != SubmitMode.DRY_RUN, (
            "SubmitMode.DEVNET must not equal SubmitMode.DRY_RUN. "
            "The devnet path actually submits transactions."
        )

    def test_mainnet_cluster_stays_dry_run_by_default(self) -> None:
        """SOLANA_CLUSTER=mainnet with no live gates → DRY_RUN (unchanged behavior)."""
        venue = JitoJupiterVenue(
            wallet_pubkey=_MOCK_PUBKEY,
            rpc_client=MockRpcClient(),
            signer_client=MockSignerClient(),
            cluster="mainnet",
            live_submit_enabled=False,
        )
        assert venue.submit_mode == SubmitMode.DRY_RUN

    def test_absent_cluster_env_defaults_to_dry_run(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Absent SOLANA_CLUSTER env var → DRY_RUN (safe default, unchanged)."""
        monkeypatch.delenv("SOLANA_CLUSTER", raising=False)
        venue = JitoJupiterVenue(
            wallet_pubkey=_MOCK_PUBKEY,
            rpc_client=MockRpcClient(),
            signer_client=MockSignerClient(),
            # cluster=None → reads SOLANA_CLUSTER env (absent → mainnet → DRY_RUN)
        )
        assert venue.submit_mode == SubmitMode.DRY_RUN, (
            "Absent SOLANA_CLUSTER must default to DRY_RUN — never auto-enable devnet or live."
        )

    def test_solana_cluster_env_var_sets_devnet_mode(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """SOLANA_CLUSTER=devnet env var → DEVNET mode."""
        monkeypatch.setenv("SOLANA_CLUSTER", "devnet")
        venue = JitoJupiterVenue(
            wallet_pubkey=_MOCK_PUBKEY,
            rpc_client=MockDevnetRpcClient(),
            signer_client=MockSignerClient(),
        )
        assert venue.submit_mode == SubmitMode.DEVNET


# ===========================================================================
# 2. Devnet submit → confirm → reconcile (full offline path)
# ===========================================================================


class TestDevnetSubmitConfirmReconcile:
    """E1 core: the full submit→land→confirm→reconcile path against MockDevnetRpcClient."""

    def test_devnet_execute_submits_and_confirms(
        self,
        devnet_venue: JitoJupiterVenue,
        devnet_rpc: MockDevnetRpcClient,
        entry_intent: EntryIntent,
        launch_event: LaunchEvent,
    ) -> None:
        """The devnet path calls send_transaction AND confirm_transaction."""
        result = devnet_venue.execute(entry_intent, launch_event)

        # send_transaction was called (real submit path, not dry_run).
        assert devnet_rpc.send_calls >= 1, (
            f"send_transaction was not called (send_calls={devnet_rpc.send_calls}). "
            "The DEVNET path MUST call send_transaction — it actually submits."
        )

        # confirm_transaction was called (confirms landing on devnet).
        assert devnet_rpc.confirm_calls >= 1, (
            f"confirm_transaction was not called (confirm_calls={devnet_rpc.confirm_calls}). "
            "The DEVNET path MUST poll for confirmation after submitting."
        )

    def test_devnet_execute_returns_landed_fill(
        self,
        devnet_venue: JitoJupiterVenue,
        entry_intent: EntryIntent,
        launch_event: LaunchEvent,
    ) -> None:
        """A successful devnet submit+confirm returns FillResult(landed=True)."""
        result = devnet_venue.execute(entry_intent, launch_event)
        assert isinstance(result, FillResult)
        assert result.landed is True, (
            f"Expected landed=True on successful devnet submit+confirm, got "
            f"landed={result.landed}, reason={result.reason!r}."
        )

    def test_devnet_signature_is_recorded(
        self,
        devnet_venue: JitoJupiterVenue,
        devnet_rpc: MockDevnetRpcClient,
        entry_intent: EntryIntent,
        launch_event: LaunchEvent,
    ) -> None:
        """The devnet signature is returned in the LandResult (for logging)."""
        devnet_venue.execute(entry_intent, launch_event)
        assert len(devnet_rpc.submitted_signatures) >= 1, (
            "No devnet signature was recorded — the submitted_signatures list is empty."
        )
        sig = devnet_rpc.submitted_signatures[0]
        assert sig.startswith(MockDevnetRpcClient.DEVNET_MOCK_SIGNATURE_PREFIX), (
            f"Unexpected signature prefix: {sig[:20]!r}. "
            "MockDevnetRpcClient should produce DevnetMockSig* signatures."
        )

    def test_devnet_fill_money_fields_are_int(
        self,
        devnet_venue: JitoJupiterVenue,
        entry_intent: EntryIntent,
        launch_event: LaunchEvent,
    ) -> None:
        """All money fields on the devnet FillResult are int (never float)."""
        result = devnet_venue.execute(entry_intent, launch_event)
        assert isinstance(result.tip_lamports, int), (
            f"tip_lamports is {type(result.tip_lamports).__name__}, expected int."
        )
        assert isinstance(result.priority_lamports, int)
        assert isinstance(result.tokens_out, int)
        assert isinstance(result.entry_slippage_bps, int)
        # effective_price must be Decimal-parseable (not float)
        assert isinstance(result.effective_price, Decimal)

    def test_devnet_land_direct(
        self,
        devnet_venue: JitoJupiterVenue,
        devnet_rpc: MockDevnetRpcClient,
    ) -> None:
        """land() in DEVNET mode calls send+confirm, returns LandResult with signature."""
        signed_tx = SignedTx(
            serialized_b64="devnet_stub_tx_b64",
            signer_pubkey=_MOCK_PUBKEY,
            intent_kind="ENTRY",
            mint=_MOCK_MINT,
            client_intent_id="ci-devnet-land-direct",
        )
        land = devnet_venue.land(signed_tx, tip_lamports=200_000, cu_price=100_000)
        assert isinstance(land, LandResult)
        assert land.submitted is True, (
            f"land() in DEVNET mode must submit — got submitted={land.submitted}, "
            f"reason={land.reason!r}."
        )
        assert land.signature is not None, "Devnet land must return a non-None signature."
        assert land.reason == "devnet_landed", (
            f"Expected reason='devnet_landed', got {land.reason!r}."
        )
        # send_transaction was called.
        assert devnet_rpc.send_calls >= 1
        # confirm_transaction was called.
        assert devnet_rpc.confirm_calls >= 1


# ===========================================================================
# 3. Failed-land path on devnet (simulate a failed send → retry)
# ===========================================================================


class TestDevnetFailedLandAndRetry:
    """E1: failed-land path and blockhash-expiry retry on devnet."""

    def test_devnet_failed_send_returns_not_landed(self) -> None:
        """If send_transaction fails on devnet, FillResult.landed=False."""
        fail_rpc = MockDevnetRpcClient(
            current_slot=_SLOT,
            cu_consumed=180_000,
            land_succeeds=False,
            fail_reason="blockhash_expired",
        )
        venue = JitoJupiterVenue(
            wallet_pubkey=_MOCK_PUBKEY,
            rpc_client=fail_rpc,
            signer_client=MockSignerClient(wallet_pubkey=_MOCK_PUBKEY),
            cluster="devnet",
        )
        result = venue.execute(make_entry_intent(intent_id="ci-devnet-fail"), make_launch_event())
        assert result.landed is False, (
            f"Expected landed=False on devnet failed-land, got {result.landed!r}."
        )
        assert result.tokens_out == 0, (
            "tokens_out must be 0 on failed devnet land (atomic — no partial fill)."
        )

    def test_devnet_blockhash_expiry_triggers_retry(self) -> None:
        """Blockhash expiry on devnet triggers re-quote+rebuild+re-sign+re-simulate+retry."""
        class _DevnetBlockhashExpiryRpc(MockDevnetRpcClient):
            """Fails first send attempt with blockhash_expired; succeeds on second."""

            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self._send_attempt = 0
                self._blockhash_call_count = 0
                self.blockhashes_issued: list[str] = []
                self.sent_bytes: list[str] = []

            def get_latest_blockhash(self) -> str:
                self._blockhash_call_count += 1
                bh = f"DevnetFreshBlockhash{self._blockhash_call_count:04d}11111111111111111111"
                self.blockhashes_issued.append(bh)
                return bh

            def send_transaction(self, signed_tx_b64: str):
                self.send_calls += 1
                self._send_attempt += 1
                self.sent_bytes.append(signed_tx_b64)
                if self._send_attempt == 1:
                    from aats.execution.rpc_client import LandAttemptResult
                    return LandAttemptResult(
                        submitted=False,
                        signature=None,
                        land_slot=None,
                        reason="blockhash_expired",
                    )
                # Second attempt: succeed.
                sig = f"DevnetRetryLandedSig{self._send_attempt:04d}111111111111111111111111111111"
                self.submitted_signatures.append(sig)
                self._poll_count[sig] = 0
                from aats.execution.rpc_client import LandAttemptResult
                return LandAttemptResult(
                    submitted=True,
                    signature=sig,
                    land_slot=None,
                    reason="submitted_pending_confirm",
                )

        expiry_rpc = _DevnetBlockhashExpiryRpc(
            current_slot=_SLOT,
            cu_consumed=180_000,
            confirm_after_polls=1,
        )
        venue = JitoJupiterVenue(
            wallet_pubkey=_MOCK_PUBKEY,
            rpc_client=expiry_rpc,
            signer_client=MockSignerClient(wallet_pubkey=_MOCK_PUBKEY),
            cluster="devnet",
        )
        result = venue.execute(
            make_entry_intent(intent_id="ci-devnet-blockhash-retry"),
            make_launch_event(),
        )

        # At least 2 send attempts.
        assert expiry_rpc.send_calls >= 2, (
            f"Expected at least 2 send_transaction calls (1 expiry + 1 retry), "
            f"got {expiry_rpc.send_calls}."
        )

        # The retry used DIFFERENT bytes (fresh blockhash was embedded).
        assert len(expiry_rpc.sent_bytes) >= 2
        assert expiry_rpc.sent_bytes[0] != expiry_rpc.sent_bytes[1], (
            "Devnet retry re-sent byte-identical bytes — the blockhash was NOT refreshed. "
            "Each retry MUST rebuild the tx with a fresh blockhash."
        )

        # get_latest_blockhash() was called more than once.
        assert expiry_rpc._blockhash_call_count >= 2, (
            f"get_latest_blockhash() called only {expiry_rpc._blockhash_call_count} time(s); "
            "retry must fetch a fresh blockhash."
        )

        # Final result: landed.
        assert result.landed is True


# ===========================================================================
# 4. DRY_RUN is still the default — devnet does NOT affect the DRY-RUN gate
# ===========================================================================


class TestDryRunStillDefaultAfterDevnetAddition:
    """Safety: DRY_RUN is unchanged as the default; the safety primitives are unaffected."""

    def test_dry_run_venue_never_submits_after_devnet_added(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """DRY_RUN venues still never call send_transaction."""
        monkeypatch.setenv("DRY_RUN_ENABLED", "true")
        monkeypatch.delenv("SOLANA_CLUSTER", raising=False)
        mock_rpc = MockRpcClient(current_slot=_SLOT, cu_consumed=180_000)
        venue = JitoJupiterVenue(
            wallet_pubkey=_MOCK_PUBKEY,
            rpc_client=mock_rpc,
            signer_client=MockSignerClient(),
            live_submit_enabled=False,
        )
        assert venue.submit_mode == SubmitMode.DRY_RUN
        result = venue.execute(
            make_entry_intent(intent_id="ci-dry-run-post-e1"),
            make_launch_event(),
        )
        assert mock_rpc.send_calls == 0, (
            f"DRY_RUN called send_transaction {mock_rpc.send_calls} time(s) — "
            "HARD VIOLATION: DRY_RUN must never call send_transaction (FR-039)."
        )
        assert result.landed is False
        assert result.reason == "dry_run"

    def test_mainnet_live_still_gated(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Mainnet LIVE still requires DRY_RUN_ENABLED=false + live_submit_enabled=True."""
        monkeypatch.setenv("DRY_RUN_ENABLED", "true")
        monkeypatch.delenv("SOLANA_CLUSTER", raising=False)
        venue = JitoJupiterVenue(
            wallet_pubkey=_MOCK_PUBKEY,
            rpc_client=MockRpcClient(),
            signer_client=MockSignerClient(),
            live_submit_enabled=True,   # gate 3 cleared; but gate 2 is not
        )
        # DRY_RUN_ENABLED=true means still DRY_RUN.
        assert venue.submit_mode == SubmitMode.DRY_RUN, (
            "Mainnet LIVE gate 2 (DRY_RUN_ENABLED=true) must block live submit."
        )

    def test_devnet_mode_does_not_unlock_mainnet_live(self) -> None:
        """SOLANA_CLUSTER=devnet must NOT unlock mainnet LIVE — they are independent gates."""
        venue = JitoJupiterVenue(
            wallet_pubkey=_MOCK_PUBKEY,
            rpc_client=MockDevnetRpcClient(),
            signer_client=MockSignerClient(),
            cluster="devnet",
            live_submit_enabled=True,  # mainnet gate 3 cleared, but cluster=devnet
        )
        # submit_mode is DEVNET (cluster wins), not LIVE.
        assert venue.submit_mode == SubmitMode.DEVNET
        assert venue.submit_mode != SubmitMode.LIVE, (
            "cluster=devnet must not enable mainnet LIVE. These are independent gates."
        )


# ===========================================================================
# 5. DevnetSubmitBlocked when SOLANA_CLUSTER=devnet but no devnet RPC
# ===========================================================================


class TestDevnetSubmitBlockedIfNoRpc:
    """E1 safety: if cluster=devnet but no devnet-capable RPC is injected, block submit."""

    def test_devnet_blocked_without_devnet_rpc(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """SOLANA_CLUSTER=devnet + plain MockRpcClient (no confirm_transaction) → blocked."""
        monkeypatch.delenv("RPC_DEVNET", raising=False)
        venue = JitoJupiterVenue(
            wallet_pubkey=_MOCK_PUBKEY,
            # MockRpcClient does NOT have confirm_transaction → devnet gate fails.
            rpc_client=MockRpcClient(current_slot=_SLOT),
            signer_client=MockSignerClient(),
            cluster="devnet",
        )
        signed_tx = SignedTx(
            serialized_b64="stub",
            signer_pubkey=_MOCK_PUBKEY,
            intent_kind="ENTRY",
            mint=_MOCK_MINT,
            client_intent_id="ci-devnet-no-rpc",
        )
        # land() in DEVNET mode with a non-devnet RPC must raise DevnetSubmitBlocked.
        with pytest.raises(DevnetSubmitBlocked):
            venue.land(signed_tx, tip_lamports=200_000, cu_price=100_000)


# ===========================================================================
# 6. Pre-send simulation is mandatory on the devnet path too
# ===========================================================================


class TestPreSendSimOnDevnetPath:
    """Simulate-before-submit is mandatory on devnet just as on mainnet."""

    def test_reverting_tx_caught_before_devnet_submit(self) -> None:
        """A deliberately reverting tx is caught by simulate — NOT sent to devnet."""
        revert_rpc = MockRpcClientRevert(revert_reason="honeypot_detected_devnet")
        # We need a venue that uses the revert-sim RPC but also supports devnet.
        # We compose: revert rpc for simulate, but the devnet path never reached.
        # Since MockRpcClientRevert inherits MockRpcClient (no confirm_transaction),
        # we use it for simulate and check the venue aborts before _send_and_confirm_devnet.
        # The venue will raise DevnetSubmitBlocked when it tries to call confirm_transaction
        # on a non-devnet RPC — but the simulate revert should abort FIRST.
        venue = JitoJupiterVenue(
            wallet_pubkey=_MOCK_PUBKEY,
            rpc_client=revert_rpc,
            signer_client=MockSignerClient(),
            cluster="devnet",
        )
        result = venue.execute(
            make_entry_intent(intent_id="ci-devnet-sim-revert"),
            make_launch_event(),
        )

        # Simulate was called.
        assert revert_rpc.simulate_calls >= 1, (
            "simulateTransaction was not called — pre-send simulation is mandatory on devnet too."
        )
        # send_transaction was NOT called (caught by simulate revert).
        assert revert_rpc.send_calls == 0, (
            f"send_transaction was called {revert_rpc.send_calls} time(s) after a simulate "
            "revert on the devnet path — HARD VIOLATION: a reverting tx must NEVER be submitted."
        )
        assert result.landed is False

    def test_simulate_called_in_devnet_mode(
        self,
        devnet_venue: JitoJupiterVenue,
        devnet_rpc: MockDevnetRpcClient,
        entry_intent: EntryIntent,
        launch_event: LaunchEvent,
    ) -> None:
        """simulateTransaction is called before the devnet submit."""
        devnet_venue.execute(entry_intent, launch_event)
        assert devnet_rpc.simulate_calls >= 1, (
            "simulateTransaction was not called in devnet mode — pre-send sim is mandatory."
        )


# ===========================================================================
# 7. Idempotency on devnet path
# ===========================================================================


class TestDevnetIdempotency:
    """Duplicate sends under the same intent_id do not double-land on devnet."""

    def test_duplicate_devnet_intent_id_does_not_double_submit(
        self,
        devnet_venue: JitoJupiterVenue,
        devnet_rpc: MockDevnetRpcClient,
        entry_intent: EntryIntent,
        launch_event: LaunchEvent,
    ) -> None:
        """Sending twice with the same intent_id on devnet must NOT double-land."""
        result1 = devnet_venue.execute(entry_intent, launch_event)
        send_after_first = devnet_rpc.send_calls

        # Second call with the SAME intent_id.
        result2 = devnet_venue.execute(entry_intent, launch_event)
        send_after_second = devnet_rpc.send_calls

        assert result1.landed is True, "First devnet attempt should have landed."
        assert result2.landed is False
        assert result2.reason == "idempotency_key_conflict"
        assert send_after_second == send_after_first, (
            f"Duplicate intent_id triggered an additional send on devnet "
            f"({send_after_second - send_after_first} extra call(s))."
        )


# ===========================================================================
# 8. SimulationVenue still satisfies the ABC (not broken by E1)
# ===========================================================================


class TestSimulationVenueUnaffectedByE1:
    """E1 must not break SimulationVenue or the DRY_RUN safety contract."""

    def test_simulation_venue_submit_mode_still_simulation(self) -> None:
        from aats.contracts.venue import SimulationVenue
        import random
        sim = SimulationVenue(rng=random.Random(42), market_tip_lamports=200_000)
        assert sim.submit_mode == SubmitMode.SIMULATION, (
            f"SimulationVenue.submit_mode is {sim.submit_mode!r} after E1 — must still be SIMULATION."
        )

    def test_simulation_venue_land_never_submits(self) -> None:
        from aats.contracts.venue import SimulationVenue
        import random
        sim = SimulationVenue(rng=random.Random(42), market_tip_lamports=200_000)
        signed_tx = SignedTx(
            serialized_b64="",
            signer_pubkey="sim_pubkey",
            intent_kind="ENTRY",
            mint=_MOCK_MINT,
            client_intent_id="ci-e1-sim-land",
        )
        land = sim.land(signed_tx, tip_lamports=0, cu_price=0)
        assert land.submitted is False
        assert land.reason == "simulation"

    def test_simulation_venue_is_still_abc_compliant(self) -> None:
        from aats.contracts.venue import SimulationVenue
        import random
        sim = SimulationVenue(rng=random.Random(42), market_tip_lamports=200_000)
        assert isinstance(sim, ExecutionVenue)


# ===========================================================================
# 9. MockDevnetRpcClient confirmation polling mechanics
# ===========================================================================


class TestMockDevnetRpcClientConfirmPolling:
    """Proves the confirm polling loop (polls until confirm_after_polls reached)."""

    def test_confirm_resolves_after_n_polls(self) -> None:
        """MockDevnetRpcClient.confirm_transaction returns confirmed after N polls."""
        rpc = MockDevnetRpcClient(confirm_after_polls=3)
        sig = "TestSig11111111111111111111111111111111111111111111"
        rpc._poll_count[sig] = 0  # simulate a submitted sig

        # 1st and 2nd poll: not yet confirmed.
        r1 = rpc.confirm_transaction(sig)
        assert r1.confirmed is False
        assert r1.polls == 1

        r2 = rpc.confirm_transaction(sig)
        assert r2.confirmed is False
        assert r2.polls == 2

        # 3rd poll: confirmed.
        r3 = rpc.confirm_transaction(sig)
        assert r3.confirmed is True
        assert r3.polls == 3
        assert r3.land_slot is not None

    def test_confirm_unknown_sig_returns_not_confirmed(self) -> None:
        """Confirming an unknown signature returns ConfirmResult(confirmed=False)."""
        rpc = MockDevnetRpcClient()
        r = rpc.confirm_transaction("UnknownSig11111111111111111111111111111111111")
        assert r.confirmed is False
        assert r.error == "signature_not_found"

    def test_confirm_result_money_invariants(self) -> None:
        """ConfirmResult.land_slot is int or None (never float)."""
        result = ConfirmResult(confirmed=True, land_slot=300_000_042, polls=1)
        assert isinstance(result.land_slot, int)
        assert isinstance(result.polls, int)


# ===========================================================================
# 10. No secrets in devnet code path
# ===========================================================================


class TestNoSecretsInDevnetPath:
    """No private keys, seeds, or secrets appear in the devnet code path."""

    def test_no_private_key_in_devnet_venue_code(self) -> None:
        """Grep the devnet code path for secret markers."""
        import pathlib, re

        # Files to check: the venue and the rpc_client module.
        files_to_check = [
            pathlib.Path(__file__).parent.parent.parent / "aats" / "execution" / "jito_jupiter_venue.py",
            pathlib.Path(__file__).parent.parent.parent / "aats" / "execution" / "rpc_client.py",
        ]
        forbidden = ("PRIVATE_KEY", "SECRET_KEY", "SEED_PHRASE", "MNEMONIC", "KEYPAIR_JSON")
        for f in files_to_check:
            content = f.read_text(encoding="utf-8", errors="ignore").upper()
            for token in forbidden:
                assert token not in content, (
                    f"Forbidden token {token!r} found in {f.name}. "
                    "No private key material is permitted in the execution module."
                )

    def test_devnet_fill_result_has_no_key_material(
        self,
        devnet_venue: JitoJupiterVenue,
        entry_intent: EntryIntent,
        launch_event: LaunchEvent,
    ) -> None:
        """The FillResult from a devnet execute contains no key material."""
        result = devnet_venue.execute(entry_intent, launch_event)
        result_str = str(result).lower()
        for forbidden in ("private", "secret", "mnemonic", "seed", "keypair"):
            assert forbidden not in result_str, (
                f"Key material marker '{forbidden}' found in FillResult repr — "
                "the execution result must never contain secret material."
            )


# ===========================================================================
# 11. BLOCKER-1 regression — unconfirmed devnet tx must NOT reconcile as filled
# ===========================================================================


class TestUnconfirmedDevnetNotFilled:
    """BLOCKER-1 regression: devnet submit succeeds but confirm times out.

    The intent MUST reconcile as FillResult.landed=False, reason must NOT be
    "filled", and the intent_id MUST NOT enter the idempotency set (so the
    unconfirmed intent stays retryable).
    """

    def test_unconfirmed_devnet_tx_reconciles_as_not_landed(self) -> None:
        """Devnet submit returns a signature, but confirm_transaction never confirms.

        Expected outcome:
          - result.landed == False
          - result.reason != "filled"  (must be "devnet_confirm_failed:*" or similar)
          - A second call with the SAME intent_id is NOT blocked by idempotency
            (the unconfirmed intent must remain retryable).
        """
        # Build a mock devnet RPC that sends successfully but NEVER confirms.
        class _NeverConfirmDevnetRpc(MockDevnetRpcClient):
            """send_transaction succeeds; confirm_transaction always returns confirmed=False."""

            def confirm_transaction(
                self,
                signature: str,
                *,
                max_polls: int = 30,
                poll_interval_s: float = 0.5,
            ) -> ConfirmResult:
                self.confirm_calls += 1
                # Always return unconfirmed — simulates timeout.
                return ConfirmResult(
                    confirmed=False,
                    land_slot=None,
                    error="confirmation_timeout",
                    polls=max_polls,
                )

        never_confirm_rpc = _NeverConfirmDevnetRpc(
            current_slot=_SLOT,
            cu_consumed=180_000,
            land_succeeds=True,
        )
        venue = JitoJupiterVenue(
            wallet_pubkey=_MOCK_PUBKEY,
            rpc_client=never_confirm_rpc,
            signer_client=MockSignerClient(wallet_pubkey=_MOCK_PUBKEY),
            cluster="devnet",
        )

        intent = make_entry_intent(intent_id="ci-devnet-unconfirmed-regression")
        event = make_launch_event()

        # First call: tx sent, confirm times out.
        result = venue.execute(intent, event)

        # BLOCKER-1 assertion 1: unconfirmed tx must NOT reconcile as a fill.
        assert result.landed is False, (
            f"BLOCKER-1: unconfirmed devnet tx must reconcile as landed=False, "
            f"got landed={result.landed!r}."
        )

        # BLOCKER-1 assertion 2: reason must NOT be "filled".
        assert result.reason != "filled", (
            f"BLOCKER-1: reconcile() set reason='filled' on an unconfirmed devnet tx "
            f"(reason={result.reason!r}). An unconfirmed tx is NOT a fill."
        )

        # BLOCKER-1 assertion 3: reason should reflect the confirmation failure.
        assert "devnet_confirm_failed" in result.reason, (
            f"BLOCKER-1: expected reason to contain 'devnet_confirm_failed', "
            f"got {result.reason!r}."
        )

        # BLOCKER-1 assertion 4: the intent_id must NOT be in the idempotency set.
        # A second call with the same intent_id must NOT return "idempotency_key_conflict"
        # — the intent is retryable because the land never confirmed.
        send_after_first = never_confirm_rpc.send_calls
        result2 = venue.execute(intent, event)
        assert result2.reason != "idempotency_key_conflict", (
            f"BLOCKER-1: unconfirmed intent entered the idempotency set — "
            f"it must remain retryable (got reason={result2.reason!r} on 2nd call)."
        )
        # A retry was attempted (new send call).
        assert never_confirm_rpc.send_calls > send_after_first, (
            "BLOCKER-1: the second call with the same intent_id made no send attempt, "
            "but the intent should be retryable (not blocked by idempotency)."
        )

    def test_confirmed_devnet_tx_enters_idempotency_set(self) -> None:
        """Counterpart: a CONFIRMED devnet tx MUST enter the idempotency set.

        After a successful devnet land, a second call with the same intent_id
        MUST return idempotency_key_conflict (no double-land).
        """
        # Use the standard mock that confirms after 1 poll.
        confirmed_rpc = MockDevnetRpcClient(
            current_slot=_SLOT,
            cu_consumed=180_000,
            confirm_after_polls=1,
            land_succeeds=True,
        )
        venue = JitoJupiterVenue(
            wallet_pubkey=_MOCK_PUBKEY,
            rpc_client=confirmed_rpc,
            signer_client=MockSignerClient(wallet_pubkey=_MOCK_PUBKEY),
            cluster="devnet",
        )
        intent = make_entry_intent(intent_id="ci-devnet-confirmed-idempotent")
        event = make_launch_event()

        result1 = venue.execute(intent, event)
        assert result1.landed is True, (
            f"Expected landed=True on successful devnet confirm, got {result1.landed!r}."
        )

        result2 = venue.execute(intent, event)
        assert result2.reason == "idempotency_key_conflict", (
            f"Confirmed devnet intent should block a second send "
            f"(got reason={result2.reason!r})."
        )
        assert result2.landed is False
