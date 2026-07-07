"""E15 (data-ingestion half) — LEAK-FREE serial-deployer reputation TALLIES.

OWNERSHIP: data-ingestion-engineer (M1).  This module owns the RAW, POINT-IN-TIME
aggregation of a creator wallet's PRIOR launch outcomes from the recorded corpus
(``LaunchOutcome`` labels, ``aats/contracts/events.py`` §3A).  It emits honest
integer TALLIES only.  It NEVER computes a rate/ratio and NEVER makes a trading
decision — the REJECT/DOWN-WEIGHT verdict is applied downstream by
``aats/risk/deployer_reputation_gate.py`` (risk-guardrails lane).

WHAT THIS MODULE IS (and is NOT)
---------------------------------
  * A read-side, injectable AGGREGATOR over already-labelled prior launches.  It
    reads the clean-room harness' ``labels/`` corpus (T-400/T-401) joined with the
    creator-wallet identity carried on ``LaunchEvent`` — it does NOT compute a
    label itself, does NOT touch capital/signer/RPC, and makes NO network calls.
  * NOT a risk decision.  ``lookup_deployer_reputation`` returns a
    ``DeployerReputationTally`` — two plain, non-negative integers
    (``prior_launches``, ``prior_rugs``) and the point-in-time anchor they were
    computed at.  There is no field here that rejects, sizes, or scores anything.
  * NOT a rate.  There is no rug-rate/win-rate/success-rate field or
    computation anywhere in this module — see the HARD RULE below.

STRICT POINT-IN-TIME (T-300a, the law of this signal)
-------------------------------------------------------
  A prior launch's outcome counts toward a creator's reputation at decision slot
  ``as_of_event_slot`` **iff** that outcome's ``outcome_slot`` (the on-chain slot
  at which the clean-room harness' resolution became knowable — i.e.
  ``LaunchOutcome.resolution_event_time.slot``) is **strictly less than**
  ``as_of_event_slot``.  A launch can NEVER use its own outcome (a launch's
  resolution always resolves strictly after its own event_slot — enforced by
  ``LaunchOutcome`` construction invariants) and can NEVER use ANY future
  outcome.  This is enforced STRUCTURALLY by the store's index (a per-key list
  sorted ascending by ``outcome_slot``, sliced with a strict-less-than bisect) —
  there is no code path that can pass a `>= as_of_event_slot` row through.

  ``as_of_event_slot`` is always an ON-CHAIN SLOT (never wall-clock).  This
  module never reads ``time.time()`` / wall-clock to decide what "counts".

O(1)-KEYED LOOKUP
------------------
  The store pre-indexes outcomes into ``dict[creator_wallet, list[...]]`` (and
  ``dict[deploy_template_fingerprint, list[...]]``) at load time.  A lookup is an
  O(1) dict access to that wallet's own (typically tiny) outcome list, followed by
  a bisect over ONLY that wallet's own launches (never a scan of the whole
  corpus) — no file I/O, no network, no full-corpus scan on the query path.

NO WIN-RATE (HARD RULE)
------------------------
  ``DeployerReputationTally`` carries ONLY ``prior_launches`` and ``prior_rugs`` —
  raw counts.  No ratio/fraction/rate field is computed or stored anywhere in this
  module.  A consumer that wants a derived statistic must compute it itself,
  outside this module, and such a computation is explicitly OUT OF SCOPE here
  (and would violate the E15 hard rule if it ever became a conviction input).

REFUSE-BY-DEFAULT
------------------
  An unknown creator_wallet (no recorded prior launches) returns
  ``prior_launches=0, prior_rugs=0`` — this is "no information", NOT "clean/good".
  The RISK-SIDE consumer (``deployer_reputation_gate.py``) treats a zero-tally
  result as SILENT (never a de-risk signal and NEVER a buy trigger) — it is never
  read as "verified safe".  A malformed/undecodable corpus row is DROPPED (never
  trusted, never silently promoted to "clean") — see ``_read_parquet_outcomes``.

FAST-PATH SAFETY
-----------------
  This module performs NO RPC, NO LLM, NO network call.  ``InMemoryDeployerOutcomeStore``
  is pure in-process.  ``ParquetDeployerOutcomeStore`` reads a local/mounted
  filesystem path ONCE (lazy-loaded, cached) — never on a per-lookup basis, and
  never raises for a missing/absent dataset (a not-yet-populated ``labels/``
  corpus is a legitimate bootstrap state: the store degrades to "no history for
  anyone", never an error, never inflated data).

E-M1-02 (deploy_template_fingerprint) — FIXED
------------------------------------------------
  The store also indexes by ``deploy_template_fingerprint`` when present.  As of
  the E-M1-02 fix, the pump.fun `create` decoder (``aats/ingestion/decoders.py``)
  derives this fingerprint from the mplTokenMetadata CPI's URI SHAPE (hosting
  domain + normalized path pattern, with the per-mint hash/CID collapsed) —
  template-invariant, so a repeat rug-factory template reused from a NEW
  creator wallet on a NEW mint now shares the SAME fingerprint, and
  ``get_outcomes_by_fingerprint`` can surface that history even when
  ``creator_wallet`` differs.  Non-`create` paths (withdraw/migration, PumpSwap
  create_pool, Raydium init) still fall back to a per-mint proxy (no metadata
  CPI to read there) and remain non-discriminating by design — this module
  does not depend on every path being fixed to be correct; it degrades to
  "no extra history via fingerprint" wherever the field is a per-mint proxy
  or ``None``.

MONEY / SECRETS
-----------------
  No money fields.  No secrets — the store path is env-configured
  (``DEPLOYER_OUTCOME_STORE_PATH``), never a literal in code.
"""

from __future__ import annotations

import bisect
import logging
import os
from dataclasses import dataclass
from typing import Protocol

from aats.contracts.events import LaunchOutcomeLabel

logger = logging.getLogger(__name__)

# Labels that represent a RESOLVED prior launch (contribute to prior_launches).
# CENSORED is un-resolvable (C-6) — it is deliberately EXCLUDED: we do not know
# whether it rugged or survived, so it can neither inflate prior_launches (a
# fabricated "we know about this launch and it was fine" claim) nor prior_rugs.
_RESOLVED_LABELS: frozenset[LaunchOutcomeLabel] = frozenset(
    {
        LaunchOutcomeLabel.SURVIVED,
        LaunchOutcomeLabel.RUGGED,
        LaunchOutcomeLabel.FADED,
    }
)


# ---------------------------------------------------------------------------
# DeployerLaunchOutcome — one resolved prior launch, keyed by creator identity
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DeployerLaunchOutcome:
    """One resolved prior launch by a creator wallet (joined LaunchEvent+LaunchOutcome).

    ``launch_event_slot`` is the slot of the launch itself (audit only — never the
    point-in-time filter key).  ``outcome_slot`` IS the point-in-time filter key:
    the slot at which the clean-room harness' resolution became knowable
    (``LaunchOutcome.resolution_event_time.slot``).

    CONTRACT (mirrors ``LaunchOutcome`` construction invariants):
      ``outcome_slot`` must be strictly greater than ``launch_event_slot`` — a
      launch cannot resolve before (or in the same slot as) it started.  Violated
      rows are a corpus defect and are rejected at construction (fail fast in
      tests; dropped-with-a-log in the Parquet reader, never silently trusted).
    """

    creator_wallet: str
    mint: str
    launch_event_slot: int
    outcome_slot: int
    label: LaunchOutcomeLabel
    deploy_template_fingerprint: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.creator_wallet, str) or not self.creator_wallet:
            raise ValueError("DeployerLaunchOutcome.creator_wallet must be a non-empty str")
        if not isinstance(self.mint, str) or not self.mint:
            raise ValueError("DeployerLaunchOutcome.mint must be a non-empty str")
        for name in ("launch_event_slot", "outcome_slot"):
            val = getattr(self, name)
            if not isinstance(val, int) or isinstance(val, bool):
                raise TypeError(
                    f"DeployerLaunchOutcome.{name} must be int, got {type(val).__name__}"
                )
            if val < 0:
                raise ValueError(f"DeployerLaunchOutcome.{name} must be non-negative")
        if not isinstance(self.label, LaunchOutcomeLabel):
            raise TypeError("DeployerLaunchOutcome.label must be a LaunchOutcomeLabel")
        if self.outcome_slot <= self.launch_event_slot:
            raise ValueError(
                "DeployerLaunchOutcome.outcome_slot must be STRICTLY greater than "
                f"launch_event_slot ({self.outcome_slot} <= {self.launch_event_slot}); "
                "a launch cannot resolve before it started (point-in-time contract)."
            )


# ---------------------------------------------------------------------------
# DeployerOutcomeStore Protocol — injected, no live on-chain reads here.
# ---------------------------------------------------------------------------


class DeployerOutcomeStore(Protocol):
    """Protocol for the recorded serial-deployer outcome backend.

    CONTRACT: both methods MUST return only outcomes with
    ``outcome_slot < as_of_event_slot`` (STRICT) — never the launch's own or any
    future outcome.  Implementations are responsible for enforcing this.
    """

    def get_outcomes(
        self, creator_wallet: str, as_of_event_slot: int
    ) -> list[DeployerLaunchOutcome]: ...

    def get_outcomes_by_fingerprint(
        self, fingerprint: str, as_of_event_slot: int
    ) -> list[DeployerLaunchOutcome]: ...


def _insert_sorted(
    index: dict[str, list[DeployerLaunchOutcome]], key: str, o: DeployerLaunchOutcome
) -> None:
    bucket = index.setdefault(key, [])
    pos = bisect.bisect_left([x.outcome_slot for x in bucket], o.outcome_slot)
    bucket.insert(pos, o)


def _strictly_before(
    bucket: list[DeployerLaunchOutcome], as_of_event_slot: int
) -> list[DeployerLaunchOutcome]:
    """Return the prefix of ``bucket`` (sorted ascending by outcome_slot) with
    ``outcome_slot < as_of_event_slot`` — STRICT, no relaxation.

    ``bisect_left`` on the ``outcome_slot`` key returns the leftmost insertion
    point for ``as_of_event_slot``; every element before that index has
    ``outcome_slot < as_of_event_slot`` even when ties are present (a tie sits at
    or after the returned index, so it is correctly EXCLUDED).
    """
    idx = bisect.bisect_left(bucket, as_of_event_slot, key=lambda o: o.outcome_slot)
    return bucket[:idx]


# ---------------------------------------------------------------------------
# InMemoryDeployerOutcomeStore (offline / test fixture)
# ---------------------------------------------------------------------------


class InMemoryDeployerOutcomeStore:
    """Offline, injectable in-memory store for tests and CI.

    Pre-indexes every added outcome into a per-``creator_wallet`` bucket (and, if
    present, a per-``deploy_template_fingerprint`` bucket), each kept sorted
    ascending by ``outcome_slot``.  A query is an O(1) dict lookup for the bucket
    plus a bisect over ONLY that key's own outcomes — never a scan of the whole
    store.
    """

    def __init__(self, outcomes: list[DeployerLaunchOutcome] | None = None) -> None:
        self._by_wallet: dict[str, list[DeployerLaunchOutcome]] = {}
        self._by_fingerprint: dict[str, list[DeployerLaunchOutcome]] = {}
        for o in outcomes or []:
            self.add_outcome(o)

    def add_outcome(self, o: DeployerLaunchOutcome) -> None:
        _insert_sorted(self._by_wallet, o.creator_wallet, o)
        if o.deploy_template_fingerprint:
            _insert_sorted(self._by_fingerprint, o.deploy_template_fingerprint, o)

    def get_outcomes(
        self, creator_wallet: str, as_of_event_slot: int
    ) -> list[DeployerLaunchOutcome]:
        return _strictly_before(self._by_wallet.get(creator_wallet, []), as_of_event_slot)

    def get_outcomes_by_fingerprint(
        self, fingerprint: str, as_of_event_slot: int
    ) -> list[DeployerLaunchOutcome]:
        return _strictly_before(self._by_fingerprint.get(fingerprint, []), as_of_event_slot)


# ---------------------------------------------------------------------------
# ParquetDeployerOutcomeStore (production backend)
# ---------------------------------------------------------------------------

# Columns the joined (LaunchEvent x LaunchOutcome) corpus MUST carry.  One row
# per resolved prior launch — mirrors DeployerLaunchOutcome field-for-field.
PARQUET_DEPLOYER_REQUIRED_COLUMNS: frozenset[str] = frozenset(
    {"creator_wallet", "mint", "launch_event_slot", "outcome_slot", "label"}
)


def _clean_store_path(raw: str | None) -> str | None:
    """Return None for an unset/placeholder DEPLOYER_OUTCOME_STORE_PATH, else the path."""
    if raw is None:
        return None
    v = raw.strip()
    if not v or (v.startswith("<") and v.endswith(">")):
        return None
    return v


def _read_parquet_outcomes(path: str | None) -> list[DeployerLaunchOutcome]:
    """Read + validate the joined serial-deployer outcome corpus.

    NEVER RAISES for a missing/unset/unreadable/malformed dataset: returns []
    (no reputation history for anyone — a not-yet-populated corpus is a
    legitimate bootstrap state, not an error).

    PIT + CELL-LEVEL SAFETY: a single malformed cell, an unknown ``label``, or a
    row violating ``outcome_slot > launch_event_slot`` is DROPPED individually
    (counted + logged) — it can never crash the read and is NEVER silently
    trusted as "clean".
    """
    if not path:
        logger.info(
            "ParquetDeployerOutcomeStore: no DEPLOYER_OUTCOME_STORE_PATH configured "
            "(offline mode) -- returning [] (no deployer-reputation history)."
        )
        return []
    if not os.path.exists(path):
        logger.info(
            "ParquetDeployerOutcomeStore: path %s does not exist (offline mode) -- "
            "returning [] (no deployer-reputation history).",
            path,
        )
        return []

    try:
        import pandas as pd  # lazy import — only needed when a real path is configured
    except ImportError:
        logger.warning(
            "ParquetDeployerOutcomeStore: pandas is not installed; cannot read %s. "
            "Returning [] (no deployer-reputation history).",
            path,
        )
        return []

    try:
        frame = pd.read_parquet(path)
    except Exception as exc:  # noqa: BLE001 — any read failure => safe empty fallback
        logger.warning(
            "ParquetDeployerOutcomeStore: failed to read %s (%s: %s). Returning [].",
            path,
            type(exc).__name__,
            exc,
        )
        return []

    missing_columns = PARQUET_DEPLOYER_REQUIRED_COLUMNS - set(frame.columns)
    if missing_columns:
        logger.warning(
            "ParquetDeployerOutcomeStore: %s is missing required column(s) %s. "
            "Returning [] rather than guessing a schema.",
            path,
            sorted(missing_columns),
        )
        return []

    has_fingerprint_col = "deploy_template_fingerprint" in frame.columns

    outcomes: list[DeployerLaunchOutcome] = []
    dropped_malformed = 0
    dropped_pit_violations = 0
    for row in frame.to_dict("records"):
        try:
            raw_wallet = row["creator_wallet"]
            raw_mint = row["mint"]
            raw_launch_slot = row["launch_event_slot"]
            raw_outcome_slot = row["outcome_slot"]
            raw_label = row["label"]
            if any(
                pd.isna(v)
                for v in (raw_wallet, raw_mint, raw_launch_slot, raw_outcome_slot, raw_label)
            ):
                raise ValueError("null/NaN cell in a required column")
            creator_wallet = str(raw_wallet)
            mint = str(raw_mint)
            launch_event_slot = int(raw_launch_slot)
            outcome_slot = int(raw_outcome_slot)
            label = LaunchOutcomeLabel(str(raw_label))
            fingerprint: str | None = None
            if has_fingerprint_col:
                raw_fp = row.get("deploy_template_fingerprint")
                if raw_fp is not None and not pd.isna(raw_fp):
                    fp_str = str(raw_fp).strip()
                    fingerprint = fp_str or None
        except (TypeError, ValueError) as exc:
            dropped_malformed += 1
            logger.debug(
                "ParquetDeployerOutcomeStore: dropped malformed row from %s (%s: %s): %r",
                path,
                type(exc).__name__,
                exc,
                row,
            )
            continue

        if outcome_slot <= launch_event_slot:
            dropped_pit_violations += 1
            continue

        outcomes.append(
            DeployerLaunchOutcome(
                creator_wallet=creator_wallet,
                mint=mint,
                launch_event_slot=launch_event_slot,
                outcome_slot=outcome_slot,
                label=label,
                deploy_template_fingerprint=fingerprint,
            )
        )

    if dropped_malformed:
        logger.warning(
            "ParquetDeployerOutcomeStore: dropped %d malformed row(s) from %s.",
            dropped_malformed,
            path,
        )
    if dropped_pit_violations:
        logger.warning(
            "ParquetDeployerOutcomeStore: dropped %d row(s) from %s violating the "
            "outcome_slot > launch_event_slot contract.",
            dropped_pit_violations,
            path,
        )
    logger.info(
        "ParquetDeployerOutcomeStore: loaded %d outcome row(s) from %s.", len(outcomes), path
    )
    return outcomes


class ParquetDeployerOutcomeStore:
    """Production ``DeployerOutcomeStore`` backend — reads a local/mounted
    Parquet path (single file or partitioned directory) at
    ``DEPLOYER_OUTCOME_STORE_PATH``.

    Same lazy-load + cache + fail-open-to-empty contract as
    ``aats.sentiment.caller_score.ParquetCallerOutcomeStore`` (EN5 precedent).
    Call ``reload()`` after the clean-room harness appends new labels.
    """

    def __init__(self, path: str | None, *, preload: bool = False) -> None:
        self._path = path
        self._by_wallet: dict[str, list[DeployerLaunchOutcome]] | None = None
        self._by_fingerprint: dict[str, list[DeployerLaunchOutcome]] | None = None
        if preload:
            self._ensure_loaded()

    @classmethod
    def from_env(
        cls, env: dict[str, str] | None = None, *, preload: bool = False
    ) -> ParquetDeployerOutcomeStore:
        src = env if env is not None else dict(os.environ)
        path = _clean_store_path(src.get("DEPLOYER_OUTCOME_STORE_PATH"))
        return cls(path=path, preload=preload)

    def reload(self) -> None:
        self._by_wallet = None
        self._by_fingerprint = None

    def _ensure_loaded(self) -> None:
        if self._by_wallet is not None:
            return
        by_wallet: dict[str, list[DeployerLaunchOutcome]] = {}
        by_fp: dict[str, list[DeployerLaunchOutcome]] = {}
        for o in _read_parquet_outcomes(self._path):
            _insert_sorted(by_wallet, o.creator_wallet, o)
            if o.deploy_template_fingerprint:
                _insert_sorted(by_fp, o.deploy_template_fingerprint, o)
        self._by_wallet = by_wallet
        self._by_fingerprint = by_fp

    def get_outcomes(
        self, creator_wallet: str, as_of_event_slot: int
    ) -> list[DeployerLaunchOutcome]:
        self._ensure_loaded()
        assert self._by_wallet is not None
        return _strictly_before(self._by_wallet.get(creator_wallet, []), as_of_event_slot)

    def get_outcomes_by_fingerprint(
        self, fingerprint: str, as_of_event_slot: int
    ) -> list[DeployerLaunchOutcome]:
        self._ensure_loaded()
        assert self._by_fingerprint is not None
        return _strictly_before(self._by_fingerprint.get(fingerprint, []), as_of_event_slot)


# ---------------------------------------------------------------------------
# DeployerReputationTally — the public, honest, TALLIES-ONLY result.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DeployerReputationTally:
    """Point-in-time serial-deployer reputation TALLY.  NEVER a rate.

    ``prior_launches``: count of distinct resolved (SURVIVED/RUGGED/FADED) prior
    launches by this creator (and, if given, this fingerprint) with
    ``outcome_slot < as_of_event_slot``.  CENSORED prior launches are excluded
    (unknown, not "clean").

    ``prior_rugs``: count of those that were RUGGED.  Always
    ``0 <= prior_rugs <= prior_launches``.

    HARD RULE: no rate/ratio/fraction field exists on this type.  A caller that
    wants "prior_rugs / prior_launches" must compute it OUTSIDE this module — and
    per the E15 directive, no consumer may treat such a derived rate as a
    conviction/win-rate input.
    """

    creator_wallet: str
    deploy_template_fingerprint: str | None
    as_of_event_slot: int
    prior_launches: int
    prior_rugs: int

    def __post_init__(self) -> None:
        for name in ("prior_launches", "prior_rugs"):
            val = getattr(self, name)
            if not isinstance(val, int) or isinstance(val, bool):
                raise TypeError(
                    f"DeployerReputationTally.{name} must be int, got {type(val).__name__}"
                )
            if val < 0:
                raise ValueError(f"DeployerReputationTally.{name} must be non-negative")
        if self.prior_rugs > self.prior_launches:
            raise ValueError(
                f"DeployerReputationTally.prior_rugs ({self.prior_rugs}) cannot exceed "
                f"prior_launches ({self.prior_launches})"
            )


def lookup_deployer_reputation(
    store: DeployerOutcomeStore,
    *,
    creator_wallet: str,
    as_of_event_slot: int,
    deploy_template_fingerprint: str | None = None,
) -> DeployerReputationTally:
    """O(1)-keyed, STRICT point-in-time reputation TALLY lookup.

    Aggregates every prior resolved launch by ``creator_wallet`` (and, if given, by
    ``deploy_template_fingerprint`` — deduplicated by mint so a launch indexed
    under BOTH keys is never double-counted) whose outcome resolved strictly
    before ``as_of_event_slot``.  Returns raw integer TALLIES only — never a rate.

    An unknown creator_wallet with no recorded prior launches returns
    ``prior_launches=0, prior_rugs=0`` (no information — NOT "clean/good"; the
    risk-side consumer treats this as SILENT, never a de-risk OR a buy signal).
    """
    seen_mints: set[str] = set()
    prior_launches = 0
    prior_rugs = 0

    for o in store.get_outcomes(creator_wallet, as_of_event_slot):
        if o.mint in seen_mints:
            continue
        seen_mints.add(o.mint)
        if o.label not in _RESOLVED_LABELS:
            continue
        prior_launches += 1
        if o.label is LaunchOutcomeLabel.RUGGED:
            prior_rugs += 1

    if deploy_template_fingerprint:
        for o in store.get_outcomes_by_fingerprint(deploy_template_fingerprint, as_of_event_slot):
            if o.mint in seen_mints:
                continue
            seen_mints.add(o.mint)
            if o.label not in _RESOLVED_LABELS:
                continue
            prior_launches += 1
            if o.label is LaunchOutcomeLabel.RUGGED:
                prior_rugs += 1

    return DeployerReputationTally(
        creator_wallet=creator_wallet,
        deploy_template_fingerprint=deploy_template_fingerprint,
        as_of_event_slot=as_of_event_slot,
        prior_launches=prior_launches,
        prior_rugs=prior_rugs,
    )
