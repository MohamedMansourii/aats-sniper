# Enhancement Wave 2 — Verification Record (E2 · E8 · E13 · E12 + 4 audits) + E1 re-fix

_Recorded: 2026-06-17 by `orchestrator`. Verified by reading the ACTUAL changed files under
`C:/dev/aats` (not trusting handoff/review JSON), cross-checked against the dual-G3 reviews,
the AATS review brief (ROSTER §5), and the safety contract. G3 is DUAL on every code task:
`code-reviewer` AND `backtest-qa-engineer` must both PASS (overlay rule 3). Source directive:
`.agency/00-brief/ENHANCEMENT-DIRECTIVE-E1-E13.md` §WAVE 2 (AUDIT-FIRST, ADDITIVE)._

## Verdict summary

| Item | Title | Verdict | G3 | Evidence (file:line) |
|---|---|---|---|---|
| **E2** | Creator/token denylist pre-filter wired into live BUY entry path | **ADDED** | dual G3 PASS (`code-reviewer` + `backtest-qa`) | `aats/risk/resting_orders.py` STEP-0 `:557-577`; `BuyFireRejection.DENYLISTED :214`; `_DenylistLike` Protocol `:432` |
| **E8** | Tunable discovery / SCREENER filter (late-entry / survivor niche) | **COVERED** | dual G3 PASS (verify-not-patch) | `aats/risk/screener.py` (586 lines, pre-existing); `tests/risk/test_screener.py` (32); `.env.example` E8 block `291-330` |
| **E13** | Anti-FOMO / already-pumped exclusion filter | **ADDED** | dual G3 PASS (`code-reviewer` + `backtest-qa`) | `aats/risk/anti_fomo.py` clamp `:439-447`; `tests/risk/test_anti_fomo.py` (33); `aats/risk/__init__.py`; `.env.example` E13 block |
| **E12** | Time-stop / stale-narrative exit | **ADDED** | dual G3 PASS (`code-reviewer` + `backtest-qa`) | `aats/risk/exit_engine.py` `STALE_NARRATIVE_TIME_STOP` (flat-clock `:829-832`); `tests/risk/test_exit_engine_time_stop.py` (22) |
| **AUDIT-ratchet** | Stepped profit-lock RATCHET (breakeven@2x, lock 2x@3x) | **ADDED** | dual G3 PASS | `aats/risk/exit_engine.py` `RATCHET_STOP :865-874` + `_advance_ratchet :827`; `tests/risk/test_exit_engine_ratchet.py` (18) |
| **AUDIT-micropreset** | Named MICRO early-entry preset (verified + LP≥30d + honeypot + top-5<20% + 0.5% + 18% stop/24h) | **ADDED** | dual G3 PASS | `aats/risk/presets.py`; `tests/risk/test_micropreset.py` (37); `aats/risk/__init__.py`; `.env.example` |
| **AUDIT-liquidity** | Pre-trade liquidity-sanity gate (24h-vol≥10x notional + x*y=k slippage sim) | **ADDED** | dual G3 PASS | `aats/risk/liquidity_sanity.py`; `tests/risk/test_liquidity_sanity.py` (31); `aats/risk/__init__.py` |
| **AUDIT-risktiers** | Soft ~2% daily-loss REDUCE/PAUSE tier + GATE-B minimum-sample guard | **ADDED** | dual G3 PASS | `aats/risk/circuit_breaker.py` soft tier (`DEFAULT_SOFT_RATIO :90`, `risk_posture :407`, `is_soft_reduced :418`); `aats/models/gate_b.py` min-sample guard `:37+` |
| **E1** | Devnet live-send validation mode (re-fix) | **STILL FAILED — NEEDS-REPLAN** | dual FAIL (re-fix engineer DIED again) | BLOCKER live: `aats/execution/jito_jupiter_venue.py:766-772/943/977` |

**Wave 2 outcome:** all 8 enhancement/audit items CLEARED dual G3 — **1 COVERED (E8), 7 ADDED.**
Architect E1 frozen-contract delta (ADR-0013 `SubmitMode.DEVNET`) reviewed and ACCEPTED.
**E1 implementation re-fix DIED again → remains NEEDS-REPLAN** (process event, NOT a content strike).

---

## Hard-rule conformance (verified per filter, from source)

Every Wave-2 filter was confirmed against the four non-negotiables before issuing a verdict:

1. **DE-RISK / SELECTIVITY ONLY (never a buy trigger, never raises risk):**
   - **E2** — `_DenylistLike.check_keys` (`resting_orders.py:432-444`) can only return a veto-hit or
     `None`; STEP-0 short-circuits to `REJECT(DENYLISTED)` with the EntryIntent builder never reached.
     The seam exposes no size/buy/trigger/widen method. Structurally a veto.
   - **E8** — `ScreenVerdict ∈ {PASS, REJECT}` only; a PASS is silent; no size/intent/stop/order field
     (`screener.py:15-20`). VOLUME_SPIKE is a contrarian INFO tag, never a buy.
   - **E13** — `FomoVerdict ∈ {PASS, DOWN_WEIGHT, REJECT}`; `conviction_multiplier` hard-clamped to
     `[0,1]` (`anti_fomo.py:439`), `>1` raises `ValueError`, REJECT→0, PASS→1. Raising conviction is
     type-inexpressible. Mainstream/CEX/Forbes mention is an EXCLUSION, never validation.
   - **E12 / ratchet** — sole outcome is a full `ExitIntent` (`remaining_bps` only shrinks; `_replace`
     refuses any increase / any `locked_floor_r` decrease); narrative score & ratchet can only force an
     exit, never size up, widen a stop, or extend a hold. `DeRiskIntentFactory` has no `entry()`.
   - **micropreset** — only TIGHTENS base thresholds (top-5<20%, LP=100%+≥30d); 0.5% sizing is a
     CEILING-shrink hard-clamped by the existing per-trade + aggregate caps; can only REJECT/SHRINK/EXIT.
   - **liquidity** — VETO only: PASS (silent) or REJECT; tighter threshold rejects MORE; constructs no
     Intent, sizes nothing, sets no stop.
   - **risktiers** — soft tier only PAUSES entries (no flatten, no latch); GATE-B guard only WITHHOLDS a
     positive bound on too-few trades — `gate_b_pass = AND(lower_95>0, n≥min_sample)` can never
     MANUFACTURE a pass (a losing 60-trade model still fails).

2. **SLOW-LOOP ONLY (off the FAST/snipe hot path), where applicable:**
   - E8, E13 are SLOW-loop screens (no controller/fast/snipe importer — grep-confirmed); E12/ratchet
     run on the exit engine off the snipe path. **Note:** E2 (membership O(1)) and `liquidity_sanity`
     (pure int/Decimal x*y=k sim, 0 RPC/LLM) are CORRECTLY placed on the FAST pre-trade path as VETOes —
     a denylist hit / thin-pool reject must fire before a buy lands; both are de-risk-only and add no
     social/news signal to the hot path.

3. **POINT-IN-TIME / no compute-time leak:** every filter reads only as-of/event-time inputs
   (`event_slot`/`event_block_time_ms`/`block_time_ms` deltas); no wall-clock; no forward/horizon field
   expressible. `backtest-qa` proved leak-freedom by shift-back-one-tick (E12/E13) and structural
   field-disjointness (E8/liquidity). Same code path live and backtest.

4. **SAFETY PRIMITIVES STILL FIRE:** the daily-loss circuit breaker (hard −3% / −0.30 SOL trip),
   survivable stop, and dead-man's switch are UNTOUCHED by Wave 2. Confirmed: the soft REDUCE/PAUSE
   tier is STRICTLY below the hard trip and a hard trip implies REDUCED; full `tests/risk` suite is
   green at each step (E2→344, E8→376, E13→409, E12→431, ratchet→449, micropreset→486, liquidity→517,
   risktiers→529). Breaker / survivable-stop / DMS suites green throughout.

5. **REAL CAPITAL DRY-RUN-DISABLED:** no Wave-2 filter touches capital/signer/RPC; `DRY_RUN_ENABLED`
   default `true` unchanged; mainnet LIVE remains hard-gated by the 3 independent gates. Money is
   int lamports / Decimal (float rejected at boundaries). No secrets in any changed file
   (`.env.example` placeholders only). `aats/contracts/` and `docker-compose.yml` NOT edited by any
   Wave-2 item.

**Tests are mutation-meaningful (not vacuous):** `backtest-qa` independently broke each filter and the
suite went RED — E2 (denylist neutered → Scam111 fires a buy, 4/6 RED), E13 (5 mutations, all caught),
E12 (4 mutations, all caught), ratchet (5 mutations, all caught), micropreset (8 mutations, all caught),
liquidity (6 mutations, all caught), risktiers (soft-tier + min-sample mutations caught). Sources
restored byte-identical.

---

## E1 — Devnet live-send validation mode — re-fix **STILL FAILED**

**Verdict: NOT COVERED, NOT ADDED — NEEDS-REPLAN (attempt count NOT incremented).**
Re-fix dispatch input: `{"taskId":"E1","status":"FAILED","reason":"engineer died"}`.

The BLOCKER from Wave 1 remains LIVE in the tree (re-confirmed against source):
- `aats/execution/jito_jupiter_venue.py:766-772` — `_send_and_confirm_devnet` returns
  `submitted=True, land_slot=None` on a confirm timeout.
- `:943` — retry loop treats `submitted` as terminal success.
- `:977-986` — `reconcile()` emits `FillResult(landed=True, reason="filled")` for a tx that never
  confirmed (`land_slot=None`).
- `:336-337` — `execute()` latches `intent_id` into the idempotency set on `submitted`, blocking
  legitimate retry of the unconfirmed intent.
Plus the MAJOR hash-seed test flake (a DRY_RUN venue intermittently resolving LIVE via leaked
`os.environ`), zero coverage for the unconfirmed branch.

**The death is a PROCESS event, not the content strike (charter §3.4/§3.5):** no `code-reviewer` +
`backtest-qa` verdict was produced on a fix diff, so there is no content failure to count. E1 stays
at its current attempt count. Two consecutive re-fix engineers have now DIED before producing a
diff/verdict — this is an EXECUTION-RELIABILITY pattern, not a design dead-end.

**Architect contract delta — ACCEPTED.** `solana-systems-architect` issued ADR-0013
(`SubmitMode.DEVNET`) + execution-venue.md / infrastructure.md deltas: a REAL devnet submit path
that is **structurally incapable** of unlocking mainnet LIVE. Verified from
`adr/ADR-0013-devnet-submit-mode.md`: `DEVNET` is bound to `SOLANA_CLUSTER=devnet`, OUTSIDE the
capital-staging ladder, never narrows the mainnet gates (`submit_mode==LIVE` + `DRY_RUN_ENABLED=false`
+ CEO auth + funded-wallet refusal), and the `aats-signer` ADR-0009 caps still apply. Additive
(one enum member + one env selector), no existing contract narrowed. This is the CORRECT honest home
for the E1 submit path and gives the re-fix engineer a legal contract to build to — it does NOT itself
ship code or close the BLOCKER.

**Re-entry for E1 (unchanged + sharpened):** genuine FIX to `jito_jupiter_venue.py` (an unconfirmed
devnet tx must reconcile as NOT-landed / retryable, never `filled`; fix the hash-seed env leak) built
to ADR-0013, then dual G3. Owner `solana-execution-engineer`. **Mitigation for the death pattern:**
scope the dispatch to the single confirm/reconcile/idempotency seam + its tests only (smaller blast
radius, faster turn) and require the unconfirmed-branch test FIRST. Runs ∥ Wave 3 (disjoint module).

---

## Decision

**Wave 2 = DONE.** E8 COVERED; E2 · E13 · E12 · AUDIT-ratchet · AUDIT-micropreset · AUDIT-liquidity ·
AUDIT-risktiers all ADDED with dual G3 PASS. Safety primitives intact and firing; real capital
DRY-RUN-disabled; contracts + compose untouched. **E1 remains NEEDS-REPLAN** (re-fix engineer died;
not a content strike). **Next → Wave 3** (E6 Discord · E7 News/breaking-news · E9 alpha-caller scoring
· E10 social-velocity/bot-ratio) — slow-loop, adversarial-by-default, none a standalone buy trigger;
E1 re-fix runs in parallel.

**Runtime action required (orchestrator has no shell):** run the consolidated deterministic suite
`PYTHONHASHSEED=0 python -m pytest tests/ -q -p no:cacheprovider` (purge `__pycache__` first) to refresh
the recorded count with the +185 Wave-2 tests and confirm no cross-suite regression. Expect
`tests/execution/` instability to persist until the E1 BLOCKER is fixed.
