/**
 * E7 — narrative & news wire adapter tests.
 *
 * Pinned to the AUTHORITATIVE wire shape: NewsSignal
 * (aats.sentiment.models.NewsSignal), returned verbatim by GET /api/narrative.
 *
 * The load-bearing invariant this file GUARDS is the DE-RISK clamp: the backend
 * formula guarantees mcs_delta <= 0 (news can only LOWER conviction), but the
 * adapter must ENFORCE [-1, 0] itself so a malformed or hostile frame can NEVER
 * surface a positive (buy-like) narrative score. It also pins the verbatim
 * red-flag passthrough (never miss a warning), the unknown-credibility-tier
 * fallback to the LOWEST trust (de-risk bias), and the honest token/asset
 * mapping. No money-as-float field exists.
 */
import { describe, expect, it } from "vitest";
import {
  adaptNarrative,
  adaptNarrativeSignal,
  type WireNarrativeSignal,
} from "./adapters";

describe("adaptNarrativeSignal — wire (NewsSignal) → view-model", () => {
  it("maps the NewsSignal frame to the camelCase view-model", () => {
    const wire: WireNarrativeSignal = {
      asset: "Abc123pump",
      token: "RUGGY",
      decision_time_ms: 1718500000000,
      freshness_ms: 38000,
      total_articles: 6,
      negative_article_count: 3,
      mcs_delta: -0.5,
      credibility_weight: 2.0,
      credible_negative_event: true,
      red_flags: ["credible_negative_news_event", "tier1_outlet_negative"],
      headline_digest: "[QUOTED UNTRUSTED DATA] exploit reported",
      posts_excluded_future: 1,
      sources: [
        {
          outlet: "CoinDesk",
          outlet_id: "coindesk",
          credibility: "tier_1",
          negative: true,
          breaking: true,
          article_count: 2,
        },
      ],
    };
    const s = adaptNarrativeSignal(wire);
    expect(s).toEqual({
      asset: "Abc123pump",
      token: "RUGGY",
      decisionTimeMs: 1718500000000,
      freshnessMs: 38000,
      totalArticles: 6,
      negativeArticleCount: 3,
      narrativeScore: -0.5,
      credibilityWeight: 2.0,
      credibleNegativeEvent: true,
      redFlags: ["credible_negative_news_event", "tier1_outlet_negative"],
      headlineDigest: "[QUOTED UNTRUSTED DATA] exploit reported",
      postsExcludedFuture: 1,
      sources: [
        {
          outlet: "CoinDesk",
          outletId: "coindesk",
          credibility: "tier_1",
          negative: true,
          breaking: true,
          articleCount: 2,
        },
      ],
    });
  });

  it("DE-RISK INVARIANT: a positive mcs_delta is clamped to 0 (never a buy signal)", () => {
    // A hostile/malformed frame sends a positive delta — the adapter REFUSES it.
    expect(adaptNarrativeSignal({ mcs_delta: 0.8 }).narrativeScore).toBe(0);
    expect(adaptNarrativeSignal({ mcs_delta: 1.0 }).narrativeScore).toBe(0);
    expect(adaptNarrativeSignal({ mcs_delta: 999 }).narrativeScore).toBe(0);
  });

  it("DE-RISK INVARIANT: an out-of-range negative is clamped to -1 (total collapse)", () => {
    expect(adaptNarrativeSignal({ mcs_delta: -5 }).narrativeScore).toBe(-1);
    expect(adaptNarrativeSignal({ mcs_delta: -1 }).narrativeScore).toBe(-1);
  });

  it("a non-finite / absent delta is treated as 0 (no impact)", () => {
    expect(adaptNarrativeSignal({}).narrativeScore).toBe(0);
    expect(adaptNarrativeSignal({ mcs_delta: NaN }).narrativeScore).toBe(0);
  });

  it("surfaces unknown news red-flag codes verbatim (never drops a warning)", () => {
    const s = adaptNarrativeSignal({
      red_flags: ["coordinated_shill", "brand_new_news_fingerprint"],
    });
    expect(s.redFlags).toEqual([
      "coordinated_shill",
      "brand_new_news_fingerprint",
    ]);
  });

  it("an unknown credibility tier falls back to the LOWEST trust (tier_3), never higher", () => {
    const s = adaptNarrativeSignal({
      sources: [
        { outlet: "Mystery Blog", outlet_id: "mb", credibility: "tier_99" },
        { outlet: "No tier", outlet_id: "nt" },
      ],
    });
    expect(s.sources[0].credibility).toBe("tier_3");
    expect(s.sources[1].credibility).toBe("tier_3");
  });

  it("displays the asset when the wire carries no separate token name", () => {
    expect(adaptNarrativeSignal({ asset: "Xyzpump" }).token).toBe("Xyzpump");
  });

  it("clamps counts/weights non-negative and freshness null when absent", () => {
    const s = adaptNarrativeSignal({
      total_articles: -3,
      negative_article_count: -1,
      credibility_weight: -2,
      posts_excluded_future: -9,
    });
    expect(s.totalArticles).toBe(0);
    expect(s.negativeArticleCount).toBe(0);
    expect(s.credibilityWeight).toBe(0);
    expect(s.postsExcludedFuture).toBe(0);
    expect(s.freshnessMs).toBeNull();
  });

  it("never throws on a sparse frame", () => {
    expect(() => adaptNarrativeSignal({})).not.toThrow();
  });

  it("adaptNarrative maps an array and tolerates an empty/absent list", () => {
    expect(adaptNarrative([])).toEqual([]);
    expect(
      adaptNarrative([{ asset: "Apump", mcs_delta: -0.2 }]).map(
        (s) => s.narrativeScore,
      ),
    ).toEqual([-0.2]);
  });
});
