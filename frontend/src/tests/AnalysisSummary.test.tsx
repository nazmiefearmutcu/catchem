import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { AnalysisSummary } from "@/components/AnalysisSummary";
import type { DemoRunResponse, FinancialRecord } from "@/types/api";

/**
 * AnalysisSummary is the compact card rendered after a paste/upload run.
 * These tests pin its truth surfaces: capture_id (truncated + full-on-hover),
 * the four KPI tiles, optional title/link, pills, evidence, and the
 * diagnostic banner — plus graceful handling when optional fields are blank.
 */

// A fully-populated record so the "everything present" path renders.
function makeRecord(overrides: Partial<FinancialRecord> = {}): FinancialRecord {
  return {
    capture_id: "abcdefghijklmnopqrstuvwxyz0123456789",
    doc_id: "doc-1",
    title: "Apple beats earnings expectations",
    domain: "reuters.com",
    language: "en",
    url: "https://reuters.com/markets/apple",
    is_finance_relevant: true,
    finance_relevance_score: 0.82,
    asset_classes: ["equity"],
    impact_reason_codes: ["earnings", "guidance"],
    candidate_symbols: ["AAPL"],
    candidate_entities: ["Apple Inc"],
    impact_horizons: ["short_term"],
    sentiment_label: "positive",
    sentiment_score: 0.6,
    evidence_sentences: ["Revenue rose 8% year over year.", "EPS beat consensus."],
    reason_text: "Strong earnings beat.",
    component_scores: {},
    diagnostic_multimodal_enabled: false,
    diagnostic_multimodal_result: null,
    processing_mode: "production_safe",
    model_versions: {},
    published_ts: null,
    created_at: "2026-05-28T00:00:00Z",
    ...overrides,
  };
}

function makeResult(recordOverrides: Partial<FinancialRecord> = {}, top: Partial<DemoRunResponse> = {}): DemoRunResponse {
  const record = makeRecord(recordOverrides);
  return {
    capture_id: record.capture_id,
    jsonl_basename: "demo-2026-05-28.jsonl",
    processed: 3,
    skipped: 1,
    record,
    ...top,
  };
}

function renderSummary(result: DemoRunResponse) {
  return render(
    <MemoryRouter>
      <AnalysisSummary result={result} />
    </MemoryRouter>,
  );
}

describe("AnalysisSummary", () => {
  beforeEach(() => {
    // navigator.clipboard isn't implemented in jsdom; stub a writeText so the
    // "copy JSON" button has something to call (it uses optional chaining, so
    // this only matters for the explicit click test).
    Object.defineProperty(navigator, "clipboard", {
      value: { writeText: vi.fn().mockResolvedValue(undefined) },
      configurable: true,
    });
  });

  it("renders the labelled article shell", () => {
    renderSummary(makeResult());
    expect(screen.getByRole("article", { name: /analysis result/i })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: /analysis result/i })).toBeInTheDocument();
  });

  it("shows a truncated capture_id with the full id available on hover (title attr)", () => {
    const result = makeResult();
    renderSummary(result);
    const display = screen.getByTestId("capture-id-display");
    // Full id is exposed via the title attribute for hover + a11y.
    expect(display).toHaveAttribute("title", result.record.capture_id);
    // Visible text is truncated to the first 16 chars + ellipsis.
    expect(display.textContent).toContain(result.record.capture_id.slice(0, 16));
    // The full 36-char id is NOT rendered verbatim in the visible chip.
    expect(display.textContent).not.toContain(result.record.capture_id);
  });

  it("renders the jsonl basename", () => {
    renderSummary(makeResult());
    expect(screen.getByText("demo-2026-05-28.jsonl")).toBeInTheDocument();
  });

  it("renders the four KPI tiles (relevant / score / sentiment / processed-skipped)", () => {
    renderSummary(makeResult());
    expect(screen.getByText("finance relevant")).toBeInTheDocument();
    expect(screen.getByText("YES")).toBeInTheDocument();
    expect(screen.getByText("score")).toBeInTheDocument();
    // fmtScore(0.82) → "0.82"
    expect(screen.getByText("0.82")).toBeInTheDocument();
    expect(screen.getByText("sentiment")).toBeInTheDocument();
    expect(screen.getByText("positive")).toBeInTheDocument();
    const procLabel = screen.getByText("processed / skipped");
    // processed (3) and skipped (1) live in the sibling value div, split by a
    // slash span — assert on the combined tile text rather than a single node.
    const procValue = procLabel.nextElementSibling;
    expect(procValue?.textContent?.replace(/\s+/g, " ")).toContain("3 / 1");
  });

  it("shows 'no' when the record is not finance-relevant", () => {
    renderSummary(makeResult({ is_finance_relevant: false }));
    expect(screen.getByText("no")).toBeInTheDocument();
    expect(screen.queryByText("YES")).toBeNull();
  });

  it("renders the title as an external link when a safe url is present", () => {
    renderSummary(makeResult());
    const link = screen.getByRole("link", { name: /apple beats earnings expectations/i });
    expect(link).toHaveAttribute("href", "https://reuters.com/markets/apple");
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveAttribute("rel", expect.stringContaining("noopener"));
  });

  it("renders the title as plain text (no link) when the url is missing/unsafe", () => {
    renderSummary(makeResult({ url: null, title: "Plain headline" }));
    expect(screen.getByText("Plain headline")).toBeInTheDocument();
    // No external link should be produced for the headline.
    expect(screen.queryByRole("link", { name: /plain headline/i })).toBeNull();
  });

  it("renders asset-class, reason-code and symbol pills", () => {
    renderSummary(makeResult());
    expect(screen.getByText("equity")).toBeInTheDocument();
    expect(screen.getByText("earnings")).toBeInTheDocument();
    expect(screen.getByText("guidance")).toBeInTheDocument();
    expect(screen.getByText("AAPL")).toBeInTheDocument();
  });

  it("renders evidence sentences (capped at 3)", () => {
    renderSummary(
      makeResult({
        evidence_sentences: ["one", "two", "three", "four"],
      }),
    );
    expect(screen.getByText("evidence")).toBeInTheDocument();
    expect(screen.getByText("one")).toBeInTheDocument();
    expect(screen.getByText("three")).toBeInTheDocument();
    // The 4th sentence is sliced off.
    expect(screen.queryByText("four")).toBeNull();
  });

  it("links to the feed detail route with an encoded capture_id", () => {
    const result = makeResult();
    renderSummary(result);
    const feedLink = screen.getByRole("link", { name: /open in feed/i });
    expect(feedLink).toHaveAttribute(
      "href",
      `/feed/${encodeURIComponent(result.record.capture_id)}`,
    );
  });

  it("flags DIAGNOSTIC mode when processing_mode is not production_safe", () => {
    renderSummary(makeResult({ processing_mode: "diagnostic" }));
    expect(screen.getByText("DIAGNOSTIC")).toBeInTheDocument();
    expect(screen.getByText(/mode diagnostic/i)).toBeInTheDocument();
  });

  it("does not show the DIAGNOSTIC pill in production_safe mode", () => {
    renderSummary(makeResult({ processing_mode: "production_safe" }));
    expect(screen.queryByText("DIAGNOSTIC")).toBeNull();
  });

  it("handles missing optional fields gracefully (no title, no domain, no evidence, em-dash sentiment)", () => {
    renderSummary(
      makeResult({
        title: null,
        domain: null,
        sentiment_label: null,
        evidence_sentences: [],
        asset_classes: [],
        impact_reason_codes: [],
        candidate_symbols: [],
      }),
    );
    // Card still renders.
    expect(screen.getByRole("article", { name: /analysis result/i })).toBeInTheDocument();
    // Sentiment falls back to an em-dash placeholder.
    expect(screen.getByText("—")).toBeInTheDocument();
    // No evidence section when there are zero sentences.
    expect(screen.queryByText("evidence")).toBeNull();
    // No headline link/heading for the (absent) title.
    expect(screen.queryByRole("heading", { name: /apple/i })).toBeNull();
  });

  it("implements custom focus-visible ring styles on footer controls for keyboard navigation", () => {
    renderSummary(makeResult());
    const feedLink = screen.getByRole("link", { name: /open in feed/i });
    const copyBtn = screen.getByRole("button", { name: /copy JSON/i });

    expect(feedLink).toHaveClass("focus:outline-none");
    expect(feedLink).toHaveClass("focus-visible:ring-1");
    expect(feedLink).toHaveClass("focus-visible:ring-accent");

    expect(copyBtn).toHaveClass("focus:outline-none");
    expect(copyBtn).toHaveClass("focus-visible:ring-1");
    expect(copyBtn).toHaveClass("focus-visible:ring-accent");
  });
});
