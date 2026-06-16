import { createRouter, publicQuery } from "./middleware";
import { marketRouter } from "./routers/market";
import { sentimentRouter } from "./routers/sentiment";
import { predictionRouter } from "./routers/prediction";
import { reasoningRouter } from "./routers/reasoning";
import { agentRouter } from "./routers/agent";
import { executionRouter } from "./routers/execution";
import { monitoringRouter } from "./routers/monitoring";

export const appRouter = createRouter({
  ping: publicQuery.query(() => ({ ok: true, ts: Date.now() })),

  // Module 1a: Data Ingestion & Feature Engineering
  market: marketRouter,

  // Module 1b: Sentiment Analysis & MCS
  sentiment: sentimentRouter,

  // Module 2a: Predictive Core (Quantitative)
  prediction: predictionRouter,

  // Module 2b: Reasoning Engine (LLM)
  reasoning: reasoningRouter,

  // Module 3: Agent Orchestration
  agent: agentRouter,

  // Module 4: Execution & Risk Management
  execution: executionRouter,

  // Module 5: Operational Security & Monitoring
  monitoring: monitoringRouter,
});

export type AppRouter = typeof appRouter;
