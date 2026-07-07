"""BACKTEST-QA adversarial audit of the block_time PERF layer (cache + concurrency).

Owner: backtest-qa-engineer.  This file is the SKEPTIC's counterpart to the author's
`test_block_time_cache.py`.  The author proved the happy paths; here I try to BREAK the
point-in-time contract and prove the five non-negotiables the perf fix must never violate:

  I.   CACHED VALUE == LIVE getTransaction value — the memo is a pure, byte-identical
       function of the immutable signature (no drift, no re-derivation).  A cache HIT
       returns exactly what a MISS would have resolved.
  II.  NO WALL-CLOCK ever substituted — the cache stores ONLY {slot, block_time_ms} from
       getTransaction; `recv_wall_ms` never leaks into the anchor, even after a disk
       round-trip; the persisted JSON contains no wall-clock field.
  III. FAIL-CLOSED preserved — an unresolvable signature is OMITTED from the map and the
       record is CENSORED, identically to the sequential path, through BOTH strategies
       (launch AND the active momentum strategy).
  IV.  LEAK BOUNDARY untouched — the decision/outcome anchor per mint is byte-identical
       whether resolved sequentially, concurrently, or from a warm disk cache.
  V.   ORDER-INDEPENDENT / DETERMINISTIC — the resolved map AND its censored complement are
       invariant to the concurrency level and to a persistent disk cache round-trip.

Plus a direct hunt for a STALE-CACHE or RACE that yields a wrong anchor (thread-safety
stress, poison-resolver warm re-run, wall-clock corpus).

Offline / injectable / deterministic.  No live network, no keypair, no capital.
"""

from __future__ import annotations

import json
import threading
from decimal import Decimal

import pytest

from aats.backtest.block_time_cache import (
    BlockTimeCache,
    CachingBlockTimeResolver,
    prefetch_from_corpus,
    resolve_block_times,
)
from aats.backtest.momentum_harness import build_momentum_from_corpus
from aats.backtest.outcome_harness import (
    BlockTime,
    BlockTimeUnavailable,
    FixtureBlockTimeResolver,
    RateLimitedError,
    build_from_corpus,
    read_corpus,
    to_entry_record,
)
from aats.backtest.run_edge_proof import (
    EXIT_FAIL_CLOSED,
    run_edge_proof,
)

# ---------------------------------------------------------------------------
# Test doubles (no live network)
# ---------------------------------------------------------------------------


class RecordingResolver:
    """Resolves from a fixed map and records EXACTLY what it returned per signature.

    Lets a test assert the cache stored the SAME object the inner resolver produced (memo
    identity), and count RPC touches.  Thread-safe.
    """

    def __init__(self, mapping: dict[str, BlockTime]) -> None:
        self._mapping = dict(mapping)
        self._lock = threading.Lock()
        self.returned: dict[str, BlockTime] = {}
        self.calls: list[str] = []

    def resolve(self, signature: str) -> BlockTime:
        with self._lock:
            self.calls.append(signature)
        bt = self._mapping.get(signature)
        if bt is None:
            raise BlockTimeUnavailable(f"no fixture for {signature} (CENSORED)")
        with self._lock:
            self.returned[signature] = bt
        return bt


class PoisonResolver:
    """Raises on ANY .resolve() call — proves a warm run touches the RPC ZERO times.

    Raises a bare AssertionError (NOT BlockTimeUnavailable) so a stray call is NOT swallowed
    by the fail-closed `except BlockTimeUnavailable`; it propagates and fails the test loudly.
    """

    def resolve(self, signature: str) -> BlockTime:  # noqa: D401
        raise AssertionError(
            f"RPC resolve() called for {signature!r} on a warm run — the disk cache MISSED. "
            "A warm re-run must be zero-RPC and reproduce anchors from the cache alone."
        )


class MixedResolver:
    """Resolves 'good*' sigs, raises BlockTimeUnavailable for 'miss*', RateLimitedError for
    'slow*' up to `throttle_times` then resolves.  Thread-safe; records call counts."""

    def __init__(self, mapping: dict[str, BlockTime], *, throttle_times: int = 0) -> None:
        self._mapping = dict(mapping)
        self._throttle_times = throttle_times
        self._counts: dict[str, int] = {}
        self._lock = threading.Lock()

    def resolve(self, signature: str) -> BlockTime:
        with self._lock:
            seen = self._counts.get(signature, 0)
            self._counts[signature] = seen + 1
        if signature.startswith("slow") and seen < self._throttle_times:
            raise RateLimitedError(f"throttled {signature} attempt {seen}")
        bt = self._mapping.get(signature)
        if bt is None:
            raise BlockTimeUnavailable(f"no fixture for {signature} (CENSORED)")
        return bt


def _bt(i: int) -> BlockTime:
    # Large, realistic on-chain values so JSON int round-trip is exercised at scale.
    return BlockTime(slot=300_000_000 + i, block_time_ms=1_752_000_000_000 + i * 1000)


def _noop_sleep(_delay: float) -> None:
    return None


# ---------------------------------------------------------------------------
# Momentum-capable corpus builder (30/60/120/300 marks with txns + liquidity)
# ---------------------------------------------------------------------------


def _mom_record(mint, sig, *, rose: bool, recv_wall_ms: int = 1_700_000_000_000) -> dict:
    """A record with a 30s + 60s + post marks so the momentum strategy actually decides.

    `rose=True` => price at 60s > price at 30s + strong buy pressure + liquidity (model &
    baseline select); `rose=False` => flat (both decline).  Post-entry marks drive the walk.
    `recv_wall_ms` is deliberately FAR from any plausible block_time to catch wall-clock leaks.
    """
    v_tokens = "1000000000"
    v_sol = "32"
    p0 = Decimal(v_sol) / Decimal(v_tokens)  # entry-implied launch price
    p30 = p0 * Decimal("1.0")
    p60 = p0 * (Decimal("1.5") if rose else Decimal("1.0"))
    p120 = p0 * (Decimal("2.0") if rose else Decimal("0.9"))
    p300 = p0 * (Decimal("3.0") if rose else Decimal("0.8"))

    def obs(h, price, buys, sells, liq):
        return {
            "price_sol": str(price),
            "liquidity_usd": (str(liq) if liq is not None else None),
            "txns_m5": {"buys": buys, "sells": sells},
            "vol_m5": "10",
            "dex": "pumpfun",
            "note": "ok",
            "horizon_s": h,
            "obs_wall_ms": recv_wall_ms + h * 1000,
        }

    forward = [
        obs(30, p30, 8, 1, 5000),
        obs(60, p60, 9, 1, 6000),
        obs(120, p120, 5, 3, 6000),
        obs(300, p300, 4, 4, 6000),
    ]
    entry = {
        "mint": mint,
        "signature": sig,
        "traderPublicKey": "T",
        "initialBuy": 1_000_000.0,
        "solAmount": 0.06,
        "vTokensInBondingCurve": float(v_tokens),
        "vSolInBondingCurve": float(v_sol),
        "marketCapSol": float(v_sol),
        "name": "n",
        "symbol": "s",
        "pool": "pump",
        "recv_wall_ms": recv_wall_ms,
    }
    return {"entry": entry, "forward": forward}


def _write_corpus(path, records: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")


def _fixture_resolver(records: list[dict], *, drop: set[str] | None = None) -> FixtureBlockTimeResolver:
    """Fixture with an anchor per signature; `drop` leaves sigs unmapped (both paths CENSOR)."""
    drop = drop or set()
    mapping: dict[str, BlockTime] = {}
    for i, r in enumerate(records):
        sig = r["entry"]["signature"]
        if sig in drop:
            continue
        # block_time chosen NEAR 2025 on-chain time, DELIBERATELY far from recv_wall_ms(2023).
        mapping[sig] = BlockTime(slot=310_000_000 + i, block_time_ms=1_752_000_100_000 + i)
    return FixtureBlockTimeResolver(mapping)


# ===========================================================================
# I. CACHED VALUE == LIVE getTransaction value (memo identity, no drift)
# ===========================================================================


def test_cache_hit_is_byte_identical_to_the_live_miss_value(tmp_path) -> None:
    """A HIT returns exactly what a MISS resolved — and exactly what the inner produced."""
    inner = RecordingResolver({"sigK": _bt(42)})
    resolver = CachingBlockTimeResolver(inner, BlockTimeCache(tmp_path / "c.json"))

    miss_value = resolver.resolve("sigK")  # MISS -> inner
    hit_value = resolver.resolve("sigK")  # HIT  -> memo

    assert miss_value == hit_value
    # The memoised value is the SAME object the inner resolver returned (pure memo, no re-derive).
    assert hit_value == inner.returned["sigK"]
    assert inner.calls == ["sigK"]  # exactly one live touch; the hit did zero


def test_cache_never_stores_a_value_the_inner_did_not_return(tmp_path) -> None:
    """Every persisted entry equals a value the inner resolver actually produced."""
    mapping = {f"s{i}": _bt(i) for i in range(25)}
    inner = RecordingResolver(mapping)
    cache = BlockTimeCache(tmp_path / "c.json")
    resolve_block_times(list(mapping), inner, cache=cache, sleep=_noop_sleep)

    raw = json.loads((tmp_path / "c.json").read_text(encoding="utf-8"))
    for sig, entry in raw["entries"].items():
        persisted = BlockTime(slot=entry["slot"], block_time_ms=entry["block_time_ms"])
        assert persisted == inner.returned[sig], f"cache stored a value the RPC never returned for {sig}"


# ===========================================================================
# II. NO WALL-CLOCK EVER SUBSTITUTED
# ===========================================================================


def test_persisted_cache_has_only_slot_and_block_time_ms(tmp_path) -> None:
    """The on-disk memo carries ONLY the two getTransaction fields — no wall-clock, no extras."""
    cache = BlockTimeCache(tmp_path / "c.json")
    cache.put("sigW", _bt(1))
    cache.flush()
    raw = json.loads((tmp_path / "c.json").read_text(encoding="utf-8"))
    assert set(raw["entries"]["sigW"].keys()) == {"slot", "block_time_ms"}


def test_anchor_is_block_time_not_recv_wall_ms_through_the_cached_prefetch(tmp_path) -> None:
    """End-to-end: the resolved decision anchor is the cached block_time, NEVER recv_wall_ms.

    recv_wall_ms is set to a 2023 timestamp; the resolver's block_time is a 2025 timestamp.
    The EntryRecord's decision anchor must equal the block_time (getTransaction), proving the
    perf layer did not swap in the collection wall-clock.
    """
    records = [_mom_record("MINT1", "sig1", rose=True, recv_wall_ms=1_699_999_999_000)]
    corpus = tmp_path / "corpus.jsonl"
    _write_corpus(corpus, records)
    resolver = _fixture_resolver(records)
    expected = resolver.resolve("sig1")  # the on-chain anchor

    prefetched = prefetch_from_corpus(
        corpus, _fixture_resolver(records), cache=BlockTimeCache(tmp_path / "c.json"),
        sleep=_noop_sleep,
    )
    rec = next(read_corpus(corpus))
    entry = to_entry_record(rec, prefetched.resolve("sig1"))

    assert entry.decision_block_time_ms == expected.block_time_ms
    assert entry.decision_slot == expected.slot
    # The wall-clock is retained for monitoring but is NOT the anchor.
    assert entry.recv_wall_ms == 1_699_999_999_000
    assert entry.decision_block_time_ms != entry.recv_wall_ms


# ===========================================================================
# III + IV + V. PREFETCH == SEQUENTIAL through BOTH strategies (anchors, censoring, verdict)
# ===========================================================================


def _decision_slots(outcomes) -> dict[str, int]:
    return {o.mint: o.decision_slot for o in outcomes}


def test_launch_prefetch_equals_sequential_anchors_and_censoring(tmp_path) -> None:
    records = [_mom_record(f"WIN{i}", f"win{i}", rose=True) for i in range(6)] + [
        _mom_record(f"FLAT{i}", f"flat{i}", rose=False) for i in range(6)
    ]
    # Drop two signatures -> both paths must CENSOR exactly those two.
    dropped = {"win0", "flat3"}
    corpus = tmp_path / "corpus.jsonl"
    _write_corpus(corpus, records)

    seq_out, seq_stats = build_from_corpus(
        corpus, block_time_resolver=_fixture_resolver(records, drop=dropped)
    )
    prefetched = prefetch_from_corpus(
        corpus, _fixture_resolver(records, drop=dropped),
        cache=BlockTimeCache(tmp_path / "c.json"), sleep=_noop_sleep,
    )
    par_out, par_stats = build_from_corpus(corpus, block_time_resolver=prefetched)

    assert seq_stats.n_censored == 2
    assert par_stats.n_censored == seq_stats.n_censored
    assert par_stats.n_resolved == seq_stats.n_resolved
    # Per-mint anchor identity — the LEAK BOUNDARY is byte-identical.
    assert _decision_slots(par_out) == _decision_slots(seq_out)


def test_momentum_prefetch_equals_sequential_anchors_and_censoring(tmp_path) -> None:
    """The ACTIVE strategy (momentum @60s) — parity the author's suite did not cover."""
    records = [_mom_record(f"WIN{i}", f"win{i}", rose=True) for i in range(6)] + [
        _mom_record(f"FLAT{i}", f"flat{i}", rose=False) for i in range(6)
    ]
    dropped = {"win2", "flat5"}
    corpus = tmp_path / "corpus.jsonl"
    _write_corpus(corpus, records)

    seq_out, seq_stats = build_momentum_from_corpus(
        corpus, block_time_resolver=_fixture_resolver(records, drop=dropped), entry_horizon_s=60
    )
    prefetched = prefetch_from_corpus(
        corpus, _fixture_resolver(records, drop=dropped),
        cache=BlockTimeCache(tmp_path / "c.json"), sleep=_noop_sleep,
    )
    par_out, par_stats = build_momentum_from_corpus(
        corpus, block_time_resolver=prefetched, entry_horizon_s=60
    )

    assert seq_stats.n_censored == 2
    assert par_stats.n_censored == seq_stats.n_censored
    assert par_stats.n_resolved == seq_stats.n_resolved
    assert par_stats.n_tradeable == seq_stats.n_tradeable
    assert _decision_slots(par_out) == _decision_slots(seq_out)
    # Selection + net PnL per mint must be byte-identical (the perf layer changed nothing).
    seq_by_mint = {o.mint: (o.model_selected, o.baseline_selected, o.net_pnl_lamports) for o in seq_out}
    par_by_mint = {o.mint: (o.model_selected, o.baseline_selected, o.net_pnl_lamports) for o in par_out}
    assert par_by_mint == seq_by_mint


def test_momentum_scoreboard_identical_through_run_edge_proof(tmp_path) -> None:
    records = [_mom_record(f"WIN{i}", f"win{i}", rose=True) for i in range(8)] + [
        _mom_record(f"FLAT{i}", f"flat{i}", rose=False) for i in range(8)
    ]
    corpus = tmp_path / "corpus.jsonl"
    _write_corpus(corpus, records)

    seq_code, seq_sb = run_edge_proof(
        corpus, block_time_resolver=_fixture_resolver(records), strategy="momentum",
        entry_horizon_s=60,
    )
    prefetched = prefetch_from_corpus(
        corpus, _fixture_resolver(records), cache=BlockTimeCache(tmp_path / "c.json"),
        sleep=_noop_sleep,
    )
    par_code, par_sb = run_edge_proof(
        corpus, block_time_resolver=prefetched, strategy="momentum", entry_horizon_s=60
    )
    assert par_code == seq_code
    assert par_sb == seq_sb


# ===========================================================================
# V. DETERMINISM — concurrency level + disk round-trip cannot change the map or censored set
# ===========================================================================


def test_resolved_map_and_censored_set_invariant_to_concurrency() -> None:
    """Mixed good/miss/throttle sigs: the resolved map (and its censored complement) is the
    SAME at concurrency 1, 4, and 32 — values AND which sigs are censored are deterministic."""
    good = {f"good{i}": _bt(i) for i in range(40)}
    slow = {f"slow{i}": _bt(1000 + i) for i in range(10)}
    mapping = {**good, **slow}
    sigs = list(good) + [f"miss{i}" for i in range(10)] + list(slow)

    def run(level: int) -> dict[str, BlockTime]:
        return resolve_block_times(
            sigs, MixedResolver(mapping, throttle_times=2), max_in_flight=level,
            sleep=_noop_sleep, base_delay_s=0.01, max_delay_s=0.02, max_retries=6,
        )

    r1, r4, r32 = run(1), run(4), run(32)
    assert r1 == r4 == r32
    # Exactly the 'miss*' sigs are censored (omitted); good + (retried) slow are resolved.
    assert set(r1) == set(good) | set(slow)
    assert not any(k.startswith("miss") for k in r1)


def test_disk_cache_roundtrip_is_value_lossless_for_large_ints(tmp_path) -> None:
    """Slot + block_time_ms survive the JSON persist/reload exactly (no float coercion)."""
    path = tmp_path / "c.json"
    original = {f"sig{i}": BlockTime(slot=2**53 + i, block_time_ms=1_752_000_000_000 + i)
                for i in range(20)}
    cold = BlockTimeCache(path)
    for sig, bt in original.items():
        cold.put(sig, bt)
    cold.flush()

    warm = BlockTimeCache(path)  # reload from disk
    for sig, bt in original.items():
        got = warm.get(sig)
        assert got == bt
        assert isinstance(got.slot, int) and isinstance(got.block_time_ms, int)


# ===========================================================================
# STALE-CACHE / RACE HUNT
# ===========================================================================


def test_warm_rerun_reproduces_exact_anchors_with_zero_rpc(tmp_path) -> None:
    """A warm run through the POISON resolver (raises on any call) reproduces the EXACT
    sequential scoreboard from the disk cache alone — proving the cache is a faithful, zero-RPC
    replay of the immutable anchors (no stale/wrong value, no fabricated anchor)."""
    records = [_mom_record(f"WIN{i}", f"win{i}", rose=True) for i in range(6)] + [
        _mom_record(f"FLAT{i}", f"flat{i}", rose=False) for i in range(6)
    ]
    corpus = tmp_path / "corpus.jsonl"
    _write_corpus(corpus, records)
    cache_path = tmp_path / "c.json"

    # Ground truth: the direct sequential fixture path.
    _, seq_sb = run_edge_proof(
        corpus, block_time_resolver=_fixture_resolver(records), strategy="momentum",
        entry_horizon_s=60,
    )
    # COLD: populate + persist the cache with the real fixture resolver.
    prefetch_from_corpus(
        corpus, _fixture_resolver(records), cache=BlockTimeCache(cache_path), sleep=_noop_sleep
    )
    # WARM: a fresh cache loaded from disk + a resolver that MUST NOT be called.
    warm = prefetch_from_corpus(
        corpus, PoisonResolver(), cache=BlockTimeCache(cache_path), sleep=_noop_sleep
    )
    _, warm_sb = run_edge_proof(
        corpus, block_time_resolver=warm, strategy="momentum", entry_horizon_s=60
    )
    assert warm_sb == seq_sb  # cache-only replay == live sequential; zero RPC (PoisonResolver)


def test_blocktimecache_is_thread_safe_under_concurrent_put_get(tmp_path) -> None:
    """Hammer put/get from many threads on distinct keys — the EXACT pattern the perf layer
    relies on (the ThreadPoolExecutor workers call `put` via CachingBlockTimeResolver).  Prove
    the final memo has every value intact and the disk snapshot matches memory (no lost/torn
    write, no wrong value).

    NOTE (deliberate): flush() is invoked ONCE, serially, at the end — mirroring the shipped
    `resolve_block_times`, which flushes a single time AFTER the pool has joined.  Concurrent
    flush() is NOT a supported pattern (see the leak-audit MINOR: the fixed temp filename makes
    concurrent flush race), and is never reached in production; this stress test does not use it.
    """
    path = tmp_path / "c.json"
    cache = BlockTimeCache(path)
    n_threads, per_thread = 16, 200
    expected = {
        f"t{t}_s{i}": _bt(t * 1000 + i) for t in range(n_threads) for i in range(per_thread)
    }
    errors: list[str] = []

    def worker(t: int) -> None:
        for i in range(per_thread):
            sig = f"t{t}_s{i}"
            bt = _bt(t * 1000 + i)
            cache.put(sig, bt)
            got = cache.get(sig)
            if got != bt:
                errors.append(f"{sig}: read-after-write mismatch {got} != {bt}")

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()

    assert not errors, errors[:5]
    assert len(cache) == len(expected)
    for sig, bt in expected.items():
        assert cache.get(sig) == bt

    cache.flush()
    raw = json.loads(path.read_text(encoding="utf-8"))
    reloaded = {s: BlockTime(slot=e["slot"], block_time_ms=e["block_time_ms"])
                for s, e in raw["entries"].items()}
    assert reloaded == expected  # disk == memory after a final flush; no torn/lost write


def test_stale_cache_is_trusted_documenting_the_immutability_dependence(tmp_path) -> None:
    """SKEPTIC PROBE (documented, not a code bug): the memo is trusted BLINDLY — a HIT returns
    the cached value without re-checking the inner resolver.  This is correct ONLY because a
    confirmed signature's block_time is IMMUTABLE.  This test PINS that trust boundary so any
    future change that could let a wrong on-disk value flow through is caught: it asserts the
    cache does NOT re-validate, which is why the immutability invariant + atomic write are the
    load-bearing guarantees (see the leak-audit report's residual-risk section)."""
    path = tmp_path / "c.json"
    # A deliberately WRONG anchor placed on disk (simulating external tamper / a prior-version bug).
    tampered = BlockTime(slot=1, block_time_ms=1)
    truth = BlockTime(slot=999, block_time_ms=1_752_000_000_000)
    cold = BlockTimeCache(path)
    cold.put("sigZ", tampered)
    cold.flush()

    warm = BlockTimeCache(path)
    resolver = CachingBlockTimeResolver(RecordingResolver({"sigZ": truth}), warm)
    got = resolver.resolve("sigZ")

    # The cache is trusted as-is (no RPC re-check). This documents WHY atomicity + immutability
    # are mandatory: the memo has no independent validation of a stale value.
    assert got == tampered
    assert got != truth


def test_censoring_count_preserved_when_all_signatures_missing(tmp_path) -> None:
    """Fail-closed at the extreme: NO signature resolves -> every record CENSORED -> the runner
    fails closed (EXIT_FAIL_CLOSED), through the prefetch path, never a fabricated pass."""
    records = [_mom_record(f"M{i}", f"m{i}", rose=True) for i in range(5)]
    corpus = tmp_path / "corpus.jsonl"
    _write_corpus(corpus, records)
    all_dropped = {r["entry"]["signature"] for r in records}

    prefetched = prefetch_from_corpus(
        corpus, _fixture_resolver(records, drop=all_dropped),
        cache=BlockTimeCache(tmp_path / "c.json"), sleep=_noop_sleep,
    )
    code, sb = run_edge_proof(corpus, block_time_resolver=prefetched)
    assert code == EXIT_FAIL_CLOSED
    assert sb["n_resolved"] == 0
    assert sb["n_censored"] == 5
    assert "FAIL-CLOSED" in sb["verdict"]


def test_empty_signature_records_each_censored_not_deduped_away(tmp_path) -> None:
    """A corpus with MISSING signatures ("") must censor EACH such record, not collapse them.

    The prefetch de-dupes the empty-string key to one failed lookup, but the harness still
    iterates every record and the PrefetchedBlockTimeResolver raises for the absent "" key on
    each — so the censored COUNT equals the number of blank-signature records (parity with the
    sequential path), not one."""
    records = []
    for i in range(4):
        r = _mom_record(f"NOSIG{i}", "x", rose=True)
        r["entry"]["signature"] = ""  # blank signature
        records.append(r)
    good = _mom_record("GOOD", "goodsig", rose=True)
    records.append(good)
    corpus = tmp_path / "corpus.jsonl"
    _write_corpus(corpus, records)

    resolver = _fixture_resolver([good])  # only "goodsig" is resolvable; "" is not
    seq_out, seq_stats = build_from_corpus(corpus, block_time_resolver=resolver)

    prefetched = prefetch_from_corpus(
        corpus, _fixture_resolver([good]), cache=BlockTimeCache(tmp_path / "c.json"),
        sleep=_noop_sleep,
    )
    par_out, par_stats = build_from_corpus(corpus, block_time_resolver=prefetched)

    assert seq_stats.n_censored == 4  # each blank-sig record censored
    assert par_stats.n_censored == seq_stats.n_censored
    assert par_stats.n_resolved == seq_stats.n_resolved == 1
    assert _decision_slots(par_out) == _decision_slots(seq_out)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v", "-p", "no:randomly"]))
