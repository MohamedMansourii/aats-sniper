# Chart-Path REGIME Label Spec (M2-CP-02)

- **Taxonomy version:** `1.0.0` (`REGIME_LABEL_TAXONOMY_VERSION` in `aats/models/regime_labels.py`)
- **Loop:** SLOW only (`REGIME_LABELS_LOOP = "slow"`; reuses `assert_slow_loop_only`). No ONNX
  export, no Rust shim, never FAST/SNIPE.
- **Status:** `is_bootstrap_not_real = True` until the R1 recorded corpus exists. **No regime label
  has earned any capital license.** DRY-RUN untouched.
- **Module:** `aats/models/regime_labels.py` · **Tests:** `tests/models/test_regime_labels.py`

This is the **label** half of the chart-path / regime model. The input tensor is M2-CP-01
(`aats.features.ta.PricePathTensor`). The trained model + frozen `RegimeSignal` contract wiring are
the DATA-BLOCKED M2-CP-03 / M2-CP-04 items and are **not** in this deliverable.

---

## 1. The taxonomy — a multiclass STATE, never a rate or a price

Over the survivor/exit horizon `[decision, resolution]`, a survivor coin is labelled with **exactly
one** of four mutually-exclusive, exhaustive STATES:

| STATE | Meaning (post-hoc, over the horizon) | De-risk action |
|---|---|---|
| `ACCUMULATION` | Still sellable at size; net-positive, holders growing, drawdown controlled — healthy base-building | `NONE` (**INERT**) |
| `NEUTRAL` | Still sellable at size; ranging / undetermined | `NONE` (**INERT**) |
| `DISTRIBUTION` | Still sellable at size, but topping / marked-down / holders shrinking (smart money distributing) | `REDUCE` |
| `RUG_IN_PROGRESS` | Sellability collapsed at decision-relevant size (LP pull / honeypot / drained reserve) | `FORCE_EXIT` |

A regime is a **STATE + (later) a calibrated probability + uncertainty**. It is **never** a
win-rate, a success-rate, a realized multiple, or a price. There is **no** `win_rate` /
`success_rate` / `price` / `size` / `realized_mult` field anywhere on the label row (`RegimeOutcome`)
or in the module (HONESTY CLAUSE, AC-037; verified by test).

### Exhaustive + mutually exclusive
`classify_regime()` is a **priority-ordered** decision with a final `NEUTRAL` fall-through:

1. **not sellable** → `RUG_IN_PROGRESS` (sellability collapse dominates every price/holder reading —
   a "green" candle you cannot exit at size is a trap, not accumulation)
2. distribution signature → `DISTRIBUTION`
3. accumulation signature → `ACCUMULATION`
4. otherwise → `NEUTRAL`

Priority ordering guarantees exactly one STATE per input (mutual exclusivity) and the fall-through
guarantees every input maps somewhere (exhaustiveness). Both are asserted by a grid test.

### Fixed (never data-fit) classification thresholds — versioned with the taxonomy
All are dimensionless RATIOS measured against the decision anchor (never money):

- **DISTRIBUTION** fires if ANY of: net return ≤ `-0.25`; OR (drawdown-from-peak ≥ `0.50` AND net
  return < 0); OR holder-base change ≤ `-0.20`.
- **ACCUMULATION** requires ALL of: net return ≥ `+0.20`; AND holder-base change ≥ `+0.10`; AND
  drawdown-from-peak ≤ `0.40`.

Changing any threshold or STATE is a taxonomy-version bump (a different label ⇒ a different
`model_version` downstream).

---

## 2. The "remains-sellable at decision-relevant size" gate

`remains_sellable_at_size(samples, decision_relevant_size_base, max_slippage_bps)` returns
`(remained_sellable, first_unsellable_slot)`. The coin **remains sellable** iff at **every**
forward outcome step, exiting a decision-relevant position incurs simulated exit slippage
≤ `max_slippage_bps` **and** the sim is well-formed. The first failing step is the sellability-
collapse slot and short-circuits the gate — that is the signature of `RUG_IN_PROGRESS`.

- **Exit sim** (`simulate_exit_slippage_bps`) is a pure constant-product (x·y=k) SELL depth probe —
  the exit-side mirror of `aats.risk.liquidity_sanity.simulate_entry_slippage_bps`. Integer/Decimal
  math throughout; **no float money**. A well-formed sell always realizes strictly below spot; a
  reported `eff_price ≥ spot` means the pool is degenerate/drained → **refuse-by-default** (not
  sellable), never a rosy 0-bps exit out of a rugged pool.
- An **empty** trajectory is refuse-by-default (not sellable): we cannot prove exit liquidity
  existed.
- Money discipline: reserves + `decision_relevant_size_base` are **int** lamports/base units;
  slippage/thresholds are **int** bps; spot price is a **ratio** (float OK per data-models §0).

This is the "sellable = real, non-honeypot, non-rugged exit liquidity existed at decision-relevant
size" gate the label definition requires.

---

## 3. Point-in-time leak-freedom (T-300a) — the whole game

A **label may look forward; a feature never may.** This module enforces the asymmetry by
construction and REUSES the frozen audit primitives:

- **Separate dataset, event-time join only.** Regime labels live in their own dataset and join to
  features by `event_time` (slot + block_time) ONLY — `join_regime_labels_by_event_time` /
  `regime_label_to_joined_example` produce the frozen `JoinedExample` shape; a feature keyed at a
  different `event_time` has no legal join; a feature with no label is dropped (CENSORED-equivalent),
  never defaulted. No wall-clock is ever a join key (`wall_clock_ms` is monitoring-only).
- **Horizon proof.** `RegimeOutcome.__post_init__` asserts `resolution_event_time` is stamped and
  **strictly later** than the decision anchor, and that `resolution.slot == decision.slot + H`
  (mirrors the frozen `LaunchOutcome` §3A invariant).
- **Forward-window gate.** `build_regime_outcome` asserts every outcome sample slot ∈
  `(decision.slot, resolution.slot]` and in strictly increasing order — the label reads ONLY the
  strictly-forward outcome window (plus the decision anchor it measures forward FROM), never a
  decision-time bar, a beyond-horizon bar, or a forward-filled duplicate. Violations raise
  `RegimeLabelLeakError`.
- **Reused leak audit.** `assert_event_time_leq_decision` (unchanged, from `training.py`) runs on the
  regime join and rejects a feature event-time after the decision, or a non-forward resolution.
- **Reused taint guard.** `assert_no_regime_label_taint` delegates to the frozen
  `assert_no_label_taint` (truth_* / `LaunchOutcome` names) and adds the regime-label names — a
  regime STATE column reaching a feature matrix fails the build (`ProvenanceTaintError`).

Co-verified leak-free with `backtest-qa-engineer` (reviewer on this task).

---

## 4. The asymmetric-trust law — accumulation/bullish class provably INERT

The regime STATE maps (total mapping `REGIME_DERISK_ACTION`) to a `RegimeDeRiskAction` whose codomain
is **only** `{NONE, REDUCE, FORCE_EXIT, VETO_ENTRY}`. There is **no** size-up / relax-stop /
delay-exit / add-leverage member — a risk-INCREASING action is **inexpressible by type**.
`ACCUMULATION` and `NEUTRAL` are pinned to `NONE`: a confident bullish prediction carries **zero**
control authority; it can never delay an exit, relax a stop, or size up. Downstream enforcement
(wiring the scalar de-risk flag into `exit_engine` / `rule_engine`) is M2-CP-04's ADR; this module
makes the inertness a property of the taxonomy itself.

---

## 5. Frozen-contract boundary

This deliverable adds a NEW module + spec + tests only. It does **not** edit any frozen contract
(`aats/contracts/*`), the frozen `LABEL_FIELD_NAMES`, `data-models.md`, or the wave workflow. Adding
the typed `RegimeSignal` contract + wiring is M2-CP-04's job (a proper `solana-systems-architect` ADR
under `.agency/02-architecture/adr/`).

---

## 6. Known failure regimes / honest caveats

- The classification thresholds are **fixed engineering constants**, not fit to recorded data (there
  is none yet). They encode a defensible post-hoc definition; they will be re-examined against the R1
  corpus at M2-CP-03 and any change is a taxonomy-version bump.
- The sellability gate uses a **single reference pool** constant-product probe per step. Multi-pool /
  routed exit liquidity, MEV-front-run exit, and time-varying honeypot taxes are **not** modelled
  here — they are conservative-by-omission (a coin that looks sellable on one pool may still be hard
  to exit), so the gate can only be **too strict**, never too loose, on those axes.
- `decision_relevant_size_base` is supplied by the caller (the sizer's notion of a real exit size);
  a wrong (too-small) size understates rug risk. The gate is honest only for a realistic size.
