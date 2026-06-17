# Code Review — T-501 (Documentation Accuracy) — RE-REVIEW

**Verdict: PASS**
**Reviewer:** code-reviewer (Quality Gate, G3)
**Date:** 2026-06-16
**Round:** 2 (re-review after fix of BLOCKER R-501-01)
**Scope:** `README.md`, `docs/deploy-ops-guide.md` (+ guide set referenced by them)

One-line assessment: **The verified-output BLOCKER is fully fixed — every printed
command now matches a re-run, no secrets, references resolve, framing stays honest; ship it.**

---

## Prior finding status

### R-501-01 (BLOCKER, round 1) — FIXED ✓
**Was:** The command `pytest tests/risk` was documented as yielding `337 passed`
(README:209 and deploy-ops:311), and README:211 claimed the validation suite was
"in the 337 above" under that command. `337` is actually the combined
`pytest tests/validation tests/risk`; the single `tests/risk` command yields 315.
A flagship "outputs are real" doc that prints a count the command does not produce
is a false verified-output.

**Now (verified by re-execution):**
- README:209 — `pytest tests/risk` → **315 passed** — re-run: `315 passed in 37.91s` ✓
- README:211 — `pytest tests/validation` → **22 passed** — re-run: `22 passed in 26.31s` ✓
- The inverted "in the 337 above" footnote is **removed**; the validation row now
  stands on its own command + own result. Grep for `in the .* above` / `337 above`
  in README → no matches.
- deploy-ops:311–312 — discrete lines `pytest tests/risk -> 315 passed` and
  `pytest tests/validation -> 22 passed`. ✓
- Repo-wide grep for `337` across `*.md` → only hit is unrelated random seed `1337`
  (`T-199fix-review.md`). No remaining `337` test-count claim. ✓

Fix applied is option (a) from the round-1 report (keep the documented command,
correct its result; give validation its own command + result). Correct choice.

---

## Re-run evidence (this build, this review)

| Documented command | Doc claim | Re-run result | Match |
|---|---|---|---|
| `pytest tests/risk` | 315 passed | 315 passed in 37.91s | ✓ |
| `pytest tests/validation` | 22 passed | 22 passed in 26.31s | ✓ |
| `pytest tests/execution` | 171 passed, 2 skipped | 171 passed, 2 skipped in 2.25s | ✓ |
| `pytest tests/e2e/test_t402_operator_demo.py` | 16 passed | 16 passed in 6.75s | ✓ |
| `pytest tests/validation tests/risk` (source of old error) | (not attributed to a single command anywhere) | 337 passed in 59.49s | ✓ |
| `JitoJupiterVenue(...).submit_mode` | DRY_RUN | DRY_RUN | ✓ |
| `from aats.control_plane.server import build_app` | OK | control-plane app OK | ✓ |

Every printed command now resolves to its claimed output. The deploy-ops headline
guarantee "Every command below was run against this build; outputs are real" holds.

---

## Regression / collateral checks (no new issues introduced)

- **References resolve:** all 15 files referenced by the two docs exist
  (EDGE-VERDICT, T-401-edge-proof, G4-PASS, G3-stabilization, the 5 docs/ guides,
  program-allowlist.json, grafana/prometheus configs, .env.example, docker-compose.yml,
  the e2e test). No broken link introduced by the edit. ✓
- **Security-audit reference (deploy-ops §5)** `.agency/05-reports/security/G4-security-audit.md`
  exists. ✓
- **No secrets:** secret-shaped scan (base58 keys, `hvs.` Vault tokens, `sk-` /
  `AKIA` keys, PEM blocks) over `docs/` and `README.md` → no matches. `.env.example`
  remains the only committed secret artifact, placeholders only. ✓
- **Honest framing preserved:** PAPER/DRY-RUN posture, edge `UNPROVEN-NO-REAL-DATA`,
  no win-rate field/panel, real capital disabled behind `DRY_RUN_ENABLED=true` +
  pre-live checklist — all intact in the edited sections (README §1/§4/§6/§8,
  deploy-ops §3/§5/§6). ✓
- **Edit blast radius:** only the two delivery docs changed; no edit to `aats/` or
  `aats/contracts`. Confirmed against engineer report and direct file read. ✓

---

## Conformance

- Blueprint / topology accuracy: ✓ (compose services + ports match docker-compose.yml)
- API contract (control plane) accuracy: ✓ (de-risk-only, 403 codes as documented)
- Design system (UI): N/A for this task (docs only)
- Verified-output integrity (the T-501 acceptance criterion): ✓ — all commands re-run

---

## Open / out of scope (not blocking)

- Engineer-noted: historical `.agency/` reports (TASKBOARD, STATUS, older gate/review
  files) cite point-in-time `tests/risk` counts (91/285/334) from earlier build stages.
  These are dated point-in-time artifacts, **not** delivery-doc verified-output claims,
  and are correctly out of scope for T-501. No action required for this gate.
- NIT (carry, not new): the `SIGNER_SOCKET_PATH` reconciliation note (deploy-ops §3)
  is already flagged in-doc as a carried devops note; tracked elsewhere, not a T-501
  finding.

---

## Findings (round 2)

None. No BLOCKER, MAJOR, MINOR, or NIT raised on the change. The single prior BLOCKER
is resolved with no regression. (Per re-review discipline: no new unrelated nitpicks
introduced.)
