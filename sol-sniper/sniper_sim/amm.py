"""Constant-product AMM math (Raydium v4 / CPMM style).

Raydium charges ~25 bps to LPs. Reserves are (SOL, token). Price quoted as
SOL-per-token so a higher price = token worth more.

The snipe-relevant twist is `simulate_entry`: by the time your buy lands you are
N slots late, and `buyers_ahead` other snipers have already bought, pushing the
price against you. Your fill is priced AFTER their impact.
"""
from __future__ import annotations

RAYDIUM_FEE = 0.0025


def buy(sol_reserve: float, tok_reserve: float, sol_in: float,
        fee: float = RAYDIUM_FEE) -> tuple[float, float, float]:
    """Return (tokens_out, new_sol_reserve, new_tok_reserve) for a SOL->token buy."""
    sol_in_eff = sol_in * (1.0 - fee)
    tok_out = tok_reserve * sol_in_eff / (sol_reserve + sol_in_eff)
    return tok_out, sol_reserve + sol_in, tok_reserve - tok_out


def spot_price(sol_reserve: float, tok_reserve: float) -> float:
    """SOL per token."""
    return sol_reserve / tok_reserve


def simulate_entry(sol_reserve: float, tok_reserve: float, sol_in: float,
                   buyers_ahead: int, ahead_size: float
                   ) -> tuple[float, float, float]:
    """Apply `buyers_ahead` co-buyers, then your buy.

    Returns (tokens_out, effective_price, entry_slippage_fraction) where slippage
    is measured against the untouched spot price (your true adverse selection).
    """
    spot_before = spot_price(sol_reserve, tok_reserve)
    s, t = sol_reserve, tok_reserve
    for _ in range(buyers_ahead):
        _, s, t = buy(s, t, ahead_size)
    tok_out, _, _ = buy(s, t, sol_in)
    if tok_out <= 0:
        return 0.0, float("inf"), 1.0
    effective_price = sol_in / tok_out
    slippage = effective_price / spot_before - 1.0
    return tok_out, effective_price, slippage
