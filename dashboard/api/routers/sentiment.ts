import { z } from "zod";
import { createRouter, publicQuery } from "../middleware";
import { getDb } from "../queries/connection";
import { sentimentScores } from "@db/schema";
import { eq, desc } from "drizzle-orm";

class SentimentEngine {
  async scoreBatch(posts: SocialPost[]): Promise<{
    conviction: number;
    momentum: number;
    novelty: number;
    redFlags: string[];
    reasoning: string;
  }> {
    const totalEngagement = posts.reduce((s, p) => s + p.engagementScore, 0);
    const bullishTerms = ['moon', 'pump', 'bull', 'hold', 'buy', 'rocket', 'lambo', 'gem'];
    const bearishTerms = ['dump', 'bear', 'rug', 'sell', 'crash', 'scam', 'panic'];

    let bullish = 0, bearish = 0;
    for (const post of posts) {
      const text = post.text.toLowerCase();
      for (const term of bullishTerms) if (text.includes(term)) bullish++;
      for (const term of bearishTerms) if (text.includes(term)) bearish++;
    }

    const total = bullish + bearish || 1;
    const rawConviction = (bullish - bearish) / total;

    const redFlags: string[] = [];
    const rugSignals = posts.filter(p =>
      p.text.toLowerCase().includes('dev selling') ||
      p.text.toLowerCase().includes('liquidity removed')
    );
    if (rugSignals.length > 0) redFlags.push('POTENTIAL_RUG_PULL_SIGNALS');

    const botSignals = posts.filter(p => p.engagementScore < 10 && p.text.length < 30);
    if (botSignals.length / posts.length > 0.5) redFlags.push('BOT_FARM_DETECTED');

    return {
      conviction: Math.max(-1, Math.min(1, rawConviction)),
      momentum: Math.min(1, totalEngagement / (posts.length * 1000)),
      novelty: Math.random() * 0.5 + 0.25,
      redFlags,
      reasoning: `Analyzed ${posts.length} posts: ${bullish} bullish vs ${bearish} bearish signals. ${redFlags.length > 0 ? 'RED FLAGS: ' + redFlags.join(', ') : 'No red flags.'}`,
    };
  }
}

interface SocialPost {
  source: string;
  text: string;
  engagementScore: number;
}

const engine = new SentimentEngine();

export const sentimentRouter = createRouter({
  score: publicQuery
    .input(
      z.object({
        symbol: z.string(),
        posts: z.array(
          z.object({
            source: z.enum(["twitter", "reddit", "telegram"]),
            text: z.string(),
            authorFollowers: z.number().default(0),
            engagementScore: z.number(),
            timestamp: z.number(),
          })
        ),
        timestamp: z.number(),
      })
    )
    .mutation(async ({ input }) => {
      const db = getDb();
      const result = await engine.scoreBatch(input.posts);

      const dbResult = await db.insert(sentimentScores).values({
        symbol: input.symbol,
        timestamp: input.timestamp,
        conviction: result.conviction,
        momentum: result.momentum,
        novelty: result.novelty,
        redFlags: result.redFlags,
        reasoning: result.reasoning,
        weightedEngagement: input.posts.reduce((s, p) => s + p.engagementScore, 0),
        postCount: input.posts.length,
        fallback: false,
      });

      return {
        success: true,
        mcs: result,
        id: Number(dbResult[0].insertId),
      };
    }),

  latest: publicQuery
    .input(z.object({ symbol: z.string() }))
    .query(async ({ input }) => {
      const db = getDb();
      const rows = await db
        .select()
        .from(sentimentScores)
        .where(eq(sentimentScores.symbol, input.symbol))
        .orderBy(desc(sentimentScores.timestamp))
        .limit(1);
      return rows[0] ?? null;
    }),

  history: publicQuery
    .input(
      z.object({
        symbol: z.string(),
        limit: z.number().default(100),
      })
    )
    .query(async ({ input }) => {
      const db = getDb();
      return db
        .select()
        .from(sentimentScores)
        .where(eq(sentimentScores.symbol, input.symbol))
        .orderBy(desc(sentimentScores.timestamp))
        .limit(input.limit);
    }),

  trend: publicQuery
    .input(
      z.object({
        symbol: z.string(),
        periods: z.number().default(24),
      })
    )
    .query(async ({ input }) => {
      const db = getDb();
      const rows = await db
        .select()
        .from(sentimentScores)
        .where(eq(sentimentScores.symbol, input.symbol))
        .orderBy(desc(sentimentScores.timestamp))
        .limit(input.periods);

      if (rows.length < 2) return { trend: "flat" as const, slope: 0 };

      const convictions = rows.map((r) => r.conviction).reverse();
      const n = convictions.length;
      const xMean = (n - 1) / 2;
      const yMean = convictions.reduce((a, b) => a + b, 0) / n;
      const numerator = convictions.reduce((s, y, x) => s + (x - xMean) * (y - yMean), 0);
      const denominator = convictions.reduce((s, _, x) => s + (x - xMean) ** 2, 0);
      const slope = denominator === 0 ? 0 : numerator / denominator;

      return {
        trend: slope > 0.02 ? "rising" as const : slope < -0.02 ? "falling" as const : "flat" as const,
        slope,
        latest: convictions[convictions.length - 1],
        average: yMean,
      };
    }),
});
