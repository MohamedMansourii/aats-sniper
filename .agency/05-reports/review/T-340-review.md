# Code Review — T-340 (Triple-loop controller, single-writer FSM, atomic snipe→fast handoff)

**Reviewer:** code-reviewer (Quality Gate, G3 half) · **Date:** 2026-06-16
**Verdict:** **PASS** (no BLOCKER) — with two MAJOR fixes required before merge (R-1, R-2) and one advisory (R-3).
**One-line:** Core T-340 properties (loop boundaries, single-writer FSM, write-ahead atomic handoff, contract-invoking controller, DRY-RUN, integer money) are implemented and verified by execution; two non-blocking correctness/contract defects in peripheral code (hard-stop value, flatten-one endpoint) must be fixed before merge.

---

## Verification (run, not assumed)

| Command | Result |
|---|---|
| `python -m pytest tests/controller -q` | **49 passed** in 3.34s |
| `python -m pytest tests/controller tests/risk -q` | **334 passed** in 29.28s (no regression in the risk lane the controller integrates) |
| `python -m ruff check aats/controller/ tests/controller/` | All checks passed |
| `python -m mypy aats/controller/ --ignore-missing-imports` | 0 errors (exit 0) |
| `grep -riE 'private_key\|secret_key\|keypair\|mnemonic\|seed_phrase' aats/controller/` | no matches (clean) |

All ten engineer self-check claims reproduced.

---

## AATS review brief (ROSTER §5) — domain gates

1. LLM increases risk anywhere — **✓** SlowLoop `_apply_reasoning_verdict` is a de-risk allowlist; `ReasoningAction` has no risk-increase member (import-time static guard); `DeRiskIntentFactory` has no `entry()`; breaker reset gated by `OperatorResetToken`.
2. LLM / slow model / unbounded RPC on FAST critical path — **✓** Static (no LLM import, no `await` in `tick()`) + runtime (hung-LLM tick < 500ms) proofs. Narrative flag is a pre-read boolean; price feed injected/pre-subscribed; store reads synchronous.
3. Float money — **✓** Integer lamports throughout; pydantic validators + FSM `TypeError` on float PnL; Decimal for price math.
4. Lookahead / compute-time leak — **✓ (controller scope)** Decisions stamped on `event_time.block_time_ms`; wall-clock used only for the DMS heartbeat-age watchdog (correct).
5. Race in handoff / dedup / FSM — **✓** Atomic `claim_entering` (RLock SETNX) proven with 1000-thread + 100-thread tests (exactly one winner); single-writer FSM; write-ahead persist before side effect; deterministic idempotent intents.
6. Private key held/logged/serialized — **✓** No secrets in source; SimulationVenue key-less.
7. Edge < round-trip cost / skip safety gate — **✓** `EntryIntent` cost gate enforced in the constructor; breaker + cooldown + safety-gate + veto precede entry.
8. Social text as instructions — **N/A** here (no NLP); `ReasoningVerdict.reason` is quoted, never executed.

---

## Conformance

- **Blueprint (§2.1 loop boundaries, §2.2 write-ahead/single-writer):** ✓
- **data-models.md (FSMState transitions, Position, integer money, no win_rate):** ✓ — uses `validate_fsm_transition`; illegal transitions raise.
- **api-contracts.md (§5 control plane):** ✗ — `/api/flatten/{mint}` semantics wrong (R-2); POST status codes 200 vs contracted 202 (R-3).
- **Design system (UI):** N/A (no UI in this task).
- **Test presence & meaningfulness:** ✓ — assert behavior (race winner count, FSM terminal states, dedup, no-LLM static+runtime, asymmetric-trust). Two gaps noted (flatten-one untested; see R-2).

---

## Findings

### R-1 — MAJOR — `aats/controller/snipe_loop.py:312-318`
Hard-stop price computed as `spot_price * Decimal("6")` (6× spot) while the adjacent comment says "60% stop = entry price * 0.6". The stored `hard_stop_price_lamports` is ~10× too high.
- **Why it matters:** wrong value in a safety-critical, operator-surfaced field (`GET /api/positions`). Currently latent — the FAST loop/ExitEngine derive the real stop from the fill price + config drawdown and never read this field (confirmed: no read site) — but a self-contradicting safety field is a trap for the next reader and for any future code that trusts it.
- **Good looks like:** `* Decimal("0.6")` (or integer-space `* 6 // 10`); add a test asserting `hard_stop_price_lamports < entry_price`.

### R-2 — MAJOR — `aats/controller/control_api.py:224-234`
`POST /api/flatten/{mint}` calls `emergency_flatten_all(...)`, flattening **every** open position. AC-044 / api-contracts.md §5 require "only that mint; A and C MUST remain unchanged."
- **Why it matters:** frozen-contract violation; Lanes E (dashboard) and F (Telegram) bind to this contract. No test covers flatten-one, so the defect is invisible to the suite.
- **Good looks like:** a `flatten_one(mint)` handler path exiting only the named position; AC-044 test (A,B,C open → flatten B → A,C unchanged). File is labeled a T-341 stub, but the endpoint is wired and behaviorally wrong now.

### R-3 — MINOR — `aats/controller/control_api.py:210-234`
`POST /api/kill` and `/api/flatten*` return HTTP 200; api-contracts.md §5 specifies **202**.
- **Why it matters:** frozen-contract deviation; harmless until a client asserts on status.
- **Good looks like:** return 202 for these de-risk POSTs.

### R-4 — NIT — `aats/controller/fast_loop.py:425-453`
Forced/full exits hardcode `realized_pnl_net_lamports=0`; the breaker is fed only unrealized deltas and realized PnL on close is not booked.
- **Why it matters:** acceptable sim/DRY-RUN simplification for T-340, but the breaker's realized-PnL accounting must be wired in the T-327 Rust hot-core lane before live capital.
- **Good looks like:** book realized PnL on close into the breaker when the venue returns a real exit fill.

---

## Re-review checklist (for the fix round)
- R-1: hard-stop multiplier corrected + assertion test.
- R-2: flatten-one exits only the named mint + AC-044 test.
- R-3 (advisory): POST de-risk endpoints return 202.
- No regression: `pytest tests/controller tests/risk` stays green.

---

=== HANDOFF ===
FROM: code-reviewer
TASK: T-340 — Triple-loop controller, single-writer FSM, atomic snipe→fast handoff (DRY-RUN/SimulationVenue)
STATUS: COMPLETE
DELIVERABLES: .agency/05-reports/review/T-340-review.md
SELF-CHECK: Read all 9 controller modules + 7 test files + the contracts they invoke (positions, intents, venue, models, risk) + CircuitBreaker/ExitEngine signatures. Ran pytest (49 controller / 334 controller+risk all pass), ruff (clean), mypy (0 errors), secret grep (clean). Cross-checked api-contracts.md §5 and AC-044, data-models.md §7 FSM, AATS-ROSTER §5 brief.
VERDICT: PASS (no BLOCKER). Two MAJOR fixes required before merge: R-1 (hard-stop 10× value error, snipe_loop.py:312), R-2 (flatten-one flattens all, control_api.py:233 — AC-044 violation). One MINOR advisory: R-3 (POST 200 vs 202). One NIT: R-4 (realized-PnL booking for T-327).
RISKS: R-1 and R-2 are latent in DRY-RUN (no real capital, wrong field not on the live exit path) but must be fixed before G4/live. backtest-qa-engineer is the other G3 half — G3 is not closed until that PASS also lands.
NEEDS: orchestrator to route R-1/R-2 fixes back to agent-orchestration-engineer, then re-review; pair with backtest-qa-engineer verdict for G3.
===============
