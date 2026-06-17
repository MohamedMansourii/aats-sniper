# T-328 — Code Review (RE-REVIEW after QA-MAJOR-1 fix)

**Task:** Multi-wallet / bundle execution + partial fills + anti-cluster (blast-radius caps)
**Owner:** `solana-execution-engineer`
**Reviewer:** `code-reviewer`
**Round:** re-review of the QA-MAJOR-1 test-only fix
**Date:** 2026-06-16
**Verdict: PASS**

_Verified by reading the ACTUAL files under `C:/dev/aats` and running the suite + mutation
proof myself — not trusting the engineer JSON._

---

## Overall assessment
The QA-MAJOR-1 fix is correct, real, and mutation-proven: the multi-wallet activation gate is
now load-bearing under test. Zero production lines changed and none needed to be. One MINOR
lint-cleanliness finding on the changed test file; it does not block.

---

## What was claimed vs. what I confirmed

| Claim | Verified | Evidence |
|---|---|---|
| 2 new mutation-killing tests added to `TestNMaxDefault` | YES | `test_multi_wallet.py:157-202` |
| `_resolve_n_max` added to test imports + docstring item 16 | YES | `:52`, docstring `:19` |
| Clean run 45 passed | YES (after env settle) | `pytest tests/execution/test_multi_wallet.py` → 45 passed (5x stable) |
| Gate-defeat mutant makes the 2 new tests RED | YES | mutant applied to `multi_wallet.py:592`, ran → exactly 2 FAILED, other 43 PASS; restored → 45 pass |
| Zero production lines changed | YES (test-only) | no edits to `aats/execution/`; gate logic pre-existing and correct |
| Zero secrets in diff | YES | secret-pattern scan of changed file → no hits |

---

## Mutation-kill proof (reviewer-run, source-confirmed)
Applied the documented gate-defeat mutation to production
`aats/execution/multi_wallet.py:592` (`if enabled != "true":` →
`if False and enabled != "true":`) and ran the file:
- `test_activation_gate_is_load_bearing_n_max_env_alone_returns_default` → **RED** (returns 3)
- `test_n_wallets_gt1_without_activation_flag_raises_config_error` → **RED** (no exception)
- **The other 43 tests still PASS under the mutant** — this is exactly the QA-MAJOR-1 gap:
  before this fix, every multi-wallet test set BOTH `N_WALLETS_MAX` and
  `N_WALLETS_MAX_ENABLED=true`, so a defeated activation gate would have shipped undetected.
- Production restored → 45/45 pass. The two tests kill the mutant and ONLY the two tests do.

**The R3 activation gate (OQ-010) is now proven load-bearing: `N_WALLETS_MAX>1` in `.env`
alone, without the explicit `N_WALLETS_MAX_ENABLED=true` flag, is clamped to 1.**

---

## Conformance
- **Blueprint / OQ-010:** ✓ `N_WALLETS_MAX_DEFAULT = 1`; multi-wallet built+tested but gated until R4.
- **custody-policy §1 (blast-radius C = per_trade_cap):** ✓ `MintExposureLedger` raises
  `AntiClusterCapExceeded` when `current + requested > cap`; cap from `PER_TRADE_CAP_LAMPORTS`.
  Cap check is pre-execution (`execute_count == 0` proven).
- **DRY-RUN no submit:** ✓ orchestrator has no submit path; delegates to `venue.execute()`;
  `rpc.send_calls == 0` asserted.
- **Key isolation (ADR-0009):** ✓ execution module loads zero key material in-process;
  signing crosses a Unix-domain socket to `aats-signer`.
- **Money int / Decimal-string:** ✓ float→TypeError guards at every money boundary; prices
  Decimal-as-string.
- **ExecutionVenue ABC preserved:** ✓ 8 abstract methods; `SimulationVenue` implements all;
  re-asserted in `TestSimulationVenueABCCompliance`.
- **Test presence & meaningfulness:** ✓ unhappy paths covered; new tests assert behavior and
  are mutation-non-vacuous (proven RED under the targeted mutant).

---

## Findings

### F-1 (MINOR) — `tests/execution/test_multi_wallet.py` fails the project's own ruff gate
`ruff check tests/execution/test_multi_wallet.py` reports 4 issues under the repo's enforced
rule set (`select = ["E","W","F","I","UP","B","C4","SIM"]`, tests only waive `S101`):
- `:21` I001 import block un-sorted (the fix added `:52` to the import region)
- `:23` F401 `os` imported but unused (pre-existing)
- `:48` F401 `aats.execution.PartialFillSummary` imported but unused (pre-existing)
- `:481` F841 local `ledger` assigned but never used (pre-existing)

Why it matters: the engineer's "clean run" claim covered pytest only; a CI `ruff check` would
fail on this file, blocking the merge pipeline. No runtime/behavior impact.
What good looks like: `ruff check --fix tests/execution/test_multi_wallet.py` (removes the 3
unused symbols, sorts imports); drop the now-unused `ledger` assignment at `:481` or assert on it.
**Non-blocking** on a test-only de-risk fix.

### F-2 (NIT, pre-existing, carry-forward — NOT in this fix's scope)
`aats/execution/multi_wallet.py:468-473` — the `except AntiClusterCapExceeded:` branch in
`execute_bundle` is unreachable: `check_and_reserve` raises at `:367`, OUTSIDE the `try`. The
branch's own comment admits this. Harmless dead code; remove the branch or move the reserve
inside the `try` for clarity. Pre-existing, not introduced by this fix.

---

## Environment note (for QA / DevOps, non-blocking)
On the very first suite run I observed the 2 new tests fail with `got 3` — caused by a stale
`N_WALLETS_MAX_ENABLED=true` in the inherited process env. This was NOT reproducible after the
var cleared: 5x full-file runs, isolated runs, and a run with a deliberately polluted parent
env (`N_WALLETS_MAX_ENABLED=true python -m pytest ...`) all pass 45/45, because every relevant
test correctly uses `monkeypatch.delenv/setenv`. The tests are robust against env pollution.
Recommend CI pin a clean env (`-p no:cacheprovider`, no inherited `N_WALLETS_*`) for determinism.

---

## Commands run (reviewer)
- `python -m pytest tests/execution/test_multi_wallet.py -q` → **45 passed** (5x stable)
- 2 new tests in isolation → **2 passed**
- gate-defeat mutant on `multi_wallet.py:592` → **2 failed, 43 passed**; restored → **45 passed**
- `N_WALLETS_MAX_ENABLED=true python -m pytest <file>` → **45 passed** (pollution-robust)
- `python -m pytest tests/execution/ -q` → **171 passed, 2 skipped**
- `ruff check tests/execution/test_multi_wallet.py` → **4 issues** (F-1)
- secret-pattern scan of changed file → **no hits**
