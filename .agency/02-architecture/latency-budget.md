# latency-budget.md — DETECTION-COMPETITIVE / SUBMISSION-DISADVANTAGED (T-205, C-1)

**Version:** 1.0.0 (G1 candidate)
**Owner of document:** `solana-systems-architect`
**ms-per-hop fed by:** `mev-latency-engineer` (embedded verbatim below)
**Date:** 2026-06-16
**Status:** This document satisfies condition C-1. It states the solo-desk floor in **plain numbers**,
**separates internal compute from block-engine RTT from the staked-lane leader-land hop**, names the
**extra-slot penalty**, and **propagates it as a `buyers_ahead` / adverse-selection input** to the
cost model — not as a footnote. The single-number "20–70 ms" claim in BRIEF §7.2 describes ONLY
internal compute; this ledger corrects the implied landing race that does not exist.

**Companion:** `BLUEPRINT.md §12` (where we win/lose), `validation-harness.md §C-1`,
`data-models.md` (FeatureFrame tip-contention + the cost stack), `api-contracts.md` (`/api/latency`,
AC-050), `infrastructure.md` (infra tier, the compressible spend).

---

## 1. The headline posture (one line, unambiguous)

**DETECTION-COMPETITIVE, SUBMISSION-DISADVANTAGED.** Internal compute (~67 ms p50 / ~135 ms p99,
ingress→submit_call) is fully competitive and largely irreducible below ~60 ms because Geyser ingress
dominates. **Submission is structurally lost for a solo desk:** the irreducible block-engine RTT
(~55 ms non-colo) plus the staked-lane slot slip put our buy a full slot behind a co-located staked
bot. We size infra to "detection-competitive," **never** to "landing-competitive."

---

## 2. Per-hop ledger (numbers from `mev-latency-engineer`, dedicated_geyser tier)

Three classes are kept separate (the C-1 mandate): **INTERNAL COMPUTE** (our code, compressible only
by ShredStream, which buys parity not edge), **SUBMISSION RTT** (network to the block engine,
compressible by co-location — an infra spend), and **LEADER-LAND** (the staked-lane hop, irreducible
for a solo desk).

| # | Hop | Class | p50 ms | p99 ms | Note |
|---|---|---|---|---|---|
| 1 | `ingress_detect` (Geyser/Yellowstone gRPC delivery of pool-init/migrate tx) | INTERNAL | 60 | 118 | dedicated_geyser: post/near-confirmation stream, mean 60ms ±~25ms jitter. NETWORK/transport-bound, not compute, and the dominant internal hop. ShredStream (colo_shred plug) replaces this with 50–200ms PRE-confirmation shred visibility — the only real detection edge, and it is TABLE STAKES in 2026, not an advantage. Geyser is detection-competitive for slot+5..+30 late-entry surfaces; NOT competitive for block-0. |
| 2 | `decode` (pump.fun/PumpSwap/Raydium account-data decode, pool keys + reserves) | INTERNAL | 1.2 | 3.5 | Rust hot-path decode against LIVE (never hardcoded) program IDs. Sub-ms typical; sits inside the lumped 6ms build block. |
| 3 | `gate` (sub-10ms 0-RPC pre-trade safety gate, checks 1–5) | INTERNAL | 2 | 6.5 | risk-guardrails local hot-path gate must pass inside this budget (its own <10ms SLA). 0-RPC local checks only; sell-sim/N+2 checks run OFF the hot path. Deterministic — NO LLM await. |
| 4 | `decide` (fast snipe classifier: LightGBM/quantized MLP via ONNX/Rust) | INTERNAL | 1.5 | 5 | Single-digit-ms calibrated-probability model. LLM NEVER here. TipController + ComputeBudget resolve from cached live tip_stream / getRecentPrioritizationFees percentiles — no network call in the hot path. |
| 5 | `build_sign` (direct AMM buy ix + tip ix; Ed25519 sign **via local-socket round-trip to `aats-signer`**; Jupiter NOT on block-0 path) | INTERNAL | 1.3 | 4 | Direct AMM ix against decoded pool keys (Jupiter v6/Ultra is exits/survivors only). CU limit set tight from simulateTransaction cache; tip ix bundled for atomic buy-with-revert. **The hot core builds the UNSIGNED tx and calls `aats-signer` over a loopback Unix-domain socket** (ADR-0009): a minimal-surface native signer, Ed25519 sign + signer-side cap/allowlist/tip-pin check, budgeted ≤1.5ms p99 added — a deliberate latency cost paid for custody isolation (the key is NOT in the hot core's address space). The p50/p99 here include that local round-trip. |
| 6 | `submit_call` (hand bundle to JitoBundleSubmitter, local serialize + enqueue) | INTERNAL | 0.8 | 2.5 | END of INTERNAL COMPUTE. Sum decode→submit_call ≈ 6.8ms p50 / 21.5ms p99 (matches sim's 6ms build block). INTERNAL TOTAL (ingress→submit_call) ≈ **67ms p50 / 135ms p99**. This is the ONLY thing BRIEF §7.2's "20–70ms internal" describes — NOT a landing time. |
| 7 | `block_engine_RTT` (node → regional Jito block-engine sendBundle, non-colo dedicated) | SUBMISSION | 55 | 95 | IRREDUCIBLE NETWORK hop, NOT compressible by our code. Non-co-located dedicated node → nearest regional block engine (amsterdam/frankfurt/ny/tokyo/slc) ~40–80ms same-continent cross-metro. **CO-LOCATING collapses this to ~1–5ms** — the single biggest compressible win, an INFRA spend, not a code change. |
| 8 | `leader_land` (block engine → leader TPU; staked QUIC vs contested unstaked lane) | LEADER-LAND | 50 | 450 | **THE LOSING HOP for a solo desk.** Jito parallel auction runs 50ms ticks; bundle must fit one slot (cannot cross slot boundaries). SWQoS reserves **80% of leader QUIC capacity for STAKED connections (~83% first-block hit)**; an unstaked solo desk fights for the contested **20% lane** and frequently **slips a FULL SLOT (~400ms)** under contention. p99=450ms reflects that extra-slot penalty. This hop is why the desk is SUBMISSION-DISADVANTAGED. |

---

## 3. The solo floor (in plain numbers)

- **detection (internal compute, ingress→submit_call):** ~**67 ms p50 / ~135 ms p99**.
- **submission RTT (node→block engine):** ~**55 ms p50 / ~95 ms p99** non-colo; **~1–5 ms** co-located.
- **extra-slot penalty:** **+1 full slot (~400 ms)** typical for an unstaked, non-co-located bundle
  under contention. SWQoS reserves 80% of leader QUIC for staked nodes; the solo desk lives in the
  contested 20% lane and slips to N+1 (or worse) on hot launches.
- **~400 ms slot quantum:** irreducible.

**Even the top sim tier (`colo_shred`) is submission-disadvantaged.** The sim places `colo_shred` at
`my_delay = ceil(29/400) = 1`, TIED with the 60% pro pack at N+1, while the 80% staked cohort lands
N+0. Co-location removes the ~55 ms block-engine RTT but does **NOT** close the staked-lane gap — only
a staked QUIC connection / SWQoS access does, and that is provisioned by `latency-devops-engineer`,
not coded.

---

## 4. Propagation into the cost model (the C-1 mandate — NOT a footnote)

The extra-slot penalty propagates **downstream as data**, two ways:

1. **`buyers_ahead` shifted RIGHT by ~one slot of staked/pro traffic.** The recorded cost stack must
   NOT inherit `venue.py._competitor_delay()` (15% N+0 / 60% N+1 / 20% N+2 / 5% N+3) — it understates
   reality by omitting the 60% of pros on the staked QUIC/SWQoS lane who land a full slot earlier
   (validation-harness.md C-2). The recorded `buyers_ahead` distribution is shifted right, increasing
   modeled entry slippage.
2. **Adverse-selection haircut re-designated a FLOOR to widen, never a band to narrow** (75–150 bps →
   **150 bps UNCALIBRATED default**, OQ-007), until live R3 fills measure it. If the calibrated
   haircut > 200 bps at target size, EH-001's midpoint is re-derived (C-11 sub-gate).

These two are inputs to `CostStack` in `data-models.md` and the cost gate (FR-027). The dashboard
`/api/latency` surfaces internal vs submission columns separately so the operator sees the honest
posture (AC-050).

---

## 5. Compressible vs irreducible (what `latency-devops-engineer` can and cannot buy)

| Item | Compressible? | How | Owner |
|---|---|---|---|
| ingress (60→18 ms) | yes — **parity, not edge** | ShredStream subscription | latency-devops |
| block-engine RTT (55→~3 ms) | yes — biggest single win | co-locate in the block-engine region | latency-devops |
| 400 ms slot quantum | **no** | — | physics |
| staked-lane slot slip | **no via code; yes via infra** | staked QUIC / SWQoS access or staked-node partner | latency-devops |

**LATENCY REQUIREMENTS handed to `latency-devops-engineer`:** (1) co-locate in the same region as the
chosen Jito block engine (amsterdam/frankfurt/ny/tokyo/slc) to kill the ~55 ms RTT; (2) provision a
STAKED QUIC connection / SWQoS access (or partner with a staked node) to move out of the contested 20%
lane — this, not code, closes the submission gap; (3) ShredStream subscription for ingress parity;
(4) region-pinned block-engine endpoint + tip-account rotation; (5) RPC SLA tight enough that
`getRecentPrioritizationFees` / `tip_stream` stay cached and never enter the hot path.

---

## 6. Honesty notes (carried verbatim from `mev-latency-engineer`)

- **C-1 SATISFIED:** internal compute (~67ms p50 / ~135ms p99) separated from block-engine RTT
  (~55ms non-colo / ~1–5ms colo) and the staked-lane leader-land hop (~50ms p50 → ~450ms p99 = +1
  full slot). The single "20–70ms" claim describes ONLY internal compute.
- **HOT PATH STAYS COLD OF THE LLM:** every hop decode→submit_call is deterministic; the snipe
  classifier is a quantized model, not a reasoning LLM. The LLM may only de-risk downstream.
- **Point-in-time honesty:** every hop p50/p99 is stamped to EVENT-TIME (the LP-add/migrate slot),
  never compute-time. The LatencyTracer uses monotonic-clock spans anchored to event-time ingress.
- **RACES THE SOLO DESK CANNOT WIN:** block-0 of any new pool (N+0 insider co-bundling the LP-add),
  migration-block-0 of PumpSwap (atomic migration-crank co-bundlers, confirmed 2026), and any pure
  tip-escalation auction (subsidizes validators; TipController caps tip at 0.30× edge).
- **THE WINNABLE NICHE is the INVERSE of the speed race:** slot+5..+30 safety-selective late entry
  and migration-SURVIVOR selection, where the race is clean tip+bundle execution and rug-avoidance,
  not raw shred latency. This matches EH-001/EH-003 and the EDGE-VERDICT §7 "where a solo operator
  cannot win" section.

---

## 7. Budget targets the build is held to (cross-ref SPEC)

| Metric | Target | Source |
|---|---|---|
| SNIPE internal (ingress→`execute()`) | ≤ 150 ms p99 / ≤ 50 ms p50 | NFR-001, FR-051, AC-016 |
| Fast snipe classifier | ≤ 5 ms p99 | FR-013, NFR-003 |
| Hard-stop trigger→`exit()` | ≤ 50 ms p99 | FR-052, AC-026 |
| FAST tick | ≤ 100 ms | FR-053, NFR-002 |
| Block-engine RTT | reported separately, NOT in the snipe budget | C-1, AC-050 |
| Geyser feed freshness | < 800 ms normal; > 1,200 ms → health alert + DMS degraded | FR-057, NFR-004 |
