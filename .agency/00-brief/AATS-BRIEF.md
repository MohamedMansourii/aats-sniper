# BRIEF — AATS: Solana Meme-Coin Ultra-Sniper (Autonomous Agent Trading System)

**Date:** 2026-06-15
**From:** CEO (direct instruction)
**Type:** Production software build — full G0–G6 gates apply.
**Note for the Orchestrator:** This is the active project brief. The earlier `BRIEF.md`
in this folder (Kooora research engagement) is a *completed, unrelated* engagement —
preserved for history, not in scope here.

**BUILD ON THE EXISTING FOUNDATION — do not restart from scratch.** A validated M4
simulation harness already exists at `./sol-sniper/` and the prior research run already
established the Solana realities, the winnable-edge verdict, and the next increments.
All of that is captured in **§7 below — read it before planning.**

---

## 1. Vision (CEO, restated)

Build a deployable autonomous trading bot that **snipes Solana meme coins** — detecting
new/early on-chain liquidity and acting inside the first blocks — and trades them through
a **Phantom wallet** via the **Jupiter swap API** and **Raydium**. The bot must predict
short-term moves, sense market narrative, reason over the two, execute with robust risk
management, and run unattended with monitoring and a kill switch.

The CEO is explicit that the goal is a system that can be **deployed and used**, that
**protects capital first**, and that **tells the truth about whether it has a real edge**
before any meaningful capital is scaled.

## 2. Locked decisions (do not re-litigate without a scope change)

These were decided with the CEO and carried in from the architecture preamble (the
"V2 Sniper Directive"). They are inputs, not open questions:

1. **Venue = Solana only.** Raydium (AMM v4 + CPMM) and pump.fun (bonding curve →
   Raydium migration) are the hunting ground. `ccxt`/CEX is a dead optional stub behind a
   pluggable `ExecutionVenue` interface — do not build a CEX-first bot.
2. **Execution surface = Phantom keypair + Jupiter (v6 / Ultra) + Raydium.** The bot
   builds, signs, and lands swap transactions programmatically.
3. **Triple-loop architecture.** SNIPE loop (ultra-fast, event-triggered entry within a
   millisecond latency budget) · FAST loop (deterministic <100ms — stop-loss / take-profit
   / OMS / reconciliation, never waits on an LLM) · SLOW loop (seconds–minutes — sense →
   predict → reason, MCS, scaling).
4. **Asymmetric LLM trust.** The reasoning LLM may only *reduce* risk (veto an entry, force
   an exit). It may never size up, widen a stop, add leverage, or override a hard stop.
5. **Survivable stops.** The stop must not depend on the bot being alive: venue-native
   resting order / keeper + in-process secondary enforcer + dead-man's switch.
6. **Point-in-time correctness** everywhere (event-time, never compute-time) — the guardrail
   against the lookahead bias that silently inflates every backtest.
7. **Adversarial sentiment.** Coordinated, low-account-age, high-synchronicity shilling
   *lowers* the Market Conviction Score (contrarian/risk signal), never raises it.
8. **Cost-aware.** Never enter when expected edge < (Jito tip + priority/CU fees + slippage
   + round-trip). The dominant failure mode is *absence of edge net of costs and adverse
   selection* — not a bug.
9. **Models output probabilities + uncertainty, never a point price.** The fast snipe model
   runs in single-digit-to-low-tens of ms (LightGBM/XGBoost or a tiny quantized MLP →
   ONNX/Rust); a heavy TFT is the slow-loop survivor brain only.

## 3. The five modules (scope)

- **M1 — Sensors:** Solana-native ingestion (Yellowstone/Geyser gRPC + pump.fun/Raydium
  decoders), quant + microstructure features, and the adversarial MCS sentiment pipeline.
- **M2 — Engine:** fast snipe classifier + slow-loop survivor model, and the schema-enforced
  LLM Reasoner.
- **M3 — Controller:** the triple loop, per-position FSM, shared Redis state.
- **M4 — Guardrails:** the `ExecutionVenue` implementations (Jupiter/Raydium/Simulation),
  Jito bundles + tip economics + sandwich avoidance, the hierarchical risk engine, the
  sub-10ms pre-trade safety gate, circuit breaker.
- **M5 — Immunity:** Docker/Compose on a co-located node, RPC strategy, Prometheus/Grafana/
  Alertmanager, keypair custody, the G4 security audit.

## 4. Definition of "done" (Gate G6 acceptance)

1. The bot runs end-to-end against a **SimulationVenue** (paper trading) and produces an
   honest, instrumented PnL report **net of modeled tips/fees/slippage**, with the
   model-vs-naive-momentum-baseline hit rate measured.
2. The **daily-loss circuit breaker, survivable stop, and dead-man's switch** are
   implemented and proven (QA fires them on demand).
3. The pre-trade safety gate rejects known honeypot/rug patterns in simulation.
4. Deployable: `docker compose up` on a single co-located host, secrets via `.env.example`
   (trade-only capped funding wallet — never main holdings), monitoring + alerting live.
5. Docs: README, deploy/ops guide, and the runbook for the kill switch.

## 5. The non-negotiable honesty clause

Per the Lead Quant's mandate: **wire the daily-loss circuit breaker and the
model-vs-baseline metric first**, fund with money the CEO can incinerate, and let the
system **prove the edge is real on small capital before scaling.** If the edge is not
demonstrable net of costs, the correct deliverable is that finding — not a bot trading live.

## 6. Execution

The specialized swarm and dispatch order are defined in `/AATS-SWARM.md` and
`.agency/04-plan/AATS-ROSTER.md`. Orchestrator: build the TASKBOARD from this brief and the
roster, run the gates, and bring the CEO only gate approvals (G0/G1/G2/G6) and
`NEEDS-CEO-DECISION` escalations.

---

## 7. Existing foundation & established realities (READ FIRST)

### 7.1 The code that already exists — extend it, don't rewrite it
`./sol-sniper/` contains a stdlib-only, validated **M4 simulation foundation** (`sniper_sim/`):
- `venue.py` — the `ExecutionVenue` seam + `SimulationVenue` (models the slot-delay landing
  race, Jito tip auction, constant-product slippage-with-co-buyers, min-out revert, exit
  sandwiching). **This seam is law** — the real `JitoJupiterVenue` drops in behind it unchanged.
- `safety.py` — the ordered pre-trade `SafetyGate` (local checks 1–5 in the hot path; sell-sim gates N+2+).
- `tips.py` — edge-bounded `TipStrategy` (never bids into a race it's priced out of).
- `exits.py` — `ExitEngine`: TP-ladder + trailing stop + hard stop + timeout, with Fast/Secure
  MEV modes (Photon-inspired). Already beats naive exits by ~+24% in sim.
- `amm.py`, `types.py`, `metrics.py` (the M5 scorecard), `demo.py` (3-scenario A/B harness).
Runs today: `python -m sniper_sim.demo`. The architect (`solana-systems-architect`) must treat
these as the starting contracts and the engineers productionize against them.

### 7.2 Solana realities established by prior research (carry as facts, do not re-derive)
- **Migration target is PumpSwap, not Raydium, by default.** pump.fun bonding curves now migrate
  to its own AMM (PumpSwap); some flow still touches Raydium/CPMM. The migration/AMM target is a
  **pluggable program ID**; decode against pump.fun + PumpSwap + Raydium AMM v4 + CPMM, and
  **verify live program IDs at build time — never hardcode a stale one into a hot path.**
- **Latency floor:** ≈20–70 ms internal is achievable on colo + ShredStream + local Geyser;
  the ~400 ms slot time is an irreducible floor; you **cannot beat an N+0 insider co-bundling
  with the LP-add.** **Jupiter is NOT on the block-0 path** — build the snipe buy as a direct
  AMM instruction against decoded pool keys; Jupiter (v6/Ultra) is for exits + survivors only.
- **Detection transports:** Yellowstone/Geyser gRPC is the baseline; **ShredStream** is the only
  pre-confirmation edge; enhanced WS (Helius/Triton/QuickNode) is slow-loop only. There is **no
  public mempool** (Jito discontinued it in 2024) — stop looking for a mempool firehose.
- **Hot path must be Rust** (snipe + fast loop). Python is for the slow loop, model training, the
  sim harness, and the control plane only.
- **Two sim priors to replace with recorded data:** the synthetic launch distribution and the
  emulated model skill. Run ingestion in **shadow/record mode** to capture first-K-slot snapshots;
  bootstrap labels with deterministic heuristics until a few thousand launches are recorded.

### 7.3 The winnable-edge verdict (from `quant-research-lead`'s domain, already concluded)
You will **lose the raw block-0 latency war** against co-located/staked/insider bots. The
defensible surface is: **(1) migration snipes** (predictable, pre-stageable), **(2) safety-selective
late entry** (slot +5..+30, win on rug-avoidance not speed), **(3) exit discipline** (where most
snipers die), **(4) coin-profile specialization.** Treat **smart-money / copy-trade as a
selectivity *filter and trigger*, never a blind mirror** (by the time their buy is on-chain you're
behind them). Re-confirm this verdict at P0; it can still return NO-GO.

### 7.4 Photon-inspired features to fold into the build (priority order)
1. **Productionize the `ExitEngine`** (TP-ladder + trailing + hard stop + Secure/Fast MEV exits) — biggest proven lever.
2. **Real `JitoJupiterVenue` exit path, dry-run first** (Jupiter v6 quote→swap, no-submit) — safest real thing to wire.
3. **Smart-money / copy-trade signal** — new M1 `accounts_subscribe` stream on profitable wallets +
   an M2 `smart_wallets_in ≥ N` feature; measure its lift on win-rate and net PnL in sim first.
4. **Multi-wallet execution** (anti-cluster detection + blast-radius caps) and limit/DCA resting entries.

### 7.5 The acceptance metric that governs scaling
Do not scale capital until **net-of-cost PnL AND snipe-model-vs-naive-baseline hit rate are BOTH
positive on small size**, on *recorded* data — not synthetic. The sim's absolute SOL figures are
illustrative; trust the **deltas** (infra tier, exit policy, gate quality), not the magnitudes.
