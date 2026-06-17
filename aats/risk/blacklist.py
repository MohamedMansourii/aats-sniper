"""E2 — Creator/token DENYLIST + trusted ALLOWLIST pre-filter.

THE PRE-GATE VETO.  This runs BEFORE the sub-10ms pre-trade safety gate
(``aats/risk/pretrade_gate.py``).  It is a single O(1) set-membership test over a
file-backed, HOT-RELOADABLE snapshot of:

  * a DENYLIST of known-scam *creator wallets* and *token mints* — a hit is an
    instant, pre-gate REJECT, surfaced as a ``RedFlag`` the gate already exports;
  * a trusted ALLOWLIST of *creator wallets* and *token mints* — which marks a
    candidate trusted but does **NOT** skip the safety gate.

ASYMMETRIC / DE-RISK-ONLY (the E2 law)
--------------------------------------
  * The DENYLIST can ONLY REJECT.  There is no code path here that sizes a trade,
    triggers a buy, widens a stop, or lifts a cap.  A denylist is structurally a
    veto: ``check`` returns either ``None`` (no opinion) or a ``RedFlagHit``
    (refuse).  It NEVER returns "buy".
  * The ALLOWLIST does NOT bypass the safety gate.  Being trusted suppresses
    nothing in ``PreTradeSafetyGate``; an allowlisted mint still runs every check.
    The allowlist exists only so a later *manual* denylist mistake does not nuke a
    known-good wallet, and so the operator dashboard can flag "trusted".  It is
    NEVER a standalone buy trigger.
  * DENYLIST WINS TIES.  A key in both lists is REJECTED (fail-closed).

FAST-PATH SAFE
--------------
  Curating the file is a SLOW-LOOP / operator job.  The FAST path reads an already-
  resolved, frozen ``frozenset`` snapshot — no JSON parse, no file I/O, no lock on
  the hot path (the snapshot is swapped atomically by an object-reference assignment,
  which is atomic in CPython).  ``check_keys`` is two ``in`` tests against frozensets.

HOT-RELOAD (fail-safe)
----------------------
  ``maybe_reload`` stat()s the file (mtime + size) on a throttled cadence and swaps
  the snapshot only when the file changed AND parsed cleanly.  A malformed reload is
  REJECTED and the previous good snapshot is retained — we never widen OR drop the
  denylist because of a typo.  The hot path calls ``check_keys`` against whatever
  snapshot is current; reload happens out of band (SLOW loop / a background tick).

POINT-IN-TIME (no compute-time leak)
------------------------------------
  Membership is a property of the KEY string, not of any future price.  The exact
  same set test runs in live and in backtest (same code path).  ``event_slot`` /
  ``event_block_time_ms`` are carried through ONLY so the reject reason can be
  stamped at the decision slot — they never influence the membership decision.

MONEY / SECRETS
---------------
  No money math here (pure membership) and no secrets — the file holds PUBLIC
  on-chain addresses only.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

from aats.risk.pretrade_gate import RedFlag, RedFlagHit

logger = logging.getLogger(__name__)

# Default location of the file-backed list (relative to repo root).  The loader is
# given an explicit Path in production; this default mirrors the program-allowlist
# convention (``config/...``) for offline/test discovery.
DEFAULT_DENYLIST_PATH = Path("config/creator-token-denylist.json")

# How often (seconds) ``maybe_reload`` is even *allowed* to stat() the file.  The
# hot path never calls reload; this just throttles the SLOW-loop refresh tick so a
# tight loop cannot hammer the filesystem.  Set to 0 to stat() on every call.
DEFAULT_RELOAD_THROTTLE_S = 2.0


# ---------------------------------------------------------------------------
# Immutable resolved snapshot — what the FAST path actually reads.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DenyAllowSnapshot:
    """A frozen, already-resolved view of the denylist + allowlist.

    The FAST path holds a reference to ONE of these and never mutates it.  A reload
    builds a NEW snapshot and the watcher swaps the reference atomically.  Because
    every field is a ``frozenset``, membership is O(1) and the object is hashable /
    safe to share across threads without a lock.
    """

    denied_creators: frozenset[str]
    denied_mints: frozenset[str]
    allowed_creators: frozenset[str]
    allowed_mints: frozenset[str]
    # Provenance for the dashboard / audit (NOT read on the hot path).
    source_mtime_ns: int = 0
    source_size: int = 0
    version: str = ""

    @classmethod
    def empty(cls) -> DenyAllowSnapshot:
        """The fail-closed-but-empty starting snapshot.

        An empty denylist denies nothing (it is purely additive selectivity), and an
        empty allowlist trusts nobody — neither is a risk-increasing default.  This
        is what we fall back to ONLY when there is no file at all; a *bad* reload
        keeps the previous good snapshot instead (see ``DenyAllowWatcher``).
        """
        return cls(
            denied_creators=frozenset(),
            denied_mints=frozenset(),
            allowed_creators=frozenset(),
            allowed_mints=frozenset(),
        )

    def is_creator_denied(self, creator: str | None) -> bool:
        return creator is not None and creator in self.denied_creators

    def is_mint_denied(self, mint: str | None) -> bool:
        return mint is not None and mint in self.denied_mints

    def is_creator_allowed(self, creator: str | None) -> bool:
        return creator is not None and creator in self.allowed_creators

    def is_mint_allowed(self, mint: str | None) -> bool:
        return mint is not None and mint in self.allowed_mints


# ---------------------------------------------------------------------------
# Parser — file -> snapshot.  Pure; raises on malformed input (caller decides).
# ---------------------------------------------------------------------------


def _coerce_str_set(items: object, key: str) -> frozenset[str]:
    """Pull a set of address strings from a list of dicts (or bare strings).

    Accepts ``[{"<key>": "addr", ...}, ...]`` (the curated form) or ``["addr", ...]``
    (a terse form).  Anything else for an entry is skipped with a warning rather
    than aborting the whole reload — but an entirely non-list ``items`` raises, so a
    structurally broken file is rejected wholesale (fail-safe: keep the old snapshot).
    """
    if items is None:
        return frozenset()
    if not isinstance(items, list):
        raise ValueError(f"expected a list for '{key}' entries, got {type(items).__name__}")
    out: set[str] = set()
    for entry in items:
        val: str | None
        if isinstance(entry, str):
            val = entry.strip()
        elif isinstance(entry, dict):
            raw = entry.get(key)
            val = raw.strip() if isinstance(raw, str) else None
        else:
            logger.warning("denylist: skipping non-str/non-dict entry under '%s': %r", key, entry)
            continue
        if val:
            out.add(val)
        else:
            logger.warning("denylist: skipping empty '%s' entry: %r", key, entry)
    return frozenset(out)


def parse_snapshot(raw_text: str, *, mtime_ns: int = 0, size: int = 0) -> DenyAllowSnapshot:
    """Parse the JSON text of the denylist file into a resolved snapshot.

    Raises ``ValueError``/``json.JSONDecodeError`` on malformed input.  The caller
    (``DenyAllowWatcher``) catches this and RETAINS the previous good snapshot — a
    typo must never silently widen or drop the denylist.
    """
    doc = json.loads(raw_text)
    if not isinstance(doc, dict):
        raise ValueError("denylist root must be a JSON object")
    deny = doc.get("denylist") or {}
    allow = doc.get("allowlist") or {}
    if not isinstance(deny, dict) or not isinstance(allow, dict):
        raise ValueError("'denylist' and 'allowlist' must be JSON objects")
    return DenyAllowSnapshot(
        denied_creators=_coerce_str_set(deny.get("creators"), "wallet"),
        denied_mints=_coerce_str_set(deny.get("mints"), "mint"),
        allowed_creators=_coerce_str_set(allow.get("creators"), "wallet"),
        allowed_mints=_coerce_str_set(allow.get("mints"), "mint"),
        source_mtime_ns=mtime_ns,
        source_size=size,
        version=str(doc.get("version", "")),
    )


# ---------------------------------------------------------------------------
# Telemetry seam — we INVOKE a sink; we do not author Prometheus here.
# ---------------------------------------------------------------------------


@runtime_checkable
class DenylistTelemetry(Protocol):
    """Optional telemetry sink for denylist pre-gate rejects + reloads."""

    def on_denylist_reject(self, mint: str, flag: str, key: str) -> None: ...

    def on_denylist_reload(self, *, denied: int, allowed: int, ok: bool) -> None: ...


# ---------------------------------------------------------------------------
# The watcher — owns the current snapshot + hot-reload.  FAST path reads it.
# ---------------------------------------------------------------------------


@dataclass
class DenyAllowWatcher:
    """File-backed, hot-reloadable holder of the denylist/allowlist snapshot.

    THE PRE-GATE FILTER.  Call ``check_keys(creator, mint, ...)`` on the FAST path
    BEFORE ``PreTradeSafetyGate.evaluate_fast``.  It returns a ``RedFlagHit`` if the
    creator or mint is denylisted (REJECT, de-risk-only) or ``None`` otherwise.  The
    allowlist is observable via ``is_allowed`` for the dashboard but is NOT consulted
    to pass anything — a ``None`` here means "no denylist opinion; proceed to the
    safety gate", never "skip the safety gate".

    Hot-reload (``maybe_reload``) is an out-of-band, SLOW-loop tick.  It is fail-safe:
    a missing file falls back to empty-once-at-start; a malformed file KEEPS the
    previous good snapshot.
    """

    path: Path = field(default_factory=lambda: DEFAULT_DENYLIST_PATH)
    reload_throttle_s: float = DEFAULT_RELOAD_THROTTLE_S
    telemetry: DenylistTelemetry | None = None
    # Injected clock for deterministic tests (defaults to monotonic wall clock).
    _now: object = field(default=time.monotonic, repr=False)

    _snapshot: DenyAllowSnapshot = field(default_factory=DenyAllowSnapshot.empty, init=False)
    _last_stat_at: float = field(default=float("-inf"), init=False)
    _loaded_mtime_ns: int = field(default=-1, init=False)
    _loaded_size: int = field(default=-1, init=False)

    def __post_init__(self) -> None:
        # Eager first load so the FAST path is never running on an empty snapshot
        # when a file exists.  A missing file leaves the empty snapshot in place.
        # Stamp the throttle clock so the FIRST ``maybe_reload`` respects the window
        # measured from construction, not from -inf.
        self._last_stat_at = self._clock()
        self._load(force=True)

    # -- the hot path: O(1) membership, REJECT-only -------------------------

    @property
    def snapshot(self) -> DenyAllowSnapshot:
        """Current resolved snapshot (the FAST path reads this reference)."""
        return self._snapshot

    def check_keys(
        self,
        *,
        creator: str | None,
        mint: str | None,
        event_slot: int = 0,
        event_block_time_ms: int = 0,
    ) -> RedFlagHit | None:
        """PRE-GATE FILTER.  Return a ``RedFlagHit`` if denylisted, else ``None``.

        DENYLIST WINS: a creator OR mint hit rejects even if the other key is on the
        allowlist (fail-closed).  The allowlist is NEVER consulted here to *pass* a
        candidate — a ``None`` return means "no denylist opinion; run the safety
        gate", not "trusted, skip the gate".

        ``event_slot`` / ``event_block_time_ms`` do not affect the decision; they are
        accepted so the caller can stamp the reject at the decision slot (point-in-
        time) and are otherwise ignored.
        """
        snap = self._snapshot  # single read of the atomic reference
        if snap.is_creator_denied(creator):
            if self.telemetry is not None:
                self.telemetry.on_denylist_reject(
                    mint or "", RedFlag.CREATOR_DENYLISTED.value, creator or ""
                )
            # ``measured``/``threshold`` are None: this is a boolean membership veto,
            # not a numeric comparison.  The flag code carries the meaning.
            return RedFlagHit(RedFlag.CREATOR_DENYLISTED, None, None)
        if snap.is_mint_denied(mint):
            if self.telemetry is not None:
                self.telemetry.on_denylist_reject(
                    mint or "", RedFlag.MINT_DENYLISTED.value, mint or ""
                )
            return RedFlagHit(RedFlag.MINT_DENYLISTED, None, None)
        return None

    def is_allowed(self, *, creator: str | None, mint: str | None) -> bool:
        """Dashboard/audit helper: True iff the creator or mint is TRUSTED.

        WARNING — being trusted does NOT skip the safety gate.  This is read by the
        operator dashboard / logging only.  No entry path may branch on this to
        bypass ``PreTradeSafetyGate``.  (Encoded as a separate method precisely so it
        can never be confused with ``check_keys``: there is no method here that turns
        'allowed' into 'pass the gate'.)
        """
        snap = self._snapshot
        return snap.is_creator_allowed(creator) or snap.is_mint_allowed(mint)

    # -- out-of-band reload (SLOW loop) -------------------------------------

    def maybe_reload(self, *, force: bool = False) -> bool:
        """Throttled hot-reload.  Returns True iff the snapshot was swapped.

        Stats the file at most once per ``reload_throttle_s``.  Swaps the snapshot
        only when (mtime, size) changed AND the file parsed cleanly.  A malformed
        file is logged and IGNORED — the previous good snapshot is retained.  NOT
        called on the hot path; this is a SLOW-loop/background tick.
        """
        now = self._clock()
        if not force and (now - self._last_stat_at) < self.reload_throttle_s:
            return False
        self._last_stat_at = now
        return self._load(force=force)

    # -- internals ----------------------------------------------------------

    def _clock(self) -> float:
        clk = self._now
        return float(clk()) if callable(clk) else float(clk)  # type: ignore[arg-type]

    def _load(self, *, force: bool) -> bool:
        try:
            st = self.path.stat()
        except FileNotFoundError:
            # No file => keep whatever we have (empty at start).  Not an error:
            # an absent denylist denies nothing and is not risk-increasing.
            logger.warning("denylist file not found at %s; keeping current snapshot", self.path)
            return False
        except OSError as exc:
            logger.warning("denylist stat failed (%s); keeping current snapshot", exc)
            return False

        mtime_ns = st.st_mtime_ns
        size = st.st_size
        if not force and mtime_ns == self._loaded_mtime_ns and size == self._loaded_size:
            return False  # unchanged

        try:
            text = self.path.read_text(encoding="utf-8")
            new_snap = parse_snapshot(text, mtime_ns=mtime_ns, size=size)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            # FAIL-SAFE: a malformed/typo'd file must NEVER widen or drop the
            # denylist.  Keep the previous good snapshot; do not advance the
            # loaded-mtime so a corrected file is picked up next tick.
            logger.error(
                "denylist reload REJECTED (malformed file %s: %s); retaining previous snapshot",
                self.path,
                exc,
            )
            if self.telemetry is not None:
                self.telemetry.on_denylist_reload(
                    denied=len(self._snapshot.denied_creators) + len(self._snapshot.denied_mints),
                    allowed=len(self._snapshot.allowed_creators)
                    + len(self._snapshot.allowed_mints),
                    ok=False,
                )
            return False

        # Atomic reference swap (atomic in CPython): the FAST path either sees the
        # whole old snapshot or the whole new one, never a half-built set.
        self._snapshot = new_snap
        self._loaded_mtime_ns = mtime_ns
        self._loaded_size = size
        denied = len(new_snap.denied_creators) + len(new_snap.denied_mints)
        allowed = len(new_snap.allowed_creators) + len(new_snap.allowed_mints)
        logger.info(
            "denylist loaded: %d denied keys, %d allowed keys (version=%s)",
            denied,
            allowed,
            new_snap.version,
        )
        if self.telemetry is not None:
            self.telemetry.on_denylist_reload(denied=denied, allowed=allowed, ok=True)
        return True


# ---------------------------------------------------------------------------
# Composed pre-gate — denylist THEN the safety gate (the wiring contract).
# ---------------------------------------------------------------------------


@runtime_checkable
class _SafetyGateLike(Protocol):
    """Structural view of ``PreTradeSafetyGate.evaluate_fast``."""

    def evaluate_fast(self, inputs: object) -> object: ...


@dataclass(frozen=True)
class PreGateResult:
    """Result of the composed denylist-then-safety-gate evaluation.

    REJECT-or-PASS only.  ``rejected_by_denylist`` records WHEN the denylist short-
    circuited (so the dashboard distinguishes a pre-gate denylist reject from a
    safety-gate reject).  There is no field that sizes/triggers anything — like the
    safety gate, this is a veto report.
    """

    mint: str
    passed: bool
    # The denylist hit, if any (None when the denylist had no opinion).
    denylist_hit: RedFlagHit | None
    # The safety-gate decision, if it was reached (None when the denylist short-
    # circuited BEFORE the gate ran — proving the gate is *after* the denylist).
    gate_decision: object | None
    # Audit-only: was this candidate on the trusted allowlist?  This NEVER changed
    # the outcome (the gate still ran); it is surfaced for the dashboard.
    was_allowlisted: bool
    event_slot: int
    event_block_time_ms: int

    @property
    def rejected_by_denylist(self) -> bool:
        return self.denylist_hit is not None


def evaluate_with_denylist(
    *,
    watcher: DenyAllowWatcher,
    gate: _SafetyGateLike,
    safety_inputs: object,
    creator: str | None,
    mint: str,
    event_slot: int,
    event_block_time_ms: int,
) -> PreGateResult:
    """Run the DENYLIST pre-filter, THEN the safety gate.  REJECT-only, fail-closed.

    Order is load-bearing:
      1. denylist ``check_keys`` — a denylisted creator/mint REJECTS *before* the
         safety gate even runs (``gate_decision is None`` proves the gate did not
         green-light a denylisted token);
      2. otherwise the FULL ``PreTradeSafetyGate.evaluate_fast`` runs — and it runs
         REGARDLESS of allowlist membership.  Being trusted does NOT skip step 2.

    This is the single function the SNIPE entry path should call; it makes "skip the
    safety gate because trusted" structurally inexpressible (there is no branch that
    returns ``passed=True`` without ``gate_decision.passed`` being True).
    """
    was_allowlisted = watcher.is_allowed(creator=creator, mint=mint)

    hit = watcher.check_keys(
        creator=creator,
        mint=mint,
        event_slot=event_slot,
        event_block_time_ms=event_block_time_ms,
    )
    if hit is not None:
        # Denylist short-circuit: the safety gate is NOT consulted (gate_decision
        # stays None) — but the candidate is REJECTED, so nothing un-gated proceeds.
        return PreGateResult(
            mint=mint,
            passed=False,
            denylist_hit=hit,
            gate_decision=None,
            was_allowlisted=was_allowlisted,
            event_slot=event_slot,
            event_block_time_ms=event_block_time_ms,
        )

    # NOT denylisted (and allowlist membership is irrelevant to this branch): the
    # FULL safety gate runs.  A trusted candidate gets exactly the same scrutiny.
    decision = gate.evaluate_fast(safety_inputs)
    passed = bool(getattr(decision, "passed", False))
    return PreGateResult(
        mint=mint,
        passed=passed,
        denylist_hit=None,
        gate_decision=decision,
        was_allowlisted=was_allowlisted,
        event_slot=event_slot,
        event_block_time_ms=event_block_time_ms,
    )
