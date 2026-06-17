"""T-324 PROPERTY / FUZZ — sizing caps are NEVER breached, under any input.

No external fuzzer (hypothesis not vendored); seeded `random` for determinism,
matching the established T-320/T-322/T-323 property-test style.  We fuzz model
probability + uncertainty + de-risk signals + config + current aggregate to
EXTREMES and assert the non-waivable invariants:

  S1  size_lamports <= per_trade_cap                      (per-trade cap, ALWAYS)
  S2  current_aggregate + size_lamports <= max_aggregate  (aggregate cap, ALWAYS)
  S3  applied_kelly_fraction <= 1/4 * full_kelly          (NEVER full Kelly)
  S4  size_lamports is a non-negative int                 (integer money)
  S5  any de-risk signal/uncertainty MONOTONICALLY shrinks (never grows) size
"""

from __future__ import annotations

import random
from decimal import Decimal

from aats.contracts.risk import RiskConfig
from aats.risk.sizing import (
    DeRiskSignals,
    FractionalKellySizer,
    full_kelly_fraction,
)


def test_property_caps_never_breached_under_fuzz():
    rng = random.Random(324)
    n = 5000

    for i in range(n):
        # Config swept within the contract's legal (tighten-only) range.
        per_trade = rng.choice([10_000_000, 50_000_000, 100_000_000])
        aggregate = rng.choice([100_000_000, 300_000_000, 500_000_000])
        kelly_cap = rng.choice([Decimal("0.05"), Decimal("0.10"), Decimal("0.25")])
        cfg = RiskConfig(
            per_trade_cap_lamports=per_trade,
            max_aggregate_lamports=aggregate,
            kelly_fraction_cap=kelly_cap,
        )
        sizer = FractionalKellySizer(cfg)

        p = rng.random()  # 0..1
        unc = rng.random()  # 0..1
        # current aggregate spanning 0 .. above the cap (to exercise the clamp).
        agg = rng.randint(0, aggregate + 50_000_000)
        sentiment = rng.uniform(0.001, 1.0)
        confidence = rng.uniform(0.001, 1.0)

        d = sizer.size(
            p_calibrated=p,
            uncertainty=unc,
            current_aggregate_lamports=agg,
            signals=DeRiskSignals(sentiment_factor=sentiment, confidence_factor=confidence),
        )

        # S1 — per-trade cap never breached.
        assert (
            d.size_lamports <= per_trade
        ), f"i={i}: size {d.size_lamports} > per_trade_cap {per_trade}"
        # S2 — aggregate cap never breached across all open positions.
        assert (
            agg + d.size_lamports <= aggregate or d.size_lamports == 0
        ), f"i={i}: agg {agg} + size {d.size_lamports} > aggregate_cap {aggregate}"
        # Stronger form: when agg <= cap, the sum is bounded; when agg > cap, size==0.
        if agg <= aggregate:
            assert agg + d.size_lamports <= aggregate, f"i={i}: agg+size exceeded aggregate cap"
        else:
            assert d.size_lamports == 0, f"i={i}: over-cap aggregate must give zero size"

        # S3 — never full Kelly: applied <= 1/4 * full (when there is edge).
        f_full = full_kelly_fraction(p)
        if f_full > 0:
            assert d.applied_kelly_fraction <= Decimal("0.25") * f_full + Decimal(
                "1e-9"
            ), f"i={i}: applied {d.applied_kelly_fraction} exceeded 1/4 * full {f_full}"
            assert d.applied_kelly_fraction <= sizer.kelly_fraction_cap + Decimal("1e-12")

        # S4 — integer, non-negative.
        assert (
            isinstance(d.size_lamports, int) and d.size_lamports >= 0
        ), f"i={i}: size not a non-negative int: {d.size_lamports!r}"


def test_property_de_risk_signals_are_monotone_shrink():
    """For ANY base case, every additional de-risk factor only shrinks size."""
    rng = random.Random(3241)
    cfg = RiskConfig()
    sizer = FractionalKellySizer(cfg)

    for _ in range(2000):
        p = rng.uniform(0.55, 1.0)  # ensure positive edge so size > 0
        agg = rng.randint(0, 100_000_000)

        # Two factors f1 >= f2 (f2 is MORE de-risk).  size(f2) <= size(f1).
        f1 = rng.uniform(0.2, 1.0)
        f2 = rng.uniform(0.001, f1)  # f2 <= f1
        s1 = sizer.size(
            p_calibrated=p,
            uncertainty=0.0,
            current_aggregate_lamports=agg,
            signals=DeRiskSignals(sentiment_factor=f1),
        ).size_lamports
        s2 = sizer.size(
            p_calibrated=p,
            uncertainty=0.0,
            current_aggregate_lamports=agg,
            signals=DeRiskSignals(sentiment_factor=f2),
        ).size_lamports
        assert s2 <= s1, f"more de-risk (factor {f2} <= {f1}) grew size: {s2} > {s1}"


def test_property_uncertainty_is_monotone_shrink():
    """Higher uncertainty NEVER grows size — across fuzzed (p, agg)."""
    rng = random.Random(3242)
    sizer = FractionalKellySizer(RiskConfig())

    for _ in range(2000):
        p = rng.uniform(0.55, 1.0)
        agg = rng.randint(0, 100_000_000)
        u1 = rng.uniform(0.0, 0.5)
        u2 = rng.uniform(u1, 1.0)  # u2 >= u1 (MORE uncertainty)
        s1 = sizer.size(
            p_calibrated=p, uncertainty=u1, current_aggregate_lamports=agg
        ).size_lamports
        s2 = sizer.size(
            p_calibrated=p, uncertainty=u2, current_aggregate_lamports=agg
        ).size_lamports
        assert s2 <= s1, f"more uncertainty ({u2} >= {u1}) grew size: {s2} > {s1}"
