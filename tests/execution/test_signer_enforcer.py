"""ADR-0015 refusal-test suite -- the conformance spec for the un-bypassable signer contract.

Mandatory coverage (ADR-0015 "Refusal tests the engineer must ship"):
  over-cap tx -> signer_per_tx_cap_exceeded
  off-allowlist program -> signer_program_not_allowlisted
  System transfer to a non-pinned recipient -> signer_unpinned_transfer
  venue instruction with an unknown discriminator -> signer_spend_undecodable
  N-th sign in-window over aggregate/velocity -> signer_aggregate_cap_exceeded / signer_velocity_exceeded
  caller lying in sol_in/intent_kind does NOT change the verdict (structural: enforce()'s
    signature has no such parameters at all -- proven at the wire level in test_signer_process.py)
  a valid in-budget tx signs (enforce() returns None)

Plus: C0 unparseable, C0a ALT-region refusal, C5 approval hygiene, own-ATA pin,
ledger non-mutation on refusal, and the real shipped config loads fail-closed.
"""
from __future__ import annotations

import hashlib
import struct
import time

import pytest

from aats.execution import solana_wire as sw
from aats.execution.exceptions import SignerRefused
from aats.execution.signer_enforcer import (
    Enforcer,
    SignerPolicy,
    VelocityLedger,
    derive_ata,
    load_signer_policy,
)

# ---------------------------------------------------------------------------
# Deterministic synthetic pubkeys (valid 32-byte base58, no collisions)
# ---------------------------------------------------------------------------


def _pubkey(seed: str) -> str:
    return sw.b58encode(hashlib.sha256(f"test-pubkey-{seed}".encode()).digest())


WALLET = _pubkey("wallet")
SYSTEM = _pubkey("system_program")
COMPUTE_BUDGET = _pubkey("compute_budget")
VENUE = _pubkey("venue_program")
OTHER_VENUE_NO_DECODER = _pubkey("venue_program_no_decoder")
TIP_1 = _pubkey("tip_account_1")
TIP_2 = _pubkey("tip_account_2")
ATA_PROGRAM = _pubkey("ata_program")
SPL_TOKEN = _pubkey("spl_token_program")
MINT = _pubkey("mint")
ATTACKER = _pubkey("attacker")
OFF_ALLOWLIST_PROGRAM = _pubkey("off_allowlist_program")

_BLOCKHASH = _pubkey("blockhash")  # any 32-byte value works as a blockhash slot

_VENUE_BUY_DISCRIMINATOR = b"\x01\x02\x03\x04"
_PER_TX_CAP = 100_000_000
_AGG_CAP = 500_000_000
_WINDOW_S = 60
_MAX_SIGN_COUNT = 10


class _FakeVenueDecoder:
    """Mirrors a real pump.fun-style bonding-curve buy: discriminator + max_sol_cost(u64 LE)."""

    def max_sol_lamports(self, ix_data: bytes) -> int:
        if ix_data[:4] != _VENUE_BUY_DISCRIMINATOR:
            raise ValueError("unknown discriminator")
        if len(ix_data) < 12:
            raise ValueError("truncated buy instruction")
        return struct.unpack("<Q", ix_data[4:12])[0]


def _make_policy(**overrides: object) -> SignerPolicy:
    defaults: dict[str, object] = {
        "per_tx_cap_lamports": _PER_TX_CAP,
        "aggregate_cap_lamports": _AGG_CAP,
        "window_seconds": _WINDOW_S,
        "max_sign_count": _MAX_SIGN_COUNT,
        "allowed_program_ids": frozenset({SYSTEM, COMPUTE_BUDGET, VENUE, ATA_PROGRAM, SPL_TOKEN}),
        "venue_program_ids": frozenset({VENUE}),
        "pinned_tip_accounts": frozenset({TIP_1, TIP_2}),
        "ata_program_id": ATA_PROGRAM,
        "spl_token_program_id": SPL_TOKEN,
        "venue_spend_decoders": {VENUE: _FakeVenueDecoder()},
        "system_program_id": SYSTEM,
        "compute_budget_program_id": COMPUTE_BUDGET,
    }
    defaults.update(overrides)
    return SignerPolicy(**defaults)  # type: ignore[arg-type]


def _system_transfer_ix(*, source: int, recipient: int, lamports: int) -> sw.RawInstruction:
    data = struct.pack("<IQ", 2, lamports)  # discriminant=2 (Transfer), lamports LE u64
    return sw.RawInstruction(program_id=SYSTEM, accounts=(source, recipient), data=data)


def _venue_buy_ix(*, sol_cost: int, discriminator: bytes = _VENUE_BUY_DISCRIMINATOR,
                   program_id: str = VENUE) -> sw.RawInstruction:
    data = discriminator + struct.pack("<Q", sol_cost)
    return sw.RawInstruction(program_id=program_id, accounts=(0,), data=data)


def _compute_budget_ix(cu_limit: int = 0) -> sw.RawInstruction:
    """ComputeBudget::SetComputeUnitLimit(cu_limit) -- tag=2, u32 LE."""
    return sw.RawInstruction(
        program_id=COMPUTE_BUDGET, accounts=(), data=b"\x02" + struct.pack("<I", cu_limit)
    )


def _compute_budget_price_ix(cu_price_microlamports: int) -> sw.RawInstruction:
    """ComputeBudget::SetComputeUnitPrice(cu_price_microlamports) -- tag=3, u64 LE."""
    return sw.RawInstruction(
        program_id=COMPUTE_BUDGET, accounts=(), data=b"\x03" + struct.pack("<Q", cu_price_microlamports)
    )


def _build_tx(account_keys: list[str], instructions: list[sw.RawInstruction], **kwargs: object) -> bytes:
    return sw.build_versioned_tx(
        account_keys=account_keys,
        num_required_signatures=1,
        recent_blockhash=_BLOCKHASH,
        instructions=instructions,
        **kwargs,  # type: ignore[arg-type]
    )


def _enforcer(policy: SignerPolicy | None = None) -> tuple[Enforcer, VelocityLedger]:
    p = policy or _make_policy()
    ledger = VelocityLedger(window_seconds=p.window_seconds)
    return Enforcer(p, ledger), ledger


# ---------------------------------------------------------------------------
# A valid, in-budget tx signs (enforce() returns None)
# ---------------------------------------------------------------------------


class TestValidTxSigns:
    def test_valid_tx_within_budget_returns_none(self) -> None:
        enforcer, _ledger = _enforcer()
        tx = _build_tx(
            [WALLET, COMPUTE_BUDGET, VENUE, SYSTEM, TIP_1],
            [
                _compute_budget_ix(),
                _venue_buy_ix(sol_cost=10_000_000),
                _system_transfer_ix(source=0, recipient=4, lamports=1_000),
            ],
        )
        enforcer.enforce(tx, WALLET, time.monotonic())

    def test_self_transfer_is_pinned(self) -> None:
        enforcer, _ledger = _enforcer()
        tx = _build_tx(
            [WALLET, SYSTEM],
            [_system_transfer_ix(source=0, recipient=0, lamports=500)],
        )
        enforcer.enforce(tx, WALLET, time.monotonic())

    def test_transfer_to_pinned_tip_account_signs(self) -> None:
        enforcer, _ledger = _enforcer()
        tx = _build_tx(
            [WALLET, SYSTEM, TIP_2],
            [_system_transfer_ix(source=0, recipient=2, lamports=200_000)],
        )
        enforcer.enforce(tx, WALLET, time.monotonic())

    def test_a_successful_enforce_can_be_recorded_and_re_checked(self) -> None:
        """A minimal end-to-end proof the ledger accepts the post-enforce() record step."""
        enforcer, ledger = _enforcer()
        tx = _build_tx(
            [WALLET, SYSTEM, TIP_1],
            [_system_transfer_ix(source=0, recipient=2, lamports=1_000)],
        )
        now = time.monotonic()
        enforcer.enforce(tx, WALLET, now)
        spend = enforcer.compute_spend_lamports(tx, WALLET)
        assert spend == 1_000
        ledger.record(now, spend)

    def test_price_cache_does_not_bleed_across_different_transactions(self) -> None:
        """The perf-only price cache (fix round 1 MINOR finding) is keyed by VALUE
        equality of (tx_bytes, wallet_pubkey) -- prove a second, different tx enforced
        on the SAME Enforcer instance gets its OWN correctly-computed spend, not a
        stale hit from the first tx's cached entry."""
        enforcer, _ledger = _enforcer()
        tx_a = _build_tx(
            [WALLET, SYSTEM, TIP_1], [_system_transfer_ix(source=0, recipient=2, lamports=1_000)]
        )
        tx_b = _build_tx(
            [WALLET, SYSTEM, TIP_1], [_system_transfer_ix(source=0, recipient=2, lamports=2_000)]
        )
        enforcer.enforce(tx_a, WALLET, time.monotonic())
        assert enforcer.compute_spend_lamports(tx_a, WALLET) == 1_000
        enforcer.enforce(tx_b, WALLET, time.monotonic())
        assert enforcer.compute_spend_lamports(tx_b, WALLET) == 2_000
        # Re-querying tx_a after tx_b was enforced must still be correct (a fresh
        # re-parse, not a stale cache hit for tx_b).
        assert enforcer.compute_spend_lamports(tx_a, WALLET) == 1_000


# ---------------------------------------------------------------------------
# C3 -- over-cap tx refuses
# ---------------------------------------------------------------------------


class TestOverCapRefuses:
    def test_over_per_tx_cap_refuses(self) -> None:
        enforcer, _ledger = _enforcer()
        tx = _build_tx(
            [WALLET, VENUE],
            [_venue_buy_ix(sol_cost=_PER_TX_CAP + 1)],
        )
        with pytest.raises(SignerRefused) as exc_info:
            enforcer.enforce(tx, WALLET, time.monotonic())
        assert exc_info.value.reason == "signer_per_tx_cap_exceeded"

    def test_combined_system_plus_venue_spend_over_cap_refuses(self) -> None:
        enforcer, _ledger = _enforcer()
        tx = _build_tx(
            [WALLET, SYSTEM, VENUE, TIP_1],
            [
                _system_transfer_ix(source=0, recipient=3, lamports=60_000_000),
                _venue_buy_ix(sol_cost=60_000_000),
            ],
        )
        with pytest.raises(SignerRefused) as exc_info:
            enforcer.enforce(tx, WALLET, time.monotonic())
        assert exc_info.value.reason == "signer_per_tx_cap_exceeded"

    def test_at_exactly_the_cap_signs(self) -> None:
        enforcer, _ledger = _enforcer()
        tx = _build_tx([WALLET, VENUE], [_venue_buy_ix(sol_cost=_PER_TX_CAP)])
        enforcer.enforce(tx, WALLET, time.monotonic())


# ---------------------------------------------------------------------------
# C3 (fix round 1 MAJOR finding) -- ComputeBudget priority-fee outflow is priced
# and bounded by the per-tx / aggregate caps, not left at 0.
# ---------------------------------------------------------------------------


class TestPriorityFeeOutflowIsPriced:
    def test_priority_fee_alone_over_cap_refuses(self) -> None:
        """The MAJOR-finding attack: [SetComputeUnitLimit(huge), SetComputeUnitPrice(huge)]
        with NO System transfer and NO venue instruction at all must still refuse --
        before the fix, system_spend=0 and venue_spend=0 meant this signed cleanly."""
        enforcer, _ledger = _enforcer()
        # 1_400_000 CU * 100_000_000 microlamports/CU / 1_000_000 = 140_000_000 lamports
        # (0.14 SOL) > the 0.1 SOL per-tx cap.
        tx = _build_tx(
            [WALLET, COMPUTE_BUDGET],
            [_compute_budget_ix(cu_limit=1_400_000), _compute_budget_price_ix(100_000_000)],
        )
        with pytest.raises(SignerRefused) as exc_info:
            enforcer.enforce(tx, WALLET, time.monotonic())
        assert exc_info.value.reason == "signer_per_tx_cap_exceeded"

    def test_priority_fee_folds_into_compute_spend_lamports(self) -> None:
        enforcer, _ledger = _enforcer()
        # 100_000 CU * 1_000 microlamports/CU / 1_000_000 = 100 lamports.
        tx = _build_tx(
            [WALLET, COMPUTE_BUDGET],
            [_compute_budget_ix(cu_limit=100_000), _compute_budget_price_ix(1_000)],
        )
        enforcer.enforce(tx, WALLET, time.monotonic())
        assert enforcer.compute_spend_lamports(tx, WALLET) == 100

    def test_missing_cu_limit_prices_the_protocol_max_conservatively(self) -> None:
        """No SetComputeUnitLimit instruction at all -- the signer must price the
        protocol-max CU ceiling (1_400_000), never assume the smaller runtime default,
        so an attacker cannot hide outflow by omitting the limit instruction."""
        enforcer, _ledger = _enforcer()
        # price alone, no limit ix: at 1_000_000 microlamports/CU (== 1 lamport/CU) the
        # fee equals the CU count directly: protocol-max 1_400_000 CU -> 1_400_000 lamports.
        tx = _build_tx([WALLET, COMPUTE_BUDGET], [_compute_budget_price_ix(1_000_000)])
        enforcer.enforce(tx, WALLET, time.monotonic())
        assert enforcer.compute_spend_lamports(tx, WALLET) == 1_400_000

    def test_no_price_ix_means_zero_priority_fee(self) -> None:
        """SetComputeUnitLimit with no SetComputeUnitPrice contributes 0 -- only the
        price (microlamports/CU) instruction, not the limit alone, spends SOL."""
        enforcer, _ledger = _enforcer()
        tx = _build_tx([WALLET, COMPUTE_BUDGET], [_compute_budget_ix(cu_limit=1_400_000)])
        enforcer.enforce(tx, WALLET, time.monotonic())
        assert enforcer.compute_spend_lamports(tx, WALLET) == 0

    def test_duplicate_instructions_take_the_max_not_the_sum(self) -> None:
        enforcer, _ledger = _enforcer()
        tx = _build_tx(
            [WALLET, COMPUTE_BUDGET],
            [
                _compute_budget_ix(cu_limit=100_000),
                _compute_budget_ix(cu_limit=50_000),  # smaller -- max() must still win
                _compute_budget_price_ix(1_000),
                _compute_budget_price_ix(500),  # smaller -- max() must still win
            ],
        )
        enforcer.enforce(tx, WALLET, time.monotonic())
        # max(100_000, 50_000) * max(1_000, 500) / 1_000_000 = 100 lamports, not 100+50.
        assert enforcer.compute_spend_lamports(tx, WALLET) == 100

    def test_priority_fee_combines_with_venue_spend_for_the_cap(self) -> None:
        enforcer, _ledger = _enforcer()
        tx = _build_tx(
            [WALLET, COMPUTE_BUDGET, VENUE],
            [
                _compute_budget_ix(cu_limit=1_000_000),
                # 1_000_000 * 50_000_000 / 1e6 = 50_000_000 lamports.
                _compute_budget_price_ix(50_000_000),
                _venue_buy_ix(sol_cost=_PER_TX_CAP - 50_000_000),  # exactly at the cap combined
            ],
        )
        enforcer.enforce(tx, WALLET, time.monotonic())  # must sign at exactly the cap

        tx_over = _build_tx(
            [WALLET, COMPUTE_BUDGET, VENUE],
            [
                _compute_budget_ix(cu_limit=1_000_000),
                _compute_budget_price_ix(50_000_000),
                _venue_buy_ix(sol_cost=_PER_TX_CAP - 50_000_000 + 1),  # 1 lamport over
            ],
        )
        with pytest.raises(SignerRefused) as exc_info:
            enforcer.enforce(tx_over, WALLET, time.monotonic())
        assert exc_info.value.reason == "signer_per_tx_cap_exceeded"

    def test_ceiling_rounding_never_underprices(self) -> None:
        enforcer, _ledger = _enforcer()
        # 3 CU * 1 microlamport = 3 microlamports -> ceil(3 / 1_000_000) = 1 lamport, not 0.
        tx = _build_tx(
            [WALLET, COMPUTE_BUDGET],
            [_compute_budget_ix(cu_limit=3), _compute_budget_price_ix(1)],
        )
        enforcer.enforce(tx, WALLET, time.monotonic())
        assert enforcer.compute_spend_lamports(tx, WALLET) == 1

    def test_other_compute_budget_instructions_carry_no_spend(self) -> None:
        """RequestHeapFrame (tag=1) and the deprecated RequestUnits (tag=0) carry no
        priced field -- they must not be mistaken for limit/price."""
        enforcer, _ledger = _enforcer()
        heap_frame_ix = sw.RawInstruction(
            program_id=COMPUTE_BUDGET, accounts=(), data=b"\x01" + struct.pack("<I", 32 * 1024)
        )
        tx = _build_tx([WALLET, COMPUTE_BUDGET], [heap_frame_ix])
        enforcer.enforce(tx, WALLET, time.monotonic())
        assert enforcer.compute_spend_lamports(tx, WALLET) == 0

    def test_malformed_price_instruction_refuses_unparseable(self) -> None:
        enforcer, _ledger = _enforcer()
        malformed = sw.RawInstruction(program_id=COMPUTE_BUDGET, accounts=(), data=b"\x03\x00\x00")
        tx = _build_tx([WALLET, COMPUTE_BUDGET], [malformed])
        with pytest.raises(SignerRefused) as exc_info:
            enforcer.enforce(tx, WALLET, time.monotonic())
        assert exc_info.value.reason == "signer_tx_unparseable"

    def test_real_shipped_config_requires_compute_budget_program_id(self) -> None:
        """The boot loader must populate compute_budget_program_id from the real
        shipped allowlist -- an empty value would silently disable this entire check."""
        policy = load_signer_policy(pinned_tip_accounts=frozenset({TIP_1}))
        assert policy.compute_budget_program_id != ""


# ---------------------------------------------------------------------------
# C1 -- off-allowlist program refuses
# ---------------------------------------------------------------------------


class TestOffAllowlistRefuses:
    def test_off_allowlist_program_refuses(self) -> None:
        enforcer, _ledger = _enforcer()
        tx = _build_tx(
            [WALLET, OFF_ALLOWLIST_PROGRAM],
            [sw.RawInstruction(program_id=OFF_ALLOWLIST_PROGRAM, accounts=(), data=b"")],
        )
        with pytest.raises(SignerRefused) as exc_info:
            enforcer.enforce(tx, WALLET, time.monotonic())
        assert exc_info.value.reason == "signer_program_not_allowlisted"

    def test_off_allowlist_checked_before_spend_computation(self) -> None:
        """C1 runs before C2/C3 -- an off-allowlist program refuses even if paired
        with an otherwise-fine, in-budget venue buy."""
        enforcer, _ledger = _enforcer()
        tx = _build_tx(
            [WALLET, VENUE, OFF_ALLOWLIST_PROGRAM],
            [
                _venue_buy_ix(sol_cost=1_000),
                sw.RawInstruction(program_id=OFF_ALLOWLIST_PROGRAM, accounts=(), data=b""),
            ],
        )
        with pytest.raises(SignerRefused) as exc_info:
            enforcer.enforce(tx, WALLET, time.monotonic())
        assert exc_info.value.reason == "signer_program_not_allowlisted"


# ---------------------------------------------------------------------------
# C2 -- unpinned transfer refuses
# ---------------------------------------------------------------------------


class TestUnpinnedTransferRefuses:
    def test_transfer_to_arbitrary_recipient_refuses(self) -> None:
        enforcer, _ledger = _enforcer()
        tx = _build_tx(
            [WALLET, SYSTEM, ATTACKER],
            [_system_transfer_ix(source=0, recipient=2, lamports=1_000)],
        )
        with pytest.raises(SignerRefused) as exc_info:
            enforcer.enforce(tx, WALLET, time.monotonic())
        assert exc_info.value.reason == "signer_unpinned_transfer"

    def test_empty_pinned_tip_set_fails_closed(self) -> None:
        """getTipAccounts unreachable at boot -> pinned set empty -> ALL tip transfers refuse."""
        policy = _make_policy(pinned_tip_accounts=frozenset())
        enforcer, _ledger = _enforcer(policy)
        tx = _build_tx(
            [WALLET, SYSTEM, TIP_1],
            [_system_transfer_ix(source=0, recipient=2, lamports=1_000)],
        )
        with pytest.raises(SignerRefused) as exc_info:
            enforcer.enforce(tx, WALLET, time.monotonic())
        assert exc_info.value.reason == "signer_unpinned_transfer"

    def test_transfer_not_sourced_from_wallet_is_out_of_scope(self) -> None:
        """A System transfer NOT sourced from the wallet is simply not priced/pinned --
        it belongs to some other signer in a (hypothetical) multi-signer tx."""
        enforcer, _ledger = _enforcer()
        someone_else = _pubkey("someone_else")
        tx = _build_tx(
            [WALLET, someone_else, SYSTEM, ATTACKER],
            [_system_transfer_ix(source=1, recipient=3, lamports=1_000)],
        )
        enforcer.enforce(tx, WALLET, time.monotonic())

    def test_own_ata_transfer_is_pinned(self) -> None:
        """C2 rule 2: a System::CreateAccount-with-lamports to the wallet's OWN derived
        ATA for a mint present elsewhere in the tx is pinned (not unpinned-transfer)."""
        enforcer, _ledger = _enforcer()
        ata = derive_ata(WALLET, MINT, ata_program_id=ATA_PROGRAM, spl_token_program_id=SPL_TOKEN)
        create_account_data = struct.pack("<IQQ", 0, 2_039_280, 165) + b"\x00" * 32
        tx = _build_tx(
            [WALLET, SYSTEM, ata, MINT],
            [sw.RawInstruction(program_id=SYSTEM, accounts=(0, 2), data=create_account_data)],
        )
        enforcer.enforce(tx, WALLET, time.monotonic())

    def test_ata_for_a_different_wallet_is_not_pinned(self) -> None:
        enforcer, _ledger = _enforcer()
        other_wallet = _pubkey("other_wallet")
        not_our_ata = derive_ata(
            other_wallet, MINT, ata_program_id=ATA_PROGRAM, spl_token_program_id=SPL_TOKEN
        )
        create_account_data = struct.pack("<IQQ", 0, 2_039_280, 165) + b"\x00" * 32
        tx = _build_tx(
            [WALLET, SYSTEM, not_our_ata, MINT],
            [sw.RawInstruction(program_id=SYSTEM, accounts=(0, 2), data=create_account_data)],
        )
        with pytest.raises(SignerRefused) as exc_info:
            enforcer.enforce(tx, WALLET, time.monotonic())
        assert exc_info.value.reason == "signer_unpinned_transfer"


# ---------------------------------------------------------------------------
# C3 -- undecodable venue spend refuses
# ---------------------------------------------------------------------------


class TestSpendUndecodableRefuses:
    def test_unknown_discriminator_refuses(self) -> None:
        enforcer, _ledger = _enforcer()
        tx = _build_tx(
            [WALLET, VENUE],
            [_venue_buy_ix(sol_cost=1_000, discriminator=b"\xff\xff\xff\xff")],
        )
        with pytest.raises(SignerRefused) as exc_info:
            enforcer.enforce(tx, WALLET, time.monotonic())
        assert exc_info.value.reason == "signer_spend_undecodable"

    def test_venue_program_with_no_registered_decoder_refuses(self) -> None:
        policy = _make_policy(
            allowed_program_ids=frozenset(
                {SYSTEM, COMPUTE_BUDGET, VENUE, OTHER_VENUE_NO_DECODER, ATA_PROGRAM, SPL_TOKEN}
            ),
            venue_program_ids=frozenset({VENUE, OTHER_VENUE_NO_DECODER}),
            # venue_spend_decoders intentionally omits OTHER_VENUE_NO_DECODER
        )
        enforcer, _ledger = _enforcer(policy)
        tx = _build_tx(
            [WALLET, OTHER_VENUE_NO_DECODER],
            [sw.RawInstruction(program_id=OTHER_VENUE_NO_DECODER, accounts=(), data=b"\x00" * 12)],
        )
        with pytest.raises(SignerRefused) as exc_info:
            enforcer.enforce(tx, WALLET, time.monotonic())
        assert exc_info.value.reason == "signer_spend_undecodable"

    def test_real_shipped_config_venues_are_unverified_and_fail_closed(self) -> None:
        """config/signer-policy.json ships every venue_spend_decoders entry as an
        UNVERIFIED VERIFY_AT_BUILD placeholder -- any buy on any live venue MUST refuse
        until the discriminators are transcribed from real IDLs (architect's recorded risk)."""
        policy = load_signer_policy(pinned_tip_accounts=frozenset({TIP_1}))
        assert policy.venue_program_ids, "expected at least one active venue program"
        for venue_pid in policy.venue_program_ids:
            decoder = policy.venue_spend_decoders[venue_pid]
            with pytest.raises(ValueError):
                decoder.max_sol_lamports(b"\x00" * 12)


# ---------------------------------------------------------------------------
# C4 -- rolling aggregate + velocity cap
# ---------------------------------------------------------------------------


class TestAggregateAndVelocityRefuses:
    def test_aggregate_cap_exceeded_refuses(self) -> None:
        policy = _make_policy(max_sign_count=1000)  # isolate the aggregate check
        enforcer, ledger = _enforcer(policy)
        now = time.monotonic()

        # Each individual sign must itself stay within the PER-TX cap (0.1 SOL) --
        # accumulate spend across several sub-cap signs to approach the aggregate cap.
        for _ in range(4):
            tx = _build_tx([WALLET, VENUE], [_venue_buy_ix(sol_cost=_PER_TX_CAP)])
            enforcer.enforce(tx, WALLET, now)
            ledger.record(now, _PER_TX_CAP)
        extra = _build_tx([WALLET, VENUE], [_venue_buy_ix(sol_cost=50_000_000)])
        enforcer.enforce(extra, WALLET, now)
        ledger.record(now, 50_000_000)
        # window_spend so far == 450_000_000; headroom == 50_000_000.

        over_tx = _build_tx([WALLET, VENUE], [_venue_buy_ix(sol_cost=60_000_000)])
        with pytest.raises(SignerRefused) as exc_info:
            enforcer.enforce(over_tx, WALLET, now)
        assert exc_info.value.reason == "signer_aggregate_cap_exceeded"

    def test_velocity_exceeded_refuses(self) -> None:
        policy = _make_policy(max_sign_count=3, aggregate_cap_lamports=_AGG_CAP)
        enforcer, ledger = _enforcer(policy)
        now = time.monotonic()
        tx = _build_tx([WALLET, VENUE], [_venue_buy_ix(sol_cost=1_000)])

        for _ in range(3):
            enforcer.enforce(tx, WALLET, now)
            ledger.record(now, 1_000)

        with pytest.raises(SignerRefused) as exc_info:
            enforcer.enforce(tx, WALLET, now)
        assert exc_info.value.reason == "signer_velocity_exceeded"

    def test_window_expiry_resets_the_cap(self) -> None:
        policy = _make_policy(window_seconds=10, max_sign_count=1)
        enforcer, ledger = _enforcer(policy)
        tx = _build_tx([WALLET, VENUE], [_venue_buy_ix(sol_cost=1_000)])

        t0 = 1_000.0
        enforcer.enforce(tx, WALLET, t0)
        ledger.record(t0, 1_000)

        # Still within the window -> velocity refuses.
        with pytest.raises(SignerRefused) as exc_info:
            enforcer.enforce(tx, WALLET, t0 + 5)
        assert exc_info.value.reason == "signer_velocity_exceeded"

        # Past the window -> the old entry is pruned, signs again.
        enforcer.enforce(tx, WALLET, t0 + 11)

    def test_would_exceed_is_read_only(self) -> None:
        ledger = VelocityLedger(window_seconds=60)
        now = 100.0
        for _ in range(5):
            violation = ledger.would_exceed(now, 1_000, aggregate_cap_lamports=10_000, max_sign_count=100)
            assert violation is None
        # No record() call was made -- a fresh ledger check at the same instant must be identical.
        assert ledger.would_exceed(now, 1_000, aggregate_cap_lamports=10_000, max_sign_count=100) is None

    def test_refusal_does_not_mutate_the_ledger(self) -> None:
        """A refused enforce() call must NOT record anything -- prove that recording
        legitimate sub-cap signs up to EXACTLY the aggregate cap still succeeds. If the
        earlier over-cap refusal had (incorrectly) also been recorded, the final sign
        below would breach C4 and refuse."""
        policy = _make_policy(max_sign_count=1000)
        enforcer, ledger = _enforcer(policy)
        now = time.monotonic()

        # This refuses (over per-tx cap) -- must NOT touch the ledger.
        over_cap_tx = _build_tx([WALLET, VENUE], [_venue_buy_ix(sol_cost=_PER_TX_CAP + 1)])
        with pytest.raises(SignerRefused):
            enforcer.enforce(over_cap_tx, WALLET, now)

        # Five sub-cap signs totaling EXACTLY the aggregate cap must all sign cleanly.
        for _ in range(5):
            tx = _build_tx([WALLET, VENUE], [_venue_buy_ix(sol_cost=_PER_TX_CAP)])
            enforcer.enforce(tx, WALLET, now)
            ledger.record(now, _PER_TX_CAP)


# ---------------------------------------------------------------------------
# C0 / C0a -- parse + ALT-region refusals
# ---------------------------------------------------------------------------


class TestPayerMismatchRefuses:
    """Fix round 2 MINOR finding: ADR-0015 C0 states "the payer (account 0 = the
    wallet)" -- enforce() must refuse a tx whose account_keys[0] is not the
    wallet_pubkey passed in, rather than silently letting it fall through to every
    other check (and eventually an invalid on-chain signature)."""

    def test_payer_not_wallet_refuses(self) -> None:
        enforcer, _ledger = _enforcer()
        someone_else = _pubkey("someone_else")
        tx = _build_tx(
            [someone_else, SYSTEM, TIP_1],
            [_system_transfer_ix(source=0, recipient=2, lamports=1_000)],
        )
        with pytest.raises(SignerRefused) as exc_info:
            enforcer.enforce(tx, WALLET, time.monotonic())
        assert exc_info.value.reason == "signer_payer_mismatch"

    def test_payer_matches_wallet_signs(self) -> None:
        """Sanity: the WALLET-is-payer case (all other tests) is unaffected."""
        enforcer, _ledger = _enforcer()
        tx = _build_tx(
            [WALLET, SYSTEM, TIP_1],
            [_system_transfer_ix(source=0, recipient=2, lamports=1_000)],
        )
        enforcer.enforce(tx, WALLET, time.monotonic())

    def test_payer_mismatch_checked_before_c0a_and_c1(self) -> None:
        """Payer mismatch refuses even paired with an ALT-lookup tx or an
        off-allowlist program -- proves it runs early (as part of C0), not after
        other checks have already had a chance to refuse for a different reason
        (both would technically refuse either way, but the reason code must be
        the payer mismatch to be diagnostically useful)."""
        enforcer, _ledger = _enforcer()
        someone_else = _pubkey("someone_else")
        tx = _build_tx([someone_else, SYSTEM], [], n_address_table_lookups=1)
        with pytest.raises(SignerRefused) as exc_info:
            enforcer.enforce(tx, WALLET, time.monotonic())
        assert exc_info.value.reason == "signer_payer_mismatch"


class TestOwnAtaFastPathAndMemoization:
    """Fix round 2 MAJOR finding: `_try_derive_ata` prefers solders' native PDA
    search when available and Enforcer memoizes (wallet, mint) -> ata across calls.
    Both must produce results IDENTICAL to the pure-Python `derive_ata()` reference
    -- the verdict never changes, only which implementation/cache path computed it.
    """

    def test_native_path_is_preferred_when_solders_available_and_matches_pure_python(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fakes a minimal solders.pubkey.Pubkey surface backed by the SAME
        low-level PDA primitives (`_create_program_address`), reached through a
        DIFFERENT call surface (from_string/bytes/find_program_address) than
        `derive_ata()` uses directly -- proving the routing/glue code
        (base58<->bytes conversions, seed order, return-value unwrapping) is
        correct, not just that "the same function was called twice"."""
        import aats.execution.signer_enforcer as se

        calls: list[str] = []

        class _FakePubkey:
            def __init__(self, raw: bytes) -> None:
                self._raw = raw

            @classmethod
            def from_string(cls, s: str) -> _FakePubkey:
                return cls(sw.b58decode(s))

            def __bytes__(self) -> bytes:
                return self._raw

            def __str__(self) -> str:
                return sw.b58encode(self._raw)

            @staticmethod
            def find_program_address(seeds: list[bytes], program_id: _FakePubkey) -> tuple:
                calls.append("native_find_program_address")
                addr, bump = se._find_program_address(list(seeds), bytes(program_id))
                return _FakePubkey(addr), bump

        monkeypatch.setattr(se, "_SOLDERS_PROBED", True)
        monkeypatch.setattr(se, "_SOLDERS_PUBKEY_CLS", _FakePubkey)

        pure_python_result = derive_ata(
            WALLET, MINT, ata_program_id=ATA_PROGRAM, spl_token_program_id=SPL_TOKEN
        )
        native_result = se._try_derive_ata(
            WALLET, MINT, ata_program_id=ATA_PROGRAM, spl_token_program_id=SPL_TOKEN
        )
        assert native_result == pure_python_result
        assert calls == ["native_find_program_address"], "the fake native path must have run"

    def test_own_ata_transfer_still_pinned_when_solders_available(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """End-to-end: with the fake native backend wired in, the existing C2
        own-ATA-pin behavior is unchanged (Enforcer._is_own_ata routes through it
        transparently)."""
        import aats.execution.signer_enforcer as se

        class _FakePubkey:
            def __init__(self, raw: bytes) -> None:
                self._raw = raw

            @classmethod
            def from_string(cls, s: str) -> _FakePubkey:
                return cls(sw.b58decode(s))

            def __bytes__(self) -> bytes:
                return self._raw

            def __str__(self) -> str:
                return sw.b58encode(self._raw)

            @staticmethod
            def find_program_address(seeds: list[bytes], program_id: _FakePubkey) -> tuple:
                addr, bump = se._find_program_address(list(seeds), bytes(program_id))
                return _FakePubkey(addr), bump

        monkeypatch.setattr(se, "_SOLDERS_PROBED", True)
        monkeypatch.setattr(se, "_SOLDERS_PUBKEY_CLS", _FakePubkey)

        enforcer, _ledger = _enforcer()
        ata = derive_ata(WALLET, MINT, ata_program_id=ATA_PROGRAM, spl_token_program_id=SPL_TOKEN)
        create_account_data = struct.pack("<IQQ", 0, 2_039_280, 165) + b"\x00" * 32
        tx = _build_tx(
            [WALLET, SYSTEM, ata, MINT],
            [sw.RawInstruction(program_id=SYSTEM, accounts=(0, 2), data=create_account_data)],
        )
        enforcer.enforce(tx, WALLET, time.monotonic())  # must NOT raise

    def test_unavailable_solders_falls_back_to_pure_python(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Explicitly force the "not importable" path and prove _try_derive_ata still
        returns the correct (pure-Python-computed) answer -- this is also the REAL
        code path exercised by every other test in this suite (solders is not
        installed in this sandbox), made explicit here."""
        import aats.execution.signer_enforcer as se

        monkeypatch.setattr(se, "_SOLDERS_PROBED", True)
        monkeypatch.setattr(se, "_SOLDERS_PUBKEY_CLS", None)

        pure_python_result = derive_ata(
            WALLET, MINT, ata_program_id=ATA_PROGRAM, spl_token_program_id=SPL_TOKEN
        )
        result = se._try_derive_ata(
            WALLET, MINT, ata_program_id=ATA_PROGRAM, spl_token_program_id=SPL_TOKEN
        )
        assert result == pure_python_result

    def test_invalid_candidate_mint_returns_none_not_raise(self) -> None:
        import aats.execution.signer_enforcer as se

        result = se._try_derive_ata(
            WALLET, "not-a-valid-pubkey", ata_program_id=ATA_PROGRAM, spl_token_program_id=SPL_TOKEN
        )
        assert result is None

    def test_ata_derivation_is_memoized_across_enforce_calls(self) -> None:
        """The SAME (wallet, mint) pair derived across TWO DIFFERENT enforce() calls
        (different tx_bytes -- the price_cache from fix round 1 cannot help here)
        must hit Enforcer._ata_cache on the second call, not recompute."""
        enforcer, _ledger = _enforcer()
        ata = derive_ata(WALLET, MINT, ata_program_id=ATA_PROGRAM, spl_token_program_id=SPL_TOKEN)
        create_account_data = struct.pack("<IQQ", 0, 2_039_280, 165) + b"\x00" * 32

        def _tx(bh: str) -> bytes:
            return sw.build_versioned_tx(
                account_keys=[WALLET, SYSTEM, ata, MINT],
                num_required_signatures=1,
                recent_blockhash=bh,
                instructions=[
                    sw.RawInstruction(program_id=SYSTEM, accounts=(0, 2), data=create_account_data)
                ],
            )

        enforcer.enforce(_tx(_pubkey("bh1")), WALLET, time.monotonic())
        assert (WALLET, MINT) in enforcer._ata_cache
        cached_value = enforcer._ata_cache[(WALLET, MINT)]
        assert cached_value == ata

        # Second, DIFFERENT tx (new blockhash -- simulates a blockhash-expiry retry
        # rebuild) with the SAME wallet/mint must still sign, using the cache.
        enforcer.enforce(_tx(_pubkey("bh2")), WALLET, time.monotonic())
        # Cache entry is unchanged (same value, proving no corruption/recompute drift).
        assert enforcer._ata_cache[(WALLET, MINT)] == cached_value

    def test_ata_cache_bounded_resets_rather_than_grows_unbounded(self) -> None:
        import aats.execution.signer_enforcer as se

        enforcer, _ledger = _enforcer()
        # Directly populate past the bound -- cheaper than driving _ATA_CACHE_MAX_ENTRIES
        # distinct real enforce() calls, and exercises the exact same eviction branch.
        for i in range(se._ATA_CACHE_MAX_ENTRIES):
            enforcer._ata_cache[(WALLET, f"filler-{i}")] = None
        assert len(enforcer._ata_cache) == se._ATA_CACHE_MAX_ENTRIES

        # One more lookup must trigger the bound check and clear rather than grow past it.
        enforcer._is_own_ata("irrelevant-recipient", WALLET, (MINT,))
        assert len(enforcer._ata_cache) <= se._ATA_CACHE_MAX_ENTRIES


class TestParseAndAltRefusals:
    def test_garbage_bytes_unparseable(self) -> None:
        enforcer, _ledger = _enforcer()
        with pytest.raises(SignerRefused) as exc_info:
            enforcer.enforce(b"\xff\xff\xff not a real tx", WALLET, time.monotonic())
        assert exc_info.value.reason == "signer_tx_unparseable"

    def test_empty_bytes_unparseable(self) -> None:
        enforcer, _ledger = _enforcer()
        with pytest.raises(SignerRefused) as exc_info:
            enforcer.enforce(b"", WALLET, time.monotonic())
        assert exc_info.value.reason == "signer_tx_unparseable"

    def test_alt_referencing_tx_refuses(self) -> None:
        enforcer, _ledger = _enforcer()
        tx = _build_tx([WALLET, SYSTEM], [], n_address_table_lookups=1)
        with pytest.raises(SignerRefused) as exc_info:
            enforcer.enforce(tx, WALLET, time.monotonic())
        assert exc_info.value.reason == "signer_alt_unresolvable_security_account"

    def test_out_of_range_program_id_index_is_treated_as_alt(self) -> None:
        enforcer, _ledger = _enforcer()
        tx = _build_tx(
            [WALLET, SYSTEM],
            [sw.RawInstruction(program_id_index_override=250, accounts=(), data=b"")],
        )
        with pytest.raises(SignerRefused) as exc_info:
            enforcer.enforce(tx, WALLET, time.monotonic())
        assert exc_info.value.reason == "signer_alt_unresolvable_security_account"


# ---------------------------------------------------------------------------
# C5 -- unbounded approval hygiene
# ---------------------------------------------------------------------------


class TestApprovalHygiene:
    _APPROVE_TAG = 4
    _APPROVE_CHECKED_TAG = 13

    def _approve_ix(self, *, amount: int, delegate_index: int = 1, tag: int | None = None) -> sw.RawInstruction:
        tag = self._APPROVE_TAG if tag is None else tag
        data = bytes([tag]) + struct.pack("<Q", amount)
        return sw.RawInstruction(program_id=SPL_TOKEN, accounts=(0, delegate_index, 2), data=data)

    def test_unbounded_approval_to_unknown_delegate_refuses(self) -> None:
        enforcer, _ledger = _enforcer()
        tx = _build_tx(
            [WALLET, ATTACKER, WALLET, SPL_TOKEN],
            [self._approve_ix(amount=2**64 - 1)],
        )
        with pytest.raises(SignerRefused) as exc_info:
            enforcer.enforce(tx, WALLET, time.monotonic())
        assert exc_info.value.reason == "signer_unbounded_approval"

    def test_unbounded_approval_to_known_venue_delegate_signs(self) -> None:
        enforcer, _ledger = _enforcer()
        tx = _build_tx(
            [WALLET, VENUE, WALLET, SPL_TOKEN],
            [self._approve_ix(amount=2**64 - 1)],
        )
        enforcer.enforce(tx, WALLET, time.monotonic())

    def test_small_approval_to_unknown_delegate_signs(self) -> None:
        enforcer, _ledger = _enforcer()
        tx = _build_tx(
            [WALLET, ATTACKER, WALLET, SPL_TOKEN],
            [self._approve_ix(amount=100)],
        )
        enforcer.enforce(tx, WALLET, time.monotonic())

    def test_approve_checked_unbounded_refuses(self) -> None:
        enforcer, _ledger = _enforcer()
        tx = _build_tx(
            [WALLET, MINT, ATTACKER, WALLET, SPL_TOKEN],
            [self._approve_ix(amount=2**64 - 1, delegate_index=2, tag=self._APPROVE_CHECKED_TAG)],
        )
        with pytest.raises(SignerRefused) as exc_info:
            enforcer.enforce(tx, WALLET, time.monotonic())
        assert exc_info.value.reason == "signer_unbounded_approval"


# ---------------------------------------------------------------------------
# Structural proof: enforce() cannot be lied to via caller metadata --
# its signature has NO sol_in/intent_kind/mint parameters at all.
# ---------------------------------------------------------------------------


class TestNoRoomForCallerMetadata:
    def test_enforce_signature_has_only_tx_bytes_wallet_and_clock(self) -> None:
        import inspect

        sig = inspect.signature(Enforcer.enforce)
        params = list(sig.parameters)
        assert params == ["self", "tx_bytes", "wallet_pubkey", "now_s"], (
            "enforce() must re-derive every policy fact from tx_bytes alone -- there is "
            "structurally no parameter through which a caller could pass sol_in / "
            "intent_kind / mint to influence the verdict (ADR-0015 trust model)."
        )


# ---------------------------------------------------------------------------
# Boot-loader against the REAL shipped config files
# ---------------------------------------------------------------------------


class TestLoadSignerPolicyRealConfig:
    def test_loads_and_admits_expected_core_and_active_venue_programs(self) -> None:
        policy = load_signer_policy(pinned_tip_accounts=frozenset({TIP_1}))
        assert policy.per_tx_cap_lamports == 100_000_000
        assert policy.aggregate_cap_lamports == 500_000_000
        assert policy.window_seconds == 60
        assert policy.max_sign_count == 10
        # Candidate-status venues (meteora, moonshot) and the candidate-flagged
        # spl_token_2022_program must NOT be admitted to the live set.
        # (We can't check by name here -- only program_id -- so assert counts are sane.)
        assert len(policy.allowed_program_ids) >= 8  # core(4) + active venues(4) + router(1)
        assert len(policy.venue_program_ids) >= 4

    def test_pinned_tip_accounts_pass_through_unmodified(self) -> None:
        accounts = frozenset({TIP_1, TIP_2})
        policy = load_signer_policy(pinned_tip_accounts=accounts)
        assert policy.pinned_tip_accounts == accounts

    def test_empty_tip_accounts_on_fetch_failure_is_fail_closed(self) -> None:
        policy = load_signer_policy(pinned_tip_accounts=frozenset())
        assert policy.pinned_tip_accounts == frozenset()

    def test_missing_allowlist_file_raises(self, tmp_path: object) -> None:
        with pytest.raises(FileNotFoundError):
            load_signer_policy(
                allowlist_path="/nonexistent/allowlist.json",
                pinned_tip_accounts=frozenset(),
            )
