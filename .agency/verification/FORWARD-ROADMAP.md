# AATS — FORWARD ROADMAP (post-M3 verification)

> Exact current state, what was verified, what remains, and the next concrete step — prioritized by capital-gating
> impact. Everything Claude-owned (Codex dropped). Real capital stays DISABLED throughout; the CEO alone authorizes any live step.

## SEQUENCING LAW — EDGE-FIRST, GO-LIVE-SECOND (CEO-approved 2026-07-07; ULTRA prompt §0.2)
The reaction edge proof is the critical path; go-live plumbing is expensive and MOOT if the edge is NO-GO. Therefore:
- **Priority #1 (now):** B fixes the out-of-sample edge proof (purged/embargoed + clustered/block bootstrap on the
  REAL corpus) → certify the reaction harness (tests) → accrue the reaction corpus → **run the decisive reaction GATE-A/GATE-B.**
  A builds the ADR-0009 signer in parallel (the #1 go-live blocker, valuable regardless). V gates every deliverable.
- **Priority #2 (HARD-GATED on a real reaction GO):** dashboard control-surface (RED-2), infra hardening, M4 live E2E,
  security re-audit → staged devnet → tiny-real → scale, CEO-authorized only. **If the reaction proof is NO-GO, none
  of Priority #2 is built** — the honest conclusion (proven-safe paper platform) is recorded and nothing else is spent.
- **Runtimes:** Session A = BUILD · Session B = EDGE · Session V = Verification/Governance (owns the gate + git-history
  secret sweep + M4 + the deferred Priority-#2 dashboard/infra when opened). Distinct from the M3 audit-lanes A–E.

## Current state (verified, honest)
- **Whole system built + safe.** 17 modules, Rust hot core, triple-loop, full safety spine; ~3,177 tests. The
  safety controls that matter (breaker hard-trip, DMS separate fail-closed domain, leak boundary, realizable-exit
  invariant, asymmetric-trust shape) are **verified GREEN**.
- **Edge = genuine NO-GO** on launch + momentum (n=4,187) — the honest, correct result. Corpus at ~7,100 launches, accruing.
- **Pivot in progress:** the smart-money/whale/KOL reaction thesis (the one lever with a real prior) — Session A
  building the recorder, Session B will build the front-run harness (`.agency/04-plan/REACTION-CORPUS-SPEC.md`).

## The gating blockers (must ALL be true — with tests, on a frozen commit — before ANY capital step)
1. **[RED · Session A] Real ADR-0009 signer enforcer.** Build `rust/aats-signer` as the un-bypassable out-of-process
   per-tx + rolling lamport cap + program/tip allowlist. Prove with **refusal tests** (over-cap tx → refuse;
   off-allowlist program → refuse). **Remove the `MockSignerClient` AND `MockRpcClient` defaults** from the production
   venue so a misconfigured LIVE fails loudly, not silently to mock.
2. **[RED · Cross-cutting, Session B] Out-of-sample edge proof.** Wire `run_edge_proof` through the **purged/embargoed
   walk-forward** on the REAL corpus (currently the walk-forward engine touches only the synthetic corpus; the real
   verdict is an in-sample iid bootstrap). For the reaction pivot add a **clustered/block bootstrap** (signals cluster
   in time; same-source reputation couples them — iid CIs are optimistic). Fix the momentum ~64s nominal-stamp timing
   optimism; calibrate the 150bps adverse-selection floor. **No real-data GATE-B PASS licenses capital until this lands.**
3. **[RED · Frontend] Operator control surface.** Make KILL/FLATTEN actually control the real backend: same-origin
   `/api/*` reverse proxy injecting the operator token server-side (browser never holds the secret) + CORS if
   cross-origin + gate destructive buttons on a live `/api/state` probe. **Remove the mock fake-success no-ops** (they
   render green "kill engaged" while the agent trades). Until then the dashboard is not a trusted control surface.
4. **[Infra · DevOps] LIVE-execution correctness + surfaces.** Add `getSignatureStatuses` confirmation poll on BOTH
   entry+exit (re-check the ORIGINAL signature before any resend — double-land guard); persist the idempotency set;
   wire the orphaned Jito atomic-buy bundle or drop the tip; add a mainnet genesis-hash assert on the DEVNET→mainnet
   env path. Fix the **env-name split** (`CEO_AUTH_TOKEN`/`OPERATOR_API_TOKEN` in compose vs `CEO_TOKEN`/`OPERATOR_TOKEN`
   in code — silent false assurance), the **Alertmanager all-null receiver** (P1 pages dropped), Prometheus
   unauthenticated `/-/reload|/-/quit`, the dev-token default, `@sha256` image pinning, the neutered secret-scan,
   constant-time (`hmac.compare_digest`) auth, and complete the `/api/risk-config` tighten-only allowlist (6 of 14 fields widenable).
5. **[Session B/E] Reaction harness certification.** `reaction_harness.py` was merged mid-audit with ZERO tests and a
   docstring falsely claiming "tested RED-before/GREEN-after" — write the leak/reputation-leak/model-subset/frozen-drift
   tests and dual-G3 it before it certifies anything.
6. **[M4] Live E2E integration:** devnet paper trade (snipe→fast→slow handoff), survivable-stop under process-kill,
   latency SLAs, and **wire the aggregate safety posture** (enforcer_wiring / gate / reasoner / regime_wiring /
   lp_unlock_source are all `None` in `__main__.py` — Layers 1–3, honeypot/rug + deployer/LP-unlock entry gates, and
   3 of 4 catastrophic-exit feeds are inactive; every one must be wired + proven before capital, not incrementally after).

## The order of operations
Verification lanes A–E are **YELLOW/RED but non-exploitable in paper** → keep running paper. Forward build proceeds:
Session A (signer + reaction recorder) ∥ Session B (OOS edge proof fix + front-run harness) → M4 E2E once producers
are wired → M5 security re-audit on the hardened stack → **the reaction edge proof**. **If GO** (real, OOS, on real
data) → CEO decision on staged devnet→tiny-real→scale. **If NO-GO** → the honest program conclusion: no solo-operator
edge; AATS is a proven-safe paper platform. Either way the truth is reported; no GO is fabricated; no capital moves without the CEO.
