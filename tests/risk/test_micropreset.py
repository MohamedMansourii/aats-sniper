"""AUDIT-micropreset — the EARLY-ENTRY MICRO preset composes EXACTLY these constraints.

Verifies the NAMED ``MICRO`` preset is a faithful, DE-RISK-ONLY composition of the
already-built primitives:

    verified + LP locked >= 30d + honeypot PASS + top-5 holders < 20%
    + 0.5% sizing (hard-clamped by caps) + (15-20% stop OR 24h time-exit)

Every test asserts the preset can only REJECT / SHRINK / EXIT — never add risk,
never raise a size above the caps, never widen a stop, never pass a candidate the
base gate would have rejected.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from aats.contracts.risk import RiskConfig
from aats.risk.exit_engine import ExitEngine, ExitReason, ExitState, MevExitMode
from aats.risk.presets import (
    BPS_DENOM,
    ENTRY_PRESETS,
    LAMPORTS_PER_SOL,
    MICRO_EXIT_CONFIG,
    MICRO_HARD_STOP_R,
    MICRO_MIN_LP_LOCK_DAYS,
    MICRO_PRESET,
    MICRO_PRESET_LABEL,
    MICRO_SIZE_FRACTION_BPS,
    MICRO_TOP5_CONCENTRATION_MAX_BPS,
    MicroSafetyFacts,
    entry_preset,
    micro_red_flag_codes,
    micro_size,
)
from aats.risk.pretrade_gate import (
    GateVerdict,
    PreTradeSafetyGate,
    SafetyInputs,
    SafetyThresholds,
)
from aats.risk.sizing import DeRiskSignals, FractionalKellySizer

# --------------------------------------------------------------------------- #
# Fixtures — a CLEAN candidate (passes every MICRO constraint) we then degrade.
# --------------------------------------------------------------------------- #

_EVENT_SLOT = 250_000_000
_EVENT_MS = 1_700_000_000_000


def _clean_inputs() -> SafetyInputs:
    """A token that passes EVERY base + MICRO safety check."""
    return SafetyInputs(
        mint="MiCr0Cl3an1111111111111111111111111111111",
        event_slot=_EVENT_SLOT,
        event_block_time_ms=_EVENT_MS,
        data_staleness_ms=100,
        freeze_authority=None,  # renounced
        mint_authority=None,  # renounced
        lp_locked_or_burned_bps=BPS_DENOM,  # 100% locked
        dev_bundle_cluster_bps=1_000,  # 10% < 20%
        holder_concentration_top10_bps=1_500,  # 15% < 20% MICRO ceiling
        sell_tax_bps=0,
        buy_tax_bps=0,
        authorities_decoded=True,
    )


def _clean_facts() -> MicroSafetyFacts:
    """The MICRO extra facts for a clean candidate."""
    return MicroSafetyFacts(
        verified=True,
        lp_lock_age_days=45,  # >= 30
        holder_concentration_top5_bps=1_200,  # 12% < 20%
    )


class _SellableProbe:
    def is_sellable(self, mint: str, event_slot: int) -> bool:  # noqa: ARG002
        return True


class _HoneypotProbe:
    def is_sellable(self, mint: str, event_slot: int) -> bool:  # noqa: ARG002
        return False


# --------------------------------------------------------------------------- #
# 1. Composition identity — the named preset binds EXACTLY these constraints.
# --------------------------------------------------------------------------- #


def test_named_preset_resolves():
    assert entry_preset() is MICRO_PRESET
    assert entry_preset(MICRO_PRESET_LABEL) is MICRO_PRESET
    assert MICRO_PRESET_LABEL in ENTRY_PRESETS


def test_unknown_preset_fails_closed():
    with pytest.raises(KeyError):
        entry_preset("aggressive_yolo")  # no silent fallback to a looser preset


def test_micro_constants_match_audit_spec():
    # top-5 < 20%
    assert MICRO_TOP5_CONCENTRATION_MAX_BPS == 2_000
    # LP lock age >= 30 days
    assert MICRO_MIN_LP_LOCK_DAYS == 30
    # 0.5% sizing
    assert MICRO_SIZE_FRACTION_BPS == 50  # 0.50%
    # hard stop inside the 15-20% band
    drawdown = (Decimal(1) - MICRO_HARD_STOP_R) * BPS_DENOM
    assert 1_500 <= drawdown <= 2_000, f"stop {drawdown} bps not in 15-20% band"
    # 24h time-exit, narrative-independent (cooled threshold maxed => pure timeout)
    assert MICRO_EXIT_CONFIG.time_stop_hours == 24
    assert MICRO_EXIT_CONFIG.time_stop_narrative_cooled_at == Decimal(1)


def test_clean_candidate_passes_full_band():
    decision = MICRO_PRESET.evaluate_safety(_clean_inputs(), _clean_facts(), probe=_SellableProbe())
    assert decision.verdict is GateVerdict.PASS
    assert decision.red_flags == ()


# --------------------------------------------------------------------------- #
# 2. Each constraint REJECTS when violated (reject-only / refuse-by-default).
# --------------------------------------------------------------------------- #


def test_not_verified_rejects():
    facts = MicroSafetyFacts(
        verified=False, lp_lock_age_days=45, holder_concentration_top5_bps=1200
    )
    d = MICRO_PRESET.evaluate_safety(_clean_inputs(), facts, probe=_SellableProbe())
    assert d.verdict is GateVerdict.REJECT
    assert "not_verified" in micro_red_flag_codes(d)


def test_verified_unknown_refuses_by_default():
    facts = MicroSafetyFacts(verified=None, lp_lock_age_days=45, holder_concentration_top5_bps=1200)
    d = MICRO_PRESET.evaluate_safety(_clean_inputs(), facts, probe=_SellableProbe())
    assert d.verdict is GateVerdict.REJECT  # unknown == reject, never pass


def test_lp_lock_too_short_rejects():
    facts = MicroSafetyFacts(verified=True, lp_lock_age_days=29, holder_concentration_top5_bps=1200)
    d = MICRO_PRESET.evaluate_safety(_clean_inputs(), facts, probe=_SellableProbe())
    assert d.verdict is GateVerdict.REJECT
    assert "lp_lock_too_short" in micro_red_flag_codes(d)


def test_lp_lock_age_unknown_refuses_by_default():
    facts = MicroSafetyFacts(
        verified=True, lp_lock_age_days=None, holder_concentration_top5_bps=1200
    )
    d = MICRO_PRESET.evaluate_safety(_clean_inputs(), facts, probe=_SellableProbe())
    assert d.verdict is GateVerdict.REJECT


def test_lp_lock_exactly_30d_passes():
    facts = MicroSafetyFacts(verified=True, lp_lock_age_days=30, holder_concentration_top5_bps=1200)
    d = MICRO_PRESET.evaluate_safety(_clean_inputs(), facts, probe=_SellableProbe())
    assert d.verdict is GateVerdict.PASS


def test_top5_concentration_at_or_above_20pct_rejects():
    # exactly 20% must REJECT (strict < 20%)
    facts = MicroSafetyFacts(verified=True, lp_lock_age_days=45, holder_concentration_top5_bps=2000)
    d = MICRO_PRESET.evaluate_safety(_clean_inputs(), facts, probe=_SellableProbe())
    assert d.verdict is GateVerdict.REJECT
    assert "top5_concentration_high" in micro_red_flag_codes(d)


def test_top5_just_under_20pct_passes():
    facts = MicroSafetyFacts(verified=True, lp_lock_age_days=45, holder_concentration_top5_bps=1999)
    d = MICRO_PRESET.evaluate_safety(_clean_inputs(), facts, probe=_SellableProbe())
    assert d.verdict is GateVerdict.PASS


def test_top5_unknown_refuses_by_default():
    facts = MicroSafetyFacts(verified=True, lp_lock_age_days=45, holder_concentration_top5_bps=None)
    d = MICRO_PRESET.evaluate_safety(_clean_inputs(), facts, probe=_SellableProbe())
    assert d.verdict is GateVerdict.REJECT


def test_honeypot_rejects():
    d = MICRO_PRESET.evaluate_safety(_clean_inputs(), _clean_facts(), probe=_HoneypotProbe())
    assert d.verdict is GateVerdict.REJECT
    assert "not_sellable" in micro_red_flag_codes(d)


def test_no_probe_refuses_by_default():
    # honeypot sell-sim seam unwired => refuse-by-default (not-proven-sellable)
    d = MICRO_PRESET.evaluate_safety(_clean_inputs(), _clean_facts(), probe=None)
    assert d.verdict is GateVerdict.REJECT
    assert "not_sellable" in micro_red_flag_codes(d)


def test_lp_fraction_not_fully_locked_rejects():
    # MICRO tightens LP to 100%: a 96% locked LP that passes the DEFAULT 95% gate
    # must be REJECTED under MICRO.
    inp = _clean_inputs()
    inp = SafetyInputs(
        mint=inp.mint,
        event_slot=inp.event_slot,
        event_block_time_ms=inp.event_block_time_ms,
        data_staleness_ms=inp.data_staleness_ms,
        freeze_authority=None,
        mint_authority=None,
        lp_locked_or_burned_bps=9_600,  # 96% — passes default, fails MICRO 100%
        dev_bundle_cluster_bps=1_000,
        holder_concentration_top10_bps=1_500,
        sell_tax_bps=0,
        buy_tax_bps=0,
    )
    # default gate passes 96%
    assert PreTradeSafetyGate().evaluate_fast(inp).verdict is GateVerdict.PASS
    # MICRO rejects 96%
    d = MICRO_PRESET.evaluate_safety(inp, _clean_facts(), probe=_SellableProbe())
    assert d.verdict is GateVerdict.REJECT


# --------------------------------------------------------------------------- #
# 3. TIGHTEN-ONLY — MICRO never passes a candidate the DEFAULT gate rejects.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "field,value",
    [
        ("freeze_authority", "Fr0z3nAuth"),
        ("mint_authority", "M1ntAuth"),
        ("lp_locked_or_burned_bps", 5_000),
        ("dev_bundle_cluster_bps", 3_000),
        ("holder_concentration_top10_bps", 5_000),
        ("sell_tax_bps", 2_000),
        ("buy_tax_bps", 2_000),
    ],
)
def test_micro_is_superset_of_default_rejections(field, value):
    base = _clean_inputs()
    kwargs = {
        "mint": base.mint,
        "event_slot": base.event_slot,
        "event_block_time_ms": base.event_block_time_ms,
        "data_staleness_ms": base.data_staleness_ms,
        "freeze_authority": base.freeze_authority,
        "mint_authority": base.mint_authority,
        "lp_locked_or_burned_bps": base.lp_locked_or_burned_bps,
        "dev_bundle_cluster_bps": base.dev_bundle_cluster_bps,
        "holder_concentration_top10_bps": base.holder_concentration_top10_bps,
        "sell_tax_bps": base.sell_tax_bps,
        "buy_tax_bps": base.buy_tax_bps,
    }
    kwargs[field] = value
    inp = SafetyInputs(**kwargs)
    # If the DEFAULT gate rejects it, MICRO (tighter) must also reject it.
    default_decision = PreTradeSafetyGate().evaluate_fast(inp)
    if default_decision.verdict is GateVerdict.REJECT:
        micro_decision = MICRO_PRESET.evaluate_safety(inp, _clean_facts(), probe=_SellableProbe())
        assert micro_decision.verdict is GateVerdict.REJECT


# --------------------------------------------------------------------------- #
# 4. 0.5% SIZING — small fraction, hard-clamped by the existing caps.
# --------------------------------------------------------------------------- #


def _sizer() -> FractionalKellySizer:
    return FractionalKellySizer(RiskConfig())


def test_micro_size_is_half_percent_of_bankroll_when_that_binds():
    # Bankroll 100 SOL; 0.5% = 0.5 SOL. The per-trade cap is 0.1 SOL, so the
    # cap-clamped Kelly size (<= 0.1 SOL) binds BELOW the 0.5% ceiling here —
    # which is the point: the caps always bind first.
    bankroll = 100 * LAMPORTS_PER_SOL
    res = micro_size(
        _sizer(),
        p_calibrated=0.99,
        uncertainty=0.0,
        current_aggregate_lamports=0,
        bankroll_lamports=bankroll,
    )
    half_pct = bankroll * MICRO_SIZE_FRACTION_BPS // BPS_DENOM
    assert res.micro_ceiling_lamports == half_pct == 500_000_000  # 0.5 SOL
    # final never exceeds EITHER the per-trade cap OR the 0.5% ceiling
    assert res.final_lamports <= RiskConfig().per_trade_cap_lamports
    assert res.final_lamports <= res.micro_ceiling_lamports


def test_micro_ceiling_binds_below_per_trade_cap_for_small_bankroll():
    # Bankroll 10 SOL; 0.5% = 0.05 SOL < 0.1 SOL per-trade cap. The 0.5% ceiling
    # is the tighter bound and must bind.
    bankroll = 10 * LAMPORTS_PER_SOL
    res = micro_size(
        _sizer(),
        p_calibrated=0.99,
        uncertainty=0.0,
        current_aggregate_lamports=0,
        bankroll_lamports=bankroll,
    )
    assert res.micro_ceiling_lamports == 50_000_000  # 0.05 SOL
    assert res.final_lamports <= 50_000_000


def test_micro_size_never_exceeds_caps_under_extreme_inputs():
    cfg = RiskConfig()
    sizer = _sizer()
    for p in (0.5, 0.66, 0.9, 0.999, 1.0):
        for unc in (0.0, 0.5, 1.0):
            for bankroll in (0, LAMPORTS_PER_SOL, 1000 * LAMPORTS_PER_SOL):
                res = micro_size(
                    sizer,
                    p_calibrated=p,
                    uncertainty=unc,
                    current_aggregate_lamports=0,
                    bankroll_lamports=bankroll,
                )
                assert res.final_lamports >= 0
                assert res.final_lamports <= cfg.per_trade_cap_lamports
                assert res.final_lamports <= cfg.max_aggregate_lamports
                ceiling = bankroll * MICRO_SIZE_FRACTION_BPS // BPS_DENOM
                assert res.final_lamports <= ceiling


def test_de_risk_signal_only_shrinks_micro_size():
    sizer = _sizer()
    bankroll = 10 * LAMPORTS_PER_SOL
    base = micro_size(
        sizer,
        p_calibrated=0.99,
        uncertainty=0.0,
        current_aggregate_lamports=0,
        bankroll_lamports=bankroll,
    )
    shrunk = micro_size(
        sizer,
        p_calibrated=0.99,
        uncertainty=0.0,
        current_aggregate_lamports=0,
        bankroll_lamports=bankroll,
        signals=DeRiskSignals(sentiment_factor=0.5, confidence_factor=0.5),
    )
    assert shrunk.final_lamports <= base.final_lamports  # a signal can only shrink


def test_de_risk_signals_cannot_represent_a_size_up():
    # Structural: DeRiskSignals rejects any factor > 1 (cannot size up).
    with pytest.raises(ValueError):
        DeRiskSignals(sentiment_factor=1.5)
    with pytest.raises(ValueError):
        DeRiskSignals(confidence_factor=2.0)


def test_aggregate_cap_binds_micro_size():
    # If the firm is already at the aggregate cap, MICRO size must be 0.
    cfg = RiskConfig()
    res = micro_size(
        _sizer(),
        p_calibrated=0.99,
        uncertainty=0.0,
        current_aggregate_lamports=cfg.max_aggregate_lamports,
        bankroll_lamports=1000 * LAMPORTS_PER_SOL,
    )
    assert res.final_lamports == 0  # no headroom => no size


# --------------------------------------------------------------------------- #
# 5. EXIT — 18% survivable hard stop AND 24h event-time time-exit both fire.
# --------------------------------------------------------------------------- #


def _micro_state() -> ExitState:
    return ExitState.at_entry(mint="X", entry_fill_price=Decimal("1"), config=MICRO_EXIT_CONFIG)


def test_hard_stop_fires_at_18pct_drawdown():
    eng = ExitEngine(config=MICRO_EXIT_CONFIG)
    state = _micro_state()
    # mark drops 18% -> R = 0.82 -> hard stop fires (full flatten)
    d = eng.on_tick(state, mark_price=Decimal("0.82"), block_time_ms=_EVENT_MS + 1)
    assert d.reason is ExitReason.HARD_STOP
    assert d.fraction_bps == 10_000  # full flatten
    assert d.exit_mode is MevExitMode.SECURE  # defensive exits route Secure


def test_above_18pct_drawdown_does_not_stop():
    eng = ExitEngine(config=MICRO_EXIT_CONFIG)
    state = _micro_state()
    d = eng.on_tick(state, mark_price=Decimal("0.83"), block_time_ms=_EVENT_MS + 1)
    assert d.reason is not ExitReason.HARD_STOP  # -17% is inside the line, holds


def test_24h_time_exit_fires_on_flat_bag_regardless_of_narrative():
    eng = ExitEngine(config=MICRO_EXIT_CONFIG)
    state = _micro_state()
    # Stay flat (mark ~ entry) for 24h of EVENT-TIME; narrative WARM (1.0). The
    # MICRO time-exit fires anyway (cooled threshold == 1.0 => always eligible).
    # Anchor the flat clock on the first tick, then tick a FULL 24h later.
    anchor_ms = _EVENT_MS + 1
    t24 = anchor_ms + 24 * 3_600_000  # exactly 24h of flat event-time after the anchor
    state = eng.on_tick(
        state, mark_price=Decimal("1.0"), block_time_ms=anchor_ms, narrative_score=Decimal(1)
    ).next_state
    d = eng.on_tick(state, mark_price=Decimal("1.0"), block_time_ms=t24, narrative_score=Decimal(1))
    assert d.reason is ExitReason.STALE_NARRATIVE_TIME_STOP
    assert d.fraction_bps == 10_000  # full exit


def test_time_exit_does_not_fire_before_24h():
    eng = ExitEngine(config=MICRO_EXIT_CONFIG)
    state = _micro_state()
    state = eng.on_tick(
        state, mark_price=Decimal("1.0"), block_time_ms=_EVENT_MS + 1, narrative_score=Decimal(1)
    ).next_state
    t23 = _EVENT_MS + 23 * 3_600_000
    d = eng.on_tick(state, mark_price=Decimal("1.0"), block_time_ms=t23, narrative_score=Decimal(1))
    assert d.reason is not ExitReason.STALE_NARRATIVE_TIME_STOP  # 23h < 24h, holds


def test_exit_config_is_de_risk_shaped():
    # The ExitConfig constructor already enforces de-risk shape (ascending TP,
    # ascending ratchet, stop in (0,1)). Constructing MICRO_EXIT_CONFIG at import
    # would have raised otherwise; assert the key invariants explicitly.
    assert 0 < MICRO_EXIT_CONFIG.hard_stop_r < 1
    assert MICRO_EXIT_CONFIG.default_exit_mode is MevExitMode.SECURE


# --------------------------------------------------------------------------- #
# 6. POINT-IN-TIME — no float money/age; safety facts reject float.
# --------------------------------------------------------------------------- #


def test_facts_reject_float_age():
    with pytest.raises(TypeError):
        MicroSafetyFacts(verified=True, lp_lock_age_days=30.0, holder_concentration_top5_bps=1000)


def test_facts_reject_float_concentration():
    with pytest.raises(TypeError):
        MicroSafetyFacts(verified=True, lp_lock_age_days=30, holder_concentration_top5_bps=1000.0)


def test_micro_size_rejects_float_bankroll():
    with pytest.raises(TypeError):
        micro_size(
            _sizer(),
            p_calibrated=0.9,
            uncertainty=0.0,
            current_aggregate_lamports=0,
            bankroll_lamports=1.5,  # float money — forbidden
        )


def test_safety_thresholds_are_tighten_only_vs_default():
    default = SafetyThresholds()
    micro = MICRO_PRESET.safety_thresholds
    # MICRO is at least as strict on every knob (tighten-only).
    assert micro.min_lp_locked_bps >= default.min_lp_locked_bps
    assert micro.max_holder_concentration_top10_bps <= default.max_holder_concentration_top10_bps
    assert micro.max_dev_bundle_cluster_bps <= default.max_dev_bundle_cluster_bps
    assert micro.max_sell_tax_bps <= default.max_sell_tax_bps
    assert micro.max_buy_tax_bps <= default.max_buy_tax_bps
