# AATS — EDGE PROOF (Phase 5) — RUN RESULT · 2026-07-06

**The edge proof was executed on the real recorded data that exists today.**

## Verdict: ⛔ NO-GO — UNPROVEN-NO-REAL-DATA
The bot is **NOT proven** to have an edge. Per the hard rule (*"If NO-GO → stop or pivot. No override, no
exceptions."*) → **stay paper. No real funds.**

## What was run (honestly, no fabrication)
- `compute_gate_a(real_outcomes=[], model=True)` → **FAIL-CLOSED**: `ValueError: empty recorded-trade list.
  GATE-A is undefined with no recorded data — fail closed (no metric), never a fabricated PnL.`
- GATE-B → cannot pass: the min-sample guard rejects 0 records (a positive-looking delta on thin/no data is
  withheld by design).
- **Resolved TradeOutcome records available: 0.** The corpus currently holds launch-moment *snapshots* only; no
  *outcomes* have been resolved (the labeling harness has not been built/run).

## Scoreboard (the ONLY metrics we report — never a win rate)
| Metric | Value |
|---|---|
| Net-of-cost PnL (w/ confidence bound) | **UNDEFINED** (no data — fail-closed) |
| Expectancy / profit factor | **UNDEFINED** |
| Edge vs baseline (walk-forward) | **UNDEFINED** |
| Max drawdown / survivability | **UNDEFINED** |
| Land rate | UNDEFINED (no live trades) |
| Honeypot-rejection rate | machinery built (sell-sim) — not yet measured on a real corpus |
| Detection latency | ✅ measured milliseconds-class |

## Why this is the *correct* result (not a failure of the build)
The gates did their job: they refused to manufacture a number from nothing. This is the exact behavior the whole
architecture was built to guarantee — **an honest "we don't know yet" instead of a comforting lie.**

## The path to a real GO (the only way this verdict changes)
1. **Accrue the corpus** — a persistent detached recorder (PID 21476) is now banking real launches → `C:/aats_shadow`.
   Needs thousands, over time (or a Bitquery archival purchase to get resolved history instantly).
2. **Build the labeling harness** — resolve each recorded launch's forward outcome into a `TradeOutcome` record
   (leak-free, point-in-time). Next build (Claude after session reset, or a Codex work-package).
3. **Re-run GATE-A + GATE-B** on the labeled corpus → a real GO or NO-GO with actual scoreboard numbers.
4. Only a **GO** unlocks Phase 4 go-live build + (with your authorization) Phase 6.

**Honest bottom line:** the edge is unproven, and it may stay NO-GO — that possibility is real and is the entire
reason real capital is gated behind this. The build is done and safe; the *proof* is the remaining work, and it is
data-bound, not code-bound.
