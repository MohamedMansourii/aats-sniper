# AATS — loop-budget.md

> Project budget overriding the global `~/.claude/loops/loop-budget.md` for the AATS elite-enhancement loop.
> The `loop-budget` skill (or the Runtime) reads this at the START of every wave and appends a run-log line at the
> END. Ultracode is on for this program (`correctness > cost`), so these are **runaway backstops, not targets** —
> but a backstop that never fires is not a budget.

## Kill switch
```
loop-pause-all: false      # inherits the global switch; set true to halt every loop
aats-loop-pause: false     # project-local pause; set true to stop AATS build waves (report-only)
```
Also pausable via `.agency/STATE.md` → a "Waiting On Human" note.

## Caps (tiers, not hard integers — sized to the plan; raise deliberately)
| Scope | Cap | Guard behavior |
|---|---|---|
| **Per wave** (one Workflow) | ~4M output tokens | at ~80% → finish current item then stop; ~100% → HALT the wave, log, escalate |
| **Per day** (all AATS waves) | ~20M output tokens | ≥80% → report-only (no new waves); ≥100% → hard stop until next day |
| **Max concurrent build waves** | **1** | never run two build workflows on the shared tree at once (overlapping-file corruption) |
| **Max fix strikes per item** | 3 | then HALT the wave + escalate (three-strikes law) |

## Cost reality (informational — from the run log)
Waves have cost ~0.25M–3.2M subagent tokens each (see `loop-run-log.md`). The full program's remaining waves
(2C, 3, 4, exec-audit) are budgeted at ≤ ~10M combined. The edge proof (Milestone E) is compute, not agent-heavy.

## The rule
At wave start: read this file + the kill switches; if paused or over the day cap → do NOT launch, write a
`no-op` run-log line + a STATE note, and stop. At wave end: append exactly one JSON line to `loop-run-log.md`
with the tokens_estimate + outcome + commit. Never let a wave run unlogged.
