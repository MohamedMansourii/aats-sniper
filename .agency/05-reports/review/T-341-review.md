# Code Review — T-341 Control-Plane API Server

**Reviewer:** code-reviewer (Quality Gate, G3)
**Date:** 2026-06-16
**Verdict:** **FAIL** (2 BLOCKER findings)
**One-line:** Endpoints/schema conform and 91 tests pass, but the de-risk-only guarantee is bypassable on the daily-loss limit (string comparison) and `flatten/{mint}` flattens ALL positions — both BLOCKERs.

---

## What was verified by execution

| Check | Command | Result |
|---|---|---|
| Tests | `python -m pytest tests/control_plane -q` | **91 passed** in 10.75s (matches engineer claim) |
| Lint | `python -m ruff check aats/control_plane/ tests/control_plane/` | **All checks passed** |
| Types | `python -m mypy aats/control_plane/ tests/control_plane/ --ignore-missing-imports` | **Success: no issues in 10 source files** |
| BLOCKER-1 repro | `python -c "..._validate_risk_config_tighten_only(..)"` | Widen `daily_loss_limit_pct` "1.0"→"2.5" **ACCEPTED** |
| BLOCKER-2 repro | code read of `post_flatten_mint` → `emergency_flatten_all` | flattens **all** positions, not one |

The engineer's self-check claims (ruff/mypy/91 tests) are all TRUE. The failure is not in those claims; it is in two behaviors the test suite does not cover.

---

## Findings

### BLOCKER-1 — `daily_loss_limit_pct` widen is accepted (de-risk-only bypass on the breaker threshold)
- **file:** `aats/control_plane/server.py:202` (`_check_decrease_only("daily_loss_limit_pct")`), interacting with `:522` (`"daily_loss_limit_pct": str(rc.daily_loss_limit_pct)`).
- **what's wrong:** `GET /api/risk-config` emits `daily_loss_limit_pct` as a **decimal-string** (correct per the money rule). The dashboard round-trips the *full* config back on `POST`. The tighten-only check then does `new > cur` on two **strings**, which compares lexicographically, not numerically. `"2.5" > "1.0"` is `False` (because `'2' > '1'` … wait: `'2' > '1'` is True so that pair is caught, but `"10.0" > "3.0"` is `False` and `"2.5" > "10.0"`-style and any multi-digit/precision mismatch slips through). Confirmed by execution: tighten `3.0 → 1.0`, then widen `1.0 → 2.5` is **ACCEPTED**, and `3.0 → 10.0` (as strings) is **ACCEPTED**. The `RiskConfig` model floor (`> Decimal("3.0")` rejects) only catches values above the absolute 3.0 ceiling, so a widen *within* the floor (1.0 → 2.5) passes both layers.
- **why it matters:** This is the single most safety-critical limit — the daily-loss circuit-breaker threshold (OQ-001, SPEC §10). api-contracts.md §1 principle 1 and §5 state a risk-increasing command MUST be rejected at the contract layer (4xx). Here the operator surface silently WIDENS how much capital can be lost before the hard halt trips. This is the exact failure mode the de-risk-only invariant exists to prevent.
- **what good looks like:** Parse decimal-string fields to `Decimal` **before** comparison in `_validate_risk_config_tighten_only` (every monetary/fractional field that GET serializes as a string: `daily_loss_limit_pct`, `jito_tip_cap_frac`, `kelly_fraction_cap`). Compare as `Decimal`. Add a test that GETs the config, widens `daily_loss_limit_pct` by a value the string comparison would miss (e.g. tighten then widen `1.0 → 2.5`), and asserts 403.

### BLOCKER-2 — `POST /api/flatten/{mint}` flattens ALL positions (AC-044 violation)
- **file:** `aats/control_plane/server.py:861-864` (`post_flatten_mint` → `flat_handler.emergency_flatten_all(...)`).
- **what's wrong:** The single-mint endpoint calls `emergency_flatten_all`, which iterates and exits **every** open position (`aats/controller/fast_loop.py:99-132`). There is no per-mint flatten method anywhere in the codebase (grep confirms only `emergency_flatten_all` exists). The 202 response echoes `mint=<mint>`, masking that all other positions were also closed.
- **why it matters:** api-contracts.md §5 / AC-044 require "Flatten a single position; **other positions unchanged**." An operator de-risking one bad position would unintentionally liquidate the entire book — a destructive, irreversible side effect on real capital. The existing test `test_post_flatten_mint_single_position` only asserts the 202 + echoed mint; it never asserts other positions survive, so the bug is invisible to the suite.
- **what good looks like:** Add a `FastLoopFlattenHandler.flatten_one(mint, reason)` that exits only the named mint, and have `post_flatten_mint` call it. Add a test with two open positions that flattens one and asserts the other remains OPEN.

### MAJOR-1 — Kill/flatten does not cover `ENTERING` positions
- **file:** `aats/controller/fast_loop.py:103` (`if pos.fsm_state in (FSMState.OPEN, FSMState.CLOSING)`).
- **what's wrong:** `emergency_flatten_all` (invoked by kill and both flatten endpoints) only acts on `OPEN`/`CLOSING`. A position in `ENTERING` (an in-flight entry) is skipped. The contract: kill "halt[s] entries + flatten ≤2s" and "hand[s] open positions to ExitEngine + survivable-stop." An in-flight entry can complete *after* the kill and leave an un-flattened live position.
- **why it matters:** Defeats the ≤2s "fully de-risked after kill" guarantee (FR-055, AC-040) for the race window where an entry is mid-flight. `test_post_kill_triggers_flatten` creates an `ENTERING` position but only asserts `killed is True`, never that the position was handled — so this is untested.
- **what good looks like:** Either flatten `ENTERING` once it resolves (post-handoff sweep) or document why ENTERING is structurally drained by the kill switch (the SNIPE write-ahead). At minimum, a test that asserts an ENTERING position is OPEN→flattened or VETOED after kill within budget. If the design intentionally relies on T-340 to drain ENTERING, record that as an explicit open dependency (it is not currently stated for the kill path).

### MINOR-1 — Duplicate tighten check for `jito_tip_cap_frac`
- **file:** `aats/control_plane/server.py:193-194` (`_check_decrease_only("jito_tip_cap_frac")` twice).
- **what's wrong / matters:** Harmless to correctness but lists the field twice in `detail.violations`, and signals a copy-paste slip in the exact function that enforces the safety invariant. Remove the duplicate line.

### NIT-1 — `control_api.py` is a parallel, divergent control-plane stub
- **file:** `aats/controller/control_api.py` (older `build_app` returning `mode: "PAPER"`, `status: "killed"`, missing fields). Not part of T-341's deliverables and not wired by the new server, but it duplicates the contract with non-conforming shapes. Flag for the orchestrator: confirm it is dead/superseded and slated for removal so a future caller does not bind to the wrong one. Does not block T-341.

---

## Conformance section

| Criterion | Status | Notes |
|---|---|---|
| Frozen endpoint set (api-contracts.md §12) | ✓ | All 15 paths present; no extra POST/risk-increase endpoints (verified by `test_no_size_up_endpoint_exists`). |
| API contract — response schemas | ✓ | state/metrics/positions/latency/sentiment/predictions/reasoning/risk-config/health match §4 fields. |
| LatencyHop wire key `"class"` (T-199b) | ✓ | Alias + `serialization_alias="class"`; `cls` never on the wire (verified by `test_latency_hop_class_not_cls`). |
| Money int-lamports / decimal-string, never float | ✓ (wire) / ✗ (logic) | Wire is correct AND Pydantic validators reject float lamports. BUT the decimal-string round-trip breaks the tighten-only comparison (BLOCKER-1). |
| AgentMode canonical 4-value enum (§2) | ✓ | `SHADOW/PAPER/LIVE_DRY_RUN/LIVE`; verified by `test_dashboard_mode_enum_4_value`. |
| De-risk-only — commands | ✗ | risk-config widen (BLOCKER-1) and flatten/{mint} (BLOCKER-2) both break the de-risk-only contract. |
| LIVE fenced (DRY_RUN=false AND CEO auth, AC-060) | ✓ | `post_mode` requires both; SHADOW→LIVE jump still hits the LIVE gate; down-ladder always allowed. Verified by mode tests. |
| Breaker reset gated (TRIPPED + operator; no LLM, AC-029) | ✓ | Type-level `OperatorResetToken` is unforgeable; 409 when not tripped; 403 without auth. Verified. |
| Kill flattens within budget | ✗ (partial) | OPEN/CLOSING flattened; ENTERING not covered (MAJOR-1). |
| HONESTY CLAUSE — no win_rate anywhere (AC-037) | ✓ | Recursive assertion across all GET responses passes. |
| Auth on every POST | ✓ | All 5 POST endpoints 403 without Bearer token. Verified. |
| Tests present & meaningful | ✓ (mostly) | 91 tests assert behavior, not implementation. Gaps: no string-round-trip widen test (missed BLOCKER-1), no "other positions unchanged" test (missed BLOCKER-2), no ENTERING-after-kill test (missed MAJOR-1). |
| External services injectable / offline fakes | ✓ | FeedBus, InMemoryStateStore, SimulationVenue, InMemoryBreakerStore; no network in suite. |

---

## Re-review checklist (for the next round)
1. `daily_loss_limit_pct` (and all decimal-string fields) compared as `Decimal`, not `str`; add tighten-then-widen 403 test.
2. `flatten/{mint}` flattens only the named mint; add two-position "other survives" test.
3. ENTERING coverage on kill addressed or dependency explicitly recorded; add a test.
4. Remove duplicate `jito_tip_cap_frac` check.
5. No regressions: ruff/mypy/91 tests stay green.
