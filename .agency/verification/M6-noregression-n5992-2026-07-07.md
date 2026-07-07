# M6 gap-closure — momentum no-regression @ n=5,992 (realizable exit) · 2026-07-07

**Closes the honest gap in the M5/M6 sign-off** (the M6 agent couldn't RPC-resolve the current corpus). V re-ran
the momentum edge proof RPC-backed on a 6,000-launch snapshot of the live corpus (now 12,377). **Verdict: NO-GO —
no regression; the NO-GO is *stronger* at larger n with the realizable-exit model.**

## Result (runner exit code 3 = NO-GO)
```
strategy : momentum (T_ENTRY=60s, tradeable=4804, skipped=1188)
records  : 5992 (resolved=5992, censored=0)   exit_model: realizable
selected : model=8   baseline=269
GATE-A [model]    n=5992 (sel=8):   total −0.071 SOL   per_sol_risk −0.089   lower95 −56,086 lamports  -> FAIL
GATE-A [baseline] n=5992 (sel=269): total −26.04 SOL   per_sol_risk −0.968   lower95 −4.89M lamports   -> FAIL
GATE-B delta = +0.0433 (lower95 +0.038)  -> PASS
VERDICT  : NO-GO
```

## What it proves
1. **No regression.** Momentum is NO-GO at n≈6,000 with the realizable-exit fidelity — the model now *loses money in
   absolute terms* (−0.089/SOL), so GATE-A correctly FAILS. Consistent with the DECISIVE n=4,187 and realizable
   n=6,547 results; more data has only strengthened the NO-GO.
2. **The GATE-B "PASS" is exactly the mirage M6 warned about.** The model "beats" the baseline only by *declining
   trades* — it selected **8 of 5,992** and still lost money; the baseline lost −26 SOL by trading. A relative-edge
   PASS built on **8 effective decisions is NOT capital-licensable** — this empirically confirms the
   **effective-sample GATE-B floor** finding routed to Session B (the same thin-cohort mechanism that reversed n=497).
3. **The runner is honest at scale:** 5,992/5,992 resolved on-chain block_time (0 censored), fail-closed discipline intact.

## Standing conditions (unchanged, reinforced)
Before any future GO may license capital: (a) real-corpus OOS purged/embargoed walk-forward replacing the in-sample
bootstrap; (b) the effective-sample GATE-B floor. Both routed to Session B. Real capital stays DISABLED; edge NO-GO.
