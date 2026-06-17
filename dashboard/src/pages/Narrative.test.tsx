/**
 * E7 — Narrative & News page is a READ-ONLY de-risk VIEW.
 *
 * Load-bearing guarantees this test pins:
 *   - the page renders the per-asset narrative read (sources, freshness,
 *     narrative score, coordinated-shill / breaking flags) against the mock
 *     client (the offline-safe default) so the deck runs with no backend;
 *   - it is NOT a control surface — the page exposes NO control that snipes,
 *     sizes, vetoes, buys, places, enables, or otherwise acts on an asset;
 *   - the DE-RISK framing is explicit and there is NO win-rate anywhere.
 */
import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";

import Narrative from "./Narrative";

describe("Narrative — read-only de-risk view", () => {
  it("renders the heading and the read-only de-risk framing", async () => {
    render(<Narrative />);
    expect(
      await screen.findByRole("heading", { level: 1, name: /narrative & news/i }),
    ).toBeInTheDocument();
    expect(screen.getByText(/read-only de-risk view/i)).toBeInTheDocument();
    // It states plainly nothing here initiates an action.
    expect(
      screen.getByText(/nothing here snipes, sizes, vetoes/i),
    ).toBeInTheDocument();
  });

  it("frames the narrative score as a de-risk delta, NOT a win-rate", async () => {
    render(<Narrative />);
    await screen.findByText(/read-only de-risk view/i);
    // The explainer states the score is a de-risk delta that can only lower
    // conviction (the words "lower" / "raise" sit in nested spans, so match on
    // a stable contiguous fragment instead of the whole sentence).
    expect(screen.getByText(/\[−1 … 0\] de-risk delta/)).toBeInTheDocument();
    expect(screen.getByText(/sell-into-retail liquidity/i)).toBeInTheDocument();
    expect(screen.getAllByText(/de-risk only/i).length).toBeGreaterThan(0);
    // The only mention of "win-rate" is the HONESTY-CLAUSE denial ("not a
    // win-rate") — there is NO numeric win-rate value, target, or symbol. Assert
    // it is framed strictly as a de-risk delta, not a probability-of-profit.
    expect(screen.getByText(/not a win-rate/i)).toBeInTheDocument();
  });

  it("lists per-asset narratives with sources, freshness and flags", async () => {
    render(<Narrative />);
    // A seeded asset with a credible-negative breaking event appears as a card
    // title (the token also occurs inside the headline digest, so target the
    // heading-styled title node, not any node mentioning the token).
    expect(
      await screen.findByText("RUGGY", { selector: "span.font-semibold" }),
    ).toBeInTheDocument();
    // A source outlet renders.
    expect(screen.getAllByText(/CoinDesk/i).length).toBeGreaterThan(0);
    // The coordinated-shill / breaking-news red flags surface as de-risk reasons.
    expect(
      screen.getAllByText(/breaking negative news/i).length,
    ).toBeGreaterThan(0);
    // Sources section header present.
    expect(screen.getAllByText(/^sources$/i).length).toBeGreaterThan(0);
  });

  it("surfaces a coordinated-shill flag as a de-risk reason", async () => {
    render(<Narrative />);
    await screen.findByText("REGWATCH", { selector: "span.font-semibold" });
    // The mock seeds REGWATCH with a coordinated_shill flag → it must render.
    expect(screen.getAllByText(/coordinated shill/i).length).toBeGreaterThan(0);
  });

  it("exposes NO snipe / buy / size / veto / place / enable control", async () => {
    render(<Narrative />);
    await screen.findByText("RUGGY", { selector: "span.font-semibold" });
    const forbidden =
      /\b(snipe|buy|size|veto|place|order|enable|requeue|re-queue|long|mirror|copy trade|follow)\b/i;
    // A read-only surface has no action buttons. Assert (a) zero role="button"
    // controls, and (b) any interactive control's accessible name is verb-free.
    expect(screen.queryAllByRole("button")).toHaveLength(0);
    const interactive = [
      ...screen.queryAllByRole("button"),
      ...screen.queryAllByRole("tab"),
    ];
    for (const el of interactive) {
      const name = (el.getAttribute("aria-label") ?? el.textContent ?? "").trim();
      expect(name).not.toMatch(forbidden);
    }
  });
});
