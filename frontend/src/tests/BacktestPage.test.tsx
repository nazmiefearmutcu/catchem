import { describe, it, expect, beforeEach, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { UIBacktest } from "@/types/api";

/**
 * Smoke tests for the Backtest page (v31 / task #117): prediction calibration
 * of the cheap stub vs DeepSeek "expert ground truth". We assert the page
 * mounts, renders the mocked summary (MAE headline + KPI tiles), shows the
 * calibration rows + predictions sample, and degrades to skeleton + the
 * friendly empty state without crashing. ECharts is stubbed to null.
 */

// `vi.hoisted` lets this spy exist before the hoisted vi.mock factory runs.
const { backtest } = vi.hoisted(() => ({ backtest: vi.fn() }));

vi.mock("@/lib/api", async (importActual) => {
  const actual = await importActual<typeof import("@/lib/api")>();
  return {
    ...actual,
    api: { backtest },
  };
});

// Calibration bar chart is visual-only; render nothing in tests.
vi.mock("@/charts/EChart", () => ({ EChart: () => null }));

import { BacktestPage } from "@/features/backtest/BacktestPage";

const POPULATED: UIBacktest = {
  schema_version: 1,
  ran_at: "2026-05-28T12:00:00Z",
  sample_size: 200,
  summary: {
    items_evaluated: 42,
    mean_abs_error: 0.073,
    mean_signed_error: 0.012,
    max_abs_error: 0.31,
  },
  calibration_bins: [
    {
      bin_low: 0.6,
      bin_high: 0.8,
      predicted_count: 18,
      avg_predicted_score: 0.71,
      avg_ground_truth_score: 0.68,
      calibration_gap: 0.03,
    },
    {
      bin_low: 0.8,
      bin_high: 1.0,
      predicted_count: 24,
      avg_predicted_score: 0.9,
      avg_ground_truth_score: 0.86,
      calibration_gap: 0.04,
    },
  ],
  predictions_sample: [
    {
      capture_id: "cap-101",
      predicted_score: 0.72,
      ground_truth_score: 0.7,
      delta: 0.02,
    },
    {
      capture_id: "cap-102",
      predicted_score: 0.55,
      ground_truth_score: 0.81,
      delta: -0.26,
    },
  ],
};

const EMPTY: UIBacktest = {
  schema_version: 1,
  ran_at: "2026-05-28T12:00:00Z",
  sample_size: 200,
  summary: {
    items_evaluated: 0,
    mean_abs_error: 0,
    mean_signed_error: 0,
    max_abs_error: 0,
  },
  calibration_bins: [],
  predictions_sample: [],
};

function makeClient() {
  return new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
}

function renderPage() {
  return render(
    <MemoryRouter initialEntries={["/backtest"]}>
      <QueryClientProvider client={makeClient()}>
        <BacktestPage />
      </QueryClientProvider>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  backtest.mockResolvedValue(POPULATED);
});

describe("BacktestPage", () => {
  it("renders the MAE headline and mocked calibration data", async () => {
    renderPage();

    // Dynamic hero headline anchors on the mean-abs-error figure.
    expect(
      await screen.findByText(/Mean abs error 7\.3pp across 42 paired reviews/i),
    ).toBeInTheDocument();

    // Eyebrow + KPI tiles + section headers mounted.
    expect(screen.getByText(/Backtest/i)).toBeInTheDocument();
    expect(screen.getByText(/items evaluated/i)).toBeInTheDocument();
    expect(
      screen.getByText(/calibration · predicted vs ground truth/i),
    ).toBeInTheDocument();

    // Populated bin count + a sample capture id prove the lists render.
    expect(screen.getByText(/2 populated bins/i)).toBeInTheDocument();
    expect(screen.getByText("cap-101")).toBeInTheDocument();
    expect(screen.getByText("cap-102")).toBeInTheDocument();
  });

  it("shows the loading skeleton before data resolves", () => {
    backtest.mockReturnValue(new Promise(() => {}));
    const { container } = renderPage();
    expect(container.querySelector(".animate-shimmer")).not.toBeNull();
    expect(screen.queryByText(/Mean abs error/i)).toBeNull();
  });

  it("renders the friendly empty state when zero paired reviews exist", async () => {
    backtest.mockResolvedValue(EMPTY);
    renderPage();

    expect(
      await screen.findByText(/No paired reviews yet/i),
    ).toBeInTheDocument();
    // Empty-state explanatory copy from the chart/table fallback.
    expect(
      screen.getAllByText(/No paired \(stub, DeepSeek\) reviews available/i).length,
    ).toBeGreaterThan(0);
    expect(screen.getByText(/0 populated bins/i)).toBeInTheDocument();
  });

  it("implements custom focus-visible ring styles on all interactive controls for keyboard navigation", async () => {
    renderPage();

    const sampleSelect = await screen.findByRole("combobox", { name: /sample size/i });
    const rerunBtn = await screen.findByRole("button", { name: /re-run/i });

    expect(sampleSelect).toHaveClass("focus:outline-none");
    expect(sampleSelect).toHaveClass("focus-visible:ring-1");
    expect(sampleSelect).toHaveClass("focus-visible:ring-accent");

    expect(rerunBtn).toHaveClass("focus:outline-none");
    expect(rerunBtn).toHaveClass("focus-visible:ring-1");
    expect(rerunBtn).toHaveClass("focus-visible:ring-accent");
  });
});
