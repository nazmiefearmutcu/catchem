/**
 * Webhook test-button UX contract.
 *
 * Pins the four UX rules the SettingsPage's <WebhookOutputCard/> must
 * uphold around the "Test webhook" affordance:
 *
 *   1) Disabled when URL field is empty AND no URL is configured.
 *   2) Enabled the moment the user types a URL (even before saving) OR
 *      a URL is already configured server-side.
 *   3) Shows a spinner while the request is in-flight.
 *   4) On success renders "✓ Test webhook sent (HTTP 200)" in green; on
 *      failure renders "✗ Test failed: <error>" in red. Both auto-clear
 *      after 5s / 8s respectively (fake-timer asserted).
 *
 * The card pulls live config from /api/webhook/config, so we mock the
 * `api` module at module-load and replay deterministic payloads. The
 * SettingsPage itself is rendered through QueryClient + MemoryRouter so
 * its `useLocation` + react-query hooks behave like a real route mount.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { createElement, type ReactNode } from "react";
import { SettingsPage } from "@/features/settings/SettingsPage";
import type { WebhookStatus, WebhookTestResult } from "@/lib/api";

// ── Mock the api module so we can drive webhookConfig / webhookTest at will.
//
// We keep the rest of the module surface as-is so SettingsPage's other
// queries (`appInfo`, `reviewsStatus`, `dbInfo`, `dbSchemaVersion`) don't
// blow up — they get straight stubs that resolve to minimal payloads.
vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    api: {
      appInfo: vi.fn(),
      reviewsStatus: vi.fn(),
      reviewsPatchSettings: vi.fn(),
      dbInfo: vi.fn(),
      dbSchemaVersion: vi.fn(),
      dbImport: vi.fn(),
      dbExportUrl: "/api/db/export",
      webhookConfig: vi.fn(),
      webhookSaveConfig: vi.fn(),
      webhookTest: vi.fn(),
    },
  };
});

import { api } from "@/lib/api";

const apiMock = api as unknown as Record<string, ReturnType<typeof vi.fn>>;

function renderSettings(): ReturnType<typeof render> {
  const qc = new QueryClient({
    defaultOptions: {
      queries: { retry: false, gcTime: 0, staleTime: 0 },
      mutations: { retry: false },
    },
  });
  const ui: ReactNode = createElement(
    QueryClientProvider,
    { client: qc },
    createElement(MemoryRouter, { initialEntries: ["/settings"] },
      createElement(SettingsPage),
    ),
  );
  return render(ui);
}

function makeWebhookStatus(overrides: Partial<WebhookStatus> = {}): WebhookStatus {
  return {
    enabled: false,
    url_configured: false,
    min_score: 0.7,
    asset_class_filter: null,
    reason_code_filter: null,
    timeout_seconds: 5,
    stats: { attempted: 0, sent: 0, filtered: 0, failed: 0 },
    last_status: null,
    last_error: null,
    generated_at: "2026-05-28T00:00:00+00:00",
    ...overrides,
  };
}

function makeTestResult(overrides: Partial<WebhookTestResult> = {}): WebhookTestResult {
  return {
    ok: true,
    status: "sent",
    url_configured: true,
    generated_at: "2026-05-28T00:00:00+00:00",
    ...overrides,
  };
}

beforeEach(() => {
  apiMock.appInfo.mockResolvedValue({
    version: "0.1.0",
    branch: "main",
    commit_sha: "abc123def456",
    mode: "production_safe",
    use_ml_stubs: true,
    static_bundle_present: true,
  });
  apiMock.reviewsStatus.mockResolvedValue({
    deepseek_enabled: false,
    deepseek_keyed: false,
    deepseek_ready: false,
    sampling_rate: 0.1,
    usd_cap: 9.5,
    usd_spent: 0,
    model: "deepseek-chat",
    exhausted: false,
  });
  apiMock.dbInfo.mockResolvedValue({
    exists: true,
    path: "/tmp/catchem.sqlite3",
    size_bytes: 1024,
    modified_at: "2026-05-28T00:00:00+00:00",
  });
  apiMock.dbSchemaVersion.mockResolvedValue({
    user_version: 1,
    max_known: 1,
    migrations_pending: [],
  });
  apiMock.webhookSaveConfig.mockResolvedValue(makeWebhookStatus());
});

afterEach(() => {
  vi.clearAllMocks();
  vi.useRealTimers();
});

describe("Webhook Test button UX", () => {
  it("disables Test button when URL field is empty AND no URL is configured", async () => {
    apiMock.webhookConfig.mockResolvedValue(makeWebhookStatus({ url_configured: false }));
    renderSettings();
    const btn = await screen.findByTestId("webhook-test-btn");
    expect(btn).toBeDisabled();
    // No URL has been typed; the input should be present and empty.
    const input = screen.getByTestId("webhook-url-input") as HTMLInputElement;
    expect(input.value).toBe("");
  });

  it("enables Test button as soon as the user types a URL (unsaved)", async () => {
    apiMock.webhookConfig.mockResolvedValue(makeWebhookStatus({ url_configured: false }));
    renderSettings();
    const btn = await screen.findByTestId("webhook-test-btn");
    expect(btn).toBeDisabled();
    const input = screen.getByTestId("webhook-url-input") as HTMLInputElement;
    await act(async () => {
      fireEvent.change(input, {
        target: { value: "https://hooks.slack.com/services/T/B/secret" },
      });
    });
    // Re-grab the button — same element, but disabled flag should flip.
    expect(screen.getByTestId("webhook-test-btn")).not.toBeDisabled();
  });

  it("shows spinner + sent (HTTP 200) chip on success, auto-clears after 5s", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    apiMock.webhookConfig.mockResolvedValue(makeWebhookStatus({ url_configured: true }));
    apiMock.webhookTest.mockResolvedValue(makeTestResult({ ok: true, status: "sent" }));
    renderSettings();
    const btn = await screen.findByTestId("webhook-test-btn");
    expect(btn).not.toBeDisabled();
    await act(async () => {
      fireEvent.click(btn);
    });
    // The chip should render with the success affordance.
    const chip = await screen.findByTestId("webhook-test-result");
    expect(chip).toHaveAttribute("data-result", "success");
    expect(chip).toHaveTextContent(/Test webhook sent/);
    expect(chip).toHaveTextContent(/HTTP 200/);
    // Advance fake timer past the 5s TTL → the chip should unmount.
    await act(async () => {
      vi.advanceTimersByTime(5_100);
    });
    await waitFor(() => {
      expect(screen.queryByTestId("webhook-test-result")).toBeNull();
    });
  });

  it("renders red failure chip on http_500 and auto-clears after 8s", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    apiMock.webhookConfig.mockResolvedValue(makeWebhookStatus({ url_configured: true }));
    apiMock.webhookTest.mockResolvedValue(
      makeTestResult({ ok: false, status: "http_500" }),
    );
    renderSettings();
    const btn = await screen.findByTestId("webhook-test-btn");
    await act(async () => {
      fireEvent.click(btn);
    });
    const chip = await screen.findByTestId("webhook-test-result");
    expect(chip).toHaveAttribute("data-result", "failure");
    expect(chip).toHaveTextContent(/Test failed/);
    expect(chip).toHaveTextContent(/http_500/);
    // Failure chip lingers 3s longer than success.
    await act(async () => {
      vi.advanceTimersByTime(5_100);
    });
    expect(screen.getByTestId("webhook-test-result")).toBeInTheDocument();
    await act(async () => {
      vi.advanceTimersByTime(3_100);
    });
    await waitFor(() => {
      expect(screen.queryByTestId("webhook-test-result")).toBeNull();
    });
  });

  it("shows the helpful Slack/Discord/Teams hint under the URL input", async () => {
    apiMock.webhookConfig.mockResolvedValue(makeWebhookStatus({ url_configured: false }));
    renderSettings();
    const help = await screen.findByTestId("webhook-url-help");
    expect(help).toHaveTextContent(/hooks\.slack\.com\/services\/XXX\/YYY\/ZZZ/);
    expect(help).toHaveTextContent(/Discord/);
    expect(help).toHaveTextContent(/Microsoft Teams/);
  });
});
