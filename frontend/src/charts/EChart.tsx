import { lazy, Suspense, useMemo, type ComponentType, forwardRef, useRef, useState, useEffect, type KeyboardEvent, type Ref, type CSSProperties } from "react";
import type { EChartsOption } from "echarts";
import { useTheme } from "@/hooks/useTheme";
import { Skeleton } from "@/components/Skeleton";

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
type ReactEChartsLike = ComponentType<any>;

// Helper to determine maximum length of data in the options object
const getDataLength = (option: EChartsOption): number => {
  if (!option.series) return 0;
  const seriesArray = Array.isArray(option.series) ? option.series : [option.series];
  let maxLen = 0;
  for (const s of seriesArray) {
    if (s && s.data && Array.isArray(s.data)) {
      maxLen = Math.max(maxLen, s.data.length);
    }
  }
  return maxLen;
};

// Helper to generate dynamic descriptions for screen readers on keyboard navigation
const getDataPointDescription = (option: EChartsOption, index: number): string => {
  if (!option.series) return "";
  const seriesArray = Array.isArray(option.series) ? option.series : [option.series];
  const firstSeries = seriesArray[0];
  if (!firstSeries || !firstSeries.data || !Array.isArray(firstSeries.data)) return "";
  
  const rawData = firstSeries.data[index];
  let valueStr = "";
  if (rawData !== undefined && rawData !== null) {
    if (Array.isArray(rawData)) {
      valueStr = rawData.map(v => String(v)).join(", ");
    } else if (typeof rawData === "object" && rawData !== null && "value" in rawData) {
      valueStr = String((rawData as any).value);
    } else {
      valueStr = String(rawData);
    }
  }

  // Check if there is an X axis category name for this index
  let categoryStr = "";
  if (option.xAxis) {
    const xAxisArray = Array.isArray(option.xAxis) ? option.xAxis : [option.xAxis];
    const firstX = xAxisArray[0] as any;
    if (firstX && firstX.data && Array.isArray(firstX.data)) {
      const cat = firstX.data[index];
      if (cat !== undefined && cat !== null) {
        categoryStr = typeof cat === "object" && cat !== null && "value" in cat ? String((cat as any).value) : String(cat);
      }
    }
  }

  const seriesName = firstSeries.name ? `${firstSeries.name}: ` : "";
  const categoryPrefix = categoryStr ? `${categoryStr} - ` : `Point ${index + 1}: `;
  return `${categoryPrefix}${seriesName}${valueStr}`;
};

// `React.lazy` requires a default-export shape, so we adapt the named export
// from echarts-for-react's core build. The default wrapper imports the entire
// ECharts registry; the core wrapper lets us register only the charts and
// components this app actually renders.
const LazyReactECharts = lazy<ReactEChartsLike>(async () => {
  const [
    { default: ReactEChartsCore },
    echarts,
    charts,
    components,
    renderers,
  ] = await Promise.all([
    import("echarts-for-react/lib/core"),
    import("echarts/core"),
    import("echarts/charts"),
    import("echarts/components"),
    import("echarts/renderers"),
  ]);

  echarts.use([
    charts.BarChart,
    charts.HeatmapChart,
    charts.LineChart,
    charts.RadarChart,
    charts.SankeyChart,
    charts.ScatterChart,
    components.DataZoomComponent,
    components.DatasetComponent,
    components.GridComponent,
    components.LegendComponent,
    components.MarkAreaComponent,
    components.MarkLineComponent,
    components.MarkPointComponent,
    components.RadarComponent,
    components.TitleComponent,
    components.TooltipComponent,
    components.TransformComponent,
    components.VisualMapComponent,
    components.AriaComponent,
    renderers.CanvasRenderer,
  ]);

  const RegisteredECharts = forwardRef<any, any>((props: any, ref) => (
    <ReactEChartsCore {...props} ref={ref} echarts={echarts} />
  ));
  return { default: RegisteredECharts as any };
});

/**
 * Theme-aware ECharts wrapper. Avoids importing the heavy default registry from
 * echarts - we pass tokens via the option object directly so the bundle stays
 * lean and the chart respects the live theme toggle.
 *
 * The underlying `echarts` + `echarts-for-react` modules are loaded lazily via
 * `React.lazy`, keeping the ~1MB charts chunk out of the initial bundle. The
 * skeleton placeholder reserves the chart's vertical space so layout doesn't
 * jump when the bundle arrives.
 */
export function EChart({ option, height = 240, className, onClick }: Props) {
  const { theme } = useTheme();
  const [focusedIndex, setFocusedIndex] = useState<number | null>(null);
  const chartRef = useRef<any>(null);

  const dataLength = useMemo(() => getDataLength(option), [option]);

  // Sync highlighting and tooltip when focusedIndex changes
  useEffect(() => {
    const chartInstance = chartRef.current?.getEchartsInstance?.();
    if (!chartInstance) return;

    if (focusedIndex !== null && dataLength > 0) {
      chartInstance.dispatchAction({
        type: "downplay",
      });
      chartInstance.dispatchAction({
        type: "highlight",
        seriesIndex: 0,
        dataIndex: focusedIndex,
      });
      chartInstance.dispatchAction({
        type: "showTip",
        seriesIndex: 0,
        dataIndex: focusedIndex,
      });
    } else {
      chartInstance.dispatchAction({
        type: "downplay",
      });
      chartInstance.dispatchAction({
        type: "hideTip",
      });
    }
  }, [focusedIndex, dataLength]);

  const handleKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    if (dataLength === 0) return;

    if (e.key === "ArrowRight") {
      e.preventDefault();
      setFocusedIndex((prev) => (prev === null ? 0 : (prev + 1) % dataLength));
    } else if (e.key === "ArrowLeft") {
      e.preventDefault();
      setFocusedIndex((prev) => (prev === null ? dataLength - 1 : (prev - 1 + dataLength) % dataLength));
    } else if (e.key === "Escape") {
      e.preventDefault();
      setFocusedIndex(null);
      e.currentTarget.blur();
    }
  };

  const handleBlur = () => {
    setFocusedIndex(null);
  };

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
      aria: {
        show: true,
        ...(option.aria as any),
      },
      ...option,
    } as any;
  }, [option, theme]);

  return (
    <div
      className={`relative outline-none focus-visible:outline-2 focus-visible:outline-accent focus-visible:outline-offset-2 rounded-lg ${className || ""}`}
      tabIndex={0}
      role="application"
      aria-label={option.title && typeof option.title === 'object' && 'text' in option.title ? `Chart: ${option.title.text}. Use Left and Right Arrow keys to navigate data points.` : "Interactive chart. Use Left and Right Arrow keys to navigate data points."}
      onKeyDown={handleKeyDown}
      onBlur={handleBlur}
    >
      <Suspense
        fallback={
          <div
            style={{ height }}
            className="w-full rounded-lg border border-[color:var(--border-muted)] bg-[color:var(--bg-elev2)] p-4 flex flex-col justify-between"
            aria-busy="true"
            aria-label="Loading chart"
          >
            {/* Title & Legend mockups */}
            <div className="flex justify-between items-center mb-2">
              <Skeleton className="h-4 w-1/4" />
              <div className="flex gap-2 w-1/3 justify-end">
                <Skeleton className="h-3 w-8" />
                <Skeleton className="h-3 w-8" />
              </div>
            </div>
            {/* Chart bars/lines simulation */}
            <div className="flex-1 flex items-end gap-3 px-2 py-4">
              <Skeleton className="h-1/3 w-full" />
              <Skeleton className="h-1/2 w-full" />
              <Skeleton className="h-2/3 w-full" />
              <Skeleton className="h-3/4 w-full" />
              <Skeleton className="h-1/2 w-full" />
              <Skeleton className="h-2/3 w-full" />
              <Skeleton className="h-5/6 w-full" />
              <Skeleton className="h-3/4 w-full" />
            </div>
            {/* X-Axis labels mockups */}
            <div className="flex justify-between items-center mt-2 border-t border-[color:var(--border-muted)] pt-2">
              <Skeleton className="h-3 w-12" />
              <Skeleton className="h-3 w-12" />
              <Skeleton className="h-3 w-12" />
            </div>
          </div>
        }
      >
        <LazyReactECharts
          ref={chartRef}
          option={merged}
          style={{ height, width: "100%" }}
          notMerge
          lazyUpdate
          opts={{ renderer: "canvas" }}
          onEvents={onClick ? { click: onClick } : undefined}
        />
      </Suspense>
      {focusedIndex !== null && (
        <div className="sr-only" aria-live="polite">
          {getDataPointDescription(option, focusedIndex)}
        </div>
      )}
    </div>
  );
}

