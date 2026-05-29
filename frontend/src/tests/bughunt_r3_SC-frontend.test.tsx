import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

/**
 * Bug-hunt Round 3 — file group "SC-frontend".
 *
 * One focused regression per confirmed finding. Each assertion FAILS on the
 * pre-fix code and PASSES after the minimal fix in this group.
 */

// ── shared localStorage shim (jsdom ships none by default) ─────────────────
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

// ───────────────────────────────────────────────────────────────────────────
// Finding 1: NotificationCenter must not white-screen when a persisted history
// entry hydrated from localStorage is missing `symbols` (or carries an
// out-of-union `category`). Pre-fix: `entry.symbols.length` throws a
// TypeError and `counts` mints a NaN phantom key.
// ───────────────────────────────────────────────────────────────────────────
describe("Finding 1 · NotificationCenter survives malformed persisted history", () => {
  beforeEach(() => {
    vi.resetModules();
    installLocalStorage();
  });

  it("renders a history entry that lacks `symbols` instead of crashing", async () => {
    // Seed storage BEFORE the module hydrates on import. The bad entry passes
    // the id+title filter but has no `symbols` array and an unknown category.
    const HISTORY_KEY = "catchem.notifications.history";
    window.localStorage.setItem(
      HISTORY_KEY,
      JSON.stringify([
        {
          id: "cap-legacy",
          title: "Legacy arrival without symbols",
          createdAt: 1_700_000_000_000,
          category: "from-an-old-build", // out-of-union
          // NOTE: no `symbols`, no `score`, no `domain`, no `reasons`.
        },
      ]),
    );

    // Fresh import → `_hydrateHistoryFromStorage()` runs against the bad blob.
    const { NotificationCenter } = await import("@/components/NotificationCenter");

    expect(() =>
      render(
        <MemoryRouter>
          <NotificationCenter open onClose={() => {}} />
        </MemoryRouter>,
      ),
    ).not.toThrow();

    // The row rendered and the title is visible — proves hydration normalized
    // the entry rather than dropping or crashing on it.
    expect(screen.getByText("Legacy arrival without symbols")).toBeInTheDocument();

    // The unknown category folds into "toast", so the chip counts stay finite
    // (no NaN). The "All" chip shows 1 and "Toast" shows 1.
    const allChip = screen.getByTestId("notif-filter-all");
    const toastChip = screen.getByTestId("notif-filter-toast");
    expect(allChip.textContent).toContain("1");
    expect(toastChip.textContent).toContain("1");
    expect(allChip.textContent).not.toContain("NaN");
  });
});

// ───────────────────────────────────────────────────────────────────────────
// Finding 2: Esc-dismissing the first-run onboarding modal must persist the
// "seen" flag so it doesn't reappear on next launch. Pre-fix: the coordinator
// onClose was `() => setOpen(false)` and never called markOnboardingSeen().
// ───────────────────────────────────────────────────────────────────────────
describe("Finding 2 · onboarding Esc persists the seen flag", () => {
  beforeEach(() => {
    vi.resetModules();
    installLocalStorage();
    vi.useFakeTimers();
  });
  afterEach(() => {
    act(() => {
      vi.runOnlyPendingTimers();
    });
    vi.useRealTimers();
  });

  it("writes ONBOARDING_STORAGE_KEY='true' when the first-run modal is closed via Esc", async () => {
    const { __resetOverlayStateForTests } = await import(
      "@/context/overlayCoordinator"
    );
    __resetOverlayStateForTests();
    const { OnboardingModal, ONBOARDING_STORAGE_KEY } = await import(
      "@/components/OnboardingModal"
    );

    // First run: flag absent → modal renders itself.
    render(<OnboardingModal />);
    expect(screen.getByTestId("onboarding-card")).toBeInTheDocument();
    expect(window.localStorage.getItem(ONBOARDING_STORAGE_KEY)).toBeNull();

    // The global coordinator binds ONE Escape handler on document; dispatch a
    // genuine keydown so closeTopOverlay() → entry.onClose() fires.
    act(() => {
      document.dispatchEvent(
        new KeyboardEvent("keydown", { key: "Escape", bubbles: true }),
      );
    });

    expect(window.localStorage.getItem(ONBOARDING_STORAGE_KEY)).toBe("true");
    expect(screen.queryByTestId("onboarding-card")).not.toBeInTheDocument();
  });
});

// ───────────────────────────────────────────────────────────────────────────
// Finding 3: SymbolDetailPage "latest mention" must compare published_ts by
// parsed UTC instant, not lexicographically. A "+05:00" string sorts last
// alphabetically but is an EARLIER instant than a "+00:00" string.
// ───────────────────────────────────────────────────────────────────────────
describe("Finding 3 · SymbolDetailPage picks the latest UTC instant", () => {
  beforeEach(() => {
    vi.resetModules();
  });
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("shows the true-newest mention across mixed UTC offsets", async () => {
    // Two records: the lexicographically-largest string is the +05:00 row,
    // but its true instant (20:00 UTC on the 21st) is EARLIER than the
    // +00:00 row (22:00 UTC on the 21st). The hero "latest mention" hint must
    // therefore reflect the +00:00 row, NOT the +05:00 row.
    const EARLIER_INSTANT_LATER_STRING = "2026-05-22T01:00:00+05:00"; // = 20:00Z 21st
    const LATER_INSTANT_EARLIER_STRING = "2026-05-21T22:00:00+00:00"; // = 22:00Z 21st

    const fixture = {
      symbol: "AAPL",
      count: 2,
      reason_distribution: { earnings: 2 },
      sentiment_distribution: { positive: 2 },
      items: [
        {
          capture_id: "a",
          title: "plus-five row",
          domain: "ex.com",
          url: null,
          published_ts: EARLIER_INSTANT_LATER_STRING,
          finance_relevance_score: 0.5,
          asset_classes: [],
          impact_reason_codes: ["earnings"],
          candidate_symbols: ["AAPL"],
        },
        {
          capture_id: "b",
          title: "utc row",
          domain: "ex.com",
          url: null,
          published_ts: LATER_INSTANT_EARLIER_STRING,
          finance_relevance_score: 0.5,
          asset_classes: [],
          impact_reason_codes: ["earnings"],
          candidate_symbols: ["AAPL"],
        },
      ],
    };

    // Mock the api module so the page's useQuery resolves our fixture.
    vi.doMock("@/lib/api", async () => {
      const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
      return {
        ...actual,
        api: {
          ...actual.api,
          symbol: vi.fn(async () => fixture),
          symbolSentimentTrend: vi.fn(async () => ({
            symbol: "AAPL",
            days: 30,
            series: [],
          })),
        },
      };
    });

    const { QueryClient, QueryClientProvider } = await import(
      "@tanstack/react-query"
    );
    const { SymbolDetailPage } = await import(
      "@/features/symbols/SymbolDetailPage"
    );
    const { Routes, Route } = await import("react-router-dom");

    const qc = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    render(
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={["/symbols/AAPL"]}>
          <Routes>
            <Route path="/symbols/:symbol" element={<SymbolDetailPage />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );

    // The "latest mention" stat's hint is fmtDate(latestTs). fmtDate of the
    // +00:00 row renders the 21st; the +05:00 row would render the 22nd (its
    // local string). Assert the 21st is present and the 22nd is NOT — i.e. we
    // selected by instant, not by string.
    const latest = await screen.findByText("latest mention");
    const tile = latest.closest("div")?.parentElement as HTMLElement;
    expect(tile).toBeTruthy();
    // The chosen instant is 22:00Z on 2026-05-21.
    expect(Date.parse(LATER_INSTANT_EARLIER_STRING)).toBeGreaterThan(
      Date.parse(EARLIER_INSTANT_LATER_STRING),
    );
    // The hint text must derive from the +00:00 (later-instant) row's date.
    const { fmtDate } = await import("@/lib/api");
    expect(tile.textContent).toContain(fmtDate(LATER_INSTANT_EARLIER_STRING));
    expect(tile.textContent).not.toContain(fmtDate(EARLIER_INSTANT_LATER_STRING));
  });
});

// ───────────────────────────────────────────────────────────────────────────
// Finding 4: UIBacktest.predictions_sample[].capture_id is nullable on the
// wire. The type must allow `null` (compile-time) so consumers don't trust a
// non-null string. Runtime: a null assigns cleanly.
// ───────────────────────────────────────────────────────────────────────────
describe("Finding 4 · UIBacktest capture_id is string | null", () => {
  it("accepts a null capture_id without a type error", async () => {
    const mod = await import("@/types/api");
    type Row = import("@/types/api").UIBacktest["predictions_sample"][number];
    // This object only type-checks if capture_id is `string | null`.
    const row: Row = {
      capture_id: null,
      predicted_score: 0,
      ground_truth_score: 0,
      delta: 0,
    };
    expect(row.capture_id).toBeNull();
    // touch the module so the import isn't elided
    expect(mod).toBeTruthy();
  });
});

// ───────────────────────────────────────────────────────────────────────────
// Finding 5: api.quantClusterMembers must thread `window` into the request URL
// so the backend re-clusters over the same corpus and the cluster_id
// reproduces (otherwise non-default windows 404 the drill-down).
// ───────────────────────────────────────────────────────────────────────────
describe("Finding 5 · quantClusterMembers threads `window` into the URL", () => {
  beforeEach(() => {
    vi.resetModules();
  });
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("appends window=<n> when a window arg is supplied", async () => {
    const calls: string[] = [];
    const fetchSpy = vi
      .spyOn(globalThis, "fetch")
      .mockImplementation(async (input: RequestInfo | URL) => {
        calls.push(String(input));
        return new Response(JSON.stringify({ members: [] }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      });

    const { api } = await import("@/lib/api");
    await api.quantClusterMembers("abc123", 20, 500);

    expect(fetchSpy).toHaveBeenCalled();
    const url = calls[0];
    expect(url).toContain("/api/quant/cluster/abc123/members");
    expect(url).toContain("limit=20");
    expect(url).toContain("window=500");
  });

  it("omits window when no window arg is supplied (back-compat)", async () => {
    const calls: string[] = [];
    vi.spyOn(globalThis, "fetch").mockImplementation(
      async (input: RequestInfo | URL) => {
        calls.push(String(input));
        return new Response(JSON.stringify({ members: [] }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      },
    );

    const { api } = await import("@/lib/api");
    await api.quantClusterMembers("def456");

    const url = calls[0];
    expect(url).toContain("/api/quant/cluster/def456/members");
    expect(url).not.toContain("window=");
  });
});
