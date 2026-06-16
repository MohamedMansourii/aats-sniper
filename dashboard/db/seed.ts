import { getDb } from "../api/queries/connection";
import { marketData, sentimentScores, predictions, decisions, riskConfig } from "./schema";

async function seed() {
  const db = getDb();
  console.log("Seeding initial data...");

  // Seed OHLCV data (last 100 5-min candles for PEPE/USDT)
  const basePrice = 0.000012;
  const now = Date.now();
  const candles = [];
  let price = basePrice;

  for (let i = 100; i >= 0; i--) {
    const ts = now - i * 5 * 60 * 1000;
    const volatility = 0.02;
    const change = (Math.random() - 0.48) * volatility * price;
    price = price + change;

    const open = price;
    const close = price + (Math.random() - 0.5) * 0.01 * price;
    const high = Math.max(open, close) * (1 + Math.random() * 0.005);
    const low = Math.min(open, close) * (1 - Math.random() * 0.005);
    const volume = 50000000 + Math.random() * 50000000;

    candles.push({
      symbol: "PEPE/USDT",
      timestamp: ts,
      open,
      high,
      low,
      close,
      volume,
    });
  }

  if (candles.length > 0) {
    await db.insert(marketData).values(candles);
    console.log(`Inserted ${candles.length} OHLCV candles`);
  }

  // Seed sentiment data (last 24 readings)
  const sentiments = [];
  for (let i = 24; i >= 0; i--) {
    const conviction = Math.sin(i * 0.3) * 0.6 + (Math.random() - 0.5) * 0.2;
    sentiments.push({
      symbol: "PEPE/USDT",
      timestamp: now - i * 30 * 60 * 1000,
      conviction: Math.max(-1, Math.min(1, conviction)),
      momentum: Math.random(),
      novelty: 0.3 + Math.random() * 0.4,
      redFlags: conviction < -0.5 ? ["BEARISH_SENTIMENT_CLUSTER"] : null,
      reasoning: `Batch analysis: ${Math.random() > 0.5 ? "Bullish" : "Mixed"} signals detected across platforms`,
      weightedEngagement: 5000 + Math.random() * 15000,
      postCount: 20 + Math.floor(Math.random() * 30),
      fallback: false,
    });
  }

  if (sentiments.length > 0) {
    await db.insert(sentimentScores).values(sentiments);
    console.log(`Inserted ${sentiments.length} sentiment records`);
  }

  // Seed predictions
  const preds = [];
  for (let i = 24; i >= 0; i--) {
    const conf = 0.4 + Math.random() * 0.5;
    const directions = ["up", "sideways", "down"] as const;
    preds.push({
      symbol: "PEPE/USDT",
      timestamp: now - i * 30 * 60 * 1000,
      prediction15m: directions[Math.floor(Math.random() * 3)],
      prediction1h: directions[Math.floor(Math.random() * 3)],
      prediction4h: directions[Math.floor(Math.random() * 3)],
      confidence15m: conf - 0.1,
      confidence1h: conf,
      confidence4h: conf + 0.05,
      probDistribution: [[0.2, 0.3, 0.5], [0.3, 0.4, 0.3], [0.5, 0.3, 0.2]],
      inferenceLatencyMs: 30 + Math.floor(Math.random() * 20),
    });
  }

  if (preds.length > 0) {
    await db.insert(predictions).values(preds);
    console.log(`Inserted ${preds.length} predictions`);
  }

  // Seed decisions
  const decs = [];
  const signals = ["Strong Buy", "Weak Buy", "Hold", "Weak Sell", "Strong Sell"] as const;
  for (let i = 24; i >= 0; i--) {
    decs.push({
      symbol: "PEPE/USDT",
      timestamp: now - i * 30 * 60 * 1000,
      signal: signals[Math.floor(Math.random() * signals.length)],
      confidence: 0.3 + Math.random() * 0.6,
      reasoning: `Divergence analysis: Quant predicts ${directions[Math.floor(Math.random() * 3)]} but narrative momentum is ${Math.random() > 0.5 ? "accelerating" : "decelerating"}`,
      overrideTriggered: Math.random() > 0.9,
      overrideReason: null,
      timeframeBias: ["short", "medium", "long"][Math.floor(Math.random() * 3)] as "short" | "medium" | "long",
      riskLevel: ["low", "medium", "high", "extreme"][Math.floor(Math.random() * 4)] as "low" | "medium" | "high" | "extreme",
      llmLatencyMs: 2000 + Math.floor(Math.random() * 2000),
    });
  }

  if (decs.length > 0) {
    await db.insert(decisions).values(decs);
    console.log(`Inserted ${decs.length} decisions`);
  }

  // Seed risk config
  await db.insert(riskConfig).values({
    symbol: "PEPE/USDT",
    maxPositionSizeUsdt: 1000,
    maxPositionPctPortfolio: 0.1,
    stopLossPct: 0.05,
    takeProfitPct: 0.15,
    maxSlippagePct: 0.01,
    dailyLossLimitUsdt: 500,
    maxHoldTimeMinutes: 240,
    consecutiveLossCooldown: 3,
  });
  console.log("Inserted risk config");

  console.log("Seeding complete!");
}

const directions = ["up", "down", "sideways"];

seed().catch(console.error);
