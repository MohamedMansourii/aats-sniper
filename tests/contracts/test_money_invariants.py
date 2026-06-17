"""Tests: money = int/Decimal NEVER float.

Every monetary field on every production model must be int (lamports/base-units)
or Decimal — never a Python float.  These tests assert:
  1. A float money value is REJECTED by the model validator.
  2. An int money value is ACCEPTED.
  3. Decimal is accepted for Decimal-annotated fields.

Source rule: data-models.md §0.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from aats.contracts.api_schemas import WalletState
from aats.contracts.events import DetectionTransport, EventTime, LaunchEvent, LaunchSource
from aats.contracts.features import FeatureFrame, FeatureProvenance
from aats.contracts.intents import CostStack, EntryIntent
from aats.contracts.positions import Position
from aats.contracts.venue import FillResult

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


def _event_time(slot: int = 1000) -> EventTime:
    return EventTime(slot=slot, block_time_ms=1_700_000_000_000, wall_clock_ms=1_700_000_000_100)


def _valid_launch_event() -> dict:
    return {
        "mint": "So11111111111111111111111111111111111111112",
        "venue_program_id": "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",
        "source": LaunchSource.PUMPFUN,
        "event_time": _event_time(),
        "observation_slot": 999,
        "confirmation_slot": 1000,
        "detection_transport": DetectionTransport.GEYSER,
        "sol_reserve_lamports": 1_000_000_000,  # 1 SOL
        "token_reserve_base": 1_000_000_000_000,
        "token_decimals": 6,
        "initial_holders": 100,
        "competitors": 5,
        "creator_wallet": "Abc123",
        "bundler_cluster_id": None,
        "deploy_template_fingerprint": None,
        "data_staleness_ms": 50,
    }


def _valid_cost_stack() -> CostStack:
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


# ---------------------------------------------------------------------------
# LaunchEvent — float rejection
# ---------------------------------------------------------------------------


class TestLaunchEventMoneyRule:
    def test_int_sol_reserve_accepted(self):
        """Int lamports are accepted (happy path)."""
        data = _valid_launch_event()
        data["sol_reserve_lamports"] = 1_000_000_000
        event = LaunchEvent(**data)
        assert event.sol_reserve_lamports == 1_000_000_000

    def test_float_sol_reserve_rejected(self):
        """Float sol_reserve_lamports MUST be rejected (data-models.md §0)."""
        data = _valid_launch_event()
        data["sol_reserve_lamports"] = 1.0  # float — must fail
        with pytest.raises(Exception) as exc_info:
            LaunchEvent(**data)
        assert "float" in str(exc_info.value).lower() or "sol_reserve" in str(exc_info.value)

    def test_float_token_reserve_rejected(self):
        """Float token_reserve_base MUST be rejected."""
        data = _valid_launch_event()
        data["token_reserve_base"] = 1000000000.5  # float — must fail
        with pytest.raises(Exception) as exc_info:
            LaunchEvent(**data)
        assert "float" in str(exc_info.value).lower() or "token_reserve" in str(exc_info.value)

    def test_int_token_reserve_accepted(self):
        data = _valid_launch_event()
        data["token_reserve_base"] = 1_000_000_000_000
        event = LaunchEvent(**data)
        assert event.token_reserve_base == 1_000_000_000_000


# ---------------------------------------------------------------------------
# FeatureFrame — float rejection on money fields
# ---------------------------------------------------------------------------


def _valid_feature_frame_dict() -> dict:
    from aats.contracts.features import FeatureSourceWindow

    windows = [
        FeatureSourceWindow(
            feature_name="first_k_volume_lamports",
            max_source_slot=1005,
            lineage_dataset="launch_events",
        ),
        FeatureSourceWindow(
            feature_name="lp_depth_lamports",
            max_source_slot=1005,
            lineage_dataset="feature_frames",
        ),
        FeatureSourceWindow(
            feature_name="tip_floor_at_decision_lamports",
            max_source_slot=1000,
            lineage_dataset="launch_events",
        ),
    ]
    provenance = FeatureProvenance(windows=windows, builder_version="v0.1.0")
    return {
        "mint": "So11111111111111111111111111111111111111112",
        "event_time": _event_time(),
        "feature_schema_version": "1.0.0",
        "data_staleness_ms": 50,
        "first_k_slots": 5,
        "first_k_buy_pressure": Decimal("1000000000"),
        "first_k_volume_lamports": 5_000_000_000,
        "first_k_buy_count": 10,
        "first_k_sell_count": 2,
        "first_k_unique_buyers": 8,
        "lp_depth_lamports": 10_000_000_000,
        "holder_count": 200,
        "holder_concentration_top10_bps": 3000,
        "sniper_cluster_score": 0.3,
        "sell_tax_bps": 0,
        "sell_reserve_trajectory": [10_000_000_000, 9_500_000_000],
        "rsi": 55.0,
        "macd": None,
        "bb_width": None,
        "smart_wallets_in": 0,
        "smart_wallet_entry_lag_slots": None,
        "tip_floor_at_decision_lamports": 200_000,
        "tip_contention_bucket": "low",
        "normalization_window": "as_of_event_time",
        "provenance": provenance,
    }


class TestFeatureFrameMoneyRule:
    def test_float_volume_rejected(self):
        data = _valid_feature_frame_dict()
        data["first_k_volume_lamports"] = 5_000_000_000.0  # float — must fail
        with pytest.raises(Exception) as exc_info:
            FeatureFrame(**data)
        err = str(exc_info.value).lower()
        assert "float" in err or "volume" in err

    def test_float_lp_depth_rejected(self):
        data = _valid_feature_frame_dict()
        data["lp_depth_lamports"] = 10_000_000_000.5
        with pytest.raises(Exception) as exc_info:
            FeatureFrame(**data)
        err = str(exc_info.value).lower()
        assert "float" in err or "lp_depth" in err

    def test_float_tip_floor_rejected(self):
        data = _valid_feature_frame_dict()
        data["tip_floor_at_decision_lamports"] = 200000.0
        with pytest.raises(Exception) as exc_info:
            FeatureFrame(**data)
        err = str(exc_info.value).lower()
        assert "float" in err or "tip_floor" in err

    def test_float_buy_pressure_rejected(self):
        """first_k_buy_pressure must be Decimal or string, not float."""
        data = _valid_feature_frame_dict()
        data["first_k_buy_pressure"] = 1.5  # float — must fail
        with pytest.raises(Exception) as exc_info:
            FeatureFrame(**data)
        assert "float" in str(exc_info.value).lower() or "decimal" in str(exc_info.value).lower()

    def test_decimal_buy_pressure_accepted(self):
        data = _valid_feature_frame_dict()
        data["first_k_buy_pressure"] = Decimal("1500000000")
        ff = FeatureFrame(**data)
        assert ff.first_k_buy_pressure == Decimal("1500000000")

    def test_string_buy_pressure_accepted(self):
        """Decimal-as-string is a valid wire format."""
        data = _valid_feature_frame_dict()
        data["first_k_buy_pressure"] = "1500000000"
        ff = FeatureFrame(**data)
        assert ff.first_k_buy_pressure == Decimal("1500000000")

    def test_float_trajectory_element_rejected(self):
        data = _valid_feature_frame_dict()
        data["sell_reserve_trajectory"] = [10_000_000_000, 9_500_000_000.5]  # float element
        with pytest.raises(Exception) as exc_info:
            FeatureFrame(**data)
        assert "float" in str(exc_info.value).lower() or "trajectory" in str(exc_info.value).lower()

    def test_int_fields_accepted(self):
        data = _valid_feature_frame_dict()
        ff = FeatureFrame(**data)
        assert ff.first_k_volume_lamports == 5_000_000_000
        assert ff.lp_depth_lamports == 10_000_000_000
        assert ff.tip_floor_at_decision_lamports == 200_000


# ---------------------------------------------------------------------------
# CostStack — float rejection
# ---------------------------------------------------------------------------


class TestCostStackMoneyRule:
    def test_float_expected_edge_rejected(self):
        with pytest.raises(Exception) as exc_info:
            CostStack(
                expected_edge_bps=300.0,  # float — must fail
                jito_tip_bps=50,
                priority_fee_bps=20,
                entry_slippage_bps=100,
                amm_fee_bps=25,
                exit_slippage_bps=80,
                adverse_selection_bps=150,
                total_cost_bps=275,
            )
        assert "float" in str(exc_info.value).lower() or "int" in str(exc_info.value).lower()

    def test_int_cost_stack_accepted(self):
        cs = _valid_cost_stack()
        assert cs.expected_edge_bps == 300
        assert cs.total_cost_bps == 275

    def test_adverse_selection_floor_enforced(self):
        """adverse_selection_bps must be >= 150 (OQ-007, C-11)."""
        with pytest.raises(Exception) as exc_info:
            CostStack(
                expected_edge_bps=300,
                jito_tip_bps=50,
                priority_fee_bps=20,
                entry_slippage_bps=100,
                amm_fee_bps=25,
                exit_slippage_bps=80,
                adverse_selection_bps=100,  # below floor
                total_cost_bps=275,
            )
        assert "150" in str(exc_info.value) or "adverse_selection" in str(exc_info.value).lower()


# ---------------------------------------------------------------------------
# EntryIntent — float rejection
# ---------------------------------------------------------------------------


class TestEntryIntentMoneyRule:
    def _valid_entry_intent(self) -> dict:
        return {
            "client_intent_id": "ci-test-1000",
            "mint": "So11111111111111111111111111111111111111112",
            "event_time": _event_time(),
            "sol_in_lamports": 100_000_000,
            "slippage_bps": 300,
            "tip_lamports": 200_000,
            "cu_price_microlamports": 100_000,
            "target_slot": 1005,  # >= 1000 + 5
            "cost_stack": _valid_cost_stack(),
            "venue": "pumpswap",
            "wallet_id": "wallet-0",
        }

    def test_float_sol_in_rejected(self):
        data = self._valid_entry_intent()
        data["sol_in_lamports"] = 100_000_000.0  # float
        with pytest.raises(Exception) as exc_info:
            EntryIntent(**data)
        assert "float" in str(exc_info.value).lower() or "sol_in" in str(exc_info.value)

    def test_float_tip_rejected(self):
        data = self._valid_entry_intent()
        data["tip_lamports"] = 200_000.0
        with pytest.raises(Exception) as exc_info:
            EntryIntent(**data)
        assert "float" in str(exc_info.value).lower()

    def test_valid_entry_intent_accepted(self):
        data = self._valid_entry_intent()
        intent = EntryIntent(**data)
        assert intent.sol_in_lamports == 100_000_000
        assert intent.tip_lamports == 200_000


# ---------------------------------------------------------------------------
# FillResult — float rejection
# ---------------------------------------------------------------------------


class TestFillResultMoneyRule:
    def test_float_tokens_out_rejected(self):
        with pytest.raises(Exception) as exc_info:
            FillResult(
                landed=True,
                reason="filled",
                tokens_out=1000000.5,  # float — must fail
                tip_lamports=200_000,
                priority_lamports=1_000,
            )
        assert "float" in str(exc_info.value).lower()

    def test_float_tip_rejected(self):
        with pytest.raises(Exception) as exc_info:
            FillResult(
                landed=True,
                reason="filled",
                tokens_out=1_000_000,
                tip_lamports=200_000.0,  # float
                priority_lamports=1_000,
            )
        assert "float" in str(exc_info.value).lower()

    def test_int_fill_result_accepted(self):
        fr = FillResult(
            landed=True,
            reason="filled",
            tokens_out=1_000_000,
            effective_price_decimal="0.000001",
            entry_slippage_bps=145,
            tip_lamports=200_000,
            priority_lamports=1_000,
        )
        assert fr.tokens_out == 1_000_000
        assert fr.effective_price == Decimal("0.000001")


# ---------------------------------------------------------------------------
# WalletState (API schema) — float rejection
# ---------------------------------------------------------------------------


class TestWalletStateMoneyRule:
    def test_float_balance_rejected(self):
        with pytest.raises(Exception) as exc_info:
            WalletState(
                pubkey="abc",
                balance_lamports=500_000_000.0,  # float
                cap_lamports=500_000_000,
            )
        assert "float" in str(exc_info.value).lower()

    def test_int_wallet_state_accepted(self):
        ws = WalletState(
            pubkey="abc",
            balance_lamports=500_000_000,
            cap_lamports=500_000_000,
        )
        assert ws.balance_lamports == 500_000_000


# ---------------------------------------------------------------------------
# Position — float rejection
# ---------------------------------------------------------------------------


class TestPositionMoneyRule:
    def _valid_position(self) -> dict:
        return {
            "mint": "So11111111111111111111111111111111111111112",
            "token": "TOKEN",
            "surface": "EH-001",
            "fsm_state": "OPEN",
            "entry_slot": 1000,
            "sol_in_lamports": 100_000_000,
            "tokens_out_base": 1_000_000,
            "entry_slippage_bps": 145,
            "exit_config_label": "balanced",
            "exit_mode": "secure",
            "tp_hit": 0,
            "tp_total": 3,
            "trailing_armed": False,
            "hard_stop_price_lamports": 60_000,
            "realized_pnl_net_lamports": None,
            "unrealized_pnl_net_lamports": -5_000,
            "cost_breakdown": _valid_cost_stack(),
            "status": "open",
            "completeness_status": "complete",
        }

    def test_float_sol_in_rejected(self):
        data = self._valid_position()
        data["sol_in_lamports"] = 100_000_000.0
        with pytest.raises(Exception) as exc_info:
            Position(**data)
        assert "float" in str(exc_info.value).lower()

    def test_int_position_accepted(self):
        data = self._valid_position()
        pos = Position(**data)
        assert pos.sol_in_lamports == 100_000_000

    def test_closed_position_requires_realized_pnl(self):
        data = self._valid_position()
        data["status"] = "closed"
        data["realized_pnl_net_lamports"] = None  # missing — must fail
        with pytest.raises(Exception) as exc_info:
            Position(**data)
        assert "realized_pnl" in str(exc_info.value).lower() or "closed" in str(exc_info.value).lower()

    def test_closed_position_with_realized_pnl_accepted(self):
        data = self._valid_position()
        data["status"] = "closed"
        data["realized_pnl_net_lamports"] = -5_000
        pos = Position(**data)
        assert pos.realized_pnl_net_lamports == -5_000
