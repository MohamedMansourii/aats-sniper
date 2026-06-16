# AATS SNIPER SWARM — ROSTER, SEAMS & DISPATCH ORDER

The specialized swarm for the **Solana meme-coin ultra-sniper** project (brief:
[`.agency/00-brief/AATS-BRIEF.md`](../00-brief/AATS-BRIEF.md)). This file is the
orchestrator's source of truth for *who owns what* on this project and *in what order*
to dispatch. It augments the charter roster in `CLAUDE.md` §2 — it does not replace it.

---

## 1. Specialized experts (15) — `.claude/agents/`

| Agent | Model | Module | Owns (one line) |
|---|---|---|---|
| `quant-research-lead` | opus | Strategy (pre-G0 + edge oracle) | The honest GO/NO-GO edge verdict, success metrics, kill criteria, capital-staging, walk-forward methodology spec |
| `quant-product-analyst` | sonnet | Spec (G0) | SPEC, operator stories, **measurable** trading acceptance criteria, scope/non-goals |
| `solana-systems-architect` | opus | Architecture (G1) | BLUEPRINT, data-models, message contracts, `ExecutionVenue` iface, feature-store design, ADRs |
| `data-ingestion-engineer` | sonnet | M1 transport | Yellowstone/Geyser gRPC, pump.fun/Raydium decoders, Redis Streams bus, point-in-time feature store |
| `feature-quant-engineer` | sonnet | M1 features | RSI/MACD/BB + first-60s microstructure (LP, holders, sniper-cluster, tax), FeatureFrame assembly |
| `nlp-sentiment-engineer` | sonnet | M1/M2 MCS | Tier-A CryptoBERT filter + Tier-B batched LLM MCS with coordinated-shill→contrarian penalty |
| `ml-prediction-engineer` | opus | M2 quant | Fast snipe classifier (LightGBM→ONNX, single-digit ms) + slow-loop TFT survivor model, calibration |
| `llm-reasoning-engineer` | opus | M2 reasoning | The schema-enforced Reasoner, LLM router, **asymmetric-trust clamp**, sub-200ms veto path |
| `agent-orchestration-engineer` | sonnet | M3 controller | Triple loop, per-position FSM, Redis state, atomic snipe→fast handoff, **operator control-plane API** |
| `solana-execution-engineer` | sonnet | M4 venue | JupiterVenue/RaydiumVenue/SimulationVenue, sign via isolated signer, simulateTransaction, retries |
| `mev-latency-engineer` | opus | M4/M5 edge | Latency budget, Jito bundles + dynamic tip economics, sandwich avoidance, detection-path recommendation |
| `risk-guardrails-engineer` | opus | M4 risk | Hierarchical rule engine, survivable stops, circuit breaker, fractional-Kelly sizing, sub-10ms safety gate |
| `backtest-qa-engineer` | opus | QA (G3/G4) | Walk-forward + purged CV, lookahead audits, sim burn-in, **edge-vs-baseline gate**, SLA/latency tests |
| `latency-devops-engineer` | sonnet | M5 ops | Docker/Compose on co-located host, RPC strategy, Prometheus/Grafana/Alertmanager, the edge-proving metrics |
| `crypto-security-engineer` | opus | M5 security (G4) | Keypair custody (isolated signer, capped funding wallet), secrets, supply chain, prompt-injection audit |

## 2. Reused charter agents (their AATS role)

| Agent | AATS role on this project |
|---|---|
| `orchestrator` | Delivery lead. Builds the TASKBOARD from the brief + this roster, runs gates, reports to CEO. **Unchanged.** |
| `code-reviewer` | The G3 quality half (paired with `backtest-qa-engineer`). Apply the **AATS review brief** in §5 to every task. |
| `docs-delivery` | G5 docs: README, deploy/ops guide, kill-switch runbook, `DELIVERY.md`. Covers the "docs unowned" gap. |
| `frontend-engineer` + `uiux-designer` | The minimal **operator dashboard** that calls the control-plane API (kill / flatten / breaker-reset / state). |
| `devops-engineer`, `backend-engineer`, `mobile-engineer`, `security-engineer`, `product-analyst`, `solutions-architect` | Superseded for this project by their specialized counterparts above. Do not dispatch the generic version when a specialized one exists. |

> Note: `qa-engineer` (generic) is superseded by `backtest-qa-engineer` for all trading-logic
> verification. `security-engineer` is superseded by `crypto-security-engineer`.

## 3. Module → agent map

- **M1 Sensors** — `data-ingestion-engineer` (transport/decode/bus/store) · `feature-quant-engineer` (the math) · `nlp-sentiment-engineer` (MCS)
- **M2 Engine** — `ml-prediction-engineer` (models) · `llm-reasoning-engineer` (Reasoner)
- **M3 Controller** — `agent-orchestration-engineer` (loops, FSM, control-plane API)
- **M4 Guardrails** — `solana-execution-engineer` (venue) · `risk-guardrails-engineer` (rules) · `mev-latency-engineer` (speed/edge)
- **M5 Immunity** — `latency-devops-engineer` (deploy/monitor) · `crypto-security-engineer` (custody/audit)

## 4. Ownership seams (collaboration boundaries — NOT conflicts)

These responsibilities legitimately touch two or three agents. Ownership is split as
**DEFINES → IMPLEMENTS → OPERATES** so there is zero ambiguity at dispatch time:

| Seam | DEFINES | IMPLEMENTS | OPERATES / SUBMITS |
|---|---|---|---|
| MCS schema & value | `solana-systems-architect` (the contract shape) | `nlp-sentiment-engineer` (computes the score) | `llm-reasoning-engineer` (consumes it) |
| Honeypot/sellability sim | `risk-guardrails-engineer` (the gate rule) | `solana-execution-engineer` (the simulation primitive) | risk engine calls it pre-trade |
| Survivable stop | `risk-guardrails-engineer` (the rule + thresholds) | `solana-execution-engineer` (venue-native resting order) | `agent-orchestration-engineer` (in-process FAST-loop enforcer + dead-man's switch) |
| Compute-unit / tip pricing | `mev-latency-engineer` (tip economics) | `solana-execution-engineer` (sets CU limit in the tx) | `mev-latency-engineer` (submits the Jito bundle) |
| Detection path | `mev-latency-engineer` (recommends fastest source) | `data-ingestion-engineer` (decides + implements the subscription) | ingestion bus |
| Latency-budget doc | `solana-systems-architect` (owns the document) | `mev-latency-engineer` (feeds the ms-per-hop numbers) | — |

## 5. AATS code-review brief (hand this to `code-reviewer` on every task)

Beyond standard review, this system fails in domain-specific ways. Reject a change that:
1. Lets the **LLM increase risk** anywhere (up-signal, size-up, stop-widen, hard-stop override) — asymmetric-trust violation.
2. Puts an **LLM / slow model / unbounded RPC `await` on the FAST-loop critical path**.
3. Uses **`float` for money/lamports/token amounts** where precision matters (use integer base units / `Decimal`).
4. Has a **lookahead / compute-time leak** — any feature or label using data not available at event-time.
5. Has a **race** in the snipe→fast handoff, intent dedup, or FSM transition (must be atomic + write-ahead).
6. Holds, logs, or serializes a **private key** outside the isolated signer; or any secret in code/images/logs.
7. Enters when **expected edge < round-trip cost** (Jito tip + priority/CU fees + slippage), or skips the pre-trade safety gate.
8. Treats **ingested social text as instructions** rather than quoted untrusted data (prompt-injection).
Verdict pairs with `backtest-qa-engineer`: **both must PASS for G3.**

## 6. Dispatch order (orchestrator: build the TASKBOARD in these waves)

```
P0  quant-research-lead         → EDGE-VERDICT. If NO-GO, STOP and escalate to CEO. (pre-G0)
P1  quant-product-analyst       → SPEC + acceptance criteria                         ── G0 (CEO)
P2  solana-systems-architect    → BLUEPRINT + contracts + ExecutionVenue iface        ── G1 (CEO)  ← no code before this
P2.5 (parallel, after G1)  latency-devops-engineer → repo/Docker/CI scaffold
                           crypto-security-engineer → custody-policy.md (signer + capped wallet)
P3  BUILD LANES (parallel; each task gated G3 by code-reviewer + backtest-qa-engineer):
      Lane A:  data-ingestion-engineer → feature-quant-engineer ∥ nlp-sentiment-engineer
      Lane B:  ml-prediction-engineer → llm-reasoning-engineer
      Lane C:  solana-execution-engineer → risk-guardrails-engineer ∥ mev-latency-engineer
      Lane D:  agent-orchestration-engineer (integrates A–C; build last, against SimulationVenue)
P4  backtest-qa-engineer (full sim burn-in + edge-vs-baseline) + crypto-security-engineer audit ── G4
P5  latency-devops-engineer (deploy) + docs-delivery (README/runbooks)                          ── G5
P6  CEO acceptance                                                                              ── G6
```

**Iron rule reminder:** wire the **daily-loss circuit breaker, survivable stop, and dead-man's
switch first** (P3 Lane C), and prove the **model-vs-baseline** metric in P4, before any real
capital. If `quant-research-lead` returns NO-GO at P0, the correct deliverable is that finding —
not a live bot.
