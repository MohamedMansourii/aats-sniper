/// AATS Hot Core — Geyser ingest + SNIPE + FAST loops.
///
/// Architecture boundary (ADR-0002, BLUEPRINT §4.1):
///   This binary is the ONLY code on the SNIPE + FAST hot path.
///   Python (SLOW loop, models, LLM) communicates ONLY through Redis.
///   The wallet secret lives in aats-signer (ADR-0009); this process
///   holds the pubkey only and sends UNSIGNED tx bytes to aats-signer
///   over a local Unix-domain socket.
///
/// DRY_RUN_ENABLED (hard constraint, infrastructure.md §2):
///   When DRY_RUN_ENABLED=true (the default), the submit path is
///   compile-time-reachable but runtime-gated: the JitoJupiterVenue
///   returns Err(SubmitDisabled) instead of sending any bytes to the
///   block engine.  A test asserts zero network sends with flag=true.
///
/// TODO (filled by solana-execution-engineer + mev-latency-engineer):
///   - T-300: Geyser/Yellowstone gRPC ingest + decoder
///   - T-327: JitoJupiterVenue (DRY-RUN first, then live)
///   - T-323: 0-RPC safety gate (≤10ms, checks 1–5)
///   - T-324: cost gate (edge > cost or NO TRADE)
///   - T-330: TipController (live tip_stream → Redis KV cache)
///   - T-331: MEV modes (fast vs secure)
///   - T-340: SNIPE + FAST loop FSM, heartbeat emit

fn main() {
    // Placeholder: real entry point implemented in T-340 (agent-orchestration-engineer)
    // and T-300/T-327 (data-ingestion-engineer / solana-execution-engineer).
    eprintln!(
        "aats-hotcore v{} — scaffold placeholder. \
         Real implementation: T-300 / T-327 / T-340.",
        env!("CARGO_PKG_VERSION")
    );
    eprintln!(
        "DRY_RUN_ENABLED={} (default: true; real submit disabled)",
        std::env::var("DRY_RUN_ENABLED").unwrap_or_else(|_| "true".to_string())
    );
}
