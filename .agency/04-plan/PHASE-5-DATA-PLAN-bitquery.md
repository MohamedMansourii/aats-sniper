# Phase 5 — the fast path to a REAL edge verdict (Bitquery archival)

> The edge proof RAN and returned **NO-GO / UNPROVEN** only because there are zero resolved outcomes. This is the
> concrete plan to supply them the fast, robust way — bypassing the recorder bug and the accrual wait entirely.
> When these steps are done, GATE-A/GATE-B produce the FIRST real GO/NO-GO with actual scoreboard numbers.

## Why Bitquery (vs fixing the recorder)
Bitquery archival gives **resolved historical pump.fun launches WITH their post-launch price paths** — i.e. both
halves the edge proof needs (the entry-moment decision inputs AND the forward outcome), for thousands of launches,
instantly. Fixing the live recorder only gets the entry half, slowly, and still needs forward-outcome resolution.
Bitquery collapses weeks → hours.

## Step 1 — Get access (owner)
- Sign up at bitquery.io; the **pump.fun / Solana DEX** datasets cover launches (bonding-curve creates),
  trades, and OHLCV since May 2024. Free tier to prototype; a paid plan for the full bulk pull.
- Provide the API key at runtime only (env / Vault — never committed; `.env.example` placeholder pattern).

## Step 2 — Pull the data (the ingest — a Codex/Claude build task)
For a window of launches (e.g. 5,000 pump.fun creates), pull per mint:
- **Entry-moment (decision) inputs** at/just after create: creator, initial reserves, first-slot buys/holders —
  the same fields the recorder's snapshot captures. (These feed the model + baseline decision, point-in-time.)
- **Forward outcome:** OHLCV / price path over the trade horizon (minutes→hours after create) — enough to
  simulate the paper trade's exit via `exit_engine` + `cost_model`.
Deliver as a local dataset (parquet/jsonl) with a strict schema; the API key stays out of the artifact.

## Step 3 — Resolve into TradeOutcome records (the outcome-labeling harness = Codex WP #3)
Run the harness in `.agency/04-plan/codex-work-packages/OUTCOME-LABELING-HARNESS.md`:
- Decision (strictly slot ≤ create): model_selected / baseline_selected.
- Outcome (strictly forward): net-of-cost PnL (Jito tip + fees + slippage + adverse-selection floor) + SOL-at-risk.
- Emit `TradeOutcome{mint, decision_slot, model_selected, baseline_selected, net_pnl_lamports, sol_at_risk_lamports}`.
- **Leak boundary is sacred** — Claude's `backtest-qa-engineer` gate audits it (a lookahead = a fabricated edge).

## Step 4 — RUN the edge proof (already built + tested)
Feed the TradeOutcome set to `compute_gate_a` + `compute_gate_b_delta`:
- **GATE-A:** aggregate net-of-cost PnL with a bootstrap lower-95% bound.
- **GATE-B:** model-vs-baseline net-PnL-per-unit-risk, purged/embargoed walk-forward.
- **Verdict: GO or NO-GO** — with the real scoreboard (net-of-cost PnL, expectancy, edge-vs-baseline, drawdown).
Honest numbers, reported for the first time — or an honest NO-GO. Either is the truth.

## Step 5 — the gate
- **NO-GO →** stop or pivot (per the hard rule). Iterate the model/features, re-run. No real funds.
- **GO →** *then* build the real signer (security-audit conditions), devnet dry-run, tiny-real, and — only with
  your explicit authorization — scale. Real capital never moves before this.

## Ownership
- **Owner (you):** Bitquery access + the go/no-go decisions + live authorization.
- **Codex:** the ingest + the outcome harness (WP#3) — heavy, self-contained.
- **Claude:** the leak audit + dual-G3 gate + running the edge proof + the honest verdict. The safety-critical
  judgment stays on Claude; the heavy build goes to Codex — exactly the split you asked for.
