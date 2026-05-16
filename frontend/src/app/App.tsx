import { Routes, Route, Navigate } from "react-router-dom";
import { Suspense, lazy } from "react";
import { Shell } from "@/layout/Shell";
import { Skeleton } from "@/components/Skeleton";

// Route-based code splitting — keep the initial bundle small.
const Overview = lazy(() => import("@/features/overview/OverviewPage").then(m => ({ default: m.OverviewPage })));
const Feed = lazy(() => import("@/features/feed/FeedPage").then(m => ({ default: m.FeedPage })));
const MarketMap = lazy(() => import("@/features/market-map/MarketMapPage").then(m => ({ default: m.MarketMapPage })));
const Symbols = lazy(() => import("@/features/symbols/SymbolsPage").then(m => ({ default: m.SymbolsPage })));
const SymbolDetail = lazy(() => import("@/features/symbols/SymbolDetailPage").then(m => ({ default: m.SymbolDetailPage })));
const Benchmark = lazy(() => import("@/features/benchmark/BenchmarkPage").then(m => ({ default: m.BenchmarkPage })));
const Ops = lazy(() => import("@/features/ops/OpsPage").then(m => ({ default: m.OpsPage })));
const Settings = lazy(() => import("@/features/settings/SettingsPage").then(m => ({ default: m.SettingsPage })));

function RouteFallback() {
  return (
    <div className="grid gap-3" aria-busy="true">
      <Skeleton className="h-6 w-48" />
      <Skeleton className="h-24 w-full" />
      <Skeleton className="h-24 w-full" />
    </div>
  );
}

export function App() {
  return (
    <Routes>
      <Route element={<Shell />}>
        <Route index element={<Suspense fallback={<RouteFallback />}><Overview /></Suspense>} />
        <Route path="feed" element={<Suspense fallback={<RouteFallback />}><Feed /></Suspense>} />
        <Route path="feed/:captureId" element={<Suspense fallback={<RouteFallback />}><Feed /></Suspense>} />
        <Route path="map" element={<Suspense fallback={<RouteFallback />}><MarketMap /></Suspense>} />
        <Route path="symbols" element={<Suspense fallback={<RouteFallback />}><Symbols /></Suspense>} />
        <Route path="symbols/:symbol" element={<Suspense fallback={<RouteFallback />}><SymbolDetail /></Suspense>} />
        <Route path="benchmark" element={<Suspense fallback={<RouteFallback />}><Benchmark /></Suspense>} />
        <Route path="ops" element={<Suspense fallback={<RouteFallback />}><Ops /></Suspense>} />
        <Route path="settings" element={<Suspense fallback={<RouteFallback />}><Settings /></Suspense>} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  );
}
