import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { api, fmtRel } from "@/lib/api";
import { freshnessLabel, useTick } from "@/lib/freshness";
import { EChart } from "@/charts/EChart";
import { Skeleton, ErrorBox, EmptyState } from "@/components/Skeleton";

/**
 * Tiny inline-markdown renderer used by the hero — mirrors /scan + /
 * so DeepSeek narratives render bold accents instead of literal `**`.
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

export function MarketMapPage() {
  // Re-render every 30s so the freshness suffix in the hero subtitle keeps
  // ticking even while the underlying matrix query is idle between refetches.
  useTick();
  const matrix = useQuery({ queryKey: ["matrix"], queryFn: api.matrix });
  const trends = useQuery({ queryKey: ["trends"], queryFn: () => api.trends(500) });
  const liveRead = useQuery({
    queryKey: ["quant-live-read", 1000],
    queryFn: () => api.quantLiveRead(1000),
    refetchInterval: 60_000,
    staleTime: 30_000,
  });
  const regime = useQuery({
    queryKey: ["quant-regime", 1000],
    queryFn: () => api.quantRegime({ limit: 1000 }),
    refetchInterval: 30_000,
    staleTime: 15_000,
  });

  return (
    <div className="grid gap-5">
      {/* Hero: narrative + regime headline */}
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
                Analysis Map · cross-asset news flow
              </div>
              <h1 className="text-lg font-semibold mt-0.5 tracking-tight">
                {liveRead.isLoading
                  ? "Reading the tape…"
                  : liveRead.data?.source === "deepseek"
                    ? "DeepSeek synthesis"
                    : "Local synthesis"}
              </h1>
              <div className="mt-1 text-[10px] text-[color:var(--fg-dim)]">
                {freshnessLabel(matrix.dataUpdatedAt)}
              </div>
            </div>
          </div>
          <Link to="/scan" className="chip text-[10px] hover:bg-[color:var(--bg-elev2)]">
            full Quant Scan →
          </Link>
        </div>
        {liveRead.isLoading ? (
          <Skeleton className="h-16" />
        ) : (
          <div className="relative text-[14px] leading-relaxed text-[color:var(--fg)] max-w-3xl grid gap-3">
            {liveRead.data?.narrative ? renderMd(liveRead.data.narrative) : <p>—</p>}
          </div>
        )}
        <div className="relative mt-4 grid gap-2 md:grid-cols-3 text-[11px]">
          <RegimeStat label="regime shifts" value={regime.data ? `${regime.data.detected_shifts.length}` : "—"} hint={regime.data ? `${regime.data.bucket_minutes}m buckets` : ""} />
          <RegimeStat
            label="latest shift"
            value={regime.data?.detected_shifts.length ? fmtRel(regime.data.detected_shifts[regime.data.detected_shifts.length - 1]) : "—"}
            hint={regime.data?.detected_shifts.length ? "click /scan for context" : "no shift detected"}
          />
          <RegimeStat
            label="bucket gating"
            value={regime.data ? `≥ ${(regime.data as { min_records_per_bucket?: number }).min_records_per_bucket ?? 3} rec` : "—"}
            hint={`threshold KL ${regime.data ? regime.data.shift_threshold.toFixed(2) : "—"}`}
          />
        </div>
      </section>

      <section className="card">
        <h2 className="label mb-2">news-impact map · asset class × reason code</h2>
        {matrix.isLoading ? <Skeleton className="h-72" /> :
          matrix.error ? <ErrorBox err={matrix.error} /> :
          !matrix.data || matrix.data.asset_classes.length === 0 ? <EmptyState title="No matrix yet" hint="Run a replay first." action={<Link to="/replay" className="btn">Open Replay/Upload</Link>} /> : (
            <EChart
              height={Math.max(280, 24 * matrix.data.asset_classes.length + 80)}
              option={{
                grid: { left: 100, right: 30, top: 40, bottom: 100, containLabel: true },
                xAxis: {
                  type: "category",
                  data: matrix.data.reason_codes,
                  axisLabel: { rotate: 50, fontSize: 10 },
                },
                yAxis: { type: "category", data: matrix.data.asset_classes },
                visualMap: {
                  min: 0,
                  max: Math.max(1, ...matrix.data.matrix.flat()),
                  calculable: false,
                  orient: "horizontal",
                  left: "center",
                  bottom: 8,
                  inRange: { color: ["#1f2531", "#3b82f6", "#fbbf24", "#f87171"] },
                  textStyle: { color: "#9aa3b2", fontSize: 10 },
                },
                series: [{
                  type: "heatmap",
                  data: matrix.data.matrix.flatMap((row, i) =>
                    row.map((v, j) => [j, i, v])
                  ),
                  label: { show: true, fontSize: 10, color: "#e7ebf0" },
                  emphasis: { itemStyle: { shadowBlur: 6, shadowColor: "rgba(95,179,255,0.6)" } },
                }],
              }}
            />
          )}
      </section>

      <section className="card">
        <div className="flex items-baseline justify-between gap-2 mb-2">
          <h2 className="label">news record trend by asset class</h2>
          {trends.data && (
            <span className="text-[10px] text-[color:var(--fg-muted)]">
              {trends.data.buckets.length} buckets · {trends.data.asset_classes.length} classes
            </span>
          )}
        </div>
        {trends.isLoading ? <Skeleton className="h-56" /> :
          trends.error ? <ErrorBox err={trends.error} /> :
          trends.data && trends.data.buckets.length === 0 ? <EmptyState title="No timeline data" hint="Run a replay first." action={<Link to="/replay" className="btn">Open Replay/Upload</Link>} /> :
          trends.data && (
            <EChart
              height={300}
              option={{
                tooltip: { trigger: "axis", axisPointer: { type: "shadow" } },
                legend: {
                  top: 0,
                  textStyle: { color: "#9aa3b2", fontSize: 10 },
                  itemWidth: 10,
                  itemHeight: 10,
                },
                grid: { left: 40, right: 16, top: 40, bottom: 60 },
                xAxis: { type: "category", data: trends.data.buckets, axisLabel: { rotate: 35, fontSize: 10 } },
                yAxis: { type: "value", minInterval: 1 },
                series: trends.data.asset_classes.map((ac) => ({
                  name: ac,
                  type: "bar",
                  stack: "ac",
                  emphasis: { focus: "series" },
                  data: trends.data!.series[ac],
                })),
              }}
            />
          )}
      </section>
    </div>
  );
}

function RegimeStat({ label, value, hint }: { label: string; value: string; hint: string }) {
  return (
    <div className="rounded-md border border-[color:var(--border-subtle)] bg-[color:var(--bg-elev2)]/40 px-3 py-2">
      <div className="text-[9px] uppercase tracking-wider text-[color:var(--fg-muted)]">{label}</div>
      <div className="mt-0.5 text-sm font-semibold tabular-nums">{value}</div>
      <div className="text-[10px] text-[color:var(--fg-dim)]">{hint}</div>
    </div>
  );
}
