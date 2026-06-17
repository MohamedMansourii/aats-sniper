# AGENCY AUTONOMY DIRECTIVE (standing order)

**Issued by:** CEO (this session). **Scope:** the entire AATS ultra-sniper build, G0 through G6.
Every dispatched agent — especially the `orchestrator` running gate reviews — must honor this.

## The order
Proceed **fully autonomously** to a deployable, paper-proven bot. The CEO has **delegated all gate
approvals to the agency.** There is **no human approval pause** at any gate.

- **G0 / G1 / G2 / G6 are auto-approved on the agency's own PASS recommendation.** When the
  `orchestrator` (or the relevant reviewer) issues a PASS / READY verdict for a gate, that gate is
  **APPROVED** — record it as `G<N>-APPROVED (agency-autonomous)` and continue immediately to the
  next wave. Do **not** write "awaiting CEO" or block.
- **Open questions are resolved by their recommended default.** The `quant-product-analyst` and
  others still record open questions *with* a recommended default; the agency **adopts the default**
  and proceeds. Only escalate a question if there is **no** safe default (genuinely ambiguous AND
  capital-affecting AND not coverable by a conservative choice) — and even then, choose the most
  conservative, capital-protecting option and continue, noting it in STATUS.
- **Reports are agency status, not requests.** Frame all output as what the agency did/decided.
  Never address the CEO directly or ask for sign-off.

## What this directive does NOT waive (locked HARD RULES — still absolute)
These are technical/safety gates, not approval gates. Autonomy does **not** loosen them:
1. **Real capital DISABLED by default.** The live-execution path stays behind a hard DRY-RUN/paper
   flag and is **not** enabled until net-of-cost PnL **and** model-vs-naive-baseline are **proven
   positive on RECORDED data.** "Bot ready to trade" = fully built, paper-proven, one funded
   throwaway wallet + one explicit flag away from live — **not** auto-trading real money.
2. **No win-rate target or claim.** Sole success metric: net-of-cost PnL + model-vs-baseline on
   recorded data.
3. **Safety built first:** daily-loss circuit breaker, survivable stop, dead-man's switch proven
   before any live-capable path exists.
4. **Asymmetric trust:** no rule/LLM/copy-trade signal may ever increase risk; LLM never on the
   FAST-loop critical path.
5. Point-in-time correctness; Rust hot path; integer/Decimal money; no secrets in code/logs/images;
   every "done" carries the command + output that proves it.

## Gate verdict bookkeeping
Continue to record every gate decision in `.agency/05-reports/gates/G<N>-*.md`. The verdict author
is the agency (orchestrator + the gate's specialized reviewer), and the approver is
**"agency-autonomous per AUTONOMY-DIRECTIVE.md"** — not the CEO.
