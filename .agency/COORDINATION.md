# AATS — MULTI-SESSION COORDINATION (read before you build)

> **TWO Claude sessions are working this repo concurrently.** This file is the async coordination channel — READ IT
> before starting work, CLAIM your lane, and stay in your lane's files to avoid collisions. Updated 2026-07-06.

## THE RULE OF LANES (do not edit files outside your lane without claiming here first)

### 🅰 SESSION A — BUILD lanes (Execution go-live + Elite completion)
Owns and edits ONLY:
- `aats/execution/**`, `rust/aats-signer/**` — the real isolated signer + custody go-live build (Phase-4 blocker).
- `aats/ingestion/**` — Wave-4 detection completeness + CP-07 creator-outflow fix (elite enhancement completion).
- Its own acceptance artifacts under `.agency/05-reports/{review,security,qa}/` (A-prefixed filenames).
**Goal:** drive Execution & Custody go-live build to 100% (build only — real capital STAYS DISABLED until GO) and
finish the remaining elite-enhancement items to 100%.

### 🅱 SESSION B — EDGE lane (Phase 5 — the gate)
Owns and edits ONLY:
- `aats/backtest/**`, `aats/models/**` (gate/harness/params) — edge-proof strategy, fidelity, features.
- **The collector `C:/aats_shadow/**` and the corpus `labeled_corpus.jsonl`** — B is the SOLE owner of the data
  engine (monitor it, keep it alive, improve fidelity/features, re-run GATE-A/GATE-B at thresholds).
- Its own acceptance artifacts under `.agency/05-reports/qa/` (edge/B-prefixed filenames).
**Goal:** drive the momentum edge proof to a REAL, rigorous GO/NO-GO. Accrue corpus → bonding-curve entry-price
fidelity → re-run. **NEVER fabricate a GO.**

## SHARED / DANGER files (coordinate here before touching)
- `aats/contracts/**` — ADR-gated, frozen. If either lane needs a contract change, write a claim line below FIRST
  and prefer an additive change.
- `.agency/STATE.md`, `loop-run-log.md` — both append. Pull --rebase before writing; keep edits to distinct sections.

## GIT PROTOCOL (both sessions push to `aats-sniper-build`)
1. Commit ONLY your lane's files (never `git add -A` blindly).
2. **`git pull --rebase origin aats-sniper-build` BEFORE every push.** Non-overlapping lanes rebase cleanly.
3. If a rebase conflict appears in a SHARED file, stop and reconcile via a claim line below.
4. Run the full suite before pushing; never push red.

## CONVERGENCE (the user's endpoint)
Once A's build lanes hit 100% AND B's corpus reaches decisive volume, BOTH sessions focus on the edge proof:
A verifies execution/custody + security for a potential go-live; B runs the decisive GATE-A/GATE-B. A real GO (if it
exists) then unlocks the security-gated live staging. If the edge is NO-GO, that is the honest, final answer — no override.

## LIVE CLAIMS / STATUS (append newest first; keep it short)
- 2026-07-06 · A · CLAIMING execution go-live build (real signer) + elite completion (Wave-4, CP-07). Launching Workflows.
- 2026-07-06 · B · (Session B: append your claim here — recommended: the EDGE lane per RESUME-HERE.md.)
