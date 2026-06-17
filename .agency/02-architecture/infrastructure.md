# infrastructure.md — Deploy Topology, RPC Strategy, Custody Isolation, DRY-RUN Gate, Dead-Man's Switch (T-206)

**Version:** 1.2.0 — **FROZEN** (E1 delta MINOR-1, ADR-0013: §6 `devnet` env corrected from
"(no submit)" to a REAL `DEVNET` submit on worthless SOL behind `SOLANA_CLUSTER=devnet`, which does NOT
unlock mainnet `LIVE`. Prior: 1.1.0 post-G1-red-team; ADR-0009 split the signer into `aats-signer` §5,
froze signer-side caps + full program allowlist + Jito-tip pinning + Vault/`mlock`/zeroize secret
handling).
**Author:** `solana-systems-architect` (`latency-devops-engineer` implements; `crypto-security-engineer` audits)
**Date:** 2026-06-16
**Status:** Architecture for the deploy. The DRY-RUN flag is a **hard architecture constraint** that
gates the live path. Secrets via `.env.example` only (Vault references; never raw key material).

**Companion:** `BLUEPRINT.md §5.2` (service inventory), `latency-budget.md` (infra tier + the
compressible spend), `execution-venue.md §4` (DRY-RUN as a venue state), `api-contracts.md` (mode
ladder), `data-models.md` (RiskConfig floors).

---

## 1. Docker-compose deploy topology (one `docker compose up`, NFR-010)

A single host (initial tier **`dedicated_geyser`**, OQ-003) brings up the whole system:

```mermaid
flowchart LR
    subgraph HOST["co-located / dedicated Linux host (dedicated_geyser tier)"]
        HC[aats-hotcore — RUST\ningest + SNIPE + FAST\nholds PUBKEY only, builds unsigned tx]
        SIG[["aats-signer — minimal-surface\nNO inbound network, NO decode\nlocal socket: sign(tx,wallet)\nholds the SECRET + signer-side caps"]]
        SL[aats-slow — PY\nSLOW loop + models + MCS + LLM]
        CP[aats-controlplane — PY\nfrozen API + SSE /feed]
        DMS[aats-dms — PY\ndead-man's switch watchdog\nseparate failure domain\npre-signed flattens via aats-signer]
        TG[aats-telegram — PY\nalerts + de-risk commands]
        DASH[dashboard — Vite static\nmock-green; live when wired]
        RD[(redis\nStreams + KV state + feature hot tier)]
        PR[prometheus]
        GR[grafana]
        AM[alertmanager]
    end
    EXTRPC[premium RPC + Geyser/Yellowstone\n(+ ShredStream overlay)]:::ext
    EXTBE[Jito block engine — regional]:::ext
    EXTTIP[Jito tip_floor / tip_stream]:::ext
    VAULT[Vault — short-lived token at boot]:::ext

    EXTRPC --> HC
    HC <--> RD
    HC -. unsigned tx over local Unix socket .-> SIG
    SIG -. signed tx bytes .-> HC
    VAULT -. wallet key via short-lived token .-> SIG
    SL <--> RD
    CP <--> RD
    DMS <--> RD
    DMS -. unsigned flatten .-> SIG
    TG --> CP
    DASH --> CP
    HC --> EXTBE
    DMS --> EXTBE
    EXTTIP -. cached off hot path .-> RD
    HC --> PR
    SL --> PR
    CP --> PR
    SIG --> PR
    PR --> GR
    PR --> AM
    classDef ext fill:#222,stroke:#888;
```

Services (compose units): `aats-hotcore`, **`aats-signer`** (minimal-surface signer, separate failure
domain, §5), `aats-slow`, `aats-controlplane`, `aats-dms`, `aats-telegram`, `dashboard`, `redis`,
`prometheus`, `grafana`, `alertmanager`. Cold-pull build < 10 minutes on a 4-vCPU host (NFR-010). The
dashboard still builds green on mock (NFR-011, AC-049). **The signer holds the secret and the
signer-side caps; the hot core holds only the pubkey** (§5.1, ADR-0009). The DMS's pre-signed flatten
transactions are produced through `aats-signer` (so the DMS path also never holds the raw key).

---

## 2. The DRY-RUN flag as a hard architecture constraint (gates the live path)

`DRY_RUN_ENABLED` is an **environment/config flag**, not a runtime POST. It gates the live path at
the level the AUTONOMY-DIRECTIVE makes non-waivable:

- **Default = `true`.** Real capital is DISABLED by default. The system boots in `SHADOW` mode (FR-004).
- `LIVE` mode is reachable **only** if `DRY_RUN_ENABLED=false` (explicitly set, not absent) **AND** a
  CEO auth token is presented to `POST /api/mode` (AC-060, api-contracts §5). Otherwise 403
  `live_requires_dry_run_disabled_and_ceo_auth`.
- Even with `LIVE` mode, the `JitoJupiterVenue` refuses to submit unless a funded isolated trade-only
  wallet is configured (execution-venue.md §4). Three independent gates: venue `submit_mode`, the
  config flag, the venue refusal.
- **No real-capital transaction is submittable while `DRY_RUN_ENABLED=true`.** This is a CI-asserted
  invariant (a test attempts a live submit with the flag true and asserts zero network sends).

This is the architecture embodiment of "REAL CAPITAL IS DISABLED by default" (BUILD-DIRECTIVE HARD
RULE; AUTONOMY-DIRECTIVE non-waivable #1).

---

## 3. Capital-staging ladder (the live path is gated rung-by-rung)

| Rung | Mode | DRY_RUN_ENABLED | Capital | Advance gate |
|---|---|---|---|---|
| R0 — Sim | SHADOW/PAPER | true | none (synthetic) | mechanism proven (sim) |
| R1 — Shadow/record | SHADOW | true | none (real data, no orders) | ≥ 3,000 recorded launches; leak audit clean; baseline computable |
| R2 — Paper/dry-run | PAPER / LIVE_DRY_RUN | true | none (paper) | GATE-A AND GATE-B pass on purged/embargoed walk-forward; safety fires on demand (T-402) |
| R3 — Tiny-real | LIVE | **false** + CEO auth | real, incinerable (≤ 2 SOL, ≤ 0.1–0.25/coin, ¼-Kelly) | live GATE-A+GATE-B hold over ≥ 100 trades / ≥ 2 windows; haircut within calibrated band |
| R4 — Scale | LIVE | false + CEO auth per step | larger, bounded | fresh passing window at new size; non-residual edge (C-3) |

**C-8/C-12 caveats baked in:** R2/GATE-A is necessary-not-sufficient (no real fills); R3 is a FRESH
proof, not a continuation (the desk's own order has market impact R2 never modeled). A **proof-
staleness bound** auto-re-runs the gate on fresh recorded data if the drift monitor flags a regime
break between an R2 pass and R3 funding (C-12). R3/R4 require explicit CEO authorization (the one
decision the agency does not make alone — `NEEDS-CEO-DECISION`).

---

## 4. RPC / detection strategy (stated honestly)

- **Primary:** premium dedicated RPC + Geyser/Yellowstone gRPC (ingress ~60 ms mean, ~25 ms jitter —
  `dedicated_geyser`, latency-budget.md). InfraTier is **pluggable config** so `colo_shred` is a later
  swap without code change (OQ-003).
- **ShredStream overlay:** optional, configured at deploy; gives pre-confirmation detection (parity,
  not edge — table stakes 2026). When active, events carry `detection_transport: shredstream` and
  `observation_slot < confirmation_slot` (AC-003).
- **Tip / priority pricing:** Jito `tip_floor` (REST) + `tip_stream` (WS) and
  `getRecentPrioritizationFees` are polled **off the hot path** into the Redis KV cache; the SNIPE
  loop reads the cached percentile, never a live network call (latency-budget.md hop 4).
- **Failover:** a secondary RPC endpoint is configured; on primary failure the ingest fails over and
  emits a health degraded signal. **Latency assumption stated honestly:** failover RPC may be slower;
  the system does not pretend failover preserves the snipe budget — it degrades to safety-selective
  late entry only (which is not speed-sensitive) and surfaces the degraded tier on `/api/health`.
- **Geyser freshness:** most-recent-event age < 800 ms normal; > 1,200 ms → health alert + DMS
  heartbeat degraded-mode (FR-057, NFR-004, AC-051).

---

## 5. Keypair custody + signing isolation (audited by `crypto-security-engineer`, T-251)

> **Topology change at G1 red-team (ADR-0009 + delta notice).** The G1-candidate placed the isolated
> signer *inside* `aats-hotcore`. The crypto-security red-team correctly found that this shares the
> private key's address space with the most attacker-exposed code in the system (untrusted on-chain
> bytes + network ingest/egress): a poisoned decode dependency or an RCE on the hot core yields the
> **raw key**, not bounded signing access. The frozen seam is now the inverse — a **separate
> minimal-surface signer process** holds the secret; the hot core holds only the pubkey. ADR-0009
> records the reversal; this section and execution-venue.md §1 / BLUEPRINT §4.1/§5.2 are the frozen
> form.

### 5.1 The signer is its own minimal-surface process (`aats-signer`) — not inside the hot core

- **`aats-signer` is a separate process / container** with **NO inbound network** and **NO
  untrusted-byte decoding**. It exposes ONLY a **local Unix-domain socket (loopback, file-permission
  + peer-credential gated)** carrying one operation: `sign(unsigned_tx_bytes, wallet_id) ->
  signed_tx_bytes`. It does not parse Geyser, does not reach the block engine, does not import the
  decoders. Its attack surface is the single local socket.
- **The hot core (`aats-hotcore`) holds the PUBKEY, never the secret.** It builds the unsigned tx,
  hands the bytes to `aats-signer` over the local socket, gets back signed bytes, and submits them to
  the block engine. A full RCE on `aats-hotcore` yields **signing requests bounded by the signer's own
  policy** (below) — not the raw key. Blast radius of a hot-core compromise is now "what the signer
  will sign," not "the float."

### 5.2 Three INDEPENDENT signer-side refusals (defense-in-depth, enforced INSIDE the signer)

The signer re-validates every tx it is asked to sign, independently of any upstream RiskConfig/cost
gate. A compromised or buggy hot core that constructs a malicious tx is stopped at the signing
boundary, not merely upstream of it.

1. **Per-transaction + rolling-aggregate SOL spend cap (red-team-2 must-fix #2).** The signer parses
   the *net SOL outflow* of the tx it is asked to sign and **REFUSES** if it exceeds
   `per_trade_cap_lamports` (0.1 SOL, OQ-005) or if the rolling-window aggregate would exceed
   `max_aggregate_lamports` (0.5 SOL) / a velocity cap (max N signs per window). These caps live
   **inside the signer**, duplicated from RiskConfig deliberately — so a compromised execution loop
   that builds an `EntryIntent` with an arbitrary `sol_in_lamports` **cannot sweep the float in one (or
   N) signatures**. The `<= float` guarantee no longer rests solely on the wallet being under-funded
   out-of-band; the signer is the second, independent line.
2. **Full enumerated program-ID allowlist (red-team-2 must-fix #3).** The signer parses every
   instruction's program ID and refuses if ANY is outside the **complete** allowed set — not the venue
   registry alone. The allowed set is: the live-verified venue programs (pump.fun, pumpswap,
   raydium_v4, raydium_cpmm, meteora, moonshot — execution-venue.md §3) **PLUS** SPL Token, Associated
   Token Account (ATA), ComputeBudget, and System program. A registry-only allowlist would reject
   every legitimate snipe/exit tx (which all carry these) — so it would be silently widened by an
   implementer, which is the bug. It is enumerated here so it is not.
3. **Value-moving-transfer pinning (red-team-2 must-fix #3).** Allowing the System program (needed for
   the Jito tip transfer and ATA rent) reopens a SOL-exfiltration path unless the *destination* of any
   value-moving `System::transfer` is constrained. The signer pins every System-program SOL transfer
   recipient to a **closed set**: the **8 live-verified Jito tip accounts** (fetched from
   `getTipAccounts` at boot and pinned — confirmed static set, Jito MEV docs) plus the trade-only
   wallet's own ATA-rent destinations. A transfer to any other recipient is REFUSED
   (`signer_unpinned_transfer`). An attacker-built tx cannot route lamports out via an
   "allowed-by-omission" System transfer.

### 5.3 Secret handling is frozen here, not deferred (red-team-2 must-fix #4)

The architecture no longer defers secret-handling to the unwritten `custody-policy.md`, and the
"env-injected" alternative for the wallet secret is **removed** (it would place the raw key in a
process environment readable via `/proc`, core dumps, and any `os.environ` logging — contradicting
"never serialized to a log"). Frozen requirements:

- The wallet secret is **fetched via a short-lived Vault token at boot** (the token, not the key, is
  the boot input), held in **`mlock`-able memory**, and **zeroized on process exit**. It is **NEVER**
  placed in a static process environment variable. The `.env` wallet field is a **Vault path /
  reference only** (never the key material).
- **Trade-only, CAPPED, non-custodial hot wallet** — never main holdings. Max balance is
  operator-configured and audited; the per-trade/aggregate floors (0.1 / 0.5 SOL, OQ-005) cap blast
  radius, and the signer-side caps (§5.2.1) enforce them independently.
- **Multi-wallet** (FR-036): N_max=1 at R3 (OQ-010); the multi-wallet path is built+tested but
  activated only at R4; blast-radius cap C = per_trade_cap. Each wallet's secret follows the same
  Vault-token / mlock / zeroize rule.

### 5.4 Telegram-command authz (red-team-2 must-fix #5)

- Telegram-command authz is a **single operator user-ID allowlist** in `.env` (OQ-004, AC-043) — but
  the user-ID check is **necessary, NOT sufficient**: a Telegram user ID is not an auth secret (it is
  guessable / not confidential). Two additional requirements are frozen: (a) the **bot token's
  secrecy** is a custody secret (Vault reference, audited at G4) — a leaked bot token plus a known
  user-ID must not drive the de-risk channel; (b) `/kill` and `/flatten` require a **per-command
  operator confirmation** (the confirm prompt AC-042 implies) so a single leaked `chat_id` cannot fire
  a de-risk command unattended. (De-risk-only commands can never *increase* risk regardless — this
  hardens the de-risk channel against spurious or hostile triggering, not against over-permission.)

`custody-policy.md` (T-251) is the detailed companion implementing this section; the requirements above
are FROZEN architecture, not deferred to it. `crypto-security-engineer` audits both.

---

## 6. Environments

| Env | `SOLANA_CLUSTER` | Purpose | submit_mode | DRY_RUN | Venue |
|---|---|---|---|---|---|
| `sim` | n/a | mechanism studies, CI | SIMULATION | true | SimulationVenue |
| `devnet` | `devnet` | wiring shakeout, **real** end-to-end land/reconcile on worthless SOL | **DEVNET** | true | JitoJupiterVenue (**REAL submit, devnet cluster only**) |
| `mainnet-shadow` | `mainnet` | R1 record (real data, no orders) | SHADOW | true | none (record only) |
| `mainnet-paper` | `mainnet` | R2 walk-forward / paper | PAPER | true | SimulationVenue (recorded replay) |
| `mainnet-live` | `mainnet` | R3+ (CEO-authorized) | LIVE | false | JitoJupiterVenue |

**`DEVNET` is a REAL submit on worthless devnet SOL — corrected from "(no submit)" by the E1 delta
MINOR-1 (ADR-0013).** Earlier this row ran `LIVE_DRY_RUN` and the venue never transmitted; E1 added a
real devnet SUBMIT path so wiring, landing, and reconcile are exercised end-to-end against a live
cluster. The invariant that makes this safe (and the reason it is a clean additive delta, not a
weakening of the live gate):

- **`DEVNET` is bound to `SOLANA_CLUSTER=devnet`.** The cluster selector pins the RPC/Geyser/block-engine
  endpoints, the airdroppable devnet wallet, and the `DEVNET` submit_mode together. The venue refuses to
  construct in `DEVNET` mode on any other cluster (`devnet_mode_requires_devnet_cluster`).
- **`DEVNET` does NOT unlock mainnet `LIVE`.** It is OUTSIDE the capital-staging ladder (§3), not a rung
  in it. Mainnet `LIVE` is reachable **only** by the unchanged §2 gates: `submit_mode == LIVE`,
  `DRY_RUN_ENABLED=false` (explicit) + CEO auth, and the funded-mainnet-wallet refusal. Note `DRY_RUN`
  stays `true` in the `devnet` env: that flag gates the **mainnet** live-capital path and is independent
  of the devnet cluster's own real submits, which spend only worthless airdrop SOL. Devnet success
  proves wiring/latency, never edge — it has no real fills, impact, or adverse selection.
- **Signer-side caps (§5.2) still apply** to every `DEVNET` tx (devnet tip-account set used when
  `SOLANA_CLUSTER=devnet`; same pinning mechanism). execution-venue.md §1.1 holds the contract form.

---

## 7. Monitoring / alerting (NFR-007)

- **Prometheus** scrapes: snipe-decision latency (histogram), FAST-tick time (histogram), model
  inference time (histogram), land rate, net-PnL/day, **model-vs-baseline delta**, circuit-breaker
  state, **dead-man's switch heartbeat age**, Geyser feed age. The promoted `sniper_sim/metrics.py`
  scorecard becomes these gauges (BLUEPRINT §3; T-250).
- **Grafana** surfaces GATE-A and GATE-B live during paper/live rungs (AC-038). **No win-rate panel
  anywhere** (HONESTY CLAUSE).
- **Alertmanager** routes: breaker trip → Telegram ≤ 10 s (AC-053); fill → Telegram ≤ 10 s (AC-052);
  Geyser staleness > 1,200 ms; FAST-tick budget breach; land-rate < 35% sustained (NFR-005).

---

## 8. Dead-man's switch wiring (the survivable-stop Layer 3, FR-033 / AC-045/046)

- `aats-dms` is a **separate process / separate failure domain** (its own compose unit). It holds
  **pre-signed flatten transactions** for all open positions (refreshed as positions open/close) and
  monitors the FAST-loop heartbeat in Redis. The pre-signed flattens are produced through `aats-signer`
  (the DMS submits already-signed bytes to the block engine; it never holds the raw key — §5.1).
  Because the flattens are pre-signed and held, the DMS fires even if `aats-signer` is also down.
- On heartbeat absence ≥ **T_DMS = 60 s** (env var, OQ-006; configurable, not a constant), it submits
  the pre-signed flattens for all open positions to the block engine — **even if `aats-hotcore` is
  dead** (NFR-006 crash gap; AC-045).
- **It cannot be disarmed** by an LLM output, a market event, or a risk update — only by a valid
  heartbeat or an explicit operator config-update (AC-046). The breaker (FR-034) and both other stop
  layers remain active independently; the DMS is the backstop that holds when the process itself is
  gone.

---

## 9. Secrets policy (NFR-008, ROSTER §5.6)

`.env.example` only — real values are CEO-provided at deploy time. Never in code, logs, images, or
any tracked file. The example enumerates: RPC/Geyser URLs + keys, ShredStream creds, Jito endpoints,
the trade-only wallet secret **reference (Vault path/token, NEVER the key material — the env-injected
raw-key alternative is removed per §5.3)**, the Telegram **bot-token Vault reference** + operator user
ID, `DRY_RUN_ENABLED` (default true), **`SOLANA_CLUSTER`** (`mainnet` | `devnet`, default `mainnet`;
selects the cluster and is the hard gate for `SubmitMode.DEVNET` — ADR-0013), `T_DMS_SECONDS`
(default 60), the InfraTier selector, and the risk-config floors. The wallet key itself is fetched by `aats-signer` via a short-lived Vault token at
boot, held in `mlock`-able memory, zeroized on exit (§5.3) — it never enters any process environment.
`crypto-security-engineer` audits at G4 that no secret leaked (T-403).

---

## 10. Post-G1 changes
Any change to the deploy topology, the DRY-RUN gating, the **signer-process split (§5, ADR-0009)**, the
**`SubmitMode.DEVNET` / `SOLANA_CLUSTER` gating (§6, ADR-0013)**, or the staging ladder is an ADR +
delta notice naming T-250 (scaffold), T-500 (deploy), T-251 (custody),
**T-352a (`aats-signer` service)**, T-327 (venue `sign()` now crosses a process boundary), and any
dependent task. The signer split is recorded in ADR-0009 and the G1 delta notice (BLUEPRINT §14).

**E1 delta MINOR-1 (ADR-0013) — `SubmitMode.DEVNET`.** The `devnet` env row in §6 was corrected from
"JitoJupiterVenue (no submit)" / `LIVE_DRY_RUN` to a REAL `DEVNET` submit on worthless devnet SOL behind
`SOLANA_CLUSTER=devnet`, which does NOT unlock mainnet `LIVE`. A clean additive delta — no contract is
narrowed, the §2 mainnet gates are untouched. Affected tasks: **T-327** (venue `submit_mode` enum +
`DEVNET` `land()` transmit-on-devnet path + cluster refusal), **T-352a** (`aats-signer` devnet tip-account
set selected by `SOLANA_CLUSTER`), **T-500** (deploy: `SOLANA_CLUSTER` env wiring, devnet endpoints,
airdrop wallet), **T-250** (`.env.example` adds `SOLANA_CLUSTER`), **T-340/341** (control plane if
`/api/mode` or `/api/health` surfaces the cluster/submit_mode), **T-251** (custody: devnet airdrop
wallet provisioning). No code change ships under this delta itself — it is the contract update those
tasks build to. Recorded in ADR-0013.
