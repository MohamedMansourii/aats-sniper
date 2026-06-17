/**
 * T-351 — Key pages render under the mock client (offline-safe default).
 *
 * Smoke + states audit: each key operator page mounts in mock mode without
 * throwing, surfaces its heading, and (where applicable) shows a loading or
 * empty state rather than a blank screen. This is the standalone-runs-on-mock
 * guarantee (NFR-011 / AC-049) exercised at the component level.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router";

import CommandDeck from "./CommandDeck";
import Narrative from "./Narrative";
import Positions from "./Positions";
import Settings from "./Settings";
import Layout from "@/components/Layout";
import { USE_MOCK } from "@/lib/api";

afterEach(() => {
  vi.restoreAllMocks();
});

describe("mock mode is the default", () => {
  it("USE_MOCK is true so the dashboard runs with no backend", () => {
    expect(USE_MOCK).toBe(true);
  });
});

describe("key pages render in mock mode", () => {
  it("CommandDeck mounts and shows its heading + live indicator", async () => {
    render(<CommandDeck />);
    expect(
      await screen.findByRole("heading", { name: /command deck/i }),
    ).toBeInTheDocument();
    expect(screen.getByText(/live telemetry/i)).toBeInTheDocument();
  });

  it("Positions mounts and shows its heading", async () => {
    render(<Positions />);
    // The page title is the level-1 heading; panels add their own h2s
    // ("Open positions", "Closed positions"), so target level 1 exactly.
    expect(
      await screen.findByRole("heading", { level: 1, name: /^positions$/i }),
    ).toBeInTheDocument();
  });

  it("Narrative (E7) mounts and shows its heading + de-risk framing", async () => {
    render(<Narrative />);
    expect(
      await screen.findByRole("heading", { level: 1, name: /narrative & news/i }),
    ).toBeInTheDocument();
    // De-risk-only framing is present (no win-rate on this surface).
    expect(screen.getByText(/read-only de-risk view/i)).toBeInTheDocument();
  });

  it("Settings mounts and shows the agent-mode control", async () => {
    render(<Settings />);
    expect(
      await screen.findByRole("heading", { name: /^settings$/i }),
    ).toBeInTheDocument();
    expect(await screen.findByText(/agent mode/i)).toBeInTheDocument();
  });

  it("Layout mounts with nav, mode badge and the destructive controls present", async () => {
    render(
      <MemoryRouter>
        <Layout>
          <div data-testid="content">child</div>
        </Layout>
      </MemoryRouter>,
    );
    expect(screen.getByTestId("content")).toBeInTheDocument();
    expect(
      await screen.findByRole("button", { name: /flatten all/i }),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /kill/i })).toBeInTheDocument();
    // Navigation present (operator can reach every page).
    expect(screen.getByRole("link", { name: /positions/i })).toBeInTheDocument();
  });
});
