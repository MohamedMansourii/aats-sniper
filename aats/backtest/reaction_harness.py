"""Reaction / front-run harness — the smart-money/KOL SIGNAL -> reaction edge proof.

WHY A THIRD STRATEGY (the pivot)
================================
The on-chain LAUNCH-DATA edge is DECISIVELY falsified: the launch-instant snipe
(`outcome_harness.py`) AND the momentum-@60s entry (`momentum_harness.py`) both ran NO-GO on
thousands of real launches, on realizable exits.  The only remaining thesis with a real prior
(REACTION-CORPUS-SPEC.md) is FRONT-RUNNING the predictable retail reaction to a PROVEN signal:
a smart-money wallet buy / a KOL call / a whale buy at time T, entered just after T and exited
into the reaction.  This harness is the leak-free GATE-A / GATE-B proof of that thesis.

It REUSES the proven point-in-time / realizable-exit / cost / gate machinery verbatim — only the
DECISION changes: the anchor is the on-chain `signal_block_time_ms` (already in the corpus, T-300a),
the entry price is the `signal_price_sol` (haircut for front-run latency), and the model is a
WALK-FORWARD source-reputation filter (not the launch/momentum trajectory model).

THE CORPUS (REACTION-CORPUS-SPEC.md AND the live recorder — one JSON line per SIGNAL event)
==========================================================================================
    { "signal_type": "whale_buy | smart_money_buy | kol_call",
      "source_id":   "<wallet | caller_id | 'whale'>",
      "mint":        "<token mint>",
      "signal_slot":          <int on-chain slot>,
      "signal_block_time_ms": <int on-chain block_time*1000 — T-300a, NEVER wall-clock>,
      "signal_price_sol":     "<Decimal-as-string price at the signal>",
      "signal_size_sol":      "<Decimal-as-string — the signal buy size>",
      "vsol_lamports":        <int | null — bonding-curve virtual SOL reserve at the signal>,
      "vtok":                 <int | null — bonding-curve virtual token reserve at the signal>,
      "source_prior":         <float | null — externally-provided reputation, AUDIT ONLY>,
      "forward": [ {horizon_s, price_sol, txns{,_m5}:{buys,sells}, liquidity_usd?, n_trades?}, ...] }

SCHEMA DRIFT — SPEC vs the LIVE bonding-curve recorder (bridged here, never assumed away)
----------------------------------------------------------------------------------------
The REACTION-CORPUS-SPEC.md forward shape is the DexScreener/launch shape (`txns_m5`,
`liquidity_usd` per mark).  The LIVE recorder emits a pump.fun BONDING-CURVE shape instead:
the forward key is `txns` (not `txns_m5`), there is NO per-mark `liquidity_usd` (a bonding curve
is not a DexScreener pool), and the SIGNAL record carries the curve reserves `vsol_lamports` /
`vtok`.  Feeding that live shape straight through the DexScreener-liquidity realizable-exit model
made EVERY mark unsellable (null liquidity => honeypot => uniform total loss), which turned the
whole reaction proof into a schema artifact (a NO-GO that measured nothing).  This harness BRIDGES
both shapes without weakening the outcome model:
  * buy/sell pressure is read from `txns` OR `txns_m5` (either shape),
  * a mark's sellable-exit liquidity is its own `liquidity_usd` when present (DexScreener shape),
    else a BONDING-CURVE liquidity proxy derived from the signal's `vsol_lamports`
    (`bonding_curve_liquidity_usd`: ~2x the curve's SOL reserve in USD, the standard
    two-sided-pool liquidity a DexScreener floor is calibrated against),
  * a record with NEITHER a per-mark liquidity NOR a usable `vsol_lamports` carries NO exit-liquidity
    information and is CENSORED (fail-closed) — never silently resolved to a fabricated -100%.
The proxy uses the SIGNAL-time reserve (a signal-time quantity, so it is not lookahead; it never
enters the DECISION — it is applied only to the OUTCOME marks).  Because the curve deepens as a
token pumps, the signal-time reserve UNDER-states later depth, so the slippage haircut is if
anything conservative on a winner; a rug/dump shows up as the price path collapsing (handled by the
walk), not as fabricated depth.  Forward horizons are post-signal (15/30/60/120/300/600 s).
The `forward` shape still MATCHES the launch corpus, so the realizable-exit walk is reused directly.

TWO STRUCTURAL LEAK BOUNDARIES (the whole game — both tested RED-before/GREEN-after)
===================================================================================
(1) DECISION vs OUTCOME (signal-anchored):
    * the DECISION reads ONLY signal-time data (signal_type, signal_size, the WALK-FORWARD source
      reputation, the signal price) — each stamped at `signal_block_time_ms` and run through
      `assert_features_leq_decision` with cutoff = `signal_block_time_ms` BEFORE any decision.  A
      feature derived from a `forward` mark carries event-time `signal_block_time + horizon_s*1000`
      > the cutoff and makes the guard RAISE `LeakError`.
    * the OUTCOME reads ONLY the `forward` marks (strictly after the signal), walked through the
      SAME production `ExitEngine` (`resolve_momentum_outcome`), netted by the SAME ~6% cost stack.

(2) SOURCE REPUTATION is WALK-FORWARD (same-source, strictly-prior, already-observable):
    the reputation used to decide signal i aggregates ONLY the realized net-PnL-per-SOL of the SAME
    source's signals j with `signal_block_time[j] < signal_block_time[i]` (strictly prior — a
    source's own current signal and any FUTURE signal are excluded) AND
    `outcome_resolved_time[j] <= signal_block_time[i]` (the prior outcome was fully realized by the
    decision instant — no outcome-completion lookahead).  `assert_reputation_inputs_prior` enforces
    BOTH conditions structurally; injecting a source's current/future outcome (or a not-yet-observed
    prior outcome) raises `ReputationLeakError` = FAIL.  A source's FIRST signals (fewer than
    `min_prior_signals` eligible prior outcomes) are decided on a NEUTRAL PRIOR (frozen policy) — no
    future outcome of the same source ever informs the current decision.

THE MODEL vs THE BASELINE (GATE-B control)
==========================================
  * BASELINE = FOLLOW EVERY SIGNAL (enter on every recorded signal, fixed per-trade risk).
  * MODEL    = the same, EXCEPT it DECLINES a signal whose source has a PROVEN track record
    (>= `min_prior_signals` eligible prior outcomes) whose mean realized net-PnL-per-SOL is BELOW
    the frozen `reputation_threshold`.  The model is therefore a strict SUBSET of the baseline (it
    can only DE-RISK — drop proven-loser sources — never add a signal), so GATE-B measures exactly
    one thing: does demoting proven-loser sources earn MORE net-of-cost PnL per unit risk than
    following everyone?  If not, there is no model (it stays silent).

FROZEN CONSTANTS (anti-p-hacking — declared artifact, hash-drift guarded)
========================================================================
The reputation threshold + policy + the front-run entry-latency haircut are NOT inline magic
numbers: they live in a declared, change-controlled artifact
(`aats/models/artifacts/REACTION_PARAMS.frozen.json`, the audit-parity twin of
`MOMENTUM_PARAMS.frozen.json` / `baseline.frozen.json`), loaded at import.  A canonical SHA-256 of
`params` is pinned in `frozen_hash`; `load_reaction_params` (and a test) FAIL if any value drifts.
Retuning a selection constant against the scoreboard is p-hacking — forbidden.

NO WIN-RATE (honesty clause, binding)
=====================================
The reputation aggregates realized net-PnL-per-SOL (money, exact Decimal) — NOT a fraction of
trades that won.  There is no win-rate field, target, or tuning objective anywhere (a test asserts
the absence of the tokens).

TESTS (the "tested" claims above are grounded, not aspirational)
================================================================
Every structural claim in this module is covered by a committed test — this docstring names them so
the "tested RED-before/GREEN-after" assertions are verifiable, not a bare comment:
  * LEAK BOUNDARY 1 (decision vs outcome): `tests/backtest/test_reaction_harness.py`
    ::test_leak_b1_red_forward_feature_injected / ::test_leak_b1_red_forward_feature_via_assembler
    (RED) and ::test_leak_b1_green_signal_time_only_decision (GREEN); the QA regression
    `tests/backtest/test_reaction_leak_audit.py`::test_b1_forward_feature_guard_is_load_bearing
    NEUTERS the guard to prove it is load-bearing.
  * LEAK BOUNDARY 2 (walk-forward reputation): ::test_leak_b2_red_current_or_future_same_source_outcome
    / ::test_leak_b2_red_prior_outcome_not_yet_observed (RED),
    ::test_leak_b2_green_strictly_prior_observed +
    ::test_leak_b2_end_to_end_reputation_ignores_future_same_source (GREEN); the audit's
    ::test_b2_future_monster_winner_cannot_rescue_earlier_decision /
    ::test_b2_reputation_guard_is_load_bearing prove no future same-source outcome can leak.
  * MODEL is a strict DE-RISK SUBSET of the follow-every-signal baseline:
    ::test_model_is_subset_and_beats_baseline_on_proven_loser_source.
  * FROZEN-DRIFT of REACTION_PARAMS (anti-p-hacking): ::test_frozen_hash_drift_fails +
    ::test_frozen_float_money_rejected.  `source_prior` is audit-only:
    ::test_source_prior_does_not_change_selection.
A committed docstring claim of "tested" that no test backs is itself a defect; these names keep this
one honest and catch any future removal of a guard (the neuter/RED tests fail if a guard is dropped).

MONEY / OFFLINE / REPRODUCIBLE
==============================
Every PnL / risk magnitude is INTEGER lamports; every price / threshold is exact Decimal.  Pure
functions of the recorded corpus + a fixed seed (inherited from the realizable-exit walk).  No RPC
(the anchor is already on-chain in the corpus), no keypair, no signing, no OMS, no capital.
"""

from __future__ import annotations

import hashlib
import heapq
import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

from aats.backtest.momentum_harness import (
    MomentumForwardObservation,
    resolve_momentum_outcome,
)
from aats.backtest.outcome_harness import (
    CorpusRecordError,
    LeakError,
    PITFeature,
    assert_features_leq_decision,
    build_round_trip_cost_stack,
)
from aats.backtest.realizable_exit import (
    EXIT_MODEL_REALIZABLE,
    EXIT_MODELS,
    REALIZABLE_PARAMS,
    RealizableParams,
)
from aats.contracts.intents import CostStack
from aats.contracts.risk import RiskConfig
from aats.models.gate_b import TradeOutcome

LAMPORTS_PER_SOL = 1_000_000_000
_BPS_DENOM = Decimal(10_000)

# The valid signal types (REACTION-CORPUS-SPEC.md §2).  An unknown type is CENSORED (never
# silently followed) so a schema drift in the recorder cannot leak an untyped signal into a proof.
VALID_SIGNAL_TYPES: frozenset[str] = frozenset(
    {"whale_buy", "smart_money_buy", "kol_call"}
)


# ---------------------------------------------------------------------------
# FROZEN model constants — declared, change-controlled ARTIFACT (anti-p-hacking)
# ---------------------------------------------------------------------------

_REACTION_PARAMS_PATH = (
    Path(__file__).resolve().parents[1] / "models" / "artifacts" / "REACTION_PARAMS.frozen.json"
)

# The exact, ORDERED set of param keys hashed into the freeze (fixed order = stable hash).
_REACTION_HASHED_PARAM_KEYS: tuple[str, ...] = (
    "reputation_threshold",
    "min_prior_signals",
    "neutral_prior_selects",
    "entry_latency_haircut_bps",
)

# Per-key coercion for the canonical hash (mirrors baseline.py's per-key normalisation): the
# money/threshold field is Decimal-as-string, counts are int, the policy flag is bool.  This keeps
# the digest reproducible on any machine / PYTHONHASHSEED and never coerces a threshold to float.
_REACTION_DECIMAL_KEYS: frozenset[str] = frozenset({"reputation_threshold"})
_REACTION_INT_KEYS: frozenset[str] = frozenset(
    {"min_prior_signals", "entry_latency_haircut_bps"}
)
_REACTION_BOOL_KEYS: frozenset[str] = frozenset({"neutral_prior_selects"})


class ReactionParamsError(ValueError):
    """Raised when the frozen reaction params artifact is missing, malformed, or has DRIFTED.

    A drifted `frozen_hash` is the anti-p-hacking failure (mirrors momentum `MomentumParamsError`
    / baseline `BaselineFrozenError`): the reputation-filter constants changed after they were
    frozen.  The fix is NOT to edit the freeze — retuning a selection constant against the corpus
    is p-hacking the control, and is forbidden.  Revert or open an ADR + delta notice.
    """


class ReactionCorpusError(CorpusRecordError):
    """A reaction-corpus line is structurally unusable (missing anchor / price / forward).

    Subclasses `CorpusRecordError` so the build loop's existing fail-closed `except` CENSORS the
    record (drops it), never fabricating an anchor or a fill.
    """


class ReputationLeakError(AssertionError):
    """Raised when a source-reputation input is NOT strictly-prior-and-observable (leak boundary 2).

    Fires the instant a same-source signal's outcome that is the CURRENT signal, a FUTURE signal, or
    a not-yet-observed prior outcome is offered to the reputation of a decision.  This is the
    structural walk-forward boundary — no future outcome of the same source may inform the decision.
    """


@dataclass(frozen=True)
class ReactionParams:
    """The frozen, money-correct reaction-filter constants (Decimal/int/bool, never float money).

    * `reputation_threshold`      — the net-PnL-per-SOL floor a source's PROVEN track record must
      clear for the model to keep following it.  Decimal (a signed money-per-risk ratio).  `"0"` =
      "do not follow a source whose realized prior track record is net-negative per SOL" — a
      structural floor, not a fitted level.
    * `min_prior_signals`         — how many ELIGIBLE (strictly-prior, already-observed) same-source
      outcomes are required before the reputation gate applies.  Below this a source is on the
      NEUTRAL PRIOR.
    * `neutral_prior_selects`     — the neutral-prior policy: whether a source with too short a
      track record is FOLLOWED (True = innocent-until-proven-loser; the model only demotes a
      source once it has proven itself a net loser).
    * `entry_latency_haircut_bps` — the front-run entry-latency haircut (bps): a real bot cannot
      fill at the exact signal tick, so the entry fill is WORSE than `signal_price_sol` by this
      many bps (the reaction has already begun moving the price).  Conservative — it can only LOWER
      realized PnL (never fabricate a GO); stacks with, and is distinct from, the ~6% cost stack's
      AMM entry slippage (that is execution slippage; this is the adverse price move during the lag).
    """

    reputation_threshold: Decimal
    min_prior_signals: int
    neutral_prior_selects: bool
    entry_latency_haircut_bps: int
    params_hash: str

    @property
    def entry_latency_haircut_fraction(self) -> Decimal:
        """The entry-latency haircut as a fraction (bps / 10_000)."""
        return Decimal(self.entry_latency_haircut_bps) / _BPS_DENOM


def _canonical_reaction_hash(params: dict) -> str:
    """Deterministic SHA-256 over the hashed param subset (Decimal-as-string / int / bool, no float).

    Reproducible on any machine / PYTHONHASHSEED: the money/threshold field is normalised via
    `str(Decimal(...))`, counts as int, the policy flag as bool.  Mirrors
    `momentum_harness._canonical_momentum_hash` / `baseline.canonical_params_hash` (audit parity).
    """
    missing = [k for k in _REACTION_HASHED_PARAM_KEYS if k not in params]
    if missing:
        raise ReactionParamsError(
            f"frozen reaction `params` is missing hashed key(s): {missing}. "
            f"Required hashed keys: {list(_REACTION_HASHED_PARAM_KEYS)}."
        )
    canonical: dict[str, object] = {}
    for key in _REACTION_HASHED_PARAM_KEYS:
        val = params[key]
        if key in _REACTION_DECIMAL_KEYS:
            if isinstance(val, float):
                raise ReactionParamsError(
                    f"reaction param {key!r} must be a Decimal string, not float (got {val!r}). "
                    "Thresholds are Decimal (data-models §0), never float."
                )
            try:
                canonical[key] = str(Decimal(str(val)))
            except InvalidOperation as exc:
                raise ReactionParamsError(
                    f"reaction param {key!r} is not a valid Decimal (got {val!r})."
                ) from exc
        elif key in _REACTION_INT_KEYS:
            if isinstance(val, bool) or not isinstance(val, int):
                raise ReactionParamsError(
                    f"reaction param {key!r} must be an int (got {val!r})."
                )
            canonical[key] = int(val)
        elif key in _REACTION_BOOL_KEYS:
            if not isinstance(val, bool):
                raise ReactionParamsError(
                    f"reaction param {key!r} must be a bool (got {val!r})."
                )
            canonical[key] = bool(val)
        else:  # pragma: no cover - defensive: the key set is fixed above
            canonical[key] = val
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def load_reaction_params(path: str | Path = _REACTION_PARAMS_PATH) -> ReactionParams:
    """Load + type the frozen reaction params, verifying the canonical hash has not drifted.

    Raises `ReactionParamsError` if the artifact is missing, a value is float-money, a value is out
    of range, or the live canonical hash != the stored `frozen_hash` (the anti-p-hacking freeze).
    """
    p = Path(path)
    if not p.exists():
        raise ReactionParamsError(f"frozen reaction params artifact not found at {p}")
    raw = json.loads(p.read_text(encoding="utf-8"))
    params = raw.get("params")
    if not isinstance(params, dict):
        raise ReactionParamsError("frozen reaction config must contain a `params` object")
    live_hash = _canonical_reaction_hash(params)
    stored_hash = raw.get("frozen_hash")
    if stored_hash not in (None, "", "null") and stored_hash != live_hash:
        raise ReactionParamsError(
            "reaction_params_changed_after_freeze: the frozen reaction-filter constants changed "
            f"after first freeze.\n  frozen_hash = {stored_hash}\n  live_hash   = {live_hash}\n"
            f"  hashed keys = {list(_REACTION_HASHED_PARAM_KEYS)}\n"
            "A frozen selection constant is an anti-p-hacking control (audit parity with the frozen "
            "baseline / momentum / realizable params). Retuning it against the corpus is forbidden — "
            "revert the value or open an ADR + delta notice. This is a TEST FAILURE, not an edit."
        )
    typed = ReactionParams(
        reputation_threshold=Decimal(str(params["reputation_threshold"])),
        min_prior_signals=int(params["min_prior_signals"]),
        neutral_prior_selects=bool(params["neutral_prior_selects"]),
        entry_latency_haircut_bps=int(params["entry_latency_haircut_bps"]),
        params_hash=live_hash,
    )
    _validate_reaction_ranges(typed)
    return typed


def _validate_reaction_ranges(p: ReactionParams) -> None:
    """Fail-closed structural checks on the frozen values (an incoherent freeze is unusable)."""
    if p.min_prior_signals < 1:
        raise ReactionParamsError(
            f"min_prior_signals must be >= 1 (a source needs a track record), got "
            f"{p.min_prior_signals}"
        )
    if p.entry_latency_haircut_bps < 0:
        raise ReactionParamsError(
            "entry_latency_haircut_bps must be >= 0 (a negative front-run latency cost is a lie)"
        )
    if p.entry_latency_haircut_bps > int(_BPS_DENOM):
        raise ReactionParamsError("entry_latency_haircut_bps must be <= 10000 (a full 100%)")


# Loaded ONCE at import (mirrors momentum_harness's use of MOMENTUM_PARAMS.frozen.json).
REACTION_PARAMS = load_reaction_params()


# ---------------------------------------------------------------------------
# The signal-time view (decision-time ONLY) + the forward view (outcome-time ONLY)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReactionSignal:
    """SIGNAL-TIME view of one reaction event — the ONLY input the decision may read.

    Carries the on-chain decision anchor (`signal_block_time_ms`, T-300a) + the signal economics
    (exact Decimal price/size).  It carries NO forward observation.  `source_prior` (an externally
    provided reputation number) is retained for AUDIT ONLY and is NEVER a selection input — its
    provenance / point-in-time correctness is unverified, so the model relies solely on the
    walk-forward reputation this harness computes itself (leak-free by construction).
    """

    mint: str
    source_id: str
    signal_type: str
    signal_slot: int
    signal_block_time_ms: int
    signal_price_sol: Decimal
    signal_size_sol: Decimal | None
    source_prior: float | None  # AUDIT ONLY — never a selection input (unverified provenance)
    # Bonding-curve virtual SOL reserve at the signal (int lamports) — OUTCOME-only: it feeds the
    # exit-liquidity proxy for bonding-curve corpora that carry no per-mark liquidity_usd.  A
    # signal-time quantity (not lookahead) and NEVER a decision feature.  None on DexScreener-shape
    # corpora that carry per-mark liquidity_usd instead.  Trailing default keeps positional callers
    # (tests constructing a ReactionSignal without it) working unchanged.
    vsol_lamports: int | None = None


def _decimal_or_none(raw: object) -> Decimal | None:
    if raw is None:
        return None
    return Decimal(str(raw))


def _vsol_lamports_or_none(raw: object) -> int | None:
    """Coerce a raw `vsol_lamports` value to a positive int, else None (never a fabricated reserve).

    A bool is refused (bool is an int subclass — money is never a flag), and a non-positive or
    non-integer reserve is treated as ABSENT (None): the curve carries no usable exit-liquidity
    signal, so downstream fails closed (censors) rather than inventing depth.
    """
    # money is integer base units: accept only an int or a Decimal-safe string, never a bool/float
    # (a float reserve is a schema violation -> ABSENT -> fail-closed censor downstream).
    if raw is None or isinstance(raw, bool) or not isinstance(raw, int | str):
        return None
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def bonding_curve_liquidity_usd(
    vsol_lamports: int | None, params: RealizableParams | None = None
) -> Decimal | None:
    """The bonding-curve exit-liquidity proxy (USD) from the curve's virtual SOL reserve.

    A pump.fun bonding curve is a two-sided constant-product AMM; the standard DexScreener-style
    `liquidity_usd` (which the realizable-exit `min_liquidity_usd_floor` is calibrated against) is
    ~2x the value of one side.  So `liquidity_usd ~= 2 * (vsol_lamports / 1e9) * sol_usd`.

    The SOL/USD rate is the SAME frozen `sol_usd_fallback` the realizable-exit layer uses when a
    mark carries no `price_usd` (the bonding-curve corpus never does), so the rate CANCELS in the
    slippage ratio `coeff * notional_usd / liquidity_usd = coeff * cap_sol / (2 * vsol_sol)` — the
    haircut is a pure pool-fraction, rate-independent, and only the (rate-scaled) `>= floor`
    sellability test depends on the exact rate.  Returns None when the reserve is absent/non-positive
    (no curve => no derivable exit liquidity => the caller fails closed).
    """
    if vsol_lamports is None or vsol_lamports <= 0:
        return None
    p = params or REALIZABLE_PARAMS
    vsol = Decimal(vsol_lamports) / Decimal(LAMPORTS_PER_SOL)
    return Decimal(2) * vsol * p.sol_usd_fallback


def parse_reaction_signal(obj: dict) -> ReactionSignal:
    """Parse one raw corpus dict into the SIGNAL-TIME view.

    Raises `ReactionCorpusError` (CENSORED) on a missing/invalid anchor, price, mint, source, or an
    unknown signal_type — never a fabricated anchor or fill.  `signal_block_time_ms` is taken
    DIRECTLY from the on-chain corpus (T-300a): the recorder already resolved it, so no RPC is
    needed and a null anchor is CENSORED here (fail-closed).
    """
    mint = str(obj.get("mint", "")).strip()
    if not mint:
        raise ReactionCorpusError("reaction record missing mint (CENSORED)")
    source_id = str(obj.get("source_id", "")).strip()
    if not source_id:
        raise ReactionCorpusError(f"{mint}: reaction record missing source_id (CENSORED)")
    signal_type = str(obj.get("signal_type", "")).strip()
    if signal_type not in VALID_SIGNAL_TYPES:
        raise ReactionCorpusError(
            f"{mint}: unknown/absent signal_type {signal_type!r} "
            f"(expected one of {sorted(VALID_SIGNAL_TYPES)}) (CENSORED)"
        )

    block_time_ms = obj.get("signal_block_time_ms")
    if block_time_ms is None:
        raise ReactionCorpusError(
            f"{mint}: signal_block_time_ms is null — no on-chain anchor (T-300a, CENSORED)"
        )
    slot = obj.get("signal_slot")
    if slot is None:
        raise ReactionCorpusError(f"{mint}: signal_slot is null (CENSORED)")
    try:
        block_time_ms_i = int(block_time_ms)
        slot_i = int(slot)
    except (TypeError, ValueError) as exc:
        raise ReactionCorpusError(
            f"{mint}: non-integer signal_slot/signal_block_time_ms (CENSORED)"
        ) from exc

    try:
        price = _decimal_or_none(obj.get("signal_price_sol"))
        size = _decimal_or_none(obj.get("signal_size_sol"))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ReactionCorpusError(
            f"{mint}: malformed signal_price_sol/signal_size_sol (CENSORED)"
        ) from exc
    if price is None or price <= 0:
        raise ReactionCorpusError(
            f"{mint}: non-positive/absent signal_price_sol — cannot price the entry (CENSORED)"
        )

    prior_raw = obj.get("source_prior")
    source_prior = float(prior_raw) if isinstance(prior_raw, int | float) else None

    return ReactionSignal(
        mint=mint,
        source_id=source_id,
        signal_type=signal_type,
        signal_slot=slot_i,
        signal_block_time_ms=block_time_ms_i,
        signal_price_sol=price,
        signal_size_sol=size,
        source_prior=source_prior,
        vsol_lamports=_vsol_lamports_or_none(obj.get("vsol_lamports")),
    )


def read_reaction_forward(
    obj: dict, *, curve_liquidity_usd: Decimal | None = None
) -> list[MomentumForwardObservation]:
    """Parse a reaction record's `forward` list into OUTCOME-time observations (bridges both shapes).

    Handles BOTH the SPEC/DexScreener shape (`txns_m5`, per-mark `liquidity_usd`) and the LIVE
    bonding-curve shape (`txns`, no `liquidity_usd`) without weakening the realizable-exit outcome
    model, and emits the SAME `MomentumForwardObservation` the realizable-exit walk consumes:
      * buy/sell pressure is read from `txns` OR `txns_m5` (whichever the recorder emitted);
      * a mark's exit liquidity is its OWN `liquidity_usd` when present, else the record-level
        `curve_liquidity_usd` proxy (from the signal's `vsol_lamports`) — so a bonding-curve mark is
        SELLABLE at the curve's real depth instead of being force-honeypotted by a missing DexScreener
        field.  A mark with no price (`price_sol is None`) stays unsellable (a rug) regardless.

    FAIL-CLOSED (never a fabricated outcome): a record with NO usable exit-liquidity signal at all —
    neither a per-mark `liquidity_usd` on ANY mark NOR a `curve_liquidity_usd` proxy — is CENSORED
    (`ReactionCorpusError`).  This is the "liquidity universally absent" guard: it drops the record
    rather than resolve every mark to a spurious total loss.  Malformed marks are CENSORED exactly as
    the launch/momentum reader does.  All forward marks are strictly POST-signal (the outcome
    partition); there is no pre-entry split for the reaction (the decision is AT the signal).
    """
    forward = obj.get("forward")
    mint = str(obj.get("mint", "?"))
    if not isinstance(forward, list) or not forward:
        raise ReactionCorpusError(f"{mint}: reaction record has no forward marks (CENSORED)")

    out: list[MomentumForwardObservation] = []
    any_mark_liquidity = False
    for obs in forward:
        # buy/sell pressure — LIVE `txns` first, then SPEC `txns_m5` (silent-drift fix): the live
        # bonding-curve recorder emits `txns`, so reading only `txns_m5` zeroed every pressure value.
        raw_txns = obs.get("txns")
        if not isinstance(raw_txns, dict):
            raw_txns = obs.get("txns_m5") or {}
        try:
            buys = int(raw_txns.get("buys", 0) or 0)
            sells = int(raw_txns.get("sells", 0) or 0)
        except (TypeError, ValueError):
            buys, sells = 0, 0

        mark_liq = _decimal_or_none(obs.get("liquidity_usd"))
        if mark_liq is not None:
            any_mark_liquidity = True
        # A mark's own DexScreener liquidity wins; else fall back to the bonding-curve proxy.  An
        # UNPRICED mark stays unsellable (None liquidity is irrelevant when price_sol is None).
        effective_liq = mark_liq if mark_liq is not None else curve_liquidity_usd

        try:
            parsed = MomentumForwardObservation(
                horizon_s=int(obs["horizon_s"]),
                price_sol=_decimal_or_none(obs.get("price_sol")),
                liquidity_usd=effective_liq,
                buys=buys,
                sells=sells,
                vol_m5=_decimal_or_none(obs.get("vol_m5")),
                note=str(obs.get("note", "")),
                obs_wall_ms=int(obs.get("obs_wall_ms", 0)),
                price_usd=_decimal_or_none(obs.get("price_usd")),
            )
        except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
            raise ReactionCorpusError(
                f"{mint}: malformed forward observation "
                f"(horizon_s={obs.get('horizon_s')!r}, price_sol={obs.get('price_sol')!r}) "
                "(CENSORED)"
            ) from exc
        out.append(parsed)

    if not any_mark_liquidity and curve_liquidity_usd is None:
        raise ReactionCorpusError(
            f"{mint}: no exit-liquidity signal — neither a per-mark liquidity_usd nor a "
            "bonding-curve vsol_lamports reserve. Cannot honestly price the exit; CENSORED "
            "(fail-closed) rather than resolve every mark to a fabricated total loss."
        )
    out.sort(key=lambda o: o.horizon_s)
    return out


def read_reaction_corpus(path: str | Path) -> Iterator[dict]:
    """Yield raw JSON dicts from a `reaction_corpus.jsonl` file (one object per line).

    A blank / malformed-JSON line is skipped (CENSORED) rather than crashing the run.  A missing
    file yields nothing (the caller fails closed on the empty set — no metric on no data).
    """
    p = Path(path)
    if not p.exists():
        return
    with p.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                yield obj


# ---------------------------------------------------------------------------
# Point-in-time decision features + the LEAK GUARD (boundary 1 — signal-anchored)
# ---------------------------------------------------------------------------


def assemble_reaction_features(
    signal: ReactionSignal,
    *,
    reputation: Decimal | None,
    extra_features: tuple[PITFeature, ...] = (),
) -> list[PITFeature]:
    """Assemble the SIGNAL-TIME decision features, EACH stamped at `signal_block_time_ms`.

    The walk-forward `reputation` is a legitimate signal-time feature: it is computed ONLY from
    same-source outcomes that were fully observed by `signal_block_time_ms` (leak boundary 2), so
    its event-time is the signal anchor.  `extra_features` is the extension/leak seam: injecting a
    feature built from a `forward` (post-signal) mark carries an event-time
    `signal_block_time + horizon_s*1000` > the cutoff and is exactly what
    `assert_features_leq_decision` catches (leak boundary 1).
    """
    bt = signal.signal_block_time_ms
    feats: list[PITFeature] = [
        PITFeature("signal_price_sol", float(signal.signal_price_sol), bt),
        PITFeature(
            "signal_size_sol",
            float(signal.signal_size_sol) if signal.signal_size_sol is not None else 0.0,
            bt,
        ),
        PITFeature("source_reputation", float(reputation) if reputation is not None else 0.0, bt),
    ]
    feats.extend(extra_features)
    return feats


# ---------------------------------------------------------------------------
# WALK-FORWARD source reputation (leak boundary 2 — strictly-prior + already-observed)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReputationInput:
    """One same-source PRIOR outcome eligible to inform a later decision's reputation.

    `signal_block_time_ms` is the prior signal's on-chain anchor; `outcome_resolved_block_time_ms`
    is when that prior outcome was fully realized (`anchor + max_horizon_s*1000`).  `net_pnl_per_sol`
    is the prior trade's realized net-PnL-per-SOL (exact Decimal, money — NOT a win-rate).
    """

    source_id: str
    signal_block_time_ms: int
    outcome_resolved_block_time_ms: int
    net_pnl_per_sol: Decimal


def assert_reputation_inputs_prior(
    inputs: Iterable[ReputationInput], decision_block_time_ms: int
) -> str:
    """LEAK AUDIT (boundary 2): every reputation input must be strictly-prior AND already-observed.

    Raises `ReputationLeakError` on the FIRST input whose prior signal is NOT strictly before the
    decision (`signal_block_time >= decision`: the source's own CURRENT signal or a FUTURE one) OR
    whose outcome was NOT yet realized at the decision instant
    (`outcome_resolved_block_time > decision`: an outcome-completion lookahead).  Returns a
    human-readable audit summary on success.
    """
    inputs = list(inputs)
    for pi in inputs:
        if pi.signal_block_time_ms >= decision_block_time_ms:
            raise ReputationLeakError(
                "REPUTATION LEAK: a same-source outcome with signal_block_time "
                f"{pi.signal_block_time_ms} ms >= decision {decision_block_time_ms} ms reached the "
                "reputation. A source's CURRENT or FUTURE signal cannot inform its own decision "
                "(walk-forward boundary breached)."
            )
        if pi.outcome_resolved_block_time_ms > decision_block_time_ms:
            raise ReputationLeakError(
                "REPUTATION LEAK: a prior outcome resolved at "
                f"{pi.outcome_resolved_block_time_ms} ms > decision {decision_block_time_ms} ms "
                "reached the reputation. Its outcome was not yet observable at the decision instant "
                "(outcome-completion lookahead breached)."
            )
    return (
        f"REPUTATION AUDIT PASS: {len(inputs)} same-source prior outcomes, all strictly prior "
        f"(signal_block_time < {decision_block_time_ms}) and already observed "
        f"(outcome_resolved <= {decision_block_time_ms}). No future outcome informed the decision."
    )


def source_reputation(
    prior_inputs: Iterable[ReputationInput], decision_block_time_ms: int
) -> tuple[Decimal | None, int, str]:
    """Compute the walk-forward source reputation = MEAN prior realized net-PnL-per-SOL.

    Runs `assert_reputation_inputs_prior` FIRST (leak boundary 2), then returns
    `(reputation, n_prior, audit_summary)`.  `reputation` is None (n_prior == 0) when the source has
    NO eligible prior outcome — a NEUTRAL PRIOR.  The aggregate is the MEAN of exact Decimal
    net-PnL-per-SOL ratios (money per unit risk) — never a win-rate / fraction-of-wins.
    """
    prior_inputs = list(prior_inputs)
    summary = assert_reputation_inputs_prior(prior_inputs, decision_block_time_ms)
    n_prior = len(prior_inputs)
    if n_prior == 0:
        return None, 0, summary
    total = sum((pi.net_pnl_per_sol for pi in prior_inputs), start=Decimal(0))
    return total / Decimal(n_prior), n_prior, summary


# ---------------------------------------------------------------------------
# The DECISION (signal-time ONLY) — model + "follow every signal" baseline
# ---------------------------------------------------------------------------


def apply_entry_latency_haircut(signal_price_sol: Decimal, params: ReactionParams) -> Decimal:
    """Worsen the entry fill for front-run latency: fill HIGHER than the signal tick.

    `entry_fill = signal_price_sol * (1 + haircut_fraction)`.  A real bot cannot fill at the exact
    signal tick — the reaction has already started moving the price by the time the order lands.
    Conservative: the haircut can only RAISE the entry price (lower realized PnL), never fabricate
    a GO.  Distinct from the ~6% cost stack's AMM entry slippage (that is execution slippage; this
    is the adverse price MOVE during the few-hundred-ms lag).
    """
    return signal_price_sol * (Decimal(1) + params.entry_latency_haircut_fraction)


@dataclass(frozen=True)
class ReactionDecision:
    """The signal-time decision: two selection masks + the (haircut) entry price + fixed risk.

    NO forward value.  `sol_at_risk_lamports` is a FIXED per-trade risk unit so both cohorts are
    compared on identical exposure (GATE-B isolates SELECTION skill, not sizing).  Baseline ALWAYS
    selects (follow every signal); the model may only DE-RISK (decline a proven-loser source).
    """

    model_selected: bool
    baseline_selected: bool
    entry_price_sol: Decimal  # after the front-run latency haircut
    sol_at_risk_lamports: int
    # audit-only (never a TradeOutcome field)
    reputation: Decimal | None
    n_prior_signals: int
    on_neutral_prior: bool
    reputation_gate_applied: bool
    leak_audit_summary: str
    reputation_audit_summary: str


def decide_reaction_entry(
    signal: ReactionSignal,
    *,
    reputation: Decimal | None,
    n_prior_signals: int,
    reputation_audit_summary: str,
    params: ReactionParams,
    risk_config: RiskConfig,
    extra_features: tuple[PITFeature, ...] = (),
) -> ReactionDecision:
    """Decide model + "follow every signal" baseline from SIGNAL-TIME data ONLY.

    Steps:
      1. assemble the signal-time decision features (each stamped at `signal_block_time_ms`);
      2. RUN THE LEAK GUARD (`assert_features_leq_decision`, cutoff = signal anchor) — a forward
         feature RAISES `LeakError` here (leak boundary 1);
      3. BASELINE selects ALWAYS (follow every recorded signal);
      4. MODEL: if the source has FEWER than `min_prior_signals` eligible prior outcomes -> NEUTRAL
         PRIOR (`neutral_prior_selects`); else follow iff `reputation >= reputation_threshold` (a
         proven-loser source is DECLINED — the model can only DE-RISK vs the baseline).

    `reputation` / `n_prior_signals` come from `source_reputation` (which already enforced leak
    boundary 2).  `extra_features` flows into step 1: injecting a forward-derived feature trips the
    guard in step 2 — the structural boundary the leak test proves.
    """
    features = assemble_reaction_features(
        signal, reputation=reputation, extra_features=extra_features
    )
    summary = assert_features_leq_decision(features, signal.signal_block_time_ms)

    entry_price = apply_entry_latency_haircut(signal.signal_price_sol, params)

    # BASELINE — follow EVERY signal (the GATE-B control).
    baseline_selected = True

    # MODEL — a strict SUBSET: decline only a source whose PROVEN track record is a net loser.
    on_neutral_prior = n_prior_signals < params.min_prior_signals
    if on_neutral_prior or reputation is None:
        model_selected = params.neutral_prior_selects
        gate_applied = False
    else:
        model_selected = reputation >= params.reputation_threshold
        gate_applied = True

    return ReactionDecision(
        model_selected=bool(model_selected),
        baseline_selected=baseline_selected,
        entry_price_sol=entry_price,
        sol_at_risk_lamports=risk_config.per_trade_cap_lamports,
        reputation=reputation,
        n_prior_signals=n_prior_signals,
        on_neutral_prior=on_neutral_prior,
        reputation_gate_applied=gate_applied,
        leak_audit_summary=summary,
        reputation_audit_summary=reputation_audit_summary,
    )


# ---------------------------------------------------------------------------
# The harness: recorded reaction corpus -> list[TradeOutcome]
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _ResolvedReaction:
    """Pass-1 resolution: a signal's identity/anchor + its REALIZED outcome (pure function of its
    OWN forward path — never enters its OWN decision; only feeds LATER same-source reputation)."""

    signal: ReactionSignal
    outcome_resolved_block_time_ms: int
    net_pnl_lamports: int
    sol_at_risk_lamports: int
    net_pnl_per_sol: Decimal


@dataclass(frozen=True)
class ReactionHarnessStats:
    """Book-keeping for one reaction run (never a win-rate — pure counts)."""

    n_records: int
    n_resolved: int
    n_censored: int
    n_sources: int
    n_model_selected: int
    n_baseline_selected: int
    n_on_neutral_prior: int  # decided on the neutral prior (too short a track record)
    n_reputation_gated: int  # decided by the reputation threshold (proven track record)
    n_declined_by_model: int  # signals the model dropped that the baseline followed
    censor_reasons: tuple[str, ...]
    # Per-outcome source_id, ALIGNED with the emitted `outcomes` list order.  Surfaced (not in the
    # TradeOutcome contract, which the sizer reads) so the runner can build a SOURCE-cluster
    # bootstrap for GATE-A/GATE-B — reaction signals are same-source-coupled via reputation, so an
    # iid resample understates variance.  Not a decision input; used only for the clustered CI.
    outcome_source_ids: tuple[str, ...]
    # Per-outcome ON-CHAIN event-time anchors, ALIGNED with `outcomes` (same order).  Surfaced for
    # the CAPITAL-LICENSING purged/embargoed WALK-FORWARD (aats.backtest.licensing): the walk-forward
    # orders decisions by `event_time_ms` (= signal_block_time_ms) into time folds and PURGES a
    # decision whose label horizon (`label_horizon_end_ms` = signal_block_time + max_horizon_s*1000)
    # overlaps a fold boundary.  These are OUTCOME/windowing metadata (never a decision feature and
    # never part of the TradeOutcome contract), defaulted to () so positional callers stay valid.
    outcome_event_times_ms: tuple[int, ...] = ()
    outcome_label_horizon_end_ms: tuple[int, ...] = ()

    @property
    def reputation_engaged(self) -> bool:
        """True iff the walk-forward reputation gate ACTUALLY fired on >= 1 decision.

        DATA-SUFFICIENCY CAVEAT (MINOR, surfaced so a null delta is not misread): when this is
        False, EVERY decision rode the neutral prior (no source accrued `min_prior_signals`
        observed prior outcomes), so the model is byte-identical to the "follow every signal"
        baseline and GATE-B delta is exactly 0 by construction.  A delta of 0 here means the
        selection filter NEVER GOT TO ACT (too few repeat-source signals), NOT that source
        selection has no skill.  The reaction edge is untestable until repeat-source density grows.
        """
        return self.n_reputation_gated > 0


def _resolve_one(
    obj: dict,
    *,
    params: ReactionParams,
    cost_stack: CostStack,
    risk_config: RiskConfig,
    exit_model: str,
) -> _ResolvedReaction:
    """Parse + resolve ONE reaction record's realized outcome (OUTCOME step — forward-time only).

    The entry fill is the front-run-latency-haircut signal price (from the SAME `params` object the
    DECISION uses — so an entry_latency_haircut_bps override changes the realized outcome, never the
    frozen global silently); the forward marks (all strictly post-signal) are walked through the SAME
    realizable-exit engine as the launch/momentum proofs.  Raises `ReactionCorpusError` (CENSORED) on
    unusable data — never a fabricated outcome.
    """
    signal = parse_reaction_signal(obj)
    # Bonding-curve exit-liquidity proxy from the SIGNAL-time reserve (OUTCOME-only; not lookahead,
    # never a decision feature).  None on DexScreener-shape corpora that carry per-mark liquidity.
    curve_liq = bonding_curve_liquidity_usd(signal.vsol_lamports)
    forward = read_reaction_forward(obj, curve_liquidity_usd=curve_liq)  # all strictly post-signal
    entry_price = apply_entry_latency_haircut(signal.signal_price_sol, params)

    outcome = resolve_momentum_outcome(
        signal.mint,
        entry_price,
        forward,
        cost_stack=cost_stack,
        sol_at_risk_lamports=risk_config.per_trade_cap_lamports,
        decision_block_time_ms=signal.signal_block_time_ms,
        exit_model=exit_model,
    )
    max_horizon_s = max(o.horizon_s for o in forward)
    resolved_time = signal.signal_block_time_ms + max_horizon_s * 1000
    sol_at_risk = risk_config.per_trade_cap_lamports
    per_sol = Decimal(outcome.net_pnl_lamports) / Decimal(sol_at_risk)
    return _ResolvedReaction(
        signal=signal,
        outcome_resolved_block_time_ms=resolved_time,
        net_pnl_lamports=outcome.net_pnl_lamports,
        sol_at_risk_lamports=sol_at_risk,
        net_pnl_per_sol=per_sol,
    )


def _walk_forward_reputations(
    resolved: list[_ResolvedReaction],
    by_source: dict[str, list[_ResolvedReaction]],
) -> list[tuple[Decimal | None, int, str]]:
    """Walk-forward source reputation for EVERY resolved signal, in NEAR-LINEAR time.

    Returns a list aligned with `resolved`: `(reputation, n_prior, audit_summary)` per signal —
    BYTE-IDENTICAL to calling `source_reputation` on the strictly-prior + already-observed same-source
    outcomes per decision, but WITHOUT the per-decision O(k) rescan of every same-source outcome
    (the previous quadratic-per-source path).  The mean of a fixed set of exact-Decimal ratios is
    order-independent, so the accumulated sum equals the per-decision sum exactly.

    METHOD (leak boundary 2, STRUCTURALLY enforced — a perf refactor, never a weakening):
    `resolved` is in global `(block_time, slot, mint)` order, so each source's list in `by_source`
    is in that order too.  Per source we keep a pointer into its signal list and a min-heap
    `pending` of strictly-prior outcomes keyed by observation time
    (`outcome_resolved_block_time_ms`).  For a decision at `decision_bt`:
      1. ADMIT into `pending` every same-source signal with `signal_block_time < decision_bt`.  A
         signal that appears AFTER this one in global order has `block_time >= decision_bt` (the sort
         key), so it can never be admitted for this decision — the admitted prefix IS exactly the
         strictly-prior set the old filter selected.
      2. RELEASE from `pending` every outcome with `outcome_resolved <= decision_bt` into the running
         `(sum, count)` accumulator.  A prior whose forward window extends past the decision stays
         pending (not-yet-observed) and is released only at a LATER decision that observes it — this
         is why a HEAP (not a second pointer) is used: `outcome_resolved` is NOT monotonic in signal
         order when forward windows differ in length.
      3. reputation = mean accumulated net-PnL-per-SOL (`None` when count == 0 -> NEUTRAL PRIOR).
    Every released input is re-asserted strictly-prior + already-observed via
    `assert_reputation_inputs_prior` (the SAME guard `source_reputation` uses), so a logic slip that
    admitted a non-prior / not-yet-observed outcome raises `ReputationLeakError` = FAIL.
    """
    ptr: dict[str, int] = {src: 0 for src in by_source}
    pending: dict[str, list[tuple[int, int, ReputationInput]]] = {src: [] for src in by_source}
    acc_sum: dict[str, Decimal] = {src: Decimal(0) for src in by_source}
    acc_count: dict[str, int] = {src: 0 for src in by_source}
    seq = 0  # monotonic heap tiebreak so ReputationInput is never compared (stable, deterministic)
    out: list[tuple[Decimal | None, int, str]] = []
    for r in resolved:
        src = r.signal.source_id
        decision_bt = r.signal.signal_block_time_ms
        lst = by_source[src]
        heap = pending[src]
        i = ptr[src]
        # (1) admit the strictly-prior prefix (signal_block_time < decision) into pending.
        while i < len(lst) and lst[i].signal.signal_block_time_ms < decision_bt:
            o = lst[i]
            heapq.heappush(
                heap,
                (
                    o.outcome_resolved_block_time_ms,
                    seq,
                    ReputationInput(
                        source_id=o.signal.source_id,
                        signal_block_time_ms=o.signal.signal_block_time_ms,
                        outcome_resolved_block_time_ms=o.outcome_resolved_block_time_ms,
                        net_pnl_per_sol=o.net_pnl_per_sol,
                    ),
                ),
            )
            seq += 1
            i += 1
        ptr[src] = i
        # (2) release every ALREADY-OBSERVED prior (outcome_resolved <= decision) into the accumulator.
        while heap and heap[0][0] <= decision_bt:
            _, _, pi = heapq.heappop(heap)
            assert_reputation_inputs_prior([pi], decision_bt)  # structural guard (leak boundary 2)
            acc_sum[src] += pi.net_pnl_per_sol
            acc_count[src] += 1
        n_prior = acc_count[src]
        reputation = (acc_sum[src] / Decimal(n_prior)) if n_prior else None
        summary = (
            f"REPUTATION AUDIT PASS (walk-forward accumulator): {n_prior} same-source prior "
            f"outcomes, all strictly prior (signal_block_time < {decision_bt}) and already observed "
            f"(outcome_resolved <= {decision_bt}). No future outcome informed the decision."
        )
        out.append((reputation, n_prior, summary))
    return out


def build_reaction_outcomes(
    records: Iterable[dict],
    *,
    params: ReactionParams | None = None,
    risk_config: RiskConfig | None = None,
    cost_stack: CostStack | None = None,
    exit_model: str = EXIT_MODEL_REALIZABLE,
) -> tuple[list[TradeOutcome], ReactionHarnessStats]:
    """Turn recorded SIGNAL events into `TradeOutcome` records under the FRONT-RUN strategy.

    TWO passes keep the walk-forward reputation leak-free:
      * PASS 1 resolves EVERY signal's realized outcome (`_resolve_one`) — a pure function of that
        signal's OWN forward path, independent of any decision.  Unusable records are CENSORED.
      * PASS 2 walks the resolved signals in on-chain (block_time, slot) order and, for each signal,
        computes the WALK-FORWARD source reputation from the SAME source's STRICTLY-PRIOR,
        ALREADY-OBSERVED outcomes (leak boundary 2, `_walk_forward_reputations` — a near-linear
        accumulator whose values equal the per-decision `source_reputation` filter and which
        re-asserts every input via `assert_reputation_inputs_prior`), then DECIDES from signal-time
        data only (leak boundary 1, `decide_reaction_entry`), emitting ONE `TradeOutcome`.

    The DECISION and the OUTCOME of the SAME signal never share an argument (structural boundary 1);
    a signal's reputation never sees its own or any future same-source outcome (structural boundary
    2).  `exit_model` selects OUTCOME fidelity (default `'realizable'`); it never touches either
    boundary.  Returns (outcomes, stats).
    """
    p = params or REACTION_PARAMS
    rconfig = risk_config or RiskConfig()
    cstack = cost_stack or build_round_trip_cost_stack()
    if exit_model not in EXIT_MODELS:
        raise ValueError(f"unknown exit_model {exit_model!r} (expected one of {EXIT_MODELS})")

    # PASS 1 — resolve every signal's realized outcome (no decision surface here).
    resolved: list[_ResolvedReaction] = []
    n_records = 0
    censor_reasons: list[str] = []
    for obj in records:
        n_records += 1
        try:
            resolved.append(
                _resolve_one(
                    obj,
                    params=p,
                    cost_stack=cstack,
                    risk_config=rconfig,
                    exit_model=exit_model,
                )
            )
        except (ReactionCorpusError, CorpusRecordError) as exc:
            censor_reasons.append(f"{obj.get('mint', '?')}: {exc}")
            continue

    # Deterministic on-chain order (block_time, slot, mint tiebreak) so equal-anchor ties are
    # resolved reproducibly and a same-anchor signal is NEVER counted as prior (strict < on time).
    resolved.sort(
        key=lambda r: (
            r.signal.signal_block_time_ms,
            r.signal.signal_slot,
            r.signal.mint,
        )
    )

    # Index each source's resolved outcomes in the SAME global (block_time, slot, mint) order so the
    # walk-forward reputation can advance a per-source pointer (near-linear, no per-decision rescan).
    by_source: dict[str, list[_ResolvedReaction]] = {}
    for r in resolved:
        by_source.setdefault(r.signal.source_id, []).append(r)

    # Walk-forward source reputation for EVERY signal (leak boundary 2, structurally guarded), in
    # near-linear time (see _walk_forward_reputations) — identical values to the per-decision filter.
    reputations = _walk_forward_reputations(resolved, by_source)

    # PASS 2 — decide each signal from its walk-forward reputation (leak-guarded both ways).
    outcomes: list[TradeOutcome] = []
    source_ids: list[str] = []
    event_times_ms: list[int] = []
    label_horizon_end_ms: list[int] = []
    n_model = 0
    n_baseline = 0
    n_neutral = 0
    n_gated = 0
    n_declined = 0
    for r, (reputation, n_prior, rep_summary) in zip(resolved, reputations, strict=True):
        sig = r.signal
        decision = decide_reaction_entry(
            sig,
            reputation=reputation,
            n_prior_signals=n_prior,
            reputation_audit_summary=rep_summary,
            params=p,
            risk_config=rconfig,
        )
        outcomes.append(
            TradeOutcome(
                mint=sig.mint,
                decision_slot=sig.signal_slot,
                model_selected=decision.model_selected,
                baseline_selected=decision.baseline_selected,
                net_pnl_lamports=r.net_pnl_lamports,
                sol_at_risk_lamports=r.sol_at_risk_lamports,
            )
        )
        source_ids.append(sig.source_id)
        event_times_ms.append(sig.signal_block_time_ms)
        label_horizon_end_ms.append(r.outcome_resolved_block_time_ms)
        n_model += int(decision.model_selected)
        n_baseline += int(decision.baseline_selected)
        n_neutral += int(decision.on_neutral_prior)
        n_gated += int(decision.reputation_gate_applied)
        n_declined += int(decision.baseline_selected and not decision.model_selected)

    stats = ReactionHarnessStats(
        n_records=n_records,
        n_resolved=len(outcomes),
        n_censored=len(censor_reasons),
        n_sources=len(by_source),
        n_model_selected=n_model,
        n_baseline_selected=n_baseline,
        n_on_neutral_prior=n_neutral,
        n_reputation_gated=n_gated,
        n_declined_by_model=n_declined,
        censor_reasons=tuple(censor_reasons),
        outcome_source_ids=tuple(source_ids),
        outcome_event_times_ms=tuple(event_times_ms),
        outcome_label_horizon_end_ms=tuple(label_horizon_end_ms),
    )
    return outcomes, stats


def build_reaction_from_corpus(
    corpus_path: str | Path,
    *,
    params: ReactionParams | None = None,
    risk_config: RiskConfig | None = None,
    cost_stack: CostStack | None = None,
    exit_model: str = EXIT_MODEL_REALIZABLE,
) -> tuple[list[TradeOutcome], ReactionHarnessStats]:
    """Convenience: read a reaction corpus file and build the TradeOutcome list in one call.

    No `block_time_resolver`: the reaction corpus already carries the on-chain
    `signal_block_time_ms` (T-300a), so there is nothing to resolve via RPC — a null anchor is
    CENSORED at parse time (fail-closed).
    """
    return build_reaction_outcomes(
        read_reaction_corpus(corpus_path),
        params=params,
        risk_config=risk_config,
        cost_stack=cost_stack,
        exit_model=exit_model,
    )


__all__ = [
    "REACTION_PARAMS",
    "VALID_SIGNAL_TYPES",
    "ReactionParams",
    "ReactionParamsError",
    "ReactionCorpusError",
    "ReputationLeakError",
    "ReactionSignal",
    "ReactionDecision",
    "ReactionHarnessStats",
    "ReputationInput",
    "LeakError",
    "load_reaction_params",
    "parse_reaction_signal",
    "bonding_curve_liquidity_usd",
    "read_reaction_forward",
    "read_reaction_corpus",
    "assemble_reaction_features",
    "assert_reputation_inputs_prior",
    "source_reputation",
    "apply_entry_latency_haircut",
    "decide_reaction_entry",
    "build_reaction_outcomes",
    "build_reaction_from_corpus",
]
