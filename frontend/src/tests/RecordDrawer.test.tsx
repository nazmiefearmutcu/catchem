import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { createElement, type ReactNode } from "react";
import { RecordDrawer } from "@/features/record-detail/RecordDrawer";
import { __resetOverlayStateForTests } from "@/context/overlayCoordinator";

// Mock the API client. Spread actual so utilities remain intact.
vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    api: {
      record: vi.fn(),
      getTags: vi.fn(),
      addTag: vi.fn(),
      removeTag: vi.fn(),
    },
  };
});

import { api } from "@/lib/api";

const apiMock = api as unknown as Record<string, ReturnType<typeof vi.fn>>;

function renderDrawer(ui: ReactNode) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    createElement(
      QueryClientProvider,
      { client: qc },
      createElement(MemoryRouter, null, ui)
    )
  );
}

const mockRecord = {
  capture_id: "cap-1",
  title: "Test Article Title",
  url: "https://example.com/test",
  published_ts: "2026-06-03T12:00:00Z",
  created_at: "2026-06-03T12:00:00Z",
  domain: "example.com",
  language: "en",
  finance_relevance_score: 0.85,
  is_finance_relevant: true,
  sentiment_label: "positive",
  sentiment_score: 0.9,
  processing_mode: "auto",
  asset_classes: ["Equity"],
  impact_reason_codes: ["Earnings"],
  impact_horizons: ["Short"],
  candidate_symbols: ["AAPL"],
  candidate_entities: ["Apple Inc."],
  evidence_sentences: ["This is a test sentence."],
  reason_text: "Reason text here",
  component_scores: { "score_1": 0.8 },
  model_versions: { "model_v1": "1.0.0" },
  diagnostic_multimodal_enabled: false,
  diagnostic_multimodal_result: null,
};

beforeEach(() => {
  __resetOverlayStateForTests();
  Object.values(apiMock).forEach((fn) => fn.mockReset());
  apiMock.record.mockResolvedValue(mockRecord);
  apiMock.getTags.mockResolvedValue({ capture_id: "cap-1", tags: ["earnings", "tech"] });
});

afterEach(() => {
  vi.restoreAllMocks();
  __resetOverlayStateForTests();
});

describe("<RecordDrawer>", () => {
  it("implements custom focus-visible ring styles on all interactive controls for keyboard navigation", async () => {
    renderDrawer(createElement(RecordDrawer, { captureId: "cap-1", onClose: () => {} }));

    // Wait for the data to load and render
    await waitFor(() => expect(screen.getByText("Test Article Title")).toBeInTheDocument());

    // 1. Close button
    const closeBtn = screen.getByRole("button", { name: /Close/i });
    expect(closeBtn).toHaveClass("focus:outline-none");
    expect(closeBtn).toHaveClass("focus-visible:ring-1");
    expect(closeBtn).toHaveClass("focus-visible:ring-accent");

    // 2. Open source link (anchor)
    const sourceLink = screen.getByRole("link", { name: /open source/i });
    expect(sourceLink).toHaveClass("focus:outline-none");
    expect(sourceLink).toHaveClass("focus-visible:ring-1");
    expect(sourceLink).toHaveClass("focus-visible:ring-accent");
    expect(sourceLink).toHaveClass("rounded");

    // 3. Show raw JSON button
    const showRawBtn = screen.getByRole("button", { name: /show raw JSON/i });
    expect(showRawBtn).toHaveClass("focus:outline-none");
    expect(showRawBtn).toHaveClass("focus-visible:ring-1");
    expect(showRawBtn).toHaveClass("focus-visible:ring-accent");

    // 4. Tag pills (chip buttons)
    const earningsTag = await screen.findByTestId("tag-pill-earnings");
    expect(earningsTag).toHaveClass("focus:outline-none");
    expect(earningsTag).toHaveClass("focus-visible:ring-1");
    expect(earningsTag).toHaveClass("focus-visible:ring-accent");

    const techTag = screen.getByTestId("tag-pill-tech");
    expect(techTag).toHaveClass("focus:outline-none");
    expect(techTag).toHaveClass("focus-visible:ring-1");
    expect(techTag).toHaveClass("focus-visible:ring-accent");

    // 5. Input draft field
    const tagInput = screen.getByTestId("tag-input");
    expect(tagInput).toHaveClass("focus:outline-none");
    expect(tagInput).toHaveClass("focus-visible:ring-1");
    expect(tagInput).toHaveClass("focus-visible:ring-accent");

    // 6. Tag add button
    const tagAddBtn = screen.getByTestId("tag-add");
    expect(tagAddBtn).toHaveClass("focus:outline-none");
    expect(tagAddBtn).toHaveClass("focus-visible:ring-1");
    expect(tagAddBtn).toHaveClass("focus-visible:ring-accent");
  });
});
