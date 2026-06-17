# ADR-0003 — Promote (not rewrite) the sol-sniper seams; ExecutionVenue seam is law; live-verified venue/program-ID registry

**Status:** Accepted (G1) · **Date:** 2026-06-16 · **Author:** `solana-systems-architect`

## Context
A validated stdlib-only M4 sim exists at `sol-sniper/sniper_sim/` (venue/safety/tips/exits/amm/types/
metrics). The brief is explicit: **extend it, do not rewrite** (BRIEF §7.1). The most dangerous
Solana sniper bug is a stale hardcoded program ID in a hot-path decoder — pump.fun now migrates to
PumpSwap (A-001) and IDs rotate.

## Options
1. **Rewrite clean** — ignore the sim, design fresh. Discards validated contracts; violates the brief;
   risks re-introducing solved problems.
2. **Promote the seams** — treat `ExecutionVenue.execute(intent,event)->FillResult`, the `SafetyGate`
   ordering, `TipStrategy` edge-bound, `ExitEngine` ladder/trailing/hard-stop, and the `Metrics`
   scorecard as the starting production contracts; productionize against them unchanged where the
   signature stands. Program IDs become **live-verified registry data**, never literals.

## Decision
**Promote the seams.** The `ExecutionVenue` interface is LAW (loop core imports the ABC, never a
concrete venue). `JitoJupiterVenue`/`RaydiumVenue`/`DeadCcxtVenue` drop in behind it; `SimulationVenue`
is retained. `TipStrategy`→ live `tip_stream`-backed TipController (edge-bound preserved). `SafetyGate`
checks 1–5 become real 0-RPC decodes; **`safety.py:43`'s `truth_*` read is DELETED** in production
(recall measured, not parametrized — C-7). `metrics.py`→ Prometheus telemetry. Program IDs + AMM fees
live in a **pluggable registry verified LIVE at startup**; a build-time guard FAILS on any hardcoded
program ID or Jito tip in a hot-path file.

## Consequences
- (+) Validated contracts carried forward; the brief honored; the seam stays swap-in for real venues.
- (+) Stale-ID hot-path bug structurally prevented (registry + startup probe + build guard).
- (−) The sim's `float` money and `truth_*` ground truth must be explicitly stripped on promotion
  (money→integer/Decimal; truth→clean-room labels). This is called out per-artifact in BLUEPRINT §3.
- (−) Two AMM fee sources (registry) to keep current; mitigated by the startup verify probe.
