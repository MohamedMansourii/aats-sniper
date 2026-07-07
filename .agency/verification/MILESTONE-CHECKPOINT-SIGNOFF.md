# AATS — MILESTONE CHECKPOINT SIGN-OFF (verification campaign)

> Governance sign-off per M1–M6. The M3 5-lane dual-G3 mega-audit (30 agents, run `wf_81b765a7-7f8`) substantially
> covered the read-only scope of M1/M2/M5/M6; M4 (live E2E) is genuinely un-exercised and remains open.
> **CTO lens = architecture/ADR conformance · CSO lens = key/custody/secrets · TPM = coverage completeness.**

| M | Gate | Verdict | Evidence / conditions |
|---|---|---|---|
| **M1** | G0-REVERIFY (ADR drift) | **PASS-WITH-DRIFT** | 14 ADRs checked via lane audits. Conformant: Redis bus (0001), Rust/Python split (0002), asymmetric-trust-by-type (0006), single-writer FSM (0007), three-layer stop (0008 — *layers wired but 3 of 4 producers unfed*), breaker event-time day-key (0012), regime-signal (0014). **DRIFT (RED):** ADR-0009 isolated-signer enforcer is a scaffold, not built. Documented in REGRESSION-LOG. |
| **M2** | G1-REVERIFY (module design) | **PASS-WITH-FINDINGS** | All 17 modules read + adjudicated across lanes A–E. Design intent sound; findings are implementation gaps, not architecture rot. |
| **M3** | G3-MASS (dual-G3) | **✅ COMPLETE** | Every finding passed `code-reviewer` + `backtest-qa-engineer`; RED findings survived an adversarial REFUTE pass. 2 RED / 32 YELLOW / 33 GREEN. |
| **M4** | G4-INTEGRATION (live E2E) | **⛔ NOT RUN — OPEN** | Un-exercised (flagged in Missing Coverage): E2E devnet paper trade, survivable-stop-under-process-kill, SNIPE/FAST latency SLAs, and the **aggregate safety posture** (enforcer_wiring / gate / reasoner / regime_wiring / lp_unlock_source are ALL `None` in `__main__.py` simultaneously — only the ExitEngine hard stop + breaker hard trip are live). Requires the running stack. |
| **M5** | G5-SECURITY | **PASS-WITH-CONDITIONS** | Consistent with `EXEC-CUSTODY-AUDIT-2026-07-06`. No exploitable breach in paper; key isolation GREEN. Conditions before capital: real signer (F1/C1), DEVNET→mainnet env-string gate (F2/C3), operator dev-token default + env-name split (F5/C6), signer egress (F4/C5), tx placeholder discriminators (F6/C8), hash-lock + pip-audit/cargo-audit, constant-time auth, full git-history secret sweep. |
| **M6** | G6-EDGE-STATUS | **PASS (verdict honest) / capital-bar UNMET** | The launch + momentum **NO-GO on n=4,187 is genuine and honest** (n=497 false-positive correctly reversed; realizable-exit only makes it more conservative; GO path reachable in tests so not rigged). **BUT the capital-licensing bar is UNMET:** `run_edge_proof` computes GATE-A/GATE-B **in-sample** (iid trade-resample bootstrap, no purge/embargo/walk-forward on the REAL corpus — the walk-forward engine is wired only to the synthetic corpus). No real-data GATE-B PASS may license capital until this is fixed. |

## Signed posture
- **CTO:** architecture is sound; the only structural drift is the unbuilt ADR-0009 enforcer. No re-architecture required — build the enforcer.
- **CSO:** no key exists; every real-money path is fail-closed and DRY_RUN-default. **Veto stands on any capital step** until the signer enforcer, the LIVE-execution-correctness cluster, and the operator/alerting surfaces are built + proven with tests, pinned to a frozen commit.
- **TPM:** M3 coverage is broad and honest; M4 (live E2E) is the real gap. Sign-off is **conditional** and explicitly does NOT authorize any capital path.

**Overall: the system is in the correct honest posture (capital disabled, edge NO-GO) and is safe as a paper platform, but is NOT ready for any step toward real capital. Two REDs + the in-sample edge-proof gate remain.**

---

## M5 / M6 dedicated re-verification (2026-07-07 · workflow `wf_4f3b1130-813`)
Both **PASS-WITH-CONDITIONS · ZERO new RED · ZERO newly-discovered secrets · ZERO NO-GO→GO regression.**

**M5 (crypto-security-engineer):** Full **git-history secret sweep over all 62 commits / all refs + tracked tree +
uncommitted files = CLEAN** — 0 keypair arrays, 0 base58 secret keys (90 long-base58 hits all proven public tx
signatures / `_FAKE_SIG` test constants / base64 wire-bytes), 0 `sk-`/`ghp_`/`xox`/AWS keys, 0 PEM blocks; every
`.env.example` secret field is a placeholder/Vault-path across all history. Key isolation confirmed by construction.
Carryover go-live conditions still OPEN (HIGH-for-go-live, N/A today): signer scaffold (F1/C1), DEVNET-overrides-
DRY_RUN w/ no genesis-hash assert (F2/C3), operator_token defaults to `dev-token` (F5/C6), non-constant-time auth
(LOW). **NEW (YELLOW/LOW):** the tracked legacy Go sub-tree `memecoin-bot/` uses the raw-`SOLANA_PRIVATE_KEY`-in-env
anti-pattern (empty placeholder, NOT wired into the AATS stack/compose/CI) — operator-confusion hygiene risk; banner
or remove. **Honest gap:** gitleaks/trufflehog/pip-audit/osv/cargo-audit not installed → manual regex sweep
substituted, no live CVE scan (condition C4/C9).

**M6 (backtest-qa-engineer):** **NO regression** — `tests/backtest` = 116 passed; NO-GO holds on all three
strategies (GATE-A absolute-PnL FAILS while GATE-B relative "PASSes" by declining trades; reaction on the live real
corpus n=436 = NO-GO, GATE-A model −1.755 SOL). Leak guards verified load-bearing at runtime; fail-closed-on-empty
confirmed. **Good news:** B has ALREADY fixed the reaction clustering concern — `reaction_gate` uses a source+time-
block clustered bootstrap. **Two forward-looking YELLOW conditions gate any FUTURE GO:** (a) the real-corpus proof is
scored **IN-SAMPLE** (the purged/embargoed walk-forward windower runs only on synthetic data) → replace with a
real-corpus OOS purged/embargoed walk-forward (≥5 folds); (b) **GATE-B needs an EFFECTIVE-sample floor**
(`n_model_selected` / `n_model≠baseline`) — current PASSes ride on ~10–20 effective decisions while `min_sample` sees
only total-n (the exact thin-cohort fragility that reversed n=497 at n=4,187). **Honest gap:** couldn't re-run the
launch/momentum proof on the *current* 11,453-line corpus (RPC_PRIMARY unset this session → path fails closed); an
RPC-backed re-run is advisable, though "more data → stronger NO-GO" makes a GO regression implausible.

**Carry-forward blockers on the go-live gate (governance):** (1) build + re-audit the real signer before any key;
(2) install + CI-enforce secret scanners + a live CVE scan; (3) hash-lock deps + `Cargo.lock` + SHA-pin CI actions;
(4) fail-closed `operator_token` + constant-time auth; (5) **real-corpus OOS purged/embargoed walk-forward + an
effective-sample GATE-B floor before any GO may license capital**; (6) banner/remove the legacy `memecoin-bot/` raw-key sub-tree.

**Milestone ledger now: M1 PASS-WITH-DRIFT · M2 PASS-WITH-FINDINGS · M3 COMPLETE · M4 OPEN (blocked on producer wiring) · M5 PASS-WITH-CONDITIONS · M6 PASS-WITH-CONDITIONS.** Capital stays DISABLED; edge is a genuine NO-GO; no GO fabricated.
