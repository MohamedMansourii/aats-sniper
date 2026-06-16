import { z } from "zod";
import { createRouter, publicQuery } from "../middleware";
import { getDb } from "../queries/connection";
import { marketData, technicalFeatures } from "@db/schema";
import { eq, desc } from "drizzle-orm";

export const marketRouter = createRouter({
  ingest: publicQuery
    .input(
      z.object({
        symbol: z.string().min(1),
        timestamp: z.number(),
        open: z.number(),
        high: z.number(),
        low: z.number(),
        close: z.number(),
        volume: z.number(),
      })
    )
    .mutation(async ({ input }) => {
      const db = getDb();
      const result = await db.insert(marketData).values(input);
      return { success: true, id: Number(result[0].insertId) };
    }),

  ingestBulk: publicQuery
    .input(
      z.array(
        z.object({
          symbol: z.string(),
          timestamp: z.number(),
          open: z.number(),
          high: z.number(),
          low: z.number(),
          close: z.number(),
          volume: z.number(),
        })
      )
    )
    .mutation(async ({ input }) => {
      const db = getDb();
      if (input.length === 0) return { success: true, inserted: 0 };
      await db.insert(marketData).values(input);
      return { success: true, inserted: input.length };
    }),

  latest: publicQuery
    .input(
      z.object({
        symbol: z.string(),
        limit: z.number().min(1).max(500).default(100),
      })
    )
    .query(async ({ input }) => {
      const db = getDb();
      return db
        .select()
        .from(marketData)
        .where(eq(marketData.symbol, input.symbol))
        .orderBy(desc(marketData.timestamp))
        .limit(input.limit);
    }),

  range: publicQuery
    .input(
      z.object({
        symbol: z.string(),
        from: z.number(),
        to: z.number(),
      })
    )
    .query(async ({ input }) => {
      const db = getDb();
      return db
        .select()
        .from(marketData)
        .where(eq(marketData.symbol, input.symbol))
        .orderBy(desc(marketData.timestamp));
    }),

  computeFeatures: publicQuery
    .input(
      z.object({
        symbol: z.string(),
        window: z.number().default(100),
      })
    )
    .mutation(async ({ input }) => {
      const db = getDb();
      const candles = await db
        .select()
        .from(marketData)
        .where(eq(marketData.symbol, input.symbol))
        .orderBy(desc(marketData.timestamp))
        .limit(input.window);

      if (candles.length < 50) {
        return { success: false, error: "Insufficient data (need 50+, got " + candles.length + ")" };
      }

      const sorted = [...candles].sort((a, b) => a.timestamp - b.timestamp);
      const features = calculateTechnicalFeatures(sorted);
      const lastCandle = sorted[sorted.length - 1];
      const result = await db.insert(technicalFeatures).values({
        marketDataId: lastCandle.id,
        symbol: input.symbol,
        timestamp: lastCandle.timestamp,
        ...features,
      });

      return { success: true, features, id: Number(result[0].insertId) };
    }),

  latestFeatures: publicQuery
    .input(z.object({ symbol: z.string() }))
    .query(async ({ input }) => {
      const db = getDb();
      const rows = await db
        .select()
        .from(technicalFeatures)
        .where(eq(technicalFeatures.symbol, input.symbol))
        .orderBy(desc(technicalFeatures.timestamp))
        .limit(1);
      return rows[0] ?? null;
    }),

  featureHistory: publicQuery
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
        .from(technicalFeatures)
        .where(eq(technicalFeatures.symbol, input.symbol))
        .orderBy(desc(technicalFeatures.timestamp))
        .limit(input.limit);
    }),

  stats: publicQuery
    .input(z.object({ symbol: z.string() }))
    .query(async ({ input }) => {
      const db = getDb();
      const rows = await db
        .select()
        .from(marketData)
        .where(eq(marketData.symbol, input.symbol))
        .orderBy(desc(marketData.timestamp))
        .limit(1);

      const allRows = await db
        .select()
        .from(marketData)
        .where(eq(marketData.symbol, input.symbol));

      return {
        count: allRows.length,
        latestClose: rows[0]?.close ?? 0,
        avgVolume: allRows.length > 0 ? allRows.reduce((s, c) => s + c.volume, 0) / allRows.length : 0,
        maxHigh: allRows.length > 0 ? Math.max(...allRows.map((c) => c.high)) : 0,
        minLow: allRows.length > 0 ? Math.min(...allRows.map((c) => c.low)) : 0,
      };
    }),
});

function calculateTechnicalFeatures(candles: any[]) {
  const closes = candles.map((c) => c.close);
  const volumes = candles.map((c) => c.volume);
  const n = closes.length;

  const rsi = calculateRSI(closes, 14);
  const ema12 = calculateEMA(closes, 12);
  const ema26 = calculateEMA(closes, 26);
  const macdLine = ema12.map((v, i) => v - ema26[i]);
  const macdSignal = calculateEMA(macdLine, 9);
  const macdHist = macdLine.map((v, i) => v - macdSignal[i]);
  const bbPos = calculateBBPosition(closes, 20);
  const volSMA = calculateSMA(volumes, 20);
  const volumeRatio = volumes[n - 1] / (volSMA[volSMA.length - 1] || 1);
  const returns = closes.slice(1).map((c, i) => (c - closes[i]) / closes[i]);
  const vol = Math.sqrt(returns.slice(-14).reduce((s, r) => s + r * r, 0) / 14) * Math.sqrt(365 * 24 * 12);
  const priceChange1h = n >= 12 ? (closes[n - 1] - closes[n - 12]) / closes[n - 12] : 0;
  const priceChange24h = n >= 288 ? (closes[n - 1] - closes[n - 288]) / closes[n - 288] : 0;

  return {
    rsi14: rsi[rsi.length - 1],
    macdLine: macdLine[macdLine.length - 1],
    macdSignal: macdSignal[macdSignal.length - 1],
    macdHist: macdHist[macdHist.length - 1],
    bbPosition: bbPos,
    volumeRatio: isFinite(volumeRatio) ? volumeRatio : 1,
    volatilityAnnualized: isFinite(vol) ? vol : 0,
    priceChange1h,
    priceChange24h,
  };
}

function calculateRSI(closes: number[], period: number): number[] {
  const rsi: number[] = [];
  let gains = 0, losses = 0;
  for (let i = 1; i <= period && i < closes.length; i++) {
    const diff = closes[i] - closes[i - 1];
    if (diff > 0) gains += diff; else losses -= diff;
  }
  for (let i = 0; i < period; i++) rsi.push(50);
  for (let i = period; i < closes.length; i++) {
    const diff = closes[i] - closes[i - 1];
    const gain = diff > 0 ? diff : 0;
    const loss = diff < 0 ? -diff : 0;
    gains = (gains * (period - 1) + gain) / period;
    losses = (losses * (period - 1) + loss) / period;
    const rs = losses === 0 ? 100 : gains / losses;
    rsi.push(100 - 100 / (1 + rs));
  }
  return rsi;
}

function calculateEMA(values: number[], period: number): number[] {
  const k = 2 / (period + 1);
  const ema: number[] = [values[0]];
  for (let i = 1; i < values.length; i++) {
    ema.push(values[i] * k + ema[i - 1] * (1 - k));
  }
  return ema;
}

function calculateSMA(values: number[], period: number): number[] {
  const sma: number[] = [];
  for (let i = 0; i < values.length; i++) {
    const start = Math.max(0, i - period + 1);
    const slice = values.slice(start, i + 1);
    sma.push(slice.reduce((a, b) => a + b, 0) / slice.length);
  }
  return sma;
}

function calculateBBPosition(closes: number[], period: number): number {
  const slice = closes.slice(-period);
  const mean = slice.reduce((a, b) => a + b, 0) / period;
  const std = Math.sqrt(slice.reduce((s, v) => s + (v - mean) ** 2, 0) / period);
  const upper = mean + 2 * std;
  const lower = mean - 2 * std;
  const range = upper - lower;
  return range === 0 ? 0.5 : Math.max(0, Math.min(1, (closes[closes.length - 1] - lower) / range));
}
