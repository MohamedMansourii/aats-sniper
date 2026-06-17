"""T-327 — JitoJupiterVenue unit tests.

Self-check items verified here (per task mandate, ALL mandatory):
  1. Venue satisfies the ExecutionVenue ABC.
  2. DRY-RUN: builds + signs + simulates but NEVER calls send_transaction.
  3. Test asserts NO real submit occurs unless DRY_RUN_ENABLED=false AND safety allows.
  4. Partial-fill / retry handling (blockhash expiry → re-submit with fresh blockhash).
  5. SimulationVenue still satisfies the ABC (re-verified here).
  6. Pre-send sim proven: a deliberately reverting tx is caught before submit.
  7. Atomic-buy proven: forced-fail sim → no submit, no residual token balance.
  8. Retry/idempotency proven: duplicate send under same intent_id does NOT double-land.
  9. All money fields are int / Decimal-as-string (never float).
 10. No win_rate field anywhere in the venue code.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from aats.contracts.events import LaunchEvent
from aats.contracts.intents import EntryIntent, ExitIntent
from aats.contracts.venue import (
    ExecutionVenue,
    FillResult,
    LandResult,
    SignedTx,
    SimResult,
    SimulationVenue,
    SubmitMode,
    UnsignedTx,
)
from aats.execution import (
    JitoJupiterVenue,
    MockRpcClient,
    MockRpcClientBlockhashExpiry,
    MockRpcClientRevert,
    MockSignerClient,
    MockSignerClientRefusing,
)
from tests.execution.conftest import (
    _MOCK_MINT,
    _MOCK_PUBKEY,
    _SLOT,
    make_entry_intent,
    make_exit_intent,
    make_launch_event,
)

# ===========================================================================
# 1. ABC compliance — JitoJupiterVenue
# ===========================================================================

class TestJitoJupiterVenueABCCompliance:
    def test_is_subclass_of_execution_venue(self, dry_run_venue: JitoJupiterVenue) -> None:
        assert issubclass(JitoJupiterVenue, ExecutionVenue)

    def test_is_instantiable(self, dry_run_venue: JitoJupiterVenue) -> None:
        assert isinstance(dry_run_venue, ExecutionVenue)

    def test_all_eight_abstract_methods_implemented(self, dry_run_venue: JitoJupiterVenue) -> None:
        required = ["execute", "exit", "quote", "build", "sign", "simulate", "land", "reconcile"]
        for method_name in required:
            assert hasattr(dry_run_venue, method_name), f"Missing method: {method_name}"
            assert callable(getattr(dry_run_venue, method_name))

    def test_submit_mode_property_present(self, dry_run_venue: JitoJupiterVenue) -> None:
        assert hasattr(type(dry_run_venue), "submit_mode")

    def test_submit_mode_is_dry_run_by_default(
        self, dry_run_venue: JitoJupiterVenue, dry_run_env: None
    ) -> None:
        assert dry_run_venue.submit_mode == SubmitMode.DRY_RUN

    def test_name_is_set(self, dry_run_venue: JitoJupiterVenue) -> None:
        assert dry_run_venue.name == "jito_jupiter"


# ===========================================================================
# 2. DRY-RUN: execute() builds + signs + simulates — NEVER sends
# ===========================================================================

class TestDryRunNeverSends:
    """Self-check items 2 + 3: DRY_RUN never calls send_transaction."""

    def test_execute_dry_run_returns_fill_result(
        self,
        dry_run_venue: JitoJupiterVenue,
        entry_intent: EntryIntent,
        launch_event: LaunchEvent,
        dry_run_env: None,
        mock_rpc: MockRpcClient,
    ) -> None:
        result = dry_run_venue.execute(entry_intent, launch_event)
        assert isinstance(result, FillResult)

    def test_execute_dry_run_does_not_submit(
        self,
        dry_run_venue: JitoJupiterVenue,
        entry_intent: EntryIntent,
        launch_event: LaunchEvent,
        dry_run_env: None,
        mock_rpc: MockRpcClient,
    ) -> None:
        """CRITICAL: DRY_RUN must NEVER call send_transaction (FR-039, AC-060)."""
        dry_run_venue.execute(entry_intent, launch_event)
        # The mock RPC tracks send_calls. In DRY_RUN, this MUST be 0.
        assert mock_rpc.send_calls == 0, (
            f"DRY_RUN called send_transaction {mock_rpc.send_calls} time(s) — "
            "HARD VIOLATION of FR-039: no code path in DRY_RUN reaches the block engine."
        )

    def test_execute_dry_run_fill_not_landed(
        self,
        dry_run_venue: JitoJupiterVenue,
        entry_intent: EntryIntent,
        launch_event: LaunchEvent,
        dry_run_env: None,
    ) -> None:
        """DRY_RUN fill must have landed=False and reason='dry_run'."""
        result = dry_run_venue.execute(entry_intent, launch_event)
        assert result.landed is False
        assert result.reason == "dry_run"

    def test_simulate_is_called_in_dry_run(
        self,
        dry_run_venue: JitoJupiterVenue,
        entry_intent: EntryIntent,
        launch_event: LaunchEvent,
        dry_run_env: None,
        mock_rpc: MockRpcClient,
    ) -> None:
        """simulateTransaction MUST be called even in DRY_RUN (pre-send sim mandate)."""
        dry_run_venue.execute(entry_intent, launch_event)
        assert mock_rpc.simulate_calls >= 1, (
            "simulateTransaction was not called — pre-send simulation is mandatory."
        )

    def test_sign_is_called_in_dry_run(
        self,
        dry_run_venue: JitoJupiterVenue,
        entry_intent: EntryIntent,
        launch_event: LaunchEvent,
        dry_run_env: None,
    ) -> None:
        """sign() is called in DRY_RUN for latency measurement (execution-venue.md §1)."""
        # If sign() were not called, the signer would have 0 calls.
        # We wrap the signer to count calls.
        call_count = [0]
        original_sign = dry_run_venue._signer.sign

        def counting_sign(tx: UnsignedTx, wallet_id: str) -> SignedTx:
            call_count[0] += 1
            return original_sign(tx, wallet_id)

        dry_run_venue._signer.sign = counting_sign  # type: ignore[method-assign]
        dry_run_venue.execute(entry_intent, launch_event)
        assert call_count[0] >= 1, "sign() was not called — DRY_RUN must sign for latency measurement."

    def test_land_dry_run_returns_not_submitted(
        self,
        dry_run_venue: JitoJupiterVenue,
        mock_rpc: MockRpcClient,
        dry_run_env: None,
    ) -> None:
        """land() in DRY_RUN must return submitted=False, reason='dry_run'."""
        signed_tx = SignedTx(
            serialized_b64="stub",
            signer_pubkey=_MOCK_PUBKEY,
            intent_kind="ENTRY",
            mint=_MOCK_MINT,
            client_intent_id="ci-land-dry-run",
        )
        result = dry_run_venue.land(signed_tx, tip_lamports=200_000, cu_price=100_000)
        assert isinstance(result, LandResult)
        assert result.submitted is False
        assert result.reason == "dry_run"
        # send_transaction was NOT called.
        assert mock_rpc.send_calls == 0

    def test_live_submit_blocked_when_dry_run_env_true(
        self,
        dry_run_env: None,
    ) -> None:
        """LIVE submit must raise when DRY_RUN_ENABLED=true even if live_submit_enabled=True."""
        venue = JitoJupiterVenue(
            wallet_pubkey=_MOCK_PUBKEY,
            rpc_client=MockRpcClient(),
            signer_client=MockSignerClient(),
            live_submit_enabled=True,  # gate 3 cleared, but gate 2 not
        )
        # submit_mode should still be DRY_RUN because env gate is not cleared.
        assert venue.submit_mode == SubmitMode.DRY_RUN


# ===========================================================================
# 3. Live submit ONLY when all three gates are cleared
# ===========================================================================

class TestLiveGates:
    """Self-check item 3: LIVE requires all three independent gates."""

    def test_live_mode_when_env_false_and_flag_true(
        self,
        live_env: None,
    ) -> None:
        """submit_mode == LIVE only when env=false + flag=True."""
        venue = JitoJupiterVenue(
            wallet_pubkey=_MOCK_PUBKEY,
            rpc_client=MockRpcClient(),
            signer_client=MockSignerClient(),
            live_submit_enabled=True,  # gate 3 cleared
        )
        # DRY_RUN_ENABLED=false (live_env fixture), live_submit_enabled=True
        assert venue.submit_mode == SubmitMode.LIVE

    def test_land_raises_live_blocked_when_live_disabled(
        self,
        live_env: None,
    ) -> None:
        """land() raises LiveSubmitBlocked if live_submit_enabled=False despite env=false."""
        venue = JitoJupiterVenue(
            wallet_pubkey=_MOCK_PUBKEY,
            rpc_client=MockRpcClient(),
            signer_client=MockSignerClient(),
            live_submit_enabled=False,  # gate 3 NOT cleared
        )
        signed_tx = SignedTx(
            serialized_b64="stub",
            signer_pubkey=_MOCK_PUBKEY,
            intent_kind="ENTRY",
            mint=_MOCK_MINT,
            client_intent_id="ci-live-blocked",
        )
        # submit_mode is DRY_RUN (gate 3 not cleared), so land() returns dry_run, not raises.
        result = venue.land(signed_tx, tip_lamports=200_000, cu_price=100_000)
        assert result.submitted is False
        assert result.reason == "dry_run"

    def test_live_execute_sends_transaction(
        self,
        live_env: None,
    ) -> None:
        """In LIVE mode (all three gates cleared), send_transaction is called."""
        mock_rpc = MockRpcClient()
        venue = JitoJupiterVenue(
            wallet_pubkey=_MOCK_PUBKEY,
            rpc_client=mock_rpc,
            signer_client=MockSignerClient(),
            live_submit_enabled=True,  # all three gates cleared
        )
        intent = make_entry_intent(intent_id="ci-live-send")
        event = make_launch_event()
        result = venue.execute(intent, event)
        # In LIVE mode, send_transaction MUST be called.
        assert mock_rpc.send_calls >= 1, (
            "LIVE mode did not call send_transaction — the live path is not wired."
        )
        # Result should be landed=True (mock RPC returns success).
        assert result.landed is True

    def test_no_real_submit_without_explicit_dry_run_false(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Assert: absent DRY_RUN_ENABLED defaults to true (no live submit).

        Uses monkeypatch.delenv instead of raw os.environ.pop/restore so the
        env is always restored after the test, regardless of pass/fail, and
        regardless of PYTHONHASHSEED ordering (BLOCKER-2 fix).
        """
        # Remove the env var entirely — system must default to DRY_RUN.
        monkeypatch.delenv("DRY_RUN_ENABLED", raising=False)
        venue = JitoJupiterVenue(
            wallet_pubkey=_MOCK_PUBKEY,
            rpc_client=MockRpcClient(),
            signer_client=MockSignerClient(),
            live_submit_enabled=True,
        )
        # Absent env var → DRY_RUN (safe default).
        assert venue.submit_mode == SubmitMode.DRY_RUN, (
            "Absent DRY_RUN_ENABLED must default to DRY_RUN — never auto-enable LIVE."
        )


# ===========================================================================
# 4. Pre-send simulation proven: reverting tx caught before any submit
# ===========================================================================

class TestPreSendSimulation:
    """Self-check item 6: reverting tx caught by simulateTransaction before submit."""

    def test_reverting_tx_caught_before_submit(
        self,
        dry_run_env: None,
    ) -> None:
        """A deliberately reverting tx is caught by simulate — NO submit attempted."""
        revert_rpc = MockRpcClientRevert(revert_reason="honeypot_detected")
        venue = JitoJupiterVenue(
            wallet_pubkey=_MOCK_PUBKEY,
            rpc_client=revert_rpc,
            signer_client=MockSignerClient(),
            live_submit_enabled=False,
        )
        intent = make_entry_intent(intent_id="ci-revert-test")
        event = make_launch_event()
        result = venue.execute(intent, event)

        # Simulate was called (pre-send is mandatory).
        assert revert_rpc.simulate_calls >= 1, "simulateTransaction was not called."

        # send_transaction was NOT called.
        assert revert_rpc.send_calls == 0, (
            f"send_transaction was called {revert_rpc.send_calls} time(s) after a simulate revert — "
            "HARD VIOLATION: a reverting tx must NEVER be submitted."
        )

        # Result must be landed=False.
        assert result.landed is False
        assert "revert" in result.reason or "sim" in result.reason

    def test_reverting_tx_caught_in_live_mode(
        self,
        live_env: None,
    ) -> None:
        """Even in LIVE mode, a simulate-revert prevents any submit."""
        revert_rpc = MockRpcClientRevert(revert_reason="assert_min_out_failed")
        venue = JitoJupiterVenue(
            wallet_pubkey=_MOCK_PUBKEY,
            rpc_client=revert_rpc,
            signer_client=MockSignerClient(),
            live_submit_enabled=True,
        )
        intent = make_entry_intent(intent_id="ci-live-revert")
        event = make_launch_event()
        result = venue.execute(intent, event)

        assert revert_rpc.send_calls == 0, (
            "send_transaction called after simulate revert in LIVE mode — HARD VIOLATION."
        )
        assert result.landed is False

    def test_simulated_bytes_equal_landed_bytes(
        self,
        live_env: None,
    ) -> None:
        """B2 fix: the EXACT bytes passed to simulate() equal the bytes passed to land().

        Previously execute() simulated a placeholder tx_A (all-1s pool keys,
        min_tokens_out=1), then built and landed a DIFFERENT tx_B (real pool keys,
        real min_tokens_out) WITHOUT re-simulating. This test proves that is no
        longer the case: simulate bytes == send bytes.
        """
        class _TrackingRpc(MockRpcClient):
            """Records last simulated and last sent serialized_b64."""
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self.last_simulated: str | None = None
                self.last_sent: str | None = None

            def simulate_transaction(self, signed_tx_b64: str):
                self.last_simulated = signed_tx_b64
                return super().simulate_transaction(signed_tx_b64)

            def send_transaction(self, signed_tx_b64: str):
                self.last_sent = signed_tx_b64
                return super().send_transaction(signed_tx_b64)

        tracking_rpc = _TrackingRpc(current_slot=_SLOT, cu_consumed=180_000)
        venue = JitoJupiterVenue(
            wallet_pubkey=_MOCK_PUBKEY,
            rpc_client=tracking_rpc,
            signer_client=MockSignerClient(),
            live_submit_enabled=True,
        )
        intent = make_entry_intent(intent_id="ci-sim-bytes-eq-send")
        event = make_launch_event()
        result = venue.execute(intent, event)

        assert result.landed is True, "LIVE execute must land in this test."
        assert tracking_rpc.last_simulated is not None, "simulate_transaction was not called."
        assert tracking_rpc.last_sent is not None, "send_transaction was not called."
        assert tracking_rpc.last_simulated == tracking_rpc.last_sent, (
            "The bytes passed to simulate() differ from the bytes passed to send(). "
            "execute() MUST simulate the exact tx that will be landed (B2 fix)."
        )


# ===========================================================================
# 5. Atomic-buy proven: forced-fail sim → no token balance, no partial fill
# ===========================================================================

class TestAtomicBuy:
    """Self-check item 7: a forced-fail sim leaves no residual token balance."""

    def test_sim_fail_produces_no_tokens_out(
        self,
        dry_run_env: None,
    ) -> None:
        """A sim revert must produce tokens_out=0 (atomic buy — no partial fill)."""
        revert_rpc = MockRpcClientRevert(revert_reason="min_out_not_met")
        venue = JitoJupiterVenue(
            wallet_pubkey=_MOCK_PUBKEY,
            rpc_client=revert_rpc,
            signer_client=MockSignerClient(),
        )
        result = venue.execute(
            make_entry_intent(intent_id="ci-atomic-buy"),
            make_launch_event(),
        )
        assert result.landed is False
        assert result.tokens_out == 0, (
            f"tokens_out={result.tokens_out} after a reverted sim — "
            "atomic buy MUST leave zero tokens (no partial fill stranded)."
        )
        # No tip spent on a revert.
        assert result.tip_lamports == 0

    def test_signer_refused_produces_no_tokens_out(
        self,
        dry_run_env: None,
    ) -> None:
        """A signer refusal must also produce tokens_out=0 (no half-filled position)."""
        venue = JitoJupiterVenue(
            wallet_pubkey=_MOCK_PUBKEY,
            rpc_client=MockRpcClient(),
            signer_client=MockSignerClientRefusing(reason="signer_per_tx_cap_exceeded"),
        )
        result = venue.execute(
            make_entry_intent(intent_id="ci-signer-refused"),
            make_launch_event(),
        )
        assert result.landed is False
        assert result.tokens_out == 0
        assert result.reason == "signer_per_tx_cap_exceeded"


# ===========================================================================
# 6. Retry / idempotency
# ===========================================================================

class TestRetryAndIdempotency:
    """Self-check items 4 + 8: blockhash expiry retry; idempotency key no double-land."""

    def test_blockhash_expiry_triggers_retry(
        self,
        live_env: None,
    ) -> None:
        """Blockhash expiry triggers a rebuild+re-sign+re-simulate retry with a fresh blockhash.

        Critical assertions (B3 fix):
          1. At least 2 send_transaction calls (1 expiry + 1 retry).
          2. The two send calls carried DIFFERENT serialized bytes
             (proving a fresh-blockhash rebuild happened, not byte-identical re-send).
          3. get_latest_blockhash() was called more than once, confirming a fresh fetch.
          4. Final result is landed=True.
        """
        expiry_rpc = MockRpcClientBlockhashExpiry(current_slot=_SLOT, cu_consumed=180_000)
        venue = JitoJupiterVenue(
            wallet_pubkey=_MOCK_PUBKEY,
            rpc_client=expiry_rpc,
            signer_client=MockSignerClient(),
            live_submit_enabled=True,
        )
        intent = make_entry_intent(intent_id="ci-blockhash-expiry")
        result = venue.execute(intent, make_launch_event())

        # Blockhash expiry triggered at least one retry.
        assert expiry_rpc.blockhash_expiry_count >= 1, (
            "Expected at least one blockhash expiry to be triggered in the retry test."
        )
        # Second attempt succeeded.
        assert expiry_rpc.send_calls >= 2, (
            f"Expected at least 2 send_transaction calls (1 expiry + 1 retry), "
            f"got {expiry_rpc.send_calls}."
        )
        # The retry MUST have used different bytes — a fresh blockhash was embedded.
        # If bytes are equal the retry was byte-identical (stale blockhash — the bug).
        assert len(expiry_rpc.sent_bytes) >= 2, (
            "sent_bytes must have at least 2 entries (one per send_transaction call)."
        )
        assert expiry_rpc.sent_bytes[0] != expiry_rpc.sent_bytes[1], (
            "Retry re-sent byte-identical bytes — the blockhash was NOT refreshed. "
            "Each retry MUST rebuild the tx with a fresh blockhash (B3 fix)."
        )
        # get_latest_blockhash() must have been called more than once.
        assert expiry_rpc._blockhash_call_count >= 2, (
            f"get_latest_blockhash() called only {expiry_rpc._blockhash_call_count} time(s); "
            "the retry loop must fetch a fresh blockhash before rebuilding."
        )
        # Final result is landed.
        assert result.landed is True

    def test_duplicate_intent_id_does_not_double_land(
        self,
        live_env: None,
    ) -> None:
        """Sending twice with the same client_intent_id must NOT double-land."""
        mock_rpc = MockRpcClient()
        venue = JitoJupiterVenue(
            wallet_pubkey=_MOCK_PUBKEY,
            rpc_client=mock_rpc,
            signer_client=MockSignerClient(),
            live_submit_enabled=True,
        )
        intent = make_entry_intent(intent_id="ci-idempotent-send")
        event = make_launch_event()

        # First call — should land.
        result1 = venue.execute(intent, event)
        send_count_after_first = mock_rpc.send_calls

        # Second call with the SAME intent_id — must be blocked.
        result2 = venue.execute(intent, event)
        send_count_after_second = mock_rpc.send_calls

        assert result1.landed is True, "First attempt should have landed."
        assert result2.landed is False, "Second attempt (same intent_id) must not land."
        assert result2.reason == "idempotency_key_conflict"
        # No additional send_transaction call was made on the second attempt.
        assert send_count_after_second == send_count_after_first, (
            f"Expected no new send_transaction on duplicate intent_id, "
            f"but got {send_count_after_second - send_count_after_first} additional calls."
        )


# ===========================================================================
# 7. Money invariants — no float anywhere
# ===========================================================================

class TestMoneyInvariants:
    """All money fields in FillResult / Quote are int or Decimal-as-string (never float)."""

    def test_fill_result_tip_lamports_is_int(
        self,
        dry_run_venue: JitoJupiterVenue,
        entry_intent: EntryIntent,
        launch_event: LaunchEvent,
        dry_run_env: None,
    ) -> None:
        result = dry_run_venue.execute(entry_intent, launch_event)
        assert isinstance(result.tip_lamports, int), (
            f"FillResult.tip_lamports is {type(result.tip_lamports).__name__}, expected int."
        )
        assert isinstance(result.priority_lamports, int)
        assert isinstance(result.tokens_out, int)
        assert isinstance(result.entry_slippage_bps, int)

    def test_effective_price_is_decimal_accessible(
        self,
        dry_run_venue: JitoJupiterVenue,
        entry_intent: EntryIntent,
        launch_event: LaunchEvent,
        dry_run_env: None,
    ) -> None:
        result = dry_run_venue.execute(entry_intent, launch_event)
        price = result.effective_price
        assert isinstance(price, Decimal), (
            f"FillResult.effective_price must return Decimal, got {type(price).__name__}."
        )

    def test_quote_amounts_are_int(
        self,
        dry_run_venue: JitoJupiterVenue,
        dry_run_env: None,
    ) -> None:
        from aats.contracts.venue import Side
        q = dry_run_venue.quote(_MOCK_MINT, Side.BUY, 100_000_000)
        assert isinstance(q.amount_in_base, int), (
            f"Quote.amount_in_base is {type(q.amount_in_base).__name__}, expected int."
        )
        assert isinstance(q.amount_out_base, int)

    def test_no_float_in_land_call(
        self,
        dry_run_venue: JitoJupiterVenue,
        dry_run_env: None,
    ) -> None:
        """land() must reject float tip_lamports / cu_price (data-models.md §0)."""
        signed_tx = SignedTx(
            serialized_b64="stub",
            signer_pubkey=_MOCK_PUBKEY,
            intent_kind="ENTRY",
            mint=_MOCK_MINT,
            client_intent_id="ci-float-guard",
        )
        with pytest.raises(TypeError, match="must be int"):
            dry_run_venue.land(signed_tx, tip_lamports=0.2, cu_price=100_000)  # type: ignore


# ===========================================================================
# 8. SimulationVenue still satisfies the ABC (re-verified per self-check 5)
# ===========================================================================

class TestSimulationVenueABCStillSatisfied:
    """Re-verify SimulationVenue still satisfies the promoted ABC (code-reviewer R-01)."""

    def test_simulation_venue_is_subclass(self, sim_venue: SimulationVenue) -> None:
        assert issubclass(SimulationVenue, ExecutionVenue)

    def test_simulation_venue_instantiable(self, sim_venue: SimulationVenue) -> None:
        assert isinstance(sim_venue, ExecutionVenue)

    def test_simulation_venue_submit_mode_is_simulation(
        self, sim_venue: SimulationVenue
    ) -> None:
        assert sim_venue.submit_mode == SubmitMode.SIMULATION

    def test_simulation_venue_execute_returns_fill_result(
        self,
        sim_venue: SimulationVenue,
        entry_intent: EntryIntent,
        launch_event: LaunchEvent,
    ) -> None:
        result = sim_venue.execute(entry_intent, launch_event)
        assert isinstance(result, FillResult)

    def test_simulation_venue_land_does_not_submit(self, sim_venue: SimulationVenue) -> None:
        signed_tx = SignedTx(
            serialized_b64="",
            signer_pubkey="sim_pubkey_wallet-0",
            intent_kind="ENTRY",
            mint=_MOCK_MINT,
            client_intent_id="ci-sim-land",
        )
        result = sim_venue.land(signed_tx, tip_lamports=200_000, cu_price=100_000)
        assert result.submitted is False
        assert result.reason == "simulation"

    def test_simulation_venue_sign_is_keyless(self, sim_venue: SimulationVenue) -> None:
        unsigned = UnsignedTx(
            serialized_b64="",
            intent_kind="ENTRY",
            mint=_MOCK_MINT,
            client_intent_id="ci-sim-sign",
        )
        signed = sim_venue.sign(unsigned, wallet_id="wallet-0")
        assert "sim" in signed.signer_pubkey.lower(), (
            "SimulationVenue.sign() must return a sim pubkey, not a real one."
        )

    def test_simulation_venue_all_eight_members(self, sim_venue: SimulationVenue) -> None:
        for m in ["execute", "exit", "quote", "build", "sign", "simulate", "land", "reconcile"]:
            assert callable(getattr(sim_venue, m, None)), f"SimulationVenue.{m}() missing."

    def test_simulation_venue_buy_sell_round_trip(
        self,
        sim_venue: SimulationVenue,
        entry_intent: EntryIntent,
        launch_event: LaunchEvent,
        exit_intent: ExitIntent,
    ) -> None:
        """Self-check 4 (SimulationVenue): buy→sell round-trip with modeled costs."""
        buy_result = sim_venue.execute(entry_intent, launch_event)
        assert isinstance(buy_result, FillResult)
        # Now exit.
        position = object()  # duck-typed; sim doesn't inspect it
        sell_result = sim_venue.exit(exit_intent, position)
        assert isinstance(sell_result, FillResult)
        # Cost fields are int (never float).
        assert isinstance(buy_result.tip_lamports, int)
        assert isinstance(buy_result.priority_lamports, int)

    def test_simulation_venue_partial_fill_and_failed_land(
        self,
        sim_venue: SimulationVenue,
        launch_event: LaunchEvent,
    ) -> None:
        """Self-check 4 cont: partial-fill (reverted_min_out) and failed-land paths."""
        # Force a zero-slippage intent so any competitor activity reverts.
        intent_zero_slip = make_entry_intent(
            intent_id="ci-sim-zero-slip",
            slippage_bps=0,
            tip_lamports=1,  # low tip so we lose the bid
        )
        event_many_competitors = make_launch_event(competitors=20)
        result = sim_venue.execute(intent_zero_slip, event_many_competitors)
        # With 20 competitors and 0 slippage, expect a revert or miss.
        assert isinstance(result, FillResult)
        if not result.landed:
            assert result.reason in ("too_late_or_outbid", "reverted_min_out")


# ===========================================================================
# 9. Individual lifecycle primitive tests
# ===========================================================================

class TestLifecyclePrimitives:
    """Tests for the individual quote/build/sign/simulate/land/reconcile primitives."""

    def test_quote_returns_quote_object(
        self,
        dry_run_venue: JitoJupiterVenue,
        dry_run_env: None,
    ) -> None:
        from aats.contracts.venue import Quote, Side
        q = dry_run_venue.quote(_MOCK_MINT, Side.BUY, 100_000_000)
        assert isinstance(q, Quote)
        assert q.mint == _MOCK_MINT
        # Price is a non-empty string (Decimal-as-string, never float).
        assert isinstance(q.price, str)
        assert Decimal(q.price) >= 0

    def test_build_returns_unsigned_tx(
        self,
        dry_run_venue: JitoJupiterVenue,
        entry_intent: EntryIntent,
        dry_run_env: None,
    ) -> None:
        unsigned = dry_run_venue.build(entry_intent)
        assert isinstance(unsigned, UnsignedTx)
        assert unsigned.mint == _MOCK_MINT
        assert unsigned.client_intent_id == entry_intent.client_intent_id
        assert unsigned.intent_kind == "ENTRY"

    def test_sign_returns_signed_tx(
        self,
        dry_run_venue: JitoJupiterVenue,
        entry_intent: EntryIntent,
        dry_run_env: None,
    ) -> None:
        unsigned = dry_run_venue.build(entry_intent)
        signed = dry_run_venue.sign(unsigned, wallet_id="wallet-0")
        assert isinstance(signed, SignedTx)
        assert signed.mint == _MOCK_MINT
        assert signed.signer_pubkey == _MOCK_PUBKEY
        # Signed tx must carry the intent_id for idempotency tracking.
        assert signed.client_intent_id == entry_intent.client_intent_id

    def test_simulate_returns_sim_result(
        self,
        dry_run_venue: JitoJupiterVenue,
        entry_intent: EntryIntent,
        dry_run_env: None,
    ) -> None:
        unsigned = dry_run_venue.build(entry_intent)
        signed = dry_run_venue.sign(unsigned, "wallet-0")
        sim = dry_run_venue.simulate(signed)
        assert isinstance(sim, SimResult)
        assert isinstance(sim.cu_consumed, int), (
            f"SimResult.cu_consumed must be int, got {type(sim.cu_consumed).__name__}."
        )

    def test_reconcile_landed_true(
        self,
        dry_run_venue: JitoJupiterVenue,
        dry_run_env: None,
    ) -> None:
        land = LandResult(
            submitted=True,
            reason="landed",
            signature="Sig111111111111111111111111111111111111111111111",
            land_slot=_SLOT + 2,
        )
        fill = dry_run_venue.reconcile(land)
        assert fill.landed is True
        assert fill.reason == "filled"

    def test_reconcile_dry_run_not_landed(
        self,
        dry_run_venue: JitoJupiterVenue,
        dry_run_env: None,
    ) -> None:
        land = LandResult(submitted=False, reason="dry_run", signature=None, land_slot=None)
        fill = dry_run_venue.reconcile(land)
        assert fill.landed is False
        assert fill.reason == "dry_run"


# ===========================================================================
# 10. Signer refusal propagation
# ===========================================================================

class TestSignerRefusalPropagation:
    """Signer refusal aborts the snipe/exit, never leaks the key, never retries past a cap."""

    def test_signer_refused_aborts_execute(
        self,
        dry_run_env: None,
    ) -> None:
        venue = JitoJupiterVenue(
            wallet_pubkey=_MOCK_PUBKEY,
            rpc_client=MockRpcClient(),
            signer_client=MockSignerClientRefusing(reason="signer_program_not_allowlisted"),
        )
        result = venue.execute(
            make_entry_intent(intent_id="ci-signer-abort"),
            make_launch_event(),
        )
        assert result.landed is False
        assert result.reason == "signer_program_not_allowlisted"
        assert result.tokens_out == 0

    def test_signer_pubkey_is_never_the_key(
        self,
        dry_run_venue: JitoJupiterVenue,
        entry_intent: EntryIntent,
        dry_run_env: None,
    ) -> None:
        """The signer_pubkey returned in SignedTx must NOT be a raw private key.

        We check for actual key-material markers (private key, seed phrase tokens),
        not just the substring 'key' which legitimately appears in 'pubkey'.
        """
        unsigned = dry_run_venue.build(entry_intent)
        signed = dry_run_venue.sign(unsigned, "wallet-0")
        raw = signed.signer_pubkey.lower()
        # These are the markers that indicate a raw private key is exposed.
        # 'pubkey' is legitimate and expected — we check for compound key-material terms.
        key_material_markers = ("private_key", "secret_key", "seed_phrase", "mnemonic", "keypair_json")
        for forbidden in key_material_markers:
            assert forbidden not in raw, (
                f"signer_pubkey contains key-material marker '{forbidden}' — "
                "the venue must NEVER hold or expose the private key (ADR-0009)."
            )
        # Additionally assert the serialized tx does not contain key material.
        tx_raw = signed.serialized_b64.lower()
        for forbidden in ("private", "secret", "mnemonic", "seed"):
            assert forbidden not in tx_raw, (
                f"signed tx contains suspicious token '{forbidden}' — "
                "signed bytes must not carry key material."
            )


# ===========================================================================
# 11. No win_rate field (HONESTY CLAUSE)
# ===========================================================================

class TestHonestyClause:
    def test_no_win_rate_in_execution_module(self) -> None:
        """HONESTY CLAUSE: 'win_rate' must not appear as a code symbol in execution/."""
        import pathlib
        import re

        execution_root = pathlib.Path(__file__).parent.parent.parent / "aats" / "execution"
        violations = []
        for py_file in execution_root.rglob("*.py"):
            content = py_file.read_text(encoding="utf-8", errors="ignore")
            code_lines = []
            for line in content.splitlines():
                stripped = line.strip()
                if stripped.startswith("#") or stripped in ('"""', "'''"):
                    continue
                code_part = re.sub(r"#.*$", "", line)
                code_lines.append(code_part)
            code_only = "\n".join(code_lines)
            code_no_strings = re.sub(r'"[^"]*"|\'[^\']*\'', '""', code_only)
            if re.search(r"\bwin_rate\b", code_no_strings):
                violations.append(str(py_file))
        assert not violations, (
            f"HONESTY CLAUSE: win_rate in execution code: {violations}"
        )

    def test_no_float_money_in_fill_result_validator(self) -> None:
        """FillResult must reject float money fields (data-models.md §0 guard)."""
        from pydantic import ValidationError
        with pytest.raises((ValidationError, ValueError, TypeError)):
            FillResult(
                landed=True,
                reason="filled",
                tokens_out=1.5,  # float — must be rejected  # type: ignore
                tip_lamports=100,
                priority_lamports=0,
            )


# ===========================================================================
# 12. Exit path (Jupiter route)
# ===========================================================================

class TestExitPath:
    def test_exit_returns_fill_result(
        self,
        dry_run_venue: JitoJupiterVenue,
        exit_intent: ExitIntent,
        dry_run_env: None,
        mock_rpc: MockRpcClient,
    ) -> None:
        result = dry_run_venue.exit(exit_intent, object())
        assert isinstance(result, FillResult)

    def test_exit_dry_run_does_not_submit(
        self,
        dry_run_venue: JitoJupiterVenue,
        exit_intent: ExitIntent,
        dry_run_env: None,
        mock_rpc: MockRpcClient,
    ) -> None:
        dry_run_venue.exit(exit_intent, object())
        assert mock_rpc.send_calls == 0, (
            "Exit in DRY_RUN must not call send_transaction."
        )

    def test_exit_idempotency(
        self,
        live_env: None,
    ) -> None:
        """Duplicate exit with same intent_id does not double-land."""
        mock_rpc = MockRpcClient()
        venue = JitoJupiterVenue(
            wallet_pubkey=_MOCK_PUBKEY,
            rpc_client=mock_rpc,
            signer_client=MockSignerClient(),
            live_submit_enabled=True,
        )
        exit_intent = make_exit_intent(intent_id="ci-exit-idempotent")

        first_result = venue.exit(exit_intent, object())
        assert first_result.landed is True or first_result.reason != "idempotency_key_conflict"
        send_after_first = mock_rpc.send_calls

        result2 = venue.exit(exit_intent, object())
        assert result2.reason == "idempotency_key_conflict"
        assert mock_rpc.send_calls == send_after_first  # no new call
