/**
 * T-351 — DESTRUCTIVE controls on the Settings page.
 *
 *   • Breaker reset (POST /api/breaker/reset) — re-arms the daily-loss breaker.
 *   • Mode switch (POST /api/mode) — switching to LIVE arms real capital and
 *     MUST route through an explicit confirm dialog; de-risking modes (paper /
 *     dry-run) may switch directly.
 *
 * Rendered against the mock client (default), with the api action functions
 * spied so we assert the wiring without a control plane.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import Settings from "./Settings";
import * as api from "@/lib/api";

describe("Settings — destructive control wiring", () => {
  beforeEach(() => {
    vi.spyOn(api, "resetBreaker").mockResolvedValue(undefined);
    vi.spyOn(api, "setMode").mockResolvedValue(undefined);
  });
  afterEach(() => {
    vi.restoreAllMocks();
    vi.clearAllTimers();
  });

  it("breaker reset calls resetBreaker exactly once", async () => {
    const user = userEvent.setup();
    render(<Settings />);

    await user.click(await screen.findByRole("button", { name: /reset daily/i }));

    await waitFor(() => expect(api.resetBreaker).toHaveBeenCalledTimes(1));
  });

  it("switching to a de-risking mode (paper) calls setMode('paper') directly", async () => {
    const user = userEvent.setup();
    render(<Settings />);

    // Default mock mode is "dry-run", so the Paper tile is enabled. The tile's
    // accessible name folds in its description, so match on that unique copy.
    const paperBtn = await screen.findByRole("button", {
      name: /simulated fills, no broadcast/i,
    });
    await user.click(paperBtn);

    await waitFor(() => expect(api.setMode).toHaveBeenCalledWith("paper"));
  });

  it("switching to LIVE requires confirmation and does NOT call setMode until confirmed", async () => {
    const user = userEvent.setup();
    render(<Settings />);

    // The Live tile's accessible name includes its risk description — unique.
    const liveBtn = await screen.findByRole("button", {
      name: /real submissions to jito/i,
    });
    await user.click(liveBtn);

    // Confirm dialog appears; setMode has NOT fired yet.
    expect(
      await screen.findByText(/switch to live trading\?/i),
    ).toBeInTheDocument();
    expect(api.setMode).not.toHaveBeenCalled();

    // Confirm → setMode('live') fires exactly once.
    await user.click(await screen.findByRole("button", { name: /go live/i }));
    await waitFor(() => expect(api.setMode).toHaveBeenCalledWith("live"));
  });

  it("cancelling the LIVE confirm leaves setMode uncalled", async () => {
    const user = userEvent.setup();
    render(<Settings />);

    await user.click(
      await screen.findByRole("button", { name: /real submissions to jito/i }),
    );
    await screen.findByText(/switch to live trading\?/i);
    await user.click(await screen.findByRole("button", { name: /stay in/i }));

    expect(api.setMode).not.toHaveBeenCalled();
  });
});
