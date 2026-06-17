"""E11 — Wallet-cluster graph schemas for GET /api/wallet-cluster.

Read-only operator view of the EXISTING sniper-cluster / bundle-detection
data already computed by:
  - aats.features.microstructure (sniper_cluster_score, bundler_wallet_count,
    bundling_detected, sniper_wallet_count, sniper_supply_share_bps)
  - aats.risk.pretrade_gate (dev_bundle_cluster_bps, SafetyInputs)
  - aats.contracts.events (bundler_cluster_id, creator_wallet)

No new detection logic is introduced here.  This module only defines the
typed wire schema for the graph projection the operator views.

Graph model:
  nodes  — wallet addresses with type, manipulation flags, and supply share.
  edges  — detected connections between wallets (shared funder, same-bundle,
           co-timing, etc.).

HARD RULES:
  - Read-only (GET only).  No POST or control action.
  - No win_rate field anywhere (HONESTY CLAUSE, AC-037).
  - No money fields (supply percentages are bps: int, not lamports).
  - No secrets, keys, or private data.
  - manipulation_flags are ONLY the set that lowers safety / adds de-risk signal.
    A flag cannot increase risk tolerance or trigger entry.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

# ---------------------------------------------------------------------------
# Node types — what category of actor this wallet is in the cluster
# ---------------------------------------------------------------------------

WalletNodeType = Literal[
    "creator",       # the deployer / developer wallet
    "bundler",       # detected same-bundle buyer (same-block funded from creator)
    "sniper",        # early buyer in first-K-slots window
    "holder",        # top-10 holder (not a bundler or sniper)
    "unknown",       # observed but not classified
]

# ---------------------------------------------------------------------------
# Edge types — the detected relationship between two wallets
# ---------------------------------------------------------------------------

WalletEdgeType = Literal[
    "same_bundle",       # bought in same block / bundle (correlated timing)
    "shared_funder",     # common ancestor funding source detected
    "co_timing",         # statistically co-timed buys (high synchronicity)
    "creator_funded",    # creator wallet directly funded the buyer
]

# ---------------------------------------------------------------------------
# Manipulation flags — de-risk-only signals (a present flag LOWERS safety)
# ---------------------------------------------------------------------------

ManipulationFlag = Literal[
    "bundling_detected",     # 3+ wallets in same-block coordinated buys
    "high_cluster_share",    # cluster holds > threshold% of supply (bps threshold)
    "freeze_authority_set",  # freeze authority not renounced (from pretrade gate)
    "high_synchronicity",    # sniper_cluster_score > 0.7 (strong coordination)
]


# ---------------------------------------------------------------------------
# WalletNode — one vertex in the cluster graph
# ---------------------------------------------------------------------------


class WalletNode(BaseModel):
    """One wallet in the cluster graph.

    All supply/share quantities are basis points (int, 0..10000), never float.
    No money (lamports) — supply share is a ratio, not a monetary quantity.
    """

    wallet: str                         # public key (display only; never a signing key)
    node_type: WalletNodeType           # role in the cluster
    supply_share_bps: int               # token supply share held (bps, 0..10000)
    buy_slot: int | None                # slot of buy event; None if not a buyer
    manipulation_flags: list[ManipulationFlag] = Field(default_factory=list)

    model_config = {"frozen": True}

    @field_validator("supply_share_bps")
    @classmethod
    def _bps_range(cls, v: int) -> int:
        if not (0 <= v <= 10_000):
            raise ValueError(f"supply_share_bps must be in [0, 10000], got {v}")
        return v


# ---------------------------------------------------------------------------
# WalletEdge — one connection between two wallet nodes
# ---------------------------------------------------------------------------


class WalletEdge(BaseModel):
    """One detected connection between two wallets in the cluster graph.

    source / target are wallet public keys (must appear as WalletNode.wallet).
    weight is a [0.0, 1.0] float — a statistical correlation strength.
    Float is correct here (weight is a statistical quantity, not money).
    """

    source: str                     # source wallet pubkey
    target: str                     # target wallet pubkey
    edge_type: WalletEdgeType       # nature of the detected connection
    weight: float                   # correlation strength [0.0, 1.0] (float, not money)

    model_config = {"frozen": True}

    @field_validator("weight")
    @classmethod
    def _weight_range(cls, v: float) -> float:
        if not (0.0 <= v <= 1.0):
            raise ValueError(f"edge weight must be in [0.0, 1.0], got {v}")
        return v


# ---------------------------------------------------------------------------
# WalletClusterGraph — the per-mint graph returned by GET /api/wallet-cluster
# ---------------------------------------------------------------------------


class WalletClusterGraph(BaseModel):
    """Per-mint wallet-connection graph for the operator read-only view.

    Exposes the sniper-cluster / bundle-detection data already computed by the
    feature pipeline (microstructure.py) and the pre-trade gate (pretrade_gate.py)
    as a structured graph the dashboard can render (Bubble Maps style).

    Fields:
        mint:                  Token mint address this graph describes.
        evaluated_at_slot:     Event-time slot (C-5) at which the cluster was
                               detected — never compute-time.
        sniper_cluster_score:  [0.0, 1.0] float from MicrostructureFeatures
                               (0=no snipers, 1=fully coordinated cluster).
                               Float is correct (statistical score, not money).
        bundling_detected:     True if >= 3 wallets bought in same-block bundle.
        bundler_wallet_count:  int count of bundler wallets (0 if no bundling).
        dev_bundle_cluster_bps: Largest single cluster holding in bps (int).
                               From SafetyInputs.dev_bundle_cluster_bps.
        nodes:                 Wallet vertices with type and supply share.
        edges:                 Detected connections between wallet pairs.
        manipulation_flags:    Summary set of all triggered manipulation flags
                               across the entire graph (union of node flags +
                               graph-level flags like bundling_detected).

    HARD RULES:
        - No money fields (dev_bundle_cluster_bps is bps: int, not lamports).
        - No win_rate field (HONESTY CLAUSE).
        - Read-only view: no field can trigger, size-up, or widen a stop.
    """

    mint: str
    evaluated_at_slot: int          # event-time slot (C-5) — never compute-time
    sniper_cluster_score: float     # [0.0, 1.0] statistical score (float OK, not money)
    bundling_detected: bool
    bundler_wallet_count: int       # int count (0..N)
    dev_bundle_cluster_bps: int     # int bps (0..10000)
    nodes: list[WalletNode] = Field(default_factory=list)
    edges: list[WalletEdge] = Field(default_factory=list)
    manipulation_flags: list[ManipulationFlag] = Field(default_factory=list)

    model_config = {"frozen": True}

    @field_validator("sniper_cluster_score")
    @classmethod
    def _score_range(cls, v: float) -> float:
        if not (0.0 <= v <= 1.0):
            raise ValueError(f"sniper_cluster_score must be in [0.0, 1.0], got {v}")
        return v

    @field_validator("dev_bundle_cluster_bps")
    @classmethod
    def _bps_range(cls, v: int) -> int:
        if not (0 <= v <= 10_000):
            raise ValueError(f"dev_bundle_cluster_bps must be in [0, 10000], got {v}")
        return v
