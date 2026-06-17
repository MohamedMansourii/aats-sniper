"""M3 Controller — WalletClusterStore: bounded ring of per-mint cluster graphs.

E11 additive feature.  The SNIPE or SLOW loop writes one WalletClusterGraph per
evaluated LaunchEvent when microstructure features are available.  The control
plane reads the store for GET /api/wallet-cluster.

Design mirrors CandidateQueue (aats/controller/candidate_store.py):
  - Bounded ring (maxlen=N, default 100): oldest entries evicted automatically.
  - Thread-safe: all mutations hold self._lock (RLock).
  - WRITE PATH: non-blocking append into a deque — no await, no Redis, no LLM.
  - READ PATH: snapshot() returns a shallow-copy list. Read-only, no mutation.

The store is INTENTIONALLY separate from the StateStore Protocol:
  - Optional: if None is wired, GET /api/wallet-cluster returns [] (backward-compat).
  - Separate bounded-memory footprint.
  - Direction: controller -> control_plane is the correct import direction.

HARD RULES:
  - No money (no lamport fields — supply shares are bps, not lamports).
  - No secrets. No keys.
  - Write path synchronous and non-blocking (FAST/SNIPE loop safe).
  - Read-only view for the control plane.
"""

from __future__ import annotations

import threading
from collections import deque
from typing import Any

# ---------------------------------------------------------------------------
# WalletClusterStore
# ---------------------------------------------------------------------------

DEFAULT_MAXLEN = 100


class WalletClusterStore:
    """Thread-safe bounded ring of per-mint wallet-cluster graph records.

    Each record is a plain dict matching the WalletClusterGraph schema in
    aats.control_plane.wallet_cluster_schemas.  Dicts are used here to keep
    the controller free of an import dependency on the control-plane schema
    (direction: controller -> control_plane imports are forbidden; controller
    imports contracts only).

    Usage (SNIPE/SLOW loop — after microstructure features are available):
        store = WalletClusterStore()
        store.record(
            mint=mint,
            evaluated_at_slot=event_time.slot,
            sniper_cluster_score=features.sniper_cluster_score,
            bundling_detected=features.bundling_detected,
            bundler_wallet_count=features.bundler_wallet_count,
            dev_bundle_cluster_bps=safety_inputs.dev_bundle_cluster_bps or 0,
            nodes=[...],
            edges=[...],
            manipulation_flags=[...],
        )

    Usage (control-plane GET /api/wallet-cluster):
        records = store.snapshot()   # list[dict], newest-first by slot
    """

    def __init__(self, maxlen: int = DEFAULT_MAXLEN) -> None:
        self._lock = threading.RLock()
        self._ring: deque[dict[str, Any]] = deque(maxlen=maxlen)

    def record(
        self,
        *,
        mint: str,
        evaluated_at_slot: int,
        sniper_cluster_score: float,
        bundling_detected: bool,
        bundler_wallet_count: int,
        dev_bundle_cluster_bps: int,
        nodes: list[dict[str, Any]] | None = None,
        edges: list[dict[str, Any]] | None = None,
        manipulation_flags: list[str] | None = None,
    ) -> None:
        """Append one cluster graph record.

        Args:
            mint:                   Token mint address.
            evaluated_at_slot:      Event-time slot (C-5) — never compute-time.
            sniper_cluster_score:   [0.0, 1.0] from MicrostructureFeatures.
            bundling_detected:      True if bundler_wallet_count >= 3.
            bundler_wallet_count:   Count of same-block bundle wallets.
            dev_bundle_cluster_bps: Largest cluster holding in bps (0..10000).
            nodes:                  List of WalletNode dicts (may be empty list).
            edges:                  List of WalletEdge dicts (may be empty list).
            manipulation_flags:     List of ManipulationFlag strings.

        Non-blocking: safe to call from the SNIPE/SLOW loop (no await, no I/O).
        """
        entry: dict[str, Any] = {
            "mint": mint,
            "evaluated_at_slot": evaluated_at_slot,
            "sniper_cluster_score": float(sniper_cluster_score),
            "bundling_detected": bool(bundling_detected),
            "bundler_wallet_count": int(bundler_wallet_count),
            "dev_bundle_cluster_bps": int(dev_bundle_cluster_bps),
            "nodes": nodes if nodes is not None else [],
            "edges": edges if edges is not None else [],
            "manipulation_flags": manipulation_flags if manipulation_flags is not None else [],
        }
        with self._lock:
            self._ring.append(entry)

    def snapshot(self, *, newest_first: bool = True) -> list[dict[str, Any]]:
        """Return a shallow copy of all current cluster records.

        Args:
            newest_first: if True (default), sort newest (highest slot) first.

        Returns:
            list[dict] — safe to iterate outside the lock.  Each dict matches
            the WalletClusterGraph schema.  Mutating the list does not affect
            the ring.
        """
        with self._lock:
            items = list(self._ring)
        if newest_first:
            items = sorted(items, key=lambda r: r["evaluated_at_slot"], reverse=True)
        else:
            items = sorted(items, key=lambda r: r["evaluated_at_slot"])
        return items

    def __len__(self) -> int:
        with self._lock:
            return len(self._ring)
