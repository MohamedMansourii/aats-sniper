# G3 — WAVE D (Lane D controller integration + operator surfaces) — VERDICT RECORD

_Recorded: 2026-06-16 by `orchestrator`. Verified by reading the ACTUAL changed files under
`C:/dev/aats`, not trusting the engineer/review JSON. G3 is DUAL on every code task:
`code-reviewer` AND `backtest-qa-engineer` must both PASS (overlay rule 3)._

**Scope:** T-340 (controller triple-loop + snipe→fast handoff vs SimulationVenue),
T-341 (control-plane API to the FROZEN T-201 contract), T-342 (FAST Layer-2 enforcer + DMS
heartbeat wiring), T-352 (dashboard live-wire), T-353 (dashboard feature pages), T-360
(Telegram alerts), T-361 (Telegram de-risk commands).

---

## Verdict table

| Task | code-reviewer | backtest-qa | G3 | Status | Evidence (source-confirmed) |
|---|---|---|---|---|---|
| **T-340** controller integration | **FAIL** | **FAIL** | **FAIL** | **NEEDS-REPLAN (att.2)** | BLK-1 FIXED; **BLK-2 NOT FIXED — vacuous safety test** |
| **T-341** control-plane API | PASS | **FAIL** | **FAIL** | **NEEDS-REPLAN (att.2)** | 3/4 targets mutation-proven; **QA-MAJOR-1 — vacuous de-risk test** |
| **T-342** FAST enforcer + DMS | PASS | PASS | **PASS** | **DONE (att.2)** | B1/B2/B3 + M1/M2 fixed; process-death→DMS flatten proven-by-firing |
| **T-352** dashboard live-wire | PASS | n/a (UI) | **PASS** | **DONE (att.2)** | BLOCKER R-01 fixed; `setMode` sends canonical wire enum |
| **T-353** dashboard feature pages | PASS | n/a (UI) | **PASS** | **DONE (att.1)** | additive, contract-respecting, honesty-clause-clean; 74/74 vitest |
| **T-360** Telegram alerts | PASS | PASS | **PASS** | **DONE (att.1)** | outbound-only, redacted token, 3 alert classes; 4 mutants KILLED |
| **T-361** Telegram de-risk cmds | PASS | PASS | **PASS** | **DONE (att.1)** | de-risk-only structural, authz-gated, confirm-nonce; 22-verb mutation probe |

**5 of 7 DONE. 2 NEEDS-REPLAN (both test-only re-spins — production code is correct in both).**

---

## NEEDS-REPLAN — confirmed from source

### T-340 — BLK-2 REOPEN (BLOCKER, dual-reviewer agreement)

**Confirmed by reading `tests/controller/test_snipe_handoff.py:462` + `aats/controller/fast_loop.py:81-167`.**

`test_breaker_trip_via_fast_loop_tick_calls_flatten_handler` drives a breaker trip by
`price_feed.set_price(mint, Decimal("0"))` (line 609). Mark=0 simultaneously breaches the
ExitEngine **position hard-stop** in the same `FastLoop.tick()`. The three assertions
(breaker TRIPPED / `mint in venue.exit_calls` / FSM CLOSED) are all satisfied by the
ExitEngine's own `hard_stop` exit — **NOT** by `FastLoopFlattenHandler.emergency_flatten_all`.

Both reviewers independently mutated `emergency_flatten_all` to a bare `return` (no-op) and
re-ran `pytest tests/controller/` → **52/52 STILL PASS** (mutation NOT killed). The
controller's circuit-breaker→flatten de-risk handoff (AC-028) remains unproven: a fully
broken flatten handler would ship undetected. A test that cannot fail on the defect it
targets is a BLOCKER under the review charter.

**BLK-1 is genuinely fixed** (both reviewers confirm): the two new tests use distinct slots →
distinct intent_ids → bypass `mark_intent_seen`, isolating `claim_entering()` as the sole
guard; mutating `InMemoryStateStore.claim_entering` (`state.py:224`) to always-True drives
7 tests RED. Load-bearing confirmed.

**Re-entry criteria (T-340 fix):** the BLK-2 test must trip the breaker WITHOUT also breaching
the position hard-stop in the same tick — e.g. feed a direct `record_pnl` realized loss, or
keep mark above the hard-stop while tripping on realized PnL; OR assert specifically on the
flatten reason (`intent.reason.startswith("emergency_flatten")`) and call ordering. Confirm by
mutation: a no-op `emergency_flatten_all` MUST make the test go RED. No production change required.

### T-341 — QA-MAJOR-1 (MAJOR, backtest-qa FAIL; G3 is dual → FAIL)

**Confirmed by reading `tests/control_plane/test_post_commands.py:538-612` +
`aats/control_plane/server.py:270-333`.** Production code is **CORRECT** — `_coerce`
(`server.py:297`) Decimal-parses the decimal-string fields before comparison and rejects the
lexicographic trap. The defect is **purely test coverage**.

The two BLOCKER-1 regression tests are mutation-vacuous on the safety-critical de-risk path:
- `test_post_risk_config_daily_loss_pct_widen_rejected_numeric` — Step 4 (`1.0 → 10.0`)
  asserts `status_code in (400, 403)` (line 586). The RiskConfig model floor returns 400
  regardless of the Decimal fix, masking the trap. The `1.0 → 2.5` step agrees under string
  AND numeric ordering. So deleting the `_coerce` Decimal branch leaves all 96 tests GREEN.
- `test_post_risk_config_jito_tip_widen_rejected_numeric` — `0.10 → 0.25` also agrees under
  both string and numeric ordering.

Direct trap proof (reviewer-run): `_validate_risk_config_tighten_only({'daily_loss_limit_pct':
'2.5'}, {'daily_loss_limit_pct':'10.0'})` returns `[]` under the mutation (lexicographic
`'10.0' > '2.5'` is False → WIDEN of the loss limit silently ACCEPTED) but
`['daily_loss_limit_pct']` with the fix. The suite would not catch a regression to the bug.

code-reviewer (the other G3 half) PASSed — all 3 other targets (flatten-one isolation AC-044,
kill→flatten within 2s AC-040, FeedBus/SSE delivery) are mutation-proven non-vacuous, and the
frozen-contract conformance holds (endpoint set §12, LatencyHop `"class"` wire key, de-risk-only,
money int-lamports/Decimal-string, no win-rate AC-037, auth on every POST). Per overlay rule 3,
**one FAIL = G3 FAIL**.

**Re-entry criteria (T-341 fix):** add a regression test asserting the WIDEN is rejected for a
current<proposed pair where lexicographic and numeric ordering DISAGREE (cleanest: a direct unit
test on `_validate_risk_config_tighten_only` asserting the `'2.5' → '10.0'` daily-loss-limit pair
== `['daily_loss_limit_pct']`, plus an analogous jito_tip_cap_frac trap in its valid (0,1] range).
Then revert the `_coerce` Decimal branch and confirm the new test goes RED before resubmitting.
No production change required.

---

## DONE — dual-PASS confirmed from source

- **T-342** (`enforcer_wiring.py`, `fast_loop.py`, `dms_service.py`): `FastLoopEnforcerWiring`
  is imported and wired into `FastLoop` — `tick()` calls `wiring.on_tick()` (the single Layer-2
  enforcement + DMS heartbeat path, `fast_loop.py:412`), `reconcile_fill()` calls `wiring.arm()`
  on ENTERING→OPEN (`:516`), exits call `wiring.deregister()` (`:589,:611`). `HeartbeatWriter`
  Protocol is write-only (no `type: ignore`). process-death→DMS flatten proven-by-firing (DMS
  `flatten_calls == 1` after ticks stop and clock advances past T_DMS); three independent runtime
  mutations (enforcer / DMS age / heartbeat) each drive the right tests RED. AC-026 (≤50ms,
  measured p99≈6.3ms@100 positions) + AC-027 PASS. No await/LLM/RPC on FAST path (static scan).

- **T-352** (`dashboard/src/lib/api.ts:770`): BLOCKER R-01 fixed — `setMode` now sends the
  canonical wire enum `{ mode: toWireMode(m) }` (paper→PAPER, dry-run→LIVE_DRY_RUN, live→LIVE),
  mirroring `fromWireMode` on the read path; the prior lowercase body failed pydantic
  `ModeRequest` validation (400). LIVE stays server-fenced 403 (de-risk-only preserved). tsc 0;
  46/46 vitest; mock build GREEN; eslint clean.

- **T-353** (dashboard, 21 files): copy-trade SELECTIVITY page (explicit "not a buy trigger",
  no buy/mirror/size control — asserted by negative test), AutoStrat/RestingOrders panels
  (configure EXITS only), token-safety RedFlags kit + SnipeFeed column + Risk aggregation,
  Positions net-of-cost-PRIMARY P&L cards + CSV/JSON export (`pnl_basis=net_of_cost...`). No new
  POST/risk-increasing endpoint (3 new GETs quarantined in `OPTIONAL_ENDPOINTS`, probe-and-fallback,
  documented as a future architect delta). No win-rate; money via int-lamport/Decimal-string
  adapter seam. 74/74 vitest; build GREEN (CopyTrade code-split); eslint clean.

- **T-360** (`aats/telegram/`, outbound ALERT channel): consumes FROZEN SnipeEvent feed (§6) via
  injectable FeedSource; 3 alert classes (FILL/RUG_AVOIDED/BREAKER_TRIP, rising-edge once).
  Outbound-only — exposes no command, cannot increase risk by construction. Token env-only +
  redaction chokepoint (source-grep test). Money int-lamports via Decimal (float→TypeError); no
  win-rate. 42 tests; 3 mutants KILLED (1 survivor is defense-in-depth latch+dedupe, non-blocking).

- **T-361** (`aats/telegram/`, de-risk command set): EXACTLY `/status /kill /flatten <mint>
  /pause` against FROZEN §7. De-risk-only is STRUCTURAL — closed `_KNOWN_COMMANDS` set;
  ControlPlaneClient Protocol exposes only 4 read/de-risk methods; `pause()` posts a hard-coded
  `SHADOW` constant (not a parameter) so mode cannot move UP from Telegram at all. Operator-ID
  allowlist gate-1 on every update (empty→fail-closed, unauthorized→dropped+fingerprint-log, ZERO
  control-plane calls). Per-command single-use TTL-bound confirm nonce on kill/flatten. 86 tests
  (44 new); adversarial probe: 22 risk-increasing/garbage verbs from the authorized operator →
  ZERO control-plane calls.

---

## MILESTONE — "bot runs end-to-end on SimulationVenue (paper) and is driveable via control-plane"

**NOT YET ACHIEVED — gated on two test-only re-spins (T-340, T-341).**

The controller core (`FastLoop` + snipe→fast handoff + FSM), the FAST Layer-2 enforcer + DMS
wiring (T-342 DONE), the control-plane server (endpoints conform to the frozen contract per the
code-reviewer PASS), and ALL operator surfaces — dashboard live-wire (T-352), dashboard feature
pages (T-353), Telegram alerts (T-360), Telegram de-risk commands (T-361) — are DONE and
source-verified. The drive-the-bot plumbing is in place and de-risk-only end-to-end.

The milestone is blocked because **G3 is not yet PASS on T-340 (the controller integration that
runs the loop vs SimulationVenue) and T-341 (the control-plane API)**. In BOTH cases the shipped
production code is CORRECT — the blocking finding is a vacuous test on a SAFETY/de-risk path
(breaker→flatten handoff; risk-config widen-rejection). Under the charter these CANNOT be waved
through: a safety-critical test that survives mutation of the code it guards is a false-confidence
ship risk on exactly the de-risk paths real capital depends on.

**This is a clean dual single-strike re-spin (attempt 3 on each, test-only, no production change,
no escalation):** add the mutation-meaningful test, prove it RED under the documented mutation,
re-run the suite, resubmit for the dual G3 re-review. The end-to-end driveable milestone is
declared the moment T-340 + T-341 clear dual G3.

---

## Carry-forwards from this wave (NON-BLOCKING, to G4/owners)

- **T-340 fix scope-note (from T-342 review M1):** `test_tick_breach_fires_venue_exit_via_wiring`
  (`test_t342_enforcer.py:1204`) is also masked by the ExitEngine hard-stop on the same below-trigger
  tick — should assert on the Layer-2 intent prefix `stop_exit:`. Bundle into the T-340 re-spin since
  it's the same ExitEngine-masking class of defect. Dedicated direct-wiring tests DO isolate the
  enforcer, so coverage is not lost.
- **T-341 carry-forwards (out of THIS fix's scope, still open from prior round, → G4/owners):**
  R-1 `snipe_loop.py:312` hard-stop `*Decimal("6")` ~10× value; R-3 POST 200 vs 202. (R-2 flatten-one
  is now FIXED per T-341 code-reviewer.)
- **T-352:** `daily_net_pnl_day_utc` (ADR-0012) not yet surfaced on dashboard; the 3 new GET
  projections need an architect-owned contract delta before they serve live.
- **T-360/T-361:** production wiring of the real SSE/Redis FeedSource + HttpTelegramClient/poller +
  Vault token resolution is a runtime-assembly step at G4 (seams defined + offline-faked here).
- **T-361 spec tension (non-defect):** AC-043 "reply with authz-failure" vs custody-policy §7
  "drop silently" — impl follows the more-specific frozen custody contract (correct precedence;
  replying would be the info-leak the policy forbids).

**Full-suite consolidated `pytest tests/ -q` count remains a RUNTIME action** (purge `__pycache__`,
pin `PYTHONHASHSEED`) before G4.
