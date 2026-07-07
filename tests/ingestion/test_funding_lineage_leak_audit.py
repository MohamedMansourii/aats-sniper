"""E-M1-06 -- BACKTEST-QA leak audit (point-in-time regression lock).

Owner: backtest-qa-engineer.  These are NON-DESTRUCTIVE, adversarial regression
tests that lock the point-in-time (T-300a) discipline of the E-M1-06
funding-lineage producer.  They are additive to (and never replace) the
author's ``test_funding_lineage.py``.

Each test here was written from a *proven-load-bearing* mutation experiment: I
injected the classic point-in-time bug and confirmed the assertion below flips
RED.  The three guards locked here are:

  1. STRICT ``<`` on the ``funding_source_seen_before`` path at an EQUAL slot.
     The author's suite locks the strict boundary for ``prior_launch_count``
     (``test_same_slot_launch_not_counted_as_prior``) and the strictly-future /
     out-of-order case for ``funding_source_seen_before``
     (``test_future_funding_source_sighting_excluded_leak_test``), but NOT the
     same-slot-EQUAL case on the seen-before path.  A regression from
     ``bisect_left`` to ``bisect_right`` there would silently treat a same-slot
     (not-provably-prior) cross-wallet sighting as "seen before" -- a lookahead
     leak.  This file closes that gap.

  2. FUTURE on-chain funding is excluded END-TO-END through the E-M1-06
     producer (not merely inside ``dev_funding_age`` in isolation): a funding
     signature that exists ONLY at ``slot > deploy_event_slot`` must yield
     UNDECODABLE / None, never a fabricated funding_source_wallet.

  3. WALL-CLOCK NEVER TOUCHES AN EVENT FIELD.  Perturbing the process wall-clock
     (``_now_ms``) changes only the provenance fields (``ingest_time_ms`` /
     ``data_staleness_ms``) and NEVER any event/feature field
     (creator_wallet, deploy_event_slot, deploy_block_time_ms,
     prior_launch_count, funding_source_wallet, funding_source_seen_before,
     funding_source_status).
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
    FundingSourceStatus,
    InMemoryFundingLineageRegistry,
)

WALLET = "DevWallet1111111111111111111111111111111"
WALLET_2 = "DevWallet2222222222222222222222222222222"
SPONSOR = "SponsorWalletXXXXXXXXXXXXXXXXXXXXXXXXXXXX"
DEPLOY_SLOT = 300_500_000
DEPLOY_BLOCK_TIME_MS = 1_750_000_000_000
MINT_A = "MintAaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
MINT_B = "MintBbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"


def _funding_tx(
    *, sponsor: str, wallet: str, slot: int, block_time_ms: int, amount_lamports: int = 5_000_000_000
) -> RawTransactionMeta:
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
) -> None:
    transport.add_signature(
        wallet, SignatureInfo(signature=signature, slot=slot, block_time_ms=block_time_ms, err=None)
    )
    transport.add_transaction(
        signature, _funding_tx(sponsor=sponsor, wallet=wallet, slot=slot, block_time_ms=block_time_ms)
    )


# ---------------------------------------------------------------------------
# Guard 1 -- STRICT `<` on the funding_source_seen_before same-slot path.
# ---------------------------------------------------------------------------


def test_same_slot_cross_wallet_seen_before_is_false_registry():
    """Two DIFFERENT wallets funded by the SAME source, both deploying in the
    SAME slot: the second must NOT see the first as "seen before" (strict `<`;
    a same-slot sighting is not provably prior).  Direct registry proof --
    RED under a bisect_right (non-strict) regression."""
    registry = InMemoryFundingLineageRegistry()
    registry.record_funding_source(
        funding_source_wallet=SPONSOR, creator_wallet=WALLET, mint=MINT_A, event_slot=DEPLOY_SLOT
    )
    seen = registry.funding_source_seen_before(
        funding_source_wallet=SPONSOR, creator_wallet=WALLET_2, as_of_event_slot=DEPLOY_SLOT
    )
    assert seen is False


def test_same_slot_cross_wallet_seen_before_is_false_end_to_end():
    """Same guard, but through the full provider.lookup() path with real
    funding decodes: two wallets, same source, same deploy_event_slot."""
    transport = StaticSolanaHistoryTransport()
    _register_funding(
        transport,
        wallet=WALLET,
        signature="sigW1",
        slot=DEPLOY_SLOT - 100,
        block_time_ms=DEPLOY_BLOCK_TIME_MS - 30_000,
    )
    _register_funding(
        transport,
        wallet=WALLET_2,
        signature="sigW2",
        slot=DEPLOY_SLOT - 90,
        block_time_ms=DEPLOY_BLOCK_TIME_MS - 27_000,
    )
    provider = FundingLineageProvider(transport, InMemoryFundingLineageRegistry())

    first = provider.lookup(
        creator_wallet=WALLET,
        mint=MINT_A,
        deploy_event_slot=DEPLOY_SLOT,
        deploy_block_time_ms=DEPLOY_BLOCK_TIME_MS,
    )
    second = provider.lookup(
        creator_wallet=WALLET_2,
        mint=MINT_B,
        deploy_event_slot=DEPLOY_SLOT,  # SAME slot as the first launch
        deploy_block_time_ms=DEPLOY_BLOCK_TIME_MS,
    )

    assert first.funding_source_wallet == SPONSOR
    assert second.funding_source_wallet == SPONSOR
    # SPONSOR funded WALLET in the SAME slot -> not provably prior -> False.
    assert second.funding_source_seen_before is False


# ---------------------------------------------------------------------------
# Guard 2 -- FUTURE on-chain funding excluded end-to-end (slot > deploy_slot).
# ---------------------------------------------------------------------------


def test_future_onchain_funding_excluded_end_to_end():
    """The wallet's ONLY funding signature sits at slot > deploy_event_slot
    (i.e. it was funded AFTER it deployed -- impossible to know at decision
    time).  The E-M1-06 producer must refuse: no funding_source_wallet is
    decoded from a post-deploy tx.  RED if the `slot <= deploy_event_slot`
    point-in-time filter is ever weakened."""
    transport = StaticSolanaHistoryTransport()
    _register_funding(
        transport,
        wallet=WALLET,
        signature="sigFuture",
        slot=DEPLOY_SLOT + 5_000,  # STRICTLY AFTER the deploy -- must be invisible
        block_time_ms=DEPLOY_BLOCK_TIME_MS + 2_000_000,
    )
    provider = FundingLineageProvider(transport, InMemoryFundingLineageRegistry())

    result = provider.lookup(
        creator_wallet=WALLET,
        mint=MINT_A,
        deploy_event_slot=DEPLOY_SLOT,
        deploy_block_time_ms=DEPLOY_BLOCK_TIME_MS,
    )

    assert result.funding_source_wallet is None
    assert result.funding_source_status is FundingSourceStatus.UNDECODABLE
    assert result.funding_source_seen_before is False
    assert provider.stats.funding_source_undecodable_age == 1


def test_same_slot_onchain_funding_still_allowed_end_to_end():
    """Control for the boundary above: funding in the SAME slot as the deploy
    (a fund-then-deploy bundle) is legitimate and NON-strict (slot <=), so it
    MUST still decode.  Proves the exclusion above is a `>` boundary, not an
    over-broad `>=` that would drop legitimate same-slot funding."""
    transport = StaticSolanaHistoryTransport()
    _register_funding(
        transport,
        wallet=WALLET,
        signature="sigSameSlot",
        slot=DEPLOY_SLOT,  # SAME slot as deploy -- legitimate bundle
        block_time_ms=DEPLOY_BLOCK_TIME_MS,
    )
    provider = FundingLineageProvider(transport, InMemoryFundingLineageRegistry())

    result = provider.lookup(
        creator_wallet=WALLET,
        mint=MINT_A,
        deploy_event_slot=DEPLOY_SLOT,
        deploy_block_time_ms=DEPLOY_BLOCK_TIME_MS,
    )

    assert result.funding_source_wallet == SPONSOR
    assert result.funding_source_status is FundingSourceStatus.FOUND


# ---------------------------------------------------------------------------
# Guard 3 -- wall-clock never touches an event/feature field.
# ---------------------------------------------------------------------------

EVENT_FIELDS = (
    "creator_wallet",
    "deploy_event_slot",
    "deploy_block_time_ms",
    "prior_launch_count",
    "funding_source_wallet",
    "funding_source_seen_before",
    "funding_source_status",
)


def _lookup_with_clock(monkeypatch: pytest.MonkeyPatch, fixed_now_ms: int):
    monkeypatch.setattr("aats.ingestion.funding_lineage._now_ms", lambda: fixed_now_ms)
    transport = StaticSolanaHistoryTransport()
    _register_funding(
        transport,
        wallet=WALLET,
        signature="sigClock",
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


def test_wall_clock_does_not_leak_into_event_fields(monkeypatch: pytest.MonkeyPatch):
    """Perturbing the process wall-clock must change ONLY provenance
    (ingest_time_ms / data_staleness_ms) and NEVER an event/feature field.
    If any event field ever derived from wall-clock instead of on-chain
    block_time, this test goes RED."""
    early = _lookup_with_clock(monkeypatch, DEPLOY_BLOCK_TIME_MS + 10_000)
    late = _lookup_with_clock(monkeypatch, DEPLOY_BLOCK_TIME_MS + 999_000_000)

    for field_name in EVENT_FIELDS:
        assert getattr(early, field_name) == getattr(late, field_name), (
            f"wall-clock leaked into event field {field_name!r}: "
            f"{getattr(early, field_name)!r} != {getattr(late, field_name)!r}"
        )

    # Provenance fields ARE wall-clock derived and therefore differ -- this
    # confirms the two runs really did see different clocks (test is live).
    assert early.ingest_time_ms != late.ingest_time_ms
    assert early.data_staleness_ms != late.data_staleness_ms
