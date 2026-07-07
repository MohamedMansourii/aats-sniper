"""GATE-B monitor (T-312) — the model-vs-naive-baseline NET-PnL delta (THE headline metric).

WHAT GATE-B IS (EDGE-VERDICT.md §4, §5 K-1)
===========================================
The single honest acceptance control for the model: does the snipe/survivor model's
SELECTED cohort earn MORE net-of-cost PnL PER UNIT RISK than the FROZEN naive-momentum
baseline's selected cohort, on the SAME recorded windows?  If the model cannot beat dumb
momentum net of cost, there is no model (it loses its license to trade — K-1 collapse).

THE HEADLINE NUMBER (and what it is NOT)
========================================
The metric is the DELTA:

    delta = model_net_pnl_per_unit_risk - baseline_net_pnl_per_unit_risk

surfaced to telemetry as `aats_model_vs_baseline_delta_net_pnl_per_sol` (metrics.py,
AC-037).  It is:
  - NET of cost (realized PnL minus the full round-trip cost stack — tips, priority,
    slippage, AMM fees, adverse-selection haircut), per data-models / cost_model.py;
  - per UNIT OF RISK (the frozen baseline's `unit_of_risk` — net-PnL per SOL at risk;
    a "/downside-deviation" variant is also provided), so a model that wins by sizing
    into more risk does not look better than one that wins per unit risk;
  - computed on RECORDED data — a list of TradeOutcome records the clean-room harness
    produces (this monitor is computable at G4 on those records).

It is EXPLICITLY NOT a win-rate.  There is no win-rate field, target, or tuning objective
anywhere in this module (HONESTY CLAUSE, binding).  A test asserts the absence.  We never
report "fraction of trades that won"; we report net-PnL-per-risk and its delta vs baseline.

LOWER-95% BOOTSTRAP BOUND (the pass bar)
========================================
A point estimate is not enough (EDGE-VERDICT §4): GATE-B passes only when the lower 95%
bootstrap bound of the delta is > 0.  We resample trades with replacement (deterministic,
seeded) and report the 2.5th percentile of the bootstrap delta distribution.  `gate_b_pass`
is True iff that lower bound is strictly > 0 AND the recorded sample is large enough.

MINIMUM-SAMPLE GUARD (AUDIT-risktiers — de-risk / fail-closed before GATE-B acts)
================================================================================
A bootstrap lower bound on a HANDFUL of trades is not evidence — on 2 lucky trades the
resample distribution is degenerate and the lower bound can sit comfortably above 0, which
would let GATE-B "pass" on pure noise (and, worse, ACT on that pass — re-scope to model-driven
entries on a statistical accident).  So GATE-B requires a MINIMUM SAMPLE of recorded trades
before it may act on a model-vs-baseline change: below `min_sample` trades, `gate_b_pass` is
forced False regardless of the bootstrap bound.  This guard is DE-RISK ONLY — it can only
WITHHOLD a pass, never manufacture one (it never turns a fail into a pass), so it can only
keep the firm at the conservative baseline, never push it toward more model-driven risk.  The
withheld reason is surfaced (`min_sample_met`) so the operator/telemetry can see WHY a
positive-looking delta did not pass.  Default `min_sample` is conservative (the bootstrap
bound is unreliable far above this); it is tightenable (raise) per AUDIT-risktiers, and the
floor is enforced so a caller cannot pass `min_sample=1` to defeat it.

EFFECTIVE-SAMPLE GUARD (the thin-cohort fragility fix — gate on the DECISION cohort)
===================================================================================
The total-n guard above counts EVERY recorded trade, but the delta is moved ONLY by the
trades where the model's selection differs from the baseline's (`model_selected !=
baseline_selected`): a trade both cohorts take contributes the same per-trade ratio to both
sides and cancels.  So a corpus of thousands of both-take trades wrapped around ~8-20
model-vs-baseline decisions clears a total-n floor of 30 while the whole edge rides a thin,
often-correlated de-risk cohort — the exact fragility that flipped an n=497 "edge" once it
grew to n=4,187.  GATE-B therefore ALSO gates on the EFFECTIVE cohort: below
`effective_min_sample` decisions where model != baseline, `gate_b_pass` is forced False no
matter how large the total sample or how positive the bound.  Like the total-n guard it is
DE-RISK ONLY (withholds a pass, never manufactures one) and tighten-only (a hard floor >= 21
rejects the whole ~8-20 band so a caller cannot license a pass on a handful of decisions).
The withheld reason (`effective_sample_met`, `n_effective`) is surfaced to telemetry.

WHY THIS IS DISTINCT FROM monitor.py (the AUC auto-disable)
==========================================================
`monitor.py` (T-310) is the LIGHTWEIGHT, continuous auto-disable: a ranking-quality (AUC)
gap on every rolling window, fast to compute, used to emit "no signal" the moment edge
decays.  THIS module (T-312) is the HEAVYWEIGHT acceptance metric: the actual net-PnL-per-
risk delta on recorded fills, with a bootstrap bound — the number on the Grafana headline
panel and the operator dashboard.  Both compare the model to the SAME frozen baseline; they
answer two questions (is the ranking still good? / does it still make money net of cost?).

NEVER SIZES, NEVER OVERRIDES (boundaries — non-waivable)
========================================================
This monitor only MEASURES.  It never sizes a position, never widens a stop, never overrides
a hard stop, never builds/signs/lands a transaction, touches no keypair, no RPC.  A failing
GATE-B de-risks (de-scope to baseline / halt model-driven entries — K-1); enforcing that halt
is the OMS/risk lane's job, not this module's.  Real capital stays DISABLED behind DRY-RUN.

MONEY DISCIPLINE (data-models §0, non-negotiable)
=================================================
Every PnL / cost / risk magnitude in a TradeOutcome is an INTEGER (lamports).  No float
money ever enters a record (the contract rejects float).  The PER-UNIT-RISK ratio is the
ONE place a final float is produced (a dimensionless ratio, not money) — computed from exact
integer lamports via Decimal, then cast once for the telemetry gauge / bootstrap.

DETERMINISTIC / INJECTABLE / OFFLINE
====================================
Pure function of the recorded TradeOutcome list + a seed.  No network, no clock, no RNG
except the seeded bootstrap.  The telemetry sink is INJECTABLE (any object with a
`model_vs_baseline_delta` gauge, e.g. AATSMetrics) so the monitor emits offline in tests
without a live Prometheus server.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

import numpy as np

# MINIMUM-SAMPLE guard (AUDIT-risktiers).  GATE-B may not act on a model-vs-baseline
# change below this many recorded trades — the bootstrap bound is not trustworthy on
# a handful of trades.  Conservative default; a caller may TIGHTEN (raise) it but the
# hard FLOOR below prevents lowering it to a value that would defeat the guard.
DEFAULT_MIN_SAMPLE = 30
# Absolute floor: even an explicit caller cannot drop the guard below this — a
# `min_sample` of 1 or 2 would re-open exactly the noise hole this guard closes.
_MIN_SAMPLE_HARD_FLOOR = 10

# EFFECTIVE-SAMPLE guard (the thin-cohort fragility fix).  The headline delta is driven
# ENTIRELY by the trades where the model's selection DIFFERS from the baseline's: a trade
# both cohorts take (model == baseline) contributes the SAME per-trade ratio to both
# cohorts and cancels in the delta.  So the real evidence behind a model-vs-baseline edge
# is the count of DECISIONS where model != baseline (the model's de-risk cohort), NOT the
# total recorded n.  A total-n guard is defeated by dilution: 4,000 both-take trades around
# ~8-20 model!=baseline decisions clears a total-n floor of 30 while the delta rides on a
# handful of correlated draws — exactly the thin cohort that flipped an n=497 "edge" once
# it grew to n=4,187.  GATE-B therefore ALSO requires this many EFFECTIVE (model != baseline)
# decisions before it may pass.  De-risk only: it can withhold a pass, never manufacture one.
DEFAULT_EFFECTIVE_MIN_SAMPLE = 25
# Absolute floor: even an explicit caller cannot drop the effective guard below this.  It is
# set to REJECT the entire ~8-20-decision fragility band (a pass needs strictly more than 20
# effective decisions at the loosest legal setting), so no caller can license capital on a
# delta riding a thin de-risk cohort.
_EFFECTIVE_MIN_SAMPLE_HARD_FLOOR = 21

# ---------------------------------------------------------------------------
# The recorded-data record GATE-B consumes (produced by the clean-room harness)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TradeOutcome:
    """One recorded would-be trade — the unit GATE-B aggregates over.

    Produced by the clean-room harness from RECORDED data (R1 shadow / R2 paper): for each
    candidate it records the model's and the baseline's SELECTION, the realized NET PnL
    (lamports, integer — gross minus the full round-trip cost stack), and the risk
    denominator (SOL-at-risk in lamports, integer).  A coin the model declined contributes
    PnL 0 to the model cohort (a skip is a real, costless outcome); likewise for baseline.

    NO win-rate, NO 'won' boolean — the record carries the SIGNED net PnL only.  Whether a
    trade "won" is never a field; the metric is the summed net-PnL-per-risk, not a count.

    Money fields are INTEGER lamports (data-models §0).  Float is rejected at construction.
    """

    mint: str
    # event-time anchor (slot) — for ordering / windowing only, never a feature
    decision_slot: int
    # the model's binary selection (calibrated prob >= threshold AND gate pass)
    model_selected: bool
    # the frozen naive-momentum baseline's binary selection on the same candidate
    baseline_selected: bool
    # realized NET PnL in lamports IF the cohort took this trade (gross - full cost stack).
    # Integer lamports, signed.  This is the outcome the harness resolved at event_time + H.
    net_pnl_lamports: int
    # SOL-at-risk for this trade, in lamports (the unit-of-risk denominator; integer, > 0).
    sol_at_risk_lamports: int

    def __post_init__(self) -> None:
        for name in ("net_pnl_lamports", "sol_at_risk_lamports"):
            v = getattr(self, name)
            if isinstance(v, float):
                raise ValueError(
                    f"TradeOutcome.{name} must be int (lamports), got float {v!r} "
                    "(money is integer base units, data-models §0)."
                )
            if not isinstance(v, int):
                raise ValueError(f"TradeOutcome.{name} must be int, got {type(v).__name__}")
        if self.sol_at_risk_lamports <= 0:
            raise ValueError(
                f"sol_at_risk_lamports must be > 0 (the risk denominator), got "
                f"{self.sol_at_risk_lamports}"
            )


# ---------------------------------------------------------------------------
# Cohort aggregation: net-PnL-per-unit-risk (NEVER a win-rate)
# ---------------------------------------------------------------------------


class UnitOfRisk(StrEnum):
    """The frozen baseline's unit-of-risk choices (C-4 / walk-forward).  NOT a win-rate."""

    NET_PNL_PER_SOL = "net_pnl_per_sol_at_risk"
    NET_PNL_PER_DOWNSIDE_DEV = "net_pnl_per_downside_deviation"


def _cohort_per_trade_ratios(outcomes: list[TradeOutcome], *, model: bool) -> np.ndarray:
    """Per-trade net-PnL-per-SOL-at-risk for the SELECTED cohort (model or baseline).

    A trade the cohort DECLINED contributes 0.0 (a skip is a costless, real outcome — it
    is correctly counted as zero contribution, never dropped, so a model that wins by
    skipping the bad cohort gets credit for the avoided loss).  The ratio is computed from
    exact integer lamports via Decimal, cast once to a dimensionless float (not money).
    """
    ratios: list[float] = []
    for o in outcomes:
        selected = o.model_selected if model else o.baseline_selected
        if not selected:
            ratios.append(0.0)
            continue
        # exact integer/Decimal ratio, single final float cast (dimensionless, not money)
        r = Decimal(o.net_pnl_lamports) / Decimal(o.sol_at_risk_lamports)
        ratios.append(float(r))
    return np.asarray(ratios, dtype=np.float64)


def _per_unit_risk(ratios: np.ndarray, unit_of_risk: UnitOfRisk) -> float:
    """Aggregate per-trade ratios into the cohort's net-PnL-per-unit-risk scalar.

    NET_PNL_PER_SOL: mean of the per-trade (net PnL / SOL-at-risk) ratios.
    NET_PNL_PER_DOWNSIDE_DEV: mean net-PnL-per-SOL divided by the DOWNSIDE deviation of the
      per-trade ratios (a Sortino-style risk normalization that penalizes losing dispersion,
      NOT a win-rate).  When there is no downside, the divisor is 1.0 (no penalty).
    """
    if ratios.size == 0:
        return 0.0
    mean_r = float(np.mean(ratios))
    if unit_of_risk == UnitOfRisk.NET_PNL_PER_SOL:
        return mean_r
    # downside deviation: RMS of the negative ratios only
    downside = ratios[ratios < 0.0]
    if downside.size == 0:
        return mean_r
    dd = float(np.sqrt(np.mean(downside**2)))
    return mean_r / dd if dd > 0 else mean_r


# ---------------------------------------------------------------------------
# The GATE-B result (the headline delta + bootstrap bound + pass verdict)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GateBResult:
    """The model-vs-baseline net-PnL-per-risk delta — THE headline GATE-B number.

    NO win-rate field.  `delta` is the headline metric emitted to telemetry; `gate_b_pass`
    is True iff the lower 95% bootstrap bound of the delta is strictly > 0.
    """

    n_trades: int
    unit_of_risk: str
    model_net_pnl_per_risk: float
    baseline_net_pnl_per_risk: float
    delta: float  # model - baseline (the headline, surfaced to Grafana)
    lower_95_bound: float  # 2.5th pct of the seeded bootstrap delta distribution
    n_bootstrap: int
    # MINIMUM-SAMPLE guard (AUDIT-risktiers).  `min_sample` is the floor of recorded
    # trades required before GATE-B may act on a change; `min_sample_met` is True iff
    # n_trades >= min_sample.  When False, gate_b_pass is forced False regardless of
    # the bootstrap bound (de-risk: withhold the pass, never manufacture one).
    min_sample: int
    min_sample_met: bool
    # EFFECTIVE-SAMPLE guard (thin-cohort fragility).  `n_effective` is the count of
    # decisions where model_selected != baseline_selected — the ONLY trades that move the
    # delta (both-take trades cancel).  `effective_min_sample` is the floor of effective
    # decisions required; `effective_sample_met` is True iff n_effective >= it.  When False,
    # gate_b_pass is forced False no matter how many total trades or how positive the bound —
    # a delta riding a thin de-risk cohort is not evidence.  De-risk only (never a pass-maker).
    n_effective: int
    effective_min_sample: int
    effective_sample_met: bool
    # True iff lower_95_bound > 0 AND min_sample_met AND effective_sample_met (a positive
    # bound on too few TOTAL or too few EFFECTIVE decisions does NOT pass — that is noise).
    gate_b_pass: bool

    def summary(self) -> str:
        verdict = "PASS" if self.gate_b_pass else "FAIL"
        reasons: list[str] = []
        if not self.min_sample_met:
            reasons.append(f"n<{self.min_sample} min-sample")
        if not self.effective_sample_met:
            reasons.append(
                f"effective_n={self.n_effective}<{self.effective_min_sample} model!=baseline"
            )
        guard = f" [WITHHELD: {'; '.join(reasons)}]" if reasons else ""
        return (
            f"GATE-B [{self.unit_of_risk}] n={self.n_trades} (effective={self.n_effective}): "
            f"model={self.model_net_pnl_per_risk:+.6f} "
            f"baseline={self.baseline_net_pnl_per_risk:+.6f} "
            f"delta={self.delta:+.6f} (lower95={self.lower_95_bound:+.6f}, "
            f"n_boot={self.n_bootstrap}) -> {verdict}{guard}"
        )


def compute_gate_b_delta(
    outcomes: list[TradeOutcome],
    *,
    unit_of_risk: UnitOfRisk = UnitOfRisk.NET_PNL_PER_SOL,
    n_bootstrap: int = 2000,
    seed: int = 20260616,
    min_sample: int = DEFAULT_MIN_SAMPLE,
    effective_min_sample: int = DEFAULT_EFFECTIVE_MIN_SAMPLE,
) -> GateBResult:
    """Compute the GATE-B model-vs-baseline net-PnL-per-unit-risk DELTA on recorded data.

    This is the headline honest metric (EDGE-VERDICT §4 / AC-037), NOT a win-rate.  It is
    computable on the harness's recorded TradeOutcome list at G4.

    Steps:
      1. per-trade net-PnL-per-SOL-at-risk for the MODEL cohort and the BASELINE cohort
         (a declined trade contributes 0.0 — a skip is a real, costless outcome);
      2. aggregate each to the cohort net-PnL-per-unit-risk scalar;
      3. delta = model - baseline (the headline);
      4. seeded paired bootstrap (resample TRADES with replacement) -> the delta sampling
         distribution -> the 2.5th percentile (lower 95% bound);
      5. gate_b_pass iff lower_95_bound > 0 (a point estimate is not enough — §4).

    Args:
        outcomes: recorded would-be trades (from the clean-room harness).
        unit_of_risk: NET_PNL_PER_SOL (default) or NET_PNL_PER_DOWNSIDE_DEV.
        n_bootstrap: bootstrap resample count (deterministic given `seed`).
        seed: RNG seed for the bootstrap (reproducible).
        min_sample: MINIMUM recorded trades before GATE-B may PASS / act on a change
            (AUDIT-risktiers).  Below this, gate_b_pass is forced False (de-risk).
            Clamped UP to a hard floor so it can only be tightened, never defeated.
        effective_min_sample: MINIMUM decisions where model != baseline (the cohort that
            actually moves the delta) before GATE-B may PASS.  Below this, gate_b_pass is
            forced False even if the total sample and the bootstrap bound would pass — the
            thin-cohort guard.  Clamped UP to a hard floor (>= 21) so a caller can only
            tighten it, never lower it to license a delta riding ~8-20 decisions.

    Returns a GateBResult.  Raises ValueError on an empty record set (no data => no metric;
    fail closed, never a fabricated 0).
    """
    if not outcomes:
        raise ValueError(
            "compute_gate_b_delta: empty recorded-trade list. GATE-B is undefined with no "
            "recorded data — fail closed (no metric), never a fabricated delta."
        )

    # MINIMUM-SAMPLE guard (AUDIT-risktiers).  Clamp UP to the hard floor — a caller
    # may RAISE the bar (de-risk) but may never LOWER it below the floor that would
    # re-open the small-sample noise hole.  This is a tighten-only knob.
    total_min_sample = max(int(min_sample), _MIN_SAMPLE_HARD_FLOOR)

    # EFFECTIVE-SAMPLE guard (thin-cohort fragility).  The effective cohort is the set of
    # decisions where the model DIFFERS from the baseline (model_selected != baseline_selected)
    # — the only trades that move the delta; both-take trades contribute identically to both
    # cohorts and cancel.  Clamp the floor UP to its hard floor so it is tighten-only: a delta
    # riding a thin de-risk cohort can never be licensed.
    effective_floor = max(int(effective_min_sample), _EFFECTIVE_MIN_SAMPLE_HARD_FLOOR)
    n_effective = sum(
        1 for o in outcomes if bool(o.model_selected) != bool(o.baseline_selected)
    )

    model_ratios = _cohort_per_trade_ratios(outcomes, model=True)
    base_ratios = _cohort_per_trade_ratios(outcomes, model=False)

    model_per_risk = _per_unit_risk(model_ratios, unit_of_risk)
    base_per_risk = _per_unit_risk(base_ratios, unit_of_risk)
    delta = model_per_risk - base_per_risk

    # Paired bootstrap over TRADES (resample indices, recompute both cohorts on the same
    # resample so the delta's dependence structure is preserved).  Deterministic via seed.
    rng = np.random.default_rng(seed)
    n = len(outcomes)
    boot_deltas = np.empty(n_bootstrap, dtype=np.float64)
    for b in range(n_bootstrap):
        idx = rng.integers(0, n, size=n)
        m = _per_unit_risk(model_ratios[idx], unit_of_risk)
        bl = _per_unit_risk(base_ratios[idx], unit_of_risk)
        boot_deltas[b] = m - bl
    lower_95 = float(np.percentile(boot_deltas, 2.5))

    # SAMPLE guards: GATE-B may only ACT on (pass) a model-vs-baseline change with enough
    # RECORDED trades AND enough EFFECTIVE (model != baseline) decisions.  Below either floor
    # the bootstrap bound is not evidence, so the pass is WITHHELD — de-risk only: this can turn
    # a would-be PASS into a FAIL, never the reverse.
    min_sample_met = n >= total_min_sample
    effective_sample_met = n_effective >= effective_floor
    gate_b_pass = bool(lower_95 > 0.0) and min_sample_met and effective_sample_met

    return GateBResult(
        n_trades=n,
        unit_of_risk=unit_of_risk.value,
        model_net_pnl_per_risk=float(model_per_risk),
        baseline_net_pnl_per_risk=float(base_per_risk),
        delta=float(delta),
        lower_95_bound=lower_95,
        n_bootstrap=int(n_bootstrap),
        min_sample=int(total_min_sample),
        min_sample_met=bool(min_sample_met),
        n_effective=int(n_effective),
        effective_min_sample=int(effective_floor),
        effective_sample_met=bool(effective_sample_met),
        gate_b_pass=gate_b_pass,
    )


# ---------------------------------------------------------------------------
# Telemetry emission (INJECTABLE sink — emits the headline delta gauge)
# ---------------------------------------------------------------------------


class _DeltaGauge(Protocol):
    """The minimal sink shape: an object with a settable `model_vs_baseline_delta` gauge."""

    model_vs_baseline_delta: object  # has .set(float) (prometheus Gauge or a stub)


def emit_gate_b_to_telemetry(result: GateBResult, metrics: _DeltaGauge) -> float:
    """Emit the GATE-B headline DELTA to the telemetry sink's model_vs_baseline_delta gauge.

    `metrics` is INJECTABLE — any object exposing `model_vs_baseline_delta.set(float)`
    (the AATSMetrics gauge in aats.telemetry.metrics, or a test stub).  This keeps the
    monitor offline-testable: no live Prometheus server is required, and the real gauge
    plugs in unchanged in production.

    Emits the `delta` (the headline net-PnL-per-SOL-at-risk delta, AC-037) — NOT a win-rate,
    NOT a point estimate dressed as a pass.  Returns the value emitted (for assertions).
    """
    value = float(result.delta)
    metrics.model_vs_baseline_delta.set(value)
    return value
