import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { EChart } from "@/charts/EChart";
import { Skeleton, ErrorBox, EmptyState } from "@/components/Skeleton";

export function MarketMapPage() {
  const matrix = useQuery({ queryKey: ["matrix"], queryFn: api.matrix });
  const trends = useQuery({ queryKey: ["trends"], queryFn: () => api.trends(500) });

  return (
    <div className="grid gap-4">
      <section className="card">
        <h2 className="label mb-2">news-impact map: asset class x reason code</h2>
        {matrix.isLoading ? <Skeleton className="h-72" /> :
          matrix.error ? <ErrorBox err={matrix.error} /> :
          !matrix.data || matrix.data.asset_classes.length === 0 ? <EmptyState title="No matrix yet" hint="Run a replay first." /> : (
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
                  inRange: { color: ["#1f2531", "#3b82f6", "#fbbf24"] },
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
        <h2 className="label mb-2">news record trend by asset class</h2>
        {trends.isLoading ? <Skeleton className="h-56" /> :
          trends.error ? <ErrorBox err={trends.error} /> :
          trends.data && trends.data.buckets.length === 0 ? <EmptyState title="No timeline data" /> :
          trends.data && (
            <EChart
              height={280}
              option={{
                xAxis: { type: "category", data: trends.data.buckets, axisLabel: { rotate: 35 } },
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
