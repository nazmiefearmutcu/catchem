import ReactECharts from "echarts-for-react";
import type { EChartsOption } from "echarts";
import { useTheme } from "@/hooks/useTheme";

interface Props {
  option: EChartsOption;
  height?: number;
  className?: string;
}

/**
 * Theme-aware ECharts wrapper. Avoids importing the heavy 'theme registry' from
 * echarts — we pass tokens via the option object directly so the bundle stays
 * lean and the chart respects the live theme toggle.
 */
export function EChart({ option, height = 240, className }: Props) {
  const { theme } = useTheme();
  const fg = theme === "dark" ? "#e7ebf0" : "#0e1014";
  const dim = theme === "dark" ? "#9aa3b2" : "#525968";
  const border = theme === "dark" ? "#232838" : "#e3e6eb";
  const merged: EChartsOption = {
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
  return (
    <div className={className}>
      <ReactECharts
        option={merged}
        style={{ height, width: "100%" }}
        notMerge
        lazyUpdate
        opts={{ renderer: "canvas" }}
      />
    </div>
  );
}
