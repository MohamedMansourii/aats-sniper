import { z } from "zod";
import { createRouter, publicQuery } from "../middleware";
import { getDb } from "../queries/connection";
import { alerts, systemMetrics, riskConfig } from "@db/schema";
import { eq, desc } from "drizzle-orm";

export const monitoringRouter = createRouter({
  createAlert: publicQuery
    .input(
      z.object({
        severity: z.enum(["info", "warning", "critical"]),
        module: z.string(),
        title: z.string(),
        message: z.string().optional(),
        metric: z.string().optional(),
        threshold: z.number().optional(),
        actualValue: z.number().optional(),
        timestamp: z.number().default(() => Date.now()),
      })
    )
    .mutation(async ({ input }) => {
      const db = getDb();
      const result = await db.insert(alerts).values(input);
      return { success: true, id: Number(result[0].insertId) };
    }),

  alerts: publicQuery
    .input(
      z.object({
        severity: z.enum(["info", "warning", "critical"]).optional(),
        acknowledged: z.boolean().optional(),
        limit: z.number().default(50),
      })
    )
    .query(async ({ input }) => {
      const db = getDb();
      const rows = await db
        .select()
        .from(alerts)
        .orderBy(desc(alerts.timestamp))
        .limit(input.limit);

      let filtered = rows;
      if (input.severity) {
        filtered = filtered.filter((r) => r.severity === input.severity);
      }
      if (input.acknowledged !== undefined) {
        filtered = filtered.filter((r) => r.acknowledged === input.acknowledged);
      }

      return filtered;
    }),

  acknowledgeAlert: publicQuery
    .input(z.object({ id: z.number() }))
    .mutation(async ({ input }) => {
      const db = getDb();
      await db.update(alerts).set({ acknowledged: true }).where(eq(alerts.id, input.id));
      return { success: true };
    }),

  recordMetric: publicQuery
    .input(
      z.object({
        metric: z.string(),
        value: z.number(),
        labels: z.record(z.string(), z.string()).optional(),
        timestamp: z.number().default(() => Date.now()),
      })
    )
    .mutation(async ({ input }) => {
      const db = getDb();
      await db.insert(systemMetrics).values(input);
      return { success: true };
    }),

  metrics: publicQuery
    .input(
      z.object({
        metric: z.string().optional(),
        limit: z.number().default(100),
      })
    )
    .query(async ({ input }) => {
      const db = getDb();
      if (input.metric) {
        return db
          .select()
          .from(systemMetrics)
          .where(eq(systemMetrics.metric, input.metric))
          .orderBy(desc(systemMetrics.timestamp))
          .limit(input.limit);
      }
      return db
        .select()
        .from(systemMetrics)
        .orderBy(desc(systemMetrics.timestamp))
        .limit(input.limit);
    }),

  dashboard: publicQuery.query(async () => {
    const db = getDb();
    const allAlerts = await db.select().from(alerts);
    const criticalCount = allAlerts.filter((a) => a.severity === "critical" && !a.acknowledged).length;
    const warningCount = allAlerts.filter((a) => a.severity === "warning" && !a.acknowledged).length;
    const latestMetrics = await db
      .select()
      .from(systemMetrics)
      .orderBy(desc(systemMetrics.timestamp))
      .limit(20);

    return {
      criticalAlertCount: criticalCount,
      warningAlertCount: warningCount,
      latestMetrics,
      systemHealth: criticalCount === 0 ? "healthy" : "degraded",
    };
  }),

  getRiskConfig: publicQuery
    .input(z.object({ symbol: z.string() }))
    .query(async ({ input }) => {
      const db = getDb();
      const rows = await db
        .select()
        .from(riskConfig)
        .where(eq(riskConfig.symbol, input.symbol));
      return rows[0] ?? null;
    }),

  updateRiskConfig: publicQuery
    .input(
      z.object({
        symbol: z.string(),
        maxPositionSizeUsdt: z.number().optional(),
        maxPositionPctPortfolio: z.number().optional(),
        stopLossPct: z.number().optional(),
        takeProfitPct: z.number().optional(),
        maxSlippagePct: z.number().optional(),
        dailyLossLimitUsdt: z.number().optional(),
        maxHoldTimeMinutes: z.number().optional(),
      })
    )
    .mutation(async ({ input }) => {
      const db = getDb();
      const { symbol, ...updates } = input;
      const existing = await db.select().from(riskConfig).where(eq(riskConfig.symbol, symbol));

      if (existing.length === 0) {
        await db.insert(riskConfig).values({ symbol, ...updates });
      } else {
        await db.update(riskConfig).set(updates).where(eq(riskConfig.symbol, symbol));
      }

      return { success: true };
    }),
});
