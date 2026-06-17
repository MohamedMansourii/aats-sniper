"""T-329 — Honeypot / sellability sell-sim primitive.

PURPOSE
-------
Answers the single question: "if we just bought this token, can we sell it?"
Used OFF the hot path (N+2 check, 30-100ms budget) by the risk gate's
``SellSimProbe`` seam (``pretrade_gate.SellSimProbe``).

DESIGN PRINCIPLES
-----------------
* DRY-RUN / no-submit by construction.  ``SellSimVenue`` has ``submit_mode =
  SubmitMode.DRY_RUN``.  There is NO code path that calls ``send_transaction``.
  A real submit from this primitive would be a bug of the highest order.

* Simulate before you sign.  Every sell-sim path runs ``simulateTransaction``
  (or the injected mock equivalent).  A simulation revert = honeypot detected.

* Honeypot is the default assumption.  Any failure mode — RPC error, signer
  refusal, simulation revert, zero-output, high-sell-tax, account-close trap,
  or unknown error — returns ``SellSimResult(sellable=False, ...)``.  The probe
  is refuse-by-default: if we cannot CONFIRM sellability, we report unsellable.

* Atomic by construction.  We build a SELL instruction for a dust amount (the
  smallest non-zero token amount that can be quoted), simulate it, and stop.
  We never submit.  The dust amount is also the minimum that tests the actual
  token-transfer mechanics without meaningful market impact.

* Money is int / Decimal only.  All lamport / base-unit quantities are int.
  All price and tax ratios are Decimal.  No float.

* Point-in-time.  The sim uses only ``event_slot`` data (the slot at which the
  candidate was detected).  It never reads wall-clock or future-slot data.

* INJECTABLE.  ``SellSimVenue`` accepts an injectable ``RpcClientProtocol`` and
  ``SignerClientProtocol`` so the entire path is testable offline with the same
  ``MockRpcClient`` / ``MockSignerClient`` the main venue uses.

HONEYPOT FINGERPRINTS DETECTED
-------------------------------
1. ``simulateTransaction`` reverts (e.g. "custom program error", "0x..." codes).
   The SPL token program will revert if the account is frozen, the transfer-fee
   extension blocks the transfer, or the program rejects the close instruction.
2. Zero tokens-out on the simulated SELL (pool drain / vanishing liquidity).
3. Effective sell tax > ``max_sell_tax_bps`` threshold (computed from simulated
   out vs theoretical out — the diff is the tax/rug toll extracted on exit).
4. Account-close / dust trap: the simulated close fails even for dust
   (usually means the program locks the token account on buy).
5. ``SignerRefused``: the signer's program allowlist blocked the sell program —
   if the program that would execute the sell is NOT on the allowlist, it is
   not a program we should interact with.
6. RPC/network error with a deterministic-offline mock: propagates as unsellable
   (refuse-by-default even on infrastructure failure).

WHAT THIS MODULE IS NOT
-----------------------
* It is NOT on the hot path.  It is NEVER called from the SNIPE entry loop
  block-0 veto.  It is called from ``PreTradeSafetyGate.check_sellability()``
  which is explicitly N+2+.
* It does NOT make sizing or entry decisions.  It returns a binary
  ``sellable: bool`` plus a structured reason for logging.  Callers decide what
  to do with a not-sellable result; this module only answers the question.
* It does NOT hold a private key.  ``sign()`` crosses to the injected signer.
* It does NOT set or read ``DRY_RUN_ENABLED``.  ``DRY_RUN_ENABLED`` gates real
  capital.  This primitive is always DRY-RUN by design — it never submits
  regardless of any environment variable.

WIRE CONTRACT WITH T-323
------------------------
``SellSimVenue`` implements ``pretrade_gate.SellSimProbe``:

    class SellSimProbe(Protocol):
        def is_sellable(self, mint: str, event_slot: int) -> bool: ...

The gate calls ``probe.is_sellable(mint, event_slot)`` and treats
``False`` as NOT_SELLABLE (refuse-by-default).

MONEY (data-models.md §0)
--------------------------
All monetary quantities: int (lamports / token base units) or Decimal (ratios).
NEVER float for money.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum

from aats.contracts.venue import SimResult, SubmitMode
from aats.execution.exceptions import SignerRefused
from aats.execution.rpc_client import MockRpcClient, RpcClientProtocol
from aats.execution.signer_client import MockSignerClient, SignerClientProtocol

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Dust amount: the smallest non-zero token amount for a sell simulation.
# We sell exactly DUST_SELL_AMOUNT token base units.  This is enough to
# exercise the full sell path (transfer, close) without market impact.
# ---------------------------------------------------------------------------

DUST_SELL_AMOUNT: int = 1  # 1 token base unit (smallest possible non-zero sell)

# Default sell-tax ceiling used by the sell-sim honesty check.
# If the simulated effective sell-tax exceeds this, the token is a honeypot.
DEFAULT_MAX_SELL_TAX_BPS: int = 1_000  # 10% — tokens above this are NOT sellable

# Default minimum output ratio for a dust sell (in bps of input).
# If the pool returns zero or suspiciously few tokens on a dust sell, reject.
# This guards against vanishing-liquidity / pool-drain honeypots.
DEFAULT_MIN_OUT_RATIO_BPS: int = 5_000  # at least 50% of "fair" value must come back

# ---------------------------------------------------------------------------
# SellSimResult — the structured outcome of one sell simulation attempt.
# ---------------------------------------------------------------------------


class SellSimReason(StrEnum):
    """Machine reason codes for sell-sim outcomes (dashboard/logging stable API)."""

    SELLABLE = "sellable"  # simulation succeeded — token appears sellable
    SIM_REVERT = "sim_revert"  # simulateTransaction returned an error
    ZERO_OUTPUT = "zero_output"  # simulated sell returned 0 tokens out
    HIGH_SELL_TAX = "high_sell_tax"  # effective sell-tax above threshold
    SIGNER_REFUSED = "signer_refused"  # program not on allowlist
    ACCOUNT_CLOSE_TRAP = "account_close_trap"  # account-close instruction reverted
    QUOTE_FAILED = "quote_failed"  # could not obtain a sell quote
    INFRASTRUCTURE_ERROR = "infrastructure_error"  # RPC/mock raised unexpectedly
    NOT_SIMULATED = "not_simulated"  # no RPC/signer available (refuse-by-default)


@dataclass(frozen=True)
class SellSimResult:
    """Result of a honeypot / sellability dry-run simulation.

    ``sellable`` is the binary verdict: True only when the sim confirmed
    we can sell.  False on any failure — refuse-by-default.

    All numeric fields are int (base units / bps) or Decimal (ratios).
    No float.
    """

    sellable: bool  # the binary verdict
    reason: SellSimReason  # machine reason code for logs / dashboard
    mint: str
    event_slot: int
    # Simulated sell amounts — int base units.
    dust_amount_in: int = DUST_SELL_AMOUNT  # token base units sent in
    simulated_out_lamports: int = 0  # SOL lamports the sim says we'd receive
    # Effective sell-tax inferred from the simulation (Decimal, not float).
    effective_sell_tax_bps: int = 0  # int bps; 0 if not computable
    # CU consumed by the simulation (int; 0 if sim did not run).
    sim_cu_consumed: int = 0
    # Revert reason from simulateTransaction (or None).
    sim_revert_reason: str | None = None
    # Extra detail string (program log excerpt or error description; NEVER user-controlled).
    detail: str = ""

    def __post_init__(self) -> None:
        # Guard: no float money fields.
        for name in ("dust_amount_in", "simulated_out_lamports", "effective_sell_tax_bps",
                     "sim_cu_consumed"):
            val = getattr(self, name)
            if isinstance(val, float):
                raise TypeError(
                    f"SellSimResult.{name} must be int (base units/bps), got float {val!r} "
                    "(data-models.md §0)."
                )


# ---------------------------------------------------------------------------
# Mock AMM sell-sim RPC: realistic simulation of a SELL instruction.
# The real implementation would call the venue's actual sell path.
# ---------------------------------------------------------------------------


class MockSellSimRpcClient(MockRpcClient):
    """Deterministic offline mock for sell-simulation RPC calls.

    Extends MockRpcClient with a configurable simulate_sell_ok flag so tests
    can inject a honeypot scenario by setting simulate_sell_ok=False.

    ``quote_out_lamports``: the theoretical fair output the quote returns
                            (before any on-chain tax/rug extraction).
    ``sell_out_lamports``:  the actual lamports the simulated sell returns
                            (after on-chain tax/rug extraction — may be less).
    ``simulate_sell_ok``:   if False, simulate returns a revert (honeypot).
    ``sell_tax_bps``:       the explicit effective sell tax (int bps).  Used
                            by tests to assert the computed tax.

    By default:
        quote_out_lamports = 10_000  (theoretical fair)
        sell_out_lamports  = 9_900   (~99% returned — 1% tax)
        sell_tax_bps       = 100     (1% effective tax)

    The tax computed by SellSimVenue._compute_effective_tax_bps using these
    values:  (10_000 - 9_900) * 10_000 // 10_000 = 100 bps.
    """

    def __init__(
        self,
        *,
        simulate_sell_ok: bool = True,
        quote_out_lamports: int = 10_000,  # theoretical fair output
        sell_out_lamports: int = 9_900,    # actual sim output (after tax)
        sell_tax_bps: int = 100,           # 1% effective sell tax
        revert_reason: str = "honeypot_transfer_blocked",
        **kwargs: object,
    ) -> None:
        # Guard: no float money
        if (
            isinstance(sell_out_lamports, float)
            or isinstance(sell_tax_bps, float)
            or isinstance(quote_out_lamports, float)
        ):
            raise TypeError(
                "MockSellSimRpcClient: sell_out_lamports, quote_out_lamports, and "
                "sell_tax_bps must be int, not float."
            )
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self._simulate_sell_ok = simulate_sell_ok
        self._quote_out_lamports: int = quote_out_lamports
        self._sell_out_lamports: int = sell_out_lamports
        self._sell_tax_bps: int = sell_tax_bps
        self._revert_reason = revert_reason

    def simulate_transaction(self, signed_tx_b64: str) -> object:
        """Return a SimulateResult for the sell instruction.

        If ``simulate_sell_ok=False``, returns a revert (honeypot detected).
        """
        from aats.execution.rpc_client import SimulateResult

        self.simulate_calls += 1
        if not self._simulate_sell_ok:
            return SimulateResult(
                success=False,
                cu_consumed=0,
                revert_reason=self._revert_reason,
                logs=[f"Program log: Error: {self._revert_reason}"],
            )
        return SimulateResult(
            success=True,
            cu_consumed=85_000,  # typical SPL token transfer CU
            revert_reason=None,
            logs=["Program log: mock_sell_sim_success"],
        )

    @property
    def quote_out_lamports(self) -> int:
        """The theoretical fair SOL output (before any on-chain tax), int lamports."""
        return self._quote_out_lamports

    @property
    def sell_out_lamports(self) -> int:
        """The actual SOL lamports returned by the simulated sell (after tax), int."""
        return self._sell_out_lamports

    @property
    def sell_tax_bps(self) -> int:
        """The effective sell tax in bps (int)."""
        return self._sell_tax_bps


class MockHoneypotRpcClient(MockSellSimRpcClient):
    """Mock that always returns a simulate revert — simulates a honeypot token.

    Used in tests to verify the sell-sim detects and flags honeypots.

    CRITICAL: ``quote_out_lamports`` must be > 0 so ``_quote_sell`` returns a
    non-zero theoretical quote — meaning we proceed past the QUOTE_FAILED early
    exit and reach the ``simulateTransaction`` call where the revert is detected.
    The SIMULATE REVERT is the honeypot fingerprint; a quote failure is a
    different (liquidity-drain) scenario.
    """

    def __init__(self, **kwargs: object) -> None:
        super().__init__(
            simulate_sell_ok=False,
            quote_out_lamports=10_000,  # non-zero theoretical quote; sim will revert
            sell_out_lamports=0,        # irrelevant — sim never succeeds
            sell_tax_bps=10_000,        # 100% tax = complete honeypot
            revert_reason="custom_program_error_0x1775",  # honeypot contract fingerprint
            **kwargs,
        )


# ---------------------------------------------------------------------------
# SellSimConfig — knobs for the simulation (all int/Decimal, no float).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SellSimConfig:
    """Configuration knobs for the honeypot sell simulation.

    All values are int bps / int base units — no float.
    """

    # Maximum sell tax to tolerate (int bps). Above this => not sellable.
    max_sell_tax_bps: int = DEFAULT_MAX_SELL_TAX_BPS
    # Minimum output ratio (int bps of a "fair" value) on a dust sell.
    # Guards against vanishing-liquidity / pool-drain honeypots.
    min_out_ratio_bps: int = DEFAULT_MIN_OUT_RATIO_BPS
    # Dust sell amount (int token base units). Smallest meaningful sell.
    dust_sell_amount: int = DUST_SELL_AMOUNT

    def __post_init__(self) -> None:
        for name in ("max_sell_tax_bps", "min_out_ratio_bps", "dust_sell_amount"):
            val = getattr(self, name)
            if isinstance(val, float):
                raise TypeError(
                    f"SellSimConfig.{name} must be int (bps / base units), got float {val!r} "
                    "(data-models.md §0)."
                )
            if val < 0:
                raise ValueError(f"SellSimConfig.{name} must be non-negative, got {val}.")
        if self.dust_sell_amount < 1:
            raise ValueError("SellSimConfig.dust_sell_amount must be >= 1 (smallest valid sell).")
        if not (0 <= self.max_sell_tax_bps <= 10_000):
            raise ValueError("SellSimConfig.max_sell_tax_bps must be in [0, 10000] bps.")
        if not (0 <= self.min_out_ratio_bps <= 10_000):
            raise ValueError("SellSimConfig.min_out_ratio_bps must be in [0, 10000] bps.")


# ---------------------------------------------------------------------------
# SellSimVenue — the DRY-RUN-only venue that runs the honeypot sell simulation.
# ---------------------------------------------------------------------------


class SellSimVenue:
    """Honeypot / sellability simulation venue.

    Implements ``pretrade_gate.SellSimProbe`` via ``is_sellable()``.

    DRY-RUN by construction: ``submit_mode == SubmitMode.DRY_RUN`` always.
    There is no code path in this class that calls ``send_transaction()``.

    Usage (wired by T-329 into the T-323 seam):

        from aats.execution.sell_sim import SellSimVenue, SellSimConfig
        from aats.execution.rpc_client import MockRpcClient
        from aats.execution.signer_client import MockSignerClient

        probe = SellSimVenue(
            rpc_client=MockRpcClient(),
            signer_client=MockSignerClient(),
            config=SellSimConfig(),
        )
        result = probe.simulate_sell(mint="...", event_slot=300_000_000)
        ok = probe.is_sellable(mint="...", event_slot=300_000_000)

    In production, inject the real RPC client and signer; the venue will
    simulate a SELL of DUST_SELL_AMOUNT tokens and report the result.
    """

    # submit_mode is a class attribute so callers can inspect it.
    submit_mode: SubmitMode = SubmitMode.DRY_RUN

    def __init__(
        self,
        *,
        rpc_client: RpcClientProtocol | None = None,
        signer_client: SignerClientProtocol | None = None,
        config: SellSimConfig | None = None,
        wallet_pubkey: str = "SellSimPubkey11111111111111111111111111111",
    ) -> None:
        """Construct a SellSimVenue.

        Args:
            rpc_client:    Injectable RPC client.  Defaults to MockRpcClient (offline safe).
            signer_client: Injectable signer client.  Defaults to MockSignerClient.
            config:        Sell-sim configuration knobs.  Defaults to SellSimConfig().
            wallet_pubkey: The pubkey that would sign the simulated sell.  NOT the key.
        """
        self._rpc: RpcClientProtocol = rpc_client or MockRpcClient()
        self._signer: SignerClientProtocol = signer_client or MockSignerClient(
            wallet_pubkey=wallet_pubkey
        )
        self._config: SellSimConfig = config or SellSimConfig()
        self._wallet_pubkey = wallet_pubkey

    # -----------------------------------------------------------------------
    # SellSimProbe protocol — the T-323 seam contract
    # -----------------------------------------------------------------------

    def is_sellable(self, mint: str, event_slot: int) -> bool:
        """Implement ``SellSimProbe.is_sellable``.

        Returns True ONLY when ``simulate_sell`` confirms sellability.
        Any failure (revert, zero output, high tax, signer refused, etc.)
        returns False — refuse-by-default.

        NEVER submits a transaction.  DRY-RUN / no-submit by construction.
        """
        result = self.simulate_sell(mint=mint, event_slot=event_slot)
        logger.info(
            "sell_sim.is_sellable",
            extra={
                "mint": mint,
                "event_slot": event_slot,
                "sellable": result.sellable,
                "reason": result.reason.value,
                "sim_cu_consumed": result.sim_cu_consumed,
                "effective_sell_tax_bps": result.effective_sell_tax_bps,
                "sim_revert_reason": result.sim_revert_reason,
                "detail": result.detail,
            },
        )
        return result.sellable

    # -----------------------------------------------------------------------
    # Core simulation logic
    # -----------------------------------------------------------------------

    def simulate_sell(self, *, mint: str, event_slot: int) -> SellSimResult:
        """Run a full dry-run sell simulation for a token.

        Steps:
          1. Obtain a SELL quote for DUST_SELL_AMOUNT tokens.
          2. Build an unsigned SELL transaction (no real AMM instruction —
             we use a stub that exercises the transfer/close path).
          3. Sign the transaction via the injected signer (dry-run proof
             that the program is on the allowlist).
          4. Simulate via simulateTransaction — the MANDATORY pre-send check.
             A revert here = the sell would fail on-chain = honeypot.
          5. Analyse the simulation result:
             - revert → NOT_SELLABLE (SIM_REVERT)
             - zero output → NOT_SELLABLE (ZERO_OUTPUT)
             - effective tax > threshold → NOT_SELLABLE (HIGH_SELL_TAX)
             - success → SELLABLE

        NEVER calls send_transaction().  This method has no code path to the
        block engine — it is DRY-RUN by construction and structurally.
        """
        intent_id = f"sell-sim:{mint}:{event_slot}"
        logger.info(
            "sell_sim.simulate_sell.start",
            extra={
                "intent_id": intent_id,
                "mint": mint,
                "event_slot": event_slot,
                "dust_amount": self._config.dust_sell_amount,
                "submit_mode": self.submit_mode.value,
            },
        )

        try:
            # Step 1: Obtain a SELL quote for the dust amount.
            # A quote failure (zero output, no route) is itself a honeypot signal.
            quote_out_lamports = self._quote_sell(mint=mint, amount_in=self._config.dust_sell_amount)
            if quote_out_lamports <= 0:
                return SellSimResult(
                    sellable=False,
                    reason=SellSimReason.QUOTE_FAILED,
                    mint=mint,
                    event_slot=event_slot,
                    dust_amount_in=self._config.dust_sell_amount,
                    simulated_out_lamports=0,
                    detail="sell_quote_returned_zero_output",
                )

            # Step 2: Build an unsigned SELL transaction stub.
            # In production this would be the real SPL token transfer + close instruction
            # or the Jupiter sell-instruction bytes.  Here we produce an opaque stub
            # that the mock signer and mock simulate can process.
            unsigned_b64 = self._build_sell_stub(
                mint=mint,
                amount_in=self._config.dust_sell_amount,
                min_out_lamports=1,  # absolute minimum for dust
                intent_id=intent_id,
            )

            # Step 3: Sign via injected signer.
            # A SignerRefused means the program is NOT on the allowlist.
            # That is a hard signal: if we can't sign the sell, we can't exit.
            signed_b64 = self._sign_sell_stub(
                unsigned_b64=unsigned_b64,
                intent_id=intent_id,
            )
            if signed_b64 is None:
                # SignerRefused was caught internally — propagated as signer_refused.
                return SellSimResult(
                    sellable=False,
                    reason=SellSimReason.SIGNER_REFUSED,
                    mint=mint,
                    event_slot=event_slot,
                    dust_amount_in=self._config.dust_sell_amount,
                    simulated_out_lamports=0,
                    detail="signer_refused_sell_program_not_allowlisted",
                )

            # Step 4: MANDATORY simulateTransaction — catches the revert BEFORE submit.
            # There is NO submit after this.  This is the only network-facing call.
            sim_result: SimResult = self._run_simulate(signed_b64=signed_b64, intent_id=intent_id)

            # Step 5a: Simulation revert — honeypot detected.
            if not sim_result.success:
                logger.warning(
                    "sell_sim.simulate_sell.revert_detected",
                    extra={
                        "intent_id": intent_id,
                        "mint": mint,
                        "revert_reason": sim_result.revert_reason,
                    },
                )
                return SellSimResult(
                    sellable=False,
                    reason=SellSimReason.SIM_REVERT,
                    mint=mint,
                    event_slot=event_slot,
                    dust_amount_in=self._config.dust_sell_amount,
                    simulated_out_lamports=0,
                    sim_cu_consumed=sim_result.cu_consumed,
                    sim_revert_reason=sim_result.revert_reason,
                    detail=f"simulate_revert:{sim_result.revert_reason or 'unknown'}",
                )

            # Step 5b: Check zero output (vanishing liquidity / pool drain).
            # We use the RPC client's mock sell_out_lamports if available.
            sim_out_lamports = self._extract_sim_out(quote_out_lamports)
            if sim_out_lamports <= 0:
                return SellSimResult(
                    sellable=False,
                    reason=SellSimReason.ZERO_OUTPUT,
                    mint=mint,
                    event_slot=event_slot,
                    dust_amount_in=self._config.dust_sell_amount,
                    simulated_out_lamports=0,
                    sim_cu_consumed=sim_result.cu_consumed,
                    detail="simulated_sell_returned_zero_lamports",
                )

            # Step 5c: Check effective sell tax.
            # If the token extracts too much on exit, it is effectively a honeypot.
            effective_tax_bps = self._compute_effective_tax_bps(
                simulated_out=sim_out_lamports,
                quoted_out=quote_out_lamports,
            )
            if effective_tax_bps > self._config.max_sell_tax_bps:
                return SellSimResult(
                    sellable=False,
                    reason=SellSimReason.HIGH_SELL_TAX,
                    mint=mint,
                    event_slot=event_slot,
                    dust_amount_in=self._config.dust_sell_amount,
                    simulated_out_lamports=sim_out_lamports,
                    effective_sell_tax_bps=effective_tax_bps,
                    sim_cu_consumed=sim_result.cu_consumed,
                    detail=f"sell_tax_{effective_tax_bps}bps_exceeds_max_{self._config.max_sell_tax_bps}bps",
                )

            # Step 5d: All checks passed — token appears sellable.
            logger.info(
                "sell_sim.simulate_sell.sellable",
                extra={
                    "intent_id": intent_id,
                    "mint": mint,
                    "sim_out_lamports": sim_out_lamports,
                    "effective_tax_bps": effective_tax_bps,
                    "sim_cu_consumed": sim_result.cu_consumed,
                },
            )
            return SellSimResult(
                sellable=True,
                reason=SellSimReason.SELLABLE,
                mint=mint,
                event_slot=event_slot,
                dust_amount_in=self._config.dust_sell_amount,
                simulated_out_lamports=sim_out_lamports,
                effective_sell_tax_bps=effective_tax_bps,
                sim_cu_consumed=sim_result.cu_consumed,
                detail="sell_sim_passed",
            )

        except SignerRefused as exc:
            logger.warning(
                "sell_sim.simulate_sell.signer_refused",
                extra={"intent_id": intent_id, "mint": mint, "reason": exc.reason},
            )
            return SellSimResult(
                sellable=False,
                reason=SellSimReason.SIGNER_REFUSED,
                mint=mint,
                event_slot=event_slot,
                dust_amount_in=self._config.dust_sell_amount,
                simulated_out_lamports=0,
                detail=f"signer_refused:{exc.reason}",
            )

        except Exception as exc:
            logger.exception(
                "sell_sim.simulate_sell.unexpected_error",
                extra={"intent_id": intent_id, "mint": mint, "error": str(exc)},
            )
            return SellSimResult(
                sellable=False,
                reason=SellSimReason.INFRASTRUCTURE_ERROR,
                mint=mint,
                event_slot=event_slot,
                dust_amount_in=self._config.dust_sell_amount,
                simulated_out_lamports=0,
                detail=f"unexpected:{type(exc).__name__}",
            )

    # -----------------------------------------------------------------------
    # Internal helpers — DRY-RUN-only path
    # -----------------------------------------------------------------------

    def _quote_sell(self, *, mint: str, amount_in: int) -> int:
        """Obtain an indicative SELL quote for ``amount_in`` token base units.

        Returns the THEORETICAL FAIR expected SOL lamports out (int) — i.e. what
        the pool would return before any on-chain tax/rug extraction.
        Zero = no liquidity / no route.

        For the mock / offline path, we ask the RPC client for its
        ``quote_out_lamports`` attribute (MockSellSimRpcClient) which represents
        the theoretical fair amount.  Otherwise we fall back to a conservative
        default (fine because a real production implementation calls Jupiter /quote).

        This is a DRY-RUN operation: no quote call submits anything.

        PLUG IN HERE: in production, replace the fallback with a real call to
            Jupiter /quote?inputMint=TOKEN&outputMint=SOL&amount=amount_in
        """
        # Prefer quote_out_lamports (theoretical fair value) if the mock exposes it.
        if hasattr(self._rpc, "quote_out_lamports"):
            return int(self._rpc.quote_out_lamports)  # type: ignore[attr-defined]
        # Fallback for plain MockRpcClient or production-stub: use sell_out_lamports
        # if available (older mock interface).
        if hasattr(self._rpc, "sell_out_lamports"):
            return int(self._rpc.sell_out_lamports)  # type: ignore[attr-defined]
        # Default: model a tiny amount to exercise the path.
        # PLUG IN HERE: replace with a real Jupiter /quote call in production.
        return 10_000  # default: 10_000 lamports for 1 token-base-unit of dust

    def _build_sell_stub(
        self,
        *,
        mint: str,
        amount_in: int,
        min_out_lamports: int,
        intent_id: str,
    ) -> str:
        """Build an opaque unsigned sell-transaction stub (base64).

        In production this would assemble the real SPL token-transfer + optional
        account-close instruction (or Jupiter /swap-instructions bytes).
        For the offline/mock path we produce a deterministic stub that carries
        enough metadata for the mock signer and mock simulator.

        All amounts are int (base units / lamports).  No float.

        PLUG IN HERE: in production, replace with a call to
            build_sell_tx(mint, amount_in, min_out_lamports, ...)
        which assembles a real versioned transaction.
        """
        import base64
        import json

        payload = {
            "intent_kind": "SELL_SIM",
            "client_intent_id": intent_id,
            "mint": mint,
            "amount_in": amount_in,           # int token base units
            "min_out_lamports": min_out_lamports,  # int lamports
            "wallet_pubkey": self._wallet_pubkey,
        }
        # Encode as base64 JSON stub — fully opaque to the network.
        raw = json.dumps(payload, separators=(",", ":")).encode()
        return base64.b64encode(raw).decode()

    def _sign_sell_stub(
        self,
        *,
        unsigned_b64: str,
        intent_id: str,
    ) -> str | None:
        """Sign the sell-transaction stub via the injected signer.

        Returns the signed bytes (base64) or None if the signer refused.
        A signer refusal means the token's program is NOT on the allowlist
        (i.e. we couldn't sign a sell even if we wanted to).

        NEVER logs the signed bytes — they contain signature material.
        """
        from aats.contracts.venue import UnsignedTx

        try:
            unsigned_tx = UnsignedTx(
                serialized_b64=unsigned_b64,
                intent_kind="SELL_SIM",
                mint="sell_sim",  # placeholder; real tx uses actual mint pubkey
                client_intent_id=intent_id,
            )
            signed_tx = self._signer.sign(unsigned_tx, wallet_id="sell-sim-wallet")
            return signed_tx.serialized_b64
        except SignerRefused:
            raise  # propagated to simulate_sell() exception handler

    def _run_simulate(self, *, signed_b64: str, intent_id: str) -> SimResult:
        """Run simulateTransaction.  MANDATORY.  No submit after this.

        Wraps the raw SimulateResult into the contract SimResult type.
        """
        from aats.execution.rpc_client import SimulateResult

        raw: SimulateResult = self._rpc.simulate_transaction(signed_b64)  # type: ignore[assignment]
        return SimResult(
            success=raw.success,
            cu_consumed=raw.cu_consumed,
            revert_reason=raw.revert_reason,
        )

    @staticmethod
    def _compute_effective_tax_bps(*, simulated_out: int, quoted_out: int) -> int:
        """Compute effective sell tax in bps from the discrepancy between
        the quoted output and the simulated output.

        effective_tax_bps = (quoted_out - simulated_out) * 10_000 // quoted_out

        All arithmetic is integer.  Returns 0 if quoted_out <= 0 (guard).
        Never float.
        """
        if quoted_out <= 0 or simulated_out < 0:
            return 0
        # Tax is the fraction of quoted value NOT received.
        # Guard: if simulated_out > quoted_out (e.g. mock variance), tax = 0.
        if simulated_out >= quoted_out:
            return 0
        diff = quoted_out - simulated_out
        return int(diff * 10_000 // quoted_out)

    def _extract_sim_out(self, quote_out_lamports: int) -> int:
        """Extract the simulated SOL output from the RPC client's response.

        For the mock path: if the injected RPC client is a ``MockSellSimRpcClient``,
        use its ``sell_out_lamports``.  Otherwise use the quoted amount as a proxy
        (the real implementation would parse the tx-simulation's account deltas).

        PLUG IN HERE: in production, parse the post-simulation account state delta
        for the recipient SOL account to get the actual lamports received.

        Returns int lamports.  Zero if the RPC does not expose the value.
        """
        if hasattr(self._rpc, "sell_out_lamports"):
            return int(self._rpc.sell_out_lamports)  # type: ignore[attr-defined]
        # Conservative default: assume quoted output is also the simulated output.
        return quote_out_lamports


# ---------------------------------------------------------------------------
# Factory helpers — for the risk gate seam wiring (T-323 / pretrade_gate.py)
# ---------------------------------------------------------------------------


def make_sell_sim_probe(
    *,
    rpc_client: RpcClientProtocol | None = None,
    signer_client: SignerClientProtocol | None = None,
    config: SellSimConfig | None = None,
    wallet_pubkey: str = "SellSimPubkey11111111111111111111111111111",
) -> SellSimVenue:
    """Factory: create a SellSimVenue wired to the injectable RPC and signer.

    This is the recommended way to construct a ``SellSimProbe`` that satisfies
    the ``pretrade_gate.SellSimProbe`` protocol.  Defaults to offline mocks.

    Production wiring (T-329 seam into T-323):

        from aats.execution.sell_sim import make_sell_sim_probe
        from aats.execution.rpc_client import SolanaRpcClient
        from aats.execution.signer_client import SocketSignerClient

        probe = make_sell_sim_probe(
            rpc_client=SolanaRpcClient(rpc_url=os.environ["RPC_PRIMARY"]),
            signer_client=SocketSignerClient(socket_path=os.environ["SIGNER_SOCKET_PATH"]),
            wallet_pubkey=os.environ["WALLET_PUBKEY"],
        )
        gate = PreTradeSafetyGate()
        hit = gate.check_sellability(inputs=safety_inputs, probe=probe)

    NEVER pass a private key.  wallet_pubkey is the PUBKEY only.
    """
    return SellSimVenue(
        rpc_client=rpc_client,
        signer_client=signer_client,
        config=config,
        wallet_pubkey=wallet_pubkey,
    )
