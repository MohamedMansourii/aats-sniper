"""AATS — Solana Meme-Coin Ultra-Sniper bot core package.

This package is the Python side of the system (SLOW loop, models, MCS, LLM veto,
control plane, dead-man's switch watchdog, Telegram channel).  The SNIPE + FAST
hot path is implemented in Rust (see rust/).  The Python side communicates with
the Rust hot core exclusively through Redis Streams and KV — never via FFI or
shared memory.

Sub-packages (each filled by a later build lane):
  ingestion   — M1: Geyser/Yellowstone transport, decoders, point-in-time store
  features    — M1: feature pipeline, FeatureFrame assembly
  models      — M2: ONNX snipe classifier (shim), TFT survivor, naive baseline
  reasoning   — M2: schema-enforced de-risk-only LLM Reasoner
  risk        — M4: risk engine, cost gate, circuit breaker, survivable stop
  execution   — M4: ExecutionVenue impls (SimulationVenue promoted, JitoJupiter)
  mev         — M4: tip controller, MEV modes, latency instrumentation
  controller  — M3: triple-loop (SLOW only from Python), FSM glue
  control_plane — M3: FastAPI control-plane server (frozen api-contracts.md)
  telemetry   — M5: Prometheus metrics, structlog decision log

Hard rules (enforced by CI; never relax):
  - Real capital disabled by default: DRY_RUN_ENABLED=true is the boot default.
  - Money is integer lamports or Decimal — NEVER float.
  - LLM never on the FAST/SNIPE path.
  - No win-rate field, metric, or log line anywhere.
  - Point-in-time correctness: event-time stamp, never compute-time.
"""

__version__ = "0.1.0-scaffold"
