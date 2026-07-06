"""Momentum / reaction-entry harness — a DIFFERENT strategy on the SAME leak-safe machinery.

WHY A SECOND STRATEGY
=====================
The launch-instant snipe (`outcome_harness.py`: predict winners from t0 economics alone) ran
on real data and returned NO-GO — the thin launch-instant model did not beat the frozen naive
baseline net of cost.  This harness tests a structurally different, plausibly-edged idea:

  * DON'T decide at launch.  WAIT until `T_ENTRY` seconds (default 60) and read the EARLY
    price/pressure TRAJECTORY (the 30s and 60s marks), then decide whether to enter and hold.

It reuses the proven point-in-time / leak / exit / cost machinery of `outcome_harness.py`
verbatim — only the DECISION BOUNDARY moves forward in time.

THE LEAK BOUNDARY (the whole point — now a MOVING boundary at T_ENTRY)
=====================================================================
The launch harness put the boundary at the launch block_time: every forward observation was
outcome-only.  Here the boundary MOVES to `decision_cutoff = launch_block_time + T_ENTRY*1000`:

  * the DECISION at T_ENTRY may read ONLY the entry create fields (launch economics, stamped at
    the launch block_time) AND the forward observations with `horizon_s <= T_ENTRY` (the 30s &
    60s marks: early price move, buy/sell pressure `txns_m5`, liquidity).  Every decision
    feature is a `PITFeature` stamped at its OWN event-time (`launch_block_time + horizon_s*1000`
    for a forward mark) and the whole set is run through `assert_features_leq_decision` with the
    cutoff BEFORE any decision is computed.  A feature stamped after the cutoff (i.e. derived
    from a `horizon_s > T_ENTRY` mark) makes the guard RAISE `LeakError` = FAIL.

  * the OUTCOME reads ONLY the observations with `horizon_s > T_ENTRY`, walked through the SAME
    production `ExitEngine` (`walk_position`), netted by the SAME ~6% round-trip cost stack.

The two never share an argument: `decide_momentum_entry(entry, pre_entry_marks, ...)` is a pure
function of the entry + the `<= T_ENTRY` marks, and `resolve_momentum_outcome(entry_price,
post_entry_marks, ...)` is a pure function of the T_ENTRY fill price + the `> T_ENTRY` marks.
`split_forward_at_entry` partitions the path at T_ENTRY; the leak guard is belt-and-suspenders.

ENTRY PRICE (cannot enter what you cannot price)
================================================
The fill is the price AT the T_ENTRY mark.  If that price is `null` (not yet tradeable at
T_ENTRY) the launch is SKIPPED — recorded as `model_selected=False, baseline_selected=False,
net_pnl_lamports=0` (a real, costless "declined by both" outcome), NEVER a fabricated fill.

THE MODEL RULE (documented, FROZEN constants — not tuned after seeing results)
=============================================================================
Select iff, using ONLY the `<= T_ENTRY` trajectory:
  1. the launch is TRADEABLE at T_ENTRY (a non-null price exists at the T_ENTRY mark), AND
  2. early BUY PRESSURE is high — `buys / (buys + sells)` at T_ENTRY >= a frozen floor, AND
  3. price ROSE from the earliest pre-entry mark to the T_ENTRY mark (momentum confirmed), AND
  4. LIQUIDITY is present at T_ENTRY (`liquidity_usd` > a frozen floor — a real, sellable market).
These constants are ILLUSTRATIVE and FROZEN — they are NOT inline magic numbers but a declared,
change-controlled artifact (`aats/models/artifacts/MOMENTUM_PARAMS.frozen.json`, loaded at import,
audit-parity twin of `baseline.frozen.json`).  They are declared ONCE and MUST NOT be re-tuned
against the scoreboard (the same anti-p-hacking discipline the frozen baseline enforces); a test
FAILS if any frozen value drifts.  See `aats/models/artifacts/MODEL_CARD_momentum.md`.

KNOWN MODELING LIMITATION — ~64s TIMING DRIFT AT T_ENTRY (mild latency-optimism)
================================================================================
The corpus collector's forward marks are stamped at NOMINAL horizons (30/60/120/... s), but the
collector actually SAMPLES with observation-time drift: the nominal-60s mark lands at ~64s MEDIAN
(p90 ≈ +15.6s).  So "the reaction read AT T_ENTRY" is really the state at `T_ENTRY + observation
drift` — a few seconds MORE trajectory than a strict-live bot would have at exactly T_ENTRY.  This
is a MILD LATENCY-OPTIMISM: a real bot deciding at exactly 60s would see slightly LESS state than
this harness does, so the harness's selection is, if anything, marginally advantaged vs a strict-
live entry.  We DO NOT correct the collector here (that is the ingestion lane's fix — improve
entry-price fidelity, e.g. bonding-curve price at exactly T_ENTRY); we document the drift honestly
so the sizer/reviewer reads the T_ENTRY features as `T_ENTRY + drift`, not as a strict-60s snapshot.
The leak boundary is UNAFFECTED: drift moves the pre-entry mark slightly LATER in real time but the
mark's stamped event-time (`launch_block_time + horizon_s*1000`) is still <= the decision cutoff, so
`assert_features_leq_decision` still holds — the drift optimism is about STALENESS, not lookahead.

THE BASELINE (naive momentum — the control the model must beat)
==============================================================
Naive momentum: enter every tradeable-at-T_ENTRY launch whose price ROSE to T_ENTRY ("buy what
is going up").  The model is a strict SUBSET (it adds the buy-pressure + liquidity quality
filter), so GATE-B measures exactly one thing: does the extra selectivity earn MORE net-of-cost
PnL per unit risk than dumb momentum?  If it does not, there is no model (it stays silent).

ADVERSARIAL-HYPE NOTE
=====================
No social / synchronicity feature enters this model, so the contrarian-shilling rule has no
surface here.  The buy-pressure feature is ON-CHAIN txn pressure, not manufactured sentiment;
the caveat that on-chain pressure can be wash-traded/bundled is documented, not modeled away.

MONEY / NO WIN-RATE
===================
Every PnL / risk magnitude is INTEGER lamports; prices are exact Decimal.  There is NO win-rate
field, target, or tuning objective anywhere (a test asserts the absence).

OFFLINE / INJECTABLE
====================
Same injected Protocols as the launch harness (block_time resolver, forward reader).  No live
network, no keypair, no signing, no OMS, no capital.
"""

from __future__ import annotations

import hashlib
import json
import random
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

from aats.backtest.outcome_harness import (
    BlockTimeResolver,
    BlockTimeUnavailable,
    CorpusRecord,
    CorpusRecordError,
    EntryRecord,
    OutcomeResult,
    PITFeature,
    _mint_walk_seed,
    assert_features_leq_decision,
    build_round_trip_cost_stack,
    read_corpus,
    to_entry_record,
)
from aats.contracts.intents import CostStack
from aats.contracts.risk import RiskConfig
from aats.models.gate_b import TradeOutcome
from aats.risk.exit_engine import ExitEngine, preset
from aats.risk.exit_sim import walk_position

# The default reaction-entry horizon: decide 60 s after launch, reading the 30s & 60s marks.
DEFAULT_ENTRY_HORIZON_S = 60

# The FIXED corpus horizon grid the collector samples on (seconds).  `_entry_mark` requires an
# EXACT grid match at T_ENTRY, so an off-grid `entry_horizon_s` (e.g. 45, 90) would silently find
# NO T_ENTRY mark and decline EVERY record — a misleading all-NO-GO.  `_validate_entry_horizon`
# fails LOUD on an off-grid value instead (see build_momentum_outcomes / decide_momentum_entry).
CORPUS_HORIZON_GRID: tuple[int, ...] = (30, 60, 120, 300, 600, 900, 1800)


# ---------------------------------------------------------------------------
# FROZEN model constants — declared, change-controlled ARTIFACT (anti-p-hacking)
# ---------------------------------------------------------------------------
# The momentum selection constants are NOT inline magic numbers: they live in a frozen JSON
# artifact (`aats/models/artifacts/MOMENTUM_PARAMS.frozen.json`), the audit-parity twin of
# `aats/models/baseline.frozen.json`, and are loaded at IMPORT below.  Values are declared ONCE
# and MUST NOT be re-tuned against the scoreboard; a test FAILS if any frozen value drifts.

_MOMENTUM_PARAMS_PATH = (
    Path(__file__).resolve().parents[1] / "models" / "artifacts" / "MOMENTUM_PARAMS.frozen.json"
)

# The exact, ORDERED set of param keys hashed into the freeze (fixed order = stable hash).
_MOMENTUM_HASHED_PARAM_KEYS: tuple[str, ...] = ("buy_fraction_floor", "min_liquidity_usd")


class MomentumParamsError(ValueError):
    """Raised when the frozen momentum params artifact is missing, malformed, or has DRIFTED.

    A drifted `frozen_hash` is the anti-p-hacking failure (mirrors baseline `BaselineFrozenError`):
    the momentum selection constants changed after they were frozen.  The fix is NOT to edit the
    freeze — it is to recognise that retuning a frozen selection constant against the corpus is
    p-hacking the control, and is forbidden.
    """


@dataclass(frozen=True)
class MomentumParams:
    """The frozen, money-correct momentum selection constants (Decimal, never float)."""

    buy_fraction_floor: Decimal
    min_liquidity_usd: Decimal
    params_hash: str


def _canonical_momentum_hash(params: dict) -> str:
    """Deterministic SHA-256 over the hashed param subset (Decimal-as-string, no float).

    Reproducible on any machine / PYTHONHASHSEED: values are normalised via `str(Decimal(...))`
    so the digest is invariant to JSON formatting and never coerces money to float.
    """
    missing = [k for k in _MOMENTUM_HASHED_PARAM_KEYS if k not in params]
    if missing:
        raise MomentumParamsError(
            f"frozen momentum config `params` is missing hashed key(s): {missing}. "
            f"Required hashed keys: {list(_MOMENTUM_HASHED_PARAM_KEYS)}."
        )
    canonical: dict[str, str] = {}
    for key in _MOMENTUM_HASHED_PARAM_KEYS:
        val = params[key]
        if isinstance(val, float):
            raise MomentumParamsError(
                f"momentum param {key!r} must be a Decimal string, not float (got {val!r}). "
                "Thresholds are Decimal (data-models §0), never float."
            )
        try:
            canonical[key] = str(Decimal(str(val)))
        except InvalidOperation as exc:
            raise MomentumParamsError(
                f"momentum param {key!r} is not a valid Decimal (got {val!r})."
            ) from exc
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def load_momentum_params(path: str | Path = _MOMENTUM_PARAMS_PATH) -> MomentumParams:
    """Load + type the frozen momentum params, verifying the canonical hash has not drifted.

    Raises `MomentumParamsError` if the artifact is missing, a value is float-money, or the live
    canonical hash != the stored `frozen_hash` (the anti-p-hacking freeze check).
    """
    p = Path(path)
    if not p.exists():
        raise MomentumParamsError(f"frozen momentum params artifact not found at {p}")
    raw = json.loads(p.read_text(encoding="utf-8"))
    params = raw.get("params")
    if not isinstance(params, dict):
        raise MomentumParamsError("frozen momentum config must contain a `params` object")
    live_hash = _canonical_momentum_hash(params)
    stored_hash = raw.get("frozen_hash")
    if stored_hash not in (None, "", "null") and stored_hash != live_hash:
        raise MomentumParamsError(
            "momentum_params_changed_after_fit: the frozen momentum selection constants changed "
            f"after first freeze.\n  frozen_hash = {stored_hash}\n  live_hash   = {live_hash}\n"
            f"  hashed keys = {list(_MOMENTUM_HASHED_PARAM_KEYS)}\n"
            "A frozen selection constant is anti-p-hacking control (audit parity with the frozen "
            "baseline). Retuning it against the corpus is forbidden — revert the value or open an "
            "ADR + delta notice. This is a TEST FAILURE, not an edit."
        )
    return MomentumParams(
        buy_fraction_floor=Decimal(str(params["buy_fraction_floor"])),
        min_liquidity_usd=Decimal(str(params["min_liquidity_usd"])),
        params_hash=live_hash,
    )


# Loaded ONCE at import (mirrors baseline.py's use of baseline.frozen.json).
MOMENTUM_PARAMS = load_momentum_params()

# Buy pressure floor: strictly MORE buyers than sellers by a clear margin at T_ENTRY.  A curve
# where sells already match buys is NOT momentum.  Frozen artifact value (== previous inline 0.55).
_MOMENTUM_BUY_FRACTION_FLOOR = MOMENTUM_PARAMS.buy_fraction_floor
# Liquidity floor: a real, sellable market must exist at T_ENTRY.  Frozen value 0 == the previous
# inline strictly-positive existence check (`liquidity_usd > 0`).
_MOMENTUM_MIN_LIQUIDITY_USD = MOMENTUM_PARAMS.min_liquidity_usd


def _validate_entry_horizon(entry_horizon_s: int) -> None:
    """Fail LOUD if `entry_horizon_s` is off the fixed corpus horizon grid.

    `_entry_mark` requires an EXACT grid match at T_ENTRY, so an off-grid horizon (e.g. 45, 90)
    would silently find no T_ENTRY mark and decline EVERY record — a misleading all-NO-GO run.
    Raise `ValueError` here so the misconfiguration is caught immediately, not hidden.
    """
    if entry_horizon_s not in CORPUS_HORIZON_GRID:
        raise ValueError(
            f"entry_horizon_s={entry_horizon_s} is OFF the corpus horizon grid "
            f"{CORPUS_HORIZON_GRID}. `_entry_mark` requires an EXACT grid match, so an off-grid "
            "horizon would silently find NO T_ENTRY mark and decline EVERY record (a misleading "
            "all-NO-GO). Choose a horizon on the grid (fail-loud, not silent)."
        )


# ---------------------------------------------------------------------------
# FORWARD-TIME observation reader (richer than the launch harness — carries txn pressure)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MomentumForwardObservation:
    """One forward observation with the fields the momentum decision + outcome need.

    Distinct from `outcome_harness.ForwardObservation` because the momentum DECISION reads
    early buy/sell pressure (`txns_m5`) and liquidity from the `<= T_ENTRY` marks — fields the
    launch harness never needed.  `price_sol is None` means NO market at that horizon (rug /
    unlisted) — a VALID state, not missing data.
    """

    horizon_s: int
    price_sol: Decimal | None
    liquidity_usd: Decimal | None
    buys: int
    sells: int
    vol_m5: Decimal | None
    note: str
    obs_wall_ms: int

    @property
    def buy_fraction(self) -> Decimal:
        """buys / (buys + sells) — the early buy-pressure ratio (0 when no txns observed)."""
        total = self.buys + self.sells
        if total <= 0:
            return Decimal(0)
        return Decimal(self.buys) / Decimal(total)

    @property
    def tradeable(self) -> bool:
        """A non-null, positive price exists — the launch can be priced/entered here."""
        return self.price_sol is not None and self.price_sol > 0

    @property
    def liquidity_present(self) -> bool:
        return self.liquidity_usd is not None and self.liquidity_usd > 0


def _decimal_or_none(raw: object) -> Decimal | None:
    if raw is None:
        return None
    return Decimal(str(raw))


def read_momentum_forward(record: CorpusRecord) -> list[MomentumForwardObservation]:
    """Parse a corpus record's `forward` list into event-time-ordered momentum observations.

    Reads `txns_m5.{buys,sells}` (default 0 when absent), `liquidity_usd`, `vol_m5`, and the
    exact `price_sol` string (or None).  Ordered by `horizon_s` (event-time order) so the
    split and the walk consume marks in order.
    """
    out: list[MomentumForwardObservation] = []
    for obs in record.forward:
        txns = obs.get("txns_m5") or {}
        try:
            buys = int(txns.get("buys", 0) or 0)
            sells = int(txns.get("sells", 0) or 0)
        except (TypeError, ValueError):
            buys, sells = 0, 0
        # A non-numeric horizon_s / price / liquidity / vol / obs_wall_ms is MALFORMED data.
        # Wrap the parse failure as CorpusRecordError so build_momentum_outcomes CENSORS this one
        # record (consistent with existing censoring) instead of a bare ValueError/InvalidOperation
        # aborting the whole run.
        try:
            parsed = MomentumForwardObservation(
                horizon_s=int(obs["horizon_s"]),
                price_sol=_decimal_or_none(obs.get("price_sol")),
                liquidity_usd=_decimal_or_none(obs.get("liquidity_usd")),
                buys=buys,
                sells=sells,
                vol_m5=_decimal_or_none(obs.get("vol_m5")),
                note=str(obs.get("note", "")),
                obs_wall_ms=int(obs.get("obs_wall_ms", 0)),
            )
        except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
            raise CorpusRecordError(
                f"{record.mint or '?'}: malformed forward observation "
                f"(horizon_s={obs.get('horizon_s')!r}, price_sol={obs.get('price_sol')!r}) "
                "(CENSORED)"
            ) from exc
        out.append(parsed)
    out.sort(key=lambda o: o.horizon_s)
    return out


# ---------------------------------------------------------------------------
# The moving leak boundary: split the forward path at T_ENTRY
# ---------------------------------------------------------------------------


def split_forward_at_entry(
    path: list[MomentumForwardObservation], entry_horizon_s: int
) -> tuple[list[MomentumForwardObservation], list[MomentumForwardObservation]]:
    """Partition the forward path at T_ENTRY into (pre_entry <= T_ENTRY, post_entry > T_ENTRY).

    This IS the moving leak boundary.  The DECISION may read ONLY the first list; the OUTCOME
    may read ONLY the second.  A `horizon_s == T_ENTRY` mark is the decision anchor (pre-entry):
    it is observable AT the decision instant, so it is a legal decision input and the entry-
    price source — never an outcome mark.
    """
    pre = [o for o in path if o.horizon_s <= entry_horizon_s]
    post = [o for o in path if o.horizon_s > entry_horizon_s]
    return pre, post


def _entry_mark(
    pre_entry_marks: list[MomentumForwardObservation], entry_horizon_s: int
) -> MomentumForwardObservation | None:
    """The observation exactly AT the T_ENTRY horizon (the fill anchor), or None if absent."""
    for o in pre_entry_marks:
        if o.horizon_s == entry_horizon_s:
            return o
    return None


def _earliest_prior_price(
    pre_entry_marks: list[MomentumForwardObservation], entry_horizon_s: int
) -> Decimal | None:
    """The earliest non-null price STRICTLY BEFORE T_ENTRY — the momentum reference point.

    (e.g. the 30s price when T_ENTRY=60).  None when no earlier tradeable mark exists (then a
    price-rise cannot be confirmed and the momentum rule conservatively does not fire).
    """
    for o in pre_entry_marks:  # already event-time ordered
        if o.horizon_s < entry_horizon_s and o.price_sol is not None and o.price_sol > 0:
            return o.price_sol
    return None


# ---------------------------------------------------------------------------
# Point-in-time decision features + the (moving-cutoff) LEAK GUARD
# ---------------------------------------------------------------------------


def momentum_decision_cutoff_ms(entry: EntryRecord, entry_horizon_s: int) -> int:
    """The decision event-time: the launch block_time PLUS T_ENTRY seconds.

    Every legal decision feature must be stamped at or before this cutoff; a `> T_ENTRY` mark
    is stamped after it and trips the guard.
    """
    return entry.decision_block_time_ms + entry_horizon_s * 1000


def assemble_momentum_features(
    entry: EntryRecord,
    pre_entry_marks: list[MomentumForwardObservation],
    *,
    extra_features: tuple[PITFeature, ...] = (),
) -> list[PITFeature]:
    """Assemble the momentum decision features, EACH stamped at its own event-time.

    Launch economics are stamped at the launch block_time; each pre-entry forward mark's
    features are stamped at `launch_block_time + horizon_s*1000` (their true event-time).
    `extra_features` is the extension/leak seam: injecting a feature built from a `> T_ENTRY`
    mark (event-time after the cutoff) is exactly what `assert_features_leq_decision` catches.
    """
    bt = entry.decision_block_time_ms
    feats: list[PITFeature] = [
        PITFeature("sol_amount_lamports", float(entry.sol_amount_lamports), bt),
        PITFeature("v_sol_lamports", float(entry.v_sol_lamports), bt),
        PITFeature("v_tokens_base", float(entry.v_tokens_base), bt),
        PITFeature("market_cap_lamports", float(entry.market_cap_lamports), bt),
        PITFeature("initial_buy_tokens", float(entry.initial_buy_tokens), bt),
    ]
    for o in pre_entry_marks:
        et = bt + o.horizon_s * 1000
        price_val = float(o.price_sol) if o.price_sol is not None else 0.0
        liq_val = float(o.liquidity_usd) if o.liquidity_usd is not None else 0.0
        feats.append(PITFeature(f"fwd_price_sol@{o.horizon_s}s", price_val, et))
        feats.append(PITFeature(f"fwd_buys@{o.horizon_s}s", float(o.buys), et))
        feats.append(PITFeature(f"fwd_sells@{o.horizon_s}s", float(o.sells), et))
        feats.append(PITFeature(f"fwd_liquidity_usd@{o.horizon_s}s", liq_val, et))
    feats.extend(extra_features)
    return feats


# ---------------------------------------------------------------------------
# The DECISION (entry-time ONLY, up to and including T_ENTRY) — model + baseline
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MomentumDecision:
    """The T_ENTRY decision: two selection masks + the fill price + the fixed unit of risk.

    NO post-T_ENTRY value.  `sol_at_risk_lamports` is a FIXED per-trade risk unit so both
    cohorts are compared on identical exposure (GATE-B isolates SELECTION skill, not sizing).
    `entry_price_sol` is None iff the launch is not tradeable at T_ENTRY (a SKIP).
    """

    model_selected: bool
    baseline_selected: bool
    tradeable_at_entry: bool
    entry_price_sol: Decimal | None
    sol_at_risk_lamports: int
    # audit-only (never a TradeOutcome field)
    buy_fraction: float
    price_rose: bool
    liquidity_present: bool
    leak_audit_summary: str


def decide_momentum_entry(
    entry: EntryRecord,
    pre_entry_marks: list[MomentumForwardObservation],
    *,
    entry_horizon_s: int,
    risk_config: RiskConfig,
    extra_features: tuple[PITFeature, ...] = (),
) -> MomentumDecision:
    """Decide model + naive-momentum SELECTION from entry-time data ONLY (<= T_ENTRY).

    Steps:
      1. assemble the decision features (each stamped at its own event-time);
      2. RUN THE LEAK GUARD at cutoff = launch_block_time + T_ENTRY*1000 — a `> T_ENTRY`
         feature RAISES `LeakError` here (this is the moving boundary the leak test proves);
      3. TRADEABILITY: a non-null price at the T_ENTRY mark; if absent -> SKIP (both False);
      4. MODEL: tradeable AND buy-pressure high AND price rose AND liquidity present;
      5. BASELINE (naive momentum): tradeable AND price rose.

    `pre_entry_marks` must be the `<= T_ENTRY` partition; passing a `> T_ENTRY` mark makes its
    feature's event-time exceed the cutoff and trips the guard in step 2.

    Raises `ValueError` (fail-loud) if `entry_horizon_s` is off the corpus horizon grid — an
    off-grid horizon would silently find no T_ENTRY mark and decline every record.
    """
    _validate_entry_horizon(entry_horizon_s)
    cutoff = momentum_decision_cutoff_ms(entry, entry_horizon_s)
    features = assemble_momentum_features(entry, pre_entry_marks, extra_features=extra_features)
    summary = assert_features_leq_decision(features, cutoff)

    mark = _entry_mark(pre_entry_marks, entry_horizon_s)
    tradeable = mark is not None and mark.tradeable
    if not tradeable or mark is None or mark.price_sol is None:
        # SKIP — cannot enter what we cannot price. Recorded as declined by BOTH cohorts.
        return MomentumDecision(
            model_selected=False,
            baseline_selected=False,
            tradeable_at_entry=False,
            entry_price_sol=None,
            sol_at_risk_lamports=risk_config.per_trade_cap_lamports,
            buy_fraction=0.0,
            price_rose=False,
            liquidity_present=False,
            leak_audit_summary=summary,
        )

    entry_price = mark.price_sol
    prior_price = _earliest_prior_price(pre_entry_marks, entry_horizon_s)
    price_rose = prior_price is not None and entry_price > prior_price
    buy_fraction = mark.buy_fraction
    liquidity_present = mark.liquidity_present  # raw existence (audit field)
    # MODEL liquidity gate: liquidity_usd strictly above the FROZEN floor (artifact-loaded).
    # With the frozen floor = 0 this is the strictly-positive existence check, identical to
    # `liquidity_present`; the None-guard mirrors the property so no TypeError on a dead market.
    liquidity_ok = (
        mark.liquidity_usd is not None and mark.liquidity_usd > _MOMENTUM_MIN_LIQUIDITY_USD
    )

    # BASELINE — naive momentum: buy any tradeable launch that is going up.
    baseline_selected = bool(price_rose)
    # MODEL — momentum + on-chain buy-pressure quality + a real, sellable market.
    model_selected = bool(
        price_rose
        and (buy_fraction >= _MOMENTUM_BUY_FRACTION_FLOOR)
        and liquidity_ok
    )

    return MomentumDecision(
        model_selected=model_selected,
        baseline_selected=baseline_selected,
        tradeable_at_entry=True,
        entry_price_sol=entry_price,
        sol_at_risk_lamports=risk_config.per_trade_cap_lamports,
        buy_fraction=float(buy_fraction),
        price_rose=bool(price_rose),
        liquidity_present=bool(liquidity_present),
        leak_audit_summary=summary,
    )


# ---------------------------------------------------------------------------
# The OUTCOME (forward-time ONLY, strictly > T_ENTRY) — paper-walk net of cost
# ---------------------------------------------------------------------------


def resolve_momentum_outcome(
    mint: str,
    entry_price_sol: Decimal,
    post_entry_marks: list[MomentumForwardObservation],
    *,
    cost_stack: CostStack,
    sol_at_risk_lamports: int,
    decision_block_time_ms: int,
    engine: ExitEngine | None = None,
) -> OutcomeResult:
    """Resolve realized NET PnL from the `> T_ENTRY` marks only, filled at the T_ENTRY price.

    Mirrors `outcome_harness.resolve_outcome` but the fill is the T_ENTRY market price (not the
    launch bonding-curve price).  Forward marks become Decimal MULTIPLES of the entry price; a
    `None` (no market) horizon is a rug -> 0 (worthless), which trips the hard stop.  The walk's
    realized gross (which already includes the SECURE exit slippage) is taken NET of the
    remaining round-trip cost (total - exit slippage) so the exit cost is counted ONCE.

    Raises `CorpusRecordError` (CENSORED) when there are no post-entry marks or the entry price
    is non-positive — never a fabricated outcome.
    """
    if not post_entry_marks:
        raise CorpusRecordError(f"{mint}: no post-T_ENTRY marks to resolve outcome (CENSORED)")
    if entry_price_sol <= 0:
        raise CorpusRecordError(f"{mint}: non-positive T_ENTRY entry price (CENSORED)")

    multiples: list[Decimal] = []
    rugged = False
    for o in post_entry_marks:
        if o.price_sol is None:
            multiples.append(Decimal(0))
            rugged = True
        else:
            multiples.append(o.price_sol / entry_price_sol)

    eng = engine if engine is not None else ExitEngine(config=preset("secure_balanced"))
    walk = walk_position(
        engine=eng,
        mint=mint,
        sol_in_lamports=sol_at_risk_lamports,
        entry_fill_price=Decimal(1),  # marks are already MULTIPLES of the entry (R = multiple)
        path=multiples,
        rng=random.Random(_mint_walk_seed(mint)),
        block_time_ms_start=decision_block_time_ms,
    )

    entry_side_bps = cost_stack.total_cost_bps - cost_stack.exit_slippage_bps
    round_trip_cost_lamports = int(
        Decimal(sol_at_risk_lamports) * Decimal(entry_side_bps) / Decimal(10_000)
    )
    net_pnl = walk.gross_pnl_lamports - round_trip_cost_lamports

    return OutcomeResult(
        net_pnl_lamports=int(net_pnl),
        gross_pnl_lamports=int(walk.gross_pnl_lamports),
        round_trip_cost_lamports=int(round_trip_cost_lamports),
        realized_multiple=(multiples[-1] if multiples else Decimal(0)),
        n_forward_marks=len(multiples),
        rugged=rugged,
    )


# ---------------------------------------------------------------------------
# The harness: recorded corpus -> list[TradeOutcome] (momentum strategy)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MomentumHarnessStats:
    """Book-keeping for one momentum run (never a win-rate — pure counts)."""

    entry_horizon_s: int
    n_records: int
    n_resolved: int  # TradeOutcome records emitted (tradeable-resolved + untradeable skips)
    n_tradeable: int  # priced at T_ENTRY and outcome resolved from post-entry marks
    n_skipped_untradeable: int  # not priceable at T_ENTRY -> declined-by-both, net 0
    n_censored: int
    censor_reasons: tuple[str, ...]


def build_momentum_outcomes(
    records: Iterable[CorpusRecord],
    *,
    block_time_resolver: BlockTimeResolver,
    forward_reader=read_momentum_forward,
    risk_config: RiskConfig | None = None,
    cost_stack: CostStack | None = None,
    entry_horizon_s: int = DEFAULT_ENTRY_HORIZON_S,
) -> tuple[list[TradeOutcome], MomentumHarnessStats]:
    """Turn recorded launches into `TradeOutcome` records under the MOMENTUM-entry strategy.

    For each record: resolve the on-chain block_time (injected resolver), read + split the
    forward path at T_ENTRY, DECIDE from the `<= T_ENTRY` marks (leak-guarded), and:
      * if NOT tradeable at T_ENTRY -> emit a SKIP (both selections False, net 0) — never a
        fabricated fill;
      * else RESOLVE the outcome from the `> T_ENTRY` marks and emit the `TradeOutcome`.
    A record whose block_time cannot be resolved, or whose data is structurally unusable, is
    CENSORED (dropped), never fabricated.

    The DECISION and OUTCOME are computed by two functions that never share an argument — the
    (moving, T_ENTRY) structural leak boundary.

    Raises `ValueError` (fail-loud) BEFORE the loop if `entry_horizon_s` is off the corpus horizon
    grid: an off-grid horizon would silently decline every record (a misleading all-NO-GO).
    """
    _validate_entry_horizon(entry_horizon_s)
    rconfig = risk_config or RiskConfig()
    cstack = cost_stack or build_round_trip_cost_stack()
    engine = ExitEngine(config=preset("secure_balanced"))

    outcomes: list[TradeOutcome] = []
    n_records = 0
    n_tradeable = 0
    n_skipped = 0
    censor_reasons: list[str] = []

    for record in records:
        n_records += 1
        try:
            block_time = block_time_resolver.resolve(record.signature)
            entry = to_entry_record(record, block_time)
            path = forward_reader(record)
            pre_entry, post_entry = split_forward_at_entry(path, entry_horizon_s)

            # DECISION — entry-time only (<= T_ENTRY), leak-guarded inside decide_momentum_entry.
            decision = decide_momentum_entry(
                entry,
                pre_entry,
                entry_horizon_s=entry_horizon_s,
                risk_config=rconfig,
            )

            if not decision.tradeable_at_entry or decision.entry_price_sol is None:
                # SKIP — cannot price at T_ENTRY. Declined by both cohorts, real costless 0.
                n_skipped += 1
                outcomes.append(
                    TradeOutcome(
                        mint=entry.mint,
                        decision_slot=entry.decision_slot,
                        model_selected=False,
                        baseline_selected=False,
                        net_pnl_lamports=0,
                        sol_at_risk_lamports=decision.sol_at_risk_lamports,
                    )
                )
                continue

            # OUTCOME — forward-time only (> T_ENTRY).
            outcome = resolve_momentum_outcome(
                entry.mint,
                decision.entry_price_sol,
                post_entry,
                cost_stack=cstack,
                sol_at_risk_lamports=decision.sol_at_risk_lamports,
                decision_block_time_ms=momentum_decision_cutoff_ms(entry, entry_horizon_s),
                engine=engine,
            )
            n_tradeable += 1
            outcomes.append(
                TradeOutcome(
                    mint=entry.mint,
                    decision_slot=entry.decision_slot,
                    model_selected=decision.model_selected,
                    baseline_selected=decision.baseline_selected,
                    net_pnl_lamports=outcome.net_pnl_lamports,
                    sol_at_risk_lamports=decision.sol_at_risk_lamports,
                )
            )
        except (BlockTimeUnavailable, CorpusRecordError) as exc:
            censor_reasons.append(f"{record.mint or '?'}: {exc}")
            continue

    stats = MomentumHarnessStats(
        entry_horizon_s=entry_horizon_s,
        n_records=n_records,
        n_resolved=len(outcomes),
        n_tradeable=n_tradeable,
        n_skipped_untradeable=n_skipped,
        n_censored=len(censor_reasons),
        censor_reasons=tuple(censor_reasons),
    )
    return outcomes, stats


def read_corpus_records(corpus_path: str | Path) -> Iterator[CorpusRecord]:
    """Thin re-export of the launch harness's corpus reader (single source of truth)."""
    return read_corpus(corpus_path)


def build_momentum_from_corpus(
    corpus_path: str | Path,
    *,
    block_time_resolver: BlockTimeResolver,
    forward_reader=read_momentum_forward,
    risk_config: RiskConfig | None = None,
    cost_stack: CostStack | None = None,
    entry_horizon_s: int = DEFAULT_ENTRY_HORIZON_S,
) -> tuple[list[TradeOutcome], MomentumHarnessStats]:
    """Convenience: read a corpus file and build the momentum TradeOutcome list in one call."""
    return build_momentum_outcomes(
        read_corpus(corpus_path),
        block_time_resolver=block_time_resolver,
        forward_reader=forward_reader,
        risk_config=risk_config,
        cost_stack=cost_stack,
        entry_horizon_s=entry_horizon_s,
    )
