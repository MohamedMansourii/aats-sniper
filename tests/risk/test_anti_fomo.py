"""E13 — Anti-FOMO / already-pumped EXCLUSION filter tests (de-risk / selectivity).

VERIFIES (per E13 directive):
  1. An ALREADY-PUMPED token (recent >300% run-up, configurable) is EXCLUDED with a
     structured reason (FOMO_ALREADY_PUMPED).
  2. A warm-band run-up (>=warm floor, <hard ceiling) is DOWN-WEIGHTED with a reason
     (conviction multiplier < 1), NOT killed.
  3. A MAINSTREAM / CEX-listing / Forbes-tier mention is an EXCLUSION signal, NOT a buy
     trigger: a HARD tier REJECTS; a SOFT tier DOWN-WEIGHTS.  No tier ever passes/buys.
  4. DE-RISK ONLY / asymmetric: the conviction multiplier is STRUCTURALLY clamped to
     [0, 1] — a FomoDecision can never raise conviction/size; ``apply`` only shrinks.
  5. Refuse-by-default on STALE data (INPUT_STALE => REJECT); but ABSENCE of pump
     history is NO opinion (NO_PUMP_HISTORY), never an exclusion (a brand-new token
     legitimately has no run-up).
  6. Tunability: tightening the hard ceiling only EXCLUDES MORE (a PASS/down-weight can
     become a REJECT; a REJECT never becomes a PASS by tightening).
  7. Point-in-time: the filter reads only as-of inputs + the event-time anchor; no
     wall-clock / forward read is expressible.
  8. Money: price history is int lamports; the pump RATIO is exact Decimal (no float).
  9. SLOW-loop / selectivity-only: the decision exposes no buy/size/stop/execution
     field; ``passed`` on a down-weight still de-rates conviction.

HARD RULES honored:
  * No network, no signer, no RPC, no capital.
  * No secrets.
  * No win-rate field anywhere.
  * DRY-RUN unaffected: the filter touches no capital / signer / RPC.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from aats.risk.anti_fomo import (
    AntiFomoFilter,
    AntiFomoInputs,
    AntiFomoThresholds,
    FomoDecision,
    FomoReason,
    FomoVerdict,
    MentionTier,
    parse_mention_tier,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

ANCHOR_MS = 1_718_700_000_000
ANCHOR_SLOT = 300_000_000


def _inputs(
    *,
    price_history_lamports: tuple[int, ...] = (),
    mention_tiers: tuple[str, ...] = (),
    data_staleness_ms: int = 1_000,
    mint: str = "MintFomo111",
) -> AntiFomoInputs:
    return AntiFomoInputs(
        mint=mint,
        event_slot=ANCHOR_SLOT,
        event_block_time_ms=ANCHOR_MS,
        data_staleness_ms=data_staleness_ms,
        price_history_lamports=price_history_lamports,
        mention_tiers=mention_tiers,
    )


# ---------------------------------------------------------------------------
# 1. Already-pumped token is EXCLUDED with a reason
# ---------------------------------------------------------------------------


def test_already_pumped_token_excluded_with_reason():
    flt = AntiFomoFilter()  # default hard ratio 4.0 => +300%
    # 1000 -> 5000 lamports = 5x = +400% run-up, well past the +300% ceiling.
    decision = flt.evaluate(_inputs(price_history_lamports=(1_000, 2_000, 5_000)))
    assert decision.verdict is FomoVerdict.REJECT
    assert decision.excluded is True
    assert decision.passed is False
    assert FomoReason.FOMO_ALREADY_PUMPED.value in decision.exclusion_codes
    # A REJECT carries a zero conviction multiplier => zero size.
    assert decision.conviction_multiplier == Decimal(0)
    assert decision.apply(1_000_000) == 0
    # The reason records the measured run-up ratio vs the threshold (for the UI).
    hit = next(h for h in decision.reasons if h.reason is FomoReason.FOMO_ALREADY_PUMPED)
    assert hit.measured == Decimal(5)
    assert hit.threshold == Decimal("4.0")


def test_pump_exactly_at_hard_ceiling_excluded():
    flt = AntiFomoFilter()
    # 1000 -> 4000 = 4.0x exactly = +300%, the ceiling is inclusive (>=).
    decision = flt.evaluate(_inputs(price_history_lamports=(1_000, 4_000)))
    assert decision.verdict is FomoVerdict.REJECT
    assert FomoReason.FOMO_ALREADY_PUMPED.value in decision.exclusion_codes


def test_pump_anchor_is_window_minimum_not_first_sample():
    flt = AntiFomoFilter()
    # The run-up is measured from the window MINIMUM (most adverse), not the first
    # sample: 3000, 800 (dip), 4000 => anchor 800 => 5x => REJECT.
    decision = flt.evaluate(_inputs(price_history_lamports=(3_000, 800, 4_000)))
    assert decision.verdict is FomoVerdict.REJECT
    hit = next(h for h in decision.reasons if h.reason is FomoReason.FOMO_ALREADY_PUMPED)
    assert hit.measured == Decimal(5)


# ---------------------------------------------------------------------------
# 2. Warm-band run-up is DOWN-WEIGHTED (not killed)
# ---------------------------------------------------------------------------


def test_warm_band_pump_downweights_not_rejects():
    flt = AntiFomoFilter()  # warm 2.0, hard 4.0, max haircut 0.50
    # 1000 -> 3000 = 3x, mid warm band => DOWN_WEIGHT with multiplier < 1.
    decision = flt.evaluate(_inputs(price_history_lamports=(1_000, 3_000)))
    assert decision.verdict is FomoVerdict.DOWN_WEIGHT
    assert decision.down_weighted is True
    assert decision.passed is True  # passed-but-de-rated
    assert decision.excluded is False
    assert FomoReason.FOMO_PUMP_DOWNWEIGHT.value in decision.reason_codes
    assert Decimal(0) <= decision.conviction_multiplier < Decimal(1)
    # 3x is the midpoint of [2,4) => haircut = 0.5 * 0.5 = 0.25 => multiplier 0.75.
    assert decision.conviction_multiplier == Decimal("0.75")
    # apply() only shrinks an already-capped size.
    assert decision.apply(1_000_000) == 750_000


def test_below_warm_floor_passes_silently():
    flt = AntiFomoFilter()
    # 1000 -> 1500 = 1.5x, below the warm floor (2.0) => PASS, multiplier 1.
    decision = flt.evaluate(_inputs(price_history_lamports=(1_000, 1_500)))
    assert decision.verdict is FomoVerdict.PASS
    assert decision.conviction_multiplier == Decimal(1)
    assert decision.apply(1_000_000) == 1_000_000


# ---------------------------------------------------------------------------
# 3. Mainstream / CEX / Forbes mention is an EXCLUSION, NOT a buy trigger
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tier",
    [MentionTier.MAINSTREAM_PRESS.value, MentionTier.MAJOR_CEX_LISTING.value],
)
def test_hard_mainstream_mention_excludes(tier: str):
    flt = AntiFomoFilter()
    # No pump at all, but a Forbes-tier / major-CEX mention => REJECT (contrarian).
    decision = flt.evaluate(_inputs(price_history_lamports=(1_000, 1_000), mention_tiers=(tier,)))
    assert decision.verdict is FomoVerdict.REJECT
    assert FomoReason.FOMO_MAINSTREAM_MENTION.value in decision.exclusion_codes
    assert decision.conviction_multiplier == Decimal(0)
    # The mention drove the verdict and is surfaced as a candidate reject reason.
    hit = next(h for h in decision.reasons if h.reason is FomoReason.FOMO_MAINSTREAM_MENTION)
    assert hit.detail == tier


def test_mainstream_mention_is_never_a_buy_trigger():
    """A mainstream mention can only REJECT/de-rate — never produce a PASS-only buy."""
    flt = AntiFomoFilter()
    decision = flt.evaluate(
        _inputs(
            price_history_lamports=(1_000, 1_000),
            mention_tiers=(MentionTier.MAINSTREAM_PRESS.value,),
        )
    )
    # The verdict for a hard mainstream mention is REJECT.  There is NO verdict value
    # that means "buy", and a mention can never improve the multiplier above 1.
    assert decision.verdict is FomoVerdict.REJECT
    assert decision.conviction_multiplier <= Decimal(1)
    # No field on the decision sizes/triggers/executes anything.
    forbidden = {"size", "buy", "amount", "lamports", "intent", "order", "trigger"}
    for name in vars(decision):
        assert name.lower() not in forbidden


@pytest.mark.parametrize(
    "tier",
    [MentionTier.MINOR_CEX_LISTING.value, MentionTier.MAINSTREAM_INFLUENCER.value],
)
def test_soft_mention_downweights(tier: str):
    flt = AntiFomoFilter()  # soft multiplier 0.50
    decision = flt.evaluate(_inputs(price_history_lamports=(1_000, 1_000), mention_tiers=(tier,)))
    assert decision.verdict is FomoVerdict.DOWN_WEIGHT
    assert FomoReason.FOMO_SOFT_MENTION.value in decision.reason_codes
    assert decision.conviction_multiplier == Decimal("0.50")
    assert decision.apply(1_000_000) == 500_000


def test_unknown_mention_tier_ignored_not_an_exclusion():
    flt = AntiFomoFilter()
    # A typo'd / unknown tier must NOT silently exclude or widen the filter.
    decision = flt.evaluate(
        _inputs(price_history_lamports=(1_000, 1_000), mention_tiers=("some_garbage_tier",))
    )
    assert decision.verdict is FomoVerdict.PASS
    assert parse_mention_tier("some_garbage_tier") is None


def test_hard_mention_dominates_soft_and_pump():
    flt = AntiFomoFilter()
    # Hard mention present alongside a warm pump and a soft mention => REJECT wins.
    decision = flt.evaluate(
        _inputs(
            price_history_lamports=(1_000, 3_000),
            mention_tiers=(
                MentionTier.MAINSTREAM_PRESS.value,
                MentionTier.MINOR_CEX_LISTING.value,
            ),
        )
    )
    assert decision.verdict is FomoVerdict.REJECT
    assert decision.conviction_multiplier == Decimal(0)


def test_compounding_downweights_multiply_downward():
    flt = AntiFomoFilter()
    # Warm pump (3x => 0.75) AND a soft mention (0.50) compound DOWNWARD: 0.75 * 0.5.
    decision = flt.evaluate(
        _inputs(
            price_history_lamports=(1_000, 3_000),
            mention_tiers=(MentionTier.MAINSTREAM_INFLUENCER.value,),
        )
    )
    assert decision.verdict is FomoVerdict.DOWN_WEIGHT
    assert decision.conviction_multiplier == Decimal("0.375")
    assert decision.conviction_multiplier < Decimal("0.50")  # strictly more de-risked


# ---------------------------------------------------------------------------
# 4. DE-RISK ONLY / asymmetric — multiplier structurally clamped to [0, 1]
# ---------------------------------------------------------------------------


def test_pass_multiplier_is_exactly_one():
    flt = AntiFomoFilter()
    decision = flt.evaluate(_inputs(price_history_lamports=(1_000, 1_100)))
    assert decision.verdict is FomoVerdict.PASS
    assert decision.conviction_multiplier == Decimal(1)


def test_apply_never_grows_a_capped_size():
    """``apply`` is DE-RISK ONLY: output is always <= the already-capped input."""
    flt = AntiFomoFilter()
    capped = 7_777_777
    for hist, mentions in [
        ((1_000, 1_100), ()),  # pass
        ((1_000, 3_000), ()),  # warm down-weight
        ((1_000, 1_000), (MentionTier.MINOR_CEX_LISTING.value,)),  # soft down-weight
        ((1_000, 9_000), ()),  # reject => 0
    ]:
        decision = flt.evaluate(_inputs(price_history_lamports=hist, mention_tiers=mentions))
        out = decision.apply(capped)
        assert 0 <= out <= capped


def test_fomo_decision_rejects_multiplier_above_one():
    """A FomoDecision with multiplier > 1 is STRUCTURALLY impossible (raises)."""
    with pytest.raises(ValueError, match="conviction_multiplier must be in"):
        FomoDecision(
            mint="X",
            verdict=FomoVerdict.PASS,
            conviction_multiplier=Decimal("1.5"),  # risk-increasing => rejected
            reasons=(),
            event_slot=ANCHOR_SLOT,
            event_block_time_ms=ANCHOR_MS,
        )


def test_fomo_decision_reject_must_be_zero_multiplier():
    with pytest.raises(ValueError, match="REJECT must carry"):
        FomoDecision(
            mint="X",
            verdict=FomoVerdict.REJECT,
            conviction_multiplier=Decimal("0.5"),
            reasons=(),
            event_slot=ANCHOR_SLOT,
            event_block_time_ms=ANCHOR_MS,
        )


def test_fomo_decision_pass_must_be_unit_multiplier():
    with pytest.raises(ValueError, match="PASS must carry"):
        FomoDecision(
            mint="X",
            verdict=FomoVerdict.PASS,
            conviction_multiplier=Decimal("0.9"),
            reasons=(),
            event_slot=ANCHOR_SLOT,
            event_block_time_ms=ANCHOR_MS,
        )


# ---------------------------------------------------------------------------
# 5. Refuse-by-default on STALE; absence of pump history is NO opinion
# ---------------------------------------------------------------------------


def test_stale_bundle_excluded_refuse_by_default():
    flt = AntiFomoFilter(thresholds=AntiFomoThresholds(max_staleness_ms=60_000))
    decision = flt.evaluate(
        _inputs(price_history_lamports=(1_000, 1_000), data_staleness_ms=120_000)
    )
    assert decision.verdict is FomoVerdict.REJECT
    assert FomoReason.INPUT_STALE.value in decision.exclusion_codes
    assert decision.conviction_multiplier == Decimal(0)


def test_no_price_history_is_no_opinion_not_exclusion():
    flt = AntiFomoFilter()
    # A brand-new token with no usable history => NO_PUMP_HISTORY (info) => PASS.
    decision = flt.evaluate(_inputs(price_history_lamports=()))
    assert decision.verdict is FomoVerdict.PASS
    assert FomoReason.NO_PUMP_HISTORY.value in decision.reason_codes
    assert decision.excluded is False
    assert decision.conviction_multiplier == Decimal(1)


def test_single_sample_is_no_opinion():
    flt = AntiFomoFilter()
    decision = flt.evaluate(_inputs(price_history_lamports=(5_000,)))
    assert decision.verdict is FomoVerdict.PASS
    assert FomoReason.NO_PUMP_HISTORY.value in decision.reason_codes


def test_zero_anchor_is_no_opinion():
    flt = AntiFomoFilter()
    # Degenerate floor price (0) cannot form a ratio => no opinion, NOT a reject.
    decision = flt.evaluate(_inputs(price_history_lamports=(0, 5_000)))
    assert decision.verdict is FomoVerdict.PASS
    assert FomoReason.NO_PUMP_HISTORY.value in decision.reason_codes


# ---------------------------------------------------------------------------
# 6. Tunability — tightening only excludes MORE
# ---------------------------------------------------------------------------


def test_tightening_hard_ceiling_excludes_more():
    # 1000 -> 2500 = 2.5x.  Under default hard 4.0 this is a warm DOWN_WEIGHT.
    loose = AntiFomoFilter()
    d_loose = loose.evaluate(_inputs(price_history_lamports=(1_000, 2_500)))
    assert d_loose.verdict is FomoVerdict.DOWN_WEIGHT

    # Tighten the hard ceiling to 2.5x => the SAME token now REJECTS.
    tight = AntiFomoFilter(
        thresholds=AntiFomoThresholds(hard_pump_ratio=Decimal("2.5"), warm_pump_ratio=Decimal("2"))
    )
    d_tight = tight.evaluate(_inputs(price_history_lamports=(1_000, 2_500)))
    assert d_tight.verdict is FomoVerdict.REJECT


def test_tightening_never_turns_reject_into_pass():
    # A token that REJECTS at a loose ceiling must never PASS at a tighter one.
    hist = (1_000, 6_000)  # 6x
    loose = AntiFomoFilter()
    assert loose.evaluate(_inputs(price_history_lamports=hist)).verdict is FomoVerdict.REJECT
    tight = AntiFomoFilter(thresholds=AntiFomoThresholds(hard_pump_ratio=Decimal("3")))
    assert tight.evaluate(_inputs(price_history_lamports=hist)).verdict is FomoVerdict.REJECT


def test_configurable_pump_ratio_directive_300_percent():
    # The directive's >300% default == ratio 4.0.  Confirm 4x rejects, 3.99x does not.
    flt = AntiFomoFilter()
    assert flt.evaluate(_inputs(price_history_lamports=(100, 400))).verdict is FomoVerdict.REJECT
    # 100 -> 399 = 3.99x => warm band (>= 2.0, < 4.0) => DOWN_WEIGHT, not REJECT.
    assert (
        flt.evaluate(_inputs(price_history_lamports=(100, 399))).verdict is FomoVerdict.DOWN_WEIGHT
    )


# ---------------------------------------------------------------------------
# 7. Point-in-time — anchor carried through; no wall-clock / forward read
# ---------------------------------------------------------------------------


def test_event_anchor_carried_onto_decision():
    flt = AntiFomoFilter()
    decision = flt.evaluate(_inputs(price_history_lamports=(1_000, 5_000)))
    assert decision.event_slot == ANCHOR_SLOT
    assert decision.event_block_time_ms == ANCHOR_MS


def test_no_forward_slot_field_expressible():
    # AntiFomoInputs has no field that admits an event_time + H sample; the price
    # window is a plain tuple the caller fills with slots <= anchor.  Assert the input
    # surface carries no 'future'/'lookahead'/'horizon' field.
    fields = set(AntiFomoInputs.__dataclass_fields__)
    for banned in ("future", "lookahead", "horizon", "next", "forward"):
        assert not any(banned in f for f in fields)


# ---------------------------------------------------------------------------
# 8. Money — int lamports in, exact Decimal ratio (no float)
# ---------------------------------------------------------------------------


def test_price_history_must_be_int_lamports():
    with pytest.raises(TypeError, match="int lamports"):
        AntiFomoInputs(
            mint="X",
            event_slot=ANCHOR_SLOT,
            event_block_time_ms=ANCHOR_MS,
            data_staleness_ms=0,
            price_history_lamports=(1_000, 5_000.0),  # float price => money-rule violation
        )


def test_pump_ratio_is_exact_decimal():
    flt = AntiFomoFilter()
    # 3 -> 10 = 10/3, an infinite binary fraction; Decimal keeps it exact and the
    # band classification is correct (3.333.. is in [2,4) => warm DOWN_WEIGHT).
    decision = flt.evaluate(_inputs(price_history_lamports=(3, 10)))
    assert decision.verdict is FomoVerdict.DOWN_WEIGHT
    hit = next(h for h in decision.reasons if h.reason is FomoReason.FOMO_PUMP_DOWNWEIGHT)
    assert hit.measured == Decimal(10) / Decimal(3)


# ---------------------------------------------------------------------------
# 9. Thresholds validation (fail-closed config) + env parsing
# ---------------------------------------------------------------------------


def test_thresholds_reject_hard_below_warm():
    with pytest.raises(ValueError, match="hard_pump_ratio"):
        AntiFomoThresholds(hard_pump_ratio=Decimal("1.5"), warm_pump_ratio=Decimal("2.0"))


def test_thresholds_reject_haircut_out_of_range():
    with pytest.raises(ValueError, match="max_warm_pump_haircut"):
        AntiFomoThresholds(max_warm_pump_haircut=Decimal("1.5"))


def test_thresholds_reject_warm_below_one():
    with pytest.raises(ValueError, match="warm_pump_ratio"):
        AntiFomoThresholds(warm_pump_ratio=Decimal("0.5"))


def test_thresholds_from_env_defaults_and_override():
    # Empty env => defaults.
    t_default = AntiFomoThresholds.from_env({})
    assert t_default.hard_pump_ratio == Decimal("4.0")
    # Override hard ratio to 3x and soft multiplier to 0.25.
    t = AntiFomoThresholds.from_env(
        {
            "ANTI_FOMO_HARD_PUMP_RATIO": "3",
            "ANTI_FOMO_SOFT_MENTION_MULTIPLIER": "0.25",
        }
    )
    assert t.hard_pump_ratio == Decimal("3")
    assert t.soft_mention_multiplier == Decimal("0.25")
