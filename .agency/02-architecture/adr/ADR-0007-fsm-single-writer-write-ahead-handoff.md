# ADR-0007 — Per-position FSM: single-writer + write-ahead atomic snipe→fast handoff

**Status:** Accepted (G1) · **Date:** 2026-06-16 · **Author:** `solana-systems-architect`

## Context
The snipe→fast handoff is the one place a race is catastrophic: a double-entry on the same mint
doubles exposure and breaks blast-radius caps (FR-024, AC-012 — zero double-entries tolerated, even
under 1,000 concurrent snipes on one mint). The SNIPE loop (Rust) claims entries; the FAST loop (Rust)
manages open positions; both touch the per-position FSM in Redis.

## Options
1. **Optimistic last-write-wins** — simple, but two SNIPE events on one mint both write `ENTERING`;
   double-entry. Rejected.
2. **Distributed lock per mint** — a Redis lock acquired before claiming; correct but adds lock
   lifecycle, expiry, and deadlock-on-crash concerns.
3. **Single-writer fields + atomic CAS claim (Lua) + write-ahead** — each FSM field has exactly one
   writer (SNIPE writes the `ENTERING` claim; FAST writes `OPEN/CLOSING/CLOSED/VETOED`); the claim is
   an atomic Redis Lua `CAS` (`IDLE→ENTERING` keyed by mint, single round-trip); the intent-id +
   write-ahead record is persisted BEFORE the tx is submitted.

## Decision
**Option 3.** The atomic Lua CAS guarantees exactly one `ENTERING` claim wins per mint; a second
snipe on an `ENTERING`/`OPEN` mint is rejected `fsm_state_conflict` (AC-012). Write-ahead means a
crash mid-submit leaves a recoverable `ENTERING` record (NFR-006 restart restores the FSM from Redis).
Single-writer-per-field removes the need for a general lock.

## Consequences
- (+) Zero double-entries by construction; no lock lifecycle to manage; crash-recoverable.
- (+) Restart restores all open positions from Redis (NFR-006); the DMS stays armed across the gap.
- (−) The Lua CAS is a small piece of careful code that must be reviewed as the critical section; it
  is the one place the handoff correctness lives. Called out in BLUEPRINT §2.2 for the reviewer.
