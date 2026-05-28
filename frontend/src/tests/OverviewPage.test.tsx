import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";

import type {
  UISummary,
  UITrends,
  UIBenchmark,
  FinancialRecord,
} from "@/types/api";
import { OverviewPage } from "@/features/overview/OverviewPage";

/**
 * Smoke test for the Overview home dashboard (`OverviewPage`).
 *
 * The page fans out to four react-query reads — `api.summary`, `api.trends`,
 * `api.benchmarkLatest` and `api.quantLiveRead`. We mock `@/lib/api` so the
 * component renders deterministically off in-memory fixtures with no network,
 * and stub `@/charts/EChart` to `null` so the heavy ECharts canvas (which jsdom
 * can't paint) is out of the render path.
 *
 * Coverage: (1) renders without crashing on a fully-populated payload, the
 * DeepSeek hero + the five KPI tiles show their mocked values; (2) the page
 * renders gracefully while `summary` is still loading (skeleton, no throw);
 * (3) it renders gracefully on an *empty* dataset (zeroed totals, no recent
 * rows, empty trends) without crashing.
 */

// ── api mock ───────────────────────────────────────────────────────────────
// vi.mock is hoisted; declare the spies up front so per-test overrides are
// possible via the typed handles below.
const summaryMock = vi.fn();
const trendsMock = vi.fn();
const benchMock = vi.fn();
const liveReadMock = vi.fn();

vi.mock("@/lib/api", async (importOriginal) => {
  // Keep the real formatting/helper exports (fmtPct, fmtRel, fmtScore,
  // safeHref, scoreToneClass) so the component's value formatting is exercised
  // for real — only the network-bound `api` object is replaced.
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    api: {
      summary: () => summaryMock(),
      trends: () => trendsMock(),
      benchmarkLatest: () => benchMock(),
      quantLiveRead: () => liveReadMock(),
    },
  };
});

// ECharts is lazy + canvas-backed; render nothing in jsdom.
vi.mock("@/charts/EChart", () => ({
  EChart: () => null,
}));

// ── fixtures ─────────────────────────────────────────────────────────────────
function makeRecord(overrides: Partial<FinancialRecord> = {}): FinancialRecord {
  return {
    capture_id: "cap-overview-0001",
    doc_id: "doc-1",
    title: "Fed holds rates steady, signals patience",
    domain: "reuters.com",
    language: "en",
    url: "https://reuters.com/markets/fed",
    is_finance_relevant: true,
    finance_relevance_score: 0.91,
    asset_classes: ["rates", "equity"],
    impact_reason_codes: ["monetary_policy"],
    candidate_symbols: ["SPY"],
    candidate_entities: ["Federal Reserve"],
    impact_horizons: ["short_term"],
    sentiment_label: "neutral",
    sentiment_score: 0.0,
    evidence_sentences: ["The committee left the target range unchanged."],
    reason_text: "Rate decision.",
    component_scores: {},
    diagnostic_multimodal_enabled: false,
    diagnostic_multimodal_result: null,
    processing_mode: "production_safe",
    model_versions: {},
    published_ts: "2026-05-28T12:00:00Z",
    created_at: "2026-05-28T12:00:05Z",
    ...overrides,
  };
}

function makeSummary(overrides: Partial<UISummary> = {}): UISummary {
  return {
    mode: "production_safe",
    is_production_safe: true,
    diagnostic_allowed: false,
    use_ml_stubs: true,
    totals: { total: 1280, finance_relevant: 512 },
    diagnostic_count: 0,
    asset_class_distribution: { equity: 300, rates: 120, fx: 92 },
    reason_code_distribution: { monetary_policy: 140, earnings: 80 },
    sentiment_distribution: { positive: 200, neutral: 220, negative: 92 },
    recent_top: [makeRecord()],
    dlq: 3,
    model_versions: {},
    guards: { ok: true },
    generated_at: "2026-05-28T12:00:00Z",
    ...overrides,
  };
}

function makeTrends(overrides: Partial<UITrends> = {}): UITrends {
  return {
    buckets: ["12:00", "12:05", "12:10"],
    asset_classes: ["equity", "rates"],
    series: { equity: [1, 2, 3], rates: [0, 1, 1] },
    ...overrides,
  };
}

function makeBench(overrides: Partial<UIBenchmark> = {}): UIBenchmark {
  return {
    relevance: { precision: 0.88, recall: 0.84, f1: 0.86 },
    asset_class_f1: { equity: 0.9 },
    reason_code_f1: { earnings: 0.8 },
    symbol_recall: 0.7,
    sentiment_accuracy: 0.75,
    n: 50,
    per_item: [],
    ...overrides,
  } as UIBenchmark;
}

function makeLiveRead() {
  return {
    narrative: "Markets are **steady** ahead of the print.",
    source: "deepseek" as const,
    usd_cost: 0.0001,
    context: {},
    generated_at: "2026-05-28T12:00:00Z",
  };
}

// React-query client with retries OFF so a rejected/never-resolving query
// surfaces deterministically instead of being retried.
function renderOverview(): { container: HTMLElement } {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  const wrapper = (children: ReactNode) => (
    <QueryClientProvider client={qc}>
      <MemoryRouter>{children}</MemoryRouter>
    </QueryClientProvider>
  );
  return render(wrapper(<OverviewPage />));
}

describe("OverviewPage (smoke)", () => {
  beforeEach(() => {
    summaryMock.mockReset();
    trendsMock.mockReset();
    benchMock.mockReset();
    liveReadMock.mockReset();
    // Sensible happy-path defaults; individual tests override as needed.
    summaryMock.mockResolvedValue(makeSummary());
    trendsMock.mockResolvedValue(makeTrends());
    benchMock.mockResolvedValue(makeBench());
    liveReadMock.mockResolvedValue(makeLiveRead());
  });

  it("renders the dashboard with KPI tiles + hero from mocked data", async () => {
    renderOverview();

    // Hero leads with the DeepSeek synthesis title once liveRead resolves.
    expect(await screen.findByRole("heading", { name: /deepseek synthesis/i })).toBeInTheDocument();

    // The KPI tile row + all five default tiles render (drag-reorder landmarks).
    expect(await screen.findByTestId("overview-tile-row")).toBeInTheDocument();
    for (const id of ["total", "relevant", "dlq", "distinct", "f1"]) {
      expect(screen.getByTestId(`overview-tile-${id}`)).toBeInTheDocument();
    }

    // Mocked totals surface: 1,280 total records and the finance-relevant tile.
    expect(screen.getByText("1,280")).toBeInTheDocument();
    expect(screen.getByText("total records")).toBeInTheDocument();
    expect(screen.getByText("finance-relevant")).toBeInTheDocument();
    expect(screen.getByText("DLQ")).toBeInTheDocument();

    // Distinct asset classes tile reflects the 3-key distribution.
    expect(screen.getByText("distinct asset classes")).toBeInTheDocument();

    // Recent-relevant section renders the mocked headline as a link.
    expect(
      screen.getByRole("link", { name: /fed holds rates steady/i }),
    ).toBeInTheDocument();
  });

  it("renders the loading skeleton without crashing while summary is pending", () => {
    // A summary query that never settles keeps the page in its isLoading branch.
    summaryMock.mockReturnValue(new Promise<UISummary>(() => {}));

    const { container } = renderOverview();

    // OverviewSkeleton marks the shell aria-busy; no tile row yet, no throw.
    expect(container.querySelector('[aria-busy="true"]')).not.toBeNull();
    expect(screen.queryByTestId("overview-tile-row")).toBeNull();
  });

  it("renders gracefully on an empty dataset (zero totals, no rows, empty trends)", async () => {
    summaryMock.mockResolvedValue(
      makeSummary({
        totals: { total: 0, finance_relevant: 0 },
        asset_class_distribution: {},
        reason_code_distribution: {},
        sentiment_distribution: {},
        recent_top: [],
        dlq: 0,
      }),
    );
    trendsMock.mockResolvedValue(makeTrends({ buckets: [], asset_classes: [], series: {} }));
    benchMock.mockResolvedValue(makeBench());
    liveReadMock.mockResolvedValue(makeLiveRead());

    renderOverview();

    // Tiles still render with zeroed values; distinct-asset-class count = 0.
    expect(await screen.findByTestId("overview-tile-row")).toBeInTheDocument();
    expect(screen.getByText("total records")).toBeInTheDocument();
    // distinct asset classes → "0" with an empty distribution.
    const distinctTile = screen.getByTestId("overview-tile-distinct");
    expect(distinctTile.textContent).toContain("0");

    // Empty recent_top shows the "No records yet" empty state, not a crash.
    expect(screen.getByText(/no records yet/i)).toBeInTheDocument();
  });
});
