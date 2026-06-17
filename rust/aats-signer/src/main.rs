/// AATS Signer — SCAFFOLD PLACEHOLDER.
///
/// The real signing logic (Vault boot, mlock secret, Unix-domain socket
/// listener, three independent refusals) is NOT yet implemented.
/// Real implementation: T-251 / T-352a (crypto-security-engineer).
///
/// What this scaffold does:
///   1. Logs a clear startup message (scaffold / no secret loaded / no signing).
///   2. Serves HTTP GET /health -> 200 OK on METRICS_PORT (default 9105)
///      so that when the compose healthcheck is re-enabled (COND-G4-2 / T-352a)
///      it finds a live endpoint.
///   3. Loops forever — the container stays UP.
///
/// Security contract (ADR-0009, infrastructure.md §5):
///   - NO wallet secret is loaded or held in scaffold mode.
///   - NO signing operations are performed.
///   - NO inbound network beyond the health/metrics port.
///   - The signer-socket volume is present but the scaffold does NOT listen on it.

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
    // --- Startup banner (clearly labelled as scaffold / no secret / no signing)
    eprintln!(
        "aats-signer v{} — SCAFFOLD PLACEHOLDER. \
         Real signing logic (Vault boot, mlock secret, Unix-domain socket, \
         three independent refusals) is FUTURE WORK (T-251 / T-352a). \
         NO wallet secret is loaded. NO signing operations performed. \
         The custody policy lives in custody-policy.md; \
         real implementation is owned by crypto-security-engineer.",
        env!("CARGO_PKG_VERSION")
    );
    eprintln!(
        "aats-signer: scaffold running in paper-only mode. \
         NO real keys, NO real capital, NO live Solana submit."
    );

    // --- HTTP /health server (distroless-compatible: pure Rust, no shell deps)
    let metrics_port: u16 = std::env::var("METRICS_PORT")
        .ok()
        .and_then(|s| s.parse().ok())
        .unwrap_or(9105);

    let addr = SocketAddr::from(([0, 0, 0, 0], metrics_port));
    let listener = TcpListener::bind(addr).await.unwrap_or_else(|e| {
        eprintln!("aats-signer: failed to bind {addr}: {e}");
        std::process::exit(1);
    });

    eprintln!("aats-signer: health endpoint listening on http://{addr}/health");

    // Accept connections forever — container stays UP.
    loop {
        let (stream, _peer) = match listener.accept().await {
            Ok(conn) => conn,
            Err(e) => {
                eprintln!("aats-signer: accept error: {e}");
                continue;
            }
        };
        let io = TokioIo::new(stream);
        tokio::spawn(async move {
            if let Err(e) = http1::Builder::new()
                .serve_connection(io, service_fn(handle))
                .await
            {
                eprintln!("aats-signer: connection error: {e}");
            }
        });
    }
}
