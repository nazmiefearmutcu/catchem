import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  api,
  fmtDate,
  fmtPct,
  fmtRel,
  fmtScore,
  safeHref,
  scoreToneClass,
} from "@/lib/api";
import { freshnessLabel, useTick } from "@/lib/freshness";
import { Pill } from "@/components/Pill";
import { ExportMenu } from "@/components/ExportMenu";
import { Icon } from "@/components/Icon";
import { Skeleton, EmptyState, ErrorBox } from "@/components/Skeleton";
import type {
  Agreement,
  CompareItem,
  CompareSummary,
  ReviewPayload,
  ReviewSide,
  ReviewsStatus,
} from "@/lib/api";

/**
 * Reviews / Compare dashboard.
 *
 * Surfaces three things:
 *   1. Status strip — DeepSeek enabled / sampling / cost dashboard
 *   2. Agreement summary — pairwise (stub vs DeepSeek) at a glance
 *   3. Disagreement spotlight — captures where the two reviewers
 *      diverge the most, drillable to a side-by-side diff drawer
 */
export function ReviewsComparePage() {
  // Re-render every 30s so the hero freshness suffix keeps ticking
  // between background refetches.
  useTick();
  const [selected, setSelected] = useState<string | null>(null);
  const [sortBy, setSortBy] = useState<"disagreement" | "newest" | "score_delta">(
    "disagreement",
  );
  // Diff-aware visibility chip strip (independent from the legacy sortBy).
  // - all              → no filter
  // - only_diff        → keep pairs where ANY axis differs (asset/reason/score/sentiment)
  // - largest_delta    → sort by abs(scoreDelta) desc (independent of sortBy)
  const [diffFilter, setDiffFilter] = useState<"all" | "only_diff" | "largest_delta">(
    "all",
  );
  // Test/smoke records (capture_ids starting with `demo-`) come from the
  // Replay/Upload paste pipeline and don't carry titles or domains. They
  // make the compare list look mostly "(untitled)" — opt-out for a real
  // news-only view.
  const [excludeDemo, setExcludeDemo] = useState<boolean>(true);

  const status = useQuery<ReviewsStatus>({
    queryKey: ["reviews-status"],
    queryFn: api.reviewsStatus,
    refetchInterval: 4_000,
  });
  const compare = useQuery({
    queryKey: ["reviews-compare"],
    queryFn: () => api.reviewsCompare(500),
    refetchInterval: 8_000,
  });

  const items = compare.data?.items ?? [];
  const summary = compare.data?.summary ?? null;

  const filtered = useMemo(
    () => (excludeDemo ? items.filter((it) => !it.capture_id.startsWith("demo-")) : items),
    [items, excludeDemo],
  );

  // Pre-compute the per-item diff once; reused for filtering, sorting and rendering.
  const withDiff = useMemo(
    () => filtered.map((it) => ({ item: it, diff: computeReviewDiff(it) })),
    [filtered],
  );

  // Diff summary chip strip on top of the list. Counts agreement vs disagreement,
  // the most-common addition by DeepSeek, and the mean score delta.
  const diffSummary = useMemo(() => buildDiffSummary(withDiff), [withDiff]);

  const visible = useMemo(() => {
    let xs = withDiff;
    if (diffFilter === "only_diff") {
      xs = xs.filter((row) => rowHasAnyDiff(row.diff));
    }
    const sortedXs = [...xs];
    if (diffFilter === "largest_delta") {
      sortedXs.sort(
        (a, b) => Math.abs(b.diff.scoreDelta) - Math.abs(a.diff.scoreDelta),
      );
    } else if (sortBy === "disagreement") {
      sortedXs.sort((a, b) => a.item.agreement.overall - b.item.agreement.overall);
    } else if (sortBy === "score_delta") {
      sortedXs.sort(
        (a, b) => b.item.agreement.score_delta - a.item.agreement.score_delta,
      );
    } else {
      sortedXs.sort((a, b) =>
        b.item.deepseek.created_at.localeCompare(a.item.deepseek.created_at),
      );
    }
    return sortedXs;
  }, [withDiff, diffFilter, sortBy]);
  const demoCount = items.length - filtered.length;

  const selectedItem = useMemo(
    () => items.find((it) => it.capture_id === selected) ?? null,
    [items, selected],
  );

  return (
    <div className="grid gap-4 lg:grid-cols-[1fr_320px]">
      <section className="grid gap-3">
        <ReviewsHero
          status={status.data}
          summary={summary}
          pairedCount={filtered.length}
          loading={status.isLoading || compare.isLoading}
          updatedAt={compare.dataUpdatedAt}
        />
        <StatusStrip status={status.data} loading={status.isLoading} />
        <SummaryCard summary={summary} loading={compare.isLoading} />
        <DiffSummaryStrip summary={diffSummary} loading={compare.isLoading} />
        <section className="card">
          <div className="mb-2 flex flex-wrap items-baseline justify-between gap-2">
            <div className="flex items-baseline gap-2">
              <h2 className="label">paired reviews</h2>
              <span className="text-[10px] text-[color:var(--fg-muted)]">
                {visible.length}
                {demoCount > 0 && excludeDemo && (
                  <span className="ml-1">· {demoCount} demo hidden</span>
                )}
              </span>
            </div>
            <div className="flex items-center gap-2 text-[10px] text-[color:var(--fg-dim)]">
              <label className="flex items-center gap-1 cursor-pointer">
                <input
                  type="checkbox"
                  checked={excludeDemo}
                  onChange={(e) => setExcludeDemo(e.target.checked)}
                  className="focus:outline-none focus-visible:ring-1 focus-visible:ring-accent rounded-sm"
                />
                <span>real news only</span>
              </label>
              <span>·</span>
              <span>sort:</span>
              {(["disagreement", "score_delta", "newest"] as const).map((k) => (
                <button
                  key={k}
                  type="button"
                  className={`chip text-[10px] focus:outline-none focus-visible:ring-1 focus-visible:ring-accent ${sortBy === k && diffFilter !== "largest_delta" ? "chip-active" : ""}`}
                  onClick={() => setSortBy(k)}
                  disabled={diffFilter === "largest_delta"}
                  title={
                    diffFilter === "largest_delta"
                      ? "sorting locked to largest score delta"
                      : undefined
                  }
                >
                  {k === "disagreement"
                    ? "biggest gap"
                    : k === "score_delta"
                      ? "score delta"
                      : "newest"}
                </button>
              ))}
            </div>
          </div>
          <div className="mb-2 flex flex-wrap items-center gap-2 text-[10px] text-[color:var(--fg-dim)]">
            <span>show:</span>
            {(
              [
                ["all", "all"],
                ["only_diff", "only disagreements"],
                ["largest_delta", "largest score delta"],
              ] as const
            ).map(([k, label]) => (
              <button
                key={k}
                type="button"
                className={`chip text-[10px] focus:outline-none focus-visible:ring-1 focus-visible:ring-accent ${diffFilter === k ? "chip-active" : ""}`}
                onClick={() => setDiffFilter(k)}
              >
                {label}
              </button>
            ))}
          </div>
          {compare.isLoading ? (
            <div className="grid gap-2">
              {Array.from({ length: 6 }).map((_, i) => (
                <Skeleton key={i} className="h-14" />
              ))}
            </div>
          ) : compare.error ? (
            <ErrorBox err={compare.error} />
          ) : visible.length === 0 ? (
            <EmptyState
              title="No paired reviews yet"
              hint="DeepSeek-sampled captures will appear here once the second-opinion reviewer fires. Enable + add the API key in Settings → DeepSeek."
              action={<Link to="/settings" className="btn focus:outline-none focus-visible:ring-1 focus-visible:ring-accent">Open Settings</Link>}
            />
          ) : (
            <ul className="divide-y divide-[color:var(--border)] rounded-md border border-[color:var(--border)] bg-[color:var(--bg-elev)]">
              {visible.map(({ item: it, diff }) => (
                <CompareRow
                  key={it.capture_id}
                  it={it}
                  diff={diff}
                  selected={selected === it.capture_id}
                  onSelect={() =>
                    setSelected(selected === it.capture_id ? null : it.capture_id)
                  }
                />
              ))}
            </ul>
          )}
        </section>
      </section>

      <aside className="grid gap-3 h-fit lg:sticky lg:top-3">
        {selectedItem ? (
          <DiffDrawer item={selectedItem} onClose={() => setSelected(null)} />
        ) : (
          <HelpRail />
        )}
      </aside>
    </div>
  );
}

// ── Diff math + summary ───────────────────────────────────────────────────

/**
 * Pure structural diff between the two review sides for a single pair.
 * Set membership is exact-string (case-sensitive) — matches what reviewers emit.
 */
export interface ReviewDiff {
  acAdded: string[]; // asset_classes only in DeepSeek
  acRemoved: string[]; // asset_classes only in stub
  acKept: string[]; // intersection
  rcAdded: string[]; // reason codes only in DeepSeek
  rcRemoved: string[]; // reason codes only in stub
  rcKept: string[];
  scoreDelta: number; // deepseek.score - stub.score
  sentimentChanged: boolean;
}

export function computeReviewDiff(item: CompareItem): ReviewDiff {
  const stubP = item.stub.payload as ReviewPayload;
  const dsP = item.deepseek.payload as ReviewPayload;
  const stubAC = new Set(stubP.asset_classes ?? []);
  const dsAC = new Set(dsP.asset_classes ?? []);
  const stubRC = new Set(stubP.impact_reason_codes ?? []);
  const dsRC = new Set(dsP.impact_reason_codes ?? []);
  return {
    acAdded: [...dsAC].filter((x) => !stubAC.has(x)).sort(),
    acRemoved: [...stubAC].filter((x) => !dsAC.has(x)).sort(),
    acKept: [...dsAC].filter((x) => stubAC.has(x)).sort(),
    rcAdded: [...dsRC].filter((x) => !stubRC.has(x)).sort(),
    rcRemoved: [...stubRC].filter((x) => !dsRC.has(x)).sort(),
    rcKept: [...dsRC].filter((x) => stubRC.has(x)).sort(),
    scoreDelta:
      (dsP.finance_relevance_score ?? 0) - (stubP.finance_relevance_score ?? 0),
    sentimentChanged: (stubP.sentiment_label ?? null) !== (dsP.sentiment_label ?? null),
  };
}

function rowHasAnyDiff(d: ReviewDiff): boolean {
  return (
    d.acAdded.length > 0 ||
    d.acRemoved.length > 0 ||
    d.rcAdded.length > 0 ||
    d.rcRemoved.length > 0 ||
    Math.abs(d.scoreDelta) > 0.01 ||
    d.sentimentChanged
  );
}

interface DiffSummary {
  total: number;
  agreed: number;
  differed: number;
  meanScoreDelta: number;
  topAddition: { label: string; n: number } | null;
}

function buildDiffSummary(
  rows: Array<{ item: CompareItem; diff: ReviewDiff }>,
): DiffSummary {
  const total = rows.length;
  if (total === 0) {
    return {
      total: 0,
      agreed: 0,
      differed: 0,
      meanScoreDelta: 0,
      topAddition: null,
    };
  }
  let differed = 0;
  let scoreSum = 0;
  const additionCounts = new Map<string, number>();
  for (const { diff } of rows) {
    if (rowHasAnyDiff(diff)) differed += 1;
    scoreSum += diff.scoreDelta;
    for (const v of diff.acAdded) {
      additionCounts.set(`ac:${v}`, (additionCounts.get(`ac:${v}`) ?? 0) + 1);
    }
    for (const v of diff.rcAdded) {
      additionCounts.set(`rc:${v}`, (additionCounts.get(`rc:${v}`) ?? 0) + 1);
    }
  }
  let top: { label: string; n: number } | null = null;
  for (const [key, n] of additionCounts) {
    if (top == null || n > top.n) {
      const [, label] = key.split(":", 2);
      top = { label, n };
    }
  }
  return {
    total,
    agreed: total - differed,
    differed,
    meanScoreDelta: scoreSum / total,
    topAddition: top,
  };
}

function DiffSummaryStrip({
  summary,
  loading,
}: {
  summary: DiffSummary;
  loading: boolean;
}) {
  if (loading) return null;
  if (summary.total === 0) return null;
  const deltaTone =
    summary.meanScoreDelta > 0.02
      ? "text-good"
      : summary.meanScoreDelta < -0.02
        ? "text-bad"
        : "text-[color:var(--fg-muted)]";
  const arrow =
    summary.meanScoreDelta > 0.02
      ? "↑"
      : summary.meanScoreDelta < -0.02
        ? "↓"
        : "·";
  const deltaSigned =
    (summary.meanScoreDelta >= 0 ? "+" : "") + summary.meanScoreDelta.toFixed(2);
  return (
    <section className="card grid gap-1.5" aria-label="Diff summary">
      <h2 className="label">diff summary</h2>
      <div className="flex flex-wrap items-center gap-2 text-[11px]">
        <Pill variant="good">{summary.agreed} agreed</Pill>
        <Pill variant={summary.differed > 0 ? "warn" : "default"}>
          {summary.differed} differed
        </Pill>
        {summary.topAddition && (
          <span className="text-[color:var(--fg-dim)]">
            most common addition by DeepSeek:{" "}
            <code className="font-mono text-good">{summary.topAddition.label}</code>{" "}
            in {summary.topAddition.n} of {summary.total} pairs
          </span>
        )}
        <span className={`tabular-nums ${deltaTone}`}>
          {arrow} mean score delta {deltaSigned}{" "}
          <span className="text-[color:var(--fg-muted)]">
            (DeepSeek vs stub)
          </span>
        </span>
      </div>
    </section>
  );
}

// ── Hero ──────────────────────────────────────────────────────────────────

function ReviewsHero({
  status,
  summary,
  pairedCount,
  loading,
  updatedAt,
}: {
  status: ReviewsStatus | undefined;
  summary: CompareSummary | null;
  pairedCount: number;
  loading: boolean;
  updatedAt: number;
}) {
  const n = summary?.n ?? 0;
  const agreement = summary?.mean_overall ?? null;
  const usdSpent = status?.usd_spent ?? 0;
  const usdCap = status?.usd_cap ?? 0;
  const sampling = status?.sampling_rate ?? 0;
  const model = status?.model ?? "deepseek-chat";

  const agreementTone =
    agreement == null
      ? ""
      : agreement >= 0.8
        ? "text-good"
        : agreement >= 0.6
          ? "text-warn"
          : "text-bad";

  const headline = loading
    ? "Reading the reviewer tape…"
    : n === 0
      ? "Awaiting first DeepSeek sample"
      : `DeepSeek vs Stub — ${n.toLocaleString()} paired review${n === 1 ? "" : "s"}`;

  const subtitleParts: string[] = [];
  if (agreement != null) subtitleParts.push(`agreement ${fmtPct(agreement, 1)}`);
  if (usdCap > 0) subtitleParts.push(`usd spend $${usdSpent.toFixed(2)} / $${usdCap.toFixed(2)}`);
  if (sampling > 0) subtitleParts.push(`sampling ${(sampling * 100).toFixed(0)}%`);

  return (
    <section className="relative overflow-hidden rounded-xl border border-accent/40 hero-gradient p-6">
      <div
        aria-hidden
        className="pointer-events-none absolute -top-20 -left-20 h-48 w-48 rounded-full bg-accent/20 blur-3xl"
      />
      <div className="relative flex flex-col gap-3 mb-3 sm:flex-row sm:items-start sm:justify-between">
        <div className="flex min-w-0 flex-1 items-center gap-3">
          <span className="relative flex h-2 w-2">
            <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-accent opacity-75" />
            <span className="relative inline-flex h-2 w-2 rounded-full bg-accent" />
          </span>
          <div>
            <div className="text-[10px] uppercase tracking-[0.25em] text-accent font-semibold">
              Reviews · second-opinion reviewer
            </div>
            <h1 className="text-lg font-semibold mt-0.5 tracking-tight">{headline}</h1>
            {subtitleParts.length > 0 && (
              <div className="mt-1 text-[11px] text-[color:var(--fg-muted)]">
                {subtitleParts.join(" · ")}
                <span className="text-[10px] text-[color:var(--fg-dim)]"> · {freshnessLabel(updatedAt)}</span>
              </div>
            )}
          </div>
        </div>
        <div className="flex max-w-full flex-wrap items-center gap-2 sm:justify-end">
          <ExportMenu
            label="export pairs"
            formats={["csv", "json"]}
            buildUrl={(f) => api.exportReviewsUrl(f, { limit: 500 })}
            filenameHint="catchem_reviews"
            hint="paired stub vs DeepSeek"
            testId="reviews-export"
          />
          <button
            type="button"
            className="chip hidden text-[10px] no-print hover:bg-[color:var(--bg-elev2)] sm:inline-flex focus:outline-none focus-visible:ring-1 focus-visible:ring-accent"
            onClick={() => window.print()}
            title="Print this page or save as PDF"
          >
            <span className="inline-flex items-center gap-1">
              <Icon name="print" />
              print / save PDF
            </span>
          </button>
          <span className="chip hidden text-[10px] sm:inline-flex" title={`model ${model}`}>
            {model}
          </span>
        </div>
      </div>
      <div className="relative grid gap-2 grid-cols-2 md:grid-cols-4">
        <HeroTile
          label="paired"
          value={loading ? "—" : pairedCount.toLocaleString()}
          hint={n !== pairedCount ? `${n.toLocaleString()} incl. demo` : "real news only"}
        />
        <HeroTile
          label="agreement"
          value={agreement == null ? "—" : fmtPct(agreement, 0)}
          hint={
            agreement == null
              ? "n=0"
              : agreement >= 0.8
                ? "strong consensus"
                : agreement >= 0.6
                  ? "mixed signal"
                  : "high disagreement"
          }
          valueClass={agreementTone}
        />
        <HeroTile
          label="usd spend"
          value={usdCap > 0 ? `$${usdSpent.toFixed(2)}` : "$—"}
          hint={usdCap > 0 ? `of $${usdCap.toFixed(2)} cap` : "no cap set"}
          valueClass={status?.exhausted ? "text-bad" : ""}
        />
        <HeroTile
          label="sampling"
          value={`${(sampling * 100).toFixed(0)}% rate`}
          hint="per ingested capture"
        />
      </div>
    </section>
  );
}

function HeroTile({
  label,
  value,
  hint,
  valueClass = "",
}: {
  label: string;
  value: string;
  hint?: string;
  valueClass?: string;
}) {
  return (
    <div className="rounded-md border border-[color:var(--border-subtle)] bg-[color:var(--bg-elev2)]/40 px-3 py-2">
      <div className="text-[9px] uppercase tracking-wider text-[color:var(--fg-muted)]">
        {label}
      </div>
      <div className={`mt-0.5 text-sm font-semibold tabular-nums ${valueClass}`}>{value}</div>
      {hint && (
        <div className="text-[10px] text-[color:var(--fg-dim)] truncate">{hint}</div>
      )}
    </div>
  );
}

// ── Status strip ──────────────────────────────────────────────────────────

function StatusStrip({
  status,
  loading,
}: {
  status: ReviewsStatus | undefined;
  loading: boolean;
}) {
  if (loading) return <Skeleton className="h-20" />;
  if (!status) return null;
  const pct = status.usd_cap > 0 ? (status.usd_spent / status.usd_cap) * 100 : 0;
  const tone =
    status.exhausted
      ? "bg-bad"
      : pct >= 80
        ? "bg-warn"
        : status.deepseek_ready
          ? "bg-good"
          : "bg-[color:var(--border)]";
  return (
    <div className="card grid gap-2">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h2 className="text-sm font-semibold">DeepSeek second-opinion reviewer</h2>
        <span className="text-[10px] text-[color:var(--fg-dim)]">
          model <code className="font-mono">{status.model}</code>
        </span>
      </div>
      <div className="grid grid-cols-2 gap-2 md:grid-cols-4">
        <Stat
          label="status"
          value={
            !status.deepseek_enabled
              ? "disabled"
              : !status.deepseek_keyed
                ? "no key"
                : status.exhausted
                  ? "budget hit"
                  : "ready"
          }
          tone={
            status.exhausted ? "bad" : status.deepseek_ready ? "good" : undefined
          }
        />
        <Stat
          label="sampling"
          value={`${(status.sampling_rate * 100).toFixed(0)}%`}
          hint="per ingested capture"
        />
        <Stat
          label="USD spent"
          value={`$${status.usd_spent.toFixed(4)}`}
          hint={`of $${status.usd_cap.toFixed(2)}`}
          tone={status.exhausted ? "bad" : undefined}
        />
        <Stat
          label="API calls"
          value={status.tokens.calls.toLocaleString()}
          hint={`${status.tokens.errors} error${status.tokens.errors === 1 ? "" : "s"}`}
          tone={status.tokens.errors > 0 ? "warn" : undefined}
        />
      </div>
      <div className="h-1.5 overflow-hidden rounded-full bg-[color:var(--bg-elev2)]">
        <div
          className={`h-full transition-[width] ${tone}`}
          style={{ width: `${Math.min(100, pct)}%` }}
          aria-hidden
        />
      </div>
      <div className="flex flex-wrap items-baseline justify-between gap-2 text-[10px] text-[color:var(--fg-muted)]">
        <span>
          {status.tokens.input.toLocaleString()} input · {status.tokens.output.toLocaleString()} output tokens
        </span>
        <span>primary <code className="font-mono">{status.primary_reviewer_version}</code></span>
      </div>
    </div>
  );
}

function Stat({
  label,
  value,
  hint,
  tone,
}: {
  label: string;
  value: string;
  hint?: string;
  tone?: "good" | "bad" | "warn";
}) {
  const cls =
    tone === "good"
      ? "text-good"
      : tone === "bad"
        ? "text-bad"
        : tone === "warn"
          ? "text-warn"
          : "";
  return (
    <div className="rounded-md border border-[color:var(--border-subtle)] bg-[color:var(--bg-elev2)]/40 px-3 py-2">
      <div className="label">{label}</div>
      <div className={`mt-0.5 text-sm font-semibold ${cls}`}>{value}</div>
      {hint && <div className="text-[10px] text-[color:var(--fg-muted)]">{hint}</div>}
    </div>
  );
}

// ── Summary card ──────────────────────────────────────────────────────────

function SummaryCard({
  summary,
  loading,
}: {
  summary: CompareSummary | null;
  loading: boolean;
}) {
  if (loading) return <Skeleton className="h-24" />;
  if (!summary || summary.n === 0) {
    return (
      <div className="card text-xs text-[color:var(--fg-dim)]">
        No paired reviews yet. The dashboard activates after DeepSeek processes its
        first sample.
      </div>
    );
  }
  return (
    <section className="card grid gap-2">
      <div className="flex items-baseline justify-between">
        <h2 className="label">agreement matrix · n = {summary.n.toLocaleString()}</h2>
        <span
          className={`text-[10px] tabular-nums ${
            summary.mean_overall >= 0.8
              ? "text-good"
              : summary.mean_overall >= 0.6
                ? "text-warn"
                : "text-bad"
          }`}
        >
          overall {fmtPct(summary.mean_overall, 1)}
        </span>
      </div>
      <div className="grid grid-cols-2 gap-2 md:grid-cols-3">
        <Bar label="relevance match" value={summary.relevance_match_rate} pct />
        <Bar label="sentiment match" value={summary.sentiment_match_rate} pct />
        <Bar
          label="score delta (lower = better)"
          value={1 - Math.min(1, summary.mean_score_delta)}
          rawHint={summary.mean_score_delta.toFixed(3)}
        />
        <Bar label="asset Jaccard" value={summary.mean_asset_jaccard} />
        <Bar label="reason Jaccard" value={summary.mean_reason_jaccard} />
        <Bar label="symbol Jaccard" value={summary.mean_symbol_jaccard} />
      </div>
      {summary.deepseek_errors > 0 && (
        <p className="text-[10px] text-warn">
          {summary.deepseek_errors} DeepSeek call{summary.deepseek_errors === 1 ? "" : "s"} returned an error — see the failing rows below.
        </p>
      )}
    </section>
  );
}

function Bar({
  label,
  value,
  pct = false,
  rawHint,
}: {
  label: string;
  value: number;
  pct?: boolean;
  rawHint?: string;
}) {
  const tone =
    value >= 0.8
      ? "bg-good"
      : value >= 0.6
        ? "bg-accent"
        : value >= 0.4
          ? "bg-warn"
          : "bg-bad";
  const display = rawHint ?? (pct ? fmtPct(value, 1) : value.toFixed(2));
  return (
    <div className="rounded-md border border-[color:var(--border-subtle)] bg-[color:var(--bg-elev2)]/40 px-3 py-2">
      <div className="flex items-baseline justify-between gap-2">
        <span className="label">{label}</span>
        <span className="text-[11px] tabular-nums">{display}</span>
      </div>
      <div className="mt-1 h-1 overflow-hidden rounded-full bg-[color:var(--bg-elev2)]">
        <div
          className={`h-full ${tone} transition-[width]`}
          style={{ width: `${Math.min(100, Math.max(0, value * 100))}%` }}
          aria-hidden
        />
      </div>
    </div>
  );
}

// ── Compare row + diff drawer ─────────────────────────────────────────────

function CompareRow({
  it,
  diff,
  selected,
  onSelect,
}: {
  it: CompareItem;
  diff: ReviewDiff;
  selected: boolean;
  onSelect: () => void;
}) {
  const a = it.agreement;
  const tone =
    a.overall >= 0.8 ? "text-good" : a.overall >= 0.6 ? "text-warn" : "text-bad";
  const stubP = it.stub.payload as ReviewPayload;
  const dsP = it.deepseek.payload as ReviewPayload;
  return (
    <li
      className={`px-3 py-2 transition-colors cursor-pointer hover:bg-[color:var(--bg-elev2)] focus:outline-none focus-visible:ring-1 focus-visible:ring-accent ${
        selected ? "bg-[color:var(--bg-elev2)]" : ""
      }`}
      onClick={onSelect}
      tabIndex={0}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          onSelect();
        }
      }}
    >
      <div className="grid grid-cols-[90px_1fr_auto] gap-3 items-start">
        <div className="grid pl-1.5" title={it.deepseek.created_at}>
          <span className="text-[11px] text-[color:var(--fg-dim)]">
            {fmtRel(it.deepseek.created_at)}
          </span>
          <span className="text-[10px] text-[color:var(--fg-muted)] truncate">
            {it.domain ?? ""}
          </span>
        </div>
        <div className="min-w-0">
          <div
            className="text-sm leading-snug truncate flex items-baseline gap-1.5"
            title={it.title ?? it.capture_id}
          >
            {it.title ? (
              <span>{it.title}</span>
            ) : (
              <>
                <span className="font-mono text-[color:var(--fg-dim)] text-[12px]">
                  {it.capture_id.slice(0, 18)}…
                </span>
                {it.capture_id.startsWith("demo-") && (
                  <span className="text-[9px] uppercase tracking-wider text-warn font-semibold">
                    (demo)
                  </span>
                )}
              </>
            )}
          </div>
          {it.deepseek.error_code ? (
            <div className="flex flex-wrap gap-1 mt-1">
              <Pill variant="bad" title={`DeepSeek error: ${it.deepseek.error_code}`}>
                deepseek · {it.deepseek.error_code}
              </Pill>
            </div>
          ) : (
            <ReviewDiffPair stub={stubP} deepseek={dsP} diff={diff} />
          )}
        </div>
        <div className="text-xs flex flex-col items-end gap-0.5 tabular-nums">
          <span className={`text-sm font-semibold ${tone}`}>{fmtPct(a.overall, 0)}</span>
          <ScoreDeltaBadge delta={diff.scoreDelta} />
        </div>
      </div>
    </li>
  );
}

/**
 * Side-by-side diff visual. Stub is on the LEFT, DeepSeek on the RIGHT.
 *
 * Color rules (matches the spec):
 *   • RIGHT (DeepSeek) → green border for `acAdded` / `rcAdded`, muted for kept
 *   • LEFT  (stub)     → red border + line-through for `acRemoved`/`rcRemoved`, muted for kept
 *
 * Sentiment is rendered once below the two columns. A score-delta badge sits in
 * the parent row's right rail (separately rendered) so the diff column stays
 * focused on categorical changes.
 */
function ReviewDiffPair({
  stub,
  deepseek,
  diff,
}: {
  stub: ReviewPayload;
  deepseek: ReviewPayload;
  diff: ReviewDiff;
}) {
  return (
    <div className="mt-1 grid gap-1">
      <div className="grid grid-cols-2 gap-2 text-[10px]">
        {/* stub column */}
        <div className="grid gap-1">
          <div className="text-[9px] uppercase tracking-wider text-[color:var(--fg-muted)]">
            stub
          </div>
          <div className="flex flex-wrap gap-1">
            {diff.acRemoved.map((v) => (
              <Pill key={`s-ac-${v}`} variant="diff-remove" title="only in stub">
                {v}
              </Pill>
            ))}
            {diff.acKept.map((v) => (
              <Pill key={`s-ack-${v}`} variant="diff-kept" title="in both">
                {v}
              </Pill>
            ))}
            {diff.rcRemoved.map((v) => (
              <Pill key={`s-rc-${v}`} variant="diff-remove" title="only in stub">
                {v}
              </Pill>
            ))}
            {diff.rcKept.map((v) => (
              <Pill key={`s-rck-${v}`} variant="diff-kept" title="in both">
                {v}
              </Pill>
            ))}
            {diff.acRemoved.length +
              diff.acKept.length +
              diff.rcRemoved.length +
              diff.rcKept.length ===
              0 && (
              <span className="text-[color:var(--fg-muted)]">—</span>
            )}
          </div>
        </div>
        {/* deepseek column */}
        <div className="grid gap-1">
          <div className="text-[9px] uppercase tracking-wider text-[color:var(--fg-muted)]">
            DeepSeek
          </div>
          <div className="flex flex-wrap gap-1">
            {diff.acAdded.map((v) => (
              <Pill
                key={`d-ac-${v}`}
                variant="diff-add"
                title="added by DeepSeek"
              >
                {v}
              </Pill>
            ))}
            {diff.acKept.map((v) => (
              <Pill key={`d-ack-${v}`} variant="diff-kept" title="in both">
                {v}
              </Pill>
            ))}
            {diff.rcAdded.map((v) => (
              <Pill
                key={`d-rc-${v}`}
                variant="diff-add"
                title="added by DeepSeek"
              >
                {v}
              </Pill>
            ))}
            {diff.rcKept.map((v) => (
              <Pill key={`d-rck-${v}`} variant="diff-kept" title="in both">
                {v}
              </Pill>
            ))}
            {diff.acAdded.length +
              diff.acKept.length +
              diff.rcAdded.length +
              diff.rcKept.length ===
              0 && (
              <span className="text-[color:var(--fg-muted)]">—</span>
            )}
          </div>
        </div>
      </div>
      <SentimentDiffLine
        stub={stub.sentiment_label ?? null}
        deepseek={deepseek.sentiment_label ?? null}
        changed={diff.sentimentChanged}
      />
    </div>
  );
}

function ScoreDeltaBadge({ delta }: { delta: number }) {
  const abs = Math.abs(delta);
  if (abs < 0.005) {
    return (
      <span className="text-[10px] text-[color:var(--fg-dim)] tabular-nums">
        · 0.00
      </span>
    );
  }
  const tone = delta > 0 ? "text-good" : "text-bad";
  const arrow = delta > 0 ? "↑" : "↓";
  const sign = delta >= 0 ? "+" : "";
  return (
    <span
      className={`text-[10px] tabular-nums ${tone}`}
      title="DeepSeek score minus stub score"
    >
      {arrow} {sign}
      {delta.toFixed(2)}
    </span>
  );
}

function SentimentDiffLine({
  stub,
  deepseek,
  changed,
}: {
  stub: string | null;
  deepseek: string | null;
  changed: boolean;
}) {
  const fmt = (s: string | null) =>
    s ?? <span className="text-[color:var(--fg-muted)]">∅</span>;
  if (!changed) {
    return (
      <div className="text-[10px] text-[color:var(--fg-muted)]">
        sentiment {fmt(stub)}
      </div>
    );
  }
  return (
    <div className="text-[10px]">
      <span className="text-[color:var(--fg-muted)]">sentiment </span>
      <span className="text-bad line-through">{fmt(stub)}</span>{" "}
      <span className="text-[color:var(--fg-muted)]">→</span>{" "}
      <span className="text-good font-semibold">{fmt(deepseek)}</span>
    </div>
  );
}

function DiffDrawer({ item, onClose }: { item: CompareItem; onClose: () => void }) {
  const qc = useQueryClient();
  const rerun = useMutation({
    mutationFn: () => api.reviewsRun(item.capture_id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["reviews-compare"] });
      qc.invalidateQueries({ queryKey: ["reviews-status"] });
    },
  });
  const href = safeHref(item.url ?? undefined);
  const diff = useMemo(() => computeReviewDiff(item), [item]);
  return (
    <section className="card grid gap-3" aria-label="Review diff drawer">
      <header className="grid gap-1">
        <div className="flex items-baseline justify-between gap-2">
          <h2 className="text-sm font-semibold">diff · {item.capture_id.slice(0, 12)}…</h2>
          <button type="button" className="btn text-[10px] py-0.5 px-2 focus:outline-none focus-visible:ring-1 focus-visible:ring-accent" onClick={onClose}>
            close
          </button>
        </div>
        {item.title && (
          <p className="text-xs leading-snug">
            {href ? (
              <a href={href} target="_blank" rel="noopener noreferrer" className="text-accent hover:underline focus:outline-none focus-visible:ring-1 focus-visible:ring-accent rounded-sm">
                {item.title}
              </a>
            ) : (
              item.title
            )}
          </p>
        )}
        <p className="text-[10px] text-[color:var(--fg-muted)]">
          {item.domain ?? "(unknown domain)"} · {fmtDate(item.deepseek.created_at)}
        </p>
      </header>
      <ReviewSidePanel title="stub (in-process)" side={item.stub} diff={diff} role="stub" />
      <ReviewSidePanel title="DeepSeek" side={item.deepseek} diff={diff} role="deepseek" />
      <div className="text-[11px] flex items-center justify-between gap-2 px-1">
        <span className="text-[color:var(--fg-muted)]">score delta</span>
        <ScoreDeltaBadge delta={diff.scoreDelta} />
      </div>
      <AgreementInline agreement={item.agreement} />
      <div className="flex items-center gap-2 pt-2 border-t border-[color:var(--border-subtle)]">
        <button
          type="button"
          className="btn btn-accent text-xs focus:outline-none focus-visible:ring-1 focus-visible:ring-accent"
          onClick={() => rerun.mutate()}
          disabled={rerun.isPending}
        >
          {rerun.isPending ? "re-running…" : "re-run DeepSeek"}
        </button>
        {rerun.isError && (
          <span className="text-[10px] text-bad">
            {rerun.error instanceof Error ? rerun.error.message : "failed"}
          </span>
        )}
      </div>
    </section>
  );
}

function ReviewSidePanel({
  title,
  side,
  diff,
  role,
}: {
  title: string;
  side: ReviewSide;
  diff: ReviewDiff;
  role: "stub" | "deepseek";
}) {
  const p = side.payload as ReviewPayload;
  // Diff-aware variant per pill: stub-side strikes removals, deepseek-side
  // greens up additions, both sides mute intersection. Falls back to the
  // pre-diff palette (ac/rc/sym) when the diff isn't meaningful, e.g. when
  // either side errored. Symbols still use the legacy `sym` variant — symbol
  // diff has its own Jaccard line and adding it here would be visual noise.
  const acAdded = new Set(diff.acAdded);
  const acRemoved = new Set(diff.acRemoved);
  const rcAdded = new Set(diff.rcAdded);
  const rcRemoved = new Set(diff.rcRemoved);
  const acVariant = (v: string): "diff-add" | "diff-remove" | "diff-kept" | "ac" => {
    if (role === "deepseek" && acAdded.has(v)) return "diff-add";
    if (role === "stub" && acRemoved.has(v)) return "diff-remove";
    if (!acAdded.has(v) && !acRemoved.has(v)) return "diff-kept";
    return "ac";
  };
  const rcVariant = (v: string): "diff-add" | "diff-remove" | "diff-kept" | "rc" => {
    if (role === "deepseek" && rcAdded.has(v)) return "diff-add";
    if (role === "stub" && rcRemoved.has(v)) return "diff-remove";
    if (!rcAdded.has(v) && !rcRemoved.has(v)) return "diff-kept";
    return "rc";
  };
  return (
    <section className="rounded-md border border-[color:var(--border-subtle)] bg-[color:var(--bg-elev2)]/40 p-2">
      <div className="flex items-baseline justify-between gap-2">
        <h3 className="text-[10px] font-semibold uppercase tracking-wider text-[color:var(--fg-dim)]">
          {title}
        </h3>
        <span className="text-[10px] text-[color:var(--fg-muted)]" title={side.reviewer_version}>
          v{side.reviewer_version.slice(0, 24)}
        </span>
      </div>
      {side.error_code ? (
        <div className="mt-2 text-[11px] text-bad">
          <code className="font-mono">{side.error_code}</code>
          {p.reason_text && <span className="ml-1 text-[color:var(--fg-dim)]">— {p.reason_text}</span>}
        </div>
      ) : (
        <>
          <div className="mt-2 flex items-baseline gap-2 text-xs">
            <span className={p.is_finance_relevant ? "text-good font-semibold" : "text-bad"}>
              {p.is_finance_relevant ? "YES" : "no"} finance
            </span>
            <span className={`tabular-nums ${scoreToneClass(p.finance_relevance_score)}`}>
              score {fmtScore(p.finance_relevance_score)}
            </span>
            {p.sentiment_label && (
              <span
                className={
                  diff.sentimentChanged
                    ? role === "stub"
                      ? "text-bad line-through"
                      : "text-good font-semibold"
                    : p.sentiment_label === "positive"
                      ? "text-good"
                      : p.sentiment_label === "negative"
                        ? "text-bad"
                        : "text-[color:var(--fg-dim)]"
                }
              >
                {p.sentiment_label}
              </span>
            )}
          </div>
          <div className="mt-1 flex flex-wrap gap-1">
            {p.asset_classes.map((a) => (
              <Pill key={`a-${a}`} variant={acVariant(a)}>
                {a}
              </Pill>
            ))}
            {p.impact_reason_codes.map((r) => (
              <Pill key={`r-${r}`} variant={rcVariant(r)}>
                {r}
              </Pill>
            ))}
            {p.candidate_symbols.map((s) => (
              <Pill key={`s-${s}`} variant="sym">{s}</Pill>
            ))}
          </div>
          {p.reason_text && (
            <p className="mt-2 text-[11px] italic text-[color:var(--fg-dim)]">
              "{p.reason_text}"
            </p>
          )}
        </>
      )}
      {(side.usd_cost ?? 0) > 0 && (
        <p className="mt-2 text-[10px] text-[color:var(--fg-muted)] tabular-nums">
          {side.input_tokens?.toLocaleString() ?? 0} in / {side.output_tokens?.toLocaleString() ?? 0} out · ${(side.usd_cost ?? 0).toFixed(5)} · {side.latency_ms ?? 0} ms
        </p>
      )}
    </section>
  );
}

function AgreementInline({ agreement }: { agreement: Agreement }) {
  return (
    <dl className="grid grid-cols-2 gap-x-3 gap-y-1 text-[11px]">
      <dt className="text-[color:var(--fg-muted)]">relevance</dt>
      <dd className={agreement.relevance_match ? "text-good" : "text-bad"}>
        {agreement.relevance_match ? "match" : "diff"}
      </dd>
      <dt className="text-[color:var(--fg-muted)]">sentiment</dt>
      <dd className={agreement.sentiment_match ? "text-good" : "text-warn"}>
        {agreement.sentiment_match ? "match" : "diff"}
      </dd>
      <dt className="text-[color:var(--fg-muted)]">score delta</dt>
      <dd className="font-mono">{agreement.score_delta.toFixed(2)}</dd>
      <dt className="text-[color:var(--fg-muted)]">asset Jaccard</dt>
      <dd className="font-mono">{agreement.asset_jaccard.toFixed(2)}</dd>
      <dt className="text-[color:var(--fg-muted)]">reason Jaccard</dt>
      <dd className="font-mono">{agreement.reason_jaccard.toFixed(2)}</dd>
      <dt className="text-[color:var(--fg-muted)]">symbol Jaccard</dt>
      <dd className="font-mono">{agreement.symbol_jaccard.toFixed(2)}</dd>
    </dl>
  );
}

function HelpRail() {
  return (
    <aside className="card text-xs grid gap-2">
      <h3 className="label">how to read this page</h3>
      <ol className="grid gap-1.5 list-decimal pl-4 text-[color:var(--fg-dim)] text-[11px]">
        <li>
          Each row pairs the in-process stub review against DeepSeek's review of the
          same article — only captures that DeepSeek actually scored show up.
        </li>
        <li>
          The percent number on the right is an equal-weight aggregate of five
          signals (relevance match, score delta, asset Jaccard, reason Jaccard,
          sentiment match). Lower = bigger disagreement.
        </li>
        <li>
          Click a row to open the diff drawer; click again to close.
        </li>
        <li>
          "biggest gap" sort puts the most-disagreed captures on top — these are
          the ones worth a human read.
        </li>
      </ol>
      <p className="pt-2 text-[10px] text-[color:var(--fg-muted)] border-t border-[color:var(--border-subtle)]">
        DeepSeek runs only when enabled in Settings AND the deterministic
        sampling bucket fires for the capture. Toggle off to restore catchem to
        fully offline behavior.
      </p>
    </aside>
  );
}
