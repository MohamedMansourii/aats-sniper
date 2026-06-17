# ADR-0008 — Three-layer survivable stop + dead-man's switch failover

**Status:** Accepted (G1) · **Date:** 2026-06-16 · **Author:** `solana-systems-architect`

## Context
The stop must NOT depend on the bot being alive (locked decision 5; FR-033). A single point of failure
on the stop is an automatic G1 reject. The three failure modes to survive: (a) the process is alive
but a tick is slow, (b) the process dies, (c) the host/network partitions. The seam is co-owned:
DEFINES (`risk-guardrails`) → IMPLEMENTS venue-native (`solana-execution`) → OPERATES enforcer + DMS
(`agent-orchestration`) (ROSTER §4).

## Options
1. **In-process stop only** — the FAST loop enforces the stop. Fails mode (b) and (c): if the process
   dies, the position is unmanaged. Rejected — single point of failure.
2. **Venue-native resting order only** — an on-chain keeper. Survives (b)/(c) for the price breach but
   does nothing for a partition that also affects the keeper, and venue-native resting is not always
   available. Insufficient alone.
3. **Three independent layers + DMS** — Layer 1 venue-native resting/keeper (survives a busy or dead
   process for the price breach), Layer 2 in-process FAST enforcer (≤50ms p99, steady-state primary),
   Layer 3 external dead-man's switch (separate failure domain; on heartbeat loss > T_DMS submits
   pre-signed flattens). Each tested independently (AC-025/026/027/045/046).

## Decision
**Option 3.** Layer 2 is steady-state primary; Layer 1 covers a busy/dead process for the on-chain
price breach independent of our liveness; Layer 3 fires after T_DMS=60s (env, OQ-006) on a full
partition/crash. The DMS is a separate process / failure domain holding pre-signed flatten tx and
**cannot be disarmed** by an LLM, market event, or risk update — only a valid heartbeat or explicit
operator config (AC-046). The breaker hands open positions to all three on trip.

## Consequences
- (+) No single point of failure on the stop across all three failure modes; each layer independently
  QA-fireable (T-402).
- (+) Safety-first build order honored: breaker + survivable stop + DMS are built and proven before
  any live-capable path (TASKBOARD T-320/321/322 precede T-327's live path).
- (−) Three mechanisms to build and keep in sync (pre-signed flatten refresh as positions change);
  the cost of a stop that survives the bot's death. Accepted.
