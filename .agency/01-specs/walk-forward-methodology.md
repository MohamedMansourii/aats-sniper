# WALK-FORWARD METHODOLOGY — the validation spec `backtest-qa-engineer` ENFORCES

**Author:** `quant-research-lead`
**Date:** 2026-06-16
**Companion to:** `./EDGE-VERDICT.md` (GATE-A / GATE-B are defined there; this file says *how* to test them without lying to ourselves).
**Audience:** `backtest-qa-engineer` runs this cold, with zero questions back to me. `ml-prediction-engineer` and `feature-quant-engineer` build to it. If any step here cannot be satisfied because the data can't prove temporal correctness, the verdict for that run is **FAIL — treat as fraudulent until shown otherwise.**

> One sentence that governs everything below: **point-in-time correctness is the whole game.** Every feature and every label uses **event-time** (the slot/timestamp at which the information was actually observable on-chain), never **compute-time** (when the backtest script happened to compute it). Lookahead silently inflates every backtest; we assume it is present until purge/embargo + an explicit leak audit prove it is not.

---

## 1. Data contract (what the recorded dataset MUST contain)

Each sample = one launch (or one migration event) and carries:
- **`event_time`** — the canonical slot + wall-clock at which the snipe decision would have been made (e.g. LP-add slot for new launch; migration slot for graduation). This is the anchor for ALL point-in-time joins.
- **Features as-of `event_time` ONLY.** Every feature value must be reconstructable from data with observation-time ≤ `event_time`. Any feature whose value depends on data after `event_time` is a **leak → FAIL**.
- **Outcome label** computed at a fixed **label horizon** measured in **event-time** from `event_time` (e.g. realized return / survival at `event_time + H`), where H is fixed per surface and declared up front.
- **Realized execution record** for paper/live rungs: actual slot delay, buyers-ahead/realized slippage, tips, priority, AMM fees, sandwich incidence — the full cost stack from `EDGE-VERDICT.md` §3.
- **No survivorship filtering.** The dataset includes the rugs, the duds, the un-graduated, and the launches we skipped. Removing dead tokens is survivorship bias → **FAIL.** Skips are recorded with their would-be decision so the counterfactual cohort exists.

---

## 2. Rolling walk-forward windows (train → validate → test)

**Anchored, rolling, forward-only.** No window ever trains on data later than its test window. Event-time ordering, not file order.

```
|<-- TRAIN -->|<-purge->|<-- VALIDATE -->|<-purge+embargo->|<-- TEST -->|   (then roll forward)
```

- **TRAIN:** fit the model / calibrate thresholds.
- **VALIDATE:** select hyperparameters, operating threshold, calibration. **Never touched for the final metric.**
- **TEST:** out-of-sample, scored ONCE. GATE-A/GATE-B are computed here only.
- **Roll forward** by the test-window length and repeat. **Minimum 5 non-overlapping test windows** before any rung gate can pass (so the result is not one lucky regime).
- Window sizing is **count-based and time-based both** — each test window must contain **≥ 300 decision events** AND span **≥ a declared minimum wall-clock** so a single launch frenzy cannot dominate. If event density is too low, widen the window, never shrink the event floor.

**Default starting split** (tune in VALIDATE, document the final values): train ≈ 60%, validate ≈ 15%, test ≈ 25% of each rolling block; ≥ 5 blocks.

---

## 3. Purge and embargo (anti-leakage — the part naive backtests skip)

Memecoin outcomes overlap in event-time (a label horizon H means a sample's outcome is still resolving for H after `event_time`). Without purge/embargo, test labels leak into training.

- **PURGE:** remove from TRAIN any sample whose **label horizon overlaps** the validate/test window's event-time span. Concretely: drop train samples with `event_time + H ≥ test_window_start`.
- **EMBARGO:** after each test window, embargo a buffer (**≥ the label horizon H**, default the larger of H or a declared min) of subsequent samples from re-entering the next train fold, to kill serial-correlation leakage across the boundary.
- **No feature computed with a global statistic** (e.g. z-score against the full-dataset mean, a "rank among all launches", an end-of-day normalization) — all normalization is **as-of `event_time`** using only past data. A global-stat feature is a leak → **FAIL.**
- **Combinatorial purged CV (CPCV)** is the preferred cross-validation for model selection on the train+validate span, with purge+embargo between every fold. Plain k-fold or random shuffle on time-series memecoin data is **FAIL on sight.**

---

## 4. The naive-momentum baseline (what the model MUST beat — GATE-B)

The baseline is **dumb, mechanical, and fixed before the model is built** so it cannot be reverse-engineered to be beatable:

> **NAIVE-MOMENTUM BASELINE:** at `event_time`, enter every candidate that passes a single, trivial momentum rule — *positive price/volume momentum over the first K slots above a fixed percentile* (e.g. buy if first-K-slot net buy pressure > median) — with **no safety gate, no ML, no smart-money filter**, the **same position cap**, the **same ExitEngine exit**, and the **same full cost stack** applied. It is allowed the same execution model so the comparison is apples-to-apples; it is denied all *selection intelligence*.

- The baseline is run through the **identical** walk-forward windows, purge/embargo, and cost model as the model.
- **GATE-B passes** iff the model's selected-cohort **net PnL per unit risk** exceeds the baseline's by a margin whose **lower 95% bootstrap bound > 0**, aggregated across the ≥ 5 test windows.
- If the model cannot beat this, there is no model. De-scope per `EDGE-VERDICT.md` K-1.

A second, even dumber **"buy-everything-that-passes-the-gate"** reference is also logged, to attribute how much edge is *selection* vs *gate* vs *exit*. Attribution is diagnostic, not a gate.

---

## 5. Cost application (no gross numbers, ever)

Every PnL figure in every window is **net** of the full `EDGE-VERDICT.md` §3 stack:
- Jito tip (from recorded live tip levels at that event-time, not a constant), priority/CU, entry slippage (realized buyers-ahead), AMM fee (0.25%/side), exit slippage + sandwich haircut, **and the explicit adverse-selection haircut** (calibrated from realized fills: realized slippage conditional on the subsequent adverse move; until calibrated use the conservative top of the 75–150 bps band).
- A backtest that reports gross PnL, or omits the adverse-selection haircut, is **FAIL — reject and return.**

---

## 6. Calibration check (the model must be honest about its probabilities)

- Produce a **reliability curve** (predicted probability vs realized frequency) on the TEST windows. Demand **calibration, not accuracy** — a model that is 90% "accurate" but whose 0.8-probability bucket realizes 0.5 is a broken gate.
- Report **Brier score** and **expected calibration error (ECE)**; a model failing calibration is sent back to `ml-prediction-engineer` regardless of PnL, because the threshold gate and the fractional-Kelly sizing both consume the probability as if it were true.
- The model must emit **uncertainty**, not a point estimate; high-uncertainty samples may be auto-skipped (de-risk only).

---

## 7. The pass bar (so QA can rule cold)

A rung gate (`EDGE-VERDICT.md` §6) **PASSES** iff ALL hold on the TEST windows of recorded data:
1. **GATE-A:** aggregate net-of-cost PnL > 0 with **lower 95% bootstrap bound > 0** across ≥ 5 purged/embargoed test windows.
2. **GATE-B:** model net-PnL-per-unit-risk beats the naive-momentum baseline with **lower 95% bound > 0** on the same windows.
3. **Leak audit clean:** no feature/label uses post-`event_time` data; purge/embargo applied; no global-stat normalization; survivorship-free dataset confirmed.
4. **Calibration acceptable:** reliability curve + ECE within declared tolerance.
5. **Safety proven:** circuit breaker, survivable stop, dead-man's switch fire on demand within budget (separate QA, but gates the rung).

Any single failure ⇒ rung **FAIL**, no capital advance. A point estimate above zero with a lower bound below zero is a **FAIL** (it is noise, not edge).

---

## 8. Adversarial / anti-overfit guards QA must apply

- **Multiple-testing discipline:** if many thresholds/features were tried, deflate significance (e.g. report a deflated Sharpe / apply a Bonferroni-style or White's-reality-check haircut). Cherry-picking the best of 50 configs is a leak of a different kind → flag it.
- **Regime stratification:** report GATE-A/GATE-B per regime bucket (tip regime, launch-density regime, pre/post any venue-mechanics change). A result that only survives in one regime is regime-fragile → annotate and tighten kill criteria.
- **Shuffle / placebo test:** re-run with labels time-shuffled within purged blocks; the edge MUST vanish. If a "model edge" survives label shuffling, there is a leak → **FAIL.**
- **Slippage stress:** re-run GATE-A with the adverse-selection haircut at the top of band and entry size scaled up; if edge inverts under realistic-worse fills, it is not robust to scaling — block R4.

---

## 9. What this methodology forbids (auto-FAIL list)

- Any feature/label using data after `event_time` (lookahead).
- Compute-time joins, global-statistic normalization, end-of-window leakage.
- Survivorship-filtered datasets (dead tokens removed).
- Plain k-fold / random shuffle / no purge / no embargo on time-series data.
- Reporting gross PnL or omitting the adverse-selection haircut.
- Tuning toward, or reporting, a fixed win-rate as a success metric.
- Passing a gate on synthetic data, or on a point estimate without a lower confidence bound > 0.

If you cannot prove temporal correctness for a run, the run **FAILS** — silence is not a pass.
