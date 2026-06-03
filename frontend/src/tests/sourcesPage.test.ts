import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { render, screen, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { createElement, type ReactNode } from "react";
import type {
  NewsCoverageGaps,
  NewsAwareness,
  NewsSourcesResponse,
} from "@/types/api";

// ── @/lib/api mock ──────────────────────────────────────────────────────────
// SourcesPage drives off three query functions: newsSources / newsAwareness /
// newsCoverageGaps (plus probeSource on click). We mock all of them so the
// page can render in jsdom without a live sidecar. Everything else on the
// module (fmtRel + format helpers) is preserved via importActual — the page
// imports `api` + `fmtRel`, and the helper tests below import the pure
// formatters straight from the page module (untouched by this mock).
vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    api: {
      newsSources: vi.fn(),
      newsAwareness: vi.fn(),
      newsCoverageGaps: vi.fn(),
      probeSource: vi.fn(),
    },
  };
});

import { api } from "@/lib/api";
import {
  extractDomain,
  formatSuccessRate,
  formatWindowSeconds,
  SourcesPage,
} from "@/features/sources/SourcesPage";

const apiMock = api as unknown as Record<string, ReturnType<typeof vi.fn>>;

/**
 * Pure-helper pins for the /sources page. The page component is wired
 * through react-query at runtime; these tests guard the two helpers that
 * shape the table cells — both of which have edge cases that would
 * silently produce garbage UI text on bad inputs:
 *
 *   - extractDomain(): pulls a publisher domain out of a full URL for
 *     the table's first column. Must strip `www.` and survive parse
 *     failure without throwing.
 *   - formatSuccessRate(): converts a success_rate ∈ [0, 1] into a
 *     percent string. Must reject NaN / negatives rather than emit
 *     `NaN%` into the cell.
 */

describe("extractDomain", () => {
  it("returns the host without `www.`", () => {
    expect(extractDomain("https://www.bbc.co.uk/news/rss")).toBe("bbc.co.uk");
    expect(extractDomain("https://feeds.bbci.co.uk/news/rss.xml")).toBe("feeds.bbci.co.uk");
  });

  it("preserves subdomains other than `www.`", () => {
    expect(extractDomain("https://feeds.feedburner.com/reuters/businessNews")).toBe(
      "feeds.feedburner.com",
    );
  });

  it("normalizes the host to lowercase", () => {
    expect(extractDomain("HTTPS://FEEDS.BBCI.CO.UK/news")).toBe("feeds.bbci.co.uk");
  });

  it("falls back to the raw string when the URL is unparseable", () => {
    expect(extractDomain("not a url")).toBe("not a url");
  });

  it("renders an em-dash placeholder for nullish inputs", () => {
    expect(extractDomain(null)).toBe("—");
    expect(extractDomain(undefined)).toBe("—");
    expect(extractDomain("")).toBe("—");
  });
});

describe("formatSuccessRate", () => {
  it("formats whole percents without a trailing decimal", () => {
    expect(formatSuccessRate(1)).toBe("100%");
    expect(formatSuccessRate(0)).toBe("0%");
    expect(formatSuccessRate(0.5)).toBe("50%");
  });

  it("formats fractional percents with at most one decimal", () => {
    expect(formatSuccessRate(0.8333)).toBe("83.3%");
    expect(formatSuccessRate(0.9876)).toBe("98.8%");
  });

  it("rejects NaN / negatives / Infinity rather than emit NaN%", () => {
    expect(formatSuccessRate(Number.NaN)).toBe("—");
    expect(formatSuccessRate(-0.5)).toBe("—");
    expect(formatSuccessRate(Number.POSITIVE_INFINITY)).toBe("—");
  });
});

describe("formatWindowSeconds", () => {
  it("formats sub-minute durations in seconds", () => {
    expect(formatWindowSeconds(0)).toBe("0s");
    expect(formatWindowSeconds(45)).toBe("45s");
    // Rounds to the nearest whole second.
    expect(formatWindowSeconds(45.4)).toBe("45s");
  });

  it("formats minute+second durations as 'Xm Ys' (drops 0s remainder)", () => {
    expect(formatWindowSeconds(100)).toBe("1m 40s");
    expect(formatWindowSeconds(120)).toBe("2m");
    // effective window ~ poll_interval(10) + median_lag(90)
    expect(formatWindowSeconds(100)).toBe("1m 40s");
  });

  it("formats hour-scale durations as 'Xh Ym' (drops 0m remainder)", () => {
    expect(formatWindowSeconds(3600)).toBe("1h");
    expect(formatWindowSeconds(3900)).toBe("1h 5m");
  });

  it("renders an em-dash for null / negative / non-finite inputs", () => {
    expect(formatWindowSeconds(null)).toBe("—");
    expect(formatWindowSeconds(undefined)).toBe("—");
    expect(formatWindowSeconds(-5)).toBe("—");
    expect(formatWindowSeconds(Number.NaN)).toBe("—");
    expect(formatWindowSeconds(Number.POSITIVE_INFINITY)).toBe("—");
  });
});

// ── Blind-spots panel (render) ───────────────────────────────────────────────
// The panel consumes api.newsCoverageGaps() — gaps (watched terms with NO
// recent coverage, shown as warning chips) + covered (term → freshest age).
// These mounted tests pin the three render branches: gaps+covered, the
// all-covered empty state, and the no-watched-terms empty state. The sibling
// awareness + sources queries are stubbed so the page can mount cleanly.

function awarenessDisabled(): NewsAwareness {
  return {
    schema_version: 1,
    generated_at: "2026-05-29T00:00:00Z",
    configured: false,
    sources_total: 0,
    sources_by_parser: {},
    poll_interval_seconds: null,
    median_publisher_lag_seconds: null,
    avg_publisher_lag_seconds: null,
    last_run_at: null,
    last_new_at: null,
    total_ingested: 0,
    window_estimate_seconds: null,
  };
}

function sourcesEmpty(): NewsSourcesResponse {
  return {
    schema_version: 1,
    generated_at: "2026-05-29T00:00:00Z",
    configured: true,
    total: 0,
    healthy_count: 0,
    degraded_count: 0,
    sources: [],
  };
}

function coverage(overrides: Partial<NewsCoverageGaps> = {}): NewsCoverageGaps {
  return {
    schema_version: 1,
    generated_at: "2026-05-29T00:00:00Z",
    window_seconds: 3600,
    covered: [
      { term: "inflation", last_seen_age_seconds: 120, mention_count: 8 },
      { term: "fed", last_seen_age_seconds: 45, mention_count: 3 },
    ],
    gaps: ["bitcoin", "earnings"],
    ...overrides,
  };
}

function renderSources() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    createElement(
      QueryClientProvider,
      { client: qc },
      createElement(
        MemoryRouter,
        { initialEntries: ["/sources"] },
        createElement(SourcesPage),
      ),
    ) as ReactNode,
  );
}

describe("SourcesPage blind-spots panel", () => {
  beforeEach(() => {
    Object.values(apiMock).forEach((fn) => fn.mockReset());
    apiMock.newsSources.mockResolvedValue(sourcesEmpty());
    apiMock.newsAwareness.mockResolvedValue(awarenessDisabled());
    apiMock.newsCoverageGaps.mockResolvedValue(coverage());
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("renders gap chips + covered rows when blind spots exist", async () => {
    renderSources();

    const panel = await screen.findByTestId("blind-spots");
    // Gaps surface as warning chips, freshest-first covered rows below.
    expect(within(panel).getByTestId("blind-spot-gap-bitcoin")).toHaveTextContent(
      "bitcoin",
    );
    expect(within(panel).getByTestId("blind-spot-gap-earnings")).toHaveTextContent(
      "earnings",
    );
    // Covered list carries the freshest-mention age + count per term.
    const fed = within(panel).getByTestId("blind-spot-covered-fed");
    expect(fed).toHaveTextContent("fed");
    expect(fed).toHaveTextContent("45s ago");
    expect(fed).toHaveTextContent("×3");
    const infl = within(panel).getByTestId("blind-spot-covered-inflation");
    expect(infl).toHaveTextContent("2m ago");
    // The gaps + covered sub-sections are both present.
    expect(within(panel).getByTestId("blind-spots-gaps")).toBeInTheDocument();
    expect(within(panel).getByTestId("blind-spots-covered")).toBeInTheDocument();
  });

  it("renders the all-covered empty state when there are zero gaps", async () => {
    apiMock.newsCoverageGaps.mockResolvedValue(coverage({ gaps: [] }));
    renderSources();

    const panel = await screen.findByTestId("blind-spots");
    // Gaps block is gone; the benign "no blind spots" state shows instead.
    expect(within(panel).getByTestId("blind-spots-all-covered")).toBeInTheDocument();
    expect(within(panel).getByText("no blind spots")).toBeInTheDocument();
    expect(within(panel).queryByTestId("blind-spots-gaps")).not.toBeInTheDocument();
    // Covered rows still render — coverage is complete, not absent.
    expect(within(panel).getByTestId("blind-spot-covered-fed")).toBeInTheDocument();
  });

  it("renders the no-watched-terms empty state when gaps + covered are both empty", async () => {
    apiMock.newsCoverageGaps.mockResolvedValue(coverage({ gaps: [], covered: [] }));
    renderSources();

    const panel = await screen.findByTestId("blind-spots");
    expect(within(panel).getByText("no watched terms")).toBeInTheDocument();
    expect(within(panel).queryByTestId("blind-spots-covered")).not.toBeInTheDocument();
    expect(within(panel).queryByTestId("blind-spots-all-covered")).not.toBeInTheDocument();
  });
});

describe("SourcesPage focus styles", () => {
  beforeEach(() => {
    Object.values(apiMock).forEach((fn) => fn.mockReset());
    apiMock.newsSources.mockResolvedValue({
      schema_version: 1,
      generated_at: "2026-05-29T00:00:00Z",
      configured: true,
      total: 1,
      healthy_count: 0,
      degraded_count: 1,
      sources: [
        {
          name: "Test RSS Feed",
          url: "https://example.com/rss",
          last_status: "error",
          last_status_at: "2026-05-29T00:00:00Z",
          polls: 5,
          failures: 2,
          success_rate: 0.6,
          items_total: 10,
          consecutive_errors: 2,
          last_error: "Connection timeout",
          last_status_code: 504,
          cooldown_until: null,
          adaptive_cadence: 1,
          consecutive_empty: 0,
          total_new_items: 2,
        },
      ],
    });
    apiMock.newsAwareness.mockResolvedValue(awarenessDisabled());
    apiMock.newsCoverageGaps.mockResolvedValue(coverage({ gaps: [], covered: [] }));
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("implements custom focus-visible ring styles on all interactive controls for keyboard navigation", async () => {
    renderSources();

    // 1. View Live Feed link
    const feedLink = await screen.findByTestId("sources-back-to-feed");
    expect(feedLink).toHaveClass("focus:outline-none");
    expect(feedLink).toHaveClass("focus-visible:ring-1");
    expect(feedLink).toHaveClass("focus-visible:ring-accent");

    // 2. Refresh button
    const refreshBtn = screen.getByTestId("sources-refresh");
    expect(refreshBtn).toHaveClass("focus:outline-none");
    expect(refreshBtn).toHaveClass("focus-visible:ring-1");
    expect(refreshBtn).toHaveClass("focus-visible:ring-accent");

    // 3. Error toggle button (on degraded source)
    const errorToggle = await screen.findByTestId("sources-error-toggle-Test RSS Feed");
    expect(errorToggle).toHaveClass("focus:outline-none");
    expect(errorToggle).toHaveClass("focus-visible:ring-1");
    expect(errorToggle).toHaveClass("focus-visible:ring-accent");

    // 4. Probe button
    const probeBtn = screen.getByTestId("sources-probe-Test RSS Feed");
    expect(probeBtn).toHaveClass("focus:outline-none");
    expect(probeBtn).toHaveClass("focus-visible:ring-1");
    expect(probeBtn).toHaveClass("focus-visible:ring-accent");
  });
});
