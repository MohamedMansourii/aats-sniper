# AATS — Deploy & Operations Guide

**Audience:** the operator deploying and running AATS, and the engineer who inherits it.
**Posture:** this guide deploys the **PAPER / DRY-RUN** system. Real capital stays disabled
until `docs/pre-live-checklist.md` clears in full. Every command below was run against this
build; outputs are real.

---

## 1. Deploy topology

One `docker compose up` brings up the whole system on a single host. The topology is frozen
in `.agency/02-architecture/infrastructure.md`.

```
            ┌──────────────────────── single co-located Linux host ───────────────────────────┐
 premium    │  aats-hotcore (Rust)        aats-signer (Rust)            aats-slow (Python)      │
 RPC/Geyser │   ingest + SNIPE + FAST ─▶  sign() over LOCAL Unix       SLOW: models, MCS,      │
 ShredStream│   holds PUBKEY only          socket; holds SECRET;         de-risk LLM            │
 ──────────▶│        │  ▲ signed bytes      NO inbound network ◀── Vault token (short-lived)    │
            │        ▼  │                                                                        │
  Jito BE ◀─│      redis (Streams + KV state)  ◀──── all services ────▶                         │
            │        │                                                                           │
            │  aats-controlplane (frozen API :8787)      aats-dms (pre-signed flattens,         │
            │        │                                    SEPARATE failure domain)              │
            │  dashboard :3000 / aats-telegram ── de-risk only ──┘                              │
            │        │                                                                           │
            │  prometheus :9090 → grafana :3001 / alertmanager :9093                            │
            └───────────────────────────────────────────────────────────────────────────────────┘
```

**Compose services** (`docker-compose.yml`): `aats-hotcore`, `aats-signer`, `aats-slow`,
`aats-controlplane`, `aats-dms`, `aats-telegram`, `dashboard`, `redis`, `prometheus`,
`grafana`, `alertmanager`.

**Published ports** (everything else is internal-only): `3000` dashboard, `8787` control plane,
`9090` Prometheus, `3001` Grafana, `9093` Alertmanager. Redis (`6379`) and the per-service
`/metrics` ports (`9101–9106`) are **not** host-exposed. `aats-signer` publishes **no** ports —
its only ingress is a local Unix-domain socket shared with the hot core via a Docker volume.

### Custody isolation (why the signer is its own service)

The wallet **secret never lives in the hot core**. The hot core builds an *unsigned* transaction
and asks `aats-signer` to sign it over the local socket; `aats-signer` holds the secret (fetched
from Vault via a short-lived token at boot, held in `mlock`-able memory, zeroized on exit) and
re-validates every transaction independently before signing. A full compromise of the hot core
yields *signing requests bounded by signer policy*, never the raw key (ADR-0009). The DMS submits
**pre-signed** flatten bytes, so it never holds the key either.

> The three signer-side refusals (per-tx + rolling SOL spend cap, full program-ID allowlist,
> Jito-tip transfer pinning) are **specified and the data is present** (`config/program-allowlist.json`)
> but the Rust signer is currently a **scaffold** — these refusals are **not yet running code**.
> They are latent because LIVE is unreachable. **They MUST be built and test-proven before
> `DRY_RUN_ENABLED=false`** (pre-live checklist item COND-G4-2 / F-01).

---

## 2. Deploy steps (paper / DRY-RUN)

Prerequisites: Docker (tested 29.x) + Compose v2. Verified on this build:

```
$ docker compose config   # validates the topology
EXIT: 0
$ docker --version
Docker version 29.2.0
```

```bash
# 1. Get the code and the env schema
git clone <repo-url> aats && cd aats
cp .env.example .env

# 2. (paper) leave the defaults. DRY_RUN_ENABLED=true, AATS_MODE=SHADOW, AATS_ENV=sim,
#    VITE_USE_MOCK=true — all safe. No real secret is needed for a paper run.

# 3. Bring it up
docker compose up            # cold build < ~10 min on a 4-vCPU host (NFR-010)

# 4. Confirm health
#    dashboard   http://localhost:3000   (green on mock immediately)
#    control API http://localhost:8787/api/health
#    Grafana     http://localhost:3001   (admin / GRAFANA_ADMIN_PASSWORD)
#    Prometheus  http://localhost:9090
```

The system boots in `SHADOW` mode with `dry_run_enabled=true`. Confirm on the dashboard
Command Deck or via the API:

```bash
curl -s http://localhost:8787/api/state | grep -o '"mode":"[A-Z_]*"'   # -> "mode":"SHADOW"
```

---

## 3. Environment configuration reference

`.env.example` is the **only** place the schema is documented and the **only** committed secret
artifact. Real values are operator-provided at deploy; placeholders are safe for paper.
**No raw private key appears here, in code, in images, or in logs — by design.**

### Capital-staging / mode (the hard DRY-RUN constraint)

| Var | Purpose | Example / default |
|---|---|---|
| `DRY_RUN_ENABLED` | **Master safety flag.** `true` = real capital disabled. Default true. Setting `false` is gated on the pre-live checklist + CEO auth. | `true` |
| `AATS_MODE` | Startup mode: `SHADOW` \| `PAPER` \| `LIVE_DRY_RUN` \| `LIVE`. | `SHADOW` |
| `AATS_ENV` | Environment label: `sim` \| `devnet` \| `mainnet-shadow` \| `mainnet-paper` \| `mainnet-live`. | `sim` |

### Secret custody (Vault — the only source of key material)

| Var | Purpose |
|---|---|
| `VAULT_ADDR` | Vault server URL. |
| `VAULT_TOKEN` | **Short-lived token** (a capability, not the key) the signer exchanges for the wallet secret at boot. Prefer AppRole/K8s-auth. |
| `WALLET_PUBKEY` | The trade wallet **pubkey only** (hot core builds unsigned tx). |
| `WALLET_SECRET_VAULT_PATH` | Vault path where the secret lives. A path, never the key. |
| `WALLET_MAX_BALANCE_LAMPORTS` | Operator hard cap on the trade wallet (≤ 2 SOL at R3). Bounds blast radius. |

### Signer policy

| Var | Purpose |
|---|---|
| `SIGNER_SOCKET_PATH` | Loopback Unix socket the hot core / DMS call `sign()` on. |
| `SIGNER_ALLOWLIST_PATH` | Path to the program-ID allowlist + tip-account pin the signer enforces. |
| `SIGNER_MAX_SIGNS_PER_WINDOW` / `SIGNER_WINDOW_SECONDS` | Velocity cap (defense-in-depth). |

> Reconcile `SIGNER_SOCKET_PATH`: `.env.example` documents `/run/aats/signer.sock`; the compose
> file mounts the shared socket volume at `/run/aats-signer/signer.sock`. Confirm both point at
> the same mounted path before any live wiring (carried devops note).

### RPC / detection

| Var | Purpose |
|---|---|
| `RPC_PRIMARY` / `RPC_SECONDARY` | Premium dedicated RPC + failover. The api-key in the URL is a placeholder. |
| `GEYSER_ENDPOINT` / `GEYSER_TOKEN` | Yellowstone/Geyser gRPC (baseline detection). |
| `SHREDSTREAM_ENDPOINT` / `SHREDSTREAM_TOKEN` | Optional pre-confirmation overlay (parity, not edge; empty disables). |
| `INFRA_TIER` | `dedicated_geyser` (default) or `colo_shred`. Pluggable config, no code change. |

### Jito / execution

| Var | Purpose |
|---|---|
| `JITO_BLOCK_ENGINE` / `JITO_TIP_FLOOR_URL` / `JITO_TIP_STREAM_WS` | Block engine + tip pricing. Tip is read **live**; never hardcode for LIVE. |
| `JUPITER_API_URL` | Jupiter v6 base (FAST-path exits only; the snipe BUY is direct-AMM). |
| `JITO_DEFAULT_TIP_LAMPORTS` / `DEFAULT_CU_PRICE_MICROLAMPORTS` | Offline/DRY-RUN fallbacks only. |
| `LAND_MAX_ATTEMPTS` | Blockhash-expiry retry budget. |

### Risk floors (the signer enforces these independently)

| Var | Purpose | Default |
|---|---|---|
| `PER_TRADE_CAP_LAMPORTS` | Per-tx signer cap. | `100000000` (0.1 SOL) |
| `MAX_AGGREGATE_LAMPORTS` | Rolling-window signer cap. | `500000000` (0.5 SOL) |
| `DAILY_LOSS_LIMIT_SOL` | Absolute daily-loss circuit-breaker floor. | `-0.30` |
| `T_DMS_SECONDS` | Dead-man's-switch heartbeat-loss timeout. | `60` |

### Multi-wallet / sell-sim (built, gated off)

| Var | Purpose |
|---|---|
| `N_WALLETS_MAX_ENABLED` / `N_WALLETS_MAX` | Multi-wallet is **built + tested but NOT activated** until R4. Must be `true`/`>1` explicitly; at R3 N=1. |
| `MULTI_WALLET_MINT_CAP_LAMPORTS` | Anti-cluster blast-radius cap to a single mint. |
| `SELL_SIM_*` | Honeypot probe thresholds (tighten-only). |

### Operator surfaces

| Var | Purpose |
|---|---|
| `CEO_AUTH_TOKEN` | Required for `POST /api/mode LIVE`. Placeholder; set at R3 only. |
| `TELEGRAM_BOT_TOKEN_VAULT_REF` | **Vault reference** to the command-bot token, never the token. |
| `TELEGRAM_OPERATOR_USER_IDS` | Operator allowlist. **Placeholder ⇒ empty ⇒ no command authorized (fail-closed).** |
| `OPERATOR_API_TOKEN` / `CONTROL_PLANE_URL` | Bearer token (Vault ref) + control-plane URL the command bot posts to. |
| `ALERTMANAGER_TELEGRAM_*` / `ALERTMANAGER_PAGERDUTY_KEY` | Alert channel (separate bot from the command bot). |
| `GRAFANA_ADMIN_USER` / `GRAFANA_ADMIN_PASSWORD` | Grafana login (set the password at deploy). |
| `VITE_USE_MOCK` / `VITE_CONTROL_PLANE_URL` | Dashboard: `true` = standalone mock; `false` = live wire. |

### Social / LLM (SLOW loop only; adversarial input)

`X_API_BEARER_TOKEN`, `REDDIT_*`, `TELEGRAM_MTProto_*`, `SOCIAL_API_KEY`, `BIRDEYE_API_KEY`,
`OPENAI_API_KEY` / `AATS_FRONTIER_API_KEY` / `AATS_OLLAMA_URL` — all Vault refs / runtime-injected
placeholders. The reasoner defaults to a deterministic **offline mock** backend (zero network);
live LLM backends are opt-in. These never touch the SNIPE hot path.

---

## 4. Environments

| `AATS_ENV` | Purpose | Mode | `DRY_RUN_ENABLED` | Venue |
|---|---|---|---|---|
| `sim` | mechanism studies, CI | PAPER | true | SimulationVenue |
| `devnet` | wiring shakeout, dry-run latency | LIVE_DRY_RUN | true | JitoJupiterVenue (no submit) |
| `mainnet-shadow` | **R1 record** (real data, no orders) | SHADOW | true | none (record only) |
| `mainnet-paper` | **R2 walk-forward / paper** | PAPER | true | SimulationVenue (recorded replay) |
| `mainnet-live` | **R3+** (CEO-authorized) | LIVE | **false** | JitoJupiterVenue |

---

## 5. The DRY-RUN gate

`DRY_RUN_ENABLED` is a **config flag, not a runtime POST**. It is the architecture-level switch
that disables real capital. Three independent gates must all open for a real submit:

1. **Venue `submit_mode`** — defaults to `DRY_RUN`. Verified:
   ```
   $ python -c "from aats.execution.jito_jupiter_venue import JitoJupiterVenue; \
       print(JitoJupiterVenue(wallet_pubkey='1111...').submit_mode)"
   DRY_RUN
   ```
2. **`DRY_RUN_ENABLED=false`** — must be *explicitly* set false (absent ≠ false).
3. **CEO auth + funded wallet** — `POST /api/mode LIVE` requires `DRY_RUN_ENABLED=false` **and**
   the CEO auth token, else `403 live_requires_dry_run_disabled_and_ceo_auth`; and the venue
   refuses to submit without a funded isolated trade-only wallet.

A CI-asserted invariant proves no network send happens while `DRY_RUN_ENABLED=true`. Verified
this build: `pytest tests/execution` → **171 passed, 2 skipped**; security audit confirmed
"no real submit path is reachable" (`.agency/05-reports/security/G4-security-audit.md §5`).

---

## 6. The staged-rollout ladder (a gate at every rung)

Real capital advances **rung by rung**, never by extrapolation. Each rung's gate must clear on
the data type stated before the next is reachable. The full ladder is in `EDGE-VERDICT.md §6`
and `infrastructure.md §3`.

| Rung | What runs | `DRY_RUN` | Capital | Gate to advance |
|---|---|---|---|---|
| **R0 — Sim** | `sniper_sim` mechanism studies | true | none (synthetic) | mechanism demonstrated (gate avoids catchable rugs, staged exit > naive, tips edge-bounded). **Synthetic — proves direction only, never licenses capital.** |
| **R1 — Shadow / record** | live ingestion in `SHADOW`, record point-in-time first-K-slot snapshots, **submit nothing** | true | none (real data, no orders) | **≥ ~3,000 recorded launches** with point-in-time features + event-time labels; leak audit clean; baseline + model both computable. |
| **R2 — Paper / dry-run** | full triple loop vs `SimulationVenue` driven by **recorded** launches; venue in `LIVE_DRY_RUN` (build+sign, no submit) | true | none (paper) | **GATE-A AND GATE-B both pass** on purged/embargoed walk-forward windows, lower-95% bound > 0; circuit breaker + survivable stop + DMS all fire on demand. |
| **R3 — Tiny-real** | live submit, capped throwaway wallet (≤ 2 SOL total, ≤ 0.1–0.25 SOL/coin, ¼-Kelly) | **false** | real, incinerable. **CEO explicit auth + legal confirmation (OQ-009) required.** | after ≥ 100 real trades across ≥ 2 windows: live GATE-A AND GATE-B hold, lower-95% bound > 0; realized adverse-selection haircut within calibrated band; no breaker-trip pathology. |
| **R4 — Scale** | stepwise size increase | false | larger, bounded | each step requires a **fresh passing walk-forward window at the new size** (slippage/adverse-selection scale with size — re-prove, never extrapolate). Any failed gate → revert to prior size. |

> **R2 → R3 is a regime change, not a continuation.** Recorded fills contain no market impact
> from the desk's own order; a passing R2 result is *necessary, not sufficient*. R3 is a **fresh**
> proof, and a proof-staleness bound auto-re-runs the gate on fresh data if the drift monitor flags
> a break before funding (C-8/C-12).

**No path reaches R3 without a passing walk-forward result on recorded data + the full
pre-live checklist (`docs/pre-live-checklist.md`).** R3 and R4 are the one decision the agency
does not make alone — they return to the CEO as an explicit authorization.

---

## 7. Monitoring & health checks

- **Prometheus** (`:9090`) scrapes per-service `/metrics`: snipe-decision latency, FAST-tick
  time, model inference time, land rate, **net-PnL/day**, **model-vs-baseline delta**,
  circuit-breaker state, **DMS heartbeat age**, Geyser feed age.
- **Grafana** (`:3001`) surfaces GATE-A and GATE-B live during paper/live rungs. **There is no
  win-rate panel anywhere** (honesty clause) — `monitoring/grafana/dashboards/aats-overview.json`.
- **Alertmanager** (`:9093`) routes the alerts defined in `monitoring/prometheus/rules/aats.yml`:

  | Alert | Fires when |
  |---|---|
  | `CircuitBreakerTripped` | `aats_circuit_breaker_tripped == 1` |
  | `DMSHeartbeatStaleWarning` / `...Critical` | DMS heartbeat age > 30s / > 45s |
  | `ModuleHeartbeatDead` | a service heartbeat == 0 |
  | `GeyserFeedStale` | data staleness > 1.2s |
  | `LandRateLow` | land rate < 0.35 sustained |
  | `FastTickBudgetBreachHigh` | FAST-tick budget breaches spike |
  | `ModelVsBaselineDeltaNegative` | the GATE-B delta turns negative (model lost its license) |
  | `PositionDriftDetected` | OMS/on-chain reconciliation mismatch |

- **Health endpoints:** control plane `GET /api/health` (per-module status + staleness); each
  service exposes a container healthcheck (see `docker-compose.yml`).

---

## 8. Routine operations

- **Start / stop:** `docker compose up -d` / `docker compose down`. State (Redis, Prometheus,
  Grafana) persists in named volumes; `down -v` wipes it.
- **Logs:** `docker compose logs -f aats-controlplane` (or any service). The Telegram bot token
  is redacted at a single chokepoint; no secret is ever logged.
- **Change risk config (tighten-only):** `POST /api/risk-config` with a tighter config, or the
  dashboard Risk page. Widening is rejected `403 risk_increase_rejected` — widening requires an
  explicit config change + redeploy, never the API.
- **De-risk now:** see `docs/kill-switch-runbook.md`. Kill / flatten from the dashboard or
  Telegram; both flatten the open book in ≤ 2s.
- **Mode down:** `POST /api/mode {PAPER|SHADOW}` or `/pause` on Telegram — always permitted.
- **Backup/restore:** the system is stateless trading-wise; recorded data and metrics are the
  assets. Back up the Prometheus volume and any recorded-launch corpus (R1+). Redis state is
  rebuildable from the loops on restart; the breaker and FSM restore to their *safe* state
  (a tripped breaker restores `TRIPPED`, never `ARMED`).

---

## 9. Common failure modes

| Symptom | Likely cause | Response |
|---|---|---|
| Dashboard blank / no data | `VITE_USE_MOCK=false` but control plane down/unreachable | Set `VITE_USE_MOCK=true` for standalone, or fix `VITE_CONTROL_PLANE_URL` + bring up `aats-controlplane`. |
| `GeyserFeedStale` alert | RPC/Geyser degraded | Failover to `RPC_SECONDARY` is automatic; the system degrades to safety-selective late entry (not speed-sensitive) and flags the tier on `/api/health`. Investigate the provider. |
| `CircuitBreakerTripped` | daily-loss floor hit | Entries halt, book flattens. **Manual review required**, then `POST /api/breaker/reset` (only when `TRIPPED`). See the runbook. |
| `DMSHeartbeatStale*` | FAST loop stalled/dead | The DMS will flatten on heartbeat loss past `T_DMS`. Investigate why the loop stopped beating. |
| Telegram commands ignored | empty/incorrect `TELEGRAM_OPERATOR_USER_IDS`, or token offline | Fail-closed is intended. Set the operator allowlist + Vault-held bot token. |
| `403 risk_increase_rejected` | a POST tried to widen a limit | Working as designed — operator surfaces are de-risk-only. Widen via config + redeploy. |

---

## 10. Self-check evidence (run this build)

```
docker compose config                              -> EXIT 0
python -c "from aats.control_plane.server import build_app"   -> OK
JitoJupiterVenue(...).submit_mode                  -> DRY_RUN
pytest tests/e2e/test_t402_operator_demo.py        -> 16 passed
pytest tests/risk                                  -> 315 passed
pytest tests/validation                            -> 22 passed
pytest tests/execution                             -> 171 passed, 2 skipped
VITE_USE_MOCK=true npm run build (dashboard)       -> built OK
```
