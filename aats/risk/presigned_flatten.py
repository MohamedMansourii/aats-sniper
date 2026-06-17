"""T-322 — pre-signed flatten holding for the aats-dms service.

infrastructure.md §8: the DMS holds PRE-SIGNED flatten transactions for all open
positions (refreshed as positions open/close, produced via `aats-signer`,
ADR-0009).  Because the flattens are already signed and HELD by the DMS, the DMS
fires even if `aats-signer` is ALSO down at fire time — it submits the held signed
bytes straight to the block engine.

SEAM OWNERSHIP (AATS-ROSTER §4):
  * THIS FILE (`risk-guardrails-engineer`): DEFINES the holding + the submit seam,
    and the de-risk-only contract (a held tx is a SELL/flatten, never a buy).  We
    do NOT author the transaction-building / signing / RPC submit code.
  * `solana-execution-engineer`: produces the signed flatten bytes via aats-signer
    and IMPLEMENTS `BlockEngineSubmitter` (the real submit to the Jito block
    engine), behind the DRY_RUN gate.

NON-WAIVABLE:
  * DRY-RUN: in DRY_RUN/SIMULATION the submit is a no-op transmit (the held bytes
    exist for latency/fidelity but never reach the network).  Real capital is
    DISABLED by default.
  * De-risk only: a `PreSignedFlatten` is a SELL/flatten of one open position.
    There is no field or method that could make it add exposure.
  * Money is integer base units.  NEVER float.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from aats.contracts.venue import SubmitMode


@dataclass(frozen=True)
class PreSignedFlatten:
    """A pre-signed flatten (full SELL) for ONE open position, held by the DMS.

    `signed_tx_b64` is the base64 of the ALREADY-SIGNED flatten transaction,
    produced upstream via aats-signer (the DMS never holds the raw key — ADR-0009).
    `fraction_bps` is fixed at 10_000 (full flatten); de-risk only.  `refreshed_at_slot`
    lets the holder discard a flatten that is stale relative to the position's
    current size after a partial fill (the holder is refreshed as positions change).
    """

    mint: str
    signed_tx_b64: str
    fraction_bps: int = 10_000  # full flatten only — de-risk
    refreshed_at_slot: int = 0

    def __post_init__(self) -> None:
        if isinstance(self.fraction_bps, float):
            raise TypeError("PreSignedFlatten.fraction_bps must be int (bps), not float.")
        if isinstance(self.refreshed_at_slot, float):
            raise TypeError("PreSignedFlatten.refreshed_at_slot must be int (slot), not float.")
        if self.fraction_bps != 10_000:
            # A pre-signed flatten is always a FULL exit (de-risk).  A partial
            # would leave residual risk the DMS cannot subsequently manage from a
            # dead-loop world — so we forbid it structurally.
            raise ValueError(
                "PreSignedFlatten.fraction_bps must be 10_000 (full flatten only). "
                "The DMS flatten is de-risk-only and cannot leave residual exposure."
            )
        if not self.signed_tx_b64:
            raise ValueError("PreSignedFlatten.signed_tx_b64 must be non-empty (a real signed tx).")


@runtime_checkable
class BlockEngineSubmitter(Protocol):
    """Submit ALREADY-SIGNED bytes to the block engine (solana-execution owns impl).

    `submit_mode` is a first-class state (DRY_RUN is not a buried flag, FR-039).
    In DRY_RUN/SIMULATION, `submit_signed()` performs everything up to transmit
    then REFUSES to send (returns submitted=False).  The DMS holds the signed
    bytes itself, so this submit fires even if aats-signer is unreachable.
    """

    @property
    def submit_mode(self) -> SubmitMode: ...

    def submit_signed(self, signed_tx_b64: str, reason: str) -> bool:
        """Return True iff the bytes were transmitted to the block engine."""
        ...


class PreSignedFlattenHolder:
    """Holds the current pre-signed flattens for all open positions (DMS-side).

    Refreshed by the FAST-loop side as positions open/close/partial-fill (upsert /
    remove).  On a DMS fire, `flatten_all()` submits EVERY held pre-signed tx to
    the block engine — even with the loop and the signer both dead, because the
    bytes are already signed and held here.

    This adapts the held flattens to the `EmergencyFlattenVenue` seam
    (`emergency_flatten(reason)`) the watchdog invokes, so the watchdog stays
    agnostic of how the flatten is realized.
    """

    def __init__(self, submitter: BlockEngineSubmitter) -> None:
        self._submitter = submitter
        self._lock = threading.RLock()
        self._held: dict[str, PreSignedFlatten] = {}

    @property
    def submit_mode(self) -> SubmitMode:
        return self._submitter.submit_mode

    def upsert(self, flatten: PreSignedFlatten) -> None:
        """Refresh the held pre-signed flatten for a mint (open / size change)."""
        with self._lock:
            self._held[flatten.mint] = flatten

    def remove(self, mint: str) -> None:
        """Drop the held flatten for a closed position (idempotent)."""
        with self._lock:
            self._held.pop(mint, None)

    def held_mints(self) -> frozenset[str]:
        with self._lock:
            return frozenset(self._held.keys())

    # -- the EmergencyFlattenVenue seam the watchdog invokes ----------------

    def emergency_flatten(self, reason: str) -> None:
        """Submit EVERY held pre-signed flatten (full book).  Idempotent.

        FAIL-CLOSED: if a single submit raises, we still attempt the rest before
        propagating the first error, so one un-landable position cannot suppress
        flattening the others.  In DRY_RUN the submitter refuses to transmit
        (real capital disabled by default).
        """
        with self._lock:
            held = list(self._held.values())
        first_error: Exception | None = None
        for flatten in held:
            try:
                self._submitter.submit_signed(
                    flatten.signed_tx_b64,
                    reason=f"{reason}|mint={flatten.mint}",
                )
            except Exception as exc:  # noqa: BLE001 — try ALL positions, fail-closed
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise first_error
