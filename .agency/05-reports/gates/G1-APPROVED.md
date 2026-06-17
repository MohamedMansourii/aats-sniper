# G1 — ARCHITECTURE GATE — APPROVED (agency-autonomous)

**Gate:** G1 (Architecture) · **Verdict:** **APPROVED**
**Approver:** agency-autonomous per `.agency/AUTONOMY-DIRECTIVE.md` (G1 delegated; no CEO pause)
**Author:** `orchestrator` (gate review) on the `solana-systems-architect` blueprint (T-200..T-206)
**Date:** 2026-06-16
**Artifacts under review:** `.agency/02-architecture/{BLUEPRINT.md, api-contracts.md, data-models.md,
execution-venue.md, validation-harness.md, latency-budget.md, infrastructure.md, adr/ADR-0001..0010}`
+ reconciliation against `dashboard/src/lib/api.ts`.

---

## 1. Verdict summary

G1 PASSES. The blueprint is complete across stack, components, data models, the FROZEN API contract,
and infra topology. The control-plane contract reconciles **exactly** with the existing dashboard
client (no green-on-mock break). The `ExecutionVenue` seam is a faithful sol-sniper drop-in. All
thirteen EDGE-VERDICT conditions C-1..C-13 are structurally enforced (type / build guard / process
boundary / frozen artifact — not prose). Money is integer/Decimal everywhere on the wire and in
storage. The LLM is physically off the FAST/SNIPE critical path. Both `blocksG1` red-team items
(leak-proofness, custody) are resolved by construction via ADR-0009 / ADR-0010, and the contract is
declared frozen with the post-G1 change protocol (ADR + delta notice) in place.

---

## 2. G1 criteria checked (each against the artifact)

| # | Criterion | Evidence | Verdict |
|---|---|---|---|
| 1 | Blueprint complete: stack, components, data models, API contract, infra topology | BLUEPRINT §1 (FR→component traceability, no orphans), §2 (triple-loop topology + diagram), §4 (Rust/Python boundary), §5 (bus + service inventory); data-models.md (typed contracts); api-contracts.md (frozen wire); infrastructure.md (compose topology) | PASS |
| 2 | Every spec FR maps to a component and back | BLUEPRINT §1 forward table; reverse is SPEC §5. No orphan components, no orphan FRs (FR-001..FR-057) | PASS |
| 3 | Control-plane contract FROZEN and reconciled with dashboard client | api-contracts.md §12 canonical endpoint set + §13 reconciliation table; verified against `api.ts` `ENDPOINTS` (lines 50-65) — all 16 paths identical incl. `flatten/{mint}` posted as `${flatten}/${mint}` | PASS — see §3 |
| 4 | No green-on-mock break | api-contracts §9 + §6: `VITE_USE_MOCK=true` mock branch unchanged; only additive supersets + a Lane-E (T-352) transcription (AgentMode 4-value enum, snake_case→camelCase adapter, money formatting). No path/shape change forces the mock to fail | PASS |
| 5 | ExecutionVenue seam is a faithful sol-sniper drop-in | execution-venue.md §1 (`execute(intent,event)->FillResult` arity+return preserved), §2 (Sim/JitoJupiter/Raydium/DeadCcxt behind one ABC; loop core imports the interface only), §6 (seam fidelity checklist); ADR-0003 | PASS |
| 6 | C-1..C-13 each structurally enforced | BLUEPRINT §7 + validation-harness.md §4 + data-models.md §3.3/§3A | PASS — see §4 checklist |
| 7 | Money is integer/Decimal, never float | data-models.md §0 (rule on every field); api-contracts §contract principle 2 (CI schema test asserts no monetary JSON float); all Intent/Position/Cost fields are `int` lamports or `Decimal`-string | PASS |
| 8 | LLM off the FAST/SNIPE critical path | BLUEPRINT §2.1 invariant (SNIPE reads only a KV scalar; FAST never awaits LLM/SLOW/RPC), §4.2 (LLM is a separate Python process, physically unreachable from the hot core); latency-budget.md §6 ("HOT PATH STAYS COLD OF THE LLM"); ADR-0002 | PASS |
| 9 | Survivable stop has no single point of failure | BLUEPRINT §9 (3 independent layers + failover); ADR-0008; infrastructure.md §8 (DMS separate failure domain, cannot be disarmed by LLM/market/risk) | PASS |
| 10 | Asymmetric trust enforced by type | data-models.md §6 (`ReasoningAction` enum has no risk-increase member; `Intent` union has no risk-increase variant; reasoning path handed only a de-risk factory); ADR-0006; BLUEPRINT §8 | PASS |
| 11 | All red-team blocksG1 items resolved by construction | BLUEPRINT §14; ADR-0009 (custody topology reversed — `aats-signer` separate process); ADR-0010 (typed `LaunchOutcome` label + provenance/lineage build guards) | PASS — see §5 |
| 12 | Every significant decision has an ADR | ADR-0001..0010 present and substantive; post-G1 change protocol (ADR + delta notice) stated in every sibling doc | PASS |

---

## 3. Contract-reconciliation confirmation (no green-on-mock break)

`api-contracts.md` is FROZEN v1.0.0 and matches `dashboard/src/lib/api.ts` `ENDPOINTS` exactly:

- `state, feed, metrics, positions, latency, sentiment, predictions, reasoning, riskConfig, health,
  kill, flatten, breakerReset, mode` — **all 14 keys present and path-identical** in both the frozen
  set (api-contracts §12) and the client (`api.ts` lines 50-65).
- `flatten/{mint}` — client posts `${ENDPOINTS.flatten}/${encodeURIComponent(mint)}` (`api.ts` line
  533); contract path `POST /api/flatten/{mint}` (api-contracts §5). **MATCHED.**
- No endpoint, field, or enum value moved during the G1 red-team (api-contracts §11 note +
  BLUEPRINT §14 closing line). The custody/leak resolutions touched data-models, infrastructure,
  execution-venue, validation-harness, latency-budget and added ADR-0009/0010 — the **wire contract
  is unchanged.**
- The only dashboard delta is a **Lane-E transcription** (T-352): `AgentMode` `paper|dry-run|live`
  → canonical `SHADOW|PAPER|LIVE_DRY_RUN|LIVE`; wire snake_case → view-model camelCase adapter on the
  live branch; money formatted from integer/decimal-string. The mock branch (`USE_MOCK` default true,
  `api.ts` line 46-47) is **untouched** — `VITE_USE_MOCK=true` stays green (api-contracts §9, NFR-011,
  AC-049). **CONFIRMED: no green-on-mock break.**

---

## 4. C-1..C-13 structural-enforcement checklist (each is a type / build guard / process boundary / frozen artifact — NOT prose)

| C | Condition | Structural enforcement | Where | Mechanism class |
|---|---|---|---|---|
| C-1 | Latency honesty | Internal compute / block-engine RTT / staked-lane leader-land kept in 3 separate ledger classes; +1-slot penalty propagated as a `buyers_ahead` right-shift + widened adverse-selection haircut into `CostStack`; `/api/latency` shows two columns | latency-budget.md §2/§4; api-contracts §4 `/api/latency` `class` field | data propagation + frozen artifact |
| C-2 | No inherited optimism | Clean-room cost stack; build FAILS if it imports `venue.py._competitor_delay` / sandwich constants / `generate_path` (`inherited_sim_constant_forbidden`); haircut is a FLOOR to widen | validation-harness.md §2 guard 2, §C-2 | **build guard** |
| C-3 | Tip-cohort-bias kill | `tip_floor_at_decision_lamports` + `tip_contention_bucket` recorded on every FeatureFrame; harness stratifies GATE-A by bucket; low-contention-only profit ⇒ `negative_selection_residual` BLOCKS R4 | data-models.md §3 FeatureFrame; validation-harness.md §C-3 | typed field + harness gate |
| C-4 | Freeze + build baseline | FeatureFrame carries `first_k_buy_pressure` + `first_k_volume_lamports` (baseline constructible); baseline params in committed hashed `baseline.frozen.json`; `baseline_changed_after_fit` FAILS on post-fit change | data-models.md §3.2; validation-harness.md §C-4 | typed field + **hashed frozen artifact + test** |
| C-5 | Clock + frozen haircut | Every frame event-time-stamped only (`wall_clock_ms` marked NOT-for-joins); Parquet partitioned by `event_date`, not compute-time; per-feature `FeatureSourceWindow` cutoff build guard; global clock-shift control (necessary) + independent label-horizon/per-feature-lineage placebo (sufficient); haircut fit train-only, per-window refit ⇒ `haircut_refit_leak` | data-models.md §3.1/§3.3/§9.2; validation-harness.md §C-5 | **build guard + partition key + placebo battery** |
| C-6 | Completeness audit | Reconcile vs independent pool-create census; un-snapshotted/un-labeled rows carried as `completeness_status=CENSORED`, never dropped; `(complete+CENSORED)/census ≥ 1−max_miss` asserted | data-models.md §7 Position; validation-harness.md §C-6 | typed enum value + measured assertion |
| C-7 | Clean-room harness | Validation is a separate package; build FAILS on any `truth_*` reference or `sniper_sim` import in its closure; PRIMARY defense is the lineage taint guard; recall ≥0.50 is a MEASURED output (`recall_must_be_measured`) | validation-harness.md §2 guards 1+4, §2.5 guard 6 | **build guard** |
| C-8 | R2 necessary-not-sufficient | Staging ladder marks R2/GATE-A necessary-not-sufficient; first real haircut validation deferred to R3 fills; fill-probability modeled conditional on outcome | infrastructure.md §3; validation-harness.md §C-8 | frozen staging artifact |
| C-9 | Experiment log + deflation | Committed, append-only, hashed experiment log is a PRECONDITION for scoring; `experiment_log_missing_or_tampered` refuses to score; significance deflated by logged trial count | validation-harness.md §C-9 | **precondition gate + hashed artifact** |
| C-10 | Group-purge | `creator_wallet` / `bundler_cluster_id` / `deploy_template_fingerprint` carried on `LaunchEvent`; harness group-purges across embargo; reports with/without; >20% delta ⇒ `actor_identity_memorization` | data-models.md §2; validation-harness.md §C-10 | typed fields + harness gate |
| C-11 | Calibrated-haircut sub-gate | Haircut calibrated from recorded R1 fills BEFORE GATE-A at R2; >200 bps at target size ⇒ EH-001 midpoint re-derived/killed; pre-cal default 150 bps labeled UNCALIBRATED | data-models.md §6.2 `CostStack`; validation-harness.md §C-11 | typed field + sub-gate |
| C-12 | Regime + staleness | GATE-A/B reported per regime bucket; drift monitor on launch-population; proof-staleness bound auto-re-runs gate on fresh data before any lamport moves | infrastructure.md §3; validation-harness.md §C-12 | staging artifact + drift monitor |
| C-13 | Independent-surface reporting | Harness reports how many EH surfaces survive independently under corrected competitor distribution; pooled-only survival flagged as one fragile edge | validation-harness.md §C-13 | harness report gate |

**All thirteen are enforced by a structural mechanism, not a reminder.** The four highest-risk leak
surfaces (forward-looking label as feature, inherited sim optimism, compute-time leakage, backfill
lookahead) are closed by build/load guards that prevent compilation, not by reviewer vigilance
(validation-harness.md §2.5 guards 5-8; data-models.md §3.3; ADR-0010).

---

## 5. Red-team blocksG1 resolution confirmation

Two of three G1 red-team lenses returned `blocksG1=true`. Both are resolved **by construction** and
the contract does not freeze with an open blocker:

- **Lens 1 — Leak-proofness (was BLOCKED) → RESOLVED by ADR-0010.** Typed `LaunchOutcome` label in
  its own event-time-partitioned `labels/` dataset (harness-only writer); per-feature
  `FeatureProvenance` cutoff guard; lineage/taint guard (`feature_lineage_touches_label`) as the new
  PRIMARY defense replacing name-scan; label/feature column-disjointness guard; `recorded_at` honesty
  guard (`recorded_at_before_knowable` / `backfill_recorded_at_regression`); per-feature/per-label
  placebo as the sufficient complement to the necessary-not-sufficient global clock shift. (BLUEPRINT
  §14 Lens 1; data-models.md §3A/§3.3/§9.2; validation-harness.md §2.5/§C-5.)
- **Lens 2 — Custody (was BLOCKED) → RESOLVED by ADR-0009.** Signer topology reversed: `aats-signer`
  is a separate minimal-surface process (no inbound network, no untrusted-byte decode) holding the
  secret; the hot core holds the pubkey only. Three independent signer-side refusals (per-tx +
  rolling SOL spend cap; full enumerated program allowlist; value-moving-transfer pinning to the 8
  live Jito tip accounts). Secret via short-lived Vault token + `mlock` + zeroize; the env-injected
  raw-key path removed. Telegram authz hardened (bot-token secrecy + per-command confirm). (BLUEPRINT
  §14 Lens 2; infrastructure.md §5; ADR-0009.)
- **Lens 3 — Code-reviewer (NON-blocking) → ADDRESSED.** SimulationVenue implements the 8 promoted
  ABC members sim-native (R-01); `execute()` arity+return preserved with `SwapIntent`→`EntryIntent`
  money promotion (R-02); Lane-E transcription tracked with the mock branch green (R-03). No contract
  edit required. (BLUEPRINT §14 Lens 3; execution-venue.md §2/§6.)

The ADR-0009/0010 deltas list every affected board task (BLUEPRINT §14 delta notice). The board is
updated below to absorb them.

---

## 6. HARD-RULE (non-waivable) compliance — verified, not assumed

The AUTONOMY-DIRECTIVE does not waive the technical/safety gates. Verified honored in the architecture:

1. **Real capital DISABLED by default** — DRY-RUN enforced at 3 structural levels (venue `submit_mode`,
   `DRY_RUN_ENABLED` config, `JitoJupiterVenue` refusal); default boot `SHADOW`; CI-asserted no-submit
   invariant. (execution-venue.md §4; infrastructure.md §2.)
2. **No win-rate field anywhere** — api-contracts §4 HONESTY CLAUSE; no win-rate in `MetricsSnapshot`;
   no Grafana win-rate panel (infrastructure.md §7).
3. **Safety built first** — TASKBOARD ordering T-320→T-321→T-322 precede T-327's live path; ADR-0008.
4. **Asymmetric trust + LLM off FAST/SNIPE** — verified §2 rows 8 + 10.
5. **Point-in-time / Rust hot path / integer-Decimal money / no secrets in code** — verified
   throughout; secrets are Vault references only (infrastructure.md §9).

---

## 7. Deferred risks (carried into P3/P4, none block G1)

These are correctly-scoped downstream proof obligations, not architecture gaps:

- **Edge remains UNPROVEN net of cost.** The architecture makes a fake edge un-reportable and a real
  edge survivable; it does not assert an edge exists. GATE-A + GATE-B on RECORDED data (T-400/T-401)
  are the proof, enforced at G4. No real capital until they pass and the CEO authorizes R3.
- **Submission disadvantage is irreducible in code.** Co-location + staked-QUIC/SWQoS are infra spends
  owned by `latency-devops-engineer` (latency-budget.md §5); the edge surface is deliberately the
  inverse of the speed race (selection + exit discipline).
- **`aats-signer` cross-process `sign()` adds ≤1.5ms p99** to the snipe hot path — budgeted and well
  inside the ≤150ms p99 SNIPE ceiling (latency-budget.md hop 5; ADR-0009 consequences).
- **R3/R4 capital advance is the one decision the agency does not make alone** — `NEEDS-CEO-DECISION`
  at the funding rung (infrastructure.md §3). Not a G1 item.
- **HSM-grade key isolation is future work** — the process split is the v1 floor, not the ceiling
  (ADR-0009 consequences). Acceptable for a ≤2 SOL incinerable trade-only wallet at R3.

---

## 8. Gate decision

**G1 ARCHITECTURE — APPROVED (agency-autonomous per AUTONOMY-DIRECTIVE.md).** The architecture wave
T-200..T-206 is DONE. Code may now begin. Dispatch P2.5 (scaffold + custody, T-250 ∥ T-251) and, as
their deps clear, the P3 build lanes. Real capital stays behind the DRY-RUN flag through P6.
