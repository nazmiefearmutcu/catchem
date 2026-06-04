/**
 * Reset-preferences settings card + confirmation modal.
 *
 * Pins the contract for the v56 "Reset all preferences" destructive
 * affordance in SettingsPage:
 *
 *   1. The card surfaces an accurate count of `catchem.*` keys held in
 *      localStorage, ignoring any keys not under that namespace.
 *   2. Clicking the open button reveals a confirmation modal — the wipe
 *      does NOT happen until "Reset & reload" is clicked.
 *   3. Confirming clears EVERY `catchem.*` key, leaves other-namespace keys
 *      alone (other apps in the same origin must not be collateral
 *      damage), and triggers a page reload.
 *   4. Cancel and Esc back out without touching storage or reloading.
 *
 * jsdom doesn't ship a full Storage implementation OR a `location.reload`
 * stub, so we install our own minimal shim before each test (matches the
 * pattern used by snapshot.test.ts and storageSync.test.ts).
 */
import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { act, fireEvent, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { createElement, type ReactNode } from "react";
import {
  SettingsPage,
  collectCatchemKeys,
  resetCatchemPreferences,
} from "@/features/settings/SettingsPage";

// ── Mock the api module so SettingsPage's other queries don't blow up.
//
// We don't care about webhook/db/DeepSeek behaviour here — they get
// straight stubs so the page renders. The reset card sits below those
// other cards and doesn't depend on any of them.
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

function installReloadSpy(): ReturnType<typeof vi.fn> {
  const reload = vi.fn();
  Object.defineProperty(window, "location", {
    configurable: true,
    value: { ...window.location, reload },
  });
  return reload;
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
});

afterEach(() => {
  vi.clearAllMocks();
});

describe("collectCatchemKeys — pure storage scan", () => {
  it("returns only keys under the `catchem.` namespace", () => {
    localStorage.setItem("catchem.theme", "dark");
    localStorage.setItem("catchem.accent", "violet");
    localStorage.setItem("catchem.watchlist", JSON.stringify(["AAPL"]));
    // Other apps in the same origin — must NOT be returned.
    localStorage.setItem("other-app.session", "xyz");
    localStorage.setItem("devtools.log-level", "debug");
    // "catchem" without trailing dot is intentionally excluded — we
    // require the namespace separator so that a third-party key like
    // "catchemistry" isn't accidentally caught.
    localStorage.setItem("catchemistry", "should-not-match");

    const keys = collectCatchemKeys();

    expect(keys.sort()).toEqual(
      ["catchem.theme", "catchem.accent", "catchem.watchlist"].sort(),
    );
    expect(keys).not.toContain("other-app.session");
    expect(keys).not.toContain("devtools.log-level");
    expect(keys).not.toContain("catchemistry");
  });

  it("returns an empty list when no catchem keys exist", () => {
    localStorage.setItem("unrelated", "1");
    expect(collectCatchemKeys()).toEqual([]);
  });
});

describe("resetCatchemPreferences — pure wipe", () => {
  it("removes every catchem.* key and leaves other keys untouched", () => {
    localStorage.setItem("catchem.theme", "dark");
    localStorage.setItem("catchem.accent", "violet");
    localStorage.setItem("catchem.watchlist", JSON.stringify(["AAPL"]));
    localStorage.setItem("catchem.onboarding.completed", "true");
    // These three must survive the reset — they belong to other apps /
    // origins / categories that the user didn't ask us to touch.
    localStorage.setItem("other-app.session", "xyz");
    localStorage.setItem("devtools.log-level", "debug");
    localStorage.setItem("catchemistry", "preserve-me");

    const removed = resetCatchemPreferences();

    expect(removed.sort()).toEqual(
      [
        "catchem.theme",
        "catchem.accent",
        "catchem.watchlist",
        "catchem.onboarding.completed",
      ].sort(),
    );
    expect(localStorage.getItem("catchem.theme")).toBeNull();
    expect(localStorage.getItem("catchem.accent")).toBeNull();
    expect(localStorage.getItem("catchem.watchlist")).toBeNull();
    expect(localStorage.getItem("catchem.onboarding.completed")).toBeNull();
    // Non-Catchem keys are preserved.
    expect(localStorage.getItem("other-app.session")).toBe("xyz");
    expect(localStorage.getItem("devtools.log-level")).toBe("debug");
    expect(localStorage.getItem("catchemistry")).toBe("preserve-me");
  });
});

describe("ResetPreferencesCard — UI flow", () => {
  it("renders the card with the live count of catchem.* keys", async () => {
    localStorage.setItem("catchem.theme", "dark");
    localStorage.setItem("catchem.watchlist", JSON.stringify(["AAPL"]));
    localStorage.setItem("not-our-key", "1");

    renderSettings();

    const card = await screen.findByTestId("reset-preferences-card");
    expect(card).toBeInTheDocument();
    const count = await screen.findByTestId("reset-preferences-count");
    // The card must show "N keys stored". useAccent may write a default
    // `catchem.accent` on mount so we only assert the floor (≥ 2 from our
    // seeds) and that the non-Catchem key is not contributing — checked
    // implicitly because exclusion logic is fully covered by
    // collectCatchemKeys' unit test above.
    const match = count.textContent?.match(/(\d+)\s+keys?\s+stored/);
    expect(match).not.toBeNull();
    expect(Number(match![1])).toBeGreaterThanOrEqual(2);
  });

  it("opens the modal on button click without touching storage", async () => {
    // We use `catchem.watchlist` (opaque JSON value, hooks don't mutate it)
    // alongside `catchem.theme` rather than `catchem.accent` because the
    // useAccent hook normalises unknown preset ids back to "blue" on mount,
    // which would defeat the "storage unchanged" assertion below.
    localStorage.setItem("catchem.theme", "dark");
    localStorage.setItem("catchem.watchlist", JSON.stringify(["AAPL"]));
    const reload = installReloadSpy();

    renderSettings();

    const openBtn = await screen.findByTestId("reset-preferences-open-btn");
    await act(async () => {
      fireEvent.click(openBtn);
    });
    // Modal up.
    expect(screen.getByTestId("reset-preferences-modal-card")).toBeInTheDocument();
    // Storage unchanged.
    expect(localStorage.getItem("catchem.theme")).toBe("dark");
    expect(localStorage.getItem("catchem.watchlist")).toBe(JSON.stringify(["AAPL"]));
    // No reload yet.
    expect(reload).not.toHaveBeenCalled();
    // Modal body shows a live count — at least the 2 keys we seeded
    // (the hook may have written `catchem.accent` itself on mount, so we
    // bound-check rather than equality-check).
    const countText = screen.getByTestId("reset-preferences-modal-count").textContent;
    expect(Number(countText)).toBeGreaterThanOrEqual(2);
  });

  it("Cancel closes the modal without wiping storage or reloading", async () => {
    localStorage.setItem("catchem.theme", "dark");
    localStorage.setItem("catchem.watchlist", JSON.stringify(["AAPL"]));
    const reload = installReloadSpy();

    renderSettings();
    const openBtn = await screen.findByTestId("reset-preferences-open-btn");
    await act(async () => {
      fireEvent.click(openBtn);
    });
    const cancel = screen.getByTestId("reset-preferences-modal-cancel");
    await act(async () => {
      fireEvent.click(cancel);
    });

    // Modal closed.
    expect(screen.queryByTestId("reset-preferences-modal-card")).toBeNull();
    // Storage intact for the keys we seeded — see comment in the previous
    // test re: useAccent normalisation.
    expect(localStorage.getItem("catchem.theme")).toBe("dark");
    expect(localStorage.getItem("catchem.watchlist")).toBe(JSON.stringify(["AAPL"]));
    // No reload.
    expect(reload).not.toHaveBeenCalled();
  });

  it("Reset & reload wipes catchem.* keys, preserves others, calls reload", async () => {
    localStorage.setItem("catchem.theme", "dark");
    localStorage.setItem("catchem.watchlist", JSON.stringify(["AAPL", "TSLA"]));
    localStorage.setItem("catchem.onboarding.completed", "true");
    // Two non-Catchem keys — must survive.
    localStorage.setItem("other-app.session", "should-survive");
    localStorage.setItem("third-party.flag", "yes");
    const reload = installReloadSpy();

    renderSettings();
    const openBtn = await screen.findByTestId("reset-preferences-open-btn");
    await act(async () => {
      fireEvent.click(openBtn);
    });
    const confirm = screen.getByTestId("reset-preferences-modal-confirm");
    await act(async () => {
      fireEvent.click(confirm);
    });

    // Every catchem.* key — including ones the SettingsPage hooks may
    // have written on mount (e.g. catchem.accent from useAccent's
    // default-write) — must be gone after a confirmed reset.
    expect(collectCatchemKeys()).toEqual([]);
    expect(localStorage.getItem("catchem.theme")).toBeNull();
    expect(localStorage.getItem("catchem.watchlist")).toBeNull();
    expect(localStorage.getItem("catchem.onboarding.completed")).toBeNull();
    // Non-Catchem keys preserved.
    expect(localStorage.getItem("other-app.session")).toBe("should-survive");
    expect(localStorage.getItem("third-party.flag")).toBe("yes");
    // Reload triggered exactly once.
    expect(reload).toHaveBeenCalledTimes(1);
  });

  it("Esc closes the modal without wiping or reloading", async () => {
    localStorage.setItem("catchem.theme", "dark");
    const reload = installReloadSpy();

    renderSettings();
    const openBtn = await screen.findByTestId("reset-preferences-open-btn");
    await act(async () => {
      fireEvent.click(openBtn);
    });
    expect(screen.getByTestId("reset-preferences-modal-card")).toBeInTheDocument();

    await act(async () => {
      fireEvent.keyDown(document, { key: "Escape" });
    });

    expect(screen.queryByTestId("reset-preferences-modal-card")).toBeNull();
    expect(localStorage.getItem("catchem.theme")).toBe("dark");
    expect(reload).not.toHaveBeenCalled();
  });

  it("modal carries aria-modal=true and a labelled dialog role", async () => {
    renderSettings();
    const openBtn = await screen.findByTestId("reset-preferences-open-btn");
    await act(async () => {
      fireEvent.click(openBtn);
    });
    const dialog = screen.getByTestId("reset-preferences-modal-card");
    expect(dialog).toHaveAttribute("role", "dialog");
    expect(dialog).toHaveAttribute("aria-modal", "true");
    // aria-labelledby points at the headline so screen readers announce
    // "Are you sure?" on open.
    const labelledBy = dialog.getAttribute("aria-labelledby");
    expect(labelledBy).toBeTruthy();
    expect(document.getElementById(labelledBy as string)).toHaveTextContent(
      /Are you sure\?/,
    );
  });
});
