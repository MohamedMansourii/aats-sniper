# AATS — STATE.md · MASTER RESUME SPINE

> **READ THIS FILE FIRST.** Single source of truth for resuming the AATS elite-enhancement program across a
> model switch. **You are the SAME worker continuing** — do not restart. Pick up at **§2 NEXT ACTIONS**.
> This file is AUTHORITATIVE for live status; the companion docs under `.agency/04-plan/` are being reconciled
> to it (they lagged a wave — see the 2026-07-03 review) so trust THIS file on any conflict.

**Updated:** 2026-07-03 (post adversarial program review). **Branch:** `aats-sniper-build` · **HEAD:** verify
with `git log -1` (top of §3 ledger). **Full suite:** `2911 passed / 0 failed`.
**⚠️ Honest status:** the SAFETY SPINE is airtight (proven — cannot add risk / fake a win-rate), but the Wave-2
**catastrophic exits are built + unit-tested yet NOT wired into the live loop** — fix that FIRST (Wave 2C).
See `.agency/05-reports/review/PROGRAM-REVIEW-2026-07-03.md`.

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
1. **⚠️ Wave 2C — LIVE WIRING & INTEGRATION (top priority; fixes the review's HIGH findings).** Author a
   sequential dual-G3 wave (copy the runner from any `.agency/04-plan/workflows/*.js`) with:
   - **2C-1 (`agent-orchestration-engineer` + `risk-guardrails-engineer`):** wire ALL Wave-2 flag PRODUCERS into
     the live SLOW/controller loop so `insider_dump` (E14), `sellability_degraded` (E17), and `lp_unlock_approaching`
     (E19) flags are actually SET on real ticks (today only `narrative_failure` is). **Add the missing E19
     StateStore `get/set_lp_unlock_approaching_flag`** (Protocol + InMemoryStateStore, mirror `insider_dump`) and
     have `fast_loop` READ + PASS `lp_unlock_approaching_flag` into `on_tick`. Ship END-TO-END integration tests
     proving each catastrophic exit FIRES live. Also decide whether the Wave-2B ENTRY gates (deployer-rep, funding-age,
     lp-unlock-entry) should wire into `snipe_loop` now or stay paper-neutral (document the choice).
   - **2C-2 (`nlp-sentiment-engineer`):** fix the `classify_direction()` negation blind spot in
     `aats/sentiment/call_extract.py` ("Not bullish … would not touch it" → wrongly `long`); add negation guards +
     tests, mirroring the buy-family negation logic.
   Verify full suite, commit, reconcile docs, write acceptance artifacts.
2. **Resume Wave 3** (chart-path architecture): `git stash list` → pop `wave3-wip-paused-for-review` +
   `wave3-wip-agency-docs` (both preserved), then re-run
   `Workflow({scriptPath:"C:\\dev\\aats\\.agency\\04-plan\\workflows\\wave3-chartpath.js", resumeFromRunId:"wf_74debb68-057"})`
   (completed items replay from cache). NOTE: M2-CP-05 has NO dispatch entry in the ready script — add it or drop
   its 20 pts from the Milestone-C tally. M2-CP-03/06 are DATA-BLOCKED (≥3,000 launches).
3. **Wave 4 — detection completeness:** `Workflow({scriptPath:"C:\\dev\\aats\\.agency\\04-plan\\workflows\\wave4-detection.js"})`.
4. **Re-run the execution/custody audit** → grounds the wallet-linking / go-live runbook.
5. **⭐ MILESTONE E — the EDGE PROOF (the gate).** GATE-A + GATE-B on the recorded corpus → GO/NO-GO. Nothing lifts `DRY_RUN`.

## 3 · DONE LEDGER (verified + committed, newest first)
| Commit | What | Honest note |
|---|---|---|
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
7. Dual-G3 mandatory (code-reviewer AND backtest-qa PASS) **+ write the acceptance artifact**. Three strikes → re-plan.

## 5 · HOW TO DISPATCH
- Ready scripts: `.agency/04-plan/workflows/{wave3-chartpath,wave4-detection}.js`. Copy the runner for Wave 2C.
- Persisted patterns: `C:\Users\manso\.claude\projects\C--dev-aats\73613d2a-b726-4baf-9355-d5da5dff14ae\workflows\scripts\`.
- Verify: `python -m pytest -p no:randomly -q`; ruff; secret grep; scaffold grep
  (`grep -rnE 'if False|MUTATION-TEST' aats/ --include=*.py` — **ignore docstring `if False, …` param docs**, which
  is why the acceptance gate's "must be 0" is wrong; the correct check excludes docstrings).

## 6 · POINTERS
- Program review (honest verdict + findings): `.agency/05-reports/review/PROGRAM-REVIEW-2026-07-03.md`
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
