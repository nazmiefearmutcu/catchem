import { describe, it, expect } from "vitest";
import {
  BENIGN_GUARD_ERROR_CODES,
  guardSeverity,
  guardStatusMessage,
  isBenignGuardState,
} from "@/lib/guardState";
import type { GuardSnapshot, Mode } from "@/types/api";

const missingGovIdx: GuardSnapshot = { ok: false, error_code: "missing_governance_index" };
const malformed: GuardSnapshot = { ok: false, error_code: "malformed_governance_index" };
const gateFlipped: GuardSnapshot = { ok: false, error_code: "release_gate_flipped" };
const okQuarantined: GuardSnapshot = {
  ok: true,
  release_gate_passed: false,
  quarantine_state: "QUARANTINED_REGRESSIVE_MULTIMODAL",
};
const okPassed: GuardSnapshot = {
  ok: true,
  release_gate_passed: true,
  quarantine_state: "RELEASED",
};

describe("guardState", () => {
  it("registers missing_governance_index as benign", () => {
    expect(BENIGN_GUARD_ERROR_CODES.has("missing_governance_index")).toBe(true);
  });

  it("treats missing_governance_index as info in production_safe", () => {
    expect(guardSeverity(missingGovIdx, "production_safe")).toBe("info");
    expect(isBenignGuardState(missingGovIdx, "production_safe")).toBe(true);
  });

  it("treats missing_governance_index as warn in research_diagnostic", () => {
    // Without governance, the diagnostic mode can't actually operate.
    expect(guardSeverity(missingGovIdx, "research_diagnostic")).toBe("warn");
    expect(isBenignGuardState(missingGovIdx, "research_diagnostic")).toBe(false);
  });

  it("treats malformed_governance_index as bad in every mode", () => {
    const modes: Mode[] = [
      "production_safe", "replay_existing", "live_tail", "research_diagnostic",
    ];
    for (const m of modes) {
      expect(guardSeverity(malformed, m)).toBe("bad");
      expect(isBenignGuardState(malformed, m)).toBe(false);
    }
  });

  it("treats release_gate_flipped as bad even in production_safe", () => {
    expect(guardSeverity(gateFlipped, "production_safe")).toBe("bad");
    expect(isBenignGuardState(gateFlipped, "production_safe")).toBe(false);
  });

  it("treats a healthy-quarantined guard as ok", () => {
    expect(guardSeverity(okQuarantined, "production_safe")).toBe("ok");
  });

  it("treats an unexpectedly-passed release gate as bad", () => {
    expect(guardSeverity(okPassed, "production_safe")).toBe("bad");
  });

  it("missing guards snapshot is info, not bad", () => {
    expect(guardSeverity(undefined, "production_safe")).toBe("info");
  });

  it("status message is human-friendly for the benign production case", () => {
    const msg = guardStatusMessage(missingGovIdx, "production_safe");
    expect(msg.toLowerCase()).toContain("not configured");
    expect(msg.toLowerCase()).toContain("production-safe");
  });

  it("status message surfaces raw error_code for non-benign failures", () => {
    expect(guardStatusMessage(gateFlipped, "production_safe")).toContain("release_gate_flipped");
    expect(guardStatusMessage(malformed, "production_safe")).toContain("malformed_governance_index");
  });

  it("BUG-PP: status message does NOT start with 'NewsImpact' to avoid label duplication", () => {
    // Callers (StatusBanner, ModelControlsPage, OpsPage) prepend their own
    // "NewsImpact" label — if the message also starts with "NewsImpact"
    // the rendered text reads "NewsImpact NewsImpact not configured".
    const cases = [
      guardStatusMessage(missingGovIdx, "production_safe"),
      guardStatusMessage(missingGovIdx, "research_diagnostic"),
      guardStatusMessage(malformed, "production_safe"),
      guardStatusMessage(gateFlipped, "production_safe"),
      guardStatusMessage(undefined, "production_safe"),
    ];
    for (const msg of cases) {
      expect(msg).not.toMatch(/^NewsImpact/);
    }
  });
});
