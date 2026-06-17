/// AATS Signer — minimal-surface process for wallet key custody.
///
/// Security contract (ADR-0009, infrastructure.md §5):
///   - NO inbound network (only a local loopback Unix-domain socket).
///   - NO untrusted-byte decoding beyond the tx bytes it is asked to sign.
///   - Wallet secret fetched from Vault via short-lived token at boot.
///   - Secret held in mlock-able memory; zeroized on exit (Zeroize).
///   - Three independent refusals on every sign() request:
///       1. Per-tx + rolling-aggregate SOL spend cap
///       2. Full enumerated program-ID allowlist
///       3. System-program transfer destination pinned to 8 Jito tip accounts
///
/// TODO (implemented by crypto-security-engineer, T-251 / T-352a):
///   - Unix-domain socket listener with peer-credential gating
///   - Vault token boot flow + mlock secret handling
///   - Three signer-side refusal implementations
///   - Prometheus heartbeat (/metrics on METRICS_PORT)

fn main() {
    eprintln!(
        "aats-signer v{} — scaffold placeholder. \
         Real implementation: T-251 / T-352a (crypto-security-engineer).",
        env!("CARGO_PKG_VERSION")
    );
    eprintln!("NO inbound network. NO wallet secret loaded in scaffold mode.");
}
