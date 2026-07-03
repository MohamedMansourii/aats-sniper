"""E15 (risk half) — serial-deployer reputation DE-RISK consumer.

Consumes the LEAK-FREE, point-in-time TALLIES produced by
``aats.ingestion.deployer_reputation`` (``prior_launches``, ``prior_rugs`` — raw
integer counts, NEVER a rate) and applies the EXISTING asymmetric-trust vocabulary
this firm already uses for every other slow-loop filter:

  * a REPEAT-RUG creator wallet (``prior_rugs >= reject_min_prior_rugs``) is an
    instant, pre-gate REJECT — wired the SAME way as the E2 denylist
    (``aats/risk/blacklist.py``): a ``RedFlagHit(RedFlag.SERIAL_DEPLOYER_RUGGED)``
    that short-circuits BEFORE the safety gate runs;
  * a SINGLE prior rug (``downweight_min_prior_rugs <= prior_rugs <
    reject_min_prior_rugs``) DOWN-WEIGHTS conviction — the SAME shape as E13
    anti-FOMO (``aats/risk/anti_fomo.py``): a ``conviction_multiplier`` clamped
    hard to ``[0, 1)`` that can only shrink an already-capped size;
  * a CLEAN or UNKNOWN wallet (``prior_rugs == 0``, regardless of
    ``prior_launches``) is SILENT — ``PASS`` with ``conviction_multiplier == 1``.
    A long clean track record is NEVER upgraded into a buy trigger or a size-up;
    it carries no information beyond "no red flag known" (refuse-by-default:
    absence of a rug is not proof of safety).

ASYMMETRIC / DE-RISK-ONLY (E15 hard rule, non-waivable)
---------------------------------------------------------
  There is no code path here that constructs an entry, sizes a trade, widens a
  stop, raises conviction, or lifts a cap.  ``conviction_multiplier`` is
  STRUCTURALLY clamped to ``[0, 1]`` (``DeployerReputationDecision.__post_init__``
  raises on any attempt to construct a decision that would violate this — not an
  ``assert``, so it survives ``python -O``).  A clean/unknown wallet is SILENT,
  never a buy trigger — exactly the same shape as ``anti_fomo``'s ``NO_PUMP_HISTORY``
  (absence of a red flag is "no opinion", never "verified good").

NO WIN-RATE
------------
  This module reads ONLY the two raw tallies supplied by the ingestion-side
  aggregator.  It never computes, stores, or logs a ratio derived from them (no
  rug-rate, win-rate, or success-rate field anywhere in this module).
  The REJECT/DOWN-WEIGHT/PASS decision is a pure THRESHOLD on the raw counts.

POINT-IN-TIME
--------------
  This module trusts the ``DeployerReputationTally`` it is handed to already be
  point-in-time correct (the ingestion-side store enforces the strict
  ``outcome_slot < as_of_event_slot`` filter — see
  ``aats/ingestion/deployer_reputation.py``).  This module performs NO additional
  time-based filtering of its own and never reads wall-clock; it is a pure
  threshold function of ``(prior_launches, prior_rugs)``.

FAST-PATH SAFE
---------------
  ``evaluate()`` is pure in-memory comparison — no I/O, no RPC, no LLM.  It is
  safe to run on the same pre-gate step as the E2 denylist (before the sub-10ms
  ``PreTradeSafetyGate``), PROVIDED the caller has already produced the
  ``DeployerReputationTally`` off the hot path (the SLOW-loop aggregation /
  Parquet read lives entirely in the ingestion-side module).

MONEY
------
  ``conviction_multiplier`` is ``Decimal`` (money-adjacent DE-RISK multiplier,
  same convention as ``anti_fomo.FomoDecision.conviction_multiplier``).  This
  module does no other money math.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Protocol, runtime_checkable

from aats.risk.pretrade_gate import RedFlag, RedFlagHit

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Reason taxonomy — dashboard / reasoner-facing vocabulary.  Stable codes.
# ---------------------------------------------------------------------------


class DeployerReputationReason(StrEnum):
    """Stable E15 reason codes (dashboard + SLOW-loop reasoner contract)."""

    SERIAL_DEPLOYER_RUGGED = "serial_deployer_rugged"  # REJECT: repeat-rug wallet
    PRIOR_RUG_DOWNWEIGHT = "prior_rug_downweight"  # DOWN_WEIGHT: single prior rug
    NO_REPUTATION_SIGNAL = "no_reputation_signal"  # info only: clean/unknown wallet


class DeployerReputationVerdict(StrEnum):
    PASS = "PASS"  # clean/unknown — SILENT (never a buy trigger)
    DOWN_WEIGHT = "DOWN_WEIGHT"  # single prior rug — conviction HAIRCUT (< 1)
    REJECT = "REJECT"  # repeat-rug wallet — EXCLUDED


# ---------------------------------------------------------------------------
# Thresholds — tighten-only de-risk knobs.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DeployerReputationThresholds:
    """Tunable E15 thresholds.  Every knob is DE-RISK / SELECTIVITY ONLY.

    LOWERING either ``*_min_prior_rugs`` threshold (tightening) only REJECTS or
    DOWN-WEIGHTS MORE — always safe.  ``downweight_multiplier`` LOWERING (more
    haircut) is likewise always safe; it can never be raised to >= 1.
    """

    # >= this many prior confirmed rugs on the SAME wallet => DOWN-WEIGHT.
    # Default 1: a single prior rug is a real signal but not (alone) a hard kill.
    downweight_min_prior_rugs: int = 1
    # >= this many prior confirmed rugs => hard REJECT (repeat/serial rugger).
    # Default 2: an unambiguous pattern, not a single bad outcome.
    reject_min_prior_rugs: int = 2
    # Conviction MULTIPLIER applied in the DOWN_WEIGHT band, in [0, 1).
    # Default 0.25 => a single prior rug cuts conviction to a quarter.
    downweight_multiplier: Decimal = Decimal("0.25")

    def __post_init__(self) -> None:
        if isinstance(self.downweight_multiplier, bool) or not isinstance(
            self.downweight_multiplier, Decimal | int | str | float
        ):
            raise TypeError("downweight_multiplier must be Decimal/int/float/str, not bool")
        if not isinstance(self.downweight_multiplier, Decimal):
            object.__setattr__(
                self, "downweight_multiplier", Decimal(str(self.downweight_multiplier))
            )
        for name in ("downweight_min_prior_rugs", "reject_min_prior_rugs"):
            val = getattr(self, name)
            if not isinstance(val, int) or isinstance(val, bool):
                raise TypeError(f"{name} must be int")
            if val < 1:
                raise ValueError(f"{name} must be >= 1")
        if self.reject_min_prior_rugs < self.downweight_min_prior_rugs:
            raise ValueError(
                f"reject_min_prior_rugs ({self.reject_min_prior_rugs}) must be >= "
                f"downweight_min_prior_rugs ({self.downweight_min_prior_rugs})"
            )
        if not (Decimal(0) <= self.downweight_multiplier < Decimal(1)):
            raise ValueError("downweight_multiplier must be a fraction in [0, 1) — de-risk only")

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> DeployerReputationThresholds:
        """Build thresholds from environment (all optional; defaults if unset).

        Env keys documented in ``.env.example`` (E15 block).  A malformed value
        raises (fail-closed on config).
        """
        import os

        e = env if env is not None else os.environ

        def _int(key: str, default: int) -> int:
            raw = e.get(key)
            return int(raw) if raw is not None and raw != "" else default

        def _dec(key: str, default: Decimal) -> Decimal:
            raw = e.get(key)
            return Decimal(str(raw)) if raw is not None and raw != "" else default

        return cls(
            downweight_min_prior_rugs=_int(
                "DEPLOYER_REPUTATION_DOWNWEIGHT_MIN_PRIOR_RUGS", cls.downweight_min_prior_rugs
            ),
            reject_min_prior_rugs=_int(
                "DEPLOYER_REPUTATION_REJECT_MIN_PRIOR_RUGS", cls.reject_min_prior_rugs
            ),
            downweight_multiplier=_dec(
                "DEPLOYER_REPUTATION_DOWNWEIGHT_MULTIPLIER", cls.downweight_multiplier
            ),
        )


# ---------------------------------------------------------------------------
# Structural view of the ingestion-side tally — decoupled by design (no hard
# import dependency from risk/ onto ingestion/; any object with this shape,
# including aats.ingestion.deployer_reputation.DeployerReputationTally, works).
# ---------------------------------------------------------------------------


@runtime_checkable
class DeployerReputationTallyLike(Protocol):
    creator_wallet: str
    deploy_template_fingerprint: str | None
    as_of_event_slot: int
    prior_launches: int
    prior_rugs: int


# ---------------------------------------------------------------------------
# Decision — REJECT / DOWN_WEIGHT / PASS only.  Never sizes / buys.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DeployerReputationDecision:
    """The E15 verdict.  Structurally incapable of raising conviction/size.

    ``conviction_multiplier`` is hard-clamped to ``[0, 1]`` by the invariants
    below — NOT an ``assert`` (survives ``python -O`` / ``PYTHONOPTIMIZE=1``,
    matching the DEF-E10-01 / DEF-EN1-01 precedent).  ``red_flag_hit`` is
    populated ONLY on REJECT so a pre-gate composition (mirroring
    ``evaluate_with_denylist``) can short-circuit BEFORE the safety gate runs.
    """

    creator_wallet: str
    deploy_template_fingerprint: str | None
    verdict: DeployerReputationVerdict
    conviction_multiplier: Decimal
    reason_codes: tuple[str, ...]
    prior_launches: int
    prior_rugs: int
    as_of_event_slot: int
    event_block_time_ms: int
    red_flag_hit: RedFlagHit | None

    def __post_init__(self) -> None:
        if not (Decimal(0) <= self.conviction_multiplier <= Decimal(1)):
            raise ValueError(
                "DeployerReputationDecision.conviction_multiplier must be in [0, 1] "
                "(E15 is DE-RISK ONLY; it can never raise conviction)."
            )
        if self.verdict is DeployerReputationVerdict.REJECT:
            if self.conviction_multiplier != Decimal(0):
                raise ValueError("a REJECT must carry conviction_multiplier == 0")
            if self.red_flag_hit is None:
                raise ValueError("a REJECT must carry a red_flag_hit (pre-gate veto contract)")
            if self.red_flag_hit.flag is not RedFlag.SERIAL_DEPLOYER_RUGGED:
                raise ValueError("a REJECT's red_flag_hit must be RedFlag.SERIAL_DEPLOYER_RUGGED")
        if self.verdict is DeployerReputationVerdict.PASS:
            if self.conviction_multiplier != Decimal(1):
                raise ValueError("a PASS must carry conviction_multiplier == 1 (no change)")
            if self.red_flag_hit is not None:
                raise ValueError("a PASS must not carry a red_flag_hit")
        if self.verdict is DeployerReputationVerdict.DOWN_WEIGHT:
            if not (Decimal(0) <= self.conviction_multiplier < Decimal(1)):
                raise ValueError("a DOWN_WEIGHT must carry conviction_multiplier in [0, 1)")
            if self.red_flag_hit is not None:
                raise ValueError("a DOWN_WEIGHT must not carry a red_flag_hit (it does not reject)")

    @property
    def passed(self) -> bool:
        """True iff NOT rejected.  NOTE: DOWN_WEIGHT is 'passed but de-rated'."""
        return self.verdict is not DeployerReputationVerdict.REJECT

    @property
    def rejected(self) -> bool:
        return self.verdict is DeployerReputationVerdict.REJECT

    @property
    def down_weighted(self) -> bool:
        return self.verdict is DeployerReputationVerdict.DOWN_WEIGHT

    def apply(self, capped_size: int) -> int:
        """Apply the conviction haircut to an ALREADY-CAPPED integer size (lamports).

        DE-RISK ONLY: ``floor(capped_size * conviction_multiplier) <= capped_size``
        always (the multiplier is clamped to <= 1).  A REJECT yields 0.
        """
        if not isinstance(capped_size, int) or isinstance(capped_size, bool):
            raise TypeError("capped_size must be int lamports (money rule)")
        if capped_size < 0:
            raise ValueError("capped_size must be non-negative lamports")
        scaled = (Decimal(capped_size) * self.conviction_multiplier).to_integral_value(
            rounding="ROUND_FLOOR"
        )
        out = int(scaled)
        return min(out, capped_size)  # defence-in-depth: never exceed the input


# ---------------------------------------------------------------------------
# Telemetry seam — we INVOKE a sink; we do not author Prometheus here.
# ---------------------------------------------------------------------------


@runtime_checkable
class DeployerReputationTelemetry(Protocol):
    def on_deployer_reject(self, creator_wallet: str, prior_rugs: int) -> None: ...

    def on_deployer_downweight(
        self, creator_wallet: str, prior_rugs: int, multiplier: str
    ) -> None: ...


# ---------------------------------------------------------------------------
# The gate.
# ---------------------------------------------------------------------------


@dataclass
class DeployerReputationGate:
    """Pure threshold evaluator over a ``DeployerReputationTally``-shaped input.

    ``evaluate(tally)`` may ONLY:
      * REJECT (``prior_rugs >= reject_min_prior_rugs``) — a ``RedFlagHit`` usable
        the same way as the E2 denylist's pre-gate veto, or
      * DOWN_WEIGHT (``downweight_min_prior_rugs <= prior_rugs <
        reject_min_prior_rugs``) — a conviction MULTIPLIER < 1, or
      * PASS (``prior_rugs == 0``) — SILENT, multiplier == 1, never a buy trigger.
    """

    thresholds: DeployerReputationThresholds = field(default_factory=DeployerReputationThresholds)
    telemetry: DeployerReputationTelemetry | None = None

    def evaluate(
        self,
        tally: DeployerReputationTallyLike,
        *,
        event_block_time_ms: int = 0,
    ) -> DeployerReputationDecision:
        prior_launches = tally.prior_launches
        prior_rugs = tally.prior_rugs

        # Refuse-by-default: never silently trust a malformed tally (negative
        # counts, or more rugs than launches) — that is a defect upstream, not
        # something this gate should paper over with a PASS.
        if prior_launches < 0 or prior_rugs < 0 or prior_rugs > prior_launches:
            raise ValueError(
                f"DeployerReputationGate: malformed tally for {tally.creator_wallet!r} "
                f"(prior_launches={prior_launches}, prior_rugs={prior_rugs})"
            )

        reasons: tuple[DeployerReputationReason, ...]
        if prior_rugs >= self.thresholds.reject_min_prior_rugs:
            verdict = DeployerReputationVerdict.REJECT
            multiplier = Decimal(0)
            reasons = (DeployerReputationReason.SERIAL_DEPLOYER_RUGGED,)
            red_flag_hit: RedFlagHit | None = RedFlagHit(
                RedFlag.SERIAL_DEPLOYER_RUGGED,
                prior_rugs,
                self.thresholds.reject_min_prior_rugs,
            )
        elif prior_rugs >= self.thresholds.downweight_min_prior_rugs:
            verdict = DeployerReputationVerdict.DOWN_WEIGHT
            multiplier = self.thresholds.downweight_multiplier
            reasons = (DeployerReputationReason.PRIOR_RUG_DOWNWEIGHT,)
            red_flag_hit = None
        else:
            # prior_rugs == 0: clean or unknown wallet.  SILENT — never a buy
            # trigger or size-up, regardless of how many prior_launches exist.
            verdict = DeployerReputationVerdict.PASS
            multiplier = Decimal(1)
            reasons = ()
            red_flag_hit = None

        decision = DeployerReputationDecision(
            creator_wallet=tally.creator_wallet,
            deploy_template_fingerprint=tally.deploy_template_fingerprint,
            verdict=verdict,
            conviction_multiplier=multiplier,
            reason_codes=tuple(r.value for r in reasons),
            prior_launches=prior_launches,
            prior_rugs=prior_rugs,
            as_of_event_slot=tally.as_of_event_slot,
            event_block_time_ms=event_block_time_ms,
            red_flag_hit=red_flag_hit,
        )

        if self.telemetry is not None:
            if decision.rejected:
                self.telemetry.on_deployer_reject(tally.creator_wallet, prior_rugs)
            elif decision.down_weighted:
                self.telemetry.on_deployer_downweight(
                    tally.creator_wallet, prior_rugs, str(multiplier)
                )

        return decision


# ---------------------------------------------------------------------------
# Composed pre-gate veto — mirrors blacklist.py's evaluate_with_denylist.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DeployerReputationPreGateResult:
    """Result of composing the E15 gate BEFORE a downstream step.

    ``next_result`` is ``None`` iff the E15 gate REJECTED and short-circuited
    BEFORE the downstream step ran — proving the downstream step never green-lit
    a repeat-rug wallet.
    """

    passed: bool
    decision: DeployerReputationDecision
    next_result: object | None

    @property
    def rejected_by_reputation(self) -> bool:
        return self.decision.rejected


def evaluate_with_deployer_reputation(
    *,
    gate: DeployerReputationGate,
    tally: DeployerReputationTallyLike,
    next_step,
    event_block_time_ms: int = 0,
) -> DeployerReputationPreGateResult:
    """Run the E15 reputation gate, THEN ``next_step()`` — REJECT-only short-circuit.

    ``next_step`` is a zero-arg callable (e.g. ``lambda: evaluate_with_denylist(...)``
    or ``lambda: safety_gate.evaluate_fast(inputs)``) invoked ONLY when the
    reputation gate does not REJECT.  This makes "skip the pre-gate veto because
    the wallet has a clean-so-far tally" structurally inexpressible: there is no
    branch that returns ``passed=True`` on a REJECT without ``next_step`` ever
    running (``next_result is None`` proves it never ran).

    A DOWN_WEIGHT verdict does NOT short-circuit — ``next_step`` still runs; the
    caller applies ``decision.conviction_multiplier`` to sizing separately (the
    same non-blocking shape as ``anti_fomo``'s down-weight band).
    """
    decision = gate.evaluate(tally, event_block_time_ms=event_block_time_ms)
    if decision.rejected:
        return DeployerReputationPreGateResult(passed=False, decision=decision, next_result=None)

    next_result = next_step()
    passed = bool(getattr(next_result, "passed", True))
    return DeployerReputationPreGateResult(
        passed=passed, decision=decision, next_result=next_result
    )
