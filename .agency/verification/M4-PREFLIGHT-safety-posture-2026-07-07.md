# M4 pre-flight — safety-posture wiring gap (precise, for Session A) · 2026-07-07

**M4 (live E2E) is OPEN and blocked on wiring.** This pins down *exactly* what is inert in the running controller
so A can wire it and M4 can then exercise the full posture. Source: `aats/controller/__main__.py`.

## What is LIVE in paper today (safe-by-design)
- **Circuit breaker hard-trip** (event-time day-key) · **Dead-man's switch** (separate fail-closed domain) ·
  **in-process ExitEngine hard stop** — all always-on (verified GREEN in M3).
- **E17 delayed-honeypot sellability re-probe** — LIVE: `sellability_reprober = SellabilityReprober(...)` (`:475`)
  driven by `SlowLoopEnrichmentWiring` (`:477`) via `slow_enrichment_task` (`:715`).

## What is `None` / inert (by-design in paper — MUST be wired + proven before real capital)
| Producer | Line | State | Meaning |
|---|---|---|---|
| `gate` (sub-10ms pre-trade safety gate) | `:436` | `None` "no pre-trade gate in paper mode" | sellability/LP-lock/mint-renounce/holder/tax entry gate **inactive** |
| `reasoner` (LLM de-risk / veto / force-exit) | `:426` | `None` "no LLM in paper mode" | narrative-failure / veto de-risk path **inactive** (SLOW-loop only by charter) |
| `sizing` (fractional-Kelly + exposure caps) | `:437` | `None` "fallback min-size" | Kelly sizer **inactive**; falls back to a fixed min size |
| `cost_model` | `:438` | `None` "fallback cost stack" | cost-aware entry gate uses a fallback |
| `lp_unlock_schedule_source` (E19 LP-unlock exit feed) | `:482` | `None` "TODO(M1): no LP-locker decoder yet" | E19 LP-unlock exit **never fires** (no decoder — A's ingestion lane) |
| `latency_budget_ms` | `:441` | `None` | no wall-clock SLA enforced in paper |
| `venue_state` | `:450` | `None` (NullVenueStateProvider) | sim venue state |
| `bundler_cluster_id` / `deploy_template_fingerprint` | `:177-178` | `None` | detection features unfed (A's detection completeness) |

## The honest reading
This is **not a paper-mode bug** — in paper the reduced posture is deliberate and safe (breaker + DMS + ExitEngine +
E17 hold the floor). It **is** a hard go-live prerequisite: for any real-capital-grade M4 E2E, Session A must wire
`gate`, `sizing`, `cost_model`, the reasoner-veto path, and the E19 `lp_unlock_schedule_source` (needs the LP-locker
decoder), then M4 must **exercise** them end-to-end (snipe→fast→slow handoff, survivable-stop under process-kill,
latency SLAs) — none of which can run while these are `None`.

## Ownership (edge-first)
- **Session A:** wire the producers above (with the E19 decoder in the ingestion lane) — a **Priority-2 / go-live**
  task, gated on a reaction-edge GO per the sequencing law; not needed for the paper platform.
- **Session V:** runs the M4 E2E verification the moment A signals the posture is wired.
- Until then M4 stays OPEN and honestly documented; it does **not** block the paper platform and is **downstream of
  the reaction edge verdict** (if NO-GO, M4 is never needed).
