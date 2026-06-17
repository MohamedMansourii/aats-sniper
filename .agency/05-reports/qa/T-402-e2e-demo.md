# T-402 — END-TO-END PAPER Operator Demo (G4) — backtest-qa-engineer

**Verdict: PASS** (the five T-402 operator-control + safety gates are proven by execution)
**Honesty verdict: LIVE EDGE NOT PROVEN, AND NOT CLAIMED** — there is no recorded real
mainnet data in this build; every model corpus is `is_bootstrap_not_real` synthetic, so
net-of-cost PnL and model-vs-baseline edge **cannot** be established here and this task does
not pretend they are.

Deliverables:
- `tests/e2e/test_t402_operator_demo.py` (16 tests, all green this session)
- `tests/e2e/__init__.py`
- this report

---

## 1. What was booted and how it was driven

A single in-process **BootedStack** wires the FROZEN control-plane server
(`aats.control_plane.server.build_app`, the api-contracts.md app) and a **running controller**
(`ControllerOrchestrator` over a `SimulationVenue`, DRY-RUN) against the **same shared objects** —
one `KillSwitch`, one real `CircuitBreaker`, one `FastLoop`, one `StateStore`, one
survivable-stop/DMS wiring. Because they share state, a POST to the control plane genuinely
mutates the running loop (a kill from the API halts the loop the controller ticks).

Both operator surfaces drive the **same contract**:
- **Dashboard** — raw httpx POST/GET over the booted ASGI app with the operator Bearer token
  (mirrors `dashboard/src/lib/api.ts` `postJSON`/`getJSON`/`EventSource`).
- **Telegram** — the **real** `TelegramCommandHandler` + **real** `HttpControlPlaneClient`
  (`aats/telegram/control_plane_client.py`), with its httpx client re-pointed at the same
  in-process ASGI app (no network, no secret). The real per-command confirm-nonce flow is exercised.

Real capital stays DRY-RUN-disabled throughout (`dry_run_enabled=True`, asserted on `/api/state`).

---

## 2. AC-by-AC evidence table (command run → output → verdict)

Runner: `python -m pytest tests/e2e/test_t402_operator_demo.py` → **16 passed in ~27s** (deterministic; seeded RNG).

| Gate | Test (named) | What it proves by running | Verdict |
|---|---|---|---|
| **KILL flattens within budget** (AC-040) | `test_kill_from_dashboard_flattens_within_budget` | Dashboard POST `/api/kill` → kill flag set on the running loop, `venue.exit()` fired for the OPEN position, position no longer OPEN, **elapsed < 2s**; post-kill new entry returns `kill_switch_active`. | PASS |
| KILL via Telegram | `test_kill_from_telegram_flattens_within_budget` | Real `/kill`→confirm-nonce→`/confirm` over the real HTTP client → same flatten + budget on the OTHER surface, SAME contract. | PASS |
| Single-mint de-risk (AC-044) | `test_flatten_one_from_dashboard_leaves_others_open` | POST `/api/flatten/{mint}` exits only that mint; the other stays OPEN. | PASS |
| **MODE propagates** | `test_mode_change_propagates_dashboard` | POST `/api/mode {PAPER}` → GET `/api/state` reflects `PAPER`; shared controller config equals `PAPER`. | PASS |
| MODE down via Telegram | `test_mode_pause_from_telegram_steps_down` | `/pause` posts the hard-coded downward target → running mode steps to `SHADOW`. | PASS |
| **FEED real events** (AC-047) | `test_feed_carries_real_controller_events` | A controller-published frame (`provenance:"live_controller"`) on the shared `FeedBus` is what the SSE subscriber receives, within **≤ 3s**. NOT a mock seed. | PASS |
| FEED HTTP shape | `test_feed_http_endpoint_is_event_stream` | GET `/api/feed` over the booted ASGI app → `Content-Type: text/event-stream`, `: connected`, and a real `data:` frame. | PASS |
| **SAFETY: breaker fires** (AC-029) | `test_breaker_fires_on_demand_and_flattens` | A −0.5 SOL event-time loss trips the breaker past the −0.30 floor → entries blocked, open book flattened via the FastLoop handler → `venue.exit` fired; operator reset re-arms (200). | PASS |
| SAFETY: asymmetric trust | `test_llm_can_trip_breaker_never_reset` | An `LLMDeRiskSignal` TRIPS the breaker; `reset()` rejects a forged token (LLM has no reset capability). | PASS |
| **SAFETY: survivable stop (L2)** (AC-026) | `test_survivable_stop_layer2_fires_on_breach` | A FAST tick with mark ≤ fixed −40% trigger fires the in-process Layer-2 stop → deregisters, reaches the venue exit, position not OPEN. | PASS |
| **SAFETY: dead-man's switch (L3)** (AC-046) | `test_dms_fires_when_fast_loop_is_killed` | Loop beats once (DMS satisfied), then the loop is **killed** (stops beating); heartbeat ages past T_DMS → DMS (separate domain, reading the same store) fires `emergency_flatten`, latches FIRED. | PASS |
| **DE-RISK ONLY** (AC-060) | `test_mode_up_to_live_rejected_behind_dry_run` | POST `/api/mode {LIVE}` with CEO auth but DRY-RUN on → **403** `live_requires_dry_run_disabled_and_ceo_auth`; mode does NOT advance; `dry_run_enabled` stays true. | PASS |
| DE-RISK: config tighten-only | `test_risk_config_widen_rejected` | Widen per-trade cap → **403** `risk_increase_rejected`; tighten → 200. | PASS |
| DE-RISK: auth | `test_unauthorized_post_rejected` | Every de-risk POST without operator auth → **403**. | PASS |
| DE-RISK: Telegram seam shape | `test_telegram_seam_is_structurally_derisk_only` | `ControlPlaneClient` exposes exactly `{get_status, kill, flatten_mint, pause}` — no risk-increase method **exists**; a non-operator sender fires nothing. | PASS |
| **HONESTY** (AC-037) | `test_no_win_rate_and_no_passing_edge_on_synthetic_build` | `/api/metrics` carries NO `win_rate`; `gate_a_pass` and `gate_b_pass` are **False**, `n_test_windows == 0` — the truthful state of a synthetic build; real capital disabled. | PASS |

### Non-vacuity proof (G-KILL)
A standalone drive (logged) showed `venue.exit_calls == []` **before** kill and
`['NONVAC_MINT']` **after**, with `reason=operator_kill` and the position transitioning to flat
(`None`). The flatten assertion is load-bearing — remove the flatten and it fails.

---

## 3. Findings (defects — filed, not fixed; production code is not mine to patch)

### T-402-F1 — MAJOR — Breaker/StateStore projection lag (observability + entry-gate correctness)
`GET /api/state` and `/api/metrics` read `breaker_tripped` from the **StateStore projection**
(`state_store.load_breaker_state()`), but the real `CircuitBreaker` persists only to its **own**
`BreakerStore`. Nothing copies the breaker's `BreakerState` back into the `StateStore`. Result:
after a real trip the bot is genuinely halted and the book is flat, **but the operator surface
shows `breaker_tripped=False`**. Worse, the **SNIPE loop** reads the same stale projection
(`snipe_loop.py:179 load_breaker_state`), so a freshly-tripped breaker that only updated its own
store would not block entries via that read path (the in-process `breaker.entries_allowed()` does
block — but the two sources can disagree). `POST /api/breaker/reset` is unaffected (it reads
`breaker.state` directly).
- **Repro:** `test_breaker_fires_on_demand_and_flattens` — `breaker.is_tripped()` is True and
  `venue.exit_calls` contains the mint, while `GET /api/state["breaker_tripped"]` is False.
- **Owner:** `agent-orchestration-engineer` / `solana-systems-architect` — a single source of
  breaker truth (shared store, or a projection writer on every trip/reset) is required before G4
  sign-off for the live surface. The test documents the seam with an explicit assertion.

### T-402-F2 — MAJOR (pre-existing, NOT this task's surface) — `claim_entering` CAS is not atomic under contention
`tests/controller/test_snipe_handoff.py::test_concurrent_thousand_snipes_one_winner` (AC-012)
**fails in isolation**: 1000 concurrent `InMemoryStateStore.claim_entering` calls produced **5
winners**, not 1. The atomic snipe→fast handoff (ADR-0007) guarantee is violated for the
in-memory store, which is the store this E2E and many tests rely on. This is intermittent
(it passed inside one 322-test run, failed standalone) — a non-deterministic concurrency defect.
- **Repro:** `python -m pytest tests/controller/test_snipe_handoff.py::test_concurrent_thousand_snipes_one_winner`
  → `AssertionError: Exactly one claim must win; got 5 out of 1000`.
- **Scope:** outside the five T-402 gates, but it undermines the no-double-entry invariant and
  must be tracked. **Owner:** `agent-orchestration-engineer` (state-store CAS).

### T-402-F3 — MINOR (flake) — `test_t342_enforcer` 100-position latency budget is load-sensitive
`tests/controller/test_t342_enforcer.py::...::test_100_position_breach_all_within_budget` failed
once under the parallel 322-test run and **passed in isolation**. A latency budget asserted on a
shared, loaded CI box is flaky. **Owner:** `mev-latency-engineer` / `backtest-qa-engineer` —
pin the budget test to a quiet run or widen the wall-clock tolerance (a flaky validation test is a
defect against the harness, per my own standard).

---

## 4. What was NOT validated (and why)

- **Net-of-cost PnL and model-vs-naive-baseline edge** — NOT validated, by design and by the
  honesty clause. There is no recorded real mainnet data (ingestion has SHADOW/RECORD but no live
  feed; all corpora are `is_bootstrap_not_real`). Any edge number produced from synthetic data
  would be a fabrication; this task asserts the absence of a passing edge gate, not a passing one.
- **Layer-1 venue-native resting stop** — not exercised here (it lives off-box behind the venue
  in production). Layer-2 (in-process) and Layer-3 (DMS) ARE proven; Layer-1 is covered by the
  execution-engineer's suite.
- **Real RPC / Jito / Jupiter fills, Redis Streams, real Telegram/Vault** — replaced with
  deterministic offline fakes (the injection seams the architecture mandates). The SSE path is
  proven on the in-process `FeedBus`; `RedisStreamFeedReader` (the prod adapter) is covered by the
  existing `tests/control_plane/test_sse_feed.py`.
- **Multi-process kill** (killing the OS process mid-trade) — approximated deterministically: the
  DMS reads a cross-process heartbeat **seam** (`InMemoryHeartbeatStore`) and the "kill" is the
  loop ceasing to beat while the watchdog's injected clock advances past T_DMS. The watchdog holds
  no reference to the loop, so this is faithful to a dead process; a true OS-level process kill is
  the `latency-devops-engineer`'s deployment-level test.

---

## 5. Self-check (mandatory items, run this session)

1. **Suite green:** `tests/e2e/test_t402_operator_demo.py` → **16 passed in 26.96s** (deterministic).
2. **Kill within budget:** asserted `elapsed < 2.0s` (AC-040) on both the dashboard and Telegram
   surfaces; non-vacuity proven (exit_calls empty pre-kill, populated only by the flatten).
3. **Mode propagates:** `/api/mode` change reflected on `/api/state` and in the shared controller config.
4. **Feed real events:** the SSE subscriber receives a controller-published `provenance:"live_controller"`
   frame within ≤ 3s; GET `/api/feed` returns `text/event-stream` with a real `data:` frame.
5. **Safety fires on demand:** breaker trips+flattens; Layer-2 stop fires on a breach tick; DMS
   fires when the loop is killed past T_DMS and latches.
6. **De-risk only:** mode-up-to-LIVE rejected behind DRY-RUN (403), risk-config widen rejected
   (403), all de-risk POSTs require auth, Telegram seam is structurally de-risk-only.
7. **Honesty enforced:** no `win_rate` field; edge gates not passing (`gate_a_pass`/`gate_b_pass`
   False, `n_test_windows==0`); DRY-RUN on. No edge was targeted, tuned-toward, or fabricated.
8. **No production code patched** — only `tests/e2e/*` authored. Findings F1/F2/F3 are filed for
   their owners.

---

```
=== HANDOFF ===
FROM: backtest-qa-engineer
TASK: T-402 — END-TO-END PAPER operator demo (G4)
STATUS: COMPLETE
DELIVERABLES: tests/e2e/test_t402_operator_demo.py; tests/e2e/__init__.py; .agency/05-reports/qa/T-402-e2e-demo.md
SELF-CHECK: ran `pytest tests/e2e/test_t402_operator_demo.py` -> 16 passed (deterministic).
  Proved by execution: KILL flattens all positions <2s from BOTH dashboard and Telegram surfaces
  (same contract); MODE change propagates to /api/state; SSE /api/feed carries a real
  controller-published frame (not mock) within <=3s; breaker + Layer-2 survivable-stop + Layer-3
  DMS each fire on demand; risk-increasing commands (mode-up-to-LIVE, risk-config widen, no-auth)
  are rejected; Telegram seam is structurally de-risk-only. Non-vacuity of the kill-flatten gate
  demonstrated. HONESTY: no edge/win-rate claimed or fabricated; edge gates asserted NOT passing
  on the synthetic (is_bootstrap_not_real) build; real capital DRY-RUN-disabled throughout.
RISKS: T-402-F1 (MAJOR) breaker state not projected into StateStore -> /api/state + SNIPE-loop
  read a stale breaker_tripped; T-402-F2 (MAJOR, pre-existing, off-T-402-surface) claim_entering
  CAS produced 5 winners of 1000 (AC-012 atomicity); T-402-F3 (MINOR) enforcer latency budget
  test is load-flaky. LIVE EDGE remains UNPROVABLE offline (no recorded real data) — by design.
NEEDS: orchestrator to route F1/F2 to agent-orchestration-engineer / solana-systems-architect and
  F3 to mev-latency-engineer. T-402 operator-control + safety gate: PASS.
===============
```
