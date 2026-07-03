"""T-323 — AC-011 honeypot/rug fixture-corpus rejection-rate test.

AC-011 (US-006, traceability AC->FR-026/FR-037):

    Given a fixture set of 10 known-honeypot tokens and 10 known-rug tokens with
    at least one detectable on-chain red flag each (freeze authority set,
    un-renounced mint authority, unburned LP, dev-cluster signature, high sell
    tax), WHEN the pre-trade safety gate runs on each, THEN ALL 20 MUST be
    rejected (100% rejection rate on the fixture set). The specific rejection
    reason (gate step 1-6) MUST be logged for each.

The directive's explicit instruction is "MEASURE the rejection rate". This file
MEASURES it: it runs ``evaluate_fast`` over a 20-token corpus, computes
``rejected_count``, asserts ``rejected_count == 20`` (== 100% on the fixture set),
and asserts EACH reject carries a non-empty, logged ``red_flag_codes`` reason that
maps to one of the AC-011-named on-chain red flags (gate step 1-6).

The corpus is 20 DISTINCT mints:
  * 10 honeypots — exit-trap archetypes (you can buy but cannot extract value):
    freeze authority, un-renounced mint authority, punitive sell tax, punitive
    buy tax, and realistic exit-trap composites of those.
  * 10 rugs — pull/dump archetypes (the deployer drains liquidity / dumps supply):
    unlocked-and-pullable LP, dev/bundle cluster, top-holder concentration, and
    realistic composites (unlocked-LP + dev-cluster, mint-authority + concentration,
    cluster + concentration, etc.).

No change to ``aats/risk/pretrade_gate.py`` is required: the gate already rejects
this corpus at 20/20. This file is the missing COVERAGE that proves it.

Money/threshold discipline: every fixture quantity is an int (bps), never float
(data-models.md §0). This is the FAST/SNIPE path: zero LLM, zero RPC, point-in-time
inputs only (no lookahead — see the staleness/event-time fields).
"""

from __future__ import annotations

import pytest

from aats.risk.pretrade_gate import (
    GateVerdict,
    PreTradeSafetyGate,
    RedFlag,
    SafetyInputs,
)

# A renounced/clean baseline. Each fixture below mutates ONLY the field(s) that
# make it a known honeypot or rug, so every rejection is attributable to a
# specific on-chain red flag (gate step 1-6) — never to an incidental default.
_CLEAN_BASE = {
    "event_slot": 1_000,
    "event_block_time_ms": 1_700_000_000_000,
    "data_staleness_ms": 50,  # fresh, in-budget point-in-time data
    "freeze_authority": None,  # renounced
    "mint_authority": None,  # renounced
    "lp_locked_or_burned_bps": 10_000,  # 100% burned
    "dev_bundle_cluster_bps": 500,  # 5%
    "holder_concentration_top10_bps": 2_000,  # 20%
    "holder_count": 50,  # E18: above the distinct-holder floor (clean baseline)
    "sell_tax_bps": 0,
    "buy_tax_bps": 0,
    "authorities_decoded": True,
}

# Distinct, deterministic 44-char-ish mint strings (base58-shaped; never a real key).
_MINTS = [f"AC011Honeypot{i:02d}{'x' * 30}"[:44] for i in range(1, 11)] + [
    f"AC011RugPull0{i:02d}{'x' * 30}"[:44] for i in range(1, 11)
]


def _mk(mint: str, **overrides) -> SafetyInputs:
    base = dict(_CLEAN_BASE)
    base["mint"] = mint
    base.update(overrides)
    return SafetyInputs(**base)


# ---------------------------------------------------------------------------
# 10 KNOWN HONEYPOTS — exit-trap archetypes (buy ok, cannot extract value).
# Each carries >=1 detectable on-chain red flag named in AC-011.
# ---------------------------------------------------------------------------

_HONEYPOTS: list[tuple[str, SafetyInputs, set[RedFlag]]] = [
    # 1. Freeze authority set — deployer can freeze your tokens (cannot sell).
    (
        "honeypot_freeze_authority",
        _mk(_MINTS[0], freeze_authority="Fr33zeAuth11111111111111111111111111111111"),
        {RedFlag.FREEZE_AUTHORITY_SET},
    ),
    # 2. Un-renounced mint authority — infinite-supply exit trap.
    (
        "honeypot_unrenounced_mint_authority",
        _mk(_MINTS[1], mint_authority="M1ntAuth111111111111111111111111111111111111"),
        {RedFlag.MINT_AUTHORITY_NOT_RENOUNCED},
    ),
    # 3. Punitive 99% sell tax — you cannot extract value on exit.
    (
        "honeypot_99pct_sell_tax",
        _mk(_MINTS[2], sell_tax_bps=9_900),
        {RedFlag.SELL_TAX_TOO_HIGH},
    ),
    # 4. Punitive 60% buy tax — value siphoned on entry.
    (
        "honeypot_60pct_buy_tax",
        _mk(_MINTS[3], buy_tax_bps=6_000),
        {RedFlag.BUY_TAX_TOO_HIGH},
    ),
    # 5. Sell tax just over the cap — a quieter exit trap (boundary-realistic).
    (
        "honeypot_sell_tax_over_cap",
        _mk(_MINTS[4], sell_tax_bps=1_500),
        {RedFlag.SELL_TAX_TOO_HIGH},
    ),
    # 6. Composite: freeze authority + high sell tax (belt-and-braces honeypot).
    (
        "honeypot_freeze_plus_sell_tax",
        _mk(
            _MINTS[5],
            freeze_authority="Fr33zeAuth55555555555555555555555555555555",
            sell_tax_bps=8_000,
        ),
        {RedFlag.FREEZE_AUTHORITY_SET, RedFlag.SELL_TAX_TOO_HIGH},
    ),
    # 7. Composite: un-renounced mint + punitive sell tax (mint-and-trap).
    (
        "honeypot_mint_auth_plus_sell_tax",
        _mk(
            _MINTS[6],
            mint_authority="M1ntAuth666666666666666666666666666666666666",
            sell_tax_bps=7_500,
        ),
        {RedFlag.MINT_AUTHORITY_NOT_RENOUNCED, RedFlag.SELL_TAX_TOO_HIGH},
    ),
    # 8. Composite: high buy AND high sell tax (double-sided skim).
    (
        "honeypot_buy_and_sell_tax",
        _mk(_MINTS[7], buy_tax_bps=4_000, sell_tax_bps=4_000),
        {RedFlag.BUY_TAX_TOO_HIGH, RedFlag.SELL_TAX_TOO_HIGH},
    ),
    # 9. Composite: freeze authority + un-renounced mint (full deployer control).
    (
        "honeypot_freeze_plus_mint_auth",
        _mk(
            _MINTS[8],
            freeze_authority="Fr33zeAuth99999999999999999999999999999999",
            mint_authority="M1ntAuth999999999999999999999999999999999999",
        ),
        {RedFlag.FREEZE_AUTHORITY_SET, RedFlag.MINT_AUTHORITY_NOT_RENOUNCED},
    ),
    # 10. Freeze authority + punitive buy tax (entry siphon + exit freeze).
    (
        "honeypot_freeze_plus_buy_tax",
        _mk(
            _MINTS[9],
            freeze_authority="Fr33zeAuthAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
            buy_tax_bps=5_000,
        ),
        {RedFlag.FREEZE_AUTHORITY_SET, RedFlag.BUY_TAX_TOO_HIGH},
    ),
]


# ---------------------------------------------------------------------------
# 10 KNOWN RUGS — pull/dump archetypes (deployer drains LP / dumps supply).
# Realistic composites per the directive (unlocked-LP+dev-cluster,
# mint-authority+concentration, cluster+concentration, ...).
# ---------------------------------------------------------------------------

_RUGS: list[tuple[str, SafetyInputs, set[RedFlag]]] = [
    # 11. Fully-unlocked, pullable LP — instant rug pull.
    (
        "rug_unlocked_lp_pullable",
        _mk(_MINTS[10], lp_locked_or_burned_bps=0),
        {RedFlag.LP_NOT_LOCKED_OR_BURNED},
    ),
    # 12. Mostly-unlocked LP (10% locked, 90% pullable).
    (
        "rug_mostly_unlocked_lp",
        _mk(_MINTS[11], lp_locked_or_burned_bps=1_000),
        {RedFlag.LP_NOT_LOCKED_OR_BURNED},
    ),
    # 13. Dev/bundle cluster holds 60% — coordinated dump risk.
    (
        "rug_dev_bundle_cluster",
        _mk(_MINTS[12], dev_bundle_cluster_bps=6_000),
        {RedFlag.DEV_OR_BUNDLE_CLUSTER},
    ),
    # 14. Top-10 holders hold 90% — concentration dump risk.
    (
        "rug_top10_concentration",
        _mk(_MINTS[13], holder_concentration_top10_bps=9_000),
        {RedFlag.HOLDER_CONCENTRATION_HIGH},
    ),
    # 15. Composite: unlocked LP + dev-cluster (classic coordinated rug).
    (
        "rug_unlocked_lp_plus_dev_cluster",
        _mk(
            _MINTS[14],
            lp_locked_or_burned_bps=2_000,
            dev_bundle_cluster_bps=5_500,
        ),
        {RedFlag.LP_NOT_LOCKED_OR_BURNED, RedFlag.DEV_OR_BUNDLE_CLUSTER},
    ),
    # 16. Composite: un-renounced mint authority + top-holder concentration.
    (
        "rug_mint_authority_plus_concentration",
        _mk(
            _MINTS[15],
            mint_authority="M1ntAuthFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF",
            holder_concentration_top10_bps=8_000,
        ),
        {RedFlag.MINT_AUTHORITY_NOT_RENOUNCED, RedFlag.HOLDER_CONCENTRATION_HIGH},
    ),
    # 17. Composite: dev-cluster + top-holder concentration (two dump vectors).
    (
        "rug_cluster_plus_concentration",
        _mk(
            _MINTS[16],
            dev_bundle_cluster_bps=4_500,
            holder_concentration_top10_bps=7_500,
        ),
        {RedFlag.DEV_OR_BUNDLE_CLUSTER, RedFlag.HOLDER_CONCENTRATION_HIGH},
    ),
    # 18. Composite: unlocked LP + concentration (pull + supply dump).
    (
        "rug_unlocked_lp_plus_concentration",
        _mk(
            _MINTS[17],
            lp_locked_or_burned_bps=500,
            holder_concentration_top10_bps=8_500,
        ),
        {RedFlag.LP_NOT_LOCKED_OR_BURNED, RedFlag.HOLDER_CONCENTRATION_HIGH},
    ),
    # 19. Composite: unlocked LP + dev-cluster + concentration (full rug stack).
    (
        "rug_full_stack",
        _mk(
            _MINTS[18],
            lp_locked_or_burned_bps=0,
            dev_bundle_cluster_bps=7_000,
            holder_concentration_top10_bps=9_500,
        ),
        {
            RedFlag.LP_NOT_LOCKED_OR_BURNED,
            RedFlag.DEV_OR_BUNDLE_CLUSTER,
            RedFlag.HOLDER_CONCENTRATION_HIGH,
        },
    ),
    # 20. Composite: mint authority + unlocked LP (mint-and-pull rug).
    (
        "rug_mint_authority_plus_unlocked_lp",
        _mk(
            _MINTS[19],
            mint_authority="M1ntAuthGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGG",
            lp_locked_or_burned_bps=1_500,
        ),
        {RedFlag.MINT_AUTHORITY_NOT_RENOUNCED, RedFlag.LP_NOT_LOCKED_OR_BURNED},
    ),
]

# The full AC-011 corpus: 10 honeypots + 10 rugs == 20 distinct tokens.
_AC011_CORPUS: list[tuple[str, SafetyInputs, set[RedFlag]]] = _HONEYPOTS + _RUGS

# The AC-011-named, detectable on-chain red flags (gate step 1-6). Every reject
# in the corpus must carry at least one of these (never an INPUT_* refuse code:
# these are all decoded, in-budget, point-in-time-clean inputs — the rejection is
# the token's own on-chain red flag, not a missing-data refusal).
_ONCHAIN_RED_FLAGS = {
    RedFlag.FREEZE_AUTHORITY_SET,
    RedFlag.MINT_AUTHORITY_NOT_RENOUNCED,
    RedFlag.LP_NOT_LOCKED_OR_BURNED,
    RedFlag.DEV_OR_BUNDLE_CLUSTER,
    RedFlag.HOLDER_CONCENTRATION_HIGH,
    RedFlag.SELL_TAX_TOO_HIGH,
    RedFlag.BUY_TAX_TOO_HIGH,
}


class _RecordingTelemetry:
    """Captures the per-token rejection reason the gate LOGS (AC-011 requires the
    specific rejection reason be logged for EACH token)."""

    def __init__(self) -> None:
        self.rejects: dict[str, tuple[str, ...]] = {}
        self.passes: list[str] = []

    def on_gate_reject(self, mint: str, flags: tuple[str, ...]) -> None:
        self.rejects[mint] = flags

    def on_gate_pass(self, mint: str) -> None:
        self.passes.append(mint)


# ---------------------------------------------------------------------------
# Corpus sanity — the fixture set is well-formed (20 DISTINCT tokens, 10 + 10).
# ---------------------------------------------------------------------------


def test_corpus_is_20_distinct_tokens_10_honeypot_10_rug():
    assert len(_HONEYPOTS) == 10, "AC-011 requires exactly 10 known-honeypot fixtures"
    assert len(_RUGS) == 10, "AC-011 requires exactly 10 known-rug fixtures"
    assert len(_AC011_CORPUS) == 20
    mints = [inp.mint for _, inp, _ in _AC011_CORPUS]
    assert len(set(mints)) == 20, "all 20 fixtures must be DISTINCT mints"


# ---------------------------------------------------------------------------
# THE AC-011 PROOF — measure the rejection rate; it must be 100% (20/20).
# ---------------------------------------------------------------------------


def test_ac011_corpus_rejection_rate_is_100_percent():
    """MEASURE the rejection rate over the 20-token corpus and assert it is 100%.

    For each token: run the FAST hot-path gate, record whether it rejected, and
    record the specific logged rejection reason. Then assert:
      * rejected_count == 20  (100% rejection rate on the fixture set)
      * every reject carries a non-empty, logged red-flag reason
      * every reason is one of the AC-011 on-chain red flags (gate step 1-6)
      * zero tokens passed
    """
    telemetry = _RecordingTelemetry()
    gate = PreTradeSafetyGate(telemetry=telemetry)

    rejected_count = 0
    per_token_reason: dict[str, tuple[str, ...]] = {}

    for name, inputs, expected_flags in _AC011_CORPUS:
        decision = gate.evaluate_fast(inputs)

        # This token must be rejected.
        assert decision.verdict is GateVerdict.REJECT, f"{name} ({inputs.mint}) was NOT rejected"
        assert not decision.passed, f"{name} unexpectedly passed"
        if decision.verdict is GateVerdict.REJECT:
            rejected_count += 1

        # The specific rejection reason MUST be logged for this token (AC-011).
        codes = decision.red_flag_codes
        assert codes, f"{name}: reject carried NO logged red_flag_codes reason"
        per_token_reason[inputs.mint] = codes

        # The logged reason must be a real on-chain red flag (gate step 1-6),
        # not a refuse-by-default INPUT_* code — these inputs are decoded & fresh.
        tripped = {hit.flag for hit in decision.red_flags}
        assert (
            tripped & _ONCHAIN_RED_FLAGS
        ), f"{name}: rejection reason {tripped} is not an AC-011 on-chain red flag"
        # The first short-circuit flag must be one we deliberately planted.
        assert decision.red_flags[0].flag in expected_flags, (
            f"{name}: short-circuited on {decision.red_flags[0].flag}, "
            f"expected one of {expected_flags}"
        )

    # === THE MEASURED REJECTION RATE ===
    rejection_rate = rejected_count / len(_AC011_CORPUS)
    assert rejected_count == 20, f"expected 20/20 rejected, got {rejected_count}/20"
    assert rejection_rate == 1.0, f"AC-011 requires 100% rejection rate, measured {rejection_rate}"

    # Per-token logged reason exists for all 20 (the telemetry sink saw each one).
    assert len(per_token_reason) == 20
    assert len(telemetry.rejects) == 20, "gate must LOG a reject reason for each of the 20 tokens"
    assert telemetry.passes == [], "no corpus token may pass"
    for mint, codes in telemetry.rejects.items():
        assert codes, f"logged reject for {mint} had an empty reason"


@pytest.mark.parametrize(
    "name,inputs,expected_flags",
    _AC011_CORPUS,
    ids=[c[0] for c in _AC011_CORPUS],
)
def test_ac011_each_token_rejected_with_logged_reason(name, inputs, expected_flags):
    """Per-token assertion (one parametrized case per fixture) so a single
    regressing token is named in the failure output, not just the aggregate."""
    telemetry = _RecordingTelemetry()
    gate = PreTradeSafetyGate(telemetry=telemetry)

    decision = gate.evaluate_fast(inputs)

    assert decision.verdict is GateVerdict.REJECT, f"{name} must be rejected"
    assert not decision.passed
    # Non-empty logged reason (AC-011: specific rejection reason logged per token).
    assert decision.red_flag_codes, f"{name}: no logged red_flag_codes"
    assert inputs.mint in telemetry.rejects, f"{name}: gate did not LOG the reject"
    assert telemetry.rejects[inputs.mint], f"{name}: logged reject reason was empty"
    # Reason is a planted on-chain red flag (gate step 1-6), not a missing-data refusal.
    assert decision.red_flags[0].flag in expected_flags


def test_ac011_scan_all_surfaces_all_planted_flags_per_token():
    """For the dashboard scanner path: ``scan_all`` (no short-circuit) must surface
    EVERY planted on-chain red flag for each composite token — so the operator sees
    the full reason set, while ``evaluate_fast`` (latency path) shows only the first.
    """
    gate = PreTradeSafetyGate()
    for name, inputs, expected_flags in _AC011_CORPUS:
        decision = gate.scan_all(inputs)
        assert decision.verdict is GateVerdict.REJECT, f"{name} must reject under scan_all"
        surfaced = {hit.flag for hit in decision.red_flags}
        missing = expected_flags - surfaced
        assert not missing, f"{name}: scan_all did not surface planted flags {missing}"
