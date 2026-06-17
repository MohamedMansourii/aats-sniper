# G3 — Wave M2 (Lane B models + reasoner · Lane A MCS + C-5 fix · Lane C sell-sim/MEV) — PASS

**Gate:** G3 (per-task Build Gate, DUAL: `code-reviewer` **AND** `backtest-qa-engineer` must both PASS).
**Verdict:** **PASS — all 9 tasks DONE.**
**Verified by:** `orchestrator`, 2026-06-16, by reading the ACTUAL changed files under `C:/dev/aats`
(not trusting handoffs) and confirming each reviewer's execution/mutation evidence against source.
**Record of evidence:** the per-task results JSON (both reviewer verdicts) + the orchestrator's
independent source reads listed below.

---

## 1. Per-task verdicts

| Task | Title | code-reviewer | backtest-qa | Attempts | Verdict |
|---|---|---|---|---|---|
| **T-311** | FROZEN naive-momentum baseline (C-4 / GATE-B control) | PASS | PASS | 2 | **DONE** |
| **T-310** | FAST snipe classifier — leak-free labels + calibration + ONNX | PASS | PASS | 1 | **DONE** |
| **T-312** | Slow-loop survivor model + GATE-B model-vs-baseline monitor | PASS | PASS | 1 | **DONE** |
| **T-300a** | C-5 wall-clock compute-time leak fix (`_make_event_time`) | PASS | PASS | 1 | **DONE** |
| **T-306** | MCS adversarial two-tier sentiment (contrarian shill penalty) | PASS | PASS | 1 | **DONE** |
| **T-329** | Honeypot/sellability sell-sim primitive (DRY-RUN) | PASS | PASS | 1 | **DONE** |
| **T-330** | Per-hop latency ledger + edge-bounded dynamic tips + atomic buy-with-revert | PASS | PASS | 1 | **DONE** |
| **T-331** | MEV fast/secure private split exits + C-3 tip-contention logging | PASS | PASS | 1 | **DONE** |
| **T-313** | Schema-enforced de-risk-only Reasoner + asymmetric-trust clamp | PASS | PASS | 1 | **DONE** |

**Three-strikes:** only T-311 took 2 attempts (attempt 1 G3 found a hash-inconsistent committed config +
vacuous-proof; attempt 2 resolved both, dual G3 PASS). No task at or near the 3-strike line. No escalation.

---

## 2. Key evidence (source-confirmed by orchestrator)

### LEAK-FREE LABELS (T-310) — the whole game
- `aats/models/training.py:106` `join_labels_by_event_time` joins on `event_time_key = (slot, block_time_ms)`
  ONLY; `wall_clock_ms` is deliberately excluded (`training.py:77-83`). A feature with no matching label is
  DROPPED (CENSORED-equivalent), never defaulted.
- `aats/models/training.py:144` `assert_event_time_leq_decision` asserts EVERY row: feature event-time
  <= decision event-time (equal by join) AND label `resolution_event_time` strictly > decision (label
  horizon proof). Raises `LeakAuditError` on the first violation; empty join also raises.
- `aats/models/featureset.py:assert_no_label_taint` (the build guard) rejects any `truth_*`/`LABEL_FIELD_NAMES`
  column and asserts feature columns DISJOINT from label columns — runs BEFORE fit.
- Forward-only split (`time_forward_split`, `training.py:198`): event-time ordered, never shuffled.
- backtest-qa LOAD-BEARING proof: injecting the TRUE LABEL as a feature -> OOS AUC 1.0000 vs clean 0.7450;
  the clean edge is decisively NOT a leak. Shuffled-label control collapses to ~0.5.

### CALIBRATION (T-310)
- Isotonic/Platt calibrator fit ONLY on the held-out, time-forward calib slice (never train/test).
- OOS test-fold Brier 0.1436-0.1486, ECE 0.0216-0.0433 (within tol); reliability bins track the diagonal.
- ONNX parity max abs diff 2.76e-07 (< 1e-5). Latency p99 0.32ms (tree) / ~1.4ms (full path) < single-digit ms.
- Monotone NON-INCREASING constraints on de-risk features (`featureset.py:104-113`: holder_concentration,
  sell_tax, smart_wallets_in, entry-lag) — manufactured hype can only push probability DOWN; all 7 hashed
  params + 4 de-risk monotone constraints proven REAL in the trained tree (value-sweep, 0 violations).

### C-4 FROZEN BASELINE (T-311) — GATE-B control hash-consistent
- `aats/models/baseline.frozen.json`: `selection_percentile=60.0`, `frozen_hash=e1e7dc6c...a5e`; stored hash
  == live `canonical_params_hash(params)` (CONSISTENT=True). hash(70.0)=ec0d43e6 (mutation flips it).
- `baseline.py:canonical_params_hash` is layout/whitespace-independent; money fields (`min_volume_lamports` int,
  `reference_threshold_lamports` Decimal-as-string) reject float at both load + hash.
- Mutation-meaningful: changing any of the 7 hashed params after fit -> `test_committed_config_is_frozen_and_hash_matches`
  goes RED (`baseline_changed_after_fit`); a non-hashed key does NOT change the hash (no over-binding).
- File now version-controlled (`git ls-files` lists it); staged==worktree (tested bytes == tracked bytes).
- `BaselineSignal` carries NO price/size/decision field (locked decision 9); no win-rate.

### GATE-B MONITOR (T-312)
- `aats/models/gate_b.py`: headline delta = `model_net_pnl_per_unit_risk - baseline_net_pnl_per_unit_risk`
  on recorded `TradeOutcome` records; net-of-cost; per SOL-at-risk. NOT a win-rate (no win-rate field/target;
  asserted). A declined trade contributes 0 (costless skip).
- Pass bar = lower 95% bootstrap bound > 0 (point estimate alone never passes), seeded/deterministic.
  Empty set -> ValueError (fail-closed); model-loses -> delta<=0, gate FAIL.
- Telemetry sink injectable -> `aats_model_vs_baseline_delta_net_pnl_per_sol` gauge (AC-037, `metrics.py:255`).
- Survivor model leak-free by the same construction; emits calibrated P(survive) + uncertainty + ORDERED
  quantiles (p10<=p50<=p90), NEVER a point price; slow-loop-only (`assert_slow_loop_only`).
- **CARRIED (re-validate at T-400/401):** survivor MCS covariates are CONSTANT in the bootstrap corpus
  (`SyntheticRow` carries no `mcs`); MCS de-risk wiring is monotone-pinned but UNEXERCISED on data — must be
  re-validated when the harness supplies real `MCSScore` rows. Non-blocking on a bootstrap deliverable.

### C-5 CLOCK LEAK CLOSED (T-300a)
- `aats/ingestion/decoders.py:202` `_make_event_time` returns `None` when `block_time_unix_s` is None or <=0;
  wall-clock is NEVER substituted into the authoritative `block_time_ms` anchor. All 9 decoder `_try_*` paths
  guard `if event_time is None: return None`.
- `STALENESS_UNKNOWN = -1` sentinel (`decoders.py:199`); `_staleness_ms(None) -> -1`, never 0 (honest staleness).
- `store.write_pending_slot_event` writes `event_date=None`, `data_staleness_ms=-1`, dataset='pending_events';
  pending rows do not pollute the event-date partition.
- Defense-in-depth: `EventTime(block_time_ms=0)` raises (contract validator).
- backtest-qa LOAD-BEARING mutation: re-introducing the leak (`block_time_ms=wall_ms`, `_staleness_ms(None)=0`)
  turned 19/33 regression tests RED across all 9 decoders + router + migration path. Tests are not vacuous.
- **CARRIED to T-301 (MAJOR, non-blocking):** `store.read_as_of("pending_events", ...)` raises `KeyError: 'mint'`
  on the mixed-shape pending row (latent — public reads short-circuit on `dataset` first). Fix when the real
  Parquet pending table is wired (T-301). Plus MINOR `_parquet._rows` private-attr write -> public method.

### MCS CONTRARIAN (T-306)
- `aats/sentiment/tier_b.py`: `conviction = clamp(raw_score * (1 - penalty), 0, 1)` — multiplicative penalty
  from synchronicity + low account-age + cluster-concentration drives manufactured euphoria toward 0.
- ONE batched LLM call per asset per cadence (never per post) — verified (20 posts -> 1 call).
- Prompt injection: ingested text is QUOTED UNTRUSTED DATA wrapped in separator tokens; output parsed to a
  structured float, never executed. An OBEYED-injection probe still cannot raise exposure.
- Point-in-time filter is the FIRST op (future posts excluded; +1ms boundary); MCS bounded [0,1] by the
  frozen `MCSScore` validator (rejects 1.2/-0.1). No float-money, no size-up arithmetic on conviction.
- backtest-qa: organic conviction 0.529 vs shill 0.214 (delta -0.314, AC-010); zeroing penalty -> 3 adversarial
  tests RED (mutation-meaningful).
- **CARRIED (MINOR):** off-topic young-account posts inflate the age penalty (de-risk direction only, not a
  blocker); `models.py` docstrings wrongly say conviction [-1,1] (impl + validator are [0,1]) — doc fix, no ADR
  (architecture has no [-1,1] anywhere); add an equal-volume AC-010 control test.

### SELL-SIM (T-329)
- `aats/execution/sell_sim.py`: DRY-RUN by construction (`submit_mode=DRY_RUN`, no `send_transaction` path);
  refuse-by-default (any failure -> not-sellable); simulate before sign.
- Honeypot fingerprints: sim revert, zero output, sell-tax > threshold, signer refusal, infra error.
- Wired into T-323 seam: `SellSimVenue` satisfies `pretrade_gate.SellSimProbe`; no-probe still refuses by default.
- backtest-qa mutation campaign: 6 production branches (revert, high-tax, quote-failed, tax-direction,
  is_sellable short-circuit, signer-refused) all KILLED; the risk-gate de-risk wiring (RedFlag.NOT_SELLABLE)
  KILLED on G1/G2 mutants. Money int/Decimal, float rejected. Off the hot path (N+2).

### EDGE-BOUNDED TIPS (T-330)
- `aats/mev/tip_controller.py:120` `price_tip`: edge_ceiling = floor(0.30 × expected_edge) via exact Decimal;
  reads the LIVE injectable `TipStreamProtocol` floor point-in-time (never a hardcoded tip); returns
  `DO_NOT_SUBMIT` when `live_floor > edge_ceiling` (priced out), no edge, or no live floor.
- Logs tip-as-%-of-edge in bps (cardinal-sin bleed guard). backtest-qa mutation: bidding ceiling+1 or disabling
  the priced-out refusal -> 7-9 tests RED (one mutant emitted a 50,000bps bleed, caught).
- Latency ledger C-1 three-class split (INTERNAL compute / SUBMISSION RTT / LEADER_LAND), never summed;
  folding RTT into internal -> 4 tests RED. Atomic buy-with-revert: `BundleResult` forbids `landed_buy` without
  `LANDED` (no orphan). DRY-RUN default, triple-gated for LIVE. No inherited sim optimism (C-2): mev imports
  none of `_competitor_delay`/sandwich constants.

### MEV FAST/SECURE + C-3 (T-331)
- `aats/mev/split_exit.py`: ASYMMETRY invariant structural — `SplitExitPlan.__post_init__:469` refuses any
  plan where `modeled_sandwich_p_bps > fast_baseline_sandwich_p_bps`; routing/split can only REDUCE exposure.
  Every leg is private; legs sum exactly to the requested fraction (integer bps, no float drift).
- C-3 tip-contention logging: `TipContentionRecorder` stamps the LIVE tip floor + `ContentionBucket`
  (LOW/MEDIUM/HIGH/PRICED_OUT) per candidate at decision time into an append-only GATE-A sink; the PRICED_OUT
  (declined) cohort is recorded so the negative-selection spiral is visible. Point-in-time enforced at
  construction (`as_of_slot <= decision_slot`) + priced_out<->PRICED_OUT consistency guards.
- SECURE is default (OQ-008). backtest-qa mutations: asymmetry guard, lookahead guard, SECURE<FAST exposure gap
  all KILLED; SECURE wins even when handicapped with a worse base haircut (split drives the win, not rigged priors).
- **CARRIED (MINOR):** a redundant planner-level clamp + the leg-sum defensive guard lack direct negative tests
  (the live planner path is fuzz-proven correct); modeled sandwich priors are illustrative, real haircut
  re-modeled at G4/T-401 per C-2/EH-002.

### DE-RISK-ONLY REASONER (T-313)
- `aats/contracts/models.py:98` `ReasoningAction` has EXACTLY four members {HOLD, VETO_ENTRY, REDUCE_SIZE,
  FORCE_EXIT}; NO SIZE_UP/WIDEN_STOP/ADD_LEVERAGE/OVERRIDE_HARD_STOP (size-up inexpressible by TYPE;
  import-time static guard at `models.py:125-134` raises if a forbidden member is added).
- `aats/reasoning/clamp.py`: any raw risk-increase token (`SIZE_UP`/`BUY`/`WIDEN_STOP`/...) is dropped to HOLD
  with `risk_increase_clamped=True`; the clamp can only NARROW risk relative to the quant ceiling.
- `DeRiskIntentFactory` exposes only exit/reduce/veto (no entry method); the reasoner holds only this factory.
- Sub-200ms veto MEASURED p99 0.07-0.11ms; timeout returns a conservative VETO (never a silent pass). LLM never
  on the SNIPE/FAST path (instructor/openai lazy imports). Narrative is quoted untrusted data; an OBEYED-injection
  on an open position -> HOLD + no Intent (no exposure change). Point-in-time refuses mismatched DecisionSignal/MCS
  event-times. Contracts import-only (git diff --stat empty).
- backtest-qa mutation: inverting the clamp's stronger-of selection -> 4 property tests RED; schema rejects a
  smuggled `size_up=True` (extra=forbid) + out-of-range confidence + closed enum.

---

## 3. Cross-cutting hard-rules sweep (all 9 tasks)

- **Money discipline (data-models §0):** int lamports / int bps / Decimal-as-string throughout; float-money
  rejected at construction on every new model/record. The only floats are statistical model inputs /
  dimensionless ratios / probability-like fields — never money arithmetic. Confirmed.
- **No win-rate anywhere:** grep clean across all new modules; only honesty-clause docstrings. The acceptance
  metric is net-of-cost PnL + model-vs-baseline. Confirmed.
- **LLM may only de-risk:** by TYPE (ReasoningAction), by clamp, by MCS multiplicative penalty. Confirmed.
- **LLM off the FAST/SNIPE path:** lazy imports; SNIPE reads pre-staged KV. Confirmed.
- **Point-in-time correctness:** leak audits (T-310/T-312), C-5 fix (T-300a), PIT filters (T-306/T-330/T-331/T-313)
  — all mutation-proven. Confirmed.
- **Real capital DISABLED:** no submit path in any of these modules; DRY-RUN by construction / triple-gated.
  `aats/contracts/` untouched (import-only) on every task. Confirmed.
- **All numbers are BOOTSTRAP/synthetic** (`is_bootstrap_not_real=True`): they prove the pipelines are
  leak-free / calibrated / fast / contrarian / edge-bounded BY CONSTRUCTION; they do NOT establish LIVE EDGE.
  Live acceptance (GATE-A net-of-cost PnL + GATE-B on RECORDED data, purged+embargoed CPCV walk-forward) is the
  clean-room harness's job at **G4 (T-400/T-401)**. This boundary is correctly scoped, not a defect.

---

## 4. Conditions advanced this wave

- **C-1** latency honesty — three-class ledger separated (T-330).
- **C-2** no inherited optimism — mev imports no sim cost constants (T-330); modeled priors labeled NOT live (T-331).
- **C-3** tip-cohort stratification — `TipContentionRecorder` + PRICED_OUT cohort to GATE-A sink (T-331).
- **C-4** freeze + build baseline — frozen, hash-consistent, mutation-meaningful (T-311); model beats it OOS (T-310).
- **C-5** clock audit — wall-clock leak CLOSED at the source decoder (T-300a); full clock-audit still owned by T-400.

---

## 5. Verdict

**G3 Wave M2 = PASS. All 9 tasks DONE (dual code-reviewer + backtest-qa PASS, source-confirmed).**
Carry-forwards are tracked on the board (T-301 pending-table KeyError; survivor MCS re-validation at T-400/401;
T-306 doc fix + equal-volume control test; T-331 two MINOR coverage tests). None block G3 or the next wave.
The model-vs-naive-baseline acceptance metric (GATE-B) is BUILT and computable on recorded data; live edge
remains UNPROVEN and gated at G4. Lane D (controller) is now unblocked.
