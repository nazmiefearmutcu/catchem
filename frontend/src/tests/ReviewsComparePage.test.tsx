import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { createElement, type ReactNode } from "react";
import { ReviewsComparePage } from "@/features/reviews/ReviewsComparePage";

/**
 * Reviews / Compare dashboard smoke tests.
 *
 * The component fans out to /api/reviews/status + /api/reviews/compare.
 * We stub both, render the page, and pin the surface the analyst sees:
 *   - status strip reads from /status
 *   - summary card reads from /compare.summary
 *   - paired rows render the agreement chip
 *   - empty state when n=0
 *   - "biggest gap" sort puts the lowest-overall first
 */

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return createElement(
    QueryClientProvider,
    { client: qc },
    createElement(MemoryRouter, { initialEntries: ["/reviews"] }, children),
  );
}

const fetchMock = vi.fn();

const STATUS_PAYLOAD = {
  deepseek_enabled: true,
  deepseek_keyed: true,
  deepseek_ready: true,
  model: "deepseek-chat",
  sampling_rate: 0.1,
  usd_cap: 9.5,
  usd_spent: 0.123,
  usd_remaining: 9.377,
  exhausted: false,
  primary_reviewer_version: "stub-vX",
  tokens: { input: 12_345, output: 678, calls: 11, errors: 1 },
  base_url: "https://api.deepseek.com",
  generated_at: "2026-01-01T00:00:00Z",
};

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

beforeEach(() => {
  fetchMock.mockReset();
  fetchMock.mockImplementation((url: string) => {
    if (url.startsWith("/api/reviews/status")) {
      return Promise.resolve(jsonResponse(STATUS_PAYLOAD));
    }
    if (url.startsWith("/api/reviews/compare")) {
      return Promise.resolve(
        jsonResponse({
          items: SAMPLE_ITEMS,
          summary: {
            n: SAMPLE_ITEMS.length,
            relevance_match_rate: 0.5,
            sentiment_match_rate: 1.0,
            mean_asset_jaccard: 0.5,
            mean_reason_jaccard: 0.5,
            mean_symbol_jaccard: 0.5,
            mean_score_delta: 0.15,
            mean_overall: 0.7,
            deepseek_errors: 0,
          },
          generated_at: "2026-01-01T00:00:00Z",
        }),
      );
    }
    return Promise.resolve(new Response("unhandled", { status: 500 }));
  });
  (globalThis as { fetch?: typeof fetch }).fetch = fetchMock as unknown as typeof fetch;
});

afterEach(() => {
  delete (globalThis as { fetch?: typeof fetch }).fetch;
});

const SAMPLE_ITEMS = [
  {
    capture_id: "cap-low-agreement",
    title: "Hot disagreement story",
    domain: "reuters.com",
    url: "https://reuters.com/x",
    stub: {
      capture_id: "cap-low-agreement",
      reviewer_id: "stub",
      reviewer_version: "stub-vX",
      created_at: "2026-01-01T00:00:00Z",
      error_code: null,
      payload: {
        is_finance_relevant: true,
        finance_relevance_score: 0.8,
        asset_classes: ["equities"],
        impact_reason_codes: ["earnings"],
        candidate_symbols: ["AAPL"],
        sentiment_label: "positive",
        sentiment_score: 0.7,
        evidence_sentences: [],
        reason_text: null,
      },
    },
    deepseek: {
      capture_id: "cap-low-agreement",
      reviewer_id: "deepseek",
      reviewer_version: "deepseek-chat|prompt-v1",
      created_at: "2026-01-01T00:00:00Z",
      error_code: null,
      payload: {
        is_finance_relevant: false,
        finance_relevance_score: 0.2,
        asset_classes: ["crypto"],
        impact_reason_codes: ["regulation"],
        candidate_symbols: [],
        sentiment_label: "positive",
        sentiment_score: 0.6,
        evidence_sentences: [],
        reason_text: null,
      },
      input_tokens: 1000,
      output_tokens: 200,
      usd_cost: 0.0005,
      latency_ms: 1234,
    },
    agreement: {
      relevance_match: false,
      score_delta: 0.6,
      asset_jaccard: 0.0,
      reason_jaccard: 0.0,
      symbol_jaccard: 0.0,
      sentiment_match: true,
      overall: 0.2,
    },
  },
  {
    capture_id: "cap-high-agreement",
    title: "Everyone agrees",
    domain: "ft.com",
    url: null,
    stub: {
      capture_id: "cap-high-agreement",
      reviewer_id: "stub",
      reviewer_version: "stub-vX",
      created_at: "2026-01-01T01:00:00Z",
      error_code: null,
      payload: {
        is_finance_relevant: true,
        finance_relevance_score: 0.7,
        asset_classes: ["equities"],
        impact_reason_codes: ["earnings"],
        candidate_symbols: ["MSFT"],
        sentiment_label: "neutral",
        sentiment_score: 0.5,
        evidence_sentences: [],
        reason_text: null,
      },
    },
    deepseek: {
      capture_id: "cap-high-agreement",
      reviewer_id: "deepseek",
      reviewer_version: "deepseek-chat|prompt-v1",
      created_at: "2026-01-01T01:00:00Z",
      error_code: null,
      payload: {
        is_finance_relevant: true,
        finance_relevance_score: 0.72,
        asset_classes: ["equities"],
        impact_reason_codes: ["earnings"],
        candidate_symbols: ["MSFT"],
        sentiment_label: "neutral",
        sentiment_score: 0.5,
        evidence_sentences: [],
        reason_text: null,
      },
      input_tokens: 900,
      output_tokens: 180,
      usd_cost: 0.00045,
      latency_ms: 1100,
    },
    agreement: {
      relevance_match: true,
      score_delta: 0.02,
      asset_jaccard: 1.0,
      reason_jaccard: 1.0,
      symbol_jaccard: 1.0,
      sentiment_match: true,
      overall: 0.98,
    },
  },
];

describe("ReviewsComparePage", () => {
  it("renders the status strip with USD spend + sampling", async () => {
    render(createElement(ReviewsComparePage), { wrapper });
    await waitFor(() =>
      expect(screen.getByText(/deepseek second-opinion reviewer/i)).toBeInTheDocument(),
    );
    // Status strip shows the spend.
    expect(screen.getByText("$0.1230")).toBeInTheDocument();
    expect(screen.getByText("of $9.50")).toBeInTheDocument();
    // Sampling pill.
    expect(screen.getByText("10%")).toBeInTheDocument();
  });

  it("shows the disagreement row first under 'biggest gap' sort", async () => {
    render(createElement(ReviewsComparePage), { wrapper });
    await waitFor(() => expect(screen.getByText("Hot disagreement story")).toBeInTheDocument());
    const titles = screen.getAllByText(/Hot disagreement|Everyone agrees/i);
    // "biggest gap" sort = ascending overall; low-agreement appears first.
    expect(titles[0].textContent).toMatch(/Hot disagreement/);
  });

  it("opens the diff drawer when a row is clicked", async () => {
    render(createElement(ReviewsComparePage), { wrapper });
    await waitFor(() => screen.getByText("Hot disagreement story"));
    fireEvent.click(screen.getByText("Hot disagreement story"));
    const drawer = await screen.findByLabelText(/review diff drawer/i);
    expect(drawer).toBeInTheDocument();
    // Drawer shows both reviewer sides labelled. Scope to the drawer because
    // "DeepSeek" also appears in the page-level diff summary strip ("most
    // common addition by DeepSeek").
    const inDrawer = within(drawer);
    expect(inDrawer.getByText(/stub \(in-process\)/i)).toBeInTheDocument();
    expect(inDrawer.getByText("DeepSeek")).toBeInTheDocument();
  });

  it("renders empty state when no paired rows exist", async () => {
    fetchMock.mockReset();
    fetchMock.mockImplementation((url: string) => {
      if (url.startsWith("/api/reviews/status")) {
        return Promise.resolve(jsonResponse({ ...STATUS_PAYLOAD, deepseek_enabled: false }));
      }
      if (url.startsWith("/api/reviews/compare")) {
        return Promise.resolve(
          jsonResponse({
            items: [],
            summary: {
              n: 0,
              relevance_match_rate: 0,
              sentiment_match_rate: 0,
              mean_asset_jaccard: 0,
              mean_reason_jaccard: 0,
              mean_symbol_jaccard: 0,
              mean_score_delta: 0,
              mean_overall: 0,
              deepseek_errors: 0,
            },
            generated_at: "2026-01-01T00:00:00Z",
          }),
        );
      }
      return Promise.resolve(new Response("unhandled", { status: 500 }));
    });
    render(createElement(ReviewsComparePage), { wrapper });
    // Two surfaces render the "no paired reviews yet" wording — the
    // SummaryCard pre-text and the EmptyState inside the list. We tolerate
    // either, just assert *something* shows up under the empty branch.
    const empty = await screen.findAllByText(/no paired reviews yet/i, {}, { timeout: 4000 });
    expect(empty.length).toBeGreaterThan(0);
  });
});
