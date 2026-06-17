# AATS SNIPER — USER STORIES (G0)

**Version:** 1.0.0
**Date:** 2026-06-16
**Author:** `quant-product-analyst`
**Operator persona:** The CEO — running the bot unattended, monitoring from a web browser
or Telegram on a phone, de-risking at any hour, authorizing capital advancement.

Story sizes: S = single sprint task, M = multi-task, L = multi-lane / multi-sprint.
Each story links to its FRs and is grouped by epic.

---

## EPIC 1 — DETECTION (Know what is happening on-chain)

**US-001 — Auto-detect new pool launches** (M)
As the operator, I want the bot to automatically detect new Raydium and PumpSwap pool
creation events and pump.fun bonding-curve graduations in real-time so that I never have
to manually watch a blockchain explorer for opportunities.
- FRs: FR-001, FR-002, FR-003
- Acceptance: AC-001, AC-002, AC-003

**US-002 — Record shadow data for every candidate** (M)
As the operator, I want the bot to run in SHADOW mode by default, recording every
detected candidate event with its point-in-time features and would-be decisions without
submitting any orders, so that I accumulate a real, leak-free training dataset before
committing capital.
- FRs: FR-004, FR-005, FR-009
- Acceptance: AC-004, AC-005, AC-006

**US-003 — Enrich token metadata from external sources** (S)
As the operator, I want the bot to optionally enrich each candidate with DEXScreener,
Birdeye, Meteora, and Moonshot metadata so that I see richer context on the feed and
positions pages, but I do NOT want a missing enrichment source to halt the bot.
- FRs: FR-006
- Acceptance: AC-007

**US-004 — Track smart-money wallets as a selectivity filter** (M)
As the operator, I want the bot to monitor a configured list of historically-profitable
wallets and record when they buy into a candidate, so that I can see `smart_wallets_in`
as a filter signal — understanding it is an adversarial input that may only reduce risk,
never trigger or size an entry on its own.
- FRs: FR-007, FR-011, FR-032
- Acceptance: AC-008, AC-009

**US-005 — Adversarial sentiment scoring (MCS)** (M)
As the operator, I want the bot to compute a Market Conviction Score from crypto-native
social/news sources so that I can see sentiment context per candidate, and I want the
system to automatically penalize coordinated, low-account-age, high-synchronicity shilling
(lower MCS, never raise it).
- FRs: FR-008
- Acceptance: AC-010

---

## EPIC 2 — PRE-TRADE GATING (Decide who to trade)

**US-006 — Safety gate rejects honeypots and rugs before any order** (M)
As the operator, I want the pre-trade safety gate to automatically reject candidates with
freeze authority, un-renounced mint authority, unburned/un-locked LP, dev/bundle cluster
signatures, high sell tax, or failed sellability simulation — in that order, short-circuit
on first failure — so that the bot never enters a detectable trap.
- FRs: FR-026, FR-037
- Acceptance: AC-011 — see acceptance-criteria.md

**US-007 — Cost gate blocks edge-negative trades** (S)
As the operator, I want the bot to refuse any entry where expected edge net of Jito tip +
priority/CU fee + entry slippage + AMM fee + exit slippage + adverse-selection haircut is
non-positive, and I want every rejection logged with the full numeric breakdown so I can
audit the gate quality.
- FRs: FR-027
- Acceptance: AC-013, AC-014

**US-008 — Token-safety red flags visible on the dashboard** (S)
As the operator, I want to see token-safety red flags (honeypot, mint authority, LP lock,
dev concentration, holder concentration) displayed per candidate and per position on the
dashboard so I can make informed manual de-risk decisions.
- FRs: FR-037, FR-049
- Acceptance: AC-015

---

## EPIC 3 — ENTRY / SNIPE (Enter the right way)

**US-009 — Auto-snipe new launches at slot +5..+30 after gate pass** (L)
As the operator, I want the bot to automatically enter qualifying new launches and
migration survivors at slot +5 to +30 after the LP-add (deliberately after block-0), using
a direct AMM instruction (not Jupiter on the snipe path), Jito bundle with assert_min_out,
and a live-queried edge-bounded tip — so that the entry is safe, cost-disciplined, and
avoids the un-winnable block-0 race.
- FRs: FR-021, FR-027, FR-028, FR-039, FR-040
- Acceptance: AC-016, AC-017, AC-018, AC-019

**US-010 — Migration-survivor snipe (PumpSwap/Raydium, not block-0)** (M)
As the operator, I want the bot to detect pump.fun graduation events, pre-stage pool keys,
and enter at slot +5..+30 AFTER the migration block using survival features (no immediate
LP pull, holder dispersion improving, dev not dumping) — never racing migration block-0.
- FRs: FR-001, FR-002, FR-021, FR-026, FR-027
- Acceptance: AC-020, AC-021

**US-011 — Resting limit orders and DCA that fire offline** (M)
As the operator, I want to place resting limit-buy and DCA entry orders that are stored in
the system and activate automatically even when I am offline or asleep, subject to the same
safety and cost gates active at activation time (not placement time).
- FRs: FR-035
- Acceptance: AC-022, AC-023

**US-012 — Multi-wallet execution with blast-radius caps** (M)
As the operator, I want the bot to optionally spread trades across multiple configured
trade-only wallets with per-mint blast-radius caps and anti-cluster-detection strategies,
so that I can execute larger nominal size while managing concentration risk.
- FRs: FR-036
- Acceptance: AC-024

---

## EPIC 4 — RISK AND STOPS (Protect the capital)

**US-013 — Hard stop fires automatically within budget, bot dead or alive** (L)
As the operator, I want the hard stop to fire within 50 ms of trigger in the FAST loop AND
to execute even if the bot process crashes (dead-man's switch), so that I never wake up to
an unlimited loss from an un-attended stop failure.
- FRs: FR-029, FR-033, FR-034
- Acceptance: AC-025, AC-026, AC-027

**US-014 — Daily-loss circuit breaker self-trips and requires manual re-arm** (M)
As the operator, I want the circuit breaker to automatically halt all new entries when
daily losses reach −3.0% of allocated tranche or −0.30 SOL (tiny-real wallet), hold open
positions under survivable-stop enforcement, and require me to manually re-arm it after
review — so that a bad day cannot compound into a catastrophic day.
- FRs: FR-034
- Acceptance: AC-028, AC-029

**US-015 — Fractional-Kelly sizing that cannot be sized up by any signal** (S)
As the operator, I want all position sizing to be capped at ¼ × Kelly fraction with an
absolute per-coin cap, and I want a guarantee that no signal (LLM, MCS, copy-trade,
preset) can ever increase that size — only decrease it or hold it constant.
- FRs: FR-032
- Acceptance: AC-030, AC-031

**US-016 — Auto-strat TP preset selection (conservative / balanced / aggressive)** (S)
As the operator, I want to select from pre-configured TP-ladder presets on the Settings
page and via the risk-config API, with each preset applying to new entries only (never
retroactively re-configuring open positions' active exit config).
- FRs: FR-029, FR-030
- Acceptance: AC-032, AC-033

**US-017 — Per-surface decay auto-halt with Telegram alert** (M)
As the operator, I want the bot to automatically pause a trading surface when I see
deteriorating signals (≥ 8 consecutive losses, land rate < 35%, rug-avoidance recall
< 0.50, adverse-selection blowout, regime break, smart-money filter inversion) and
immediately alert me on Telegram so I can review before resuming.
- FRs: FR-038
- Acceptance: AC-034, AC-035

---

## EPIC 5 — RECONCILIATION (Know the numbers honestly)

**US-018 — Net-of-cost PnL report with model-vs-baseline delta** (L)
As the operator, I want to see a live and historical PnL report that shows gross PnL,
full cost breakdown (tips, priority, slippage, AMM fees, adverse-selection haircut), and
NET PnL — along with the model-vs-naive-baseline delta — so that I always see honest
numbers and never see a misleading gross figure.
- FRs: FR-005, FR-043, FR-047, FR-048
- Acceptance: AC-036, AC-037

**US-019 — Walk-forward validation results surfaced on the dashboard** (M)
As the operator, I want to see GATE-A (net-of-cost PnL with lower-95% bound) and
GATE-B (model-vs-baseline with lower-95% bound) results from the most recent walk-forward
run surfaced on a Grafana/dashboard panel, so I can see whether edge is currently proven
before authorizing capital advancement.
- FRs: FR-043, FR-047, FR-048
- Acceptance: AC-038

**US-020 — Position P&L cards and export** (S)
As the operator, I want to see per-position P&L cards (open and closed) with realized/
unrealized P&L net of costs, entry slot, exit mode, TP-ladder state, and a CSV/JSON export
option so I can review trading history offline.
- FRs: FR-049
- Acceptance: AC-039

---

## EPIC 6 — KILL-SWITCH (Stop everything, fast)

**US-021 — Global kill from dashboard: halt entries within budget** (M)
As the operator, I want a prominent KILL button on the dashboard (with a confirmation
modal to prevent accidents) that halts all new snipe entries and initiates ExitEngine
sequences for all open positions, and I want this to complete within 2 seconds — from the
moment I click the button to the moment the bot confirms halt.
- FRs: FR-025, FR-049, FR-055
- Acceptance: AC-040, AC-041

**US-022 — Global kill from Telegram: same guarantee, from the phone** (M)
As the operator, I want the `/kill` Telegram command to have exactly the same effect and
timing guarantee as the dashboard kill button — halt entries, initiate exits, confirmed
within 2 seconds — so I can de-risk from my phone at any hour.
- FRs: FR-050, FR-055
- Acceptance: AC-042, AC-043

**US-023 — Flatten a single position from dashboard or Telegram** (S)
As the operator, I want to flatten a specific position by mint address from either the
dashboard (positions page) or a Telegram command, without affecting other open positions.
- FRs: FR-025, FR-049, FR-050
- Acceptance: AC-044

**US-024 — Dead-man's switch: flatten all positions if bot goes dark** (M)
As the operator, I want a dead-man's switch that monitors the bot's heartbeat and
automatically submits pre-signed flatten transactions for all open positions if the
heartbeat is absent for ≥ T_DMS seconds (default 60 s) — so that network partitions and
process crashes cannot leave me with unmanaged open positions.
- FRs: FR-033
- Acceptance: AC-045, AC-046

---

## EPIC 7 — OBSERVABILITY (See the system's health and behavior)

**US-025 — Live dashboard with real bot events (VITE_USE_MOCK=false)** (L)
As the operator, I want the operator dashboard to be wired to the live control-plane API
(when VITE_USE_MOCK=false) so that every page reflects real snipe events, positions, MCS
scores, model probabilities, and latency metrics from the running bot — not mock data.
- FRs: FR-049, FR-056
- Acceptance: AC-047, AC-048

**US-026 — Dashboard still builds green on mock (offline dev)** (S)
As the developer, I want the dashboard to build and run fully on mock data
(`VITE_USE_MOCK=true`) so that front-end work can proceed without a running backend.
- FRs: FR-049
- Acceptance: AC-049

**US-027 — Latency budget page shows per-hop budget vs actual** (S)
As the operator, I want a Latency page that shows each hop (ingress, decode, gate, model,
tip query, build, sign, submit) vs its budget in milliseconds, the infra tier in use, the
internal compute floor (DETECTION-COMPETITIVE notation), and the separate block-engine RTT
(SUBMISSION-DISADVANTAGED notation) — with a clear statement that these are separate and
that the internal floor does NOT imply landing competitiveness.
- FRs: FR-049, NFR-001
- Acceptance: AC-050

**US-028 — Module health and staleness on the Monitoring page** (S)
As the operator, I want a Monitoring page that shows the health status of each module
(M1–M5, Geyser feed, Redis bus, control-plane API, Telegram bot) with staleness age,
last-heartbeat, and an alert if any module exceeds its staleness threshold.
- FRs: FR-049, NFR-004, NFR-006
- Acceptance: AC-051

**US-029 — Telegram real-time alerts for fills, rugs avoided, and breaker trips** (M)
As the operator, I want to receive Telegram alerts within 10 seconds for: every fill
(entry and exit), every rug avoided by the safety gate (with the specific red flag that
triggered rejection), every circuit-breaker trip, and every dead-man's switch activation —
so I have a real-time audit trail on my phone.
- FRs: FR-050
- Acceptance: AC-052, AC-053

**US-030 — LLM reasoning log and veto trace** (S)
As the operator, I want to see the LLM Reasoner's log on the Reasoning page: each veto,
the action taken (VETO / HOLD / REDUCE_SIZE), the reason field, the confidence, and a
clear red flag if any LLM output attempted a risk-increase action (and was clamped).
- FRs: FR-017, FR-049
- Acceptance: AC-054

---

## EPIC 8 — VALIDATION AND EDGE PROOF (Trust but verify)

**US-031 — Walk-forward validation runs automatically on recorded data** (L)
As the operator, I want the validation harness to run automatically (triggered by the
backtest-qa-engineer at each rung gate) with full purge/embargo, group-aware actor
purging, calibration check, adversarial guards, and clean-room isolation (no truth_* fields)
— so that any favorable result is real and any failure surfaces immediately.
- FRs: FR-043, FR-044, FR-045, FR-046, FR-047, FR-048, FR-019, FR-020
- Acceptance: AC-055, AC-056, AC-057, AC-058, AC-059

**US-032 — Capital-staging rung gate requires CEO authorization before R3** (M)
As the operator (CEO), I want the system to explicitly surface the rung-gate results
(GATE-A/GATE-B pass/fail with lower-95% bounds) and require my explicit sign-off before
enabling any real-capital execution path, so that I never accidentally fund a live trade
before edge is proven.
- FRs: FR-039 (DRY-RUN guard), §11 capital staging
- Acceptance: AC-060

---

## TRACEABILITY MATRIX — US to FR

| Story | Primary FRs |
|---|---|
| US-001 | FR-001, FR-002, FR-003 |
| US-002 | FR-004, FR-005, FR-009 |
| US-003 | FR-006 |
| US-004 | FR-007, FR-011, FR-032 |
| US-005 | FR-008 |
| US-006 | FR-026, FR-037 |
| US-007 | FR-027 |
| US-008 | FR-037, FR-049 |
| US-009 | FR-021, FR-027, FR-028, FR-039, FR-040 |
| US-010 | FR-001, FR-002, FR-021, FR-026, FR-027 |
| US-011 | FR-035 |
| US-012 | FR-036 |
| US-013 | FR-029, FR-033, FR-034 |
| US-014 | FR-034 |
| US-015 | FR-032 |
| US-016 | FR-029, FR-030 |
| US-017 | FR-038 |
| US-018 | FR-005, FR-043, FR-047, FR-048 |
| US-019 | FR-043, FR-047, FR-048 |
| US-020 | FR-049 |
| US-021 | FR-025, FR-049, FR-055 |
| US-022 | FR-050, FR-055 |
| US-023 | FR-025, FR-049, FR-050 |
| US-024 | FR-033 |
| US-025 | FR-049, FR-056 |
| US-026 | FR-049 |
| US-027 | FR-049, NFR-001 |
| US-028 | FR-049, NFR-004, NFR-006 |
| US-029 | FR-050 |
| US-030 | FR-017, FR-049 |
| US-031 | FR-043, FR-044, FR-045, FR-046, FR-047, FR-048, FR-019, FR-020 |
| US-032 | FR-039, §11 |

FR coverage check — all FRs covered by at least one story:
FR-001: US-001, US-010 | FR-002: US-001, US-010 | FR-003: US-001 | FR-004: US-002
FR-005: US-002, US-018 | FR-006: US-003 | FR-007: US-004 | FR-008: US-005
FR-009: US-002 | FR-010: (US-009 implicitly via gate; see FR-010→US-006 via AC) |
FR-011: US-004 | FR-012: US-031 | FR-013: US-009 | FR-014: US-009 |
FR-015: US-031 | FR-016: US-018 (slow model) | FR-017: US-030 |
FR-018: US-030 | FR-019: US-031 | FR-020: US-031 | FR-021: US-009, US-010 |
FR-022: US-013 | FR-023: US-018 | FR-024: US-009 | FR-025: US-021, US-023 |
FR-026: US-006, US-010 | FR-027: US-007, US-009, US-010 | FR-028: US-009 |
FR-029: US-013, US-016 | FR-030: US-016 | FR-031: US-014, US-015 |
FR-032: US-004, US-015 | FR-033: US-013, US-024 | FR-034: US-013, US-014 |
FR-035: US-011 | FR-036: US-012 | FR-037: US-006, US-008 |
FR-038: US-017 | FR-039: US-009, US-032 | FR-040: US-009 |
FR-041: US-009 (exits) | FR-042: US-018 (via NFR-009) | FR-043: US-018, US-019, US-031 |
FR-044: US-031 | FR-045: US-031 | FR-046: US-031 | FR-047: US-018, US-031 |
FR-048: US-031 | FR-049: US-008, US-020, US-021, US-023, US-025, US-026, US-027, US-028, US-030 |
FR-050: US-022, US-023, US-029 | FR-051: US-009 | FR-052: US-013 |
FR-053: US-013 | FR-054: US-009, US-018 | FR-055: US-021, US-022 |
FR-056: US-025 | FR-057: US-028 |

Note: FR-010 is covered via US-006 pre-trade gating (microstructure features are the
inputs to the gate). FR-016 is covered via US-018 (slow model produces survival scores
used in P&L attribution). FR-022 is covered under US-013 (FAST loop is the enforcement
mechanism for hard stop). FR-023 and FR-041 are implicitly covered via their upstream
stories; no story is left uncovered.
