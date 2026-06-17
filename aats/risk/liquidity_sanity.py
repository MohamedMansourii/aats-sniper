"""AUDIT-liquidity — Pre-trade LIQUIDITY-SANITY gate (depth + slippage simulation).

WHY THIS EXISTS
---------------
The block-0 token-safety gate (``pretrade_gate.py``) proves a token is not a
honeypot/rug; the cost gate (``cost_model.py``) proves edge beats a *declared*
cost stack.  Neither proves the pool can actually ABSORB the intended position
without unacceptable price impact, nor that the token has enough real turnover to
exit.  A token can pass every safety check and still be a thin, illiquid trap:
you fill, the pool moves 40% against you on the way in, and there is no 24h
volume to sell back into.  That is the gap this gate closes.

THE TWO CHECKS (both REJECT-only; refuse-by-default)
----------------------------------------------------
  1. LIQUIDITY-DEPTH / TURNOVER:  24h volume MUST be >= ``min_volume_multiple``
     (default 10x) times the intended position notional.  A pool whose whole-day
     turnover is a small multiple of your single ticket cannot be exited without
     becoming the volume.  Less than the multiple => REJECT.
  2. PRE-TRADE SLIPPAGE SIMULATION:  a constant-product (x*y=k) dry-run of the
     intended buy against the point-in-time reserves MUST produce a simulated
     entry slippage <= ``max_sim_slippage_bps``.  Over threshold => REJECT.

DE-RISK / SELECTIVITY ONLY (charter law)
----------------------------------------
This gate is structurally a VETO.  It returns PASS (silent — it sizes nothing,
triggers no buy, sets no stop, widens nothing) or REJECT (refuse the entry).
There is NO code path here that constructs an Intent, raises a size, lifts a cap,
or moves a stop.  A tighter threshold can only REJECT MORE.  It is never a
standalone buy trigger — it can only exclude.  (It is also NOT a social/news
signal: it reads on-chain depth + turnover facts only.)

POINT-IN-TIME (C-5)
-------------------
Every field on ``LiquidityInputs`` is stamped at ``event_time`` (the decision
slot/block_time).  There is no field that admits ``event_time + H`` data, so a
lookahead leak is not expressible.  ``data_staleness_ms`` is the freshness
budget; over budget => stale => REJECT (refuse-by-default).  No wall-clock is
read here, so the live and backtest paths are the same code.

FAST vs SLOW
------------
The slippage SIMULATION is pure integer/Decimal arithmetic over reserves already
held at event_time — 0 RPC, no LLM, no social/news/screener input.  It is safe to
run on the FAST/snipe pre-trade path.  (Discovery-time turnover screening lives
separately in ``screener.py`` on the SLOW loop; this gate is the per-ticket,
position-size-aware depth check that the screener cannot do because the screener
does not know the intended notional.)

MONEY (data-models.md §0)
-------------------------
All notionals/volumes/reserves are integer lamports/base-units; thresholds are
int bps or exact Decimal multiples.  NEVER float for money.

This module touches NO contracts and NO execution code.  It only DEFINES and
ENFORCES a reject-only rule, consistent with the risk-engineer boundary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Protocol, runtime_checkable

BPS_DENOM = 10_000


# ---------------------------------------------------------------------------
# Reject-reason taxonomy (stable string codes, dashboard/telemetry-facing).
# ---------------------------------------------------------------------------


class LiquidityReject(StrEnum):
    """Stable reject codes for the liquidity-sanity gate."""

    # 24h volume below the required multiple of the intended position notional.
    VOLUME_BELOW_MULTIPLE = "volume_below_position_multiple"
    # Simulated entry slippage exceeded the threshold (pool too thin for the size).
    SIM_SLIPPAGE_TOO_HIGH = "sim_slippage_too_high"
    # Refuse-by-default: a required input was missing/unknown/non-positive.
    INPUT_INCOMPLETE = "input_incomplete"
    # Refuse-by-default: point-in-time data older than the freshness budget.
    INPUT_STALE = "input_stale"


# ---------------------------------------------------------------------------
# Thresholds — tighten-only de-risk knobs (a tighter value rejects MORE).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LiquidityThresholds:
    """Liquidity-sanity thresholds.

    These are DE-RISK knobs: each can only be made STRICTER (reject more) without
    violating the no-rule-increases-risk law.  Loosening a threshold (smaller
    volume multiple, larger slippage tolerance) lets more candidates through —
    that is a reviewed config change, not something the gate code does at runtime.

    Money / counts are int bps or exact Decimal multiples — NO float money.
    """

    # Required ratio of 24h volume to intended position notional.  Default 10x:
    # the whole day's turnover must be at least 10 ticket-sizes deep, else exit is
    # not credible.  Decimal so "~10x" is exact; must be >= 1 (a sane floor).
    min_volume_multiple: Decimal = Decimal(10)
    # Max acceptable SIMULATED entry slippage (bps) for the intended size against
    # point-in-time reserves.  Default 300 bps (3%).  Above this the pool is too
    # thin for the ticket — REJECT.
    max_sim_slippage_bps: int = 300
    # Freshness budget (event-time staleness, ms).  Over budget => REJECT.
    max_staleness_ms: int = 2_000

    def __post_init__(self) -> None:
        # No float money on the int fields.
        for name in ("max_sim_slippage_bps", "max_staleness_ms"):
            val = getattr(self, name)
            if isinstance(val, float):
                raise TypeError(
                    f"LiquidityThresholds.{name} must be int (bps/ms), got float {val!r} "
                    "(data-models.md §0)."
                )
            if val < 0:
                raise ValueError(f"LiquidityThresholds.{name} must be non-negative, got {val}.")
        if not isinstance(self.min_volume_multiple, Decimal):
            raise TypeError(
                "LiquidityThresholds.min_volume_multiple must be Decimal (exact ratio), "
                f"got {type(self.min_volume_multiple).__name__} (data-models.md §0)."
            )
        if self.min_volume_multiple < Decimal(1):
            raise ValueError(
                "min_volume_multiple must be >= 1 (turnover must exceed one ticket); "
                f"got {self.min_volume_multiple}."
            )


# ---------------------------------------------------------------------------
# Inputs — point-in-time facts, ALL stamped at event_time.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LiquidityInputs:
    """Depth + turnover facts about a candidate, as-of ``event_time``.

    Every field is observable at the decision slot.  There is no field admitting
    ``event_time + H`` data, so a lookahead leak is not expressible.  A ``None``
    on a required field means "could not be decoded in budget" => REFUSE.

    Money fields are integer lamports / base units.  NO float.
    """

    mint: str
    event_slot: int  # event_time.slot — the as-of anchor (no forward reads)
    event_block_time_ms: int  # event_time.block_time_ms — AUTHORITATIVE clock
    data_staleness_ms: int  # age of freshest source datum vs decode

    # The intended position notional in lamports (SOL spent on entry).  This is
    # the size the SNIPE path WANTS to take.  The gate NEVER changes it — it only
    # judges whether the pool/turnover can support it.  Must be > 0.
    intended_position_lamports: int

    # 24h traded volume of the pair, in lamports (SOL-denominated).  None =>
    # unknown => REFUSE (we never read unknown turnover as "deep enough").
    volume_24h_lamports: int | None

    # Point-in-time pool reserves for the constant-product slippage sim.  Both
    # required for the sim; either None => REFUSE.  Integer base units.
    sol_reserve_lamports: int | None
    token_reserve_base: int | None

    def __post_init__(self) -> None:
        _INT_FIELDS = (
            "event_slot",
            "event_block_time_ms",
            "data_staleness_ms",
            "intended_position_lamports",
        )
        for name in _INT_FIELDS:
            val = getattr(self, name)
            if isinstance(val, float):
                raise TypeError(
                    f"LiquidityInputs.{name} must be int (lamports/slot/ms), got float {val!r} "
                    "(data-models.md §0)."
                )
        _INT_OR_NONE = (
            "volume_24h_lamports",
            "sol_reserve_lamports",
            "token_reserve_base",
        )
        for name in _INT_OR_NONE:
            val = getattr(self, name)
            if val is not None and isinstance(val, float):
                raise TypeError(
                    f"LiquidityInputs.{name} must be int lamports/base or None, got float {val!r} "
                    "(data-models.md §0)."
                )
        if self.event_block_time_ms <= 0:
            raise ValueError(
                "LiquidityInputs.event_block_time_ms must be > 0 (real on-chain time)."
            )
        if self.data_staleness_ms < 0:
            raise ValueError("LiquidityInputs.data_staleness_ms must be non-negative.")
        if self.intended_position_lamports <= 0:
            raise ValueError("LiquidityInputs.intended_position_lamports must be > 0.")


# ---------------------------------------------------------------------------
# Decision — reject-or-pass only.  Never sizes/forces.
# ---------------------------------------------------------------------------


class LiquidityVerdict(StrEnum):
    PASS = "PASS"  # depth + turnover adequate — gate is SILENT (sizes/triggers nothing)
    REJECT = "REJECT"  # at least one liquidity check failed — entry REFUSED (de-risk)


@dataclass(frozen=True)
class LiquidityRejectHit:
    """One failed liquidity check with the measured value vs threshold (for UI).

    All values are ints (bps / lamports) except ``measured_ratio`` which is the
    exact Decimal volume/position ratio for the volume check (None otherwise).
    """

    reason: LiquidityReject
    measured: int | None
    threshold: int | None
    measured_ratio: Decimal | None = None


@dataclass(frozen=True)
class LiquidityDecision:
    """The gate's verdict + the failed-check report.

    REJECT-or-PASS only: no field sizes a trade, sets a stop, or instructs
    execution.  It is a veto report.
    """

    mint: str
    verdict: LiquidityVerdict
    rejects: tuple[LiquidityRejectHit, ...]
    event_slot: int
    event_block_time_ms: int

    @property
    def passed(self) -> bool:
        return self.verdict is LiquidityVerdict.PASS

    @property
    def reject_codes(self) -> tuple[str, ...]:
        return tuple(hit.reason.value for hit in self.rejects)


# ---------------------------------------------------------------------------
# Telemetry seam — we INVOKE a sink; we do not author Prometheus here.
# ---------------------------------------------------------------------------


@runtime_checkable
class LiquidityTelemetry(Protocol):
    def on_liquidity_reject(self, mint: str, reasons: tuple[str, ...]) -> None: ...

    def on_liquidity_pass(self, mint: str) -> None: ...


# ---------------------------------------------------------------------------
# Pure constant-product slippage simulation (integer/Decimal; no float money).
# ---------------------------------------------------------------------------


def simulate_entry_slippage_bps(
    sol_reserve_lamports: int,
    token_reserve_base: int,
    sol_in_lamports: int,
) -> int | None:
    """Dry-run a constant-product (x*y=k) BUY and return entry slippage in bps.

    Mirrors the venue sim's ``_simulate_amm_entry`` math (constant-product, no
    fee term — this is a pure DEPTH/impact probe, the AMM fee is a separate cost
    line in the cost model).  Integer arithmetic throughout; the slippage ratio is
    computed in exact Decimal then floored to int bps.  No float money.

    Returns the simulated entry slippage (>= 0) in bps, or ``None`` when the sim
    cannot be formed (degenerate reserves / non-positive fill) — the caller treats
    ``None`` as refuse-by-default.
    """
    if sol_reserve_lamports <= 0 or token_reserve_base <= 0 or sol_in_lamports <= 0:
        return None
    k = sol_reserve_lamports * token_reserve_base
    new_reserve_sol = sol_reserve_lamports + sol_in_lamports
    new_reserve_tok = k // new_reserve_sol
    tokens_out = token_reserve_base - new_reserve_tok
    if tokens_out <= 0:
        return None
    spot_price = Decimal(sol_reserve_lamports) / Decimal(token_reserve_base)
    eff_price = Decimal(sol_in_lamports) / Decimal(tokens_out)
    if spot_price <= 0:
        return None
    slip_fraction = (eff_price - spot_price) / spot_price
    slip_bps = int(slip_fraction * BPS_DENOM)
    return max(0, slip_bps)


# ---------------------------------------------------------------------------
# The gate.
# ---------------------------------------------------------------------------


@dataclass
class LiquiditySanityGate:
    """Ordered, deterministic, refuse-by-default liquidity-sanity gate.

    Evaluates, in order:
      0a. staleness (refuse-by-default on over-budget point-in-time data),
      0b. required-input completeness (refuse-by-default on any None),
      1.  24h volume >= ``min_volume_multiple`` x intended position notional,
      2.  simulated entry slippage <= ``max_sim_slippage_bps``.

    ``evaluate`` short-circuits on the first failure (latency); ``scan_all``
    reports every failure for the dashboard.  The gate may ONLY reject — it
    constructs no Intent, sizes nothing, sets no stop, holds no entry/sizing path.
    """

    thresholds: LiquidityThresholds = field(default_factory=LiquidityThresholds)
    enabled: bool = True
    telemetry: LiquidityTelemetry | None = None

    # -- hot path -----------------------------------------------------------

    def evaluate(self, inputs: LiquidityInputs) -> LiquidityDecision:
        """Ordered, short-circuit-on-first-failure evaluation (refuse-by-default)."""
        # 0a. Staleness — over-budget point-in-time data is not trusted.
        if inputs.data_staleness_ms > self.thresholds.max_staleness_ms:
            return self._reject(
                inputs,
                (
                    LiquidityRejectHit(
                        LiquidityReject.INPUT_STALE,
                        inputs.data_staleness_ms,
                        self.thresholds.max_staleness_ms,
                    ),
                ),
            )
        for check in (self._check_volume, self._check_slippage):
            hit = check(inputs)
            if hit is not None:
                return self._reject(inputs, (hit,))
        return self._pass(inputs)

    def scan_all(self, inputs: LiquidityInputs) -> LiquidityDecision:
        """Evaluate ALL checks (no short-circuit) so the dashboard sees every fail."""
        hits: list[LiquidityRejectHit] = []
        if inputs.data_staleness_ms > self.thresholds.max_staleness_ms:
            hits.append(
                LiquidityRejectHit(
                    LiquidityReject.INPUT_STALE,
                    inputs.data_staleness_ms,
                    self.thresholds.max_staleness_ms,
                )
            )
        for check in (self._check_volume, self._check_slippage):
            hit = check(inputs)
            if hit is not None:
                hits.append(hit)
        if hits:
            return self._reject(inputs, tuple(hits))
        return self._pass(inputs)

    # -- per-check logic (pure; integer/Decimal only) -----------------------

    def _check_volume(self, inputs: LiquidityInputs) -> LiquidityRejectHit | None:
        """24h volume must be >= min_volume_multiple x intended position notional.

        Refuse-by-default: unknown volume => INPUT_INCOMPLETE.
        """
        vol = inputs.volume_24h_lamports
        if vol is None:
            return LiquidityRejectHit(LiquidityReject.INPUT_INCOMPLETE, None, None)
        # Required minimum 24h volume = multiple x position.  Exact in Decimal,
        # then a ceil so the floor is never understated by integer truncation.
        pos = inputs.intended_position_lamports
        required = self.thresholds.min_volume_multiple * Decimal(pos)
        # Ceil the requirement (require strictly enough depth; never round down).
        required_int = int(required) + (1 if required != int(required) else 0)
        if vol < required_int:
            ratio = Decimal(vol) / Decimal(pos)
            return LiquidityRejectHit(
                LiquidityReject.VOLUME_BELOW_MULTIPLE,
                vol,
                required_int,
                measured_ratio=ratio,
            )
        return None

    def _check_slippage(self, inputs: LiquidityInputs) -> LiquidityRejectHit | None:
        """Simulated entry slippage must be <= max_sim_slippage_bps.

        Refuse-by-default: missing reserves, or a sim that cannot be formed
        (degenerate pool), => INPUT_INCOMPLETE.
        """
        if inputs.sol_reserve_lamports is None or inputs.token_reserve_base is None:
            return LiquidityRejectHit(LiquidityReject.INPUT_INCOMPLETE, None, None)
        slip = simulate_entry_slippage_bps(
            inputs.sol_reserve_lamports,
            inputs.token_reserve_base,
            inputs.intended_position_lamports,
        )
        if slip is None:
            # Degenerate / non-positive fill — cannot prove acceptable impact.
            return LiquidityRejectHit(LiquidityReject.INPUT_INCOMPLETE, None, None)
        if slip > self.thresholds.max_sim_slippage_bps:
            return LiquidityRejectHit(
                LiquidityReject.SIM_SLIPPAGE_TOO_HIGH,
                slip,
                self.thresholds.max_sim_slippage_bps,
            )
        return None

    # -- verdict helpers ----------------------------------------------------

    def _reject(
        self, inputs: LiquidityInputs, hits: tuple[LiquidityRejectHit, ...]
    ) -> LiquidityDecision:
        decision = LiquidityDecision(
            mint=inputs.mint,
            verdict=LiquidityVerdict.REJECT,
            rejects=hits,
            event_slot=inputs.event_slot,
            event_block_time_ms=inputs.event_block_time_ms,
        )
        if self.telemetry is not None:
            self.telemetry.on_liquidity_reject(inputs.mint, decision.reject_codes)
        return decision

    def _pass(self, inputs: LiquidityInputs) -> LiquidityDecision:
        if self.telemetry is not None:
            self.telemetry.on_liquidity_pass(inputs.mint)
        return LiquidityDecision(
            mint=inputs.mint,
            verdict=LiquidityVerdict.PASS,
            rejects=(),
            event_slot=inputs.event_slot,
            event_block_time_ms=inputs.event_block_time_ms,
        )

    # -- entry-eligibility guard (fail-closed for LIVE) ---------------------

    def require_enabled_for_entry(self, decision: LiquidityDecision) -> bool:
        """True iff this decision may green-light a real entry.

        A disabled gate can NEVER green-light an entry (fail-closed): SHADOW/RECORD
        may run ``enabled=False`` to observe without blocking, but the SNIPE entry
        path must require a PASS from an ENABLED gate.
        """
        return self.enabled and decision.passed
