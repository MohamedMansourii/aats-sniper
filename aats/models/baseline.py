"""Naive-momentum baseline — FROZEN, hashed config (C-4, T-311).

WHAT THIS IS
============
The GATE-B control. The snipe model has NO license to trade unless it beats THIS
baseline, net of cost, on recorded data (EDGE-VERDICT.md GATE-B / §5 K-1). A model
that cannot beat dumb momentum is not a model — it is overfitting.

The baseline rule (data-models.md §3.2, walk-forward §4):

    "Enter every candidate with positive first-K-slot net buy pressure above a
     fixed percentile."

It is CONSTRUCTIBLE from the first-K buy-pressure feature ALONE:
  - first_k_buy_pressure   (Decimal, net SOL pressure over first K slots)
  - first_k_volume_lamports (int, total volume over first K slots)
Both come from aats.features.buy_pressure (T-305). NO future data, NO labels, NO
model output. Point-in-time correct by construction: the inputs already passed the
per-feature provenance cutoff guard (buy_pressure.py), so a future-slot datum cannot
reach this rule.

WHY FROZEN (C-4, red-team flaw 2A)
==================================
A baseline whose knobs (K, percentile, unit-of-risk, universe) are tuned AFTER seeing
model results is a tautology, not a control — it can always be made to lose. So the
params live in a committed, hashed config (`baseline.frozen.json`) and are pinned at
the FIRST fit. `assert_frozen_or_freeze()` computes a canonical hash of the params and:
  - on the first fit, stamps the hash into the config (freeze);
  - on every subsequent fit, RAISES BaselineFrozenError if the live params hash differs
    from the stored `frozen_hash` (`baseline_changed_after_fit`).
The hash-freeze test (tests/models) drives this: changing any param after first fit
makes the test FAIL deterministically.

WHAT THIS IS NOT (boundaries — non-waivable)
============================================
  - NOT a price. The baseline emits a SELECTION (0/1), a momentum score, and a
    baseline probability proxy in [0,1] — never a point price (locked decision 9).
  - NOT a trade decision and NOT a size. Whether to enter, how much, where the stop
    sits — all downstream (OMS / sizer). This module never sizes, never widens a stop,
    never overrides anything. It only DE-RISKs in the sense that it is a gate input.
  - NOT a capital path. This is an OFFLINE / VALIDATION artifact (BLUEPRINT line 49:
    loop = "offline/validation"). It never builds, signs, or lands a transaction and
    never touches a keypair. Real capital stays DISABLED behind DRY-RUN by default
    (api_schemas.AgentMode); this module has no submit path to disable.
  - NO win-rate. The sole metrics are net-of-cost PnL + model-vs-baseline (EDGE-VERDICT
    §4). There is no win-rate target, field, or tuning objective anywhere here.

MONEY DISCIPLINE (data-models.md §0, non-negotiable)
====================================================
  - first_k_buy_pressure        : Decimal (exact net quantity), NEVER float.
  - first_k_volume_lamports      : int (lamports), NEVER float.
  - reference_threshold_lamports : Decimal-as-string in config, parsed to Decimal here.
All comparisons are exact Decimal/int — no float ever touches the selection rule.

INJECTABLE / OFFLINE
====================
No I/O on the hot path of construction: the config path is injectable (load_config),
and the only data the rule consumes is the BuyPressureFeatures object the caller
passes in. No RPC, no LLM, no Geyser. Deterministic and reproducible from the pinned
config + the feature object alone.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from aats.features.buy_pressure import BuyPressureFeatures

# ---------------------------------------------------------------------------
# Canonical config location (injectable — callers may pass an alternate path)
# ---------------------------------------------------------------------------

DEFAULT_CONFIG_PATH = Path(__file__).with_name("baseline.frozen.json")

# The exact, ORDERED set of param keys that are hashed into the freeze.
# Adding/removing a key here is a deliberate schema change (bump schema_version);
# it is NOT a silent edit. The order is fixed so the canonical hash is stable.
_HASHED_PARAM_KEYS: tuple[str, ...] = (
    "first_k_slots",
    "selection_percentile",
    "unit_of_risk",
    "candidate_universe",
    "feature_schema_version",
    "min_volume_lamports",
    "reference_threshold_lamports",
)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class BaselineFrozenError(RuntimeError):
    """Raised when the live baseline params hash != the stored frozen hash.

    This is the C-4 `baseline_changed_after_fit` failure. It fires when any param
    in `_HASHED_PARAM_KEYS` changes AFTER the baseline was first frozen. The fix is
    NOT to edit the freeze — it is to recognise that altering a frozen baseline after
    a model fit is p-hacking the control, and is forbidden (validation-harness.md §4
    C-4, §7).
    """

    def __init__(self, live_hash: str, frozen_hash: str, changed_keys: list[str]) -> None:
        self.live_hash = live_hash
        self.frozen_hash = frozen_hash
        self.changed_keys = changed_keys
        super().__init__(
            "baseline_changed_after_fit: the naive-momentum baseline params changed "
            f"after first fit.\n  frozen_hash = {frozen_hash}\n  live_hash   = {live_hash}\n"
            f"  changed/uncertain keys = {sorted(changed_keys) if changed_keys else '<hash differs>'}\n"
            "A frozen baseline is the GATE-B control (EDGE-VERDICT.md). Tuning it after a "
            "model fit makes the model trivially 'beat' it (red-team flaw 2A). This is a "
            "TEST FAILURE, not an edit — revert the params or open an ADR + delta notice "
            "(validation-harness.md §7)."
        )


class BaselineConfigError(ValueError):
    """Raised when the frozen config is structurally invalid (missing keys, bad money type)."""


# ---------------------------------------------------------------------------
# Canonical hashing — deterministic, reproducible, order-independent of JSON layout
# ---------------------------------------------------------------------------


def canonical_params_hash(params: dict[str, Any]) -> str:
    """Compute the canonical SHA-256 of the hashed param subset.

    The hash is over a canonical JSON encoding of ONLY the `_HASHED_PARAM_KEYS`,
    with sorted keys and no whitespace, so it is invariant to JSON formatting and
    key ordering in the file. Money/Decimal-as-string values are hashed as their
    exact string form (never coerced to float).

    Reproducible: the same params always produce the same hash, on any machine,
    any PYTHONHASHSEED.
    """
    missing = [k for k in _HASHED_PARAM_KEYS if k not in params]
    if missing:
        raise BaselineConfigError(
            f"frozen config `params` is missing hashed key(s): {missing}. "
            f"Required hashed keys: {list(_HASHED_PARAM_KEYS)}."
        )
    # Build the canonical subset in fixed key order; values normalised to their
    # exact string form for money fields so no float ever enters the digest.
    canonical: dict[str, Any] = {}
    for key in _HASHED_PARAM_KEYS:
        val = params[key]
        if key == "reference_threshold_lamports":
            # Money: must be Decimal-as-string; reject float explicitly.
            if isinstance(val, float):
                raise BaselineConfigError(
                    "reference_threshold_lamports must be a Decimal string, not float "
                    f"(got {val!r}). Money is integer base units / Decimal (data-models §0)."
                )
            # Normalise via Decimal so '300000000' and '300000000.0'(str) are distinct
            # but the canonical form is exact and reproducible.
            canonical[key] = str(Decimal(str(val)))
        elif key == "min_volume_lamports":
            if isinstance(val, float):
                raise BaselineConfigError(
                    "min_volume_lamports must be int (lamports), not float "
                    f"(got {val!r}). Money is integer base units (data-models §0)."
                )
            canonical[key] = int(val)
        else:
            canonical[key] = val
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Frozen params (typed, money-correct)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BaselineParams:
    """The frozen, money-correct baseline parameters.

    Loaded from `baseline.frozen.json`. All money fields are int / Decimal.
    `params_hash` is the canonical hash of these params (the freeze identity).
    """

    first_k_slots: int
    selection_percentile: float  # a percentile (0..100), not money — float is correct
    unit_of_risk: str
    candidate_universe: str
    feature_schema_version: str
    min_volume_lamports: int  # lamports, integer
    reference_threshold_lamports: Decimal  # lamports, exact (Decimal), never float
    params_hash: str

    def __post_init__(self) -> None:
        if not (0.0 <= self.selection_percentile <= 100.0):
            raise BaselineConfigError(
                f"selection_percentile must be in [0,100], got {self.selection_percentile}"
            )
        if self.first_k_slots <= 0:
            raise BaselineConfigError(f"first_k_slots must be > 0, got {self.first_k_slots}")
        if self.min_volume_lamports < 0:
            raise BaselineConfigError(
                f"min_volume_lamports must be >= 0, got {self.min_volume_lamports}"
            )


# ---------------------------------------------------------------------------
# Config load / freeze
# ---------------------------------------------------------------------------


def _read_config(config_path: Path) -> dict[str, Any]:
    if not config_path.exists():
        raise BaselineConfigError(f"frozen baseline config not found at {config_path}")
    with config_path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def load_params(config_path: Path = DEFAULT_CONFIG_PATH) -> BaselineParams:
    """Load and type the frozen params (does NOT freeze; see assert_frozen_or_freeze).

    Reads money fields as int / Decimal. Computes the canonical params_hash.
    Raises BaselineConfigError on any float-money or missing-key violation.
    """
    raw = _read_config(config_path)
    params = raw.get("params")
    if not isinstance(params, dict):
        raise BaselineConfigError("frozen config must contain a `params` object")

    # Float-money guard at load (defense-in-depth with canonical_params_hash).
    for money_key in ("min_volume_lamports",):
        if isinstance(params.get(money_key), float):
            raise BaselineConfigError(
                f"Money field '{money_key}' must be int (lamports), got float "
                f"{params[money_key]!r} (data-models §0)."
            )
    if isinstance(params.get("reference_threshold_lamports"), float):
        raise BaselineConfigError(
            "reference_threshold_lamports must be Decimal-string, got float "
            f"{params['reference_threshold_lamports']!r} (data-models §0)."
        )

    params_hash = canonical_params_hash(params)

    return BaselineParams(
        first_k_slots=int(params["first_k_slots"]),
        selection_percentile=float(params["selection_percentile"]),
        unit_of_risk=str(params["unit_of_risk"]),
        candidate_universe=str(params["candidate_universe"]),
        feature_schema_version=str(params["feature_schema_version"]),
        min_volume_lamports=int(params["min_volume_lamports"]),
        reference_threshold_lamports=Decimal(str(params["reference_threshold_lamports"])),
        params_hash=params_hash,
    )


def assert_frozen_or_freeze(
    config_path: Path = DEFAULT_CONFIG_PATH,
    *,
    now_ms: int | None = None,
) -> BaselineParams:
    """The C-4 freeze gate. Call this BEFORE any model fit.

    Behaviour:
      - If the config has NOT yet been frozen (`frozen_hash` is null), this stamps the
        canonical params hash into the config, sets `frozen_at_first_fit=true`, and
        records `first_fit_recorded_at_ms`. This is the ONE legal write to the freeze.
      - If the config IS already frozen, this RECOMPUTES the live params hash and
        RAISES BaselineFrozenError if it differs from the stored `frozen_hash`
        (`baseline_changed_after_fit`). No write occurs.

    Returns the typed, frozen BaselineParams. The returned `params_hash` always equals
    the (now-)stored `frozen_hash`.

    Reproducible / injectable: `config_path` and `now_ms` are injectable so tests are
    deterministic and offline (no wall-clock dependency unless none is provided).
    """
    raw = _read_config(config_path)
    params = raw.get("params")
    if not isinstance(params, dict):
        raise BaselineConfigError("frozen config must contain a `params` object")

    live_hash = canonical_params_hash(params)
    stored_hash = raw.get("frozen_hash")

    if stored_hash in (None, "", "null"):
        # First fit — freeze now.
        stamp_ms = now_ms if now_ms is not None else int(time.time() * 1000)
        raw["frozen_hash"] = live_hash
        raw["frozen_at_first_fit"] = True
        raw["first_fit_recorded_at_ms"] = stamp_ms
        with config_path.open("w", encoding="utf-8") as fh:
            json.dump(raw, fh, indent=2)
            fh.write("\n")
    elif stored_hash != live_hash:
        # Already frozen and params changed — HARD FAIL (C-4).
        # Report which hashed keys to inspect (best-effort: we cannot reconstruct the
        # original params from a hash, so we report all hashed keys as the audit set).
        raise BaselineFrozenError(
            live_hash=live_hash,
            frozen_hash=str(stored_hash),
            changed_keys=list(_HASHED_PARAM_KEYS),
        )
    # else: already frozen and unchanged — no-op, returns the frozen params.

    return load_params(config_path)


# ---------------------------------------------------------------------------
# The baseline signal (NEVER a price, NEVER a size, NEVER a decision)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BaselineSignal:
    """Output of the naive-momentum baseline for ONE candidate.

    Fields
    ------
    mint : str
        The candidate's mint (routed from the FeatureFrame / BuyPressureFeatures).
    selected : bool
        The baseline's binary selection: True iff net buy pressure is positive AND
        >= the frozen reference threshold AND volume >= min_volume_lamports.
        This is a GATE signal, not a buy order.
    momentum_score : Decimal
        The raw net buy pressure (lamports, exact Decimal). This is the "momentum"
        the baseline ranks on. Money — Decimal, never float.
    baseline_p : float
        A baseline PROBABILITY PROXY in [0,1] for the DecisionSignal.baseline_p
        contract (data-models §4). It is a monotone, bounded transform of the
        momentum relative to the frozen threshold — NOT a calibrated model output,
        NOT a price. 1.0 means "well above threshold", 0.0 means "at/below zero".
    first_k_slots : int
        K used (echoes the frozen param; must match the feature's window).
    params_hash : str
        The frozen params hash this signal was produced under (audit trail).

    NO price field. NO size field. NO trade decision field. (Locked decision 9.)
    """

    mint: str
    selected: bool
    momentum_score: Decimal
    baseline_p: float
    first_k_slots: int
    params_hash: str


def _baseline_probability_proxy(net_pressure: Decimal, threshold: Decimal) -> float:
    """Map net buy pressure to a bounded [0,1] proxy, monotone in pressure.

    This is a PROXY for the DecisionSignal.baseline_p contract field — a number the
    GATE-B comparison can place beside the model's calibrated probability. It is NOT a
    calibrated probability and is NEVER a price.

    Mapping (deterministic, reproducible):
      - net_pressure <= 0           -> 0.0   (no buying pressure)
      - net_pressure >= 2*threshold -> 1.0   (strong buying pressure, saturates)
      - linear in between (relative to the frozen threshold band)
    When threshold <= 0 the band degenerates; we then use a simple sign indicator
    (1.0 if net_pressure > 0 else 0.0) so the proxy stays well-defined.
    """
    if net_pressure <= 0:
        return 0.0
    if threshold <= 0:
        return 1.0
    saturate_at = threshold * 2
    if net_pressure >= saturate_at:
        return 1.0
    # Linear interpolation in exact Decimal, then a single, final float cast for the
    # [0,1] proxy field (the proxy is a probability-like float by contract, not money).
    frac = net_pressure / saturate_at
    return float(frac)


def evaluate_baseline(
    features: BuyPressureFeatures,
    params: BaselineParams,
    *,
    mint: str,
) -> BaselineSignal:
    """Evaluate the frozen naive-momentum baseline on ONE candidate's features.

    The ONLY inputs are the frozen params and the first-K buy-pressure features —
    no future data, no labels, no model output. Point-in-time correct because
    `features` already passed the per-feature provenance cutoff guard at construction
    (buy_pressure.py); this rule reads only those already-clean values.

    Selection rule (data-models §3.2):
        selected := (net_buy_pressure > 0)
                    AND (net_buy_pressure >= reference_threshold_lamports)
                    AND (first_k_volume_lamports >= min_volume_lamports)

    All comparisons are exact Decimal/int. No float touches the selection.

    Parameters
    ----------
    features : BuyPressureFeatures
        Output of build_buy_pressure_features / BuyPressureAccumulator.to_features.
        Carries first_k_buy_pressure (Decimal) and first_k_volume_lamports (int).
        (BuyPressureFeatures carries no mint of its own — the caller routes it.)
    params : BaselineParams
        The frozen, hashed baseline params (from assert_frozen_or_freeze / load_params).
    mint : str
        The candidate's mint, supplied by the caller (the feature object does not
        carry it). Used for attribution / audit only — never for selection.

    Returns
    -------
    BaselineSignal
        selection + momentum score + baseline_p proxy. NEVER a price or a size.

    Raises
    ------
    BaselineConfigError
        If the feature window K does not match the frozen first_k_slots (a baseline
        evaluated on a different window than it was frozen for is not the frozen
        baseline — refuse rather than silently mis-compare).
    """
    if features.first_k_slots != params.first_k_slots:
        raise BaselineConfigError(
            f"feature window K={features.first_k_slots} does not match frozen baseline "
            f"first_k_slots={params.first_k_slots}. The baseline must be evaluated on the "
            "exact window it was frozen for (C-4). Recompute features at the frozen K."
        )

    net_pressure: Decimal = features.first_k_buy_pressure  # Decimal, exact
    volume: int = features.first_k_volume_lamports  # int lamports
    threshold: Decimal = params.reference_threshold_lamports  # Decimal, exact

    selected = (
        net_pressure > 0
        and net_pressure >= threshold
        and volume >= params.min_volume_lamports
    )

    baseline_p = _baseline_probability_proxy(net_pressure, threshold)

    return BaselineSignal(
        mint=mint,
        selected=selected,
        momentum_score=net_pressure,
        baseline_p=baseline_p,
        first_k_slots=params.first_k_slots,
        params_hash=params.params_hash,
    )
