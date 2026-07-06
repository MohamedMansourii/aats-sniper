# AATS — STATE.md · MASTER RESUME SPINE

> **READ THIS FILE FIRST.** Single source of truth for resuming the AATS elite-enhancement program across a
> model switch. **You are the SAME worker continuing** — do not restart. Pick up at **§2 NEXT ACTIONS**.
> This file is AUTHORITATIVE for live status; the companion docs under `.agency/04-plan/` are being reconciled
> to it (they lagged a wave — see the 2026-07-03 review) so trust THIS file on any conflict.

**Updated:** 2026-07-06 (edge proof RAN→NO-GO; corpus collector live). **Branch:** `aats-sniper-build` ·
**HEAD:** `ba05fc9` (verify `git log -1`). **Full suite:** `3142 passed / 0 failed`.
**✅ Honest status:** SAFETY SPINE airtight; Wave-2 exits WIRED LIVE + E2E-proven; Wave 3 (Phase 2 regime
architecture) DONE 4/5 — CP-07 → Codex WP#1. Exec/custody **security audit DONE = PASS-WITH-CONDITIONS**
(`.agency/05-reports/security/EXEC-CUSTODY-AUDIT-2026-07-06.md`; go-live blocker = build the real signer).
**⭐ EDGE PROOF EXECUTED 2026-07-06 → NO-GO / UNPROVEN-NO-REAL-DATA** (GATE-A fail-closed on 0 outcomes; never
fabricates — `.agency/05-reports/qa/EDGE-PROOF-2026-07-06.md`). **The in-repo PumpPortal recorder is bugged**
(banks ~0; feed+RPC proven healthy) → bypassed by a WORKING standalone **labeled-corpus collector**
(`C:/aats_shadow/_collector.py`, detached PID varies, entry+forward-outcome at 1m/5m/15m). Corpus now accruing
autonomously. Reviews + ledger: `.agency/05-reports/`.

---

## 0 · WHO YOU ARE / HOW TO OPERATE
- You are the **Agency Runtime** (top-level session) per `C:\dev\aats\CLAUDE.md`. You **dispatch** specialized
  subagents and enforce gates; **you write NO production code/specs/tests yourself.**
- **Ultracode on:** run a **Workflow** per wave; adversarially dual-G3 everything; correctness > cost.
- **Every wave = one sequential dual-G3 Workflow:** per item → build (owner) → `code-reviewer` **and**
  `backtest-qa-engineer` in parallel → fix-loop (≤3). Items one-at-a-time (no overlapping-file races).
- **After each wave:** *you* run the full suite yourself, commit + push, **reconcile the companion docs to STATE**,
  and **write the dual-G3 acceptance artifact** under `.agency/05-reports/{review,qa}/` (the review found these
  were missing — do not claim "accepted" without the artifact). Guards (keep them): build/fix agents must NOT run
  git; reviewers must do NON-DESTRUCTIVE review.

## 1 · PURPOSE + STOP CONDITION
Elite (competitor-parity), de-risk-only, then the **edge-proof gate**. Stop when all missions are G3-PASS **and**
integrated-live **and** GATE-A/GATE-B on the RECORDED corpus yield **GO/NO-GO**. Real capital stays
`DRY_RUN`-disabled until the CEO authorizes.

## 2 · NEXT ACTIONS — do these in order
1. **✅ Wave 2C DONE** (`7722294`) — Wave-2 catastrophic exits now WIRED LIVE (`SlowLoopEnrichmentWiring` drives the
   producers; E19 StateStore + fast_loop added; `test_e2e_catastrophic_exits.py` with a control test proves each
   exit fires; classify_direction negation fixed). **Milestone B is now genuinely LIVE-WIRED.** ▸ NEXT CLEANUP
   (small): reconcile MISSION-BOARD/MILESTONES to this STATE, and backfill per-item dual-G3 acceptance artifacts for
   EN1/Wave-1/E15/E16/E18/E19 — ✅ DONE, see `.agency/05-reports/review/ACCEPTANCE-LEDGER.md`. One OPEN
   decision: whether the Wave-2B ENTRY gates (deployer-rep/funding-age/lp-unlock-entry) wire into `snipe_loop` now
   or stay paper-neutral — currently paper-neutral (document if you change it).
2. **✅ Wave 3 DONE** (`1eead58`, 4/5 G3-PASS, suite 3142). **CP-07 (creator-outflow) fix = HYBRID:** Codex builds
   it (`.agency/04-plan/codex-work-packages/CP-07-creator-outflow-fix.md`), then Claude runs the dual-G3 review-gate
   on the result before committing (maker=Codex, checker=Claude — never Codex grading itself). Regime model
   TRAINING + M2-CP-03/05/06 stay DATA-GATED (≥3,000 recorded launches).
2b. **Wave 4 — detection completeness** (next Claude wave): `Workflow({scriptPath:"C:\\dev\\aats\\.agency\\04-plan\\workflows\\wave4-detection.js"})`.
3. **Wave 4 — detection completeness:** `Workflow({scriptPath:"C:\\dev\\aats\\.agency\\04-plan\\workflows\\wave4-detection.js"})`.
4. **✅ Exec/custody security audit DONE** — PASS-WITH-CONDITIONS (`.agency/05-reports/security/EXEC-CUSTODY-AUDIT-2026-07-06.md`).
   Go-live blocker: build the real isolated `aats-signer` (currently scaffold) + dep hash-lock/pip-audit. Post-edge-proof work.
5. **⭐ MILESTONE E — the EDGE PROOF (the gate). EXECUTED 2026-07-06 → NO-GO / UNPROVEN-NO-REAL-DATA** (no resolved
   outcomes yet). **The remaining path to a REAL verdict (in priority):**
   - a) **Corpus** — the standalone collector (`C:/aats_shadow/_collector.py`) is accruing labeled data (entry+forward
     outcome) autonomously. Restart if the PC rebooted: `python C:/aats_shadow/_collector.py` (detached). **Bitquery
     archival is the FASTER path** (instant resolved history) — plan: `.agency/04-plan/PHASE-5-DATA-PLAN-bitquery.md`.
   - b) **Outcome-labeling harness** = Codex WP#3 (`.agency/04-plan/codex-work-packages/OUTCOME-LABELING-HARNESS.md`) —
     resolve the corpus into `TradeOutcome` records (leak-safe; Claude's `backtest-qa` gate is MANDATORY). NOTE: the
     WP reads the recorder's `snapshots.jsonl`; the live collector emits `labeled_corpus.jsonl` ({entry,forward}) —
     reconcile the reader to the collector format (or enrich collector entries with first-K-slot microstructure for the
     FULL feature set; current collector entries are THIN = PumpPortal create fields only).
   - c) **Re-run GATE-A/GATE-B** on the resolved corpus → the FIRST real GO/NO-GO with actual scoreboard numbers.
   Nothing lifts `DRY_RUN` before a GO + owner authorization. 3 Codex packages ready: CP-07, Wave-4, outcome-harness.

## 3 · DONE LEDGER (verified + committed, newest first)
| Commit | What | Honest note |
|---|---|---|
| `7722294` | **Wave 2C** — Wave-2 catastrophic exits WIRED LIVE + E2E proof + classify_direction fix | ✅ Milestone B now genuinely live-wired; suite 2965 |
| `9441359` | loop-governance install (LOOP.md + budget + run-log + control-plane reg) | loop-engineering PARTIAL→governed |
| `a6a85ad` | program review recorded + STATE overclaims corrected | — |
| `5209933` | STATE resume-spine update | — |
| `abf2477` | E18 min-holder floor (WIRED) + E19 LP-unlock code | ⚠️ E19 open-position exit **NOT live-wired** (Wave 2C); E18 gate IS wired |
| `1c70513` | E15 serial-deployer rep + E16 fresh-wallet | gates built + unit-tested; **entry-gate wiring into snipe_loop pending** (Wave 2C decision) |
| `fbf4731` | Wave 2A — E14a/E14b insider-dump + E17 sellability | branches correct + tested; **flag producers NOT wired into live loop** (Wave 2C) |
| `cce5440` | Wave 1 — alpha engine | velocity-sidecar + Telethon + live smart-wallet WIRED; **caller-score/CA half built-but-unwired** |
| `7faccf5` | EN1 caller-score de-risk guard survives `python -O` | fully done |
> E16 review-gate PASSED clean (`wf_642d4457-982`); the program review (`wf_459f6ac8-685`, 2026-07-03) is
> recorded at `.agency/05-reports/review/PROGRAM-REVIEW-2026-07-03.md`.

## 4 · INVARIANTS (never violate — the safety law) — AUDITED AIRTIGHT 2026-07-03
1. No win-rate metric (success = net-of-cost PnL + model-vs-baseline on RECORDED data).
2. `DRY_RUN` disabled until edge proven AND CEO authorizes.
3. Asymmetric trust — signals ONLY de-risk (reject/shrink/down-weight≤1/exit); never size up/widen/raise/leverage.
4. LLM/heavy models never on FAST/SNIPE (SLOW-loop; hot branch reads a pre-set flag).
5. Point-in-time (T-300a): on-chain event-time only; no wall-clock in decision fields; no lookahead.
6. Money int lamports/Decimal; no secrets in code/logs; custody = capped hot wallet + isolated Vault signer.
7. Dual-G3 mandatory (code-reviewer AND backtest-qa PASS) **+ write the acceptance artifact** under `.agency/05-reports/`. Three strikes → HALT the wave + escalate (do not silently continue).
8. **STANDING E2E GATE** (added after the 2026-07-03 review caught unwired exits): any feature that must ACT in a
   live loop — an exit branch, a flag producer, a gate consumer — is **NOT** G3-PASS on unit tests alone; it needs
   an END-TO-END integration test proving it fires in the running controller loop. "Green unit tests ≠ wired live."
9. **Every wave appends one JSON line to `loop-run-log.md`** and reconciles the companion docs to this STATE.

## 5 · HOW TO DISPATCH
- Ready scripts: `.agency/04-plan/workflows/{wave3-chartpath,wave4-detection}.js`. Copy the runner for Wave 2C.
- Persisted patterns: `C:\Users\manso\.claude\projects\C--dev-aats\73613d2a-b726-4baf-9355-d5da5dff14ae\workflows\scripts\`.
- Verify: `python -m pytest -p no:randomly -q`; ruff; secret grep; scaffold grep
  (`grep -rnE 'if False|MUTATION-TEST' aats/ --include=*.py` — **ignore docstring `if False, …` param docs**, which
  is why the acceptance gate's "must be 0" is wrong; the correct check excludes docstrings).

## 6 · POINTERS
- **Loop governance (AATS is now a registered L2 loop — SIM+BUILD ONLY):** `C:\dev\aats\LOOP.md` (constitution:
  denylist + human gates + dispatch discipline) · `loop-budget.md` (caps + kill-switch) · `loop-run-log.md`
  (append one JSON line per wave) · registered in `~/.claude/loops/CONTROL-PLANE.md`.
- **Acceptance ledger** (per-wave dual-G3 evidence, EN1→Wave 2C): `.agency/05-reports/review/ACCEPTANCE-LEDGER.md`
- Program review (honest verdict + findings): `.agency/05-reports/review/PROGRAM-REVIEW-2026-07-03.md`
- Methodology review (loop/swarm compliance): `.agency/05-reports/review/METHODOLOGY-REVIEW-2026-07-03.md`
- Missions/points: `MISSION-BOARD.md` (⚠️ being reconciled) · Architecture: `ELITE-ARCHITECTURE.md` ·
  Milestones/rewards: `MILESTONES-ACCEPTANCE-REWARDS.md` · Directive/thesis: `00-brief/ELITE-ENHANCEMENT-DIRECTIVE.md`

## 7 · KNOWN HAZARDS
- Build agents crash on their FINAL report AFTER writing the code (E14b, E16). Check `git status` + suite for ground
  truth; review-gate in-tree code.
- Reviewers doing in-place mutation testing once corrupted the tree → non-destructive-review guard.
- "Tests pass + unit-reviewed" ≠ "wired live." The 2026-07-03 review caught Wave-2 exits unwired despite green tests —
  **always add an END-TO-END integration test that proves the feature fires in the running loop, not just in a unit.**
- Infra: an org subscription-access block and a Fable session limit were both hit + resolved. `if False, …` strings in
  `sell_sim.py`/`rpc_client.py` are DOCSTRINGS, not scaffolding.
