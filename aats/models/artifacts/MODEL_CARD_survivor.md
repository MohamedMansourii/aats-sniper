# MODEL CARD — Slow-loop SURVIVOR model + GATE-B monitor (T-312)

> **BOOTSTRAP, NOT REAL.** `is_bootstrap_not_real = True`.
> Trained on the SYNTHETIC labeled launch dataset (brief 7.2) because recorded chain data
> does not exist yet (T-400/401). It has earned **NO capital license**. The sole
> production-acceptance metric is **net-of-cost PnL + model-vs-baseline on RECORDED data**
> — never on this synthetic set. Real capital stays **DISABLED behind DRY-RUN** regardless
> of any number below. The TASK is explicit: with no recorded data, the **architecture +
> the GATE-B monitor** are what matter; heavy TFT/N-HiTS tuning is **deferred** to T-400/401.

## What this delivers

1. **Slow-loop survivor model** (`survivor.py`) — for coins that already survived the snipe
   window. Consumes MCS + first-60s survivor microstructure as exogenous covariates; emits
   **calibrated P(survive) + uncertainty + quantiles (p10/p50/p90) + a volatility estimate**.
   SLOW-loop only (never FAST/SNIPE). A pragmatic, monotone-constrained GBT survivor head +
   isotonic calibrator behind a `predict() -> SurvivorPrediction` contract a TFT/N-HiTS can
   be slotted into later without changing the SLOW loop, the monitor, or the telemetry
   (seam: `TFT_SWAP_POINT`).
2. **GATE-B monitor** (`gate_b.py`) — the **headline honest metric**: the model-vs-naive-
   baseline **net-of-cost PnL per unit-of-risk DELTA** on recorded data, with a lower-95%
   bootstrap bound, emitted to telemetry `aats_model_vs_baseline_delta_net_pnl_per_sol`
   (AC-037). **NOT a win-rate.** Computable on the harness's recorded `TradeOutcome` list at G4.

## Survivor model — identity
- **model_version:** `survivor-lgbm-v1.0.0-seed20260616-BOOTSTRAP`
- **seed:** `20260616` (deterministic; re-train reproduces predictions bit-for-bit).
- **algorithm:** LightGBM survival head, binary objective (log-loss — proper scoring rule),
  isotonic calibrator, residual-dispersion quantile estimator. **No ONNX export, no Rust
  shim** — by design, so it cannot be dropped into the snipe race core.
- **loop:** `slow` — `assert_slow_loop_only()` raises `SurvivorLoopViolation` on any fast/snipe call.
- **output:** calibrated probability + uncertainty + ordered quantiles + vol ONLY.
  **No price field. No win-rate field. No trade decision.** (data-models §4; locked decision 9.)

## Survivor — covariates (23 columns)
First-60s survivor microstructure (LP depth, holder count, top-10 concentration, sell tax,
sniper-cluster, first-K buy pressure, unique buyers), survivor TA (rsi/macd/bb_width +
presence flags), adversarial selectivity (smart_wallets_in, entry lag), and **MCS exogenous
covariates** (conviction, momentum, novelty, synchronicity, account-age, coordinated-shill
flag, `mcs_present`). MCS is optional — neutral sentinels + `mcs_present=0` when a coin has
no social coverage (never fabricated sentiment). **No truth_*/label column can enter** —
`assert_no_label_taint()` fails the build on any (lineage taint).

### Monotone (de-risk) constraints — adversarial-sentiment rule (verified by sign test)
Pinned **monotone non-increasing** (a higher value can only push P(survive) DOWN):
`holder_concentration_top10_bps`, `sell_tax_bps`, `smart_wallets_in`,
`smart_wallet_entry_lag_slots`, **`mcs_synchronicity`**, **`mcs_coordinated_shill_flag`**.
Manufactured hype and being *behind* smarter money are CONTRARIAN RISK signals — the model
is forbidden from learning to treat them as bullish. `mcs_account_age_median_days` is the
only non-decreasing covariate (older crowd => less manufactured hype) and still never
*triggers* an entry. The sign test FAILS the build if any sign is wrong.

## Survivor — leak-free label + training window
- Same leak-free construction as the snipe classifier: labels in a **separate dataset**,
  joined **by event_time ONLY**; `resolution_event_time = event_time + H` stamped and
  strictly later (horizon proof). **Leak audit:** every row asserts
  `feature_event_time <= decision_event_time` AND `resolution_event_time > decision_event_time`.
  Result: **PASS** (3000 rows).
- corpus n=3000, positive rate ≈ 0.226; forward-only event-time split (NEVER shuffled):
  train=1800 / calib=450 / test=750 (latest event-time window is OOS).

## Survivor — calibration (calibration before accuracy)
- method: **isotonic**, fit on the held-out time-forward calibration slice.
- **OOS test-fold:** Brier = **0.1510**, ECE = **0.0328** (within tolerance 0.25 / 0.10).
- Uncertainty = blend of the Bernoulli band and the calibration-evidence band; quantile
  spread widens with per-prediction Bernoulli variance — high uncertainty / wide spread
  **de-risks** the ¼-Kelly sizer (shrinks size); it can NEVER size up.

## Survivor — model vs baseline (beat the baseline or stay silent) — OOS test fold
- model AUC = **0.7354** vs baseline AUC = **0.5864** → **edge = +0.1490**.
- model PR-AUC = **0.4076** vs baseline PR-AUC = **0.2587**.

## GATE-B monitor — the headline NET-PnL delta (NOT a win-rate)
- **metric:** `delta = model_net_pnl_per_unit_risk − baseline_net_pnl_per_unit_risk` on
  recorded `TradeOutcome` records (net-of-cost PnL, per SOL-at-risk; a `/downside-deviation`
  Sortino-style variant is also provided). A declined trade contributes **0** (a skip is a
  costless real outcome — the model gets credit for the loss it avoided).
- **pass bar:** `gate_b_pass` iff the **lower 95% bootstrap bound > 0** (a point estimate is
  not enough — EDGE-VERDICT §4). Seeded, deterministic paired bootstrap.
- **emitted to telemetry:** `aats_model_vs_baseline_delta_net_pnl_per_sol` via an injectable
  sink (`emit_gate_b_to_telemetry`) — the real `AATSMetrics` gauge plugs in unchanged.
- **fail closed:** empty record set → `ValueError` (no fabricated delta); a model that loses
  to the baseline → delta ≤ 0, `gate_b_pass = False` (de-scope to baseline / halt model-
  driven entries — K-1). It never sizes, never overrides a stop, never lands a transaction.
- **fixture verification:** model takes 10 winners (+0.5 SOL @ 0.1 SOL risk) and skips 10
  losers (−0.4 SOL @ 0.1 SOL risk) the baseline takes → model=+2.5, baseline=+0.5,
  **delta=+2.0, lower95=+1.2 → PASS** (hand-checkable, deterministic).
- **NO win-rate** field, target, or tuning objective anywhere (HONESTY CLAUSE, binding;
  asserted by tests on both `GateBResult` and `TradeOutcome`).

## Relationship to the T-310 AUC monitor (`monitor.py`)
Two distinct controls against the SAME frozen baseline:
- `monitor.py` (T-310): lightweight, continuous **ranking-quality (AUC) auto-disable** — emits
  "no signal" the moment ranking edge decays; runs on every rolling window.
- `gate_b.py` (T-312): heavyweight **net-PnL-per-risk delta** with a bootstrap bound — the
  Grafana/dashboard headline acceptance number on recorded fills.

## Known failure regimes
- **Synthetic data is not the live distribution** — these numbers prove the *architecture is
  leak-free, calibrated, and the GATE-B metric is correct*, NOT that the model has live edge.
- Edge decay / regime change: the AUC auto-disable (T-310) and a failing GATE-B (de-scope to
  baseline, K-1) are the backstops.
- MCS coverage is partial; `mcs_present` carries the missingness so absent social data is a
  split, not a fabricated sentiment.
- The pragmatic survivor head is a TFT/N-HiTS **placeholder**; native temporal-quantile
  tuning is deferred to recorded data (T-400/401) at `TFT_SWAP_POINT`.

## Boundaries (non-waivable)
Outputs probability + uncertainty + quantiles, never a price. SLOW-loop only — never on the
FAST/SNIPE path. No execution code, no keypair, no RPC key, no swap building. Money is integer
lamports / Decimal, never float. Any downstream signal may only DE-RISK — never size up, widen
a stop, or override a hard stop (enforced by the OMS). Real capital DISABLED behind DRY-RUN.
