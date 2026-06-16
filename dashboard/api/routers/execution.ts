import { z } from "zod";
import { createRouter, publicQuery } from "../middleware";
import { getDb } from "../queries/connection";
import { positions, trades } from "@db/schema";
import { eq, desc, and } from "drizzle-orm";

class ExecutionEngine {
  async execute(
    symbol: string,
    decision: {
      signal: string;
      confidence: number;
      reasoning: string;
      overrideTriggered: boolean;
      overrideReason?: string;
      riskLevel: string;
    },
    currentPosition: any,
    riskParams: { stopLossPct: number; takeProfitPct: number }
  ): Promise<{
    success: boolean;
    action: string;
    reason?: string;
    orderId?: string;
    executedPrice?: number;
    size?: number;
    slippage?: number;
    newPosition?: any;
    exitReason?: string;
    pnl?: number;
  }> {
    if (currentPosition && currentPosition.side !== "flat") {
      const currentPrice = await this.getCurrentPrice(symbol);
      const exitCheck = await this.checkExitConditions(
        currentPosition,
        currentPrice,
        decision,
        riskParams
      );

      if (exitCheck) {
        const closeTrade = await this.closePosition(currentPosition, currentPrice, exitCheck);
        return {
          success: true,
          action: "SELL",
          exitReason: exitCheck,
          executedPrice: currentPrice,
          pnl: closeTrade.pnl,
        };
      }
    }

    if (decision.signal === "Strong Buy" || decision.signal === "Weak Buy") {
      if (currentPosition && currentPosition.side === "long") {
        return { success: true, action: "HOLD", reason: "already_long" };
      }

      if (currentPosition && currentPosition.side === "short") {
        const price = await this.getCurrentPrice(symbol);
        const closeTrade = await this.closePosition(currentPosition, price, "DECISION_SIGNAL");
        return {
          success: true,
          action: "CLOSE_SHORT",
          executedPrice: price,
          pnl: closeTrade.pnl,
        };
      }

      const price = await this.getCurrentPrice(symbol);
      const size = this.calculatePositionSize(decision, price);
      const slippage = Math.random() * 0.005;
      const executedPrice = price * (1 + slippage);

      const newPosition = {
        symbol,
        side: "long" as const,
        entryPrice: executedPrice,
        size,
        stopPrice: executedPrice * (1 - riskParams.stopLossPct),
        takeProfitPrice: executedPrice * (1 + riskParams.takeProfitPct),
        entryTime: Date.now(),
        decisionConfidence: decision.confidence,
        status: "open" as const,
      };

      return {
        success: true,
        action: "BUY",
        executedPrice,
        size,
        slippage,
        newPosition,
      };
    }

    if (decision.signal === "Strong Sell" || decision.signal === "Weak Sell") {
      if (!currentPosition || currentPosition.side === "flat") {
        return { success: true, action: "HOLD", reason: "no_position" };
      }

      const price = await this.getCurrentPrice(symbol);
      const closeTrade = await this.closePosition(currentPosition, price, "DECISION_SIGNAL");

      return {
        success: true,
        action: "SELL",
        executedPrice: price,
        pnl: closeTrade.pnl,
      };
    }

    return { success: true, action: "HOLD" };
  }

  async checkExitConditions(
    position: any,
    currentPrice: number,
    decision: any,
    riskParams: any
  ): Promise<string | null> {
    const entry = position.entryPrice;
    const pnlPct = (currentPrice - entry) / entry;

    if (pnlPct <= -riskParams.stopLossPct) {
      return "STOP_LOSS_HARD_EXIT";
    }

    if (decision.overrideTriggered && decision.overrideReason?.toLowerCase().includes("catastrophe")) {
      return "LLM_CATASTROPHE_EXIT";
    }

    if (pnlPct >= riskParams.takeProfitPct) {
      return "TAKE_PROFIT_EXIT";
    }

    if (pnlPct > 0.05) {
      const trailingStop = currentPrice * 0.97;
      if (currentPrice <= trailingStop) {
        return "TRAILING_STOP_EXIT";
      }
    }

    return null;
  }

  async getCurrentPrice(_symbol: string): Promise<number> {
    const basePrice = 0.000012;
    const jitter = (Math.random() - 0.5) * basePrice * 0.02;
    return basePrice + jitter;
  }

  calculatePositionSize(decision: any, _price: number): number {
    const maxSize = 1000 / 0.000012;
    return maxSize * decision.confidence * 0.5;
  }

  async closePosition(position: any, currentPrice: number, _reason: string) {
    const pnl = (currentPrice - position.entryPrice) * position.size;
    return { pnl };
  }
}

const engine = new ExecutionEngine();

export const executionRouter = createRouter({
  execute: publicQuery
    .input(
      z.object({
        symbol: z.string(),
        decision: z.object({
          signal: z.string(),
          confidence: z.number(),
          reasoning: z.string(),
          overrideTriggered: z.boolean(),
          overrideReason: z.string().optional(),
          riskLevel: z.string(),
        }),
        riskParams: z.object({
          stopLossPct: z.number().default(0.05),
          takeProfitPct: z.number().default(0.15),
        }),
      })
    )
    .mutation(async ({ input }) => {
      const db = getDb();

      const positions_ = await db
        .select()
        .from(positions)
        .where(and(eq(positions.symbol, input.symbol), eq(positions.status, "open")))
        .orderBy(desc(positions.createdAt))
        .limit(1);

      const currentPosition = positions_[0] || null;

      const result = await engine.execute(
        input.symbol,
        input.decision,
        currentPosition,
        input.riskParams
      );

      if (result.newPosition) {
        const dbResult = await db.insert(positions).values({
          symbol: input.symbol,
          ...result.newPosition,
        });

        await db.insert(trades).values({
          symbol: input.symbol,
          positionId: Number(dbResult[0].insertId),
          action: "BUY",
          size: result.size ?? 0,
          executedPrice: result.executedPrice,
          slippageActual: result.slippage,
          timestamp: Date.now(),
        });
      }

      if (result.action === "SELL" && currentPosition) {
        await db
          .update(positions)
          .set({
            status: "closed",
            realizedPnl: result.pnl ?? 0,
            updatedAt: new Date(),
          })
          .where(eq(positions.id, currentPosition.id));

        await db.insert(trades).values({
          symbol: input.symbol,
          positionId: currentPosition.id,
          action: "SELL",
          size: currentPosition.size ?? 0,
          executedPrice: result.executedPrice,
          pnl: result.pnl,
          exitReason: result.exitReason as any,
          timestamp: Date.now(),
        });
      }

      return {
        success: result.success,
        action: result.action,
        executedPrice: result.executedPrice,
        pnl: result.pnl,
        exitReason: result.exitReason,
      };
    }),

  riskCheck: publicQuery
    .input(
      z.object({
        decision: z.any(),
        recentTrades: z.array(z.any()).default([]),
        portfolioValue: z.number().default(10000),
      })
    )
    .query(async ({ input }) => {
      const recentLosses = input.recentTrades.filter((t: any) => (t.pnl || 0) < 0).length;
      if (recentLosses >= 3) {
        return { approved: false, reason: `COOLDOWN: ${recentLosses} recent losses` };
      }
      return {
        approved: true,
        riskParams: { stopLossPct: 0.05, takeProfitPct: 0.15, maxSlippagePct: 0.01 },
      };
    }),

  positions: publicQuery
    .input(z.object({ symbol: z.string().optional() }))
    .query(async ({ input }) => {
      const db = getDb();
      if (input.symbol) {
        return db
          .select()
          .from(positions)
          .where(eq(positions.symbol, input.symbol))
          .orderBy(desc(positions.createdAt));
      }
      return db.select().from(positions).orderBy(desc(positions.createdAt)).limit(50);
    }),

  trades: publicQuery
    .input(
      z.object({
        symbol: z.string().optional(),
        limit: z.number().default(50),
      })
    )
    .query(async ({ input }) => {
      const db = getDb();
      if (input.symbol) {
        return db
          .select()
          .from(trades)
          .where(eq(trades.symbol, input.symbol))
          .orderBy(desc(trades.timestamp))
          .limit(input.limit);
      }
      return db
        .select()
        .from(trades)
        .orderBy(desc(trades.timestamp))
        .limit(input.limit);
    }),

  stats: publicQuery.query(async () => {
    const db = getDb();
    const allTrades = await db
      .select()
      .from(trades)
      .orderBy(desc(trades.timestamp))
      .limit(200);

    const winningTrades = allTrades.filter((t) => (t.pnl || 0) > 0);
    const losingTrades = allTrades.filter((t) => (t.pnl || 0) < 0);
    const totalPnl = allTrades.reduce((s, t) => s + (t.pnl || 0), 0);
    const avgWin = winningTrades.length > 0 ? winningTrades.reduce((s, t) => s + (t.pnl || 0), 0) / winningTrades.length : 0;
    const avgLoss = losingTrades.length > 0 ? losingTrades.reduce((s, t) => s + (t.pnl || 0), 0) / losingTrades.length : 0;

    return {
      totalTrades: allTrades.length,
      winningTrades: winningTrades.length,
      losingTrades: losingTrades.length,
      winRate: allTrades.length > 0 ? winningTrades.length / allTrades.length : 0,
      totalPnl: Math.round(totalPnl * 100) / 100,
      avgWin: Math.round(avgWin * 100) / 100,
      avgLoss: Math.round(avgLoss * 100) / 100,
      profitFactor: avgLoss !== 0 ? Math.abs(avgWin / avgLoss) : 0,
    };
  }),
});
