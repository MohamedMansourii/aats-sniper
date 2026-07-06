# CODEX WORK-PACKAGE #3 — the outcome-labeling harness (unblocks the edge proof)

> **This is the single missing piece for Phase 5.** The edge proof (GATE-A/GATE-B) ran and returned NO-GO only
> because there are ZERO resolved `TradeOutcome` records. This harness produces them from recorded launches.
> **Codex builds; Claude runs the dual-G3 gate — and `backtest-qa-engineer`'s LEAK AUDIT is mandatory and
> non-negotiable, because a lookahead leak here would fabricate a false edge (the exact failure that blows up
> real accounts).** Codex must NOT run git; do NOT edit `aats/contracts/`; no secrets.

## The target: produce `TradeOutcome` records the gates already consume
`aats.models.gate_b.TradeOutcome` fields (do NOT change the contract):
`mint`, `decision_slot`, `model_selected` (bool), `baseline_selected` (bool), `net_pnl_lamports` (int),
`sol_at_risk_lamports` (int). GATE-A (`aats/models/gate_a.py:compute_gate_a`) and GATE-B
(`aats/models/gate_b.py:compute_gate_b_delta`) take a list of these and return the verdict.

## Inputs available
- **Recorded launches:** `C:/aats_shadow/snapshots.jsonl` (one genuine launch per line — mint, event_slot,
  event_block_time_ms, first-K-slot microstructure; a persistent recorder is accruing these now). Schema: see
  `aats/ingestion/store.py` (ShadowRecorder / the snapshot format) and `aats/ingestion/shadow_record.py`.
- **Decision pipeline (reuse, do not reimplement):** the snipe classifier (`aats/models/inference.py` /
  `featureset.py`), the frozen naive baseline (`aats/models/baseline.py:evaluate_baseline`), the pre-trade
  safety gate (`aats/risk/pretrade_gate.py`), the cost model (`aats/risk/cost_model.py`), the sizer
  (`aats/risk/sizing.py`), and the exit engine (`aats/risk/exit_engine.py`).

## What to build: `aats/backtest/outcome_harness.py` (+ tests) — a clean-room replay
For each recorded launch:
1. **Decision (STRICTLY point-in-time at `decision_slot`):** assemble the feature frame from ONLY data with
   slot ≤ decision_slot. Run the model → `model_selected` (passes gate + threshold). Run the frozen baseline →
   `baseline_selected`. **No forward data may touch this step — this is the leak boundary.**
2. **Forward outcome:** resolve the launch's price path AFTER decision_slot over the trade horizon, and simulate
   the paper trade the bot would have taken (entry size from the sizer, exit via `exit_engine` + `cost_model`,
   all safety gates applied) → realized `net_pnl_lamports` (net of the ~6% round-trip cost: Jito tip + fees +
   slippage + the adverse-selection floor) and `sol_at_risk_lamports` (integer lamports/Decimal only).
   **Forward price source:** either (a) a forward-fetch per mint (Birdeye/DexScreener/RPC price at horizon
   points — rate-limited on free tier), or (b) preferred — **Bitquery archival OHLCV** (resolved history,
   instant; the operator may provide a Bitquery dataset/key). Make the price source an injected Protocol so
   tests use a fixture and no live network.
3. Emit one `TradeOutcome` per launch. Write them to a parquet/jsonl the edge-proof runner reads.
4. **A tiny runner** (`python -m aats.backtest.run_edge_proof` or a script) that loads the TradeOutcome set,
   calls `compute_gate_a` + `compute_gate_b_delta`, and prints/writes the GATE-A/GATE-B verdict + scoreboard.

## HARD RULES (Claude's backtest-qa gate will fail you on any violation)
- **LEAK-FREE / POINT-IN-TIME (the whole game):** the decision uses only slot ≤ decision_slot; the outcome uses
  only slot > decision_slot; they must be structurally separated (reuse `aats/models/training.py`'s
  `assert_event_time_leq_decision` / `assert_no_label_taint`). A single lookahead = automatic FAIL.
- **NO win-rate** anywhere. Outcomes carry net-PnL/risk only; never a "fraction that won."
- **Purged/embargoed walk-forward** where the gates expect it; money is int lamports/Decimal (no float PnL).
- Reuse the existing decision/exit/cost code — do NOT fork the trading logic (a second copy would drift).

## Acceptance (Claude dual-G3 + a dedicated leak audit)
1. A leak test: injecting a forward feature into the decision step makes a test FAIL (proves the boundary is real).
2. On a fixture corpus (known outcomes), the harness produces correct `TradeOutcome` records and GATE-A/GATE-B
   return the expected verdict; on the empty set, still fail-closed.
3. Full suite stays green; ruff clean; no contract edits; no secrets; no git.
4. Hand back: changed files + commands/output + the leak-test RED-before/GREEN-after proof.

Once this lands (Claude-gated) and the corpus has volume, re-running the edge proof gives the FIRST real
GATE-A/GATE-B verdict with actual scoreboard numbers — GO or NO-GO, honestly measured.
