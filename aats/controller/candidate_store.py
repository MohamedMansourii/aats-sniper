"""M3 Controller — CandidateQueue: bounded ring of recently-evaluated candidates.

E3 additive feature.  The SNIPE loop writes one CandidateRecord per evaluated
LaunchEvent (regardless of outcome).  The control-plane reads the queue for
GET /api/candidates.

Design:
  - Bounded ring (maxlen=N, default 200): oldest entries evicted automatically.
  - Thread-safe: all mutations hold self._lock (RLock, same posture as StateStore).
  - WRITE PATH: snipe_loop.process_event() calls record() after each decision.
    This is a non-blocking append into a deque — no await, no Redis, no LLM.
  - READ PATH: the FastAPI GET /api/candidates handler calls snapshot() which
    returns a shallow copy of the deque as a list.  Read-only.  No mutation.

The CandidateQueue is INTENTIONALLY separate from the StateStore Protocol:
  - Extending the Protocol would cascade to every mock and impl.
  - The candidate queue is optional (if None, /api/candidates returns empty).
  - It has its own bounded-memory footprint contract (maxlen cap).

HARD RULES:
  - No money in this layer (no lamport fields in CandidateRecord — candidates
    not entered have no filled size).
  - No secrets.  No keys.
  - Write path is synchronous and non-blocking (FAST/SNIPE loop safe).
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any

# ---------------------------------------------------------------------------
# CandidateQueue
# ---------------------------------------------------------------------------

# Maximum records retained; older entries auto-evict.
DEFAULT_MAXLEN = 200


class CandidateQueue:
    """Thread-safe bounded ring of evaluated-candidate records.

    Each record is a plain dict matching the CandidateRecord schema in
    aats.control_plane.candidate_schemas — we use dicts here to keep the
    controller layer free of a dependency on the control-plane schema
    (direction: controller -> control_plane is the wrong direction for imports).
    The control-plane converts dicts to CandidateRecord at response time.

    Usage (SNIPE loop):
        q = CandidateQueue()
        # ... after each process_event() decision ...
        q.record(mint=mint, slot=slot, wall_ms=wall_ms, status="skipped",
                 model_p=signal.p_calibrated if signal else None,
                 reason=skip_reason,
                 gate_passed=False, gate_reasons=["safety_gate_fail"])

    Usage (control-plane GET /api/candidates):
        records = q.snapshot()   # list[dict], newest-first
    """

    def __init__(self, maxlen: int = DEFAULT_MAXLEN) -> None:
        self._lock = threading.RLock()
        # deque with maxlen: append pushes newest to right; popleft drops oldest.
        self._ring: deque[dict[str, Any]] = deque(maxlen=maxlen)

    def record(
        self,
        *,
        mint: str,
        evaluated_at_slot: int,
        evaluated_at_wall_ms: int | None = None,
        status: str,
        model_p: float | None,
        reason: str,
        gate_passed: bool,
        gate_reasons: list[str] | None = None,
    ) -> None:
        """Append one candidate evaluation record.

        Args:
            mint:                  The token mint address.
            evaluated_at_slot:     Event-time slot of the LaunchEvent (C-5 event-time).
            evaluated_at_wall_ms:  Wall-clock ms at evaluation; defaults to now().
            status:                One of "monitoring"|"pending"|"skipped"|"sniped".
            model_p:               Calibrated classifier p in [0,1] or None.
            reason:                SnipeSkipReason code or "entered".
            gate_passed:           True if the M4 safety gate did not veto.
            gate_reasons:          Structured gate reason codes.

        This method is synchronous and non-blocking.  It is safe to call from
        the SNIPE loop (no await, no Redis, no LLM call).
        """
        wall_ms = evaluated_at_wall_ms if evaluated_at_wall_ms is not None else (
            time.monotonic_ns() // 1_000_000
        )
        record: dict[str, Any] = {
            "mint": mint,
            "evaluated_at_slot": evaluated_at_slot,
            "evaluated_at_wall_ms": wall_ms,
            "status": status,
            "model_p": model_p,
            "reason": reason,
            "safety_report": {
                "gate_passed": gate_passed,
                "reasons": gate_reasons or [],
            },
        }
        with self._lock:
            self._ring.append(record)

    def snapshot(self, *, newest_first: bool = True) -> list[dict[str, Any]]:
        """Return a shallow copy of all current records.

        Args:
            newest_first: if True (default), sort newest (highest slot) first.

        Returns:
            list[dict] — safe to iterate outside the lock; each dict is a
            shallow copy of the internal record.

        The list is a new object; mutating it does not affect the ring.
        """
        with self._lock:
            items = list(self._ring)
        if newest_first:
            items.reverse()
        return items

    def __len__(self) -> int:
        with self._lock:
            return len(self._ring)
