"""T-323 — pre-trade safety gate functional tests + known honeypot/rug fixtures.

Proves:
  * Known honeypot / rug archetypes are REJECTED with the right red flag(s).
  * A clean token PASSes (gate is silent — it does not size/trigger anything).
  * Refuse-by-default: unknown/None inputs and stale data REJECT, never pass.
  * Ordered short-circuit matches safety.py GATE_ORDER.
  * Reject-only: the gate constructs no Intent / sizes nothing / sets no stop.
  * Red-flag codes are exported (the dashboard token-safety scanner contract).
  * Money is int/Decimal — float thresholds/inputs are refused at construction.
"""

from __future__ import annotations

import pytest

from aats.risk.pretrade_gate import (
    HOT_CHECK_ORDER,
    GateVerdict,
    PreTradeSafetyGate,
    RedFlag,
    SafetyInputs,
    SafetyThresholds,
)

MINT = "So1111111111111111111111111111111111111111"
SLOT = 1_000
BTIME = 1_700_000_000_000


def _clean_inputs(**overrides) -> SafetyInputs:
    """A point-in-time-clean candidate: all authorities renounced, LP burned,
    no cluster, low concentration, zero taxes, fresh data."""
    base = {
        "mint": MINT,
        "event_slot": SLOT,
        "event_block_time_ms": BTIME,
        "data_staleness_ms": 50,
        "freeze_authority": None,  # renounced
        "mint_authority": None,  # renounced
        "lp_locked_or_burned_bps": 10_000,  # 100% burned
        "dev_bundle_cluster_bps": 500,  # 5%
        "holder_concentration_top10_bps": 2_000,  # 20%
        "sell_tax_bps": 0,
        "buy_tax_bps": 0,
        "authorities_decoded": True,
    }
    base.update(overrides)
    return SafetyInputs(**base)


# ---------------------------------------------------------------------------
# Clean token PASSes (and the pass is SILENT — see reject-only test below).
# ---------------------------------------------------------------------------


def test_clean_token_passes():
    gate = PreTradeSafetyGate()
    dec = gate.evaluate_fast(_clean_inputs())
    assert dec.passed
    assert dec.verdict is GateVerdict.PASS
    assert dec.red_flags == ()
    assert dec.red_flag_codes == ()


# ---------------------------------------------------------------------------
# Known honeypot / rug fixtures — each MUST be rejected with the right flag.
# ---------------------------------------------------------------------------

# Each fixture: (name, override-kwargs, expected first short-circuit flag).
_HONEYPOT_FIXTURES = [
    (
        "freeze_authority_honeypot",  # deployer can freeze => cannot sell
        {"freeze_authority": "Fr33ze1111111111111111111111111111111111111"},
        RedFlag.FREEZE_AUTHORITY_SET,
    ),
    (
        "mint_authority_infinite_supply",  # deployer can mint infinite => rug
        {"mint_authority": "M1nt11111111111111111111111111111111111111"},
        RedFlag.MINT_AUTHORITY_NOT_RENOUNCED,
    ),
    (
        "unlocked_lp_pullable",  # LP not locked => instant rug pull
        {"lp_locked_or_burned_bps": 1_000},  # 10% locked, 90% pullable
        RedFlag.LP_NOT_LOCKED_OR_BURNED,
    ),
    (
        "dev_bundle_cluster_dump",  # one cluster holds 60% => dump risk
        {"dev_bundle_cluster_bps": 6_000},
        RedFlag.DEV_OR_BUNDLE_CLUSTER,
    ),
    (
        "top10_concentration_dump",  # top-10 hold 90%
        {"holder_concentration_top10_bps": 9_000},
        RedFlag.HOLDER_CONCENTRATION_HIGH,
    ),
    (
        "sell_tax_honeypot",  # 99% sell tax => you cannot extract value
        {"sell_tax_bps": 9_900},
        RedFlag.SELL_TAX_TOO_HIGH,
    ),
    (
        "buy_tax_trap",  # 50% buy tax
        {"buy_tax_bps": 5_000},
        RedFlag.BUY_TAX_TOO_HIGH,
    ),
]


@pytest.mark.parametrize(
    "name,override,expected", _HONEYPOT_FIXTURES, ids=[f[0] for f in _HONEYPOT_FIXTURES]
)
def test_known_honeypot_rug_rejected(name, override, expected):
    gate = PreTradeSafetyGate()
    dec = gate.evaluate_fast(_clean_inputs(**override))
    assert not dec.passed, f"{name} should be REJECTED"
    assert dec.verdict is GateVerdict.REJECT
    # The first short-circuit flag is the expected one (ordered evaluation).
    assert dec.red_flags[0].flag is expected
    assert expected.value in dec.red_flag_codes


def test_scan_all_surfaces_every_red_flag():
    """The scanner (NOT block-0) reports EVERY tripped flag for the dashboard."""
    gate = PreTradeSafetyGate()
    # A token that trips multiple flags at once.
    bad = _clean_inputs(
        lp_locked_or_burned_bps=0,
        dev_bundle_cluster_bps=9_000,
        holder_concentration_top10_bps=9_500,
        sell_tax_bps=5_000,
        buy_tax_bps=5_000,
    )
    dec = gate.scan_all(bad)
    assert not dec.passed
    codes = set(dec.red_flag_codes)
    assert RedFlag.LP_NOT_LOCKED_OR_BURNED.value in codes
    assert RedFlag.DEV_OR_BUNDLE_CLUSTER.value in codes
    assert RedFlag.HOLDER_CONCENTRATION_HIGH.value in codes
    assert RedFlag.SELL_TAX_TOO_HIGH.value in codes
    assert RedFlag.BUY_TAX_TOO_HIGH.value in codes
    # evaluate_fast short-circuits to ONLY the first flag (latency); scan_all does not.
    fast = gate.evaluate_fast(bad)
    assert len(fast.red_flags) == 1


# ---------------------------------------------------------------------------
# Refuse-by-default — unknown / None / stale inputs REJECT, never pass.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "field",
    [
        "lp_locked_or_burned_bps",
        "dev_bundle_cluster_bps",
        "holder_concentration_top10_bps",
        "sell_tax_bps",
        "buy_tax_bps",
    ],
)
def test_unknown_input_refuses_by_default(field):
    """A None (un-decodable in budget) input REJECTS — never read as 'safe'."""
    gate = PreTradeSafetyGate()
    dec = gate.evaluate_fast(_clean_inputs(**{field: None}))
    assert not dec.passed
    assert dec.red_flags[0].flag is RedFlag.INPUT_INCOMPLETE


def test_undecoded_authorities_refuse_by_default():
    gate = PreTradeSafetyGate()
    dec = gate.evaluate_fast(_clean_inputs(authorities_decoded=False))
    assert not dec.passed
    assert dec.red_flags[0].flag is RedFlag.INPUT_INCOMPLETE


def test_stale_input_refuses_by_default():
    """Over-budget point-in-time staleness REJECTS before any token check runs."""
    gate = PreTradeSafetyGate(thresholds=SafetyThresholds(max_staleness_ms=1_000))
    dec = gate.evaluate_fast(_clean_inputs(data_staleness_ms=5_000))
    assert not dec.passed
    assert dec.red_flags[0].flag is RedFlag.INPUT_STALE
    # And the staleness check fires FIRST (before any token check) — refuse-by-default.
    assert dec.red_flags[0].measured == 5_000


# ---------------------------------------------------------------------------
# Boundary correctness — threshold edges are exact (int bps).
# ---------------------------------------------------------------------------


def test_threshold_boundaries_are_exact():
    th = SafetyThresholds()
    gate = PreTradeSafetyGate(thresholds=th)
    # Exactly at the LP floor passes; one bps below rejects.
    assert gate.evaluate_fast(_clean_inputs(lp_locked_or_burned_bps=th.min_lp_locked_bps)).passed
    assert not gate.evaluate_fast(
        _clean_inputs(lp_locked_or_burned_bps=th.min_lp_locked_bps - 1)
    ).passed
    # Exactly at the sell-tax cap passes; one bps above rejects.
    assert gate.evaluate_fast(_clean_inputs(sell_tax_bps=th.max_sell_tax_bps)).passed
    assert not gate.evaluate_fast(_clean_inputs(sell_tax_bps=th.max_sell_tax_bps + 1)).passed
    # Concentration exactly at cap passes; above rejects.
    assert gate.evaluate_fast(
        _clean_inputs(holder_concentration_top10_bps=th.max_holder_concentration_top10_bps)
    ).passed
    assert not gate.evaluate_fast(
        _clean_inputs(holder_concentration_top10_bps=th.max_holder_concentration_top10_bps + 1)
    ).passed


# ---------------------------------------------------------------------------
# Ordering matches safety.py GATE_ORDER (promoted).
# ---------------------------------------------------------------------------


def test_hot_check_order_matches_safety_py_sequence():
    """The first three hot checks match the sim's GATE_ORDER prefix exactly:
    freeze -> mint -> LP -> (dev/bundle, concentration) -> taxes.  sell-sim is NOT here."""
    order = list(HOT_CHECK_ORDER)
    assert order[0] is RedFlag.FREEZE_AUTHORITY_SET
    assert order[1] is RedFlag.MINT_AUTHORITY_NOT_RENOUNCED
    assert order[2] is RedFlag.LP_NOT_LOCKED_OR_BURNED
    assert RedFlag.DEV_OR_BUNDLE_CLUSTER in order
    assert RedFlag.SELL_TAX_TOO_HIGH in order
    # sellability sim (NOT_SELLABLE) is OFF the hot path — not in HOT_CHECK_ORDER.
    assert RedFlag.NOT_SELLABLE not in order


def test_freeze_authority_short_circuits_first():
    """When MANY flags would trip, evaluate_fast returns ONLY freeze (cheapest+first)."""
    gate = PreTradeSafetyGate()
    everything_wrong = _clean_inputs(
        freeze_authority="Fr33ze",
        mint_authority="M1nt",
        lp_locked_or_burned_bps=0,
        dev_bundle_cluster_bps=9_999,
        sell_tax_bps=9_999,
        buy_tax_bps=9_999,
    )
    dec = gate.evaluate_fast(everything_wrong)
    assert len(dec.red_flags) == 1
    assert dec.red_flags[0].flag is RedFlag.FREEZE_AUTHORITY_SET


# ---------------------------------------------------------------------------
# Money discipline — float is refused at construction (data-models.md §0).
# ---------------------------------------------------------------------------


def test_float_threshold_refused():
    with pytest.raises(TypeError):
        SafetyThresholds(max_sell_tax_bps=10.0)  # type: ignore[arg-type]


def test_float_input_refused():
    with pytest.raises(TypeError):
        _clean_inputs(sell_tax_bps=10.0)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        _clean_inputs(data_staleness_ms=50.0)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Decision shape — REJECT-ONLY; no sizing/forcing surface exists.
# ---------------------------------------------------------------------------


def test_decision_is_reject_or_pass_only():
    """The GateDecision has NO field that sizes a trade, sets a stop, or instructs
    execution.  Its only verdicts are PASS and REJECT (a veto report)."""
    dec = PreTradeSafetyGate().evaluate_fast(_clean_inputs())
    assert set(GateVerdict) == {GateVerdict.PASS, GateVerdict.REJECT}
    fields = set(vars(dec).keys())
    forbidden = {
        "size",
        "sol_in_lamports",
        "tip_lamports",
        "stop_price",
        "leverage",
        "intent",
        "entry",
        "amount",
    }
    assert fields.isdisjoint(forbidden), f"GateDecision exposes a sizing/forcing field: {fields}"


def test_gate_constructs_no_intent_and_holds_no_entry_path():
    """Structural reject-only: the gate has no method/attr that BUILDS an Intent,
    SIZES a trade, BUYS, WIDENS a stop, or INCREASES exposure.

    The only 'entry'-named surface is ``require_enabled_for_entry`` — a read-only,
    fail-closed eligibility predicate (it returns a bool; it cannot size/trigger).
    It is explicitly allowed: it can only ever WITHHOLD a green-light, never grant
    risk.  Everything else matching a risk-increasing verb is forbidden.
    """
    gate = PreTradeSafetyGate()
    _ALLOWED_DE_RISK = {"require_enabled_for_entry"}
    public = [a for a in dir(gate) if not a.startswith("_") and a not in _ALLOWED_DE_RISK]
    # Risk-INCREASING verbs: a surface that constructs/sizes/grows a position.
    for forbidden in (
        "entry",
        "size",
        "buy",
        "widen",
        "increase",
        "build_intent",
        "open_position",
        "leverage",
    ):
        assert not any(
            forbidden in a.lower() for a in public
        ), f"gate exposes a risk-increasing surface matching {forbidden!r}: {public}"
    # And the one allowed 'entry' surface is a pure bool predicate (de-risk only):
    assert gate.require_enabled_for_entry(gate.evaluate_fast(_clean_inputs())) in (True, False)
