# api-contracts.md — FROZEN Control-Plane API Contract (T-201)

**Version:** 1.0.0 — **FROZEN AT G1.**
**Author:** `solana-systems-architect`
**Date:** 2026-06-16
**Status:** This contract is LAW. The control-plane server (Lane D, `agent-orchestration-engineer`,
T-341), the operator dashboard (Lane E, `frontend-engineer`, T-352), and the Telegram channel
(Lane F, `backend-engineer`, T-360/361) ALL build to this and only this. After G1, the
`solana-systems-architect` is the only agent who may change this contract, and any change ships as a
**new ADR + a delta notice** listing every affected board task — never a silent in-place edit.

**Reconciliation source:** `dashboard/src/lib/api.ts` `ENDPOINTS` + `dashboard/src/lib/types.ts`.
This contract matches those endpoint paths exactly. Where the existing dashboard types must adjust
to the canonical wire schema, the exact delta is called out in §2 and §13 (these are Lane-E
transcription tasks, permitted because this frozen contract supersedes the mock types).

---

## 1. Principles (binding on every endpoint)

1. **GET = read-only. POST = de-risk-only.** There is no endpoint, no field, and no body value that
   can increase risk (size up, widen a stop, add leverage, override a hard stop, advance capital
   automatically). A command that *could* increase risk is **rejected at the contract layer** (HTTP
   4xx), not merely discouraged. (BUILD-DIRECTIVE HARD RULES; SPEC §10.)
2. **Money is integer base units / Decimal-as-string, NEVER float.** Every monetary field on the
   wire is either an **integer of base units** (lamports for SOL; token base units for token
   amounts) OR a **decimal string** (e.g. `"net_pnl_sol": "-0.0123"`) parsed to `Decimal`. No JSON
   number is ever used for a monetary quantity. (NFR-009, FR-042, applies to dashboard too.)
3. **Event-time stamping.** Every event/record carries `event_time` (the canonical decision slot +
   wall-clock), separate from any `recorded_at`/compute-time. (C-5.)
4. **Authorized operator only.** Every POST requires operator auth (bearer/session for the
   dashboard; Telegram user-ID allowlist for the channel, OQ-004). Unauthorized POST → 403.
5. **Idempotent de-risk.** `kill`, `flatten`, `breaker/reset` are idempotent — repeating a de-risk
   command is always safe and never escalates.

---

## 2. Canonical mode enum (the one reconciliation that needs a dashboard delta)

The wire enum for `AgentMode` is **FROZEN** as the 4-value staging ladder from SPEC §10:

```
SHADOW | PAPER | LIVE_DRY_RUN | LIVE
```

- `SHADOW` — record only, submit nothing (default startup mode, FR-004).
- `PAPER` — full triple loop vs `SimulationVenue` / recorded replay; no real submit.
- `LIVE_DRY_RUN` — real venue path **builds and signs** but does NOT submit (FR-039 dry-run).
- `LIVE` — real submit. Settable **only** if `DRY_RUN_ENABLED=false` (config, not a POST) AND CEO
  auth token present, else 403 `live_requires_dry_run_disabled_and_ceo_auth` (AC-060).

**Dashboard delta (Lane E, T-352):** `dashboard/src/lib/types.ts` currently declares
`AgentMode = "paper" | "dry-run" | "live"`. Lane E MUST update this to the canonical 4-value enum
above (string-equal, uppercase). The mock (`mock.ts setMockMode`) and the `/mode` POST body adopt the
canonical values. This is a transcription change, not a redesign; the frozen contract is the source
of truth. No other dashboard type requires a value change (field shapes below are supersets of the
existing mock types and are additively compatible).

---

## 3. Transport, errors, base URL

- Base URL: `VITE_CONTROL_PLANE_URL` (dashboard) / control-plane host (Telegram). All paths under
  `/api`.
- Content-Type `application/json` for request/response bodies; `/api/feed` is `text/event-stream`.
- Error envelope (all non-2xx): `{ "error": "<machine_code>", "message": "<human>", "detail": {...} }`.
- Standard codes: `200` ok; `202` accepted (async de-risk initiated); `400` bad request; `403`
  unauthorized or risk-increase rejected; `409` conflict (e.g. breaker reset when not tripped);
  `503` degraded.

---

## 4. GET endpoints (read-only)

### GET `/api/state` → `AgentState`
Current mode, loop states, connection, wallet, kill flag. (Matches `useAgentState`.)
```jsonc
{
  "mode": "SHADOW",                       // canonical enum §2
  "loops": { "snipe": "idle|armed|firing", "fast": "string", "slow": "string" },
  "connection": { "geyser": true, "shredstream": false, "internal_ms": 67 },
  "wallet": { "pubkey": "string", "balance_lamports": 0, "cap_lamports": 500000000 },
  "dry_run_enabled": true,                // mirrors the hard DRY-RUN flag (read-only here)
  "breaker_tripped": false,
  "killed": false
}
```
> Note: wallet balances are **lamports (integer)**, not `balanceSol` float. Dashboard formats for
> display; it never computes money in float (NFR-009).

### GET (SSE) `/api/feed` → stream of `SnipeEvent` frames
See §6 for the SSE event schema.

### GET `/api/metrics` → `MetricsSnapshot`
```jsonc
{
  "net_pnl_sol": "-0.0123",               // decimal string; PRIMARY, always net (AC-036)
  "gross_pnl_sol": "0.0044",              // secondary, explicitly labeled gross
  "land_rate_pct": 38.2,
  "median_slot_delay": 7,
  "rug_avoidance_pct": 61.0,
  "tip_efficiency": 0.42,
  "model_vs_baseline": {                  // GATE-B headline (AC-037)
    "delta_net_pnl_per_sol_at_risk": "0.013",
    "lower_95_bound": "0.004",
    "n_test_windows": 5,
    "gate_b_pass": true
  },
  "gate_a": { "net_pnl_sol": "0.021", "lower_95_bound": "0.006", "gate_a_pass": true },
  "open_positions": 2,
  "daily_pnl_sol": "-0.0123",
  "daily_loss_limit_sol": "-0.30",        // floor (OQ-001/005)
  "breaker_tripped": false
}
```
> No win-rate field exists anywhere in this schema (HONESTY CLAUSE; AC-037).

### GET `/api/positions` → `Position[]`
```jsonc
[{
  "mint": "string", "token": "string",
  "entry_slot": 12345,
  "sol_in_lamports": 100000000,           // integer
  "unrealized_pnl_net_lamports": -120000, // integer, labeled net (AC-039)
  "entry_slip_bps": 145,
  "tp_hit": 1, "tp_total": 3,
  "trailing_armed": false,
  "hard_stop_pct": -40.0,
  "exit_mode": "secure|fast",
  "fsm_state": "OPEN|ENTERING|CLOSING|CLOSED|VETOED",
  "age_sec": 42,
  "status": "open|closed",
  "realized_pnl_net_lamports": null,
  "surface": "EH-001|EH-003|...",
  "cost_breakdown": { "tip_lamports": 0, "priority_lamports": 0,
                      "entry_slippage_bps": 145, "amm_fee_bps": 50,
                      "exit_slippage_bps": 0, "adverse_selection_bps": 150 }
}]
```

### GET `/api/latency` → `LatencyBudget` (+ `tiers`)
Splits internal compute from block-engine RTT (C-1; AC-050). `tiers` is the InfraTier array the
existing `useInfraTiers` hook reads from `budget.tiers`.
```jsonc
{
  "hops": [ { "name": "ingress_detect", "ms": 60, "budget_ms": 70,
              "class": "internal_compute" },
            { "name": "block_engine_rtt", "ms": 55, "budget_ms": 60,
              "class": "submission" },
            { "name": "leader_land", "ms": 50, "budget_ms": 400,
              "class": "submission", "p99_ms": 450, "note": "+1 slot under contention" } ],
  "internal_ms": 67,
  "block_engine_rtt_ms": 55,
  "slot_floor_ms": 400,
  "posture": "DETECTION-COMPETITIVE, SUBMISSION-DISADVANTAGED",
  "tiers": [ { "name": "dedicated_geyser", "land_rate": 0.38,
               "median_slot_delay": 7, "entry_slip_pct": 1.45, "active": true },
             { "name": "colo_shred", "land_rate": 0.0,
               "median_slot_delay": 0, "entry_slip_pct": 0.0, "active": false } ]
}
```

### GET `/api/sentiment` → `MCSScore[]`
MCS per tracked asset, including the adversarial features (synchronicity, account-age, red flags).
Higher synchronicity → lower conviction (FR-008, AC-010). Schema matches existing `MCSScore`.

### GET `/api/predictions` → `Prediction`
Calibrated classifier probability + baseline probability + calibration bins + feature importance.
Adds `uncertainty` band (FR-014). Matches existing `Prediction` plus `"uncertainty": 0.12`.

### GET `/api/reasoning` → `Reasoning[]`
LLM veto log. **Includes clamp events** (AC-054): each entry carries `action_received`,
`action_applied`, `risk_increase_clamped: bool`. Matches existing `Reasoning` plus those three
fields.

### GET `/api/risk-config` → `RiskConfig`
Current risk config (read). See §5 for the de-risk-only POST.

### GET `/api/health` → `ModuleHealth[]`
Module status + staleness. A Geyser feed age > 1,200 ms surfaces as `degraded/STALE` (FR-057,
AC-051). Matches existing `ModuleHealth`.

---

## 5. POST endpoints (de-risk ONLY — risk-increase rejected at the contract layer)

### POST `/api/risk-config` → `RiskConfig`
**TIGHTEN-ONLY.** The server MUST reject (`403 risk_increase_rejected`) any body that would *widen* a
limit beyond the current value or beyond the hardcoded floor. Concretely (OQ-001/005):
- `per_trade_cap_lamports`: may only **decrease**; hard floor 0.1 SOL (`100000000` lamports) is a
  ceiling on this value — a POST may not set it above the current value, and never above the floor.
- `max_aggregate_lamports`: may only decrease; floor 0.5 SOL (`500000000`).
- `daily_loss_limit_sol`: may only become **more negative or equal** (tighter); never wider than
  `-3.0%` of tranche, and the absolute `-0.30 SOL` floor is independent (OQ-001).
- `max_slippage_bps`, `snipe_threshold` (raising the threshold is de-risk; lowering it is reject),
  `jito_tip_cap`: tighten-only.
- Widening any limit requires an **explicit R3 config change** (file + deploy), not this API.

Request body = full `RiskConfig`; server validates field-by-field against current + floors and
returns the persisted config or 403 with `detail.violations: [<field>...]`. (AC-031 direction.)

### POST `/api/kill` → `202`
Halt all new entries immediately; hand open positions to ExitEngine + survivable-stop. Idempotent.
Effect within ≤2s end-to-end (FR-055, AC-040). No body.

### POST `/api/flatten` → `202`
Flatten ALL open positions via ExitEngine. Idempotent. ≤2s (AC-040).

### POST `/api/flatten/{mint}` → `202`
Flatten a single position; other positions unchanged (AC-044). `{mint}` URL-encoded.

### POST `/api/breaker/reset` → `200` | `409`
Re-arm the daily-loss circuit breaker **after manual review**. Requires: breaker currently
`TRIPPED` (else `409 breaker_not_tripped`) AND operator auth. **No automated path and no LLM may
call this** (AC-029; the LLM may *trip* the breaker, never reset it). This is the *only* POST whose
effect is "less halted" — and it is gated on an explicit human-reviewed operator action, so it
cannot silently re-arm after a crash/restart (the FSM restores `TRIPPED`, never `ARMED`).

### POST `/api/mode` → `200` | `403`
Body `{ "mode": "SHADOW|PAPER|LIVE_DRY_RUN|LIVE" }`. Mode may only advance toward live by **explicit
authorization**. `LIVE` requires `DRY_RUN_ENABLED=false` (config) AND CEO auth token, else
`403 live_requires_dry_run_disabled_and_ceo_auth` (AC-060). Moving *down* the ladder (toward SHADOW)
is always permitted (de-risk). The mode POST is the one place "risk posture" changes, and it is
fenced so that real-capital exposure cannot happen by accident.

---

## 6. SSE `/api/feed` event schema

`text/event-stream`. Each frame is `data: <SnipeEvent JSON>\n\n`. The dashboard's existing
`useSnipeFeed` parses `JSON.parse(msg.data) as SnipeEvent`. Frozen `SnipeEvent`:

```jsonc
{
  "id": "string",
  "event_time": { "slot": 12345, "wall_clock_ms": 1718500000000 },  // C-5 event-time
  "observation_slot": 12345,          // may precede confirmation_slot when ShredStream (AC-003)
  "confirmation_slot": 12346,
  "detection_transport": "geyser|shredstream",
  "ts": 1718500000000,                // compute/emit time — distinct from event_time
  "slot": 12345,
  "token": "string",
  "mint": "string",
  "source": "pump.fun|pumpswap|raydium_v4|raydium_cpmm|migration",
  "gate_passed": true,
  "gate_reasons": ["freeze_authority|mint_authority|lp_unlocked|sniper_cluster|high_tax|low_liquidity|passed"],
  "red_flags": ["freeze_authority", "..."],  // token-safety scanner (FR-037, AC-015)
  "model_p": 0.31,                    // calibrated probability (null if no score)
  "model_uncertainty": 0.12,
  "action": "sniped|skipped|vetoed",
  "veto_source": "gate|llm|cost_gate|null",
  "slot_delay": 7,
  "smart_wallets": 0,                 // adversarial selectivity feature (never a buy trigger)
  "tip_contention_bucket": "low|medium|high",   // C-3 stratification input
  "cost_gate": { "expected_edge_bps": 80, "total_cost_bps": 240, "passed": false },
  "pnl_pct": null
}
```

Field naming reconciliation: the existing mock `SnipeEvent` uses `gatePassed`, `gateReasons`,
`modelP`, `slotDelay`, `smartWallets`, `pnlPct` (camelCase). **The wire schema is snake_case** as
above; Lane E maps wire snake_case → its camelCase view model in `api.ts` (a thin adapter in the
non-mock branch). The mock branch is unchanged. This keeps the existing 10 pages' prop shapes intact
while the wire stays consistent with the Python/Rust contracts (data-models.md).

---

## 7. The de-risk-only command semantics (what BOTH dashboard and Telegram bind to)

| Command | Dashboard | Telegram | Direction | Contract guarantee |
|---|---|---|---|---|
| status | GET `/api/state`+`/api/metrics` | `/status` | read | — |
| kill | POST `/api/kill` (modal confirm, AC-041) | `/kill` (confirm prompt, AC-042) | de-risk | halt entries + flatten ≤2s |
| flatten all | POST `/api/flatten` | (operator-gated) | de-risk | all positions ≤2s |
| flatten one | POST `/api/flatten/{mint}` | `/flatten <mint>` | de-risk | only that mint (AC-044) |
| pause | POST `/api/mode {PAPER\|SHADOW}` (down only) | `/pause` | de-risk | mode steps DOWN only |
| breaker reset | POST `/api/breaker/reset` | NOT exposed on Telegram | de-risk-gate | requires TRIPPED + auth |
| risk tighten | POST `/api/risk-config` (tighten) | NOT exposed | de-risk | tighten-only, 403 on widen |

There is **no** size-up, stop-widen, leverage, or "go live" command on either surface. `LIVE` mode is
not reachable from Telegram at all, and on the dashboard only via the explicitly-gated `/api/mode`
with DRY-RUN off + CEO auth. This is the structural embodiment of "operator surfaces may only
de-risk" (BUILD-DIRECTIVE HARD RULES).

---

## 8. Server obligations (Lane D, T-341)

- Implement every endpoint above **exactly**; no additional POST command endpoints (especially none
  that could increase risk).
- Reject risk-increase at the validation layer with `403 risk_increase_rejected` and a `violations`
  list.
- `/api/feed` emits from the `ops.feed` Redis stream; ≤3s lag (FR-056, AC-047).
- All monetary fields integer-lamports or decimal-string; a CI schema test asserts no monetary field
  is a JSON float.
- Enforce auth on every POST (custody policy, T-251).

## 9. Dashboard obligations (Lane E, T-352)

- Adopt the canonical `AgentMode` enum (§2 delta).
- Keep `VITE_USE_MOCK=true` building green (NFR-011, AC-049); the live branch maps wire snake_case →
  view model.
- Format money from integer/decimal-string wire values; never parse to float for arithmetic (AC-036).

## 10. Telegram obligations (Lane F, T-360/361)

- Bind to the SAME endpoints; expose only `/status /kill /flatten[mint] /pause`.
- Enforce operator user-ID allowlist (OQ-004, AC-043); unauthorized → no API call.

---

## 11. Post-G1 change protocol (this contract is frozen)

A change to any endpoint, field, or enum value here requires: (1) a new ADR, (2) a **delta notice**
appended to this file listing every affected board task (T-341 server, T-352 dashboard, T-360/361
Telegram, plus any consumer), (3) the Orchestrator re-dispatching those tasks. No in-place edit that
silently breaks a downstream lane. Only the `solana-systems-architect` may issue it.

> **G1 red-team note (no change).** The G1 red-team (leak-proofness, custody, code-reviewer) touched
> `data-models.md`, `infrastructure.md`, `execution-venue.md`, `validation-harness.md`,
> `latency-budget.md`, and added ADR-0009/0010 (BLUEPRINT §14). **This frozen control-plane wire
> contract is UNCHANGED by all of it** — no endpoint, field, or enum value moved. The code-reviewer's
> R-03 (AgentMode 4-value enum, money formatting, `SnipeEvent` snake_case→camelCase adapter) was
> confirmed already specified in §2/§6/§13 as Lane-E (T-352) transcription, with `api.ts` `ENDPOINTS`
> matching the frozen set exactly (§13). No contract edit was required or made.

## 12. Frozen endpoint list (the canonical set)

`/api/state`, `/api/feed` (SSE), `/api/metrics`, `/api/positions`, `/api/latency`, `/api/sentiment`,
`/api/predictions`, `/api/reasoning`, `/api/risk-config` (GET+POST), `/api/health`, `/api/kill`,
`/api/flatten`, `/api/flatten/{mint}`, `/api/breaker/reset`, `/api/mode`.

## 13. Reconciliation summary vs `dashboard/src/lib/api.ts`

| `api.ts` ENDPOINTS key | Path | Status |
|---|---|---|
| state, feed, metrics, positions, latency, sentiment, predictions, reasoning, riskConfig, health | identical | MATCHED — no path change |
| kill, flatten, breakerReset, mode | identical | MATCHED |
| (flatten/{mint}) | `flatten(mint)` already posts to `${flatten}/${mint}` | MATCHED |

Type deltas Lane E must apply: (a) `AgentMode` → 4-value canonical enum (§2); (b) money fields read
as integer-lamports/decimal-string and formatted (NFR-009); (c) live-branch snake_case→camelCase
adapter for `SnipeEvent` (§6); (d) additive fields (`uncertainty`, `red_flags`, `model_vs_baseline`,
clamp fields) surfaced on the relevant pages. All are transcription, not redesign.

---

## 14. E3 Additive Delta — GET /api/candidates (candidate/watchlist queue)

**Author:** `agent-orchestration-engineer`
**Date:** 2026-06-17
**ADR:** none required (additive GET only; no existing endpoint, field, or enum changed).
**Status:** ADDITIVE — the frozen §12 endpoint list is UNCHANGED; this section documents
the new endpoint per §11 change protocol (additive read-only = no downstream tasks affected).

### New endpoint

```
GET /api/candidates → CandidateRecord[]
```

**Description:** Returns the bounded candidate watchlist — tokens evaluated by the snipe loop
(regardless of outcome), ordered newest-first.  Read-only.  No control action.

**Schema** (`aats.control_plane.candidate_schemas.CandidateRecord`):

```jsonc
[{
  "mint": "string",                     // token mint address
  "evaluated_at_slot": 12345,           // event-time slot (C-5 event-time)
  "evaluated_at_wall_ms": 1718500000000, // wall-clock ms at evaluation
  "status": "monitoring|pending|skipped|sniped",
  "model_p": 0.31,                      // calibrated p in [0,1], or null
  "reason": "string",                   // SnipeSkipReason code or "entered"
  "safety_report": {
    "gate_passed": false,               // true if M4 safety gate did not veto
    "reasons": ["safety_gate_fail"]     // structured gate/screener reason codes
  }
}]
```

**Constraints (HARD RULES, all verified in tests):**
- GET only.  No POST variant exists or will exist (read-only).
- No `win_rate` field anywhere (HONESTY CLAUSE, AC-037).
- No money fields (model_p is float probability, not money; candidates not entered have no size).
- No new risk surface: operator can VIEW but not act on this list.
- Backward-compatible: if `candidates_provider` is None, returns `[]`.

**Affected tasks:** none (T-341 server, T-352 dashboard, T-360/361 Telegram are NOT required
to consume this endpoint; it is additive view-only for the operator dashboard candidates page).
The dashboard may optionally add a candidates tab reading this endpoint.

**Schema location:** `aats/control_plane/candidate_schemas.py` (NOT in frozen `aats/contracts/`).
**Queue implementation:** `aats/controller/candidate_store.py` (CandidateQueue, maxlen=200).
**Wiring:** `SnipeLoop(candidate_queue=q)` → `build_app(candidates_provider=q.snapshot)`.

> This endpoint does NOT narrow the frozen contract.  Every existing endpoint, field, enum, and
> wire value in §1–§13 is unchanged.  The §12 frozen endpoint list remains as printed.
