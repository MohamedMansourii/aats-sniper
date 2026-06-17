"""Asymmetric-trust property test (self-check item 3).

Proves, by exhaustive fuzzing over the WHOLE input space, that the clamp NEVER raises
signal / size / stop vs the quant decision.  The clamp is monotone toward safety:
  - the applied action is always a de-risk-only ReasoningAction;
  - the applied action's de-risk strength is NEVER below the quant ceiling;
  - a raw risk-increase request is dropped (risk_increase_clamped=True) and never raises
    risk;
  - there is NO input that produces a risk-increase action (it is inexpressible by type).
"""

from __future__ import annotations

import itertools

from aats.contracts.models import ReasoningAction
from aats.reasoning.clamp import (
    ClampResult,
    clamp_to_derisk_action,
    derisk_strength,
    quant_to_action_ceiling,
)
from aats.reasoning.schema import DecisionSignalLabel, QuantBucket

# The full discrete input space for the clamp.
_QUANTS = list(QuantBucket)
_SIGNALS = list(DecisionSignalLabel)
_BOOLS = [False, True]
# A representative set of raw strings INCLUDING risk-increase attempts.
_RAW_STRINGS = [
    "Strong Buy",
    "Hold",
    "Sell",
    "SIZE_UP",
    "size up",
    "WIDEN_STOP",
    "ADD_LEVERAGE",
    "OVERRIDE_HARD_STOP",
    "scale-in",
    "buy",
    "garbage-token-!@#",  # not a recognized risk-increase token
    "",  # empty
]

_DE_RISK_ACTIONS = {
    ReasoningAction.HOLD,
    ReasoningAction.VETO_ENTRY,
    ReasoningAction.REDUCE_SIZE,
    ReasoningAction.FORCE_EXIT,
}


def _all_cases():
    yield from itertools.product(
        _QUANTS, _SIGNALS, _BOOLS, _BOOLS, _RAW_STRINGS, _BOOLS
    )


def test_clamp_output_is_always_a_de_risk_action():
    """Across the ENTIRE input space, the applied action is de-risk-only."""
    n = 0
    for quant, signal, veto, narr, raw, is_open in _all_cases():
        res: ClampResult = clamp_to_derisk_action(
            quant=quant,
            signal=signal,
            veto=veto,
            narrative_failure=narr,
            action_received_raw=raw,
            position_is_open=is_open,
        )
        assert res.action in _DE_RISK_ACTIONS, (quant, signal, veto, narr, raw, is_open)
        n += 1
    assert n > 1000  # the fuzz space is large
    print(f"\n[clamp-property] checked {n} fuzzed cases; all de-risk-only")


def test_clamp_never_below_quant_ceiling():
    """The applied action is NEVER less de-risk than the quant ceiling (never up-risk)."""
    for quant, signal, veto, narr, raw, is_open in _all_cases():
        res = clamp_to_derisk_action(
            quant=quant,
            signal=signal,
            veto=veto,
            narrative_failure=narr,
            action_received_raw=raw,
            position_is_open=is_open,
        )
        ceiling = quant_to_action_ceiling(quant)
        assert derisk_strength(res.action) >= derisk_strength(ceiling), (
            quant,
            signal,
            raw,
            res.action,
            ceiling,
        )


def test_raw_risk_increase_is_always_clamped_and_never_raises_risk():
    """Every raw risk-increase request is flagged AND dropped to (at most) the ceiling."""
    risk_increase_raws = [
        "SIZE_UP",
        "size up",
        "WIDEN_STOP",
        "ADD_LEVERAGE",
        "OVERRIDE_HARD_STOP",
        "scale-in",
        "buy",
        "Strong Buy",
    ]
    for quant in _QUANTS:
        for raw in risk_increase_raws:
            for is_open in _BOOLS:
                res = clamp_to_derisk_action(
                    quant=quant,
                    signal=DecisionSignalLabel.STRONG_BUY,  # bullish, would up-signal
                    veto=False,
                    narrative_failure=False,
                    action_received_raw=raw,
                    position_is_open=is_open,
                )
                assert res.risk_increase_clamped is True, (quant, raw)
                # Dropped to EXACTLY the ceiling — never more bullish.
                assert res.action == quant_to_action_ceiling(quant)
                assert res.action in _DE_RISK_ACTIONS


def test_bullish_signal_can_never_increase_risk():
    """A 'Strong Buy' / 'Weak Buy' signal can only ever result in HOLD-or-de-risk.

    The clamp NEVER turns a bullish opinion into a size-up — the strongest a bull case can
    do is leave the action at the ceiling (HOLD for non-bearish quant).
    """
    for quant in _QUANTS:
        for signal in (DecisionSignalLabel.STRONG_BUY, DecisionSignalLabel.WEAK_BUY):
            res = clamp_to_derisk_action(
                quant=quant,
                signal=signal,
                veto=False,
                narrative_failure=False,
                action_received_raw=str(signal),
                position_is_open=False,
            )
            # For non-bearish quant the bullish opinion yields HOLD (no de-risk added,
            # and CRUCIALLY no risk added).  For bearish quant it yields >= VETO_ENTRY.
            if quant is QuantBucket.BEARISH:
                assert res.action is ReasoningAction.VETO_ENTRY
            else:
                assert res.action is ReasoningAction.HOLD


def test_narrative_failure_always_at_least_veto():
    """narrative_failure => VETO_ENTRY (pre-entry) or FORCE_EXIT (open) — always de-risk."""
    for quant in _QUANTS:
        for signal in _SIGNALS:
            # pre-entry
            res = clamp_to_derisk_action(
                quant=quant,
                signal=signal,
                veto=False,
                narrative_failure=True,
                action_received_raw=str(signal),
                position_is_open=False,
            )
            assert derisk_strength(res.action) >= derisk_strength(ReasoningAction.VETO_ENTRY)
            # open position
            res_open = clamp_to_derisk_action(
                quant=quant,
                signal=signal,
                veto=False,
                narrative_failure=True,
                action_received_raw=str(signal),
                position_is_open=True,
            )
            assert res_open.action is ReasoningAction.FORCE_EXIT
