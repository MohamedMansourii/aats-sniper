# BLUEPRINT — AATS Solana Meme-Coin Ultra-Sniper (T-200)

**Version:** 1.1.0 — **CONTROL-PLANE CONTRACT FROZEN** (post-G1-red-team). See §14 for the red-team
resolutions and the ADR-0009/0010 delta notice; v1.0.0 was the pre-red-team G1 candidate.
**Author:** `solana-systems-architect`
**Date:** 2026-06-16
**Gate:** G1 (architecture) — no production code is written before this is approved. **All blocking
red-team items (leak-proofness, custody) are resolved by construction (§14); the contract is FROZEN.**
**Source authorities (read cold):** `CLAUDE.md §8`, `AATS-BRIEF.md`, `BUILD-DIRECTIVE-v3.md`,
`AUTONOMY-DIRECTIVE.md`, `EDGE-VERDICT.md` (GO-PAPER-ONLY + C-1..C-13), `walk-forward-methodology.md`,
`SPEC.md`, `acceptance-criteria.md`, `open-questions.md` (10 OQ defaults ADOPTED),
`dashboard/src/lib/api.ts` + `types.ts`, `sol-sniper/sniper_sim/*`.

**Companion documents (this blueprint is the master; these are normative siblings):**
`api-contracts.md` (T-201, FROZEN control plane), `data-models.md` (T-203, typed contracts +
point-in-time store), `execution-venue.md` (T-202, the seam), `validation-harness.md` (T-204,
clean-room), `latency-budget.md` (T-205), `infrastructure.md` (T-206), `adr/ADR-0001..0008`.

---

## 0. The one-paragraph thesis (what this architecture is FOR)

We are building a **detection-competitive, submission-disadvantaged** Solana sniper. The latency
ledger (`latency-budget.md`) is blunt: a solo, unstaked, non-co-located desk **cannot win block-0,
migration-block-0, or any tip-escalation auction** — SWQoS reserves ~80% of leader QUIC for staked
nodes and our bundle slips a full slot under contention. So this architecture deliberately spends
its complexity budget on the **inverse of the speed race**: safety-selective late entry (slot +5..+30),
migration-**survivor** selection, and exit discipline — surfaces where the edge is *selection and
risk*, not nanoseconds. Every structural choice below — the Rust/Python split, the clean-room harness,
the point-in-time store, the asymmetric-trust type system, the DRY-RUN flag — exists to make a *fake*
edge impossible to report and a *real* edge survivable. The dominant failure mode is absence of edge
net of cost; we architect against it, not around it.

---

## 1. Spec requirement → component traceability (every FR maps to a component; every component to a requirement)

| Component | Module | Owns (FR / AC) | Loop |
|---|---|---|---|
| **Ingestion transport** (Geyser/Yellowstone gRPC + ShredStream overlay, decoders) | M1 | FR-001/002/003, AC-001/002/003 | SNIPE feed source |
| **Venue/program-ID registry** (live-verified, pluggable) | M1/M4 | FR-001, AC-002, `execution-venue.md` | startup + SNIPE |
| **Point-in-time feature store** (Redis hot + Parquet history) | M1 | FR-004/005, AC-004/005, `data-models.md` | SLOW writes, SNIPE reads |
| **Completeness-audit hooks** (census reconcile, censored rows) | M1 | FR-009, AC-006 | offline/SLOW |
| **Feature pipeline** (first-60s microstructure, survivor TA, `smart_wallets_in`) | M1 | FR-010/011/012, AC-005/008 | SLOW |
| **first-K buy-pressure/volume feature** (baseline enabler, C-4) | M1 | FR-005, AC-005, `data-models.md` §FeatureFrame | SLOW |
| **Smart-money stream** (`accounts_subscribe`, lag accounting) | M1 | FR-007, AC-008 | SLOW |
| **MCS sentiment** (Tier-A filter + Tier-B LLM, adversarial) | M1/M2 | FR-008, AC-010 | SLOW only |
| **Fast snipe classifier** (LightGBM/MLP → ONNX/Rust) | M2 | FR-013/014, AC-016, NFR-003 | SNIPE (read pre-staged) |
| **Frozen naive-momentum baseline** (hashed config, C-4) | M2 | FR-015, AC-056..059 | offline/validation |
| **TFT survivor model** (calibrated p + uncertainty) | M2 | FR-016, NFR-003 | SLOW only |
| **LLM Reasoner** (schema-enforced, de-risk-only clamp) | M2 | FR-017/018, AC-019/054 | SLOW (pre-stages veto flag) |
| **Triple loop controller** (SNIPE/FAST/SLOW) | M3 | FR-021/022/023, AC-016/017 | all |
| **Per-position FSM** (single-writer, write-ahead) | M3 | FR-024, AC-012 | FAST owns |
| **Control-plane API** (frozen contract) | M3 | FR-025/049/050/055/056, `api-contracts.md` | OPS |
| **Pre-trade safety gate** (0-RPC checks 1–5 + sell-sim) | M4 | FR-026/037, AC-011/015 | SNIPE |
| **Cost gate** (`edge > cost` or NO TRADE) | M4 | FR-027, AC-013/014 | SNIPE |
| **Tip controller** (live tip_stream, edge-bounded) | M4 | FR-027, AC-014, `latency-budget.md` | SNIPE (cached read) |
| **ExecutionVenue impls** (Sim / JitoJupiter / Raydium / dead-ccxt) | M4 | FR-028/039/040/041, AC-018, `execution-venue.md` | SNIPE/FAST |
| **ExitEngine** (TP ladder + trailing + hard stop + timeout, Fast/Secure) | M4 | FR-029/030, AC-032/033 | FAST |
| **Hierarchical risk engine + ¼-Kelly sizing** | M4 | FR-031/032, AC-030/031 | FAST/SNIPE |
| **Daily-loss circuit breaker** | M4 | FR-034, AC-028/029 | FAST |
| **Survivable stop (3 layers)** | M4/M3 | FR-033, AC-025/026/027 | venue + FAST + DMS |
| **Dead-man's switch** (external watchdog) | M4/M3 | FR-033, AC-045/046, OQ-006 (T_DMS=60s env) | external process |
| **Resting limit/DCA orders** (fire offline) | M4 | FR-035, AC-022/023 | FAST |
| **Multi-wallet execution** (blast-radius cap, anti-cluster) | M4 | FR-036, AC-024, OQ-010 (N_max=1 @ R3) | SNIPE |
| **Per-surface decay halts** | M4 | FR-038, AC-034/035 | FAST |
| **Walk-forward validation harness** (clean-room) | M5 | FR-019/020/043..048, AC-055..060, `validation-harness.md` | offline |
| **Redis Streams message bus** | infra | FR-021/023 decoupling, ADR-0001 | all |
| **Docker/Compose deploy + monitoring + DMS wiring** | M5 | NFR-007/010, `infrastructure.md` | ops |
| **Operator dashboard** (existing, finish + wire) | E | FR-049, AC-036..039/047..051 | OPS |
| **Telegram channel** (alerts + de-risk commands) | F | FR-050/055, AC-042/043/052/053 | OPS |

No orphan components; no orphan FRs. The reverse map (FR → component) is the SPEC §5 list — every
FR-001..FR-057 lands in exactly one row above.

---

## 2. Triple-loop topology

The three loops run as **separate OS processes** (not threads) so a stall in one can never block
another at the GIL or the scheduler. They communicate **only** through Redis (Streams for events,
keyspace for shared state). The SNIPE+FAST loops are a **single Rust process** (the hot core); the
SLOW loop, models, and control plane are **Python processes**. See §4 for the boundary justification.

```mermaid
flowchart TB
    subgraph EXT["External (network-bound, NOT our compute)"]
        GEY[Geyser/Yellowstone gRPC]
        SHRED[ShredStream overlay]
        SOCIAL[Social/news APIs]
        SMART[smart-wallet accounts_subscribe]
        JITOTIP[Jito tip_stream WS]
        BE[Jito block engine - regional]
    end

    subgraph BUS["Redis (Streams + keyspace) — the decoupling membrane"]
        S_EVENTS[(stream: launch.events)]
        S_FEAT[(stream: feature.frames)]
        S_DEC[(stream: decision.signals)]
        S_INTENT[(stream: intents)]
        S_FILL[(stream: fills)]
        S_OPS[(stream: ops.feed)]
        KV[(keyspace: FSM, risk state,\npre-staged scores, veto flags,\ntip cache, breaker state)]
    end

    subgraph RUST["RUST HOT CORE (one process)"]
        direction TB
        ING[Ingest+decode\nGeyser/Shred → LaunchEvent]
        SNIPE["SNIPE LOOP\n(ms budget, event-triggered)\ngate→cost-gate→model-read→build→sign→submit"]
        FAST["FAST LOOP\n(<=100ms tick, deterministic)\nSL/TP/OMS/reconcile/breaker\nNEVER awaits an LLM"]
    end

    subgraph PY["PYTHON (separate processes)"]
        direction TB
        SLOW["SLOW LOOP\n(seconds–minutes)\nfeature assembly · model inference\n· MCS · LLM veto · scaling · staging"]
        TFT[TFT survivor model]
        LLM[LLM Reasoner - de-risk only]
        CP[Control-plane API server]
    end

    DMS[["Dead-man's switch\n(external watchdog process)\nheartbeat loss > T_DMS → flatten pre-signed"]]

    GEY --> ING
    SHRED --> ING
    ING -->|XADD| S_EVENTS
    S_EVENTS -->|consumer group| SNIPE
    S_EVENTS -->|consumer group| SLOW

    SOCIAL --> SLOW
    SMART --> SLOW
    SLOW -->|XADD| S_FEAT
    SLOW -->|write pre-staged score+veto| KV
    SLOW --> TFT
    SLOW --> LLM
    S_FEAT --> CP

    SNIPE -->|read pre-staged score, veto, tip cache| KV
    JITOTIP -->|cache write (off hot path)| KV
    SNIPE -->|XADD| S_INTENT
    SNIPE -->|build/sign/submit| BE
    BE -->|fill ack| S_FILL
    S_FILL --> FAST
    FAST <-->|single-writer FSM, breaker| KV
    FAST -->|exit tx| BE
    FAST -->|XADD| S_OPS
    SNIPE -->|XADD| S_OPS
    S_OPS -->|SSE /api/feed| CP

    FAST -.heartbeat.-> DMS
    DMS -.on heartbeat loss.-> BE

    CP <-->|read state / de-risk commands| KV
```

### 2.1 Loop budgets, ownership, and back-pressure rules

| Loop | Latency budget | Owns | Reads | Writes | Back-pressure rule |
|---|---|---|---|---|---|
| **SNIPE** | ingress→`execute()` ≤ **150 ms p99 / ≤50 ms p50** internal (FR-051, NFR-001; block-engine RTT excluded, counted separately per C-1) | event decode, 0-RPC safety gate (≤10ms), cost gate, intent build/sign/submit | `launch.events`, KV pre-staged score + veto flag + tip cache | `intents`, `ops.feed`, FSM `ENTERING` claim | If a candidate's pre-staged score is **missing or stale** (`data_staleness_ms` over budget) the SNIPE loop **SKIPs** (it never blocks waiting for the SLOW loop to produce one). Missing score = no trade, logged. This is the structural guarantee that a slow model NEVER stalls the hot path. |
| **FAST** | tick ≤ **100 ms** (FR-053, NFR-002); hard-stop trigger→`exit()` ≤ **50 ms p99** (FR-052) | SL/TP/trailing/hard-stop, OMS, fill reconciliation, circuit breaker, resting-order activation, per-surface decay halts, **survivable-stop Layer 2 enforcer**, DMS heartbeat emit | `fills`, KV FSM + risk state + veto flag, mark prices | FSM transitions, `exit` intents, breaker state, `ops.feed` | The FAST loop **never awaits** an LLM, a network RPC not already subscribed, or the SLOW loop. It reads only **pre-computed flags** from KV (atomic GET). If a tick exceeds 100 ms it emits a `fast_tick_budget_breach` metric and continues — it does not queue work that compounds. A breached tick is a monitored anomaly, never a silent stall. |
| **SLOW** | seconds–minutes; per-call ceilings: ONNX classifier ≤5ms, LLM veto ≤200ms, TFT ≤500ms (FR-054, NFR-003) | feature assembly, model inference (pre-stages classifier score), MCS, LLM veto generation, position-scaling proposals (de-risk only), capital-staging state | `launch.events`, `feature.frames`, social/smart-money, recorded store | `feature.frames`, KV pre-staged score + uncertainty + veto flag, `decision.signals` | A flaky social/RPC/enrichment API **lags or drops its own stream** (Redis `MAXLEN` cap on each producer stream) — it does **NOT** back-pressure the SNIPE path, because SNIPE reads a *cached* KV value, not the live SLOW computation. If the SLOW loop falls behind, its pre-staged scores go stale and SNIPE SKIPs (de-risk), rather than the system blocking. **Ingestion is decoupled from compute by construction.** |

**The non-negotiable, stated as an invariant:** *the only thing the SNIPE loop reads from the SLOW
loop is a pre-staged scalar (score + uncertainty + veto bit) written to Redis KV with an event-time
stamp. SNIPE never calls into Python, never awaits a model, never awaits the LLM.* If a reviewer
finds the SNIPE path `await`-ing anything other than the venue submit and a local KV read, the
design is violated (ROSTER §5.2, FR-021).

### 2.2 The snipe → fast handoff (atomic, write-ahead)

This is the one place a race would be catastrophic (double-entry, FR-024/AC-012). The handoff:

1. SNIPE loop, before building a tx, performs an **atomic FSM claim** on the mint: a Redis Lua
   script (single round-trip, atomic) that `CAS`es FSM state `IDLE → ENTERING` keyed by mint. If
   the mint is already `ENTERING`/`OPEN`, the script returns reject → SNIPE logs `fsm_state_conflict`
   and drops. **1,000 concurrent snipes on one mint → exactly one claim wins** (AC-012).
2. The claim writes an **intent-id + write-ahead record** to KV *before* the tx is submitted, so a
   crash mid-submit leaves a recoverable `ENTERING` record (NFR-006 restart restores it).
3. On fill ack (`fills` stream), the FAST loop transitions `ENTERING → OPEN` (single-writer: only
   the FAST loop ever writes `OPEN/CLOSING/CLOSED`; only the SNIPE loop ever writes the `ENTERING`
   claim). No two writers touch the same FSM field — that is what "single-writer" means here.

ADR-0002 records the Rust/Python split that makes this handoff cheap; ADR-0007 records the FSM
single-writer + write-ahead decision.

---

## 3. Promoting the `sol-sniper` seams to production (the seam is law)

The validated sim is the **starting contract**, not a throwaway. Each seam is promoted as follows.
Engineers productionize against these — they do not redesign them.

| `sol-sniper` artifact | Production promotion | Owner task | Key constraint |
|---|---|---|---|
| `venue.py` `ExecutionVenue` ABC + `SimulationVenue` + `JitoJupiterVenue` stub | The `ExecutionVenue` interface in `execution-venue.md` is the **literal seam**. `SimulationVenue` is kept (paper/replay) and **must implement the 8 promoted ABC members sim-native** (`submit_mode=SIMULATION`, no-network `land()`, key-less `sign()`) so it stays instantiable (code-reviewer R-01). `JitoJupiterVenue` becomes the real impl, **DRY-RUN first** (FR-039); its `sign()` **crosses to `aats-signer`** (ADR-0009). `RaydiumVenue` and `dead-ccxt` added behind the same ABC. Loop core imports the **interface only** (never a concrete venue). | T-202 / T-327 | `execute(intent, event)` **arity + return** preserved (intent promoted `SwapIntent`→`EntryIntent`, money→integer/Decimal, code-reviewer R-02); tip/CU/slippage first-class on the intent. |
| `tips.py` `TipStrategy` (`edge_cap_frac=0.30`, floor) | Becomes the **TipController** reading **live** `tip_stream`/`tip_floor` (bundles-api-rest.jito.wtf) into a KV cache off the hot path; the SNIPE loop reads the cached percentile. The `min(market_floor, 0.30×edge)` bound is preserved verbatim (FR-027). **Hardcoded tip = build-FAIL** (AC-014). | T-330 | `floor_frac`/`edge_cap_frac` stay; market floor is now live, never the constant `200_000`. |
| `safety.py` `SafetyGate` + `GATE_ORDER` | Becomes the real ordered gate. Checks 1–5 are **real 0-RPC on-chain decodes** in the Rust hot path (≤10ms). **`safety.py:43`'s read of `truth_is_rug`/`truth_rug_detectable` is DELETED in the production gate** — recall ≥0.50 is *measured* on held-out labeled rugs, never a `catch_rate` parameter (C-7, FR-019, AC-056). | T-323 / T-204 | `catch_rate` does not exist in production. The clean-room import guard FAILS the build if any `truth_*` symbol appears (validation-harness.md). |
| `exits.py` `ExitEngine` (FAST/SECURE configs, ladder, trailing, hard stop) | Productionized as the FAST-loop `ExitEngine` (FR-029). The **synthetic `generate_path`** is sim-only and **must NOT** drive any production or recorded-data PnL. The SECURE/FAST `sandwich_p`/`sandwich_loss` **constants must NOT be inherited** by the R1/R2 cost stack (C-2) — they are re-derived from observed dump incidence. | T-325 / T-331 | Default exit mode = **Secure-MEV** (OQ-008). Hard stop has priority over all rules (preserved). |
| `amm.py` (constant-product, `RAYDIUM_FEE=0.0025`) | Production AMM math for slippage modeling and `assert_min_out`. Fee per venue read from the registry, not hardcoded (PumpSwap 0.25%, Raydium 0.25% — A-005/A-006). | T-329 | `simulate_entry` `buyers_ahead` model is the slippage primitive; the recorded harness must shift `buyers_ahead` RIGHT by the staked-lane penalty (C-1/C-2). |
| `metrics.py` `Metrics` (the M5 scorecard) | Promoted to **real Prometheus telemetry** (NFR-007); `net_pnl_sol`, `land_rate`, `rug_avoid_rate`, `tip_efficiency`, `mean_slot_delay` become live gauges/histograms the dashboard reads via the control plane (T-250). | T-250 | `net_pnl_sol` is always **net** (the dashboard's primary number, AC-036). |
| `types.py` (`LaunchEvent`, `SwapIntent`, `FillResult`, `Decision`, `LAMPORTS_PER_SOL`) | Promoted to the typed contracts in `data-models.md`. **The `truth_*` block of `LaunchEvent` is sim-only and is split out** — production `LaunchEvent` carries no ground truth. Money becomes integer base units / `Decimal` (the sim's `float` reserves are sim-only; production is lamports/base-units, NFR-009, FR-042). | T-203 | `Decision` enum and `SwapIntent` fields preserved; types gain Pydantic (Python) + serde (Rust) parity. |

**ADR-0003** records the decision to promote (not rewrite) the seams.

---

## 4. Rust hot-path vs Python boundary (justified by latency numbers, not preference)

The boundary is **a process split**, not PyO3/FFI. Rationale (ADR-0002): the hot core's hard
constraint is *no GC pause, no GIL, no foreign-call jitter on a ≤150 ms p99 path*; embedding Python
in the Rust process (PyO3) re-introduces exactly the GIL/GC unpredictability we are paying Rust to
remove, and a model crash in-process would take down the snipe path. A clean **process split with
Redis as the boundary** keeps the hot core's failure domain isolated and the Python side independently
restartable (NFR-006). The cost is one Redis hop (sub-ms local, in budget). We pay it deliberately.

### 4.1 MANDATORY RUST (the hot core — one process)

| Responsibility | Why Rust (the latency number) | FR |
|---|---|---|
| Geyser/ShredStream ingest + account-data decode (pool keys, reserves) | decode p50 1.2ms / p99 3.5ms inside the 6ms build block (`latency-budget.md`); a Python decode + GC pause would blow the ≤10ms gate budget alone | FR-001/002 |
| 0-RPC pre-trade safety gate (checks 1–5) | must complete inside ≤10ms p99 with zero GC jitter; deterministic short-circuit | FR-026 |
| Cost gate (edge vs cost) | gate p50 2ms / p99 6.5ms; runs every candidate on the hot path | FR-027 |
| Snipe-path tx **build + tip ix + submit** (the hot core builds the *unsigned* tx and submits the *signed* bytes; **signing crosses to `aats-signer`**, ADR-0009) | build p50 1.3ms / p99 4ms; the `aats-signer` round-trip is a local Unix-socket Ed25519 sign — a minimal-surface native process, budgeted at ≤1.5ms p99 added (a deliberate cost paid for custody isolation, §5.2); submit_call ≤2.5ms p99 | FR-028/039/040 |
| ONNX/quantized-MLP **inference shim** (the snipe classifier read) | decide p50 1.5ms / p99 5ms (≤5ms FR-013); native ONNX runtime, no Python interpreter on the path | FR-013/014 |
| FAST loop: deterministic stop enforcement, OMS, FSM transitions, breaker | tick ≤100ms, hard-stop→exit ≤50ms p99; determinism + no GC pause is the whole point | FR-022/029/033/034 |

### 4.2 PYTHON (separate processes — never on the hot path)

| Responsibility | Why Python is fine (the budget) | FR |
|---|---|---|
| SLOW loop: feature assembly, sensor fusion, MCS | seconds–minutes cadence; latency irrelevant; ecosystem (pandas, transformers) decisive | FR-008/010/023 |
| TFT survivor model | ≤500ms on SLOW loop, separate process; PyTorch ecosystem | FR-016 |
| LLM Reasoner (de-risk-only) | ≤200ms, pre-stages a flag; **never** inline on FAST/SNIPE | FR-017/018 |
| Model **training**, ONNX export, calibration | offline entirely | FR-013/015 |
| Backtest / walk-forward harness (clean-room) | offline; correctness over speed | FR-019/043 |
| Control-plane API server + orchestration glue | OPS-cadence (seconds); SSE/HTTP | FR-025/049/050 |
| Dead-man's switch watchdog | external process; polls heartbeat, holds pre-signed flatten tx | FR-033 Layer 3 |

**The model that touches the SNIPE path is the ONNX artifact, run by the Rust inference shim.** The
LightGBM/MLP is *trained* in Python, *exported* to ONNX, and *executed* in Rust. The LLM and TFT are
SLOW-loop only and physically cannot reach the hot path because they live in a different process and
SNIPE reads only a KV scalar. (ADR-0002.)

---

## 5. Message bus & service decomposition

**v1 bus = Redis Streams** (ADR-0001). Rationale: the team already standardizes on Redis for shared
state (A-012, SPEC); Streams give consumer groups (independent fan-out to SNIPE and SLOW), `MAXLEN`
caps (a flaky producer self-bounds), replay (R1 shadow-record can re-drive the loop from recorded
`launch.events`), and a single infra dependency for v1. Documented **migration path to NATS
JetStream** when fan-out/throughput demands it (multi-host, higher sustained event rates, or
cross-region) — the contracts in `data-models.md` are transport-agnostic so the swap is a wiring
change, not a contract change.

### 5.1 Streams (each producer self-bounds; MAXLEN caps stated)

| Stream | Producer | Consumers (group) | MAXLEN cap | Replayable? |
|---|---|---|---|---|
| `launch.events` | Rust ingest | SNIPE (`g_snipe`), SLOW (`g_slow`) | ~100k (≈ rolling window) | **Yes** — R1 shadow replay drives the whole system |
| `feature.frames` | SLOW | control plane, recorder | ~50k | Yes (append to Parquet history) |
| `decision.signals` | SLOW | recorder, control plane | ~50k | Yes |
| `intents` | SNIPE/FAST | recorder, control plane | ~50k | audit only |
| `fills` | venue adapter | FAST, recorder | ~50k | audit only |
| `ops.feed` | all loops | control plane → SSE `/api/feed` | ~10k | no (live ops) |

**The decoupling guarantee restated:** a producer that stalls (e.g. a social API timeout in the
SLOW loop, or an enrichment provider down per FR-006/AC-007) only causes *its own* stream to lag and
hit its MAXLEN cap, dropping its oldest entries. It **cannot** back-pressure `launch.events` or the
SNIPE consumer group, because (a) streams are independent and (b) SNIPE reads a *cached* pre-staged
score from KV, never the live SLOW computation. Price/event processing is structurally insulated from
flaky off-chain dependencies. (ADR-0001 consequence; FR-021/023.)

### 5.2 Service inventory (each is a deployable in `infrastructure.md`)

1. `aats-hotcore` (Rust) — ingest + SNIPE + FAST. Holds the **pubkey only**; builds unsigned tx and
   submits signed bytes. **Does NOT hold the secret** (ADR-0009, red-team-2 must-fix #1).
2. `aats-signer` (minimal-surface, separate failure domain) — the **only** holder of the wallet
   secret; NO inbound network, NO untrusted-byte decode; exposes one local-socket `sign(tx, wallet)`
   with independent signer-side caps + full program-ID allowlist + Jito-tip-account pinning
   (infrastructure.md §5). This is the frozen custody seam.
3. `aats-slow` (Python) — SLOW loop, models, MCS, LLM router.
4. `aats-controlplane` (Python) — frozen API server (`api-contracts.md`).
5. `aats-dms` (Python, minimal) — dead-man's switch watchdog (external failure domain); holds
   pre-signed flattens produced via `aats-signer`.
6. `aats-telegram` (Python) — alert + de-risk command channel (Lane F).
7. `dashboard` (existing Vite app, Lane E).
8. `redis`, `prometheus`, `grafana`, `alertmanager` — infra.

---

## 6. Module breakdown M1–M5 (component → lane → task)

- **M1 Sensors** (`data-ingestion-engineer`, `feature-quant-engineer`, `nlp-sentiment-engineer`):
  transport/decode (T-300), enrichment registry (T-301), smart-money stream (T-302), completeness
  audit (T-303), feature pipeline + `smart_wallets_in` (T-304), first-K buy-pressure feature (T-305),
  MCS (T-306). Writes `launch.events`, `feature.frames`, the point-in-time store.
- **M2 Engine** (`ml-prediction-engineer`, `llm-reasoning-engineer`): ONNX snipe classifier (T-310),
  frozen baseline (T-311), TFT survivor + model-vs-baseline monitor (T-312), de-risk-only Reasoner
  (T-313). Pre-stages score+veto into KV.
- **M3 Controller** (`agent-orchestration-engineer`): triple loop + FSM (T-340), control-plane API
  (T-341), in-process survivable-stop enforcer + DMS heartbeat wiring (T-342).
- **M4 Guardrails** (`solana-execution-engineer`, `risk-guardrails-engineer`, `mev-latency-engineer`):
  breaker (T-320), survivable stop (T-321), DMS (T-322), safety gate (T-323), risk engine + ¼-Kelly
  + cost gate (T-324), ExitEngine (T-325), resting orders (T-326), JitoJupiterVenue dry-run (T-327),
  multi-wallet (T-328), sell-sim (T-329), latency+tips (T-330), MEV modes (T-331).
- **M5 Immunity** (`latency-devops-engineer`, `crypto-security-engineer`): scaffold + monitoring
  (T-250), custody policy (T-251), deploy (T-500). Plus the validation harness owned at architecture
  level (T-204) and run by `backtest-qa-engineer` (T-400/401).

---

## 7. How each EDGE-VERDICT condition C-1..C-13 is STRUCTURALLY enforced

Not "we'll remember to" — each is a structural property of the architecture, enforced by a type, a
build-time guard, a process boundary, or a frozen artifact. (Full wiring in `validation-harness.md`.)

| C | Condition | Structural enforcement (where) |
|---|---|---|
| **C-1** | Latency honesty | `latency-budget.md` separates internal compute (~67ms p50) from block-engine RTT (~55ms) from staked-lane leader-land (+1 slot p99). The +1-slot penalty is **propagated as a `buyers_ahead` right-shift and a widened adverse-selection haircut** into the cost model — not a footnote. Dashboard `/api/latency` shows the two columns separately (AC-050). |
| **C-2** | No inherited optimism | The R1/R2 recorded cost stack is built in the **clean-room harness** which does **not import** `venue.py._competitor_delay` or `exits.py` sandwich constants. The import guard (validation-harness.md) makes inheritance a **build failure**. Haircut is a FLOOR to widen, never a band to narrow. |
| **C-3** | Tip-cohort-bias kill | The TipController logs the **live tip floor at decision time** per candidate into the recorded store (`data-models.md` FeatureFrame). The harness stratifies GATE-A by tip-contention bucket (FR-047); low-contention-only profit BLOCKS R4. |
| **C-4** | Freeze + build baseline | The FeatureFrame schema **carries `first_K_buy_pressure_volume`** (data-models.md) so the naive-momentum baseline is constructible. The baseline params live in a **committed hashed config**; a test FAILS if they change after first fit (validation-harness.md, AC-056-adjacent / FR-015). |
| **C-5** | Clock + frozen haircut | Every FeatureFrame is stamped with **event-time only** (slot + block_time), never compute-time, and now carries a **per-feature provenance manifest** whose source-window cutoff is a build guard (data-models.md §3.3; ADR-0010). The harness runs a **global shifted-clock control that MUST change results** (AC-057) — relabeled **necessary-not-sufficient** — PLUS an **independent label-horizon + per-feature-lineage placebo** that catches a horizon-preserving leak the global shift hides (validation-harness.md C-5). The adverse-selection haircut is fit on **train folds only** and frozen across test folds; per-window re-fit = auto-FAIL. |
| **C-6** | Completeness audit | Ingestion reconciles against an **independent pool-create census**; un-snapshotted/un-labeled tokens are carried as **`status: CENSORED`** in the data model, never dropped (FR-009, AC-006, data-models.md Position/LaunchRecord). |
| **C-7** | Clean-room harness | The validation pipeline is a **separate package** with build-time guards. The **PRIMARY** anti-label-leak defense is now a **lineage/taint guard** (any feature whose lineage touches `labels/` FAILS the build, regardless of column name — ADR-0010, validation-harness.md §2.5); the `truth_*` name-scan is retained as a demoted belt-and-suspenders second line. Recall ≥0.50 is a **measured output**, never a parameter (AC-056). |
| **C-8** | R2 necessary-not-sufficient | Stated in `infrastructure.md` staging + `validation-harness.md`: R2/GATE-A is necessary-not-sufficient; first real haircut validation deferred to R3 fills; fill-probability modeled **conditional on outcome** so the stress test perturbs the correlation. |
| **C-9** | Experiment log + deflation | A **committed, append-only, hashed experiment log** is a **precondition** for computing GATE-A/GATE-B; the harness refuses to score without it (`experiment_log_missing_or_tampered`, AC-059). Significance deflation is a function of the logged trial count. |
| **C-10** | Group-purge | The data model carries **creator-wallet / bundler-cluster / deploy-template fingerprints** so the harness can group-purge across the embargo boundary and report metrics **with and without** (AC-058, FR-046). |
| **C-11** | Calibrated-haircut sub-gate | Haircut calibrated from **recorded R1 fills before GATE-A at R2** (FR-044); if calibrated >200 bps at target size, EH-001's midpoint is re-derived (validation-harness.md sub-gate). Pre-calibration default = **150 bps, labeled UNCALIBRATED** (OQ-007). |
| **C-12** | Regime + staleness | `infrastructure.md` carries the R2→R3 own-order market-impact caveat and a **proof-staleness bound** that auto-re-runs the gate on fresh data if drift breaks before funding. Drift monitor on launch-population distribution (FR-038). |
| **C-13** | Independent-surface reporting | The harness reports **how many EH surfaces survive independently** under the corrected competitor distribution; pooled-only survival is flagged as one fragile edge (FR-048). |

---

## 8. Asymmetric trust — enforced by types, not docstrings

The reasoning/LLM/social path can emit **only de-risk Intents**. The `Intent` union and the
`ReasoningVerdict` action enum (data-models.md) are constructed so that `SIZE_UP`, `WIDEN_STOP`,
`ADD_LEVERAGE`, `OVERRIDE_HARD_STOP` are **not expressible** — there is no variant, no field, no
constructor that produces them. The clamp (FR-017, AC-019) is the runtime backstop; the type system
is the primary defense. ¼-Kelly sizing (FR-032) is monotonic non-increasing in every secondary
signal (MCS, smart-money, LLM, uncertainty) — proven by AC-021/031. (ADR-0006.)

---

## 9. Survivable-stop architecture (three independent layers + failover)

A single point of failure on the stop is an automatic G1 reject. All three are designed; the seam is
split DEFINES (`risk-guardrails`) → IMPLEMENTS (`solana-execution` venue-native) → OPERATES
(`agent-orchestration` enforcer + DMS) per ROSTER §4.

1. **Layer 1 — Venue-native resting order / keeper** (FR-033, AC-025). A pre-signed keeper
   transaction (refreshed periodically) or venue-native resting stop where available, triggering on
   stop-price breach. Holds even if our process is busy.
2. **Layer 2 — In-process FAST-loop enforcer** (FR-033, AC-026). The FAST loop polls every tick and
   calls `ExecutionVenue.exit()` within ≤50ms p99 of breach. Holds while the process is alive.
3. **Layer 3 — Dead-man's switch** (FR-033, AC-045/046). External watchdog process; on heartbeat
   loss > **T_DMS = 60s (env var, OQ-006)** it submits **pre-signed flatten** transactions for all
   open positions. Holds even if the bot process is dead.

**Failover path:** Layer 2 is primary in steady state. If the FAST loop tick breaches or the process
dies, Layer 1's keeper covers the price breach on-chain independent of our liveness; if the *whole
host/network* partitions, Layer 3 fires after T_DMS. The DMS **cannot be disarmed** by an LLM,
market event, or risk update — only a valid heartbeat or explicit operator config (AC-046). The
breaker (FR-034) hands open positions to all three layers on trip. (ADR-0008.)

---

## 10. Cost-awareness by construction (no Intent without cost inputs)

An `Intent` **cannot be constructed** without the cost stack as inputs (data-models.md): the cost
gate (FR-027) computes `total_cost_bps = jito_tip_bps + priority_fee_bps + entry_slippage_bps +
amm_fee_bps + exit_slippage_bps + adverse_selection_bps` from the **live** tip cache, the registry
fee, the AMM slippage model, and the calibrated/UNCALIBRATED haircut — and refuses to emit an Intent
when `expected_edge_bps ≤ total_cost_bps` (AC-013). The Jito tip is read live (AC-014; hardcoded tip
= build-FAIL) and bounded by `min(market_floor, 0.30×edge)`. Adverse selection includes the
staked-lane +1-slot penalty (C-1). The dominant failure mode — fake edge — is architected against:
gross-edge claims are rejected on sight.

---

## 11. NON-goals of v1 (explicit, so engineers build no speculative lanes)

CEX live trading (ccxt is a dead stub, FR — `execution-venue.md`); block-0 / migration-block-0 race
wins (intentionally NOT attempted, SPEC §3); cross-chain / multi-chain; native mobile app (web
dashboard + Telegram only); automated capital scaling (CEO-gated R3/R4); any win-rate target or
claim; blind copy-trade mirror (smart-money is filter-only, EH-005 default ZERO); float money. Real
capital is **DISABLED by default** behind the DRY-RUN flag and stays so until edge is proven on
recorded data and the CEO explicitly authorizes R3.

---

## 12. Latency-floor honesty (where we win and where we do not)

- **We are detection-competitive** on `dedicated_geyser` (~60ms ingress) and ~67ms p50 internal
  compute — fully in budget, largely irreducible below ~60ms because Geyser ingress dominates.
- **We are submission-disadvantaged and cannot fix it in code.** The block-engine RTT (~55ms
  non-colo) is compressible by co-location (an infra spend, not code); the **staked-lane slot slip
  is irreducible for a solo desk** and requires a staked QUIC / SWQoS partnership (infra, owned by
  `latency-devops-engineer`).
- **We do not race block-0, migration-block-0, or tip auctions.** We win — *if* we win — on
  safety-selective late entry, migration-**survivor** selection, and exit discipline. That is the
  only place the numbers can come out positive net of cost. See `latency-budget.md` (C-1).

---

## 13. ADR index

| ADR | Decision |
|---|---|
| ADR-0001 | Message bus = Redis Streams (v1) with documented NATS JetStream migration path |
| ADR-0002 | Rust hot-core / Python split as **process boundary** (not PyO3), Redis as the membrane |
| ADR-0003 | Promote (not rewrite) the `sol-sniper` seams; `ExecutionVenue` seam is law |
| ADR-0004 | Clean-room validation harness + `truth_*` import guard (C-7) |
| ADR-0005 | FREEZE the control-plane API contract at G1; one contract for server/dashboard/Telegram |
| ADR-0006 | Asymmetric trust enforced by the `Intent`/`ReasoningVerdict` type system |
| ADR-0007 | Per-position FSM single-writer + write-ahead atomic snipe→fast handoff |
| ADR-0008 | Three-layer survivable stop + dead-man's switch failover |
| ADR-0009 | Isolated signer is a SEPARATE minimal-surface process (`aats-signer`), not in the hot core (G1 red-team) |
| ADR-0010 | Typed `LaunchOutcome` label dataset + per-feature provenance/lineage build guards (G1 red-team) |

---

## SELF-CHECK (all mandatory items)

1. **Every spec req maps to a component and back** — §1 table (forward) + SPEC §5 (reverse). PASS.
2. **Triple-loop diagram + budgets + back-pressure; FAST has zero LLM on its critical path** — §2,
   §2.1. Traced: FAST reads only KV scalars (atomic GET), never awaits LLM/SLOW/RPC. PASS.
3. **`Intent` cannot express risk-increase** — §8 + data-models.md (de-risk-only union pasted there). PASS.
4. **Survivable stop names all three layers + failover** — §9. PASS.
5. **Entry path proves cost-awareness before Intent** — §10; Intent un-constructable without cost
   inputs; `edge > cost` or NO TRADE. PASS.
6. **Point-in-time: FeatureFrame + Parquet event-time stamped; compute-time leakage prevented** —
   §7 C-5, data-models.md (event-time-only stamping + shifted-clock control). PASS.
7. **ExecutionVenue has Jupiter/Raydium/Simulation/dead-ccxt behind one interface; core imports the
   interface** — §3, execution-venue.md. SimulationVenue implements the 8 promoted members sim-native
   so it stays instantiable (R-01); `sign()` crosses to `aats-signer` (ADR-0009). PASS.
8. **Every significant decision has an ADR; post-G1 change carries a delta notice** — §13 + the
   post-G1 change protocol in each sibling doc. PASS.
9. **Latency-floor honesty stated** — §0, §12, latency-budget.md. PASS.

---

## 14. G1 red-team resolutions (each critique → fix or rebuttal) + delta notice

Three red-team lenses reviewed the G1-candidate blueprint. Two returned `blocksG1=true`
(leak-proofness, custody). Every blocking item is resolved below — the contract does not freeze with
an open blocker. Fixes are by construction (a type, a build guard, a process boundary), not by prose.

### Lens 1 — Leak-proofness-by-construction (backtest-qa) — BLOCKED → RESOLVED (ADR-0010)

| # | Critique (severity) | Resolution |
|---|---|---|
| 1A | The label has no typed contract, no `labels/` dataset, no horizon stamp, no build guard; the `truth_*` guard is name-based so a leaked label under an innocuous name passes (high) | **FIXED by construction.** Added the typed `LaunchOutcome` contract in its own event-time-partitioned `labels/` dataset (data-models §3A), produced only by the harness, joined to features by `event_time` only, forbidden from `feature_frames/` by a column-disjointness guard (§3.3 guard 3). The PRIMARY anti-leak defense is now a **lineage/taint guard** (`feature_lineage_touches_label`, validation-harness §2.5 guard 6): a feature whose lineage touches `labels/` FAILS the build regardless of its name. Name-scan demoted to belt-and-suspenders. |
| 1B | The shifted-clock control (C-5) is sold as the structural clock guarantee but is necessary-not-sufficient: a uniform global +1-slot shift preserves the relative label horizon, so a horizon-preserving leak survives (med) | **FIXED.** The global clock-shift control is **relabeled necessary-not-sufficient** in validation-harness.md C-5 and BLUEPRINT §7. Added an **independent label-horizon + per-feature-lineage placebo** (perturbs H and each feature's lineage individually, not a uniform global shift) so a horizon-preserving leak is detectable (validation-harness §C-5, must-fix #4). |
| 1C | `recorded_at` honesty is unconstrained; a backfill row with `recorded_at` set to the original event-time silently reintroduces lookahead (med) | **FIXED by construction.** data-models §9.2 now enforces `recorded_at_ms >= event_time.block_time_ms` at write (`recorded_at_before_knowable`), and the as-of-read audit flags any correction row whose `recorded_at` regresses below the latest already present for its key (`backfill_recorded_at_regression`, validation-harness §2.5 guard 8). The live-backfill lookahead vector is closed. |
| — | Per-feature event-time cutoff was a runtime audit, not a build guard (the umbrella must-fix) | **FIXED.** Each FeatureFrame now carries a `FeatureProvenance` manifest (one `FeatureSourceWindow` per feature, declaring `max_source_slot`); the build FAILS if any window exceeds `event_time.slot + K` (`feature_window_exceeds_cutoff`, §2.5 guard 5). Promoted from policed to prevented. |

### Lens 2 — Custody / signing / secrets (crypto-security) — BLOCKED → RESOLVED (ADR-0009)

| # | Critique (severity) | Resolution |
|---|---|---|
| 2A | The isolated signer is INSIDE the network-facing hot core; an RCE / poisoned decode dep yields the raw key (high) | **FIXED — topology reversed.** The signer is now a **separate minimal-surface process `aats-signer`** (no inbound network, no untrusted-byte decode) exposing only a loopback `sign(tx, wallet)`; the hot core holds the **pubkey only** and builds unsigned tx (infrastructure.md §5.1, BLUEPRINT §5.2, execution-venue.md §1, ADR-0009). Blast radius of a hot-core compromise drops from "the key" to "what the signer's policy permits." This reverses topology that was "law" → ADR-0009 + delta notice below. |
| 2B | No signer-side per-tx SOL spend cap; the cap lives in RiskConfig upstream of signing (high) | **FIXED.** The signer now independently enforces a **per-tx + rolling-aggregate/velocity SOL spend cap** (0.1 / 0.5 SOL, duplicated from RiskConfig deliberately) and REFUSES an over-cap tx — so a compromised loop cannot sweep the float in one/N signatures (infrastructure.md §5.2.1). |
| 2C | The signing allowlist is the venue registry only — under-inclusive (breaks legit txs) and leaves a System-transfer exfiltration path (med) | **FIXED.** The allowlist is enumerated as the **full set** (venue programs + SPL Token + ATA + ComputeBudget + System) AND every value-moving System transfer recipient is **pinned to the 8 live-verified Jito tip accounts** (`getTipAccounts` at boot — confirmed static set, Jito MEV docs) + own ATA-rent destinations (infrastructure.md §5.2.2/§5.2.3). |
| 2D | "Vault / env-injected" secret handling leaves an env-var path that puts the raw key in `/proc` / core dumps (med) | **FIXED.** The env-injected alternative is **removed**. The wallet secret is fetched via a **short-lived Vault token at boot**, held in **`mlock`-able memory**, **zeroized on exit**, NEVER in a static env var; the `.env` field is a Vault reference only (infrastructure.md §5.3, §9). |
| 2E | Telegram authz is a single user-ID allowlist; a user ID is not an auth secret (lower priority) | **FIXED.** infrastructure.md §5.4 states the user-ID check is necessary-not-sufficient, requires the **bot-token secrecy** as a custody secret (Vault reference, audited) and a **per-command operator confirmation for `/kill` `/flatten`** so a leaked `chat_id` alone cannot drive the de-risk channel (consistent with AC-042). |

### Lens 3 — Code-reviewer (control-plane / seam fidelity) — NON-blocking → ADDRESSED

| # | Critique (severity) | Resolution |
|---|---|---|
| R-01 | The promoted ABC adds 8 members; `SimulationVenue` "retained unchanged" would be abstract/uninstantiable (MAJOR, non-blocking) | **ADDRESSED.** execution-venue.md §2 + §6 and BLUEPRINT §3 now state the promotion task MUST implement the 8 members **sim-native** (`submit_mode=SIMULATION`, no-network `land()`, key-less `sign()`); "retained" = `execute()` semantics preserved, not body untouched. |
| R-02 | `execute()` "signature unchanged" should reflect `SwapIntent`→`EntryIntent` money promotion (MINOR) | **ADDRESSED.** execution-venue.md §6 + BLUEPRINT §3 reworded: **arity + return** unchanged; intent promoted to integer/Decimal money fields per data-models §0/§6.2. |
| R-03 | Lane-E transcription (AgentMode 4-value enum, money formatting, snake_case→camelCase `SnipeEvent` adapter), keep `VITE_USE_MOCK=true` green (tracking only) | **TRACKED, no contract change.** Already specified in api-contracts.md §2/§6/§13 and ADR-0005 as Lane-E (T-352) transcription tasks; the frozen wire contract is the source of truth, the mock branch stays green. No contract edit required — confirmed the dashboard `ENDPOINTS` paths in `api.ts` match the frozen set exactly (api-contracts §13). |

### Delta notice (the topology reversal in ADR-0009 was "law" — every affected task listed)

The signer-process split changes the frozen topology and the `sign()` seam. Per the post-G1 change
protocol, every affected board task:

- **T-327** (JitoJupiterVenue): `sign()` now crosses to `aats-signer` over a local socket; the venue
  holds the pubkey, not the key. Re-scope to call the signer, handle `SignerRefused`.
- **T-251** (custody policy): now implements the `aats-signer` process, the three signer-side refusals,
  Vault-token/`mlock`/zeroize secret handling, and Jito-tip-account pinning.
- **T-352a** (NEW — `aats-signer` service): the minimal-surface signer compose unit + loopback socket
  + peer-cred hardening. `latency-devops-engineer` builds; `crypto-security-engineer` audits.
- **T-500 / T-250** (deploy / scaffold): add the `aats-signer` compose unit and Vault wiring.
- **T-340/342** (controller / DMS): the DMS pre-signed flattens are produced through `aats-signer`.

The ADR-0010 label/provenance guards affect:
- **T-304 / T-305** (feature pipeline): each feature must emit a `FeatureSourceWindow` (provenance manifest).
- **T-400 / T-401** (harness): owns the `LaunchOutcome` `labels/` writer and the four §2.5 build/load
  guards + the label-horizon/per-feature placebos.
- **T-310 / T-311** (model / baseline): train/score join labels by `event_time` from `labels/` only.

`api-contracts.md` (the frozen control-plane wire) is **unchanged** by these resolutions — no endpoint,
field, or enum value moved; the dashboard reconciliation in §13 still holds. The contract is frozen.
