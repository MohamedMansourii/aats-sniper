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
    _EFFECTIVE_MIN_SAMPLE_HARD_FLOOR,
    DEFAULT_EFFECTIVE_MIN_SAMPLE,
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


def _model_beats_baseline_fixture(n_win: int = 10, n_lose: int = 10) -> list[TradeOutcome]:
    """The model SELECTS the winners and SKIPS the losers the baseline takes.

    Baseline takes ALL candidates (naive momentum); the model takes only the winners.
    So the baseline eats the losers (negative PnL) the model avoids -> model delta > 0.
    All PnL / risk are integer lamports.  Hand-checkable.

    `n_win` both-take winners (model == baseline, NOT effective — they cancel in the delta)
    and `n_lose` model-declined losers (model != baseline, the EFFECTIVE cohort that moves the
    delta).  Defaults (10, 10) preserve the hand-checked 20-trade fixture; the effective-sample
    tests raise `n_lose` above the GATE-B effective floor so a genuine edge on a sufficient
    de-risk cohort is what is exercised (not the thin-cohort withhold, which has its own test).
    """
    out: list[TradeOutcome] = []
    # winners: both take them, +0.5 SOL each at 0.1 SOL risk (model == baseline -> cancel)
    for i in range(n_win):
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
    # losers: baseline takes them, model SKIPS them, -0.4 SOL each (model != baseline -> effective)
    for i in range(n_lose):
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
        # 40-trade fixture with 30 EFFECTIVE (model!=baseline) decisions: pass min_sample=10
        # (the total-n hard floor) so the MINIMUM-SAMPLE guard is satisfied AND the effective
        # cohort clears the effective floor — we test what this case means: a GENUINE edge on a
        # sufficient de-risk cohort passes.  (Each guard is proven separately below.)
        outs = _model_beats_baseline_fixture(n_win=10, n_lose=30)
        res = compute_gate_b_delta(outs, seed=7, n_bootstrap=2000, min_sample=10)
        assert res.delta > 0
        assert res.lower_95_bound > 0
        assert res.min_sample_met is True
        assert res.effective_sample_met is True  # 30 model!=baseline decisions clear the floor
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
        """A caller may RAISE the total-n bar (de-risk).  A 42-trade genuine edge (28 effective
        decisions, clearing the effective floor) passes at the total-n floor but is WITHHELD when
        min_sample is tightened above the total sample size."""
        outs = _model_beats_baseline_fixture(n_win=14, n_lose=28)  # 42 trades, 28 effective
        passed = compute_gate_b_delta(outs, seed=7, n_bootstrap=2000, min_sample=10)
        assert passed.effective_sample_met is True  # the effective guard is not what is tested here
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


# ---------------------------------------------------------------------------
# 9. EFFECTIVE-SAMPLE GUARD (thin-cohort fragility — gate on model!=baseline, not total-n)
# ---------------------------------------------------------------------------


def _thin_effective_cohort(n_both_take: int, n_effective: int) -> list[TradeOutcome]:
    """A DILUTED corpus: many both-take trades (model == baseline, cancel in the delta) around a
    THIN de-risk cohort (model declines a proven loser) that carries the ENTIRE positive delta.

    This is the exact shape the total-n guard is blind to: the effective cohort is
    `n_effective` while total n is `n_both_take + n_effective`.
    """
    out: list[TradeOutcome] = []
    slot = 0
    for i in range(n_both_take):  # model == baseline (both take a flat winner) -> NOT effective
        out.append(
            TradeOutcome(
                mint=f"both{i}",
                decision_slot=slot,
                model_selected=True,
                baseline_selected=True,
                net_pnl_lamports=SOL // 100,  # small positive, identical to both cohorts
                sol_at_risk_lamports=SOL // 10,
            )
        )
        slot += 1
    for i in range(n_effective):  # model declines a loser the baseline eats -> EFFECTIVE
        out.append(
            TradeOutcome(
                mint=f"eff{i}",
                decision_slot=slot,
                model_selected=False,
                baseline_selected=True,
                net_pnl_lamports=-SOL // 2,  # the loss the model avoids (drives the +delta)
                sol_at_risk_lamports=SOL // 10,
            )
        )
        slot += 1
    return out


class TestEffectiveSampleGuard:
    def test_thin_effective_cohort_is_withheld_despite_large_total_n(self):
        """The fragility case: a HUGE total sample (well past the total-n floor) wrapped around
        ~12 model!=baseline decisions must NOT pass — the delta rides a thin de-risk cohort."""
        outs = _thin_effective_cohort(n_both_take=4000, n_effective=12)
        res = compute_gate_b_delta(outs, seed=3, n_bootstrap=2000)
        assert res.n_trades == 4012
        assert res.n_effective == 12
        assert res.delta > 0  # the point delta is positive...
        assert res.lower_95_bound > 0  # ...and the total-n i.i.d. bound clears zero...
        assert res.min_sample_met is True  # ...and the TOTAL-n guard is satisfied (4012 >> 30)...
        assert res.effective_sample_met is False  # ...but the EFFECTIVE cohort is too thin.
        assert res.gate_b_pass is False  # withheld — no capital on ~12 decisions
        assert "WITHHELD" in res.summary()
        assert "model!=baseline" in res.summary()

    def test_eight_to_twenty_decisions_never_pass(self):
        """The whole ~8-20-decision fragility band must be rejected, even at the LOOSEST legal
        effective floor (a caller cannot license a pass on it)."""
        for n_eff in (8, 12, 16, 20):
            outs = _thin_effective_cohort(n_both_take=200, n_effective=n_eff)
            # Try to defeat the guard with the smallest possible effective floor.
            res = compute_gate_b_delta(outs, seed=1, n_bootstrap=1000, effective_min_sample=1)
            assert res.effective_min_sample >= _EFFECTIVE_MIN_SAMPLE_HARD_FLOOR
            assert res.effective_min_sample > 20  # the band is below the clamped floor
            assert res.gate_b_pass is False, f"a {n_eff}-decision delta must not pass"

    def test_effective_floor_cannot_be_defeated_by_caller(self):
        """A caller cannot pass effective_min_sample=1 to license a thin cohort — it is clamped
        UP to the hard floor (>= 21), exactly like the total-n guard's floor."""
        outs = _thin_effective_cohort(n_both_take=100, n_effective=15)
        res = compute_gate_b_delta(outs, seed=1, n_bootstrap=1000, effective_min_sample=1)
        assert res.effective_min_sample >= _EFFECTIVE_MIN_SAMPLE_HARD_FLOOR
        assert res.effective_sample_met is False
        assert res.gate_b_pass is False

    def test_sufficient_effective_cohort_passes(self):
        """De-risk monotonicity check: the SAME edge on a SUFFICIENT effective cohort (above the
        floor) DOES pass — the guard withholds thin cohorts, it is not a blanket veto."""
        outs = _thin_effective_cohort(n_both_take=100, n_effective=40)
        res = compute_gate_b_delta(outs, seed=1, n_bootstrap=2000)
        assert res.n_effective == 40
        assert res.effective_sample_met is True
        assert res.delta > 0
        assert res.gate_b_pass is True

    def test_effective_guard_never_turns_fail_into_pass(self):
        """The guard is DE-RISK only: a model that LOSES to the baseline fails regardless of how
        large the effective cohort is — the effective floor can only withhold, never manufacture."""
        outs: list[TradeOutcome] = []
        for i in range(60):  # model eats losers the baseline skips -> negative delta, 60 effective
            outs.append(
                TradeOutcome(
                    mint=f"bad{i}",
                    decision_slot=i,
                    model_selected=True,
                    baseline_selected=False,
                    net_pnl_lamports=-SOL // 2,
                    sol_at_risk_lamports=SOL // 10,
                )
            )
        res = compute_gate_b_delta(outs, seed=1, n_bootstrap=2000)
        assert res.n_effective == 60
        assert res.effective_sample_met is True
        assert res.delta < 0
        assert res.gate_b_pass is False

    def test_default_effective_min_sample_rejects_the_fragility_band(self):
        assert DEFAULT_EFFECTIVE_MIN_SAMPLE > 20
        assert _EFFECTIVE_MIN_SAMPLE_HARD_FLOOR > 20

    def test_effective_field_counts_model_ne_baseline(self):
        """`n_effective` is exactly the count of decisions where model_selected != baseline_selected
        (not total-n, not n_model_selected)."""
        outs = _thin_effective_cohort(n_both_take=7, n_effective=5)
        res = compute_gate_b_delta(outs, seed=1, n_bootstrap=500)
        assert res.n_trades == 12
        assert res.n_effective == 5
