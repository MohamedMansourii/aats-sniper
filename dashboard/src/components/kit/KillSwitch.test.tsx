/**
 * T-351 — DESTRUCTIVE control requires explicit confirmation.
 *
 * The kill switch must NOT fire on a single click: it opens an AlertDialog and
 * only calls killSwitch() once the operator confirms. Cancelling must call
 * nothing. This is the safety property for the single most destructive control
 * on the deck.
 */
import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { KillSwitch } from "./KillSwitch";
import * as api from "@/lib/api";

describe("KillSwitch — confirm-gated destructive control", () => {
  beforeEach(() => {
    vi.spyOn(api, "killSwitch").mockResolvedValue(undefined);
  });
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("does NOT call killSwitch on the first click — it opens a confirm dialog", async () => {
    const user = userEvent.setup();
    render(<KillSwitch />);

    await user.click(screen.getByRole("button", { name: /kill/i }));

    // Dialog is open with the confirmation prompt…
    expect(
      await screen.findByText(/engage kill switch\?/i),
    ).toBeInTheDocument();
    // …but the destructive call has NOT fired yet.
    expect(api.killSwitch).not.toHaveBeenCalled();
  });

  it("calls killSwitch exactly once only after the operator confirms", async () => {
    const user = userEvent.setup();
    render(<KillSwitch />);

    await user.click(screen.getByRole("button", { name: /kill/i }));
    await user.click(await screen.findByRole("button", { name: /engage kill/i }));

    await waitFor(() => expect(api.killSwitch).toHaveBeenCalledTimes(1));
  });

  it("calls nothing when the operator cancels the dialog", async () => {
    const user = userEvent.setup();
    render(<KillSwitch />);

    await user.click(screen.getByRole("button", { name: /kill/i }));
    await user.click(await screen.findByRole("button", { name: /^cancel$/i }));

    expect(api.killSwitch).not.toHaveBeenCalled();
  });
});
