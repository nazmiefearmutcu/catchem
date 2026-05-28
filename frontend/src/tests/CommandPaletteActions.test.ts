import { describe, it, expect, beforeEach, vi } from "vitest";
import { QueryClient } from "@tanstack/react-query";
import {
  buildActions,
  fuzzyScore,
  fuzzyScoreWithAliases,
  loadRecent,
  pushRecent,
  tagAction,
  tagNav,
  untag,
  OPEN_SHORTCUT_OVERLAY_EVENT,
  type ActionGroup,
} from "@/components/CommandPalette";
import { ONBOARDING_STORAGE_KEY } from "@/components/OnboardingModal";

/**
 * v23 (task #84) — pin the imperative action surface added on top of
 * the v19 fuzzy/nav palette. We exercise:
 *   - The discriminated action list (group + aliases + run callback)
 *   - Fuzzy scoring across aliases (e.g. "dark" → "Toggle theme")
 *   - Recent-store tag format + migration from v19 bare-path entries
 *   - Each action's side effect (theme toggle, navigate, qc.invalidate,
 *     news poll, qc.clear, localStorage removeItem, dispatched event,
 *     window.location.reload)
 */

vi.mock("@/lib/api", () => ({
  api: {
    newsPollNow: vi.fn(async () => ({ ok: true, ingested: 0 })),
  },
}));

import { api } from "@/lib/api";

const apiMock = api as unknown as { newsPollNow: ReturnType<typeof vi.fn> };

const RECENT_KEY = "catchem.palette.recent";

// jsdom in this project ships without a full Storage implementation, so we
// install a minimal shim before each test (same pattern as
// desktopAlerts.test.ts).
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

beforeEach(() => {
  installLocalStorage();
  vi.clearAllMocks();
});

function makeDeps() {
  const themeToggle = vi.fn();
  const navigate = vi.fn();
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const invalidateSpy = vi.spyOn(qc, "invalidateQueries");
  const clearSpy = vi.spyOn(qc, "clear");
  return {
    deps: { themeToggle, themeLabel: "dark", navigate, qc },
    themeToggle,
    navigate,
    qc,
    invalidateSpy,
    clearSpy,
  };
}

describe("buildActions — discriminated list shape", () => {
  it("exposes every contracted action with stable ids and a group", () => {
    const { deps } = makeDeps();
    const list = buildActions(deps);
    const ids = list.map((a) => a.id);
    expect(ids).toEqual([
      "toggle-theme",
      "run-benchmark",
      "poll-news-now",
      "clear-query-caches",
      "open-settings-deepseek",
      "show-keyboard-shortcuts",
      "open-new-window",
      "restart-onboarding",
    ]);
    for (const a of list) {
      expect(a.kind).toBe("action");
      expect(typeof a.label).toBe("string");
      expect(typeof a.run).toBe("function");
      expect(["Settings", "Data", "View", "System"] as ActionGroup[]).toContain(a.group);
    }
  });

  it("places groups consistently per action", () => {
    const { deps } = makeDeps();
    const list = buildActions(deps);
    const byId = Object.fromEntries(list.map((a) => [a.id, a.group] as const));
    expect(byId["toggle-theme"]).toBe("View");
    expect(byId["run-benchmark"]).toBe("Data");
    expect(byId["poll-news-now"]).toBe("Data");
    expect(byId["clear-query-caches"]).toBe("System");
    expect(byId["open-settings-deepseek"]).toBe("Settings");
    expect(byId["show-keyboard-shortcuts"]).toBe("View");
    expect(byId["restart-onboarding"]).toBe("Settings");
    expect(byId["open-new-window"]).toBe("View");
  });

  it("toggle-theme label reflects the current theme", () => {
    const dark = buildActions({ ...makeDeps().deps, themeLabel: "dark" });
    const light = buildActions({ ...makeDeps().deps, themeLabel: "light" });
    expect(dark.find((a) => a.id === "toggle-theme")?.label).toMatch(/currently dark/);
    expect(light.find((a) => a.id === "toggle-theme")?.label).toMatch(/currently light/);
  });
});

describe("action side effects", () => {
  it("toggle-theme calls themeToggle", () => {
    const { deps, themeToggle } = makeDeps();
    buildActions(deps).find((a) => a.id === "toggle-theme")!.run();
    expect(themeToggle).toHaveBeenCalledTimes(1);
  });

  it("run-benchmark invalidates bench + bench-hist and navigates", () => {
    const { deps, invalidateSpy, navigate } = makeDeps();
    buildActions(deps).find((a) => a.id === "run-benchmark")!.run();
    const keys = invalidateSpy.mock.calls.map((c) => (c[0] as { queryKey: unknown[] }).queryKey[0]);
    expect(keys).toContain("bench");
    expect(keys).toContain("bench-hist");
    expect(navigate).toHaveBeenCalledWith("/benchmark");
  });

  it("poll-news-now hits api.newsPollNow + invalidates news-status + feed-list", async () => {
    const { deps, invalidateSpy } = makeDeps();
    buildActions(deps).find((a) => a.id === "poll-news-now")!.run();
    // Await microtasks so the .finally() runs.
    await Promise.resolve();
    await Promise.resolve();
    expect(apiMock.newsPollNow).toHaveBeenCalledTimes(1);
    const keys = invalidateSpy.mock.calls.map((c) => (c[0] as { queryKey: unknown[] }).queryKey[0]);
    expect(keys).toContain("news-status");
    expect(keys).toContain("feed-list");
  });

  it("poll-news-now still invalidates caches even when the request fails", async () => {
    apiMock.newsPollNow.mockRejectedValueOnce(new Error("nope"));
    const { deps, invalidateSpy } = makeDeps();
    buildActions(deps).find((a) => a.id === "poll-news-now")!.run();
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
    const keys = invalidateSpy.mock.calls.map((c) => (c[0] as { queryKey: unknown[] }).queryKey[0]);
    expect(keys).toContain("news-status");
    expect(keys).toContain("feed-list");
  });

  it("clear-query-caches calls qc.clear() but leaves localStorage alone", () => {
    localStorage.setItem("catchem.theme", "dark");
    localStorage.setItem("catchem.palette.recent", JSON.stringify([tagNav("/feed")]));
    const { deps, clearSpy } = makeDeps();
    buildActions(deps).find((a) => a.id === "clear-query-caches")!.run();
    expect(clearSpy).toHaveBeenCalledTimes(1);
    expect(localStorage.getItem("catchem.theme")).toBe("dark");
    expect(localStorage.getItem("catchem.palette.recent")).toBe(
      JSON.stringify([tagNav("/feed")]),
    );
  });

  it("open-settings-deepseek navigates to /settings#deepseek", () => {
    const { deps, navigate } = makeDeps();
    buildActions(deps).find((a) => a.id === "open-settings-deepseek")!.run();
    expect(navigate).toHaveBeenCalledWith("/settings#deepseek");
  });

  it("show-keyboard-shortcuts dispatches the overlay event", () => {
    const seen: Event[] = [];
    const handler = (e: Event) => seen.push(e);
    window.addEventListener(OPEN_SHORTCUT_OVERLAY_EVENT, handler);
    const { deps } = makeDeps();
    buildActions(deps).find((a) => a.id === "show-keyboard-shortcuts")!.run();
    window.removeEventListener(OPEN_SHORTCUT_OVERLAY_EVENT, handler);
    expect(seen.length).toBe(1);
    expect(seen[0].type).toBe(OPEN_SHORTCUT_OVERLAY_EVENT);
  });

  it("open-new-window dispatches the catchem:menu new_window event", () => {
    const seen: CustomEvent[] = [];
    const handler = (e: Event) => seen.push(e as CustomEvent);
    window.addEventListener("catchem:menu", handler);
    const { deps } = makeDeps();
    buildActions(deps).find((a) => a.id === "open-new-window")!.run();
    window.removeEventListener("catchem:menu", handler);
    expect(seen.length).toBe(1);
    expect(seen[0].type).toBe("catchem:menu");
    expect(seen[0].detail).toBe("new_window");
  });

  it("restart-onboarding clears the onboarding flag and reloads", () => {
    localStorage.setItem(ONBOARDING_STORAGE_KEY, "true");
    // jsdom's `window.location.reload` is not implemented; stub it.
    const reload = vi.fn();
    Object.defineProperty(window, "location", {
      configurable: true,
      value: { ...window.location, reload },
    });
    const { deps } = makeDeps();
    buildActions(deps).find((a) => a.id === "restart-onboarding")!.run();
    expect(localStorage.getItem(ONBOARDING_STORAGE_KEY)).toBeNull();
    expect(reload).toHaveBeenCalledTimes(1);
  });
});

describe("fuzzy scoring honors aliases", () => {
  it('"dark" matches "Toggle theme" via the alias list', () => {
    const direct = fuzzyScore("dark", "Toggle theme (currently dark)");
    const withAliases = fuzzyScoreWithAliases("dark", "Toggle theme", ["dark", "light"]);
    expect(withAliases).toBeGreaterThan(direct === -1 ? 0 : direct);
    expect(withAliases).toBeGreaterThanOrEqual(50);
  });

  it("alias miss falls back to label score (no false positives)", () => {
    const score = fuzzyScoreWithAliases("xyz_nope", "Toggle theme", ["dark", "light"]);
    expect(score).toBe(-1);
  });

  it("alias score wins even when the label does not match the prefix", () => {
    const score = fuzzyScoreWithAliases("rss", "Poll news now", ["refresh", "rss"]);
    expect(score).toBeGreaterThanOrEqual(50);
  });
});

describe("recent-commands store — tag encoding + migration", () => {
  it("tags nav and action ids unambiguously", () => {
    expect(tagNav("/feed")).toBe("nav:/feed");
    expect(tagAction("toggle-theme")).toBe("action:toggle-theme");
  });

  it("untag round-trips both kinds and rejects garbage", () => {
    expect(untag("nav:/feed")).toEqual({ kind: "nav", value: "/feed" });
    expect(untag("action:toggle-theme")).toEqual({
      kind: "action",
      value: "toggle-theme",
    });
    expect(untag("garbage")).toBeNull();
  });

  it("loadRecent migrates v19 bare-path entries to tagged form", () => {
    localStorage.setItem(RECENT_KEY, JSON.stringify(["/feed", "/ops"]));
    expect(loadRecent()).toEqual([tagNav("/feed"), tagNav("/ops")]);
  });

  it("loadRecent accepts already-tagged entries unchanged", () => {
    localStorage.setItem(
      RECENT_KEY,
      JSON.stringify([tagAction("toggle-theme"), tagNav("/feed")]),
    );
    expect(loadRecent()).toEqual([tagAction("toggle-theme"), tagNav("/feed")]);
  });

  it("loadRecent drops malformed entries and caps at 5", () => {
    localStorage.setItem(
      RECENT_KEY,
      JSON.stringify([
        "nav:/feed",
        42,
        "garbage",
        "action:run-benchmark",
        "/symbols",
        "nav:/ops",
        "nav:/scan",
        "nav:/replay",
      ]),
    );
    const r = loadRecent();
    expect(r.length).toBeLessThanOrEqual(5);
    expect(r).toContain("nav:/feed");
    expect(r).toContain("action:run-benchmark");
    expect(r).toContain(tagNav("/symbols")); // migrated from bare path
    // garbage / numbers are dropped.
    expect(r.every((s) => typeof s === "string")).toBe(true);
  });

  it("pushRecent dedupes + bounds to 5", () => {
    let r: string[] = [];
    r = pushRecent(tagNav("/feed"), r);
    r = pushRecent(tagAction("toggle-theme"), r);
    r = pushRecent(tagNav("/feed"), r); // promote /feed back to head
    expect(r).toEqual([tagNav("/feed"), tagAction("toggle-theme")]);
    r = pushRecent(tagNav("/ops"), r);
    r = pushRecent(tagNav("/scan"), r);
    r = pushRecent(tagNav("/replay"), r);
    r = pushRecent(tagAction("run-benchmark"), r);
    expect(r.length).toBe(5);
    expect(r[0]).toBe(tagAction("run-benchmark"));
  });

  it("loadRecent gracefully recovers from corrupt JSON", () => {
    localStorage.setItem(RECENT_KEY, "{not-json");
    expect(loadRecent()).toEqual([]);
  });
});
