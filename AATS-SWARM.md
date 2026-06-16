# AATS Sniper Swarm — Deployment & Usage Guide

Your expert agent swarm for building the **Solana meme-coin ultra-sniper** is installed and
ready. This guide tells you what you have, how it's already deployed, and how to drive it to
build the project end-to-end.

---

## What you have

**18 agents total** = 15 new domain experts + 3 reused governance agents, all living in
`.claude/agents/*.md`. They operate under your existing charter (`CLAUDE.md`): the main Claude
session is the **Agency Runtime** (it dispatches agents, never writes code itself), and every
deliverable flows through the gates **G0–G6**.

| Lane | Agents | Tuned model |
|---|---|---|
| **Strategy / Gov** | `quant-research-lead`, `quant-product-analyst`, `solana-systems-architect` | opus / sonnet / opus |
| **M1 Sensors** | `data-ingestion-engineer`, `feature-quant-engineer`, `nlp-sentiment-engineer` | sonnet ×3 |
| **M2 Engine** | `ml-prediction-engineer`, `llm-reasoning-engineer` | opus ×2 |
| **M3 Controller** | `agent-orchestration-engineer` | sonnet |
| **M4 Execution / Risk** | `solana-execution-engineer`, `mev-latency-engineer`, `risk-guardrails-engineer` | sonnet / opus / opus |
| **M5 OpSec** | `latency-devops-engineer`, `crypto-security-engineer` | sonnet / opus |
| **Quality** | `backtest-qa-engineer` (+ reused `code-reviewer`, `docs-delivery`, `orchestrator`) | opus |

The **Opus** roles are the ones where a subtle mistake is expensive — the edge verdict, the
architecture, the models, the reasoning clamp, the MEV/latency edge, the risk engine, the
backtest-leak hunt, and key custody. The **Sonnet** roles are the well-scoped build lanes.
Every model assignment is just the `model:` line in each agent file — change any of them freely.

Full operational detail (ownership seams, code-review brief, dispatch waves):
[`.agency/04-plan/AATS-ROSTER.md`](.agency/04-plan/AATS-ROSTER.md).
The project brief the swarm builds against: [`.agency/00-brief/AATS-BRIEF.md`](.agency/00-brief/AATS-BRIEF.md).

---

## How it's deployed

Nothing to install. Claude Code auto-loads every `.md` file in `.claude/agents/` as a callable
subagent at session start. They're already there. To confirm in an interactive terminal you'd run
`/agents`; in this app they're simply available to the Runtime as dispatch targets.

---

## How to use it — three ways

### 1. Run the whole pipeline (recommended)
Just tell the Runtime to start. For example:

> **"Start the AATS build. Dispatch the swarm per `.agency/04-plan/AATS-ROSTER.md`."**

The Runtime will dispatch `quant-research-lead` first (the edge gate), bring you the **G0**
scope and **G1** architecture approvals, then run the parallel build lanes — each task verified
by `code-reviewer` + `backtest-qa-engineer` before it counts as done. You'll be brought in only
for gate approvals (G0/G1/G2/G6) and `NEEDS-CEO-DECISION` escalations.

### 2. Run one stage at a time
Ask for a single wave, e.g. *"Run P0 only — I want the honest edge verdict before we spend a
dollar on the build,"* or *"Dispatch the architect for the blueprint and stop at G1."*

### 3. Call one expert directly
Ask the Runtime to put a specific agent on a focused job, e.g. *"Have `mev-latency-engineer`
produce the detection→land latency budget and the Jito tip strategy,"* or *"Have
`risk-guardrails-engineer` build the circuit breaker first."*

---

## The build sequence (what will happen)

```
P0   quant-research-lead   → honest GO / NO-GO edge verdict      ← a NO-GO halts the project
P1   quant-product-analyst → spec + measurable acceptance        ── G0  (you approve)
P2   solana-systems-architect → blueprint + contracts            ── G1  (you approve) — no code before this
P2.5 latency-devops (scaffold) ∥ crypto-security (custody policy)
P3   build lanes in parallel, each task gated G3 (code-reviewer + backtest-qa):
        A ingestion → features ∥ sentiment
        B models → reasoner
        C execution → risk ∥ mev/latency
        D orchestration (integrates everything, against the paper-trading SimulationVenue)
P4   full sim burn-in + edge-vs-baseline + security audit         ── G4
P5   deploy (Docker/Compose, monitoring) + docs/runbooks          ── G5
P6   you accept the delivery package                              ── G6
```

---

## How you'll deploy and run the finished bot

The swarm builds toward a `docker compose up` deployment on a single co-located host:
- **Paper first.** It runs end-to-end against a `SimulationVenue` and reports honest PnL **net of
  modeled tips/fees/slippage**, plus the model-vs-baseline hit rate. You scale capital only after
  the edge is proven on small money.
- **Secrets.** A `.env.example` documents every key. You supply a **trade-only funding wallet with
  a capped balance** — never your main Phantom holdings. The private key lives in an isolated
  signer process, never in code or logs.
- **Safety on by default.** Daily-loss circuit breaker, survivable (venue-native) stop, and
  dead-man's switch are built and proven *first*. An operator control plane exposes
  `kill` / `flatten` / `breaker-reset` / `state`.
- **Monitoring.** Prometheus + Grafana + Alertmanager page you on a dead feed, a failed land, or
  position drift.

---

## The one thing to remember

Per your Lead Quant's mandate: **the architecture makes a real edge tradeable — it cannot
manufacture one.** The swarm is built to *measure honestly* whether you can beat the faster,
better-funded snipers on the niches they ignore — and to tell you the truth, including "no," before
you risk capital. Wire the circuit breaker first, fund it with money you can incinerate, and let it
prove the edge before you believe it.

> To start: **"Start the AATS build per the roster."**
