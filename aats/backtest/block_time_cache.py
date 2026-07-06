"""Persistent + concurrent block_time resolution — a PERF layer over the RPC resolver.

WHY (perf ONLY — the resolved values and point-in-time semantics are UNCHANGED)
==============================================================================
`build_from_corpus` / `build_momentum_from_corpus` resolve each corpus record's on-chain
block_time by calling `BlockTimeResolver.resolve(signature)` SEQUENTIALLY.  On a 3000-record
corpus that is ~3000 serial getTransaction round-trips (~50 min) and times out.  Nothing about
the value is slow — it is the network latency, serialised.

This module keeps EVERY resolved value byte-identical to the sequential path and only removes
the wall-clock waiting:

  * `BlockTimeCache` — a disk memo keyed by tx SIGNATURE -> {block_time_ms, slot}.  A confirmed
    transaction's block_time is IMMUTABLE, so a per-signature memo is always valid; a warm
    re-run (mostly-cached corpus) is near-instant.  It is a PURE memo of getTransaction — it
    never fabricates, interpolates, or wall-clock-anchors a value (T-300a).
  * `CachingBlockTimeResolver` — a cache-first `.resolve()` wrapper over any inner resolver:
    hit -> return the memo with NO RPC call; miss -> delegate, then write the result back.
  * `resolve_block_times` — resolve a batch of signatures with BOUNDED concurrency (a thread
    pool, default 16 in-flight) and 429/rate-limit backoff+retry.  The result is a
    `{signature: BlockTime}` map that is DETERMINISTIC regardless of concurrency (it is keyed by
    signature; completion order cannot change it).  A signature that is genuinely unresolvable
    (a non-rate-limit `BlockTimeUnavailable`) is OMITTED from the map — never fabricated.
  * `PrefetchedBlockTimeResolver` — a resolver backed by that pre-resolved map (O(1), no
    network).  A signature ABSENT from the map raises `BlockTimeUnavailable`, so the harness
    CENSORS it in exactly the same place, with the same count, as the sequential fail-closed
    path.  This is the drop-in the harness loop consumes unchanged.

FAIL-CLOSED (unchanged): a rate-limit is retried with backoff; a genuine miss is CENSORED; a
value is NEVER fabricated or interpolated.

OFFLINE / INJECTABLE: the inner resolver, the cache path, and the `sleep` used for backoff are
all injected, so the tests drive every path (cache hit/miss, parallel==sequential, 429 retry,
fail-closed) with fixtures and NO live network.  The RPC URL still comes from the env only and
is never logged; the cache stores only signatures + on-chain (slot, block_time_ms) — no secrets.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Protocol

from aats.backtest.outcome_harness import (
    BlockTime,
    BlockTimeResolver,
    BlockTimeUnavailable,
    RateLimitedError,
    read_corpus,
)

# Bounded-concurrency + backoff defaults (all overridable per call for tests / operators).
DEFAULT_MAX_IN_FLIGHT = 16
DEFAULT_MAX_RETRIES = 6
DEFAULT_BASE_DELAY_S = 0.5
DEFAULT_MAX_DELAY_S = 8.0

# On-disk cache format version (bump only on a breaking schema change).
_CACHE_VERSION = 1


class BlockTimeCacheProtocol(Protocol):
    """The memo interface the caching resolver depends on (so tests can inject a fake)."""

    def get(self, signature: str) -> BlockTime | None: ...

    def put(self, signature: str, block_time: BlockTime) -> None: ...

    def flush(self) -> None: ...


# ---------------------------------------------------------------------------
# The persistent signature -> BlockTime memo
# ---------------------------------------------------------------------------


class BlockTimeCache:
    """A disk-backed, thread-safe memo of `signature -> BlockTime` (getTransaction results).

    Load-at-start (`autoload`), persist at end / on demand (`flush`).  The write is ATOMIC
    (temp file + os.replace) so a crash mid-flush never corrupts the cache.  A malformed cache
    entry is skipped on load (never trusted, never fabricated).  `path=None` yields a purely
    in-memory cache (useful in tests / when no persistence is wanted).
    """

    def __init__(self, path: str | Path | None, *, autoload: bool = True) -> None:
        self._path = Path(path) if path is not None else None
        self._lock = threading.Lock()
        self._map: dict[str, BlockTime] = {}
        self._dirty = False
        if autoload:
            self.load()

    @property
    def path(self) -> Path | None:
        return self._path

    def __len__(self) -> int:
        with self._lock:
            return len(self._map)

    def __contains__(self, signature: str) -> bool:
        with self._lock:
            return signature in self._map

    def load(self) -> None:
        """Populate the in-memory memo from disk (no-op if the file is absent)."""
        if self._path is None or not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            # A corrupt/unreadable cache is a PERF artifact, not a source of truth: ignore it
            # and re-resolve. Never trust a malformed memo.
            return
        entries = raw.get("entries", {}) if isinstance(raw, dict) else {}
        loaded: dict[str, BlockTime] = {}
        for sig, val in entries.items():
            try:
                loaded[sig] = BlockTime(
                    slot=int(val["slot"]), block_time_ms=int(val["block_time_ms"])
                )
            except (KeyError, TypeError, ValueError):
                continue  # skip a malformed entry — never fabricate a value
        with self._lock:
            self._map.update(loaded)

    def get(self, signature: str) -> BlockTime | None:
        with self._lock:
            return self._map.get(signature)

    def put(self, signature: str, block_time: BlockTime) -> None:
        with self._lock:
            if self._map.get(signature) != block_time:
                self._map[signature] = block_time
                self._dirty = True

    def flush(self) -> None:
        """Atomically persist the memo to disk if it changed since the last flush."""
        if self._path is None:
            return
        with self._lock:
            if not self._dirty:
                return
            snapshot = {
                sig: {"block_time_ms": bt.block_time_ms, "slot": bt.slot}
                for sig, bt in sorted(self._map.items())
            }
        payload = {"version": _CACHE_VERSION, "entries": snapshot}
        data = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(data, encoding="utf-8")
        os.replace(tmp, self._path)  # atomic on Windows + POSIX
        with self._lock:
            self._dirty = False


# ---------------------------------------------------------------------------
# Cache-first resolver wrapper + prefetched (map-backed) resolver
# ---------------------------------------------------------------------------


class CachingBlockTimeResolver:
    """Cache-first `.resolve()` wrapper over any inner `BlockTimeResolver`.

    Hit  -> return the memo, NO inner RPC call.
    Miss -> delegate to the inner resolver, write the result back, return it.

    A `RateLimitedError` from the inner resolver propagates UNCACHED (a throttle is not a value);
    the concurrent layer retries it.  A genuine `BlockTimeUnavailable` also propagates uncached
    (CENSORED).  Only real, immutable getTransaction results are memoised.
    """

    def __init__(self, inner: BlockTimeResolver, cache: BlockTimeCacheProtocol) -> None:
        self._inner = inner
        self._cache = cache

    def resolve(self, signature: str) -> BlockTime:
        cached = self._cache.get(signature)
        if cached is not None:
            return cached
        block_time = self._inner.resolve(signature)  # may raise (Rate)BlockTimeUnavailable
        self._cache.put(signature, block_time)
        return block_time


class PrefetchedBlockTimeResolver:
    """A `BlockTimeResolver` backed by a pre-resolved `{signature: BlockTime}` map (O(1)).

    A signature ABSENT from the map raises `BlockTimeUnavailable`, so the harness CENSORS it in
    the same place, with the same count, as the sequential fail-closed path — identical
    semantics, zero network in the harness loop.
    """

    def __init__(self, resolved: dict[str, BlockTime]) -> None:
        self._resolved = dict(resolved)

    def __len__(self) -> int:
        return len(self._resolved)

    def resolve(self, signature: str) -> BlockTime:
        block_time = self._resolved.get(signature)
        if block_time is None:
            raise BlockTimeUnavailable(
                f"no prefetched block_time for {signature[:12]}... (CENSORED, fail-closed)"
            )
        return block_time


# ---------------------------------------------------------------------------
# Bounded-concurrency batch resolution with 429/rate-limit backoff+retry
# ---------------------------------------------------------------------------


def _resolve_one_with_backoff(
    resolver: BlockTimeResolver,
    signature: str,
    *,
    max_retries: int,
    base_delay_s: float,
    max_delay_s: float,
    sleep: Callable[[float], None],
) -> BlockTime:
    """Resolve one signature, retrying ONLY a `RateLimitedError` with exponential backoff.

    A non-rate-limit `BlockTimeUnavailable` is NOT retried — it propagates immediately so the
    caller CENSORS it (fail-closed).  Backoff delay = min(base * 2**attempt, max) — deterministic
    (no jitter), so a test can assert the exact sleep sequence.  On the final rate-limited attempt
    the `RateLimitedError` propagates and is treated as unresolvable (CENSORED), never fabricated.
    """
    attempt = 0
    while True:
        try:
            return resolver.resolve(signature)
        except RateLimitedError:
            if attempt >= max_retries:
                raise  # exhausted retries -> unresolvable this run (CENSORED), never a fake value
            sleep(min(base_delay_s * (2**attempt), max_delay_s))
            attempt += 1


def resolve_block_times(
    signatures: Iterable[str],
    resolver: BlockTimeResolver,
    *,
    cache: BlockTimeCacheProtocol | None = None,
    max_in_flight: int = DEFAULT_MAX_IN_FLIGHT,
    max_retries: int = DEFAULT_MAX_RETRIES,
    base_delay_s: float = DEFAULT_BASE_DELAY_S,
    max_delay_s: float = DEFAULT_MAX_DELAY_S,
    sleep: Callable[[float], None] = time.sleep,
    flush: bool = True,
) -> dict[str, BlockTime]:
    """Resolve `signatures` -> `{signature: BlockTime}` with bounded concurrency + backoff.

    DETERMINISTIC: the returned map is keyed by signature, so completion order (which varies with
    concurrency) cannot change it — a parallel run yields the SAME map a sequential loop would.
    Signatures are de-duplicated (a confirmed block_time is immutable, so a repeat is one lookup).
    When `cache` is supplied the inner resolver is wrapped cache-first, so a warm re-run makes
    zero RPC calls; the cache is flushed once at the end unless `flush=False`.

    A genuinely unresolvable signature (non-rate-limit `BlockTimeUnavailable`, e.g. tx-not-found)
    is OMITTED from the map — the downstream `PrefetchedBlockTimeResolver` then CENSORS its
    record, identical to the sequential fail-closed path.  Nothing is ever fabricated.
    """
    unique = list(dict.fromkeys(signatures))  # de-dupe, preserve first-seen order (cosmetic only)
    if not unique:
        return {}

    effective: BlockTimeResolver = (
        CachingBlockTimeResolver(resolver, cache) if cache is not None else resolver
    )
    workers = max(1, min(max_in_flight, len(unique)))

    def _work(signature: str) -> tuple[str, BlockTime | None]:
        try:
            return signature, _resolve_one_with_backoff(
                effective,
                signature,
                max_retries=max_retries,
                base_delay_s=base_delay_s,
                max_delay_s=max_delay_s,
                sleep=sleep,
            )
        except BlockTimeUnavailable:
            # Unresolvable this run (tx-not-found, or retries exhausted) -> omit -> CENSORED.
            return signature, None

    resolved: dict[str, BlockTime] = {}
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="blocktime") as pool:
        for signature, block_time in pool.map(_work, unique):
            if block_time is not None:
                resolved[signature] = block_time

    if cache is not None and flush:
        cache.flush()
    return resolved


def prefetch_from_corpus(
    corpus_path: str | Path,
    resolver: BlockTimeResolver,
    *,
    cache: BlockTimeCacheProtocol | None = None,
    max_in_flight: int = DEFAULT_MAX_IN_FLIGHT,
    max_retries: int = DEFAULT_MAX_RETRIES,
    base_delay_s: float = DEFAULT_BASE_DELAY_S,
    max_delay_s: float = DEFAULT_MAX_DELAY_S,
    sleep: Callable[[float], None] = time.sleep,
) -> PrefetchedBlockTimeResolver:
    """Read a corpus's signatures, resolve them concurrently (cache-first), and return a
    `PrefetchedBlockTimeResolver` the harness consumes unchanged.

    This is the drop-in that turns the harness's ~3000 serial getTransaction calls into one
    bounded-concurrency, cached batch — with the resolved values and censoring semantics
    identical to the sequential path.
    """
    signatures = [record.signature for record in read_corpus(corpus_path)]
    resolved = resolve_block_times(
        signatures,
        resolver,
        cache=cache,
        max_in_flight=max_in_flight,
        max_retries=max_retries,
        base_delay_s=base_delay_s,
        max_delay_s=max_delay_s,
        sleep=sleep,
    )
    return PrefetchedBlockTimeResolver(resolved)


def default_cache_path() -> str:
    """The persistent cache path: `$BLOCKTIME_CACHE_PATH` or the shadow default (never in-repo)."""
    return os.environ.get("BLOCKTIME_CACHE_PATH", "C:/aats_shadow/blocktime_cache.json")


__all__ = [
    "DEFAULT_MAX_IN_FLIGHT",
    "DEFAULT_MAX_RETRIES",
    "DEFAULT_BASE_DELAY_S",
    "DEFAULT_MAX_DELAY_S",
    "BlockTimeCache",
    "BlockTimeCacheProtocol",
    "CachingBlockTimeResolver",
    "PrefetchedBlockTimeResolver",
    "resolve_block_times",
    "prefetch_from_corpus",
    "default_cache_path",
]
