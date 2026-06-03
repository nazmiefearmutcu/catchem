import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { useRef, useState, type ReactNode } from "react";
import { api, fmtPct, fmtRel, fmtScore, safeHref, scoreToneClass } from "@/lib/api";
import { t, useLang } from "@/lib/i18n";
import { freshnessLabel, useTick } from "@/lib/freshness";
import { Skeleton, ErrorBox, EmptyState } from "@/components/Skeleton";
import { Pill } from "@/components/Pill";
import { Icon } from "@/components/Icon";
import { EChart } from "@/charts/EChart";
import {
  DEFAULT_TILE_ORDER,
  useOverviewTileOrder,
  type TileId,
} from "./useOverviewTileOrder";
import type { GlobalTone, GlobalToneTheme } from "@/types/api";

/**
 * Tiny inline markdown renderer (bold + para-break). Mirrored from /scan
 * so hosted or local live-read narratives render with proper emphasis on
 * the home page instead of literal asterisks.
 */
function renderMd(text: string): ReactNode[] {
  const blocks = text.split(/\n{2,}/).map((b) => b.trim()).filter(Boolean);
  return blocks.map((block, bi) => {
    const out: ReactNode[] = [];
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
          flush(`t-${bi}-${i}`);
          out.push(
            <strong key={`b-${bi}-${i}`} className="text-accent">
              {src.slice(i + 2, close)}
            </strong>,
          );
          i = close + 2;
          continue;
        }
      }
      buf += src[i];
      i++;
    }
    flush(`t-${bi}-end`);
    return (
      <p key={`p-${bi}`} className="leading-relaxed">
        {out}
      </p>
    );
  });
}

export function OverviewPage() {
  // Re-render every 30s so the freshness suffix (`data 2m ago`) ticks
  // naturally between background refetches.
  useTick();
  // Subscribe to locale changes so the hero eyebrow re-renders on swap.
  useLang();
  const summary = useQuery({ queryKey: ["summary"], queryFn: api.summary });
  const trends = useQuery({ queryKey: ["trends"], queryFn: () => api.trends(500) });
  const bench = useQuery({ queryKey: ["bench"], queryFn: api.benchmarkLatest });
  // Pull the same source-aware live-read used by /scan so the home page
  // leads with the same analyst-grade narrative. The backend chooses
  // DeepSeek only when configured, budgeted, and worth calling; otherwise
  // it returns deterministic local synthesis.
  const liveRead = useQuery({
    queryKey: ["quant-live-read", 1000],
    queryFn: () => api.quantLiveRead(1000),
    refetchInterval: 60_000,
    staleTime: 30_000,
  });
  // GDELT-derived macro news tone. Backend caches ~120s, so match that
  // cadence here — polling faster only re-serves the same cached payload.
  const globalTone = useQuery({
    queryKey: ["quant-global-tone"],
    queryFn: api.quantGlobalTone,
    refetchInterval: 120_000,
    staleTime: 60_000,
  });

  const { order: tileOrder, reorder: reorderTile, reset: resetTileOrder } = useOverviewTileOrder();
  const [dragIdx, setDragIdx] = useState<number | null>(null);
  const [dropIdx, setDropIdx] = useState<number | null>(null);
  const dragIdxRef = useRef<number | null>(null);

  if (summary.isLoading) return <OverviewSkeleton />;
  if (summary.error) return <ErrorBox err={summary.error} />;
  if (!summary.data) return null;

  const s = summary.data;
  const totalRatio = s.totals.total ? s.totals.finance_relevant / s.totals.total : 0;

  const tiles: Record<TileId, ReactNode> = {
    total: <Card label="total records" value={s.totals.total.toLocaleString()} />,
    relevant: (
      <Card
        label="finance-relevant"
        value={`${s.totals.finance_relevant.toLocaleString()}`}
        hint={`${fmtPct(totalRatio)} of total`}
      />
    ),
    dlq: (
      <Card
        label="DLQ"
        value={s.dlq.toLocaleString()}
        tone={s.dlq > 0 ? "warn" : undefined}
      />
    ),
    distinct: (
      <Card
        label="distinct asset classes"
        value={String(Object.keys(s.asset_class_distribution).length)}
      />
    ),
    f1: (
      <Card
        label="benchmark F1"
        tone={bench.data && bench.data.relevance.f1 < 0.8 ? "bad" : undefined}
        value={bench.isLoading ? "…" : bench.data ? fmtPct(bench.data.relevance.f1) : "—"}
        hint={
          bench.data
            ? `prec ${fmtPct(bench.data.relevance.precision)} · rec ${fmtPct(bench.data.relevance.recall)}`
            : undefined
        }
      />
    ),
  };

  const isCustomTileOrder =
    tileOrder.length !== DEFAULT_TILE_ORDER.length ||
    tileOrder.some((id, i) => id !== DEFAULT_TILE_ORDER[i]);

  // HTML5 DnD handlers for KPI tiles — operate on the persisted-order index
  // directly. The hook owns the source of truth, so we just translate from
  // (fromIdx, toIdx) into reorderTile().
  const onTileDragStart =
    (idx: number) => (e: React.DragEvent<HTMLDivElement>) => {
      dragIdxRef.current = idx;
      setDragIdx(idx);
      try {
        e.dataTransfer.setData("text/plain", tileOrder[idx] ?? "");
      } catch {
        /* some test envs don't implement DataTransfer */
      }
      e.dataTransfer.effectAllowed = "move";
    };
  const onTileDragOver =
    (idx: number) => (e: React.DragEvent<HTMLDivElement>) => {
      if (dragIdxRef.current === null) return;
      e.preventDefault();
      e.dataTransfer.dropEffect = "move";
      if (dropIdx !== idx) setDropIdx(idx);
    };
  const onTileDrop = (idx: number) => (e: React.DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    const src = dragIdxRef.current;
    dragIdxRef.current = null;
    setDragIdx(null);
    setDropIdx(null);
    if (src === null || src === idx) return;
    reorderTile(src, idx);
  };
  const onTileDragEnd = () => {
    dragIdxRef.current = null;
    setDragIdx(null);
    setDropIdx(null);
  };

  // Alt+ArrowLeft / Alt+ArrowRight keyboard reorder (parity with watchlist).
  const onTileKeyDown =
    (idx: number) => (e: React.KeyboardEvent<HTMLDivElement>) => {
      if (!e.altKey) return;
      if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
      e.preventDefault();
      const delta = e.key === "ArrowLeft" ? -1 : 1;
      const toIdx = Math.max(0, Math.min(tileOrder.length - 1, idx + delta));
      if (toIdx === idx) return;
      reorderTile(idx, toIdx);
    };

  return (
    <div className="grid gap-5">
      {/* Hero: source-aware live-read narrative — first thing the analyst sees. */}
      <section className="relative overflow-hidden rounded-xl border border-accent/40 hero-gradient p-6">
        <div
          aria-hidden
          className="pointer-events-none absolute -top-20 -left-20 h-48 w-48 rounded-full bg-accent/20 blur-3xl"
        />
        <div className="relative flex items-baseline justify-between gap-3 mb-3">
          <div className="flex items-center gap-3">
            <span className="relative flex h-2 w-2">
              <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-accent opacity-75" />
              <span className="relative inline-flex h-2 w-2 rounded-full bg-accent" />
            </span>
            <div>
              <div className="text-[10px] uppercase tracking-[0.25em] text-accent font-semibold">
                {t("overview.eyebrow")}
              </div>
              <h1 className="text-lg font-semibold mt-0.5 tracking-tight">
                {liveRead.isLoading
                  ? "Reading the tape…"
                  : liveRead.data?.source === "deepseek"
                    ? "DeepSeek synthesis"
                    : "Local synthesis"}
              </h1>
            </div>
          </div>
          <div className="flex items-center gap-2">
            <button
              type="button"
              className="chip text-[10px] no-print hover:bg-[color:var(--bg-elev2)] focus:outline-none focus-visible:ring-1 focus-visible:ring-accent"
              onClick={() => window.print()}
              title="Print this page or save as PDF"
            >
              <span className="inline-flex items-center gap-1">
                <Icon name="print" />
                print / save PDF
              </span>
            </button>
            <Link to="/scan" className="chip text-[10px] hover:bg-[color:var(--bg-elev2)] focus:outline-none focus-visible:ring-1 focus-visible:ring-accent">
              <span className="inline-flex items-center gap-1">
                open Quant Scan
                <Icon name="arrowRight" />
              </span>
            </Link>
          </div>
        </div>
        {liveRead.isLoading ? (
          <Skeleton className="h-20" />
        ) : (
          <div className="relative text-[14px] leading-relaxed text-[color:var(--fg)] max-w-3xl grid gap-3">
            {liveRead.data?.narrative ? renderMd(liveRead.data.narrative) : <p>—</p>}
          </div>
        )}
        <div className="relative mt-4 flex flex-wrap items-baseline gap-3 text-[10px] text-[color:var(--fg-muted)]">
          {liveRead.data?.generated_at && (
            <span>
              generated <span className="text-[color:var(--fg-dim)]">{fmtRel(liveRead.data.generated_at)}</span>
            </span>
          )}
          {liveRead.data?.usd_cost != null && (
            <span className="tabular-nums">cost ${liveRead.data.usd_cost.toFixed(5)}</span>
          )}
          {liveRead.data?.fallback_reason && (
            <span className="text-warn">fallback: {liveRead.data.fallback_reason}</span>
          )}
          <span className="text-[10px] text-[color:var(--fg-dim)]">· {freshnessLabel(summary.dataUpdatedAt)}</span>
          <span className="ml-auto">auto-refresh 60s</span>
        </div>
      </section>

      {/* Cards row — user-reorderable via drag-drop or Alt+ArrowLeft/Right */}
      <div
        className="group/tiles relative"
        data-testid="overview-tile-row"
      >
        {isCustomTileOrder && (
          <button
            type="button"
            className="chip text-[10px] absolute -top-6 right-0 opacity-0 group-hover/tiles:opacity-100 focus:opacity-100 transition-opacity hover:bg-[color:var(--bg-elev2)] focus:outline-none focus-visible:ring-1 focus-visible:ring-accent"
            onClick={resetTileOrder}
            title="Reset KPI tile order to default"
            data-testid="overview-tile-reset"
          >
            <span className="inline-flex items-center gap-1">
              <Icon name="drag" size={10} />
              reset order
            </span>
          </button>
        )}
        <section
          className="grid gap-3 grid-cols-2 md:grid-cols-4 lg:grid-cols-5"
          aria-label="Key performance tiles (drag to reorder)"
        >
          {tileOrder.map((id, idx) => {
            const isDragging = dragIdx === idx;
            const isDropTarget = dropIdx === idx && dragIdx !== idx;
            return (
              <div
                key={id}
                draggable
                tabIndex={0}
                role="group"
                aria-label={`KPI tile ${id} (Alt+ArrowLeft / Alt+ArrowRight to reorder)`}
                onDragStart={onTileDragStart(idx)}
                onDragOver={onTileDragOver(idx)}
                onDrop={onTileDrop(idx)}
                onDragEnd={onTileDragEnd}
                onKeyDown={onTileKeyDown(idx)}
                data-testid={`overview-tile-${id}`}
                data-tile-idx={idx}
                data-dragging={isDragging ? "true" : undefined}
                data-drop-target={isDropTarget ? "true" : undefined}
                className={[
                  "relative rounded transition-shadow",
                  "cursor-grab active:cursor-grabbing select-none",
                  "focus:outline-none focus-visible:ring-1 focus-visible:ring-accent",
                  isDragging ? "opacity-50" : "",
                  isDropTarget ? "ring-1 ring-accent" : "",
                ]
                  .filter(Boolean)
                  .join(" ")}
                title="Drag to reorder · Alt+← / Alt+→"
              >
                {tiles[id]}
              </div>
            );
          })}
        </section>
      </div>

      {/* Global news tone — GDELT macro sentiment lens */}
      <GlobalTonePanel
        data={globalTone.data}
        isLoading={globalTone.isLoading}
      />

      {/* Distribution + trends */}
      <section className="grid gap-3 lg:grid-cols-3">
        <DistributionCard title="asset classes" items={Object.entries(s.asset_class_distribution)} />
        <DistributionCard title="reason codes" items={Object.entries(s.reason_code_distribution)} />
        <div className="card">
          <h2 className="label mb-2">trend (last buckets)</h2>
          {trends.isLoading ? <Skeleton className="h-40" /> :
            trends.data && trends.data.buckets.length === 0 ? <EmptyState title="No trend data yet" hint="Run a replay first." action={<Link to="/replay" className="btn focus:outline-none focus-visible:ring-1 focus-visible:ring-accent">Open Replay/Upload</Link>} /> :
            trends.data && (
              <EChart
                height={180}
                option={{
                  xAxis: { type: "category", data: trends.data.buckets, axisLabel: { rotate: 30 } },
                  yAxis: { type: "value", minInterval: 1 },
                  legend: { top: 0 },
                  series: trends.data.asset_classes.slice(0, 6).map((ac) => ({
                    name: ac,
                    type: "line",
                    smooth: true,
                    stack: "ac",
                    areaStyle: { opacity: 0.18 },
                    showSymbol: false,
                    data: trends.data!.series[ac],
                  })),
                }}
              />
            )}
        </div>
      </section>

      {/* Recent top */}
      <section className="card">
        <div className="flex items-center justify-between mb-2">
          <h2 className="label">most recent relevant</h2>
          <Link to="/feed" className="text-xs text-accent hover:underline focus:outline-none focus-visible:ring-1 focus-visible:ring-accent rounded-sm">view all →</Link>
        </div>
        {s.recent_top.length === 0 ? (
          <EmptyState title="No records yet"
                      hint="The live news poller is fetching now — items will appear here within a minute. You can also paste an article in Replay/Upload to ingest immediately."
                      action={<Link to="/replay" className="btn focus:outline-none focus-visible:ring-1 focus-visible:ring-accent">Open Replay/Upload</Link>} />
        ) : (
          <ul className="divide-y divide-[color:var(--border)]">
            {s.recent_top.map((r) => {
              const href = safeHref(r.url);
              return (
                <li key={r.capture_id} className="py-2 grid gap-1">
                  <div className="flex items-baseline gap-2 flex-wrap">
                    <span className="text-[11px] text-[color:var(--fg-dim)]" title={r.published_ts ?? ""}>{fmtRel(r.published_ts)}</span>
                    <span className="text-[10px] text-[color:var(--fg-muted)]">{r.domain}</span>
                    <span className={`ml-auto text-[10px] tabular-nums ${scoreToneClass(r.finance_relevance_score)}`}>
                      score {fmtScore(r.finance_relevance_score)}
                    </span>
                  </div>
                  <div className="text-sm">
                    {href ? (
                      <a href={href} target="_blank" rel="noopener noreferrer" className="text-accent hover:underline focus:outline-none focus-visible:ring-1 focus-visible:ring-accent rounded-sm">
                        {r.title ?? "(untitled)"}
                      </a>
                    ) : (
                      <span>{r.title ?? "(untitled)"}</span>
                    )}
                  </div>
                  <div className="flex flex-wrap gap-1">
                    {r.asset_classes.map((ac) => <Pill key={ac} variant="ac">{ac}</Pill>)}
                    {r.impact_reason_codes.slice(0, 3).map((rc) => <Pill key={rc} variant="rc">{rc}</Pill>)}
                    {r.candidate_symbols.slice(0, 3).map((s) => <Pill key={s} variant="sym">{s}</Pill>)}
                  </div>
                </li>
              );
            })}
          </ul>
        )}
      </section>
    </div>
  );
}

// Map a GDELT tone_state to a label + tone class + Pill variant. "improving"
// is good news (green), "deteriorating" is bad (red), "stable" is neutral.
function toneStateMeta(state: GlobalToneTheme["tone_state"]): {
  label: string;
  cls: string;
  pill: "good" | "bad" | "default";
} {
  if (state === "improving") return { label: "improving", cls: "text-good", pill: "good" };
  if (state === "deteriorating") return { label: "deteriorating", cls: "text-bad", pill: "bad" };
  return { label: "stable", cls: "text-[color:var(--fg-dim)]", pill: "default" };
}

// Compact macro-sentiment panel backed by GET /api/quant/global-tone. Shows
// the overall tone state + value, then a small per-theme row. Degrades to a
// muted "tone unavailable" line when GDELT is down (degraded:true).
function GlobalTonePanel({
  data,
  isLoading,
}: {
  data: GlobalTone | undefined;
  isLoading: boolean;
}) {
  const themeOrder = ["markets", "economy", "crypto", "fed"];

  return (
    <section className="card" data-testid="global-tone-panel" aria-label="Global news tone">
      <div className="flex items-center justify-between mb-2">
        <h2 className="label">global news tone</h2>
        <span className="text-[10px] text-[color:var(--fg-dim)]">GDELT · macro</span>
      </div>

      {isLoading ? (
        <Skeleton className="h-16" />
      ) : !data || data.degraded ? (
        <EmptyState
          title="tone unavailable"
          hint="GDELT macro tone isn’t available right now — it’ll refill on the next poll."
        />
      ) : (
        (() => {
          const meta = toneStateMeta(data.overall_state);
          const themes = themeOrder
            .map((name) => [name, data.by_theme[name]] as const)
            .filter((entry): entry is readonly [string, GlobalToneTheme] => Boolean(entry[1]));
          return (
            <div className="grid gap-3">
              {/* Overall */}
              <div className="flex items-baseline gap-3">
                <span className={`text-xl font-semibold ${meta.cls}`} data-testid="global-tone-overall-state">
                  {meta.label}
                </span>
                <span className="text-sm tabular-nums text-[color:var(--fg-muted)]">
                  tone {fmtScore(data.overall_tone)}
                </span>
              </div>
              {/* Per-theme row */}
              <div className="flex flex-wrap gap-2">
                {themes.map(([name, theme]) => {
                  const tm = toneStateMeta(theme.tone_state);
                  return (
                    <div
                      key={name}
                      className="flex items-center gap-1.5 rounded border border-[color:var(--border)] px-2 py-1"
                      data-testid={`global-tone-theme-${name}`}
                    >
                      <span className="text-[11px] text-[color:var(--fg-muted)] capitalize">{name}</span>
                      <Pill variant={tm.pill} title={`${name}: ${tm.label}`}>{tm.label}</Pill>
                      <span className="text-[10px] tabular-nums text-[color:var(--fg-dim)]">
                        {fmtScore(theme.latest_tone)}
                      </span>
                    </div>
                  );
                })}
              </div>
            </div>
          );
        })()
      )}
    </section>
  );
}

function Card({ label, value, hint, tone }: { label: string; value: string; hint?: string; tone?: "warn" | "bad" }) {
  const toneCls = tone === "bad" ? "text-bad" : tone === "warn" ? "text-warn" : "";
  return (
    <div className="card">
      <div className="label">{label}</div>
      <div className={`mt-1 text-xl font-semibold ${toneCls}`}>{value}</div>
      {hint && <div className="text-[10px] text-[color:var(--fg-dim)] mt-0.5">{hint}</div>}
    </div>
  );
}

function DistributionCard({ title, items }: { title: string; items: [string, number][] }) {
  const max = Math.max(1, ...items.map(([, n]) => n));
  return (
    <div className="card">
      <h2 className="label mb-2">{title}</h2>
      {items.length === 0 ? <EmptyState title="empty" /> : (
        <ul className="space-y-1.5">
          {items.slice(0, 8).map(([k, n]) => (
            <li key={k} className="grid grid-cols-[100px_1fr_40px] gap-2 items-center text-xs">
              <span className="truncate" title={k}>{k}</span>
              <span className="h-2 rounded bg-[color:var(--bg-elev2)] overflow-hidden">
                <span className="block h-full bg-accent/70" style={{ width: `${100 * n / max}%` }} />
              </span>
              <span className="text-right text-[color:var(--fg-dim)]">{n}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function OverviewSkeleton() {
  return (
    <div className="grid gap-3" aria-busy="true">
      <div className="grid gap-3 grid-cols-2 md:grid-cols-4">
        {Array.from({ length: 4 }).map((_, i) => <Skeleton key={i} className="h-16" />)}
      </div>
      <Skeleton className="h-48" />
      <Skeleton className="h-64" />
    </div>
  );
}
