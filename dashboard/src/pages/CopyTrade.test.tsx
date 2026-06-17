/**
 * T-353 — Copy-trade / smart-money page is a SELECTIVITY FILTER VIEW.
 *
 * Load-bearing guarantee: this surface DISPLAYS which tracked wallets the
 * engine would consider as a feature input — it is NOT a buy trigger. The page
 * must render the wallet list and must NOT expose any control that places,
 * sizes, mirrors, copies, or enables an order. Rendered against the mock client
 * (the offline-safe default).
 */
import { describe, expect, it } from "vitest";
import { render, screen, within } from "@testing-library/react";

import CopyTrade from "./CopyTrade";

describe("CopyTrade — selectivity view", () => {
  it("renders the heading and the selectivity-filter framing", async () => {
    render(<CopyTrade />);
    expect(
      await screen.findByRole("heading", { name: /copy-trade & smart money/i }),
    ).toBeInTheDocument();
    expect(screen.getByText(/selectivity filter view/i)).toBeInTheDocument();
    // It states plainly it is not a buy trigger.
    expect(screen.getByText(/not a buy trigger/i)).toBeInTheDocument();
  });

  it("lists tracked wallets with a considered / filtered-out verdict", async () => {
    render(<CopyTrade />);
    expect(await screen.findByText(/early-lp sniper/i)).toBeInTheDocument();
    expect(screen.getAllByText(/considered/i).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/filtered out/i).length).toBeGreaterThan(0);
  });

  it("exposes NO buy / mirror / copy / place-order / enable control", async () => {
    render(<CopyTrade />);
    await screen.findByText(/early-lp sniper/i);
    const buttons = screen.getAllByRole("button");
    const forbidden = /\b(buy|mirror|copy trade|place|order|enable|follow|long|size up)\b/i;
    for (const b of buttons) {
      const name = (b.getAttribute("aria-label") ?? b.textContent ?? "").trim();
      // The only buttons on the page are filter tabs and per-row "copy address".
      // "copy address" is a clipboard action, not a copy-TRADE control.
      const isClipboard = /copy address/i.test(name);
      if (!isClipboard) {
        expect(name).not.toMatch(forbidden);
      }
    }
  });

  it("surfaces entry-lag honestly (we follow, not front-run)", async () => {
    render(<CopyTrade />);
    const table = await screen.findByRole("table");
    // The "Entry lag" column header exists (honest follower framing).
    expect(
      within(table).getByRole("columnheader", { name: /entry lag/i }),
    ).toBeInTheDocument();
  });
});
