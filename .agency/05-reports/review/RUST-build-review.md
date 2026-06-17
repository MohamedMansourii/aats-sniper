# Code Review — RUST-build (aats-hotcore + aats-signer Rust scaffolds)

**Reviewer:** code-reviewer (Quality Gate, Gate G3)
**Date:** 2026-06-17
**Verdict:** **PASS**
**One-line:** Both Rust crates build clean (no-cache, zero warnings) and run as honest, paper-only `/health` scaffolds with no real submit/secret — ship it.

---

## Scope reviewed
Files in the engineer handoff, every changed line plus surrounding integration:
- `rust/Cargo.toml` (workspace + release profile)
- `rust/aats-hotcore/Cargo.toml`, `rust/aats-hotcore/src/main.rs`
- `rust/aats-signer/Cargo.toml`, `rust/aats-signer/src/main.rs`
- `docker/Dockerfile.hotcore`, `docker/Dockerfile.signer`
- `docker-compose.yml` (healthcheck wiring for both services)

## Verification by execution (run by reviewer, not assumed)
Local `cargo`/`rustc` are not installed; the canonical build path is Docker (allowed by the task), so all builds were run through Docker.

| Check | Command | Result |
|---|---|---|
| hotcore build (fresh, no cache) | `docker build --no-cache --target rust-builder -f docker/Dockerfile.hotcore .` | `Compiling aats-hotcore v0.1.0` → `Finished release profile [optimized] in 1m37s`. **Zero warnings, zero errors.** Exit 0 |
| signer build (fresh, no cache) | `docker build --no-cache --target rust-builder -f docker/Dockerfile.signer .` | `Compiling aats-signer v0.1.0` → `Finished release profile [optimized] in 1m53s`. **Zero warnings, zero errors.** Exit 0 (workspace `panic=abort` profile builds fine) |
| full images | `docker build -f docker/Dockerfile.{hotcore,signer} .` | Both export images successfully (distroless runtime for signer, debian-slim+wget for hotcore) |
| hotcore /health | `curl localhost:9102/health` | `HTTP 200`, body `ok`; bad path → `HTTP 404` |
| signer /health | `curl localhost:9105/health` | `HTTP 200`, body `ok` |
| hotcore liveness | `docker inspect .State.Health` | transitions to **healthy** (in-image HEALTHCHECK on 9102) |
| both stay up | `docker ps` | both `Up`, no crash/exit |
| compose parse | `docker compose config --quiet` | **VALID** |
| dep scan | grep Cargo.toml for ort/solana/reqwest/etc. | only in removed-dep comments; **no live declarations**. `ort` fully gone |
| secret scan (source + logs) | grep source + `docker logs` | **clean** — no hardcoded secrets, no key material in logs |
| submit/sign logic scan | grep source for sign/submit/solana/keypair/Vault | matches are **comments + log strings only**; no real logic |

## Logs confirm honest scaffold + paper-only
- hotcore: `SCAFFOLD PLACEHOLDER … FUTURE WORK`, `DRY_RUN_ENABLED=true … NO real keys, NO real capital, NO live Solana submit`
- signer: `SCAFFOLD PLACEHOLDER … NO wallet secret is loaded. NO signing operations performed … paper-only mode`

## Conformance
- **Blueprint / ADRs (ADR-0002 hot path, ADR-0009 signer custody):** ✓ — comments accurately state the boundary; scaffold deliberately does NOT load a secret, listen on the signer socket, or touch Solana. No premature implementation of custody.
- **Compose healthcheck wiring:** ✓ (hotcore) — compose healthcheck `wget http://localhost:9102/health` matches the served port; in-image HEALTHCHECK reaches healthy. Signer healthcheck is `disable: true` by design (distroless has no wget/curl; real HTTP check lands at T-352a) — documented in compose lines 91–93, 112–119 and Dockerfile.signer lines 44–45. Consistent across Dockerfile, compose, and source.
- **Dependencies vs blueprint:** ✓ — only tokio + hyper 1.x + http-body-util + hyper-util in each crate; nothing added beyond what the `/health` scaffold needs; the non-existent `ort = "1.19"` removed.
- **Tests:** N/A for a placeholder scaffold — no behavior to unit-test beyond the `/health` contract, which is verified by the live curl + healthcheck above. Not a blocker for a documented scaffold.
- **Consistency / maintainability:** ✓ — identical structure across both binaries, idiomatic hyper 1.x, accept-loop errors logged-and-continued (no panic), bind failure exits non-zero cleanly. Workspace-level `[profile.release]` correctly placed (per-crate profiles are ignored by cargo) with an honest comment on `panic=abort` safety.

## Findings

### NIT-1 — "serves /health on the compose healthcheck port" is literally true only for hotcore
`file: docker-compose.yml:112-119`, `docker/Dockerfile.signer:44-45`
The signer **binary serves /health on 9105**, but no compose healthcheck probes it (`disable: true`). This is a deliberate, well-documented choice (distroless has no wget/curl; real check deferred to T-352a) and `aats-hotcore depends_on aats-signer: condition: service_started`, so it does not break `docker compose up`. Good-looks-like: when T-352a lands, enable a binary-self `--health-check` (the binary already has an HTTP endpoint, so a tiny self-probe subcommand or a static-curl sidecar would close the gap). **Does not block** — informational so the next owner doesn't forget the signer endpoint is live but unwatched.

### NIT-2 — no `Cargo.lock` committed for the workspace
`file: rust/`
A binary workspace would normally commit `Cargo.lock` for reproducible builds. For a 4-dep scaffold the risk is negligible and the Docker build resolves cleanly today. Worth adding when real deps (solana-sdk, etc.) land. **Does not block.**

No BLOCKER, MAJOR, or MINOR findings.

## Conclusion
Every task requirement is met and verified by execution: both crates build (Docker stage succeeds, fresh no-cache, zero warnings), no `ort`/unused deps remain, both binaries stay running and serve `/health`, comments are honest scaffold placeholders, and there is no real submit logic or secret material. DRY_RUN read is present and defaults to true. **PASS.**
