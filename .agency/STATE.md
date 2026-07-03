# AATS — STATE.md · MASTER RESUME SPINE

> **READ THIS FILE FIRST.** Single source of truth for resuming the AATS elite-enhancement program across a
> model switch (Fable 5 ↔ Opus 4.8 ultracode). **You are the SAME worker continuing** — do not restart, do not
> re-plan from scratch. Pick up at **§2 NEXT ACTIONS**. This file is AUTHORITATIVE for live status;
> `MISSION-BOARD.md` / `MILESTONES-ACCEPTANCE-REWARDS.md` may lag by a wave — reconcile them from here.

**Updated:** 2026-07-03 (Fable session limit reset; program running). **Branch:** `aats-sniper-build` ·
**HEAD:** `abf2477` · pushed. **Full suite:** `2911 passed, 2 skipped, 0 failed`.

---

## 0 · WHO YOU ARE / HOW TO OPERATE
- You are the **Agency Runtime** (top-level session) per `C:\dev\aats\CLAUDE.md`. You **dispatch** specialized
  subagents and enforce gates; **you write NO production code/specs/tests yourself.**
- **Ultracode on:** run a **Workflow** per wave; adversarially dual-G3 everything; correctness > cost.
- **Every wave = one sequential dual-G3 Workflow:** per item → build (owner agent) → `code-reviewer` **and**
  `backtest-qa-engineer` in parallel → fix-loop (≤3). Items run **one-at-a-time** (no overlapping-file races).
- **After each wave:** *you* run the full suite yourself, then **commit + push**. Baked-in guards (keep them):
  **build/fix agents must NOT run git**; **reviewers must do NON-DESTRUCTIVE review** (never edit the tree).
- Only the top-level session dispatches (subagents can't spawn subagents).

## 1 · PURPOSE + STOP CONDITION
Take AATS from paper-proven to **elite** (competitor-parity), every enhancement **de-risk-only**, then reach
the **edge-proof gate**. Stop when all MISSION-BOARD items are G3-PASS **and** the edge proof (GATE-A/GATE-B on
the RECORDED corpus) yields **GO or NO-GO**. Real capital stays `DRY_RUN`-disabled until the CEO authorizes.

## 2 · NEXT ACTIONS — do these in order
1. **Wave 3 — chart-path model architecture (buildable-now items).** Run:
   `Workflow({scriptPath: "C:\\dev\\aats\\.agency\\04-plan\\workflows\\wave3-chartpath.js"})`
   Then verify full suite + commit. NOTE: M2-CP-03 (train the regime model) + M2-CP-06 (survivor temporal) are
   **DATA-BLOCKED** until ≥3,000 recorded launches — build the tensor/label/contract/feature/card now, train later.
2. **Wave 4 — detection completeness.** Run:
   `Workflow({scriptPath: "C:\\dev\\aats\\.agency\\04-plan\\workflows\\wave4-detection.js"})`
   (E-M1-01 live Geyser multi-venue validation needs a real paid gRPC endpoint — do the code + honest labeling;
   don't claim multi-venue live coverage without real data.)
3. **Re-run the execution/custody audit** (`solana-execution-engineer` + `crypto-security-engineer` lens) → this
   is the domain reader that died on the earlier account error; it grounds the wallet-linking / go-live runbook.
4. **⭐ MILESTONE E — the EDGE PROOF (the gate).** When the corpus is large enough, run GATE-A + GATE-B on the
   recorded data (`aats/models/gate_a.py`, `gate_b.py`) → **GO / NO-GO**. Escalate the honest verdict to the CEO.
   This is the fork toward real capital; nothing lifts `DRY_RUN`.

## 3 · DONE LEDGER (verified + committed, newest first)
| Commit | What |
|---|---|
| `abf2477` | **E18** min-holder floor + **E19** LP-unlock de-risk → **MILESTONE B COMPLETE** (2911 green) |
| `1c70513` | **E15** serial-deployer reputation (G3-PASS) + **E16** fresh-wallet heuristic |
| `4be643c` | continuation system (STATE + mission board + architecture + rewards + ready wave scripts) |
| `fbf4731` | Wave 2A — **E14a/E14b** insider/dev-sell auto-exit + **E17** delayed-honeypot re-probe |
| `cce5440` | Wave 1 — alpha engine (velocity-wire, CA-extraction, Telethon, caller/velocity backends, live smart-wallet) |
| `7faccf5` | **EN1** — caller-score de-risk guard survives `python -O` |
> **E16 status:** originally committed UNREVIEWED at `1c70513`; its dual-G3 review-gate later PASSED clean
> (workflow `wf_642d4457-982`, 0 findings) — E16 is now fully accepted.

## 4 · INVARIANTS (never violate — the safety law)
1. **No win-rate metric.** Success = net-of-cost PnL + model-vs-baseline on RECORDED data; honest tallies only.
2. **`DRY_RUN` stays disabled** until edge proven AND CEO authorizes. No wave lifts it.
3. **Asymmetric trust:** signals may ONLY de-risk (reject / shrink / down-weight ≤1 / exit). Never size up,
   widen/relax a stop, raise conviction, or add leverage.
4. **LLM / heavy models never on FAST/SNIPE** (SLOW-loop only; hot branch reads a pre-set flag).
5. **Point-in-time (T-300a):** on-chain event-time only; absent → None, never wall-clock; no lookahead.
6. **Money = int lamports/Decimal. No secrets in code/logs.** Custody = capped hot wallet + isolated Vault signer.
7. **Dual-G3 mandatory** (code-reviewer AND backtest-qa PASS). Three strikes → re-plan.

## 5 · HOW TO DISPATCH
- Ready scripts: `.agency/04-plan/workflows/wave3-chartpath.js`, `wave4-detection.js`
  (`Workflow({scriptPath})`). The wave-runner template is inside them — copy it for new waves (exec-audit, etc.).
- Persisted prior scripts (patterns to copy):
  `C:\Users\manso\.claude\projects\C--dev-aats\73613d2a-b726-4baf-9355-d5da5dff14ae\workflows\scripts\` —
  `aats-e16-review-gate-...` (review-gate pattern), `aats-wave2b-finish-e18-e19-...`, `aats-wave2a-exits-...`.
- Verify: `python -m pytest -p no:randomly -q` (~2.5 min); `ruff check`; secret grep
  (`git diff | grep -iE '5c4f747f|api-key=[a-z0-9]{8}|BEGIN .*PRIVATE|sk-ant'`); scaffold grep
  (`grep -rnE 'if False|MUTATION-TEST' aats/ --include=*.py` — ignore docstring `if False, ...` param docs).

## 6 · POINTERS
- Missions + acceptance gates + points: `.agency/04-plan/MISSION-BOARD.md`
- Detailed architecture: `.agency/04-plan/ELITE-ARCHITECTURE.md`
- Milestones + reward ledger: `.agency/04-plan/MILESTONES-ACCEPTANCE-REWARDS.md`
- Full 28-item roadmap: `.agency/04-plan/ELITE-ENHANCEMENT-ROADMAP.md`
- Honest-edge directive/thesis: `.agency/00-brief/ELITE-ENHANCEMENT-DIRECTIVE.md`
- Deploy + live-feed runbook: `~/.claude/projects/C--dev-aats/memory/deploy-and-live-feed-runbook.md`

## 7 · KNOWN HAZARDS (learned — avoid re-discovering)
- Build agents sometimes **crash on their final report AFTER writing the code** (E14b, E16 both did). Result
  object may be junk (`files:["a.py"]`) or empty while the real edits ARE in the tree. **Always check
  `git status` + run the suite for ground truth; a "no result" is NOT "no work" — review-gate the in-tree code.**
- A reviewer doing in-place mutation testing once **corrupted the shared tree** → the non-destructive-review guard.
- Infra interruptions seen this program: an **org subscription-access block** and a **Fable session usage
  limit** (both resolved). If dispatch fails with those errors, it's billing/infra, not code — switch model or wait.
- The `if False, ...` strings in `sell_sim.py`/`rpc_client.py` are **docstrings** documenting bool params, NOT
  mutation scaffolding — don't flag them.
