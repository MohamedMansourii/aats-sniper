# ADR-0012 — BreakerState carries its event-time UTC day key (point-in-time restart seeding)

**Status:** Accepted (post-G1 delta; G3 fix for T-320 finding B1) · **Date:** 2026-06-16
**Author:** `risk-guardrails-engineer` (schema delta authored under ADR + delta-notice rule, data-models.md §10)
**Supersedes/relates:** data-models.md §8 (`BreakerState`), C-5 point-in-time, AC-029 restart safety.

## Context
The daily-loss circuit breaker is the single most important safety primitive in the system, built and
proven FIRST so it can be trusted before any live-capable path exists (TASKBOARD §3 SAFETY-FIRST). Its
headline C-5 claim is: *"the same code runs live and in backtest; the tranche resets at UTC midnight in
event-time."*

`BreakerState` (frozen schema, data-models.md §8) persisted only a scalar `daily_net_pnl_lamports` with
**no record of which event-time UTC day that net belonged to**. On process restart the breaker seeded
that persisted net into whichever UTC day the *first post-restart event* happened to fall in. A 24/7
sniper restarts/redeploys/reconnects constantly, and many of those straddle UTC midnight — so the seed
routinely landed in the WRONG day.

Reproduced by execution (finding B1):
- **(a) spurious trip (merely costly):** persisted ARMED net `-14_000_000` from day 1 + a day-2 loss of
  `-10_000_000` (alone well inside the fresh `-15M` day-2 limit) pooled to `-24M` and TRIPPED on day 2,
  when the point-in-time-correct day-2 net is `-10M` (ARMED).
- **(b) MASKED trip (latent loss of the hard halt — the dangerous direction):** persisted ARMED net
  `+50_000_000` (a profitable day 1) + a real day-2 loss of `-20_000_000` (which alone crosses the fresh
  `-15M` limit and MUST halt) pooled to `+30M`, leaving the breaker ARMED — the daily-loss HALT silently
  did not fire, letting the bot keep entering after a day's losses should have stopped it.

The C-5 claim was therefore FALSE across a restart boundary.

## Options
1. **Reset the day net to 0 on every restart.** Simple, but discards a same-day loss already accumulated
   before the crash — a restart mid-day would forget the day's drawdown and under-protect. Rejected.
2. **Stamp wall-clock at restart and assume "today".** Compute-time leak; violates C-5 (event-time only)
   and still mis-seeds on a replay/backtest. Rejected.
3. **Persist the event-time UTC day the net belongs to, and seed by THAT day.** The persisted net is
   re-applied only to its own day; a first event on a later UTC day starts fresh at 0; a same-day event
   continues correctly. Same code live and in backtest. Chosen.

## Decision
**Option 3.** Add `daily_net_pnl_day_utc: str | None` ('YYYY-MM-DD', derived from event-time
`block_time_ms`, UTC) to `BreakerState`, with the invariant **`daily_net_pnl_lamports != 0 ⇒
daily_net_pnl_day_utc is not None`** (enforced by a pydantic validator). On restart the breaker seeds
`self._day_pnl[persisted_day] = persisted_net` keyed by the stored day — never by the first arriving
event's day. A legacy row with a non-zero net and no day key is rejected fail-closed at load.

The LLM-trip and operator-reset paths are unchanged in capability: the day key is descriptive
point-in-time metadata, not a control. It cannot be used to widen, size up, or reset — asymmetric trust
(ADR-0006) is untouched.

## Consequences
- (+) The masked-trip direction (b) is closed: a real next-day loss that crosses the fresh limit now
  halts, because day-1 profit no longer bleeds into day 2.
- (+) The spurious-trip direction (a) is closed: day-1 loss no longer deepens a fresh day-2 net.
- (+) Live and backtest run the identical seeding code; C-5 holds across restarts.
- (+) Fail-closed migration: a malformed/legacy persisted net without a day key is rejected at load; the
  safe default is a fresh ARMED day (a TRIPPED latch is independent and still re-asserted).
- (−) A post-G1 change to a frozen contract (delta notice in data-models.md §8). Accepted: the schema
  was incorrect for its own stated point-in-time guarantee; the fix is a strict superset (one nullable
  field) with a clear migration.
- Affected tasks: T-320 (this breaker), T-340/341 (control plane + `/api/breaker` projection if it
  echoes the field), T-352 (dashboard read-only).
