/// AATS Hot Core — SCAFFOLD PLACEHOLDER.
///
/// The real SNIPE + FAST loop logic is NOT in Rust yet.
/// Paper trading runs entirely in the Python controller.
/// This binary exists to keep the container UP + HEALTHY so that
/// `docker compose up` succeeds and Prometheus sees a live target.
///
/// What this scaffold does:
///   1. Logs a clear startup message (scaffold / paper-only / no real submit).
///   2. Reads DRY_RUN_ENABLED from the environment (default: "true").
///      Real submission is disabled by default and is never called here.
///   3. Serves HTTP GET /health -> 200 OK on METRICS_PORT (default 9102)
///      so the compose healthcheck succeeds.
///   4. Loops forever — the container stays UP + HEALTHY.
///
/// Architecture boundary (ADR-0002, BLUEPRINT §4.1):
///   Python communicates with the hot path only via Redis Streams + KV.
///   Wallet signing lives entirely in aats-signer (ADR-0009).
///
/// TODO (filled by solana-execution-engineer + mev-latency-engineer):
///   T-300: Geyser/Yellowstone gRPC ingest + decoder
///   T-327: JitoJupiterVenue (DRY-RUN first, then live)
///   T-323: 0-RPC safety gate (≤10ms, checks 1–5)
///   T-324: cost gate (edge > cost or NO TRADE)
///   T-330: TipController (live tip_stream → Redis KV cache)
///   T-331: MEV modes (fast vs secure)
///   T-340: SNIPE + FAST loop FSM, heartbeat emit

use std::convert::Infallible;
use std::net::SocketAddr;

use http_body_util::Full;
use hyper::body::Bytes;
use hyper::server::conn::http1;
use hyper::service::service_fn;
use hyper::{Request, Response, StatusCode};
use hyper_util::rt::TokioIo;
use tokio::net::TcpListener;

/// Serve GET /health -> 200 OK, everything else -> 404.
async fn handle(req: Request<hyper::body::Incoming>) -> Result<Response<Full<Bytes>>, Infallible> {
    match (req.method(), req.uri().path()) {
        (&hyper::Method::GET, "/health") => {
            let body = Bytes::from_static(b"ok");
            Ok(Response::builder()
                .status(StatusCode::OK)
                .header("content-type", "text/plain")
                .body(Full::new(body))
                .unwrap())
        }
        _ => {
            let body = Bytes::from_static(b"not found");
            Ok(Response::builder()
                .status(StatusCode::NOT_FOUND)
                .body(Full::new(body))
                .unwrap())
        }
    }
}

#[tokio::main]
async fn main() {
    // --- Startup banner (clearly labelled as scaffold / paper-only) ----------
    eprintln!(
        "aats-hotcore v{} — SCAFFOLD PLACEHOLDER. \
         Real Rust hot path (SNIPE + FAST loops) is FUTURE WORK. \
         Paper trading runs in the Python controller (aats-slow / aats-controlplane).",
        env!("CARGO_PKG_VERSION")
    );

    let dry_run = std::env::var("DRY_RUN_ENABLED")
        .unwrap_or_else(|_| "true".to_string());

    eprintln!(
        "DRY_RUN_ENABLED={dry_run} (default: true) — \
         real transaction submission is DISABLED in scaffold mode. \
         NO real keys, NO real capital, NO live Solana submit."
    );

    // --- HTTP /health server -------------------------------------------------
    let metrics_port: u16 = std::env::var("METRICS_PORT")
        .ok()
        .and_then(|s| s.parse().ok())
        .unwrap_or(9102);

    let addr = SocketAddr::from(([0, 0, 0, 0], metrics_port));
    let listener = TcpListener::bind(addr).await.unwrap_or_else(|e| {
        eprintln!("aats-hotcore: failed to bind {addr}: {e}");
        std::process::exit(1);
    });

    eprintln!("aats-hotcore: health endpoint listening on http://{addr}/health");

    // Accept connections forever — container stays UP + HEALTHY.
    loop {
        let (stream, _peer) = match listener.accept().await {
            Ok(conn) => conn,
            Err(e) => {
                eprintln!("aats-hotcore: accept error: {e}");
                continue;
            }
        };
        let io = TokioIo::new(stream);
        tokio::spawn(async move {
            if let Err(e) = http1::Builder::new()
                .serve_connection(io, service_fn(handle))
                .await
            {
                // Ignore closed-connection errors from healthcheck probes.
                eprintln!("aats-hotcore: connection error: {e}");
            }
        });
    }
}
