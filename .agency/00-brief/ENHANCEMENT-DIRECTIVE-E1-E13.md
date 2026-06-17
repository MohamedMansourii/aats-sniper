# AATS MASTER ENHANCEMENT DIRECTIVE — E1–E13 + audits (AUDIT-FIRST, ADDITIVE)

**From:** CEO (this session) · **Logged by:** Agency Runtime · **Status:** ACTIVE, runs AFTER P3
build waves land (so it does not collide with in-flight file edits). This is ONE additive program,
NOT a rebuild. Every dispatched agent reads this cold alongside `BUILD-DIRECTIVE-v3.md`.

**Reference:** `./memecoin-bot` (a Go bot) is for IDEAS ONLY — never integrate its Go code; never
adopt its CEX/Base/80%-win-rate framing (conflicts with our locked Solana-native, GO-PAPER-ONLY thesis).

## SHARED RULES (apply to EVERY item)
- **AUDIT BEFORE BUILD:** for each item, the owning agent first checks whether it already exists in
  `aats/`. Present + adequate → mark **COVERED** with the file path, do nothing. Missing/weak → build.
- Every build task is **dual-G3** (code-reviewer + backtest-qa-engineer) and honors every HARD RULE
  in `BUILD-DIRECTIVE-v3.md`. Update `TASKBOARD.md` as you go.
- **HONESTY:** no win-rate target/guarantee/"100x/moonshot-predictor" framing. Models emit calibrated
  probability + uncertainty; report net-of-cost edge + model-vs-baseline. A *descriptive* realized
  win-% on the metrics page is fine; a target/promise is not.
- **ADVERSARIAL-BY-DEFAULT:** coordinated / low-account-age / high-synchronicity hype, alpha-caller
  signals, social/news all LOWER conviction by default; none is ever a standalone buy trigger.
- Social / news / discovery / caller signals are **SLOW-LOOP ONLY** — never the FAST/snipe hot path.
- **POINT-IN-TIME** event-time on every record; no compute-time leakage. Secrets via `.env.example` only.
- After every change the safety primitives (circuit breaker → survivable stop → dead-man switch) must
  still fire; mainnet-live stays hard-gated behind PROVEN edge on recorded data.

## WAVE 1 — Execution & control-plane hardening (E1 + E4 are TOP PRIORITY)
- **E1 — Devnet live-send validation mode (HIGHEST VALUE).** Add a DEVNET execution target that
  actually SUBMITS + confirms on Solana devnet (worthless SOL), exercising the real
  submit→land→confirm→reconcile path behind `SOLANA_CLUSTER=devnet|mainnet`. Owner:
  `solana-execution-engineer` + `latency-devops-engineer` (RPC); `backtest-qa-engineer` proves a full
  devnet round-trip AND that the safety primitives still fire.
- **E4 — Control-plane API auth + exposure hardening (VERIFY then fix).** Destructive endpoints
  (`/kill /flatten /mode /breaker-reset`) require an auth token and bind to localhost (never 0.0.0.0);
  nginx reverse-proxy + TLS recipe + IP allowlist for remote dashboard; confirm Telegram authz.
  de-risk-only stays de-risk-only. Owner: `crypto-security-engineer` + `latency-devops-engineer`.
- **E5 — Always-on operational hardening.** systemd unit (alt to docker-compose), log rotation,
  Redis/state snapshot backup + documented restore, startup self-check. Folds into G5 deploy artifacts.
  Owner: `latency-devops-engineer` + `docs-delivery`.

## WAVE 2 — Safety, risk & screener filters (+ audits)
- **E2 — Creator/token blacklist + whitelist pre-filter.** Fast, file-backed, hot-reloadable denylist
  (scam creator wallets + token mints) + trusted allowlist, applied BEFORE the sub-10ms gate, surfaced
  in reject reasons. Owner: `risk-guardrails-engineer` + `data-ingestion-engineer`.
- **E8 — Tunable discovery/screener filter layer** for the late-entry/survivor niche: market-cap band
  (~$70K–$11M), min liquidity, 24h-volume-to-mcap ratio (≥5–10%), volume-spike trigger — slow loop,
  surfaced as candidate reasons. Audit `ingestion/enrichment.py` + `pretrade_gate.py` first. Owner:
  `data-ingestion-engineer` + `feature-quant-engineer` + `risk-guardrails-engineer`.
- **E13 — Anti-FOMO / already-pumped exclusion filter.** Exclude/down-weight tokens that already ran
  (e.g. recent >300% pump); mainstream/CEX-listing/Forbes-tier mentions are an EXCLUSION signal, not a
  buy signal. Surface as a candidate reject reason. Owner: `risk-guardrails-engineer` + `feature-quant-engineer`.
- **E12 — Time-stop / stale-narrative exit (AUDIT first).** If a position is flat N configurable hours
  AND narrative score cooled → exit (de-risk only). Add to `exit_engine.py` only if missing. Owner:
  `risk-guardrails-engineer`.
- **AUDIT/VERIFY (add only if missing):**
  - Trailing-stop RATCHET in `exit_engine.py`: breakeven at 2x, lock 2x at 3x, rungs below — confirm a
    ratchet, not a fixed −20%.
  - Early-entry micro-preset: verified + LP locked ≥30d + honeypot pass + top-5 holders <20% + 0.5%
    sizing + (15–20% stop OR 24h time-exit) — components exist; wire as a named preset.
  - Liquidity sanity in `pretrade_gate`/`cost_model`: require 24h volume ≥ ~10x intended position + a
    pre-trade slippage simulation within threshold before entry.
  - Risk tiers: a soft ~2% daily-loss halt tier below the hard breaker; a minimum-sample guard before
    GATE-B acts on a model-vs-baseline change.

## WAVE 3 — Narrative & social intelligence (slow-loop, adversarial-by-default)
- **E6 — Discord ingestion adapter.** Add `DiscordAdapter` to `aats/sentiment/adapters.py` following
  the existing pattern; extend the source Literal in `aats/sentiment/models.py` to include `"discord"`.
  Ingest from an ALLOWLIST of servers/channels via a PROPER Discord bot token (NOT a self-bot — ToS).
  `event_time_ms` from message timestamp; feed Tier-A CryptoBERT → MCS. Owner: `nlp-sentiment-engineer`
  + `data-ingestion-engineer`.
- **E7 — News / breaking-news layer.** Add a `NewsAdapter` producing a separate news-narrative signal:
  crypto-native (CoinDesk/The Block/Cointelegraph via RSS/news API) + mainstream incl. Forbes. Wire as
  (a) MCS/narrative contributor and (b) a narrative-FAILURE input — a credible negative event can
  trigger the LLM catastrophic-exit (DE-RISK ONLY). New "Narrative & News" dashboard page. Owner:
  `nlp-sentiment-engineer` + `data-ingestion-engineer` + `llm-reasoning-engineer` (exit wiring) +
  `frontend-engineer`. NOTE: X/Reddit/Telegram adapters already exist — wire real endpoints/keys, do
  not rebuild; only Discord + News are net-new.
- **E9 — Alpha-caller / call-channel track-record scoring (honest).** Track named callers; score each
  by HISTORICAL accuracy on recorded on-chain outcomes (leak-free, backtest-qa-validated); use as a
  WEIGHTED selectivity signal — a filter/confidence input, NEVER a buy trigger. Most callers score
  near-zero/negative; surface honestly. Owner: `nlp-sentiment-engineer` + `ml-prediction-engineer` +
  `backtest-qa-engineer`.
- **E10 — Social-velocity + bot-ratio features.** Engagement velocity, organic-vs-bot growth ratio,
  unique-holder growth rate → MCS. Coordinated/bot-driven growth LOWERS conviction. Owner:
  `nlp-sentiment-engineer` + `feature-quant-engineer`.

## WAVE 4 — Operator UI & observability
- **E3 — Candidate / watchlist queue + dashboard view.** Read-only pipeline of tokens
  evaluated-but-not-sniped (status monitoring/pending/skipped/sniped, model p, safety report, reason);
  `GET /candidates` + a new dashboard page. No new control actions. Owner:
  `agent-orchestration-engineer` (API) + `frontend-engineer` (page).
- **E11 — Wallet-cluster ("Bubble Maps") visualization.** Render the EXISTING sniper-cluster/bundle
  detection as a wallet-connection graph (read-only, no new actions). Owner: `frontend-engineer` +
  `agent-orchestration-engineer` (API).

## DO NOT ADD (binding)
- Base/EVM multi-chain (dilutes the Solana-native latency/MEV edge — locked).
- OKX Wallet SDK (CEX wallet; we use the isolated Phantom/`aats-signer`).
- Any win-rate target/guarantee or "100x/moonshot predictor" framing.
- Caller-as-buy-trigger or any paid-signal-group dependency.
- Kubernetes (declined for a solo bot — ADR-0011).

## EXECUTION
4 sequenced waves (Wave 1 first; E1 + E4 top priority). Within a wave, parallelize disjoint-module
items, serialize same-file items. Per item: **audit → build-if-needed → dual-G3 → TASKBOARD update.**
At program end: a one-line **COVERED / ADDED** verdict per item (E1–E13 + each audit item) with paths.

**ORDERING (CEO reorder, supersedes the original placement):** this program runs **AS THE FINAL STEP,
AFTER G6** — the core build (stabilization → G4 integration/edge-proof → G5 release → G6 acceptance)
completes FIRST. E4 (auth) and E5 (ops) are still BUILT here and dual-G3 gated; because they land
after the G4 security audit (T-403) and the G5 deploy, the enhancement program ends with a final
security + deploy + consolidated-suite re-verification that confirms E4/E5 coverage and an updated
delivery package.
