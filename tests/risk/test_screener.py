"""E8 — Tunable discovery / SCREENER filter tests (late-entry / survivor niche).

VERIFIES (per E8 directive):
  1. Out-of-band candidates are EXCLUDED, each with a structured reason:
       * market cap below band / above band
       * liquidity below the minimum
       * 24h-volume-to-mcap below the turnover floor
  2. In-band ("survivor") candidates PASS with the IN_SURVIVOR_BAND info tag.
  3. Thresholds are TUNABLE (tighter band excludes more; looser band passes more)
     and tightening is always safe.
  4. SLOW-LOOP only / selectivity-only: the screen can ONLY reject or pass-through
     (no buy, no size, no stop, no execution path on the object).
  5. Refuse-by-default: missing enrichment data => INPUT_INCOMPLETE => REJECT;
     over-stale bundle => INPUT_STALE => REJECT.  No "unknown == in-band" path.
  6. Point-in-time: the screen reads only as-of inputs + the event-time anchor; no
     wall-clock / forward read is expressible.
  7. Volume-spike is a CONTRARIAN-AWARE INFO tag — never a standalone buy/pass.
  8. Money: USD comparisons are exact via Decimal; no float-drift mis-classification.
  9. Builds inputs from a real T-301 EnrichmentBundle (offline fixtures only).
 10. Property/fuzz: an in-band PASS is structurally impossible to turn into a buy;
     tightening any threshold never turns a REJECT into a PASS.

HARD RULES honored:
  * No network: enrichment uses OfflineFixtureTransport only.
  * No secrets: api_key="offline" placeholder only.
  * No win-rate field anywhere.
  * DRY-RUN unaffected: the screen touches no capital / signer / RPC.
"""

from __future__ import annotations

import asyncio
import random
from decimal import Decimal

import pytest

from aats.ingestion.enrichment import (
    DEXScreenerResult,
    EnrichmentBundle,
    EnrichmentMeta,
    build_offline_enrichment_client,
)
from aats.risk.screener import (
    DiscoveryScreener,
    ScreenDecision,
    ScreenerThresholds,
    ScreenInputs,
    ScreenReason,
    ScreenVerdict,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

ANCHOR_MS = 1_718_700_000_000
ANCHOR_SLOT = 300_000_000


def _inputs(
    *,
    market_cap_usd: float | None = 5_000_000.0,
    liquidity_usd: float | None = 200_000.0,
    volume_usd_h24: float | None = 1_000_000.0,
    baseline_volume_usd_h24: float | None = None,
    data_staleness_ms: int = 1_000,
    mint: str = "MintSurvivor111",
) -> ScreenInputs:
    return ScreenInputs(
        mint=mint,
        event_slot=ANCHOR_SLOT,
        event_block_time_ms=ANCHOR_MS,
        data_staleness_ms=data_staleness_ms,
        market_cap_usd=market_cap_usd,
        liquidity_usd=liquidity_usd,
        volume_usd_h24=volume_usd_h24,
        baseline_volume_usd_h24=baseline_volume_usd_h24,
    )


# ---------------------------------------------------------------------------
# 1. In-band survivor PASSES with the info tag
# ---------------------------------------------------------------------------


def test_in_band_survivor_passes_with_info_tag():
    screener = DiscoveryScreener()
    # 5M mcap, 200K liquidity, 1M volume => 20% turnover, all in-band.
    decision = screener.screen(_inputs())
    assert decision.verdict is ScreenVerdict.PASS
    assert decision.passed is True
    assert decision.excluded is False
    assert ScreenReason.IN_SURVIVOR_BAND.value in decision.reason_codes
    # PASS => no exclusion codes.
    assert decision.exclusion_codes == ()


# ---------------------------------------------------------------------------
# 2. Out-of-band exclusions, each with the right reason
# ---------------------------------------------------------------------------


def test_mcap_below_band_excluded():
    screener = DiscoveryScreener()
    decision = screener.screen(_inputs(market_cap_usd=50_000.0))  # below 70K default
    assert decision.verdict is ScreenVerdict.REJECT
    assert ScreenReason.MCAP_BELOW_BAND.value in decision.exclusion_codes


def test_mcap_above_band_excluded_already_run():
    screener = DiscoveryScreener()
    decision = screener.screen(_inputs(market_cap_usd=50_000_000.0))  # above 11M default
    assert decision.verdict is ScreenVerdict.REJECT
    assert ScreenReason.MCAP_ABOVE_BAND.value in decision.exclusion_codes


def test_liquidity_below_min_excluded():
    screener = DiscoveryScreener()
    decision = screener.screen(_inputs(liquidity_usd=5_000.0))  # below 20K default
    assert decision.verdict is ScreenVerdict.REJECT
    assert ScreenReason.LIQUIDITY_BELOW_MIN.value in decision.exclusion_codes


def test_volume_to_mcap_below_floor_excluded_dead_token():
    screener = DiscoveryScreener()
    # 5M mcap but only 100K volume => 2% turnover, below 10% default floor.
    decision = screener.screen(_inputs(volume_usd_h24=100_000.0))
    assert decision.verdict is ScreenVerdict.REJECT
    assert ScreenReason.VOLUME_TO_MCAP_BELOW_MIN.value in decision.exclusion_codes


def test_multiple_exclusions_all_surfaced():
    """All reasons are surfaced (no short-circuit) so the dashboard sees every one."""
    screener = DiscoveryScreener()
    decision = screener.screen(
        _inputs(market_cap_usd=50_000.0, liquidity_usd=5_000.0, volume_usd_h24=1_000.0)
    )
    assert decision.verdict is ScreenVerdict.REJECT
    codes = set(decision.exclusion_codes)
    assert ScreenReason.MCAP_BELOW_BAND.value in codes
    assert ScreenReason.LIQUIDITY_BELOW_MIN.value in codes


# ---------------------------------------------------------------------------
# 3. Thresholds are tunable; tightening excludes more
# ---------------------------------------------------------------------------


def test_thresholds_tunable_tighter_band_excludes_more():
    loose = DiscoveryScreener(
        thresholds=ScreenerThresholds(mcap_min_usd=10_000, mcap_max_usd=100_000_000)
    )
    tight = DiscoveryScreener(
        thresholds=ScreenerThresholds(mcap_min_usd=1_000_000, mcap_max_usd=8_000_000)
    )
    cand = _inputs(market_cap_usd=500_000.0)  # in loose band, below tight band
    assert loose.screen(cand).passed is True
    assert tight.screen(cand).excluded is True
    assert ScreenReason.MCAP_BELOW_BAND.value in tight.screen(cand).exclusion_codes


def test_tighter_turnover_floor_excludes_more():
    loose = DiscoveryScreener(thresholds=ScreenerThresholds(min_volume_to_mcap_ratio="0.01"))
    tight = DiscoveryScreener(thresholds=ScreenerThresholds(min_volume_to_mcap_ratio="0.50"))
    cand = _inputs(market_cap_usd=5_000_000.0, volume_usd_h24=500_000.0)  # 10% turnover
    assert loose.screen(cand).passed is True
    assert tight.screen(cand).excluded is True


def test_thresholds_reject_inverted_band():
    with pytest.raises(ValueError):
        ScreenerThresholds(mcap_min_usd=10_000_000, mcap_max_usd=1_000_000)


def test_thresholds_reject_out_of_range_ratio():
    with pytest.raises(ValueError):
        ScreenerThresholds(min_volume_to_mcap_ratio="1.5")


def test_thresholds_reject_spike_multiplier_below_one():
    with pytest.raises(ValueError):
        ScreenerThresholds(volume_spike_multiplier="0.5")


def test_thresholds_from_env_overrides_and_defaults():
    env = {
        "SCREENER_MCAP_MIN_USD": "100000",
        "SCREENER_MIN_VOLUME_TO_MCAP_RATIO": "0.20",
    }
    th = ScreenerThresholds.from_env(env)
    assert th.mcap_min_usd == Decimal("100000")
    assert th.min_volume_to_mcap_ratio == Decimal("0.20")
    # Unset keys fall back to defaults.
    assert th.mcap_max_usd == ScreenerThresholds.mcap_max_usd


# ---------------------------------------------------------------------------
# 4. Selectivity-only: no buy / size / stop / execution surface
# ---------------------------------------------------------------------------


def test_decision_is_reject_or_pass_only_no_execution_surface():
    """The screen object exposes NO field that sizes, sets a stop, or instructs a buy."""
    decision = DiscoveryScreener().screen(_inputs())
    fields = set(vars(decision).keys())
    forbidden = {
        "size",
        "size_lamports",
        "buy",
        "amount",
        "intent",
        "entry",
        "stop",
        "stop_price",
        "leverage",
        "order",
    }
    assert fields.isdisjoint(forbidden), f"screener decision leaked an execution field: {fields}"
    # The only verdict values are PASS / REJECT — there is no BUY verdict.
    assert set(ScreenVerdict) == {ScreenVerdict.PASS, ScreenVerdict.REJECT}


def test_pass_is_silent_not_a_buy():
    """A PASS means 'not excluded by the discovery band' — never 'buy'."""
    decision = DiscoveryScreener().screen(_inputs())
    assert decision.passed is True
    # passed is the strongest positive signal available; it is NOT a buy trigger.
    # There is no attribute that turns this PASS into an order.
    assert not hasattr(decision, "place_order")
    assert not hasattr(decision, "to_intent")


# ---------------------------------------------------------------------------
# 5. Refuse-by-default: missing / stale data excludes
# ---------------------------------------------------------------------------


def test_missing_mcap_refuses_by_default():
    decision = DiscoveryScreener().screen(_inputs(market_cap_usd=None))
    assert decision.excluded is True
    assert ScreenReason.INPUT_INCOMPLETE.value in decision.exclusion_codes


def test_missing_liquidity_refuses_by_default():
    decision = DiscoveryScreener().screen(_inputs(liquidity_usd=None))
    assert decision.excluded is True
    assert ScreenReason.INPUT_INCOMPLETE.value in decision.exclusion_codes


def test_missing_volume_refuses_by_default():
    decision = DiscoveryScreener().screen(_inputs(volume_usd_h24=None))
    assert decision.excluded is True
    assert ScreenReason.INPUT_INCOMPLETE.value in decision.exclusion_codes


def test_stale_bundle_refuses_by_default():
    decision = DiscoveryScreener().screen(_inputs(data_staleness_ms=999_999))
    assert decision.excluded is True
    assert ScreenReason.INPUT_STALE.value in decision.exclusion_codes


def test_zero_mcap_cannot_form_ratio_refuses():
    decision = DiscoveryScreener().screen(_inputs(market_cap_usd=0.0))
    assert decision.excluded is True
    # mcap below band AND incomplete turnover ratio — both are exclusion reasons.
    assert ScreenReason.INPUT_INCOMPLETE.value in decision.exclusion_codes


# ---------------------------------------------------------------------------
# 6. Point-in-time: anchor carried, no forward field
# ---------------------------------------------------------------------------


def test_event_time_anchor_carried_no_forward_field():
    inp = _inputs()
    decision = DiscoveryScreener().screen(inp)
    assert decision.event_slot == ANCHOR_SLOT
    assert decision.event_block_time_ms == ANCHOR_MS
    # No field on ScreenInputs admits event_time + H data.
    input_fields = set(vars(inp).keys())
    forward = {"resolution_slot", "future_price", "exit_price", "label", "next_slot"}
    assert input_fields.isdisjoint(forward)


def test_block_time_must_be_real():
    with pytest.raises(ValueError):
        ScreenInputs(
            mint="m",
            event_slot=1,
            event_block_time_ms=0,  # zero-init clock => reject
            data_staleness_ms=0,
            market_cap_usd=1.0,
            liquidity_usd=1.0,
            volume_usd_h24=1.0,
        )


# ---------------------------------------------------------------------------
# 7. Volume-spike is a CONTRARIAN-AWARE info tag, never a buy/pass
# ---------------------------------------------------------------------------


def test_volume_spike_is_info_tag_not_exclusion():
    screener = DiscoveryScreener()
    # In-band, with a 4x volume spike vs baseline (>3x default multiplier).
    decision = screener.screen(
        _inputs(volume_usd_h24=4_000_000.0, baseline_volume_usd_h24=1_000_000.0)
    )
    assert decision.has_volume_spike is True
    # The spike does NOT exclude (it is INFO only).
    assert decision.passed is True
    # And it is NOT an exclusion code.
    assert ScreenReason.VOLUME_SPIKE.value not in decision.exclusion_codes
    assert ScreenReason.VOLUME_SPIKE.value in decision.reason_codes


def test_volume_spike_does_not_rescue_out_of_band_candidate():
    """A spike never rescues / passes an otherwise-excluded candidate (no standalone buy)."""
    screener = DiscoveryScreener()
    # Above the mcap band (already-run) BUT with a huge volume spike.
    decision = screener.screen(
        _inputs(
            market_cap_usd=50_000_000.0,
            volume_usd_h24=40_000_000.0,
            baseline_volume_usd_h24=1_000_000.0,
        )
    )
    assert decision.has_volume_spike is True
    # Still EXCLUDED — the spike cannot turn a reject into a pass.
    assert decision.excluded is True
    assert ScreenReason.MCAP_ABOVE_BAND.value in decision.exclusion_codes


def test_no_baseline_means_no_spike_tag():
    decision = DiscoveryScreener().screen(_inputs(baseline_volume_usd_h24=None))
    assert decision.has_volume_spike is False


# ---------------------------------------------------------------------------
# 8. Money: exact Decimal comparison, no float drift
# ---------------------------------------------------------------------------


def test_exact_boundary_mcap_not_excluded():
    """A market cap exactly at the band edge is IN-band (>= min, <= max)."""
    screener = DiscoveryScreener(
        thresholds=ScreenerThresholds(mcap_min_usd=70_000, mcap_max_usd=11_000_000)
    )
    at_min = screener.screen(_inputs(market_cap_usd=70_000.0))
    at_max = screener.screen(_inputs(market_cap_usd=11_000_000.0, volume_usd_h24=2_000_000.0))
    assert at_min.passed is True
    assert at_max.passed is True


def test_turnover_ratio_exact_no_float_misclassification():
    """0.1 turnover with a Decimal floor of 0.10 must PASS (binary-float 0.1 would drift)."""
    screener = DiscoveryScreener(thresholds=ScreenerThresholds(min_volume_to_mcap_ratio="0.10"))
    # 1_000_000 / 10_000_000 == exactly 0.1
    decision = screener.screen(_inputs(market_cap_usd=10_000_000.0, volume_usd_h24=1_000_000.0))
    assert decision.passed is True
    assert ScreenReason.VOLUME_TO_MCAP_BELOW_MIN.value not in decision.exclusion_codes


# ---------------------------------------------------------------------------
# 9. Build inputs from a real T-301 EnrichmentBundle (offline fixtures)
# ---------------------------------------------------------------------------


def _recent_anchor_ms() -> int:
    """A near-now on-chain anchor so the offline bundle's recorded staleness is small.

    The offline fixtures compute data_staleness_ms = ingest_time(now) - anchor.  With the
    fixed 2024 ANCHOR_MS that staleness is ~years (correctly => INPUT_STALE).  For the
    band-logic assertions we anchor near now so staleness stays inside the budget and the
    market-cap band check is exercised on its own.
    """
    import time

    return int(time.time() * 1_000)


def test_from_enrichment_bundle_offline():
    client = build_offline_enrichment_client()
    anchor = _recent_anchor_ms()
    bundle = asyncio.run(
        client.enrich(
            mint="FixtureMint1111111111111111111111111111111111",
            request_slot=ANCHOR_SLOT,
            event_time_block_time_ms=anchor,
        )
    )
    inp = ScreenInputs.from_enrichment_bundle(bundle)
    # Fixture: birdeye mc=42000, liquidity=32000, volume=85000.
    assert inp.market_cap_usd == 42000.0
    assert inp.liquidity_usd == 32000.0
    assert inp.volume_usd_h24 == 85000.0
    assert inp.event_slot == ANCHOR_SLOT
    assert inp.event_block_time_ms == anchor
    assert inp.data_staleness_ms < 60_000  # within budget — band check is exercised

    # 42K mcap is BELOW the default 70K band => excluded on the band, not staleness.
    decision = DiscoveryScreener().screen(inp)
    assert decision.excluded is True
    assert ScreenReason.MCAP_BELOW_BAND.value in decision.exclusion_codes
    assert ScreenReason.INPUT_STALE.value not in decision.exclusion_codes


def test_screen_bundle_convenience_excludes_too_early_token():
    client = build_offline_enrichment_client()
    bundle = asyncio.run(
        client.enrich(
            mint="FixtureMint1111111111111111111111111111111111",
            request_slot=ANCHOR_SLOT,
            event_time_block_time_ms=_recent_anchor_ms(),
        )
    )
    decision = DiscoveryScreener().screen_bundle(bundle)
    assert isinstance(decision, ScreenDecision)
    assert decision.excluded is True


def test_from_bundle_empty_sources_refuses_by_default():
    """A bundle with no enrichment data (max_staleness_ms == -1) is treated as stale."""
    bundle = EnrichmentBundle(
        mint="MintNoData",
        request_slot=ANCHOR_SLOT,
        event_time_block_time_ms=ANCHOR_MS,
    )
    assert bundle.max_staleness_ms() == -1
    decision = DiscoveryScreener().screen_bundle(bundle)
    assert decision.excluded is True


def test_from_bundle_uses_fdv_fallback_for_mcap():
    """When Birdeye mcap is absent, DEXScreener FDV is the documented mcap fallback."""
    meta = EnrichmentMeta(
        source="dexscreener",
        mint="M",
        request_slot=ANCHOR_SLOT,
        event_time_block_time_ms=ANCHOR_MS,
        ingest_time_ms=ANCHOR_MS + 500,
        data_staleness_ms=500,
    )
    dex = DEXScreenerResult(
        meta=meta,
        pool_address="P",
        base_token_symbol="T",
        quote_token_symbol="SOL",
        price_usd=0.001,
        price_usd_change_h1=1.0,
        volume_usd_h24=900_000.0,
        liquidity_usd=150_000.0,
        txns_h24_buys=10,
        txns_h24_sells=5,
        fdv_usd=5_000_000.0,  # FDV used as mcap proxy
        pair_created_at_ms=ANCHOR_MS,
        dex_id="raydium",
    )
    bundle = EnrichmentBundle(
        mint="M",
        request_slot=ANCHOR_SLOT,
        event_time_block_time_ms=ANCHOR_MS,
        dexscreener=dex,
        birdeye=None,
    )
    inp = ScreenInputs.from_enrichment_bundle(bundle)
    assert inp.market_cap_usd == 5_000_000.0
    assert inp.liquidity_usd == 150_000.0
    assert DiscoveryScreener().screen(inp).passed is True


# ---------------------------------------------------------------------------
# 10. Property / fuzz: tightening never flips REJECT->PASS; PASS is never a buy
# ---------------------------------------------------------------------------


def test_property_tightening_never_turns_reject_into_pass():
    rng = random.Random(8)
    base = ScreenerThresholds()  # defaults
    for _ in range(3000):
        mcap = rng.choice([None, 0.0, 1_000.0, 80_000.0, 5_000_000.0, 12_000_000.0, 9e9])
        liq = rng.choice([None, 0.0, 1_000.0, 25_000.0, 500_000.0])
        vol = rng.choice([None, 0.0, 5_000.0, 1_000_000.0, 9e9])
        stale = rng.choice([0, 1_000, 60_000, 999_999])
        inp = _inputs(
            market_cap_usd=mcap,
            liquidity_usd=liq,
            volume_usd_h24=vol,
            data_staleness_ms=stale,
        )
        loose_decision = DiscoveryScreener(thresholds=base).screen(inp)
        if loose_decision.excluded:
            # Tighten the band: a stricter screener must STILL exclude (selectivity is
            # monotone — tightening only ever rejects MORE, never rescues).
            tighter = ScreenerThresholds(
                mcap_min_usd=base.mcap_min_usd * 2,
                mcap_max_usd=base.mcap_max_usd // 2
                if base.mcap_max_usd // 2 >= base.mcap_min_usd * 2
                else base.mcap_min_usd * 2,
                min_liquidity_usd=base.min_liquidity_usd * 2,
                min_volume_to_mcap_ratio=min(base.min_volume_to_mcap_ratio * 2, Decimal(1)),
                max_staleness_ms=base.max_staleness_ms // 2,
            )
            assert DiscoveryScreener(thresholds=tighter).screen(inp).excluded is True


def test_property_pass_never_exposes_a_buy_field():
    rng = random.Random(80)
    for _ in range(2000):
        inp = _inputs(
            market_cap_usd=rng.uniform(70_000, 11_000_000),
            liquidity_usd=rng.uniform(20_000, 1_000_000),
            volume_usd_h24=rng.uniform(2_000_000, 5_000_000),
        )
        decision = DiscoveryScreener().screen(inp)
        # Whatever the verdict, the decision never carries an order/size/buy field.
        assert not hasattr(decision, "size_lamports")
        assert not hasattr(decision, "buy")
        assert decision.verdict in (ScreenVerdict.PASS, ScreenVerdict.REJECT)
