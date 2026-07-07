# AATS — STATE.md · MASTER RESUME SPINE

> ⏸ **PAUSED 2026-07-07 (weekly limit).** ⛔ **DECISIVE EDGE VERDICT = NO-GO.** The reaction/whale-front-run thesis
> ran through the full capital-licensing walk-forward (n=2,218; OOS n=1,184; effective=310; purged+embargoed; clustered
> bootstrap): **GATE-B PASS out-of-sample** (real selection skill — a first) **but GATE-A FAIL** (model loses money
> net-of-cost, −0.028/SOL). **All three theses (launch/momentum/reaction) decisively NO-GO — no solo-operator edge net
> of cost.** Per IF-NO-GO = **completion**: proven-safe paper platform, Priority-2 NOT built, **NO capital moves**.
> Edge-proof machinery committed `8fa5462` (walk-forward + effective-sample floor + certified reaction harness, dual-G3).
> **IN-FLIGHT:** signer workflow `wf_cddc4d98-dc3` (uncommitted signer lane) — resume + gate per RESUME-HERE §NEXT.

> **READ THIS FILE FIRST, THEN `.agency/RESUME-HERE.md`.** Single source of truth for resuming across a session
> restart / model switch. **You are the SAME worker continuing** — the **Agency Runtime**. Do not restart from zero.
> **Compacted 2026-07-06** after the CEO's checkpoint directive.

**Branch:** `aats-sniper-build` · **HEAD:** `071b1f5` (verify `git log -1`) · **Suite:** 3177 tests (green at last full run).

---

## 0 · HOW YOU OPERATE (non-negotiable)
- You are the **Agency Runtime** (`C:\dev\aats\CLAUDE.md`): you **dispatch** specialized subagents + Workflows and
  enforce gates. **You write NO production code/specs/tests yourself.**
- **Ultracode ON** → a **Workflow per build**; adversarially **dual-G3** everything (correctness > cost).
- **⛔ CODEX IS DROPPED.** The CEO revoked the Codex hand-off (2026-07-06). **Everything is built by YOU** via your
  multi-agent Workflows/agent-swarm. The 3 docs in `.agency/04-plan/codex-work-packages/` are now **Claude Workflow
  specs** (one — OUTCOME-LABELING-HARNESS — is already DONE; see §4).
- **Wave-runner pattern (each build):** maker agent builds → **`code-reviewer` AND `backtest-qa-engineer` review in
  parallel** (leak audit mandatory for anything touching the edge proof) → fix-loop ≤2 → both PASS = G3. Guards:
  build/fix agents **must NOT run git**; reviewers do **NON-DESTRUCTIVE** review (never edit the tree).
- **After each build:** *you* run the full suite, commit + push, write the acceptance artifact under
  `.agency/05-reports/{qa,review}/`, and **reconcile this STATE**.

## 1 · THE GOAL (6 phases) + HARD RULES
**Goal:** an elite Solana meme-coin bot with a **proven** edge, then staged live trading — but ONLY if the edge proves.
**HARD RULES (never violate):** (1) NO win-rate metric ever — success = positive expectancy **net of ~6% round-trip
cost** with survivable risk; (2) NO real funds until Phase-5 edge proof returns **GO** AND security audit passes AND
CEO authorizes; (3) point-in-time correct data only — no lookahead/leakage; (4) every signal may only **de-risk**;
(5) money = int lamports/Decimal; (6) no secrets in code/logs.
**Phases:** 1 Detection&Data ✅ · 2 Intelligence 🟠 · 3 Risk&Safety ✅ · 4 Execution&Custody (built-in-sim, go-live
gated) · **5 ⭐ EDGE PROOF 🔒 (the gate — currently NO-GO, one promising signal)** · 6 Live (gated on GO + CEO auth).

## 2 · WHERE WE ARE — checkpoints DONE vs OPEN
**DONE & COMMITTED:**
- Phases 1–4 built + dual-G3'd across many waves (ingestion, features, nlp/sentiment, ml, reasoning, controller
  triple-loop, execution/custody in sim, risk/guardrails live-wired, mev). Safety spine audited airtight.
- **Exec/custody security audit = PASS-WITH-CONDITIONS** (`.agency/05-reports/security/EXEC-CUSTODY-AUDIT-2026-07-06.md`);
  go-live blocker = build the real isolated signer (currently scaffold).
- **Edge-proof machine BUILT + dual-G3 PASS** (leak-audited): `aats/backtest/outcome_harness.py`,
  `momentum_harness.py`, `run_edge_proof.py` + `gate_a.py`/`gate_b.py`/`baseline.py`. Momentum params frozen.
- **Live labeled-corpus collector** `C:/aats_shadow/_collector.py` (price-path + buy/sell pressure) accruing real data.
- **Edge proof RUN on real data** (see §4).

**OPEN (the real remaining work — all Claude-owned):**
- **Phase 5 verdict is NO-GO.** Momentum strategy shows the FIRST GATE-B PASS but too few selected trades for GATE-A.
  → **needs a larger corpus + re-run** (§3.1). This is the critical path.
- Detection-completeness (`WAVE-4-detection-completeness.md`) + CP-07 creator-outflow fix (`CP-07-...md`) — now Claude
  Workflows, NOT yet done. Lower priority than the edge verdict.
- Real isolated signer build (Phase-4 go-live blocker) — ONLY after a GO.

## 3 · NEXT ACTIONS (in strict order)
1. **⭐ RE-RUN THE MOMENTUM EDGE PROOF when corpus ≥ ~3000** (check `wc -l < C:/aats_shadow/labeled_corpus.jsonl`):
   ```
   cd /c/dev/aats; export RPC_PRIMARY=$(grep '^RPC_PRIMARY=' .env | cut -d= -f2- | tr -d '\r'); export DRY_RUN_ENABLED=true
   python -m aats.backtest.run_edge_proof --corpus C:/aats_shadow/labeled_corpus.jsonl --strategy momentum --entry-horizon 60 --out C:/aats_shadow/momentum_result.json
   ```
   Record the result as a dated artifact in `.agency/05-reports/qa/`, commit, update this STATE. **NEVER fabricate a
   GO** — if GATE-A still fails, it stays NO-GO; accrue more / iterate features.
2. **If GATE-B holds but GATE-A still marginal:** build (Workflow) **bonding-curve entry-price fidelity** (record
   on-curve price at each horizon in the collector — kills DexScreener sparsity + the ~64s drift, raises the
   selectable set) and/or improve outcome fidelity (realizable-exit sell-sim). Re-run.
3. **If a real GO emerges:** re-run the security audit, build the real isolated signer, then Phase-6 devnet→tiny-real,
   CEO-authorized only.
4. **In parallel / lower priority (Claude Workflows):** Wave-4 detection completeness, CP-07 creator-outflow fix.

## 4 · THE EDGE-PROOF JOURNEY (the heart of the project)
- **Launch strategy (predict launch winners):** RAN real → **NO-GO** (model +0.311 < baseline +0.619; GATE-A lower-95%
  negative). Honest: launch-winner prediction has no edge (expected). `EDGE-PROOF-2026-07-06-REAL.md`.
- **Momentum/reaction entry @60s** (decide on ≤60s buy-pressure/price move, leak-safe): RAN real (497) → **NO-GO but
  FIRST GATE-B PASS** — model **beats** the naive baseline (delta +0.041, lower-95% +0.026>0); naive momentum LOSES
  money (−0.345/SOL). GATE-A FAIL: model selected only **2/497** (too selective → not stat-positive on tiny sample).
  `EDGE-PROOF-momentum-2026-07-06.md`. **This is the promising thread — more data is the input needed.**
- Caveats (documented): ~64s collector timing drift; DexScreener sparsity (93/497 unpriced@60s); horizon-compressed exit.

## 5 · ACTIVE PROCESSES (may need restart after reopen)
- **Collector** `C:/aats_shadow/_collector.py` — detached OS process (was PID 22012), writing `labeled_corpus.jsonl`
  (persists on disk; ~2100+ records at compaction, accruing ~1000/hr). A **detached process may survive a Claude
  restart but NOT a reboot.** On resume: `tasklist | grep python` — if not running, restart detached:
  `Start-Process python C:/aats_shadow/_collector.py -WindowStyle Hidden` (PowerShell) or `python C:/aats_shadow/_collector.py &`.
  The corpus file is NEVER lost by restarting the collector (append-only).

## 6 · INVARIANTS — see §1 HARD RULES. Audited airtight. Real capital stays `DRY_RUN`-disabled until GO + CEO auth.

## 7 · KEY FILE MAP
- Edge proof: `aats/backtest/{outcome_harness,momentum_harness,run_edge_proof}.py`, `aats/models/{gate_a,gate_b,baseline}.py`.
- Collector (operational, outside repo): `C:/aats_shadow/_collector.py` → `labeled_corpus.jsonl`.
- Results: `.agency/05-reports/qa/EDGE-PROOF-*.md` · Security: `.agency/05-reports/security/` · Run log: `loop-run-log.md`.
- This spine: `.agency/STATE.md` + `.agency/RESUME-HERE.md` (the 60-second resume).
