# G3 — Wave M1 + C1 verdicts (Lane A sensors + Lane C core) + T-199 closure + T-351 dual-close

**Gate:** G3 (per-task build) — DUAL per AATS overlay (ROSTER §5): `code-reviewer` AND
`backtest-qa-engineer` must both PASS. No verdict, no progress.
**Verified by:** `orchestrator` — 2026-06-16 — by reading the ACTUAL changed files under
`C:/dev/aats`, not trusting handoffs. Source confirmations are quoted with file:line below.
**Streams:** A (M1 sensors: T-300 ingestion, T-304/T-305 features) · C1 (M4 core:
T-323/T-324/T-325 risk, T-327 execution) · contracts fix (T-199fix → closes T-199) · E (T-351 dashboard).

> Domain mandate for this wave (ROSTER §5): point-in-time / no-leak for every feature path;
> DRY-RUN no-submit for the execution venue; integer-money discipline; asymmetric LLM trust
> (de-risk only); G3 is DUAL on every code task.

---

## Per-task verdicts

| Task | Title | code-reviewer | backtest-qa-engineer | Verdict |
|---|---|---|---|---|
| T-199fix | Contracts fix re-validation (closes T-199 → T-199a + T-199b) | PASS | PASS | **DONE** (T-199a, T-199b DONE; **T-199 CLOSED**) |
| T-300 | M1 ingestion (transport/decoders/bus/point-in-time store + SHADOW) | PASS | PASS | **DONE** (1 MAJOR filed to transport-wiring task; non-blocking — live transport is a disclosed STUB) |
| T-304 | Feature engineering + FeatureFrame assembler (point-in-time) | PASS | PASS | **DONE** |
| T-305 | Buy-pressure / volume first-K-slot feature (C-4 baseline enabler) | PASS | PASS | **DONE** |
| T-323 | Sub-10ms pre-trade safety gate (token-safety scanner) | PASS | PASS | **DONE** |
| T-324 | Hierarchical risk rule engine + ¼-Kelly sizing + cost gate | PASS | PASS | **DONE** |
| T-325 | TP-ladder + trailing + hard-stop + timeout ExitEngine | PASS | PASS | **DONE** |
| T-327 | Real `JitoJupiterVenue` behind seam — DRY-RUN / no-submit FIRST | PASS | PASS | **DONE** (DRY-RUN no-submit verified by source + mutation) |
| T-351 | Dashboard destructive-control tests (Lane E) | PASS (prior) | PASS (this wave) | **DONE** (dual G3 closed) |

**All 9 tasks: dual G3 PASS → DONE.**

---

## T-199fix — closes T-199 (T-199a leak-guard + T-199b LatencyHop alias) — DONE

**Files read:** `aats/contracts/features.py`, `aats/contracts/api_schemas.py`,
`tests/contracts/test_no_truth_fields.py`, `tests/contracts/test_api_schemas.py`.

**T-199a (FeatureFrame leak-guard determinism) confirmed in source:**
- `from __future__ import annotations` is NOT an active import (only mentioned in the docstring,
  features.py:24-49 explaining its removal). Eager annotation evaluation restored.
- `model_config = {"frozen": True, "extra": "forbid"}` — features.py:170.
- Belt-and-suspenders `FeatureFrame.model_rebuild(force=True)` at module bottom — features.py:292
  (+ FeatureProvenance/FeatureSourceWindow rebuild, 293-294) — materialises the CoreSchema with
  `extra_fields_behavior='forbid'` before any external import can reach the class.

**T-199b (LatencyHop wire key "class") confirmed in source:**
- api_schemas.py:248 — `cls: str = Field(default="", alias="class", serialization_alias="class")`;
  `model_config = {"frozen": True, "populate_by_name": True}` (line 252). Matches FROZEN
  api-contracts.md §4 `/api/latency` wire key `"class"`.

**Dual G3 evidence (both reviewers ran commands, not trusted handoffs):**
- code-reviewer: 180 contracts pass; mutation `extra="forbid" → "ignore"` flipped 6 negatives RED
  (3 TestLabelColumnDisjointness + 3 TestLeakGuardMutationProof), reverted to byte-identical
  SHA256 `29bc85f4…`; `model_dump_json(by_alias=True)` emits `"class"`, `"cls"` absent. ruff+mypy clean.
- backtest-qa: 180 contracts pass across **14 PYTHONHASHSEED values**; 20-seed standalone-import proof
  shows `extra_fields_behavior=='forbid'` + `__pydantic_complete__==True` with **zero deviation**;
  own forbid→ignore mutation drove 6 negatives RED; direct `truth_*`/`label` injection rejected 5/5.
- **PROCESS NOTE (CI advisory, carried):** a transient isolated-file 6-fail was root-caused to a
  **stale mutant `.pyc`** from a forbid→ignore mutation, NOT a defect — never reproduced after cache
  clear. **Mitigation: pin `PYTHONHASHSEED` + add `PYTHONDONTWRITEBYTECODE=1` to the mutation-test CI
  step.** Full-directory CI run is unaffected. (Recorded for `latency-devops-engineer` at T-250/CI hardening.)

**Verdict: T-199a DONE, T-199b DONE → T-199 CLOSED.** Three-strikes ledger: NOT a 3rd content strike —
the prior dispatch died with no verdict; this is the dual re-review on the landed diff, both PASS.

---

## T-300 — M1 ingestion module — DONE (1 MAJOR filed forward, non-blocking)

**Files read:** `aats/ingestion/{registry,decoders,transport,bus,store}.py`,
`tests/ingestion/{test_decoders,test_point_in_time,test_bus,test_resilience}.py`.

**Confirmed in source / by both reviewers (independently re-run):**
- **No venue program-ID base58 literal in any hot/decode path** — IDs flow through the pluggable
  fail-closed `ProgramRegistry` only. (Only base58 literal is the universal wrapped-SOL mint constant.)
- **Point-in-time store correct:** partition key `event_date = date(event_time.block_time_ms)` (store.py),
  never wall-clock; `assert_recorded_at_honesty` rejects future-dated `recorded_at` (provenance taint);
  ShadowRecorder flushes open windows **CENSORED** on reconnect (survivorship-free, carried not dropped).
- Money fields int/Decimal with active float-rejecting validator. No live submit (bus is read-side only).
- Suite: **107 passed** (re-run by both reviewers, deterministic), ruff clean, mypy 0 errors (6 files).

**MAJOR filed (NON-BLOCKING for T-300 PASS) — carried to the transport-wiring task:**
- `decoders.py:_make_event_time` (lines 193-207) substitutes **wall-clock** into the AUTHORITATIVE
  on-chain `block_time_ms` anchor (C-5) when `tx.block_time_unix_s` is None/0 — a **compute-time leak**
  on the pre-confirmation (ShredStream) path, and `data_staleness_ms` collapses to ~0, hiding staleness.
  **CONFIRMED in source by orchestrator** (decoders.py:200-202: `block_time_ms = wall_ms`).
  Not blocking T-300 because the live transport that produces a None block_time is itself a disclosed
  **STUB** (GeyserTransport/EnhancedWsFallback are PLUG_IN_HERE). **MUST be fixed before any LIVE
  point-in-time/SHADOW corpus is recorded (R1).** → assigned to `data-ingestion-engineer` as a
  fix-task on the transport-wiring lane (new T-300a, see board); also gates T-400 clock-audit.
- MINOR (Raydium v4 init2 reserve mapping not byte-verified against a real mainnet sig) — engineer
  open-issue #2; must be confirmed before Raydium v4 reserves feed any live sizing.

**Verdict: DONE.** Store/decoder/bus point-in-time correctness is the gate's law and holds.

---

## T-304 — Feature engineering + FeatureFrame assembler — DONE

**Files read:** `aats/features/frame.py`, `tests/features/test_frame.py`,
`aats/features/microstructure.py`, `aats/contracts/provenance.py`.

**Point-in-time / no-leak confirmed in source (prior B1 BLOCKER + M1 MAJOR fixed):**
- **event_time consistency guard** — frame.py:165/192 (`FrameAssemblyError` on any upstream
  event_time mismatch).
- **first_k_slots consistency guard** — frame.py:204-226 (all modules compared against canonical
  buy_pressure.first_k_slots; K-mismatch raises).
- **Final post-merge per-window cutoff re-validation** via `check_feature_window_cutoff` — catches
  the TA path (which has no upstream provenance guard of its own).
- `smart_wallets_in` is a pure **count** feature — NEVER a buy trigger (frame.py:138); no
  sol_in/should_enter/entry_signal field is expressible anywhere on the frame.
- Both reviewers ran 5 adversarial probes beyond the test fixtures (event_time mismatch per-module,
  forged provenance past cutoff, K-mismatch, future TA bar, source-level future event) — all caught.
- Suite: **177 features tests passed**, ruff + mypy clean. Lookahead refused at three layers
  (observe-time, module to_features, assembler final re-validation).

**Verdict: DONE.** Lookahead refused on every feature; features match definitions; money int.

---

## T-305 — Buy-pressure / volume first-K-slot feature (C-4 baseline enabler) — DONE

**Files read:** `aats/features/buy_pressure.py`, `tests/features/test_buy_pressure.py`,
`aats/ingestion/decoders.py` (reserve=0 on create).

**Confirmed in source (prior B-1 BLOCKER + M-1 MAJOR fixed):**
- `classify_event_direction` now **raises `ClassificationUndefinedError` on zero-reserve anchor**
  (buy_pressure.py:485) — the real pump.fun decoder emits `sol_reserve_lamports=0` on create events,
  which previously silently classified everything as BUY (collapsing net pressure to gross volume).
- `build_buy_pressure_features` signature changed to `Sequence[tuple[LaunchEvent, bool]]`
  (buy_pressure.py:496) where `is_buy` comes from the **decode-time discriminator**
  (PUMP_DISC_BUY/SELL) — the feature layer no longer re-derives what the decoder knows.
- **Lower-bound guard:** `observe()` raises `FeatureWindowError` on `event_slot < event_time_slot`
  (buy_pressure.py:323-329) — pre-creation events rejected, window closed on both ends.
- **Mutation-meaningful (both reviewers ran their own mutations):** neutering the upper-bound guard →
  6 tests RED ("DID NOT RAISE"); re-introducing the all-BUY bug → 5 tests RED (net collapses to gross).
  The C-4 baseline feature **detects its own corruption**. Defense-in-depth: provenance guard
  independently raises on the batch path even with observe() guard off.
- Money: Decimal accumulator (exact cancellation), int volume; float rejected.
- Suite: **48 buy_pressure tests + 228 features+contracts regression** — all green.

**Verdict: DONE.** Net buy-pressure genuinely constructible for GATE-B (C-4). **API-change notice:**
T-300/T-340 callers must pass `Sequence[tuple[LaunchEvent, bool]]` with `is_buy` from the discriminator
(relayed below).

---

## T-323 — Sub-10ms pre-trade safety gate — DONE

**Files:** `aats/risk/pretrade_gate.py` (unchanged — fix was test-coverage-only),
`tests/risk/test_pretrade_gate_ac011_corpus.py`, `…_latency.py`.

**Confirmed by both reviewers (re-run + own mutation):**
- **AC-011 coverage closed:** new 20-distinct-token corpus (10 honeypots + 10 rugs); suite **MEASURES**
  `rejected_count == 20` (rejection_rate 1.0), asserts each reject carries a **logged** `red_flag_codes`
  reason that is a real on-chain flag (gate step 1-6), not an INPUT_* refuse-by-default.
- **<10ms MEASURED not asserted:** `time.perf_counter()` benchmark over n≈10240 → p50≈19µs, p99≈60µs
  vs 10ms budget; hot path proven 0-IO (ExplodingProbe), 0-RPC, 0-LLM.
- **Mutation-meaningful:** monkeypatching the gate to always-PASS / no-op `_check_one` / loosening the
  LP boundary each turned the corpus + boundary tests RED naming the exact token; production gate file
  restored git-clean.
- Money int bps only; point-in-time inputs (in-budget staleness); reject-only (no sizing surface).
- Suite: **165 risk tests passed** (142 baseline + 23 new). ruff clean, no secrets.

**Verdict: DONE.**

---

## T-324 — Risk rule engine + ¼-Kelly sizing + cost-aware entry gate — DONE

**Files read:** `aats/risk/{sizing,cost_model,rule_engine}.py`, `tests/risk/test_*` (6 files).

**Confirmed in source + by both reviewers (independent sweeps):**
- **Cost gate rejects iff `expected_edge_bps <= total_cost_bps`** (cost_model.py:13, REASON_EDGE_BELOW_COST
  at :194) — **equality REJECTS (no free trades)**; agrees with the EntryIntent contract validator.
  150bps UNCALIBRATED adverse-selection floor enforced **widen-only** (cost_model.py:55-58, :159);
  fail-closed on negative line / total≠sum / non-positive edge.
- **≤¼ Kelly binds across P 0.1-0.9** — independent sweep: applied fraction == EXACTLY 0.25×full_kelly
  (0.6→0.05 … 0.9→0.20), never exceeded; clamped in code independent of config (RiskConfig refuses >0.25,
  sizer re-clamps). 5000-9000-iteration property/fuzz tests show no cap breach.
- **No signal path increases size** — `DeRiskSignals` factors in (0,1]; signal>1.0 rejected at
  construction; 20,000 fuzzed cases never grew size above baseline; `RuleInputs` has no risk-increase field.
- **Asymmetric LLM trust** — engine holds only `DeRiskIntentFactory` (no `.entry` constructor);
  LLM reaches it ONLY as a pre-set bool flag → can only force a full exit; FAST decision path fully
  synchronous (zero async/await, no LLM call). Idempotency keyed on **event-time** block_time_ms.
- Money int lamports / Decimal bps, floor-toward-zero so rounding never overshoots a cap; no win_rate field.
- Suite: **237 risk tests passed** (165 baseline + 72 new), 0 regressions; ruff/mypy clean, no secrets.
- One MINOR (dead `CostGateRejection` class) + NITs — non-blocking.

**Verdict: DONE.**

---

## T-325 — TP-ladder + trailing + hard-stop + timeout ExitEngine — DONE

**Files read:** `aats/risk/{exit_engine,exit_sim}.py`, `tests/risk/test_exit_engine{,_ab,_property}.py`,
`aats/risk/survivable_stop.py`.

**Confirmed in source + by both reviewers (mutation-proven):**
- **Secure-MEV default (OQ-008)** — `default_exit_mode = MevExitMode.SECURE` (exit_engine.py:216);
  ALL defensive exits force Secure even on a Fast preset.
- **Trailing tightens, never widens** — `peak_r` ratchet (only updates if `r > peak_r`, line 390);
  `_replace` **refuses to grow `remaining_bps`** (lines 434-439, de-risk only). Mutation: making peak
  follow the mark DOWN → 3 tests RED (monotonicity invariant bites).
- **De-risk only** — emits ExitIntent/ReduceIntent/NO-OP, never EntryIntent; `remaining_bps` monotone
  non-increasing (fuzzed 3000× across all presets). Mutation: disabling the grow-guard → test RED.
- **Hard stop IS the T-321 StopState trigger** (verified identical line) — ladder and survivable stop
  cannot disagree.
- **A/B vs naive reproduces the documented lift** — independently re-run: ~984 post-gate candidates,
  ladder beats naive by **+25%** of |naive| (documented ~+24%), both arms net positive, deterministic;
  proven NOT an RNG artifact (lift holds with slippage zeroed → comes from the exit POLICY).
- **Asymmetric trust:** `narrative_failure` is a plain bool that can only force an exit (no size-up input).
- **No real-capital path:** no ExecutionVenue import, no network; FAST on_tick p99≈158µs (pure).
- Suite: **285 risk tests passed** (237 + 48 new), 0 regressions; ruff/format clean, no secrets.
- F1 (MINOR, latent — 1-bp rung rounds to 0 only on custom configs, unreachable via the 4 shipped presets)
  + NITs — non-blocking; routed to risk-guardrails next touch.

**Verdict: DONE.** NOTE: per-task G3 PASS only — does NOT constitute the G4 edge-vs-real-baseline
clearance (that is T-400/T-401).

---

## T-327 — Real `JitoJupiterVenue` behind the seam — DRY-RUN no-submit FIRST — DONE

**Files read:** `aats/execution/{jito_jupiter_venue,tx_builder,rpc_client,signer_client}.py`,
`tests/execution/{test_tx_builder,test_jito_jupiter_venue}.py`.

**DRY-RUN no-submit verified in source (orchestrator) + both reviewers (mutation):**
- **Triple DRY-RUN gate** (jito_jupiter_venue.py:155-173): `submit_mode` property requires
  `_live_submit_enabled AND _dry_run_env_disabled()`; default is `SubmitMode.DRY_RUN`.
- **`land()` and `_land_with_retry_entry` both short-circuit** to `LandResult(submitted=False,
  reason="dry_run")` BEFORE any network call (lines 594-607, 680-688). "There is NO code path in
  DRY_RUN that reaches the block engine (FR-039)" — confirmed; `send_transaction` only reachable past
  `_assert_live_allowed`.
- **Mutation (backtest-qa):** gutting all 3 DRY_RUN gates → `test_execute_dry_run_does_not_submit`
  FAILS ("DRY_RUN called send_transaction 1 time"). The no-submit invariant is load-bearing.
- **B1/B2/B3 prior blockers FIXED + each mutation-proven load-bearing:** B1 no hardcoded
  program-ID/tip-account literals, fail-CLOSED `VenueError` (CI guard test greps all aats/execution/*.py,
  zero literal matches); B2 simulated bytes == landed bytes (mutation → test FAILS); B3 fresh blockhash
  per retry, no byte-identical re-send (mutation → test FAILS).
- `sign()` delegates to an injected signer over a UDS (ADR-0009) — key never in-process. Money int.
  No win_rate, no key material in production code.
- Suite: **75 passed, 2 skipped** (2 skips are solders-gated build-path program-ID fail-CLOSED tests,
  environmental in offline CI). ruff clean.

**NOT VALIDATED (carried, out of scope):** the 2 solders-path program-ID fail-CLOSED tests must be
re-run with solders installed before any LIVE deployment. LIVE mainnet submission is structurally
unreachable here by design.

**Verdict: DONE.** DRY-RUN / no-submit proven first, as the safety-first build order requires.

---

## T-351 — Dashboard destructive-control tests (Lane E) — DONE (dual G3 closed)

**Files:** `dashboard/src/lib/api.destructive.test.ts`, `…/KillSwitch.test.tsx`,
`…/Settings.destructive.test.tsx`, `…/flatten.test.tsx`, `…/pages.render.test.tsx`.

**code-reviewer:** PASS (prior wave, mutation-proven). **backtest-qa-engineer (this wave):** PASS —
re-ran `npx vitest run --project dashboard` → **5 files / 24 tests passed**; re-proved
mutation-meaningfulness with 4 own mutants (kill mis-wired, confirm-gate bypassed, USE_MOCK network
leak, go-live confirm removed) — all caught; 3 touched files restored byte-identical.
- Endpoints pinned to FROZEN contract (api-contracts.md L202-219, 273-277): `/api/kill`, `/api/flatten`,
  `/api/flatten/{mint}`, `/api/breaker/reset`, `/api/mode`. Confirm-gating: kill + go-live route through
  AlertDialog (de-risk actions fire directly per contract asymmetry). Zero network in mock mode.

**NOT VALIDATED (scope-honest, → G4 T-402):** live transport against the running control-plane (D) /
Telegram (F); server-side mode-DOWN-only + breaker auth/TRIPPED gating (403/409) are server obligations.

**Verdict: DONE (G3 DUAL CLOSED).** Unblocks T-352.

---

## Full test suite

**Single-threaded consolidated run is a RUNTIME action** (this orchestrator pass runs no shell).
Procedure mandated to the Runtime, in order:
1. **Purge bytecode first** (the backtest-qa advisory: a stale forbid→ignore mutant `.pyc` can leak a
   false 6-fail into an isolated-file run): delete all `__pycache__` and `*.pyc` under `C:/dev/aats`.
2. `python -m pytest tests/ -q` (single worker — no `-n`/xdist) with `PYTHONHASHSEED` pinned.
3. Record the consolidated `passed / skipped / failed` count into this section.

**Per-module passing counts verified this wave (from reviewer re-runs, each on the landed tree):**
| Module | Tests | Result |
|---|---|---|
| `tests/contracts/` | 180 | PASS (green across 14 PYTHONHASHSEED values) |
| `tests/ingestion/` | 107 | PASS (deterministic, 2× re-run) |
| `tests/features/` | 177 | PASS (T-304 assembler suite; incl. T-305 buy_pressure 48) |
| `tests/risk/` | 285 | PASS (breaker/stop/DMS/pretrade/sizing/cost/rule/exit) |
| `tests/execution/` | 75 + 2 skipped | PASS (2 skips solders-gated, offline-CI environmental) |
| top-level (`test_dry_run_invariant`, `test_telemetry`) | — | run in the consolidated pass |
| `dashboard` (vitest, separate runner) | 24 | PASS (not part of `pytest tests/`) |

**Expected consolidated pytest result: ALL PASS, with the 2 solders-gated execution skips** (and any
top-level count). The Runtime's single `pytest tests/ -q` count is authoritative and must be pasted
above; if it deviates from green, re-open the affected task. Note module sub-runs overlap (e.g. the
228-count features run included contracts), so the consolidated total is the sum of distinct files,
not the sum of these rows.

---

## Verdict

**G3 WAVE M1+C1: PASS — all 9 tasks dual-PASS → DONE.** T-199 CLOSED. The live-capable execution path
(T-327) is proven DRY-RUN/no-submit and sits behind the three already-proven safety primitives
(T-320/321/322). Point-in-time / no-leak holds across every feature path (T-300 store, T-304 assembler,
T-305 buy-pressure). C-4 naive-momentum baseline feature is now CONSTRUCTIBLE.

**Carried forward (NON-BLOCKING for this gate, tracked on the board):**
1. **T-300a (new):** fix `_make_event_time` wall-clock substitution (compute-time leak) before any LIVE
   SHADOW/R1 corpus — owner `data-ingestion-engineer`; also gates T-400 clock-audit (C-5).
2. **CI hardening:** pin `PYTHONHASHSEED` + `PYTHONDONTWRITEBYTECODE=1` on the mutation-test step —
   owner `latency-devops-engineer` (T-250/CI).
3. **API-change notice:** `build_buy_pressure_features` now takes `Sequence[tuple[LaunchEvent, bool]]`
   — T-300/T-340 callers must pass decode-time `is_buy`.
4. **Pre-LIVE:** re-run the 2 solders-gated T-327 program-ID fail-CLOSED tests with solders installed;
   byte-verify Raydium v4 init2 reserve mapping (T-300 MINOR).
