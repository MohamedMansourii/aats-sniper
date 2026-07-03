# AATS Program Review — 2026-07-03 (adversarial 5-auditor swarm, HEAD 5209933)

**Overall verdict: NEEDS-WORK.** Honest answers to the two questions:
- **Is it a completed work of art?** ❌ Not yet. The **safety spine IS a work of art** — but "completed" is false.
- **Would a different model resume seamlessly?** ❌ Resumable-with-caveats, not seamless (doc drift).

## What is genuinely excellent (proven, not claimed)
Five independent auditors executed **forged malicious signals under `python -O`** and could NOT find any path
where a signal increases size, widens/relaxes a stop, raises conviction, or adds leverage. The de-risk-only /
asymmetric-trust law is enforced with **real `ValueError` guards (survive `-O`), clamps, and closed Intent
unions** — belt-and-suspenders. Point-in-time honesty is structural (strict PIT filters first, refuse-by-default,
wall-clock only in monitoring-only fields). **No win-rate anywhere** (runtime HONESTY-CLAUSE assertions at the
API). FAST/SNIPE hot path is provably pure. 343 targeted + 2911 full-suite tests green. **The bot cannot add
risk or fabricate performance — that is real and important.**

## The real defects (fix-first — none is a capital-safety emergency; all fail safe under DRY_RUN/paper)
| # | Sev | Finding |
|---|---|---|
| 1 | **HIGH** | **Wave-2 catastrophic exits are UNWIRED live.** No controller loop SETS the insider-dump (E14), sellability (E17), or LP-unlock (E19) flags on real ticks — SlowLoop sets only `narrative_failure`. So these exits provide **zero live protection at HEAD** (positions still rely on the wired hard-stop + narrative-failure exit). Built + unit-tested, not integrated. |
| 2 | **HIGH** | **E19 LP-unlock exit is doubly dead** — no `get/set_lp_unlock_approaching_flag` exists in the StateStore Protocol or InMemoryStateStore, and `fast_loop` never reads/passes it. The branch in `exit_engine.py` is unreachable in production. |
| 3 | MED | **Overclaims:** "Milestone B COMPLETE" and "KOL edge LIVE end-to-end" overstate reality. `pipeline.py` does not import `caller_score`/`call_extract` — the KOL-call half is built-but-unwired (smart-money wallet copy-signal IS live). |
| 4 | MED | **Continuation docs lag a full wave** — MISSION-BOARD/MILESTONES/ROADMAP still show E16 unreviewed, E18/E19 queued, Milestone B 70/110, contradicting STATE. A resumer opening MISSION-BOARD first re-dispatches finished work. STATE also records the wrong HEAD. |
| 5 | MED | **No dual-G3 acceptance ARTIFACTS** exist under `.agency/05-reports/` for E15/E16/E18/E19/Wave-1 — acceptance was asserted in prose only. (This report begins correcting that.) |
| 6 | MED | **`classify_direction()` negation blind spot** — "Not bullish… would not touch it" → classified `long`. Dormant (CA-extraction not wired) but real; fix before wiring the caller path. |
| 7 | LOW | insider_dump approximate `_seen` eviction can re-count past 50k; stale STUB/PLUG_IN_HERE docstrings in transport.py/smart_money.py; de-risk flag PRESENCE expires on wall-clock TTL (live/backtest-parity caveat). |

## Remediation (assigned)
- **Findings 1, 2 → Wave 2C** (`agent-orchestration-engineer` + `risk-guardrails`): wire all Wave-2 producers into
  the live loop, add the E19 StateStore methods + fast_loop read/pass, with END-TO-END integration tests proving
  each exit fires live. Dual-G3.
- **Finding 6 → Wave 2C** (`nlp-sentiment-engineer`): add negation guards + tests to `classify_direction`.
- **Findings 3, 4, 5 → Runtime (me):** reconcile the docs to reality, downgrade the overclaims, fix HEAD, and
  record acceptance artifacts (this file + per-wave records).
- **Finding 7 → backlog** (LOW; fold into Wave 2C or a cleanup pass).

## Recommendation (verbatim from the edge oracle)
**FIX-FIRST.** Do not mark Milestone B complete; do not treat the waves as auditable-complete; and (unchanged)
do not scale past paper/SHADOW. The foundation is elite and safe; the integration and the claims need to catch up.
