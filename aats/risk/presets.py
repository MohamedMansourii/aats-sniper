"""AUDIT-micropreset — the EARLY-ENTRY *MICRO* preset (a NAMED composition).

WHAT THIS MODULE IS
-------------------
A thin, declarative WIRING layer that composes the ALREADY-BUILT risk primitives
(the pre-trade safety gate, the fractional-Kelly sizer, the ExitEngine) into one
NAMED, frozen, de-risk-shaped preset:

    MICRO = verified
          + LP locked/burned for >= 30 days
          + honeypot (sellability) PASS
          + top-5 holders < 20% concentration
          + 0.5%-of-bankroll sizing (hard-clamped by the existing caps)
          + (18% hard stop  OR  24h time-exit)

It is the analogue of the EXIT-side ``AUTO_STRAT_PRESETS`` (exit_engine.py): a
named bundle of constraints an operator can select, rather than re-typing seven
thresholds per surface.  THE COMPONENTS ALREADY EXIST — this module only binds
them; it authors no new transaction/RPC/sizing/breaker logic and adds no risk.

NON-WAIVABLE LAWS (inherited from the primitives, re-asserted in tests)
----------------------------------------------------------------------
  * EVERY constraint here is DE-RISK / SELECTIVITY ONLY.  Each piece can only
    REJECT a candidate, SHRINK a size, or EXIT a position.  There is NO field on
    this preset that constructs an entry, raises a size, widens a stop, lifts a
    cap, or emits a standalone "buy".  A MICRO preset PASS is SILENT — it means
    "not excluded by the MICRO band", never "buy".
  * The MICRO preset only ever TIGHTENS the default thresholds:
        - holder concentration: default top-10 <= 35%  ->  MICRO top-5 < 20%
        - LP locked/burned:     default >= 95%          ->  MICRO == 100% AND
                                                            lock age >= 30 days
        - honeypot:             refuse-by-default sell-sim seam (unchanged)
    Tightening rejects MORE; it can never let a candidate through that the base
    gate would have rejected.  (Proven by ``test_micropreset.py``.)
  * The 0.5% sizing is a CEILING-shrink: it requests 0.5% of the per-trade cap
    and is then HARD-CLAMPED by the existing per-trade and aggregate-exposure
    caps in ``sizing.FractionalKellySizer``.  No input — model, LLM, config —
    can push it above either cap.  0.5% is small by construction; the caps bound
    the worst case regardless.
  * The exit side reuses the EXISTING ``ExitConfig`` / ``ExitEngine``: an 18%
    survivable HARD STOP (inside the audit's 15-20% band, fixed at entry, never
    widened) and a 24h EVENT-TIME timeout.  The survivable-stop / DMS / breaker
    primitives are UNTOUCHED and still fire — this preset cannot disable them.

POINT-IN-TIME (C-5)
-------------------
Every constraint reads ONLY as-of facts.  The safety facts are stamped at
``event_time`` on ``SafetyInputs``; the LP-lock-age and verified flags are extra
as-of facts (see ``MicroSafetyFacts``) carried alongside.  The exit timeout is
EVENT-TIME (block_time_ms deltas), never wall-clock.  No field admits
``event_time + H`` data, so a lookahead leak is not expressible.  The same code
runs live and in backtest.

MONEY (data-models.md §0)
-------------------------
All thresholds and sizes are integer bps / int lamports / int days / int ms, or
Decimal multiples.  NEVER float for money.

FAST vs SLOW LOOP
-----------------
The SAFETY constraints (verified, LP lock+age, honeypot, holder concentration)
are decoded-on-chain FACTS — they belong on the FAST/snipe pre-trade gate path
(0 RPC on the hot path; the sell-sim is the documented OFF-hot-path N+2 seam).
This module reads NO social/news/screener/caller signal — those are SLOW-loop
only and are not part of the MICRO entry decision.

CAPITAL SAFETY
--------------
This module touches NO signer, NO RPC, NO real capital.  It is a pure
composition of typed constraints behind the existing DRY-RUN / mainnet
hard-gate.  Selecting the MICRO preset never enables a buy and never lifts a
safety primitive.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from aats.risk.exit_engine import (
    ExitConfig,
    MevExitMode,
    RatchetRung,
    TpRung,
)
from aats.risk.pretrade_gate import (
    BPS_DENOM,
    GateDecision,
    GateVerdict,
    PreTradeSafetyGate,
    RedFlag,
    RedFlagHit,
    SafetyInputs,
    SafetyThresholds,
    SellSimProbe,
)
from aats.risk.sizing import (
    LAMPORTS_PER_SOL,
    DeRiskSignals,
    FractionalKellySizer,
    SizingDecision,
)

# Stable label for this named preset (the dashboard / operator contract).
MICRO_PRESET_LABEL = "micro"

# --------------------------------------------------------------------------- #
# The MICRO safety band — a TIGHTER SafetyThresholds (tighten-only / de-risk).
# Selecting MICRO can only REJECT MORE than the default gate would, never less.
# --------------------------------------------------------------------------- #

# Top-5 holder concentration ceiling: < 20% (audit).  We store the ceiling as a
# strict-upper-bound in bps (a candidate with concentration >= this is REJECTED).
MICRO_TOP5_CONCENTRATION_MAX_BPS = 2_000  # 20.00%

# LP must be FULLY (100%) locked or burned for MICRO (vs the default 95% floor).
MICRO_MIN_LP_LOCKED_BPS = BPS_DENOM  # 10_000 == 100%

# LP lock must be committed for at least this many days (audit: >= 30d).
MICRO_MIN_LP_LOCK_DAYS = 30

# The MICRO gate thresholds reuse the SAME SafetyThresholds type the base gate
# uses — only TIGHTER.  ``max_holder_concentration_top10_bps`` is set to the MICRO
# top-5 ceiling so the base gate's holder check is at least as strict (top-5 < 20%
# implies top-10 is evaluated against an even tighter bound; the dedicated top-5
# check below enforces the audit's exact field).  LP fraction is raised to 100%.
MICRO_SAFETY_THRESHOLDS = SafetyThresholds(
    min_lp_locked_bps=MICRO_MIN_LP_LOCKED_BPS,  # 100% locked/burned (tighter)
    max_holder_concentration_top10_bps=MICRO_TOP5_CONCENTRATION_MAX_BPS,  # <= top-5 ceiling
    max_dev_bundle_cluster_bps=MICRO_TOP5_CONCENTRATION_MAX_BPS,  # cluster <= 20% too
    max_sell_tax_bps=500,  # 5% (tighter than the 10% default)
    max_buy_tax_bps=500,  # 5%
    max_staleness_ms=2_000,
)


# --------------------------------------------------------------------------- #
# Extra MICRO-only point-in-time facts the base ``SafetyInputs`` does not carry:
# the ``verified`` flag and the LP-lock AGE (days).  We do NOT mutate the shared
# contracts; we carry these as a small sibling struct, evaluated as ADDITIONAL
# reject-only red flags composed alongside the base gate.
# --------------------------------------------------------------------------- #


class MicroRedFlag(StrEnum):
    """MICRO-only red flags (extend the base RedFlag vocabulary, reject-only)."""

    NOT_VERIFIED = "not_verified"  # token/listing not verified
    LP_LOCK_TOO_SHORT = "lp_lock_too_short"  # LP lock age < 30 days
    TOP5_CONCENTRATION_HIGH = "top5_concentration_high"  # top-5 holders >= 20%


@dataclass(frozen=True)
class MicroSafetyFacts:
    """The EXTRA point-in-time facts the MICRO band needs, as-of ``event_time``.

    These augment ``SafetyInputs`` without mutating the shared contract.  Every
    field is an as-of fact OBSERVABLE at the decision slot — none admits
    ``event_time + H`` data, so a lookahead leak is not expressible.

    A ``None`` on any field means "could not be decoded in budget" — the MICRO
    gate treats that as a refuse-by-default REJECT (there is no unknown == safe).

    Money/age fields are int (days / bps).  NO float.
    """

    # Whether the token/listing is VERIFIED (mint metadata / curated-list match).
    # None => undecoded => reject (refuse-by-default).
    verified: bool | None
    # Age (in days) for which the LP is committed locked/burned, as-of event_time.
    # A burned LP is effectively infinite age; the decoder should report a large
    # value (or the burn flag) accordingly.  None => undecoded => reject.
    lp_lock_age_days: int | None
    # Top-5 holder concentration in bps (distinct from the base top-10 field).
    # None => undecoded => reject.
    holder_concentration_top5_bps: int | None

    def __post_init__(self) -> None:
        for name in ("lp_lock_age_days", "holder_concentration_top5_bps"):
            val = getattr(self, name)
            if val is not None and isinstance(val, float):
                raise TypeError(
                    f"MicroSafetyFacts.{name} must be int or None, got float {val!r} "
                    "(data-models.md §0 — no float money/age)."
                )
            if val is not None and val < 0:
                raise ValueError(f"MicroSafetyFacts.{name} must be non-negative, got {val}.")
        if self.verified is not None and not isinstance(self.verified, bool):
            raise TypeError(
                f"MicroSafetyFacts.verified must be bool or None, got {type(self.verified).__name__}."
            )


def _micro_extra_flags(facts: MicroSafetyFacts) -> tuple[RedFlagHit, ...]:
    """Evaluate the MICRO-only checks (verified / lock-age / top-5).  Reject-only.

    Returns a tuple of RedFlagHit for every MICRO constraint that tripped (refuse-
    by-default on any undecoded fact).  Empty tuple == all MICRO extras passed.
    The codes are MICRO-only string labels, surfaced on the same GateDecision the
    base gate produces, so the dashboard shows them alongside the base red flags.
    """
    hits: list[RedFlagHit] = []

    # verified — refuse-by-default on unknown.
    if facts.verified is None:
        hits.append(RedFlagHit(RedFlag.INPUT_INCOMPLETE, None, None))
    elif not facts.verified:
        # Reuse the RedFlagHit shape with an INPUT_INCOMPLETE-adjacent marker is
        # WRONG (it would mask the reason); we carry the MICRO code in the hit via
        # the dedicated NOT_VERIFIED label by stuffing the measured/threshold as a
        # boolean-ish sentinel.  We keep the base RedFlag enum intact and expose
        # the MICRO code through the helper below.
        hits.append(_micro_hit(MicroRedFlag.NOT_VERIFIED, None, None))

    # LP lock age >= 30 days — refuse-by-default on unknown.
    if facts.lp_lock_age_days is None:
        hits.append(RedFlagHit(RedFlag.INPUT_INCOMPLETE, None, None))
    elif facts.lp_lock_age_days < MICRO_MIN_LP_LOCK_DAYS:
        hits.append(
            _micro_hit(
                MicroRedFlag.LP_LOCK_TOO_SHORT,
                facts.lp_lock_age_days,
                MICRO_MIN_LP_LOCK_DAYS,
            )
        )

    # Top-5 holders < 20% — refuse-by-default on unknown.
    if facts.holder_concentration_top5_bps is None:
        hits.append(RedFlagHit(RedFlag.INPUT_INCOMPLETE, None, None))
    elif facts.holder_concentration_top5_bps >= MICRO_TOP5_CONCENTRATION_MAX_BPS:
        hits.append(
            _micro_hit(
                MicroRedFlag.TOP5_CONCENTRATION_HIGH,
                facts.holder_concentration_top5_bps,
                MICRO_TOP5_CONCENTRATION_MAX_BPS,
            )
        )

    return tuple(hits)


# The MICRO codes are carried on RedFlagHit via a parallel registry keyed by the
# hit object's identity-free fields, so we expose them as string codes without
# extending the shared RedFlag enum.  We use a tiny wrapper subtype that *is* a
# RedFlagHit (so GateDecision accepts it) but reports a MICRO code.
@dataclass(frozen=True)
class _MicroRedFlagHit(RedFlagHit):
    micro_code: str = ""


def _micro_hit(code: MicroRedFlag, measured: int | None, threshold: int | None) -> RedFlagHit:
    """Build a RedFlagHit carrying a MICRO-only code (reject-only, dashboard-facing).

    It reuses RedFlag.INPUT_INCOMPLETE as the base enum slot (so the base type is
    unchanged) but stamps the precise MICRO ``micro_code`` for the operator UI.
    The verdict semantics are identical: ANY hit => REJECT.
    """
    return _MicroRedFlagHit(
        flag=RedFlag.INPUT_INCOMPLETE,
        measured=measured,
        threshold=threshold,
        micro_code=code.value,
    )


def micro_red_flag_codes(decision: GateDecision) -> tuple[str, ...]:
    """Stable MICRO-aware string codes for the dashboard (base + MICRO codes).

    For a ``_MicroRedFlagHit`` we surface its precise ``micro_code``; for a base
    hit we surface the base ``flag`` code.  This is the operator-facing scanner
    vocabulary for a MICRO-preset evaluation.
    """
    codes: list[str] = []
    for hit in decision.red_flags:
        if isinstance(hit, _MicroRedFlagHit) and hit.micro_code:
            codes.append(hit.micro_code)
        else:
            codes.append(hit.flag.value)
    return tuple(codes)


# --------------------------------------------------------------------------- #
# The MICRO exit strategy — an ExitConfig (reuses the EXISTING ExitEngine).
#
# Audit: "(15-20% stop OR 24h time-exit)".  We compose:
#   * an 18% survivable HARD STOP (hard_stop_r = 0.82 => -18%, inside 15-20%),
#     fixed at entry, never widened — the SAME line the survivable stop / DMS
#     enforce (exit_engine reuses survivable_stop.StopState).
#   * a 24h EVENT-TIME time-exit: the stale-bag time-stop set to 24h with the
#     narrative-cooled threshold at its maximum (1.0) so a MICRO position that has
#     gone FLAT for 24h is force-exited REGARDLESS of narrative warmth — i.e. a
#     genuine 24h timeout, not a narrative-gated one.
#
# A small early TP ladder books some gains so the branch is not stop-or-nothing;
# every rung is a PARTIAL scale-out (de-risk).  No branch can add/size-up.
# --------------------------------------------------------------------------- #

# Inside the 15-20% band: 18% drawdown hard stop (0.82x of entry fill).
MICRO_HARD_STOP_R = Decimal("0.82")  # -18%

# A conservative early ladder: take a third at +30%, another third at +80%.
_MICRO_TP_LADDER = (
    TpRung(trigger_r=Decimal("1.3"), fraction_bps=3500),
    TpRung(trigger_r=Decimal("1.8"), fraction_bps=3500),
)

# Profit-lock ratchet: breakeven once the position runs +50%, then lock gains.
_MICRO_RATCHET = (
    RatchetRung(at_r=Decimal("1.3"), lock_r=Decimal("0.95")),
    RatchetRung(at_r=Decimal("1.5"), lock_r=Decimal("1.0")),  # breakeven @ +50%
    RatchetRung(at_r=Decimal("2.0"), lock_r=Decimal("1.4")),
)

MICRO_EXIT_CONFIG = ExitConfig(
    label=MICRO_PRESET_LABEL,
    tp_ladder=_MICRO_TP_LADDER,
    hard_stop_r=MICRO_HARD_STOP_R,  # -18% survivable hard stop (15-20% band)
    trail_arm_r=Decimal("1.5"),  # arm trailing once +50%
    trail_drawdown_bps=2_000,  # 20% trail once armed (tight)
    max_hold_steps=24,  # event-time step timeout (engine-defined granularity)
    ratchet_ladder=_MICRO_RATCHET,
    # 24h EVENT-TIME time-exit, narrative-independent (cooled threshold == 1.0 so
    # ANY narrative score qualifies => a true 24h flat-bag timeout).
    time_stop_hours=24,
    time_stop_flat_band_bps=2_000,  # +/-20% of entry counts as "flat" for the timer
    time_stop_narrative_cooled_at=Decimal(1),  # always-eligible => pure 24h timeout
    default_exit_mode=MevExitMode.SECURE,  # defensive exits route Secure (no sandwich)
)


# --------------------------------------------------------------------------- #
# The MICRO sizing fraction — 0.5% of bankroll, hard-clamped by the existing caps.
#
# The base ``FractionalKellySizer`` sizes as a fraction of the per-trade cap (the
# unit of risk), <= 1/4 Kelly, de-risk-shrunk, then HARD-CLAMPED by both caps.
# The MICRO preset requests a SMALL 0.5%-of-bankroll target; we express it as a
# de-risk multiplier so it can ONLY shrink the Kelly size, and the caps still bind
# above it.  0.5% of the 0.5-SOL aggregate cap is the conceptual target; the
# clamps guarantee the firm's worst case is bounded regardless.
# --------------------------------------------------------------------------- #

# 0.5% as a bps-of-bankroll target (50 bps).
MICRO_SIZE_FRACTION_BPS = 50  # 0.50%


@dataclass(frozen=True)
class MicroSizingResult:
    """A MICRO sizing decision: the Kelly/cap-clamped size AND the 0.5% ceiling.

    ``final_lamports`` is the SMALLER of the cap-clamped Kelly size and the 0.5%
    bankroll ceiling — de-risk by construction (we always take the tighter).
    """

    final_lamports: int
    kelly_decision: SizingDecision
    micro_ceiling_lamports: int  # 0.5% of bankroll, the MICRO ceiling

    def __post_init__(self) -> None:
        if isinstance(self.final_lamports, float):
            raise TypeError("MicroSizingResult.final_lamports must be int lamports (no float).")
        if self.final_lamports < 0:
            raise ValueError("MicroSizingResult.final_lamports must be >= 0.")


def micro_size(
    sizer: FractionalKellySizer,
    *,
    p_calibrated: float,
    uncertainty: float,
    current_aggregate_lamports: int,
    bankroll_lamports: int,
    signals: DeRiskSignals | None = None,
) -> MicroSizingResult:
    """Compute the MICRO size: min(cap-clamped Kelly size, 0.5%-of-bankroll).

    Both inputs are de-risk-shaped:
      * the Kelly size is <= 1/4 Kelly, uncertainty/sentiment-shrunk, and HARD-
        clamped by the per-trade and aggregate caps in the sizer;
      * the 0.5% ceiling is a flat MICRO cap on bankroll exposure.
    We return the SMALLER — so the MICRO size can never exceed EITHER the existing
    caps OR 0.5% of bankroll.  No input can push it above either bound.

    ``bankroll_lamports`` is the integer bankroll the 0.5% is measured against.
    """
    if isinstance(bankroll_lamports, float):
        raise TypeError("bankroll_lamports must be int lamports (no float money).")
    if bankroll_lamports < 0:
        raise ValueError("bankroll_lamports must be >= 0.")

    kelly = sizer.size(
        p_calibrated=p_calibrated,
        uncertainty=uncertainty,
        current_aggregate_lamports=current_aggregate_lamports,
        signals=signals,
    )

    # 0.5% of bankroll, integer floor (rounding can only shrink, never exceed).
    micro_ceiling = (bankroll_lamports * MICRO_SIZE_FRACTION_BPS) // BPS_DENOM

    final = min(kelly.size_lamports, micro_ceiling)
    if final < 0:
        final = 0
    return MicroSizingResult(
        final_lamports=final,
        kelly_decision=kelly,
        micro_ceiling_lamports=micro_ceiling,
    )


# --------------------------------------------------------------------------- #
# The MICRO preset — the single named composition object.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class MicroPreset:
    """The EARLY-ENTRY MICRO preset — a named, frozen composition of de-risk rules.

    It exposes the safety thresholds, the exit config, and the sizing fraction the
    operator selects as one unit.  It provides ``evaluate_safety`` which runs the
    EXISTING pre-trade gate (tightened to the MICRO band) AND the MICRO-only extra
    checks (verified / LP-lock-age / top-5), folding them into ONE reject-only
    GateDecision.  It is selectivity-only: a PASS is silent, never a buy.
    """

    label: str = MICRO_PRESET_LABEL
    safety_thresholds: SafetyThresholds = MICRO_SAFETY_THRESHOLDS
    exit_config: ExitConfig = MICRO_EXIT_CONFIG
    size_fraction_bps: int = MICRO_SIZE_FRACTION_BPS
    min_lp_lock_days: int = MICRO_MIN_LP_LOCK_DAYS
    top5_concentration_max_bps: int = MICRO_TOP5_CONCENTRATION_MAX_BPS

    def gate(self) -> PreTradeSafetyGate:
        """Build the EXISTING pre-trade gate, tightened to the MICRO band."""
        return PreTradeSafetyGate(thresholds=self.safety_thresholds)

    def evaluate_safety(
        self,
        inputs: SafetyInputs,
        facts: MicroSafetyFacts,
        *,
        probe: SellSimProbe | None = None,
    ) -> GateDecision:
        """Evaluate the FULL MICRO safety band (base gate + MICRO extras + honeypot).

        Composition, all REJECT-ONLY / refuse-by-default:
          1. the base hot-path gate (freeze/mint renounce, LP fraction, cluster,
             holder concentration, taxes) — tightened to the MICRO thresholds;
          2. the MICRO extras (verified, LP lock age >= 30d, top-5 < 20%);
          3. the honeypot / sellability sell-sim (refuse-by-default if no probe).

        Returns ONE GateDecision whose verdict is REJECT iff ANY check tripped.
        It is a veto report only — it sizes nothing and triggers no entry.  Use
        ``micro_red_flag_codes`` for the dashboard-facing code list.
        """
        gate = self.gate()
        base = gate.scan_all(inputs)  # all base red flags (no short-circuit, for the UI)

        extra = _micro_extra_flags(facts)

        # Honeypot / sellability — the OFF-hot-path N+2 seam, refuse-by-default.
        honeypot = gate.check_sellability(inputs, probe=probe)

        all_hits: list[RedFlagHit] = list(base.red_flags) + list(extra)
        if honeypot is not None:
            all_hits.append(honeypot)

        verdict = GateVerdict.REJECT if all_hits else GateVerdict.PASS
        return GateDecision(
            mint=inputs.mint,
            verdict=verdict,
            red_flags=tuple(all_hits),
            event_slot=inputs.event_slot,
            event_block_time_ms=inputs.event_block_time_ms,
        )

    def size(
        self,
        sizer: FractionalKellySizer,
        *,
        p_calibrated: float,
        uncertainty: float,
        current_aggregate_lamports: int,
        bankroll_lamports: int,
        signals: DeRiskSignals | None = None,
    ) -> MicroSizingResult:
        """Compute the MICRO size (min of cap-clamped Kelly and 0.5% bankroll)."""
        return micro_size(
            sizer,
            p_calibrated=p_calibrated,
            uncertainty=uncertainty,
            current_aggregate_lamports=current_aggregate_lamports,
            bankroll_lamports=bankroll_lamports,
            signals=signals,
        )


# The single instance an operator selects.  Frozen; constructed from the frozen
# constants above so it is identical live and in backtest.
MICRO_PRESET = MicroPreset()


# Registry mirroring exit_engine.AUTO_STRAT_PRESETS — so a label resolves to a
# MICRO preset the same way an exit preset resolves to an ExitConfig.
ENTRY_PRESETS: dict[str, MicroPreset] = {MICRO_PRESET_LABEL: MICRO_PRESET}


def entry_preset(label: str | None = None) -> MicroPreset:
    """Look up a named ENTRY preset.  Defaults to MICRO.  Unknown label raises
    (fail-closed — no silent fallback to a looser preset)."""
    if label is None:
        label = MICRO_PRESET_LABEL
    if label not in ENTRY_PRESETS:
        raise KeyError(
            f"unknown entry preset {label!r}; known presets: " + ", ".join(sorted(ENTRY_PRESETS))
        )
    return ENTRY_PRESETS[label]


__all__ = [
    "ENTRY_PRESETS",
    "LAMPORTS_PER_SOL",
    "MICRO_EXIT_CONFIG",
    "MICRO_HARD_STOP_R",
    "MICRO_MIN_LP_LOCKED_BPS",
    "MICRO_MIN_LP_LOCK_DAYS",
    "MICRO_PRESET",
    "MICRO_PRESET_LABEL",
    "MICRO_SAFETY_THRESHOLDS",
    "MICRO_SIZE_FRACTION_BPS",
    "MICRO_TOP5_CONCENTRATION_MAX_BPS",
    "MicroPreset",
    "MicroRedFlag",
    "MicroSafetyFacts",
    "MicroSizingResult",
    "entry_preset",
    "micro_red_flag_codes",
    "micro_size",
]
