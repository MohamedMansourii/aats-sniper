# Wave 2C — dual-G3 Acceptance Artifact (2026-07-03)

**Commit:** `7722294` · **Full suite:** `2965 passed / 2 skipped / 0 failed` (re-run by the Runtime, not trusted
from agent reports) · **Workflow:** `wf_061f0fec-813`.

Closes the 2026-07-03 PROGRAM-REVIEW HIGH findings (Wave-2 catastrophic exits unwired live).

| Item | Owner | Reviewers (dual-G3) | Verdict |
|---|---|---|---|
| 2C-1 wire exit producers into the live loop + E19 StateStore/fast_loop + E2E test | agent-orchestration-engineer | code-reviewer + backtest-qa-engineer | **G3-PASS** |
| 2C-2 fix classify_direction() negation | nlp-sentiment-engineer | code-reviewer + backtest-qa-engineer | **G3-PASS** |

## Evidence the HIGH findings are actually fixed (Runtime-verified)
- **E19 StateStore methods now exist** — `get/set_lp_unlock_approaching_flag` in Protocol + InMemoryStateStore
  (21 refs, was 0); `fast_loop` reads + passes `lp_unlock_approaching_flag` into `on_tick` (was 0).
- **Producers now SET the flags live** — `InsiderDumpDetector` (`insider_dump.py:559`), `SellabilityReprober`
  (`sellability_reprobe.py:326`), `LpUnlockExitWatcher` (`lp_unlock_gate.py:765`), driven by the new
  `SlowLoopEnrichmentWiring` (`aats/controller/enrichment_wiring.py`) from the SLOW loop, OFF the FAST hot path.
- **STANDING E2E GATE satisfied** — `tests/controller/test_e2e_catastrophic_exits.py` (8 tests) drives each REAL
  producer through the orchestrator's public entrypoint (NOT hand-set flags) and asserts the position
  force-closes SECURE, **with a control test** (`test_control_no_producer_driven_position_holds`) proving the exit
  was CAUSED by the producer. Plus `test_fast_loop_no_llm_proof.py` (hot-path purity).
- **classify_direction negation** — `classify_direction('Not bullish … would not touch it')` now returns `None`
  (was `'long'`); mutation-tested.

## Note
`parallel[0]` reviewer hit one transient StructuredOutput failure on a strike; both items still reached G3-PASS on
clean reviewer runs. This is the FIRST wave to ship with its acceptance artifact + run-log line per the new
governance (STATE §4.8/§4.9). Prior waves' artifacts are backfill-pending (tracked in STATE).
