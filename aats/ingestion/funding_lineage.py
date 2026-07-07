"""E-M1-06 — RAW dev-wallet history + cross-wallet funding-lineage ingestion.

OWNERSHIP: data-ingestion-engineer (M1).  This module owns the RAW,
POINT-IN-TIME production of one tuple per observed launch:

    {creator_wallet, prior_launch_count, funding_source_wallet,
     funding_source_seen_before}

It composes two ALREADY-OWNED M1 signals:
  (a) ``prior_launch_count`` — a count of this creator wallet's OWN prior
      launches, sourced from LaunchEvents we have already decoded off our own
      bus (cheap, in-process, no new RPC/HTTP dependency).  This is
      deliberately NOT the labelled-outcome tally
      (``aats.ingestion.deployer_reputation.DeployerReputationTally`` — E15),
      which requires a RESOLVED ``LaunchOutcome`` corpus.  Here we count raw
      launch EVENTS regardless of outcome — "has this wallet deployed
      before", not "how did those deploys turn out".
  (b) ``funding_source_wallet`` / ``funding_source_seen_before`` — the
      on-chain address that funded this creator wallet's first pre-deploy SOL
      transfer, and whether THAT source address has previously funded a
      DIFFERENT creator wallet we have already observed.  This is the raw
      cross-wallet "funding lineage" clustering fact a sybil/rug-factory
      network relies on (one operator funding many disposable deployer
      wallets from a single source) — surfaced here as a plain fact, never a
      score.

NO SCORING (hard rule, non-waivable)
--------------------------------------
  This module STOPS at the raw tuple.  There is no risk decision, no
  reject/downweight verdict, no rate/ratio/probability field anywhere here.
  The REJECT/DOWN-WEIGHT verdict belongs to the existing risk-guardrails
  gates (E15 ``aats/risk/deployer_reputation_gate.py``, E16
  ``aats/risk/dev_funding_age_gate.py``, and any future E-M1-06 consumer) —
  never to this module.  This tuple ALSO seeds a future smart-money /
  reaction corpus (per the build directive) — it is kept clean and reusable
  precisely because it carries no baked-in verdict.

REUSE, NOT REINVENTION
-------------------------
  The funding-SOURCE decode is built directly on top of the ALREADY TESTED,
  mutation-proven bounded-budget paginated walk in
  ``aats.ingestion.dev_funding_age.RpcFundingHistoryProvider`` (same
  ``SolanaHistoryTransport`` Protocol, same ``RawTransactionMeta`` /
  ``SignatureInfo`` shapes, same refuse-by-default genesis-reached /
  page-budget / RPC-error handling).  This module does NOT re-implement
  pagination — it calls ``RpcFundingHistoryProvider.lookup()`` to obtain the
  wallet's first pre-deploy funding transaction signature (the "first N
  inbound SOL-funding transfers" walk, bounded by ``max_transactions_decoded``
  exactly as in E16), then performs ONE extra, layout-agnostic decode step to
  identify WHICH account in that same transaction was the debited source
  (see ``_identify_funding_source`` below).

DECODE METHOD — largest-debit heuristic, not instruction-byte offsets
--------------------------------------------------------------------------
  Given the funding transaction's balance deltas (the same
  ``meta.preBalances``/``meta.postBalances`` shape ``dev_funding_age.py``
  already decodes), the funding SOURCE is the account (other than the funded
  wallet) with the LARGEST net debit.  For an ordinary System Program
  transfer (or a fee-payer-is-sender transfer) this is unambiguous: the
  sender's balance decreases by (transfer amount [+ fee if same account]),
  while any other debit in the same transaction (e.g. an unrelated fee-payer
  quirk) is normally orders of magnitude smaller.  If two or more accounts
  show the EXACT SAME largest debit magnitude (a genuine tie) the source is
  AMBIGUOUS and this module REFUSES rather than guess.  A more precise decode
  would parse the System Program ``Transfer`` instruction's ``from``/``to``
  account indices directly; the balance-delta heuristic is used instead
  because it is layout-agnostic and also covers CPI-based funding paths
  (mirrors ``dev_funding_age.py``'s own justification for the same choice).

STRICT POINT-IN-TIME (T-300a, the law of this signal)
---------------------------------------------------------
  ``funding_source_wallet`` inherits the SAME point-in-time law as
  ``dev_funding_age.py``: only a funding transaction with on-chain
  ``slot <= deploy_event_slot`` is ever considered (non-strict — a bundle
  that funds-then-deploys in the SAME slot is still legitimate).

  ``prior_launch_count`` and ``funding_source_seen_before`` are both computed
  from ``InMemoryFundingLineageRegistry``, an append-only, event-time-sorted,
  in-process index.  A prior sighting counts **iff** its ``event_slot`` is
  STRICTLY LESS THAN the current launch's ``deploy_event_slot`` (mirrors
  ``deployer_reputation.py``'s ``_strictly_before`` bisect convention) — a
  launch can never see itself or any future launch.  Because
  ``FundingLineageProvider.lookup()`` ALWAYS queries the registry BEFORE
  recording the current launch into it, and because the query is additionally
  guarded by the strict ``<`` comparison against the CURRENT launch's own
  slot, self-reference is structurally impossible even under exact-duplicate
  replay of the same LaunchEvent (the just-inserted sighting sits at
  ``event_slot == deploy_event_slot``, which fails the strict-less-than test
  and is excluded automatically) — no explicit "exclude self" parameter is
  needed; see the registry's docstring for the argument in full.

  ``funding_source_seen_before`` deliberately EXCLUDES sightings of the SAME
  creator_wallet (a repeat launch from a wallet funded by the same source
  every time is not new information — ``prior_launch_count`` already carries
  that fact).  It fires True only when the SAME funding_source_wallet
  previously funded a DIFFERENT creator_wallet — the non-redundant,
  cross-wallet clustering signal this module exists to surface.

REFUSE-BY-DEFAULT
--------------------
  ``funding_source_wallet`` is None (status ``UNDECODABLE``) whenever:
    * the underlying ``dev_funding_age`` lookup itself is UNDECODABLE (RPC
      error, no history, budget exhausted, no confirmed candidate — see that
      module's own docstring), or
    * the funding transaction cannot be re-fetched (RPC error), or
    * the debited-source heuristic is ambiguous (a tie) or finds no debited
      account at all.
  An UNDECODABLE result NEVER carries a fabricated ``funding_source_wallet``
  or a fabricated ``funding_source_seen_before=True`` — both are forced to
  ``None``/``False`` and this is enforced structurally by
  ``FundingLineageResult.__post_init__``.  ``prior_launch_count`` is NEVER
  "undecodable" — an unknown creator_wallet simply has zero recorded prior
  launches (no information, NOT "clean/good"; mirrors
  ``deployer_reputation.py``'s zero-tally convention).

FAST-PATH SAFETY
-------------------
  This module performs RPC I/O (via the injected ``SolanaHistoryTransport``)
  — it is explicitly an enrichment-tier / SLOW producer, never called from
  the sub-10ms SNIPE hot path.  ``FundingLineageProvider.lookup()`` is meant
  to be called once per observed LaunchEvent (off the hot path); the registry
  accumulates "our own bus" state as a side effect of that single call site —
  no separate feed/subscription is required.

MONEY / SECRETS
-------------------
  No money field is stored or computed here (`dev_funding_age.py` already
  owns ``first_funding_amount_lamports``).  No secrets — the RPC endpoint is
  injected via the SAME ``SolanaHistoryTransport`` used by
  ``dev_funding_age.py`` (env-configured ``RPC_PRIMARY``), never a literal.
"""

from __future__ import annotations

import bisect
import logging
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from aats.ingestion.dev_funding_age import (
    FundingAgeStatus,
    RawTransactionMeta,
    RpcFundingHistoryProvider,
    SolanaHistoryTransport,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Type guards (money/identity rule; mirrors dev_funding_age.py / deployer_reputation.py)
# ---------------------------------------------------------------------------


def _check_int(name: str, value: object, *, min_value: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be int, not {type(value).__name__}={value!r}.")
    if min_value is not None and value < min_value:
        raise ValueError(f"{name} must be >= {min_value}, got {value}.")
    return value


def _check_str(name: str, value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty str, got {value!r}.")
    return value


def _check_bool(name: str, value: object) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"{name} must be bool, not {type(value).__name__}={value!r}.")
    return value


def _now_ms() -> int:
    return int(time.time() * 1_000)


# ---------------------------------------------------------------------------
# Funding-SOURCE decode — largest-debit heuristic (see module docstring).
# ---------------------------------------------------------------------------


def _identify_funding_source(tx: RawTransactionMeta, funded_wallet: str) -> str | None:
    """Return the single account that funded `funded_wallet` in `tx`, or None.

    Returns None (refuse) when:
      - `funded_wallet` is not present in `tx.account_keys`,
      - no OTHER account shows a net debit in this transaction, or
      - two or more accounts tie for the largest debit (genuinely ambiguous).

    Never guesses among multiple plausible candidates.
    """
    try:
        funded_idx = tx.account_keys.index(funded_wallet)
    except ValueError:
        return None

    debits: list[tuple[int, str]] = []  # (debit magnitude, account key)
    for idx, key in enumerate(tx.account_keys):
        if idx == funded_idx:
            continue
        if idx >= len(tx.pre_balances_lamports) or idx >= len(tx.post_balances_lamports):
            continue
        delta = tx.post_balances_lamports[idx] - tx.pre_balances_lamports[idx]
        if delta < 0:
            debits.append((-delta, key))

    if not debits:
        return None

    debits.sort(key=lambda d: d[0], reverse=True)
    if len(debits) > 1 and debits[0][0] == debits[1][0]:
        return None  # ambiguous tie -- refuse rather than guess

    return debits[0][1]


# ---------------------------------------------------------------------------
# FundingLineageResult -- the public, honest, REFUSE-BY-DEFAULT tuple.
# ---------------------------------------------------------------------------


class FundingSourceStatus(StrEnum):
    FOUND = "found"  # funding_source_wallet was conclusively decoded
    UNDECODABLE = "undecodable"  # refuse-by-default -- see module docstring


@dataclass(frozen=True)
class FundingLineageResult:
    """Point-in-time RAW dev-wallet-history + funding-lineage tuple.  NEVER a score.

    Exactly the tuple specified by the E-M1-06 build directive:
    {creator_wallet, prior_launch_count, funding_source_wallet,
     funding_source_seen_before}, plus the honest provenance/status fields
    this codebase's ingestion side always carries (mirrors
    ``DevFundingAgeResult`` / ``DeployerReputationTally``).

    ``prior_launch_count``: raw count of this creator wallet's OWN prior
    LaunchEvents (regardless of outcome) with event_slot strictly before this
    launch's ``deploy_event_slot``.  Always defined -- 0 means "no information",
    NEVER "clean/good".

    ``funding_source_wallet`` / ``funding_source_seen_before``: None / False
    (with ``funding_source_status=UNDECODABLE``) whenever the funding source
    could not be conclusively decoded -- NEVER a fabricated value.

    NO rate/ratio/probability/win-rate field exists on this type.
    """

    creator_wallet: str
    deploy_event_slot: int
    deploy_block_time_ms: int
    prior_launch_count: int
    funding_source_wallet: str | None
    funding_source_seen_before: bool
    funding_source_status: FundingSourceStatus
    ingest_time_ms: int = field(default_factory=_now_ms)
    data_staleness_ms: int = 0

    def __post_init__(self) -> None:
        _check_str("FundingLineageResult.creator_wallet", self.creator_wallet)
        _check_int("FundingLineageResult.deploy_event_slot", self.deploy_event_slot, min_value=0)
        _check_int(
            "FundingLineageResult.deploy_block_time_ms", self.deploy_block_time_ms, min_value=1
        )
        _check_int(
            "FundingLineageResult.prior_launch_count", self.prior_launch_count, min_value=0
        )
        if self.funding_source_wallet is not None:
            _check_str("FundingLineageResult.funding_source_wallet", self.funding_source_wallet)
        _check_bool(
            "FundingLineageResult.funding_source_seen_before", self.funding_source_seen_before
        )
        if not isinstance(self.funding_source_status, FundingSourceStatus):
            raise TypeError(
                "FundingLineageResult.funding_source_status must be a FundingSourceStatus"
            )
        _check_int("FundingLineageResult.ingest_time_ms", self.ingest_time_ms, min_value=0)
        _check_int("FundingLineageResult.data_staleness_ms", self.data_staleness_ms, min_value=0)

        if self.funding_source_status is FundingSourceStatus.UNDECODABLE:
            # Refuse-by-default, structurally: UNDECODABLE can never smuggle a
            # funding_source_wallet or a fabricated True through.
            if self.funding_source_wallet is not None or self.funding_source_seen_before:
                raise ValueError(
                    "FundingLineageResult: funding_source_status=UNDECODABLE must not carry "
                    "a funding_source_wallet or funding_source_seen_before=True "
                    "(refuse-by-default; unknown != safe, unknown != first-time)."
                )
        else:
            # status is FOUND -- funding_source_wallet is required.
            if self.funding_source_wallet is None:
                raise ValueError(
                    "FundingLineageResult: funding_source_status=FOUND requires a "
                    "non-None funding_source_wallet."
                )


# ---------------------------------------------------------------------------
# FundingLineageRegistry -- append-only, event-time-sorted, in-process index.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _LaunchSighting:
    mint: str
    event_slot: int


@dataclass(frozen=True)
class _FundingSourceSighting:
    creator_wallet: str
    mint: str
    event_slot: int


class FundingLineageRegistry(Protocol):
    """Protocol for the "our own bus" launch-history / funding-lineage index.

    CONTRACT: both query methods MUST only consider sightings with
    ``event_slot < as_of_event_slot`` (STRICT) -- never the current launch's
    own or any future sighting.  Implementations are responsible for
    enforcing this (see ``InMemoryFundingLineageRegistry`` for the reference
    bisect-based implementation).
    """

    def record_launch(self, *, creator_wallet: str, mint: str, event_slot: int) -> None: ...

    def record_funding_source(
        self, *, funding_source_wallet: str, creator_wallet: str, mint: str, event_slot: int
    ) -> None: ...

    def prior_launch_count(self, *, creator_wallet: str, as_of_event_slot: int) -> int: ...

    def funding_source_seen_before(
        self, *, funding_source_wallet: str, creator_wallet: str, as_of_event_slot: int
    ) -> bool: ...


class InMemoryFundingLineageRegistry:
    """In-process, injectable ``FundingLineageRegistry`` -- "our own bus", cheap.

    Pre-indexes every recorded launch into a per-``creator_wallet`` bucket
    (sorted ascending by ``event_slot``) and every recorded funding-lineage
    sighting into a per-``funding_source_wallet`` bucket (same sort order).  A
    query is an O(1) dict lookup for the bucket plus a bisect over ONLY that
    key's own sightings -- never a scan of the whole registry.

    IDEMPOTENT: ``record_launch``/``record_funding_source`` dedupe on
    (creator_wallet, mint) / (funding_source_wallet, creator_wallet, mint)
    respectively -- replaying the same LaunchEvent twice (duplicate delivery,
    out-of-order redelivery) never inflates a count or flips a bool.
    """

    def __init__(self) -> None:
        self._by_wallet: dict[str, list[_LaunchSighting]] = {}
        self._wallet_mints_seen: dict[str, set[str]] = {}
        self._by_funding_source: dict[str, list[_FundingSourceSighting]] = {}
        self._funding_source_keys_seen: dict[str, set[tuple[str, str]]] = {}

    def record_launch(self, *, creator_wallet: str, mint: str, event_slot: int) -> None:
        _check_str("creator_wallet", creator_wallet)
        _check_str("mint", mint)
        _check_int("event_slot", event_slot, min_value=0)

        seen = self._wallet_mints_seen.setdefault(creator_wallet, set())
        if mint in seen:
            return  # idempotent replay guard
        seen.add(mint)

        bucket = self._by_wallet.setdefault(creator_wallet, [])
        pos = bisect.bisect_left([s.event_slot for s in bucket], event_slot)
        bucket.insert(pos, _LaunchSighting(mint=mint, event_slot=event_slot))

    def record_funding_source(
        self, *, funding_source_wallet: str, creator_wallet: str, mint: str, event_slot: int
    ) -> None:
        _check_str("funding_source_wallet", funding_source_wallet)
        _check_str("creator_wallet", creator_wallet)
        _check_str("mint", mint)
        _check_int("event_slot", event_slot, min_value=0)

        seen = self._funding_source_keys_seen.setdefault(funding_source_wallet, set())
        key = (creator_wallet, mint)
        if key in seen:
            return  # idempotent replay guard
        seen.add(key)

        bucket = self._by_funding_source.setdefault(funding_source_wallet, [])
        pos = bisect.bisect_left([s.event_slot for s in bucket], event_slot)
        bucket.insert(
            pos,
            _FundingSourceSighting(creator_wallet=creator_wallet, mint=mint, event_slot=event_slot),
        )

    def prior_launch_count(self, *, creator_wallet: str, as_of_event_slot: int) -> int:
        bucket = self._by_wallet.get(creator_wallet, [])
        idx = bisect.bisect_left(bucket, as_of_event_slot, key=lambda s: s.event_slot)
        return idx  # count of sightings strictly before as_of_event_slot

    def funding_source_seen_before(
        self, *, funding_source_wallet: str, creator_wallet: str, as_of_event_slot: int
    ) -> bool:
        bucket = self._by_funding_source.get(funding_source_wallet, [])
        idx = bisect.bisect_left(bucket, as_of_event_slot, key=lambda s: s.event_slot)
        # True iff a DIFFERENT wallet was funded by this source strictly before now.
        return any(sighting.creator_wallet != creator_wallet for sighting in bucket[:idx])


# ---------------------------------------------------------------------------
# FundingLineageProvider -- ties the RPC decode + registry into one tuple.
# ---------------------------------------------------------------------------


@dataclass
class FundingLineageStats:
    """Runtime counters -- proves "refuse" is never silent (observability)."""

    lookups: int = 0
    funding_source_found: int = 0
    funding_source_undecodable_age: int = 0  # dev_funding_age itself was UNDECODABLE
    funding_source_undecodable_tx_fetch: int = 0  # re-fetch of the funding tx failed
    funding_source_undecodable_ambiguous: int = 0  # debited-source heuristic was ambiguous
    funding_source_seen_before_true: int = 0


class FundingLineageProvider:
    """Off-hot-path producer: creator wallet -> raw funding-lineage tuple.

    Composes ``RpcFundingHistoryProvider`` (dev_funding_age.py, reused
    as-is) for the funding-SOURCE decode with a ``FundingLineageRegistry``
    for the "our own bus" prior-launch-count / cross-wallet-seen-before
    facts.  Call ``lookup()`` once per observed LaunchEvent -- the registry
    accumulates state as a side effect of that call, so no separate
    subscription/backfill step is required.
    """

    def __init__(
        self,
        transport: SolanaHistoryTransport,
        registry: FundingLineageRegistry,
        *,
        page_size: int = 1_000,
        max_pages: int = 10,
        max_transactions_decoded: int = 50,
    ) -> None:
        self._transport = transport
        self._registry = registry
        self._age_provider = RpcFundingHistoryProvider(
            transport,
            page_size=page_size,
            max_pages=max_pages,
            max_transactions_decoded=max_transactions_decoded,
        )
        self.stats = FundingLineageStats()

    def lookup(
        self,
        *,
        creator_wallet: str,
        mint: str,
        deploy_event_slot: int,
        deploy_block_time_ms: int,
    ) -> FundingLineageResult:
        _check_str("creator_wallet", creator_wallet)
        _check_str("mint", mint)
        _check_int("deploy_event_slot", deploy_event_slot, min_value=0)
        _check_int("deploy_block_time_ms", deploy_block_time_ms, min_value=1)

        ingest_time_ms = _now_ms()
        self.stats.lookups += 1

        # 1. prior_launch_count -- query BEFORE recording this launch, and the
        #    registry's own strict `<` comparison independently guarantees
        #    this launch can never count itself (see module docstring).
        prior_launch_count = self._registry.prior_launch_count(
            creator_wallet=creator_wallet, as_of_event_slot=deploy_event_slot
        )

        # 2. funding_source_wallet -- reuse dev_funding_age's tested walk.
        funding_source_wallet: str | None = None
        funding_source_status = FundingSourceStatus.UNDECODABLE

        age_result = self._age_provider.lookup(
            creator_wallet,
            deploy_event_slot=deploy_event_slot,
            deploy_block_time_ms=deploy_block_time_ms,
        )
        if age_result.status is FundingAgeStatus.FOUND:
            assert age_result.first_funding_signature is not None
            try:
                tx = self._transport.get_transaction(age_result.first_funding_signature)
            except Exception as exc:  # noqa: BLE001 -- any transport failure => refuse
                logger.warning(
                    "FundingLineageProvider: RPC error re-fetching funding tx %s for %s "
                    "(%s: %s) -- refusing funding_source_wallet.",
                    age_result.first_funding_signature,
                    creator_wallet,
                    type(exc).__name__,
                    exc,
                )
                tx = None
                self.stats.funding_source_undecodable_tx_fetch += 1

            if tx is not None and tx.err is None:
                source = _identify_funding_source(tx, creator_wallet)
                if source is not None:
                    funding_source_wallet = source
                    funding_source_status = FundingSourceStatus.FOUND
                    self.stats.funding_source_found += 1
                else:
                    self.stats.funding_source_undecodable_ambiguous += 1
            elif tx is not None:
                # tx.err is not None -- the re-fetched tx failed on-chain; refuse.
                self.stats.funding_source_undecodable_ambiguous += 1
        else:
            self.stats.funding_source_undecodable_age += 1

        # 3. funding_source_seen_before -- query BEFORE recording (same
        #    self-reference guard as step 1).
        funding_source_seen_before = False
        if funding_source_wallet is not None:
            funding_source_seen_before = self._registry.funding_source_seen_before(
                funding_source_wallet=funding_source_wallet,
                creator_wallet=creator_wallet,
                as_of_event_slot=deploy_event_slot,
            )
            if funding_source_seen_before:
                self.stats.funding_source_seen_before_true += 1

        # 4. Record this launch (and funding lineage, if resolved) into the
        #    registry AFTER computing the tuple -- "our own bus" accumulates.
        self._registry.record_launch(
            creator_wallet=creator_wallet, mint=mint, event_slot=deploy_event_slot
        )
        if funding_source_wallet is not None:
            self._registry.record_funding_source(
                funding_source_wallet=funding_source_wallet,
                creator_wallet=creator_wallet,
                mint=mint,
                event_slot=deploy_event_slot,
            )

        return FundingLineageResult(
            creator_wallet=creator_wallet,
            deploy_event_slot=deploy_event_slot,
            deploy_block_time_ms=deploy_block_time_ms,
            prior_launch_count=prior_launch_count,
            funding_source_wallet=funding_source_wallet,
            funding_source_seen_before=funding_source_seen_before,
            funding_source_status=funding_source_status,
            ingest_time_ms=ingest_time_ms,
            data_staleness_ms=max(0, ingest_time_ms - deploy_block_time_ms),
        )
