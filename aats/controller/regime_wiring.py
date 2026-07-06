"""M3 Controller — SLOW-loop REGIME / SURVIVOR de-risk wiring (M2-CP-04, ADR-0014).

WHAT THIS IS (and where it MUST NOT run)
=========================================
This module is the missing wire that finally connects the SLOW-loop chart-path /
regime model (M2-CP-01/02/03) and the previously-ORPHANED survivor model
(`aats.models.survivor`) into the running control plane — the SAME de-risk-only
way the E14/E17/E19 catastrophic-exit flags are wired (`enrichment_wiring.py`).

It runs on the SLOW loop ONLY (`assert_slow_loop_only`, reused from
`aats.models.survivor`).  It NEVER runs on the FAST/SNIPE hot path and holds no
ONNX/Rust shim.  The heavy ML guard is imported LAZILY inside the guarded methods
so importing this controller module (and `loops.py`) stays lightweight — the
regime/survivor models are only pulled in when a SLOW-cadence caller actually
drives them.

HOW A REGIME/SURVIVOR OUTPUT REACHES EXECUTION — PRE-SET SCALAR FLAGS ONLY
=========================================================================
The regime STATE and the survivor P(survive) are translated into a
`RegimeDeRiskDirective` and then pre-staged as the EXISTING pre-set scalar flags
that `exit_engine` / `rule_engine` ALREADY read:

    directive         StateStore flag set                 downstream effect
    ---------         -------------------                 -----------------
    NONE       -> (nothing — INERT)                       hold; provably no effect
    REDUCE     -> set_veto_flag(mint, ttl//2)             de-risk (shrink / skip new entry)
    VETO_ENTRY -> set_veto_flag(mint)                     de-risk (SNIPE skips a NEW entry)
    FORCE_EXIT -> set_narrative_failure_flag(mint)        FAST force-exits the open position

The SNIPE loop reads a PRE-COMPUTED scalar (`get_veto_flag`); the FAST loop reads
a PRE-COMPUTED scalar (`get_narrative_failure_flag`).  Neither loop ever consumes
the `RegimeSignal`/`SurvivorPrediction` object or calls the model — exactly the
`_apply_reasoning_verdict` pattern already used for the LLM reasoner in
`slow_loop.py`.  This module invents NO new StateStore key and NO new hot-path
read; it reuses the audited narrative/veto mechanism.

THE ASYMMETRIC-TRUST LAW (de-risk only; accumulation/bullish provably INERT)
===========================================================================
`_apply_directive` is an EXPLICIT allowlist over the de-risk-or-inert codomain
{NONE, REDUCE, FORCE_EXIT, VETO_ENTRY}.  There is no branch that sizes up, widens
a stop, relaxes a stop, delays an exit, or adds leverage — such a directive is
inexpressible by type (`RegimeDeRiskDirective`, ADR-0014) AND unreachable here.
The bullish/ranging regimes (ACCUMULATION / NEUTRAL) map to NONE and are a strict
no-op: they set NO flag, so they can NEVER delay an exit, relax a stop, or size up.
The survivor translation is monotone NON-INCREASING in P(survive): a HIGHER
survival probability can only move toward NONE (never toward more control), so a
confident "survives" prediction carries ZERO risk-increasing authority.

POINT-IN-TIME
=============
Every signal carries `event_time`; this module uses it only for the audit
log/reason, never as a decision field, and never compares it to wall-clock.  The
TTLs on the flags are monitoring-side expiry, not a decision clock.

MONEY / SECRETS
===============
This module touches no money field, sizes no trade, and holds no key, RPC, or
network.  It only ever SETS a de-risk StateStore flag.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from aats.contracts.models import RegimeDeRiskDirective, RegimeSignal

if TYPE_CHECKING:  # typing only — never pulled into the runtime import graph
    from aats.models.survivor import SurvivorPrediction

logger = logging.getLogger(__name__)

# Conservative fixed de-risk thresholds for the survivor P(survive AND sellable).
# Monotone NON-INCREASING in p_calibrated: a HIGHER survival probability moves
# strictly toward NONE (never toward more control).  These are NOT win-rates —
# they are de-risk trip points on a calibrated survival probability.
DEFAULT_FORCE_EXIT_SURVIVAL_BELOW = 0.15   # survival collapsing -> FORCE_EXIT
DEFAULT_REDUCE_SURVIVAL_BELOW = 0.35       # weak survival -> REDUCE
DEFAULT_HIGH_UNCERTAINTY_REDUCE_AT = 0.85  # very uncertain -> at least REDUCE


# ---------------------------------------------------------------------------
# Structural read/write surfaces (no hard import of the concrete StateStore)
# ---------------------------------------------------------------------------


@runtime_checkable
class RegimeDeRiskFlagSink(Protocol):
    """The minimal StateStore surface this wiring needs.

    Satisfied by `aats.controller.state.StateStore` (InMemory + Redis) WITHOUT
    importing the concrete classes — only the shape.  All three methods already
    exist and are the EXISTING de-risk mechanism; this wiring adds no new key.
    """

    def set_veto_flag(self, mint: str, ttl_seconds: int = 60) -> None: ...  # pragma: no cover

    def set_narrative_failure_flag(
        self, mint: str, ttl_seconds: int = 120
    ) -> None: ...  # pragma: no cover

    def load_all_positions(self) -> list: ...  # pragma: no cover


@runtime_checkable
class SurvivorModelLike(Protocol):
    """A trained survivor model that emits a calibrated `SurvivorPrediction`.

    Satisfied by `aats.models.survivor.TrainedSurvivorModel` WITHOUT importing it
    at runtime (kept behind the injection seam so the controller import graph never
    pulls lightgbm).  `predict` is SLOW-loop only (its own `assert_slow_loop_only`).
    """

    def predict(
        self, frame: object, mcs: object, *, loop: str = "slow"
    ): ...  # pragma: no cover


@runtime_checkable
class SurvivorFeatureSource(Protocol):
    """Injectable source of the survivor covariate inputs for ONE open mint.

    Returns `(frame, mcs)` for a mint, or `None` when no survivor features are
    available yet (no coverage) — treated as a SKIP (counted, logged), never as a
    forced action.  This mirrors the enrichment wiring's "NOT YET WIRED" handling:
    absence of an input is not a de-risk signal.
    """

    def get_survivor_inputs(
        self, mint: str
    ) -> tuple[object, object | None] | None: ...  # pragma: no cover


# ---------------------------------------------------------------------------
# Stats — observability that proves the wiring ran (never silent)
# ---------------------------------------------------------------------------


@dataclass
class RegimeWiringStats:
    """Runtime counters — each proves a path actually ran (mirrors EnrichmentStats)."""

    regime_signals_applied: int = 0
    survivor_predictions_applied: int = 0
    survivor_features_absent_skipped: int = 0
    force_exits_raised: int = 0
    reduces_raised: int = 0
    veto_entries_raised: int = 0
    inert_noops: int = 0


# ---------------------------------------------------------------------------
# SlowLoopRegimeWiring — the single SLOW-loop de-risk translator
# ---------------------------------------------------------------------------


class SlowLoopRegimeWiring:
    """Wires the regime + (orphaned) survivor models into the control plane, de-risk-only.

    USAGE (wired by `ControllerOrchestrator`; see `loops.py`):

        regime = SlowLoopRegimeWiring(
            store=state_store,
            survivor_model=trained_survivor,        # optional (None = disabled)
            survivor_feature_source=feature_source,  # optional (None = disabled)
        )
        orchestrator = ControllerOrchestrator(..., regime_wiring=regime)

        # event/candidate-driven (a fresh RegimeSignal is produced off the hot path):
        orchestrator.apply_regime_signal(signal)

        # periodic, SLOW cadence (e.g. every 5-30s), off the hot path:
        orchestrator.run_slow_regime_tick(event_slot=current_slot)

    The survivor model + feature source are OPTIONAL (`None` disables that path
    cleanly and is counted, never silently pretended covered) so the wiring can be
    constructed before the (bootstrap, no-capital-license) survivor model is armed.
    """

    def __init__(
        self,
        store: RegimeDeRiskFlagSink,
        *,
        survivor_model: SurvivorModelLike | None = None,
        survivor_feature_source: SurvivorFeatureSource | None = None,
        force_exit_survival_below: float = DEFAULT_FORCE_EXIT_SURVIVAL_BELOW,
        reduce_survival_below: float = DEFAULT_REDUCE_SURVIVAL_BELOW,
        high_uncertainty_reduce_at: float = DEFAULT_HIGH_UNCERTAINTY_REDUCE_AT,
        veto_ttl_seconds: int = 60,
        narrative_ttl_seconds: int = 120,
    ) -> None:
        if not (0.0 <= force_exit_survival_below <= reduce_survival_below <= 1.0):
            raise ValueError(
                "survivor thresholds must satisfy 0 <= force_exit_survival_below "
                f"<= reduce_survival_below <= 1 (got {force_exit_survival_below}, "
                f"{reduce_survival_below})."
            )
        if not (0.0 <= high_uncertainty_reduce_at <= 1.0):
            raise ValueError(
                f"high_uncertainty_reduce_at must be in [0, 1], got {high_uncertainty_reduce_at}."
            )
        self._store = store
        self._survivor_model = survivor_model
        self._survivor_feature_source = survivor_feature_source
        self._force_exit_survival_below = force_exit_survival_below
        self._reduce_survival_below = reduce_survival_below
        self._high_uncertainty_reduce_at = high_uncertainty_reduce_at
        self._veto_ttl = veto_ttl_seconds
        self._narrative_ttl = narrative_ttl_seconds
        self.stats = RegimeWiringStats()

    # -- guard ------------------------------------------------------------

    @staticmethod
    def _assert_slow_loop_only(loop: str) -> None:
        """Reuse the CANONICAL slow-loop guard (lazily imported to keep this module light).

        The chart-path/regime model AND the survivor model are SLOW-loop only
        (BLUEPRINT triple-loop boundary).  A fast/snipe loop raises loudly.
        """
        from aats.models.survivor import assert_slow_loop_only

        assert_slow_loop_only(loop)

    # -- regime path ------------------------------------------------------

    def apply_regime_signal(
        self, signal: RegimeSignal, *, loop: str = "slow"
    ) -> RegimeDeRiskDirective:
        """Translate ONE `RegimeSignal` into a pre-set de-risk flag and apply it.

        SLOW-loop only.  Maps `signal.derisk_directive` (the taxonomy-fixed, de-risk-
        or-inert directive) to the existing narrative/veto flags.  The bullish/ranging
        regimes (ACCUMULATION / NEUTRAL) are INERT (NONE) — a strict no-op.  Returns the
        directive applied (NONE when inert).
        """
        self._assert_slow_loop_only(loop)
        directive = signal.derisk_directive
        self.stats.regime_signals_applied += 1
        return self._apply_directive(
            signal.mint, directive, event_slot=signal.event_time.slot, source="regime"
        )

    # -- survivor path (finally wiring the orphaned survivor model) --------

    def survivor_directive(self, pred: SurvivorPrediction) -> RegimeDeRiskDirective:
        """Pure translation of a `SurvivorPrediction` -> de-risk directive (NO side effects).

        Monotone NON-INCREASING in `p_calibrated` (higher survival probability moves
        strictly toward NONE).  Codomain is {NONE, REDUCE, FORCE_EXIT} — there is NO
        size-up branch, so the survivor output can ONLY de-risk or be inert:

            p_calibrated <= force_exit_survival_below      -> FORCE_EXIT
            p_calibrated <= reduce_survival_below           -> REDUCE
            uncertainty  >= high_uncertainty_reduce_at      -> REDUCE (very uncertain)
            otherwise (healthy survival, low uncertainty)   -> NONE   (INERT)
        """
        p = float(pred.p_calibrated)
        u = float(pred.uncertainty)
        if p <= self._force_exit_survival_below:
            return RegimeDeRiskDirective.FORCE_EXIT
        if p <= self._reduce_survival_below or u >= self._high_uncertainty_reduce_at:
            return RegimeDeRiskDirective.REDUCE
        return RegimeDeRiskDirective.NONE

    def apply_survivor_prediction(
        self, pred: SurvivorPrediction, *, loop: str = "slow"
    ) -> RegimeDeRiskDirective:
        """Translate ONE `SurvivorPrediction` into a pre-set de-risk flag and apply it.

        SLOW-loop only.  De-risk only (see `survivor_directive`).  Returns the directive
        applied (NONE when the survival read is healthy / inert).
        """
        self._assert_slow_loop_only(loop)
        directive = self.survivor_directive(pred)
        self.stats.survivor_predictions_applied += 1
        return self._apply_directive(
            pred.mint, directive, event_slot=pred.event_time.slot, source="survivor"
        )

    def run_survivor_derisk(
        self, event_slot: int, *, loop: str = "slow"
    ) -> list[RegimeDeRiskDirective]:
        """Run the survivor de-risk read over EVERY open position at the SLOW cadence.

        For each open position, fetch its survivor inputs via the injected feature
        source and apply the (de-risk-only) survivor directive.  A mint whose features
        are absent is SKIPPED and counted (`survivor_features_absent_skipped`) — never
        forced to act.  Returns `[]` (no-op) if no model OR no feature source is wired.

        Off the FAST/SNIPE hot path — call from a periodic SLOW-loop driver only.
        """
        self._assert_slow_loop_only(loop)
        if self._survivor_model is None or self._survivor_feature_source is None:
            return []
        directives: list[RegimeDeRiskDirective] = []
        for pos in self._store.load_all_positions():
            mint = pos.mint  # type: ignore[attr-defined]
            inputs = self._survivor_feature_source.get_survivor_inputs(mint)
            if inputs is None:
                self.stats.survivor_features_absent_skipped += 1
                logger.debug(
                    "regime_wiring.survivor_features_absent: mint=%s event_slot=%d "
                    "(no survivor coverage yet — skipped, not forced)",
                    mint,
                    event_slot,
                )
                continue
            frame, mcs = inputs
            pred = self._survivor_model.predict(frame, mcs, loop=loop)
            directives.append(self.apply_survivor_prediction(pred, loop=loop))
        return directives

    # -- the SINGLE de-risk application surface (explicit allowlist) -------

    def _apply_directive(
        self,
        mint: str,
        directive: RegimeDeRiskDirective,
        *,
        event_slot: int,
        source: str,
    ) -> RegimeDeRiskDirective:
        """Apply a de-risk directive by setting the EXISTING pre-set scalar flag.

        ASYMMETRIC-TRUST GUARD (enforced here, not by comment) — an EXPLICIT allowlist
        over the de-risk-or-inert codomain.  There is no branch that increases risk:

          NONE        -> no-op (INERT: sets NO flag — provably cannot delay an exit,
                         relax a stop, or size up)
          REDUCE      -> set_veto_flag(mint, ttl//2)   (de-risk; same as REDUCE_SIZE)
          VETO_ENTRY  -> set_veto_flag(mint)            (de-risk: SNIPE skips a NEW entry)
          FORCE_EXIT  -> set_narrative_failure_flag(mint)  (FAST force-exits the position)

        Anything else is a risk-increase attempt (inexpressible by the
        `RegimeDeRiskDirective` type) and is REJECTED — logged, never applied.
        `event_slot` is used only for the audit log (point-in-time; never wall-clock).
        """
        if directive is RegimeDeRiskDirective.NONE:
            self.stats.inert_noops += 1
            logger.debug(
                "regime_wiring.inert: mint=%s source=%s event_slot=%d (bullish/ranging "
                "or healthy survival — no de-risk authority, no flag set)",
                mint,
                source,
                event_slot,
            )
            return directive

        if directive is RegimeDeRiskDirective.REDUCE:
            self._store.set_veto_flag(mint, ttl_seconds=self._veto_ttl // 2)
            self.stats.reduces_raised += 1
            logger.info(
                "regime_wiring.reduce: mint=%s source=%s event_slot=%d (de-risk)",
                mint,
                source,
                event_slot,
            )
            return directive

        if directive is RegimeDeRiskDirective.VETO_ENTRY:
            self._store.set_veto_flag(mint, ttl_seconds=self._veto_ttl)
            self.stats.veto_entries_raised += 1
            logger.info(
                "regime_wiring.veto_entry: mint=%s source=%s event_slot=%d (de-risk)",
                mint,
                source,
                event_slot,
            )
            return directive

        if directive is RegimeDeRiskDirective.FORCE_EXIT:
            self._store.set_narrative_failure_flag(mint, ttl_seconds=self._narrative_ttl)
            self.stats.force_exits_raised += 1
            logger.info(
                "regime_wiring.force_exit: mint=%s source=%s event_slot=%d "
                "(rug-in-progress / survival collapse — FAST will exit)",
                mint,
                source,
                event_slot,
            )
            return directive

        # Unreachable while the type is intact (the enum has only the four members
        # above, none risk-increasing).  Belt-and-suspenders firewall — never applies.
        logger.error(  # pragma: no cover
            "regime_wiring.REJECTED_risk_increase: mint=%s source=%s directive=%s "
            "(asymmetric-trust violation — not applied)",
            mint,
            source,
            directive,
        )
        return RegimeDeRiskDirective.NONE  # pragma: no cover
