"""`python -m aats.telegram_bot` — aats-telegram service entrypoint scaffold.

SCAFFOLD PLACEHOLDER — real wiring in T-360 / T-361.

What this does:
  1. Hard-gates on DRY_RUN_ENABLED: exits(1) if false (no real capital).
  2. Starts the /health + /metrics HTTP server on METRICS_PORT (default 9104).
  3. Logs startup banners: paper-only, no real Telegram sends, no bot token used.
  4. Loops forever (select-wait on a stop event) until SIGTERM/SIGINT.

What this does NOT do:
  - No Telegram HTTP API calls.
  - No bot token loaded or used.
  - No feed consumption, no alert dispatch.
  - No secrets — .env.example placeholders only.

These are the T-360/T-361 seams; the real service is in aats.telegram.service
and will be wired here once the compose stack is proven healthy end-to-end.
"""
from __future__ import annotations

import os
import signal
import sys
import threading

# ---------------------------------------------------------------------------
# Hard gate: DRY_RUN_ENABLED must be "true" (or absent).  We exit(1) if it is
# explicitly "false" — real capital is never enabled from a scaffold process.
# ---------------------------------------------------------------------------
_DRY_RUN = os.environ.get("DRY_RUN_ENABLED", "true").strip().lower()
if _DRY_RUN == "false":
    print(  # noqa: T201
        "[aats-telegram] FATAL: DRY_RUN_ENABLED=false detected — "
        "real capital is DISABLED by default. "
        "This scaffold NEVER enables live Telegram sends. Exiting.",
        file=sys.stderr,
        flush=True,
    )
    sys.exit(1)

_METRICS_PORT = int(os.environ.get("METRICS_PORT", "9104"))


def _start_health_server(port: int) -> None:
    """Start /health + /metrics HTTP server in a background daemon thread."""
    from aats.telemetry.http_endpoint import run_metrics_server

    run_metrics_server(port=port)


def main() -> None:  # pragma: no cover — process entrypoint
    """Run the aats-telegram scaffold until stopped."""
    stop = threading.Event()

    def _handle_signal(*_: object) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    _start_health_server(_METRICS_PORT)

    # Boot banner — paper-only, no real tokens, no real sends.
    print(  # noqa: T201
        f"[aats-telegram] SCAFFOLD PLACEHOLDER — paper-only. "
        f"DRY_RUN_ENABLED=true. "
        f"NO Telegram bot token loaded. "
        f"NO real Telegram sends. "
        f"NO feed consumed. "
        f"Serving /health on port {_METRICS_PORT}. "
        f"Awaiting T-360/T-361 real wiring.",
        flush=True,
    )

    # Wait until stopped (SIGTERM/SIGINT from `docker compose stop`).
    stop.wait()
    print("[aats-telegram] shutdown — goodbye.", flush=True)  # noqa: T201


if __name__ == "__main__":
    main()
