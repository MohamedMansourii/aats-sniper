import { relations } from "drizzle-orm";
import {
  marketData,
  technicalFeatures,
  sentimentScores,
  predictions,
  decisions,
  positions,
  trades,
} from "./schema";

export const marketDataRelations = relations(marketData, ({ one }) => ({
  technicalFeatures: one(technicalFeatures, {
    fields: [marketData.id],
    references: [technicalFeatures.marketDataId],
  }),
}));

export const decisionsRelations = relations(decisions, ({ one }) => ({
  prediction: one(predictions, {
    fields: [decisions.quantPredictionId],
    references: [predictions.id],
  }),
  sentiment: one(sentimentScores, {
    fields: [decisions.sentimentScoreId],
    references: [sentimentScores.id],
  }),
}));

export const tradesRelations = relations(trades, ({ one }) => ({
  position: one(positions, {
    fields: [trades.positionId],
    references: [positions.id],
  }),
  decision: one(decisions, {
    fields: [trades.decisionId],
    references: [decisions.id],
  }),
}));

export const positionsRelations = relations(positions, ({ one }) => ({
  decision: one(decisions, {
    fields: [positions.decisionId],
    references: [decisions.id],
  }),
}));
