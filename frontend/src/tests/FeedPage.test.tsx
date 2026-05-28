import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { createElement, type ReactNode } from "react";
import { FeedPage } from "@/features/feed/FeedPage";
import type { FinancialRecord } from "@/types/api";

// ── api boundary ──────────────────────────────────────────────────────────
// FeedPage hits api.facets / listTags / newsStatus / archiveStatus on mount,
// plus api.recent (the default list query when no filter chip is active).
// We mock the whole `api` object so no real fetch is attempted, but keep the
// pure helpers (fmtRel/fmtScore/safeHref/scoreToneClass) from the real module
// since the rows call them during render.
vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    api: {
      facets: vi.fn(),
      listTags: vi.fn(),
      newsStatus: vi.fn(),
      newsPollNow: vi.fn(),
      archiveStatus: vi.fn(),
      archiveNow: vi.fn(),
      recent: vi.fn(),
      recordsByTag: vi.fn(),
      byAssetClass: vi.fn(),
      byReason: vi.fn(),
      bySymbol: vi.fn(),
      exportRecordsUrl: vi.fn(() => "/ui/export/records.csv"),
    },
  };
});

// The record drawer is a heavy overlay child (locks body, own query, focus
// trap). The smoke test never opens it, but mocking it to null keeps the
// surface lean and isolates the list behavior.
vi.mock("@/features/record-detail/RecordDrawer", () => ({
  RecordDrawer: () => null,
}));

// Desktop-alert hook reads/writes localStorage + fires toasts; pin it so the
// alert chip renders deterministically and bulk-action toasts are inert.
vi.mock("@/hooks/useDesktopAlerts", () => ({
  getAlertThreshold: vi.fn(() => 0.65),
  setAlertThreshold: vi.fn((value: number) => value),
  useDesktopAlertState: vi.fn(() => ["off", vi.fn()]),
  pushToast: vi.fn(),
}));

import { api } from "@/lib/api";

const apiMock = api as unknown as Record<string, ReturnType<typeof vi.fn>>;

function renderWithProviders(ui: ReactNode, initialEntries = ["/feed"]) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const result = render(
    createElement(
      QueryClientProvider,
      { client: qc },
      createElement(MemoryRouter, { initialEntries }, ui),
    ),
  );
  return { ...result, qc };
}

function record(captureId: string, title: string, domain: string): FinancialRecord {
  return {
    capture_id: captureId,
    doc_id: captureId,
    title,
    domain,
    language: "en",
    url: `https://${domain}/article`,
    is_finance_relevant: true,
    finance_relevance_score: 0.72,
    asset_classes: ["equity"],
    impact_reason_codes: ["EARNINGS"],
    candidate_symbols: ["AAPL"],
    candidate_entities: [],
    impact_horizons: [],
    sentiment_label: "positive",
    sentiment_score: 0.4,
    evidence_sentences: [],
    reason_text: null,
    component_scores: {},
    diagnostic_multimodal_enabled: false,
    diagnostic_multimodal_result: null,
    processing_mode: "live",
    model_versions: {},
    published_ts: "2026-05-28T10:00:00Z",
    created_at: "2026-05-28T10:00:01Z",
  };
}

// Quiet poller/archiver heroes (enabled:false) so the test focuses on the
// list itself. listTags returns no user tags so the sidebar stays minimal.
function quietDefaults() {
  apiMock.facets.mockResolvedValue({
    window_total: 0,
    window_relevant: 0,
    asset_classes: [],
    reason_codes: [],
    symbols: [],
    domains: [],
    sentiments: [],
  });
  apiMock.listTags.mockResolvedValue({ items: [] });
  apiMock.newsStatus.mockResolvedValue({
    enabled: false,
    feeds: 0,
    interval_seconds: 10,
    last_run_at: null,
    next_run_at: null,
    last_ingested: 0,
    total_ingested: 0,
    last_error: null,
    is_polling: false,
    last_new_at: null,
    empty_ticks: 0,
    last_avg_publisher_lag_seconds: null,
    last_median_publisher_lag_seconds: null,
  });
  apiMock.archiveStatus.mockResolvedValue({
    enabled: false,
    drive_dir: null,
    interval_seconds: 30,
    local_cap_rows: 150,
    last_run_at: null,
    last_archived_count: 0,
    total_archived: 0,
    last_error: null,
    is_archiving: false,
    current_csv_path: null,
  });
  apiMock.recent.mockResolvedValue({ items: [] });
}

beforeEach(() => {
  Object.values(apiMock).forEach((fn) => fn.mockReset());
  quietDefaults();
  apiMock.exportRecordsUrl.mockReturnValue("/ui/export/records.csv");
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("FeedPage (smoke)", () => {
  it("renders without crashing and shows the recent records (titles + domains)", async () => {
    apiMock.recent.mockResolvedValue({
      items: [
        record("cap-1", "Fed holds rates steady", "reuters.com"),
        record("cap-2", "Apple beats Q3 estimates", "bloomberg.com"),
      ],
    });

    renderWithProviders(createElement(FeedPage));

    expect(await screen.findByText("Fed holds rates steady")).toBeInTheDocument();
    expect(screen.getByText("Apple beats Q3 estimates")).toBeInTheDocument();
    expect(screen.getByText("reuters.com")).toBeInTheDocument();
    expect(screen.getByText("bloomberg.com")).toBeInTheDocument();
    // Default list query is the unfiltered "recent" feed.
    expect(apiMock.recent).toHaveBeenCalled();
  });

  it("renders the empty state when the feed has no records", async () => {
    apiMock.recent.mockResolvedValue({ items: [] });

    renderWithProviders(createElement(FeedPage));

    expect(await screen.findByText("No matches")).toBeInTheDocument();
    expect(screen.getByText("Try clearing filters.")).toBeInTheDocument();
  });

  it("typing in the search box filters the visible records client-side", async () => {
    apiMock.recent.mockResolvedValue({
      items: [
        record("cap-1", "Fed holds rates steady", "reuters.com"),
        record("cap-2", "Apple beats Q3 estimates", "bloomberg.com"),
      ],
    });

    renderWithProviders(createElement(FeedPage));

    // Both rows present before filtering.
    await screen.findByText("Fed holds rates steady");
    expect(screen.getByText("Apple beats Q3 estimates")).toBeInTheDocument();

    const search = screen.getByPlaceholderText("title, domain, symbol…");
    fireEvent.change(search, { target: { value: "apple" } });

    // The non-matching row drops out; the matching row stays.
    await waitFor(() => {
      expect(screen.queryByText("Fed holds rates steady")).not.toBeInTheDocument();
    });
    expect(screen.getByText("Apple beats Q3 estimates")).toBeInTheDocument();
  });

  it("selecting a row reveals the bulk-action toolbar", async () => {
    apiMock.recent.mockResolvedValue({
      items: [record("cap-1", "Fed holds rates steady", "reuters.com")],
    });

    renderWithProviders(createElement(FeedPage));

    await screen.findByText("Fed holds rates steady");
    // Toolbar is hidden until something is selected.
    expect(screen.queryByTestId("feed-bulk-toolbar")).not.toBeInTheDocument();

    fireEvent.click(screen.getByTestId("feed-row-checkbox-cap-1"));

    const toolbar = await screen.findByTestId("feed-bulk-toolbar");
    expect(toolbar).toBeInTheDocument();
    // "1 selected" also appears in the top select-all bar, so scope to the toolbar.
    expect(within(toolbar).getByText("1 selected")).toBeInTheDocument();
  });
});
