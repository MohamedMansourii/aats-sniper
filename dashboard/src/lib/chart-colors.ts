/**
 * AATS Sniper — canonical chart palette.
 *
 * Recharts renders to an SVG/canvas surface that cannot resolve CSS custom
 * properties (`var(--accent)`), so chart series, axes, grids, and tooltips need
 * literal hex values. This module is the single source of those hexes for every
 * chart on the deck — previously each page redeclared its own `HEX`/`C` object,
 * which drifted and made a palette change an 8-file edit.
 *
 * These values MUST mirror the design tokens in `src/index.css` (the `:root`
 * custom properties). If a token changes there, change it here too — they are
 * the same palette expressed for two different rendering surfaces.
 */
export const CHART = {
  bg: "#0a0b0d",
  panel: "#111418",
  panel2: "#171b21",
  border: "#1e242c",

  text: "#e8edf4",
  muted: "#9aa4b2",
  faint: "#646e7e",

  // Semantic — color always encodes meaning.
  accent: "#22e39a",
  up: "#22e39a",
  down: "#ff5d5d",
  danger: "#ff5d5d",
  warn: "#ffb020",
  info: "#4aa3ff",
  violet: "#8b7bff",
} as const;

export type ChartColor = keyof typeof CHART;
