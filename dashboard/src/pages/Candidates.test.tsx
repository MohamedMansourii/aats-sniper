/**
 * E3 — Candidate / watchlist queue page is a READ-ONLY pipeline VIEW.
 *
 * Load-bearing guarantee: this surface DISPLAYS the queue of tokens the engine
 * evaluated (monitoring / pending / skipped / sniped) with model p, the safety
 * report, and the reason. It is NOT a control surface: the page must render the
 * candidate list and must NOT expose any control that snipes, sizes, vetoes,
 * re-queues, places, or enables an order. Rendered against the mock client (the
 * offline-safe default) so the deck runs with no backend.
 */
import { describe, expect, it } from "vitest";
import { render, screen, within } from "@testing-library/react";

import Candidates from "./Candidates";

describe("Candidates — read-only queue view", () => {
  it("renders the heading and the read-only pipeline framing", async () => {
    render(<Candidates />);
    expect(
      await screen.findByRole("heading", { level: 1, name: /candidate queue/i }),
    ).toBeInTheDocument();
    expect(screen.getByText(/read-only pipeline view/i)).toBeInTheDocument();
    // It states plainly nothing here initiates an action.
    expect(
      screen.getByText(/nothing here snipes, sizes, vetoes, or re-queues/i),
    ).toBeInTheDocument();
  });

  it("lists candidates with their pipeline status, model p, safety and reason", async () => {
    render(<Candidates />);
    // A seeded candidate appears.
    expect(await screen.findByText(/FWOG/)).toBeInTheDocument();
    const table = await screen.findByRole("table");
    // Status / model-p / safety / reason columns are all present.
    expect(
      within(table).getByRole("columnheader", { name: /status/i }),
    ).toBeInTheDocument();
    expect(
      within(table).getByRole("columnheader", { name: /model p/i }),
    ).toBeInTheDocument();
    expect(
      within(table).getByRole("columnheader", { name: /safety report/i }),
    ).toBeInTheDocument();
    expect(
      within(table).getByRole("columnheader", { name: /reason/i }),
    ).toBeInTheDocument();
    // All four pipeline statuses are represented in the mock queue.
    expect(screen.getAllByText(/monitoring/i).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/^skipped$/i).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/^sniped$/i).length).toBeGreaterThan(0);
  });

  it("surfaces a token-safety red flag on an unsafe candidate (de-risk reason)", async () => {
    render(<Candidates />);
    // SLERF is seeded unsafe (sniper_cluster + lp_unlocked); its flags render.
    await screen.findByText(/SLERF/);
    expect(screen.getAllByText(/sniper cluster/i).length).toBeGreaterThan(0);
  });

  it("exposes NO snipe / buy / size / veto / place / enable control", async () => {
    render(<Candidates />);
    await screen.findByText(/FWOG/);
    const forbidden =
      /\b(snipe|buy|size|veto|place|order|enable|requeue|re-queue|long|mirror|copy trade|follow)\b/i;
    // The only interactive elements on this read-only page are the status filter
    // tabs (rendered as <button role="tab">). There are NO action buttons — a
    // read-only surface has nothing with the implicit button role. Assert both:
    // (a) zero `role="button"` controls, and (b) every interactive control's
    // accessible name is free of any control verb.
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
