"""Loads COMMITTED real-mainnet transaction fixtures (E-M1-05).

Converts the raw Solana `getTransaction` JSON-RPC response body (captured
once, offline, via ``tests/ingestion/fixtures/real_tx/_capture.py`` and
committed to ``tests/ingestion/fixtures/real_tx/*.json``) into the decoder's
transport-agnostic ``aats.ingestion.decoders.RawTransaction`` input type.

NO NETWORK CALL happens here or in any test that imports this module — the
JSON files are static, committed fixtures (same "no live network required"
posture as ``tests/ingestion/fixtures.py``).  This module is a pure,
offline JSON -> RawTransaction converter.

Address-lookup-table (versioned tx) resolution: `accountKeys` is the
transaction's own STATIC key list; any account referenced only via an
Address Lookup Table appears in `meta.loadedAddresses.{writable,readonly}`
and Solana's wire convention is to logically APPEND writable-then-readonly
after the static keys so that every `programIdIndex` / per-instruction
`accounts` index resolves against the SAME flat, ordered list the RPC node
used. That is exactly what `_flat_account_keys` below reconstructs.
"""

from __future__ import annotations

import json
from pathlib import Path

from aats.ingestion.decoders import RawInstruction, RawTransaction

_FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "real_tx"

_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b58decode(s: str) -> bytes:
    """Minimal, dependency-free base58 decoder (Bitcoin/Solana alphabet).

    Instruction `data` fields on the Solana JSON-RPC "json"-encoded
    transaction come back base58-encoded; the production decoder expects
    base64 (`RawInstruction.data_b64`) so every real fixture is re-encoded
    at load time via this decoder + `base64.b64encode`.
    """
    if not s:
        return b""
    num = 0
    for ch in s:
        num = num * 58 + _B58_ALPHABET.index(ch)
    n_bytes = (num.bit_length() + 7) // 8
    combined = num.to_bytes(n_bytes, "big") if num else b""
    n_pad = len(s) - len(s.lstrip("1"))
    return b"\x00" * n_pad + combined


def _flat_account_keys(tx: dict) -> list[str]:
    message = (tx.get("transaction") or {}).get("message") or {}
    keys = list(message.get("accountKeys") or [])
    loaded = (tx.get("meta") or {}).get("loadedAddresses") or {}
    keys += list(loaded.get("writable") or [])
    keys += list(loaded.get("readonly") or [])
    return keys


def _to_raw_instruction(ix: dict, account_keys: list[str]) -> RawInstruction | None:
    import base64

    pid_idx = ix.get("programIdIndex")
    if pid_idx is None or pid_idx >= len(account_keys):
        return None
    program_id = account_keys[pid_idx]
    acct_indices = ix.get("accounts") or []
    ix_accounts = [account_keys[i] for i in acct_indices if i < len(account_keys)]
    data_b58 = ix.get("data") or ""
    data_bytes = b58decode(data_b58)
    return RawInstruction(
        program_id=program_id,
        data_b64=base64.b64encode(data_bytes).decode(),
        account_keys=ix_accounts,
        program_index=pid_idx,
    )


def raw_transaction_from_rpc_json(tx: dict) -> RawTransaction:
    """Convert one `getTransaction` RPC result dict into a `RawTransaction`.

    Pure function, no I/O.  `tx` is the `result` object of the RPC response
    (i.e. the dict with keys `slot`, `blockTime`, `transaction`, `meta`).
    """
    account_keys = _flat_account_keys(tx)
    message = (tx.get("transaction") or {}).get("message") or {}
    meta = tx.get("meta") or {}

    outer: list[RawInstruction] = []
    for ix in message.get("instructions") or []:
        parsed = _to_raw_instruction(ix, account_keys)
        if parsed is not None:
            outer.append(parsed)

    inner: list[RawInstruction] = []
    for group in meta.get("innerInstructions") or []:
        for ix in group.get("instructions") or []:
            parsed = _to_raw_instruction(ix, account_keys)
            if parsed is not None:
                inner.append(parsed)

    fee_payer = account_keys[0] if account_keys else ""
    block_time = tx.get("blockTime")
    logs = list(meta.get("logMessages") or [])
    err = meta.get("err")

    signatures = (tx.get("transaction") or {}).get("signatures") or []
    signature = signatures[0] if signatures else ""

    return RawTransaction(
        signature=signature,
        slot=int(tx["slot"]),
        block_time_unix_s=(int(block_time) if block_time is not None else None),
        fee_payer=fee_payer,
        instructions=outer,
        inner_instructions=inner,
        program_logs=logs,
        is_vote=False,
        err=(json.dumps(err) if err is not None else None),
    )


def load_real_tx(case_name: str) -> RawTransaction:
    """Load a committed real-tx fixture by case name (e.g. "pumpfun_create").

    Raises FileNotFoundError with a clear message if the fixture has not
    been captured yet (see real_tx/manifest.json + real_tx/_capture.py).
    """
    path = _FIXTURE_DIR / f"{case_name}.json"
    if not path.exists():
        raise FileNotFoundError(
            f"real-tx fixture {case_name!r} not captured yet — run "
            f"tests/ingestion/real_tx/_capture.py (requires RPC_PRIMARY) to "
            f"produce {path}."
        )
    tx = json.loads(path.read_text(encoding="utf-8"))
    return raw_transaction_from_rpc_json(tx)


def available_cases() -> list[str]:
    """Names of every real-tx fixture currently committed on disk."""
    if not _FIXTURE_DIR.exists():
        return []
    return sorted(p.stem for p in _FIXTURE_DIR.glob("*.json") if p.stem != "manifest")


def manifest() -> dict:
    """The provenance manifest (signature/slot/block_time) for captured cases."""
    path = _FIXTURE_DIR / "manifest.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))
