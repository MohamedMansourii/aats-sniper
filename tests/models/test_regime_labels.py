"""Tests for the chart-path REGIME label taxonomy + remains-sellable gate (M2-CP-02).

Self-check criteria (task M2-CP-02; charter §6; data-models §3A; T-300a):
  1. LEAK AUDIT (reused): a label built from a NON-forward outcome (resolution <= decision,
     or a feature event-time AFTER the decision) is REJECTED by the reused
     `assert_event_time_leq_decision`; the build-time forward-window gate rejects an outcome
     sample at/before the decision slot or beyond the resolution slot.
  2. TAXONOMY EXHAUSTIVE + MUTUALLY EXCLUSIVE: `classify_regime` returns exactly one of the
     four STATES for every input; the enum has exactly four members; a random grid always
     resolves to exactly one member.
  3. NO WIN-RATE / NO PRICE / NO SIZE: no win_rate / success_rate / hit_rate / realized_mult
     / price / size field anywhere in the label row or the module.
  4. ASYMMETRIC-TRUST / INERTNESS: the accumulation + neutral classes map to NONE (inert);
     the de-risk action codomain contains NO risk-increasing member (size-up/relax/delay
     INEXPRESSIBLE BY TYPE).
  5. REMAINS-SELLABLE GATE: a healthy pool stays sellable; a drained (rug) pool fails the
     gate at the collapse slot -> RUG_IN_PROGRESS.
  6. TAINT GUARD (reused): a regime-label column (and a frozen truth_*/LaunchOutcome column)
     reaching a feature matrix fails the build.
  7. SLOW-LOOP ONLY: classify/build REFUSE to run on the FAST/SNIPE loop (reused guard).
  8. VERSIONED + BOOTSTRAP: the taxonomy version is stamped; is_bootstrap_not_real defaults
     True (no capital license).

Offline / injectable / deterministic — no RPC/LLM/Geyser/Redis/network/keypair.
"""

from __future__ import annotations

import dataclasses

import pytest

from aats.contracts.events import EventTime
from aats.contracts.provenance import ProvenanceTaintError
from aats.models.regime_labels import (
    REGIME_DERISK_ACTION,
    REGIME_LABEL_CODES,
    REGIME_LABEL_TAXONOMY_VERSION,
    REGIME_LABELS_LOOP,
    RegimeDeRiskAction,
    RegimeLabel,
    RegimeLabelLeakError,
    RegimeOutcome,
    RegimeOutcomeSample,
    assert_no_regime_label_taint,
    build_regime_outcome,
    classify_regime,
    derisk_action,
    is_inert,
    join_regime_labels_by_event_time,
    regime_label_to_joined_example,
    remains_sellable_at_size,
    simulate_exit_slippage_bps,
)
from aats.models.survivor import SurvivorLoopViolation
from aats.models.training import LeakAuditError, assert_event_time_leq_decision

LAMPORTS_PER_SOL = 1_000_000_000
BASE_SLOT = 320_000_000
BASE_BT = 1_718_000_000_000
SLOT_MS = 400


def _et(slot: int) -> EventTime:
    return EventTime(
        slot=slot,
        block_time_ms=BASE_BT + (slot - BASE_SLOT) * SLOT_MS,
        wall_clock_ms=BASE_BT + (slot - BASE_SLOT) * SLOT_MS + 5,
    )


def _sample(slot: int, *, sol: int, tok: int, holders: int, price: float) -> RegimeOutcomeSample:
    return RegimeOutcomeSample(
        outcome_slot=slot,
        sol_reserve_lamports=sol,
        token_reserve_base=tok,
        holder_count=holders,
        spot_price_ratio=price,
    )


# A deep, liquid pool: selling a modest position moves price negligibly (stays sellable).
_DEEP_SOL = 500 * LAMPORTS_PER_SOL
_DEEP_TOK = 500_000_000_000
_EXIT_SIZE = 1_000_000_000  # decision-relevant position (base units) — small vs the pool
_MAX_SLIP = 300             # 3% max acceptable exit slippage


# ---------------------------------------------------------------------------
# 2. Taxonomy: exhaustive + mutually exclusive
# ---------------------------------------------------------------------------


def test_taxonomy_has_exactly_four_states():
    assert set(RegimeLabel) == {
        RegimeLabel.ACCUMULATION,
        RegimeLabel.DISTRIBUTION,
        RegimeLabel.RUG_IN_PROGRESS,
        RegimeLabel.NEUTRAL,
    }
    assert len(RegimeLabel) == 4


def test_classify_is_total_and_single_valued_over_a_grid():
    """Every combination resolves to exactly one member of the taxonomy (exhaustive+ME)."""
    seen: set[RegimeLabel] = set()
    for sellable in (True, False):
        for net in (-0.9, -0.3, -0.1, 0.0, 0.05, 0.25, 1.0):
            for dd in (0.0, 0.2, 0.45, 0.6, 0.95):
                for hd in (-0.5, -0.2, -0.05, 0.0, 0.1, 0.5):
                    label = classify_regime(
                        remained_sellable=sellable,
                        net_return_ratio=net,
                        max_drawdown_ratio=dd,
                        holder_delta_ratio=hd,
                    )
                    assert isinstance(label, RegimeLabel)  # exactly one member
                    seen.add(label)
    # The grid exercises all four states (the taxonomy is reachable, not vacuous).
    assert seen == set(RegimeLabel)


def test_not_sellable_always_rug_regardless_of_price():
    """Sellability collapse DOMINATES: a 'green' but unsellable path is a rug, not accumulation.

    Mutation-meaningful: if the classifier checked net return BEFORE sellability, this
    strongly-bullish-but-unsellable case would misclassify as ACCUMULATION.
    """
    label = classify_regime(
        remained_sellable=False,
        net_return_ratio=5.0,   # +500% on paper
        max_drawdown_ratio=0.0,
        holder_delta_ratio=1.0,
    )
    assert label is RegimeLabel.RUG_IN_PROGRESS


def test_distribution_and_accumulation_are_disjoint():
    """The two directional states cannot both fire (mutual exclusivity of the signatures)."""
    acc = classify_regime(
        remained_sellable=True, net_return_ratio=0.4, max_drawdown_ratio=0.1,
        holder_delta_ratio=0.3,
    )
    dist = classify_regime(
        remained_sellable=True, net_return_ratio=-0.4, max_drawdown_ratio=0.7,
        holder_delta_ratio=-0.3,
    )
    assert acc is RegimeLabel.ACCUMULATION
    assert dist is RegimeLabel.DISTRIBUTION


def test_ranging_is_neutral():
    label = classify_regime(
        remained_sellable=True, net_return_ratio=0.05, max_drawdown_ratio=0.2,
        holder_delta_ratio=0.02,
    )
    assert label is RegimeLabel.NEUTRAL


# ---------------------------------------------------------------------------
# 4. Asymmetric-trust law: accumulation/neutral provably inert; no risk-up member
# ---------------------------------------------------------------------------


def test_derisk_action_codomain_has_no_risk_increasing_member():
    members = {a.value for a in RegimeDeRiskAction}
    assert members == {"NONE", "REDUCE", "FORCE_EXIT", "VETO_ENTRY"}
    # A risk-INCREASING action is inexpressible by type.
    for forbidden in ("SIZE_UP", "RELAX_STOP", "DELAY_EXIT", "ADD_LEVERAGE", "WIDEN_STOP"):
        assert forbidden not in members


def test_bullish_and_ranging_classes_are_inert():
    assert derisk_action(RegimeLabel.ACCUMULATION) is RegimeDeRiskAction.NONE
    assert derisk_action(RegimeLabel.NEUTRAL) is RegimeDeRiskAction.NONE
    assert is_inert(RegimeLabel.ACCUMULATION)
    assert is_inert(RegimeLabel.NEUTRAL)
    # De-risk classes are NOT inert and map to de-risk actions only.
    assert derisk_action(RegimeLabel.DISTRIBUTION) is RegimeDeRiskAction.REDUCE
    assert derisk_action(RegimeLabel.RUG_IN_PROGRESS) is RegimeDeRiskAction.FORCE_EXIT
    assert not is_inert(RegimeLabel.DISTRIBUTION)
    assert not is_inert(RegimeLabel.RUG_IN_PROGRESS)


def test_derisk_mapping_is_total_over_the_enum():
    assert set(REGIME_DERISK_ACTION.keys()) == set(RegimeLabel)


# ---------------------------------------------------------------------------
# 5. Remains-sellable-at-size gate
# ---------------------------------------------------------------------------


def test_exit_slippage_grows_as_pool_drains():
    deep = simulate_exit_slippage_bps(_DEEP_SOL, _DEEP_TOK, _EXIT_SIZE)
    thin = simulate_exit_slippage_bps(1 * LAMPORTS_PER_SOL, 10_000_000, _EXIT_SIZE)
    assert deep is not None and thin is not None
    assert deep < thin
    # Fully rugged reserves -> refuse-by-default (None).
    assert simulate_exit_slippage_bps(0, _DEEP_TOK, _EXIT_SIZE) is None


def test_gate_sellable_on_deep_pool():
    samples = [_sample(BASE_SLOT + i, sol=_DEEP_SOL, tok=_DEEP_TOK, holders=100, price=1.0)
               for i in range(1, 6)]
    ok, first = remains_sellable_at_size(
        samples, decision_relevant_size_base=_EXIT_SIZE, max_slippage_bps=_MAX_SLIP
    )
    assert ok is True
    assert first is None


def test_gate_flags_collapse_slot_on_rug():
    # Pool is deep for two steps then the sell-side reserve collapses (LP pull).
    samples = [
        _sample(BASE_SLOT + 1, sol=_DEEP_SOL, tok=_DEEP_TOK, holders=100, price=1.0),
        _sample(BASE_SLOT + 2, sol=_DEEP_SOL, tok=_DEEP_TOK, holders=100, price=1.0),
        _sample(BASE_SLOT + 3, sol=1, tok=_DEEP_TOK, holders=100, price=0.0001),
    ]
    ok, first = remains_sellable_at_size(
        samples, decision_relevant_size_base=_EXIT_SIZE, max_slippage_bps=_MAX_SLIP
    )
    assert ok is False
    assert first == BASE_SLOT + 3


def test_empty_trajectory_is_not_sellable_refuse_by_default():
    ok, first = remains_sellable_at_size(
        [], decision_relevant_size_base=_EXIT_SIZE, max_slippage_bps=_MAX_SLIP
    )
    assert ok is False
    assert first is None


def test_gate_rejects_non_positive_size():
    with pytest.raises(ValueError):
        remains_sellable_at_size([], decision_relevant_size_base=0, max_slippage_bps=_MAX_SLIP)


# ---------------------------------------------------------------------------
# build_regime_outcome — happy path + point-in-time forward-window gate
# ---------------------------------------------------------------------------


def _deep_forward_samples(decision_slot: int, horizon: int, *, price=1.0, holders=100):
    return [
        _sample(decision_slot + step, sol=_DEEP_SOL, tok=_DEEP_TOK, holders=holders, price=price)
        for step in range(1, horizon + 1)
    ]


def test_build_regime_outcome_accumulation_happy_path():
    decision_slot = BASE_SLOT
    horizon = 5
    samples = [
        _sample(decision_slot + step, sol=_DEEP_SOL, tok=_DEEP_TOK,
                holders=100 + 5 * step, price=1.0 + 0.06 * step)
        for step in range(1, horizon + 1)
    ]
    out = build_regime_outcome(
        mint="MintAcc",
        decision_event_time=_et(decision_slot),
        resolution_event_time=_et(decision_slot + horizon),
        label_horizon_h_slots=horizon,
        decision_reference_price_ratio=1.0,
        decision_reference_holder_count=100,
        outcome_samples=samples,
        decision_relevant_size_base=_EXIT_SIZE,
        max_slippage_bps=_MAX_SLIP,
    )
    assert out.label is RegimeLabel.ACCUMULATION
    assert out.remained_sellable is True
    assert out.taxonomy_version == REGIME_LABEL_TAXONOMY_VERSION
    assert out.is_bootstrap_not_real is True
    assert out.derisk_action is RegimeDeRiskAction.NONE  # inert


def test_build_regime_outcome_rug_when_sellability_collapses():
    decision_slot = BASE_SLOT
    horizon = 3
    samples = [
        _sample(decision_slot + 1, sol=_DEEP_SOL, tok=_DEEP_TOK, holders=100, price=1.2),
        _sample(decision_slot + 2, sol=_DEEP_SOL, tok=_DEEP_TOK, holders=100, price=1.3),
        _sample(decision_slot + 3, sol=1, tok=_DEEP_TOK, holders=100, price=0.0001),
    ]
    out = build_regime_outcome(
        mint="MintRug",
        decision_event_time=_et(decision_slot),
        resolution_event_time=_et(decision_slot + horizon),
        label_horizon_h_slots=horizon,
        decision_reference_price_ratio=1.0,
        decision_reference_holder_count=100,
        outcome_samples=samples,
        decision_relevant_size_base=_EXIT_SIZE,
        max_slippage_bps=_MAX_SLIP,
    )
    assert out.label is RegimeLabel.RUG_IN_PROGRESS
    assert out.remained_sellable is False
    assert out.first_unsellable_slot == decision_slot + 3
    assert out.derisk_action is RegimeDeRiskAction.FORCE_EXIT


def test_build_rejects_sample_at_or_before_decision_slot():
    decision_slot = BASE_SLOT
    horizon = 5
    bad = _deep_forward_samples(decision_slot, horizon)
    # Plant a leak: a sample AT the decision slot (peeking at the decision-time bar).
    bad.insert(0, _sample(decision_slot, sol=_DEEP_SOL, tok=_DEEP_TOK, holders=100, price=1.0))
    with pytest.raises(RegimeLabelLeakError):
        build_regime_outcome(
            mint="MintLeak",
            decision_event_time=_et(decision_slot),
            resolution_event_time=_et(decision_slot + horizon),
            label_horizon_h_slots=horizon,
            decision_reference_price_ratio=1.0,
            decision_reference_holder_count=100,
            outcome_samples=bad,
            decision_relevant_size_base=_EXIT_SIZE,
            max_slippage_bps=_MAX_SLIP,
        )


def test_build_rejects_sample_beyond_resolution_slot():
    decision_slot = BASE_SLOT
    horizon = 5
    bad = _deep_forward_samples(decision_slot, horizon)
    # Plant a leak: a sample AFTER the resolution slot (reading past the declared horizon).
    bad.append(_sample(decision_slot + horizon + 1, sol=_DEEP_SOL, tok=_DEEP_TOK,
                        holders=100, price=1.0))
    with pytest.raises(RegimeLabelLeakError):
        build_regime_outcome(
            mint="MintLeak2",
            decision_event_time=_et(decision_slot),
            resolution_event_time=_et(decision_slot + horizon),
            label_horizon_h_slots=horizon,
            decision_reference_price_ratio=1.0,
            decision_reference_holder_count=100,
            outcome_samples=bad,
            decision_relevant_size_base=_EXIT_SIZE,
            max_slippage_bps=_MAX_SLIP,
        )


def test_build_rejects_out_of_order_samples():
    decision_slot = BASE_SLOT
    horizon = 5
    samples = _deep_forward_samples(decision_slot, horizon)
    samples[2], samples[3] = samples[3], samples[2]  # break strict-increasing order
    with pytest.raises(RegimeLabelLeakError):
        build_regime_outcome(
            mint="MintOrder",
            decision_event_time=_et(decision_slot),
            resolution_event_time=_et(decision_slot + horizon),
            label_horizon_h_slots=horizon,
            decision_reference_price_ratio=1.0,
            decision_reference_holder_count=100,
            outcome_samples=samples,
            decision_relevant_size_base=_EXIT_SIZE,
            max_slippage_bps=_MAX_SLIP,
        )


# ---------------------------------------------------------------------------
# RegimeOutcome horizon-proof invariants (resolution strictly > decision)
# ---------------------------------------------------------------------------


def test_regime_outcome_requires_strictly_forward_resolution():
    with pytest.raises(RegimeLabelLeakError):
        RegimeOutcome(
            mint="M",
            event_time=_et(BASE_SLOT + 100),
            label_horizon_h_slots=100,
            resolution_event_time=_et(BASE_SLOT),  # BEFORE the decision -> leak
            taxonomy_version=REGIME_LABEL_TAXONOMY_VERSION,
            label=RegimeLabel.NEUTRAL,
            remained_sellable=True,
            first_unsellable_slot=None,
            is_bootstrap_not_real=True,
        )


def test_regime_outcome_horizon_must_match_stamped_resolution():
    with pytest.raises(RegimeLabelLeakError):
        RegimeOutcome(
            mint="M",
            event_time=_et(BASE_SLOT),
            label_horizon_h_slots=50,
            resolution_event_time=_et(BASE_SLOT + 999),  # != decision + horizon
            taxonomy_version=REGIME_LABEL_TAXONOMY_VERSION,
            label=RegimeLabel.NEUTRAL,
            remained_sellable=True,
            first_unsellable_slot=None,
            is_bootstrap_not_real=True,
        )


def test_regime_outcome_sellability_consistency():
    with pytest.raises(ValueError):
        RegimeOutcome(
            mint="M",
            event_time=_et(BASE_SLOT),
            label_horizon_h_slots=10,
            resolution_event_time=_et(BASE_SLOT + 10),
            taxonomy_version=REGIME_LABEL_TAXONOMY_VERSION,
            label=RegimeLabel.RUG_IN_PROGRESS,
            remained_sellable=False,
            first_unsellable_slot=None,  # inconsistent: collapsed but no collapse slot
            is_bootstrap_not_real=True,
        )


# ---------------------------------------------------------------------------
# 1. LEAK AUDIT (reused assert_event_time_leq_decision) over the regime join
# ---------------------------------------------------------------------------


def test_regime_join_passes_reused_leak_audit():
    decision_slot = BASE_SLOT
    horizon = 10
    out = build_regime_outcome(
        mint="MintJoin",
        decision_event_time=_et(decision_slot),
        resolution_event_time=_et(decision_slot + horizon),
        label_horizon_h_slots=horizon,
        decision_reference_price_ratio=1.0,
        decision_reference_holder_count=100,
        outcome_samples=_deep_forward_samples(decision_slot, horizon),
        decision_relevant_size_base=_EXIT_SIZE,
        max_slippage_bps=_MAX_SLIP,
    )
    joined = join_regime_labels_by_event_time([(_et(decision_slot), [0.0, 1.0])], [out])
    assert len(joined) == 1
    summary = assert_event_time_leq_decision(joined)  # REUSED audit -> passes
    assert "LEAK AUDIT PASS" in summary


def test_reused_leak_audit_rejects_feature_after_decision():
    """A feature event-time AFTER the decision anchor is a forward-looking leak (rejected)."""
    ex = regime_label_to_joined_example(
        feature_row=[0.0],
        feature_event_time=_et(BASE_SLOT),
        outcome=RegimeOutcome(
            mint="M",
            event_time=_et(BASE_SLOT),
            label_horizon_h_slots=10,
            resolution_event_time=_et(BASE_SLOT + 10),
            taxonomy_version=REGIME_LABEL_TAXONOMY_VERSION,
            label=RegimeLabel.NEUTRAL,
            remained_sellable=True,
            first_unsellable_slot=None,
            is_bootstrap_not_real=True,
        ),
    )
    # Plant the leak: bump the feature event-time PAST the decision anchor.
    tainted = dataclasses.replace(ex, feature_event_time=_et(BASE_SLOT + 5))
    with pytest.raises(LeakAuditError):
        assert_event_time_leq_decision([tainted])


def test_reused_leak_audit_rejects_non_forward_label():
    """A label whose resolution is NOT strictly after the decision fails the reused audit."""
    ex = regime_label_to_joined_example(
        feature_row=[0.0],
        feature_event_time=_et(BASE_SLOT),
        outcome=RegimeOutcome(
            mint="M",
            event_time=_et(BASE_SLOT),
            label_horizon_h_slots=10,
            resolution_event_time=_et(BASE_SLOT + 10),
            taxonomy_version=REGIME_LABEL_TAXONOMY_VERSION,
            label=RegimeLabel.NEUTRAL,
            remained_sellable=True,
            first_unsellable_slot=None,
            is_bootstrap_not_real=True,
        ),
    )
    # Plant the leak: collapse the resolution back onto the decision (no forward horizon).
    tainted = dataclasses.replace(ex, resolution_event_time=_et(BASE_SLOT))
    with pytest.raises(LeakAuditError):
        assert_event_time_leq_decision([tainted])


def test_join_is_by_event_time_only():
    out = RegimeOutcome(
        mint="M",
        event_time=_et(BASE_SLOT),
        label_horizon_h_slots=10,
        resolution_event_time=_et(BASE_SLOT + 10),
        taxonomy_version=REGIME_LABEL_TAXONOMY_VERSION,
        label=RegimeLabel.NEUTRAL,
        remained_sellable=True,
        first_unsellable_slot=None,
        is_bootstrap_not_real=True,
    )
    # A feature keyed at a DIFFERENT event_time has no legal join to this label.
    with pytest.raises(RegimeLabelLeakError):
        regime_label_to_joined_example(
            feature_row=[0.0], feature_event_time=_et(BASE_SLOT + 1), outcome=out
        )
    # A feature with no matching label is dropped (CENSORED-equivalent), never defaulted.
    assert join_regime_labels_by_event_time([(_et(BASE_SLOT + 999), [0.0])], [out]) == []


# ---------------------------------------------------------------------------
# 6. Taint guard (reused assert_no_label_taint + regime-label names)
# ---------------------------------------------------------------------------


def test_taint_guard_rejects_regime_label_column():
    with pytest.raises(ProvenanceTaintError):
        assert_no_regime_label_taint(["lp_depth_lamports", "regime_label"])


def test_taint_guard_reuses_frozen_label_names():
    # A frozen truth_* / LaunchOutcome column is still caught (delegates to the frozen guard).
    with pytest.raises(ProvenanceTaintError):
        assert_no_regime_label_taint(["holder_count", "truth_is_rug"])
    with pytest.raises(ProvenanceTaintError):
        assert_no_regime_label_taint(["holder_count", "realized_mult_decimal"])


def test_taint_guard_passes_clean_feature_columns():
    # Clean, PIT feature columns pass (no raise).
    assert_no_regime_label_taint(["lp_depth_lamports", "holder_count", "first_k_buy_pressure"])


# ---------------------------------------------------------------------------
# 3. NO win-rate / price / size field anywhere
# ---------------------------------------------------------------------------


def test_no_win_rate_or_price_field_on_label_row():
    fields = {f.name for f in dataclasses.fields(RegimeOutcome)}
    forbidden_substrings = (
        "win_rate", "winrate", "success_rate", "hit_rate", "price", "size",
        "realized_mult", "pnl", "profit", "return",
    )
    for name in fields:
        for bad in forbidden_substrings:
            assert bad not in name.lower(), f"forbidden field '{name}' on RegimeOutcome"


def test_no_win_rate_in_module_source():
    from pathlib import Path

    import aats.models.regime_labels as mod

    src = Path(mod.__file__).read_text(encoding="utf-8").lower()
    assert "win_rate" not in src
    assert "winrate" not in src
    assert "success_rate" not in src


# ---------------------------------------------------------------------------
# 7. SLOW-loop only (reused assert_slow_loop_only guard)
# ---------------------------------------------------------------------------


def test_classify_refuses_fast_loop():
    with pytest.raises(SurvivorLoopViolation):
        classify_regime(
            remained_sellable=True, net_return_ratio=0.0, max_drawdown_ratio=0.0,
            holder_delta_ratio=0.0, loop="fast",
        )


def test_build_refuses_snipe_loop():
    decision_slot = BASE_SLOT
    horizon = 5
    with pytest.raises(SurvivorLoopViolation):
        build_regime_outcome(
            mint="M",
            decision_event_time=_et(decision_slot),
            resolution_event_time=_et(decision_slot + horizon),
            label_horizon_h_slots=horizon,
            decision_reference_price_ratio=1.0,
            decision_reference_holder_count=100,
            outcome_samples=_deep_forward_samples(decision_slot, horizon),
            decision_relevant_size_base=_EXIT_SIZE,
            max_slippage_bps=_MAX_SLIP,
            loop="snipe",
        )


def test_regime_labels_loop_is_slow():
    assert REGIME_LABELS_LOOP == "slow"


# ---------------------------------------------------------------------------
# 8. Versioned + label codes
# ---------------------------------------------------------------------------


def test_label_codes_are_distinct_and_cover_the_enum():
    assert set(REGIME_LABEL_CODES.keys()) == set(RegimeLabel)
    assert len(set(REGIME_LABEL_CODES.values())) == len(RegimeLabel)


def test_taxonomy_version_is_stamped():
    assert REGIME_LABEL_TAXONOMY_VERSION == "1.0.0"
