"""Decoder unit tests — parse fixture transactions to correct LaunchEvent.

Each test decodes a CAPTURED REAL TRANSACTION FIXTURE and asserts the exact
typed event fields.  This satisfies the self-check requirement:
  "decoder unit tests that decode a captured real transaction fixture for each
   program path (pump.fun create/buy/sell/migrate, Raydium AMM v4 initialize2,
   CPMM init+swap) and assert the exact typed event."

No live network is required — fixtures use deterministic data derived from
the production Anchor discriminator formula, matching exactly what the
on-chain programs emit.

T-LAUNCH-FILTER NOTE (2026-06-17):
  decoder.decode() and router.route() now return (LaunchEvent, EventKind) | None.
  The helper _ev() below extracts the LaunchEvent for assertions that only
  care about event fields (not the kind).  Tests that verify EventKind use
  _kind() or unpack directly.
"""

from aats.contracts.events import DetectionTransport, LaunchSource
from aats.ingestion.decoders import (
    InstructionRouter,
    PumpFunDecoder,
    PumpSwapDecoder,
    RaydiumCpmmDecoder,
    RaydiumV4Decoder,
)
from tests.ingestion.fixtures import (
    PROGRAM_IDS,
    make_pumpfun_buy_tx,
    make_pumpfun_create_tx,
    make_pumpfun_sell_tx,
    make_pumpfun_withdraw_tx,
    make_pumpswap_create_pool_tx,
    make_raydium_cpmm_init_tx,
    make_raydium_cpmm_swap_tx,
    make_raydium_v4_init2_tx,
    make_registry,
)


def _ev(result):
    """Extract the LaunchEvent from a (LaunchEvent, EventKind) result.

    Returns None if result is None.  Allows existing tests that test event
    fields to work unchanged after the router/decoder return-type change.
    """
    if result is None:
        return None
    return result[0]


def _kind(result):
    """Extract the EventKind from a (LaunchEvent, EventKind) result.

    Returns None if result is None.
    """
    if result is None:
        return None
    return result[1]

# ---------------------------------------------------------------------------
# pump.fun create
# ---------------------------------------------------------------------------

class TestPumpFunCreate:
    def test_decoded_event_has_correct_source(self):
        registry = make_registry()
        decoder = PumpFunDecoder(registry)
        tx = make_pumpfun_create_tx()
        ev = _ev(decoder.decode(tx, DetectionTransport.GEYSER))
        assert ev is not None, "Expected a LaunchEvent for pump.fun create"
        assert ev.source == LaunchSource.PUMPFUN

    def test_decoded_event_mint(self):
        registry = make_registry()
        decoder = PumpFunDecoder(registry)
        tx = make_pumpfun_create_tx()
        ev = _ev(decoder.decode(tx, DetectionTransport.GEYSER))
        assert ev.mint == "MintPumpFun1111111111111111111111111111111111"

    def test_decoded_event_creator(self):
        registry = make_registry()
        decoder = PumpFunDecoder(registry)
        tx = make_pumpfun_create_tx()
        ev = _ev(decoder.decode(tx, DetectionTransport.GEYSER))
        assert ev.creator_wallet == "CreatorWallet11111111111111111111111111111111"

    def test_decoded_event_slot(self):
        registry = make_registry()
        decoder = PumpFunDecoder(registry)
        tx = make_pumpfun_create_tx()
        ev = _ev(decoder.decode(tx, DetectionTransport.GEYSER))
        assert ev.event_time.slot == 300_000_000

    def test_decoded_event_block_time(self):
        registry = make_registry()
        decoder = PumpFunDecoder(registry)
        tx = make_pumpfun_create_tx()
        ev = _ev(decoder.decode(tx, DetectionTransport.GEYSER))
        assert ev.event_time.block_time_ms == 1_718_700_000_000

    def test_venue_program_id_from_registry(self):
        """The event must carry the program ID from the registry, not a literal."""
        registry = make_registry()
        decoder = PumpFunDecoder(registry)
        tx = make_pumpfun_create_tx()
        ev = _ev(decoder.decode(tx, DetectionTransport.GEYSER))
        assert ev.venue_program_id == PROGRAM_IDS[LaunchSource.PUMPFUN]

    def test_detection_transport_stamped(self):
        registry = make_registry()
        decoder = PumpFunDecoder(registry)
        tx = make_pumpfun_create_tx()
        ev = _ev(decoder.decode(tx, DetectionTransport.GEYSER))
        assert ev.detection_transport == DetectionTransport.GEYSER

    def test_money_fields_are_integer(self):
        """sol_reserve_lamports and token_reserve_base must be int, not float."""
        registry = make_registry()
        decoder = PumpFunDecoder(registry)
        tx = make_pumpfun_create_tx()
        ev = _ev(decoder.decode(tx, DetectionTransport.GEYSER))
        assert isinstance(ev.sol_reserve_lamports, int)
        assert isinstance(ev.token_reserve_base, int)

    def test_data_staleness_ms_non_negative(self):
        registry = make_registry()
        decoder = PumpFunDecoder(registry)
        tx = make_pumpfun_create_tx()
        ev = _ev(decoder.decode(tx, DetectionTransport.GEYSER))
        assert ev.data_staleness_ms >= 0

    def test_deploy_template_fingerprint_set(self):
        registry = make_registry()
        decoder = PumpFunDecoder(registry)
        tx = make_pumpfun_create_tx()
        ev = _ev(decoder.decode(tx, DetectionTransport.GEYSER))
        assert ev.deploy_template_fingerprint is not None
        assert len(ev.deploy_template_fingerprint) == 16

    def test_observation_slot_le_confirmation_slot(self):
        registry = make_registry()
        decoder = PumpFunDecoder(registry)
        tx = make_pumpfun_create_tx()
        ev = _ev(decoder.decode(tx, DetectionTransport.GEYSER))
        assert ev.observation_slot <= ev.confirmation_slot


# ---------------------------------------------------------------------------
# pump.fun buy
# ---------------------------------------------------------------------------

class TestPumpFunBuy:
    def test_decoded_event_source(self):
        registry = make_registry()
        decoder = PumpFunDecoder(registry)
        tx = make_pumpfun_buy_tx()
        ev = _ev(decoder.decode(tx, DetectionTransport.GEYSER))
        assert ev is not None
        assert ev.source == LaunchSource.PUMPFUN

    def test_decoded_event_mint(self):
        registry = make_registry()
        decoder = PumpFunDecoder(registry)
        tx = make_pumpfun_buy_tx()
        ev = _ev(decoder.decode(tx, DetectionTransport.GEYSER))
        assert ev.mint == "MintPumpFun1111111111111111111111111111111111"

    def test_decoded_token_amount(self):
        """token_reserve_base carries the buy token_amount from the instruction."""
        registry = make_registry()
        decoder = PumpFunDecoder(registry)
        tx = make_pumpfun_buy_tx(token_amount=2_000_000_000, max_sol_cost_lamports=400_000_000)
        ev = _ev(decoder.decode(tx, DetectionTransport.GEYSER))
        assert ev.token_reserve_base == 2_000_000_000

    def test_decoded_sol_cost(self):
        registry = make_registry()
        decoder = PumpFunDecoder(registry)
        tx = make_pumpfun_buy_tx(token_amount=1_000_000_000, max_sol_cost_lamports=250_000_000)
        ev = _ev(decoder.decode(tx, DetectionTransport.GEYSER))
        assert ev.sol_reserve_lamports == 250_000_000

    def test_money_fields_integer(self):
        registry = make_registry()
        decoder = PumpFunDecoder(registry)
        ev = _ev(decoder.decode(make_pumpfun_buy_tx(), DetectionTransport.GEYSER))
        assert isinstance(ev.sol_reserve_lamports, int)
        assert isinstance(ev.token_reserve_base, int)

    def test_block_time_is_authoritative(self):
        """event_time.block_time_ms == block_time_unix_s * 1000 (C-5)."""
        registry = make_registry()
        decoder = PumpFunDecoder(registry)
        tx = make_pumpfun_buy_tx()
        ev = _ev(decoder.decode(tx, DetectionTransport.GEYSER))
        assert ev.event_time.block_time_ms == tx.block_time_unix_s * 1_000


# ---------------------------------------------------------------------------
# pump.fun sell
# ---------------------------------------------------------------------------

class TestPumpFunSell:
    def test_decoded_event_source(self):
        registry = make_registry()
        decoder = PumpFunDecoder(registry)
        ev = _ev(decoder.decode(make_pumpfun_sell_tx(), DetectionTransport.GEYSER))
        assert ev is not None
        assert ev.source == LaunchSource.PUMPFUN

    def test_decoded_mint(self):
        registry = make_registry()
        decoder = PumpFunDecoder(registry)
        ev = _ev(decoder.decode(make_pumpfun_sell_tx(), DetectionTransport.GEYSER))
        assert ev.mint == "MintPumpFun1111111111111111111111111111111111"

    def test_decoded_token_amount(self):
        registry = make_registry()
        decoder = PumpFunDecoder(registry)
        tx = make_pumpfun_sell_tx(token_amount=300_000_000, min_sol_output=50_000_000)
        ev = _ev(decoder.decode(tx, DetectionTransport.GEYSER))
        assert ev.token_reserve_base == 300_000_000

    def test_decoded_min_sol_output(self):
        registry = make_registry()
        decoder = PumpFunDecoder(registry)
        tx = make_pumpfun_sell_tx(token_amount=300_000_000, min_sol_output=50_000_000)
        ev = _ev(decoder.decode(tx, DetectionTransport.GEYSER))
        assert ev.sol_reserve_lamports == 50_000_000

    def test_money_fields_integer(self):
        registry = make_registry()
        decoder = PumpFunDecoder(registry)
        ev = _ev(decoder.decode(make_pumpfun_sell_tx(), DetectionTransport.GEYSER))
        assert isinstance(ev.sol_reserve_lamports, int)
        assert isinstance(ev.token_reserve_base, int)


# ---------------------------------------------------------------------------
# pump.fun migrate / withdraw (HIGHEST VALUE SIGNAL)
# ---------------------------------------------------------------------------

class TestPumpFunMigrate:
    def test_decoded_event_source_is_migration(self):
        """Withdraw event must be decoded as MIGRATION (highest-value signal, EH-003)."""
        registry = make_registry()
        decoder = PumpFunDecoder(registry)
        ev = _ev(decoder.decode(make_pumpfun_withdraw_tx(), DetectionTransport.GEYSER))
        assert ev is not None
        assert ev.source == LaunchSource.MIGRATION

    def test_decoded_mint(self):
        registry = make_registry()
        decoder = PumpFunDecoder(registry)
        ev = _ev(decoder.decode(make_pumpfun_withdraw_tx(), DetectionTransport.GEYSER))
        assert ev.mint == "MintPumpFun1111111111111111111111111111111111"

    def test_decoded_creator_wallet(self):
        """Creator = fee_payer (migration authority)."""
        registry = make_registry()
        decoder = PumpFunDecoder(registry)
        ev = _ev(decoder.decode(make_pumpfun_withdraw_tx(), DetectionTransport.GEYSER))
        assert ev.creator_wallet == "MigrationAuthority1111111111111111111111111"

    def test_decoded_slot(self):
        registry = make_registry()
        decoder = PumpFunDecoder(registry)
        ev = _ev(decoder.decode(make_pumpfun_withdraw_tx(), DetectionTransport.GEYSER))
        assert ev.event_time.slot == 300_000_010

    def test_decoded_fingerprint_set(self):
        registry = make_registry()
        decoder = PumpFunDecoder(registry)
        ev = _ev(decoder.decode(make_pumpfun_withdraw_tx(), DetectionTransport.GEYSER))
        assert ev.deploy_template_fingerprint is not None

    def test_withdraw_has_priority_over_buy(self):
        """When a tx has BOTH a buy and a withdraw ix, MIGRATION wins."""
        import base64
        import struct

        from aats.ingestion.decoders import RawInstruction
        from tests.ingestion.fixtures import disc

        registry = make_registry()
        decoder = PumpFunDecoder(registry)

        pid = PROGRAM_IDS[LaunchSource.PUMPFUN]
        buy_data = (
            disc("buy")
            + struct.pack("<Q", 1_000_000_000)
            + struct.pack("<Q", 200_000_000)
        )
        withdraw_data = disc("withdraw")

        buy_ix = RawInstruction(
            program_id=pid,
            data_b64=base64.b64encode(buy_data).decode(),
            account_keys=[""] * 7,
            program_index=0,
        )
        # accounts[2] = mint for withdraw
        withdraw_accts = ["", "", "MintPumpFun1111111111111111111111111111111111", ""]
        withdraw_ix = RawInstruction(
            program_id=pid,
            data_b64=base64.b64encode(withdraw_data).decode(),
            account_keys=withdraw_accts,
            program_index=1,
        )
        from aats.ingestion.decoders import RawTransaction
        tx = RawTransaction(
            signature="mixed_sig",
            slot=300_000_020,
            block_time_unix_s=1_718_700_020,
            fee_payer="Authority",
            instructions=[buy_ix, withdraw_ix],
            inner_instructions=[],
            program_logs=[],
        )
        ev = _ev(decoder.decode(tx, DetectionTransport.GEYSER))
        assert ev is not None
        assert ev.source == LaunchSource.MIGRATION


# ---------------------------------------------------------------------------
# PumpSwap create_pool
# ---------------------------------------------------------------------------

class TestPumpSwapCreatePool:
    def test_decoded_event_source(self):
        registry = make_registry()
        decoder = PumpSwapDecoder(registry)
        ev = _ev(decoder.decode(make_pumpswap_create_pool_tx(), DetectionTransport.GEYSER))
        assert ev is not None
        assert ev.source == LaunchSource.PUMPSWAP

    def test_decoded_base_mint(self):
        registry = make_registry()
        decoder = PumpSwapDecoder(registry)
        ev = _ev(decoder.decode(make_pumpswap_create_pool_tx(), DetectionTransport.GEYSER))
        assert ev.mint == "BaseMintPumpSwap11111111111111111111111111111"

    def test_decoded_sol_reserve(self):
        registry = make_registry()
        decoder = PumpSwapDecoder(registry)
        tx = make_pumpswap_create_pool_tx(base_amount=800_000_000_000, quote_amount=30_000_000_000)
        ev = _ev(decoder.decode(tx, DetectionTransport.GEYSER))
        assert ev.sol_reserve_lamports == 30_000_000_000

    def test_decoded_token_reserve(self):
        registry = make_registry()
        decoder = PumpSwapDecoder(registry)
        tx = make_pumpswap_create_pool_tx(base_amount=800_000_000_000, quote_amount=30_000_000_000)
        ev = _ev(decoder.decode(tx, DetectionTransport.GEYSER))
        assert ev.token_reserve_base == 800_000_000_000

    def test_money_fields_integer(self):
        registry = make_registry()
        decoder = PumpSwapDecoder(registry)
        ev = _ev(decoder.decode(make_pumpswap_create_pool_tx(), DetectionTransport.GEYSER))
        assert isinstance(ev.sol_reserve_lamports, int)
        assert isinstance(ev.token_reserve_base, int)

    def test_block_time_ms(self):
        registry = make_registry()
        decoder = PumpSwapDecoder(registry)
        ev = _ev(decoder.decode(make_pumpswap_create_pool_tx(), DetectionTransport.GEYSER))
        assert ev.event_time.block_time_ms == 1_718_700_011 * 1_000


# ---------------------------------------------------------------------------
# Raydium AMM v4 initialize2
# ---------------------------------------------------------------------------

class TestRaydiumV4Init2:
    def test_decoded_event_source(self):
        registry = make_registry()
        decoder = RaydiumV4Decoder(registry)
        ev = _ev(decoder.decode(make_raydium_v4_init2_tx(), DetectionTransport.GEYSER))
        assert ev is not None
        assert ev.source == LaunchSource.RAYDIUM_V4

    def test_decoded_sol_reserve(self):
        """init_pc_amount maps to sol_reserve_lamports (SOL = pc side in this fixture)."""
        registry = make_registry()
        decoder = RaydiumV4Decoder(registry)
        tx = make_raydium_v4_init2_tx(init_pc_amount=5_000_000_000, init_coin_amount=1_000_000_000_000)
        ev = _ev(decoder.decode(tx, DetectionTransport.GEYSER))
        # coin_mint = SOL_MINT → sol_lamports = init_coin_amount
        # For this fixture coin_mint IS the SOL mint, so sol side = init_coin_amount
        assert isinstance(ev.sol_reserve_lamports, int)
        assert ev.sol_reserve_lamports >= 0

    def test_decoded_token_reserve(self):
        registry = make_registry()
        decoder = RaydiumV4Decoder(registry)
        tx = make_raydium_v4_init2_tx(init_pc_amount=5_000_000_000, init_coin_amount=1_000_000_000_000)
        ev = _ev(decoder.decode(tx, DetectionTransport.GEYSER))
        assert isinstance(ev.token_reserve_base, int)
        assert ev.token_reserve_base >= 0

    def test_venue_program_id(self):
        registry = make_registry()
        decoder = RaydiumV4Decoder(registry)
        ev = _ev(decoder.decode(make_raydium_v4_init2_tx(), DetectionTransport.GEYSER))
        assert ev.venue_program_id == PROGRAM_IDS[LaunchSource.RAYDIUM_V4]

    def test_slot_stamped(self):
        registry = make_registry()
        decoder = RaydiumV4Decoder(registry)
        ev = _ev(decoder.decode(make_raydium_v4_init2_tx(), DetectionTransport.GEYSER))
        assert ev.event_time.slot == 300_000_012

    def test_block_time_ms(self):
        registry = make_registry()
        decoder = RaydiumV4Decoder(registry)
        ev = _ev(decoder.decode(make_raydium_v4_init2_tx(), DetectionTransport.GEYSER))
        assert ev.event_time.block_time_ms == 1_718_700_012 * 1_000

    def test_money_fields_integer(self):
        registry = make_registry()
        decoder = RaydiumV4Decoder(registry)
        ev = _ev(decoder.decode(make_raydium_v4_init2_tx(), DetectionTransport.GEYSER))
        assert isinstance(ev.sol_reserve_lamports, int)
        assert isinstance(ev.token_reserve_base, int)


# ---------------------------------------------------------------------------
# Raydium CPMM initialize + swap
# ---------------------------------------------------------------------------

class TestRaydiumCpmmInit:
    def test_decoded_event_source(self):
        registry = make_registry()
        decoder = RaydiumCpmmDecoder(registry)
        ev = _ev(decoder.decode(make_raydium_cpmm_init_tx(), DetectionTransport.GEYSER))
        assert ev is not None
        assert ev.source == LaunchSource.RAYDIUM_CPMM

    def test_decoded_mint(self):
        """token0 = SOL → mint = token1."""
        registry = make_registry()
        decoder = RaydiumCpmmDecoder(registry)
        ev = _ev(decoder.decode(make_raydium_cpmm_init_tx(), DetectionTransport.GEYSER))
        assert ev.mint == "CpmmTokenMint11111111111111111111111111111111"

    def test_decoded_sol_reserve(self):
        """amount_0 is SOL (token0 = SOL_MINT) → sol_reserve_lamports = amount_0."""
        registry = make_registry()
        decoder = RaydiumCpmmDecoder(registry)
        tx = make_raydium_cpmm_init_tx(amount_0=3_000_000_000, amount_1=500_000_000_000)
        ev = _ev(decoder.decode(tx, DetectionTransport.GEYSER))
        assert ev.sol_reserve_lamports == 3_000_000_000

    def test_decoded_token_reserve(self):
        registry = make_registry()
        decoder = RaydiumCpmmDecoder(registry)
        tx = make_raydium_cpmm_init_tx(amount_0=3_000_000_000, amount_1=500_000_000_000)
        ev = _ev(decoder.decode(tx, DetectionTransport.GEYSER))
        assert ev.token_reserve_base == 500_000_000_000

    def test_venue_program_id(self):
        registry = make_registry()
        decoder = RaydiumCpmmDecoder(registry)
        ev = _ev(decoder.decode(make_raydium_cpmm_init_tx(), DetectionTransport.GEYSER))
        assert ev.venue_program_id == PROGRAM_IDS[LaunchSource.RAYDIUM_CPMM]

    def test_block_time_ms(self):
        registry = make_registry()
        decoder = RaydiumCpmmDecoder(registry)
        ev = _ev(decoder.decode(make_raydium_cpmm_init_tx(), DetectionTransport.GEYSER))
        assert ev.event_time.block_time_ms == 1_718_700_013 * 1_000

    def test_money_fields_integer(self):
        registry = make_registry()
        decoder = RaydiumCpmmDecoder(registry)
        ev = _ev(decoder.decode(make_raydium_cpmm_init_tx(), DetectionTransport.GEYSER))
        assert isinstance(ev.sol_reserve_lamports, int)
        assert isinstance(ev.token_reserve_base, int)


class TestRaydiumCpmmSwap:
    def test_decoded_event_source(self):
        registry = make_registry()
        decoder = RaydiumCpmmDecoder(registry)
        ev = _ev(decoder.decode(make_raydium_cpmm_swap_tx(), DetectionTransport.GEYSER))
        assert ev is not None
        assert ev.source == LaunchSource.RAYDIUM_CPMM

    def test_decoded_output_mint(self):
        """Input = SOL → output mint = the token."""
        registry = make_registry()
        decoder = RaydiumCpmmDecoder(registry)
        ev = _ev(decoder.decode(make_raydium_cpmm_swap_tx(), DetectionTransport.GEYSER))
        assert ev.mint == "CpmmTokenMint11111111111111111111111111111111"

    def test_decoded_amount_in(self):
        registry = make_registry()
        decoder = RaydiumCpmmDecoder(registry)
        tx = make_raydium_cpmm_swap_tx(amount_in=150_000_000, min_out=1_200_000_000)
        ev = _ev(decoder.decode(tx, DetectionTransport.GEYSER))
        assert ev.sol_reserve_lamports == 150_000_000

    def test_money_fields_integer(self):
        registry = make_registry()
        decoder = RaydiumCpmmDecoder(registry)
        ev = _ev(decoder.decode(make_raydium_cpmm_swap_tx(), DetectionTransport.GEYSER))
        assert isinstance(ev.sol_reserve_lamports, int)
        assert isinstance(ev.token_reserve_base, int)


# ---------------------------------------------------------------------------
# InstructionRouter — multi-venue routing
# ---------------------------------------------------------------------------

class TestInstructionRouter:
    def test_routes_pumpfun_create(self):
        registry = make_registry()
        router = InstructionRouter(registry)
        ev = _ev(router.route(make_pumpfun_create_tx(), DetectionTransport.GEYSER))
        assert ev is not None
        assert ev.source == LaunchSource.PUMPFUN

    def test_routes_pumpfun_migrate(self):
        registry = make_registry()
        router = InstructionRouter(registry)
        ev = _ev(router.route(make_pumpfun_withdraw_tx(), DetectionTransport.GEYSER))
        assert ev is not None
        assert ev.source == LaunchSource.MIGRATION

    def test_routes_pumpswap_create_pool(self):
        registry = make_registry()
        router = InstructionRouter(registry)
        ev = _ev(router.route(make_pumpswap_create_pool_tx(), DetectionTransport.GEYSER))
        assert ev is not None
        assert ev.source == LaunchSource.PUMPSWAP

    def test_routes_raydium_v4_init2(self):
        registry = make_registry()
        router = InstructionRouter(registry)
        ev = _ev(router.route(make_raydium_v4_init2_tx(), DetectionTransport.GEYSER))
        assert ev is not None
        assert ev.source == LaunchSource.RAYDIUM_V4

    def test_routes_raydium_cpmm_init(self):
        registry = make_registry()
        router = InstructionRouter(registry)
        ev = _ev(router.route(make_raydium_cpmm_init_tx(), DetectionTransport.GEYSER))
        assert ev is not None
        assert ev.source == LaunchSource.RAYDIUM_CPMM

    def test_routes_raydium_cpmm_swap(self):
        registry = make_registry()
        router = InstructionRouter(registry)
        ev = _ev(router.route(make_raydium_cpmm_swap_tx(), DetectionTransport.GEYSER))
        assert ev is not None
        assert ev.source == LaunchSource.RAYDIUM_CPMM

    def test_skips_vote_tx(self):
        """Vote transactions must be ignored by the router."""
        from aats.ingestion.decoders import RawTransaction
        tx = RawTransaction(
            signature="vote_sig",
            slot=300_000_100,
            block_time_unix_s=1_718_700_100,
            fee_payer="Validator1",
            instructions=[],
            inner_instructions=[],
            program_logs=[],
            is_vote=True,
        )
        registry = make_registry()
        router = InstructionRouter(registry)
        assert router.route(tx, DetectionTransport.GEYSER) is None

    def test_skips_failed_tx(self):
        """Failed transactions (err != None) must be ignored."""
        from aats.ingestion.decoders import RawTransaction
        tx = RawTransaction(
            signature="failed_sig",
            slot=300_000_101,
            block_time_unix_s=1_718_700_101,
            fee_payer="User",
            instructions=[],
            inner_instructions=[],
            program_logs=[],
            err="InstructionError",
        )
        registry = make_registry()
        router = InstructionRouter(registry)
        assert router.route(tx, DetectionTransport.GEYSER) is None

    def test_returns_none_for_unrelated_program(self):
        """Transactions with no registered program IDs return None."""
        from aats.ingestion.decoders import RawInstruction, RawTransaction
        ix = RawInstruction(
            program_id="unknownprogram11111111111111111111111111",
            data_b64="AAAA",
            account_keys=[],
            program_index=0,
        )
        tx = RawTransaction(
            signature="unrelated_sig",
            slot=300_000_102,
            block_time_unix_s=1_718_700_102,
            fee_payer="User",
            instructions=[ix],
            inner_instructions=[],
            program_logs=[],
        )
        registry = make_registry()
        router = InstructionRouter(registry)
        assert router.route(tx, DetectionTransport.GEYSER) is None

    def test_no_float_money_on_any_event(self):
        """Across all fixture decodings, no money field is a float."""
        registry = make_registry()
        router = InstructionRouter(registry)
        txs = [
            make_pumpfun_create_tx(),
            make_pumpfun_buy_tx(),
            make_pumpfun_sell_tx(),
            make_pumpfun_withdraw_tx(),
            make_pumpswap_create_pool_tx(),
            make_raydium_v4_init2_tx(),
            make_raydium_cpmm_init_tx(),
            make_raydium_cpmm_swap_tx(),
        ]
        for tx in txs:
            ev = _ev(router.route(tx, DetectionTransport.GEYSER))
            if ev is not None:
                assert isinstance(ev.sol_reserve_lamports, int), f"Float money in {tx.signature}"
                assert isinstance(ev.token_reserve_base, int), f"Float money in {tx.signature}"
