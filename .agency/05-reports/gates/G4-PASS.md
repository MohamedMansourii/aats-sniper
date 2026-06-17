# GATE G4 — INTEGRATION — VERDICT: **PASS (conditional)**

_Recorded 2026-06-16 by `orchestrator`. Verified by reading the ACTUAL reports under
`C:/dev/aats/.agency/05-reports/` and spot-checking the cited SOURCE (`aats/models/gate_a.py`,
`tests/validation/*`, `aats/controller/snipe_loop.py:179`), not by trusting the handoff JSON.
G4 is an orchestrator-approved gate (no CEO pause — `AUTONOMY-DIRECTIVE.md`)._

**Headline:** G4 PASSES. The leak/clock foundation is sound and proven NON-VACUOUS; the edge
harness is BUILT and COMPUTES CORRECTLY; the e2e PAPER operator demo PASSES (kill flattens in
budget, de-risk-only, all three safety layers fire); the security audit's core controls
(secrets, Telegram authz, DRY-RUN, prompt-injection) PASS. **`edgeVerdict = UNPROVEN-NO-REAL-DATA`
is the CORRECT, honest, ACCEPTABLE outcome for a PAPER deliverable** — G4 does not require proven
edge, it requires the harness + honest characterization + no leak. Real capital stays
DRY-RUN-disabled. Two MAJOR items are carried as G4-remediation/R3-precondition conditions, not
G4 blockers (neither is a leak, a broken harness, a failed safety path, or a live security hole).

---

## 1. The G4 pass bar (charter §4 + the project's explicit G4 rule) vs evidence

| Criterion | Verdict | Source-confirmed evidence |
|---|---|---|
| Leak/clock/group-purge audits SOUND + non-vacuous | **PASS** | T-400: all 4 ADR-0010 provenance/load guards (per-feature cutoff, lineage-taint, column-disjointness, recorded_at-honesty) are real functions, CALLED in the live pipeline (`training.py:296` + `survivor.py:474` before fit; `store.py` on every write), and RAISE on planted leaks re-run this session. Model-side `assert_no_label_taint` raises on planted `truth_*`/`realized_mult`/`fwd_return`/`survived_60s`/`label`. Training-wired `assert_event_time_leq_decision` RAISES when a feature event_time is shifted +5 slots past the decision anchor. Join is event-time-only (`(slot,block_time_ms)`, wall_clock excluded). No `truth_*` field on any production model; zero `sniper_sim` imports inside `aats/`. T-300a clock fix re-verified: `_make_event_time` returns None for absent block_time (NO wall-clock substitution), 33 tests PASS. 214 focused leak/clock/gate tests PASS. C-6 survivorship MEASURED (Wilson-upper-bound), 65 tests PASS. |
| Consolidated suite STABLE | **PASS (with 1 carried test-hygiene defect)** | Suite proven stable at G3-stabilization (1803/2/0 ×10 identical). T-400 reproduced ONE failure: `test_concurrent_thousand_snipes_one_winner` (non-hermetic). NOT a leak/harness/safety/security FAIL — see §2.1. T-401 saw it PASS 5/5 in isolation (load/box-dependent). Full suite minus this test: 1840 passed / 2 skipped. |
| Edge HARNESS BUILT + computes correctly (proven edge NOT required) | **PASS** | T-401: source-verified on disk — `aats/models/gate_a.py` (substantive: aggregate net-of-cost PnL, seeded lower-95% bootstrap bound, `gate_a_pass iff bound>0`, declined→0, NO win-rate field, fail-closed on empty, deterministic) + `tests/validation/{__init__,harness,test_edge_gate_proof,test_clean_room_import_guard}.py`. Right SIGN on both controls: oracle model-WINS GATE-A +104.63 SOL / GATE-B delta +0.40 (both lower-95% >0 → PASS); anti-oracle model-LOSES GATE-A -289.08 SOL / GATE-B delta -0.58 → FAIL. Declined trade contributes 0 (proven). Net-of-cost (310 bps stack incl. 150 bps widen-only haircut). Clean-room AST/import guard non-vacuous; purge load-bearing. tests/validation 22 PASS; models+validation 118 PASS; gate_b 15 PASS; provenance+truth guards 46 PASS. |
| edgeVerdict acceptable | **PASS** | `UNPROVEN-NO-REAL-DATA` is the CORRECT honest outcome: no recorded mainnet data exists (ingestion has SHADOW/RECORD but no live feed); every corpus is `is_bootstrap_not_real` synthetic; the lone model>baseline number is a beatable-by-design smoke test, explicitly NOT edge. No edge/win-rate targeted, tuned-toward, or fabricated. Matches `GO-PAPER-ONLY`. |
| e2e operator demo PASSES (kill in budget, de-risk-only, safety fires) | **PASS** | T-402: PASS — `tests/e2e/test_t402_operator_demo.py` 16 tests green, deterministic. KILL flattens open book <2s (AC-040) from BOTH dashboard AND Telegram (same FROZEN contract, real confirm-nonce flow); non-vacuity proven (exit_calls empty pre-kill, populated only by flatten). MODE propagates to `/api/state`. SSE `/api/feed` carries a real `provenance:live_controller` frame ≤3s (NOT mock). Breaker + Layer-2 survivable-stop + Layer-3 DMS each fire on demand (DMS fires when FAST loop killed past T_DMS). Risk-increase (mode-up-to-LIVE behind DRY-RUN → 403, risk-config widen → 403, no-auth → 403) rejected; Telegram seam structurally de-risk-only. No win-rate; edge gates assert NOT passing. |
| Security: no secrets, custody sound, allowlist on signing, prompt-injection de-risk-only | **PASS (core) / conditional (custody impl)** | T-403: SECRETS PASS (detect-secrets + manual base58/BIP-39/keypair/token sweeps over tree AND history; `.env.example` placeholders-only + FORBIDDEN banner; `.gitignore`/`.dockerignore` exclude all secret patterns). TELEGRAM AUTHZ PASS (operator-ID-only first gate, fail-closed empty allowlist, closed de-risk-only command set, per-command single-use confirm). DRY-RUN PASS (3 venue gates + control-plane CEO-auth-AND-`DRY_RUN_ENABLED=false`; no real submit path reachable). PROMPT-INJECTION PASS (narrative is quoted untrusted data; `ReasoningAction` has no risk-increase member — type-inexpressible; clamp takes stronger of {LLM de-risk, quant ceiling}; adversarial sweep yielded only HOLD/VETO_ENTRY). 452 tests pass. Custody allowlist-on-signing is policy/data-PASS but enforcement is unbuilt (F-01) — latent, see §2.2. |

**Verdict: PASS (conditional).** Every G4 criterion is met. The only items short of a clean
unconditional pass are two MAJOR conditions that — per the project's explicit G4 rule — are NOT
in the FAIL set (a FAIL is only a real leak, a broken harness, a failed e2e/safety path, or a
live security finding).

---

## 2. The two conditions (carried, not blocking)

### 2.1 COND-G4-1 — non-hermetic concurrent test (test hygiene, NOT a safety/leak/harness fail)
`tests/controller/test_snipe_handoff.py::test_concurrent_thousand_snipes_one_winner` is
NON-HERMETIC: `InMemoryStateStore.claim_entering` (`state.py`) holds a 30s wall-clock lock TTL;
the 1000-OS-thread storm takes 80–155s on this box, so thread #1's lock TTL-expires mid-storm and
a second claim LEGITIMATELY wins (`assert 2==1`). T-400 reproduced 5/5 in isolation; T-402 saw 5
winners once; T-401 saw it PASS 5/5 (load/box-dependent flake). **Production logic is defensible**
(stale-lock re-entry after TTL is intended). This is a test-hygiene defect, not one of the four
G4-FAIL classes. **Owner:** `agent-orchestration-engineer`. **Remediation:** freeze the injectable
clock in `InMemoryStateStore` (or scope the assertion to the lock lifetime) so the TTL cannot
elapse during the storm. **Re-entry:** the test passes hermetically under load. Carried into G5.

### 2.2 COND-G4-2 — signer-side custody enforcement is an unbuilt scaffold (R3/LIVE precondition)
T-403 F-01 (HIGH-latent): `rust/aats-signer/src/main.rs` is a scaffold — the three signer-side
refusals (SOL spend cap, program-ID allowlist enforcement, Jito-tip transfer pin) plus
Vault/`mlock`/zeroize secret handling are SPECIFIED but UNIMPLEMENTED. The ADR-0009 CLIENT seam is
correct (hot core holds only the pubkey; `sign()` crosses the Unix-socket boundary; `SignerRefused`
aborts) and `config/program-allowlist.json` is correct least-privilege fail-closed data — but no
running code parses tx net SOL outflow, checks program IDs, or pins recipients. **Latent, not a
present-tense drain path:** LIVE is hard-gated off (DRY-RUN default + 3 independent gates +
unfunded wallet); the audit found NO open exploitable CRITICAL/HIGH in this offline build. Plus
F-10 (HIGH-for-LIVE: `Dockerfile.signer` `@sha256:placeholder` digests) and F-02/F-03/F-04
(MEDIUM/LOW: hash-lock deps, add pip-audit/OSV CVE gate, pin GH Actions to SHAs). **None gate the
PAPER deliverable.** **HARD BLOCKING CHECKLIST before `DRY_RUN_ENABLED=false` is ever set (R3):**
F-01 (build + test-prove the signer refuses an over-cap and an off-allowlist tx — T-251/T-352a),
F-10, F-07. **Owners:** `crypto-security-engineer` (F-01 refusals), `latency-devops-engineer`
(F-10/F-07 image+host hardening). These thread into the E-program / R3 capital-staging gate.

---

## 3. Carry-forward observability finding (MAJOR, non-blocking for PAPER G4)
T-402-F1 (source-confirmed at `snipe_loop.py:179`): the real `CircuitBreaker` persists only to its
own `BreakerStore` and is never projected into the `StateStore`, so `GET /api/state` + `/api/metrics`
+ the SNIPE-loop entry read (`load_breaker_state()`) can show `breaker_tripped=False` while the bot
is actually halted and flat after a real trip (the in-process `breaker.entries_allowed()` DOES block,
so it is an observability + dual-source-of-truth defect, not an unsafe trade path). **Owner:**
`agent-orchestration-engineer` / `solana-systems-architect` — single source of breaker truth (shared
store or projection-writer on every trip/reset) required before the LIVE surface. Carried into G5.
(T-402-F3 MINOR: `test_t342_enforcer` 100-position latency-budget test is load-flaky — widen
tolerance / pin to a quiet run. Owner: `mev-latency-engineer`.)

---

## 4. Honesty ledger (the deliverable, upheld across all four tasks)
NO live edge is or can be proven in this offline build. There is NO recorded mainnet data; every
corpus is `is_bootstrap_not_real` synthetic; the only "model>baseline" number is AUC/PnL on a
corpus engineered to be beatable — a pipeline smoke test, NOT edge, NOT a capital license. GATE-A/
GATE-B on RECORDED data cannot be computed because no recorded data exists. Real capital stays
DRY-RUN-disabled. No agent targeted, tuned toward, or fabricated a passing edge or win-rate; the
honest absence is itself the deliverable. This is exactly `GO-PAPER-ONLY` re-confirmed.

Honest GAPS reported (NOT credited, T-401 scope deferred to recorded-data R1 + the harness
buildout): C-9 experiment-log/trial-count deflation, C-11 calibrated-haircut sub-gate, C-3/C-13
tip-contention + independent-surface stratification, C-5 global-clock-shift bootstrap control,
C-10 group-aware purge engine (fingerprints carried on `LaunchEvent` but unused by any purge),
≥5-window CPCV with per-window CIs, SimulationVenue depth-based cost burn-in. None can change the
verdict — no recorded data exists to run them on. These are the recorded-data validation program,
carried as R1/E-program work, not a G4 defect.

---

## 5. Decision
**G4 = PASS (conditional).** Stage advances to **G5 (Release)**.
- Conditions COND-G4-1 (non-hermetic concurrent test → `agent-orchestration-engineer`) and
  T-402-F1 (breaker projection → `agent-orchestration-engineer`/`solana-systems-architect`) are
  carried as G5-entry remediation (PAPER-non-blocking; the production safety logic is sound).
- COND-G4-2 (F-01 signer refusals + F-10/F-07 image/host hardening + F-02/F-03/F-04 supply chain)
  is the **hard blocking checklist before `DRY_RUN_ENABLED=false` (R3/LIVE)** — it does NOT gate
  the PAPER G5/G6 path but MUST be cleared before any real-capital promotion.
- Real capital stays DISABLED behind DRY-RUN through P6.

**Tasks:** T-400 **DONE (FINDINGS)** · T-401 **DONE (UNPROVEN-NO-REAL-DATA, harness built+correct)**
· T-402 **DONE (PASS)** · T-403 **DONE (FINDINGS — core PASS, custody-impl conditional)**.
