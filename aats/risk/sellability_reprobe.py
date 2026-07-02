"""E17 — SLOW-loop delayed-honeypot / tax-flip sellability RE-PROBE (risk-guardrails).

WHAT THIS MODULE DOES
----------------------
A token can PASS the pre-trade sellability gate at entry and then be flipped into
a honeypot AFTER we are already in the position: a freeze-authority abuse, a
transfer-fee/sell-tax spike, an LP pull that drains the exit route, or an
account-close trap.  This module periodically (SLOW / N+2 tier, OFF the FAST/snipe
hot path) RE-RUNS the SHARED sell-sim (``aats.execution.sell_sim.SellSimVenue``)
on OPEN positions.  On a DEGRADED result — sim-revert / zero-output / high
effective sell-tax / account-close trap — OR an INCONCLUSIVE one (refuse-by-default
=> treated as degraded), it PRE-SETS a ``sellability_degraded`` flag in the shared
StateStore using the SAME mechanism ``insider_dump`` already uses
(``StateStore.set_sellability_degraded_flag`` / ``get_sellability_degraded_flag``,
mirrored key-for-key).  The FAST exit branch (``exit_engine.ExitEngine.on_tick``)
reads ONLY that bare bool and force-exits SECURE while an exit is still possible.

REUSE, NEVER FORK (co-ownership with solana-execution-engineer)
---------------------------------------------------------------
The honeypot / sellability MECHANICS live in ONE place: ``aats.execution.sell_sim``.
This module does NOT re-implement any of it — no ``simulateTransaction``, no tax
computation, no quote, no revert-parsing.  It INJECTS a ``SellSimVenue`` and calls
``simulate_sell(...)`` (or ``is_sellable(...)``); it only CLASSIFIES the returned
``SellSimResult`` into a de-risk decision.  Grep-provable: this file defines no
simulate/quote/tax primitive.

HARD RULES (non-waivable, enforced by construction / asserted in tests)
-------------------------------------------------------------------------
  * DE-RISK ONLY.  This module can only ever SET the flag (a signal that biases an
    exit).  There is no method here that clears a flag, sizes a trade, widens a
    stop, holds longer, or adds leverage.  The FAST exit branch consumes the flag
    as a PRE-SET boolean (E17) — this module never touches the FAST/snipe hot path.
  * REFUSE-BY-DEFAULT / FAIL-CLOSED TOWARD EXIT.  An inconclusive re-probe (the
    shared sell-sim raised, or returned a contradictory result) is treated as
    DEGRADED, never as healthy.  ONLY a shared-sell-sim result that CONFIRMED
    sellability (``sellable is True and reason is SELLABLE``) is HEALTHY; every
    other outcome sets the flag.  The shared sell-sim is itself refuse-by-default
    (any failure => ``sellable=False``); this module adds a second refuse layer
    around the CALL itself.
  * DRY-RUN BY CONSTRUCTION.  The injected ``SellSimVenue`` is DRY-RUN
    (``submit_mode == SubmitMode.DRY_RUN``): it simulates, it never submits.  This
    module has no submit/send path of its own.  A real submit from here would be a
    bug of the highest order.
  * OFF THE HOT PATH.  This is a SLOW / N+2-tier producer (same posture as
    ``insider_dump.py``): it does its work off the FAST/snipe path and writes a
    flag the FAST loop reads as a plain bool.  It performs no signing, no
    submission, no LLM call.
  * POINT-IN-TIME / EVENT-TIME ONLY (T-300a).  The re-probe is attributed to an
    on-chain ``event_slot`` supplied by the SLOW-loop driver (event-time).  Nothing
    here reads a wall-clock into a stored/emitted decision field; there is no
    wall-clock in any decision.

WHAT THIS MODULE DOES NOT DO (owner boundaries)
------------------------------------------------
  * It does not decide WHEN a position is open — the caller passes
    ``is_open_position`` (typically ``store.load_position(mint) is not None``), or
    uses the ``reprobe_open_positions`` batch driver which iterates
    ``store.load_all_positions()``.
  * It does not build the FAST-loop read path — that is the ``ExitEngine`` branch
    (``sellability_degraded_flag``) plus the one-line StateStore read in
    ``fast_loop.py``.
  * It does not touch ``aats/contracts/`` — no frozen contract is edited.  The
    ``sellability_degraded`` flag is added via the existing StateStore mechanism.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from aats.execution.sell_sim import SellSimReason, SellSimResult, SellSimVenue

logger = logging.getLogger(__name__)

# Mirrors set_insider_dump_flag's default TTL (120s) — same presence semantics.
DEFAULT_FLAG_TTL_SECONDS = 120


# ---------------------------------------------------------------------------
# Type guards (money/identity rule; mirrors insider_dump.py's boundary checks)
# ---------------------------------------------------------------------------


def _check_int(name: str, value: object, *, min_value: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be int, not {type(value).__name__}={value!r}.")
    if min_value is not None and value < min_value:
        raise ValueError(f"{name} must be >= {min_value}, got {value}.")
    return value


def _check_str(name: str, value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty str, got {value!r}.")
    return value


# ---------------------------------------------------------------------------
# Re-probe classification — every non-HEALTHY value is DEGRADED (sets the flag)
# ---------------------------------------------------------------------------


class SellabilityReprobeReason(StrEnum):
    """Classification of a single re-probe.  HEALTHY is the ONLY non-degrading
    outcome; everything else raises the ``sellability_degraded`` flag.

    The four DEGRADED_* codes below name the concrete delayed-honeypot / tax-flip
    fingerprints the shared sell-sim reports.  DEGRADED_OTHER catches every OTHER
    conclusively-unsellable shared-sell-sim reason (signer-refused / quote-failed /
    infra-error / not-simulated) — still de-risk.  INCONCLUSIVE is the
    refuse-by-default layer around the CALL: the shared sell-sim raised or returned
    a contradictory result, and we fail closed toward exit.
    """

    HEALTHY = "healthy"  # shared sell-sim CONFIRMED sellability — no flag
    DEGRADED_SIM_REVERT = "degraded_sim_revert"  # simulateTransaction reverted
    DEGRADED_ZERO_OUTPUT = "degraded_zero_output"  # dust sell returned 0 out
    DEGRADED_HIGH_SELL_TAX = "degraded_high_sell_tax"  # effective sell-tax > ceiling
    DEGRADED_ACCOUNT_CLOSE_TRAP = "degraded_account_close_trap"  # account-close reverted
    DEGRADED_OTHER = "degraded_other"  # any other conclusively-unsellable result
    INCONCLUSIVE = "inconclusive"  # probe raised / contradictory — refuse => degraded

    @property
    def is_degraded(self) -> bool:
        """True for every outcome except HEALTHY — i.e. every outcome that must
        set the flag.  Refuse-by-default: INCONCLUSIVE is degraded."""
        return self is not SellabilityReprobeReason.HEALTHY


# The concrete degraded fingerprints the shared sell-sim can report, mapped to a
# specific classification.  A shared-sell-sim reason NOT in this map that is still
# not-sellable falls through to DEGRADED_OTHER (see ``_classify``).
_DEGRADED_REASON_MAP: dict[SellSimReason, SellabilityReprobeReason] = {
    SellSimReason.SIM_REVERT: SellabilityReprobeReason.DEGRADED_SIM_REVERT,
    SellSimReason.ZERO_OUTPUT: SellabilityReprobeReason.DEGRADED_ZERO_OUTPUT,
    SellSimReason.HIGH_SELL_TAX: SellabilityReprobeReason.DEGRADED_HIGH_SELL_TAX,
    SellSimReason.ACCOUNT_CLOSE_TRAP: SellabilityReprobeReason.DEGRADED_ACCOUNT_CLOSE_TRAP,
}


def _classify(result: SellSimResult) -> SellabilityReprobeReason:
    """Classify a shared-sell-sim ``SellSimResult`` into a re-probe outcome.

    REFUSE-BY-DEFAULT: HEALTHY is returned ONLY when the shared sell-sim CONFIRMED
    sellability (``sellable is True`` AND ``reason is SELLABLE``).  A not-sellable
    result maps to its specific degraded fingerprint (or DEGRADED_OTHER).  A
    contradictory result (``sellable is True`` but ``reason`` is not SELLABLE) is
    treated as INCONCLUSIVE — we never trust a self-inconsistent "sellable".
    """
    if result.sellable is not True:
        # Conclusively unsellable — specific fingerprint if known, else other.
        return _DEGRADED_REASON_MAP.get(result.reason, SellabilityReprobeReason.DEGRADED_OTHER)
    # sellable is True — only trust it if the reason agrees (SELLABLE).
    if result.reason is SellSimReason.SELLABLE:
        return SellabilityReprobeReason.HEALTHY
    return SellabilityReprobeReason.INCONCLUSIVE


# ---------------------------------------------------------------------------
# Result / stats
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SellabilityReprobeResult:
    """The outcome of ONE re-probe on ONE open position.

    ``degraded`` is the de-risk verdict (True => the ``sellability_degraded`` flag
    was raised for the FAST exit branch).  ``flag_set`` records whether the flag was
    actually written this call (False when no ``flag_sink`` is wired, e.g. a dry
    classification).  ``event_slot`` is the on-chain slot this re-probe is attributed
    to (event-time).  ``sell_sim_reason`` is the underlying shared-sell-sim reason
    (audit only; None when the probe raised).
    """

    mint: str
    event_slot: int
    reason: SellabilityReprobeReason
    degraded: bool
    flag_set: bool
    sell_sim_reason: str | None = None
    detail: str = ""


@dataclass
class SellabilityReprobeStats:
    """Runtime counters (observability — proves "refuse" is never silent).

    ``probe_exceptions`` is load-bearing: it PROVES a raised shared-sell-sim call
    was counted distinctly and failed CLOSED (INCONCLUSIVE => degraded), never
    swallowed and treated as healthy.
    """

    reprobes_run: int = 0
    not_open_skipped: int = 0
    degraded: int = 0
    healthy: int = 0
    probe_exceptions: int = 0
    flags_set: int = 0
    last_event_slot: int = 0


# ---------------------------------------------------------------------------
# Structural sinks (no risk->controller import dependency)
# ---------------------------------------------------------------------------


class SellabilityDegradedFlagSink(Protocol):
    """The minimal write surface this re-probe needs from the shared StateStore.

    Structural (duck-typed): ``aats.controller.state.StateStore`` /
    ``InMemoryStateStore`` satisfies this WITHOUT importing the controller package.
    """

    def set_sellability_degraded_flag(
        self,
        mint: str,
        *,
        reason: str,
        event_slot: int,
        ttl_seconds: int = 120,
    ) -> None: ...  # pragma: no cover


class OpenPositionSource(Protocol):
    """The minimal read surface the batch driver needs (structural).

    Satisfied by ``StateStore.load_all_positions()``.  Each element only needs a
    ``.mint`` attribute.
    """

    def load_all_positions(self) -> list: ...  # pragma: no cover


class _SellSimLike(Protocol):
    """The minimal read surface of the SHARED sell-sim this module consumes.

    Satisfied by ``aats.execution.sell_sim.SellSimVenue``.  We depend ONLY on
    ``simulate_sell`` (the shared mechanics) — we do not re-implement it.
    """

    def simulate_sell(self, *, mint: str, event_slot: int) -> SellSimResult: ...  # pragma: no cover


# ---------------------------------------------------------------------------
# SellabilityReprober — the SLOW-loop re-probe runner
# ---------------------------------------------------------------------------


class SellabilityReprober:
    """Stateless-per-call, deterministic, de-risk-only sellability re-probe.

    USAGE (off the hot path — the SLOW-loop / enrichment runner drives this):

        from aats.execution.sell_sim import make_sell_sim_probe
        reprober = SellabilityReprober(
            probe=make_sell_sim_probe(rpc_client=..., signer_client=...),
            flag_sink=state_store,   # satisfies SellabilityDegradedFlagSink
        )
        # periodically, with the current on-chain slot (event-time):
        results = reprober.reprobe_open_positions(state_store, event_slot=current_slot)
        # each degraded/inconclusive result has ALREADY pre-set the flag the FAST
        # exit branch reads via StateStore.get_sellability_degraded_flag(mint).

    De-risk-only: ``reprobe`` can only ever SET the flag (via ``flag_sink``); there
    is no method here that clears it, sizes a trade, widens a stop, or holds longer.
    DRY-RUN by construction: it only ever calls the injected sell-sim's
    ``simulate_sell`` — it has no submit/send path.
    """

    def __init__(
        self,
        *,
        probe: SellSimVenue | _SellSimLike,
        flag_sink: SellabilityDegradedFlagSink | None = None,
        flag_ttl_seconds: int = DEFAULT_FLAG_TTL_SECONDS,
    ) -> None:
        self._probe = probe
        self._flag_sink = flag_sink
        self._flag_ttl = _check_int(
            "SellabilityReprober.flag_ttl_seconds", flag_ttl_seconds, min_value=1
        )
        self.stats = SellabilityReprobeStats()

    # -- the SLOW-loop re-probe (off the FAST/snipe path) ----------------- #

    def reprobe(
        self,
        mint: str,
        *,
        event_slot: int,
        is_open_position: bool,
    ) -> SellabilityReprobeResult | None:
        """Re-probe ONE mint.  Returns the classification (and, for a degraded/
        inconclusive result, sets the flag via ``flag_sink``); returns None when the
        position is not open (nothing to protect — no flag).

        Off the hot path: this calls the SHARED sell-sim (a ~30-100ms simulate),
        never the FAST/snipe loop.  Event-time: ``event_slot`` is the on-chain slot
        the SLOW-loop driver attributes this re-probe to — no wall-clock is read.
        """
        _check_str("reprobe.mint", mint)
        event_slot = _check_int("reprobe.event_slot", event_slot, min_value=0)
        if not isinstance(is_open_position, bool):
            raise TypeError(
                "reprobe.is_open_position must be a plain bool, "
                f"not {type(is_open_position).__name__}."
            )

        if not is_open_position:
            self.stats.not_open_skipped += 1
            return None

        self.stats.reprobes_run += 1
        self.stats.last_event_slot = max(self.stats.last_event_slot, event_slot)

        reason, sell_sim_reason, detail = self._run_probe(mint, event_slot)
        degraded = reason.is_degraded
        flag_set = False

        if degraded:
            self.stats.degraded += 1
            if self._flag_sink is not None:
                self._flag_sink.set_sellability_degraded_flag(
                    mint,
                    reason=reason.value,
                    event_slot=event_slot,
                    ttl_seconds=self._flag_ttl,
                )
                flag_set = True
                self.stats.flags_set += 1
            logger.warning(
                "SellabilityReprober: DEGRADED mint=%s reason=%s sell_sim_reason=%s "
                "event_slot=%d — sellability_degraded flag SET=%s (de-risk force-exit).",
                mint,
                reason.value,
                sell_sim_reason,
                event_slot,
                flag_set,
            )
        else:
            self.stats.healthy += 1
            logger.debug(
                "SellabilityReprober: HEALTHY mint=%s event_slot=%d — no flag.",
                mint,
                event_slot,
            )

        return SellabilityReprobeResult(
            mint=mint,
            event_slot=event_slot,
            reason=reason,
            degraded=degraded,
            flag_set=flag_set,
            sell_sim_reason=sell_sim_reason,
            detail=detail,
        )

    def reprobe_open_positions(
        self,
        store: OpenPositionSource,
        *,
        event_slot: int,
    ) -> list[SellabilityReprobeResult]:
        """Batch driver: re-probe EVERY open position in ``store`` at the current
        on-chain ``event_slot`` (event-time).  Returns one result per position;
        degraded/inconclusive results have already pre-set their flag.
        """
        event_slot = _check_int("reprobe_open_positions.event_slot", event_slot, min_value=0)
        results: list[SellabilityReprobeResult] = []
        for pos in store.load_all_positions():
            result = self.reprobe(pos.mint, event_slot=event_slot, is_open_position=True)
            if result is not None:
                results.append(result)
        return results

    # -- internal: REFUSE-BY-DEFAULT wrapper around the SHARED sell-sim --- #

    def _run_probe(
        self, mint: str, event_slot: int
    ) -> tuple[SellabilityReprobeReason, str | None, str]:
        """Call the SHARED sell-sim and classify the result.

        REFUSE-BY-DEFAULT: if the shared sell-sim raises (the probe could not
        CONCLUDE), fail CLOSED — return INCONCLUSIVE, which ``is_degraded`` counts
        as degrading.  We NEVER treat an unconcluded probe as healthy.
        """
        try:
            result = self._probe.simulate_sell(mint=mint, event_slot=event_slot)
        except Exception as exc:  # noqa: BLE001 — any failure fails closed to degraded
            self.stats.probe_exceptions += 1
            logger.warning(
                "SellabilityReprober: shared sell-sim RAISED for mint=%s "
                "(%s) -> INCONCLUSIVE (refuse-by-default => degraded).",
                mint,
                type(exc).__name__,
            )
            return (
                SellabilityReprobeReason.INCONCLUSIVE,
                None,
                f"probe_raised:{type(exc).__name__}",
            )
        reason = _classify(result)
        return reason, result.reason.value, result.detail
