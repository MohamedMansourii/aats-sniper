"""E4 — control-plane exposure hardening: default bind = loopback, never 0.0.0.0.

These tests pin the E4 security invariants that complement the existing
operator-Bearer-token auth (already covered in test_post_commands.py):

  1. The control plane binds to 127.0.0.1 by DEFAULT (no env override).
  2. 'localhost' / ::1 / 127.x are accepted as loopback.
  3. A 0.0.0.0 (or any non-loopback) bind is NOT the default — it is opt-in
     via CONTROL_PLANE_BIND_HOST and must be explicitly chosen.
  4. The production ASGI `app` object the Dockerfile imports actually exists
     and is a FastAPI app, and its destructive POSTs reject an unauthenticated
     request (defense-in-depth: token auth holds independent of bind host).

No server is launched (no real socket bind) — `resolve_bind_host` is a pure
function so the exposure policy is asserted deterministically and offline.
"""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from aats.control_plane.app import (
    DEFAULT_BIND_HOST,
    resolve_bind_host,
    resolve_bind_port,
)
from aats.control_plane.app import app as production_app


# ---------------------------------------------------------------------------
# Default bind = loopback (the E4 invariant)
# ---------------------------------------------------------------------------


def test_default_bind_host_is_loopback():
    """With no override, the control plane binds to 127.0.0.1, never 0.0.0.0."""
    host = resolve_bind_host(env={})
    assert host == DEFAULT_BIND_HOST == "127.0.0.1"
    # Hard assertion: the default must never be a wildcard / all-interfaces bind.
    assert host != "0.0.0.0"
    assert host not in ("::", "0:0:0:0:0:0:0:0")


def test_default_bind_host_with_unset_env_var_still_loopback():
    """An empty CONTROL_PLANE_BIND_HOST falls back to loopback (fail-safe)."""
    assert resolve_bind_host(env={"CONTROL_PLANE_BIND_HOST": ""}) == "127.0.0.1"
    assert resolve_bind_host(env={"CONTROL_PLANE_BIND_HOST": "   "}) == "127.0.0.1"


@pytest.mark.parametrize("loopback", ["127.0.0.1", "127.0.0.5", "localhost", "::1"])
def test_loopback_overrides_accepted(loopback):
    """Loopback overrides are honored without warning."""
    assert resolve_bind_host(env={"CONTROL_PLANE_BIND_HOST": loopback}) == loopback


@pytest.mark.parametrize("exposed", ["0.0.0.0", "::", "10.0.0.5", "192.168.1.10", "example.com"])
def test_non_loopback_bind_is_opt_in_only(exposed, caplog):
    """A non-loopback bind is never the default; it is opt-in and warns loudly."""
    import logging

    with caplog.at_level(logging.WARNING):
        host = resolve_bind_host(env={"CONTROL_PLANE_BIND_HOST": exposed})
    # It is honored (for behind-the-proxy deployments) ...
    assert host == exposed
    # ... but ONLY because explicitly requested, and a SECURITY warning fired.
    assert any("SECURITY" in rec.message for rec in caplog.records), (
        "A non-loopback bind must emit a SECURITY warning documenting the exposure."
    )
    # And critically: it is NOT what you get by default.
    assert resolve_bind_host(env={}) != exposed


def test_bind_port_default_and_override():
    assert resolve_bind_port(env={}) == 8787
    assert resolve_bind_port(env={"CONTROL_PLANE_BIND_PORT": "9999"}) == 9999
    # Invalid -> safe fallback, never crash.
    assert resolve_bind_port(env={"CONTROL_PLANE_BIND_PORT": "not-an-int"}) == 8787


# ---------------------------------------------------------------------------
# The production ASGI app the Dockerfile imports actually exists & is gated
# ---------------------------------------------------------------------------


def test_production_app_object_exists():
    """`aats.control_plane.app:app` (Dockerfile CMD target) is importable & built."""
    assert production_app is not None
    # FastAPI app duck-type: it has a routing table.
    assert hasattr(production_app, "router")


@pytest.mark.asyncio
async def test_production_app_destructive_post_rejects_without_token():
    """Defense-in-depth: even on the production wiring, an unauthenticated
    destructive POST is rejected (403) regardless of bind host."""
    transport = ASGITransport(app=production_app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        for path, body in [
            ("/api/kill", None),
            ("/api/flatten", None),
            ("/api/mode", {"mode": "SHADOW"}),
            ("/api/breaker/reset", None),
            ("/api/risk-config", {}),
        ]:
            resp = await ac.post(path, json=body)
            assert resp.status_code in (401, 403), (
                f"POST {path} without auth must be rejected; got {resp.status_code}"
            )
