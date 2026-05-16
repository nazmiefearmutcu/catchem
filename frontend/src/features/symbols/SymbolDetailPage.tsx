import { useQuery } from "@tanstack/react-query";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api, fmtDate, fmtScore, safeHref } from "@/lib/api";
import { Pill } from "@/components/Pill";
import { Skeleton, ErrorBox, EmptyState } from "@/components/Skeleton";
import { EChart } from "@/charts/EChart";

export function SymbolDetailPage() {
  const { symbol = "" } = useParams();
  const nav = useNavigate();
  const { data, isLoading, error } = useQuery({
    queryKey: ["symbol", symbol],
    queryFn: () => api.symbol(symbol, 100),
  });

  if (isLoading) return <Skeleton className="h-72" />;
  if (error) return <ErrorBox err={error} />;
  if (!data) return null;

  return (
    <div className="grid gap-4">
      <button onClick={() => nav("/symbols")} className="btn w-fit">← all symbols</button>
      <h1 className="text-2xl font-bold tracking-wide text-good">{data.symbol}</h1>
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        <div className="card"><div className="label">records</div><div className="mt-1 text-xl font-semibold">{data.count}</div></div>
        {Object.entries(data.sentiment_distribution).map(([k, v]) => (
          <div key={k} className="card">
            <div className="label">sentiment · {k}</div>
            <div className={`mt-1 text-xl font-semibold ${
              k === "positive" ? "text-good" : k === "negative" ? "text-bad" : ""
            }`}>{v}</div>
          </div>
        ))}
      </div>

      {Object.keys(data.reason_distribution).length > 0 && (
        <section className="card">
          <h2 className="label mb-2">reason distribution</h2>
          <EChart
            height={200}
            option={{
              xAxis: { type: "category", data: Object.keys(data.reason_distribution) },
              yAxis: { type: "value", minInterval: 1 },
              series: [{ type: "bar", data: Object.values(data.reason_distribution), itemStyle: { color: "#5fb3ff" } }],
            }}
          />
        </section>
      )}

      <section className="card">
        <h2 className="label mb-2">records mentioning {data.symbol}</h2>
        {data.items.length === 0 ? <EmptyState title="No records" /> : (
          <ul className="divide-y divide-[color:var(--border)]">
            {data.items.map((r) => {
              const href = safeHref(r.url);
              return (
                <li key={r.capture_id} className="py-2">
                  <div className="flex items-baseline gap-2 text-[10px] text-[color:var(--fg-dim)]">
                    <span>{fmtDate(r.published_ts)}</span>
                    <span>{r.domain}</span>
                    <span className="ml-auto">score {fmtScore(r.finance_relevance_score)}</span>
                  </div>
                  <div className="text-sm mt-1">
                    <Link to={`/feed/${encodeURIComponent(r.capture_id)}`} className="hover:underline">
                      {r.title ?? "(untitled)"}
                    </Link>
                    {href && <a href={href} target="_blank" rel="noopener noreferrer" className="ml-2 text-[10px] text-[color:var(--fg-dim)]">↗</a>}
                  </div>
                  <div className="flex flex-wrap gap-1 mt-1">
                    {r.asset_classes.map((a) => <Pill key={a} variant="ac">{a}</Pill>)}
                    {r.impact_reason_codes.slice(0, 3).map((rc) => <Pill key={rc} variant="rc">{rc}</Pill>)}
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
