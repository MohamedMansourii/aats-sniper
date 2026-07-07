"""Realizable-exit outcome model — turn OPTIMISTIC spot marks into REALIZABLE fills.

WHY THIS EXISTS (the spot-optimism bug this closes)
===================================================
The edge proof resolves each trade's OUTCOME by walking the forward DexScreener SPOT prices
through the production exit engine (`outcome_harness.resolve_outcome`,
`momentum_harness.resolve_momentum_outcome`).  Spot price is OPTIMISTIC: it silently assumes
you can SELL a fresh meme token AT the quoted mid, at your full position size, at every mark.
You cannot.  Two realities make the realized exit strictly WORSE than spot:

  1. LIQUIDITY IMPACT — selling `per_trade_cap` of SOL into a thin pool moves the price against
     you.  The realized fill is worse than spot by a slippage that GROWS with
     `position_notional / pool_liquidity`.  A conservative, capped linear-impact (first-order
     constant-product) model haircuts the sell proceeds.

  2. HONEYPOT / UNSELLABLE — a forward mark with `price_sol is None` (no market) OR liquidity
     below a real-market floor is a mark you CANNOT exit into at any size.  A position that is
     NEVER sellable in its hold window is a near-total LOSS (you are bag-held), NOT a gain at a
     price you could not realize.

This module is the OUTCOME-ONLY realism layer.  It NEVER touches the decision, the selection,
or the point-in-time leak boundary — it only makes the realized PnL honest.  The `~6%`
round-trip cost stack (`build_round_trip_cost_stack`) still stacks ON TOP of this slippage.

THE CONSERVATIVE INVARIANT (non-waivable, red-team constraint)
=============================================================
For the SAME price path, REALIZABLE net PnL is ALWAYS <= SPOT net PnL.  Realism can only LOWER
PnL, never inflate it.  This is guaranteed BY CONSTRUCTION: the realizable gross is the spot
gross MINUS a non-negative deduction (`realizable_gross = spot_gross - deduction`, deduction >=
0), and a belt-and-suspenders clamp refuses any result above spot.  A test asserts it holds on
winner / flat / rug / thin / honeypot paths.

HOW THE DEDUCTION IS BUILT (reuses the walk verbatim — no forked exit logic)
===========================================================================
The exit engine still decides WHEN to sell on the observed spot marks (that is what a live bot
sees), via the SAME `walk_position` used by spot mode — the exit SCHEDULE is unchanged.  The
realism is applied to the walk's realized PROCEEDS:

    realizable_proceeds = int(spot_proceeds * multiplier),   multiplier in [0, 1]
    deduction           = spot_proceeds - realizable_proceeds        (>= 0)
    realizable_gross    = spot_gross - deduction
                        = (spot_proceeds - notional) - deduction
                        = realizable_proceeds - notional

where, on the OUTCOME (post-decision) marks:
  * multiplier = (1 - slip_fraction)  when at least one mark is SELLABLE (priced AND liquidity
    at/above the floor).  `slip_fraction = min(cap, coeff * notional_usd / representative_liq)`,
    with `representative_liq` = the MINIMUM sellable liquidity in the hold window (the thinnest
    book you would realistically have to exit into — the conservative choice).
  * multiplier = honeypot_residual  (~0) when NO mark is ever sellable — near-total loss.

`spot_proceeds >= 0` always (a sum of sales) and `multiplier <= 1`, so `deduction >= 0` and the
invariant holds with no assumption about the exit engine's internal monotonicity.

FROZEN CONSTANTS (anti-p-hacking — declared artifact, hash-drift guarded)
========================================================================
The slippage / honeypot constants are NOT inline magic numbers: they live in a declared,
change-controlled artifact (`aats/models/artifacts/REALIZABLE_EXIT_PARAMS.frozen.json`, the
audit-parity twin of `MOMENTUM_PARAMS.frozen.json` / `baseline.frozen.json`), loaded at import.
A canonical SHA-256 of `params` is pinned in `frozen_hash`; `load_realizable_params` (and a
test) FAIL if any value drifts.  Retuning a realism constant against the scoreboard is p-hacking
the OUTCOME — forbidden.  See `aats/models/artifacts/MODEL_CARD_realizable_exit.md`.

MONEY DISCIPLINE
================
Every PnL / deduction magnitude is INTEGER lamports; every constant / ratio is exact `Decimal`
(never float money, data-models §0).  There is no win-rate anywhere.

OFFLINE / PURE
==============
Pure functions of the (already-resolved) walk outputs + the outcome marks.  No network, no RNG,
no keypair, no capital.  Deterministic and reproducible.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Protocol

LAMPORTS_PER_SOL = 1_000_000_000

# --- exit-fidelity modes (the toggle) --------------------------------------
EXIT_MODEL_SPOT = "spot"  # optimistic: fill AT the quoted spot mark (parity / regression)
EXIT_MODEL_REALIZABLE = "realizable"  # conservative: liquidity impact + honeypot (DEFAULT)
EXIT_MODELS: tuple[str, ...] = (EXIT_MODEL_SPOT, EXIT_MODEL_REALIZABLE)


# ---------------------------------------------------------------------------
# FROZEN realism constants — declared, change-controlled ARTIFACT (anti-p-hacking)
# ---------------------------------------------------------------------------

_REALIZABLE_PARAMS_PATH = (
    Path(__file__).resolve().parents[1]
    / "models"
    / "artifacts"
    / "REALIZABLE_EXIT_PARAMS.frozen.json"
)

# The exact, ORDERED set of param keys hashed into the freeze (fixed order = stable hash).
_REALIZABLE_HASHED_PARAM_KEYS: tuple[str, ...] = (
    "linear_impact_coeff",
    "max_slippage_fraction",
    "min_liquidity_usd_floor",
    "honeypot_residual_bps",
    "sol_usd_fallback",
)

_BPS_DENOM = Decimal(10_000)


class RealizableExitError(ValueError):
    """Raised when the frozen realizable-exit artifact is missing, malformed, or has DRIFTED.

    A drifted `frozen_hash` is the anti-p-hacking failure (mirrors momentum
    `MomentumParamsError` / baseline `BaselineFrozenError`): the realism constants changed after
    they were frozen.  The fix is NOT to edit the freeze — retuning an outcome-realism constant
    against the corpus is p-hacking the OUTCOME, and is forbidden.  Revert or open an ADR.
    """


@dataclass(frozen=True)
class RealizableParams:
    """The frozen, money-correct realizable-exit constants (Decimal, never float).

    * `linear_impact_coeff`   — first-order (constant-product) price-impact coefficient: the
      slippage fraction for selling `notional_usd` into `liquidity_usd` is
      `coeff * notional_usd / liquidity_usd` (capped).  1.0 = "sell X% of the pool -> ~X% impact".
    * `max_slippage_fraction` — hard cap on a single realizable exit's slippage (0..1). A sellable
      exit never keeps LESS than `(1 - cap)` of spot proceeds (prevents an unbounded haircut that
      would double-count the honeypot branch).
    * `min_liquidity_usd_floor` — a mark with `liquidity_usd` below this (or null) is UNSELLABLE
      (no real, sellable market at decision-relevant size). Null liquidity is always unsellable.
    * `honeypot_residual_bps`   — residual recovery (bps of notional) on a position that is NEVER
      sellable in its window. 0 = a total loss of the notional (minus fees) = "near-total loss".
    * `sol_usd_fallback`        — coarse SOL/USD used ONLY when a mark carries no `price_usd` to
      derive the rate from (the live corpus carries `price_usd`, so this is rarely used).
    """

    linear_impact_coeff: Decimal
    max_slippage_fraction: Decimal
    min_liquidity_usd_floor: Decimal
    honeypot_residual_bps: Decimal
    sol_usd_fallback: Decimal
    params_hash: str

    @property
    def honeypot_residual_fraction(self) -> Decimal:
        """Residual recovery fraction of notional on a never-sellable (honeypot) position."""
        return self.honeypot_residual_bps / _BPS_DENOM


def _canonical_realizable_hash(params: dict) -> str:
    """Deterministic SHA-256 over the hashed param subset (Decimal-as-string, no float).

    Reproducible on any machine / PYTHONHASHSEED: values are normalised via `str(Decimal(...))`
    so the digest is invariant to JSON formatting and never coerces money/threshold to float.
    Mirrors `momentum_harness._canonical_momentum_hash` exactly (audit parity).
    """
    missing = [k for k in _REALIZABLE_HASHED_PARAM_KEYS if k not in params]
    if missing:
        raise RealizableExitError(
            f"frozen realizable-exit `params` is missing hashed key(s): {missing}. "
            f"Required hashed keys: {list(_REALIZABLE_HASHED_PARAM_KEYS)}."
        )
    canonical: dict[str, str] = {}
    for key in _REALIZABLE_HASHED_PARAM_KEYS:
        val = params[key]
        if isinstance(val, float):
            raise RealizableExitError(
                f"realizable-exit param {key!r} must be a Decimal string, not float (got {val!r}). "
                "Realism constants are Decimal (data-models §0), never float."
            )
        try:
            canonical[key] = str(Decimal(str(val)))
        except InvalidOperation as exc:
            raise RealizableExitError(
                f"realizable-exit param {key!r} is not a valid Decimal (got {val!r})."
            ) from exc
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def load_realizable_params(path: str | Path = _REALIZABLE_PARAMS_PATH) -> RealizableParams:
    """Load + type the frozen realizable-exit params, verifying the canonical hash unchanged.

    Raises `RealizableExitError` if the artifact is missing, a value is float-money, a value is
    out of range, or the live canonical hash != the stored `frozen_hash` (anti-p-hacking freeze).
    """
    p = Path(path)
    if not p.exists():
        raise RealizableExitError(f"frozen realizable-exit params artifact not found at {p}")
    raw = json.loads(p.read_text(encoding="utf-8"))
    params = raw.get("params")
    if not isinstance(params, dict):
        raise RealizableExitError("frozen realizable-exit config must contain a `params` object")
    live_hash = _canonical_realizable_hash(params)
    stored_hash = raw.get("frozen_hash")
    if stored_hash not in (None, "", "null") and stored_hash != live_hash:
        raise RealizableExitError(
            "realizable_exit_params_changed_after_freeze: the frozen realism constants changed "
            f"after first freeze.\n  frozen_hash = {stored_hash}\n  live_hash   = {live_hash}\n"
            f"  hashed keys = {list(_REALIZABLE_HASHED_PARAM_KEYS)}\n"
            "A frozen realism constant is an anti-p-hacking control (audit parity with the frozen "
            "baseline / momentum params). Retuning it against the corpus is forbidden — revert the "
            "value or open an ADR + delta notice. This is a TEST FAILURE, not an edit."
        )
    typed = RealizableParams(
        linear_impact_coeff=Decimal(str(params["linear_impact_coeff"])),
        max_slippage_fraction=Decimal(str(params["max_slippage_fraction"])),
        min_liquidity_usd_floor=Decimal(str(params["min_liquidity_usd_floor"])),
        honeypot_residual_bps=Decimal(str(params["honeypot_residual_bps"])),
        sol_usd_fallback=Decimal(str(params["sol_usd_fallback"])),
        params_hash=live_hash,
    )
    _validate_ranges(typed)
    return typed


def _validate_ranges(p: RealizableParams) -> None:
    """Fail-closed structural checks on the frozen values (an incoherent freeze is unusable)."""
    if p.linear_impact_coeff < 0:
        raise RealizableExitError("linear_impact_coeff must be >= 0 (a negative impact is a lie)")
    if not (Decimal(0) <= p.max_slippage_fraction <= Decimal(1)):
        raise RealizableExitError("max_slippage_fraction must be in [0, 1]")
    if p.min_liquidity_usd_floor < 0:
        raise RealizableExitError("min_liquidity_usd_floor must be >= 0")
    if not (Decimal(0) <= p.honeypot_residual_bps <= _BPS_DENOM):
        raise RealizableExitError("honeypot_residual_bps must be in [0, 10000]")
    if p.sol_usd_fallback <= 0:
        raise RealizableExitError("sol_usd_fallback must be > 0 (a coarse SOL/USD rate)")


# Loaded ONCE at import (mirrors momentum_harness's use of MOMENTUM_PARAMS.frozen.json).
REALIZABLE_PARAMS = load_realizable_params()


# ---------------------------------------------------------------------------
# The mark interface + the realizable adjustment
# ---------------------------------------------------------------------------


class SellableMark(Protocol):
    """The OUTCOME-mark fields the realism layer reads (duck-typed over both harnesses).

    Satisfied by `outcome_harness.ForwardObservation` and
    `momentum_harness.MomentumForwardObservation` (both carry `price_sol`, `price_usd`,
    `liquidity_usd`).  The layer reads ONLY post-decision (outcome) marks — never a decision
    input — so it cannot touch the leak boundary.
    """

    @property
    def price_sol(self) -> Decimal | None: ...
    @property
    def price_usd(self) -> Decimal | None: ...
    @property
    def liquidity_usd(self) -> Decimal | None: ...


def slippage_fraction(
    notional_usd: Decimal, liquidity_usd: Decimal, params: RealizableParams
) -> Decimal:
    """Conservative, capped linear price-impact slippage for selling `notional_usd` into a pool.

    `slip = min(cap, coeff * notional_usd / liquidity_usd)`.  Monotone NON-DECREASING in
    `notional_usd` and monotone NON-INCREASING in `liquidity_usd` (below the cap).  A non-positive
    liquidity returns the hard cap (you cannot realize into an empty book).  Returns a Decimal in
    `[0, cap]`.
    """
    if liquidity_usd <= 0 or notional_usd <= 0:
        return params.max_slippage_fraction if liquidity_usd <= 0 else Decimal(0)
    raw = params.linear_impact_coeff * notional_usd / liquidity_usd
    return raw if raw < params.max_slippage_fraction else params.max_slippage_fraction


def _median(values: list[Decimal]) -> Decimal:
    s = sorted(values)
    n = len(s)
    mid = n // 2
    if n % 2 == 1:
        return s[mid]
    return (s[mid - 1] + s[mid]) / Decimal(2)


def derive_sol_usd(marks: Iterable[SellableMark], params: RealizableParams) -> Decimal:
    """Derive the SOL/USD rate from the marks (median of `price_usd / price_sol`), else fallback.

    SOL/USD barely moves over a launch's hold window, so the median across the marks that carry
    BOTH a positive `price_sol` and `price_usd` is a robust, corpus-derived rate.  If NO mark
    carries a usable USD price (older corpora), the documented frozen `sol_usd_fallback` is used
    (coarse — the live corpus carries `price_usd`, so this branch is rarely taken).
    """
    ratios: list[Decimal] = []
    for m in marks:
        ps = m.price_sol
        pu = m.price_usd
        if ps is not None and ps > 0 and pu is not None and pu > 0:
            ratios.append(pu / ps)
    if not ratios:
        return params.sol_usd_fallback
    return _median(ratios)


@dataclass(frozen=True)
class RealizableAdjustment:
    """Audit record of the realism applied to one position (integer-lamport deduction)."""

    sellable_market_existed: bool
    sol_usd: Decimal
    representative_liquidity_usd: Decimal | None  # min sellable liquidity, or None = honeypot
    notional_usd: Decimal
    slip_fraction: Decimal
    multiplier: Decimal  # applied to spot proceeds (0..1)
    deduction_lamports: int  # >= 0 — the amount spot PnL is lowered by


def _sellable(mark: SellableMark, floor: Decimal) -> bool:
    """A mark is SELLABLE iff it is priced (>0) AND its liquidity is present and >= the floor.

    `price_sol is None` (no market) OR `liquidity_usd` null / below the floor => UNSELLABLE (you
    cannot realize an exit into it) — the honeypot/thin-liquidity condition.
    """
    ps = mark.price_sol
    lq = mark.liquidity_usd
    return ps is not None and ps > 0 and lq is not None and lq >= floor


def compute_realizable_adjustment(
    marks: Iterable[SellableMark],
    notional_lamports: int,
    spot_proceeds_lamports: int,
    params: RealizableParams,
) -> RealizableAdjustment:
    """Compute the conservative deduction that turns spot proceeds into REALIZABLE proceeds.

    `marks` are the OUTCOME (post-decision) marks the position is walked over.  Steps:
      1. derive SOL/USD (corpus-median, else fallback) and `notional_usd = per_trade_cap * rate`;
      2. find the SELLABLE marks (priced AND liquidity >= floor);
      3. if NONE is sellable -> HONEYPOT: multiplier = honeypot residual (~0) => near-total loss;
         else -> slip on the THINNEST sellable book: multiplier = 1 - slip_fraction;
      4. `deduction = spot_proceeds - int(spot_proceeds * multiplier)` (>= 0, integer lamports).

    Reads ONLY outcome marks — no decision input, no leak-boundary surface.
    """
    marks = list(marks)
    sol_usd = derive_sol_usd(marks, params)
    notional_sol = Decimal(notional_lamports) / Decimal(LAMPORTS_PER_SOL)
    notional_usd = notional_sol * sol_usd

    sellable_liqs = [
        m.liquidity_usd
        for m in marks
        if _sellable(m, params.min_liquidity_usd_floor) and m.liquidity_usd is not None
    ]

    if not sellable_liqs:
        # HONEYPOT / never sellable in the hold window -> bag-held, near-total loss.
        multiplier = params.honeypot_residual_fraction
        slip = Decimal(1) - multiplier
        rep_liq: Decimal | None = None
    else:
        rep_liq = min(sellable_liqs)
        slip = slippage_fraction(notional_usd, rep_liq, params)
        multiplier = Decimal(1) - slip

    realizable_proceeds = int(Decimal(spot_proceeds_lamports) * multiplier)
    deduction = spot_proceeds_lamports - realizable_proceeds
    if deduction < 0:
        # Belt-and-suspenders: proceeds >= 0 and multiplier <= 1 make this unreachable, but the
        # conservative invariant must NEVER be violated even by a rounding edge.
        deduction = 0

    return RealizableAdjustment(
        sellable_market_existed=bool(sellable_liqs),
        sol_usd=sol_usd,
        representative_liquidity_usd=rep_liq,
        notional_usd=notional_usd,
        slip_fraction=slip,
        multiplier=multiplier,
        deduction_lamports=int(deduction),
    )


def realizable_gross_pnl(
    *,
    spot_gross_lamports: int,
    spot_proceeds_lamports: int,
    marks: Iterable[SellableMark],
    notional_lamports: int,
    exit_model: str,
    params: RealizableParams | None = None,
) -> tuple[int, RealizableAdjustment | None]:
    """Apply the exit-fidelity model to a walk's spot result. Returns (gross_lamports, adj|None).

    * `exit_model == 'spot'`       -> return the walk's spot gross unchanged (parity/regression),
                                      no adjustment.
    * `exit_model == 'realizable'` -> subtract the conservative realism deduction; the returned
                                      gross is ALWAYS <= the spot gross (the invariant), enforced
                                      by construction and a final clamp.

    Raises `ValueError` on an unknown `exit_model` (fail-loud, never silently optimistic).
    """
    if exit_model == EXIT_MODEL_SPOT:
        return spot_gross_lamports, None
    if exit_model != EXIT_MODEL_REALIZABLE:
        raise ValueError(
            f"unknown exit_model {exit_model!r} (expected one of {EXIT_MODELS})"
        )
    p = params or REALIZABLE_PARAMS
    adj = compute_realizable_adjustment(
        marks, notional_lamports, spot_proceeds_lamports, p
    )
    gross = spot_gross_lamports - adj.deduction_lamports
    # The conservative invariant, defended one more time: realism can only LOWER PnL.
    if gross > spot_gross_lamports:
        gross = spot_gross_lamports
    return int(gross), adj
