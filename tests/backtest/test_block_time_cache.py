"""Tests for the block_time PERF layer (persistent cache + bounded-concurrency resolution).

The whole point of this layer is speed WITHOUT changing a single resolved value or the
point-in-time / fail-closed semantics. So every test pins one of those invariants:

  (a) CACHE HIT returns WITHOUT an RPC call (spy the client) — warm re-runs are near-instant.
  (b) CACHE MISS resolves via the inner resolver and WRITES BACK (second call is a hit).
  (c) PARALLEL resolution yields the SAME {signature->block_time} map as a sequential loop
      (and is invariant to the concurrency level) — determinism regardless of in-flight count.
  (d) 429 / rate-limit -> BACKOFF + RETRY (deterministic delay sequence), then resolves; an
      always-throttled signature exhausts retries and is CENSORED (never fabricated).
  (e) FAIL-CLOSED: a genuinely unresolvable signature is OMITTED (not retried) and the
      PrefetchedBlockTimeResolver CENSORS it — identical to the sequential fail-closed path.

Plus: disk persistence + zero-RPC warm re-run, and an END-TO-END proof that the prefetched
path produces a byte-identical `run_edge_proof` scoreboard vs the direct sequential fixture
resolver (values + verdict UNCHANGED).

Offline / injectable / deterministic. No live network, no keypair, no capital.
"""

from __future__ import annotations

import json
import threading
import time
from decimal import Decimal

import pytest

from aats.backtest.block_time_cache import (
    BlockTimeCache,
    CachingBlockTimeResolver,
    PrefetchedBlockTimeResolver,
    prefetch_from_corpus,
    resolve_block_times,
)
from aats.backtest.outcome_harness import (
    BlockTime,
    BlockTimeUnavailable,
    FixtureBlockTimeResolver,
    RateLimitedError,
)
from aats.backtest.run_edge_proof import run_edge_proof

# ---------------------------------------------------------------------------
# Test doubles (no live network) — a spy resolver, a flaky rate-limited resolver
# ---------------------------------------------------------------------------


class SpyResolver:
    """A `BlockTimeResolver` that records every call and resolves from a fixed map.

    A signature absent from the map raises `BlockTimeUnavailable` (a genuine miss). `latency_s`
    lets a test make each call take real wall-time so the concurrency speedup is measurable.
    """

    def __init__(self, mapping: dict[str, BlockTime], *, latency_s: float = 0.0) -> None:
        self._mapping = dict(mapping)
        self._latency = latency_s
        self._lock = threading.Lock()
        self.calls: list[str] = []

    def resolve(self, signature: str) -> BlockTime:
        with self._lock:
            self.calls.append(signature)
        if self._latency:
            time.sleep(self._latency)  # OUTSIDE the lock — threads run concurrently
        block_time = self._mapping.get(signature)
        if block_time is None:
            raise BlockTimeUnavailable(f"no fixture for {signature} (CENSORED)")
        return block_time

    @property
    def call_count(self) -> int:
        return len(self.calls)


class FlakyRateLimitedResolver:
    """Raises `RateLimitedError` the first `fail_times` calls PER signature, then resolves."""

    def __init__(self, mapping: dict[str, BlockTime], *, fail_times: int) -> None:
        self._mapping = dict(mapping)
        self._fail_times = fail_times
        self._counts: dict[str, int] = {}
        self._lock = threading.Lock()
        self.calls: list[str] = []

    def resolve(self, signature: str) -> BlockTime:
        with self._lock:
            self.calls.append(signature)
            seen = self._counts.get(signature, 0)
            self._counts[signature] = seen + 1
        if seen < self._fail_times:
            raise RateLimitedError(f"throttled {signature} attempt {seen}")
        block_time = self._mapping.get(signature)
        if block_time is None:
            raise BlockTimeUnavailable(f"no fixture for {signature} (CENSORED)")
        return block_time


def _bt(i: int) -> BlockTime:
    return BlockTime(slot=300_000_000 + i, block_time_ms=1_700_000_000_000 + i)


def _noop_sleep(_delay: float) -> None:
    return None


# ---------------------------------------------------------------------------
# (a) CACHE HIT — no RPC call
# ---------------------------------------------------------------------------


def test_cache_hit_returns_without_rpc_call(tmp_path) -> None:
    cache = BlockTimeCache(tmp_path / "c.json")
    memo = _bt(1)
    cache.put("sigA", memo)
    # The inner resolver would return a DIFFERENT value — proving the memo (not the inner
    # resolver) is what the caller gets, and that the inner resolver is never touched.
    spy = SpyResolver({"sigA": _bt(999)})
    resolver = CachingBlockTimeResolver(spy, cache)

    got = resolver.resolve("sigA")

    assert got == memo
    assert spy.call_count == 0


# ---------------------------------------------------------------------------
# (b) CACHE MISS — resolve + write back; the next call is a hit
# ---------------------------------------------------------------------------


def test_cache_miss_resolves_and_writes_back(tmp_path) -> None:
    cache = BlockTimeCache(tmp_path / "c.json")
    memo = _bt(5)
    spy = SpyResolver({"sigB": memo})
    resolver = CachingBlockTimeResolver(spy, cache)

    first = resolver.resolve("sigB")
    assert first == memo
    assert spy.call_count == 1
    assert cache.get("sigB") == memo  # written back

    # Second call is served from the cache — no new RPC.
    second = resolver.resolve("sigB")
    assert second == memo
    assert spy.call_count == 1


def test_cache_persists_and_warm_rerun_is_zero_rpc(tmp_path) -> None:
    """A cold run resolves + persists; a fresh cache loaded from disk does ZERO RPC calls."""
    path = tmp_path / "cache.json"
    memo = _bt(7)

    cold_spy = SpyResolver({"sigW": memo})
    cold_cache = BlockTimeCache(path)
    resolve_block_times(["sigW"], cold_spy, cache=cold_cache, sleep=_noop_sleep)
    assert cold_spy.call_count == 1
    assert path.exists()  # persisted at end

    # New "process": a fresh cache loads the memo from disk; the inner resolver is never called.
    warm_spy = SpyResolver({"sigW": memo})
    warm_cache = BlockTimeCache(path)
    resolved = resolve_block_times(["sigW"], warm_spy, cache=warm_cache, sleep=_noop_sleep)
    assert resolved == {"sigW": memo}
    assert warm_spy.call_count == 0


def test_malformed_cache_file_is_ignored_not_trusted(tmp_path) -> None:
    path = tmp_path / "cache.json"
    path.write_text("{not valid json", encoding="utf-8")
    cache = BlockTimeCache(path)  # autoload must not raise
    assert len(cache) == 0
    memo = _bt(3)
    spy = SpyResolver({"sig": memo})
    resolved = resolve_block_times(["sig"], spy, cache=cache, sleep=_noop_sleep)
    assert resolved == {"sig": memo}  # re-resolved cleanly


# ---------------------------------------------------------------------------
# (c) PARALLEL == SEQUENTIAL and invariant to concurrency (determinism)
# ---------------------------------------------------------------------------


def test_parallel_map_equals_sequential_and_is_concurrency_invariant() -> None:
    mapping = {f"sig{i}": _bt(i) for i in range(200)}
    signatures = list(mapping)

    seq_spy = SpyResolver(mapping)
    sequential = {sig: seq_spy.resolve(sig) for sig in signatures}

    parallel_16 = resolve_block_times(
        signatures, SpyResolver(mapping), max_in_flight=16, sleep=_noop_sleep
    )
    parallel_1 = resolve_block_times(
        signatures, SpyResolver(mapping), max_in_flight=1, sleep=_noop_sleep
    )
    parallel_8 = resolve_block_times(
        signatures, SpyResolver(mapping), max_in_flight=8, sleep=_noop_sleep
    )

    assert parallel_16 == sequential
    assert parallel_1 == sequential
    assert parallel_8 == sequential


def test_duplicate_signatures_resolved_once(tmp_path) -> None:
    mapping = {"dup": _bt(1)}
    spy = SpyResolver(mapping)
    resolved = resolve_block_times(["dup", "dup", "dup"], spy, sleep=_noop_sleep)
    assert resolved == {"dup": _bt(1)}
    assert spy.call_count == 1  # de-duped: one lookup for an immutable value


def test_parallel_is_faster_than_sequential() -> None:
    """MEASURED perf: bounded-concurrency resolution beats a serial loop under real latency."""
    n = 32
    latency = 0.02  # 20 ms/call
    mapping = {f"s{i}": _bt(i) for i in range(n)}
    signatures = list(mapping)

    seq_spy = SpyResolver(mapping, latency_s=latency)
    t0 = time.perf_counter()
    for sig in signatures:
        seq_spy.resolve(sig)
    sequential_elapsed = time.perf_counter() - t0

    par_spy = SpyResolver(mapping, latency_s=latency)
    t0 = time.perf_counter()
    resolve_block_times(signatures, par_spy, max_in_flight=16, sleep=_noop_sleep)
    parallel_elapsed = time.perf_counter() - t0

    # 32 x 20ms serial ~= 0.64s; with 16 workers ~= 0.04s. Assert a large, robust margin.
    assert parallel_elapsed < sequential_elapsed * 0.5


# ---------------------------------------------------------------------------
# (d) 429 / rate-limit BACKOFF + RETRY
# ---------------------------------------------------------------------------


def test_rate_limit_backoff_retries_then_resolves() -> None:
    memo = _bt(9)
    flaky = FlakyRateLimitedResolver({"sigR": memo}, fail_times=2)
    delays: list[float] = []

    resolved = resolve_block_times(
        ["sigR"],
        flaky,
        sleep=delays.append,
        base_delay_s=0.5,
        max_delay_s=8.0,
        max_retries=6,
    )

    assert resolved == {"sigR": memo}
    # Two throttles -> two backoffs with the deterministic exponential schedule base*2**attempt.
    assert delays == [0.5, 1.0]
    assert flaky.calls.count("sigR") == 3  # 2 throttled + 1 success


def test_rate_limit_backoff_is_capped_at_max_delay() -> None:
    memo = _bt(2)
    flaky = FlakyRateLimitedResolver({"sigC": memo}, fail_times=5)
    delays: list[float] = []
    resolved = resolve_block_times(
        ["sigC"], flaky, sleep=delays.append, base_delay_s=1.0, max_delay_s=4.0, max_retries=6
    )
    assert resolved == {"sigC": memo}
    # 1, 2, 4, 4, 4 — exponential then capped at max_delay.
    assert delays == [1.0, 2.0, 4.0, 4.0, 4.0]


def test_rate_limit_exhausts_retries_then_censored() -> None:
    """An always-throttled signature gives up after max_retries and is CENSORED (never faked)."""
    flaky = FlakyRateLimitedResolver({"sigX": _bt(1)}, fail_times=999)
    resolved = resolve_block_times(
        ["sigX"], flaky, sleep=_noop_sleep, max_retries=3
    )
    assert resolved == {}  # omitted -> downstream CENSORED
    assert flaky.calls.count("sigX") == 4  # initial try + 3 retries


# ---------------------------------------------------------------------------
# (e) FAIL-CLOSED — genuine miss is omitted (not retried) and prefetched resolver CENSORS
# ---------------------------------------------------------------------------


def test_unresolvable_is_omitted_and_not_retried() -> None:
    good = _bt(1)
    spy = SpyResolver({"good": good})  # "bad" is missing -> BlockTimeUnavailable (not a throttle)
    resolved = resolve_block_times(["good", "bad"], spy, sleep=_noop_sleep, max_retries=6)
    assert resolved == {"good": good}
    # A non-rate-limit miss is NOT retried — exactly one attempt.
    assert spy.calls.count("bad") == 1


def test_prefetched_resolver_censors_absent_signature() -> None:
    good = _bt(1)
    resolved = {"good": good}
    prefetched = PrefetchedBlockTimeResolver(resolved)
    assert prefetched.resolve("good") == good
    with pytest.raises(BlockTimeUnavailable):
        prefetched.resolve("bad")


# ---------------------------------------------------------------------------
# END-TO-END: prefetched path == sequential fixture path through run_edge_proof
# ---------------------------------------------------------------------------


def _rec_dict(mint, sig, *, sol_amount, v_sol_sol, forward_multiples, v_tokens="1000000000"):
    entry_price = Decimal(v_sol_sol) / Decimal(v_tokens)
    forward = []
    for horizon, mult in zip((60, 300, 900), forward_multiples, strict=True):
        price = None if mult is None else str(entry_price * Decimal(mult))
        forward.append(
            {
                "price_sol": price,
                "liquidity_usd": None,
                "dex": "pumpfun",
                "note": "ok",
                "horizon_s": horizon,
                "obs_wall_ms": 1_700_000_000_000 + horizon * 1000,
            }
        )
    entry = {
        "mint": mint,
        "signature": sig,
        "traderPublicKey": "T",
        "initialBuy": 1_000_000.0,
        "solAmount": float(sol_amount),
        "vTokensInBondingCurve": float(v_tokens),
        "vSolInBondingCurve": float(v_sol_sol),
        "marketCapSol": float(v_sol_sol),
        "name": "n",
        "symbol": "s",
        "pool": "pump",
        "recv_wall_ms": 1_700_000_000_000,
    }
    return {"entry": entry, "forward": forward}


def _write_corpus(path, records: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")


def _fixture_resolver(records: list[dict], *, include_missing: bool = False) -> FixtureBlockTimeResolver:
    mapping = {}
    for i, r in enumerate(records):
        sig = r["entry"]["signature"]
        if include_missing and sig == "UNRESOLVABLE":
            continue  # deliberately leave one signature unmapped -> both paths CENSOR it
        mapping[sig] = BlockTime(slot=300_000_000 + i, block_time_ms=1_700_000_100_000 + i)
    return FixtureBlockTimeResolver(mapping)


def test_prefetch_matches_sequential_through_run_edge_proof(tmp_path) -> None:
    records = [
        _rec_dict(f"WIN{i}", f"win{i}", sol_amount="0.06", v_sol_sol="32",
                  forward_multiples=["1", "2", "3"])
        for i in range(12)
    ] + [
        _rec_dict(f"LOSE{i}", f"lose{i}", sol_amount="0.5", v_sol_sol="30",
                  forward_multiples=["1", "1", "1"])
        for i in range(12)
    ]
    corpus = tmp_path / "corpus.jsonl"
    _write_corpus(corpus, records)

    # SEQUENTIAL: the direct fixture resolver (today's path).
    seq_code, seq_scoreboard = run_edge_proof(
        corpus, block_time_resolver=_fixture_resolver(records)
    )

    # CONCURRENT + DISK-CACHED prefetch over the SAME fixture -> a PrefetchedBlockTimeResolver.
    cache = BlockTimeCache(tmp_path / "cache.json")
    prefetched = prefetch_from_corpus(
        corpus, _fixture_resolver(records), cache=cache, sleep=_noop_sleep
    )
    par_code, par_scoreboard = run_edge_proof(corpus, block_time_resolver=prefetched)

    assert par_code == seq_code
    assert par_scoreboard == seq_scoreboard  # identical resolved values + verdict


def test_prefetch_censors_unresolvable_identically(tmp_path) -> None:
    records = [
        _rec_dict("M1", "s1", sol_amount="0.06", v_sol_sol="32", forward_multiples=["1", "2", "3"]),
        _rec_dict("M2", "UNRESOLVABLE", sol_amount="0.5", v_sol_sol="30",
                  forward_multiples=["1", None, None]),
    ]
    corpus = tmp_path / "corpus.jsonl"
    _write_corpus(corpus, records)

    seq_code, seq_sb = run_edge_proof(
        corpus, block_time_resolver=_fixture_resolver(records, include_missing=True)
    )
    prefetched = prefetch_from_corpus(
        corpus, _fixture_resolver(records, include_missing=True), sleep=_noop_sleep
    )
    par_code, par_sb = run_edge_proof(corpus, block_time_resolver=prefetched)

    assert seq_sb["n_censored"] == 1
    assert par_sb["n_censored"] == seq_sb["n_censored"]
    assert par_sb["n_resolved"] == seq_sb["n_resolved"]
    assert par_code == seq_code
