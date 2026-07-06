# CODEX WORK-PACKAGE #2 — Wave 4: detection completeness (4 items)

> **How to use:** paste into a fresh Codex / high-reasoning GPT session, repo open at `C:\dev\aats`. Codex BUILDS;
> Claude then runs the dual-G3 review gate on the frozen tree (maker=Codex, checker=Claude — never the same agent
> grades itself). **Do NOT run git. Do NOT edit `aats/contracts/`. No secrets.** Do the items **one at a time**,
> smallest correct change each, and after each run `python -m pytest -p no:randomly -q` (baseline: **3142 passed /
> 0 failed** — keep it green) + `ruff check` on the files you touched.

## Project context (1 paragraph)
AATS is a paper-only quantitative-trading research system for Solana launches (pure SWE/DS/ML; real capital hard-
disabled). These items strengthen **detection & provenance** — raw on-chain data only, **no scoring, no trade
decisions, no win-rate**. Hard rules: **point-in-time correctness** (on-chain event-time only, slot ≤ decision
slot, never wall-clock, no lookahead; a decoder that can't determine on-chain time holds the event PENDING/None,
never substitutes), integer/Decimal money, refuse-by-default on undecodable input.

## Items (build in this order)

### E-M1-02 — fix `deploy_template_fingerprint` (template-invariant, not per-mint)
File: `aats/ingestion/decoders.py`. Today the fingerprint = `sha256(creator_wallet + mint[:8])`, which changes
every launch even for a repeat rug-factory creator, so it can't express "same template, new wallet." **Fix:**
decode the pump.fun/PumpSwap create's inner mplTokenMetadata URI (referenced by account index but not parsed
today) and fingerprint on the URI **domain + path-shape** (not the full URI), stable across a template reused by
different wallets. Same `str | None` field — no contract change. **Test:** two different mints from the same
creator+template → SAME fingerprint; different templates differ; malformed metadata → None.

### E-M1-05 — real captured-mainnet-signature decoder fixtures
File: `tests/ingestion/` fixtures + tests. Replace synthetic hand-built fixtures (fabricated signatures) with at
least ONE **real captured mainnet transaction per venue** (pump.fun create/buy/sell/migrate, Raydium v4
initialize2, CPMM init+swap, PumpSwap create_pool/swap): fetch via `getTransaction` (needs `RPC_PRIMARY` in
`.env`), commit the raw tx as a fixture, and assert the decoder produces the exact typed `LaunchEvent` on REAL
bytes. If an endpoint is unavailable, capture what you can and clearly mark the rest pending — **do not fabricate.**

### E-M1-06 — raw dev-wallet history + funding-lineage ingestion (feeds the existing risk gates)
New producer. Ingest, per `creator_wallet`: (a) prior LaunchEvents from that wallet already on our own bus (cheap,
no new dependency); (b) the wallet's first N inbound SOL-funding transfers (`getSignaturesForAddress` +
`getTransaction`) to surface the funding-source address, staleness-tagged. **Stop at raw data** — publish the
tuple `{creator_wallet, prior_launch_count, funding_source_wallet, funding_source_seen_before: bool}`. **NO
scoring** (that's the existing risk gates). Point-in-time: only funding with slot ≤ deploy event-time. Off the
FAST hot path (enrichment tier). **Test:** tuple produced point-in-time; future funding excluded (leak test);
undecodable → refuse/None; no scoring/win-rate field.

### E-M1-07 — remove/guard the dead `shredstream_endpoint` param (fail loud)
File: `aats/ingestion/transport.py`. `GeyserTransport` accepts a `shredstream_endpoint` that is stored and NEVER
read — a silent no-op. **Fix:** make it a no-op-safe stub that raises `NotImplementedError` with a clear message
when `INFRA_TIER=colo_shred` is set but no client exists, instead of silently ignoring a configured endpoint.
**Test:** a configured shredstream endpoint no longer silently no-ops (raises or is explicitly logged as
unimplemented).

## NOT in this package (leave for Claude)
- **E-M1-01** (live-validate Geyser multi-venue) — needs a real paid Geyser gRPC endpoint (owner-provided).
- **E-M1-03** (ADR: `bundler_cluster_id` populate-vs-deprecate) — architectural judgment, stays on Claude.

## Acceptance (Claude's dual-G3 gate checks all)
Per item: bug/feature done with smallest change; **mutation-meaningful tests** (RED before / GREEN after) covering
the stated cases; **strict point-in-time** (no wall-clock, future data provably excluded); **raw-data-only** (no
scoring / no win-rate); refuse-by-default; full suite stays green (3142); ruff clean; **no git, no contract edits,
no secrets**. Hand back: changed files + exact commands & output + a 3-line summary per item.
