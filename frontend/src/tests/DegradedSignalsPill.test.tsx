import { describe, it, expect, afterEach } from "vitest";
import { render, screen, cleanup } from "@testing-library/react";
import {
  DegradedSignalsPill,
  type DegradedDiagnostics,
} from "@/features/quant/DegradedSignalsPill";
import { setLang, _testResetLang } from "@/lib/i18n";

afterEach(() => {
  cleanup();
  _testResetLang();
});

describe("DegradedSignalsPill", () => {
  // ── healthy steady state: pill must be invisible ──────────────────────

  it("renders nothing when diagnostics is undefined", () => {
    const { container } = render(<DegradedSignalsPill diagnostics={undefined} />);
    expect(container.firstChild).toBeNull();
    expect(screen.queryByTestId("quant-degraded-pill")).toBeNull();
  });

  it("renders nothing when total_failures is 0 (nominal dashboard stays quiet)", () => {
    const diag: DegradedDiagnostics = { total_failures: 0, per_signal: {} };
    const { container } = render(<DegradedSignalsPill diagnostics={diag} />);
    expect(container.firstChild).toBeNull();
  });

  it("renders nothing for a negative count (defensive — never a phantom chip)", () => {
    const diag: DegradedDiagnostics = { total_failures: -1, per_signal: {} };
    const { container } = render(<DegradedSignalsPill diagnostics={diag} />);
    expect(container.firstChild).toBeNull();
  });

  // ── degraded state: pill renders with count + tooltip ─────────────────

  it("shows the pill with the failure count when a signal has failed", () => {
    const diag: DegradedDiagnostics = {
      total_failures: 3,
      per_signal: { spillover: 2, anomaly: 1 },
    };
    render(<DegradedSignalsPill diagnostics={diag} />);
    const pill = screen.getByTestId("quant-degraded-pill");
    expect(pill).toBeInTheDocument();
    // The {n} placeholder must be interpolated with the real count.
    expect(pill).toHaveTextContent("3");
    // Default (English) locale wording.
    expect(pill).toHaveTextContent(/signals degraded/i);
  });

  it("lists per-signal counts in the tooltip, highest-failure signal first", () => {
    const diag: DegradedDiagnostics = {
      total_failures: 6,
      per_signal: { anomaly: 1, spillover: 4, regime: 1 },
    };
    render(<DegradedSignalsPill diagnostics={diag} />);
    const pill = screen.getByTestId("quant-degraded-pill");
    const title = pill.getAttribute("title") ?? "";
    // spillover (4) must precede anomaly/regime (1) — descending by count,
    // mirroring `catchem signals --diagnostics` (v73) ordering.
    expect(title.indexOf("spillover: 4")).toBeGreaterThanOrEqual(0);
    expect(title.indexOf("spillover: 4")).toBeLessThan(title.indexOf("anomaly: 1"));
    // Newlines separate the entries so the native tooltip stacks them.
    expect(title.split("\n").length).toBe(3);
  });

  // ── i18n: count wording follows the active locale (v74 leak fix) ──────

  it("uses Turkish wording when the locale is tr", () => {
    setLang("tr");
    const diag: DegradedDiagnostics = {
      total_failures: 2,
      per_signal: { spillover: 2 },
    };
    render(<DegradedSignalsPill diagnostics={diag} />);
    const pill = screen.getByTestId("quant-degraded-pill");
    expect(pill).toHaveTextContent("2 sinyal degrade");
    // English wording must NOT leak through in Turkish locale.
    expect(pill).not.toHaveTextContent(/signals degraded/i);
  });

  it("uses English wording when the locale is en", () => {
    setLang("en");
    const diag: DegradedDiagnostics = {
      total_failures: 5,
      per_signal: { anomaly: 5 },
    };
    render(<DegradedSignalsPill diagnostics={diag} />);
    expect(screen.getByTestId("quant-degraded-pill")).toHaveTextContent(
      "5 signals degraded",
    );
  });
});
