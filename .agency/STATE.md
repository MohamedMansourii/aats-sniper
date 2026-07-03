# AATS — STATE.md · MASTER RESUME SPINE

> **READ THIS FILE FIRST.** It is the single source of truth for resuming the AATS elite-enhancement
> program across a model switch (Fable 5 → Opus 4.8 ultracode). **You are the SAME worker continuing** —
> do not restart, do not re-plan from scratch, do not re-litigate settled decisions. Pick up at
> **§2 NEXT ACTIONS** and continue as if you had been driving all along.

**Updated:** 2026-07-03 by Fable 5 (paused at the Fable session usage limit).
**Branch:** `aats-sniper-build` · **HEAD:** `1c70513` · **Remote:** pushed to `origin/aats-sniper-build`.
**Full suite at pause:** `2856 passed, 2 skipped, 0 failed`.

---

## 0 · WHO YOU ARE / HOW TO OPERATE
- You are the **Agency Runtime** (the top-level session) per `C:\dev\aats\CLAUDE.md`. You **dispatch** the
  specialized subagents and enforce the gates; **you write NO production code, specs, or tests yourself.**
- **Ultracode is on:** author and run a **Workflow** for every substantive wave; adversarially dual-G3
  everything; token cost is not a constraint — correctness and completeness are.
- **Every wave = one sequential dual-G3 Workflow:** for each item → build (owning agent) → `code-reviewer`
  **and** `backtest-qa-engineer` in parallel → fix-loop (≤3 strikes). **Items run one-at-a-time** (sequential)
  so parallel agents never edit overlapping files.
- **After each wave:** *you* (main session) independently VERIFY — run the full suite yourself — then
  **commit + push**. Two guards are baked into the ready scripts and MUST stay: **(a) build/fix agents must
  NOT run git** (version control is the Runtime's job); **(b) reviewers must do NON-DESTRUCTIVE review**
  (never edit the tree — mutation-test on a copy). These exist because a reviewer editing `exit_engine.py`
  in-place once corrupted the tree (recovered).
- **Only the top-level session dispatches.** Subagents cannot spawn subagents.

## 1 · PURPOSE + STOP CONDITION
**Purpose:** take AATS from paper-proven to *elite* (competitor-parity with Trojan/BonkBot/Banana Gun/GMGN/
ISAC), every enhancement **de-risk-only**, then reach the **edge-proof gate**.
**Stop condition:** all MISSION-BOARD items are G3-PASS **and** the R3 edge-proof (Block A: GATE-A/GATE-B on
the *recorded* corpus) has produced a **GO or NO-GO** verdict. Real capital stays `DRY_RUN`-disabled until the
CEO explicitly authorizes live.

## 2 · NEXT ACTIONS — do these in order
1. **⚠️ REVIEW-GATE E16 first.** `dev_funding_age.py` + `dev_funding_age_gate.py` (+ tests) are committed
   (`1c70513`) and the suite is green, **but E16 was never dual-G3 reviewed** (its build agent crashed on its
   final report when the session limit hit). Run a **review-gate workflow** on the in-tree E16 code (copy the
   pattern from the persisted E14b review-gate script, §5). If reviewers PASS → commit a note. If they FAIL →
   dispatch `risk-guardrails-engineer`/`data-ingestion-engineer` to fix per findings; if unsalvageable,
   `git revert` E16's files and re-queue it as a fresh build.
2. **Finish Wave 2B (E18 + E19).** Resume the saved Wave 2B workflow — E15/E16 replay from cache instantly,
   E18 + E19 build fresh:
   `Workflow({scriptPath: "C:\\Users\\manso\\.claude\\projects\\C--dev-aats\\73613d2a-b726-4baf-9355-d5da5dff14ae\\workflows\\scripts\\aats-wave2b-provenance-wf_f5060cc5-362.js", resumeFromRunId: "wf_f5060cc5-362"})`
   Then verify full suite + commit.
3. **Wave 3 — chart-path model architecture.** Run `.agency/04-plan/workflows/wave3-chartpath.js`. Build the
   *architecture + tensor + label-spec + contract + baseline/monitor* now; the **model cannot train for real
   until ≥3,000 recorded launches** (data-blocked — see MILESTONES §M-C). Do not fake-train on synthetic data.
4. **Wave 4 — detection completeness.** Run `.agency/04-plan/workflows/wave4-detection.js`.
5. **Re-run the execution/custody audit** (the domain reader that died on the account error) →
   `solana-execution-engineer` + `crypto-security-engineer` lens → grounds the wallet-linking / go-live runbook.
6. **MILESTONE E — edge proof (the GATE).** Once the corpus is large enough, run GATE-A/GATE-B on the recorded
   data → **GO / NO-GO**. This is the fork toward real capital. Bring the honest verdict to the CEO.

## 3 · DONE LEDGER (verified + committed, most recent first)
| Commit | What |
|---|---|
| `1c70513` | Wave 2B partial — **E15** serial-deployer reputation (G3-PASS) + **E16** fresh-wallet (⚠️ UNREVIEWED) |
| `fbf4731` | Wave 2A — **E14a/E14b** insider/dev-sell auto-exit + **E17** delayed-honeypot re-probe (G3-PASS) |
| `cce5440` | Wave 1 — alpha engine: velocity-wire, CA-extraction, Telethon adapter, caller/velocity backends, live smart-wallet (G3-PASS) |
| `7faccf5` | **EN1** — caller-score de-risk guard survives `python -O` (G3-PASS) |
| `5fc74ec` | docker: package `config/` into the bot image |
| `c235509` | controller: live PumpPortal feed in SHADOW mode (real launches on the dashboard) |
| `14ba3df`/`c346ea0`/`bbf4118`/`409ca36` | ingestion: quote-mint guard · PumpPortal feed · launch-filter · free-tier WS |

## 4 · INVARIANTS (never violate — the safety law)
1. **No win-rate / success-rate metric anywhere.** Success = net-of-cost PnL + model-vs-baseline on RECORDED
   data. Honest tallies only, never a rate.
2. **Real capital stays `DRY_RUN`-disabled** until the edge is proven AND the CEO authorizes. No wave lifts it.
3. **Asymmetric trust:** every signal may ONLY de-risk (reject / shrink / down-weight ≤1 / exit). NEVER size
   up, widen/relax a stop, raise conviction, or add leverage.
4. **LLM / heavy models never on the FAST/SNIPE hot path** (SLOW-loop only; hot branch reads a pre-set flag).
5. **Point-in-time (T-300a):** event-time from on-chain data only; absent block_time stays None, never
   wall-clock-substituted; no lookahead.
6. **Money = integer lamports / Decimal, never float. No secrets in code/logs/images** (`.env`/Vault
   placeholders only). Custody = capped hot wallet + isolated Vault signer.
7. **Dual-G3 is mandatory:** `code-reviewer` AND `backtest-qa-engineer` must both PASS. Three strikes → re-plan.

## 5 · HOW TO DISPATCH (the proven pattern + saved scripts)
- **Ready-to-run wave scripts:** `.agency/04-plan/workflows/wave3-chartpath.js`, `.../wave4-detection.js`
  (author with `Workflow({scriptPath})`). The wave-runner template lives inside them — copy it for new waves.
- **Persisted prior scripts (resumable)** under
  `C:\Users\manso\.claude\projects\C--dev-aats\73613d2a-b726-4baf-9355-d5da5dff14ae\workflows\scripts\`:
  `aats-wave2b-provenance-wf_f5060cc5-362.js` (resume for E18/E19), `aats-e14b-review-gate-wf_86d3bf1b-a20.js`
  (the review-gate pattern for E16), `aats-wave2a-exits-...`, `aats-wave1-alpha-engine-...`.
- Verify commands: `python -m pytest -p no:randomly -q` (full suite, ~3.5 min), `ruff check`, secret grep
  (`git diff | grep -iE '5c4f747f|api-key=[a-z0-9]{8}|BEGIN .*PRIVATE|sk-ant'`), scaffold grep
  (`grep -rnE 'if False|MUTATION-TEST' aats/`).

## 6 · POINTERS
- **MISSION-BOARD** (divided missions + acceptance gates + points): `.agency/04-plan/MISSION-BOARD.md`
- **Detailed architecture:** `.agency/04-plan/ELITE-ARCHITECTURE.md`
- **Milestones + rewarding acceptance system:** `.agency/04-plan/MILESTONES-ACCEPTANCE-REWARDS.md`
- **Full roadmap (28 items):** `.agency/04-plan/ELITE-ENHANCEMENT-ROADMAP.md`
- **The honest-edge directive + thesis:** `.agency/00-brief/ELITE-ENHANCEMENT-DIRECTIVE.md`
- **Deploy + live-feed runbook:** `~/.claude/projects/C--dev-aats/memory/deploy-and-live-feed-runbook.md`
- **Operator procurement report (PDF):** `.agency/06-delivery/AATS-Infrastructure-Procurement-Report.pdf`

## 7 · KNOWN HAZARDS (learned this run — avoid re-discovering)
- Build agents sometimes **crash on their final structured-output report AFTER writing the code** (E14b, E16).
  Result object may be junk (`files:["a.py"]`) or empty while the real edits ARE in the tree. **Always check
  `git status` + run the suite for ground truth; never trust a "no result" as "no work".**
- A reviewer doing in-place mutation testing can **corrupt the shared tree** → the non-destructive-review guard.
- Fable 5 hit a **session usage limit** (resets ~08:10 Africa/Tunis) and earlier an **org subscription-access
  block**. If agent dispatch fails with those errors, it is infra/billing, not code — switch model or wait.
