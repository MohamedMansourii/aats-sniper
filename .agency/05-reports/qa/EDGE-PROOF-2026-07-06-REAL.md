# AATS — EDGE PROOF (Phase 5) — FIRST REAL-DATA RUN · 2026-07-06

**Supersedes the earlier same-day fail-closed placeholder.** GATE-A/GATE-B ran on **real recorded launches with
resolved outcomes** for the first time. This is a genuine verdict with a real scoreboard — not "no data".

## Verdict: ⛔ NO-GO (on real data)
Per the hard rule (*NO-GO → stop or pivot, no override*) → **stay paper, no real funds.**

## The run
- Corpus: 400 labeled launches (from the live collector `labeled_corpus.jsonl`), **resolved=400, censored=0** —
  every entry's on-chain `block_time` resolved via `getTransaction` (point-in-time anchor; T-300a honored).
- Selected: model=120, baseline=187.

## Scoreboard (REAL measured numbers — never a win rate)
| Gate | Metric | Value | Result |
|---|---|---|---|
| GATE-A [model] | net-PnL lower-95% bound | mean +31.1M lamports/trade but **lower95 = −4.52M lamports** | **FAIL** |
| GATE-A [baseline] | net-PnL lower-95% bound | mean +61.9M lamports/trade but **lower95 = −5.32M lamports** | **FAIL** |
| GATE-B [model vs baseline] | net-PnL per SOL-at-risk delta | model +0.311 − baseline +0.619 = **−0.307** (lower95 −0.987) | **FAIL** |
| — | model net-PnL / SOL-risk | +1.038 | — |
| — | baseline net-PnL / SOL-risk | +1.323 | — |

**Two honest takeaways:** (1) the thin-feature **model does NOT beat the naive baseline** — it is *worse* (GATE-B);
(2) neither model nor baseline has a **statistically-positive** net-of-cost edge at 95% (GATE-A lower bounds are
negative) — the positive point-estimates are not distinguishable from luck at this sample size / fidelity.

## Caveats — why this is a PRELIMINARY NO-GO, not the definitive verdict
This proof is honest but **thin**, and the caveats matter:
1. **Thin decision features.** The "model" uses only the launch-instant PumpPortal create fields (initial
   reserves, initial buy, creator) — NOT the first-60s microstructure (holder concentration, buy/sell pressure,
   sniper clusters, smart-money) that the full alpha thesis relies on. The collector doesn't capture those yet.
2. **Coarse outcome fidelity.** Outcomes come from DexScreener spot prices at 1m/5m/15m, walked through the exit
   engine at a compressed step (reviewer P2). Real sellability, slippage, and honeypot behavior are not fully
   modeled — the *positive means* are likely optimistic (spot price ≠ realizable exit for fresh tokens).
3. **Small sample.** n=400 (the collector is still accruing toward thousands); wider n tightens the bounds.

## Interpretation
The result is exactly what a rigorous gate should produce on thin data: **it refuses to certify an edge, and it
shows the simple model isn't beating the dumb rule.** That's the honest state — the bot is NOT proven, on real
data. It does not mean no edge exists; it means the *thin-feature, coarse-fidelity* version has none, and the
richer thesis is untested.

## Path to a definitive verdict
1. **Enrich the collector** to capture first-60s microstructure per launch (the real features).
2. **Improve outcome fidelity** — model realizable exit (bonding-curve/AMM sell sim, slippage, honeypot) not spot.
3. **Accrue thousands** (running) or buy Bitquery archival for instant scale + resolved OHLCV.
4. **Re-run GATE-A/GATE-B.** Only a GO (model beats baseline + statistically-positive net-of-cost) lifts anything.

**Bottom line:** Phase 5 now yields a real, honest **NO-GO** on real data — the gate works, the pipeline works, and
the bot's current thin-feature edge is unproven (in fact negative vs baseline). Real capital stays disabled.
