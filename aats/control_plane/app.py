"""M3 Control-Plane — production ASGI entrypoint + bind-host exposure policy (E4).

This module is the thing `uvicorn aats.control_plane.app:app` imports and serves.
It exists for ONE security reason on top of building the frozen-contract app:

    THE CONTROL PLANE BINDS TO LOOPBACK (127.0.0.1) BY DEFAULT, NEVER 0.0.0.0.

The destructive POSTs (/api/kill, /api/flatten, /api/flatten/{mint}, /api/mode,
/api/breaker/reset, /api/risk-config) are already operator-Bearer-token gated in
`server.py` (see `_check_operator_auth`). Token auth is the authorization control;
binding to loopback is the *exposure* control — defense-in-depth. A control plane
that listens on 0.0.0.0 puts the de-risk/kill surface on every interface of the
host, reachable by anything that can route to the box, with only a bearer token
(which may sit in the operator's `.env`) between an attacker and the kill switch.

EXPOSURE POLICY (E4):
  - Default bind host is **127.0.0.1** (loopback only). The dashboard and the
    Telegram command bot run on the same host / docker network and reach the API
    locally; remote operators go through the documented nginx reverse proxy
    (`deploy/nginx/`) which adds TLS + an IP allowlist + (optionally) basic-auth
    in front of the bearer-token app — they do NOT widen the app's own bind.
  - `CONTROL_PLANE_BIND_HOST` may override the bind, but choosing `0.0.0.0`
    (or any non-loopback address) emits a loud SECURITY WARNING at boot and is
    only acceptable behind the reverse proxy on an isolated network. This is the
    documented 0.0.0.0 risk: it is opt-in, never the default, and it screams.
  - In docker-compose the service is on the internal bridge network; the
    published `8787:8787` port should be changed to `127.0.0.1:8787:8787` (host
    loopback only) via the documented override file
    `deploy/docker-compose.controlplane-bind.override.yml` so the API is not
    reachable from the host's external interfaces. We do NOT edit the base
    docker-compose.yml here (parallel-lane rule); the override is additive.

HARD RULES honored: no secrets in code (tokens come from env/`.env.example`
placeholders only); money untouched; de-risk-only semantics of `server.py`
are UNCHANGED — this module only wires dependencies and decides the bind host.
"""

from __future__ import annotations

import ipaddress
import logging
import os
from typing import Any

from aats.control_plane.server import ControlPlaneConfig, FeedBus, build_app

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Bind-host exposure policy (the E4 control)
# ---------------------------------------------------------------------------

#: The safe default: loopback only. Nothing off-box can reach the kill switch.
DEFAULT_BIND_HOST = "127.0.0.1"

#: Env var an operator may set to override the bind host. Anything that is not a
#: loopback address triggers a loud security warning (and is only sane behind the
#: documented reverse proxy on an isolated network).
BIND_HOST_ENV = "CONTROL_PLANE_BIND_HOST"

DEFAULT_BIND_PORT = 8787
BIND_PORT_ENV = "CONTROL_PLANE_BIND_PORT"


def _is_loopback(host: str) -> bool:
    """True iff `host` is a loopback address (127.0.0.0/8, ::1) or 'localhost'.

    A hostname other than 'localhost' is treated as non-loopback (we cannot
    resolve it safely/deterministically here, and the secure default must be
    fail-safe: anything we are not certain is loopback is treated as exposed).
    """
    h = (host or "").strip().lower()
    if h in ("localhost",):
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


def resolve_bind_host(env: dict[str, str] | None = None) -> str:
    """Resolve the bind host per the E4 exposure policy.

    Pure function (no I/O beyond reading the provided env mapping) so the test
    suite can assert the default is loopback without launching a server.

    - No override set            -> DEFAULT_BIND_HOST (127.0.0.1).
    - Override is loopback        -> that loopback address.
    - Override is non-loopback    -> that address, but a SECURITY WARNING is
                                     logged (documenting the 0.0.0.0 risk).
                                     The operator opted in explicitly; we honor
                                     it (for behind-the-proxy deployments) but
                                     never silently.
    """
    src = os.environ if env is None else env
    raw = src.get(BIND_HOST_ENV, "").strip()
    if not raw:
        return DEFAULT_BIND_HOST
    if _is_loopback(raw):
        return raw
    # Non-loopback bind requested — honor but scream.
    log.warning(
        "SECURITY: control plane requested to bind to %r (non-loopback). "
        "The destructive de-risk/kill surface will be reachable on that "
        "interface, gated ONLY by the operator bearer token. This is acceptable "
        "ONLY behind the documented nginx reverse proxy (TLS + IP allowlist) on "
        "an isolated network (deploy/nginx/). The secure default is %s. "
        "If you did not intend this, unset %s.",
        raw,
        DEFAULT_BIND_HOST,
        BIND_HOST_ENV,
    )
    return raw


def resolve_bind_port(env: dict[str, str] | None = None) -> int:
    """Resolve the bind port (default 8787); never affects host exposure."""
    src = os.environ if env is None else env
    raw = src.get(BIND_PORT_ENV, "").strip()
    if not raw:
        return DEFAULT_BIND_PORT
    try:
        return int(raw)
    except ValueError:
        log.warning("invalid %s=%r — falling back to %d", BIND_PORT_ENV, raw, DEFAULT_BIND_PORT)
        return DEFAULT_BIND_PORT


# ---------------------------------------------------------------------------
# Production app wiring
# ---------------------------------------------------------------------------


def _build_production_app() -> Any:
    """Wire the frozen-contract control-plane app with production dependencies.

    External services (Redis/RPC/LLM) are injectable; in the absence of a wired
    runtime controller this builds the app against in-memory stores so the
    container starts, serves the read-only GETs, and the auth-gated POSTs behave
    per contract. The real controller injects its own StateStore/FastLoop/
    CircuitBreaker via `build_app` at process start when present.

    No secrets are read here. Tokens are resolved by `ControlPlaneConfig` from
    the environment (`OPERATOR_TOKEN`, `CEO_TOKEN`) whose placeholders live in
    `.env.example` (OPERATOR_API_TOKEN / CEO_AUTH_TOKEN).
    """
    from aats.controller.control_api import KillSwitch
    from aats.controller.state import InMemoryStateStore
    from aats.risk.circuit_breaker import CircuitBreaker, InMemoryBreakerStore
    from aats.contracts.risk import RiskConfig

    store = InMemoryStateStore()
    kill_switch = KillSwitch()

    class _NullFlattener:
        def emergency_flatten_all(self, reason: str) -> None:  # pragma: no cover - wiring stub
            log.warning("control_plane: flatten requested with no wired flatten_handler (%s)", reason)

    breaker = CircuitBreaker(
        config=RiskConfig(),
        store=InMemoryBreakerStore(),
        flatten_handler=_NullFlattener(),
    )
    feed_bus = FeedBus()
    config = ControlPlaneConfig()

    return build_app(
        state_store=store,
        fast_loop=None,
        kill_switch=kill_switch,
        breaker=breaker,
        feed_bus=feed_bus,
        config=config,
    )


# The ASGI application object uvicorn imports: `aats.control_plane.app:app`.
app = _build_production_app()


def main() -> None:
    """Serve the control plane, binding to loopback by default (E4).

    Equivalent to `uvicorn aats.control_plane.app:app` but with the bind host
    resolved through `resolve_bind_host` so the default is 127.0.0.1 and a
    0.0.0.0 bind is explicit + warned. Used when launched as a module:
        python -m aats.control_plane.app
    """
    import uvicorn  # local import: not needed for ASGI import-time

    host = resolve_bind_host()
    port = resolve_bind_port()
    log.info("control_plane: serving on %s:%d (default loopback; E4 exposure policy)", host, port)
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":  # pragma: no cover - process entrypoint
    main()
