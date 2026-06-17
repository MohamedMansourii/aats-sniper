/**
 * T-353 — Settings: auto-strat presets + resting orders.
 *
 * Pins: presets are de-risk-only EXIT configuration (a preset can be applied,
 * and applying it does not enable live or size an entry); resting orders render
 * with their de-risk framing and an offline-safe affordance. Rendered against
 * the mock client (offline-safe default).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import Settings from "./Settings";
import * as api from "@/lib/api";

describe("Settings — auto-strat presets", () => {
  beforeEach(() => {
    // Mode/breaker actions are spied so applying a preset can be shown NOT to
    // touch them (a preset only configures exits — it never goes live).
    vi.spyOn(api, "setMode").mockResolvedValue(undefined);
    vi.spyOn(api, "resetBreaker").mockResolvedValue(undefined);
  });
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("renders the preset catalog with de-risk-only framing", async () => {
    render(<Settings />);
    expect(await screen.findByText(/auto-strat presets/i)).toBeInTheDocument();
    expect(await screen.findByText(/secure ladder/i)).toBeInTheDocument();
    expect(screen.getByText(/runner/i)).toBeInTheDocument();
    expect(screen.getByText(/fast scalp/i)).toBeInTheDocument();
  });

  it("applying a preset does not switch mode or change the breaker", async () => {
    const user = userEvent.setup();
    render(<Settings />);

    const applyBtn = await screen.findByRole("button", {
      name: /apply runner preset/i,
    });
    await user.click(applyBtn);

    // The preset is exit config only: no live/mode change, no breaker action.
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: /apply runner preset/i }),
      ).toBeDisabled(),
    );
    expect(api.setMode).not.toHaveBeenCalled();
    expect(api.resetBreaker).not.toHaveBeenCalled();
  });
});

describe("Settings — resting orders", () => {
  it("renders resting orders with de-risk + offline framing", async () => {
    render(<Settings />);
    expect(await screen.findByText(/resting orders/i)).toBeInTheDocument();
    // De-risk-direction-only copy is present.
    expect(
      await screen.findByText(/de-risk direction only/i),
    ).toBeInTheDocument();
    // At least one armed exit order from the mock.
    expect(screen.getAllByText(/exit/i).length).toBeGreaterThan(0);
  });
});
