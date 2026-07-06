# MODEL CARD — SLOW-loop chart-path / REGIME model (M2-CP-08)

> **BOOTSTRAP, NOT REAL — NO CAPITAL LICENSE.** `is_bootstrap_not_real = True`,
> `no_capital_license = True`.  This is a **retraining-harness SCAFFOLD**: the
> trained production regime model (M2-CP-03) is DATA-BLOCKED until the **R1 recorded corpus**
> (>=3,000 recorded launches with post-migration price-path history) exists.  The numbers below are
> computed on the SYNTHETIC bootstrap corpus and prove the pipeline is **leak-free, calibrated,
> reproducible, and de-risk-inert** — NOT that the model has live edge.  The sole production
> acceptance metric is **net-of-cost PnL + model-vs-baseline on RECORDED data** (the clean-room
> harness), never this synthetic set.  **NO win-rate anywhere.**  Real capital stays **DISABLED
> behind DRY-RUN** regardless of any number here.

## Identity
- **model_version:** `regime-lgbm-mc-v1.0.0-seed20260706-BOOTSTRAP`
- **taxonomy_version:** `1.0.0` (`aats.models.regime_labels`)
- **seed:** `20260706` (deterministic; re-training reproduces these metrics bit-for-bit).
- **loop:** `slow` — SLOW-loop only.  **No ONNX export, no Rust shim, never FAST/SNIPE**
  (`assert_slow_loop_only`; BLUEPRINT triple-loop boundary).
- **algorithm:** multiclass gradient-boosted trees (LightGBM, `objective=multiclass`, multiclass
  log-loss — a proper scoring rule) with per-class isotonic calibration.  The production form is a
  **deep temporal head** (`deep_model_dep = pytorch-forecasting`, TFT / N-HiTS) consuming the
  M2-CP-01 `PricePathTensor`; it is **declared but lazily imported** and slots into
  `aats.models.train_regime._fit_regime_head` without changing the output contract.
- **output:** the frozen `aats.contracts.models.RegimeSignal` (ADR-0014) — a multiclass STATE +
  the four CALIBRATED class probabilities (a genuine distribution) + a predictive uncertainty band.
  **No price field.  No size field.  No win-rate / success-rate / realized-mult field.**

## Asymmetric trust — the accumulation/bullish class is provably INERT (de-risk-only)
A regime output reaches a position ONLY via the SLOW-loop de-risk wiring
(`aats.controller.regime_wiring.SlowLoopRegimeWiring`), which maps the STATE through
`RegimeDeRiskDirective` (codomain `{NONE, REDUCE, FORCE_EXIT, VETO_ENTRY}` — a risk-INCREASING
directive is **inexpressible by type**):

| STATE | directive | effect |
|---|---|---|
| `ACCUMULATION` | `NONE` | **INERT** — sets no flag; cannot delay an exit, relax a stop, or size up |
| `NEUTRAL` | `NONE` | **INERT** |
| `DISTRIBUTION` | `REDUCE` | de-risk: shrink / veto-half (`set_veto_flag`) |
| `RUG_IN_PROGRESS` | `FORCE_EXIT` | de-risk-maximal: `set_narrative_failure_flag` -> ExitEngine |

The SNIPE/FAST loops NEVER consume `RegimeSignal` or call this model — they read the SAME pre-set
scalar de-risk flags (`veto` / `narrative_failure`) they already read.  No hot-path model call.

## Features (input) — 25 exogenous covariates (bootstrap)
Decision-time survivor exogenous covariates (`SURVIVOR_COVARIATE_COLUMNS`): first-60s
microstructure (LP depth, holder count, top-10 concentration, sell tax, sniper-cluster, first-K
buy pressure / unique buyers), survivor TA (rsi/macd/bb_width + presence flags), adversarial
selectivity (smart_wallets_in, entry lag), MCS exogenous covariates (adversarial / contrarian by
construction), and the M2-CP-07 creator-outflow-velocity feature.  **In production the PRIMARY
input is the M2-CP-01 `PricePathTensor`** (log-return / drawdown / holder- & volume-delta channels
with a CENSORED mask), consumed by the deep temporal head; these covariates enter as exogenous
inputs.  **No truth_*/label column can enter the matrix** — `assert_no_regime_label_taint()` fails
the build on any (lineage taint).

## Label (leak-free, co-owned with backtest-qa-engineer) — M2-CP-02 taxonomy
A multiclass STATE over the survivor/exit horizon `[decision, resolution]`, produced by
`build_regime_outcome` / `classify_regime`: `ACCUMULATION | NEUTRAL | DISTRIBUTION |
RUG_IN_PROGRESS` (exhaustive + mutually exclusive), gated by the "remains-sellable at
decision-relevant size" constant-product exit-depth probe.  **A regime is a STATE, never a
win-rate, success-rate, realized multiple, or price.**
- Labels live in a **separate dataset**, joined to features **by event_time ONLY**.
- **Point-in-time (T-300a):** every outcome sample slot in `(decision.slot, resolution.slot]`, in
  strictly increasing order (`build_regime_outcome` forward-window gate); `resolution_event_time =
  decision + H` is stamped and strictly later (horizon proof).  No wall-clock, no forward-fill.
- **Leak audit:** every row asserts `feature_event_time <= decision_event_time` AND
  `resolution_event_time > decision_event_time` (reused `assert_event_time_leq_decision`).
  Result: `LEAK AUDIT PASS: 3000 rows. For every row: feature_event_time <= decision_event_time (equal by event-time join), AND label resolution_event_time strictly > decision_event_time (label horizon proof present). No feature reads post-decision data; the label is the only forward-looking quantity and it lives in a separate dataset joined by event_time only.`
- **Bootstrap class distribution:** ACCUMULATION=222,
  NEUTRAL=836, DISTRIBUTION=987,
  RUG_IN_PROGRESS=955.

## Training window
- corpus n = 3000 (synthetic bootstrap).
- forward-only, event-time-ordered split (**never shuffled**): train=1800 /
  calib=450 / test=750 (the latest event-time window is OOS).
- The real corpus (R1) plugs in at the same `SyntheticRow` / `JoinedExample` interface.

## Calibration (calibration before accuracy)
- method: **per-class isotonic**, fit one-vs-rest on the held-out, time-forward calibration slice
  (never the train or test fold), then row-renormalized to a genuine distribution.
- **OOS test-fold multiclass Brier = 0.5837**,
  **top-label ECE = 0.0432**.
- Reliability diagram (top-label): `artifacts/reliability_regime.json` (10 bins).
- Uncertainty = normalized Shannon entropy of the class distribution in [0,1]; a flat / undecided
  distribution => high uncertainty => **DE-RISK only** (shrinks size downstream); it can NEVER
  size up, widen a stop, or override a hard stop.

## Baseline gap (beat the baseline or stay silent) — OOS test fold, NOT a win-rate
- FROZEN naive-momentum regime baseline (`frozen_regime_baseline_probs`, fixed constants, never
  fit): maps first-K net buy pressure to ACCUMULATION vs DISTRIBUTION and is **structurally blind
  to sellability collapse** (RUG logit is a fixed floor).  The model's honest edge, if any, is
  exactly the DISTRIBUTION/RUG detection the momentum reader cannot see.
- model macro-OVR-AUC = **0.7762** vs baseline macro-OVR-AUC =
  **0.6895** -> **edge = 0.0867**.
- model multiclass log-loss = 1.1001 vs baseline
  1.7145 (lower is better).
- **HONEST CAVEAT:** these are SYNTHETIC-corpus numbers.  They do NOT establish live edge; they
  prove the baseline-gap machinery is correct and computable.  The binding acceptance gate is
  net-PnL + model-vs-baseline on RECORDED data (GATE-B), never this bootstrap.

## Disable thresholds (auto-disable -> INERT, never risk-up)
- min macro-OVR-AUC edge over baseline per rolling OOS window: **0.02**.
- consecutive no-signal windows to auto-disable: **2**.
- rolling monitor `disabled` on the bootstrap test fold: **False**.
- When the model auto-disables (or a signal is absent/stale), the SLOW loop sets **no de-risk
  flag** — the regime STATE goes **INERT**.  Because the model is de-risk-only, disabling it
  removes an *extra* de-risk layer but can NEVER increase risk; the survivable-stop layers
  (breaker / survivable stop / DMS) remain fully in force.  "No signal -> inert" is the safe default.

## Known failure regimes / honest caveats
- **Synthetic data is not the live distribution** — this card's numbers do not transfer; they prove
  the *pipeline* is leak-free, calibrated, and de-risk-inert, not that the model has live edge.
- The bootstrap regime LABELS come from fixed, never-data-fit classification thresholds
  (M2-CP-02); re-examined against the R1 corpus at M2-CP-03 (a taxonomy-version bump if changed).
- The sellability gate uses a single-pool constant-product exit probe: multi-pool / routed exit,
  MEV-front-run exit, and time-varying honeypot taxes are conservative-by-omission (the gate can
  only be too STRICT, never too loose).
- The bootstrap head is a **deep-temporal-head PLACEHOLDER** (`aats.models.train_regime._fit_regime_head`);
  native temporal-quantile modelling on the `PricePathTensor` is deferred to the R1 corpus.
- Multiclass monotone (de-risk) constraints are not expressible per-class in LightGBM; the
  de-risk-INERT guarantee is enforced STRUCTURALLY by `RegimeDeRiskDirective` + the argmax->
  directive mapping, not by the head.

## Boundaries (non-waivable)
Outputs a multiclass STATE + calibrated probabilities + uncertainty, **never a price, a size, a
win-rate, or a trade decision**.  SLOW-loop only — never on the FAST/SNIPE path.  No execution
code, no keypair, no RPC key, no swap building.  Any downstream use may only **DE-RISK** — never
size up, widen a stop, or override a hard stop (enforced by the type system + the OMS).  Real
capital DISABLED behind DRY-RUN.  **NO capital license until the R1 recorded corpus exists.**
