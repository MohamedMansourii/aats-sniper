"""T-323 — Sub-10ms pre-trade SAFETY GATE / token-safety scanner.

THE BLOCK-0 VETO.  This is the ordered, deterministic, refuse-by-default gate the
SNIPE path runs BEFORE an EntryIntent is ever constructed.  It promotes
``sniper_sim/sniper_sim/safety.py`` (the ordered ``GATE_ORDER`` list) from a
sim ``catch_rate`` model to a real, evaluable rule set over point-in-time inputs.

WHAT THIS MODULE IS (and is NOT)
--------------------------------
  * It is the LOCAL hot-path scanner: every check (1..5) is a pure comparison over
    data ALREADY decoded/held at ``event_time`` — **0 RPC on the hot path**, <10ms.
  * It is the token-safety SCANNER: it exports a structured ``red_flags`` list the
    dashboard renders (BUILD-DIRECTIVE token-safety scanner; T-353 consumes it).
  * It is REJECT-ONLY.  The gate can only DE-RISK: PASS (silent — does not size,
    trigger, or widen anything) or REJECT (refuse the entry).  There is no code
    path here that constructs an ``EntryIntent``, sizes a trade, widens a stop, or
    lifts a cap.  A gate cannot add risk — it is structurally a veto.
  * It is REFUSE-BY-DEFAULT.  Missing/stale/unknown input => REJECT, never pass.
    A check that cannot be evaluated inside the budget refuses; it never times out
    into a pass.

WHAT IS *NOT* HERE (off the hot path, by design)
------------------------------------------------
  * The sell-sim / sellability simulation (honeypot dry-run sell) is 30..100ms and
    is EXPLICITLY NOT on the block-0 path (safety.py:6, GATE_ORDER[5]).  It is a
    STUBBED seam (``SellSimProbe`` Protocol) wired at T-329 by
    ``solana-execution-engineer``.  If an N+2 caller invokes it without a probe,
    the seam refuses-by-default (no probe => not-sellable-unknown => REJECT).

POINT-IN-TIME (C-5)
-------------------
  Every field on ``SafetyInputs`` is stamped at ``event_time`` (the LaunchEvent's
  decision slot/block_time).  The gate uses ONLY those as-of values.  It never
  reads wall-clock and never reaches forward to ``event_time + H`` data — there is
  no field on the input that admits a later slot, so a lookahead leak is not
  expressible.  ``data_staleness_ms`` is the freshness budget; over budget => stale
  => REJECT (refuse-by-default), never a silent pass on stale data.

MONEY (data-models.md §0)
-------------------------
  All thresholds and measured quantities are integer base units / bps, or Decimal
  for exact ratios.  NEVER float for money.  (Concentration is bps:int; tax is
  bps:int; LP is lamports:int.)

ASYMMETRIC TRUST
----------------
  The gate consumes ONLY decoded on-chain facts.  No LLM, no social/MCS signal,
  no model probability is read here — those live on the SLOW loop and can only
  de-risk elsewhere.  This gate is on the FAST/SNIPE path: zero LLM, zero RPC.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, runtime_checkable

# Basis-point scale (10_000 bps == 100%).  Integer math only (no float money).
BPS_DENOM = 10_000


# ---------------------------------------------------------------------------
# Red-flag taxonomy — the token-safety scanner's vocabulary (dashboard-facing).
# These string codes are the stable contract the dashboard (T-353) renders.
# ---------------------------------------------------------------------------


class RedFlag(StrEnum):
    """Stable red-flag codes exported to the dashboard token-safety scanner.

    Ordering of the *enum* is documentary; the ORDER OF EVALUATION is fixed in
    ``HOT_CHECK_ORDER`` (cheapest + most-decisive first, short-circuit on first
    fail — promoted from safety.py ``GATE_ORDER``).
    """

    FREEZE_AUTHORITY_SET = "freeze_authority_set"  # 1 — instant reject
    MINT_AUTHORITY_NOT_RENOUNCED = "mint_authority_not_renounced"  # 2
    LP_NOT_LOCKED_OR_BURNED = "lp_not_locked_or_burned"  # 3
    DEV_OR_BUNDLE_CLUSTER = "dev_or_bundle_cluster"  # 4
    HOLDER_CONCENTRATION_HIGH = "holder_concentration_high"  # 4b
    SELL_TAX_TOO_HIGH = "sell_tax_too_high"  # 5
    BUY_TAX_TOO_HIGH = "buy_tax_too_high"  # 5b
    # E2 — file-backed denylist pre-filter (runs BEFORE the hot checks).
    # A denylisted creator wallet or token mint is an instant, pre-gate REJECT.
    # The allowlist NEVER appears as a red flag — it cannot pass anything; it only
    # records that a candidate is trusted, and trust does NOT skip the safety gate.
    CREATOR_DENYLISTED = "creator_denylisted"  # 0 — known scam creator wallet
    MINT_DENYLISTED = "mint_denylisted"  # 0b — known scam token mint
    # E15 — serial-deployer / dev-wallet-rugged-before reputation pre-gate veto
    # (aats/risk/deployer_reputation_gate.py + aats/ingestion/deployer_reputation.py).
    # A creator wallet with >= the configured count of PRIOR (point-in-time,
    # leak-free) confirmed RUGGED launches is an instant, pre-gate REJECT.  A
    # single prior rug DOWN-WEIGHTS conviction instead (see the gate module) —
    # it never appears as a red flag on its own.
    SERIAL_DEPLOYER_RUGGED = "serial_deployer_rugged"  # 0c — repeat-rug creator wallet
    # E16 — dev-funded-just-before-launch fresh-wallet pre-gate veto
    # (aats/risk/dev_funding_age_gate.py + aats/ingestion/dev_funding_age.py).
    # A creator wallet whose FIRST observed inbound SOL funding lands within
    # the configured reject window before this launch's deploy slot is an
    # instant, pre-gate REJECT — a known rug tell (freshly-funded burner dev
    # wallet).  A funding age in the wider down-weight band instead shrinks
    # conviction (see the gate module); it never appears as a red flag alone.
    DEV_FUNDED_JUST_BEFORE_LAUNCH = "dev_funded_just_before_launch"  # 0d
    # Refuse-by-default: the creator wallet's funding history could not be
    # conclusively decoded (RPC failure, no visible history, or budget
    # exhausted before reaching a confirmed inbound-funding transaction).
    # Unknown is NEVER treated as "established/safe" — see dev_funding_age.py.
    DEV_FUNDING_UNDECODABLE = "dev_funding_undecodable"  # 0e
    # Refuse-by-default conditions (input could not be evaluated in budget):
    INPUT_STALE = "input_stale"  # data older than the staleness budget
    INPUT_INCOMPLETE = "input_incomplete"  # a required field was missing/unknown
    # Off-hot-path (N+2) sellability seam, stubbed until T-329:
    NOT_SELLABLE = "not_sellable"  # sell-sim said honeypot (or no probe => unknown)


# The hot-path evaluation order — promoted from safety.py GATE_ORDER.
# Cheapest + most decisive first; short-circuit-eligible.  sell-sim is NOT here
# (it is off the hot path; see SellSimProbe below).
HOT_CHECK_ORDER: tuple[RedFlag, ...] = (
    RedFlag.FREEZE_AUTHORITY_SET,
    RedFlag.MINT_AUTHORITY_NOT_RENOUNCED,
    RedFlag.LP_NOT_LOCKED_OR_BURNED,
    RedFlag.DEV_OR_BUNDLE_CLUSTER,
    RedFlag.HOLDER_CONCENTRATION_HIGH,
    RedFlag.SELL_TAX_TOO_HIGH,
    RedFlag.BUY_TAX_TOO_HIGH,
)


# ---------------------------------------------------------------------------
# Thresholds — tighten-only de-risk knobs (a tighter threshold rejects MORE).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SafetyThresholds:
    """Pre-trade gate thresholds.

    These are DE-RISK knobs: every value can only be made stricter (reject more)
    without violating the no-rule-increases-risk law.  Loosening a threshold lets
    more candidates through — that is a config change reviewed elsewhere; the gate
    code does not loosen anything at runtime.

    All values are int bps / int counts / int ms — NO float money.
    """

    # Max acceptable LP NOT-locked fraction.  LP must be >= this fraction
    # locked-or-burned.  10_000 bps == require 100% locked/burned.  Default 9_500
    # bps (95%) tolerates a tiny unlocked dust tranche but rejects a pullable LP.
    min_lp_locked_bps: int = 9_500
    # Max top-10 holder concentration (bps).  Above this => cluster/dump risk.
    max_holder_concentration_top10_bps: int = 3_500  # 35%
    # Max single-cluster (dev/bundle/sniper) holding (bps).
    max_dev_bundle_cluster_bps: int = 2_000  # 20%
    # Max sell tax (bps).  Above this the token is effectively a honeypot on exit.
    max_sell_tax_bps: int = 1_000  # 10%
    # Max buy tax (bps).
    max_buy_tax_bps: int = 1_000  # 10%
    # Freshness budget: inputs older than this (event-time staleness) are REJECTED
    # refuse-by-default.  We never pass on stale point-in-time data.
    max_staleness_ms: int = 2_000

    def __post_init__(self) -> None:
        for name in (
            "min_lp_locked_bps",
            "max_holder_concentration_top10_bps",
            "max_dev_bundle_cluster_bps",
            "max_sell_tax_bps",
            "max_buy_tax_bps",
            "max_staleness_ms",
        ):
            val = getattr(self, name)
            if isinstance(val, float):
                raise TypeError(
                    f"SafetyThresholds.{name} must be int (bps/ms), got float {val!r} "
                    "(data-models.md §0)."
                )
            if val < 0:
                raise ValueError(f"SafetyThresholds.{name} must be non-negative, got {val}.")
        if not (0 <= self.min_lp_locked_bps <= BPS_DENOM):
            raise ValueError("min_lp_locked_bps must be in [0, 10000] bps.")


# ---------------------------------------------------------------------------
# Inputs — the point-in-time facts the gate scans.  ALL stamped at event_time.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SafetyInputs:
    """Decoded on-chain facts about a candidate token, as-of ``event_time``.

    Every field is a fact OBSERVABLE at the decision slot — there is no field that
    admits ``event_time + H`` data, so a lookahead leak is not expressible here.
    The producer (the decoder/feature path on the SLOW or ingest side) stamps
    these from data with observation-slot <= event_time.slot; the gate trusts the
    stamp and never reaches forward.

    Money / bps fields are int.  Ratios that must be exact are Decimal.  NO float.

    A field set to ``None`` means "could not be decoded in budget" — the gate
    treats that as INPUT_INCOMPLETE and REJECTS (refuse-by-default).  There is no
    'unknown == safe' path.
    """

    mint: str
    event_slot: int  # event_time.slot — the as-of anchor (no forward reads)
    event_block_time_ms: int  # event_time.block_time_ms — AUTHORITATIVE clock
    data_staleness_ms: int  # age of freshest source datum vs decode (FR-057)

    # 1. Freeze authority — present means the deployer can FREEZE holders' tokens
    #    (cannot sell).  Must be renounced (None/empty).  None => unknown => reject.
    freeze_authority: str | None
    # 2. Mint authority — present means deployer can mint infinite supply.
    #    Must be renounced.  None => unknown => reject.
    mint_authority: str | None
    # 3. LP lock/burn — fraction of LP tokens that are locked or burned, in bps.
    #    None => unknown => reject.
    lp_locked_or_burned_bps: int | None
    # 4. Dev/bundle/sniper cluster — largest single coordinated cluster holding, bps.
    #    None => unknown => reject.  (Adversarial: a bigger cluster LOWERS safety.)
    dev_bundle_cluster_bps: int | None
    # 4b. Top-10 holder concentration, bps (FeatureFrame.holder_concentration_top10_bps).
    holder_concentration_top10_bps: int | None
    # 5. Sell tax, bps (token-2022 transfer-fee extension / cached).  None => reject.
    sell_tax_bps: int | None
    # 5b. Buy tax, bps.  None => reject.
    buy_tax_bps: int | None

    # Whether the freeze/mint authorities were actually decoded (vs absent-because-
    # we-could-not-read).  This disambiguates "renounced" (decoded, None) from
    # "unknown" (not decoded).  Refuse-by-default: not decoded => reject.
    authorities_decoded: bool = True

    def __post_init__(self) -> None:
        # No float money — int bps / int ms only on the numeric fields.
        _INT_FIELDS = (
            "event_slot",
            "event_block_time_ms",
            "data_staleness_ms",
        )
        for name in _INT_FIELDS:
            val = getattr(self, name)
            if isinstance(val, float):
                raise TypeError(
                    f"SafetyInputs.{name} must be int, got float {val!r} (data-models.md §0)."
                )
        _INT_OR_NONE = (
            "lp_locked_or_burned_bps",
            "dev_bundle_cluster_bps",
            "holder_concentration_top10_bps",
            "sell_tax_bps",
            "buy_tax_bps",
        )
        for name in _INT_OR_NONE:
            val = getattr(self, name)
            if val is not None and isinstance(val, float):
                raise TypeError(
                    f"SafetyInputs.{name} must be int bps or None, got float {val!r} "
                    "(data-models.md §0)."
                )
        if self.event_block_time_ms <= 0:
            raise ValueError("SafetyInputs.event_block_time_ms must be > 0 (real on-chain time).")
        if self.data_staleness_ms < 0:
            raise ValueError("SafetyInputs.data_staleness_ms must be non-negative.")


# ---------------------------------------------------------------------------
# Decision — the gate's only output.  PASS or REJECT.  Never sizes/forces.
# ---------------------------------------------------------------------------


class GateVerdict(StrEnum):
    PASS = "PASS"  # no red flag tripped — the gate is SILENT; it does not size/trigger
    REJECT = "REJECT"  # at least one red flag tripped — entry is REFUSED (de-risk)


@dataclass(frozen=True)
class RedFlagHit:
    """One tripped red flag with the measured value vs the threshold (for the UI).

    ``detail`` is a short machine string (NOT a free-text/LLM string) so the
    dashboard can render it without trusting untrusted input.  Values are ints.
    """

    flag: RedFlag
    measured: int | None  # the measured bps/value, or None when the input was unknown
    threshold: int | None  # the threshold it violated, or None for boolean/unknown checks


@dataclass(frozen=True)
class GateDecision:
    """The gate's verdict + the full red-flag report for the dashboard.

    ``passed`` is True iff NO red flag tripped.  ``red_flags`` is the ordered list
    of every flag that tripped (the scanner shows ALL of them, not just the first
    — the short-circuit only governs *latency*, see ``evaluate_fast`` vs
    ``scan_all``).  This object is REJECT-or-PASS only: there is no field that
    sizes a trade, sets a stop, or instructs execution.  It is a veto report.
    """

    mint: str
    verdict: GateVerdict
    red_flags: tuple[RedFlagHit, ...]
    event_slot: int
    event_block_time_ms: int

    @property
    def passed(self) -> bool:
        return self.verdict is GateVerdict.PASS

    @property
    def red_flag_codes(self) -> tuple[str, ...]:
        """Stable string codes for the dashboard token-safety scanner (T-353)."""
        return tuple(hit.flag.value for hit in self.red_flags)


# ---------------------------------------------------------------------------
# Telemetry seam — we INVOKE a sink; we do not author Prometheus here.
# ---------------------------------------------------------------------------


@runtime_checkable
class GateTelemetry(Protocol):
    """Optional telemetry sink (Prometheus counters in prod)."""

    def on_gate_reject(self, mint: str, flags: tuple[str, ...]) -> None: ...

    def on_gate_pass(self, mint: str) -> None: ...


# ---------------------------------------------------------------------------
# OFF-HOT-PATH sell-sim seam — STUBBED, wired at T-329 (refuse-by-default).
# ---------------------------------------------------------------------------


@runtime_checkable
class SellSimProbe(Protocol):
    """The sellability-simulation seam (honeypot dry-run sell).

    OWNED BY ``solana-execution-engineer`` (T-329); the shared honeypot-sim
    mechanics are co-owned.  This is NOT a hot-path check — it is 30..100ms and
    gates slightly-later (N+2+) entries only (safety.py:6).  The risk gate defines
    the SEAM; T-329 supplies the implementation.

    Contract: returns True iff a dry-run/simulated SELL of a dust amount succeeds
    (the token is sellable).  A probe that cannot determine sellability must return
    False (refuse-by-default) — there is no 'unknown == sellable' answer.
    """

    def is_sellable(self, mint: str, event_slot: int) -> bool: ...


# ---------------------------------------------------------------------------
# The gate.
# ---------------------------------------------------------------------------


@dataclass
class PreTradeSafetyGate:
    """Ordered, deterministic, refuse-by-default pre-trade safety gate.

    Hot path (``evaluate_fast``): the 0-RPC local checks (1..5) in HOT_CHECK_ORDER,
    short-circuiting on the first failure for minimum latency.  No LLM, no RPC, no
    wall-clock.  <10ms.

    Scanner (``scan_all``): the SAME checks but evaluating ALL of them (no
    short-circuit) so the dashboard sees every red flag.  Slightly slower; NOT on
    the block-0 path — used for the token-safety scanner panel.

    Off-hot-path (``check_sellability``): the N+2 sell-sim seam, stubbed until
    T-329; refuse-by-default if no probe is supplied.

    The gate may ONLY reject.  It constructs no Intent, sizes nothing, and holds no
    reference to any entry/sizing path.
    """

    thresholds: SafetyThresholds = field(default_factory=SafetyThresholds)
    enabled: bool = True  # a DISABLED gate still rejects-by-default? No — see note.
    telemetry: GateTelemetry | None = None

    # NOTE on ``enabled``: unlike the sim (where disabling the gate passes
    # everything), a DISABLED real gate is a fail-CLOSED condition for any LIVE
    # path.  We keep ``enabled`` only so a SHADOW/RECORD pass can observe red flags
    # without blocking the recorder; the SNIPE entry path must call ``evaluate_fast``
    # with ``enabled=True``.  ``require_enabled_for_entry`` (below) enforces that a
    # disabled gate can never green-light a real entry.

    # -- the hot path -------------------------------------------------------

    def evaluate_fast(self, inputs: SafetyInputs) -> GateDecision:
        """HOT PATH: ordered checks, short-circuit on first failure.  <10ms, 0 RPC.

        Refuse-by-default: a stale or incomplete input REJECTS before any token
        check runs.  Returns a GateDecision (PASS/REJECT) with the tripped flag(s).
        """
        # Refuse-by-default gate 0a: staleness.  Over-budget point-in-time data is
        # not trusted — we reject rather than pass on stale facts.
        if inputs.data_staleness_ms > self.thresholds.max_staleness_ms:
            return self._reject(
                inputs,
                (
                    RedFlagHit(
                        RedFlag.INPUT_STALE,
                        inputs.data_staleness_ms,
                        self.thresholds.max_staleness_ms,
                    ),
                ),
            )

        # Ordered checks — first failure short-circuits (latency).
        for flag in HOT_CHECK_ORDER:
            hit = self._check_one(flag, inputs)
            if hit is not None:
                return self._reject(inputs, (hit,))

        # No flag tripped — PASS (silent: the gate does not size/trigger anything).
        if self.telemetry is not None:
            self.telemetry.on_gate_pass(inputs.mint)
        return GateDecision(
            mint=inputs.mint,
            verdict=GateVerdict.PASS,
            red_flags=(),
            event_slot=inputs.event_slot,
            event_block_time_ms=inputs.event_block_time_ms,
        )

    def scan_all(self, inputs: SafetyInputs) -> GateDecision:
        """SCANNER (NOT block-0): evaluate ALL checks so the dashboard sees every
        red flag.  Same verdict semantics (any flag => REJECT) but does not
        short-circuit.  Still 0 RPC and pure — fine for the /risk panel; not on the
        latency-critical entry path."""
        hits: list[RedFlagHit] = []
        if inputs.data_staleness_ms > self.thresholds.max_staleness_ms:
            hits.append(
                RedFlagHit(
                    RedFlag.INPUT_STALE, inputs.data_staleness_ms, self.thresholds.max_staleness_ms
                )
            )
        for flag in HOT_CHECK_ORDER:
            hit = self._check_one(flag, inputs)
            if hit is not None:
                hits.append(hit)
        if hits:
            return self._reject(inputs, tuple(hits))
        if self.telemetry is not None:
            self.telemetry.on_gate_pass(inputs.mint)
        return GateDecision(
            mint=inputs.mint,
            verdict=GateVerdict.PASS,
            red_flags=(),
            event_slot=inputs.event_slot,
            event_block_time_ms=inputs.event_block_time_ms,
        )

    # -- per-check logic (pure; integer/Decimal only) -----------------------

    def _check_one(self, flag: RedFlag, inputs: SafetyInputs) -> RedFlagHit | None:
        """Evaluate one ordered check.  Returns a RedFlagHit if it trips, else None.

        Refuse-by-default: a None/unknown input on a required check trips
        INPUT_INCOMPLETE (we never read None as 'safe').
        """
        if flag is RedFlag.FREEZE_AUTHORITY_SET:
            if not inputs.authorities_decoded:
                return RedFlagHit(RedFlag.INPUT_INCOMPLETE, None, None)
            # freeze_authority MUST be renounced (None / empty string).
            if inputs.freeze_authority:
                return RedFlagHit(RedFlag.FREEZE_AUTHORITY_SET, None, None)
            return None

        if flag is RedFlag.MINT_AUTHORITY_NOT_RENOUNCED:
            if not inputs.authorities_decoded:
                return RedFlagHit(RedFlag.INPUT_INCOMPLETE, None, None)
            if inputs.mint_authority:
                return RedFlagHit(RedFlag.MINT_AUTHORITY_NOT_RENOUNCED, None, None)
            return None

        if flag is RedFlag.LP_NOT_LOCKED_OR_BURNED:
            v = inputs.lp_locked_or_burned_bps
            if v is None:
                return RedFlagHit(RedFlag.INPUT_INCOMPLETE, None, None)
            # LP must be >= min_lp_locked_bps locked/burned.  Less => pullable LP.
            if v < self.thresholds.min_lp_locked_bps:
                return RedFlagHit(
                    RedFlag.LP_NOT_LOCKED_OR_BURNED, v, self.thresholds.min_lp_locked_bps
                )
            return None

        if flag is RedFlag.DEV_OR_BUNDLE_CLUSTER:
            v = inputs.dev_bundle_cluster_bps
            if v is None:
                return RedFlagHit(RedFlag.INPUT_INCOMPLETE, None, None)
            if v > self.thresholds.max_dev_bundle_cluster_bps:
                return RedFlagHit(
                    RedFlag.DEV_OR_BUNDLE_CLUSTER, v, self.thresholds.max_dev_bundle_cluster_bps
                )
            return None

        if flag is RedFlag.HOLDER_CONCENTRATION_HIGH:
            v = inputs.holder_concentration_top10_bps
            if v is None:
                return RedFlagHit(RedFlag.INPUT_INCOMPLETE, None, None)
            if v > self.thresholds.max_holder_concentration_top10_bps:
                return RedFlagHit(
                    RedFlag.HOLDER_CONCENTRATION_HIGH,
                    v,
                    self.thresholds.max_holder_concentration_top10_bps,
                )
            return None

        if flag is RedFlag.SELL_TAX_TOO_HIGH:
            v = inputs.sell_tax_bps
            if v is None:
                return RedFlagHit(RedFlag.INPUT_INCOMPLETE, None, None)
            if v > self.thresholds.max_sell_tax_bps:
                return RedFlagHit(RedFlag.SELL_TAX_TOO_HIGH, v, self.thresholds.max_sell_tax_bps)
            return None

        if flag is RedFlag.BUY_TAX_TOO_HIGH:
            v = inputs.buy_tax_bps
            if v is None:
                return RedFlagHit(RedFlag.INPUT_INCOMPLETE, None, None)
            if v > self.thresholds.max_buy_tax_bps:
                return RedFlagHit(RedFlag.BUY_TAX_TOO_HIGH, v, self.thresholds.max_buy_tax_bps)
            return None

        # Unknown flag in the order list — refuse-by-default (should be unreachable).
        return RedFlagHit(RedFlag.INPUT_INCOMPLETE, None, None)

    def _reject(self, inputs: SafetyInputs, hits: tuple[RedFlagHit, ...]) -> GateDecision:
        decision = GateDecision(
            mint=inputs.mint,
            verdict=GateVerdict.REJECT,
            red_flags=hits,
            event_slot=inputs.event_slot,
            event_block_time_ms=inputs.event_block_time_ms,
        )
        if self.telemetry is not None:
            self.telemetry.on_gate_reject(inputs.mint, decision.red_flag_codes)
        return decision

    # -- entry-eligibility guard (fail-closed for LIVE) ---------------------

    def require_enabled_for_entry(self, decision: GateDecision) -> bool:
        """Return True iff this decision may green-light a real entry.

        A disabled gate can NEVER green-light an entry (fail-closed): in
        SHADOW/RECORD the gate may be ``enabled=False`` to observe-without-blocking,
        but the SNIPE entry path must require a PASS from an ENABLED gate.  This is
        the structural fail-closed: no PASS from a disabled gate reaches execution.
        """
        return self.enabled and decision.passed

    # -- OFF-hot-path sellability seam (stubbed until T-329) ----------------

    def check_sellability(
        self,
        inputs: SafetyInputs,
        probe: SellSimProbe | None = None,
    ) -> RedFlagHit | None:
        """OFF the hot path (N+2): the sellability / honeypot sell-sim seam.

        REFUSE-BY-DEFAULT: if no ``probe`` is supplied (T-329 not yet wired), this
        returns a NOT_SELLABLE red-flag hit — the gate treats an un-simulated token
        as not-proven-sellable and rejects it on the N+2 path.  A token is sellable
        ONLY when an actual probe says so.  This is NEVER called on block-0.

        Returns a RedFlagHit (NOT_SELLABLE) if the token failed/cannot-be-simulated,
        else None (sellable).  The caller folds this into an N+2 GateDecision.
        """
        if probe is None:
            # Seam not wired (pre-T-329): refuse-by-default.
            return RedFlagHit(RedFlag.NOT_SELLABLE, None, None)
        if not probe.is_sellable(inputs.mint, inputs.event_slot):
            return RedFlagHit(RedFlag.NOT_SELLABLE, None, None)
        return None
