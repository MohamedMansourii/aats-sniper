# MODEL CARD — FAST Snipe Classifier (T-310)

> **BOOTSTRAP, NOT REAL.** `is_bootstrap_not_real = True`.
> This model is trained on a SYNTHETIC labeled launch dataset (brief 7.2) because recorded
> chain data does not exist yet. It has earned **NO capital license**. The sole
> production-acceptance metric is **net-of-cost PnL + model-vs-baseline on RECORDED data**
> (clean-room harness, T-400/401) — never on this synthetic set. Real capital stays
> **DISABLED behind DRY-RUN** regardless of any number below.

## Identity
- **model_version:** `snipe-lgbm-v1.0.0-seed20260616-BOOTSTRAP`
- **seed:** `20260616` (deterministic; re-train reproduces these metrics bit-for-bit)
- **algorithm:** LightGBM gradient-boosted trees, binary objective (log-loss — a proper
  scoring rule), exported to **ONNX** (run by the Rust hot core on the SNIPE path).
- **output:** calibrated probability + uncertainty ONLY. **No price field. No win-rate
  field. No trade decision.** (data-models §4; locked decision 9; HONESTY CLAUSE.)

## Label (leak-free, co-owned with backtest-qa-engineer)
- Binary: `1 = coin reached the target multiple within the horizon AND remained sellable`
  (non-honeypot, non-rugged exit liquidity existed); `0 = FADED/RUGGED`.
- Lives in a **separate labels dataset**, joined to features **by event_time ONLY**.
- `resolution_event_time = event_time + H` is **stamped and strictly later** than the
  decision anchor (horizon proof). H (synthetic) = 1500 slots (~10 min).
- **Leak audit:** every training row asserts `feature_event_time <= decision_event_time`
  AND `resolution_event_time > decision_event_time`. Result: PASS (see metrics.json).

## Features (25 columns)
On-chain, point-in-time, available inside the first blocks. First-K microstructure
(buy/sell pressure, volume, counts, unique-buyer velocity), LP depth, holder count,
top-10 concentration, sniper-cluster score, sell tax, survivor TA (rsi/macd/bb_width with
presence flags), adversarial selectivity (smart_wallets_in, entry lag), tip-contention
one-hot. **No truth_*/label column can enter the matrix** — `assert_no_label_taint()`
fails the build on any (lineage taint).

### Monotone (de-risk) constraints — adversarial-sentiment rule
These features are constrained **monotone non-increasing** (a higher value can only push
the probability DOWN, never up): `holder_concentration_top10_bps`, `sell_tax_bps`,
`smart_wallets_in`, `smart_wallet_entry_lag_slots`. Whale concentration, honeypot-adjacent
tax, and being *behind* smarter money are RISK signals — the model is forbidden from
learning to treat them as bullish.

## Training window
- corpus n = 4000, positive rate = 0.224
- forward-only, event-time-ordered split (NEVER shuffled): train=2400 /
  calib=600 / test=1000 (the latest event-time window is OOS).

## Calibration (calibration before accuracy)
- method: **isotonic**, fit on the held-out, time-forward
  calibration slice (never the train or test fold).
- **OOS test-fold calibration:** Brier = **0.1436**, ECE = **0.0433**.
- Reliability diagram: `artifacts/reliability_diagram.json` (10 bins).
- Uncertainty = `sqrt(p*(1-p))` (Bernoulli band) — high uncertainty **de-risks** the
  ¼-Kelly sizer (shrinks size); it can NEVER size up.

## Model vs baseline (beat the baseline or stay silent) — OOS test fold
- model AUC = **0.7700** vs baseline AUC = **0.5785**
  -> **edge = +0.1915**.
- model PR-AUC = 0.4610 vs baseline PR-AUC = 0.2567.
- **Auto-disable:** the model-vs-baseline monitor emits **NO_SIGNAL** (no edge traded)
  on any rolling OOS window where `model_AUC - baseline_AUC < 0.02`,
  and auto-disables after 2 consecutive NO_SIGNAL windows. Degraded/shuffled input ->
  NO_SIGNAL (verified by the shuffle-collapse test).

## ONNX parity + latency (measured, not estimated)
- Python-vs-ONNX max abs diff (raw proba) = **2.76e-07**.
- single-row inference: **p50 = 0.0618 ms, p99 = 0.2323 ms**
  (n=3000), well inside the single-digit-ms SNIPE budget.

## Known failure regimes
- **Synthetic data is not the live distribution** — this card's numbers do not transfer;
  they prove the *pipeline* is leak-free and calibrated, not that the model has edge live.
- Edge decay / regime change: the auto-disable monitor is the backstop (emit no signal).
- Class imbalance at low positive rate inflates AUC's apparent stability — PR-AUC and the
  harness's net-PnL are the real bars.
- TA features absent for ~40% of launches; presence flags carry the missingness.

## Disable thresholds
- min edge over baseline (AUC): **0.02**.
- consecutive NO_SIGNAL windows to disable: **2**.
- absent/stale score => SNIPE SKIPs (no block) — "no signal" is the safe default.

## Boundaries (non-waivable)
Outputs probability + uncertainty, never a price. No execution code, no keypair, no RPC
key, no swap building. LLM/slow-model never on this FAST path. Any downstream signal may
only DE-RISK — never size up, widen a stop, or override a hard stop (enforced by the OMS).
