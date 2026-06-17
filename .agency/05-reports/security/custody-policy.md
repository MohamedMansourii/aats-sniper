# custody-policy.md — AATS Wallet Custody, Signer Isolation, Spend Caps, Secrets (T-251)

**Owner:** `crypto-security-engineer` · **Date:** 2026-06-16 · **Status:** AUTHORITATIVE — the
engineers build against this. **Authority:** ADR-0009, `infrastructure.md §5`, `execution-venue.md §1`,
`api-contracts.md §5/§7/§10`. **Companion files:** `config/program-allowlist.json` (the enforced
allowlist + tip pin), `.env.example` (the secret schema), `<G4>-audit.md` (the PASS/FAIL verdict).

> **Blast-radius thesis (the one sentence everything serves):** when — not if — `aats-hotcore` is
> fully compromised (RCE, poisoned decode dependency, leaked `.env`), the attacker gets *signing
> requests bounded by the signer's policy*, never the raw key and never more than the per-trade float.
> The funding-wallet cap + signer isolation + signer-side spend caps compose so the answer to "how much
> can one signature drain?" is **≤ the float**, never "everything."

---

## 1. The trade-only funding wallet (the single highest-leverage control)

- **A dedicated Solana keypair holding ONLY working capital.** Hard cap `WALLET_MAX_BALANCE_LAMPORTS`
  ≤ **2 SOL** at R3 (`infrastructure.md §5.3`; brief: "incinerable"). **Never the operator's main
  holdings**, never a wallet that has ever touched cold storage directly.
- **Topped up out-of-band** from cold storage in small tranches. Cold storage NEVER signs through this
  system and its key NEVER enters this host. The bound on total loss is the float on this wallet, not
  net worth.
- **Multi-wallet** (FR-036): `N_max = 1` at R3; the multi-wallet path is built+tested but activated only
  at R4. Each wallet's secret follows the identical Vault → mlock → zeroize rule (§4). Blast-radius cap
  per wallet `C = per_trade_cap`.
- **No standing token delegations** on the funding wallet (on-chain hygiene, §6).

---

## 2. The isolated signer process (`aats-signer`) — separate failure domain

Per **ADR-0009**, signing is **its own minimal-surface process / container** (Rust crate
`rust/aats-signer`), NOT inside `aats-hotcore`.

### 2.1 Surface

- **NO inbound network.** The only ingress is a **local Unix-domain socket** (`SIGNER_SOCKET_PATH`,
  loopback, file-permission + peer-credential gated). It does not listen on TCP, does not reach the
  block engine, does not import the Geyser/pool decoders. Metrics are exported on a loopback-only
  `METRICS_PORT` heartbeat gauge (no command surface).
- **NO untrusted-byte decoding** beyond the tx bytes it is asked to sign. It parses the transaction it
  signs only to *enforce policy on it* (program IDs, lamport flows) — it never decodes attacker-chosen
  on-chain account data. This keeps the key out of the address space that handles the two highest-risk
  surfaces (untrusted on-chain bytes + the network).
- **The one operation:** `sign(unsigned_tx_bytes, wallet_id) -> signed_tx_bytes`. On any policy
  violation it returns `SignerRefused{reason}` and signs nothing. A refusal aborts the snipe/exit; it
  never leaks the key.

### 2.2 Who holds what

| Process | Holds | Never holds |
|---|---|---|
| `aats-hotcore` (Rust) | the **PUBKEY**; builds the *unsigned* tx; submits *signed* bytes | the secret |
| `aats-signer` (Rust) | the **SECRET** (mlock'd); enforces all signer-side policy | the network, the decoders |
| `aats-dms` (Py) | **pre-signed** flatten bytes (produced via the signer) | the secret |
| `aats-slow`, control-plane, Telegram | nothing key-related | the secret, the pubkey-signing path |

The execution venue's `sign()` (`execution-venue.md §1`) **crosses the process boundary**: it
serializes the `UnsignedTx`, calls `aats-signer` over the socket, and gets signed bytes back. Budgeted
≤ 1.5 ms p99 added (`latency-budget.md` hop 5) — well inside the ≤150 ms SNIPE budget.

### 2.3 Container hardening (built by `latency-devops-engineer`, T-352a; audited here)

The signer image is the crown jewel. Required posture (audit findings F-07..F-09 track gaps):
- distroless, **non-root** (UID 1000, already set), no shell, no package manager — DONE.
- **`cap_drop: [ALL]`** then **`cap_add: [IPC_LOCK]`** — IPC_LOCK is REQUIRED for `mlock` to succeed;
  without it the secret can be swapped to disk (F-07, HIGH).
- **`security_opt: ["no-new-privileges:true"]`**, **`read_only: true`** root filesystem, tmpfs for
  `/run` — (F-08).
- **No host-published ports** (DONE) and membership on an **isolated network** reachable only by
  `aats-hotcore` + `aats-dms`, or socket-only with the metrics endpoint scraped over a dedicated
  scrape network (F-09).
- Base images pinned to **real `@sha256` digests** before any live build (currently
  `@sha256:placeholder` — must be resolved; F-10).
- The distroless healthcheck must not assume `wget`/shell (distroless has none) — use a binary-native
  liveness or a Prometheus-side up-check (F-11, MEDIUM).

---

## 3. Spend caps INSIDE the signer (defense-in-depth) + program allowlist + transfer pin

The signer re-validates **every** tx independently of any upstream RiskConfig/cost gate. A compromised
or buggy `aats-hotcore` that builds a malicious `EntryIntent` with an arbitrary `sol_in_lamports` is
stopped **at the signing boundary**, not merely upstream. Three independent refusals
(`infrastructure.md §5.2`):

### 3.1 Refusal #1 — SOL spend cap (per-tx + rolling + velocity)

The signer parses the **net SOL outflow** of the tx and REFUSES if:
- net outflow > `PER_TRADE_CAP_LAMPORTS` (**0.1 SOL**, `signer_per_tx_cap_exceeded`), OR
- the rolling-window aggregate would exceed `MAX_AGGREGATE_LAMPORTS` (**0.5 SOL**,
  `signer_aggregate_cap_exceeded`), OR
- sign-count in the window exceeds `SIGNER_MAX_SIGNS_PER_WINDOW` (velocity, `signer_velocity_exceeded`).

All money here is **integer lamports** (`u64`), never float (`data-models.md §0`). The rolling window
is wall-clock `SIGNER_WINDOW_SECONDS`. These caps are **duplicated from RiskConfig deliberately** —
the `≤ float` guarantee no longer rests solely on the wallet being under-funded out-of-band; the signer
is the second, independent line. A compromised loop **cannot sweep the float in one (or N) signatures.**

### 3.2 Refusal #2 — full enumerated program-ID allowlist

The signer parses every instruction's `program_id` and refuses (`signer_program_not_allowlisted`) if
ANY is outside the live (verified) set loaded from `config/program-allowlist.json`:
**System, ComputeBudget, SPL Token (+Token-2022 only if emitted), ATA** (the core programs every legit
snipe/exit carries) **PLUS** the live-verified venue programs (**pump.fun, PumpSwap, Raydium v4, Raydium
CPMM**) and the **Jupiter v6** router (exits only). `meteora`/`moonshot` are *candidates* — present for
discovery, NOT admitted to signing until promoted by deploy. **Default-deny / fail-closed.** Program IDs
are loaded from the file (data), never literals in a hot-path decoder (build-time guard,
`execution-venue.md §3.2`). Each entry is **VERIFY-AT-BUILD**: the boot/CI probe confirms
`executable=true` and drops any unverifiable ID from the live set (an unverified ID is refused — a stale
ID is the single most dangerous sniper bug, A-001).

### 3.3 Refusal #3 — value-moving-transfer pinning (the 8 live Jito tip accounts)

Admitting the System program (needed for the Jito tip + ATA rent) reopens a SOL-exfiltration path unless
the *destination* of every value-moving `System::transfer` is constrained. The signer pins every System
SOL-transfer recipient to a **closed set** (`signer_unpinned_transfer` otherwise):
- the **8 Jito tip accounts**, fetched from **`getTipAccounts` at boot** and pinned (the boot fetch is
  authoritative; `config/program-allowlist.json` holds a VERIFY-AT-BUILD reference copy used only to
  diff/alert and to **fail-closed** — refuse value transfers — if the live fetch is unreachable), PLUS
- the wallet's **own ATA-rent destinations**, validated by recomputing the ATA address from the held
  pubkey + the tx's mint (not trusting an address the caller supplied).

An attacker-built tx therefore cannot route lamports out via an "allowed-by-omission" System transfer.

---

## 4. Secret handling (FROZEN — `infrastructure.md §5.3`)

- The wallet secret is **fetched via a short-lived Vault token at boot** (the *token*, not the key, is
  the boot input — prefer Vault AppRole/Kubernetes-auth so even the token is not a static secret), held
  in **`mlock`-able memory** (requires `IPC_LOCK`, §2.3), and **zeroized on process exit** (Rust
  `zeroize`, already a dep). It is **NEVER** placed in a static process environment variable, never
  logged, never serialized to disk in plaintext.
- The `.env` wallet field is a **Vault path / reference only** (`WALLET_SECRET_VAULT_PATH`); never key
  material.
- **The env-injected raw-key path is FORBIDDEN.** There is, by design, **no** `WALLET_PRIVATE_KEY` /
  `WALLET_SECRET_KEY` / `KEYPAIR_JSON` variable in the schema — an env-var key would land in
  `/proc/<pid>/environ`, core dumps, and any `os.environ`/`std::env` dump. `.env.example` carries an
  explicit FORBIDDEN banner; CI (T-403) greps for any such var and HARD-FAILS.
- **Zero real secrets** in code, images, logs, or chat. `.env.example` (placeholders) is the only
  committed secret artifact. The `.dockerignore` excludes `.env*` (except `.env.example`) from image
  layers; no Dockerfile COPYs `.env` or bakes a key (verified, §audit).

---

## 5. The DRY-RUN hard gate (real capital disabled by default)

Custody composes with the DRY-RUN constraint (`infrastructure.md §2`, `execution-venue.md §4`):
- `DRY_RUN_ENABLED` defaults **true**; real capital is DISABLED by default. Boot mode is `SHADOW`.
- `LIVE` is reachable ONLY if `DRY_RUN_ENABLED=false` (explicitly set) **AND** a CEO auth token is
  presented to `POST /api/mode` (else `403 live_requires_dry_run_disabled_and_ceo_auth`).
- Three independent gates: venue `submit_mode`, the config flag, the `JitoJupiterVenue` refusal (it
  won't run without a funded isolated wallet). No code path reaches the block engine while
  `DRY_RUN_ENABLED=true` (CI-asserted invariant).
- **Custody corollary:** even past all three gates, the signer's spend cap (§3.1) still bounds a `LIVE`
  signature to ≤ 0.1 SOL per tx / 0.5 SOL rolling. DRY-RUN protects *whether* you submit; the signer
  caps *how much* a submitted signature can move.

---

## 6. On-chain hygiene — token approvals / delegations + revocation runbook

- **No standing unlimited `Approve`/delegate** on the funding wallet. The bot must use **exact-amount**
  approvals (or no approval — direct AMM CPIs that don't require a standing delegate), and **revoke**
  immediately after use. A standing unlimited delegation is a CRITICAL drain path independent of the
  signer (the delegate can move tokens without a fresh signature) — the signer SHOULD additionally
  refuse an SPL `Approve`/`ApproveChecked` carrying an unbounded amount to an unknown delegate.
- **Revocation runbook (operator):**
  1. Enumerate delegations: for each token account of `WALLET_PUBKEY`, read `delegate` /
     `delegated_amount` (RPC `getTokenAccountsByOwner`, or a wallet tool).
  2. For any non-null delegate, submit SPL Token `Revoke` for that token account (a `Revoke` tx is
     itself value-neutral and passes the signer: no outflow, allowlisted program, no unpinned transfer).
  3. Verify `delegated_amount == 0` post-revoke.
  4. If a delegation cannot be explained, treat as compromise → de-risk (kill/flatten) and rotate the
     wallet (§8).
- The bot's exit path should prefer **direct AMM / Jupiter swaps that do not leave a standing
  delegation**; any approval is exact-amount and revoked in the same logical operation.

---

## 7. Telegram-command authorization policy

Bound to the frozen control-plane contract (`api-contracts.md §7/§10`). De-risk-only; nothing here can
increase risk.

- **Single operator user-ID allowlist** (`TELEGRAM_OPERATOR_USER_IDS`, OQ-004, AC-043). An unlisted
  sender gets **no API call** (the bot drops the update silently). The user-ID check is **necessary, NOT
  sufficient** — a Telegram user ID is guessable, not an auth secret.
- **The bot token is a custody secret** (`TELEGRAM_BOT_TOKEN_VAULT_REF`, Vault reference, audited at
  G4). A leaked bot token + a known user-ID must not drive the channel — which is why the token is
  Vault-held and the de-risk commands are confirm-gated (below). The **alert** bot
  (`ALERTMANAGER_TELEGRAM_BOT_TOKEN`) is a **separate** bot from the command bot.
- **Command set (de-risk only):**

  | Command | Endpoint | Direction | Confirm |
  |---|---|---|---|
  | `/status` | GET `/api/state` + `/api/metrics` | read | no |
  | `/kill` | POST `/api/kill` (halt entries + flatten ≤2s) | de-risk | **YES — per-command confirm** (AC-042) |
  | `/flatten <mint>` | POST `/api/flatten/{mint}` | de-risk | **YES — per-command confirm** |
  | `/pause` | POST `/api/mode {PAPER\|SHADOW}` (down only) | de-risk | no (already only de-risks) |

- **Per-command operator confirmation** on `/kill` and `/flatten`: the bot replies with an
  inline-confirm prompt carrying a short-lived nonce; only the confirm from the **same operator user-ID**
  fires the API call. A single leaked `chat_id` cannot fire a de-risk command unattended.
- **NOT exposed on Telegram:** `breaker/reset` (requires TRIPPED + auth, human-reviewed),
  `risk-config` tighten, and **anything that advances mode toward `LIVE`** (LIVE is unreachable from
  Telegram entirely). `/flatten` (all) is operator-gated; per-mint is the default surface.
- **Idempotent de-risk:** repeating any command is always safe and never escalates (contract §1.5).

> Note on asymmetric trust: every Telegram command can only *de-risk*, so even a fully hostile sender
> who passes the user-ID check cannot increase risk. The confirm-gate and Vault-held token harden the
> channel against *spurious/hostile triggering* of de-risk actions, not against over-permission (there
> is no over-permission to grant — the contract has no risk-increasing command).

---

## 8. Key-compromise / rotation runbook (secrets are radioactive)

If real key material ever appears in code, a log, an image, or git history, it is **CRITICAL and the key
is considered burned** — remediation is **rotate the wallet, not delete the commit**:
1. Operator-confirmed `/kill` + `/flatten` (de-risk to zero open exposure).
2. Sweep remaining funding-wallet balance to cold storage out-of-band.
3. Generate a NEW trade-only keypair; store in Vault; update `WALLET_PUBKEY` +
   `WALLET_SECRET_VAULT_PATH`; restart `aats-signer` (fresh Vault token).
4. Revoke the leaked Vault token and any leaked provider/Telegram tokens; rotate those too.
5. Run the on-chain approval sweep (§6) on the old wallet; assume it is drainable and keep nothing on it.

---

## 9. Engineer build checklist (what each lane must implement against this)

- **`aats-signer` (T-251 / T-352a, Rust):** the socket listener + peer-cred gate; the three refusals
  (§3) with integer-lamport math; Vault-token boot + `mlock` + `zeroize`; load
  `config/program-allowlist.json`; boot `getTipAccounts` fetch + pin (fail-closed on fetch failure).
- **`solana-execution-engineer` (T-327):** venue `sign()` → loopback socket to `aats-signer`; handle
  `SignerRefused` (abort snipe/exit, never retry past a cap); never hold the key in the hot core.
- **`latency-devops-engineer` (T-250 / T-352a / T-500):** signer container hardening (§2.3) — `cap_drop
  ALL` + `cap_add IPC_LOCK`, `no-new-privileges`, `read_only`, isolated network, real image digests;
  Vault wiring; add `pip-audit` + `gitleaks` to CI (§audit).
- **`backend-engineer` (T-361):** Telegram authz exactly per §7 — user-ID allowlist + per-command
  confirm + Vault-held bot token.
- **`risk-guardrails-engineer`:** RiskConfig floors mirror the signer caps (defense-in-depth is
  intentional duplication, not a single source); exact-amount approvals + revoke (§6).
```
```
> This policy is FROZEN architecture for custody. Any change to the signer split, the caps, the
> allowlist enforcement, the tip pin, or the secret-handling rule is an ADR + delta notice (ADR-0009
> protocol). `crypto-security-engineer` re-audits at G4 (T-403) and issues PASS/FAIL.
