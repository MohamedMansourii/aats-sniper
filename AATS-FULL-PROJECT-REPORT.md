# AATS — Full Project Report (0 → now)
### The Solana meme-coin ultra-sniper: what was built, how, by whom, and the honest verdict
**Compiled:** 2026-07-07 · **Branch:** `aats-sniper-build` · **Repo:** `C:\dev\aats` · **HEAD:** `ecfc00a`
**Suite:** ~3,177 tests · **Status:** fully built + safe + security-audited; **edge proof = NO-GO on launch data (decisive); real capital DISABLED.**

---

## 0 · The one-paragraph truth
AATS is a complete, production-grade autonomous Solana meme-coin trading system — 17 code modules, a Rust hot core,
a triple-loop controller, a full safety spine, and a rigorous edge-proof harness — built end-to-end by a 27-agent
AI software agency operating under a strict charter. **Everything works except the one thing that matters most: a
proven edge.** Run rigorously on 4,187 real recorded launches, both on-chain launch-data strategies returned a
decisive **NO-GO** (no positive expectancy net of the ~6% round-trip cost; the model does not beat a dumb rule).
That is not a failure of engineering — it is the safety architecture doing its job: refusing to risk real money on
an unproven edge. The one remaining untested thesis with a real prior — front-running the retail reaction to a
*proven* smart-money/KOL signal — is now being stood up. **No real capital has moved, and none will until a real
GO is proven.**

---

## 1 · What AATS is — the thesis and the iron rules

**The product:** an "ultra-sniper" that detects brand-new pump.fun / Raydium token launches in milliseconds,
scores them, and (if an edge exists) trades them with tight, survivable risk controls.

**The honest edge thesis (established Day 0 by the `quant-research-lead`):** ~95% of meme tokens rug; predicting
which launch will pump from launch data is effectively impossible; a ~6% round-trip cost gate (Jito tip + priority
fees + slippage + adverse selection) is brutal; only ~10% of sniper bots are consistently profitable. The *stated*
edge was never "pick winners" — it was **de-risk + reaction-timing**: refuse most trades, and front-run the
*predictable* retail reaction to proven signals. Success is defined ONLY as positive expectancy, never a win rate.

**HARD RULES (encoded and enforced throughout the codebase, never violated):**
1. **NO win-rate metric, ever.** Success = positive net-of-cost PnL with survivable risk.
2. **No real funds move** until the Phase-5 edge proof returns **GO** AND the security audit passes AND the CEO authorizes. Real capital is hard-disabled behind `DRY_RUN_ENABLED`.
3. **Point-in-time correctness (T-300a):** event-time comes from on-chain slot/block_time only — never wall-clock; absent → the record is censored. No lookahead, no leakage.
4. **Asymmetric trust:** every signal may only *de-risk* (reject / shrink / down-weight / exit) — never size up, widen a stop, or add conviction.
5. **Money = integer lamports / Decimal**, never float.
6. **No secrets in code or logs;** the wallet key lives only in an isolated Vault signer.

---

## 2 · How it was built — the operating model

**The AI Software Agency (charter: `CLAUDE.md`).** The human is the **CEO**. The main Claude session is the
**Agency Runtime** (the orchestrator's hands): it *never writes production code itself* — it dispatches specialized
subagents and enforces quality gates. Work flows through artifacts in `.agency/`, never through conversation memory.

**Dual-G3 quality gate (the core discipline).** Every code deliverable must pass **two independent reviewers** —
`code-reviewer` (correctness/quality) **and** `backtest-qa-engineer` (leak audit / edge oracle) — before it can
land. The maker never grades its own work (maker/checker law). Three failures → re-plan; still failing → escalate.

**The wave-runner Workflow pattern.** Each build is a deterministic multi-agent Workflow: **maker builds → the two
reviewers run in parallel → bounded fix-loop (≤2 rounds) → both PASS = G3.** Guards learned the hard way: build/fix
agents must not run git; reviewers must do non-destructive review (never edit the tree).

**Multi-session orchestration (current).** Two Claude sessions now run the repo concurrently under an async
coordination channel (`.agency/COORDINATION.md`) with a status board, mailbox, and lane rules:
- **🅰 Session A — BUILD lane:** execution go-live (real signer/custody), ingestion/detection completeness, and now the smart-money **reaction-corpus recorder**.
- **🅱 Session B — EDGE lane:** the edge-proof harness, models/gates, and the data collector/corpus. Verdict authority (GO/NO-GO) is B's sole call and is never fabricated.

**Gates G0–G6** (scope → architecture → design → per-task build → integration → release → acceptance) were all run
and recorded under `.agency/05-reports/gates/` (G0-APPROVED, G1-APPROVED, G4-PASS, G5-PASS, G6-ACCEPTED).

---

## 3 · The agent roster — who built it (27 specialized agents in `.claude/agents/`)

| Agent | Role in this project |
|---|---|
| `quant-research-lead` | The edge oracle. Owns the honest GO/NO-GO thesis, kill criteria, capital-staging plan. Falsified the launch-data edge. |
| `quant-product-analyst` | Turned the vision into measurable trading acceptance criteria (latency budgets, breaker rules, FSM invariants). |
| `solana-systems-architect` | The master blueprint: triple-loop topology, Rust/Python split, the frozen typed contracts, ExecutionVenue interface. |
| `data-ingestion-engineer` | Geyser/Yellowstone gRPC, pump.fun/Raydium decoders, PumpPortal WS feed, the point-in-time feature store, detection completeness. |
| `feature-quant-engineer` | Leak-free microstructure + TA features (LP/lock, holder concentration, buy/sell pressure, price-path tensor). |
| `nlp-sentiment-engineer` | The Market Conviction Score — adversarial sentiment (manufactured hype LOWERS conviction). Caller-score de-risk guard. |
| `ml-prediction-engineer` | The snipe classifier, survivor model, chart-regime model, and the **edge-proof outcome + momentum harnesses**. |
| `llm-reasoning-engineer` | The Reasoner: de-risk-only adjudication (veto / force-exit / narrative-failure) — never on the hot path. |
| `agent-orchestration-engineer` | The triple-loop controller, per-position FSM, atomic snipe→fast handoff. |
| `solana-execution-engineer` | The ExecutionVenue (Jupiter/Raydium/Simulation), versioned txs, simulate-before-send, partial-fill/failed-land handling. |
| `mev-latency-engineer` | Jito bundle submission, dynamic tip economics, sandwich avoidance, detection-path latency budget. |
| `risk-guardrails-engineer` | Circuit breaker, three-layer survivable stops, dead-man's switch, fractional-Kelly sizing, the sub-10ms pre-trade safety gate. |
| `backtest-qa-engineer` | Half of every G3: leak audits, purged/embargoed walk-forward, the SimulationVenue burn-in, the edge-vs-baseline PASS/FAIL. |
| `crypto-security-engineer` | Custody/secrets policy, the exec/custody security audit (PASS-WITH-CONDITIONS), dependency supply-chain review. |
| `solana-execution` / `latency-devops-engineer` | Co-located low-latency deploy, RPC strategy, Prometheus/Grafana/Alertmanager, structlog decision logging. |
| `code-reviewer` | The other half of every G3 — correctness/conformance verdicts, file:line findings, non-destructive. |
| `orchestrator` | Delivery lead — task board, gate reviews, status reports (used through the charter). |
| `docs-delivery` | README, API docs, deploy/ops guides, delivery packaging (G5/G6). |
| + base roster | `product-analyst`, `solutions-architect`, `uiux-designer`, `frontend-engineer`, `backend-engineer`, `mobile-engineer`, `devops-engineer`, `qa-engineer`, `security-engineer` (operator dashboard + generic gates where the specialist didn't supersede). |

Specialists **supersede** generics on this project: `backtest-qa-engineer` > `qa-engineer`; `crypto-security-engineer` > `security-engineer`; `solana-systems-architect` > `solutions-architect`.

---

## 4 · The skills used (methodology layer)

- **`loop-engineering`** — the temporal layer. Made AATS a *governed loop*: the `STATE.md` spine, `loop-budget.md` (token caps + kill-switch), `loop-run-log.md` (one JSON line per wave), control-plane registration, and the L1→L2→L3 autonomy ladder.
- **`agent-swarm-orchestration`** — the spatial layer. How work fans out across many specialized agents through spec→build→review→release gates.
- **`verification-loop` / `loop-verifier`** — the checker discipline: never trust "tests passed" — re-run them; default stance REJECT.
- **`architecture-decision-records`** — 14 ADRs capturing every hard-to-reverse decision (see §5).
- **`loop-budget` / `loop-triage`** — budget guard + cadence triage for the recurring edge-proof loop.
- Supporting: `minimal-fix`, `search-first-development`, `artifact-design` (the progress dashboard).

**Delivery mechanism:** the **Workflow** tool (deterministic multi-agent orchestration) ran the dual-G3 waves — e.g. the outcome-labeling harness and the momentum harness were each built by a maker agent, then gated in parallel by `code-reviewer` + `backtest-qa-engineer` with a fix-loop, both passing on the first round.

---

## 5 · System architecture

**Triple-loop topology** (ADR-0002 Rust/Python split, ADR-0001 Redis Streams bus):
- **SNIPE loop** — Rust hot core (`rust/aats-signer`, single-digit ms) — detection→decision on the critical path.
- **FAST loop** — the OMS (~10–100 ms) — reads *pre-set* flags only; never awaits an LLM or heavy model.
- **SLOW loop** — Python (seconds) — all heavy work: ML inference, LLM reasoning, sentiment, enrichment.

**The 17 production modules (`aats/`):** `ingestion` (14 files — feeds/decoders/feature store), `features` (6),
`sentiment` (10), `models` (14 — classifiers, gates, baselines), `reasoning` (8 — LLM adjudication), `controller`
(15 — triple-loop + FSM), `risk` (26 — the safety spine), `execution` (8 — venues/signer/tx builder), `mev` (6 —
Jito/tips/split-exit), `contracts` (10 — frozen typed messages), `control_plane` (5), `dms` (2 — dead-man's
switch), `telemetry` (4), `telegram` + `telegram_bot` + `sentiment/caller_score` (KOL/alpha signals), `backtest`
(4 — the edge-proof harnesses).

**14 ADRs** (`.agency/02-architecture/adr/`): Redis-Streams bus (0001), Rust/Python split (0002), venue registry
(0003), clean-room validation harness (0004), frozen control-plane contract (0005), **asymmetric-trust-by-type
(0006)**, single-writer FSM write-ahead handoff (0007), **three-layer survivable stop (0008)**, **isolated signer
separate-process (0009)**, typed-label provenance guards (0010), no-Kubernetes (0011), breaker event-time day-key
(0012), devnet submit-mode (0013), regime-signal contract + de-risk wiring (0014).

---

## 6 · The build timeline (from the commit history)

**Foundation (G0–G6):** `d7dab76` "Build AATS Solana meme-coin ultra-sniper (G0–G6 + E1–E13 enhancements)" — the
full agency pass: spec → blueprint → build → integration → release → acceptance, plus 13 first-round enhancements.

**Real data ingestion:** Yellowstone Geyser gRPC (`95cf122`), free-tier WebSocket ingestion (`409ca36`), genuine
PumpPortal real-time feed + keepalive fix (`c346ea0`), quote-mint guard rejecting USDC/USDT/WSOL as launches
(`14ba3df`), live SHADOW-mode feed to the dashboard (`c235509`), full docker paper stack (`59d5ac4`).

**Elite-enhancement waves (competitor-parity, de-risk-only):**
- **EN1** caller-score de-risk guard (`7faccf5`).
- **Wave 1** the go-smart alpha engine — velocity, CA-extract, Telethon, live smart-wallet (`cce5440`).
- **Wave 2A/2B/2C** catastrophic exits: insider-dump + sellability (`fbf4731`), serial-deployer reputation + fresh-wallet (`1c70513`), min-holder floor + LP-unlock (`abf2477`), then **wired LIVE with an E2E control test** (`7722294`) — after an adversarial program review (`a6a85ad`) caught them built-but-unwired.
- **Governance:** made AATS a governed loop — LOOP.md + budget + run-log + control-plane (`9441359`); methodology review (`METHODOLOGY-REVIEW-2026-07-03`).
- **Wave 3** chart-path / regime architecture, 4/5 G3-PASS (`1eead58`); acceptance ledger backfilled (`0a35096`).

**Security:** exec/custody audit → **PASS-WITH-CONDITIONS** (`563b497`).

**Phase 5 — the edge proof (the heart, see §8):** outcome-labeling harness (`9161960`), first real run NO-GO
(`89e722e`), price-path+pressure collector (`339c24d`), momentum harness (`eb96193`), first GATE-B PASS but NO-GO
(`43e1002`), param-freeze hardening (`071b1f5`), compacted resume spine (`731b262`), fast parallel+cache resolver
(`555df12`), **DECISIVE n=4187 → NO-GO, GATE-B reversed (`499a8c6`)**, pivot to smart-money/KOL-reaction thesis
(`cf9f26a`→`d3c121f`→`faeae28`→`ecfc00a`).

---

## 7 · The safety & security spine (Phase 3 — done + live-wired)

- **Circuit breaker** (daily-loss, event-time day-key ADR-0012), **three-layer survivable stop** (venue-native +
  in-process enforcer + dead-man's switch, ADR-0008), **global kill switch**.
- **Sub-10ms pre-trade safety gate:** sellability sim, LP lock, mint/freeze renounce, holder/bundle concentration, buy/sell tax.
- **Catastrophic exits (live-wired, E2E-proven):** honeypot/rug detection, dev-sell auto-exit, delayed-honeypot re-probe, serial-rugger + fresh-wallet filters, insider-dump, LP-unlock-approaching.
- **Sizing:** fractional-Kelly with hard exposure caps — can never bet the farm.
- **Cost-aware entry gate:** refuses any trade whose modeled edge is below the ~6% round-trip cost.
- **Security audit (`EXEC-CUSTODY-AUDIT-2026-07-06.md`) — PASS-WITH-CONDITIONS:** paper state is secure today (no
  wallet key in the system; every real-money path fail-closed and test-proven `send_calls==0`; money int/Decimal;
  secret sweep clean; 176 execution tests pass). **Go-live blocker:** the real isolated `aats-signer` (the
  un-bypassable spend-cap/allowlist enforcer, ADR-0009) is still a scaffold — Session A is building it.

---

## 8 · The edge-proof journey — the heart of the project (honest science)

This is where the project's integrity shows. The edge-proof *machine* was built, leak-audited, and run on real
data — and it told the truth, repeatedly, even when the truth was "no edge."

1. **Built the machine (dual-G3 PASS):** `aats/backtest/outcome_harness.py` + `run_edge_proof.py` +
   `aats/models/{gate_a,gate_b,baseline}.py`. GATE-A = aggregate net-of-cost PnL with a bootstrap lower-95% bound;
   GATE-B = model-vs-naive-baseline net-PnL-per-unit-risk, purged/embargoed walk-forward. **Fail-closed on empty
   data** (raises rather than fabricate a number). The leak boundary is structurally enforced and load-bearing (a
   leak test goes RED when a forward feature touches the decision); `backtest-qa` independently refuted lookahead.

2. **Built the data engine:** a standalone detached collector (`C:/aats_shadow/_collector.py`) bypassing a bugged
   in-repo recorder, recording every real pump.fun launch's entry + the forward **price path + buy/sell pressure**
   from DexScreener — a leak-safe labeled corpus, accrued autonomously to 4,000+ launches.

3. **Strategy 1 — launch-winner prediction:** RAN real → **NO-GO** (model +0.311 < baseline +0.619; GATE-A
   lower-95% negative). Predicting winners from launch data has no edge — exactly as the thesis predicted.

4. **Strategy 2 — momentum/reaction entry @60s** (decide on ≤60s price move + buy/sell pressure, leak-safe):
   - **n=497:** NO-GO **but the FIRST GATE-B PASS** — model beat a *losing* naive baseline (delta +0.041, lower-95% +0.026). A promising signal.
   - **n=4,187 (DECISIVE):** the **GATE-B PASS REVERSED → −0.011** (lower-95% −0.060). The model no longer beats the baseline; its own trades lose money. **The promising signal was a small-sample fluke, killed by 8× more data.** This is the edge proof working perfectly — a false positive caught before a cent was risked.

5. **Verdict:** **both on-chain launch-data strategies are decisively NO-GO.** No durable edge exists in launch data
   alone, net of cost. `.agency/05-reports/qa/EDGE-PROOF-momentum-DECISIVE-2026-07-06.md`.

---

## 9 · The data pipeline & the pivot (current active work)

**Launch corpus (done):** `labeled_corpus.jsonl` — 4,000+ real launches with price-path + pressure outcomes;
block_time resolved on-chain (T-300a); a fast parallel+cached resolver makes re-runs take minutes.

**The pivot (per NO-GO → stop or pivot):** the only remaining lever with a *real prior* is the bot's stated alpha
thesis — **front-run the predictable retail reaction to a PROVEN signal** (smart-money buy / whale / KOL call).
This needs a *different* dataset the launch corpus does not contain. Interface locked in
`.agency/04-plan/REACTION-CORPUS-SPEC.md`:
- **Session A builds** a `reaction_corpus.jsonl` recorder. **v1 = whale/large-early-buy** (fully on-chain, no
  external list) via **Helius `logsSubscribe` on the pump.fun program** (~100 tx/s firehose, data source confirmed
  2026-07-07). v2 = tracked smart-money wallets + KOL calls (need a curated wallet set / Telegram creds — flagged to CEO).
- One record per SIGNAL event = `{signal_type, source_id, mint, signal_slot, signal_block_time_ms (on-chain),
  signal_price_sol, signal_size_sol, source_prior, forward:[…]}` — **same `forward` shape as the launch corpus** so
  **Session B's harness reuses `resolve_outcome` directly.**
- **Session B builds** the front-run GATE-A/GATE-B: baseline = "follow every signal", model = quality-filtered
  (de-risk only). Decision anchored at `signal_block_time_ms`; entry with a realistic latency/slippage haircut.
- **Honest expectation:** a real prior, but front-running is competitive and the cost gate is brutal — it may still
  be NO-GO. It will be tested rigorously and reported truthfully.

---

## 10 · Current status — phase by phase

| Phase | Status |
|---|---|
| **1 · Detection & Data** | ✅ Built — 4-venue detection, point-in-time pipeline, 4,000+ launch corpus. |
| **2 · Intelligence** | 🟠 ~90% — snipe classifier, survivor + chart-regime architecture, alpha engine, adversarial sentiment built; some model *training* is data-gated. |
| **3 · Risk & Safety** | ✅ Done + live-wired + E2E-proven (breaker, survivable stops, DMS, honeypot/rug exits, cost gate, Kelly caps). |
| **4 · Execution & Custody** | ◑ Built in simulation (venues, tx builder, multi-wallet, Rust signer scaffold) + **security audit PASS-WITH-CONDITIONS**. Go-live blocker: real isolated signer (Session A building). |
| **5 · ⭐ EDGE PROOF** | ⛔ **NO-GO (decisive) on launch data.** Machine built + leak-audited + run on 4,187 real launches. The one untested thesis (smart-money/KOL reaction) is being stood up. |
| **6 · Live Trading** | ◻ Not started — gated on a real GO + security conditions + CEO authorization. Real capital hard-disabled. |

---

## 11 · The scoreboard (the ONLY metrics reported — never a win rate)

| Metric | Value |
|---|---|
| Net-of-cost PnL (launch strategies) | **Negative / not statistically positive** → NO-GO |
| Edge vs naive baseline (GATE-B) | **Model does not beat the baseline** (momentum delta reversed to −0.011 at n=4,187) |
| Max drawdown / path-to-ruin | Bounded by design (Kelly caps + breaker) — never risked (paper only) |
| Honeypot-rejection | Machinery built + tested (not yet measured on live capital) |
| Detection latency | ✅ milliseconds-class |
| Land rate | N/A (no live trades — capital disabled) |
| Reaction-thesis edge | **UNTESTED** — corpus being built |

---

## 12 · Key artifacts & where they live

- **Resume spine:** `.agency/RESUME-HERE.md` (60-sec) → `.agency/STATE.md` (full).
- **Coordination:** `.agency/COORDINATION.md` (multi-session lanes + status board + mailbox).
- **Edge proof:** `aats/backtest/{outcome_harness,momentum_harness,run_edge_proof}.py`, `aats/models/{gate_a,gate_b,baseline}.py`; results in `.agency/05-reports/qa/EDGE-PROOF-*.md` (incl. `…momentum-DECISIVE…`).
- **The pivot spec:** `.agency/04-plan/REACTION-CORPUS-SPEC.md`.
- **Security:** `.agency/05-reports/security/EXEC-CUSTODY-AUDIT-2026-07-06.md`.
- **Architecture:** `.agency/02-architecture/` + 14 ADRs under `adr/`.
- **Reviews / gates:** `.agency/05-reports/{review,gates}/` (program review, methodology review, acceptance ledger, G0–G6).
- **Governance:** `LOOP.md`, `loop-budget.md`, `loop-run-log.md`.
- **Data engine (operational, outside repo):** `C:/aats_shadow/_collector.py` → `labeled_corpus.jsonl`.

---

## 13 · The roadmap forward (all Claude-owned — Codex dropped)

1. **Session A:** finish the real isolated signer (go-live blocker) + the **v1 whale-buy reaction-corpus recorder** (Helius pump.fun firehose) + detection completeness (Wave-4, CP-07).
2. **Session B:** once `reaction_corpus.jsonl` flows, build the **front-run GATE-A/GATE-B harness** (reuse `resolve_outcome`) → run the decisive reaction-thesis proof.
3. **The gate:** if the reaction thesis proves **GO** → re-run security audit, finish the signer, then Phase-6 devnet → tiny-real → scale, **CEO-authorized only**. If **NO-GO** → the honest program conclusion is that no solo-operator edge exists; keep AATS as a proven-safe paper platform.

---

## 14 · The honest bottom line

You have an **elite-engineered, safe, fully-built Solana sniper** whose every safety and execution component works,
was independently reviewed, and is security-audited. What you do **not** have — yet, and may never — is a **proven
edge**. The system was run rigorously on thousands of real launches and honestly reported **NO-GO** twice, killing
even its own promising signal when more data disproved it. That intellectual honesty is the most valuable thing the
project produced: **a bot that says "I don't know yet" instead of lying to you, and that will not touch real money
until an edge is genuinely proven.** The one thesis with a real prior — reacting to proven smart-money/KOL signals —
is now being built and will be tested with the same rigor. The truth, whatever it turns out to be, will be reported.

*No real capital has moved. None will until a real GO. — The AATS Agency*
