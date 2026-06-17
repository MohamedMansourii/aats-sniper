# G3 — Wave S verdicts (safety-first Lane C + dashboard + contracts fix)

**Gate:** G3 (per-task build) — DUAL per AATS overlay: `code-reviewer` AND `backtest-qa-engineer` must both PASS.
**Verified by:** `orchestrator` — 2026-06-16 — by reading the ACTUAL changed files under `C:/dev/aats`, not trusting handoffs.
**Streams:** P (contracts fix + safety primitives) · E (operator dashboard).

> CRITICAL MANDATE for this wave: the three safety primitives (breaker / survivable-stop / DMS) must be
> **PROVEN-BY-FIRING** — the tests must actually fire them, not merely instantiate them. Confirmed below
> with the exact firing tests and their assertions, read from source.

---

## Per-task verdicts

| Task | Title | code-reviewer | backtest-qa-engineer | Proven-by-firing | Verdict |
|---|---|---|---|---|---|
| T-320 | Daily-loss circuit breaker (G3 fix, BLOCKER B1) | PASS | PASS | YES | **DONE** |
| T-321 | Survivable stop (3 independent layers) | PASS | PASS | YES | **DONE** |
| T-322 | Dead-man's switch | PASS | PASS | YES | **DONE** |
| T-350 | Dashboard review-item cleanup | PASS | n/a (UI, non-trading) | n/a | **DONE** |
| T-351 | Dashboard destructive-control tests | PASS | **MISSING** | n/a | **NEEDS dual re-review** (2nd PASS) |
| T-199fix | Contracts fix (T-199a leak-guard + T-199b LatencyHop alias) | **MISSING** | **MISSING** | n/a | **NEEDS-REVIEW** (engineer died; fix is in source but unverified) |

---

## T-320 — Daily-loss circuit breaker (BLOCKER B1 fix) — DONE

**Files read:** `aats/risk/circuit_breaker.py`, `aats/contracts/risk.py`,
`tests/risk/test_circuit_breaker.py`, `.agency/02-architecture/data-models.md §8`,
`.agency/02-architecture/adr/ADR-0012-breaker-event-time-day-key.md`.

**B1 fix confirmed in source.** `BreakerState` now carries `daily_net_pnl_day_utc` (contracts/risk.py:144)
with a model validator that rejects a non-zero net lacking a day key (`_day_key_invariant`, lines 175–186) —
fail-closed at load. Restart seeding (circuit_breaker.py:288–298) seeds the persisted net into its STORED
event-time day, never into the first arriving event's day; corrupt store (non-zero net, no day) raises (293–297).

**Proven-by-firing (read from `tests/risk/test_circuit_breaker.py`):**
- `test_loss_sequence_crossing_floor_trips_breaker` (66–88): a `-5M/-5M/-8M` loss sequence TRIPS, entries STOP
  (`entries_allowed() is False`), flatten invoked exactly once, state persisted TRIPPED. The breaker FIRES.
- `test_restart_positive_carry_does_not_mask_real_next_day_halt` (285–312): the DANGEROUS direction — day-1
  `+50M` no longer masks a real day-2 `-20M` loss; the hard halt now FIRES (`is_tripped() is True`, flatten==1).
- `test_restart_negative_carry_does_not_spuriously_trip_fresh_day` (255–283): day-1 `-14M` carry no longer
  bleeds into a fresh day-2 `-10M` event; stays ARMED. Both B1 directions closed.
- `test_breaker_state_nonzero_net_requires_day_key` (347–357): contract invariant fires (ValidationError).
- Asymmetric trust (165–206): LLM can `trip_from_llm` but `reset()` rejects the de-risk signal
  (PermissionError) and `OperatorResetToken("attacker")` is unforgeable. Latch survives restart (212–229).
- Money: float PnL rejected at `PnLEvent` and `BreakerState` (397–410). No win-rate field anywhere.

**Reviews:** code-reviewer PASS (re-ran cold after purging stale `__pycache__`; full suite 225 passed; the new
restart-seeding + contract-invariant tests are meaningful, would fail on old logic). backtest-qa-engineer PASS
(mutation A: trip branch gated off → 16 reds; mutation B: re-introduced exact B1 bug → both B1 regression tests
go RED; persist-before-flatten durable; no auto-reset path; point-in-time/asymmetric-trust hold under probing).

**Carry (non-blocking):** `data-models.md §8` is a frozen contract — schema change recorded as ADR-0012 + delta
notice (confirmed both present). `solana-systems-architect` to ratify; T-340/341 control plane (`/api/breaker`)
and T-352 dashboard must echo the new nullable `daily_net_pnl_day_utc` field. Logged on board, not a G3 blocker.

## T-321 — Survivable stop (3 independent layers) — DONE

**Files read:** `aats/risk/survivable_stop.py`, `deadman.py`,
`tests/risk/test_survivable_stop_process_death.py`.

**Proven-by-firing (read from `tests/risk/test_survivable_stop_process_death.py`):**
- `test_venue_native_resting_stop_fires_with_process_dead` (83–105): arms all 3 layers, then CEASES the FAST loop
  (simulated process death); the OFF-BOX keeper (`keeper_tick`) flattens at the breach with the in-process
  enforcer NEVER running (`exit_venue.exits == []`). Layer 1 fires with the loop DEAD.
- `test_dead_mans_switch_flattens_when_heartbeat_lost` (111–134): with no heartbeat for >T_DMS, the separate
  failure domain fires the flatten and latches EXACTLY once (`is_fired() is True`, second check returns False).
- `test_in_process_enforcer_fires_when_alive` (140–151): steady-state Layer 2 fires on breach.
- `test_all_layers_share_the_same_fixed_trigger` (157–165): all three enforce the identical fixed trigger.

`StopState.tighten()` (survivable_stop.py:181–217) is tighten-only — refuses any lower trigger (raise) and
refuses tightening above entry. Float prices rejected at every boundary (`_to_decimal_price`, 56–71).

**Reviews:** code-reviewer PASS (3 layers in separate failure domains; FAST decision is a pure synchronous
Decimal compare, `_ExplodingVenue` proves zero IO; venue-native uses the ExecutionVenue seam faithfully).
backtest-qa-engineer PASS (39 suite + full 264 passed; decide() p99=2.4us << 50ms; 4 production-code mutations
all caught then reverted byte-identical; DMS non-disarmable by LLM via unforgeable `DmsStandDownToken`).

## T-322 — Dead-man's switch — DONE

**Files read:** `aats/risk/deadman.py` (shared with T-321); fix files `aats/risk/__init__.py`,
`aats/risk/dms_main.py`.

Both prior G3 BLOCKERs fixed under CI-pinned ruff 0.4.10: R-01 (I001 import sort in `__init__.py`) and
R-02 (UP038 `isinstance(raw, bytes | bytearray)` in `dms_main.py:138`). DMS fires on heartbeat loss >T_DMS,
latches FIRED before submit (fail-closed if the flatten handler raises), and is non-disarmable by LLM/market —
`DmsStandDownToken` raises PermissionError on direct construction.

**Reviews:** code-reviewer PASS (both BLOCKERs cleared in isolation; `tests/risk/` 91 passed; T_DMS
env-configurable; separate failure domain; money int/Decimal). backtest-qa-engineer PASS (mutation-meaningful:
M1 never-fire → 9 reds; M2 always-stale → suppression tests red; M3 forged duck-typed token accepted → token
test red; all reverted byte-identical, suite re-green at 91).

## T-350 — Dashboard review-item cleanup — DONE

**Files spot-checked:** `dashboard/src/App.tsx`, `dashboard/src/lib/chart-colors.ts`, and the 5 test files
under `dashboard/src/**` confirmed present (Glob). All four review items closed: centralized `<Layout>` via
`AppLayout` + `<Outlet/>`; shared `chart-colors.ts`; stale events/min counter fixed with a ticking clock; ESLint
37→0. Verified by reviewer's own execution: `VITE_USE_MOCK=true npm run build` exit 0, `npm run lint` exit 0.
No win-rate field, no new float-money rendering, DRY-RUN/mock default intact.

**Reviews:** code-reviewer PASS (executed build + lint himself; two MINOR non-blocking notes — pre-existing
float-SOL telemetry in mock types is out-of-scope, deferred to G4/architect; vendored shadcn lint scoped off).
No backtest-qa needed — UI display surface, not a trading/safety path.

## T-351 — Dashboard destructive-control tests — NEEDS dual re-review (2nd PASS)

**Files confirmed present:** `dashboard/src/lib/api.destructive.test.ts`,
`components/kit/KillSwitch.test.tsx`, `pages/{Settings.destructive,flatten,pages.render}.test.tsx`.

24 tests pin the destructive controls (kill/flatten/flatten/{mint}/breaker-reset/mode) to the FROZEN
api-contracts §5 endpoints; confirm-gating on kill and go-live; zero network calls in mock mode.
code-reviewer PASS — and proved meaningfulness by mutation (breaking `ENDPOINTS.kill` → 2 reds; removing the
confirm guard → 3 reds). No `.only/.skip`, no `as any`, no win-rate/float-money/secret patterns.

**WHY NOT DONE:** AATS overlay §3 — G3 is DUAL. Only the `code-reviewer` half is on record. The reviewer's
own handoff explicitly states: *"Orchestrator must route to backtest-qa-engineer for the second PASS before
T-351 closes and unblocks T-352."* No second verdict → not closeable. Re-entry: `backtest-qa-engineer` PASS.

## T-199fix — Contracts fix (T-199a + T-199b) — NEEDS-REVIEW (FAILED dispatch)

**Stream P JSON:** `{"taskId":"T-199fix","status":"FAILED","reason":"engineer died"}` — the dispatched fix
agent terminated without producing a handoff or review verdicts.

**However, the fixes ARE present in source** (read directly):
- T-199a: `aats/contracts/features.py` — `from __future__ import annotations` REMOVED, eager annotation
  evaluation, explicit `FeatureFrame.model_rebuild(force=True)` at module bottom; docstring (24–49) documents
  root cause (lazy PEP-563 schema left `extra="forbid"` unenforced) and the mutation proof.
- T-199b: `aats/contracts/api_schemas.py:248` — `cls: str = Field(default="", alias="class",
  serialization_alias="class")` with `model_config = {"frozen": True, "populate_by_name": True}` (252).
  Wire-key test present in `tests/contracts/test_api_schemas.py`.

**WHY NOT CLOSEABLE:** Iron Rule §3.4 — code is not done without `code-reviewer` AND `backtest-qa-engineer`
PASS. The engineer died before review; there is NO verdict on record, NO confirmation the PYTHONHASHSEED-sweep
green suite or the mutation test were actually run. The source looks correct but is **UNVERIFIED**. This is a
process re-dispatch (re-run the dual review against the already-landed diff), NOT a re-build — the code exists.

---

## Verdict roll-up

- **DONE (5 streams of work):** T-320, T-321, T-322, T-350. The three safety primitives are **PROVEN-BY-FIRING**.
- **NEEDS dual re-review:** T-351 (backtest-qa 2nd PASS).
- **NEEDS-REVIEW (FAILED dispatch, code already landed):** T-199fix — re-run dual review on the in-tree diff.

**Safety mandate satisfied:** breaker, survivable-stop, and DMS each have a test that ACTUALLY FIRES the
primitive (trip / off-box keeper flatten with loop dead / DMS heartbeat-loss flatten), confirmed by reading the
test assertions from source. Mutation tests on all three prove the firing logic is load-bearing.

**`allPass`: NO** — two items (T-351, T-199fix) lack the dual-verdict required to close.
**`safetyProven`: YES** — all three safety primitives are DONE with both reviewers PASS and proven-by-firing.
