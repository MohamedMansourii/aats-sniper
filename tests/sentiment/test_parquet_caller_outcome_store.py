"""EN5 — ParquetCallerOutcomeStore: production CallerOutcomeStore backend.

Self-check items verified here:
  EN5-A: Offline / missing-path behaviour — unset, placeholder, non-existent,
         or malformed-schema paths never raise; they behave exactly like an
         empty InMemoryCallerOutcomeStore (neutral, no caller signal).
  EN5-B: POINT-IN-TIME (the mandated self-check) — outcomes whose
         `outcome_event_time_ms` resolves AFTER `as_of_ms` are dropped from
         get_outcomes()/get_all_caller_ids()/get_universe_accuracy(), and a
         direct diff of scores WITH vs. WITHOUT the future row proves zero
         lookahead effect.
  EN5-C: Malformed rows (outcome_event_time_ms <= call_event_time_ms, a
         CallerCallOutcome contract violation) are dropped, never trusted,
         never crash the store.
  EN5-D: Protocol parity — ParquetCallerOutcomeStore and
         InMemoryCallerOutcomeStore produce IDENTICAL CallerScore output for
         identical underlying data (drop-in interchangeable, no relaxation
         of the CallerOutcomeStore Protocol).
  EN5-E: reload() picks up newly-appended rows; without it the cached read
         is stable (no surprise re-reads mid-computation).
  EN5-F: Directory-of-partitioned-parquet-files dataset layout is supported.
  EN5-G: No win_rate field/computation anywhere (grep-assert).
  EN5-H: No network calls — pure local filesystem read.

GOLDEN FIXTURES (deterministic, no wall-clock):
  All outcome rows are built via `_row()` with fixed integer millisecond
  timestamps anchored to DECISION_TIME_MS.
"""

from __future__ import annotations

import inspect

import pandas as pd
import pytest

from aats.sentiment.caller_score import (
    DEFAULT_UNIVERSE_ACCURACY,
    PARQUET_OUTCOME_REQUIRED_COLUMNS,
    CallerCallOutcome,
    CallerTrackRecordScorer,
    InMemoryCallerOutcomeStore,
    ParquetCallerOutcomeStore,
)

# ---------------------------------------------------------------------------
# Time anchors (deterministic — no wall-clock)
# ---------------------------------------------------------------------------

DECISION_TIME_MS: int = 1_718_500_000_000  # 2024-06-16T00:00:00Z
ONE_HOUR_MS = 3_600_000
ONE_DAY_MS = 86_400_000

T_MINUS_3D = DECISION_TIME_MS - 3 * ONE_DAY_MS
T_MINUS_2D = DECISION_TIME_MS - 2 * ONE_DAY_MS
T_MINUS_1D = DECISION_TIME_MS - ONE_DAY_MS
T_PLUS_1H = DECISION_TIME_MS + ONE_HOUR_MS


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _row(
    caller_id: str,
    call_t: int,
    outcome_t: int,
    correct: bool,
    asset: str = "M1ntTestAssetxxxxxxxxxxxxxxxxxxxxxxxxxx",
) -> dict:
    return {
        "caller_id": caller_id,
        "call_event_time_ms": call_t,
        "outcome_event_time_ms": outcome_t,
        "asset": asset,
        "outcome_correct": correct,
    }


def _write_parquet(path, rows: list[dict]) -> None:
    pd.DataFrame(rows).to_parquet(path)


# ---------------------------------------------------------------------------
# EN5-A: Offline / missing-path behaviour — never raises, always neutral
# ---------------------------------------------------------------------------


class TestOfflineMissingPath:
    """EN5-A: unset/placeholder/missing/malformed inputs never raise."""

    def test_none_path_returns_empty(self) -> None:
        store = ParquetCallerOutcomeStore(path=None)
        assert store.get_outcomes("caller_x", DECISION_TIME_MS) == []
        assert store.get_all_caller_ids(DECISION_TIME_MS) == []
        assert store.get_universe_accuracy(DECISION_TIME_MS) == DEFAULT_UNIVERSE_ACCURACY

    def test_nonexistent_path_returns_empty(self, tmp_path) -> None:
        missing = tmp_path / "does_not_exist.parquet"
        store = ParquetCallerOutcomeStore(path=str(missing))
        assert store.get_outcomes("caller_x", DECISION_TIME_MS) == []
        assert store.get_universe_accuracy(DECISION_TIME_MS) == DEFAULT_UNIVERSE_ACCURACY

    def test_from_env_unset_is_offline(self) -> None:
        store = ParquetCallerOutcomeStore.from_env(env={})
        assert store.get_outcomes("caller_x", DECISION_TIME_MS) == []

    def test_from_env_placeholder_is_offline(self) -> None:
        store = ParquetCallerOutcomeStore.from_env(
            env={
                "CALLER_OUTCOME_STORE_PATH": (
                    "<path-to-labels-parquet-e.g. data/labels/caller_outcomes>"
                )
            }
        )
        assert store.get_outcomes("caller_x", DECISION_TIME_MS) == []

    def test_from_env_blank_is_offline(self) -> None:
        store = ParquetCallerOutcomeStore.from_env(env={"CALLER_OUTCOME_STORE_PATH": "   "})
        assert store.get_outcomes("caller_x", DECISION_TIME_MS) == []

    def test_from_env_real_path_reads_data(self, tmp_path) -> None:
        path = tmp_path / "real.parquet"
        _write_parquet(path, [_row("caller_env", T_MINUS_3D, T_MINUS_2D, True)])
        store = ParquetCallerOutcomeStore.from_env(
            env={"CALLER_OUTCOME_STORE_PATH": str(path)}
        )
        outcomes = store.get_outcomes("caller_env", DECISION_TIME_MS)
        assert len(outcomes) == 1

    def test_missing_column_returns_empty(self, tmp_path) -> None:
        path = tmp_path / "bad_schema.parquet"
        pd.DataFrame([{"caller_id": "x", "call_event_time_ms": 1}]).to_parquet(path)
        store = ParquetCallerOutcomeStore(path=str(path))
        assert store.get_outcomes("x", DECISION_TIME_MS) == []
        assert store.get_universe_accuracy(DECISION_TIME_MS) == DEFAULT_UNIVERSE_ACCURACY

    def test_empty_dataset_returns_empty(self, tmp_path) -> None:
        path = tmp_path / "empty.parquet"
        _write_parquet(path, [])
        store = ParquetCallerOutcomeStore(path=str(path))
        assert store.get_outcomes("x", DECISION_TIME_MS) == []


# ---------------------------------------------------------------------------
# EN5-B: POINT-IN-TIME — future outcomes dropped (mandated self-check)
# ---------------------------------------------------------------------------


class TestPointInTimeFilterDropsFutureOutcomes:
    """EN5-B: outcome_event_time_ms > as_of_ms rows must never contribute."""

    def test_future_outcome_excluded_from_get_outcomes(self, tmp_path) -> None:
        path = tmp_path / "outcomes.parquet"
        rows = [
            _row("caller_a", T_MINUS_3D, T_MINUS_2D, True),  # resolved in the past — included
            _row("caller_a", T_MINUS_1D, T_PLUS_1H, True),  # resolves in the FUTURE — excluded
        ]
        _write_parquet(path, rows)
        store = ParquetCallerOutcomeStore(path=str(path))

        outcomes = store.get_outcomes("caller_a", DECISION_TIME_MS)
        assert len(outcomes) == 1, (
            f"PIT LEAK: expected 1 resolved outcome, got {len(outcomes)}. "
            "Future outcome (outcome_event_time_ms > decision_time_ms) leaked in."
        )
        assert outcomes[0].outcome_event_time_ms == T_MINUS_2D

    def test_diff_scores_with_and_without_future_outcome(self, tmp_path) -> None:
        """Prove no lookahead by diffing scores WITH vs. WITHOUT the future row."""
        path_without = tmp_path / "without_future.parquet"
        path_with = tmp_path / "with_future.parquet"

        past_only = [_row("caller_b", T_MINUS_3D, T_MINUS_2D, False)]
        with_future = past_only + [_row("caller_b", T_MINUS_1D, T_PLUS_1H, True)]

        _write_parquet(path_without, past_only)
        _write_parquet(path_with, with_future)

        store_without = ParquetCallerOutcomeStore(path=str(path_without))
        store_with = ParquetCallerOutcomeStore(path=str(path_with))

        scorer_without = CallerTrackRecordScorer(
            store_without, universe_accuracy=DEFAULT_UNIVERSE_ACCURACY
        )
        scorer_with = CallerTrackRecordScorer(
            store_with, universe_accuracy=DEFAULT_UNIVERSE_ACCURACY
        )

        score_without = scorer_without.score_caller("caller_b", DECISION_TIME_MS)
        score_with = scorer_with.score_caller("caller_b", DECISION_TIME_MS)

        print(
            f"\n[EN5-B] without_future: calls={score_without.call_count} "
            f"acc={score_without.accuracy_raw:.4f} weight={score_without.selectivity_weight:.4f}\n"
            f"[EN5-B] with_future:    calls={score_with.call_count} "
            f"acc={score_with.accuracy_raw:.4f} weight={score_with.selectivity_weight:.4f}"
        )

        # The future row must have ZERO effect on the score — identical results.
        assert score_without.call_count == score_with.call_count == 1, (
            "POINT-IN-TIME VIOLATED: the future outcome changed the effective call count."
        )
        assert score_without.accuracy_raw == pytest.approx(score_with.accuracy_raw, abs=1e-9), (
            "POINT-IN-TIME VIOLATED: future outcome changed accuracy_raw."
        )
        assert score_without.selectivity_weight == pytest.approx(
            score_with.selectivity_weight, abs=1e-9
        ), "POINT-IN-TIME VIOLATED: future outcome changed selectivity_weight."

    def test_get_all_caller_ids_excludes_future_only_caller(self, tmp_path) -> None:
        path = tmp_path / "future_only.parquet"
        rows = [_row("caller_future_only", T_MINUS_1D, T_PLUS_1H, True)]
        _write_parquet(path, rows)
        store = ParquetCallerOutcomeStore(path=str(path))
        assert store.get_all_caller_ids(DECISION_TIME_MS) == [], (
            "A caller whose ONLY outcome resolves in the future must not appear "
            "in get_all_caller_ids() at decision_time_ms."
        )

    def test_universe_accuracy_excludes_future_outcomes(self, tmp_path) -> None:
        path = tmp_path / "universe.parquet"
        rows = [
            _row("caller_c", T_MINUS_3D, T_MINUS_2D, True),
            _row("caller_d", T_MINUS_3D, T_MINUS_2D, True),
            _row("caller_e", T_MINUS_1D, T_PLUS_1H, False),  # future — must not count
        ]
        _write_parquet(path, rows)
        store = ParquetCallerOutcomeStore(path=str(path))
        # If the future row leaked in, accuracy would be 2/3; without it, 2/2 = 1.0.
        acc = store.get_universe_accuracy(DECISION_TIME_MS)
        assert acc == pytest.approx(1.0, abs=1e-9), (
            f"Universe accuracy leaked a future outcome: expected 1.0, got {acc:.4f}."
        )

    def test_boundary_outcome_at_decision_time_is_included(self, tmp_path) -> None:
        """An outcome resolved exactly at decision_time_ms must be included (inclusive)."""
        path = tmp_path / "boundary.parquet"
        rows = [_row("caller_boundary", T_MINUS_1D, DECISION_TIME_MS, True)]
        _write_parquet(path, rows)
        store = ParquetCallerOutcomeStore(path=str(path))
        outcomes = store.get_outcomes("caller_boundary", DECISION_TIME_MS)
        assert len(outcomes) == 1, "Outcome at exactly decision_time_ms must be included."


# ---------------------------------------------------------------------------
# EN5-C: Malformed rows (PIT contract violation) are dropped, never crash
# ---------------------------------------------------------------------------


class TestMalformedRowsDropped:
    """EN5-C: outcome_event_time_ms <= call_event_time_ms rows are silently dropped."""

    def test_pit_violating_rows_are_dropped_not_raised(self, tmp_path) -> None:
        path = tmp_path / "malformed.parquet"
        rows = [
            _row("caller_bad", T_MINUS_1D, T_MINUS_1D - 1_000, True),  # outcome BEFORE call
            _row("caller_bad", T_MINUS_3D, T_MINUS_3D, False),  # outcome == call (not strictly after)
            _row("caller_bad", T_MINUS_3D, T_MINUS_2D, True),  # the one VALID row
        ]
        _write_parquet(path, rows)
        store = ParquetCallerOutcomeStore(path=str(path))
        outcomes = store.get_outcomes("caller_bad", DECISION_TIME_MS)
        assert len(outcomes) == 1, (
            f"Only the single valid (outcome strictly after call) row should survive. "
            f"Got {len(outcomes)}."
        )
        assert outcomes[0].outcome_event_time_ms == T_MINUS_2D


class TestMalformedCellsDropped:
    """EN5-C (cell-level): a null/NaN cell in a required column is dropped
    per-row and never crashes the entire read (a single corrupt label must
    not take down every other resolved outcome in the dataset)."""

    def test_nan_timestamp_cell_dropped_not_raised(self, tmp_path) -> None:
        path = tmp_path / "nan_timestamp.parquet"
        rows = [
            _row("caller_nan", T_MINUS_3D, T_MINUS_2D, True),  # valid
            {
                "caller_id": "caller_nan",
                "call_event_time_ms": None,  # becomes NaN once pandas casts the column
                "outcome_event_time_ms": T_MINUS_1D,
                "asset": "M1ntTestAssetxxxxxxxxxxxxxxxxxxxxxxxxxx",
                "outcome_correct": True,
            },
        ]
        _write_parquet(path, rows)
        store = ParquetCallerOutcomeStore(path=str(path))
        # Must not raise: the NaN-timestamp row is dropped, the valid row survives.
        outcomes = store.get_outcomes("caller_nan", DECISION_TIME_MS)
        assert len(outcomes) == 1, (
            f"Expected the NaN-timestamp row to be dropped and the valid row to "
            f"survive. Got {len(outcomes)} outcome(s)."
        )
        assert outcomes[0].outcome_event_time_ms == T_MINUS_2D

    def test_null_outcome_correct_cell_dropped_not_raised(self, tmp_path) -> None:
        path = tmp_path / "null_bool.parquet"
        rows = [
            _row("caller_nullbool", T_MINUS_3D, T_MINUS_2D, True),  # valid
            {
                "caller_id": "caller_nullbool",
                "call_event_time_ms": T_MINUS_3D,
                "outcome_event_time_ms": T_MINUS_1D,
                "asset": "M1ntTestAssetxxxxxxxxxxxxxxxxxxxxxxxxxx",
                "outcome_correct": None,
            },
        ]
        _write_parquet(path, rows)
        store = ParquetCallerOutcomeStore(path=str(path))
        outcomes = store.get_outcomes("caller_nullbool", DECISION_TIME_MS)
        assert len(outcomes) == 1, (
            f"Expected the null outcome_correct row to be dropped and the valid "
            f"row to survive. Got {len(outcomes)} outcome(s)."
        )

    def test_nan_cells_scattered_across_dataset_do_not_crash_universe_accuracy(
        self, tmp_path
    ) -> None:
        """A malformed cell anywhere in the dataset must never crash
        get_universe_accuracy() / get_all_caller_ids() either — same guarantee
        as get_outcomes()."""
        path = tmp_path / "scattered_nan.parquet"
        rows = [
            _row("caller_x", T_MINUS_3D, T_MINUS_2D, True),
            _row("caller_y", T_MINUS_3D, T_MINUS_2D, False),
            {
                "caller_id": "caller_z",
                "call_event_time_ms": None,
                "outcome_event_time_ms": T_MINUS_1D,
                "asset": "M1ntTestAssetxxxxxxxxxxxxxxxxxxxxxxxxxx",
                "outcome_correct": True,
            },
        ]
        _write_parquet(path, rows)
        store = ParquetCallerOutcomeStore(path=str(path))
        # Must not raise from any of the three protocol methods.
        assert store.get_universe_accuracy(DECISION_TIME_MS) == pytest.approx(0.5, abs=1e-9)
        assert set(store.get_all_caller_ids(DECISION_TIME_MS)) == {"caller_x", "caller_y"}


# ---------------------------------------------------------------------------
# EN5-D: Protocol parity — drop-in interchangeable with InMemoryCallerOutcomeStore
# ---------------------------------------------------------------------------


class TestParityWithInMemoryStore:
    """EN5-D: identical underlying data => identical CallerScore output."""

    def test_parity_with_in_memory_store_for_identical_data(self, tmp_path) -> None:
        outcomes = [
            CallerCallOutcome(
                caller_id="caller_parity",
                call_event_time_ms=T_MINUS_3D + i * ONE_HOUR_MS,
                outcome_event_time_ms=T_MINUS_3D + i * ONE_HOUR_MS + ONE_HOUR_MS,
                asset="M1ntParityxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
                outcome_correct=(i % 3 != 0),
            )
            for i in range(10)
        ]
        rows = [
            _row(o.caller_id, o.call_event_time_ms, o.outcome_event_time_ms, o.outcome_correct, o.asset)
            for o in outcomes
        ]
        path = tmp_path / "parity.parquet"
        _write_parquet(path, rows)

        parquet_store = ParquetCallerOutcomeStore(path=str(path))
        memory_store = InMemoryCallerOutcomeStore(outcomes=outcomes)

        scorer_parquet = CallerTrackRecordScorer(
            parquet_store, universe_accuracy=DEFAULT_UNIVERSE_ACCURACY
        )
        scorer_memory = CallerTrackRecordScorer(
            memory_store, universe_accuracy=DEFAULT_UNIVERSE_ACCURACY
        )

        score_parquet = scorer_parquet.score_caller("caller_parity", DECISION_TIME_MS)
        score_memory = scorer_memory.score_caller("caller_parity", DECISION_TIME_MS)

        print(
            f"\n[EN5-D] parquet: calls={score_parquet.call_count} "
            f"acc={score_parquet.accuracy_raw:.4f} weight={score_parquet.selectivity_weight:.4f}\n"
            f"[EN5-D] memory:  calls={score_memory.call_count} "
            f"acc={score_memory.accuracy_raw:.4f} weight={score_memory.selectivity_weight:.4f}"
        )

        assert score_parquet.call_count == score_memory.call_count
        assert score_parquet.accuracy_raw == pytest.approx(score_memory.accuracy_raw, abs=1e-9)
        assert score_parquet.accuracy_delta == pytest.approx(score_memory.accuracy_delta, abs=1e-9)
        assert score_parquet.selectivity_weight == pytest.approx(
            score_memory.selectivity_weight, abs=1e-9
        )
        assert score_parquet.is_negative == score_memory.is_negative
        assert score_parquet.is_thin_data == score_memory.is_thin_data

    def test_parity_get_all_caller_ids(self, tmp_path) -> None:
        outcomes = [
            CallerCallOutcome("caller_1", T_MINUS_3D, T_MINUS_2D, "M1ntA", True),
            CallerCallOutcome("caller_2", T_MINUS_3D, T_MINUS_2D, "M1ntA", False),
            CallerCallOutcome("caller_3", T_MINUS_1D, T_PLUS_1H, "M1ntA", True),  # future
        ]
        rows = [
            _row(o.caller_id, o.call_event_time_ms, o.outcome_event_time_ms, o.outcome_correct, o.asset)
            for o in outcomes
        ]
        path = tmp_path / "ids.parquet"
        _write_parquet(path, rows)

        parquet_store = ParquetCallerOutcomeStore(path=str(path))
        memory_store = InMemoryCallerOutcomeStore(outcomes=outcomes)

        assert set(parquet_store.get_all_caller_ids(DECISION_TIME_MS)) == set(
            memory_store.get_all_caller_ids(DECISION_TIME_MS)
        )


# ---------------------------------------------------------------------------
# EN5-E: reload() semantics
# ---------------------------------------------------------------------------


class TestReload:
    """EN5-E: cached rows are stable until reload() is called explicitly."""

    def test_reload_picks_up_new_rows(self, tmp_path) -> None:
        path = tmp_path / "reloadable.parquet"
        _write_parquet(path, [_row("caller_r", T_MINUS_3D, T_MINUS_2D, True)])
        store = ParquetCallerOutcomeStore(path=str(path))
        assert len(store.get_outcomes("caller_r", DECISION_TIME_MS)) == 1

        # Simulate the clean-room harness appending new labels.
        _write_parquet(
            path,
            [
                _row("caller_r", T_MINUS_3D, T_MINUS_2D, True),
                _row("caller_r", T_MINUS_2D, T_MINUS_1D, True),
            ],
        )
        # Without reload(), the cached read is stable (still 1).
        assert len(store.get_outcomes("caller_r", DECISION_TIME_MS)) == 1, (
            "Store must not silently re-read on every call (cache is load-bearing)."
        )
        store.reload()
        assert len(store.get_outcomes("caller_r", DECISION_TIME_MS)) == 2, (
            "reload() must force a fresh read from disk."
        )

    def test_preload_true_loads_eagerly(self, tmp_path) -> None:
        path = tmp_path / "preload.parquet"
        _write_parquet(path, [_row("caller_p", T_MINUS_3D, T_MINUS_2D, True)])
        store = ParquetCallerOutcomeStore(path=str(path), preload=True)
        # Internal cache should already be populated (no public API to check
        # directly except by observing behaviour is unaffected by deleting
        # the file after construction).
        import os as _os

        _os.remove(path)
        # Cache was already warmed at construction; query still succeeds.
        outcomes = store.get_outcomes("caller_p", DECISION_TIME_MS)
        assert len(outcomes) == 1, "preload=True must read eagerly at construction time."


# ---------------------------------------------------------------------------
# EN5-F: directory-of-partitioned-parquet-files dataset layout
# ---------------------------------------------------------------------------


class TestDirectoryDataset:
    """EN5-F: CALLER_OUTCOME_STORE_PATH may be a directory of parquet parts."""

    def test_reads_directory_of_partitioned_parquet_files(self, tmp_path) -> None:
        rows_a = [_row("caller_dir", T_MINUS_3D, T_MINUS_2D, True)]
        rows_b = [_row("caller_dir", T_MINUS_2D, T_MINUS_1D, False)]
        pd.DataFrame(rows_a).to_parquet(tmp_path / "part-0.parquet")
        pd.DataFrame(rows_b).to_parquet(tmp_path / "part-1.parquet")

        store = ParquetCallerOutcomeStore(path=str(tmp_path))
        outcomes = store.get_outcomes("caller_dir", DECISION_TIME_MS)
        assert len(outcomes) == 2


# ---------------------------------------------------------------------------
# EN5-G: No win_rate field/computation anywhere (grep-assert)
# ---------------------------------------------------------------------------


class TestNoWinRate:
    """EN5-G: 'win_rate' must not appear anywhere in caller_score.py source."""

    def test_no_win_rate_in_module_source(self) -> None:
        import aats.sentiment.caller_score as caller_mod

        source = inspect.getsource(caller_mod)
        assert "win_rate" not in source, (
            "HARD RULE VIOLATED: 'win_rate' found in aats.sentiment.caller_score source. "
            "ParquetCallerOutcomeStore must expose accuracy DELTA vs. the universe "
            "baseline, NEVER a win-rate headline."
        )

    def test_required_columns_constant_has_no_win_rate(self) -> None:
        assert "win_rate" not in PARQUET_OUTCOME_REQUIRED_COLUMNS


# ---------------------------------------------------------------------------
# EN5-H: No network calls — pure local filesystem read
# ---------------------------------------------------------------------------


class TestNoNetworkCalls:
    """EN5-H: ParquetCallerOutcomeStore never touches the network."""

    def test_no_network_libraries_referenced(self) -> None:
        import aats.sentiment.caller_score as caller_mod

        source = inspect.getsource(caller_mod)
        for forbidden in ("httpx", "requests.", "asyncpraw", "telethon", "socket.", "aiohttp"):
            assert forbidden not in source, (
                f"caller_score.py source references {forbidden!r} — "
                "ParquetCallerOutcomeStore must be a pure local-filesystem reader, "
                "no network calls."
            )

    def test_constructor_and_from_env_do_not_require_credentials(self) -> None:
        sig = inspect.signature(ParquetCallerOutcomeStore.__init__)
        params = list(sig.parameters)
        assert params == ["self", "path", "preload"], (
            f"ParquetCallerOutcomeStore.__init__ must accept only a local path "
            f"(+ preload flag) — no adapter/token/credential params. Got: {params}"
        )
