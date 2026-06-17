/**
 * T-353 — Positions P&L cards + CSV/JSON export.
 *
 * Pins: net-of-cost PnL is the headline (a card labeled "net of cost"); gross
 * is present but explicitly secondary; export buttons exist and call the
 * download path. NO win-rate label appears on the page.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import Positions from "./Positions";
import * as exportLib from "@/lib/export";

afterEach(() => {
  vi.restoreAllMocks();
});

describe("Positions — P&L cards (net primary, gross secondary)", () => {
  it("shows a net-of-cost realized headline card", async () => {
    render(<Positions />);
    expect(
      await screen.findByText(/realized p&l \(net of cost\)/i),
    ).toBeInTheDocument();
    // Gross appears but is labeled secondary.
    expect(screen.getByText(/gross \(secondary\)/i)).toBeInTheDocument();
    // Cost-drag card is present.
    expect(screen.getByText(/cost drag/i)).toBeInTheDocument();
  });

  it("never shows a win-rate label", async () => {
    const { container } = render(<Positions />);
    await screen.findByText(/realized p&l \(net of cost\)/i);
    expect(container.textContent?.toLowerCase()).not.toContain("win rate");
    expect(container.textContent?.toLowerCase()).not.toContain("win-rate");
  });
});

describe("Positions — export", () => {
  it("CSV button triggers a CSV download", async () => {
    const spy = vi.spyOn(exportLib, "downloadFile").mockImplementation(() => {});
    const user = userEvent.setup();
    render(<Positions />);

    await user.click(
      await screen.findByRole("button", { name: /export positions as csv/i }),
    );
    expect(spy).toHaveBeenCalledTimes(1);
    const [filename, , mime] = spy.mock.calls[0];
    expect(filename).toMatch(/\.csv$/);
    expect(mime).toContain("text/csv");
  });

  it("JSON button triggers a JSON download", async () => {
    const spy = vi.spyOn(exportLib, "downloadFile").mockImplementation(() => {});
    const user = userEvent.setup();
    render(<Positions />);

    await user.click(
      await screen.findByRole("button", { name: /export positions as json/i }),
    );
    expect(spy).toHaveBeenCalledTimes(1);
    const [filename, , mime] = spy.mock.calls[0];
    expect(filename).toMatch(/\.json$/);
    expect(mime).toContain("application/json");
  });
});
