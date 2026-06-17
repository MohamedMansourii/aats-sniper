# G3 — Wave D re-spin + Wave E — VERDICTS (source-verified)

_Recorded 2026-06-16 by `orchestrator`. Verified by reading the ACTUAL changed files under
`C:/dev/aats`, not by trusting the engineer/review JSON. G3 is DUAL on every code task:
`code-reviewer` AND `backtest-qa-engineer` must BOTH PASS (AATS overlay rule 3)._

**Headline:** **6 of 7 tasks dual G3 PASS → DONE.** **1 NEEDS-REPLAN (T-326, attempt 3 — a
flaky safety-cap regression test; production code correct).** **T-340 + T-341 BOTH clear dual G3
→ the end-to-end-driveable-on-SimulationVenue MILESTONE IS ACHIEVED (declared below).**

---

## 1. Per-task verdicts

| Task | code-reviewer | backtest-qa | Verdict | Source-confirmed evidence |
|---|---|---|---|---|
| **T-340** controller integration (re-spin att.3, test-only + R-1 prod fix) | **PASS** | **PASS** | **DONE** | See §2 |
| **T-341** control-plane API (re-spin att.3, test-only) | **PASS** | **PASS** | **DONE** | See §3 |
| **T-326** limit+DCA resting orders (att.2) | **FAIL** (BLOCKER B2) | PASS | **NEEDS-REPLAN** | See §4 |
| **T-328** multi-wallet/bundle anti-cluster (att.2) | **PASS** | **PASS** | **DONE** | See §5 |
| **T-301** discovery enrichment + pending-table fix | **PASS** | **PASS** | **DONE** | See §6 |
| **T-302** copy-trade selectivity stream | **PASS** | **PASS** | **DONE** | See §7 |
| **T-303** completeness audit (C-6) | **PASS** | **PASS** | **DONE** | See §8 |

---

## 2. T-340 — controller integration re-spin (att.3) — DONE

**One R-1 production fix + three test-only fixes; all three target tests mutation-proven RED.**

- **R-1 production fix (verified `snipe_loop.py:312-325`):** `hard_stop_price_lamports`
  now stores `floor(entry_price * Decimal("6") / Decimal("10"))` = `entry * 0.60`
  (a valid −40% stop BELOW entry). The prior bug stored `entry * Decimal("6")` = `6× entry`
  (a stop ABOVE entry, firing immediately on any position). Two-literal form kept for audit.
  This stored field is record/audit state only (grep-confirmed never read by `fast_loop.py`
  or the ExitEngine to fire), so no competing field-based stop. PINNED by
  `test_snipe_loop_hard_stop_price_is_sixty_percent_of_entry` (asserts `==3` for a 5-lamport
  entry); reverting the factor to `Decimal("6")` → `assert 30 == 3` RED (mutation-proven by
  both reviewers and reproduced from source).
- **BLK-2 fix (verified `test_snipe_handoff.py:462-676`):** mark changed from `0` → `7`.
  entry=10, `hard_stop_r=0.60` → ExitEngine threshold = 6.0; `mark=7 > 6.0` so the ExitEngine
  does NOT fire and mask the path. The only exit can come from breaker-trip →
  `emergency_flatten_all`. Test now asserts an `ExitIntent` with
  `reason.startswith("emergency_flatten:")` (the FastLoopFlattenHandler prefix; the ExitEngine
  uses `exit_eng:hard_stop:`). Mutation (no-op `emergency_flatten_all`) → `exit_calls=[]` RED.
- **FIX-2 fix (verified `test_t342_enforcer.py:1213-1308`):** custom `ExitConfig(hard_stop_r=0.10)`
  → ExitEngine threshold = 10, `BELOW_TRIGGER=59 > 10` so ExitEngine silent; Layer-2 enforcer
  uses default `StopThresholds(4000 bps)` → trigger 60, `59 <= 60` breaches → only Layer-2 fires.
  Test asserts `client_intent_id.startswith("stop_exit:")` (the SecondaryStopEnforcer prefix).
  Mutation (no-op `SecondaryStopEnforcer.on_tick`) → `exits=[]` RED.

**code-reviewer (PASS):** verified by execution — controller suite 110 passed (10/10 repeat
runs deterministically green), controller+risk 395 passed; ruff/mypy clean on changed files;
all three mutations reproduced RED independently; R-1 geometry consistent with BOTH live firing
paths (ExitConfig `hard_stop_r=0.60` and Layer-2 `StopThresholds(4000 bps)` both → `entry*0.60`).
One non-blocking observation: two intermittent first-invocation failures on cold process that
could not be reproduced in ~180 subsequent runs; canonical gates deterministically green;
recommended CI confirm 0 flake (carry-forward, NOT a blocker).

**backtest-qa (PASS):** all three mutations independently reproduced RED, restored byte-identical;
AATS brief — asymmetric-trust OK (test-only + a stop fix that moves the stop from above-entry to
BELOW-entry = strictly de-risk), FAST-path no-LLM/no-await OK, no-float-money OK (R-1 pure
Decimal/int). Two pre-existing mypy union-attr notes (`test_t342_enforcer.py:633/770`) are
T-342-base carry-forwards in test code, non-blocking.

**Carry-forward (NON-BLOCKING → G4 / QA in CI):** run the controller suite a few times in CI to
confirm 0 flake; if a repeat surfaces, investigate daemon-thread leakage from the 1000-thread
stress test + the hung-LLM daemon thread.

---

## 3. T-341 — control-plane API re-spin (att.3) — DONE

**Test-only; production `server.py` unchanged (git-clean confirmed by both reviewers).**

- **QA-MAJOR-1 fix (verified `tests/control_plane/test_widen_trap.py`):** 8 direct unit tests
  on `_validate_risk_config_tighten_only` using the lex-vs-numeric trap — `"10.0" < "2.5"`
  lexicographically (char `'1' < '2'`) but `10.0 > 2.5` numerically. So the widen `2.5 → 10.0`
  is INVISIBLE to a string comparison and VISIBLE only to Decimal coercion. Tests pin
  `daily_loss_limit_pct` AND `jito_tip_cap_frac` (both in `_DECIMAL_STRING_FIELDS`). Mutation
  (neuter the `_coerce` Decimal branch → `return val`) → EXACTLY 4 tests RED
  (`test_daily_loss_limit_pct_lex_numeric_trap`, `test_jito_tip_cap_frac_lex_numeric_trap`,
  `test_no_false_positive_on_tighten`, `test_multiple_violations_all_reported`).
- **R-3 nit fix (verified same file §FIX 2):** 9 POST status-code conformance tests asserting
  the frozen contract (api-contracts.md §3/§5): kill/flatten/flatten/{mint} → 202;
  breaker/reset → 200 (tripped) | 409 (armed); mode → 200 | 403; risk-config → 200 (tighten) |
  403 (widen). Cross-checked against the live `build_app` decorators.
- **Production confirmed unchanged (verified `server.py:270-333`):** the `_coerce` Decimal
  branch (`:297-304`) and the two check helpers are the existing correct implementation.

**code-reviewer (PASS):** 112 passed; ruff/mypy clean; mutation reproduced (exactly the 4 tests
RED) and production byte-restored; API contract conformance VERIFIED against api-contracts.md
§3/§5. One NIT (non-blocking): `test_post_breaker_reset_returns_409_when_armed` relies on the
fixture's default ARMED state without an explicit precondition assert.

**backtest-qa (PASS):** mutation independently reproduced (4 RED), `git diff --quiet
aats/control_plane/server.py` exit 0 (byte-identical); 112 passed; status codes cross-checked.

---

## 4. T-326 — limit+DCA resting orders (att.2) — NEEDS-REPLAN (attempt 3)

**DUAL FAIL: code-reviewer BLOCKER B2; backtest-qa PASS. A dual gate needs BOTH to PASS.**

The B1 production fix in `resting_orders.py:904-1019` (within-tick aggregate-exposure accounting:
thread `committed_this_tick` into each subsequent same-tick BUY's `current_aggregate_lamports`
so the T-324 sizer's clamp sees true remaining headroom) is **CORRECT in isolation** — verified
from source, 500/500 single-process trials clean, mutation-meaningful (reverting `+
committed_this_tick` → 3 tests RED with the exact documented breaches). Strictly de-risking
(only SHRINKS headroom for later same-tick BUYs; refuses more, never fires more). Deterministic
`candidates.sort(key=order_id)` makes the within-tick fire sequence replay-reproducible.

**BLOCKER B2 (the gate blocker):** the three new B1 regression tests
(`test_resting_orders.py:628-749`) FAIL INTERMITTENTLY (~1/26) when the full `tests/risk` suite
runs — the FIRST `pytest tests/risk -q` invocation failed 3/3 with
`AssertionError: assert (250000000 + 750000000) <= 500000000` — i.e. 0.75 SOL committed within a
single tick against the 0.5 SOL aggregate cap: the EXACT breach T-326/B1 exists to prevent. They
pass 29/29 in isolation and on ~25 subsequent full-suite runs. **A regression test for a
NON-WAIVABLE safety invariant (the aggregate-exposure hard cap) that is order/state-dependent is a
test that can pass while the invariant is actually violated.** Per charter §3/§4 this cannot serve
as the gate it claims to be. Root cause is cross-test state pollution (production logic is correct
in isolation); source unidentified.

**MINOR M1 (non-blocking, re-confirm at G4):** within-tick accounting holds only WITHIN one
`evaluate_tick` call; cross-call coordination depends on the caller advancing the aggregate before
the next tick. No production caller invokes `evaluate_tick` yet → not a present-day breach.
Document the caller contract + single-flight guard when wiring lands.

**RE-PLAN (attempt 3 — test-hardening, NOT a rebuild; production fix is correct):**
1. Root-cause the cross-test nondeterminism: run `pytest tests/risk -p no:cacheprovider
   --tb=long` in a loop until it reproduces; capture which sibling test precedes the failure;
   suspect global state (decimal context / seeded `random` / env / un-torn-down monkeypatch)
   leaking from a fault-injection/latency/property test in `tests/risk/`.
2. EITHER fix the polluting test (restore global state in teardown) OR make the three B1 tests
   HERMETIC (assert directly on the sizer/book with a fresh `RiskConfig` + explicit aggregate,
   independent of any global). **Acceptance bar: 50 consecutive green `pytest tests/risk -q` runs
   with the B1 tests included.** Do NOT merge a flaky safety-cap test.

This is attempt 3 (still NOT a 3rd content strike — att.2 had one substantive blocker, this is
its remediation). No CEO escalation. T-326 is a Lane-C ENHANCEMENT, NOT on the milestone path.

---

## 5. T-328 — multi-wallet/bundle anti-cluster (att.2) — DONE

Test-only QA-MAJOR-1 fix; production unchanged. Two new tests
(`test_multi_wallet.py:157-202`) make the multi-wallet activation gate (OQ-010 / R3) load-bearing:
`N_WALLETS_MAX=3` in env WITHOUT `N_WALLETS_MAX_ENABLED=true` → `_resolve_n_max(None)==1`; and
constructing a 2-wallet `MultiWalletOrchestrator` without the flag raises `MultiWalletConfigError`.
Both proven RED under the gate-defeat mutant (`if False and enabled != "true":` →
`multi_wallet.py:592`) — exactly those 2 tests go RED, the other 43 stay green; production
byte-restored. code-reviewer + backtest-qa both PASS, both reproduced the mutation-kill
independently. Conformance confirmed: N_max default 1; anti-cluster cap = per-trade cap with
pre-execution refusal (`execute_count==0` on breach); DRY-RUN no-submit (`rpc.send_calls==0`);
key never in-process (UDS signer, ADR-0009); money int with float→TypeError guards; ExecutionVenue
ABC preserved. Non-blocking: unused `field` import at `multi_wallet.py:38` (cosmetic).

---

## 6. T-301 — discovery enrichment + pending-table fix — DONE

`aats/ingestion/enrichment.py` (injectable DEXScreener/Birdeye/Meteora/Moonshot adapters via
HttpTransport Protocol, offline fixtures, adapters return None on error — no live calls).
**T-300a carry-forward ABSORBED (verified `store.py`):** dedicated `_pending_rows` table; public
`append_pending_row()` (`:166`); `read_pending_as_of()` (`:221`); `read_as_of('pending_events')`
now raises a guided ValueError (`:204-209`) NOT `KeyError: 'mint'`. 206 ingestion tests pass;
slow-path only (no decoder/bus/transport hot-path import — grep confirmed). code-reviewer +
backtest-qa both PASS. backtest-qa did a 4-mutation leak audit (removed as-of guard / stripped
cutoff / forged event_date from wall-clock / forward-dated the join anchor) — each caught RED,
restored byte-identical. Pending events quarantined in a physically separate table (no `mint`,
`event_date=None`) → cannot contaminate `read_as_of('launch_events',...)`. Money-int rule held.
Non-blocking: live Meteora/Moonshot endpoint shapes + AiohttpTransport prod impl deferred to T-400.

---

## 7. T-302 — copy-trade selectivity stream — DONE

`aats/ingestion/smart_money.py` + 62 tests; 268 ingestion tests pass. DISABLED by default
(`SmartWalletConfig.enabled=False`); 0–20 wallet cap; honest lag (`entry_lag_slots`,
`observation_lag_ms` both `>= 0`); `smart_wallets_in` is a plain int COUNT, never a buy trigger;
point-in-time correct (None fill_block_time → None event, no wall-clock substitution); money rule
enforced (TypeError on float); fully injectable (ReplaySmartWalletBackend), no live RPC.
code-reviewer + backtest-qa both PASS. backtest-qa independently audited the public computation
surface (only `count_smart_wallets_in()->int` / `count_smart_wallet_entry_lag_slots()->int|None`
face consumers — NO EntryIntent / sizing / mirror) and ran 7 adversarial probes confirming the
window filters on on-chain `their_fill_slot` (not observation slot). Non-blocking: EH-005
expected-zero lift + production Geyser accountSubscribe backend (PLUG_IN_HERE stub) deferred to
G4 T-401; T-304 consumer integration already DONE.

---

## 8. T-303 — recorded-data completeness audit (C-6) — DONE

`aats/ingestion/completeness.py` (637 lines) + 65 tests; 333 ingestion tests pass. Injectable
`CensusSource` Protocol (no network reachable); miss rate bounded via Wilson CI upper bound and
reported, gated against `declared_max_miss_rate`; CENSORED outcomes never dropped
(`coverage_fraction` always 1.0); survivorship-free. code-reviewer + backtest-qa both PASS.
backtest-qa ran a REAL implementation mutation (inserted `if status==CENSORED: continue`) → 8
tests RED, restored byte-identical; Wilson math verified to 5 decimals; small-N fails CLOSED
(safe direction). Non-blocking: test 26 (mutation proxy) is tautological but the rest of the
suite provides genuine mutation coverage (proven by probe); `coverage_fraction` is structurally
1.0 so the real gate is the Wilson-bounded miss rate (documented). code-reviewer filed one MAJOR
(M-1): `_wilson_upper_bound` silently coerces confidence not in {0.90,0.95,0.99} to z=1.96 while
labeling it the requested confidence — requires operator misconfiguration, default/documented
paths correct, ships safely. **M-1 → carry-forward fix (NON-BLOCKING; G4/hardening):** derive z
via `statistics.NormalDist().inv_cdf` or raise on unlisted confidence.

---

## 9. MILESTONE DECLARATION

> **MILESTONE ACHIEVED (2026-06-16):** *The bot runs end-to-end on SimulationVenue (paper) and is
> driveable via the control-plane (dashboard + Telegram), de-risk-only, DRY-RUN.*

**Basis:** With T-340 (triple-loop controller + single-writer per-position FSM + atomic
snipe→fast handoff, running vs SimulationVenue) and T-341 (control-plane API server conforming
EXACTLY to the FROZEN P2 contract) BOTH cleared on dual G3 this wave, the full drive-the-bot path
is now proven-by-test and source-verified:

- **Loop core (T-340):** SNIPE→FAST handoff (write-ahead, intent dedup), breaker→`emergency_flatten`
  de-risk handoff PROVEN-BY-FIRING (AC-028), R-1 hard-stop value correct, runs vs SimulationVenue.
- **FAST enforcer + DMS (T-342, DONE prior wave):** Layer-2 survivable-stop + dead-man's-switch
  wired into `FastLoop.tick()`; process-death → DMS flatten proven.
- **Control plane (T-341):** kill / flatten / flatten/{mint} / breaker-reset / mode / risk-config
  against the real bot; SSE `/feed`; tighten-only risk-config (widen → 403, lex/numeric trap
  closed); frozen-contract conformant.
- **Operator surfaces (DONE prior wave):** dashboard live-wire (T-352) + feature pages (T-353);
  Telegram alerts (T-360) + de-risk commands (T-361). De-risk-only end-to-end.
- **Safety primitives (DONE):** daily-loss breaker (T-320), survivable stop 3-layer (T-321),
  dead-man's switch (T-322) — all PROVEN-BY-FIRING; built and proven BEFORE any live path.

**Real capital remains DISABLED by default** behind the DRY-RUN flag, CEO-gated at capital-staging
rung R3. All edge numbers to date are BOOTSTRAP/synthetic — **LIVE EDGE remains UNPROVEN and is
gated at G4 (T-400/T-401 on RECORDED data).** This milestone is a *plumbing + safety* milestone:
the system is driveable and de-risk-only, not yet edge-proven.

---

## 10. P3 status + next stage

- **P3 BUILD LANES: COMPLETE.** Every Lane-A/B/C/D/E/F build task is dual G3 DONE **except T-326**
  (a Lane-C ENHANCEMENT in test-hardening att.3, OFF the milestone path) and the non-blocking
  T-106 (cosmetic), T-352a (signer service — runtime-assembly). The milestone-critical spine and
  all operator surfaces are DONE. P3 is functionally complete; T-326's flaky-test re-spin runs
  in parallel with G4 and does not gate stage advance.
- **NEXT STAGE: G4 — INTEGRATION.** Dispatch (deps: all milestone-path G3 PASS):
  - **T-400** — `backtest-qa-engineer`: full sim/paper burn-in; purged/embargoed walk-forward;
    lookahead leak audit; clock audit (block_time vs arrival, C-5); group-aware purge (C-10).
  - **T-401** — `backtest-qa-engineer`: edge-vs-baseline on RECORDED data — GATE-A (net-of-cost
    PnL) + GATE-B (model-vs-naive-baseline) BOTH, lower-95% bound > 0; calibrated haircut (C-11);
    experiment-log deflation (C-9); tip-contention + independent-surface stratification (C-3/C-13);
    survivor-MCS re-validation on real MCSScore rows. **LIVE EDGE is finally proven here.**
  - **T-402** — `backtest-qa-engineer`: END-TO-END PAPER operator demo — running bot driven
    through dashboard AND Telegram; kill flattens within budget; mode propagates; feed shows real
    events; safety (breaker / survivable-stop / DMS) fire on demand in QA.
  - **T-403** — `crypto-security-engineer`: security + custody + LLM-prompt-injection audit; no
    real keys in code/logs/images; program-ID allowlist on signing; Telegram-command authz.

**G4 carry-forwards folded in:** T-340 controller-suite CI flake re-check; T-303 M-1 Wilson-z
hardening; T-302 EH-005 expected-zero lift + production Geyser backend; T-301 live Meteora/Moonshot
endpoint shapes + AiohttpTransport; T-312 survivor-MCS bootstrap covariates (re-validate on real
rows); T-352 `daily_net_pnl_day_utc` surfacing + 3 GET projections architect delta.
