"""LatencyTracer — per-hop instrumentation for the snipe→submit path.

Implements the C-1 mandate from latency-budget.md: every hop is tagged with a
CLASS, and the three classes are kept SEPARATE so the ledger never lets the
internal-compute number masquerade as a landing time.

  INTERNAL   — our code (ingress_detect, decode, gate, decide, build_sign,
               submit_call).  Compressible only by ShredStream (parity, not edge).
  SUBMISSION — network to the block engine (block_engine_rtt).  Compressible
               by co-location only — an INFRA spend, not a code change.
  LEADER_LAND — the staked-lane hop (leader_land).  Irreducible for a solo desk.

Spans are measured on a MONOTONIC clock (never affected by NTP steps), but each
trace is ANCHORED to EVENT-TIME (the LP-add/migrate slot + block_time_ms), so
the latency ledger is point-in-time honest (C-5): a span is "30ms after the
event", not "30ms after some wall clock".

The tracer aggregates p50/p95/p99 per hop and emits the ledger to Prometheus via
the existing `AATSMetrics` (telemetry/metrics.py).  Internal vs submission are
emitted to SEPARATE histograms (the dashboard surfaces them in separate columns,
AC-050) — they are never summed into one "latency" number.

No money here; durations are integer microseconds internally, floats only at the
percentile/seconds boundary (ratios/durations are not money — exact-money rule
does not apply to clock readings).
"""
from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import StrEnum

logger = logging.getLogger(__name__)


class HopClass(StrEnum):
    """The C-1 three-class taxonomy — the split that keeps the ledger honest."""

    INTERNAL = "INTERNAL"  # our compute (ingress->submit_call)
    SUBMISSION = "SUBMISSION"  # network RTT to the block engine
    LEADER_LAND = "LEADER_LAND"  # staked-lane hop — irreducible for a solo desk


# Canonical hop names + their class, in order (latency-budget.md §2).
# This is the authoritative ledger order.  A hop name not in this table is
# rejected (typo guard) so the ledger can never silently grow an unclassed hop.
HOP_CLASS: dict[str, HopClass] = {
    "ingress_detect": HopClass.INTERNAL,
    "decode": HopClass.INTERNAL,
    "gate": HopClass.INTERNAL,
    "decide": HopClass.INTERNAL,
    "build_sign": HopClass.INTERNAL,
    "submit_call": HopClass.INTERNAL,
    "block_engine_rtt": HopClass.SUBMISSION,
    "leader_land": HopClass.LEADER_LAND,
}

# The end of INTERNAL COMPUTE (ingress->submit_call).  Used to assert the
# internal total is reported separately from RTT/leader-land.
_INTERNAL_HOPS = tuple(h for h, c in HOP_CLASS.items() if c is HopClass.INTERNAL)


def _now_us() -> int:
    """Monotonic clock in integer microseconds (immune to NTP steps)."""
    return time.perf_counter_ns() // 1_000


@dataclass
class HopSpan:
    """A single measured hop within one trace."""

    name: str
    hop_class: HopClass
    duration_us: int


@dataclass
class LatencyTrace:
    """One end-to-end trace, anchored to EVENT-TIME (the LP-add/migrate slot).

    `event_slot` / `event_block_time_ms` are the point-in-time anchor (C-5).
    `wall_clock_ms` is carried for monitoring ONLY and is never a join/sort key
    (mirrors EventTime's contract).
    """

    event_slot: int
    event_block_time_ms: int
    wall_clock_ms: int
    spans: list[HopSpan] = field(default_factory=list)

    def _add(self, name: str, duration_us: int) -> None:
        if name not in HOP_CLASS:
            raise ValueError(
                f"Unknown hop '{name}'. Add it to HOP_CLASS with its C-1 class "
                f"before tracing it (no unclassed hop in the ledger)."
            )
        if duration_us < 0:
            raise ValueError(f"hop '{name}' duration must be >= 0us, got {duration_us}")
        self.spans.append(
            HopSpan(name=name, hop_class=HOP_CLASS[name], duration_us=duration_us)
        )

    @contextmanager
    def hop(self, name: str) -> Iterator[None]:
        """Time a hop with a monotonic span.

        Usage:
            with trace.hop("decode"):
                decode_pool_keys(...)
        """
        start = _now_us()
        try:
            yield
        finally:
            self._add(name, _now_us() - start)

    def record_hop_us(self, name: str, duration_us: int) -> None:
        """Record a hop whose duration was measured elsewhere (e.g. replayed
        from a recorded liquidity event, or a network RTT measured by the RPC
        client).  Integer microseconds in."""
        self._add(name, int(duration_us))

    # ---- per-trace rollups (kept SEPARATE per C-1) ----

    def internal_total_us(self) -> int:
        """Sum of INTERNAL hops only (ingress->submit_call).  This is the ONLY
        thing BRIEF §7.2's '20-70ms' describes — never a landing time."""
        return sum(s.duration_us for s in self.spans if s.hop_class is HopClass.INTERNAL)

    def submission_rtt_us(self) -> int:
        """Sum of SUBMISSION hops (block-engine RTT).  Reported SEPARATELY."""
        return sum(s.duration_us for s in self.spans if s.hop_class is HopClass.SUBMISSION)

    def leader_land_us(self) -> int:
        """Sum of LEADER_LAND hops (staked-lane).  Reported SEPARATELY."""
        return sum(s.duration_us for s in self.spans if s.hop_class is HopClass.LEADER_LAND)


class LatencyTracer:
    """Aggregates traces into per-hop p50/p95/p99 and emits the ledger.

    INJECTABLE metrics sink: pass an `AATSMetrics` (or any object exposing the
    histogram observers used below).  When `metrics=None` the tracer still
    aggregates percentiles (so it is usable offline / in tests without
    prometheus); only the emit step is skipped.

    The tracer keeps the raw per-hop samples so it can compute true percentiles
    on demand and prove the budget against a recorded/replayed event in CI.
    """

    def __init__(self, metrics: object | None = None) -> None:
        self._metrics = metrics
        # hop_name -> list of durations (us)
        self._samples: dict[str, list[int]] = {h: [] for h in HOP_CLASS}
        # class rollups
        self._internal_totals_us: list[int] = []
        self._submission_us: list[int] = []
        self._leader_land_us: list[int] = []

    def start_trace(
        self, *, event_slot: int, event_block_time_ms: int
    ) -> LatencyTrace:
        """Open a trace anchored to EVENT-TIME (point-in-time, C-5)."""
        return LatencyTrace(
            event_slot=event_slot,
            event_block_time_ms=event_block_time_ms,
            wall_clock_ms=int(time.time() * 1000),  # monitoring ONLY, never a join key
        )

    def record(self, trace: LatencyTrace) -> None:
        """Fold a completed trace into the aggregates and emit it to telemetry."""
        for span in trace.spans:
            self._samples[span.name].append(span.duration_us)
        self._internal_totals_us.append(trace.internal_total_us())
        self._submission_us.append(trace.submission_rtt_us())
        self._leader_land_us.append(trace.leader_land_us())
        self._emit(trace)

    # ---- emit to Prometheus (separate internal vs submission columns) ----

    def _emit(self, trace: LatencyTrace) -> None:
        if self._metrics is None:
            return
        m = self._metrics
        try:
            # INTERNAL total -> the snipe-decision histogram (internal compute only).
            internal_s = trace.internal_total_us() / 1_000_000.0
            hist = getattr(m, "snipe_decision_latency_seconds", None)
            if hist is not None:
                hist.observe(internal_s)
            # SUBMISSION RTT -> the time-to-land histogram (explicitly separate, C-1).
            sub_s = trace.submission_rtt_us() / 1_000_000.0
            ttl = getattr(m, "time_to_land_seconds", None)
            if ttl is not None and sub_s > 0:
                ttl.observe(sub_s)
        except Exception:  # noqa: BLE001 — telemetry must never break the hot path
            logger.exception("latency_tracer.emit_failed")

    # ---- percentile ledger (proven, not asserted) ----

    def ledger(self) -> dict[str, object]:
        """Return the per-hop p50/p95/p99 ledger plus the C-1 class rollups.

        Shape:
            {
              "hops": { "<hop>": {"class": ..., "p50_ms": .., "p95_ms": .., "p99_ms": .., "n": ..}},
              "rollups": {
                 "internal_total": {"p50_ms":..,"p95_ms":..,"p99_ms":..},
                 "submission_rtt": {...},
                 "leader_land": {...},
              }
            }
        Durations are reported in ms (float) at this boundary — these are clock
        readings, not money, so float is fine here.
        """
        hops: dict[str, dict[str, object]] = {}
        for hop, cls in HOP_CLASS.items():
            samples = self._samples[hop]
            hops[hop] = {
                "class": cls.value,
                "p50_ms": _pctl_ms(samples, 50),
                "p95_ms": _pctl_ms(samples, 95),
                "p99_ms": _pctl_ms(samples, 99),
                "n": len(samples),
            }
        return {
            "hops": hops,
            "rollups": {
                "internal_total": _rollup_ms(self._internal_totals_us),
                "submission_rtt": _rollup_ms(self._submission_us),
                "leader_land": _rollup_ms(self._leader_land_us),
            },
        }


# ---------------------------------------------------------------------------
# Percentile helpers — nearest-rank, no numpy dependency.
# ---------------------------------------------------------------------------


def _pctl_us(samples: list[int], pct: int) -> int:
    """Nearest-rank percentile in microseconds; 0 for empty."""
    if not samples:
        return 0
    ordered = sorted(samples)
    # nearest-rank: ceil(pct/100 * n) - 1, clamped
    rank = (pct * len(ordered) + 99) // 100  # ceil(pct*n/100)
    idx = min(max(rank - 1, 0), len(ordered) - 1)
    return ordered[idx]


def _pctl_ms(samples: list[int], pct: int) -> float:
    """Nearest-rank percentile in ms (float at the reporting boundary)."""
    return round(_pctl_us(samples, pct) / 1000.0, 3)


def _rollup_ms(samples: list[int]) -> dict[str, float]:
    return {
        "p50_ms": _pctl_ms(samples, 50),
        "p95_ms": _pctl_ms(samples, 95),
        "p99_ms": _pctl_ms(samples, 99),
    }
