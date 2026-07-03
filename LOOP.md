# AATS — LOOP.md (loop-engineering governance for the elite-enhancement program)

> This file makes the AATS orchestration a **governed loop**, not just a swarm with a STATE file. It is the
> loop's constitution: purpose, level, denylist, human gates, and the dispatch discipline. Live status lives in
> `.agency/STATE.md`; budget in `loop-budget.md`; run history in `loop-run-log.md`. Registered in
> `~/.claude/loops/CONTROL-PLANE.md`.

## Purpose + stop condition
**Purpose:** drive the AATS elite-enhancement program (competitor-parity, de-risk-only upgrades) to completion
and to the edge-proof gate. **Stop condition:** all MISSION-BOARD items G3-PASS **and** integrated-live **and**
GATE-A/GATE-B on the RECORDED corpus yield GO/NO-GO. Then the loop idles (no self-work) until a new CEO brief.

## Level = L2 (assisted) — with capital HARD-DENYLISTED regardless of level
- **What the loop MAY do autonomously (L2):** author + run a sequential dual-G3 Workflow per wave (build →
  code-reviewer + backtest-qa in parallel → 3-strike fix), independently re-run the full suite, and **commit +
  push code/doc changes** on a feature branch (`aats-sniper-build`). This is assisted auto-fix in the shared
  working tree (NOT worktree-isolated → not clean L3).
- **Not L3** because fan-out is serialized in a shared tree, not worktree-isolated, and there is no unattended
  cadence. Graduation criteria to L3 are below.

## DENYLIST — never auto-edit / never auto-do (human-gated, like sol-sniper's SIM+BUILD-ONLY)
The loop is **SIM + BUILD ONLY.** These are forbidden to any agent and to the Runtime without explicit CEO action:
1. **Flipping `DRY_RUN_ENABLED` to false** / enabling any live execution mode.
2. **Any mainnet transaction, signing, send, or wallet/keypair action.** The signer/Vault keys and
   `WALLET_*` values are never touched.
3. **Editing `.env`** or committing any secret (`.env.example` placeholders only).
4. **Editing frozen contracts** under `aats/contracts/` without a `solana-systems-architect` ADR.
5. **Deleting/rewriting the safety primitives** (circuit breaker, survivable stop, dead-man switch, cost gate).
The safety substance (asymmetric-trust, no-win-rate, point-in-time) is audited airtight (2026-07-03) and must stay.

## HUMAN GATES (the loop reports/proposes; the CEO decides)
- **DRY_RUN → LIVE flip** and any real-capital scaling.
- **The edge-proof GO/NO-GO** (Milestone E) — the loop RUNS the proof and reports the verdict; it does not act on it.
- **Funding a wallet / provisioning paid infra** (external to the loop).
- Any three-strikes `G3-FAIL` that survives re-plan → escalate to CEO with options.

## Dispatch discipline (the maker/checker + swarm laws, encoded)
- **Maker/checker:** dual-G3 = build agent (maker) + `code-reviewer` **and** `backtest-qa-engineer` (two
  independent checkers, default skeptical, RE-RUN tests). Supersedes the single `loop-verifier`. Maker never
  grades itself.
- **STANDING E2E GATE (added 2026-07-03 after the review caught unwired exits):** any feature that must ACT in a
  live loop (an exit branch, a flag producer, a gate consumer) **cannot be marked G3-PASS on unit tests alone** —
  it requires an END-TO-END integration test proving it fires in the running controller loop. "Green unit tests ≠
  wired live."
- **Guards:** build/fix agents must NOT run git; reviewers do NON-DESTRUCTIVE review (never edit the tree).
- **Sequential items** (shared tree, no worktree isolation) → no overlapping-file races.
- **Every wave writes a dual-G3 acceptance ARTIFACT** under `.agency/05-reports/{review,qa}/` (prose claims are
  not acceptance) and **appends one JSON line to `loop-run-log.md`**, and reconciles the companion docs to STATE.
- **Three strikes:** on `G3-FAIL` after 3 strikes, HALT the wave and escalate (do not silently continue).

## Cadence
Currently **human-launched** (the CEO says "continue" / launches the next wave). Given real capital is at stake,
the build gate SHOULD stay human-initiated. An optional **L1 daily-triage cadence** may be added later
(`/loop 1d Read .agency/STATE.md; run loop-budget start; triage §2 NEXT ACTIONS; PROPOSE the next wave; run
loop-budget end`) to keep STATE current and propose — never auto-dispatch — the next action.

## Graduation criteria (L2 → L3)
- Parallel lanes run in isolated git worktrees with a serialized integrator (not the current shared tree).
- Budget caps + kill-switch proven to fire (`loop-budget.md`).
- The checker has caught ≥1 real bad change — **MET** (the 2026-07-03 review caught the unwired Wave-2 exits).
- Capital/outward actions remain human-gated **regardless of level** (never graduates).

## Pointers
Live status: `.agency/STATE.md` · Budget: `loop-budget.md` · Run history: `loop-run-log.md` ·
Missions: `.agency/04-plan/MISSION-BOARD.md` · Architecture: `.agency/04-plan/ELITE-ARCHITECTURE.md` ·
Reviews: `.agency/05-reports/review/`.
