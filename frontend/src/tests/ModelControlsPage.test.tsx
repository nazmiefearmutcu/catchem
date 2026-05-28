import { describe, it, expect, beforeEach, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import type { ReactNode } from "react";
import { ModelControlsPage } from "@/features/model-controls/ModelControlsPage";
import type { AppInfo, SidecarStatus, GuardSnapshot } from "@/types/api";

// ── api boundary ──────────────────────────────────────────────────────────
// ModelControlsPage hits api.appInfo / sidecarStatus / guards on mount (each
// behind a polling useQuery). Mock the whole `api` object so no real fetch is
// attempted, but keep the pure helpers from the real module — JargonTooltip +
// guardState read nothing from `api`, yet other consumers of this module do.
vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    api: {
      appInfo: vi.fn(),
      sidecarStatus: vi.fn(),
      guards: vi.fn(),
    },
  };
});

import { api } from "@/lib/api";

const apiMock = api as unknown as Record<string, ReturnType<typeof vi.fn>>;

function makeAppInfo(over: Partial<AppInfo> = {}): AppInfo {
  return {
    name: "catchem",
    version: "0.99.0",
    commit_sha: "abc1234def5678",
    branch: "main",
    mode: "production_safe",
    use_ml_stubs: true,
    diagnostic_allowed: false,
    static_bundle_present: true,
    model_versions: { relevance: "stub-v1", sentiment: "hf-roberta-1" },
    generated_at: "2026-05-28T12:00:00Z",
    ...over,
  };
}

function makeStatus(over: Partial<SidecarStatus> = {}): SidecarStatus {
  return {
    healthy: true,
    api_host: "127.0.0.1",
    api_port: 8087,
    pid: 4242,
    uptime_seconds: 3725,
    records: { total: 120, finance_relevant: 80 },
    dlq: 0,
    diagnostic_enabled: false,
    generated_at: "2026-05-28T12:00:00Z",
    ...over,
  };
}

// Benign fresh-install guard: missing_governance_index + production_safe.
function makeGuards(over: Partial<GuardSnapshot> = {}): GuardSnapshot {
  return {
    ok: false,
    error_code: "missing_governance_index",
    ...over,
  };
}

function renderPage(initialEntries = ["/model"]): ReturnType<typeof render> {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <MemoryRouter initialEntries={initialEntries}>
      <QueryClientProvider client={qc}>
        <ModelControlsPage />
      </QueryClientProvider>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe("ModelControlsPage smoke", () => {
  it("renders the synthesized hero headline + KPI tiles from mocked data", async () => {
    apiMock.appInfo.mockResolvedValue(makeAppInfo());
    apiMock.sidecarStatus.mockResolvedValue(makeStatus());
    apiMock.guards.mockResolvedValue(makeGuards());

    renderPage();

    // use_ml_stubs:true + healthy + diagnostic off → stubs headline.
    expect(
      await screen.findByRole("heading", {
        name: /running on deterministic ml stubs/i,
      }),
    ).toBeInTheDocument();

    // Provenance subtitle threads version + pid.
    expect(screen.getByText(/v0\.99\.0/)).toBeInTheDocument();
    expect(screen.getByText(/pid 4242/)).toBeInTheDocument();

    // Connection card surfaces host:port.
    expect(screen.getAllByText(/127\.0\.0\.1:8087/).length).toBeGreaterThan(0);

    // Model-version provenance rows render the mocked entries.
    expect(screen.getByText("stub-v1")).toBeInTheDocument();
    expect(screen.getByText("hf-roberta-1")).toBeInTheDocument();
  });

  it("flips the hero to a bad tone when the sidecar is unreachable", async () => {
    apiMock.appInfo.mockResolvedValue(makeAppInfo({ use_ml_stubs: false }));
    apiMock.sidecarStatus.mockResolvedValue(makeStatus({ healthy: false }));
    apiMock.guards.mockResolvedValue(makeGuards());

    renderPage();

    // sidecarDown wins the headline over every other signal.
    expect(
      await screen.findByRole("heading", { name: /sidecar is unreachable/i }),
    ).toBeInTheDocument();
  });

  it("shows the benign NewsImpact note for a fresh production-safe install", async () => {
    apiMock.appInfo.mockResolvedValue(makeAppInfo());
    apiMock.sidecarStatus.mockResolvedValue(makeStatus());
    apiMock.guards.mockResolvedValue(makeGuards());

    renderPage();

    await screen.findByRole("heading", {
      name: /running on deterministic ml stubs/i,
    });
    // isBenignGuardState → informational copy, not a red error box.
    expect(screen.getByText(/is not configured/i)).toBeInTheDocument();
  });

  it("renders the loading skeleton without crashing while queries are pending", () => {
    // Never-resolving promises keep info/status in the isLoading branch.
    apiMock.appInfo.mockReturnValue(new Promise<AppInfo>(() => {}));
    apiMock.sidecarStatus.mockReturnValue(new Promise<SidecarStatus>(() => {}));
    apiMock.guards.mockReturnValue(new Promise<GuardSnapshot>(() => {}));

    expect(() => renderPage()).not.toThrow();
    expect(screen.queryByRole("heading")).toBeNull();
  });

  it("surfaces an error box when app-info fails", async () => {
    apiMock.appInfo.mockRejectedValue(new Error("boom-appinfo"));
    apiMock.sidecarStatus.mockResolvedValue(makeStatus());
    apiMock.guards.mockResolvedValue(makeGuards());

    renderPage();

    await waitFor(() =>
      expect(screen.getByText(/boom-appinfo/i)).toBeInTheDocument(),
    );
  });
});
