# AATS — Solana Meme-Coin Ultra-Sniper (PAPER build)

> **Read this first.** AATS is an autonomous Solana meme-coin sniping system that
> currently runs in **PAPER / DRY-RUN mode only**. Real capital is **DISABLED by
> default and cannot be enabled** until a documented pre-live checklist clears.
> The system's edge on live mainnet is **UNPROVEN** — no recorded mainnet data
> exists yet. This is the honest, verified state of the build, not a marketing claim.

---

## 1. What this IS (and what it is not)

**What it IS — today:**

- A full, deployable **triple-loop trading system** (SNIPE → FAST → SLOW) that detects
  early Solana liquidity events, scores them, applies hard risk guardrails, and trades
  them against a **`SimulationVenue`** (paper) or builds-and-signs-but-does-not-submit
  against the real venue (`LIVE_DRY_RUN`).
- Driveable end-to-end by an operator via a **dashboard** and a **Telegram** channel,
  both of which can **only de-risk** (kill, flatten, pause, tighten) — never increase risk.
- Backed by three independent, **proven-by-firing** safety primitives: a daily-loss
  **circuit breaker**, a three-layer **survivable stop**, and a **dead-man's switch (DMS)**.
- Honestly instrumented: the *only* performance metrics are **net-of-cost PnL** and
  **model-vs-naive-baseline** delta. There is **no win-rate** field anywhere — by design.

**What it is NOT — today:**

- **Not** trading real money. `DRY_RUN_ENABLED=true` is the default; no real-submit path
  is reachable while it is set (verified — see §6). The trade wallet is unfunded.
- **Not** a proven edge. The favorable numbers in the simulation harness are direction
  indicators from synthetic data, not evidence of live profitability. Live edge is
  `UNPROVEN-NO-REAL-DATA` until proven on **recorded** mainnet data (GATE-A + GATE-B,
  see §4).
- **Not** a block-0 latency racer. Per the edge verdict, a solo desk *cannot* win the
  block-0 / migration-block-0 race against co-located, staked, insider co-bundlers. The
  defensible surface is **selection + exit discipline**, not speed.

The governing edge verdict is **GO-PAPER-ONLY** — build the paper/record system, prove the
edge on recorded data with real capital disabled, and treat "no edge net of cost" as a
*successful* outcome. Full reasoning: `.agency/01-specs/EDGE-VERDICT.md`.

---

## 2. Quick start (one command, paper/DRY-RUN)

Prerequisites: **Docker** (tested with 29.x) and **Docker Compose v2**. Nothing else is
required to bring up the paper system — there are no real secrets to provide for a paper run.

```bash
git clone <repo-url> aats && cd aats
cp .env.example .env          # placeholders only — the paper defaults work as-is
docker compose up             # builds + starts the full topology in DRY-RUN/paper
```

What comes up (all on one host):

| Service           | Port  | What it is                                                      |
|-------------------|-------|----------------------------------------------------------------|
| `dashboard`       | 3000  | Operator command deck (builds **green on mock** by default)    |
| `aats-controlplane` | 8787 | Frozen de-risk-only control-plane API                          |
| `prometheus`      | 9090  | Metrics                                                         |
| `grafana`         | 3001  | Dashboards (GATE-A / GATE-B; **no win-rate panel**)            |
| `alertmanager`    | 9093  | Breaker / fill / staleness alerts                              |
| `aats-hotcore`    | —     | Rust SNIPE + FAST loops (holds the **pubkey only**)            |
| `aats-signer`     | —     | Isolated signer (holds the secret; **no inbound network**)     |
| `aats-slow`       | —     | Python SLOW loop (models, MCS sentiment, de-risk LLM)          |
| `aats-dms`        | —     | Dead-man's switch (separate failure domain)                    |
| `aats-telegram`   | —     | Alert + de-risk command channel (offline unless configured)    |
| `redis`           | —     | Message bus + state (internal only)                            |

Open the dashboard at **http://localhost:3000**. With the defaults (`VITE_USE_MOCK=true`)
it renders a full, realistic telemetry stream with no backend required — this is the
fastest way to see every page green. To wire the dashboard to the live control plane, set
`VITE_USE_MOCK=false` and `VITE_CONTROL_PLANE_URL=http://localhost:8787`.

> **The default state after `docker compose up` is safe:** mode = `SHADOW`,
> `DRY_RUN_ENABLED=true`, wallet unfunded, no real submit path reachable. You cannot
> accidentally trade real money from this state.

### Verifying it works without Docker

The Python control plane and safety core run directly:

```bash
pip install -r requirements/requirements.txt
python -c "from aats.control_plane.server import build_app; print('control-plane app OK')"
python -m pytest tests/e2e/test_t402_operator_demo.py -q   # the operator + safety demo
```

The dashboard runs standalone on mock data:

```bash
cd dashboard && npm install && npm run dev   # http://localhost:3000, no backend needed
```

---

## 3. Architecture overview

```
            ┌────────────────────────── one co-located Linux host ───────────────────────────┐
 RPC/Geyser │  aats-hotcore (Rust)         aats-signer (Rust)        aats-slow (Python)        │
 ShredStream│   ingest + SNIPE + FAST  ──▶  sign() over local      SLOW loop: models,          │
 ──────────▶│   holds PUBKEY only          Unix socket; holds      MCS sentiment, de-risk LLM  │
            │        │   ▲                  SECRET; NO inbound net       │                      │
            │        ▼   │ signed bytes         ▲ Vault token            ▼                      │
 Jito BE ◀──│      Redis Streams + KV state ◀───┴───────────────────────┘                      │
            │        │                                                                          │
            │   aats-controlplane (frozen API)   aats-dms (pre-signed flattens, own domain)    │
            │        │                                    │                                     │
            │   dashboard / Telegram  ───── de-risk only ─┘   Prometheus → Grafana / Alertmgr   │
            └──────────────────────────────────────────────────────────────────────────────────┘
```

**The three loops (BLUEPRINT §3):**

- **SNIPE** (ultra-fast, event-triggered): detect a launch/migration → 0-RPC safety gate →
  calibrated model probability → cost gate → entry. Rust. Never waits on an LLM.
- **FAST** (deterministic, <100 ms): stop-loss / take-profit ladder / OMS / reconciliation /
  survivable-stop enforcement. Rust. Never waits on an LLM.
- **SLOW** (seconds–minutes): sense (MCS adversarial sentiment) → predict (survivor model) →
  reason (schema-enforced de-risk LLM). Python. **May only reduce risk** — a veto or an exit,
  never a size-up.

**Load-bearing design rules (all enforced in code, audited at G4):**

- **Asymmetric trust by type.** The LLM's `ReasoningAction` enum has *only* four de-risk
  members; size-up / widen-stop / add-leverage are **inexpressible** — not just discouraged.
- **Survivable stops.** The stop does not depend on the bot being alive: a venue-native /
  in-process enforcer **plus** a separate-failure-domain dead-man's switch that fires
  pre-signed flattens even if the hot core process is dead.
- **Isolated signer (ADR-0009).** The secret lives in `aats-signer` (no inbound network, no
  decoders). The hot core holds only the pubkey. A full RCE on the hot core yields *signing
  requests bounded by signer policy*, never the raw key.
- **Money is integer lamports / `Decimal`, never float**, on every wire field and in every
  calculation.
- **Point-in-time correctness everywhere** (event-time, never compute-time) — the guardrail
  against the lookahead bias that silently inflates backtests.

**Stack:** Rust (hot path), Python 3.11 (slow loop, models, control plane, sim), React 18 +
Vite + TypeScript + Tailwind (dashboard), Redis (bus + state), Prometheus / Grafana /
Alertmanager (monitoring), Docker Compose (deploy), Vault (secret custody at deploy).

---

## 4. The honest framing (how success is measured)

There is **no win-rate target, field, or panel** anywhere in this system. A high win-rate is
trivially manufactured by holding losers and clipping winners; it is not evidence of edge.
The two — and only two — acceptance metrics are:

- **GATE-A — Net-of-cost PnL > 0.** Realized PnL minus the full round-trip cost stack (Jito
  tip + priority/CU fee + entry slippage + AMM fee + exit slippage/sandwich + an
  adverse-selection haircut), with a **lower 95% bootstrap bound > 0** over purged/embargoed
  walk-forward windows. A point estimate is not enough.
- **GATE-B — model beats the naive-momentum baseline.** The snipe classifier's selected-cohort
  net-PnL-per-unit-risk must beat a *frozen* naive-momentum baseline by a margin whose lower
  95% bound > 0. *If the model cannot beat dumb momentum net of cost, there is no model.*

**Both gates must pass on RECORDED mainnet data — never synthetic.** This build has the gates
**built and proven to compute correctly** (right sign on both controls, declines contribute 0,
net-of-cost, deterministic, clean-room/leak-guarded). But it has **no recorded data**, so the
honest edge verdict is `UNPROVEN-NO-REAL-DATA` — which is the **correct, accepted** outcome for
a paper deliverable. See `.agency/05-reports/qa/T-401-edge-proof.md` and the G4 verdict
`.agency/05-reports/gates/G4-PASS.md`.

---

## 5. Project structure

```
aats/                     # Python package — the bot
  contracts/              # shared typed contracts (import-only seam; FROZEN wire schema)
  ingestion/              # M1 — Geyser/ShredStream transport, decoders, point-in-time store
  features/               # M1 — quant + microstructure + buy-pressure features
  sentiment/              # M1 — adversarial MCS sentiment (shilling LOWERS conviction)
  models/                 # M2 — snipe classifier, survivor model, GATE-A / GATE-B harness
  reasoning/              # M2 — schema-enforced de-risk-only LLM reasoner + clamp
  controller/            # M3 — triple loop, per-position FSM, shared state
  control_plane/          # M3 — frozen de-risk-only API server (the operator contract)
  execution/              # M4 — JitoJupiterVenue (DRY-RUN-first), sell-sim, signer client, multi-wallet
  risk/                   # M4 — circuit breaker, survivable stop, DMS, cost model, exit engine, Kelly sizing
  mev/                    # M4 — Jito tips (edge-bounded), bundle submit, latency tracer
  telegram/               # M5 — alert channel + de-risk command bot
  dms/                    # M5 — dead-man's switch entrypoint
  telemetry/              # M5 — Prometheus metrics
dashboard/                # operator command deck (React/Vite; builds green on mock)
sol-sniper/               # the original validated M4 simulation harness (the seam is law)
rust/                     # aats-hotcore + aats-signer (hot path + isolated signer)
docker/                   # per-service Dockerfiles
monitoring/               # Prometheus + Grafana + Alertmanager config
config/                   # program-allowlist.json (signer least-privilege data)
docs/                     # deploy/ops guide, operator guides, runbooks, pre-live checklist
.agency/                  # the agency's specs, architecture, plans, gate reports
.env.example              # the ONLY committed secret artifact — placeholders, never real values
docker-compose.yml        # one `docker compose up` brings up the whole topology
```

---

## 6. Safety posture, verified

These are not aspirations — they are verified in this build by running the code:

| Property | Verified how | Result |
|---|---|---|
| Venue defaults to DRY-RUN | `JitoJupiterVenue(...).submit_mode` | `DRY_RUN` |
| Operator demo (kill/flatten/safety) | `pytest tests/e2e/test_t402_operator_demo.py` | **16 passed** |
| Risk + safety primitives | `pytest tests/risk` | **315 passed** |
| Execution / DRY-RUN gating | `pytest tests/execution` | **171 passed, 2 skipped** |
| Edge harness + leak guards | `pytest tests/validation` | **22 passed** |
| Dashboard builds green on mock | `VITE_USE_MOCK=true npm run build` | **✓ built** |
| Compose topology validates | `docker compose config` | exit 0 |

The full consolidated suite is **green at 1842 passed / 2 skipped / 0 failed** (verified post-G4-fix;
the 2 skips are the solders-gated execution tests `tests/execution/test_tx_builder.py:161/:186`). The
suite was first proven stable at 1803 / 2 / 0 bit-for-bit across 10 deterministic runs
(`.agency/05-reports/gates/G3-stabilization.md`); the G4-fix wave added the breaker→StateStore
projection test, its production change, and the frozen-clock concurrent test (+39).

---

## 7. Documentation map

| Doc | Read it when |
|---|---|
| `docs/deploy-ops-guide.md` | Deploying, configuring env, monitoring, the staged-rollout ladder |
| `docs/dashboard-operator-guide.md` | Driving the bot from the dashboard; what each page means |
| `docs/telegram-operator-guide.md` | Driving the bot from Telegram; the de-risk command set |
| `docs/kill-switch-runbook.md` | Stopping the bot fast; what fires automatically; recovery |
| `docs/pre-live-checklist.md` | **Before** `DRY_RUN_ENABLED=false` — every item that must clear |
| `.agency/01-specs/EDGE-VERDICT.md` | Why this is GO-PAPER-ONLY and where a solo desk cannot win |
| `.agency/02-architecture/` | Blueprint, frozen API contract, infrastructure, ADRs |

---

## 8. The one rule that governs real capital

**Real capital stays DISABLED behind `DRY_RUN_ENABLED=true` until the pre-live checklist in
`docs/pre-live-checklist.md` clears in full** — edge proven on recorded data (GATE-A + GATE-B),
the signer-side custody refusals built and test-proven, image/host hardening done, and the
CEO's explicit legal + funding authorization. Enabling live without that is a hard-rule
violation, not a configuration choice. If the recorded-data gate fails, the correct deliverable
is the finding "no edge net of cost" — and that is a successful project outcome.
