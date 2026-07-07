# AATS — EDGE PROOF: MOMENTUM ENTRY @60s — DECISIVE RUN (n=4,187) · 2026-07-06

**Supersedes the promising n=497 result. On 8× more data, the GATE-B PASS REVERSED — the momentum edge was a
small-sample fluke. Decisive verdict: NO-GO.** (Enabled by the new parallel+cached block_time resolver — 4,193
records resolved in minutes.)

## Verdict: ⛔ NO-GO (decisive). Real capital stays disabled.

## The reversal (why rigor matters)
| Metric | n=497 (first) | **n=4,187 (decisive)** |
|---|---|---|
| GATE-B delta (model − baseline, net-PnL/SOL-risk) | **+0.041** (lower95 +0.026) ✅ | **−0.011** (lower95 −0.060) ❌ |
| model selected | 2 | 5 → total net **−0.11 SOL** (losing) |
| baseline selected | 58 (losing) | 224 → +4.58 SOL mean but lower95 **negative** (not significant) |
| GATE-A model | marginal | **FAIL** (−0.219/SOL, lower95 negative) |

**The model no longer beats the naive baseline — it is worse, and its own trades lose money.** The n=497 GATE-B PASS
did not survive more data. This is the edge proof working: a false positive killed by a larger sample.

## What this means, honestly
- **Both on-chain-launch strategies are now decisively NO-GO:** launch-winner prediction (earlier) AND
  momentum/reaction-entry-@60s (here). On 4,000+ real launches, neither beats a dumb rule net-of-cost.
- This strongly matches the honest thesis established at the start: **predicting or timing meme-coin outcomes from
  on-chain launch data alone has no durable edge** (~95% rug; the cost gate is brutal).
- The bot's *stated* alpha thesis — front-run predictable retail reaction to **proven KOL calls / smart-money buys**
  — is a DIFFERENT strategy needing a DIFFERENT dataset (social + smart-money signal→reaction events), which the
  launch corpus does not contain and which has NOT been tested.

## Caveats (do not use to rescue the verdict)
DexScreener sparsity (645/4,187 unpriced@60s) and ~64s timing drift remain. But the model at n=4,187 is *clearly*
worse than baseline (not marginal), so better entry-price fidelity would not plausibly flip a −0.011 delta to a
significant positive. The signal is genuinely absent, not merely noisy.

## The honest pivot (per NO-GO → stop or pivot)
The launch-data edge is falsified. The remaining honest options, in order of value:
1. **Test the REAL alpha thesis** — stand up recording of KOL-call / smart-money-buy → reaction events and edge-prove
   front-running them net-of-cost. This is a major, separate data+research effort (the bot has the components:
   caller-score, smart-money, Telethon). ← the only lever with a real prior for an edge.
2. Accept that a solo on-chain-launch sniper has no proven edge (consistent with "only ~10% of bots profit"), and
   keep the system as a proven-safe paper platform.

**Bottom line:** rigorous, decisive **NO-GO** on the launch-data strategies. Capital disabled. The one untested thesis
with a real prior is the social/smart-money reaction edge — a deliberate, larger effort, not a quick re-run.
