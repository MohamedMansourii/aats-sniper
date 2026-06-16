import { z } from "zod";
import { createRouter, publicQuery } from "../middleware";
import { getDb } from "../queries/connection";
import { decisions } from "@db/schema";
import { eq, desc } from "drizzle-orm";

class ReasoningEngine {
  private history: Array<{ decision: string; confidence: number; reasoning: string; timestamp: number }> = [];

  async reason(
    quantPrediction: {
      prediction15m: string;
      prediction1h: string;
      prediction4h: string;
      confidence15m: number;
      confidence1h: number;
      confidence4h: number;
    },
    sentimentFeatures: {
      conviction: number;
      momentum: number;
      novelty: number;
      redFlags: string[] | null;
    },
    _marketFeatures: {
      rsi14: number;
      volumeRatio: number;
      bbPosition: number;
    }
  ): Promise<{
    decision: string;
    confidence: number;
    reasoning: string;
    overrideTriggered: boolean;
    overrideReason: string | null;
    timeframeBias: string;
    riskLevel: string;
  }> {
    const quantBullish = quantPrediction.prediction1h === "up" || quantPrediction.prediction4h === "up";
    const quantBearish = quantPrediction.prediction1h === "down" || quantPrediction.prediction4h === "down";
    const narrativeBullish = sentimentFeatures.conviction > 0.3;
    const narrativeBearish = sentimentFeatures.conviction < -0.3;

    const hasCriticalRedFlag = (sentimentFeatures.redFlags || []).some(
      (rf) => rf.includes("RUG") || rf.includes("EXPLOIT") || rf.includes("DEV")
    );

    let decision = "Hold";
    let confidence = 0.5;
    let reasoning = "";
    let overrideTriggered = false;
    let overrideReason = null;
    let riskLevel = "medium";

    if (hasCriticalRedFlag) {
      decision = "Strong Sell";
      confidence = 0.9;
      reasoning = "CRITICAL RED FLAG detected: " + sentimentFeatures.redFlags!.join(", ");
      overrideTriggered = true;
      overrideReason = "Catastrophic narrative failure — immediate exit";
      riskLevel = "extreme";
    } else if (quantBullish && narrativeBullish) {
      const conf = (quantPrediction.confidence1h + Math.abs(sentimentFeatures.conviction)) / 2;
      decision = conf > 0.7 ? "Strong Buy" : "Weak Buy";
      confidence = conf;
      reasoning = `Quantitative (${quantPrediction.prediction1h}) and narrative (conviction: ${sentimentFeatures.conviction.toFixed(2)}) align bullish. Momentum: ${sentimentFeatures.momentum.toFixed(2)}`;
      riskLevel = sentimentFeatures.momentum > 0.7 ? "medium" : "high";
    } else if (quantBearish && narrativeBearish) {
      const conf = (quantPrediction.confidence1h + Math.abs(sentimentFeatures.conviction)) / 2;
      decision = conf > 0.7 ? "Strong Sell" : "Weak Sell";
      confidence = conf;
      reasoning = `Quantitative (${quantPrediction.prediction1h}) and narrative (conviction: ${sentimentFeatures.conviction.toFixed(2)}) align bearish.`;
      riskLevel = "medium";
    } else if (quantBullish && narrativeBearish) {
      decision = "Hold";
      confidence = 0.6;
      reasoning = `DIVERGENCE: Quant predicts ${quantPrediction.prediction1h} but narrative conviction is negative (${sentimentFeatures.conviction.toFixed(2)}). Quant may be stale.`;
      riskLevel = "high";
      if (sentimentFeatures.conviction < -0.6 && quantPrediction.confidence1h < 0.5) {
        decision = "Weak Sell";
        confidence = 0.55;
      }
    } else if (quantBearish && narrativeBullish) {
      decision = "Hold";
      confidence = 0.55;
      reasoning = `DIVERGENCE: Quant predicts ${quantPrediction.prediction1h} but narrative is bullish (${sentimentFeatures.conviction.toFixed(2)}). Possible reversal.`;
      riskLevel = "high";
    } else {
      decision = "Hold";
      confidence = 0.4;
      reasoning = `Weak signals. Quant: ${quantPrediction.prediction1h} (conf: ${quantPrediction.confidence1h.toFixed(2)}), Narrative: ${sentimentFeatures.conviction.toFixed(2)}`;
      riskLevel = "low";
    }

    const timeframeBias =
      quantPrediction.prediction4h !== "sideways" && quantPrediction.confidence4h > 0.6
        ? "long"
        : quantPrediction.prediction1h !== "sideways" && quantPrediction.confidence1h > 0.6
          ? "medium"
          : "short";

    this.history.push({ decision, confidence, reasoning, timestamp: Date.now() });
    if (this.history.length > 20) this.history.shift();

    return { decision, confidence, reasoning, overrideTriggered, overrideReason, timeframeBias, riskLevel };
  }
}

const engine = new ReasoningEngine();

export const reasoningRouter = createRouter({
  decide: publicQuery
    .input(
      z.object({
        symbol: z.string(),
        quantPrediction: z.object({
          prediction15m: z.string(),
          prediction1h: z.string(),
          prediction4h: z.string(),
          confidence15m: z.number(),
          confidence1h: z.number(),
          confidence4h: z.number(),
        }),
        sentimentFeatures: z.object({
          conviction: z.number(),
          momentum: z.number(),
          novelty: z.number(),
          redFlags: z.array(z.string()).nullable(),
        }),
        marketFeatures: z.object({
          rsi14: z.number(),
          volumeRatio: z.number(),
          bbPosition: z.number(),
        }),
        timestamp: z.number(),
        quantPredictionId: z.number().optional(),
        sentimentScoreId: z.number().optional(),
      })
    )
    .mutation(async ({ input }) => {
      const start = Date.now();
      const result = await engine.reason(
        input.quantPrediction,
        input.sentimentFeatures,
        input.marketFeatures
      );
      const latency = Date.now() - start;

      const validDecisions = ["Strong Buy", "Weak Buy", "Hold", "Weak Sell", "Strong Sell"] as const;
      const decisionValue = validDecisions.includes(result.decision as any)
        ? (result.decision as typeof validDecisions[number])
        : "Hold";

      const db = getDb();
      const dbResult = await db.insert(decisions).values({
        symbol: input.symbol,
        timestamp: input.timestamp,
        signal: decisionValue,
        confidence: result.confidence,
        reasoning: result.reasoning,
        overrideTriggered: result.overrideTriggered,
        overrideReason: result.overrideReason,
        timeframeBias: result.timeframeBias as "short" | "medium" | "long",
        riskLevel: result.riskLevel as "low" | "medium" | "high" | "extreme",
        quantPredictionId: input.quantPredictionId,
        sentimentScoreId: input.sentimentScoreId,
        llmLatencyMs: latency,
      });

      return {
        success: true,
        decision: result,
        latency,
        id: Number(dbResult[0].insertId),
      };
    }),

  latest: publicQuery
    .input(z.object({ symbol: z.string() }))
    .query(async ({ input }) => {
      const db = getDb();
      const rows = await db
        .select()
        .from(decisions)
        .where(eq(decisions.symbol, input.symbol))
        .orderBy(desc(decisions.timestamp))
        .limit(1);
      return rows[0] ?? null;
    }),

  history: publicQuery
    .input(
      z.object({
        symbol: z.string(),
        limit: z.number().default(50),
      })
    )
    .query(async ({ input }) => {
      const db = getDb();
      return db
        .select()
        .from(decisions)
        .where(eq(decisions.symbol, input.symbol))
        .orderBy(desc(decisions.timestamp))
        .limit(input.limit);
    }),

  stats: publicQuery
    .input(z.object({ symbol: z.string() }))
    .query(async ({ input }) => {
      const db = getDb();
      const rows = await db
        .select()
        .from(decisions)
        .where(eq(decisions.symbol, input.symbol))
        .orderBy(desc(decisions.timestamp))
        .limit(100);

      const signalCounts: Record<string, number> = {};
      for (const r of rows) {
        signalCounts[r.signal] = (signalCounts[r.signal] || 0) + 1;
      }
      const avgConfidence = rows.reduce((s, r) => s + r.confidence, 0) / (rows.length || 1);
      const avgLatency = rows.reduce((s, r) => s + (r.llmLatencyMs || 0), 0) / (rows.length || 1);
      const overrideCount = rows.filter((r) => r.overrideTriggered).length;

      return {
        totalDecisions: rows.length,
        signalDistribution: signalCounts,
        avgConfidence: Math.round(avgConfidence * 100) / 100,
        avgLatencyMs: Math.round(avgLatency),
        overrideCount,
      };
    }),
});
