# ADR-0014 — Frozen `RegimeSignal` contract + SLOW-loop regime/survivor de-risk wiring (reuse the narrative/veto mechanism)

**Status:** Accepted (G1 delta) · **Date:** 2026-07-06 · **Author:** `solana-systems-architect`
**Task:** M2-CP-04 (Wave 3 — chart-path architecture).
**Extends:** ADR-0006 (asymmetric trust by type) · ADR-0008 (three-layer survivable stop) ·
ADR-0010 (typed label dataset / provenance guards). Builds on M2-CP-02 (`aats/models/regime_labels.py`,
the leak-free regime *label* taxonomy) and the (until now orphaned) survivor model
(`aats/models/survivor.py`).

## Context

M2-CP-02 shipped the leak-free, event-time-joined regime **label** taxonomy
(`ACCUMULATION | DISTRIBUTION | RUG_IN_PROGRESS | NEUTRAL`) with a de-risk-or-inert action
codomain, but explicitly deferred the **contract + wiring** to M2-CP-04 (its spec §5: "Adding the
typed `RegimeSignal` contract + wiring is M2-CP-04's job — a proper `solana-systems-architect` ADR").
Separately, the calibrated survivor model (`aats/models/survivor.py`, `SurvivorPrediction` =
P(survive AND sellable) + uncertainty + quantiles) was built but **orphaned** — nothing in the
running control plane consumed it, so its de-risk signal never reached a position.

Two problems to close, once, correctly:

1. There is **no typed output contract** for the SLOW-loop regime model. A model produced later
   (M2-CP-03, data-blocked) needs a frozen shape to emit into — probabilities + uncertainty + a
   STATE, never a price, size, or win-rate.
2. Neither the regime STATE nor the survivor probability is **wired to anything**. They must reach
   execution the **only** legal way a SLOW-loop signal may: as a **pre-set scalar de-risk flag**
   that the FAST/SNIPE loops already read — never as a live model call on the hot path, and never
   as anything that can increase risk.

Constraints that shape the decision:
- The frozen contract layer (`aats/contracts/`) must stay **lightweight** (pydantic only). The
  model-layer taxonomy (`aats/models/regime_labels.py`) transitively imports lightgbm/numpy (via
  `aats/models/survivor.py`), and `aats/models/survivor.py` imports `aats.contracts.models`
  (`MCSScore`). Importing the model-layer enum into `aats.contracts.models` would be a **circular
  import** and would drag lightgbm into every contract consumer.
- The chart-path/regime model and the survivor model are **SLOW-loop only** (BLUEPRINT triple-loop
  boundary; no ONNX/Rust shim; never FAST/SNIPE).
- Asymmetric trust (locked decision 3, ADR-0006): a regime/survivor output may **only** de-risk.
  The bullish/ranging class must be **provably inert** — structurally unable to delay an exit,
  relax a stop, or size up.

## Options

**(A) Put `RegimeSignal` in the model layer / import the model enum into the contract.** Rejected:
circular import (`contracts.models → models.regime_labels → models.survivor → contracts.models`) and
it pollutes the frozen contract layer with lightgbm. A contract must not depend on an ML runtime.

**(B) Invent a NEW StateStore key + a NEW FAST-loop read for the regime de-risk flag.** Rejected as
over-engineering: the FAST loop already reads a de-risk scalar (`narrative_failure`) and the SNIPE
sizing path already reads `veto`. A new hot-path read is new surface area, a new leak/latency vector,
and duplicates an audited mechanism. The task's own brief says reuse "the narrative_failure/veto
mechanism."

**(C) Frozen, self-contained `RegimeSignal` contract in `aats/contracts/models.py` + a SLOW-loop
translator that reuses the EXISTING narrative/veto flags.** Chosen.

## Decision

**Option C.**

1. **Frozen `RegimeSignal` contract** (`aats/contracts/models.py`, exported from `aats.contracts`):
   `mint`, `event_time` (point-in-time anchor), `model_version`, `taxonomy_version`, `regime`
   (argmax STATE), the four calibrated class probabilities `p_accumulation / p_neutral /
   p_distribution / p_rug_in_progress` (a genuine distribution — validator asserts sum == 1 and that
   `regime` is an argmax class), `uncertainty`, `is_bootstrap_not_real`. **No price, no size, no
   win-rate / success-rate / realized-mult field** (HONESTY CLAUSE, AC-037; asserted by a
   field-name scan test).
   - The STATE enum (`RegimeState`) and the de-risk directive enum (`RegimeDeRiskDirective`,
     codomain `{NONE, REDUCE, FORCE_EXIT, VETO_ENTRY}`) are **re-declared self-contained** in the
     contract layer with the **same string values** as the model layer, so contracts stay
     lightweight and circular-import-free. A **consistency test**
     (`tests/contracts/test_regime_signal.py`) fails the build if the contract taxonomy ever drifts
     from `aats.models.regime_labels` (`RegimeLabel` / `RegimeDeRiskAction` / `REGIME_DERISK_ACTION`).
     One taxonomy, two homes, provably in sync.
   - `RegimeDeRiskDirective` carries an **import-time forbidden-name guard** (mirrors
     `ReasoningAction`): `SIZE_UP / WIDEN_STOP / RELAX_STOP / DELAY_EXIT / ADD_LEVERAGE /
     OVERRIDE_HARD_STOP` are inexpressible. `ACCUMULATION` and `NEUTRAL` map to `NONE` (INERT).

2. **SLOW-loop de-risk wiring** (`aats/controller/regime_wiring.py`, `SlowLoopRegimeWiring`): the
   single translator from a regime STATE / survival probability to the **existing** pre-set scalar
   flags — mirroring `SlowLoop._apply_reasoning_verdict` and the E14/E17/E19 enrichment wiring:

   | directive | flag set (existing StateStore method) | who reads the scalar |
   |---|---|---|
   | `NONE` | (nothing — INERT no-op) | — |
   | `REDUCE` | `set_veto_flag(mint, ttl//2)` | SNIPE sizing path |
   | `VETO_ENTRY` | `set_veto_flag(mint)` | SNIPE entry path |
   | `FORCE_EXIT` | `set_narrative_failure_flag(mint)` | FAST loop → `ExitEngine` / `rule_engine` |

   - The survivor translation is **monotone non-increasing in P(survive)**: lower survival ⇒ more
     de-risk (`≤0.15 → FORCE_EXIT`, `≤0.35 or uncertainty ≥0.85 → REDUCE`, else `NONE`). A healthy
     survival read is INERT. Codomain `{NONE, REDUCE, FORCE_EXIT}` — **no size-up branch exists.**
   - SLOW-loop only: every acting method calls the canonical `assert_slow_loop_only`
     (lazily imported to keep the controller import light) — a fast/snipe loop raises loudly.
   - No new StateStore key, no new FAST/SNIPE read, no `EntryIntent`, no size method, no wall-clock.

3. **Control-plane wiring** (`aats/controller/loops.py`): `ControllerOrchestrator` gains an optional
   `regime_wiring` plus `apply_regime_signal` / `apply_survivor_prediction` (event/candidate-driven)
   and `run_slow_regime_tick` (periodic SLOW cadence) — all OFF the FAST/SNIPE hot path, mirroring
   the enrichment wiring exactly. When not injected, every hook is a safe no-op. The
   regime/survivor models are `is_bootstrap_not_real=True` until the R1 corpus exists, so this stays
   **disabled by default (no capital license)**; the de-risk plumbing is proven regardless. DRY_RUN
   untouched.

## Consequences

- (+) A regime/survivor output can reach execution **only** as a de-risk flag. `SIZE_UP` /
  `WIDEN_STOP` / relax-stop / delay-exit are **not expressible values** on this path
  (`RegimeDeRiskDirective`), and `ACCUMULATION`/`NEUTRAL` set **no flag** — the bullish class is
  provably inert (test-proven: no flag written).
- (+) The SNIPE loop reads a pre-computed scalar (`get_veto_flag`) and the FAST loop reads a
  pre-computed scalar (`get_narrative_failure_flag`) — **neither ever consumes `RegimeSignal` or
  calls the model.** No hot-path model call, no new hot-path read.
- (+) Point-in-time preserved: `RegimeSignal` carries `event_time`; the wiring uses the event-time
  slot for audit only and never a wall-clock decision field. The M2-CP-02 label leak audit
  (`assert_event_time_leq_decision` + `assert_no_regime_label_taint`) is unchanged.
- (+) The contract layer stays pydantic-only and circular-import-free; the consistency test prevents
  taxonomy drift.
- (−) The regime STATE enum + de-risk mapping now live in **two** modules (contract law + model
  taxonomy). Accepted, and defended by a build-failing consistency test — the alternative (a
  circular import dragging lightgbm into the contract layer) is worse.
- (−) Survivor de-risk thresholds (0.15 / 0.35 / 0.85) are **fixed engineering constants**, not
  fit to recorded data (there is none). They are conservative and de-risk-only, so they can only be
  too strict, never too loose; they are re-examined against the R1 corpus and any change is a
  visible config edit.

## Delta notice — affected board tasks

This is an **additive** contract change (new `RegimeSignal` / `RegimeState` /
`RegimeDeRiskDirective`; **no existing contract, field, or enum modified**). Per the frozen-contract
change protocol (api-contracts.md §11), the affected/authorized tasks:

- **M2-CP-03** (train the regime model, DATA-BLOCKED): MUST emit the frozen `RegimeSignal` shape as
  its inference output. No other output shape is legal on the SLOW loop.
- **M2-CP-08** (regime model card + retraining harness): reference `RegimeSignal` + `SlowLoopRegimeWiring`
  as the output/consumption seam; document the fixed survivor de-risk thresholds + `is_bootstrap_not_real`.
- **`agent-orchestration-engineer`** (SLOW-loop driver / T-340): may call
  `ControllerOrchestrator.apply_regime_signal` / `run_slow_regime_tick` once a regime model +
  survivor feature source are armed (post-R1). Until then the hooks are no-ops.
- No change to the frozen control-plane wire contract (api-contracts.md §12 endpoint list unchanged),
  the FAST loop, the SNIPE loop, `exit_engine`, or `rule_engine` — they read the SAME pre-set
  `narrative_failure` / `veto` scalars they already read. No downstream lane is broken.
