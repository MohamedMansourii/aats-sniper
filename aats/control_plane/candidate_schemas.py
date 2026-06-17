"""Candidate-queue schemas for GET /api/candidates (E3 additive read-only endpoint).

NOT in aats.contracts (frozen) — this is a control-plane-local view schema.

A candidate is any token the snipe loop evaluated but did NOT enter (status=skipped or
status=pending monitoring), plus tokens that were sniped (status=sniped).  The queue
is a bounded ring of the most-recent N evaluations, read-only, view-only.

HARD RULES:
  - Read-only.  No field that can trigger, size-up, or widen a stop.
  - No win_rate field anywhere (HONESTY CLAUSE, AC-037).
  - Money: model_p is float (probability, not money). sol_in_lamports would be int.
  - No secrets, keys, or PII.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator


class SafetyReport(BaseModel):
    """Structured pre-trade safety report for one evaluated candidate.

    Derived from the pretrade_gate reasons and screener output.
    gate_passed: True if the M4 pre-trade safety gate did not veto this candidate.
    reasons: the list of gate/screener PASS or REJECT codes for this candidate.
    """

    gate_passed: bool
    reasons: list[str] = Field(default_factory=list)

    model_config = {"frozen": True}


class CandidateRecord(BaseModel):
    """One candidate evaluated by the snipe loop — read-only operator view.

    status:
      "monitoring"  — evaluated but not yet at the snipe decision (signal pending)
      "pending"     — passed pre-checks; snipe outcome pending (rare race window)
      "skipped"     — evaluated and NOT entered; reason explains why
      "sniped"      — entry was submitted (landed or attempted)

    model_p: calibrated classifier probability in [0,1], or None if no signal was staged.
    reason: the SnipeSkipReason code if skipped; "fill_not_landed" if attempted but missed;
            "entered" if sniped; "no_signal" / "stale_signal" / etc.
    safety_report: the pre-trade gate verdict + structured reason codes.

    HONESTY CLAUSE: no win_rate field anywhere (AC-037).
    MONEY RULE: model_p is float (probability, not money). Any sol amounts would be
                integer lamports — this schema has none for candidates not yet entered.
    """

    mint: str
    evaluated_at_slot: int   # event-time slot of the LaunchEvent (C-5 event-time)
    evaluated_at_wall_ms: int  # wall-clock ms at evaluation (monotonic; for staleness only)
    status: Literal["monitoring", "pending", "skipped", "sniped"]
    model_p: float | None     # calibrated probability (null if no signal)
    reason: str               # SnipeSkipReason code or "entered"
    safety_report: SafetyReport

    model_config = {"frozen": True}

    @field_validator("model_p")
    @classmethod
    def _model_p_range(cls, v: float | None) -> float | None:
        if v is not None and not (0.0 <= v <= 1.0):
            raise ValueError(f"model_p must be in [0, 1] or None, got {v}")
        return v
