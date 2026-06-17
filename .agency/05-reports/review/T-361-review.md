# Code Review — T-361 (Telegram de-risk-only command set)

**Reviewer:** code-reviewer (Quality Gate, G3)
**Date:** 2026-06-16
**Verdict:** **PASS**
**One-line:** De-risk-only command surface is correct, contract-conformant, and structurally fenced; authz/confirm/secret invariants verified by inspection AND execution — merge approved.

---

## Scope reviewed
- `aats/telegram/command_config.py` (operator allowlist, Vault-ref secrets, fail-closed)
- `aats/telegram/control_plane_client.py` (4-method de-risk seam, offline fake, httpx adapter)
- `aats/telegram/commands.py` (authz gate, confirm flow, closed command registry, /status formatting)
- `aats/telegram/__init__.py` (exports)
- `tests/telegram/conftest.py`, `test_commands.py`, `test_command_config_secrets.py`
- `.env.example` (added OPERATOR_API_TOKEN, CONTROL_PLANE_URL)
- Cross-read: `api-contracts.md §2/§5/§7/§10`, `custody-policy.md §7`, shared `config.py`/`alerts.py`

## Commands run (by reviewer)
| Command | Result |
|---|---|
| `python -m pytest tests/telegram -q` | **86 passed** in 8.62s |
| `python -m pytest tests/telegram/test_commands.py test_command_config_secrets.py -q` | **44 passed** (39+5) — matches claim |
| `python -m ruff check aats/telegram/ tests/telegram/` | All checks passed |
| `python -m mypy commands.py command_config.py control_plane_client.py` | Success: no issues, 3 files |
| Behavioral probe (`/flatten all`, `/pause LIVE`, unauthorized `/confirm`) | de-risk-only confirmed live (see below) |

## Behavioral verification (reviewer-run, not just unit assertions)
- `/flatten all` → routes to `flatten_mint("all")` (a literal mint string to `/api/flatten/{mint}`), NOT the flatten-all endpoint. No path to flatten-all exists on Telegram, per contract §7 / custody §7.
- `/pause LIVE` → the "LIVE" arg is discarded; client posts hard-coded `{"mode":"SHADOW"}`. Mode-up is structurally unreachable.
- Unauthorized `/confirm` (non-operator) → `authorized=False`, ZERO control-plane calls; operator-ID gate runs before confirm handling. Log line carries only a `…99` fingerprint, not the full ID.

---

## Conformance
- **Frozen API contract (api-contracts.md §7):** ✓ Endpoints exact — GET `/api/state`+`/api/metrics`, POST `/api/kill`, POST `/api/flatten/{mint}` (URL-encoded via `quote(safe="")`), POST `/api/mode {SHADOW}`. No extra POST. `breaker/reset`, `risk-config`, flatten-all, and any mode-up are correctly NOT exposed.
- **Money/honesty (NFR-009, AC-037):** ✓ Money is int lamports / decimal-string via shared Decimal formatter; float lamports raise TypeError (`_status_from_wire` and `format_sol_from_lamports`). No win-rate/hit-rate field — asserted against a banned-substring list.
- **Custody policy §7 (authz):** ✓ Single operator-ID allowlist; unlisted sender → dropped + logged, no API call; bool/None/non-int rejected; empty allowlist fail-closed. Per-command confirm on /kill and /flatten: single-use nonce bound to the same operator-ID, TTL-bounded, consumed before firing (replay-safe), bounded pending table (FIFO-evict at 64).
- **Secret discipline (custody §4/§7):** ✓ Vault refs only; `redact_token` chokepoint; `safe_repr` redacts tokens and never lists operator IDs (count only); `.env.example` placeholders; source-grep test asserts no token-shaped literal. Bearer token assembled at call time, never logged. No edits to any contract artifact.
- **Test presence & meaningfulness:** ✓ 44 tests assert behavior (authz drop, no-call, routing per endpoint, confirm single-use/TTL/operator-bind, structural de-risk via seam introspection, money/honesty, graceful failure without leak). Unhappy paths covered (unreachable control plane, failed outcome, expired/invalid/hijacked nonce).

---

## Findings

### NIT-1 — `/flatten all` sends literal "all" as a mint
`commands.py:322` / `_do_flatten`. A user typing `/flatten all` posts to `/api/flatten/all`, relying on the server to 4xx an invalid mint. This can only ever de-risk a (nonexistent) single mint — never reaches flatten-all — so it is safe, but a one-line client-side mint-shape sanity note in the confirm prompt would make operator intent clearer. Optional; does not block.

### NIT-2 — `HttpControlPlaneClient` is untested by construction
`control_plane_client.py:201`. The real httpx adapter is excluded from tests (no network here), which is the correct call, but its status/error mapping (200/202 → ok; URL-encoding; redacted error path) is unexercised. Owner should ensure the runtime-assembly task (noted in the handoff open issues) adds an integration/contract test against a stub server before live. Optional for G3; flag for G4/runtime wiring.

No MAJOR or BLOCKER findings.

---

## Re-review notes
First review of T-361. All four reviewer-priority invariants (authz operator-only + logged; de-risk-only structural; frozen-contract routing; confirm-gating on kill/flatten; no secret) hold under both inspection and execution.

**Verdict: PASS.** Proceed to backtest-qa-engineer (G3 dual) and crypto-security-engineer (G4) per AATS overlay.
