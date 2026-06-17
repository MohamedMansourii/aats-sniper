# AATS — HONEST EDGE REPORT

**Project:** AATS Solana Meme-Coin Ultra-Sniper (PAPER build)
**Author:** `docs-delivery` · **Date:** 2026-06-17 · **Gate:** G6 (Acceptance)
**Sources (read, not asserted):** `.agency/01-specs/EDGE-VERDICT.md`,
`.agency/05-reports/qa/T-401-edge-proof.md`, `.agency/05-reports/gates/G4-PASS.md`,
`.agency/05-reports/gates/G5-PASS.md`, `aats/models/gate_a.py`, `aats/models/gate_b.py`,
`tests/validation/`, `docs/pre-live-checklist.md`.

---

## VERDICT (read this first, three lines)

1. **The edge is UNPROVEN. `edgeVerdict = UNPROVEN-NO-REAL-DATA` (GO-PAPER-ONLY).** There is a
   *plausible, structurally-defensible* edge on a narrow set of surfaces, but it has **not been
   demonstrated on recorded mainnet data**. Real capital stays disabled.
2. **This finding IS the deliverable.** The brief's honesty clause is explicit: if the edge is not
   demonstrable net of costs, the correct deliverable is *that finding* — not a bot trading live. We
   are reporting reality, and reality is "not yet proven."
3. **The acceptance harness is BUILT and proven to compute correctly. There is no recorded data to
   run it on.** Every number produced to date is synthetic and stamped `is_bootstrap_not_real`. None
   of them license one lamport of capital.

> **There is no win-rate number, target, or claim anywhere in this report, this codebase, or this
> delivery — by design.** A high win-rate is trivially manufactured (hold losers, clip winners). It
> is not evidence of edge and is explicitly forbidden as a metric, a target, and a tuning objective.

---

## 1. How edge is measured here (net-of-cost PnL + model-vs-baseline)

The system has **exactly two** acceptance metrics. Both are net of the full cost stack; neither is a
win-rate.

### GATE-A — Net-of-cost PnL > 0
`Σ(realized PnL) − Σ(tips + priority/CU fee + entry slippage + AMM fee + exit slippage/sandwich +
adverse-selection haircut) > 0`, with a **lower 95% bootstrap confidence bound strictly > 0** over the
purged/embargoed walk-forward test windows. A point estimate is not enough.

Implemented in `aats/models/gate_a.py` (`compute_gate_a() → GateAResult`). Headline =
`total_net_pnl_lamports` (exact integer aggregate, net of cost). A declined trade contributes 0
(skip-credit). Fail-closed on empty. No win-rate field. Money is integer lamports / `Decimal` — never
float.

### GATE-B — Model beats the naive-momentum baseline
The snipe classifier's selected-cohort **net-PnL-per-unit-risk** must exceed a **frozen**
naive-momentum baseline by a margin whose lower 95% bound > 0, on the same purged/embargoed windows.
*If the model cannot beat dumb momentum net of cost, there is no model.*

Implemented in `aats/models/gate_b.py`. Delta = `model_net_pnl_per_risk − baseline_net_pnl_per_risk`.
The baseline is frozen in a committed, hashed config (`baseline.frozen.json`, C-4); a test fails if its
parameters change after the first model fit. No win-rate field anywhere on `GateBResult` or
`TradeOutcome` (asserted absent).

**Both gates must pass on RECORDED mainnet data — never synthetic.** That is the whole point.

---

## 2. The cost stack every PnL is netted against

No gross number ever reaches a gate. The round-trip cost stack used by the harness
(`tests/validation/harness.py`) totals **310 bps** and is deducted from **every** trade:

| Component | bps | Note |
|---|---|---|
| AMM round-trip fee | 50 | 0.25% PumpSwap / Raydium each side (2026, confirmed) |
| Jito tip | 30 | pure cost; edge-bounded (`tips.py` caps at 0.30× expected edge); read LIVE in production, never hardcoded |
| Priority / CU fee | 5 | small but non-zero, counted |
| Entry slippage | 40 | modeled against untouched spot via co-buyers (adverse selection at entry) |
| Exit slippage / sandwich | 35 | Secure mode lowers sandwich probability at a slightly worse base price |
| **Adverse-selection haircut** | **150** | **UNCALIBRATED floor (OQ-007 / C-11).** Widen-only — `CostParams` raises `ValueError` if lowered. This is the line item that kills naive backtests; it is mandatory, not optional. To be calibrated from recorded R1 fills before GATE-A is honored. If calibrated > 200 bps, EH-001's net midpoint is re-derived or the surface killed. |

A candidate must clear roughly **150–300+ bps net** before any surface's hypothesized edge is real.

---

## 3. What is BUILT and PROVEN to compute correctly

The GATE-A / GATE-B harness is real, exercised, and correct on its controls. This was verified at G4
(`T-401-edge-proof.md`) and re-confirmed by running `tests/validation/` (22 passed) and the
consolidated suite (1842 passed / 2 skipped) for this delivery.

**The gates return the RIGHT SIGN on planted controls** (run on the bootstrap corpus
`generate_synthetic_corpus(n=4000, seed=20260616)`, `IS_BOOTSTRAP_NOT_REAL=True`, 310 bps cost stack,
2000 bootstrap resamples):

| Control cohort | GATE-A total net | GATE-A lower-95% | GATE-A | GATE-B delta | GATE-B lower-95% | GATE-B |
|---|---|---|---|---|---|---|
| **oracle (model-WINS control)** | +104.63 SOL | +0.0247 SOL/trade | **PASS** | +0.3999 | +0.3852 | **PASS** |
| **frozen naive baseline** | −55.36 SOL | −0.0158 SOL/trade | **FAIL** | (control) | (control) | — |
| **anti-oracle (model-LOSES control)** | −289.08 SOL | −0.0734 SOL/trade | **FAIL** | −0.5843 | −0.5989 | **FAIL** |

These figures are **`is_bootstrap_not_real` synthetic** — they prove the *machinery* is correct, not
that an edge exists. The oracle "model" passes only because it reads the post-hoc public label to
*simulate* skilful selection; it is a CONTROL, not a predictor, and earns no capital license.

**Correctness properties proven (right sign on the controls, leak-free, net-of-cost):**
- The model-WINS control passes both gates with a lower-95% bound strictly > 0. **Correct sign.**
- The model-LOSES control fails both gates (the dangerous stale-edge case does not slip through).
  **Correct sign.**
- A **declined trade contributes 0** (skip-credit) — proven by a 2-row fixture.
- The frozen naive-momentum baseline itself **fails** GATE-A (−55.36 SOL net of cost) — exactly the
  EH-001 premise: dumb momentum that buys hype-pumped rugs bleeds out net of cost.
- **Net-of-cost discipline:** every PnL is `gross − cost`; `net < gross` asserted; the 150 bps haircut
  floor is widen-only (lowering it raises `ValueError`).
- **Leak-free / clean-room (C-7 / C-2):** the gate path references **no** `truth_*` field and imports
  **no** `sniper_sim` module — enforced by an AST + import-graph guard, proven non-vacuous by
  planted-leak tests (the guard RAISES when a leak is planted).
- **Purge is load-bearing (anti-leakage):** `assert_purge_is_load_bearing` proves the purge drops train
  rows whose label horizon overlaps the test window, and a non-vacuity check confirms it removed rows a
  no-op purge would have left in. Forward-only, event-time-ordered, never shuffled.
- **Deterministic:** the bound reproduces byte-for-byte given the seed.

---

## 4. Why the verdict is UNPROVEN (the binding fact)

**There is no recorded real mainnet data in this build.** Ingestion has a SHADOW/RECORD mode but no
live feed has been run; every corpus is `IS_BOOTSTRAP_NOT_REAL=True` synthetic. Therefore:

- GATE-A / GATE-B **cannot be computed on anything that means edge** — there is nothing real to compute
  them on.
- The lone "model beats baseline" number is on a corpus *constructed* so a risk-reading model can win.
  It is a **pipeline smoke test, NOT edge**.
- Building the remaining harness machinery (deflation, stratification, clock-shift control, group-purge)
  on synthetic data would still not produce edge — it would produce more `is_bootstrap_not_real` numbers.

No agent targeted, tuned toward, or fabricated a passing edge or win-rate. The honest absence is the
deliverable. This matches the `GO-PAPER-ONLY` edge verdict exactly.

---

## 5. Where a solo desk cannot win (stated plainly, carried from the edge verdict)

This is not pessimism; it is the design constraint that shapes the entire system. The bot is
**DETECTION-COMPETITIVE, SUBMISSION-DISADVANTAGED**:

- **Block-0 of any new pool** is owned by N+0 insiders co-bundling with the LP-add. Do not race it.
- **Migration-block-0 of PumpSwap** is owned by atomic migration-crank co-bundlers (confirmed 2026).
- **SWQoS reserves ~80% of leader QUIC for staked nodes (~83% first-block hit rate);** an unstaked solo
  desk lives in the contested ~20% lane. Co-location does not close the staked-lane gap.
- **ShredStream is table stakes in 2026** — it lets us *play*, it does not make us faster than the pros
  who also have it.

The realistic niche is the **inverse of the speed race**: skip the detectable rugs the fast bots eat,
enter the survivors after the block-0 melee clears, and exit with discipline. That is **selection and
risk discipline, not latency** — the only surface where the numbers can come out positive net of cost.
The five edge hypotheses (EH-001 safety-selective late entry, EH-002 exit discipline, EH-003
migration-survivor selection, EH-004 coin-profile specialization, EH-005 smart-money-as-filter) each
carry a pre-registered **kill condition** and are measured — not assumed — on recorded data. EH-002 is
flagged as the most sim-circular surface; EH-005 is classified expected-ZERO (default dead, filter-only,
never a buy trigger).

---

## 6. Honest gaps — reported, NOT credited

The recorded-data validation modules below are **not yet implemented in production code**. They are not
needed to render the PAPER verdict (no recorded data exists to run them on), but they are **blocking on
the path to real capital** and are tracked in `docs/pre-live-checklist.md` block A:

| Condition | What is missing | Status |
|---|---|---|
| **C-9** | Append-only hashed experiment log + trial-count significance deflation | NOT built — `grep` for `deflat*`/`ExperimentLog` returns nothing |
| **C-11** | Adverse-selection haircut calibration from recorded R1 fills (currently the static 150 bps UNCALIBRATED floor) + ">200 bps → re-derive EH-001" sub-gate | NOT built (no recorded fills exist) |
| **C-3 / C-13** | Tip-contention-stratified GATE-A report + per-surface independence report (data foundation present: `tip_floor_at_decision_lamports`, `tip_contention_bucket` carried on FeatureFrame) | NOT built (report layer) |
| **C-5** | Global-clock-shift bootstrap control + per-feature/label-horizon placebos (contract-level clock discipline IS enforced — block_time authoritative, wall_clock never a join key) | NOT built (the over-a-backtest control) |
| **C-10** | Group-aware purge by creator / bundler / deploy-template fingerprint (fingerprints carried on `LaunchEvent`, foundation present) | NOT built (the purge consumer) |
| **≥5-window CPCV** | Full purged+embargoed CPCV with per-window CIs, shuffle/placebo + slippage-stress adversarial guards, SimulationVenue depth-based cost burn-in | NOT built |

These are the **recorded-data validation program** (R1/E-program work), not a defect in the PAPER
deliverable. They were reported honestly at G4 and not credited.

---

## 7. The path to a real verdict (the staged-rollout ladder)

Real capital is **DISABLED by default behind `DRY_RUN_ENABLED=true`** and is enabled only by explicit
CEO authorization after the recorded-data gates pass. The ladder (fractional-Kelly throughout, hard cap
≤ ¼ Kelly; no signal or LLM may ever grow size):

| Rung | What runs | Capital | Gate to advance |
|---|---|---|---|
| **R0 — Sim** | `sniper_sim` mechanism studies | None (synthetic) | Direction only — **NEVER licenses capital** |
| **R1 — Shadow / record** | Live ingestion in SHADOW; record point-in-time snapshots; submit nothing | None (real data, no orders) | ≥ ~3,000 recorded launches with point-in-time features + event-time labels; leak audit clean; baseline + model both computable |
| **R2 — Paper / dry-run** | Full triple loop vs SimulationVenue driven by recorded launches; JitoJupiterVenue DRY-RUN (quote→build→sign→DON'T-send) | None (paper) | **GATE-A AND GATE-B both pass** on purged/embargoed walk-forward windows, lower 95% bound > 0 on both; safety primitives all fire on demand. **Necessary, NOT sufficient (C-8)** — recorded fills have no own-order market impact |
| **R3 — Tiny-real** | Live submit, capped throwaway wallet the CEO can fully lose (≤ 2 SOL, ¼-Kelly) | Real, incinerable. **CEO authorization required** | After ≥ 100 real trades across ≥ 2 windows: live GATE-A AND GATE-B both hold, lower 95% bound > 0; realized haircut within calibrated band; no breaker-trip pathology. **R3 is a FRESH proof, not a continuation of R2** |
| **R4 — Scale** | Stepwise size increase | Larger, still bounded | Each step requires a fresh passing walk-forward window **at the new size**; any failed gate → revert |

**No path reaches R3 without a passing walk-forward result on RECORDED data.**

---

## 8. The pre-live (R3) checklist — three blocks, all must be green

Source of truth: `docs/pre-live-checklist.md`. Current build status: **A = NOT MET, B = NOT MET, C =
NOT GIVEN** — the correct, honest paper-deliverable state.

- **A. Edge proven on RECORDED data** — R1 recording (≥ ~3,000 launches), completeness/survivorship
  bounded (C-6), leak/clock audit clean (C-5/C-7), baseline frozen (C-4), haircut calibrated (C-11),
  experiment log + deflation (C-9), group-purge (C-10), GATE-A **and** GATE-B PASS, tip-contention
  stratification (C-3), independent-surface report (C-13). *If A fails, "no edge net of cost" is the
  correct, successful deliverable — do not fund.*
- **B. Custody & security hardened (COND-G4-2)** — signer-side spend-cap / program-allowlist /
  transfer-pin refusals **built + test-proven** (F-01, currently a scaffold), real image digests (F-10),
  signer container lockdown (F-07), hash-locked deps (F-02), CI CVE scan (F-03), GH Actions pinned to
  SHAs (F-04), secret-clean re-verified on the committed tree.
- **C. CEO legal + funding authorization** — legal confirmation for the operator's jurisdiction
  (OQ-009), funding policy (capped incinerable trade-only wallet ≤ 2 SOL, never main holdings), risk
  floors tightened for the live tranche, explicit R3 sign-off recorded.

---

## 9. The one-line summary

**AATS is an honestly-built paper sniper whose edge is not yet proven, whose acceptance harness is built
and proven correct, whose every number to date is synthetic, and whose real-capital path is disabled
until that edge is demonstrated on recorded mainnet data. The finding "unproven" is the deliverable, and
it is a successful project outcome — not a failure.**
