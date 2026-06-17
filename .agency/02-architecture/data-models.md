# data-models.md — Typed Message Contracts + Point-in-Time Feature Store (T-203)

**Version:** 1.1.0 — **FROZEN** (post-G1-red-team; ADR-0010 added the `LaunchOutcome` label dataset
§3A, the `FeatureProvenance` manifest §3.3, and the `recorded_at` honesty constraint §9.2).
**Author:** `solana-systems-architect`
**Date:** 2026-06-16
**Status:** These schemas are the law engineers transcribe directly. Pydantic (Python) and matching
serde Rust structs are derived from the same field list so the Redis-Streams wire is identical on
both sides of the boundary. After G1, only the `solana-systems-architect` changes a contract (ADR +
delta notice).

**Companion:** `BLUEPRINT.md` (topology), `api-contracts.md` (the read-only projection of these onto
the wire), `validation-harness.md` (how the store prevents leakage), `execution-venue.md` (Intent →
venue).

---

## 0. Money rule (applies to EVERY field below)

**No `float` for any monetary value, ever** (NFR-009, FR-042, ROSTER §5.3). On the wire and in
storage:
- **SOL amounts → integer lamports** (`LAMPORTS_PER_SOL = 1_000_000_000`).
- **Token amounts → integer base units** (per-mint decimals; store the raw integer + the decimals).
- **Fees / tips / priority → integer lamports.**
- **Derived ratios that must be exact (PnL, bps) → `Decimal`** (Python `decimal.Decimal`; Rust
  `rust_decimal::Decimal`), serialized as a **decimal string** on the wire.
- Probabilities, uncertainties, latency ms, and feature values that are genuinely real-valued may be
  `float` — they are not money. The rule is precise: *money and money-derived exact quantities are
  integer/Decimal; statistical quantities are float.*

The sim's `float` reserves (`sol_reserve`, `token_reserve` in `sniper_sim/types.py`) are **sim-only**.
Production reserves are integer base units. Engineers transcribing from the sim MUST convert.

---

## 1. Entity catalog (what crosses a loop boundary or is persisted)

| Entity | Produced by | Consumed by | Persisted | Retention |
|---|---|---|---|---|
| `LaunchEvent` | Rust ingest | SNIPE, SLOW | Parquet history (raw) | append-only, indefinite (training corpus) |
| `FeatureFrame` | SLOW (feature pipeline) | classifier, control plane, recorder | Redis hot (TTL) + Parquet history | hot: TTL 5 min; history: indefinite |
| `LaunchOutcome` (the **label**, §3A) | clean-room harness ONLY (post-hoc) | walk-forward training/scoring ONLY | Parquet history `labels/` (event-time partitioned) — **NEVER Redis hot, NEVER `feature_frames/`** | append-only, indefinite |
| `Prediction` (DecisionSignal) | SLOW (model inference) | SNIPE (via KV pre-stage), control plane | Redis KV (pre-staged) + Parquet | KV: TTL ≤ slow-cadence; history: indefinite |
| `MCSScore` | SLOW (NLP) | SLOW reasoning, control plane | Redis hot + Parquet | hot: TTL 10 min; history: indefinite |
| `ReasoningVerdict` | SLOW (LLM Reasoner) | SLOW → KV veto flag, control plane | Parquet (audit) | indefinite |
| `Intent` | SNIPE / FAST | ExecutionVenue | Redis stream `intents` + Parquet | MAXLEN + history indefinite |
| `FillResult` | venue adapter | FAST, recorder | Redis stream `fills` + Parquet | history indefinite |
| `Position` (+ FSM) | FAST (single writer) | control plane, ExitEngine | Redis KV (live) + Parquet (closed) | KV: while open; history: indefinite |
| `RiskConfig` | control plane | risk engine | Redis KV | current only (versioned in Parquet on change) |
| `BreakerState` | risk engine (FAST) | all loops, control plane | Redis KV | current only |

---

## 2. `LaunchEvent` (detected liquidity event — point-in-time)

Promoted from `sniper_sim/types.py` with the **`truth_*` block removed** (production never observes
ground truth; the clean-room harness computes labels separately, C-7).

```python
class DetectionTransport(str, Enum):
    GEYSER = "geyser"
    SHREDSTREAM = "shredstream"

class LaunchSource(str, Enum):
    PUMPFUN = "pump.fun"
    PUMPSWAP = "pumpswap"
    RAYDIUM_V4 = "raydium_v4"
    RAYDIUM_CPMM = "raydium_cpmm"
    MIGRATION = "migration"

class EventTime(BaseModel):
    slot: int                       # canonical decision slot (LP-add / migration slot)
    block_time_ms: int              # on-chain block_time (ms) — the AUTHORITATIVE clock (C-5)
    wall_clock_ms: int              # node wall-clock at decode (for monitoring only, NOT for joins)

class LaunchEvent(BaseModel):
    mint: str
    venue_program_id: str           # the LIVE-verified program ID used to decode (registry, FR-001)
    source: LaunchSource
    event_time: EventTime           # ALL point-in-time joins anchor on event_time.slot/block_time
    observation_slot: int           # may be < confirmation_slot when ShredStream (AC-003)
    confirmation_slot: int
    detection_transport: DetectionTransport

    sol_reserve_lamports: int       # integer base units (was float in sim)
    token_reserve_base: int         # integer base units
    token_decimals: int
    initial_holders: int
    competitors: int                # other bots observed racing (point-in-time estimate)

    # actor-identity fingerprints — REQUIRED for group-aware purge (C-10, FR-046)
    creator_wallet: str
    bundler_cluster_id: str | None
    deploy_template_fingerprint: str | None

    data_staleness_ms: int          # age of the freshest source datum vs wall_clock at decode (FR-057)
```

**Rust serde struct** mirrors every field (snake_case, `u64` for lamports/base units, `i64` slots).
The Redis-Streams payload is the JSON of this model; the Rust ingest writes it, Python reads the
identical shape.

> **No `truth_is_rug`, `truth_max_multiple`, `truth_rug_detectable`.** Those exist only in the sim.
> The production label is computed post-hoc in the clean-room harness from observed on-chain outcomes
> at `event_time + H`, never carried on the live event (C-7; the import guard FAILS the build if any
> production/validation path references a `truth_*` symbol).

---

## 3. `FeatureFrame` (event-time-stamped sensor output)

The single most leak-sensitive contract. **Every feature value is reconstructable from data with
observation-time ≤ `event_time`** (walk-forward §1; FR-005/010/012). The frame carries its own
event-time so a Parquet row can never be joined on compute-time.

```python
class FeatureFrame(BaseModel):
    mint: str
    event_time: EventTime           # the anchor — copied from the LaunchEvent (C-5)
    feature_schema_version: str     # bump on any feature add/change (frozen-baseline safety, C-4)
    data_staleness_ms: int          # max staleness across the features in this frame (FR-057)

    # --- first-K-slot microstructure (point-in-time, slots <= event_time + K only) ---
    first_k_slots: int              # K (declared per surface)
    first_k_buy_pressure: Decimal   # net buy SOL pressure over first K slots — C-4 BASELINE ENABLER
    first_k_volume_lamports: int    # total volume over first K slots — C-4 BASELINE ENABLER
    first_k_buy_count: int
    first_k_sell_count: int
    first_k_unique_buyers: int

    # --- first-60s survivor microstructure (FR-010) ---
    lp_depth_lamports: int
    holder_count: int
    holder_concentration_top10_bps: int   # share held by top 10, in bps
    sniper_cluster_score: float
    sell_tax_bps: int
    sell_reserve_trajectory: list[int]    # reserve samples, each stamped <= event_time

    # --- survivor TA (FR-010) ---
    rsi: float | None
    macd: float | None
    bb_width: float | None

    # --- adversarial selectivity (NEVER a buy trigger; FR-007/011, EH-005 default ZERO) ---
    smart_wallets_in: int           # count of tracked wallets buying in first K slots (slots <= event_time+K)
    smart_wallet_entry_lag_slots: int | None   # our entry slot - their fill slot (we are BEHIND)

    # --- tip-contention context (C-3, FR-047) ---
    tip_floor_at_decision_lamports: int        # LIVE tip floor read at event_time (recorded for stratification)
    tip_contention_bucket: Literal["low", "medium", "high"]

    # --- normalization provenance (C-5 / walk-forward §3) ---
    normalization_window: str       # "as_of_event_time" — NO global-stat normalization permitted

    # --- per-feature provenance manifest (anti-leak BY CONSTRUCTION, red-team-1 must-fix #2) ---
    provenance: FeatureProvenance   # declares each feature's source-data max-slot; build FAILS if any
                                    # feature's window extends past event_time + K (see §3.3)


class FeatureSourceWindow(BaseModel):
    """Per-feature declaration of the LATEST source slot the feature is allowed to read.
    The producing pipeline MUST stamp this; the build guard (validation-harness.md §2.5) FAILS
    if max_source_slot > event_time.slot + first_k_slots for ANY feature, OR if any field's
    lineage touches a `labels/` row or post-event_time data."""
    feature_name: str
    max_source_slot: int            # the highest slot any datum feeding this feature came from
    lineage_dataset: Literal["launch_events", "feature_frames", "mcs_scores", "fills_pre_event"]
    # NOTE: `labels` is NOT a legal lineage_dataset value — a feature whose lineage touches a label
    # is a build failure (`feature_lineage_touches_label`). The taint check is structural, not name-based.


class FeatureProvenance(BaseModel):
    windows: list[FeatureSourceWindow]   # one per non-metadata feature field in this frame
    builder_version: str                 # the feature-pipeline build that produced these windows
```

### 3.1 Why this prevents compute-time leakage structurally

1. **The only timestamp used for any join is `event_time` (slot + block_time).** `wall_clock_ms` is
   carried but explicitly marked NOT-for-joins (it is a monitoring field). The Parquet partition key
   (§7) is `event_date(block_time)`, so a row physically cannot be addressed by when the backtest
   computed it.
2. **`first_k_*` and `smart_wallets_in` are defined over slots ≤ `event_time + K`** with K declared
   per surface — there is no field whose definition admits a slot after the decision point. **This is
   now enforced by construction, not convention (§3.3):** every feature carries a
   `FeatureSourceWindow.max_source_slot`, and the build guard FAILS if any feature's declared source
   window extends past `event_time.slot + first_k_slots`. A feature that secretly read `event_time + H`
   data cannot pass the build because its `max_source_slot` would exceed the cutoff — and if the
   producer lies about `max_source_slot`, the lineage taint check (§3.3) catches a `labels/`-derived
   column independently of its declared window or its field name.
3. **`normalization_window = "as_of_event_time"` is a required, validated field.** A global-stat
   normalization (z-score over the whole dataset) cannot be expressed — there is no field to hold a
   global statistic, and the leak audit (validation-harness.md) FAILS any frame whose normalization
   provenance is not `as_of_event_time` (walk-forward §3, FR-012).
4. **`feature_schema_version`** is hashed into the frozen-baseline config (C-4): if the feature set
   changes after the baseline is frozen, the version bumps and the baseline-immutability test FAILS.
5. **The shifted-clock control** (AC-057): the harness re-runs with `event_time.slot` shifted +1 and
   asserts results CHANGE — proving *some* timestamp is load-bearing. This is **necessary-not-sufficient**
   (a uniform global shift preserves the relative label horizon, so a horizon-preserving leak survives
   it); the sufficient complement is the §3.3 build guards (per-feature provenance cutoff + lineage
   taint) plus the independent label-horizon / per-feature-lineage placebo (validation-harness.md C-5).

### 3.2 The `first_K_buy_pressure_volume` feature exists FOR the baseline (C-4)

The naive-momentum baseline (walk-forward §4, FR-015) is "enter every candidate with positive
first-K-slot net buy pressure above a fixed percentile." That rule is **unbuildable** unless the
FeatureFrame carries the raw first-K buy-pressure and volume — which the sim's `LaunchEvent` did NOT
(red-team flaw 2A). `first_k_buy_pressure` + `first_k_volume_lamports` + `first_k_buy_count` make the
baseline constructible from the same point-in-time store the model reads. The baseline gets the
**same** ExitEngine, cost stack, and position cap — only `selection intelligence` is denied it.

### 3.3 The build-time leak guards (promoted from runtime audit to CONTRACT, red-team-1 must-fix #2)

The point-in-time guarantee is no longer "a reviewer will notice." Three build-time guards make the
single highest-risk leak surface in meme-coin backtests — a forward-looking label joined in as a
feature — structurally impossible. They run in the validation package's blocking lint stage
(validation-harness.md §2.5); a FeatureFrame that violates any of them cannot compile.

1. **Per-feature cutoff guard (`feature_window_exceeds_cutoff`).** For every `FeatureSourceWindow` in
   the frame's `provenance.windows`, the build FAILS if `max_source_slot > event_time.slot +
   first_k_slots`. A feature pipeline that read a slot after the decision point is rejected by the
   build, not by a reviewer. The producer is REQUIRED to emit one window per non-metadata feature
   field; a missing window is `provenance_window_missing` (also a build failure), so a feature cannot
   silently escape the check by omitting its declaration.

2. **Lineage / taint guard (`feature_lineage_touches_label`).** This replaces the name-based `truth_*`
   scan as the *primary* defense. Every feature's `lineage_dataset` is checked against an allowlist
   (`launch_events`, `feature_frames`, `mcs_scores`, `fills_pre_event`). **`labels` is not on the
   allowlist** — a feature whose computation lineage touches a `labels/` row (or any dataset derived
   from one) is a build failure *regardless of the column's name*. So an innocuously-named leaked
   label (`survived_60s`, `realized_mult`, `fwd_return`) is caught because its **lineage** is tainted,
   not because its **name** matched `truth_*`. The `truth_*` AST scan is retained as a cheap belt-and-
   suspenders second line, explicitly demoted to necessary-not-sufficient.

3. **Label-column exclusion guard (`label_column_in_feature_frame`).** No field name or value that
   appears in the `LaunchOutcome` schema (§3A) may ever appear in a `feature_frames/` row. The harness
   asserts the column sets of `feature_frames/` and `labels/` are **disjoint** at load time; an overlap
   is a build/load failure. Labels live in their own dataset (§3A) and are joined to features **by
   `event_time` only**, inside the harness, never carried on the live frame.

---

## 3A. `LaunchOutcome` (the LABEL — its own typed, event-time-partitioned dataset; red-team-1 must-fix #1)

The label was previously prose-only ("computed post-hoc at event_time + H"). That left the single
highest-risk leak surface — a label built from the migration pump or the LP-pull rug — closed by
naming convention rather than by construction. It now has a typed contract, a dedicated event-time-
partitioned dataset (`labels/`), a stamped horizon resolution, and construction-time guards that
forbid it from ever reaching a feature path.

```python
class LaunchOutcomeLabel(str, Enum):
    SURVIVED = "SURVIVED"           # alive and tradeable at resolution
    RUGGED = "RUGGED"              # LP pulled / freeze / honeypot realized
    FADED = "FADED"               # not rugged but decayed below exit floor
    CENSORED = "CENSORED"         # outcome un-resolvable (C-6 survivorship guard)

class LaunchOutcome(BaseModel):
    """The post-hoc label. PRODUCED ONLY by the clean-room harness, NEVER on the live path.
    Written to `labels/` (event-time partitioned). FORBIDDEN from `feature_frames/` by §3.3 guard 3."""
    mint: str
    event_time: EventTime           # the DECISION ANCHOR — labels join to features on this and ONLY this
    label_horizon_h_slots: int      # H, declared per surface up front (purge/embargo uses it)
    resolution_event_time: EventTime  # = event_time + H, STAMPED (block_time at resolution); proves the
                                      # label was resolved at a strictly-later on-chain time than the anchor
    label: LaunchOutcomeLabel
    realized_mult_decimal: Decimal | None   # outcome multiple at resolution (None when CENSORED)
    max_drawdown_bps: int | None
    rug_detectable_at_decision: bool | None # for recall measurement on HELD-OUT folds (never a feature)
    resolution_recorded_at_ms: int  # when the harness COMPUTED the label (compute-time; audited >= resolution_event_time.block_time_ms)

    # construction-time invariant (asserted at write, validation-harness.md §2.5):
    #   resolution_event_time.slot == event_time.slot + label_horizon_h_slots
    #   resolution_event_time.block_time_ms  >  event_time.block_time_ms
    #   resolution_recorded_at_ms            >= resolution_event_time.block_time_ms
```

**Why this closes the leak by construction:**
- The label is its **own dataset** (`labels/`), partitioned by `event_date(event_time.block_time_ms)`,
  produced **only** by the clean-room harness — production code has no writer for it.
- The harness joins `labels/` to `feature_frames/` **on `event_time` only**, after the purge/embargo
  drop (validation-harness.md §3). It cannot be joined onto a feature.
- `resolution_event_time` is **stamped and strictly later** than the decision anchor (asserted), so a
  label built from data at `event_time + H` carries a horizon proof — a horizon-preserving leak (the
  one a uniform clock shift would hide, red-team-1 flaw 2) is now detectable by the label-shifting
  placebo (validation-harness.md §4 C-5) because the label's own horizon is perturbed independently.
- The §3.3 guard 3 asserts `feature_frames/` and `labels/` column sets are **disjoint** — a leaked
  forward-looking column under an innocuous name has no legal home in a feature frame.

---

## 4. `Prediction` / `DecisionSignal` (probability + uncertainty — NEVER a point price)

```python
class DecisionSignal(BaseModel):
    mint: str
    event_time: EventTime
    model_version: str
    p_calibrated: float             # calibrated probability in [0,1] (FR-014; reliability-curve-checked)
    uncertainty: float              # predictive uncertainty band — high uncertainty => de-risk only
    baseline_p: float               # the frozen naive-momentum baseline's signal on the same candidate
    surface: str                    # EH-001 | EH-003 | ... (which thesis fired)
    # NO point price field exists. The model never emits a price target (locked decision 9).
```

This is the contract `api-contracts.md /api/predictions` projects. The SNIPE loop reads
`p_calibrated` + `uncertainty` from KV (pre-staged); it never recomputes. High `uncertainty` is a
de-risk input to ¼-Kelly (size shrinks), never a size-up (FR-014/032).

---

## 5. `MCSScore` (adversarial sentiment — contrarian by construction)

```python
class MCSScore(BaseModel):
    asset: str
    event_time: EventTime
    conviction: float               # the score the SLOW loop consumes (de-risk/gate only)
    momentum: float
    novelty: float
    synchronicity: float            # HIGH synchronicity LOWERS conviction (FR-008, AC-010)
    account_age_median_days: float  # LOW age LOWERS conviction (adversarial shill signal)
    coordinated_shill_flag: bool
    red_flags: list[str]
    post_count: int
    reasoning: str                  # quoted untrusted text — NEVER executed as an instruction
```

`conviction` may only **gate or de-risk** an entry; a high MCS can never trigger or size up an entry
(FR-008; AC-021 monotone non-increasing). `synchronicity` and `account_age_median_days` are first-
class so the SLOW loop treats narrative as contrarian (BUILD-DIRECTIVE adversarial-sentiment rule).

---

## 6. `ReasoningVerdict` + `Intent` — asymmetric trust enforced BY TYPE

### 6.1 `ReasoningVerdict` (the LLM's ONLY output shape)

```python
class ReasoningAction(str, Enum):
    HOLD = "HOLD"               # no-op (the default/clamp target)
    VETO_ENTRY = "VETO_ENTRY"   # de-risk
    REDUCE_SIZE = "REDUCE_SIZE" # de-risk
    FORCE_EXIT = "FORCE_EXIT"   # de-risk

class ReasoningVerdict(BaseModel):
    mint: str
    event_time: EventTime
    action: ReasoningAction          # the enum has NO risk-increase variant — see proof below
    reason: str                      # quoted untrusted rationale
    confidence: float
    # audit fields (AC-054): what the raw LLM tried vs what was applied
    action_received_raw: str         # the unvalidated string the LLM emitted
    risk_increase_clamped: bool      # true if the raw action was a risk-increase, clamped to HOLD
```

**Proof the type cannot express a risk-increase (paste, per self-check item 3):** the
`ReasoningAction` enum has exactly four members — `HOLD`, `VETO_ENTRY`, `REDUCE_SIZE`, `FORCE_EXIT` —
**all de-risk or no-op**. There is no `SIZE_UP`, `WIDEN_STOP`, `ADD_LEVERAGE`, or `OVERRIDE_HARD_STOP`
member. A raw LLM string outside this set is caught at parse time and forced to `HOLD` with
`risk_increase_clamped=True` and a `llm_risk_increase_clamped` metric increment (FR-017, AC-019/054).
The type system is the primary defense; the clamp is the backstop.

### 6.2 `Intent` (the ONLY thing that reaches execution — de-risk union)

The `Intent` is a **tagged union**. Two construction paths exist, and only the entry path can
*increase* exposure — and it is the cost-gated SNIPE path, never a reasoning/LLM/social path. The
reasoning path can construct only the de-risk variants.

```python
class IntentKind(str, Enum):
    ENTRY = "ENTRY"             # increases exposure — ONLY constructible by the cost-gated SNIPE path
    EXIT = "EXIT"               # de-risk
    REDUCE = "REDUCE"           # de-risk (partial exit)
    VETO = "VETO"               # de-risk (cancel a pending entry)

class CostStack(BaseModel):
    """An ENTRY Intent CANNOT be constructed without this. Cost-aware by construction (FR-027)."""
    expected_edge_bps: int
    jito_tip_bps: int
    priority_fee_bps: int
    entry_slippage_bps: int
    amm_fee_bps: int
    exit_slippage_bps: int
    adverse_selection_bps: int      # FLOOR 150 bps pre-calibration, labeled UNCALIBRATED (OQ-007, C-11)
    total_cost_bps: int             # = sum; ENTRY rejected if expected_edge_bps <= total_cost_bps

class EntryIntent(BaseModel):
    kind: Literal[IntentKind.ENTRY]
    mint: str
    event_time: EventTime
    sol_in_lamports: int            # integer; <= min(per_coin_cap, 0.25 x kelly) (FR-032)
    slippage_bps: int
    tip_lamports: int               # from LIVE tip cache, bounded by min(floor, 0.30 x edge) (FR-027)
    cu_price_microlamports: int
    target_slot: int                # MUST be >= event_time.slot + 5 (no block-0, AC-017)
    cost_stack: CostStack           # REQUIRED — no ENTRY without the full cost stack present
    venue: str                      # registry venue id (execution-venue.md)
    wallet_id: str                  # multi-wallet (FR-036); single for R3 (OQ-010)

class ExitIntent(BaseModel):
    kind: Literal[IntentKind.EXIT]
    mint: str
    fraction_bps: int               # 0..10000 of remaining position
    exit_mode: Literal["secure", "fast"]   # default secure (OQ-008)
    reason: str

class ReduceIntent(BaseModel):
    kind: Literal[IntentKind.REDUCE]
    mint: str
    fraction_bps: int               # must be a REDUCTION; cannot exceed remaining
    reason: str

class VetoIntent(BaseModel):
    kind: Literal[IntentKind.VETO]
    mint: str
    reason: str

Intent = Union[EntryIntent, ExitIntent, ReduceIntent, VetoIntent]   # discriminated on `kind`
```

**Why this is structurally de-risk-safe:**
- The reasoning/LLM/social/MCS code path is **only handed a factory** that produces `ExitIntent`,
  `ReduceIntent`, or `VetoIntent` — it has no reference to `EntryIntent`'s constructor. There is no
  function in its module that returns an `EntryIntent`, so it cannot widen a stop, add leverage, or
  size up — those Intents do not exist as expressible values on that path. (BLUEPRINT §8; ADR-0006.)
- `EntryIntent` **cannot be constructed without a populated `CostStack`** and a `target_slot ≥ slot+5`.
  There is no default that bypasses the cost gate; an ENTRY with `expected_edge_bps ≤ total_cost_bps`
  is rejected before the Intent leaves the SNIPE loop (FR-027, AC-013).
- `ReduceIntent.fraction_bps` and `ExitIntent.fraction_bps` are bounded to reductions; there is no
  "increase position" Intent variant at all. The union is **closed** — adding a risk-increasing
  member would be a contract change requiring an ADR + delta notice.

---

## 7. Position + FSM

```python
class FSMState(str, Enum):
    IDLE = "IDLE"
    ENTERING = "ENTERING"       # claimed by SNIPE (atomic CAS); write-ahead before submit
    OPEN = "OPEN"
    CLOSING = "CLOSING"
    CLOSED = "CLOSED"
    VETOED = "VETOED"

class Position(BaseModel):
    mint: str
    token: str
    surface: str
    fsm_state: FSMState              # single-writer: SNIPE writes ENTERING; FAST writes the rest (FR-024)
    entry_slot: int
    sol_in_lamports: int
    tokens_out_base: int
    entry_slippage_bps: int
    exit_config_label: str           # frozen at entry; preset changes never retro-modify (AC-033)
    exit_mode: Literal["secure", "fast"]
    tp_hit: int
    tp_total: int
    trailing_armed: bool
    hard_stop_price_lamports: int
    realized_pnl_net_lamports: int | None
    unrealized_pnl_net_lamports: int
    cost_breakdown: CostStack
    status: Literal["open", "closed"]
    completeness_status: Literal["complete", "CENSORED"]  # C-6: never drop unlabeled/un-snapshotted
```

`completeness_status = CENSORED` is how un-snapshotted / un-labeled outcomes survive in the dataset
(C-6, AC-006) — they are carried, never dropped, so survivorship bias cannot creep in.

FSM legal transitions (single-writer enforced by the atomic Lua CAS, BLUEPRINT §2.2):
`IDLE→ENTERING` (SNIPE), `ENTERING→OPEN|VETOED` (FAST on fill/veto), `OPEN→CLOSING` (FAST),
`CLOSING→CLOSED` (FAST). A second `IDLE→ENTERING` on a mint already `ENTERING/OPEN` is rejected
`fsm_state_conflict` (AC-012).

---

## 8. `RiskConfig` + `BreakerState`

```python
class RiskConfig(BaseModel):
    # hardcoded FLOORS (OQ-005) — API may only TIGHTEN below these, never widen (api-contracts §5)
    per_trade_cap_lamports: int = 100_000_000        # 0.1 SOL
    max_aggregate_lamports: int = 500_000_000        # 0.5 SOL
    daily_risk_tranche_lamports: int = 500_000_000   # 0.5 SOL
    daily_loss_limit_pct: Decimal = Decimal("3.0")   # tighten-only; never widen beyond 3.0% (OQ-001)
    daily_loss_floor_lamports: int = 300_000_000     # absolute -0.30 SOL floor, independent (OQ-001)
    max_slippage_bps: int
    snipe_threshold: float            # raising it is de-risk; lowering rejected by API
    veto_threshold: float
    jito_tip_cap_frac: Decimal = Decimal("0.30")     # 0.30 x edge cap (tips.py invariant)
    kelly_fraction_cap: Decimal = Decimal("0.25")    # hard 1/4 Kelly (FR-032)
    n_wallets_max: int = 1            # R3 (OQ-010); multi-wallet built but N_max=1 until R4
    blast_radius_cap_lamports: int    # = per_trade_cap at R3 (OQ-010)
    t_dms_seconds: int                # 60 default, ENV-CONFIGURABLE not constant (OQ-006)
    pre_calibration_haircut_bps: int = 150   # UNCALIBRATED floor (OQ-007); widen-only pre-R3

class BreakerState(BaseModel):
    state: Literal["ARMED", "TRIPPED"]
    tripped_at_utc: str | None
    daily_net_pnl_lamports: int       # net for ONE event-time UTC day (see day key below)
    daily_net_pnl_day_utc: str | None # 'YYYY-MM-DD' (event-time UTC) the net above belongs to; ADR-0012
    threshold_breached: str | None    # e.g. "-3.0% tranche" or "-0.30 SOL"
    # re-arm ONLY via POST /api/breaker/reset with operator auth (AC-029); LLM may trip not reset
    # INVARIANT (ADR-0012): daily_net_pnl_lamports != 0  ⇒  daily_net_pnl_day_utc is not None
```

> **DELTA NOTICE (ADR-0012, 2026-06-16 — post-G1 schema change to a FROZEN contract).** Added
> `daily_net_pnl_day_utc: str | None` to `BreakerState`. The breaker previously persisted only the
> scalar daily net with no record of *which* event-time UTC day it belonged to; on restart it seeded
> that net into whichever UTC day the first post-restart event happened to fall in. Across a restart
> that straddles UTC midnight this both spuriously tripped (negative carry into a fresh day) and — the
> dangerous direction — **MASKED a real next-day loss that must halt** (positive carry hiding it),
> defeating the daily-loss circuit breaker and breaking the C-5 point-in-time claim. The day key makes
> the seed day-aware: the persisted net is re-applied ONLY to the day it was accumulated in, so a first
> event on a later UTC day starts that day fresh at 0. **Affected tasks (T-320 risk breaker, T-340/341
> control plane + `/api/breaker` projection if it echoes the field, T-352 dashboard).** Migration: a
> legacy persisted row with no `daily_net_pnl_day_utc` and a non-zero net is rejected fail-closed at
> load — operators clear stale breaker state on the deploy that ships this change (a fresh ARMED day
> is the safe default; a TRIPPED latch is re-asserted by the control plane).

---

## 9. Point-in-time feature store design (Redis hot tier + Parquet history)

### 9.1 Redis HOT tier (live FeatureFrames, TTL'd)

- Key: `feat:{mint}:{event_time.slot}` → serialized `FeatureFrame`, **TTL 5 minutes**. This is what
  the SLOW loop writes and the classifier/control-plane read for live candidates.
- Pre-staged decision: `score:{mint}` → `{p_calibrated, uncertainty, veto_bit, event_time, stamped_at}`
  with TTL ≤ SLOW cadence. **This is the ONLY thing SNIPE reads from the model path** (BLUEPRINT §2.1).
  If absent/stale (`data_staleness_ms` over budget), SNIPE SKIPs — no block.
- The hot tier is for **liveness**, not history; it is TTL'd and is never the source of truth for a
  backtest.

### 9.2 Parquet HISTORY (point-in-time, append-only, partitioned by EVENT-TIME)

- **Append-only.** Rows are never updated in place. A correction is a new row with a later
  `recorded_at` and the same `event_time`; the harness reads the as-of view (latest `recorded_at` ≤
  the cutoff), so a backtest reconstructs exactly what was knowable at any past instant.
- **`recorded_at` honesty is constrained structurally (red-team-1 must-fix #3).** As-of correctness
  is only leak-free if `recorded_at` truly equals when the datum became knowable. The contract now
  enforces an invariant on **every** row, original or correction:
  **`recorded_at_ms >= event_time.block_time_ms`** — a datum cannot have been recorded before the
  on-chain event it describes existed. A correction/backfill row for a past `event_time` that sets
  `recorded_at` to (or before) the original event-time is **rejected at write** (`recorded_at_before_knowable`).
  Additionally, a backfill row MUST carry `recorded_at >=` the wall-clock at which the correction was
  computed (it cannot back-date itself into the past), and the as-of-read audit (validation-harness.md
  §3) flags any correction row whose `recorded_at` predates the latest `recorded_at` already present
  for that `(dataset, event_time)` key — a backfill that travels backward in compute-time is a leak
  vector and fails the audit (`backfill_recorded_at_regression`). This closes the live backfill
  lookahead vector: there is no honest way to reintroduce future knowledge under a past `recorded_at`.
- **Partitioned by `event_date = date(event_time.block_time_ms)`** — NOT by `recorded_at`. This is
  the structural guarantee against compute-time leakage: you physically cannot scan rows by when they
  were computed; the partition key is on-chain event-time. (C-5; walk-forward §1/§3.)
- Separate datasets, all event-time partitioned: `launch_events/`, `feature_frames/`,
  `decision_signals/`, `fills/`, `positions_closed/`, `mcs_scores/`, `reasoning_verdicts/`, and
  **`labels/`** (the `LaunchOutcome` dataset, §3A — written ONLY by the clean-room harness, joined to
  features by `event_time` only, NEVER merged into `feature_frames/` per §3.3 guard 3).
- Each row carries both `event_time` (the join anchor) and `recorded_at` (compute-time, for the as-of
  read and for the clock audit), so the shifted-clock control (AC-057) can perturb `event_time` and
  observe the result change.
- `completeness_status` rows (`CENSORED`, C-6) are written for every census-known launch we failed
  to snapshot or label, so the dataset is survivorship-free by construction.

### 9.3 Retention

| Dataset | Hot (Redis) | History (Parquet) |
|---|---|---|
| FeatureFrame | TTL 5 min | indefinite (training corpus; R1 ≥3,000 launches target, A-010) |
| LaunchOutcome (label, §3A) | **never hot** (harness-only) | indefinite (`labels/`, event-time partitioned) |
| DecisionSignal | TTL ≤ slow cadence | indefinite |
| MCSScore | TTL 10 min | indefinite |
| Fills / Positions (closed) | while open | indefinite (PnL audit) |
| ops.feed events | MAXLEN ~10k | not persisted to history (live ops) |

---

## 10. Versioning + post-G1 changes

`feature_schema_version` and `model_version` are first-class so a frozen baseline (C-4) and the
walk-forward windows can detect a feature/model change. Any change to a schema in this file after G1
is an ADR + delta notice naming affected tasks (T-300/304/305 ingestion+features, T-310/311 model,
T-340/341 controller+API, T-352 dashboard, **T-400/401 harness** — owner of the `LaunchOutcome` label
and provenance/lineage guards) — never a silent edit.
