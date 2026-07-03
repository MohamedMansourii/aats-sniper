"""E16 (data-ingestion half) — dev-wallet first-funding-age RAW FEATURE tests.

Self-check items verified here (per the E16 directive):
  1. A wallet FIRST funded seconds-to-minutes before deploy is decoded FOUND
     with a small funding_age_ms/slots.
  2. An old, established wallet (funded long before deploy) is decoded FOUND
     with a large funding_age_ms/slots.
  3. STRICT point-in-time / no-lookahead: a funding-looking transaction with
     slot > deploy_event_slot is EXCLUDED from consideration entirely.
  4. Refuse-by-default: an RPC error while paging signatures -> UNDECODABLE.
  5. Refuse-by-default: no signature history at all before deploy ->
     UNDECODABLE (never treated as "brand new, therefore fresh").
  6. Refuse-by-default: the page budget is exhausted before genesis is
     reached -> UNDECODABLE (never guess the oldest-VISIBLE candidate is the
     wallet's true first funding).
  7. Resilience: a decode error on one candidate tx tries the next-oldest
     candidate rather than crashing or silently skipping to "undecodable".
  8. Event-time honesty: funding_age_ms/slots are pure functions of on-chain
     block_time/slot deltas, never wall-clock (two lookups at different
     wall-clock times over identical on-chain data yield identical ages).
  9. DevFundingAgeResult structural invariants: UNDECODABLE never carries a
     numeric age; FOUND never carries a slot/block_time after the deploy
     anchor; money field (lamports) rejects float/bool.
  10. Idempotent / deterministic: repeated lookups over identical data yield
      identical results.
"""

from __future__ import annotations

import time

import pytest

from aats.ingestion.dev_funding_age import (
    DevFundingAgeResult,
    FundingAgeStatus,
    HttpJsonRpcSolanaTransport,
    RawTransactionMeta,
    RpcFundingHistoryProvider,
    SignatureInfo,
    StaticSolanaHistoryTransport,
)

WALLET = "DevWallet1111111111111111111111111111111"
DEPLOY_SLOT = 300_500_000
DEPLOY_BLOCK_TIME_MS = 1_750_000_000_000  # arbitrary but realistic epoch-ms anchor


def _funding_tx(
    *,
    wallet: str,
    slot: int,
    block_time_ms: int,
    amount_lamports: int = 5_000_000_000,
) -> RawTransactionMeta:
    """A minimal decoded tx showing an inbound SOL balance increase for `wallet`."""
    return RawTransactionMeta(
        slot=slot,
        block_time_ms=block_time_ms,
        err=None,
        account_keys=("SponsorWalletXXXXXXXXXXXXXXXXXXXXXXXXXXXX", wallet),
        pre_balances_lamports=(10_000_000_000, 0),
        post_balances_lamports=(10_000_000_000 - amount_lamports, amount_lamports),
    )


def _register(
    transport: StaticSolanaHistoryTransport,
    *,
    wallet: str,
    signature: str,
    slot: int,
    block_time_ms: int,
    amount_lamports: int = 5_000_000_000,
    err: object | None = None,
) -> None:
    transport.add_signature(
        wallet, SignatureInfo(signature=signature, slot=slot, block_time_ms=block_time_ms, err=err)
    )
    if err is None:
        transport.add_transaction(
            signature,
            _funding_tx(
                wallet=wallet,
                slot=slot,
                block_time_ms=block_time_ms,
                amount_lamports=amount_lamports,
            ),
        )


# ---------------------------------------------------------------------------
# 1. Fresh-funded wallet -> FOUND, small age
# ---------------------------------------------------------------------------


def test_fresh_funded_wallet_found_with_small_age():
    transport = StaticSolanaHistoryTransport()
    funding_slot = DEPLOY_SLOT - 100  # ~40s of slots before deploy (~400ms/slot)
    funding_block_time_ms = DEPLOY_BLOCK_TIME_MS - 30_000  # 30 seconds before deploy
    _register(
        transport,
        wallet=WALLET,
        signature="sigFresh",
        slot=funding_slot,
        block_time_ms=funding_block_time_ms,
    )
    provider = RpcFundingHistoryProvider(transport)

    result = provider.lookup(
        WALLET, deploy_event_slot=DEPLOY_SLOT, deploy_block_time_ms=DEPLOY_BLOCK_TIME_MS
    )

    assert result.status is FundingAgeStatus.FOUND
    assert result.funding_age_ms == 30_000
    assert result.funding_age_slots == 100
    assert result.first_funding_signature == "sigFresh"
    assert result.first_funding_amount_lamports == 5_000_000_000
    assert provider.stats.found == 1


# ---------------------------------------------------------------------------
# 2. Old, established wallet -> FOUND, large age
# ---------------------------------------------------------------------------


def test_old_established_wallet_found_with_large_age():
    transport = StaticSolanaHistoryTransport()
    thirty_days_ms = 30 * 24 * 60 * 60 * 1_000
    funding_slot = DEPLOY_SLOT - 6_000_000  # far in the past
    funding_block_time_ms = DEPLOY_BLOCK_TIME_MS - thirty_days_ms
    _register(
        transport,
        wallet=WALLET,
        signature="sigOld",
        slot=funding_slot,
        block_time_ms=funding_block_time_ms,
    )
    provider = RpcFundingHistoryProvider(transport)

    result = provider.lookup(
        WALLET, deploy_event_slot=DEPLOY_SLOT, deploy_block_time_ms=DEPLOY_BLOCK_TIME_MS
    )

    assert result.status is FundingAgeStatus.FOUND
    assert result.funding_age_ms == thirty_days_ms
    assert result.funding_age_ms > 0


# ---------------------------------------------------------------------------
# 3. STRICT point-in-time / no-lookahead
# ---------------------------------------------------------------------------


def test_future_funding_tx_excluded_leak_test():
    """A funding-looking tx AFTER deploy_event_slot must never be visible to
    this lookup -- the load-bearing no-lookahead guarantee of the signal."""
    transport = StaticSolanaHistoryTransport()
    _register(
        transport,
        wallet=WALLET,
        signature="sigBefore",
        slot=DEPLOY_SLOT - 500,
        block_time_ms=DEPLOY_BLOCK_TIME_MS - 200_000,
    )
    _register(
        transport,
        wallet=WALLET,
        signature="sigAfter",
        slot=DEPLOY_SLOT + 10_000,  # strictly AFTER deploy — must be excluded
        block_time_ms=DEPLOY_BLOCK_TIME_MS + 5_000_000,
    )
    provider = RpcFundingHistoryProvider(transport)

    result = provider.lookup(
        WALLET, deploy_event_slot=DEPLOY_SLOT, deploy_block_time_ms=DEPLOY_BLOCK_TIME_MS
    )

    # The BEFORE-deploy funding must win — the after-deploy one never counts.
    assert result.status is FundingAgeStatus.FOUND
    assert result.first_funding_signature == "sigBefore"
    assert result.funding_age_ms == 200_000


def test_only_future_funding_present_refuses_never_leaks():
    """If the ONLY visible funding-looking tx is after deploy_event_slot, the
    lookup must refuse -- it must NEVER report the future tx's age (which
    would be a direct point-in-time leak)."""
    transport = StaticSolanaHistoryTransport()
    _register(
        transport,
        wallet=WALLET,
        signature="sigFutureOnly",
        slot=DEPLOY_SLOT + 1,
        block_time_ms=DEPLOY_BLOCK_TIME_MS + 1_000,
    )
    provider = RpcFundingHistoryProvider(transport)

    result = provider.lookup(
        WALLET, deploy_event_slot=DEPLOY_SLOT, deploy_block_time_ms=DEPLOY_BLOCK_TIME_MS
    )

    assert result.status is FundingAgeStatus.UNDECODABLE
    assert result.funding_age_ms is None
    assert result.funding_age_slots is None


# ---------------------------------------------------------------------------
# 4/5. Refuse-by-default: RPC error / no history at all
# ---------------------------------------------------------------------------


def test_rpc_error_on_signatures_page_refuses():
    transport = StaticSolanaHistoryTransport()
    transport.raise_on_signatures(WALLET)
    provider = RpcFundingHistoryProvider(transport)

    result = provider.lookup(
        WALLET, deploy_event_slot=DEPLOY_SLOT, deploy_block_time_ms=DEPLOY_BLOCK_TIME_MS
    )

    assert result.status is FundingAgeStatus.UNDECODABLE
    assert provider.stats.undecodable_rpc_error == 1


def test_no_history_at_all_refuses_never_treated_as_fresh():
    transport = StaticSolanaHistoryTransport()  # WALLET has zero registered signatures
    provider = RpcFundingHistoryProvider(transport)

    result = provider.lookup(
        WALLET, deploy_event_slot=DEPLOY_SLOT, deploy_block_time_ms=DEPLOY_BLOCK_TIME_MS
    )

    assert result.status is FundingAgeStatus.UNDECODABLE
    assert provider.stats.undecodable_no_history == 1


# ---------------------------------------------------------------------------
# 6. Refuse-by-default: page budget exhausted before genesis
# ---------------------------------------------------------------------------


def test_page_budget_exhausted_before_genesis_refuses():
    """A very active wallet whose ENTIRE pre-deploy history cannot be reached
    within the configured page budget must refuse -- never report the
    oldest-VISIBLE candidate as if it were proven to be the true first."""
    transport = StaticSolanaHistoryTransport()
    # Register more signatures than max_pages * page_size can reach.
    for i in range(50):
        _register(
            transport,
            wallet=WALLET,
            signature=f"sig{i:03d}",
            slot=DEPLOY_SLOT - i - 1,
            block_time_ms=DEPLOY_BLOCK_TIME_MS - (i + 1) * 1_000,
        )
    provider = RpcFundingHistoryProvider(transport, page_size=10, max_pages=2)  # only reaches 20

    result = provider.lookup(
        WALLET, deploy_event_slot=DEPLOY_SLOT, deploy_block_time_ms=DEPLOY_BLOCK_TIME_MS
    )

    assert result.status is FundingAgeStatus.UNDECODABLE
    assert provider.stats.undecodable_budget_exhausted == 1


def test_genesis_reached_within_budget_still_resolves():
    """Sanity counterpart: if genesis IS reached within the page budget, the
    lookup resolves normally (proves the cap isn't over-triggering)."""
    transport = StaticSolanaHistoryTransport()
    for i in range(15):
        _register(
            transport,
            wallet=WALLET,
            signature=f"sigG{i:03d}",
            slot=DEPLOY_SLOT - i - 1,
            block_time_ms=DEPLOY_BLOCK_TIME_MS - (i + 1) * 1_000,
        )
    provider = RpcFundingHistoryProvider(transport, page_size=10, max_pages=5)

    result = provider.lookup(
        WALLET, deploy_event_slot=DEPLOY_SLOT, deploy_block_time_ms=DEPLOY_BLOCK_TIME_MS
    )

    assert result.status is FundingAgeStatus.FOUND
    # The oldest of the 15 registered signatures (i=14) is the true first funding.
    assert result.first_funding_signature == "sigG014"
    assert result.funding_age_ms == 15_000


# ---------------------------------------------------------------------------
# 7. Resilience: one bad candidate tx tries the next-oldest candidate
# ---------------------------------------------------------------------------


def test_decode_error_on_oldest_candidate_falls_back_to_next():
    transport = StaticSolanaHistoryTransport()
    # Oldest signature registered but its getTransaction() call will raise.
    transport.add_signature(
        WALLET,
        SignatureInfo(
            signature="sigBroken",
            slot=DEPLOY_SLOT - 2_000,
            block_time_ms=DEPLOY_BLOCK_TIME_MS - 2_000_000,
            err=None,
        ),
    )
    transport.raise_on_transaction("sigBroken")
    # Next-oldest signature decodes fine.
    _register(
        transport,
        wallet=WALLET,
        signature="sigGood",
        slot=DEPLOY_SLOT - 1_000,
        block_time_ms=DEPLOY_BLOCK_TIME_MS - 1_000_000,
    )
    provider = RpcFundingHistoryProvider(transport)

    result = provider.lookup(
        WALLET, deploy_event_slot=DEPLOY_SLOT, deploy_block_time_ms=DEPLOY_BLOCK_TIME_MS
    )

    assert result.status is FundingAgeStatus.FOUND
    assert result.first_funding_signature == "sigGood"


def test_candidate_without_balance_increase_is_skipped():
    """A decoded candidate where the wallet's balance did NOT increase (e.g. it
    was the sender, not the recipient) must never be reported as funding."""
    transport = StaticSolanaHistoryTransport()
    outbound_slot = DEPLOY_SLOT - 2_000
    transport.add_signature(
        WALLET,
        SignatureInfo(
            signature="sigOutbound",
            slot=outbound_slot,
            block_time_ms=DEPLOY_BLOCK_TIME_MS - 2_000_000,
            err=None,
        ),
    )
    transport.add_transaction(
        "sigOutbound",
        RawTransactionMeta(
            slot=outbound_slot,
            block_time_ms=DEPLOY_BLOCK_TIME_MS - 2_000_000,
            err=None,
            account_keys=(WALLET, "SomeoneElseXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX"),
            pre_balances_lamports=(5_000_000_000, 0),
            post_balances_lamports=(4_000_000_000, 1_000_000_000),  # WALLET's balance DECREASED
        ),
    )
    _register(
        transport,
        wallet=WALLET,
        signature="sigGenuine",
        slot=DEPLOY_SLOT - 1_000,
        block_time_ms=DEPLOY_BLOCK_TIME_MS - 1_000_000,
    )
    provider = RpcFundingHistoryProvider(transport)

    result = provider.lookup(
        WALLET, deploy_event_slot=DEPLOY_SLOT, deploy_block_time_ms=DEPLOY_BLOCK_TIME_MS
    )

    assert result.status is FundingAgeStatus.FOUND
    assert result.first_funding_signature == "sigGenuine"


def test_all_candidates_fail_to_confirm_refuses():
    transport = StaticSolanaHistoryTransport()
    outbound_slot = DEPLOY_SLOT - 500
    transport.add_signature(
        WALLET,
        SignatureInfo(
            signature="sigOnlyOutbound",
            slot=outbound_slot,
            block_time_ms=DEPLOY_BLOCK_TIME_MS - 500_000,
            err=None,
        ),
    )
    transport.add_transaction(
        "sigOnlyOutbound",
        RawTransactionMeta(
            slot=outbound_slot,
            block_time_ms=DEPLOY_BLOCK_TIME_MS - 500_000,
            err=None,
            account_keys=(WALLET,),
            pre_balances_lamports=(5_000_000_000,),
            post_balances_lamports=(4_000_000_000,),  # decreased, not funding
        ),
    )
    provider = RpcFundingHistoryProvider(transport)

    result = provider.lookup(
        WALLET, deploy_event_slot=DEPLOY_SLOT, deploy_block_time_ms=DEPLOY_BLOCK_TIME_MS
    )

    assert result.status is FundingAgeStatus.UNDECODABLE
    assert provider.stats.undecodable_no_confirmed_funding == 1


# ---------------------------------------------------------------------------
# 8. Event-time honesty: funding_age is a pure function of on-chain deltas
# ---------------------------------------------------------------------------


def test_funding_age_is_wall_clock_independent(monkeypatch):
    transport = StaticSolanaHistoryTransport()
    funding_slot = DEPLOY_SLOT - 100
    funding_block_time_ms = DEPLOY_BLOCK_TIME_MS - 45_000
    _register(
        transport,
        wallet=WALLET,
        signature="sigWallClockTest",
        slot=funding_slot,
        block_time_ms=funding_block_time_ms,
    )
    provider = RpcFundingHistoryProvider(transport)

    monkeypatch.setattr(time, "time", lambda: 1_000_000.0)
    result_a = provider.lookup(
        WALLET, deploy_event_slot=DEPLOY_SLOT, deploy_block_time_ms=DEPLOY_BLOCK_TIME_MS
    )
    monkeypatch.setattr(time, "time", lambda: 9_000_000.0)
    result_b = provider.lookup(
        WALLET, deploy_event_slot=DEPLOY_SLOT, deploy_block_time_ms=DEPLOY_BLOCK_TIME_MS
    )

    assert result_a.funding_age_ms == result_b.funding_age_ms == 45_000
    assert result_a.funding_age_slots == result_b.funding_age_slots == 100
    # Only the monitoring-only wall-clock fields differ.
    assert result_a.ingest_time_ms != result_b.ingest_time_ms


# ---------------------------------------------------------------------------
# 9. DevFundingAgeResult structural invariants
# ---------------------------------------------------------------------------


def test_undecodable_result_cannot_carry_a_numeric_age():
    with pytest.raises(ValueError, match="UNDECODABLE"):
        DevFundingAgeResult(
            creator_wallet=WALLET,
            deploy_event_slot=DEPLOY_SLOT,
            deploy_block_time_ms=DEPLOY_BLOCK_TIME_MS,
            status=FundingAgeStatus.UNDECODABLE,
            funding_age_ms=1_000,  # smuggled numeric age -- must raise
        )


def test_found_result_requires_all_fields():
    with pytest.raises(ValueError, match="FOUND"):
        DevFundingAgeResult(
            creator_wallet=WALLET,
            deploy_event_slot=DEPLOY_SLOT,
            deploy_block_time_ms=DEPLOY_BLOCK_TIME_MS,
            status=FundingAgeStatus.FOUND,
            # missing first_funding_* / funding_age_* fields
        )


def test_found_result_rejects_funding_after_deploy_slot():
    with pytest.raises(ValueError, match="point-in-time law"):
        DevFundingAgeResult(
            creator_wallet=WALLET,
            deploy_event_slot=DEPLOY_SLOT,
            deploy_block_time_ms=DEPLOY_BLOCK_TIME_MS,
            status=FundingAgeStatus.FOUND,
            first_funding_slot=DEPLOY_SLOT + 1,  # AFTER deploy -- illegal
            first_funding_block_time_ms=DEPLOY_BLOCK_TIME_MS - 1_000,
            first_funding_signature="sigBad",
            funding_age_slots=0,
            funding_age_ms=1_000,
        )


def test_found_result_rejects_inconsistent_age_arithmetic():
    with pytest.raises(ValueError, match="inconsistent"):
        DevFundingAgeResult(
            creator_wallet=WALLET,
            deploy_event_slot=DEPLOY_SLOT,
            deploy_block_time_ms=DEPLOY_BLOCK_TIME_MS,
            status=FundingAgeStatus.FOUND,
            first_funding_slot=DEPLOY_SLOT - 100,
            first_funding_block_time_ms=DEPLOY_BLOCK_TIME_MS - 30_000,
            first_funding_signature="sigMismatch",
            funding_age_slots=999_999,  # wrong -- should be 100
            funding_age_ms=30_000,
        )


def test_money_field_rejects_float():
    with pytest.raises(TypeError):
        DevFundingAgeResult(
            creator_wallet=WALLET,
            deploy_event_slot=DEPLOY_SLOT,
            deploy_block_time_ms=DEPLOY_BLOCK_TIME_MS,
            status=FundingAgeStatus.FOUND,
            first_funding_slot=DEPLOY_SLOT - 100,
            first_funding_block_time_ms=DEPLOY_BLOCK_TIME_MS - 30_000,
            first_funding_amount_lamports=1.5,  # float money -- must raise
            first_funding_signature="sigFloat",
            funding_age_slots=100,
            funding_age_ms=30_000,
        )


def test_money_field_rejects_bool():
    with pytest.raises(TypeError):
        DevFundingAgeResult(
            creator_wallet=WALLET,
            deploy_event_slot=DEPLOY_SLOT,
            deploy_block_time_ms=DEPLOY_BLOCK_TIME_MS,
            status=FundingAgeStatus.FOUND,
            first_funding_slot=DEPLOY_SLOT - 100,
            first_funding_block_time_ms=DEPLOY_BLOCK_TIME_MS - 30_000,
            first_funding_amount_lamports=True,  # bool money -- must raise
            first_funding_signature="sigBool",
            funding_age_slots=100,
            funding_age_ms=30_000,
        )


# ---------------------------------------------------------------------------
# 10. Idempotent / deterministic repeated lookups
# ---------------------------------------------------------------------------


def test_repeated_lookup_is_deterministic():
    transport = StaticSolanaHistoryTransport()
    _register(
        transport,
        wallet=WALLET,
        signature="sigRepeat",
        slot=DEPLOY_SLOT - 100,
        block_time_ms=DEPLOY_BLOCK_TIME_MS - 30_000,
    )
    provider = RpcFundingHistoryProvider(transport)

    r1 = provider.lookup(
        WALLET, deploy_event_slot=DEPLOY_SLOT, deploy_block_time_ms=DEPLOY_BLOCK_TIME_MS
    )
    r2 = provider.lookup(
        WALLET, deploy_event_slot=DEPLOY_SLOT, deploy_block_time_ms=DEPLOY_BLOCK_TIME_MS
    )

    assert r1.status == r2.status
    assert r1.funding_age_ms == r2.funding_age_ms
    assert r1.funding_age_slots == r2.funding_age_slots
    assert r1.first_funding_signature == r2.first_funding_signature


# ---------------------------------------------------------------------------
# HttpJsonRpcSolanaTransport -- construction / config (no live network)
# ---------------------------------------------------------------------------


def test_http_transport_raises_when_rpc_primary_unset():
    with pytest.raises(ValueError, match="RPC_PRIMARY"):
        HttpJsonRpcSolanaTransport(rpc_url=None)


def test_http_transport_from_env_uses_rpc_primary():
    transport = HttpJsonRpcSolanaTransport.from_env({"RPC_PRIMARY": "https://example-rpc.test"})
    assert transport is not None


def test_http_transport_from_env_raises_when_unset():
    with pytest.raises(ValueError, match="RPC_PRIMARY"):
        HttpJsonRpcSolanaTransport.from_env({})


# ---------------------------------------------------------------------------
# No score/rate field anywhere (grep-assert, matches the E15 precedent)
# ---------------------------------------------------------------------------


def test_no_rate_or_score_field_anywhere_in_module():
    import inspect

    import aats.ingestion.dev_funding_age as mod

    src = inspect.getsource(mod)
    for forbidden in ("win_rate", "success_rate", "rug_rate", "hit_rate", "risk_score"):
        assert forbidden not in src
