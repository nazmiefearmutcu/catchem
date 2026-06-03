import { describe, it, expect, beforeEach, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { LogsPage, classifyLine, deriveLinesPerMinute } from "@/features/logs/LogsPage";

// ── api mock ───────────────────────────────────────────────────────────────
const logTailMock = vi.fn();

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    api: {
      logTail: (limit: number) => logTailMock(limit),
    },
  };
});

function renderLogsPage(): ReturnType<typeof render> {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <LogsPage />
      </MemoryRouter>
    </QueryClientProvider>
  );
}

describe("classifyLine", () => {
  it("tags python/uvicorn level prefixes", () => {
    expect(classifyLine("INFO:     uvicorn started")).toBe("info");
    expect(classifyLine("WARNING: feed not configured")).toBe("warn");
    expect(classifyLine("ERROR: failed to connect")).toBe("error");
  });

  it("tags structlog-style key=value lines", () => {
    expect(classifyLine("2026-05-28T01:00 level=info msg=started")).toBe("info");
    expect(classifyLine("2026-05-28T01:00 level=warning msg=slow")).toBe("warn");
    expect(classifyLine("2026-05-28T01:00 level=error msg=oops")).toBe("error");
  });

  it("falls back to other for unrecognized lines", () => {
    expect(classifyLine("just a free-form trace")).toBe("other");
    expect(classifyLine("")).toBe("other");
    expect(classifyLine("12345")).toBe("other");
  });

  it("treats critical/fatal as error", () => {
    expect(classifyLine("CRITICAL: oh no")).toBe("error");
    expect(classifyLine("FATAL: stop")).toBe("error");
  });

  it("does not match info/error if they appear only inside a URL", () => {
    expect(classifyLine("GET /api/info HTTP/1.1")).toBe("other");
  });
});

describe("deriveLinesPerMinute", () => {
  it("returns 0 when the window is smaller than 1s", () => {
    expect(deriveLinesPerMinute(0, 1000, 100, 1500)).toBe(0);
  });

  it("returns 0 on a stale or zero delta", () => {
    expect(deriveLinesPerMinute(50, 1000, 50, 4000)).toBe(0);
    expect(deriveLinesPerMinute(50, 1000, 40, 4000)).toBe(0);
  });

  it("computes lines/min from delta over elapsed window", () => {
    expect(deriveLinesPerMinute(0, 0, 60, 60_000)).toBe(60);
    expect(deriveLinesPerMinute(0, 0, 10, 30_000)).toBe(20);
  });

  it("guards against backwards clock skew", () => {
    expect(deriveLinesPerMinute(0, 5000, 100, 4000)).toBe(0);
  });
});

describe("LogsPage component rendering and focus rings", () => {
  beforeEach(() => {
    logTailMock.mockReset();
  });

  it("renders the toolbar and interactive controls with focus rings", async () => {
    logTailMock.mockResolvedValue({
      lines: [
        "INFO:     uvicorn started",
        "WARNING:  feed not configured",
        "ERROR:    failed to connect"
      ],
      total_lines: 3,
      truncated: false
    });

    renderLogsPage();

    // Wait for the data to resolve and check that controls have the focus classes
    const refreshBtn = await screen.findByRole("button", { name: /refresh/i });
    expect(refreshBtn).toHaveClass("focus:outline-none");
    expect(refreshBtn).toHaveClass("focus-visible:ring-1");
    expect(refreshBtn).toHaveClass("focus-visible:ring-accent");

    const filterSelect = screen.getByTestId("logs-filter-select");
    expect(filterSelect).toHaveClass("focus:outline-none");
    expect(filterSelect).toHaveClass("focus-visible:ring-1");
    expect(filterSelect).toHaveClass("focus-visible:ring-accent");

    const searchInput = screen.getByTestId("logs-search-input");
    expect(searchInput).toHaveClass("focus:outline-none");
    expect(searchInput).toHaveClass("focus-visible:ring-1");
    expect(searchInput).toHaveClass("focus-visible:ring-accent");

    const autoscrollCheckbox = screen.getByTestId("logs-autoscroll-toggle");
    expect(autoscrollCheckbox).toHaveClass("focus:outline-none");
    expect(autoscrollCheckbox).toHaveClass("focus-visible:ring-1");
    expect(autoscrollCheckbox).toHaveClass("focus-visible:ring-accent");

    const pauseCheckbox = screen.getByTestId("logs-pause-toggle");
    expect(pauseCheckbox).toHaveClass("focus:outline-none");
    expect(pauseCheckbox).toHaveClass("focus-visible:ring-1");
    expect(pauseCheckbox).toHaveClass("focus-visible:ring-accent");

    const copyBtn = screen.getByTestId("logs-copy-button");
    expect(copyBtn).toHaveClass("focus:outline-none");
    expect(copyBtn).toHaveClass("focus-visible:ring-1");
    expect(copyBtn).toHaveClass("focus-visible:ring-accent");
  });

  it("renders empty state with action link having custom focus rings", async () => {
    logTailMock.mockResolvedValue({
      lines: [],
      total_lines: 0,
      truncated: false
    });

    renderLogsPage();

    const emptyCta = await screen.findByTestId("logs-empty-cta");
    expect(emptyCta).toHaveClass("focus:outline-none");
    expect(emptyCta).toHaveClass("focus-visible:ring-1");
    expect(emptyCta).toHaveClass("focus-visible:ring-accent");
  });

  it("renders clear filters button when search returns no match with focus rings", async () => {
    logTailMock.mockResolvedValue({
      lines: ["INFO: line 1"],
      total_lines: 1,
      truncated: false
    });

    renderLogsPage();

    // Type query that won't match
    const searchInput = await screen.findByTestId("logs-search-input");
    fireEvent.change(searchInput, { target: { value: "nonexistent-query-string" } });

    const clearFiltersBtn = await screen.findByRole("button", { name: /clear filters/i });
    expect(clearFiltersBtn).toHaveClass("focus:outline-none");
    expect(clearFiltersBtn).toHaveClass("focus-visible:ring-1");
    expect(clearFiltersBtn).toHaveClass("focus-visible:ring-accent");
  });
});
