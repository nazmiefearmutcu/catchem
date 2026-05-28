import { lazy, Suspense, useMemo, type ComponentType } from "react";
import type { EChartsOption } from "echarts";
import { useTheme } from "@/hooks/useTheme";

interface Props {
  option: EChartsOption;
  height?: number;
  className?: string;
  /**
   * Optional click handler. Receives the raw ECharts click params (which
   * include `componentType`, `seriesType`, `value`, `dataIndex`, etc.).
   * Wired to ReactECharts' `onEvents.click` — the standard surface for
   * heatmap/sankey/scatter drill-downs.
   */
  onClick?: (params: unknown) => void;
}

/**
 * Props shape for the deferred ECharts wrapper. We mirror only the surface
 * we actually consume so the inner module can be lazy-loaded without
 * dragging the heavy echarts core into the initial bundle.
 */
type ReactEChartsLike = ComponentType<{
  option: EChartsOption;
  style?: React.CSSProperties;
  notMerge?: boolean;
  lazyUpdate?: boolean;
  opts?: { renderer?: "canvas" | "svg" };
  onEvents?: Record<string, (params: unknown) => void>;
}>;

// `React.lazy` requires a default-export shape, so we adapt the named export
// (`echarts-for-react` ships ESM with `default` already, but importing
// `echarts` alongside it inside the same dynamic import keeps both heavy
// modules off the critical path — they end up in the same async chunk that
// rollup splits out of `manualChunks.charts`).
const LazyReactECharts = lazy<ReactEChartsLike>(async () => {
  // Side-effect import to register echarts core before the wrapper mounts.
  // Both modules live in the `charts` manual chunk, so this single dynamic
  // import pulls the whole charting bundle on demand.
  await import("echarts");
  const mod = await import("echarts-for-react");
  return { default: mod.default as ReactEChartsLike };
});

/**
 * Theme-aware ECharts wrapper. Avoids importing the heavy 'theme registry' from
 * echarts — we pass tokens via the option object directly so the bundle stays
 * lean and the chart respects the live theme toggle.
 *
 * The underlying `echarts` + `echarts-for-react` modules are loaded lazily via
 * `React.lazy`, keeping the ~1MB charts chunk out of the initial bundle. The
 * skeleton placeholder reserves the chart's vertical space so layout doesn't
 * jump when the bundle arrives.
 */
export function EChart({ option, height = 240, className, onClick }: Props) {
  const { theme } = useTheme();
  const merged: EChartsOption = useMemo(() => {
    const fg = theme === "dark" ? "#e7ebf0" : "#0e1014";
    const dim = theme === "dark" ? "#9aa3b2" : "#525968";
    const border = theme === "dark" ? "#232838" : "#e3e6eb";
    return {
      backgroundColor: "transparent",
      textStyle: { color: fg, fontFamily: "ui-monospace, monospace", fontSize: 11 },
      grid: { left: 32, right: 16, top: 16, bottom: 24, containLabel: true, ...(option.grid as object | undefined) },
      legend: { textStyle: { color: dim, fontSize: 10 }, top: 0, ...(option.legend as object | undefined) },
      tooltip: {
        backgroundColor: theme === "dark" ? "#1f2531" : "#ffffff",
        borderColor: border,
        borderWidth: 1,
        textStyle: { color: fg, fontSize: 11 },
        ...(option.tooltip as object | undefined),
      },
      xAxis: option.xAxis && {
        ...(option.xAxis as object),
        axisLine: { lineStyle: { color: border } },
        axisLabel: { color: dim, fontSize: 10 },
        splitLine: { lineStyle: { color: border } },
      },
      yAxis: option.yAxis && {
        ...(option.yAxis as object),
        axisLine: { lineStyle: { color: border } },
        axisLabel: { color: dim, fontSize: 10 },
        splitLine: { lineStyle: { color: border } },
      },
      ...option,
    };
  }, [option, theme]);

  return (
    <div className={className}>
      <Suspense
        fallback={
          <div
            style={{ height, width: "100%" }}
            className="rounded bg-[color:var(--bg-elev2)] animate-skeleton-shimmer-wrap"
            aria-busy="true"
            aria-label="Loading chart"
          />
        }
      >
        <LazyReactECharts
          option={merged}
          style={{ height, width: "100%" }}
          notMerge
          lazyUpdate
          opts={{ renderer: "canvas" }}
          onEvents={onClick ? { click: onClick } : undefined}
        />
      </Suspense>
    </div>
  );
}
