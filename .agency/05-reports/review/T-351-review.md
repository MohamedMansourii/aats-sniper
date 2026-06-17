# Code Review — T-351: Vitest tests for destructive controls + key pages

**Reviewer:** `code-reviewer` (Quality Gate, Gate G3)
**Task:** T-351 — Vitest tests for destructive controls (kill / flatten / breaker-reset / mode) + key pages
**Lane:** E (operator dashboard) · **Owner:** `frontend-engineer`
**Date:** 2026-06-16
**Verdict:** **PASS**

> One-line: Tests genuinely exercise every destructive control's wiring and the kill/go-live confirm guard, verified meaningful by mutation; build/typecheck/lint/tests all green by my own execution; CI gate wired. Ship.

---

## Verdict: PASS

No BLOCKER or MAJOR findings. The deliverable is a tests-only task; it adds no production behavior, conforms to the frozen contract for the controls it covers, and the tests would fail if a destructive control were mis-wired (proven below).

---

## Verification by execution (I ran these myself)

| Command | Result |
|---|---|
| `VITE_USE_MOCK=true npx vitest run` | **5 files / 24 tests PASSED** (13.3s) |
| `npx tsc -b` (typecheck) | exit 0 |
| `npx eslint .` (lint) | exit 0, no output |
| `VITE_USE_MOCK=true npm run build` | exit 0; `dist/public` produced |
| `npm ci --dry-run` | "up to date" — lockfile ⇄ package.json in sync (CI `npm ci` will work) |

All six engineer claims confirmed.

### Meaningfulness proven by mutation (not assumed)

A passing suite is worthless if it can't fail. I mutated production code in a backed-up copy, ran the relevant test, and restored byte-identical:

1. **Endpoint path** — changed `ENDPOINTS.kill` `/api/kill` → `/api/WRONG-kill`.
   → `api.destructive.test.ts` **failed 2 tests** (endpoint pin + `killSwitch → POST /api/kill`). Restored.
2. **Confirm guard** — added `onClick={confirmKill}` to the KillSwitch *trigger* button so it fires without the dialog.
   → `KillSwitch.test.tsx` **failed all 3 tests** (no-fire-on-first-click, confirm-once, cancel-no-call). Restored.

Both production files verified clean after restore (`onClick={confirmKill}` present only on the legitimate `AlertDialogAction`, line 74; no `WRONG` residue).

No `.only` / `.skip` / `xit`, no `as any`, no `@ts-ignore` in any test file — nothing silently disabled, no type-escape hatches.

---

## Conformance

| Dimension | Status | Note |
|---|---|---|
| Blueprint / frozen API contract (api-contracts.md §5, §12) | ✓ | Tests pin `kill`→`/api/kill`, `flatten`→`/api/flatten`, `flatten(mint)`→`/api/flatten/{mint}` URL-encoded, `breaker/reset`→`/api/breaker/reset`, `mode`→`/api/mode {mode}`. Exact match to the frozen endpoint set. Non-OK responses assert as thrown errors (de-risk must not silently succeed). |
| Mock-default offline safety (NFR-011 / AC-049) | ✓ | Asserts `USE_MOCK` default true and **zero** network calls for every destructive action in mock mode. |
| Confirm-gating (AC-041 kill; go-live) | ✓ | Kill and mode→LIVE both routed through AlertDialog; setMode/killSwitch not called until confirm; cancel calls nothing. |
| Design system (UI) | ✓ (n/a-light) | Tests assert against accessible roles/names (`getByRole`), reinforcing a11y; no design drift introduced (tests only). |
| Test presence + meaningfulness | ✓ | See mutation proof above. |
| CI gate wired | ✓ | `.github/workflows/ci.yml` Gate 5b `dashboard-test` runs `npm test` with `VITE_USE_MOCK=true`. |

### Scope boundary (why the enum mismatch is NOT a finding here)
The tests assert the **lowercase 3-value** `AgentMode` (`paper` / `dry-run` / `live`) — which is the dashboard's *current* code. The frozen contract (§2) mandates the canonical **4-value uppercase** enum (`SHADOW|PAPER|LIVE_DRY_RUN|LIVE`) plus integer-lamports/decimal-string money. That transcription is explicitly **T-352's** deliverable (TASKBOARD: T-352 depends on T-351; T-351 is "tests" only). Testing the controls as they exist today is the correct job for T-351 and is exactly what unblocks T-352. **Out of scope — not a defect.** When T-352 flips the enum, those test literals must be updated in lockstep; that is T-352's review surface, flagged here for the Orchestrator.

---

## Findings

### NIT-01 — Empty `api/**` vitest project matches zero files
`file: dashboard/vitest.config.ts:33-40`
The `api` (node) project's `include: ["api/**/*.{test,spec}.ts"]` matches no files today (`dashboard/api/` has no tests). The suite still reports correctly (5 dashboard files) and does not falsely pass on emptiness, so this is harmless forward-scaffolding for the T-352 control-plane seam. *Good would look like:* leave as-is if T-352 lands soon; otherwise drop the project until there's a node-side test to run, to avoid a config that looks like it covers a surface it doesn't.

### NIT-02 — Flatten-all / per-row flatten fire without a second confirm
`file: dashboard/src/components/Layout.tsx:46` · `dashboard/src/pages/Positions.tsx:107`
Only kill and mode→LIVE are confirm-gated; flatten is one-click. The tests *encode* this (asserting direct call), and it is correct per contract: flatten is de-risk-only (§5, §7) and AC-041/042 mandate confirmation for **kill** specifically. The engineer flagged this correctly as a spec/design decision, not a test gap. *No change required for T-351.* If product wants a flatten-all confirm, that is a `product-analyst` + `uiux-designer` change that returns through the board (Iron Rule 6) and would then update this test.

### NIT-03 — HARD RULES holding (informational, no action)
No win-rate field/assertion, no float-money assertion, no secrets/keys/bearer tokens in any test file (grep-verified). LLM/FAST execution path untouched (dashboard-only). Recorded as positive confirmation, not a finding.

---

## Re-review note
First review of T-351. No prior findings to reconcile.
