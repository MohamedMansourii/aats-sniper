"""Recorded-data completeness audit hooks (T-303, C-6, AC-006).

OWNERSHIP: data-ingestion-engineer (M1 / Lane A).

PURPOSE
-------
Reconcile the set of launches we RECORDED against an INDEPENDENT pool-create
census (a second, disjoint data source).  Compute and bound the miss rate.
Carry every census-known launch that we did NOT fully snapshot or label as
CENSORED — never drop it.

DESIGN PRINCIPLES
-----------------
1. Census source is INJECTABLE.  No live network is reached here.  Tests inject
   ``InMemoryCensusSource`` with deterministic fixtures.  Production wires a
   Geyser/on-chain census client behind the same ``CensusSource`` Protocol.

2. Survivorship-free by construction.  If a mint appears in the census but
   has no recorded snapshot (or has a CENSORED snapshot), the audit emits a
   ``CompletenessRow`` with ``status=CompletenessStatus.CENSORED`` and carries
   it into the output set.  It is NEVER silently dropped.

3. Miss-rate bounding.  The audit computes a POINT ESTIMATE and an asymptotic
   UPPER BOUND (using the Wilson score interval at the declared confidence
   level).  Both are reported.  The bound, not the point estimate, is compared
   against the declared max miss rate.

4. Point-in-time correctness.  ``event_time`` (on-chain slot + block_time_ms)
   is the only time that matters.  ``recorded_at_ms`` is monitoring-only and
   never used as a join anchor.

5. No truth_* fields, no win_rate, no label writes.  This module is read-side
   M1 only.  ``LaunchOutcome`` labels are written exclusively by the clean-room
   harness (T-400/T-401).

DATA FLOW
---------
  CensusSource.pool_creates_in_slot_range(start, end)
      → list[CensusRecord]            (independent source)

  ShadowRecorder.completed_snapshots()
      → list[dict]                    (our recorded data)

  CompletenessAuditor.reconcile(census_records, snapshot_rows)
      → AuditReport                   (miss rate, rows, CENSORED list)

MONEY RULE (data-models.md §0):
  All lamport / base-unit fields are int.  The miss_rate and bound are float
  (they are statistical quantities, not monetary).  There is no Decimal here
  because nothing is priced.

HARD RULES (non-waivable):
  - REAL network not reachable from here.  Census source is INJECTABLE.
  - No secrets in code.  External URLs/keys come from env.
  - CENSORED rows NEVER dropped.
  - No feature math.  Raw completeness accounting only.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# CompletenessStatus — mirrors data-models.md §7 / Position.completeness_status
# ---------------------------------------------------------------------------


class CompletenessStatus(StrEnum):
    """Whether a census-known launch has a complete recorded snapshot.

    COMPLETE  — The launch has a full first-K-slot snapshot in our store.
    CENSORED  — The launch appeared in the census but we did NOT record a
                complete snapshot (stream dropout, overflow, or the mint was
                never seen by our transport).  Carried, NEVER dropped (C-6).
    """

    COMPLETE = "complete"
    CENSORED = "CENSORED"


# ---------------------------------------------------------------------------
# CensusRecord — a single entry from the independent pool-create census
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CensusRecord:
    """One pool-create event from the INDEPENDENT census source (C-6).

    The census is a second, disjoint data source (e.g. a separate Geyser
    subscription, a Solscan/Helius scrape, or an on-chain program-log scan)
    that records ALL pool-create transactions in a slot range — including ones
    our primary ingest may have missed.

    Attributes:
        mint:              Token mint address.
        pool_create_slot:  On-chain slot of the pool-create transaction.
        pool_create_block_time_ms:  On-chain block_time (ms) at pool-create.
                           This is the AUTHORITATIVE event-time anchor (C-5).
        source:            Human-readable label for the census source
                           (e.g. "geyser_secondary", "helius_log_scan").
    """

    mint: str
    pool_create_slot: int
    pool_create_block_time_ms: int  # authoritative on-chain time (C-5)
    source: str = "census"


# ---------------------------------------------------------------------------
# CensusSource Protocol — injectable, no live network
# ---------------------------------------------------------------------------


@runtime_checkable
class CensusSource(Protocol):
    """Protocol for an injectable pool-create census source (C-6).

    Production:  a GeyserCensusClient that streams ALL pool-creates in the
                 slot range via a SECONDARY Geyser subscription (independent
                 of the primary ingest).
    Tests:       InMemoryCensusSource with deterministic fixtures.

    The protocol returns CensusRecord objects so the auditor is decoupled from
    the transport implementation.
    """

    def pool_creates_in_slot_range(
        self,
        start_slot: int,
        end_slot: int,
    ) -> list[CensusRecord]:
        """Return all pool-create events in [start_slot, end_slot] (inclusive).

        The implementation MUST be survivorship-free: it must include EVERY
        pool-create it observed, even if our primary ingest missed it.

        Args:
            start_slot: Inclusive lower bound.
            end_slot:   Inclusive upper bound.

        Returns:
            List of CensusRecord, possibly empty.
        """
        ...


# ---------------------------------------------------------------------------
# InMemoryCensusSource — deterministic offline fixture for tests
# ---------------------------------------------------------------------------


class InMemoryCensusSource:
    """Offline census source for tests.  No network; fully injectable.

    Usage:
        source = InMemoryCensusSource([
            CensusRecord(mint="MintA...", pool_create_slot=100, ...),
            CensusRecord(mint="MintB...", pool_create_slot=200, ...),
        ])
        records = source.pool_creates_in_slot_range(50, 300)
    """

    def __init__(self, records: list[CensusRecord]) -> None:
        self._records = list(records)

    def pool_creates_in_slot_range(
        self,
        start_slot: int,
        end_slot: int,
    ) -> list[CensusRecord]:
        """Return records whose pool_create_slot is in [start, end]."""
        return [
            r
            for r in self._records
            if start_slot <= r.pool_create_slot <= end_slot
        ]

    def add(self, record: CensusRecord) -> None:
        """Add a record (convenience for incremental test construction)."""
        self._records.append(record)

    def __len__(self) -> int:
        return len(self._records)


# ---------------------------------------------------------------------------
# CompletenessRow — one row in the audit output (per census mint)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CompletenessRow:
    """One row in the completeness audit output.

    Carries the census record alongside the reconciliation result.
    Rows with ``status=CENSORED`` are NOT dropped — they are the mechanism
    by which survivorship-free accounting is achieved (C-6, AC-006,
    data-models.md §7).

    Attributes:
        census_record:     The original CensusRecord from the independent source.
        status:            COMPLETE if a full snapshot exists; CENSORED otherwise.
        snapshot_event_count: Number of LaunchEvents in the recorded snapshot
                              (0 for CENSORED rows — they have no snapshot).
        snapshot_completeness_status:  The raw completeness_status string from
                              the shadow_snapshots table ("complete" / "CENSORED"
                              / None for mints never seen by our ingest).
        miss_reason:       Human-readable reason for CENSORED status.  None if
                           COMPLETE.
    """

    census_record: CensusRecord
    status: CompletenessStatus
    snapshot_event_count: int
    snapshot_completeness_status: str | None
    miss_reason: str | None = None


# ---------------------------------------------------------------------------
# AuditReport — the output of a reconciliation run
# ---------------------------------------------------------------------------


@dataclass
class AuditReport:
    """Output of CompletenessAuditor.reconcile().

    Attributes:
        census_total:         Total pool-creates in the census for the slot range.
        recorded_complete:    Count of census mints with a COMPLETE snapshot.
        censored_count:       Count of census mints carried as CENSORED.
        miss_count:           = censored_count (aliases for clarity).
        miss_rate_point:      Point estimate: censored_count / census_total.
        miss_rate_upper_bound: Upper bound of the Wilson score CI at the declared
                               confidence level (asymmetric — the upper end).
        confidence_level:     The declared CI confidence level (e.g. 0.95).
        declared_max_miss_rate: The operator-declared maximum acceptable miss rate
                               (e.g. 0.05 for 5%).
        gate_passes:          True iff miss_rate_upper_bound <= declared_max_miss_rate.
        rows:                 Full list of CompletenessRow (one per census mint).
                              CENSORED rows are INCLUDED — never dropped.
        slot_range_start:     The audited slot range start.
        slot_range_end:       The audited slot range end.
        audit_recorded_at_ms: Wall-clock ms when this report was computed
                              (monitoring only — NOT a join anchor).
    """

    census_total: int
    recorded_complete: int
    censored_count: int
    miss_rate_point: float
    miss_rate_upper_bound: float
    confidence_level: float
    declared_max_miss_rate: float
    gate_passes: bool
    rows: list[CompletenessRow]
    slot_range_start: int
    slot_range_end: int
    audit_recorded_at_ms: int

    @property
    def miss_count(self) -> int:
        """Alias: miss_count == censored_count."""
        return self.censored_count

    @property
    def coverage_fraction(self) -> float:
        """(complete + CENSORED) / census_total.

        This is always 1.0 when the audit is survivorship-free: every census
        mint appears in `rows` either as COMPLETE or CENSORED.
        """
        if self.census_total == 0:
            return 1.0
        return (self.recorded_complete + self.censored_count) / self.census_total

    def censored_rows(self) -> list[CompletenessRow]:
        """Return only the CENSORED rows (for downstream analysis)."""
        return [r for r in self.rows if r.status == CompletenessStatus.CENSORED]

    def complete_rows(self) -> list[CompletenessRow]:
        """Return only the COMPLETE rows."""
        return [r for r in self.rows if r.status == CompletenessStatus.COMPLETE]

    def summary(self) -> str:
        """Human-readable one-liner for logs."""
        return (
            f"CompletenessAudit slots=[{self.slot_range_start},{self.slot_range_end}] "
            f"census={self.census_total} complete={self.recorded_complete} "
            f"censored={self.censored_count} "
            f"miss_rate_point={self.miss_rate_point:.4f} "
            f"miss_rate_upper={self.miss_rate_upper_bound:.4f}@{self.confidence_level:.2f} "
            f"declared_max={self.declared_max_miss_rate:.4f} "
            f"gate={'PASS' if self.gate_passes else 'FAIL'}"
        )


# ---------------------------------------------------------------------------
# Wilson score CI upper bound — the statistical bounding function
# ---------------------------------------------------------------------------


def _wilson_upper_bound(n_miss: int, n_total: int, confidence: float = 0.95) -> float:
    """Wilson score interval UPPER bound for a proportion.

    Used to compute a statistically sound UPPER BOUND on the miss rate.
    We compare this upper bound (not the point estimate) against the declared
    max miss rate, so the gate is conservative: if the upper bound still fits
    below the declared max, we have strong evidence the true miss rate is below it.

    NOTE: The Wilson CI upper bound is positive even when n_miss == 0 and
    n_total > 0.  This is mathematically correct: observing zero misses in N
    trials does not mean the true miss rate is zero; it means we have an upper
    bound of roughly z^2 / (n + z^2) at the declared confidence level.  The
    gate should be configured with a declared_max_miss_rate that accounts for
    this — e.g., for N=1000 launches and 0 misses the 95% CI upper bound is
    approximately 0.4%.

    Args:
        n_miss:     Number of misses (censored launches).
        n_total:    Total census size.
        confidence: Confidence level (default 0.95 → z ≈ 1.96).

    Returns:
        The upper bound of the Wilson score CI for the miss proportion.
        Returns 1.0 if n_total == 0 (no information → worst case, fail-closed).
        Returns a small positive value if n_miss == 0 (see note above).
    """
    if n_total == 0:
        return 1.0  # no data → worst case (fail-closed)

    p_hat = n_miss / n_total

    # z-score for confidence level (two-sided, upper tail)
    # Using a lookup for common values to avoid scipy dependency
    _Z_LOOKUP = {
        0.90: 1.645,
        0.95: 1.960,
        0.99: 2.576,
    }
    z = _Z_LOOKUP.get(confidence, 1.960)  # default to 95%

    # Wilson score interval formula
    # p_upper = (p_hat + z^2/(2n) + z*sqrt(p_hat(1-p_hat)/n + z^2/(4n^2))) / (1 + z^2/n)
    n = n_total
    z2 = z * z
    centre = p_hat + z2 / (2 * n)
    margin = z * math.sqrt(p_hat * (1 - p_hat) / n + z2 / (4 * n * n))
    denominator = 1 + z2 / n
    upper = (centre + margin) / denominator
    return min(1.0, upper)


# ---------------------------------------------------------------------------
# _normalize_snapshot_row — handle two input shapes for snapshot rows
# ---------------------------------------------------------------------------


def _normalize_snapshot_row(row: dict) -> dict:
    """Normalize a snapshot row to always have top-level completeness fields.

    ShadowRecorder.completed_snapshots() returns PointInTimeRecord.to_row()
    dicts, which store the rich snapshot dict inside ``payload_json``.  This
    helper unwraps it so the reconciliation logic sees a uniform shape with
    top-level ``completeness_status`` and ``event_count`` keys.

    Two input shapes supported:
      (a) Rich dict (from tests / direct callers) — already has top-level keys.
      (b) PointInTimeRecord.to_row() dict — has ``payload_json`` containing
          a JSON string with the rich dict.

    Returns a new dict (original unmodified).
    """
    if "completeness_status" in row:
        return row  # shape (a) — already normalized

    # shape (b): payload_json contains the rich row
    payload_json = row.get("payload_json")
    if payload_json:
        try:
            inner = json.loads(payload_json)
            # Merge inner into a copy of the outer row (inner takes precedence)
            merged = dict(row)
            merged.update(inner)
            return merged
        except Exception:
            pass  # fall through to return as-is

    return row  # fallback: return as-is (completeness_status will be missing → CENSORED)


# ---------------------------------------------------------------------------
# CompletenessAuditor — the main reconciliation engine
# ---------------------------------------------------------------------------


@dataclass
class CompletenessAuditor:
    """Reconciles recorded shadow snapshots against an independent census (C-6).

    Construction:
        auditor = CompletenessAuditor(
            census_source=InMemoryCensusSource([...]),  # injectable
            declared_max_miss_rate=0.05,                # 5% max miss
            confidence_level=0.95,                      # Wilson CI level
        )

    Usage:
        snapshot_rows = recorder.completed_snapshots()   # from ShadowRecorder
        report = auditor.reconcile(
            snapshot_rows=snapshot_rows,
            slot_range_start=100_000,
            slot_range_end=200_000,
        )
        logger.info(report.summary())
        assert report.gate_passes, f"Completeness gate FAIL: {report.summary()}"

    The auditor is STATELESS — call reconcile() as often as needed.

    Point-in-time correctness:
      The reconciliation joins on ``mint`` only.  The slot-range filter is
      applied to the CENSUS (pool_create_slot) to bound the scope.  Snapshot
      rows are matched on mint.  The result is point-in-time correct because
      the census is authoritative on WHAT existed; our snapshots are
      authoritative on WHAT WE RECORDED.

    Survivorship:
      Every census mint appears in ``report.rows`` exactly once.
      Census mints we missed are CompletenessStatus.CENSORED.
      They are NEVER dropped.  The assertion:

          (complete + CENSORED) / census_total >= 1 - declared_max_miss_rate

      is verified by ``report.gate_passes``.
    """

    census_source: CensusSource
    declared_max_miss_rate: float = 0.05  # 5% default; operator-configurable
    confidence_level: float = 0.95

    def reconcile(
        self,
        snapshot_rows: list[dict],
        slot_range_start: int,
        slot_range_end: int,
        now_ms: int | None = None,
    ) -> AuditReport:
        """Reconcile snapshot_rows against the census for the slot range.

        Args:
            snapshot_rows:    Rows from ShadowRecorder.completed_snapshots().
                              Each row must have keys: ``mint``,
                              ``completeness_status`` ("complete"/"CENSORED"),
                              ``event_count``, ``event_slot``.
            slot_range_start: Inclusive start slot for census query.
            slot_range_end:   Inclusive end slot for census query.
            now_ms:           Wall-clock ms at audit time (optional; defaults to
                              current time).  Monitoring only — NOT a join anchor.

        Returns:
            AuditReport with full row-level detail, miss rate, and gate verdict.
        """
        import time as _time

        if now_ms is None:
            now_ms = int(_time.time() * 1_000)

        # --- Fetch census ---
        census_records = self.census_source.pool_creates_in_slot_range(
            slot_range_start, slot_range_end
        )
        census_total = len(census_records)

        # --- Build snapshot index (mint → best snapshot row) ---
        # A mint may have multiple snapshot rows (e.g. CENSORED + later complete).
        # We prefer "complete" over "CENSORED".
        #
        # Two input shapes are accepted:
        #   (a) Direct dict from _snapshot_row() helpers (tests / direct callers)
        #       with top-level "completeness_status" and "event_count" keys.
        #   (b) PointInTimeRecord.to_row() from ShadowRecorder.completed_snapshots()
        #       which wraps the rich snapshot dict inside "payload_json".
        #       In this case we parse "payload_json" to extract the fields.
        snap_index: dict[str, dict] = {}
        for row in snapshot_rows:
            row = _normalize_snapshot_row(row)
            mint = row.get("mint")
            if mint is None:
                continue  # pending_events rows have no mint; skip
            existing = snap_index.get(mint)
            if existing is None:
                snap_index[mint] = row
            else:
                # Prefer "complete" status
                if (
                    row.get("completeness_status") == "complete"
                    and existing.get("completeness_status") != "complete"
                ):
                    snap_index[mint] = row

        # --- Reconcile each census record ---
        result_rows: list[CompletenessRow] = []
        recorded_complete = 0
        censored_count = 0

        for cr in census_records:
            snap = snap_index.get(cr.mint)

            if snap is not None and snap.get("completeness_status") == "complete":
                status = CompletenessStatus.COMPLETE
                event_count = snap.get("event_count", 0)
                snap_cs = snap.get("completeness_status")
                miss_reason = None
                recorded_complete += 1
            elif snap is not None and snap.get("completeness_status") == "CENSORED":
                # We saw it, but the snapshot is censored (stream dropout/overflow)
                status = CompletenessStatus.CENSORED
                event_count = snap.get("event_count", 0)
                snap_cs = snap.get("completeness_status")
                miss_reason = "snapshot_censored_stream_dropout_or_overflow"
                censored_count += 1
            else:
                # Never seen at all — transport missed it entirely
                status = CompletenessStatus.CENSORED
                event_count = 0
                snap_cs = None
                miss_reason = "mint_not_observed_by_primary_transport"
                censored_count += 1

            result_rows.append(
                CompletenessRow(
                    census_record=cr,
                    status=status,
                    snapshot_event_count=event_count,
                    snapshot_completeness_status=snap_cs,
                    miss_reason=miss_reason,
                )
            )

        # --- Compute miss rate + Wilson upper bound ---
        if census_total == 0:
            miss_rate_point = 0.0
            miss_rate_upper = 0.0
        else:
            miss_rate_point = censored_count / census_total
            miss_rate_upper = _wilson_upper_bound(
                n_miss=censored_count,
                n_total=census_total,
                confidence=self.confidence_level,
            )

        # --- Gate: upper bound <= declared max ---
        gate_passes = miss_rate_upper <= self.declared_max_miss_rate

        report = AuditReport(
            census_total=census_total,
            recorded_complete=recorded_complete,
            censored_count=censored_count,
            miss_rate_point=miss_rate_point,
            miss_rate_upper_bound=miss_rate_upper,
            confidence_level=self.confidence_level,
            declared_max_miss_rate=self.declared_max_miss_rate,
            gate_passes=gate_passes,
            rows=result_rows,
            slot_range_start=slot_range_start,
            slot_range_end=slot_range_end,
            audit_recorded_at_ms=now_ms,
        )

        logger.info(report.summary())
        if not gate_passes:
            logger.warning(
                "CompletenessAudit GATE FAIL: miss_rate_upper=%.4f > declared_max=%.4f "
                "(C-6, AC-006).  Censored mints: %s",
                miss_rate_upper,
                self.declared_max_miss_rate,
                [r.census_record.mint for r in report.censored_rows()],
            )

        return report

    def annotate_snapshot_rows(
        self,
        snapshot_rows: list[dict],
        slot_range_start: int,
        slot_range_end: int,
    ) -> list[dict]:
        """Return snapshot_rows annotated with completeness_status from census.

        For each snapshot row:
          - If the mint appears in the census AND has a complete snapshot →
            ``completeness_status`` stays "complete".
          - If the mint appears in the census AND snapshot is CENSORED →
            ``completeness_status`` stays "CENSORED".
          - If the mint does NOT appear in the census (e.g. it is outside
            the slot range) → ``completeness_status`` is left unchanged.
          - Census mints with NO snapshot row are NOT added here; they appear
            in the full AuditReport.rows instead.

        This is a convenience method for writing annotated rows back to the
        point-in-time store.  It does NOT replace reconcile().

        Args:
            snapshot_rows:    Rows from ShadowRecorder.completed_snapshots().
            slot_range_start: Census slot range start.
            slot_range_end:   Census slot range end.

        Returns:
            Annotated copy of snapshot_rows (new list; original unmodified).
        """
        census_records = self.census_source.pool_creates_in_slot_range(
            slot_range_start, slot_range_end
        )
        census_mints: set[str] = {cr.mint for cr in census_records}

        annotated = []
        for row in snapshot_rows:
            new_row = dict(row)
            mint = new_row.get("mint")
            if mint and mint in census_mints:
                # Already has a completeness_status from ShadowRecorder;
                # we do not override it — the annotation confirms census membership.
                new_row["in_census"] = True
            else:
                new_row["in_census"] = False
            annotated.append(new_row)
        return annotated
