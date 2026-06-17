# G3 — Suite-Stabilization — VERDICTS (source-verified, FINAL)

_Recorded 2026-06-16 by `orchestrator`. Verified by reading the ACTUAL files under
`C:/dev/aats` (`tests/ingestion/conftest.py`, `tests/risk/test_resting_orders.py`,
`aats/risk/sizing.py`, `tests/execution/test_tx_builder.py`), not by trusting the
engineer/review JSON. G3 is DUAL on every code task: `code-reviewer` AND
`backtest-qa-engineer` must BOTH PASS (AATS overlay rule 3)._

**Headline (FINAL):**
- **STAB-ingestion → DONE** (dual PASS, source-confirmed). Root cause + fix below.
- **Consolidated suite is now reliably green and PROVEN STABLE** — the 10x consolidated
  stability gate (which was `null` in the prior recording) has now been **RUN and is PASS:
  1803 passed / 2 skipped / 0 failed, bit-for-bit identical across all 10 consecutive runs.**
- **T-326 att.3 → STILL NOT DONE.** The production hermetic-decimal-context fix HAS LANDED in
  the tree and the 10x stability gate (which exercises `tests/risk`, including the T-326
  covering test — see the +1 pass count) is green, so the **flake is empirically eliminated**.
  BUT the att.3 re-dispatch to obtain the dual G3 verdict **FAILED** (`status: FAILED,
  reason: "engineer died"`). A safety-path production change cannot be recorded DONE without a
  `code-reviewer` + `backtest-qa-engineer` PASS on the att.3 diff. **T-326 stays
  NEEDS-REPLAN (att.3 landed + flake-resolved, REVIEW-VERDICT MISSING).**

---

## 1. Per-item verdicts

| Item | code-reviewer | backtest-qa | 10x consolidated stability gate | Verdict |
|---|---|---|---|---|
| **STAB-ingestion** (event-loop pollution) | **PASS** | **PASS** (leak-guard mutation-proven) | **PASS** (1803/2/0 ×10, identical) | **DONE** |
| **T-326 att.3** (resting-orders hermetic sizing fix) | _att.2 PASS only_ | _att.2 FAIL (49/50)_ | **PASS** (flake gone) | **NEEDS-REPLAN — fix landed + flake-resolved, dual-G3 verdict MISSING (re-dispatch died)** |

---

## 2. STAB-ingestion — root cause, fix, evidence (DONE)

### Root cause (global-state pollution)
`pytest-asyncio` 1.4.0 with `asyncio_mode=auto` uses a session-scoped runner whose
`_temporary_event_loop()` captures `old_loop = None` at session start (no loop yet), then
**restores `asyncio.set_event_loop(None)` after the last async test in `tests/control_plane`
completes**. Every subsequent *synchronous* ingestion test that called the deprecated
`asyncio.get_event_loop().run_until_complete(coro)` then raised
`RuntimeError: There is no current event loop in thread 'MainThread'` — 112/112 ingestion
failures in the consolidated suite. Polluter = `tests/control_plane`; victim = `tests/ingestion`.

### Fix (test-layer only — zero production change)
- **PRIMARY:** replaced **all 94** `asyncio.get_event_loop().run_until_complete(coro)` call
  sites with self-contained `asyncio.run(coro)` across 6 ingestion test files. `asyncio.run()`
  creates/runs/closes its own loop without reading or mutating global loop state — immune to a
  leaked `set_event_loop(None)`.
- **DEFENSE-IN-DEPTH:** added `tests/ingestion/conftest.py` — a session-scoped autouse fixture
  that installs a fresh loop iff none is healthy and always yields. Dormant when healthy.
- **Zero assertions changed; zero test logic altered** (382 real behavioral assertions remain;
  no `skip`/`xfail`/`assert True` placeholders).

### Source confirmation (orchestrator)
- `tests/ingestion/conftest.py` present, matches the described defensive fixture.
- `grep get_event_loop().run_until_complete tests/ingestion` → **1** occurrence, a **doc-comment**
  in `conftest.py` (not executable). Zero executable call sites remain.

### Leak-guard integrity (backtest-qa, mutation-proven — fix did NOT gut the guards)
Five real source mutations applied/RED/restored (MD5 byte-identical after restore):
M1 wall-clock substitution → 19 RED · M2 `_staleness_ms` sentinel collapse → 2 RED ·
M3 `assert_recorded_at_honesty` disabled → RED · M4 backfill `recorded_at` guard disabled → RED ·
M5 pending `event_date` substitution → RED. Every point-in-time / no-leak guard still goes RED
under its own mutation; the loop fix changed only test plumbing, not assertions.

**Verdict: STAB-ingestion DONE.** Dual PASS, source-confirmed.

---

## 3. Consolidated stability gate — 10x consecutive, PROVEN STABLE (PASS)

Run by `backtest-qa-engineer` from `C:/dev/aats` with deterministic settings
(`PYTHONDONTWRITEBYTECODE=1`, `PYTHONHASHSEED=0`, `-p no:cacheprovider`, `__pycache__` purged
before every run). Environment: Python 3.11.9, pytest 9.1.0.

| Run | Result | Wall time |
|---|---|---|
| 1 | 1803 passed, 2 skipped, 0 failed | 157.11s (cold caches) |
| 2 | 1803 passed, 2 skipped, 0 failed | 73.61s |
| 3 | 1803 passed, 2 skipped, 0 failed | 74.14s |
| 4 | 1803 passed, 2 skipped, 0 failed | 74.79s |
| 5 | 1803 passed, 2 skipped, 0 failed | 75.27s |
| 6 | 1803 passed, 2 skipped, 0 failed | 74.24s |
| 7 | 1803 passed, 2 skipped, 0 failed | 73.15s |
| 8 | 1803 passed, 2 skipped, 0 failed | 76.19s |
| 9 | 1803 passed, 2 skipped, 0 failed | 75.84s |
| 10 | 1803 passed, 2 skipped, 0 failed | 75.34s |

**Verdict: STABLE — PASS.** Bit-for-bit identical pass count across all 10 runs; no flakiness,
no failures, no errors, no order-dependent variance. `failingFiles` empty (no `--tb=no -rf`
investigation needed — no run produced a failure).

- **The only non-pass result is the 2 known solders-gated execution skips** — the explicitly
  allowed exception. Verified with `-rs` (not on faith): both are in
  `tests/execution/test_tx_builder.py` (**line 161** and **line 186**), each gated on
  `"solders not installed — _build_swap_accounts is not on the live path."` Source-confirmed by
  the orchestrator this run. No unexpected skip masks a failure.
- **1803 vs the prior 1802:** +1 passing test, NOT a regression. Consistent with the T-326
  risk-flake hardening adding a covering test (`test_sizing_hermetic_under_hostile_decimal_context`);
  it passed in all 10 runs.
- **No resource leak / progressive slowdown:** run 1 was slower (157s, cold imports); runs 2-10
  settled to a stable ~73-77s.
- The previously-reported ingestion event-loop pollution and the T-326 risk flake **did not
  reproduce in any of the 10 runs.**

---

## 4. T-326 att.3 — what landed, why the flake is gone, why it is STILL NOT DONE

### What the att.3 fix did (source-confirmed by orchestrator this run)
The att.2 BLOCKER (backtest-qa) was: the stated root cause (leaked decimal context) was
addressed only by a test fixture, but the captured cap breach reproduced with UNMODIFIED
production code. The att.3 fix takes the diagnosis further and **fixes production**:

- **`aats/risk/sizing.py:89`** — new `_hermetic_decimal_ctx()` context manager (confirmed present).
- **`aats/risk/sizing.py:288`** — `FractionalKellySizer.size()` now runs **all** Decimal
  arithmetic `with _hermetic_decimal_ctx():` (prec=28, ROUND_HALF_EVEN), so the fractional steps
  that FEED the integer aggregate clamp are point-in-time reproducible and immune to any ambient
  process-global decimal context. This is the true fix: the global context (not a boundary
  constant) was the leak feeding the within-tick aggregate-cap path.
- **`tests/risk/test_resting_orders.py`** — new `test_sizing_hermetic_under_hostile_decimal_context`
  asserting BOTH the hard cap holds AND the within-tick result is byte-identical across 63 hostile
  (prec×9, rounding×7) contexts.

### Why the flake is now empirically resolved
The 10x consolidated stability gate (§3) exercises `tests/risk` and includes the new T-326
covering test (the +1 pass count). All 10 runs were green, identical, and the att.2 ~1/50 flake
did not reproduce in any run. The flake source — the process-global decimal context — is closed
by the hermetic context, and the gate confirms it.

### Why it is STILL NOT recorded DONE
1. **The att.3 re-dispatch DIED.** `{"taskId":"T-326","status":"FAILED","reason":"engineer died"}`.
   There is **no `code-reviewer` PASS and no `backtest-qa-engineer` PASS on the att.3 production+test
   diff.** The only embedded reviews remain the **att.2** verdicts (code-reviewer PASS, backtest-qa
   FAIL on 49/50).
2. **Charter §3.4 / §4 (G3) + overlay rule 3:** a SAFETY-path production change (aggregate-exposure
   cap sizing) cannot pass on one stale partial review. No verdict, no progress — even with the
   flake demonstrably gone. The stability gate proves the flake is fixed; it does NOT substitute for
   a code-review of the safety-path production diff.

### Re-entry criteria (NARROWED — no further fix work needed)
The fix is landed and the flake is proven gone, so the remaining work is **verdict-only**:
re-dispatch the dual G3 (`code-reviewer` + `backtest-qa-engineer`) on the **already-landed** att.3
production+test diff (`aats/risk/sizing.py`, `tests/risk/test_resting_orders.py`). The
`backtest-qa-engineer` may cite this 10x-green gate as the stability evidence and need only confirm
the hermetic-context fix is mutation-meaningful (revert the hermetic context / cap threading → the
B1 aggregate-cap tests go RED) and the hostile-context test genuinely binds the cap. On dual PASS →
T-326 DONE. If att.3 fails the dual G3 → that is the **3rd content strike** → CEO escalation with
options (charter §3.5), not a silent att.4. **This was a re-dispatch DEATH (process event), not a
content failure — it does not by itself consume the 3rd content strike.**

---

## 5. Suite-stability statement (FINAL)

- **The consolidated test suite is now reliably green and PROVEN STABLE** — 1803 passed / 2 skipped
  / 0 failed, identical across **10 consecutive deterministic runs**. The only non-pass result is
  the 2 explicitly-allowed solders-gated execution skips.
- **STAB-ingestion is COMPLETE and proven** (dual G3 PASS, mutation-proven leak guards intact).
- **T-326 is NOT closed:** the production fix is landed and the flake is empirically eliminated by
  the 10x gate, but the att.3 dual-G3 verdict is MISSING because the re-dispatch died. T-326 stays
  NEEDS-REPLAN (verdict-only re-dispatch outstanding), OFF the milestone path, runs ∥ G4.
- **P3 is therefore NOT *fully* complete on the T-326 enhancement.** The end-to-end driveable-on-
  SimulationVenue MILESTONE (T-340 + T-341, `G3-waveE.md`) is unaffected and remains ACHIEVED;
  every milestone-path build task is dual G3 DONE; the consolidated suite is reliably green.

**Bottom line:** suite stabilization is COMPLETE and the consolidated suite is proven stable (10x
green). T-326's production flake is fixed and empirically gone; only its dual-G3 verdict is
outstanding (re-dispatch died). Next stage = **G4 INTEGRATION**; the T-326 verdict-only re-dispatch
runs in parallel. The E1–E13 Enhancement program runs **LAST, after G6** per the CEO reorder
(`.agency/00-brief/ENHANCEMENT-DIRECTIVE-E1-E13.md` §"ORDERING").
