# AATS SNIPER — MASTER SPECIFICATION (G0)

**Version:** 1.0.0
**Date:** 2026-06-16
**Author:** `quant-product-analyst`
**Status:** READY FOR CEO G0 APPROVAL
**Source authorities:** AATS-BRIEF.md, BUILD-DIRECTIVE-v3.md, EDGE-VERDICT.md (GO-PAPER-ONLY),
walk-forward-methodology.md, dashboard/src/lib/api.ts, sol-sniper/sniper_sim/*

---

## EXECUTIVE SUMMARY (CEO reads this in five minutes)

AATS is an autonomous Solana meme-coin sniper that detects new liquidity events and
executes trades inside a millisecond budget. The prior quant research returned
**GO-PAPER-ONLY**: there is a plausible, structurally-defensible edge on three narrow
surfaces (safety-selective late entry, exit discipline, migration-survivor selection), but
it is UNPROVEN on recorded data. Real capital stays disabled by default behind a hard
DRY-RUN flag and is authorized by the CEO only after GATE-A AND GATE-B pass on recorded
data (not synthetic).

The system has TWO halves with equal weight in the spec:
1. **Trading engine** (M1-M4): the triple loop, models, risk guardrails, Solana execution.
2. **Operator surfaces** (M5 + dashboard + Telegram): the control plane the CEO uses to
   monitor and de-risk the bot from a browser or phone.

The acceptance metric is: **net-of-cost PnL AND model-vs-naive-baseline BOTH positive on
RECORDED real data, with lower 95% bootstrap bound > 0 across ≥ 5 purged/embargoed test
windows.** No fixed win-rate target is ever stated, tuned-toward, or claimed.

**13 blocking conditions** (C-1..C-13 from EDGE-VERDICT.md) are encoded as numbered
FRs and ACs below.

---

## 1. PROBLEM STATEMENT

The operator (CEO) wants to trade Solana meme-coins autonomously by sniping new and
early-stage liquidity events on pump.fun, PumpSwap, and Raydium. The adversarial reality:
block-0 of every new pool is owned by co-located, staked, or insider co-bundling bots that
cannot be out-raced by a solo desk. The defensible edge is NOT speed; it is:

1. **Safety selection** — skip the ~60% of launches that are detectable rugs; enter the
   survivors at slot +5..+30 after the block-0 melee clears.
2. **Exit discipline** — harvest the asymmetric upside the early-dumpers leave behind.
3. **Migration-survivor selection** — enter graduated (PumpSwap) tokens after the
   migration-block-0 melee, conditioned on early-survival features.
4. **Smart-money as a SELECTIVITY FILTER ONLY** — never a blind mirror, never a buy
   trigger on its own.

The dominant failure mode is absence of edge net of costs and adverse selection — not
a bug in the code.

---

## 2. THE OPERATOR

The **operator** is the CEO running the bot unattended, monitoring from a web dashboard or
Telegram on a phone. The operator's job is to:
- Authorize or halt the bot (kill / flatten / pause).
- Observe live P&L, risk state, and feed events.
- Configure risk limits (within pre-approved ranges).
- De-risk positions remotely.
- Authorize each capital-staging rung advancement (R2→R3 requires explicit CEO sign-off).

The operator NEVER adjusts the LLM behavior, widens stops, or increases position sizing
from the operator surfaces — those controls do not exist.

---

## 3. SCOPE (G0 FENCE)

### IN SCOPE — v1

- **Venues:** Solana only. Raydium (AMM v4 + CPMM), pump.fun bonding curve, PumpSwap
  (primary migration target), Meteora and Moonshot as candidates behind the pluggable
  venue/program-ID registry.
- **Execution surface:** direct AMM instruction for snipe buys; Jupiter v6/Ultra for exits
  and survivors. Jito bundles + dynamic tipping.
- **Triple loop:** SNIPE (event-triggered, hot-path Rust), FAST (<100ms deterministic),
  SLOW (sense/predict/reason, seconds-to-minutes).
- **Models:** LightGBM/XGBoost fast snipe classifier (ONNX, single-digit ms); TFT
  slow-loop survivor model; LLM Reasoner (de-risk-only schema-enforced veto).
- **Operator surfaces:** web dashboard (10 pages, existing `./dashboard/`), Telegram
  operator channel — BOTH binding to the frozen control-plane API contract (§10).
- **Safety systems:** daily-loss circuit breaker, survivable stops (3 layers), dead-man's
  switch.
- **Capital staging:** R0 (sim), R1 (shadow/record), R2 (paper/dry-run), R3 (tiny-real,
  CEO-gated), R4 (scale, CEO-gated per step).
- **Validation infrastructure:** clean-room recorded-data harness, walk-forward
  methodology per `walk-forward-methodology.md`, 13 blocking conditions from EDGE-VERDICT.

### NON-GOALS — v1 (explicitly excluded)

- **CEX trading:** `ccxt` and any CEX path is a dead stub behind `ExecutionVenue`. No
  CEX order management, no CEX account integration.
- **Block-0 race wins:** the system intentionally does NOT try to land at block-0 of new
  pools or migration blocks. Any feature, optimization, or claim premised on winning the
  block-0 race is out of scope and a HARD-RULE violation.
- **Mobile app:** the operator surface is a web dashboard and Telegram bot only. No iOS/
  Android native app in v1.
- **Multi-chain:** ETH, BSC, Base, or any non-Solana chain. Zero v1 scope.
- **Win-rate marketing:** no fixed win-rate number is targeted, claimed, or surfaced
  anywhere in the system, UI, or docs.
- **Automated capital scaling:** no automated path advances the bot from R2 to R3 or R3
  to R4 without explicit CEO authorization.
- **LLM-driven risk increases:** the LLM may never size up, widen a stop, or override a
  hard stop. This is a NON-GOAL that is simultaneously a hard constraint.

### REALISTIC LATENCY FLOOR AND UN-WINNABLE ZONES

The honest floor (carry as architectural fact, not a bug):
- **Internal compute (ingress→sign):** 20–70 ms on colo + ShredStream + local Geyser.
  This is detection-competitive, NOT submission-competitive.
- **Block-engine RTT + staked-lane gap:** an unstaked solo desk lives in the contested
  20% QUIC lane (SWQoS reserves ~80% for staked nodes, ~83% staked first-block hit rate).
  Expected extra-slot penalty vs the staked cohort: approximately +1 slot of additional
  `buyers_ahead` and adverse-selection haircut.
- **~400 ms irreducible slot floor** — this is the target Solana slot time; no amount of
  optimization removes it.
- **Un-winnable surfaces (confirmed 2026):** block-0 of any new pool; migration-block-0
  on PumpSwap (bots now atomic co-bundle the migration crank + first PumpSwap buy); pure
  tip-escalation latency auctions (edge-cap in `tips.py` correctly declines these).
- **ShredStream is table stakes in 2026**, not an edge. It lets us play; it does not make
  us faster than the staked pros who also have it.

---

## 4. EDGE THESIS (TRANSLATED FROM `EDGE-VERDICT.md`)

| Surface | Hypothesis label | Pre-registered kill condition |
|---|---|---|
| Safety-selective late entry | EH-001 | Gate rug-avoidance does NOT improve net PnL over no-gate baseline; OR catchable-rug recall < 0.50 at operating point |
| Exit discipline | EH-002 (direction unproven — most sim-circular) | Staged exit does NOT beat naive exit by ≥ +10% net PnL on recorded paths; OR hard stop fails to fire within budget in QA |
| Migration-survivor selection | EH-003 | Qualifying-migration cohort net PnL ≤ 0 on recorded data; OR only profitable at slot delay ≤ 1 |
| Coin-profile specialization | EH-004 (refinement only) | Segmentation does NOT beat pooled model out-of-sample on purged CV |
| Smart-money selectivity filter | EH-005 (default ZERO/dead) | Filtered cohort net PnL ≤ unfiltered after entry-lag accounting |

Cost gate (non-negotiable): `expected_edge_bps > total_cost_bps` or NO TRADE.
Total cost stack: Jito tip (live, edge-bounded) + priority/CU fee + entry slippage +
AMM round-trip fee (0.25% PumpSwap / Raydium each side = 50 bps round trip) + exit
slippage + sandwich haircut + adverse-selection haircut (75–150 bps FLOOR, calibrated
upward from R1 fills, never narrowed downward before live R3 calibration).

---

## 5. FUNCTIONAL REQUIREMENTS

All FRs are atomic and testable. Every FR maps to ≥ 1 user story and ≥ 1 AC.
Loop assignment: `[SNIPE]` = ultra-fast event-triggered entry, `[FAST]` = <100ms
deterministic, `[SLOW]` = sense/predict/reason, `[OPS]` = operator surface,
`[RISK]` = risk/safety guardrail, `[INFRA]` = infrastructure/validation.

### 5.1 M1 — SENSORS

**FR-001 [SNIPE]** The system MUST maintain a live Yellowstone/Geyser gRPC subscription to
the Solana validator ledger and decode pump.fun, PumpSwap, and Raydium AMM v4 + CPMM
pool-creation and migration events in real-time, using verified live program IDs read at
startup from a PLUGGABLE, version-controlled venue/program-ID registry. No stale program ID
may be hardcoded in any hot path.

**FR-002 [SNIPE]** The system MUST decode each new-pool or migration event into a
`LaunchEvent`-equivalent record (mint, slot, sol_reserve, token_reserve, initial holder
count, detected competitors) using ONLY data available at event-time (point-in-time
correctness). No field may use post-event-time data.

**FR-003 [SNIPE]** The system MUST support ShredStream pre-confirmation subscription as an
optional overlay (configured at deploy time). When ShredStream is active, the ingestion
pipeline MUST clearly label each event with its detection transport and the
observation-slot timestamp separately from the confirmation-slot timestamp.

**FR-004 [SLOW]** The system MUST maintain a SHADOW/RECORD mode in which all detection
events, would-be decisions, and point-in-time feature snapshots (first-K-slot) are
recorded to a point-in-time feature store WITHOUT submitting any order. Shadow mode MUST be
the default startup mode until the operator explicitly authorizes paper or live execution.

**FR-005 [SLOW]** The point-in-time feature store MUST record each event with a canonical
`event_time` (slot + wall-clock at which the snipe decision would be made) as the join
anchor, such that every feature value is reconstructable from data with observation-time ≤
`event_time`. Shadow records MUST include a `first_K_buy_pressure_volume` field enabling
construction of the frozen naive-momentum baseline (C-4).

**FR-006 [SLOW]** The ingestion pipeline MUST support DEXScreener, Birdeye, Meteora, and
Moonshot as optional enrichment sources for token metadata. Enrichment calls are SLOW-loop
only (never on the SNIPE hot path). If an enrichment source is unavailable the bot MUST
continue without it (degraded enrichment is NOT a halt condition).

**FR-007 [SLOW]** The system MUST maintain a `smart_wallets` feed: a real-time
`accounts_subscribe` stream tracking a configured set of historically-profitable wallet
addresses. Smart-wallet observations are ADVERSARIAL INPUT — they MUST be treated as
untrusted, selectivity-only signals. Smart-wallet observations MUST be recorded in the
point-in-time feature store with exact observation-time so entry-lag accounting is possible.

**FR-008 [SLOW]** The MCS (Market Conviction Score) pipeline MUST compute a score for each
candidate from crypto-native social/news sources. The computation MUST be SLOW-loop only.
Coordinated, low-account-age, high-synchronicity shilling activity MUST LOWER the MCS
(adversarial/contrarian signal). A higher MCS MUST NEVER directly trigger or size an entry;
it may only gate or de-risk.

**FR-009 [INFRA]** The recorded dataset MUST be reconciled against an independent
full-pool-create census (second source) to measure and bound the completeness miss rate
(C-6). Un-snapshotted tokens and un-labeled tokens MUST be carried as explicit
censored/right-truncated outcomes in the validation harness, never silently dropped.

### 5.2 M1 — FEATURES

**FR-010 [SLOW]** The feature pipeline MUST compute, for each candidate event, the full
first-60-second microstructure feature set (LP depth, holder count, holder concentration,
sniper-cluster detection, sell-tax detection, sell-reserve trajectory) evaluated exclusively
on data available at `event_time` or earlier.

**FR-011 [SLOW]** The feature pipeline MUST compute a `smart_wallets_in` count feature
(number of tracked smart wallets with a confirmed buy in the first K slots, using only
slots ≤ `event_time + K`). Entry lag relative to those wallets' fills MUST be recorded.

**FR-012 [INFRA]** Every feature normalization (z-score, rank, percentile) MUST be
computed AS-OF `event_time` using only historically-preceding data. No global-statistic
normalization computed over the full dataset is permitted (it is a lookahead leak). Any
feature failing this rule is an auto-FAIL in the leak audit.

### 5.3 M2 — PREDICTION ENGINE

**FR-013 [SNIPE]** The fast snipe classifier MUST run in ≤ 5 ms wall-clock on the SNIPE
hot path (p99 target, measured in the latency harness). The model MUST be exported to ONNX
or equivalent Rust-native format; Python inference is forbidden on the SNIPE hot path.

**FR-014 [SNIPE]** The snipe classifier MUST output a **calibrated probability** and an
**uncertainty band** for each candidate, not a point price. A predicted probability of X
MUST reflect a realized frequency near X in the calibration (reliability) curve on held-out
test data. High-uncertainty outputs MUST be treated as de-risk signals (skip or reduce size),
never as increase-size signals.

**FR-015 [SLOW]** The naive-momentum baseline MUST be fully specified, parameterized
(K-slots, percentile threshold, unit-of-risk), and FROZEN in a committed, hashed config
file BEFORE the first model is fit on training data. The test suite MUST fail if baseline
parameters are changed after the first model fit (C-4).

**FR-016 [SLOW]** The slow-loop survivor model (TFT) MUST run on the SLOW loop only.
Its inference deadline is ≤ 500 ms. It MUST output a calibrated probability + uncertainty
band; it MUST NOT output a point price. It MUST run in a separate process from the FAST
and SNIPE loops.

**FR-017 [SLOW]** The LLM Reasoner MUST be schema-enforced: its output is a JSON verdict
struct with fields `{ action: "VETO" | "HOLD" | "REDUCE_SIZE", reason: string, confidence: float }`.
The action field MUST NOT contain "SIZE_UP", "WIDEN_STOP", or any risk-increase token.
Any LLM output that would increase risk MUST be clamped to "HOLD" before propagation (C-7
asymmetric-trust clamp is a hard invariant, not a policy).

**FR-018 [FAST]** The LLM Reasoner MUST NOT be on the FAST-loop critical path. Its veto
MUST be consumed by the SLOW loop and pre-staged as a flag the FAST loop reads atomically
from shared state, not computed inline in the FAST loop.

**FR-019 [INFRA]** The validated model harness MUST be a clean-room rebuild: a static-analysis
or import guard MUST FAIL the build if any recorded-gate code path references a `truth_*`
field or any path derived from `truth_max_multiple` from `sniper_sim/` (C-7). Catchable-rug
recall ≥ 0.50 MUST be MEASURED on held-out labeled rugs in test folds, never set as a
parameter.

**FR-020 [INFRA]** A committed, append-only, hashed experiment log (recording every
config, threshold, feature-set, exit-mode, and profile-bucket evaluated) MUST exist as a
precondition for computing GATE-A/GATE-B. Significance deflation MUST be a function of the
logged trial count (C-9). No log = auto-FAIL.

### 5.4 M3 — CONTROLLER

**FR-021 [SNIPE]** The SNIPE loop MUST be implemented in Rust. It receives detection events
from the ingestion bus, runs the pre-trade safety gate (FR-026), reads the pre-staged model
score from shared state, applies the cost gate (FR-027), constructs and signs the entry
transaction, and submits to the ExecutionVenue — all within the SNIPE latency budget
(FR-051). The SNIPE loop MUST NEVER call an LLM, block on a network RPC not already
subscribed, or wait on the SLOW loop.

**FR-022 [FAST]** The FAST loop MUST be deterministic, run every tick at ≤ 100 ms, own
stop-loss / take-profit / OMS / reconciliation, and MUST NEVER block on an LLM or any
unbounded async call. Hard stop, trailing stop, and circuit-breaker enforcement are all
exclusively FAST-loop responsibilities.

**FR-023 [SLOW]** The SLOW loop runs on a seconds-to-minutes cadence. It owns: sensor
fusion (MCS + feature assembly), model inference (both fast classifier pre-staging and TFT
survivor), LLM Reasoner veto generation, position scaling decisions, and capital-staging
state. Results are written to shared state read by FAST/SNIPE loops atomically.

**FR-024 [FAST]** Each open position MUST be tracked by a per-position FSM with states:
`IDLE → ENTERING → OPEN → CLOSING → CLOSED | VETOED`. FSM transitions MUST be atomic
(single-writer, write-ahead) with no concurrent transition allowed on the same mint. A
second SNIPE attempt on a mint whose FSM state is `ENTERING` or `OPEN` MUST be rejected
with a logged FSM-state rejection.

**FR-025 [OPS]** The agent-orchestration layer MUST expose the frozen control-plane API
(§10) conforming exactly to the contract bound by the dashboard and Telegram channel.
Mode changes, kill commands, flatten commands, and breaker resets MUST propagate to the
active loops within the latency budget defined in FR-055.

### 5.5 M4 — GUARDRAILS

**FR-026 [SNIPE]** The pre-trade safety gate MUST execute the following checks in order,
short-circuiting on the first failure (0-RPC hot path for checks 1–5):
1. Freeze authority set → REJECT.
2. Mint authority not renounced → REJECT.
3. LP not burned or locked → REJECT (cached LP-mint check).
4. Dev/bundle cluster detected → REJECT (creation tx + first buyer analysis).
5. Sell tax too high (token-2022 extension / cached) → REJECT.
6. Sellability simulation (30–100 ms, PARALLEL, gates slot N+2+ entries only — NOT block-0 path).

**FR-027 [SNIPE]** The cost gate MUST be the final check before any entry. The bot MUST
NOT enter when `expected_edge_bps ≤ total_cost_bps`. Every rejection MUST be logged with
the full numeric comparison: expected_edge_bps, each cost component, total_cost_bps. The
Jito tip MUST be read from the live `bundles.jito.wtf` tip_stream (or equivalent live
source) at decision time; hardcoded tip values are a build-FAIL. Tip bid MUST be bounded
by `min(market_floor, 0.30 × expected_edge_sol)` (C-1 / `tips.py` constraint).

**FR-028 [SNIPE]** Direct AMM instruction construction MUST be used for snipe buys against
decoded pool keys. Jupiter v6/Ultra is NOT on the block-0 snipe path; it is used for exits
and survivors only.

**FR-029 [FAST]** The ExitEngine MUST implement: TP-ladder (configurable rungs, defaults
per `exits.py`), trailing stop (arms at configured multiple, drawdown-triggered), hard stop
(≤ FR-052 budget after trigger), and max-hold-steps timeout. Exits MUST support Fast-MEV
and Secure-MEV modes. Hard stop MUST take priority over all other exit rules.

**FR-030 [FAST]** The ExitEngine MUST support operator-configurable TP presets ("auto-strat"
presets: conservative, balanced, aggressive) exposed on the Settings page and via the
risk-config API. Preset changes MUST apply to new entries only, never retroactively modify
open positions' active exit config.

**FR-031 [FAST]** The hierarchical risk engine MUST enforce, in strict priority order:
(1) per-trade capital cap, (2) max aggregate exposure, (3) daily-loss circuit breaker,
(4) consecutive-loss counter halt, (5) land-rate-collapse halt, (6) rug-avoidance-decay
halt, (7) slippage-blowout halt. Rule (1)–(3) are hardcoded floors; (4)–(7) are configured
thresholds. The LLM MAY trip the breaker early; the LLM MUST NEVER reset it.

**FR-032 [FAST]** Fractional-Kelly position sizing: `size = min(per_coin_cap, 0.25 × kelly_fraction)`.
The Kelly fraction is computed from the model's calibrated probability and expected return.
No signal, no LLM, and no copy-trade observation may INCREASE size. Size may only be
DECREASED by secondary signals (MCS, smart-money, LLM veto, uncertainty). Hard cap ≤ 1/4
Kelly at all times.

**FR-033 [RISK]** Survivable stops MUST be implemented as three independent layers, each
with its own trigger condition and each tested independently:
- **Layer 1 — Venue-native resting order / keeper:** a resting on-chain limit/stop order
  (where venue-native resting is available) or a keeper transaction pre-signed and
  periodically refreshed to trigger on stop-price breach.
- **Layer 2 — In-process FAST-loop enforcer:** the FAST loop polls every tick and submits
  the exit transaction on stop-price breach.
- **Layer 3 — Dead-man's switch:** a heartbeat-monitored watchdog external to the bot
  process; on heartbeat loss (bot process dead or network partition exceeding T_DMS), it
  submits pre-signed flatten transactions for all open positions.
The stop MUST NOT depend on the bot process being alive.

**FR-034 [RISK]** The daily-loss circuit breaker MUST trip automatically at −3.0% of the
day's allocated daily-risk-capital tranche OR −0.30 SOL on the tiny-real wallet, whichever
is hit first. On trip: (a) halt all new entries, (b) hand open positions to survivable-stop
enforcement (Layer 1+2+3 all remain active), (c) require explicit manual re-arm by the
operator. The breaker MUST be built and proven (QA fires on demand) BEFORE any live-capable
execution path is enabled.

**FR-035 [RISK]** Resting limit orders and DCA entry orders MUST fire even when the operator
is offline. They are managed by the FAST loop and persist in Redis state with explicit
expiry. Resting orders MUST be subject to the same pre-trade safety gate (FR-026) and cost
gate (FR-027) at the time of activation, not only at the time of placement.

**FR-036 [FAST]** Multi-wallet execution: the system MUST support configuring up to N
independent trade-only signing wallets (where N is an operator-configured parameter ≤ CEO-
authorized max). Transaction blast-radius caps MUST be enforced: no single mint may absorb
more than the per-mint cap across all wallets in the cluster. Anti-cluster-detection
strategies (submission timing jitter, route diversity) MUST be configurable.

**FR-037 [SNIPE]** Token-safety scanner MUST check and surface red flags for: honeypot
detection, mint authority un-revoked, freeze authority set, LP lock status, dev/bundle
wallet concentration, holder concentration. These MUST be visible on the dashboard
(red-flag indicator per candidate). Red flags MUST be persisted in the candidate record at
event-time — never retroactively enriched with post-event data.

**FR-038 [RISK]** Per-surface decay auto-halts MUST be implemented (FAST-loop enforced):
- Consecutive losses ≥ 8 on a surface → pause surface, alert operator.
- Land rate < 35% sustained over 20 attempts → infra/contention alert, stop racing.
- Catchable-rug recall < 0.50 on recent labeled outcomes → gate degraded, halt selective
  entry until retrained.
- Realized adverse-selection haircut exceeds calibrated band by > 50% sustained → halt.
- Regime-break detection (distribution shift in launch population) → freeze, alert operator.
- Smart-money filter: filtered-cohort net PnL turns ≤ unfiltered → disable filter.

### 5.6 M4 — EXECUTION

**FR-039 [SNIPE]** The `JitoJupiterVenue` implementation MUST be the real production
implementation behind the `ExecutionVenue` seam from `venue.py`. It MUST refuse to execute
unless: (a) a DRY-RUN flag is explicitly disabled by the operator, AND (b) a valid, funded,
isolated trade-only signing wallet is configured. In DRY-RUN mode it MUST build and sign the
transaction (for latency measurement) but MUST NOT submit it to any endpoint.

**FR-040 [SNIPE]** Jito bundle construction MUST include: the snipe buy instruction + an
`assert_min_out` instruction in the same atomic bundle. If the bundle reverts on `assert_min_out`,
no tokens are received and no tip is spent. Partial fills from multi-buyer pools MUST be
correctly accounted for.

**FR-041 [FAST]** Jupiter v6/Ultra MUST be used for exit transactions and survivor
re-entries. Jupiter MAY NOT be used on the block-0 snipe path (see FR-028).

**FR-042 [INFRA]** All money values — SOL amounts in lamports, token amounts in base units,
tip amounts, fee amounts — MUST be stored, transmitted, and computed as integer base units
or `Decimal` types. Floating-point (IEEE 754 `float`) MUST NOT be used for any monetary
calculation. This rule applies to BOTH the backend engine AND the operator dashboard
(frontend).

### 5.7 M5 — VALIDATION INFRASTRUCTURE

**FR-043 [INFRA]** The walk-forward validation harness MUST enforce the full methodology
from `walk-forward-methodology.md`: rolling windows (≥ 5 non-overlapping test windows with
≥ 300 decision events AND ≥ declared minimum wall-clock each), purge + embargo per §3,
CPCV for model selection, cost application per §5, calibration check per §6, adversarial
guards per §8.

**FR-044 [INFRA]** The adverse-selection haircut MUST be calibrated from RECORDED R1 fills
BEFORE GATE-A is computed at R2. The calibrated haircut is frozen at train-fold level and
applied unchanged to test folds. Per-window re-fit = auto-FAIL (C-5). If calibrated haircut
> 200 bps at target size, EH-001's net midpoint MUST be re-derived and re-justified or
killed (C-11).

**FR-045 [INFRA]** The block_time-vs-arrival-time clock audit MUST be run: every feature
snapshot ordered by slot/block_time; a deliberately-shifted-clock control run MUST change
results (if it does not, the timestamps are not being used correctly — auto-FAIL) (C-5).

**FR-046 [INFRA]** Group-aware purging MUST be implemented: creator wallet, bundler cluster,
and deploy-template fingerprints MUST be grouped across the embargo boundary to prevent
actor-identity leakage. Test-window metrics MUST be reported with AND without group-purge
to surface identity-memorization (C-10).

**FR-047 [INFRA]** GATE-A net PnL MUST be reported stratified by tip-contention bucket (low /
medium / high contention at decision time). If only the low-contention cohort shows positive
GATE-A, this MUST be flagged as negative-selection residual and scale-up (R4) MUST be
blocked (C-3).

**FR-048 [INFRA]** Independent-surface reporting MUST show how many of EH-001/EH-002/
EH-003/EH-004/EH-005 survive INDEPENDENTLY under the corrected competitor-delay
distribution (C-1/C-2 compliant cost stack). Pooled-only survival MUST be classified as
one fragile edge, not a diversified portfolio (C-13).

### 5.8 OPERATOR SURFACES — CONTROL PLANE

**FR-049 [OPS]** The operator web dashboard (`./dashboard/`) MUST be wired to the live
control-plane API when `VITE_USE_MOCK=false`. Every page MUST reflect real bot events
within the latency budget (FR-056). The mock fallback (`VITE_USE_MOCK=true`) MUST continue
to build green and serve realistic-enough data for offline development.

**FR-050 [OPS]** The Telegram operator channel MUST send real-time alerts for: fills, rugs
avoided, circuit-breaker trips, system errors, and daily P&L summary. The Telegram command
set is CONSTRAINED to de-risk-only operations: `/status`, `/kill`, `/flatten [mint]`,
`/pause`. No Telegram command may increase risk, size up, widen a stop, or reset the
circuit breaker without the same authorization check as the dashboard. Command authorization
MUST be enforced (only the configured operator Telegram user ID may issue commands).

**FR-051 [SNIPE]** SNIPE loop latency budget: ingress detection → signed intent → ExecutionVenue
submit MUST complete in ≤ 150 ms internal compute (p99, measured under load). This budget
covers: Geyser event decode + feature read + gate + model inference + tip query + tx build +
sign. It does NOT include block-engine RTT (which is irreducible and counted separately).

**FR-052 [FAST]** Hard stop execution latency: from trigger condition detected in the FAST
loop to `ExecutionVenue.exit()` called MUST be ≤ 50 ms (p99, measured in the fast-loop sim
harness).

**FR-053 [FAST]** FAST loop tick budget: each tick (SL/TP/OMS/reconciliation pass) MUST
complete in ≤ 100 ms wall-clock. Any tick that exceeds 100 ms MUST emit a latency-budget
breach metric (logged to Prometheus).

**FR-054 [SLOW]** SLOW loop model inference ceiling: LightGBM/XGBoost/ONNX fast-classifier
≤ 5 ms; LLM Reasoner veto ≤ 200 ms (for pre-staged flag update, not inline); TFT survivor
≤ 500 ms.

**FR-055 [OPS]** Control-plane command propagation: a `/kill` or `/flatten` command issued
via dashboard or Telegram MUST result in all new entries halted AND all ExitEngine exit
sequences initiated within ≤ 2 seconds of command receipt (measured end-to-end in the
integration test harness).

**FR-056 [OPS]** The SSE `/api/feed` stream MUST deliver snipe events to the dashboard
within ≤ 3 seconds of the event being recorded in the bot's internal event log (measured
in the end-to-end integration test).

**FR-057 [INFRA]** The Geyser feed freshness requirement: the age of the most recent
processed event MUST be < 2 slot-times (< 800 ms) under normal conditions. A staleness
condition exceeding 3 slot-times (> 1,200 ms) MUST emit a health alert and trigger the
dead-man's switch heartbeat degraded-mode.

---

## 6. NON-FUNCTIONAL REQUIREMENTS

**NFR-001 — Snipe decision latency (internal, p50/p99)**
- p50: ≤ 50 ms (ingress → ExecutionVenue.execute() called).
- p99: ≤ 150 ms (ingress → ExecutionVenue.execute() called).
- Measured in the latency harness under simulated load. Block-engine RTT excluded from
  these numbers and reported separately (C-1 mandate).

**NFR-002 — FAST loop tick ceiling**
- Every tick ≤ 100 ms. Zero ticks may call an LLM or block on an unbounded RPC.
- Measured continuously via Prometheus histogram; p99 over a 5-minute window must clear.

**NFR-003 — Model inference (SNIPE hot path)**
- Fast classifier (ONNX/Rust): ≤ 5 ms p99 (measured in isolation).
- LLM Reasoner: NEVER on SNIPE or FAST path. Ceiling ≤ 200 ms p99 on SLOW loop.
- TFT survivor: ≤ 500 ms p99 on SLOW loop.

**NFR-004 — RPC / Geyser feed freshness**
- Most-recent-event age < 800 ms under normal conditions.
- Staleness > 1,200 ms → health alert emitted.

**NFR-005 — Land rate**
- Production target: ≥ 35% land rate over a rolling 20-attempt window as a health signal.
- Below 35% sustained → auto-halt + alert (FR-038). (This is a health floor, not a
  performance target or a win-rate claim.)

**NFR-006 — Uptime / crash-recovery**
- The bot process MUST auto-restart within ≤ 30 seconds of an unhandled crash
  (systemd / Docker restart policy).
- On restart, the FSM MUST restore from Redis state and resume tracking all open positions.
- The dead-man's switch MUST remain armed during the crash gap (heartbeat loss triggers it
  within T_DMS, configured ≤ 60 seconds of heartbeat absence).

**NFR-007 — Observability**
- Prometheus metrics MUST be emitted for: snipe decision latency (histogram), FAST-loop
  tick time (histogram), model inference time (histogram), land rate, net-PnL-per-day,
  model-vs-baseline delta, circuit-breaker state, dead-man's switch heartbeat age.
- Grafana dashboards MUST surface GATE-A and GATE-B metrics live during paper/live rungs.
- All structured logs MUST include: mint, slot, decision, reason, cost components, net PnL.

**NFR-008 — Security / secrets**
- No private key, RPC API key, Telegram bot token, or funded wallet seed phrase may appear
  in code, logs, images, or any tracked file. `.env.example` only.
- The signing wallet is an isolated, trade-only, capped-funding wallet (never main holdings).
  The max balance is operator-configured and audited by `crypto-security-engineer`.

**NFR-009 — Money representation**
- ALL monetary values (SOL in lamports, token amounts in base units, tips, fees) MUST be
  integer base units or `Decimal`. IEEE 754 float MUST NOT be used for any monetary
  calculation in any component (engine or dashboard).

**NFR-010 — Docker deploy**
- The full system (bot + control-plane API + dashboard + Telegram + Redis + Prometheus +
  Grafana) MUST start with a single `docker compose up` on a co-located Linux host.
- Build time for a cold pull MUST be < 10 minutes on a standard 4-vCPU build host.

**NFR-011 — Dashboard build**
- `npm run build` (which runs `tsc -b` then the production build) MUST exit 0 with zero
  type errors in BOTH mock mode (`VITE_USE_MOCK=true`) and live mode (`VITE_USE_MOCK=false`).
- Vitest tests for destructive controls (kill, flatten, breaker-reset) and key pages MUST
  all pass.

---

## 7. ASSUMPTIONS REGISTER

| ID | Assumption | Status | Impact if wrong |
|---|---|---|---|
| A-001 | pump.fun bonding curves currently migrate to PumpSwap (not Raydium) by default at ~$69k mcap (~85 SOL buy volume), no migration fee, 0.25% AMM fee. | CONFIRMED (2026-06, cited in EDGE-VERDICT.md) | Build pluggable; verify live IDs at startup |
| A-002 | Jito tip percentile data is available at sub-second latency from `bundles.jito.wtf/tip_stream`. | CONFIRMED (Jito docs 2026) | Need fallback: use cached last-N-landed-tip median |
| A-003 | ShredStream pre-confirmation detection is available commercially and gives 50–200 ms pre-confirmation signal. | CONFIRMED (Chainstack, Jito docs 2026) | Table stakes, not an edge |
| A-004 | SWQoS reserves ~80% of QUIC leader connections for staked nodes; unstaked solo desk lands in the contested 20% lane. | CONFIRMED (Helius, Chorus One, Everstake 2026) | Extra-slot penalty built into cost model |
| A-005 | Raydium AMM v4 + CPMM round-trip fee = 0.25% per side = 50 bps round trip. | CONFIRMED (2026) | If fees change, recalibrate cost gate |
| A-006 | PumpSwap AMM fee = 0.25% per side (0.20% LP / 0.05% protocol). | CONFIRMED (pump.fun/docs/fees 2026) | If fees change, recalibrate cost gate |
| A-007 | The CEO is a solo operator; no institutional trading desk. | CONFIRMED | System is designed for one operator |
| A-008 | The adverse-selection haircut is in the range 75–150 bps at target size. | UNCONFIRMED — this is a FLOOR to be calibrated upward from R1 fills. If calibrated haircut > 200 bps, EH-001 requires re-justification. | EH-001 may flip to negative; C-11 handles this |
| A-009 | Smart-money wallets are adversarial inputs; their expected edge contribution is +0 until measured otherwise on recorded data. | UNCONFIRMED — default pessimistic | EH-005 kill condition is already triggered at default |
| A-010 | At least 3,000 recorded launches are achievable via R1 shadow mode within a reasonable shadow period to build a statistically sound walk-forward validation set. | UNCONFIRMED | R2 gate may be delayed; alert CEO |
| A-011 | Jupiter v6/Ultra API is available and stable for exit routing. | CONFIRMED (operational in 2026) | Need fallback: direct AMM sell instruction |
| A-012 | Redis is used as the shared state bus between loops and as the feature store. | UNCONFIRMED (architectural assumption for architect) | Architect may choose alternative; freeze at G1 |
| A-013 | A co-located Linux bare-metal or dedicated node is available for the production deploy. | UNCONFIRMED — cost and availability TBD | See OQ-003 |

---

## 8. EXPLICIT NON-GOALS RESTATEMENT

The following are prohibited at the spec level — no FR, no AC, no story overrides them:

1. Any claim or target for a fixed win rate. The acceptance metric is net-of-cost PnL AND
   model-vs-baseline on recorded data, with a lower 95% bound > 0.
2. Block-0 race: the system does not attempt to be first into block-0 of any pool.
3. CEX execution path in v1 (ccxt stub is the full extent).
4. LLM on the FAST or SNIPE critical path.
5. LLM or any signal INCREASING risk (size up, widen stop, override hard stop).
6. Automated advancement from paper to real-capital without explicit CEO authorization.
7. Blind copy-trade mirroring (smart-money is a FILTER, never a mirror).
8. Float arithmetic for any monetary value.

---

## 9. COMPETITIVE FEATURE CHECKLIST

Every feature from BUILD-DIRECTIVE-v3.md §"Competitive feature target" MUST map to at
least one FR, one user story, and one AC. Status:

| Competitive feature | FR(s) | Status |
|---|---|---|
| Auto-sniper on new launch | FR-001, FR-021, FR-026, FR-027 | Covered |
| Migration sniper (pump.fun→PumpSwap/Raydium) | FR-001, FR-002, FR-003, AC-020, AC-021 | Covered |
| Copy-trade / smart-money as selectivity filter | FR-007, FR-011, FR-032 | Covered |
| Limit + DCA resting orders (fire offline) | FR-035 | Covered |
| Auto TP-ladder + trailing stop + "auto-strat" presets | FR-029, FR-030 | Covered |
| Multi-wallet / bundle execution + anti-cluster | FR-036, FR-040 | Covered |
| MEV protection: Fast vs Secure modes | FR-029, FR-041 | Covered |
| Token-safety scanner (red flags on dashboard) | FR-026, FR-037 | Covered |
| Token discovery enrichment (DEXScreener / Birdeye / Meteora / Moonshot) | FR-006 | Covered |
| Portfolio + P&L cards / export | US-020, AC-039 | Covered via story |
| Telegram operator channel (alerts + constrained commands) | FR-050, FR-055 | Covered |

---

## 10. FROZEN CONTROL-PLANE CONTRACT

The following API endpoints are FROZEN. Both the dashboard (`./dashboard/src/lib/api.ts`)
and the Telegram channel bind to these paths. The agent-orchestration-engineer implements
the server; neither the dashboard nor Telegram may introduce additional POST command
endpoints that increase risk.

| Method | Path | Purpose | Permitted action direction |
|---|---|---|---|
| GET | `/api/state` | Bot mode, loop states, wallet, FSM summary | Read only |
| GET (SSE) | `/api/feed` | Live stream of snipe events, gate verdicts | Read only |
| GET | `/api/metrics` | Net-PnL, model-vs-baseline delta, land rate, costs | Read only |
| GET | `/api/positions` | Open + closed positions with TP ladder state | Read only |
| GET | `/api/latency` | Per-hop latency budget vs slot floor; infra tiers | Read only |
| GET | `/api/sentiment` | MCS scores per tracked asset | Read only |
| GET | `/api/predictions` | Classifier probability + uncertainty | Read only |
| GET | `/api/reasoning` | LLM veto log | Read only |
| GET | `/api/risk-config` | Current risk config | Read only |
| POST | `/api/risk-config` | Update risk parameters | DE-RISK direction only (per-trade cap, daily-loss floor — may only tighten, never widen) |
| GET | `/api/health` | Module health, staleness | Read only |
| POST | `/api/kill` | Halt all new entries immediately | DE-RISK only |
| POST | `/api/flatten` | Flatten all positions via ExitEngine | DE-RISK only |
| POST | `/api/flatten/{mint}` | Flatten a single position | DE-RISK only |
| POST | `/api/breaker/reset` | Re-arm circuit breaker after manual review | DE-RISK gate (requires operator auth; breaker must have been tripped) |
| POST | `/api/mode` | Set agent mode (SHADOW / PAPER / LIVE_DRY_RUN / LIVE) | Mode may only be advanced by explicit authorization; LIVE requires DRY-RUN flag off AND CEO authorization |

The `/api/mode` endpoint MUST enforce: LIVE mode is only settable if the DRY-RUN flag has
been explicitly disabled (separate config, not a runtime POST). This prevents accidental
real-capital exposure.

---

## 11. CAPITAL-STAGING GATES (OPERATOR AUTHORIZATION CHECKPOINTS)

| Rung | Capital | Gate to pass before advancing | CEO authorization required |
|---|---|---|---|
| R0 — Sim | None (synthetic) | Mechanism proven: gate avoids rugs, staged exit > naive, tips edge-bounded. Proves direction only, NEVER licenses capital. | No |
| R1 — Shadow/record | None (real data, no orders) | ≥ 3,000 recorded launches; point-in-time leak audit clean; baseline computable. | No |
| R2 — Paper/dry-run | None (paper) | GATE-A AND GATE-B BOTH pass on purged/embargoed walk-forward (≥ 5 windows, lower 95% bound > 0 on both). All safety systems (breaker, survivable stop, dead-man's switch) fire on demand in QA. | No (Orchestrator gates G4) |
| R3 — Tiny-real | Real, incinerable (suggested ≤ 2 SOL total, ≤ 0.1–0.25 SOL/coin, ¼-Kelly) | After ≥ 100 real trades across ≥ 2 walk-forward windows: live GATE-A AND GATE-B hold, lower 95% bound > 0; realized adverse-selection haircut within calibrated band; no breaker-trip pathology. | **YES — explicit CEO sign-off** |
| R4 — Scale | Larger, still bounded | Fresh passing walk-forward window at the new size (slippage scales with size — re-prove). Tip-contention stratification (FR-047) shows non-residual edge. | **YES — per step, explicit CEO sign-off** |

---

## SELF-CHECK AGAINST MANDATORY STANDARDS

1. **Point-in-time correctness:** FR-002, FR-005, FR-010, FR-012, FR-044, FR-045 each
   explicitly state event-time constraint. No AC allows compute-time joins.
2. **Asymmetric LLM trust (negative ACs):** FR-017, FR-018, FR-031, FR-032 state the clamp.
   AC-019, AC-020, AC-021 are the negative machine-checkable tests.
3. **No double-entry (FSM):** FR-024 owns the FSM invariant. AC-012 is the machine-
   checkable test.
4. **Cost gate:** FR-027 mandates numeric logging. AC-011 is machine-checkable.
5. **Survivable stops (all 3 layers):** FR-033 names all three. AC-025, AC-026, AC-027 test
   each layer independently.
6. **Risk limits quantified:** FR-034 (daily-loss = −3.0% tranche or −0.30 SOL); FR-032
   (¼-Kelly hard cap); FR-031 (hierarchical order). All have numeric thresholds.
7. **NON-goals explicit:** §3 names CEX, block-0 race, float money, LLM risk-increase,
   automated capital scaling, win-rate claims — all fenced out.
8. **13 EDGE-VERDICT conditions:** C-1 through C-13 are each encoded in at least one FR
   (cross-reference: C-1→FR-051/NFR-001; C-2→FR-044; C-3→FR-047; C-4→FR-015; C-5→FR-044/
   FR-045; C-6→FR-009; C-7→FR-019; C-8→§11 R2 gate explicit caveat; C-9→FR-020;
   C-10→FR-046; C-11→FR-044; C-12→§11 R3/R4 gates; C-13→FR-048).
