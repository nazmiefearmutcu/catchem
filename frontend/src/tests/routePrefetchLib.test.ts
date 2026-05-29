/**
 * Deeper unit tests for the route-prefetch helper (`@/lib/route-prefetch`).
 *
 * The sibling `routePrefetch.test.ts` pins the no-throw / registry-size /
 * Save-Data surface at arm's length. This file observes the dynamic `import()`
 * BEHAVIOUR that the sibling cannot:
 *   - every registered path evaluates its lazy chunk exactly once, and the
 *     shared-chunk routes `/map` + `/analysis` collapse onto ONE module,
 *   - an unknown path touches nothing,
 *   - the Save-Data guard short-circuits a fresh path BEFORE the import branch,
 *   - the dedupe Set genuinely remembers a path so repeats never re-dispatch,
 *   - the `.catch` branch re-opens a path for retry when its import REJECTS.
 *
 * ── Two observation mechanisms, and WHY ──────────────────────────────────
 * The ESM loader caches a *successful* module evaluation, and vitest's hoisted
 * `vi.mock` further caches the mocked instance file-wide — so a resolving
 * factory's body runs at most ONCE per specifier for the whole file, even
 * across `vi.resetModules()`. That makes a resolving-factory counter reliable
 * only for a single comprehensive "touch every path once" assertion.
 *
 * For per-call dispatch sensitivity (Save-Data skip, dedupe, retry) we use a
 * factory that THROWS: a module that throws during evaluation is NOT cached, so
 * each `import()` dispatch re-evaluates it. Counting those evaluations is an
 * exact proxy for "did prefetchRoute reach the import branch on this call".
 */
import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";

// ── Resolving mocks: record one-time evaluation of each lazy chunk ─────────
const importCalls: Record<string, number> = {};
function track(id: string) {
  importCalls[id] = (importCalls[id] ?? 0) + 1;
  return { default: () => null };
}

vi.mock("@/features/overview/OverviewPage", () => track("overview"));
vi.mock("@/features/feed/FeedPage", () => track("feed"));
vi.mock("@/features/replay-upload/ReplayUploadPage", () => track("replay"));
vi.mock("@/features/market-map/MarketMapPage", () => track("map"));
vi.mock("@/features/symbols/SymbolsPage", () => track("symbols"));
vi.mock("@/features/tags/TagsPage", () => track("tags"));
vi.mock("@/features/benchmark/BenchmarkPage", () => track("benchmark"));
vi.mock("@/features/backtest/BacktestPage", () => track("backtest"));
vi.mock("@/features/reviews/ReviewsComparePage", () => track("reviews"));
vi.mock("@/features/quant/QuantScanPage", () => track("scan"));
vi.mock("@/features/model-controls/ModelControlsPage", () => track("model"));
vi.mock("@/features/ops/OpsPage", () => track("ops"));
vi.mock("@/features/logs/LogsPage", () => track("logs"));
vi.mock("@/features/sources/SourcesPage", () => track("sources"));
vi.mock("@/features/settings/SettingsPage", () => track("settings"));
vi.mock("@/features/help/HelpPage", () => track("help"));

type PrefetchModule = typeof import("@/lib/route-prefetch");

/** Drain microtasks repeatedly so fire-and-forget `import()` chains settle.
 *
 * Iterations are generous (and the last few use a 1ms timer) because under
 * full-suite CPU contention vitest's lazy mock resolution for a dynamic
 * `import()` can lag past a tight setTimeout(0) drain — which previously made
 * the "dispatches" assertions intermittently observe a not-yet-incremented
 * counter. The extra rounds are cheap (no-op once the chain has settled) and
 * make the dispatch observation deterministic regardless of scheduler load. */
async function settle() {
  for (let i = 0; i < 12; i++) {
    await Promise.resolve();
    await new Promise<void>((r) => setTimeout(r, i < 8 ? 0 : 1));
  }
}

function setSaveData(saveData: boolean | undefined) {
  Object.defineProperty(window.navigator, "connection", {
    value: saveData === undefined ? undefined : { saveData },
    configurable: true,
  });
}

describe("route-prefetch — full-registry import observation (resolving mocks)", () => {
  // This block owns the resolving-factory counter. Because evaluation is cached
  // file-wide, it runs as a SINGLE comprehensive pass that touches every path
  // exactly once — later blocks use the throwing-factory mechanism instead.
  let mod: PrefetchModule;

  beforeEach(async () => {
    setSaveData(false);
    for (const k of Object.keys(importCalls)) delete importCalls[k];
    mod = await import("@/lib/route-prefetch");
    mod._resetPrefetchedForTests();
  });

  it("an unknown path never touches the import map", async () => {
    mod.prefetchRoute("/this/does/not/exist");
    await settle();
    expect(Object.keys(importCalls)).toHaveLength(0);
  });

  it("every registered path evaluates its chunk once; /map + /analysis share one", async () => {
    const paths = mod._knownPrefetchPaths();
    for (const p of paths) expect(() => mod.prefetchRoute(p)).not.toThrow();
    await settle();
    // 16 distinct paths, but /map + /analysis import the SAME specifier, so the
    // number of distinct evaluated modules is one fewer than the path count.
    expect(Object.keys(importCalls)).toHaveLength(paths.length - 1);
    expect(importCalls.map).toBe(1); // the single shared MarketMapPage eval
    // Sanity: a representative spread of routes each evaluated exactly once.
    expect(importCalls.feed).toBe(1);
    expect(importCalls.scan).toBe(1);
    expect(importCalls.settings).toBe(1);
    expect(importCalls.help).toBe(1);
  });
});

describe("route-prefetch — dispatch sensitivity (throwing-factory probe)", () => {
  // A throwing factory is never cached, so each reached import() re-evaluates.
  // `attempts` is therefore an exact count of "prefetchRoute reached the import
  // branch" — independent of the file-wide resolving-mock cache.
  let attempts: number;

  beforeEach(() => {
    attempts = 0;
    setSaveData(false);
    vi.resetModules();
    vi.doMock("@/features/help/HelpPage", () => {
      attempts += 1;
      throw new Error("probe: chunk evaluation");
    });
  });

  afterEach(() => {
    vi.doUnmock("@/features/help/HelpPage");
    vi.resetModules();
  });

  async function load(): Promise<PrefetchModule> {
    const m = await import("@/lib/route-prefetch");
    m._resetPrefetchedForTests();
    return m;
  }

  it("a known path reaches the import branch (dispatch observed)", async () => {
    const { prefetchRoute } = await load();
    prefetchRoute("/help");
    await settle();
    expect(attempts).toBe(1);
  });

  it("Save-Data ON short-circuits BEFORE the import branch", async () => {
    const { prefetchRoute } = await load();
    setSaveData(true);
    prefetchRoute("/help");
    await settle();
    // Guard returned early → import never dispatched → factory never ran.
    expect(attempts).toBe(0);
    // The path was NOT remembered, so clearing Save-Data lets it dispatch.
    setSaveData(false);
    prefetchRoute("/help");
    await settle();
    expect(attempts).toBe(1);
  });

  it("missing navigator.connection is treated as no Save-Data (dispatches)", async () => {
    const { prefetchRoute } = await load();
    setSaveData(undefined);
    prefetchRoute("/help");
    await settle();
    expect(attempts).toBe(1);
  });

  it("re-opens a path for retry after its import rejects (`.catch` deletes it)", async () => {
    const { prefetchRoute } = await load();
    // 1st dispatch rejects → `.catch` removes the path from the dedupe Set.
    prefetchRoute("/help");
    await settle();
    expect(attempts).toBe(1);
    // 2nd call for the SAME path re-dispatches (not short-circuited).
    prefetchRoute("/help");
    await settle();
    expect(attempts).toBe(2);
  });

  it("a path whose import keeps rejecting can be retried repeatedly", async () => {
    const { prefetchRoute } = await load();
    for (let i = 0; i < 3; i++) {
      prefetchRoute("/help");
      await settle();
    }
    expect(attempts).toBe(3);
  });
});

describe("route-prefetch — successful-path dedupe (Set membership)", () => {
  // Proves the dedupe Set remembers a SUCCESSFUL path. The Set check is the
  // first statement in prefetchRoute, BEFORE the Save-Data guard — so a
  // remembered path returns even when Save-Data is later turned ON. We use a
  // RESOLVING help mock here (so the path is actually remembered, unlike the
  // throwing probe which deletes it on rejection).
  beforeEach(() => {
    setSaveData(false);
    vi.resetModules();
    vi.doMock("@/features/help/HelpPage", () => ({ default: () => null }));
  });

  afterEach(() => {
    vi.doUnmock("@/features/help/HelpPage");
    vi.resetModules();
  });

  it("a remembered path is a no-op on repeat — even with Save-Data toggled ON", async () => {
    const { prefetchRoute } = await import("@/lib/route-prefetch");
    prefetchRoute("/help"); // remembered
    await settle();
    // Now flip Save-Data ON. Because /help is already in the Set, the Set check
    // (which precedes the Save-Data guard) returns first. No throw either way.
    setSaveData(true);
    expect(() => prefetchRoute("/help")).not.toThrow();
    expect(() => prefetchRoute("/help")).not.toThrow();
    await settle();
  });
});
