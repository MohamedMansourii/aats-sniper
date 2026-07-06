# AATS — EDGE PROOF: MOMENTUM/REACTION ENTRY (60s) · 2026-07-06

**First test of a plausibly-edged strategy (not launch-prediction). First GATE-B PASS in the project — but the
overall verdict is still NO-GO, honestly, because GATE-A fails on too-thin a selected sample.**

## Verdict: ⛔ NO-GO — but with a PROMISING signal (do not overclaim)
Real capital stays disabled. This is *encouraging evidence*, not a proven edge.

## The run
- Strategy: momentum entry at **T_ENTRY=60s** (decide on ≤60s price move + buy/sell pressure; hold to a later exit).
  Leak-safe (dual-G3 PASS; backtest-qa refuted lookahead — mutating all >60s marks changed 0 decisions).
- Corpus: 497 launches, resolved 497/497 (block_time via RPC). Tradeable at 60s: 404; skipped-untradeable: 93.
- Selected: **model=2**, baseline=58.

## Scoreboard (real measured numbers)
| Gate | Metric | Value | Result |
|---|---|---|---|
| **GATE-B** (model vs baseline) | net-PnL/SOL-risk delta | **+0.0409** (lower95 **+0.0257 > 0**) | **✅ PASS** |
| GATE-A [model] | net-PnL lower-95% | mean +67K lamports but selected only **2** trades → lower95 −70K | ❌ FAIL |
| GATE-A [baseline] | net-PnL lower-95% | **−4.03M lamports/trade** (naive momentum LOSES money, −0.345/SOL) | ❌ FAIL |

## What this means, honestly
1. **The naive momentum baseline loses money** net of cost (−0.345/SOL) — buying early-momentum tokens blindly is
   a losing strategy. That is the honest, realistic result (and better-calibrated than the launch strategy's
   optimism, since here even the baseline is correctly negative).
2. **The momentum MODEL genuinely beats that baseline (GATE-B PASS).** Its tight selectivity (2/497) avoids the
   losers the baseline eats. This is the **first real evidence that a model adds selection value** — the thing the
   whole project is trying to prove.
3. **But it is NOT a proven edge (GATE-A FAIL).** The model traded only twice — far too few to establish positive
   expectancy with confidence. A 2-trade positive is indistinguishable from luck.

## Caveats (do not ignore)
- **Timing drift:** the "60s" decision mark actually samples ~64s median (collector v2 obs-time drift) — a mild
  latency-optimism; a strict-60s live bot would see slightly less state.
- **DexScreener sparsity:** 93/497 unpriced at 60s (indexing lag), so the tradeable set is filtered.
- **Small n + horizon-compressed exit walk** (reviewer notes) — coarse exit fidelity.

## The path (this verdict can plausibly move — unlike the launch strategy)
1. **Accrue more corpus** (collector v2 running) — with thousands of launches the model selects more than 2 trades;
   GATE-A can then become statistically testable while GATE-B is re-checked.
2. **Improve entry-price fidelity** (bonding-curve price at exactly 60s vs DexScreener) to kill the drift + sparsity.
3. **Re-run.** If GATE-B holds AND GATE-A turns significantly positive on a larger selected sample → a real GO
   candidate (then, and only then, security-gated live staging).

**Bottom line:** the first honest *hint* of an edge — the model beats a (losing) naive baseline — but far from
proven. NO-GO stands; capital disabled; more data is the next input.
