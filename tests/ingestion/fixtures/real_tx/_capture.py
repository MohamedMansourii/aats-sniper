"""ONE-OFF CAPTURE SCRIPT (E-M1-05) — not a pytest test, not imported by any test.

Fetches REAL mainnet transactions for each venue/instruction-path via
`getTransaction` (Solana JSON-RPC) and writes the raw RPC response JSON into
`tests/ingestion/fixtures/real_tx/` as committed fixtures.  Re-run manually
(never at test time, never in CI) if the fixture set needs refreshing:

    python tests/ingestion/fixtures/real_tx/_capture.py

Requires RPC_PRIMARY in `.env` (repo root).  This script is the ONLY thing in
the ingestion test suite that touches the live network — every test that
consumes the JSON files it writes is 100% offline (loads the committed JSON,
no RPC call at test time), matching this repo's "no live network required"
fixture convention (see tests/ingestion/fixtures.py's module docstring).

No secrets are written to the output: the fixture JSON is the RPC RESPONSE
BODY only (public on-chain data — signatures, slots, instruction bytes,
account pubkeys). The RPC URL / API key used to fetch it is never embedded.

Classification method: PRIMARILY the Anchor auto-log line
`Program log: Instruction: <Name>` that appears immediately after the
`Program <program_id> invoke [N]` log line for the target program (this is
what the real on-chain program itself logs — the most reliable signal,
independent of any discriminator assumption).  As a defense-in-depth cross
check, the matched instruction's raw data bytes are ALSO required to match
the SAME Anchor-discriminator / manual-Borsh markers the production decoder
(aats/ingestion/decoders.py) uses, imported directly from that module so the
capture criterion can never drift from the real decode criterion.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(_REPO_ROOT))

from aats.ingestion.decoders import (  # noqa: E402
    CPMM_DISC_INITIALIZE,
    CPMM_DISC_SWAP_INPUT,
    CPMM_DISC_SWAP_OUTPUT,
    PUMP_DISC_BUY,
    PUMP_DISC_CREATE,
    PUMP_DISC_SELL,
    PUMP_DISC_WITHDRAW,
    PUMPSWAP_DISC_BUY,
    PUMPSWAP_DISC_CREATE_POOL,
    PUMPSWAP_DISC_SELL,
    RAYDIUM_V4_DISC_INIT2,
    RAYDIUM_V4_DISC_SWAP,
    RAYDIUM_V4_DISC_SWAP2,
)

OUT_DIR = Path(__file__).resolve().parent

PROGRAM_IDS = {
    "pumpfun": "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",
    "pumpswap": "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA",
    "raydium_v4": "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8",
    "raydium_cpmm": "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C",
}
PID_TO_PROGKEY = {v: k for k, v in PROGRAM_IDS.items()}

# case_name -> (program key, {candidate "Instruction: <Name>" log tokens}, byte matcher)
CASES: dict[str, tuple[str, set[str], callable]] = {
    "pumpfun_create": ("pumpfun", {"Create"}, lambda d: len(d) >= 8 and d[:8] == PUMP_DISC_CREATE),
    "pumpfun_buy": ("pumpfun", {"Buy"}, lambda d: len(d) >= 24 and d[:8] == PUMP_DISC_BUY),
    "pumpfun_sell": ("pumpfun", {"Sell"}, lambda d: len(d) >= 24 and d[:8] == PUMP_DISC_SELL),
    "pumpfun_withdraw": (
        "pumpfun",
        {"Withdraw", "Migrate"},
        lambda d: len(d) >= 8 and d[:8] == PUMP_DISC_WITHDRAW,
    ),
    "pumpswap_create_pool": (
        "pumpswap",
        {"CreatePool", "Create"},
        lambda d: len(d) >= 24 and d[:8] == PUMPSWAP_DISC_CREATE_POOL,
    ),
    "pumpswap_buy": ("pumpswap", {"Buy"}, lambda d: len(d) >= 24 and d[:8] == PUMPSWAP_DISC_BUY),
    "pumpswap_sell": ("pumpswap", {"Sell"}, lambda d: len(d) >= 24 and d[:8] == PUMPSWAP_DISC_SELL),
    "raydium_v4_initialize2": (
        "raydium_v4",
        set(),  # non-anchor program, no auto-log; byte matcher only
        lambda d: len(d) >= 26 and d[:1] == RAYDIUM_V4_DISC_INIT2,
    ),
    "raydium_v4_swap": (
        "raydium_v4",
        set(),
        lambda d: len(d) >= 17 and d[:1] in (RAYDIUM_V4_DISC_SWAP, RAYDIUM_V4_DISC_SWAP2),
    ),
    "raydium_cpmm_initialize": (
        "raydium_cpmm",
        {"Initialize"},
        lambda d: len(d) >= 24 and d[:8] == CPMM_DISC_INITIALIZE,
    ),
    "raydium_cpmm_swap": (
        "raydium_cpmm",
        {"SwapBaseInput", "SwapBaseOutput", "Swap"},
        lambda d: len(d) >= 24 and d[:8] in (CPMM_DISC_SWAP_INPUT, CPMM_DISC_SWAP_OUTPUT),
    ),
}


def _load_rpc_url() -> str:
    env_path = _REPO_ROOT / ".env"
    with open(env_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line.startswith("RPC_PRIMARY="):
                return line.split("=", 1)[1].strip()
    raise SystemExit("RPC_PRIMARY not found in .env")


_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b58decode(s: str) -> bytes:
    if not s:
        return b""
    num = 0
    for ch in s:
        num = num * 58 + _B58_ALPHABET.index(ch)
    n_bytes = (num.bit_length() + 7) // 8
    combined = num.to_bytes(n_bytes, "big") if num else b""
    n_pad = len(s) - len(s.lstrip("1"))
    return b"\x00" * n_pad + combined


def _get_transaction(http, url: str, sig: str) -> dict | None:
    resp = http.post(
        url,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getTransaction",
            "params": [sig, {"encoding": "json", "maxSupportedTransactionVersion": 0}],
        },
        timeout=30.0,
    )
    resp.raise_for_status()
    return resp.json().get("result")


def _instruction_log_names(logs: list[str], program_id: str) -> list[str]:
    """Extract every 'Instruction: <Name>' token logged immediately inside a
    `Program <program_id> invoke [N]` ... `Program <program_id> success` block
    (handles nested CPI blocks by depth-tracking each program's own invoke
    depth, so a same-named inner program call doesn't confuse attribution).
    """
    names: list[str] = []
    depth_stack: list[str] = []  # program ids currently "in" (by invoke order)
    pending_name_for: str | None = None
    for line in logs:
        if " invoke [" in line and line.startswith("Program "):
            pid = line.split(" ", 2)[1]
            depth_stack.append(pid)
            if pid == program_id:
                pending_name_for = pid
            continue
        if pending_name_for is not None and line.startswith("Program log: Instruction: "):
            names.append(line.split("Program log: Instruction: ", 1)[1].strip())
            pending_name_for = None
            continue
        if line.startswith("Program ") and (" success" in line or " failed" in line) and depth_stack:
            depth_stack.pop()
    return names


def _preload_existing(found: dict[str, dict], manifest: dict[str, dict]) -> None:
    """Load already-captured fixtures from disk so a re-run never re-fetches
    (or re-burns rate-limit budget on) a case that is already committed."""
    manifest_path = OUT_DIR / "manifest.json"
    if manifest_path.exists():
        manifest.update(json.loads(manifest_path.read_text(encoding="utf-8")))
    for case_name in CASES:
        p = OUT_DIR / f"{case_name}.json"
        if p.exists():
            found[case_name] = json.loads(p.read_text(encoding="utf-8"))
            print(f"  preloaded {case_name} from disk (skipping re-capture)")


def main() -> None:
    import httpx

    url = _load_rpc_url()
    http = httpx.Client(timeout=30.0)

    found: dict[str, dict] = {}
    manifest: dict[str, dict] = {}
    _preload_existing(found, manifest)

    def try_flush() -> None:
        for case_name, raw in found.items():
            out_path = OUT_DIR / f"{case_name}.json"
            if not out_path.exists():
                out_path.write_text(json.dumps(raw, indent=2, sort_keys=True), encoding="utf-8")
                print(f"  wrote {out_path.name}")
        manifest_path = OUT_DIR / "manifest.json"
        existing = {}
        if manifest_path.exists():
            existing = json.loads(manifest_path.read_text(encoding="utf-8"))
        existing.update(manifest)
        manifest_path.write_text(json.dumps(existing, indent=2, sort_keys=True), encoding="utf-8")

    def scan_tx(tx: dict) -> None:
        """Check EVERY known-venue program present in this tx against every
        case (not just the program whose signature list we're paging) — a
        pump.fun migration and its PumpSwap create_pool CPI land in the SAME
        atomic transaction, so scanning one venue's history opportunistically
        captures the other venue's fixture too.
        """
        meta = tx.get("meta") or {}
        message = (tx.get("transaction") or {}).get("message") or {}
        account_keys = list(message.get("accountKeys") or [])
        loaded = meta.get("loadedAddresses") or {}
        account_keys += list(loaded.get("writable") or [])
        account_keys += list(loaded.get("readonly") or [])
        logs = list(meta.get("logMessages") or [])

        def all_ixs():
            yield from message.get("instructions") or []
            for grp in meta.get("innerInstructions") or []:
                yield from grp.get("instructions") or []

        present_pids = set()
        for ix in all_ixs():
            pid_idx = ix.get("programIdIndex")
            if pid_idx is not None and pid_idx < len(account_keys):
                present_pids.add(account_keys[pid_idx])

        log_names_by_pid = {
            pid: set(_instruction_log_names(logs, pid))
            for pid in present_pids
            if pid in PID_TO_PROGKEY
        }

        for ix in all_ixs():
            pid_idx = ix.get("programIdIndex")
            if pid_idx is None or pid_idx >= len(account_keys):
                continue
            program_id = account_keys[pid_idx]
            prog_key = PID_TO_PROGKEY.get(program_id)
            if prog_key is None:
                continue
            data_b58 = ix.get("data") or ""
            try:
                data_bytes = b58decode(data_b58)
            except Exception:
                continue
            observed_names = log_names_by_pid.get(program_id, set())
            for case_name, (pk2, name_tokens, matcher) in CASES.items():
                if pk2 != prog_key or case_name in found:
                    continue
                try:
                    byte_ok = matcher(data_bytes)
                except Exception:
                    byte_ok = False
                if not byte_ok:
                    continue
                if name_tokens and not (observed_names & name_tokens):
                    continue  # byte match but log name disagrees -> skip, keep scanning
                found[case_name] = tx
                sig0 = ((tx.get("transaction") or {}).get("signatures") or [""])[0]
                manifest[case_name] = {
                    "signature": sig0,
                    "slot": tx["slot"],
                    "block_time": tx["blockTime"],
                    "program_id": program_id,
                    "log_names_observed": sorted(observed_names),
                }
                print(f"  FOUND {case_name}: {sig0} slot={tx['slot']}")

    all_case_names = list(CASES.keys())
    for prog_key, program_id in PROGRAM_IDS.items():
        if all(name in found for name in all_case_names):
            break
        # Only THIS program's own cases gate its scan loop (a case for a
        # DIFFERENT program can never be found by paging this program's
        # signatures) — prevents re-scanning an already-satisfied program's
        # full history just because an unrelated program still has gaps.
        own_cases = [name for name, (pk, _, _) in CASES.items() if pk == prog_key]
        if all(name in found for name in own_cases):
            print(f"--- skipping {prog_key}: all its cases already captured ---")
            continue

        print(f"--- scanning {prog_key} ({program_id}) ---")
        before: str | None = None
        scanned = 0
        max_scan = 6000
        consecutive_429 = 0
        while scanned < max_scan and not all(name in found for name in own_cases):
            params: dict = {"limit": 200}
            if before is not None:
                params["before"] = before
            try:
                sig_resp = http.post(
                    url,
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "getSignaturesForAddress",
                        "params": [program_id, params],
                    },
                    timeout=30.0,
                )
                sig_resp.raise_for_status()
                sig_result = sig_resp.json().get("result") or []
                consecutive_429 = 0
            except Exception as exc:  # noqa: BLE001
                print(f"  signatures error: {exc}")
                consecutive_429 += 1
                time.sleep(min(2.0 * consecutive_429, 20.0))
                continue
            if not sig_result:
                print(f"  exhausted signature history for {prog_key} at scanned={scanned}")
                break
            before = sig_result[-1]["signature"]
            scanned += len(sig_result)

            sigs = [row["signature"] for row in sig_result if row.get("err") is None]
            print(f"  page: {len(sig_result)} sigs, {len(sigs)} ok, scanned={scanned}")

            for sig in sigs:
                if all(name in found for name in own_cases):
                    break
                try:
                    tx = _get_transaction(http, url, sig)
                    consecutive_429 = 0
                except Exception as exc:  # noqa: BLE001
                    print(f"  tx error: {exc}")
                    consecutive_429 += 1
                    time.sleep(min(1.0 * consecutive_429, 20.0))
                    continue
                time.sleep(0.12)
                if tx is None or tx.get("blockTime") is None:
                    continue
                if (tx.get("meta") or {}).get("err") is not None:
                    continue
                scan_tx(tx)

            try_flush()

    try_flush()
    missing = [name for name in CASES if name not in found]
    if missing:
        print(f"MISSING (not found within scan budget): {missing}")
    else:
        print("ALL CASES CAPTURED.")


if __name__ == "__main__":
    main()
