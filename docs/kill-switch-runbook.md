# AATS — Kill-Switch Runbook

**When to use this:** you need the bot to stop and flatten *now*, or a safety primitive has
fired and you need to understand what happened and how to recover. This is the 3am page.

**The one fact to keep in mind:** every manual control here only **de-risks**. Nothing in this
runbook can make the bot riskier. You cannot make things worse by killing or flattening.

---

## 1. Stop it now (manual de-risk)

Three independent ways, all flatten the open book in **≤ 2 seconds** (verified —
`tests/e2e/test_t402_operator_demo.py`, 16 passed; kill from both dashboard and Telegram
flattens within budget):

| Surface | Action | Result |
|---|---|---|
| **Dashboard** | Kill button (Settings / Command Deck) → confirm modal | Halt all entries, flatten all positions |
| **Telegram** | `/kill` → confirm | Same, out-of-band |
| **API** | `POST /api/kill` (operator Bearer) → `202` | Same, scriptable |

**Flatten a single position** instead of everything: dashboard Positions (flatten-one) /
Telegram `/flatten <mint>` / `POST /api/flatten/{mint}`.

**Cool down without killing:** `/pause` or `POST /api/mode {SHADOW}` — steps the mode down so no
new entries are taken; open positions continue under normal exit management.

`kill`, `flatten`, and `breaker/reset` are **idempotent** — repeating them is always safe and
never escalates.

---

## 2. What fires automatically (and you cannot disarm)

Three safety primitives operate without you. All are **proven by firing** in the test suite
(`.agency/05-reports/gates/G3-waveS.md`: each has a test that actually trips it). The LLM and
any signal may *trip* or *tighten* these, but **none can reset, disarm, widen, or raise** them.

### 2.1 Daily-loss circuit breaker (`aats/risk/circuit_breaker.py`)

- **Trips** when the day's net loss crosses the floor — absolute `DAILY_LOSS_LIMIT_SOL` (default
  `-0.30 SOL`), tightened-only by the operator at funding time.
- **On trip:** halts all new entries, hands the open book to flatten / survivable-stop enforcement.
- **Latches.** A tripped breaker stays `TRIPPED` and persists. **On process restart it comes back
  `TRIPPED`, never auto-`ARMED`** (`test_restart_stays_tripped`). A new event-time day re-arms the
  *limit*, but never clears a latched trip — only a manual reset does.
- **Reset is manual + reviewed only:** `POST /api/breaker/reset`, which requires the breaker to be
  `TRIPPED` (else `409 breaker_not_tripped`) and operator auth. **No automated path and no LLM can
  reset it.** Not exposed on Telegram.

### 2.2 Survivable stop — three independent layers (`aats/risk/`, ADR-0008)

The stop does **not** depend on the bot being alive. Three separate failure domains:

- **Layer 1 — venue-native resting stop** (`venue_native_stop.py`): an off-box resting order /
  keeper that fires on a price/schedule trigger even if the whole bot is dead.
- **Layer 2 — in-process enforcer** (inside the FAST loop): deterministic hard-stop on a breach tick.
- **Layer 3 — dead-man's switch** (`deadman.py` in-process + `dms_service.py` standalone): see below.

They are tighten-only and never disagree; whichever fires first flattens the position.

### 2.3 Dead-man's switch / DMS (`aats-dms`, separate failure domain)

- **Fires** when the FAST-loop heartbeat is absent for ≥ `T_DMS_SECONDS` (default `60s`).
- **On fire:** submits **pre-signed flatten transactions** for all open positions to the block
  engine — **even if `aats-hotcore` is dead** (it holds pre-signed bytes, produced via the signer,
  so it never holds the raw key and fires even if `aats-signer` is also down).
- **Cannot be disarmed** by an LLM output, a market event, or a risk update — only by a valid
  heartbeat or an explicit operator config update. Latches once fired (`test_dead_mans_switch_*`).

> Alerts: a breaker trip and DMS heartbeat staleness both page you via Telegram/Alertmanager
> (`CircuitBreakerTripped`, `DMSHeartbeatStaleWarning`/`Critical`). Fill and rug-avoided are
> informational.

---

## 3. Recovery — after an automatic fire

### After a circuit-breaker trip

1. **Confirm flat.** Check the dashboard Positions page (open positions should be flattening/flat).
   Cross-check the `CircuitBreakerTripped` alert and the Risk page.
   > Note (carried defect T-402-F1): `GET /api/state` may briefly show `breaker_tripped=false`
   > while the bot is genuinely halted — the in-process gate still blocks entries. Trust the Risk
   > page / the Prometheus alert / `breaker.state` over `/api/state` until the projection fix lands.
2. **Investigate the loss.** Read the Snipe Feed and Positions history. Why did the day cross the
   floor — a cluster of rugs, slippage blowout, a regime change? Do **not** reset on reflex.
3. **Decide.** If the cause is understood and contained, re-arm: dashboard Risk page →
   `POST /api/breaker/reset` (requires `TRIPPED` + auth). If not, leave it tripped and pause.
4. **Tighten if needed.** You can only tighten (`POST /api/risk-config`) — lower the per-trade cap,
   raise the snipe threshold, make the daily floor tighter. Widening is rejected.

### After a DMS / survivable-stop fire

1. **Confirm flat** (positions should be flat — the DMS submitted the pre-signed flattens).
2. **Find out why the heartbeat stopped.** A dead/stalled FAST loop, a host problem, a Redis
   outage. Read `docker compose logs aats-hotcore aats-dms`.
3. **Restart cleanly.** `docker compose up -d`. The breaker/FSM restore to their *safe* state on
   restart (a tripped breaker stays tripped). The DMS re-arms once the heartbeat resumes.

---

## 4. Suspected key/secret compromise (escalation)

If real key material ever appears in code, a log, an image, or git history, treat the key as
**burned** — the fix is to **rotate the wallet, not delete the commit** (custody-policy §8):

1. Operator-confirmed `/kill` + `/flatten` to zero open exposure.
2. Sweep the funding-wallet balance to cold storage out-of-band.
3. Generate a NEW trade-only keypair, store in Vault, update `WALLET_PUBKEY` +
   `WALLET_SECRET_VAULT_PATH`, restart `aats-signer` (fresh Vault token).
4. Revoke the leaked Vault token and any leaked provider/Telegram tokens; rotate them.
5. Run the on-chain approval sweep (custody-policy §6) on the old wallet; keep nothing on it.

---

## 5. Quick card

```
STOP NOW:        dashboard Kill (confirm)  |  Telegram /kill (confirm)  |  POST /api/kill
ONE COIN OUT:    Positions flatten-one     |  Telegram /flatten <mint>  |  POST /api/flatten/{mint}
COOL DOWN:       /pause  |  POST /api/mode {SHADOW}            (no new entries)
RE-ARM BREAKER:  dashboard Risk → reset (TRIPPED + auth only)  — NEVER on Telegram, never automatic
FIRES BY ITSELF: daily-loss breaker (latches) · 3-layer survivable stop · DMS (heartbeat loss ≥ T_DMS)
NEVER:           no manual/LLM/automatic path can disarm, widen, or raise any of the above
```
