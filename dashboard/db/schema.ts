import {
  mysqlTable,
  mysqlEnum,
  serial,
  varchar,
  text,
  timestamp,
  float,
  json,
  boolean,
  int,
  bigint,
} from "drizzle-orm/mysql-core";

// ─── Module 1a: Market Data (OHLCV) ───
export const marketData = mysqlTable("market_data", {
  id: serial("id").primaryKey(),
  symbol: varchar("symbol", { length: 32 }).notNull(),
  timestamp: bigint("timestamp", { mode: "number" }).notNull(),
  open: float("open").notNull(),
  high: float("high").notNull(),
  low: float("low").notNull(),
  close: float("close").notNull(),
  volume: float("volume").notNull(),
  createdAt: timestamp("created_at").notNull().defaultNow(),
});

// ─── Module 1a: Technical Features (computed from OHLCV) ───
export const technicalFeatures = mysqlTable("technical_features", {
  id: serial("id").primaryKey(),
  marketDataId: bigint("market_data_id", { mode: "number", unsigned: true }).notNull(),
  symbol: varchar("symbol", { length: 32 }).notNull(),
  timestamp: bigint("timestamp", { mode: "number" }).notNull(),
  rsi14: float("rsi_14"),
  macdLine: float("macd_line"),
  macdSignal: float("macd_signal"),
  macdHist: float("macd_hist"),
  bbPosition: float("bb_position"),
  volumeRatio: float("volume_ratio"),
  volatilityAnnualized: float("volatility_annualized"),
  priceChange1h: float("price_change_1h"),
  priceChange24h: float("price_change_24h"),
  createdAt: timestamp("created_at").notNull().defaultNow(),
});

// ─── Module 1b: Sentiment / MCS Scores ───
export const sentimentScores = mysqlTable("sentiment_scores", {
  id: serial("id").primaryKey(),
  symbol: varchar("symbol", { length: 32 }).notNull(),
  timestamp: bigint("timestamp", { mode: "number" }).notNull(),
  conviction: float("conviction").notNull(),
  momentum: float("momentum").notNull(),
  novelty: float("novelty").notNull(),
  redFlags: json("red_flags").$type<string[]>(),
  reasoning: text("reasoning"),
  weightedEngagement: float("weighted_engagement"),
  postCount: int("post_count"),
  fallback: boolean("fallback").default(false),
  createdAt: timestamp("created_at").notNull().defaultNow(),
});

// ─── Module 2a: Quantitative Predictions ───
export const predictions = mysqlTable("predictions", {
  id: serial("id").primaryKey(),
  symbol: varchar("symbol", { length: 32 }).notNull(),
  timestamp: bigint("timestamp", { mode: "number" }).notNull(),
  prediction15m: mysqlEnum("prediction_15m", ["down", "sideways", "up"]).notNull(),
  prediction1h: mysqlEnum("prediction_1h", ["down", "sideways", "up"]).notNull(),
  prediction4h: mysqlEnum("prediction_4h", ["down", "sideways", "up"]).notNull(),
  confidence15m: float("confidence_15m").notNull(),
  confidence1h: float("confidence_1h").notNull(),
  confidence4h: float("confidence_4h").notNull(),
  probDistribution: json("prob_distribution").$type<number[][]>(),
  modelVersion: varchar("model_version", { length: 32 }).default("tft-v1"),
  inferenceLatencyMs: int("inference_latency_ms"),
  createdAt: timestamp("created_at").notNull().defaultNow(),
});

// ─── Module 2b: Reasoning Decisions ───
export const decisions = mysqlTable("decisions", {
  id: serial("id").primaryKey(),
  symbol: varchar("symbol", { length: 32 }).notNull(),
  timestamp: bigint("timestamp", { mode: "number" }).notNull(),
  signal: mysqlEnum("signal", ["Strong Buy", "Weak Buy", "Hold", "Weak Sell", "Strong Sell"]).notNull(),
  confidence: float("confidence").notNull(),
  reasoning: text("reasoning"),
  overrideTriggered: boolean("override_triggered").default(false),
  overrideReason: text("override_reason"),
  timeframeBias: mysqlEnum("timeframe_bias", ["short", "medium", "long"]),
  riskLevel: mysqlEnum("risk_level", ["low", "medium", "high", "extreme"]),
  quantPredictionId: bigint("quant_prediction_id", { mode: "number", unsigned: true }),
  sentimentScoreId: bigint("sentiment_score_id", { mode: "number", unsigned: true }),
  llmLatencyMs: int("llm_latency_ms"),
  createdAt: timestamp("created_at").notNull().defaultNow(),
});

// ─── Module 3: Agent State ───
export const agentStates = mysqlTable("agent_states", {
  id: serial("id").primaryKey(),
  state: mysqlEnum("state", ["SENSING", "PREDICTING", "REASONING", "EXECUTING", "RECOVERING"]).notNull(),
  tickCount: int("tick_count").default(0),
  consecutiveErrors: int("consecutive_errors").default(0),
  dailyPnl: float("daily_pnl").default(0),
  dailyLossLimitHit: boolean("daily_loss_limit_hit").default(false),
  lastDecisionId: bigint("last_decision_id", { mode: "number", unsigned: true }),
  currentPositionId: bigint("current_position_id", { mode: "number", unsigned: true }),
  memorySnapshot: json("memory_snapshot"),
  timestamp: bigint("timestamp", { mode: "number" }).notNull(),
  createdAt: timestamp("created_at").notNull().defaultNow(),
});

// ─── Module 4: Positions ───
export const positions = mysqlTable("positions", {
  id: serial("id").primaryKey(),
  symbol: varchar("symbol", { length: 32 }).notNull(),
  side: mysqlEnum("side", ["long", "short", "flat"]).notNull(),
  entryPrice: float("entry_price"),
  size: float("size"),
  stopPrice: float("stop_price"),
  takeProfitPrice: float("take_profit_price"),
  entryTime: bigint("entry_time", { mode: "number" }),
  decisionConfidence: float("decision_confidence"),
  unrealizedPnl: float("unrealized_pnl").default(0),
  realizedPnl: float("realized_pnl").default(0),
  status: mysqlEnum("status", ["open", "closed", "liquidated"]).default("open"),
  decisionId: bigint("decision_id", { mode: "number", unsigned: true }),
  createdAt: timestamp("created_at").notNull().defaultNow(),
  updatedAt: timestamp("updated_at").notNull().defaultNow(),
});

// ─── Module 4: Trades ───
export const trades = mysqlTable("trades", {
  id: serial("id").primaryKey(),
  symbol: varchar("symbol", { length: 32 }).notNull(),
  positionId: bigint("position_id", { mode: "number", unsigned: true }),
  action: mysqlEnum("action", ["BUY", "SELL", "HOLD"]).notNull(),
  orderType: mysqlEnum("order_type", ["market", "limit", "stop"]).default("market"),
  size: float("size").notNull(),
  executedPrice: float("executed_price"),
  slippageActual: float("slippage_actual"),
  fees: float("fees").default(0),
  pnl: float("pnl"),
  decisionId: bigint("decision_id", { mode: "number", unsigned: true }),
  exitReason: mysqlEnum("exit_reason", [
    "STOP_LOSS_HARD_EXIT",
    "LLM_CATASTROPHE_EXIT",
    "TAKE_PROFIT_EXIT",
    "TRAILING_STOP_EXIT",
    "DECISION_SIGNAL",
    "EMERGENCY_CLOSE",
    "MANUAL",
  ]),
  timestamp: bigint("timestamp", { mode: "number" }).notNull(),
  createdAt: timestamp("created_at").notNull().defaultNow(),
});

// ─── Module 5: Alerts / Monitoring ───
export const alerts = mysqlTable("alerts", {
  id: serial("id").primaryKey(),
  severity: mysqlEnum("severity", ["info", "warning", "critical"]).notNull(),
  module: varchar("module", { length: 32 }).notNull(),
  title: varchar("title", { length: 255 }).notNull(),
  message: text("message"),
  metric: varchar("metric", { length: 64 }),
  threshold: float("threshold"),
  actualValue: float("actual_value"),
  acknowledged: boolean("acknowledged").default(false),
  timestamp: bigint("timestamp", { mode: "number" }).notNull(),
  createdAt: timestamp("created_at").notNull().defaultNow(),
});

// ─── Module 5: Risk Configuration ───
export const riskConfig = mysqlTable("risk_config", {
  id: serial("id").primaryKey(),
  symbol: varchar("symbol", { length: 32 }).notNull(),
  maxPositionSizeUsdt: float("max_position_size_usdt").default(1000),
  maxPositionPctPortfolio: float("max_position_pct_portfolio").default(0.1),
  stopLossPct: float("stop_loss_pct").default(0.05),
  takeProfitPct: float("take_profit_pct").default(0.15),
  maxSlippagePct: float("max_slippage_pct").default(0.01),
  dailyLossLimitUsdt: float("daily_loss_limit_usdt").default(500),
  maxHoldTimeMinutes: int("max_hold_time_minutes").default(240),
  consecutiveLossCooldown: int("consecutive_loss_cooldown").default(3),
  updatedAt: timestamp("updated_at").notNull().defaultNow(),
});

// ─── Module 5: System Metrics ───
export const systemMetrics = mysqlTable("system_metrics", {
  id: serial("id").primaryKey(),
  metric: varchar("metric", { length: 64 }).notNull(),
  value: float("value").notNull(),
  labels: json("labels"),
  timestamp: bigint("timestamp", { mode: "number" }).notNull(),
  createdAt: timestamp("created_at").notNull().defaultNow(),
});
