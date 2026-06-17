# AATS — Operator Onboarding + Credentials Guide

**Audience:** the operator (you). Non-expert assumed. This guide tells you **exactly** what
accounts / API keys / wallets the AATS sniper needs, **where each one goes** (the precise `.env`
variable or Vault path), **how to obtain each**, and **at which rollout stage** — and, just as
importantly, what is **NOT needed yet**.

**Companion docs:** `docs/deploy-ops-guide.md` (how to run it), `docs/pre-live-checklist.md`
(the gate between paper and real money), `.agency/05-reports/security/custody-policy.md`
(the wallet/signer/Vault custody model, ADR-0009). The authoritative env schema is
`.env.example` — every variable name in this guide is copied verbatim from there.

> **Honest framing up front.** There is **no win-rate** anywhere in this system, by design. Real
> capital is **disabled by default** and stays disabled until the edge is **proven on recorded
> mainnet data** (it is **UNPROVEN** today — no recorded data exists yet) **and** the pre-live
> checklist clears in full **and** the CEO gives explicit legal + funding authorization. Social /
> news / caller signals are **de-risk only** — they can lower conviction, never raise it, never
> trigger a buy. Nothing in this guide changes any of that.

---

## 0. Clear up these confusions (read this first)

Three things confuse almost every new operator. Settle them before you create a single account.

### (a) There is no "pump.fun account" or "Jupiter login" to create. You pay an RPC provider — that's it.

The bot reaches Solana, pump.fun, Raydium, PumpSwap, and Jupiter the way every on-chain program
does: **over Solana RPC + Geyser (Yellowstone gRPC) subscriptions + direct on-chain program
reads.** pump.fun, Raydium, and Jupiter are **on-chain programs and public HTTP APIs** — there is
**no account to register, no login, no API key to create with "pump.fun" or "Jupiter."**

- The pump.fun / Raydium / PumpSwap venues are read and traded **directly on-chain** (the snipe BUY
  is direct-AMM, FR-028). No vendor relationship exists or is possible.
- Jupiter's quote API (`JUPITER_API_URL`, used for FAST-path **exits only**) is a **public endpoint
  with no key**.
- Jito's block engine (`JITO_BLOCK_ENGINE`) is a **public endpoint**; bundle auth is optional.
- The **only** thing you pay for is an **RPC + Geyser provider** (Helius / Triton One / QuickNode).
  That single relationship is what gives you fast on-chain detection. That's the whole picture.

So when this guide says "get the Geyser key," it means **one** thing: sign up with **one** RPC
provider and copy **their** key. There is no second, third, or fourth "exchange account" to make.

### (b) The bot never logs into Phantom. It uses its own isolated trade-only keypair.

Phantom is just a **keypair UI** — a convenient front-end over a Solana keypair. **The bot does not
log into the Phantom app, does not connect to your Phantom wallet, and never touches your main
holdings.** Instead:

- You create a **separate, dedicated, trade-only keypair** with the Solana CLI
  (`solana-keygen new`). This wallet holds **only** small working capital you are willing to lose
  (`WALLET_MAX_BALANCE_LAMPORTS` ≤ 2 SOL at R3). It is **"incinerable."**
- That keypair's **SECRET** lives in **Vault**, read **only** by the isolated `aats-signer`
  process at boot — **never** in any `.env` variable, never in code, never in a log (ADR-0009; the
  custody policy). There is, by design, **no** `WALLET_PRIVATE_KEY`/`KEYPAIR_JSON` variable in the
  schema at all.
- The bot's hot core holds only the **public key** (`WALLET_PUBKEY`) so it can build *unsigned*
  transactions. Even a full compromise of the bot cannot drain more than the per-trade float
  (≤ 0.1 SOL/tx, ≤ 0.5 SOL rolling), and it **never** reaches your Phantom main holdings — those
  are on a completely different keypair the bot has never seen.

You **may** import the trade-only keypair into a throwaway Phantom profile later just to *watch* it,
but that is optional and unrelated to how the bot signs.

### (c) Paper (what runs right now) needs nothing. Keys are added stage by stage.

Out of the box the system runs in **PAPER / SHADOW / DRY-RUN** mode and needs **zero** real
credentials. You only add keys as you climb the rollout ladder, and most keys stay **optional** even
several stages in. The single thing worth setting on day one is your **local Grafana password**.

**What you need RIGHT NOW: nothing. Keep running paper.** (Restated at the end.)

---

## 1. Master credentials table

Every credential the system can use, in one place. "First stage needed" is the earliest rung at
which the credential becomes relevant; until then you can leave the placeholder untouched.
Stages: **S0 PAPER** → **S1 DEVNET** → **S2 SHADOW/RECORD (mainnet)** → **S3 LIVE**.

| Credential | Exact env var / Vault path | First stage | Required? | Provider / how |
|---|---|---|---|---|
| Local Grafana login password | `GRAFANA_ADMIN_PASSWORD` (user `GRAFANA_ADMIN_USER`) | S0 PAPER | Optional (recommended) | You choose it. Local dashboard login only — not an external account. |
| Devnet cluster switch | `SOLANA_CLUSTER=devnet` | S1 DEVNET | Required for S1 | Config value, not a secret. |
| Devnet RPC URL | `RPC_DEVNET` | S1 DEVNET | Required for S1 | Public `https://api.devnet.solana.com` (no key) **or** premium Helius devnet (key). |
| Devnet wallet pubkey | `DEVNET_WALLET_PUBKEY` | S1 DEVNET | Required for S1 | `solana-keygen` (worthless devnet SOL; **not** the mainnet wallet). |
| Devnet confirm tuning | `DEVNET_CONFIRM_MAX_POLLS`, `DEVNET_CONFIRM_POLL_INTERVAL_S` | S1 DEVNET | Optional | Tuning ints; sane defaults shipped. |
| Mainnet primary RPC | `RPC_PRIMARY` | S2 SHADOW | Required for S2 | Helius / Triton One / QuickNode premium RPC (paid). |
| Mainnet failover RPC | `RPC_SECONDARY` | S2 SHADOW | Optional (recommended) | A second provider, or public `https://api.mainnet-beta.solana.com`. |
| Geyser gRPC endpoint | `GEYSER_ENDPOINT` | S2 SHADOW | Required for S2 | Yellowstone/Geyser gRPC plan from your RPC provider. |
| Geyser auth token | `GEYSER_TOKEN` | S2 SHADOW | Required for S2 | Same provider's gRPC token (the `x-token`). |
| ShredStream overlay endpoint | `SHREDSTREAM_ENDPOINT` | S2 SHADOW | Optional | Colo/ShredStream provider; empty disables. |
| ShredStream token | `SHREDSTREAM_TOKEN` | S2 SHADOW | Optional | Same provider; empty disables. |
| Infra tier label | `INFRA_TIER` | S2 SHADOW | Optional | `dedicated_geyser` (default) or `colo_shred`. |
| Birdeye enrichment | `BIRDEYE_API_KEY` | S2 SHADOW | Optional | birdeye.so API key (discovery enrichment, SLOW loop). |
| DEXScreener enrichment | `DEXSCREENER_API_KEY` | S2 SHADOW | Optional | dexscreener.com (often keyless; blank = disabled). |
| Social aggregator | `SOCIAL_API_KEY` | S2 SHADOW | Optional | LunarCrush (or similar) API key. |
| X (Twitter) search | `X_API_BEARER_TOKEN` | S2 SHADOW | Optional | X API v2 Bearer token (Basic tier+). Vault ref. |
| Reddit read-only | `REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET`, `REDDIT_USER_AGENT` | S2 SHADOW | Optional | Reddit app (script type). Vault ref. |
| Telegram channel monitor | `TELEGRAM_MTProto_API_ID`, `TELEGRAM_MTProto_API_HASH`, `TELEGRAM_MTProto_SESSION` | S2 SHADOW | Optional | my.telegram.org MTProto app. Vault ref. |
| Discord channel monitor | `DISCORD_BOT_TOKEN`, `DISCORD_CHANNEL_ALLOWLIST` | S2 SHADOW | Optional | Discord Developer Portal bot. |
| News feeds | `NEWS_RSS_*` (6 URLs), `NEWS_API_KEY` | S2 SHADOW | Optional | Public RSS URLs; optional newsapi.org key. |
| Live LLM (local) | `AATS_OLLAMA_URL` | S2 SHADOW | Optional | Your own Ollama server URL. Default = offline mock. |
| Live LLM (frontier) | `AATS_FRONTIER_API_KEY`, `OPENAI_API_KEY` | S2 SHADOW | Optional | Frontier LLM provider key. Vault ref. Default = offline mock. |
| **Trade-only wallet pubkey** | `WALLET_PUBKEY` | **S3 LIVE** | **Required for S3** | `solana-keygen` (separate, capped, incinerable — **NOT** Phantom). |
| **Trade-only wallet SECRET** | **Vault path** `WALLET_SECRET_VAULT_PATH` (default `secret/aats/trade-wallet`) | **S3 LIVE** | **Required for S3** | Stored in Vault; read only by `aats-signer`. **NEVER an env var.** |
| Wallet hard cap | `WALLET_MAX_BALANCE_LAMPORTS` | S3 LIVE | Required for S3 | Integer lamports; ≤ 2 SOL (`2000000000`) at R3. |
| Vault server URL | `VAULT_ADDR` | S3 LIVE | Required for S3 | Your HashiCorp Vault server. |
| Vault boot token | `VAULT_TOKEN` | S3 LIVE | Required for S3 | Short-lived token (AppRole preferred). Capability, not the key. |
| Risk floors | `PER_TRADE_CAP_LAMPORTS`, `MAX_AGGREGATE_LAMPORTS`, `DAILY_LOSS_LIMIT_SOL` | S3 LIVE | Required for S3 | Integer/float floors; tighten-only at funding. |
| CEO LIVE auth (mode) | `CEO_TOKEN` and `CEO_AUTH_TOKEN` | S3 LIVE | Required for S3 | Random secret; `openssl rand -hex 32`. Gate `POST /api/mode LIVE`. |
| Operator API bearer | `OPERATOR_TOKEN` and `OPERATOR_API_TOKEN` | S3 LIVE (control plane) | Required to drive de-risk | Random secret; `openssl rand -hex 32`. Vault ref. |
| Telegram **command** bot token | **Vault ref** `TELEGRAM_BOT_TOKEN_VAULT_REF` (default `secret/aats/telegram-bot-token`) | Any stage (operator surface) | Optional | @BotFather. **Vault-held, never an env var.** |
| Operator Telegram user ID(s) | `TELEGRAM_OPERATOR_USER_IDS` | Any stage | Required to use the command bot | @userinfobot gives your numeric ID. |
| Telegram **alert** bot token | `ALERTMANAGER_TELEGRAM_BOT_TOKEN` | Any stage | Optional | @BotFather (a **separate** bot from the command bot). |
| Telegram alert chat ID | `ALERTMANAGER_TELEGRAM_CHAT_ID` | Any stage | Optional | The chat/channel the alert bot posts to. |
| PagerDuty routing | `ALERTMANAGER_PAGERDUTY_KEY` | Any stage | Optional | PagerDuty integration key (blank disables). |
| Control-plane URL | `CONTROL_PLANE_URL`, `VITE_CONTROL_PLANE_URL` | Any stage | Default localhost | The control-plane base URL; default `http://localhost:8787`. |
| Dashboard live-wire | `VITE_USE_MOCK` | Any stage | Default `true` (mock) | Set `false` to wire the dashboard to a live control plane. |
| Multi-wallet activation | `N_WALLETS_MAX_ENABLED`, `N_WALLETS_MAX` | S3→R4 | Optional (off at R3) | Built+tested, **not activated** until R4. `1` at R3. |

> **Reading the Vault-path rows:** where the table says "Vault ref / Vault path," the `.env` value
> is a **pointer** (a path or reference), not the secret. The secret itself is stored in Vault and
> fetched at runtime. This is the whole point of the custody model: a leaked `.env` leaks no keys.

---

## 2. Stage 0 — PAPER (this runs right now). No keys required.

This is the default state of a fresh checkout. **Nothing external is needed.** Real capital is
unreachable (three independent gates plus an unfunded wallet).

**The safe defaults already in `.env.example`:**

```
DRY_RUN_ENABLED=true     # master safety flag — real capital DISABLED
AATS_MODE=SHADOW         # boot mode
AATS_ENV=sim             # simulation environment label
VITE_USE_MOCK=true       # dashboard runs standalone on mock telemetry
```

**Bring it up** (from `docs/deploy-ops-guide.md`, verified on this build):

```bash
git clone <repo-url> aats && cd aats
cp .env.example .env
docker compose up
#   dashboard   http://localhost:3000   (green on mock immediately)
#   control API http://localhost:8787/api/health
#   Grafana     http://localhost:3001   (admin / GRAFANA_ADMIN_PASSWORD)
```

**Run the offline ingestion demo** (proves the decode/record pipeline with synthetic data — **no
network, no keys**; the corpus is SYNTHETIC and carries no edge):

```bash
python -m aats.ingestion.shadow_record --source=replay --out /tmp/aats_shadow_demo --max-events 25
```

### The one credential worth setting at S0

| What it's for | Exact var | Provider / how | Required? |
|---|---|---|---|
| Local Grafana login | `GRAFANA_ADMIN_PASSWORD` (user `GRAFANA_ADMIN_USER`, default `admin`) | **You** pick it. This is the password for your **local** Grafana at `http://localhost:3001` — it is **not** an external account, no signup. Just put a value in `.env`. | Optional but recommended (otherwise you log in with the placeholder). |

Everything else stays as the shipped placeholder. **Do not** create any RPC, wallet, Vault, or
social account to run paper.

---

## 3. Stage 1 — DEVNET (real transaction path, worthless SOL)

**Purpose:** exercise the real submit → land → confirm → reconcile path against Solana **devnet** —
a *separate* cluster whose SOL has **no monetary value**. This is wiring shakeout (`AATS_ENV=devnet`,
rung E1), not edge validation. **No real capital is ever at risk here.**

### 3.1 Cluster switch — `SOLANA_CLUSTER`

| What it's for | Exact var | How | Required? |
|---|---|---|---|
| Point the venue at devnet | `SOLANA_CLUSTER=devnet` | Set the value. `mainnet` (default) keeps the normal DRY-RUN path; `devnet` enables `SubmitMode.DEVNET`. Not a secret. | Required for S1 |

### 3.2 Devnet RPC — `RPC_DEVNET`

| What it's for | Exact var | Provider / how | Required? |
|---|---|---|---|
| Devnet RPC endpoint | `RPC_DEVNET` | **Free, no key:** `https://api.devnet.solana.com` (rate-limited — fine for shakeout). **Premium (Helius devnet):** `https://devnet.helius-rpc.com/?api-key=<key>` (paste your Helius key in place of `<key>`). Blank = devnet submit is blocked (`DevnetSubmitBlocked`). | Required for S1 |

### 3.3 Devnet wallet — `DEVNET_WALLET_PUBKEY`

This is a **throwaway devnet keypair** holding worthless devnet SOL. **It is NOT the mainnet trade
wallet** — keep them separate. Exact commands (standard Solana CLI):

```bash
# 1. Create a devnet keypair file
solana-keygen new --outfile devnet-wallet.json

# 2. Print its public address
solana address -k devnet-wallet.json
#    -> paste this value into DEVNET_WALLET_PUBKEY in .env

# 3. Airdrop free, worthless devnet SOL to it
solana airdrop 2 <pubkey> --url devnet
```

| What it's for | Exact var | How | Required? |
|---|---|---|---|
| Devnet test wallet | `DEVNET_WALLET_PUBKEY` | The pubkey printed by `solana address -k devnet-wallet.json` above. Worthless devnet SOL only. | Required for S1 |

> Install the Solana CLI from the official docs if you don't have it. (These commands are the
> standard Solana CLI invocations; the CLI is not installed in the build environment, so they are
> documented, not executed here.)

### 3.4 Optional devnet tuning

| What it's for | Exact var | Default | Required? |
|---|---|---|---|
| Max confirmation polls | `DEVNET_CONFIRM_MAX_POLLS` | `30` | Optional |
| Poll interval (seconds) | `DEVNET_CONFIRM_POLL_INTERVAL_S` | `0.5` | Optional |

Devnet blocks land slower than mainnet; raise these if you see premature confirmation timeouts.

**Still no mainnet wallet, no Vault, no real capital at this stage.**

---

## 4. Stage 2 — SHADOW / RECORD on mainnet (collect REAL data, NO trading)

**Purpose:** run live mainnet ingestion in **SHADOW** mode to **record real launches** with
point-in-time features and event-time outcome labels. **`AATS_ENV=mainnet-shadow`,
`DRY_RUN_ENABLED=true` — the bot submits NOTHING.** This recorded corpus is the **prerequisite** for
ever proving the edge. This stage needs **one** real provider relationship (RPC + Geyser); all the
enrichment and sentiment keys remain **optional**.

### 4.1 The one thing you actually buy: a Solana RPC + Geyser provider

You need an account with **one** of these providers, on a plan that includes a **gRPC
Geyser/Yellowstone** stream (not just plain JSON-RPC):

| Provider | The product to buy | What you copy |
|---|---|---|
| **Helius** | **Geyser / "Atlas"** (Yellowstone gRPC) plan | The gRPC endpoint + your API key (the `x-token`). |
| **Triton One** | **"Yellowstone gRPC"** plan | The gRPC endpoint + access token. |
| **QuickNode** | **"Yellowstone"** gRPC add-on | The gRPC endpoint + token. |

**How to obtain (any of the three):** create an account on the provider's site → create/activate a
**mainnet** endpoint with the **Yellowstone gRPC / Geyser** product enabled → copy the **gRPC
endpoint host:port** and the **token** from the dashboard. (Plain HTTP RPC for `RPC_PRIMARY` comes
from the same account.)

Map them to these exact variables:

| What it's for | Exact var | Example shape | Required? |
|---|---|---|---|
| Primary mainnet RPC (HTTP) | `RPC_PRIMARY` | `https://mainnet.helius-rpc.com/?api-key=<key>` | Required for S2 |
| Failover RPC (HTTP) | `RPC_SECONDARY` | A second provider, or public `https://api.mainnet-beta.solana.com` | Optional (recommended) |
| Geyser gRPC endpoint | `GEYSER_ENDPOINT` | `https://atlas-mainnet.helius-rpc.com:2053` (Helius/Atlas shape) | Required for S2 |
| Geyser auth token | `GEYSER_TOKEN` | your provider's gRPC token (sent as `x-token`) | Required for S2 |
| ShredStream endpoint (overlay) | `SHREDSTREAM_ENDPOINT` | colo/ShredStream provider host; **empty disables** | Optional |
| ShredStream token | `SHREDSTREAM_TOKEN` | matching token; **empty disables** | Optional |
| Infra tier label | `INFRA_TIER` | `dedicated_geyser` (default) or `colo_shred` | Optional |

### 4.2 Wiring the live data path (`--source=geyser`)

**This is the one piece of real engineering S2 requires.** `GeyserTransport` in
`aats/ingestion/transport.py` ships as a **documented stub** — it logs a warning and yields nothing,
because the live gRPC endpoint is not reachable in the build environment. To turn `--source=geyser`
from a stub into real ingestion, an engineer must implement the two `PLUG_IN_HERE` markers:

**1. `_parse_geyser_tx(...)`** (`transport.py`, around line 247) — convert the Yellowstone
**protobuf** `SubscribeUpdateTransactionInfo` into the transport-agnostic `RawTransaction`. Extract,
per the in-code spec: `signature` (bytes → base58), `slot` (from the outer `SubscribeUpdate.slot`),
`block_time`, `fee_payer` (`message.account_keys[0]`), `instructions` (resolve account keys),
`inner_instructions` (`meta.inner_instructions`), `program_logs` (`meta.log_messages`), and `err`
(`meta.err`, `None` on success). It currently `raise NotImplementedError(... PLUG_IN_HERE ...)`.

**2. `GeyserTransport.subscribe(...)`** (`transport.py`, around line 196) — open the gRPC stream.
The exact shape is sketched in the docstring at the `PLUG_IN_HERE` marker:

- open `grpc.aio.secure_channel(self._endpoint, grpc.ssl_channel_credentials(), ...)`,
- build a Yellowstone `GeyserStub(channel)`,
- send a `SubscribeRequest` filtering `transactions` by `account_include=list(program_ids)`,
  `failed=False`, `vote=False`, `commitment=PROCESSED`, `from_slot=last_slot or None`,
- attach metadata `[("x-token", self._x_token)]` (this is `GEYSER_TOKEN`),
- `async for update in stub.Subscribe(...)`: on `update.HasField("transaction")`, call
  `_parse_geyser_tx(update.transaction)`, set `self._last_slot`, and `yield` the `RawTransaction`.

The credentials are **never** hardcoded — `GeyserTransport.__init__` already reads `endpoint` from
`GEYSER_ENDPOINT` and `x_token` from `GEYSER_TOKEN` (the `shadow_record` runner wires this for you).

**Once wired, record real data:**

```bash
SOLANA_CLUSTER=mainnet GEYSER_ENDPOINT=<grpc-endpoint> GEYSER_TOKEN=<token> \
  python -m aats.ingestion.shadow_record --source=geyser --out /data/aats_shadow --max-events 5000
```

This submits **nothing** (the module is read-side only — never holds a keypair, never touches the
OMS). It writes a recorded corpus to `/data/aats_shadow/snapshots.jsonl`. Reaching **≥ ~3,000
recorded mainnet launches** is the R1 milestone in `docs/pre-live-checklist.md` (Block A).

### 4.3 Enrichment + sentiment + LLM keys — ALL optional, even here

The system runs **fine** with **no** enrichment, **no** social feeds, and the **offline mock LLM**.
These keys only enable *live* sentiment/news/enrichment, which is **de-risk / selectivity only**
(SLOW loop, never on the snipe hot path, never a buy trigger). Add any subset you want, or none.

**Discovery / enrichment (optional):**

| What it's for | Exact var | Provider / how |
|---|---|---|
| Birdeye token enrichment | `BIRDEYE_API_KEY` | Sign up at birdeye.so → API key. |
| DEXScreener enrichment | `DEXSCREENER_API_KEY` | dexscreener.com (often keyless; leave blank to disable). |
| Social aggregator | `SOCIAL_API_KEY` | LunarCrush (or similar) → API key. |

**Sentiment sources (all optional; SLOW loop, de-risk only):**

| What it's for | Exact var(s) | Provider / how |
|---|---|---|
| X (Twitter) recent search | `X_API_BEARER_TOKEN` | X Developer Portal → project/app → **Bearer token** (Basic tier minimum). Vault ref. |
| Reddit read-only search | `REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET`, `REDDIT_USER_AGENT` | reddit.com/prefs/apps → create a **script** app → copy client id + secret. |
| Telegram channel monitor | `TELEGRAM_MTProto_API_ID`, `TELEGRAM_MTProto_API_HASH`, `TELEGRAM_MTProto_SESSION` | my.telegram.org → API development tools → create app → copy `api_id` + `api_hash`; generate a telethon session string. Vault ref. |
| Discord channel monitor | `DISCORD_BOT_TOKEN`, `DISCORD_CHANNEL_ALLOWLIST` | Discord Developer Portal (see §6.2). Read-only. |
| News (RSS) | `NEWS_RSS_COINDESK_URL`, `NEWS_RSS_THEBLOCK_URL`, `NEWS_RSS_COINTELEGRAPH_URL`, `NEWS_RSS_FORBES_URL`, `NEWS_RSS_BLOOMBERG_URL`, `NEWS_RSS_REUTERS_URL` | Paste each outlet's public RSS feed URL. Leave as placeholder = no feeds (the adapter safely returns `[]`). |
| News (supplemental API) | `NEWS_API_KEY` | newsapi.org → free plan (100 req/day). Leave blank to disable NewsAPI. |

**LLM reasoner (optional — default is the deterministic offline mock, zero network):**

| What it's for | Exact var | Provider / how |
|---|---|---|
| Local Ollama backend | `AATS_OLLAMA_URL` | Your own Ollama server, e.g. `http://ollama:11434/v1`. Runtime-injected. |
| Frontier escalation | `AATS_FRONTIER_API_KEY` | Frontier LLM provider key (ambiguous/high-stakes cases only). Vault ref. |
| OpenAI key (if used) | `OPENAI_API_KEY` | OpenAI key. Vault ref / runtime-injected. |

> The Reasoner adjudicates quant-prob vs sentiment and emits a **de-risk** verdict; positive/neutral
> sentiment and "mainstream adoption" headlines **never** raise conviction (a Forbes/Bloomberg
> headline is a *sell-into-retail* signal here). All of the above is SLOW-loop only.

**Still no trade wallet, no Vault, no real capital at S2.**

---

## 5. Stage 3 — LIVE (real capital). Only after the edge is proven + gates clear.

**Do not reach this stage casually.** LIVE is reachable **only** after **all three** blocks of
`docs/pre-live-checklist.md` are green:

- **A** — edge **proven on recorded mainnet data** (GATE-A + GATE-B pass, lower-95% bound > 0). This
  is **NOT MET** today (no recorded data). The edge is **UNPROVEN**.
- **B** — custody & security hardened (**COND-G4-2**): the Rust signer's three refusals built +
  test-proven, real image digests, container lockdown, hash-locked deps. Several items are **scaffold
  / latent** today.
- **C** — CEO **legal** confirmation (OQ-009) + **funding** authorization + explicit **R3 sign-off**.

Only then do you set `DRY_RUN_ENABLED=false` **explicitly** and `AATS_ENV=mainnet-live`.

### 5.1 The trade-only wallet — do NOT connect Phantom

**Create a SEPARATE, dedicated, trade-only keypair.** It must **never** be your main holdings and
**never** a wallet that has touched cold storage directly.

```bash
# Create a brand-new trade-only keypair (mainnet)
solana-keygen new --outfile trade-wallet.json

# Print its public address -> this goes in WALLET_PUBKEY
solana address -k trade-wallet.json
```

Then:

1. **Fund it small, out-of-band, from cold storage** in tiny tranches — at R3 the wallet hard cap is
   **≤ 2 SOL** (`WALLET_MAX_BALANCE_LAMPORTS=2000000000`). Cold storage **never** signs through this
   system; its key **never** enters this host. The bound on total loss is the float on this wallet,
   not your net worth.
2. **Put the SECRET in Vault**, at the path `WALLET_SECRET_VAULT_PATH` (default
   `secret/aats/trade-wallet`). The `aats-signer` process reads it at boot via a short-lived Vault
   token, holds it in `mlock`-able memory, and zeroizes it on exit. **The secret is NEVER an env var**
   — there is, by design, no `WALLET_PRIVATE_KEY`/`KEYPAIR_JSON` variable in the schema (ADR-0009).
3. The bot holds only `WALLET_PUBKEY`. **Your Phantom main holdings are never touched** — they live
   on a different keypair the bot has never seen.

| What it's for | Exact var / path | How | Required? |
|---|---|---|---|
| Trade wallet pubkey | `WALLET_PUBKEY` | `solana address -k trade-wallet.json` output. | Required for S3 |
| Trade wallet SECRET | **Vault** `WALLET_SECRET_VAULT_PATH` (default `secret/aats/trade-wallet`) | Stored in Vault; read only by `aats-signer`. **NEVER an env var.** | Required for S3 |
| Wallet hard cap | `WALLET_MAX_BALANCE_LAMPORTS` | `2000000000` (= 2 SOL) ceiling at R3. Integer lamports. | Required for S3 |

### 5.2 Vault setup — `VAULT_ADDR` / `VAULT_TOKEN`

Vault is the **only** source of key material. The signer fetches the wallet secret from Vault at boot
using a **short-lived token** (a *capability* with a short TTL — **not** the key itself).

| What it's for | Exact var | How | Required? |
|---|---|---|---|
| Vault server URL | `VAULT_ADDR` | Your HashiCorp Vault server, e.g. `https://vault.example.com`. | Required for S3 |
| Vault boot token | `VAULT_TOKEN` | A **short-lived** token. **Strongly prefer AppRole** (or Kubernetes-auth): the signer logs in with a role-id + secret-id and Vault issues a short-TTL token, so even this input is not a long-lived static secret. The signer exchanges the token for the wallet secret, then it is useless. | Required for S3 |

**The AppRole / short-lived-token model in one line:** the host never stores the wallet key; it
stores a *way to ask Vault for it once, briefly*. Rotate the role credentials on any suspicion; the
key-compromise runbook (custody policy §8) is "rotate the wallet," not "delete the commit."

### 5.3 Risk floors (the signer enforces these independently)

Set these tighter (never looser) at funding time. The signer re-validates every transaction against
them at the signing boundary, independent of any upstream gate — a compromised hot core **cannot**
sweep the float.

| What it's for | Exact var | Default | Required? |
|---|---|---|---|
| Per-tx SOL spend cap | `PER_TRADE_CAP_LAMPORTS` | `100000000` (0.1 SOL) | Required for S3 |
| Rolling-window aggregate cap | `MAX_AGGREGATE_LAMPORTS` | `500000000` (0.5 SOL) | Required for S3 |
| Daily-loss circuit-breaker floor | `DAILY_LOSS_LIMIT_SOL` | `-0.30` (absolute SOL) | Required for S3 |

### 5.4 Authorization tokens — generate strong secrets

Two distinct concerns, two pairs of variables. The control plane carries the destructive de-risk
surface, so both pairs gate it. Generate each as a strong random secret:

```bash
openssl rand -hex 32      # -> a 64-char hex token (run once per token)
```

| What it's for | Exact var(s) | Where it goes | Required? |
|---|---|---|---|
| CEO authorization for `POST /api/mode LIVE` (the `X-CEO-Auth` header, AC-060) | `CEO_TOKEN` **and** `CEO_AUTH_TOKEN` | The control plane requires the CEO token to advance mode to LIVE (in addition to `DRY_RUN_ENABLED=false`). Set both to the same generated secret at R3 only. | Required for S3 |
| Operator bearer token on every destructive POST (`/api/kill`, `/api/flatten`, `/api/mode`, `/api/breaker/reset`) | `OPERATOR_TOKEN` **and** `OPERATOR_API_TOKEN` | The control plane reads `OPERATOR_TOKEN` (`server.py:_check_operator_auth`); the Telegram command bot presents `OPERATOR_API_TOKEN`. Use the **same** generated secret for both; store as a **Vault ref**, never the raw token. | Required to drive de-risk actions |

> `CEO_TOKEN`/`CEO_AUTH_TOKEN` only **advance** mode toward LIVE; `OPERATOR_TOKEN`/`OPERATOR_API_TOKEN`
> only authorize **de-risk** (kill/flatten/pause). There is no risk-*increasing* command on any
> operator surface — widening a limit requires a config change + redeploy, never the API.

### 5.5 Jito + Jupiter — public, no account needed

| What it's for | Exact var | Note |
|---|---|---|
| Jito block engine | `JITO_BLOCK_ENGINE` | **Public** endpoint (default `https://frankfurt.mainnet.block-engine.jito.wtf`). Bundle auth is optional; no signup required to submit. |
| Jupiter v6 quote API (FAST-path exits) | `JUPITER_API_URL` | **Public** endpoint (default `https://quote-api.jup.ag/v6`). **No key.** The snipe BUY is direct-AMM, not Jupiter. |

### 5.6 Multi-wallet — off at R3, on only at R4

| What it's for | Exact var | R3 value | Note |
|---|---|---|---|
| Multi-wallet activation flag | `N_WALLETS_MAX_ENABLED` | `false` | Built + tested but **NOT activated** until R4. Must be explicitly `true` to allow N > 1. |
| Number of signing wallet slots | `N_WALLETS_MAX` | `1` | Must be `1` at R3. Multi-wallet only after net-of-cost PnL + model-vs-baseline proven at R4. |

### 5.7 The COND-G4-2 signer hardening that MUST clear first

These are **hard blockers** before `DRY_RUN_ENABLED=false`. They are latent today only because LIVE
is unreachable. Source of truth: `docs/pre-live-checklist.md` Block B + the G4 security audit.

- **Spend-cap refusal** built + **test-proven** (per-tx `PER_TRADE_CAP_LAMPORTS`, rolling
  `MAX_AGGREGATE_LAMPORTS`, velocity cap) — integer-lamport math.
- **Program-ID allowlist refusal** built + test-proven (refuses any off-allowlist program ID).
- **Value-transfer pinning** built (every System SOL transfer recipient pinned to the live Jito tip
  accounts + own ATA-rent destinations).
- **Secret handling** built (Vault short-lived token → `mlock` → zeroize; never an env var/log/disk).
- **Peer-cred gate** on the signer's Unix socket; no inbound network.
- **Real image digests + container lockdown + hash-locked deps** (the image/supply-chain items,
  including the F-10 digest restoration — see §7 below).

### 5.8 Flip the switch (only when A + B + C are all green)

```
DRY_RUN_ENABLED=false      # EXPLICITLY false (absent != false). Set ONLY here.
AATS_ENV=mainnet-live
```

Then fund the capped wallet, bring up the signer with a fresh Vault token, confirm it refuses an
over-cap and an off-allowlist tx, and `POST /api/mode {LIVE}` with the CEO token. Watch GATE-A /
GATE-B live on Grafana. **R3 is a fresh proof, not a continuation of R2.**

---

## 6. Operator surfaces (any stage)

These let *you* observe and de-risk. They are de-risk-only and can be set at any stage; the
command/alert tokens become meaningful once a control plane is running.

### 6.1 Telegram — TWO separate bots via @BotFather

You create **two distinct bots**. Do not reuse one for both roles.

**(1) Operator COMMAND bot** — `/status`, `/kill`, `/flatten <mint>`, `/pause` (de-risk only).

1. In Telegram, open **@BotFather** → send `/newbot` → choose a name and username → BotFather replies
   with a **bot token**.
2. Store that token in **Vault**, referenced by `TELEGRAM_BOT_TOKEN_VAULT_REF` (default path
   `secret/aats/telegram-bot-token`). **The token is never an env var** — a leaked token must not be
   able to drive the de-risk channel.
3. The command bot presents the **operator bearer token** (`OPERATOR_TOKEN` / `OPERATOR_API_TOKEN`,
   §5.4) to the control plane on every de-risk POST.
4. Find **your** numeric Telegram user ID via **@userinfobot** (send it any message; it replies with
   your ID). Put it in `TELEGRAM_OPERATOR_USER_IDS` (comma/space-separated; placeholder = empty
   allowlist = **no command authorized**, fail-closed). `/kill` and `/flatten` additionally require a
   per-command confirm.

**(2) ALERT bot** — receives Alertmanager notifications. A **separate** bot.

1. **@BotFather** → `/newbot` again → a **different** bot → copy its token into
   `ALERTMANAGER_TELEGRAM_BOT_TOKEN`.
2. Put the destination chat/channel ID in `ALERTMANAGER_TELEGRAM_CHAT_ID`.
3. Optional: `ALERTMANAGER_PAGERDUTY_KEY` for PagerDuty routing (blank disables).

| What it's for | Exact var | Required? |
|---|---|---|
| Command-bot token (Vault) | `TELEGRAM_BOT_TOKEN_VAULT_REF` | Optional surface; required to use the command bot |
| Your operator user ID(s) | `TELEGRAM_OPERATOR_USER_IDS` | Required to authorize any command (fail-closed if empty) |
| Alert-bot token | `ALERTMANAGER_TELEGRAM_BOT_TOKEN` | Optional |
| Alert chat ID | `ALERTMANAGER_TELEGRAM_CHAT_ID` | Optional |

### 6.2 Discord — read-only channel monitor (optional, SLOW loop)

1. Go to the **Discord Developer Portal** (`https://discord.com/developers/applications`) → **New
   Application**.
2. **Bot** tab → **Add Bot** → **copy the Bot token** → put it in `DISCORD_BOT_TOKEN`. (Self-bot /
   user tokens are **forbidden** and structurally rejected.)
3. **OAuth2** → invite the bot to each allowlisted server with **Read Messages** + **Read Message
   History** only — no other permissions.
4. List the exact channels it may read in `DISCORD_CHANNEL_ALLOWLIST` as a JSON array of
   `[guild_id, channel_id]` integer pairs, e.g.
   `[[1234567890123456789, 9876543210987654321]]`. The bot reads **only** these channels.

| What it's for | Exact var | Required? |
|---|---|---|
| Discord bot token | `DISCORD_BOT_TOKEN` | Optional |
| Channel allowlist (JSON pairs) | `DISCORD_CHANNEL_ALLOWLIST` | Optional |

### 6.3 Dashboard — live-wire to the control plane

| What it's for | Exact var | How |
|---|---|---|
| Use real telemetry instead of mock | `VITE_USE_MOCK=false` | Default `true` (standalone mock). Set `false` to fetch the live operator API. |
| Control-plane base URL | `VITE_CONTROL_PLANE_URL` | Point at your control plane, e.g. `http://localhost:8787`. (`CONTROL_PLANE_URL` is the server-side equivalent the Telegram bot uses.) |

> If the dashboard is blank with `VITE_USE_MOCK=false`, the control plane is down or the URL is
> wrong — set `VITE_USE_MOCK=true` to run standalone, or fix `VITE_CONTROL_PLANE_URL`.

---

## 7. Before you flip the live switch: restore real image digests (F-10)

A reminder cross-referenced from `docs/pre-live-checklist.md` Block B (F-10). Before
`DRY_RUN_ENABLED=false`, every base image must be pinned to a **real** `@sha256` digest — the repo
ships `@sha256:placeholder` pins that **cannot** ship live. For each base image:

```bash
docker pull <image>
docker inspect --format '{{index .RepoDigests 0}}' <image>
# -> pin the printed <image>@sha256:<digest> in the 7 docker/Dockerfile.* and docker-compose.yml
```

The 7 Dockerfiles are: `Dockerfile.bot`, `Dockerfile.controlplane`, `Dockerfile.dashboard`,
`Dockerfile.dms`, `Dockerfile.hotcore`, `Dockerfile.signer`, `Dockerfile.telegram`.

---

## 8. What you need RIGHT NOW: nothing — keep running paper.

The system runs today in PAPER / SHADOW / DRY-RUN with **zero** real credentials. The edge is
**UNPROVEN** until proven on recorded mainnet data; real capital is **disabled by default** and stays
that way until the pre-live checklist clears and the CEO authorizes R3. Set your local
`GRAFANA_ADMIN_PASSWORD` if you like, keep the paper loop running, and add keys **only** as you climb
the ladder — S1 devnet, then S2 record real data, and S3 only after the edge is proven and the gates
are green.
