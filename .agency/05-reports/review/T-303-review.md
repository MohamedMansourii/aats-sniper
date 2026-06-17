# Code Review — T-303 Recorded-data completeness audit hooks (C-6, AC-006)

**Reviewer:** code-reviewer (Quality Gate, G3)
**Date:** 2026-06-16
**Verdict:** **PASS** (no BLOCKER findings)
**Files reviewed:**
- `aats/ingestion/completeness.py` (new, 637 lines)
- `tests/ingestion/test_t303_completeness.py` (new, 65 tests)
- `aats/ingestion/__init__.py` (export wiring)

One-line assessment: Correct, survivorship-free, contract-conformant read-side audit; ship it — fix the silent Wilson-z fallback (MAJOR) before any operator sets a non-{0.90/0.95/0.99} confidence level.

---

## Verification (run by reviewer, not assumed)

| Check | Command | Result |
|---|---|---|
| Module tests | `python -m pytest tests/ingestion/test_t303_completeness.py -q` | **65 passed** in 3.96s |
| Full ingestion suite (regression) | `python -m pytest tests/ingestion -q` | **333 passed** in 4.91s — no regressions |
| Lint | `python -m ruff check aats/ingestion/completeness.py tests/...` | All checks passed (exit 0) |
| Types | `python -m mypy aats/ingestion/completeness.py --ignore-missing-imports` | Success: no issues |
| Secrets | grep `sk-/api_key/secret/private_key/Bearer/BEGIN PRIVATE` | 0 hits (only doc strings) |
| Float-money | grep `float(` in module | 0 hits — all counts int; rates float (statistical, allowed) |
| Network / LLM / await on path | grep `await/asyncio/llm/requests/httpx/socket` | 0 hits — pure offline read-side |
| Wilson math (independent) | hand-verified 0/100, 0/1000, 50/1000, 1/1, 0/0 | matches reference values |

Engineer's self-check claims (65 new tests, 333 total, ruff clean, mypy clean, 0 secrets) all **reproduced**.

---

## Task requirements (the four named in the handoff) — all met

1. **Census injectable** ✓ — `CensusSource` Protocol + `InMemoryCensusSource` offline fixture; production wires Geyser behind the same Protocol. No live network reachable (grep-confirmed). Protocol compliance asserted in test (`isinstance(source, CensusSource)`).
2. **Miss rate bounded + reported** ✓ — Wilson score CI upper bound (`_wilson_upper_bound`) is the gated quantity, not the point estimate (conservative/fail-closed); both surfaced in `AuditReport` + `summary()`. `n_total==0 → 1.0` (fail-closed) is correct.
3. **Censored outcomes carried, not dropped** ✓ — every census mint emits exactly one `CompletenessRow`; un-snapshotted → CENSORED with reason `mint_not_observed_by_primary_transport`; stream-dropout → CENSORED with reason `snapshot_censored_stream_dropout_or_overflow`. `coverage_fraction == 1.0` always.
4. **Survivorship-free** ✓ — structurally guaranteed; mutation-proxy test (#26) proves dropping CENSORED rows breaks `coverage_fraction < 1.0`.

---

## Conformance

| Dimension | Status | Notes |
|---|---|---|
| Blueprint / data-models.md §7 | ✓ | `CompletenessStatus` mirrors `Position.completeness_status: Literal["complete","CENSORED"]` exactly (casing included). |
| data-models.md §0 money rule | ✓ | All slot/time/count fields `int`; only statistical rates are `float`. No `Decimal` needed (nothing priced). |
| AC-006 (acceptance-criteria.md L119–124) | ✓ | The literal coverage assertion `(complete+CENSORED)/census_total ≥ 1 - declared_max_miss_rate` is verifiable from `coverage_fraction`; the stronger Wilson-bound gate is layered on top and reported. |
| AATS review brief (ROSTER §5) | ✓ | No LLM risk-up (N/A), no LLM/await on fast path (N/A — read-side), no float money, no lookahead (joins on `mint`; `event_time` is the only anchor; `recorded_at_ms` monitoring-only), no secrets, no prompt-injection surface. |
| Tests present + meaningful | ✓ | 65 tests assert behavior (counts, rates, reasons, coverage invariant, idempotence, monotonicity), include a real `ShadowRecorder` integration path and a mutation-proxy. Not a suite that can't fail. |

---

## Findings

### M-1 (MAJOR) — Silent z-score fallback under-bounds the miss rate for unlisted confidence levels
`aats/ingestion/completeness.py:345-350` (`_wilson_upper_bound`)

**What's wrong:** `z = _Z_LOOKUP.get(confidence, 1.960)` silently coerces any `confidence` not in `{0.90, 0.95, 0.99}` to the 95% z (1.96). The `AuditReport` then records `confidence_level` as the *requested* value (e.g. 0.999) while the bound was computed at 95%. Verified end-to-end: an auditor built with `confidence_level=0.975` or `0.999` reports `miss_rate_upper_bound = 0.03699` for 0/100 (the 95% value) and labels it as the higher confidence. For a safety/completeness gate, this produces a falsely-tight CI and can let the gate PASS when a true 99.9% bound would FAIL — with zero error or warning.

**Why it matters:** This is the gate the operator relies on to bound how much of the training corpus we silently missed (survivorship). A statistical bound that quietly ignores the requested confidence is a correctness defect in the one number the gate is built around. The default (0.95) and documented (0.90/0.99) paths are correct, so this is not a BLOCKER — but it is a trap for any operator who tunes confidence.

**What good looks like:** Either (a) compute z from the normal inverse-CDF (`statistics.NormalDist().inv_cdf(1 - (1-confidence)/2)` — stdlib, no scipy) so any confidence is honored; or (b) raise `ValueError` on a confidence not in the lookup, so misconfiguration fails loud instead of silently degrading. Option (a) preferred — it removes the lookup table entirely and the `summary()`/report stay truthful.

### N-1 (NIT) — Broad `except Exception` swallows malformed `payload_json`
`aats/ingestion/completeness.py:395-396` (`_normalize_snapshot_row`)

A malformed `payload_json` is silently dropped to the fallback path, where the row then reconciles as CENSORED (`mint_not_observed_...`). The survivorship behavior is safe (it errs toward CENSORED, verified), but a corrupt snapshot masquerading as "never observed" hides a data-quality problem. Consider `except (json.JSONDecodeError, TypeError)` and a `logger.warning` so corruption is visible. Optional — does not block.

### N-2 (NIT) — `import time as _time` inside `reconcile()`
`aats/ingestion/completeness.py:472`

Function-local import of a stdlib module; module already imports at top elsewhere in the package. Hoist to module scope for consistency. Cosmetic.

---

## Open issues from engineer (acknowledged, not blocking)
- Wilson upper bound is positive for 0 observed misses — correct math, documented in code + tests; operators must size `declared_max_miss_rate` to expected N. Acceptable.
- T-301 (pending-table) must absorb before annotated rows flow into the full point-in-time store. Cross-task sequencing, owned by orchestrator — not a defect in T-303.

---

## Re-review checklist for the engineer
1. Address M-1 (honor requested confidence, or fail loud on unlisted values) and keep `report.confidence_level` truthful.
2. (Optional) N-1, N-2.
3. Re-run the suite; no new findings will be raised on unrelated lines.
