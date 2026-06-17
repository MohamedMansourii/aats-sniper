/**
 * T-351 — Flatten controls fire the correct de-risking action.
 *
 *   • Layout top-bar "Flatten all"  → flattenAll()  (POST /api/flatten)
 *   • Positions per-row "Flatten"    → flatten(mint) (POST /api/flatten/{mint})
 *
 * Flatten is a de-risk-only action (market-exit), so — unlike kill / go-live —
 * it fires on click without a second confirm. We assert it calls the right
 * client function with the right argument.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";

import Layout from "@/components/Layout";
import Positions from "./Positions";
import * as api from "@/lib/api";
import * as mock from "@/lib/mock";

describe("Flatten controls", () => {
  beforeEach(() => {
    vi.spyOn(api, "flattenAll").mockResolvedValue(undefined);
    vi.spyOn(api, "flatten").mockResolvedValue(undefined);
  });
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("top-bar 'Flatten all' calls flattenAll()", async () => {
    const user = userEvent.setup();
    render(
      <MemoryRouter>
        <Layout>
          <div />
        </Layout>
      </MemoryRouter>,
    );

    await user.click(
      await screen.findByRole("button", { name: /flatten all/i }),
    );

    await waitFor(() => expect(api.flattenAll).toHaveBeenCalledTimes(1));
  });

  it("per-position 'Flatten' calls flatten(mint) with that position's mint", async () => {
    // Pin deterministic positions so we know an open row exists and its mint.
    const open = mock.makePositions().find((p) => p.status === "open");
    expect(open).toBeDefined();
    vi.spyOn(mock, "makePositions").mockReturnValue([open!]);

    const user = userEvent.setup();
    render(<Positions />);

    const flattenBtn = await screen.findByRole("button", {
      name: new RegExp(`flatten ${open!.token}`, "i"),
    });
    await user.click(flattenBtn);

    await waitFor(() => expect(api.flatten).toHaveBeenCalledWith(open!.mint));
  });

  it("renders the empty state when there are no open positions", async () => {
    vi.spyOn(mock, "makePositions").mockReturnValue([]);
    render(<Positions />);

    expect(
      await screen.findByText(/no open positions/i),
    ).toBeInTheDocument();
  });
});

describe("per-row flatten — accessible labelling", () => {
  it("each flatten button is reachable by an accessible name including the token", async () => {
    const open = mock.makePositions().find((p) => p.status === "open");
    vi.spyOn(mock, "makePositions").mockReturnValue([open!]);
    render(<Positions />);

    const row = await screen.findByRole("button", {
      name: new RegExp(`flatten ${open!.token}`, "i"),
    });
    // Sanity: button lives in a table row alongside the token label.
    expect(within(row).getByText(/flatten/i)).toBeInTheDocument();
  });
});
