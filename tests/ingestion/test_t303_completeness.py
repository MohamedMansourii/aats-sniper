"""T-303: Recorded-data completeness audit hooks.

C-6 (AC-006): reconcile recorded launches against an INDEPENDENT pool-create
census; bound + report the miss rate; carry un-snapshotted/un-labeled tokens
as CENSORED (never drop them — survivorship-free).

Test inventory
--------------
 1.  Empty census → zero miss rate, gate passes, zero rows.
 2.  All launches complete → miss_rate_point=0, miss_rate_upper small, gate passes.
 3.  All launches censored → miss_rate_point=1.0, gate fails.
 4.  Partial miss: census=10, complete=8, censored=2 — miss rate bounded + reported.
 5.  CENSORED rows are NEVER dropped from AuditReport.rows (survivorship-free).
 6.  Gate fails when miss_rate_upper_bound > declared_max_miss_rate.
 7.  Gate passes when miss_rate_upper_bound <= declared_max_miss_rate.
 8.  (coverage_fraction) = (complete+censored)/census_total == 1.0 always.
 9.  Wilson upper bound > point estimate for non-zero miss (conservative).
10.  Wilson upper bound monotonically increases with miss count (ceteris paribus).
11.  InMemoryCensusSource slot-range filter works correctly.
12.  Mint not in census snapshot → not a miss (out of scope).
13.  Snapshot CENSORED (stream dropout) → carried as CENSORED in report.
14.  Snapshot COMPLETE → carried as COMPLETE in report.
15.  Mint in census but NEVER seen by ingest → CENSORED, reason=mint_not_observed.
16.  Mint seen by ingest CENSORED (overflow) → CENSORED, reason=snapshot_censored.
17.  annotate_snapshot_rows adds in_census=True/False correctly.
18.  Multiple snapshot rows for same mint: prefer "complete" over "CENSORED".
19.  census_total=0 → coverage_fraction=1.0 (no data → no miss claim).
20.  census_total=1, censored=1 → Wilson upper bound = 1.0 (worst case).
21.  Point-in-time: audit_recorded_at_ms is wall-clock (monitoring only, not event-time).
22.  report.summary() contains required fields (mint rate, gate verdict).
23.  CompletenessAuditor is stateless: two reconcile() calls with same input → same output.
24.  CENSORED-only census entry appears in censored_rows(), not complete_rows().
25.  ShadowRecorder integration: on_reconnect → CENSORED snapshot → audit marks CENSORED.
26.  Mutation test: if we drop CENSORED rows from result_rows, coverage_fraction < 1.0 → FAIL.
27.  Large census (N=1000): all complete → miss_rate_upper bounded below 0.01 (Wilson).
28.  Large census (N=1000, 50 misses): upper bound ~ 6.8% (Wilson sanity check).
29.  AuditReport.miss_count property == censored_count.
30.  InMemoryCensusSource.add() increments len correctly.

HARD RULES VERIFIED
-------------------
  - No real network calls (all offline fixtures).
  - No secrets/keys in code or fixtures.
  - No float money (all lamport fields are int).
  - No truth_* / win_rate fields.
  - DRY_RUN implicit: this module is read-side only.
  - CENSORED rows NEVER dropped (tested by mutation proxy in test 26).
"""

from __future__ import annotations

import time

from aats.contracts.events import DetectionTransport, EventTime, LaunchEvent, LaunchSource
from aats.ingestion.completeness import (
    CensusRecord,
    CompletenessAuditor,
    CompletenessStatus,
    InMemoryCensusSource,
    _wilson_upper_bound,
)
from aats.ingestion.store import InMemoryParquetBackend, ShadowRecorder

# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

_SLOT_BASE = 300_000_000
_BLOCK_TIME_BASE_MS = 1_718_700_000_000  # 2024-06-18T00:00:00 UTC


def _census_record(mint: str, slot_offset: int = 0) -> CensusRecord:
    return CensusRecord(
        mint=mint,
        pool_create_slot=_SLOT_BASE + slot_offset,
        pool_create_block_time_ms=_BLOCK_TIME_BASE_MS + slot_offset * 400,
        source="test_fixture",
    )


def _snapshot_row(
    mint: str,
    completeness_status: str = "complete",
    event_count: int = 5,
    slot_offset: int = 0,
) -> dict:
    """Build a shadow_snapshots row as ShadowRecorder._flush() would produce."""
    return {
        "dataset": "shadow_snapshots",
        "mint": mint,
        "event_slot": _SLOT_BASE + slot_offset,
        "event_block_time_ms": _BLOCK_TIME_BASE_MS + slot_offset * 400,
        "event_date": "2024-06-18",
        "first_k_slots": 10,
        "completeness_status": completeness_status,
        "recorded_at_ms": int(time.time() * 1_000),
        "events_json": "[]",
        "event_count": event_count,
    }


def _make_launch_event(mint: str, slot_offset: int = 0) -> LaunchEvent:
    slot = _SLOT_BASE + slot_offset
    block_time_ms = _BLOCK_TIME_BASE_MS + slot_offset * 400
    return LaunchEvent(
        mint=mint,
        venue_program_id="6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",
        source=LaunchSource.PUMPFUN,
        event_time=EventTime(
            slot=slot,
            block_time_ms=block_time_ms,
            wall_clock_ms=block_time_ms + 50,
        ),
        observation_slot=slot,
        confirmation_slot=slot,
        detection_transport=DetectionTransport.GEYSER,
        sol_reserve_lamports=5_000_000_000,
        token_reserve_base=1_000_000_000_000,
        token_decimals=6,
        initial_holders=1,
        competitors=0,
        creator_wallet="Creator111111111111111111111111111111111111",
        bundler_cluster_id=None,
        deploy_template_fingerprint=None,
        data_staleness_ms=50,
    )


def _make_auditor(
    records: list[CensusRecord],
    declared_max_miss_rate: float = 0.10,
    confidence_level: float = 0.95,
) -> CompletenessAuditor:
    source = InMemoryCensusSource(records)
    return CompletenessAuditor(
        census_source=source,
        declared_max_miss_rate=declared_max_miss_rate,
        confidence_level=confidence_level,
    )


# ---------------------------------------------------------------------------
# Test 1: Empty census → zero miss rate, gate passes, zero rows
# ---------------------------------------------------------------------------

class TestEmptyCensus:
    def test_empty_census_zero_rows(self):
        auditor = _make_auditor([])
        report = auditor.reconcile([], slot_range_start=0, slot_range_end=999_999)
        assert report.census_total == 0
        assert report.recorded_complete == 0
        assert report.censored_count == 0
        assert report.rows == []

    def test_empty_census_miss_rate_zero(self):
        auditor = _make_auditor([])
        report = auditor.reconcile([], 0, 999_999)
        assert report.miss_rate_point == 0.0
        assert report.miss_rate_upper_bound == 0.0

    def test_empty_census_gate_passes(self):
        auditor = _make_auditor([])
        report = auditor.reconcile([], 0, 999_999)
        assert report.gate_passes is True

    def test_empty_census_coverage_fraction_one(self):
        auditor = _make_auditor([])
        report = auditor.reconcile([], 0, 999_999)
        assert report.coverage_fraction == 1.0


# ---------------------------------------------------------------------------
# Test 2: All launches complete
# ---------------------------------------------------------------------------

class TestAllComplete:
    def test_all_complete_zero_miss_rate(self):
        mints = [f"Mint{'A' * 43}{i}" for i in range(5)]
        records = [_census_record(m, i) for i, m in enumerate(mints)]
        snaps = [_snapshot_row(m, "complete") for m in mints]
        auditor = _make_auditor(records)
        report = auditor.reconcile(snaps, _SLOT_BASE, _SLOT_BASE + 100)
        assert report.miss_rate_point == 0.0
        assert report.censored_count == 0

    def test_all_complete_gate_passes(self):
        # Need enough samples for the Wilson upper bound to fit below declared max.
        # With 0 misses / 100 samples the Wilson 95% CI upper ≈ 3.7% < 5% declared.
        mints = [f"GateMint{'B' * 35}{i:03d}" for i in range(100)]
        records = [_census_record(m, i) for i, m in enumerate(mints)]
        snaps = [_snapshot_row(m, "complete") for m in mints]
        auditor = _make_auditor(records, declared_max_miss_rate=0.05)
        report = auditor.reconcile(snaps, _SLOT_BASE, _SLOT_BASE + 200)
        assert report.gate_passes is True

    def test_all_complete_rows_all_complete_status(self):
        mints = [f"Mint{'C' * 43}{i}" for i in range(3)]
        records = [_census_record(m, i) for i, m in enumerate(mints)]
        snaps = [_snapshot_row(m, "complete") for m in mints]
        auditor = _make_auditor(records)
        report = auditor.reconcile(snaps, _SLOT_BASE, _SLOT_BASE + 100)
        assert all(r.status == CompletenessStatus.COMPLETE for r in report.rows)


# ---------------------------------------------------------------------------
# Test 3: All launches censored
# ---------------------------------------------------------------------------

class TestAllCensored:
    def test_all_censored_miss_rate_one(self):
        mints = [f"Mint{'D' * 43}{i}" for i in range(4)]
        records = [_census_record(m, i) for i, m in enumerate(mints)]
        auditor = _make_auditor(records, declared_max_miss_rate=0.05)
        # No snapshots at all
        report = auditor.reconcile([], _SLOT_BASE, _SLOT_BASE + 100)
        assert report.miss_rate_point == 1.0
        assert report.censored_count == 4

    def test_all_censored_gate_fails(self):
        mints = [f"Mint{'E' * 43}{i}" for i in range(4)]
        records = [_census_record(m, i) for i, m in enumerate(mints)]
        auditor = _make_auditor(records, declared_max_miss_rate=0.05)
        report = auditor.reconcile([], _SLOT_BASE, _SLOT_BASE + 100)
        assert report.gate_passes is False

    def test_all_censored_upper_bound_le_one(self):
        mints = [f"Mint{'F' * 43}{i}" for i in range(4)]
        records = [_census_record(m, i) for i, m in enumerate(mints)]
        auditor = _make_auditor(records)
        report = auditor.reconcile([], _SLOT_BASE, _SLOT_BASE + 100)
        assert report.miss_rate_upper_bound <= 1.0


# ---------------------------------------------------------------------------
# Test 4: Partial miss — 10 census, 8 complete, 2 censored
# ---------------------------------------------------------------------------

class TestPartialMiss:
    def setup_method(self):
        self.mints = [f"PMint{'G' * 38}{i:04d}" for i in range(10)]
        self.records = [_census_record(m, i) for i, m in enumerate(self.mints)]
        # 8 complete, 2 not in snapshots → censored
        self.snaps = [_snapshot_row(m, "complete") for m in self.mints[:8]]
        self.auditor = _make_auditor(self.records, declared_max_miss_rate=0.50)

    def test_partial_miss_count(self):
        report = self.auditor.reconcile(self.snaps, _SLOT_BASE, _SLOT_BASE + 100)
        assert report.censored_count == 2
        assert report.recorded_complete == 8

    def test_partial_miss_rate_point(self):
        report = self.auditor.reconcile(self.snaps, _SLOT_BASE, _SLOT_BASE + 100)
        assert abs(report.miss_rate_point - 0.2) < 1e-9

    def test_partial_miss_rate_upper_bound_gt_point(self):
        report = self.auditor.reconcile(self.snaps, _SLOT_BASE, _SLOT_BASE + 100)
        # Wilson upper bound must be > point estimate when there are misses
        assert report.miss_rate_upper_bound > report.miss_rate_point

    def test_partial_miss_total_rows(self):
        report = self.auditor.reconcile(self.snaps, _SLOT_BASE, _SLOT_BASE + 100)
        assert len(report.rows) == 10


# ---------------------------------------------------------------------------
# Test 5: CENSORED rows are NEVER dropped (survivorship-free)
# ---------------------------------------------------------------------------

class TestSurvivorshipFree:
    def test_censored_rows_present_in_report(self):
        mints = [f"SFMint{'H' * 38}{i:04d}" for i in range(5)]
        records = [_census_record(m, i) for i, m in enumerate(mints)]
        # Only 3 recorded
        snaps = [_snapshot_row(m, "complete") for m in mints[:3]]
        auditor = _make_auditor(records, declared_max_miss_rate=0.99)
        report = auditor.reconcile(snaps, _SLOT_BASE, _SLOT_BASE + 100)
        # All 5 mints must appear in rows
        row_mints = {r.census_record.mint for r in report.rows}
        assert row_mints == set(mints)

    def test_all_rows_accounted(self):
        mints = [f"SFMint{'I' * 38}{i:04d}" for i in range(6)]
        records = [_census_record(m, i) for i, m in enumerate(mints)]
        snaps = [_snapshot_row(m, "complete") for m in mints[:4]]
        auditor = _make_auditor(records, declared_max_miss_rate=0.99)
        report = auditor.reconcile(snaps, _SLOT_BASE, _SLOT_BASE + 100)
        assert len(report.rows) == 6

    def test_coverage_fraction_always_one(self):
        """(complete + CENSORED) / census_total must always == 1.0."""
        mints = [f"SFMint{'J' * 38}{i:04d}" for i in range(8)]
        records = [_census_record(m, i) for i, m in enumerate(mints)]
        snaps = [_snapshot_row(m, "complete") for m in mints[:3]]
        auditor = _make_auditor(records, declared_max_miss_rate=0.99)
        report = auditor.reconcile(snaps, _SLOT_BASE, _SLOT_BASE + 100)
        # coverage_fraction = (3 + 5) / 8 = 1.0
        assert report.coverage_fraction == 1.0


# ---------------------------------------------------------------------------
# Test 6: Gate fails when upper bound > declared max
# ---------------------------------------------------------------------------

class TestGateFailure:
    def test_gate_fails_tight_declared_max(self):
        mints = [f"GFMint{'K' * 38}{i:04d}" for i in range(20)]
        records = [_census_record(m, i) for i, m in enumerate(mints)]
        # 5 misses out of 20 → upper bound >> 0.05
        snaps = [_snapshot_row(m, "complete") for m in mints[:15]]
        auditor = _make_auditor(records, declared_max_miss_rate=0.05)
        report = auditor.reconcile(snaps, _SLOT_BASE, _SLOT_BASE + 100)
        assert report.gate_passes is False


# ---------------------------------------------------------------------------
# Test 7: Gate passes when upper bound <= declared max
# ---------------------------------------------------------------------------

class TestGatePass:
    def test_gate_passes_generous_declared_max(self):
        mints = [f"GPMint{'L' * 38}{i:04d}" for i in range(100)]
        records = [_census_record(m, i) for i, m in enumerate(mints)]
        snaps = [_snapshot_row(m, "complete") for m in mints]
        auditor = _make_auditor(records, declared_max_miss_rate=0.05)
        report = auditor.reconcile(snaps, _SLOT_BASE, _SLOT_BASE + 200)
        assert report.gate_passes is True

    def test_gate_passes_zero_miss(self):
        # Wilson CI upper for 0 misses / 1000 is ~0.38%, below 1% declared max
        mints = [f"GPMint{'M' * 35}{i:04d}" for i in range(1000)]
        records = [_census_record(m, i) for i, m in enumerate(mints)]
        snaps = [_snapshot_row(m, "complete") for m in mints]
        auditor = _make_auditor(records, declared_max_miss_rate=0.01)
        report = auditor.reconcile(snaps, _SLOT_BASE, _SLOT_BASE + 1200)
        assert report.gate_passes is True


# ---------------------------------------------------------------------------
# Test 8: coverage_fraction = (complete+censored)/census_total == 1.0
# ---------------------------------------------------------------------------

class TestCoverageFraction:
    def test_coverage_fraction_mixed(self):
        mints = [f"CFMint{'N' * 38}{i:04d}" for i in range(10)]
        records = [_census_record(m, i) for i, m in enumerate(mints)]
        snaps = [_snapshot_row(m, "complete") for m in mints[:6]]
        snaps += [_snapshot_row(m, "CENSORED") for m in mints[6:8]]
        # mints[8:] have no snapshot at all
        auditor = _make_auditor(records, declared_max_miss_rate=0.99)
        report = auditor.reconcile(snaps, _SLOT_BASE, _SLOT_BASE + 100)
        # complete=6, censored=4, total=10 → fraction=1.0
        assert report.coverage_fraction == 1.0

    def test_coverage_fraction_all_censored(self):
        mints = [f"CFMint{'O' * 38}{i:04d}" for i in range(5)]
        records = [_census_record(m, i) for i, m in enumerate(mints)]
        auditor = _make_auditor(records, declared_max_miss_rate=0.99)
        report = auditor.reconcile([], _SLOT_BASE, _SLOT_BASE + 100)
        assert report.coverage_fraction == 1.0


# ---------------------------------------------------------------------------
# Test 9: Wilson upper bound > point estimate for non-zero miss (conservative)
# ---------------------------------------------------------------------------

class TestWilsonBound:
    def test_wilson_upper_gt_point_estimate(self):
        # 5 misses out of 50
        upper = _wilson_upper_bound(5, 50, 0.95)
        point = 5 / 50
        assert upper > point

    def test_wilson_upper_le_one(self):
        upper = _wilson_upper_bound(50, 50, 0.95)
        assert upper <= 1.0

    def test_wilson_zero_misses(self):
        upper = _wilson_upper_bound(0, 100, 0.95)
        # Zero misses in 100 trials → Wilson CI upper is small positive (correct math).
        # The true rate is not provably 0; the CI gives a conservative upper bound.
        # For 0/100 at 95%: z^2/(n+z^2) ≈ 1.96^2/(100+1.96^2) ≈ 3.7%
        assert upper > 0.0  # Wilson is always positive for any finite n
        assert upper < 0.05  # But small for large n

    def test_wilson_all_misses_upper_is_one(self):
        upper = _wilson_upper_bound(1, 1, 0.95)
        assert upper == 1.0

    def test_wilson_no_data(self):
        upper = _wilson_upper_bound(0, 0, 0.95)
        assert upper == 1.0  # no data → worst case


# ---------------------------------------------------------------------------
# Test 10: Wilson upper bound monotonically increases with miss count
# ---------------------------------------------------------------------------

class TestWilsonMonotone:
    def test_monotone_increase(self):
        n = 100
        bounds = [_wilson_upper_bound(k, n, 0.95) for k in range(0, 10)]
        for i in range(len(bounds) - 1):
            assert bounds[i] <= bounds[i + 1], (
                f"Wilson bound not monotone at k={i}: {bounds[i]} > {bounds[i+1]}"
            )


# ---------------------------------------------------------------------------
# Test 11: InMemoryCensusSource slot-range filter
# ---------------------------------------------------------------------------

class TestCensusSourceFilter:
    def test_slot_range_inclusive(self):
        records = [
            _census_record("MintSlot01111111111111111111111111111111111", 0),   # slot=_SLOT_BASE
            _census_record("MintSlot21111111111111111111111111111111111", 2),   # slot=_SLOT_BASE+2
            _census_record("MintSlot51111111111111111111111111111111111", 5),   # slot=_SLOT_BASE+5
            _census_record("MintSlot101111111111111111111111111111111111", 10), # slot=_SLOT_BASE+10
        ]
        source = InMemoryCensusSource(records)
        result = source.pool_creates_in_slot_range(_SLOT_BASE + 2, _SLOT_BASE + 5)
        assert len(result) == 2
        assert {r.mint for r in result} == {records[1].mint, records[2].mint}

    def test_slot_range_excludes_outside(self):
        records = [_census_record(f"MintExcl{'A' * 36}{i}", i) for i in range(5)]
        source = InMemoryCensusSource(records)
        result = source.pool_creates_in_slot_range(_SLOT_BASE + 2, _SLOT_BASE + 3)
        assert len(result) == 2  # only offsets 2 and 3

    def test_empty_range(self):
        records = [_census_record(f"MintEmpty{'B' * 35}{i}", i) for i in range(5)]
        source = InMemoryCensusSource(records)
        result = source.pool_creates_in_slot_range(_SLOT_BASE + 100, _SLOT_BASE + 200)
        assert result == []


# ---------------------------------------------------------------------------
# Test 12: Mint not in census snapshot → NOT a miss (out of scope)
# ---------------------------------------------------------------------------

class TestOutOfScopeMint:
    def test_snapshot_mint_not_in_census_ignored(self):
        """A mint we recorded but is not in the census → not counted as miss or complete."""
        census_mint = "CensMint11111111111111111111111111111111111111"
        extra_mint  = "ExtraMint1111111111111111111111111111111111111"
        records = [_census_record(census_mint, 0)]
        # Both census and extra mints are in snapshots
        snaps = [
            _snapshot_row(census_mint, "complete"),
            _snapshot_row(extra_mint, "complete"),
        ]
        auditor = _make_auditor(records)
        report = auditor.reconcile(snaps, _SLOT_BASE, _SLOT_BASE + 100)
        # Only census mint matters; extra mint is invisible to the audit
        assert report.census_total == 1
        assert report.recorded_complete == 1
        assert report.censored_count == 0
        row_mints = {r.census_record.mint for r in report.rows}
        assert row_mints == {census_mint}


# ---------------------------------------------------------------------------
# Test 13: Snapshot CENSORED (stream dropout) → CENSORED in report
# ---------------------------------------------------------------------------

class TestSnapshotCensored:
    def test_snapshot_censored_by_stream_dropout(self):
        mint = "DrpMint111111111111111111111111111111111111111"
        records = [_census_record(mint, 0)]
        snaps = [_snapshot_row(mint, "CENSORED", event_count=0)]
        auditor = _make_auditor(records, declared_max_miss_rate=0.99)
        report = auditor.reconcile(snaps, _SLOT_BASE, _SLOT_BASE + 100)
        assert report.censored_count == 1
        row = report.rows[0]
        assert row.status == CompletenessStatus.CENSORED
        assert row.miss_reason == "snapshot_censored_stream_dropout_or_overflow"

    def test_snapshot_censored_snapshot_completeness_status_preserved(self):
        mint = "DrpMint211111111111111111111111111111111111111"
        records = [_census_record(mint, 0)]
        snaps = [_snapshot_row(mint, "CENSORED", event_count=3)]
        auditor = _make_auditor(records, declared_max_miss_rate=0.99)
        report = auditor.reconcile(snaps, _SLOT_BASE, _SLOT_BASE + 100)
        row = report.rows[0]
        assert row.snapshot_completeness_status == "CENSORED"
        assert row.snapshot_event_count == 3


# ---------------------------------------------------------------------------
# Test 14: Snapshot COMPLETE → COMPLETE in report
# ---------------------------------------------------------------------------

class TestSnapshotComplete:
    def test_complete_status(self):
        mint = "CmpMint111111111111111111111111111111111111111"
        records = [_census_record(mint, 0)]
        snaps = [_snapshot_row(mint, "complete", event_count=7)]
        auditor = _make_auditor(records)
        report = auditor.reconcile(snaps, _SLOT_BASE, _SLOT_BASE + 100)
        row = report.rows[0]
        assert row.status == CompletenessStatus.COMPLETE
        assert row.snapshot_event_count == 7
        assert row.miss_reason is None


# ---------------------------------------------------------------------------
# Test 15: Mint in census but NEVER seen by ingest → CENSORED, correct reason
# ---------------------------------------------------------------------------

class TestNeverSeen:
    def test_never_seen_mint_is_censored(self):
        mint = "NvrMint111111111111111111111111111111111111111"
        records = [_census_record(mint, 0)]
        auditor = _make_auditor(records, declared_max_miss_rate=0.99)
        report = auditor.reconcile([], _SLOT_BASE, _SLOT_BASE + 100)
        assert report.censored_count == 1
        row = report.rows[0]
        assert row.status == CompletenessStatus.CENSORED
        assert row.miss_reason == "mint_not_observed_by_primary_transport"
        assert row.snapshot_completeness_status is None
        assert row.snapshot_event_count == 0


# ---------------------------------------------------------------------------
# Test 16: Seen by ingest as CENSORED (overflow) → carries that reason
# ---------------------------------------------------------------------------

class TestSeenCensored:
    def test_overflow_censored_reason(self):
        mint = "OvfMint111111111111111111111111111111111111111"
        records = [_census_record(mint, 0)]
        snaps = [_snapshot_row(mint, "CENSORED", event_count=2)]
        auditor = _make_auditor(records, declared_max_miss_rate=0.99)
        report = auditor.reconcile(snaps, _SLOT_BASE, _SLOT_BASE + 100)
        row = report.rows[0]
        assert row.status == CompletenessStatus.CENSORED
        assert "snapshot_censored" in row.miss_reason


# ---------------------------------------------------------------------------
# Test 17: annotate_snapshot_rows
# ---------------------------------------------------------------------------

class TestAnnotateSnapshotRows:
    def test_in_census_true_for_matching_mint(self):
        mint_in = "AnnMint1In11111111111111111111111111111111111"
        mint_out = "AnnMint1Out1111111111111111111111111111111111"
        records = [_census_record(mint_in, 0)]
        snaps = [
            _snapshot_row(mint_in, "complete"),
            _snapshot_row(mint_out, "complete"),
        ]
        auditor = _make_auditor(records)
        annotated = auditor.annotate_snapshot_rows(snaps, _SLOT_BASE, _SLOT_BASE + 50)
        by_mint = {r["mint"]: r for r in annotated}
        assert by_mint[mint_in]["in_census"] is True
        assert by_mint[mint_out]["in_census"] is False

    def test_annotate_does_not_modify_original(self):
        mint = "AnnMint2111111111111111111111111111111111111111"
        records = [_census_record(mint, 0)]
        snap = _snapshot_row(mint, "complete")
        original_keys = set(snap.keys())
        auditor = _make_auditor(records)
        auditor.annotate_snapshot_rows([snap], _SLOT_BASE, _SLOT_BASE + 50)
        assert set(snap.keys()) == original_keys  # original unmodified


# ---------------------------------------------------------------------------
# Test 18: Multiple snapshot rows for same mint → prefer "complete"
# ---------------------------------------------------------------------------

class TestDuplicateMintRows:
    def test_prefer_complete_over_censored(self):
        mint = "DupMint111111111111111111111111111111111111111"
        records = [_census_record(mint, 0)]
        # First snapshot is CENSORED (stream dropout), second is complete (after recovery)
        snaps = [
            _snapshot_row(mint, "CENSORED", event_count=2),
            _snapshot_row(mint, "complete", event_count=10),
        ]
        auditor = _make_auditor(records)
        report = auditor.reconcile(snaps, _SLOT_BASE, _SLOT_BASE + 100)
        assert report.recorded_complete == 1
        assert report.censored_count == 0
        assert report.rows[0].status == CompletenessStatus.COMPLETE

    def test_two_censored_rows_stays_censored(self):
        mint = "DupMint211111111111111111111111111111111111111"
        records = [_census_record(mint, 0)]
        snaps = [
            _snapshot_row(mint, "CENSORED", event_count=0),
            _snapshot_row(mint, "CENSORED", event_count=1),
        ]
        auditor = _make_auditor(records, declared_max_miss_rate=0.99)
        report = auditor.reconcile(snaps, _SLOT_BASE, _SLOT_BASE + 100)
        assert report.censored_count == 1


# ---------------------------------------------------------------------------
# Test 19: census_total=0 → coverage_fraction=1.0
# ---------------------------------------------------------------------------

class TestZeroCensusTotal:
    def test_zero_total_coverage_fraction_one(self):
        auditor = _make_auditor([])
        report = auditor.reconcile([], 0, 999_999)
        assert report.coverage_fraction == 1.0


# ---------------------------------------------------------------------------
# Test 20: census_total=1, censored=1 → Wilson upper = 1.0
# ---------------------------------------------------------------------------

class TestSingleCensored:
    def test_single_censored_upper_one(self):
        upper = _wilson_upper_bound(1, 1, 0.95)
        assert upper == 1.0

    def test_report_upper_one_for_single_censored(self):
        mint = "Sngl1Mint1111111111111111111111111111111111111"
        records = [_census_record(mint, 0)]
        auditor = _make_auditor(records, declared_max_miss_rate=0.99)
        report = auditor.reconcile([], _SLOT_BASE, _SLOT_BASE + 100)
        assert report.miss_rate_upper_bound == 1.0


# ---------------------------------------------------------------------------
# Test 21: audit_recorded_at_ms is wall-clock (monitoring only)
# ---------------------------------------------------------------------------

class TestAuditRecordedAt:
    def test_audit_recorded_at_ms_is_wall_clock(self):
        auditor = _make_auditor([])
        before_ms = int(time.time() * 1_000)
        report = auditor.reconcile([], 0, 999_999)
        after_ms = int(time.time() * 1_000)
        # audit_recorded_at_ms should be between the before and after wall-clock
        assert before_ms <= report.audit_recorded_at_ms <= after_ms + 100

    def test_custom_now_ms_is_used(self):
        auditor = _make_auditor([])
        custom_ms = 1_718_700_999_000
        report = auditor.reconcile([], 0, 999_999, now_ms=custom_ms)
        assert report.audit_recorded_at_ms == custom_ms


# ---------------------------------------------------------------------------
# Test 22: report.summary() contains required fields
# ---------------------------------------------------------------------------

class TestSummaryFormat:
    def test_summary_contains_gate_verdict(self):
        mints = [f"SumMint{'P' * 38}{i:04d}" for i in range(5)]
        records = [_census_record(m, i) for i, m in enumerate(mints)]
        snaps = [_snapshot_row(m, "complete") for m in mints]
        auditor = _make_auditor(records)
        report = auditor.reconcile(snaps, _SLOT_BASE, _SLOT_BASE + 100)
        s = report.summary()
        assert "PASS" in s or "FAIL" in s

    def test_summary_contains_miss_rate(self):
        auditor = _make_auditor([])
        report = auditor.reconcile([], 0, 999_999)
        s = report.summary()
        assert "miss_rate" in s

    def test_summary_contains_census_total(self):
        auditor = _make_auditor([])
        report = auditor.reconcile([], 0, 999_999)
        s = report.summary()
        assert "census=" in s


# ---------------------------------------------------------------------------
# Test 23: Auditor is stateless — two calls with same input give same output
# ---------------------------------------------------------------------------

class TestAuditorStateless:
    def test_idempotent_reconcile(self):
        mints = [f"IdmpMint{'Q' * 37}{i:04d}" for i in range(6)]
        records = [_census_record(m, i) for i, m in enumerate(mints)]
        snaps = [_snapshot_row(m, "complete") for m in mints[:4]]
        auditor = _make_auditor(records, declared_max_miss_rate=0.99)
        fixed_ms = 1_718_800_000_000
        r1 = auditor.reconcile(snaps, _SLOT_BASE, _SLOT_BASE + 100, now_ms=fixed_ms)
        r2 = auditor.reconcile(snaps, _SLOT_BASE, _SLOT_BASE + 100, now_ms=fixed_ms)
        assert r1.census_total == r2.census_total
        assert r1.recorded_complete == r2.recorded_complete
        assert r1.censored_count == r2.censored_count
        assert r1.miss_rate_point == r2.miss_rate_point
        assert r1.miss_rate_upper_bound == r2.miss_rate_upper_bound
        assert r1.gate_passes == r2.gate_passes


# ---------------------------------------------------------------------------
# Test 24: CENSORED-only census entry in censored_rows(), not complete_rows()
# ---------------------------------------------------------------------------

class TestHelperMethods:
    def test_censored_rows_and_complete_rows_disjoint(self):
        mints = [f"HelperMint{'R' * 34}{i:04d}" for i in range(6)]
        records = [_census_record(m, i) for i, m in enumerate(mints)]
        snaps = [_snapshot_row(m, "complete") for m in mints[:4]]
        auditor = _make_auditor(records, declared_max_miss_rate=0.99)
        report = auditor.reconcile(snaps, _SLOT_BASE, _SLOT_BASE + 100)
        censored_mints = {r.census_record.mint for r in report.censored_rows()}
        complete_mints = {r.census_record.mint for r in report.complete_rows()}
        assert censored_mints.isdisjoint(complete_mints)

    def test_censored_rows_correct_count(self):
        mints = [f"HelperMint{'S' * 34}{i:04d}" for i in range(8)]
        records = [_census_record(m, i) for i, m in enumerate(mints)]
        snaps = [_snapshot_row(m, "complete") for m in mints[:5]]
        auditor = _make_auditor(records, declared_max_miss_rate=0.99)
        report = auditor.reconcile(snaps, _SLOT_BASE, _SLOT_BASE + 100)
        assert len(report.censored_rows()) == 3
        assert len(report.complete_rows()) == 5


# ---------------------------------------------------------------------------
# Test 25: ShadowRecorder integration — on_reconnect → CENSORED → audit marks CENSORED
# ---------------------------------------------------------------------------

class TestShadowRecorderIntegration:
    def test_reconnect_censor_propagates_to_audit(self):
        """on_reconnect() marks open windows CENSORED; audit picks that up.

        ShadowRecorder.completed_snapshots() returns PointInTimeRecord.to_row()
        dicts, which store the rich snapshot data inside payload_json.  The
        CompletenessAuditor._normalize_snapshot_row() unwraps payload_json so
        completeness_status is visible to the reconciliation logic.
        """
        import json as _json

        backend = InMemoryParquetBackend()
        recorder = ShadowRecorder(backend, first_k_slots=10)

        # Observe two mints
        ev_a = _make_launch_event("RcnMint_A111111111111111111111111111111111111", 0)
        ev_b = _make_launch_event("RcnMint_B111111111111111111111111111111111111", 1)
        recorder.observe(ev_a)
        recorder.observe(ev_b)

        # Simulate stream reconnect before K slots are complete
        recorder.on_reconnect()

        snaps = recorder.completed_snapshots()
        assert len(snaps) == 2

        # ShadowRecorder stores completeness_status inside payload_json
        # (the PointInTimeRecord.to_row() format used by all_rows())
        for s in snaps:
            inner = _json.loads(s["payload_json"])
            assert inner["completeness_status"] == "CENSORED"

        # Census knows about both
        records = [
            CensusRecord(
                mint="RcnMint_A111111111111111111111111111111111111",
                pool_create_slot=_SLOT_BASE,
                pool_create_block_time_ms=_BLOCK_TIME_BASE_MS,
            ),
            CensusRecord(
                mint="RcnMint_B111111111111111111111111111111111111",
                pool_create_slot=_SLOT_BASE + 1,
                pool_create_block_time_ms=_BLOCK_TIME_BASE_MS + 400,
            ),
        ]
        auditor = _make_auditor(records, declared_max_miss_rate=0.99)
        # Auditor normalizes payload_json automatically via _normalize_snapshot_row
        report = auditor.reconcile(snaps, _SLOT_BASE, _SLOT_BASE + 50)

        # Both are CENSORED in the report (survivorship-free, not dropped)
        assert report.censored_count == 2
        assert report.recorded_complete == 0
        assert report.coverage_fraction == 1.0

    def test_complete_snapshot_passes_audit(self):
        """A complete K-slot window passes the audit as COMPLETE.

        ShadowRecorder.completed_snapshots() stores completeness_status inside
        payload_json; the auditor's _normalize_snapshot_row() unwraps it.
        """
        import json as _json

        backend = InMemoryParquetBackend()
        recorder = ShadowRecorder(backend, first_k_slots=5)

        # Build K+1 events to trigger auto-flush
        mint = "CmpRcnMint1111111111111111111111111111111111"
        for slot_offset in range(7):  # 0..6 → slot 6 > K=5 triggers flush of mint
            ev = _make_launch_event(mint, slot_offset)
            recorder.observe(ev)

        snaps = recorder.completed_snapshots()
        assert len(snaps) == 1

        # Verify payload_json contains completeness_status = complete
        inner = _json.loads(snaps[0]["payload_json"])
        assert inner["completeness_status"] == "complete"

        records = [CensusRecord(
            mint=mint,
            pool_create_slot=_SLOT_BASE,
            pool_create_block_time_ms=_BLOCK_TIME_BASE_MS,
        )]
        # Wilson upper for 0/1 miss at 95%: 1.0 (single sample — known pessimistic)
        # Use declared_max=1.0 so the gate passes for this unit test
        auditor = _make_auditor(records, declared_max_miss_rate=1.0)
        report = auditor.reconcile(snaps, _SLOT_BASE, _SLOT_BASE + 50)
        assert report.recorded_complete == 1
        assert report.censored_count == 0
        # Coverage is complete (all census mints accounted for)
        assert report.coverage_fraction == 1.0


# ---------------------------------------------------------------------------
# Test 26: Mutation proxy — if we drop CENSORED rows coverage_fraction < 1.0
# ---------------------------------------------------------------------------

class TestMutationProxySurvivorshipFree:
    def test_dropping_censored_rows_breaks_coverage(self):
        """Simulates what would happen if CENSORED rows were dropped.

        This is the mutation proxy: instead of mutating the implementation,
        we manually replicate the "drop censored" behavior and assert it
        breaks the invariant.
        """
        mints = [f"MutProx{'T' * 37}{i:04d}" for i in range(10)]
        records = [_census_record(m, i) for i, m in enumerate(mints)]
        snaps = [_snapshot_row(m, "complete") for m in mints[:6]]
        auditor = _make_auditor(records, declared_max_miss_rate=0.99)
        report = auditor.reconcile(snaps, _SLOT_BASE, _SLOT_BASE + 100)

        # Correct: coverage_fraction == 1.0 (CENSORED rows carried)
        assert report.coverage_fraction == 1.0

        # Simulate dropping CENSORED rows (what a WRONG implementation would do)
        wrong_rows = [r for r in report.rows if r.status != CompletenessStatus.CENSORED]
        wrong_complete = sum(1 for r in wrong_rows if r.status == CompletenessStatus.COMPLETE)
        wrong_censored = 0  # dropped
        wrong_coverage = (wrong_complete + wrong_censored) / report.census_total

        # A coverage_fraction < 1.0 proves survivorship bias was introduced
        assert wrong_coverage < 1.0, (
            "Expected dropping CENSORED to produce coverage < 1.0 — "
            "if this fails, the mutation proxy itself is wrong."
        )


# ---------------------------------------------------------------------------
# Test 27: Large census N=1000, all complete → upper bound < 0.01
# ---------------------------------------------------------------------------

class TestLargeCensusAllComplete:
    def test_large_all_complete_tight_bound(self):
        upper = _wilson_upper_bound(0, 1000, 0.95)
        # Zero misses in 1000 → Wilson upper bound is small positive (correct math).
        # For 0/1000 at 95% CI: approximately 0.38%
        assert upper > 0.0
        assert upper < 0.01, f"Wilson upper={upper:.5f} should be < 1% for 0/1000 misses"


# ---------------------------------------------------------------------------
# Test 28: Large census N=1000, 50 misses → upper bound ~ 6.8%
# ---------------------------------------------------------------------------

class TestLargeCensusPartialMiss:
    def test_large_partial_miss_upper_bound_reasonable(self):
        upper = _wilson_upper_bound(50, 1000, 0.95)
        point = 50 / 1000  # 5%
        # Upper bound should be above the point estimate but below ~8%
        assert upper > point
        assert upper < 0.08, f"Wilson upper {upper:.4f} unexpectedly large for 50/1000 misses"

    def test_large_census_report_gate_fail_with_tight_max(self):
        n = 1000
        mints = [f"LrgMnt{'U' * 38}{i:04d}" for i in range(n)]
        records = [_census_record(m, i) for i, m in enumerate(mints)]
        # 50 missed
        snaps = [_snapshot_row(m, "complete") for m in mints[:950]]
        auditor = _make_auditor(records, declared_max_miss_rate=0.04)
        report = auditor.reconcile(snaps, _SLOT_BASE, _SLOT_BASE + n)
        # Point = 5%, upper > 5% > 4% declared max → should fail
        assert report.gate_passes is False


# ---------------------------------------------------------------------------
# Test 29: AuditReport.miss_count property
# ---------------------------------------------------------------------------

class TestMissCountProperty:
    def test_miss_count_equals_censored_count(self):
        mints = [f"McPropMt{'V' * 36}{i:04d}" for i in range(5)]
        records = [_census_record(m, i) for i, m in enumerate(mints)]
        snaps = [_snapshot_row(m, "complete") for m in mints[:3]]
        auditor = _make_auditor(records, declared_max_miss_rate=0.99)
        report = auditor.reconcile(snaps, _SLOT_BASE, _SLOT_BASE + 100)
        assert report.miss_count == report.censored_count
        assert report.miss_count == 2


# ---------------------------------------------------------------------------
# Test 30: InMemoryCensusSource.add() increments len
# ---------------------------------------------------------------------------

class TestInMemoryCensusSourceAdd:
    def test_add_increments_len(self):
        source = InMemoryCensusSource([])
        assert len(source) == 0
        source.add(_census_record("AddMint1111111111111111111111111111111111111", 0))
        assert len(source) == 1
        source.add(_census_record("AddMint2111111111111111111111111111111111111", 1))
        assert len(source) == 2

    def test_add_then_filter(self):
        source = InMemoryCensusSource([])
        source.add(_census_record("AddMint3111111111111111111111111111111111111", 0))
        source.add(_census_record("AddMint4111111111111111111111111111111111111", 50))
        result = source.pool_creates_in_slot_range(_SLOT_BASE, _SLOT_BASE + 10)
        assert len(result) == 1  # only offset=0 is in range

    def test_add_returns_none(self):
        source = InMemoryCensusSource([])
        ret = source.add(_census_record("AddMint5111111111111111111111111111111111111", 0))
        assert ret is None


# ---------------------------------------------------------------------------
# Extra: No float money fields in any CensusRecord or AuditReport
# ---------------------------------------------------------------------------

class TestNoFloatMoney:
    def test_census_record_lamport_fields_are_int(self):
        cr = _census_record("FloatCheckMint111111111111111111111111111111", 0)
        assert isinstance(cr.pool_create_slot, int)
        assert isinstance(cr.pool_create_block_time_ms, int)

    def test_audit_report_count_fields_are_int(self):
        mints = [f"FltChkMnt{'W' * 35}{i:04d}" for i in range(3)]
        records = [_census_record(m, i) for i, m in enumerate(mints)]
        snaps = [_snapshot_row(m, "complete") for m in mints]
        auditor = _make_auditor(records)
        report = auditor.reconcile(snaps, _SLOT_BASE, _SLOT_BASE + 100)
        assert isinstance(report.census_total, int)
        assert isinstance(report.recorded_complete, int)
        assert isinstance(report.censored_count, int)
        assert isinstance(report.audit_recorded_at_ms, int)
        # miss_rate_point and miss_rate_upper_bound are float (statistical, not monetary)
        assert isinstance(report.miss_rate_point, float)
        assert isinstance(report.miss_rate_upper_bound, float)


# ---------------------------------------------------------------------------
# Extra: CensusSource Protocol compliance
# ---------------------------------------------------------------------------

class TestCensusSourceProtocol:
    def test_in_memory_source_satisfies_protocol(self):
        from aats.ingestion.completeness import CensusSource
        source = InMemoryCensusSource([])
        assert isinstance(source, CensusSource)
