"""E8 — Tunable discovery / SCREENER filter (late-entry / survivor niche).

THE SLOW-LOOP SELECTIVITY FILTER.  This is NOT on the FAST/snipe hot path.  It is a
SLOW-LOOP, point-in-time, REJECT-or-DOWN-WEIGHT-ONLY screen over the already-fetched
discovery enrichment (``aats/ingestion/enrichment.py`` — DEXScreener / Birdeye, T-301).
It surfaces structured CANDIDATE REASONS for the operator dashboard and the SLOW-loop
reasoner, narrowing the universe to the late-entry / survivor niche.

WHAT THIS MODULE IS (and is emphatically NOT)
---------------------------------------------
  * It is a SLOW-LOOP screen.  Discovery enrichment is seconds-stale HTTP-polled data
    (``EnrichmentMeta.data_staleness_ms``); using it on the snipe hot path would be a
    correctness AND a latency violation.  This filter MUST run on the SLOW loop only,
    against an event-time anchor — never inline on the FAST/snipe path.
  * It is SELECTIVITY / DE-RISK ONLY.  Every band/ratio/spike check can only EXCLUDE a
    candidate (out-of-band => REJECT) or, at most, leave it IN with a recorded reason.
    There is NO code path here that constructs an entry, sizes a trade, sets/widens a
    stop, lifts a cap, or emits a "buy".  A passing screen is SILENT: it returns a
    ``ScreenVerdict.PASS`` that means "not excluded by the discovery band" — never "buy".
    A standalone screen PASS is structurally incapable of triggering an entry.
  * It is REFUSE-BY-DEFAULT on missing data.  A band check whose underlying enrichment
    datum is ``None`` (could not be fetched / not yet indexed) EXCLUDES the candidate
    with an ``INPUT_INCOMPLETE`` reason — there is no "unknown == in-band" path.  An
    over-stale bundle is likewise EXCLUDED.

THE LATE-ENTRY / SURVIVOR NICHE (the band this screen selects)
--------------------------------------------------------------
  A "survivor" is a token that already SURVIVED the launch gauntlet and is in a
  tradable mid-cap window with real, sustained turnover.  The band, all TUNABLE:
    * market-cap band      ~$70K .. ~$11M   (below => too-early/illiquid; above => already-run)
    * minimum liquidity    >= a floor       (no liquidity => no exit => exclude)
    * 24h-volume-to-mcap   >= ~5..10%        (turnover floor: dead tokens are excluded)
    * volume-spike trigger  surfaced as a REASON when 24h volume jumps vs a baseline.
  These are SELECTIVITY knobs: TIGHTENING any of them EXCLUDES MORE.  Loosening them lets
  more candidates through — that is a config change reviewed elsewhere; this code never
  loosens anything at runtime, and the volume-spike "trigger" is a *reason tag*, never a
  buy signal (see ``ScreenReason.VOLUME_SPIKE`` — it is recorded, not acted on).

ASYMMETRIC TRUST / NO STANDALONE BUY
------------------------------------
  Social / news / screener / caller signals are SLOW-LOOP ONLY and may only de-risk.
  This screen is exactly such a signal: it can REJECT a candidate or pass it through to
  the EXISTING safety gate + classifier + asymmetric-trust pipeline — it can never, by
  itself, cause a buy.  The volume-spike reason is contrarian-aware: a spike is recorded
  as INFORMATION the reasoner may weigh DOWN (manufactured hype), never as conviction up.

POINT-IN-TIME (C-5) — NO COMPUTE-TIME LEAK
------------------------------------------
  The screen reads ONLY the enrichment bundle's as-of values and the event-time anchor
  carried on the bundle (``request_slot`` / ``event_time_block_time_ms``).  It never reads
  wall-clock and never reaches forward to ``event_time + H`` data — there is no field on
  the inputs that admits a later slot, so a lookahead leak is not expressible.  The
  staleness budget (``max_staleness_ms``) is enforced against the bundle's recorded
  staleness; over budget => EXCLUDE (refuse-by-default), never a silent pass on stale data.
  The same code path runs in live and in backtest.

MONEY (data-models.md §0)
-------------------------
  Discovery USD values (market cap, liquidity, 24h volume) arrive from the enrichment APIs
  as ``float`` METADATA — they are explicitly NOT trade-sizing money (enrichment.py §0).
  We do NOT do trade-sizing here.  For the band/ratio COMPARISONS we coerce those floats to
  ``Decimal`` via ``Decimal(str(x))`` (shortest round-trip) so the comparisons are exact and
  free of binary-float drift, and all configured thresholds are ``Decimal``.  No screen
  output feeds sizing; sizing money stays integer lamports / Decimal in ``sizing.py``.

CAPITAL SAFETY
--------------
  This module touches NO capital, NO signer, NO RPC.  It is pure over already-fetched
  metadata.  DRY-RUN / mainnet hard-gate are unaffected — a screen PASS never enables a
  buy, so it cannot lift any safety primitive (breaker / survivable-stop / DMS are
  untouched and still fire).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Screen-reason taxonomy — the dashboard / reasoner-facing vocabulary.
# Stable string codes.  EXCLUSION reasons cause REJECT; INFO reasons are tags
# recorded alongside a candidate (they never, by themselves, pass anything).
# ---------------------------------------------------------------------------


class ScreenReason(StrEnum):
    """Stable screener reason codes (dashboard + SLOW-loop reasoner contract).

    EXCLUSION reasons (a hit => candidate is OUT-OF-BAND => REJECT):
      * MCAP_BELOW_BAND / MCAP_ABOVE_BAND — outside the survivor market-cap window.
      * LIQUIDITY_BELOW_MIN — not enough liquidity to exit; exclude.
      * VOLUME_TO_MCAP_BELOW_MIN — turnover too low (dead token); exclude.
      * INPUT_INCOMPLETE — a required enrichment datum was missing/unknown (refuse-by-default).
      * INPUT_STALE — enrichment bundle older than the staleness budget (refuse-by-default).

    INFO reasons (recorded alongside an in-band candidate; NEVER a standalone pass/buy):
      * IN_SURVIVOR_BAND — the candidate is inside the full discovery band (a PASS tag).
      * VOLUME_SPIKE — 24h volume jumped vs the baseline (a CONTRARIAN-AWARE tag the
        reasoner may weight DOWN; it is NOT a buy trigger).
    """

    # -- exclusion reasons (REJECT) --
    MCAP_BELOW_BAND = "mcap_below_band"
    MCAP_ABOVE_BAND = "mcap_above_band"
    LIQUIDITY_BELOW_MIN = "liquidity_below_min"
    VOLUME_TO_MCAP_BELOW_MIN = "volume_to_mcap_below_min"
    INPUT_INCOMPLETE = "input_incomplete"
    INPUT_STALE = "input_stale"
    # -- informational reasons (tags; never a standalone pass) --
    IN_SURVIVOR_BAND = "in_survivor_band"
    VOLUME_SPIKE = "volume_spike"


# Reasons that, when present, mean the candidate is EXCLUDED (REJECT).
_EXCLUSION_REASONS: frozenset[ScreenReason] = frozenset(
    {
        ScreenReason.MCAP_BELOW_BAND,
        ScreenReason.MCAP_ABOVE_BAND,
        ScreenReason.LIQUIDITY_BELOW_MIN,
        ScreenReason.VOLUME_TO_MCAP_BELOW_MIN,
        ScreenReason.INPUT_INCOMPLETE,
        ScreenReason.INPUT_STALE,
    }
)


# ---------------------------------------------------------------------------
# Thresholds — tunable, tighten-only selectivity knobs.  All Decimal/int.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScreenerThresholds:
    """Tunable discovery-band thresholds for the late-entry / survivor niche.

    These are SELECTIVITY knobs: tightening any value EXCLUDES MORE candidates and is
    always safe.  USD bands/floors are ``Decimal`` (exact comparison, no float drift);
    the volume-to-mcap floor is a ``Decimal`` ratio in [0, 1]; the spike multiplier is a
    ``Decimal`` >= 1.  Staleness budget is int ms.

    Defaults match the E8 directive: ~$70K .. ~$11M market-cap band, a liquidity floor,
    a >=10% (1_000 bps-equivalent => 0.10) 24h-volume-to-mcap turnover floor, and a 3x
    volume-spike tag.  None of these are buy triggers — they only narrow / tag.
    """

    # Market-cap band (USD).  Below `min` => too-early/illiquid; above `max` => already-run.
    mcap_min_usd: Decimal = Decimal("70000")
    mcap_max_usd: Decimal = Decimal("11000000")
    # Minimum liquidity (USD).  Below this there is no realistic exit — exclude.
    min_liquidity_usd: Decimal = Decimal("20000")
    # Minimum 24h-volume-to-mcap turnover ratio (fraction in [0,1]).  Default 0.10 (10%).
    min_volume_to_mcap_ratio: Decimal = Decimal("0.10")
    # Volume-spike multiplier: 24h volume >= this * baseline_volume => VOLUME_SPIKE tag.
    # This is a *reason tag only* (contrarian-aware), NEVER a buy trigger.
    volume_spike_multiplier: Decimal = Decimal("3")
    # Freshness budget: enrichment bundles older than this (recorded data_staleness_ms)
    # are EXCLUDED refuse-by-default.  We never pass on stale discovery data.
    max_staleness_ms: int = 60_000

    def __post_init__(self) -> None:
        # Coerce any int/str-configured values to Decimal so callers can pass plain ints.
        for name in (
            "mcap_min_usd",
            "mcap_max_usd",
            "min_liquidity_usd",
            "min_volume_to_mcap_ratio",
            "volume_spike_multiplier",
        ):
            val = getattr(self, name)
            if isinstance(val, Decimal):
                continue
            if isinstance(val, bool):  # bool is an int subclass — reject explicitly
                raise TypeError(f"ScreenerThresholds.{name} must be Decimal/int/str, not bool")
            if isinstance(val, int | str):
                object.__setattr__(self, name, Decimal(str(val)))
            elif isinstance(val, float):
                # Accept float configs (e.g. env-derived) but coerce via str for exactness.
                object.__setattr__(self, name, Decimal(str(val)))
            else:
                raise TypeError(
                    f"ScreenerThresholds.{name} must be Decimal/int/float/str, "
                    f"got {type(val).__name__}"
                )
        # Range / ordering validation.
        if self.mcap_min_usd < 0 or self.mcap_max_usd < 0 or self.min_liquidity_usd < 0:
            raise ValueError("market-cap band and liquidity floor must be non-negative")
        if self.mcap_min_usd > self.mcap_max_usd:
            raise ValueError(
                f"mcap_min_usd ({self.mcap_min_usd}) must be <= mcap_max_usd ({self.mcap_max_usd})"
            )
        if not (Decimal(0) <= self.min_volume_to_mcap_ratio <= Decimal(1)):
            raise ValueError("min_volume_to_mcap_ratio must be a fraction in [0, 1]")
        if self.volume_spike_multiplier < Decimal(1):
            raise ValueError("volume_spike_multiplier must be >= 1 (a spike is an increase)")
        if not isinstance(self.max_staleness_ms, int) or isinstance(self.max_staleness_ms, bool):
            raise TypeError("max_staleness_ms must be int (ms)")
        if self.max_staleness_ms < 0:
            raise ValueError("max_staleness_ms must be non-negative")

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> ScreenerThresholds:
        """Build thresholds from environment (all optional; defaults if unset).

        Env keys (see .env.example E8 block).  Absent keys fall back to the defaults
        above.  Values are parsed as Decimal/int; a malformed value raises (fail-closed
        on config — we do not silently widen the band).
        """
        e = env if env is not None else os.environ

        def _dec(key: str, default: Decimal) -> Decimal:
            raw = e.get(key)
            return Decimal(str(raw)) if raw is not None and raw != "" else default

        def _int(key: str, default: int) -> int:
            raw = e.get(key)
            return int(raw) if raw is not None and raw != "" else default

        return cls(
            mcap_min_usd=_dec("SCREENER_MCAP_MIN_USD", cls.mcap_min_usd),
            mcap_max_usd=_dec("SCREENER_MCAP_MAX_USD", cls.mcap_max_usd),
            min_liquidity_usd=_dec("SCREENER_MIN_LIQUIDITY_USD", cls.min_liquidity_usd),
            min_volume_to_mcap_ratio=_dec(
                "SCREENER_MIN_VOLUME_TO_MCAP_RATIO", cls.min_volume_to_mcap_ratio
            ),
            volume_spike_multiplier=_dec(
                "SCREENER_VOLUME_SPIKE_MULTIPLIER", cls.volume_spike_multiplier
            ),
            max_staleness_ms=_int("SCREENER_MAX_STALENESS_MS", cls.max_staleness_ms),
        )


# ---------------------------------------------------------------------------
# Inputs — the point-in-time discovery facts, as-of the event-time anchor.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScreenInputs:
    """Point-in-time discovery facts about a candidate, as-of the event-time anchor.

    Every field is an as-of value from the SLOW-loop enrichment bundle.  There is no
    field that admits ``event_time + H`` data, so a lookahead leak is not expressible.
    A ``None`` USD field means "could not be fetched/indexed in budget" — the screen
    treats it as INPUT_INCOMPLETE and EXCLUDES (refuse-by-default).

    USD values are float METADATA from the enrichment APIs (enrichment.py §0) — we coerce
    them to Decimal internally for exact comparison; they are NEVER used for trade sizing.
    """

    mint: str
    event_slot: int  # request_slot — the as-of anchor (no forward reads)
    event_block_time_ms: int  # event_time.block_time_ms — AUTHORITATIVE clock
    data_staleness_ms: int  # recorded staleness of the enrichment bundle

    market_cap_usd: float | None
    liquidity_usd: float | None
    volume_usd_h24: float | None
    # Optional baseline 24h volume (e.g. a prior sample / rolling median) for the
    # spike tag.  None => no spike comparison is made (the tag is simply absent — its
    # absence never excludes anything, since the spike is INFO-only).
    baseline_volume_usd_h24: float | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.event_slot, int) or isinstance(self.event_slot, bool):
            raise TypeError("ScreenInputs.event_slot must be int")
        if not isinstance(self.event_block_time_ms, int) or isinstance(
            self.event_block_time_ms, bool
        ):
            raise TypeError("ScreenInputs.event_block_time_ms must be int")
        if self.event_block_time_ms <= 0:
            raise ValueError("ScreenInputs.event_block_time_ms must be > 0 (real on-chain time)")
        if self.data_staleness_ms < 0:
            raise ValueError("ScreenInputs.data_staleness_ms must be non-negative")

    @classmethod
    def from_enrichment_bundle(cls, bundle: object) -> ScreenInputs:
        """Build screen inputs from an ``EnrichmentBundle`` (T-301), point-in-time.

        Reads ONLY as-of fields from the bundle:
          * market cap   <- Birdeye ``market_cap_usd`` (or DEXScreener ``fdv_usd`` fallback)
          * liquidity    <- bundle.liquidity_usd() (DEXScreener primary, Meteora fallback)
          * 24h volume   <- DEXScreener ``volume_usd_h24`` (Birdeye fallback)
          * staleness    <- bundle.max_staleness_ms()  (recorded HTTP-poll lag)
          * anchor       <- bundle.request_slot / event_time_block_time_ms

        This is a SLOW-loop construction — the bundle is the output of an off-hot-path
        HTTP enrichment fetch.  It is never built on the snipe path.
        """
        mint = getattr(bundle, "mint", "")
        request_slot = int(getattr(bundle, "request_slot", 0))
        anchor_ms = int(getattr(bundle, "event_time_block_time_ms", 0))

        dex = getattr(bundle, "dexscreener", None)
        birdeye = getattr(bundle, "birdeye", None)

        # Market cap: Birdeye is primary; DEXScreener FDV is a documented fallback proxy.
        market_cap: float | None = None
        if birdeye is not None and getattr(birdeye, "market_cap_usd", None) is not None:
            market_cap = birdeye.market_cap_usd
        elif dex is not None and getattr(dex, "fdv_usd", None) is not None:
            market_cap = dex.fdv_usd

        # Liquidity: prefer the bundle's resolved helper (DEXScreener -> Meteora fallback).
        liquidity: float | None = None
        liq_fn = getattr(bundle, "liquidity_usd", None)
        if callable(liq_fn):
            liquidity = liq_fn()

        # 24h volume: DEXScreener primary, Birdeye fallback.
        volume: float | None = None
        if dex is not None and getattr(dex, "volume_usd_h24", None) is not None:
            volume = dex.volume_usd_h24
        elif birdeye is not None and getattr(birdeye, "volume_usd_h24", None) is not None:
            volume = birdeye.volume_usd_h24

        # Staleness: the bundle's max recorded staleness across sources (-1 => no data).
        stale_fn = getattr(bundle, "max_staleness_ms", None)
        staleness = stale_fn() if callable(stale_fn) else -1
        # A -1 (no source data) is treated as maximally stale => refuse-by-default.
        staleness_ms = staleness if staleness >= 0 else (anchor_ms if anchor_ms > 0 else 1)

        return cls(
            mint=mint,
            event_slot=request_slot,
            event_block_time_ms=anchor_ms if anchor_ms > 0 else 1,
            data_staleness_ms=staleness_ms,
            market_cap_usd=market_cap,
            liquidity_usd=liquidity,
            volume_usd_h24=volume,
            baseline_volume_usd_h24=None,
        )


# ---------------------------------------------------------------------------
# Verdict + reason hit.  PASS-or-REJECT only.  Never sizes / triggers / buys.
# ---------------------------------------------------------------------------


class ScreenVerdict(StrEnum):
    PASS = "PASS"  # in-band — NOT excluded by discovery; SILENT (does not buy/size/trigger)
    REJECT = "REJECT"  # out-of-band — candidate is EXCLUDED (de-risk / selectivity)


@dataclass(frozen=True)
class ScreenReasonHit:
    """One screener reason with the measured value vs the threshold (for the UI/reasoner).

    ``measured`` / ``threshold`` are Decimal for the USD/ratio comparisons (exact), or
    None for input-completeness reasons.  ``is_exclusion`` marks whether this reason, on
    its own, EXCLUDES the candidate.  INFO reasons (e.g. VOLUME_SPIKE) carry
    ``is_exclusion=False`` and never cause a reject.
    """

    reason: ScreenReason
    measured: Decimal | None
    threshold: Decimal | None

    @property
    def is_exclusion(self) -> bool:
        return self.reason in _EXCLUSION_REASONS


@dataclass(frozen=True)
class ScreenDecision:
    """The screener's verdict + the full reason report.

    ``verdict`` is REJECT iff ANY exclusion reason tripped; otherwise PASS.  ``reasons``
    is the ordered list of every reason (exclusion AND info) the scan found — the
    dashboard / reasoner sees them all.  This object is REJECT-or-PASS only: there is no
    field that sizes a trade, sets a stop, or instructs execution.  A PASS is purely
    "not excluded by the discovery band" — it is NOT a buy.
    """

    mint: str
    verdict: ScreenVerdict
    reasons: tuple[ScreenReasonHit, ...]
    event_slot: int
    event_block_time_ms: int

    @property
    def passed(self) -> bool:
        return self.verdict is ScreenVerdict.PASS

    @property
    def excluded(self) -> bool:
        return self.verdict is ScreenVerdict.REJECT

    @property
    def reason_codes(self) -> tuple[str, ...]:
        """Stable string codes for the dashboard / reasoner (all reasons, in order)."""
        return tuple(hit.reason.value for hit in self.reasons)

    @property
    def exclusion_codes(self) -> tuple[str, ...]:
        """Only the codes that caused exclusion (empty when PASS)."""
        return tuple(hit.reason.value for hit in self.reasons if hit.is_exclusion)

    @property
    def has_volume_spike(self) -> bool:
        """Info tag: did the candidate show a 24h volume spike? (Contrarian-aware INFO.)"""
        return any(hit.reason is ScreenReason.VOLUME_SPIKE for hit in self.reasons)


# ---------------------------------------------------------------------------
# Telemetry seam — we INVOKE a sink; we do not author Prometheus here.
# ---------------------------------------------------------------------------


@runtime_checkable
class ScreenerTelemetry(Protocol):
    """Optional telemetry sink for screener excludes / passes (Prometheus in prod)."""

    def on_screen_reject(self, mint: str, reasons: tuple[str, ...]) -> None: ...

    def on_screen_pass(self, mint: str, reasons: tuple[str, ...]) -> None: ...


# ---------------------------------------------------------------------------
# The screener.
# ---------------------------------------------------------------------------


def _to_decimal(x: float | int | None) -> Decimal | None:
    """Coerce an enrichment USD float to Decimal via shortest round-trip; None passes through."""
    if x is None:
        return None
    return Decimal(str(x))


@dataclass
class DiscoveryScreener:
    """Tunable SLOW-LOOP discovery / survivor-niche screener.  REJECT-or-PASS only.

    ``screen(inputs)`` evaluates the full discovery band over point-in-time enrichment
    facts and returns a ``ScreenDecision``.  It may ONLY exclude (out-of-band => REJECT)
    or pass through (in-band => PASS, silent).  It constructs no Intent, sizes nothing,
    sets no stop, and holds no reference to any entry/sizing path.  The volume-spike
    reason is recorded as a contrarian-aware INFO tag — never a buy trigger.

    This is a SLOW-LOOP component: it is NOT wired onto the FAST/snipe hot path.  Its
    output narrows the universe BEFORE the existing safety gate / classifier / asymmetric-
    trust pipeline runs; it never replaces or bypasses any of them.
    """

    thresholds: ScreenerThresholds = field(default_factory=ScreenerThresholds)
    telemetry: ScreenerTelemetry | None = None

    def screen(self, inputs: ScreenInputs) -> ScreenDecision:
        """Evaluate the full discovery band.  Refuse-by-default on missing/stale data.

        Returns a ``ScreenDecision`` (PASS/REJECT) with every reason found.  ALL checks
        are evaluated (no short-circuit) so the dashboard/reasoner sees every reason; the
        verdict is REJECT iff any exclusion reason tripped.  This is a SLOW-loop screen,
        so completeness over micro-latency is the right trade.
        """
        reasons: list[ScreenReasonHit] = []

        # -- refuse-by-default gate: staleness -------------------------------
        if inputs.data_staleness_ms > self.thresholds.max_staleness_ms:
            reasons.append(
                ScreenReasonHit(
                    ScreenReason.INPUT_STALE,
                    Decimal(inputs.data_staleness_ms),
                    Decimal(self.thresholds.max_staleness_ms),
                )
            )
            # Stale data is untrusted — exclude immediately, do not evaluate the band on it.
            return self._finalize(inputs, reasons)

        mcap = _to_decimal(inputs.market_cap_usd)
        liquidity = _to_decimal(inputs.liquidity_usd)
        volume = _to_decimal(inputs.volume_usd_h24)

        # -- market-cap band -------------------------------------------------
        if mcap is None:
            reasons.append(ScreenReasonHit(ScreenReason.INPUT_INCOMPLETE, None, None))
        else:
            if mcap < self.thresholds.mcap_min_usd:
                reasons.append(
                    ScreenReasonHit(
                        ScreenReason.MCAP_BELOW_BAND, mcap, self.thresholds.mcap_min_usd
                    )
                )
            elif mcap > self.thresholds.mcap_max_usd:
                reasons.append(
                    ScreenReasonHit(
                        ScreenReason.MCAP_ABOVE_BAND, mcap, self.thresholds.mcap_max_usd
                    )
                )

        # -- minimum liquidity ----------------------------------------------
        if liquidity is None:
            reasons.append(ScreenReasonHit(ScreenReason.INPUT_INCOMPLETE, None, None))
        elif liquidity < self.thresholds.min_liquidity_usd:
            reasons.append(
                ScreenReasonHit(
                    ScreenReason.LIQUIDITY_BELOW_MIN,
                    liquidity,
                    self.thresholds.min_liquidity_usd,
                )
            )

        # -- 24h-volume-to-mcap turnover floor ------------------------------
        # ratio = volume / mcap, both required; mcap must be > 0 to form the ratio.
        if volume is None or mcap is None:
            reasons.append(ScreenReasonHit(ScreenReason.INPUT_INCOMPLETE, None, None))
        elif mcap <= 0:
            # A zero/negative market cap cannot form a turnover ratio — refuse-by-default.
            reasons.append(ScreenReasonHit(ScreenReason.INPUT_INCOMPLETE, None, None))
        else:
            ratio = volume / mcap
            if ratio < self.thresholds.min_volume_to_mcap_ratio:
                reasons.append(
                    ScreenReasonHit(
                        ScreenReason.VOLUME_TO_MCAP_BELOW_MIN,
                        ratio,
                        self.thresholds.min_volume_to_mcap_ratio,
                    )
                )

        # -- volume-spike INFO tag (contrarian-aware; NEVER a buy trigger) ---
        # Only meaningful when a baseline is supplied AND current volume is known.
        baseline = _to_decimal(inputs.baseline_volume_usd_h24)
        if (
            volume is not None
            and baseline is not None
            and baseline > 0
            and volume >= baseline * self.thresholds.volume_spike_multiplier
        ):
            # INFO reason: recorded for the reasoner to WEIGH DOWN if it looks
            # manufactured.  is_exclusion is False — this never rejects or passes
            # anything on its own.
            reasons.append(
                ScreenReasonHit(
                    ScreenReason.VOLUME_SPIKE,
                    volume,
                    baseline * self.thresholds.volume_spike_multiplier,
                )
            )

        return self._finalize(inputs, reasons)

    def _finalize(self, inputs: ScreenInputs, reasons: list[ScreenReasonHit]) -> ScreenDecision:
        """Compute the verdict from the reason list and tag the IN_SURVIVOR_BAND INFO."""
        any_exclusion = any(hit.is_exclusion for hit in reasons)

        if not any_exclusion:
            # In-band: record the IN_SURVIVOR_BAND info tag so the dashboard can show WHY
            # this candidate surfaced.  This is an INFO tag, not a pass-by-itself — the
            # verdict is PASS only because NO exclusion reason tripped, never because this
            # tag is present.
            reasons.append(ScreenReasonHit(ScreenReason.IN_SURVIVOR_BAND, None, None))
            verdict = ScreenVerdict.PASS
        else:
            verdict = ScreenVerdict.REJECT

        decision = ScreenDecision(
            mint=inputs.mint,
            verdict=verdict,
            reasons=tuple(reasons),
            event_slot=inputs.event_slot,
            event_block_time_ms=inputs.event_block_time_ms,
        )

        if self.telemetry is not None:
            if decision.passed:
                self.telemetry.on_screen_pass(inputs.mint, decision.reason_codes)
            else:
                self.telemetry.on_screen_reject(inputs.mint, decision.exclusion_codes)

        return decision

    def screen_bundle(self, bundle: object) -> ScreenDecision:
        """Convenience: build ``ScreenInputs`` from a T-301 ``EnrichmentBundle`` and screen.

        SLOW-LOOP ONLY — the bundle is the output of an off-hot-path HTTP enrichment fetch.
        """
        return self.screen(ScreenInputs.from_enrichment_bundle(bundle))
