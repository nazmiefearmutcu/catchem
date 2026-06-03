import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { createElement, type ReactNode } from "react";

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    api: {
      appInfo: vi.fn(),
    },
  };
});

import { api } from "@/lib/api";
import { HelpPage } from "@/features/help/HelpPage";

const apiMock = api as unknown as Record<string, ReturnType<typeof vi.fn>>;

function renderHelp() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    createElement(
      QueryClientProvider,
      { client: qc },
      createElement(
        MemoryRouter,
        { initialEntries: ["/help"] },
        createElement(HelpPage),
      ),
    ) as ReactNode,
  );
}

describe("HelpPage", () => {
  beforeEach(() => {
    Object.values(apiMock).forEach((fn) => fn.mockReset());
    apiMock.appInfo.mockResolvedValue({
      name: "catchem",
      version: "0.1.0",
      branch: "main",
      commit_sha: "ed7b00a",
      mode: "production",
      use_ml_stubs: true,
    });
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("renders help page content and info", async () => {
    renderHelp();
    expect(await screen.findByText(/Local-first news-to-finance analyst workstation/i)).toBeInTheDocument();
    expect(screen.getByText("quick start")).toBeInTheDocument();
    expect(screen.getByText("glossary")).toBeInTheDocument();
  });

  it("implements custom focus-visible ring styles on all interactive controls for keyboard navigation", async () => {
    renderHelp();
    
    // Wait for content to render
    expect(await screen.findByText(/Local-first news-to-finance/)).toBeInTheDocument();

    // 1. Replay welcome tour button
    const replayBtn = screen.getByRole("button", { name: /Replay welcome tour/i });
    expect(replayBtn).toHaveClass("focus:outline-none");
    expect(replayBtn).toHaveClass("focus-visible:ring-1");
    expect(replayBtn).toHaveClass("focus-visible:ring-accent");

    // 2. HelpStat step 1 Link
    const step1Link = screen.getByRole("link", { name: /Open Live Feed/i });
    expect(step1Link).toHaveClass("focus:outline-none");
    expect(step1Link).toHaveClass("focus-visible:ring-1");
    expect(step1Link).toHaveClass("focus-visible:ring-accent");

    // 3. Quick start inline link "Live Feed"
    const liveFeedLink = screen.getByRole("link", { name: /^Live Feed$/i });
    expect(liveFeedLink).toHaveClass("focus:outline-none");
    expect(liveFeedLink).toHaveClass("focus-visible:ring-1");
    expect(liveFeedLink).toHaveClass("focus-visible:ring-accent");

    // 4. API Reference link card (swagger)
    const swaggerLink = screen.getByRole("link", { name: /API reference/i });
    expect(swaggerLink).toHaveClass("focus:outline-none");
    expect(swaggerLink).toHaveClass("focus-visible:ring-1");
    expect(swaggerLink).toHaveClass("focus-visible:ring-accent");

    // 5. Settings link at bottom
    const settingsLink = screen.getByRole("link", { name: /keyboard shortcuts/i });
    expect(settingsLink).toHaveClass("focus:outline-none");
    expect(settingsLink).toHaveClass("focus-visible:ring-1");
    expect(settingsLink).toHaveClass("focus-visible:ring-accent");
  });
});
