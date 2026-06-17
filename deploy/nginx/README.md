# Control-plane exposure hardening (E4)

The AATS control plane carries the destructive de-risk surface (`/api/kill`,
`/api/flatten`, `/api/flatten/{mint}`, `/api/mode`, `/api/breaker/reset`,
`/api/risk-config`). This directory holds the **documented** recipe for exposing
it safely. These are config artifacts, not a running server.

## The layered model

```
                       TLS              IP allowlist        bearer token       de-risk-only
  remote operator --[ nginx :443 ]--[ allow/deny map ]--> 127.0.0.1:8787 --> [ app auth ] --> [ contract ]
                   (deploy/nginx/aats-controlplane.conf)   (loopback only)   server.py        every POST
```

Each layer is independent; an attacker must defeat all of them. The app already
enforces the inner two (operator bearer token on every POST, de-risk-only
contract — both PASS in `G4-security-audit.md` §4/§5). E4 adds/locks the outer
two and the loopback bind.

## What E4 changed (and what it did not)

- **Default bind = loopback.** `aats/control_plane/app.py` is the production
  entrypoint. `resolve_bind_host()` returns `127.0.0.1` by default and only
  binds a non-loopback address if `CONTROL_PLANE_BIND_HOST` is explicitly set,
  in which case it logs a `SECURITY:` warning. `Dockerfile.controlplane` now
  launches `python -m aats.control_plane.app` instead of
  `uvicorn ... --host 0.0.0.0`.
- **Host port restricted to loopback.** Apply
  `deploy/docker-compose.controlplane-bind.override.yml` (additive — the base
  `docker-compose.yml` is unchanged) to publish `127.0.0.1:8787:8787` and
  `127.0.0.1:3000:3000`.
- **Reverse proxy recipe.** `aats-controlplane.conf` terminates TLS, enforces an
  IP allowlist (deny-by-default `geo` map), rate-limits, optionally adds HTTP
  Basic auth, and proxies to the loopback upstream. SSE (`/api/feed`) buffering
  is disabled so the live feed streams.
- **Unchanged:** the operator bearer token on every POST, the CEO-auth + DRY-RUN
  gate on LIVE, the de-risk-only semantics, all money/int/Decimal rules. No
  endpoint can increase risk. The safety primitives (breaker / survivable-stop /
  DMS) are untouched.

## Deploy steps (operator)

1. Edit `aats-controlplane.conf`: set `server_name`, the TLS cert/key paths, and
   the operator source IPs in the `geo $aats_cp_allowed` map (deny-by-default).
2. (Optional) create `htpasswd` and uncomment the `auth_basic` block.
3. Restrict host port exposure:
   ```
   docker compose \
     -f docker-compose.yml \
     -f deploy/docker-compose.controlplane-bind.override.yml \
     up -d
   ```
4. Place `aats-controlplane.conf` in `/etc/nginx/conf.d/`, install the certs,
   `nginx -t`, reload. Verify from a non-allowlisted IP that you get `403`, and
   from an allowlisted IP over TLS that a bearer-token POST is accepted and a
   token-less POST is `403`.

## The 0.0.0.0 risk (why loopback is the default)

Binding the API to `0.0.0.0` puts the kill/flatten/mode surface on every host
interface, gated only by the bearer token (which often lives in the operator's
`.env`). Anyone who can route to the box — a co-tenant, a misconfigured
firewall, a leaked token — can then halt trading or, worse, probe for auth
weaknesses against a live financial control plane. Loopback-by-default means the
API is invisible off-box until the operator deliberately fronts it with this
proxy. Honoring a non-loopback bind is therefore opt-in and always logged.
