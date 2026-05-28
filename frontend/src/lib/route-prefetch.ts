/**
 * Route prefetch — preload the lazy-loaded chunk for a route on hover/focus.
 *
 * Rationale: the app uses `React.lazy(() => import("..."))` per route, so the
 * first navigation to any page fetches its JS chunk over the network. With
 * code-split chunks at ~5-65 KB this is fast but noticeable on slow networks.
 * Triggering the same dynamic `import()` on mouseEnter/focus means by the time
 * the user clicks, the chunk is already in the module cache → click-to-paint
 * has zero network wait.
 *
 * Dedupe via a Set so multiple hovers don't refire fetches; the browser's HTTP
 * cache would handle that anyway, but skipping the JS function call is cheaper.
 *
 * Reduced-motion / data-saver respect: if the user has Save-Data hinted, skip
 * prefetch entirely to honour low-bandwidth intent.
 */

const PREFETCH: Record<string, () => Promise<unknown>> = {
  "/": () => import("@/features/overview/OverviewPage"),
  "/feed": () => import("@/features/feed/FeedPage"),
  "/replay": () => import("@/features/replay-upload/ReplayUploadPage"),
  "/map": () => import("@/features/market-map/MarketMapPage"),
  "/analysis": () => import("@/features/market-map/MarketMapPage"),
  "/symbols": () => import("@/features/symbols/SymbolsPage"),
  "/tags": () => import("@/features/tags/TagsPage"),
  "/benchmark": () => import("@/features/benchmark/BenchmarkPage"),
  "/backtest": () => import("@/features/backtest/BacktestPage"),
  "/reviews": () => import("@/features/reviews/ReviewsComparePage"),
  "/scan": () => import("@/features/quant/QuantScanPage"),
  "/model-controls": () => import("@/features/model-controls/ModelControlsPage"),
  "/ops": () => import("@/features/ops/OpsPage"),
  "/logs": () => import("@/features/logs/LogsPage"),
  "/sources": () => import("@/features/sources/SourcesPage"),
  "/settings": () => import("@/features/settings/SettingsPage"),
  "/help": () => import("@/features/help/HelpPage"),
};

const prefetched = new Set<string>();

function isDataSaver(): boolean {
  // Save-Data hint via Network Information API (Chrome/Edge). Tauri's WebKit
  // doesn't expose this, so the check returns false there — prefetch always
  // runs in the native app (no real bandwidth concern on localhost).
  type ConnectionWithSaveData = { saveData?: boolean };
  type NavigatorWithConnection = Navigator & { connection?: ConnectionWithSaveData };
  const nav = (typeof navigator !== "undefined" ? navigator : null) as NavigatorWithConnection | null;
  return !!nav?.connection?.saveData;
}

export function prefetchRoute(path: string): void {
  if (prefetched.has(path)) return;
  if (isDataSaver()) return;
  const fn = PREFETCH[path];
  if (!fn) return;
  prefetched.add(path);
  // Fire-and-forget; failures are non-critical (the lazy() call on click
  // will retry the import naturally).
  fn().catch(() => {
    // Allow retry on subsequent hover
    prefetched.delete(path);
  });
}

// Test helpers
export function _resetPrefetchedForTests(): void {
  prefetched.clear();
}

export function _knownPrefetchPaths(): string[] {
  return Object.keys(PREFETCH);
}
