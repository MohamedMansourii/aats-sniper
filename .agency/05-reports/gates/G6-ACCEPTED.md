# GATE G6 — Acceptance — VERDICT: ACCEPTED

**Gate:** G6 (Acceptance) — the final core-build gate.
**Approver:** **agency-autonomous per `.agency/AUTONOMY-DIRECTIVE.md`** (G6 auto-approved on the agency's own PASS recommendation; no CEO pause).
**Verdict author:** `orchestrator` (Delivery Lead) + `code-reviewer` (T-600 dual-G3 PASS) + `backtest-qa-engineer` (final verification).
**Date:** 2026-06-17
**Task gated:** T-600 (delivery package: `DELIVERY.md` + `HONEST-EDGE-REPORT.md` + README refresh).
**Build state:** PAPER / DRY-RUN. Real capital **DISABLED by default and unreachable**.

**Verdict:** **ACCEPTED** — a safe, deployable, honestly-instrumented PAPER system. All five BRIEF §4 acceptance criteria are met; all locked HARD RULES hold. The live edge is honestly **UNPROVEN-NO-REAL-DATA** — the correct, accepted PAPER outcome per the brief's honesty clause, **not** a failure. Turning on real capital (R3) is the one decision the agency does not make alone; it is gated behind a documented pre-live checklist that is **not yet met**.

Method: every criterion checked against the ACTUAL files under `C:/dev/aats` (read, not trusted from the handoff JSON). The T-600 `code-reviewer` PASS and the `backtest-qa-engineer` final verification both re-ran the load-bearing checks first-hand this session (full suite, T-402 e2e, edge-gate proof, compose config, win-rate scan, secret sweep).

---

## 1. BRIEF §4 acceptance criteria vs evidence

Source of criteria: `.agency/00-brief/AATS-BRIEF.md §4` ("Definition of done — Gate G6 acceptance").

| # | BRIEF §4 criterion | Verdict | Evidence (file path / executed check) |
|---|---|---|---|
| **1** | Bot runs end-to-end vs **SimulationVenue** (paper) + honest **net-of-cost PnL** report (net of modeled tips/fees/slippage) + **model-vs-naive-baseline** measured | **MET** | T-402 e2e (`tests/e2e/test_t402_operator_demo.py`, **16 passed**) boots the FROZEN control-plane + controller over a `SimulationVenue` against shared objects — an operator command genuinely mutates the running loop. Edge metrics are **GATE-A** (net-of-cost PnL, `aats/models/gate_a.py`) + **GATE-B** (model−baseline net-PnL-per-risk, `aats/models/gate_b.py`), both net of the **310 bps** cost stack (`tests/validation/harness.py`), lower-95% bootstrap bound. Harness BUILT + proven correct (`tests/validation/` 22 passed). `HONEST-EDGE-REPORT.md` §1-3. **NOTE:** the brief's loose "hit rate" phrasing is superseded by AUTONOMY-DIRECTIVE locked HARD RULE 2 ("no win-rate target or claim") — the agency delivered net-of-cost PnL + model-vs-baseline and **no win-rate anywhere**; this is the correct, mandated substitution. |
| **2** | **Daily-loss circuit breaker + survivable stop + dead-man's switch** IMPLEMENTED + PROVEN (QA fires them) | **MET** | All three built (T-320 / T-321 / T-322, dual G3 PASS, PROVEN-BY-FIRING) and re-proven by firing in the T-402 e2e this session: breaker trips past the −0.30 SOL floor and flattens through `FastLoop`; Layer-2 survivable stop fires on a −40% breach tick (deterministic, no LLM/RPC on path) → `venue.exit`; Layer-3 DMS fires when the FAST loop is killed (heartbeat ages past `T_DMS=60s`) from its OWN failure domain and **latches** (process-killed-mid-trade survivability proof). Kill flattens the open book **< 2s** (AC-040 budget asserted on measured elapsed). |
| **3** | Pre-trade safety gate **rejects honeypot/rug** patterns in sim | **MET** | T-323 sub-10ms pre-trade gate (AC-011 corpus **20/20 rejection MEASURED** with logged on-chain reason; p99 ≈ 60µs; 0-IO/0-RPC/0-LLM) + T-329 adversarial sell-sim honeypot fingerprints (revert / zero-out / sell-tax>thresh / signer-refusal / infra-err); risk gate de-risks on `RedFlag.NOT_SELLABLE`. Surfaced as dashboard red flags (T-353). |
| **4** | Deployable: **ONE `docker compose up`** on a single host; secrets via **`.env.example`** (trade-only capped wallet, never main holdings); **monitoring + alerting live** | **MET** | `docker compose config --quiet` → **exit 0**, 11 services validated (re-run this session). `DRY_RUN_ENABLED:-true` default on all 5 tx-capable services; `redis` + `aats-signer` publish no ports (ADR-0009). Monitoring: Prometheus / Grafana / Alertmanager in the topology (no win-rate panel). Secrets: only `.env.example` (placeholders + FORBIDDEN banner; no `WALLET_PRIVATE_KEY`/`KEYPAIR_JSON` var by design); trade-only capped incinerable wallet documented (`WALLET_MAX_BALANCE_LAMPORTS` ≤ 2 SOL, R3 only). `DELIVERY.md §3/§8`, `G5-PASS.md`. |
| **5** | Docs: **README + deploy/ops guide + kill-switch runbook** | **MET** | `README.md` (refreshed 1803→1842), `docs/deploy-ops-guide.md`, `docs/dashboard-operator-guide.md`, `docs/telegram-operator-guide.md`, `docs/kill-switch-runbook.md`, `docs/pre-live-checklist.md` — all present, link-checked at T-501, honest framing intact (G5-PASS.md). |

**All five BRIEF §4 criteria: MET (`acAllMet = true`).**

---

## 2. Locked HARD RULES (AUTONOMY-DIRECTIVE §"What this does NOT waive") — independently re-verified

| HARD RULE | Verdict | Evidence |
|---|---|---|
| **NO win-rate** target/field/claim anywhere | **HOLDS** | Win-rate scan over both deliverables + README + `aats/` returns **only** explicit negations/honesty clauses (`inference.py:154`, `monitor.py:29`, `alerts.py:137` are absence-proofs). Runtime guard `control_plane/server.py:768` `assert "win_rate" not in snap` on `GET /api/metrics`; e2e `test_no_win_rate` confirms the field absent AND `gate_a_pass==False`, `gate_b_pass==False`, `n_test_windows==0` on the synthetic build. **Zero number, target, or claim.** |
| **Edge honestly UNPROVEN** (real capital only after net-of-cost PnL **and** model-vs-baseline proven positive on RECORDED data) | **HOLDS** | `edgeVerdict = UNPROVEN-NO-REAL-DATA (GO-PAPER-ONLY)`. `test_edge_gate_proof` confirms corpus stamped `is_bootstrap_not_real`, gates fail-closed on empty, gate-A/gate-B FAIL on synthetic, purge/embargo walk-forward load-bearing (`test_purge_is_load_bearing`). No recorded data; no edge manufactured. The finding "unproven" is the deliverable. |
| **Real capital DISABLED** behind hard DRY-RUN | **HOLDS** | `ControlPlaneConfig.dry_run_enabled` defaults true; `JitoJupiterVenue.submit_mode` = DRY_RUN at runtime (live only when `_live_submit_enabled` AND `_dry_run_env_disabled`); compose `DRY_RUN_ENABLED:-true`; LIVE hard-gated on dry-run disabled + CEO auth (`server.py:1115`); wallet unfunded. No submit path reachable. |
| **Safety built first**, proven before any live path | **HOLDS** | T-320/321/322 built + PROVEN-BY-FIRING before T-327's live-capable path was enabled; re-proven in T-402 this session (criterion 2 above). |
| **Asymmetric trust** (no signal/LLM raises risk; LLM never on FAST path) | **HOLDS** | `ReasoningAction` has exactly four de-risk members (size-up/widen-stop/add-leverage type-inexpressible); AST analysis of `aats/controller/fast_loop.py` → 0 `await`, 0 `async def` (LLM cannot block the critical path; LLM-derived input can ONLY force an exit). |
| **Money int/Decimal; no secrets in code/logs/images** | **HOLDS** | lamports/PnL fields int with `_reject_float_lamports` validators that RAISE on float (`api_schemas.py:71/173/222`, `risk.py:152`); remaining floats are non-money. `git ls-files` shows only `.env.example`; tracked-tree + git-history secret sweep clean. |
| **Suite GREEN (1842)** | **HOLDS** | Full suite **1842 passed / 2 skipped / 0 failed** (PYTHONHASHSEED=0, `-p no:cacheprovider`, `__pycache__` purged) — reproduced on a second deterministic run. The 2 skips are the benign solders-gated `tests/execution/test_tx_builder.py:161/:186` (`_build_swap_accounts` not on the live path). No safety/edge/leak test skipped. |

---

## 3. The honest edge verdict (the deliverable)

**`edgeVerdict = UNPROVEN-NO-REAL-DATA (GO-PAPER-ONLY)`.**

There is a *plausible, structurally-defensible* edge on a narrow set of surfaces (selection + exit discipline — the bot is DETECTION-COMPETITIVE, SUBMISSION-DISADVANTAGED; block-0 and migration-block-0 races are DESCOPED as unwinnable for a solo unstaked desk), but it has **not been demonstrated on recorded mainnet data**. The GATE-A / GATE-B acceptance harness is BUILT and proven to compute correctly (right sign on planted oracle/anti-oracle controls; frozen naive baseline FAILS GATE-A at −55.36 SOL net of cost; declines→0; net-of-cost; leak-free clean-room; purge load-bearing; deterministic). **Every number to date is `is_bootstrap_not_real` synthetic** and licenses zero capital. No agent targeted, tuned toward, or fabricated a passing edge or win-rate.

Per the brief's non-negotiable honesty clause (§5): if the edge is not demonstrable net of costs, the correct deliverable is *that finding* — not a bot trading live. The agency delivered exactly that. Source: `.agency/06-delivery/HONEST-EDGE-REPORT.md`, `.agency/01-specs/EDGE-VERDICT.md`, `.agency/05-reports/qa/T-401-edge-proof.md`.

---

## 4. Gate history (G0–G6) — all recorded

| Gate | Name | Verdict | Record |
|---|---|---|---|
| pre-G0 | Edge gate | **GO-PAPER-ONLY** (13 conditions C-1..C-13) | `EDGE-VERDICT.md` |
| G0 | Scope | **APPROVED** (agency-autonomous) | `G0-APPROVED.md` |
| G1 | Architecture | **APPROVED** (agency-autonomous) | `G1-APPROVED.md` |
| G2 | Design | FOLDED into Lane E (right-sized, not skipped) | — |
| G3 | Build (per task) | **COMPLETE** (milestone path, dual `code-reviewer`+`backtest-qa-engineer` PASS) | `G3-wave*.md`, `G3-stabilization.md` |
| G4 | Integration | **PASS (conditional)** | `G4-PASS.md` |
| G5 | Release | **PASS** | `G5-PASS.md` |
| **G6** | **Acceptance** | **ACCEPTED (agency-autonomous)** | **this file** |

---

## 5. CARRIED — Pre-Live (R3) checklist before `DRY_RUN_ENABLED=false`

These are **NOT G6/PAPER blockers**. They are the documented HARD precondition (COND-G4-2 + edge + legal) that must clear in full before real capital is ever enabled. Current build status: **A = NOT MET, B = NOT MET, C = NOT GIVEN** — the correct, honest paper-deliverable state. Source of truth: `docs/pre-live-checklist.md`.

**Block A — Edge proven on RECORDED data (NOT MET):**
- R1 recording complete (≥ ~3,000 mainnet launches, SHADOW mode, point-in-time first-K-slot features + event-time labels).
- Completeness/survivorship bounded (C-6); leak/clock audit clean re-run on recorded data (C-5, C-7); baseline frozen (C-4).
- Adverse-selection haircut calibrated from recorded fills (C-11), widen-only from the 150 bps floor; if > 200 bps, EH-001 re-derived or killed.
- Experiment log + trial-count deflation (C-9); group-aware purge by creator/bundler/deploy-template (C-10).
- **GATE-A PASS** (net-of-cost PnL, lower-95% bound > 0) **AND GATE-B PASS** (model beats frozen baseline, lower-95% bound > 0) on purged/embargoed walk-forward windows.
- Tip-contention stratification (C-3); independent-surface report (C-13).
- *If A fails, "no edge net of cost" is the correct, successful deliverable — do not fund.*

**Block B — Custody & security hardened, COND-G4-2 (NOT MET):**
- F-01: `aats-signer` three refusals **built + test-proven** (per-tx + rolling SOL spend cap; full program-ID allowlist; Jito-tip-account-pinned transfers) + Vault short-lived token / `mlock` / zeroize secret handling (currently a scaffold).
- F-10: placeholder `@sha256:placeholder` image digests replaced with real verified digests.
- F-07: signer container locked down (cap_drop[ALL] + IPC_LOCK, no-new-privileges, read-only rootfs, socket-only network) + host hardening.
- F-02: hash-locked deps (`--require-hashes`); F-03: CI CVE scan (pip-audit/OSV; currently INCONCLUSIVE offline); F-04: GH Actions pinned to SHAs.
- Secret-clean property re-verified on the committed tree + history.

**Block C — CEO legal + funding authorization (NOT GIVEN):**
- Legal confirmation for the operator's jurisdiction (OQ-009) — the agency makes no legal representation.
- Funding policy: dedicated trade-only, capped, incinerable wallet (`WALLET_MAX_BALANCE_LAMPORTS` ≤ 2 SOL), never main holdings.
- Risk floors tightened (never loosened) for the live tranche.
- Explicit R3 sign-off recorded (the `NEEDS-CEO-DECISION` the agency does not make alone).

**Flip rule:** `DRY_RUN_ENABLED=false` is legal only when A + B + C are all green; R3 is a FRESH proof, not a continuation of R2.

---

## 6. Non-blocking loose ends (recorded, OFF the milestone path)

- **T-326** (limit + DCA resting orders): production fix landed + flake empirically eliminated (10x stability gate green); only its dual-G3 *verdict* is missing (att.3 re-dispatch died — a PROCESS event, not a content strike). Bookkeeping only, OFF the milestone path.
- **T-402-F3**: a latency-budget test is load-sensitive (minor test-hygiene note).
- **T-106**: cosmetic spec traceability fix.

None affect the PAPER deliverable or G6 acceptance.

---

## 7. Verdict

**G6: ACCEPTED (agency-autonomous per `.agency/AUTONOMY-DIRECTIVE.md`).** The AATS ultra-sniper is built, safe-by-construction, runs on one `docker compose up`, and is driveable in PAPER from both the dashboard and Telegram, de-risk-only. The safety stack (breaker + survivable stop + DMS) is proven by firing. The pre-trade gate rejects honeypot/rug in sim. Monitoring + alerting are live. Docs are complete and honest. Real capital is DISABLED and unreachable. The live edge is honestly **UNPROVEN-NO-REAL-DATA** — and that finding, delivered straight with no win-rate anywhere, is exactly what the brief's honesty clause asked for.

**The core build (G0–G6) is COMPLETE.** Next and final: the **E1–E13 enhancement program** (`.agency/00-brief/ENHANCEMENT-DIRECTIVE-E1-E13.md` §ORDERING — runs as the final step, AUDIT-FIRST, additive; E1 Devnet live-send + E4 top priority). Real capital stays DISABLED behind DRY-RUN throughout.
