import { describe, it, expect, beforeEach, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { UIBenchmark } from "@/types/api";

/**
 * Smoke tests for the Benchmark Lab page (task #48: hero + per-axis trend
 * visual). We assert the page mounts, renders the mocked golden-set metrics
 * (F1 / asset-class / reason-code axes), and degrades to skeleton/error
 * without crashing. The heavy ECharts trend visual and KPI sparklines are
 * stubbed to null so jsdom never touches canvas/SVG layout.
 */

// ── api mock ───────────────────────────────────────────────────────────────
// Keep the real formatters (fmtPct/fmtScore) so percentage rendering matches
// production; only the network surface is replaced. `vi.hoisted` lets these
// spies exist before the hoisted vi.mock factory runs.
const { benchmarkLatest, benchmarkHistory } = vi.hoisted(() => ({
  benchmarkLatest: vi.fn(),
  benchmarkHistory: vi.fn(),
}));

vi.mock("@/lib/api", async (importActual) => {
  const actual = await importActual<typeof import("@/lib/api")>();
  return {
    ...actual,
    api: { benchmarkLatest, benchmarkHistory },
  };
});

// Trend chart + sparkline are visual-only; render nothing in tests.
vi.mock("@/charts/EChart", () => ({ EChart: () => null }));
vi.mock("@/components/Sparkline", () => ({ Sparkline: () => null }));

import { BenchmarkPage } from "@/features/benchmark/BenchmarkPage";

const LATEST: UIBenchmark = {
  relevance: { precision: 0.91, recall: 0.88, f1: 0.895 },
  asset_class_f1: { EQUITY: 0.93, CRYPTO: 0.7 },
  reason_code_f1: { EARNINGS: 0.82, MACRO: 0.61 },
  symbol_recall: 0.77,
  sentiment_accuracy: 0.84,
  n: 2,
  per_item: [
    {
      capture_id: "cap-1",
      expected_finance_relevant: true,
      predicted_finance_relevant: true,
      score: 0.92,
      expected_asset_classes: ["EQUITY"],
      predicted_asset_classes: ["EQUITY"],
      expected_reason_codes: ["EARNINGS"],
      predicted_reason_codes: ["EARNINGS"],
    },
    {
      capture_id: "cap-2",
      expected_finance_relevant: true,
      predicted_finance_relevant: false,
      score: 0.31,
      expected_asset_classes: ["CRYPTO"],
      predicted_asset_classes: [],
      expected_reason_codes: ["MACRO"],
      predicted_reason_codes: [],
    },
  ],
  ran_at: "2026-05-28T12:00:00Z",
};

function HISTORY(): { history: UIBenchmark[] } {
  return {
    history: [
      LATEST,
      {
        ...LATEST,
        relevance: { precision: 0.85, recall: 0.82, f1: 0.83 },
        ran_at: "2026-05-27T12:00:00Z",
      },
    ],
  };
}

function makeClient() {
  return new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
}

function renderPage() {
  return render(
    <MemoryRouter initialEntries={["/benchmark"]}>
      <QueryClientProvider client={makeClient()}>
        <BenchmarkPage />
      </QueryClientProvider>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  benchmarkLatest.mockResolvedValue(LATEST);
  benchmarkHistory.mockResolvedValue(HISTORY());
});

describe("BenchmarkPage", () => {
  it("renders the hero outcome headline and mocked golden-set metrics", async () => {
    renderPage();

    // Hero h1 reflects the one misclassified golden item.
    expect(
      await screen.findByText(/1 item misclassified/i),
    ).toBeInTheDocument();

    // Eyebrow + KPI axis labels prove the metric tiles mounted.
    expect(screen.getByText(/Benchmark Lab/i)).toBeInTheDocument();
    expect(screen.getByText(/asset-class F1/i)).toBeInTheDocument();
    expect(screen.getByText(/reason-code F1/i)).toBeInTheDocument();

    // Per-item table renders the mocked capture ids.
    expect(screen.getByText("cap-1")).toBeInTheDocument();
    expect(screen.getByText("cap-2")).toBeInTheDocument();
  });

  it("shows the loading skeleton before data resolves", () => {
    // Never-resolving promise keeps the query in the loading state.
    benchmarkLatest.mockReturnValue(new Promise(() => {}));
    const { container } = renderPage();
    expect(container.querySelector(".animate-shimmer")).not.toBeNull();
    expect(screen.queryByText(/misclassified/i)).toBeNull();
  });

  it("renders an all-correct headline when no items disagree", async () => {
    benchmarkLatest.mockResolvedValue({
      ...LATEST,
      per_item: [LATEST.per_item[0]],
    });
    renderPage();
    expect(
      await screen.findByText(/All golden items predicted correctly/i),
    ).toBeInTheDocument();
  });

  it("handles an empty history without crashing (first-run state)", async () => {
    benchmarkHistory.mockResolvedValue({ history: [] });
    renderPage();
    expect(
      await screen.findByText(/1 item misclassified/i),
    ).toBeInTheDocument();
  });
});
