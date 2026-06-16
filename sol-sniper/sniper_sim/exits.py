"""Exit engine — TP ladder + trailing stop + hard stop + timeout, Smart-MEV aware.

Inspired by Photon's limit/DCA + auto take-profit/stop-loss and its Fast/Secure
MEV modes. Exits are where the defensible edge lives (Part C.5), so we model
staged exits over a realistic price PATH instead of the old "capture 0.5x of
peak" hand-wave.

PnL bookkeeping is in units of the position cost (sol_in). Triggers are evaluated
on R = realized return on ENTRY = path_multiple / (1 + entry_slippage), so a "2x
TP" means 2x your actual fill, not 2x the launch spot.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ExitConfig:
    label: str
    tp_ladder: tuple          # ((trigger_R, fraction_to_sell), ...)
    hard_stop: float          # dump all if R <= this (e.g. 0.60 => -40%)
    trail_arm: float          # arm trailing once R >= this
    trail_drawdown: float     # after armed, exit if R <= peakR*(1-this)
    max_hold_steps: int
    exit_slippage: float      # per-sell base haircut
    sandwich_p: float         # P(sandwiched on a sell)
    sandwich_loss: float      # extra haircut if sandwiched


# Photon's two MEV modes, modeled. Fast = speed/public-ish (more sandwich);
# Secure = private routing (sandwich-safe, slightly worse base price).
FAST_EXIT = ExitConfig(
    label="fast", tp_ladder=((1.5, 0.30), (3.0, 0.30), (6.0, 0.20)),
    hard_stop=0.60, trail_arm=2.0, trail_drawdown=0.30, max_hold_steps=24,
    exit_slippage=0.015, sandwich_p=0.30, sandwich_loss=0.12)

SECURE_EXIT = ExitConfig(
    label="secure", tp_ladder=((1.5, 0.30), (3.0, 0.30), (6.0, 0.20)),
    hard_stop=0.60, trail_arm=2.0, trail_drawdown=0.30, max_hold_steps=24,
    exit_slippage=0.025, sandwich_p=0.08, sandwich_loss=0.04)


def generate_path(max_multiple: float, is_rug: bool, rng, n: int = 30) -> list[float]:
    """Synthetic price path in multiples of launch spot.

    Rug: brief blip then liquidity pulled -> collapses toward ~0 (the hard stop
    should save most of the position). Runner/dud: rise to peak=max_multiple then
    retrace to a floor (TP ladder + trailing harvest the move).
    """
    path = []
    if is_rug:
        peak = rng.randint(1, 3)
        for i in range(n):
            if i <= peak:
                m = 1.0 + (max_multiple - 1.0) * (i / max(peak, 1))
            else:
                m = max(0.02, max_multiple * (0.5 ** (i - peak)))
            path.append(max(0.01, m * rng.gauss(1.0, 0.03)))
        return path
    peak_step = rng.randint(4, 14)
    floor = max_multiple * rng.uniform(0.25, 0.45)
    for i in range(n):
        if i <= peak_step:
            m = 1.0 + (max_multiple - 1.0) * (i / peak_step)
        else:
            m = floor + (max_multiple - floor) * (0.85 ** (i - peak_step))
        path.append(max(0.01, m * rng.gauss(1.0, 0.04)))
    return path


def _sell(frac: float, R: float, cfg: ExitConfig, rng) -> float:
    haircut = cfg.exit_slippage + (cfg.sandwich_loss if rng.random() < cfg.sandwich_p else 0.0)
    return frac * R * (1.0 - haircut)


def run_exit(sol_in: float, entry_slippage: float, path: list[float],
             cfg: ExitConfig, rng) -> float:
    """Walk the path applying rules. Returns GROSS PnL in SOL (excl. tips)."""
    basis = 1.0 + entry_slippage
    remaining = 1.0
    proceeds = 0.0                      # in units of sol_in
    peakR = 0.0
    armed = False
    fired = [False] * len(cfg.tp_ladder)

    for step, m in enumerate(path):
        if remaining <= 0:
            break
        R = m / basis
        peakR = max(peakR, R)
        if not armed and R >= cfg.trail_arm:
            armed = True
        # 1) hard stop (highest priority — limits rug damage)
        if R <= cfg.hard_stop:
            proceeds += _sell(remaining, R, cfg, rng)
            remaining = 0.0
            break
        # 2) TP ladder
        for i, (trig, frac) in enumerate(cfg.tp_ladder):
            if not fired[i] and R >= trig and remaining > 0:
                f = min(frac, remaining)
                proceeds += _sell(f, R, cfg, rng)
                remaining -= f
                fired[i] = True
        # 3) trailing stop on the runner remainder
        if armed and remaining > 0 and R <= peakR * (1.0 - cfg.trail_drawdown):
            proceeds += _sell(remaining, R, cfg, rng)
            remaining = 0.0
            break
        # 4) timeout
        if step >= cfg.max_hold_steps:
            break

    if remaining > 0:                   # forced exit at last observed price
        idx = min(len(path) - 1, cfg.max_hold_steps)
        proceeds += _sell(remaining, path[idx] / basis, cfg, rng)

    return sol_in * (proceeds - 1.0)
