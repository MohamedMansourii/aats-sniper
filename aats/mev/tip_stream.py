"""LIVE Jito tip stream — the injectable source of tip-floor percentiles.

This is the *only* place a tip number enters the system, and it is NEVER a
hardcoded constant in the hot path (AC-014: a hardcoded tip is a build-FAIL).
The TipController reads a percentile from this stream; the stream itself is
populated OFF the hot path (Redis KV cache fed by the Jito block-engine
`getTipAccounts` + tip-floor/percentile websocket — `latency-devops-engineer`
owns the real plumbing, infrastructure.md §4).

INJECTABLE + OFFLINE-SAFE (HARD RULE): the real block-engine endpoint is not
reachable in this environment. `TipStreamProtocol` is the seam; the real
endpoint plugs in at the marked site. `StaticTipStream` is a deterministic
offline fixture for tests/replay — it carries a recorded percentile snapshot
stamped to EVENT-TIME, never compute-time (point-in-time correctness, C-5).

Money: every tip quantity is int lamports. No float. Ever.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import BaseModel, model_validator

# ---------------------------------------------------------------------------
# The tip-floor snapshot — what the stream yields at a point in time.
# ---------------------------------------------------------------------------


class TipFloorSnapshot(BaseModel):
    """A point-in-time read of the Jito tip-floor percentile ladder.

    Mirrors the Jito `getTipFloor` / tip-percentile feed: a ladder of
    landed-bundle tip percentiles over a trailing window, plus the rotation
    set of tip accounts (getTipAccounts).

    EVENT-TIME STAMPED: `as_of_slot` / `as_of_block_time_ms` are the on-chain
    clock at which this snapshot was valid. The TipController must only ever
    use a snapshot whose `as_of_slot` is <= the decision slot — a snapshot
    from the future is a lookahead leak (C-5).  Money fields are int lamports.
    """

    # percentile ladder over recent LANDED bundle tips (integer lamports)
    p25_lamports: int
    p50_lamports: int
    p75_lamports: int
    p95_lamports: int
    # the rotation set of Jito tip accounts (getTipAccounts) — pubkeys only
    tip_accounts: tuple[str, ...]
    # POINT-IN-TIME anchor — the on-chain clock this snapshot was valid at
    as_of_slot: int
    as_of_block_time_ms: int

    model_config = {"frozen": True}

    @model_validator(mode="before")
    @classmethod
    def _reject_float_lamports(cls, data: object) -> object:
        """Reject float for any lamports field (data-models.md §0)."""
        if not isinstance(data, dict):
            return data
        for field in ("p25_lamports", "p50_lamports", "p75_lamports", "p95_lamports"):
            val = data.get(field)
            if isinstance(val, float):
                raise ValueError(
                    f"TipFloorSnapshot.{field} must be int (lamports), got float {val!r}. "
                    "Tips are integer lamports (data-models.md §0)."
                )
        return data

    @model_validator(mode="after")
    def _ladder_monotonic(self) -> TipFloorSnapshot:
        """Percentile ladder must be non-decreasing and non-negative."""
        ladder = (self.p25_lamports, self.p50_lamports, self.p75_lamports, self.p95_lamports)
        if any(x < 0 for x in ladder):
            raise ValueError(f"TipFloorSnapshot percentiles must be >= 0, got {ladder}")
        if not (ladder[0] <= ladder[1] <= ladder[2] <= ladder[3]):
            raise ValueError(
                f"TipFloorSnapshot percentile ladder must be non-decreasing, got {ladder}"
            )
        if not self.tip_accounts:
            raise ValueError("TipFloorSnapshot.tip_accounts must be non-empty (getTipAccounts).")
        return self

    def percentile(self, pct: int) -> int:
        """Return the tip-floor lamports at one of the supported percentiles.

        Supported: 25, 50, 75, 95.  Caller picks the percentile that matches the
        contention bucket (low/medium/high).  Integer lamports out.
        """
        table = {
            25: self.p25_lamports,
            50: self.p50_lamports,
            75: self.p75_lamports,
            95: self.p95_lamports,
        }
        if pct not in table:
            raise ValueError(f"Unsupported percentile {pct}; supported: {sorted(table)}")
        return table[pct]


# ---------------------------------------------------------------------------
# The injectable seam.
# ---------------------------------------------------------------------------


@runtime_checkable
class TipStreamProtocol(Protocol):
    """The injectable LIVE tip-stream seam.

    Production: a Redis-KV-backed reader fed OFF the hot path from the Jito
    block-engine `getTipAccounts` + tip-floor websocket (the real endpoint
    plugs in here — `latency-devops-engineer`).  The SNIPE loop reads the
    cached snapshot synchronously; there is NO network call in the hot path
    (latency-budget.md hop #4).

    Tests/replay: `StaticTipStream`, a deterministic offline fixture.
    """

    def current(self, *, decision_slot: int) -> TipFloorSnapshot:
        """Return the freshest tip-floor snapshot valid AT OR BEFORE decision_slot.

        Implementations MUST NOT return a snapshot from a slot in the future of
        `decision_slot` — that would be a lookahead leak (C-5).  Raise
        `StaleTipStreamError` if no point-in-time-valid snapshot exists.
        """
        ...


class StaleTipStreamError(RuntimeError):
    """Raised when no tip snapshot valid at-or-before the decision slot exists.

    The TipController treats this as do-not-submit: with no point-in-time tip
    floor we cannot price a competitive tip, and guessing a constant is the
    banned anti-pattern.
    """

    def __init__(self, decision_slot: int, freshest_slot: int | None) -> None:
        self.decision_slot = decision_slot
        self.freshest_slot = freshest_slot
        super().__init__(
            f"No tip-floor snapshot valid at-or-before decision_slot={decision_slot} "
            f"(freshest available={freshest_slot}). Refusing to price a tip from a "
            "future/absent snapshot (lookahead-safe, C-5)."
        )


# ---------------------------------------------------------------------------
# Deterministic offline fixture — the OFFLINE MOCK (no network).
# ---------------------------------------------------------------------------


class StaticTipStream:
    """A deterministic, point-in-time-correct offline tip stream for tests/replay.

    Holds a list of recorded snapshots ordered by `as_of_slot`.  `current()`
    returns the freshest snapshot whose `as_of_slot <= decision_slot` — i.e. it
    enforces point-in-time correctness in the fixture itself, so a test cannot
    accidentally read a future tip floor.

    This is where the REAL block-engine reader is swapped in for production;
    the contract (`TipStreamProtocol`) is identical.
    """

    def __init__(self, snapshots: list[TipFloorSnapshot]) -> None:
        if not snapshots:
            raise ValueError("StaticTipStream requires at least one snapshot.")
        # Sort ascending by event-time slot for deterministic point-in-time lookup.
        self._snapshots = sorted(snapshots, key=lambda s: s.as_of_slot)

    def current(self, *, decision_slot: int) -> TipFloorSnapshot:
        """Freshest snapshot with as_of_slot <= decision_slot (point-in-time, C-5)."""
        valid = [s for s in self._snapshots if s.as_of_slot <= decision_slot]
        if not valid:
            freshest = self._snapshots[0].as_of_slot if self._snapshots else None
            raise StaleTipStreamError(decision_slot=decision_slot, freshest_slot=freshest)
        # valid is sorted ascending; the last is the freshest at-or-before.
        return valid[-1]
