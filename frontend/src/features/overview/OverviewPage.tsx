import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api, fmtDate, fmtPct, fmtScore, safeHref } from "@/lib/api";
import { Skeleton, ErrorBox, EmptyState } from "@/components/Skeleton";
import { Pill } from "@/components/Pill";
import { EChart } from "@/charts/EChart";

export function OverviewPage() {
  const summary = useQuery({ queryKey: ["summary"], queryFn: api.summary });
  const trends = useQuery({ queryKey: ["trends"], queryFn: () => api.trends(500) });
  const bench = useQuery({ queryKey: ["bench"], queryFn: api.benchmarkLatest });

  if (summary.isLoading) return <OverviewSkeleton />;
  if (summary.error) return <ErrorBox err={summary.error} />;
  if (!summary.data) return null;

  const s = summary.data;
  const totalRatio = s.totals.total ? s.totals.finance_relevant / s.totals.total : 0;

  return (
    <div className="grid gap-4">
      {/* Cards row */}
      <section className="grid gap-3 grid-cols-2 md:grid-cols-4 lg:grid-cols-5">
        <Card label="total records" value={s.totals.total.toLocaleString()} />
        <Card label="finance-relevant" value={`${s.totals.finance_relevant.toLocaleString()}`}
              hint={`${fmtPct(totalRatio)} of total`} />
        <Card label="DLQ" value={s.dlq.toLocaleString()} tone={s.dlq > 0 ? "warn" : undefined} />
        <Card label="distinct asset classes" value={String(Object.keys(s.asset_class_distribution).length)} />
        <Card label="benchmark F1"
              tone={bench.data && bench.data.relevance.f1 < 0.8 ? "bad" : undefined}
              value={bench.isLoading ? "…" : bench.data ? fmtPct(bench.data.relevance.f1) : "—"}
              hint={bench.data ? `prec ${fmtPct(bench.data.relevance.precision)} · rec ${fmtPct(bench.data.relevance.recall)}` : undefined} />
      </section>

      {/* Distribution + trends */}
      <section className="grid gap-3 lg:grid-cols-3">
        <DistributionCard title="asset classes" items={Object.entries(s.asset_class_distribution)} />
        <DistributionCard title="reason codes" items={Object.entries(s.reason_code_distribution)} />
        <div className="card">
          <h2 className="label mb-2">trend (last buckets)</h2>
          {trends.isLoading ? <Skeleton className="h-40" /> :
            trends.data && trends.data.buckets.length === 0 ? <EmptyState title="No trend data yet" hint="Run a replay first." /> :
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
          <Link to="/feed" className="text-xs text-accent hover:underline">view all →</Link>
        </div>
        {s.recent_top.length === 0 ? (
          <EmptyState title="No records yet"
                      hint="Run `fusion-stack run --mode replay_existing --max-records 50` to populate." />
        ) : (
          <ul className="divide-y divide-[color:var(--border)]">
            {s.recent_top.map((r) => {
              const href = safeHref(r.url);
              return (
                <li key={r.capture_id} className="py-2 grid gap-1">
                  <div className="flex items-baseline gap-2 flex-wrap">
                    <span className="text-[10px] text-[color:var(--fg-dim)]">{fmtDate(r.published_ts)}</span>
                    <span className="text-[10px] text-[color:var(--fg-muted)]">{r.domain}</span>
                    <span className="ml-auto text-[10px] text-[color:var(--fg-dim)]">score {fmtScore(r.finance_relevance_score)}</span>
                  </div>
                  <div className="text-sm">
                    {href ? (
                      <a href={href} target="_blank" rel="noopener noreferrer" className="text-accent hover:underline">
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
