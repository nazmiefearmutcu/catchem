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

  it("shows friendly NewsImpact-not-configured message in production_safe (BUG-OO)", () => {
    // Pre-fix this exact state ({ok:false, error_code:"missing_governance_index"})
    // rendered a red `<b>guard error</b> missing_governance_index` banner.
    // For a fresh install without merged_news on the box, the catchem
    // pipeline is fully operational — the diagnostic adapter is forbidden
    // in production_safe regardless. Surface the state as informational,
    // not as a critical error.
    const { container } = render(
      <StatusBanner
        mode="production_safe"
        diagnosticAllowed={false}
        useMlStubs={true}
        guards={{ ok: false, error_code: "missing_governance_index" }}
      />
    );
    // The benign path uses the "NewsImpact" label + friendly status, NOT
    // the alarming "guard error" wording.
    expect(screen.queryByText(/guard error/i)).not.toBeInTheDocument();
    // BUG-PP: the rendered text must NOT read "NewsImpact NewsImpact ..."
    // (label-noun duplication). The label is the bold prefix; the message
    // is the state phrase only.
    expect(container.textContent).not.toMatch(/NewsImpact\s+NewsImpact/);
    expect(screen.getByText(/not configured \(production-safe/i)).toBeInTheDocument();
    // And the banner must render in the neutral muted tone, not bad.
    const banner = container.querySelector('[role="status"]') as HTMLElement;
    expect(banner.className).not.toMatch(/border-bad/);
    expect(banner.className).toMatch(/border-\[color:var\(--border\)\]/);
  });

  it("still shows guard error tone for non-benign error_codes", () => {
    // release_gate_flipped is a REAL concern — banner must stay red.
    const { container } = render(
      <StatusBanner
        mode="production_safe"
        diagnosticAllowed={false}
        useMlStubs={true}
        guards={{ ok: false, error_code: "release_gate_flipped" }}
      />
    );
    expect(screen.getByText(/guard error/i)).toBeInTheDocument();
    expect(screen.getByText(/release_gate_flipped/)).toBeInTheDocument();
    const banner = container.querySelector('[role="status"]') as HTMLElement;
    expect(banner.className).toMatch(/border-bad/);
  });
});
