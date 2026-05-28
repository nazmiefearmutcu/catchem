/**
 * SMOKE test for the Settings page.
 *
 * Distinct from `resetPreferences.test.tsx` (which exhaustively pins the
 * v56 reset-preferences card + its pure helpers). This file is a breadth
 * check: it mounts the WHOLE `<SettingsPage>` against a minimal-but-valid
 * mock of `@/lib/api`, proves it renders without crashing, that every
 * major settings section is present, and that one preference interaction
 * (the language picker) actually persists to localStorage and re-renders
 * the active state.
 *
 * The api module is fully stubbed so the page's React Query reads resolve
 * deterministically — none of the cards depend on a live sidecar. The
 * preference hooks (useTheme / useAccent / i18n) are exercised for real;
 * an in-memory localStorage shim (same pattern as notificationCenter.test
 * + resetPreferences.test) backs their reads/writes. The i18n store is a
 * module-level singleton, so we reset it to English before each test to
 * keep the language assertion order-independent.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { act, fireEvent, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { createElement, type ReactNode } from "react";
import { SettingsPage } from "@/features/settings/SettingsPage";
import { I18N_KEY, setLang } from "@/lib/i18n";

// ── Mock the api module so SettingsPage's many queries resolve with a
// minimal valid config fixture. The page mounts DeepSeek, webhook, db
// backup and app-info cards; each gets a stable, well-shaped payload so
// nothing throws on first render.
vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    api: {
      appInfo: vi.fn().mockResolvedValue({
        version: "0.1.0",
        branch: "main",
        commit_sha: "abc123def456",
        mode: "production_safe",
        use_ml_stubs: true,
        static_bundle_present: true,
      }),
      reviewsStatus: vi.fn().mockResolvedValue({
        deepseek_enabled: false,
        deepseek_keyed: false,
        deepseek_ready: false,
        sampling_rate: 0.1,
        usd_cap: 9.5,
        usd_spent: 0,
        model: "deepseek-chat",
        exhausted: false,
      }),
      reviewsSpendHistory: vi.fn().mockResolvedValue({
        days: 7,
        history: [],
        totals: { calls: 0, cost_usd: 0 },
      }),
      reviewsPatchSettings: vi.fn(),
      dbInfo: vi.fn().mockResolvedValue({
        exists: true,
        path: "/tmp/catchem.sqlite3",
        size_bytes: 1024,
        modified_at: "2026-05-28T00:00:00+00:00",
      }),
      dbSchemaVersion: vi.fn().mockResolvedValue({
        user_version: 1,
        max_known: 1,
        migrations_pending: [],
      }),
      dbImport: vi.fn(),
      dbExportUrl: "/api/db/export",
      webhookConfig: vi.fn().mockResolvedValue({
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
      }),
      webhookSaveConfig: vi.fn(),
      webhookTest: vi.fn(),
    },
  };
});

// jsdom ships no Storage by default and the suite-wide setup leaves the
// shim to individual tests (see notificationCenter.test.tsx).
function installLocalStorage(): Storage {
  const store = new Map<string, string>();
  const shim: Storage = {
    get length() {
      return store.size;
    },
    clear: () => store.clear(),
    getItem: (k) => (store.has(k) ? store.get(k)! : null),
    key: (i) => Array.from(store.keys())[i] ?? null,
    removeItem: (k) => {
      store.delete(k);
    },
    setItem: (k, v) => {
      store.set(k, String(v));
    },
  };
  Object.defineProperty(window, "localStorage", { value: shim, configurable: true });
  return shim;
}

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
    createElement(
      MemoryRouter,
      { initialEntries: ["/settings"] },
      createElement(SettingsPage),
    ),
  );
  return render(ui);
}

beforeEach(() => {
  installLocalStorage();
  // The i18n store is a module-level singleton that survives across
  // tests; pin it to English so the language-picker assertions don't
  // depend on a previous test's locale flip.
  setLang("en");
});

afterEach(() => {
  vi.clearAllMocks();
});

describe("SettingsPage — smoke", () => {
  it("renders without crashing and shows the settings hero", async () => {
    renderSettings();
    // Hero headline mentions the active cockpit theme — proves the page
    // body mounted (not just a router shell).
    expect(
      await screen.findByText(/cockpit ·/i),
    ).toBeInTheDocument();
    // The shortcuts card lists the canonical "Open the command palette"
    // chord, so the static section also rendered.
    expect(screen.getByText(/Open the command palette/i)).toBeInTheDocument();
  });

  it("renders every major settings section", async () => {
    renderSettings();
    // Synchronous (no query): preference cards.
    expect(screen.getByTestId("accent-picker-card")).toBeInTheDocument();
    expect(screen.getByTestId("language-picker-card")).toBeInTheDocument();
    // Query-backed cards — appear once their queryFn resolves.
    expect(await screen.findByTestId("deepseek-reviewer-card")).toBeInTheDocument();
    expect(await screen.findByTestId("webhook-output-card")).toBeInTheDocument();
    expect(await screen.findByTestId("db-backup-card")).toBeInTheDocument();
    expect(await screen.findByTestId("workspace-snapshot-card")).toBeInTheDocument();
    expect(await screen.findByTestId("reset-preferences-card")).toBeInTheDocument();
  });

  it("exposes the accent swatches and theme toggle controls", async () => {
    renderSettings();
    // Accent radiogroup with the six presets + custom swatch.
    expect(screen.getByTestId("accent-swatches")).toBeInTheDocument();
    expect(screen.getByTestId("accent-swatch-blue")).toBeInTheDocument();
    expect(screen.getByTestId("accent-swatch-custom")).toBeInTheDocument();
    // Theme toggle in the hero — copy depends on the active theme.
    expect(
      screen.getByRole("button", { name: /switch to (light|dark)/i }),
    ).toBeInTheDocument();
  });

  it("reset-preferences open button is present and clickable", async () => {
    renderSettings();
    const openBtn = await screen.findByTestId("reset-preferences-open-btn");
    expect(openBtn).toBeInTheDocument();
    await act(async () => {
      fireEvent.click(openBtn);
    });
    // Clicking surfaces the confirmation modal — no wipe happens yet.
    expect(screen.getByTestId("reset-preferences-modal-card")).toBeInTheDocument();
  });

  it("toggling the language preference persists catchem.lang to localStorage", async () => {
    renderSettings();
    // English active by default (set in beforeEach).
    const en = await screen.findByTestId("language-option-en");
    const tr = await screen.findByTestId("language-option-tr");
    expect(en).toHaveAttribute("aria-checked", "true");
    expect(tr).toHaveAttribute("aria-checked", "false");
    // Nothing persisted until the user actually changes the locale.
    expect(window.localStorage.getItem(I18N_KEY)).toBeNull();

    await act(async () => {
      fireEvent.click(tr);
    });

    // Türkçe is now the active option and the choice is on disk.
    expect(tr).toHaveAttribute("aria-checked", "true");
    expect(en).toHaveAttribute("aria-checked", "false");
    expect(window.localStorage.getItem(I18N_KEY)).toBe("tr");
  });
});
