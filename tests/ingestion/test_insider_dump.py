"""E14a — tests for insider/dev-sell distribution detection
(`aats.ingestion.insider_dump`).

Covers (VERIFY criteria):
  - a creator sell crossing the threshold emits the flag with correct
    fraction_sold_bps + event_slot
  - a below-threshold sell does not emit
  - a non-creator/non-cluster sell does not emit
  - refuse-by-default: undecodable/unknown held-supply is NEVER silently
    treated as "no dump" (counted distinctly, logged, never swallowed)
  - event-time honesty: event_slot/block_time_ms come from on-chain fields
    only, never wall-clock
  - idempotent + order-tolerant: duplicated + out-of-order replay produces
    the IDENTICAL final state as in-order, once-each delivery
  - the StateStore flag mirrors narrative_failure_flag exactly (same TTL
    mechanism — see tests/controller/test_state_store.py for the isolated
    StateStore-side proof; here we prove the detector actually calls it)
  - a real captured pump.fun sell-transaction fixture adapts correctly
  - the bus publisher wire format
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock

import pytest

from aats.contracts.events import DetectionTransport
from aats.controller.state import InMemoryStateStore
from aats.ingestion.decoders import EventKind, InstructionRouter
from aats.ingestion.insider_dump import (
    MAXLEN_INSIDER_DUMP,
    STREAM_INSIDER_DUMP,
    InsiderDumpBusPublisher,
    InsiderDumpConfig,
    InsiderDumpDetector,
    InsiderDumpSignal,
    SellObservation,
    StaticHeldSupplyProvider,
    WalletRole,
    sell_observation_from_launch_event,
)
from tests.ingestion.fixtures import make_pumpfun_buy_tx, make_pumpfun_sell_tx, make_registry

MINT = "MintPumpFun1111111111111111111111111111111111"
CREATOR = "CreatorWallet11111111111111111111111111111111"
CLUSTER_A = "ClusterWalletA11111111111111111111111111111111"
CLUSTER_B = "ClusterWalletB11111111111111111111111111111111"
OUTSIDER = "RandomWallet1111111111111111111111111111111111"


def _obs(
    *,
    wallet: str,
    amount: int,
    slot: int,
    mint: str = MINT,
    sig: str = "sig1",
    ix: int = 0,
    block_time_ms: int = 1_718_700_000_000,
) -> SellObservation:
    return SellObservation(
        mint=mint,
        wallet=wallet,
        token_amount_sold_base=amount,
        event_slot=slot,
        block_time_ms=block_time_ms,
        signature=sig,
        instruction_index=ix,
    )


def _detector(
    *,
    threshold_bps: int = 2_000,
    held_supply: int | None = 1_000_000,
    flag_sink: object | None = None,
) -> tuple[InsiderDumpDetector, StaticHeldSupplyProvider]:
    provider = StaticHeldSupplyProvider()
    if held_supply is not None:
        provider.set_held_supply(MINT, CREATOR, held_supply)
        provider.set_held_supply(MINT, CLUSTER_A, held_supply)
    det = InsiderDumpDetector(
        config=InsiderDumpConfig(threshold_bps=threshold_bps),
        held_supply=provider,
        flag_sink=flag_sink,  # type: ignore[arg-type]
    )
    det.register_creator(MINT, CREATOR)
    det.register_cluster(MINT, {CLUSTER_A, CLUSTER_B})
    return det, provider


# ---------------------------------------------------------------------------
# AC: creator sell crossing threshold emits flag with correct
#     fraction_sold_bps + event_slot
# ---------------------------------------------------------------------------


class TestThresholdCrossing:
    def test_creator_sell_crossing_threshold_emits_signal(self):
        det, _ = _detector(threshold_bps=2_000, held_supply=1_000_000)
        # Sell 25% of the 1,000,000 baseline -> 2500bps >= 2000bps threshold
        sig = det.on_sell(_obs(wallet=CREATOR, amount=250_000, slot=555), is_open_position=True)
        assert sig is not None
        assert sig.mint == MINT
        assert sig.wallet == CREATOR
        assert sig.role is WalletRole.CREATOR
        assert sig.fraction_sold_bps == 2_500
        assert sig.event_slot == 555
        assert sig.cumulative_sold_base == 250_000
        assert sig.held_supply_base == 1_000_000

    def test_flag_written_to_state_store_mirrors_narrative_failure(self):
        store = InMemoryStateStore()
        det, _ = _detector(threshold_bps=2_000, held_supply=1_000_000, flag_sink=store)
        assert store.get_insider_dump_flag(MINT) is False
        det.on_sell(_obs(wallet=CREATOR, amount=250_000, slot=777), is_open_position=True)
        assert store.get_insider_dump_flag(MINT) is True
        detail = store.get_insider_dump_detail(MINT)
        assert detail == (2_500, 777)

    def test_cluster_sell_crossing_threshold_emits_signal(self):
        det, _ = _detector(threshold_bps=5_000, held_supply=1_000_000)
        sig = det.on_sell(_obs(wallet=CLUSTER_A, amount=600_000, slot=42), is_open_position=True)
        assert sig is not None
        assert sig.role is WalletRole.CLUSTER
        assert sig.fraction_sold_bps == 6_000

    def test_cumulative_across_multiple_sells_crosses_threshold(self):
        det, _ = _detector(threshold_bps=3_000, held_supply=1_000_000)
        sig1 = det.on_sell(
            _obs(wallet=CREATOR, amount=100_000, slot=1, sig="s1"), is_open_position=True
        )
        assert sig1 is None  # 10% -- below 30%
        sig2 = det.on_sell(
            _obs(wallet=CREATOR, amount=250_000, slot=2, sig="s2"), is_open_position=True
        )
        assert sig2 is not None  # cumulative 35% -- crosses 30%
        assert sig2.fraction_sold_bps == 3_500
        assert sig2.cumulative_sold_base == 350_000
        assert sig2.event_slot == 2  # the CROSSING sell's slot, not the first sell's


# ---------------------------------------------------------------------------
# AC: a below-threshold sell does not emit
# ---------------------------------------------------------------------------


class TestBelowThreshold:
    def test_below_threshold_sell_holds(self):
        det, _ = _detector(threshold_bps=5_000, held_supply=1_000_000)
        sig = det.on_sell(_obs(wallet=CREATOR, amount=100_000, slot=1), is_open_position=True)
        assert sig is None
        assert det.stats.signals_emitted == 0

    def test_exactly_at_threshold_boundary_fires(self):
        det, _ = _detector(threshold_bps=2_000, held_supply=1_000_000)
        sig = det.on_sell(_obs(wallet=CREATOR, amount=200_000, slot=1), is_open_position=True)
        assert sig is not None  # ">=" boundary -- exactly at threshold DOES fire
        assert sig.fraction_sold_bps == 2_000

    def test_one_bps_below_threshold_does_not_fire(self):
        det, _ = _detector(threshold_bps=2_000, held_supply=1_000_000)
        sig = det.on_sell(_obs(wallet=CREATOR, amount=199_999, slot=1), is_open_position=True)
        assert sig is None  # fraction floors to 1999bps, strictly below 2000


# ---------------------------------------------------------------------------
# AC: a non-creator/non-cluster sell does not emit
# ---------------------------------------------------------------------------


class TestUntrackedWallet:
    def test_outsider_sell_never_triggers_regardless_of_size(self):
        det, _ = _detector(threshold_bps=1, held_supply=1_000_000)
        sig = det.on_sell(_obs(wallet=OUTSIDER, amount=999_999, slot=1), is_open_position=True)
        assert sig is None
        assert det.stats.untracked_sells == 1
        assert det.stats.signals_emitted == 0

    def test_outsider_never_registered_in_role_lookup(self):
        det, _ = _detector()
        assert det._role_of(MINT, OUTSIDER) is None
        assert det._role_of(MINT, CREATOR) is WalletRole.CREATOR
        assert det._role_of(MINT, CLUSTER_A) is WalletRole.CLUSTER


# ---------------------------------------------------------------------------
# AC: refuse-by-default -- undecodable supply/holder data must NOT silently
#     read as "no dump"
# ---------------------------------------------------------------------------


class TestRefuseByDefault:
    def test_unknown_held_supply_refuses_not_silently_clean(self):
        det, _ = _detector(held_supply=None)  # nothing registered -> unknown baseline
        sig = det.on_sell(_obs(wallet=CREATOR, amount=999_999, slot=1), is_open_position=True)
        assert sig is None
        # The critical assertion: this must be counted as a REFUSAL, distinct
        # from a genuine below-threshold no-op or an untracked-wallet no-op.
        assert det.stats.refused_undecodable_supply == 1
        assert det.stats.untracked_sells == 0
        assert det.stats.signals_emitted == 0

    def test_default_provider_refuses_everything_out_of_the_box(self):
        """A bare InsiderDumpDetector() with no held_supply injected must
        REFUSE every creator/cluster sell -- refuse-by-default is the
        out-of-the-box posture, not an opt-in."""
        det = InsiderDumpDetector()
        det.register_creator(MINT, CREATOR)
        sig = det.on_sell(_obs(wallet=CREATOR, amount=1, slot=1), is_open_position=True)
        assert sig is None
        assert det.stats.refused_undecodable_supply == 1

    def test_zero_held_supply_is_refused_at_registration(self):
        provider = StaticHeldSupplyProvider()
        with pytest.raises(ValueError):
            provider.set_held_supply(MINT, CREATOR, 0)

    def test_negative_held_supply_is_refused_at_registration(self):
        provider = StaticHeldSupplyProvider()
        with pytest.raises(ValueError):
            provider.set_held_supply(MINT, CREATOR, -5)

    def test_refusal_is_logged_not_swallowed(self, caplog):
        det, _ = _detector(held_supply=None)
        with caplog.at_level("WARNING"):
            det.on_sell(_obs(wallet=CREATOR, amount=1, slot=1), is_open_position=True)
        assert any("REFUSING" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# AC: event-time honesty (on-chain slot only, never wall-clock)
# ---------------------------------------------------------------------------


class TestEventTimeHonesty:
    def test_signal_event_time_is_on_chain_only_not_wall_clock(self, monkeypatch):
        # Freeze wall-clock far away from the on-chain values below.
        monkeypatch.setattr(time, "time", lambda: 0.0)
        det, _ = _detector(threshold_bps=1_000, held_supply=1_000_000)
        sig = det.on_sell(
            _obs(
                wallet=CREATOR,
                amount=200_000,
                slot=123_456_789,
                block_time_ms=1_718_700_555_000,
            ),
            is_open_position=True,
        )
        assert sig is not None
        assert sig.event_slot == 123_456_789
        assert sig.block_time_ms == 1_718_700_555_000
        # detected_wall_ms is monitoring-ONLY and reflects the frozen
        # wall-clock (0) -- proving it is a SEPARATE field, never substituted
        # into the on-chain anchors (event_slot / block_time_ms).
        assert sig.detected_wall_ms == 0

    def test_sell_observation_rejects_float_amount(self):
        with pytest.raises(TypeError):
            SellObservation(
                mint=MINT, wallet=CREATOR, token_amount_sold_base=1.5,
                event_slot=1, block_time_ms=1, signature="s",
            )

    def test_sell_observation_rejects_bool_amount(self):
        with pytest.raises(TypeError):
            SellObservation(
                mint=MINT, wallet=CREATOR, token_amount_sold_base=True,
                event_slot=1, block_time_ms=1, signature="s",
            )

    def test_sell_observation_rejects_negative_slot(self):
        with pytest.raises(ValueError):
            SellObservation(
                mint=MINT, wallet=CREATOR, token_amount_sold_base=1,
                event_slot=-1, block_time_ms=1, signature="s",
            )

    def test_signal_rejects_fraction_over_10000(self):
        with pytest.raises(ValueError):
            InsiderDumpSignal(
                mint=MINT, wallet=CREATOR, role=WalletRole.CREATOR,
                fraction_sold_bps=10_001, event_slot=1, block_time_ms=1,
                cumulative_sold_base=1, held_supply_base=1,
            )

    def test_signal_rejects_zero_held_supply(self):
        with pytest.raises(ValueError):
            InsiderDumpSignal(
                mint=MINT, wallet=CREATOR, role=WalletRole.CREATOR,
                fraction_sold_bps=1, event_slot=1, block_time_ms=1,
                cumulative_sold_base=1, held_supply_base=0,
            )


# ---------------------------------------------------------------------------
# AC: idempotent + order-tolerant (dedup on (signature, instruction_index))
# ---------------------------------------------------------------------------


class TestIdempotencyAndOrderTolerance:
    def test_duplicate_sell_same_sig_ix_is_not_double_counted(self):
        det, _ = _detector(threshold_bps=9_000, held_supply=1_000_000)
        obs = _obs(wallet=CREATOR, amount=250_000, slot=10, sig="dup_sig", ix=0)
        sig1 = det.on_sell(obs, is_open_position=True)
        sig2 = det.on_sell(obs, is_open_position=True)  # exact duplicate replay
        assert sig1 is None  # 25% < 90% threshold
        assert sig2 is None  # duplicate -- must NOT add another 25%
        assert det.stats.duplicates_skipped == 1
        assert det._cumulative_sold[(MINT, CREATOR)] == 250_000

    def test_out_of_order_and_duplicated_replay_matches_in_order_once_each(self):
        """Point-in-time law: a replay tolerant of duplication/reordering must
        produce the IDENTICAL final state as clean in-order, once-each delivery."""
        sells = [
            _obs(wallet=CREATOR, amount=100_000, slot=1, sig="a"),
            _obs(wallet=CREATOR, amount=150_000, slot=2, sig="b"),
            _obs(wallet=CREATOR, amount=200_000, slot=3, sig="c"),
        ]

        det_in_order, _ = _detector(threshold_bps=3_000, held_supply=1_000_000)
        results_in_order = [det_in_order.on_sell(o, is_open_position=True) for o in sells]

        # Duplicated + reordered: c, a, a(dup), b, c(dup), b(dup)
        shuffled_with_dupes = [sells[2], sells[0], sells[0], sells[1], sells[2], sells[1]]
        det_shuffled, _ = _detector(threshold_bps=3_000, held_supply=1_000_000)
        for o in shuffled_with_dupes:
            det_shuffled.on_sell(o, is_open_position=True)

        assert (
            det_shuffled._cumulative_sold[(MINT, CREATOR)]
            == det_in_order._cumulative_sold[(MINT, CREATOR)]
            == 450_000
        )
        emitted_in_order = [s for s in results_in_order if s is not None]
        assert emitted_in_order[-1].fraction_sold_bps == 4_500

    def test_not_open_position_is_skipped_and_not_tracked(self):
        det, _ = _detector(threshold_bps=1, held_supply=1_000_000)
        sig = det.on_sell(_obs(wallet=CREATOR, amount=999_000, slot=1), is_open_position=False)
        assert sig is None
        assert det.stats.not_open_position_skipped == 1
        assert (MINT, CREATOR) not in det._cumulative_sold

    def test_reset_mint_clears_cumulative_and_registration(self):
        det, _ = _detector(threshold_bps=9_000, held_supply=1_000_000)
        det.on_sell(_obs(wallet=CREATOR, amount=100_000, slot=1), is_open_position=True)
        assert (MINT, CREATOR) in det._cumulative_sold
        det.reset_mint(MINT)
        assert (MINT, CREATOR) not in det._cumulative_sold
        assert det._role_of(MINT, CREATOR) is None


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------


class TestConfigValidation:
    def test_threshold_bps_rejects_bool(self):
        with pytest.raises(TypeError):
            InsiderDumpConfig(threshold_bps=True)

    def test_threshold_bps_rejects_float(self):
        with pytest.raises(TypeError):
            InsiderDumpConfig(threshold_bps=20.0)

    def test_threshold_bps_rejects_zero(self):
        with pytest.raises(ValueError):
            InsiderDumpConfig(threshold_bps=0)

    def test_threshold_bps_rejects_over_10000(self):
        with pytest.raises(ValueError):
            InsiderDumpConfig(threshold_bps=10_001)

    def test_cluster_size_is_capped(self):
        det, _ = _detector()
        many = {f"Wallet{i}" for i in range(200)}
        det.register_cluster(MINT, many)
        assert len(det._cluster_wallets[MINT]) == 50  # MAX_CLUSTER_WALLETS


# ---------------------------------------------------------------------------
# Adapter: real captured pump.fun sell fixture -> SellObservation
# ---------------------------------------------------------------------------


class TestLaunchEventAdapter:
    def test_adapts_real_pumpfun_sell_fixture(self):
        registry = make_registry()
        router = InstructionRouter(registry)
        tx = make_pumpfun_sell_tx(token_amount=500_000_000, min_sol_output=90_000_000)
        result = router.route(tx, DetectionTransport.GEYSER)
        assert result is not None
        event, kind = result
        assert kind is EventKind.SELL

        obs = sell_observation_from_launch_event(signature=tx.signature, event=event, kind=kind)
        assert obs is not None
        assert obs.mint == "MintPumpFun1111111111111111111111111111111111"
        assert obs.wallet == "SellerWallet111111111111111111111111111111111"
        assert obs.token_amount_sold_base == 500_000_000
        assert obs.event_slot == tx.slot
        assert obs.block_time_ms == tx.block_time_unix_s * 1_000
        assert obs.signature == tx.signature

    def test_adapter_returns_none_for_non_sell_kind(self):
        registry = make_registry()
        router = InstructionRouter(registry)
        tx = make_pumpfun_buy_tx()
        result = router.route(tx, DetectionTransport.GEYSER)
        assert result is not None
        event, kind = result
        assert kind is EventKind.BUY
        obs = sell_observation_from_launch_event(signature=tx.signature, event=event, kind=kind)
        assert obs is None

    def test_end_to_end_real_fixture_through_detector(self):
        """The real decoded sell fixture, fed through the detector end-to-end,
        crosses a low threshold and sets the StateStore flag."""
        registry = make_registry()
        router = InstructionRouter(registry)
        tx = make_pumpfun_sell_tx(token_amount=500_000_000, min_sol_output=90_000_000)
        event, kind = router.route(tx, DetectionTransport.GEYSER)
        obs = sell_observation_from_launch_event(signature=tx.signature, event=event, kind=kind)
        assert obs is not None

        provider = StaticHeldSupplyProvider()
        provider.set_held_supply(obs.mint, obs.wallet, 1_000_000_000)  # baseline = 1e9
        store = InMemoryStateStore()
        det = InsiderDumpDetector(
            config=InsiderDumpConfig(threshold_bps=2_000), held_supply=provider, flag_sink=store
        )
        det.register_creator(obs.mint, obs.wallet)

        sig = det.on_sell(obs, is_open_position=True)
        assert sig is not None
        assert sig.fraction_sold_bps == 5_000  # 500_000_000 / 1_000_000_000
        assert sig.event_slot == tx.slot
        assert store.get_insider_dump_flag(obs.mint) is True
        assert store.get_insider_dump_detail(obs.mint) == (5_000, tx.slot)


# ---------------------------------------------------------------------------
# Bus publisher
# ---------------------------------------------------------------------------


class TestBusPublisher:
    def _signal(self) -> InsiderDumpSignal:
        return InsiderDumpSignal(
            mint=MINT, wallet=CREATOR, role=WalletRole.CREATOR,
            fraction_sold_bps=2_500, event_slot=555, block_time_ms=1_718_700_000_000,
            cumulative_sold_base=250_000, held_supply_base=1_000_000,
        )

    def test_publish_uses_correct_stream_and_maxlen(self):
        redis_mock = AsyncMock()
        redis_mock.xadd = AsyncMock(return_value="1-0")
        publisher = InsiderDumpBusPublisher(redis_mock)
        asyncio.run(publisher.publish(self._signal()))
        args, kwargs = redis_mock.xadd.call_args
        assert args[0] == STREAM_INSIDER_DUMP
        assert kwargs.get("maxlen") == MAXLEN_INSIDER_DUMP
        assert kwargs.get("approximate") is True

    def test_publish_payload_money_fields_are_strings(self):
        captured: dict = {}

        async def fake_xadd(stream, fields, **kwargs):
            captured.update(fields)
            return "1-0"

        redis_mock = AsyncMock()
        redis_mock.xadd.side_effect = fake_xadd
        publisher = InsiderDumpBusPublisher(redis_mock)
        asyncio.run(publisher.publish(self._signal()))
        assert captured["fraction_sold_bps"] == "2500"
        assert captured["event_slot"] == "555"
        assert captured["cumulative_sold_base"] == "250000"
        assert captured["held_supply_base"] == "1000000"
        assert captured["role"] == "creator"
