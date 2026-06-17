# GATE G0 — SCOPE GATE — APPROVED (agency-autonomous)

**Gate:** G0 — Scope Gate (charter §4)
**Project:** AATS Solana Meme-Coin Ultra-Sniper
**Verdict:** **APPROVED**
**Approver:** **agency-autonomous per `.agency/AUTONOMY-DIRECTIVE.md`** (NOT the CEO).
The CEO delegated all gate approvals to the agency; the `orchestrator` G0-REVIEW issued
**READY-FOR-CEO / APPROVE ALL DEFAULTS AS-IS**, which under the standing directive constitutes
approval. No human approval pause was taken.
**Author:** `orchestrator` (Delivery Lead)
**Date:** 2026-06-16
**Basis of approval:** `.agency/05-reports/gates/G0-REVIEW.md` (9/9 G0 criteria PASS) +
`.agency/AUTONOMY-DIRECTIVE.md` (open questions resolved by their recommended default).

---

## 1. What is approved

- **Scope as specified:** 57 FRs · 11 NFRs · 32 user stories · 60 ACs (SPEC v1.0.0,
  user-stories v1.0.0, acceptance-criteria v1.0.0). All numbered, no coverage gaps.
- **11 / 11 competitive features** covered by a real, measurable AC.
- **All 4 mandatory invariants** carry explicit NEGATIVE ACs.
- **13 / 13 EDGE-VERDICT conditions (C-1..C-13)** encoded as FR/AC/§-gate.
- **Honest acceptance metric:** net-of-cost PnL AND model-vs-naive-baseline (lower 95% bound > 0).
  No win-rate target or claim anywhere.
- **Real capital DISABLED by default** behind the DRY-RUN flag; CEO-gated at capital-staging rung R3.

The 9/9 G0 criteria check is recorded in `G0-REVIEW.md §1`. Two cosmetic traceability fixes
(F-1a / F-1b / F-2) are scheduled as **T-106** before the architect consumes the spec; they do
not affect coverage and do not block this approval.

---

## 2. Adopted open-question defaults (OQ-001..OQ-010) — resolved values

Per the AUTONOMY-DIRECTIVE, each open question is **resolved by its recommended default** (the
conservative, capital-protective choice). All 10 are adopted as-is. Source:
`.agency/01-specs/open-questions.md`.

| OQ | Question | Priority | **ADOPTED resolved value** |
|---|---|---|---|
| OQ-001 | Daily-risk-capital tranche definition | G1-BLOCKER | **(B) Daily-reset to wallet balance at UTC midnight**, plus a hard-coded absolute floor of **−0.30 SOL** independent of the % calc. `/api/risk-config` may **tighten** the % only (never widen beyond the spec-stated 3.0%). |
| OQ-002 | Smart-money wallet list size / source | non-blocker | **(3) Disabled by default.** CEO supplies 0–20 public addresses at deploy time. Feature labeled **EXPERIMENTAL — EH-005 expected-ZERO until measured**; Lane A builds the infra, list populated as lift is proven. |
| OQ-003 | Colocation / RPC tier | G1-BLOCKER (before P2.5) | **(1) `dedicated_geyser`** (~$200–500/mo). Build is tier-agnostic via pluggable `InfraTier` config; upgrade to `colo_shred` later. Sufficient for shadow/paper; revisit only if R3 land-rate < 35%. |
| OQ-004 | Telegram bot token + operator user ID | non-blocker (BLOCKER for Lane F) | **(1) Single operator Telegram user ID in `.env`.** Secret lives in `.env.example` only; a second authorized user is a one-line env change. |
| OQ-005 | Per-trade cap + max aggregate exposure | G1-BLOCKER | **(1) per-trade cap = 0.1 SOL, max aggregate = 0.5 SOL, daily-risk tranche = 0.5 SOL** as the hardcoded floor. `/api/risk-config` may only **tighten** (≤ floors). Widening is an explicit CEO config act at R3, never a runtime API call. |
| OQ-006 | Dead-man's switch `T_DMS` | G1-BLOCKER | **60 seconds**, encoded as an **environment variable** (not a hardcoded constant) so it can be tightened without a code change. |
| OQ-007 | Pre-calibration adverse-selection haircut | non-blocker (BLOCKER at R2 QA) | **150 bps (conservative top of band)**, prominently labeled **UNCALIBRATED** in all pre-R1 reports. Measured value replaces it after R1; if measured > 200 bps the C-11 sub-gate fires. |
| OQ-008 | Default exit mode (Fast vs Secure MEV) | non-blocker | **Secure-MEV default** (capital-preservation priority, lower sandwich risk). Operator may switch to Fast-MEV via Settings preset; EH-002 measures the realized delta. |
| OQ-009 | Jurisdiction / legal classification | non-blocker (build); **BLOCKER at R3 only** | **(2) `docs-delivery` adds a legal-disclaimer** to README + deploy guide stating the operator is responsible for confirming local-law compliance before enabling real capital. **No code impact. This gates only R3 (real capital), which stays DISABLED by default.** R3 authorization carries the legal-confirmation requirement; G0..G6 paper-build is unaffected. |
| OQ-010 | Multi-wallet `N_max` + blast-radius cap `C` | non-blocker (BLOCKER for FR-036 test) | **N_max = 1 at R3** (single wallet, no complexity); `C = per_trade_cap`. Multi-wallet (Option 2) is built and tested in the integration harness but **not activated until R4**. |

**G1-blockers — all settled by adopted default:** OQ-001, OQ-003, OQ-005, OQ-006. These numeric
values are now baked into the architecture wave (P2) blueprint.

**R3-only blocker (real capital, not on the paper-build path):** OQ-009 (legal). It does not
touch any G0–G2 artifact or any code; it is a precondition of R3 real-capital authorization only,
and real capital is DISABLED by default.

---

## 3. HARD RULES not waived by this approval (per AUTONOMY-DIRECTIVE §"does NOT waive")

This is an approval-gate verdict only. The locked technical/safety rules remain absolute:
real capital disabled by default; no win-rate metric; safety built first (breaker / survivable
stop / dead-man's switch proven before any live-capable path); asymmetric trust; point-in-time
correctness, Rust hot path, integer/Decimal money, no secrets in code/logs/images.

---

## 4. Next

P1 spec is APPROVED. The agency advances to **P2 — Architecture (G1)**:
- `quant-product-analyst → T-106` applies the cosmetic traceability fixes (F-1a/F-1b/F-2).
- `solana-systems-architect → T-200..T-206` produces the blueprint with the four settled
  numeric blockers (OQ-001/003/005/006) baked in.
- **No code before G1.**
