# Code Review — T-199fix (T-199a + T-199b verification on landed diff)

**Reviewer:** code-reviewer (Quality Gate, G3)
**Date:** 2026-06-16
**Scope:** `aats/contracts/features.py` (T-199a leak-guard determinism),
`aats/contracts/api_schemas.py` (T-199b LatencyHop wire key `"class"`), and the
contracts test suite under `tests/contracts/`.
**Change type:** verification of already-landed code (engineer made zero code changes;
instruction was DO NOT edit `aats/contracts`). All findings below are verified by my own
command execution, not by trusting the engineer's report.

---

## VERDICT: PASS

One-line assessment for the Orchestrator: **T-199a and T-199b are correct, contract-
conformant, and proven by meaningful tests under my own execution — PASS. Four pre-
existing UP007 lint findings in `features.py` (untouched `Optional[...]` fields, outside
this change's scope) are a separate MINOR cleanup item for the owning lane, not a blocker.**

---

## What I executed (not assumed)

| Check | Command | Result |
|---|---|---|
| LatencyHop wire key | `LatencyHop(...).model_dump_json(by_alias=True)` | `{"name":...,"class":"internal_compute","p99_ms":null,"note":null}` — key `class` present, `cls` absent |
| LatencyHop round-trip | `model_validate({... "class":"submission"})` | `hop.cls == "submission"` ✓ |
| No `__future__` in features.py | AST walk for `ImportFrom(module="__future__")` | 0 nodes ✓ |
| Full contracts suite | `pytest tests/contracts/ -q` | **180 passed** |
| Determinism sweep | seeds 0,1,42,1337,99999,314159 | 180 passed on every seed |
| Mutation proof (mine) | flip `extra="forbid"→"ignore"`, run leak-guard tests | **6 tests went RED** (3 negative-construction + 3 mutation-proof); restored byte-identical (SHA `29bc85f4…` matches) |
| Suite after restore | `pytest tests/contracts/ -q` | 180 passed |
| CoreSchema state | `FeatureFrame.__pydantic_complete__`, `model_config["extra"]` | `True`, `forbid` |
| mypy (touched files) | `mypy features.py api_schemas.py` | Success: no issues |
| ruff (api_schemas.py) | `ruff check api_schemas.py` | All checks passed |
| ruff (features.py) | `ruff check features.py` | 4× UP007 (see F-1) |
| Secrets scan | contracts + tests | zero production secrets |

The mutation test is the load-bearing one: the leak-guard negative tests assert real
runtime behavior (`FeatureFrame(**data, label="RUGGED")` must raise), and they DO fail
when the guard is removed. This is not a test suite that can't fail. The reproduction
matched the engineer's claim exactly, including the byte-identical SHA-256 after revert.

---

## Findings

### F-1 — MINOR — `aats/contracts/features.py:138,139,140,144`
`Optional[float]` / `Optional[int]` annotations trip ruff `UP007` ("Use `X | Y`").
CI runs `ruff check aats/` (`.github/workflows/ci.yml:124`) and `UP` is selected
(`pyproject.toml:67`), so these will register against the lint step.
**Why it matters:** repo-wide CI lint cleanliness. **Why not a blocker:** these fields
(`rsi`, `macd`, `bb_width`, `smart_wallet_entry_lag_slots`) pre-date T-199 and were NOT
modified by the leak-guard fix; this is inherited style debt, and the same UP007/UP037/
F401/I001/SIM/C408 pattern exists across `aats/telemetry/` and `sol-sniper/` (45 total in
the CI command) — i.e. a separate lint-hygiene pass owned by those lanes, not a T-199fix
regression. **What good looks like:** `rsi: float | None` etc.; fold into the next
contracts lint sweep, or `ruff check --fix`.

No other findings. No BLOCKER, no MAJOR.

---

## Conformance

| Criterion | Status | Evidence |
|---|---|---|
| Blueprint / data-models.md (FeatureFrame frozen, money int/Decimal, no truth_*/label) | ✓ | `extra="forbid"` enforced in live CoreSchema; negative tests raise; 180 green |
| API contract (api-contracts.md §4 `/api/latency`) | ✓ | spec lines 144–160 require key `"class"` with values `internal_compute`/`submission`; LatencyHop emits exactly that, full shape (`name`/`ms`/`budget_ms`/`class`/`p99_ms?`/`note?`) matches |
| "No imported field-shape changed" | ✓ | LatencyHop field set/types unchanged vs contract; FeatureFrame field set unchanged (only `__future__` import + a validator return-annotation removed); mypy clean on both |
| Design system (UI) | n/a | contracts module, no UI |
| Tests present & meaningful | ✓ | leak-guard tests assert behavior (construction raises), proven by mutation going RED; LatencyHop has 6 wire-key tests incl. regression guard against `"cls"` |
| Dependencies | ✓ | no new deps; pydantic/pydantic-core only |
| Secrets | ✓ | none in `aats/contracts` or `tests/contracts` |

---

## Re-review notes
The two open issues in the engineer's report are process items for the Orchestrator,
not code findings: (1) record this dual-G3 PASS, and (2) move T-199a/T-199b on the
taskboard IN-REVIEW → DONE once `backtest-qa-engineer` also PASSes (G3 is dual per
AATS-ROSTER §5). My gate is satisfied.

---

```
=== HANDOFF ===
FROM: code-reviewer
TASK: T-199fix — verify T-199a (leak-guard determinism) + T-199b (LatencyHop wire key "class")
STATUS: COMPLETE
DELIVERABLES: .agency/05-reports/review/T-199fix-review.md
SELF-CHECK: Ran pytest tests/contracts/ (180 passed); ran LatencyHop.model_dump_json(by_alias=True)
  -> emits "class", not "cls"; AST-confirmed 0 __future__ imports in features.py; reproduced the
  extra="forbid"->"ignore" mutation myself -> 6 leak-guard tests went RED, restored byte-identically
  (SHA256 29bc85f4… matched); determinism sweep across 6 PYTHONHASHSEED values all 180 green;
  mypy clean on both touched files; secrets scan clean; api-contracts.md §4 confirms "class" wire key.
RISKS: F-1 — 4 pre-existing UP007 lint findings in features.py (untouched Optional[...] fields)
  will register against CI ruff; MINOR, owned by contracts lint-hygiene sweep, not a T-199fix regression.
NEEDS: backtest-qa-engineer dual-G3 PASS to close the gate; Orchestrator to update taskboard.
===============
```
