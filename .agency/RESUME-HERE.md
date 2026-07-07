# ▶ RESUME HERE — AATS (60-second orientation)

## 🚦 CONTINUOUS-DRIVE STATE (2026-07-07 — STANDING ORDER: drive to completion, DO NOT STOP)
CEO issued **CONTINUOUS AUTONOMOUS DRIVE MODE**. Do NOT wait for "continue". Chain work until: scope done / a
real CAPITAL escalation / a hard blocker / the decisive reaction GO-NO-GO. End every turn on an ACTION, never a question.
Iron rules hold (no real capital without proven GO + audit + CEO auth; no fabricated GO; dual-G3 every deliverable).

**IN-FLIGHT WORKFLOWS** — gate each with dual-G3 on completion (ground-truth: `git status` + run the FULL suite
yourself; a build agent can crash on its final report while its edits ARE in the tree), commit on PASS via
`git pull --rebase --autostash` then push:
- `wjdwzovju` (runId wf_1207b3ec-b18) — **EDGE-PROOF COMPLETION**: real-corpus purged/embargoed walk-forward +
  effective-sample GATE-B floor + certify `reaction_harness.py` (tests). Lane: `aats/backtest` + `aats/models`.
- `wczblnbbu` (runId wf_cddc4d98-dc3) — **REAL ADR-0009 SIGNER**: enforcer (per-tx/rolling caps + program allowlist +
  tip pin + refusals) + remove `MockSignerClient/MockRpcClient` defaults + refusal tests. Lane: `rust/aats-signer` + `aats/execution`.
  NOTE: both share the main working tree — when gating, re-run the suite AFTER both land to avoid cross-edit false failures.

**CHAIN PLAN (next in-bounds units, §0.2 edge-first):**
1. Gate + commit the two in-flight workflows.
2. **CP-07 creator-outflow fix + Wave-4 detection completeness** (lane `aats/ingestion`) — next workflow.
3. **DECISIVE REACTION VERDICT** — once edge-proof-completion lands AND the reaction corpus has volume: run the
   walk-forward reaction GATE-A/GATE-B. **This resolves the project → SURFACE to CEO (pause reason #3).**
4. **M4 live E2E** — once A wires the `None` safety producers (see `M4-PREFLIGHT-safety-posture-2026-07-07.md`).
5. **Priority-2** (dashboard RED-2 + infra hardening) — ONLY if reaction returns GO.

**Edge state:** launch+momentum NO-GO robust (n=5,992 realizable; model loses money; GATE-B "PASS" is decline-everything on ~8 decisions); reaction early NO-GO (n=436). **Capital DISABLED. No GO fabricated.**
**Verification:** M1–M3, M5, M6 signed; M4 documented-blocked. Artifacts in `.agency/verification/`.

---


**You are the Agency Runtime** continuing the AATS Solana meme-coin bot. You dispatch Workflows/agents (dual-G3),
you write no production code yourself. **Read `.agency/STATE.md` next for full detail.** Branch `aats-sniper-build`.

## The one-line status
The whole bot is **built, safe, and security-audited**; the **edge proof runs on real data** and currently says
**NO-GO** — BUT the **momentum/reaction-entry strategy showed the first GATE-B PASS** (the model beats a losing
baseline). It's not proven only because the model is very selective and needs a **bigger corpus** to make GATE-A
statistically testable. A live collector is accruing that corpus autonomously.

## ⛔ Codex is DROPPED — you build everything yourself (CEO order, 2026-07-06).

## Your FIRST action on resume
1. Check the collector + corpus:
   ```
   tasklist | grep 22012        # collector alive? if not: Start-Process python C:/aats_shadow/_collector.py -WindowStyle Hidden
   wc -l < C:/aats_shadow/labeled_corpus.jsonl    # corpus size
   ```
2. **If corpus ≥ ~3000 → RE-RUN the momentum edge proof (the decisive test):**
   ```
   cd /c/dev/aats; export RPC_PRIMARY=$(grep '^RPC_PRIMARY=' .env | cut -d= -f2- | tr -d '\r'); export DRY_RUN_ENABLED=true
   python -m aats.backtest.run_edge_proof --corpus C:/aats_shadow/labeled_corpus.jsonl --strategy momentum --entry-horizon 60 --out C:/aats_shadow/momentum_result.json
   ```
   Record the verdict (`.agency/05-reports/qa/`), commit, update STATE. **Never fabricate a GO** — NO-GO stays NO-GO.
3. **If corpus < 3000 →** let it accrue; meanwhile advance the roadmap in `STATE.md §3` (bonding-curve price fidelity
   Workflow, then Wave-4 detection / CP-07 — all Claude-owned).

## The rule that matters most
No real money moves, ever, until Phase-5 returns a real **GO** (model beats baseline net-of-cost, statistically) AND
the security audit passes AND the CEO authorizes. Until then: honest verdicts only, `DRY_RUN` locked.
