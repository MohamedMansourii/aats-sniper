"""Runnable scenario harness: measure land rate, slippage, rug-avoidance, and
PnL net of tips+priority+slippage across infra/strategy choices — on the SAME
synthetic launches, so the deltas are apples-to-apples.

Run:
    python -m sniper_sim.demo          # from C:\\Users\\manso\\sol-sniper
    python sniper_sim/demo.py          # also works (self-paths)

ALL NUMBERS ARE ILLUSTRATIVE PRIORS. The whole point of this harness is that you
replace the synthetic launch distribution and the emulated model skill with your
OWN recorded first-K-slot data before believing a single figure.
"""
from __future__ import annotations

import math
import os
import random
import sys

# self-path so `python sniper_sim/demo.py` works regardless of cwd
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sniper_sim import exits
from sniper_sim.metrics import Metrics
from sniper_sim.safety import SafetyGate
from sniper_sim.tips import TipStrategy
from sniper_sim.types import LaunchEvent, SwapIntent
from sniper_sim.venue import TIERS, SimulationVenue

SOL_IN = 1.0           # per-coin position cap (SOL)
EXIT_EFF = 0.50        # fraction of the peak you actually capture on exit
SLIPPAGE_BPS = 1500    # 15% max — beyond this the min-out assert reverts the buy


def sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


def generate_launches(n: int, rng: random.Random) -> list[LaunchEvent]:
    out = []
    for i in range(n):
        is_rug = rng.random() < 0.60                      # most launches are traps
        if is_rug:
            max_mult = rng.uniform(0.0, 0.5)              # you salvage 0-50%
            detectable = rng.random() < 0.70              # 70% are gate-catchable
        else:
            max_mult = 0.8 + rng.lognormvariate(0.3, 1.0) # mostly small, rare moonshot
            detectable = False
        sol_reserve = rng.uniform(20, 85)
        token_reserve = sol_reserve * rng.uniform(1e6, 5e7)
        competitors = max(1, int(rng.lognormvariate(1.3, 0.7)))
        out.append(LaunchEvent(
            mint=f"MINT{i:04d}", slot=1000 + i * 4,
            sol_reserve=sol_reserve, token_reserve=token_reserve,
            competitors=competitors, truth_is_rug=is_rug,
            truth_max_multiple=max_mult, truth_rug_detectable=detectable))
    return out


def model_prob(ev: LaunchEvent, skill: float, rng: random.Random) -> float:
    """EMULATED snipe-classifier output.

    Real version: LightGBM->ONNX on point-in-time first-K-slot features. Here we
    emulate a model of given `skill` (0=coin-flip, 1=oracle) by reading truth
    through a noise channel whose width shrinks with skill. This is ONLY to study
    how model quality moves net PnL — it is not a real predictor.
    """
    signal = math.log(max(ev.truth_max_multiple, 1e-3)) - (2.0 if ev.truth_is_rug else 0.0)
    noise = rng.gauss(0.0, (1.0 - skill) * 3.0 + 0.3)
    return sigmoid(0.6 * signal + noise)


def realize_pnl(ev, fill, p_sandwich, sandwich_loss, rng) -> float:
    """Gross trade PnL in SOL (excludes tips/priority — those are costs)."""
    spot_before = ev.sol_reserve / ev.token_reserve
    peak_price = spot_before * ev.truth_max_multiple
    haircut = sandwich_loss if rng.random() < p_sandwich else 0.0
    exit_price = peak_price * EXIT_EFF * (1.0 - haircut)
    proceeds = fill.tokens_out * exit_price
    return proceeds - SOL_IN


def run_scenario(name, launches, *, tier, gate_on, gate_catch, model_skill,
                 model_threshold, flat_tip, exit_public, seed, exit_cfg=None) -> Metrics:
    rng_v = random.Random(seed)          # venue (landing race)
    rng_m = random.Random(seed + 1)      # model + gate
    rng_x = random.Random(seed + 2)      # exit sandwich
    venue = SimulationVenue(tier, rng_v, market_tip_lamports=3_000_000)
    gate = SafetyGate(enabled=gate_on, catch_rate=gate_catch, rng=rng_m)
    tips = TipStrategy(market_tip_lamports=3_000_000, edge_cap_frac=0.30)
    m = Metrics(name)
    p_sandwich, sandwich_loss = (0.35, 0.15) if exit_public else (0.10, 0.05)

    for ev in launches:
        # SENSE/DECIDE
        p = model_prob(ev, model_skill, rng_m)
        if p < model_threshold:
            continue                                  # model SKIP
        passed, _ = gate.local_pass(ev)
        if not passed:
            m.record_skip_rug()                       # gate VETO (de-risk)
            continue
        m.candidates += 1

        # SIZE / TIP / SUBMIT
        if flat_tip is not None:
            tip = flat_tip
        else:
            tip = tips.competitive(SOL_IN, p, expected_multiple=1.0 + 3.0 * p)
        intent = SwapIntent(mint=ev.mint, sol_in=SOL_IN, slippage_bps=SLIPPAGE_BPS,
                            tip_lamports=tip, cu_price_microlamports=50_000,
                            target_slot=ev.slot + 1)
        fill = venue.execute(intent, ev)
        m.record_attempt(fill, faced_rug=ev.truth_is_rug)
        if fill.landed:
            if exit_cfg is None:                         # legacy naive "0.5x peak"
                m.add_pnl(realize_pnl(ev, fill, p_sandwich, sandwich_loss, rng_x))
            else:                                        # Photon-style staged exit
                path = exits.generate_path(ev.truth_max_multiple, ev.truth_is_rug, rng_x)
                m.add_pnl(exits.run_exit(SOL_IN, fill.entry_slippage, path, exit_cfg, rng_x))
    return m


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 4000
    launches = generate_launches(n, random.Random(42))
    n_rug = sum(1 for e in launches if e.truth_is_rug)
    print("=" * 70)
    print(f" sniper_sim — {n} synthetic launches  ({n_rug} rugs / {n-n_rug} non-rugs)")
    print(" ILLUSTRATIVE PRIORS ONLY — replace with your recorded first-K-slot data")
    print("=" * 70)

    scenarios = [
        ("1) Baseline: buy-all, NO gate, generic infra, flat tip",
         dict(tier=TIERS["generic_ws"], gate_on=False, gate_catch=0.0,
              model_skill=0.0, model_threshold=0.0, flat_tip=3_000_000,
              exit_public=True)),
        ("2) Gate + model, generic infra, Fast-MEV exit ladder",
         dict(tier=TIERS["generic_ws"], gate_on=True, gate_catch=0.75,
              model_skill=0.60, model_threshold=0.55, flat_tip=None,
              exit_public=True, exit_cfg=exits.FAST_EXIT)),
        ("3) Gate + model, COLO + ShredStream, Secure-MEV exit ladder",
         dict(tier=TIERS["colo_shred"], gate_on=True, gate_catch=0.75,
              model_skill=0.60, model_threshold=0.55, flat_tip=None,
              exit_public=False, exit_cfg=exits.SECURE_EXIT)),
    ]

    results = []
    for title, kw in scenarios:
        m = run_scenario(title, launches, seed=7, **kw)
        results.append(m)
        print(f"\n{title}\n{m.render()}")

    # ---- EXIT-POLICY A/B: identical colo fills, naive 0.5x-peak vs staged ladder ----
    colo = dict(tier=TIERS["colo_shred"], gate_on=True, gate_catch=0.75,
                model_skill=0.60, model_threshold=0.55, flat_tip=None,
                exit_public=False)
    naive = run_scenario("naive", launches, seed=7, exit_cfg=None, **colo)
    ladder = run_scenario("ladder", launches, seed=7, exit_cfg=exits.SECURE_EXIT, **colo)
    print("=" * 70)
    print(" EXIT-POLICY A/B (same colo fills) — the Photon-inspired upgrade")
    print(f"   naive 0.5x-peak exit         NET {naive.net_pnl_sol:+8.2f} SOL")
    print(f"   TP-ladder + trail + hardstop NET {ladder.net_pnl_sol:+8.2f} SOL")
    print(f"   exit-discipline delta        {ladder.net_pnl_sol - naive.net_pnl_sol:+8.2f} SOL"
          f"  ({'ladder wins' if ladder.net_pnl_sol > naive.net_pnl_sol else 'naive wins'})")

    base = results[0].net_pnl_sol
    print("=" * 70)
    print(" MODEL/INFRA vs BASELINE (net PnL delta on identical launches)")
    for m in results[1:]:
        print(f"  {m.name[:42]:42s}  {m.net_pnl_sol - base:+8.2f} SOL vs baseline")
    print("=" * 70)
    print(" Read it honestly: if the best scenario's NET PnL isn't comfortably")
    print(" positive here — with an OPTIMISTIC emulated model — it won't be live.")


if __name__ == "__main__":
    main()
