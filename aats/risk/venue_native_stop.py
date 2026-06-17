"""T-321 — Layer 1: venue-native resting protection (the off-box keeper).

ADR-0008 Layer 1: a keeper/limit-style resting exit placed ON THE VENUE so the
exit lives OFF-BOX.  It survives a busy OR fully-dead bot process for the
on-chain price breach — the whole point of a survivable stop is that it does NOT
depend on this process being alive.

SEAM OWNERSHIP (AATS-ROSTER §4):
  - THIS FILE (`risk-guardrails-engineer`):  DEFINES the placement REQUEST and
    the seam (`RestingStopVenue`), derives the trigger from the fixed StopState,
    and INVOKES placement.  We do NOT author the transaction-building / RPC code.
  - `solana-execution-engineer`:  IMPLEMENTS `RestingStopVenue` (Jupiter
    limit/trigger order, or a venue keeper) behind the ExecutionVenue seam, with
    sign() crossing to aats-signer (ADR-0009) and DRY_RUN refusing to transmit.

NON-WAIVABLE:
  * The resting order is a SELL trigger at the FIXED stop price (de-risk only).
    There is no place-order method that adds exposure or moves a stop down.
  * Tightening cancels+replaces with a HIGHER trigger only (one-directional);
    `replace_tighter()` refuses a lower trigger.
  * Money is integer base units / Decimal price.  NEVER float.
  * DRY-RUN: a placement in DRY_RUN/SIMULATION mode builds-but-does-not-transmit
    (the execution seam enforces this); the *reference* sim placement below
    records the resting order in memory and fires it on a breach tick WITHOUT a
    live process loop — that is how the test proves "process dead, stop still
    exits".
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol, runtime_checkable

from aats.contracts.venue import FillResult, SubmitMode
from aats.risk.survivable_stop import StopState


@dataclass(frozen=True)
class RestingStopOrder:
    """A venue-native SELL trigger resting off-box at the FIXED stop price.

    `trigger_price` is Decimal SOL-lamports per token base unit.  `fraction_bps`
    is the share of the position the resting order sells on trigger (10000 = all,
    the hard-stop default).  De-risk only: there is no risk-increasing field.
    """

    mint: str
    trigger_price: Decimal
    fraction_bps: int = 10_000
    order_id: str | None = None  # set by the venue once placed (off-box handle)

    def __post_init__(self) -> None:
        if isinstance(self.trigger_price, float):
            raise TypeError(
                f"RestingStopOrder.trigger_price must be Decimal, got float "
                f"{self.trigger_price!r} (data-models.md §0)."
            )
        if isinstance(self.fraction_bps, float):
            raise TypeError("RestingStopOrder.fraction_bps must be int (bps), not float.")
        if not (1 <= self.fraction_bps <= 10_000):
            raise ValueError("RestingStopOrder.fraction_bps must be in [1, 10000].")
        if self.trigger_price <= 0:
            raise ValueError("RestingStopOrder.trigger_price must be > 0.")


@runtime_checkable
class RestingStopVenue(Protocol):
    """The venue seam for OFF-BOX resting stop orders.

    Implemented by `solana-execution-engineer` (Jupiter limit/trigger order or a
    venue keeper) behind the ExecutionVenue ABC.  All three methods are de-risk:
    place a SELL trigger, cancel it, or replace it with a TIGHTER trigger.  There
    is deliberately no method that widens a stop or adds exposure.
    """

    @property
    def submit_mode(self) -> SubmitMode: ...

    def place_resting_stop(self, order: RestingStopOrder) -> RestingStopOrder:
        """Place an off-box SELL trigger; returns the order with its venue order_id."""
        ...

    def cancel_resting_stop(self, mint: str, order_id: str) -> None: ...

    def replace_tighter(self, mint: str, order_id: str, new_trigger: Decimal) -> RestingStopOrder:
        """Cancel + replace with a HIGHER trigger only (tighten).  Refuses a lower one."""
        ...


def resting_order_from_stop(stop: StopState, *, fraction_bps: int = 10_000) -> RestingStopOrder:
    """Build the venue-native resting-order request from the FIXED StopState.

    The resting trigger IS the StopState trigger — the off-box order and the
    in-process enforcer enforce the SAME fixed price, so the two layers agree and
    Layer 1 holds the exact same line when Layer 2 (the process) is dead.
    """
    return RestingStopOrder(
        mint=stop.mint,
        trigger_price=stop.trigger_price,
        fraction_bps=fraction_bps,
    )


class VenueNativeStopPlacer:
    """Places / tightens / cancels the off-box resting stop via the venue seam.

    This is the RISK-side invoker.  It holds the seam, derives orders from the
    FIXED StopState, and enforces tighten-only at this layer too (defense in
    depth: even if a caller hands a lower trigger, `replace_tighter` on both this
    class and the venue refuses it).
    """

    def __init__(self, venue: RestingStopVenue) -> None:
        self._venue = venue
        self._placed: dict[str, RestingStopOrder] = {}

    @property
    def submit_mode(self) -> SubmitMode:
        return self._venue.submit_mode

    def place_for(self, stop: StopState, *, fraction_bps: int = 10_000) -> RestingStopOrder:
        """Place the off-box resting stop for a position at the fixed trigger."""
        req = resting_order_from_stop(stop, fraction_bps=fraction_bps)
        placed = self._venue.place_resting_stop(req)
        self._placed[stop.mint] = placed
        return placed

    def tighten(self, mint: str, new_trigger: Decimal | int | str) -> RestingStopOrder:
        """Tighten the off-box order (raise its trigger).  Refuses a widen."""
        existing = self._placed.get(mint)
        if existing is None or existing.order_id is None:
            raise KeyError(f"no placed resting stop for mint {mint!r}")
        proposed = new_trigger if isinstance(new_trigger, Decimal) else Decimal(new_trigger)
        if proposed < existing.trigger_price:
            raise ValueError(
                f"replace refused: new trigger {proposed} is lower than existing "
                f"{existing.trigger_price} — a venue-native stop may only be TIGHTENED."
            )
        replaced = self._venue.replace_tighter(mint, existing.order_id, proposed)
        self._placed[mint] = replaced
        return replaced

    def cancel(self, mint: str) -> None:
        existing = self._placed.pop(mint, None)
        if existing is not None and existing.order_id is not None:
            self._venue.cancel_resting_stop(mint, existing.order_id)

    def placed_order(self, mint: str) -> RestingStopOrder | None:
        return self._placed.get(mint)


# ---------------------------------------------------------------------------
# Reference SIMULATION resting-stop venue — proves the OFF-BOX property.
#
# This is a TEST/sim stand-in for `solana-execution-engineer`'s real Jupiter
# limit/trigger implementation.  Crucially it holds the resting order in its OWN
# state (the "off-box keeper") and exposes a `keeper_tick()` that fires the
# resting order on a price breach WITHOUT the bot's FAST loop running — i.e. with
# the in-process enforcer fully dead.  That is exactly the survivability the test
# must demonstrate.  submit_mode = SIMULATION: it never touches a network.
# ---------------------------------------------------------------------------
@dataclass
class _RestingRecord:
    order: RestingStopOrder
    fired: bool = False


class SimulationRestingStopVenue:
    """In-memory off-box keeper for tests (SIMULATION mode, no network).

    Models a venue/keeper that holds the SELL trigger off-box and fires it when
    the on-chain price falls to/through the trigger — independent of our process.
    `keeper_tick(mint, mark_price)` is the off-box keeper observing the chain; it
    is NOT our FAST loop and runs even when the bot is "dead".
    """

    def __init__(self) -> None:
        self._orders: dict[str, _RestingRecord] = {}
        self.fired_exits: list[FillResult] = []
        self._seq = 0

    @property
    def submit_mode(self) -> SubmitMode:
        return SubmitMode.SIMULATION

    def place_resting_stop(self, order: RestingStopOrder) -> RestingStopOrder:
        self._seq += 1
        placed = RestingStopOrder(
            mint=order.mint,
            trigger_price=order.trigger_price,
            fraction_bps=order.fraction_bps,
            order_id=f"sim-resting-{self._seq}",
        )
        self._orders[order.mint] = _RestingRecord(order=placed)
        return placed

    def cancel_resting_stop(self, mint: str, order_id: str) -> None:
        rec = self._orders.get(mint)
        if rec is not None and rec.order.order_id == order_id:
            self._orders.pop(mint, None)

    def replace_tighter(self, mint: str, order_id: str, new_trigger: Decimal) -> RestingStopOrder:
        rec = self._orders.get(mint)
        if rec is None or rec.order.order_id != order_id:
            raise KeyError(f"no resting order {order_id!r} for mint {mint!r}")
        if new_trigger < rec.order.trigger_price:
            raise ValueError(
                f"venue refused widen: new trigger {new_trigger} < existing "
                f"{rec.order.trigger_price} (tighten-only)."
            )
        self._seq += 1
        replaced = RestingStopOrder(
            mint=mint,
            trigger_price=new_trigger,
            fraction_bps=rec.order.fraction_bps,
            order_id=f"sim-resting-{self._seq}",
        )
        self._orders[mint] = _RestingRecord(order=replaced)
        return replaced

    def keeper_tick(self, mint: str, mark_price: Decimal | int | str) -> FillResult | None:
        """OFF-BOX keeper: fire the resting SELL if the chain price breaches it.

        Runs WITHOUT our process loop.  This is the layer that protects a dead
        bot — the test drives this after 'killing' the in-process enforcer.
        """
        rec = self._orders.get(mint)
        if rec is None or rec.fired:
            return None
        mark = mark_price if isinstance(mark_price, Decimal) else Decimal(mark_price)
        if mark <= rec.order.trigger_price:
            rec.fired = True
            fill = FillResult(
                landed=True,
                reason="venue_native_resting_stop_filled",
                land_slot=None,
                slot_delay=0,
                buyers_ahead=0,
                tokens_out=0,
                effective_price_decimal=str(rec.order.trigger_price),
                entry_slippage_bps=0,
                tip_lamports=0,
                priority_lamports=0,
            )
            self.fired_exits.append(fill)
            self._orders.pop(mint, None)
            return fill
        return None
