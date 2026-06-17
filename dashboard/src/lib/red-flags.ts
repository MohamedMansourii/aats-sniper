/**
 * AATS Sniper — token-safety red-flag labels (T-323/T-329 scanner → `red_flags`).
 *
 * Pure data + a label function, kept out of the component module so the kit's
 * <RedFlags> file only exports a component (react-refresh constraint).
 */

import type { RedFlag } from "./types";

export const RED_FLAG_LABEL: Record<string, string> = {
  freeze_authority: "freeze authority",
  mint_authority: "mint authority",
  lp_unlocked: "LP unlocked",
  lp_not_locked: "LP not locked",
  sniper_cluster: "sniper cluster",
  dev_cluster: "dev cluster",
  bundle_cluster: "bundle cluster",
  high_tax: "high tax",
  honeypot: "honeypot",
  not_sellable: "not sellable",
  holder_concentration: "holder concentration",
  low_liquidity: "low liquidity",
};

/** Human label for a red-flag code; unknown codes are surfaced verbatim. */
export function redFlagLabel(code: RedFlag): string {
  return RED_FLAG_LABEL[code] ?? code.replace(/_/g, " ");
}
