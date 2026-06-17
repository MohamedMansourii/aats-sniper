"""T-328 — MultiWalletOrchestrator unit + integration tests.

Self-check items verified here (ALL mandatory per task mandate):
  1. Aggregate exposure to a mint <= C (anti-cluster cap enforced).
  2. Partial fills handled: some slots land, some fail → partial_fill=True.
  3. Anti-cluster cap enforced: cap exceeded → AntiClusterCapExceeded BEFORE any execute().
  4. DRY-RUN no submit: venue.execute() is the submit gate; no send_transaction() call.
  5. N_max default = 1 (OQ-010 / R3 guard).
  6. SimulationVenue still satisfies the ABC (venue contract compliance).
  7. Multi-wallet enabled at N=2 when activation gate is open.
  8. Duplicate wallet_ids are rejected.
  9. All money fields are int / Decimal-as-string (never float).
 10. Cap released only when appropriate (ledger state correct after failure).
 11. Idempotent slot intent IDs (derived deterministically from base_intent_id + slot).
 12. n_max hard ceiling enforced.
 13. MintExposureLedger check_and_reserve / release / get_exposure semantics.
 14. BundleFillResult aggregate price is Decimal-as-string, not float.
 15. PartialFillReconciler produces integer bps ratio.
 16. Activation gate is load-bearing: N_WALLETS_MAX>1 WITHOUT flag is clamped to 1 (QA-MAJOR-1).
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
    SimulationVenue,
    SubmitMode,
)
from aats.execution import (
    AntiClusterCapExceeded,
    BundleFillResult,
    JitoJupiterVenue,
    MintExposureLedger,
    MockRpcClient,
    MockSignerClient,
    MultiWalletConfigError,
    MultiWalletOrchestrator,
    N_WALLETS_MAX_DEFAULT,
    N_WALLETS_MAX_HARD_CEILING,
    PartialFillReconciler,
    PartialFillSummary,
    WalletSlotFill,
    make_single_wallet_orchestrator,
)
from aats.execution.multi_wallet import _resolve_n_max
from tests.execution.conftest import (
    _MOCK_MINT,
    _MOCK_PUBKEY,
    _SLOT,
    make_entry_intent,
    make_launch_event,
)

# ---------------------------------------------------------------------------
# Test constants
# ---------------------------------------------------------------------------

_CAP = 100_000_000  # 0.1 SOL per-mint cap (lamports, int)
_SOL_100M = 100_000_000  # 0.1 SOL in lamports (int)
_SOL_50M = 50_000_000   # 0.05 SOL in lamports (int)
_SOL_60M = 60_000_000   # 0.06 SOL in lamports (int)


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def make_sim_venue(seed: int = 42) -> SimulationVenue:
    import random
    return SimulationVenue(
        rng=random.Random(seed),
        market_tip_lamports=200_000,
        ahead_size_sol_lamports=1_200_000_000,
    )


def make_dry_run_venue() -> JitoJupiterVenue:
    return JitoJupiterVenue(
        wallet_pubkey=_MOCK_PUBKEY,
        rpc_client=MockRpcClient(current_slot=_SLOT, cu_consumed=180_000),
        signer_client=MockSignerClient(wallet_pubkey=_MOCK_PUBKEY),
        live_submit_enabled=False,
    )


def make_ledger(cap: int = _CAP) -> MintExposureLedger:
    return MintExposureLedger(cap_lamports=cap)


# ---------------------------------------------------------------------------
# 1. N_max default = 1 (R3 guard, OQ-010)
# ---------------------------------------------------------------------------


class TestNMaxDefault:
    def test_default_n_max_is_one(self) -> None:
        """N_WALLETS_MAX_DEFAULT must be 1 (OQ-010)."""
        assert N_WALLETS_MAX_DEFAULT == 1

    def test_hard_ceiling_is_five(self) -> None:
        """Hard ceiling on N wallets must be 5."""
        assert N_WALLETS_MAX_HARD_CEILING == 5

    def test_single_wallet_orchestrator_n_is_one(self) -> None:
        orch = make_single_wallet_orchestrator(
            venue=make_sim_venue(),
            wallet_id="wallet-0",
            per_trade_cap_lamports=_CAP,
        )
        assert orch.n_wallets == 1
        assert orch.effective_n_max == 1

    def test_multi_wallet_not_enabled_by_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Without N_WALLETS_MAX_ENABLED=true, N>1 is refused."""
        monkeypatch.delenv("N_WALLETS_MAX_ENABLED", raising=False)
        with pytest.raises(MultiWalletConfigError, match="N_max"):
            MultiWalletOrchestrator(
                venue=make_sim_venue(),
                wallet_ids=["wallet-0", "wallet-1"],
                ledger=make_ledger(),
                n_max=1,  # explicit n_max=1 but 2 wallets given
            )

    def test_multi_wallet_enabled_at_n2_when_gate_open(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With N_WALLETS_MAX_ENABLED=true and N_WALLETS_MAX=2, N=2 is allowed."""
        monkeypatch.setenv("N_WALLETS_MAX_ENABLED", "true")
        monkeypatch.setenv("N_WALLETS_MAX", "2")
        orch = MultiWalletOrchestrator(
            venue=make_sim_venue(),
            wallet_ids=["wallet-0", "wallet-1"],
            ledger=make_ledger(),
            # No n_max override → reads from env
        )
        assert orch.n_wallets == 2
        assert orch.effective_n_max == 2

    # ---- QA-MAJOR-1 fix: activation gate is the load-bearing line ----
    # These two tests together kill the mutation:
    #   `if False and enabled != "true":` (gate defeated, multi-wallet always on)
    # Without these tests, 43 existing tests pass under that mutant because every
    # multi-wallet test sets BOTH N_WALLETS_MAX=2 AND N_WALLETS_MAX_ENABLED=true.
    # The safety contract is: N_WALLETS_MAX>1 in .env alone MUST NOT activate
    # multi-wallet — the operator MUST also flip N_WALLETS_MAX_ENABLED=true.

    def test_activation_gate_is_load_bearing_n_max_env_alone_returns_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """QA-MAJOR-1 (R3 guard): N_WALLETS_MAX=3 WITHOUT activation flag → _resolve_n_max returns 1.

        Mutation target: `if enabled != "true": return N_WALLETS_MAX_DEFAULT`
        Under the gate-defeat mutation (`if False and ...`) this test returns 3 → FAILS RED.
        Under the correct gate it returns 1 → PASSES GREEN.

        Realistic failure mode: .env edited to N_WALLETS_MAX=2 without the flag
        (e.g., a copy-paste from a future R4 template). The gate must clamp to 1.
        """
        monkeypatch.delenv("N_WALLETS_MAX_ENABLED", raising=False)
        monkeypatch.setenv("N_WALLETS_MAX", "3")

        effective = _resolve_n_max(None)
        assert effective == N_WALLETS_MAX_DEFAULT == 1, (
            f"Expected _resolve_n_max(None)==1 when N_WALLETS_MAX_ENABLED is absent, "
            f"but got {effective}. The R3 activation gate (OQ-010) is NOT holding."
        )

    def test_n_wallets_gt1_without_activation_flag_raises_config_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """QA-MAJOR-1 (R3 guard): N=2 wallets constructed WITHOUT activation flag raises MultiWalletConfigError.

        Mutation target: `if enabled != "true": return N_WALLETS_MAX_DEFAULT`
        Under the gate-defeat mutation the effective_n_max becomes 2 (from N_WALLETS_MAX=2)
        and MultiWalletOrchestrator accepts both wallets → no exception → test FAILS RED.
        Under the correct gate effective_n_max=1 and len(wallet_ids)=2 > 1 → raises → GREEN.

        The realistic failure scenario is an operator who edits .env to:
            N_WALLETS_MAX=2          <-- set for future R4
            # N_WALLETS_MAX_ENABLED  <-- forgot to uncomment
        Without this test there is no coverage proving the gate blocked that deploy.
        """
        monkeypatch.delenv("N_WALLETS_MAX_ENABLED", raising=False)
        monkeypatch.setenv("N_WALLETS_MAX", "2")

        with pytest.raises(MultiWalletConfigError, match="N_max"):
            MultiWalletOrchestrator(
                venue=make_sim_venue(),
                wallet_ids=["wallet-0", "wallet-1"],
                ledger=make_ledger(),
                # No n_max override → reads from env → gate must clamp to 1
            )


# ---------------------------------------------------------------------------
# 2. Anti-cluster cap enforcement
# ---------------------------------------------------------------------------


class TestAntiClusterCap:
    def test_cap_enforced_exact_amount(self) -> None:
        """Allocating exactly the cap is allowed."""
        ledger = make_ledger(cap=_CAP)
        ledger.check_and_reserve(_MOCK_MINT, _CAP)  # should not raise
        assert ledger.get_exposure(_MOCK_MINT) == _CAP

    def test_cap_exceeded_raises(self) -> None:
        """Allocating more than the cap raises AntiClusterCapExceeded."""
        ledger = make_ledger(cap=_CAP)
        with pytest.raises(AntiClusterCapExceeded) as exc_info:
            ledger.check_and_reserve(_MOCK_MINT, _CAP + 1)
        err = exc_info.value
        assert err.mint == _MOCK_MINT
        assert err.cap_lamports == _CAP
        assert err.requested_lamports == _CAP + 1
        assert err.current_lamports == 0

    def test_cumulative_cap_exceeded_raises(self) -> None:
        """Two allocations that together exceed the cap must fail on the second."""
        ledger = make_ledger(cap=_CAP)
        ledger.check_and_reserve(_MOCK_MINT, _SOL_50M)  # first 50M OK
        with pytest.raises(AntiClusterCapExceeded) as exc_info:
            ledger.check_and_reserve(_MOCK_MINT, _SOL_60M)  # 50M + 60M > 100M
        err = exc_info.value
        assert err.current_lamports == _SOL_50M
        assert err.requested_lamports == _SOL_60M

    def test_cap_independent_per_mint(self) -> None:
        """Different mints do NOT share a cap — each has its own budget."""
        ledger = make_ledger(cap=_CAP)
        other_mint = "OtherMint1111111111111111111111111111111111"
        ledger.check_and_reserve(_MOCK_MINT, _CAP)     # fills cap for _MOCK_MINT
        ledger.check_and_reserve(other_mint, _CAP)     # other_mint cap is independent
        assert ledger.get_exposure(_MOCK_MINT) == _CAP
        assert ledger.get_exposure(other_mint) == _CAP

    def test_release_restores_budget(self) -> None:
        """release() after a failed trade restores the mint's budget."""
        ledger = make_ledger(cap=_CAP)
        ledger.check_and_reserve(_MOCK_MINT, _SOL_50M)
        ledger.release(_MOCK_MINT, _SOL_50M)
        assert ledger.get_exposure(_MOCK_MINT) == 0
        # Budget restored — can allocate again.
        ledger.check_and_reserve(_MOCK_MINT, _SOL_50M)  # should not raise
        assert ledger.get_exposure(_MOCK_MINT) == _SOL_50M

    def test_reset_mint_clears_exposure(self) -> None:
        """reset_mint() clears the exposure for a mint (called after position closed)."""
        ledger = make_ledger(cap=_CAP)
        ledger.check_and_reserve(_MOCK_MINT, _SOL_50M)
        ledger.reset_mint(_MOCK_MINT)
        assert ledger.get_exposure(_MOCK_MINT) == 0

    def test_cap_exceeded_in_execute_bundle_before_any_execute(self) -> None:
        """AntiClusterCapExceeded is raised BEFORE any venue.execute() call."""
        import random

        class CountingVenue(SimulationVenue):
            """Counts how many times execute() was called."""
            execute_count = 0

            def execute(self, intent: EntryIntent, event: LaunchEvent) -> FillResult:
                CountingVenue.execute_count += 1
                return super().execute(intent, event)

        CountingVenue.execute_count = 0
        venue = CountingVenue(rng=random.Random(42))
        # Pre-fill the ledger so any new allocation exceeds cap.
        ledger = make_ledger(cap=_CAP)
        ledger.check_and_reserve(_MOCK_MINT, _CAP)  # cap now exactly full

        orch = MultiWalletOrchestrator(
            venue=venue,
            wallet_ids=["wallet-0"],
            ledger=ledger,
            n_max=1,
        )
        intent = make_entry_intent(
            mint=_MOCK_MINT,
            sol_in_lamports=1,  # even 1 lamport pushes over
        )
        event = make_launch_event(mint=_MOCK_MINT)

        with pytest.raises(AntiClusterCapExceeded):
            orch.execute_bundle(intent, event)

        # execute() was NEVER called — cap check is pre-execution.
        assert CountingVenue.execute_count == 0

    def test_float_cap_rejected(self) -> None:
        """MintExposureLedger must reject float cap (data-models.md §0)."""
        with pytest.raises(TypeError, match="must be int"):
            MintExposureLedger(cap_lamports=0.1)  # type: ignore[arg-type]

    def test_float_reserve_rejected(self) -> None:
        """check_and_reserve must reject float amounts."""
        ledger = make_ledger(cap=_CAP)
        with pytest.raises(TypeError, match="must be int"):
            ledger.check_and_reserve(_MOCK_MINT, 50_000_000.0)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 3. Partial fill handling
# ---------------------------------------------------------------------------


class TestPartialFills:
    def test_partial_fill_when_one_slot_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If slot 0 lands and slot 1 fails, partial_fill=True."""
        monkeypatch.setenv("N_WALLETS_MAX_ENABLED", "true")
        monkeypatch.setenv("N_WALLETS_MAX", "2")

        fill_count = {"n": 0}

        class AlternatingVenue(SimulationVenue):
            """Lands on even slots, fails on odd slots."""
            import random as _random
            _rng = _random.Random(42)

            def execute(self, intent: EntryIntent, event: LaunchEvent) -> FillResult:
                idx = fill_count["n"]
                fill_count["n"] += 1
                if idx % 2 == 0:
                    return FillResult(
                        landed=True,
                        reason="filled",
                        tokens_out=int(intent.sol_in_lamports * 9975 // 10000),
                        effective_price_decimal="1",
                        tip_lamports=intent.tip_lamports,
                        priority_lamports=20,
                        entry_slippage_bps=25,
                    )
                else:
                    return FillResult(
                        landed=False,
                        reason="too_late_or_outbid",
                        tip_lamports=0,
                        priority_lamports=0,
                    )

        import random
        venue = AlternatingVenue(rng=random.Random(42))
        ledger = make_ledger(cap=_CAP)
        orch = MultiWalletOrchestrator(
            venue=venue,
            wallet_ids=["wallet-0", "wallet-1"],
            ledger=ledger,
        )
        intent = make_entry_intent(sol_in_lamports=_SOL_50M * 2)  # 100M total
        event = make_launch_event()

        result = orch.execute_bundle(intent, event)
        assert result.partial_fill is True, "Expected partial_fill=True when 1/2 slots land"
        assert result.any_landed is True
        # Only slot 0 landed → total_tokens_out comes from slot 0 only
        assert result.total_tokens_out > 0
        # slot 1 did not land → total_sol_spent < total_sol_allocated
        assert result.total_sol_spent_lamports < _CAP

    def test_all_slots_land_no_partial_fill(self) -> None:
        """If all slots land, partial_fill=False."""
        import random

        class AlwaysLandVenue(SimulationVenue):
            def execute(self, intent: EntryIntent, event: LaunchEvent) -> FillResult:
                return FillResult(
                    landed=True,
                    reason="filled",
                    tokens_out=int(intent.sol_in_lamports * 9975 // 10000),
                    effective_price_decimal="1",
                    tip_lamports=intent.tip_lamports,
                    priority_lamports=20,
                    entry_slippage_bps=25,
                )

        venue = AlwaysLandVenue(rng=random.Random(42))
        ledger = make_ledger(cap=_CAP)
        orch = MultiWalletOrchestrator(
            venue=venue,
            wallet_ids=["wallet-0"],
            ledger=ledger,
            n_max=1,
        )
        intent = make_entry_intent(sol_in_lamports=_SOL_50M)
        event = make_launch_event()

        result = orch.execute_bundle(intent, event)
        assert result.partial_fill is False
        assert result.any_landed is True
        assert result.total_tokens_out > 0

    def test_no_slots_land_not_partial_fill(self) -> None:
        """If NO slots land, partial_fill=False (not partial — fully failed)."""
        import random

        class NeverLandVenue(SimulationVenue):
            def execute(self, intent: EntryIntent, event: LaunchEvent) -> FillResult:
                return FillResult(
                    landed=False,
                    reason="too_late_or_outbid",
                    tip_lamports=0,
                    priority_lamports=0,
                )

        venue = NeverLandVenue(rng=random.Random(42))
        ledger = make_ledger(cap=_CAP)
        orch = MultiWalletOrchestrator(
            venue=venue,
            wallet_ids=["wallet-0"],
            ledger=ledger,
            n_max=1,
        )
        intent = make_entry_intent(sol_in_lamports=_SOL_50M)
        event = make_launch_event()

        result = orch.execute_bundle(intent, event)
        # partial_fill requires at least one landed AND at least one failed.
        assert result.partial_fill is False
        assert result.any_landed is False
        assert result.total_tokens_out == 0
        assert result.total_sol_spent_lamports == 0


# ---------------------------------------------------------------------------
# 4. DRY-RUN no submit (venue enforces, confirmed here)
# ---------------------------------------------------------------------------


class TestDryRunNoSubmit:
    def test_dry_run_venue_no_send_transaction_calls(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """DRY_RUN venue: no send_transaction() calls through the orchestrator."""
        monkeypatch.setenv("DRY_RUN_ENABLED", "true")

        rpc = MockRpcClient(current_slot=_SLOT, cu_consumed=180_000)
        venue = JitoJupiterVenue(
            wallet_pubkey=_MOCK_PUBKEY,
            rpc_client=rpc,
            signer_client=MockSignerClient(wallet_pubkey=_MOCK_PUBKEY),
            live_submit_enabled=False,
        )
        ledger = make_ledger(cap=_CAP)
        orch = MultiWalletOrchestrator(
            venue=venue,
            wallet_ids=["wallet-0"],
            ledger=ledger,
            n_max=1,
        )
        intent = make_entry_intent(sol_in_lamports=_SOL_50M)
        event = make_launch_event()

        result = orch.execute_bundle(intent, event)

        # Venue is DRY_RUN: land() short-circuits before any send.
        assert rpc.send_calls == 0, (
            f"send_transaction was called {rpc.send_calls} times — "
            "DRY_RUN must NEVER call send_transaction()."
        )
        # Result should reflect the dry_run outcome.
        assert not result.any_landed  # dry_run → landed=False
        assert result.total_tokens_out == 0

    def test_sim_venue_no_network_calls(self) -> None:
        """SimulationVenue: submit_mode=SIMULATION, never any network calls."""
        venue = make_sim_venue()
        assert venue.submit_mode == SubmitMode.SIMULATION

        ledger = make_ledger(cap=_CAP)
        orch = make_single_wallet_orchestrator(
            venue=venue,
            wallet_id="wallet-0",
            per_trade_cap_lamports=_CAP,
        )
        intent = make_entry_intent(sol_in_lamports=_SOL_50M)
        event = make_launch_event()

        # Should complete without raising (and without network).
        result = orch.execute_bundle(intent, event)
        # SimulationVenue may or may not land depending on the RNG; the point is
        # no network was called (SimulationVenue has no send path).
        assert isinstance(result, BundleFillResult)


# ---------------------------------------------------------------------------
# 5. SimulationVenue ABC compliance (re-verified per mandate)
# ---------------------------------------------------------------------------


class TestSimulationVenueABCCompliance:
    def test_is_subclass_of_execution_venue(self) -> None:
        assert issubclass(SimulationVenue, ExecutionVenue)

    def test_all_eight_abstract_methods_implemented(self) -> None:
        required = ["execute", "exit", "quote", "build", "sign", "simulate", "land", "reconcile"]
        venue = make_sim_venue()
        for method_name in required:
            assert hasattr(venue, method_name), f"Missing method: {method_name}"
            assert callable(getattr(venue, method_name)), f"{method_name} not callable"

    def test_submit_mode_is_simulation(self) -> None:
        assert make_sim_venue().submit_mode == SubmitMode.SIMULATION


# ---------------------------------------------------------------------------
# 6. Money integer invariants
# ---------------------------------------------------------------------------


class TestMoneyInvariants:
    def test_wallet_slot_fill_sol_allocated_is_int(self) -> None:
        """WalletSlotFill.sol_allocated_lamports must be int (never float)."""
        fill = FillResult(landed=False, reason="dry_run", tip_lamports=0, priority_lamports=0)
        slot_fill = WalletSlotFill(
            wallet_id="wallet-0",
            intent_id="test-id:w0",
            fill=fill,
            sol_allocated_lamports=_SOL_50M,  # int
        )
        assert isinstance(slot_fill.sol_allocated_lamports, int)
        assert slot_fill.sol_allocated_lamports == _SOL_50M

    def test_bundle_fill_result_money_all_int(self) -> None:
        """All BundleFillResult money fields must be int. Price is Decimal-as-string."""
        fill = FillResult(
            landed=True,
            reason="filled",
            tokens_out=1000,
            tip_lamports=300_000,
            priority_lamports=20,
            effective_price_decimal="0.01",
            entry_slippage_bps=25,
        )
        slot_fill = WalletSlotFill(
            wallet_id="wallet-0",
            intent_id="test-id:w0",
            fill=fill,
            sol_allocated_lamports=_SOL_50M,
        )
        bundle = BundleFillResult(
            wallet_fills=(slot_fill,),
            any_landed=True,
            total_tokens_out=1000,
            total_sol_spent_lamports=_SOL_50M,
            total_tip_lamports=300_000,
            total_priority_lamports=20,
            partial_fill=False,
            aggregate_effective_price_decimal="50000.0",
        )
        assert isinstance(bundle.total_tokens_out, int)
        assert isinstance(bundle.total_sol_spent_lamports, int)
        assert isinstance(bundle.total_tip_lamports, int)
        assert isinstance(bundle.total_priority_lamports, int)
        # Price is NOT float.
        assert isinstance(bundle.aggregate_effective_price_decimal, str)
        # Parseable as Decimal.
        price = Decimal(bundle.aggregate_effective_price_decimal)
        assert isinstance(price, Decimal)
        # float() of a Decimal-as-string should NOT appear in any assertion here.

    def test_float_cap_rejected_at_make_single(self) -> None:
        """make_single_wallet_orchestrator must reject float per_trade_cap."""
        with pytest.raises(TypeError, match="must be int"):
            make_single_wallet_orchestrator(
                venue=make_sim_venue(),
                per_trade_cap_lamports=0.1,  # type: ignore[arg-type]
            )

    def test_intent_sol_distribution_sum_invariant(self) -> None:
        """Sum of per-slot SOL allocations must equal total sol_in_lamports exactly (int)."""
        import random

        class RecordingVenue(SimulationVenue):
            received_sols: list[int] = []

            def execute(self, intent: EntryIntent, event: LaunchEvent) -> FillResult:
                RecordingVenue.received_sols.append(intent.sol_in_lamports)
                return FillResult(landed=False, reason="dry_run", tip_lamports=0, priority_lamports=0)

        RecordingVenue.received_sols = []
        venue = RecordingVenue(rng=random.Random(42))

        # Use an amount that doesn't divide evenly by 1 (single wallet, trivial).
        total_sol = 99_999_999  # odd number to stress remainder logic
        ledger = make_ledger(cap=total_sol + 1)
        orch = MultiWalletOrchestrator(
            venue=venue,
            wallet_ids=["wallet-0"],
            ledger=ledger,
            n_max=1,
        )
        intent = make_entry_intent(sol_in_lamports=total_sol)
        event = make_launch_event()
        orch.execute_bundle(intent, event)

        assert len(RecordingVenue.received_sols) == 1
        assert sum(RecordingVenue.received_sols) == total_sol, (
            "Per-slot SOL sum must equal total sol_in_lamports exactly (int arithmetic)."
        )
        # Verify it's actually int.
        assert all(isinstance(s, int) for s in RecordingVenue.received_sols)


# ---------------------------------------------------------------------------
# 7. PartialFillReconciler
# ---------------------------------------------------------------------------


class TestPartialFillReconciler:
    def _make_bundle(
        self, landed: bool, tokens: int, sol: int
    ) -> BundleFillResult:
        fill = FillResult(
            landed=landed,
            reason="filled" if landed else "too_late_or_outbid",
            tokens_out=tokens if landed else 0,
            tip_lamports=300_000 if landed else 0,
            priority_lamports=20 if landed else 0,
            effective_price_decimal="1" if landed else "0",
            entry_slippage_bps=25 if landed else 0,
        )
        slot_fill = WalletSlotFill(
            wallet_id="wallet-0",
            intent_id="test:w0",
            fill=fill,
            sol_allocated_lamports=sol,
        )
        return BundleFillResult(
            wallet_fills=(slot_fill,),
            any_landed=landed,
            total_tokens_out=tokens if landed else 0,
            total_sol_spent_lamports=sol if landed else 0,
            total_tip_lamports=300_000 if landed else 0,
            total_priority_lamports=20 if landed else 0,
            partial_fill=False,
            aggregate_effective_price_decimal="1" if landed else "0",
        )

    def test_full_fill_ratio_is_10000_bps(self) -> None:
        """A perfect fill returns partial_fill_ratio_bps=10000."""
        bundle = self._make_bundle(landed=True, tokens=1000, sol=_SOL_50M)
        summary = PartialFillReconciler.reconcile(bundle, expected_tokens_out=1000)
        assert summary.partial_fill_ratio_bps == 10_000
        assert isinstance(summary.partial_fill_ratio_bps, int)

    def test_zero_fill_ratio_is_zero(self) -> None:
        """A failed fill returns partial_fill_ratio_bps=0."""
        bundle = self._make_bundle(landed=False, tokens=0, sol=_SOL_50M)
        summary = PartialFillReconciler.reconcile(bundle, expected_tokens_out=1000)
        assert summary.partial_fill_ratio_bps == 0
        assert isinstance(summary.partial_fill_ratio_bps, int)

    def test_half_fill_ratio_is_5000_bps(self) -> None:
        """Half fill returns partial_fill_ratio_bps=5000."""
        bundle = self._make_bundle(landed=True, tokens=500, sol=_SOL_50M)
        summary = PartialFillReconciler.reconcile(bundle, expected_tokens_out=1000)
        assert summary.partial_fill_ratio_bps == 5_000
        assert isinstance(summary.partial_fill_ratio_bps, int)

    def test_ratio_clamped_at_10000(self) -> None:
        """Over-fill is clamped at 10000 bps (should not exceed 100%)."""
        bundle = self._make_bundle(landed=True, tokens=1200, sol=_SOL_50M)
        summary = PartialFillReconciler.reconcile(bundle, expected_tokens_out=1000)
        assert summary.partial_fill_ratio_bps <= 10_000
        assert isinstance(summary.partial_fill_ratio_bps, int)

    def test_summary_money_fields_are_int(self) -> None:
        bundle = self._make_bundle(landed=True, tokens=1000, sol=_SOL_50M)
        summary = PartialFillReconciler.reconcile(bundle, expected_tokens_out=1000)
        assert isinstance(summary.net_sol_spent_lamports, int)
        assert isinstance(summary.tokens_received, int)
        assert isinstance(summary.total_tip_lamports, int)
        assert isinstance(summary.total_priority_lamports, int)
        assert isinstance(summary.partial_fill_ratio_bps, int)
        # Price is Decimal-as-string, not float.
        assert isinstance(summary.aggregate_effective_price_decimal, str)
        p = summary.aggregate_effective_price  # Decimal property
        assert isinstance(p, Decimal)

    def test_float_expected_tokens_rejected(self) -> None:
        """PartialFillReconciler must reject float expected_tokens_out."""
        bundle = self._make_bundle(landed=True, tokens=1000, sol=_SOL_50M)
        with pytest.raises(TypeError, match="must be int"):
            PartialFillReconciler.reconcile(bundle, expected_tokens_out=1000.0)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 8. Idempotent slot intent IDs
# ---------------------------------------------------------------------------


class TestIdempotentSlotIntentIds:
    def test_slot_intent_id_format(self) -> None:
        """Slot intent IDs must follow the format base_id:w<slot>."""
        import random

        received_ids: list[str] = []

        class CapturingVenue(SimulationVenue):
            def execute(self, intent: EntryIntent, event: LaunchEvent) -> FillResult:
                received_ids.append(intent.client_intent_id)
                return FillResult(landed=False, reason="dry_run", tip_lamports=0, priority_lamports=0)

        venue = CapturingVenue(rng=random.Random(42))
        ledger = make_ledger(cap=_CAP)
        orch = MultiWalletOrchestrator(
            venue=venue,
            wallet_ids=["wallet-0"],
            ledger=ledger,
            n_max=1,
        )
        base_id = "test-intent-0001"
        intent = make_entry_intent(intent_id=base_id, sol_in_lamports=_SOL_50M)
        event = make_launch_event()

        orch.execute_bundle(intent, event)
        assert len(received_ids) == 1
        assert received_ids[0] == f"{base_id}:w0"

    def test_deterministic_slot_ids_on_retry(self) -> None:
        """Same base_id produces the same slot intent IDs on repeated calls."""
        import random

        received_ids_run1: list[str] = []
        received_ids_run2: list[str] = []
        run = {"n": 1}

        class CapturingVenue(SimulationVenue):
            def execute(self, intent: EntryIntent, event: LaunchEvent) -> FillResult:
                if run["n"] == 1:
                    received_ids_run1.append(intent.client_intent_id)
                else:
                    received_ids_run2.append(intent.client_intent_id)
                return FillResult(landed=False, reason="dry_run", tip_lamports=0, priority_lamports=0)

        venue = CapturingVenue(rng=random.Random(42))
        ledger = make_ledger(cap=_CAP * 10)  # large cap to allow two calls
        orch = MultiWalletOrchestrator(
            venue=venue,
            wallet_ids=["wallet-0"],
            ledger=ledger,
            n_max=1,
        )
        base_id = "deterministic-id-007"
        intent = make_entry_intent(intent_id=base_id, sol_in_lamports=_SOL_50M)
        event = make_launch_event()

        # Run 1
        orch.execute_bundle(intent, event)
        run["n"] = 2
        # Run 2 — same intent (simulate a retry scenario)
        orch.execute_bundle(intent, event)

        assert received_ids_run1 == received_ids_run2, (
            "Slot intent IDs must be deterministic for the same base_id."
        )


# ---------------------------------------------------------------------------
# 9. Configuration guards
# ---------------------------------------------------------------------------


class TestConfigGuards:
    def test_empty_wallet_ids_rejected(self) -> None:
        with pytest.raises(MultiWalletConfigError, match="empty"):
            MultiWalletOrchestrator(
                venue=make_sim_venue(),
                wallet_ids=[],
                ledger=make_ledger(),
                n_max=1,
            )

    def test_duplicate_wallet_ids_rejected(self) -> None:
        """Duplicate wallet IDs must be rejected — each slot must be distinct."""
        with pytest.raises(MultiWalletConfigError, match="unique"):
            MultiWalletOrchestrator(
                venue=make_sim_venue(),
                wallet_ids=["wallet-0", "wallet-0"],
                ledger=make_ledger(),
                n_max=2,  # must override to allow 2 wallets in this test
            )

    def test_n_max_hard_ceiling_enforced(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """N_max > N_WALLETS_MAX_HARD_CEILING is refused even with activation gate open."""
        monkeypatch.setenv("N_WALLETS_MAX_ENABLED", "true")
        with pytest.raises(MultiWalletConfigError, match="ceiling"):
            MultiWalletOrchestrator(
                venue=make_sim_venue(),
                wallet_ids=["w0", "w1", "w2", "w3", "w4", "w5"],
                ledger=make_ledger(),
                n_max=N_WALLETS_MAX_HARD_CEILING + 1,  # 6 — over ceiling
            )

    def test_zero_cap_rejected(self) -> None:
        """MintExposureLedger with zero cap is invalid."""
        with pytest.raises(MultiWalletConfigError):
            MintExposureLedger(cap_lamports=0)

    def test_negative_cap_rejected(self) -> None:
        """MintExposureLedger with negative cap is invalid."""
        with pytest.raises(MultiWalletConfigError):
            MintExposureLedger(cap_lamports=-1)

    def test_invalid_n_max_env_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Non-integer N_WALLETS_MAX env raises MultiWalletConfigError."""
        monkeypatch.setenv("N_WALLETS_MAX_ENABLED", "true")
        monkeypatch.setenv("N_WALLETS_MAX", "not_an_int")
        with pytest.raises(MultiWalletConfigError, match="integer"):
            MultiWalletOrchestrator(
                venue=make_sim_venue(),
                wallet_ids=["wallet-0"],
                ledger=make_ledger(),
            )


# ---------------------------------------------------------------------------
# 10. Aggregate exposure <= C (end-to-end integration)
# ---------------------------------------------------------------------------


class TestAggregateExposureCapEndToEnd:
    def test_single_wallet_exposure_at_cap_ok(self) -> None:
        """Single wallet at exactly the cap succeeds."""
        venue = make_sim_venue()
        ledger = make_ledger(cap=_CAP)
        orch = MultiWalletOrchestrator(
            venue=venue,
            wallet_ids=["wallet-0"],
            ledger=ledger,
            n_max=1,
        )
        # Use exactly the cap.
        intent = make_entry_intent(sol_in_lamports=_CAP)
        event = make_launch_event()

        result = orch.execute_bundle(intent, event)
        # Regardless of landing outcome, no AntiClusterCapExceeded should be raised.
        assert isinstance(result, BundleFillResult)
        # After execution, exposure is recorded at the cap.
        assert ledger.get_exposure(_MOCK_MINT) == _CAP

    def test_second_bundle_same_mint_rejected_after_cap_used(self) -> None:
        """After first bundle fills the cap, second bundle raises AntiClusterCapExceeded."""
        venue = make_sim_venue()
        ledger = make_ledger(cap=_CAP)
        orch = MultiWalletOrchestrator(
            venue=venue,
            wallet_ids=["wallet-0"],
            ledger=ledger,
            n_max=1,
        )
        intent = make_entry_intent(sol_in_lamports=_CAP)
        event = make_launch_event()

        # First bundle fills the cap exactly.
        orch.execute_bundle(intent, event)
        assert ledger.get_exposure(_MOCK_MINT) == _CAP

        # Second bundle for same mint must be refused.
        intent2 = make_entry_intent(intent_id="second-intent", sol_in_lamports=1)
        with pytest.raises(AntiClusterCapExceeded):
            orch.execute_bundle(intent2, event)

    def test_exposure_matches_sol_in_lamports_for_landed_slot(self) -> None:
        """Ledger exposure exactly equals sol_in_lamports after a bundle completes."""
        import random

        class AlwaysLandVenue(SimulationVenue):
            def execute(self, intent: EntryIntent, event: LaunchEvent) -> FillResult:
                return FillResult(
                    landed=True,
                    reason="filled",
                    tokens_out=int(intent.sol_in_lamports * 9975 // 10000),
                    effective_price_decimal="1",
                    tip_lamports=intent.tip_lamports,
                    priority_lamports=20,
                    entry_slippage_bps=25,
                )

        venue = AlwaysLandVenue(rng=random.Random(42))
        sol_amount = _SOL_50M
        ledger = make_ledger(cap=_CAP)
        orch = MultiWalletOrchestrator(
            venue=venue,
            wallet_ids=["wallet-0"],
            ledger=ledger,
            n_max=1,
        )
        intent = make_entry_intent(sol_in_lamports=sol_amount)
        event = make_launch_event()

        orch.execute_bundle(intent, event)
        # The ledger records the RESERVED amount (sol_in_lamports), not the actual landed amount.
        # This is the blast-radius accounting — we track what we COMMITTED, not what landed.
        assert ledger.get_exposure(_MOCK_MINT) == sol_amount
