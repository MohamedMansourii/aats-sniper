/**
 * E3 — candidate-queue wire adapter tests.
 *
 * Pinned to the AUTHORITATIVE wire shape: CandidateRecord
 * (api-contracts.md §14 / aats.control_plane.candidate_schemas.py), returned
 * verbatim by GET /api/candidates. The fixtures below are the §14 JSON example
 * verbatim — a drift between the adapter and the real schema must FAIL here.
 *
 * Pins: the safety_report → safetyPassed/redFlags derivation (an operator must
 * NEVER miss a safety warning — a vetoed candidate must not render clean), the
 * closed status set (unknown → safest "monitoring", never "sniped"), the
 * verbatim reason-code passthrough, the mint→{id,token} mapping (the wire has no
 * separate token name), the evaluated_at_wall_ms→{firstSeenMs,updatedMs}
 * mapping, and the honest absence of source/smartWallets (null, never faked).
 * No money-as-float field exists (modelP is a probability).
 */
import { describe, expect, it } from "vitest";
import { adaptCandidate, adaptCandidates, type WireCandidate } from "./adapters";

describe("adaptCandidate — wire (CandidateRecord) → view-model", () => {
  it("maps the §14 CandidateRecord frame to the camelCase view-model", () => {
    // api-contracts.md §14 example, verbatim shape (a skipped, vetoed candidate).
    const wire: WireCandidate = {
      mint: "Abc123pump",
      evaluated_at_slot: 12345,
      evaluated_at_wall_ms: 1718500000000,
      status: "skipped",
      model_p: 0.31,
      reason: "safety_gate_fail",
      safety_report: {
        gate_passed: false,
        reasons: ["safety_gate_fail"],
      },
    };
    const c = adaptCandidate(wire);
    expect(c).toEqual({
      id: "Abc123pump:12345",
      firstSeenMs: 1718500000000,
      updatedMs: 1718500000000,
      // The wire has no separate token name — the mint is displayed.
      token: "Abc123pump",
      mint: "Abc123pump",
      // Not on the candidate wire — marked absent, never fabricated.
      source: null,
      status: "skipped",
      modelP: 0.31,
      // Derived from safety_report only.
      redFlags: ["safety_gate_fail"],
      safetyPassed: false,
      smartWallets: null,
      reason: "safety_gate_fail",
    });
  });

  it("BLOCKER REGRESSION: a vetoed candidate (gate_passed:false, honeypot) surfaces the warning", () => {
    // This is the exact failure the prior adapter dropped: it read a missing
    // `safety_passed`/`red_flags` key and rendered a blank safety cell.
    const c = adaptCandidate({
      mint: "Bdn456pump",
      evaluated_at_slot: 99,
      evaluated_at_wall_ms: 1718500001000,
      status: "skipped",
      model_p: null,
      reason: "honeypot",
      safety_report: { gate_passed: false, reasons: ["honeypot"] },
    });
    expect(c.safetyPassed).toBe(false);
    expect(c.redFlags).toContain("honeypot");
    // Load-bearing for the page: NOT clean → the RedFlags cell renders the flag.
    expect(c.safetyPassed && c.redFlags.length === 0).toBe(false);
  });

  it("a clean candidate (gate_passed:true, empty reasons) renders clean", () => {
    const c = adaptCandidate({
      mint: "Cln789pump",
      evaluated_at_slot: 7,
      evaluated_at_wall_ms: 1718500002000,
      status: "sniped",
      model_p: 0.84,
      reason: "entered",
      safety_report: { gate_passed: true, reasons: [] },
    });
    expect(c.safetyPassed).toBe(true);
    expect(c.redFlags).toEqual([]);
    expect(c.safetyPassed && c.redFlags.length === 0).toBe(true);
  });

  it("drops the trivial 'passed' affirmation but keeps real reason codes", () => {
    const c = adaptCandidate({
      mint: "M",
      safety_report: { gate_passed: true, reasons: ["passed"] },
    });
    expect(c.redFlags).toEqual([]);
  });

  it("fails closed: a missing safety_report is NOT treated as clean", () => {
    const c = adaptCandidate({ mint: "M", status: "skipped" });
    expect(c.safetyPassed).toBe(false);
    expect(c.redFlags).toEqual([]);
  });

  it("an unknown status falls back to the safest VIEW state (monitoring, never sniped)", () => {
    expect(adaptCandidate({ status: "exploding" }).status).toBe("monitoring");
    expect(adaptCandidate({}).status).toBe("monitoring");
  });

  it("keeps an unscored candidate's model p as null (not 0)", () => {
    expect(adaptCandidate({ model_p: null }).modelP).toBeNull();
    expect(adaptCandidate({}).modelP).toBeNull();
    expect(adaptCandidate({ model_p: 0.7 }).modelP).toBe(0.7);
  });

  it("surfaces unknown safety reason codes verbatim (never drops a safety warning)", () => {
    const c = adaptCandidate({
      safety_report: {
        gate_passed: false,
        reasons: ["honeypot", "brand_new_fingerprint"],
      },
    });
    expect(c.redFlags).toEqual(["honeypot", "brand_new_fingerprint"]);
  });

  it("the wire carries no source/smartWallets — marked absent, never invented", () => {
    const c = adaptCandidate({ mint: "M", status: "monitoring" });
    expect(c.source).toBeNull();
    expect(c.smartWallets).toBeNull();
  });

  it("never throws on a sparse frame", () => {
    expect(() => adaptCandidate({})).not.toThrow();
  });

  it("adaptCandidates maps an array and tolerates an empty/absent list", () => {
    expect(adaptCandidates([])).toEqual([]);
    expect(
      adaptCandidates([{ mint: "PNUTpump", status: "pending" }]).map(
        (c) => c.mint,
      ),
    ).toEqual(["PNUTpump"]);
  });
});
