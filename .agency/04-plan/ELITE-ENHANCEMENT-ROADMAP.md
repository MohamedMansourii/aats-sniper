# AATS — ELITE ENHANCEMENT ROADMAP (synthesized from the domain audit)

**Synthesized:** 2026-07-01 by Agency Runtime (the `orchestrator`/synthesis + honest-edge agents failed on an
account-access error — see §BLOCKER). Source: `.agency/00-brief/ELITE-ENHANCEMENT-DIRECTIVE.md` + the audit
workflow `wf_0e242949-63a` (4/5 domain readers completed; full findings in the workflow task output).

## HEADLINE FINDING (honest)
The risk-guardrails auditor's blunt verdict, which the other three corroborate: **on capital-safety and
execution discipline AATS is already AT or ABOVE the elite retail bots** (three-layer survivable stops,
dead-man switch, hard+soft daily-loss breaker, and — critically — a **cost-aware entry gate that rejects any
entry whose edge is below round-trip cost incl. Jito tip + fees + slippage + a 150bps adverse-selection
floor**, which directly answers ISAC's "~6% slippage kills it" finding). The genuine gaps are three:
1. **The "go-smart" alpha engine is stubbed** — E9 caller-score + E10 velocity are built & tested but wired
   to nothing, every real social adapter body raises `NotImplementedError`, and there is **no CA-extraction
   layer**, so the KOL-call edge can't be exercised end-to-end yet.
2. **Catastrophe-avoidance exit levers are missing** — no dev-sell/insider-dump auto-exit, no delayed-honeypot
   re-probe, no serial-deployer reputation.
3. **The chart-path model is unbuilt** — and cannot train for real until the shadow corpus reaches ~3,000
   real launches (still accruing via the live PumpPortal feed).

Every one of the 28 proposals is `derisk_only=true`. None lifts DRY_RUN, sizes up, widens a stop, or adds a
win-rate. The honest caveat threaded through all four audits: **most of this is catastrophe-avoidance, not
alpha** — the real edge lever (the cost gate) already exists.

## WAVE PLAN

### Wave 0 — latent safety-hole fixes (P0, cheap, do first)
- **EN1** fix bare-`assert` de-risk guard in `caller_score.py` (vanishes under `python -O` → asymmetric-trust hole). `nlp-sentiment-engineer`
- **E-M1-02** fix `deploy_template_fingerprint` (hash(creator+mint) changes every launch → useless for serial-deployer detection; fingerprint on metadata-URI template shape). `data-ingestion-engineer`
- **E-M1-07** remove/guard the dead `shredstream_endpoint` constructor param (accepted-but-never-read silent gap). `data-ingestion-engineer`
- **E-M1-03** escalate `bundler_cluster_id` dead-field to `solana-systems-architect` for an ADR (populate at decode-time vs deprecate).

### Wave 1 — the "go-smart" alpha engine (the CEO's core ask; biggest real gap)
- **EN3** real Telethon `TelegramAdapter` against a curated call-channel allowlist (injectable, offline-mocked in tests). `nlp-sentiment-engineer`
- **EN4** native Python **CA-extraction + call-record builder** (RawPost → `CallerCall`; base58 mint regex + no-LLM direction heuristic; do NOT port the forbidden Go code). `nlp-sentiment-engineer`
- **E-M1-04** wire a **live SmartWalletBackend** (Geyser `accountSubscribe`) behind the existing Protocol (stays disabled-by-default, capped 20, count-only). `data-ingestion-engineer`
- **EN2a** wire `VelocitySignal` into the MCS pipeline (de-risk sidecar; no data dependency). `nlp-sentiment-engineer`
- **EN5** implement `ParquetCallerOutcomeStore` + `SocialAdapterVelocitySource` (the promised production backends). `nlp-sentiment-engineer`
- **EN2b / EN6** wire caller-score + coordinate the caller×smart-money fusion — **build code now, stays neutral until real recorded outcomes exist** (does NOT bypass the edge-proof gate).

### Wave 2 — catastrophe-avoidance exit & filter upgrades (from the elite bots)
- **E14** ⭐ **dev-sell / insider-dump auto-exit** (Pepe-Boost trigger; on-chain-fact-driven, pre-set flag read on the FAST exit branch, detection off the hot path). Highest-value new de-risk lever. `risk-guardrails` + `data-ingestion`
- **E17** delayed-honeypot / tax-flip **sellability re-probe** on open positions → pre-set exit flag (reuses the shared sell-sim). `risk-guardrails` + `solana-execution`
- **E15** leak-free **serial-deployer / dev-wallet-rugged-before** reputation (reject/down-weight; backtest-qa must co-sign leak-free). `data-ingestion` + `risk-guardrails`
- **E16** **dev-funded-just-before-launch** fresh-wallet heuristic (creator first-funding event-time age). `data-ingestion` + `risk-guardrails`
- **E18** minimum distinct-holder floor on the pre-trade gate (P2, cheap). `risk-guardrails`
- **E19** LP-unlock-approaching de-risk awareness for time-locked LPs (P2). `risk-guardrails` + `data-ingestion`

### Wave 3 — the chart-path model, done honestly (the CNN ask) — ARCH NOW, TRAIN AFTER CORPUS
- **M2-CP-01** point-in-time post-migration **price-path feature tensor** (CENSORED-aware). `feature-quant` + `ml-prediction`
- **M2-CP-02** **regime-label spec** (accumulation/distribution/rug-in-progress/neutral) + remains-sellable gate, leak-verified with `backtest-qa`. `ml-prediction`
- **M2-CP-03** SLOW-loop **regime/exit classifier** (temporal head or small 1D-CNN; distribution/rug classes de-risk only; **accumulation class provably INERT**). `ml-prediction`
- **M2-CP-04** `RegimeSignal` contract + StateStore de-risk wiring (also finally wires the orphaned survivor model). `solana-systems-architect`
- **M2-CP-05** regime calibration + **its own frozen naive baseline + auto-disable** — must beat the EXISTING exit engine (already +24% vs naive) net of cost, or stay silent. `ml-prediction` + `backtest-qa`
- **M2-CP-06** fill the survivor `TFT_SWAP_POINT` with a real temporal model (post-corpus). `ml-prediction`
- **M2-CP-07** creator/dev-wallet **distribution-velocity feature** (monotone de-risk). `data-ingestion` + `feature-quant`
- **M2-CP-08** regime model card + reproducible pinned training harness. `ml-prediction`
> **Hard blocker:** M2-CP-03/06 cannot train for real until ≥3,000 recorded launches. The live feed is
> accruing them now. Honest caveat: the regime model's realistic marginal edge is SMALL — it must beat a
> strong existing exit baseline, and if it can't, it stays silent (correct outcome, not a failure).

### Wave 4 — detection completeness (proof + provenance)
- **E-M1-01** live-validate Geyser multi-venue (Raydium v4/CPMM/PumpSwap) against a real endpoint; label coverage UNPROVEN-LIVE until then. `data-ingestion`
- **E-M1-05** replace synthetic decoder fixtures with real captured-mainnet-signature decode-and-assert tests. `data-ingestion`
- **E-M1-06** raw dev-wallet history + funding-lineage ingestion (feeds E15/E16). `data-ingestion`

### NOT YET AUDITED (re-run when access restored)
- Execution / MEV / custody / wallet-linking domain (`solana-execution-engineer` reader failed on the account error) — needed before the go-live runbook is fully grounded.

## GATES (unchanged)
Every item is dual-G3 (`code-reviewer` + `backtest-qa-engineer`). The edge-proof (R3 Block A) remains the gate
before real capital. The chart-path model + caller-score wiring do NOT bypass it — they stay neutral/
bootstrap until the recorded corpus proves an edge.
