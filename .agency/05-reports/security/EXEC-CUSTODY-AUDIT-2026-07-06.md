# EXEC / CUSTODY SECURITY AUDIT — AATS M4 Execution + Signer

- **Auditor:** crypto-security-engineer
- **Date:** 2026-07-06
- **Repo state:** branch `aats-sniper-build`, DRY-RUN paper-only (real capital hard-disabled)
- **Type:** READ-ONLY. No code edited, no git mutated. Scanners run, tests executed.

---

## VERDICT

**PASS-WITH-CONDITIONS.**

Two-part reading, stated plainly:

- **Current DRY-RUN state — PASS (safe to keep running paper).** There is no open
  CRITICAL or HIGH finding *exploitable today*. No wallet secret exists in the system;
  the signer holds no key and performs no signing; every real-money path is fail-closed
  and proven so by test (`send_calls == 0` in DRY_RUN; `LiveSubmitBlocked` when gates
  are not cleared). Money is integer/Decimal end-to-end. Secret hygiene is clean across
  tree and git history.

- **Go-live readiness — FAIL until CONDITIONS met.** The single security control the
  whole custody model rests on — the isolated `aats-signer` that independently enforces
  spend caps + program allowlist + tip-account pin and *cannot be bypassed by the caller*
  — **is an unimplemented scaffold**. Until it exists, ADR-0009's "one compromise drains
  ≤ the float" guarantee is not real. That is a go-live blocker (F1), joined by the
  conditions listed at the end.

Flipping `DRY_RUN_ENABLED=false` before the CONDITIONS below hold would be unsafe.

---

## CHECKLIST (brief's go-live items)

| # | Item | Status | Evidence |
|---|------|--------|----------|
| 1 | No raw private key in env/code/log/Docker layer; signer fetches from Vault only, hot core holds pubkey | **PASS (today) / signer unbuilt** | `.env.example:11-21` (WALLET_PRIVATE_KEY structurally removed by design); `docker-compose.yml:137` hotcore gets `WALLET_PUBKEY` only, `:101-102` signer gets `VAULT_*` token only; `.gitignore:29-31` + `.dockerignore:12-14` exclude keypair files; git history clean (see §Secret sweep). Vault-fetch/mlock path is **designed but not implemented** — `rust/aats-signer/src/main.rs:1-18` |
| 2 | No secret/API key logged (RPC, Jito, etc.) | **PASS** | `signer_client.py:39-40` log level forced WARNING, no bytes; `jito_jupiter_venue.py:588` "NEVER log signed.serialized_b64"; `sell_sim.py:685`; no RPC URL (which embeds `?api-key=`) or token logged in `aats/execution/*` (grep clean) |
| 3 | Signer independently enforces program-ID allowlist + tip pin + per-tx/window velocity + balance cap; cannot be bypassed by caller | **FAIL (go-live)** | `rust/aats-signer/src/main.rs:1-102` is a SCAFFOLD: no key, no signing, no socket listener, none of the 3 refusals. Policy is *designed* only: `config/program-allowlist.json`, `ADR-0009`, `exceptions.py:21-31`. See **F1** |
| 4 | DRY_RUN fail-closed; live requires explicit CEO auth, not silent default | **PASS** | `jito_jupiter_venue.py:1060-1066` absent→enabled; `:204-218` submit_mode; `:1028-1052` three-gate `_assert_live_allowed`; control plane `server.py:1122-1137` LIVE needs operator+CEO+`DRY_RUN_ENABLED=false`. Tests: `test_jito_jupiter_venue.py:108,177-179` (`send_calls==0`), `:221` (`LiveSubmitBlocked`). **Caveat: DEVNET mode bypasses DRY_RUN — F2** |
| 5 | Money is int lamports / Decimal (no float) on every tx value path | **PASS** | Pervasive float-guard `TypeError`: `tx_builder.py:156-161,422-434`; `jito_jupiter_venue.py:642-645,1129-1135,1145-1147,1165-1175`; `multi_wallet.py:160-165,185,214,360-363`; `sell_sim.py:158-167,209-218`; prices are `Decimal`-as-string throughout |
| 6 | Honeypot sell-sim is DRY-RUN by construction (never submits) | **PASS** | `sell_sim.py:357` class attr `submit_mode = SubmitMode.DRY_RUN`; no `send_transaction` code path in the module; `test_sell_sim.py:7` + suite assert `send_calls == 0` on every path (39 tests pass) |
| 7 | Versioned tx, simulate-before-send, no stuck honeypot, partial-fill/failed-land handled | **PASS** | `tx_builder.py:283-295` MessageV0/VersionedTransaction; `jito_jupiter_venue.py:296-319` build→sign→simulate→size-CU→rebuild→**re-simulate landed bytes**; `:885-961` fresh-blockhash retry + re-simulate each attempt; `:342-343` idempotency set only on confirmed land; `multi_wallet.py:486-545` partial-fill reconciler; sell-sim refuse-by-default |
| 8 | Python dependency supply chain | **PARTIAL / MEDIUM** | `requirements/requirements.txt` version-pinned (`==`) but NOT hash-locked; `pyproject.toml:14-52` `>=` ranges; `ci.yml:146` test deps unpinned; no `pip-audit`/`osv-scanner`. See **F3** |

Additional cross-checks: DRY_RUN gates proven (14 targeted tests pass); full execution
suite **176 passed**; anti-cluster per-mint cap raises **before** any `execute()`
(`test_multi_wallet.py:265,294`).

---

## SECRET SWEEP (history-aware)

- `gitleaks` / `trufflehog`: **not installed** in this environment — manual regex sweep
  performed instead (recommend automating in CI — see C9).
- **JSON keypair-array** (`[n,n,n,n,n,n,n,n,...]` 64-byte secret): **none** in tree.
- **base58 privkey / OpenAI `sk-` / Helius `?api-key=<real>` / GH `ghp_` / Slack `xox`**:
  **none** in tree.
- **`-----BEGIN … PRIVATE KEY-----` in full `git log -p --all`**: 4 matches, all are the
  **scanner regex patterns themselves** (`ci.yml`, a detect-secrets rule) — no key material.
- **`_CRYPTO_SEED_PHRASES`** (`tier_a.py:60`): benign NLP keyword seeds ("meme coin pump",
  "rug pull warning"), NOT a BIP-39 mnemonic.
- `.env*` gitignored (`.gitignore:24-26`), only `.env.example` files tracked, placeholders only.
- CI has a secret gate: `detect-secrets` baseline + hardcoded-key grep (`ci.yml:76-105`).

**Result: no key material or live secret in code, logs, Docker layers, or git history.**

---

## FINDINGS

### F1 — Isolated signer is an unimplemented scaffold (HIGH for go-live; N/A today)
`rust/aats-signer/src/main.rs:1-102`, `rust/aats-signer/Cargo.toml:14-31`
The entire caller-independent enforcement boundary mandated by ADR-0009 does not exist.
The binary loads **no** wallet secret, does **no** Vault boot / mlock / zeroize, does **not**
listen on the Unix-domain socket, and implements **none** of the three refusals (per-tx +
rolling-window SOL spend cap, full program-ID allowlist, value-transfer recipient pin).
It serves `/health` and loops.
- *Attack scenario (at go-live):* a compromised/poisoned execution loop builds an
  arbitrary-`sol_in` `EntryIntent` or an off-allowlist instruction; with no independent
  signer to refuse, the raw key signs it and the float is swept in one signature. The
  "≤ float" blast-radius guarantee collapses to "whatever the caller signs."
- *Today:* NOT exploitable — no key is loaded, no signing occurs, and the running controller
  wires `SimulationVenue` (`controller/__main__.py:355`), not the live venue.
- *Remediation:* implement T-251/T-352a before any real key is provisioned. Caps, allowlist,
  and tip-pin MUST live in the signer process, not the caller. Prove by test that the signer
  refuses (a) an over-cap tx and (b) an off-allowlist program-ID tx. Re-audit required.

### F2 — DEVNET submit mode bypasses DRY_RUN and trusts RPC_DEVNET blindly (MEDIUM)
`jito_jupiter_venue.py:214-218` (`submit_mode` returns `DEVNET` whenever `cluster=="devnet"`,
independent of `DRY_RUN_ENABLED` and `live_submit_enabled`); `:677-700` `_assert_devnet_allowed`
only checks for a `confirm_transaction` attr / non-empty `RPC_DEVNET` — it never asserts the
endpoint is actually a devnet cluster; `rpc_client.py:465-472` `DevnetRpcClient` accepts any URL.
- *Attack scenario:* `SOLANA_CLUSTER=devnet` + `RPC_DEVNET=<a mainnet RPC URL>` routes a real
  `send_transaction` to **mainnet** with **no CEO auth and no DRY_RUN check** — a real-money
  path gated only by two env strings.
- *Today:* not reachable (signer scaffold cannot sign; loop uses `SimulationVenue`) — latent.
- *Remediation:* (a) assert the RPC genesis hash is the devnet genesis before any devnet send;
  (b) require an explicit non-default opt-in flag for devnet submit; (c) keep `DRY_RUN` the
  authoritative kill — cluster selection alone must never disable it.

### F3 — Deps version-pinned but not hash-locked; no CI vuln scan (MEDIUM)
`requirements/requirements.txt` (all `==`, no hashes, no lockfile), `pyproject.toml:14-52`
(`>=` ranges), `ci.yml:146` (test deps installed unpinned). No `pip-audit`/`osv-scanner`;
no `cargo audit` for the Rust signer that becomes the key holder.
- *Attack scenario:* a yanked/compromised release of a pinned-but-unhashed dependency, or a
  dependency-confusion package, is pulled at build time. Hot-value targets: `solders`,
  `solana`, `anchorpy`, `httpx`.
- *Remediation:* generate a hash-locked lockfile (`uv`/`pip-compile --generate-hashes`) and
  install with `--require-hashes`; add `pip-audit`/`osv-scanner` + `cargo audit` to CI; pin
  CI tool and test installs by exact version.

### F4 — Key-holding signer container has unrestricted egress (MEDIUM, go-live)
`docker-compose.yml:110-111` + `:431-436` — `aats-signer` sits on a plain bridge network
`aats-internal` (not `internal: true`), with no egress allowlist.
- *Attack scenario:* at go-live the signer holds the raw key in memory; a compromise of the
  signer (or a poisoned Rust dep) exfiltrates the key over open outbound network. The signer
  needs egress to **Vault only**.
- *Remediation (latency-devops owns, crypto-security verifies):* internal network + a narrowly
  scoped egress path (Vault host/port only) via firewall/proxy; drop all other outbound.

### F5 — Control-plane operator token defaults to a known literal (LOW→MEDIUM, go-live)
`aats/control_plane/server.py:76` — `operator_token` defaults to `"dev-token"` if
`OPERATOR_TOKEN` is unset.
- *Attack scenario:* an unset env in a live deploy leaves all destructive de-risk POSTs
  (`/api/kill`, `/api/flatten`, `/api/mode`, `/api/breaker/reset`) accepting a publicly-known
  bearer token. Mitigated by loopback-default bind, the de-risk-only nature of these routes,
  and the separate CEO token gate for LIVE.
- *Remediation:* fail-closed if `OPERATOR_TOKEN` is unset in a non-dev env (refuse to start /
  reject all POSTs) instead of defaulting to `"dev-token"`.

### F6 — tx_builder uses placeholder instruction discriminators (LOW / INFO)
`tx_builder.py:262` (`SWAP_DISCRIMINATOR` placeholder), `:272` (hand-rolled System-transfer
`TIP_DISCRIMINATOR`). Not a secret leak. Positive: program-ID and tip-account loading is
correctly fail-closed (no hardcoded IDs; `VenueError` on any missing entry — `tx_builder.py:215-235,318-397`).
- *Remediation:* replace with IDL-derived encodings + on-chain-shape tests before go-live; the
  signer's tip-account pin (F1) is the compensating control against a mis-encoded tip transfer.

---

## POSITIVE CONTROLS CONFIRMED (defensive design that holds)

- Env schema **structurally removes** any raw-key variable and documents why (`.env.example:11-21`).
- Three-gate LIVE lock (`submit_mode` + `DRY_RUN_ENABLED=false` + `live_submit_enabled`),
  fail-safe default (absent ⇒ DRY_RUN), proven by tests.
- Mandatory simulate-before-send on the **exact landed bytes**; fresh-blockhash retry
  re-simulates each attempt; idempotency set entered only on confirmed land.
- Anti-cluster per-mint cap enforced **before** execution; `N_max=1` gated to R4.
- Sell-sim refuse-by-default honeypot probe, DRY by construction.
- Integer/Decimal money discipline enforced with runtime `TypeError` guards on every value path.
- Signer image: distroless, non-root UID 1000, no shell, no host-published ports.
- CI secret-scan gate present (detect-secrets + hardcoded-key grep).

---

## CONDITIONS FOR GO-LIVE (all must hold before `DRY_RUN_ENABLED=false`)

- **C1.** Implement `aats-signer` (T-251/T-352a): Vault short-lived-token boot, `mlock`,
  zeroize-on-exit, Unix-socket listener with peer-credential + file-permission gating, and the
  **three independent refusals** — per-tx + rolling-window SOL spend caps, full enumerated
  program-ID allowlist, value-transfer recipient pin (`getTipAccounts` + own-ATA-rent). Prove
  by test: signer refuses an over-cap tx **and** an off-allowlist program-ID tx. **Re-audit.** (F1)
- **C2.** Prove the key never crosses the process boundary: `WALLET_PUBKEY` only in hotcore
  env; raw key absent from hotcore `/proc/<pid>/environ` and memory; signer holds it mlock'd
  and zeroizes on exit.
- **C3.** Fix DEVNET/DRY_RUN precedence: assert devnet genesis before any devnet send; keep
  DRY_RUN authoritative; explicit opt-in for any non-DRY_RUN submit. (F2)
- **C4.** Hash-lock Python deps + add `pip-audit`/`osv-scanner` to CI; add `cargo audit` for
  the signer crate. (F3)
- **C5.** Restrict signer container egress to Vault only. (F4)
- **C6.** Require `OPERATOR_TOKEN` + `CEO_TOKEN` to be set (fail-closed if unset in prod);
  rotate before go-live. (F5)
- **C7.** On-chain hygiene: confirm the funding wallet is trade-only (≤ 2 SOL cap, topped up
  out-of-band), has **no standing unlimited SPL approvals/delegations**, and the signer rejects
  unbounded `Approve`/`ApproveChecked` to unknown delegates. Document a revocation runbook.
- **C8.** Replace `tx_builder` placeholder discriminators with IDL-derived encodings +
  on-chain-shape tests. (F6)
- **C9.** Automate `gitleaks` + `trufflehog` over tree **and** full history as a CI-enforced
  gate (manual sweep done here; make it durable).

---

## SCOPE STATEMENT

**Audited:** the M4 execution custody/secrets/submit-gate surface named in the brief —
`aats/execution/{signer_client,jito_jupiter_venue,tx_builder,multi_wallet,rpc_client,sell_sim,exceptions}.py`;
`rust/aats-signer/{src/main.rs,Cargo.toml}`; `.env.example` + `WALLET_*`/`VAULT_*`/`SIGNER_*`
config; `config/program-allowlist.json`; the DRY_RUN gate and every path that could reach a
real sign/submit; the control-plane mode/auth gate (`aats/control_plane/server.py`); dependency
manifests; the signer Dockerfile + compose signer wiring; and a secret sweep of the working
tree **and** git history.

**NOT audited (out of this task's scope):** the M1/M2/M3 modules (ingestion/features/nlp/ml/llm/
orchestration) except where they touch secrets or a submit path; the **LLM prompt-injection
asymmetric-trust clamp** (M2 Reasoner) — flagged for a separate dedicated audit; the live Vault
server, host provisioning, and network firewall (latency-devops); the actual Rust signer
implementation (does not exist yet — audited as scaffold); and trading/sizing/risk-math
*correctness* (backtest-qa + risk-guardrails). This audit certifies the **custody/secrets/submit
mechanism**, not the profitability or model logic.
