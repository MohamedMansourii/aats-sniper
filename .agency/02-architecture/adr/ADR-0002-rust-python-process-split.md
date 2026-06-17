# ADR-0002 — Rust hot-core / Python split as a PROCESS boundary (not PyO3), Redis as the membrane

**Status:** Accepted (G1) · **Date:** 2026-06-16 · **Author:** `solana-systems-architect`

## Context
The snipe + fast hot path must be Rust (BUILD-DIRECTIVE HARD RULE; BRIEF §7.2). The hard constraint is
a ≤150 ms p99 snipe budget and a ≤50 ms p99 hard-stop with **no GC pause, no GIL, no foreign-call
jitter** (latency-budget.md). Python owns the SLOW loop, training, the sim, and the control plane. The
question is HOW Rust and Python share data: in-process (PyO3/FFI) or process-split.

## Options
1. **PyO3 / embedded FFI** — Python embedded in the Rust process (or vice versa). One process, no
   serialization hop. BUT re-introduces the GIL/GC unpredictability we pay Rust to remove, and a model
   crash takes down the snipe path (shared failure domain).
2. **Process split, Redis as the boundary** — Rust hot-core and Python services are separate OS
   processes; they exchange typed messages over Redis. One local Redis hop (sub-ms, in budget);
   isolated failure domains; independently restartable.
3. **gRPC between processes** — typed, but adds a synchronous call surface and a network stack on a
   path that must read a cached scalar in microseconds.

## Decision
**Process split with Redis as the membrane.** The Rust hot-core (ingest + SNIPE + FAST) is one
process owning the isolated signer; Python (SLOW, models, LLM, control plane, DMS) are separate
processes. The ONLY thing the SNIPE path reads from the model path is a **pre-staged KV scalar**
(score + uncertainty + veto bit, event-time stamped) written by the SLOW loop — never an in-process
call into Python. The model that touches the hot path is the **ONNX artifact run by the Rust
inference shim**; the LightGBM/MLP is trained in Python and exported to ONNX.

## Consequences
- (+) The hot core has no GC/GIL jitter and an isolated failure domain (NFR-006 independent restart).
- (+) The LLM and TFT physically cannot reach the hot path — they live in another process and SNIPE
  reads only a KV scalar (enforces "LLM never on FAST/SNIPE", FR-018/021).
- (−) One Redis hop and a serialization cost on the boundary — sub-ms local, deliberately paid and in
  budget.
- (−) Two language toolchains to maintain; mitigated by deriving Pydantic + serde structs from one
  field list (`data-models.md`).
