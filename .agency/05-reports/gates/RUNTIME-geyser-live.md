# RUNTIME RECORD — GEYSER-live: Yellowstone Geyser gRPC live-ingestion wiring

**Task:** `GEYSER-live` — real Yellowstone/Dragon's-Mouth gRPC live transport
**Date:** 2026-06-17
**Recorded by:** `orchestrator` (verified against actual source under `C:/dev/aats/aats/ingestion/`)
**Gate status:** dual G3 PASS (`code-reviewer` PASS + `backtest-qa-engineer` PASS)
**Verdict:** RECORDED — code is correct and offline-proven; LIVE ingestion remains UNVALIDATED pending operator key.

---

## 1. What was implemented (verified from source, not from handoff JSON)

The `GeyserTransport` in `aats/ingestion/transport.py` is now a **real Yellowstone gRPC
client**, not a stub. Confirmed by direct read of the source:

- **`subscribe()` (`transport.py:300`)** — the live entrypoint. Returns early and yields
  nothing if `GEYSER_ENDPOINT` is empty (`:318`); otherwise drives an infinite
  reconnect loop with exponential backoff + jitter (`:392-404`), resuming from
  `from_slot = self._last_slot` on every reconnect. `CancelledError` propagates for
  clean shutdown (`:378-382`); `AioRpcError` and generic exceptions are absorbed and
  retried (`:370-390`).
- **`_stream_once()` (`transport.py:406`)** — opens one `grpc.aio.secure_channel` with
  TLS + per-call `x-token` metadata credentials (`:424-431`), builds the
  `SubscribeRequest` via the pure helper, sends it as a one-element async iterator on the
  bidirectional `Subscribe` RPC (`:464-467`), and consumes the server's `SubscribeUpdate`
  stream — skipping non-transaction updates via `HasField("transaction")` (`:468`).
- **`_build_subscribe_request()` (`transport.py:669`)** — pure, testable helper (extracted
  per review finding B2). Filters TRANSACTIONS by `account_include = sorted(program_ids)`,
  `vote=False`, `failed=False`, `commitment=PROCESSED (0)`, and sets `from_slot` only when
  `> 0`. Matches the Yellowstone proto contract.
- **`_parse_geyser_tx()` (`transport.py:483`)** — converts a `SubscribeUpdateTransaction`
  into a `RawTransaction`: signature bytes → base58, slot passthrough, ALT
  `loaded_writable` + `loaded_readonly` keys appended after static account keys,
  instruction data → base64, inner instructions flattened, program logs passed through.
- **Vendored proto stubs** — `aats/ingestion/geyser_proto/` carries `geyser_pb2`,
  `geyser_pb2_grpc`, `solana_storage_pb2` (+ `.proto` sources / `.pyi`); `grpcio==1.64.1`
  and `grpcio-tools==1.64.1` are pinned in `requirements/requirements.txt`.
- **`shadow_record.py`** — `--source=geyser` reads `GEYSER_ENDPOINT` + `GEYSER_TOKEN`
  from environment only (`shadow_record.py:320-321`), warns and exits cleanly if unset.
  All `PLUG_IN_HERE` clauses removed (finding B3); the HONESTY NOTICE (`:17-31`) states
  that `--source=replay` is SYNTHETIC/DETERMINISTIC and carries NO edge signal.

**Point-in-time correctness (the one law) — HELD.** `_parse_geyser_tx()` hard-sets
`block_time_unix_s = None` for every PROCESSED Geyser update (`transport.py:610`). The
decoder holds such events PENDING rather than substituting wall-clock (T-300a). The
transport CANNOT fabricate an event time.

---

## 2. How it is offline-tested (the proof that exists today)

`tests/ingestion/test_geyser_transport.py` — **42/42 passing**; full `tests/ingestion/`
suite **393/393**, zero regressions.

- **`TestSubscribeStreamPath` (9 tests)** patches `geyser_pb2_grpc.GeyserStub` with a fake
  async iterator of `SubscribeUpdate` protos and drives `subscribe()` / `_stream_once()`
  through: valid-tx yield, slot-only `HasField` skip, parse-exception-caught-and-continues,
  captured-`SubscribeRequest` field assertions, `is_connected` transitions, `AioRpcError`
  absorbed, generic `Exception` absorbed, `CancelledError` propagates. `transport.py`
  coverage rose 52% → **78%** (lines 330-404 and 424-492 no longer in the missing list).
- **`TestBuildSubscribeRequest` (7 tests)** asserts on the PRODUCTION
  `_build_subscribe_request()` function (not a local copy).
- **Mutation-meaningful (the load-bearing QA check):** backtest-qa mutated production
  `transport.py` twice (`vote=False`→`True`; disabled the slot-skip) → 3 tests went RED.
  The new tests assert on production output and have real teeth.
- **End-to-end fixture path (AC-10):** `SubscribeUpdateTransaction` → `_parse_geyser_tx`
  → `RawTransaction`; with `block_time=None` the decoder correctly returns None (T-300a),
  and with an injected block_time it decodes to a PUMPFUN `LaunchEvent`.
- **Dedicated leak guards:** `test_t300a_block_time_leak.py` (33) + `test_point_in_time.py`
  (21), all green.

What the offline suite proves: **parse + transport correctness, reconnect/resume
semantics, credential handling, and point-in-time honesty** — all without a network.

---

## 3. The HONEST caveat — LIVE validation is NOT done

**This task wired and offline-proved the Geyser transport. It did NOT validate it against
a live feed, and could not.**

- **Stage 2 still needs the operator key.** Running `--source=geyser` against real
  on-chain activity requires a Helius / Triton / QuickNode Yellowstone endpoint and token,
  supplied at runtime via `GEYSER_ENDPOINT` + `GEYSER_TOKEN` (env only; see `.env.example`).
  No live endpoint was exercised in this task — correct and expected, since it requires a
  real operator-provided key. Live wallet/RPC custody safety remains the
  `crypto-security-engineer` lane (COND-G4-2), not cleared here.
- **No real on-chain transaction has ever flowed through this code.** Every test driving
  the live path uses a FAKE in-process async iterator of proto messages. The behaviour
  against the real Yellowstone wire (real proto field population, real ALT resolution on
  versioned txs, real reconnect under server-side stream drops) is UNVERIFIED until an
  operator points it at a live endpoint and the recorded corpus is inspected.
- **EDGE REMAINS UNPROVEN.** `GEYSER-live` is ingestion plumbing only. It is NOT an
  edge-vs-baseline / walk-forward / SimulationVenue cost gate. The headline acceptance
  metric (model-vs-naive-baseline net-of-cost delta, GATE-A / GATE-B) cannot move on this
  task. Edge stays **`UNPROVEN-NO-REAL-DATA`** until:
  1. the operator deploys a real Geyser endpoint + key,
  2. a real recorded corpus is collected via `--source=geyser`, and
  3. GATE-A (edge harness on recorded data) and GATE-B (min-sample model-vs-baseline)
     are re-run on that real corpus and PASS.
- **Real capital stays DRY-RUN-disabled + unreachable.** `shadow_record.py` is read-side
  only (never submits, never holds a keypair). The R3 pre-live checklist (Block A edge on
  recorded data · Block B custody/security · Block C CEO legal + funding + sign-off) is
  unchanged by this task and remains the gate before `DRY_RUN_ENABLED=false`.

---

## 4. Open items (non-blocking, honestly disclosed)

- `transport.py:332-334` — the grpcio-absent `ImportError` branch is uncovered; needs
  import-time isolation. MINOR; does not touch the event/parse path.
- `EnhancedWsFallback` (`transport.py:716-779`) retains its `PLUG_IN_HERE` labels
  correctly — it is a genuine, out-of-scope WS-fallback stub, not GEYSER-live code.

---

## 5. Files (absolute)

- `C:/dev/aats/aats/ingestion/transport.py` (live transport + parse + request builder)
- `C:/dev/aats/aats/ingestion/shadow_record.py` (--source=geyser entrypoint, env-only creds)
- `C:/dev/aats/tests/ingestion/test_geyser_transport.py` (42 tests, offline)
- `C:/dev/aats/aats/ingestion/geyser_proto/` (vendored Yellowstone stubs)
- `C:/dev/aats/aats/ingestion/decoders.py` (read for point-in-time verification)
- `C:/dev/aats/requirements/requirements.txt` (grpcio==1.64.1 pin)

## Verdict

**GEYSER-live: RECORDED — dual G3 PASS.** The real Yellowstone subscribe + parse is
implemented, point-in-time-honest, and offline-proven (42 tests, mutation-meaningful,
78% transport coverage). **LIVE ingestion is wired but UNVALIDATED:** Stage 2 still needs
the operator's Helius/Triton Geyser endpoint + token, and **edge remains
`UNPROVEN-NO-REAL-DATA`** until recorded real data is collected and GATE-A / GATE-B
re-run and pass.
