/**
 * Backtest page — prediction calibration of stub vs DeepSeek (v31, task #117).
 *
 * Sibling page to /benchmark: same hero+tiles+chart skeleton, different
 * data source. Where the benchmark grades the pipeline against a curated
 * golden set, the backtest grades it against DeepSeek's review as
 * "expert ground truth" — useful once the analyst has accumulated a few
 * hundred paired reviews and wants to know whether the cheap stub is
 * really tracking the expensive reviewer.
 *
 * Layout mirrors BenchmarkPage so the visual rhythm of /benchmark and
 * /backtest reads as one family:
 *   1. Accent-gradient hero with a dynamic h1 + sample-size selector
 *   2. KPI tile row: items_evaluated, MAE, signed error, max error
 *   3. ECharts bar chart — per-bin avg predicted vs avg ground truth
 *   4. Predictions sample table (up to 50 rows)
 *
 * Empty state lives below the hero so the page header still tells the
 * analyst what they're looking at even when zero paired reviews exist.
 */

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { t, useLang } from "@/lib/i18n";
import { Skeleton, ErrorBox } from "@/components/Skeleton";
import { EChart } from "@/charts/EChart";

const SAMPLE_OPTIONS: ReadonlyArray<number> = [50, 100, 200, 500, 1000];

export function BacktestPage() {
  // Subscribe to locale changes so the eyebrow + tile labels react to swap.
  useLang();
  const [sampleSize, setSampleSize] = useState<number>(200);
  const { data, isLoading, error, refetch, isFetching } = useQuery({
    queryKey: ["backtest", sampleSize],
    queryFn: () => api.backtest(sampleSize),
    // Backtest is read-only and cheap — refresh on demand, not on a timer.
    staleTime: 60_000,
  });

  if (isLoading) return <Skeleton className="h-72" />;
  if (error) return <ErrorBox err={error} />;
  if (!data) return null;

  const evaluated = data.summary.items_evaluated;
  const mae = data.summary.mean_abs_error;
  const isEmpty = evaluated === 0;

  // Dynamic headline — anchors the page on the number the analyst cares
  // most about. Empty state nudges them toward DeepSeek enablement
  // instead of leaving the hero feeling vacant.
  const headline = isEmpty
    ? "No paired reviews yet — backtest is waiting for ground truth"
    : `Mean abs error ${(mae * 100).toFixed(1)}pp across ${evaluated} paired review${
        evaluated === 1 ? "" : "s"
      }`;

  return (
    <div className="grid w-full min-w-0 gap-5">
      {/* Hero — same accent-gradient pattern as the benchmark page so the
          two evaluation surfaces read as one family. */}
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
                {t("backtest.eyebrow")}
              </div>
              <h1 className="text-lg font-semibold mt-0.5 tracking-tight">
                {headline}
              </h1>
              <div className="mt-1 text-[11px] text-[color:var(--fg-muted)]">
                ran {new Date(data.ran_at).toLocaleString()} · sample size{" "}
                {data.sample_size}
              </div>
            </div>
          </div>
          <div className="flex max-w-full flex-wrap items-center gap-2">
            <label
              className="flex items-center gap-1 text-[10px] text-[color:var(--fg-muted)]"
              title="How many recent paired reviews to evaluate"
            >
              <span>sample</span>
              <select
                aria-label="Sample size"
                className="bg-[color:var(--bg-elev2)] border border-[color:var(--border)] rounded px-1.5 py-0.5 text-[11px]"
                value={sampleSize}
                onChange={(e) => setSampleSize(Number(e.target.value))}
                disabled={isFetching}
              >
                {SAMPLE_OPTIONS.map((n) => (
                  <option key={n} value={n}>
                    {n}
                  </option>
                ))}
              </select>
            </label>
            <button
              className="btn shrink-0"
              onClick={() => refetch()}
              disabled={isFetching}
              title="Re-evaluate against the latest paired reviews"
            >
              {isFetching ? "running…" : "re-run"}
            </button>
          </div>
        </div>
        <div className="relative grid w-full min-w-0 gap-2 grid-cols-1 sm:grid-cols-2 md:grid-cols-4 text-[11px]">
          <BacktestStat
            label="items evaluated"
            value={evaluated.toString()}
            hint="paired (stub, deepseek) rows"
          />
          <BacktestStat
            label="mean abs error"
            value={isEmpty ? "—" : `${(mae * 100).toFixed(2)}pp`}
            tone={isEmpty ? undefined : mae < 0.1 ? "good" : mae > 0.2 ? "bad" : "warn"}
            hint="lower is better"
          />
          <BacktestStat
            label="signed error"
            value={
              isEmpty
                ? "—"
                : `${data.summary.mean_signed_error >= 0 ? "+" : ""}${(
                    data.summary.mean_signed_error * 100
                  ).toFixed(2)}pp`
            }
            hint="positive ⇒ stub under-scores"
          />
          <BacktestStat
            label="max abs error"
            value={isEmpty ? "—" : `${(data.summary.max_abs_error * 100).toFixed(2)}pp`}
            hint="worst-case disagreement"
          />
        </div>
      </section>

      {/* Calibration chart — bar pair per quintile of predicted scores so
          the analyst can see at a glance whether the stub's "70-80%" bucket
          really lines up with DeepSeek's actual rating. */}
      <section className="card w-full min-w-0">
        <div className="flex items-baseline justify-between gap-2 mb-2">
          <h2 className="label">calibration · predicted vs ground truth</h2>
          <span className="text-[10px] text-[color:var(--fg-muted)]">
            {data.calibration_bins.length} populated bin
            {data.calibration_bins.length === 1 ? "" : "s"}
          </span>
        </div>
        {data.calibration_bins.length === 0 ? (
          <EmptyState />
        ) : (
          <EChart
            height={240}
            option={{
              tooltip: { trigger: "axis", axisPointer: { type: "shadow" } },
              legend: { top: 0 },
              grid: { left: 40, right: 16, top: 28, bottom: 28 },
              xAxis: {
                type: "category",
                data: data.calibration_bins.map(
                  (b) => `${b.bin_low.toFixed(1)}–${b.bin_high.toFixed(1)}`
                ),
                axisLabel: { fontSize: 10 },
              },
              yAxis: { type: "value", min: 0, max: 1, axisLabel: { fontSize: 10 } },
              series: [
                {
                  type: "bar",
                  name: "avg predicted",
                  data: data.calibration_bins.map((b) => b.avg_predicted_score),
                  itemStyle: { color: "rgba(99,102,241,0.85)" },
                },
                {
                  type: "bar",
                  name: "avg ground truth",
                  data: data.calibration_bins.map((b) => b.avg_ground_truth_score),
                  itemStyle: { color: "rgba(34,197,94,0.85)" },
                },
              ],
            }}
          />
        )}
        {data.calibration_bins.length > 0 && (
          <ul className="mt-3 grid w-full min-w-0 gap-1 text-[10px] text-[color:var(--fg-muted)]">
            {data.calibration_bins.map((b) => (
              <li
                key={`${b.bin_low}-${b.bin_high}`}
                className="grid w-full min-w-0 grid-cols-[minmax(0,110px)_minmax(0,60px)_minmax(0,1fr)] gap-2 items-center tabular-nums"
              >
                <span>
                  bin {b.bin_low.toFixed(1)}–{b.bin_high.toFixed(1)}
                </span>
                <span>n={b.predicted_count}</span>
                <span
                  className={
                    Math.abs(b.calibration_gap) < 0.05
                      ? "text-good"
                      : Math.abs(b.calibration_gap) > 0.15
                        ? "text-bad"
                        : "text-warn"
                  }
                >
                  gap {b.calibration_gap >= 0 ? "+" : ""}
                  {(b.calibration_gap * 100).toFixed(2)}pp
                </span>
              </li>
            ))}
          </ul>
        )}
      </section>

      {/* Predictions sample — the raw rows so the analyst can spot-check.
          Capped at 50 by the backend to keep the wire payload small. */}
      <section className="card w-full min-w-0">
        <div className="flex items-baseline justify-between gap-2 mb-2">
          <h2 className="label">predictions sample</h2>
          <span className="text-[10px] text-[color:var(--fg-muted)]">
            up to 50 most recent paired rows
          </span>
        </div>
        {data.predictions_sample.length === 0 ? (
          <EmptyState />
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr className="text-left text-[color:var(--fg-dim)]">
                  <th className="py-1">capture_id</th>
                  <th>predicted</th>
                  <th>ground truth</th>
                  <th>delta</th>
                </tr>
              </thead>
              <tbody>
                {data.predictions_sample.map((p, i) => (
                  // Fold the row index into the fallback key: two null-capture
                  // rows can share identical predicted/ground-truth scores
                  // (e.g. both 0.000/0.000), which would collide on a
                  // score-only key and produce duplicate React keys.
                  <tr key={p.capture_id ?? `row-${i}`}>
                    <td className="py-1 pr-2 font-mono">{p.capture_id ?? "—"}</td>
                    <td className="tabular-nums">{p.predicted_score.toFixed(3)}</td>
                    <td className="tabular-nums">{p.ground_truth_score.toFixed(3)}</td>
                    <td
                      className={`tabular-nums ${
                        Math.abs(p.delta) < 0.05
                          ? "text-good"
                          : Math.abs(p.delta) > 0.2
                            ? "text-bad"
                            : "text-warn"
                      }`}
                    >
                      {p.delta >= 0 ? "+" : ""}
                      {p.delta.toFixed(3)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  );
}

/**
 * Shared stat tile — same visual rhythm as the Benchmark page so the two
 * evaluation surfaces feel like siblings. Tone-driven coloring is opt-in:
 * leave `tone` undefined when the metric doesn't have a "good" direction
 * (e.g. signed error, where zero is best and the sign is informational).
 */
function BacktestStat({
  label,
  value,
  tone,
  hint,
}: {
  label: string;
  value: string;
  tone?: "good" | "bad" | "warn";
  hint?: string;
}) {
  const valueCls =
    tone === "good"
      ? "text-good"
      : tone === "bad"
        ? "text-bad"
        : tone === "warn"
          ? "text-warn"
          : "";
  return (
    <div className="w-full min-w-0 rounded-md border border-[color:var(--border-subtle)] bg-[color:var(--bg-elev2)]/40 px-3 py-2">
      <div className="text-[9px] uppercase tracking-wider text-[color:var(--fg-muted)]">
        {label}
      </div>
      <div className={`mt-0.5 text-sm font-semibold tabular-nums ${valueCls}`}>{value}</div>
      {hint && (
        <div className="text-[10px] text-[color:var(--fg-muted)]">{hint}</div>
      )}
    </div>
  );
}

/**
 * Friendly empty state — reused by the chart + table so the user always
 * gets the same explanatory copy when DeepSeek hasn't paired anything yet.
 */
function EmptyState() {
  return (
    <div className="py-8 text-center text-xs text-[color:var(--fg-dim)]">
      <p className="mb-1">No paired (stub, DeepSeek) reviews available.</p>
      <p className="text-[10px] text-[color:var(--fg-muted)]">
        Enable DeepSeek in Settings → Reviewers and run a few replays so both
        reviewers score the same captures.
      </p>
    </div>
  );
}
