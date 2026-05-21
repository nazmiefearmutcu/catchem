import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { createElement, type ReactNode } from "react";
import { FeedPage } from "@/features/feed/FeedPage";
import { SymbolsPage } from "@/features/symbols/SymbolsPage";
import { OpsPage } from "@/features/ops/OpsPage";
import { Shell } from "@/layout/Shell";
import type { FinancialRecord, GuardSnapshot } from "@/types/api";

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    api: {
      facets: vi.fn(),
      newsStatus: vi.fn(),
      newsPollNow: vi.fn(),
      archiveStatus: vi.fn(),
      archiveNow: vi.fn(),
      recent: vi.fn(),
      byAssetClass: vi.fn(),
      byReason: vi.fn(),
      bySymbol: vi.fn(),
      topSymbols: vi.fn(),
      summary: vi.fn(),
      config: vi.fn(),
      metrics: vi.fn(),
      guards: vi.fn(),
    },
  };
});

vi.mock("@/hooks/useLiveStream", () => ({
  useLiveStream: vi.fn(() => ({ status: "idle", lastBeatAt: null })),
}));

vi.mock("@/hooks/useDesktopAlerts", () => ({
  getAlertThreshold: vi.fn(() => 0.65),
  setAlertThreshold: vi.fn((value: number) => value),
  useDesktopAlertState: vi.fn(() => ["off", vi.fn()]),
  useDesktopAlerts: vi.fn(),
}));

vi.mock("@/components/CommandPalette", () => ({
  CommandPalette: () => null,
}));

vi.mock("@/components/ToastTray", () => ({
  ToastTray: () => null,
}));

import { api } from "@/lib/api";
import { useLiveStream } from "@/hooks/useLiveStream";

const apiMock = api as unknown as Record<string, ReturnType<typeof vi.fn>>;
const liveStreamMock = vi.mocked(useLiveStream);

function renderWithProviders(ui: ReactNode, initialEntries = ["/"]) {
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

function jsonDefaults() {
  apiMock.facets.mockResolvedValue({
    window_total: 0,
    window_relevant: 0,
    asset_classes: [],
    reason_codes: [],
    symbols: [],
    domains: [],
    sentiments: [],
  });
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
  apiMock.topSymbols.mockResolvedValue({ items: [] });
  apiMock.summary.mockResolvedValue(summary());
  apiMock.config.mockResolvedValue({});
  apiMock.metrics.mockResolvedValue({});
  apiMock.guards.mockResolvedValue(guard());
}

beforeEach(() => {
  Object.values(apiMock).forEach((fn) => fn.mockReset());
  liveStreamMock.mockReset();
  liveStreamMock.mockReturnValue({ status: "idle", lastBeatAt: null });
  jsonDefaults();
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("UI truth regressions", () => {
  it("Feed shows news errors as the primary status, not live", async () => {
    apiMock.newsStatus.mockResolvedValue({
      enabled: true,
      feeds: 3,
      interval_seconds: 10,
      last_run_at: null,
      next_run_at: null,
      last_ingested: 0,
      total_ingested: 10,
      last_error: "rss timeout",
      is_polling: false,
      last_new_at: null,
      empty_ticks: 0,
      last_avg_publisher_lag_seconds: null,
      last_median_publisher_lag_seconds: null,
    });

    renderWithProviders(createElement(FeedPage), ["/feed"]);

    expect(await screen.findByText("error")).toBeInTheDocument();
    expect(screen.getByText(/rss timeout/)).toBeInTheDocument();
    expect(screen.queryByText("live")).not.toBeInTheDocument();
  });

  it("Feed treats the first non-empty snapshot after an empty load as baseline, not new", async () => {
    apiMock.recent
      .mockResolvedValueOnce({ items: [] })
      .mockResolvedValueOnce({ items: [record("cap-1", "First baseline row")] });

    const { qc } = renderWithProviders(createElement(FeedPage), ["/feed"]);

    await screen.findByText("No matches");
    await act(async () => {
      await qc.invalidateQueries({
        predicate: (q) => Array.isArray(q.queryKey) && q.queryKey[0] === "feed-list",
      });
    });
    await waitFor(() => expect(screen.getByText("First baseline row")).toBeInTheDocument());
    expect(screen.queryByLabelText("freshly arrived")).not.toBeInTheDocument();
  });

  it("Feed archive renders a nullable local cap honestly", async () => {
    apiMock.archiveStatus.mockResolvedValue({
      enabled: true,
      drive_dir: "/tmp/archive",
      interval_seconds: 30,
      local_cap_rows: null,
      last_run_at: null,
      last_archived_count: 0,
      total_archived: 0,
      last_error: null,
      is_archiving: false,
      current_csv_path: null,
    });

    renderWithProviders(createElement(FeedPage), ["/feed"]);

    expect(await screen.findByText("cap —")).toBeInTheDocument();
    expect(screen.queryByText("cap null")).not.toBeInTheDocument();
  });

  it("Shell keeps the live dot idle when the stream has opened but no beat arrived", async () => {
    liveStreamMock.mockReturnValue({ status: "open", lastBeatAt: null });
    renderWithProviders(
      createElement(Routes, null,
        createElement(Route, { path: "/", element: createElement(Shell) },
          createElement(Route, { index: true, element: createElement("div", null, "home") }),
        ),
      ),
    );

    expect(await screen.findByText("idle")).toBeInTheDocument();
    expect(screen.queryByText("open")).not.toBeInTheDocument();
  });

  it("Symbols shows a no-match state when filtering removes all server results", async () => {
    apiMock.topSymbols.mockResolvedValue({ items: [{ symbol: "AAPL", count: 4 }] });
    renderWithProviders(createElement(SymbolsPage), ["/symbols"]);

    fireEvent.change(await screen.findByLabelText("filter symbol mentions"), { target: { value: "BTC" } });

    expect(screen.getByText("No matching symbol mentions")).toBeInTheDocument();
    expect(screen.getByText("Clear or change the symbol mention filter.")).toBeInTheDocument();
  });

  it("Ops shows /ui/guards query errors instead of a permanent skeleton", async () => {
    apiMock.guards.mockRejectedValue(new Error("/ui/guards → 500 guard exploded"));
    renderWithProviders(createElement(OpsPage), ["/ops"]);

    expect(await screen.findByText(/guard exploded/)).toBeInTheDocument();
    expect(screen.queryByTestId("skeleton")).not.toBeInTheDocument();
  });

  it("Ops avoids undefined governance hashes and surfaces guard error_code", async () => {
    apiMock.guards.mockResolvedValue({
      ...guard(),
      governance_index_sha256: undefined,
      error_code: "GOV_HASH_MISSING",
    } as GuardSnapshot & { error_code: string });
    renderWithProviders(createElement(OpsPage), ["/ops"]);

    const guardSection = await screen.findByText("NewsImpact guard");
    const section = guardSection.closest("section");
    expect(section).not.toBeNull();
    expect(within(section as HTMLElement).getByText("not reported")).toBeInTheDocument();
    expect(within(section as HTMLElement).getByText("GOV_HASH_MISSING")).toBeInTheDocument();
    expect(within(section as HTMLElement).queryByText(/undefined/)).not.toBeInTheDocument();
  });
});

function guard(): GuardSnapshot {
  return {
    ok: true,
    release_gate_passed: false,
    quarantine_state: "QUARANTINED",
    fusion_verdict_class: "FUSION_REGRESSIVE",
    safe_to_publish: false,
    safe_to_promote: false,
    governance_index_sha256: "abcdef0123456789abcdef0123456789",
  };
}

function summary() {
  return {
    mode: "production_safe",
    is_production_safe: true,
    diagnostic_allowed: false,
    use_ml_stubs: true,
    totals: { total: 1, finance_relevant: 1 },
    diagnostic_count: 0,
    asset_class_distribution: {},
    reason_code_distribution: {},
    sentiment_distribution: {},
    recent_top: [],
    dlq: 0,
    model_versions: {},
    guards: guard(),
    generated_at: "2026-05-21T00:00:00Z",
  };
}

function record(captureId: string, title: string): FinancialRecord {
  return {
    capture_id: captureId,
    doc_id: captureId,
    title,
    domain: "example.com",
    language: "en",
    url: "https://example.com",
    is_finance_relevant: true,
    finance_relevance_score: 0.75,
    asset_classes: ["equity"],
    impact_reason_codes: ["earnings"],
    candidate_symbols: ["AAPL"],
    candidate_entities: [],
    impact_horizons: [],
    sentiment_label: "neutral",
    sentiment_score: null,
    evidence_sentences: [],
    reason_text: null,
    component_scores: {},
    diagnostic_multimodal_enabled: false,
    diagnostic_multimodal_result: null,
    processing_mode: "production_safe",
    model_versions: {},
    published_ts: "2026-05-21T00:00:00Z",
    created_at: "2026-05-21T00:00:00Z",
  };
}
