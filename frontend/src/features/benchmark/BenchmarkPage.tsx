import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, fmtPct, fmtScore } from "@/lib/api";
import { Pill } from "@/components/Pill";
import { Skeleton, ErrorBox } from "@/components/Skeleton";
import { EChart } from "@/charts/EChart";

export function BenchmarkPage() {
  const qc = useQueryClient();
  const { data, isLoading, error, refetch, isFetching } = useQuery({
    queryKey: ["bench"],
    queryFn: api.benchmarkLatest,
    staleTime: 60_000,
  });
  const history = useQuery({
    queryKey: ["bench-hist"],
    queryFn: api.benchmarkHistory,
    staleTime: 60_000,
  });

  if (isLoading) return <Skeleton className="h-72" />;
  if (error) return <ErrorBox err={error} />;
  if (!data) return null;

  const failures = data.per_item.filter(
    (it) => it.expected_finance_relevant !== it.predicted_finance_relevant
  );

  return (
    <div className="grid gap-4">
      <header className="flex flex-wrap items-baseline gap-3">
        <h1 className="text-lg font-bold">Benchmark Lab</h1>
        <span className="text-xs text-[color:var(--fg-dim)]">ran {new Date(data.ran_at).toLocaleString()}</span>
        <button className="ml-auto btn"
                onClick={() => { refetch(); qc.invalidateQueries({ queryKey: ["bench-hist"] }); }}
                disabled={isFetching}>
          {isFetching ? "running…" : "re-run"}
        </button>
      </header>

      <section className="grid grid-cols-2 md:grid-cols-5 gap-3">
        <Card label="precision" value={fmtPct(data.relevance.precision, 1)} />
        <Card label="recall" value={fmtPct(data.relevance.recall, 1)} />
        <Card label="F1" value={fmtPct(data.relevance.f1, 1)}
              tone={data.relevance.f1 < 0.8 ? "bad" : "good"} />
        <Card label="symbol recall"
              value={data.symbol_recall != null ? fmtPct(data.symbol_recall, 1) : "—"} />
        <Card label="sentiment accuracy"
              value={data.sentiment_accuracy != null ? fmtPct(data.sentiment_accuracy, 1) : "—"} />
      </section>

      <section className="grid lg:grid-cols-2 gap-3">
        <div className="card">
          <h2 className="label mb-2">asset-class F1</h2>
          <ScoreBars items={data.asset_class_f1} />
        </div>
        <div className="card">
          <h2 className="label mb-2">reason-code F1</h2>
          <ScoreBars items={data.reason_code_f1} />
        </div>
      </section>

      <section className="card">
        <h2 className="label mb-2">per-item</h2>
        <table className="w-full text-xs">
          <thead>
            <tr className="text-left text-[color:var(--fg-dim)]">
              <th className="py-1">id</th>
              <th>expected</th>
              <th>predicted</th>
              <th>score</th>
              <th>asset_classes</th>
              <th>reason_codes</th>
            </tr>
          </thead>
          <tbody>
            {data.per_item.map((it) => {
              const ok = it.expected_finance_relevant === it.predicted_finance_relevant;
              return (
                <tr key={it.capture_id} className={ok ? "" : "bg-bad/10"}>
                  <td className="py-1 pr-2">{it.capture_id}</td>
                  <td>{String(it.expected_finance_relevant)}</td>
                  <td>{String(it.predicted_finance_relevant)}</td>
                  <td className="tabular-nums">{fmtScore(it.score)}</td>
                  <td><div className="flex flex-wrap gap-1">{it.predicted_asset_classes.map((c) => <Pill key={c} variant="ac">{c}</Pill>)}</div></td>
                  <td><div className="flex flex-wrap gap-1">{it.predicted_reason_codes.slice(0, 4).map((c) => <Pill key={c} variant="rc">{c}</Pill>)}</div></td>
                </tr>
              );
            })}
          </tbody>
        </table>
        {failures.length > 0 && (
          <p className="mt-3 text-xs text-bad" role="status">
            {failures.length} disagreement{failures.length === 1 ? "" : "s"} — see highlighted rows.
          </p>
        )}
      </section>

      {history.data && history.data.history.length > 0 && (
        <section className="card">
          <h2 className="label mb-2">history (last {history.data.history.length})</h2>
          <EChart
            height={180}
            option={{
              xAxis: { type: "category", data: history.data.history.map((h) => h.ran_at.slice(11, 16)) },
              yAxis: { type: "value", min: 0, max: 1 },
              series: [
                { type: "line", name: "precision", data: history.data.history.map((h) => h.relevance.precision), smooth: true },
                { type: "line", name: "recall", data: history.data.history.map((h) => h.relevance.recall), smooth: true },
                { type: "line", name: "f1", data: history.data.history.map((h) => h.relevance.f1), smooth: true },
              ],
              legend: { top: 0 },
            }}
          />
        </section>
      )}
    </div>
  );
}

function Card({ label, value, tone }: { label: string; value: string; tone?: "good" | "bad" }) {
  const cls = tone === "good" ? "text-good" : tone === "bad" ? "text-bad" : "";
  return (
    <div className="card">
      <div className="label">{label}</div>
      <div className={`mt-1 text-xl font-semibold ${cls}`}>{value}</div>
    </div>
  );
}

function ScoreBars({ items }: { items: Record<string, number> }) {
  const entries = Object.entries(items).sort(([, a], [, b]) => b - a);
  if (entries.length === 0) return <p className="text-xs text-[color:var(--fg-dim)]">none</p>;
  return (
    <ul className="grid gap-1.5">
      {entries.map(([k, v]) => (
        <li key={k} className="grid grid-cols-[120px_1fr_50px] gap-2 items-center text-xs">
          <span>{k}</span>
          <span className="h-2 rounded bg-[color:var(--bg-elev2)] overflow-hidden">
            <span className={`block h-full ${v >= 0.8 ? "bg-good" : v >= 0.5 ? "bg-warn" : "bg-bad"}`}
                  style={{ width: `${100 * v}%` }} />
          </span>
          <span className="text-right tabular-nums">{fmtPct(v, 0)}</span>
        </li>
      ))}
    </ul>
  );
}
