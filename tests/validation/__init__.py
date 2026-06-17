"""Clean-room recorded-data validation harness (T-401).

CLEAN-ROOM GUARANTEE (validation-harness.md §1-2, C-7)
======================================================
This package imports ONLY the point-in-time store + production feature/cost/model code
and NOTHING from `sniper_sim/`. It references no `truth_*` field and inherits no sim
constant (`_competitor_delay`, SECURE/FAST sandwich constants, `generate_path`). The
import-graph / AST guard in `test_clean_room_import_guard.py` enforces this at test time:
a `truth_*` reference or a `sniper_sim` import anywhere in this package's dependency
closure FAILS the build.

WHAT THIS HARNESS DOES (T-401 scope)
====================================
1. Turns a (bootstrap, synthetic) labeled launch corpus into `TradeOutcome` records via:
   - the FROZEN naive-momentum baseline selection (C-4, baseline.py), and
   - a model selection (a trained snipe classifier OR an injected control selector), and
   - a NET-OF-COST PnL resolver that deducts the full round-trip cost stack every trade.
2. Splits the corpus into >=1 forward-only, event-time-ordered, PURGED + EMBARGOED test
   windows (drop train where event_time+H >= test_window_start; embargo >= H after each).
3. Runs GATE-A (compute_gate_a) and GATE-B (compute_gate_b_delta) on the resolved
   TradeOutcome records, with a model-WINS control and a model-LOSES control.

HONESTY (non-waivable)
======================
The ONLY corpus available in this offline build is `generate_synthetic_corpus`, stamped
`IS_BOOTSTRAP_NOT_REAL = True`. NO live edge can be or is proven here. Every number this
harness produces on the bootstrap corpus is labeled is_bootstrap_not_real and DOES NOT
license capital. Real capital stays DISABLED behind DRY-RUN. The full >=5-window CPCV with
the C-5 global-clock-shift control, C-10 group-purge, and the SimulationVenue depth-based
cost burn-in are larger deliverables tracked separately; this package proves the GATE-A /
GATE-B harness is BUILT and COMPUTES CORRECTLY on the available corpus, which is the
T-401 confirmation deliverable.
"""
