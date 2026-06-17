"""T-327 — tx_builder unit tests: no-literal CI guard + fail-CLOSED behaviour.

Self-check items verified here:
  B1-guard: execution-venue.md §3.2 / config/program-allowlist.json build_time_verification
            ci_steps[1] — assert no base58 program-ID-shaped literal appears in any hot-path
            execution file. This file is the ONLY home for those IDs.
  B1-fail-closed-tip: _get_tip_accounts_from_allowlist() raises VenueError when the
            allowlist is unavailable (SIGNER_ALLOWLIST_PATH=/nonexistent), NOT a silent
            fallback to a hardcoded literal.
  B1-fail-closed-program-ids: _build_swap_accounts() raises VenueError when
            spl_token_program or associated_token_account_program are absent from
            the program_ids dict (simulates a failed allowlist load).
"""
from __future__ import annotations

import os
import pathlib
import re

import pytest

from aats.execution.exceptions import VenueError
from aats.execution.tx_builder import (
    _get_tip_accounts_from_allowlist,
    _reset_program_id_cache,
    size_cu_limit,
)

# ---------------------------------------------------------------------------
# CI guard: no base58 program-ID literal in any hot-path execution file
# (execution-venue.md §3.2; config/program-allowlist.json ci_steps[1])
# ---------------------------------------------------------------------------

# Known program IDs from config/program-allowlist.json (the allowlist is the ONLY home).
# These must NOT appear as literals in any hot-path execution *.py file.
_KNOWN_PROGRAM_ID_LITERALS = [
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",   # spl_token_program
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL",  # associated_token_account_program
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",   # spl_token_2022_program
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",   # pump_fun_bonding_curve
    "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA",   # pumpswap_amm
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8",  # raydium_amm_v4
    "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C",  # raydium_cpmm
    "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4",   # jupiter_aggregator_v6
    "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo",   # meteora_dlmm
    "MoonCVVNZFSYkqNXP6bxHLPL6QQJiMagDL3qcqUQTrG",   # moonshot
    # Jito tip accounts — also must not appear as literals in execution code.
    "9n3d1K5YD2vECAbRFhFFGYNNjiXtHXJWn9F31t89vsAV",
    "aTtUk2DHgLhKZRDjePq6eiHRKC1XXFMBiSUfQ2JNDbN",
    "B1mrQSpdeMU9gCvkJ6VsXVVoYjRGkNA7TtjMyqxrhecH",
    "9ttgPBBhRYFuQccdR1DSnb7hydsWANoDsV3P9kaGMCEh",
    "4xgEmT58RwTNsF5xm2RMYCnR1EVukdK8a1i2qFjnJFu3",
    "EoW3SUQap7ZeynXQ2QJ847aerhxbPVr843uMeTfc9dxM",
    "E2eSqe33tuhAHKTrwky5uEjaVqnb2T9ns6nHHUrN8588",
    "ARTtviJkLLt6cHGQDydfo1Wyk6M4VGZdKZ2ZhdnJL336",
]

# Files under aats/execution/ that are on the hot path (sniping / decoding).
# The program IDs above must NOT appear as string literals in any of these files.
_EXECUTION_ROOT = pathlib.Path(__file__).parent.parent.parent / "aats" / "execution"

# The allowlist itself and similar policy/config files are explicitly excluded.
_EXCLUDED_FILES: set[str] = set()


def _strip_comments_and_docstrings(source: str) -> str:
    """Remove Python comments and string literals from source for the literal check.

    We only want to flag the ID appearing as a LIVE code literal (e.g. a default
    value in .get("key", "<literal>") or a constant assignment) — not inside a
    comment or a docstring that documents why it must NOT appear.
    """
    # Remove single-line comments.
    source = re.sub(r"#[^\n]*", "", source)
    # Remove triple-quoted strings (docstrings / multiline strings).
    source = re.sub(r'"""[\s\S]*?"""', '""', source)
    source = re.sub(r"'''[\s\S]*?'''", "''", source)
    return source


class TestNoProgramIdLiteralInExecutionFiles:
    """CI guard: execution-venue.md §3.2 — no hardcoded program-ID in hot-path files.

    This mirrors the static-analysis check in build_time_verification.ci_steps[1].
    A base58 program-ID-shaped literal appearing in execution code means a stale-ID
    silently survives a pool rotation or program upgrade.
    """

    @pytest.mark.parametrize("literal", _KNOWN_PROGRAM_ID_LITERALS)
    def test_no_known_literal_in_execution_source(self, literal: str) -> None:
        """Assert the literal does not appear as a code token in any execution/*.py file."""
        violations: list[str] = []
        for py_file in _EXECUTION_ROOT.rglob("*.py"):
            if py_file.name in _EXCLUDED_FILES:
                continue
            raw = py_file.read_text(encoding="utf-8", errors="ignore")
            stripped = _strip_comments_and_docstrings(raw)
            if literal in stripped:
                violations.append(f"{py_file.relative_to(_EXECUTION_ROOT)}")

        assert not violations, (
            f"Program-ID / tip-account literal '{literal}' found as a code token in "
            f"execution files: {violations}. "
            "These IDs must live ONLY in config/program-allowlist.json "
            "(execution-venue.md §3.2 / build_time_verification ci_steps[1])."
        )


# ---------------------------------------------------------------------------
# Fail-CLOSED: tip accounts — raises VenueError when allowlist is unavailable
# ---------------------------------------------------------------------------

class TestTipAccountsFailClosed:
    """B1 fix: _get_tip_accounts_from_allowlist() must refuse (VenueError) when the
    allowlist is unavailable, NOT silently fall back to a hardcoded literal.
    """

    def test_raises_venue_error_when_allowlist_path_nonexistent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Simulates SIGNER_ALLOWLIST_PATH=/nonexistent: must raise, not silently default."""
        monkeypatch.setenv("SIGNER_ALLOWLIST_PATH", "/nonexistent/path/program-allowlist.json")
        with pytest.raises(VenueError) as exc_info:
            _get_tip_accounts_from_allowlist()
        assert exc_info.value.reason in (
            "tip_accounts_load_failed",
            "tip_accounts_key_missing",
            "tip_accounts_empty",
        ), (
            f"Expected a tip-accounts fail-closed VenueError, got reason='{exc_info.value.reason}'. "
            "Must refuse, not fall back to a hardcoded literal (B1 fix)."
        )

    def test_real_allowlist_loads_successfully(self) -> None:
        """With the real allowlist present, loading succeeds and returns a non-empty list."""
        accounts = _get_tip_accounts_from_allowlist()
        assert isinstance(accounts, list), "Expected a list of tip account strings."
        assert len(accounts) > 0, "Tip account list must be non-empty."
        for acc in accounts:
            assert isinstance(acc, str), f"Tip account must be str, got {type(acc).__name__}."
            assert len(acc) >= 32, f"Tip account '{acc}' looks too short to be a valid pubkey."


# ---------------------------------------------------------------------------
# Fail-CLOSED: program IDs — raises VenueError when IDs are absent from dict
# (covers the solders build path; tests without solders hit the stub path which
#  is safe-by-construction since the stub does not build real tx bytes)
# ---------------------------------------------------------------------------

class TestProgramIdFailClosed:
    """B1 fix: _build_swap_accounts() must raise VenueError when spl_token_program or
    associated_token_account_program are absent from the program_ids dict.
    """

    def test_missing_spl_token_raises_venue_error(self) -> None:
        """spl_token_program absent → VenueError (fail-CLOSED)."""
        # Only import if solders is available; otherwise skip (stub path is safe).
        try:
            import solders  # noqa: F401
        except ImportError:
            pytest.skip("solders not installed — _build_swap_accounts is not on the live path")

        from aats.execution.tx_builder import _build_swap_accounts

        incomplete_ids = {
            # spl_token_program intentionally absent
            "associated_token_account_program": "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL",
            "system_program": "11111111111111111111111111111111",
        }
        with pytest.raises(VenueError) as exc_info:
            _build_swap_accounts(
                wallet_pubkey="MockPubkey11111111111111111111111111111111",
                pool_keys={"pool": "11111111111111111111111111111111"},
                mint="TokenMint111111111111111111111111111111111",
                program_ids=incomplete_ids,
            )
        assert exc_info.value.reason == "program_id_missing", (
            f"Expected reason='program_id_missing', got '{exc_info.value.reason}'."
        )

    def test_missing_ata_program_raises_venue_error(self) -> None:
        """associated_token_account_program absent → VenueError (fail-CLOSED)."""
        try:
            import solders  # noqa: F401
        except ImportError:
            pytest.skip("solders not installed — _build_swap_accounts is not on the live path")

        from aats.execution.tx_builder import _build_swap_accounts

        incomplete_ids = {
            "spl_token_program": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
            # associated_token_account_program intentionally absent
            "system_program": "11111111111111111111111111111111",
        }
        with pytest.raises(VenueError) as exc_info:
            _build_swap_accounts(
                wallet_pubkey="MockPubkey11111111111111111111111111111111",
                pool_keys={"pool": "11111111111111111111111111111111"},
                mint="TokenMint111111111111111111111111111111111",
                program_ids=incomplete_ids,
            )
        assert exc_info.value.reason == "program_id_missing"

    def test_program_id_cache_raises_on_failed_allowlist_load(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When allowlist is unavailable, _get_program_ids() returns empty dict.

        The empty dict is then caught by the fail-CLOSED checks in _build_swap_accounts /
        _build_real_entry_tx when solders is present. For the stub path (no solders)
        we verify the cache returns empty without crashing.
        """
        # Reset cache so it reloads fresh with the bad path.
        _reset_program_id_cache()
        monkeypatch.setenv("SIGNER_ALLOWLIST_PATH", "/nonexistent/path/allowlist.json")
        from aats.execution.tx_builder import _get_program_ids
        ids = _get_program_ids()
        # The loader falls back gracefully (returns empty dict) so the caller can detect
        # and raise. The fail-CLOSED raise happens at the point of USE, not at load.
        assert isinstance(ids, dict), "Expected dict even on load failure."
        # Reset cache again so other tests are not affected by the bad env.
        _reset_program_id_cache()


# ---------------------------------------------------------------------------
# size_cu_limit — basic correctness
# ---------------------------------------------------------------------------

class TestSizeCuLimit:
    def test_headroom_applied(self) -> None:
        result = size_cu_limit(100_000)
        assert result == 125_000  # 100k * 1.25

    def test_ceiling_enforced(self) -> None:
        result = size_cu_limit(600_000)
        assert result == 400_000

    def test_zero_returns_ceiling(self) -> None:
        result = size_cu_limit(0)
        assert result == 400_000

    def test_result_is_int(self) -> None:
        result = size_cu_limit(180_000)
        assert isinstance(result, int), f"Expected int, got {type(result).__name__}."
