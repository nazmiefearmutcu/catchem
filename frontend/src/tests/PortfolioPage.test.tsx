import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { createElement, type ReactNode } from "react";
import type { PortfolioEnriched, PortfolioEnrichedHolding } from "@/types/api";

// ── @/lib/api mock ──────────────────────────────────────────────────────────
// PortfolioPage drives off portfolioEnriched (polled) plus the add/remove
// mutations. We mock those four; everything else on the module — fmtPct,
// fmtRel, safeHref, ApiError — is preserved via importActual so the page's
// formatting + safe-link helpers behave exactly as in production.
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

import { api } from "@/lib/api";

const apiMock = api as unknown as Record<string, ReturnType<typeof vi.fn>>;

// Import after the mock is registered so the page binds to the mocked `api`.
import { PortfolioPage } from "@/features/portfolio/PortfolioPage";

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
  return {
    schema_version: 1,
    generated_at: "2026-05-28T00:00:00Z",
    holdings,
  };
}

beforeEach(() => {
  Object.values(apiMock).forEach((fn) => fn.mockReset());
  apiMock.portfolioEnriched.mockResolvedValue(enriched([holding()]));
  apiMock.portfolioAdd.mockResolvedValue({
    id: 2,
    symbol: "MSFT",
    added_at: "2026-05-28T00:00:00Z",
  });
  apiMock.portfolioDelete.mockResolvedValue({ ok: true });
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("PortfolioPage", () => {
  it("renders a holding row with its quote and recent-news count", async () => {
    renderPortfolio();

    // Symbol + label render.
    expect(await screen.findByText("AAPL")).toBeInTheDocument();
    expect(screen.getByText("Core tech")).toBeInTheDocument();
    // Quote last price + colored change_pct (0.83% → +0.83%).
    expect(screen.getByText("302.5")).toBeInTheDocument();
    const change = screen.getByTestId("quote-change");
    expect(change).toHaveTextContent("+0.83%");
    expect(change.className).toContain("text-good");
    // Sentiment chip + top headline link.
    expect(screen.getByText("positive")).toBeInTheDocument();
    const link = screen.getByRole("link", { name: /Apple unveils new chip/i });
    expect(link).toHaveAttribute("href", "https://example.com/aapl");
  });

  it("shows the hero coverage synthesis (covered, no blind spots)", async () => {
    renderPortfolio();

    // Wait for the enriched query to resolve — the hero testid is always
    // mounted (so loading→loaded doesn't flicker), so we anchor on the
    // resolved headline text instead of the element's existence.
    await waitFor(() =>
      expect(screen.getByTestId("portfolio-hero-headline")).toHaveTextContent(
        "All 1 holding covered",
      ),
    );
    // No blind-spot badge when every holding is covered.
    expect(screen.queryByText("blind spot")).not.toBeInTheDocument();
  });

  it("surfaces a blind-spot badge when coverage.covered is false", async () => {
    apiMock.portfolioEnriched.mockResolvedValue(
      enriched([
        holding({
          coverage: { covered: false, last_seen_age_seconds: null, mention_count: 0 },
          recent_news_count: 0,
          recent_top: [],
          sentiment_label: null,
        }),
      ]),
    );

    renderPortfolio();

    expect(await screen.findByText("blind spot")).toBeInTheDocument();
    // Hero flips to the warning headline.
    expect(screen.getByTestId("portfolio-hero-headline")).toHaveTextContent(
      "1 of 1 holding in a blind spot",
    );
  });

  it("renders the 'no quote' state when a holding has no market data", async () => {
    apiMock.portfolioEnriched.mockResolvedValue(
      enriched([holding({ symbol: "ZZZZ", quote: null })]),
    );

    renderPortfolio();

    expect(await screen.findByText("ZZZZ")).toBeInTheDocument();
    expect(screen.getByText("no quote")).toBeInTheDocument();
  });

  it("shows the empty state when there are no holdings", async () => {
    apiMock.portfolioEnriched.mockResolvedValue(enriched([]));

    renderPortfolio();

    expect(await screen.findByText("No holdings tracked yet")).toBeInTheDocument();
    // No table rendered.
    expect(screen.queryByText("AAPL")).not.toBeInTheDocument();
  });

  it("calls portfolioAdd with the typed symbol + shares on submit", async () => {
    renderPortfolio();
    await screen.findByText("AAPL");

    fireEvent.change(screen.getByLabelText("symbol"), { target: { value: "msft" } });
    fireEvent.change(screen.getByLabelText("shares (optional)"), {
      target: { value: "50" },
    });
    fireEvent.click(screen.getByRole("button", { name: /add holding/i }));

    // useMutation's mutate() forwards a second context arg to mutationFn, so
    // we assert on the first positional argument (the request body) only.
    await waitFor(() => expect(apiMock.portfolioAdd).toHaveBeenCalled());
    expect(apiMock.portfolioAdd.mock.calls[0][0]).toMatchObject({
      symbol: "MSFT",
      shares: 50,
    });
  });

  it("calls portfolioDelete with the holding id when × is clicked", async () => {
    renderPortfolio();
    await screen.findByText("AAPL");

    const row = screen.getByText("AAPL").closest("tr") as HTMLElement;
    fireEvent.click(within(row).getByRole("button", { name: /remove aapl/i }));

    await waitFor(() => expect(apiMock.portfolioDelete).toHaveBeenCalledWith(1));
  });
});
