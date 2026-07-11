# ▶ RESUME HERE — AATS (60-second orientation)

## ✅ PROGRAM COMPLETE (2026-07-07) — edge is a decisive NO-GO; nothing further to build
Per the CEO's IF-NO-GO directive, the decisive NO-GO verdict **is** completion. AATS is delivered as a proven-safe
PAPER platform. **No real capital was ever enabled and none moves** — there is no proven edge to license it.
Remaining items (signer C6 SPL-Token guard, GEYSER_TOKEN rotation, dashboard RED-2, M4 wiring, Priority-2) are ALL
**MOOT / optional** because they only matter for a go-live that the NO-GO verdict forecloses. Do NOT launch
Priority-2 work — it is hard-gated on a GO that does not exist. If resuming to POLISH (not to trade), the only
in-bounds items are the OPTIONAL list in §NEXT; otherwise the program is done.

## ⏸ RESUME CHECKPOINT (2026-07-07)

### THE HEADLINE — DECISIVE EDGE VERDICT = ⛔ NO-GO (project resolved)
The reaction/whale-front-run thesis — the last strategy with a real prior — **ran through the full capital-licensing
walk-forward and returned NO-GO.** Result (n=2,218 whale signals; pooled OOS n=1,184; **effective=310**; 6/6 folds,
purged+embargoed, clustered bootstrap, realizable exit): **GATE-B PASS out-of-sample** (delta +0.063, lower95 +0.003 —
the model has GENUINE OOS selection skill, the first strategy ever to pass GATE-B OOS) **but GATE-A FAIL** (model
net-of-cost −0.028/SOL — it still LOSES money). Real skill, unprofitable net of the ~6% cost gate. **All three theses
(launch, momentum, reaction) are decisively NO-GO. No solo-operator edge net of cost.** Per the CEO's IF-NO-GO
directive: **this IS completion** — AATS is a proven-safe paper platform; Priority-2 NOT built; **no capital moves**.

### DONE (committed)
- `8fa5462` — capital-licensing edge proof COMPLETE: `licensing.py` purged/embargoed walk-forward + effective-sample
  GATE-B floor (hard-clamp ≥21) + certified `reaction_harness.py` (dual-G3 PASS, no leaks, 164 backtest tests green).
- Verification M1–M3, M5, M6 signed + M6 gap-closer + M4 pre-flight (`.agency/verification/`).
- DECISIVE reaction verdict RAN (NO-GO) — captured here + in STATE; standalone artifact
  `.agency/verification/DECISIVE-REACTION-VERDICT-2026-07-07.md` ✅ WRITTEN + committed (50 lines).
- `AATS-COMPLETE-CONTEXT-FOR-AI.md` (repo root) — full single-file project brief for feeding to another AI (untracked; keep or attach as desired).

### SIGNER WORKFLOW — COMPLETED, dual-G3 FAILED (committed WIP `a9a7cca`, NOT audit-passed, DO NOT ENABLE)
`wf_cddc4d98-dc3` finished after 2 fix rounds. Enforcer is substantially real (338 execution tests pass incl 83
refusal tests; wallet secret only in signer_process; hot core holds pubkey only; SOL/allowlist/tip paths
un-bypassable; fails-loud-not-to-mock). **BLOCKER (custody audit):** the SPL-Token program is allowlisted but the
value-transfer pin covers ONLY System-program ix — so Transfer/TransferChecked/SetAuthority/Burn/CloseAccount can
sweep the token/wSOL position in ONE unguarded 0-priced signature → "one compromise ≤ float" fails for the token leg.
**FIX (resume):** add enforcer **C6 SPL-Token/Token-2022 value-move guard** (default-deny non-emitted tags; pin
Transfer/TransferChecked/CloseAccount destinations to wallet-owned accounts like C2; refuse SetAuthority/Burn; +
refusal tests). This is an **ADR-0015 frozen-C-set delta** → route to solana-systems-architect (contract) +
solana-execution-engineer (build), then re-audit with crypto-security-engineer. MOOT for capital (edge is NO-GO;
DRY_RUN default holds; no capital path enabled). MINOR: rotate the real `GEYSER_TOKEN` in `.env` (plaintext, gitignored, read-only feed token).
- Recorders: reaction recorder + launch collector (PID 22012) accruing autonomously (reaction 2,264+; launch 16,707+).

### SIGNER LANE — ✅ COMMITTED at `a9a7cca` (was flagged uncommitted; that note is now stale). Clean working tree.

### NEXT UNIT (on resume, in order)
1. **Write the standalone `DECISIVE-REACTION-VERDICT-2026-07-07.md`** (content = THE HEADLINE above) and deliver the
   honest final conclusion to the CEO. This is the program's answer.
2. **(OPTIONAL, MOOT for capital)** If the signer is wanted for completeness: fix the BLOCKER — add the enforcer **C6
   SPL-Token value-move guard** (ADR-0015 delta → solana-systems-architect + solana-execution-engineer) + re-audit.
   The WIP is committed at `a9a7cca`, clearly marked NOT-audit-passed. Skippable — the edge is NO-GO so the signer
   never goes live.
3. **Rotate `GEYSER_TOKEN`** in `.env` → Vault ref (MINOR hygiene).
4. **STOP — program complete.** CP-07 / M4 / Priority-2 are MOOT (edge NO-GO). **No capital moves, ever, without a proven GO that does not exist.**

---


## 🚦 CONTINUOUS-DRIVE STATE (2026-07-07 — STANDING ORDER: drive to completion, DO NOT STOP)
CEO issued **CONTINUOUS AUTONOMOUS DRIVE MODE**. Do NOT wait for "continue". Chain work until: scope done / a
real CAPITAL escalation / a hard blocker / the decisive reaction GO-NO-GO. End every turn on an ACTION, never a question.
Iron rules hold (no real capital without proven GO + audit + CEO auth; no fabricated GO; dual-G3 every deliverable).

**IN-FLIGHT WORKFLOWS** — gate each with dual-G3 on completion (ground-truth: `git status` + run the FULL suite
yourself; a build agent can crash on its final report while its edits ARE in the tree), commit on PASS via
`git pull --rebase --autostash` then push:
- `wjdwzovju` (runId wf_1207b3ec-b18) — **EDGE-PROOF COMPLETION**: real-corpus purged/embargoed walk-forward +
  effective-sample GATE-B floor + certify `reaction_harness.py` (tests). Lane: `aats/backtest` + `aats/models`.
- `wczblnbbu` (runId wf_cddc4d98-dc3) — **REAL ADR-0009 SIGNER**: enforcer (per-tx/rolling caps + program allowlist +
  tip pin + refusals) + remove `MockSignerClient/MockRpcClient` defaults + refusal tests. Lane: `rust/aats-signer` + `aats/execution`.
  NOTE: both share the main working tree — when gating, re-run the suite AFTER both land to avoid cross-edit false failures.

**CHAIN PLAN (next in-bounds units, §0.2 edge-first):**
1. Gate + commit the two in-flight workflows.
2. **CP-07 creator-outflow fix + Wave-4 detection completeness** (lane `aats/ingestion`) — next workflow.
3. **DECISIVE REACTION VERDICT** — once edge-proof-completion lands AND the reaction corpus has volume: run the
   walk-forward reaction GATE-A/GATE-B. **This resolves the project → SURFACE to CEO (pause reason #3).**
4. **M4 live E2E** — once A wires the `None` safety producers (see `M4-PREFLIGHT-safety-posture-2026-07-07.md`).
5. **Priority-2** (dashboard RED-2 + infra hardening) — ONLY if reaction returns GO.

**Edge state:** launch+momentum NO-GO robust (n=5,992 realizable; model loses money; GATE-B "PASS" is decline-everything on ~8 decisions); reaction early NO-GO (n=436). **Capital DISABLED. No GO fabricated.**
**Verification:** M1–M3, M5, M6 signed; M4 documented-blocked. Artifacts in `.agency/verification/`.

---


**You are the Agency Runtime** continuing the AATS Solana meme-coin bot. You dispatch Workflows/agents (dual-G3),
you write no production code yourself. **Read `.agency/STATE.md` next for full detail.** Branch `aats-sniper-build`.

## The one-line status
The whole bot is **built, safe, and security-audited**; the **edge proof runs on real data** and currently says
**NO-GO** — BUT the **momentum/reaction-entry strategy showed the first GATE-B PASS** (the model beats a losing
baseline). It's not proven only because the model is very selective and needs a **bigger corpus** to make GATE-A
statistically testable. A live collector is accruing that corpus autonomously.

## ⛔ Codex is DROPPED — you build everything yourself (CEO order, 2026-07-06).

## Your FIRST action on resume
1. Check the collector + corpus:
   ```
   tasklist | grep 22012        # collector alive? if not: Start-Process python C:/aats_shadow/_collector.py -WindowStyle Hidden
   wc -l < C:/aats_shadow/labeled_corpus.jsonl    # corpus size
   ```
2. **If corpus ≥ ~3000 → RE-RUN the momentum edge proof (the decisive test):**
   ```
   cd /c/dev/aats; export RPC_PRIMARY=$(grep '^RPC_PRIMARY=' .env | cut -d= -f2- | tr -d '\r'); export DRY_RUN_ENABLED=true
   python -m aats.backtest.run_edge_proof --corpus C:/aats_shadow/labeled_corpus.jsonl --strategy momentum --entry-horizon 60 --out C:/aats_shadow/momentum_result.json
   ```
   Record the verdict (`.agency/05-reports/qa/`), commit, update STATE. **Never fabricate a GO** — NO-GO stays NO-GO.
3. **If corpus < 3000 →** let it accrue; meanwhile advance the roadmap in `STATE.md §3` (bonding-curve price fidelity
   Workflow, then Wave-4 detection / CP-07 — all Claude-owned).

## The rule that matters most
No real money moves, ever, until Phase-5 returns a real **GO** (model beats baseline net-of-cost, statistically) AND
the security audit passes AND the CEO authorizes. Until then: honest verdicts only, `DRY_RUN` locked.
