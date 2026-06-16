"""Metrics that prove (or disprove) the edge — the M5 scorecard.

Every number here maps to something you'd put on a Grafana panel in production:
land rate, time-to-land in slots, entry slippage, rug-avoidance, and PnL NET of
tips + priority + slippage. If net PnL and model-vs-baseline aren't both positive,
you do not scale.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .types import LAMPORTS_PER_SOL


@dataclass
class Metrics:
    name: str
    candidates: int = 0          # launches the model/gate said SNIPE on
    attempts: int = 0            # intents actually sent to the venue
    lands: int = 0
    reverts: int = 0             # min-out reverts (saved from a bad fill)
    slot_delays: list = field(default_factory=list)
    slippages: list = field(default_factory=list)
    rugs_faced: int = 0          # rugs among launches we tried to snipe
    rugs_avoided: int = 0        # rugs the gate made us skip before attempting
    gross_pnl_sol: float = 0.0
    tips_sol: float = 0.0
    priority_sol: float = 0.0

    def record_skip_rug(self):
        self.rugs_avoided += 1

    def record_attempt(self, fill, faced_rug: bool):
        self.attempts += 1
        if faced_rug:
            self.rugs_faced += 1
        if fill.slot_delay is not None:
            self.slot_delays.append(fill.slot_delay)
        if fill.landed:
            self.lands += 1
            self.slippages.append(fill.entry_slippage)
            self.tips_sol += fill.tip_lamports / LAMPORTS_PER_SOL
            self.priority_sol += fill.priority_lamports / LAMPORTS_PER_SOL
        elif fill.reason == "reverted_min_out":
            self.reverts += 1

    def add_pnl(self, gross_sol: float):
        self.gross_pnl_sol += gross_sol

    # ---- derived ----
    @property
    def land_rate(self):
        return self.lands / self.attempts if self.attempts else 0.0

    @property
    def mean_slot_delay(self):
        return sum(self.slot_delays) / len(self.slot_delays) if self.slot_delays else 0.0

    @property
    def mean_slippage(self):
        return sum(self.slippages) / len(self.slippages) if self.slippages else 0.0

    @property
    def rug_avoid_rate(self):
        total = self.rugs_faced + self.rugs_avoided
        return self.rugs_avoided / total if total else 0.0

    @property
    def costs_sol(self):
        return self.tips_sol + self.priority_sol

    @property
    def net_pnl_sol(self):
        return self.gross_pnl_sol - self.costs_sol

    @property
    def tip_efficiency(self):
        # tips as a fraction of gross profit (lower is better; >1 = bleeding)
        return self.tips_sol / self.gross_pnl_sol if self.gross_pnl_sol > 0 else float("inf")

    def render(self) -> str:
        return (
            f"  candidates (sniped)      {self.candidates}\n"
            f"  attempts / lands         {self.attempts} / {self.lands}  "
            f"(land rate {self.land_rate:6.1%})\n"
            f"  min-out reverts          {self.reverts}  (bad fills avoided)\n"
            f"  mean slot delay          {self.mean_slot_delay:5.2f} slots "
            f"(~{self.mean_slot_delay*0.4:.2f}s behind LP-add)\n"
            f"  mean entry slippage      {self.mean_slippage:6.1%}\n"
            f"  rug-avoidance rate       {self.rug_avoid_rate:6.1%}  "
            f"(avoided {self.rugs_avoided}, ate {self.rugs_faced})\n"
            f"  gross PnL                {self.gross_pnl_sol:+8.2f} SOL\n"
            f"  tips + priority          {self.costs_sol:8.2f} SOL\n"
            f"  NET PnL (after costs)    {self.net_pnl_sol:+8.2f} SOL\n"
            f"  tip efficiency           {self.tip_efficiency:6.2f}  "
            f"(tips/gross; >1.0 = subsidizing validators)\n"
        )
