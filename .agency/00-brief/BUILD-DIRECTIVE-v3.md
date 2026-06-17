# AATS BUILD DIRECTIVE v3 — DEPLOY-READY, FEATURE-COMPETITIVE, PAPER-PROVEN

**From:** CEO (this session) · **Logged by:** Agency Runtime · **Status:** ACTIVE build directive.
This is the authoritative scope overlay for the AATS ultra-sniper. Every dispatched agent must
read this file cold alongside `AATS-BRIEF.md`, `AATS-ROSTER.md`, and `CLAUDE.md §8`.

---

## North Star (read before anything)
The goal is an **honest, instrumented edge — NOT a win-rate number.** Do NOT target, tune toward,
or claim any fixed win rate (no "99%", no "80% wins"). A model-probability THRESHOLD is a gate,
never a promise of realized wins. The **single acceptance metric is net-of-cost PnL AND
model-vs-naive-baseline, both positive on RECORDED real data.** If the edge isn't there, the
deliverable is that finding — reported honestly — not a "winning" bot.

## Current state (EXTEND, do not rebuild)
- `./dashboard/` — premium dark operator UI, 10 pages, typed API client (`src/lib/api.ts`) mapping
  to `/state /feed /kill /flatten /breaker/reset /mode`. Build GREEN; runs on mock
  (`VITE_USE_MOCK=true`). Open items: no frontend tests; ~17 ESLint advisories; centralize
  `<Layout>`; shared chart-color module; fix the stale events/min counter on SnipeFeed.
- `./sol-sniper/` — validated M4 sim foundation (ExecutionVenue seam, SimulationVenue, SafetyGate,
  TipStrategy, ExitEngine, Metrics, demo). Runs: `python -m sniper_sim.demo`. **The seam is law.**

## Competitive feature target (match or beat GMGN / BonkBot / Trojan — assign each to a lane, surface each in the dashboard)
- Auto-sniper on new launch + **Migration sniper** (pump.fun→PumpSwap/Raydium) — core snipe loop.
- **Copy-trade / smart-money mirroring** — as a SELECTIVITY filter + trigger ("a proven wallet is
  in"), never a blind mirror (you're behind their fill). New M1 stream + M2 feature + dashboard page.
- **Limit + DCA resting orders** that fire even when the operator is offline — M4 resting-order module.
- **Auto take-profit ladder + trailing stop + "auto-strat" presets** — productionize ExitEngine;
  expose presets on Settings.
- **Multi-wallet / bundle execution** + partial fills + anti-cluster — M4 execution.
- **MEV protection** with fast vs secure (private) modes — M4 (already designed).
- **Token-safety scanner** (honeypot, mint/freeze authority, LP lock, dev/bundle, holder
  concentration) surfaced as red flags — risk gate + dashboard.
- **Token discovery enrichment:** DEXScreener / Birdeye / Meteora / Moonshot; keep the program-ID +
  venue registry PLUGGABLE and verified LIVE — M1.
- **Portfolio + P&L cards/export** — dashboard.
- **Telegram operator channel** — real-time alerts (fills, rugs avoided, breaker trips) + a
  CONSTRAINED command set (status / kill / flatten / pause) that can only DE-RISK. New Lane F.

None of these may violate a HARD RULE below; copy-trade and any social feed are adversarial input.

## The build, wave by wave (build ON `./sol-sniper` and `./dashboard`; honor every locked decision)

- **P0** `quant-research-lead` — re-confirm the edge verdict (GO / GO-PAPER-ONLY / NO-GO) and
  success metrics (§7.3/§7.5). **NO-GO HALTS the build → escalate.** On GO, continue.
- **P1** `quant-product-analyst` (G0) — numbered FRs/NFRs + MEASURABLE acceptance criteria for BOTH
  halves (latency budget per hop, land rate, honeypot-rejection rate, stop-fires-within-budget, FSM
  invariants, net-of-cost-PnL + model-vs-baseline gate) AND the competitive feature list above AND
  operator-UI criteria (every dashboard/Telegram control drives the real bot; kill flattens within
  budget; live feed reflects real events). Bring CEO G0.
- **P2** `solana-systems-architect` (G1) — blueprint promoting `./sol-sniper`'s seams to production;
  FREEZE the control-plane API contract that BOTH `agent-orchestration-engineer` (server) and the
  dashboard + Telegram channel (clients) build to (`/state /feed /kill /flatten/{mint}
  /breaker/reset /mode`); triple-loop topology, Rust-hot-path/Python boundary, message bus, typed
  contracts, pluggable ExecutionVenue + pluggable program-ID/venue registry (pump.fun + PumpSwap +
  Raydium v4 + CPMM + Meteora/Moonshot candidates, verified LIVE — never hardcode a stale ID in a
  hot path), the point-in-time feature store, and the docker-compose deploy topology (bot +
  control-plane API + dashboard + Telegram + Redis + Prometheus/Grafana). **NO code before CEO
  approves G1.**
- **P2.5 (parallel, after G1):**
  - `latency-devops-engineer` — repo/Docker/Compose scaffold (dashboard + Telegram as services), CI
    for Python + dashboard, Prometheus/Grafana/Alertmanager, `sniper_sim/metrics.py` promoted to real
    telemetry the dashboard reads.
  - `crypto-security-engineer` — `custody-policy.md`: isolated signer, trade-only CAPPED
    non-custodial hot wallet (never main holdings), Vault/secrets, `.env.example`, program-ID
    allowlist on signing, Telegram-command authz.
- **P3 BUILD LANES (parallel; each task gated G3 by `code-reviewer` + `backtest-qa-engineer`):**
  - **Lane A (M1):** `data-ingestion-engineer` (Yellowstone gRPC + ShredStream, pump.fun/PumpSwap/
    Raydium decoders, DEXScreener/Birdeye/Meteora enrichment, smart-money wallet stream, Redis
    Streams bus, point-in-time store, SHADOW/RECORD mode for real first-K-slot training data) →
    `feature-quant-engineer` (first-60s microstructure + survivor TA + `smart_wallets_in` feature)
    ∥ `nlp-sentiment-engineer` (MCS from crypto-native social/news, slow-loop only, adversarial)
  - **Lane B (M2):** `ml-prediction-engineer` (LightGBM→ONNX snipe classifier, leak-free label,
    survivor TFT, model-vs-baseline monitor) → `llm-reasoning-engineer` (schema-enforced
    de-risk-only Reasoner + sub-200ms veto; ingested narrative is untrusted data)
  - **Lane C (M4):** `solana-execution-engineer` (real JitoJupiterVenue behind the seam — direct-AMM
    snipe buy + Jupiter exits — DRY-RUN/no-submit FIRST; partial fills; multi-wallet) →
    `risk-guardrails-engineer` (hierarchical rules, survivable stops, daily-loss circuit breaker,
    sub-10ms safety gate, fractional-Kelly, resting limit/DCA orders, TP-ladder + trailing
    ExitEngine) ∥ `mev-latency-engineer` (latency budget, Jito bundles + edge-bounded tips, atomic
    buy-with-revert, private split exits, fast/secure modes)
  - **Lane D (M3):** `agent-orchestration-engineer` — three loops + single-writer FSM + atomic
    snipe→fast handoff + the control-plane API conforming EXACTLY to the P2 contract. Runs vs
    SimulationVenue first.
  - **Lane E (operator UI):** `frontend-engineer` + `uiux-designer` — FINISH `./dashboard` (don't
    rebuild): close open review items, add Vitest tests for destructive controls + key pages, then
    WIRE to the live control plane (`VITE_USE_MOCK=false`, live `/feed`) with mock as offline
    fallback; surface every new feature (copy-trade, resting orders, presets, P&L cards).
  - **Lane F (Telegram):** `backend-engineer` — the alert + constrained-command channel against the
    SAME control-plane contract (de-risk-only commands; authz from crypto-security).
  - Photon/competitor features fold in per priority: ExitEngine → JitoJupiterVenue dry-run →
    smart-money selectivity (measure lift in sim BEFORE trusting) → multi-wallet + limit/DCA.
- **P4 INTEGRATION (G4):** `backtest-qa-engineer` runs the full sim/paper burn-in, purged/embargoed
  walk-forward, lookahead leak audit, edge-vs-baseline gate on RECORDED data, AND an END-TO-END
  test: the running bot (paper) driven through the dashboard AND Telegram — kill flattens within
  budget, mode propagates, feed shows real events. `crypto-security-engineer` runs the security +
  custody + LLM-prompt-injection audit. Both PASS.
- **P5 RELEASE (G5):** `latency-devops-engineer` delivers ONE `docker compose up` (bot +
  control-plane + dashboard + Telegram + Redis + monitoring) + the colocation/RPC/staked-connection
  plan; `docs-delivery` writes README, deploy/ops guide, dashboard + Telegram operator guides,
  kill-switch runbook, and the staged-rollout guide (sim → shadow/record → paper/dry-run →
  tiny-real → scale).
- **P6 Deliver for acceptance (G6):** one command brings the whole system up; the dashboard AND
  Telegram drive the bot live in PAPER; attached is the HONEST edge report (net-of-cost PnL +
  model-vs-baseline on recorded data) with NO win-rate marketing number.

## HARD RULES (reject any change that violates these)
- **HONESTY:** never target/tune-toward/claim a fixed win rate; the only success metric is
  net-of-cost PnL + model-vs-baseline on recorded data. A probability threshold is a gate, not a
  promise.
- **REAL CAPITAL IS DISABLED by default** and stays behind a hard DRY-RUN/paper flag until the CEO
  EXPLICITLY authorizes it AFTER edge is proven on recorded data; first live capital is a capped
  throwaway wallet the CEO can lose entirely. No real keys ever in code/logs/images — `.env.example`
  only.
- **Safety FIRST in build order:** daily-loss circuit breaker, survivable stop, dead-man's switch
  built and proven BEFORE any live-capable path is enabled.
- **Dashboard + Telegram are operator surfaces over ONE control-plane contract;** their commands may
  only DE-RISK; both must be demoed driving the bot end-to-end; dashboard still builds green on mock.
- **Asymmetric trust:** no rule, no LLM, no copy-trade signal may EVER increase risk (no size-up,
  stop-widen, hard-stop override). LLM never on the FAST-loop critical path.
- **Copy-trade and all social/news inputs are ADVERSARIAL:** selectivity filters only, never blind
  mirrors or buy triggers on their own.
- **Point-in-time correctness** everywhere; any lookahead/compute-time leak fails review.
- **Snipe + fast hot path is RUST;** Python never touches the block-0 path.
- **Money is integer base units / Decimal, never float** (backend AND dashboard).
- **Verify by execution:** every "done" carries the command run + output seen (`npm run build`,
  `tsc -b`, `npm test`, `pytest`, `python -m sniper_sim.demo`, `docker compose up`).

## How this build is run
- Dispatch the specialized agents per the roster. Use multi-agent Workflow orchestration to run
  independent lanes in parallel and to adversarially verify high-risk work; git-worktree isolation
  when parallel agents touch the same files.
- Enforce every gate. After each handoff, re-dispatch the orchestrator to verify against acceptance
  criteria and update `.agency/04-plan/TASKBOARD.md`. No verdict, no progress.
- Work AUTONOMOUSLY. Stop only for CEO gates — G0 scope, G1 architecture, G2 design, G6 acceptance —
  and any `NEEDS-CEO-DECISION` with 2–3 options + a recommendation. G3/G4/G5 are the Runtime's to run.
