# G3 — Wave F (Foundation) — per-task build-gate verdicts

_Gate: G3 (Build, per task) · Approver: `orchestrator` · Date: 2026-06-16_
_Rule: G3 is DUAL — `code-reviewer` **AND** `backtest-qa-engineer` must both PASS (CLAUDE.md §8.3, TASKBOARD §0)._
_Method: orchestrator opened and inspected the actual changed files under `C:/dev/aats` (not the handoff text alone)._

Wave F = the foundation the P3 build lanes stand on: the Docker/Compose scaffold + telemetry (T-250),
the shared typed contracts (T-199), and the custody/secrets policy + signer allowlist (T-251).

---

## Verdict summary

| Task | Title | code-reviewer | backtest-qa | G3 | Attempts |
|---|---|---|---|---|---|
| T-250 | Repo/Docker/Compose scaffold + CI + telemetry | **PASS** (×2) | n/a (devops scaffold; QA = code-reviewer dual) | **PASS** | 2 |
| T-251 | Custody policy + signer allowlist + `.env.example` | **PASS** | n/a (security policy; reviewed by code-reviewer) | **PASS** | 1 |
| T-199 | Shared typed contracts (events/features/venue/api) | **FAIL** (1 BLOCKER + 1 MAJOR) | PASS | **FAIL → NEEDS-REPLAN** | 2 |

Foundation coherence: contracts package imports cleanly and is shared-usable; compose topology
complete; no secret committed; honesty clause clean across dashboard; `sniper_sim.demo` + dashboard
mock build intact per re-run evidence. **The one blocker is isolated to T-199** and does not
contaminate T-250/T-251.

---

## T-250 — Scaffold + CI + telemetry — **PASS** (attempt 2)

**Both prior G3 blockers fixed and independently re-verified by two code-reviewers; no regressions.**

Evidence inspected by orchestrator:
- **Honesty clause:** `dashboard/src/pages/Positions.tsx:310` — "Realized PnL (closed)" subtitle is now
  `` `${closedCount} closed · net SOL` `` (a literal, not a derived win-rate metric). `Grep win[Rr]ate|winRatePct|win_rate`
  across `dashboard/src` returns **zero matches**. New guard `test_no_win_rate_in_dashboard_typescript()`
  added to `tests/test_dry_run_invariant.py`, proven meaningful by reviewer (catches a re-introduced symbol,
  ignores honesty-aligned prose).
- **CI Gate 1 runnable:** `.secrets.baseline` committed at repo root; `.github/workflows/ci.yml:67-75`
  carries a generate-if-absent guard so CI never exits 2 on a missing baseline. `.gitignore` keeps the
  baseline trackable (line 49 comment + no ignore entry).
- **Compose topology (`docker-compose.yml`):** all 11 services present — `aats-hotcore`, `aats-signer`,
  `aats-slow`, `aats-controlplane`, `aats-dms`, `aats-telegram`, `dashboard`, `redis`, `prometheus`,
  `grafana`, `alertmanager`. `DRY_RUN_ENABLED=${...:-true}` on every tx service via `x-common-env`;
  `aats-signer` publishes NO ports (expose 9105 metrics only); `signer-socket` volume mounted `:ro` into
  hot-core; Redis internal-only (expose 6379, no host port). Conforms to `infrastructure.md §2/§5.1` and ADR-0009.
- **Demo + telemetry:** `python -m sniper_sim.demo` runs (scenario 3 NET PnL +621.37 SOL, above the CI floor 580);
  full pytest 15 passed / 0 failed; telemetry promoted to an empty integer-lamports registry (no baked-in magnitudes).

Non-blocking, correctly deferred (NOT gating scaffold): `prom/grafana/alertmanager @sha256:placeholder`
pins (T-500), real RPC benchmark (T-500), live alert-path test (T-402/T-500), `--fail-on-unaudited`
guarded by `|| true` (pre-existing). dashboard-lint `continue-on-error:true` acceptable at scaffold (tracked to T-350).

**Verdict: PASS. T-250 → DONE.**

---

## T-251 — Custody policy + signer allowlist + `.env.example` — **PASS** (attempt 1)

**`code-reviewer` PASS by execution.** Security-policy deliverable; the dual-gate QA counterpart is the
code-reviewer's secret-scan + conformance review (no backtest surface here).

Evidence inspected by orchestrator:
- **SECRET-FREE:** `.env.example` is placeholder-only with the FORBIDDEN raw-key banner (lines 11-20); there is
  **no** `WALLET_PRIVATE_KEY`/`WALLET_SECRET_KEY`/`KEYPAIR_JSON` var by design (ADR-0009 §5.3). Wallet secret
  is Vault-only via `aats-signer`; hot core holds `WALLET_PUBKEY` only. detect-secrets/manual sweeps/git-history
  sweep all clean across AATS prod paths (the only flags are legacy/mock/placeholder false-positives).
- **Allowlist (`config/program-allowlist.json`):** valid JSON; fail-closed default-deny; every entry
  `verify_at_build:true`; core + 4 active venues (pump.fun, PumpSwap, Raydium v4, Raydium CPMM) + Jupiter v6;
  meteora/moonshot/token-2022 marked `candidate` and EXCLUDED from the live signing set (least privilege);
  8 Jito tip accounts as a fail-closed reference pin with `getTipAccounts`-at-boot authoritative; own-ATA-rent
  recipient derivation pinned. No venue ID appears as a hot-path literal.
- **`custody-policy.md`:** isolated `aats-signer` design (no inbound network, holds the secret; 3 independent
  signer-side refusals — per-tx 0.1 SOL + rolling 0.5 SOL + velocity cap, full program allowlist, tip-account
  pinning), Vault short-lived token + mlock + zeroize, Telegram authz (single operator, de-risk-only, per-command
  confirm on /kill and /flatten).

Correctly scoped OUT of this policy deliverable (handed to latency-devops; gated at G4 T-403, NOT here):
dependency vulns (starlette/python-multipart/protobuf/lightgbm), hash-locked lockfile + solana/websockets
resolver conflict, CI supply-chain (SHA-pin actions, `--require-hashes`, wire pip-audit/gitleaks), signer
container hardening (`IPC_LOCK`, `no-new-privileges`, `read_only`, real `@sha256` pins). Live execution of the
3 refusals + LLM prompt-injection clamp prove-by-execution at T-403/G4 once impl lands.

**Reconcile before G4 (non-blocking F-1):** `.env.example` `SIGNER_SOCKET_PATH=/run/aats/signer.sock` (line 55)
vs `docker-compose.yml` `SIGNER_SOCKET_PATH=/run/aats-signer/signer.sock` (line 98) — paths must match.
Owner: `crypto-security-engineer` + `latency-devops-engineer` at T-352a.

**Verdict: PASS. T-251 → DONE.**

---

## T-199 — Shared typed contracts — **FAIL → NEEDS-REPLAN** (attempt 2)

`backtest-qa-engineer` **PASS** (mutation-tested the leak guard — reverting `extra="forbid"` makes the 3 negative
tests go RED, proving they exercise the real path). But `code-reviewer` returned **FAIL** with one BLOCKER + one
MAJOR. **Dual-gate requires BOTH PASS → G3 is NOT met.** Substance of the contracts is strong (money int/Decimal,
ExecutionVenue ABC preserves the sol-sniper `execute()` seam, ReasoningAction has no risk-increase member, ADR-0010
guards present) — but two defects remain on the most leak-sensitive contract in the system.

### BLOCKER — intermittent leak-guard enforcement (orchestrator confirmed the failure surface)
`tests/contracts/test_no_truth_fields.py::TestLabelColumnDisjointness` — the 3 negative-construction tests
(`test_label_field_..._at_construction`, `test_truth_is_rug_kwarg_...`, `test_truth_max_multiple_kwarg_...`) failed
on the reviewer's FIRST full-suite run ("DID NOT RAISE") and could not be reproduced across ~45 reruns / hash-seed
sweeps. Suspected root cause: an ordering/state interaction between `model_config extra="forbid"` and the
`@model_validator(mode="before") _reject_float_money` on a `from __future__ import annotations` forward-ref model
that builds lazily (`features.py:140` + `:142`). A leak guard whose own proof tests flake is not trustworthy on
`FeatureFrame` (ADR-0010 / C-7). The engineer's `selfCheckPass=true / 185/185` claim is falsified by the observed failure.

### MAJOR — frozen-contract wire-key drift (orchestrator verified directly)
`aats/contracts/api_schemas.py:239` declares `cls: str = ""` with NO Pydantic alias. `api-contracts.md:147-151`
(FROZEN) specifies the hop field key as `"class"`. `model_dump_json()` will emit `cls`, so Lane E's `/api/latency`
reader (bound to `class`) sees undefined. Fix: `cls: str = Field(default="", alias="class")` +
`populate_by_name=True` + `serialization_alias="class"`, plus a schema test asserting the wire key is `class`.

### Re-plan (attempt 2 → re-dispatch; NOT yet three-strikes)
Split the remediation so the diagnosis is isolated from the contract fix:
- **T-199a (root-cause + de-flake the leak guard):** diagnose the non-deterministic `extra="forbid"` enforcement;
  eliminate the flake (e.g. resolve the before-validator / forward-ref interaction, or rebuild the model after
  field definitions); demonstrate a REPEATABLE green full suite under PYTHONHASHSEED variation in CI (run N times).
- **T-199b (LatencyHop alias):** add the `class` serialization alias + a wire-key schema test; re-run the
  api-contracts conformance check.

Re-entry criteria: full suite green and repeatable (hash-seed sweep), `model_dump_json()` of `LatencyHop` emits
key `class`, and the leak guard proven by mutation test — verified by `code-reviewer` AND `backtest-qa-engineer`.
Owner: `solana-systems-architect` owns the frozen contract; the de-flake fix is implemented to its spec.

**Verdict: FAIL. T-199 → NEEDS-REPLAN (split into T-199a / T-199b).**

---

## Gate decision

- **T-250 — PASS → DONE.**
- **T-251 — PASS → DONE** (reconcile F-1 socket path before G4).
- **T-199 — FAIL → NEEDS-REPLAN** (split T-199a leak-guard de-flake + T-199b LatencyHop alias).

**Foundation status:** the scaffold and custody layers are SOLID and unblock the P3 build lanes.
The contracts package is **usable today** for downstream type imports (the two T-199 defects are a
test-flake and a single wire-key alias — neither changes the field shapes lanes import), so **Wave S
(safety-first Lane C: T-320 → T-321 → T-322) may proceed in parallel** with the T-199a/b fix. The
LatencyHop `class` alias MUST land before Lane E wires `/api/latency` (T-352) and before G4.
