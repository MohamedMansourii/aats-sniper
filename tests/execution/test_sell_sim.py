"""T-329 — Honeypot/sellability sell-sim tests.

SELF-CHECK ITEMS (all mandatory per task mandate):
  1. sell-sim flags a honeypot fixture as NOT sellable (MockHoneypotRpcClient).
  2. sell-sim confirms a normal token as sellable (MockSellSimRpcClient).
  3. The risk gate seam (pretrade_gate.check_sellability) now calls the probe.
  4. No real submit — send_calls == 0 on every path.
  5. Pre-send sim proven: revert caught BEFORE any submit.
  6. Atomic-buy analogue: a failed sim returns sellable=False, not a partial.
  7. SellSimConfig rejects float money.
  8. SellSimResult rejects float money.
  9. Zero-output quote → NOT_SELLABLE (QUOTE_FAILED).
 10. High-sell-tax simulation → NOT_SELLABLE (HIGH_SELL_TAX).
 11. SignerRefused propagates → NOT_SELLABLE (SIGNER_REFUSED).
 12. submit_mode is always DRY_RUN (never LIVE/SIMULATION mix-up).
 13. pretrade_gate.SellSimProbe protocol is satisfied by SellSimVenue.
 14. Risk gate: no probe → refuse-by-default (NOT_SELLABLE) — T-323 seam unchanged.
 15. Idempotency: multiple is_sellable() calls on the same mint do not double-land.
 16. Effective-tax helper is integer-only (no float arithmetic).
"""
from __future__ import annotations

import pytest

from aats.contracts.venue import SubmitMode
from aats.execution.sell_sim import (
    DUST_SELL_AMOUNT,
    MockHoneypotRpcClient,
    MockSellSimRpcClient,
    SellSimConfig,
    SellSimReason,
    SellSimResult,
    SellSimVenue,
    make_sell_sim_probe,
)
from aats.execution.signer_client import MockSignerClient, MockSignerClientRefusing
from aats.risk.pretrade_gate import (
    PreTradeSafetyGate,
    RedFlag,
    SafetyInputs,
    SellSimProbe,
)

# ---------------------------------------------------------------------------
# Shared test fixtures
# ---------------------------------------------------------------------------

_SLOT = 300_000_001
_MINT = "HoneypotTokenMint1111111111111111111111111"
_NORMAL_MINT = "NormalTokenMint11111111111111111111111111111"
_WALLET_PUBKEY = "SellSimTestPubkey1111111111111111111111111"


@pytest.fixture()
def normal_rpc() -> MockSellSimRpcClient:
    """RPC mock that simulates a successful (sellable) token."""
    return MockSellSimRpcClient(
        simulate_sell_ok=True,
        sell_out_lamports=9_900,
        sell_tax_bps=100,
        current_slot=_SLOT,
    )


@pytest.fixture()
def honeypot_rpc() -> MockHoneypotRpcClient:
    """RPC mock that simulates a honeypot token (simulate reverts)."""
    return MockHoneypotRpcClient(current_slot=_SLOT)


@pytest.fixture()
def normal_signer() -> MockSignerClient:
    return MockSignerClient(wallet_pubkey=_WALLET_PUBKEY)


@pytest.fixture()
def refusing_signer() -> MockSignerClientRefusing:
    return MockSignerClientRefusing(reason="signer_program_not_allowlisted")


@pytest.fixture()
def normal_venue(normal_rpc: MockSellSimRpcClient, normal_signer: MockSignerClient) -> SellSimVenue:
    return SellSimVenue(
        rpc_client=normal_rpc,
        signer_client=normal_signer,
        config=SellSimConfig(),
        wallet_pubkey=_WALLET_PUBKEY,
    )


@pytest.fixture()
def honeypot_venue(
    honeypot_rpc: MockHoneypotRpcClient, normal_signer: MockSignerClient
) -> SellSimVenue:
    return SellSimVenue(
        rpc_client=honeypot_rpc,
        signer_client=normal_signer,
        config=SellSimConfig(),
        wallet_pubkey=_WALLET_PUBKEY,
    )


def make_clean_safety_inputs(mint: str = _NORMAL_MINT) -> SafetyInputs:
    """Build SafetyInputs that pass ALL hot-path checks."""
    return SafetyInputs(
        mint=mint,
        event_slot=_SLOT,
        event_block_time_ms=1_750_000_000_000,
        data_staleness_ms=100,
        freeze_authority=None,      # renounced
        mint_authority=None,        # renounced
        lp_locked_or_burned_bps=10_000,  # 100% locked
        dev_bundle_cluster_bps=500,      # 5% cluster
        holder_concentration_top10_bps=2_000,  # 20% top-10
        holder_count=50,  # E18: above the distinct-holder floor
        sell_tax_bps=50,
        buy_tax_bps=50,
        authorities_decoded=True,
    )


# ===========================================================================
# 1. Honeypot detection — the primary mandate of T-329
# ===========================================================================


class TestHoneypotDetection:
    """Core honeypot detection tests.  The most critical tests in this suite.

    A honey-pot fixture (MockHoneypotRpcClient) MUST be flagged as NOT sellable.
    """

    def test_honeypot_rpc_simulate_returns_revert(
        self, honeypot_rpc: MockHoneypotRpcClient
    ) -> None:
        """Verify the mock honeypot RPC returns a simulate failure."""
        from aats.execution.rpc_client import SimulateResult

        result: SimulateResult = honeypot_rpc.simulate_transaction("any_base64")  # type: ignore[assignment]
        assert not result.success, "MockHoneypotRpcClient must return simulate failure"
        assert result.revert_reason is not None
        assert "honeypot" in result.revert_reason or "error" in result.revert_reason.lower()

    def test_honeypot_venue_returns_not_sellable(
        self, honeypot_venue: SellSimVenue
    ) -> None:
        """A honeypot token must be flagged as NOT sellable."""
        result = honeypot_venue.simulate_sell(mint=_MINT, event_slot=_SLOT)
        assert not result.sellable, f"Honeypot must NOT be sellable; got reason={result.reason}"

    def test_honeypot_reason_is_sim_revert(
        self, honeypot_venue: SellSimVenue
    ) -> None:
        """Honeypot detection reason MUST be SIM_REVERT."""
        result = honeypot_venue.simulate_sell(mint=_MINT, event_slot=_SLOT)
        assert result.reason == SellSimReason.SIM_REVERT, (
            f"Expected SIM_REVERT; got {result.reason}"
        )

    def test_honeypot_detail_contains_revert_info(
        self, honeypot_venue: SellSimVenue
    ) -> None:
        """Detail string must carry revert information for the dashboard."""
        result = honeypot_venue.simulate_sell(mint=_MINT, event_slot=_SLOT)
        assert "simulate_revert" in result.detail, (
            f"Detail must reference simulate_revert; got '{result.detail}'"
        )

    def test_is_sellable_returns_false_for_honeypot(
        self, honeypot_venue: SellSimVenue
    ) -> None:
        """is_sellable() — the SellSimProbe protocol method — returns False for honeypot."""
        sellable = honeypot_venue.is_sellable(mint=_MINT, event_slot=_SLOT)
        assert sellable is False

    def test_honeypot_no_submit_ever(
        self, honeypot_rpc: MockHoneypotRpcClient, normal_signer: MockSignerClient
    ) -> None:
        """CRITICAL: no send_transaction() call even for honeypot detection — DRY-RUN."""
        venue = SellSimVenue(
            rpc_client=honeypot_rpc,
            signer_client=normal_signer,
            config=SellSimConfig(),
        )
        venue.simulate_sell(mint=_MINT, event_slot=_SLOT)
        assert honeypot_rpc.send_calls == 0, (
            "send_transaction() was called during honeypot sim — DRY-RUN violation."
        )


# ===========================================================================
# 2. Normal / sellable token
# ===========================================================================


class TestSellablePath:
    def test_normal_token_is_sellable(self, normal_venue: SellSimVenue) -> None:
        result = normal_venue.simulate_sell(mint=_NORMAL_MINT, event_slot=_SLOT)
        assert result.sellable, f"Normal token must be sellable; reason={result.reason}"
        assert result.reason == SellSimReason.SELLABLE

    def test_is_sellable_returns_true_for_normal(self, normal_venue: SellSimVenue) -> None:
        ok = normal_venue.is_sellable(mint=_NORMAL_MINT, event_slot=_SLOT)
        assert ok is True

    def test_normal_no_submit(
        self, normal_rpc: MockSellSimRpcClient, normal_signer: MockSignerClient
    ) -> None:
        """No send_transaction() call even for a normal token sell-sim."""
        venue = SellSimVenue(rpc_client=normal_rpc, signer_client=normal_signer)
        venue.simulate_sell(mint=_NORMAL_MINT, event_slot=_SLOT)
        assert normal_rpc.send_calls == 0, "send_transaction must NEVER be called in sell-sim."

    def test_simulate_called_once_per_sell_sim(
        self, normal_rpc: MockSellSimRpcClient, normal_signer: MockSignerClient
    ) -> None:
        """Simulate is called exactly once per simulate_sell() call."""
        venue = SellSimVenue(rpc_client=normal_rpc, signer_client=normal_signer)
        venue.simulate_sell(mint=_NORMAL_MINT, event_slot=_SLOT)
        assert normal_rpc.simulate_calls == 1


# ===========================================================================
# 3. Pre-send sim proven: revert caught BEFORE any submit
# ===========================================================================


class TestPreSendSimProven:
    """Verifies execution-venue.md Standards §1: simulate before sign/submit."""

    def test_revert_caught_before_submit(
        self, honeypot_rpc: MockHoneypotRpcClient, normal_signer: MockSignerClient
    ) -> None:
        """A reverting tx is caught by simulateTransaction — NO submit attempted."""
        venue = SellSimVenue(rpc_client=honeypot_rpc, signer_client=normal_signer)
        result = venue.simulate_sell(mint=_MINT, event_slot=_SLOT)
        # Revert caught
        assert not result.sellable
        assert result.reason == SellSimReason.SIM_REVERT
        # No submit
        assert honeypot_rpc.send_calls == 0, "send_transaction must not be called after sim revert"

    def test_sim_revert_reason_propagated(
        self, honeypot_rpc: MockHoneypotRpcClient, normal_signer: MockSignerClient
    ) -> None:
        """The revert reason from the sim is surfaced in the result for logs."""
        venue = SellSimVenue(rpc_client=honeypot_rpc, signer_client=normal_signer)
        result = venue.simulate_sell(mint=_MINT, event_slot=_SLOT)
        assert result.sim_revert_reason is not None
        assert "honeypot" in result.sim_revert_reason or "error" in result.sim_revert_reason.lower()


# ===========================================================================
# 4. DRY-RUN submit_mode invariant
# ===========================================================================


class TestDryRunInvariant:
    def test_submit_mode_is_dry_run(self) -> None:
        """SellSimVenue always exposes DRY_RUN submit_mode."""
        venue = SellSimVenue()
        assert venue.submit_mode == SubmitMode.DRY_RUN

    def test_submit_mode_class_attribute(self) -> None:
        """The class-level attribute is DRY_RUN."""
        assert SellSimVenue.submit_mode == SubmitMode.DRY_RUN


# ===========================================================================
# 5. Money / type invariants (data-models.md §0)
# ===========================================================================


class TestMoneyInvariants:
    def test_sell_sim_config_rejects_float_max_tax(self) -> None:
        with pytest.raises(TypeError, match="float"):
            SellSimConfig(max_sell_tax_bps=0.1)  # type: ignore[arg-type]

    def test_sell_sim_config_rejects_float_min_out_ratio(self) -> None:
        with pytest.raises(TypeError, match="float"):
            SellSimConfig(min_out_ratio_bps=0.5)  # type: ignore[arg-type]

    def test_sell_sim_config_rejects_float_dust_amount(self) -> None:
        with pytest.raises(TypeError, match="float"):
            SellSimConfig(dust_sell_amount=1.0)  # type: ignore[arg-type]

    def test_sell_sim_config_rejects_zero_dust(self) -> None:
        with pytest.raises(ValueError, match="dust_sell_amount"):
            SellSimConfig(dust_sell_amount=0)

    def test_sell_sim_result_rejects_float_dust_amount_in(self) -> None:
        with pytest.raises(TypeError, match="float"):
            SellSimResult(
                sellable=True,
                reason=SellSimReason.SELLABLE,
                mint=_MINT,
                event_slot=_SLOT,
                dust_amount_in=1.0,  # type: ignore[arg-type]
            )

    def test_sell_sim_result_rejects_float_out_lamports(self) -> None:
        with pytest.raises(TypeError, match="float"):
            SellSimResult(
                sellable=True,
                reason=SellSimReason.SELLABLE,
                mint=_MINT,
                event_slot=_SLOT,
                simulated_out_lamports=9900.0,  # type: ignore[arg-type]
            )

    def test_sell_sim_result_rejects_float_tax_bps(self) -> None:
        with pytest.raises(TypeError, match="float"):
            SellSimResult(
                sellable=False,
                reason=SellSimReason.HIGH_SELL_TAX,
                mint=_MINT,
                event_slot=_SLOT,
                effective_sell_tax_bps=100.0,  # type: ignore[arg-type]
            )

    def test_mock_honeypot_rpc_rejects_float_sell_out(self) -> None:
        with pytest.raises(TypeError, match="float"):
            MockSellSimRpcClient(sell_out_lamports=9900.0)  # type: ignore[arg-type]

    def test_mock_honeypot_rpc_rejects_float_tax_bps(self) -> None:
        with pytest.raises(TypeError, match="float"):
            MockSellSimRpcClient(sell_tax_bps=100.0)  # type: ignore[arg-type]

    def test_effective_tax_computation_is_integer(self) -> None:
        """_compute_effective_tax_bps must return int, not float."""
        result = SellSimVenue._compute_effective_tax_bps(simulated_out=9_900, quoted_out=10_000)
        assert isinstance(result, int)
        assert result == 100  # (10000 - 9900) * 10000 // 10000 = 1000000 // 10000 = 100

    def test_effective_tax_zero_for_equal_amounts(self) -> None:
        result = SellSimVenue._compute_effective_tax_bps(simulated_out=10_000, quoted_out=10_000)
        assert result == 0

    def test_effective_tax_zero_for_zero_quote(self) -> None:
        result = SellSimVenue._compute_effective_tax_bps(simulated_out=0, quoted_out=0)
        assert result == 0

    def test_effective_tax_zero_when_sim_exceeds_quote(self) -> None:
        """If simulated_out > quoted_out (mock rounding), tax is 0, not negative."""
        result = SellSimVenue._compute_effective_tax_bps(simulated_out=10_001, quoted_out=10_000)
        assert result == 0


# ===========================================================================
# 6. Zero-output scenario (vanishing liquidity / pool drain honeypot)
# ===========================================================================


class TestZeroOutputScenario:
    def test_zero_output_quote_returns_not_sellable(self) -> None:
        """If the sell quote returns 0 lamports, the token is flagged as not sellable."""
        # quote_out_lamports=0 means the pool has no liquidity / no route.
        # This triggers QUOTE_FAILED before even building a tx.
        rpc = MockSellSimRpcClient(
            simulate_sell_ok=True,
            quote_out_lamports=0,   # zero theoretical output — no liquidity
            sell_out_lamports=0,
        )
        venue = SellSimVenue(rpc_client=rpc, signer_client=MockSignerClient())
        result = venue.simulate_sell(mint=_MINT, event_slot=_SLOT)
        assert not result.sellable
        assert result.reason == SellSimReason.QUOTE_FAILED
        # Must not submit even on zero quote
        assert rpc.send_calls == 0


# ===========================================================================
# 7. High sell-tax scenario
# ===========================================================================


class TestHighSellTaxScenario:
    def test_high_sell_tax_returns_not_sellable(self) -> None:
        """If the effective sell tax exceeds max_sell_tax_bps, token is not sellable.

        quote_out_lamports=10_000 (theoretical fair) vs sell_out_lamports=500 (actual
        sim output) → effective_tax_bps = (10_000-500)*10_000//10_000 = 9500 bps = 95%.
        This vastly exceeds max_sell_tax_bps=1000 (10%).
        """
        rpc = MockSellSimRpcClient(
            simulate_sell_ok=True,
            quote_out_lamports=10_000,  # theoretical fair
            sell_out_lamports=500,      # only 5% returned — 95% tax
            sell_tax_bps=9_500,
        )
        # Default max_sell_tax_bps = 1000 (10%). Effective tax here = 9500 bps = 95%.
        venue = SellSimVenue(rpc_client=rpc, signer_client=MockSignerClient())
        result = venue.simulate_sell(mint=_MINT, event_slot=_SLOT)
        assert not result.sellable
        assert result.reason == SellSimReason.HIGH_SELL_TAX
        assert result.effective_sell_tax_bps > 1_000  # above threshold
        assert rpc.send_calls == 0

    def test_acceptable_tax_is_sellable(self) -> None:
        """A token with a 1% effective sell tax is sellable.

        quote_out_lamports=10_000 (fair) vs sell_out_lamports=9_900 (1% tax) →
        effective_tax_bps = (10_000-9_900)*10_000//10_000 = 100 bps = 1%.
        """
        rpc = MockSellSimRpcClient(
            simulate_sell_ok=True,
            quote_out_lamports=10_000,  # theoretical fair
            sell_out_lamports=9_900,    # 1% tax applied
            sell_tax_bps=100,
        )
        venue = SellSimVenue(rpc_client=rpc, signer_client=MockSignerClient())
        result = venue.simulate_sell(mint=_NORMAL_MINT, event_slot=_SLOT)
        assert result.sellable
        assert result.effective_sell_tax_bps == 100  # 1% = 100 bps
        assert rpc.send_calls == 0


# ===========================================================================
# 8. Signer refusal scenario
# ===========================================================================


class TestSignerRefusalScenario:
    def test_signer_refused_returns_not_sellable(
        self, normal_rpc: MockSellSimRpcClient, refusing_signer: MockSignerClientRefusing
    ) -> None:
        """If signer refuses, token is flagged not sellable (program not allowlisted)."""
        venue = SellSimVenue(rpc_client=normal_rpc, signer_client=refusing_signer)
        result = venue.simulate_sell(mint=_MINT, event_slot=_SLOT)
        assert not result.sellable
        assert result.reason == SellSimReason.SIGNER_REFUSED
        # No simulate call after signer refusal
        assert normal_rpc.send_calls == 0


# ===========================================================================
# 9. Risk gate seam wiring (T-323 integration)
# ===========================================================================


class TestRiskGateSeamWiring:
    """Verify the seam is correctly wired: check_sellability uses the probe."""

    def test_gate_check_sellability_with_probe_sellable(
        self, normal_venue: SellSimVenue
    ) -> None:
        """check_sellability with a probe that says sellable returns None (no flag)."""
        gate = PreTradeSafetyGate()
        inputs = make_clean_safety_inputs()
        hit = gate.check_sellability(inputs=inputs, probe=normal_venue)
        assert hit is None, f"No flag expected for sellable token; got {hit}"

    def test_gate_check_sellability_with_probe_honeypot(
        self, honeypot_venue: SellSimVenue
    ) -> None:
        """check_sellability with a honeypot probe returns NOT_SELLABLE flag."""
        gate = PreTradeSafetyGate()
        inputs = make_clean_safety_inputs()
        hit = gate.check_sellability(inputs=inputs, probe=honeypot_venue)
        assert hit is not None, "Expected NOT_SELLABLE flag for honeypot"
        assert hit.flag == RedFlag.NOT_SELLABLE

    def test_gate_check_sellability_no_probe_refuse_by_default(self) -> None:
        """Without a probe (T-329 not wired), gate refuses by default (NOT_SELLABLE)."""
        gate = PreTradeSafetyGate()
        inputs = make_clean_safety_inputs()
        hit = gate.check_sellability(inputs=inputs, probe=None)
        assert hit is not None, "Must refuse-by-default when no probe supplied"
        assert hit.flag == RedFlag.NOT_SELLABLE

    def test_sell_sim_probe_satisfies_protocol(self, normal_venue: SellSimVenue) -> None:
        """SellSimVenue satisfies the SellSimProbe Protocol (runtime check)."""
        assert isinstance(normal_venue, SellSimProbe), (
            "SellSimVenue must satisfy pretrade_gate.SellSimProbe protocol"
        )

    def test_honeypot_venue_satisfies_protocol(self, honeypot_venue: SellSimVenue) -> None:
        """Both normal and honeypot venues must satisfy the Protocol."""
        assert isinstance(honeypot_venue, SellSimProbe)


# ===========================================================================
# 10. Idempotency / multiple calls
# ===========================================================================


class TestIdempotency:
    def test_multiple_is_sellable_calls_no_double_submit(
        self, normal_rpc: MockSellSimRpcClient, normal_signer: MockSignerClient
    ) -> None:
        """Multiple is_sellable() calls on same mint never submit a transaction."""
        venue = SellSimVenue(rpc_client=normal_rpc, signer_client=normal_signer)
        for _ in range(5):
            venue.is_sellable(mint=_NORMAL_MINT, event_slot=_SLOT)
        assert normal_rpc.send_calls == 0

    def test_multiple_calls_consistent_result(
        self, normal_venue: SellSimVenue
    ) -> None:
        """Multiple calls on same mint return consistent results."""
        results = [
            normal_venue.is_sellable(mint=_NORMAL_MINT, event_slot=_SLOT)
            for _ in range(3)
        ]
        assert all(r is True for r in results), "Consistent sellable result expected"

    def test_honeypot_multiple_calls_consistent(
        self, honeypot_venue: SellSimVenue
    ) -> None:
        """Multiple calls on a honeypot return consistently False."""
        results = [
            honeypot_venue.is_sellable(mint=_MINT, event_slot=_SLOT)
            for _ in range(3)
        ]
        assert all(r is False for r in results), "Consistent not-sellable result expected"


# ===========================================================================
# 11. make_sell_sim_probe factory
# ===========================================================================


class TestFactory:
    def test_factory_returns_sell_sim_venue(self) -> None:
        probe = make_sell_sim_probe()
        assert isinstance(probe, SellSimVenue)

    def test_factory_with_mock_rpc(self) -> None:
        from aats.execution.rpc_client import MockRpcClient

        probe = make_sell_sim_probe(rpc_client=MockRpcClient())
        assert isinstance(probe, SellSimVenue)

    def test_factory_satisfies_protocol(self) -> None:
        probe = make_sell_sim_probe()
        assert isinstance(probe, SellSimProbe)

    def test_factory_no_submit(self) -> None:
        from aats.execution.rpc_client import MockRpcClient

        rpc = MockRpcClient()
        probe = make_sell_sim_probe(rpc_client=rpc)
        probe.is_sellable(mint=_NORMAL_MINT, event_slot=_SLOT)
        assert rpc.send_calls == 0


# ===========================================================================
# 12. SellSimConfig validation
# ===========================================================================


class TestSellSimConfigValidation:
    def test_default_config_is_valid(self) -> None:
        cfg = SellSimConfig()
        assert cfg.dust_sell_amount == DUST_SELL_AMOUNT
        assert cfg.max_sell_tax_bps == 1_000
        assert cfg.min_out_ratio_bps == 5_000

    def test_config_rejects_negative_tax(self) -> None:
        with pytest.raises(ValueError, match="non-negative"):
            SellSimConfig(max_sell_tax_bps=-1)

    def test_config_rejects_tax_over_10000(self) -> None:
        with pytest.raises(ValueError, match="max_sell_tax_bps"):
            SellSimConfig(max_sell_tax_bps=10_001)

    def test_config_rejects_min_out_over_10000(self) -> None:
        with pytest.raises(ValueError, match="min_out_ratio_bps"):
            SellSimConfig(min_out_ratio_bps=10_001)


# ===========================================================================
# 13. SellSimResult structure
# ===========================================================================


class TestSellSimResultStructure:
    def test_sellable_result_is_frozen(self) -> None:
        result = SellSimResult(
            sellable=True,
            reason=SellSimReason.SELLABLE,
            mint=_NORMAL_MINT,
            event_slot=_SLOT,
        )
        with pytest.raises((AttributeError, TypeError)):
            result.sellable = False  # type: ignore[misc]

    def test_not_sellable_result_fields(self) -> None:
        result = SellSimResult(
            sellable=False,
            reason=SellSimReason.SIM_REVERT,
            mint=_MINT,
            event_slot=_SLOT,
            sim_revert_reason="custom_program_error_0x1775",
            detail="simulate_revert:custom_program_error_0x1775",
        )
        assert result.sellable is False
        assert result.reason == SellSimReason.SIM_REVERT
        assert isinstance(result.effective_sell_tax_bps, int)
        assert isinstance(result.sim_cu_consumed, int)


# ===========================================================================
# 14. No win_rate field anywhere (honesty clause)
# ===========================================================================


class TestNoWinRateField:
    def test_sell_sim_result_has_no_win_rate_field(self) -> None:
        """No win_rate field exists on SellSimResult (honesty clause)."""
        result = SellSimResult(
            sellable=True,
            reason=SellSimReason.SELLABLE,
            mint=_NORMAL_MINT,
            event_slot=_SLOT,
        )
        assert not hasattr(result, "win_rate"), "win_rate field must not exist (honesty clause)"

    def test_sell_sim_venue_has_no_win_rate_field(self, normal_venue: SellSimVenue) -> None:
        assert not hasattr(normal_venue, "win_rate")
