# ADR-0010 — Typed `LaunchOutcome` label dataset + per-feature provenance/lineage build guards (leak-proofness by construction)

**Status:** Accepted (G1 red-team resolution) · **Date:** 2026-06-16 · **Author:** `solana-systems-architect`
**Extends:** ADR-0004 (clean-room validation harness + `truth_*` guard). Issued with a delta notice
(BLUEPRINT §14).

## Context
The leak-proofness red-team found that the single highest-risk leak surface in meme-coin backtests —
a **forward-looking label** (built from the migration pump or the LP-pull rug at `event_time + H`)
joined into features — was closed only in prose. Specifically: (a) the label had no typed contract, no
dedicated dataset, no horizon-resolution stamp, no construction-time guard (data-models §1 listed no
`labels/` dataset; the label was "computed post-hoc" in prose); (b) the `truth_*` guard was
**name-based**, so a leaked label under an innocuous name (`survived_60s`, `realized_mult`,
`fwd_return`) passes the AST scan; (c) the shifted-clock control (C-5/AC-057) was sold as the
structural clock guarantee but is only necessary-not-sufficient — a uniform global +1-slot shift
preserves the relative label horizon, so a horizon-preserving leak survives it; (d) `recorded_at`
honesty depended on the recorder stamping it truthfully, with no constraint that `recorded_at >=`
knowable-time, leaving a live-backfill lookahead vector.

## Options
1. **Keep the prose label + strengthen the name scan.** Add more forbidden names. Rejected — an
   adversary (or an honest engineer) names the column anything; name-based scanning is unbounded and
   loses by construction.
2. **Runtime leak audit over the joined training matrix.** Detect post-`event_time` columns at score
   time. Better, but it is *policing not prevention* — the leak can be built and only caught if the
   audit happens to fire; it does not make the leak inexpressible.
3. **Typed label dataset + provenance manifest + build-time taint/lineage guards.** Give the label its
   own typed contract (`LaunchOutcome`) in its own event-time-partitioned dataset (`labels/`), produced
   only by the harness, joined to features by `event_time` only, forbidden from `feature_frames/` by a
   disjointness guard. Require each feature to declare a `FeatureSourceWindow` (max source slot +
   lineage dataset); FAIL the build if any window exceeds the cutoff or any lineage touches `labels/`.
   Constrain `recorded_at >= event_time.block_time_ms` at write. Add a per-feature/per-label-horizon
   placebo that catches horizon-preserving leaks the global shift hides.

## Decision
**Option 3.** (data-models.md §3A, §3.3, §9.2; validation-harness.md §2.5, §3, C-5.)
- `LaunchOutcome` is a typed contract in a dedicated `labels/` dataset: `mint`, `event_time` (decision
  anchor), `label_horizon_h_slots`, **stamped** `resolution_event_time` (= anchor + H), label value,
  `resolution_recorded_at_ms`. Construction asserts `resolution_event_time` strictly later than the
  anchor and `resolution_recorded_at >= resolution_event_time.block_time`.
- Four build/load guards (validation-harness.md §2.5): per-feature cutoff
  (`feature_window_exceeds_cutoff`), **lineage taint** against `labels/`
  (`feature_lineage_touches_label`) — the PRIMARY defense, replacing name-scan as primary — label/feature
  column disjointness (`label_column_in_feature_frame`), and `recorded_at` honesty
  (`recorded_at_before_knowable` / `backfill_recorded_at_regression`). The `truth_*` AST scan is
  retained, explicitly demoted to belt-and-suspenders.
- The C-5 global clock-shift control is **relabeled necessary-not-sufficient**; an independent
  label-horizon + per-feature-lineage placebo is the sufficient complement (a horizon-preserving leak
  surfaces because the perturbation is per-feature/per-label, not global).

## Consequences
- (+) A forward-looking label has no legal home in a feature frame — the leak is inexpressible, not
  merely policed. An innocuously-named leaked label is caught by lineage, not by name.
- (+) The live-backfill lookahead vector is closed: there is no honest way to back-date `recorded_at`.
- (+) Horizon-preserving leaks invisible to a global clock shift are now caught by the per-feature /
  per-label-horizon placebo.
- (−) Every feature pipeline must emit a `FeatureProvenance` manifest (one window per feature) — real
  transcription work for M1 (T-304/305) and the harness (T-400/401). Stated in the delta notice. The
  cost of point-in-time correctness by construction; accepted.
- (−) The harness owns a new writer (`labels/`) and four new build/load guards (T-400/401).
