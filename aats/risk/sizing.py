"""T-324 — Fractional-Kelly position sizing with HARD exposure caps.

WHAT THIS MODULE IS
-------------------
The single chokepoint that turns a calibrated model probability + uncertainty
into a per-trade SOL size, in integer lamports.  It is the only place a size is
ever computed for an EntryIntent, and EVERY input — model, LLM, config
hot-reload, copy-trade signal — may only SHRINK that size, NEVER grow it.

NON-WAIVABLE RULES (encoded as math/asserts, not comments)
----------------------------------------------------------
  * FRACTIONAL Kelly, NEVER full.  The bet fraction applied is
        f = min(config.kelly_fraction_cap, 0.25) * kelly_fraction
    The 0.25 (1/4) cap is hard-clamped in code regardless of config (FR-032).
    A config that tries to set kelly_fraction_cap > 0.25 is already refused at
    RiskConfig construction (contracts/risk.py); here we clamp AGAIN, so even a
    forged/duck-typed config cannot push the multiplier past 1/4 Kelly.
  * Uncertainty WIDENS (shrinks size), NEVER tightens it.  Higher predictive
    uncertainty multiplies the size DOWN by a factor in (0, 1].  It can never
    multiply up.  (data-models.md §4; FR-014/032.)
  * Adversarial sentiment is CONTRARIAN.  A coordinated-shill / low-account-age
    conviction signal LOWERS size (a multiplier in (0,1]); narrative hype is a
    risk signal, never a size-up (BUILD-DIRECTIVE adversarial-sentiment rule;
    AC-021 monotone non-increasing).
  * HARD CAPS bound the worst case BEFORE it happens (blast-radius thinking):
      - per-trade cap            (RiskConfig.per_trade_cap_lamports, 0.1 SOL floor)
      - max-aggregate exposure   (RiskConfig.max_aggregate_lamports, 0.5 SOL floor)
        across ALL open positions — the new size is clamped to the REMAINING
        aggregate headroom, so the firm's total open SOL can never exceed the cap.
    No input can push a size above EITHER cap.  Caps are tighten-only.
  * Money is integer lamports / Decimal.  NEVER float.  Probabilities and
    uncertainties are float (they are statistics, not money) — but every lamport
    quantity that leaves this module is an int.

POINT-IN-TIME
-------------
Sizing is a pure function of (probability, uncertainty, signals, caps, current
aggregate exposure).  It reads no wall-clock and no future data.  The same
function runs live and in backtest.

THE KELLY MATH (binary outcome, the only form we use)
-----------------------------------------------------
For a binary bet that pays net odds `b` on a win with win-prob `p`:
    full_kelly_fraction = (b * p - (1 - p)) / b   =   p - (1 - p)/b
We then apply ONLY a fraction of that (<= 1/4), clamp negative Kelly to 0 (no
bet when edge is negative), and multiply by the de-risk signal factors.  The
result is a fraction of the per-trade cap (the per-trade cap is the unit of
risk), then clamped by both caps.  We never size off the full bankroll directly
— the per-trade cap IS the bankroll slice exposed to one trade.
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_HALF_EVEN, Context, Decimal, localcontext

from aats.contracts.risk import RiskConfig

LAMPORTS_PER_SOL = 1_000_000_000

# --------------------------------------------------------------------------- #
# HERMETIC DECIMAL CONTEXT (T-326 re-spin — true flake fix).
#
# Every Decimal operation in this module reads the PROCESS-GLOBAL decimal context
# (precision / rounding / traps) unless we pin one explicitly.  That global is
# shared mutable state: ANY code anywhere in the process (a sibling, a dependency,
# a future feature) that mutates `decimal.getcontext()` would silently perturb the
# lamport size this module computes — by a lamport (enough to flip a boundary cap)
# or, under a hostile low-precision rounding-UP context, enough to round the
# applied Kelly fraction PAST the 1/4 cap and trip this module's own invariant
# assert.  A risk sizer whose output depends on ambient interpreter state is not
# point-in-time correct: the SAME inputs must yield the SAME lamports live, in
# backtest, and in test, regardless of what context happens to be installed.
#
# We therefore compute EVERY size inside a FIXED local context — the canonical
# 28-digit ROUND_HALF_EVEN the math is designed against — so the sizing path is
# fully hermetic w.r.t. the global context.  This is a tighten/de-risk-only change:
# it removes a non-determinism, changes no cap, weakens no invariant.  The hard
# integer clamps (per-trade, aggregate) are unaffected (they are pure int math);
# pinning the context guarantees the *fractional* steps that FEED those clamps are
# reproducible too.  prec=28 is ample headroom for lamport-scale quantities
# (max ~1.8e19 < 28 digits), so the result is exact for every realistic input.
# --------------------------------------------------------------------------- #

_SIZING_DECIMAL_CONTEXT = Context(prec=28, rounding=ROUND_HALF_EVEN)


def _hermetic_decimal_ctx() -> AbstractContextManager[Context]:
    """Pin the canonical sizing decimal context for the duration of a `with` block.

    Returns a fresh `localcontext` bound to a COPY of the fixed sizing context, so
    the size math runs under prec=28 / ROUND_HALF_EVEN no matter what global
    context is installed — and restores the caller's context on exit.  Pure
    isolation: it does not (and cannot) change any cap or invariant."""
    return localcontext(_SIZING_DECIMAL_CONTEXT.copy())


# The hard 1/4-Kelly ceiling, in code, independent of any config (FR-032).
# Even if a forged config claims kelly_fraction_cap > 0.25, this clamps it.
_HARD_KELLY_CAP = Decimal("0.25")

# Default net odds `b` for the binary Kelly form when a surface does not supply
# a measured payoff.  Conservative: b=1 (even-money) makes Kelly = 2p - 1, i.e.
# it demands p > 0.5 for ANY positive size.  A surface MAY pass a measured `b`;
# it can only be a de-risk to assume a lower-than-true payoff, never the reverse,
# so we keep the default low.
_DEFAULT_NET_ODDS = Decimal("1")


def _reject_float_money(value: object, field: str) -> None:
    if isinstance(value, float):
        raise TypeError(
            f"{field} must be int lamports (not float) — money is integer "
            f"base units (data-models.md §0); got {value!r}."
        )


def _clamp_unit(x: float, field: str) -> Decimal:
    """Coerce a probability/uncertainty/signal in [0,1] to Decimal, range-checked.

    These are statistics (float on the wire), but we carry them as Decimal INSIDE
    the size math so the lamport result is exact and reproducible — float
    accumulation in the size path is forbidden (data-models.md §0).
    """
    if isinstance(x, bool):  # bool is an int subclass; reject it explicitly
        raise TypeError(f"{field} must be a real number in [0,1], got bool {x!r}.")
    if not (0.0 <= x <= 1.0):
        raise ValueError(f"{field} must be in [0, 1], got {x}.")
    # str(float) is the shortest round-trip repr; Decimal(str(x)) avoids binary
    # float artifacts while staying deterministic.
    return Decimal(str(x))


@dataclass(frozen=True)
class DeRiskSignals:
    """Secondary, DE-RISK-ONLY multipliers on the Kelly size.

    EVERY field is a factor in (0, 1]:  1.0 = no de-risk, < 1.0 = shrink.  There
    is NO field that can be > 1.0 — by construction this struct can only SHRINK a
    size, never grow it (AC-021 monotone non-increasing).  A value outside (0,1]
    is rejected at construction, so a caller cannot smuggle a size-up through a
    "signal".

    sentiment_factor:  conviction/sentiment de-risk.  Coordinated shilling / low
        account age must map to a LOWER factor (contrarian).  The NLP/LLM slow
        loop computes it; this module only consumes it as a shrink.
    confidence_factor: any additional de-risk multiplier (e.g. a reasoner
        REDUCE_SIZE verdict, a stale-data haircut).  De-risk only.
    """

    sentiment_factor: float = 1.0
    confidence_factor: float = 1.0

    def __post_init__(self) -> None:
        for name in ("sentiment_factor", "confidence_factor"):
            v = getattr(self, name)
            if isinstance(v, bool):
                raise TypeError(f"DeRiskSignals.{name} must be a float in (0,1], got bool.")
            if not (0.0 < float(v) <= 1.0):
                raise ValueError(
                    f"DeRiskSignals.{name} must be in (0, 1] — a de-risk signal may ONLY "
                    f"shrink size, never grow it (AC-021); got {v}."
                )

    def combined_factor(self) -> Decimal:
        """The product of all de-risk factors, in (0, 1].  Always <= 1."""
        f = _clamp_unit(self.sentiment_factor, "sentiment_factor") * _clamp_unit(
            self.confidence_factor, "confidence_factor"
        )
        # Product of two values each in (0,1] is in (0,1]; assert the invariant.
        assert f <= Decimal(1), "combined de-risk factor exceeded 1 — invariant violated"
        return f


@dataclass(frozen=True)
class SizingDecision:
    """The result of a sizing computation — fully auditable, integer lamports.

    `size_lamports` is the FINAL size after caps; it is what an EntryIntent may
    use (and only if the cost gate also passes).  The intermediate fields are
    carried for telemetry/audit so a reviewer can see exactly which constraint
    was binding.
    """

    size_lamports: int  # FINAL, post-cap, integer lamports
    full_kelly_fraction: Decimal  # the raw Kelly fraction (may be 0 or negative→0)
    applied_kelly_fraction: Decimal  # after the <=1/4 cap + de-risk factors
    pre_cap_lamports: int  # size before the per-trade / aggregate caps
    binding_constraint: str  # "kelly" | "per_trade_cap" | "aggregate_cap" | "zero_edge"

    def __post_init__(self) -> None:
        _reject_float_money(self.size_lamports, "SizingDecision.size_lamports")
        _reject_float_money(self.pre_cap_lamports, "SizingDecision.pre_cap_lamports")
        if self.size_lamports < 0:
            raise ValueError("SizingDecision.size_lamports must be >= 0 (no negative size).")


def full_kelly_fraction(
    p_calibrated: float, net_odds: Decimal | int | str = _DEFAULT_NET_ODDS
) -> Decimal:
    """The FULL-Kelly bet fraction for a binary outcome — for reference/clamping.

    f* = p - (1 - p)/b.  Negative (no edge) is clamped to 0 by the caller (we
    return the raw value here so a test can see the sign).  This is NEVER applied
    directly — the only fraction that leaves this module is <= 1/4 of it.
    """
    p = _clamp_unit(p_calibrated, "p_calibrated")
    if isinstance(net_odds, float):
        raise TypeError("net_odds must be Decimal/int/str (exact), not float (data-models.md §0).")
    b = Decimal(net_odds) if not isinstance(net_odds, Decimal) else net_odds
    if b <= 0:
        raise ValueError(f"net_odds must be > 0, got {b}.")
    return p - (Decimal(1) - p) / b


class FractionalKellySizer:
    """Compute a per-trade size: <= 1/4 Kelly, de-risk-shrunk, hard-capped.

    The sizer holds the RiskConfig (caps + the kelly_fraction_cap).  It exposes
    ONE method, `size()`, which is a pure function of its inputs.  There is no
    method on this class that can raise a size above the caps — the caps are
    applied unconditionally at the end, after every other factor.
    """

    def __init__(self, config: RiskConfig) -> None:
        self._config = config
        # The applied Kelly fraction cap is min(config, hard 1/4).  Even a forged
        # config that somehow carried > 0.25 (RiskConfig already refuses it) is
        # clamped here, so 1/4 Kelly is enforced in TWO independent places.
        cfg_cap = config.kelly_fraction_cap
        if not isinstance(cfg_cap, Decimal):
            cfg_cap = Decimal(str(cfg_cap))
        self._kelly_cap = min(cfg_cap, _HARD_KELLY_CAP)

    @property
    def kelly_fraction_cap(self) -> Decimal:
        return self._kelly_cap

    def size(
        self,
        *,
        p_calibrated: float,
        uncertainty: float,
        current_aggregate_lamports: int,
        signals: DeRiskSignals | None = None,
        net_odds: Decimal | int | str = _DEFAULT_NET_ODDS,
    ) -> SizingDecision:
        """Return the FINAL per-trade size in integer lamports.

        Args:
            p_calibrated: the model's calibrated win probability in [0,1].
            uncertainty: predictive uncertainty in [0,1] — HIGHER shrinks size.
            current_aggregate_lamports: SOL (lamports) already at risk across all
                open positions.  The new size is clamped to the REMAINING
                aggregate headroom so the firm's total exposure cannot exceed the
                aggregate cap (blast-radius).
            signals: optional de-risk multipliers (sentiment/confidence), each in
                (0,1].  Default = no de-risk.
            net_odds: binary payoff `b`; default conservative (even money).

        Returns:
            A SizingDecision whose `size_lamports` is the FINAL, post-cap size.

        The math, in order (every step can only SHRINK):
            1. full Kelly f* = p - (1-p)/b      (negative → 0, no edge no bet)
            2. apply the <=1/4 cap:  f = min(cap, 0.25) * f*
            3. uncertainty haircut:  f *= (1 - uncertainty)   ∈ (0,1] shrink
            4. de-risk signals:      f *= combined_factor      ∈ (0,1] shrink
            5. size = floor(f * per_trade_cap)                 (per-trade is the unit)
            6. clamp to per_trade_cap                          (hard ceiling)
            7. clamp to remaining aggregate headroom           (hard ceiling)
        """
        _reject_float_money(current_aggregate_lamports, "current_aggregate_lamports")
        if current_aggregate_lamports < 0:
            raise ValueError("current_aggregate_lamports must be >= 0.")
        sig = signals or DeRiskSignals()

        # ALL Decimal arithmetic below runs inside the FIXED sizing context
        # (prec=28, ROUND_HALF_EVEN) so the lamport result is hermetic w.r.t. the
        # process-global decimal context — identical live, in backtest, and in test,
        # immune to any ambient context another part of the process may have left
        # installed.  This is what makes the size point-in-time reproducible (and is
        # the true fix for the T-326 aggregate-cap flake: the global context, not a
        # boundary constant, was the leak).  The hard integer clamps that follow are
        # pure int math, but they are FED by these fractional steps, so the steps
        # must be pinned for the clamps to be reproducible.
        with _hermetic_decimal_ctx():
            # Validate ALL inputs up front (fail-closed): an out-of-range
            # probability/uncertainty is a malformed input and must be REJECTED
            # before any size decision, even the zero-edge early return.  A bad
            # input must never silently produce a (zero) size.
            unc = _clamp_unit(uncertainty, "uncertainty")

            # 1. Full Kelly (raw, for audit).  Negative edge → no bet.
            f_full = full_kelly_fraction(p_calibrated, net_odds)
            if f_full <= 0:
                # No positive edge → zero size.  This is a de-risk default, not a
                # failure: the cost gate / rule engine handles the "no trade" routing.
                return SizingDecision(
                    size_lamports=0,
                    full_kelly_fraction=f_full,
                    applied_kelly_fraction=Decimal(0),
                    pre_cap_lamports=0,
                    binding_constraint="zero_edge",
                )

            # 2. Apply the <= 1/4 Kelly cap.  NEVER the full fraction.
            f_applied = self._kelly_cap * f_full

            # 3. Uncertainty haircut — higher uncertainty shrinks size (never grows).
            #    `unc` was range-validated up front (fail-closed on a bad input).
            f_applied = f_applied * (Decimal(1) - unc)

            # 4. De-risk signals (sentiment/confidence) — product in (0,1], shrink only.
            f_applied = f_applied * sig.combined_factor()

            # Defensive: f_applied can never exceed the cap (every factor in (0,1]).
            # Under the pinned context this is exact; the assert is a true invariant
            # guard, not a victim of ambient rounding-UP.
            assert (
                f_applied <= self._kelly_cap
            ), "applied Kelly fraction exceeded the 1/4 cap — invariant violated"

            # 5. Size as a fraction of the per-trade cap (the unit of risk).  floor
            #    toward zero so rounding never pushes us OVER a cap.
            per_trade_cap = self._config.per_trade_cap_lamports
            _reject_float_money(per_trade_cap, "per_trade_cap_lamports")
            pre_cap = int(
                (Decimal(per_trade_cap) * f_applied).to_integral_value(rounding=ROUND_DOWN)
            )

        # The hard caps below are PURE INTEGER math (immune to decimal context); we
        # apply them outside the pinned block to make that immunity explicit.
        # 6. Hard clamp to the per-trade cap.
        binding = "kelly"
        size = pre_cap
        if size > per_trade_cap:
            size = per_trade_cap
            binding = "per_trade_cap"

        # 7. Hard clamp to the REMAINING aggregate headroom.  This is what makes
        #    the firm's TOTAL open SOL unable to exceed the aggregate cap.
        agg_cap = self._config.max_aggregate_lamports
        _reject_float_money(agg_cap, "max_aggregate_lamports")
        remaining = agg_cap - current_aggregate_lamports
        if remaining < 0:
            remaining = 0
        if size > remaining:
            size = remaining
            binding = "aggregate_cap"

        if size < 0:  # cannot happen, but never emit a negative size
            size = 0

        return SizingDecision(
            size_lamports=size,
            full_kelly_fraction=f_full,
            applied_kelly_fraction=f_applied,
            pre_cap_lamports=pre_cap,
            binding_constraint=binding,
        )
