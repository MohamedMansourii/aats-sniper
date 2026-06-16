"""ExecutionVenue interface + SimulationVenue + a stub for the real Jito/Jupiter venue.

The interface is the seam: the snipe/fast loops call `execute(intent, event)` and
get a FillResult, never knowing if they're in sim or live. Swap SimulationVenue
for JitoJupiterVenue and the loops are unchanged.
"""
from __future__ import annotations

import math
from abc import ABC, abstractmethod
from dataclasses import dataclass

from . import amm
from .types import FillResult, LaunchEvent, SwapIntent


SLOT_MS = 400  # Solana target slot time


class ExecutionVenue(ABC):
    @abstractmethod
    def execute(self, intent: SwapIntent, event: LaunchEvent) -> FillResult:
        ...


# --- infra tiers: internal-latency profile that decides your targetable slot ---
@dataclass(frozen=True)
class InfraTier:
    name: str
    ingress_ms: float      # observe-the-event latency (mean)
    jitter_ms: float
    build_ms: float        # decode+gate+model+build+sign+submit (non-ingress)


TIERS = {
    # generic VPS + enhanced WS + Jupiter HTTP build: loses block-0, lands late
    "generic_ws": InfraTier("generic_ws", ingress_ms=300, jitter_ms=120, build_ms=250),
    # dedicated node + Yellowstone + direct AMM build
    "dedicated_geyser": InfraTier("dedicated_geyser", ingress_ms=60, jitter_ms=25, build_ms=6),
    # colo bare-metal + ShredStream + direct AMM build + Jito
    "colo_shred": InfraTier("colo_shred", ingress_ms=18, jitter_ms=8, build_ms=3),
}


class SimulationVenue(ExecutionVenue):
    """Models the landing race + slippage. No network. Seeded for reproducibility."""

    def __init__(self, tier: InfraTier, rng,
                 slot_capacity: int = 3, max_slots: int = 3,
                 market_tip_lamports: int = 200_000,
                 ahead_size_sol: float = 1.2):
        self.tier = tier
        self.rng = rng
        self.slot_capacity = slot_capacity        # winning bundles per slot region
        self.max_slots = max_slots                # give up after this many slots
        self.market_tip = market_tip_lamports
        self.ahead_size = ahead_size_sol          # avg SOL each co-buyer spends

    # ---- helpers ----
    def _poisson(self, lam: float) -> int:
        # Knuth's algorithm; fine for the small lambdas here.
        L, k, p = math.exp(-lam), 0, 1.0
        while True:
            k += 1
            p *= self.rng.random()
            if p <= L:
                return k - 1

    def _competitor_tip(self) -> int:
        # lognormal around the market median tip
        return int(self.market_tip * self.rng.lognormvariate(0.0, 0.6))

    def _competitor_delay(self) -> int:
        """How late a competing PRO bot is. Pros are fast: mostly N+0/N+1, and
        ~15% are insiders co-bundling with the LP-add at N+0 (you cannot beat
        those — that's the structural gap from Part A.3)."""
        r = self.rng.random()
        if r < 0.15:
            return 0          # insider / co-bundle — ahead of everyone
        if r < 0.75:
            return 1
        if r < 0.95:
            return 2
        return 3

    def _attempt_land(self, intent: SwapIntent, event: LaunchEvent):
        """Return (landed, land_slot, slot_delay, buyers_ahead).

        It's an AMM, not a winner-take-all auction: you almost always get *a*
        fill, but your price is set by how many faster/higher-tipped competitors
        bought AHEAD of you (-> slippage). If you're so late the launch ran past
        your slot window, you miss entirely.
        """
        internal_ms = max(1.0, self.rng.gauss(self.tier.ingress_ms, self.tier.jitter_ms)) \
            + self.tier.build_ms
        my_delay = max(1, math.ceil(internal_ms / SLOT_MS))
        if my_delay > self.max_slots:                 # launch already ran — too late
            return False, None, my_delay, event.competitors
        buyers_ahead = 0
        for _ in range(event.competitors):
            cd = self._competitor_delay()
            if cd < my_delay:                         # they landed in an earlier slot
                buyers_ahead += 1
            elif cd == my_delay and self._competitor_tip() > intent.tip_lamports:
                buyers_ahead += 1                     # same slot, outbid on tip -> ahead
        return True, event.slot + my_delay, my_delay, buyers_ahead

    def execute(self, intent: SwapIntent, event: LaunchEvent) -> FillResult:
        from .tips import priority_lamports
        prio = priority_lamports(intent.cu_price_microlamports)
        landed, land_slot, slot_delay, buyers_ahead = self._attempt_land(intent, event)
        if not landed:
            # didn't win the viable slot window -> launch ran / outbid. In a Jito
            # bundle you spend nothing, but you also caught nothing.
            return FillResult(landed=False, reason="too_late_or_outbid",
                              slot_delay=slot_delay, buyers_ahead=buyers_ahead,
                              tip_lamports=0, priority_lamports=0)

        tok_out, eff_price, slip = amm.simulate_entry(
            event.sol_reserve, event.token_reserve, intent.sol_in,
            buyers_ahead=buyers_ahead, ahead_size=self.ahead_size)

        # assert_min_out: if slippage blew past tolerance, the bundle reverts ->
        # no fill, no tip spent (this is the anti-honeypot atomicity in action).
        if slip > intent.slippage_bps / 10_000:
            return FillResult(landed=False, reason="reverted_min_out",
                              land_slot=land_slot, slot_delay=slot_delay,
                              buyers_ahead=buyers_ahead, entry_slippage=slip,
                              tip_lamports=0, priority_lamports=0)

        return FillResult(landed=True, reason="filled", land_slot=land_slot,
                          slot_delay=slot_delay, buyers_ahead=buyers_ahead,
                          tokens_out=tok_out, effective_price=eff_price,
                          entry_slippage=slip, tip_lamports=intent.tip_lamports,
                          priority_lamports=prio)


class JitoJupiterVenue(ExecutionVenue):
    """Real venue — NOT implemented here on purpose.

    Wiring (see M4): Jito searcher client for bundles[buy, assert_min_out] with a
    tip account; Jupiter v6 quote->swap for exits/survivors; staked send for
    landing. It will refuse to run until creds + a funded trade-only wallet are
    configured, so you can never accidentally fire it from the sim.
    """

    def execute(self, intent: SwapIntent, event: LaunchEvent) -> FillResult:
        raise NotImplementedError(
            "Live venue disabled. Prove edge in SimulationVenue first (M5: net-of-cost "
            "PnL AND model-vs-baseline both positive on small size).")
