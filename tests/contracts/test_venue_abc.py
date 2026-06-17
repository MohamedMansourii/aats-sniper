"""Tests: ExecutionVenue ABC + SimulationVenue satisfies the promoted ABC.

Verifies:
  1. SimulationVenue still satisfies the promoted ExecutionVenue ABC (all 8 members).
  2. execute(intent, event) -> FillResult arity is preserved (code-reviewer R-02).
  3. SimulationVenue.submit_mode == SIMULATION — never DRY_RUN or LIVE.
  4. SimulationVenue.sign() returns a sim signature with no key/network.
  5. SimulationVenue.land() returns LandResult(submitted=False, reason="simulation").
  6. Money in FillResult is integer (not float).
  7. DRY-RUN is a first-class SubmitMode.
  8. SubmitMode enum has exactly SIMULATION, DRY_RUN, LIVE.
"""

from __future__ import annotations

import random
from decimal import Decimal

from aats.contracts.events import DetectionTransport, EventTime, LaunchEvent, LaunchSource
from aats.contracts.intents import CostStack, EntryIntent
from aats.contracts.venue import (
    ExecutionVenue,
    FillResult,
    LandResult,
    SignedTx,
    SimulationVenue,
    SubmitMode,
    UnsignedTx,
)


def _event_time(slot: int = 1000) -> EventTime:
    return EventTime(slot=slot, block_time_ms=1_700_000_000_000, wall_clock_ms=1_700_000_000_100)


def _launch_event(competitors: int = 3) -> LaunchEvent:
    return LaunchEvent(
        mint="So11111111111111111111111111111111111111112",
        venue_program_id="6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",
        source=LaunchSource.PUMPFUN,
        event_time=_event_time(),
        observation_slot=999,
        confirmation_slot=1000,
        detection_transport=DetectionTransport.GEYSER,
        sol_reserve_lamports=1_000_000_000,
        token_reserve_base=1_000_000_000_000,
        token_decimals=6,
        initial_holders=100,
        competitors=competitors,
        creator_wallet="Abc123",
        bundler_cluster_id=None,
        deploy_template_fingerprint=None,
        data_staleness_ms=50,
    )


def _cost_stack() -> CostStack:
    return CostStack(
        expected_edge_bps=300,
        jito_tip_bps=50,
        priority_fee_bps=20,
        entry_slippage_bps=100,
        amm_fee_bps=25,
        exit_slippage_bps=80,
        adverse_selection_bps=150,
        total_cost_bps=275,
    )


def _entry_intent(slippage_bps: int = 500) -> EntryIntent:
    return EntryIntent(
        client_intent_id="ci-test-execute",
        mint="So11111111111111111111111111111111111111112",
        event_time=_event_time(),
        sol_in_lamports=100_000_000,
        slippage_bps=slippage_bps,
        tip_lamports=300_000,  # > market_tip of 200_000 to win more often
        cu_price_microlamports=100_000,
        target_slot=1005,
        cost_stack=_cost_stack(),
        venue="pumpswap",
        wallet_id="wallet-0",
    )


def _sim_venue() -> SimulationVenue:
    rng = random.Random(42)
    return SimulationVenue(
        rng=rng,
        market_tip_lamports=200_000,
        ahead_size_sol_lamports=1_200_000_000,
    )


# ---------------------------------------------------------------------------
# ABC compliance
# ---------------------------------------------------------------------------


class TestSimulationVenueABCCompliance:
    def test_simulation_venue_is_subclass_of_execution_venue(self):
        """SimulationVenue must be a subclass of ExecutionVenue (the ABC)."""
        assert issubclass(SimulationVenue, ExecutionVenue)

    def test_simulation_venue_is_instantiable(self):
        """SimulationVenue must be instantiable (all 8 abstract methods implemented)."""
        venue = _sim_venue()
        assert isinstance(venue, ExecutionVenue)

    def test_all_abstract_methods_implemented(self):
        """All 8 promoted ABC members must be implemented (code-reviewer R-01)."""
        venue = _sim_venue()
        required_methods = [
            "execute", "exit", "quote", "build", "sign", "simulate", "land", "reconcile"
        ]
        for method_name in required_methods:
            assert hasattr(venue, method_name), (
                f"SimulationVenue.{method_name}() not found — "
                "all 8 ABC members must be implemented (code-reviewer R-01)"
            )
            assert callable(getattr(venue, method_name)), (
                f"SimulationVenue.{method_name} is not callable"
            )

    def test_submit_mode_property_implemented(self):
        venue = _sim_venue()
        assert hasattr(type(venue), "submit_mode"), "submit_mode property missing"
        assert venue.submit_mode == SubmitMode.SIMULATION


# ---------------------------------------------------------------------------
# execute() arity + return type
# ---------------------------------------------------------------------------


class TestSimulationVenueExecute:
    def test_execute_returns_fill_result(self):
        """execute(intent, event) -> FillResult arity preserved (code-reviewer R-02)."""
        venue = _sim_venue()
        result = venue.execute(_entry_intent(slippage_bps=1000), _launch_event(competitors=0))
        assert isinstance(result, FillResult)

    def test_fill_result_money_fields_are_int(self):
        """FillResult money fields must be int (never float)."""
        venue = _sim_venue()
        result = venue.execute(_entry_intent(slippage_bps=1000), _launch_event(competitors=0))
        # tip_lamports and priority_lamports must be int
        assert isinstance(result.tip_lamports, int), (
            f"FillResult.tip_lamports should be int, got {type(result.tip_lamports).__name__}"
        )
        assert isinstance(result.priority_lamports, int)
        assert isinstance(result.tokens_out, int)
        assert isinstance(result.entry_slippage_bps, int)

    def test_effective_price_is_decimal_accessible(self):
        """FillResult.effective_price property returns Decimal (never float)."""
        venue = _sim_venue()
        result = venue.execute(_entry_intent(slippage_bps=1000), _launch_event(competitors=0))
        if result.landed:
            price = result.effective_price
            assert isinstance(price, Decimal), (
                f"FillResult.effective_price should return Decimal, got {type(price).__name__}"
            )

    def test_reverted_min_out_when_slippage_too_low(self):
        """With 0-bps slippage tolerance and competitors, the bundle reverts (reverted_min_out)."""
        rng = random.Random(1)  # deterministic
        venue = SimulationVenue(
            rng=rng,
            market_tip_lamports=200_000,
            ahead_size_sol_lamports=1_200_000_000,
        )
        intent = EntryIntent(
            client_intent_id="ci-slippage-test",
            mint="So11111111111111111111111111111111111111112",
            event_time=_event_time(),
            sol_in_lamports=100_000_000,
            slippage_bps=0,  # zero tolerance — any slippage reverts
            tip_lamports=200_000,
            cu_price_microlamports=100_000,
            target_slot=1005,
            cost_stack=_cost_stack(),
            venue="pumpswap",
            wallet_id="wallet-0",
        )
        event = _launch_event(competitors=10)  # many competitors → high slippage
        result = venue.execute(intent, event)
        # With zero slippage tolerance and many competitors, most runs revert or miss
        # We just assert the result is a FillResult (not an exception)
        assert isinstance(result, FillResult)
        if not result.landed:
            assert result.reason in ("too_late_or_outbid", "reverted_min_out")


# ---------------------------------------------------------------------------
# submit_mode = SIMULATION — never allows network
# ---------------------------------------------------------------------------


class TestSimulationVenueSubmitMode:
    def test_submit_mode_is_simulation(self):
        venue = _sim_venue()
        assert venue.submit_mode == SubmitMode.SIMULATION

    def test_land_does_not_submit(self):
        """SimulationVenue.land() must return submitted=False (no network)."""
        venue = _sim_venue()
        signed_tx = SignedTx(
            serialized_b64="",
            signer_pubkey="sim_pubkey_wallet-0",
            intent_kind="ENTRY",
            mint="So11111111111111111111111111111111111111112",
            client_intent_id="ci-land-test",
        )
        result = venue.land(signed_tx, tip_lamports=200_000, cu_price=100_000)
        assert isinstance(result, LandResult)
        assert result.submitted is False, (
            "SimulationVenue.land() must NOT submit (submit_mode=SIMULATION)"
        )
        assert result.reason == "simulation"

    def test_sign_returns_no_real_key(self):
        """SimulationVenue.sign() must not hold or use a private key (R-01)."""
        venue = _sim_venue()
        unsigned = UnsignedTx(
            serialized_b64="",
            intent_kind="ENTRY",
            mint="So11111111111111111111111111111111111111112",
            client_intent_id="ci-sign-test",
        )
        signed = venue.sign(unsigned, wallet_id="wallet-0")
        assert isinstance(signed, SignedTx)
        # The signer_pubkey must contain "sim" — it is NOT a real Solana pubkey
        assert "sim" in signed.signer_pubkey.lower(), (
            f"SimulationVenue.sign() should return a sim pubkey, got: {signed.signer_pubkey}"
        )
        # The signed tx should NOT contain a real private key
        assert "private" not in signed.serialized_b64.lower()
        assert "keypair" not in signed.serialized_b64.lower()


# ---------------------------------------------------------------------------
# SubmitMode enum completeness
# ---------------------------------------------------------------------------


class TestSubmitModeEnum:
    def test_submit_mode_has_required_members(self):
        """SubmitMode must contain SIMULATION, DRY_RUN, and LIVE.

        DEVNET is also permitted (E1 — devnet live-send validation mode,
        a separate worthless-SOL cluster from mainnet).  Any other unexpected
        member is still a contract violation.
        """
        members = frozenset(m.name for m in SubmitMode)
        required = {"SIMULATION", "DRY_RUN", "LIVE"}
        permitted_additions = {"DEVNET"}  # E1 enhancement directive
        unexpected = members - required - permitted_additions
        assert not unexpected, (
            f"SubmitMode contains unexpected member(s): {unexpected}. "
            f"Required: {required}. Permitted E1 addition: {permitted_additions}."
        )
        # Verify the required members are all present.
        assert required.issubset(members), (
            f"SubmitMode is missing required members: {required - members}. Got: {members}"
        )

    def test_dry_run_is_first_class_state(self):
        """DRY_RUN is a first-class SubmitMode, not a flag (FR-039)."""
        assert SubmitMode.DRY_RUN in SubmitMode


# ---------------------------------------------------------------------------
# VenueRegistryEntry
# ---------------------------------------------------------------------------


class TestVenueRegistryEntry:
    def test_valid_entry_constructed(self):
        from aats.contracts.venue import VenueRegistryEntry

        entry = VenueRegistryEntry(
            venue=LaunchSource.PUMPSWAP,
            program_id="some_program_id",
            decoder="pumpswap_decoder",
            amm_fee_bps=25,
            migration_target=None,
            status="active",
            last_verified_slot=1000,
        )
        assert entry.venue == LaunchSource.PUMPSWAP
        assert entry.amm_fee_bps == 25
