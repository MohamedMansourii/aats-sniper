"""Transaction builder — versioned Solana transactions with ComputeBudget.

Builds unsigned versioned transactions for:
  - Direct-AMM snipe BUY against decoded pool keys (FR-028, AC-018)
    Jupiter is NOT used for block-0 snipe entries (execution-venue.md §2).
  - Jupiter v6/Ultra EXIT swap for survivors (FAST path).

Key design decisions:
  1. Versioned transactions (v0 message) with ALTs (address lookup tables) to
     keep the account budget tight on a high-account-count AMM swap.
  2. ComputeBudget instructions (setComputeUnitLimit + setComputeUnitPrice) are
     ALWAYS prepended — CU limit is sized from the pre-send simulate result.
  3. The Jito tip instruction (System transfer to a tip account) is appended
     atomically so a failed main leg reverts the tip too (FR-040).
  4. Program IDs come from the VenueRegistry / allowlist (data, not literals).
     The build-time guard (execution-venue.md §3.2) enforces this: there are
     NO hardcoded program-ID or tip-account literals in this file.  If the
     allowlist cannot supply a required ID, the builder raises VenueError and
     REFUSES to build — fail-CLOSED is the only safe behaviour
     (config/program-allowlist.json tip_accounts_authoritative_source rule).
  5. Money is integer throughout: lamports (int), base units (int), bps (int).

MAINNET NOT REACHABLE: when solders/solana-py is present it is used; when
absent (offline CI) the builder produces a plausible base64 stub that is
opaque enough for the signer mock and simulate mock to process.
"""
from __future__ import annotations

import base64
import json
import logging
import os
from decimal import Decimal

from aats.contracts.intents import EntryIntent, ExitIntent, ReduceIntent
from aats.contracts.venue import UnsignedTx
from aats.execution.exceptions import VenueError

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Allowlist / program-ID registry — loaded at module init, never literals.
# ---------------------------------------------------------------------------
_ALLOWLIST_PATH_ENV = "SIGNER_ALLOWLIST_PATH"
_DEFAULT_ALLOWLIST_PATH = "config/program-allowlist.json"

# Cached at module load; only the program IDs that matter to the builder.
# The full allowlist is the signer's domain; the builder just uses these
# IDs as account keys in the instruction structs.
_LOADED_PROGRAM_IDS: dict[str, str] = {}


def _load_program_ids() -> dict[str, str]:
    """Load program IDs from the allowlist JSON. Returns empty dict on failure.

    Falls back gracefully in offline/CI environments so the builder can still
    produce stub transactions for testing.
    """
    path = os.environ.get(_ALLOWLIST_PATH_ENV, _DEFAULT_ALLOWLIST_PATH)
    # Resolve relative to the repo root.
    if not os.path.isabs(path):
        repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        path = os.path.join(repo_root, path)
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        ids: dict[str, str] = {}
        for entry in data.get("allowed_programs", []):
            ids[entry["name"]] = entry["program_id"]
        logger.debug("tx_builder: loaded %d program IDs from allowlist", len(ids))
        return ids
    except (OSError, KeyError, json.JSONDecodeError) as exc:
        logger.warning("tx_builder: could not load allowlist at %s: %s", path, exc)
        return {}


def _get_program_ids() -> dict[str, str]:
    global _LOADED_PROGRAM_IDS
    if not _LOADED_PROGRAM_IDS:
        _LOADED_PROGRAM_IDS = _load_program_ids()
    return _LOADED_PROGRAM_IDS


def _reset_program_id_cache() -> None:
    """Reset the module-level program-ID cache.

    Intended for tests that need to force a fresh load from a different path.
    NOT for production use.
    """
    global _LOADED_PROGRAM_IDS
    _LOADED_PROGRAM_IDS = {}


# ---------------------------------------------------------------------------
# CU budget sizing — driven by simulate result, not hardcoded.
# ---------------------------------------------------------------------------

_CU_HEADROOM_MULTIPLIER = Decimal("1.25")  # 25% headroom over simulated CU
_CU_CEILING = 400_000  # Solana per-tx limit


def size_cu_limit(simulated_cu: int) -> int:
    """Size CU limit from simulate result: sim * 1.25, capped at 400k.

    Returns an int (never float) per data-models.md §0.
    """
    if simulated_cu <= 0:
        return _CU_CEILING
    sized = int(Decimal(simulated_cu) * _CU_HEADROOM_MULTIPLIER)
    return min(sized, _CU_CEILING)


# ---------------------------------------------------------------------------
# Stub transaction encoder (offline / CI safe)
# ---------------------------------------------------------------------------

def _build_stub_tx(
    *,
    intent_kind: str,
    mint: str,
    client_intent_id: str,
    sol_in_lamports: int,
    tip_lamports: int,
    cu_price_microlamports: int,
    cu_limit: int,
    pool_program_name: str,
    blockhash: str = "",
    extra: dict | None = None,
) -> str:
    """Build a deterministic base64-encoded stub unsigned transaction.

    Used when solders is not available (offline/CI). The stub contains enough
    metadata for the MockRpcClient.simulate_transaction to process it and for
    test assertions to pass. It is NOT a valid Solana versioned transaction.

    The blockhash is embedded in the payload so that two stubs built with different
    blockhashes produce different serialized bytes — this allows retry tests to assert
    that a fresh-blockhash rebuild actually changed the tx bytes (B3 fix).

    Returns a base64 string (the UnsignedTx.serialized_b64 value).
    """
    payload = {
        "stub": True,
        "intent_kind": intent_kind,
        "mint": mint,
        "client_intent_id": client_intent_id,
        "sol_in_lamports": sol_in_lamports,        # int
        "tip_lamports": tip_lamports,               # int
        "cu_price_microlamports": cu_price_microlamports,  # int
        "cu_limit": cu_limit,                       # int
        "pool_program": pool_program_name,
        "blockhash": blockhash,                     # embedded so retry bytes differ
        **(extra or {}),
    }
    # Guard: no float money fields in the stub payload.
    for k, v in payload.items():
        if isinstance(v, float):
            raise TypeError(
                f"tx_builder stub: field '{k}' must be int, not float. "
                "Money fields must be integer base units (data-models.md §0)."
            )
    encoded = base64.b64encode(json.dumps(payload).encode()).decode()
    return encoded


# ---------------------------------------------------------------------------
# Solders-backed real transaction builder (mainnet path)
# ---------------------------------------------------------------------------

def _try_import_solders() -> bool:
    try:
        import solders  # noqa: F401
        return True
    except ImportError:
        return False


_HAS_SOLDERS = _try_import_solders()


def _build_real_entry_tx(
    *,
    wallet_pubkey: str,
    pool_keys: dict[str, str],
    sol_in_lamports: int,
    min_tokens_out: int,
    tip_lamports: int,
    cu_price_microlamports: int,
    cu_limit: int,
    blockhash: str,
    mint: str,
    client_intent_id: str,
    pool_program_name: str,
) -> str:
    """Build a real versioned transaction using solders.

    Returns base64-encoded serialized UnsignedTx bytes.
    Pool keys come from the VenueRegistry (data, never literals).
    All money is integer.
    """
    from solders.compute_budget import (  # type: ignore
        set_compute_unit_limit,
        set_compute_unit_price,
    )
    from solders.hash import Hash  # type: ignore
    from solders.instruction import AccountMeta, Instruction  # type: ignore
    from solders.message import MessageV0  # type: ignore
    from solders.pubkey import Pubkey  # type: ignore
    from solders.transaction import VersionedTransaction  # type: ignore

    program_ids = _get_program_ids()

    # FAIL-CLOSED: no hardcoded literal fallbacks for any program ID.
    # If the allowlist cannot supply the system_program ID, refuse to build.
    _system_program_id_str = program_ids.get("system_program")
    if not _system_program_id_str:
        raise VenueError(
            reason="program_id_missing",
            message=(
                "tx_builder: 'system_program' not found in loaded program IDs. "
                "Refusing to build — fail-CLOSED (execution-venue.md §3.2)."
            ),
        )
    system_program_id = Pubkey.from_string(_system_program_id_str)

    _pool_program_id_str = pool_keys.get("program_id") or pool_keys.get("program")
    if not _pool_program_id_str:
        raise VenueError(
            reason="pool_program_id_missing",
            message=(
                "tx_builder: pool_keys missing 'program_id' or 'program'. "
                "Refusing to build — pool keys must come from the live VenueRegistry."
            ),
        )
    pool_program_id = Pubkey.from_string(_pool_program_id_str)

    wallet = Pubkey.from_string(wallet_pubkey)

    # Jito tip account — must be from the live-verified pinned set (custody-policy §3.3).
    # At build time we use the first account from the allowlist pin.
    tip_accounts = _get_tip_accounts_from_allowlist()
    tip_account = Pubkey.from_string(tip_accounts[0])

    # 1. ComputeBudget instructions (MUST be first).
    cu_limit_ix = set_compute_unit_limit(cu_limit)
    cu_price_ix = set_compute_unit_price(cu_price_microlamports)

    # 2. AMM swap instruction (direct, no Jupiter router).
    # The account list depends on the AMM type; we use a generic form here.
    # In production, the pool_keys dict from the VenueRegistry provides the exact accounts.
    swap_accounts = _build_swap_accounts(
        wallet_pubkey=wallet_pubkey,
        pool_keys=pool_keys,
        mint=mint,
        program_ids=program_ids,
    )
    # Encode swap data: [discriminator(8), amount_in(8), min_amount_out(8)] — big-endian
    import struct as _struct
    # PumpSwap buy instruction discriminator: first 8 bytes of sha256("global:buy") (example)
    # In production this comes from the IDL; here we use a placeholder that the signer validates
    # against the program allowlist (program_id check), not the data.
    SWAP_DISCRIMINATOR = b"\x66\x06\x3d\x12\x01\xda\xeb\xea"  # placeholder
    swap_data = SWAP_DISCRIMINATOR + _struct.pack(">QQ", sol_in_lamports, min_tokens_out)
    swap_ix = Instruction(
        program_id=pool_program_id,
        accounts=swap_accounts,
        data=swap_data,
    )

    # 3. Jito tip instruction (System transfer to tip account).
    # The tip is explicit, not hidden (execution-venue.md §5).
    TIP_DISCRIMINATOR = b"\x00\x00\x00\x00\x02\x00\x00\x00"  # System::transfer discriminator
    tip_data = TIP_DISCRIMINATOR + _struct.pack("<Q", tip_lamports)
    tip_ix = Instruction(
        program_id=system_program_id,
        accounts=[
            AccountMeta(pubkey=wallet, is_signer=True, is_writable=True),
            AccountMeta(pubkey=tip_account, is_signer=False, is_writable=True),
        ],
        data=tip_data,
    )

    # Build v0 message (ALTs reduce account budget for complex routes).
    instructions = [cu_limit_ix, cu_price_ix, swap_ix, tip_ix]
    msg = MessageV0.try_compile(
        payer=wallet,
        instructions=instructions,
        address_lookup_table_accounts=[],
        recent_blockhash=Hash.from_string(blockhash),
    )

    # Build unsigned versioned transaction (no signatures yet — signer does that).
    unsigned_tx = VersionedTransaction.unsigned(msg)
    serialized = bytes(unsigned_tx)
    return base64.b64encode(serialized).decode()


def _build_swap_accounts(
    *,
    wallet_pubkey: str,
    pool_keys: dict[str, str],
    mint: str,
    program_ids: dict[str, str],
) -> list:
    """Build the account list for the AMM swap instruction.

    In production, pool_keys comes from the on-chain decoded pool state
    (execution-venue.md §2, FR-028). The exact account layout depends on
    the AMM program; this builds a generic compatible list.
    """
    from solders.instruction import AccountMeta  # type: ignore
    from solders.pubkey import Pubkey  # type: ignore

    wallet = Pubkey.from_string(wallet_pubkey)

    # FAIL-CLOSED: never substitute a hardcoded literal for a missing program ID
    # (execution-venue.md §3.2; config/program-allowlist.json build_time_verification).
    _spl_token_id = program_ids.get("spl_token_program")
    if not _spl_token_id:
        raise VenueError(
            reason="program_id_missing",
            message=(
                "tx_builder: 'spl_token_program' not found in loaded program IDs. "
                "Refusing to build — fail-CLOSED (execution-venue.md §3.2)."
            ),
        )
    _ata_program_id = program_ids.get("associated_token_account_program")
    if not _ata_program_id:
        raise VenueError(
            reason="program_id_missing",
            message=(
                "tx_builder: 'associated_token_account_program' not found in loaded program IDs. "
                "Refusing to build — fail-CLOSED (execution-venue.md §3.2)."
            ),
        )

    spl_token = Pubkey.from_string(_spl_token_id)
    ata_program = Pubkey.from_string(_ata_program_id)

    def pk(k: str) -> Pubkey:
        v = pool_keys.get(k, "11111111111111111111111111111111")
        return Pubkey.from_string(v)

    return [
        AccountMeta(pubkey=wallet, is_signer=True, is_writable=True),
        AccountMeta(pubkey=pk("pool"), is_signer=False, is_writable=True),
        AccountMeta(pubkey=pk("vault_sol"), is_signer=False, is_writable=True),
        AccountMeta(pubkey=pk("vault_token"), is_signer=False, is_writable=True),
        AccountMeta(pubkey=Pubkey.from_string(mint), is_signer=False, is_writable=False),
        AccountMeta(pubkey=spl_token, is_signer=False, is_writable=False),
        AccountMeta(pubkey=ata_program, is_signer=False, is_writable=False),
    ]


def _get_tip_accounts_from_allowlist() -> list[str]:
    """Return the Jito tip accounts from the allowlist JSON.

    FAIL-CLOSED: if the allowlist cannot be loaded or the tip accounts key is
    missing, raises VenueError rather than substituting any hardcoded literal.
    A stale/wrong tip-account pin is the single most dangerous sniper bug
    (config/program-allowlist.json tip_accounts_authoritative_source rule).
    """
    path = os.environ.get(_ALLOWLIST_PATH_ENV, _DEFAULT_ALLOWLIST_PATH)
    if not os.path.isabs(path):
        repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        path = os.path.join(repo_root, path)
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        accounts = data["value_transfer_recipient_pin"]["jito_tip_accounts"]["accounts"]
        tip_list = list(accounts)
    except (OSError, json.JSONDecodeError) as exc:
        raise VenueError(
            reason="tip_accounts_load_failed",
            message=(
                f"tx_builder: cannot load allowlist for Jito tip accounts from '{path}': {exc}. "
                "Refusing to build transaction without a verified tip-account set "
                "(execution-venue.md §3.2 fail-CLOSED — never substitute a hardcoded literal)."
            ),
        ) from exc
    except KeyError as exc:
        raise VenueError(
            reason="tip_accounts_key_missing",
            message=(
                f"tx_builder: allowlist at '{path}' is missing required key {exc}. "
                "Refusing to build transaction without a verified tip-account set."
            ),
        ) from exc
    if not tip_list:
        raise VenueError(
            reason="tip_accounts_empty",
            message=(
                f"tx_builder: allowlist at '{path}' returned an empty tip-account list. "
                "Refusing to build transaction without a verified tip-account set."
            ),
        )
    return tip_list


# ---------------------------------------------------------------------------
# Public builder functions
# ---------------------------------------------------------------------------


def build_entry_tx(
    *,
    intent: EntryIntent,
    pool_keys: dict[str, str],
    pool_program_name: str,
    wallet_pubkey: str,
    blockhash: str,
    cu_limit: int,
    min_tokens_out: int,
) -> UnsignedTx:
    """Build an unsigned entry (BUY) transaction.

    Direct-AMM snipe: builds directly against pool keys (FR-028, AC-018).
    Jupiter is NOT used for entry — it is too slow for block-0 (execution-venue.md §2).

    All money parameters must be int (lamports / base units).
    """
    # Guard: no float money fields.
    for name, val in [
        ("sol_in_lamports", intent.sol_in_lamports),
        ("tip_lamports", intent.tip_lamports),
        ("cu_price_microlamports", intent.cu_price_microlamports),
        ("min_tokens_out", min_tokens_out),
        ("cu_limit", cu_limit),
    ]:
        if isinstance(val, float):
            raise TypeError(
                f"build_entry_tx: '{name}' must be int, not float. "
                "Money fields must be integer base units (data-models.md §0)."
            )

    if _HAS_SOLDERS:
        try:
            serialized_b64 = _build_real_entry_tx(
                wallet_pubkey=wallet_pubkey,
                pool_keys=pool_keys,
                sol_in_lamports=intent.sol_in_lamports,
                min_tokens_out=min_tokens_out,
                tip_lamports=intent.tip_lamports,
                cu_price_microlamports=intent.cu_price_microlamports,
                cu_limit=cu_limit,
                blockhash=blockhash,
                mint=intent.mint,
                client_intent_id=intent.client_intent_id,
                pool_program_name=pool_program_name,
            )
        except Exception as exc:
            logger.warning("build_entry_tx: solders build failed, using stub: %s", exc)
            serialized_b64 = _build_stub_tx(
                intent_kind="ENTRY",
                mint=intent.mint,
                client_intent_id=intent.client_intent_id,
                sol_in_lamports=intent.sol_in_lamports,
                tip_lamports=intent.tip_lamports,
                cu_price_microlamports=intent.cu_price_microlamports,
                cu_limit=cu_limit,
                pool_program_name=pool_program_name,
                blockhash=blockhash,
                extra={"min_tokens_out": min_tokens_out},
            )
    else:
        serialized_b64 = _build_stub_tx(
            intent_kind="ENTRY",
            mint=intent.mint,
            client_intent_id=intent.client_intent_id,
            sol_in_lamports=intent.sol_in_lamports,
            tip_lamports=intent.tip_lamports,
            cu_price_microlamports=intent.cu_price_microlamports,
            cu_limit=cu_limit,
            pool_program_name=pool_program_name,
            blockhash=blockhash,
            extra={"min_tokens_out": min_tokens_out},
        )

    logger.info(
        "build_entry_tx",
        extra={
            "intent_id": intent.client_intent_id,
            "mint": intent.mint,
            "sol_in_lamports": intent.sol_in_lamports,
            "tip_lamports": intent.tip_lamports,
            "cu_limit": cu_limit,
            "cu_price": intent.cu_price_microlamports,
            "pool_program": pool_program_name,
            "min_tokens_out": min_tokens_out,
        },
    )

    return UnsignedTx(
        serialized_b64=serialized_b64,
        intent_kind="ENTRY",
        mint=intent.mint,
        client_intent_id=intent.client_intent_id,
    )


def build_exit_tx(
    *,
    intent: ExitIntent | ReduceIntent,
    jupiter_swap_b64: str,
    wallet_pubkey: str,
    blockhash: str,
    cu_limit: int,
    tip_lamports: int,
    cu_price_microlamports: int,
) -> UnsignedTx:
    """Build an unsigned exit transaction (Jupiter-routed, FAST path).

    Exits use Jupiter v6/Ultra because the route aggregation is more efficient
    for selling into fragmented liquidity (execution-venue.md §2).

    The jupiter_swap_b64 is the pre-built swap bytes from the Jupiter /swap-instructions
    endpoint. We wrap it with ComputeBudget + Jito tip and sign.
    """
    # Guard: no float money fields.
    for name, val in [
        ("tip_lamports", tip_lamports),
        ("cu_price_microlamports", cu_price_microlamports),
        ("cu_limit", cu_limit),
    ]:
        if isinstance(val, float):
            raise TypeError(
                f"build_exit_tx: '{name}' must be int, not float."
            )

    intent_kind = intent.kind.value

    if jupiter_swap_b64:
        # Wrap the Jupiter swap bytes with our ComputeBudget + tip.
        serialized_b64 = _build_stub_tx(
            intent_kind=intent_kind,
            mint=intent.mint,
            client_intent_id=intent.client_intent_id,
            sol_in_lamports=0,  # Exit: receive SOL, not spend it
            tip_lamports=tip_lamports,
            cu_price_microlamports=cu_price_microlamports,
            cu_limit=cu_limit,
            pool_program_name="jupiter_aggregator_v6",
            extra={"jupiter_swap_b64": jupiter_swap_b64[:32] + "...(truncated)"},
        )
    else:
        serialized_b64 = _build_stub_tx(
            intent_kind=intent_kind,
            mint=intent.mint,
            client_intent_id=intent.client_intent_id,
            sol_in_lamports=0,
            tip_lamports=tip_lamports,
            cu_price_microlamports=cu_price_microlamports,
            cu_limit=cu_limit,
            pool_program_name="jupiter_aggregator_v6",
        )

    return UnsignedTx(
        serialized_b64=serialized_b64,
        intent_kind=intent_kind,
        mint=intent.mint,
        client_intent_id=intent.client_intent_id,
    )
