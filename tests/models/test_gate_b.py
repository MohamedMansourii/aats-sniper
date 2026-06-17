"""Tests for the GATE-B monitor (T-312) — the headline model-vs-baseline NET-PnL delta.

Self-check criteria (EDGE-VERDICT §4 / §5 K-1; AC-037; HONESTY CLAUSE; data-models §0):
  1. CORRECTNESS: the delta = model_net_pnl_per_risk - baseline_net_pnl_per_risk is computed
     correctly on a deterministic fixture (hand-checked numbers).
  2. HEADLINE, NOT WIN-RATE: there is NO win-rate field / target anywhere; the metric is
     net-PnL-per-unit-risk.  A test asserts the absence.
  3. EMITS TO TELEMETRY: the delta is emitted to an INJECTABLE sink's model_vs_baseline_delta
     gauge (offline stub here; the real AATSMetrics gauge plugs in unchanged).
  4. BOOTSTRAP BOUND: gate_b_pass is True iff the lower 95% bootstrap bound > 0 (a point
     estimate alone never passes); deterministic given the seed.
  5. RECORDED-DATA COMPUTABLE: the monitor is a pure function of a recorded TradeOutcome
     list (what the clean-room harness produces at G4).
  6. MONEY DISCIPLINE: TradeOutcome PnL / risk are INTEGER lamports; float is rejected.
  7. FAIL CLOSED: empty record set -> ValueError (no fabricated delta); a model that loses
     to the baseline -> delta <= 0, gate_b_pass False (de-risk, not a stale edge).
  8. SKIP CREDIT: a declined trade contributes 0 (a skip is a costless real outcome) — a
     model that avoids the losing cohort beats the baseline that took it.

Offline / injectable / deterministic.  No RPC, no LLM, no network, no keypair, no live
Prometheus server.
"""

from __future__ import annotations

import pytest

from aats.models.gate_b import (
    DEFAULT_MIN_SAMPLE,
    GateBResult,
    TradeOutcome,
    UnitOfRisk,
    compute_gate_b_delta,
    emit_gate_b_to_telemetry,
)

SOL = 1_000_000_000  # lamports per SOL


class _GaugeStub:
    """Minimal injectable telemetry sink: one settable model_vs_baseline_delta gauge."""

    class _G:
        def __init__(self) -> None:
            self.value: float | None = None

        def set(self, v: float) -> None:
            self.value = float(v)

    def __init__(self) -> None:
        self.model_vs_baseline_delta = self._G()


# ---------------------------------------------------------------------------
# Fixtures: deterministic recorded TradeOutcome sets
# ---------------------------------------------------------------------------


def _model_beats_baseline_fixture() -> list[TradeOutcome]:
    """The model SELECTS the winners and SKIPS the losers the baseline takes.

    Baseline takes ALL candidates (naive momentum); the model takes only the winners.
    So the baseline eats the losers (negative PnL) the model avoids -> model delta > 0.
    All PnL / risk are integer lamports.  Hand-checkable.
    """
    out: list[TradeOutcome] = []
    # 10 winners: both take them, +0.5 SOL each at 0.1 SOL risk
    for i in range(10):
        out.append(
            TradeOutcome(
                mint=f"win{i}",
                decision_slot=100 + i,
                model_selected=True,
                baseline_selected=True,
                net_pnl_lamports=SOL // 2,  # +0.5 SOL net
                sol_at_risk_lamports=SOL // 10,  # 0.1 SOL at risk
            )
        )
    # 10 losers: baseline takes them, model SKIPS them, -0.4 SOL each at 0.1 SOL risk
    for i in range(10):
        out.append(
            TradeOutcome(
                mint=f"lose{i}",
                decision_slot=200 + i,
                model_selected=False,  # model avoids the catchable losers
                baseline_selected=True,  # naive momentum eats them
                net_pnl_lamports=-2 * SOL // 5,  # -0.4 SOL net
                sol_at_risk_lamports=SOL // 10,
            )
        )
    return out


def _model_loses_fixture() -> list[TradeOutcome]:
    """A degraded model that SELECTS the losers the baseline correctly skips -> delta <= 0.

    This is the de-risk case: the model is no better (worse) than dumb momentum, so GATE-B
    must NOT pass (lower bound <= 0) — a stale edge is the dangerous failure.
    """
    out: list[TradeOutcome] = []
    for i in range(10):
        out.append(
            TradeOutcome(
                mint=f"win{i}",
                decision_slot=100 + i,
                model_selected=False,  # model SKIPS the winners
                baseline_selected=True,
                net_pnl_lamports=SOL // 2,
                sol_at_risk_lamports=SOL // 10,
            )
        )
    for i in range(10):
        out.append(
            TradeOutcome(
                mint=f"lose{i}",
                decision_slot=200 + i,
                model_selected=True,  # model EATS the losers
                baseline_selected=False,
                net_pnl_lamports=-2 * SOL // 5,
                sol_at_risk_lamports=SOL // 10,
            )
        )
    return out


# ---------------------------------------------------------------------------
# 1 + 4 + 5. CORRECTNESS + BOOTSTRAP BOUND + RECORDED-DATA
# ---------------------------------------------------------------------------


class TestDeltaCorrectness:
    def test_delta_is_model_minus_baseline(self):
        outs = _model_beats_baseline_fixture()
        res = compute_gate_b_delta(outs, seed=1, n_bootstrap=500)
        assert isinstance(res, GateBResult)
        # Hand-check: per-trade ratio = net_pnl / sol_at_risk.
        #   winners (model+baseline): +0.5/0.1 = +5.0  (x10)
        #   losers  (baseline only) : -0.4/0.1 = -4.0  (x10); model contributes 0 (skip)
        # model mean over 20 trades  = (10*5 + 10*0) / 20 = +2.5
        # baseline mean over 20 trades = (10*5 + 10*(-4)) / 20 = +0.5
        # delta = 2.5 - 0.5 = +2.0
        assert res.model_net_pnl_per_risk == pytest.approx(2.5, abs=1e-9)
        assert res.baseline_net_pnl_per_risk == pytest.approx(0.5, abs=1e-9)
        assert res.delta == pytest.approx(2.0, abs=1e-9)
        assert res.n_trades == 20

    def test_gate_b_passes_when_lower_bound_positive(self):
        # 20-trade fixture: pass min_sample=10 (the hard floor) so the MINIMUM-SAMPLE
        # guard is satisfied and we test what this case means — a GENUINE edge with a
        # sufficient sample passes.  (The guard itself is proven in TestMinSampleGuard.)
        outs = _model_beats_baseline_fixture()
        res = compute_gate_b_delta(outs, seed=7, n_bootstrap=2000, min_sample=10)
        assert res.delta > 0
        assert res.lower_95_bound > 0
        assert res.min_sample_met is True
        assert res.gate_b_pass is True

    def test_bootstrap_is_deterministic(self):
        outs = _model_beats_baseline_fixture()
        r1 = compute_gate_b_delta(outs, seed=42, n_bootstrap=1000)
        r2 = compute_gate_b_delta(outs, seed=42, n_bootstrap=1000)
        assert r1.lower_95_bound == r2.lower_95_bound
        assert r1.delta == r2.delta

    def test_downside_deviation_unit_of_risk(self):
        outs = _model_beats_baseline_fixture()
        res = compute_gate_b_delta(
            outs, unit_of_risk=UnitOfRisk.NET_PNL_PER_DOWNSIDE_DEV, seed=3, n_bootstrap=500
        )
        # model cohort has NO downside (it skipped all losers) -> divisor 1.0 -> same mean.
        # baseline has downside -> its per-risk is penalized -> model delta still > 0.
        assert res.unit_of_risk == "net_pnl_per_downside_deviation"
        assert res.delta > 0


# ---------------------------------------------------------------------------
# 2 + 8. HEADLINE NOT WIN-RATE + SKIP CREDIT
# ---------------------------------------------------------------------------


class TestHonestyAndSkipCredit:
    def test_no_win_rate_field_on_result(self):
        fields = set(GateBResult.__dataclass_fields__.keys())
        for forbidden in ("win_rate", "winrate", "hit_rate", "wins", "n_wins"):
            assert forbidden not in fields

    def test_no_win_rate_field_on_trade_outcome(self):
        fields = set(TradeOutcome.__dataclass_fields__.keys())
        for forbidden in ("win_rate", "winrate", "won", "is_win", "hit"):
            assert forbidden not in fields

    def test_skip_contributes_zero_not_dropped(self):
        """A declined trade contributes 0 to the cohort (skip = costless real outcome),
        so a model that avoids the losers gets credit for the avoided loss."""
        outs = _model_beats_baseline_fixture()
        res = compute_gate_b_delta(outs, seed=1, n_bootstrap=500)
        # model takes 10 winners, skips 10 losers; its mean over ALL 20 trades is +2.5,
        # which is strictly above the baseline's +0.5 BECAUSE the skips count as 0.
        assert res.model_net_pnl_per_risk > res.baseline_net_pnl_per_risk


# ---------------------------------------------------------------------------
# 3. EMITS TO TELEMETRY (injectable sink)
# ---------------------------------------------------------------------------


class TestTelemetryEmission:
    def test_emits_delta_to_injectable_gauge(self):
        outs = _model_beats_baseline_fixture()
        res = compute_gate_b_delta(outs, seed=1, n_bootstrap=500)
        stub = _GaugeStub()
        emitted = emit_gate_b_to_telemetry(res, stub)
        assert emitted == pytest.approx(res.delta)
        assert stub.model_vs_baseline_delta.value == pytest.approx(res.delta)

    def test_emits_to_real_aats_metrics_gauge(self):
        """The real telemetry gauge is the production sink — emit to it offline (isolated
        registry, no live Prometheus server)."""
        prom = pytest.importorskip("prometheus_client")
        from aats.telemetry.metrics import AATSMetrics

        metrics = AATSMetrics(registry=prom.CollectorRegistry())
        outs = _model_beats_baseline_fixture()
        res = compute_gate_b_delta(outs, seed=1, n_bootstrap=500)
        emit_gate_b_to_telemetry(res, metrics)
        # read the gauge value back from the registry
        samples = list(metrics.model_vs_baseline_delta.collect())[0].samples
        assert samples[0].value == pytest.approx(res.delta)


# ---------------------------------------------------------------------------
# 6. MONEY DISCIPLINE (integer lamports; float rejected)
# ---------------------------------------------------------------------------


class TestMoneyDiscipline:
    def test_float_pnl_rejected(self):
        with pytest.raises(ValueError):
            TradeOutcome(
                mint="x",
                decision_slot=1,
                model_selected=True,
                baseline_selected=True,
                net_pnl_lamports=0.5,  # float money — rejected
                sol_at_risk_lamports=SOL // 10,
            )

    def test_float_risk_rejected(self):
        with pytest.raises(ValueError):
            TradeOutcome(
                mint="x",
                decision_slot=1,
                model_selected=True,
                baseline_selected=True,
                net_pnl_lamports=SOL,
                sol_at_risk_lamports=0.1,  # float money — rejected
            )

    def test_nonpositive_risk_rejected(self):
        with pytest.raises(ValueError):
            TradeOutcome(
                mint="x",
                decision_slot=1,
                model_selected=True,
                baseline_selected=True,
                net_pnl_lamports=SOL,
                sol_at_risk_lamports=0,  # zero risk denominator — rejected
            )


# ---------------------------------------------------------------------------
# 7. FAIL CLOSED (empty -> raise; model loses -> de-risk, NOT a stale edge)
# ---------------------------------------------------------------------------


class TestFailClosed:
    def test_empty_record_set_raises(self):
        with pytest.raises(ValueError):
            compute_gate_b_delta([], seed=1, n_bootstrap=100)

    def test_model_that_loses_does_not_pass_gate_b(self):
        """A degraded model that eats the losers the baseline skips must FAIL GATE-B
        (delta <= 0, lower bound <= 0) — emitting a passing edge here would be the
        dangerous stale-edge failure (K-1)."""
        outs = _model_loses_fixture()
        res = compute_gate_b_delta(outs, seed=5, n_bootstrap=2000)
        assert res.delta < 0
        assert res.lower_95_bound <= 0
        assert res.gate_b_pass is False

    def test_min_sample_guard_blocks_tiny_positive_sample(self):
        """AUDIT-risktiers: a tiny sample with a POSITIVE bootstrap bound must NOT pass
        GATE-B — a 2-trade 'edge' is noise, and acting on it is the danger the guard
        closes.  The guard is DE-RISK: it withholds the pass, never manufactures one."""
        SOL = 1_000_000_000
        outs = [
            TradeOutcome(
                mint="a",
                decision_slot=1,
                model_selected=True,
                baseline_selected=False,
                net_pnl_lamports=SOL // 2,
                sol_at_risk_lamports=SOL // 10,
            ),
            TradeOutcome(
                mint="b",
                decision_slot=2,
                model_selected=True,
                baseline_selected=False,
                net_pnl_lamports=SOL // 2,
                sol_at_risk_lamports=SOL // 10,
            ),
        ]
        res = compute_gate_b_delta(outs, seed=1, n_bootstrap=2000)
        # The bootstrap bound is positive (degenerate, all-win resample)...
        assert res.lower_95_bound > 0
        # ...but the sample is below the minimum, so GATE-B does NOT pass.
        assert res.min_sample_met is False
        assert res.gate_b_pass is False
        assert "WITHHELD" in res.summary()

    def test_min_sample_guard_floor_cannot_be_defeated(self):
        """A caller cannot pass min_sample=1 to defeat the guard — it is clamped UP to
        the hard floor.  Two trades still fail even with min_sample=1."""
        SOL = 1_000_000_000
        outs = [
            TradeOutcome(
                mint=f"x{i}",
                decision_slot=i,
                model_selected=True,
                baseline_selected=False,
                net_pnl_lamports=SOL // 2,
                sol_at_risk_lamports=SOL // 10,
            )
            for i in range(2)
        ]
        res = compute_gate_b_delta(outs, seed=1, n_bootstrap=500, min_sample=1)
        assert res.min_sample >= 10  # clamped up to the hard floor
        assert res.min_sample_met is False
        assert res.gate_b_pass is False

    def test_min_sample_guard_is_tightenable(self):
        """A caller may RAISE the bar (de-risk).  A 20-trade genuine edge passes at the
        floor but is WITHHELD when min_sample is tightened above the sample size."""
        outs = _model_beats_baseline_fixture()  # 20 trades, real edge
        passed = compute_gate_b_delta(outs, seed=7, n_bootstrap=2000, min_sample=10)
        assert passed.gate_b_pass is True
        withheld = compute_gate_b_delta(outs, seed=7, n_bootstrap=2000, min_sample=50)
        assert withheld.lower_95_bound > 0  # the edge is real...
        assert withheld.min_sample_met is False  # ...but the sample is below the raised bar
        assert withheld.gate_b_pass is False  # de-risk: withheld

    def test_min_sample_guard_never_turns_fail_into_pass(self):
        """De-risk monotonicity: the guard can ONLY withhold a pass, never create one.
        A losing model fails on a LARGE sample regardless of the guard."""
        outs = _model_loses_fixture() * 3  # 60 trades, model loses
        res = compute_gate_b_delta(outs, seed=5, n_bootstrap=2000)
        assert res.min_sample_met is True  # plenty of trades
        assert res.delta < 0
        assert res.gate_b_pass is False  # still fails — the guard cannot save a bad model

    def test_default_min_sample_is_conservative(self):
        assert DEFAULT_MIN_SAMPLE >= 30

    def test_shuffled_degraded_window_does_not_pass(self):
        """Degrade the window so model and baseline are indistinguishable noise -> the
        delta's lower bound is not > 0 -> GATE-B does not pass (no stale edge)."""
        import random

        rng = random.Random(0)
        outs: list[TradeOutcome] = []
        for i in range(200):
            pnl = rng.choice([SOL // 2, -2 * SOL // 5])
            sel = rng.random() < 0.5
            outs.append(
                TradeOutcome(
                    mint=f"m{i}",
                    decision_slot=i,
                    model_selected=sel,
                    baseline_selected=rng.random() < 0.5,
                    net_pnl_lamports=pnl,
                    sol_at_risk_lamports=SOL // 10,
                )
            )
        res = compute_gate_b_delta(outs, seed=9, n_bootstrap=2000)
        # noise: the lower 95% bound straddles / is below 0 -> not a pass
        assert res.gate_b_pass is False
