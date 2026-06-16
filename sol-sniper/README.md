# sol-sniper — M4 simulation venue (measure before you risk a lamport)

A stdlib-only harness that models the **landing race + slippage + rugs + exit
sandwiching** behind the same `ExecutionVenue` interface the real Jito/Jupiter
venue will implement. Use it to prove (or disprove) edge in simulation before any
SOL moves.

## Run

```powershell
# from C:\Users\manso\sol-sniper
python -m sniper_sim.demo            # 4000 launches, 3 scenarios
python -m sniper_sim.demo 20000      # more launches
```

No dependencies. Python 3.11+.

## What it models (and what it fakes)

| Real thing | How the sim models it | File |
|---|---|---|
| Infra latency → which slot you can target | `InfraTier` (generic_ws / dedicated_geyser / colo_shred) → slot delay | `venue.py` |
| Jito tip auction | competitors' tips ~ lognormal; you land a slot only if your tip ranks within `slot_capacity` | `venue.py` |
| Entry slippage | constant-product AMM with `buyers_ahead` co-buyers consuming liquidity first | `amm.py` |
| Anti-honeypot bundle | `assert_min_out`: if slippage > tolerance the buy **reverts, no tip spent** | `venue.py` |
| Safety gate | ordered 0-RPC checks; `catch_rate` = gate quality on catchable rugs | `safety.py` |
| Tip discipline | tip clamped to `edge_cap_frac · expected_edge` (never subsidize validators) | `tips.py` |
| Exit sandwiching | public vs private submit → different `(p_sandwich, loss)` | `demo.py` |
| Snipe model | **EMULATED** skill (reads truth through a noise channel) — NOT a real model | `demo.py` |

**Faked, on purpose:** the launch distribution and the model's predictive skill.
These are illustrative priors. Replace them with your **own recorded first-K-slot
launch data** (M1 shadow/record mode) and a **real LightGBM→ONNX** model before
trusting any number.

## Going live (the seam)

`venue.JitoJupiterVenue` is the production stub. It deliberately raises until you
wire:
- Jito searcher client → `bundle([buy_ix, assert_min_out_ix])` + tip account
- direct Raydium/PumpSwap swap-instruction build (NOT Jupiter, for block-0)
- Jupiter v6/Ultra quote→swap for **exits/survivors** only
- staked-send / SWQOS endpoint for landing
- a **trade-only, capped, isolated-signer** wallet (never your main Phantom key)

## Hot path is Rust

Python is fine for this sim, the slow loop, and model training. The **snipe + fast
loops are Rust** (`yellowstone-grpc`, `solana-sdk`, `jito-searcher-client`, `ort`
for ONNX). Python's borsh decode + GIL + GC turns a 20ms budget into 200ms — that
is the difference between landing N+1 and N+5.

## Edge gate before scaling (M5)

Do not add capital until, on small live size, BOTH are true:
1. **NET PnL** (after tips + priority + slippage) is positive.
2. **Snipe-model-vs-baseline** (vs buy-everything-that-lands) is positive.
