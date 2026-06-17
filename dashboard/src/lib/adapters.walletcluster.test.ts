/**
 * E11 — wallet-cluster ("Bubble Maps") wire adapter tests.
 *
 * Pinned to the AUTHORITATIVE wire shape: WalletClusterGraph
 * (aats.control_plane.wallet_cluster_schemas.py), returned verbatim by
 * GET /api/wallet-cluster. A drift between the adapter and the real schema must
 * FAIL here.
 *
 * Pins (all load-bearing):
 *  - snake_case → camelCase field mapping for the graph, its nodes and edges;
 *  - share quantities are basis points (int 0..10000), NOT money — clamped to
 *    range; scores/weights are statistical floats clamped to [0,1];
 *  - a dangling edge (an endpoint with no node) is DROPPED — an operator must
 *    never see a connection drawn against a phantom wallet;
 *  - the closed node/edge type sets fall back to the safest neutral VIEW value
 *    ("unknown" / "co_timing"), never an invented manipulation category;
 *  - manipulation flags are surfaced verbatim (never dropped) — an operator
 *    must never miss a manipulation warning;
 *  - NO win-rate field exists anywhere (HONESTY CLAUSE);
 *  - the wire has no token name → token := mint (never fabricated).
 */
import { describe, expect, it } from "vitest";
import {
  adaptWalletClusterGraph,
  adaptWalletClusters,
  type WireWalletClusterGraph,
} from "./adapters";

describe("adaptWalletClusterGraph — wire (WalletClusterGraph) → view-model", () => {
  it("maps the schema frame (graph + nodes + edges) to the camelCase view-model", () => {
    const wire: WireWalletClusterGraph = {
      mint: "Scam111pump",
      evaluated_at_slot: 287_443_120,
      sniper_cluster_score: 0.86,
      bundling_detected: true,
      bundler_wallet_count: 3,
      dev_bundle_cluster_bps: 4200,
      nodes: [
        {
          wallet: "Creator11",
          node_type: "creator",
          supply_share_bps: 1500,
          buy_slot: null,
          manipulation_flags: ["bundling_detected", "freeze_authority_set"],
        },
        {
          wallet: "Bundler22",
          node_type: "bundler",
          supply_share_bps: 900,
          buy_slot: 287_443_120,
          manipulation_flags: ["bundling_detected"],
        },
      ],
      edges: [
        {
          source: "Creator11",
          target: "Bundler22",
          edge_type: "creator_funded",
          weight: 0.95,
        },
      ],
      manipulation_flags: ["bundling_detected", "high_cluster_share"],
    };
    const g = adaptWalletClusterGraph(wire);
    expect(g).toEqual({
      mint: "Scam111pump",
      token: "Scam111pump", // the wire has no separate token name
      evaluatedAtSlot: 287_443_120,
      sniperClusterScore: 0.86,
      bundlingDetected: true,
      bundlerWalletCount: 3,
      devBundleClusterBps: 4200,
      nodes: [
        {
          wallet: "Creator11",
          nodeType: "creator",
          supplyShareBps: 1500,
          buySlot: null,
          manipulationFlags: ["bundling_detected", "freeze_authority_set"],
        },
        {
          wallet: "Bundler22",
          nodeType: "bundler",
          supplyShareBps: 900,
          buySlot: 287_443_120,
          manipulationFlags: ["bundling_detected"],
        },
      ],
      edges: [
        {
          source: "Creator11",
          target: "Bundler22",
          edgeType: "creator_funded",
          weight: 0.95,
        },
      ],
      manipulationFlags: ["bundling_detected", "high_cluster_share"],
    });
  });

  it("DROPS a dangling edge (endpoint with no node) — never a phantom connection", () => {
    const g = adaptWalletClusterGraph({
      mint: "M",
      nodes: [{ wallet: "A", node_type: "holder", supply_share_bps: 100 }],
      edges: [
        // A→A endpoint exists; B does not → dropped.
        { source: "A", target: "B", edge_type: "co_timing", weight: 0.5 },
        // both missing → dropped.
        { source: "X", target: "Y", edge_type: "same_bundle", weight: 0.9 },
      ],
    });
    expect(g.edges).toEqual([]);
  });

  it("keeps an edge only when BOTH endpoints are real nodes", () => {
    const g = adaptWalletClusterGraph({
      mint: "M",
      nodes: [
        { wallet: "A", node_type: "creator", supply_share_bps: 100 },
        { wallet: "B", node_type: "sniper", supply_share_bps: 50 },
      ],
      edges: [{ source: "A", target: "B", edge_type: "shared_funder", weight: 0.7 }],
    });
    expect(g.edges).toHaveLength(1);
    expect(g.edges[0]).toMatchObject({ source: "A", target: "B" });
  });

  it("clamps supply share bps into [0, 10000] (a ratio, never money)", () => {
    const g = adaptWalletClusterGraph({
      mint: "M",
      dev_bundle_cluster_bps: 99_999,
      nodes: [
        { wallet: "A", node_type: "holder", supply_share_bps: 50_000 },
        { wallet: "B", node_type: "holder", supply_share_bps: -10 },
      ],
    });
    expect(g.devBundleClusterBps).toBe(10_000);
    expect(g.nodes[0].supplyShareBps).toBe(10_000);
    expect(g.nodes[1].supplyShareBps).toBe(0);
  });

  it("clamps cluster score and edge weight into [0, 1]", () => {
    const g = adaptWalletClusterGraph({
      mint: "M",
      sniper_cluster_score: 1.5,
      nodes: [
        { wallet: "A", node_type: "creator", supply_share_bps: 10 },
        { wallet: "B", node_type: "sniper", supply_share_bps: 10 },
      ],
      edges: [{ source: "A", target: "B", edge_type: "co_timing", weight: 9 }],
    });
    expect(g.sniperClusterScore).toBe(1);
    expect(g.edges[0].weight).toBe(1);
  });

  it("an unknown node type falls back to 'unknown' (a neutral, non-manipulation role)", () => {
    const g = adaptWalletClusterGraph({
      mint: "M",
      nodes: [{ wallet: "A", node_type: "whale", supply_share_bps: 100 }],
    });
    expect(g.nodes[0].nodeType).toBe("unknown");
  });

  it("an unknown edge type falls back to the weakest relation ('co_timing'), never invented", () => {
    const g = adaptWalletClusterGraph({
      mint: "M",
      nodes: [
        { wallet: "A", node_type: "creator", supply_share_bps: 10 },
        { wallet: "B", node_type: "holder", supply_share_bps: 10 },
      ],
      edges: [{ source: "A", target: "B", edge_type: "telepathy", weight: 0.5 }],
    });
    expect(g.edges[0].edgeType).toBe("co_timing");
  });

  it("surfaces unknown manipulation-flag codes verbatim (never drops a warning)", () => {
    const g = adaptWalletClusterGraph({
      mint: "M",
      manipulation_flags: ["bundling_detected", "brand_new_fingerprint"],
      nodes: [
        {
          wallet: "A",
          node_type: "creator",
          supply_share_bps: 10,
          manipulation_flags: ["some_future_flag"],
        },
      ],
    });
    expect(g.manipulationFlags).toEqual([
      "bundling_detected",
      "brand_new_fingerprint",
    ]);
    expect(g.nodes[0].manipulationFlags).toEqual(["some_future_flag"]);
  });

  it("keeps buy_slot null when absent (a non-buyer wallet), never faked to 0", () => {
    const g = adaptWalletClusterGraph({
      mint: "M",
      nodes: [
        { wallet: "A", node_type: "creator", supply_share_bps: 10 },
        { wallet: "B", node_type: "sniper", supply_share_bps: 10, buy_slot: 42 },
      ],
    });
    expect(g.nodes[0].buySlot).toBeNull();
    expect(g.nodes[1].buySlot).toBe(42);
  });

  it("carries no win-rate field anywhere (HONESTY CLAUSE)", () => {
    const g = adaptWalletClusterGraph({ mint: "M" });
    expect(JSON.stringify(g)).not.toMatch(/win.?rate/i);
  });

  it("never throws on a sparse / empty frame and defaults to a safe empty graph", () => {
    expect(() => adaptWalletClusterGraph({})).not.toThrow();
    const g = adaptWalletClusterGraph({});
    expect(g).toMatchObject({
      mint: "",
      token: "",
      sniperClusterScore: 0,
      bundlingDetected: false,
      bundlerWalletCount: 0,
      devBundleClusterBps: 0,
      nodes: [],
      edges: [],
      manipulationFlags: [],
    });
  });

  it("adaptWalletClusters maps an array and tolerates an empty/absent list", () => {
    expect(adaptWalletClusters([])).toEqual([]);
    expect(
      adaptWalletClusters([{ mint: "Apump" }, { mint: "Bpump" }]).map(
        (g) => g.mint,
      ),
    ).toEqual(["Apump", "Bpump"]);
  });
});
