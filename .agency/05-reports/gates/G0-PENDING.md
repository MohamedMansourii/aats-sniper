# GATE G0 — SCOPE — STATUS: PENDING (pre-G0 edge gate PASSED; CEO scope approval awaited)

**Recorded by:** `orchestrator`
**Date:** 2026-06-16
**Project:** AATS Solana Meme-Coin Ultra-Sniper

---

## Part 1 — Pre-G0 edge gate: PASS (project overlay `CLAUDE.md §8.1`)

The overlay mandates a pre-G0 edge gate before any spec/build; a NO-GO halts the project.

| Criterion | Evidence | Verdict |
|---|---|---|
| Honest GO / GO-PAPER-ONLY / NO-GO verdict exists | `.agency/01-specs/EDGE-VERDICT.md` lines 10–18 | **PASS** |
| Verdict re-confirmed against current ground truth + sim source | EDGE-VERDICT §"Red-team review", lines 184–250 (3 independent red-teams, code-level citations verified) | **PASS** |
| Success metrics defined (machine-checkable) | EDGE-VERDICT §4 — GATE-A (net-of-cost PnL, lower-95%>0) + GATE-B (model-vs-naive-baseline) | **PASS** |
| Kill criteria + capital-staging defined | EDGE-VERDICT §5 (K-0 breaker, K-1 collapse, decay triggers) + §6 (R0..R4 ladder) | **PASS** |
| Blocking conditions enumerated | EDGE-VERDICT §"Conditions for GO" — C-1..C-13 | **PASS** |

### Verdict carried forward: **GO-PAPER-ONLY**

Edge is **plausible and structurally defensible** (safety-selective late entry + exit discipline +
migration-survivor selection + smart-money-as-filter) but **UNPROVEN net of cost** on recorded data.
Red-team (verified against sim source + 2026 ground truth) showed several favorable numbers are
**sim artifacts (direction-contaminated, not merely magnitude-uncertain)** — which mandates proving
on RECORDED data with **real capital DISABLED by default**, not a hopeful GO and not a halt.

**Consequence for the build:** proceed to spec/build the paper/shadow-record system. Real capital
stays behind the DRY-RUN flag and is authorized by the CEO ONLY at capital-staging rung **R3**, after
both GATE-A and GATE-B pass on recorded data with the 13 hardened conditions satisfied. A
recorded-gate failure is a SUCCESSFUL outcome ("no edge net of cost"), not a project failure.

**The project is NOT halted. proceedToSpec = true.**

---

## Part 2 — G0 Scope gate: PENDING

G0 (charter §4) passes when spec + user/operator stories + measurable acceptance criteria are
complete and open questions answered — **approved by the CEO**.

| G0 criterion | Status |
|---|---|
| SPEC with numbered FR/NFR (trading + operator UI) | IN PROGRESS — T-100 (`quant-product-analyst`) |
| Measurable acceptance criteria, incl. GATE-A/GATE-B + HONESTY CLAUSE verbatim | TODO — T-101 |
| C-1..C-13 encoded as testable acceptance criteria | TODO — T-102 |
| Competitive-feature acceptance criteria mapped to lanes | TODO — T-103 |
| Operator-UI + Telegram acceptance criteria (controls drive real bot; de-risk only) | TODO — T-104 |
| Capital-staging R0..R4 + staged-rollout criteria | TODO — T-105 |
| Open questions closed | TODO (assemble in spec) |
| **CEO scope approval** | **NOT YET REQUESTED** — Orchestrator will assemble the one-screen G0 briefing after the spec is delivered and verified. |

**Re-entry:** when T-100..T-105 are delivered and Orchestrator-verified, this file is superseded by
`G0-PASS.md` (or `G0-FAIL.md` with remediation) and the CEO scope briefing is presented.
