# REACTION-CORPUS-SPEC — the smart-money / signal→reaction edge (joint A+B)

> The on-chain LAUNCH-DATA edge is decisively falsified (both launch-winner and momentum-@60s ran NO-GO on 4,187 real
> launches; the early momentum GATE-B reversed with more data). Per the honest thesis, the only remaining lever with a
> real prior is **front-running the predictable retail reaction to a PROVEN signal**. This spec defines the corpus
> (Session A / ingestion builds the recorder) and the interface B's backtest consumes.

## The thesis (what we're testing)
A *proven* actor (smart-money wallet / KOL / whale) takes an observable action on a token at time T. Retail predictably
reacts in the seconds–minutes after T. **Does entering just after T (front-running the reaction) and exiting into it
yield positive net-of-cost PnL, and does a quality filter beat "follow every signal"?** If NO-GO here too, the honest
program conclusion is: no solo-operator edge exists for this bot — stay paper.

## Signal sources (Session A, ingestion lane) — in tractability order
1. **v1 — WHALE / large-early-buy (fully on-chain, NO external list needed).** Subscribe to the trade stream for fresh
   tokens; a single buy above a size threshold (e.g. ≥ X SOL, param) is the signal. A smart-money PROXY that needs no
   curated wallet set or credentials → buildable + testable here immediately.
2. **v2 — SMART-MONEY-WALLET buys.** `aats/ingestion/smart_money.py` already subscribes to ≤20 tracked wallets'
   account-trades. Signal = a tracked wallet buys a token. **Data dependency: a curated set of proven smart-money
   wallets** (operator/config-provided; sourced from a paid API or a vetted public list — flagged, not free).
3. **v2 — KOL calls.** `aats/telegram/` (Telethon) + `aats/sentiment/caller_score.py`. Signal = a proven caller posts a
   token. **Data dependency: Telegram API creds + channel list + a caller reputation prior** (flagged).

## The record (one JSON line per SIGNAL event) → `C:/aats_shadow/reaction_corpus.jsonl`
```
{ "signal_type": "whale_buy | smart_money_buy | kol_call",
  "source_id":   "<wallet | caller_id | 'whale'>",
  "mint":        "<token mint>",
  "signal_slot":          <int on-chain slot of the signal tx>,
  "signal_block_time_ms": <int on-chain block_time*1000 — T-300a, NEVER wall-clock; record None+censor if unavailable>,
  "signal_price_sol":     "<Decimal-as-string price at the signal>",
  "signal_size_sol":      "<Decimal-as-string — the signal buy size, for the quality filter>",
  "source_prior":         <float | null — caller-score / wallet reputation, if any (SELECTIVITY ONLY, de-risk)>,
  "forward": [ {horizon_s, price_sol, txns_m5:{buys,sells}, liquidity_usd, obs_wall_ms}, ... ] }
```
**`forward` uses the SAME shape as the launch corpus** so B's `resolve_outcome` / harness reuse directly. Horizons
(post-signal): 15/30/60/120/300/600 s (reaction is fast — tighter than the launch grid).

## The leak boundary (B's backtest must enforce; A records honestly)
- **Decision anchor = `signal_block_time_ms`** (on-chain). The front-run entry is at/just after it. The DECISION may
  use only the signal fields + any strictly-pre-signal context — NEVER a `forward` (post-signal) value.
- **Outcome = the `forward` path** (strictly after the signal). Entry price = `signal_price_sol` (+ realistic slippage/
  latency haircut — model a few-hundred-ms lag, since a real bot can't fill at the exact signal tick).
- Money int lamports/Decimal; net of the ~6% round-trip cost.

## The backtest (Session B, backtest lane)
- **Baseline (GATE-B control) = "follow EVERY signal"** (enter on every recorded signal, fixed size).
- **Model = quality-filtered** (by `signal_size_sol`, `source_prior`, early-pressure — de-risk/selectivity only).
- GATE-A: aggregate net-of-cost PnL with a real lower-95% bound. GATE-B: model beats "follow-every-signal" per unit
  risk, leak-free walk-forward. **NEVER fabricate a GO.** Same discipline as the launch proof.

## Build plan (A)
1. v1 whale-buy recorder (operational script, imports ingestion read-only; detached like B's collector) → accrue.
2. Ping B via MAILBOX when `reaction_corpus.jsonl` is flowing + confirm the schema. B builds the front-run harness.
3. v2 smart-money-wallet + KOL once a wallet set / Telegram creds are available (flag to CEO if they must be provided).

## Honest expectation
This has a *real prior* (proven-actor signals are informative in a way launch data is not), but front-running is
competitive and the ~6% cost gate is brutal — it may still be NO-GO. We test it rigorously and report the truth.
