"""E16 (risk half) — dev-funded-just-before-launch fresh-wallet DE-RISK consumer.

Consumes the LEAK-FREE, point-in-time ``DevFundingAgeResult`` produced by
``aats.ingestion.dev_funding_age`` (raw decoded ``funding_age_slots`` /
``funding_age_ms`` — event-time deltas, NEVER a rate/score) and applies the
EXISTING asymmetric-trust vocabulary this firm already uses for every other
slow-loop filter (same shape as E15's ``deployer_reputation_gate.py``):

  * a wallet FIRST FUNDED within ``reject_max_age_seconds`` of the deploy
    (a classic burner-wallet rug tell — freshly funded, immediately deploys)
    is an instant, pre-gate REJECT — a ``RedFlagHit(RedFlag.
    DEV_FUNDED_JUST_BEFORE_LAUNCH)`` that short-circuits BEFORE the safety
    gate runs, exactly like the E2 denylist / E15 serial-deployer reject;
  * a wallet funded in the wider ``(reject_max_age_seconds,
    downweight_max_age_seconds]`` band DOWN-WEIGHTS conviction — a
    ``conviction_multiplier`` clamped hard to ``[0, 1)``, the same shape as
    E13 anti-FOMO / E15's single-prior-rug band;
  * a wallet whose funding predates ``downweight_max_age_seconds`` (an
    established, non-fresh wallet) is SILENT — ``PASS`` with
    ``conviction_multiplier == 1``.  A long-funded wallet is NEVER upgraded
    into a buy trigger or a size-up; it carries no information beyond "no red
    flag known" (refuse-by-default: absence of the tell is not proof of
    safety);
  * an UNDECODABLE result (the ingestion side could not conclusively decode
    the wallet's funding history) is ALSO an instant pre-gate REJECT — a
    DISTINCT ``RedFlagHit(RedFlag.DEV_FUNDING_UNDECODABLE)`` so the dashboard
    / audit trail never conflates "we proved this wallet is a rug tell" with
    "we simply could not check" (the module owner's "honest tagging" ethos —
    see ``dev_funding_age.py`` module docstring).  This mirrors the EXISTING
    codebase-wide refuse-by-default convention for missing/undecodable
    enrichment data (``pretrade_gate.py``'s ``INPUT_INCOMPLETE`` and
    ``screener.py``'s missing-datum EXCLUDE) — unknown is NEVER "safe".

ASYMMETRIC / DE-RISK-ONLY (E16 hard rule, non-waivable)
---------------------------------------------------------
  There is no code path here that constructs an entry, sizes a trade, widens
  a stop, raises conviction, or lifts a cap.  ``conviction_multiplier`` is
  STRUCTURALLY clamped to ``[0, 1]`` (``DevFundingAgeDecision.__post_init__``
  raises on any attempt to construct a decision that would violate this — not
  an ``assert``, so it survives ``python -O``, matching the DEF-E10-01 /
  DEF-EN1-01 / E15 precedent).

NO WIN-RATE
------------
  This module reads ONLY the raw event-time deltas supplied by the ingestion
  side.  It never computes, stores, or logs a rate/ratio/probability
  anywhere.  The REJECT/DOWN-WEIGHT/PASS decision is a pure THRESHOLD on
  ``funding_age_ms`` (an integer millisecond delta between two on-chain
  anchors).

POINT-IN-TIME
--------------
  This module trusts the ``DevFundingAgeResult`` it is handed to already be
  point-in-time correct (the ingestion-side decoder enforces
  ``first_funding_slot <= deploy_event_slot`` structurally — see
  ``aats/ingestion/dev_funding_age.py``).  This module performs NO additional
  time-based filtering of its own and never reads wall-clock; it is a pure
  threshold function of ``(status, funding_age_ms)``.

FAST-PATH SAFE
---------------
  ``evaluate()`` is pure in-memory comparison — no I/O, no RPC, no LLM.  It is
  safe to run on the same pre-gate step as the E2 denylist / E15 reputation
  gate, PROVIDED the caller has already produced the ``DevFundingAgeResult``
  off the hot path (the RPC decode lives entirely in
  ``aats/ingestion/dev_funding_age.py``).

MONEY
------
  ``conviction_multiplier`` is ``Decimal`` (money-adjacent DE-RISK multiplier,
  same convention as ``anti_fomo.FomoDecision`` / E15's
  ``DeployerReputationDecision``).  This module does no other money math.
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


class DevFundingAgeReason(StrEnum):
    """Stable E16 reason codes (dashboard + SLOW-loop reasoner contract)."""

    DEV_FUNDED_JUST_BEFORE_LAUNCH = "dev_funded_just_before_launch"  # REJECT band
    FRESH_FUNDING_DOWNWEIGHT = "fresh_funding_downweight"  # DOWN_WEIGHT band
    DEV_FUNDING_UNDECODABLE = "dev_funding_undecodable"  # REJECT: refuse-by-default
    NO_FUNDING_AGE_SIGNAL = "no_funding_age_signal"  # info only: established wallet


class DevFundingAgeVerdict(StrEnum):
    PASS = "PASS"  # established wallet — SILENT (never a buy trigger)
    DOWN_WEIGHT = "DOWN_WEIGHT"  # fresh-ish funding — conviction HAIRCUT (< 1)
    REJECT = "REJECT"  # extremely fresh funding OR undecodable — EXCLUDED


# ---------------------------------------------------------------------------
# Thresholds — tighten-only de-risk knobs.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DevFundingAgeThresholds:
    """Tunable E16 thresholds.  Every knob is DE-RISK / SELECTIVITY ONLY.

    RAISING either ``*_max_age_seconds`` threshold WIDENS the window treated
    as "suspiciously fresh" — it can only REJECT or DOWN-WEIGHT MORE, never
    less (always safe).  ``downweight_multiplier`` LOWERING (more haircut) is
    likewise always safe; it can never be raised to >= 1.
    """

    # Funding age (deploy_block_time_ms - first_funding_block_time_ms), in
    # seconds, <= this value => hard REJECT (a burner wallet funded seconds
    # before it deploys is the textbook rug tell).  Default 120s (2 minutes).
    reject_max_age_seconds: int = 120
    # Funding age <= this value (but > reject_max_age_seconds) => DOWN-WEIGHT.
    # Default 1800s (30 minutes) — "freshly funded, minutes before deploy" per
    # the E16 directive, without being an automatic kill.
    downweight_max_age_seconds: int = 1_800
    # Conviction MULTIPLIER applied in the DOWN_WEIGHT band, in [0, 1).
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
        for name in ("reject_max_age_seconds", "downweight_max_age_seconds"):
            val = getattr(self, name)
            if not isinstance(val, int) or isinstance(val, bool):
                raise TypeError(f"{name} must be int")
            if val < 0:
                raise ValueError(f"{name} must be >= 0")
        if self.downweight_max_age_seconds < self.reject_max_age_seconds:
            raise ValueError(
                f"downweight_max_age_seconds ({self.downweight_max_age_seconds}) must be >= "
                f"reject_max_age_seconds ({self.reject_max_age_seconds})"
            )
        if not (Decimal(0) <= self.downweight_multiplier < Decimal(1)):
            raise ValueError("downweight_multiplier must be a fraction in [0, 1) — de-risk only")

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> DevFundingAgeThresholds:
        """Build thresholds from environment (all optional; defaults if unset).

        Env keys documented in ``.env.example`` (E16 block).  A malformed
        value raises (fail-closed on config).
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
            reject_max_age_seconds=_int(
                "DEV_FUNDING_AGE_REJECT_MAX_SECONDS", cls.reject_max_age_seconds
            ),
            downweight_max_age_seconds=_int(
                "DEV_FUNDING_AGE_DOWNWEIGHT_MAX_SECONDS", cls.downweight_max_age_seconds
            ),
            downweight_multiplier=_dec(
                "DEV_FUNDING_AGE_DOWNWEIGHT_MULTIPLIER", cls.downweight_multiplier
            ),
        )


# ---------------------------------------------------------------------------
# Structural view of the ingestion-side result — decoupled by design (no hard
# import dependency from risk/ onto ingestion/; any object with this shape,
# including aats.ingestion.dev_funding_age.DevFundingAgeResult, works).
# ---------------------------------------------------------------------------


@runtime_checkable
class DevFundingAgeResultLike(Protocol):
    creator_wallet: str
    deploy_event_slot: int
    deploy_block_time_ms: int
    status: object  # FundingAgeStatus — compared by .value == "found"/"undecodable"
    funding_age_slots: int | None
    funding_age_ms: int | None


def _is_found(result: DevFundingAgeResultLike) -> bool:
    status = result.status
    value = getattr(status, "value", status)
    return value == "found"


# ---------------------------------------------------------------------------
# Decision — REJECT / DOWN_WEIGHT / PASS only.  Never sizes / buys.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DevFundingAgeDecision:
    """The E16 verdict.  Structurally incapable of raising conviction/size.

    ``conviction_multiplier`` is hard-clamped to ``[0, 1]`` by the invariants
    below — NOT an ``assert`` (survives ``python -O`` / ``PYTHONOPTIMIZE=1``).
    ``red_flag_hit`` is populated ONLY on REJECT so a pre-gate composition
    (mirroring ``evaluate_with_denylist`` / ``evaluate_with_deployer_
    reputation``) can short-circuit BEFORE the safety gate runs.
    """

    creator_wallet: str
    verdict: DevFundingAgeVerdict
    conviction_multiplier: Decimal
    reason_codes: tuple[str, ...]
    funding_age_slots: int | None
    funding_age_ms: int | None
    deploy_event_slot: int
    event_block_time_ms: int
    red_flag_hit: RedFlagHit | None

    def __post_init__(self) -> None:
        if not (Decimal(0) <= self.conviction_multiplier <= Decimal(1)):
            raise ValueError(
                "DevFundingAgeDecision.conviction_multiplier must be in [0, 1] "
                "(E16 is DE-RISK ONLY; it can never raise conviction)."
            )
        if self.verdict is DevFundingAgeVerdict.REJECT:
            if self.conviction_multiplier != Decimal(0):
                raise ValueError("a REJECT must carry conviction_multiplier == 0")
            if self.red_flag_hit is None:
                raise ValueError("a REJECT must carry a red_flag_hit (pre-gate veto contract)")
            if self.red_flag_hit.flag not in (
                RedFlag.DEV_FUNDED_JUST_BEFORE_LAUNCH,
                RedFlag.DEV_FUNDING_UNDECODABLE,
            ):
                raise ValueError(
                    "a REJECT's red_flag_hit must be DEV_FUNDED_JUST_BEFORE_LAUNCH or "
                    "DEV_FUNDING_UNDECODABLE"
                )
        if self.verdict is DevFundingAgeVerdict.PASS:
            if self.conviction_multiplier != Decimal(1):
                raise ValueError("a PASS must carry conviction_multiplier == 1 (no change)")
            if self.red_flag_hit is not None:
                raise ValueError("a PASS must not carry a red_flag_hit")
        if self.verdict is DevFundingAgeVerdict.DOWN_WEIGHT:
            if not (Decimal(0) <= self.conviction_multiplier < Decimal(1)):
                raise ValueError("a DOWN_WEIGHT must carry conviction_multiplier in [0, 1)")
            if self.red_flag_hit is not None:
                raise ValueError("a DOWN_WEIGHT must not carry a red_flag_hit (it does not reject)")

    @property
    def passed(self) -> bool:
        """True iff NOT rejected.  NOTE: DOWN_WEIGHT is 'passed but de-rated'."""
        return self.verdict is not DevFundingAgeVerdict.REJECT

    @property
    def rejected(self) -> bool:
        return self.verdict is DevFundingAgeVerdict.REJECT

    @property
    def down_weighted(self) -> bool:
        return self.verdict is DevFundingAgeVerdict.DOWN_WEIGHT

    @property
    def refused_undecodable(self) -> bool:
        """True iff the REJECT was caused by an undecodable funding history
        (distinct from a CONFIRMED fresh-funded rug tell — honest tagging)."""
        return (
            self.verdict is DevFundingAgeVerdict.REJECT
            and self.red_flag_hit is not None
            and self.red_flag_hit.flag is RedFlag.DEV_FUNDING_UNDECODABLE
        )

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
class DevFundingAgeTelemetry(Protocol):
    def on_dev_funding_reject(self, creator_wallet: str, funding_age_ms: int | None) -> None: ...

    def on_dev_funding_downweight(
        self, creator_wallet: str, funding_age_ms: int, multiplier: str
    ) -> None: ...


# ---------------------------------------------------------------------------
# The gate.
# ---------------------------------------------------------------------------


@dataclass
class DevFundingAgeGate:
    """Pure threshold evaluator over a ``DevFundingAgeResult``-shaped input.

    ``evaluate(result)`` may ONLY:
      * REJECT — ``status == UNDECODABLE`` (refuse-by-default) OR
        ``funding_age_seconds <= reject_max_age_seconds`` (confirmed fresh
        funding) — a ``RedFlagHit`` usable the same way as the E2 denylist's
        pre-gate veto, or
      * DOWN_WEIGHT — ``reject_max_age_seconds < funding_age_seconds <=
        downweight_max_age_seconds`` — a conviction MULTIPLIER < 1, or
      * PASS — ``funding_age_seconds > downweight_max_age_seconds`` (an
        established wallet) — SILENT, multiplier == 1, never a buy trigger.
    """

    thresholds: DevFundingAgeThresholds = field(default_factory=DevFundingAgeThresholds)
    telemetry: DevFundingAgeTelemetry | None = None

    def evaluate(
        self,
        result: DevFundingAgeResultLike,
        *,
        event_block_time_ms: int | None = None,
    ) -> DevFundingAgeDecision:
        block_time_ms = (
            event_block_time_ms if event_block_time_ms is not None else result.deploy_block_time_ms
        )

        if not _is_found(result):
            # Refuse-by-default: an undecodable funding history is NEVER
            # silently trusted as "established/safe" — instant pre-gate REJECT,
            # tagged DISTINCTLY from a confirmed fresh-funded rug tell.
            decision = DevFundingAgeDecision(
                creator_wallet=result.creator_wallet,
                verdict=DevFundingAgeVerdict.REJECT,
                conviction_multiplier=Decimal(0),
                reason_codes=(DevFundingAgeReason.DEV_FUNDING_UNDECODABLE.value,),
                funding_age_slots=None,
                funding_age_ms=None,
                deploy_event_slot=result.deploy_event_slot,
                event_block_time_ms=block_time_ms,
                red_flag_hit=RedFlagHit(RedFlag.DEV_FUNDING_UNDECODABLE, None, None),
            )
            if self.telemetry is not None:
                self.telemetry.on_dev_funding_reject(result.creator_wallet, None)
            return decision

        funding_age_ms = result.funding_age_ms
        funding_age_slots = result.funding_age_slots
        # Malformed found-result (should be structurally impossible given
        # DevFundingAgeResult's own invariants) — refuse-by-default, never
        # silently trust it.
        if funding_age_ms is None or funding_age_ms < 0:
            raise ValueError(
                f"DevFundingAgeGate: malformed FOUND result for {result.creator_wallet!r} "
                f"(funding_age_ms={funding_age_ms!r})"
            )

        age_seconds = funding_age_ms // 1_000
        reject_threshold_ms = self.thresholds.reject_max_age_seconds * 1_000
        downweight_threshold_ms = self.thresholds.downweight_max_age_seconds * 1_000

        if funding_age_ms <= reject_threshold_ms:
            verdict = DevFundingAgeVerdict.REJECT
            multiplier = Decimal(0)
            reasons: tuple[DevFundingAgeReason, ...] = (
                DevFundingAgeReason.DEV_FUNDED_JUST_BEFORE_LAUNCH,
            )
            red_flag_hit: RedFlagHit | None = RedFlagHit(
                RedFlag.DEV_FUNDED_JUST_BEFORE_LAUNCH,
                int(age_seconds),
                self.thresholds.reject_max_age_seconds,
            )
        elif funding_age_ms <= downweight_threshold_ms:
            verdict = DevFundingAgeVerdict.DOWN_WEIGHT
            multiplier = self.thresholds.downweight_multiplier
            reasons = (DevFundingAgeReason.FRESH_FUNDING_DOWNWEIGHT,)
            red_flag_hit = None
        else:
            verdict = DevFundingAgeVerdict.PASS
            multiplier = Decimal(1)
            reasons = ()
            red_flag_hit = None

        decision = DevFundingAgeDecision(
            creator_wallet=result.creator_wallet,
            verdict=verdict,
            conviction_multiplier=multiplier,
            reason_codes=tuple(r.value for r in reasons),
            funding_age_slots=funding_age_slots,
            funding_age_ms=funding_age_ms,
            deploy_event_slot=result.deploy_event_slot,
            event_block_time_ms=block_time_ms,
            red_flag_hit=red_flag_hit,
        )

        if self.telemetry is not None:
            if decision.rejected:
                self.telemetry.on_dev_funding_reject(result.creator_wallet, funding_age_ms)
            elif decision.down_weighted:
                self.telemetry.on_dev_funding_downweight(
                    result.creator_wallet, funding_age_ms, str(multiplier)
                )

        return decision


# ---------------------------------------------------------------------------
# Composed pre-gate veto — mirrors blacklist.py's evaluate_with_denylist.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DevFundingAgePreGateResult:
    """Result of composing the E16 gate BEFORE a downstream step.

    ``next_result`` is ``None`` iff the E16 gate REJECTED and short-circuited
    BEFORE the downstream step ran — proving the downstream step never
    green-lit a freshly-funded-burner (or undecodable) wallet.
    """

    passed: bool
    decision: DevFundingAgeDecision
    next_result: object | None

    @property
    def rejected_by_funding_age(self) -> bool:
        return self.decision.rejected


def evaluate_with_dev_funding_age(
    *,
    gate: DevFundingAgeGate,
    result: DevFundingAgeResultLike,
    next_step,
    event_block_time_ms: int | None = None,
) -> DevFundingAgePreGateResult:
    """Run the E16 funding-age gate, THEN ``next_step()`` — REJECT-only
    short-circuit.

    ``next_step`` is a zero-arg callable invoked ONLY when the gate does not
    REJECT.  This makes "skip the pre-gate veto because the funding-age
    result looked fine" structurally inexpressible: there is no branch that
    returns ``passed=True`` on a REJECT without ``next_step`` ever running
    (``next_result is None`` proves it never ran).

    A DOWN_WEIGHT verdict does NOT short-circuit — ``next_step`` still runs;
    the caller applies ``decision.conviction_multiplier`` to sizing
    separately (the same non-blocking shape as ``anti_fomo`` / E15).
    """
    decision = gate.evaluate(result, event_block_time_ms=event_block_time_ms)
    if decision.rejected:
        return DevFundingAgePreGateResult(passed=False, decision=decision, next_result=None)

    next_result = next_step()
    passed = bool(getattr(next_result, "passed", True))
    return DevFundingAgePreGateResult(passed=passed, decision=decision, next_result=next_result)
