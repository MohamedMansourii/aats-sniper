"""Minimal /metrics HTTP endpoint for Prometheus scraping.

Each Python service exposes this on its configured metrics port.
Usage:
    from aats.telemetry.http_endpoint import run_metrics_server
    run_metrics_server(port=int(os.environ.get("METRICS_PORT", "9090")))
"""
from __future__ import annotations

import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Optional

try:
    from prometheus_client import generate_latest, CONTENT_TYPE_LATEST
    _PROM_AVAILABLE = True
except ImportError:
    _PROM_AVAILABLE = False


class _MetricsHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/metrics":
            if _PROM_AVAILABLE:
                output = generate_latest()
                self.send_response(200)
                self.send_header("Content-Type", CONTENT_TYPE_LATEST)
                self.send_header("Content-Length", str(len(output)))
                self.end_headers()
                self.wfile.write(output)
            else:
                body = b"# prometheus_client not installed\n"
                self.send_response(503)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(body)
        elif self.path == "/health":
            body = b'{"status":"ok"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args: object) -> None:  # silence access log
        pass


def run_metrics_server(
    port: int = 9100,
    host: str = "0.0.0.0",
    daemon: bool = True,
) -> Optional[threading.Thread]:
    """Start the /metrics HTTP server in a background thread.

    Args:
        port:   Port to bind.  Each service uses its own METRICS_PORT env var.
        host:   Bind address.
        daemon: If True (default) the thread dies with the process.

    Returns the thread object so callers can join if needed.
    """
    server = HTTPServer((host, port), _MetricsHandler)
    t = threading.Thread(target=server.serve_forever, daemon=daemon)
    t.start()
    return t
