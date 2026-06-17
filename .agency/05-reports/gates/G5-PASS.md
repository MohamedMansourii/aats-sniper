# GATE G5 — Release — VERDICT: PASS

**Gate:** G5 (Release)
**Approver:** `orchestrator` (Delivery Lead)
**Date:** 2026-06-17
**Tasks gated:** T-500 (one-command deploy + colocation/RPC plan), T-501 (docs/runbooks), G4-fixes (COND-G4-1 + T-402-F1)
**Verdict:** **PASS** — PAPER release. Real capital DISABLED by default (DRY_RUN_ENABLED=true). Live edge UNPROVEN-NO-REAL-DATA (correct, accepted PAPER outcome). R3/LIVE checklist (COND-G4-2) remains a documented HARD precondition before `DRY_RUN_ENABLED=false`, not a PAPER blocker.

Method: every criterion checked against the ACTUAL files under `C:/dev/aats` (read, not trusted from handoff JSON). Suite-execution evidence is the three independent full-suite runs recorded in the two G4-fixes `code-reviewer` re-reviews (1842 passed / 2 skipped / 0 failed, deterministic ×3 each) plus their mutation proofs. See §5 for the execution-context note.

---

## 1. G5 criteria (charter §4) vs evidence

| Criterion | Evidence (file path) | Verdict |
|---|---|---|
| One `docker compose up` validated + documented | `docker-compose.yml` — single file, 11 services, dependency-ordered `depends_on`; `x-common-env DRY_RUN_ENABLED:-true` (line 51); validated `docker compose config --quiet` exit 0 (G5-EVIDENCE §1.1, re-run by code-reviewer on Docker 29.2.0). One-command path documented in `README.md` §2, `docs/deploy-ops-guide.md` §2, `deploy/colocation-rpc-plan.md` §8. | PASS |
| Deploy verified by devops | T-500 dual-PASS (`latency-devops-engineer` build self-check exit 0 + `code-reviewer` PASS, blocking=[]). Rendered-config parse (authoritative, not grep): 11 services, redis & signer NO published ports, DRY_RUN_ENABLED=true on all 5 tx-capable services. | PASS |
| Startup self-check fail-closes on live path | `scripts/startup-self-check.sh` — `DRY_RUN_ENABLED=false` without `PRE_LIVE_CHECKLIST_SIGNED=yes` → `fail()` → exit 1 (line 80-81); verified exit 1 by code-reviewer, exit 0 in default sim. | PASS |
| Docs/runbooks complete | `README.md`, `docs/deploy-ops-guide.md`, `docs/dashboard-operator-guide.md`, `docs/telegram-operator-guide.md`, `docs/kill-switch-runbook.md`, `docs/pre-live-checklist.md` (all referenced + resolving per T-501 review link-check). | PASS |
| Docs honest: no win-rate | README §4 ("no win-rate target, field, or panel anywhere"); deploy-ops §7 ("There is no win-rate panel anywhere"); repo-wide grep over `*.md` returns no win-rate metric claim (T-501 review). | PASS |
| Docs honest: edge unproven | README §1/§4 (`UNPROVEN-NO-REAL-DATA`, GATE-A + GATE-B on RECORDED data only); deploy-ops §1/§6. | PASS |
| Docs honest: real capital disabled | README §8 + §6 table (`submit_mode=DRY_RUN`); deploy-ops §5 (three independent gates); `docker-compose.yml` DRY-RUN default. | PASS |
| Staged-rollout ladder + pre-live checklist | deploy-ops §6 (R0–R4 ladder, gate at every rung); `deploy/colocation-rpc-plan.md` §6 (Pre-Live Checklist, `PRE_LIVE_CHECKLIST_SIGNED=yes` gate); `docs/pre-live-checklist.md`. | PASS |
| Colocation/RPC plan honest + traceable | `deploy/colocation-rpc-plan.md` — DETECTION-COMPETITIVE / SUBMISSION-DISADVANTAGED in plain numbers (~67ms p50 internal; ~55ms→~1-5ms colo block-engine RTT; ~450ms p99 staked-lane slip; 17%/83% SWQoS), traceable to `latency-budget.md`; "colo does not close the staked-lane gap" caveat stated. | PASS |
| No secrets in tracked files | Independent secret scan (G5-EVIDENCE §5 + code-reviewer Grep): only pattern-definition / test-assertion hits; `.env` gitignored, not tracked; `.env.example` placeholders only. | PASS |
| G4 carry-forward COND-G4-1 fixed (hermetic concurrent test) | `tests/controller/test_snipe_handoff.py:146` `frozen_clock = lambda: 0` injected into `InMemoryStateStore(clock=...)`; `aats/controller/state.py:186/216/219/224` clock param is production-supported; `0 >= 30000` False forever → lock-TTL-expiry impossible during storm. Single-winner invariant mutation-proven RED by both reviewers (mutated `state.py:234`/`claim_entering`, then reverted). | PASS |
| G4 carry-forward T-402-F1 fixed (breaker projection) | `aats/controller/fast_loop.py:385` `self._store.save_breaker_state(new_breaker_state)` after `record_pnl`; read paths consistent (`server.py:492/705` + `snipe_loop.py:179` all read `load_breaker_state()`). Mutation-proven: commenting line 385 turns `test_t402_f1_breaker_projected_to_state_store` RED (both reviewers, byte-restored). | PASS |
| Consolidated suite green incl. concurrent test | 1842 passed / 2 skipped / 0 failed — three deterministic full-suite runs in EACH of the two G4-fixes code-reviewer re-reviews (bit-identical counts), incl. 8x-concurrent-load repro of the original COND-G4-1 flake (held). The 2 skips are the documented solders-gated execution skips (`tests/execution/test_tx_builder.py:161/:186`). | PASS (by recorded execution; see §5) |

---

## 2. T-500 — one-command deploy + colocation/RPC plan

**Status: DONE.** Dual gate cleared (build self-check exit 0 + `code-reviewer` PASS, blocking=[]).

- `docker compose config` → 11 services, validated on a host with Docker 29.2.0 (rendered, not grepped).
- DRY-RUN is the default on all 5 tx-capable services (controlplane, dms, hotcore, slow, telegram) — confirmed from the RENDERED config.
- Redis (6379) and per-service metrics (9101-9106) NOT host-published; only 8787/3000/9090/3001/9093 exposed. `aats-signer` has NO `ports:` (ADR-0009 conformant).
- Startup self-check is fail-closed on the live path (exit 1) and exits 0 in the default sim config.
- Colocation plan is honest and traceable to `latency-budget.md`.

**Documented OPEN, correctly OUT of PAPER scope (HARD R3/LIVE blockers, carried into COND-G4-2):**
- F-01: `aats-signer` three signer-side refusals unimplemented (scaffold); healthcheck disabled (`docker-compose.yml:119`). No real signing needed under DRY_RUN. **Must be built + test-proven before `DRY_RUN_ENABLED=false`.**
- F-10: `Dockerfile.hotcore/.signer/.dashboard` carry `@sha256:placeholder` build-time digests. **Must be replaced with verified digests on the target host before R3.**
- F-07: host hardening not yet applied (R3).
- Third-party compose digests (prometheus/grafana/alertmanager): documented 2026-06-16 amd64 digests; operator must verify on target host (R3).

---

## 3. T-501 — docs/runbooks

**Status: DONE.** Dual gate cleared on attempt 2 (`code-reviewer` re-review PASS, blocking=[]).

The attempt-1 BLOCKER R-501-01 (false verified-output: `pytest tests/risk` documented as 337 passed; inverted "in the 337 above" footnote on the validation row) is FULLY FIXED and re-verified by re-execution: `tests/risk`=315, `tests/validation`=22, combined=337. Every printed command in README §6 + deploy-ops §10 now matches reality. Repo-wide grep over `*.md` shows no remaining 337 test-count claim. No `aats/` or `contracts/` touched. Honest framing intact.

---

## 4. G4-fixes — COND-G4-1 + T-402-F1 (+ QA-G4FIX-1/-2)

**Status: DONE.** Dual gate cleared on attempt 2 (TWO independent `code-reviewer` re-reviews, both PASS, blocking=[]).

Three fixes verified in the ACTUAL files and independently mutation-proven by both reviewers (they mutated production code RED, then byte-restored — diff clean):
- COND-G4-1 frozen-clock fix (`test_snipe_handoff.py:146` + production-supported `state.py` clock seam) — root cause eliminated, not papered over.
- T-402-F1 breaker StateStore projection (`fast_loop.py:385`, production change) — de-risk-only read-path; drives the REAL `FastLoop.tick()` path in the covering test.
- QA-G4FIX-1 latency-test CPU-time fix (`test_t342_enforcer.py`) — defensible (log I/O off the CPU-budget critical path; the true wall-clock AC-026 gate preserved by the unchanged single-position test).

**Noted, non-blocking:** the G4-fixes ENGINEER handoff JSON under-declared its changeset (claimed "sole change is a test-layer fix … no production code modified" while the task also included the `fast_loop.py:385` production change + the `test_snipe_handoff.py` change). Both code-reviewers caught and flagged this; all three changes were inspected and PASS regardless. Recorded for the audit trail.

---

## 5. Execution-context note (honesty)

The orchestrator ran this verification in a **read/verify-only context with no shell available** — I could NOT execute the consolidated suite myself under the requested COND-G4-1 repro command (`find … __pycache__ … PYTHONDONTWRITEBYTECODE=1 PYTHONHASHSEED=0 python -m pytest tests/ -q -p no:cacheprovider --tb=no`). The Runtime must run that command once to refresh the recorded count.

The GREEN verdict rests on **recorded execution by the dual reviewers**, which IS first-hand execution evidence per the charter:
- Both G4-fixes `code-reviewer` re-reviews ran `pytest tests/ -q` to **1842 passed / 2 skipped / 0 failed**, three consecutive deterministic runs each, bit-identical counts.
- The original COND-G4-1 flake repro (8x concurrent `pytest tests/controller`) was re-run and HELD (111 passed × 8 workers, single-winner assertion never failed).
- Both reviewers mutation-proved the single-winner and breaker-projection invariants RED by their own probes, then restored production byte-for-byte.

**Doc-staleness finding (non-blocking, queued for T-600):** `README.md` §6 line 215 states the consolidated suite is "proven stable at 1803 passed / 2 skipped / 0 failed" citing `G3-stabilization.md` — a pre-G4-fix artifact. After G4-fixes the count is **1842 / 2 / 0** (the breaker-projection production change + the projection test + the frozen-clock test). The 1803 figure is internally consistent with the dated artifact it cites and is in the same class as the historical-`.agency/` counts T-501 explicitly scoped out — it is NOT a false per-command verified-output (all README §6 command/result pairs match: tests/risk=315, tests/validation=22, tests/execution=171, e2e=16). Refresh the 1803→1842 prose figure when assembling the T-600 delivery package.

---

## 6. Verdict

**G5: PASS.** One `docker compose up` is validated + documented; docs/runbooks are complete + honest (no win-rate, edge UNPROVEN, real capital disabled, staged-rollout ladder + pre-live checklist); both G4 carry-forwards (COND-G4-1 hermetic concurrent test, T-402-F1 breaker projection) are fixed and mutation-proven; the consolidated suite is GREEN at 1842/2/0 per recorded dual-reviewer execution incl. the concurrent test now hermetic.

**Conditions carried (NOT G5 blockers):**
1. COND-G4-2 R3/LIVE HARD checklist (F-01 signer refusals, F-10 digests, F-07 host hardening, F-02/03/04 supply-chain) — must clear before `DRY_RUN_ENABLED=false`. Documented in `deploy/colocation-rpc-plan.md` §6 + `docs/pre-live-checklist.md`.
2. Runtime to execute the COND-G4-1 repro suite command once to refresh the recorded count to 1842/2/0.
3. T-600 to refresh the README §6 1803→1842 prose figure.

**Stage advances → G6 (Acceptance).** Next: T-600 delivery package + CEO sign-off, then the E1–E13 enhancement program (per CEO reorder, `ENHANCEMENT-DIRECTIVE-E1-E13.md` §ORDERING). Real capital stays DISABLED behind DRY-RUN through P6.
