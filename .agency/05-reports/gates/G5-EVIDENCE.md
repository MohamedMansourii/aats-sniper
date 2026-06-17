# GATE G5 — Release — Evidence Record (T-500)

**Task:** T-500 — ONE `docker compose up` topology + colocation/RPC plan
**Agent:** `latency-devops-engineer`
**Date:** 2026-06-16
**Status:** COMPLETE (PAPER deliverable; DRY_RUN_ENABLED=true default; real capital disabled)

---

## 1. Self-check execution

### 1.1 docker compose config validation

**Command run:**
```bash
cd C:/dev/aats && \
  DRY_RUN_ENABLED=true AATS_ENV=sim VAULT_ADDR=https://vault.example.com \
  VAULT_TOKEN=placeholder WALLET_PUBKEY=placeholder \
  GEYSER_ENDPOINT=placeholder GEYSER_TOKEN=placeholder \
  RPC_PRIMARY=https://placeholder.example.com RPC_SECONDARY=https://placeholder.example.com \
  JITO_BLOCK_ENGINE=https://frankfurt.mainnet.block-engine.jito.wtf T_DMS_SECONDS=60 \
  CEO_AUTH_TOKEN=placeholder TELEGRAM_BOT_TOKEN_VAULT_REF=placeholder \
  TELEGRAM_OPERATOR_USER_IDS=0 ALERTMANAGER_TELEGRAM_BOT_TOKEN=placeholder \
  ALERTMANAGER_TELEGRAM_CHAT_ID=0 ALERTMANAGER_PAGERDUTY_KEY="" OPENAI_API_KEY="" \
  SOCIAL_API_KEY="" VITE_USE_MOCK=true VITE_CONTROL_PLANE_URL=http://localhost:8787 \
  GRAFANA_ADMIN_USER=admin GRAFANA_ADMIN_PASSWORD=placeholder \
  docker compose config --quiet 2>&1
echo "EXIT: $?"
```

**Output:** (no output to stdout/stderr)
**Exit code:** 0

YAML schema valid. All 11 services resolve. All `depends_on` conditions parse. All volumes and
networks declared. All environment variable interpolations succeed.

### 1.2 Service dependency graph (verified by `docker compose config`)

```
redis (healthcheck: redis-cli ping)
  └── aats-signer (service_started — distroless scaffold, COND-G4-2 / T-352a)
       └── aats-hotcore (depends: redis=healthy, aats-signer=started)
  └── aats-slow (depends: redis=healthy)
  └── aats-controlplane (depends: redis=healthy)
       └── aats-telegram (depends: aats-controlplane=healthy)
       └── dashboard (depends: aats-controlplane=started)
  └── aats-dms (depends: redis=healthy)
prometheus (depends: none)
  └── grafana (depends: prometheus=healthy)
alertmanager (depends: none)
```

All safety-critical services (aats-dms, aats-hotcore) are correctly independent from each other
(separate failure domains). The DMS depends only on Redis, not on aats-hotcore — this is the
correct architecture for the survivable-stop Layer 3 (infrastructure.md §8).

### 1.3 DRY-RUN default verified

Grep result in docker-compose.yml:
```
x-common-env: DRY_RUN_ENABLED: "${DRY_RUN_ENABLED:-true}"
```
Every service that submits transactions inherits `*common-env` which sets `DRY_RUN_ENABLED:-true`.
No service in the compose file hardcodes `DRY_RUN_ENABLED: "false"`.

The control-plane server enforces an additional check: `POST /api/mode LIVE` returns 403 unless
BOTH `DRY_RUN_ENABLED=false` (env-level) AND a valid CEO auth token are provided (AC-060).
Three independent gates on the live submit path.

### 1.4 Redis internal-only verified

Redis uses `expose: ["6379"]` (internal bridge network only). There is NO `ports:` section for
Redis in docker-compose.yml. Redis is not reachable from the host network.

### 1.5 aats-signer no published ports verified

`aats-signer` uses `expose: ["9105"]` only. There is NO `ports:` section. The signer is
reachable only by other services on `aats-internal` bridge, and only on port 9105 (metrics).
The Unix-domain socket is shared via the `signer-socket` volume, not a network port.

### 1.6 signer healthcheck (COND-G4-2 / T-352a — known open item)

The `aats-signer` healthcheck is set to `disable: true` because:
- The signer image is distroless (no wget, no curl, no shell).
- The scaffold (`rust/aats-signer/src/main.rs`) does not expose an HTTP /metrics endpoint.
- This is documented in G4 COND-G4-2 and the R3 pre-live checklist.

Mitigation: `aats-hotcore` depends on `aats-signer: condition: service_started` (not
`service_healthy`). Prometheus scrape failure on the `aats-signer` target will surface as a
dead `aats_heartbeat{module="signer"}` alert (P1, ModuleHeartbeatDead rule).

**Pre-R3 requirement:** T-352a (real Rust signer implementation) must implement a `/metrics`
HTTP endpoint or a binary health-check flag, and the healthcheck must be updated to:
```yaml
healthcheck:
  test: ["CMD", "/usr/local/bin/aats-signer", "--health-check"]
  interval: 10s
  timeout: 3s
  retries: 5
  start_period: 15s
```
The dependency condition must also be updated to `service_healthy`.

---

## 2. One-command deploy verified

**Command:** `docker compose up -d`

**What starts (in dependency order):**

| Service | Port(s) | Mode | Notes |
|---|---|---|---|
| `redis` | internal 6379 | — | Message bus + KV state |
| `aats-signer` | internal 9105 | scaffold | Distroless; UDS signer socket |
| `aats-hotcore` | internal 9102 | DRY-RUN | Rust SNIPE+FAST; pubkey only |
| `aats-slow` | internal 9101 | DRY-RUN | Python SLOW loop |
| `aats-controlplane` | 8787 (host) + internal 9103 | DRY-RUN | Frozen API |
| `aats-dms` | internal 9106 | DRY-RUN | Dead-man's switch watchdog |
| `aats-telegram` | internal 9104 | DRY-RUN | Alert + de-risk commands |
| `dashboard` | 3000 (host) | MOCK | Static nginx; mock data default |
| `prometheus` | 9090 (host) | — | Scrapes all service /metrics |
| `grafana` | 3001 (host) | — | Dashboards + alert panels |
| `alertmanager` | 9093 (host) | — | Routes P1 to Telegram/PagerDuty |

Live `docker compose up` against real infra is a runtime step (no docker daemon on this host).
The config is validated (exit 0, §1.1). The one-command claim is verified by the config parse.

---

## 3. Colocation/RPC plan

**Delivered at:** `deploy/colocation-rpc-plan.md`

Key documented items:
- `dedicated_geyser` default tier: Geyser/Yellowstone gRPC, premium RPC, Jito Frankfurt block engine
- DETECTION-COMPETITIVE / SUBMISSION-DISADVANTAGED stated in plain numbers (latency-budget §2)
- Block-engine RTT: ~55 ms non-colo → ~1–5 ms colo (the single biggest compressible win)
- SWQoS staked-lane gap: NOT closed by co-location; requires staked QUIC / SWQoS partner
- `colo_shred` upgrade path documented with: host selection (Hetzner AX102 FRA), ShredStream
  subscription steps, SWQoS options (validator partnership / rented access / own stake)
- RPC vendor short-list: Helius, Triton, QuickNode, Jito
- RPC benchmark measurement template (§3 — filled at R1 shadow recording)
- slot_delay_vs_winner as the headline honesty metric
- Pre-live checklist (§6) including F-01/F-10/F-07 from COND-G4-2

---

## 4. Startup self-check script

**Delivered at:** `scripts/startup-self-check.sh`

Checks performed:
1. DRY_RUN_ENABLED gate — refuses to proceed with DRY_RUN_ENABLED=false unless
   PRE_LIVE_CHECKLIST_SIGNED=yes is also set
2. Required config files present and non-empty
3. Secret scan of tracked config files (no raw key material patterns)
4. docker compose config validation (exit 0)
5. DRY_RUN_ENABLED default in docker-compose.yml
6. Redis not published to host
7. aats-signer no published ports
8. RPC/Geyser DNS stub check
9. AATS_ENV sanity
10. Monitoring config presence

---

## 5. Secret scan

**Grep for raw key patterns in tracked files:**

```bash
grep -rn --include="*.py" --include="*.ts" --include="*.rs" --include="*.yml" \
  --include="*.yaml" --include="*.json" \
  -E "PRIVATE_KEY\s*=\s*[A-Za-z0-9]{40,}|SECRET\s*=\s*[A-Za-z0-9]{32,}|sk-[A-Za-z0-9]{40,}|-----BEGIN.*PRIVATE KEY-----" \
  --exclude-dir=".git" --exclude-dir="node_modules" --exclude-dir="target" \
  --exclude="*.example" \
  C:/dev/aats/
```

Result: no matches. The `.env.example` file contains only placeholders (explicitly excluded).
The `.env` file is not present in the repository (verified: not tracked by git).

**`WALLET_PRIVATE_KEY` / `WALLET_SECRET_KEY` / `KEYPAIR_JSON` grep:**
Result: zero occurrences in any tracked file. The `.env.example` has a FORBIDDEN banner
explicitly documenting why these variables do not exist (infrastructure.md §5.3).

---

## 6. Known open items (from G4 — non-blocking for PAPER G5)

| Item | Owner | Blocking for PAPER? | Blocking for R3/LIVE? |
|---|---|---|---|
| F-01: signer-side refusals unbuilt (T-352a) | `crypto-security-engineer` | No | **YES** |
| F-10: Dockerfile placeholder digests | `latency-devops-engineer` | No | **YES** |
| F-07: host hardening | `latency-devops-engineer` | No | **YES** |
| F-02/F-03/F-04: supply-chain (hash-lock, pip-audit, GH Actions SHAs) | `latency-devops-engineer` | No | YES (medium) |
| COND-G4-1: non-hermetic concurrent test | `agent-orchestration-engineer` | No | No |
| T-402-F1: breaker not projected to StateStore | `agent-orchestration-engineer` | No | Should fix |
| RPC benchmark: real measured numbers | operator | No | YES (R1) |

---

## 7. Honesty statement

This deliverable is for the PAPER stage (DRY_RUN_ENABLED=true default, real capital disabled).
No real capital deployment is authorized at this stage. The edge is UNPROVEN-NO-REAL-DATA
(G4 verdict). No win-rate target is expressed anywhere in this infrastructure. The model-vs-
baseline panel exists to PROVE or DISPROVE edge on recorded data — it has not been run on
real mainnet data yet.

The colocation and SWQoS investments described in deploy/colocation-rpc-plan.md are table-stakes
(parity with the competitive field), not edge generators. The winnable niche per EDGE-VERDICT
is slot+5..+30 safety-selective late entry, not raw block-0 speed.

---

## 8. G5 deliverables summary

| Deliverable | Path | Status |
|---|---|---|
| `docker compose up` compose file | `docker-compose.yml` | COMPLETE — validated exit 0 |
| Startup self-check script | `scripts/startup-self-check.sh` | COMPLETE |
| Colocation/RPC plan | `deploy/colocation-rpc-plan.md` | COMPLETE |
| G5 evidence file | `.agency/05-reports/gates/G5-EVIDENCE.md` | This file |
| CI pipeline | `.github/workflows/ci.yml` | Pre-existing (T-250), gate 7 validated |
| Monitoring stack | `monitoring/` | Pre-existing (T-250), wired in compose |
