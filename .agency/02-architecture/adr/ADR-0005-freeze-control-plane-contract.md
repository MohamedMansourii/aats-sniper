# ADR-0005 — Freeze the control-plane API contract at G1; one contract for server, dashboard, Telegram

**Status:** Accepted (G1) · **Date:** 2026-06-16 · **Author:** `solana-systems-architect`

## Context
Three lanes build against the control plane: the server (Lane D, `agent-orchestration-engineer`), the
existing dashboard (Lane E), and the Telegram channel (Lane F). They build in parallel. An existing
typed client (`dashboard/src/lib/api.ts`) already pins endpoint paths. Without a frozen contract the
three lanes drift and integration (G4) fails.

## Options
1. **Let the server define the contract as it builds** — server-first, but the dashboard and Telegram
   lanes cannot start until the server is done; serializes the build and risks rework.
2. **Freeze the contract at G1** — reconcile exactly with the existing `api.ts` ENDPOINTS, publish
   request/response/SSE schemas, and make all three lanes build to it. Architect-owned; change-
   controlled by ADR + delta notice after G1.

## Decision
**Freeze at G1** (`api-contracts.md`). The endpoint set matches `api.ts` exactly. Principles baked in:
GET=read-only, POST=de-risk-only (risk-increase rejected at the contract layer, 403); money is
integer-lamports/decimal-string never float; event-time stamping; operator auth on every POST. The one
reconciliation delta — the dashboard's `AgentMode` (`paper|dry-run|live`) → canonical 4-value
`SHADOW|PAPER|LIVE_DRY_RUN|LIVE` — is a Lane-E transcription, documented in the contract.

## Consequences
- (+) Lanes D/E/F build in parallel with zero contract ambiguity; G4 integration is a wiring check.
- (+) "Operator surfaces may only de-risk" becomes a contract-layer guarantee, not a UI convention —
  there is no risk-increasing endpoint to call.
- (−) A frozen contract resists mid-build improvements; mitigated by the ADR+delta-notice change
  protocol (only the architect changes it, and every affected task is listed).
- (−) The dashboard takes a small enum/type transcription; called out explicitly so Lane E plans it.
