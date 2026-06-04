import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, fmtPct, fmtScore } from "@/lib/api";
import { t, useLang } from "@/lib/i18n";
import { Pill } from "@/components/Pill";
import { Skeleton, ErrorBox } from "@/components/Skeleton";
import { Sparkline } from "@/components/Sparkline";
import { Icon } from "@/components/Icon";
import { EChart } from "@/charts/EChart";

export function BenchmarkPage() {
  // Subscribe to locale changes so the hero eyebrow re-renders on swap.
  useLang();
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

  // Delta vs prior run: history is most-recent-first, so [1] is the prior run.
  // Empty deltas (no prior run) render as a hairline placeholder instead of
  // a "+0.0pp vs prev" lie.
  const prev = history.data?.history?.[1];
  const deltaF1   = prev ? data.relevance.f1        - prev.relevance.f1        : null;
  const deltaPrec = prev ? data.relevance.precision - prev.relevance.precision : null;
  const deltaRec  = prev ? data.relevance.recall    - prev.relevance.recall    : null;
  const deltaSent = prev && data.sentiment_accuracy != null && prev.sentiment_accuracy != null
    ? data.sentiment_accuracy - prev.sentiment_accuracy
    : null;
  const deltaSym = prev && data.symbol_recall != null && prev.symbol_recall != null
    ? data.symbol_recall - prev.symbol_recall
    : null;

  // Per-metric trajectories — extracted in chronological order so each tile's
  // sparkline reads left→right = oldest→newest.
  const hist = (history.data?.history ?? []).slice().reverse();
  const trail = {
    f1:   hist.map((h) => h.relevance.f1),
    prec: hist.map((h) => h.relevance.precision),
    rec:  hist.map((h) => h.relevance.recall),
    sent: hist.map((h) => h.sentiment_accuracy).filter((v): v is number => v != null),
    sym:  hist.map((h) => h.symbol_recall).filter((v): v is number => v != null),
  };

  return (
    <div className="grid w-full min-w-0 gap-5">
      {/* Hero: outcome headline + 5 KPI tiles with run-over-run delta. */}
      <section className="relative w-full min-w-0 overflow-hidden rounded-xl border border-accent/40 hero-gradient p-6">
        <div
          aria-hidden
          className="pointer-events-none absolute -top-20 -left-20 h-48 w-48 rounded-full bg-accent/20 blur-3xl"
        />
        <div className="relative flex flex-wrap items-start justify-between gap-3 mb-3">
          <div className="flex min-w-0 flex-1 items-center gap-3">
            <span className="relative flex h-2 w-2">
              <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-accent opacity-75" />
              <span className="relative inline-flex h-2 w-2 rounded-full bg-accent" />
            </span>
            <div className="min-w-0">
              <div className="text-[10px] uppercase tracking-[0.25em] text-accent font-semibold">
                {t("benchmark.eyebrow")}
              </div>
              <h1 className="text-lg font-semibold mt-0.5 tracking-tight">
                {failures.length === 0
                  ? "All golden items predicted correctly"
                  : `${failures.length} item${failures.length === 1 ? "" : "s"} misclassified`}
              </h1>
              <div className="mt-1 text-[11px] text-[color:var(--fg-muted)]">
                {data.per_item.length} items · ran {new Date(data.ran_at).toLocaleString()}
                {history.data && history.data.history.length > 1 && (
                  <> · {history.data.history.length} historical runs</>
                )}
              </div>
            </div>
          </div>
          <div className="flex max-w-full flex-wrap items-center gap-2">
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
            <button
              className="btn shrink-0 focus:outline-none focus-visible:ring-1 focus-visible:ring-accent"
              onClick={() => { refetch(); qc.invalidateQueries({ queryKey: ["bench-hist"] }); }}
              disabled={isFetching}
              title="Re-run the benchmark over the golden set"
            >
              {isFetching ? "running…" : "re-run"}
            </button>
          </div>
        </div>
        <div className="relative grid w-full min-w-0 gap-2 grid-cols-1 sm:grid-cols-2 md:grid-cols-5 text-[11px]">
          <BenchStat label="F1"
                     value={fmtPct(data.relevance.f1, 1)}
                     delta={deltaF1}
                     trail={trail.f1}
                     tone={data.relevance.f1 < 0.8 ? "bad" : "good"} />
          <BenchStat label="precision"
                     value={fmtPct(data.relevance.precision, 1)}
                     delta={deltaPrec}
                     trail={trail.prec} />
          <BenchStat label="recall"
                     value={fmtPct(data.relevance.recall, 1)}
                     delta={deltaRec}
                     trail={trail.rec} />
          <BenchStat label="symbol recall"
                     value={data.symbol_recall != null ? fmtPct(data.symbol_recall, 1) : "—"}
                     delta={deltaSym}
                     trail={trail.sym} />
          <BenchStat label="sentiment acc"
                     value={data.sentiment_accuracy != null ? fmtPct(data.sentiment_accuracy, 1) : "—"}
                     delta={deltaSent}
                     trail={trail.sent} />
        </div>
      </section>

      <section className="grid w-full min-w-0 lg:grid-cols-2 gap-3">
        <div className="card w-full min-w-0">
          <h2 className="label mb-2">asset-class F1</h2>
          <ScoreBars items={data.asset_class_f1} />
        </div>
        <div className="card w-full min-w-0">
          <h2 className="label mb-2">reason-code F1</h2>
          <ScoreBars items={data.reason_code_f1} />
        </div>
      </section>

      <section className="card w-full min-w-0">
        <h2 className="label mb-2">per-item</h2>
        <div className="overflow-x-auto">
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
        </div>
        {failures.length > 0 && (
          <p className="mt-3 text-xs text-bad" role="status">
            {failures.length} disagreement{failures.length === 1 ? "" : "s"} — see highlighted rows.
          </p>
        )}
      </section>

      {history.data && history.data.history.length > 0 && (
        <section className="card w-full min-w-0">
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

function BenchStat({
  label,
  value,
  delta,
  trail,
  tone,
}: {
  label: string;
  value: string;
  delta: number | null;
  trail: number[];
  tone?: "good" | "bad";
}) {
  const valueCls = tone === "good" ? "text-good" : tone === "bad" ? "text-bad" : "";
  // Delta is already in [0,1] units (precision/recall/F1 ratios), so the
  // arithmetic difference equals "percentage points" — display as `pp`.
  const deltaCls =
    delta == null
      ? "text-[color:var(--fg-muted)]"
      : delta > 0.0005
        ? "text-good"
        : delta < -0.0005
          ? "text-bad"
          : "text-[color:var(--fg-dim)]";
  const arrow =
    delta == null ? "·" : delta > 0.0005 ? "▲" : delta < -0.0005 ? "▼" : "→";
  return (
    <div className="w-full min-w-0 rounded-md border border-[color:var(--border-subtle)] bg-[color:var(--bg-elev2)]/40 px-3 py-2">
      <div className="flex items-baseline justify-between gap-2">
        <div className="text-[9px] uppercase tracking-wider text-[color:var(--fg-muted)]">{label}</div>
        <Sparkline points={trail} className="text-accent" />
      </div>
      <div className={`mt-0.5 text-sm font-semibold tabular-nums ${valueCls}`}>{value}</div>
      <div className={`text-[10px] tabular-nums ${deltaCls}`}>
        {delta == null
          ? trail.length > 0 ? `${trail.length} run${trail.length === 1 ? "" : "s"}` : "first run"
          : `${arrow} ${delta >= 0 ? "+" : ""}${(delta * 100).toFixed(1)}pp vs prev`}
      </div>
    </div>
  );
}

function ScoreBars({ items }: { items: Record<string, number> }) {
  const entries = Object.entries(items).sort(([, a], [, b]) => b - a);
  if (entries.length === 0) return <p className="text-xs text-[color:var(--fg-dim)]">none</p>;
  return (
  <ul className="grid w-full min-w-0 gap-1.5">
      {entries.map(([k, v]) => (
        <li key={k} className="grid w-full min-w-0 grid-cols-[minmax(0,120px)_minmax(0,1fr)_minmax(0,50px)] gap-2 items-center text-xs">
          <span>{k}</span>
          <span className="h-2 min-w-0 rounded bg-[color:var(--bg-elev2)] overflow-hidden">
            <span className={`block h-full ${v >= 0.8 ? "bg-good" : v >= 0.5 ? "bg-warn" : "bg-bad"}`}
                  style={{ width: `${100 * v}%` }} />
          </span>
          <span className="text-right tabular-nums">{fmtPct(v, 0)}</span>
        </li>
      ))}
    </ul>
  );
}
