# ENHANCEMENT PROGRAM E1–E13 — CLOSE-OUT LEDGER (COVERED / ADDED)

**Gate:** ENH-COMPLETE — final close of the post-G6 E1–E13 enhancement program (+ 4 running audits).
**Closed by:** `orchestrator`, 2026-06-17, by reading the ACTUAL changed files under `C:/dev/aats`
across all four waves (not the handoff / review JSON), cross-checked against the dual-G3 reviews,
the consolidated suite, and the enhancement-wave security re-audit.
**Directive:** `.agency/00-brief/ENHANCEMENT-DIRECTIVE-E1-E13.md` §ORDERING (AUDIT-FIRST, ADDITIVE).
**Overlay rule 3 — G3 is DUAL** on every model/trade-path code task (`code-reviewer` AND
`backtest-qa-engineer` must both PASS); ops/deploy-only and UI-only items are `code-reviewer`-gated.

**Verdict: PROGRAM COMPLETE.** 13/13 enhancements + 4/4 audit items cleared. 1 COVERED (E8), 16 ADDED.
Every item dual-G3 PASS where the dual gate applies. **No regression to the G6-accepted core build.**
**Real capital stays DRY-RUN-disabled and structurally unreachable.** Security re-audit = **PASS**.

---

## A. Per-item ledger — one line each (E1..E13 + audits)

Legend: **ADDED** = a real gap existed and net-new code/config now closes it (dual-G3 PASS).
**COVERED** = the capability already existed adequately and was verified-not-patched (no code change).

| Item | Title | Verdict | G3 | Primary file(s) — absolute paths | Wave |
|---|---|---|---|---|---|
| **E1** | Devnet live-send validation mode (real devnet SUBMIT; confirm/reconcile/idempotency seam) | **ADDED** | dual PASS (FIXED after 2 process-deaths) | `C:/dev/aats/aats/execution/jito_jupiter_venue.py` (confirm-gate `:1004-1006`; idempotency on `fill.landed` `:342-343`/`:463-464`); `C:/dev/aats/aats/execution/rpc_client.py` (`DevnetRpcClient`); `C:/dev/aats/tests/execution/conftest.py` (`_env_isolation` `:55-89`); ADR-0013 `SubmitMode.DEVNET` (`C:/dev/aats/aats/contracts/venue.py`) | W1→W3 |
| **E2** | Creator/token denylist pre-filter wired into the live BUY entry path | **ADDED** | dual PASS | `C:/dev/aats/aats/risk/resting_orders.py` (STEP-0 `:557-577`; `BuyFireRejection.DENYLISTED :214`; `_DenylistLike` Protocol `:432`) | W2 |
| **E3** | Candidate-queue read-only API + dashboard surface (`GET /api/candidates`) | **ADDED** | dual PASS (API) + `code-reviewer` PASS → backtest-qa PASS (page) | `C:/dev/aats/aats/control_plane/candidate_schemas.py`; `C:/dev/aats/aats/controller/candidate_store.py`; `C:/dev/aats/aats/controller/snipe_loop.py`; `C:/dev/aats/aats/control_plane/server.py`; `C:/dev/aats/tests/control_plane/test_candidates.py`; `C:/dev/aats/dashboard/src/pages/Candidates.tsx`; `C:/dev/aats/dashboard/src/lib/adapters.ts` | W4 |
| **E4** | Control-plane auth + network-exposure hardening (loopback-default bind + fixed Dockerfile + nginx TLS/allowlist) | **ADDED** | dual `code-reviewer` PASS (ops/deploy) | `C:/dev/aats/aats/control_plane/app.py` (`:54`/`:81-113`); `C:/dev/aats/tests/control_plane/test_bind_exposure.py`; `C:/dev/aats/deploy/nginx/aats-controlplane.conf`; `C:/dev/aats/docker/Dockerfile.controlplane`; `C:/dev/aats/.env.example` | W1 |
| **E5** | Always-on operational hardening (systemd units + logrotate + read-only Redis backup) | **ADDED** | `code-reviewer` PASS (ops/deploy — no backtest-qa lane) | `C:/dev/aats/deploy/systemd/aats.service` (+controlplane/backup/timer); `C:/dev/aats/deploy/logrotate/aats`; `C:/dev/aats/scripts/redis-backup.sh`; `C:/dev/aats/docs/redis-backup-restore.md` | W1 |
| **E6** | Discord ingestion adapter (MCS slow-loop sentiment, coordinated-shill de-risk) | **ADDED** | dual PASS | `C:/dev/aats/aats/sentiment/adapters.py` (`DiscordAdapter`+`MockDiscordClient`); `C:/dev/aats/aats/sentiment/models.py` (`source` Literal `"discord" :45`) | W3 |
| **E7** | News / breaking-news layer (de-risk-only) + Narrative & News dashboard page | **ADDED** | dual PASS (backend) + `code-reviewer` PASS (page) | `C:/dev/aats/aats/sentiment/news_scorer.py`; `C:/dev/aats/aats/sentiment/models.py` (`NewsSignal.mcs_delta`∈[-1,0] `:280,295`); `C:/dev/aats/aats/reasoning/reasoner.py` (`adjudicate_with_news() :193`); `C:/dev/aats/dashboard/src/pages/Narrative.tsx` | W3+W4 |
| **E8** | Tunable discovery / SCREENER filter (late-entry / survivor niche) | **COVERED** | dual PASS (verify-not-patch) | `C:/dev/aats/aats/risk/screener.py` (586L, pre-existing); `C:/dev/aats/tests/risk/test_screener.py`; `C:/dev/aats/.env.example` E8 block `291-330` | W2 |
| **E9** | Alpha-caller track-record scoring (HONEST, selectivity-only, no win-rate) | **ADDED** | dual PASS | `C:/dev/aats/aats/sentiment/caller_score.py` (`mcs_delta_contribution<=0`; `assert_caller_signal_cannot_raise_conviction`) | W3 |
| **E10** | Social-velocity + bot-ratio features (de-risk-only) + `python -O` de-risk-guard fix | **ADDED** | dual PASS (E10) + dual PASS (DEF-E10-01) | `C:/dev/aats/aats/sentiment/velocity.py` (`mcs_penalty>=0`; raise-ValueError de-risk guard `:663-687`); `C:/dev/aats/tests/sentiment/test_velocity.py` (`TestDeRiskGuardSurvivesOptimizedMode`) | W3+W4 |
| **E11** | Wallet-cluster / bundle "Bubble Maps" read-only API + dashboard graph (`GET /api/wallet-cluster`) | **ADDED** | dual PASS (API) + `code-reviewer` PASS → backtest-qa PASS (page) | `C:/dev/aats/aats/control_plane/wallet_cluster_schemas.py`; `C:/dev/aats/aats/controller/wallet_cluster_store.py`; `C:/dev/aats/aats/control_plane/server.py`; `C:/dev/aats/tests/control_plane/test_wallet_cluster.py`; `C:/dev/aats/dashboard/src/pages/WalletClusters.tsx`; detection source pre-exists in `C:/dev/aats/aats/features/microstructure.py` + `C:/dev/aats/aats/risk/pretrade_gate.py` | W4 |
| **E12** | Time-stop / stale-narrative exit | **ADDED** | dual PASS | `C:/dev/aats/aats/risk/exit_engine.py` (`STALE_NARRATIVE_TIME_STOP`, flat-clock `:829-832`); `C:/dev/aats/tests/risk/test_exit_engine_time_stop.py` | W2 |
| **E13** | Anti-FOMO / already-pumped exclusion filter | **ADDED** | dual PASS | `C:/dev/aats/aats/risk/anti_fomo.py` (`conviction_multiplier` clamp `[0,1] :439-447`, `>1` raises); `C:/dev/aats/tests/risk/test_anti_fomo.py`; `C:/dev/aats/aats/risk/__init__.py` | W2 |

### Audit items (the 4 running audits embedded in the directive)

| Audit item | Title | Verdict | G3 | Primary file(s) — absolute paths | Wave |
|---|---|---|---|---|---|
| **AUDIT — trailing-ratchet** | Stepped profit-lock RATCHET (breakeven@2x, lock 2x@3x; `locked_floor_r` monotone) | **ADDED** | dual PASS | `C:/dev/aats/aats/risk/exit_engine.py` (`RATCHET_STOP :865-874` + `_advance_ratchet :827`); `C:/dev/aats/tests/risk/test_exit_engine_ratchet.py` | W2 |
| **AUDIT — micro-preset** | Named MICRO early-entry preset (verified + LP≥30d + honeypot + top-5<20% + 0.5% + 18%-stop/24h) | **ADDED** | dual PASS | `C:/dev/aats/aats/risk/presets.py`; `C:/dev/aats/tests/risk/test_micropreset.py`; `C:/dev/aats/aats/risk/__init__.py`; `C:/dev/aats/.env.example` | W2 |
| **AUDIT — liquidity-sanity** | Pre-trade liquidity-sanity VETO (24h-vol≥10× notional + x·y=k slippage sim ≤300 bps) | **ADDED** | dual PASS | `C:/dev/aats/aats/risk/liquidity_sanity.py`; `C:/dev/aats/tests/risk/test_liquidity_sanity.py`; `C:/dev/aats/aats/risk/__init__.py` | W2 |
| **AUDIT — risk-tiers** | Soft ~2% daily-loss REDUCE/PAUSE tier (strictly below the hard breaker) + GATE-B minimum-sample guard | **ADDED** | dual PASS | `C:/dev/aats/aats/risk/circuit_breaker.py` (`DEFAULT_SOFT_RATIO :90`, `risk_posture :407`, `is_soft_reduced :418`); `C:/dev/aats/aats/models/gate_b.py` (min-sample guard `:37+`) | W2 |

**Tally:** E1–E13 → 12 ADDED + 1 COVERED (E8). Audits → 4 ADDED. **Total: 16 ADDED, 1 COVERED, 0 FAILED, 0 outstanding.**

---

## B. Wave-by-wave provenance

| Wave | Record | Items | Outcome |
|---|---|---|---|
| W1 | `ENH-wave1.md` | E1 · E4 · E5 | E4+E5 ADDED; E1 FAILED→NEEDS-REPLAN (BLOCKER live, engineer died) |
| W2 | `ENH-wave2.md` | E2 · E8 · E13 · E12 + 4 audits; E1 re-fix | E8 COVERED, 7 ADDED; E1 re-fix died again (process event, attempt unchanged); ADR-0013 ACCEPTED |
| W3 | `ENH-wave3.md` | E6 · E7(backend) · E9 · E10; E1 re-fix | 4/4 ADDED; **E1 re-fix FIXED — dual G3 PASS** (BLOCKER closed) |
| W4 | this close-out | E3(api+page) · E11(api+page) · E7(page) · DEF-E10-01 fix | all ADDED; final security/suite re-verification |

**E1 history (3-strikes lens):** two consecutive dispatch *deaths* (process events, NOT content strikes —
no `code-reviewer`+`backtest-qa` verdict was ever produced on a fix diff, so no content failure to count).
The W3 scoped re-plan (confirm/reconcile/idempotency seam only, unconfirmed-branch test FIRST) landed and
both reviewers re-ran it independently → dual PASS. E1 is now **FIXED / ADDED**, never CEO-escalated.

---

## C. Wave-4 detail (closed this gate)

- **E3 API — `GET /api/candidates` (ADDED).** Additive read-only endpoint. `CandidateQueue` bounded ring
  (`candidate_store.py`, thread-safe, `maxlen=200`); `CandidateRecord`+`SafetyReport` Pydantic schemas
  (`candidate_schemas.py`, NOT in frozen `aats/contracts/`); `SnipeLoop._record_candidate()` records at
  every decision exit (no-op when queue is None — backward-compatible). 16 tests. §12 frozen endpoint list
  UNCHANGED; §14 additive delta note added to `api-contracts.md`. Dual G3 PASS — both reviewers independently
  enumerated routes (GET-only, real 405 on POST), confirmed no `win_rate`, no money/lamport field
  (`model_p` is a probability float), drove `process_event` through every decision branch and verified the
  recorded reason matches the returned reason in EVERY branch.
- **E3 page — Candidates surface (ADDED).** Re-review after a BLOCKER fix (E3-B1: the candidate WIRE adapter
  was typed to an invented shape and silently dropped every safety warning). Re-typed to the AUTHORITATIVE
  `CandidateRecord`; safety derived from `safety_report` alone, fail-closed on missing `gate_passed`.
  Read-only (GET-only, no POST/control), 89/89 dashboard tests, mock build GREEN. `code-reviewer` PASS →
  backtest-qa PASS.
- **E11 API — `GET /api/wallet-cluster` (ADDED).** Typed `WalletClusterGraph` schema
  (`wallet_cluster_schemas.py`) + `WalletClusterStore` bounded ring (`wallet_cluster_store.py`) + additive
  GET-only endpoint. **No new detection logic** — projects EXISTING data (`sniper_cluster_score`,
  `bundling_detected`, `bundler_wallet_count` from `microstructure.py`; `dev_bundle_cluster_bps` from
  `pretrade_gate.py`). 15 tests; 157 control_plane pass (deterministic ×2). Read-only is framework-enforced
  (GET-only route, true 405). All shares int bps in `[0,10000]`; scores/weights bounded statistical floats;
  no money, no `win_rate`.
- **E11 page — "Bubble Maps" wallet-cluster graph (ADDED).** SVG-native (no recharts) code-split page;
  typed adapter with dangling-edge drop + bps/score clamps; GET-only hook with `[]`-fallback. 107 tests
  (+18 E11). `code-reviewer` PASS → backtest-qa PASS.
- **E7 page — Narrative & News (ADDED).** Deferred dashboard half built. Per-asset narrative cards;
  de-risk `mcs_delta`∈[-1,0] re-clamped IN THE ADAPTER (`clampDeRiskDelta` — a positive/hostile frame clamps
  to 0, never a buy signal). 123 dashboard tests; mock build GREEN. `code-reviewer` PASS → backtest-qa PASS.
- **DEF-E10-01 fix — `python -O` de-risk guard (ADDED).** The W3 carry-forward MAJOR. The bare
  `assert adjusted <= base_conviction` (stripped under `python -O`) in
  `apply_velocity_penalty_to_conviction` replaced with (1) an explicit `raise ValueError` on a negative raw
  `mcs_penalty`, (2) a `max(0.0, penalty_raw)` clamp, (3) a `raise ValueError` output guard. Proven under
  `python -O` (`__debug__=False`): a forged `mcs_penalty=-0.5` now RAISES instead of silently raising
  conviction. `TestDeRiskGuardSurvivesOptimizedMode` (7 tests); mutation-proven (revert to bare assert → 5/7
  RED; the mutated code leaks conviction 0.7→1.0 under `-O`). 209 sentiment tests. Dual G3 PASS.

---

## D. Non-blocking carry-forwards filed during the program (none gate close-out)

- **QA-E3 / QA-E11-001 (MINOR, shared):** the E3 and E11 read-only endpoints' top-level `win_rate`/money
  guards do not deep-walk nested `node`/`edge` dicts, and the store takes free-form `list[dict]`.
  Unreachable in production today — **no producer is wired** (`wallet_cluster_provider` is never wired in
  prod app construction; the live endpoints return `[]`). Future hardening: coerce each record through the
  Pydantic model at response time (`[WalletClusterGraph(**r).model_dump() for r in provider()]`). Owners:
  `backend-engineer` / `solana-systems-architect`.
- **QA-E3 snapshot copy (MINOR):** `CandidateQueue.snapshot()` returns the same dict objects (docstring
  claims a shallow copy). Latent — the GET path never mutates; the writer builds a fresh dict per record.
  Owner: `agent-orchestration-engineer`.
- **Mock-key NIT (codebase-wide):** E3/E7/E11 mock factories regenerate `asset`/`mint` per poll tick →
  React-key churn (mock-only; live path uses stable wire mint). Optional, agency-wide.
- **E6 doc drift (MINOR):** comment cites `DISCORD_ALLOWLIST` while `.env.example` defines
  `DISCORD_CHANNEL_ALLOWLIST` (doc-only). E9 unused `field` import. E10 docstrings.
- **Deploy-time:** Discord snowflake→account-age formula validation; live-LLM injection contract test;
  E9/E10 pipeline wiring + Parquet caller-outcome store (re-validate the `<=0` contribution when wired).

---

## E. Hard-rule conformance — re-confirmed for the WHOLE program

1. **De-risk / selectivity-only.** Every enhancement signal can ONLY lower conviction / force-exit / veto /
   reduce / reject. Raising conviction, sizing up, widening a stop, adding leverage, or producing a buy
   trigger is **inexpressible by type** across the program (`ReasoningAction` has 4 de-risk members only;
   `NewsSignal`/`CallerSignal`/`VelocitySignal` are strictly non-positive; `conviction_multiplier`∈[0,1];
   the `DeRiskIntentFactory` has no `entry()`). The W4 read-only endpoints (E3/E11) and dashboard pages
   (E3/E7/E11) expose **zero control surface** (GET-only, no POST, no kill/flatten/size/veto control).
   Confirmed by mutation tests across the program and by the security 720-combination adversarial sweep
   (ZERO risk-increase escapes).
2. **Slow-loop only where applicable.** `aats.sentiment` + `aats.reasoning` are imported by NO
   FAST/snipe/execution/controller module (grep-confirmed). E2 (O(1) denylist) and liquidity-sanity (pure
   int/Decimal x·y=k) are CORRECTLY on the FAST pre-trade path as de-risk VETOes — a denylist hit / thin-pool
   reject must fire before a buy lands; neither adds a social/news signal to the hot path.
3. **Point-in-time / leak-free.** Every signal reads event-time only (event_slot / block_time deltas);
   shift-back-one-tick leak-proven; future records excluded as the first op. The E3/E11 endpoints carry
   `evaluated_at_slot` event-time and are pure projections (no feature/label builder, no lookahead surface).
4. **Safety primitives still fire.** The daily-loss circuit breaker, three-layer survivable stop, and
   dead-man's switch are UNTOUCHED by the entire program — present at
   `circuit_breaker.py:267` / `survivable_stop_coordinator.py:41` / `deadman.py:98`. The new soft −2%
   REDUCE/PAUSE tier is STRICTLY below the hard −3% / −0.30 SOL trip; a hard trip implies REDUCED.
5. **Real capital DRY-RUN-disabled + unreachable.** `DRY_RUN_ENABLED` default `true` unchanged; mainnet LIVE
   remains hard-gated by 3 independent gates; `SubmitMode.DEVNET` (ADR-0013) is devnet-cluster-bound and
   structurally CANNOT unlock mainnet LIVE; devnet = worthless SOL. Money is int lamports / `Decimal`
   everywhere (float rejected at boundaries). No `win_rate` field anywhere. No secrets in any changed file
   (`.env.example` placeholders / Vault refs only). **`aats/contracts/` and `docker-compose.yml` were NOT
   edited by any enhancement item** (the sole frozen-contract change is the additive ADR-0013
   `SubmitMode.DEVNET` enum member, audited + accepted).

---

## F. Consolidated suite — GREEN

```
Python tests/:   2283 passed, 2 skipped (121.74s)
Dashboard build: GREEN on mock (built in 21.02s)
Dashboard tests: 17 files / 123 tests passed (23.40s)
```

Deterministic flags: `PYTHONHASHSEED=0`, `PYTHONDONTWRITEBYTECODE=1`, `-p no:cacheprovider`, `__pycache__`
purged first. The 2 skips are the allowed solders-gated `test_tx_builder.py:161/:186` (conditional skips,
not failures). No failing files. `tests/execution` deterministic (E1 env-leak flake gone since W3).

> Scope note: this is a green-and-stable + source-conformance confirmation. It does NOT re-derive the
> purged/embargoed walk-forward, the net-of-cost model, or the edge-vs-baseline proofs from scratch — those
> assertions are embedded in the 2283 passing tests. The live edge remains `UNPROVEN-NO-REAL-DATA` (the
> correct, accepted PAPER outcome; the enhancement program does not change it).

---

## G. Security re-audit — PASS

Record: `.agency/05-reports/security/ENH-security-reaudit.md` (`crypto-security-engineer`, 2026-06-17).

- **Verdict: PASS** — no open CRITICAL/HIGH introduced by the enhancements. Secrets sweep CLEAN (git history
  + content): no keypair/PEM/mnemonic/`sk-`/`hvs.` literal; only fake redaction fixtures.
- **E1 SAFE:** DRY-RUN default; `devnet + live_submit_enabled + DRY_RUN_ENABLED=false` → `submit_mode=DEVNET`,
  NEVER LIVE (structural mutual exclusion `jito_jupiter_venue.py:214-218`); mainnet 3-gate intact.
- **E4 SAFE:** destructive POSTs operator-Bearer gated (no-auth kill → 403, no side effect); LIVE fenced;
  loopback-default bind; nginx TLS+HSTS+default-deny allowlist recipe.
- **E3/E11 read-only endpoints SAFE:** GET-only (POST → 405), money-correct (bps int, probabilities/scores
  non-money floats), no `win_rate`, fail-safe `[]` with no provider — no new control/risk surface.
- **Prompt-injection clamp HOLDS:** 720-combination adversarial sweep — every applied action de-risk, ZERO
  risk-increase escapes.
- **Pre-live checklist ACCURATE:** `docs/pre-live-checklist.md` correctly tracks COND-G4-2 (F-01 signer
  refusals, F-10 image digests, F-07 container lockdown, F-02/03/04 supply-chain). Carried unchanged, latent
  because LIVE is unreachable. **The R3 pre-live checklist remains the gate before `DRY_RUN_ENABLED=false`.**

---

## H. Verdict

**ENHANCEMENT PROGRAM E1–E13 → COMPLETE.** 13/13 enhancements (1 COVERED · 12 ADDED) + 4/4 audits (ADDED),
every dual-G3-gated item PASS, no regression to the G6-accepted core, consolidated suite GREEN
(2283/2 + dashboard 123), security re-audit PASS. **The whole engagement is done: core build G0–G6 ACCEPTED
+ the E1–E13 enhancement program CLOSED.**

**Unchanged and carried (the one decision the agency does not make alone):** real capital stays
DRY-RUN-disabled and unreachable; the **R3 pre-live checklist** (Block A edge-on-RECORDED-data · Block B
custody/security COND-G4-2 · Block C CEO legal + funding + R3 sign-off) remains the gate before
`DRY_RUN_ENABLED=false` and is NOT cleared by this program.
