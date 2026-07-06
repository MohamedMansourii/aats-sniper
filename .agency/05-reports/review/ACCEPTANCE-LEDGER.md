# AATS — Dual-G3 Acceptance Ledger (backfill)

> Addresses the 2026-07-03 methodology-review finding: acceptance was asserted in STATE prose, not recorded on
> disk. This backfills the per-wave dual-G3 evidence (reviewers + suite counts + commit + workflow id) for the
> completed waves. Reviewers = `code-reviewer` + `backtest-qa-engineer` unless noted. Suite counts were re-run by
> the Runtime, not trusted from agent reports.

| Item(s) | Commit | Workflow | Verdict | Suite (Runtime-verified) | Evidence / note |
|---|---|---|---|---|---|
| **EN1** — caller-score de-risk guard survives `python -O` | `7faccf5` | `wb83qx0oo` | G3-PASS | 217 sentiment / full green | mutation-proven: revert → 5/8 RED; the `-O` leak (0.70→1.00) reproduced, fix raises ValueError |
| **Wave 1** — EN2a velocity-wire · EN4 CA-extract · EN3 Telethon · EN5 backends · E-M1-04 live smart-wallet | `cce5440` | `wyt5zima0` | G3-PASS (5/5) | **2652** passed / 0 failed | offline-safe (no live net in tests); point-in-time; deps pyarrow+telethon lazy-imported |
| **Wave 2A** — E14a insider-dump detect · E14b auto-exit · E17 sellability re-probe | `fbf4731` | `wct1itvf6` + review-gate `wtjdbvh87` | G3-PASS | **2757** passed / 0 failed | E14b build crashed post-write → dedicated review-gate (removed a stray mutation scaffold, fixed on_tick/fast_loop signature); frozen-tree re-verified |
| **Wave 2B (a)** — E15 serial-deployer reputation · E16 fresh-wallet | `1c70513` | `ws1mvv3ba` (E15) + review-gate `we1q7zmn7` (E16) | G3-PASS | full green (2856 at E16) | E16 build crashed at session limit → review-gate PASSED clean (0 findings); strict point-in-time leak-free |
| **Wave 2B (b)** — E18 min-holder floor · E19 LP-unlock de-risk | `abf2477` | `wyzgxk1sg` | G3-PASS (2/2) | **2911** passed / 0 failed | E18 rippled to gate tests (new pre-trade check); de-risk/tighten-only |
| **Wave 2C** — wire Wave-2 exits LIVE + E19 StateStore + classify_direction | `7722294` | `wf_061f0fec-813` | G3-PASS (2/2) | **2965** passed / 0 failed | see `WAVE-2C-acceptance.md`; **standing E2E gate** satisfied (control test proves producer caused the exit) |

## Reviews on record
- `PROGRAM-REVIEW-2026-07-03.md` — 5-auditor adversarial code review (found the unwired exits; fixed by Wave 2C).
- `METHODOLOGY-REVIEW-2026-07-03.md` — loop-engineering + agent-swarm compliance (loop was PARTIAL → now governed).
- `WAVE-2C-acceptance.md` — the first wave shipped with its acceptance artifact under the new governance.

## Standing gates now enforced (STATE §4.7–4.9)
1. Dual-G3 + **written acceptance artifact** (this ledger going forward).
2. **E2E integration test** for anything that must act in a live loop ("green unit tests ≠ wired live").
3. **HALT + escalate on 3-strike G3-FAIL** (no silent continue).
4. **One `loop-run-log.md` JSON line per wave.**
