# CODEX WORK-PACKAGE #1 — Fix CP-07 (creator-outflow feature, 2 edge-case bugs)

> **How to use:** paste this whole file into a fresh Codex / ChatGPT (high-reasoning) session with the repo open
> at `C:\dev\aats`. Codex BUILDS the fix. Then bring it back here and Claude runs the dual-G3 review gate (the
> checker stays on Claude — never let the same agent build and grade its own work). Codex must NOT run git.

## Project context (1 paragraph)
AATS is a paper-only quantitative-trading research system for Solana token launches (pure SWE/DS/ML). It runs
in simulation only — real capital is hard-disabled. The file you're fixing is a **de-risk feature**: it measures
how fast a token creator is *selling out* after migration, used only to *lower* a model's confidence (never to
increase risk). Hard rules you must not break: **de-risk-only** (output can only push toward caution),
**point-in-time correctness** (only on-chain data with slot ≤ the decision slot; never wall-clock; no lookahead),
integer/Decimal money (no float money), no secrets, and **do not edit `aats/contracts/`**.

## The file
`C:\dev\aats\aats\ingestion\creator_outflow_velocity.py` (+ its test `tests\ingestion\test_creator_outflow_velocity.py`).

## The two bugs to fix (found by an independent reviewer; the current unit tests do NOT catch them)

### BUG 1 — stale feature after a degenerate-baseline recompute (around lines 501–509)
`_recompute_mint` pops `_baseline_base` and `_cumulative_outflow_base` when the earliest post-migration reading
is `0`. After that pop, a subsequent read can serve a **stale** feature value instead of recomputing from a valid
baseline. **Fix:** when the baseline is degenerate (earliest reading 0 / missing), the feature must return the
refuse-by-default / "no-signal" value (consistent with how the module already handles undecodable input) — never
a stale cached value. Recompute correctly once a valid baseline exists.

### BUG 2 — same-slot distinct writes miscounted as duplicates (around lines 560–573)
The dedup key `(mint, creator_wallet, event_slot)` conflates **distinct** outflow writes that happen in the same
slot — so two genuinely different transfers in one slot are collapsed to one, and the result is
arrival-order-dependent. **Fix:** make the dedup key distinguish genuinely distinct events (e.g. include the
transaction signature / a stable per-event id in the key) so same-slot distinct transfers are all counted, and
the result is deterministic regardless of arrival order. Do NOT count the *same* event twice (true idempotency
must still hold).

## Acceptance criteria (the Claude review gate will check ALL of these)
1. Both bugs fixed with the smallest correct change; no unrelated refactors.
2. **New unit tests that FAIL on the current code and PASS after your fix** — one per bug:
   (a) a degenerate-baseline case that would have served a stale value now returns the no-signal value;
   (b) two distinct same-slot transfers are both counted, and the result is identical regardless of insertion order,
   while a genuinely duplicated event is still counted once.
3. **De-risk-only preserved:** the feature can still only push toward caution (monotone non-increasing on the model
   side); prove it isn't inverted.
4. **Point-in-time preserved:** only slots ≤ the decision slot; no wall-clock in any value used for a decision.
5. `python -m pytest tests/ingestion/test_creator_outflow_velocity.py -p no:randomly -q` → all pass; and the full
   suite `python -m pytest -p no:randomly -q` stays green (was 3142 passed / 0 failed).
6. `python -m ruff check aats/ingestion/creator_outflow_velocity.py tests/ingestion/test_creator_outflow_velocity.py`
   → clean.
7. **Do NOT run any git command.** Do NOT edit `aats/contracts/`. No secrets.

## What to hand back
- The changed files (Codex edits them in place).
- The exact commands you ran + their output (pytest counts, ruff result).
- A 3–5 line summary of what each fix changed and the mutation proof (test RED before / GREEN after).

Then return to Claude: it will run `code-reviewer` + `backtest-qa-engineer` on the frozen tree, and only commit on
a clean dual-G3 PASS + write the acceptance artifact.
