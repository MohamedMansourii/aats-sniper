import { z } from "zod";
import { createRouter, publicQuery } from "../middleware";
import { getDb } from "../queries/connection";
import { agentStates } from "@db/schema";
import { desc } from "drizzle-orm";

type AgentState = "SENSING" | "PREDICTING" | "REASONING" | "EXECUTING" | "RECOVERING";

class AgentStateMachine {
  state: AgentState = "SENSING";
  tickCount = 0;
  consecutiveErrors = 0;
  dailyPnl = 0;
  dailyLossLimitHit = false;
  lastDecisionId: number | null = null;
  currentPositionId: number | null = null;

  getState() {
    return {
      state: this.state,
      tickCount: this.tickCount,
      consecutiveErrors: this.consecutiveErrors,
      dailyPnl: this.dailyPnl,
      dailyLossLimitHit: this.dailyLossLimitHit,
      lastDecisionId: this.lastDecisionId,
      currentPositionId: this.currentPositionId,
    };
  }

  transition(newState: AgentState) {
    this.state = newState;
    if (newState === "SENSING") {
      this.tickCount++;
    }
  }

  recordError() {
    this.consecutiveErrors++;
    if (this.consecutiveErrors >= 5) {
      this.dailyLossLimitHit = true;
    }
  }

  clearErrors() {
    this.consecutiveErrors = 0;
  }

  setDecisionId(id: number) {
    this.lastDecisionId = id;
  }

  setPositionId(id: number | null) {
    this.currentPositionId = id;
  }

  updatePnl(pnl: number) {
    this.dailyPnl += pnl;
    if (this.dailyPnl < -500) {
      this.dailyLossLimitHit = true;
    }
  }

  resetDaily() {
    this.dailyPnl = 0;
    this.dailyLossLimitHit = false;
  }
}

const stateMachine = new AgentStateMachine();

interface TrpcResponse {
  result?: {
    data?: any;
  };
}

async function callTrpc(proc: string, payload: unknown): Promise<TrpcResponse> {
  const res = await fetch(`http://localhost:3000/api/trpc/${proc}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  return res.json() as Promise<TrpcResponse>;
}

export const agentRouter = createRouter({
  state: publicQuery.query(async () => {
    const db = getDb();
    const rows = await db
      .select()
      .from(agentStates)
      .orderBy(desc(agentStates.timestamp))
      .limit(1);

    const currentState = stateMachine.getState();

    await db.insert(agentStates).values({
      state: currentState.state,
      tickCount: currentState.tickCount,
      consecutiveErrors: currentState.consecutiveErrors,
      dailyPnl: currentState.dailyPnl,
      dailyLossLimitHit: currentState.dailyLossLimitHit,
      lastDecisionId: currentState.lastDecisionId,
      currentPositionId: currentState.currentPositionId,
      memorySnapshot: currentState,
      timestamp: Date.now(),
    });

    return {
      ...currentState,
      persisted: rows[0] ?? null,
    };
  }),

  tick: publicQuery
    .input(
      z.object({
        symbol: z.string(),
        features: z.object({
          quant: z.object({
            rsi14: z.number(),
            macdHist: z.number(),
            bbPosition: z.number(),
            volumeRatio: z.number(),
            volatilityAnnualized: z.number(),
            priceChange1h: z.number(),
            priceChange24h: z.number(),
          }),
          sentiment: z.object({
            conviction: z.number(),
            momentum: z.number(),
            novelty: z.number(),
            redFlags: z.array(z.string()).nullable(),
          }),
        }),
      })
    )
    .mutation(async ({ input }) => {
      const results: Record<string, any> = {};
      const symbol = input.symbol;

      try {
        stateMachine.transition("SENSING");
        stateMachine.transition("PREDICTING");

        const prediction = await callTrpc("prediction.predict", {
          symbol,
          features: input.features.quant,
          timestamp: Date.now(),
        });
        results.prediction = prediction;

        const quantPred = prediction.result?.data;

        if (
          quantPred &&
          Math.max(
            quantPred.prediction.confidence15m,
            quantPred.prediction.confidence1h,
            quantPred.prediction.confidence4h
          ) < 0.3
        ) {
          stateMachine.transition("RECOVERING");
          return { success: true, phase: "PREDICT", results, action: "hold", reason: "low_confidence" };
        }

        stateMachine.transition("REASONING");

        const reasoning = await callTrpc("reasoning.decide", {
          symbol,
          quantPrediction: quantPred?.prediction,
          sentimentFeatures: input.features.sentiment,
          marketFeatures: {
            rsi14: input.features.quant.rsi14,
            volumeRatio: input.features.quant.volumeRatio,
            bbPosition: input.features.quant.bbPosition,
          },
          timestamp: Date.now(),
        });
        results.reasoning = reasoning;

        const decision = reasoning.result?.data;

        stateMachine.transition("EXECUTING");

        const execution = await callTrpc("execution.execute", {
          symbol,
          decision: decision?.decision,
          riskParams: { stopLossPct: 0.05, takeProfitPct: 0.15 },
        });
        results.execution = execution;

        stateMachine.clearErrors();
        stateMachine.transition("RECOVERING");

        return {
          success: true,
          phase: "COMPLETE",
          results,
          action: execution.result?.data?.action ?? "hold",
        };
      } catch (error: any) {
        stateMachine.recordError();
        stateMachine.transition("RECOVERING");
        return {
          success: false,
          phase: "ERROR",
          error: error.message,
          consecutiveErrors: stateMachine.getState().consecutiveErrors,
        };
      }
    }),

  history: publicQuery
    .input(z.object({ limit: z.number().default(50) }))
    .query(async ({ input }) => {
      const db = getDb();
      return db
        .select()
        .from(agentStates)
        .orderBy(desc(agentStates.timestamp))
        .limit(input.limit);
    }),

  resetDaily: publicQuery.mutation(async () => {
    stateMachine.resetDaily();
    return { success: true, dailyPnl: 0 };
  }),

  emergencyStop: publicQuery.mutation(async () => {
    stateMachine.dailyLossLimitHit = true;
    return { success: true, status: "EMERGENCY_STOPPED" };
  }),
});
