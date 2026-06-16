"""
sniper_sim — a stdlib-only simulation harness for the M4 execution/landing path.

Goal: measure land rate, slot delay, entry slippage, rug-avoidance, and PnL
*net of tips + priority + slippage* BEFORE risking a lamport on-chain.

Nothing here touches the network. The SimulationVenue models the parts of the
real race that actually decide your edge:
  - slot-delay landing (infra-tier latency -> targetable slot)
  - Jito tip auction (you win a slot only if your tip ranks high enough)
  - constant-product (Raydium-style) slippage WITH co-buyers ahead of you
  - rugs (catchable vs uncatchable) and exit sandwiching

The same ExecutionVenue interface is what the real Jito+Jupiter venue implements,
so the slow/fast loops never learn whether they're in sim or live.
"""

__all__ = ["types", "amm", "safety", "tips", "venue", "metrics"]
