# ADR-0006 — Asymmetric LLM/signal trust enforced by the Intent / ReasoningVerdict type system

**Status:** Accepted (G1) · **Date:** 2026-06-16 · **Author:** `solana-systems-architect`

## Context
No rule, no LLM, no copy-trade signal, no MCS may EVER increase risk — no size-up, stop-widen,
leverage, or hard-stop override (locked decision 4; BUILD-DIRECTIVE HARD RULE; ROSTER §5.1). A
docstring or a runtime clamp alone is fragile: a future contributor can route around a clamp. The
requirement is to make a risk-increase **inexpressible**.

## Options
1. **Runtime clamp only** — validate the LLM output and force risk-increase to HOLD. Necessary, but
   if the type can express SIZE_UP, a code path can construct it before the clamp, or a new path can
   skip the clamp.
2. **Type-level prohibition + clamp backstop** — the `ReasoningAction` enum has only HOLD/VETO_ENTRY/
   REDUCE_SIZE/FORCE_EXIT (no risk-increase member exists); the `Intent` union has only ENTRY (cost-
   gated SNIPE path) / EXIT / REDUCE / VETO, with the reasoning/LLM/social path handed only a factory
   that produces EXIT/REDUCE/VETO — it holds no reference to the ENTRY constructor.

## Decision
**Option 2.** The de-risk-only enum/union is the primary defense (`data-models.md §6`); the clamp
(FR-017, AC-019/054) is the backstop that catches a raw LLM string outside the enum and forces HOLD
with an audit flag. `EntryIntent` cannot be constructed without a populated `CostStack` and
`target_slot ≥ slot+5`. ¼-Kelly sizing is monotone non-increasing in every secondary signal (AC-021/031).

## Consequences
- (+) `SIZE_UP`/`WIDEN_STOP`/`ADD_LEVERAGE`/`OVERRIDE_HARD_STOP` are **not expressible values** on the
  reasoning path — the type system rejects them at compile/parse time, not at review time.
- (+) Adding a risk-increasing Intent variant would be a contract change requiring an ADR + delta
  notice — a visible, gated act, never a quiet one.
- (−) The reasoning path needs a separate factory module with no ENTRY access; a small structural
  cost. Accepted — it IS the guarantee.
