"""Tests for the FROZEN naive-momentum baseline (T-311, C-4).

Self-check criteria (charter §6; EDGE-VERDICT C-4; validation-harness §4 C-4):
  1. The committed baseline.frozen.json is loadable and money-correct (int/Decimal).
  2. assert_frozen_or_freeze() freezes on first fit (stamps the canonical hash).
  3. HASH-FREEZE: after first fit, changing ANY hashed param FAILS with
     BaselineFrozenError (`baseline_changed_after_fit`).  This is the headline C-4 test.
  4. The baseline is CONSTRUCTIBLE from the first-K buy-pressure feature ONLY
     (no future data, no labels, no model output) — evaluate_baseline on a
     BuyPressureFeatures object produces a selection + momentum score.
  5. Money discipline: a float in a money param is REJECTED (never coerced).
  6. The canonical hash is deterministic / reproducible (same params -> same hash,
     independent of JSON key order / whitespace).
  7. No price field, no size field, no trade-decision field on the output
     (BaselineSignal); the baseline NEVER emits a price (locked decision 9).
  8. Re-freeze is idempotent: re-running assert_frozen_or_freeze on an already-frozen,
     UNCHANGED config is a no-op (no spurious failure, no rewrite of the hash).
  9. A feature window K mismatch is refused (the frozen baseline is only valid on its
     frozen K).

Point-in-time: the only inputs to the rule are the frozen params + the first-K
buy-pressure feature object (already provenance-guarded at construction).  There is
NO path through which future data, a label, or a model output reaches the baseline.

Offline / injectable: every freeze/mutate test runs on a tmp_path COPY of the config
(now_ms injected) so the committed baseline.frozen.json is never mutated and no
wall-clock is read.  No RPC, no LLM, no network.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from aats.contracts.events import DetectionTransport, EventTime, LaunchEvent, LaunchSource
from aats.features.buy_pressure import build_buy_pressure_features
from aats.models.baseline import (
    DEFAULT_CONFIG_PATH,
    BaselineConfigError,
    BaselineFrozenError,
    BaselineSignal,
    assert_frozen_or_freeze,
    canonical_params_hash,
    evaluate_baseline,
    load_params,
)

# ---------------------------------------------------------------------------
# Deterministic fixtures (no wall-clock, no network) — mirror test_buy_pressure
# ---------------------------------------------------------------------------

BASE_SLOT = 300_000_000
BASE_BLOCK_TIME_MS = 1_718_700_000_000
LAMPORTS_PER_SOL = 1_000_000_000
FIRST_K_SLOTS = 10
MINT_A = "MintAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
WALLET_BUYER_1 = "BuyerWallet111111111111111111111111111111111"
WALLET_BUYER_2 = "BuyerWallet222222222222222222222222222222222"
WALLET_SELLER_1 = "SellerWallet11111111111111111111111111111111"
FROZEN_NOW_MS = 1_718_700_500_000  # injected freeze timestamp (deterministic)


def _make_event_time(slot: int) -> EventTime:
    return EventTime(
        slot=slot,
        block_time_ms=BASE_BLOCK_TIME_MS + (slot - BASE_SLOT) * 400,
        wall_clock_ms=BASE_BLOCK_TIME_MS + (slot - BASE_SLOT) * 400 + 50,
    )


def _make_launch_event(
    slot: int, sol_reserve_lamports: int, creator_wallet: str = WALLET_BUYER_1
) -> LaunchEvent:
    return LaunchEvent(
        mint=MINT_A,
        venue_program_id="6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",
        source=LaunchSource.PUMPFUN,
        event_time=_make_event_time(slot),
        observation_slot=slot,
        confirmation_slot=slot,
        detection_transport=DetectionTransport.GEYSER,
        sol_reserve_lamports=sol_reserve_lamports,
        token_reserve_base=1_000_000_000,
        token_decimals=6,
        initial_holders=0,
        competitors=0,
        creator_wallet=creator_wallet,
        bundler_cluster_id=None,
        deploy_template_fingerprint=None,
        data_staleness_ms=50,
    )


POOL_CREATION = _make_launch_event(slot=BASE_SLOT, sol_reserve_lamports=0)


def _features_with_net_pressure(
    buy_lamports: list[int], sell_lamports: list[int]
):
    """Build a real BuyPressureFeatures from pre-classified (event, is_buy) pairs.

    This exercises the SAME T-305 feature path the production baseline consumes —
    the baseline is proven constructible from the feature, not from a hand-built mock.
    """
    classified: list[tuple[LaunchEvent, bool]] = []
    slot = BASE_SLOT + 1
    for amt in buy_lamports:
        classified.append(
            (_make_launch_event(slot=slot, sol_reserve_lamports=amt, creator_wallet=WALLET_BUYER_1), True)
        )
        slot += 1
    for amt in sell_lamports:
        classified.append(
            (_make_launch_event(slot=slot, sol_reserve_lamports=amt, creator_wallet=WALLET_SELLER_1), False)
        )
        slot += 1
    return build_buy_pressure_features(
        pool_creation_event=POOL_CREATION,
        classified_events=classified,
        first_k_slots=FIRST_K_SLOTS,
    )


def _copy_config_to_tmp(tmp_path: Path, *, frozen: bool = False) -> Path:
    """Copy the committed config into tmp_path so tests never mutate the real file.

    frozen=False clears `frozen_hash` so the copy is in the pre-first-fit state.
    """
    with DEFAULT_CONFIG_PATH.open("r", encoding="utf-8") as fh:
        raw = json.load(fh)
    if not frozen:
        raw["frozen_hash"] = None
        raw["frozen_at_first_fit"] = False
        raw["first_fit_recorded_at_ms"] = None
    dest = tmp_path / "baseline.frozen.json"
    with dest.open("w", encoding="utf-8") as fh:
        json.dump(raw, fh, indent=2)
    return dest


# ---------------------------------------------------------------------------
# 1. Committed config loads and is money-correct
# ---------------------------------------------------------------------------


def test_committed_config_loads_money_correct():
    params = load_params(DEFAULT_CONFIG_PATH)
    assert params.first_k_slots == FIRST_K_SLOTS
    # Money fields are the exact types — int / Decimal, never float.
    assert isinstance(params.min_volume_lamports, int)
    assert isinstance(params.reference_threshold_lamports, Decimal)
    assert not isinstance(params.reference_threshold_lamports, float)
    # The hash is a 64-char hex sha256.
    assert isinstance(params.params_hash, str) and len(params.params_hash) == 64


def test_committed_config_is_frozen_and_hash_matches():
    """The COMMITTED baseline.frozen.json is ALREADY frozen (first fit happened, C-4).

    This is the real-file guard: it FAILS if anyone edits a hashed param in the
    committed config without re-doing the freeze (the stored frozen_hash would no
    longer match the canonical hash of the params).  Editing a frozen param to chase
    a model result is `baseline_changed_after_fit` — a TEST FAILURE, not an edit.
    """
    raw = json.loads(DEFAULT_CONFIG_PATH.read_text())
    assert raw["frozen_at_first_fit"] is True, (
        "committed baseline must be frozen before any model fit (T-311/C-4)"
    )
    assert raw["frozen_hash"] is not None
    # The stored frozen hash must equal the canonical hash of the committed params.
    live = canonical_params_hash(raw["params"])
    assert raw["frozen_hash"] == live, (
        "baseline_changed_after_fit: committed params no longer match the frozen hash. "
        "A hashed param was edited after the baseline was frozen — revert it or open an ADR."
    )
    # And the freeze gate on the committed file is a clean no-op (already frozen, unchanged).
    params = assert_frozen_or_freeze(DEFAULT_CONFIG_PATH)
    assert params.params_hash == raw["frozen_hash"]


# ---------------------------------------------------------------------------
# 2. Freeze on first fit
# ---------------------------------------------------------------------------


def test_first_fit_freezes_hash(tmp_path):
    cfg = _copy_config_to_tmp(tmp_path, frozen=False)
    # Pre-fit: no frozen hash.
    raw_before = json.loads(cfg.read_text())
    assert raw_before["frozen_hash"] is None

    params = assert_frozen_or_freeze(cfg, now_ms=FROZEN_NOW_MS)

    raw_after = json.loads(cfg.read_text())
    assert raw_after["frozen_hash"] == params.params_hash
    assert raw_after["frozen_at_first_fit"] is True
    assert raw_after["first_fit_recorded_at_ms"] == FROZEN_NOW_MS


# ---------------------------------------------------------------------------
# 3. HEADLINE C-4 TEST — param change after first fit FAILS (hash mismatch)
# ---------------------------------------------------------------------------


def test_param_change_after_freeze_fails(tmp_path):
    cfg = _copy_config_to_tmp(tmp_path, frozen=False)
    # First fit: freeze.
    frozen = assert_frozen_or_freeze(cfg, now_ms=FROZEN_NOW_MS)

    # Now mutate a hashed param (selection_percentile) — simulating a post-fit tweak.
    raw = json.loads(cfg.read_text())
    raw["params"]["selection_percentile"] = 75.0  # was 60.0
    cfg.write_text(json.dumps(raw, indent=2))

    # The freeze gate MUST reject the changed params.
    with pytest.raises(BaselineFrozenError) as exc:
        assert_frozen_or_freeze(cfg, now_ms=FROZEN_NOW_MS)
    assert "baseline_changed_after_fit" in str(exc.value)
    assert exc.value.frozen_hash == frozen.params_hash
    assert exc.value.live_hash != frozen.params_hash


def test_money_param_change_after_freeze_fails(tmp_path):
    """Changing the FROZEN money threshold after first fit must also FAIL."""
    cfg = _copy_config_to_tmp(tmp_path, frozen=False)
    assert_frozen_or_freeze(cfg, now_ms=FROZEN_NOW_MS)

    raw = json.loads(cfg.read_text())
    # bump the reference threshold (Decimal-string money) post-fit
    raw["params"]["reference_threshold_lamports"] = "999000000"
    cfg.write_text(json.dumps(raw, indent=2))

    with pytest.raises(BaselineFrozenError):
        assert_frozen_or_freeze(cfg, now_ms=FROZEN_NOW_MS)


def test_first_k_change_after_freeze_fails(tmp_path):
    cfg = _copy_config_to_tmp(tmp_path, frozen=False)
    assert_frozen_or_freeze(cfg, now_ms=FROZEN_NOW_MS)
    raw = json.loads(cfg.read_text())
    raw["params"]["first_k_slots"] = 20  # was 10
    cfg.write_text(json.dumps(raw, indent=2))
    with pytest.raises(BaselineFrozenError):
        assert_frozen_or_freeze(cfg, now_ms=FROZEN_NOW_MS)


# ---------------------------------------------------------------------------
# 8. Idempotent re-freeze (already frozen, unchanged) — no-op, no failure
# ---------------------------------------------------------------------------


def test_refreeze_unchanged_is_noop(tmp_path):
    cfg = _copy_config_to_tmp(tmp_path, frozen=False)
    first = assert_frozen_or_freeze(cfg, now_ms=FROZEN_NOW_MS)
    raw_after_first = cfg.read_text()
    # Re-run on the already-frozen, UNCHANGED config — must not raise, must not rewrite hash.
    second = assert_frozen_or_freeze(cfg, now_ms=FROZEN_NOW_MS + 999)
    assert second.params_hash == first.params_hash
    raw_after_second = json.loads(cfg.read_text())
    assert raw_after_second["frozen_hash"] == first.params_hash
    # The first-fit timestamp is NOT overwritten on a re-freeze.
    assert raw_after_second["first_fit_recorded_at_ms"] == FROZEN_NOW_MS
    # And the file content for the frozen identity is stable.
    assert json.loads(raw_after_first)["frozen_hash"] == first.params_hash


# ---------------------------------------------------------------------------
# 4. CONSTRUCTIBLE from the first-K buy-pressure feature ONLY (no future data)
# ---------------------------------------------------------------------------


def test_baseline_constructible_from_feature_selected():
    """Strong net buy pressure above threshold -> selected, positive momentum."""
    params = load_params(DEFAULT_CONFIG_PATH)
    threshold = params.reference_threshold_lamports  # Decimal lamports
    # Net pressure = 2x threshold (well above) with a small sell.
    feats = _features_with_net_pressure(
        buy_lamports=[int(threshold) + 50 * LAMPORTS_PER_SOL],
        sell_lamports=[1 * LAMPORTS_PER_SOL],
    )
    sig = evaluate_baseline(feats, params, mint=MINT_A)
    assert isinstance(sig, BaselineSignal)
    assert sig.selected is True
    assert sig.momentum_score > 0
    assert isinstance(sig.momentum_score, Decimal)  # money: Decimal, not float
    assert 0.0 <= sig.baseline_p <= 1.0
    assert sig.params_hash == params.params_hash


def test_baseline_not_selected_below_threshold():
    """Positive but below-threshold pressure -> NOT selected (the gate bites)."""
    params = load_params(DEFAULT_CONFIG_PATH)
    threshold = params.reference_threshold_lamports
    # Net pressure positive but strictly below the frozen threshold.
    below = int(threshold) - 1 if int(threshold) > 0 else 0
    feats = _features_with_net_pressure(buy_lamports=[below], sell_lamports=[])
    sig = evaluate_baseline(feats, params, mint=MINT_A)
    assert sig.selected is False
    assert sig.momentum_score == Decimal(below)


def test_baseline_not_selected_on_net_selling():
    """Net SELLING pressure (sells exceed buys) -> not selected, negative momentum."""
    params = load_params(DEFAULT_CONFIG_PATH)
    feats = _features_with_net_pressure(
        buy_lamports=[10 * LAMPORTS_PER_SOL],
        sell_lamports=[40 * LAMPORTS_PER_SOL],
    )
    sig = evaluate_baseline(feats, params, mint=MINT_A)
    assert sig.selected is False
    assert sig.momentum_score < 0
    assert sig.baseline_p == 0.0  # no buying pressure -> proxy floors at 0


# ---------------------------------------------------------------------------
# 5. Money discipline — a float money param is REJECTED, never coerced
# ---------------------------------------------------------------------------


def test_float_money_param_rejected(tmp_path):
    cfg = _copy_config_to_tmp(tmp_path, frozen=False)
    raw = json.loads(cfg.read_text())
    raw["params"]["min_volume_lamports"] = 1.5  # float — illegal money
    cfg.write_text(json.dumps(raw, indent=2))
    with pytest.raises(BaselineConfigError):
        load_params(cfg)


def test_float_threshold_param_rejected(tmp_path):
    cfg = _copy_config_to_tmp(tmp_path, frozen=False)
    raw = json.loads(cfg.read_text())
    raw["params"]["reference_threshold_lamports"] = 3.0e8  # float — illegal money
    cfg.write_text(json.dumps(raw, indent=2))
    with pytest.raises(BaselineConfigError):
        load_params(cfg)


# ---------------------------------------------------------------------------
# 6. Canonical hash is deterministic / reproducible (layout-independent)
# ---------------------------------------------------------------------------


def test_canonical_hash_layout_independent():
    base = {
        "first_k_slots": 10,
        "selection_percentile": 60.0,
        "unit_of_risk": "net_pnl_per_sol_at_risk",
        "candidate_universe": "u",
        "feature_schema_version": "1.0.0",
        "min_volume_lamports": 0,
        "reference_threshold_lamports": "300000000",
    }
    reordered = dict(reversed(list(base.items())))
    # Extra non-hashed keys must NOT affect the hash.
    with_extra = dict(base)
    with_extra["_comment"] = ["irrelevant"]
    h1 = canonical_params_hash(base)
    h2 = canonical_params_hash(reordered)
    h3 = canonical_params_hash(with_extra)
    assert h1 == h2 == h3


def test_canonical_hash_changes_on_param_change():
    base = {
        "first_k_slots": 10,
        "selection_percentile": 60.0,
        "unit_of_risk": "net_pnl_per_sol_at_risk",
        "candidate_universe": "u",
        "feature_schema_version": "1.0.0",
        "min_volume_lamports": 0,
        "reference_threshold_lamports": "300000000",
    }
    changed = dict(base)
    changed["selection_percentile"] = 60.0001
    assert canonical_params_hash(base) != canonical_params_hash(changed)


# ---------------------------------------------------------------------------
# 7. NO price / size / trade-decision field on the output (locked decision 9)
# ---------------------------------------------------------------------------


def test_signal_has_no_price_or_size_field():
    params = load_params(DEFAULT_CONFIG_PATH)
    feats = _features_with_net_pressure(buy_lamports=[5 * LAMPORTS_PER_SOL], sell_lamports=[])
    sig = evaluate_baseline(feats, params, mint=MINT_A)
    field_names = set(sig.__dataclass_fields__.keys())
    forbidden = {"price", "price_target", "size", "size_sol", "size_lamports",
                 "amount", "buy", "sell", "order", "trade", "decision", "stop"}
    assert field_names.isdisjoint(forbidden), (
        f"BaselineSignal must never carry a price/size/trade field; found {field_names & forbidden}"
    )


# ---------------------------------------------------------------------------
# 9. Window-K mismatch is refused
# ---------------------------------------------------------------------------


def test_window_k_mismatch_refused():
    params = load_params(DEFAULT_CONFIG_PATH)
    # Build features on a DIFFERENT K than the frozen baseline.
    classified = [
        (_make_launch_event(slot=BASE_SLOT + 1, sol_reserve_lamports=5 * LAMPORTS_PER_SOL), True)
    ]
    feats_wrong_k = build_buy_pressure_features(
        pool_creation_event=POOL_CREATION,
        classified_events=classified,
        first_k_slots=params.first_k_slots + 5,  # mismatched window
    )
    with pytest.raises(BaselineConfigError):
        evaluate_baseline(feats_wrong_k, params, mint=MINT_A)
