"""T-324 — Cost-aware entry gate: REJECT when edge <= total round-trip cost.

THE DOMINANT FAILURE MODE
-------------------------
"Absence of real edge net of costs and adverse selection" is the single most
common way a meme-coin sniper bleeds out: a model says "go", the trade fills,
and the round-trip cost (Jito tip + priority/CU fee + entry slippage + AMM fee +
exit slippage + adverse-selection haircut) quietly exceeds the edge.  This gate
is where we kill that trade BEFORE it is placed.

THE RULE (FR-027, AC-013 — non-waivable)
----------------------------------------
    REJECT the entry iff   expected_edge_bps <= total_cost_bps
i.e. an entry passes ONLY if the expected edge STRICTLY exceeds the full
round-trip cost.  Equality is a REJECT (no free trades; ties go to "no trade").
This mirrors the EntryIntent contract's own `_cost_gate` validator
(contracts/intents.py) — the gate here is the pre-Intent check on the SNIPE path
so we never even attempt to construct a cost-failing Intent.

THE 150bps UNCALIBRATED HAIRCUT (OQ-007, C-11)
----------------------------------------------
Pre-calibration, the adverse-selection line item has a HARD FLOOR of 150 bps,
labeled UNCALIBRATED.  Until the calibrated-haircut sub-gate (C-11, T-401)
measures the real figure from R1 fills, the cost stack MUST include at least 150
bps of adverse selection.  The floor is WIDEN-ONLY pre-R3 (a config may raise it,
never lower it).  This gate refuses any cost stack whose adverse_selection_bps is
below the configured floor — a sub-floor haircut is an optimistic cost lie.

FAIL CLOSED
-----------
A missing/negative/incoherent cost line, or a total that does not reconcile to
the sum of its parts, is a REJECT — never a pass.  An entry that cannot be proven
edge-positive net of cost does not trade.

POINT-IN-TIME
-------------
The gate is a pure function of the CostStack values, which are stamped at
event_time on the SNIPE path.  No wall-clock, no future data.  Same code live and
in backtest.

MONEY
-----
Every line item is integer basis points.  NEVER float.  The CostStack contract
already rejects float bps; this gate adds the edge-vs-cost decision and the
floor + reconciliation checks.
"""

from __future__ import annotations

from dataclasses import dataclass

from aats.contracts.intents import CostStack
from aats.contracts.risk import RiskConfig

# The UNCALIBRATED adverse-selection floor (OQ-007, C-11).  The CostStack
# contract enforces >= 150 already; we re-assert against the (widen-only) config
# value so a raised floor is honored here too.
_DEFAULT_HAIRCUT_FLOOR_BPS = 150


class CostGateRejection(str):
    """Stable reject-reason codes (dashboard-facing / telemetry)."""


# Reason codes (kept as plain string constants for a stable telemetry contract).
REASON_EDGE_BELOW_COST = "edge_below_cost"  # expected_edge_bps <= total_cost_bps
REASON_HAIRCUT_BELOW_FLOOR = "haircut_below_floor"  # adverse_selection_bps < floor
REASON_TOTAL_MISMATCH = "total_cost_mismatch"  # total != sum of parts
REASON_NEGATIVE_LINE = "negative_cost_line"  # a cost line is negative
REASON_NEGATIVE_EDGE = "non_positive_edge"  # expected_edge_bps <= 0


@dataclass(frozen=True)
class CostGateDecision:
    """The cost gate's verdict — auditable, integer bps throughout.

    `passed` is True ONLY when the edge STRICTLY exceeds total cost AND every
    fail-closed check holds.  `margin_bps` is `expected_edge_bps - total_cost_bps`
    (positive iff passed on the edge test); it is surfaced for telemetry so an
    operator can see how thin a passing trade's net edge is.
    """

    passed: bool
    reason: str | None  # None iff passed; otherwise a REASON_* code
    expected_edge_bps: int
    total_cost_bps: int
    margin_bps: int  # expected_edge_bps - total_cost_bps (signed)


class CostAwareEntryGate:
    """The pre-Intent cost gate.  REJECT iff expected_edge_bps <= total_cost_bps.

    Holds the RiskConfig so the adverse-selection floor honors the (widen-only)
    `pre_calibration_haircut_bps`.  Exposes ONE method, `evaluate()`, a pure
    function of the CostStack.  It can only REJECT or PASS — it never sizes,
    widens, or constructs anything.  A gate cannot add risk.
    """

    def __init__(self, config: RiskConfig | None = None) -> None:
        self._config = config
        # The effective floor is the MAX of the contract floor and the config's
        # (widen-only) pre_calibration_haircut_bps — a raised floor is honored,
        # a lowered one is impossible (RiskConfig refuses < 150).
        cfg_floor = (
            config.pre_calibration_haircut_bps if config is not None else _DEFAULT_HAIRCUT_FLOOR_BPS
        )
        self._haircut_floor_bps = max(_DEFAULT_HAIRCUT_FLOOR_BPS, int(cfg_floor))

    @property
    def haircut_floor_bps(self) -> int:
        return self._haircut_floor_bps

    def _sum_of_parts(self, cs: CostStack) -> int:
        """The reconciled total: sum of every cost line (integer bps)."""
        return (
            cs.jito_tip_bps
            + cs.priority_fee_bps
            + cs.entry_slippage_bps
            + cs.amm_fee_bps
            + cs.exit_slippage_bps
            + cs.adverse_selection_bps
        )

    def evaluate(self, cost_stack: CostStack) -> CostGateDecision:
        """Decide PASS/REJECT for a candidate's CostStack (pure, fail-closed).

        Order of checks (all fail-closed — first failure REJECTS):
          1. No cost line is negative (an optimistic negative cost is a lie).
          2. The adverse-selection haircut is at/above the UNCALIBRATED floor.
          3. The declared total reconciles to the sum of parts (no understated
             total slipping a sub-cost trade through).
          4. The expected edge is strictly positive.
          5. THE RULE: expected_edge_bps STRICTLY exceeds total_cost_bps.
        """
        cs = cost_stack
        edge = cs.expected_edge_bps
        total = cs.total_cost_bps
        margin = edge - total

        # 1. No negative cost line (fail closed on an optimistic negative cost).
        lines = (
            cs.jito_tip_bps,
            cs.priority_fee_bps,
            cs.entry_slippage_bps,
            cs.amm_fee_bps,
            cs.exit_slippage_bps,
            cs.adverse_selection_bps,
        )
        if any(line < 0 for line in lines) or total < 0:
            return CostGateDecision(
                passed=False,
                reason=REASON_NEGATIVE_LINE,
                expected_edge_bps=edge,
                total_cost_bps=total,
                margin_bps=margin,
            )

        # 2. Adverse-selection haircut floor (OQ-007, C-11) — widen-only.
        if cs.adverse_selection_bps < self._haircut_floor_bps:
            return CostGateDecision(
                passed=False,
                reason=REASON_HAIRCUT_BELOW_FLOOR,
                expected_edge_bps=edge,
                total_cost_bps=total,
                margin_bps=margin,
            )

        # 3. Total must reconcile to the sum of its parts.  An understated total
        #    that hides a real cost is a sub-cost trade in disguise — REJECT.
        if total != self._sum_of_parts(cs):
            return CostGateDecision(
                passed=False,
                reason=REASON_TOTAL_MISMATCH,
                expected_edge_bps=edge,
                total_cost_bps=total,
                margin_bps=margin,
            )

        # 4. Edge must be strictly positive (a zero/negative edge never trades).
        if edge <= 0:
            return CostGateDecision(
                passed=False,
                reason=REASON_NEGATIVE_EDGE,
                expected_edge_bps=edge,
                total_cost_bps=total,
                margin_bps=margin,
            )

        # 5. THE RULE (FR-027, AC-013): edge must STRICTLY exceed total cost.
        #    Equality is a REJECT — ties go to "no trade".
        if edge <= total:
            return CostGateDecision(
                passed=False,
                reason=REASON_EDGE_BELOW_COST,
                expected_edge_bps=edge,
                total_cost_bps=total,
                margin_bps=margin,
            )

        return CostGateDecision(
            passed=True,
            reason=None,
            expected_edge_bps=edge,
            total_cost_bps=total,
            margin_bps=margin,
        )

    def allows_entry(self, cost_stack: CostStack) -> bool:
        """Convenience boolean for the SNIPE path: True iff the entry may proceed."""
        return self.evaluate(cost_stack).passed
