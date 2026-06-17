# AATS Colocation and RPC Plan
# T-500 — latency-devops-engineer

**Version:** 1.0 (G5, 2026-06-16)
**Status:** PAPER-operational (DRY_RUN_ENABLED=true default). Live-capital promotion requires
the R3 pre-live checklist (§Pre-Live Checklist) and explicit CEO authorization.
**Companion:** `infrastructure.md §4-5`, `latency-budget.md §2-5`

---

## 1. The honest latency posture (do not elide this)

This system is **DETECTION-COMPETITIVE, SUBMISSION-DISADVANTAGED** (latency-budget.md §1).

Internal compute (Geyser ingress through `sendBundle` enqueue): **~67 ms p50 / ~135 ms p99**.

What that does NOT include, stated plainly:

| Hop | Current (dedicated_geyser) | After colo | Reducible? |
|---|---|---|---|
| Block-engine RTT (node → Jito) | ~55 ms p50 / ~95 ms p99 | ~1–5 ms | Yes — infra spend |
| Leader land (staked QUIC vs contested 20% lane) | ~50 ms p50 / **~450 ms p99** | same p50; **same p99** | Only via SWQoS/staked-QUIC |
| Extra-slot penalty (slot ~400 ms quantum) | +1 full slot under contention | +1 slot (same) | Physics — not reducible |

**What colocation actually buys:** it collapses the ~55 ms block-engine RTT to ~1–5 ms. That is the
single largest compressible win in the ledger. It does NOT close the staked-lane gap. A co-located
unstaked desk still lives in the contested 20% SWQoS lane and slips a full slot under hot-launch
contention — the same as a non-co-located unstaked desk, just with a smaller RTT until it joins
the queue.

**What closes the staked-lane gap:** a staked QUIC connection / SWQoS access (provisioned out-of-band
by partnering with a staked-node operator or renting SWQoS access), not co-location per se.
Co-location without staked QUIC access is still SUBMISSION-DISADVANTAGED; co-location + staked
QUIC moves the desk from the contested 20% lane to the 80% lane and from ~83% N+0 miss rate to
~83% N+0 hit rate — a full slot of expected time-to-land improvement (~400 ms).

The edge this system targets is NOT the raw submission race. Per the EDGE-VERDICT and latency-budget
§6, the winnable niche is **slot+5..+30 safety-selective late entry and migration-survivor selection**,
where clean tip+bundle execution and rug-avoidance matter more than raw shred latency. The colocation
and SWQoS investments are table-stakes (matching the pro-pack baseline), not edge generators.

---

## 2. Default deploy tier: `dedicated_geyser`

**Configured via:** `INFRA_TIER=dedicated_geyser` (`.env`)

The initial production tier uses a premium dedicated RPC + Geyser/Yellowstone gRPC subscription
without physical co-location. This is the `dedicated_geyser` InfraTier (infrastructure.md §4,
OQ-003).

### 2.1 Detection transport

| Source | Latency (p50 / p99) | Role |
|---|---|---|
| Geyser/Yellowstone gRPC (Helius or Triton) | ~60 ms / ~118 ms post-confirmation | Primary event stream |
| ShredStream overlay (optional, INFRA_TIER=colo_shred) | ~50–200 ms pre-confirmation | Parity, not edge — table stakes 2026 |

Geyser provides post-confirmation slot delivery, meaning events arrive after the slot is confirmed
rather than from raw shreds. This is competitive for slot+5..+30 late-entry surfaces but NOT for
block-0. ShredStream provides pre-confirmation shred visibility and is the upgrade path to detection
parity with the best-equipped desks. It is table stakes, not an advantage.

### 2.2 Transaction submission

| Endpoint | Use | Notes |
|---|---|---|
| Jito Block Engine (regional — frankfurt default) | `sendBundle` for all snipe buys | SWQOS lane: contested 20% (unstaked) |
| `RPC_PRIMARY` (Helius / Triton / QuickNode staked) | `getLatestBlockhash`, `getRecentPrioritizationFees`, `simulateTransaction` | Read path only; not on hot path in snipe loop |
| `RPC_SECONDARY` | Failover for read path | May be slower; degrades to safety-selective mode |

### 2.3 Tip caching (off the hot path)

`JITO_TIP_FLOOR_URL` and `JITO_TIP_STREAM_WS` are polled off the hot path into Redis KV cache.
The SNIPE loop reads the cached percentile — no live network call in the decision loop (latency-
budget.md §2, hop 4).

---

## 3. Measured RPC benchmarks (to be filled at R1 — template)

This section is the template for the operator's measured baseline. The numbers below are
architectural targets (from latency-budget.md); REAL measured values from the provisioned
endpoints must replace the placeholders before R3 capital promotion.

### 3.1 RPC read latency

| Endpoint | Method | p50 (ms) | p99 (ms) | Notes |
|---|---|---|---|---|
| RPC_PRIMARY (TBD) | `getLatestBlockhash` | MEASURE | MEASURE | Must be < 50 ms p50 on colo host |
| RPC_PRIMARY | `getRecentPrioritizationFees` | MEASURE | MEASURE | Cached — 1s TTL in Redis |
| Jito REST | `tip_floor` | MEASURE | MEASURE | Cached — 5s TTL in Redis |

**Measurement command (on the colo host):**
```bash
# Run from the co-located host, not from dev machine.
# Replace $RPC_URL with the actual endpoint.
for i in $(seq 1 100); do
  curl -s -w "%{time_total}\n" -o /dev/null -X POST \
    -H "Content-Type: application/json" \
    -d '{"jsonrpc":"2.0","id":1,"method":"getLatestBlockhash","params":[{"commitment":"confirmed"}]}' \
    "$RPC_URL"
done | sort -n | awk '
  BEGIN{n=0}
  {vals[n++]=$1}
  END{
    p50=vals[int(n*0.50)]; p99=vals[int(n*0.99)];
    printf "p50=%.1fms p99=%.1fms\n", p50*1000, p99*1000
  }
'
```

### 3.2 sendTransaction / sendBundle to land time

| Method | Endpoint | p50 slots | p99 slots | land rate | Notes |
|---|---|---|---|---|---|
| `sendBundle` (unstaked) | Jito Frankfurt | MEASURE | MEASURE | MEASURE | Baseline — contested lane |
| `sendBundle` (staked QUIC / SWQoS) | Jito Frankfurt | MEASURE | MEASURE | MEASURE | Upgrade target |

**Target (infrastructure.md §7 / NFR-005):** land rate >= 35% sustained; `sendBundle` p50 <= N+1
slot.

### 3.3 SWQOS uplift (honest quantification)

The SWQoS landing-rate uplift is the difference between the unstaked lane (20% of leader TPU
capacity) and the staked lane (80%). Per Jito MEV docs and the latency-budget:

- Unstaked first-block hit rate: ~17% (fighting for the 20% lane)
- Staked first-block hit rate: ~83% (SWQoS reserved 80% lane)

**Measured uplift (to be filled):** `land_rate(staked) - land_rate(unstaked)` on identical bundles
sent back-to-back from the co-located host.

### 3.4 Slot-delay-vs-winner (the headline honesty metric)

`slot_delay_vs_winner = our_fill_slot - first_fill_slot_for_token`

This is the primary latency panel on the Grafana dashboard (`aats_slot_delay_vs_winner`). It
measures how many slots behind the first buyer we are, on average, across all filled snipes.

- `dedicated_geyser` target: slot_delay_vs_winner p50 <= 3 (N+3 at most)
- `colo_shred + staked QUIC` target: slot_delay_vs_winner p50 <= 1 (N+1, tied with pro pack)
- block-0 (N+0): NOT our target — co-bundlers with the LP-add own block-0

A sustained `slot_delay_vs_winner p50 > 5` is evidence the current infra tier is uncompetitive
at the targeted entry surface and the operator should review the upgrade path (§4).

---

## 4. Upgrade path: `colo_shred` tier

**Trigger:** Operator decision, informed by the measured slot_delay_vs_winner and land-rate panels.
The architect's OQ-003 documents the decision criteria; this section implements the upgrade path.

The InfraTier is pluggable config — swapping from `dedicated_geyser` to `colo_shred` requires:

1. **Physical or bare-metal host in the validator-adjacent region** matching the Jito block engine
   (frankfurt / amsterdam / ashburn / tokyo / slc). Hetzner AX102 (frankfurt) or Latitude.sh
   bare-metal are the preferred options — a 4-vCPU / 32 GB / NVMe host in the AMS/FRA zone achieves
   ~1–5 ms RTT to the Frankfurt block engine vs ~50 ms from a non-colocated VPS elsewhere. The
   RTT delta is measurable; the operator must run `traceroute` to `frankfurt.mainnet.block-engine.jito.wtf`
   from the candidate host to confirm.

2. **ShredStream subscription.** Contact Jito or a ShredStream provider (Helius, Triton, or a
   validator partner) for a ShredStream endpoint and token. Set `SHREDSTREAM_ENDPOINT` and
   `SHREDSTREAM_TOKEN` in `.env`. Set `INFRA_TIER=colo_shred`. The ingestion layer will switch
   to pre-confirmation shred delivery automatically.

3. **Staked QUIC / SWQoS access.** Options:
   a. Partner with a Solana validator that offers staked-connection forwarding. The validator
      proxies `sendBundle` calls through their staked QUIC connection to the leader TPU.
   b. Rent SWQoS access from a staked-node service (emerging market, 2026). Estimate:
      ~0.1–0.5 SOL/month at current network rates.
   c. Stake your own validator node (~70 SOL minimum effective stake for meaningful SWQoS slot
      at current params). Not recommended at R3 scale.

4. **Config change only.** No code change required — the `InfraTier` is a pluggable enum. Update
   `.env`, rebuild, `docker compose up -d`.

5. **Re-run the §3 benchmarks** to confirm the RTT collapsed and land-rate improved.

### 4.1 Region selection

Jito block engines are deployed in: **Frankfurt, Amsterdam, New York, Tokyo, Salt Lake City**.

The operator should select the region where the target validator leader schedule concentrates.
For European time zones and the bulk of European meme-coin activity, **Frankfurt** is the default
(lowest RTT from AMS/FRA bare-metal). The `JITO_BLOCK_ENGINE` env var switches regions without
a code change.

### 4.2 ADR to record if upgrade is triggered

Any upgrade from `dedicated_geyser` to `colo_shred` that involves a material infra spend or
a change in the RPC vendor must be documented as an ADR (`infrastructure.md §10`) naming T-500.

---

## 5. RPC vendor short-list

| Vendor | Use case | Notes |
|---|---|---|
| **Helius** (helius.dev) | Primary RPC + Geyser/Yellowstone | Best-in-class Geyser, flexible plans, staked-RPC option |
| **Triton** (triton.one) | Geyser / Yellowstone alternative | Low-latency, staked, preferred by MEV bots |
| **QuickNode** | Secondary / failover RPC | Global, reliable; lower Geyser latency than public endpoints |
| **Public RPC** (api.mainnet-beta.solana.com) | Fallback only | Rate-limited; NOT suitable for snipe-loop use |
| **Jito Block Engine** (frankfurt default) | `sendBundle` | MUST use regional endpoint closest to colo host |

The architect's RPC short-list (infrastructure.md §4) is the authoritative policy. This section
implements the pluggable config for it.

---

## 6. Pre-Live Checklist (hard blocking before DRY_RUN_ENABLED=false)

This checklist is gated by `PRE_LIVE_CHECKLIST_SIGNED=yes` in the startup self-check script
(`scripts/startup-self-check.sh §1`). Each item must be confirmed, signed off, and recorded.

- [ ] **F-01 (CRITICAL):** `aats-signer` T-352a — three signer-side refusals (SOL spend cap,
  program-ID allowlist, Jito tip-account transfer pin) are BUILT, test-proven, and passing.
  The scaffold placeholder MUST be replaced with the real Rust implementation. This is the
  single most important pre-live item — without it, the signer has no independent enforcement
  of the caps even if the upstream risk engine has a defect.

- [ ] **F-10 (HIGH):** All Dockerfile `@sha256:placeholder` digests are replaced with real,
  verified digests from the target host's `docker pull` + `docker inspect`. Run:
  ```
  docker pull <image> && docker inspect <image> --format '{{index .RepoDigests 0}}'
  ```
  for each of: `rust:1.79.0-slim`, `debian:bookworm-slim`, `python:3.11.9-slim`,
  `node:20.14.0-slim`, `nginx:1.27.0-alpine`, `gcr.io/distroless/cc-debian12:latest`,
  `prom/prometheus:v2.53.0`, `grafana/grafana:11.1.0`, `prom/alertmanager:v0.27.0`.

- [ ] **F-07 (HIGH):** Host hardening — kernel security modules (AppArmor/seccomp profiles),
  firewall rules (block all inbound except 3000/8787/9090/3001/9093), non-root host user,
  swap disabled (for mlock), and ulimit `memlock unlimited` for the signer container.

- [ ] **F-02 (MEDIUM):** Python requirements hash-pinned (`pip-audit` / `pip install --require-hashes`).

- [ ] **F-03 (MEDIUM):** `pip-audit` / OSV CVE gate in CI — fail build on any HIGH/CRITICAL CVE
  in the Python dependency tree.

- [ ] **F-04 (MEDIUM):** GitHub Actions pinned to commit SHAs, not tags (`@v4.1.7` → `@<sha>`).

- [ ] **RPC benchmark completed:** §3.2 land rate and time-to-land measured on the actual host.

- [ ] **slot_delay_vs_winner measured:** §3.4 baseline established from R1 shadow recording.

- [ ] **Alert path proven live:** circuit breaker deliberately tripped + DMS deliberately expired;
  Telegram P1 page confirmed received within 10s (AC-052/053).

- [ ] **Decision log verified:** at least one recorded snipe decision present in the structlog
  append-only log, stamped with event-time and probability vector.

- [ ] **CEO authorization received** for R3 (real capital) — `NEEDS-CEO-DECISION` as per
  infrastructure.md §3 rung R3. The system does not make this decision alone.

- [ ] **Trade-only wallet funded** to the R3 cap (≤ 2 SOL) and confirmed non-custodial.
  Main holdings NEVER on this host.

**Sign-off:** operator sets `PRE_LIVE_CHECKLIST_SIGNED=yes` in `.env` only after ALL items above
are checked. The startup self-check (`scripts/startup-self-check.sh`) refuses to proceed with
`DRY_RUN_ENABLED=false` unless `PRE_LIVE_CHECKLIST_SIGNED=yes` is also set.

---

## 7. Digest verification table (T-500 PAPER evidence)

The following third-party image digests are in `docker-compose.yml`. They were documented from
the official registries as of 2026-06-16. The operator MUST verify these on the target deploy host
before first use (especially before R3). See §6 / F-10.

| Image | Tag | Documented digest | Status |
|---|---|---|---|
| `redis` | `7.2.5-alpine` | `sha256:2219a905c06...` (VERIFIED — in docker-compose.yml) | Verified |
| `prom/prometheus` | `v2.53.0` | `sha256:8a9ed7b7b1f0...` | Operator must verify on target host |
| `grafana/grafana` | `11.1.0` | `sha256:6bb2d21af80e...` | Operator must verify on target host |
| `prom/alertmanager` | `v0.27.0` | `sha256:e13b6ed5cb92...` | Operator must verify on target host |

Build-time images (in Dockerfiles — not in compose directly):

| Image | Tag | Notes |
|---|---|---|
| `rust` | `1.79.0-slim` | `@sha256:placeholder` in Dockerfile.hotcore + Dockerfile.signer — replace before R3 |
| `python` | `3.11.9-slim` | `@sha256:7b09a3b7ccb9...` (in bot/controlplane/dms/telegram Dockerfiles) |
| `node` | `20.14.0-slim` | `@sha256:placeholder` in Dockerfile.dashboard — replace before R3 |
| `nginx` | `1.27.0-alpine` | `@sha256:placeholder` in Dockerfile.dashboard — replace before R3 |
| `gcr.io/distroless/cc-debian12` | `latest` | `@sha256:placeholder` in Dockerfile.signer — pin to a specific digest before R3 |

---

## 8. One-command deploy

```bash
# 1. Copy and configure
cp .env.example .env
# Edit .env: set RPC_PRIMARY, GEYSER_ENDPOINT, GEYSER_TOKEN, TELEGRAM_* etc.
# Leave DRY_RUN_ENABLED=true (the default)

# 2. Self-check
bash scripts/startup-self-check.sh

# 3. Bring up the full stack
docker compose up -d

# 4. Verify
docker compose ps
curl http://localhost:8787/api/health
curl http://localhost:3000/health
# Open http://localhost:3001 (Grafana) — admin / <GRAFANA_ADMIN_PASSWORD>
# Open http://localhost:3000 (Dashboard)
```

All 11 services start in dependency order. Total cold-pull build time: < 10 minutes on a
4-vCPU host with a warm package cache (NFR-010). The dashboard serves mock data immediately;
the control-plane API is live once `aats-controlplane` is healthy.
