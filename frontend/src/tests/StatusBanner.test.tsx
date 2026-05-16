import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { StatusBanner } from "@/components/StatusBanner";

describe("StatusBanner", () => {
  it("shows release_gate state and quarantine", () => {
    render(
      <StatusBanner
        mode="production_safe"
        diagnosticAllowed={false}
        useMlStubs={true}
        guards={{
          ok: true,
          release_gate_passed: false,
          quarantine_state: "QUARANTINED_REGRESSIVE_MULTIMODAL",
          fusion_verdict_class: "FUSION_REGRESSIVE",
          safe_to_publish: false,
          safe_to_promote: false,
          governance_index_sha256: "abcdef0123456789".repeat(4),
        }}
      />
    );
    expect(screen.getByText(/QUARANTINED_REGRESSIVE_MULTIMODAL/)).toBeInTheDocument();
    expect(screen.getByText(/release_gate/)).toBeInTheDocument();
    expect(screen.getByText(/production-safe/)).toBeInTheDocument();
  });

  it("warns when diagnostic is active", () => {
    render(
      <StatusBanner
        mode="research_diagnostic"
        diagnosticAllowed={true}
        useMlStubs={true}
        guards={{ ok: true, release_gate_passed: false, quarantine_state: "Q" }}
      />
    );
    expect(screen.getByText(/diagnostic mode active/)).toBeInTheDocument();
  });

  it("shows guard error tone when guard fails", () => {
    render(
      <StatusBanner
        mode="production_safe"
        diagnosticAllowed={false}
        useMlStubs={true}
        guards={{ ok: false, error: "missing governance_index.json" }}
      />
    );
    expect(screen.getByText(/missing governance_index/)).toBeInTheDocument();
  });
});
