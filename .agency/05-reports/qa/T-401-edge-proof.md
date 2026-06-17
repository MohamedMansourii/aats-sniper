# T-401 — HONEST EDGE PROOF (G4) — `backtest-qa-engineer`

**Task:** T-401 — confirm GATE-A + GATE-B harness BUILT and COMPUTES CORRECTLY; run on the
available (bootstrap/synthetic) corpus; report numbers labeled `is_bootstrap_not_real`.
**Date:** 2026-06-16
**Verdict:** **UNPROVEN-NO-REAL-DATA** (the correct, honest outcome — there is NO recorded
mainnet data in this offline build; live edge cannot be and is not proven).
**Companion:** `EDGE-VERDICT.md` (GO-PAPER-ONLY), `walk-forward-methodology.md`,
`validation-harness.md`.

---

## 0. THE HEADLINE (read first)

**Live edge is UNPROVEN. The GATE-A / GATE-B harness is BUILT and computes correctly. No
real recorded data exists; every number below is `is_bootstrap_not_real` and DOES NOT license
one lamport of capital.** This matches and re-confirms the `GO-PAPER-ONLY` verdict: real
recorded data via SHADOW/RECORD on mainnet (R1) is required before GATE-A/GATE-B can be
computed on anything that means edge. Real capital stays DRY-RUN-disabled.

I did NOT target, tune toward, or fabricate any passing edge or win-rate. The only
"model-beats-baseline" number here is on a synthetic corpus whose generative process is
constructed so a risk-reading model CAN beat naive momentum; it is a **pipeline smoke test,
NOT edge**.

---

## 1. What I confirmed BUILT and CORRECT this session

### 1.1 GATE-B (model-vs-naive-baseline, lower-95% bootstrap bound > 0) — BUILT, CORRECT
`aats/models/gate_b.py`. Re-ran `tests/models/test_gate_b.py` -> **15/15 PASS** this session.
- Delta = `model_net_pnl_per_risk - baseline_net_pnl_per_risk`, hand-checked (+2.0 on the
  deterministic fixture).
- `gate_b_pass` iff lower-95% bootstrap bound > 0 (seeded, deterministic).
- A declined trade contributes 0 (skip credit), proven.
- NO win-rate field anywhere (asserted absent on both `GateBResult` and `TradeOutcome`).
- Fail-closed on empty (`ValueError`); model-loses fixture -> `gate_b_pass=False`; noise
  fixture -> `gate_b_pass=False`.

### 1.2 GATE-A (aggregate net-of-cost PnL, lower-95% bootstrap bound > 0) — BUILT THIS SESSION
GATE-A had **no standalone aggregate** in the codebase before T-401 — only the per-trade
cost gate (`aats/risk/cost_model.py`, REJECT iff `edge <= total_cost`). The aggregate gate
(the symmetric companion to GATE-B with a lower-95% bound and skip-credit) is harness code,
my lane to write and own. I built it:
- **`aats/models/gate_a.py`** — `compute_gate_a()` -> `GateAResult`. It consumes the SAME
  `TradeOutcome` record as GATE-B (single source of truth). Headline =
  `total_net_pnl_lamports` (exact integer aggregate, net of cost); `gate_a_pass` iff the
  lower-95% bootstrap bound of the per-trade mean is strictly > 0.
- A declined trade contributes 0 (skip credit). NO win-rate field. Fail-closed on empty.
  Deterministic given the seed.
- Exported from `aats/models/__init__.py`.

### 1.3 Clean-room walk-forward harness package — BUILT THIS SESSION (`tests/validation/`)
This directory **did not exist** before T-401. I built:
- **`tests/validation/harness.py`** — the NET-OF-COST `TradeOutcome` resolver (full
  round-trip cost stack deducted EVERY trade; **total = 310 bps** = 50 AMM + 30 tip + 5
  priority + 40 entry-slip + 35 exit-slip + **150 UNCALIBRATED adverse-selection floor**,
  widen-only C-11); the **purge + embargo walk-forward windowing** (forward-only,
  event-time-ordered, never shuffled; PURGE drops train rows whose label horizon overlaps
  the test window; with a load-bearing-purge assertion).
- **`tests/validation/test_edge_gate_proof.py`** — drives GATE-A + GATE-B on the bootstrap
  corpus with a model-WINS control (oracle selects winners) and a model-LOSES control
  (anti-oracle selects losers), plus the FROZEN naive-momentum baseline (C-4).
- **`tests/validation/test_clean_room_import_guard.py`** — AST + import-graph guard: FAILs on
  any `truth_*` reference or `sniper_sim` import in the gate path; proven non-vacuous by
  planted-leak tests.

**Suite result this session:** `tests/validation/` -> **22 PASS**. Combined
`tests/models/ + tests/validation/` -> **118 PASS**. Full suite (excl. the known
non-hermetic concurrent test) -> **1840 passed, 2 skipped**.

---

## 2. The gates return the RIGHT SIGN on the controls (correctness proof)

Run on the bootstrap corpus (`generate_synthetic_corpus(n=4000, seed=20260616)`,
`IS_BOOTSTRAP_NOT_REAL=True`, 895 survived / 3105 faded-or-rugged), cost stack 310 bps,
0.1 SOL at risk/trade, 2000 bootstrap resamples:

| Cohort | GATE-A total net | GATE-A lower-95% | GATE-A | GATE-B delta | GATE-B lower-95% | GATE-B |
|---|---|---|---|---|---|---|
| **oracle (model-WINS control)** | **+104.63 SOL** | +0.0247 SOL/trade | **PASS** | **+0.3999** | +0.3852 | **PASS** |
| **frozen naive baseline** | **-55.36 SOL** | -0.0158 SOL/trade | **FAIL** | (control) | (control) | — |
| **anti-oracle (model-LOSES control)** | **-289.08 SOL** | -0.0734 SOL/trade | **FAIL** | **-0.5843** | -0.5989 | **FAIL** |

**Interpretation (correctness, not edge):**
- The model-WINS control passes BOTH gates with a lower-95% bound strictly > 0. Correct sign.
- The model-LOSES control FAILs BOTH gates (negative aggregate, negative delta, lower bounds
  below 0). The dangerous stale-edge case does NOT pass. Correct sign.
- A **declined trade contributes 0** — proven by a 2-row fixture (winner taken, loser
  declined -> total equals the single winner's net, not the loss).
- **The frozen naive-momentum baseline itself FAILs GATE-A (-55.36 SOL net of cost).** This
  is NOT a harness bug: the corpus has ~78% non-survivors and a 310 bps cost stack, so dumb
  momentum that buys hype-pumped rugs bleeds out net of cost. This is exactly the EH-001
  premise (naive momentum is fooled by detectable rugs). The oracle "model" only passes
  because it reads the **post-hoc public label** to simulate skilful selection — it is a
  CONTROL, not a predictor, and it earns no capital license.

All five comparisons are **deterministic** (same seed reproduces the bound byte-for-byte;
asserted in `test_gate_a_deterministic`).

---

## 3. Net-of-cost discipline (no gross number reaches a gate)

`walk-forward-methodology.md §5`: every PnL is NET. Proven:
- `resolve_trade_outcome` computes `net = gross - (total_cost_bps/10000 * sol_at_risk)` in
  exact integer lamports via Decimal. `test_winner_net_is_gross_minus_full_cost_stack`
  asserts the arithmetic and that `net < gross`.
- The adverse-selection haircut floor is **widen-only**: `CostParams(adverse_selection_bps=100)`
  raises `ValueError` (the 150 bps UNCALIBRATED floor, C-11, cannot be lowered).
- The cost stack is non-trivial (>= 200 bps before slippage; 310 bps total).

---

## 4. Leak / clock discipline carried into T-401

- **Clean-room import boundary (C-7/C-2):** the gate path (`gate_a.py`, `gate_b.py`) + the
  harness package reference NO `truth_*` field and import NO `sniper_sim` module. Enforced
  by an AST + import-graph guard, proven non-vacuous by planted-leak detection
  (`test_guard_is_non_vacuous_*`, `test_guard_detects_truth_attr_access`).
- **Purge is load-bearing (anti-leakage):** with a large label horizon (H=20000 slots), the
  purge MUST drop the train rows just before the test window; `assert_purge_is_load_bearing`
  proves no surviving train row's horizon overlaps the test window, and a non-vacuity check
  confirms purge removed rows a no-op purge would have left in.
- **Forward-only, event-time-ordered windows:** every train row's event-time is strictly
  before its test window start; the test window is contiguous in event-time. Never shuffled.
- The **T-400 leak/clock foundation stands** (re-confirmed there this session): all four
  ADR-0010 provenance/load guards, the model-side `assert_no_label_taint`, the training-wired
  `assert_event_time_leq_decision`, the T-300a block-time clock fix, the frozen C-4 baseline,
  and the C-6 completeness audit are real, called in the live pipeline, and RAISE on planted
  leaks. Provenance + no-truth-fields guards re-run this session: **46/46 PASS**.

---

## 5. What is NOT validated and why (honest gaps — reported, NOT credited)

The T-401 brief asks me to confirm C-9 / C-11 / C-3 / C-13 "wired." The honest answer:
**they are NOT yet implemented in production code.** I will not fabricate credit.

- **C-9 (experiment-log deflation):** NO append-only hashed experiment log, NO trial-count
  significance deflation (White's-reality-check / Bonferroni) exists. `grep` for any
  `deflat*` / `ExperimentLog` / `experiment_log` implementation returns NOTHING in `aats/`.
  GATE-A/GATE-B currently compute an UNDEFLATED lower-95% bound. **GAP.**
- **C-11 (calibrated-haircut sub-gate):** the haircut is the static 150 bps UNCALIBRATED
  floor; there is NO calibration from recorded R1 fills (no recorded fills exist) and NO
  ">200 bps -> re-derive EH-001" sub-gate. **GAP.**
- **C-3 / C-13 (tip-contention + independent-surface stratification):** the FeatureFrame
  CARRIES `tip_floor_at_decision_lamports` + `tip_contention_bucket` (data foundation present),
  but there is NO stratified GATE-A report by contention bucket and NO
  `negative_selection_residual` flag, and NO per-surface independence report. **GAP.**
- **C-5 (global-clock-shift control):** the contract-level clock discipline is enforced
  (block_time authoritative, wall_clock never a join key — T-400 proven), but the
  shift-every-slot-+1-and-assert-PnL-statistically-different bootstrap control
  (`clock_not_load_bearing` auto-FAIL) over a backtest is NOT wired. The per-feature /
  label-horizon placebos are NOT wired. **GAP.**
- **C-10 (group-aware purge):** `creator_wallet` / `bundler_cluster_id` /
  `deploy_template_fingerprint` are carried on `LaunchEvent` (foundation present), but NO
  harness groups by them across the embargo boundary and NO with/without group-purge report
  exists. **GAP.**
- **>= 5-window CPCV:** the windower produces purged+embargoed forward windows, but the full
  >= 5 non-overlapping windows with per-window bootstrap CIs aggregated, CPCV for model
  selection, the shuffle/placebo and slippage-stress adversarial guards, and the
  SimulationVenue depth-based cost-stack burn-in are NOT yet built. **GAP.**

None of these gaps can change the verdict, because the binding fact dominates them all:

- **NO RECORDED REAL DATA EXISTS.** Ingestion has SHADOW/RECORD but no live feed. Every
  corpus is `IS_BOOTSTRAP_NOT_REAL=True` synthetic. GATE-A/GATE-B on recorded data **cannot
  be computed** because there is no recorded data. Building the deflation / stratification /
  clock-shift / group-purge machinery on synthetic data would still not produce edge — it
  would produce more is_bootstrap_not_real numbers.

---

## 6. Suite status (honest)

- `tests/validation/` (T-401): **22 PASS**.
- `tests/models/` + `tests/validation/`: **118 PASS**.
- Full suite minus the known non-hermetic test: **1840 passed, 2 skipped**.
- **Known defect (NOT my lane, filed against `agent-orchestration-engineer`):**
  `tests/controller/test_snipe_handoff.py::test_concurrent_thousand_snipes_one_winner` is
  NON-HERMETIC — `InMemoryStateStore.claim_entering` holds a 30s wall-clock lock TTL, and the
  1000-OS-thread storm can exceed it, so a second claim legitimately wins (`assert 2==1`).
  **This session it PASSED in isolation 5/5 (~1s each, under the TTL)** — the flake is
  load/box-dependent (T-400 saw it fail 5/5 at 80-155s). Root cause unchanged: the test must
  freeze its injectable clock. It is a controller-test hygiene defect, independent of the edge
  verdict; it does not gate GATE-A/GATE-B correctness.

---

## 7. VERDICT

**edgeVerdict = UNPROVEN-NO-REAL-DATA.**

- The GATE-A + GATE-B harness is **BUILT and COMPUTES CORRECTLY** (right sign on
  model-wins/model-loses controls, declines contribute 0, lower-95% bound logic correct,
  net-of-cost, fail-closed, deterministic, clean-room, purge load-bearing).
- **NO live edge is or can be proven** — there is no recorded mainnet data; all corpora are
  `is_bootstrap_not_real` synthetic; the lone model>baseline number is a beatable-by-design
  smoke test, NOT edge.
- **Required next step:** recorded real data via SHADOW/RECORD on mainnet (R1, >= ~3000
  launches with point-in-time features + event-time labels), THEN GATE-A/GATE-B on the
  purged/embargoed walk-forward windows with C-9 deflation + C-5 clock control + C-10
  group-purge + C-11 calibration wired. This matches `GO-PAPER-ONLY`. Real capital stays
  DRY-RUN-disabled.

---

```
=== HANDOFF ===
FROM: backtest-qa-engineer
TASK: T-401 — HONEST EDGE PROOF (G4)
STATUS: COMPLETE (verdict UNPROVEN-NO-REAL-DATA)
DELIVERABLES:
  - aats/models/gate_a.py (the GATE-A aggregate net-of-cost gate, exported)
  - tests/validation/__init__.py, harness.py, test_edge_gate_proof.py,
    test_clean_room_import_guard.py (the clean-room walk-forward harness package)
  - .agency/05-reports/qa/T-401-edge-proof.md (this report)
SELF-CHECK: ran tests/validation/ (22 PASS), tests/models/+validation/ (118 PASS),
  full suite minus the known non-hermetic test (1840 passed / 2 skipped), provenance+truth
  guards (46 PASS), gate_b (15 PASS). Confirmed GATE-A/GATE-B return the right sign on
  model-wins (+104.63 SOL / delta +0.40, both lower-95% > 0 -> PASS) and model-loses
  (-289.08 SOL / delta -0.58 -> FAIL) controls; declined trade contributes 0; net-of-cost
  (310 bps stack, 150 bps haircut floor widen-only); clean-room import guard non-vacuous;
  purge load-bearing. All bootstrap numbers stamped is_bootstrap_not_real.
RISKS: live edge unprovable until recorded mainnet data exists; C-9/C-11/C-3/C-13/C-5/C-10
  and >=5-window CPCV + SimulationVenue burn-in are gaps (reported, not credited).
NEEDS: R1 SHADOW/RECORD recorded corpus before any GATE-A/GATE-B can mean edge; the C-5/C-9/
  C-10/C-11 condition modules + >=5-window CPCV + SimulationVenue cost burn-in on this
  harness foundation; agent-orchestration-engineer to freeze the injectable clock in the
  concurrent single-winner test.
===============
```
