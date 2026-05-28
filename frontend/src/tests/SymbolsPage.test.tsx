import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { createElement, type ReactNode } from "react";
import { SymbolsPage } from "@/features/symbols/SymbolsPage";

// Mock the API client. Spread the actual module so the format helpers
// (fmtDate / fmtPct / fmtScore / safeHref) keep their real implementations
// — only `api` is swapped for vi.fn() spies. Mirrors uiTruthSurfaces.test.tsx.
vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    api: {
      topSymbols: vi.fn(),
      quotes: vi.fn(),
    },
  };
});

// SymbolsPage itself draws no charts, but mock these defensively so a future
// chart import can't drag a canvas/ECharts dependency into the headless DOM.
vi.mock("@/charts/EChart", () => ({ EChart: () => null }));
vi.mock("@/components/Sparkline", () => ({ Sparkline: () => null }));

import { api } from "@/lib/api";

const apiMock = api as unknown as Record<string, ReturnType<typeof vi.fn>>;

function renderPage(ui: ReactNode, initialEntries = ["/symbols"]) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    createElement(
      QueryClientProvider,
      { client: qc },
      createElement(MemoryRouter, { initialEntries }, ui),
    ),
  );
}

function quoteRow(symbol: string, overrides: Record<string, unknown> = {}) {
  return {
    symbol,
    provider: "local_fixture",
    as_of: "2024-01-02T21:00:00+00:00",
    retrieved_at: "2026-05-21T12:00:00+00:00",
    currency: "USD",
    last: 189.98,
    prev_close: 188.85,
    change_abs: 1.13,
    change_pct: 0.0059846,
    market_state: "fixture_snapshot",
    stale_after: "2024-01-02T21:15:00+00:00",
    freshness_status: "stale",
    error_code: null,
    ...overrides,
  };
}

beforeEach(() => {
  Object.values(apiMock).forEach((fn) => fn.mockReset());
  // Sensible defaults; individual tests override as needed.
  apiMock.topSymbols.mockResolvedValue({ items: [] });
  apiMock.quotes.mockResolvedValue({
    items: [],
    provider: "local_fixture",
    generated_at: "2026-05-21T12:00:00+00:00",
  });
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("SymbolsPage smoke", () => {
  it("renders the mocked top symbol and its mention count", async () => {
    apiMock.topSymbols.mockResolvedValue({
      items: [
        { symbol: "AAPL", count: 12 },
        { symbol: "MSFT", count: 5 },
      ],
    });
    apiMock.quotes.mockResolvedValue({
      items: [quoteRow("AAPL"), quoteRow("MSFT")],
      provider: "local_fixture",
      generated_at: "2026-05-21T12:00:00+00:00",
    });

    renderPage(createElement(SymbolsPage));

    // AAPL appears (hero top-3 tile + mention-count list both render it).
    expect((await screen.findAllByText("AAPL")).length).toBeGreaterThan(0);
    // "N mentions" shows in both the hero tile and the count list → use All.
    expect(screen.getAllByText("12 mentions").length).toBeGreaterThan(0);
    expect(screen.getAllByText("5 mentions").length).toBeGreaterThan(0);
    // Quote fan-out is gated on the symbol set being present.
    expect(apiMock.quotes).toHaveBeenCalledWith(["AAPL", "MSFT"]);
  });

  it("shows the empty state when no symbols are returned", async () => {
    apiMock.topSymbols.mockResolvedValue({ items: [] });

    renderPage(createElement(SymbolsPage));

    expect(await screen.findByText("No symbol mentions found")).toBeInTheDocument();
    // With zero symbols the quote query is disabled — never called.
    expect(apiMock.quotes).not.toHaveBeenCalled();
  });

  it("surfaces a query error instead of hanging on a skeleton", async () => {
    apiMock.topSymbols.mockRejectedValue(
      new Error("/ui/top-symbols → 500 symbols exploded"),
    );

    renderPage(createElement(SymbolsPage));

    expect(await screen.findByText(/symbols exploded/)).toBeInTheDocument();
  });

  it("does not crash when topSymbols resolves to a null payload", async () => {
    // The hero math reads `top.data?.items ?? []`; a null body must degrade
    // gracefully to the empty state, not throw on `.items`.
    apiMock.topSymbols.mockResolvedValue(null);

    renderPage(createElement(SymbolsPage));

    expect(await screen.findByText("No symbol mentions found")).toBeInTheDocument();
  });
});
