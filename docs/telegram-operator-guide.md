# AATS — Telegram Operator Guide

The Telegram channel is the **out-of-band de-risk + alert** surface for AATS. Like the
dashboard, it can **only de-risk** — there is no command, anywhere in the registry, that can
increase risk. The command set is a closed frozen set; nothing can be added at runtime. `LIVE`
mode is **not reachable from Telegram at all**.

> Two separate bots: a **command bot** (the de-risk commands below) and an **alert bot**
> (Alertmanager push). They use different tokens. A leaked alert-bot token cannot drive commands.

---

## 1. Setup (operator)

In `.env` (real values at deploy; Vault-held where noted):

| Var | What |
|---|---|
| `TELEGRAM_BOT_TOKEN_VAULT_REF` | **Vault reference** to the command-bot token — never the raw token. |
| `TELEGRAM_OPERATOR_USER_IDS` | Your Telegram user-ID(s), comma/space-separated. **Placeholder ⇒ empty ⇒ every command rejected (fail-closed).** |
| `OPERATOR_API_TOKEN` | Vault-ref Bearer the command bot presents to the control plane. |
| `CONTROL_PLANE_URL` | Where the bot routes de-risk commands (e.g. `http://aats-controlplane:8787`). |
| `ALERTMANAGER_TELEGRAM_BOT_TOKEN` / `ALERTMANAGER_TELEGRAM_CHAT_ID` | The **separate** alert bot + chat. |

Until `TELEGRAM_OPERATOR_USER_IDS` is set, the command bot authorizes nobody — that is the
intended safe default.

---

## 2. The command set (exactly four — `aats/telegram/commands.py`)

`_KNOWN_COMMANDS = {status, kill, flatten, pause}`. Anything else gets
`unknown command. available: /status /kill /flatten <mint> /pause`.

| Command | Effect | Confirm | Maps to |
|---|---|---|---|
| **`/status`** | Read agent state + metrics: mode, loops, net-of-cost PnL, daily-loss vs floor, breaker, open positions. **No win-rate.** | no | `GET /api/state` + `/api/metrics` |
| **`/kill`** | Halt all entries + flatten the **entire** open book (≤ 2s). | **YES — per-command confirm** | `POST /api/kill` |
| **`/flatten <mint>`** | Flatten **one** position; others untouched. A mint is required. | **YES — per-command confirm** | `POST /api/flatten/{mint}` |
| **`/pause`** | Step the agent mode **DOWN** to SHADOW (hard-coded downward; can only de-risk). | no | `POST /api/mode {SHADOW}` |

**Not exposed on Telegram** (by design): `breaker/reset` (requires manual-reviewed dashboard
action), `risk-config` tighten, and **anything that advances toward LIVE**. There is no way to
go live, size up, widen a stop, or add leverage from Telegram.

---

## 3. Authorization — two gates, fail-closed

1. **Operator user-ID allowlist** (first gate). An unlisted sender gets **no** control-plane
   call — the update is dropped (logged without its body). Empty allowlist ⇒ everything rejected.
   *A Telegram user-ID is necessary but NOT sufficient* — it is guessable, not a secret.
2. **Per-command confirm** on `/kill` and `/flatten`. The first call replies with a single-use,
   TTL-bound nonce tied to the **same** operator user-ID; the action fires only on a matching
   confirm. The nonce is consumed before firing (no replay) and is bound to the requesting user
   (no cross-operator consumption). So a single leaked `chat_id` cannot fire a de-risk command
   unattended.

This is verified by execution in the security audit (`.agency/05-reports/security/G4-security-audit.md §4`):
unauthorized `/kill` → `authorized=False, fired=False, cp_calls=[]`; authorized `/kill` →
confirm-nonce set, no control-plane call until the matching `/confirm`.

> Even a fully hostile sender who somehow passes the user-ID check **cannot increase risk** —
> every command only de-risks. The confirm-gate and Vault-held token harden the channel against
> *spurious/hostile triggering* of de-risk actions; there is no risk-increasing permission to abuse.

---

## 4. Alerts (the alert bot pushes these)

Routed by Alertmanager from `monitoring/prometheus/rules/aats.yml`. The Telegram alert
*categories* the bot formats (`aats/telegram/alerts.py`): **`FILL`**, **`RUG_AVOIDED`**,
**`BREAKER_TRIP`** (rising-edge, deduped — you get each event once).

| Alert you'll receive | Means | Your move |
|---|---|---|
| ✅ **FILL** | the bot entered a position (`action == sniped`) | informational; watch it on Positions |
| 🛡️ **RUG_AVOIDED** | the gate skipped a launch it flagged as a likely rug | informational — this is the edge working |
| ⛔ **BREAKER_TRIP** | daily-loss circuit breaker tripped; entries halted, book flattening | go to the kill-switch runbook; review before reset |
| **Geyser stale / land-rate low / DMS heartbeat stale** | infra/execution degradation | investigate the provider / why the FAST loop stopped beating |
| **Model-vs-baseline delta negative** | the model stopped beating dumb momentum | the model lost its license to trade until re-proven |

Alerts are **outbound-only** — the alert bot cannot, by construction, change any risk.

---

## 5. Operator playbook

- **"Is it OK?"** → `/status`. Confirm mode, `dry_run` true (paper), breaker not tripped, daily
  PnL above the floor.
- **"Stop everything now."** → `/kill` → confirm. Flat in ≤2s. Then read the kill-switch runbook.
- **"Get me out of one coin."** → `/flatten <mint>` → confirm.
- **"Cool it down."** → `/pause` (steps mode down to SHADOW; no new entries).
- **"Re-arm after a breaker trip."** → **not** on Telegram. Do it on the dashboard Risk page after
  a manual review (`POST /api/breaker/reset`, only when `TRIPPED`).
