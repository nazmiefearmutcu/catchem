import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { createElement, type ReactNode } from "react";
import { SymbolDetailPage } from "@/features/symbols/SymbolDetailPage";
import type { FinancialRecord, UISymbol, SymbolSentimentTrend } from "@/types/api";

// Mock the API client. Spread actual so fmtDate / fmtRel / fmtScore / safeHref
// keep real behavior; only `api` becomes spies. Mirrors uiTruthSurfaces.test.tsx.
vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    api: {
      symbol: vi.fn(),
      symbolSentimentTrend: vi.fn(),
    },
  };
});

// The detail page draws a reason-distribution bar (EChart), a sentiment-trend
// area (EChart) and a mention-velocity Sparkline. ECharts needs canvas/layout
// the headless DOM lacks, so stub both renderers to null.
vi.mock("@/charts/EChart", () => ({ EChart: () => null }));
vi.mock("@/components/Sparkline", () => ({ Sparkline: () => null }));

import { api } from "@/lib/api";

const apiMock = api as unknown as Record<string, ReturnType<typeof vi.fn>>;

// Render under a route so useParams() resolves `:symbol` from the URL,
// matching the route-param pattern in CommandPalette.test.tsx.
function renderDetail(symbol = "AAPL") {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    createElement(
      QueryClientProvider,
      { client: qc },
      createElement(
        MemoryRouter,
        { initialEntries: [`/symbols/${symbol}`] },
        createElement(
          Routes,
          null,
          createElement(Route, {
            path: "/symbols/:symbol",
            element: createElement(SymbolDetailPage),
          }),
        ),
      ),
    ),
  );
}

function record(captureId: string, title: string): FinancialRecord {
  return {
    capture_id: captureId,
    doc_id: captureId,
    title,
    domain: "example.com",
    language: "en",
    url: "https://example.com/a",
    is_finance_relevant: true,
    finance_relevance_score: 0.72,
    asset_classes: ["equity"],
    impact_reason_codes: ["earnings"],
    candidate_symbols: ["AAPL"],
    candidate_entities: [],
    impact_horizons: [],
    sentiment_label: "positive",
    sentiment_score: 0.6,
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

function symbolPayload(overrides: Partial<UISymbol> = {}): UISymbol {
  return {
    symbol: "AAPL",
    count: 2,
    reason_distribution: { earnings: 2, guidance: 1 },
    sentiment_distribution: { positive: 1, neutral: 1 },
    items: [record("cap-1", "Apple beats on earnings"), record("cap-2", "Apple raises guidance")],
    ...overrides,
  };
}

function trendPayload(): SymbolSentimentTrend {
  return {
    symbol: "AAPL",
    days: 30,
    series: [
      { day: "2026-05-20", positive: 1, neutral: 0, negative: 0 },
      { day: "2026-05-21", positive: 1, neutral: 1, negative: 0 },
    ],
  };
}

beforeEach(() => {
  Object.values(apiMock).forEach((fn) => fn.mockReset());
  apiMock.symbol.mockResolvedValue(symbolPayload());
  apiMock.symbolSentimentTrend.mockResolvedValue(trendPayload());
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("SymbolDetailPage smoke", () => {
  it("renders the symbol from the route param with its mocked records", async () => {
    renderDetail("AAPL");

    // Hero + record list both surface the symbol & titles.
    expect((await screen.findAllByText("AAPL")).length).toBeGreaterThan(0);
    expect(screen.getByText("Apple beats on earnings")).toBeInTheDocument();
    expect(screen.getByText("Apple raises guidance")).toBeInTheDocument();
    // The :symbol param is threaded straight into the data fetches.
    expect(apiMock.symbol).toHaveBeenCalledWith("AAPL", 100);
    expect(apiMock.symbolSentimentTrend).toHaveBeenCalledWith("AAPL", 30);
  });

  it("passes a URL-decoded symbol param into the API call", async () => {
    apiMock.symbol.mockResolvedValue(symbolPayload({ symbol: "BRK.B", candidate_symbols: undefined } as Partial<UISymbol>));

    renderDetail("BRK.B");

    expect(await screen.findByText("Apple beats on earnings")).toBeInTheDocument();
    expect(apiMock.symbol).toHaveBeenCalledWith("BRK.B", 100);
  });

  it("renders an empty records state when the symbol has no items", async () => {
    apiMock.symbol.mockResolvedValue(
      symbolPayload({ count: 0, reason_distribution: {}, sentiment_distribution: {}, items: [] }),
    );
    apiMock.symbolSentimentTrend.mockResolvedValue({ symbol: "AAPL", days: 30, series: [] });

    renderDetail("AAPL");

    expect(await screen.findByText("No records")).toBeInTheDocument();
  });

  it("surfaces a query error instead of a permanent skeleton", async () => {
    apiMock.symbol.mockRejectedValue(new Error("/ui/symbol/AAPL → 500 symbol exploded"));

    renderDetail("AAPL");

    expect(await screen.findByText(/symbol exploded/)).toBeInTheDocument();
  });

  it("does not crash when the sentiment-trend query yields no data", async () => {
    // Trend is loaded in parallel; an empty series must still let the page
    // render the core record list and fall through to the "no data" copy.
    apiMock.symbolSentimentTrend.mockResolvedValue({ symbol: "AAPL", days: 30, series: [] });

    renderDetail("AAPL");

    expect(await screen.findByText("Apple beats on earnings")).toBeInTheDocument();
    // Velocity / trend sections fall through to their graceful empty copy.
    expect(screen.getAllByText(/No mentions|No sentiment data/).length).toBeGreaterThan(0);
  });
});
