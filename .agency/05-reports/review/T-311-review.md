# Code Review — T-311 Frozen naive-momentum baseline (C-4)

**Reviewer:** code-reviewer (Quality Gate, G3)
**Task:** T-311 — GATE-B frozen naive-momentum baseline
**Verdict:** **FAIL** (1 BLOCKER)
**One-line:** Logic is correct and well-built, but the deliverable was handed off with the committed `baseline.frozen.json` in a mutated, hash-inconsistent state — the headline C-4 test FAILS as delivered, and the engineer's "16 passed / config restored / git diff clean" self-check is false.

---

## How verified (commands I ran)

| Command | Result |
|---|---|
| `python -m pytest tests/models -q` (file as delivered, pct=70.0) | **1 failed, 15 passed** — `test_committed_config_is_frozen_and_hash_matches` FAILED |
| `pytest …::test_committed_config_is_frozen_and_hash_matches` (isolated) | **FAILED** — `e1e7dc6c… != ec0d43e6…` |
| Independent recompute of canonical hash of committed params (pct=70.0) | `ec0d43e6…` — does NOT equal stored `frozen_hash e1e7dc6c…` |
| Hash of params with pct=60.0 | `e1e7dc6c…` == stored `frozen_hash` (so the stored hash is the *60.0* hash) |
| `pytest tests/models -q` after restoring pct→60.0 (consistent state) | **16 passed** |
| `git status` | `aats/models/baseline.frozen.json` is **untracked** (`??`) — `git diff` is empty by construction, so it proves nothing about restoration |
| `ruff check` (3 files) | All checks passed |
| `mypy aats/models/baseline.py` | 1 error (MINOR, see F-3) |

---

## Findings

### BLOCKER

**F-1 — Deliverable committed in failing state; the C-4 freeze the task exists to prove is broken as handed off.**
`aats/models/baseline.frozen.json:18,36`
- As delivered, `params.selection_percentile = 70.0` while `frozen_hash = e1e7dc6c…` is the hash of the **60.0** params. The canonical hash of the on-disk params is `ec0d43e6…`. They do not match.
- This is precisely the `baseline_changed_after_fit` condition. The headline test `test_committed_config_is_frozen_and_hash_matches` FAILS in isolation and in a clean suite run (`1 failed, 15 passed`).
- Root cause: the engineer's mutation proof flipped `selection_percentile` 60.0→70.0, then the "restore" never landed in the deliverable. The self-check claim `git diff --stat clean` is vacuous — the file is **untracked**, so git shows no diff regardless of content. The reported "16 passed" is not reproducible against the delivered file.
- **Why it matters:** the entire point of T-311 is an immutable, hash-consistent GATE-B control. Shipping it inconsistent means the control is provably p-hackable in exactly the way C-4 forbids, and CI would be red on first run. A self-check that reports PASS while the headline test fails is the more serious issue — it means the verification protocol (charter §6) was not actually executed against the artifact.
- **What good looks like:** restore `selection_percentile` to `60.0` (the value the stored `frozen_hash e1e7dc6c…` corresponds to) **or** re-stamp `frozen_hash` to match the intended params *before* first fit; then re-run `pytest tests/models -q` and confirm `16 passed` against the file that is actually committed. (I restored the file to the consistent 60.0 state during review so the tree is not left red, but the engineer owns choosing and committing the intended frozen value — I do not own this decision.)

### MAJOR

**F-2 — Test-suite self-check was reported without reproducing it against the artifact; suite count is run-order/state sensitive.**
`tests/models/test_baseline_freeze.py:160` + handoff JSON `selfCheckPass:true,"16 passed"`
- The pass count depends entirely on the on-disk state of the committed config at run time. The same `pytest tests/models -q` yields `16 passed` (consistent file) or `1 failed, 15 passed` (mutated file). The handoff asserted the green number while the artifact was in the red state.
- **Why it matters:** `STATUS: COMPLETE` is only legal if SELF-CHECK reflects actual verification of the delivered artifact (charter §6). Here it did not.
- **What good looks like:** the freeze/mutate tests already correctly use `tmp_path` copies; keep that. The one real-file test (`test_committed_config_is_frozen_and_hash_matches`) is the canary — its result MUST be the number quoted in the handoff, captured after the final config write. Quote the run that matches the committed bytes.

### MINOR

**F-3 — `mypy` error: `_read_config` returns `Any`.**
`aats/models/baseline.py:223` — `Returning Any from function declared to return "dict[str, Any]" [no-any-return]`
- `json.load` is typed `Any`; the annotation promises `dict[str, Any]`.
- **What good looks like:** `data = json.load(fh); return data if isinstance(data, dict) else _raise(...)`, or `cast(dict[str, Any], json.load(fh))` with a structural check. Cheap, removes the only type error in the module.

**F-4 — `params_provenance` block can silently desync from `params` (it is not hashed).**
`aats/models/baseline.frozen.json:25-33`
- Only `_HASHED_PARAM_KEYS` are hashed. The human-readable `params_provenance` mirror is free to drift (e.g., if a value changes but its provenance note does not). Not a correctness bug, but a documentation-integrity foot-gun on a change-controlled artifact.
- **What good looks like:** a small test asserting `set(params_provenance.keys()) == set(_HASHED_PARAM_KEYS)`, or a note that provenance is descriptive-only.

### NIT

**F-5 — `baseline_p` proxy uses `float(frac)` on a `Decimal` ratio.** `aats/models/baseline.py:381`
- Correct by contract (`DecisionSignal.baseline_p` is a `float` in [0,1], not money) and documented as such. No action required; flagged only to confirm it was reviewed and is intentional, not a money-discipline slip.

---

## Conformance

| Criterion | Status | Note |
|---|---|---|
| Blueprint / validation-harness §4 C-4 (committed hashed config, `baseline_changed_after_fit` test) | **✗** | Mechanism is correctly built; **delivered artifact fails its own C-4 test** (F-1). |
| data-models §3.2 selection rule (positive first-K net buy pressure above threshold) | ✓ | `evaluate_baseline` implements `net>0 AND net>=threshold AND volume>=min_volume`, exact Decimal/int. |
| data-models §0 money discipline (int/Decimal, never float for money) | ✓ | Float-money rejected at load and in `canonical_params_hash`; momentum_score is Decimal. |
| data-models §4 `DecisionSignal.baseline_p` (probability proxy, never a price) | ✓ | `baseline_p ∈ [0,1]`; no price/size/decision field on `BaselineSignal` (test F covers it). |
| Point-in-time correctness (first-K feature only, no future data/labels/model output) | ✓ | Inputs flow only through T-305 `build_buy_pressure_features`, which enforces the cutoff/provenance guard; K-mismatch refused. |
| Clean-room (no `truth_*`, no win-rate, no execution/keypair/RPC/secrets) | ✓ | Grep scans clean (only boundary-statement doc lines). |
| Test presence & meaningfulness | ✓ (design) / **✗ (as-run)** | Tests assert behavior incl. mutation-fails-loud; but the real-file canary fails against the delivered config (F-1/F-2). |

---

## Re-review checklist (for the fix round)
1. Commit `baseline.frozen.json` in a state where `frozen_hash == canonical_params_hash(params)` (decide 60.0 vs re-stamp; record the choice).
2. `pytest tests/models -q` shows `16 passed` **against the committed bytes**; quote that run in the handoff.
3. Fix F-3 (mypy clean) — optional but should land.
4. No new findings will be raised on unrelated lines; this is round 1.
