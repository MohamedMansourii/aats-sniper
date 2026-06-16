# memecoin-bot

An event-driven, multi-agent **research scaffold** for studying automated memecoin
discovery and scoring on Solana and Base. It ingests signals (Telegram calls,
on-chain events), runs them through safety + probability + decision agents, and
**paper-trades** the survivors while exposing everything on a live dashboard.

> **Read this first.** This is a learning and research tool, not a money printer.
> It ships in **dry-run** mode, and live order execution is deliberately left
> unimplemented (see [Limitations](#limitations--honest-disclaimers)). The default
> strategy this kind of bot chases — buying tokens off KOL "calls" and dumping a
> few minutes later — loses money for the overwhelming majority of people who try
> it. Treat this as a way to *understand* the pipeline, not as financial advice or
> a green light to risk funds.

---

## What it does

```
                 ┌──────────────┐
   Telegram ───► │              │
   On-chain ───► │   Scanner    │──► signals chan ──► worker pool
   (Twitter) ──► │  (ingestion) │                         │
                 └──────────────┘                         ▼
                                          ┌──────────────────────────────┐
                                          │  Safety  → honeypot / rug /   │
                                          │            liquidity / holders│
                                          │  KOL DB  → per-caller win rate │
                                          │  Prob.   → win_probability 0-100│
                                          │  Decision→ threshold + filters │
                                          └──────────────────────────────┘
                                                         │
                                       approved? ────────┤
                                                         ▼
                                          ┌──────────────────────────────┐
                                          │  Execution (PAPER by default) │
                                          │  Risk     → circuit breakers   │
                                          │  Reporting→ dashboard :8080    │
                                          └──────────────────────────────┘
```

- **Ingestion** — a scanner that can take Telegram messages / on-chain events and
  extract Solana contract addresses (base58 regex) and `$TICKERS`. A `-demo` mode
  emits synthetic signals so the whole pipeline is observable with zero credentials.
- **Safety agent** — pluggable `Provider` interface for sell-simulation (honeypot),
  dev-wallet rug history, liquidity lock, top-holder concentration, and mint/freeze
  authority. **Fails closed**: timeouts or errors count against the token.
- **KOL performance database ("ISAC")** — records each caller's calls and snapshots
  price/return at 2m/6m/10m/1h/4h/12h, computing per-KOL win rates over time.
- **Probability engine** — blends KOL win rate (sample-size weighted), market-cap
  sweet spot, safety score, volume, and holder count into a 0–100 score. A safety
  score below 70 hard-caps the probability at 20.
- **Decision gate** — approves only when probability ≥ threshold (default 80) and
  market cap / volume / holders fall in the configured bands.
- **Execution** — `PaperExecutor` simulates fills (with slippage + a random-walk
  PnL) so you can watch trades flow. `LiveExecutor` is a stub that refuses to run.
- **Risk manager** — daily-loss cap, consecutive-loss cooldown (pause for 1h), and
  a rolling slippage guard. Injectable clock for deterministic tests.
- **Dashboard** — vanilla-JS page on `:8080` polling `/api/metrics` and
  `/api/candidates`; control endpoints for start/stop behind basic auth.

---

## Requirements

- **Go 1.21+**
- (Optional) **Docker** for the bundled PostgreSQL + Adminer
- No API keys are needed to run in demo / dry-run mode.

---

## Quick start (dry-run, no credentials)

```bash
# 1. Resolve dependencies (writes go.sum; needs network the first time)
go mod tidy

# 2. Run the demo: synthetic signals exercise the full pipeline + paper trades
make run-demo
# ...equivalent to: go build -o bin/trading ./cmd/trading && ./bin/trading -config config.yaml -demo

# 3. Open the dashboard
#    http://localhost:8080
```

You should see tokens get found → filtered → scored → (some) paper-executed, with
the **dry-run banner** lit and PnL accumulating in simulation only.

Run the tests:

```bash
make test     # go test ./...
```

---

## Configuration

Two layers, by design:

| Layer        | File           | Holds                                              |
|--------------|----------------|----------------------------------------------------|
| Behaviour    | `config.yaml`  | thresholds, filters, slippage, intervals, ports    |
| **Secrets**  | `.env`         | private keys, RPC URLs, API tokens, DB password    |

Copy the example and fill in only what you need:

```bash
cp .env.example .env
```

`.env` is read at startup and **overrides** the matching values. **Never commit a
real `.env`** — the wallet keys in it can move funds.

Key `config.yaml` switches:

```yaml
bot:
  dry_run: true          # simulate everything; never submit real transactions
  auto_execute: false    # when false the bot only *recommends* candidates
  max_daily_loss_sol: 5.0
  consecutive_loss_limit: 3

filters:
  win_probability_threshold: 0.80
  min_market_cap_usd: 600000
  max_market_cap_usd: 30000000
```

### Optional PostgreSQL

The bot uses an **in-memory store** unless `database.host` (or `DB_HOST`) is set.
To enable persistence:

```bash
make docker-up         # postgres:16 on :5432, Adminer on :8081
# then set DB_HOST=localhost in .env (and matching DB_* vars)
```

Tables are created automatically via gorm `AutoMigrate` on startup. `make
docker-down` stops them.

---

## Dashboard & API

| Endpoint                     | Method | Purpose                              |
|------------------------------|--------|--------------------------------------|
| `/`                          | GET    | dashboard UI                         |
| `/api/health`                | GET    | liveness                             |
| `/api/status`                | GET    | mode / dry-run / paused              |
| `/api/metrics`               | GET    | counters, PnL, win rate, risk state  |
| `/api/candidates`            | GET    | recent scored tokens                 |
| `/api/control/start` `/stop` | POST   | pause/resume (HTTP basic auth)       |

Control endpoints require `auth_username` (config) + `DASHBOARD_PASSWORD` (env).
If `DASHBOARD_PASSWORD` is unset, control auth **fails closed** (no one can toggle).

---

## Project layout

```
cmd/trading           entrypoint, flags, wiring
internal/
  config              layered viper config (+ env secrets)
  models              shared domain types (no third-party imports)
  store               Store interface, MemoryStore + GormStore
  orchestrator        pipeline wiring + worker pool
  agents/
    scanner           ingestion (telegram/on-chain stubs + demo)
    safety            honeypot/rug/liquidity scoring
    analytics         KOL tracker + win-probability engine
    decision          approval gate
    execution         PaperExecutor (default) + LiveExecutor (stub)
    risk              circuit breakers
    reporting         state + dashboard server
pkg/
  jupiter             read-only Jupiter quote client
  telegram            address/ticker extraction
frontend              vanilla-JS dashboard
```

---

## Limitations & honest disclaimers

- **Live execution is intentionally a stub.** `LiveExecutor.Buy/Sell` returns
  `ErrLiveNotImplemented`. Building real, signed swaps that move funds is left to
  you on purpose — so this repo can't silently drain a wallet, and so you have to
  consciously implement (and own) that step.
- **No front-running / sandwiching.** The MEV relay hooks (Jito/BloXroute) are
  scoped for *transaction protection* only. This tool does not, and will not here,
  help you attack other traders' transactions.
- **Safety checks are interfaces with conservative stubs.** The real honeypot
  simulation, dev-wallet history, and liquidity-lock lookups need live RPC/API
  providers you must wire in. Until then, assume the safety score is a placeholder.
- **The strategy is structurally unprofitable for most.** Chasing KOL calls into
  low-cap memecoins and exiting on a fixed timer means competing against bots that
  are faster, callers who are often exit liquidity for insiders, and a token
  population where the large majority go to zero. Backtest skeptically; the demo's
  simulated PnL is *random*, not predictive.
- **Not audited, not "production-ready."** Treat it as a scaffold for learning the
  architecture, not as something to point at the mainnet with real SOL.

---

## Before you ever consider live trading

1. **Stay in dry-run.** Keep `dry_run: true` and `auto_execute: false` while you
   learn the pipeline. The bot will only *recommend* — it won't act.
2. **Test on testnet.** Point RPC endpoints at Solana **devnet** / Base **Sepolia**
   and exercise the full flow with worthless tokens before anything else.
3. **Implement and review execution yourself.** The live path is a stub for a
   reason; if you fill it in, read every line and understand the failure modes.
4. **Risk only what you can afford to lose entirely.** Size positions assuming the
   token goes to zero, because many will.
5. **None of this is financial advice.** You are solely responsible for any funds
   you choose to put at risk.
