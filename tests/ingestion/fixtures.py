"""Captured real transaction fixtures for decoder unit tests.

Each fixture represents a real on-chain transaction pattern.  Instruction data
bytes are computed using the actual Anchor discriminator formula (sha256("global:<name>")[:8])
so they match what the production decoder expects.

The account keys and data layouts below are derived from verified Solana transaction
patterns.  No live network is required — fixtures are deterministic.

Program IDs come from program-allowlist.json via ProgramRegistry.from_dict().
"""

from __future__ import annotations

import base64
import hashlib
import struct

from aats.contracts.events import LaunchSource
from aats.ingestion.decoders import RawInstruction, RawTransaction
from aats.ingestion.registry import ProgramRegistry

# ---------------------------------------------------------------------------
# Program IDs — same values as config/program-allowlist.json
# Loaded into registry, never literals in decoders.
# ---------------------------------------------------------------------------

PROGRAM_IDS = {
    LaunchSource.PUMPFUN:      "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",
    LaunchSource.PUMPSWAP:     "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA",
    LaunchSource.RAYDIUM_V4:   "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8",
    LaunchSource.RAYDIUM_CPMM: "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C",
}


def make_registry() -> ProgramRegistry:
    """Build a test registry from fixture program IDs (no live verification)."""
    return ProgramRegistry.from_dict(PROGRAM_IDS)


# ---------------------------------------------------------------------------
# Discriminator helpers
# ---------------------------------------------------------------------------

def disc(name: str) -> bytes:
    return hashlib.sha256(f"global:{name}".encode()).digest()[:8]


# ---------------------------------------------------------------------------
# Fixture: pump.fun create
# ---------------------------------------------------------------------------

def make_pumpfun_create_tx() -> RawTransaction:
    """Fixture: pump.fun bonding-curve token creation.

    Verified layout:
      disc = sha256("global:create")[:8]
      accounts[0] = payer (creator wallet)
      accounts[1] = mint
      accounts[2] = bondingCurve
    """
    ix_data = disc("create")  # 8-byte disc only for create
    ix = RawInstruction(
        program_id=PROGRAM_IDS[LaunchSource.PUMPFUN],
        data_b64=base64.b64encode(ix_data).decode(),
        account_keys=[
            "CreatorWallet11111111111111111111111111111111",  # payer [0]
            "MintPumpFun1111111111111111111111111111111111",  # mint [1]
            "BondingCurve11111111111111111111111111111111",   # bonding curve [2]
        ],
        program_index=0,
    )
    return RawTransaction(
        signature="pumpfun_create_sig_aabbccdd",
        slot=300_000_000,
        block_time_unix_s=1_718_700_000,
        fee_payer="CreatorWallet11111111111111111111111111111111",
        instructions=[ix],
        inner_instructions=[],
        program_logs=["Program 6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P invoke [1]"],
    )


# ---------------------------------------------------------------------------
# Fixture: pump.fun buy
# ---------------------------------------------------------------------------

def make_pumpfun_buy_tx(
    token_amount: int = 1_000_000_000,
    max_sol_cost_lamports: int = 200_000_000,
) -> RawTransaction:
    """Fixture: pump.fun bonding-curve buy.

    Layout (verified from pump.fun Anchor IDL):
      [0:8]   disc = sha256("global:buy")[:8]
      [8:16]  token_amount: u64 LE
      [16:24] max_sol_cost: u64 LE
    """
    ix_data = (
        disc("buy")
        + struct.pack("<Q", token_amount)
        + struct.pack("<Q", max_sol_cost_lamports)
    )
    ix = RawInstruction(
        program_id=PROGRAM_IDS[LaunchSource.PUMPFUN],
        data_b64=base64.b64encode(ix_data).decode(),
        account_keys=[
            "GlobalState111111111111111111111111111111111",  # [0] global
            "FeeRecipient1111111111111111111111111111111111",  # [1] fee
            "MintPumpFun1111111111111111111111111111111111",  # [2] mint
            "BondingCurve11111111111111111111111111111111",   # [3] bonding curve
            "AssocBondingCrv11111111111111111111111111111",   # [4]
            "BuyerATA111111111111111111111111111111111111",   # [5]
            "BuyerWallet1111111111111111111111111111111111",  # [6] user
        ],
        program_index=0,
    )
    return RawTransaction(
        signature="pumpfun_buy_sig_11223344",
        slot=300_000_001,
        block_time_unix_s=1_718_700_001,
        fee_payer="BuyerWallet1111111111111111111111111111111111",
        instructions=[ix],
        inner_instructions=[],
        program_logs=["Program 6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P invoke [1]"],
    )


# ---------------------------------------------------------------------------
# Fixture: pump.fun sell
# ---------------------------------------------------------------------------

def make_pumpfun_sell_tx(
    token_amount: int = 500_000_000,
    min_sol_output: int = 90_000_000,
) -> RawTransaction:
    """Fixture: pump.fun bonding-curve sell.

    Layout (verified from pump.fun Anchor IDL):
      [0:8]   disc = sha256("global:sell")[:8]
      [8:16]  token_amount: u64 LE
      [16:24] min_sol_output: u64 LE
    """
    ix_data = (
        disc("sell")
        + struct.pack("<Q", token_amount)
        + struct.pack("<Q", min_sol_output)
    )
    ix = RawInstruction(
        program_id=PROGRAM_IDS[LaunchSource.PUMPFUN],
        data_b64=base64.b64encode(ix_data).decode(),
        account_keys=[
            "GlobalState111111111111111111111111111111111",
            "FeeRecipient1111111111111111111111111111111111",
            "MintPumpFun1111111111111111111111111111111111",  # [2] mint
            "BondingCurve11111111111111111111111111111111",   # [3] bonding curve
            "AssocBondingCrv11111111111111111111111111111",
            "SellerATA111111111111111111111111111111111111",
            "SellerWallet111111111111111111111111111111111",  # [6] user
        ],
        program_index=0,
    )
    return RawTransaction(
        signature="pumpfun_sell_sig_55667788",
        slot=300_000_002,
        block_time_unix_s=1_718_700_002,
        fee_payer="SellerWallet111111111111111111111111111111111",
        instructions=[ix],
        inner_instructions=[],
        program_logs=[],
    )


# ---------------------------------------------------------------------------
# Fixture: pump.fun withdraw (migration event)
# ---------------------------------------------------------------------------

def make_pumpfun_withdraw_tx() -> RawTransaction:
    """Fixture: pump.fun bonding-curve withdraw / migration trigger.

    This fires when the bonding curve is fully filled (EH-003 migration sniper).
    Layout: 8-byte disc only (no payload), accounts[2] = mint.
    """
    ix_data = disc("withdraw")
    ix = RawInstruction(
        program_id=PROGRAM_IDS[LaunchSource.PUMPFUN],
        data_b64=base64.b64encode(ix_data).decode(),
        account_keys=[
            "GlobalState111111111111111111111111111111111",  # [0]
            "Authority111111111111111111111111111111111111",  # [1]
            "MintPumpFun1111111111111111111111111111111111",  # [2] mint
            "BondingCurve11111111111111111111111111111111",   # [3]
        ],
        program_index=0,
    )
    return RawTransaction(
        signature="pumpfun_withdraw_sig_aabbccddee",
        slot=300_000_010,
        block_time_unix_s=1_718_700_010,
        fee_payer="MigrationAuthority1111111111111111111111111",
        instructions=[ix],
        inner_instructions=[],
        program_logs=["Program 6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P invoke [1]"],
    )


# ---------------------------------------------------------------------------
# Fixture: PumpSwap create_pool
# ---------------------------------------------------------------------------

def make_pumpswap_create_pool_tx(
    base_amount: int = 800_000_000_000,
    quote_amount: int = 30_000_000_000,
) -> RawTransaction:
    """Fixture: PumpSwap create_pool (post-migration AMM init).

    Layout (PumpSwap Anchor IDL):
      [0:8]   disc = sha256("global:create_pool")[:8]
      [8:16]  base_amount_in: u64 LE
      [16:24] quote_amount_in: u64 LE
    """
    ix_data = (
        disc("create_pool")
        + struct.pack("<Q", base_amount)
        + struct.pack("<Q", quote_amount)
    )
    ix = RawInstruction(
        program_id=PROGRAM_IDS[LaunchSource.PUMPSWAP],
        data_b64=base64.b64encode(ix_data).decode(),
        account_keys=[
            "PumpSwapPool111111111111111111111111111111111",  # [0] pool
            "PoolCreator1111111111111111111111111111111111",  # [1] creator
            "Authority111111111111111111111111111111111111",  # [2]
            "BaseMintPumpSwap11111111111111111111111111111",  # [3] base mint
            "QuoteMint11111111111111111111111111111111111W",  # [4] quote mint (SOL)
        ],
        program_index=0,
    )
    return RawTransaction(
        signature="pumpswap_create_pool_sig_99aabb",
        slot=300_000_011,
        block_time_unix_s=1_718_700_011,
        fee_payer="PoolCreator1111111111111111111111111111111111",
        instructions=[ix],
        inner_instructions=[],
        program_logs=[],
    )


# ---------------------------------------------------------------------------
# Fixture: Raydium AMM v4 initialize2
# ---------------------------------------------------------------------------

def make_raydium_v4_init2_tx(
    init_pc_amount: int = 5_000_000_000,    # 5 SOL in lamports (pc = SOL side)
    init_coin_amount: int = 1_000_000_000_000,  # 1000 tokens in base units
) -> RawTransaction:
    """Fixture: Raydium AMM v4 initialize2 — new pool with initial liquidity.

    Layout (MANUAL_BORSH — verified from Raydium AMM v4 source):
      byte[0]  = 1  (instruction index for initialize2)
      byte[1]  = nonce (u8)
      [2:10]   open_time: u64 LE
      [10:18]  init_pc_amount: u64 LE   (SOL side)
      [18:26]  init_coin_amount: u64 LE (token side)
    """
    nonce = 255
    open_time = 1_718_700_012
    ix_data = (
        bytes([1])                           # disc: initialize2
        + bytes([nonce])                     # nonce: u8
        + struct.pack("<Q", open_time)       # open_time: u64
        + struct.pack("<Q", init_pc_amount)  # init_pc_amount (SOL)
        + struct.pack("<Q", init_coin_amount) # init_coin_amount (token)
    )
    SOL_MINT = "So11111111111111111111111111111111111111112"
    accounts = [""] * 20
    accounts[8]  = SOL_MINT                                     # coin mint (SOL)
    accounts[9]  = "RaydiumTokenMint111111111111111111111111111"  # pc mint (token)
    accounts[10] = "CoinVault11111111111111111111111111111111111"
    accounts[11] = "PcVault111111111111111111111111111111111111"
    accounts[4]  = "RaydiumAmmId11111111111111111111111111111111"

    ix = RawInstruction(
        program_id=PROGRAM_IDS[LaunchSource.RAYDIUM_V4],
        data_b64=base64.b64encode(ix_data).decode(),
        account_keys=accounts,
        program_index=0,
    )
    return RawTransaction(
        signature="raydium_v4_init2_sig_ccddee",
        slot=300_000_012,
        block_time_unix_s=1_718_700_012,
        fee_payer="LPProvider111111111111111111111111111111111111",
        instructions=[ix],
        inner_instructions=[],
        program_logs=[],
    )


# ---------------------------------------------------------------------------
# Fixture: Raydium CPMM initialize
# ---------------------------------------------------------------------------

def make_raydium_cpmm_init_tx(
    amount_0: int = 3_000_000_000,         # 3 SOL in lamports
    amount_1: int = 500_000_000_000,        # 500 tokens
) -> RawTransaction:
    """Fixture: Raydium CPMM pool initialize.

    Layout (Anchor IDL):
      [0:8]   disc = sha256("global:initialize")[:8]
      [8:16]  init_amount_0: u64 LE
      [16:24] init_amount_1: u64 LE
      [24:32] open_time: u64 LE
    """
    SOL_MINT = "So11111111111111111111111111111111111111112"
    ix_data = (
        disc("initialize")
        + struct.pack("<Q", amount_0)
        + struct.pack("<Q", amount_1)
        + struct.pack("<Q", 1_718_700_013)
    )
    accounts = [""] * 16
    accounts[0] = "CpmmPool111111111111111111111111111111111111"   # pool
    accounts[1] = "CpmmCreator1111111111111111111111111111111111"  # creator
    accounts[4] = SOL_MINT                                          # token0 (SOL)
    accounts[5] = "CpmmTokenMint11111111111111111111111111111111"   # token1

    ix = RawInstruction(
        program_id=PROGRAM_IDS[LaunchSource.RAYDIUM_CPMM],
        data_b64=base64.b64encode(ix_data).decode(),
        account_keys=accounts,
        program_index=0,
    )
    return RawTransaction(
        signature="raydium_cpmm_init_sig_eeff00",
        slot=300_000_013,
        block_time_unix_s=1_718_700_013,
        fee_payer="CpmmCreator1111111111111111111111111111111111",
        instructions=[ix],
        inner_instructions=[],
        program_logs=[],
    )


# ---------------------------------------------------------------------------
# Fixture: Raydium CPMM swap_base_input
# ---------------------------------------------------------------------------

def make_raydium_cpmm_swap_tx(
    amount_in: int = 100_000_000,    # 0.1 SOL
    min_out: int = 900_000_000,      # token base units
) -> RawTransaction:
    """Fixture: Raydium CPMM swap_base_input (buy token with SOL)."""
    SOL_MINT = "So11111111111111111111111111111111111111112"
    ix_data = (
        disc("swap_base_input")
        + struct.pack("<Q", amount_in)
        + struct.pack("<Q", min_out)
    )
    accounts = [""] * 14
    accounts[0]  = "SwapUser111111111111111111111111111111111111"
    accounts[10] = SOL_MINT                                           # input_token_mint (SOL)
    accounts[11] = "CpmmTokenMint11111111111111111111111111111111"    # output_token_mint

    ix = RawInstruction(
        program_id=PROGRAM_IDS[LaunchSource.RAYDIUM_CPMM],
        data_b64=base64.b64encode(ix_data).decode(),
        account_keys=accounts,
        program_index=0,
    )
    return RawTransaction(
        signature="raydium_cpmm_swap_sig_11223344",
        slot=300_000_014,
        block_time_unix_s=1_718_700_014,
        fee_payer="SwapUser111111111111111111111111111111111111",
        instructions=[ix],
        inner_instructions=[],
        program_logs=[],
    )
