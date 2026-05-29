import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { render, screen, waitFor, within, fireEvent } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { createElement, type ReactNode } from "react";
import type { PortfolioEnriched, PortfolioEnrichedHolding } from "@/types/api";

// ── Bug hunt I1-fe-portfolio ─────────────────────────────────────────────────
// Three confirmed findings:
//  1. PortfolioPage quote % rendered 100x too small (double percent-division).
//  2. PortfolioHolding.id typed `string` but the backend wires it as a number.
//  3. fmtRel renders >14d-future timestamps as a misleading absolute past date.
//
// We mock only the four `api` members the page consumes; fmtPct/fmtRel/safeHref
// are preserved via importActual so the page's real formatting is exercised.
vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    api: {
      portfolioEnriched: vi.fn(),
      portfolioAdd: vi.fn(),
      portfolioDelete: vi.fn(),
    },
  };
});

import { api, fmtRel } from "@/lib/api";
import { PortfolioPage } from "@/features/portfolio/PortfolioPage";

const apiMock = api as unknown as Record<string, ReturnType<typeof vi.fn>>;

function renderPortfolio() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    createElement(
      QueryClientProvider,
      { client: qc },
      createElement(
        MemoryRouter,
        { initialEntries: ["/portfolio"] },
        createElement(PortfolioPage),
      ),
    ) as ReactNode,
  );
}

// Fixture mirrors the real wire shape: `id` is a NUMBER (finding #2) and
// `change_pct` is a FRACTION, not a percent (finding #1 — backend emits
// change_abs / prev_close, e.g. 0.0083 for a +0.83% move).
function holding(overrides: Partial<PortfolioEnrichedHolding> = {}): PortfolioEnrichedHolding {
  return {
    id: 1,
    symbol: "AAPL",
    label: "Core tech",
    shares: 100,
    weight: null,
    cost_basis: 150,
    notes: null,
    added_at: "2026-05-28T00:00:00Z",
    quote: { last: 302.5, prev_close: 300, change_pct: 0.0083 },
    coverage: { covered: true, last_seen_age_seconds: 120, mention_count: 4 },
    recent_news_count: 4,
    recent_top: [
      { title: "Apple unveils new chip", url: "https://example.com/aapl", score: 0.78 },
    ],
    sentiment_label: "positive",
    ...overrides,
  };
}

function enriched(holdings: PortfolioEnrichedHolding[]): PortfolioEnriched {
  return { schema_version: 1, generated_at: "2026-05-28T00:00:00Z", holdings };
}

beforeEach(() => {
  Object.values(apiMock).forEach((fn) => fn.mockReset());
  apiMock.portfolioEnriched.mockResolvedValue(enriched([holding()]));
  apiMock.portfolioDelete.mockResolvedValue({ ok: true });
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("I1-fe-portfolio · finding #1 — quote % is a fraction, not pre-multiplied", () => {
  it("renders a +0.83% move from a 0.0083 fraction (not the 100x-too-small +0.01%)", async () => {
    renderPortfolio();

    const change = await screen.findByTestId("quote-change");
    // Before the fix the page did fmtPct(0.0083 / 100, 2) = "0.01%".
    expect(change).toHaveTextContent("+0.83%");
    expect(change).not.toHaveTextContent("+0.01%");
    expect(change.className).toContain("text-good");
  });

  it("renders a negative fraction with the correct magnitude + sign", async () => {
    apiMock.portfolioEnriched.mockResolvedValue(
      enriched([holding({ quote: { last: 297, prev_close: 300, change_pct: -0.01 } })]),
    );
    renderPortfolio();

    const change = await screen.findByTestId("quote-change");
    expect(change).toHaveTextContent("-1.00%");
    expect(change.className).toContain("text-bad");
  });
});

describe("I1-fe-portfolio · finding #2 — holding id is a number on the wire", () => {
  it("forwards the numeric id straight through to portfolioDelete", async () => {
    apiMock.portfolioEnriched.mockResolvedValue(enriched([holding({ id: 42 })]));
    renderPortfolio();
    await screen.findByText("AAPL");

    const row = screen.getByText("AAPL").closest("tr") as HTMLElement;
    fireEvent.click(within(row).getByRole("button", { name: /remove aapl/i }));

    // Strict number comparison — would fail if the id were coerced to a string.
    await waitFor(() => expect(apiMock.portfolioDelete).toHaveBeenCalledWith(42));
    const arg = apiMock.portfolioDelete.mock.calls[0][0];
    expect(typeof arg).toBe("number");
  });
});

describe("I1-fe-portfolio · finding #3 — fmtRel keeps far-future timestamps relative", () => {
  const NOW = Date.parse("2026-05-29T00:00:00Z");
  const day = 86_400_000;

  it("renders a 20-day-future timestamp as 'in 20d', not an absolute date", () => {
    const future = new Date(NOW + 20 * day).toISOString();
    const out = fmtRel(future, NOW);
    expect(out).toBe("in 20d");
    // Must NOT fall through to the YYYY-MM-DD absolute-date branch.
    expect(out).not.toMatch(/^\d{4}-\d{2}-\d{2}$/);
  });

  it("still shows an absolute date for >14d-PAST items", () => {
    const old = new Date(NOW - 92 * day).toISOString();
    expect(fmtRel(old, NOW)).toMatch(/^\d{4}-\d{2}-\d{2}$/);
  });

  it("keeps the existing <14d future behavior ('in 13d')", () => {
    const soon = new Date(NOW + 13 * day).toISOString();
    expect(fmtRel(soon, NOW)).toBe("in 13d");
  });
});
