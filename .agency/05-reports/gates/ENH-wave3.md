# ENHANCEMENT WAVE 3 — Gate Record (+ E1 re-fix verdict)

**Gate:** G3 (per-task build) for the E1–E13 enhancement program, Wave 3.
**Verified by:** `orchestrator`, 2026-06-17, by reading the ACTUAL changed files under `C:/dev/aats`
(not the handoff / review JSON).
**Scope:** Wave 3 = E6 (Discord ingestion) · E7 (News/breaking-news layer) · E9 (alpha-caller
track-record scoring) · E10 (social-velocity / bot-ratio features). Plus the **E1 re-fix**
(devnet confirm/reconcile/idempotency seam), which ran in parallel.

**Overlay rule 3 — G3 is DUAL:** `code-reviewer` **AND** `backtest-qa-engineer` must both PASS.
For UI-only / ops-only items a single `code-reviewer` PASS is sufficient; every Wave-3 signal item
is a model/signal-path change so the dual gate applies and was met.

---

## Verdict summary

| Item | Title | Verdict | code-reviewer | backtest-qa | Primary path(s) |
|---|---|---|---|---|---|
| **E1** | Devnet live-send confirm/reconcile/idempotency re-fix | **PASS (FIXED)** | PASS | PASS | `aats/execution/jito_jupiter_venue.py`, `aats/execution/rpc_client.py`, `tests/execution/conftest.py` |
| **E6** | Discord ingestion adapter (MCS slow-loop sentiment) | **ADDED** | PASS | PASS | `aats/sentiment/adapters.py`, `aats/sentiment/models.py` |
| **E7** | News / breaking-news layer (de-risk-only) | **ADDED** | PASS | PASS | `aats/sentiment/news_scorer.py`, `aats/reasoning/reasoner.py` |
| **E9** | Alpha-caller track-record scoring (HONEST, selectivity-only) | **ADDED** | PASS | PASS | `aats/sentiment/caller_score.py` |
| **E10** | Social-velocity + bot-ratio features (de-risk-only) | **ADDED** | PASS | PASS | `aats/sentiment/velocity.py` |

**Wave 3: 4/4 items CLEARED dual G3 (all ADDED). E1 re-fix: dual G3 PASS — the BLOCKER is FIXED.**

---

## E1 re-fix — devnet confirm/reconcile/idempotency seam (PASS, BLOCKER FIXED)

The 2nd-consecutive-death re-plan landed and was independently re-run by both reviewers. The
orchestrator confirmed the fix directly in source:

- **BLOCKER-1 (false-positive fill) — FIXED.** `reconcile()` now gates `landed=True` on
  `land.submitted AND land.signature AND reason in ("landed","devnet_landed")`
  (`jito_jupiter_venue.py:1004-1006`). An unconfirmed devnet tx
  (`reason="devnet_confirm_failed:*"`, `land_slot=None`, `submitted=True`) reconciles as
  `landed=False` — it is NOT a fill. The idempotency set is now keyed on `fill.landed`
  (`:342-343` execute, `:463-464` exit), so an unconfirmed intent stays **retryable** and the
  set is not poisoned. Mutation-proven by backtest-qa: reverting the confirm-gate to
  `if land.submitted and land.signature:` turns `test_unconfirmed_devnet_tx_reconciles_as_not_landed`
  RED (1 failed / 200 passed) — the regression test is the sole, load-bearing guard.
- **BLOCKER-2 (DRY_RUN→LIVE env-leak flake) — FIXED.** Autouse `_env_isolation` fixture
  (`tests/execution/conftest.py:55-89`) snapshots + restores all 9 venue env vars per test via the
  fixture-provided `monkeypatch` (auto-rollback); raw `os.environ` mutations in
  `test_jito_jupiter_venue.py` replaced with `monkeypatch.delenv`. Grep confirms NO raw
  `os.environ.pop` / `os.environ[...]=` mutations remain under `tests/execution`. The dangerous
  DRY_RUN→LIVE flip vector is HARD-CLOSED (suite green under hostile `DRY_RUN_ENABLED=false` process
  env across seeds).
- **MAJOR-1 (confirm-timeout not configurable) — FIXED.** `_send_and_confirm_devnet()` reads
  `DEVNET_CONFIRM_MAX_POLLS` / `DEVNET_CONFIRM_POLL_INTERVAL_S` from env and passes them as kwargs to
  `confirm_transaction()` (`jito_jupiter_venue.py:755-765`); `MockDevnetRpcClient` /
  `DevnetRpcClient` signatures unified.

**Determinism proof:** `tests/execution` = **201 passed, 2 skipped** across 5 claimed PYTHONHASHSEED
seeds (0,42,123,999,7777) + 10 additional unclaimed seeds + 8 consecutive identical runs + 5x hostile
`DRY_RUN_ENABLED=false`. The env-leak flake does NOT reproduce. The 2 skips are the allowed
solders-gated `test_tx_builder.py:161/:186`.

**Safety contract:** unchanged. `SubmitMode.DEVNET` (ADR-0013) is devnet-cluster-bound and structurally
CANNOT unlock mainnet LIVE; DRY-RUN remains the default; devnet = worthless SOL. Mypy clean on the two
changed production files (the 2 reported mypy errors are pre-existing, in unchanged files —
`signer_client.py` AF_UNIX on win32, `sell_sim.py` override — out of scope).

---

## E6 — Discord ingestion adapter (ADDED)

- **Net-new, source-confirmed:** `source` Literal extended to include `"discord"`
  (`aats/sentiment/models.py:45`); `DiscordAdapter` + `MockDiscordClient` added in
  `aats/sentiment/adapters.py`. No prior Discord path existed.
- **De-risk / selectivity-only:** coordinated shill burst LOWERS conviction — reproduced
  shill=0.191 vs organic=0.418, penalty=0.550, `coordinated_shill_flag=True`. `assert_mcs_cannot_size_up`
  proves size is identity for conviction in {0.0, 0.5, 0.99, 1.0}; even a max-bullish LLM caps at
  `(1 - penalty)`. MCS bounded [0,1] by the frozen pydantic validator.
- **Slow-loop only:** `aats.sentiment` is imported by NO FAST / snipe / execution / controller module
  (grep-confirmed). Discord posts → exactly 1 Tier-B LLM call (no per-post LLM).
- **Point-in-time:** future posts excluded as the FIRST Tier-A op; adding a t+1ms post leaves the score
  byte-identical. `event_time_ms` from the message timestamp, not fetch time.
- **Injection-safe:** ingested text routed inside the QUOTED-UNTRUSTED block; injection attempt →
  conviction 0.243, not 1.0.
- **Safety / secrets:** `bot_token=None` offline default returns `[]`; no self-bot field (structurally
  impossible); `.env.example` E6 block = placeholders only; no `win_rate` anywhere; money untouched.
- **MINOR (non-blocking):** F1 dead offline-injection seam in `MockDiscordClient` docstring; **F2**
  comment cites `DISCORD_ALLOWLIST` while `.env.example` defines `DISCORD_CHANNEL_ALLOWLIST`
  (`adapters.py:363` — confirmed name drift, doc-only); F3 single-cluster synchronicity=0.0 (penalty
  comes from age+concentration; pre-existing Tier-A behavior); snowflake→account-age formula
  (Discord epoch) is offline/commented and must be validated at deploy.

## E7 — News / breaking-news layer (ADDED)

- **Net-new, source-confirmed:** `aats/sentiment/news_scorer.py` (keyword heuristic, ZERO LLM calls);
  `NewsSignal.mcs_delta` documented + constrained to **[-1.0, 0.0]** (`models.py:280,295`);
  `Reasoner.adjudicate_with_news()` added (`reasoning/reasoner.py:193`).
- **De-risk-only by construction:** `mcs_delta` architecturally clamped non-positive — positive/neutral
  news = exactly 0.0 (TIER1 -0.50 / TIER2 -0.35 / TIER3 -0.125). `credible_negative_event` (TIER_1/TIER_2)
  forces `narrative_failure=True` → FORCE_EXIT (open position) or VETO_ENTRY (no position) via the
  `DeRiskIntentFactory`. `ReasoningAction` = {HOLD, VETO_ENTRY, REDUCE_SIZE, FORCE_EXIT} only — an
  EntryIntent / size-up is INEXPRESSIBLE BY TYPE. Worst-case STRONG_BUY LLM + credible-negative news →
  FORCE_EXIT, not entry.
- **Slow-loop only:** news module referenced only by `aats.sentiment` + `aats.reasoning`; FAST-loop
  enforcer wiring has NO import of either (grep-confirmed).
- **Point-in-time:** shift-back-one-tick removes the articles' effect entirely; `fetch_time` never used
  as `event_time`; future articles excluded with a belt-and-suspenders guard.
- **Injection-safe:** injection article → `mcs_delta=0.0`, `credible_negative_event=False`; wrapped as
  QUOTED UNTRUSTED DATA; control flow unaffected.
- **Resilience:** `RSSNewsAdapter.fetch_news()` NotImplementedError (offline-first) is caught by the
  pipeline `asyncio.gather(return_exceptions=True)` → degrades to zero-news, no crash.
- **Secrets:** zero matches; `.env.example` E7 placeholders only; no `win_rate`.

## E9 — Alpha-caller track-record scoring (ADDED)

- **Net-new, source-confirmed:** `aats/sentiment/caller_score.py` (380 lines). Referenced only by itself
  + its test — standalone, integration deferred to T-401 clean-room harness (correctly flagged).
- **Selectivity-only:** `mcs_delta_contribution <= 0` always; `selectivity_weight` in [0,1] (never raises
  conviction); `assert_caller_signal_cannot_raise_conviction` enforces `adjusted <= base` and is LIVE
  (a hand-forged `mcs_delta=+0.3` raises AssertionError). Always-wrong / thin-data callers → weight 0.
- **Slow-loop only:** imports only logging/math/dataclasses/typing — no asyncio / RPC / LLM / disk /
  network. Offline-proven (blocked socket, ran end-to-end).
- **Point-in-time / leak-free:** `get_outcomes` filters `outcome_event_time_ms <= decision_time_ms` BEFORE
  aggregation; differential test proves identical score with/without a future outcome record. 50 perfect
  future outcomes leave the score byte-identical.
- **Honesty clause:** NO `win_rate` field/attribute/computation (grep-confirmed across `aats/sentiment`);
  the metric is caller accuracy delta vs the universe prior.
- **Mutation-meaningful (3/3 mutants KILLED):** positive-delta = 10 RED; remove point-in-time filter = 3
  RED; net-negative callers given weight 1.0 = 6 RED; source restored byte-identical.
- **MINOR (non-blocking):** unused `from dataclasses import field` (`caller_score.py:51`). ParquetCallerOutcomeStore
  + pipeline integration deferred to T-401 — re-validate the `<=0` contribution when wired.

## E10 — Social-velocity + bot-ratio features (ADDED)

- **Net-new, source-confirmed:** `aats/sentiment/velocity.py` (VelocitySignalComputer + Protocol +
  InMemoryVelocitySource + 3 penalty components). ZERO importers outside the module — ships **dormant**
  (not yet wired into the pipeline/Reasoner).
- **De-risk-only:** `mcs_penalty >= 0` always (subtractive); coordinated/bot/factory growth only RAISES
  the penalty (lowers conviction). 2000-trial fuzz: conviction never raised over base; mutation-proven
  (neuter bot-velocity → 1 RED; negate total penalty → 14 RED).
- **Slow-loop only:** declared + structurally enforced (no FAST-path coupling). No live network/LLM/disk.
- **Point-in-time:** PIT filter is the FIRST list op; future events excluded + counted; 30 future
  good-news events leave the bot penalty pinned at 1.0.
- **Injection-safe:** ingests only metadata (no raw text) — no LLM-steerable path.
- **Secrets:** zero; `.env.example` E10 placeholders only; no `win_rate`.
- **DEF-E10-01 (MAJOR, engineer's lane, NON-BLOCKING for this dormant unit):**
  `apply_velocity_penalty_to_conviction` enforces de-risk-only via a bare `assert`; under `python -O`
  assertions are stripped and a forged negative `mcs_penalty` could raise conviction. NOT reachable via
  `compute()` (every component clamped `>= 0`) and the module is un-wired, hence MAJOR not BLOCKER.
  **MUST be fixed (clamp without assert) BEFORE E10 feeds the live MCS at G4 (wiring task T-313).**
- **MINOR (non-blocking):** F1 fabricated `tier_a.py AGE_FULL_PENALTY_DAYS` cross-reference comment; F2
  docstring documents a non-existent `text_hash` field; F3 `account_age_days` validated only in the
  builder, not `__post_init__` (direct construction of a negative age still de-risk-direction).

---

## Cross-cutting HARD RULES — upheld for every Wave-3 signal

- **De-risk / selectivity-only:** every signal can ONLY lower conviction / force-exit / veto / reduce.
  Raising conviction, sizing up, widening a stop, or producing a buy trigger is **inexpressible by type**
  (NewsSignal/CallerSignal/VelocitySignal are strictly non-positive; ReasoningAction has no risk-increase
  member; the DeRiskIntentFactory has no entry method). Confirmed by mutation tests on E6/E7/E9/E10.
- **Slow-loop only:** `aats.sentiment` + `aats.reasoning` are imported by NO FAST / snipe / execution /
  controller module (grep-confirmed). One Tier-B LLM call per asset; none on the hot path.
- **Point-in-time / leak-free:** event-time-only join; shift-back-one-tick leak-proven on every signal;
  future records excluded as the first operation.
- **Injection-safe:** all ingested text is QUOTED UNTRUSTED DATA; injection attempts do not flip control
  flow or raise conviction (E6/E7); velocity/caller ingest only metadata/outcomes.
- **Safety primitives still fire:** `CircuitBreaker` (`risk/circuit_breaker.py:267`), `DeadMansSwitch`
  (`risk/deadman.py:98`), `SurvivableStopCoordinator` (`risk/survivable_stop_coordinator.py:41`) present
  and UNTOUCHED by Wave 3.
- **Real capital DRY-RUN-disabled:** `.env.example` `DRY_RUN_ENABLED=true` default (line 27); mainnet LIVE
  hard-gated; devnet = worthless SOL. No secrets in any changed file (placeholders only); no `win_rate`
  field anywhere; money int/Decimal-as-string preserved; `aats/contracts` + `docker-compose.yml` not edited.

---

## Consolidated suite

**GREEN — 2245 passed, 2 skipped in 111.32s** (PYTHONHASHSEED=0, `__pycache__` purged,
`-p no:cacheprovider`, `--tb=no`). Wave 2 + Wave 3 + E1 re-fix all green.
`tests/execution` = **201 passed, 2 skipped** on every run across 6 PYTHONHASHSEED values
(0,1,42,1337,99991,7) — the E1 env-leak flake is GONE (fully deterministic). The 2 skips are the
allowed solders-gated `test_tx_builder.py:161/:186`. `failingFiles` empty.

**Scope note (what was NOT re-validated this run):** this was a green-and-stable + source-conformance
confirmation. It did NOT re-audit Wave-2/3/E1 source for full point-in-time / cost-net edge correctness
(shift-back leak audit on data, purge/embargo, SimulationVenue net-of-cost burn-in, edge-vs-baseline gate)
— those remain G4 deliverables and the edge stays `UNPROVEN-NO-REAL-DATA`.

---

## Verdict

**WAVE 3 → PASS. 4/4 items ADDED (dual G3). E1 re-fix → PASS (BLOCKER FIXED, dual G3).**

Carry-forward (NON-BLOCKING, routed to owners):
- **DEF-E10-01 (MAJOR):** replace the bare `assert` de-risk guard with a clamp before E10 is wired into
  the live MCS — `llm-reasoning-engineer` / wiring task T-313, must clear before G4.
- **MINOR doc/cleanup:** E6 F1/F2 (offline-seam docstring + `DISCORD_ALLOWLIST`→`DISCORD_CHANNEL_ALLOWLIST`
  comment), E9 unused `field` import, E10 F1/F2/F3 docstring/validation — owning engineers, next pass.
- **Deploy-time:** Discord snowflake→account-age formula validation; live-LLM injection contract test;
  Parquet caller-outcome store + pipeline wiring of E9/E10 (re-validate `<=0` contribution at G4).

**Next:** Enhancement **Wave 4** = E3 (candidate-queue) · E11 (wallet-cluster map) · the deferred E7
Narrative-and-News dashboard page; then a final security / deploy / consolidated-suite re-verification of
E1 / E4 / E5. Real capital stays DRY-RUN-disabled.
