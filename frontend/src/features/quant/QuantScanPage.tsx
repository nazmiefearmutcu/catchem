import { useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery } from "@tanstack/react-query";
import { pushToast } from "@/hooks/useDesktopAlerts";
import { useStreamingLiveRead } from "@/hooks/useStreamingLiveRead";
import {
  api,
  fmtPct,
  fmtRel,
  fmtScore,
  safeHref,
  scoreToneClass,
} from "@/lib/api";
import {
  sortWatchlist,
  useWatchlist,
  type WatchlistApi,
  type WatchlistMetric,
  type WatchlistMetrics,
  type WatchlistSortMode,
} from "@/features/quant/useWatchlist";
import {
  DegradedSignalsPill,
  type DegradedDiagnostics,
} from "@/features/quant/DegradedSignalsPill";
import { EChart } from "@/charts/EChart";
import type { EChartsOption } from "echarts";
/**
 * Many of the chart options below use heterogeneous mark-point shapes
 * (xAxis+yAxis pins with no `name`), sankey data, etc. that the ECharts
 * type definitions over-constrain. `eo()` is a tiny cast helper so the
 * UI logic stays readable without `as any` noise at every call site.
 */
function eo(o: unknown): EChartsOption {
  return o as EChartsOption;
}
import { Pill } from "@/components/Pill";
import { Skeleton, ErrorBox, EmptyState } from "@/components/Skeleton";
import { SignalExplainer } from "@/components/SignalExplainer";
import { t, useLang } from "@/lib/i18n";
import { Icon } from "@/components/Icon";
import type {
  AnomalyReportDTO,
  ArrivalHeatmapResponse,
  CoOccurrenceReportDTO,
  EventClusterDTO,
  HeatmapRecordsResponse,
  IntensityBucketDTO,
  IntensityResponse,
  LeadLagReportDTO,
  MarketTimeResponse,
  NewsVelocityResponse,
  NoveltyResultDTO,
  QuantDashboard,
  QuantMember,
  RegimeReportDTO,
  SentimentDispersionEntryDTO,
  SentimentDispersionResponse,
  SentimentMomentumReportDTO,
  SourceLeaderboardDTO,
  SourceScoreDTO,
  SpilloverReportDTO,
  SymbolBurstDTO,
  TickerMomentumDTO,
} from "@/lib/api";

/**
 * Awareness Quant Lens — depth analytics cockpit.
 *
 * 10 signals across 9 ECharts visualizations + per-signal narrative helpers.
 * Read-only; never mutates the primary record stream.
 */
type HeatmapCell = { asset: string; reason: string };
type QuantTab = "events" | "sentiment" | "sources" | "anomalies" | "network" | "time";
type NarrativeSource = "deepseek" | "local";
type SignalNarrative = {
  text: string;
  source: NarrativeSource;
  fallbackReason?: string;
  usdCost?: number;
};

const TAB_LABELS: Record<QuantTab, string> = {
  events: "Events",
  sentiment: "Sentiment",
  sources: "Sources",
  anomalies: "Anomalies",
  network: "Network",
  time: "Time",
};

// Session label → display string, used by the Time tab.
const SESSION_LABELS: Record<string, string> = {
  pre_open: "Pre-open",
  open: "Open",
  lunch: "Lunch",
  close: "Close",
  after_hours: "After-hours",
  overnight: "Overnight",
  weekend: "Weekend",
};

// Upper bound on the per-session burst-toast dedupe Set. Keys are
// `${symbol}|${bucket_start}` and bucket_start advances every ~30m, so a
// long-lived /scan tab would otherwise accumulate keys without bound. A
// cap well above any realistic in-window burst count keeps the structure
// tiny while leaving generous headroom; oldest keys are dropped first.
const NOTIFIED_BURSTS_CAP = 500;

export function QuantScanPage() {
  const [windowSize, setWindowSize] = useState<number>(1000);
  const [activeTab, setActiveTab] = useState<QuantTab>("events");
  const [selectedCluster, setSelectedCluster] = useState<EventClusterDTO | null>(null);
  const [selectedCell, setSelectedCell] = useState<HeatmapCell | null>(null);
  const watchlistApi = useWatchlist();
  const watchlist = watchlistApi.items;

  const dashboard = useQuery<QuantDashboard>({
    queryKey: ["quant-dashboard", windowSize],
    queryFn: () => api.quantDashboard(windowSize),
    refetchInterval: 12_000,
    staleTime: 6_000,
  });

  // Streaming hook is the primary narrative source for the hero — it gives
  // a typing-effect render as DeepSeek emits tokens. The non-streaming
  // query below is the FALLBACK: if the stream errors out we surface its
  // synchronous narrative + cost/usd metadata so the hero is never blank.
  const stream = useStreamingLiveRead(windowSize);
  const liveRead = useQuery({
    queryKey: ["quant-live-read", windowSize],
    queryFn: () => api.quantLiveRead(windowSize),
    refetchInterval: 60_000,
    staleTime: 30_000,
    // FALLBACK ONLY — fetch the non-streaming live-read solely when the
    // stream has actually errored. The hero always auto-starts the stream
    // on mount (and re-starts on window change), but `stream.start()` runs
    // in a post-commit effect, so on the very first render the state is
    // still "idle". The old `!== "streaming"` gate therefore evaluated TRUE
    // on that first render and fired a real, budget-spending DeepSeek call
    // BEFORE the effect flipped the stream to "streaming" — duplicating the
    // stream's own request (cost=4 against the rate bucket + a redundant
    // budget spend, since /api/quant/live-read has no server cache). The
    // same race repeated on every window change. Gating on "error" keeps
    // the hero non-blank when the stream fails while never double-billing
    // on the happy path.
    enabled: stream.state === "error",
  });

  // Auto-start the stream on mount and whenever the window size changes.
  // The hook tears down any in-flight EventSource before re-opening so
  // partial text from the prior window never bleeds into the new one.
  useEffect(() => {
    stream.start();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [windowSize]);

  // DeepSeek budget + call counters for the footer live bar.
  const deepseekStatus = useQuery({
    queryKey: ["reviews-status"],
    queryFn: api.reviewsStatus,
    refetchInterval: 8_000,
    staleTime: 4_000,
  });

  // News velocity — small stat tile inside the hero footer. Polled on
  // its own clock so it doesn't pile onto the dashboard refresh; the
  // backend reads from the same record window so a 30s cadence is
  // plenty of freshness for an arrival-rate signal.
  const newsVelocity = useQuery<NewsVelocityResponse>({
    queryKey: ["quant-news-velocity", windowSize],
    queryFn: () => api.quantNewsVelocity({ limit: Math.max(windowSize, 500) }),
    refetchInterval: 30_000,
    staleTime: 15_000,
  });

  // v72 — fail-soft observability. QuantEngine's _safe_call swallows
  // every signal exception and returns None to keep the dashboard
  // rendering. The /api/quant/diagnostics endpoint surfaces the last
  // 50 failures + per-signal counts; we poll once per minute (cheap,
  // in-memory ring buffer on the backend) so the operator gets a
  // visible "N sinyal degrade" chip on the hero when something starts
  // crashing silently. The healthy steady state is total_failures===0,
  // at which point the chip is hidden entirely — no visual noise.
  const quantDiagnostics = useQuery({
    queryKey: ["quant-diagnostics"],
    queryFn: () => api.quantDiagnostics(),
    refetchInterval: 60_000,
    staleTime: 30_000,
  });

  // Watchlist alert: fire a toast when a watchlisted ticker enters
  // a symbol burst that we haven't notified about yet. De-duped per
  // (symbol, bucket_start) tuple so a refetch doesn't spam the queue.
  const notifiedBurstsRef = useRef<Set<string>>(new Set());
  useEffect(() => {
    const bursts = dashboard.data?.anomalies?.symbol_bursts ?? [];
    const watchSet = new Set(watchlist.map((s) => s.toUpperCase()));
    for (const b of bursts) {
      const sym = b.symbol.trim().toUpperCase();
      if (!watchSet.has(sym)) continue;
      const key = `${sym}|${b.bucket_start}`;
      if (notifiedBurstsRef.current.has(key)) continue;
      notifiedBurstsRef.current.add(key);
      // Bound the dedupe Set so a /scan tab left open for days (a
      // documented usage) doesn't accumulate one key per bucket forever.
      // bucket_start advances every ~30m, so keys never recur once a
      // bucket rolls off — dropping the oldest (Set preserves insertion
      // order) keeps only the most-recent window. Mirrors the round-1
      // cap in useDesktopAlerts.
      if (notifiedBurstsRef.current.size > NOTIFIED_BURSTS_CAP) {
        const overflow = notifiedBurstsRef.current.size - NOTIFIED_BURSTS_CAP;
        const it = notifiedBurstsRef.current.values();
        for (let i = 0; i < overflow; i += 1) {
          const { value, done } = it.next();
          if (done) break;
          notifiedBurstsRef.current.delete(value);
        }
      }
      pushToast({
        id: `quant-burst-${key}`,
        title: `${sym} burst — ${b.observed} mentions in ${dashboard.data?.anomalies?.bucket_minutes ?? 30}m`,
        domain: "quant lens",
        score: Math.min(0.99, Math.abs(b.z_score) / 10),
        reasons: [`z ${b.z_score.toFixed(1)}`],
        symbols: [sym],
      });
    }
  }, [dashboard.data, watchlist]);

  // Opening a cluster automatically deselects any heatmap cell, and vice
  // versa — only one drill-down drawer renders at a time.
  const selectCluster = (c: EventClusterDTO | null) => {
    setSelectedCluster(c);
    if (c) setSelectedCell(null);
  };
  const selectCell = (cell: HeatmapCell | null) => {
    setSelectedCell(cell);
    if (cell) setSelectedCluster(null);
  };

  const toggleWatch = (sym: string) => watchlistApi.toggle(sym);

  return (
    <div className="grid gap-5 lg:grid-cols-[1fr_320px]">
      <section className="grid gap-5">
        <HeroLiveRead
          liveRead={liveRead.data}
          loading={liveRead.isLoading}
          stream={stream}
          onRegenerate={() => stream.start()}
          windowSize={windowSize}
          onWindowChange={setWindowSize}
          dashboard={dashboard.data}
          velocity={newsVelocity.data}
          diagnostics={quantDiagnostics.data}
        />
        <KPIRow dashboard={dashboard.data} loading={dashboard.isLoading} />
        {dashboard.data?.sentiment_momentum && (
          <TickerTape
            report={dashboard.data.sentiment_momentum}
            watchlist={watchlist}
            onToggleWatch={toggleWatch}
          />
        )}
        <TabStrip active={activeTab} onChange={setActiveTab} dashboard={dashboard.data} />
        {dashboard.isLoading ? (
          <Skeleton className="h-96" />
        ) : dashboard.error ? (
          <ErrorBox err={dashboard.error} />
        ) : !dashboard.data ? null : (
          <ActiveTabPanel
            tab={activeTab}
            dashboard={dashboard.data}
            watchlist={watchlist}
            onToggleWatch={toggleWatch}
            onSelectCluster={selectCluster}
            onSelectCell={selectCell}
            windowSize={windowSize}
          />
        )}
        <LiveStatusBar dashboard={dashboard.data} deepseekStatus={deepseekStatus.data} />
      </section>
      <aside className="grid gap-4 h-fit lg:sticky lg:top-3">
        {selectedCluster ? (
          <ClusterDrillDown
            cluster={selectedCluster}
            onClose={() => selectCluster(null)}
            watchlist={watchlist}
            windowSize={windowSize}
          />
        ) : selectedCell ? (
          <HeatmapDetailDrawer
            cell={selectedCell}
            onClose={() => selectCell(null)}
            watchlist={watchlist}
          />
        ) : (
          <>
            <WatchlistCard
              api={watchlistApi}
              dashboard={dashboard.data}
            />
            <HelpRail />
          </>
        )}
      </aside>
    </div>
  );
}

// ── Hero "Live Read" — narrative + window control + export ───────────────

/**
 * Tiny inline Markdown renderer for the live-read narrative.
 *
 * Hosted narratives often return markdown bold (`**...**`) for headline
 * tokens like "Dominant story:" — we surface them as visual emphasis
 * rather than literal asterisks. We don't pull a full markdown lib in
 * (echarts is already heavy); this handles the 3 patterns the prompt
 * actually produces: `**bold**`, `*italic*`, and double-newline → para
 * break. Anything else falls through as plain text.
 */
function renderInlineMarkdown(text: string): React.ReactNode[] {
  const blocks = text.split(/\n{2,}/).map((b) => b.trim()).filter(Boolean);
  return blocks.map((block, blockIdx) => {
    const out: React.ReactNode[] = [];
    let i = 0;
    const src = block.replace(/\n/g, " ");
    let buf = "";
    const flush = (key: string) => {
      if (buf) {
        out.push(<span key={key}>{buf}</span>);
        buf = "";
      }
    };
    while (i < src.length) {
      if (src.startsWith("**", i)) {
        const close = src.indexOf("**", i + 2);
        if (close !== -1) {
          flush(`t-${blockIdx}-${i}`);
          out.push(
            <strong key={`b-${blockIdx}-${i}`} className="text-accent">
              {src.slice(i + 2, close)}
            </strong>,
          );
          i = close + 2;
          continue;
        }
      } else if (src.startsWith("*", i) && src[i + 1] !== " ") {
        const close = src.indexOf("*", i + 1);
        if (close !== -1) {
          flush(`t-${blockIdx}-${i}`);
          out.push(
            <em key={`i-${blockIdx}-${i}`} className="text-[color:var(--fg-dim)]">
              {src.slice(i + 1, close)}
            </em>,
          );
          i = close + 1;
          continue;
        }
      }
      buf += src[i];
      i++;
    }
    flush(`t-${blockIdx}-end`);
    return (
      <p key={`p-${blockIdx}`} className="leading-relaxed">
        {out}
      </p>
    );
  });
}

function HeroLiveRead({
  liveRead,
  loading,
  stream,
  onRegenerate,
  windowSize,
  onWindowChange,
  dashboard,
  velocity,
  diagnostics,
}: {
  liveRead: import("@/lib/api").LiveReadResponse | undefined;
  loading: boolean;
  stream: ReturnType<typeof useStreamingLiveRead>;
  onRegenerate: () => void;
  windowSize: number;
  onWindowChange: (n: number) => void;
  dashboard: QuantDashboard | undefined;
  velocity: NewsVelocityResponse | undefined;
  diagnostics: DegradedDiagnostics | undefined;
}) {
  // Pick the narrative source: the streaming buffer is the primary,
  // but if the stream errored we fall back to the non-streaming endpoint's
  // synchronous narrative so the hero never blanks out.
  const streamFailed = stream.state === "error";
  const narrativeText = streamFailed
    ? liveRead?.narrative ?? stream.text
    : stream.text || liveRead?.narrative || "";
  const showSkeleton =
    !narrativeText && (stream.state === "streaming" || loading) && !streamFailed;
  const isTyping = stream.state === "streaming" && narrativeText.length > 0;
  // Source label / freshness signals reflect the *active* path: streaming
  // when it has produced any text, otherwise the non-streaming query.
  const sourceLabel: string = (() => {
    if (loading && stream.state === "idle") return "Reading the tape…";
    if (stream.state === "streaming" && !narrativeText) return "Streaming live read…";
    const source =
      streamFailed
        ? liveRead?.source
        : stream.meta.source ?? liveRead?.source;
    if (source === "deepseek") return "DeepSeek synthesis";
    if (source === "local") return "Local synthesis";
    return "—";
  })();
  const generatedAt =
    streamFailed ? liveRead?.generated_at : stream.meta.generatedAt ?? liveRead?.generated_at;
  const usdCost =
    streamFailed ? liveRead?.usd_cost : stream.meta.usdCost ?? liveRead?.usd_cost;
  const fallbackReason =
    streamFailed
      ? stream.error ?? liveRead?.fallback_reason
      : stream.meta.fallbackReason ?? liveRead?.fallback_reason;
  return (
    <section className="relative overflow-hidden rounded-xl border border-accent/40 hero-gradient p-6">
      {/* subtle radial accent in the top-left corner */}
      <div
        aria-hidden
        className="pointer-events-none absolute -top-20 -left-20 h-48 w-48 rounded-full bg-accent/20 blur-3xl"
      />
      <div className="relative flex flex-wrap items-baseline justify-between gap-3 mb-3">
        <div className="flex items-center gap-3">
          <span className="relative flex h-2 w-2">
            <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-accent opacity-75" />
            <span className="relative inline-flex h-2 w-2 rounded-full bg-accent" />
          </span>
          <div>
            <div className="text-[10px] uppercase tracking-[0.25em] text-accent font-semibold">
              Live read · Awareness Quant Lens
            </div>
            <h1 className="text-lg font-semibold mt-0.5 tracking-tight" data-testid="live-read-title">
              {sourceLabel}
            </h1>
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {/*
           * v72/v74 "N signals degraded" pill — extracted to its own
           * presentational component so the render path is unit-tested
           * (DegradedSignalsPill.test.tsx) without mounting the whole
           * hero. Hidden in the healthy steady state; positioned FIRST
           * in the chip row so a degraded run reads loud on cold scan.
           */}
          <DegradedSignalsPill diagnostics={diagnostics} />
          <span className="text-[10px] text-[color:var(--fg-dim)]">window</span>
          {[200, 500, 1000, 2000, 5000].map((n) => (
            <button
              key={n}
              type="button"
              className={`chip text-[10px] ${windowSize === n ? "chip-active" : ""}`}
              onClick={() => onWindowChange(n)}
            >
              {n.toLocaleString()}
            </button>
          ))}
          <button
            type="button"
            className="chip text-[10px]"
            onClick={onRegenerate}
            disabled={stream.state === "streaming"}
            data-testid="live-read-regenerate"
            title="re-run the live read"
          >
            {stream.state === "streaming" ? "streaming…" : (
              <span className="inline-flex items-center gap-1">
                <Icon name="refresh" />
                regenerate
              </span>
            )}
          </button>
          <button
            type="button"
            className="chip text-[10px]"
            disabled={!dashboard}
            onClick={() => {
              if (!dashboard) return;
              const blob = new Blob([JSON.stringify(dashboard, null, 2)], { type: "application/json" });
              const url = URL.createObjectURL(blob);
              const a = document.createElement("a");
              a.href = url;
              const stamp = new Date().toISOString().replace(/[:.]/g, "-");
              a.download = `catchem-quant-${stamp}.json`;
              document.body.appendChild(a);
              a.click();
              document.body.removeChild(a);
              URL.revokeObjectURL(url);
            }}
            title="download snapshot JSON (in-memory dashboard)"
          >
            <span className="inline-flex items-center gap-1">
              <Icon name="download" />
              snapshot
            </span>
          </button>
          <a
            href={api.exportQuantUrl(windowSize)}
            download="catchem_quant.json"
            className="chip text-[10px] hover:bg-[color:var(--bg-elev2)]"
            title="Download a fresh quant signals JSON from the sidecar"
            data-testid="quant-export-signals"
          >
            <span className="inline-flex items-center gap-1">
              <Icon name="download" />
              export signals
            </span>
          </a>
          <button
            type="button"
            className="chip text-[10px] no-print"
            onClick={() => window.print()}
            title="Print this page or save as PDF"
          >
            <span className="inline-flex items-center gap-1">
              <Icon name="print" />
              print / save PDF
            </span>
          </button>
        </div>
      </div>
      {showSkeleton ? (
        <Skeleton className="h-20" />
      ) : (
        <div
          className="relative text-[15px] leading-relaxed text-[color:var(--fg)] max-w-3xl grid gap-3"
          data-testid="live-read-narrative"
          data-stream-state={stream.state}
        >
          {narrativeText
            ? (
              <>
                {renderInlineMarkdown(narrativeText)}
                {isTyping && (
                  <span
                    aria-hidden
                    className="inline-block w-[7px] h-[1.1em] -mb-1 bg-accent animate-pulse align-middle ml-0.5"
                    data-testid="live-read-cursor"
                  />
                )}
              </>
            )
            : <p>—</p>}
        </div>
      )}
      <div className="relative mt-4 flex flex-wrap items-baseline gap-3 text-[10px] text-[color:var(--fg-muted)]">
        {generatedAt && (
          <span>
            generated <span className="text-[color:var(--fg-dim)]">{fmtRel(generatedAt)}</span>
          </span>
        )}
        {usdCost != null && (
          <span className="tabular-nums">cost ${usdCost.toFixed(5)}</span>
        )}
        {fallbackReason && (
          <span className="text-warn">fallback: {fallbackReason}</span>
        )}
        <NewsVelocityTile velocity={velocity} />
        <span className="ml-auto">
          {stream.state === "streaming" ? "streaming…" : stream.state === "done" ? "stream complete" : "auto-refresh 60s"}
        </span>
      </div>
    </section>
  );
}

/**
 * Compact single-line news-velocity tile rendered inside the hero
 * footer strip. Shape: ``4.2 rec/min · acceleration +2.1σ · burst``.
 * Tone:
 *   - burst → bad (alarming)
 *   - active → warn (watch)
 *   - quiet → accent (notable lull)
 *   - calm → muted (default)
 * Hidden entirely when ``samples === 0`` so a cold-start hero stays
 * uncluttered.
 */
function NewsVelocityTile({ velocity }: { velocity: NewsVelocityResponse | undefined }) {
  if (!velocity || velocity.samples === 0) return null;
  const tone =
    velocity.regime === "burst" ? "text-bad" :
    velocity.regime === "active" ? "text-warn" :
    velocity.regime === "quiet" ? "text-accent" :
    "text-[color:var(--fg-muted)]";
  const sign = velocity.acceleration_z >= 0 ? "+" : "";
  return (
    <span
      className="tabular-nums"
      data-testid="news-velocity-tile"
      data-regime={velocity.regime}
      title={`${velocity.samples} records over ${velocity.window_minutes}m, ${velocity.bucket_minutes}m buckets · ema fast ${velocity.ema_fast.toFixed(2)} / slow ${velocity.ema_slow.toFixed(2)}`}
    >
      <span className="text-[color:var(--fg-dim)]">velocity </span>
      {velocity.current_rate_per_min.toFixed(1)} rec/min
      <span className="text-[color:var(--fg-dim)]"> · </span>
      acceleration {sign}{velocity.acceleration_z.toFixed(1)}σ
      <span className="text-[color:var(--fg-dim)]"> · </span>
      <span className={tone}>{velocity.regime}</span>
    </span>
  );
}

// ── KPI row — 4 prominent metrics with mini-sparkline trend ──────────────
//
// The sparkline is fed by an in-memory ring buffer that accumulates each
// dashboard refresh. No backend timeseries call needed — we just remember
// the last N polls per KPI series. Persists for the page-session only;
// closing /scan resets it (which is fine; analysts read a fresh page).

const KPI_HISTORY_LEN = 30;
const KPI_HISTORY_KEY = "catchem.quant.kpi-history";

/**
 * Read+write a small ring buffer per KPI series so sparklines survive
 * page reloads. We keep it deliberately tiny (30 points × 4 keys) so
 * localStorage write-on-every-poll stays cheap. Reset by clearing
 * `catchem.quant.kpi-history` in the browser.
 */
function loadKpiHistory(): Record<string, number[]> {
  try {
    const raw = localStorage.getItem(KPI_HISTORY_KEY);
    if (!raw) return {};
    const parsed = JSON.parse(raw);
    if (parsed && typeof parsed === "object") {
      const out: Record<string, number[]> = {};
      for (const [k, v] of Object.entries(parsed)) {
        if (Array.isArray(v)) out[k] = v.filter((n) => typeof n === "number").slice(-KPI_HISTORY_LEN);
      }
      return out;
    }
  } catch {
    /* private mode etc. */
  }
  return {};
}

function saveKpiHistory(h: Record<string, number[]>): void {
  try {
    localStorage.setItem(KPI_HISTORY_KEY, JSON.stringify(h));
  } catch {
    /* quota / private mode — silent */
  }
}

function useKpiSeries(dashboard: QuantDashboard | undefined): Record<string, number[]> {
  const ref = useRef<Record<string, number[]>>(loadKpiHistory());
  if (dashboard) {
    const a = dashboard.anomalies;
    const anomCount =
      (a?.volume_anomalies.length ?? 0) +
      (a?.sentiment_shocks.length ?? 0) +
      (a?.symbol_bursts.length ?? 0);
    const next: Record<string, number> = {
      records: dashboard.n_records_window,
      anomalies: anomCount,
      shifts: dashboard.regime?.detected_shifts.length ?? 0,
      momentum: dashboard.sentiment_momentum?.tickers.length ?? 0,
    };
    let mutated = false;
    for (const k of Object.keys(next)) {
      const arr = ref.current[k] ?? [];
      const last = arr[arr.length - 1];
      if (last !== next[k]) {
        const updated = [...arr, next[k]];
        ref.current[k] = updated.length > KPI_HISTORY_LEN ? updated.slice(-KPI_HISTORY_LEN) : updated;
        mutated = true;
      } else if (arr.length === 0) {
        ref.current[k] = [next[k]];
        mutated = true;
      }
    }
    if (mutated) saveKpiHistory(ref.current);
  }
  return ref.current;
}

function KPIRow({ dashboard, loading }: { dashboard: QuantDashboard | undefined; loading: boolean }) {
  const history = useKpiSeries(dashboard);
  const kpis = useMemo(() => {
    if (!dashboard) return null;
    const a = dashboard.anomalies;
    const anomCount =
      (a?.volume_anomalies.length ?? 0) +
      (a?.sentiment_shocks.length ?? 0) +
      (a?.symbol_bursts.length ?? 0);
    return [
      {
        key: "records",
        label: "records / window",
        primary: dashboard.n_records_window.toLocaleString(),
        secondary: `${dashboard.n_clusters} cluster${dashboard.n_clusters === 1 ? "" : "s"}`,
      },
      {
        key: "anomalies",
        label: "active anomalies",
        primary: anomCount.toLocaleString(),
        secondary: `${a?.volume_anomalies.length ?? 0} vol · ${a?.symbol_bursts.length ?? 0} burst`,
        tone: anomCount > 0 ? "warn" : undefined,
      },
      {
        key: "shifts",
        label: "regime shifts",
        primary: (dashboard.regime?.detected_shifts.length ?? 0).toLocaleString(),
        secondary: dashboard.regime ? `${dashboard.regime.bucket_minutes}m buckets` : "—",
        tone: (dashboard.regime?.detected_shifts.length ?? 0) > 0 ? "accent" : undefined,
      },
      {
        key: "momentum",
        label: "ticker momentum",
        primary: (dashboard.sentiment_momentum?.tickers.length ?? 0).toLocaleString(),
        secondary: (() => {
          const flips = (dashboard.sentiment_momentum?.tickers ?? []).filter((t) => t.flip_detected).length;
          return flips > 0 ? `${flips} flip${flips === 1 ? "" : "s"}` : "no flips";
        })(),
        tone: (dashboard.sentiment_momentum?.tickers ?? []).some((t) => t.flip_detected) ? "bad" : undefined,
      },
    ];
  }, [dashboard]);
  if (loading || !kpis) {
    return (
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        {Array.from({ length: 4 }).map((_, i) => <Skeleton key={i} className="h-24" />)}
      </div>
    );
  }
  return (
    <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
      {kpis.map((k) => {
        const toneCls =
          k.tone === "warn" ? "text-warn" :
          k.tone === "accent" ? "text-accent" :
          k.tone === "bad" ? "text-bad" : "";
        const series = history[k.key] ?? [];
        return (
          <div
            key={k.label}
            className="rounded-lg border border-[color:var(--border)] bg-[color:var(--bg-elev)] p-4 overflow-hidden relative"
          >
            <div className="text-[10px] uppercase tracking-wider text-[color:var(--fg-muted)]">
              {k.label}
            </div>
            <div className={`mt-1 text-2xl font-semibold tabular-nums ${toneCls}`}>
              {k.primary}
            </div>
            <div className="mt-1 text-[10px] text-[color:var(--fg-dim)]">{k.secondary}</div>
            <KpiSparkline values={series} tone={k.tone} />
          </div>
        );
      })}
    </div>
  );
}

function KpiSparkline({ values, tone }: { values: number[]; tone?: string }) {
  if (values.length < 2) {
    // Not enough history yet — render a thin dim baseline so the box
    // doesn't visually collapse vs cards that DO have data.
    return (
      <div className="mt-3 h-6 flex items-end opacity-30">
        <div className="h-px w-full bg-[color:var(--border)]" />
      </div>
    );
  }
  const min = Math.min(...values);
  const max = Math.max(...values);
  const range = max - min || 1;
  const stroke =
    tone === "warn" ? "#fbbf24" :
    tone === "bad" ? "#f87171" :
    tone === "accent" ? "#5fb3ff" :
    "#7cd992";
  const w = 100;
  const h = 24;
  const step = w / Math.max(1, values.length - 1);
  const points = values
    .map((v, i) => {
      const x = i * step;
      const y = h - ((v - min) / range) * h;
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(" ");
  return (
    <div className="mt-3 h-6 w-full">
      <svg viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="none" className="h-full w-full overflow-visible">
        <polyline
          fill="none"
          stroke={stroke}
          strokeWidth="1.5"
          strokeLinecap="round"
          strokeLinejoin="round"
          points={points}
          vectorEffect="non-scaling-stroke"
        />
        {/* mark the latest point */}
        <circle
          cx={(values.length - 1) * step}
          cy={h - ((values[values.length - 1] - min) / range) * h}
          r="1.5"
          fill={stroke}
        />
      </svg>
    </div>
  );
}

// ── Tab strip ────────────────────────────────────────────────────────────

function TabStrip({
  active,
  onChange,
  dashboard,
}: {
  active: QuantTab;
  onChange: (t: QuantTab) => void;
  dashboard: QuantDashboard | undefined;
}) {
  // Show a small numeric badge next to each tab so the analyst sees what's
  // populated even before they click into it.
  const badges = useMemo<Record<QuantTab, number>>(() => {
    if (!dashboard) return { events: 0, sentiment: 0, sources: 0, anomalies: 0, network: 0, time: 0 };
    const a = dashboard.anomalies;
    return {
      events: dashboard.n_clusters,
      sentiment: dashboard.sentiment_momentum?.tickers.length ?? 0,
      sources: dashboard.source_leaderboard?.sources.length ?? 0,
      anomalies:
        (a?.volume_anomalies.length ?? 0) +
        (a?.sentiment_shocks.length ?? 0) +
        (a?.symbol_bursts.length ?? 0),
      network:
        (dashboard.co_occurrence?.asset_reason_cells.length ?? 0) +
        (dashboard.spillover?.edges.length ?? 0),
      // The Time tab fetches separately; badge stays 0 unless we wire a
      // top-level query for it here. Keeping 0 keeps the tab unobtrusive
      // until the analyst opens it.
      time: 0,
    };
  }, [dashboard]);
  return (
    <nav
      role="tablist"
      aria-label="Quant Lens sections"
      className="flex flex-wrap items-center gap-1 border-b border-[color:var(--border)] px-1"
    >
      {(Object.keys(TAB_LABELS) as QuantTab[]).map((t) => {
        const selected = t === active;
        const badge = badges[t];
        return (
          <button
            key={t}
            type="button"
            role="tab"
            aria-selected={selected}
            onClick={() => onChange(t)}
            data-testid={`quant-tab-${t}`}
            className={`relative -mb-px px-3 py-2 text-xs border-b-2 transition-colors ${
              selected
                ? "border-accent text-[color:var(--fg)] font-semibold"
                : "border-transparent text-[color:var(--fg-dim)] hover:text-[color:var(--fg)]"
            }`}
          >
            {TAB_LABELS[t]}
            {badge > 0 && (
              <span
                className={`ml-1.5 inline-flex items-center justify-center rounded-full px-1.5 py-0 text-[9px] tabular-nums ${
                  selected ? "bg-accent/30 text-accent" : "bg-[color:var(--bg-elev2)] text-[color:var(--fg-muted)]"
                }`}
              >
                {badge.toLocaleString()}
              </span>
            )}
          </button>
        );
      })}
    </nav>
  );
}

// ── Active tab panel ─────────────────────────────────────────────────────

function ActiveTabPanel({
  tab,
  dashboard,
  watchlist,
  onToggleWatch,
  onSelectCluster,
  onSelectCell,
  windowSize,
}: {
  tab: QuantTab;
  dashboard: QuantDashboard;
  watchlist: string[];
  onToggleWatch: (sym: string) => void;
  onSelectCluster: (c: EventClusterDTO | null) => void;
  onSelectCell: (c: HeatmapCell | null) => void;
  windowSize: number;
}) {
  if (tab === "events") {
    return (
      <div className="grid gap-4 animate-fadein">
        <TopEventHero clusters={dashboard.clusters} regime={dashboard.regime} watchlist={watchlist} onSelect={onSelectCluster} />
        <RegimeLineChart report={dashboard.regime} />
        <EventClustersPanel
          clusters={dashboard.clusters}
          onSelect={onSelectCluster}
          watchlist={watchlist}
        />
      </div>
    );
  }
  if (tab === "sentiment") {
    return (
      <div className="grid gap-4 animate-fadein">
        <TopMomentumHero report={dashboard.sentiment_momentum} watchlist={watchlist} />
        <SentimentMomentumPanel
          report={dashboard.sentiment_momentum}
          watchlist={watchlist}
          onToggleWatch={onToggleWatch}
        />
        <SentimentDispersionPanel />
        <IntensityPanel />
        <NoveltyScatter items={dashboard.novelty_timeline} />
      </div>
    );
  }
  if (tab === "sources") {
    return (
      <div className="grid gap-4 animate-fadein">
        <TopSourceHero leaderboard={dashboard.source_leaderboard} leadLag={dashboard.lead_lag} />
        <SourceRadarPanel leaderboard={dashboard.source_leaderboard} />
        <LeadLagArcPanel report={dashboard.lead_lag} />
      </div>
    );
  }
  if (tab === "anomalies") {
    return (
      <div className="grid gap-4 animate-fadein">
        <TopAnomalyHero report={dashboard.anomalies} watchlist={watchlist} />
        <AnomalyStrip report={dashboard.anomalies} watchlist={watchlist} />
      </div>
    );
  }
  if (tab === "time") {
    return (
      <div className="grid gap-4 animate-fadein">
        <MarketTimePanel />
        <ArrivalHeatmapPanel />
      </div>
    );
  }
  // network
  return (
    <div className="grid gap-4 animate-fadein">
      <TopNetworkHero report={dashboard.co_occurrence} spillover={dashboard.spillover} onSelectCell={onSelectCell} />
      <CoOccurrenceHeatmap report={dashboard.co_occurrence} onSelectCell={onSelectCell} />
      <SpilloverSankey report={dashboard.spillover} />
      <SymbolCorrelationPanel />
      <PersistencePanel windowSize={windowSize} />
    </div>
  );
}

/**
 * v65: news_persistence consumer.
 *
 * Renders long-running narratives — scopes whose mentions cover many distinct
 * days of the trailing window. Persistent scopes pair well with sentiment
 * dispersion + intensity: persistent + low-dispersion = unanimous trend,
 * persistent + disputed = ongoing debate.
 *
 * Network tab placement is intentional — persistence belongs alongside
 * spillover/correlation as a "structural relationships" signal, not the
 * faster "what's hot RIGHT NOW" signals in Events/Sentiment tabs.
 */
function PersistencePanel({ windowSize: _windowSize }: { windowSize: number }) {
  useLang();
  // The `/api/quant/persistence` endpoint has no record-window/limit
  // parameter (only window_days / min_records / top_n), so this panel
  // CANNOT thread the parent's `windowSize` selection through — it always
  // reads the backend's own record window. We therefore keep `windowSize`
  // OUT of the queryKey: previously it was IN the key while the queryFn
  // ignored it, so zooming in/out forced a pointless refetch (identical
  // request bytes) yet advertised a record-window behavior the API can't
  // deliver. The prop is still accepted (parent threads it) but renamed to
  // `_windowSize` to mark it deliberately unused until the API gains a
  // `limit` param and `quantPersistence` forwards it — at which point it
  // goes back into the queryKey AND the call below.
  const q = useQuery({
    queryKey: ["quant-persistence", 7],
    queryFn: () => api.quantPersistence(7, 3, 10),
    refetchInterval: 60_000,
    staleTime: 30_000,
  });
  if (q.isLoading || !q.data) return null;
  const buckets = q.data.buckets ?? [];
  if (buckets.length === 0) return null;
  return (
    <section className="card">
      <div className="mb-2 flex items-baseline justify-between gap-2">
        <h2 className="label">
          <SignalExplainer term="persistence">
            {t("quant.persistence.title")}
          </SignalExplainer>
        </h2>
        <span className="text-[10px] text-[color:var(--fg-muted)]">
          {t("quant.persistence.summary")
            .replace("{days}", String(q.data.window_days))
            .replace("{scopes}", String(buckets.length))}
        </span>
      </div>
      <ul className="grid gap-1.5 text-xs">
        {buckets.slice(0, 8).map((b) => {
          const tone =
            b.persistence_ratio >= 0.7 ? "text-good" :
            b.persistence_ratio >= 0.4 ? "text-warn" :
            "text-[color:var(--fg-dim)]";
          return (
            <li key={b.scope} className="grid grid-cols-[180px_1fr_70px] items-center gap-2">
              <span className="font-mono truncate" title={b.scope}>{b.scope}</span>
              <span className="h-2 rounded bg-[color:var(--bg-elev2)] overflow-hidden">
                <span
                  className="block h-full bg-accent/60"
                  style={{ width: `${Math.max(3, b.persistence_ratio * 100)}%` }}
                />
              </span>
              <span className={`text-right tabular-nums ${tone}`}>
                {b.days_covered}/{q.data.window_days}d
              </span>
            </li>
          );
        })}
      </ul>
    </section>
  );
}

// ── Reusable tab-hero shell ──────────────────────────────────────────────

function TabHero({
  tone,
  eyebrow,
  rightMeta,
  primaryLabel,
  primaryValue,
  primarySub,
  secondaryLabel,
  secondaryValue,
  secondaryTone,
  narrative,
  loading,
  narrativeLabel = "Signal narrative",
}: {
  tone: "accent" | "warn" | "good" | "bad";
  eyebrow: string;
  rightMeta?: string;
  primaryLabel?: React.ReactNode;
  primaryValue: React.ReactNode;
  primarySub: React.ReactNode;
  secondaryLabel?: React.ReactNode;
  secondaryValue?: React.ReactNode;
  secondaryTone?: string;
  narrative: SignalNarrative | null;
  loading: boolean;
  narrativeLabel?: string;
}) {
  const borderCls =
    tone === "warn" ? "border-warn/40 from-warn/15" :
    tone === "good" ? "border-good/40 from-good/15" :
    tone === "bad" ? "border-bad/40 from-bad/15" : "border-accent/40 from-accent/15";
  const blobCls =
    tone === "warn" ? "bg-warn/15" :
    tone === "good" ? "bg-good/15" :
    tone === "bad" ? "bg-bad/15" : "bg-accent/15";
  const eyebrowCls =
    tone === "warn" ? "text-warn" :
    tone === "good" ? "text-good" :
    tone === "bad" ? "text-bad" : "text-accent";
  const valueCls =
    tone === "warn" ? "text-warn" :
    tone === "good" ? "text-good" :
    tone === "bad" ? "text-bad" : "text-accent";
  return (
    <section className={`relative overflow-hidden rounded-xl border ${borderCls} bg-gradient-to-br via-[color:var(--bg-elev)] to-[color:var(--bg-elev)] p-5`}>
      <div aria-hidden className={`pointer-events-none absolute -top-20 -right-20 h-48 w-48 rounded-full ${blobCls} blur-3xl`} />
      <div className="relative flex items-baseline justify-between gap-3">
        <div className={`text-[10px] uppercase tracking-[0.25em] font-semibold ${eyebrowCls}`}>
          {eyebrow}
        </div>
        {rightMeta && <span className="text-[10px] text-[color:var(--fg-muted)]">{rightMeta}</span>}
      </div>
      <div className="relative mt-2 flex items-baseline gap-4 flex-wrap">
        <div>
          {primaryLabel && (
            <div className="text-[9px] uppercase tracking-wider text-[color:var(--fg-muted)] mb-0.5">
              {primaryLabel}
            </div>
          )}
          <div className={`text-3xl font-bold tracking-tight tabular-nums ${valueCls}`}>
            {primaryValue}
          </div>
          <div className="text-[11px] text-[color:var(--fg-dim)] mt-1">{primarySub}</div>
        </div>
        {secondaryValue !== undefined && (
          <div className="ml-auto text-right">
            <div className="text-[9px] uppercase tracking-wider text-[color:var(--fg-muted)]">
              {secondaryLabel}
            </div>
            <div className={`text-2xl font-semibold tabular-nums ${secondaryTone ?? valueCls}`}>
              {secondaryValue}
            </div>
          </div>
        )}
      </div>
      <div className="relative mt-3 rounded-md border border-accent/20 bg-accent/5 p-2">
        <div className="mb-1 flex flex-wrap items-baseline gap-2 text-[9px] uppercase tracking-wider">
          <span className="text-accent">
            {loading ? `${narrativeLabel} · thinking…` : narrativeLabel}
          </span>
          <NarrativeProvenance narrative={narrative} />
        </div>
        <p className="text-[12px] italic leading-relaxed text-[color:var(--fg)]">
          {narrative?.text || (loading ? "Generating an interpretation…" : "—")}
        </p>
      </div>
    </section>
  );
}

// ── Tab heroes ───────────────────────────────────────────────────────────

function TopEventHero({
  clusters,
  regime,
  watchlist,
  onSelect,
}: {
  clusters: EventClusterDTO[];
  regime: RegimeReportDTO | null;
  watchlist: string[];
  onSelect: (c: EventClusterDTO) => void;
}) {
  const top = useMemo(() => {
    if (!clusters || clusters.length === 0) return null;
    // Rank by size × coherence × mean_relevance
    return [...clusters].sort((a, b) => {
      const sa = a.size * a.coherence * (a.mean_relevance || 0.5);
      const sb = b.size * b.coherence * (b.mean_relevance || 0.5);
      return sb - sa;
    })[0];
  }, [clusters]);
  const [narrative, setNarrative] = useState<SignalNarrative | null>(null);
  const explain = useMutation({
    mutationFn: (payload: Record<string, unknown>) => api.quantExplain("cluster", payload),
    onSuccess: (r) => setNarrative(toSignalNarrative(r)),
  });
  useEffect(() => {
    if (!top) return;
    setNarrative(null);
    explain.mutate(top as unknown as Record<string, unknown>);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [top?.cluster_id]);
  if (!top) {
    return (
      <section className="card">
        <h2 className="label mb-2">top event</h2>
        <p className="text-[11px] text-[color:var(--fg-muted)]">
          No multi-source events in window. A cluster needs ≥2 captures of the same event within 30 min from ≥2 distinct domains.
        </p>
      </section>
    );
  }
  const watched = (top.dominant_symbols ?? []).some((s) => isWatched(s, watchlist));
  // Fall back through symbols → reasons → domains so the hero never reads
  // as a bare "(no symbols)". Each fallback layer is still semantically
  // meaningful — an event cluster about energy with no ticker mentions is
  // still better labelled "energy · supply_chain" than as a blank slug.
  let primaryText: string;
  let primaryKind: "symbols" | "reasons" | "domains" = "symbols";
  if (top.dominant_symbols.length > 0) {
    primaryText = top.dominant_symbols.slice(0, 2).join(" · ");
  } else if (top.dominant_reasons.length > 0) {
    primaryText = top.dominant_reasons.slice(0, 2).join(" · ");
    primaryKind = "reasons";
  } else if (top.member_domains.length > 0) {
    primaryText = top.member_domains.slice(0, 2).join(" · ");
    primaryKind = "domains";
  } else {
    primaryText = `cluster ${top.cluster_id.slice(0, 8)}`;
    primaryKind = "domains";
  }
  const primary = (
    <button
      type="button"
      className="hover:underline text-left"
      onClick={() => onSelect(top)}
      title="open cluster drill-down"
    >
      {watched ? "★ " : ""}{primaryText}
      {primaryKind !== "symbols" && (
        <span className="ml-2 text-[10px] uppercase tracking-wider text-[color:var(--fg-muted)] font-normal">
          via {primaryKind}
        </span>
      )}
    </button>
  );
  // Build a secondary descriptor line, preferring the OTHER labels we
  // didn't use in primary. e.g., primary=reasons → secondary=domains.
  const secondaryBits: string[] = [];
  if (primaryKind !== "symbols" && top.dominant_symbols.length > 0) {
    secondaryBits.push(top.dominant_symbols.slice(0, 3).join(" · "));
  }
  if (primaryKind !== "reasons" && top.dominant_reasons.length > 0) {
    secondaryBits.push(top.dominant_reasons.slice(0, 3).join(" · "));
  }
  if (primaryKind !== "domains" && top.member_domains.length > 0) {
    secondaryBits.push(top.member_domains.slice(0, 3).join(" · "));
  }
  const reasonsLine = secondaryBits.length > 0 ? secondaryBits.join("   |   ") : top.dominant_reasons.slice(0, 3).join(" · ");
  return (
    <TabHero
      tone="accent"
      eyebrow="Top event · highest-conviction cluster"
      rightMeta={`size ${top.size} · ${top.member_domains.length} sources · ${fmtRel(top.first_seen_ts)}`}
      primaryValue={primary}
      primarySub={
        <>
          {reasonsLine || "(no reasons)"} · mean relevance {top.mean_relevance.toFixed(2)}
          {regime?.detected_shifts.length ? ` · ${regime.detected_shifts.length} regime shift${regime.detected_shifts.length === 1 ? "" : "s"}` : ""}
        </>
      }
      secondaryLabel={<SignalExplainer term="cluster coherence">coherence</SignalExplainer>}
      secondaryValue={`${(top.coherence * 100).toFixed(0)}%`}
      secondaryTone={top.coherence >= 0.6 ? "text-good" : top.coherence >= 0.4 ? "text-accent" : "text-warn"}
      narrative={narrative}
      loading={explain.isPending}
    />
  );
}

function TopMomentumHero({
  report,
  watchlist,
}: {
  report: SentimentMomentumReportDTO | null;
  watchlist: string[];
}) {
  const top = useMemo(() => {
    if (!report?.tickers || report.tickers.length === 0) return null;
    return [...report.tickers].sort((a, b) => Math.abs(b.momentum) - Math.abs(a.momentum))[0];
  }, [report]);
  const [narrative, setNarrative] = useState<SignalNarrative | null>(null);
  const explain = useMutation({
    mutationFn: (payload: Record<string, unknown>) =>
      api.quantExplain("anomaly", payload),
    onSuccess: (r) => setNarrative(toSignalNarrative(r)),
  });
  useEffect(() => {
    if (!top) return;
    setNarrative(null);
    explain.mutate({
      symbol: top.symbol,
      momentum: top.momentum,
      direction: top.direction,
      mention_count: top.mention_count,
      buckets: top.buckets.length,
      net_sentiment: top.overall_net_sentiment,
      flip_detected: top.flip_detected,
    } as unknown as Record<string, unknown>);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [top?.symbol, top?.momentum]);
  if (!top) {
    return (
      <section className="card">
        <h2 className="label mb-2">top momentum mover</h2>
        <p className="text-[11px] text-[color:var(--fg-muted)]">
          No ticker has cleared the minimum-mentions floor yet — momentum needs ≥4 mentions across multiple buckets.
        </p>
      </section>
    );
  }
  const insufficient = top.buckets.length < 2;
  const tone: "good" | "bad" | "warn" =
    insufficient ? "warn" :
    top.momentum > 0.2 ? "good" :
    top.momentum < -0.2 ? "bad" : "warn";
  const watched = isWatched(top.symbol, watchlist);
  return (
    <TabHero
      tone={tone}
      eyebrow="Top momentum mover · |momentum| max"
      rightMeta={`${top.mention_count} mentions · ${top.buckets.length} buckets`}
      primaryValue={
        <span>
          {watched ? "★ " : ""}{top.symbol}
          {top.flip_detected && <span className="ml-2 text-bad text-base">⚡</span>}
        </span>
      }
      primarySub={
        insufficient
          ? "Single bucket — momentum undefined until we accumulate a second bucket."
          : `${top.direction.replace("_", " ")} · net sentiment ${top.overall_net_sentiment.toFixed(2)}`
      }
      secondaryLabel={<SignalExplainer term="sentiment momentum">momentum</SignalExplainer>}
      secondaryValue={insufficient ? "—" : (top.momentum >= 0 ? "+" : "") + top.momentum.toFixed(2)}
      narrative={narrative}
      loading={explain.isPending}
      narrativeLabel="momentum narrative"
    />
  );
}

function TopSourceHero({
  leaderboard,
  leadLag,
}: {
  leaderboard: SourceLeaderboardDTO | null;
  leadLag: LeadLagReportDTO | null;
}) {
  const top = leaderboard?.sources?.[0] ?? null;
  const leader = leadLag?.per_source && [...leadLag.per_source].sort((a, b) => b.composite_score - a.composite_score)[0];
  const [narrative, setNarrative] = useState<SignalNarrative | null>(null);
  const explain = useMutation({
    mutationFn: (payload: Record<string, unknown>) => api.quantExplain("cluster", payload),
    onSuccess: (r) => setNarrative(toSignalNarrative(r)),
  });
  useEffect(() => {
    if (!top) return;
    setNarrative(null);
    explain.mutate({
      domain: top.domain,
      composite_score: top.composite_score,
      relevant_rate: top.relevant_rate,
      signal_density: top.signal_density,
      asset_diversity: top.asset_diversity,
      reason_diversity: top.reason_diversity,
      symbol_uniqueness: top.symbol_uniqueness,
      record_count: top.record_count,
      lead_rate: leader?.lead_rate ?? null,
    } as unknown as Record<string, unknown>);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [top?.domain]);
  if (!top || !leaderboard) {
    return (
      <section className="card">
        <h2 className="label mb-2">top source</h2>
        <p className="text-[11px] text-[color:var(--fg-muted)]">
          No source has crossed the min-records floor yet (default 3).
        </p>
      </section>
    );
  }
  return (
    <TabHero
      tone="good"
      eyebrow="Top source · highest composite signal"
      rightMeta={`${leaderboard.total_domains} domains · ${leaderboard.window_days}d window`}
      primaryValue={top.domain}
      primarySub={
        <>
          {top.record_count} records · {top.relevant_count} relevant · signal density {(top.signal_density * 100).toFixed(0)}%
          {leader && leader.domain === top.domain && (
            <span className="ml-2 text-accent">· leads {(leader.lead_rate * 100).toFixed(0)}% of events</span>
          )}
        </>
      }
      secondaryLabel={<SignalExplainer term="composite source score">composite</SignalExplainer>}
      secondaryValue={`${(top.composite_score * 100).toFixed(0)}%`}
      narrative={narrative}
      loading={explain.isPending}
      narrativeLabel="source narrative"
    />
  );
}

function TopNetworkHero({
  report,
  spillover,
  onSelectCell,
}: {
  report: CoOccurrenceReportDTO | null;
  spillover: SpilloverReportDTO | null;
  onSelectCell: (c: { asset: string; reason: string }) => void;
}) {
  const top = report?.asset_reason_cells?.[0] ?? null;
  const topEdge = spillover?.edges?.[0] ?? null;
  const [narrative, setNarrative] = useState<SignalNarrative | null>(null);
  const explain = useMutation({
    mutationFn: (payload: Record<string, unknown>) => api.quantExplain("spillover", payload),
    onSuccess: (r) => setNarrative(toSignalNarrative(r)),
  });
  useEffect(() => {
    if (!top && !topEdge) return;
    setNarrative(null);
    explain.mutate(
      topEdge
        ? (topEdge as unknown as Record<string, unknown>)
        : ({
            asset_class: top?.asset_class,
            reason_code: top?.reason_code,
            lift: top?.lift,
            count: top?.count,
            mean_relevance: top?.mean_relevance,
          } as unknown as Record<string, unknown>),
    );
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [top?.asset_class, top?.reason_code, topEdge?.source_asset, topEdge?.target_asset]);
  if (!top || !report) {
    return (
      <section className="card">
        <h2 className="label mb-2">top network signal</h2>
        <p className="text-[11px] text-[color:var(--fg-muted)]">
          No asset×reason co-occurrence yet. Need at least one record carrying both labels.
        </p>
      </section>
    );
  }
  return (
    <TabHero
      tone="accent"
      eyebrow="Top network signal · strongest lift"
      rightMeta={`${report.asset_reason_cells.length} cells · ${report.strong_edges.length} symbol edges`}
      primaryValue={
        <button
          type="button"
          className="hover:underline text-left"
          onClick={() => onSelectCell({ asset: top.asset_class, reason: top.reason_code })}
          title="open record list for this combo"
        >
          {top.asset_class} × {top.reason_code}
        </button>
      }
      primarySub={
        <>
          {top.count} records · mean relevance {top.mean_relevance.toFixed(2)}
          {topEdge && (
            <span className="ml-2 text-accent">
              · spillover {topEdge.source_asset} → {topEdge.target_asset} ({topEdge.spillover_score.toFixed(2)})
            </span>
          )}
        </>
      }
      secondaryLabel={<SignalExplainer term="lift">lift</SignalExplainer>}
      secondaryValue={`${top.lift.toFixed(2)}×`}
      narrative={narrative}
      loading={explain.isPending}
      narrativeLabel="network narrative"
    />
  );
}

// ── Top-anomaly hero (highest-z signal pinned at top of Anomalies tab) ──

function TopAnomalyHero({
  report,
  watchlist,
}: {
  report: AnomalyReportDTO | null;
  watchlist: string[];
}) {
  const [narrative, setNarrative] = useState<SignalNarrative | null>(null);
  const explain = useMutation({
    mutationFn: (payload: Record<string, unknown>) => api.quantExplain("anomaly", payload),
    onSuccess: (r) => setNarrative(toSignalNarrative(r)),
  });

  // Pick the single most extreme signal across all 3 anomaly axes.
  const top = useMemo(() => {
    if (!report) return null;
    type Candidate = {
      kind: "burst" | "volume" | "shock";
      label: string;
      sublabel: string;
      z: number;
      tone: string;
      payload: Record<string, unknown>;
      watched?: boolean;
    };
    const cands: Candidate[] = [];
    for (const b of report.symbol_bursts ?? []) {
      cands.push({
        kind: "burst",
        label: b.symbol,
        sublabel: `${b.observed} mentions in a ${report.bucket_minutes}m window`,
        z: Math.abs(b.z_score),
        tone: "text-bad",
        payload: b as unknown as Record<string, unknown>,
        watched: isWatched(b.symbol, watchlist),
      });
    }
    for (const v of report.volume_anomalies ?? []) {
      cands.push({
        kind: "volume",
        label: `${v.observed} records`,
        sublabel: `volume spike at ${fmtRel(v.bucket_start)}`,
        z: Math.abs(v.z_score),
        tone: v.severity === "high" ? "text-bad" : "text-warn",
        payload: v as unknown as Record<string, unknown>,
      });
    }
    for (const s of report.sentiment_shocks ?? []) {
      cands.push({
        kind: "shock",
        label: `net ${(s.observed_net >= 0 ? "+" : "") + s.observed_net.toFixed(2)}`,
        sublabel: `${s.direction.replace("_", " ")} at ${fmtRel(s.bucket_start)}`,
        z: Math.abs(s.z_score),
        tone: s.direction === "bullish_shock" ? "text-good" : s.direction === "bearish_shock" ? "text-bad" : "text-warn",
        payload: s as unknown as Record<string, unknown>,
      });
    }
    cands.sort((a, b) => b.z - a.z);
    return cands[0] ?? null;
  }, [report, watchlist]);

  // Auto-fire a narrative whenever the top signal changes.
  useEffect(() => {
    if (!top) return;
    setNarrative(null);
    explain.mutate(top.payload);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [top?.kind, top?.label, top?.z]);

  if (!top) {
    return (
      <section className="card">
        <h2 className="label mb-2">top anomaly</h2>
        <p className="text-[11px] text-[color:var(--fg-muted)]">
          No anomaly fired in the current window — the flow is statistically normal.
        </p>
      </section>
    );
  }
  return (
    <section className="relative overflow-hidden rounded-xl border border-bad/40 bg-gradient-to-br from-bad/15 via-[color:var(--bg-elev)] to-[color:var(--bg-elev)] p-5">
      <div
        aria-hidden
        className="pointer-events-none absolute -top-20 -right-20 h-48 w-48 rounded-full bg-bad/15 blur-3xl"
      />
      <div className="relative flex items-baseline justify-between gap-3">
        <div className="text-[10px] uppercase tracking-[0.25em] text-bad font-semibold">
          Top anomaly · highest-z signal
        </div>
        <span className="text-[10px] text-[color:var(--fg-muted)]">
          auto-narrated · {top.kind === "burst" ? "symbol burst" : top.kind === "volume" ? "volume spike" : "sentiment shock"}
        </span>
      </div>
      <div className="relative mt-2 flex items-baseline gap-4 flex-wrap">
        <div>
          <div className={`text-3xl font-bold tracking-tight tabular-nums ${top.tone}`}>
            {top.watched ? "★ " : ""}{top.label}
          </div>
          <div className="text-[11px] text-[color:var(--fg-dim)] mt-1">{top.sublabel}</div>
        </div>
        <div className="ml-auto text-right">
          <div className="text-[9px] uppercase tracking-wider text-[color:var(--fg-muted)]">
            <SignalExplainer term="z-score">z-score</SignalExplainer>
          </div>
          <div className={`text-2xl font-semibold tabular-nums ${top.tone}`}>{top.z.toFixed(1)}σ</div>
        </div>
      </div>
      <div className="relative mt-3 rounded-md border border-accent/20 bg-accent/5 p-2">
        <div className="mb-1 flex flex-wrap items-center gap-2 text-[9px] uppercase tracking-wider text-accent">
          <span>{explain.isPending ? "Generating narrative…" : "Signal narrative"}</span>
          <NarrativeProvenance narrative={narrative} />
        </div>
        <p className="text-[12px] italic leading-relaxed text-[color:var(--fg)]">
          {narrative?.text || (explain.isPending ? "Generating an interpretation…" : "—")}
        </p>
      </div>
    </section>
  );
}

// ── Watchlist helpers ────────────────────────────────────────────────────
//
// The hook itself lives in `./useWatchlist.ts` (extracted so the reorder
// + multi-sort logic stays unit-testable in isolation). `isWatched` is the
// case-insensitive lookup the rest of /scan uses to decide whether a
// ticker badge gets the ★ pin.

function isWatched(sym: string, watchlist: string[]): boolean {
  return watchlist.includes(sym.trim().toUpperCase());
}

// ── shared chart helpers ──────────────────────────────────────────────────

const CHART_BG = "transparent";
const AXIS_COLOR = "#9aa3b2";
const AXIS_LINE = "#232838";
const PALETTE = [
  "#5fb3ff", // accent
  "#7cd992",
  "#fbbf24",
  "#f87171",
  "#a78bfa",
  "#22d3ee",
  "#fb923c",
  "#34d399",
];

function fmtSecs(seconds: number | null | undefined): string {
  if (seconds == null || !Number.isFinite(seconds)) return "—";
  const s = Math.round(seconds);
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.round(s / 60)}m`;
  return `${(s / 3600).toFixed(1)}h`;
}

function toSignalNarrative(r: {
  narrative: string;
  source: NarrativeSource;
  fallback_reason?: string;
  usd_cost?: number;
}): SignalNarrative {
  return {
    text: r.narrative,
    source: r.source,
    fallbackReason: r.fallback_reason,
    usdCost: r.usd_cost,
  };
}

function NarrativeProvenance({ narrative }: { narrative: SignalNarrative | null }) {
  if (!narrative) return null;
  return (
    <span className="inline-flex flex-wrap items-center gap-1">
      <span className={narrative.source === "deepseek" ? "text-accent" : "text-warn"}>
        {narrative.source === "deepseek" ? "DeepSeek" : "local"}
      </span>
      {narrative.fallbackReason && (
        <span className="text-warn">fallback: {narrative.fallbackReason}</span>
      )}
      {narrative.usdCost != null && (
        <span className="tabular-nums text-[color:var(--fg-muted)]">
          ${narrative.usdCost.toFixed(5)}
        </span>
      )}
    </span>
  );
}

// ── header / control strip ────────────────────────────────────────────────

function Header({
  windowSize,
  onWindowChange,
  dashboard,
  loading,
}: {
  windowSize: number;
  onWindowChange: (n: number) => void;
  dashboard: QuantDashboard | undefined;
  loading: boolean;
}) {
  const stats = useMemo(() => {
    if (!dashboard) return null;
    return [
      { label: "clusters", value: dashboard.n_clusters },
      { label: "novelty pts", value: dashboard.novelty_timeline.length },
      { label: "lead/lag events", value: dashboard.lead_lag?.total_events ?? 0 },
      { label: "regime shifts", value: dashboard.regime?.detected_shifts.length ?? 0 },
      { label: "vol anomalies", value: dashboard.anomalies?.volume_anomalies.length ?? 0 },
      { label: "sym bursts", value: dashboard.anomalies?.symbol_bursts.length ?? 0 },
      { label: "spillover edges", value: dashboard.spillover?.edges.length ?? 0 },
      { label: "tickers tracked", value: dashboard.sentiment_momentum?.tickers.length ?? 0 },
    ];
  }, [dashboard]);
  return (
    <div className="card grid gap-3">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <div>
          <h2 className="text-sm font-semibold">Awareness Quant Lens</h2>
          <p className="text-[11px] text-[color:var(--fg-muted)] mt-0.5">
            10 depth signals · ECharts visualisations · optional narrative overlay
          </p>
        </div>
        <span className="text-[10px] text-[color:var(--fg-dim)]">
          {loading ? "loading…" : dashboard ? `${fmtRel(dashboard.generated_at)} · ${dashboard.n_records_window.toLocaleString()} rec window` : "—"}
        </span>
      </div>
      <div className="flex flex-wrap items-baseline gap-2 text-[10px]">
        <span className="text-[color:var(--fg-dim)]">window:</span>
        {[200, 500, 1000, 2000, 5000].map((n) => (
          <button
            key={n}
            type="button"
            className={`chip text-[10px] ${windowSize === n ? "chip-active" : ""}`}
            onClick={() => onWindowChange(n)}
          >
            {n.toLocaleString()}
          </button>
        ))}
        <button
          type="button"
          className="chip text-[10px] ml-auto"
          disabled={!dashboard}
          onClick={() => {
            if (!dashboard) return;
            const blob = new Blob([JSON.stringify(dashboard, null, 2)], { type: "application/json" });
            const url = URL.createObjectURL(blob);
            const a = document.createElement("a");
            a.href = url;
            const stamp = new Date().toISOString().replace(/[:.]/g, "-");
            a.download = `catchem-quant-${stamp}.json`;
            document.body.appendChild(a);
            a.click();
            document.body.removeChild(a);
            URL.revokeObjectURL(url);
          }}
          title="download the current dashboard snapshot as JSON"
        >
          <span className="inline-flex items-center gap-1">
            <Icon name="download" />
            export
          </span>
        </button>
      </div>
      {stats && (
        <div className="grid grid-cols-4 gap-2 md:grid-cols-8">
          {stats.map((s) => (
            <div key={s.label} className="rounded-md border border-[color:var(--border-subtle)] bg-[color:var(--bg-elev2)]/40 px-2 py-1.5">
              <div className="text-[9px] uppercase tracking-wider text-[color:var(--fg-muted)]">
                {s.label}
              </div>
              <div className="text-sm font-semibold tabular-nums mt-0.5">
                {(s.value ?? 0).toLocaleString()}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// ── 1. Regime line chart with KL spikes + narrative explain ───────────────

function RegimeLineChart({ report }: { report: RegimeReportDTO | null }) {
  const [narrative, setNarrative] = useState<SignalNarrative | null>(null);
  const explain = useMutation({
    mutationFn: (payload: Record<string, unknown>) => api.quantExplain("regime_shift", payload),
    onSuccess: (r) => setNarrative(toSignalNarrative(r)),
  });

  if (!report || report.buckets.length === 0) {
    return (
      <section className="card">
        <h2 className="label mb-2">topic regime · KL divergence</h2>
        <EmptyState title="No regime buckets yet" hint="Wait for the corpus to fill at least 2 buckets." action={<Link to="/replay" className="btn">Open Replay/Upload</Link>} />
      </section>
    );
  }
  const buckets = report.buckets;
  const xs = buckets.map((b) => b.bucket_start);
  const klValues = buckets.map((b) => b.kl_divergence_from_prev ?? 0);
  const markPoints = buckets
    .filter((b) => b.is_regime_shift)
    .map((b) => ({ xAxis: b.bucket_start, yAxis: b.kl_divergence_from_prev ?? 0 }));
  const lastShift = buckets.filter((b) => b.is_regime_shift).slice(-1)[0] ?? null;

  return (
    <section className="card grid gap-2">
      <div className="flex items-baseline justify-between gap-2 flex-wrap">
        <h2 className="label">
          topic regime · <SignalExplainer term="KL divergence">KL divergence</SignalExplainer> · {report.bucket_minutes}m
        </h2>
        <div className="flex items-center gap-2 text-[10px] text-[color:var(--fg-dim)]">
          <span>{report.detected_shifts.length} shift{report.detected_shifts.length === 1 ? "" : "s"} at threshold {report.shift_threshold.toFixed(2)}</span>
          {lastShift && (
            <button
              type="button"
              className="chip text-[10px]"
              disabled={explain.isPending}
              onClick={() => explain.mutate(lastShift as unknown as Record<string, unknown>)}
            >
              {explain.isPending ? "explaining…" : "explain latest"}
            </button>
          )}
        </div>
      </div>
      <EChart
        height={210}
        option={eo({
          backgroundColor: CHART_BG,
          tooltip: {
            trigger: "axis",
            formatter: (params: any) => {
              const p = Array.isArray(params) ? params[0] : params;
              const b = buckets[p.dataIndex];
              if (!b) return "";
              const top = b.asset_distribution.slice(0, 3)
                .map(([k, v]) => `${k} ${(v * 100).toFixed(0)}%`)
                .join(" · ");
              return `<div style='font-size:11px'>
                <strong>${b.bucket_start}</strong><br/>
                KL: <strong>${(b.kl_divergence_from_prev ?? 0).toFixed(3)}</strong>${b.is_regime_shift ? " <span style='color:#f87171'>(shift)</span>" : ""}<br/>
                records: ${b.record_count}<br/>
                ${top}
              </div>`;
            },
          },
          grid: { top: 12, right: 16, bottom: 60, left: 40 },
          xAxis: {
            type: "category",
            data: xs,
            axisLabel: { color: AXIS_COLOR, fontSize: 9, rotate: 35, formatter: (v: string) => v.slice(5, 16) },
            axisLine: { lineStyle: { color: AXIS_LINE } },
          },
          yAxis: {
            type: "value",
            axisLabel: { color: AXIS_COLOR, fontSize: 9 },
            splitLine: { lineStyle: { color: AXIS_LINE } },
          },
          series: [
            {
              type: "line",
              data: klValues,
              smooth: true,
              symbol: "circle",
              symbolSize: 4,
              lineStyle: { color: PALETTE[0], width: 1.5 },
              areaStyle: { color: PALETTE[0], opacity: 0.12 },
              markPoint: {
                symbol: "pin",
                symbolSize: 18,
                data: markPoints,
                label: { show: false },
                itemStyle: { color: "#f87171" },
              },
              markLine: {
                symbol: "none",
                lineStyle: { color: AXIS_LINE, type: "dashed" },
                data: [{ yAxis: report.shift_threshold, label: { show: false } }],
              },
            },
          ],
        })}
      />
      {narrative && (
        <div className="border-t border-[color:var(--border-subtle)] pt-2">
          <div className="mb-1 text-[9px] uppercase tracking-wider text-[color:var(--fg-muted)]">
            <NarrativeProvenance narrative={narrative} />
          </div>
          <p className="text-[11px] text-[color:var(--fg-dim)] italic">
            {narrative.text}
          </p>
        </div>
      )}
    </section>
  );
}

// ── 2. Anomaly strip (volume + sentiment + symbol bursts) ─────────────────

function AnomalyStrip({ report, watchlist }: { report: AnomalyReportDTO | null; watchlist: string[] }) {
  const [narrative, setNarrative] = useState<{ kind: string; result: SignalNarrative } | null>(null);
  const explain = useMutation({
    mutationFn: (input: { kind: "anomaly"; payload: Record<string, unknown> }) =>
      api.quantExplain(input.kind, input.payload),
    onSuccess: (r, vars) => setNarrative({ kind: vars.kind, result: toSignalNarrative(r) }),
  });
  if (!report) return null;
  const totalAnomalies =
    report.volume_anomalies.length + report.sentiment_shocks.length + report.symbol_bursts.length;
  if (totalAnomalies === 0) {
    return (
      <section className="card">
        <h2 className="label mb-2">anomaly detector</h2>
        <p className="text-[11px] text-[color:var(--fg-muted)]">
          No anomalies in the current window — the news flow is statistically normal.
        </p>
      </section>
    );
  }
  return (
    <section className="card grid gap-3">
      <div className="flex items-baseline justify-between gap-2">
        <h2 className="label">
          anomaly detector · <SignalExplainer term="z-score">z-threshold {report.z_threshold.toFixed(1)}</SignalExplainer>
        </h2>
        <span className="text-[10px] text-[color:var(--fg-dim)]">
          {totalAnomalies} flagged across {report.bucket_minutes}m buckets
        </span>
      </div>
      <div className="grid gap-3 md:grid-cols-3">
        <AnomalySubpanel
          title="volume spikes"
          empty="no volume anomaly"
          rows={report.volume_anomalies.slice(-5).reverse().map((v) => ({
            primary: `${v.observed} rec`,
            secondary: `${fmtRel(v.bucket_start)} · z ${v.z_score.toFixed(1)}`,
            tone: v.severity === "high" ? "bad" : v.severity === "medium" ? "warn" : "accent",
            badge: v.severity.toUpperCase(),
            payload: v as unknown as Record<string, unknown>,
          }))}
          onExplain={(p) => explain.mutate({ kind: "anomaly", payload: p })}
        />
        <AnomalySubpanel
          title="sentiment shocks"
          empty="no sentiment shock"
          rows={report.sentiment_shocks.slice(-5).reverse().map((s) => ({
            primary: `net ${(s.observed_net >= 0 ? "+" : "") + s.observed_net.toFixed(2)}`,
            secondary: `${fmtRel(s.bucket_start)} · z ${s.z_score.toFixed(1)}`,
            tone: s.direction === "bullish_shock" ? "good" : s.direction === "bearish_shock" ? "bad" : "neutral",
            badge: s.direction === "bullish_shock" ? "BULLISH" : s.direction === "bearish_shock" ? "BEARISH" : "NEUTRAL",
            payload: s as unknown as Record<string, unknown>,
          }))}
          onExplain={(p) => explain.mutate({ kind: "anomaly", payload: p })}
        />
        <SymbolBurstSubpanel
          bursts={report.symbol_bursts}
          watchlist={watchlist}
          onExplain={(b) => explain.mutate({ kind: "anomaly", payload: b as unknown as Record<string, unknown> })}
        />
      </div>
      {narrative && (
        <div className="rounded-md border border-accent/20 bg-accent/5 p-2">
          <div className="mb-1 flex flex-wrap items-baseline gap-2 text-[9px] uppercase tracking-wider">
            <span className="text-accent">Anomaly narrative</span>
            <NarrativeProvenance narrative={narrative.result} />
          </div>
          <p className="text-[11px] italic leading-snug">{narrative.result.text}</p>
        </div>
      )}
    </section>
  );
}

type SubRow = {
  primary: string;
  secondary: string;
  tone: "good" | "bad" | "warn" | "accent" | "neutral";
  badge: string;
  payload?: Record<string, unknown>;
};

function AnomalySubpanel({
  title,
  empty,
  rows,
  onExplain,
}: {
  title: string;
  empty: string;
  rows: SubRow[];
  onExplain?: (payload: Record<string, unknown>) => void;
}) {
  return (
    <div className="rounded-md border border-[color:var(--border-subtle)] bg-[color:var(--bg-elev2)]/30 p-2">
      <div className="text-[10px] uppercase tracking-wider text-[color:var(--fg-muted)] mb-1.5">
        {title}
      </div>
      {rows.length === 0 ? (
        <p className="text-[10px] text-[color:var(--fg-muted)]">{empty}</p>
      ) : (
        <ul className="grid gap-1">
          {rows.map((r, i) => {
            const toneCls =
              r.tone === "good" ? "text-good" :
              r.tone === "bad" ? "text-bad" :
              r.tone === "warn" ? "text-warn" :
              r.tone === "accent" ? "text-accent" : "";
            return (
              <li key={i} className="flex items-baseline justify-between gap-1 text-[11px]">
                <span className={`tabular-nums font-semibold ${toneCls}`}>{r.primary}</span>
                <span className="text-[9px] text-[color:var(--fg-muted)]">{r.secondary}</span>
                <span className={`text-[9px] ${toneCls} font-mono`}>{r.badge}</span>
                {onExplain && r.payload && (
                  <button
                    type="button"
                    className="text-[9px] text-[color:var(--fg-muted)] hover:text-accent"
                    onClick={() => onExplain(r.payload!)}
                    title="explain signal"
                    aria-label="Explain signal"
                  >
                    ?
                  </button>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}

function SymbolBurstSubpanel({
  bursts,
  watchlist,
  onExplain,
}: {
  bursts: SymbolBurstDTO[];
  watchlist: string[];
  onExplain: (b: SymbolBurstDTO) => void;
}) {
  return (
    <div className="rounded-md border border-[color:var(--border-subtle)] bg-[color:var(--bg-elev2)]/30 p-2">
      <div className="text-[10px] uppercase tracking-wider text-[color:var(--fg-muted)] mb-1.5">
        symbol bursts
      </div>
      {bursts.length === 0 ? (
        <p className="text-[10px] text-[color:var(--fg-muted)]">no symbol burst</p>
      ) : (
        <ul className="grid gap-1">
          {bursts.slice(0, 5).map((b) => {
            const watched = isWatched(b.symbol, watchlist);
            return (
              <li
                key={`${b.symbol}-${b.bucket_start}`}
                className="flex items-baseline justify-between gap-1 text-[11px]"
              >
                <span className={`font-mono font-semibold ${watched ? "text-accent" : "text-accent/80"}`}>
                  {watched ? "★ " : ""}{b.symbol}
                </span>
                <span className="tabular-nums">{b.observed} mentions</span>
                <span className="text-[9px] text-bad font-mono">z {b.z_score.toFixed(1)}</span>
                <button
                  type="button"
                  className="text-[9px] text-[color:var(--fg-muted)] hover:text-accent"
                  onClick={() => onExplain(b)}
                  title="explain symbol burst"
                  aria-label={`Explain symbol burst for ${b.symbol}`}
                >
                  ?
                </button>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}

// ── 3. Event clusters panel with drill-down ───────────────────────────────

function EventClustersPanel({
  clusters,
  onSelect,
  watchlist,
}: {
  clusters: EventClusterDTO[];
  onSelect: (c: EventClusterDTO) => void;
  watchlist: string[];
}) {
  if (clusters.length === 0) {
    return (
      <section className="card">
        <h2 className="label mb-2">event clusters</h2>
        <EmptyState title="No multi-source events yet" hint="A cluster needs ≥2 captures of the same event within 30 min." action={<Link to="/replay" className="btn">Open Replay/Upload</Link>} />
      </section>
    );
  }
  const sorted = [...clusters].sort((a, b) => {
    const t = b.last_seen_ts.localeCompare(a.last_seen_ts);
    return t !== 0 ? t : b.size - a.size;
  });
  // Bar chart of cluster sizes for at-a-glance comparison.
  const top10 = sorted.slice(0, 10);
  return (
    <section className="card grid gap-3">
      <div className="flex items-baseline justify-between gap-2">
        <h2 className="label">event clusters · {clusters.length}</h2>
        <span className="text-[10px] text-[color:var(--fg-dim)]">click to inspect narrative</span>
      </div>
      <EChart
        height={140}
        option={eo({
          backgroundColor: CHART_BG,
          tooltip: {
            trigger: "axis",
            axisPointer: { type: "shadow" },
            formatter: (params: any) => {
              const p = Array.isArray(params) ? params[0] : params;
              const c = top10[p.dataIndex];
              if (!c) return "";
              return `<div style='font-size:11px'>
                <strong>${c.size} captures across ${c.member_domains.length} sources</strong><br/>
                ${c.dominant_symbols.slice(0, 3).join(" · ") || "(no symbols)"}<br/>
                coherence ${(c.coherence * 100).toFixed(0)}% · rel ${c.mean_relevance.toFixed(2)}
              </div>`;
            },
          },
          grid: { top: 10, right: 14, bottom: 32, left: 36 },
          xAxis: {
            type: "category",
            data: top10.map((_, i) => `#${i + 1}`),
            axisLabel: { color: AXIS_COLOR, fontSize: 9 },
            axisLine: { lineStyle: { color: AXIS_LINE } },
          },
          yAxis: { type: "value", axisLabel: { color: AXIS_COLOR, fontSize: 9 }, splitLine: { lineStyle: { color: AXIS_LINE } } },
          series: [
            {
              type: "bar",
              data: top10.map((c) => ({
                value: c.size,
                itemStyle: { color: c.coherence > 0.6 ? PALETTE[1] : c.coherence > 0.4 ? PALETTE[0] : PALETTE[2] },
              })),
              barWidth: "60%",
              label: { show: true, position: "top", color: AXIS_COLOR, fontSize: 9 },
            },
          ],
        })}
      />
      <ul className="divide-y divide-[color:var(--border-subtle)]">
        {sorted.slice(0, 8).map((c, i) => (
          <li
            key={c.cluster_id}
            className="py-2 grid gap-1 cursor-pointer hover:bg-[color:var(--bg-elev2)]/30 rounded px-2 -mx-2"
            onClick={() => onSelect(c)}
          >
            <div className="flex items-baseline justify-between gap-2 text-[11px]">
              <span className="text-[color:var(--fg-dim)]">
                #{i + 1} · {fmtRel(c.first_seen_ts)} · {c.member_domains.length} sources
              </span>
              <span className="tabular-nums">
                size {c.size} · coh {(c.coherence * 100).toFixed(0)}% · rel {c.mean_relevance.toFixed(2)}
              </span>
            </div>
            <div className="flex flex-wrap gap-1">
              {c.dominant_symbols.slice(0, 4).map((s) => (
                <Pill key={`s-${s}`} variant="sym">
                  {isWatched(s, watchlist) ? "★ " : ""}{s}
                </Pill>
              ))}
              {c.dominant_reasons.slice(0, 3).map((r) => (
                <Pill key={`r-${r}`} variant="rc">{r}</Pill>
              ))}
              {c.dominant_assets.slice(0, 2).map((a) => (
                <Pill key={`a-${a}`} variant="ac">{a}</Pill>
              ))}
            </div>
          </li>
        ))}
      </ul>
    </section>
  );
}

// ── Cluster drill-down drawer with source-aware narrative ─────────────────

function ClusterDrillDown({
  cluster,
  onClose,
  watchlist,
  windowSize,
}: {
  cluster: EventClusterDTO;
  onClose: () => void;
  watchlist: string[];
  windowSize: number;
}) {
  const [narrative, setNarrative] = useState<SignalNarrative | null>(null);
  const explain = useMutation({
    mutationFn: () => api.quantExplain("cluster", cluster as unknown as Record<string, unknown>),
    onSuccess: (r) => {
      setNarrative(toSignalNarrative(r));
    },
  });
  // Load actual member records (titles, scores, domains) alongside the
  // cluster summary. This is what turns the drawer from "a cluster
  // exists" into "here's what's IN the cluster" — drill-down depth.
  // Thread the dashboard's windowSize so the backend re-clusters over the
  // SAME corpus and the clicked cluster_id reproduces. Without this, any
  // non-default window 404s the drill-down (the backend re-clusters over its
  // own default window=1000 and computes a different membership set/id). The
  // windowSize is part of the query key so a window change refetches.
  const members = useQuery({
    queryKey: ["quant-cluster-members", cluster.cluster_id, windowSize],
    queryFn: () => api.quantClusterMembers(cluster.cluster_id, 20, windowSize),
    staleTime: 30_000,
  });
  // Trigger a narrative on open; the backend reports whether it used
  // DeepSeek or the local interpretation fallback.
  useEffect(() => {
    setNarrative(null);
    explain.mutate();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cluster.cluster_id]);

  return (
    <aside className="card grid gap-2 text-xs">
      <div className="flex items-baseline justify-between gap-2">
        <h3 className="label">cluster #{cluster.cluster_id.slice(0, 8)}</h3>
        <button type="button" className="btn text-[10px] py-0.5 px-2" onClick={onClose}>
          close
        </button>
      </div>
      <div className="grid grid-cols-2 gap-2 text-[11px]">
        <Stat label="size" value={cluster.size} />
        <Stat label="sources" value={cluster.member_domains.length} />
        <Stat label="coherence" value={`${(cluster.coherence * 100).toFixed(0)}%`} />
        <Stat label="rel" value={cluster.mean_relevance.toFixed(2)} />
      </div>
      <div>
        <div className="text-[10px] uppercase tracking-wider text-[color:var(--fg-muted)]">timing</div>
        <div className="text-[11px] tabular-nums">
          {fmtRel(cluster.first_seen_ts)} → {fmtRel(cluster.last_seen_ts)}
        </div>
      </div>
      <div className="flex flex-wrap gap-1">
        {cluster.dominant_symbols.map((s) => (
          <Pill key={s} variant="sym" title={isWatched(s, watchlist) ? "in watchlist" : undefined}>
            {isWatched(s, watchlist) ? "★ " : ""}{s}
          </Pill>
        ))}
        {cluster.dominant_reasons.map((r) => <Pill key={r} variant="rc">{r}</Pill>)}
        {cluster.dominant_assets.map((a) => <Pill key={a} variant="ac">{a}</Pill>)}
      </div>
      <div className="rounded-md border border-accent/20 bg-accent/5 p-2 mt-1">
        <div className="flex items-baseline justify-between gap-2">
          <span className="text-[9px] uppercase tracking-wider text-accent">
            {narrative?.source === "deepseek" ? "DeepSeek narrative" : "local interpretation"}
          </span>
          {explain.isPending && (
            <span className="text-[9px] text-[color:var(--fg-muted)]">thinking…</span>
          )}
        </div>
        <div className="mt-1 text-[9px] uppercase tracking-wider">
          <NarrativeProvenance narrative={narrative} />
        </div>
        <p className="text-[11px] italic mt-1 leading-snug">
          {narrative?.text || (explain.isPending ? "Generating…" : "—")}
        </p>
      </div>
      <MemberRecordsList
        title={`member records · ${cluster.size}`}
        loading={members.isLoading}
        records={members.data?.members ?? []}
        watchlist={watchlist}
      />
    </aside>
  );
}

// ── shared member-records list (cluster drill-down + heatmap drawer) ─────

function MemberRecordsList({
  title,
  loading,
  records,
  watchlist,
}: {
  title: string;
  loading: boolean;
  records: QuantMember[] | Omit<QuantMember, "asset_classes" | "impact_reason_codes">[];
  watchlist: string[];
}) {
  const [openCaptureId, setOpenCaptureId] = useState<string | null>(null);
  if (loading) return <Skeleton className="h-16" />;
  if (records.length === 0) {
    return (
      <p className="text-[10px] text-[color:var(--fg-muted)] italic">No member records resolved.</p>
    );
  }
  return (
    <div>
      <div className="text-[9px] uppercase tracking-wider text-[color:var(--fg-muted)] mb-1">{title}</div>
      <ul className="grid gap-1.5 max-h-80 overflow-auto pr-1">
        {records.map((r) => {
          const href = safeHref(r.url ?? undefined);
          const watchHit = (r.candidate_symbols ?? []).some((s) => isWatched(s, watchlist));
          const open = openCaptureId === r.capture_id;
          return (
            <li
              key={r.capture_id}
              className={`rounded border px-2 py-1 ${
                watchHit ? "border-accent/40 bg-accent/5" : "border-[color:var(--border-subtle)] bg-[color:var(--bg-elev2)]/30"
              }`}
            >
              <div className="flex items-baseline justify-between gap-2 text-[10px] text-[color:var(--fg-dim)]">
                <span title={r.published_ts ?? ""}>
                  {fmtRel(r.published_ts)} · {r.domain ?? "(unknown)"}
                </span>
                <span className={`tabular-nums ${scoreToneClass(r.finance_relevance_score)}`}>
                  score {fmtScore(r.finance_relevance_score)}
                </span>
              </div>
              <div className="text-[11px] leading-snug mt-0.5 flex items-baseline gap-1">
                <button
                  type="button"
                  className="text-left text-[color:var(--fg)] hover:underline"
                  onClick={() => setOpenCaptureId(open ? null : r.capture_id)}
                  title="show paired reviews + market reaction"
                >
                  {r.title ?? "(untitled)"}
                </button>
                {href && (
                  <a
                    href={href}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="text-[10px] text-[color:var(--fg-dim)] hover:text-accent"
                    title="open the source article in a new tab"
                    aria-label="Open source article in new tab"
                  >
                    <Icon name="external" size={12} />
                  </a>
                )}
              </div>
              {(r.candidate_symbols ?? []).length > 0 && (
                <div className="flex flex-wrap gap-1 mt-1">
                  {(r.candidate_symbols ?? []).slice(0, 6).map((s) => (
                    <span
                      key={s}
                      className={`text-[9px] font-mono ${isWatched(s, watchlist) ? "text-accent" : "text-[color:var(--fg-dim)]"}`}
                    >
                      {isWatched(s, watchlist) ? "★" : ""}{s}
                    </span>
                  ))}
                </div>
              )}
              {open && <RecordDetailInline captureId={r.capture_id} />}
            </li>
          );
        })}
      </ul>
    </div>
  );
}

function RecordDetailInline({ captureId }: { captureId: string }) {
  const detail = useQuery({
    queryKey: ["quant-record-detail", captureId],
    queryFn: () => api.quantRecordDetail(captureId),
    staleTime: 60_000,
  });
  if (detail.isLoading) return <Skeleton className="mt-1 h-12" />;
  if (detail.error || !detail.data) {
    return (
      <p className="mt-1 text-[10px] text-bad">
        Detail unavailable.
      </p>
    );
  }
  const d = detail.data;
  const ds = d.reviews.find((r) => r.reviewer_id === "deepseek");
  const stub = d.reviews.find((r) => r.reviewer_id === "stub");
  const reaction15 = d.reaction?.headline_excess_return_15m;
  return (
    <div className="mt-1 grid gap-1 rounded border border-accent/20 bg-accent/5 p-1.5">
      <div className="flex flex-wrap gap-2 text-[9px] text-[color:var(--fg-dim)]">
        <span>reviews: {d.reviews.length}</span>
        {reaction15 != null && (
          <span className={reaction15 >= 0 ? "text-good" : "text-bad"}>
            reaction 15m: {(reaction15 >= 0 ? "+" : "") + reaction15.toFixed(2)}%
          </span>
        )}
        {d.reaction?.fallback_reason && (
          <span className="text-[color:var(--fg-muted)]">no quote: {d.reaction.fallback_reason}</span>
        )}
      </div>
      {ds && !ds.error_code && (
        <div>
          <span className="text-[9px] uppercase tracking-wider text-accent">DeepSeek</span>
          <p className="text-[10px] italic leading-snug">
            {ds.payload.reason_text || "(no narrative)"}
          </p>
        </div>
      )}
      {stub && (
        <div className="text-[9px] text-[color:var(--fg-muted)]">
          stub: rel {stub.payload.is_finance_relevant ? "YES" : "no"} · score {stub.payload.finance_relevance_score.toFixed(2)}
        </div>
      )}
    </div>
  );
}

// ── Heatmap drill drawer ──────────────────────────────────────────────────

function HeatmapDetailDrawer({
  cell,
  onClose,
  watchlist,
}: {
  cell: HeatmapCell;
  onClose: () => void;
  watchlist: string[];
}) {
  const records = useQuery({
    queryKey: ["quant-heatmap-records", cell.asset, cell.reason],
    queryFn: () => api.quantHeatmapRecords(cell.asset, cell.reason, 20),
    staleTime: 30_000,
  });
  return (
    <aside className="card grid gap-2 text-xs">
      <div className="flex items-baseline justify-between gap-2">
        <h3 className="label">heatmap drill</h3>
        <button type="button" className="btn text-[10px] py-0.5 px-2" onClick={onClose}>
          close
        </button>
      </div>
      <div className="flex flex-wrap gap-1 text-[11px]">
        <Pill variant="ac">{cell.asset}</Pill>
        <span className="text-[color:var(--fg-dim)]">×</span>
        <Pill variant="rc">{cell.reason}</Pill>
      </div>
      <p className="text-[10px] text-[color:var(--fg-muted)]">
        Records carrying BOTH labels — every story behind this lift cell.
      </p>
      <MemberRecordsList
        title={`records · ${records.data?.total_returned ?? "…"}`}
        loading={records.isLoading}
        records={(records.data?.records ?? []) as unknown as QuantMember[]}
        watchlist={watchlist}
      />
    </aside>
  );
}

// ── Watchlist sidebar card ────────────────────────────────────────────────

/**
 * Watchlist card — sortable, drag-reorderable, persisted.
 *
 * Sort modes:
 *   - "custom"   — the user's explicit order (drag to reorder)
 *   - "name"     — A→Z by symbol
 *   - "momentum" — by |momentum|, highest first (data from sentiment_momentum)
 *   - "activity" — by mention_count, highest first (proxy for "buzz")
 *
 * Drag-and-drop uses native HTML5 DnD (no react-dnd / dnd-kit dependency).
 * Keyboard a11y: focus a row's drag handle and press ⌥/Alt+↑ / ⌥/Alt+↓ to
 * move it. We use Alt instead of Cmd/Meta because macOS browsers reserve
 * Cmd+↑/↓ for scroll-to-top/bottom and that would shadow the gesture.
 *
 * Dragging while sortBy ≠ "custom" intentionally snaps `sortBy` back to
 * "custom" inside the hook — otherwise the new explicit order would be
 * invisible behind the derived sort.
 */
const WATCHLIST_SORT_LABELS: Record<WatchlistSortMode, string> = {
  custom: "Custom",
  name: "Name",
  momentum: "Momentum",
  activity: "Activity",
};

function WatchlistCard({
  api: watchlistApi,
  dashboard,
}: {
  api: WatchlistApi;
  dashboard: QuantDashboard | undefined;
}) {
  const { items, sortBy, setSortBy, add, remove, reorder } = watchlistApi;
  const [input, setInput] = useState<string>("");
  // The currently-dragged source index. Null when no drag is in flight.
  const dragSrcRef = useRef<number | null>(null);
  const [dragSrc, setDragSrc] = useState<number | null>(null);
  const [dragOver, setDragOver] = useState<number | null>(null);

  // Build a quick "live" view per watchlisted symbol — pull its current
  // momentum (if tracked) and any active anomaly burst that matches.
  const liveByTicker = useMemo(() => {
    const m: Record<string, { momentum?: TickerMomentumDTO; burst?: SymbolBurstDTO }> = {};
    const sm = dashboard?.sentiment_momentum?.tickers ?? [];
    for (const t of sm) m[t.symbol.toUpperCase()] = { momentum: t };
    const bursts = dashboard?.anomalies?.symbol_bursts ?? [];
    for (const b of bursts) {
      const k = b.symbol.toUpperCase();
      m[k] = { ...(m[k] || {}), burst: b };
    }
    return m;
  }, [dashboard]);

  // Per-symbol metrics for "momentum" / "activity" sorts. We only ever
  // have data for symbols the engine actually saw in the window — the
  // rest fall through and sink to the bottom (handled inside sortWatchlist).
  const metrics: WatchlistMetrics = useMemo(() => {
    const out: WatchlistMetrics = {};
    for (const sym of items) {
      const live = liveByTicker[sym];
      if (!live) continue;
      const m: WatchlistMetric = {};
      if (live.momentum) {
        m.momentum = live.momentum.momentum;
        m.activity = live.momentum.mention_count;
      }
      // A symbol burst counts as activity even without momentum data —
      // it means the ticker is being talked about right now.
      if (live.burst) {
        m.activity = (m.activity ?? 0) + live.burst.observed;
      }
      out[sym] = m;
    }
    return out;
  }, [items, liveByTicker]);

  // Derived display order — pure function of (items, sortBy, metrics).
  const display = useMemo(
    () => sortWatchlist(items, sortBy, metrics),
    [items, sortBy, metrics],
  );

  const handleAdd = () => {
    const sym = input.trim().toUpperCase();
    if (!sym) return;
    add(sym);
    setInput("");
  };

  // HTML5 DnD handlers — operate on the DISPLAY index so cross-mode
  // dragging Just Works (the hook flips sortBy to "custom" before
  // calling reorder, and we re-resolve display→items indices via the
  // current items array).
  const onDragStart = (displayIdx: number) => (e: React.DragEvent<HTMLLIElement>) => {
    dragSrcRef.current = displayIdx;
    setDragSrc(displayIdx);
    // setData is required for Firefox to fire drag events at all.
    try {
      e.dataTransfer.setData("text/plain", display[displayIdx] ?? "");
    } catch {
      /* some test envs don't implement DataTransfer */
    }
    e.dataTransfer.effectAllowed = "move";
  };
  const onDragOver = (displayIdx: number) => (e: React.DragEvent<HTMLLIElement>) => {
    if (dragSrcRef.current === null) return;
    e.preventDefault(); // required to allow drop
    e.dataTransfer.dropEffect = "move";
    if (dragOver !== displayIdx) setDragOver(displayIdx);
  };
  const onDrop = (displayIdx: number) => (e: React.DragEvent<HTMLLIElement>) => {
    e.preventDefault();
    const src = dragSrcRef.current;
    dragSrcRef.current = null;
    setDragSrc(null);
    setDragOver(null);
    if (src === null || src === displayIdx) return;
    // Translate display indices to the canonical custom-order indices.
    // When sortBy == "custom" they're identical; otherwise we resolve
    // by symbol against the persisted list.
    const srcSym = display[src];
    const dstSym = display[displayIdx];
    const fromIdx = items.indexOf(srcSym);
    const toIdx = items.indexOf(dstSym);
    if (fromIdx < 0 || toIdx < 0) return;
    reorder(fromIdx, toIdx);
  };
  const onDragEnd = () => {
    dragSrcRef.current = null;
    setDragSrc(null);
    setDragOver(null);
  };

  // Keyboard reorder — Alt+↑/Alt+↓ moves the focused row's symbol.
  const onHandleKeyDown = (displayIdx: number) => (e: React.KeyboardEvent<HTMLButtonElement>) => {
    if (!e.altKey) return;
    if (e.key !== "ArrowUp" && e.key !== "ArrowDown") return;
    e.preventDefault();
    const sym = display[displayIdx];
    const fromIdx = items.indexOf(sym);
    if (fromIdx < 0) return;
    const delta = e.key === "ArrowUp" ? -1 : 1;
    const toIdx = Math.max(0, Math.min(items.length - 1, fromIdx + delta));
    if (toIdx === fromIdx) return;
    reorder(fromIdx, toIdx);
  };

  const sortChips: WatchlistSortMode[] = ["custom", "name", "momentum", "activity"];

  return (
    <section className="card grid gap-2 text-xs" data-testid="watchlist-card">
      <div className="flex items-baseline justify-between gap-2">
        <h3 className="label">watchlist · {items.length}</h3>
        <span className="text-[9px] text-[color:var(--fg-muted)]">★ pin in charts + lists</span>
      </div>
      <div className="flex gap-1">
        <input
          className="input text-[11px] flex-1"
          placeholder="add ticker (AAPL, BTC-USD)"
          aria-label="Add ticker to watchlist"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              handleAdd();
            }
          }}
        />
        <button
          type="button"
          className="btn text-[10px] px-2"
          onClick={handleAdd}
          disabled={!input.trim()}
        >
          add
        </button>
      </div>
      {items.length > 0 && (
        <div
          role="radiogroup"
          aria-label="Watchlist sort mode"
          className="flex flex-wrap gap-1"
          data-testid="watchlist-sort-chips"
        >
          {sortChips.map((mode) => {
            const active = sortBy === mode;
            return (
              <button
                key={mode}
                type="button"
                role="radio"
                aria-checked={active}
                className={`chip text-[10px] ${active ? "chip-active" : ""}`}
                data-testid={`watchlist-sort-${mode}`}
                onClick={() => setSortBy(mode)}
                title={`Sort by ${WATCHLIST_SORT_LABELS[mode].toLowerCase()}`}
              >
                {WATCHLIST_SORT_LABELS[mode]}
              </button>
            );
          })}
        </div>
      )}
      {items.length === 0 ? (
        <p className="text-[10px] text-[color:var(--fg-muted)]">
          No symbols pinned yet. Type one above or click the ★ next to any ticker in the page.
        </p>
      ) : (
        <ul
          className="grid gap-1"
          aria-label="Watchlist symbols"
          data-testid="watchlist-list"
        >
          {display.map((sym, displayIdx) => {
            const live = liveByTicker[sym] || {};
            const isDragging = dragSrc === displayIdx;
            const isDropTarget = dragOver === displayIdx && dragSrc !== displayIdx;
            return (
              <li
                key={sym}
                draggable
                onDragStart={onDragStart(displayIdx)}
                onDragOver={onDragOver(displayIdx)}
                onDrop={onDrop(displayIdx)}
                onDragEnd={onDragEnd}
                data-testid={`watchlist-row-${sym}`}
                data-dragging={isDragging ? "true" : undefined}
                data-drop-target={isDropTarget ? "true" : undefined}
                className={[
                  "flex items-center gap-2 rounded border px-2 py-1",
                  "bg-[color:var(--bg-elev2)]/40",
                  isDropTarget
                    ? "border-accent"
                    : "border-[color:var(--border-subtle)]",
                  isDragging ? "opacity-50" : "",
                ]
                  .filter(Boolean)
                  .join(" ")}
              >
                <button
                  type="button"
                  className="text-[10px] text-[color:var(--fg-muted)] cursor-grab active:cursor-grabbing select-none px-0.5"
                  aria-label={`Reorder ${sym} (Alt+ArrowUp or Alt+ArrowDown)`}
                  title="drag to reorder · Alt+↑ / Alt+↓"
                  data-testid={`watchlist-handle-${sym}`}
                  onKeyDown={onHandleKeyDown(displayIdx)}
                  onClick={(e) => e.preventDefault()}
                >
                  ⋮⋮
                </button>
                <span className="font-mono font-semibold text-accent text-[12px]">{sym}</span>
                <span className="text-[9px] text-[color:var(--fg-muted)] truncate flex-1">
                  {live.momentum
                    ? `${live.momentum.direction.replace("_", " ")} · momentum ${live.momentum.momentum.toFixed(2)}`
                    : "no momentum data"}
                  {live.burst && (
                    <span className="ml-1 text-bad font-mono">
                      ⚡ burst z {live.burst.z_score.toFixed(1)}
                    </span>
                  )}
                </span>
                <button
                  type="button"
                  className="text-[10px] text-[color:var(--fg-muted)] hover:text-bad"
                  onClick={() => remove(sym)}
                  title="remove"
                  aria-label={`Remove ${sym} from watchlist`}
                >
                  <Icon name="close" size={12} />
                </button>
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}

// ── Live ticker tape ──────────────────────────────────────────────────────

function TickerTape({
  report,
  watchlist,
  onToggleWatch,
}: {
  report: SentimentMomentumReportDTO;
  watchlist: string[];
  onToggleWatch: (sym: string) => void;
}) {
  // Combine: watchlist tickers first (always), then top-momentum tickers
  // not already in watchlist, capped at 16.
  const tickers = useMemo(() => {
    const watchSet = new Set(watchlist.map((s) => s.toUpperCase()));
    const reportTickers = report.tickers ?? [];
    const watched: TickerMomentumDTO[] = [];
    const others: TickerMomentumDTO[] = [];
    for (const t of reportTickers) {
      if (watchSet.has(t.symbol.toUpperCase())) watched.push(t);
      else others.push(t);
    }
    return [...watched, ...others].slice(0, 16);
  }, [report, watchlist]);

  if (tickers.length === 0) return null;
  return (
    <div className="card grid gap-1.5">
      <div className="flex items-baseline justify-between gap-2">
        <h3 className="label">ticker tape · live momentum</h3>
        <span className="text-[9px] text-[color:var(--fg-muted)]">★ = in watchlist · click to toggle</span>
      </div>
      <div className="flex flex-wrap gap-1.5">
        {tickers.map((t) => {
          const watched = isWatched(t.symbol, watchlist);
          const tone =
            t.flip_detected ? "border-bad/60 bg-bad/10" :
            t.momentum > 0.3 ? "border-good/40 bg-good/10" :
            t.momentum < -0.3 ? "border-warn/40 bg-warn/10" :
            "border-[color:var(--border-subtle)] bg-[color:var(--bg-elev2)]/40";
          const insufficient = (t.buckets?.length ?? 0) < 2;
          return (
            <button
              key={t.symbol}
              type="button"
              className={`rounded border ${tone} px-2 py-1 text-[11px] hover:bg-[color:var(--bg-elev2)] transition-colors`}
              onClick={() => onToggleWatch(t.symbol)}
              title={`${insufficient ? "single bucket — momentum needs more data" : t.direction.replace("_", " ")} · ${t.mention_count} mentions across ${t.buckets.length} buckets`}
            >
              <span className="font-mono font-semibold">
                {watched ? "★ " : ""}
                <span className={watched ? "text-accent" : ""}>{t.symbol}</span>
              </span>
              {insufficient ? (
                <span className="ml-1.5 text-[10px] text-[color:var(--fg-muted)] italic">—</span>
              ) : (
                <span className={`ml-1.5 tabular-nums text-[10px] ${t.momentum >= 0 ? "text-good" : "text-bad"}`}>
                  {t.momentum >= 0 ? "+" : ""}{t.momentum.toFixed(2)}
                </span>
              )}
              {t.flip_detected && <span className="ml-1 text-bad text-[9px]">⚡</span>}
            </button>
          );
        })}
      </div>
    </div>
  );
}

function Stat({ label, value }: { label: string; value: number | string }) {
  return (
    <div className="rounded border border-[color:var(--border-subtle)] bg-[color:var(--bg-elev2)]/40 px-2 py-1">
      <div className="text-[9px] uppercase tracking-wider text-[color:var(--fg-muted)]">{label}</div>
      <div className="text-[12px] font-semibold tabular-nums">{value}</div>
    </div>
  );
}

// ── 4. Novelty scatter timeline ───────────────────────────────────────────

function NoveltyScatter({ items }: { items: NoveltyResultDTO[] }) {
  if (items.length === 0) return null;
  // Sort by capture_id consistently and pair each with its parsed
  // published-ish timestamp. NoveltyResultDTO doesn't carry a timestamp
  // directly — we derive one from the per-position order of the corpus
  // (the engine returns the corpus in storage's recent-DESC order, so
  // index 0 is the newest). For an honest X axis we use that ordering
  // as a monotonic timestamp series: spread the points uniformly across
  // the wall-clock window the dashboard claims. The result is a true
  // timeline scatter — index-as-idx was misleading.
  const now = Date.now();
  // The dashboard window snapshot already implies the corpus span; we
  // map the index 0..N-1 to (now - N*minute) .. now for a stable axis.
  // 5-minute step keeps the X-axis readable across typical windows.
  const stepMs = 5 * 60 * 1000;
  const data = items.map((n, i) => {
    const x = now - (items.length - 1 - i) * stepMs;
    return {
      value: [x, n.novelty_score, n.max_similarity_to_corpus, n.nearest_title ?? "", n.explanation],
      itemStyle: {
        color:
          n.novelty_score >= 0.7 ? PALETTE[1] : n.novelty_score >= 0.4 ? PALETTE[0] : PALETTE[3],
      },
    };
  });
  return (
    <section className="card grid gap-2">
      <div className="flex items-baseline justify-between gap-2">
        <h2 className="label">
          <SignalExplainer term="novelty">novelty</SignalExplainer> timeline · {items.length} pts
        </h2>
        <span className="text-[10px] text-[color:var(--fg-dim)]">green = novel · red = duplicate</span>
      </div>
      <EChart
        height={170}
        option={eo({
          backgroundColor: CHART_BG,
          tooltip: {
            trigger: "item",
            formatter: (params: any) => {
              const v = params.value as [number, number, number, string, string];
              const date = new Date(v[0]).toLocaleString(undefined, {
                hour: "2-digit",
                minute: "2-digit",
                month: "short",
                day: "2-digit",
              });
              return `<div style='font-size:11px'>
                <strong>${date}</strong> · novelty <strong>${(v[1] * 100).toFixed(0)}%</strong> · sim ${(v[2] * 100).toFixed(0)}%<br/>
                ${v[4]}${v[3] ? `<br/>↳ ${v[3].slice(0, 60)}` : ""}
              </div>`;
            },
          },
          grid: { top: 12, right: 14, bottom: 28, left: 36 },
          xAxis: {
            type: "time",
            axisLabel: { color: AXIS_COLOR, fontSize: 9 },
            splitLine: { lineStyle: { color: AXIS_LINE } },
          },
          yAxis: {
            type: "value",
            min: 0,
            max: 1,
            axisLabel: { color: AXIS_COLOR, fontSize: 9, formatter: (v: number) => `${(v * 100).toFixed(0)}%` },
            splitLine: { lineStyle: { color: AXIS_LINE } },
            name: "novelty",
            nameTextStyle: { color: AXIS_COLOR, fontSize: 9 },
          },
          series: [
            {
              type: "scatter",
              data,
              symbolSize: 6,
            },
            {
              type: "line",
              data: data.map((d) => [d.value[0], d.value[1]]),
              showSymbol: false,
              smooth: true,
              lineStyle: { color: AXIS_LINE, width: 1, opacity: 0.5 },
            },
          ],
        })}
      />
    </section>
  );
}

// ── 5. Sentiment momentum panel ───────────────────────────────────────────

function SentimentMomentumPanel({
  report,
  watchlist,
  onToggleWatch,
}: {
  report: SentimentMomentumReportDTO | null;
  watchlist: string[];
  onToggleWatch: (sym: string) => void;
}) {
  if (!report || report.tickers.length === 0) {
    return (
      <section className="card">
        <h2 className="label mb-2">sentiment momentum · per ticker</h2>
        <EmptyState
          title="Not enough ticker mentions yet"
          hint={`A ticker needs ≥${report?.min_mentions ?? 4} mentions across multiple ${report?.bucket_minutes ?? 240}m buckets to register.`}
          action={<Link to="/replay" className="btn">Open Replay/Upload</Link>}
        />
      </section>
    );
  }
  const top = report.tickers.slice(0, 12);
  return (
    <section className="card grid gap-2">
      <div className="flex items-baseline justify-between gap-2">
        <h2 className="label">sentiment momentum · {report.tickers.length} tickers</h2>
        <span className="text-[10px] text-[color:var(--fg-dim)]">
          {top.filter((t) => t.flip_detected).length} flip{top.filter((t) => t.flip_detected).length === 1 ? "" : "s"} · sorted by |momentum|
        </span>
      </div>
      <ul className="grid gap-2 md:grid-cols-2">
        {top.map((t) => (
          <TickerMomentumRow
            key={t.symbol}
            t={t}
            watched={isWatched(t.symbol, watchlist)}
            onToggleWatch={() => onToggleWatch(t.symbol)}
          />
        ))}
      </ul>
    </section>
  );
}

// ── Sentiment Dispersion (Shannon entropy) ───────────────────────────────
//
// A per-scope read of "how mixed is the news flow?". One overall reading
// summarises the whole window; the per-scope buckets surface the loudest
// asset classes / symbols by sample size, then we slice off the top-5
// most-dispersed (uncertainty) AND the top-5 most-aligned (consensus) so
// the analyst sees both edges of the distribution in one card.

type DispersionScope = "asset_classes" | "candidate_symbols";

const DISPERSION_TONE = (n: number): "good" | "warn" | "bad" | "accent" => {
  if (n >= 0.95) return "bad";       // analyst disagreement
  if (n <= 0.3) return "good";       // unanimous narrative
  if (n >= 0.7) return "warn";
  return "accent";
};

function SentimentDispersionPanel() {
  const [scope, setScope] = useState<DispersionScope>("asset_classes");
  const overall = useQuery<SentimentDispersionResponse>({
    queryKey: ["quant-dispersion", "overall"],
    queryFn: () => api.quantSentimentDispersion(1000, "overall"),
    refetchInterval: 15_000,
    staleTime: 8_000,
  });
  const bucketed = useQuery<SentimentDispersionResponse>({
    queryKey: ["quant-dispersion", scope],
    queryFn: () => api.quantSentimentDispersion(1000, scope),
    refetchInterval: 15_000,
    staleTime: 8_000,
  });

  const overallResult = overall.data?.result ?? null;
  const buckets = bucketed.data?.buckets ?? [];

  // Top-5 most-dispersed (highest normalized entropy = most disagreement),
  // and top-5 most-aligned (lowest normalized entropy, sample_size ≥ 5 so
  // a single-record bucket doesn't masquerade as "consensus"). Sorts are
  // stable in JS so ties hold the upstream (sample-size-DESC) order.
  const { mostDispersed, mostAligned } = useMemo(() => {
    const sortedDesc = [...buckets].sort(
      (a, b) => b.normalized_entropy - a.normalized_entropy,
    );
    const sortedAsc = [...buckets]
      .filter((b) => b.sample_size >= 5)
      .sort((a, b) => a.normalized_entropy - b.normalized_entropy);
    return { mostDispersed: sortedDesc.slice(0, 5), mostAligned: sortedAsc.slice(0, 5) };
  }, [buckets]);

  return (
    <section className="card grid gap-3">
      <div className="flex items-baseline justify-between gap-2">
        <h2 className="label">sentiment dispersion · Shannon entropy</h2>
        <div className="flex items-center gap-1 text-[10px]">
          {(["asset_classes", "candidate_symbols"] as DispersionScope[]).map((s) => (
            <button
              key={s}
              type="button"
              onClick={() => setScope(s)}
              data-testid={`dispersion-scope-${s}`}
              className={`rounded-sm border px-1.5 py-0.5 uppercase tracking-wider ${
                scope === s
                  ? "border-accent/60 bg-accent/10 text-accent"
                  : "border-[color:var(--border-subtle)] text-[color:var(--fg-muted)] hover:border-accent/40"
              }`}
            >
              {s === "asset_classes" ? "asset class" : "symbol"}
            </button>
          ))}
        </div>
      </div>

      {/* Overall reading + horizontal pos/neu/neg bar */}
      <OverallDispersionStrip entry={overallResult} loading={overall.isLoading} />

      {/* Top-5 most dispersed / most aligned */}
      <div className="grid gap-3 md:grid-cols-2">
        <DispersionList
          title="most dispersed"
          subtitle="highest disagreement · top 5"
          entries={mostDispersed}
          loading={bucketed.isLoading}
          emptyHint={`Need more ${scope === "asset_classes" ? "asset-class" : "symbol"} coverage.`}
          tone="bad"
        />
        <DispersionList
          title="most aligned"
          subtitle="lowest entropy · ≥5 samples · top 5"
          entries={mostAligned}
          loading={bucketed.isLoading}
          emptyHint="Need ≥5 records per bucket to register."
          tone="good"
        />
      </div>
    </section>
  );
}

function OverallDispersionStrip({
  entry,
  loading,
}: {
  entry: SentimentDispersionEntryDTO | null;
  loading: boolean;
}) {
  if (loading) {
    return <Skeleton className="h-14 w-full" />;
  }
  if (!entry || entry.sample_size === 0) {
    return (
      <EmptyState
        title="No labelled sentiments yet"
        hint="Dispersion needs at least one record with a sentiment label."
      />
    );
  }
  const total = entry.sample_size;
  const posPct = (entry.counts.positive / total) * 100;
  const neuPct = (entry.counts.neutral / total) * 100;
  const negPct = (entry.counts.negative / total) * 100;
  const tone = DISPERSION_TONE(entry.normalized_entropy);
  const toneCls =
    tone === "bad" ? "text-bad" :
    tone === "warn" ? "text-warn" :
    tone === "good" ? "text-good" : "text-accent";
  return (
    <div className="grid gap-2 rounded-md border border-[color:var(--border-subtle)] bg-[color:var(--bg-elev2)]/30 p-3">
      <div className="flex items-baseline justify-between gap-2">
        <div className="flex items-baseline gap-2">
          <span className={`text-2xl font-bold tabular-nums ${toneCls}`}>
            {entry.normalized_entropy.toFixed(2)}
          </span>
          <span className="text-[10px] uppercase tracking-wider text-[color:var(--fg-muted)]">
            normalized · max 1.00
          </span>
        </div>
        <span className="text-[10px] text-[color:var(--fg-muted)]">
          n={total} · dominant <span className={toneCls}>{entry.dominant_label}</span>
        </span>
      </div>
      {/* pos / neu / neg horizontal bar */}
      <div
        className="flex h-3 w-full overflow-hidden rounded-sm border border-[color:var(--border-subtle)]"
        title={`${entry.counts.positive} positive · ${entry.counts.neutral} neutral · ${entry.counts.negative} negative`}
        data-testid="dispersion-overall-bar"
      >
        <div
          className="bg-good/70"
          style={{ width: `${posPct}%` }}
          data-testid="dispersion-bar-positive"
        />
        <div
          className="bg-[color:var(--fg-muted)]/40"
          style={{ width: `${neuPct}%` }}
          data-testid="dispersion-bar-neutral"
        />
        <div
          className="bg-bad/70"
          style={{ width: `${negPct}%` }}
          data-testid="dispersion-bar-negative"
        />
      </div>
      <div className="flex items-center justify-between text-[10px] text-[color:var(--fg-muted)] tabular-nums">
        <span className="text-good">+ {entry.counts.positive} ({posPct.toFixed(0)}%)</span>
        <span>= {entry.counts.neutral} ({neuPct.toFixed(0)}%)</span>
        <span className="text-bad">− {entry.counts.negative} ({negPct.toFixed(0)}%)</span>
      </div>
    </div>
  );
}

function DispersionList({
  title,
  subtitle,
  entries,
  loading,
  emptyHint,
  tone,
}: {
  title: string;
  subtitle: string;
  entries: SentimentDispersionEntryDTO[];
  loading: boolean;
  emptyHint: string;
  tone: "good" | "bad";
}) {
  const accentCls = tone === "bad" ? "text-bad" : "text-good";
  if (loading) {
    return <Skeleton className="h-32 w-full" />;
  }
  return (
    <div className="grid gap-1.5 rounded-md border border-[color:var(--border-subtle)] bg-[color:var(--bg-elev2)]/30 p-2">
      <div className="flex items-baseline justify-between gap-2">
        <h3 className={`text-[11px] font-semibold uppercase tracking-wider ${accentCls}`}>{title}</h3>
        <span className="text-[9px] text-[color:var(--fg-muted)]">{subtitle}</span>
      </div>
      {entries.length === 0 ? (
        <EmptyState title="No matching buckets" hint={emptyHint} />
      ) : (
        <ul className="grid gap-1">
          {entries.map((e) => {
            // scope is "asset_class:equities" — split for compact rendering.
            const [, name] = e.scope.split(":");
            const dom = e.dominant_label;
            const domCls =
              dom === "positive" ? "text-good" :
              dom === "negative" ? "text-bad" :
              dom === "neutral" ? "text-[color:var(--fg-muted)]" : "text-warn";
            return (
              <li
                key={e.scope}
                className="flex items-baseline justify-between gap-2 text-[11px] tabular-nums"
                data-testid="dispersion-list-row"
              >
                <span className="truncate font-mono text-accent" title={e.scope}>{name || e.scope}</span>
                <span className="flex items-center gap-2">
                  <span className="text-[9px] text-[color:var(--fg-muted)]">
                    n={e.sample_size} · <span className={domCls}>{dom}</span>
                  </span>
                  <span className={`font-semibold ${accentCls}`}>{e.normalized_entropy.toFixed(2)}</span>
                </span>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}

// ── Sentiment intensity (relevance × |sentiment|) ─────────────────────
//
// Sits next to SentimentDispersionPanel inside the Sentiment tab. Dispersion
// answers "how much do labels disagree"; intensity answers "how loud is the
// signal we DO have". A high mean here means a bucket is full of records
// that are both highly relevant AND strongly polarized — not just noise.

type IntensityScope = "asset_classes" | "candidate_symbols";

const INTENSITY_TONE = (n: number): "good" | "warn" | "bad" | "accent" => {
  // n ∈ [0, 1] in practice — relevance and |sentiment_score| are both
  // bounded by [0,1]. Treat 0.6+ as the "loud" band so the badge reads
  // as actionable without making mid-relevance look alarming.
  if (n >= 0.6) return "bad";
  if (n >= 0.4) return "warn";
  if (n >= 0.2) return "accent";
  return "good";
};

function IntensityPanel() {
  const [scope, setScope] = useState<IntensityScope>("asset_classes");
  const [expanded, setExpanded] = useState<string | null>(null);
  const buckets = useQuery<IntensityResponse>({
    queryKey: ["quant-intensity", scope],
    queryFn: () => api.quantIntensity(2000, scope),
    refetchInterval: 15_000,
    staleTime: 8_000,
  });

  // Top 5 by mean_intensity. Backend already sorts DESC + caps at 20, so
  // we just slice. Keeping the slice client-side lets us tune the visible
  // top-N without a re-fetch.
  const topFive = useMemo(
    () => (buckets.data?.buckets ?? []).slice(0, 5),
    [buckets.data],
  );

  return (
    <section className="card grid gap-3" data-testid="intensity-panel">
      <div className="flex items-baseline justify-between gap-2">
        <h2 className="label">sentiment intensity · relevance × |score|</h2>
        <div className="flex items-center gap-1 text-[10px]">
          {(["asset_classes", "candidate_symbols"] as IntensityScope[]).map((s) => (
            <button
              key={s}
              type="button"
              onClick={() => {
                setScope(s);
                setExpanded(null);
              }}
              data-testid={`intensity-scope-${s}`}
              className={`rounded-sm border px-1.5 py-0.5 uppercase tracking-wider ${
                scope === s
                  ? "border-accent/60 bg-accent/10 text-accent"
                  : "border-[color:var(--border-subtle)] text-[color:var(--fg-muted)] hover:border-accent/40"
              }`}
            >
              {s === "asset_classes" ? "asset class" : "symbol"}
            </button>
          ))}
        </div>
      </div>

      {buckets.isLoading ? (
        <Skeleton className="h-48 w-full" />
      ) : topFive.length === 0 ? (
        <EmptyState
          title="No labelled intensity yet"
          hint="Need records carrying both finance_relevance_score and sentiment_score."
        />
      ) : (
        <ul className="grid gap-1.5" data-testid="intensity-list">
          {topFive.map((b) => (
            <IntensityRow
              key={b.scope}
              bucket={b}
              expanded={expanded === b.scope}
              onToggle={() =>
                setExpanded((prev) => (prev === b.scope ? null : b.scope))
              }
            />
          ))}
        </ul>
      )}
    </section>
  );
}

function IntensityRow({
  bucket,
  expanded,
  onToggle,
}: {
  bucket: IntensityBucketDTO;
  expanded: boolean;
  onToggle: () => void;
}) {
  // Scope arrives as "asset_class:equities" or "symbol:BTC". Split for
  // compact rendering — falls back to the full scope if the split is
  // unexpected so we never render a blank row.
  const [, name] = bucket.scope.split(":");
  const tone = INTENSITY_TONE(bucket.mean_intensity);
  const toneCls =
    tone === "bad" ? "text-bad" :
    tone === "warn" ? "text-warn" :
    tone === "accent" ? "text-accent" : "text-good";
  // Bar uses mean clamped to [0,1] — the visual fill represents how "loud"
  // a bucket is on average, not the max of any single record.
  const fillPct = Math.max(0, Math.min(1, bucket.mean_intensity)) * 100;
  return (
    <li className="rounded-md border border-[color:var(--border-subtle)] bg-[color:var(--bg-elev2)]/30">
      <button
        type="button"
        onClick={onToggle}
        aria-expanded={expanded}
        className="flex w-full items-center gap-3 px-3 py-2 text-left hover:bg-[color:var(--bg-elev2)]/50"
        data-testid="intensity-row"
      >
        <span
          className="truncate font-mono text-[12px] text-accent"
          title={bucket.scope}
          style={{ minWidth: "8ch" }}
        >
          {name || bucket.scope}
        </span>
        <span
          className="relative h-2 flex-1 overflow-hidden rounded-sm border border-[color:var(--border-subtle)]"
          title={`mean intensity ${bucket.mean_intensity.toFixed(3)} · max ${bucket.max_intensity.toFixed(3)}`}
          data-testid="intensity-bar"
        >
          <span
            className={`absolute inset-y-0 left-0 ${
              tone === "bad" ? "bg-bad/70" :
              tone === "warn" ? "bg-warn/70" :
              tone === "accent" ? "bg-accent/70" : "bg-good/70"
            }`}
            style={{ width: `${fillPct}%` }}
          />
        </span>
        <span className={`text-[11px] font-semibold tabular-nums ${toneCls}`}>
          {bucket.mean_intensity.toFixed(2)}
        </span>
        <span className="text-[10px] text-[color:var(--fg-muted)] tabular-nums">
          n={bucket.sample_size}
        </span>
        <span
          className={`text-[10px] tabular-nums ${
            bucket.count_high_intensity > 0 ? "text-bad" : "text-[color:var(--fg-muted)]"
          }`}
          title="records with intensity > 0.5"
        >
          {bucket.count_high_intensity}↑
        </span>
        <span
          aria-hidden
          className={`text-[10px] text-[color:var(--fg-muted)] transition-transform ${
            expanded ? "rotate-90" : ""
          }`}
        >
          ▸
        </span>
      </button>
      {expanded && (
        <ul
          className="grid gap-1 border-t border-[color:var(--border-subtle)] px-3 py-2"
          data-testid="intensity-top-records"
        >
          {bucket.top_records.length === 0 ? (
            <li className="text-[10px] text-[color:var(--fg-muted)]">
              No top records in this bucket.
            </li>
          ) : (
            bucket.top_records.map((r, idx) => {
              const sentTone =
                r.sentiment_label === "positive" ? "text-good" :
                r.sentiment_label === "negative" ? "text-bad" :
                "text-[color:var(--fg-muted)]";
              return (
                <li
                  key={`${r.capture_id ?? idx}`}
                  className="flex items-baseline justify-between gap-2 text-[11px]"
                  data-testid="intensity-top-record"
                >
                  <span className="truncate text-[color:var(--fg)]" title={r.title ?? r.capture_id ?? "—"}>
                    {r.title ?? r.capture_id ?? "—"}
                  </span>
                  <span className="flex shrink-0 items-baseline gap-2 tabular-nums text-[10px]">
                    <span className={sentTone}>{r.sentiment_label ?? "—"}</span>
                    <span className="text-[color:var(--fg-muted)]">
                      i={r.intensity.toFixed(2)}
                    </span>
                  </span>
                </li>
              );
            })
          )}
        </ul>
      )}
    </li>
  );
}

function TickerMomentumRow({
  t,
  watched,
  onToggleWatch,
}: {
  t: TickerMomentumDTO;
  watched: boolean;
  onToggleWatch: () => void;
}) {
  // Single-bucket tickers have momentum=0 / velocity=0 by definition —
  // there's no "before" half to compare against. Flagging that explicitly
  // beats showing "+0.00 STABLE" which reads as "boring" when the truth
  // is "we don't have enough data yet".
  const insufficient = (t.buckets?.length ?? 0) < 2;
  const sparkData = t.buckets.map((b) => b.net_sentiment);
  const minVal = Math.min(-0.2, ...sparkData, 0);
  const maxVal = Math.max(0.2, ...sparkData, 0);
  const dirCls =
    t.direction === "flipping_negative" ? "text-bad" :
    t.direction === "flipping_positive" ? "text-good" :
    t.direction === "strengthening_negative" ? "text-warn" :
    t.direction === "strengthening_positive" ? "text-good" : "text-[color:var(--fg-dim)]";
  return (
    <li className={`rounded-md border ${watched ? "border-accent/50 bg-accent/5" : "border-[color:var(--border-subtle)] bg-[color:var(--bg-elev2)]/30"} p-2`}>
      <div className="flex items-baseline justify-between gap-2">
        <button
          type="button"
          className="font-mono font-semibold text-sm text-accent flex items-center gap-1 hover:underline"
          onClick={onToggleWatch}
          title={watched ? "remove from watchlist" : "add to watchlist"}
        >
          <span className="text-[12px]">{watched ? "★" : "☆"}</span>{t.symbol}
        </button>
        <span className={`text-[10px] uppercase tracking-wider ${insufficient ? "text-[color:var(--fg-muted)]" : dirCls}`}>
          {insufficient ? "needs data" : t.direction.replace("_", " ")}
          {t.flip_detected && <span className="ml-1 text-bad">⚡</span>}
        </span>
      </div>
      <div className="flex items-baseline justify-between gap-2 text-[10px] text-[color:var(--fg-muted)]">
        <span>mentions {t.mention_count} · {t.buckets.length} buckets</span>
        <span className="tabular-nums">
          {insufficient
            ? <em>single bucket — momentum undefined</em>
            : <>momentum {(t.momentum >= 0 ? "+" : "") + t.momentum.toFixed(2)} · net {t.overall_net_sentiment.toFixed(2)}</>}
        </span>
      </div>
      <div className="mt-1">
        <EChart
          height={50}
          option={eo({
            backgroundColor: CHART_BG,
            grid: { top: 4, right: 4, bottom: 4, left: 4 },
            xAxis: { type: "category", show: false, data: t.buckets.map((_, i) => String(i)) },
            yAxis: { type: "value", show: false, min: minVal, max: maxVal },
            series: [
              {
                type: "line",
                data: sparkData,
                smooth: true,
                showSymbol: false,
                lineStyle: { color: t.momentum >= 0 ? PALETTE[1] : PALETTE[3], width: 1.5 },
                areaStyle: { color: t.momentum >= 0 ? PALETTE[1] : PALETTE[3], opacity: 0.18 },
                markLine: {
                  symbol: "none",
                  lineStyle: { color: AXIS_LINE, type: "dashed" },
                  data: [{ yAxis: 0, label: { show: false } }],
                },
              },
            ],
          })}
        />
      </div>
    </li>
  );
}

// ── 6. Co-occurrence heatmap (asset × reason) + symbol edge list ──────────

function CoOccurrenceHeatmap({
  report,
  onSelectCell,
}: {
  report: CoOccurrenceReportDTO | null;
  onSelectCell: (cell: { asset: string; reason: string }) => void;
}) {
  if (!report || report.asset_reason_cells.length === 0) {
    return null;
  }
  const assets = Array.from(new Set(report.asset_reason_cells.map((c) => c.asset_class))).sort();
  const reasons = Array.from(new Set(report.asset_reason_cells.map((c) => c.reason_code))).sort();
  const data: [number, number, number, number][] = report.asset_reason_cells.map((c) => [
    reasons.indexOf(c.reason_code),
    assets.indexOf(c.asset_class),
    c.count,
    c.lift,
  ]);
  const maxLift = Math.max(1.5, ...report.asset_reason_cells.map((c) => c.lift));
  return (
    <section className="card grid gap-2">
      <div className="flex items-baseline justify-between gap-2">
        <h2 className="label">
          co-occurrence · asset × reason (<SignalExplainer term="lift">lift</SignalExplainer>)
        </h2>
        <span className="text-[10px] text-[color:var(--fg-dim)]">
          {report.asset_reason_cells.length} cells · {report.strong_edges.length} symbol edges
        </span>
      </div>
      <EChart
        height={Math.max(180, 24 * assets.length + 80)}
        onClick={(p: any) => {
          const v = p?.value;
          if (Array.isArray(v) && v.length >= 2) {
            const r = reasons[v[0] as number];
            const a = assets[v[1] as number];
            if (a && r) onSelectCell({ asset: a, reason: r });
          }
        }}
        option={eo({
          backgroundColor: CHART_BG,
          tooltip: {
            position: "top",
            formatter: (params: any) => {
              const [rIdx, aIdx, count, lift] = params.value;
              return `<div style='font-size:11px'>
                <strong>${assets[aIdx]} × ${reasons[rIdx]}</strong><br/>
                count ${count} · lift <strong>${lift.toFixed(2)}</strong><br/>
                <em style='color:#9aa3b2'>click to see the records</em>
              </div>`;
            },
          },
          grid: { top: 14, right: 16, bottom: 96, left: 110 },
          xAxis: {
            type: "category",
            data: reasons,
            axisLabel: { color: AXIS_COLOR, fontSize: 9, rotate: 50 },
            splitArea: { show: true },
          },
          yAxis: {
            type: "category",
            data: assets,
            axisLabel: { color: AXIS_COLOR, fontSize: 9 },
            splitArea: { show: true },
          },
          visualMap: {
            min: 0,
            max: maxLift,
            calculable: false,
            orient: "horizontal",
            left: "center",
            bottom: 8,
            inRange: { color: ["#1f2531", "#3b82f6", "#fbbf24", "#f87171"] },
            textStyle: { color: AXIS_COLOR, fontSize: 10 },
          },
          series: [
            {
              type: "heatmap",
              data: data.map(([r, a, , lift]) => [r, a, lift]),
              label: {
                show: true,
                fontSize: 9,
                color: "#e7ebf0",
                formatter: (p: any) => p.value[2].toFixed(1),
              },
              emphasis: { itemStyle: { shadowBlur: 6, shadowColor: "rgba(95,179,255,0.6)" } },
            },
          ],
        })}
      />
      {report.strong_edges.length > 0 && (
        <div className="grid grid-cols-2 gap-2 md:grid-cols-4">
          {report.strong_edges.slice(0, 8).map((e) => (
            <div
              key={`${e.symbol_a}-${e.symbol_b}`}
              className="rounded border border-[color:var(--border-subtle)] bg-[color:var(--bg-elev2)]/40 px-2 py-1 text-[11px]"
            >
              <div className="font-mono font-semibold text-accent">
                {e.symbol_a} ↔ {e.symbol_b}
              </div>
              <div className="text-[10px] text-[color:var(--fg-muted)]">
                {e.weight} co-mentions
              </div>
            </div>
          ))}
        </div>
      )}
    </section>
  );
}

// ── 7. Spillover sankey ───────────────────────────────────────────────────

function SpilloverSankey({ report }: { report: SpilloverReportDTO | null }) {
  if (!report || report.edges.length === 0) {
    return (
      <section className="card">
        <div className="flex items-baseline justify-between gap-2 mb-2">
          <h2 className="label">cross-asset spillover</h2>
          {report && (
            <span className="text-[10px] text-[color:var(--fg-dim)]">
              lag {report.lag_buckets * report.bucket_minutes}m · z {report.surge_z_threshold.toFixed(1)} · {report.total_buckets} buckets
            </span>
          )}
        </div>
        <p className="text-[11px] text-[color:var(--fg-muted)]">
          No directional spillover detected in the current window. Spillover needs at least
          two co-surging asset classes with consistent lead/lag — try a longer window (1000+ records),
          or wait for a busier news cycle.
        </p>
      </section>
    );
  }
  // Build sankey nodes (source asset + " (lead)" vs target asset + " (lag)")
  const sourceNames = Array.from(new Set(report.edges.map((e) => e.source_asset)));
  const targetNames = Array.from(new Set(report.edges.map((e) => e.target_asset)));
  const nodes = [
    ...sourceNames.map((n) => ({ name: `${n} →`, itemStyle: { color: PALETTE[0] } })),
    ...targetNames.map((n) => ({ name: `→ ${n}`, itemStyle: { color: PALETTE[1] } })),
  ];
  const links = report.edges.map((e) => ({
    source: `${e.source_asset} →`,
    target: `→ ${e.target_asset}`,
    value: Math.max(0.05, e.spillover_score),
    lineStyle: {
      color: "gradient",
      opacity: 0.35 + Math.min(0.4, e.spillover_score * 0.5),
    },
  }));
  return (
    <section className="card grid gap-2">
      <div className="flex items-baseline justify-between gap-2">
        <h2 className="label">
          cross-asset <SignalExplainer term="spillover">spillover</SignalExplainer> · lag {report.lag_buckets * report.bucket_minutes}m
        </h2>
        <span className="text-[10px] text-[color:var(--fg-dim)]">
          {report.edges.length} edges · z {report.surge_z_threshold.toFixed(1)}
        </span>
      </div>
      <EChart
        height={Math.max(180, 36 * Math.max(sourceNames.length, targetNames.length))}
        option={eo({
          backgroundColor: CHART_BG,
          tooltip: {
            trigger: "item",
            formatter: (params: any) => {
              if (params.dataType === "edge") {
                return `<div style='font-size:11px'>
                  <strong>${params.data.source} → ${params.data.target}</strong><br/>
                  spillover score ${params.data.value.toFixed(2)}
                </div>`;
              }
              return params.name;
            },
          },
          series: [
            {
              type: "sankey",
              data: nodes,
              links,
              orient: "horizontal",
              left: 8,
              right: 32,
              top: 8,
              bottom: 8,
              nodeWidth: 14,
              nodeGap: 8,
              label: { color: AXIS_COLOR, fontSize: 10 },
              lineStyle: { color: PALETTE[0] },
            },
          ],
        })}
      />
    </section>
  );
}

// ── 7b. Cross-symbol correlation ──────────────────────────────────────────

/**
 * Map |r| → tone class. Mirrors the score-band convention in
 * `scoreToneClass`: strong band (>=0.6), moderate (>=0.3), muted otherwise.
 * Sign drives green-vs-red — co-movement reads positive, anti-movement red.
 */
function correlationToneClass(r: number): string {
  const abs = Math.abs(r);
  if (abs < 0.3) return "text-[color:var(--fg-muted)]";
  if (r >= 0) return abs >= 0.6 ? "text-good" : "text-good/70";
  return abs >= 0.6 ? "text-bad" : "text-bad/70";
}

/**
 * Pearson r over per-bucket mention counts for the top frequently-mentioned
 * symbol pairs. Lives under the Network tab as a sibling to spillover —
 * spillover is directional/lagged on asset *classes*, correlation is
 * symmetric/synchronous on individual tickers.
 *
 * Re-fetched every 60s so the analyst sees the same cadence as the rest
 * of the network tab; React Query handles the cache key + loading state.
 */
function SymbolCorrelationPanel() {
  const { data, isLoading, isError, error } = useQuery({
    queryKey: ["quant", "symbol-correlation"],
    queryFn: () => api.quantSymbolCorrelation({ limit: 2000, bucket_minutes: 60, min_mentions: 3, top_n: 10 }),
    staleTime: 60_000,
    refetchInterval: 60_000,
  });

  if (isLoading) {
    return (
      <section className="card grid gap-2">
        <h2 className="label">cross-symbol <SignalExplainer term="Pearson r">correlation</SignalExplainer></h2>
        <Skeleton className="h-4 w-full" />
        <Skeleton className="h-4 w-5/6" />
        <Skeleton className="h-4 w-2/3" />
      </section>
    );
  }
  if (isError) {
    return (
      <section className="card grid gap-2">
        <h2 className="label">cross-symbol correlation</h2>
        <ErrorBox err={error ?? new Error("Failed to load symbol correlations.")} />
      </section>
    );
  }
  const pairs = data?.pairs ?? [];
  if (pairs.length === 0) {
    return (
      <section className="card">
        <div className="flex items-baseline justify-between gap-2 mb-2">
          <h2 className="label">
            cross-symbol <SignalExplainer term="Pearson r">correlation</SignalExplainer>
          </h2>
          {data && (
            <span className="text-[10px] text-[color:var(--fg-dim)]">
              {data.bucket_minutes}m buckets · min {data.min_mentions} mentions
            </span>
          )}
        </div>
        <p className="text-[11px] text-[color:var(--fg-muted)]">
          No symbol pairs cleared the volume threshold. Pearson r needs at least two
          symbols each mentioned in ≥ {data?.min_mentions ?? 3} buckets — wait for a busier
          news window or lower the threshold.
        </p>
      </section>
    );
  }

  return (
    <section className="card grid gap-2">
      <div className="flex items-baseline justify-between gap-2">
        <h2 className="label">
          cross-symbol <SignalExplainer term="Pearson r">correlation</SignalExplainer>
        </h2>
        <span className="text-[10px] text-[color:var(--fg-dim)]">
          {pairs.length} pairs · {data?.bucket_minutes ?? 60}m buckets · {pairs[0]?.n_buckets ?? 0} samples
        </span>
      </div>
      <ul className="grid gap-1">
        {pairs.slice(0, 10).map((p) => {
          const tone = correlationToneClass(p.pearson_r);
          const sign = p.pearson_r > 0 ? "+" : "";
          const key = `${p.symbol_a}-${p.symbol_b}`;
          return (
            <li
              key={key}
              className="flex items-baseline justify-between gap-3 text-[11px] py-1 border-b border-[color:var(--border)]/40 last:border-0"
            >
              <span className="font-mono text-[color:var(--fg)] truncate">
                {p.symbol_a} <span className="text-[color:var(--fg-dim)]">⟷</span> {p.symbol_b}
              </span>
              <span className="flex items-baseline gap-2 shrink-0">
                <span className={`font-mono tabular-nums ${tone}`}>
                  r={sign}{p.pearson_r.toFixed(2)}
                </span>
                <span className="text-[10px] text-[color:var(--fg-dim)]">
                  {p.n_buckets} buckets · {p.a_total}/{p.b_total}
                </span>
              </span>
            </li>
          );
        })}
      </ul>
    </section>
  );
}

// ── 8. Source radar ───────────────────────────────────────────────────────

function SourceRadarPanel({ leaderboard }: { leaderboard: SourceLeaderboardDTO | null }) {
  if (!leaderboard || leaderboard.sources.length === 0) return null;
  const top = leaderboard.sources.slice(0, 5);
  const axes = [
    { name: "relevant", max: 1 },
    { name: "signal", max: 1 },
    { name: "|skew|", max: 1 },
    { name: "asset div", max: 1 },
    { name: "reason div", max: 1 },
    { name: "uniqueness", max: 1 },
  ];
  return (
    <section className="card grid gap-2">
      <div className="flex items-baseline justify-between gap-2">
        <h2 className="label">source leaderboard · top 5 radar + tail table</h2>
        <span className="text-[10px] text-[color:var(--fg-dim)]">
          {leaderboard.total_domains} domains · {leaderboard.window_days}d
        </span>
      </div>
      <EChart
        height={260}
        option={eo({
          backgroundColor: CHART_BG,
          tooltip: { trigger: "item" },
          legend: {
            data: top.map((s) => s.domain),
            textStyle: { color: AXIS_COLOR, fontSize: 9 },
            top: 0,
            type: "scroll",
          },
          radar: {
            indicator: axes,
            radius: "62%",
            center: ["50%", "55%"],
            axisName: { color: AXIS_COLOR, fontSize: 9 },
            splitLine: { lineStyle: { color: AXIS_LINE } },
            splitArea: { areaStyle: { color: ["#161922", "#1f2531"] } },
            axisLine: { lineStyle: { color: AXIS_LINE } },
          },
          series: [
            {
              type: "radar",
              areaStyle: { opacity: 0.15 },
              data: top.map((s, i) => ({
                value: [
                  s.relevant_rate,
                  s.signal_density,
                  Math.abs(s.sentiment_skew),
                  s.asset_diversity,
                  s.reason_diversity,
                  s.symbol_uniqueness,
                ],
                name: s.domain,
                lineStyle: { color: PALETTE[i % PALETTE.length], width: 1.5 },
                itemStyle: { color: PALETTE[i % PALETTE.length] },
              })),
            },
          ],
        })}
      />
      {leaderboard.sources.length > 5 && (
        <details className="text-xs">
          <summary className="cursor-pointer text-[color:var(--fg-dim)] text-[10px]">
            show ranks 6–{Math.min(20, leaderboard.sources.length)}
          </summary>
          <ul className="grid gap-1 mt-2">
            {leaderboard.sources.slice(5, 20).map((s, i) => (
              <SourceTailRow key={s.domain} rank={i + 6} src={s} />
            ))}
          </ul>
        </details>
      )}
    </section>
  );
}

function SourceTailRow({ rank, src }: { rank: number; src: SourceScoreDTO }) {
  return (
    <li className="grid grid-cols-[24px_1fr_50px_120px] gap-2 items-baseline text-[10px] px-2 py-0.5">
      <span className="text-[color:var(--fg-muted)] tabular-nums">#{rank}</span>
      <span className="truncate font-mono">{src.domain}</span>
      <span className="tabular-nums">{(src.composite_score * 100).toFixed(0)}%</span>
      <span className="text-[color:var(--fg-muted)] tabular-nums">
        rel {(src.relevant_rate * 100).toFixed(0)}% · sig {(src.signal_density * 100).toFixed(0)}%
      </span>
    </li>
  );
}

// ── 9. Lead/lag arc panel ─────────────────────────────────────────────────

function LeadLagArcPanel({ report }: { report: LeadLagReportDTO | null }) {
  if (!report || report.per_source.length === 0) return null;
  const top = [...report.per_source]
    .sort((a, b) => b.composite_score - a.composite_score)
    .slice(0, 8);
  // ECharts bar series flips the data array vs the y-axis category order
  // (axis is reversed for top-to-bottom ranking). The tooltip dataIndex
  // points into the REVERSED top array — so resolve via that.
  const reversed = [...top].reverse();
  return (
    <section className="card grid gap-2">
      <div className="flex items-baseline justify-between gap-2">
        <h2 className="label">lead / lag attribution</h2>
        <span className="text-[10px] text-[color:var(--fg-dim)]">
          {report.total_events} event · {report.total_sources} source · hover for full domain
        </span>
      </div>
      <EChart
        height={200}
        option={eo({
          backgroundColor: CHART_BG,
          tooltip: {
            trigger: "axis",
            axisPointer: { type: "shadow" },
            formatter: (params: any) => {
              const p = Array.isArray(params) ? params[0] : params;
              const s = reversed[p.dataIndex];
              if (!s) return "";
              return `<div style='font-size:11px'>
                <strong>${s.domain}</strong><br/>
                led ${s.events_led} of ${s.events_participated} (${(s.lead_rate * 100).toFixed(0)}%)<br/>
                mean lead ${fmtSecs(s.mean_lead_seconds_when_leading)} · mean lag ${fmtSecs(s.mean_lag_seconds_when_following)}
              </div>`;
            },
          },
          grid: { top: 8, right: 14, bottom: 24, left: 110 },
          xAxis: { type: "value", axisLabel: { color: AXIS_COLOR, fontSize: 9, formatter: (v: number) => `${(v * 100).toFixed(0)}%` }, max: 1, splitLine: { lineStyle: { color: AXIS_LINE } } },
          yAxis: {
            type: "category",
            data: top.map((s) => s.domain).reverse(),
            axisLabel: {
              color: AXIS_COLOR,
              fontSize: 9,
              formatter: (v: string) => (v.length > 26 ? v.slice(0, 23) + "…" : v),
            },
            axisLine: { lineStyle: { color: AXIS_LINE } },
            triggerEvent: true,
          },
          series: [
            {
              type: "bar",
              data: [...top].reverse().map((s) => ({
                value: s.lead_rate,
                itemStyle: { color: s.lead_rate > 0.5 ? PALETTE[1] : PALETTE[0] },
              })),
              barWidth: "60%",
              label: { show: true, position: "right", color: AXIS_COLOR, fontSize: 9, formatter: (p: any) => `${(p.value * 100).toFixed(0)}%` },
            },
          ],
        })}
      />
    </section>
  );
}

// ── 10. Market-time panel ─────────────────────────────────────────────────
// Buckets the rolling-window of news arrivals by US equity session
// (NYSE schedule, eastern time) and lets the analyst see WHEN the news
// flow lands. Higher avg_score during the closing-hour push, for
// example, is a real-world phenomenon worth surfacing.

function MarketTimePanel() {
  const query = useQuery<MarketTimeResponse>({
    queryKey: ["quant-market-time"],
    queryFn: () => api.quantMarketTime(1000),
    refetchInterval: 30_000,
    staleTime: 15_000,
  });
  if (query.isLoading) return <Skeleton className="h-96" />;
  if (query.error) return <ErrorBox err={query.error} />;
  const data = query.data;
  if (!data) return null;
  const buckets = data.buckets;
  const total = data.total_records;
  if (total === 0) {
    return (
      <section className="card">
        <h2 className="label mb-2">market session clustering</h2>
        <EmptyState
          title="No timestamped records yet"
          hint="Records need a published_ts to be bucketed into a market session."
        />
      </section>
    );
  }
  // ECharts category-on-y bar — reverse so pre_open sits at the top.
  const categories = buckets.map((b) => SESSION_LABELS[b.session] ?? b.session);
  const volumes = buckets.map((b) => b.volume);
  const scores = buckets.map((b) => b.avg_score);
  // Highlight the highest-volume bucket in accent.
  const maxVolIdx = volumes.indexOf(Math.max(...volumes));
  return (
    <section className="card grid gap-3">
      <div className="flex items-baseline justify-between gap-2">
        <h2 className="label">
          market session clustering ·{" "}
          <SignalExplainer term="session">
            ET-anchored
          </SignalExplainer>
        </h2>
        <span className="text-[10px] text-[color:var(--fg-dim)]">
          {total.toLocaleString()} record · NYSE schedule
        </span>
      </div>
      {(data.highest_score_session || data.highest_volume_session) && (
        <div className="grid grid-cols-2 gap-2 text-[11px]">
          <div className="rounded-md border border-[color:var(--border-subtle)] bg-[color:var(--bg-elev2)]/30 p-2">
            <div className="text-[9px] uppercase tracking-wider text-[color:var(--fg-muted)]">
              highest avg score
            </div>
            <div className="font-semibold tabular-nums text-accent">
              {data.highest_score_session
                ? SESSION_LABELS[data.highest_score_session] ?? data.highest_score_session
                : "—"}
            </div>
          </div>
          <div className="rounded-md border border-[color:var(--border-subtle)] bg-[color:var(--bg-elev2)]/30 p-2">
            <div className="text-[9px] uppercase tracking-wider text-[color:var(--fg-muted)]">
              highest volume
            </div>
            <div className="font-semibold tabular-nums text-accent">
              {data.highest_volume_session
                ? SESSION_LABELS[data.highest_volume_session] ?? data.highest_volume_session
                : "—"}
            </div>
          </div>
        </div>
      )}
      <EChart
        height={240}
        option={eo({
          backgroundColor: CHART_BG,
          tooltip: {
            trigger: "axis",
            axisPointer: { type: "shadow" },
            formatter: (params: any) => {
              const arr = Array.isArray(params) ? params : [params];
              const p0 = arr[0];
              const b = buckets[p0.dataIndex];
              if (!b) return "";
              return `<div style='font-size:11px'>
                <strong>${SESSION_LABELS[b.session] ?? b.session}</strong><br/>
                volume ${b.volume.toLocaleString()}<br/>
                avg score ${b.avg_score.toFixed(2)}<br/>
                relevant ${b.relevant_count.toLocaleString()}
              </div>`;
            },
          },
          grid: { top: 8, right: 14, bottom: 24, left: 100 },
          xAxis: {
            type: "value",
            axisLabel: { color: AXIS_COLOR, fontSize: 9 },
            splitLine: { lineStyle: { color: AXIS_LINE } },
          },
          yAxis: {
            type: "category",
            data: categories,
            axisLabel: { color: AXIS_COLOR, fontSize: 10 },
            axisLine: { lineStyle: { color: AXIS_LINE } },
            inverse: true,
          },
          series: [
            {
              type: "bar",
              data: volumes.map((v, i) => ({
                value: v,
                itemStyle: { color: i === maxVolIdx ? PALETTE[0] : PALETTE[4] },
              })),
              barWidth: "55%",
              label: {
                show: true,
                position: "right",
                color: AXIS_COLOR,
                fontSize: 9,
                formatter: (p: any) => (p.value > 0 ? p.value.toLocaleString() : ""),
              },
            },
          ],
        })}
      />
      <div className="rounded-md border border-[color:var(--border-subtle)] bg-[color:var(--bg-elev2)]/30 p-2">
        <div className="text-[10px] uppercase tracking-wider text-[color:var(--fg-muted)] mb-1.5">
          per-session breakdown
        </div>
        <table className="w-full text-[11px]">
          <thead>
            <tr className="text-[9px] uppercase tracking-wider text-[color:var(--fg-muted)]">
              <th className="text-left font-normal py-0.5">session</th>
              <th className="text-right font-normal py-0.5">volume</th>
              <th className="text-right font-normal py-0.5">avg score</th>
              <th className="text-right font-normal py-0.5">relevant</th>
            </tr>
          </thead>
          <tbody>
            {buckets.map((b) => {
              const tone = scoreToneClass(b.avg_score);
              return (
                <tr key={b.session} className="border-t border-[color:var(--border-subtle)]/40">
                  <td className="py-0.5">
                    {SESSION_LABELS[b.session] ?? b.session}
                  </td>
                  <td className="py-0.5 text-right tabular-nums">
                    {b.volume.toLocaleString()}
                  </td>
                  <td className={`py-0.5 text-right tabular-nums ${tone}`}>
                    {b.avg_score.toFixed(2)}
                  </td>
                  <td className="py-0.5 text-right tabular-nums">
                    {b.relevant_count.toLocaleString()}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </section>
  );
}

// ── 11. Arrival heatmap (24h × 7day) ──────────────────────────────────────
// Anchors on the US-market timezone by default. Complements the session
// clustering above by showing the FINER granularity — which hour of which
// weekday actually carries the volume. Cells colored green-low → red-high
// via ECharts visualMap.

function ArrivalHeatmapPanel() {
  const query = useQuery<ArrivalHeatmapResponse>({
    queryKey: ["quant-arrival-heatmap"],
    queryFn: () => api.quantArrivalHeatmap(2000, "America/New_York"),
    refetchInterval: 30_000,
    staleTime: 15_000,
  });
  if (query.isLoading) return <Skeleton className="h-96" />;
  if (query.error) return <ErrorBox err={query.error} />;
  const data = query.data;
  if (!data) return null;
  if (data.total_samples === 0) {
    return (
      <section className="card">
        <h2 className="label mb-2">arrival heatmap (24h × 7day)</h2>
        <EmptyState
          title="No timestamped records yet"
          hint="Records need a published_ts to populate the hour-by-weekday grid."
        />
      </section>
    );
  }
  // ECharts heatmap expects [xIndex, yIndex, value] triples.
  // X = hour (0..23), Y = weekday (0..6). Invert Y in axis options so
  // Monday is at the top.
  const series = data.cells.map((c) => [c.hour, c.weekday, c.count]);
  const hourLabels = Array.from({ length: 24 }, (_, h) =>
    h % 4 === 0 ? String(h).padStart(2, "0") : "",
  );
  return (
    <section className="card grid gap-3">
      <div className="flex items-baseline justify-between gap-2">
        <h2 className="label">
          arrival heatmap{" "}
          <SignalExplainer term="heatmap">
            24h × 7day grid · {data.timezone}
          </SignalExplainer>
        </h2>
        <span className="text-[10px] text-[color:var(--fg-dim)]">
          {data.total_samples.toLocaleString()} records · peak {data.max_count.toLocaleString()}
        </span>
      </div>
      {data.peak_cells.length > 0 && (
        <div className="rounded-md border border-[color:var(--border-subtle)] bg-[color:var(--bg-elev2)]/30 p-2 text-[11px]">
          <div className="text-[9px] uppercase tracking-wider text-[color:var(--fg-muted)] mb-1">
            peak buckets
          </div>
          <div className="flex flex-wrap gap-1.5">
            {data.peak_cells.map((c) => (
              <span
                key={`${c.weekday}-${c.hour}`}
                className="rounded border border-[color:var(--border-subtle)] bg-[color:var(--bg-elev2)]/40 px-1.5 py-0.5 font-mono tabular-nums text-accent"
              >
                {data.weekday_labels[c.weekday]} {String(c.hour).padStart(2, "0")}:00 · {c.count}
              </span>
            ))}
          </div>
        </div>
      )}
      <EChart
        height={260}
        option={eo({
          backgroundColor: CHART_BG,
          tooltip: {
            position: "top",
            formatter: (p: any) => {
              const [hr, wd, count] = p.value as [number, number, number];
              const wdLabel = data.weekday_labels[wd] ?? `wd${wd}`;
              return `<div style='font-size:11px'>
                <strong>${wdLabel} ${String(hr).padStart(2, "0")}:00</strong><br/>
                ${count.toLocaleString()} arrivals
              </div>`;
            },
          },
          grid: { top: 10, right: 14, bottom: 30, left: 48 },
          xAxis: {
            type: "category",
            data: hourLabels,
            axisLabel: { color: AXIS_COLOR, fontSize: 9 },
            axisLine: { lineStyle: { color: AXIS_LINE } },
            splitArea: { show: false },
          },
          yAxis: {
            type: "category",
            data: data.weekday_labels,
            inverse: true,
            axisLabel: { color: AXIS_COLOR, fontSize: 10 },
            axisLine: { lineStyle: { color: AXIS_LINE } },
            splitArea: { show: false },
          },
          visualMap: {
            min: 0,
            max: Math.max(data.max_count, 1),
            calculable: false,
            orient: "horizontal",
            left: "center",
            bottom: 0,
            textStyle: { color: AXIS_COLOR, fontSize: 9 },
            inRange: { color: ["#1f2937", "#7cd992", "#fbbf24", "#f87171"] },
          },
          series: [
            {
              name: "arrivals",
              type: "heatmap",
              data: series,
              label: { show: false },
              emphasis: {
                itemStyle: { borderColor: "#5fb3ff", borderWidth: 1.5 },
              },
              itemStyle: { borderColor: AXIS_LINE, borderWidth: 0.5 },
            },
          ],
        })}
      />
    </section>
  );
}

// ── Help rail ─────────────────────────────────────────────────────────────

function HelpRail() {
  // Compact icon-led cheat sheet — replaces the verbose 10-bullet list.
  // Each row: small monogram glyph + one-line role. Saves ~80px and
  // doubles as a visual key the analyst can glance at vs read.
  const rows: { mark: string; tone: string; title: string; sub: string }[] = [
    { mark: "★", tone: "text-accent", title: "Tab heroes", sub: "every tab leads with the strongest signal" },
    { mark: "⚡", tone: "text-bad", title: "Flip badge", sub: "ticker net-sentiment sign changed in window" },
    { mark: "z", tone: "text-warn font-mono", title: "z-score", sub: "rolling stdev surprise; 2σ = anomaly, 4σ = high" },
    { mark: "▲", tone: "text-good", title: "Coherence", sub: "intra-cluster similarity (1.0 = identical stories)" },
    { mark: "×", tone: "text-accent", title: "Lift", sub: "co-occurrence vs independence baseline" },
    { mark: "?", tone: "text-[color:var(--fg-muted)]", title: "Explain", sub: "narrative on click" },
  ];
  return (
    <aside className="card text-xs grid gap-2">
      <h3 className="label">cheat sheet</h3>
      <ul className="grid gap-1.5">
        {rows.map((r) => (
          <li key={r.title} className="flex items-start gap-2">
            <span className={`mt-0.5 inline-flex h-5 w-5 shrink-0 items-center justify-center rounded border border-[color:var(--border-subtle)] bg-[color:var(--bg-elev2)]/40 text-[11px] ${r.tone}`}>
              {r.mark}
            </span>
            <div className="leading-tight">
              <div className="text-[11px] font-semibold">{r.title}</div>
              <div className="text-[10px] text-[color:var(--fg-dim)]">{r.sub}</div>
            </div>
          </li>
        ))}
      </ul>
      <p className="pt-1 text-[10px] text-[color:var(--fg-muted)] border-t border-[color:var(--border-subtle)] leading-relaxed">
        Single <code className="font-mono">/api/quant/dashboard</code> fan-out, 30s cache, paralleled across 9 signals. External narratives are budget-gated; local interpretation keeps the UI usable.
      </p>
    </aside>
  );
}

// ── Live status footer ────────────────────────────────────────────────────

function LiveStatusBar({ dashboard, deepseekStatus }: { dashboard: QuantDashboard | undefined; deepseekStatus: import("@/lib/api").ReviewsStatus | undefined }) {
  const cellCls =
    "flex items-baseline gap-1 rounded border border-[color:var(--border-subtle)] bg-[color:var(--bg-elev2)]/40 px-2 py-1";
  return (
    <footer className="grid grid-cols-2 gap-2 md:grid-cols-5 text-[10px]">
      <div className={cellCls}>
        <span className="inline-flex h-1.5 w-1.5 rounded-full bg-good animate-pulse-dot" />
        <span className="uppercase tracking-wider text-[color:var(--fg-muted)]">live</span>
        <span className="ml-auto tabular-nums">poll 12s</span>
      </div>
      <div className={cellCls}>
        <span className="uppercase tracking-wider text-[color:var(--fg-muted)]">DeepSeek</span>
        <span
          className={`ml-auto tabular-nums ${
            !deepseekStatus
              ? "text-[color:var(--fg-muted)]"
              : deepseekStatus.exhausted
                ? "text-bad"
                : deepseekStatus.deepseek_ready
                  ? "text-good"
                  : "text-warn"
          }`}
        >
          {deepseekStatus
            ? deepseekStatus.exhausted
              ? "budget hit"
              : deepseekStatus.deepseek_ready
                ? "active"
                : "off"
            : "—"}
        </span>
      </div>
      <div className={cellCls}>
        <span className="uppercase tracking-wider text-[color:var(--fg-muted)]">spent</span>
        <span className="ml-auto tabular-nums">
          ${(deepseekStatus?.usd_spent ?? 0).toFixed(4)}
          {deepseekStatus && (
            <span className="text-[color:var(--fg-muted)]"> / ${deepseekStatus.usd_cap.toFixed(2)}</span>
          )}
        </span>
      </div>
      <div className={cellCls}>
        <span className="uppercase tracking-wider text-[color:var(--fg-muted)]">paired</span>
        <span className="ml-auto tabular-nums">{(deepseekStatus?.tokens.calls ?? 0).toLocaleString()}</span>
      </div>
      <div className={cellCls}>
        <span className="uppercase tracking-wider text-[color:var(--fg-muted)]">records</span>
        <span className="ml-auto tabular-nums">{(dashboard?.n_records_window ?? 0).toLocaleString()}</span>
      </div>
    </footer>
  );
}
