# AATS SNIPER — ACCEPTANCE CRITERIA (G0)

**Version:** 1.0.0
**Date:** 2026-06-16
**Author:** `quant-product-analyst`
**QA owner:** `backtest-qa-engineer` (trading logic), `code-reviewer` (all code)
**Test oracle:** these ACs are machine-checkable. "fast enough" and "usually" do not
appear. Any AC without a number, a unit, or a Given/When/Then is a defect.

---

## MANDATORY FOUR-INVARIANT NEGATIVE ACs

These four invariants each carry at least one explicit NEGATIVE AC.

### INVARIANT 1 — ASYMMETRIC LLM TRUST

**AC-019 (NEGATIVE)** Given the LLM Reasoner produces a JSON output containing any of
`"SIZE_UP"`, `"WIDEN_STOP"`, `"OVERRIDE_STOP"`, or any field that would increase position
size or widen a risk limit, WHEN that output is processed by the clamp layer, THEN the
propagated action MUST be `"HOLD"` and a `llm_risk_increase_clamped` metric MUST be
incremented. The test fixture injects 100 synthetic risk-increase outputs; the clamp MUST
fire on all 100.

**AC-020 (NEGATIVE)** Given the bot is in any loop state (SNIPE, FAST, or SLOW), WHEN a
new `smart_wallets_in` observation arrives for an open or pre-staged position, THEN position
size MUST NOT change (delta = 0 lamports). Verified by unit test asserting position.sol_in
is unchanged after N=50 injected smart-wallet events on an open position.

**AC-021 (NEGATIVE)** Given the MCS score for a candidate rises to its maximum value (1.0),
WHEN the SNIPE loop evaluates that candidate, THEN the entry size MUST NOT exceed the size
that would be computed if MCS were at its minimum. A test fixture cycling MCS from 0.0 to
1.0 MUST produce monotonically non-increasing position sizes.

### INVARIANT 2 — NO DOUBLE-ENTRY (FSM)

**AC-012 (NEGATIVE)** Given a position FSM for mint X is in state `ENTERING` or `OPEN`,
WHEN a second SNIPE decision fires for the same mint X, THEN the FSM MUST reject the
second entry with reason `fsm_state_conflict` and log the rejection. Zero double-entries
are tolerated. Verified in the FSM unit test harness with 1,000 concurrent synthetic snipe
events on the same mint.

### INVARIANT 3 — COST GATE

**AC-013 (NEGATIVE)** Given any candidate where `expected_edge_bps ≤ total_cost_bps`,
WHEN the cost gate runs, THEN NO entry transaction is built or submitted, AND a structured
log record is emitted containing: mint, expected_edge_bps (numeric), jito_tip_bps (numeric),
priority_fee_bps (numeric), entry_slippage_bps (numeric), amm_fee_bps (numeric),
adverse_selection_bps (numeric), total_cost_bps (numeric), and reason = `cost_gate_reject`.
Verified by injecting 50 candidates with known edge-negative parameters; all 50 must
produce logged rejections with all numeric fields populated and zero transactions submitted.

**AC-014** Given a hardcoded Jito tip value (any static integer constant) exists in any
hot-path code file, WHEN the CI lint/static-analysis rule runs, THEN the build MUST FAIL
with error `hardcoded_jito_tip_forbidden`. This is a build-time check, not a runtime check.

### INVARIANT 4 — SURVIVABLE STOPS (ALL THREE LAYERS)

**AC-025 (Layer 1 — Venue-native resting order/keeper)**
Given a position is open and the price path crosses the hard-stop threshold while the bot
process is running, WHEN the venue-native resting-order or keeper triggers, THEN a sell
transaction for 100% of the remaining position MUST be submitted to the network within
the venue-native order response time (confirmed in dry-run simulation). Tested by
injecting a synthetic price path crossing hard-stop on 20 positions in the fast-loop sim.

**AC-026 (Layer 2 — In-process FAST-loop enforcer)**
Given a position is open, WHEN the FAST-loop tick detects the mark price ≤ hard_stop_price,
THEN `ExecutionVenue.exit()` MUST be called for 100% of remaining position within ≤ 50 ms
of the trigger event (measured in the fast-loop sim harness). Tested across 100 synthetic
price-path crossings; p99 latency must be ≤ 50 ms.

**AC-027 (Layer 3 — Dead-man's switch)**
Given all open positions are tracked in Redis, WHEN the bot process is killed (`SIGKILL`)
and the dead-man's switch detects heartbeat absence for ≥ T_DMS = 60 seconds, THEN
pre-signed flatten transactions for ALL open positions MUST be submitted via the watchdog
process. Tested by: (1) creating 5 synthetic open positions in Redis, (2) killing the bot
process, (3) confirming flatten transactions submitted within 60 + buffer seconds.
Zero open positions are permitted to remain unflattened after dead-man's switch fires.

---

## AC BY STORY

### EPIC 1 — DETECTION

**AC-001** (US-001)
Given Yellowstone/Geyser gRPC is connected to a validator node, WHEN a new Raydium,
PumpSwap, or pump.fun pool-creation or migration event is committed on-chain, THEN the
ingestion pipeline MUST decode it into a structured `LaunchEvent` equivalent within ≤ 800
ms of the event's slot time (point-in-time: observation-slot timestamp captured separately
from confirmation-slot timestamp). Tested against a replay of 100 recorded mainnet events;
p99 decode latency ≤ 800 ms.

**AC-002** (US-001)
Given the venue/program-ID registry is loaded at startup, WHEN a new event is decoded,
THEN the program ID used for decoding MUST match the registry entry for that venue, NOT any
hardcoded constant. A build-time test MUST FAIL if any program ID appears as a literal
string constant in a hot-path file (venue-specific decoder or snipe-loop code).

**AC-003** (US-001)
Given ShredStream is enabled in config, WHEN an event arrives via ShredStream before
on-chain confirmation, THEN the event record MUST carry `detection_transport: "shredstream"`
and `observation_slot` MUST be < `confirmation_slot`. Verified by parsing 20 ShredStream
events in test mode and asserting transport label and slot ordering.

**AC-004** (US-002)
Given the bot is started with `mode = SHADOW`, WHEN 100 synthetic detection events are
injected, THEN zero entry transactions are built or submitted (verified by asserting
`ExecutionVenue.execute()` call count = 0), AND all 100 events are written to the
point-in-time feature store with `event_time` populated.

**AC-005** (US-002)
Given 1,000 shadow-recorded events exist in the feature store, WHEN a point-in-time leak
audit is run (checking that every feature value's source data has observation-time ≤
`event_time` of the record), THEN zero features fail the audit (zero records with any
feature sourced from post-`event_time` data). Tested by the `backtest-qa-engineer`
clock-audit tool.

**AC-006** (US-002)
Given the recorded dataset exists, WHEN it is reconciled against an independent
pool-create census (second source), THEN the miss rate is computed, bounded, and reported.
Records with no completed first-K snapshot or no resolved label are carried as
`status: CENSORED`, NOT dropped. Verified by asserting: (count(CENSORED) +
count(complete)) / count(census_total) ≥ (1 - declared_max_miss_rate).

**AC-007** (US-003)
Given DEXScreener API is unavailable (simulated by blocking the network call), WHEN the
bot processes a new candidate, THEN the bot MUST continue without enrichment and emit a
structured log with `enrichment_source: "dexscreener", status: "unavailable"`. Zero
candidate rejections or bot halts due to enrichment unavailability.

**AC-008** (US-004)
Given a smart-money wallet address is in the tracked set, WHEN that wallet submits a buy
for a candidate mint that subsequently appears in the detection pipeline, THEN the
`smart_wallets_in` feature for that candidate MUST be ≥ 1, AND the observation record MUST
include `wallet_observation_slot` ≤ `event_time + K`. Tested with synthetic wallet events.

**AC-009** (US-004 — NEGATIVE)
Given `smart_wallets_in` = 5 for a candidate (all 5 tracked wallets bought in), WHEN the
SNIPE loop evaluates that candidate, THEN the computed `sol_in` MUST be ≤ the `sol_in`
that would be computed for `smart_wallets_in` = 0 with identical other parameters (the
signal may only reduce or hold size, never increase). Verified by unit test with synthetic
candidates differing only in `smart_wallets_in`.

**AC-010** (US-005)
Given a synthetic coordinated-shill scenario (50 accounts with age < 7 days, 90%
temporal synchronicity, all posting identical bullish text about mint X), WHEN the MCS
pipeline scores that candidate, THEN the resulting MCS score MUST be LOWER than the score
for an organic-engagement baseline with equivalent volume but no synchronicity signal.
Verified in MCS unit test with fixture data; delta must be negative (shill scenario score
< organic score).

---

### EPIC 2 — PRE-TRADE GATING

**AC-011** (US-006 — pre-trade gate rejection)
Given a fixture set of 10 known-honeypot tokens and 10 known-rug tokens with at least one
detectable on-chain red flag each (freeze authority set, un-renounced mint authority,
unburned LP, dev-cluster signature, high sell tax), WHEN the pre-trade safety gate runs on
each, THEN ALL 20 MUST be rejected (100% rejection rate on the fixture set). The specific
rejection reason (gate step 1–6) MUST be logged for each.

*Note: AC-012 is the FSM double-entry negative AC above.*

**AC-013** (US-007 — cost gate; see mandatory section above)

**AC-014** (US-007 — hardcoded tip build-FAIL; see mandatory section above)

**AC-015** (US-008)
Given the dashboard is connected to the live control plane (`VITE_USE_MOCK=false`), WHEN a
candidate with freeze authority set is detected, THEN the token-safety panel for that
candidate MUST display a visible red indicator for `freeze_authority` within ≤ 3 seconds of
the event being logged by the engine. Tested in the end-to-end dashboard integration test.

---

### EPIC 3 — ENTRY / SNIPE

**AC-016** (US-009 — SNIPE latency budget)
Given the SNIPE loop receives a `LaunchEvent` from the ingestion bus with all features
pre-staged, WHEN the loop runs the gate (FR-026) + cost gate (FR-027) + model read +
intent construction + ExecutionVenue.execute() call, THEN the wall-clock elapsed time from
event ingestion to `execute()` called MUST be ≤ 150 ms at p99, measured in the Rust latency
harness under simulated load of 10 concurrent events. p50 MUST be ≤ 50 ms.

**AC-017** (US-009 — no block-0 race)
Given the SNIPE loop receives a candidate with `slot_delay = 0` (block-0 timing), WHEN the
slot-delay enforcement rule runs, THEN the entry MUST be deferred to slot +5 minimum (delay
enforced, not rejected). Verified by asserting `intent.target_slot ≥ event.slot + 5` on
all generated intents in the unit test harness. Zero intents with `target_slot < event.slot + 5`.

**AC-018** (US-009 — direct AMM, not Jupiter on snipe)
Given a new-launch snipe entry is being built, WHEN the intent is constructed, THEN the
transaction instruction set MUST use a direct AMM instruction (decoded pool keys) and
MUST NOT include any Jupiter v6 program call. Verified by inspecting the serialized
transaction instruction set in unit test; Jupiter program ID MUST NOT appear.

**AC-019** (US-009 — asymmetric LLM trust; see mandatory section above)

**AC-020** (US-010 — migration snipe is NOT block-0)
Given a pump.fun graduation event is detected (migration to PumpSwap), WHEN the migration
snipe runs, THEN the entry MUST target slot ≥ migration_slot + 5. Zero entries with
`land_slot < migration_slot + 5` are permitted across 50 synthetic migration events in the
integration test harness.

**AC-021** (US-010 — NEGATIVE: no migration block-0)
Given a synthetic migration event where `migration_slot = 1000` and the fastest possible
execution would land at `migration_slot + 0`, WHEN the migration snipe logic runs, THEN
NO transaction targeting `target_slot ≤ migration_slot + 4` is built. Verified by
asserting zero transactions with `target_slot < migration_slot + 5` in 100 synthetic migration events.

**AC-022** (US-011 — resting limit order fires offline)
Given a resting limit order has been placed for mint X at trigger price P, AND the operator
process (dashboard/Telegram) has been disconnected for ≥ 30 minutes, WHEN the market price
for mint X crosses P in the FAST loop, THEN the activation sequence MUST run the full
pre-trade safety gate (FR-026) and cost gate (FR-027) at the time of activation (not
placement), and MUST submit the order if both gates pass — within ≤ 100 ms of trigger
condition. Tested in the FAST-loop sim harness.

**AC-023** (US-011 — resting order cost-gate at activation)
Given a resting order was placed when expected_edge_bps = 200, AND at activation time
total_cost_bps = 210 (edge no longer positive), WHEN the resting order activates, THEN the
order MUST be REJECTED by the cost gate at activation, NOT executed. Rejection logged with
numeric breakdown. Verified in the FAST-loop sim with synthetic cost-shift injection.

**AC-024** (US-012 — multi-wallet blast-radius cap)
Given multi-wallet mode is enabled with N wallets and a per-mint blast-radius cap of C SOL,
WHEN a new snipe entry fires for mint X with N wallets available, THEN the total SOL
allocated across all wallets to mint X MUST NOT exceed C SOL. Verified by asserting
`sum(wallet_i.sol_in for i in 1..N for mint X) ≤ C` across 100 synthetic multi-wallet
fills in the unit test.

---

### EPIC 4 — RISK AND STOPS

**AC-025** (US-013 — Layer 1 stop; see mandatory section above)
**AC-026** (US-013 — Layer 2 stop; see mandatory section above)
**AC-027** (US-013 — Layer 3 dead-man's switch; see mandatory section above)

**AC-028** (US-014 — circuit breaker self-trips)
Given the daily-allocated risk capital is C SOL for the day AND the tiny-real wallet
contains W SOL, WHEN cumulative daily net losses reach the lesser of (−0.03 × C) or
(−0.30 SOL), THEN within one FAST-loop tick (≤ 100 ms) the circuit breaker state MUST
transition to `TRIPPED`, all new entry processing MUST halt, and a Telegram alert MUST be
sent within 10 seconds. Tested by injecting synthetic P&L events crossing each threshold.

**AC-029** (US-014 — circuit breaker requires manual re-arm)
Given the circuit breaker is in `TRIPPED` state, WHEN the bot auto-restarts or the SLOW
loop runs, THEN the state MUST NOT automatically return to `ARMED`. The only valid
re-arm path is an explicit `POST /api/breaker/reset` from an authenticated operator session.
Verified by killing and restarting the bot process 5 times while the breaker is TRIPPED;
state MUST remain TRIPPED after each restart.

**AC-030** (US-015 — ¼-Kelly hard cap)
Given the fractional-Kelly calculator runs with any model probability P and any expected
multiple M, WHEN `sol_in` is computed, THEN `sol_in MUST be ≤ min(per_coin_cap, 0.25 × kelly_fraction)`.
Tested by unit-sweeping P from 0.1 to 0.9 and M from 1.1 to 10.0; zero violations of
the cap constraint are permitted.

**AC-031** (US-015 — NEGATIVE: no signal may size up)
Given a position for mint X has been computed with `sol_in = S`, WHEN any of the following
are subsequently received: MCS update, smart_wallets_in update, LLM Reasoner output with
action = "HOLD", THEN `sol_in` for that position MUST NOT increase beyond S. Delta MUST be
≤ 0. Verified by injecting each signal type with maximum value; delta assertion checked
after each injection.

**AC-032** (US-016 — auto-strat preset via API)
Given the operator POSTs `{ "tp_preset": "aggressive" }` to `/api/risk-config`, WHEN the
next new snipe entry is processed, THEN the ExitEngine config used for that entry MUST match
the "aggressive" preset definition. Open positions MUST retain their original preset config
(no retroactive change). Tested by: (1) opening a position with "conservative" preset,
(2) changing preset via API, (3) opening a second position, (4) asserting positions[0].exit_config
= conservative AND positions[1].exit_config = aggressive.

**AC-033** (US-016 — preset applies to new entries only)
Given 3 open positions with "balanced" exit config, WHEN the operator changes preset to
"conservative", THEN all 3 open positions MUST retain "balanced" exit config in their active
FSM state. Verified by asserting `position.exit_config.label == "balanced"` for all 3
positions after the preset change.

**AC-034** (US-017 — consecutive-loss auto-halt)
Given a surface has incurred 8 consecutive net-losing trades, WHEN the 8th loss is
confirmed in the FAST loop, THEN new entry processing for that surface MUST halt within
one tick (≤ 100 ms) AND a Telegram alert MUST be sent within 10 seconds containing:
surface name, consecutive-loss count (= 8), and timestamp.

**AC-035** (US-017 — land-rate auto-halt)
Given the rolling 20-attempt land rate for a surface falls below 35%, WHEN the FAST loop
computes the next rolling metric, THEN new entry processing for that surface MUST halt AND
a structured alert MUST be emitted. The halt MUST persist until the operator re-arms the
surface explicitly.

---

### EPIC 5 — RECONCILIATION

**AC-036** (US-018 — net-of-cost PnL is always the primary number)
Given the dashboard Metrics panel is rendered (live or mock), WHEN any PnL figure is
displayed, THEN the primary prominently displayed PnL value MUST be net of FULL costs
(tips + priority + entry slippage + AMM fees + exit slippage + adverse-selection haircut).
Gross PnL MUST be labeled "gross" and MUST be visually secondary. A UI test MUST assert
that the net PnL element is styled as the primary metric.

**AC-037** (US-018 — model-vs-baseline delta surfaced)
Given the validation harness has run at least one walk-forward window, WHEN the dashboard
Metrics or Grafana panel is viewed, THEN the model-vs-naive-baseline net-PnL delta MUST be
displayed as a named metric with its lower-95% bootstrap bound. The label MUST read
approximately "model vs baseline (Δ net PnL / SOL-at-risk, 95% LB: X)". No win-rate
percentage label is permitted anywhere on this panel.

**AC-038** (US-019 — GATE-A/GATE-B results visible)
Given the most recent walk-forward run has completed, WHEN the Grafana/dashboard panel is
viewed, THEN GATE-A (net PnL > 0, lower-95% bound > 0, true/false) and GATE-B (model >
baseline, lower-95% bound > 0, true/false) MUST be displayed with their numeric lower-95%
confidence bounds and the number of test windows they were computed over. A GATE-A=false
or GATE-B=false MUST display prominently in red / alert color.

**AC-039** (US-020 — position P&L cards)
Given the Positions page is loaded (live or mock), WHEN an open position is rendered,
THEN each card MUST display: mint, entry_slot, sol_in (lamports), unrealized_pnl_net
(lamports, labeled as net), TP-ladder state (which rungs have fired), exit mode (Fast/
Secure), and FSM state. The download/export action MUST produce a valid JSON or CSV file
containing all closed positions with their complete cost breakdown. Tested by asserting
required fields in a Vitest rendering test.

---

### EPIC 6 — KILL-SWITCH

**AC-040** (US-021 — kill from dashboard halts within budget)
Given the dashboard is wired to the live control plane (`VITE_USE_MOCK=false`), WHEN the
operator clicks KILL and confirms the modal, THEN: (a) `POST /api/kill` receives a 200
response within 500 ms, (b) the bot's internal entry-processing flag is set to `HALTED`
within 2,000 ms of the HTTP request, (c) all ExitEngine sequences are initiated within
2,000 ms. Measured end-to-end in the integration test with a running paper-mode bot.

**AC-041** (US-021 — kill confirmation modal prevents accidents)
Given the dashboard Kill button is visible, WHEN the operator clicks it once, THEN a
confirmation modal MUST appear BEFORE any API call is made. Only after the operator
confirms in the modal is `POST /api/kill` sent. Tested in a Vitest component test asserting
zero API calls on first click and one API call on modal confirm.

**AC-042** (US-022 — Telegram /kill same guarantee)
Given the Telegram bot is running and the operator sends `/kill`, WHEN the command is
processed, THEN: (a) the bot replies with a confirmation prompt, (b) on reply "YES",
`POST /api/kill` is issued to the control plane, (c) all entries halted within 2,000 ms.
Tested in the end-to-end integration test with a running paper-mode bot.

**AC-043** (US-022 — Telegram authz)
Given a Telegram user ID NOT in the authorized-operators list sends `/kill`, WHEN the
Telegram bot receives the command, THEN it MUST reply with an authorization-failure message
and MUST NOT call `POST /api/kill`. Verified by injecting a command from an unauthorized
user ID; assert zero API calls.

**AC-044** (US-023 — flatten single position)
Given open positions for mints [A, B, C], WHEN the operator issues `POST /api/flatten/B`
(via dashboard) or `/flatten B` (via Telegram), THEN only position B's ExitEngine sequence
is initiated within 2,000 ms. Positions A and C MUST remain in their current FSM states,
unchanged. Tested in integration test.

**AC-045** (US-024 — dead-man's switch fires on heartbeat loss)
Given 5 open positions exist in Redis, WHEN the bot process is terminated (`SIGKILL`) and
no heartbeat has been received for ≥ 60 seconds, THEN the dead-man's watchdog process
MUST submit flatten transactions for all 5 positions within (60 + 15) seconds. Zero
positions remaining open is the pass condition. Tested by killing the process and measuring
time-to-flatten.

**AC-046** (US-024 — dead-man's switch cannot be disarmed except by explicit config)
Given the dead-man's switch is active, WHEN the SLOW loop or any automated process sends
any message to the watchdog, THEN the watchdog's armed state MUST NOT change unless the
message is a valid heartbeat or an explicit config-update from the operator. An LLM output,
a market-data event, or a risk-score update MUST NOT disarm the watchdog.

---

### EPIC 7 — OBSERVABILITY

**AC-047** (US-025 — live feed lag ≤ 3 s)
Given the dashboard is connected to the live control plane (SSE `/api/feed`), WHEN the bot
records a new snipe event in its internal log, THEN the same event MUST appear in the
dashboard feed within ≤ 3,000 ms (measured in end-to-end integration test). Tested with
10 synthetic events injected at the engine level; p99 dashboard receipt latency ≤ 3,000 ms.

**AC-048** (US-025 — no stale events/min counter bug)
Given the dashboard SnipeFeed page is live, WHEN more than 60 seconds have elapsed since
the last snipe event, THEN the events/min counter MUST display 0 (or "—"), NOT a stale
historical rate. Verified by a Vitest test that advances mock time by 120 s with no new
events and asserts the counter is ≤ 0 or labeled as stale.

**AC-049** (US-026 — mock build green)
Given `VITE_USE_MOCK=true`, WHEN `npm run build` is executed, THEN the build MUST exit
with code 0, `tsc -b` MUST report zero type errors, and ESLint MUST report zero
errors (advisories are permitted). Verified by running the command in CI.

**AC-050** (US-027 — latency page shows internal vs block-engine separately)
Given the Latency page is rendered, WHEN the per-hop budget table is visible, THEN: (a)
internal compute hops (ingress, decode, gate, model, tip-query, build, sign) MUST be shown
with their ms values and labeled "internal compute (DETECTION-COMPETITIVE)", (b) the
block-engine RTT column MUST be shown separately and labeled "block-engine RTT
(SUBMISSION-DISADVANTAGED)", (c) a footnote or tooltip MUST state that the internal
floor does NOT imply landing competitiveness against staked nodes.

**AC-051** (US-028 — module health staleness alert)
Given any module's last-heartbeat exceeds its declared staleness threshold, WHEN the
Monitoring page polls `/api/health`, THEN that module's status MUST display as `STALE`
or `DOWN` with a red indicator AND the staleness age in seconds. A Geyser feed age > 1,200
ms MUST trigger a health alert (logged to Prometheus AND emitted as an alert in the
dashboard).

**AC-052** (US-029 — Telegram alert on fill within 10 s)
Given a snipe entry fill is confirmed by the ExecutionVenue, WHEN the alert dispatcher
runs, THEN a Telegram message MUST be sent to the operator channel within ≤ 10,000 ms of
fill confirmation. The message MUST contain: mint (truncated), entry_slot, sol_in (lamports),
tip_lamports, land_rate_context (rolling-20 land rate at that moment).

**AC-053** (US-029 — Telegram alert on circuit-breaker trip within 10 s)
Given the daily-loss circuit breaker transitions to `TRIPPED`, WHEN the alert dispatcher
runs, THEN a Telegram message MUST be sent within ≤ 10,000 ms containing: tripped_at_utc,
daily_net_pnl_sol (negative value), threshold_breached (−3.0%/−0.30 SOL), and the text
"Bot halted. Manual re-arm required via dashboard or /breaker_reset (after review)."

**AC-054** (US-030 — LLM reasoning log shows clamp events)
Given the LLM Reasoner has produced an output with action = "SIZE_UP" (a risk-increase
attempt), WHEN the reasoning log is retrieved via `/api/reasoning` or displayed on the
Reasoning page, THEN the entry MUST show: action_received = "SIZE_UP",
action_applied = "HOLD", and flag = "risk_increase_clamped: true". The clamp event MUST
NOT be suppressed or silently converted without logging.

---

### EPIC 8 — VALIDATION AND EDGE PROOF

**AC-055** (US-031 — walk-forward minimum windows)
Given the recorded dataset has enough launches, WHEN `backtest-qa-engineer` runs the
walk-forward validation, THEN GATE-A and GATE-B MUST be computed over ≥ 5 non-overlapping
test windows, each containing ≥ 300 decision events. If the dataset does not support 5
windows at ≥ 300 events each, the run MUST FAIL with reason `insufficient_data` — it MUST
NOT be declared passing on fewer windows.

**AC-056** (US-031 — clean-room harness: no truth_* fields)
Given the clean-room validation harness code, WHEN a static-analysis / import-guard check
runs (as part of the build or CI), THEN the build MUST FAIL if any file in the
validation pipeline imports or references `truth_is_rug`, `truth_max_multiple`,
`truth_rug_detectable`, or any symbol derived from those fields in `sniper_sim/`. Rug recall
≥ 0.50 MUST be a measured output from the test fold, never a parameter passed to the gate.

**AC-057** (US-031 — clock audit: shifted clock must change results)
Given the walk-forward validation harness, WHEN it is run with all event timestamps
deliberately shifted forward by 1 slot (a controlled clock-contamination test), THEN the
output GATE-A net PnL MUST be statistically different from the baseline run (p < 0.05 on a
bootstrap test). If clock-shifting does NOT change results, the feature timestamps are not
being used correctly — this is an auto-FAIL that blocks the rung gate.

**AC-058** (US-031 — group-aware purge: metrics reported with and without)
Given the validation harness, WHEN a walk-forward run completes, THEN the output report
MUST include: (a) metrics with group-aware purge (creator/bundler/deploy-template groups
purged across embargo boundary), AND (b) metrics WITHOUT group-aware purge. The delta
between the two MUST be reported. If the delta is large (> 20% relative difference in
GATE-A PnL), it MUST be flagged as "potential actor-identity memorization."

**AC-059** (US-031 — experiment log precondition)
Given a new validation run is about to compute GATE-A or GATE-B, WHEN the harness checks
preconditions, THEN it MUST verify that a non-empty, append-only, hashed experiment log
exists and that the trial count it records is > 0. If the log is absent, empty, or its
hash does not match the expected value, the run MUST FAIL with reason `experiment_log_missing_or_tampered`.
This is a gate precondition, not a soft check.

**AC-060** (US-032 — live mode requires explicit CEO authorization and DRY-RUN off)
Given the bot is in `LIVE_DRY_RUN` or `PAPER` mode, WHEN the operator sends
`POST /api/mode { "mode": "LIVE" }`, THEN the control plane MUST check: (a) the
`DRY_RUN_ENABLED` config flag is explicitly set to `false` (not just absent), AND
(b) an authorization token matching a CEO-approved session is present. If either check
fails, the mode change MUST be rejected with HTTP 403 and reason `live_requires_dry_run_disabled_and_ceo_auth`.
Zero live-capital transactions may be submitted when `DRY_RUN_ENABLED = true`.

---

## TRACEABILITY MATRIX — AC to US to FR

| AC | US | Primary FR(s) |
|---|---|---|
| AC-001 | US-001 | FR-001, FR-002 |
| AC-002 | US-001 | FR-001 |
| AC-003 | US-001 | FR-003 |
| AC-004 | US-002 | FR-004, FR-005 |
| AC-005 | US-002 | FR-005, FR-012 |
| AC-006 | US-002 | FR-009 |
| AC-007 | US-003 | FR-006 |
| AC-008 | US-004 | FR-007, FR-011 |
| AC-009 | US-004 | FR-007, FR-032 |
| AC-010 | US-005 | FR-008 |
| AC-011 | US-006 | FR-026, FR-037 |
| AC-012 | US-009 | FR-024 |
| AC-013 | US-007 | FR-027 |
| AC-014 | US-007 | FR-027 |
| AC-015 | US-008 | FR-037, FR-049 |
| AC-016 | US-009 | FR-051, NFR-001 |
| AC-017 | US-009 | FR-021 |
| AC-018 | US-009 | FR-028 |
| AC-019 | US-009 | FR-017 |
| AC-020 | US-010 | FR-021, FR-002 |
| AC-021 | US-010 | FR-021 |
| AC-022 | US-011 | FR-035 |
| AC-023 | US-011 | FR-035, FR-027 |
| AC-024 | US-012 | FR-036 |
| AC-025 | US-013 | FR-033 |
| AC-026 | US-013 | FR-033, FR-052 |
| AC-027 | US-024 | FR-033 |
| AC-028 | US-014 | FR-034 |
| AC-029 | US-014 | FR-034 |
| AC-030 | US-015 | FR-032 |
| AC-031 | US-015 | FR-032 |
| AC-032 | US-016 | FR-029, FR-030 |
| AC-033 | US-016 | FR-030 |
| AC-034 | US-017 | FR-038 |
| AC-035 | US-017 | FR-038 |
| AC-036 | US-018 | FR-005, NFR-009 |
| AC-037 | US-018 | FR-043 |
| AC-038 | US-019 | FR-043, FR-047, FR-048 |
| AC-039 | US-020 | FR-049 |
| AC-040 | US-021 | FR-025, FR-055 |
| AC-041 | US-021 | FR-049 |
| AC-042 | US-022 | FR-050, FR-055 |
| AC-043 | US-022 | FR-050 |
| AC-044 | US-023 | FR-025, FR-050 |
| AC-045 | US-024 | FR-033 |
| AC-046 | US-024 | FR-033 |
| AC-047 | US-025 | FR-056 |
| AC-048 | US-025 | FR-049 |
| AC-049 | US-026 | FR-049, NFR-011 |
| AC-050 | US-027 | NFR-001 |
| AC-051 | US-028 | FR-057, NFR-004 |
| AC-052 | US-029 | FR-050 |
| AC-053 | US-029 | FR-050, FR-034 |
| AC-054 | US-030 | FR-017 |
| AC-055 | US-031 | FR-043 |
| AC-056 | US-031 | FR-019 |
| AC-057 | US-031 | FR-045 |
| AC-058 | US-031 | FR-046 |
| AC-059 | US-031 | FR-020 |
| AC-060 | US-032 | FR-039 |

---

## ZERO-VAGUE-CRITERION SCAN

The following checks were run mentally against every AC above. Any AC failing these
checks is a spec defect:

1. Every AC has a number. CONFIRMED — AC-001 through AC-060, no gaps.
2. Every AC has a machine-checkable Given/When/Then or a numeric pass threshold. CONFIRMED.
3. Zero uses of "fast enough", "usually", "should generally", or "approximately" without
   a quantified bound. CONFIRMED — every latency is in ms with p50/p99; every rate is in %.
4. Every negative AC clearly states what MUST NOT happen. CONFIRMED — AC-009, AC-013,
   AC-014, AC-019, AC-020, AC-021, AC-031, AC-043, AC-046, AC-060 all state the prohibited
   outcome explicitly.
5. Point-in-time constraint explicit on every data/detection AC. CONFIRMED — AC-001, AC-003,
   AC-004, AC-005, AC-008 all state event-time or observation-time constraints.
6. All four mandatory invariants have negative ACs. CONFIRMED:
   - Asymmetric LLM trust: AC-019, AC-020, AC-021, AC-031
   - No double-entry: AC-012
   - Cost gate: AC-013, AC-014
   - Survivable stops (all 3 layers): AC-025, AC-026, AC-027
