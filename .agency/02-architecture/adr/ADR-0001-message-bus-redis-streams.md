# ADR-0001 — Message bus = Redis Streams (v1), NATS JetStream as the documented migration path

**Status:** Accepted (G1) · **Date:** 2026-06-16 · **Author:** `solana-systems-architect`

## Context
The loops must be decoupled so a flaky social/RPC/enrichment API can NEVER stall price processing
(BUILD-DIRECTIVE; FR-021/023). We need consumer-group fan-out (SNIPE and SLOW consume `launch.events`
independently), bounded buffers (a stalled producer must self-limit), and replay (R1 shadow-record
must re-drive the whole system from recorded events). Redis is already the shared-state store (A-012).

## Options
1. **Redis Streams** — consumer groups, `MAXLEN` caps, `XADD`/`XREADGROUP`, replay; one infra
   dependency; sub-ms local. Lower throughput ceiling than a dedicated broker; single-node durability.
2. **NATS JetStream** — higher throughput, multi-host fan-out, stronger persistence; an extra infra
   component and operational surface for a single-host v1.
3. **Kafka** — industrial throughput/retention; heavy operational weight, overkill for one host.

## Decision
**Redis Streams for v1.** Each producer stream has a `MAXLEN` cap; consumer groups give SNIPE/SLOW
independent fan-out; `launch.events` is replayable for R1. Document the **migration path to NATS
JetStream** for when fan-out/throughput/cross-region demands it. The typed contracts (`data-models.md`)
are transport-agnostic, so the swap is wiring, not a contract change.

## Consequences
- (+) One infra dependency for v1; replay for shadow-record; the decoupling guarantee holds — a
  stalled producer lags/drops its OWN stream (MAXLEN) and never back-pressures the snipe path, which
  reads a cached KV scalar, not a live stream.
- (+) Single source for state + bus simplifies the deploy.
- (−) Single-node Redis is a throughput/durability ceiling; mitigated by the documented JetStream
  path and by the fact that v1 runs on one co-located host.
- This is neither over-engineering (we did not adopt Kafka for one host) nor corner-cutting (we did
  not couple ingestion to compute) — the trade-off is "single-host simplicity now, broker later."
