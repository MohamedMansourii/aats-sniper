"""E-M1-06 -- RAW dev-wallet history + cross-wallet funding-lineage tests.

Self-check items verified here (per the E-M1-06 / WAVE-4 directive):
  1. The full tuple {creator_wallet, prior_launch_count, funding_source_wallet,
     funding_source_seen_before} is produced point-in-time from a single
     `FundingLineageProvider.lookup()` call.
  2. prior_launch_count accumulates across launches on "our own bus" (cheap,
     no new RPC dependency) and is order-tolerant / idempotent-replay-safe.
  3. STRICT point-in-time / no-lookahead: a FUTURE launch (later event_slot)
     never counts toward an earlier launch's prior_launch_count; a FUTURE
     funding-lineage sighting never flips an earlier launch's
     funding_source_seen_before.
  4. funding_source_wallet is decoded via the largest-net-debit heuristic;
     an ambiguous tie refuses (None) rather than guessing.
  5. funding_source_seen_before is True only for a DIFFERENT creator_wallet
     previously funded by the same source (same-wallet repeats stay False --
     that fact is already carried by prior_launch_count).
  6. Refuse-by-default: dev_funding_age UNDECODABLE, a failed tx re-fetch, and
     an ambiguous debit tie all produce funding_source_wallet=None /
     funding_source_status=UNDECODABLE / funding_source_seen_before=False --
     never a fabricated value.
  7. FundingLineageResult structural invariants (UNDECODABLE never carries a
     wallet/True; FOUND requires a wallet; type guards on money-adjacent /
     bool fields).
  8. No rate/ratio/win-rate field anywhere in the module (grep-assert, mirrors
     the E15/E16 precedent).
  9. Determinism: two independent providers over identical on-chain data
     produce identical tuples (barring the wall-clock-only ingest_time_ms).
"""

from __future__ import annotations

import pytest

from aats.ingestion.dev_funding_age import (
    RawTransactionMeta,
    SignatureInfo,
    StaticSolanaHistoryTransport,
)
from aats.ingestion.funding_lineage import (
    FundingLineageProvider,
    FundingLineageResult,
    FundingSourceStatus,
    InMemoryFundingLineageRegistry,
    _identify_funding_source,
)

WALLET = "DevWallet1111111111111111111111111111111"
WALLET_2 = "DevWallet2222222222222222222222222222222"
SPONSOR = "SponsorWalletXXXXXXXXXXXXXXXXXXXXXXXXXXXX"
DEPLOY_SLOT = 300_500_000
DEPLOY_BLOCK_TIME_MS = 1_750_000_000_000  # arbitrary but realistic epoch-ms anchor
MINT_A = "MintAaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
MINT_B = "MintBbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"


def _funding_tx(
    *,
    sponsor: str,
    wallet: str,
    slot: int,
    block_time_ms: int,
    amount_lamports: int = 5_000_000_000,
) -> RawTransactionMeta:
    """A minimal decoded tx: `sponsor` debited, `wallet` credited `amount_lamports`."""
    return RawTransactionMeta(
        slot=slot,
        block_time_ms=block_time_ms,
        err=None,
        account_keys=(sponsor, wallet),
        pre_balances_lamports=(10_000_000_000, 0),
        post_balances_lamports=(10_000_000_000 - amount_lamports, amount_lamports),
    )


def _register_funding(
    transport: StaticSolanaHistoryTransport,
    *,
    wallet: str,
    signature: str,
    slot: int,
    block_time_ms: int,
    sponsor: str = SPONSOR,
    amount_lamports: int = 5_000_000_000,
) -> None:
    transport.add_signature(
        wallet, SignatureInfo(signature=signature, slot=slot, block_time_ms=block_time_ms, err=None)
    )
    transport.add_transaction(
        signature,
        _funding_tx(
            sponsor=sponsor,
            wallet=wallet,
            slot=slot,
            block_time_ms=block_time_ms,
            amount_lamports=amount_lamports,
        ),
    )


def _provider(transport: StaticSolanaHistoryTransport | None = None) -> FundingLineageProvider:
    return FundingLineageProvider(
        transport or StaticSolanaHistoryTransport(), InMemoryFundingLineageRegistry()
    )


# ---------------------------------------------------------------------------
# 1. Full tuple produced point-in-time
# ---------------------------------------------------------------------------


def test_full_tuple_produced_point_in_time():
    transport = StaticSolanaHistoryTransport()
    _register_funding(
        transport,
        wallet=WALLET,
        signature="sigFund",
        slot=DEPLOY_SLOT - 100,
        block_time_ms=DEPLOY_BLOCK_TIME_MS - 30_000,
    )
    provider = _provider(transport)

    result = provider.lookup(
        creator_wallet=WALLET,
        mint=MINT_A,
        deploy_event_slot=DEPLOY_SLOT,
        deploy_block_time_ms=DEPLOY_BLOCK_TIME_MS,
    )

    assert result.creator_wallet == WALLET
    assert result.prior_launch_count == 0
    assert result.funding_source_wallet == SPONSOR
    assert result.funding_source_seen_before is False
    assert result.funding_source_status is FundingSourceStatus.FOUND
    assert provider.stats.funding_source_found == 1


# ---------------------------------------------------------------------------
# 2/3. prior_launch_count: accumulates, order-tolerant, no-lookahead
# ---------------------------------------------------------------------------


def test_prior_launch_count_accumulates_across_launches():
    registry = InMemoryFundingLineageRegistry()
    provider = FundingLineageProvider(StaticSolanaHistoryTransport(), registry)

    r1 = provider.lookup(
        creator_wallet=WALLET,
        mint=MINT_A,
        deploy_event_slot=DEPLOY_SLOT,
        deploy_block_time_ms=DEPLOY_BLOCK_TIME_MS,
    )
    r2 = provider.lookup(
        creator_wallet=WALLET,
        mint=MINT_B,
        deploy_event_slot=DEPLOY_SLOT + 1_000,
        deploy_block_time_ms=DEPLOY_BLOCK_TIME_MS + 400_000,
    )

    assert r1.prior_launch_count == 0
    assert r2.prior_launch_count == 1


def test_future_launch_never_counts_toward_an_earlier_one_leak_test():
    """Process a LATER launch first (out-of-order arrival), then an EARLIER
    one for the same wallet -- the earlier launch's prior_launch_count must
    NOT see the later one.  This is the load-bearing no-lookahead guarantee."""
    registry = InMemoryFundingLineageRegistry()
    provider = FundingLineageProvider(StaticSolanaHistoryTransport(), registry)

    later = provider.lookup(
        creator_wallet=WALLET,
        mint=MINT_B,
        deploy_event_slot=DEPLOY_SLOT + 10_000,
        deploy_block_time_ms=DEPLOY_BLOCK_TIME_MS + 4_000_000,
    )
    earlier = provider.lookup(
        creator_wallet=WALLET,
        mint=MINT_A,
        deploy_event_slot=DEPLOY_SLOT,
        deploy_block_time_ms=DEPLOY_BLOCK_TIME_MS,
    )

    assert later.prior_launch_count == 0  # nothing preceded it at insert time
    assert earlier.prior_launch_count == 0  # the future launch must not leak backward


def test_same_slot_launch_not_counted_as_prior():
    """Two distinct mints from the same wallet in the SAME slot: neither
    counts as "prior" for the other (strict `<`, no same-slot ordering
    claim -- mirrors deployer_reputation.py's strict convention)."""
    registry = InMemoryFundingLineageRegistry()
    provider = FundingLineageProvider(StaticSolanaHistoryTransport(), registry)

    provider.lookup(
        creator_wallet=WALLET,
        mint=MINT_A,
        deploy_event_slot=DEPLOY_SLOT,
        deploy_block_time_ms=DEPLOY_BLOCK_TIME_MS,
    )
    same_slot = provider.lookup(
        creator_wallet=WALLET,
        mint=MINT_B,
        deploy_event_slot=DEPLOY_SLOT,  # identical slot
        deploy_block_time_ms=DEPLOY_BLOCK_TIME_MS,
    )

    assert same_slot.prior_launch_count == 0


def test_duplicate_replay_does_not_inflate_prior_launch_count():
    registry = InMemoryFundingLineageRegistry()
    provider = FundingLineageProvider(StaticSolanaHistoryTransport(), registry)

    kwargs = {
        "creator_wallet": WALLET,
        "mint": MINT_A,
        "deploy_event_slot": DEPLOY_SLOT,
        "deploy_block_time_ms": DEPLOY_BLOCK_TIME_MS,
    }
    provider.lookup(**kwargs)
    provider.lookup(**kwargs)  # exact duplicate redelivery

    later = provider.lookup(
        creator_wallet=WALLET,
        mint=MINT_B,
        deploy_event_slot=DEPLOY_SLOT + 1_000,
        deploy_block_time_ms=DEPLOY_BLOCK_TIME_MS + 400_000,
    )
    assert later.prior_launch_count == 1  # not 2 -- the replay was a no-op


def test_unknown_wallet_prior_launch_count_is_zero_not_fabricated():
    registry = InMemoryFundingLineageRegistry()
    assert registry.prior_launch_count(creator_wallet="NeverSeen", as_of_event_slot=DEPLOY_SLOT) == 0


# ---------------------------------------------------------------------------
# 4. funding_source_wallet decode -- largest-debit heuristic + ambiguity refusal
# ---------------------------------------------------------------------------


def test_identify_funding_source_single_debited_account():
    tx = _funding_tx(sponsor=SPONSOR, wallet=WALLET, slot=1, block_time_ms=1)
    assert _identify_funding_source(tx, WALLET) == SPONSOR


def test_identify_funding_source_ambiguous_tie_refuses():
    tx = RawTransactionMeta(
        slot=1,
        block_time_ms=1,
        err=None,
        account_keys=("SenderA", "SenderB", WALLET),
        pre_balances_lamports=(10_000_000_000, 10_000_000_000, 0),
        post_balances_lamports=(7_500_000_000, 7_500_000_000, 5_000_000_000),  # tied debits
    )
    assert _identify_funding_source(tx, WALLET) is None


def test_identify_funding_source_no_debited_account_refuses():
    tx = RawTransactionMeta(
        slot=1,
        block_time_ms=1,
        err=None,
        account_keys=(WALLET,),
        pre_balances_lamports=(0,),
        post_balances_lamports=(5_000_000_000,),  # only the wallet itself present
    )
    assert _identify_funding_source(tx, WALLET) is None


def test_identify_funding_source_wallet_absent_refuses():
    tx = _funding_tx(sponsor=SPONSOR, wallet=WALLET, slot=1, block_time_ms=1)
    assert _identify_funding_source(tx, "SomeOtherWallet") is None


def test_full_lookup_ambiguous_debit_refuses_never_fabricates():
    transport = StaticSolanaHistoryTransport()
    signature = "sigAmbiguous"
    slot = DEPLOY_SLOT - 100
    block_time_ms = DEPLOY_BLOCK_TIME_MS - 30_000
    transport.add_signature(
        WALLET, SignatureInfo(signature=signature, slot=slot, block_time_ms=block_time_ms, err=None)
    )
    transport.add_transaction(
        signature,
        RawTransactionMeta(
            slot=slot,
            block_time_ms=block_time_ms,
            err=None,
            account_keys=("SenderA", "SenderB", WALLET),
            pre_balances_lamports=(10_000_000_000, 10_000_000_000, 0),
            post_balances_lamports=(7_500_000_000, 7_500_000_000, 5_000_000_000),
        ),
    )
    provider = _provider(transport)

    result = provider.lookup(
        creator_wallet=WALLET,
        mint=MINT_A,
        deploy_event_slot=DEPLOY_SLOT,
        deploy_block_time_ms=DEPLOY_BLOCK_TIME_MS,
    )

    assert result.funding_source_wallet is None
    assert result.funding_source_status is FundingSourceStatus.UNDECODABLE
    assert result.funding_source_seen_before is False
    assert provider.stats.funding_source_undecodable_ambiguous == 1


# ---------------------------------------------------------------------------
# 6. Refuse-by-default: age UNDECODABLE / tx re-fetch failure
# ---------------------------------------------------------------------------


def test_no_funding_history_at_all_refuses_never_fabricates():
    transport = StaticSolanaHistoryTransport()  # WALLET has zero signature history
    provider = _provider(transport)

    result = provider.lookup(
        creator_wallet=WALLET,
        mint=MINT_A,
        deploy_event_slot=DEPLOY_SLOT,
        deploy_block_time_ms=DEPLOY_BLOCK_TIME_MS,
    )

    assert result.funding_source_wallet is None
    assert result.funding_source_status is FundingSourceStatus.UNDECODABLE
    assert result.funding_source_seen_before is False
    # prior_launch_count is UNAFFECTED by an undecodable funding lookup.
    assert result.prior_launch_count == 0
    assert provider.stats.funding_source_undecodable_age == 1


class _FlakyReFetchTransport:
    """Wraps a StaticSolanaHistoryTransport; the Nth get_transaction() call
    for `fail_signature` raises; all other calls delegate untouched."""

    def __init__(
        self, inner: StaticSolanaHistoryTransport, *, fail_signature: str, fail_on_call_number: int
    ) -> None:
        self._inner = inner
        self._fail_signature = fail_signature
        self._fail_on_call_number = fail_on_call_number
        self._call_count = 0

    def get_signatures_for_address(self, wallet: str, *, before: str | None, limit: int):
        return self._inner.get_signatures_for_address(wallet, before=before, limit=limit)

    def get_transaction(self, signature: str):
        if signature == self._fail_signature:
            self._call_count += 1
            if self._call_count == self._fail_on_call_number:
                raise ConnectionError("simulated flaky re-fetch")
        return self._inner.get_transaction(signature)


def test_funding_source_undecodable_when_tx_refetch_fails():
    """dev_funding_age resolves FOUND (call #1 to get_transaction succeeds),
    but this module's OWN re-fetch (call #2, same signature) fails -- must
    refuse the funding_source_wallet rather than crash or fabricate."""
    inner = StaticSolanaHistoryTransport()
    _register_funding(
        inner,
        wallet=WALLET,
        signature="sigFlaky",
        slot=DEPLOY_SLOT - 100,
        block_time_ms=DEPLOY_BLOCK_TIME_MS - 30_000,
    )
    flaky = _FlakyReFetchTransport(inner, fail_signature="sigFlaky", fail_on_call_number=2)
    provider = FundingLineageProvider(flaky, InMemoryFundingLineageRegistry())

    result = provider.lookup(
        creator_wallet=WALLET,
        mint=MINT_A,
        deploy_event_slot=DEPLOY_SLOT,
        deploy_block_time_ms=DEPLOY_BLOCK_TIME_MS,
    )

    assert result.funding_source_wallet is None
    assert result.funding_source_status is FundingSourceStatus.UNDECODABLE
    assert provider.stats.funding_source_undecodable_tx_fetch == 1


# ---------------------------------------------------------------------------
# 5. funding_source_seen_before -- cross-wallet clustering, same-wallet excluded
# ---------------------------------------------------------------------------


def test_funding_source_seen_before_true_for_a_different_wallet():
    transport = StaticSolanaHistoryTransport()
    _register_funding(
        transport,
        wallet=WALLET,
        signature="sigW1",
        slot=DEPLOY_SLOT - 100,
        block_time_ms=DEPLOY_BLOCK_TIME_MS - 30_000,
        sponsor=SPONSOR,
    )
    _register_funding(
        transport,
        wallet=WALLET_2,
        signature="sigW2",
        slot=DEPLOY_SLOT + 900,  # later than WALLET's deploy
        block_time_ms=DEPLOY_BLOCK_TIME_MS + 300_000,
        sponsor=SPONSOR,  # SAME funding source
    )
    registry = InMemoryFundingLineageRegistry()
    provider = FundingLineageProvider(transport, registry)

    first = provider.lookup(
        creator_wallet=WALLET,
        mint=MINT_A,
        deploy_event_slot=DEPLOY_SLOT,
        deploy_block_time_ms=DEPLOY_BLOCK_TIME_MS,
    )
    second = provider.lookup(
        creator_wallet=WALLET_2,
        mint=MINT_B,
        deploy_event_slot=DEPLOY_SLOT + 1_000,
        deploy_block_time_ms=DEPLOY_BLOCK_TIME_MS + 400_000,
    )

    assert first.funding_source_wallet == SPONSOR
    assert first.funding_source_seen_before is False  # first-ever sighting of SPONSOR
    assert second.funding_source_wallet == SPONSOR
    assert second.funding_source_seen_before is True  # SPONSOR already funded a DIFFERENT wallet


def test_funding_source_seen_before_false_for_same_wallet_repeat():
    """A wallet re-funded by the SAME source for its own second launch must
    NOT flip seen_before True on its own (that fact is already carried by
    prior_launch_count -- this field is the cross-wallet signal only)."""
    transport = StaticSolanaHistoryTransport()
    _register_funding(
        transport,
        wallet=WALLET,
        signature="sigFirst",
        slot=DEPLOY_SLOT - 5_000,
        block_time_ms=DEPLOY_BLOCK_TIME_MS - 2_000_000,
        sponsor=SPONSOR,
    )
    _register_funding(
        transport,
        wallet=WALLET,
        signature="sigSecond",
        slot=DEPLOY_SLOT + 900,
        block_time_ms=DEPLOY_BLOCK_TIME_MS + 300_000,
        sponsor=SPONSOR,
    )
    registry = InMemoryFundingLineageRegistry()
    provider = FundingLineageProvider(transport, registry)

    first = provider.lookup(
        creator_wallet=WALLET,
        mint=MINT_A,
        deploy_event_slot=DEPLOY_SLOT,
        deploy_block_time_ms=DEPLOY_BLOCK_TIME_MS,
    )
    second = provider.lookup(
        creator_wallet=WALLET,
        mint=MINT_B,
        deploy_event_slot=DEPLOY_SLOT + 1_000,
        deploy_block_time_ms=DEPLOY_BLOCK_TIME_MS + 400_000,
    )

    assert first.funding_source_seen_before is False
    assert second.funding_source_seen_before is False
    assert second.prior_launch_count == 1  # the "seen before" info lives here instead


def test_future_funding_source_sighting_excluded_leak_test():
    """Process a LATER wallet's funding-lineage sighting first, then an
    EARLIER wallet funded by the same source -- the earlier lookup must NOT
    see the later sighting.  Direct no-lookahead proof for the registry's
    own state (distinct from dev_funding_age's own point-in-time guarantee)."""
    transport = StaticSolanaHistoryTransport()
    _register_funding(
        transport,
        wallet=WALLET_2,
        signature="sigLater",
        slot=DEPLOY_SLOT + 900,
        block_time_ms=DEPLOY_BLOCK_TIME_MS + 300_000,
        sponsor=SPONSOR,
    )
    _register_funding(
        transport,
        wallet=WALLET,
        signature="sigEarlier",
        slot=DEPLOY_SLOT - 100,
        block_time_ms=DEPLOY_BLOCK_TIME_MS - 30_000,
        sponsor=SPONSOR,
    )
    registry = InMemoryFundingLineageRegistry()
    provider = FundingLineageProvider(transport, registry)

    later = provider.lookup(
        creator_wallet=WALLET_2,
        mint=MINT_B,
        deploy_event_slot=DEPLOY_SLOT + 1_000,
        deploy_block_time_ms=DEPLOY_BLOCK_TIME_MS + 400_000,
    )
    earlier = provider.lookup(
        creator_wallet=WALLET,
        mint=MINT_A,
        deploy_event_slot=DEPLOY_SLOT,
        deploy_block_time_ms=DEPLOY_BLOCK_TIME_MS,
    )

    assert later.funding_source_seen_before is False  # nothing preceded it at insert time
    assert earlier.funding_source_seen_before is False  # the future sighting must not leak backward


# ---------------------------------------------------------------------------
# 7. FundingLineageResult structural invariants
# ---------------------------------------------------------------------------


def test_undecodable_result_cannot_carry_a_funding_source_wallet():
    with pytest.raises(ValueError, match="UNDECODABLE"):
        FundingLineageResult(
            creator_wallet=WALLET,
            deploy_event_slot=DEPLOY_SLOT,
            deploy_block_time_ms=DEPLOY_BLOCK_TIME_MS,
            prior_launch_count=0,
            funding_source_wallet=SPONSOR,  # smuggled -- must raise
            funding_source_seen_before=False,
            funding_source_status=FundingSourceStatus.UNDECODABLE,
        )


def test_undecodable_result_cannot_carry_seen_before_true():
    with pytest.raises(ValueError, match="UNDECODABLE"):
        FundingLineageResult(
            creator_wallet=WALLET,
            deploy_event_slot=DEPLOY_SLOT,
            deploy_block_time_ms=DEPLOY_BLOCK_TIME_MS,
            prior_launch_count=0,
            funding_source_wallet=None,
            funding_source_seen_before=True,  # smuggled -- must raise
            funding_source_status=FundingSourceStatus.UNDECODABLE,
        )


def test_found_result_requires_a_funding_source_wallet():
    with pytest.raises(ValueError, match="FOUND"):
        FundingLineageResult(
            creator_wallet=WALLET,
            deploy_event_slot=DEPLOY_SLOT,
            deploy_block_time_ms=DEPLOY_BLOCK_TIME_MS,
            prior_launch_count=0,
            funding_source_wallet=None,  # missing -- must raise
            funding_source_seen_before=False,
            funding_source_status=FundingSourceStatus.FOUND,
        )


def test_prior_launch_count_rejects_bool():
    with pytest.raises(TypeError):
        FundingLineageResult(
            creator_wallet=WALLET,
            deploy_event_slot=DEPLOY_SLOT,
            deploy_block_time_ms=DEPLOY_BLOCK_TIME_MS,
            prior_launch_count=True,  # bool masquerading as int -- must raise
            funding_source_wallet=None,
            funding_source_seen_before=False,
            funding_source_status=FundingSourceStatus.UNDECODABLE,
        )


def test_funding_source_seen_before_rejects_non_bool():
    with pytest.raises(TypeError):
        FundingLineageResult(
            creator_wallet=WALLET,
            deploy_event_slot=DEPLOY_SLOT,
            deploy_block_time_ms=DEPLOY_BLOCK_TIME_MS,
            prior_launch_count=0,
            funding_source_wallet=None,
            funding_source_seen_before=1,  # int, not bool -- must raise
            funding_source_status=FundingSourceStatus.UNDECODABLE,
        )


def test_creator_wallet_rejects_empty_string():
    with pytest.raises(ValueError):
        FundingLineageResult(
            creator_wallet="",
            deploy_event_slot=DEPLOY_SLOT,
            deploy_block_time_ms=DEPLOY_BLOCK_TIME_MS,
            prior_launch_count=0,
            funding_source_wallet=None,
            funding_source_seen_before=False,
            funding_source_status=FundingSourceStatus.UNDECODABLE,
        )


# ---------------------------------------------------------------------------
# 8. No rate/score/win-rate field anywhere (grep-assert, matches E15/E16 precedent)
# ---------------------------------------------------------------------------


def test_no_rate_or_score_field_anywhere_in_module():
    import inspect

    import aats.ingestion.funding_lineage as mod

    src = inspect.getsource(mod)
    for forbidden in (
        "win_rate",
        "success_rate",
        "rug_rate",
        "hit_rate",
        "risk_score",
        "conviction",
    ):
        assert forbidden not in src


# ---------------------------------------------------------------------------
# 9. Determinism across independent providers
# ---------------------------------------------------------------------------


def test_determinism_across_independent_providers():
    def _fresh_result() -> FundingLineageResult:
        transport = StaticSolanaHistoryTransport()
        _register_funding(
            transport,
            wallet=WALLET,
            signature="sigDet",
            slot=DEPLOY_SLOT - 100,
            block_time_ms=DEPLOY_BLOCK_TIME_MS - 30_000,
        )
        provider = FundingLineageProvider(transport, InMemoryFundingLineageRegistry())
        return provider.lookup(
            creator_wallet=WALLET,
            mint=MINT_A,
            deploy_event_slot=DEPLOY_SLOT,
            deploy_block_time_ms=DEPLOY_BLOCK_TIME_MS,
        )

    r1 = _fresh_result()
    r2 = _fresh_result()

    assert r1.prior_launch_count == r2.prior_launch_count
    assert r1.funding_source_wallet == r2.funding_source_wallet
    assert r1.funding_source_seen_before == r2.funding_source_seen_before
    assert r1.funding_source_status == r2.funding_source_status
