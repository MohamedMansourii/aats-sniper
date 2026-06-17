"""Pluggable venue / program-ID registry (T-300, execution-venue.md §3).

The registry is the SINGLE source of program IDs for ALL decoders and
transport filters.  No decoder, no hot-path file, and no snipe file ever
carries a base58 program-ID literal (execution-venue.md §3.2 build guard;
validation-harness.md §2 guard 3).

Program IDs are loaded from config (program-allowlist.json OR environment),
verified at startup (getAccountInfo → executable=true), and injected into
every decoder via this registry.  The hot core reads the same IDs from here
after the Python slow-loop verifies them at boot.

Live verification is INJECTABLE so tests can pass a mock verifier.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from aats.contracts.events import LaunchSource

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VenueEntry:
    """A single entry in the pluggable program-ID registry.

    program_id: the on-chain pubkey string — loaded from config, never hardcoded
    in any decoder file (execution-venue.md §3.2).
    """

    venue: LaunchSource
    program_id: str  # populated from config / allowlist, not a literal here
    amm_fee_bps: int  # 25 for PumpSwap / Raydium (A-005/006)
    migration_target: LaunchSource | None
    status: str  # "active" | "candidate" | "deprecated"
    last_verified_slot: int = 0  # set by startup verify_live()


# Verifier type: (program_id: str) -> bool (executable=true on-chain)
Verifier = Callable[[str], bool]


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class ProgramRegistry:
    """Live-verified program-ID registry.

    Usage:
        registry = ProgramRegistry.from_allowlist(path, verifier=live_verifier)
        pid = registry.program_id(LaunchSource.PUMPFUN)   # the hot path reads this

    The registry exposes only ACTIVE entries after verify_live().
    Decoders receive a reference to this registry at construction time —
    they never hold a program-ID literal.
    """

    def __init__(self) -> None:
        self._entries: dict[LaunchSource, VenueEntry] = {}
        self._by_pid: dict[str, VenueEntry] = {}

    # ---- construction ----

    @classmethod
    def from_allowlist(
        cls,
        allowlist_path: Path,
        verifier: Verifier | None = None,
        filter_active_only: bool = True,
    ) -> ProgramRegistry:
        """Build from the config/program-allowlist.json file.

        Args:
            allowlist_path: Path to program-allowlist.json.
            verifier: callable(program_id) -> bool.  If None, skips on-chain verify
                      (offline / test mode — still loads IDs from config).
            filter_active_only: if True, only 'active' status entries are admitted.

        The registry is fail-closed: an entry that fails verification is logged and
        dropped from the live set, never silently allowed (allowlist HARD_RULES).
        """
        registry = cls()
        with open(allowlist_path) as f:
            allowlist = json.load(f)

        # Map venue names in the allowlist to LaunchSource enum values
        venue_name_map: dict[str, LaunchSource] = {
            "pump.fun": LaunchSource.PUMPFUN,
            "pumpswap": LaunchSource.PUMPSWAP,
            "raydium_v4": LaunchSource.RAYDIUM_V4,
            "raydium_cpmm": LaunchSource.RAYDIUM_CPMM,
        }
        amm_fees: dict[LaunchSource, int] = {
            LaunchSource.PUMPFUN: 100,      # pump.fun bonding-curve has no AMM fee (flat)
            LaunchSource.PUMPSWAP: 25,      # A-005
            LaunchSource.RAYDIUM_V4: 25,    # A-006
            LaunchSource.RAYDIUM_CPMM: 25,  # A-006
            LaunchSource.MIGRATION: 0,      # synthetic event, no AMM fee
        }
        migration_targets: dict[LaunchSource, LaunchSource] = {
            LaunchSource.PUMPFUN: LaunchSource.PUMPSWAP,  # A-001: pump.fun migrates to PumpSwap
        }

        for prog_entry in allowlist.get("allowed_programs", []):
            venue_str: str | None = prog_entry.get("venue")
            if venue_str is None:
                continue  # core programs (system, compute_budget, etc.) are not venues
            venue = venue_name_map.get(venue_str)
            if venue is None:
                continue
            status = prog_entry.get("status", "candidate")
            if filter_active_only and status not in ("active",):
                logger.debug("Skipping non-active venue entry: %s (%s)", venue_str, status)
                continue

            pid = prog_entry["program_id"]

            # Live verification (fail-closed on failure)
            if verifier is not None:
                try:
                    ok = verifier(pid)
                except Exception as exc:
                    logger.warning(
                        "Verification error for %s (%s): %s — dropping from live set",
                        venue_str, pid, exc,
                    )
                    ok = False
                if not ok:
                    logger.warning(
                        "Program %s (%s) failed live verification — DROPPED from live set (fail-closed)",
                        pid, venue_str,
                    )
                    continue

            entry = VenueEntry(
                venue=venue,
                program_id=pid,
                amm_fee_bps=amm_fees.get(venue, 25),
                migration_target=migration_targets.get(venue),
                status=status,
            )
            registry._entries[venue] = entry
            registry._by_pid[pid] = entry

        logger.info(
            "ProgramRegistry loaded: %d active venues: %s",
            len(registry._entries),
            [e.venue.value for e in registry._entries.values()],
        )
        return registry

    @classmethod
    def from_dict(cls, entries: dict[LaunchSource, str]) -> ProgramRegistry:
        """Build from a plain {LaunchSource: program_id} dict (test / offline use).

        Useful in tests where you want to inject specific IDs without
        touching the filesystem or doing live verification.
        """
        registry = cls()
        for venue, pid in entries.items():
            entry = VenueEntry(
                venue=venue,
                program_id=pid,
                amm_fee_bps=25,
                migration_target=None,
                status="active",
            )
            registry._entries[venue] = entry
            registry._by_pid[pid] = entry
        return registry

    # ---- lookups ----

    def program_id(self, venue: LaunchSource) -> str:
        """Return the program ID for a venue.  Raises KeyError if not active."""
        return self._entries[venue].program_id

    def venue_for_pid(self, pid: str) -> LaunchSource | None:
        """Reverse-lookup: program ID → venue.  Returns None if unrecognized."""
        entry = self._by_pid.get(pid)
        return entry.venue if entry else None

    def entry(self, venue: LaunchSource) -> VenueEntry | None:
        return self._entries.get(venue)

    def active_program_ids(self) -> frozenset[str]:
        """All program IDs currently active in the live registry."""
        return frozenset(e.program_id for e in self._entries.values())

    def __contains__(self, pid: str) -> bool:
        return pid in self._by_pid

    def __repr__(self) -> str:
        return f"ProgramRegistry(venues={[v.value for v in self._entries]})"
