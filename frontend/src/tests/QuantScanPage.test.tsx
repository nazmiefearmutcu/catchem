import { describe, it, expect, beforeEach, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

/**
 * SMOKE test for the QuantScan flagship page.
 *
 * QuantScanPage fans out into ~15 useQuery hooks + a streaming EventSource
 * hook + ECharts visualisations. None of that is exercisable in jsdom, so we
 * stub the three heavy seams and assert the page mounts, renders its hero +
 * window chips, and survives an interaction (clicking a window chip).
 *
 *   1. @/charts/EChart  → null component (echarts never touches the DOM).
 *   2. @/hooks/useStreamingLiveRead → frozen idle state (no EventSource).
 *   3. @/lib/api → every quant method the page calls returns a minimal-but-
 *      valid fixture. The non-`api` exports (fmtRel, safeHref, …) are spread
 *      through from the real module so the page's formatting helpers work.
 *
 * This is deliberately NOT exhaustive — only the default Events tab renders,
 * and most child panels short-circuit to empty states off the fixtures below.
 */

// ── 1. ECharts: render nothing (avoid canvas / sizing in jsdom) ────────────
vi.mock("@/charts/EChart", () => ({
  EChart: () => null,
}));

// ── 2. Streaming hook: stable idle snapshot, start/stop are no-ops ─────────
vi.mock("@/hooks/useStreamingLiveRead", () => ({
  useStreamingLiveRead: () => ({
    text: "",
    state: "idle" as const,
    error: null,
    meta: { source: null, generatedAt: null, usdCost: null, fallbackReason: null },
    start: vi.fn(),
    stop: vi.fn(),
  }),
}));

// ── 3. api: minimal valid fixtures for every method the page touches ───────
// Fixtures live inside vi.hoisted so they're initialised BEFORE the hoisted
// vi.mock factory below references them (avoids "cannot access before init").
const {
  dashboardFixture,
  liveReadFixture,
  newsVelocityFixture,
  diagnosticsFixture,
  reviewsStatusFixture,
} = vi.hoisted(() => ({
  dashboardFixture: {
    n_records_window: 1234,
    n_clusters: 0,
    clusters: [],
    source_leaderboard: null,
    novelty_timeline: [],
    lead_lag: null,
    regime: null,
    sentiment_momentum: null,
    co_occurrence: null,
    anomalies: null,
    spillover: null,
    generated_at: "2026-05-28T12:00:00Z",
  },
  liveReadFixture: {
    narrative: "The tape is quiet.",
    source: "local" as const,
    context: {},
    generated_at: "2026-05-28T12:00:00Z",
  },
  newsVelocityFixture: {
    schema_version: 1,
    generated_at: "2026-05-28T12:00:00Z",
    limit: 1000,
    bucket_minutes: 30,
    window_minutes: 360,
    current_rate_per_min: 0,
    ema_fast: 0,
    ema_slow: 0,
    baseline_rate: 0,
    baseline_std: 0,
    acceleration_z: 0,
    regime: "calm" as const,
    samples: 0,
  },
  diagnosticsFixture: {
    schema_version: 1,
    generated_at: "2026-05-28T12:00:00Z",
    total_failures: 0,
    per_signal: {},
    recent: [],
    buffer_capacity: 50,
  },
  reviewsStatusFixture: {
    deepseek_enabled: false,
    deepseek_keyed: false,
    deepseek_ready: false,
    model: "stub",
    sampling_rate: 0,
    usd_cap: 1,
    usd_spent: 0,
    usd_remaining: 1,
    exhausted: false,
    primary_reviewer_version: "stub-1",
    tokens: { input: 0, output: 0, calls: 0, errors: 0 },
    base_url: "",
    generated_at: "2026-05-28T12:00:00Z",
  },
}));

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    api: {
      quantDashboard: vi.fn(async (_limit?: number) => dashboardFixture),
      quantLiveRead: vi.fn(async () => liveReadFixture),
      quantNewsVelocity: vi.fn(async () => newsVelocityFixture),
      quantDiagnostics: vi.fn(async () => diagnosticsFixture),
      reviewsStatus: vi.fn(async () => reviewsStatusFixture),
      // Tab-scoped panels (not the default Events tab, but mocked defensively).
      quantSentimentDispersion: vi.fn(async () => ({ result: null, buckets: [] })),
      quantIntensity: vi.fn(async () => ({ buckets: [] })),
      quantMarketTime: vi.fn(async () => ({ buckets: [], total_records: 0 })),
      quantArrivalHeatmap: vi.fn(async () => ({
        cells: [],
        peak_cells: [],
        weekday_labels: [],
        total_samples: 0,
        max_count: 0,
        timezone: "America/New_York",
      })),
      quantSymbolCorrelation: vi.fn(async () => ({ pairs: [], bucket_minutes: 60, min_mentions: 3 })),
      quantPersistence: vi.fn(async () => ({ buckets: [], window_days: 7 })),
      quantClusterMembers: vi.fn(async () => ({ members: [] })),
      quantHeatmapRecords: vi.fn(async () => ({ records: [], total_returned: 0 })),
      quantRecordDetail: vi.fn(async () => ({ record: {}, reviews: [], reaction: null })),
      quantExplain: vi.fn(async () => ({ kind: "cluster", narrative: "—", source: "local" })),
      exportQuantUrl: (limit = 1000) => `/api/export/quant?format=json&limit=${limit}`,
    },
  };
});

import { QuantScanPage } from "@/features/quant/QuantScanPage";
import { api } from "@/lib/api";

function renderPage() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <MemoryRouter initialEntries={["/scan"]}>
      <QueryClientProvider client={qc}>
        <QuantScanPage />
      </QueryClientProvider>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  // Reset call history between tests. clearAllMocks keeps each vi.fn's
  // implementation (the async fixture returns) intact, so the api mock
  // still resolves valid shapes after the clear.
  vi.clearAllMocks();
});

describe("QuantScanPage (smoke)", () => {
  it("renders the hero live-read title without crashing", async () => {
    renderPage();
    // The hero h1 always renders — it shows the active narrative source label.
    const title = await screen.findByTestId("live-read-title");
    expect(title).toBeInTheDocument();
    // The default dashboard query fires on mount at the initial window (1000).
    await waitFor(() => expect(api.quantDashboard).toHaveBeenCalledWith(1000));
  });

  it("renders the five window-size chips", async () => {
    renderPage();
    await screen.findByTestId("live-read-title");
    for (const label of ["200", "500", "1,000", "2,000", "5,000"]) {
      expect(screen.getByRole("button", { name: label })).toBeInTheDocument();
    }
  });

  it("clicking a window chip re-queries the dashboard and does not crash", async () => {
    renderPage();
    await screen.findByTestId("live-read-title");

    const chip = screen.getByRole("button", { name: "2,000" });
    act(() => {
      fireEvent.click(chip);
    });

    // The window-size change threads through to a fresh dashboard fetch.
    await waitFor(() => expect(api.quantDashboard).toHaveBeenCalledWith(2000));
    // Hero is still mounted — the interaction didn't blow up the tree.
    expect(screen.getByTestId("live-read-title")).toBeInTheDocument();
  });
});
