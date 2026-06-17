# validation-harness.md — Clean-Room Recorded-Data Validation Harness (T-204)

**Version:** 1.1.0 — **FROZEN** (post-G1-red-team; ADR-0010 added §2.5 build/load guards — per-feature
provenance cutoff, label lineage taint, label/feature disjointness, `recorded_at` honesty — and the
label-horizon/per-feature-lineage placebos in C-5).
**Author:** `solana-systems-architect`
**Date:** 2026-06-16
**Status:** Architecture for the harness `backtest-qa-engineer` builds (T-400/401) and enforces.
It makes lookahead leakage and inherited-optimism **structurally impossible**, not merely policed.
Enforces `walk-forward-methodology.md` cold and the 13 EDGE-VERDICT conditions.

**Companion:** `BLUEPRINT.md §7` (condition map), `data-models.md §9` (point-in-time store),
`walk-forward-methodology.md` (the spec this implements), `EDGE-VERDICT.md` (C-1..C-13).

---

## 1. Why "clean-room" — the leakage this design closes

The sim is admittedly circular: `safety.py:43` reads `truth_is_rug`/`truth_rug_detectable` at an
assumed `catch_rate=0.75`; net PnL is monotone in `model_skill`; the SECURE/FAST exit constants and
`_competitor_delay()` are shared optimistic knobs (red-team 1A/1B/2D). If any of that scaffolding
leaks into the recorded-data harness, the gate proves nothing. The clean-room harness is a
**separate package** that imports the point-in-time store and the production feature/cost code — and
**nothing from `sniper_sim/`**. The guarantee is enforced at build time (§2).

---

## 2. The import / static-analysis guard — FAILS the build on any `truth_*` reference (C-7, AC-056)

A CI build-time check (runs in the validation package's lint stage, blocking):

1. **`truth_*` reference guard.** The build FAILS if any file in the validation pipeline (or any
   module it imports, transitively) references `truth_is_rug`, `truth_max_multiple`,
   `truth_rug_detectable`, or any symbol/attribute/path **derived from** `truth_max_multiple`. Error:
   `truth_field_reference_forbidden`. Implementation: AST scan for those identifiers + an import-graph
   walk that FAILS if `sniper_sim` (or its `types/safety/exits/venue` symbols) appears anywhere in the
   validation package's dependency closure.
2. **Inherited-optimism guard (C-2).** The build FAILS if the recorded cost-stack code imports
   `sniper_sim.venue._competitor_delay`, the `SECURE_EXIT`/`FAST_EXIT` sandwich constants, or any
   `generate_path` symbol. Error: `inherited_sim_constant_forbidden`. The recorded cost stack derives
   `buyers_ahead` and the sandwich/dump haircut from **observed** pool depth-decay and **observed**
   insider/LP-puller dump incidence, calibrated UPWARD until R3 fills measure it.
3. **Program-ID / hardcoded-tip literal guard** (shared with execution-venue.md §3.2): no base58
   program-ID-shaped literal and no hardcoded Jito tip integer in a hot-path or cost-stack file.
4. **Recall-as-parameter guard.** The build FAILS if `catch_rate` (or any rug-recall value) is passed
   as an input to a gate in the validation path. Recall ≥ 0.50 must be a **measured output** of the
   test fold, never a parameter (C-7, AC-056). Error: `recall_must_be_measured`.

These are build failures. The harness cannot even be compiled with a leak of this class present.

### 2.5 Point-in-time guards promoted from runtime audit to build/contract (red-team-1 must-fix #2/#3)

The §2 guards above caught *name-based* leaks (`truth_*`) and *inherited-constant* leaks. The
red-team correctly found that the single highest-risk leak — a forward-looking **label** joined in as
a feature under an innocuous name — was previously closed only in prose. These four guards close it by
construction. They run in the same blocking lint/load stage and are build/load failures, not runtime
audits. They consume the new `FeatureProvenance` manifest and `LaunchOutcome` label dataset
(data-models.md §3.3 / §3A).

5. **Per-feature cutoff guard (`feature_window_exceeds_cutoff`).** For every `FeatureSourceWindow` in
   a frame's `provenance.windows`, FAIL if `max_source_slot > event_time.slot + first_k_slots`. A
   missing window for a non-metadata feature field is `provenance_window_missing` (also a failure) so
   a feature cannot dodge the check by omitting its declaration. This promotes the per-feature
   event-time cutoff from "a reviewer reads the pipeline" to "the build refuses to compile."

6. **Lineage / taint guard (`feature_lineage_touches_label`).** The PRIMARY anti-label-leak defense,
   replacing the name-based scan. Each feature's `lineage_dataset` must be on the allowlist
   (`launch_events`, `feature_frames`, `mcs_scores`, `fills_pre_event`); **`labels` is not on it.** A
   feature whose lineage touches a `labels/` row (or anything derived from one) FAILS regardless of
   column name — so `survived_60s`, `realized_mult`, `fwd_return` are caught by lineage, not by name.
   The `truth_*` AST scan (§2.1) is retained as a cheap belt-and-suspenders second line, demoted
   explicitly to necessary-not-sufficient.

7. **Label/feature disjointness guard (`label_column_in_feature_frame`).** At load, assert the column
   sets of `feature_frames/` and `labels/` are disjoint. Any overlap FAILS the load. Labels live only
   in `labels/` and are joined to features by `event_time` only (§3), never carried on a live frame.

8. **`recorded_at` honesty guard (`recorded_at_before_knowable` / `backfill_recorded_at_regression`).**
   At write/load, FAIL any row with `recorded_at_ms < event_time.block_time_ms` (a datum recorded
   before its on-chain event existed), and the as-of-read audit (§3) FLAGS any correction row whose
   `recorded_at` predates the latest `recorded_at` already present for the same `(dataset, event_time)`
   key. This closes the live-backfill lookahead vector the red-team identified: there is no honest way
   to reintroduce future knowledge under a back-dated `recorded_at`.

---

## 3. Walk-forward engine (implements `walk-forward-methodology.md`)

- **Anchored rolling windows**, forward-only, event-time ordered (block_time, never file order or
  compute-time). Default split train 60% / validate 15% / test 25%; **≥ 5 non-overlapping test
  windows**, each **≥ 300 decision events AND ≥ a declared min wall-clock** (AC-055). Fewer windows /
  events ⇒ `insufficient_data` FAIL (never pass on fewer).
- **Purge + embargo** (walk-forward §3): drop train samples with `event_time + H ≥ test_window_start`;
  embargo ≥ H after each test window. The label horizon H is declared per surface up front.
- **CPCV** for model selection on train+validate; plain k-fold / random shuffle = FAIL on sight.
- **As-of reads from Parquet history** (data-models §9): every feature reconstructed from rows with
  `recorded_at ≤ cutoff` and joined on `event_time` — the partition key is `event_date`, so a
  compute-time join is not addressable. The as-of read runs the `recorded_at` honesty audit (§2.5
  guard 8): any correction row whose `recorded_at` regresses below the latest already present for its
  `(dataset, event_time)` key is flagged a backfill lookahead vector.
- **Labels are joined separately, on `event_time` only.** `LaunchOutcome` rows from `labels/`
  (data-models §3A) are joined to features strictly by `event_time` AFTER the purge/embargo drop; they
  are never merged into `feature_frames/` (§2.5 guard 7 asserts the column sets are disjoint). The
  label horizon H comes from `LaunchOutcome.label_horizon_h_slots`, the same H that drives purge.

---

## 4. Condition wiring (each C is a harness module, not a hope)

### C-1 — latency-honesty propagation (with `latency-budget.md`)
The recorded `buyers_ahead` distribution is **shifted right by ~one slot** of staked/pro traffic
(the staked-lane penalty), and the adverse-selection haircut is treated as a **FLOOR to widen**. The
harness reads the named extra-slot penalty from `latency-budget.md` and applies it to the cost stack;
it never uses the sim's understated `_competitor_delay` (which is import-blocked, §2).

### C-2 — no inherited optimism
Enforced by the §2 import guard. The cost-stack code derives line items from observed data; a build
that imports a sim constant fails.

### C-3 — tip-cohort-bias stratification (FR-047)
Each candidate's `tip_floor_at_decision_lamports` + `tip_contention_bucket` (FeatureFrame, recorded at
event-time) drive a stratified GATE-A report: net PnL by low/medium/high contention bucket. If only
the **low-contention** cohort is profitable, the harness flags `negative_selection_residual` and
**blocks R4** (scale-up). (C-13 reporting is the same module.)

### C-4 — frozen baseline (FR-015, AC-056-adjacent)
The naive-momentum baseline params — `K`, percentile threshold, unit-of-risk
(net-PnL/SOL-at-risk or /downside-deviation), candidate universe — live in a **committed, hashed
config** (`baseline.frozen.json`, hash recorded). The baseline is built from `first_k_buy_pressure` +
`first_k_volume_lamports` (data-models §3.2). A test **FAILS** (`baseline_changed_after_fit`) if the
config hash changes after the first model fit. The baseline runs through the **identical** windows,
purge/embargo, cost stack, position cap, and ExitEngine as the model — only selection intelligence is
denied it (walk-forward §4).

### C-5 — clock audit + frozen haircut (FR-044/045, AC-057)
- **Global-clock-shift control (NECESSARY, NOT SUFFICIENT — red-team-1 must-fix #4).** The harness
  runs a control with all `event_time.slot` shifted +1 and asserts GATE-A net PnL is **statistically
  different** (p<0.05 bootstrap). If shifting the clock does NOT change results, *some* timestamp is
  not load-bearing ⇒ auto-FAIL (`clock_not_load_bearing`). **This control is explicitly
  necessary-not-sufficient:** a *uniform* +1-slot shift of every event preserves the relative label
  horizon, so a **horizon-preserving leak** (a feature that read `event_time + H` data, or a label
  joined as a feature) survives it. It proves a timestamp matters; it does NOT prove every feature
  respects the per-feature cutoff. The structural guarantee against that class is the §2.5 build
  guards (provenance cutoff + lineage taint), not this control.
- **Independent label-horizon + per-feature-lineage placebo (the SUFFICIENT complement, red-team-1
  must-fix #4).** In addition to the global shift, the harness runs placebos that perturb the **label
  horizon** and **individual feature lineages** independently (not a uniform global slot shift):
  (a) re-resolve `LaunchOutcome` at `event_time + H'` for a shuffled-within-block `H'` and assert
  GATE-B collapses to noise — if a feature is secretly the label, GATE-B *survives* the label-horizon
  shuffle, which auto-FAILs (`feature_is_label_horizon_invariant`); (b) for each feature in turn,
  shift only that feature's source window by +1 slot and assert the feature value changes — a feature
  invariant to its own lineage shift is either constant or leaking from a fixed forward source
  (`feature_lineage_not_load_bearing`). A horizon-preserving leak that the global shift hides is
  caught here because the perturbation is **per-feature and per-label**, not global.
- **Frozen haircut:** the adverse-selection haircut is fit on **train-fold fills only**, frozen, and
  applied **unchanged** to test folds. Any per-window re-fit ⇒ auto-FAIL (`haircut_refit_leak`).

### C-6 — completeness audit (FR-009, AC-006)
Reconcile recorded launches against an **independent pool-create census** (second source). Compute +
bound the miss rate. Rows with no completed first-K snapshot OR no resolved label are carried as
`completeness_status = CENSORED` (data-models §7), never dropped. Assert
`(complete + CENSORED) / census_total ≥ 1 − declared_max_miss_rate`. Survivorship-free is **measured**,
not asserted.

### C-7 — clean-room (FR-019, AC-056)
The §2 guards + recall-as-measured-output. Catchable-rug recall ≥ 0.50 is computed on **held-out
labeled rugs in test folds**; it is a result, never an input.

### C-8 — R2 necessary-not-sufficient (§staging caveat)
The harness report states R2/GATE-A is necessary-not-sufficient; the first **real** haircut
validation is deferred to R3 fills. Fill-probability is modeled **conditional on the outcome label**
(not independent), so the slippage stress test perturbs the **correlation** (you fill worst exactly
when you are most wrong), not just the level (C-8/2E).

### C-9 — experiment log + deflation (FR-020, AC-059)
A **committed, append-only, hashed experiment log** records every config / threshold / feature-set /
profile-bucket / exit-mode evaluated. It is a **precondition** for computing GATE-A/GATE-B: the
harness refuses to score (`experiment_log_missing_or_tampered`) if the log is absent, empty, or its
hash mismatches. The significance threshold is **deflated as a function of the logged trial count**
(White's-reality-check / Bonferroni-style haircut). No log ⇒ auto-FAIL.

### C-10 — group-aware purge (FR-046, AC-058)
Group by `creator_wallet` / `bundler_cluster_id` / `deploy_template_fingerprint` (carried on
`LaunchEvent`, data-models §2) across the embargo boundary. Report metrics **with AND without**
group-purge; a >20% relative GATE-A delta is flagged `actor_identity_memorization`.

### C-11 — calibrated-haircut sub-gate (FR-044)
The haircut is **calibrated from recorded R1 fills BEFORE GATE-A is computed at R2** (realized
slippage conditional on subsequent adverse move, fit on train folds only per C-5). If the calibrated
haircut **> 200 bps at target size**, EH-001's net midpoint is re-derived and the surface re-justified
or **killed**. Pre-calibration default = **150 bps, labeled UNCALIBRATED** (OQ-007).

### C-12 — regime + staleness (§staging)
Report GATE-A/GATE-B **per regime bucket** (tip regime, launch-density regime, pre/post venue-mechanics
change). A drift monitor on the launch-population distribution can flag a regime break; if it breaks
between an R2 pass and R3 funding, or the passing window exceeds the declared freshness bound, the
gate **auto-re-runs on fresh recorded data** before any lamport moves (infrastructure.md staging).

### C-13 — independent-surface reporting (FR-048)
Report how many of EH-001..EH-005 survive **independently** under the C-1/C-2-corrected competitor
distribution. Pooled-only survival ⇒ classified as **one fragile edge**, not a portfolio.

---

## 5. The pass bar (so QA rules cold — `walk-forward-methodology.md §7`)

A rung gate PASSES iff ALL hold on the TEST windows of **recorded** data:
1. **GATE-A:** aggregate net-of-cost PnL > 0, **lower 95% bootstrap bound > 0**, ≥ 5 purged/embargoed
   windows, under the **trial-count-deflated** threshold (C-9).
2. **GATE-B:** model net-PnL-per-unit-risk beats the frozen naive-momentum baseline, lower 95% bound
   > 0, same windows.
3. **Leak audit clean:** no post-`event_time` feature/label — proven by the §2.5 build guards
   (per-feature provenance cutoff, lineage taint against `labels/`, label/feature disjointness,
   `recorded_at` honesty), NOT by name-scan alone; purge+embargo applied; no global-stat
   normalization; survivorship-free measured (C-6); the global clock-shift control is load-bearing AND
   the per-feature/per-label-horizon placebos collapse to noise (C-5); shuffle/placebo edge vanishes.
4. **Calibration acceptable:** reliability curve + ECE within declared tolerance (the threshold gate
   and ¼-Kelly both consume the probability as if true).
5. **Safety proven:** breaker, survivable stop, dead-man's switch fire on demand within budget (T-402).

A point estimate > 0 with a lower bound < 0 is a **FAIL** (noise, not edge). Any single failure ⇒
rung FAIL, no capital advance.

---

## 6. Adversarial guards (walk-forward §8)
Shuffle/placebo (edge must vanish under label shuffle within purged blocks); slippage stress
(haircut at top of band + size scaled up; if edge inverts, block R4); regime stratification;
multiple-testing deflation (C-9). All wired into the harness; none optional.

---

## 7. Post-G1 changes
Any change to the harness contract (window sizing, pass bar, condition wiring) is an ADR + delta
notice naming T-400/T-401 and any dependent task. The frozen-baseline config and experiment-log hash
are themselves change-controlled artifacts — altering them after first fit is a test FAILURE, not an
edit.
