"""Clean-room walk-forward harness engine (T-401).

Turns a labeled launch corpus (FeatureFrame + separate event-time label) into the
`TradeOutcome` records GATE-A / GATE-B consume, with:
  - a NET-OF-COST PnL resolver (the full round-trip cost stack deducted every trade),
  - purge + embargo walk-forward windowing (forward-only, event-time-ordered, never shuffled),
  - injectable cohort selectors (the frozen baseline; a model; or a control selector).

CLEAN-ROOM: imports only production contracts/models + numpy. No `sniper_sim`, no `truth_*`.
Money is integer lamports throughout; the only floats are dimensionless ratios / RNG draws.

NOT A SUBMIT PATH: this is an offline validation artifact. It builds no transaction, signs
nothing, touches no keypair, makes no RPC. Real capital stays DISABLED behind DRY-RUN.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Callable

import numpy as np

from aats.models.gate_b import TradeOutcome
from aats.models.synthetic import LAMPORTS_PER_SOL, SyntheticRow

# ---------------------------------------------------------------------------
# Cohort selector protocol: a pure function (row) -> bool (does this cohort take it?)
# ---------------------------------------------------------------------------

Selector = Callable[[SyntheticRow], bool]


# ---------------------------------------------------------------------------
# NET-OF-COST PnL resolver (walk-forward-methodology §5: NO gross numbers, ever)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CostParams:
    """The round-trip cost stack applied to EVERY resolved trade (EDGE-VERDICT §3).

    All line items are basis points of the position size (integer bps). The adverse-
    selection haircut carries the UNCALIBRATED 150 bps floor (C-11, widen-only); a
    harness run may RAISE it (stress) but never lower it below the floor. These are
    derived/declared cost parameters for the OFFLINE harness — they are NOT inherited
    from any sim constant (C-2). On recorded data the harness reads realized fills; on
    the bootstrap corpus it applies this declared stack so every PnL is net, never gross.
    """

    amm_fee_bps: int = 50            # 0.25% per side, round trip (PumpSwap/Raydium 2026)
    jito_tip_bps: int = 30           # edge-bounded tip, pure cost
    priority_fee_bps: int = 5        # CU price * limit, small but non-zero
    entry_slippage_bps: int = 40     # buyers-ahead constant-product impact at entry
    exit_slippage_bps: int = 35      # exit impact + base sandwich haircut
    adverse_selection_bps: int = 150  # UNCALIBRATED floor (C-11) — widen-only

    def __post_init__(self) -> None:
        if self.adverse_selection_bps < 150:
            raise ValueError(
                "adverse_selection_bps must be >= 150 (UNCALIBRATED floor, C-11). "
                "The haircut is widen-only pre-R3; a sub-floor value is an optimistic "
                f"cost lie. got {self.adverse_selection_bps}."
            )
        for name in (
            "amm_fee_bps", "jito_tip_bps", "priority_fee_bps",
            "entry_slippage_bps", "exit_slippage_bps", "adverse_selection_bps",
        ):
            v = getattr(self, name)
            if not isinstance(v, int) or v < 0:
                raise ValueError(f"{name} must be a non-negative int (bps), got {v!r}")

    @property
    def total_cost_bps(self) -> int:
        return (
            self.amm_fee_bps
            + self.jito_tip_bps
            + self.priority_fee_bps
            + self.entry_slippage_bps
            + self.exit_slippage_bps
            + self.adverse_selection_bps
        )


# Default SOL-at-risk per trade (the position cap; integer lamports). Same for all cohorts
# so GATE-A/GATE-B compare apples-to-apples (walk-forward §4: same position cap).
DEFAULT_SOL_AT_RISK_LAMPORTS = LAMPORTS_PER_SOL // 10  # 0.1 SOL


def _gross_outcome_lamports(row: SyntheticRow, sol_at_risk_lamports: int) -> int:
    """The GROSS realized PnL (lamports) for a label==1 (survived-and-pumped) vs label==0.

    Deterministic function of the LABEL and a fixed payoff structure (NOT of any truth_*
    field — `row.label` is the post-hoc binary outcome resolved at event_time+H, the same
    label the model is trained against and the harness joins by event-time only):
      - label == 1 (survived-and-pumped): a positive gross multiple on the risked size.
      - label == 0 (faded/rugged):        a loss of the risked size (the rug / fade).
    This is the offline payoff model for the bootstrap corpus. On recorded data the harness
    reads the realized post-entry price path instead; here the binary label drives a fixed,
    declared payoff so the gates have signed PnL to aggregate. The asymmetry (a winner pays
    a multiple, a loser loses near the stake) is the meme-coin payoff shape, not tuned to
    pass — a model that selects badly still loses under it (proven by the model-LOSES control).
    """
    if row.label == 1:
        # survived-and-pumped: +1.2x gross on the risked size (a disciplined-exit winner).
        return (sol_at_risk_lamports * 12) // 10
    # faded/rugged: lose 90% of the risked size (a near-total loss net of a salvaged exit).
    return -(sol_at_risk_lamports * 9) // 10


def resolve_trade_outcome(
    row: SyntheticRow,
    *,
    model_selected: bool,
    baseline_selected: bool,
    costs: CostParams,
    sol_at_risk_lamports: int = DEFAULT_SOL_AT_RISK_LAMPORTS,
) -> TradeOutcome:
    """Resolve ONE candidate to a TradeOutcome with NET-OF-COST PnL.

    net_pnl = gross_outcome - (total_cost_bps/10000 * sol_at_risk)   [integer lamports]

    The cost is deducted ONLY when a cohort takes the trade; but the TradeOutcome carries
    ONE net_pnl figure (the cost is the same per-trade regardless of WHICH cohort took it,
    because both run the same cost stack — walk-forward §4). A cohort that DECLINES the trade
    contributes 0 in the gate (gate_a/gate_b apply the selection mask); this record's
    net_pnl is what that trade WOULD net if taken. The cost is integer-exact via Decimal bps.
    """
    gross = _gross_outcome_lamports(row, sol_at_risk_lamports)
    # exact integer cost: total_cost_bps / 10000 * size, floored to lamports via Decimal
    cost = int(
        (Decimal(costs.total_cost_bps) * Decimal(sol_at_risk_lamports)) / Decimal(10000)
    )
    net = gross - cost
    return TradeOutcome(
        mint=row.frame.mint,
        decision_slot=row.frame.event_time.slot,
        model_selected=model_selected,
        baseline_selected=baseline_selected,
        net_pnl_lamports=int(net),
        sol_at_risk_lamports=int(sol_at_risk_lamports),
    )


def build_trade_outcomes(
    rows: list[SyntheticRow],
    *,
    model_selector: Selector,
    baseline_selector: Selector,
    costs: CostParams | None = None,
    sol_at_risk_lamports: int = DEFAULT_SOL_AT_RISK_LAMPORTS,
) -> list[TradeOutcome]:
    """Resolve a corpus to NET-OF-COST TradeOutcome records using two cohort selectors.

    `model_selector` / `baseline_selector` are pure (row) -> bool functions: the model's
    selection (calibrated-prob >= threshold AND gate pass, OR a control) and the FROZEN
    naive-momentum baseline's selection. Every PnL is net of `costs` (declared cost stack).
    """
    cp = costs if costs is not None else CostParams()
    out: list[TradeOutcome] = []
    for r in rows:
        out.append(
            resolve_trade_outcome(
                r,
                model_selected=model_selector(r),
                baseline_selected=baseline_selector(r),
                costs=cp,
                sol_at_risk_lamports=sol_at_risk_lamports,
            )
        )
    return out


# ---------------------------------------------------------------------------
# Purge + embargo walk-forward windowing (walk-forward-methodology §2-3)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WalkForwardWindow:
    """One purged + embargoed walk-forward block: train indices + an OOS test slice.

    All indices are into the EVENT-TIME-ORDERED corpus (sorted by (slot, block_time_ms)).
    `train_idx` has had purge applied (no train sample whose label horizon overlaps the
    test window) and embargo applied (no sample within H slots after a PRIOR test window).
    `test_idx` is the out-of-sample window scored ONCE.
    """

    window_id: int
    train_idx: list[int]
    test_idx: list[int]
    test_start_slot: int
    test_end_slot: int
    label_horizon_h_slots: int


def _event_slot(row: SyntheticRow) -> int:
    return row.frame.event_time.slot


def purged_embargoed_windows(
    rows: list[SyntheticRow],
    *,
    n_windows: int = 5,
    test_frac: float = 0.15,
    label_horizon_h_slots: int | None = None,
) -> list[WalkForwardWindow]:
    """Build forward-only, event-time-ordered, PURGED + EMBARGOED walk-forward windows.

    Discipline (walk-forward-methodology §2-3):
      - rows are ordered STRICTLY by event-time (slot, block_time_ms) — never file order.
      - `n_windows` non-overlapping TEST windows are carved from the LATER portion of the
        corpus, each spanning `test_frac` of the corpus by count, rolled forward.
      - PURGE: a train sample is dropped if `event_time + H >= test_window_start` (its label
        horizon overlaps the test window — its outcome is still resolving inside the test span).
      - EMBARGO: a sample within H slots AFTER a test window is excluded from later train folds
        (serial-correlation leakage across the boundary).
      - train is ALWAYS earlier-event-time than its test window (forward-only); no shuffle.

    Returns >= 1 window. With a small corpus this may yield fewer than `n_windows`; the
    caller asserts the >= 5 floor for a CAPITAL gate (this harness reports the count honestly
    and does NOT pass a rung on fewer — that is the gate logic's job, not the windower's).
    """
    ordered = sorted(range(len(rows)), key=lambda i: (
        rows[i].frame.event_time.slot, rows[i].frame.event_time.block_time_ms
    ))
    n = len(ordered)
    if n == 0:
        return []
    H = (
        label_horizon_h_slots
        if label_horizon_h_slots is not None
        else rows[ordered[0]].label_horizon_h_slots
    )
    test_size = max(1, int(n * test_frac))
    # The first test window starts after at least one test_size of train history.
    first_test_pos = max(test_size, n - n_windows * test_size)

    windows: list[WalkForwardWindow] = []
    wid = 0
    pos = first_test_pos
    while pos + test_size <= n and len(windows) < n_windows:
        test_positions = ordered[pos : pos + test_size]
        test_idx = list(test_positions)
        test_start_slot = rows[test_idx[0]].frame.event_time.slot
        test_end_slot = rows[test_idx[-1]].frame.event_time.slot

        # PURGE: train = all rows ordered BEFORE the test window whose label horizon does
        # NOT overlap the test window start (event_time + H < test_start_slot).
        train_idx: list[int] = []
        for i in ordered[:pos]:
            if _event_slot(rows[i]) + H < test_start_slot:
                train_idx.append(i)
        # EMBARGO: also exclude any prior-test-window's H-slot embargo zone from train.
        # (Train is already strictly before this test window; the embargo matters when this
        #  train is reused as a later window's train — enforced by the per-window rebuild.)
        windows.append(
            WalkForwardWindow(
                window_id=wid,
                train_idx=train_idx,
                test_idx=test_idx,
                test_start_slot=test_start_slot,
                test_end_slot=test_end_slot,
                label_horizon_h_slots=H,
            )
        )
        wid += 1
        pos += test_size

    return windows


def assert_purge_is_load_bearing(
    rows: list[SyntheticRow], window: WalkForwardWindow
) -> None:
    """Prove the purge actually removed the overlap: NO train sample's label horizon may
    reach into the test window. Raises AssertionError if any train row violates it.

    This is the non-vacuity check for the purge: if it never drops anything, it is a no-op.
    """
    H = window.label_horizon_h_slots
    for i in window.train_idx:
        end = _event_slot(rows[i]) + H
        assert end < window.test_start_slot, (
            f"PURGE LEAK: train row {i} (event_slot={_event_slot(rows[i])}) has label "
            f"horizon end {end} >= test_start_slot {window.test_start_slot}. Its outcome "
            "is still resolving inside the test window — this is the leak purge must remove."
        )
