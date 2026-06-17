# Enhancement Wave 1 — Verification Record (E1 · E4 · E5)

_Recorded: 2026-06-17 by `orchestrator`. Verified by reading the ACTUAL changed files under
`C:/dev/aats` (not trusting handoff/review JSON), cross-checked against the dual-G3 reviews
and the safety contract. Source: `.agency/00-brief/ENHANCEMENT-DIRECTIVE-E1-E13.md` §ORDERING
(AUDIT-FIRST, ADDITIVE). G3 is DUAL on every code task: `code-reviewer` AND `backtest-qa-engineer`
must both PASS (overlay rule 3); ops/deploy-only items with no model/trade-path logic are
`code-reviewer`-gated.)_

## Verdict summary

| Item | Title | Verdict | G3 |
|---|---|---|---|
| **E1** | Devnet live-send validation mode | **FAILED — not COVERED, not ADDED** | dual FAIL (engineer died on fix) |
| **E4** | Control-plane auth + exposure hardening | **ADDED** | dual G3 PASS (`code-reviewer` ×2) |
| **E5** | Always-on operational hardening (systemd + logrotate + Redis backup) | **ADDED** | G3 PASS (`code-reviewer`; ops/deploy — no backtest-qa lane) |

**Wave 1 outcome:** E4 + E5 ADDED and recorded DONE. **E1 stays IN-PROGRESS / NEEDS-REPLAN**
— the BLOCKER is live in the tree and the fix dispatch died before any PASS. Real capital
stays DRY-RUN-disabled; devnet is worthless-SOL; mainnet LIVE remains hard-gated.

---

## E1 — Devnet live-send validation mode — **FAILED (BLOCKER live in tree)**

**Verdict: NOT COVERED, NOT ADDED.** Status from input: `FAILED`, `reason: "engineer died on fix"`.
Both G3 reviewers returned **FAIL** (one `code-reviewer`, one `backtest-qa`-class probe). Dual G3
requires BOTH PASS — neither passed.

**BLOCKER (orchestrator re-confirmed from source, this verification):**
A devnet tx that is SUBMITTED but never CONFIRMS is reconciled as a successful **landed fill**.
- `aats/execution/jito_jupiter_venue.py:766-772` — `_send_and_confirm_devnet` returns
  `LandResult(submitted=True, reason="devnet_confirm_failed:...", land_slot=None)` on a confirm
  timeout (`if not confirm.confirmed:`).
- `:943` — the retry loop returns on `if result.submitted: return result` (treats `submitted` as
  terminal success).
- `:977-986` — `reconcile()` keys success on `if land.submitted and land.signature:` → emits
  `FillResult(landed=True, reason="filled")` for a tx that never confirmed (`land_slot=None`).
- `:336-337` — `execute()` adds `intent_id` to the idempotency set on `land.submitted` (including
  the timeout case), so the unconfirmed intent can never be legitimately retried.

This is a PnL-corrupting false-positive fill on **exactly** the submit→confirm→reconcile path E1
exists to validate — and there is **zero test coverage** for the unconfirmed branch
(`tests/execution/test_devnet_submit.py` — every devnet test uses `confirm_after_polls=1` (always
confirms) or a failed send). Both reviewers reproduced `landed=True` for
`MockDevnetRpcClient(confirm_after_polls=99, land_succeeds=True)`.

**MAJOR (test-reliability):** `tests/execution/` is order/hash-seed dependent and intermittently
fails (most safety-sensitive: a DRY_RUN venue resolved to LIVE under a leaked `DRY_RUN_ENABLED=false`
in `os.environ`). Root cause: process-level env-state leakage not reverted between tests
(`test_jito_jupiter_venue.py:266-280` raw `os.environ.pop`/restore instead of `monkeypatch`).

**MINOR-1 (traceability, architect's lane):** E1 adds `SubmitMode.DEVNET` + a real devnet SUBMIT
path, but the FROZEN `execution-venue.md` SubmitMode lists only SIMULATION/DRY_RUN/LIVE and
`infrastructure.md` §6 still says devnet = "no submit". The directive authorizes E1, but no blueprint
delta/ADR was recorded for a change to a FROZEN contract.

**Required to clear E1 (engineer's lane):**
1. DEVNET reconcile must gate `landed` on CONFIRMATION, not submission: treat `confirm.confirmed=False`
   (reason `devnet_confirm_failed:*` / `land_slot=None`) as `landed=False`. Propagate a `confirmed`
   flag through `LandResult`, or reject the unconfirmed reason in `reconcile()` on the devnet path.
2. Only add `intent_id` to the idempotency set on a CONFIRMED land.
3. Add a regression test: devnet submit succeeds but never confirms → assert `FillResult.landed is
   False` and `reason != "filled"`.
4. (MAJOR) autouse monkeypatch fixture snapshotting/restoring `DRY_RUN_ENABLED`, `SOLANA_CLUSTER`,
   `RPC_DEVNET` around every test; replace raw `os.environ` mutation in `test_jito_jupiter_venue.py`;
   prove `tests/execution/` green across ≥50 consecutive runs and PYTHONHASHSEED=random.
5. (MINOR-1) `solana-systems-architect` issues a short delta notice/ADR updating `execution-venue.md`
   SubmitMode + `infrastructure.md` §6 to reflect E1 DEVNET submit semantics. No code change.

**Re-plan note:** the death is a PROCESS event, not a content strike — but unlike T-326/T-199fix
(fix landed, just unverified), E1 has a **real BLOCKER live in the tree**. Re-entry is a genuine
FIX + dual G3, NOT verdict-only. Re-dispatch `solana-execution-engineer`.

**Files (E1 changeset, NOT accepted):**
`aats/execution/jito_jupiter_venue.py`, `tests/execution/test_devnet_submit.py`,
plus `aats/execution/rpc_client.py` devnet client + `.env.example` E1 block (placeholders only,
verified clean).

---

## E4 — Control-plane auth + exposure hardening — **ADDED**

**Verdict: ADDED.** AUDIT found destructive-POST operator-auth already adequate (COVERED portion);
the real gap was network exposure — the control plane could not even start (Dockerfile pointed at a
nonexistent module and hardcoded `--host 0.0.0.0`). Dual G3 PASS (two `code-reviewer` passes; one
neutered the auth check to a no-op → 8 rejection tests went RED, then restored byte-identical —
mutation-meaningful, not tautological).

**Source-confirmed (orchestrator read the files):**
- `aats/control_plane/app.py:54` `DEFAULT_BIND_HOST = "127.0.0.1"`; `resolve_bind_host` (`:81-113`)
  returns loopback absent override; a non-loopback override is honored but logs a loud `SECURITY:`
  warning. `main()` (`:181-194`) binds via `resolve_bind_host`; `app` object is a real FastAPI
  instance (`:178`).
- `docker/Dockerfile.controlplane:62` `CMD ["python", "-m", "aats.control_plane.app"]` — fixes the
  broken/nonexistent `aats.control_plane.app:app` + bare `uvicorn --host 0.0.0.0`.
- `deploy/nginx/aats-controlplane.conf` + README — TLS 1.2/1.3 + HSTS, geo deny-by-default IP
  allowlist, rate-limited destructive surface, optional basic-auth, proxies to loopback upstream,
  SSE buffering off, 80→443 redirect.
- `deploy/docker-compose.controlplane-bind.override.yml` — re-publishes as `127.0.0.1:8787:8787`;
  base `docker-compose.yml` UNTOUCHED (parallel-lane rule honored — confirmed).
- `.env.example` E4 block — placeholders only (`CONTROL_PLANE_BIND_HOST=127.0.0.1`, vault-ref token
  placeholders); secret sweep clean.
- Telegram authz is operator-ID-only + fail-closed + de-risk-only by construction (`pause()` hard-codes
  SHADOW — channel structurally cannot move mode up / reset breaker / widen risk).

**Tests:** `tests/control_plane/test_bind_exposure.py` (14 new) — default == 127.0.0.1 and != 0.0.0.0,
empty/whitespace env → loopback fail-safe, non-loopback opt-in requires the SECURITY warning, invalid
port → safe fallback, unauthenticated destructive POSTs rejected. `pytest tests/control_plane -q` →
126 passed (per both reviewers' execution).

**Non-blocking follow-ups (routed, do not gate ADDED):**
- MINOR (pre-existing, OUT OF E4 scope) `server.py:76` `operator_token` defaults to literal
  `"dev-token"` when `OPERATOR_TOKEN` unset → loopback-default bind substantially mitigates, but a
  hardened deploy should fail-closed. Flag to `backend-engineer`/`crypto-security-engineer`.
- nginx conf carries placeholder `server_name`/cert paths/IP-allowlist the operator must replace —
  `latency-devops-engineer` owns provisioning.
- F-03 (gitleaks/pip-audit in CI with egress) + F-01 (Rust signer-side spend-cap/allowlist) remain
  pre-existing R3/LIVE preconditions — NOT E4 deliverables.

**Files:** `aats/control_plane/app.py`, `tests/control_plane/test_bind_exposure.py`,
`deploy/nginx/aats-controlplane.conf`, `deploy/nginx/README.md`,
`deploy/docker-compose.controlplane-bind.override.yml`, `docker/Dockerfile.controlplane`,
`.env.example`.

---

## E5 — Always-on operational hardening — **ADDED**

**Verdict: ADDED.** AUDIT found `startup-self-check.sh` already present and adequate (COVERED
portion). Three missing components were built. G3 PASS (`code-reviewer`; this is an ops/deploy item
with no model/trade-path logic, so no `backtest-qa` lane applies — the reviewer ran syntax +
structural + dry-run + byte-level + secret-sweep verification).

**Source-confirmed (orchestrator read the files):**
- `deploy/systemd/aats.service` — full compose-stack unit; `ExecStartPre` runs
  `scripts/startup-self-check.sh` (shell-level DRY-RUN gate); **never sets `DRY_RUN_ENABLED=false`**
  (`:66-69` explicit comment; absent → compose default "true"); `Type=oneshot`/`RemainAfterExit=yes`;
  wraps (does not replace) `docker-compose.yml`.
- `deploy/systemd/aats-controlplane.service`, `aats-backup.service`, `aats-backup.timer`
  (daily 03:00 UTC, `Persistent=true`), `README.md`.
- `deploy/logrotate/aats` — decision log 90d, startup 8w, backup 12w, docker json-file 7d.
- `scripts/redis-backup.sh` — `set -euo pipefail`; docker + container-running guards; BGSAVE trigger
  + LASTSAVE poll (30s timeout, graceful fallback); `docker cp` → `gzip -9` → `gzip -t` integrity
  check; `find -mtime +RETENTION_DAYS` prune; `DRY_RUN_BACKUP=true` smoke mode (exit 0, writes
  nothing); **does NOT consult `DRY_RUN_ENABLED`** (read-only from Redis; never touches trading logic).
- `docs/redis-backup-restore.md` — full step-by-step restore (stop → gunzip → volume-inspect → cp
  dump.rdb → restart → verify breaker/DMS state).
- `docker-compose.yml` UNCHANGED (`DRY_RUN_ENABLED: "${DRY_RUN_ENABLED:-true}"` safe default intact).

**Verification (reviewer-run):** `bash -n` SYNTAX OK on both shell scripts; `DRY_RUN_BACKUP=true`
exits 0 writing nothing; `startup-self-check.sh` 10/10 hard checks PASS (its embedded
`docker compose config --quiet` proves compose YAML valid + unclobbered); systemd structural lint
4/4; logrotate brace balance 4/4; pure-LF (no CRLF deploy hazard); secret scan across 9 new files 0
hits; container naming consistent with compose default project `aats`.

**Limitation (documented, not blocking):** `systemd-analyze verify` + `logrotate --debug` require a
Linux host with systemd/logrotate — cannot run in the Windows dev env. Byte-level + structural
validation substituted. Pre-live host steps documented in each unit's install comment +
`G5-E5-EVIDENCE.md`. Decision-log logrotate stanza requires a host volume mount (operator step,
documented; compose edit deferred to avoid parallel-lane collision).

**Files:** `deploy/systemd/aats.service`, `deploy/systemd/aats-controlplane.service`,
`deploy/systemd/aats-backup.service`, `deploy/systemd/aats-backup.timer`, `deploy/systemd/README.md`,
`deploy/logrotate/aats`, `scripts/redis-backup.sh`, `docs/redis-backup-restore.md`,
`.agency/05-reports/gates/G5-E5-EVIDENCE.md`.

---

## Safety contract — re-confirmed intact after Wave 1

- **DRY-RUN still the default.** `_dry_run_env_disabled()` (`jito_jupiter_venue.py:1031-1037`) defaults
  to "true" (safe); only an explicit `DRY_RUN_ENABLED=false` disables it. No E4/E5 unit or script sets
  it false.
- **Mainnet LIVE still hard-gated.** `_assert_live_allowed` (`:999-1023`) requires
  `live_submit_enabled` AND `DRY_RUN_ENABLED=false` (3 independent gates). `cluster=devnet` does NOT
  unlock LIVE — independent gates, test-proven (`test_devnet_mode_does_not_unlock_mainnet_live`).
- **Devnet = worthless SOL** on a separate cluster (E1 path, currently FAILED/not accepted).
- **Safety primitives (breaker / survivable stop / DMS) untouched** — E4 (control plane) and E5
  (ops/deploy) sit above/outside M4; no change to the firing logic.
- **No secrets** in any Wave-1 file (placeholders only). Real capital stays DRY-RUN-disabled and
  unreachable.

---

## Consolidated suite

**Orchestrator has no shell — the consolidated suite was NOT run in this verification.** The Runtime
must run it once and paste the count here:

```
find aats tests -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null; \
  PYTHONDONTWRITEBYTECODE=1 PYTHONHASHSEED=0 python -m pytest tests/ -q \
  -p no:cacheprovider --tb=no 2>&1 | tail -1
```

Reviewer-reported (NOT orchestrator-run): broad suite `tests/` minus e2e/validation = 1846 passed /
2 skipped; `tests/control_plane` = 126 passed; safety primitives `tests/risk` + `test_dry_run_invariant.py`
= 322 passed. **Caveat:** E1's `tests/execution/` is intermittently order/hash-seed flaky (E1 MAJOR) —
a deterministic `PYTHONHASHSEED=0` run is the right gate; expect it to surface the E1 instability until
the E1 MAJOR is fixed.

---

## Next: Wave 2

Per `ENHANCEMENT-DIRECTIVE-E1-E13.md` §ORDERING — **Wave 2 = E2 · E8 · E13 · E12** + the running audits.
E1 re-plan (genuine FIX + dual G3, `solana-execution-engineer`) runs IN PARALLEL with Wave 2 (disjoint
module). Real capital stays DRY-RUN-disabled throughout.
