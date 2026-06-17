/**
 * E11 — Wallet-cluster ("Bubble Maps") page is a READ-ONLY visualization.
 *
 * Load-bearing guarantee: this surface RENDERS the wallet-connection graph the
 * engine already computes (creators / bundlers / snipers / holders and the
 * detected links between them) so the operator can SEE coordinated
 * manipulation. It is NOT a control surface: the page must render the graph and
 * must NOT expose any control that snipes, blocks, denylists, sizes, vetoes,
 * places, or enables anything against a wallet. The only interactive elements
 * are read-only wallet-inspection bubbles (selecting one shows its share/flags;
 * it takes no action). Rendered against the mock client (the offline-safe
 * default) so the deck runs with no backend.
 */
import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import WalletClusters from "./WalletClusters";

describe("WalletClusters — read-only Bubble Maps view", () => {
  it("renders the heading and the read-only framing", async () => {
    render(<WalletClusters />);
    expect(
      await screen.findByRole("heading", { level: 1, name: /wallet clusters/i }),
    ).toBeInTheDocument();
    expect(screen.getByText(/read-only Bubble Maps view/i)).toBeInTheDocument();
    // It states plainly nothing here initiates an action.
    expect(
      screen.getByText(
        /nothing here snipes, blocks, denylists, or otherwise acts on a wallet/i,
      ),
    ).toBeInTheDocument();
  });

  it("renders a wallet-connection graph for a mapped mint", async () => {
    const { container } = render(<WalletClusters />);
    // The heavily-coordinated mock mint and its graph render.
    expect(await screen.findByText(/SCAMINU/)).toBeInTheDocument();
    // The graph is an <svg role="img"> with a descriptive label; jsdom does not
    // compute an accessible name for inline SVG, so assert on the element + its
    // aria-label attribute directly.
    const graphs = container.querySelectorAll('svg[role="img"]');
    expect(graphs.length).toBeGreaterThan(0);
    expect(graphs[0].getAttribute("aria-label")).toMatch(
      /wallet-connection graph/i,
    );
  });

  it("surfaces coordinated-manipulation signals (de-risk reasons)", async () => {
    render(<WalletClusters />);
    await screen.findByText(/SCAMINU/);
    // The coordinated mint shows its manipulation flags + bundling.
    expect(screen.getAllByText(/bundling detected/i).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/high synchronicity/i).length).toBeGreaterThan(0);
  });

  it("lets the operator inspect a wallet (read-only) without taking any action", async () => {
    const user = userEvent.setup();
    render(<WalletClusters />);
    await screen.findByText(/SCAMINU/);
    // Wallet bubbles are exposed as inspection controls; selecting one reveals
    // its detail panel. This is the ONLY interaction and it acts on nothing.
    const bubbles = screen.getAllByRole("button");
    expect(bubbles.length).toBeGreaterThan(0);
    await user.click(bubbles[0]);
    expect(screen.getByText(/of supply/i)).toBeInTheDocument();
  });

  it("exposes NO snipe / block / denylist / size / veto / place / enable control", async () => {
    render(<WalletClusters />);
    await screen.findByText(/SCAMINU/);
    const forbidden =
      /\b(snipe|buy|sell|block|denylist|blacklist|ban|size|veto|place|order|enable|flag wallet|trade)\b/i;
    // Every interactive element on this read-only page (wallet-inspection
    // bubbles) must have an accessible name free of any control verb — selecting
    // a wallet inspects it, it never acts on it.
    const interactive = [
      ...screen.queryAllByRole("button"),
      ...screen.queryAllByRole("link"),
    ];
    for (const el of interactive) {
      const name = (el.getAttribute("aria-label") ?? el.textContent ?? "").trim();
      expect(name).not.toMatch(forbidden);
    }
  });

  it("never frames the cluster score as a win-rate (HONESTY CLAUSE)", async () => {
    render(<WalletClusters />);
    await screen.findByText(/SCAMINU/);
    // The page must explicitly DISCLAIM win-rate framing: the only mention of
    // "win-rate" is the disclaimer "not a win-rate". There is no win-rate
    // metric, value, target, or column anywhere on the surface.
    expect(screen.getByText(/coordination measure/i)).toBeInTheDocument();
    expect(screen.getByText(/not a/i)).toBeInTheDocument();
    // No win-rate is ever shown as a percentage stat tile or chip value.
    expect(screen.queryByText(/win.?rate\s*[:=]/i)).toBeNull();
  });
});
