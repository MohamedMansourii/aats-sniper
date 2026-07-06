# AATS — loop-run-log.md

> Append-only run history for the AATS elite-enhancement loop. **Exactly one JSON line per wave/run.** Backfilled
> 2026-07-03 from the persisted Workflow transcripts (tokens_estimate = subagent tokens reported by each run).
> The `loop-budget` guard sums today's `tokens_estimate` against the cap in `loop-budget.md`.

## Entries
{"date":"2026-07-01","pattern":"enhancement-wave","run":"EN1-caller-guard","outcome":"G3-PASS","tokens_estimate":284000,"commit":"7faccf5","note":"caller-score de-risk guard survives python -O"}
{"date":"2026-07-01","pattern":"enhancement-wave","run":"wave1-alpha-engine","outcome":"G3-PASS (5 items)","tokens_estimate":3170000,"commit":"cce5440","note":"velocity-wire, CA-extract, Telethon, backends, live smart-wallet"}
{"date":"2026-07-01","pattern":"enhancement-wave","run":"wave2a-exits","outcome":"G3-PASS (E14a,E17) + E14b review-gate","tokens_estimate":1520000,"commit":"fbf4731","note":"insider-dump + sellability exits; E14b crashed-then-review-gated"}
{"date":"2026-07-01","pattern":"enhancement-wave","run":"wave2b-E15-E16","outcome":"E15 G3-PASS / E16 unreviewed-then-passed","tokens_estimate":1014000,"commit":"1c70513","note":"serial-deployer reputation + fresh-wallet (E16 review-gate wf_642d4457)"}
{"date":"2026-07-01","pattern":"docs","run":"continuation-system","outcome":"COMPLETE","tokens_estimate":40000,"commit":"4be643c","note":"STATE spine + mission board + architecture + rewards + ready wave scripts"}
{"date":"2026-07-02","pattern":"enhancement-wave","run":"wave2b-finish-E18-E19","outcome":"G3-PASS (2 items)","tokens_estimate":721000,"commit":"abf2477","note":"min-holder floor + LP-unlock; Milestone B code-complete"}
{"date":"2026-07-02","pattern":"enhancement-wave","run":"wave3-chartpath","outcome":"PAUSED (stashed for review)","tokens_estimate":600000,"commit":null,"note":"~2 items built (tensor,label,ADR) then paused; stash wave3-wip-*"}
{"date":"2026-07-03","pattern":"review","run":"program-review-5auditor","outcome":"NEEDS-WORK (caught unwired exits)","tokens_estimate":802000,"commit":"a6a85ad","note":"safety airtight; Wave-2 exits built-not-wired; docs drifted"}
{"date":"2026-07-03","pattern":"enhancement-wave","run":"wave2c-live-wiring","outcome":"G3-PASS (2 items)","tokens_estimate":1623000,"commit":"7722294","note":"exit producers wired live via SlowLoopEnrichmentWiring + E19 StateStore + E2E test w/ control + classify_direction negation; suite 2965; artifact WAVE-2C-acceptance.md"}
{"date":"2026-07-03","pattern":"loop-governance","run":"loop-governance-install","outcome":"COMPLETE","tokens_estimate":30000,"commit":"9441359","note":"LOOP.md + loop-budget.md + loop-run-log.md + control-plane registration; loop-engineering PARTIAL->governed"}
{"date":"2026-07-03","pattern":"review","run":"methodology-compliance-review","outcome":"SOLID-WITH-GAPS (swarm MOSTLY, loop PARTIAL)","tokens_estimate":248000,"commit":null,"note":"loop governance missing: cadence, budget/run-log, L-level, control-plane reg -> this file + LOOP.md + budget fix it"}

{"date":"2026-07-06","pattern":"enhancement-wave","run":"wave3-chartpath","outcome":"4/5 G3-PASS (CP-07 FAIL->Codex)","tokens_estimate":2108000,"commit":"1eead58","note":"regime tensor+label(ADR-0014)+RegimeSignal contract+model-card/harness; CP-07 creator-outflow 2 edge-bugs -> Codex work-package #1; suite 3142; regime training data-gated"}
{"date":"2026-07-06","pattern":"loop-op","run":"corpus-recorder-start","outcome":"KILLED (bg non-persistent)","tokens_estimate":0,"commit":null,"note":"recorder connected + correctly dropped USDC noise, but Claude bg tasks die across turns -> corpus accrual needs a PERSISTENT process (docker stack always-on OR operator terminal) OR paid Bitquery archival. Phase-5 data is now the top external blocker."}
{"date":"2026-07-06","pattern":"codex-handoff","run":"codex-work-packages-1-and-2","outcome":"READY","tokens_estimate":25000,"commit":"pending","note":"CP-07 fix + Wave 4 (E-M1-02/05/06/07) packaged for Codex (maker=Codex, checker=Claude). E-M1-01/03 stay on Claude."}

{"date":"2026-07-06","pattern":"security-audit","run":"exec-custody-audit","outcome":"PASS-WITH-CONDITIONS","tokens_estimate":180000,"commit":"pending","note":"DRY-RUN paper state SECURE today (no exploitable crit/high; no wallet key in system; every real-money path fail-closed + test-proven send_calls==0; money int/Decimal; secret sweep clean; 176 exec tests pass). GO-LIVE BLOCKED until: F1 real aats-signer built (currently scaffold — the un-bypassable spend-cap/allowlist enforcer), F2 DEVNET-bypasses-DRY_RUN caveat, F3 dep hash-lock + pip-audit. Report EXEC-CUSTODY-AUDIT-2026-07-06.md. Agent hit Fable session limit (resets 12:40) but wrote the report first."}

{"date":"2026-07-06","pattern":"loop-op","run":"corpus-recorder-persistent","outcome":"RUNNING (detached PID 21476)","tokens_estimate":0,"commit":null,"note":"Start-Process detached OS process (survives Claude turns + session limits) accruing up to 20000 pump.fun launches -> C:/aats_shadow. Phase-5 SNAPSHOT accrual (option A, self-managed). REMAINING Phase-5 pieces: (1) labeling harness to resolve outcomes into TradeOutcome records, (2) run GATE-A/GATE-B. Recorder dies only on PC sleep/reboot -> restart with: python C:\\aats_shadow\\_launch.py (detached)."}

{"date":"2026-07-06","pattern":"edge-proof","run":"gate-a-gate-b-run","outcome":"NO-GO / UNPROVEN-NO-REAL-DATA","tokens_estimate":5000,"commit":"pending","note":"Phase 5 EXECUTED: GATE-A on 0 real outcomes -> fail-closed (ValueError, refuses to fabricate PnL); GATE-B min-sample not met. Scoreboard UNDEFINED (honest). HARD RULE: stay paper, no real funds. Path to GO = accrue corpus (recorder PID 21476 running) + build labeling harness + re-run. Artifact: .agency/05-reports/qa/EDGE-PROOF-2026-07-06.md"}

## Alerts This Period
- 2026-07-06: Fable session limit hit mid-audit (resets ~12:40 Africa/Tunis) — agent dispatch paused until reset; main-loop (commits/docs/verify) + detached local processes still available.
- 2026-07-06: persistent corpus recorder started (PID 21476) — Phase-5 snapshots now accruing autonomously; keep PC on.
