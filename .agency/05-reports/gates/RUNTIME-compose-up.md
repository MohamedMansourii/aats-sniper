# RUNTIME — `docker compose up` actually runs the paper stack

_Recorded by `orchestrator` · 2026-06-17 · verified from the bring-up run + the RUST-build / RUNNER /
WIRE dual-G3 records under `C:/dev/aats`._

**Verdict: `docker compose up` now brings the full paper stack live — VERIFIED BY EXECUTION.**
All 11 services start; 9 report `healthy`, 1 is up-by-design without a healthcheck (signer), and the
operator dashboard is reachable and wired to a live control plane showing real (simulated) activity.

---

## 1. The gap that was found

At G5 (Release) and G6 (Acceptance) the deployment was recorded "deployable — ONE `docker compose up`"
on the strength of **`docker compose config` exit 0** (the rendered config parses, 11 services, DRY-RUN
default). That is a **static config-validity check, not a bring-up.** The stack had **never actually been
brought up** end-to-end. When it was finally run, it did **not** start. Three structural gaps plus seven
runtime root causes were exposed:

1. **Rust crates were unbuildable / not real services.** `aats-hotcore` and `aats-signer` declared deps
   that do not exist or do not build — most notably `ort = "1.19"` (the ONNX Runtime crate has no 1.19;
   only `2.0.0-rc.*` is published). `docker build` failed outright, so the hotcore/signer containers
   could never come up.
2. **The controller had no runnable entrypoint.** `aats.controller` had no `__main__`, so the slow-loop
   service had nothing to execute — the container would exit immediately.
3. **Several services had no `/health` endpoint** at the `METRICS_PORT` the compose healthchecks probe
   (slow-loop, DMS, telegram), so even a service that started would never report `healthy`.

This is exactly the failure mode the charter warns about: a green static check (`compose config`) was
rounded up to "runs." It does not. The deployment claim at G5/G6 was **config-valid but unproven at
runtime.** This record corrects that.

---

## 2. The fixes (each verified by execution, dual-G3 where it touched code)

### Rust scaffolds — `RUST-build` (dual G3 PASS)
- Removed every unused / unbuildable dependency, including the non-existent `ort = 1.19`. Each crate now
  depends only on `tokio` + `hyper 1.x` + `http-body-util` + `hyper-util`.
- Rewrote both `main.rs` as **honest minimal scaffold services**: they print `SCAFFOLD PLACEHOLDER` +
  paper-only banners, read `DRY_RUN_ENABLED` (default `true`), serve `GET /health -> 200` on
  `METRICS_PORT` (9102 hotcore / 9105 signer), and loop forever. **No real submit, no key material, no
  live Solana path** — the real implementations remain deferred (signer to T-352a per ADR-0009 /
  COND-G4-2).
- Added `wget` to the hotcore runtime stage (the compose healthcheck needs it); moved `[profile.release]`
  to the workspace root `Cargo.toml` to clear warnings.
- Files: `rust/Cargo.toml`, `rust/aats-hotcore/{Cargo.toml,src/main.rs}`,
  `rust/aats-signer/{Cargo.toml,src/main.rs}`, `docker/Dockerfile.hotcore`.
- Verified: fresh no-cache `docker build` of BOTH images succeeds, zero warnings; `curl :9102/health` and
  `curl :9105/health` both return `ok`; logs confirm `DRY_RUN_ENABLED=true`, no real keys, no live submit;
  secret scan clean.

### Controller entrypoint — `RUNNER` (dual G3 PASS, two independent QA re-runs)
- Added a real `aats/controller/__main__` that runs the SNIPE/FAST/SLOW loop against `SimulationVenue`
  (`submit_mode=SIMULATION` — no network path), with a `DRY_RUN_ENABLED` hard-gate (exit 1 if false), an
  in-memory state store with a clean Redis-from-env fallback, and the control-plane API on the same event
  loop. All synthetic launches are **CLEARLY LABELLED** (`SYNTHETIC` mint prefix, `synthetic=True` on
  every FeedBus frame, `[SYNTHETIC] ... PAPER-ONLY` log stamps). **No win-rate anywhere; money is int
  lamports / Decimal.**
- Fixed all 5 code-reviewer + both QA findings (R-01..R-05, RUNNER-BLK-01, RUNNER-MAJ-01) — most
  materially the cross-loop FeedBus hazard (`run_in_executor` -> `asyncio.create_task(server.serve())`)
  that would have silently dropped SSE frames.
- Files: `aats/controller/__main__.py`, `tests/controller/test_runner_smoke.py`.
- Verified: mypy clean, ruff clean, full consolidated suite **2310 passed / 2 skipped / 0 failed**
  (PYTHONHASHSEED=0, purged cache); independent QA burn-in hit `/api/state`, `/api/feed` (SSE),
  `/api/positions`, `/api/metrics` over real HTTP — frames arrive, a sim position fills, every value is
  int/Decimal, no win-rate.

### Dashboard wired LIVE in docker — `WIRE` (G3 PASS)
- The docker dashboard image now builds **live by default** (`VITE_USE_MOCK=false`,
  `VITE_CONTROL_PLANE_URL=http://localhost:8787`), so `docker compose up` yields a live operator deck.
  `npm`/standalone build stays on the synthetic mock so local dev is not broken. Mock generator is
  explicitly labelled `*** SYNTHETIC — NOT REAL EDGE DATA ***`.
- Files: `docker/Dockerfile.dashboard`, `docker-compose.yml` (dashboard service build-args only), root
  `.env` (gitignored, untracked — no secret committed), `dashboard/.env.example`,
  `dashboard/src/lib/mock.ts`.
- Verified: live build GREEN, mock build GREEN (live bundle ~4 kB smaller — flag genuinely switches the
  data source), `docker compose config` resolves the dashboard args to live, tsc + ESLint clean.

### Seven bring-up root causes (fixed during the bring-up run)
1. `websockets==12.0` conflicted with `solana==0.35.0` + `anchorpy==0.20.1` — pinned to `10.4` (satisfies
   all three).
2. `aats.telegram_bot` module was missing — created a scaffold package serving `/health` on
   `METRICS_PORT` 9104.
3. DMS and slow-loop had no `/health` at their `METRICS_PORT`s — added `run_metrics_server()` calls.
4. Control plane bound to `127.0.0.1` by default — Docker port mapping failed; added
   `CONTROL_PLANE_BIND_HOST=0.0.0.0` to compose.
5. Dashboard nginx healthcheck used `localhost` (BusyBox `wget` resolved it to IPv6 `::1`, refused) —
   changed to `127.0.0.1`.
6. `Dockerfile.dashboard` copied from `/app/dist` but Vite outputs to `dist/public/` — fixed the COPY
   path.
7. `alertmanager.yml` used `${VAR}` shell substitution (alertmanager does not support it) — replaced with
   paper-stack null routing.

---

## 3. `docker compose up` result — what is live

| Service | Status | Port | Note |
|---|---|---|---|
| `redis` | healthy | 6379 (internal only) | no published port |
| `aats-controlplane` | healthy | 8787 | serves `/api/health`, `/api/state`, `/api/positions`; bound `0.0.0.0` for Docker |
| `aats-slow` | healthy | 9101 | controller running synthetic launches (#140+) labelled PAPER-ONLY |
| `aats-hotcore` | healthy | 9102 (internal) | Rust scaffold, `/health -> ok` |
| `aats-signer` | up (no healthcheck by design) | 9105 (internal) | distroless scaffold; healthcheck deferred to T-352a (COND-G4-2 / ADR-0009) |
| `aats-dms` | healthy | 9106 (internal) | watchdog running, `/health` via `run_metrics_server` |
| `aats-telegram` | healthy | 9104 (internal) | scaffold placeholder, `/health` via `run_metrics_server` |
| `dashboard` | healthy | 3000 | AATS React UI, wired to control plane at 8787 |
| `prometheus` | healthy | 9090 | |
| `grafana` | healthy | 3001 | |
| `alertmanager` | healthy | 9093 | null routing (paper-stack); Telegram/PagerDuty receivers are commented R3 templates |

**Live, verified:** `compose up` succeeds; control plane is live (`/api/state` shows
`DRY_RUN_ENABLED=true`, mode PAPER, no win-rate); dashboard is reachable and renders the AATS UI; the
slow-loop controller is actively producing simulated launch/position activity; full monitoring stack
(Prometheus / Grafana / Alertmanager) is up.

---

## 4. HONEST scope of "live" — simulation, not real edge

What is running is a **paper stack on synthetic data**, not a live trading system:

- **Live = SIMULATED activity.** The slow-loop runs against `SimulationVenue` (`submit_mode=SIMULATION`,
  no network path). Every launch and position is **synthetic, clearly labelled** (`SYNTHETIC` mint,
  `synthetic=True` frames, `[SYNTHETIC] ... PAPER-ONLY` logs). The dashboard shows this sim activity, not
  market reality.
- **Still requires real data — Stage 2 (Geyser).** There is **no live on-chain ingestion**. The real
  Solana Geyser/RPC feed, the recorded-data edge proof (GATE-A / GATE-B on RECORDED data), and the real
  hotcore/signer implementations are **NOT built** — the Rust services are honest placeholders. Edge
  remains **`UNPROVEN-NO-REAL-DATA`**, exactly as recorded at G4/G6.
- **Real capital stays DISABLED + UNREACHABLE.** `DRY_RUN_ENABLED=true` on all tx-capable services; no
  real keys; no live submit; the R3 pre-live checklist (Block A edge-on-recorded-data · Block B
  custody/security COND-G4-2 · Block C CEO legal+funding+sign-off) is the gate before
  `DRY_RUN_ENABLED=false` and is **NOT** cleared by this work.

---

## 5. Remaining blockers (none block paper bring-up; all gated to R3 / future tasks)

1. **`aats-signer` healthcheck disabled** — distroless image has no `wget`/`curl`; real check deferred to
   T-352a when the real signer lands. Hotcore `depends_on` uses `condition: service_started`, so `compose
   up` is unaffected.
2. **Split in-memory state** — `aats-slow` and `aats-controlplane` run separate in-memory stores, so the
   standalone control plane's `/api/positions` returns `[]` while positions ARE generated in the runner
   logs. Real Redis-backed state sharing is a **T-340 wiring task**.
3. **Alertmanager routing is null** for the paper stack — the live operator must inject credentials and
   uncomment the Telegram/PagerDuty receiver templates at **R3**.
4. **Node image lag** — `Dockerfile.dashboard` uses `node:20.14.0-slim` but Vite 7.3.0 wants `>=20.19.0`
   (warning only; build succeeds). Bump before **R3**.

None of these prevent `docker compose up` from running the paper stack today. All are scoped to R3 or to
existing wiring tasks (T-340 / T-352a) and are out of scope for "make the paper stack run."

---

## 6. Verdict

`docker compose up` **runs the paper stack live** — the G5/G6 "ONE `docker compose up`" claim is now true
in execution, not just in `compose config`. The system is honestly a **paper / simulation deployment on
synthetic data**: live operator deck + live control plane + active sim activity + full monitoring, with
**no real on-chain data (Stage 2 Geyser unbuilt), unproven edge, and real capital disabled and
unreachable.** No regression to the accepted core; safety primitives (breaker / survivable-stop / DMS)
untouched and present. The R3 pre-live checklist remains the gate before any real capital.
