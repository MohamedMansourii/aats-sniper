# AATS Methodology Compliance Review — 2026-07-03 (loop-engineering + agent-swarm)

**Verdict: SOLID-WITH-GAPS.** Honest answers:
- **Complete implementation of BOTH skills?** ❌ No. **agent-swarm = MOSTLY** (real, battle-tested).
  **loop-engineering = PARTIAL** (great STATE spine + maker/checker, but the loop *governance* was missing).
- **Would Opus resume seamlessly AND governed?** Split: **resume-the-work = near-seamless** (STATE.md is excellent);
  **resume-governed = No** — there was no budget/run-log/control-plane/L-level, so a resumer inherited the work
  but not the guardrails. **Now fixed** (see Remediation).

## agent-swarm-orchestration — MOSTLY (genuinely implemented, not name-dropped)
Strengths: every wave = owner-build → parallel independent `code-reviewer` + `backtest-qa-engineer` → 3-strike
fix, both-must-PASS, maker-never-grades-self; specialist-over-generic; serialized shared seams; asymmetric-trust
safety proven adversarially. The strongest proof it works: **the checker caught the unwired Wave-2 exits.**
Gaps: (HIGH) dual-G3 accepted unit-green as "done" for live-wiring safety controls → **now a STANDING E2E gate**
(STATE §4.8); (MED) companion-doc drift breaks "work cold from files"; (MED) no on-disk dual-G3 acceptance
artifacts (verdicts were prose); (LOW) three-strikes was a cap not an auto-escalation → **now HALT+escalate**
(STATE §4.7); (LOW) shared-tree fan-out, no worktree isolation (defensible: items were sequenced).

## loop-engineering — PARTIAL (STATE spine ✅; governance was ❌)
DONE: Primitive 6 STATE spine (model-grade), Primitive 5 + Law 1 maker/checker (dual-G3 supersedes loop-verifier),
Law 3 safety substance (DRY_RUN/secrets/contracts). MISSING (now remediated): Primitive 1 cadence/self-prompting
(it was a hand-launched swarm sequence — kept human-launched by design given real capital, documented in LOOP.md);
Law 2 budget (empty run-log, no cap, no kill-switch); L1→L2→L3 placement + control-plane registration (was
mis-catalogued under "Coding Agency L1 report-only" while actually dispatching + committing = L2).

## Remediation applied this session
- **`C:\dev\aats\LOOP.md`** — the loop constitution: purpose/stop, **Level = L2 (SIM+BUILD ONLY, capital
  hard-denylisted + human-gated)**, denylist, human gates, dispatch discipline (incl. the standing E2E gate),
  graduation criteria.
- **`C:\dev\aats\loop-budget.md`** — per-wave (~4M) + per-day (~20M) token caps, kill-switches
  (`loop-pause-all`, `aats-loop-pause`), max-1-concurrent-wave, 3-strike halt.
- **`C:\dev\aats\loop-run-log.md`** — backfilled one JSON line per wave (10 runs) + the append rule.
- **Registered AATS** as its own **L2** operation in `~/.claude/loops/CONTROL-PLANE.md` (removed the mis-catalogued
  bundling under Coding Agency).
- **STATE.md §4** now encodes the E2E standing gate, HALT-on-3-strikes, and the run-log-per-wave rule; §6 points to
  the governance files.
- **Still to do** (tracked in STATE): reconcile the companion docs mechanically to STATE; backfill per-item dual-G3
  acceptance artifacts for E15/E16/E18/E19/Wave-1; optional L1 daily-triage cadence.

## Recommendation (verbatim)
"Strong agent-swarm build + excellent STATE spine — NOT a name-drop — but it was NOT yet a governed loop. The
swarm methodology is real and battle-tested; what was missing is the loop-engineering governance the Opus handoff
depends on: budget law, L-level, control-plane registration." → Now added; the loop is governed.
