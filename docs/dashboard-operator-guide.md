# AATS — Dashboard Operator Guide

**What the dashboard is:** a pro trading-terminal command deck for driving and watching the
AATS sniper. **Every action it can take is de-risk-only** — it can stop, flatten, pause, and
tighten, but there is *no* button, field, or flow that can increase risk. This is structural
(the frozen control-plane contract rejects any risk-increase at the API layer with HTTP 4xx),
not a matter of UI discipline.

URL: **http://localhost:3000**. With the default `VITE_USE_MOCK=true` it renders a full,
realistic telemetry stream with no backend — the fastest way to learn every page. To drive a
real (paper) bot, set `VITE_USE_MOCK=false` and `VITE_CONTROL_PLANE_URL=http://localhost:8787`.

> **No win-rate, anywhere.** The dashboard never shows a win-rate. The headline performance
> numbers are **net-of-cost PnL** and the **model-vs-baseline** delta. If you are looking for a
> win-rate, the absence is deliberate (see the honest framing in the README).

---

## 1. The pages

| Route | Page | What it shows | What you do here |
|---|---|---|---|
| `/` | **Command Deck** | Mode, loop states (snipe/fast/slow), connection (Geyser/ShredStream + internal ms), wallet (pubkey + balance + cap), kill flag, breaker state. The at-a-glance health of the whole bot. | Confirm mode = `SHADOW`/`PAPER` and `dry_run_enabled=true`; spot a tripped breaker or a killed bot fast. |
| `/feed` | **Snipe Feed** | Live stream of launch events: detection transport, gate verdict + reasons, **red flags** (token-safety scanner), model probability + uncertainty, action (sniped/skipped/vetoed), veto source, slot delay, tip-contention bucket, the cost-gate (expected edge vs total cost). | Watch what the bot is deciding and *why* it skips. The skips are the product. |
| `/latency` | **Latency** | Per-hop latency budget split into **internal compute** vs **submission** (block-engine RTT + leader-land), the slot floor, and the posture line **`DETECTION-COMPETITIVE, SUBMISSION-DISADVANTAGED`**. Infra tiers. | Sanity-check execution. The posture line is the honest statement that you do not win the landing race. |
| `/positions` | **Positions** | Open + closed positions. **Net-of-cost PnL is the primary number** (gross is secondary and labeled); TP ladder progress, trailing-armed, hard-stop %, exit mode (secure/fast), FSM state, per-position cost breakdown. Export. | Monitor exposure; flatten a single mint if needed. |
| `/copy-trade` | **Copy-Trade** | Smart-money / copy-trade signal as a **selectivity filter only** — `smart_wallets_in` is a count, shown with honest entry-lag. **It is never a buy/mirror trigger.** | Read the filter context. There is no "mirror this wallet" action — by design (EH-005 is expected-ZERO until proven). |
| `/sentiment` | **Sentiment** | Adversarial **MCS** scores per asset. Synchronicity, low account-age, red flags. **Higher coordinated shilling → LOWER conviction** (contrarian/risk signal). | Understand the narrative read. Manufactured hype lowers, never raises, conviction. |
| `/model` | **Model** | Calibrated classifier probability + baseline probability, the **calibration / reliability curve**, uncertainty band, feature importance. | Judge the model by *calibration*, not accuracy. A miscalibrated probability is a broken gate. |
| `/reasoning` | **Reasoning** | The de-risk LLM log: each entry shows `action_received`, `action_applied`, and `risk_increase_clamped`. Narrative-failure vetoes. | See where the LLM vetoed/exited. If it ever *requested* a risk increase, you'll see it was **clamped** to a no-op. |
| `/risk` | **Risk** | Editable risk config (**tighten-only**), the daily-loss gauge vs the floor, the circuit-breaker state + reset control. | Tighten caps; reset the breaker after manual review. Widening is rejected by the server. |
| `/monitoring` | **Monitoring** | Module health, per-module latency, staleness. A Geyser feed older than ~1.2s surfaces as degraded/STALE. | First stop when something looks wrong. |
| `/settings` | **Settings** | Operator settings; the **kill** and **go-live** controls (go-live is server-fenced). | Emergency kill; confirm-gated destructive actions. |

---

## 2. The de-risk controls (what the buttons actually do)

All of these map to the frozen control-plane contract (`.agency/02-architecture/api-contracts.md`).
The ones that change state are **confirm-gated** in the UI.

| Control | Endpoint | Effect | Confirm |
|---|---|---|---|
| **Kill** | `POST /api/kill` | Halt all new entries immediately; hand the open book to the exit engine + survivable stop. Flat in **≤ 2s**. Idempotent. | Modal confirm |
| **Flatten all** | `POST /api/flatten` | Flatten every open position via the exit engine. ≤ 2s. Idempotent. | Modal confirm |
| **Flatten one** | `POST /api/flatten/{mint}` | Flatten a single position; others untouched. | — |
| **Pause** | `POST /api/mode {PAPER\|SHADOW}` | Step mode **down** the ladder (toward SHADOW). Always allowed. | — |
| **Breaker reset** | `POST /api/breaker/reset` | Re-arm the daily-loss breaker **after manual review**. Only when `TRIPPED` (else `409`). No automated path and no LLM can call this. | yes |
| **Tighten risk** | `POST /api/risk-config` | Lower a cap / raise the snipe threshold. **Widening is rejected** `403 risk_increase_rejected`. | — |
| **Go LIVE** | `POST /api/mode {LIVE}` | **Server-fenced.** Requires `DRY_RUN_ENABLED=false` (config) AND the CEO auth token, else `403`. Unreachable in the paper build. | yes |

There is **no** size-up, stop-widen, leverage, or "advance capital" control. The dashboard
cannot make the bot riskier.

---

## 3. Reading the live state

- **Mode** (Command Deck): expect `SHADOW` on boot, `PAPER` when running the sim loop. `LIVE` is
  unreachable in this build. `dry_run_enabled` should read **true** at all times in paper.
- **Breaker** (Command Deck / Risk): `breaker_tripped` true means entries are halted and the book
  was flattened — investigate before resetting.
  > Known observability gap (carried, non-blocking for paper): the breaker persists to its own
  > store and is not yet projected into the state store, so `GET /api/state` can briefly show
  > `breaker_tripped=false` while the bot is genuinely halted (the in-process gate still blocks
  > entries). Cross-check the Risk page / the `CircuitBreakerTripped` Prometheus alert. Fix is
  > tracked as T-402-F1 before the live surface.
- **Connection** (Command Deck / Monitoring): Geyser green; ShredStream optional. Internal ms is
  the *compute* budget, not the landing time — see the Latency page for the honest split.
- **Positions** (Positions): the **net** PnL column is the one that matters. Gross is shown only
  for transparency.

---

## 4. What each alert/colour means

Colour always encodes meaning: **green up / red down / amber warn / blue info / violet model**.

- **Red flag chips** on the feed/positions = the token-safety scanner caught a known rug pattern
  (freeze authority, un-renounced mint, unlocked LP, sniper cluster, high sell-tax, low liquidity).
  A gated-out launch with red flags is the system working — rug avoidance is the edge.
- **Amber STALE** on Monitoring = a feed is older than its freshness budget; the bot degrades to
  safety-selective late entry and flags the tier.
- **Breaker / DMS banners** = a safety primitive fired. Treat as a page (see the kill-switch runbook).

---

## 5. Quick reference

- **See it working with zero setup:** `cd dashboard && npm run dev` → http://localhost:3000 (mock).
- **Wire to a paper bot:** `VITE_USE_MOCK=false`, `VITE_CONTROL_PLANE_URL=http://localhost:8787`,
  bring up `aats-controlplane`.
- **Emergency stop:** Kill (confirm) → flat in ≤2s. Then read `docs/kill-switch-runbook.md`.
- **Build green on mock (verified):** `VITE_USE_MOCK=true npm run build` → builds OK.
