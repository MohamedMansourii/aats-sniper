"""M4 Guardrails — MEV protection and latency instrumentation.

Implemented by: mev-latency-engineer (T-330, T-331).

Responsibilities (T-330 delivered here):
  - LatencyTracer: per-hop instrumentation, C-1 three-class split
    (INTERNAL compute vs SUBMISSION block-engine RTT vs LEADER_LAND staked
    lane), p50/p95/p99 ledger emitted to telemetry (latency-budget.md, FR-053).
  - TipController: edge-bounded dynamic tip pricing — reads the LIVE tip_stream
    (getTipAccounts/percentiles) OFF the hot path, bounds the tip at
    <= 0.30x expected edge, and returns DO-NOT-SUBMIT when priced out of the
    race.  A hardcoded tip is a banned anti-pattern (AC-014).
  - JitoBundleSubmitter: atomic buy-with-revert — the buy + min-out + tip ride
    in ONE bundle; a reverting buy lands NOTHING (no orphan position, FR-040).
    DRY-RUN by default — no code path reaches the block engine (FR-039).

  T-331 (MEV modes: fast vs secure, private split exits, sandwich avoidance)
  builds on these:
  - ExitProtectionPlanner: turns a risk-lane exit decision (fraction + mode) into
    a sandwich-minimizing PRIVATE SPLIT plan — more legs on thin pools, every leg
    private (no public-mempool exposure), each leg a bounded min_out.  The plan's
    modeled sandwich exposure is CLAMPED never to exceed the FAST baseline: a
    routing/split choice may ONLY reduce risk (asymmetric, ADR-0006 / HARD RULE).
  - TipContentionRecorder: stamps the LIVE tip floor + contention bucket per
    candidate at decision time so GATE-A can stratify net PnL by tip-contention
    bucket (C-3) — low-contention-only profit is flaggable as negative-selection
    residual, blocking scale-up.  Point-in-time (no future snapshot).

HARD RULES enforced here:
  - Money is integer lamports / base units (Decimal for fractions) — NEVER float.
  - DRY-RUN by default; real submit is gated behind DRY_RUN_ENABLED=false.
  - No win-rate field/metric anywhere — the bleed metric is tip-as-%-of-PnL.
  - Point-in-time correctness: tips priced from a snapshot stamped at-or-before
    the decision slot; latency anchored to event-time, not compute-time.
  - The LLM/slow model is NEVER on this path — tip pricing and submission are
    deterministic.
"""

from aats.mev.bundle_submitter import (
    AtomicBuyBundle,
    BlockEngineClientProtocol,
    BundleResult,
    BundleStatus,
    BundleSubmitMode,
    JitoBundleSubmitter,
    LiveBundleBlocked,
    MockBlockEngine,
)
from aats.mev.latency_tracer import (
    HOP_CLASS,
    HopClass,
    HopSpan,
    LatencyTrace,
    LatencyTracer,
)
from aats.mev.split_exit import (
    DEFAULT_EXIT_MODE,
    ContentionBucket,
    ExitLeg,
    ExitProtectionPlanner,
    MevExitMode,
    PoolLiquidity,
    SplitExitPlan,
    TipContentionRecorder,
    TipContentionStamp,
    slippage_model_for,
)
from aats.mev.tip_controller import (
    DEFAULT_EDGE_CAP_FRAC,
    TipController,
    TipDecision,
    TipDecisionKind,
)
from aats.mev.tip_stream import (
    StaleTipStreamError,
    StaticTipStream,
    TipFloorSnapshot,
    TipStreamProtocol,
)

__all__ = [
    # latency
    "LatencyTracer",
    "LatencyTrace",
    "HopSpan",
    "HopClass",
    "HOP_CLASS",
    # tips
    "TipController",
    "TipDecision",
    "TipDecisionKind",
    "DEFAULT_EDGE_CAP_FRAC",
    "TipStreamProtocol",
    "StaticTipStream",
    "TipFloorSnapshot",
    "StaleTipStreamError",
    # bundles
    "JitoBundleSubmitter",
    "AtomicBuyBundle",
    "BundleResult",
    "BundleStatus",
    "BundleSubmitMode",
    "BlockEngineClientProtocol",
    "MockBlockEngine",
    "LiveBundleBlocked",
    # T-331 MEV protection — split exits, modes, sandwich avoidance, C-3
    "DEFAULT_EXIT_MODE",
    "MevExitMode",
    "slippage_model_for",
    "ContentionBucket",
    "TipContentionStamp",
    "TipContentionRecorder",
    "ExitLeg",
    "SplitExitPlan",
    "PoolLiquidity",
    "ExitProtectionPlanner",
]
