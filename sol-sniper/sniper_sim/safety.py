"""Pre-trade safety gate — the block-0 veto.

ORDERING MATTERS: cheapest + most-decisive checks first, short-circuit on the
first failure. Checks 1-5 are decodable from data you already hold (0 RPC) and
belong in the sub-10ms hot path. Check 6 (sell simulation) is 30-100ms and is
explicitly NOT in the block-0 path — it gates slightly-later (N+2+) entries.

In live code each method does a real on-chain check. Here we model the *gate's
quality* with `catch_rate`: the probability the gate catches a catchable rug.
That lets the sim answer "how much does a better gate improve net PnL?".
"""
from __future__ import annotations

from .types import LaunchEvent


# Ordered list documents the real hot-path sequence (and that sell-sim is last).
GATE_ORDER = [
    "freeze_authority_set",        # 1. instant reject — 0 RPC
    "mint_authority_not_renounced",# 2. 0 RPC
    "lp_not_burned_or_locked",     # 3. cached LP-mint check
    "dev_or_bundle_cluster",       # 4. creation tx + first buyers
    "sell_tax_too_high",           # 5. token-2022 ext / cached
    "sellability_sim",             # 6. 30-100ms — parallel, gates N+2+ only
]


class SafetyGate:
    def __init__(self, enabled: bool = True, catch_rate: float = 0.75, rng=None):
        self.enabled = enabled
        self.catch_rate = catch_rate          # quality of checks 1-5 combined
        self.rng = rng

    def local_pass(self, ev: LaunchEvent) -> tuple[bool, str]:
        """Run the 0-RPC checks (1-5). Returns (passed, reason_if_failed).

        Modelled: a catchable rug is rejected with probability `catch_rate`; an
        uncatchable rug slips through (that's what `truth_rug_detectable=False`
        means). Non-rugs always pass here.
        """
        if not self.enabled:
            return True, ""
        if ev.truth_is_rug and ev.truth_rug_detectable:
            if self.rng.random() < self.catch_rate:
                return False, "gate:rug_signature"
        return True, ""

    def hard_veto(self, ev: LaunchEvent) -> bool:
        """Non-negotiable kill switches independent of the model score.

        Placeholder for: freeze authority present, LP unburned over threshold,
        dev% over cap. Wired to the same path as the LLM exit-veto (de-risk only).
        """
        return False
