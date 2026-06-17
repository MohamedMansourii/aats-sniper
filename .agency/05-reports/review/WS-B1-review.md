# Code Review — WS-B1 (EnhancedWsFallback: WS logsSubscribe + RPC polling fallback)

- Task: WS-B1 — WS ingest (`logsSubscribe` one-sub-per-program + getTransaction enrichment;
  `getSignaturesForAddress` polling fallback; `--source=ws` wiring) + RE-REVIEW of the
  determinism fix for the three flaky WS-fallback tests.
- Round: re-review (after engineer's "test-layer-only `_FakeClock`" fix).
- Reviewer: code-reviewer (Quality Gate, G3).
- Verdict: **FAIL** (one BLOCKER).
- One-line: The feature is correct and the wiring/security are clean, but the determinism
  fix it claims to deliver is incomplete — the full suite still fails intermittently
  (1 failing run in 16, a 20-test cascade that includes a test inside the WS-B1 file),
  so the suite is NOT deterministically green and the engineer's "0 failed across runs"
  claim does not reproduce.

---

## What I verified (commands run)

| Check | Result |
|---|---|
| `pytest tests/ingestion/test_ws_fallback.py` | 29 passed (matches claim) |
| `pytest tests/ingestion/` | 422 passed (matches claim) |
| 3 flaky tests x5 in isolation | 3 passed x5 (matches claim) |
| `pytest tests/ingestion/` x6 loop | 6/6 clean (flake does NOT reproduce in-suite alone) |
| Full suite `PYTHONHASHSEED=0 pytest tests/` — **run 1** | **20 FAILED, 2361 passed** |
| Full suite — 15 further runs (incl. 3+ consecutive) | 2381 passed each |
| Full suite WITHOUT WS-B1 file (`--ignore`) x5 | 2352 passed each, 0 failed |
| Secrets scan on diff + new test file | clean (all values FAKE/EXPLICIT/`<key>`/empty) |
| Read-only audit of WS transport additions | confirmed — no sign/submit/keypair |
| `ruff check tests/ingestion/test_ws_fallback.py` | 13 errors (see B-2) |

Net flake rate observed: **1 / 16 full-suite runs (~6%) WITH the WS-B1 file present;
0 / 5 WITHOUT it.** Directional but unambiguous: the new file is implicated.

---

## Findings

### B-1 — BLOCKER — Determinism fix is incomplete; full suite still flakes
`tests/ingestion/test_ws_fallback.py` (whole file) + interaction with the broader suite.

**What's wrong.** The stated purpose of this re-review fix is to make the three flaky
WS-fallback tests deterministic and the suite green. On my first independent full-suite
run (`PYTHONHASHSEED=0 python -m pytest tests/ -q`) I got **20 failed, 2361 passed** — not
the claimed "2381 passed / 0 failed." The failures were a cascade that included:
- `tests/ingestion/test_ws_fallback.py::TestPointInTimeCorrectness::test_decoder_holds_pending_when_block_time_absent` (a test inside THIS file)
- `tests/ingestion/test_t300a_block_time_leak.py::...test_none_block_time_does_not_produce_event_time_close_to_now`
- `...test_zero_block_time_does_not_produce_event_time_close_to_now`
- `...test_pumpfun_migrate_none_block_time_does_not_produce_event_with_wall_clock_date`
- `...test_router_none_block_time_not_substituted_with_wall_clock`
- (+15 more in the same run)

**Why it matters.** Several of these assertions are *logically impossible* to fail in
isolation — e.g. `test_decoder_holds_pending_when_block_time_absent` asserts only
`_make_event_time(slot=..., block_time_unix_s=None) is None`, and `_make_event_time`
(decoders.py:202) is pure and unconditionally returns `None` for a `None`/`<=0` block_time
with no global state. For that assertion to fail, the *test infrastructure* (event loop or a
monkeypatched global) must have been corrupted by an earlier test — a classic symptom of a
leaked `asyncio.run()` loop and/or an `unittest.mock.patch` of the module global
`aats.ingestion.transport._time_now_s` not being cleanly torn down under contention. The
WS-B1 file is the strongest suspect: with it present 1/16 runs failed; with it removed
(`--ignore`) 0/5 failed, and the ingestion suite alone never reproduces (6/6). The fix
replaced wall-clock timing with `_FakeClock` *inside the three named tests*, but did not
address the cross-test pollution surface that lets a full-suite run go red. The acceptance
condition for WS-B1 ("the three flaky tests fixed; suite deterministically green") is
therefore NOT met.

**What good looks like.** Make the suite green across, say, 20 consecutive full-suite runs.
Concretely: (a) convert the `asyncio.run(run())`-inside-sync-test pattern to
`pytest.mark.asyncio` async test functions so pytest-asyncio owns one loop per test and
tears it down deterministically (the file mixes both styles today); (b) scope every
`patch("aats.ingestion.transport._time_now_s", ...)` and every `patch.object(...)` so it is
guaranteed restored even when the async-for body `break`s early or an inner generator is left
un-`aclose()`d — i.e. ensure the async generators created in `run()` (`subscribe(...)`,
`_stream_poll(...)`, `_empty_async_gen()`) are explicitly closed (`async with aclosing(...)`
or `await gen.aclose()`), since an abandoned generator can resume a patched-clock body after
the `with` has exited. The fix must be re-proven by a loop (n>=20) of the FULL suite, not the
ingestion subset (the flake only surfaces under full-suite contention — the engineer's own
root-cause note says so).

> Note for the orchestrator: B-1 is a test-suite reliability blocker, not a product-logic bug.
> The production WS transport behavior (below) is correct. If the orchestrator wants to unblock
> the *feature* while the flake is chased separately, that is a re-plan decision — but per the
> charter G3 cannot PASS while the delivered fix's own acceptance condition (deterministic
> green suite) is unproven.

### B-2 — MINOR — "0 new lint errors" claim is misleading for a brand-new file
`tests/ingestion/test_ws_fallback.py` (untracked / net-new).

`ruff check` reports **13 errors**, including `F401` (`typing.Callable` imported but unused,
line 57), `I001` import-block unsorted (lines 50, 822), `UP035`/`UP037` modernizations, and
seven `SIM117` nested-`with`. The handoff frames these as "identical — 13 pre-existing errors,
0 new introduced," but this is a *new* file (`git status` shows `?? test_ws_fallback.py`), so
all 13 are net-new to the repo. None are behavioral; `F401` is a genuine dead import. Fix the
`F401` at minimum and run `ruff --fix` on the rest. Severity MINOR because none change behavior;
flagged because the self-check claim does not match reality.

### N-1 — NIT — `_FakeClock.advance()` and `Callable` import are dead code
`tests/ingestion/test_ws_fallback.py:103-114, 57`. `_FakeClock.advance()` is defined and
documented but never called; `Callable` is imported but unused. Remove or wire up. Optional.

---

## Conformance

| Item | Verdict | Notes |
|---|---|---|
| logsSubscribe: one sub/program, `mentions` filter, `err==null` drop | PASS | transport.py:973-1063 — one `logsSubscribe` per program id, `{"mentions":[pid]}`, `commitment=confirmed`; errored notifications dropped (1036). |
| getTransaction enrichment (jsonParsed, blockTime → block_time_unix_s) | PASS | transport.py:1120-1162, 1242-1336. blockTime absent/null/0 → None (T-300a honored); errored tx (`meta.err`) dropped (1286). |
| getSignaturesForAddress polling fallback present + RPC-only | PASS | transport.py:1082-1203. Standard JSON-RPC only; activates on WS connect-fail / idle. |
| Keys/URLs from env only (never logged/hardcoded) | PASS | shadow_record.py reads `RPC_PRIMARY`/`WS_ENDPOINT` from `os.environ` only; `_ws_url`/`_rpc_url` marked "NOT logged"; `.env.example` adds `WS_ENDPOINT=` blank with "NEVER hardcode the key." Secrets scan clean. |
| Read-only (no sign/submit/keypair) | PASS | Only read-only JSON-RPC (`getTransaction`, `getSignaturesForAddress`, `logsSubscribe`); no signing path. |
| `--source=ws` wired; graceful when RPC_PRIMARY unset | PASS | shadow_record.py:340-377 adds `ws` branch, argparse choice, https→wss derivation; empty RPC_PRIMARY logs clear error and yields nothing (no crash). |
| replay + geyser still work | PASS | Those branches untouched; full ingestion suite (422) green; geyser/replay tests pass. |
| Blueprint / api-contracts / data-models conformance | PASS | Emits the same `RawTransaction` contract as GeyserTransport; T-300a point-in-time law upheld. |
| Tests present + meaningful | PARTIAL | Coverage is good and asserts behavior (dedup, err-drop, T-300a, URL derivation, graceful-empty). BUT the suite is not reliably green (B-1) — a test suite that intermittently fails on impossible-to-fail assertions is not yet a trustworthy gate. |

---

## Overall

Feature implementation: solid and conformant. The blocker is the *deliverable of this
re-review itself* — determinism. The engineer's headline claim ("2381 passed / 0 failed across
3 consecutive runs", "the three flaky tests now deterministic") did not reproduce: I hit a
20-test cascade failure on a full-suite run, including a test in the WS-B1 file. The fix
treated the three named tests' symptoms (wall-clock timing) but not the cross-test
event-loop / global-patch pollution that lets a full run go red. Re-fix and re-prove with a
>=20x full-suite loop.

**VERDICT: FAIL (B-1 BLOCKER).**
