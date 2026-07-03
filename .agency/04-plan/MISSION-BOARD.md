# AATS — MISSION BOARD (divided missions for the agent swarm)

> ⚠️ **THE PER-ITEM STATUS MARKERS BELOW LAG — `.agency/STATE.md` is authoritative.** As of 2026-07-03:
> EN1 · Wave 1 · Wave 2A · Wave 2B (E15/E16/E18/E19) · Wave 2C are ALL DONE, and **Milestone B is COMPLETE and
> live-wired**. The ◻/⚠️ markers in this file are stale (mechanical reconciliation is a tracked cleanup step);
> trust STATE's DONE ledger + NEXT ACTIONS over this board.

> The single dispatch queue. Each mission is self-contained and works **cold, from files alone**. Dispatch in
> the order below (dependencies are marked). Every mission's Definition-of-Done is the **Acceptance Gate**
> (§Acceptance). Points feed the reward system in `MILESTONES-ACCEPTANCE-REWARDS.md`.
> Status keys: ✅ done · ⚠️ unreviewed · ▶ next · ◻ queued · ⛔ blocked.

## Legend
- **Owner** = the specialized agent that BUILDS it (from `.claude/agents/`). **Reviewers** are always
  `code-reviewer` + `backtest-qa-engineer` (dual-G3).
- **DoD** = Definition of Done (must all be true to claim the mission).

---

## WAVE 2B (finish) — rug-provenance filters

### M-E16-REVIEW · ⚠️ FIRST ACTION · Owner: review-gate (code-reviewer + backtest-qa)
Review the **already-committed, unreviewed** E16 fresh-wallet heuristic (`aats/ingestion/dev_funding_age.py`,
`aats/risk/dev_funding_age_gate.py`, tests). Use the E14b review-gate pattern (persisted script in STATE §5).
- **DoD:** both reviewers PASS on the frozen tree; if FAIL → fix per findings (owner `data-ingestion-engineer`)
  or `git revert` E16 and re-queue as a build. de-risk-only + strict point-in-time (funding-slot ≤ deploy-slot,
  never wall-clock) + refuse-by-default verified.
- **Points:** 20 (P1). **Deps:** none.

### M-E18 · ◻ Owner: `risk-guardrails-engineer`
Minimum distinct-holder floor on the sub-10ms pre-trade gate (`aats/risk/pretrade_gate.py`). Reject-only,
tighten-only, 0 RPC, refuse-by-default on undecoded holder_count. Brief: ELITE-ENHANCEMENT-ROADMAP §Wave2 E18.
- **DoD:** below-floor rejects; above-floor passes; undecoded → refuse; mutation-meaningful; dual-G3 PASS.
- **Points:** 10 (P2). **Deps:** none. **Resume via** the saved Wave 2B script (`resumeFromRunId`).

### M-E19 · ◻ Owner: `risk-guardrails-engineer` (+ `data-ingestion-engineer` for LP-unlock decode)
LP-unlock-approaching de-risk: entry reject/down-weight + open-position pre-set exit flag for time-locked LPs.
- **DoD:** approaching unlock rejects at entry AND raises the exit flag; burned/far-off LP does not; unknown
  schedule → refuse; point-in-time (event-time slots); FAST branch reads a pre-set flag; dual-G3 PASS.
- **Points:** 10 (P2). **Deps:** none.

---

## WAVE 3 — chart-path / regime model (the CNN ask, done honestly)
> ARCH NOW, TRAIN LATER. Build the tensor + label-spec + model scaffold + contract + baseline/monitor now.
> **M2-CP-03/06 cannot train for real until ≥3,000 recorded launches** (data-blocked — MILESTONE C). The
> regime model's realistic edge is SMALL and it must beat the existing exit engine net of cost, or stay silent.
Ready script: `.agency/04-plan/workflows/wave3-chartpath.js`.

| ID | Title | Owner | Pts | Deps |
|---|---|---|---|---|
| M2-CP-01 ◻ | Point-in-time post-migration price-path tensor (CENSORED-aware) | `feature-quant-engineer` | 30 (P0) | — |
| M2-CP-02 ◻ | Regime-label spec (accum/distrib/rug-in-progress/neutral) + remains-sellable gate, leak-verified | `ml-prediction-engineer` (co `backtest-qa`) | 30 (P0) | CP-01 |
| M2-CP-03 ⛔ | SLOW-loop regime/exit classifier (distrib/rug de-risk only; accumulation INERT) | `ml-prediction-engineer` | 30 (P0) | CP-01/02 + **corpus** |
| M2-CP-04 ◻ | `RegimeSignal` contract + StateStore de-risk wiring (also wires orphaned survivor model) | `solana-systems-architect` | 20 (P1) | CP-02 |
| M2-CP-05 ◻ | Regime calibration + frozen naive exit-timing baseline + auto-disable (must beat existing exit engine) | `ml-prediction-engineer` (co `backtest-qa`) | 20 (P1) | CP-03 |
| M2-CP-06 ⛔ | Fill survivor `TFT_SWAP_POINT` with a real temporal model | `ml-prediction-engineer` | 20 (P1) | **corpus** |
| M2-CP-07 ◻ | Creator/dev-wallet distribution-velocity feature (monotone de-risk) | `data-ingestion-engineer` (co `feature-quant`) | 10 (P2) | — |
| M2-CP-08 ◻ | Regime model card + reproducible pinned training harness | `ml-prediction-engineer` | 10 (P2) | CP-03 |

## WAVE 4 — detection completeness (proof + provenance)
Ready script: `.agency/04-plan/workflows/wave4-detection.js`.

| ID | Title | Owner | Pts | Deps |
|---|---|---|---|---|
| E-M1-02 ◻ | Fix `deploy_template_fingerprint` (creator/template-invariant via metadata-URI shape, not mint) | `data-ingestion-engineer` | 30 (P0) | — |
| E-M1-01 ◻ | Live-validate Geyser multi-venue (Raydium v4/CPMM/PumpSwap); label UNPROVEN-LIVE until real capture | `data-ingestion-engineer` | 30 (P0) | needs a real Geyser endpoint* |
| E-M1-05 ◻ | Real captured-mainnet-signature decoder fixtures (decode-and-assert per venue) | `data-ingestion-engineer` | 20 (P1) | — |
| E-M1-06 ◻ | Raw dev-wallet history + funding-lineage ingestion (feeds E15/E16) | `data-ingestion-engineer` | 10 (P2) | — |
| E-M1-07 ◻ | Remove/guard the dead `shredstream_endpoint` param (fail loud, no silent no-op) | `data-ingestion-engineer` | 10 (P2) | — |
| E-M1-03 ◻ | ADR for the dead `bundler_cluster_id` field (populate at decode vs deprecate) | `solana-systems-architect` | 10 (P2) | — |

\* E-M1-01 needs a paid/real Geyser endpoint (CEO-provided). If absent, do the code + honest labeling and mark
the live-capture step as pending credentials — do NOT claim multi-venue live coverage without real data.

## WAVE 5 — execution / custody / go-live grounding
| ID | Title | Owner | Pts | Deps |
|---|---|---|---|---|
| M-EXEC-AUDIT ◻ | Re-run the execution/custody/wallet-linking domain audit (died on the account error) | `solana-execution-engineer` (+ `crypto-security-engineer` lens) | 20 (P1) | — |
| (outputs) | Grounds the honest wallet-linking + DRY_RUN-flip go-live runbook | docs-delivery | — | audit |

## MILESTONE E — THE EDGE PROOF (the gate; not a build wave)
Run GATE-A (aggregate net-of-cost PnL + lower-95% bound) and GATE-B (model-vs-baseline net-PnL-per-unit-risk)
on the **recorded corpus** (`aats/models/gate_a.py`, `gate_b.py`). Owner: `backtest-qa-engineer` +
`quant-research-lead` (edge oracle). **Blocked on corpus size** (MILESTONE C). Output = **GO / NO-GO** →
escalate the honest verdict to the CEO. This is the fork toward real capital; nothing here lifts `DRY_RUN`.

---

## ACCEPTANCE GATE (applies to EVERY build mission — the reward is earned only when all are true)
1. **Dual-G3 PASS** — `code-reviewer` AND `backtest-qa-engineer` both PASS on a **frozen** tree.
2. **Full suite green** — `python -m pytest -p no:randomly -q` = `0 failed`, re-run by the Runtime (not trusted
   from the agent's report).
3. **De-risk-only proven** — a mutation test shows the signal can only reject/shrink/exit; removing the guard
   fails a test RED.
4. **Point-in-time honest** — no wall-clock substitution; leak test passes (future data provably excluded).
5. **No secrets, no scaffolding** — secret grep clean; `grep -rnE 'if False|MUTATION-TEST' aats/` = 0.
6. **No frozen-contract drift** — `aats/contracts/` untouched unless an architect ADR authorized it.
7. **ruff + mypy clean** on changed files.
Only when 1–7 hold does the mission count as done and its **points** are awarded (see rewards doc).
