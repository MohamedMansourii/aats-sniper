# GATE G0 — SCOPE GATE REVIEW

**Gate:** G0 — Scope Gate (charter §4)
**Project:** AATS Solana Meme-Coin Ultra-Sniper
**Reviewer:** `orchestrator` (Delivery Lead)
**Date:** 2026-06-16
**Approver:** CEO
**Artifacts under review:**
- `.agency/01-specs/SPEC.md` (v1.0.0)
- `.agency/01-specs/user-stories.md` (v1.0.0)
- `.agency/01-specs/acceptance-criteria.md` (v1.0.0)
- `.agency/01-specs/open-questions.md` (v1.0.0)
- `.agency/01-specs/EDGE-VERDICT.md` (GO-PAPER-ONLY, C-1..C-13)

---

## VERDICT

**READY-FOR-CEO — APPROVE WITH 2 TRACEABILITY FIXES (non-blocking, no re-spec).**

The spec passes every substantive G0 criterion: spec + user stories + acceptance criteria
are complete and MEASURABLE; every competitive feature and every operator-UI criterion has
≥ 1 acceptance criterion; every HARD RULE is reflected as an AC or NFR; all 13 EDGE-VERDICT
conditions are encoded; all 10 open questions carry a recommended default.

Two **documentation-traceability defects** exist in cross-reference tables (not coverage
gaps — the covering ACs all exist and are testable). They are listed in §4 below as
correct-on-merge items assigned to `quant-product-analyst`. They do NOT block CEO G0
approval because no requirement is left uncovered and no AC is unmeasurable. The CEO may
approve scope now; the fixes are applied before the architect consumes the spec at G1.

---

## 1. G0 CRITERIA CHECK (charter §4)

| # | G0 criterion | Verdict | Evidence |
|---|---|---|---|
| 1 | Spec complete (problem, scope, non-goals, FRs, NFRs) | PASS | SPEC.md §1–§11: 57 FRs (FR-001..FR-057), 11 NFRs (NFR-001..011), explicit IN/NON scope §3, non-goals restated §8 |
| 2 | User stories complete + traceable to FRs | PASS | user-stories.md: 32 stories (US-001..032) across 8 epics; US→FR matrix lines 288–322; FR-coverage check lines 323–341 (every FR mapped to ≥ 1 story) |
| 3 | Acceptance criteria complete + MEASURABLE | PASS | acceptance-criteria.md: 60 ACs (AC-001..060); zero-vague scan lines 547–566; every AC has a number + unit + Given/When/Then or numeric threshold |
| 4 | Every competitive feature has ≥ 1 AC | PASS (2 mislabels) | §3 below — 11/11 features covered by a real AC; 2 §9 cross-refs point at wrong AC ids (fix F-1) |
| 5 | Every operator-UI / Telegram criterion has ≥ 1 AC | PASS | §3 below — dashboard, latency, monitoring, kill, flatten, Telegram alerts/authz, P&L, reasoning all covered |
| 6 | Every HARD RULE reflected as AC or NFR | PASS | §5 below — all 4 invariants have NEGATIVE ACs; all 13 C-conditions encoded; all 8 non-goals fenced |
| 7 | Open questions each have a recommended default | PASS | open-questions.md OQ-001..010, summary table lines 228–239; every row has a Recommended Default |
| 8 | Acceptance metric is honest (no win-rate) | PASS | SPEC §8.1, EDGE-VERDICT §4 HONESTY CLAUSE; AC-037 forbids any win-rate label; metric = net-of-cost PnL AND model-vs-baseline, lower 95% bound > 0 |
| 9 | Real-capital safety encoded | PASS | DRY-RUN default (FR-039, AC-060); capital-staging R0..R4 §11; R3 CEO-gated (US-032, AC-060); safety-first build order on board |

**9 / 9 criteria PASS** (criterion 4 carries 2 cosmetic cross-ref fixes that do not affect coverage).

---

## 2. HEADLINE NUMBERS

- **57 FRs · 11 NFRs · 32 user stories · 60 ACs** — all numbered, no gaps.
- **11 / 11 competitive features** covered by a real AC.
- **All 4 mandatory invariants** carry explicit NEGATIVE ACs.
- **13 / 13 EDGE-VERDICT conditions** (C-1..C-13) encoded as FR/AC/§-gate.
- **10 / 10 open questions** carry a recommended default; **4 are G1-blockers**
  (OQ-001, OQ-003, OQ-005, OQ-006), **1 is an R3-blocker** (OQ-009 legal).
- **0** uses of unquantified language ("fast enough", "usually") in any AC.
- Acceptance metric stated **3×** identically (SPEC exec summary, §8, EDGE-VERDICT §4); no win-rate target anywhere.

---

## 3. AC-COVERAGE CHECKLIST — COMPETITIVE FEATURE & OPERATOR-UI → AC IDS

### 3a. Competitive features (SPEC §9) → covering ACs

| Competitive feature | FR(s) | Covering AC(s) | Status |
|---|---|---|---|
| Auto-sniper on new launch | FR-001, FR-021, FR-026, FR-027 | AC-001, AC-016, AC-011, AC-013, AC-017 | COVERED |
| Migration sniper (pump.fun→PumpSwap/Raydium) | FR-001, FR-002, FR-021 | **AC-020, AC-021** (and AC-001 decode) | COVERED — §9 mislabels these as "FR-001..003" (fix F-1a) |
| Copy-trade / smart-money as selectivity filter | FR-007, FR-011, FR-032 | AC-008, AC-009 (neg), AC-020 (neg sizing) | COVERED |
| Limit + DCA resting orders (fire offline) | FR-035 | AC-022, AC-023 | COVERED |
| Auto TP-ladder + trailing + auto-strat presets | FR-029, FR-030 | AC-032, AC-033 | COVERED |
| Multi-wallet / bundle execution + anti-cluster | FR-036, FR-040 | AC-024 | COVERED |
| MEV protection: Fast vs Secure modes | FR-029, FR-041 | AC-018 (direct-AMM), AC-039 (exit-mode field) | COVERED |
| Token-safety scanner (red flags on dashboard) | FR-026, FR-037 | AC-011, AC-015 | COVERED |
| Token discovery enrichment (DEXScreener/Birdeye/Meteora/Moonshot) | FR-006 | AC-007 | COVERED |
| Portfolio + P&L cards / export | FR-049 | **AC-039** | COVERED — §9 mislabels as "US-011, AC-034" (fix F-1b) |
| Telegram operator channel (alerts + constrained commands) | FR-050, FR-055 | AC-042, AC-043, AC-052, AC-053 | COVERED |

**Result: 11 / 11 competitive features have ≥ 1 real, measurable AC.**

### 3b. Operator-UI / Telegram control-plane criteria → covering ACs

| Operator-UI / Telegram criterion | FR(s) | Covering AC(s) | Status |
|---|---|---|---|
| Live dashboard wired to real bot (VITE_USE_MOCK=false) | FR-049, FR-056 | AC-047, AC-048 | COVERED |
| Dashboard builds green on mock (offline dev) | FR-049, NFR-011 | AC-049 | COVERED |
| Token-safety red flags visible per candidate | FR-037, FR-049 | AC-015 | COVERED |
| Net-of-cost PnL primary; gross secondary | FR-005, NFR-009 | AC-036 | COVERED |
| Model-vs-baseline delta surfaced (no win-rate label) | FR-043 | AC-037 | COVERED |
| GATE-A / GATE-B results visible | FR-043, FR-047, FR-048 | AC-038 | COVERED |
| Position P&L cards + CSV/JSON export | FR-049 | AC-039 | COVERED |
| Kill from dashboard within 2 s + confirm modal | FR-025, FR-049, FR-055 | AC-040, AC-041 | COVERED |
| Kill from Telegram, same 2 s guarantee | FR-050, FR-055 | AC-042 | COVERED |
| Telegram command authz (only operator ID) | FR-050 | AC-043 | COVERED |
| Flatten single position (dashboard or Telegram) | FR-025, FR-049, FR-050 | AC-044 | COVERED |
| Latency page: internal vs block-engine separated | FR-049, NFR-001 | AC-050 | COVERED |
| Module health + staleness alert | FR-057, NFR-004, NFR-006 | AC-051 | COVERED |
| Telegram alert on fill ≤ 10 s | FR-050 | AC-052 | COVERED |
| Telegram alert on breaker trip ≤ 10 s | FR-050, FR-034 | AC-053 | COVERED |
| LLM reasoning log + clamp trace | FR-017, FR-049 | AC-054 | COVERED |
| Mode change requires DRY-RUN off + CEO auth | FR-039 | AC-060 | COVERED |

**Result: every operator-UI and Telegram criterion has ≥ 1 measurable AC.**

---

## 4. TRACEABILITY DEFECTS (CORRECT-ON-MERGE — NON-BLOCKING)

These are cross-reference errors in summary tables. The covering ACs exist and are
testable, so coverage is intact and CEO approval is not blocked. Assigned to
`quant-product-analyst` to fix before the architect consumes the spec at G1 dispatch.

| ID | Defect | Location | Correct value | Severity |
|---|---|---|---|---|
| F-1a | Migration-sniper row cites "FR-001, FR-002, FR-003" as its coverage but the migration-specific ACs are AC-020/AC-021 | SPEC.md §9 line 593 | add AC-020, AC-021 to the row | LOW (cosmetic) |
| F-1b | Portfolio/P&L-cards row cites "US-011, AC-034" — US-011 is resting orders, AC-034 is consecutive-loss halt | SPEC.md §9 line 601 | correct to US-020, AC-039 | LOW (cosmetic) |
| F-2 | US-006 acceptance line carries an unresolved editorial note "AC-011, AC-012 (wait — renumber post-merge)"; AC-012 is the FSM/US-009 negative AC, not a US-006 gate AC | user-stories.md line 64 | replace with "AC-011" (US-006's gate AC); remove the renumber note | LOW (editorial) |

None of F-1a/F-1b/F-2 leaves a requirement uncovered or an AC unmeasurable.
**They do not change scope and do not require re-running spec.** Fix is a 3-line edit.

---

## 5. HARD-RULE → AC / NFR COVERAGE

Every HARD RULE from EDGE-VERDICT.md and SPEC §8 non-goals maps to an enforcing AC or NFR.

### 5a. Four mandatory invariants (each has ≥ 1 NEGATIVE AC)

| Invariant | FR | NEGATIVE AC(s) | Status |
|---|---|---|---|
| Asymmetric LLM trust (LLM may never size up / widen / override) | FR-017, FR-031, FR-032 | AC-019, AC-020, AC-021, AC-031, AC-054 | COVERED |
| No double-entry (single-writer FSM) | FR-024 | AC-012 | COVERED |
| Cost gate (`edge > cost` or NO TRADE; tip never hardcoded) | FR-027 | AC-013, AC-014 | COVERED |
| Survivable stops (3 independent layers) | FR-033 | AC-025, AC-026, AC-027 | COVERED |

### 5b. EDGE-VERDICT conditions C-1..C-13

| Condition | FR / §-gate | AC(s) | Status |
|---|---|---|---|
| C-1 latency honesty (detection-competitive, submission-disadvantaged) | FR-051, NFR-001 | AC-016, AC-050 | COVERED |
| C-2 no inherited optimism in cost stack | FR-044 | AC-057 (clock), enforced at G4/T-401 | COVERED |
| C-3 tip-contention stratification / scale-up block | FR-047 | AC-038 (+ T-401 gate) | COVERED |
| C-4 frozen + constructible naive baseline | FR-015, FR-005 | AC-037, AC-005; build-fail on param change (FR-015) | COVERED |
| C-5 clock audit + frozen train-fold haircut | FR-044, FR-045 | AC-057 | COVERED |
| C-6 completeness / census / censored outcomes | FR-009 | AC-006 | COVERED |
| C-7 clean-room harness, no truth_* | FR-019 | AC-056 | COVERED |
| C-8 R2 necessary-not-sufficient | §11 R2/R3 | AC-060 (live gate) + §11 caveat | COVERED |
| C-9 experiment-log precondition + deflation | FR-020 | AC-059 | COVERED |
| C-10 group-aware purge | FR-046 | AC-058 | COVERED |
| C-11 calibrated-haircut sub-gate (>200 bps → re-justify/kill) | FR-044 | enforced at T-401 (G4); A-008 register | COVERED |
| C-12 regime/staleness re-prove before R3 funding | §11 R3/R4 | §11 + T-105/T-401 | COVERED |
| C-13 independent-surface reporting | FR-048 | AC-038 (+ T-401) | COVERED |

### 5c. Non-goals (SPEC §8) — each fenced

| Non-goal | Fence | Status |
|---|---|---|
| Fixed win-rate target/claim | SPEC §3, §8.1; AC-037 forbids label | FENCED |
| Block-0 / migration-block-0 race | FR-017, AC-017, AC-020, AC-021 (slot ≥ +5) | FENCED |
| CEX execution path | SPEC §3 (ccxt dead stub) | FENCED |
| LLM on FAST/SNIPE critical path | FR-018, FR-021 | FENCED |
| Any signal increasing risk | FR-032, AC-009, AC-020, AC-021, AC-031 | FENCED |
| Automated capital scaling | §11, AC-060 (CEO-gated R3) | FENCED |
| Blind copy-trade mirror | FR-007, FR-032, AC-009 | FENCED |
| Float arithmetic for money | NFR-009, FR-042 | FENCED |

**Result: every hard rule is reflected as an AC or NFR. No hard rule is unprotected.**

---

## 6. MEASURABILITY SPOT-CHECK (anti-vagueness)

Confirmed the zero-vague-criterion claim by sampling latency, rate, and negative ACs:
- Latency ACs carry ms + p50/p99: AC-001 (≤ 800 ms p99), AC-016 (≤ 150 ms p99 / ≤ 50 ms p50),
  AC-026 (≤ 50 ms p99), AC-040 (≤ 2,000 ms), AC-047 (≤ 3,000 ms p99), AC-052/053 (≤ 10,000 ms).
- Rate / count ACs carry numeric thresholds: AC-011 (100% / 20 fixtures), AC-028 (−3.0% / −0.30 SOL),
  AC-030 (¼-Kelly cap, swept P 0.1–0.9), AC-055 (≥ 5 windows, ≥ 300 events).
- Negative ACs name the prohibited outcome and an injected-fixture count: AC-013 (50),
  AC-019 (100), AC-020 (50), AC-012 (1,000), AC-031 (per-injection delta ≤ 0).

No AC relies on subjective language. The acceptance metric never resolves to a win-rate.

---

## 7. CONSOLIDATED CEO DECISION LIST (open questions + defaults + my recommendation)

All 10 open questions carry a defensible default. The CEO can approve all defaults in one
pass; only the 4 G1-blockers must be settled before architecture begins, and the R3-legal
item before any real capital. My recommendation is to **APPROVE ALL DEFAULTS AS-IS** — each
default is the conservative, capital-protective choice consistent with GO-PAPER-ONLY.

| # | Question | Priority | Recommended default | My recommendation |
|---|---|---|---|---|
| OQ-001 | Daily-risk-capital tranche definition | **G1-BLOCKER** | (B) daily-reset to wallet balance at UTC midnight + hard −0.30 SOL floor; CEO may tighten % only | **ACCEPT default.** Auto-tightening as wallet depletes is the protective behavior; the absolute floor caps a bad day regardless of % math. |
| OQ-002 | Smart-money wallet list size/source | non-blocker | (3) disabled by default; CEO supplies 0–20 public addrs; EH-005 labeled EXPERIMENTAL | **ACCEPT default.** EH-005 is expected-ZERO per EDGE-VERDICT; build infra, prove lift before enabling. Zero capital impact. |
| OQ-003 | Colocation / RPC tier | **G1-BLOCKER (before P2.5)** | (1) dedicated_geyser (~$200–500/mo); pluggable InfraTier, upgrade to colo_shred later | **ACCEPT default.** Late-entry thesis (slot +5..+30) is not submission-speed-sensitive; dedicated tier is sufficient for shadow/paper. Revisit only if R3 land-rate < 35%. |
| OQ-004 | Telegram bot token + operator user ID | non-blocker (BLOCKER for Lane F) | (1) single operator ID in `.env` | **ACCEPT default.** Solo operator; second user is a one-line env change. Secret stays in `.env.example` only. |
| OQ-005 | Per-trade cap + max aggregate exposure | **G1-BLOCKER** | (1) per-trade 0.1 SOL, aggregate 0.5 SOL, tranche 0.5 SOL as hardcoded floor; API may only tighten | **ACCEPT default.** Matches R3 "incinerable ≤ 2 SOL" posture. Widening is an explicit CEO config act at R3, never a runtime API call. |
| OQ-006 | Dead-man's switch T_DMS | **G1-BLOCKER** | 60 s, env-configurable | **ACCEPT default.** 60 s balances false-trigger vs unmanaged-position risk; env var lets CEO tighten without a code change. |
| OQ-007 | Pre-calibration adverse-selection haircut | non-blocker (BLOCKER at R2 QA) | 150 bps (conservative top of band), labeled UNCALIBRATED | **ACCEPT default.** C-2 mandates floor-widen-only; 150 bps is the honest pre-R1 placeholder. Measured value replaces it after R1. |
| OQ-008 | Default exit mode (Fast vs Secure MEV) | non-blocker | Secure-MEV default | **ACCEPT default.** Capital-preservation priority; lower sandwich risk. Operator can switch via preset; EH-002 measures the realized delta. |
| OQ-009 | Jurisdiction / legal classification | non-blocker (BLOCKER at R3) | (2) docs add legal-disclaimer; R3 authorization withheld until CEO confirms legal review | **ACCEPT default — and flag to CEO now.** No code impact, but R3 real-capital authorization must include the CEO's confirmation of local-law compliance. This is a pure CEO decision. |
| OQ-010 | Multi-wallet N_max + blast-radius cap C | non-blocker (BLOCKER for FR-036 test) | N_max = 1 at R3; multi-wallet (Option 2) at R4; C = per_trade_cap | **ACCEPT default.** Single wallet at first real capital removes complexity; multi-wallet is built and tested but not activated until R4. |

**G1-blockers to settle before architecture:** OQ-001, OQ-003, OQ-005, OQ-006.
**R3-blocker (real capital):** OQ-009 (legal) plus all G1-blockers.

---

## 8. RISKS CARRIED FORWARD (not gate-blocking)

- **Edge is UNPROVEN net of cost** (GO-PAPER-ONLY). This is the dominant project risk and is
  correctly handled by the recorded-data gates, not by spec wording. Real capital disabled by default.
- **A-008 (haircut 75–150 bps) is UNCONFIRMED** — a FLOOR to calibrate upward. If calibrated
  > 200 bps, EH-001 may flip negative (C-11 sub-gate fires at R2/T-401). Surfaced, not resolved.
- **A-010 (≥ 3,000 recorded launches achievable) is UNCONFIRMED** — R2 gate may be delayed.
  Alert CEO at the R1→R2 boundary if shadow accumulation is slow.
- **OQ-009 legal** is a genuine CEO-only decision gating R3; flagged now so it is not a surprise
  at funding time.

---

## 9. RE-ENTRY (if CEO returns FAIL or CONDITIONAL)

If the CEO rejects scope or changes a requirement: route to `quant-product-analyst` (spec)
and, where capital-at-risk numbers move, to `solana-systems-architect` (blueprint delta)
per charter §3.6 before any downstream work. The two cosmetic fixes (F-1a, F-1b, F-2) are
applied regardless, as a condition of advancing to G1.

---

## DECISION REQUESTED OF THE CEO

1. **Approve G0 scope** as specified (57 FRs, 60 ACs, honest acceptance metric, real capital
   disabled by default).
2. **Approve the 10 open-question defaults** (or amend OQ-001 / OQ-003 / OQ-005 / OQ-006,
   the 4 G1-blockers).
3. **Acknowledge OQ-009** — real-capital (R3) authorization will later require your
   confirmation of local-law compliance.

On CEO approval, the orchestrator records `G0-PASS.md`, applies fixes F-1a/F-1b/F-2, and
dispatches `solana-systems-architect` for the architecture wave (P2) with the 4 settled
numeric blockers baked into the blueprint.
