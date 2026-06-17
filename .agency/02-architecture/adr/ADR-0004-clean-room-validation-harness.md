# ADR-0004 — Clean-room validation harness with a build-failing `truth_*` import guard (C-7)

**Status:** Accepted (G1) · **Date:** 2026-06-16 · **Author:** `solana-systems-architect`

## Context
The sim is admittedly circular (red-team 2D): `safety.py:43` reads `truth_is_rug`/`truth_rug_detectable`
at an assumed `catch_rate=0.75`; net PnL is monotone in `model_skill`; `_competitor_delay()` and the
SECURE/FAST exit constants are shared optimistic knobs. The genuine new risk is that scaffolding
**leaks into the recorded-data harness**, making GATE-A/GATE-B prove nothing. EDGE-VERDICT condition
C-7 demands a clean-room rebuild with a static guard; the brief forbids inheriting the optimistic
constants (C-2).

## Options
1. **Reuse the sim harness for recorded data** — fastest, but the circular constants and `truth_*`
   reads would silently leak; recall would be a parameter, not a measurement. Rejected: this is the
   exact failure C-7 exists to prevent.
2. **Clean-room package + build-time import guard** — a separate validation package that imports the
   point-in-time store and production feature/cost code and **nothing from `sniper_sim/`**; an AST +
   import-graph guard FAILS the build on any `truth_*` reference, any inherited sim constant, any
   `catch_rate`/recall-as-parameter, or any hardcoded program-ID/tip literal.

## Decision
**Option 2.** Recall ≥ 0.50 is a **measured output** of held-out labeled rugs in test folds, never an
input. The recorded cost stack derives `buyers_ahead` and the dump/sandwich haircut from observed
data, calibrated upward, never from the sim's `_competitor_delay`/sandwich constants. The experiment
log is a hashed precondition (C-9); the baseline is frozen (C-4); the haircut is train-fold-frozen
(C-5). All of it is wired in `validation-harness.md`.

## Consequences
- (+) Lookahead and inherited-optimism leakage become **build failures**, not review judgment calls.
- (+) The two controls that matter (breaker, model-vs-baseline) sit on a leak-audited foundation.
- (−) A second package and a CI guard to maintain; the cost of honesty. Accepted.
- (−) The harness cannot share convenience code with the sim; some duplication is deliberate (the
  separation IS the guarantee).
