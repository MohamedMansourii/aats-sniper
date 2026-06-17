# AATS — DELIVERY PACKAGE (Gate G6 — Acceptance)

**Project:** AATS — Solana Meme-Coin Ultra-Sniper (PAPER build)
**Prepared by:** `docs-delivery` for **CEO acceptance sign-off** · **Date:** 2026-06-17
**Companion:** `HONEST-EDGE-REPORT.md` (the honest edge finding — read it alongside this).
**Build state:** PAPER / DRY-RUN. Real capital **DISABLED by default and unreachable**. Live edge
**UNPROVEN-NO-REAL-DATA** (the correct, accepted paper outcome).

---

## 1. Executive summary (in CEO language)

We built the autonomous Solana meme-coin sniper you asked for — a deployable system that watches the
chain for new liquidity, scores each launch, applies hard risk guardrails, and trades it — **and we
built it to tell you the truth about whether it actually makes money before you risk a cent.**

Three things are true today, all verified by running the system:

1. **It runs, end-to-end, on one command.** `docker compose up` brings up the full 11-service system on
   a single host. You can drive it — kill it, flatten positions, pause it, tighten its limits — from
   **both** a web dashboard and a **Telegram** channel. Both surfaces can *only reduce risk*; neither can
   make the bot trade bigger, wider, or with more leverage. That is enforced in the code's type system,
   not by a warning label.

2. **The safety stack is proven by firing it.** A daily-loss **circuit breaker**, a three-layer
   **survivable stop** (the stop still works even if the bot process dies), and a **dead-man's switch**
   are all built and each is proven by a test that actually *fires* it. From the moment you hit kill,
   the open book is flat in **under 2 seconds** — demonstrated from both the dashboard and Telegram.

3. **The edge is honestly UNPROVEN — and that finding is the deliverable.** Per your own honesty
   mandate, we did not manufacture a win-rate or a profit number. The system has the acceptance harness
   built and proven to compute correctly, but it has **no recorded live-market data yet**, so it cannot
   and does not claim a real edge. Every number to date is synthetic. **Real money stays disabled until
   the edge is proven on recorded data — and "no edge net of cost" would be a successful outcome, not a
   failure.**

What you are accepting at G6 is a **safe, deployable, honestly-instrumented paper system**, not a money
machine. The one decision the agency does not make alone — turning on real capital — is yours, gated
behind a documented checklist that is not yet met.

---

## 2. What was built (the system)

A **triple-loop** trading system with a **single-writer per-position FSM**, built on the validated
`sol-sniper` simulation foundation (the seam is law) and productionized behind it:

| Layer | What it does | Language | Key rule |
|---|---|---|---|
| **SNIPE loop** | Detect a launch/migration → 0-RPC safety gate → calibrated model probability → cost gate → entry | Rust (hot path) | Never waits on an LLM |
| **FAST loop** | Stop-loss / TP ladder / OMS / reconciliation / survivable-stop enforcement, deterministic <100 ms | Rust (hot path) | Never waits on an LLM |
| **SLOW loop** | Sense (MCS sentiment) → predict (survivor model) → reason (de-risk LLM), seconds–minutes | Python | **May only reduce risk** |

**M1 — Sensors:** Solana-native ingestion (Geyser/ShredStream transport seam, pump.fun / PumpSwap /
Raydium decoders), point-in-time quant + microstructure features (lookahead refused at three layers),
first-K-slot buy-pressure feature (makes the naive baseline constructible, C-4), and the **adversarial
MCS sentiment pipeline** (coordinated/synchronous shilling *lowers* conviction — manufactured hype →
conviction 0, never raises it).

**M2 — Engine:** a calibrated **LightGBM → ONNX** snipe classifier (outputs a probability + uncertainty,
not a point price; ECE ≈ 0.04; ONNX parity 2.8e-07; de-risk monotone constraints real in the tree; leak
proof: label-as-feature drives AUC to 1.0 vs 0.745 clean) behind a **FROZEN naive-momentum baseline**
(hash-pinned, C-4) and a **GATE-B monitor** (model-vs-baseline net-PnL-per-risk delta, lower-95% bound);
a slow-loop survivor model; and a **schema-enforced de-risk-only LLM reasoner** (`ReasoningAction` has
exactly four de-risk members — size-up / widen-stop / add-leverage are *inexpressible by type*).

**M3 — Controller:** the triple loop, per-position FSM with an atomic snipe→fast handoff (ADR-0007),
shared Redis state, the **control-plane API** (the FROZEN de-risk-only operator contract), the
**dashboard**, and the **Telegram** channel.

**M4 — Guardrails:** the **safety stack** — daily-loss **circuit breaker**, three-layer **survivable
stop**, **dead-man's switch** (all proven-by-firing); the **sub-10ms pre-trade safety gate** (p99 ≈ 60µs
measured, 0-IO/0-RPC/0-LLM); **Kelly + cost-aware sizing** (rejects iff edge ≤ cost; hard cap ≤ ¼ Kelly;
no signal can size up); the **ExitEngine** (TP-ladder + trailing + hard stop + timeout, Secure-MEV
default); the **JitoJupiterVenue** in **DRY-RUN/no-submit** (build→sign→simulate→DON'T-send, triple-gated);
edge-bounded **Jito tips** (capped at 0.30× edge, read live, never hardcoded); **multi-wallet** execution
(anti-cluster, blast-radius caps, N_max=1 default until R4); adversarial **sell-sim** honeypot detection.

**M5 — Immunity:** Docker Compose topology on a single co-located host, **isolated signer** (separate
process, holds the secret, no inbound network — ADR-0009), Prometheus / Grafana / Alertmanager
monitoring (no win-rate panel anywhere), Vault-based key custody, the G4 security audit.

**Money is integer lamports / `Decimal` everywhere — never float**, on every wire field and in every
calculation. **Point-in-time correctness everywhere** (event-time, never compute-time).

---

## 3. How to see it working (run commands + demo path)

### One command brings it up (verified, T-500)
```bash
git clone <repo-url> aats && cd aats
cp .env.example .env          # placeholders only — paper defaults work as-is
docker compose up             # builds + starts the full 11-service topology, DRY-RUN/paper
```
Verified for this delivery: `docker compose config --quiet` → **exit 0**; 11 services; `DRY_RUN_ENABLED`
defaults to `true` on every tx-capable service; `redis` and `aats-signer` publish no ports.

Open the dashboard at **http://localhost:3000** (renders a full telemetry stream on mock data with no
backend; set `VITE_USE_MOCK=false` + `VITE_CONTROL_PLANE_URL=http://localhost:8787` to wire it live).

### The default state after `docker compose up` is safe
Mode = `SHADOW`, `DRY_RUN_ENABLED=true`, wallet unfunded, **no real submit path reachable.** You cannot
accidentally trade real money from this state.

### Drive-the-bot demo (dashboard AND Telegram, in PAPER) — verified, T-402
```bash
python -m pytest tests/e2e/test_t402_operator_demo.py -q   # → 16 passed
```
This boots the FROZEN control-plane server and a running controller over a `SimulationVenue` against the
**same shared objects**, so an operator command genuinely mutates the running loop. It proves by
execution:
- **KILL flattens the open book < 2s** from **both** the dashboard and Telegram (same contract, real
  confirm-nonce flow).
- **MODE** changes propagate to `/api/state`; `/pause` steps the mode *down* only.
- **SSE `/api/feed`** carries a real controller-published frame (not a mock seed).
- **Breaker + Layer-2 survivable-stop + Layer-3 dead-man's switch each fire on demand** (the DMS fires
  when the FAST loop is killed and stops beating).
- Every **risk-increasing** command is rejected: mode-up-to-LIVE behind DRY-RUN → 403, risk-config
  widen → 403, no-auth → 403; the Telegram seam exposes only `{status, kill, flatten, pause}`.
- **Honesty enforced:** `/api/metrics` carries no `win_rate`; the edge gates assert *not passing* on the
  synthetic build; `dry_run_enabled` stays true.

### Quick safety/quality verification (verified for this delivery)
| Check | Command | Result |
|---|---|---|
| Venue defaults to DRY-RUN | source `aats/execution/jito_jupiter_venue.py` | `submit_mode = DRY_RUN` |
| Operator + safety demo | `pytest tests/e2e/test_t402_operator_demo.py` | **16 passed** |
| Risk + safety primitives | `pytest tests/risk` | **315 passed** |
| Edge harness + leak guards | `pytest tests/validation` | **22 passed** |
| Compose topology validates | `docker compose config` | **exit 0** |
| Full consolidated suite | `pytest tests/` | **1842 passed / 2 skipped / 0 failed** |

---

## 4. Scope ledger — every competitive feature → DELIVERED / status

Source: SPEC §9 (11/11 covered by a real, measurable AC) and the gate record.

| Competitive feature | FR(s) | Verdict |
|---|---|---|
| Auto-sniper on new launch | FR-001/021/026/027 | **DELIVERED** (paper, DRY-RUN) |
| Migration sniper (pump.fun → PumpSwap / Raydium) | FR-001/002/003 | **DELIVERED** — survivor selection, NOT migration-block-0 (unwinnable race, by design) |
| Copy-trade / smart-money as selectivity FILTER | FR-007/011/032 | **DELIVERED** — count-only, never a buy trigger; EH-005 default-ZERO; disabled by default |
| Limit + DCA resting orders (fire offline) | FR-035 | **DELIVERED** (T-326 production fix landed + flake proven gone; dual-G3 verdict-only re-entry is the sole bookkeeping loose end, OFF the milestone path) |
| Auto TP-ladder + trailing + "auto-strat" presets | FR-029/030 | **DELIVERED** — ExitEngine, Secure-MEV default; beats naive exit +25% in sim (direction only) |
| Multi-wallet / bundle execution + anti-cluster | FR-036/040 | **DELIVERED** — N_max=1 default, activation-gated to R4 |
| MEV protection: Fast vs Secure modes | FR-029/041 | **DELIVERED** — Secure default; asymmetry invariant structural |
| Token-safety scanner (red flags on dashboard) | FR-026/037 | **DELIVERED** — sub-10ms gate + sell-sim honeypot fingerprints |
| Token discovery enrichment (DEXScreener / Birdeye / Meteora / Moonshot) | FR-006 | **DELIVERED** — injectable enrichment registry |
| Portfolio + P&L cards / export | US-020 | **DELIVERED** — net-of-cost PRIMARY, export; NO win-rate |
| Telegram operator channel (alerts + constrained commands) | FR-050/055 | **DELIVERED** — outbound alerts + exactly 4 de-risk commands |

**DESCOPED / deliberately NOT built (with recorded reason):**
- **Block-0 and migration-block-0 racing** — DESCOPED by the edge verdict: a solo, unstaked, non-colocated
  desk *cannot* win these races (SWQoS staked-lane gap; atomic co-bundlers). Racing them was killed as
  NO-GO; the edge surface is deliberately the inverse (selection + exit discipline).
- **Blind copy-trade mirroring** — DESCOPED as a guaranteed loss (you are exit liquidity by the time their
  buy is on-chain). Smart-money is a filter only, never a mirror — wiring it as a buy trigger is a release
  blocker.
- **Live real-capital trading** — NOT ENABLED: gated behind the pre-live checklist (see §7), which is not
  yet met. Disabled by default behind `DRY_RUN_ENABLED=true`.
- **The recorded-data validation modules** (C-9 deflation, C-11 haircut calibration, C-3/C-13
  stratification, C-5 clock-shift control, C-10 group-purge, ≥5-window CPCV) — NOT YET BUILT: they have no
  recorded data to run on; they are the R1/E-program work and are blocking only on the path to real capital.

---

## 5. Quality evidence (test totals, QA/security verdicts, gate history)

### Test totals (verified by execution for this delivery)
- **Consolidated suite: 1842 passed / 2 skipped / 0 failed** (the 2 skips are the solders-gated execution
  tests `tests/execution/test_tx_builder.py:161/:186`). First proven stable at 1803/2/0 bit-for-bit across
  10 deterministic runs; the G4-fix wave added +39 (breaker→StateStore projection test + its production
  change + the frozen-clock concurrent test).
- Per-area: risk 315 · validation 22 · execution 171 (+2 skip) · e2e operator demo 16 · control-plane 112
  · reasoning/contracts/execution/telegram/control-plane 452.

### QA and security verdicts
- **Edge (T-401):** `UNPROVEN-NO-REAL-DATA` — the honest, accepted outcome. The GATE-A/GATE-B harness is
  **built and computes correctly** (right sign on both controls, declines→0, net-of-cost, leak-free,
  clean-room, purge load-bearing, deterministic). No recorded data exists. See `HONEST-EDGE-REPORT.md`.
- **E2E operator demo (T-402):** **PASS** — kill flattens < 2s from both surfaces; de-risk-only; all three
  safety layers fire.
- **Security audit (T-403):** **FINDINGS (core PASS)** — no open secret leak (whole tree + history clean);
  Telegram authz fail-closed + de-risk-only; DRY-RUN triple-gated unreachable; LLM prompt-injection cannot
  raise exposure (size-up type-inexpressible). One HIGH custody item (F-01, signer is a scaffold) is
  **latent** because LIVE is unreachable, and is a documented R3/LIVE precondition (see §7).

### Gate history (G0–G6)
| Gate | Name | Verdict | Record |
|---|---|---|---|
| **pre-G0** | Edge gate | **GO-PAPER-ONLY** (13 blocking conditions C-1..C-13) | `EDGE-VERDICT.md` |
| **G0** | Scope | **APPROVED** (agency-autonomous) — 57 FR · 11 NFR · 32 stories · 60 AC; 11/11 competitive features; C-1..C-13 encoded; no win-rate metric | `G0-APPROVED.md` |
| **G1** | Architecture | **APPROVED** (agency-autonomous) — blueprint complete; control-plane contract FROZEN + reconciled with dashboard; C-1..C-13 structurally enforced; ADR-0001..0012; custody (ADR-0009) + leak-proofness (ADR-0010) resolved by construction | `G1-APPROVED.md` |
| **G2** | Design | FOLDED into Lane E (finishing existing dashboard) — right-sized, not skipped | — |
| **G3** | Build (per task) | **COMPLETE** (milestone path) — all milestone-path tasks dual `code-reviewer` + `backtest-qa-engineer` PASS across Waves F/S/M1+C1/M2/D/E; suite proven stable | `G3-wave*.md`, `G3-stabilization.md` |
| **G4** | Integration | **PASS (conditional)** — leak/clock non-vacuous; edge harness built+correct; e2e PASS; security core PASS. Carried non-blocking: COND-G4-1 (fixed), T-402-F1 (fixed); COND-G4-2 = R3/LIVE checklist | `G4-PASS.md` |
| **G5** | Release | **PASS** — one `docker compose up` validated; docs/runbooks complete + honest; both G4 carry-forwards fixed + mutation-proven; suite GREEN 1842/2/0 | `G5-PASS.md` |
| **G6** | Acceptance | **PRESENTED** — this package + `HONEST-EDGE-REPORT.md`; awaiting CEO sign-off | this file |

---

## 5a. Enhancement program (E1–E13) — COMPLETE (post-G6, additive)

After G6 acceptance, the agency ran a final **AUDIT-FIRST, ADDITIVE** enhancement program covering 13
requested features (E1–E13) plus 4 running audits, in 4 sequenced waves. **It is now CLOSED.** Full
close-out ledger with file paths: `.agency/05-reports/gates/ENH-COMPLETE.md` (wave records:
`ENH-wave1.md`, `ENH-wave2.md`, `ENH-wave3.md`).

**Outcome: 16 ADDED · 1 COVERED · 0 FAILED.** Every code item that touches a model/trade path cleared the
**dual G3** gate (`code-reviewer` **and** `backtest-qa-engineer` both PASS); ops/deploy and UI items cleared
`code-reviewer`. The program did **not regress** the accepted core build, and **real capital stays
DRY-RUN-disabled and unreachable** throughout.

| # | Enhancement | Verdict | What it adds (de-risk / selectivity / visibility only) |
|---|---|---|---|
| E1 | Devnet live-send validation mode | **ADDED** | Real **devnet** SUBMIT path; an unconfirmed tx reconciles as NOT-landed/retryable (the false-fill BLOCKER is fixed). `SubmitMode.DEVNET` (ADR-0013) structurally **cannot** unlock mainnet LIVE; devnet = worthless SOL |
| E2 | Creator/token denylist pre-filter | **ADDED** | A denylist VETO wired as STEP-0 of the live BUY path — a denylisted creator/token can never fire a buy |
| E3 | Candidate-queue surface | **ADDED** | Read-only `GET /api/candidates` + dashboard page — see every launch the bot evaluated and why it skipped/sniped (no win-rate, GET-only) |
| E4 | Control-plane auth + exposure hardening | **ADDED** | Loopback-default bind, fixed Dockerfile entrypoint, nginx TLS/HSTS + default-deny IP allowlist; destructive POSTs operator-Bearer gated |
| E5 | Always-on operational hardening | **ADDED** | systemd units (never enable real capital), logrotate, a read-only Redis backup script + restore guide |
| E6 | Discord ingestion | **ADDED** | A slow-loop Discord sentiment adapter where coordinated shilling **lowers** conviction (never raises it) |
| E7 | News / breaking-news layer + page | **ADDED** | Keyword news scoring clamped to **de-risk only** ([-1,0]); a credible negative event forces an exit/veto. Read-only Narrative & News dashboard page |
| E8 | Tunable discovery / SCREENER filter | **COVERED** | The survivor/late-entry screener already existed and was verified adequate (no code change) |
| E9 | Alpha-caller track-record scoring | **ADDED** | An **honest** caller-accuracy weight in [0,1] used as a selectivity filter — **no win-rate**, leak-free |
| E10 | Social-velocity + bot-ratio features | **ADDED** | Bot/factory growth raises a de-risk penalty; the guard now survives `python -O` (raise + clamp, not a bare assert) |
| E11 | Wallet-cluster "Bubble Maps" | **ADDED** | Read-only `GET /api/wallet-cluster` + SVG graph page that **shows** coordinated bundler/sniper clusters (projection of existing detection; no new logic, GET-only) |
| E12 | Time-stop / stale-narrative exit | **ADDED** | A flat-position, cooled-narrative time-stop that forces a full SECURE exit |
| E13 | Anti-FOMO / already-pumped exclusion | **ADDED** | Already-pumped (>300%) / mainstream-mention tokens are **excluded** — the multiplier is clamped to [0,1] |

**Audit items (all ADDED):** stepped profit-lock **trailing-ratchet** (breakeven@2x, lock 2x@3x); the named
**MICRO early-entry preset** (tighten-only composition of existing primitives); a pre-trade
**liquidity-sanity VETO** (24h-vol ≥ 10× notional + x·y=k slippage sim ≤ 300 bps); and a soft ~2% daily-loss
**REDUCE/PAUSE risk tier** (strictly below the hard breaker) plus a GATE-B minimum-sample guard.

**Invariants re-confirmed for the whole program:** every signal is de-risk / selectivity-only (a buy
trigger, size-up, stop-widen, or leverage is *inexpressible by type*); the new dashboard surfaces are
read-only (GET-only, zero control action); the three safety primitives (breaker / survivable stop / dead-man's
switch) are **untouched**; money stays int-lamports/`Decimal`; **no win-rate field anywhere**; no secrets;
`aats/contracts/` and `docker-compose.yml` were not edited (the only frozen-contract change is the additive,
audited `SubmitMode.DEVNET` enum member). **Consolidated suite GREEN — 2283 passed / 2 skipped (Python) +
dashboard build GREEN + 123 dashboard tests passed.** Enhancement-wave **security re-audit = PASS** (no new
CRITICAL/HIGH; record `.agency/05-reports/security/ENH-security-reaudit.md`). The live edge remains honestly
`UNPROVEN-NO-REAL-DATA` and **real capital stays DRY-RUN-disabled — the R3 pre-live checklist (§7) is
unchanged by this program.**

---

## 6. Staged rollout — sim → shadow/record → paper → tiny-real → scale

Each rung has a gate that must pass before advancing. Real capital is disabled until R3, which requires
explicit CEO authorization. (Full table with metrics in `HONEST-EDGE-REPORT.md` §7 and `docs/deploy-ops-guide.md` §6.)

| Rung | Stage | Capital | Gate to advance | Current |
|---|---|---|---|---|
| **R0** | Sim | None (synthetic) | Mechanism demonstrated — **never licenses capital** | ✓ done |
| **R1** | Shadow / record | None (real data, no orders) | ≥ ~3,000 recorded launches, leak audit clean | **← we are here / not started (no live feed run)** |
| **R2** | Paper / dry-run | None (paper) | GATE-A **and** GATE-B pass on walk-forward windows; safety fires | harness ready, no recorded data |
| **R3** | Tiny-real | Real, incinerable (≤ 2 SOL, ¼-Kelly) | Live GATE-A+B over ≥ 100 trades; **CEO authorization + pre-live checklist** | **gated, not met** |
| **R4** | Scale | Larger, bounded | Fresh passing window at each new size | — |

The bot is currently at the boundary of R0/R1: fully built and paper-driveable, but **no live ingestion
has been run to produce recorded data**, so R1 is not complete and R2's gates cannot yet be computed on
anything that means edge.

---

## 7. Known limitations & the one decision that is yours (R3/LIVE checklist)

**Real capital stays DISABLED behind `DRY_RUN_ENABLED=true` until `docs/pre-live-checklist.md` clears in
full.** Three independent blocks, all currently **NOT MET** — the correct, honest paper state:

- **A — Edge proven on RECORDED data (NOT MET):** no recorded data exists; GATE-A/GATE-B cannot be passed
  yet. Requires R1 recording, the C-5/C-9/C-10/C-11 validation modules, GATE-A **and** GATE-B PASS.
  *If A fails, "no edge net of cost" is the correct, successful deliverable — do not fund.*
- **B — Custody & security hardened, COND-G4-2 (NOT MET):** the `aats-signer` is a scaffold — its three
  refusals (per-tx/rolling SOL spend cap, program-ID allowlist, transfer pin) must be **built +
  test-proven** (F-01); placeholder image digests replaced (F-10); signer container locked down (F-07);
  deps hash-locked (F-02); CI CVE scan added (F-03); GH Actions pinned to SHAs (F-04). All latent today
  only because LIVE is unreachable.
- **C — CEO legal + funding authorization (NOT GIVEN):** legal confirmation for your jurisdiction
  (OQ-009); a capped, incinerable, **trade-only** wallet (≤ 2 SOL, never main holdings); risk floors
  tightened for the live tranche; explicit R3 sign-off.

**Other known items (non-blocking for paper):** T-326 (resting orders) production fix is landed and the
flake is empirically eliminated; only its dual-G3 *verdict* is a bookkeeping loose end (OFF the milestone
path). T-402-F3 (a latency-budget test is load-sensitive) is a minor test-hygiene note.

---

## 8. Handover inventory — what the CEO now owns

### Secrets the CEO must provide (names only — never values; `.env.example` placeholders only)
| Name | Purpose | When |
|---|---|---|
| `WALLET_SECRET_VAULT_PATH` | Vault path to the trade-only wallet secret (signer fetches via short-lived token) | R3 only |
| `WALLET_MAX_BALANCE_LAMPORTS` | Hard cap on the funded wallet (≤ 2 SOL at R3) | R3 only |
| `TELEGRAM_BOT_TOKEN` (Vault ref) | Telegram alert + de-risk command channel | optional |
| `TELEGRAM_OPERATOR_USER_ID` | The single authorized operator (allowlist gate-1) | optional |
| `CEO_AUTH_TOKEN` | Required (with `DRY_RUN_ENABLED=false`) to move mode to LIVE | R3 only |
| `RPC_URL` / `GEYSER_*` / `JITO_*` | Provider endpoints (dedicated Geyser tier per OQ-003) | R1+ |
| `LLM_API_KEY` (Vault ref) | Slow-loop de-risk reasoner | optional |
| `DAILY_LOSS_LIMIT_SOL`, per-trade / aggregate caps | Risk floors, tightened for the live tranche | R3 only |

No real secret value is in the tree, history, or any image — verified by the security audit. The only
committed secret artifact is `.env.example` (placeholders + a FORBIDDEN banner; there is no
`WALLET_PRIVATE_KEY`/`KEYPAIR_JSON` var by design).

### Assets the CEO owns
- The full codebase (`aats/` package, `rust/` hot core + signer, `dashboard/`, `sol-sniper/` foundation).
- The deployment topology (`docker-compose.yml`, `docker/`, `monitoring/`, `config/program-allowlist.json`).
- The agency record (`.agency/` — specs, architecture + 12 ADRs, plans, all gate reports).
- The documentation set (see below).

### Decisions / locked positions the CEO now owns
- **GO-PAPER-ONLY** edge posture and the 13 conditions C-1..C-13.
- All 10 open-question defaults (OQ-001..OQ-010), including risk caps (0.1 SOL/trade, 0.5 SOL aggregate,
  −0.30 SOL daily floor), DMS T_DMS = 60s, Secure-MEV default, multi-wallet N_max=1 until R4.
- The one decision the agency does not make alone: **authorizing R3 real capital** (`NEEDS-CEO-DECISION`).

### Documentation map
| Doc | Read it when |
|---|---|
| `README.md` | First — what this is, one-command quick start, safety posture |
| `docs/deploy-ops-guide.md` | Deploying, env config reference, monitoring, staged-rollout ladder |
| `docs/dashboard-operator-guide.md` | Driving the bot from the dashboard |
| `docs/telegram-operator-guide.md` | Driving the bot from Telegram (the de-risk command set) |
| `docs/kill-switch-runbook.md` | Stopping the bot fast; what fires automatically; recovery |
| `docs/pre-live-checklist.md` | **Before** `DRY_RUN_ENABLED=false` — every item that must clear |
| `.agency/06-delivery/HONEST-EDGE-REPORT.md` | The honest edge finding (net-of-cost, model-vs-baseline) |
| `.agency/01-specs/EDGE-VERDICT.md` | Why GO-PAPER-ONLY and where a solo desk cannot win |

---

## 9. Recommended next steps

1. **Accept G6 on the facts:** a safe, deployable, honestly-instrumented paper system whose edge is
   correctly reported as unproven.
2. **Authorize R1 shadow/record** (no capital, no orders) to capture ≥ ~3,000 recorded mainnet launches —
   the prerequisite for ever computing GATE-A/GATE-B on anything that means edge.
3. **Run the recorded-data validation program** (the C-5/C-9/C-10/C-11 modules + ≥5-window CPCV) on that
   data. If the gates pass, proceed to the R3 pre-live checklist; if they fail, accept "no edge net of
   cost" as the successful finding.
4. **Only if edge is proven:** complete COND-G4-2 (build + test-prove the signer refusals, harden
   images/host/supply-chain), obtain your own legal confirmation, fund a capped incinerable wallet, and
   sign off R3.
5. The **E1–E13 enhancement program** runs last, after G6, per your reorder.

---

**Bottom line for sign-off:** The system is built, safe-by-construction, runs on one command, and is
driveable in paper from both the dashboard and Telegram. Real capital is disabled and unreachable. The
edge is honestly unproven — and that finding, delivered straight, is exactly what you asked for.
