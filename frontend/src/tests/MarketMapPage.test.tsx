import { describe, it, expect, beforeEach, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import type { ReactNode } from "react";
import { MarketMapPage } from "@/features/market-map/MarketMapPage";
import type { UIMatrix, UITrends } from "@/types/api";
import type { RegimeReportDTO, LiveReadResponse } from "@/lib/api";

// ── heavy children ──────────────────────────────────────────────────────────
// The matrix heatmap + trends bar both render via the lazy ECharts wrapper
// (pulls ~1MB echarts core). Mock it to null so the smoke test exercises the
// page's data-binding + empty/loading branches, not the chart engine.
vi.mock("@/charts/EChart", () => ({ EChart: () => null }));

// ── api boundary ──────────────────────────────────────────────────────────
// MarketMapPage hits api.matrix / trends / quantLiveRead / quantRegime on
// mount. Mock the whole `api` object; keep the real pure helpers (fmtRel is
// called for the regime "latest shift" tile) via importActual spread.
vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    api: {
      matrix: vi.fn(),
      trends: vi.fn(),
      quantLiveRead: vi.fn(),
      quantRegime: vi.fn(),
    },
  };
});

import { api } from "@/lib/api";

const apiMock = api as unknown as Record<string, ReturnType<typeof vi.fn>>;

function makeMatrix(over: Partial<UIMatrix> = {}): UIMatrix {
  return {
    asset_classes: ["EQUITY", "CRYPTO"],
    reason_codes: ["earnings", "macro"],
    matrix: [
      [3, 1],
      [0, 2],
    ],
    ...over,
  };
}

function makeTrends(over: Partial<UITrends> = {}): UITrends {
  return {
    buckets: ["09:00", "10:00"],
    asset_classes: ["EQUITY", "CRYPTO"],
    series: { EQUITY: [2, 4], CRYPTO: [1, 3] },
    ...over,
  };
}

function makeLiveRead(over: Partial<LiveReadResponse> = {}): LiveReadResponse {
  return {
    narrative: "**Risk-on** tape: equities lead, crypto follows.",
    source: "deepseek",
    context: {},
    generated_at: "2026-05-28T12:00:00Z",
    ...over,
  };
}

function makeRegime(over: Partial<RegimeReportDTO> = {}): RegimeReportDTO {
  return {
    bucket_minutes: 30,
    shift_threshold: 0.35,
    buckets: [],
    detected_shifts: ["2026-05-28T11:30:00Z"],
    ...over,
  };
}

function renderPage(initialEntries = ["/map"]): ReturnType<typeof render> {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <MemoryRouter initialEntries={initialEntries}>
      <QueryClientProvider client={qc}>
        <MarketMapPage />
      </QueryClientProvider>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe("MarketMapPage smoke", () => {
  it("renders the narrative hero + section landmarks from mocked data", async () => {
    apiMock.matrix.mockResolvedValue(makeMatrix());
    apiMock.trends.mockResolvedValue(makeTrends());
    apiMock.quantLiveRead.mockResolvedValue(makeLiveRead());
    apiMock.quantRegime.mockResolvedValue(makeRegime());

    renderPage();

    // source:"deepseek" → DeepSeek synthesis headline.
    expect(
      await screen.findByRole("heading", { name: /deepseek synthesis/i }),
    ).toBeInTheDocument();

    // renderMd strips the ** and emits a <strong> for the bold accent.
    expect(screen.getByText("Risk-on")).toBeInTheDocument();

    // Static section headings always present.
    expect(
      screen.getByText(/news-impact map · asset class × reason code/i),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/news record trend by asset class/i),
    ).toBeInTheDocument();
  });

  it("renders the regime stat tiles once regime data resolves", async () => {
    apiMock.matrix.mockResolvedValue(makeMatrix());
    apiMock.trends.mockResolvedValue(makeTrends());
    apiMock.quantLiveRead.mockResolvedValue(makeLiveRead());
    apiMock.quantRegime.mockResolvedValue(makeRegime());

    renderPage();

    expect(await screen.findByText(/regime shifts/i)).toBeInTheDocument();
    // detected_shifts.length === 1.
    await waitFor(() => expect(screen.getByText("1")).toBeInTheDocument());
    // bucket gating hint folds in the formatted shift_threshold.
    expect(screen.getByText(/threshold KL 0\.35/i)).toBeInTheDocument();
  });

  it("shows the local-synthesis headline when source is not deepseek", async () => {
    apiMock.matrix.mockResolvedValue(makeMatrix());
    apiMock.trends.mockResolvedValue(makeTrends());
    apiMock.quantLiveRead.mockResolvedValue(
      makeLiveRead({ source: "local", narrative: "" }),
    );
    apiMock.quantRegime.mockResolvedValue(makeRegime());

    renderPage();

    expect(
      await screen.findByRole("heading", { name: /local synthesis/i }),
    ).toBeInTheDocument();
  });

  it("renders empty states gracefully when matrix + trends are empty", async () => {
    apiMock.matrix.mockResolvedValue(
      makeMatrix({ asset_classes: [], reason_codes: [], matrix: [] }),
    );
    apiMock.trends.mockResolvedValue(
      makeTrends({ buckets: [], asset_classes: [], series: {} }),
    );
    apiMock.quantLiveRead.mockResolvedValue(makeLiveRead());
    apiMock.quantRegime.mockResolvedValue(makeRegime());

    renderPage();

    expect(await screen.findByText(/no matrix yet/i)).toBeInTheDocument();
    expect(screen.getByText(/no timeline data/i)).toBeInTheDocument();
  });

  it("does not crash while all queries are pending (loading skeletons)", () => {
    apiMock.matrix.mockReturnValue(new Promise<UIMatrix>(() => {}));
    apiMock.trends.mockReturnValue(new Promise<UITrends>(() => {}));
    apiMock.quantLiveRead.mockReturnValue(new Promise<LiveReadResponse>(() => {}));
    apiMock.quantRegime.mockReturnValue(new Promise<RegimeReportDTO>(() => {}));

    expect(() => renderPage()).not.toThrow();
    // Hero eyebrow renders regardless of query state.
    expect(screen.getByText(/analysis map · cross-asset news flow/i)).toBeInTheDocument();
  });
});
