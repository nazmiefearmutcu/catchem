import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { render, screen, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { createElement, type ReactNode } from "react";
import type { GuardSnapshot, UISummary } from "@/types/api";

// ── @/lib/api mock ──────────────────────────────────────────────────────────
// OpsPage drives off seven query functions: summary / config / metrics /
// guards / stats / healthDeep / dbStats. We mock all of them so the page can
// render in jsdom without a live sidecar. Everything else on the module
// (ApiError, fmt* helpers) is preserved via importActual — the page imports
// only `api`, but the i18n + freshness libs it pulls in are untouched.
vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    api: {
      summary: vi.fn(),
      config: vi.fn(),
      metrics: vi.fn(),
      guards: vi.fn(),
      stats: vi.fn(),
      healthDeep: vi.fn(),
      dbStats: vi.fn(),
    },
  };
});

import { api } from "@/lib/api";

const apiMock = api as unknown as Record<string, ReturnType<typeof vi.fn>>;

function renderOps() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    createElement(
      QueryClientProvider,
      { client: qc },
      createElement(MemoryRouter, { initialEntries: ["/ops"] }, createElement(OpsPage)),
    ) as ReactNode,
  );
}

// Import after the mock is registered so the page binds to the mocked `api`.
import { OpsPage } from "@/features/ops/OpsPage";

function guard(): GuardSnapshot {
  return {
    ok: true,
    release_gate_passed: false,
    quarantine_state: "QUARANTINED",
    fusion_verdict_class: "FUSION_REGRESSIVE",
    safe_to_publish: false,
    safe_to_promote: false,
    governance_index_sha256: "abcdef0123456789abcdef0123456789",
  };
}

function summary(overrides: Partial<UISummary> = {}): UISummary {
  return {
    mode: "production_safe",
    is_production_safe: true,
    diagnostic_allowed: false,
    use_ml_stubs: true,
    totals: { total: 1280, finance_relevant: 640 },
    diagnostic_count: 0,
    asset_class_distribution: {},
    reason_code_distribution: {},
    sentiment_distribution: {},
    recent_top: [],
    dlq: 0,
    model_versions: { scorer: "v3", sentiment: "stub-1" },
    guards: guard(),
    generated_at: "2026-05-28T00:00:00Z",
    ...overrides,
  };
}

function stats() {
  return {
    schema_version: 1,
    generated_at: "2026-05-28T00:00:00Z",
    uptime_seconds: 3725,
    total_requests: 4096,
    request_counts: { "/ui/summary": 120, "/api/stats": 80 },
    db: { records: 1280, reviews: 12, dlq: 0 },
    reviewers: { deepseek_usd_spent: 0, stub_active: true },
    process: {
      rss_mb: 142.5,
      vms_mb: 512,
      cpu_percent: 1.4,
      num_threads: 8,
      psutil_available: true,
    },
    version: "test",
  };
}

function deepHealth(ok = true) {
  return {
    ok,
    checks: { storage: "ok" },
    issues: ok ? [] : ["storage_locked"],
    generated_at: "2026-05-28T00:00:00Z",
    schema_version: 1,
  };
}

function dbStats() {
  return {
    schema_version: 1,
    generated_at: "2026-05-28T00:00:00Z",
    tables: [
      { name: "records", rows: 1280 },
      { name: "reviews", rows: 12 },
      { name: "record_tags", rows: 0 },
    ],
    indexes: [{ name: "ix_records_ts", table: "records" }],
    total_tables: 3,
    total_indexes: 1,
    page_count: 512,
    page_size_bytes: 4096,
    estimated_size_bytes: 2_097_152,
  };
}

function happyDefaults() {
  apiMock.summary.mockResolvedValue(summary());
  apiMock.config.mockResolvedValue({});
  apiMock.metrics.mockResolvedValue({});
  apiMock.guards.mockResolvedValue(guard());
  apiMock.stats.mockResolvedValue(stats());
  apiMock.healthDeep.mockResolvedValue(deepHealth(true));
  apiMock.dbStats.mockResolvedValue(dbStats());
}

beforeEach(() => {
  Object.values(apiMock).forEach((fn) => fn.mockReset());
  happyDefaults();
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("OpsPage smoke", () => {
  it("renders the status hero with mocked summary data", async () => {
    renderOps();

    // Hero eyebrow synthesizes "Ops · system status · <mode>".
    expect(await screen.findByText(/system status/i)).toBeInTheDocument();
    // Nominal headline (no DLQ / diagnostic alarm) resolves from i18n.
    expect(screen.getByText("All systems nominal")).toBeInTheDocument();
    // KPI tile reflects the finance-relevant ratio from totals.
    expect(screen.getByText("1,280 · 640")).toBeInTheDocument();
  });

  it("renders the Database breakdown card with per-table rows", async () => {
    renderOps();

    const heading = await screen.findByText("Database breakdown");
    const section = heading.closest("section");
    expect(section).not.toBeNull();
    // Biggest table sorts to the top; rows are locale-formatted.
    expect(within(section as HTMLElement).getByText("records")).toBeInTheDocument();
    expect(within(section as HTMLElement).getByText("1,280")).toBeInTheDocument();
    // Zero-row table surfaces explicitly (no silent drop).
    expect(within(section as HTMLElement).getByText("record_tags")).toBeInTheDocument();
    // Summary line interpolates table/index/size counts.
    expect(
      within(section as HTMLElement).getByText(/3 tables · 1 indexes · 2\.0 MB/),
    ).toBeInTheDocument();
  });

  it("renders runtime stats with a READY deep-health pill", async () => {
    renderOps();

    const pill = await screen.findByTestId("deep-health-pill");
    expect(pill).toHaveAttribute("data-ok", "1");
    expect(within(pill).getByText(/READY/)).toBeInTheDocument();
    // Uptime humanizes 3725s → "1h 2m".
    expect(screen.getByText("1h 2m")).toBeInTheDocument();
  });

  it("renders gracefully when the DLQ alarm fires and deep health degrades", async () => {
    apiMock.summary.mockResolvedValue(summary({ dlq: 3, diagnostic_count: 0 }));
    apiMock.healthDeep.mockResolvedValue(deepHealth(false));

    renderOps();

    // Hero headline flips to the loud DLQ message instead of "nominal".
    expect(
      await screen.findByText("DLQ has 3 unprocessed records"),
    ).toBeInTheDocument();
    expect(screen.queryByText("All systems nominal")).not.toBeInTheDocument();
    // Deep-health pill reports the issue count, not READY.
    const pill = screen.getByTestId("deep-health-pill");
    expect(pill).toHaveAttribute("data-ok", "0");
    expect(within(pill).getByText(/1 ISSUE/)).toBeInTheDocument();
  });

  it("renders without crashing when telemetry endpoints fail or return empty", async () => {
    // dbStats + stats reject; the page must still render the hero. The
    // DatabaseBreakdownCard + RuntimeStatsCard return null / error-box
    // instead of throwing.
    apiMock.stats.mockRejectedValue(new Error("/api/stats → 500 boom"));
    apiMock.dbStats.mockRejectedValue(new Error("/api/db/stats → 500 boom"));
    apiMock.guards.mockResolvedValue({ ok: false, error_code: "missing_governance_index" });

    renderOps();

    // Hero still synthesizes from summary even with sibling card failures.
    expect(await screen.findByText("All systems nominal")).toBeInTheDocument();
    // Database breakdown card is absent (errored → null), not a crash.
    expect(screen.queryByText("Database breakdown")).not.toBeInTheDocument();
    // Benign guard note renders instead of an alarming red error.
    expect(
      screen.getByText(/not configured on this machine/i),
    ).toBeInTheDocument();
  });
});
