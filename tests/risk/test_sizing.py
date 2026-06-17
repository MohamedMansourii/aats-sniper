"""T-324 — Fractional-Kelly sizing: <=1/4 Kelly, de-risk-only, hard caps.

Self-check items proven here:
  * Kelly is CAPPED at 1/4 — never full — swept across P 0.1..0.9.
  * Size only SHRINKS under a bearish (de-risk) signal — never grows.
  * Per-trade and aggregate-exposure caps are NEVER breached.
  * Uncertainty WIDENS (shrinks) the size, never tightens it.
  * Money is integer lamports; float money inputs are rejected.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from aats.contracts.risk import RiskConfig
from aats.risk.sizing import (
    DeRiskSignals,
    FractionalKellySizer,
    SizingDecision,
    full_kelly_fraction,
)

PER_TRADE = 100_000_000  # 0.1 SOL
AGGREGATE = 500_000_000  # 0.5 SOL


def _sizer() -> FractionalKellySizer:
    return FractionalKellySizer(RiskConfig())


# --------------------------------------------------------------------------- #
# Kelly is CAPPED at 1/4 — swept across P 0.1..0.9 (self-check item 6).
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "p",
    [Decimal("0.1") * i for i in range(1, 10)],  # 0.1, 0.2, ... 0.9
)
def test_applied_kelly_never_exceeds_quarter_kelly(p: float):
    sizer = _sizer()
    d = sizer.size(
        p_calibrated=float(p),
        uncertainty=0.0,  # no de-risk haircut: pure cap test
        current_aggregate_lamports=0,
    )
    f_full = full_kelly_fraction(float(p))
    # The applied fraction is AT MOST 1/4 of full Kelly (and 0 when no edge).
    if f_full <= 0:
        assert d.applied_kelly_fraction == Decimal(0)
        assert d.size_lamports == 0
        assert d.binding_constraint == "zero_edge"
    else:
        # applied <= 0.25 * full_kelly  (the hard 1/4 cap), strictly never full.
        assert d.applied_kelly_fraction <= Decimal("0.25") * f_full + Decimal("1e-9")
        assert d.applied_kelly_fraction < f_full  # NEVER full Kelly
        assert d.applied_kelly_fraction <= sizer.kelly_fraction_cap


def test_kelly_cap_clamped_even_if_config_tries_to_exceed():
    """RiskConfig already refuses >0.25, but the sizer clamps a SECOND time."""
    # RiskConfig refuses kelly_fraction_cap > 0.25 at construction.
    with pytest.raises(ValueError):
        RiskConfig(kelly_fraction_cap=Decimal("0.50"))
    # And a config AT the cap is honored exactly (never exceeded).
    sizer = FractionalKellySizer(RiskConfig(kelly_fraction_cap=Decimal("0.25")))
    assert sizer.kelly_fraction_cap == Decimal("0.25")
    # A config BELOW the cap tightens further (de-risk) — honored.
    sizer2 = FractionalKellySizer(RiskConfig(kelly_fraction_cap=Decimal("0.10")))
    assert sizer2.kelly_fraction_cap == Decimal("0.10")


def test_negative_edge_yields_zero_size():
    """P below break-even (even-money b=1 → p<=0.5) → no bet."""
    sizer = _sizer()
    for p in (0.1, 0.3, 0.5):
        d = sizer.size(p_calibrated=p, uncertainty=0.0, current_aggregate_lamports=0)
        assert d.size_lamports == 0
        assert d.binding_constraint == "zero_edge"


# --------------------------------------------------------------------------- #
# Size only SHRINKS under a bearish / de-risk signal (self-check item: sizing).
# --------------------------------------------------------------------------- #


def test_bearish_signal_only_shrinks_size():
    sizer = _sizer()
    base = sizer.size(p_calibrated=0.9, uncertainty=0.0, current_aggregate_lamports=0)
    # A bearish sentiment factor < 1.0 must produce a SMALLER (or equal) size.
    shrunk = sizer.size(
        p_calibrated=0.9,
        uncertainty=0.0,
        current_aggregate_lamports=0,
        signals=DeRiskSignals(sentiment_factor=0.5),
    )
    assert shrunk.size_lamports <= base.size_lamports
    assert shrunk.size_lamports < base.size_lamports  # 0.5 strictly shrinks


def test_de_risk_signal_cannot_exceed_one():
    """A 'signal' > 1.0 (a smuggled size-up) is rejected at construction."""
    with pytest.raises(ValueError):
        DeRiskSignals(sentiment_factor=1.5)
    with pytest.raises(ValueError):
        DeRiskSignals(confidence_factor=2.0)
    with pytest.raises(ValueError):
        DeRiskSignals(sentiment_factor=0.0)  # 0 is out of (0,1]


def test_combined_factor_is_monotone_shrink():
    """More de-risk (lower factors) → smaller size, monotonically."""
    sizer = _sizer()
    prev = sizer.size(
        p_calibrated=0.95, uncertainty=0.0, current_aggregate_lamports=0
    ).size_lamports
    for factor in (0.9, 0.7, 0.5, 0.3, 0.1):
        s = sizer.size(
            p_calibrated=0.95,
            uncertainty=0.0,
            current_aggregate_lamports=0,
            signals=DeRiskSignals(sentiment_factor=factor),
        ).size_lamports
        assert s <= prev, f"factor {factor}: size {s} grew above {prev}"
        prev = s


# --------------------------------------------------------------------------- #
# Uncertainty WIDENS (shrinks) size, never tightens it.
# --------------------------------------------------------------------------- #


def test_uncertainty_only_shrinks_size():
    sizer = _sizer()
    prev = sizer.size(
        p_calibrated=0.95, uncertainty=0.0, current_aggregate_lamports=0
    ).size_lamports
    for unc in (0.1, 0.3, 0.5, 0.9):
        s = sizer.size(
            p_calibrated=0.95, uncertainty=unc, current_aggregate_lamports=0
        ).size_lamports
        assert s <= prev, f"uncertainty {unc}: size {s} grew above {prev}"
        prev = s


# --------------------------------------------------------------------------- #
# HARD CAPS — per-trade and aggregate are NEVER breached.
# --------------------------------------------------------------------------- #


def test_per_trade_cap_never_breached():
    sizer = _sizer()
    # Max edge, zero de-risk: size must still be <= per-trade cap.
    d = sizer.size(p_calibrated=1.0, uncertainty=0.0, current_aggregate_lamports=0)
    assert d.size_lamports <= PER_TRADE


def test_aggregate_cap_clamps_to_remaining_headroom():
    sizer = _sizer()
    # At p=1.0 the pre-cap Kelly size is 0.25 * per_trade_cap = 25M lamports.
    # Set the aggregate so the REMAINING headroom (10M) is BELOW that, so the
    # aggregate clamp is the binding constraint.
    already = AGGREGATE - 10_000_000  # 490M at risk → 10M headroom
    d = sizer.size(p_calibrated=1.0, uncertainty=0.0, current_aggregate_lamports=already)
    assert d.size_lamports <= AGGREGATE - already
    assert d.size_lamports == 10_000_000  # clamped to remaining headroom
    assert d.binding_constraint == "aggregate_cap"
    # Sanity: the firm's total open SOL stays at/under the aggregate cap.
    assert already + d.size_lamports <= AGGREGATE


def test_aggregate_full_yields_zero_size():
    sizer = _sizer()
    # Aggregate already AT the cap → no headroom → zero size.
    d = sizer.size(p_calibrated=1.0, uncertainty=0.0, current_aggregate_lamports=AGGREGATE)
    assert d.size_lamports == 0


def test_aggregate_over_cap_yields_zero_never_negative():
    sizer = _sizer()
    d = sizer.size(p_calibrated=1.0, uncertainty=0.0, current_aggregate_lamports=AGGREGATE + 1)
    assert d.size_lamports == 0  # never negative


# --------------------------------------------------------------------------- #
# Money is integer lamports; float money inputs rejected.
# --------------------------------------------------------------------------- #


def test_size_is_always_int():
    sizer = _sizer()
    d = sizer.size(p_calibrated=0.8, uncertainty=0.2, current_aggregate_lamports=0)
    assert isinstance(d.size_lamports, int)
    assert isinstance(d.pre_cap_lamports, int)


def test_float_aggregate_rejected():
    sizer = _sizer()
    with pytest.raises(TypeError):
        sizer.size(p_calibrated=0.8, uncertainty=0.0, current_aggregate_lamports=1.5)


def test_float_net_odds_rejected():
    with pytest.raises(TypeError):
        full_kelly_fraction(0.8, net_odds=2.0)


def test_probability_out_of_range_rejected():
    sizer = _sizer()
    with pytest.raises(ValueError):
        sizer.size(p_calibrated=1.5, uncertainty=0.0, current_aggregate_lamports=0)
    with pytest.raises(ValueError):
        sizer.size(p_calibrated=0.5, uncertainty=2.0, current_aggregate_lamports=0)


def test_sizing_decision_rejects_negative_and_float():
    with pytest.raises(ValueError):
        SizingDecision(
            size_lamports=-1,
            full_kelly_fraction=Decimal(0),
            applied_kelly_fraction=Decimal(0),
            pre_cap_lamports=0,
            binding_constraint="kelly",
        )
    with pytest.raises(TypeError):
        SizingDecision(
            size_lamports=1.0,  # float money
            full_kelly_fraction=Decimal(0),
            applied_kelly_fraction=Decimal(0),
            pre_cap_lamports=0,
            binding_constraint="kelly",
        )
