"""Tests: Control-plane API Pydantic schemas (api-contracts.md FROZEN).

Verifies:
  1. AgentMode has EXACTLY the 4 canonical values (api-contracts §2).
  2. No win_rate field on any schema (HONESTY CLAUSE, AC-037).
  3. Money fields are int/decimal-string, NEVER float.
  4. SnipeEvent has snake_case wire fields matching api-contracts §6.
  5. All major response schemas construct without error.
"""

from __future__ import annotations

import pytest

import json

from aats.contracts.api_schemas import (
    AgentMode,
    AgentState,
    BreakerResetResponse,
    CostBreakdown,
    ErrorEnvelope,
    FlattenResponse,
    InfraTier,
    KillResponse,
    LatencyBudget,
    LatencyHop,
    MetricsSnapshot,
    ModelVsBaseline,
    ModeRequest,
    ModuleHealth,
    PositionRecord,
    SnipeEvent,
    WalletState,
)

# ---------------------------------------------------------------------------
# AgentMode: canonical 4-value enum (api-contracts §2)
# ---------------------------------------------------------------------------


class TestAgentModeEnum:
    _CANONICAL_VALUES = frozenset({"SHADOW", "PAPER", "LIVE_DRY_RUN", "LIVE"})

    def test_has_exactly_four_members(self):
        assert len(list(AgentMode)) == 4, (
            f"AgentMode must have exactly 4 members (api-contracts §2). "
            f"Got: {[m.name for m in AgentMode]}"
        )

    def test_has_canonical_values(self):
        actual = frozenset(m.value for m in AgentMode)
        assert actual == self._CANONICAL_VALUES, (
            f"AgentMode values must be {self._CANONICAL_VALUES}. Got: {actual}"
        )

    def test_shadow_is_default_mode(self):
        """SHADOW is the startup mode (FR-004)."""
        assert AgentMode.SHADOW in AgentMode

    def test_live_requires_explicit_auth(self):
        """LIVE is in the enum but unreachable without DRY_RUN_ENABLED=false + CEO auth."""
        assert AgentMode.LIVE in AgentMode

    def test_no_old_dry_run_value(self):
        """The old 'dry-run' (lowercase hyphen) value is NOT in the canonical enum."""
        values = [m.value for m in AgentMode]
        assert "dry-run" not in values
        assert "live" not in values  # must be "LIVE" uppercase


# ---------------------------------------------------------------------------
# No win_rate field on any schema (HONESTY CLAUSE, AC-037)
# ---------------------------------------------------------------------------


class TestNoWinRateField:
    _SCHEMAS = [
        AgentState,
        MetricsSnapshot,
        ModelVsBaseline,
        SnipeEvent,
        PositionRecord,
        WalletState,
        LatencyBudget,
        KillResponse,
        FlattenResponse,
        BreakerResetResponse,
        ErrorEnvelope,
        ModuleHealth,
    ]

    @pytest.mark.parametrize("schema_cls", _SCHEMAS)
    def test_no_win_rate_field(self, schema_cls):
        """No API schema may have a win_rate field (HONESTY CLAUSE, AC-037)."""
        assert "win_rate" not in schema_cls.model_fields, (
            f"{schema_cls.__name__} has a 'win_rate' field — "
            "this violates the HONESTY CLAUSE (AC-037).  "
            "The HONESTY CLAUSE forbids win-rate metrics anywhere in the system."
        )

    @pytest.mark.parametrize("schema_cls", _SCHEMAS)
    def test_no_win_rate_pct_field(self, schema_cls):
        assert "win_rate_pct" not in schema_cls.model_fields


# ---------------------------------------------------------------------------
# Money fields: integer lamports / decimal-string, NEVER float
# ---------------------------------------------------------------------------


class TestAPISchemaMoneyFields:
    def test_wallet_state_rejects_float_balance(self):
        with pytest.raises(Exception) as exc_info:
            WalletState(
                pubkey="abc",
                balance_lamports=500_000_000.0,  # float — must fail
                cap_lamports=500_000_000,
            )
        assert "float" in str(exc_info.value).lower()

    def test_wallet_state_int_accepted(self):
        ws = WalletState(
            pubkey="abc",
            balance_lamports=500_000_000,
            cap_lamports=500_000_000,
        )
        assert ws.balance_lamports == 500_000_000

    def test_metrics_pnl_is_decimal_string(self):
        """net_pnl_sol must be a decimal string, not a float (api-contracts §4)."""
        m = MetricsSnapshot(
            net_pnl_sol="-0.0123",
            gross_pnl_sol="0.0044",
            land_rate_pct=38.2,
            median_slot_delay=7,
            rug_avoidance_pct=61.0,
            tip_efficiency=0.42,
            model_vs_baseline=ModelVsBaseline(
                delta_net_pnl_per_sol_at_risk="0.013",
                lower_95_bound="0.004",
                n_test_windows=5,
                gate_b_pass=True,
            ),
            gate_a={"net_pnl_sol": "0.021", "lower_95_bound": "0.006", "gate_a_pass": True},
            open_positions=2,
            daily_pnl_sol="-0.0123",
            daily_loss_limit_sol="-0.30",
            breaker_tripped=False,
        )
        # net_pnl_sol is a string (decimal), not a float
        assert isinstance(m.net_pnl_sol, str)
        assert m.net_pnl_sol == "-0.0123"

    def test_position_record_float_sol_in_rejected(self):
        with pytest.raises(Exception) as exc_info:
            PositionRecord(
                mint="abc",
                token="TOKEN",
                entry_slot=1000,
                sol_in_lamports=100_000_000.0,  # float — must fail
                unrealized_pnl_net_lamports=-5_000,
                entry_slip_bps=145,
                tp_hit=0,
                tp_total=3,
                trailing_armed=False,
                hard_stop_pct=-40.0,
                exit_mode="secure",
                fsm_state="OPEN",
                age_sec=42,
                status="open",
                realized_pnl_net_lamports=None,
                surface="EH-001",
                cost_breakdown=CostBreakdown(
                    tip_lamports=0,
                    priority_lamports=0,
                    entry_slippage_bps=145,
                    amm_fee_bps=50,
                    exit_slippage_bps=0,
                    adverse_selection_bps=150,
                ),
            )
        assert "float" in str(exc_info.value).lower()


# ---------------------------------------------------------------------------
# SnipeEvent: snake_case wire fields
# ---------------------------------------------------------------------------


class TestSnipeEventSchema:
    def _valid_snipe_event(self) -> dict:
        return {
            "id": "evt-001",
            "event_time": {"slot": 12345, "wall_clock_ms": 1718500000000},
            "observation_slot": 12345,
            "confirmation_slot": 12346,
            "detection_transport": "geyser",
            "ts": 1718500000000,
            "slot": 12345,
            "token": "TOKEN",
            "mint": "So11111111111111111111111111111111111111112",
            "source": "pump.fun",
            "gate_passed": True,
            "gate_reasons": ["passed"],
            "red_flags": [],
            "model_p": 0.31,
            "model_uncertainty": 0.12,
            "action": "sniped",
            "veto_source": None,
            "slot_delay": 7,
            "smart_wallets": 0,
            "tip_contention_bucket": "low",
            "cost_gate": {"expected_edge_bps": 80, "total_cost_bps": 75, "passed": True},
            "pnl_pct": None,
        }

    def test_snipe_event_snake_case_fields(self):
        """SnipeEvent uses snake_case wire format (api-contracts §6)."""
        evt = SnipeEvent(**self._valid_snipe_event())
        # Verify snake_case fields exist
        assert hasattr(evt, "gate_passed")
        assert hasattr(evt, "gate_reasons")
        assert hasattr(evt, "model_p")
        assert hasattr(evt, "slot_delay")
        assert hasattr(evt, "smart_wallets")
        assert hasattr(evt, "pnl_pct")
        # NOT camelCase
        assert not hasattr(evt, "gatePassed")
        assert not hasattr(evt, "gateReasons")

    def test_snipe_event_no_win_rate(self):
        assert "win_rate" not in SnipeEvent.model_fields

    def test_valid_snipe_event_constructs(self):
        evt = SnipeEvent(**self._valid_snipe_event())
        assert evt.action == "sniped"
        assert evt.gate_passed is True


# ---------------------------------------------------------------------------
# ModeRequest: only valid AgentMode values accepted
# ---------------------------------------------------------------------------


class TestModeRequest:
    def test_valid_mode_accepted(self):
        req = ModeRequest(mode=AgentMode.PAPER)
        assert req.mode == AgentMode.PAPER

    def test_invalid_mode_rejected(self):
        with pytest.raises((ValueError, Exception)):  # noqa: B017
            ModeRequest(mode="dry-run")  # old lowercase value — must fail

    def test_live_mode_accepted_in_schema(self):
        """LIVE is a valid schema value; the server enforces the auth gate separately."""
        req = ModeRequest(mode=AgentMode.LIVE)
        assert req.mode == AgentMode.LIVE


# ---------------------------------------------------------------------------
# ModuleHealth
# ---------------------------------------------------------------------------


class TestModuleHealth:
    def test_module_health_constructs(self):
        mh = ModuleHealth(
            name="geyser_ingest",
            status="ok",
            staleness_ms=50,
            last_heartbeat_ms=1_700_000_000_000,
        )
        assert mh.status == "ok"

    def test_stale_module_health(self):
        mh = ModuleHealth(
            name="geyser_ingest",
            status="degraded",
            staleness_ms=1500,  # > 1200 ms → STALE (FR-057, AC-051)
            last_heartbeat_ms=1_700_000_000_000,
        )
        assert mh.status == "degraded"
        assert mh.staleness_ms > 1200


# ---------------------------------------------------------------------------
# T-199b: LatencyHop wire-key test (api-contracts.md §4 /api/latency)
# ---------------------------------------------------------------------------


class TestLatencyHopWireKey:
    """T-199b: LatencyHop.cls must serialise to wire key 'class' (FROZEN contract).

    api-contracts.md §4 /api/latency specifies the JSON key as "class":
      { "name": "ingress_detect", "ms": 60, "budget_ms": 70,
        "class": "internal_compute" }

    Python cannot use 'class' as an identifier, so the field is named 'cls'
    with alias="class" and serialization_alias="class".  populate_by_name=True
    allows construction from Python code with cls= as well as from parsed JSON.
    """

    def _make_hop(self, cls_value: str = "internal_compute") -> LatencyHop:
        return LatencyHop(name="ingress_detect", ms=60, budget_ms=70, cls=cls_value)

    def test_model_dump_json_emits_class_key(self):
        """model_dump_json() MUST emit 'class', not 'cls' (T-199b wire contract)."""
        hop = self._make_hop("internal_compute")
        wire = json.loads(hop.model_dump_json(by_alias=True))
        assert "class" in wire, (
            f"LatencyHop.model_dump_json(by_alias=True) must emit key 'class' "
            f"(api-contracts.md §4 /api/latency). Got keys: {list(wire.keys())}"
        )
        assert wire["class"] == "internal_compute"
        assert "cls" not in wire, (
            "Wire JSON must NOT contain 'cls' — the wire key is 'class' (frozen contract)."
        )

    def test_class_key_value_correct(self):
        """Wire value of 'class' is preserved correctly through serialisation."""
        for cls_value in ("internal_compute", "submission", ""):
            hop = LatencyHop(name="hop", ms=10, budget_ms=20, cls=cls_value)
            wire = json.loads(hop.model_dump_json(by_alias=True))
            assert wire["class"] == cls_value, (
                f"Expected wire['class'] == {cls_value!r}, got {wire['class']!r}"
            )

    def test_construct_with_alias_class_key(self):
        """Construction from wire JSON (using 'class' key) succeeds (populate_by_name=True)."""
        wire_data = {"name": "block_engine_rtt", "ms": 55, "budget_ms": 60,
                     "class": "submission"}
        hop = LatencyHop.model_validate(wire_data)
        assert hop.cls == "submission"
        assert hop.name == "block_engine_rtt"

    def test_construct_with_python_name_cls(self):
        """Construction from Python code (using 'cls=' kwarg) succeeds (populate_by_name=True)."""
        hop = LatencyHop(name="leader_land", ms=50, budget_ms=400,
                         cls="submission", p99_ms=450)
        assert hop.cls == "submission"
        wire = json.loads(hop.model_dump_json(by_alias=True))
        assert wire["class"] == "submission"
        assert wire["p99_ms"] == 450

    def test_latency_budget_hops_emit_class_key(self):
        """Full LatencyBudget serialisation: each hop emits 'class' key."""
        budget = LatencyBudget(
            hops=[
                LatencyHop(name="ingress_detect", ms=60, budget_ms=70,
                           cls="internal_compute"),
                LatencyHop(name="block_engine_rtt", ms=55, budget_ms=60,
                           cls="submission"),
                LatencyHop(name="leader_land", ms=50, budget_ms=400,
                           cls="submission", p99_ms=450,
                           note="+1 slot under contention"),
            ],
            internal_ms=67,
            block_engine_rtt_ms=55,
            slot_floor_ms=400,
            posture="DETECTION-COMPETITIVE, SUBMISSION-DISADVANTAGED",
            tiers=[
                InfraTier(name="dedicated_geyser", land_rate=0.38,
                          median_slot_delay=7, entry_slip_pct=1.45, active=True),
            ],
        )
        wire = json.loads(budget.model_dump_json(by_alias=True))
        for i, hop in enumerate(wire["hops"]):
            assert "class" in hop, (
                f"hops[{i}] missing 'class' key in wire JSON. "
                f"Got keys: {list(hop.keys())}"
            )
            assert "cls" not in hop, (
                f"hops[{i}] must not emit 'cls' in wire JSON (alias='class' must be used)."
            )

    def test_no_cls_key_in_wire_json(self):
        """Regression: 'cls' must NEVER appear as a wire key (it was the pre-fix bug)."""
        hop = self._make_hop("submission")
        wire_str = hop.model_dump_json(by_alias=True)
        assert '"cls"' not in wire_str, (
            f"Wire JSON must not contain '\"cls\"'. Pre-fix bug: cls: str = '' with no alias "
            f"caused wire to emit 'cls' instead of 'class'. Got: {wire_str}"
        )
        assert '"class"' in wire_str, (
            f"Wire JSON must contain '\"class\"'. Got: {wire_str}"
        )
