import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, fmtRel } from "@/lib/api";
import { Skeleton, ErrorBox, EmptyState } from "@/components/Skeleton";

/**
 * Per-feed news source health at /sources.
 *
 * The Live Feed surface tells the analyst "what's flowing"; this page
 * tells them "what isn't" — which of the 50+ configured RSS sources are
 * silently failing, which are nominal, and how the success rate has
 * trended since the sidecar booted.
 *
 * Refetch cadence: 15s (slower than /logs at 3s — feed health changes on
 * the poller's own 10s+ tick, so anything tighter is wasted bandwidth).
 *
 * Reads `/api/news/sources`; the endpoint always returns 200, even when
 * the poller is disabled, so the page can render a clean empty state
 * without a separate failure path.
 */

// ── URL helpers ─────────────────────────────────────────────────────────────

/**
 * Extract the public-facing host portion of a URL for the table's domain
 * cell. Strips `www.` and the path so the operator sees "bbc.co.uk"
 * instead of `https://feeds.bbci.co.uk/news/...`. Falls back to the raw
 * URL on parse failure rather than throwing.
 *
 * Exported so the test file can pin behaviour without mounting the page.
 */
export function extractDomain(rawUrl: string | null | undefined): string {
  if (!rawUrl) return "—";
  try {
    const u = new URL(rawUrl);
    const host = u.host.toLowerCase();
    return host.startsWith("www.") ? host.slice(4) : host;
  } catch {
    return rawUrl;
  }
}

/**
 * Format `success_rate` ∈ [0, 1] to a percentage with at most one decimal.
 * Returns "—" for negative / non-finite values rather than NaN%. Pure +
 * pinned in tests so the KPI tile copy can't silently drift.
 */
export function formatSuccessRate(rate: number): string {
  if (!Number.isFinite(rate) || rate < 0) return "—";
  // Whole-percent breaks (100% / 0%) read cleaner without the trailing
  // ".0" so we strip it manually instead of relying on toFixed slicing.
  const pct = rate * 100;
  if (Math.abs(pct - Math.round(pct)) < 0.05) {
    return `${Math.round(pct)}%`;
  }
  return `${pct.toFixed(1)}%`;
}

type StatusBadgeProps = { status: "ok" | "error" | "unknown" | "backed_off" };

function StatusBadge({ status }: StatusBadgeProps) {
  if (status === "ok") {
    return (
      <span
        data-testid="sources-status-ok"
        className="inline-flex items-center gap-1 rounded-full bg-good/15 px-2 py-0.5 text-[10px] uppercase tracking-wider text-good"
      >
        <span aria-hidden className="inline-block h-1.5 w-1.5 rounded-full bg-good" />
        healthy
      </span>
    );
  }
  if (status === "error") {
    return (
      <span
        data-testid="sources-status-error"
        className="inline-flex items-center gap-1 rounded-full bg-bad/15 px-2 py-0.5 text-[10px] uppercase tracking-wider text-bad"
      >
        <span aria-hidden className="inline-block h-1.5 w-1.5 rounded-full bg-bad" />
        degraded
      </span>
    );
  }
  if (status === "backed_off") {
    return (
      <span
        data-testid="sources-status-backed-off"
        className="inline-flex items-center gap-1 rounded-full bg-warn/15 px-2 py-0.5 text-[10px] uppercase tracking-wider text-warn"
        title="Circuit breaker open — paused after 5+ consecutive failures"
      >
        <span aria-hidden className="inline-block h-1.5 w-1.5 rounded-full bg-warn" />
        backed off
      </span>
    );
  }
  return (
    <span
      data-testid="sources-status-unknown"
      className="inline-flex items-center gap-1 rounded-full bg-[color:var(--bg-elev2)] px-2 py-0.5 text-[10px] uppercase tracking-wider text-[color:var(--fg-muted)]"
    >
      <span aria-hidden className="inline-block h-1.5 w-1.5 rounded-full bg-[color:var(--fg-dim)]" />
      idle
    </span>
  );
}

/**
 * Format a cooldown_until ISO timestamp as a human-friendly "in 4m 30s"
 * remaining-time string. Returns "now" when the cooldown has expired but
 * the next tick hasn't fired yet, "—" when no cooldown is active.
 *
 * Exported so the test file can pin behaviour without mounting the page.
 */
export function formatCooldownRemaining(
  cooldownUntilIso: string | null | undefined,
  now: Date = new Date(),
): string {
  if (!cooldownUntilIso) return "—";
  const target = new Date(cooldownUntilIso);
  if (Number.isNaN(target.getTime())) return "—";
  const deltaMs = target.getTime() - now.getTime();
  if (deltaMs <= 0) return "now";
  const totalSec = Math.ceil(deltaMs / 1000);
  if (totalSec < 60) return `${totalSec}s`;
  const m = Math.floor(totalSec / 60);
  const s = totalSec % 60;
  if (m < 60) return s === 0 ? `${m}m` : `${m}m ${s}s`;
  const h = Math.floor(m / 60);
  const rm = m % 60;
  return rm === 0 ? `${h}h` : `${h}h ${rm}m`;
}

/**
 * Format a duration in seconds as a compact "Xm Ys" / "Xs" / "Xh Ym"
 * string for the awareness-window panel. Returns "—" for null / negative /
 * non-finite so the cell never renders "NaNs". Pure + pinned in tests.
 *
 * Exported so the test file can pin behaviour without mounting the page.
 */
export function formatWindowSeconds(secs: number | null | undefined): string {
  if (secs == null || !Number.isFinite(secs) || secs < 0) return "—";
  const total = Math.round(secs);
  if (total < 60) return `${total}s`;
  const m = Math.floor(total / 60);
  const s = total % 60;
  if (m < 60) return s === 0 ? `${m}m` : `${m}m ${s}s`;
  const h = Math.floor(m / 60);
  const rm = m % 60;
  return rm === 0 ? `${h}h` : `${h}h ${rm}m`;
}

/**
 * Small live "awareness window" panel — answers "how fresh / how broad is
 * awareness right now?". Reads /api/news/awareness (always 200; degraded
 * envelope when the poller is off). Card styling mirrors the per-feed
 * table card below it.
 */
function AwarenessWindowPanel() {
  const awareness = useQuery({
    queryKey: ["news-awareness"],
    queryFn: () => api.newsAwareness(),
    refetchInterval: 15_000,
  });

  const data = awareness.data;
  if (awareness.isLoading) return <Skeleton className="h-28" />;
  if (!data) return null;

  const parserEntries = Object.entries(data.sources_by_parser).sort(
    (a, b) => b[1] - a[1] || a[0].localeCompare(b[0]),
  );
  const windowLabel =
    data.window_estimate_seconds == null
      ? "—"
      : `~${formatWindowSeconds(data.window_estimate_seconds)} to now`;

  return (
    <section className="card" data-testid="awareness-window">
      <div className="flex items-center justify-between mb-2">
        <h2 className="label">
          awareness window{" "}
          <span className="text-[color:var(--fg-muted)] font-normal">
            how fresh · how broad
          </span>
        </h2>
        {data.last_new_at && (
          <span className="text-[10px] text-[color:var(--fg-dim)] tabular-nums">
            last new arrival {fmtRel(data.last_new_at)}
          </span>
        )}
      </div>
      {!data.configured ? (
        <EmptyState
          title="news poller disabled"
          hint="Enable the RSS poller (CATCHEM_NEWS__POLLER_ENABLED=true) to see the live awareness window."
        />
      ) : (
        <div className="grid gap-2 grid-cols-1 sm:grid-cols-2 md:grid-cols-4 text-[11px]">
          <SourcesStat
            label="total sources"
            value={data.sources_total.toLocaleString()}
            hint="configured feeds"
          />
          <SourcesStat
            label="effective window"
            value={windowLabel}
            hint="poll interval + publisher lag"
            tone="good"
          />
          <SourcesStat
            label="median publisher lag"
            value={formatWindowSeconds(data.median_publisher_lag_seconds)}
            hint={
              data.poll_interval_seconds != null
                ? `polls every ${formatWindowSeconds(data.poll_interval_seconds)}`
                : "publisher-side delay"
            }
          />
          <SourcesStat
            label="by parser"
            value={
              parserEntries.length === 0
                ? "—"
                : parserEntries.map(([p, n]) => `${p}:${n}`).join(" · ")
            }
            hint={`${data.total_ingested.toLocaleString()} items ingested`}
          />
        </div>
      )}
    </section>
  );
}

/**
 * Blind-spots panel — answers "what am I NOT seeing?". Reads
 * /api/news/coverage-gaps (always 200; empty gaps + covered on a fresh
 * boot / disabled poller). Gaps — watched terms with NO recent coverage —
 * are shown prominently as warning chips; the freshest-seen "covered"
 * terms follow in a compact list (term → freshest mention age). When every
 * watched term has coverage we surface the benign "all covered" empty
 * state. Card styling mirrors the awareness-window panel above it.
 *
 * Poll cadence: 30s — gaps shift on the poller's own multi-second tick, so
 * anything tighter is wasted bandwidth.
 */
function BlindSpotsPanel() {
  const coverage = useQuery({
    queryKey: ["news-coverage-gaps"],
    queryFn: () => api.newsCoverageGaps(),
    refetchInterval: 30_000,
  });

  const data = coverage.data;
  if (coverage.isLoading) return <Skeleton className="h-28" />;
  if (!data) return null;

  const gaps = data.gaps ?? [];
  const covered = [...(data.covered ?? [])].sort(
    (a, b) => a.last_seen_age_seconds - b.last_seen_age_seconds,
  );
  const gapCount = gaps.length;
  const coveredCount = covered.length;
  const totalWatched = gapCount + coveredCount;
  const windowLabel = formatWindowSeconds(data.window_seconds);

  return (
    <section className="card" data-testid="blind-spots">
      <div className="flex items-center justify-between mb-2">
        <h2 className="label">
          blind spots{" "}
          <span className="text-[color:var(--fg-muted)] font-normal">
            what am I not seeing?
          </span>
        </h2>
        <span className="text-[10px] text-[color:var(--fg-dim)] tabular-nums">
          window ~{windowLabel}
        </span>
      </div>

      {totalWatched === 0 ? (
        <EmptyState
          title="no watched terms"
          hint="The blind-spot detector has no terms to track yet. Configure a watch list (or enable the poller) so coverage gaps can be surfaced here."
        />
      ) : (
        <>
          <div className="grid gap-2 grid-cols-1 sm:grid-cols-3 text-[11px] mb-3">
            <SourcesStat
              label="watched terms"
              value={totalWatched.toLocaleString()}
              hint="total tracked"
            />
            <SourcesStat
              label="blind spots"
              value={gapCount.toLocaleString()}
              hint={gapCount === 0 ? "all covered" : "no recent coverage"}
              tone={gapCount > 0 ? "warn" : "good"}
            />
            <SourcesStat
              label="covered"
              value={coveredCount.toLocaleString()}
              hint="seen in window"
              tone={coveredCount > 0 ? "good" : undefined}
            />
          </div>

          {gapCount === 0 ? (
            <div data-testid="blind-spots-all-covered">
              <EmptyState
                title="no blind spots"
                hint={`Every watched term has been seen in the last ~${windowLabel}. Awareness coverage is complete for now.`}
              />
            </div>
          ) : (
            <div className="mb-3" data-testid="blind-spots-gaps">
              <div className="text-[9px] uppercase tracking-wider text-[color:var(--fg-muted)] mb-1.5">
                no recent coverage
              </div>
              <div className="flex flex-wrap gap-1.5">
                {gaps.map((term) => (
                  <span
                    key={term}
                    data-testid={`blind-spot-gap-${term}`}
                    className="inline-flex items-center gap-1 rounded-full bg-warn/15 px-2 py-0.5 text-[11px] text-warn"
                    title={`No mentions of "${term}" in the last ~${windowLabel}`}
                  >
                    <span aria-hidden className="inline-block h-1.5 w-1.5 rounded-full bg-warn" />
                    {term}
                  </span>
                ))}
              </div>
            </div>
          )}

          {coveredCount > 0 && (
            <div data-testid="blind-spots-covered">
              <div className="text-[9px] uppercase tracking-wider text-[color:var(--fg-muted)] mb-1.5">
                covered · freshest first
              </div>
              <ul className="grid gap-1 grid-cols-1 sm:grid-cols-2">
                {covered.map((c) => (
                  <li
                    key={c.term}
                    data-testid={`blind-spot-covered-${c.term}`}
                    className="flex items-center justify-between gap-2 rounded-md border border-[color:var(--border-subtle)] bg-[color:var(--bg-elev2)]/40 px-2.5 py-1.5"
                  >
                    <span className="flex items-center gap-1.5 min-w-0">
                      <span aria-hidden className="inline-block h-1.5 w-1.5 rounded-full bg-good shrink-0" />
                      <span className="truncate text-[color:var(--fg)]">{c.term}</span>
                      {c.mention_count > 0 && (
                        <span className="text-[10px] text-[color:var(--fg-dim)] tabular-nums shrink-0">
                          ×{c.mention_count.toLocaleString()}
                        </span>
                      )}
                    </span>
                    <span className="text-[10px] text-[color:var(--fg-dim)] tabular-nums whitespace-nowrap">
                      {formatWindowSeconds(c.last_seen_age_seconds)} ago
                    </span>
                  </li>
                ))}
              </ul>
            </div>
          )}
        </>
      )}
    </section>
  );
}

function SourcesStat({
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
      <div className="text-[9px] uppercase tracking-wider text-[color:var(--fg-muted)]">
        {label}
      </div>
      <div className={`mt-0.5 text-sm font-semibold tabular-nums ${cls}`}>{value}</div>
      {hint && (
        <div className="text-[10px] text-[color:var(--fg-dim)] truncate">{hint}</div>
      )}
    </div>
  );
}

export function SourcesPage() {
  const qc = useQueryClient();
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set());

  // Re-render every 30s so the "last fetch" relative-time string stays
  // honest even when the query itself returns identical data.
  const [, setTick] = useState(0);
  useEffect(() => {
    const id = window.setInterval(() => setTick((n) => n + 1), 30_000);
    return () => window.clearInterval(id);
  }, []);

  // Per-feed probe state. Keyed by feed.url so concurrent rows can be
  // tracked independently and a click on row A doesn't lock row B.
  // `inFlight`     — set of URLs currently awaiting a probe response
  //                  (used to disable the button + show a spinner glyph)
  // `errors`       — last-known per-URL error message; cleared on success
  const [inFlight, setInFlight] = useState<Set<string>>(() => new Set());
  const [errors, setErrors] = useState<Record<string, string>>({});

  const handleProbe = async (url: string) => {
    if (!url || inFlight.has(url)) return;
    setInFlight((prev) => new Set(prev).add(url));
    // Optimistically clear any prior error for this row so the spinner
    // isn't competing with an out-of-date message.
    setErrors((prev) => {
      const { [url]: _drop, ...rest } = prev;
      void _drop;
      return rest;
    });
    try {
      const res = await api.probeSource(url);
      if (res.ok) {
        // Force a refetch of /api/news/sources so the row reflects the
        // post-probe state (status, polls, items, last_error).
        await qc.invalidateQueries({ queryKey: ["news-sources"] });
      } else {
        setErrors((prev) => ({ ...prev, [url]: res.error || "probe failed" }));
      }
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      setErrors((prev) => ({ ...prev, [url]: msg }));
    } finally {
      setInFlight((prev) => {
        const next = new Set(prev);
        next.delete(url);
        return next;
      });
    }
  };

  const sources = useQuery({
    queryKey: ["news-sources"],
    queryFn: () => api.newsSources(),
    refetchInterval: 15_000,
  });

  // Sorted feed list. Hoisted ABOVE the early returns below so the hook
  // count stays constant across the loading→loaded transition (React's
  // Rules of Hooks — a useMemo after a conditional `return` throws
  // "Rendered more hooks than during the previous render" the moment the
  // query resolves). Reads optional-chained source rows with an empty
  // fallback so it's safe before data lands.
  const sorted = useMemo(() => {
    // Sort priority: hard errors (still being probed) first, then
    // circuit-broken / backed-off feeds, then healthy, then idle. Within
    // each bucket sort by (1 - success_rate) to surface the worst
    // performer; alphabetic on URL for tiebreak stability.
    const arr = [...(sources.data?.sources ?? [])];
    const rank = (s: string): number => {
      if (s === "error") return 0;
      if (s === "backed_off") return 1;
      if (s === "ok") return 2;
      return 3; // unknown / idle
    };
    arr.sort((a, b) => {
      const ra = rank(a.last_status);
      const rb = rank(b.last_status);
      if (ra !== rb) return ra - rb;
      if (a.success_rate !== b.success_rate) return a.success_rate - b.success_rate;
      return (a.url || "").localeCompare(b.url || "");
    });
    return arr;
  }, [sources.data?.sources]);

  if (sources.isLoading) return <Skeleton className="h-72" />;
  if (sources.error) return <ErrorBox err={sources.error} />;
  const data = sources.data;
  if (!data) return null;

  const total = data.total;
  const healthy = data.healthy_count;
  const degraded = data.degraded_count;
  const backedOff =
    data.backed_off_count ??
    data.sources.reduce((acc, s) => acc + (s.last_status === "backed_off" ? 1 : 0), 0);
  const totalItems = data.total_items ?? data.sources.reduce((acc, s) => acc + s.items_total, 0);
  const totalPolls = data.sources.reduce((acc, s) => acc + s.polls, 0);
  const totalFailures = data.sources.reduce((acc, s) => acc + s.failures, 0);
  // Aggregate success rate = totalSuccesses / totalPolls. When nothing
  // has been polled yet (totalPolls = 0) we surface "—" rather than 0%
  // so the analyst can't mis-read a fresh boot as a fully-failing
  // poller.
  const aggregateRate = totalPolls > 0 ? (totalPolls - totalFailures) / totalPolls : null;

  // Hero tone: degraded sources dominate any "all clear" framing.
  const heroTone: "good" | "warn" | "bad" =
    degraded > 0 ? (degraded >= Math.max(3, Math.ceil(total * 0.2)) ? "bad" : "warn") : "good";
  const heroAccent =
    heroTone === "good"
      ? "border-good/40 from-good/15"
      : heroTone === "warn"
        ? "border-warn/40 from-warn/15"
        : "border-bad/40 from-bad/15";
  const dotAccent =
    heroTone === "good" ? "bg-good" : heroTone === "warn" ? "bg-warn" : "bg-bad";
  const eyebrowAccent =
    heroTone === "good" ? "text-good" : heroTone === "warn" ? "text-warn" : "text-bad";

  const toggleExpand = (name: string) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(name)) next.delete(name);
      else next.add(name);
      return next;
    });
  };

  return (
    <div className="grid gap-5" data-testid="sources-page">
      {/* Premium hero — mirrors the LogsPage / OpsPage structure. */}
      <section
        className={`relative overflow-hidden rounded-xl border ${heroAccent} bg-gradient-to-br via-[color:var(--bg-elev)] to-[color:var(--bg-elev)] p-6`}
      >
        <div
          aria-hidden
          className={`pointer-events-none absolute -top-20 -left-20 h-48 w-48 rounded-full ${
            heroTone === "good"
              ? "bg-good/20"
              : heroTone === "warn"
                ? "bg-warn/20"
                : "bg-bad/20"
          } blur-3xl`}
        />
        <div className="relative flex flex-wrap items-start justify-between gap-3 mb-3">
          <div className="flex items-center gap-3">
            <span className="relative flex h-2 w-2">
              <span
                className={`absolute inline-flex h-full w-full animate-ping rounded-full ${dotAccent} opacity-75`}
              />
              <span
                className={`relative inline-flex h-2 w-2 rounded-full ${dotAccent}`}
              />
            </span>
            <div>
              <div
                className={`text-[10px] uppercase tracking-[0.25em] ${eyebrowAccent} font-semibold`}
              >
                NEWS SOURCES · per-feed health
              </div>
              <h1
                data-testid="sources-headline"
                className="text-lg font-semibold mt-0.5 tracking-tight"
              >
                {data.configured
                  ? `${healthy}/${total} sources healthy`
                  : "News poller not configured"}
              </h1>
              <div className="mt-1 text-[11px] text-[color:var(--fg-muted)]">
                {data.configured ? (
                  <>
                    {degraded > 0
                      ? `${degraded} degraded · ${totalPolls.toLocaleString()} polls · ${totalItems.toLocaleString()} items ingested`
                      : `${totalPolls.toLocaleString()} polls · ${totalItems.toLocaleString()} items ingested`}
                    {data.last_run_at && (
                      <>
                        {" · last tick "}
                        <span className="tabular-nums">{fmtRel(data.last_run_at)}</span>
                      </>
                    )}
                  </>
                ) : (
                  "Enable the RSS poller (CATCHEM_NEWS__POLLER_ENABLED=true) to populate this page."
                )}
              </div>
            </div>
          </div>
          <div className="flex shrink-0 items-center gap-2">
            <Link to="/feed" className="btn" data-testid="sources-back-to-feed">
              View Live Feed
            </Link>
            <button
              type="button"
              className="btn"
              data-testid="sources-refresh"
              onClick={() => qc.invalidateQueries({ queryKey: ["news-sources"] })}
            >
              refresh
            </button>
          </div>
        </div>
        <div className="relative grid gap-2 grid-cols-1 sm:grid-cols-2 md:grid-cols-4 text-[11px]">
          <SourcesStat
            label="total"
            value={total.toLocaleString()}
            hint="configured feeds"
          />
          <SourcesStat
            label="healthy"
            value={healthy.toLocaleString()}
            hint={healthy === total ? "all green" : "last poll ok"}
            tone={healthy === total && total > 0 ? "good" : undefined}
          />
          <SourcesStat
            label="degraded"
            value={degraded.toLocaleString()}
            hint={degraded > 0 ? "last poll failed" : "no errors"}
            tone={degraded > 0 ? "bad" : "good"}
          />
          <SourcesStat
            label="avg success"
            value={aggregateRate === null ? "—" : formatSuccessRate(aggregateRate)}
            hint={
              aggregateRate === null
                ? "no polls yet"
                : aggregateRate >= 0.95
                  ? "nominal"
                  : aggregateRate >= 0.8
                    ? "review"
                    : "investigate"
            }
            tone={
              aggregateRate === null
                ? undefined
                : aggregateRate >= 0.95
                  ? "good"
                  : aggregateRate >= 0.8
                    ? "warn"
                    : "bad"
            }
          />
        </div>
      </section>

      {/* Live awareness window — how fresh + how broad, right now. */}
      <AwarenessWindowPanel />

      {/* Blind spots — what am I NOT seeing right now? */}
      <BlindSpotsPanel />

      {/* Per-feed table. */}
      <section className="card">
        <div className="flex items-center justify-between mb-2">
          <h2 className="label">
            per-feed health{" "}
            <span className="text-[color:var(--fg-muted)] font-normal">
              ({sorted.length} {sorted.length === 1 ? "feed" : "feeds"})
            </span>
          </h2>
          <span className="text-[10px] text-[color:var(--fg-dim)]">
            sorted: degraded first · then worst success rate
          </span>
        </div>
        {!data.configured ? (
          <EmptyState
            title="news poller disabled"
            hint="Set CATCHEM_NEWS__POLLER_ENABLED=true in your env to enable the background RSS poller. 50+ default sources are bundled and ingest immediately on boot."
            action={
              <Link to="/feed" className="btn" data-testid="sources-empty-cta">
                Open Live Feed
              </Link>
            }
          />
        ) : sorted.length === 0 ? (
          <EmptyState
            title="no feeds configured"
            hint="The poller is enabled but its feed list is empty. Check CATCHEM_NEWS__FEEDS if you've overridden the defaults."
            action={
              <Link to="/feed" className="btn">
                Open Live Feed
              </Link>
            }
          />
        ) : (
          <div className="overflow-x-auto">
            <table
              data-testid="sources-table"
              className="w-full text-xs"
            >
              <thead>
                <tr className="text-left text-[10px] uppercase tracking-wider text-[color:var(--fg-muted)] border-b border-[color:var(--border-subtle)]">
                  <th className="py-2 pr-3">Domain</th>
                  <th className="py-2 pr-3">Status</th>
                  <th className="py-2 pr-3 text-right">Polls</th>
                  <th className="py-2 pr-3 text-right">Success rate</th>
                  <th className="py-2 pr-3 text-right">Items</th>
                  <th className="py-2 pr-3">Last fetch</th>
                  <th className="py-2 pr-3">Last error</th>
                  <th className="py-2 text-right">Probe</th>
                </tr>
              </thead>
              <tbody>
                {sorted.map((s) => {
                  const isExpanded = expanded.has(s.name);
                  const hasError = !!s.last_error;
                  return (
                    <tr
                      key={s.name}
                      data-testid={`sources-row-${s.name}`}
                      data-status={s.last_status}
                      className="border-b border-[color:var(--border-subtle)]/40 hover:bg-[color:var(--bg-elev2)]/40"
                    >
                      <td className="py-2 pr-3">
                        <div className="font-medium text-[color:var(--fg)]">
                          {extractDomain(s.url)}
                        </div>
                        <div className="text-[10px] text-[color:var(--fg-dim)] truncate max-w-[260px]">
                          {s.name}
                        </div>
                      </td>
                      <td className="py-2 pr-3">
                        <StatusBadge status={s.last_status} />
                        {s.consecutive_errors > 1 && (
                          <div className="mt-0.5 text-[10px] text-bad tabular-nums">
                            {s.consecutive_errors}× in a row
                          </div>
                        )}
                        {s.cooldown_until && (
                          <div
                            data-testid={`sources-cooldown-${s.name}`}
                            className="mt-0.5 text-[10px] text-warn tabular-nums"
                            title={`Next probe at ${s.cooldown_until}`}
                          >
                            probe in {formatCooldownRemaining(s.cooldown_until)}
                          </div>
                        )}
                        {(s.adaptive_cadence ?? 1) > 1 && (
                          <div
                            data-testid={`sources-cadence-${s.name}`}
                            className="mt-0.5 inline-flex items-center gap-1 rounded-full bg-[color:var(--bg-elev2)] px-1.5 py-0.5 text-[9px] uppercase tracking-wider text-[color:var(--fg-muted)] tabular-nums"
                            title={`Adaptive cadence — this feed keeps coming back empty (${s.consecutive_empty ?? 0} in a row), so the poller stretched its interval to 1-in-${s.adaptive_cadence} cycles to save bandwidth.`}
                          >
                            every {s.adaptive_cadence} cycles
                          </div>
                        )}
                      </td>
                      <td className="py-2 pr-3 text-right tabular-nums">
                        {s.polls.toLocaleString()}
                        {s.failures > 0 && (
                          <div className="text-[10px] text-[color:var(--fg-dim)]">
                            ({s.failures} failed)
                          </div>
                        )}
                      </td>
                      <td className="py-2 pr-3 text-right tabular-nums">
                        <span
                          className={
                            s.polls === 0
                              ? "text-[color:var(--fg-muted)]"
                              : s.success_rate >= 0.95
                                ? "text-good"
                                : s.success_rate >= 0.8
                                  ? "text-warn"
                                  : "text-bad"
                          }
                          data-testid={`sources-rate-${s.name}`}
                        >
                          {s.polls === 0 ? "—" : formatSuccessRate(s.success_rate)}
                        </span>
                      </td>
                      <td className="py-2 pr-3 text-right tabular-nums">
                        {s.items_total.toLocaleString()}
                        {s.total_new_items != null && (
                          <div
                            data-testid={`sources-new-items-${s.name}`}
                            className="text-[10px] text-[color:var(--fg-dim)]"
                            title="New (deduped) items ingested from this feed since boot"
                          >
                            {s.total_new_items.toLocaleString()} new
                          </div>
                        )}
                      </td>
                      <td className="py-2 pr-3 text-[color:var(--fg-dim)] tabular-nums whitespace-nowrap">
                        {s.last_status_at ? fmtRel(s.last_status_at) : "never"}
                      </td>
                      <td className="py-2 pr-3 max-w-[260px]">
                        {hasError ? (
                          <button
                            type="button"
                            data-testid={`sources-error-toggle-${s.name}`}
                            onClick={() => toggleExpand(s.name)}
                            className="text-left text-bad hover:underline w-full"
                            aria-expanded={isExpanded}
                            title={isExpanded ? "Click to collapse" : "Click to expand full error"}
                          >
                            <span
                              className={
                                isExpanded
                                  ? "break-words"
                                  : "block truncate"
                              }
                            >
                              {s.last_error}
                              {!isExpanded && s.last_status_code && ` · ${s.last_status_code}`}
                            </span>
                          </button>
                        ) : (
                          <span className="text-[color:var(--fg-dim)]">—</span>
                        )}
                      </td>
                      <td className="py-2 text-right whitespace-nowrap">
                        {(() => {
                          const busy = inFlight.has(s.url);
                          const err = errors[s.url];
                          return (
                            <div className="flex flex-col items-end gap-1">
                              <button
                                type="button"
                                data-testid={`sources-probe-${s.name}`}
                                onClick={() => handleProbe(s.url)}
                                disabled={busy || !s.url}
                                className="btn !px-2 !py-0.5 !text-[10px] disabled:opacity-50 disabled:cursor-not-allowed"
                                aria-busy={busy}
                                aria-label={`Probe ${s.name} now`}
                                title="Probe this feed now — bypasses cooldown"
                              >
                                {busy ? (
                                  <span
                                    data-testid={`sources-probe-spinner-${s.name}`}
                                    className="inline-block h-3 w-3 animate-spin rounded-full border border-[color:var(--fg-muted)] border-t-transparent"
                                    aria-hidden
                                  />
                                ) : (
                                  "probe"
                                )}
                              </button>
                              {err && (
                                <div
                                  data-testid={`sources-probe-error-${s.name}`}
                                  className="text-[10px] text-bad max-w-[140px] break-words text-right"
                                  role="alert"
                                >
                                  {err}
                                </div>
                              )}
                            </div>
                          );
                        })()}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  );
}
