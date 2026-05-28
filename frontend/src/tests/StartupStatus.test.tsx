import { describe, it, expect, beforeEach, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createElement, type ReactNode } from "react";
import type { SidecarStatus } from "@/types/api";

// StartupStatus is a one-line react-query consumer of api.sidecarStatus. We
// mock the api module so each test can drive the loading / error / success
// branches deterministically without touching the network.
const sidecarStatusMock = vi.fn();
vi.mock("@/lib/api", () => ({
  api: {
    sidecarStatus: () => sidecarStatusMock(),
  },
}));

// Import after vi.mock is registered so the component binds the mocked api.
import { StartupStatus } from "@/components/StartupStatus";

function withQuery(child: ReactNode): ReactNode {
  // The component bakes in `retry: 1`, which overrides any client-level
  // `retry: false`. Left at the default exponential back-off (~1s) the single
  // retry pushes the error state past waitFor's 1s window, so the query reads
  // as "still loading". Collapsing retryDelay to 0 lets that retry fire
  // instantly and the error branch settles immediately — purely a test-timing
  // knob, the component's own retry contract is untouched.
  const qc = new QueryClient({
    defaultOptions: { queries: { retryDelay: 0 } },
  });
  return createElement(QueryClientProvider, { client: qc }, child);
}

function status(overrides: Partial<SidecarStatus> = {}): SidecarStatus {
  return {
    healthy: true,
    api_host: "127.0.0.1",
    api_port: 8421,
    pid: 4242,
    uptime_seconds: 12.7,
    records: { total: 91, finance_relevant: 30 },
    dlq: 0,
    diagnostic_enabled: false,
    generated_at: "2026-05-28T00:00:00Z",
    ...overrides,
  };
}

beforeEach(() => {
  sidecarStatusMock.mockReset();
});

describe("<StartupStatus>", () => {
  it("shows the 'starting…' placeholder while the status query is in flight", () => {
    // A never-resolving promise keeps the query in its loading state.
    sidecarStatusMock.mockReturnValue(new Promise<SidecarStatus>(() => {}));
    render(withQuery(<StartupStatus />));
    const el = screen.getByText("starting…");
    expect(el).toBeInTheDocument();
    expect(el.getAttribute("aria-live")).toBe("polite");
  });

  it("renders pid / uptime / records once the status resolves", async () => {
    sidecarStatusMock.mockResolvedValue(status({ pid: 4242, uptime_seconds: 12.7 }));
    render(withQuery(<StartupStatus />));
    // pid value
    expect(await screen.findByText("4242")).toBeInTheDocument();
    // uptime is rounded to a whole second + "s" suffix (12.7 → 13s)
    expect(screen.getByText("13s")).toBeInTheDocument();
    // records.total
    expect(screen.getByText("91")).toBeInTheDocument();
    // The DIAG flag is hidden when diagnostics are off.
    expect(screen.queryByText("DIAG")).toBeNull();
  });

  it("renders the DIAG flag when diagnostics are enabled", async () => {
    sidecarStatusMock.mockResolvedValue(status({ diagnostic_enabled: true }));
    render(withQuery(<StartupStatus />));
    expect(await screen.findByText("DIAG")).toBeInTheDocument();
  });

  it("shows 'sidecar unreachable' when the status query errors", async () => {
    sidecarStatusMock.mockRejectedValue(new Error("connection refused"));
    render(withQuery(<StartupStatus />));
    await waitFor(() => {
      const el = screen.getByText("sidecar unreachable");
      expect(el).toBeInTheDocument();
      expect(el.getAttribute("role")).toBe("status");
    });
  });
});
