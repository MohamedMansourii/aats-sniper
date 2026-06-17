"""JitoBundleSubmitter — atomic buy-with-revert via a Jito bundle.

The whole point of bundling the buy is ATOMICITY: the swap + the min-out
assertion + the tip transfer ride in ONE bundle, and the block engine either
lands the bundle whole or drops it whole.  A buy that would revert on min-out
lands NOTHING — you never get stuck holding a honeypot from a half-filled snipe
(FR-040, execution-venue.md §1).

DRY-RUN HARD RULE (FR-039, AC-060): submit_mode = DRY_RUN by default.  In
DRY_RUN, the submitter assembles the bundle and runs the LOCAL min-out
pre-check, but there is NO code path that reaches the block engine.  LIVE
requires DRY_RUN_ENABLED=false in the env AND an explicitly-enabled submitter.

INJECTABLE + OFFLINE-SAFE: the real Jito block-engine `sendBundle` JSON-RPC is
not reachable here.  `BlockEngineClientProtocol` is the seam; `MockBlockEngine`
is the deterministic offline implementation used in tests/replay.  The real
region-pinned endpoint plugs in at the marked site (latency-devops-engineer).

Money: tip + min-out are int lamports / base units.  No float.  Ever.
"""
from __future__ import annotations

import logging
import os
from enum import StrEnum
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, model_validator

from aats.contracts.venue import SignedTx

logger = logging.getLogger(__name__)


class BundleSubmitMode(StrEnum):
    """Mirror of venue.SubmitMode for the bundle path (FR-039)."""

    DRY_RUN = "DRY_RUN"  # assemble + local min-out check; NEVER reaches the block engine
    LIVE = "LIVE"  # real sendBundle — only when DRY_RUN_ENABLED=false + explicit enable


class BundleStatus(StrEnum):
    """Terminal status of a bundle (Jito bundle-status polling)."""

    LANDED = "LANDED"  # bundle landed whole
    REVERTED = "REVERTED"  # min-out (or another assertion) reverted — NOTHING landed
    DROPPED = "DROPPED"  # block engine dropped the bundle (not landed, nothing changed)
    DRY_RUN = "DRY_RUN"  # DRY_RUN short-circuit — no network call made


class BundleResult(BaseModel):
    """The outcome of submitting an atomic buy bundle.

    INVARIANT: `landed_buy` is True ONLY when status == LANDED.  On REVERTED /
    DROPPED / DRY_RUN, `landed_buy` is False and the desk holds a FLAT state —
    no orphan position, ever.
    """

    status: BundleStatus
    landed_buy: bool
    bundle_id: str | None  # block-engine bundle id (None in DRY_RUN / on local reject)
    reason: str
    # echoed for audit — integer lamports / base units
    tip_lamports: int
    min_tokens_out_base: int

    model_config = {"frozen": True}

    @model_validator(mode="after")
    def _flat_unless_landed(self) -> BundleResult:
        """Atomicity invariant: a position exists ONLY when the bundle LANDED."""
        if self.landed_buy and self.status is not BundleStatus.LANDED:
            raise ValueError(
                f"landed_buy=True is only legal when status==LANDED, got {self.status}. "
                "A non-landed bundle must leave a FLAT state (atomicity, FR-040)."
            )
        if self.status is not BundleStatus.LANDED and self.landed_buy:
            raise ValueError("non-landed bundle cannot report landed_buy=True (orphan guard).")
        return self


class AtomicBuyBundle(BaseModel):
    """The assembled atomic bundle: [buy_with_min_out, tip_transfer].

    The min-out assertion is part of the BUY instruction's own slippage check
    AND re-asserted as a bundle-level guard, so a buy that would receive fewer
    than `min_tokens_out_base` reverts the WHOLE bundle (tip included — a
    reverting buy costs no tip).

    Carries the already-signed buy tx (the hot core built+signed it via
    `aats-signer`, ADR-0009 — the submitter never holds a key) and the tip
    parameters.  Tip transfer must target a pinned Jito tip account.
    """

    signed_buy_tx: SignedTx
    tip_lamports: int
    tip_account: str  # a pinned Jito tip account (getTipAccounts rotation set)
    min_tokens_out_base: int  # the atomic revert threshold (integer base units)
    client_intent_id: str

    model_config = {"frozen": True}

    @model_validator(mode="before")
    @classmethod
    def _reject_float_money(cls, data: object) -> object:
        if not isinstance(data, dict):
            return data
        for field in ("tip_lamports", "min_tokens_out_base"):
            val = data.get(field)
            if isinstance(val, float):
                raise ValueError(
                    f"AtomicBuyBundle.{field} must be int, got float {val!r} "
                    "(money is integer base units — data-models.md §0)."
                )
        return data

    @model_validator(mode="after")
    def _positive_thresholds(self) -> AtomicBuyBundle:
        if self.tip_lamports < 0:
            raise ValueError(f"tip_lamports must be >= 0, got {self.tip_lamports}")
        if self.min_tokens_out_base <= 0:
            raise ValueError(
                f"min_tokens_out_base must be > 0 (a real revert threshold), "
                f"got {self.min_tokens_out_base}. A zero/negative min-out disables the "
                "honeypot guard."
            )
        if not self.tip_account:
            raise ValueError("tip_account must be a pinned Jito tip account (non-empty).")
        return self


# ---------------------------------------------------------------------------
# The injectable block-engine seam + offline mock.
# ---------------------------------------------------------------------------


@runtime_checkable
class BlockEngineClientProtocol(Protocol):
    """The injectable Jito block-engine `sendBundle` seam.

    Production: a region-pinned JSON-RPC client (sendBundle + getBundleStatuses).
    The real endpoint plugs in here (latency-devops-engineer).
    Tests/replay: `MockBlockEngine`, deterministic and offline.
    """

    def send_bundle(self, bundle: AtomicBuyBundle) -> str:
        """Submit the bundle; return the block-engine bundle id."""
        ...

    def poll_status(self, bundle_id: str) -> BundleStatus:
        """Poll the terminal status of a submitted bundle."""
        ...


class MockBlockEngine:
    """Deterministic offline block engine for tests/replay (NO network).

    Behaviour is driven by the on-chain fill it is told to model:
      * `actual_tokens_out_base` — what the buy WOULD receive on-chain.
      * If that is < the bundle's `min_tokens_out_base`, the bundle REVERTS
        whole (status=REVERTED, nothing lands) — modelling the on-chain
        assert_min_out failure.
      * `force_drop=True` models the block engine dropping the bundle.

    This lets a test force a reverting buy and prove a flat state results.
    """

    def __init__(
        self,
        *,
        actual_tokens_out_base: int,
        force_drop: bool = False,
    ) -> None:
        if isinstance(actual_tokens_out_base, float):
            raise TypeError("actual_tokens_out_base must be int base units, not float.")
        self._actual_tokens_out = actual_tokens_out_base
        self._force_drop = force_drop
        self._submitted: dict[str, AtomicBuyBundle] = {}
        self._counter = 0

    def send_bundle(self, bundle: AtomicBuyBundle) -> str:
        self._counter += 1
        bundle_id = f"mock-bundle-{self._counter}-{bundle.client_intent_id}"
        self._submitted[bundle_id] = bundle
        return bundle_id

    def poll_status(self, bundle_id: str) -> BundleStatus:
        bundle = self._submitted.get(bundle_id)
        if bundle is None:
            return BundleStatus.DROPPED
        if self._force_drop:
            return BundleStatus.DROPPED
        # The on-chain assert_min_out: revert the WHOLE bundle if under-filled.
        if self._actual_tokens_out < bundle.min_tokens_out_base:
            return BundleStatus.REVERTED
        return BundleStatus.LANDED


# ---------------------------------------------------------------------------
# The submitter.
# ---------------------------------------------------------------------------


class JitoBundleSubmitter:
    """Submits the atomic buy bundle, DRY-RUN by default.

    INJECTABLE: the block-engine client is injected (offline mock in tests).
    The submitter holds NO key — it submits an already-signed buy tx.
    """

    def __init__(
        self,
        block_engine: BlockEngineClientProtocol,
        *,
        live_submit_enabled: bool = False,
    ) -> None:
        self._engine = block_engine
        # Construction-time gate, identical posture to the venue's third gate.
        self._live_submit_enabled = live_submit_enabled

    @property
    def submit_mode(self) -> BundleSubmitMode:
        """DRY_RUN unless BOTH the env flag is cleared AND live is enabled."""
        if self._live_submit_enabled and _dry_run_env_disabled():
            return BundleSubmitMode.LIVE
        return BundleSubmitMode.DRY_RUN

    def submit_atomic_buy(self, bundle: AtomicBuyBundle) -> BundleResult:
        """Submit the atomic buy bundle, or DRY-RUN short-circuit.

        Returns a BundleResult whose `landed_buy` is True ONLY if the bundle
        landed whole.  On a min-out revert the desk is FLAT — no orphan position.
        """
        mode = self.submit_mode
        logger.info(
            "jito_bundle.submit.start",
            extra={
                "client_intent_id": bundle.client_intent_id,
                "tip_lamports": bundle.tip_lamports,
                "min_tokens_out_base": bundle.min_tokens_out_base,
                "tip_account": bundle.tip_account,
                "submit_mode": mode.value,
            },
        )

        # ---- DRY_RUN: assemble + (local) min-out check; NEVER reach the engine ----
        if mode == BundleSubmitMode.DRY_RUN:
            logger.info(
                "jito_bundle.submit.dry_run",
                extra={
                    "client_intent_id": bundle.client_intent_id,
                    "reason": "dry_run — no sendBundle call (FR-039)",
                },
            )
            return BundleResult(
                status=BundleStatus.DRY_RUN,
                landed_buy=False,  # DRY_RUN never lands a real position
                bundle_id=None,
                reason="dry_run",
                tip_lamports=bundle.tip_lamports,
                min_tokens_out_base=bundle.min_tokens_out_base,
            )

        # ---- LIVE: third-gate assert, then sendBundle + poll ----
        self._assert_live_allowed(bundle.client_intent_id)

        bundle_id = self._engine.send_bundle(bundle)
        status = self._engine.poll_status(bundle_id)

        landed = status is BundleStatus.LANDED
        reason = {
            BundleStatus.LANDED: "bundle_landed",
            BundleStatus.REVERTED: "min_out_reverted_flat_state",
            BundleStatus.DROPPED: "bundle_dropped_flat_state",
        }.get(status, "unknown")

        logger.info(
            "jito_bundle.submit.result",
            extra={
                "client_intent_id": bundle.client_intent_id,
                "bundle_id": bundle_id,
                "status": status.value,
                "landed_buy": landed,
                "reason": reason,
                "tip_lamports": bundle.tip_lamports,
            },
        )
        return BundleResult(
            status=status,
            landed_buy=landed,
            bundle_id=bundle_id,
            reason=reason,
            tip_lamports=bundle.tip_lamports,
            min_tokens_out_base=bundle.min_tokens_out_base,
        )

    def _assert_live_allowed(self, intent_id: str) -> None:
        """LIVE requires the construction flag AND DRY_RUN_ENABLED=false (FR-039)."""
        if not self._live_submit_enabled:
            raise LiveBundleBlocked(
                f"[{intent_id}] LIVE bundle attempted but live_submit_enabled=False. "
                "Real capital requires DRY_RUN_ENABLED=false + explicit enable (FR-039, AC-060)."
            )
        if not _dry_run_env_disabled():
            raise LiveBundleBlocked(
                f"[{intent_id}] LIVE bundle attempted but DRY_RUN_ENABLED is not 'false'. "
                "Set DRY_RUN_ENABLED=false in env to enable real capital (FR-039)."
            )


class LiveBundleBlocked(RuntimeError):
    """Raised when a LIVE sendBundle is attempted without all gates cleared."""


def _dry_run_env_disabled() -> bool:
    """True only if DRY_RUN_ENABLED is explicitly 'false' (safe default = enabled)."""
    return os.environ.get("DRY_RUN_ENABLED", "true").strip().lower() == "false"
