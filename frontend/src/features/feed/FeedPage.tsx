import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate, useParams } from "react-router-dom";
import { api, fmtRel, fmtScore, safeHref } from "@/lib/api";
import { mergeByCaptureId, newCaptureIds } from "@/lib/feedMerge";
import { Pill } from "@/components/Pill";
import { Skeleton, ErrorBox, EmptyState } from "@/components/Skeleton";
import { useUrlFilters } from "@/hooks/useUrlFilters";
import { RecordDrawer } from "@/features/record-detail/RecordDrawer";
import { useDesktopAlertState } from "@/hooks/useDesktopAlerts";
import type { FinancialRecord } from "@/types/api";

export function FeedPage() {
  const { filters, setFilter, clear } = useUrlFilters();
  const { captureId } = useParams();
  const nav = useNavigate();

  const qc = useQueryClient();
  const facets = useQuery({ queryKey: ["facets"], queryFn: () => api.facets(500), staleTime: 10_000 });
  const news = useQuery({
    queryKey: ["news-status"],
    queryFn: api.newsStatus,
    // Poll more often than the backend ticks so the "fetching now" badge
    // and the next-poll countdown stay visibly fresh.
    refetchInterval: 2_000,
    staleTime: 1_000,
  });
  const pollNow = useMutation({
    mutationFn: api.newsPollNow,
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["news-status"] });
      qc.invalidateQueries({ queryKey: ["summary"] });
      qc.invalidateQueries({
        predicate: (q) => Array.isArray(q.queryKey) && q.queryKey[0] === "feed-list",
      });
    },
  });

  // In-app toast on/off (defaults on; persisted in localStorage).
  const [alertState, setAlertEnabled] = useDesktopAlertState();


  const list = useQuery<{ items: FinancialRecord[] }>({
    queryKey: ["feed-list", filters.ac, filters.rc, filters.sym, filters.relevant],
    queryFn: async () => {
      if (filters.ac) return api.byAssetClass(filters.ac, 200);
      if (filters.rc) return api.byReason(filters.rc, 200);
      if (filters.sym) return api.bySymbol(filters.sym, 200);
      return api.recent(200, filters.relevant !== "all");
    },
    staleTime: 3_000,
    // Polling fallback: even with SSE up, the news poller runs every 20s,
    // and this guarantees the feed redraws within ~5s of a new ingest if
    // SSE is dropped or the browser tab is throttled.
    refetchInterval: 5_000,
    refetchIntervalInBackground: false,
  });

  // ── live-polling buffer ─────────────────────────────────────────────────
  // While a record is open in the drawer (or the user explicitly froze the
  // list), incoming rows are buffered instead of replacing the current
  // snapshot. This prevents the analyst's reading viewport from reordering.
  const [stableRows, setStableRows] = useState<FinancialRecord[]>([]);
  const [buffered, setBuffered] = useState<FinancialRecord[]>([]);
  const isFrozen = !!captureId;
  // The last query key that fed `stableRows` — change → reset snapshot.
  const prevQueryKey = useRef<string>("");

  // ── freshness tracking ──────────────────────────────────────────────────
  // The user shouldn't need to count records to know the poller is working.
  // We keep a per-session Set of capture_ids we've seen since the page
  // mounted; any row not in that set is "fresh" and gets a brief CSS flash
  // (`animate-feed-flash`). Tracked in refs so updates don't re-render.
  const seenIds = useRef<Set<string>>(new Set());
  const [freshIds, setFreshIds] = useState<Set<string>>(new Set());
  const firstSnapshotApplied = useRef(false);
  const freshTimers = useRef<Map<string, number>>(new Map());

  function markFresh(ids: string[]) {
    if (ids.length === 0) return;
    setFreshIds((prev) => {
      const next = new Set(prev);
      for (const id of ids) next.add(id);
      return next;
    });
    for (const id of ids) {
      // Clear after the CSS animation finishes so the row settles into
      // its normal background.
      const t = window.setTimeout(() => {
        setFreshIds((prev) => {
          if (!prev.has(id)) return prev;
          const next = new Set(prev);
          next.delete(id);
          return next;
        });
        freshTimers.current.delete(id);
      }, 4_800);
      freshTimers.current.set(id, t);
    }
  }
  useEffect(() => () => {
    freshTimers.current.forEach((t) => window.clearTimeout(t));
    freshTimers.current.clear();
  }, []);

  useEffect(() => {
    const key = JSON.stringify([filters.ac, filters.rc, filters.sym, filters.relevant]);
    const incoming = list.data?.items ?? [];
    if (key !== prevQueryKey.current) {
      // Filter changed — always replace, drop any buffer + freshness
      // (the user is asking for a different cut, not for live deltas).
      prevQueryKey.current = key;
      setStableRows(incoming);
      setBuffered([]);
      seenIds.current = new Set(incoming.map((i) => i.capture_id));
      firstSnapshotApplied.current = true;
      setFreshIds(new Set());
      return;
    }
    if (!isFrozen) {
      // Free viewport — merge in place, sorted, deduped.
      setStableRows((prev) => mergeByCaptureId(prev, incoming));
      setBuffered([]);
      // Detect genuinely new items vs the seen-set and flash them.
      // Skip the very first snapshot (the initial backfill is not "new").
      if (firstSnapshotApplied.current) {
        const newOnes = incoming
          .map((i) => i.capture_id)
          .filter((id) => !seenIds.current.has(id));
        if (newOnes.length > 0) {
          newOnes.forEach((id) => seenIds.current.add(id));
          markFresh(newOnes);
        }
      } else {
        incoming.forEach((i) => seenIds.current.add(i.capture_id));
        firstSnapshotApplied.current = true;
      }
      return;
    }
    // Drawer open — buffer only what's NEW relative to current snapshot.
    const newIds = newCaptureIds(stableRows, incoming);
    if (newIds.length === 0) return;
    const idSet = new Set(newIds);
    setBuffered((prev) => mergeByCaptureId(prev, incoming.filter((i) => idSet.has(i.capture_id))));
  }, [list.data, filters.ac, filters.rc, filters.sym, filters.relevant, isFrozen]);

  function applyBufferedRows() {
    setStableRows((prev) => mergeByCaptureId(prev, buffered));
    setBuffered([]);
  }

  // client-side filters that the backend doesn't index for
  const filtered = useMemo(() => {
    let items = stableRows;
    if (filters.relevant === "only") items = items.filter((i) => i.is_finance_relevant);
    if (filters.sentiment) items = items.filter((i) => i.sentiment_label === filters.sentiment);
    if (filters.diagnosticOnly === "1") items = items.filter((i) => i.diagnostic_multimodal_enabled);
    if (filters.q) {
      const q = filters.q.toLowerCase();
      items = items.filter((i) =>
        (i.title ?? "").toLowerCase().includes(q) ||
        (i.domain ?? "").toLowerCase().includes(q) ||
        i.candidate_symbols.some((s) => s.toLowerCase().includes(q))
      );
    }
    return items;
  }, [stableRows, filters]);

  return (
    <div className="grid gap-3 lg:grid-cols-[260px_1fr]">
      {/* Sidebar — filters */}
      <aside className="card grid gap-3 h-fit lg:sticky lg:top-3">
        <div>
          <label className="label" htmlFor="q">search</label>
          <input
            id="q"
            type="search"
            className="input w-full mt-1"
            placeholder="title, domain, symbol…"
            value={filters.q ?? ""}
            onChange={(e) => setFilter("q", e.target.value || null)}
          />
        </div>
        <div>
          <div className="label mb-1">relevance</div>
          <div className="flex gap-1">
            {(["only", "all"] as const).map((v) => (
              <button
                key={v}
                onClick={() => setFilter("relevant", v)}
                className={`chip ${filters.relevant === v ? "chip-active" : ""}`}
                aria-pressed={filters.relevant === v}
              >
                {v === "only" ? "finance-only" : "all"}
              </button>
            ))}
          </div>
        </div>
        {facets.data && (
          <>
            <FacetGroup
              label="asset class"
              items={facets.data.asset_classes}
              active={filters.ac ?? null}
              onPick={(v) => setFilter("ac", filters.ac === v ? null : v)}
            />
            <FacetGroup
              label="reason code"
              items={facets.data.reason_codes}
              active={filters.rc ?? null}
              onPick={(v) => setFilter("rc", filters.rc === v ? null : v)}
            />
            <FacetGroup
              label="symbol"
              items={facets.data.symbols.slice(0, 20)}
              active={filters.sym ?? null}
              onPick={(v) => setFilter("sym", filters.sym === v ? null : v)}
            />
            <FacetGroup
              label="sentiment"
              items={facets.data.sentiments}
              active={filters.sentiment ?? null}
              onPick={(v) => setFilter("sentiment", filters.sentiment === v ? null : v)}
            />
          </>
        )}
        <div className="flex items-center justify-between">
          <button className="btn" onClick={clear}>clear all</button>
          <button
            className="btn"
            onClick={() => navigator.clipboard?.writeText(window.location.href)}
            title="Copy current filtered view"
          >
            copy link
          </button>
        </div>
      </aside>

      {/* Results */}
      <section>
        {news.data?.enabled && (
          <div
            className="mb-2 flex flex-wrap items-center gap-x-3 gap-y-1 rounded-md border border-[color:var(--border)] bg-[color:var(--bg-elev)] px-3 py-1.5 text-[11px] text-[color:var(--fg-dim)]"
            aria-live="polite"
          >
            <span className="inline-flex items-center gap-1.5">
              <span
                className={`inline-block h-1.5 w-1.5 rounded-full animate-pulse-dot ${
                  news.data.is_polling ? "bg-accent" : "bg-good"
                }`}
                aria-hidden="true"
              />
              <span className="uppercase tracking-wider">
                {news.data.is_polling ? "fetching" : "live"}
              </span>
            </span>
            <span>
              {news.data.feeds} source{news.data.feeds === 1 ? "" : "s"} · every {Math.round((news.data.interval_seconds ?? 30))}s
            </span>
            <span>
              last fetch <span className="text-[color:var(--fg)]">{fmtRel(news.data.last_run_at) || "—"}</span>
              {news.data.last_ingested > 0 && (
                <span
                  key={news.data.last_run_at ?? "0"}
                  className="text-good inline-block animate-count-pulse"
                > · +{news.data.last_ingested}</span>
              )}
            </span>
            {news.data.next_run_at && !news.data.is_polling && (
              <span>next {fmtRel(news.data.next_run_at) || "soon"}</span>
            )}
            <span className="ml-auto inline-flex items-center gap-3">
              <span>{news.data.total_ingested.toLocaleString()} ingested this session</span>
              <button
                type="button"
                className={`chip text-[11px] ${alertState === "on" ? "chip-active" : ""}`}
                onClick={() => setAlertEnabled(alertState !== "on")}
                title={
                  alertState === "on"
                    ? "In-app toasts active for high-relevance arrivals (score ≥ 0.85). Click to mute."
                    : "Toasts muted. Click to show a top-right notification each time a high-relevance article arrives."
                }
              >
                {alertState === "on" ? "🔔 alerts on" : "🔕 alerts off"}
              </button>
              <button
                type="button"
                className="chip text-[11px]"
                onClick={() => pollNow.mutate()}
                disabled={pollNow.isPending || news.data.is_polling}
                title="Trigger an immediate poll across all sources"
              >
                {pollNow.isPending || news.data.is_polling ? "polling…" : "poll now"}
              </button>
            </span>
            {news.data.last_error && (
              <span className="text-warn" title={news.data.last_error}>· error</span>
            )}
          </div>
        )}
        <div className="flex items-baseline gap-3 mb-2 text-xs text-[color:var(--fg-dim)]">
          <span>{filtered.length} record{filtered.length === 1 ? "" : "s"}</span>
          {list.isFetching && <span>(refreshing…)</span>}
          {filters.q && <span>· q="{filters.q}"</span>}
          {filters.ac && <span>· asset={filters.ac}</span>}
          {filters.rc && <span>· reason={filters.rc}</span>}
          {filters.sym && <span>· sym={filters.sym}</span>}
          {isFrozen && (
            <span className="text-warn" title="The feed is paused while you read a record">· paused</span>
          )}
        </div>
        {buffered.length > 0 && (
          <button
            type="button"
            onClick={applyBufferedRows}
            className="mb-2 w-full rounded-md border border-accent/60 bg-accent/10 px-3 py-1.5 text-xs text-accent hover:bg-accent/15"
            aria-live="polite"
            data-testid="buffer-flush"
          >
            {buffered.length} new item{buffered.length === 1 ? "" : "s"} available — click to update
          </button>
        )}
        {list.isLoading ? (
          <div className="grid gap-2">
            {Array.from({ length: 8 }).map((_, i) => <Skeleton key={i} className="h-12" />)}
          </div>
        ) : list.error ? <ErrorBox err={list.error} /> :
          filtered.length === 0 ? <EmptyState title="No matches" hint="Try clearing filters." /> : (
            <ul className="divide-y divide-[color:var(--border)] rounded-md border border-[color:var(--border)] bg-[color:var(--bg-elev)]">
              {filtered.map((r) => (
                <FeedRow
                  key={r.capture_id}
                  r={r}
                  isFresh={freshIds.has(r.capture_id)}
                  onOpen={() => nav(`/feed/${encodeURIComponent(r.capture_id)}`)}
                />
              ))}
            </ul>
          )}
      </section>

      {captureId && (
        <RecordDrawer
          captureId={captureId}
          onClose={() => nav(`/feed${window.location.search}`)}
        />
      )}
    </div>
  );
}

function FacetGroup({
  label, items, active, onPick,
}: {
  label: string;
  items: [string, number][];
  active: string | null;
  onPick: (v: string) => void;
}) {
  if (items.length === 0) return null;
  return (
    <div>
      <div className="label mb-1">{label}</div>
      <div className="flex flex-wrap gap-1">
        {items.slice(0, 16).map(([v, n]) => (
          <button
            key={v}
            onClick={() => onPick(v)}
            className={`chip text-[11px] ${active === v ? "chip-active" : ""}`}
            aria-pressed={active === v}
          >
            {v} <span className="text-[10px] opacity-70">{n}</span>
          </button>
        ))}
      </div>
    </div>
  );
}

function FeedRow({ r, onOpen, isFresh = false }: { r: FinancialRecord; onOpen: () => void; isFresh?: boolean }) {
  const href = safeHref(r.url);
  return (
    <li
      className={`px-3 py-2 hover:bg-[color:var(--bg-elev2)] ${isFresh ? "animate-feed-flash" : ""}`}
      data-fresh={isFresh ? "true" : undefined}
    >
      <div className="grid grid-cols-[90px_1fr_auto] gap-3 items-start">
        <div className="grid" title={r.published_ts ?? ""}>
          <span className="text-[11px] text-[color:var(--fg-dim)]">
            {fmtRel(r.published_ts)}
            {isFresh && (
              <span className="ml-1 text-good text-[9px] uppercase tracking-wider" aria-label="freshly arrived">
                new
              </span>
            )}
          </span>
          <span className="text-[10px] text-[color:var(--fg-muted)] truncate">{r.domain ?? ""}</span>
        </div>
        <div className="min-w-0">
          <div className="text-sm">
            <button onClick={onOpen} className="text-left hover:underline">
              {r.title ?? "(untitled)"}
            </button>
            {href && (
              <a href={href} target="_blank" rel="noopener noreferrer"
                 className="ml-2 text-[10px] text-[color:var(--fg-dim)] hover:text-accent" aria-label="Open external">
                ↗
              </a>
            )}
          </div>
          <div className="flex flex-wrap gap-1 mt-1">
            {r.asset_classes.map((ac) => <Pill key={ac} variant="ac">{ac}</Pill>)}
            {r.impact_reason_codes.slice(0, 3).map((rc) => <Pill key={rc} variant="rc">{rc}</Pill>)}
            {r.candidate_symbols.slice(0, 3).map((s) => <Pill key={s} variant="sym">{s}</Pill>)}
            {r.sentiment_label && r.sentiment_label !== "unknown" && (
              <Pill variant={r.sentiment_label === "positive" ? "good" : r.sentiment_label === "negative" ? "bad" : "default"}>
                {r.sentiment_label}
              </Pill>
            )}
          </div>
        </div>
        <div className="text-xs text-[color:var(--fg-dim)] flex flex-col items-end gap-1">
          <span className="font-semibold text-[color:var(--fg)]">{fmtScore(r.finance_relevance_score)}</span>
          {!r.is_finance_relevant && <Pill variant="warn">filtered</Pill>}
          {r.diagnostic_multimodal_enabled && <Pill variant="warn" title="research diagnostic">diag</Pill>}
        </div>
      </div>
    </li>
  );
}
