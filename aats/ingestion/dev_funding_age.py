"""E16 (data-ingestion half) — LEAK-FREE dev-wallet "funded-just-before-launch"
first-funding-age RAW FEATURE.

OWNERSHIP: data-ingestion-engineer (M1).  This module owns the RAW,
POINT-IN-TIME decode of a creator wallet's FIRST observed inbound-SOL funding
transaction, and the trivial event-time-delta arithmetic against the deploy
event (mirrors ``aats.ingestion.deployer_reputation``'s "raw TALLY" boundary —
one subtraction between two already-decoded on-chain anchors is a raw fact,
not a rolling window / ratio / multi-signal score, so it stays in M1).  It
emits an honest, typed ``DevFundingAgeResult`` — REFUSE-BY-DEFAULT
(``FundingAgeStatus.UNDECODABLE``) whenever the funding event cannot be
conclusively decoded.  It NEVER makes a trading decision — the REJECT /
DOWN-WEIGHT verdict is applied downstream by
``aats.risk.dev_funding_age_gate`` (risk-guardrails lane).

WHAT THIS MODULE IS (and is NOT)
---------------------------------
  * A read-side RPC DECODER: ``RpcFundingHistoryProvider.lookup()`` walks a
    creator wallet's transaction-signature history (``getSignaturesForAddress``
    paging) and decodes candidate transactions (``getTransaction``) to find
    the EARLIEST confirmed inbound-SOL-balance-increase for that wallet, with
    slot <= the deploy event's slot.  It performs NO signing, NO submission,
    NO sizing, NO LLM call.
  * NOT a risk decision.  ``DevFundingAgeResult`` carries the raw decoded
    anchors (``first_funding_slot``, ``first_funding_block_time_ms``) and the
    derived event-time deltas (``funding_age_slots``, ``funding_age_ms``) —
    there is no field here that rejects, sizes, or scores anything.
  * NOT a rate.  There is no probability/score/rate field anywhere in this
    module — only raw slot/ms integers and a wallet's own decoded history.

STRICT POINT-IN-TIME (T-300a, the law of this signal)
-------------------------------------------------------
  A candidate funding transaction counts **iff** its on-chain ``slot`` is
  ``<= deploy_event_slot`` (non-strict: a bundle that funds-then-deploys in
  the SAME slot is still a legitimate "funded immediately before deploy"
  fact, unlike ``deployer_reputation``'s outcome-vs-launch relationship,
  which is strictly non-self-referential).  Every RPC page is filtered on
  this condition BEFORE a candidate is buffered; a signature with
  ``slot > deploy_event_slot`` (i.e. wallet activity that happened AFTER this
  launch — entirely possible when this lookup runs well after the fact) is
  discarded and NEVER decoded, let alone allowed to leak into the result.
  ``deploy_event_slot`` / ``deploy_block_time_ms`` are always ON-CHAIN
  anchors (the ``LaunchEvent.event_time`` the caller passes in) — this module
  never reads ``time.time()`` to decide what "counts".  ``ingest_time_ms`` /
  ``data_staleness_ms`` are wall-clock, carried for MONITORING ONLY (mirrors
  ``enrichment.py``'s ``EnrichmentMeta`` convention) — never a join key,
  never part of the ``funding_age_*`` arithmetic.

REFUSE-BY-DEFAULT (unknown != safe — non-waivable)
-----------------------------------------------------
  ``RpcFundingHistoryProvider.lookup()`` returns
  ``FundingAgeStatus.UNDECODABLE`` (never a numeric age) whenever it cannot
  CONCLUSIVELY establish the wallet's first pre-deploy inbound funding:
    * an RPC/transport error at any page,
    * no signature history at all before ``deploy_event_slot`` (the wallet's
      funding is simply not visible to this decoder — this is NOT the same
      as "brand-new, therefore fresh"; we refuse to invent a delta),
    * the transaction-decode/page budget is exhausted before reaching
      genesis (older history may exist that we never saw), or
    * every buffered candidate decodes but none shows a genuine inbound SOL
      balance increase for the wallet (defensive; never silently assumed).
  An ``UNDECODABLE`` result NEVER carries a fabricated ``first_funding_*`` /
  ``funding_age_*`` value (enforced in ``DevFundingAgeResult.__post_init__``)
  — there is no code path that can smuggle a guessed number past this refusal.
  The risk-side consumer (``dev_funding_age_gate.py``) treats UNDECODABLE as
  a de-risk REJECT (same posture as ``pretrade_gate.py``'s
  ``INPUT_INCOMPLETE`` / ``screener.py``'s missing-enrichment-datum EXCLUDE)
  — it is NEVER read as "old, established, safe".

DECODE METHOD — balance-delta, not instruction-byte offsets
--------------------------------------------------------------
  A candidate transaction is confirmed as "inbound funding" by comparing
  ``meta.postBalances[idx] - meta.preBalances[idx]`` for the wallet's own
  account-key index in that transaction (a positive delta = it received
  lamports).  This is the standard, layout-agnostic Solana decode technique —
  it works for a plain System Program ``transfer``, a ``createAccount``, or
  any CPI-based funding path, and it needs NO hard-coded instruction byte
  offset (the charter's "never hard-code an offset you have not confirmed on
  a real signature" caveat does not apply — there is no offset here).

FAST-PATH SAFETY
-----------------
  This module performs RPC I/O — it is EXPLICITLY an enrichment-tier / SLOW
  producer (same posture as ``enrichment.py``'s Birdeye/DexScreener pollers
  and ``deployer_reputation.py``'s Parquet reads), never called from the
  sub-10ms SNIPE hot path.  ``HttpJsonRpcSolanaTransport`` is INJECTABLE
  (``SolanaHistoryTransport`` Protocol) so decode logic is fully offline-
  testable via ``StaticSolanaHistoryTransport`` — no live network at test
  time, mirroring ``enrichment.py``'s ``OfflineFixtureTransport`` and
  ``execution/rpc_client.py``'s ``MockRpcClient`` precedent.

MONEY / SECRETS
-----------------
  ``first_funding_amount_lamports`` is int lamports (never float) — audit
  context only; it plays NO role in the age threshold decision.  No secrets —
  the RPC endpoint is env-configured (``RPC_PRIMARY``, the SAME variable
  ``execution/rpc_client.py`` already uses), never a literal in code.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Type guards (money rule; mirrors insider_dump.py's boundary checks)
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


def _now_ms() -> int:
    return int(time.time() * 1_000)


# ---------------------------------------------------------------------------
# RPC transport surface — injectable, no live network at test time.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SignatureInfo:
    """One entry from ``getSignaturesForAddress`` — the minimal decoded shape.

    ``block_time_ms`` is None when the node has not indexed a block time for
    that slot yet (rare, recent-slot edge case) — treated as un-usable (the
    caller filters it out rather than guessing).
    """

    signature: str
    slot: int
    block_time_ms: int | None
    err: object | None  # non-None => the tx failed on-chain; never a fund source


@dataclass(frozen=True)
class RawTransactionMeta:
    """The minimal decoded shape of a ``getTransaction`` response this module needs.

    ``account_keys`` / ``pre_balances_lamports`` / ``post_balances_lamports``
    are positionally aligned (index i of each list refers to the same
    account) — exactly the shape ``meta.preBalances`` / ``meta.postBalances``
    / ``transaction.message.accountKeys`` (or ``staticAccountKeys`` for a
    versioned tx) take on the wire.
    """

    slot: int
    block_time_ms: int | None
    err: object | None
    account_keys: tuple[str, ...]
    pre_balances_lamports: tuple[int, ...]
    post_balances_lamports: tuple[int, ...]


class SolanaHistoryTransport(Protocol):
    """Injectable read-only Solana JSON-RPC surface (no signing, no submission).

    Both methods MUST raise on a genuine transport/RPC failure (never return a
    silently-empty/garbage result to signal an error) — the caller
    (``RpcFundingHistoryProvider``) treats an exception as REFUSE-BY-DEFAULT
    (``FundingAgeStatus.UNDECODABLE``), never as "no history".
    """

    def get_signatures_for_address(
        self, wallet: str, *, before: str | None, limit: int
    ) -> list[SignatureInfo]: ...  # pragma: no cover

    def get_transaction(self, signature: str) -> RawTransactionMeta | None: ...  # pragma: no cover


# ---------------------------------------------------------------------------
# StaticSolanaHistoryTransport — offline / test / fixture transport.
# ---------------------------------------------------------------------------


class StaticSolanaHistoryTransport:
    """Deterministic, in-memory ``SolanaHistoryTransport`` (no network).

    ``add_signature`` registers one signature (newest-added is NOT assumed to
    be newest-in-time — signatures are always returned sorted descending by
    slot, exactly like the real RPC).  ``add_transaction`` registers the
    decoded body for a signature already added.  ``raise_on_signatures`` /
    ``raise_on_transaction`` simulate a transport failure for a specific
    wallet/signature, for refuse-by-default tests.
    """

    def __init__(self) -> None:
        self._by_wallet: dict[str, list[SignatureInfo]] = {}
        self._transactions: dict[str, RawTransactionMeta] = {}
        self._raise_on_signatures: set[str] = set()
        self._raise_on_transaction: set[str] = set()

    def add_signature(self, wallet: str, sig: SignatureInfo) -> None:
        bucket = self._by_wallet.setdefault(wallet, [])
        bucket.append(sig)
        bucket.sort(key=lambda s: s.slot, reverse=True)

    def add_transaction(self, signature: str, tx: RawTransactionMeta) -> None:
        self._transactions[signature] = tx

    def raise_on_signatures(self, wallet: str) -> None:
        self._raise_on_signatures.add(wallet)

    def raise_on_transaction(self, signature: str) -> None:
        self._raise_on_transaction.add(signature)

    def get_signatures_for_address(
        self, wallet: str, *, before: str | None, limit: int
    ) -> list[SignatureInfo]:
        if wallet in self._raise_on_signatures:
            raise ConnectionError(f"StaticSolanaHistoryTransport: simulated RPC error for {wallet}")
        bucket = self._by_wallet.get(wallet, [])
        if before is None:
            page = bucket
        else:
            idx = next((i for i, s in enumerate(bucket) if s.signature == before), None)
            page = bucket[idx + 1 :] if idx is not None else []
        return list(page[:limit])

    def get_transaction(self, signature: str) -> RawTransactionMeta | None:
        if signature in self._raise_on_transaction:
            raise ConnectionError(
                f"StaticSolanaHistoryTransport: simulated RPC error for signature {signature}"
            )
        return self._transactions.get(signature)


# ---------------------------------------------------------------------------
# HttpJsonRpcSolanaTransport — real, injectable-httpx production backend.
# ---------------------------------------------------------------------------


class HttpJsonRpcSolanaTransport:
    """Production ``SolanaHistoryTransport`` backed by a real Solana JSON-RPC
    endpoint (httpx).  Mirrors ``execution/rpc_client.py``'s
    ``SolanaRpcClient._rpc`` pattern exactly (same env var, same error
    handling shape) — this module does NOT import from ``aats.execution``
    (module-boundary discipline: M1 is read-side only), so the client is
    reimplemented here at the same fidelity.

    Any RPC/network failure raises (never returns a silently-empty result) —
    ``RpcFundingHistoryProvider`` is responsible for converting that into
    ``FundingAgeStatus.UNDECODABLE``.
    """

    def __init__(self, rpc_url: str | None = None, *, timeout_seconds: float = 5.0) -> None:
        self._url = rpc_url or os.environ.get("RPC_PRIMARY", "")
        if not self._url:
            raise ValueError(
                "HttpJsonRpcSolanaTransport: RPC_PRIMARY env var not set. "
                "Provide an rpc_url or set RPC_PRIMARY."
            )
        self._timeout = timeout_seconds
        self._http = None  # lazy-initialized (offline tests never import httpx)

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> HttpJsonRpcSolanaTransport:
        src = env if env is not None else dict(os.environ)
        return cls(rpc_url=src.get("RPC_PRIMARY") or None)

    def _client(self):  # type: ignore[no-untyped-def]
        import httpx

        if self._http is None:
            self._http = httpx.Client(timeout=self._timeout)
        return self._http

    def _rpc(self, method: str, params: list[Any]) -> Any:  # noqa: ANN401
        import httpx

        body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        try:
            r = self._client().post(self._url, json=body)
            r.raise_for_status()
            data = r.json()
        except httpx.HTTPError as exc:
            raise OSError(f"RPC call {method} failed: {exc}") from exc
        if "error" in data:
            raise OSError(f"RPC error {method}: {data['error']}")
        return data.get("result")

    def get_signatures_for_address(
        self, wallet: str, *, before: str | None, limit: int
    ) -> list[SignatureInfo]:
        opts: dict[str, object] = {"limit": limit}
        if before is not None:
            opts["before"] = before
        result = self._rpc("getSignaturesForAddress", [wallet, opts])
        out: list[SignatureInfo] = []
        for row in result or []:
            block_time = row.get("blockTime")
            out.append(
                SignatureInfo(
                    signature=str(row["signature"]),
                    slot=int(row["slot"]),
                    block_time_ms=(int(block_time) * 1_000 if block_time is not None else None),
                    err=row.get("err"),
                )
            )
        return out

    def get_transaction(self, signature: str) -> RawTransactionMeta | None:
        result = self._rpc(
            "getTransaction",
            [signature, {"encoding": "json", "maxSupportedTransactionVersion": 0}],
        )
        if result is None:
            return None
        meta = result.get("meta") or {}
        message = (result.get("transaction") or {}).get("message") or {}
        account_keys = message.get("accountKeys") or message.get("staticAccountKeys") or []
        block_time = result.get("blockTime")
        return RawTransactionMeta(
            slot=int(result["slot"]),
            block_time_ms=(int(block_time) * 1_000 if block_time is not None else None),
            err=meta.get("err"),
            account_keys=tuple(str(k) for k in account_keys),
            pre_balances_lamports=tuple(int(v) for v in meta.get("preBalances") or ()),
            post_balances_lamports=tuple(int(v) for v in meta.get("postBalances") or ()),
        )


# ---------------------------------------------------------------------------
# DevFundingAgeResult — the public, honest, REFUSE-BY-DEFAULT result.
# ---------------------------------------------------------------------------


class FundingAgeStatus(StrEnum):
    FOUND = "found"  # a confirmed pre-deploy inbound-funding tx was decoded
    UNDECODABLE = "undecodable"  # refuse-by-default — see module docstring


@dataclass(frozen=True)
class DevFundingAgeResult:
    """Point-in-time dev-wallet first-funding-age RAW FEATURE.  NEVER a score.

    ``funding_age_slots`` / ``funding_age_ms`` are event-time deltas computed
    ONLY from on-chain anchors (``deploy_event_slot``/``deploy_block_time_ms``
    minus the decoded funding tx's own slot/block_time) — never wall-clock.
    Both are None (never a fabricated 0 or guess) unless
    ``status is FundingAgeStatus.FOUND``.

    ``ingest_time_ms`` / ``data_staleness_ms`` are wall-clock, MONITORING
    ONLY (mirrors ``enrichment.py``'s ``EnrichmentMeta``) — never used in the
    age arithmetic and never read by the risk-side threshold gate.
    """

    creator_wallet: str
    deploy_event_slot: int
    deploy_block_time_ms: int
    status: FundingAgeStatus
    first_funding_slot: int | None = None
    first_funding_block_time_ms: int | None = None
    first_funding_amount_lamports: int | None = None
    first_funding_signature: str | None = None
    funding_age_slots: int | None = None
    funding_age_ms: int | None = None
    ingest_time_ms: int = field(default_factory=_now_ms)
    data_staleness_ms: int = 0

    def __post_init__(self) -> None:
        _check_str("DevFundingAgeResult.creator_wallet", self.creator_wallet)
        _check_int("DevFundingAgeResult.deploy_event_slot", self.deploy_event_slot, min_value=0)
        _check_int(
            "DevFundingAgeResult.deploy_block_time_ms", self.deploy_block_time_ms, min_value=1
        )
        if not isinstance(self.status, FundingAgeStatus):
            raise TypeError("DevFundingAgeResult.status must be a FundingAgeStatus")
        _check_int("DevFundingAgeResult.ingest_time_ms", self.ingest_time_ms, min_value=0)
        _check_int("DevFundingAgeResult.data_staleness_ms", self.data_staleness_ms, min_value=0)

        found_fields = (
            self.first_funding_slot,
            self.first_funding_block_time_ms,
            self.first_funding_signature,
            self.funding_age_slots,
            self.funding_age_ms,
        )
        if self.status is FundingAgeStatus.UNDECODABLE:
            # Refuse-by-default, structurally: an UNDECODABLE result can NEVER
            # smuggle a numeric age through — every derived field must be None.
            if any(v is not None for v in found_fields):
                raise ValueError(
                    "DevFundingAgeResult: status=UNDECODABLE must not carry any "
                    "first_funding_*/funding_age_* value (refuse-by-default; "
                    "unknown != safe, and unknown != a fabricated number)."
                )
            return

        # status is FOUND — every anchor/derived field is required.
        if any(v is None for v in found_fields):
            raise ValueError(
                "DevFundingAgeResult: status=FOUND requires all first_funding_*/"
                "funding_age_* fields to be populated."
            )
        first_slot = _check_int(
            "DevFundingAgeResult.first_funding_slot", self.first_funding_slot, min_value=0
        )
        first_block_time = _check_int(
            "DevFundingAgeResult.first_funding_block_time_ms",
            self.first_funding_block_time_ms,
            min_value=1,
        )
        _check_str("DevFundingAgeResult.first_funding_signature", self.first_funding_signature)
        if self.first_funding_amount_lamports is not None:
            _check_int(
                "DevFundingAgeResult.first_funding_amount_lamports",
                self.first_funding_amount_lamports,
                min_value=1,
            )
        age_slots = _check_int(
            "DevFundingAgeResult.funding_age_slots", self.funding_age_slots, min_value=0
        )
        age_ms = _check_int("DevFundingAgeResult.funding_age_ms", self.funding_age_ms, min_value=0)

        # STRICT point-in-time: the funding tx can never be after the deploy
        # event (slot OR block_time), and the deltas must be internally
        # consistent (defence-in-depth against a caller-constructed mismatch).
        if first_slot > self.deploy_event_slot:
            raise ValueError(
                f"DevFundingAgeResult: first_funding_slot ({first_slot}) must be "
                f"<= deploy_event_slot ({self.deploy_event_slot}) — point-in-time law."
            )
        if first_block_time > self.deploy_block_time_ms:
            raise ValueError(
                f"DevFundingAgeResult: first_funding_block_time_ms ({first_block_time}) must be "
                f"<= deploy_block_time_ms ({self.deploy_block_time_ms}) — point-in-time law."
            )
        if age_slots != self.deploy_event_slot - first_slot:
            raise ValueError("DevFundingAgeResult: funding_age_slots is inconsistent.")
        if age_ms != self.deploy_block_time_ms - first_block_time:
            raise ValueError("DevFundingAgeResult: funding_age_ms is inconsistent.")


# ---------------------------------------------------------------------------
# RpcFundingHistoryProvider — the bounded-budget paginated decoder.
# ---------------------------------------------------------------------------


@dataclass
class DevFundingAgeStats:
    """Runtime counters — proves "refuse" is never silent (observability)."""

    lookups: int = 0
    found: int = 0
    undecodable_rpc_error: int = 0
    undecodable_no_history: int = 0
    undecodable_budget_exhausted: int = 0
    undecodable_no_confirmed_funding: int = 0


class RpcFundingHistoryProvider:
    """Off-hot-path decoder: creator wallet -> first pre-deploy inbound funding.

    Pages ``getSignaturesForAddress`` backward (bounded by ``max_pages`` /
    ``page_size``), filters to ``slot <= deploy_event_slot`` (point-in-time),
    and ONLY reports a result once pagination has reached the wallet's
    genesis (proving the ENTIRE pre-deploy history was seen — see the
    "CORRECTNESS LAW" comment in ``lookup()``: without this, the oldest
    *visible* candidate could be mistaken for the wallet's true first
    funding, when an even-older one exists beyond the page budget).  Once
    genesis is proven, candidates are decoded oldest-first via
    ``getTransaction`` (bounded by ``max_transactions_decoded``) and the
    FIRST one confirmed as a genuine inbound SOL balance increase is the
    answer.  Refuses (never guesses) whenever any budget is exhausted before
    a conclusive answer is reached.
    """

    def __init__(
        self,
        transport: SolanaHistoryTransport,
        *,
        page_size: int = 1_000,
        max_pages: int = 10,
        max_transactions_decoded: int = 50,
    ) -> None:
        self._transport = transport
        self._page_size = _check_int("page_size", page_size, min_value=1)
        self._max_pages = _check_int("max_pages", max_pages, min_value=1)
        self._max_transactions_decoded = _check_int(
            "max_transactions_decoded", max_transactions_decoded, min_value=1
        )
        self.stats = DevFundingAgeStats()

    def _undecodable(
        self, wallet: str, *, deploy_event_slot: int, deploy_block_time_ms: int, ingest_time_ms: int
    ) -> DevFundingAgeResult:
        return DevFundingAgeResult(
            creator_wallet=wallet,
            deploy_event_slot=deploy_event_slot,
            deploy_block_time_ms=deploy_block_time_ms,
            status=FundingAgeStatus.UNDECODABLE,
            ingest_time_ms=ingest_time_ms,
            data_staleness_ms=max(0, ingest_time_ms - deploy_block_time_ms),
        )

    def lookup(
        self, wallet: str, *, deploy_event_slot: int, deploy_block_time_ms: int
    ) -> DevFundingAgeResult:
        _check_str("RpcFundingHistoryProvider.wallet", wallet)
        _check_int("deploy_event_slot", deploy_event_slot, min_value=0)
        _check_int("deploy_block_time_ms", deploy_block_time_ms, min_value=1)

        ingest_time_ms = _now_ms()
        self.stats.lookups += 1

        def undecodable(counter: str) -> DevFundingAgeResult:
            setattr(self.stats, counter, getattr(self.stats, counter) + 1)
            return self._undecodable(
                wallet,
                deploy_event_slot=deploy_event_slot,
                deploy_block_time_ms=deploy_block_time_ms,
                ingest_time_ms=ingest_time_ms,
            )

        cursor: str | None = None
        candidates: list[SignatureInfo] = []
        reached_genesis = False

        for _page_num in range(self._max_pages):
            try:
                page = self._transport.get_signatures_for_address(
                    wallet, before=cursor, limit=self._page_size
                )
            except Exception as exc:  # noqa: BLE001 — any transport failure => refuse
                logger.warning(
                    "RpcFundingHistoryProvider: RPC error paging signatures for %s (%s: %s).",
                    wallet,
                    type(exc).__name__,
                    exc,
                )
                return undecodable("undecodable_rpc_error")

            if not page:
                reached_genesis = True
                break

            qualifying = [
                s
                for s in page
                if s.err is None and s.block_time_ms is not None and s.slot <= deploy_event_slot
            ]
            candidates.extend(qualifying)

            cursor = page[-1].signature
            if len(page) < self._page_size:
                reached_genesis = True
                break

        # CORRECTNESS LAW: we may only report a "first funding" once we have
        # PROVEN we have seen the wallet's ENTIRE pre-deploy signature history
        # (reached genesis — an empty or short-of-page_size final page).  If
        # we stopped paging merely because ``max_pages`` ran out, an
        # even-older inbound-funding tx might exist beyond what we scanned —
        # reporting the oldest-*visible* candidate in that case would risk
        # mistaking a wallet's Nth top-up for its FIRST funding (a false
        # "established" or a false "fresh" verdict).  Refuse instead.
        if not reached_genesis:
            logger.warning(
                "RpcFundingHistoryProvider: max_pages=%d exhausted for %s before reaching "
                "genesis -- refusing (older history may exist, unseen; never guess 'first').",
                self._max_pages,
                wallet,
            )
            return undecodable("undecodable_budget_exhausted")

        if not candidates:
            logger.info(
                "RpcFundingHistoryProvider: no funding history found for %s at/before "
                "slot=%d -- refusing (unknown != safe).",
                wallet,
                deploy_event_slot,
            )
            return undecodable("undecodable_no_history")

        candidates_sorted = sorted(candidates, key=lambda s: s.slot)
        budget = self._max_transactions_decoded
        for sig_info in candidates_sorted:
            if budget <= 0:
                break
            budget -= 1
            try:
                tx = self._transport.get_transaction(sig_info.signature)
            except Exception as exc:  # noqa: BLE001 — try the next candidate, never crash
                logger.warning(
                    "RpcFundingHistoryProvider: RPC error decoding tx %s for %s (%s: %s) -- "
                    "trying next candidate.",
                    sig_info.signature,
                    wallet,
                    type(exc).__name__,
                    exc,
                )
                continue
            if tx is None or tx.err is not None:
                continue
            if tx.slot > deploy_event_slot:
                continue  # defensive re-check of the point-in-time law
            try:
                idx = tx.account_keys.index(wallet)
            except ValueError:
                continue
            if idx >= len(tx.pre_balances_lamports) or idx >= len(tx.post_balances_lamports):
                continue
            delta = tx.post_balances_lamports[idx] - tx.pre_balances_lamports[idx]
            if delta <= 0:
                continue  # not an inbound increase for this wallet

            funding_block_time_ms = (
                tx.block_time_ms if tx.block_time_ms is not None else sig_info.block_time_ms
            )
            if funding_block_time_ms is None or funding_block_time_ms > deploy_block_time_ms:
                continue  # refuse rather than emit a nonsensical/unknown-time age

            self.stats.found += 1
            return DevFundingAgeResult(
                creator_wallet=wallet,
                deploy_event_slot=deploy_event_slot,
                deploy_block_time_ms=deploy_block_time_ms,
                status=FundingAgeStatus.FOUND,
                first_funding_slot=tx.slot,
                first_funding_block_time_ms=funding_block_time_ms,
                first_funding_amount_lamports=delta,
                first_funding_signature=sig_info.signature,
                funding_age_slots=deploy_event_slot - tx.slot,
                funding_age_ms=deploy_block_time_ms - funding_block_time_ms,
                ingest_time_ms=ingest_time_ms,
                data_staleness_ms=max(0, ingest_time_ms - deploy_block_time_ms),
            )

        logger.info(
            "RpcFundingHistoryProvider: %d candidate(s) decoded for %s but none confirmed an "
            "inbound SOL balance increase -- refusing (unknown != safe).",
            len(candidates_sorted),
            wallet,
        )
        return undecodable("undecodable_no_confirmed_funding")
