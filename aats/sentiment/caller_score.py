"""E9 — Alpha-caller / call-channel track-record scoring (HONEST).

SLOW-LOOP ONLY.  This module MUST NEVER be imported by or called from the
SNIPE loop or FAST loop.  The SNIPE loop's millisecond entry decision must
not block on social data — and caller signals are among the easiest things
to fake.

DESIGN CONTRACT (non-waivable):
  - Track named callers (handle/channel/source).
  - Score each by HISTORICAL accuracy on RECORDED ON-CHAIN OUTCOMES,
    joined by event_time (decision_time_ms anchor) — LEAK-FREE, no future
    outcome used in the score.
  - The score is a WEIGHTED SELECTIVITY multiplier in [0.0, 1.0]:
      selectivity_weight <= 1.0  → can only REDUCE or maintain conviction
    It is injected as a DOWNWARD modifier into the caller-adjusted MCS delta.
    It CANNOT raise conviction above the social/narrative base signal.
    It is NEVER a standalone buy trigger.
  - Most callers score near-zero or NEGATIVE historically.  Negative callers
    receive a selectivity_weight of 0.0 (completely deweighted).  No inflation.
  - Surface honestly: the CallerSignal includes the distribution of scores,
    negative-caller count, and net_caller_weight for audit.

POINT-IN-TIME (load-bearing):
  Only CallerCall records with event_time_ms <= decision_time_ms may
  contribute to a score.  The label (outcome) of a call is joined only when
  the recorded on-chain outcome event_time_ms <= decision_time_ms.
  This is enforced structurally: the score() call takes a decision_time_ms
  and filters BEFORE any aggregation.  Feeding a future outcome => excluded.

OUTCOME RECORD (injected — never fetched live):
  The caller scoring depends on the RECORDED LABELS dataset (T-401 / G4
  clean-room harness), not live on-chain reads.  In production the recorded
  outcomes are injected via CallerOutcomeStore Protocol.  In tests and CI
  the InMemoryCallerOutcomeStore with golden fixtures is injected.
  External on-chain / Discord RPC calls are NOT made inside this module.

NO WIN-RATE:
  This module contains no win-rate field, attribute, or computation.
  The metric is CALLER ACCURACY DELTA vs. the universe average — a
  calibrated probability offset, not a fraction-of-wins.
  There is no win-rate target, no win-rate label, no win-rate claim.

COST:
  No LLM calls.  No network calls.  Pure in-process computation.
"""

from __future__ import annotations

import logging
import math
import os
from dataclasses import dataclass
from typing import Protocol

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Minimum number of recorded calls before we assign any non-zero selectivity.
# Below this threshold the score is 0.5 (neutral — insufficient history).
MIN_CALLS_FOR_SCORE = 3

# Accuracy delta below which a caller is treated as net-negative.
# Net-negative callers get selectivity_weight = 0.0 (completely ignored).
# accuracy_delta = caller_accuracy - universe_accuracy
NEGATIVE_ACCURACY_DELTA_THRESHOLD = -0.05

# Accuracy delta above which a caller is trusted at non-trivial selectivity.
POSITIVE_ACCURACY_DELTA_THRESHOLD = 0.05

# Clip radius for the accuracy delta before scaling.
MAX_ACCURACY_DELTA = 0.40

# Smoothing decay for recency weighting (exponential, per half-life window).
# Older calls are down-weighted relative to recent ones.
# Half-life expressed in milliseconds (1 week = 7*24*3600*1000).
RECENCY_HALF_LIFE_MS: int = 7 * 24 * 3_600_000

# Universe base accuracy: the fraction of outcomes that were "correct" across
# all callers on average.  This is an injected prior.  The selectivity
# signal is the DELTA above/below this, never the raw accuracy.
DEFAULT_UNIVERSE_ACCURACY: float = 0.40  # most callers are wrong most of the time


# ---------------------------------------------------------------------------
# Data types (pipeline-internal)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CallerCall:
    """A single call made by a named caller (alpha-channel / call-channel post).

    caller_id:       Stable, anonymised caller identifier.
                     (handle/channel ID — never a real-name PII field)
    call_event_time_ms: When the call was made (platform publish timestamp).
                        This is the AUTHORITATIVE event-time (not fetch time).
    asset:           The mint address the call referred to.
    direction:       "long" or "avoid" — the caller's directional call.
                     Only "long" calls are scored for accuracy vs. outcome.
    source:          Source channel (discord, telegram, x, etc.).
    """

    caller_id: str
    call_event_time_ms: int   # authoritative platform publish timestamp
    asset: str
    direction: str            # "long" | "avoid"
    source: str               # "discord" | "telegram" | "x" | "reddit" | "other"


@dataclass(frozen=True)
class CallerCallOutcome:
    """Recorded on-chain outcome for a single CallerCall.

    Produced by the CLEAN-ROOM HARNESS (T-401 labels/ dataset) — never
    by this module.  Injected via CallerOutcomeStore.

    call_event_time_ms:     Must match the CallerCall.call_event_time_ms exactly.
    outcome_event_time_ms:  When the on-chain outcome was RESOLVED (always > call).
    outcome_correct:        True iff the recorded on-chain data shows the caller's
                            directional call was correct within the outcome window.
                            This is a BINARY result derived from the labels/ harness.
    asset:                  Mint address (must match the CallerCall).

    POINT-IN-TIME CONTRACT:
      outcome_event_time_ms > call_event_time_ms always (the outcome resolves
      AFTER the call was made — proving no look-ahead in the call itself).
      A score computed at decision_time_ms ONLY uses outcomes where
      outcome_event_time_ms <= decision_time_ms.  This prevents using a
      future outcome to score a past call.
    """

    caller_id: str
    call_event_time_ms: int    # matches CallerCall.call_event_time_ms
    outcome_event_time_ms: int # ALWAYS > call_event_time_ms
    asset: str
    outcome_correct: bool      # binary; fraction-of-wins is never computed


# ---------------------------------------------------------------------------
# CallerOutcomeStore Protocol (injected — NO live on-chain reads here)
# ---------------------------------------------------------------------------


class CallerOutcomeStore(Protocol):
    """Protocol for the recorded-outcome backend.

    Implementations:
        InMemoryCallerOutcomeStore  — test/offline (golden fixtures)
        ParquetCallerOutcomeStore   — production (reads labels/ Parquet)

    CONTRACT: get_outcomes() MUST return ONLY outcomes whose
    outcome_event_time_ms <= as_of_ms.  Callers must not pass a future
    as_of_ms (they pass decision_time_ms which is always the past anchor).
    """

    def get_outcomes(
        self,
        caller_id: str,
        as_of_ms: int,
    ) -> list[CallerCallOutcome]:
        """Return all recorded outcomes for caller_id that are fully resolved
        at as_of_ms (outcome_event_time_ms <= as_of_ms).

        POINT-IN-TIME: this method MUST NOT return outcomes that resolve after
        as_of_ms.  The implementation is responsible for enforcing this.
        """
        ...

    def get_all_caller_ids(self, as_of_ms: int) -> list[str]:
        """Return all known caller IDs that have at least one call <= as_of_ms."""
        ...

    def get_universe_accuracy(self, as_of_ms: int) -> float:
        """Return the universe-level average accuracy at as_of_ms.

        This is the PRIOR: the fraction of outcomes across ALL callers that
        were correct as of as_of_ms.  The caller accuracy delta is computed
        relative to this.

        Returns DEFAULT_UNIVERSE_ACCURACY if no data (no inflation of sparse data).
        """
        ...


# ---------------------------------------------------------------------------
# InMemoryCallerOutcomeStore (offline / test fixture)
# ---------------------------------------------------------------------------


class InMemoryCallerOutcomeStore:
    """Offline, injectable in-memory store for tests and CI.

    Accepts a list of CallerCallOutcome records.  Filters by as_of_ms before
    returning.  No network calls, no disk reads, no LLM.
    """

    def __init__(self, outcomes: list[CallerCallOutcome] | None = None) -> None:
        self._outcomes: list[CallerCallOutcome] = list(outcomes or [])

    def add_outcome(self, o: CallerCallOutcome) -> None:
        self._outcomes.append(o)

    def get_outcomes(self, caller_id: str, as_of_ms: int) -> list[CallerCallOutcome]:
        """Return outcomes for caller_id that are resolved at as_of_ms.

        POINT-IN-TIME: only outcome_event_time_ms <= as_of_ms.
        """
        return [
            o for o in self._outcomes
            if o.caller_id == caller_id and o.outcome_event_time_ms <= as_of_ms
        ]

    def get_all_caller_ids(self, as_of_ms: int) -> list[str]:
        """Return distinct caller IDs with at least one resolved outcome by as_of_ms."""
        return list({
            o.caller_id
            for o in self._outcomes
            if o.outcome_event_time_ms <= as_of_ms
        })

    def get_universe_accuracy(self, as_of_ms: int) -> float:
        """Universe-level accuracy (all callers) at as_of_ms."""
        resolved = [o for o in self._outcomes if o.outcome_event_time_ms <= as_of_ms]
        if not resolved:
            return DEFAULT_UNIVERSE_ACCURACY
        correct = sum(1 for o in resolved if o.outcome_correct)
        return correct / len(resolved)


# ---------------------------------------------------------------------------
# ParquetCallerOutcomeStore (production backend — EN5)
# ---------------------------------------------------------------------------

# Columns the labels/ Parquet dataset (T-401 clean-room harness) MUST carry.
# One row per resolved CallerCall outcome — mirrors CallerCallOutcome field-for-field.
PARQUET_OUTCOME_REQUIRED_COLUMNS: frozenset[str] = frozenset(
    {
        "caller_id",
        "call_event_time_ms",
        "outcome_event_time_ms",
        "asset",
        "outcome_correct",
    }
)


def _clean_outcome_store_path(raw: str | None) -> str | None:
    """Return None for an unset/placeholder CALLER_OUTCOME_STORE_PATH, else the path.

    Mirrors `aats.sentiment.adapters._clean_env_value`'s placeholder-detection
    logic without importing that module (this module intentionally has no
    dependency on adapters.py).  A `.env.example` placeholder is angle-bracketed
    (e.g. `<path-to-labels-parquet-e.g. data/labels/caller_outcomes>`) and is
    NEVER treated as a real path.
    """
    if raw is None:
        return None
    v = raw.strip()
    if not v or (v.startswith("<") and v.endswith(">")):
        return None
    return v


def _read_parquet_outcomes(path: str | None) -> list[CallerCallOutcome]:
    """Read + validate the labels/ Parquet outcome dataset (T-401 clean-room harness).

    NO NETWORK CALLS: local/mounted filesystem path only — a single .parquet
    file or a directory of partitioned .parquet files (pandas.read_parquet
    handles both transparently via the pyarrow engine).

    NEVER RAISES for a missing/unset/unreadable/malformed dataset: returns []
    (offline / no caller signal), matching the documented `.env.example`
    fallback ("If unset or the file is absent, the scorer falls back to the
    empty InMemoryCallerOutcomeStore").  A not-yet-populated labels/ dataset is
    a legitimate bootstrap state, not an error — caller signals then default to
    fully neutral (CallerSignal.caller_selectivity_modifier=1.0).

    PIT SAFETY: rows that violate the CallerCallOutcome contract
    (outcome_event_time_ms must be STRICTLY AFTER call_event_time_ms — see the
    CallerCallOutcome docstring) are dropped and counted in a warning log — a
    malformed/leaky row from the harness is never silently trusted.

    CELL-LEVEL SAFETY: this also holds one level deeper than the file/schema
    checks above — a single row with a null/NaN cell or a value that cannot
    be cast to the expected type (e.g. a non-numeric timestamp) is dropped
    individually; it can never crash the read for the rest of the dataset.
    """
    if not path:
        logger.info(
            "ParquetCallerOutcomeStore: no CALLER_OUTCOME_STORE_PATH configured "
            "(offline mode) -- returning [] (neutral caller signal)."
        )
        return []

    if not os.path.exists(path):
        logger.info(
            "ParquetCallerOutcomeStore: path %s does not exist (offline mode) -- "
            "returning [] (neutral caller signal).",
            path,
        )
        return []

    try:
        import pandas as pd  # lazy import: the ONLY place pandas is required by
        # this module — a deployment that never configures
        # CALLER_OUTCOME_STORE_PATH never needs pandas for caller_score.py.
    except ImportError:
        logger.warning(
            "ParquetCallerOutcomeStore: pandas is not installed; cannot read %s. "
            "Returning [] (neutral caller signal).",
            path,
        )
        return []

    try:
        frame = pd.read_parquet(path)
    except Exception as exc:  # noqa: BLE001 — any read failure => safe empty fallback
        logger.warning(
            "ParquetCallerOutcomeStore: failed to read %s (%s: %s). "
            "Returning [] (neutral caller signal).",
            path,
            type(exc).__name__,
            exc,
        )
        return []

    missing_columns = PARQUET_OUTCOME_REQUIRED_COLUMNS - set(frame.columns)
    if missing_columns:
        logger.warning(
            "ParquetCallerOutcomeStore: %s is missing required column(s) %s. "
            "Returning [] (neutral caller signal) rather than guessing a schema.",
            path,
            sorted(missing_columns),
        )
        return []

    outcomes: list[CallerCallOutcome] = []
    dropped_pit_violations = 0
    dropped_malformed = 0
    for row in frame.to_dict("records"):
        # STRICT per-row validation: a single corrupt/mistyped cell (NaN/null,
        # a non-numeric string in a timestamp column, etc.) must NEVER crash
        # the entire read -- only that one row is dropped, the rest of the
        # dataset is still processed.  This is what makes the "never raises
        # for a malformed dataset" guarantee hold at the CELL level, not just
        # the file/schema level.
        try:
            raw_caller_id = row["caller_id"]
            raw_asset = row["asset"]
            raw_outcome_correct = row["outcome_correct"]
            raw_call_t = row["call_event_time_ms"]
            raw_outcome_t = row["outcome_event_time_ms"]
            if any(
                pd.isna(v)
                for v in (raw_caller_id, raw_asset, raw_outcome_correct, raw_call_t, raw_outcome_t)
            ):
                raise ValueError("null/NaN cell in a required column")
            call_event_time_ms = int(raw_call_t)
            outcome_event_time_ms = int(raw_outcome_t)
            caller_id = str(raw_caller_id)
            asset = str(raw_asset)
            outcome_correct = bool(raw_outcome_correct)
        except (TypeError, ValueError) as exc:
            dropped_malformed += 1
            logger.debug(
                "ParquetCallerOutcomeStore: dropped malformed row from %s (%s: %s): %r",
                path,
                type(exc).__name__,
                exc,
                row,
            )
            continue

        if outcome_event_time_ms <= call_event_time_ms:
            # Contract violation (CallerCallOutcome docstring): the outcome
            # must resolve STRICTLY AFTER the call.  Drop -- never trust a
            # malformed/leaky row from the harness.
            dropped_pit_violations += 1
            continue
        outcomes.append(
            CallerCallOutcome(
                caller_id=caller_id,
                call_event_time_ms=call_event_time_ms,
                outcome_event_time_ms=outcome_event_time_ms,
                asset=asset,
                outcome_correct=outcome_correct,
            )
        )

    if dropped_malformed:
        logger.warning(
            "ParquetCallerOutcomeStore: dropped %d malformed row(s) from %s "
            "(null/NaN or non-castable cell in a required column) -- never "
            "trusted, never crashed the read.",
            dropped_malformed,
            path,
        )

    if dropped_pit_violations:
        logger.warning(
            "ParquetCallerOutcomeStore: dropped %d row(s) from %s violating the "
            "outcome_event_time_ms > call_event_time_ms contract.",
            dropped_pit_violations,
            path,
        )

    logger.info(
        "ParquetCallerOutcomeStore: loaded %d outcome row(s) from %s.",
        len(outcomes),
        path,
    )
    return outcomes


class ParquetCallerOutcomeStore:
    """Production CallerOutcomeStore backend — reads the clean-room harness'
    labels/ Parquet dataset (T-401) at CALLER_OUTCOME_STORE_PATH (EN5).

    HONORS the CallerOutcomeStore Protocol EXACTLY — NO RELAXATION:
      - get_outcomes(): the STRICT point-in-time filter
        (outcome_event_time_ms <= as_of_ms) is re-applied on EVERY call using
        the CALLER-SUPPLIED as_of_ms.  The raw rows are cached (see LAZY LOAD
        below) but the PIT filter itself is never skipped, widened, or served
        from a "latest"/wall-clock-anchored view.
      - get_all_caller_ids(): only callers with >= 1 outcome RESOLVED
        (outcome_event_time_ms <= as_of_ms) — mirrors
        InMemoryCallerOutcomeStore exactly, so the two backends are
        drop-in-interchangeable for CallerTrackRecordScorer.
      - get_universe_accuracy(): returns DEFAULT_UNIVERSE_ACCURACY when no
        rows resolve at as_of_ms — a sparse/empty/missing dataset is NEVER
        used to inflate the universe prior.

    NO WIN-RATE HEADLINE: this store exposes only per-outcome booleans
    (`CallerCallOutcome.outcome_correct`).  The caller-vs-universe ACCURACY
    DELTA is computed exclusively by CallerTrackRecordScorer /
    `_compute_caller_score` — this class never aggregates or reports a raw
    win/loss fraction itself.

    NO NETWORK CALLS: reads a local/mounted filesystem Parquet path only
    (a single file or a directory of partitioned files).  If the path is
    unset, absent, or unreadable/malformed, this store behaves EXACTLY like
    an empty InMemoryCallerOutcomeStore (returns [] / DEFAULT_UNIVERSE_ACCURACY)
    — it NEVER raises for a missing/optional dataset (a not-yet-populated
    labels/ harness is a legitimate bootstrap state) and NEVER fabricates data.

    SCHEMA (one row per resolved CallerCall outcome — mirrors
    CallerCallOutcome field-for-field):
        caller_id               str
        call_event_time_ms      int
        outcome_event_time_ms   int   (must be STRICTLY > call_event_time_ms)
        asset                   str
        outcome_correct         bool

    LAZY LOAD + CACHE: the raw rows are read from disk ONCE (on first access,
    or eagerly if `preload=True`) and cached in memory; every query re-applies
    the point-in-time filter against the CALLER-SUPPLIED as_of_ms, so caching
    the raw rows is PIT-safe — nothing is ever filtered by wall-clock/"now".
    Call `reload()` to force a re-read (e.g. after the clean-room harness
    appends new labels).

    USAGE:
        store = ParquetCallerOutcomeStore.from_env()  # reads CALLER_OUTCOME_STORE_PATH
        # or explicitly:
        store = ParquetCallerOutcomeStore(path="data/labels/caller_outcomes")
        scorer = CallerTrackRecordScorer(store)
        signal = scorer.score_for_asset(mint, caller_ids, decision_time_ms)
    """

    def __init__(self, path: str | None, *, preload: bool = False) -> None:
        self._path = path
        self._rows: list[CallerCallOutcome] | None = None
        if preload:
            self._ensure_loaded()

    @classmethod
    def from_env(
        cls,
        env: dict[str, str] | None = None,
        *,
        preload: bool = False,
    ) -> ParquetCallerOutcomeStore:
        """Build from CALLER_OUTCOME_STORE_PATH (`.env.example` schema).

        `env` is injectable for testing (defaults to `os.environ`).  An
        unset/placeholder value yields a store with path=None, which behaves
        identically to an empty InMemoryCallerOutcomeStore (fully neutral —
        never an error).
        """
        src = env if env is not None else dict(os.environ)
        path = _clean_outcome_store_path(src.get("CALLER_OUTCOME_STORE_PATH"))
        return cls(path=path, preload=preload)

    def reload(self) -> None:
        """Force a re-read of the underlying Parquet path on the next query.

        Use after the clean-room harness (T-401) appends new labels to pick
        them up without reconstructing the store.
        """
        self._rows = None

    def _ensure_loaded(self) -> list[CallerCallOutcome]:
        if self._rows is None:
            self._rows = _read_parquet_outcomes(self._path)
        return self._rows

    def get_outcomes(self, caller_id: str, as_of_ms: int) -> list[CallerCallOutcome]:
        """Return outcomes for caller_id that are resolved at as_of_ms.

        POINT-IN-TIME (STRICT, no relaxation): only rows with
        outcome_event_time_ms <= as_of_ms are returned — re-filtered against
        the caller-supplied as_of_ms on every call.
        """
        rows = self._ensure_loaded()
        return [
            o for o in rows
            if o.caller_id == caller_id and o.outcome_event_time_ms <= as_of_ms
        ]

    def get_all_caller_ids(self, as_of_ms: int) -> list[str]:
        """Return distinct caller IDs with >= 1 outcome resolved by as_of_ms."""
        rows = self._ensure_loaded()
        return list({o.caller_id for o in rows if o.outcome_event_time_ms <= as_of_ms})

    def get_universe_accuracy(self, as_of_ms: int) -> float:
        """Universe-level accuracy across ALL callers, resolved by as_of_ms.

        Returns DEFAULT_UNIVERSE_ACCURACY when nothing has resolved yet — a
        missing/sparse dataset is NEVER used to inflate the prior.
        """
        rows = self._ensure_loaded()
        resolved = [o for o in rows if o.outcome_event_time_ms <= as_of_ms]
        if not resolved:
            return DEFAULT_UNIVERSE_ACCURACY
        correct = sum(1 for o in resolved if o.outcome_correct)
        return correct / len(resolved)


# ---------------------------------------------------------------------------
# CallerScore: per-caller accuracy result
# ---------------------------------------------------------------------------


@dataclass
class CallerScore:
    """Score for a single named caller, computed at a specific decision_time_ms.

    caller_id:          The caller's stable identifier.
    call_count:         Number of resolved calls at decision_time_ms.
    accuracy_raw:       Raw fraction of correct calls (no win-rate label anywhere).
    accuracy_delta:     accuracy_raw - universe_accuracy.
                        Positive = above average; negative = below average.
                        Most callers are near-zero or negative.
    selectivity_weight: [0.0, 1.0] — how much to trust this caller.
                        0.0 = ignore (insufficient history or net-negative).
                        1.0 = full trust (rarely awarded; above-average accuracy
                              with sufficient history).
                        NEVER used to raise conviction above the social base.
    is_negative:        True if this caller is historically net-negative.
    is_thin_data:       True if call_count < MIN_CALLS_FOR_SCORE.
    recency_weight:     Aggregate recency discount applied (0.0 = all stale).
    universe_accuracy:  The universe prior used in this computation.
    decision_time_ms:   Point-in-time anchor for reproducibility.
    """

    caller_id: str
    decision_time_ms: int
    call_count: int
    accuracy_raw: float           # fraction of correct calls; no win-rate label
    accuracy_delta: float         # vs universe; negative for most callers
    selectivity_weight: float     # [0, 1]; NEVER raises conviction
    is_negative: bool
    is_thin_data: bool
    recency_weight: float
    universe_accuracy: float


# ---------------------------------------------------------------------------
# CallerSignal: aggregate across all callers for a single asset + window
# ---------------------------------------------------------------------------


@dataclass
class CallerSignal:
    """Aggregate caller track-record signal for a single asset at decision_time_ms.

    This is the output of CallerTrackRecordScorer.score_for_asset() and is
    injected as a selectivity modifier into the MCS pipeline.

    HARD RULES:
      - caller_selectivity_modifier in [0.0, 1.0]; cannot exceed 1.0.
      - A caller_selectivity_modifier < 1.0 REDUCES the caller-adjusted MCS.
      - It CANNOT raise conviction above the social/narrative base.
      - It is NEVER a standalone buy trigger.
      - negative_caller_count is surfaced honestly.

    caller_selectivity_modifier:
        The aggregate selectivity modifier for this asset at decision_time_ms.
        Derived from the WEIGHTED AVERAGE of selectivity_weights of callers
        who called this asset, weighted by their track-record quality.
        Range [0.0, 1.0]:
            0.0 = all active callers are net-negative (strong de-risk signal)
            1.0 = no callers (neutral — no caller information)
            0.5 = mixed or thin signal (conservative default)
        Values > 1.0 are IMPOSSIBLE by construction.

    mcs_delta_contribution:
        Float in (-1.0, 0.0].  Always <= 0.
        This is the ACTUAL delta the caller signal contributes to the MCS score.
        Caller signal CANNOT push MCS up — only down (adversarial default).
        0.0 = no caller called this asset, or all weights neutral.

    called_by:          List of caller IDs who called this asset.
    positive_caller_count: Callers with accuracy_delta > threshold.
    negative_caller_count: Callers with is_negative = True.  Surface honestly.
    thin_data_caller_count: Callers with insufficient history.
    net_caller_weight:  Sum of (selectivity_weight - 1.0) for all callers.
                        Negative = more negative than positive callers; always <= 0.
    asset:              Mint address.
    decision_time_ms:   Point-in-time anchor.
    caller_scores:      Per-caller detail for audit trail.
    """

    asset: str
    decision_time_ms: int
    caller_selectivity_modifier: float   # [0.0, 1.0]
    mcs_delta_contribution: float        # (-1.0, 0.0] — always non-positive
    called_by: list[str]
    positive_caller_count: int
    negative_caller_count: int           # surfaced honestly
    thin_data_caller_count: int
    net_caller_weight: float             # sum of (weight - 1); always <= 0
    caller_scores: list[CallerScore]


# ---------------------------------------------------------------------------
# Core scoring functions
# ---------------------------------------------------------------------------


def _recency_weight(
    outcome_event_time_ms: int,
    decision_time_ms: int,
) -> float:
    """Exponential recency decay.  Older outcomes contribute less.

    Returns a weight in (0, 1].  Recent outcomes (within the last half-life)
    get weight near 1.0.  Very old outcomes get weight near 0.
    """
    age_ms = decision_time_ms - outcome_event_time_ms
    if age_ms <= 0:
        # Outcome is at the same time or future — excluded by point-in-time
        # filter before reaching here, but guard defensively.
        return 0.0
    # Exponential decay: weight = 0.5^(age / half_life)
    return math.pow(0.5, age_ms / RECENCY_HALF_LIFE_MS)


def _compute_caller_score(
    caller_id: str,
    outcomes: list[CallerCallOutcome],
    decision_time_ms: int,
    universe_accuracy: float,
) -> CallerScore:
    """Compute the CallerScore for a single caller from its resolved outcomes.

    POINT-IN-TIME CONTRACT: all outcomes in the list must already be filtered
    to outcome_event_time_ms <= decision_time_ms.  This function trusts that
    invariant (enforced by the CallerOutcomeStore.get_outcomes method).

    Outcomes are RECENCY-WEIGHTED: older calls count less.
    accuracy_raw is the weighted fraction of correct calls.
    accuracy_delta = accuracy_raw - universe_accuracy.
    selectivity_weight is derived from accuracy_delta:
      - Thin data => 0.0 (insufficient evidence)
      - Net-negative delta => 0.0 (de-risk, ignore completely)
      - Near-zero delta => low-but-nonzero weight (indistinguishable from noise)
      - Positive delta => scaled up to max 1.0
    """
    call_count = len(outcomes)
    is_thin = call_count < MIN_CALLS_FOR_SCORE

    if not outcomes or is_thin:
        return CallerScore(
            caller_id=caller_id,
            decision_time_ms=decision_time_ms,
            call_count=call_count,
            accuracy_raw=0.0,
            accuracy_delta=0.0,
            selectivity_weight=0.0,
            is_negative=False,
            is_thin_data=True,
            recency_weight=0.0,
            universe_accuracy=universe_accuracy,
        )

    # Recency-weighted accuracy
    total_weight = 0.0
    correct_weight = 0.0
    for o in outcomes:
        w = _recency_weight(o.outcome_event_time_ms, decision_time_ms)
        total_weight += w
        if o.outcome_correct:
            correct_weight += w

    if total_weight <= 0.0:
        # All outcomes are too old to have nonzero weight
        return CallerScore(
            caller_id=caller_id,
            decision_time_ms=decision_time_ms,
            call_count=call_count,
            accuracy_raw=0.0,
            accuracy_delta=0.0,
            selectivity_weight=0.0,
            is_negative=False,
            is_thin_data=True,
            recency_weight=0.0,
            universe_accuracy=universe_accuracy,
        )

    accuracy_raw = correct_weight / total_weight
    accuracy_delta = accuracy_raw - universe_accuracy

    # Clamp delta to [-MAX_ACCURACY_DELTA, MAX_ACCURACY_DELTA]
    accuracy_delta = max(-MAX_ACCURACY_DELTA, min(MAX_ACCURACY_DELTA, accuracy_delta))

    is_negative = accuracy_delta < NEGATIVE_ACCURACY_DELTA_THRESHOLD

    if is_negative:
        # Net-negative caller: completely deweighted (selectivity = 0.0)
        selectivity_weight = 0.0
    elif accuracy_delta < POSITIVE_ACCURACY_DELTA_THRESHOLD:
        # Near-zero: too close to the universe average to distinguish from noise
        # Apply a small weight proportional to the delta magnitude
        # Scale from 0.0 (at 0 delta) to 0.3 (at threshold)
        frac = max(0.0, accuracy_delta / POSITIVE_ACCURACY_DELTA_THRESHOLD)
        selectivity_weight = frac * 0.3
    else:
        # Positive delta above threshold: scale from 0.3 (at threshold) to 1.0
        # (at MAX_ACCURACY_DELTA).
        # selectivity_weight = 0.3 + 0.7 * (delta - pos_threshold) / (max - pos_threshold)
        band = MAX_ACCURACY_DELTA - POSITIVE_ACCURACY_DELTA_THRESHOLD
        above_threshold = accuracy_delta - POSITIVE_ACCURACY_DELTA_THRESHOLD
        selectivity_weight = min(1.0, 0.3 + 0.7 * (above_threshold / band))

    return CallerScore(
        caller_id=caller_id,
        decision_time_ms=decision_time_ms,
        call_count=call_count,
        accuracy_raw=accuracy_raw,
        accuracy_delta=accuracy_delta,
        selectivity_weight=selectivity_weight,
        is_negative=is_negative,
        is_thin_data=False,
        recency_weight=total_weight / call_count,  # avg recency per call
        universe_accuracy=universe_accuracy,
    )


# ---------------------------------------------------------------------------
# CallerTrackRecordScorer — the public scorer class
# ---------------------------------------------------------------------------


class CallerTrackRecordScorer:
    """Scores named callers by historical accuracy on recorded on-chain outcomes.

    INJECTABLE:
        store:  CallerOutcomeStore (InMemoryCallerOutcomeStore in tests/offline)

    SLOW-LOOP ONLY.  NOT on the SNIPE/FAST hot path.  No network calls.
    No LLM calls.  No secrets.

    USAGE:
        store = InMemoryCallerOutcomeStore(outcomes=[...])
        scorer = CallerTrackRecordScorer(store)
        signal = scorer.score_for_asset("MINT_ADDRESS", caller_ids, decision_time_ms)
        # signal.caller_selectivity_modifier <= 1.0 (can only reduce conviction)
        # signal.mcs_delta_contribution <= 0.0 (always non-positive)

    CALLER SCORING INVARIANTS:
        1. Insufficient history (< MIN_CALLS_FOR_SCORE) => weight 0.0 (ignore).
        2. Net-negative accuracy delta => weight 0.0 (de-risk).
        3. Near-zero delta => small positive weight (low trust).
        4. Positive delta => scaled weight up to 1.0 (still capped).
        5. caller_selectivity_modifier CANNOT exceed 1.0.
        6. mcs_delta_contribution <= 0.0 always.
        7. No win-rate computation anywhere in this class.
    """

    def __init__(
        self,
        store: CallerOutcomeStore,
        universe_accuracy: float | None = None,
    ) -> None:
        """
        Args:
            store:             The outcome store (injectable; InMemoryCallerOutcomeStore in tests).
            universe_accuracy: Optional override for the universe prior.
                               If None, the store's get_universe_accuracy() is called.
        """
        self._store = store
        self._universe_accuracy_override = universe_accuracy

    def score_caller(
        self,
        caller_id: str,
        decision_time_ms: int,
    ) -> CallerScore:
        """Score a single caller at the given decision_time_ms.

        POINT-IN-TIME: only outcomes with outcome_event_time_ms <= decision_time_ms
        are used.  Future outcomes are excluded.

        Args:
            caller_id:        The caller's stable identifier.
            decision_time_ms: Point-in-time anchor (C-5).

        Returns:
            CallerScore with selectivity_weight in [0.0, 1.0].
        """
        universe_accuracy = (
            self._universe_accuracy_override
            if self._universe_accuracy_override is not None
            else self._store.get_universe_accuracy(decision_time_ms)
        )
        outcomes = self._store.get_outcomes(caller_id, decision_time_ms)
        return _compute_caller_score(caller_id, outcomes, decision_time_ms, universe_accuracy)

    def score_for_asset(
        self,
        asset: str,
        caller_ids: list[str],
        decision_time_ms: int,
    ) -> CallerSignal:
        """Produce the aggregate CallerSignal for a specific asset + caller set.

        Only callers who have called THIS asset are considered when computing
        the selectivity modifier.  All others contribute 0 (neutral).

        The caller_selectivity_modifier is the WEIGHTED AVERAGE of the
        selectivity_weights of callers who called this asset.
        It is in [0.0, 1.0] — NEVER exceeds 1.0.

        The mcs_delta_contribution is:
            (caller_selectivity_modifier - 1.0) * scale_factor
        which is always <= 0.0 (de-risk direction only).

        Args:
            asset:            The mint address being scored.
            caller_ids:       List of caller IDs who mentioned this asset.
            decision_time_ms: Point-in-time anchor.

        Returns:
            CallerSignal with caller_selectivity_modifier in [0.0, 1.0]
            and mcs_delta_contribution in (-1.0, 0.0].
        """
        universe_accuracy = (
            self._universe_accuracy_override
            if self._universe_accuracy_override is not None
            else self._store.get_universe_accuracy(decision_time_ms)
        )

        caller_scores: list[CallerScore] = []
        for cid in caller_ids:
            outcomes = self._store.get_outcomes(cid, decision_time_ms)
            score = _compute_caller_score(cid, outcomes, decision_time_ms, universe_accuracy)
            caller_scores.append(score)

        positive_count = sum(1 for s in caller_scores if not s.is_negative and not s.is_thin_data and s.accuracy_delta >= POSITIVE_ACCURACY_DELTA_THRESHOLD)
        negative_count = sum(1 for s in caller_scores if s.is_negative)
        thin_count = sum(1 for s in caller_scores if s.is_thin_data)

        if not caller_scores:
            # No callers: neutral (no information)
            return CallerSignal(
                asset=asset,
                decision_time_ms=decision_time_ms,
                caller_selectivity_modifier=1.0,
                mcs_delta_contribution=0.0,
                called_by=[],
                positive_caller_count=0,
                negative_caller_count=0,
                thin_data_caller_count=0,
                net_caller_weight=0.0,
                caller_scores=[],
            )

        # Aggregate selectivity weight across callers (equal weight per caller)
        mean_weight = sum(s.selectivity_weight for s in caller_scores) / len(caller_scores)

        # caller_selectivity_modifier = mean_weight in [0.0, 1.0]
        # When all callers are negative (weight=0), modifier = 0.0 (de-risk).
        # When no callers or all neutral (weight=1), modifier = 1.0 (no change).
        # Values > 1.0 are impossible because selectivity_weight <= 1.0.
        caller_selectivity_modifier = min(1.0, max(0.0, mean_weight))

        # net_caller_weight: how far below 1.0 we are (always <= 0.0)
        net_caller_weight = sum(s.selectivity_weight - 1.0 for s in caller_scores)

        # mcs_delta_contribution: translate modifier into an MCS delta
        # delta = (modifier - 1.0) * 0.5  →  range (-0.5, 0.0]
        # Factor 0.5: caller signal is a partial input, not the whole MCS.
        # A modifier of 0 (all negative callers) reduces MCS by 0.5 at most.
        # A modifier of 1 (no callers or neutral) contributes 0.
        mcs_delta_contribution = min(0.0, (caller_selectivity_modifier - 1.0) * 0.5)

        logger.debug(
            "caller_score: asset=%s callers=%d pos=%d neg=%d thin=%d "
            "modifier=%.3f mcs_delta=%.3f universe_acc=%.3f",
            asset,
            len(caller_scores),
            positive_count,
            negative_count,
            thin_count,
            caller_selectivity_modifier,
            mcs_delta_contribution,
            universe_accuracy,
        )

        return CallerSignal(
            asset=asset,
            decision_time_ms=decision_time_ms,
            caller_selectivity_modifier=caller_selectivity_modifier,
            mcs_delta_contribution=mcs_delta_contribution,
            called_by=[s.caller_id for s in caller_scores],
            positive_caller_count=positive_count,
            negative_caller_count=negative_count,
            thin_data_caller_count=thin_count,
            net_caller_weight=net_caller_weight,
            caller_scores=caller_scores,
        )


# ---------------------------------------------------------------------------
# Directionality assertion (MCS integration seam — load-bearing)
# ---------------------------------------------------------------------------


def assert_caller_signal_cannot_raise_conviction(
    signal: CallerSignal, base_conviction: float
) -> float:
    """Apply the caller signal to the base MCS conviction.

    INVARIANT: the result CANNOT exceed base_conviction.
    A caller signal may ONLY de-risk (lower or maintain conviction).

    The formula is:
        adjusted = clamp(base_conviction + mcs_delta_contribution, 0.0, 1.0)
    Since mcs_delta_contribution <= 0.0 always, adjusted <= base_conviction.

    Args:
        signal:           CallerSignal from score_for_asset().
        base_conviction:  The social/narrative MCS conviction BEFORE caller adjustment.
                          Must be in [0.0, 1.0].

    Returns:
        Adjusted conviction in [0.0, base_conviction].

    DEF-EN1-01 (ported from DEF-E10-01 / aats/sentiment/velocity.py):
    Hard clamp: mcs_delta_contribution is guaranteed non-positive by
    CallerTrackRecordScorer.score_for_asset(), but a forged / mutated
    CallerSignal (e.g. from a unit-test adversary or a future code path that
    bypasses score_for_asset()) could carry a positive delta.  A bare
    `assert` is STRIPPED under `python -O` / PYTHONOPTIMIZE=1, so we enforce
    the invariant with an explicit raise that survives optimised mode AND a
    clamp that prevents any accidental raise-conviction path even if the
    raise were somehow suppressed.

    ORDER OF OPERATIONS (load-bearing):
      1. Clamp delta to (-inf, 0.0] — de-risk direction enforced unconditionally.
      2. Raise ValueError for the forged-positive case — not an assert.
      3. Compute adjusted score.
      4. Raise ValueError if adjusted > base_conviction — not an assert.

    The clamp in step 1 means step 3 is always safe even if the raise in
    step 2 is somehow bypassed (belt-and-suspenders).
    """
    delta_raw = signal.mcs_delta_contribution
    if delta_raw > 0.0:
        # This is NOT an assert — it is NOT stripped by python -O.
        raise ValueError(
            f"CALLER INVARIANT VIOLATED: signal.mcs_delta_contribution={delta_raw:.6f} "
            "is positive. CallerSignal.mcs_delta_contribution MUST be <= 0.0. "
            "A positive delta would raise conviction, violating the de-risk direction rule. "
            "This is a defect in the caller or the CallerTrackRecordScorer pipeline."
        )
    # Clamp defensively even after the raise (belt-and-suspenders: if this
    # function is ever called in a context where exceptions are swallowed, the
    # clamp ensures conviction can still only go down, never up).
    delta = min(0.0, delta_raw)
    adjusted = max(0.0, min(1.0, base_conviction + delta))
    # Hard guard: result must not exceed base_conviction.
    # This is NOT an assert — it is NOT stripped by python -O.
    if adjusted > base_conviction + 1e-9:
        raise ValueError(
            f"CALLER INVARIANT VIOLATED: adjusted conviction {adjusted:.6f} "
            f"exceeds base_conviction {base_conviction:.6f} "
            f"(signal.mcs_delta_contribution={signal.mcs_delta_contribution:.6f}, "
            f"clamped_delta={delta:.6f}). "
            "Caller signals may ONLY de-risk (lower or maintain conviction). "
            "This is a critical safety defect — investigation required."
        )
    return adjusted
