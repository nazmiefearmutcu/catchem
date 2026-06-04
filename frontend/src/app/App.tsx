import { Routes, Route, Navigate, Link } from "react-router-dom";
import { lazy } from "react";
import { Shell } from "@/layout/Shell";
import { AppErrorBoundary } from "@/components/AppErrorBoundary";

// Route-based code splitting — keep the initial bundle small. Each page is
// emitted as its own async chunk; the single <Suspense> lives in Shell.tsx
// inside the `animate-page-enter` wrapper so the fallback skeleton fades in
// alongside route content instead of snapping into place.
const Overview = lazy(() => import("@/features/overview/OverviewPage").then(m => ({ default: m.OverviewPage })));
const Feed = lazy(() => import("@/features/feed/FeedPage").then(m => ({ default: m.FeedPage })));
const MarketMap = lazy(() => import("@/features/market-map/MarketMapPage").then(m => ({ default: m.MarketMapPage })));
const Symbols = lazy(() => import("@/features/symbols/SymbolsPage").then(m => ({ default: m.SymbolsPage })));
const SymbolDetail = lazy(() => import("@/features/symbols/SymbolDetailPage").then(m => ({ default: m.SymbolDetailPage })));
const Portfolio = lazy(() => import("@/features/portfolio/PortfolioPage").then(m => ({ default: m.PortfolioPage })));
const Tags = lazy(() => import("@/features/tags/TagsPage").then(m => ({ default: m.TagsPage })));
const Benchmark = lazy(() => import("@/features/benchmark/BenchmarkPage").then(m => ({ default: m.BenchmarkPage })));
const Backtest = lazy(() => import("@/features/backtest/BacktestPage").then(m => ({ default: m.BacktestPage })));
const Ops = lazy(() => import("@/features/ops/OpsPage").then(m => ({ default: m.OpsPage })));
const Settings = lazy(() => import("@/features/settings/SettingsPage").then(m => ({ default: m.SettingsPage })));
const ReplayUpload = lazy(() => import("@/features/replay-upload/ReplayUploadPage").then(m => ({ default: m.ReplayUploadPage })));
const ModelControls = lazy(() => import("@/features/model-controls/ModelControlsPage").then(m => ({ default: m.ModelControlsPage })));
const Reviews = lazy(() => import("@/features/reviews/ReviewsComparePage").then(m => ({ default: m.ReviewsComparePage })));
const QuantScan = lazy(() => import("@/features/quant/QuantScanPage").then(m => ({ default: m.QuantScanPage })));
const Logs = lazy(() => import("@/features/logs/LogsPage").then(m => ({ default: m.LogsPage })));
const Sources = lazy(() => import("@/features/sources/SourcesPage").then(m => ({ default: m.SourcesPage })));
const Help = lazy(() => import("@/features/help/HelpPage").then(m => ({ default: m.HelpPage })));

export function App() {
  return (
    // Top-level safety net — catches Shell crashes that the per-route
    // RouteErrorBoundary cannot (since the boundary lives inside Shell).
    <AppErrorBoundary>
      <Routes>
        <Route element={<Shell />}>
          <Route index element={<Overview />} />
          <Route path="feed" element={<Feed />} />
          <Route path="feed/:captureId" element={<Feed />} />
          <Route path="map" element={<MarketMap />} />
          <Route path="symbols" element={<Symbols />} />
          <Route path="symbols/:symbol" element={<SymbolDetail />} />
          <Route path="portfolio" element={<Portfolio />} />
          <Route path="tags" element={<Tags />} />
          <Route path="benchmark" element={<Benchmark />} />
          <Route path="backtest" element={<Backtest />} />
          <Route path="ops" element={<Ops />} />
          <Route path="settings" element={<Settings />} />
          {/* Catchem tabs */}
          <Route path="replay" element={<ReplayUpload />} />
          <Route path="model-controls" element={<ModelControls />} />
          <Route path="reviews" element={<Reviews />} />
          <Route path="scan" element={<QuantScan />} />
          <Route path="logs" element={<Logs />} />
          <Route path="sources" element={<Sources />} />
          <Route path="help" element={<Help />} />
          {/* Analysis tab is a portmanteau: route to /map by default */}
          <Route path="analysis" element={<Navigate to="/map" replace />} />
          <Route
            path="*"
            element={
              <section className="min-h-[60vh] flex items-center justify-center p-6">
                <div className="max-w-md w-full rounded-xl border border-[color:var(--border)] bg-[color:var(--bg-elev)] p-6">
                  <h1 className="text-lg font-semibold">Page not found</h1>
                  <p className="mt-2 text-sm text-[color:var(--fg-dim)]">
                    Bu rota bulunamadı. Menüden bir sekmeye dönün veya başlangıç
                    ekranına geri gidin.
                  </p>
                  <div className="mt-4 flex gap-2">
                    <Link to="/" className="btn">
                      Overview
                    </Link>
                  </div>
                </div>
              </section>
            }
          />
        </Route>
      </Routes>
    </AppErrorBoundary>
  );
}
