"""TipController — edge-bounded dynamic tip pricing.

The cardinal sin (latency-budget.md §6, EDGE-VERDICT C.5) is escalating tips
into a latency war a solo desk cannot win: that just converts edge into
validator revenue.  So the tip here is bounded on BOTH sides:

  * FLOOR  — the LIVE tip-floor percentile (read from the injectable
             `TipStreamProtocol`, NEVER a hardcoded constant; AC-014).  You
             must clear recent landed tips to have any chance of landing.
  * CEILING — a hard fraction (default 0.30x) of THIS trade's expected edge.
             A tip is pure cost; it never improves the fill, only the odds.
             jito_tip_cap_frac = 0.30 (data-models.md §, tips.py invariant).

The decisive rule is the INVERSE of "bid harder": **if the live floor exceeds
the edge-derived ceiling, the race is priced out and we DO NOT SUBMIT.**  We do
not bid up to the floor "to be safe" — that is a guaranteed negative-edge tax.
This is the niche from latency-budget.md §6: we win where the race is clean
tip+bundle execution and rug-avoidance, and we *refuse* the auctions we are
priced out of.

Every decision logs tip-as-%-of-expected-edge so the bleed is auditable.

Money: every quantity is int lamports / int bps.  No float money.  Ever.
Point-in-time: the tip is priced from a snapshot stamped at-or-before the
decision slot (C-5); a future snapshot is refused upstream by the stream.
"""
from __future__ import annotations

import logging
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel

from aats.mev.tip_stream import StaleTipStreamError, TipFloorSnapshot, TipStreamProtocol

logger = logging.getLogger(__name__)

LAMPORTS_PER_SOL = 1_000_000_000

# Hard ceiling fraction: a tip is never more than this * expected edge.
# Source: data-models.md jito_tip_cap_frac = Decimal("0.30"); tips.py edge_cap_frac.
# Stored as a Decimal so the cap math never touches float.
DEFAULT_EDGE_CAP_FRAC = Decimal("0.30")

# Contention bucket -> which live percentile we must clear to have a chance.
# low contention: clear the median; high contention: clear the 95th.
_BUCKET_TO_PERCENTILE: dict[str, int] = {
    "low": 50,
    "medium": 75,
    "high": 95,
}


class TipDecisionKind(StrEnum):
    """The outcome of pricing a tip."""

    SUBMIT = "SUBMIT"  # tip priced within [floor, edge_ceiling]; safe to submit
    DO_NOT_SUBMIT = "DO_NOT_SUBMIT"  # priced out / no edge / no live floor


class TipDecision(BaseModel):
    """The priced tip plus the full audit trail for one trade.

    Frozen + all-integer money.  `tip_lamports` is 0 when kind=DO_NOT_SUBMIT;
    the caller MUST check `kind` and never read `tip_lamports` as "the tip to
    pay" without it.
    """

    kind: TipDecisionKind
    tip_lamports: int  # 0 when DO_NOT_SUBMIT
    # audit — every number that produced the decision
    expected_edge_lamports: int
    edge_ceiling_lamports: int  # floor(edge_cap_frac * expected_edge)
    live_floor_lamports: int  # the live percentile we had to clear
    percentile_used: int
    contention_bucket: str
    snapshot_slot: int  # event-time slot of the tip snapshot used (point-in-time proof)
    reason: str
    # tip-as-%-of-expected-edge in bps (integer); the bleed metric (cardinal-sin guard)
    tip_pct_of_edge_bps: int

    model_config = {"frozen": True}


class TipController:
    """Prices the Jito tip from the LIVE floor + this trade's expected edge.

    INJECTABLE: the tip stream is injected at construction (no hidden global,
    no network in the hot path).  In production this is the Redis-backed live
    reader; in tests it is `StaticTipStream`.

    The controller NEVER hardcodes a tip value.  The only constants it carries
    are the edge-cap FRACTION (a risk parameter, not a tip) and the bucket->
    percentile mapping (which percentile to clear, not how many lamports).
    """

    def __init__(
        self,
        tip_stream: TipStreamProtocol,
        *,
        edge_cap_frac: Decimal | str = DEFAULT_EDGE_CAP_FRAC,
    ) -> None:
        self._stream = tip_stream
        # Coerce to Decimal so the cap is exact; reject float to keep money exact.
        if isinstance(edge_cap_frac, float):
            raise TypeError(
                "edge_cap_frac must be Decimal or str, not float (exact-money rule)."
            )
        cap = Decimal(edge_cap_frac)
        if not (Decimal("0") < cap <= Decimal("1")):
            raise ValueError(f"edge_cap_frac must be in (0, 1], got {cap}")
        self._edge_cap_frac = cap

    # ------------------------------------------------------------------ #
    # The one public method on the hot path.
    # ------------------------------------------------------------------ #

    def price_tip(
        self,
        *,
        expected_edge_lamports: int,
        decision_slot: int,
        contention_bucket: Literal["low", "medium", "high"] = "medium",
    ) -> TipDecision:
        """Price the tip for ONE trade, or refuse.

        Args:
            expected_edge_lamports: the trade's expected NET edge in lamports
                (position-sized, model-probability-weighted), computed UPSTREAM
                by the cost model.  Integer lamports.  The controller never
                invents edge; it only bounds the tip by the edge it is given.
            decision_slot: the event-time slot of the decision.  The live
                snapshot must be valid at-or-before this slot (point-in-time).
            contention_bucket: which live percentile we must clear.

        Returns:
            TipDecision — SUBMIT with a bounded tip, or DO_NOT_SUBMIT with the
            reason.  The caller checks `.kind` before paying `.tip_lamports`.

        Refuses (DO_NOT_SUBMIT) when:
            * expected_edge_lamports <= 0          (no edge to spend a tip on)
            * no live snapshot valid at decision_slot (cannot price; never guess)
            * the live floor exceeds the edge ceiling (priced out of the race)
        """
        if isinstance(expected_edge_lamports, float):
            raise TypeError(
                "expected_edge_lamports must be int (lamports), not float (exact-money rule)."
            )

        # 1. No edge -> nothing to spend a tip on.  Refuse before touching the stream.
        if expected_edge_lamports <= 0:
            return self._refuse(
                reason="no_expected_edge",
                expected_edge_lamports=max(0, expected_edge_lamports),
                edge_ceiling_lamports=0,
                live_floor_lamports=0,
                percentile_used=0,
                contention_bucket=contention_bucket,
                snapshot_slot=-1,
            )

        # 2. Read the LIVE floor point-in-time.  No live floor -> do not submit
        #    (we never substitute a hardcoded constant — that is the banned pattern).
        try:
            snap: TipFloorSnapshot = self._stream.current(decision_slot=decision_slot)
        except StaleTipStreamError as exc:
            logger.warning(
                "tip_controller.no_live_floor",
                extra={"decision_slot": decision_slot, "reason": str(exc)},
            )
            return self._refuse(
                reason="no_live_tip_floor",
                expected_edge_lamports=expected_edge_lamports,
                edge_ceiling_lamports=0,
                live_floor_lamports=0,
                percentile_used=0,
                contention_bucket=contention_bucket,
                snapshot_slot=-1,
            )

        percentile = _BUCKET_TO_PERCENTILE[contention_bucket]
        live_floor = snap.percentile(percentile)

        # 3. The hard ceiling: floor(edge_cap_frac * expected_edge).  Exact via Decimal,
        #    then floored to integer lamports.  NEVER float.
        edge_ceiling = int(
            (self._edge_cap_frac * Decimal(expected_edge_lamports)).to_integral_value(
                rounding="ROUND_FLOOR"
            )
        )

        tip_pct_of_edge_bps = _pct_of_edge_bps(live_floor, expected_edge_lamports)

        # 4. The decisive rule: if clearing the live floor would breach the edge
        #    ceiling, the race is priced out.  DO NOT bid up to the floor.
        if live_floor > edge_ceiling:
            logger.info(
                "tip_controller.priced_out",
                extra={
                    "decision_slot": decision_slot,
                    "snapshot_slot": snap.as_of_slot,
                    "live_floor_lamports": live_floor,
                    "edge_ceiling_lamports": edge_ceiling,
                    "expected_edge_lamports": expected_edge_lamports,
                    "percentile_used": percentile,
                    "contention_bucket": contention_bucket,
                    "tip_pct_of_edge_bps": tip_pct_of_edge_bps,
                    "reason": "live_floor_exceeds_edge_ceiling",
                },
            )
            return self._refuse(
                reason="priced_out_live_floor_exceeds_edge_ceiling",
                expected_edge_lamports=expected_edge_lamports,
                edge_ceiling_lamports=edge_ceiling,
                live_floor_lamports=live_floor,
                percentile_used=percentile,
                contention_bucket=contention_bucket,
                snapshot_slot=snap.as_of_slot,
            )

        # 5. We can land within budget.  Bid exactly the live floor — never a
        #    basis point over it, never up to the ceiling "to be safe".  The
        #    floor is the minimum competitive tip; paying more is pure bleed.
        tip = live_floor
        decision = TipDecision(
            kind=TipDecisionKind.SUBMIT,
            tip_lamports=tip,
            expected_edge_lamports=expected_edge_lamports,
            edge_ceiling_lamports=edge_ceiling,
            live_floor_lamports=live_floor,
            percentile_used=percentile,
            contention_bucket=contention_bucket,
            snapshot_slot=snap.as_of_slot,
            reason="priced_at_live_floor_within_edge_ceiling",
            tip_pct_of_edge_bps=tip_pct_of_edge_bps,
        )
        logger.info(
            "tip_controller.submit",
            extra={
                "decision_slot": decision_slot,
                "snapshot_slot": snap.as_of_slot,
                "tip_lamports": tip,
                "edge_ceiling_lamports": edge_ceiling,
                "expected_edge_lamports": expected_edge_lamports,
                "percentile_used": percentile,
                "contention_bucket": contention_bucket,
                # the bleed metric — tip as % of edge, in bps (cardinal-sin guard)
                "tip_pct_of_edge_bps": tip_pct_of_edge_bps,
            },
        )
        return decision

    # ------------------------------------------------------------------ #
    # tip-as-%-of-realized-PnL — logged AFTER the fill (post-trade audit).
    # ------------------------------------------------------------------ #

    @staticmethod
    def log_tip_pct_of_pnl(
        *,
        client_intent_id: str,
        tip_lamports: int,
        realized_pnl_lamports: int,
    ) -> int:
        """Log tip as % of REALIZED PnL (post-fill) and return it in bps.

        This is the honest after-the-fact bleed metric the standards demand
        ("log tip-as-%-of-realized-PnL every trade").  When realized PnL <= 0
        the tip was pure loss; we emit a sentinel (-1 bps) and log it loudly so
        the bleed is never hidden behind a divide-by-zero.

        Returns the integer bps value emitted (or -1 sentinel).
        """
        if isinstance(tip_lamports, float) or isinstance(realized_pnl_lamports, float):
            raise TypeError("tip / pnl must be int lamports, not float (exact-money rule).")
        if realized_pnl_lamports <= 0:
            logger.warning(
                "tip_controller.tip_pct_of_pnl.non_positive_pnl",
                extra={
                    "client_intent_id": client_intent_id,
                    "tip_lamports": tip_lamports,
                    "realized_pnl_lamports": realized_pnl_lamports,
                    "tip_pct_of_pnl_bps": -1,
                    "note": "tip was pure cost — realized PnL non-positive",
                },
            )
            return -1
        pct_bps = (tip_lamports * 10_000) // realized_pnl_lamports
        logger.info(
            "tip_controller.tip_pct_of_pnl",
            extra={
                "client_intent_id": client_intent_id,
                "tip_lamports": tip_lamports,
                "realized_pnl_lamports": realized_pnl_lamports,
                "tip_pct_of_pnl_bps": pct_bps,
            },
        )
        return pct_bps

    # ------------------------------------------------------------------ #
    # internal
    # ------------------------------------------------------------------ #

    def _refuse(
        self,
        *,
        reason: str,
        expected_edge_lamports: int,
        edge_ceiling_lamports: int,
        live_floor_lamports: int,
        percentile_used: int,
        contention_bucket: str,
        snapshot_slot: int,
    ) -> TipDecision:
        return TipDecision(
            kind=TipDecisionKind.DO_NOT_SUBMIT,
            tip_lamports=0,
            expected_edge_lamports=expected_edge_lamports,
            edge_ceiling_lamports=edge_ceiling_lamports,
            live_floor_lamports=live_floor_lamports,
            percentile_used=percentile_used,
            contention_bucket=contention_bucket,
            snapshot_slot=snapshot_slot,
            reason=reason,
            tip_pct_of_edge_bps=_pct_of_edge_bps(
                live_floor_lamports, expected_edge_lamports
            ),
        )


def _pct_of_edge_bps(tip_lamports: int, expected_edge_lamports: int) -> int:
    """tip / edge in integer bps; 0 when edge is non-positive (no float)."""
    if expected_edge_lamports <= 0:
        return 0
    return (tip_lamports * 10_000) // expected_edge_lamports
