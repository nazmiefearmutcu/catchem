import { useQuery } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";
import { api, fmtDate, fmtRel, fmtScore, safeHref } from "@/lib/api";
import { freshnessLabel, useTick } from "@/lib/freshness";
import { Pill } from "@/components/Pill";
import { Skeleton, ErrorBox, EmptyState } from "@/components/Skeleton";
import { Sparkline } from "@/components/Sparkline";
import { Icon } from "@/components/Icon";
import { EChart } from "@/charts/EChart";
import type { SymbolSentimentTrend } from "@/types/api";

export function SymbolDetailPage() {
  // Re-render every 30s so the hero freshness suffix keeps ticking.
  useTick();
  const { symbol = "" } = useParams();
  const { data, dataUpdatedAt, isLoading, error } = useQuery({
    queryKey: ["symbol", symbol],
    queryFn: () => api.symbol(symbol, 100),
  });
  // Sentiment trend over the trailing 30 days — feeds both the stacked
  // area chart (last 7d slice) and the 30d mention-velocity sparkline.
  // Loaded in parallel; we fall through to a graceful "no data" message
  // in the section bodies when the query is pending or empty.
  const { data: trendData } = useQuery({
    queryKey: ["symbol-sentiment-trend", symbol, 30],
    queryFn: () => api.symbolSentimentTrend(symbol, 30),
    enabled: Boolean(symbol),
  });

  if (isLoading) return <Skeleton className="h-72" />;
  if (error) return <ErrorBox err={error} />;
  if (!data) return null;

  // ── Hero stats ──────────────────────────────────────────────────────────
  // Top reason: highest count in reason_distribution (ties → first by Object iteration order).
  const reasonEntries = Object.entries(data.reason_distribution);
  const topReason = reasonEntries.length
    ? reasonEntries.reduce((acc, cur) => (cur[1] > acc[1] ? cur : acc))
    : null;

  // Dominant sentiment: highest count; map to good/warn/bad tones.
  const sentimentEntries = Object.entries(data.sentiment_distribution);
  const dominantSent = sentimentEntries.length
    ? sentimentEntries.reduce((acc, cur) => (cur[1] > acc[1] ? cur : acc))
    : null;
  const sentToneMap: Record<string, "good" | "warn" | "bad" | undefined> = {
    positive: "good",
    negative: "bad",
    mixed: "warn",
  };
  const dominantTone = dominantSent ? sentToneMap[dominantSent[0]] : undefined;

  // Latest mention: most recent published_ts across items.
  const latestTs = data.items
    .map((it) => it.published_ts)
    .filter((t): t is string => typeof t === "string" && t.length > 0)
    .sort()
    .at(-1) ?? null;

  // Sentiment summary line: "positive 4 · neutral 6 · negative 2".
  const sentSummary = sentimentEntries
    .map(([k, v]) => `${k} ${v}`)
    .join(" · ");

  const mentionsPlural = data.items.length === 1 ? "" : "s";

  return (
    <div className="grid gap-5">
      {/* Premium hero — matches Overview / Symbols / Ops / Model Controls / Settings / Help / Benchmark. */}
      <section className="relative overflow-hidden rounded-xl border border-accent/40 hero-gradient p-6">
        <div
          aria-hidden
          className="pointer-events-none absolute -top-20 -left-20 h-48 w-48 rounded-full bg-accent/20 blur-3xl"
        />
        <div className="relative flex flex-wrap items-start justify-between gap-3 mb-3">
          <div className="flex items-center gap-3">
            <span className="relative flex h-2 w-2">
              <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-accent opacity-75" />
              <span className="relative inline-flex h-2 w-2 rounded-full bg-accent" />
            </span>
            <div>
              <div className="text-[10px] uppercase tracking-[0.25em] text-accent font-semibold">
                Symbol detail · {data.symbol}
              </div>
              <h1 className="text-lg font-semibold mt-0.5 tracking-tight text-good">
                {data.symbol}
              </h1>
              <div className="mt-1 text-[11px] text-[color:var(--fg-muted)]">
                {data.items.length} mention{mentionsPlural}
                {sentSummary && <> · {sentSummary}</>}
                {" · "}{reasonEntries.length} distinct reason{reasonEntries.length === 1 ? "" : "s"}
                <span className="text-[10px] text-[color:var(--fg-dim)]"> · {freshnessLabel(dataUpdatedAt)}</span>
              </div>
            </div>
          </div>
          <Link
            to="/symbols"
            className="chip text-[10px] hover:bg-[color:var(--bg-elev2)] shrink-0"
          >
            ← all symbols
          </Link>
        </div>
        <div className="relative grid gap-2 grid-cols-1 sm:grid-cols-2 md:grid-cols-4 text-[11px]">
          <SymbolStat
            label="mentions"
            value={String(data.items.length)}
            hint={`${reasonEntries.length} reason${reasonEntries.length === 1 ? "" : "s"}`}
          />
          <SymbolStat
            label="top reason"
            value={topReason ? topReason[0] : "—"}
            hint={topReason ? `${topReason[1]} mention${topReason[1] === 1 ? "" : "s"}` : undefined}
          />
          <SymbolStat
            label="dominant sentiment"
            value={dominantSent ? dominantSent[0] : "—"}
            hint={dominantSent ? `${dominantSent[1]} record${dominantSent[1] === 1 ? "" : "s"}` : undefined}
            tone={dominantTone}
          />
          <SymbolStat
            label="latest mention"
            value={latestTs ? fmtRel(latestTs) : "—"}
            hint={latestTs ? fmtDate(latestTs) : undefined}
          />
        </div>
      </section>

      <ReasonDistribution distribution={data.reason_distribution} />

      <section className="card">
        <h2 className="label mb-2">records mentioning {data.symbol}</h2>
        {data.items.length === 0 ? <EmptyState title="No records" action={<Link to="/symbols" className="btn">← back to symbols</Link>} /> : (
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
                    {href && (
                      <a href={href} target="_blank" rel="noopener noreferrer" className="ml-2 inline-flex align-middle text-[color:var(--fg-dim)] hover:text-accent">
                        <Icon name="external" size={12} />
                      </a>
                    )}
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

      {/* Deeper analytics — sentiment shape + mention velocity. Two-col on
          md+ so the wide stacked area sits next to the compact velocity
          tile; collapses to a single column on phones. */}
      <div className="grid gap-5 md:grid-cols-2">
        <SentimentTrend trend={trendData} symbol={data.symbol} />
        <MentionSparkline trend={trendData} />
      </div>
    </div>
  );
}

// ── Reason-code distribution ───────────────────────────────────────────────
// Horizontal bar of the top 10 reason codes — bars accent-coloured so the
// chart picks up theme changes via the CSS custom property.
function ReasonDistribution({ distribution }: { distribution: Record<string, number> }) {
  const entries = Object.entries(distribution).sort((a, b) => b[1] - a[1]).slice(0, 10);
  if (entries.length === 0) return null;
  // Reverse so the highest count sits at the top of the horizontal bar chart
  // (ECharts plots category axis bottom-up by default).
  const labels = entries.map(([k]) => k).reverse();
  const values = entries.map(([, v]) => v).reverse();
  return (
    <section className="card">
      <h2 className="label mb-2">reason distribution · top {entries.length}</h2>
      <EChart
        height={Math.max(180, entries.length * 22 + 40)}
        option={{
          grid: { left: 8, right: 24, top: 8, bottom: 24, containLabel: true },
          tooltip: { trigger: "axis", axisPointer: { type: "shadow" } },
          xAxis: { type: "value", minInterval: 1 },
          yAxis: { type: "category", data: labels },
          series: [
            {
              type: "bar",
              data: values,
              itemStyle: { color: "var(--accent)" },
              barMaxWidth: 18,
              label: { show: true, position: "right", fontSize: 10 },
            },
          ],
        }}
      />
    </section>
  );
}

// ── Sentiment trend over time ──────────────────────────────────────────────
// Stacked area of positive / neutral / negative counts over the trailing
// 7 days (UTC). Hard-coded semantic colours so the chart still reads even
// in themes where ``--good`` / ``--bad`` are subtly different — the user
// expects green-grey-red and we deliver it without theme drift.
function SentimentTrend({
  trend,
  symbol,
}: {
  trend: SymbolSentimentTrend | undefined;
  symbol: string;
}) {
  // Slice to the trailing 7 days; the API returns the full requested
  // window (30d), letting the velocity sparkline cover a longer horizon
  // without a second fetch.
  const last7 = (trend?.series ?? []).slice(-7);
  const hasAnyMention = last7.some((d) => d.positive + d.neutral + d.negative > 0);
  return (
    <section className="card">
      <h2 className="label mb-2">sentiment trend · 7d</h2>
      {!trend || !hasAnyMention ? (
        <EmptyState
          title="No sentiment data"
          hint={`No sentiment-tagged mentions of ${symbol} in the last 7 days.`}
        />
      ) : (
        <EChart
          height={220}
          option={{
            grid: { left: 8, right: 16, top: 32, bottom: 24, containLabel: true },
            legend: {
              top: 0,
              data: ["positive", "neutral", "negative"],
              textStyle: { fontSize: 10 },
            },
            tooltip: { trigger: "axis" },
            xAxis: { type: "category", data: last7.map((d) => d.day.slice(5)) },
            yAxis: { type: "value", minInterval: 1 },
            series: [
              {
                name: "positive",
                type: "line",
                stack: "sent",
                smooth: true,
                showSymbol: false,
                areaStyle: { opacity: 0.55 },
                lineStyle: { width: 1 },
                itemStyle: { color: "#3ad29f" },
                data: last7.map((d) => d.positive),
              },
              {
                name: "neutral",
                type: "line",
                stack: "sent",
                smooth: true,
                showSymbol: false,
                areaStyle: { opacity: 0.45 },
                lineStyle: { width: 1 },
                itemStyle: { color: "#9aa3b2" },
                data: last7.map((d) => d.neutral),
              },
              {
                name: "negative",
                type: "line",
                stack: "sent",
                smooth: true,
                showSymbol: false,
                areaStyle: { opacity: 0.55 },
                lineStyle: { width: 1 },
                itemStyle: { color: "#ff6b6b" },
                data: last7.map((d) => d.negative),
              },
            ],
          }}
        />
      )}
    </section>
  );
}

// ── Mention velocity sparkline ─────────────────────────────────────────────
// Total daily mention count over the trailing 30 days as a single sparkline,
// plus a short stats line (sum, peak, peak day). Sums all three sentiment
// buckets so the velocity counts every mention the trend endpoint sees.
function MentionSparkline({ trend }: { trend: SymbolSentimentTrend | undefined }) {
  const series = trend?.series ?? [];
  const dailyTotals = series.map((d) => d.positive + d.neutral + d.negative);
  const total = dailyTotals.reduce((a, b) => a + b, 0);
  const peak = dailyTotals.length ? Math.max(...dailyTotals) : 0;
  const peakDayIdx = peak > 0 ? dailyTotals.indexOf(peak) : -1;
  const peakDay = peakDayIdx >= 0 ? series[peakDayIdx].day : null;
  return (
    <section className="card">
      <h2 className="label mb-2">mention velocity · 30d</h2>
      {total === 0 ? (
        <EmptyState title="No mentions" hint="No mentions in the last 30 days." />
      ) : (
        <div className="flex flex-col gap-3">
          <div className="flex items-center gap-3">
            <div className="flex-1 text-accent">
              <Sparkline
                points={dailyTotals}
                width={280}
                height={48}
                strokeWidth={1.5}
                opacity={0.9}
                ariaLabel="30-day mention velocity"
                className="w-full h-12"
              />
            </div>
          </div>
          <div className="grid grid-cols-3 gap-2 text-[11px]">
            <div className="rounded-md border border-[color:var(--border-subtle)] bg-[color:var(--bg-elev2)]/40 px-3 py-2">
              <div className="text-[9px] uppercase tracking-wider text-[color:var(--fg-muted)]">total · 30d</div>
              <div className="mt-0.5 text-sm font-semibold tabular-nums">{total}</div>
            </div>
            <div className="rounded-md border border-[color:var(--border-subtle)] bg-[color:var(--bg-elev2)]/40 px-3 py-2">
              <div className="text-[9px] uppercase tracking-wider text-[color:var(--fg-muted)]">peak day</div>
              <div className="mt-0.5 text-sm font-semibold tabular-nums">{peak}</div>
              {peakDay && (
                <div className="text-[10px] text-[color:var(--fg-dim)] truncate">{peakDay}</div>
              )}
            </div>
            <div className="rounded-md border border-[color:var(--border-subtle)] bg-[color:var(--bg-elev2)]/40 px-3 py-2">
              <div className="text-[9px] uppercase tracking-wider text-[color:var(--fg-muted)]">days active</div>
              <div className="mt-0.5 text-sm font-semibold tabular-nums">
                {dailyTotals.filter((v) => v > 0).length}
              </div>
              <div className="text-[10px] text-[color:var(--fg-dim)]">of {dailyTotals.length}</div>
            </div>
          </div>
        </div>
      )}
    </section>
  );
}

function SymbolStat({
  label,
  value,
  hint,
  tone,
}: {
  label: string;
  value: string;
  hint?: string;
  tone?: "good" | "warn" | "bad";
}) {
  const cls =
    tone === "good"
      ? "text-good"
      : tone === "warn"
        ? "text-warn"
        : tone === "bad"
          ? "text-bad"
          : "";
  return (
    <div className="rounded-md border border-[color:var(--border-subtle)] bg-[color:var(--bg-elev2)]/40 px-3 py-2">
      <div className="text-[9px] uppercase tracking-wider text-[color:var(--fg-muted)]">{label}</div>
      <div className={`mt-0.5 text-sm font-semibold tabular-nums ${cls}`}>{value}</div>
      {hint && <div className="text-[10px] text-[color:var(--fg-dim)] truncate">{hint}</div>}
    </div>
  );
}
