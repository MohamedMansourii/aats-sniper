"""Jito tip strategy — bid to land, but never out of proportion to the edge.

A tip is pure cost: it does not improve your fill, only your odds of landing the
slot. The cardinal sin (Part C.5) is escalating tips into a latency war you can't
win — that just subsidizes validators. So the tip is bounded BOTH by a market
floor (you must clear recent landed tips to have a chance) and by a hard fraction
of the trade's expected edge.
"""
from __future__ import annotations

from .types import LAMPORTS_PER_SOL


class TipStrategy:
    def __init__(self, market_tip_lamports: int = 200_000,
                 floor_frac: float = 1.0, edge_cap_frac: float = 0.30):
        self.market_tip = market_tip_lamports     # recent landed-tip median proxy
        self.floor_frac = floor_frac              # bid at least this * market median
        self.edge_cap_frac = edge_cap_frac        # never tip more than this * edge

    def competitive(self, sol_in: float, p_model: float,
                    expected_multiple: float) -> int:
        """Return tip in lamports.

        expected_edge (SOL) ~= position * P(win) * (E[multiple]-1), conservative.
        Tip is clamped to [floor, edge_cap]. If the cap is below the floor, the
        race isn't worth entering at a competitive tip — caller can treat a tip at
        floor as "long shot" and the metrics will show it bleeding.
        """
        expected_edge_sol = max(0.0, sol_in * p_model * (expected_multiple - 1.0))
        edge_cap = int(self.edge_cap_frac * expected_edge_sol * LAMPORTS_PER_SOL)
        floor = int(self.floor_frac * self.market_tip)
        return max(floor, min(edge_cap, 5 * self.market_tip)) if edge_cap > floor else floor


def priority_lamports(cu_price_microlamports: int, cu_limit: int = 200_000) -> int:
    """Priority fee = CU price (micro-lamports/CU) * CU limit, converted to lamports."""
    return int(cu_price_microlamports * cu_limit / 1_000_000)
