# AATS — ELITE ARCHITECTURE (the mental model for whoever continues)

> Purpose: give any model picking up the program the complete mental map — topology, module seams, data flow,
> and the exact places every hard rule is enforced — so new missions plug in correctly without re-deriving it.

## 1 · The system in one paragraph
AATS is a Solana meme-coin sniper built as a **triple-loop** engine with a Rust hot path and a Python slow
path, joined by a Redis Streams bus and frozen typed contracts. It detects genuine launches, evaluates each
against a leak-free model + a sub-10ms safety gate, and (only when `DRY_RUN` is off and the CEO has authorized)
lands swaps via an `ExecutionVenue` seam through an isolated Vault signer. The honest edge is **de-risk +
reaction-timing**, not a profit oracle. Real capital is disabled until an edge is proven on *recorded* data.

## 2 · The triple loop (latency tiers)
| Loop | Budget | Owns | May call |
|---|---|---|---|
| **SNIPE** (Rust hot core) | single-digit ms | first-block entry decision; reads the ONNX snipe classifier + a pre-set safety verdict | NOTHING slow — no RPC, no LLM, no sell-sim |
| **FAST** (Rust/Python OMS) | ~10–100 ms | per-position FSM, stop/TP enforcement, exit branches | reads PRE-SET de-risk flags only |
| **SLOW** (Python) | seconds | features, ML, MCS/sentiment, LLM reasoner, detection/enrichment, sell-sim re-probe | everything heavy lives here |
**Iron law:** heavy work (LLM, sell-sim, detection, model training) is SLOW-loop; it publishes a **pre-set
flag/scalar** that the FAST/SNIPE branch merely *reads*. This is why the exit branches (E14b insider-dump,
E17 sellability, E19 LP-unlock) take a `*_flag: bool` and never compute anything on the hot path.

## 3 · Module map (where things live)
```
aats/
  ingestion/      M1 — detection + decode + raw producers
    transport.py      GeyserTransport (Yellowstone gRPC) · EnhancedWsFallback · PumpPortalTransport
    decoders.py       pump.fun / PumpSwap / Raydium v4 / CPMM decoders (+ EventKind sidecar)
    shadow_record.py  first-K-slot corpus recorder (create-anchored, CENSORED-on-reconnect)
    smart_money.py    wallet-watch (disabled-default, capped 20, count-only)  [E-M1-04 live backend]
    insider_dump.py   creator/top-holder SELL detection -> pre-set flag        [E14a]
    deployer_reputation.py  serial-deployer point-in-time reputation            [E15]
    dev_funding_age.py      fresh-wallet funding-age heuristic  ⚠️UNREVIEWED     [E16]
  features/       M1 — point-in-time feature math (microstructure, TA)          [+ M2-CP-01 tensor]
  sentiment/      M1 — MCS pipeline (Tier-A/B), caller-score(E9), velocity(E10), news(E7)
    call_extract.py   RawPost -> CallerCall (base58 + no-LLM direction)          [EN4]
    adapters.py       X/Reddit/Telegram(telethon)/Discord adapters               [EN3 live Telegram]
    caller_score.py   KOL track-record scorer + ParquetCallerOutcomeStore        [EN1 fix, EN5]
  models/         M2 — snipe classifier (ONNX), survivor model, calibration, baseline, monitor, gate_a/b
                       [+ M2-CP-03 regime model, M2-CP-05 regime baseline]
  reasoning/      M2 — LLM reasoner (asymmetric-trust adjudication, SLOW-only)
  controller/     M3 — triple-loop FSM, state.py (StateStore flags), fast_loop.py, slow_loop.py, __main__.py
  risk/           M4 — pretrade_gate, exit_engine, sizing (fractional-Kelly), circuit_breaker,
                       survivable_stop + deadman, blacklist(E2), screener(E8), anti_fomo(E13), cost_model,
                       sellability_reprobe(E17), deployer_reputation_gate(E15), dev_funding_age_gate(E16)
  execution/      M4 — ExecutionVenue (Jupiter/Raydium/Simulation), sell_sim honeypot probe, signer client
  contracts/      FROZEN typed contracts — DO NOT edit without a solana-systems-architect ADR
rust/             aats-hotcore, aats-signer (scaffold /health today; real impl deferred to R3)
```

## 4 · Data flow (a launch's life)
```
Geyser/PumpPortal ──▶ decoders ──▶ LaunchEvent (event-time, T-300a) ──▶ Redis bus
     │                                          │
     ▼ (SLOW enrichment: features, MCS, reputation, funding-age, insider-dump, sell-sim re-probe)
 pre-set de-risk flags in StateStore  ◀─────────┘
     │
SNIPE loop: safety gate (pretrade_gate) + ONNX snipe prob  ──▶ ENTER? (SimulationVenue in paper)
     │
FAST loop (per position): exit_engine reads pre-set flags
   ordered de-risk hierarchy: narrative_failure ▶ INSIDER_DUMP(E14b) ▶ hard_stop ▶ ratchet ▶
   trailing ▶ sellability_degraded(E17) ▶ timeout ▶ stale-narrative ▶ TP-ladder
```

## 5 · The asymmetric-trust seam (the single most important invariant)
Every new signal enters through a **de-risk-only** consumer. Concretely, the ONLY legal effects are:
`REJECT` (pre-gate veto, like `blacklist.py`), `DOWN-WEIGHT` (a conviction multiplier **clamped ≤ 1**, like
`anti_fomo.py`), or `FORCE-EXIT/REDUCE` (an ExitEngine branch, SECURE-routed). There is **no code path** by
which a signal can raise size, widen a stop, raise conviction, or add leverage — and the contracts
(`DecisionSignal`, `MCSScore`, `ReasoningAction` = HOLD/VETO_ENTRY/REDUCE_SIZE/FORCE_EXIT) make the forbidden
literally unrepresentable. **When you add a mission, wire it through one of these three effects only.**

## 6 · Where each hard rule is enforced (so you don't break it)
- **No win-rate:** contracts have no such field; `backtest-qa` greps for it. Report outcome *tallies*.
- **DRY_RUN gate:** `controller/__main__.py` exits if `DRY_RUN_ENABLED` is false unless CEO-gated; venue is
  `SimulationVenue` in paper. Nothing in Waves 1–5 touches this.
- **Point-in-time:** `EventTime{slot, block_time_ms, wall_clock_ms}`; `assert_event_time_leq_decision` +
  `assert_no_label_taint` in `models/training.py`; absent block_time → held pending.
- **Hot-path purity:** exit branches take pre-set flags; detection/sell-sim/LLM are SLOW-loop.
- **Custody:** wallet SECRET only in Vault, fetched by `aats-signer` at boot; hot core holds PUBKEY only;
  `.env.example` is the ONLY schema doc, placeholders only.

## 7 · The honest edge thesis (why the architecture looks like this)
From the elite-bot + honest-builder research (see the directive): predicting "which token moons" fails
(~95% rug); the exploitable edges are **(a) speed** (detection-competitive here; submission-disadvantaged for a
solo op — see `deploy/colocation-rpc-plan.md`) and **(b) reaction-timing** — front-running the predictable
retail flood after a *proven* KOL call / smart-money buy, with disciplined laddered exits. The **cost gate**
(`risk/cost_model.py`) rejects any entry whose edge is below round-trip cost (~6% incl. Jito tip + fees +
slippage + 150bps adverse-selection floor) — the direct answer to why most bots end up break-even. Everything
else is catastrophe-avoidance. This is why the whole program is de-risk-only and gated on a real edge proof.
