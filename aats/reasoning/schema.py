"""Schema-enforced LLM output + decision-state types for the de-risk-only Reasoner (T-313).

THE LLM SPEAKS ONLY IN A VALID OBJECT, OR IT DID NOT SPEAK
=========================================================
The LLM's raw structured output is validated into `LLMRawVerdict` (a pydantic model)
BEFORE anything is trusted.  We never parse free text with a regex.  An invalid object
(bad enum, out-of-range confidence, malformed JSON, missing field) is a HARD REJECT —
the caller fails safe to the most conservative action (HOLD / veto), never a guess.

`LLMRawVerdict` is INTERNAL to the reasoning module.  The bus-facing contract is
`aats.contracts.models.ReasoningVerdict`, whose `ReasoningAction` enum has NO
risk-increase member (ADR-0006).  The clamp (clamp.py) maps a raw LLM signal onto the
de-risk-only action space; an LLM that asks to size up is dropped on the floor.

WHY A SEPARATE RAW SCHEMA
=========================
The frontier/local LLM is prompted to emit a *signal* in the human vocabulary
{"Strong Buy","Weak Buy","Hold","Sell","Strong Sell"} plus three booleans
(veto / narrative_failure / abort-class) and a calibrated confidence.  That richer
vocabulary is deliberately *constrained* before it can touch risk: the clamp turns
"Strong Buy" into HOLD (we never up-signal) and turns "Sell"/veto/narrative_failure
into the de-risk actions the contract permits.  Keeping the raw schema separate makes
the asymmetry auditable: `action_received_raw` records exactly what the LLM tried.

NO win-rate field anywhere (HONESTY CLAUSE).  NO point price field (locked decision 9).
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, field_validator


class DecisionSignalLabel(StrEnum):
    """The human-vocabulary signal the LLM is allowed to emit (raw, pre-clamp).

    This is the LLM's *opinion*.  It is NOT a control signal — the clamp (clamp.py)
    can only ever narrow risk relative to the quant decision, so a bullish label here
    can never raise exposure.  The enum is closed: an out-of-set string is a parse
    failure and fails safe to HOLD.
    """

    STRONG_BUY = "Strong Buy"
    WEAK_BUY = "Weak Buy"
    HOLD = "Hold"
    SELL = "Sell"
    STRONG_SELL = "Strong Sell"


# Ordering for monotonicity proofs: higher index = more bullish = more risk.
# The clamp must NEVER move the applied action up this scale relative to quant.
_SIGNAL_RISK_ORDER: dict[DecisionSignalLabel, int] = {
    DecisionSignalLabel.STRONG_SELL: 0,
    DecisionSignalLabel.SELL: 1,
    DecisionSignalLabel.HOLD: 2,
    DecisionSignalLabel.WEAK_BUY: 3,
    DecisionSignalLabel.STRONG_BUY: 4,
}


def signal_risk_rank(label: DecisionSignalLabel) -> int:
    """Rank a signal label by how much risk it implies (higher = more bullish)."""
    return _SIGNAL_RISK_ORDER[label]


class LLMRawVerdict(BaseModel):
    """The schema the LLM MUST emit.  Validation failure => hard reject => fail-safe.

    This is what `instructor` (or constrained decoding / function-calling) coerces the
    model output into.  Every field is validated:
      - `decision_signal` is a closed enum (bad value => ValidationError)
      - `confidence` is a float in [0, 1] (out of range => ValidationError)
      - the booleans are real booleans (a string "true" that pydantic can't coerce
        => ValidationError)

    The fields can ONLY express a de-risk or a no-op once the clamp runs:
      - `veto`            -> block/abort the entry (de-risk)
      - `narrative_failure` -> the story is dead -> M4 catastrophic FORCE_EXIT (de-risk)
      - `decision_signal` -> opinion; the clamp turns any bullishness into HOLD

    `rationale` is for the AUDIT LOG ONLY.  It is NEVER used for control flow and is
    treated as QUOTED UNTRUSTED TEXT (the same wallets distributing into your buy may
    have written the narrative the model is paraphrasing).
    """

    decision_signal: DecisionSignalLabel
    # Calibrated probability that the de-risk verdict is correct, in [0, 1].
    confidence: float
    # Block/abort a pending entry.
    veto: bool
    # The thesis that justified the position is dead -> M4 catastrophic exit.
    narrative_failure: bool
    # Audit-only rationale.  QUOTED UNTRUSTED TEXT — never parsed, never executed.
    rationale: str

    model_config = {"frozen": True, "extra": "forbid"}

    @field_validator("confidence")
    @classmethod
    def _confidence_in_unit_interval(cls, v: float) -> float:
        if not (0.0 <= v <= 1.0):
            raise ValueError(f"confidence must be in [0, 1], got {v}")
        return v

    @field_validator("rationale")
    @classmethod
    def _rationale_bounded(cls, v: str) -> str:
        # Bound the audit string so an adversarial narrative cannot blow up storage.
        # We do NOT parse it; we only cap its length.  (No regex on model output.)
        MAX_RATIONALE = 2000
        if len(v) > MAX_RATIONALE:
            return v[:MAX_RATIONALE]
        return v


# ---------------------------------------------------------------------------
# Discretized decision-state buckets (for the matrix + the signal-bucket cache).
# These are PURE functions of the inputs — no time, no randomness.  Identical
# market states therefore hash to the same cache key and re-use the same verdict.
# ---------------------------------------------------------------------------


class QuantBucket(StrEnum):
    """Discretized quant probability state (calibrated p adjusted for uncertainty)."""

    BEARISH = "BEARISH"  # low p (or high uncertainty crushing an apparent edge)
    NEUTRAL = "NEUTRAL"
    BULLISH = "BULLISH"  # high p, acceptable uncertainty


class SentimentRisk(StrEnum):
    """Discretized adversarial-sentiment risk derived from the MCS."""

    BENIGN = "BENIGN"  # organic-ish, no coordination flags
    ELEVATED = "ELEVATED"  # some adversarial markers
    MANUFACTURED = "MANUFACTURED"  # coordinated shill / synthetic euphoria


# Default discretization thresholds.  These are gates, not predictions — they only
# ever serve to DE-RISK (push a borderline candidate toward caution).
QUANT_BULLISH_P = 0.62
QUANT_BEARISH_P = 0.45
# High predictive uncertainty cannot create an edge — it only erodes one.  Above this
# band an apparent BULLISH p is demoted toward NEUTRAL (de-risk, never size-up).
UNCERTAINTY_DERISK_BAND = 0.35


def bucket_quant(p_calibrated: float, uncertainty: float) -> QuantBucket:
    """Discretize the calibrated quant probability into a risk bucket.

    High uncertainty DEMOTES a bullish read (de-risk only); it can never promote one.
    This mirrors FR-014/032: high uncertainty shrinks size, never grows it.
    """
    if not (0.0 <= p_calibrated <= 1.0):
        raise ValueError(f"p_calibrated must be in [0, 1], got {p_calibrated}")
    if not (0.0 <= uncertainty <= 1.0):
        raise ValueError(f"uncertainty must be in [0, 1], got {uncertainty}")

    if p_calibrated >= QUANT_BULLISH_P and uncertainty <= UNCERTAINTY_DERISK_BAND:
        return QuantBucket.BULLISH
    # An apparently-bullish p with high uncertainty is demoted, never promoted.
    if p_calibrated >= QUANT_BULLISH_P:
        return QuantBucket.NEUTRAL
    if p_calibrated < QUANT_BEARISH_P:
        return QuantBucket.BEARISH
    return QuantBucket.NEUTRAL


# MCS conviction thresholds (low conviction = adversarial; see tier_b.py).
MCS_BENIGN_CONVICTION = 0.55
MCS_HIGH_SYNCHRONICITY = 0.70
MCS_YOUNG_ACCOUNTS_DAYS = 14.0


def bucket_sentiment(
    *,
    conviction: float,
    synchronicity: float,
    account_age_median_days: float,
    coordinated_shill_flag: bool,
) -> SentimentRisk:
    """Discretize the MCS into an adversarial-sentiment risk bucket.

    Adversarial sentiment (coordinated, low-account-age, high-synchronicity shilling)
    LOWERS conviction — it is NEVER a buy signal (BUILD-DIRECTIVE).  This function
    therefore maps the adversarial markers UP the risk scale; it never reads sentiment
    as bullish.
    """
    if coordinated_shill_flag:
        return SentimentRisk.MANUFACTURED
    if (
        synchronicity >= MCS_HIGH_SYNCHRONICITY
        and account_age_median_days <= MCS_YOUNG_ACCOUNTS_DAYS
    ):
        return SentimentRisk.MANUFACTURED
    if (
        synchronicity >= MCS_HIGH_SYNCHRONICITY
        or account_age_median_days <= MCS_YOUNG_ACCOUNTS_DAYS
        or conviction < MCS_BENIGN_CONVICTION
    ):
        return SentimentRisk.ELEVATED
    return SentimentRisk.BENIGN
