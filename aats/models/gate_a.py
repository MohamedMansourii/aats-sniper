"""GATE-A monitor (T-401) — aggregate NET-OF-COST PnL with a lower-95% bootstrap bound.

WHAT GATE-A IS (EDGE-VERDICT.md §4, walk-forward-methodology.md §7.1)
====================================================================
The first of the two binary acceptance controls. GATE-A passes iff the SELECTED cohort's
AGGREGATE net-of-cost PnL is positive AND its lower 95% bootstrap bound is strictly > 0,
over the purged/embargoed walk-forward test windows. A point estimate above zero with a
lower bound below zero is a FAIL (noise, not edge).

    GATE-A pass  iff  lower_95_bound( Σ net_pnl_lamports over SELECTED trades ) > 0

It consumes the SAME recorded `TradeOutcome` record as GATE-B (gate_b.py): the realized
NET PnL in lamports is gross MINUS the full round-trip cost stack (Jito tip + priority/CU
+ entry slippage + AMM fee + exit slippage + adverse-selection haircut). There is no gross
number here — `net_pnl_lamports` is net by contract (the harness deducts the cost stack
when it resolves the trade).

WHO IT MEASURES (model OR baseline OR any cohort)
=================================================
GATE-A is cohort-agnostic: it aggregates the net PnL of whichever cohort's selection mask
you pass (`model=True` aggregates the model's selected trades; `model=False` the baseline's).
A DECLINED trade contributes 0 (a skip is a real, costless outcome — never dropped). This is
the same skip-credit discipline as GATE-B: a cohort that wins by SKIPPING the losing trades
gets credit for the avoided loss.

WHY A SEPARATE GATE FROM GATE-B
===============================
GATE-A asks "does the SELECTED cohort make money net of cost, at all?" (absolute edge).
GATE-B asks "does the MODEL beat dumb momentum net of cost?" (relative edge). A strategy can
pass one and fail the other: a model can beat the baseline while both lose money (GATE-B
pass, GATE-A fail -> still no capital), or make money only because the whole launch
population pumped (GATE-A pass, GATE-B fail -> no model edge). Both must hold (walk-forward §7).

NOT A WIN-RATE (HONESTY CLAUSE, binding)
========================================
There is NO win-rate field, target, or tuning objective anywhere in this module. The metric
is summed net-PnL (and net-PnL-per-unit-risk as a normalized companion). A test asserts the
absence. We never report "fraction of trades that won".

MONEY DISCIPLINE (data-models §0)
=================================
Every PnL / risk magnitude in a TradeOutcome is an INTEGER (lamports). The aggregate sum is
exact integer lamports. The per-unit-risk ratio and the bootstrap statistics are the only
places a final float is produced (a dimensionless quantity / a SOL figure for reporting),
computed from exact integer lamports.

DETERMINISTIC / INJECTABLE / OFFLINE
====================================
Pure function of the recorded TradeOutcome list + a seed. No network, no clock, no RNG
except the seeded bootstrap. Same `seed` reproduces the bound byte-for-byte.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

# GATE-A reuses GATE-B's recorded-data record verbatim — the harness produces ONE
# TradeOutcome list and BOTH gates read it (single source of truth, no divergent schema).
from aats.models.gate_b import TradeOutcome

LAMPORTS_PER_SOL = 1_000_000_000


def _cohort_net_pnl_per_trade(
    outcomes: list[TradeOutcome], *, model: bool
) -> np.ndarray:
    """Per-trade NET PnL (lamports, signed) for the SELECTED cohort.

    A trade the cohort DECLINED contributes 0 (a skip is a real, costless outcome — never
    dropped, so a cohort that wins by skipping the losers gets credit for the avoided loss).
    Returns a float64 array of lamports (large integers held exactly in float64 up to 2^53;
    SOL-scale lamport PnL is far below that, so no precision is lost for the bootstrap).
    """
    vals: list[float] = []
    for o in outcomes:
        selected = o.model_selected if model else o.baseline_selected
        vals.append(float(o.net_pnl_lamports) if selected else 0.0)
    return np.asarray(vals, dtype=np.float64)


def _cohort_risk_per_trade(
    outcomes: list[TradeOutcome], *, model: bool
) -> np.ndarray:
    """Per-trade SOL-at-risk (lamports) for the SELECTED cohort; 0 for a declined trade.

    Used only for the net-PnL-PER-UNIT-RISK companion figure (a declined trade carries no
    risk and no PnL). Never used for the headline net-PnL sum (which is the absolute figure).
    """
    vals: list[float] = []
    for o in outcomes:
        selected = o.model_selected if model else o.baseline_selected
        vals.append(float(o.sol_at_risk_lamports) if selected else 0.0)
    return np.asarray(vals, dtype=np.float64)


@dataclass(frozen=True)
class GateAResult:
    """The aggregate net-of-cost PnL result — THE GATE-A number.

    NO win-rate field. `total_net_pnl_lamports` is the exact integer aggregate (the headline);
    `gate_a_pass` is True iff the lower 95% bootstrap bound of the per-trade net PnL MEAN is
    strictly > 0 (a positive mean over N trades <=> a positive sum; bootstrapping the mean
    gives the standard CI for the aggregate).
    """

    n_trades: int
    cohort: str  # "model" or "baseline"
    total_net_pnl_lamports: int      # exact integer aggregate (headline, net of cost)
    mean_net_pnl_lamports: float     # total / n_trades (declines count as 0)
    net_pnl_per_sol_at_risk: float   # Σ net PnL / Σ SOL-at-risk over SELECTED trades
    lower_95_bound_lamports: float   # 2.5th pct of the seeded bootstrap MEAN distribution
    n_selected: int                  # trades the cohort actually took (non-skip)
    n_bootstrap: int
    gate_a_pass: bool                # True iff lower_95_bound_lamports > 0

    @property
    def total_net_pnl_sol(self) -> float:
        return self.total_net_pnl_lamports / LAMPORTS_PER_SOL

    def summary(self) -> str:
        return (
            f"GATE-A [{self.cohort}] n={self.n_trades} (selected={self.n_selected}): "
            f"total_net={self.total_net_pnl_sol:+.4f} SOL "
            f"mean={self.mean_net_pnl_lamports:+.1f} lamports/trade "
            f"per_sol_risk={self.net_pnl_per_sol_at_risk:+.4f} "
            f"(lower95_mean={self.lower_95_bound_lamports:+.1f} lamports, "
            f"n_boot={self.n_bootstrap}) -> {'PASS' if self.gate_a_pass else 'FAIL'}"
        )


def compute_gate_a(
    outcomes: list[TradeOutcome],
    *,
    model: bool = True,
    n_bootstrap: int = 2000,
    seed: int = 20260616,
) -> GateAResult:
    """Compute GATE-A: aggregate net-of-cost PnL + lower-95% bootstrap bound for a cohort.

    Args:
        outcomes: recorded would-be trades (from the clean-room harness). `net_pnl_lamports`
            is already net of the full cost stack.
        model: True -> aggregate the MODEL's selected cohort; False -> the BASELINE's.
        n_bootstrap: bootstrap resample count (deterministic given `seed`).
        seed: RNG seed for the bootstrap (reproducible).

    Steps:
      1. per-trade net PnL for the selected cohort (declined trade -> 0);
      2. exact integer aggregate (the headline net-of-cost PnL);
      3. seeded bootstrap of the per-trade MEAN -> the 2.5th percentile (lower 95% bound);
      4. gate_a_pass iff lower_95_bound > 0 (a point estimate is not enough — §4).

    Raises ValueError on an empty record set (no data => no metric; fail closed, never a
    fabricated 0). A cohort that selected NOTHING yields total 0 and FAILS (no edge from
    no trades — correctly not a pass).
    """
    if not outcomes:
        raise ValueError(
            "compute_gate_a: empty recorded-trade list. GATE-A is undefined with no "
            "recorded data — fail closed (no metric), never a fabricated PnL."
        )

    per_trade = _cohort_net_pnl_per_trade(outcomes, model=model)
    risk_per_trade = _cohort_risk_per_trade(outcomes, model=model)

    total = int(round(float(np.sum(per_trade))))
    n = len(outcomes)
    n_selected = int(np.count_nonzero(np.asarray(
        [o.model_selected if model else o.baseline_selected for o in outcomes]
    )))
    mean = float(np.mean(per_trade)) if n else 0.0

    sum_risk = float(np.sum(risk_per_trade))
    per_sol = (float(np.sum(per_trade)) / sum_risk) if sum_risk > 0 else 0.0

    # Bootstrap the per-trade MEAN (CI for the aggregate). Resample TRADES with replacement,
    # deterministic via seed. The lower 95% bound is the 2.5th percentile of the mean.
    rng = np.random.default_rng(seed)
    boot_means = np.empty(n_bootstrap, dtype=np.float64)
    for b in range(n_bootstrap):
        idx = rng.integers(0, n, size=n)
        boot_means[b] = float(np.mean(per_trade[idx]))
    lower_95 = float(np.percentile(boot_means, 2.5))

    return GateAResult(
        n_trades=n,
        cohort="model" if model else "baseline",
        total_net_pnl_lamports=total,
        mean_net_pnl_lamports=mean,
        net_pnl_per_sol_at_risk=per_sol,
        lower_95_bound_lamports=lower_95,
        n_selected=n_selected,
        n_bootstrap=int(n_bootstrap),
        gate_a_pass=bool(lower_95 > 0.0),
    )
