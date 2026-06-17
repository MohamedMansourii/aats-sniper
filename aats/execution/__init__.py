"""M4 Guardrails — ExecutionVenue implementations (T-327).

Implemented by: solana-execution-engineer (T-327, T-328, T-329).

The ExecutionVenue ABC (promoted from sol-sniper/sniper_sim/venue.py via T-202)
is the seam: the loop core imports the interface only, never a concrete venue.

Venue implementations:
  SimulationVenue    — paper / replay mode (aats.contracts.venue); no network;
                       key-less sign(); submit_mode=SIMULATION.
  JitoJupiterVenue   — real venue (this module); DRY-RUN first (build+sign+simulate,
                       NO submit when DRY_RUN_ENABLED=true); sign() crosses to
                       aats-signer over a local Unix-domain socket (ADR-0009).

The loop core does:
    from aats.contracts.venue import ExecutionVenue   # ABC — the ONLY import
    # NEVER: from aats.execution.jito_jupiter_venue import JitoJupiterVenue

DRY-RUN is a HARD ARCHITECTURE CONSTRAINT (infrastructure.md §2, execution-venue.md §4):
  DRY_RUN_ENABLED=true (the default) makes real-capital submission physically
  impossible through THREE independent gates:
    1. venue.submit_mode == DRY_RUN  (structural)
    2. DRY_RUN_ENABLED env var != "false"  (config)
    3. JitoJupiterVenue live_submit_enabled=False  (construction flag)
"""
from aats.execution.exceptions import (
    AtomicityViolation,
    DevnetSubmitBlocked,
    DryRunBlocked,
    IdempotencyKeyConflict,
    LiveSubmitBlocked,
    QuoteStalenessError,
    SignerRefused,
    SimulationReverted,
    VenueError,
)
from aats.execution.jito_jupiter_venue import JitoJupiterVenue
from aats.execution.rpc_client import (
    ConfirmResult,
    DevnetRpcClient,
    MockDevnetRpcClient,
    MockRpcClient,
    MockRpcClientBlockhashExpiry,
    MockRpcClientRevert,
    SolanaRpcClient,
)
from aats.execution.multi_wallet import (
    AntiClusterCapExceeded,
    BundleFillResult,
    MintExposureLedger,
    MultiWalletConfigError,
    MultiWalletOrchestrator,
    N_WALLETS_MAX_DEFAULT,
    N_WALLETS_MAX_HARD_CEILING,
    PartialFillReconciler,
    PartialFillSummary,
    WalletSlotFill,
    make_single_wallet_orchestrator,
)
from aats.execution.sell_sim import (
    MockHoneypotRpcClient,
    MockSellSimRpcClient,
    SellSimConfig,
    SellSimReason,
    SellSimResult,
    SellSimVenue,
    make_sell_sim_probe,
)
from aats.execution.signer_client import (
    MockSignerClient,
    MockSignerClientRefusing,
    SocketSignerClient,
)

__all__ = [
    # Venue
    "JitoJupiterVenue",
    # Exceptions
    "VenueError",
    "SignerRefused",
    "SimulationReverted",
    "DryRunBlocked",
    "QuoteStalenessError",
    "AtomicityViolation",
    "IdempotencyKeyConflict",
    "LiveSubmitBlocked",
    "DevnetSubmitBlocked",
    # RPC clients (injectable)
    "MockRpcClient",
    "MockRpcClientRevert",
    "MockRpcClientBlockhashExpiry",
    "MockDevnetRpcClient",
    "DevnetRpcClient",
    "ConfirmResult",
    "SolanaRpcClient",
    # Signer clients (injectable)
    "MockSignerClient",
    "MockSignerClientRefusing",
    "SocketSignerClient",
    # Sell-sim (T-329)
    "SellSimVenue",
    "SellSimResult",
    "SellSimReason",
    "SellSimConfig",
    "MockSellSimRpcClient",
    "MockHoneypotRpcClient",
    "make_sell_sim_probe",
    # Multi-wallet / bundle execution (T-328)
    "MultiWalletOrchestrator",
    "MintExposureLedger",
    "BundleFillResult",
    "WalletSlotFill",
    "PartialFillReconciler",
    "PartialFillSummary",
    "AntiClusterCapExceeded",
    "MultiWalletConfigError",
    "N_WALLETS_MAX_DEFAULT",
    "N_WALLETS_MAX_HARD_CEILING",
    "make_single_wallet_orchestrator",
]
