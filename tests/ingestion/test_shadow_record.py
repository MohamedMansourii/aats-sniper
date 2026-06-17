"""Tests for the shadow_record entrypoint (M1 data collection runner).

Covers:
  - Demo (replay) run: assembles ProgramRegistry + InstructionRouter +
    ShadowRecorder + PointInTimeStoreWriter + TransportPipeline and ingests
    synthetic launch transactions.
  - Point-in-time correctness: recorded_at_ms >= event_block_time_ms for
    every snapshot row (honesty invariant).
  - Corpus read-back: corpus is JSON-L and all fields are present.
  - No float money: sol_reserve_lamports and token_reserve_base are int in
    every stored LaunchEvent payload.
  - No secrets / no win-rate / no truth_* fields anywhere in output.
  - max_events cap: pipeline stops at the requested limit.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_ALLOWLIST = _PROJECT_ROOT / "config" / "program-allowlist.json"


def _run_shadow(max_events: int = 25, first_k_slots: int = 10) -> dict:
    """Drive the _run() coroutine for the demo (replay) source and return stats."""
    from aats.ingestion.shadow_record import _run

    with tempfile.TemporaryDirectory(prefix="aats_test_shadow_") as tmp:
        out_dir = Path(tmp)
        stats = asyncio.run(
            _run(
                source="replay",
                out_dir=out_dir,
                max_events=max_events,
                allowlist_path=_ALLOWLIST,
                first_k_slots=first_k_slots,
            )
        )
        # Read back the corpus while the tempdir is still alive
        corpus_path = Path(stats["corpus_path"])
        rows: list[dict] = []
        if corpus_path.exists():
            for line in corpus_path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    rows.append(json.loads(line))
        stats["_rows"] = rows
    return stats


# ---------------------------------------------------------------------------
# Basic ingestion
# ---------------------------------------------------------------------------


class TestShadowRecordReplay:
    """Demo (replay) source — no network required."""

    def test_events_are_decoded(self):
        stats = _run_shadow(max_events=25)
        assert stats["events_decoded"] > 0, (
            "Expected at least one decoded event from the demo transaction set"
        )

    def test_events_decoded_respects_max_events(self):
        """Pipeline must stop at max_events (backpressure / cap)."""
        stats = _run_shadow(max_events=5)
        assert stats["events_decoded"] <= 5

    def test_snapshots_are_recorded(self):
        """At least one ShadowSnapshot must be flushed and written to corpus."""
        stats = _run_shadow(max_events=25)
        assert stats["snapshots_recorded"] > 0, (
            "Expected at least one ShadowSnapshot in the corpus"
        )

    def test_corpus_path_exists(self):
        """The corpus file must be created even if it is being accessed after close."""
        from aats.ingestion.shadow_record import _run

        with tempfile.TemporaryDirectory(prefix="aats_test_corpus_") as tmp:
            out_dir = Path(tmp)
            stats = asyncio.run(
                _run(
                    source="replay",
                    out_dir=out_dir,
                    max_events=10,
                    allowlist_path=_ALLOWLIST,
                    first_k_slots=10,
                )
            )
            assert Path(stats["corpus_path"]).exists()

    def test_no_decode_errors(self):
        stats = _run_shadow(max_events=25)
        assert stats["decode_errors"] == 0, (
            f"Unexpected decode errors: {stats['decode_errors']}"
        )


# ---------------------------------------------------------------------------
# Point-in-time correctness
# ---------------------------------------------------------------------------


class TestPointInTimeCorrectness:
    """recorded_at_ms >= event_block_time_ms on every snapshot row."""

    def test_recorded_at_honesty_invariant(self):
        """Every snapshot row: recorded_at_ms >= event_block_time_ms."""
        stats = _run_shadow(max_events=25)
        rows = stats["_rows"]
        assert rows, "No snapshot rows to check — recorded corpus is empty"
        violations = []
        for i, row in enumerate(rows):
            event_block_time_ms = row.get("event_block_time_ms", 0)
            recorded_at_ms = row.get("recorded_at_ms", 0)
            if recorded_at_ms < event_block_time_ms:
                violations.append(
                    f"row[{i}] mint={row.get('mint')!r} "
                    f"recorded_at_ms={recorded_at_ms} < "
                    f"event_block_time_ms={event_block_time_ms}"
                )
        assert not violations, (
            "PROVENANCE TAINT — recorded_at_ms < event_block_time_ms:\n"
            + "\n".join(violations)
        )

    def test_event_slot_is_present(self):
        """Every snapshot row must carry event_slot (on-chain slot anchor)."""
        stats = _run_shadow(max_events=25)
        rows = stats["_rows"]
        assert rows
        for i, row in enumerate(rows):
            assert "event_slot" in row, f"row[{i}] missing event_slot"
            assert isinstance(row["event_slot"], int), f"row[{i}] event_slot not int"
            assert row["event_slot"] > 0, f"row[{i}] event_slot must be positive"

    def test_event_block_time_ms_is_present(self):
        """Every snapshot row must carry event_block_time_ms (authoritative clock)."""
        stats = _run_shadow(max_events=25)
        rows = stats["_rows"]
        assert rows
        for i, row in enumerate(rows):
            assert "event_block_time_ms" in row, f"row[{i}] missing event_block_time_ms"
            assert row["event_block_time_ms"] > 0, (
                f"row[{i}] event_block_time_ms must be > 0 (not wall-clock)"
            )


# ---------------------------------------------------------------------------
# Corpus schema correctness
# ---------------------------------------------------------------------------


class TestCorpusSchema:
    """The stored corpus must have the correct shape and no forbidden fields."""

    def test_corpus_rows_have_required_fields(self):
        stats = _run_shadow(max_events=25)
        rows = stats["_rows"]
        assert rows
        required = {
            "dataset", "mint", "event_slot", "event_block_time_ms",
            "recorded_at_ms", "completeness_status", "event_count",
        }
        for i, row in enumerate(rows):
            missing = required - set(row.keys())
            assert not missing, f"row[{i}] missing fields: {missing}"

    def test_dataset_is_shadow_snapshots(self):
        stats = _run_shadow(max_events=25)
        rows = stats["_rows"]
        assert rows
        for i, row in enumerate(rows):
            assert row["dataset"] == "shadow_snapshots", (
                f"row[{i}] dataset={row['dataset']!r}, expected 'shadow_snapshots'"
            )

    def test_no_win_rate_field(self):
        """No win_rate, truth_*, or label fields in any snapshot row."""
        stats = _run_shadow(max_events=25)
        rows = stats["_rows"]
        forbidden_substrings = ("win_rate", "truth_", "label")
        for i, row in enumerate(rows):
            row_str = json.dumps(row).lower()
            for bad in forbidden_substrings:
                assert bad not in row_str, (
                    f"row[{i}] contains forbidden field substring {bad!r}. "
                    "No win-rate, truth_*, or label fields allowed in production corpus."
                )

    def test_no_float_money_in_payloads(self):
        """sol_reserve_lamports and token_reserve_base must be int in every stored event."""
        stats = _run_shadow(max_events=25)
        rows = stats["_rows"]
        for i, row in enumerate(rows):
            events_json = row.get("events_json", "[]")
            events = json.loads(events_json)
            for j, ev in enumerate(events):
                for field in ("sol_reserve_lamports", "token_reserve_base"):
                    val = ev.get(field)
                    if val is not None:
                        assert isinstance(val, int) and not isinstance(val, bool), (
                            f"row[{i}].events[{j}].{field}={val!r} is not int "
                            "(money fields must be integer lamports/base-units)"
                        )

    def test_completeness_status_valid(self):
        """completeness_status must be 'complete' or 'CENSORED'."""
        stats = _run_shadow(max_events=25)
        rows = stats["_rows"]
        assert rows
        valid = {"complete", "CENSORED"}
        for i, row in enumerate(rows):
            assert row["completeness_status"] in valid, (
                f"row[{i}] invalid completeness_status={row['completeness_status']!r}"
            )


# ---------------------------------------------------------------------------
# Multi-mint coverage
# ---------------------------------------------------------------------------


class TestMultiMintCoverage:
    """Verify that multiple unique mints are captured."""

    def test_multiple_mints_captured(self):
        """The demo set covers pump.fun, PumpSwap, Raydium v4, and CPMM mints."""
        stats = _run_shadow(max_events=50, first_k_slots=5)
        rows = stats["_rows"]
        assert rows, "No snapshot rows"
        mints = {row["mint"] for row in rows}
        # We expect at least 3 distinct mints from the demo transaction set
        assert len(mints) >= 3, (
            f"Expected >= 3 distinct mints in corpus, got {len(mints)}: {mints}"
        )

    def test_event_count_per_snapshot_positive(self):
        """Each snapshot must have at least 1 event."""
        stats = _run_shadow(max_events=50)
        rows = stats["_rows"]
        for i, row in enumerate(rows):
            assert row["event_count"] >= 1, (
                f"row[{i}] event_count={row['event_count']} — expected >= 1"
            )


# ---------------------------------------------------------------------------
# Stats dict contract
# ---------------------------------------------------------------------------


class TestStatsDict:
    """The stats dict returned by _run() must have the correct keys."""

    def test_stats_keys_present(self):
        stats = _run_shadow(max_events=5)
        required_keys = {
            "source", "events_decoded", "events_skipped", "decode_errors",
            "snapshots_recorded", "corpus_path", "elapsed_ms", "data_staleness_ms",
        }
        missing = required_keys - set(stats.keys())
        assert not missing, f"Stats dict missing keys: {missing}"

    def test_stats_source_is_replay(self):
        stats = _run_shadow(max_events=5)
        assert stats["source"] == "replay"

    def test_stats_elapsed_ms_non_negative(self):
        stats = _run_shadow(max_events=5)
        assert stats["elapsed_ms"] >= 0
