import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate, useParams } from "react-router-dom";
import { api, fmtRel, fmtScore, safeHref, scoreToneClass } from "@/lib/api";
import { t, useLang } from "@/lib/i18n";
import { mergeByCaptureId, newCaptureIds } from "@/lib/feedMerge";
import { Pill } from "@/components/Pill";
import { JargonTooltip } from "@/components/JargonTooltip";
import { ExportMenu } from "@/components/ExportMenu";
import { Icon } from "@/components/Icon";
import { Skeleton, ErrorBox, EmptyState } from "@/components/Skeleton";
import { useUrlFilters } from "@/hooks/useUrlFilters";
import { RecordDrawer } from "@/features/record-detail/RecordDrawer";
import { getAlertThreshold, pushToast, setAlertThreshold, useDesktopAlertState } from "@/hooks/useDesktopAlerts";
import { useWatchlist } from "@/features/quant/useWatchlist";
import { hasOpenOverlays } from "@/context/overlayCoordinator";
import {
  clearSelection,
  downloadSelection,
  extractUniqueSymbols,
  extractUrls,
  selectAll,
  selectedRecords,
  toggleSelection,
} from "@/features/feed/bulkSelection";
import type { FinancialRecord } from "@/types/api";

export function FeedPage() {
  // Subscribe to locale changes so the search placeholder and relevance
  // chips re-render when the user flips between en/tr on Settings.
  useLang();
  const { filters, setFilter, clear } = useUrlFilters();
  const { captureId } = useParams();
  const nav = useNavigate();

  const qc = useQueryClient();
  const facets = useQuery({ queryKey: ["facets"], queryFn: () => api.facets(500), staleTime: 10_000 });
  // Top user-defined tags — distinct from pipeline labels above. Refetched on
  // every add/remove (the RecordDrawer mutation invalidates this query key).
  const topTags = useQuery({
    queryKey: ["tags-top"],
    queryFn: () => api.listTags(20),
    staleTime: 15_000,
  });
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

  // Drive archiver — every 30s the backend drains old rows to a CSV on
  // the user's cloud-sync folder; the UI shows where + the running total.
  const archive = useQuery({
    queryKey: ["archive-status"],
    queryFn: api.archiveStatus,
    refetchInterval: 4_000,
    staleTime: 2_000,
  });
  const archiveNow = useMutation({
    mutationFn: api.archiveNow,
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["archive-status"] });
      qc.invalidateQueries({ queryKey: ["summary"] });
      qc.invalidateQueries({
        predicate: (q) => Array.isArray(q.queryKey) && q.queryKey[0] === "feed-list",
      });
    },
  });

  // In-app toast on/off (defaults on; persisted in localStorage).
  const [alertState, setAlertEnabled] = useDesktopAlertState();
  // Threshold knob — defaults to 0.65 (calibrated against the real
  // scoring distribution; the older default of 0.85 was higher than any
  // item ever scored, which is why the alarm never fired).
  const [alertThreshold, setAlertThresholdState] = useState<number>(() => getAlertThreshold());


  const list = useQuery<{ items: FinancialRecord[] }>({
    queryKey: ["feed-list", filters.ac, filters.rc, filters.sym, filters.tag, filters.relevant],
    queryFn: async () => {
      // Tag filter wins when set — user tags are a deliberate analyst cut and
      // shouldn't be silently overridden by a stale ac/rc/sym chip.
      if (filters.tag) return api.recordsByTag(filters.tag, 200);
      if (filters.ac) return api.byAssetClass(filters.ac, 200);
      if (filters.rc) return api.byReason(filters.rc, 200);
      if (filters.sym) return api.bySymbol(filters.sym, 200);
      return api.recent(200, filters.relevant !== "all");
    },
    staleTime: 3_000,
    // Polling fallback: even with SSE up, the news poller runs every 10s
    // (NewsPollerConfig.poll_interval_seconds), and this guarantees the
    // feed redraws within ~5s of a new ingest if SSE is dropped or the
    // browser tab is throttled.
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
    const key = JSON.stringify([filters.ac, filters.rc, filters.sym, filters.tag, filters.relevant]);
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
      const shouldSeedFirstNonEmptySnapshot =
        !firstSnapshotApplied.current ||
        (incoming.length > 0 && stableRows.length === 0 && seenIds.current.size === 0);

      // Free viewport — merge in place, sorted, deduped.
      setStableRows((prev) => mergeByCaptureId(prev, incoming));
      setBuffered([]);
      // Detect genuinely new items vs the seen-set and flash them.
      // Skip the very first snapshot (the initial backfill is not "new").
      if (!shouldSeedFirstNonEmptySnapshot) {
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
    // NOTE: stableRows.length is included so the "first non-empty snapshot"
    // seed gate (line ~174) reads a live value instead of a closed-over zero
    // from the initial render. stableRows itself is mutated through
    // setStableRows callbacks above, so adding `.length` is loop-safe.
  }, [list.data, filters.ac, filters.rc, filters.sym, filters.tag, filters.relevant, isFrozen, stableRows.length]);

  function applyBufferedRows() {
    setStableRows((prev) => mergeByCaptureId(prev, buffered));
    setBuffered([]);
  }

  // ── multi-select bulk ops ───────────────────────────────────────────────
  // capture_id Set; component-local, never persisted across navigation.
  // Cleared (a) on the Esc shortcut, (b) on filter changes (so a user
  // narrowing the view doesn't keep stale ticks for rows that fell out),
  // (c) on explicit "clear selection".
  const [selectedIds, setSelectedIds] = useState<Set<string>>(() => new Set());
  // Track the row the user navigated to via arrow keys (or last clicked
  // toggle) so the `a` shortcut knows which one to toggle. -1 = no focus.
  const [focusedIdx, setFocusedIdx] = useState<number>(-1);
  const focusedIdxRef = useRef(focusedIdx);
  useEffect(() => {
    focusedIdxRef.current = focusedIdx;
  }, [focusedIdx]);

  const watchlist = useWatchlist();
  const watchlistAddRef = useRef(watchlist.add);
  useEffect(() => {
    watchlistAddRef.current = watchlist.add;
  }, [watchlist.add]);

  const toggleOne = useCallback((captureId: string) => {
    setSelectedIds((prev) => toggleSelection(prev, captureId));
  }, []);
  const clearAll = useCallback(() => setSelectedIds(clearSelection()), []);

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

  // Reset selection when the user changes the underlying filter cut —
  // otherwise the toolbar could read "N selected" while none of the ticked
  // rows are even visible. The dep is on filter primitives, not `filtered`,
  // so polling deltas to the same cut don't blow away selection state.
  const filterKey = JSON.stringify([
    filters.ac,
    filters.rc,
    filters.sym,
    filters.tag,
    filters.relevant,
    filters.sentiment,
    filters.diagnosticOnly,
    filters.q,
  ]);
  const lastFilterKey = useRef(filterKey);
  useEffect(() => {
    if (filterKey !== lastFilterKey.current) {
      lastFilterKey.current = filterKey;
      setSelectedIds(clearSelection());
      setFocusedIdx(-1);
    }
  }, [filterKey]);

  const allSelected = filtered.length > 0 && filtered.every((r) => selectedIds.has(r.capture_id));
  const someSelected = !allSelected && filtered.some((r) => selectedIds.has(r.capture_id));
  const selectionCount = selectedIds.size;

  const handleSelectAllVisible = useCallback(() => {
    setSelectedIds(selectAll(filtered));
  }, [filtered]);

  const handleSelectAllToggle = useCallback(() => {
    if (allSelected) {
      setSelectedIds(clearSelection());
    } else {
      handleSelectAllVisible();
    }
  }, [allSelected, handleSelectAllVisible]);

  // ── bulk action handlers ────────────────────────────────────────────────
  const handleBulkWatchlist = useCallback(() => {
    const picked = selectedRecords(filtered, selectedIds);
    const symbols = extractUniqueSymbols(picked);
    if (symbols.length === 0) {
      pushToast({
        id: `bulk-watchlist-empty-${Date.now()}`,
        title: "No symbols in selection",
        domain: "feed · bulk",
        score: 0,
        reasons: [`${picked.length} records had zero candidate symbols`],
        symbols: [],
        severity: "warning",
      });
      return;
    }
    for (const sym of symbols) watchlistAddRef.current(sym);
    pushToast({
      id: `bulk-watchlist-${Date.now()}`,
      title: `Added ${symbols.length} symbol${symbols.length === 1 ? "" : "s"} to watchlist`,
      domain: "feed · bulk",
      score: 0.6,
      reasons: [`from ${picked.length} selected record${picked.length === 1 ? "" : "s"}`],
      symbols: symbols.slice(0, 5),
      severity: "success",
    });
  }, [filtered, selectedIds]);

  const handleBulkCopyUrls = useCallback(() => {
    const picked = selectedRecords(filtered, selectedIds);
    const urls = extractUrls(picked);
    if (urls.length === 0) {
      pushToast({
        id: `bulk-copy-empty-${Date.now()}`,
        title: "No URLs in selection",
        domain: "feed · bulk",
        score: 0,
        reasons: ["selected records had no URL"],
        symbols: [],
        severity: "warning",
      });
      return;
    }
    const text = urls.join("\n");
    const ok = (msg: string) => pushToast({
      id: `bulk-copy-${Date.now()}`,
      title: msg,
      domain: "feed · bulk",
      score: 0.5,
      reasons: [`${picked.length} record${picked.length === 1 ? "" : "s"} selected`],
      symbols: [],
      severity: "success",
    });
    const fail = (err: unknown) => pushToast({
      id: `bulk-copy-fail-${Date.now()}`,
      title: "Copy failed",
      domain: "feed · bulk",
      score: 0,
      reasons: [String((err as { message?: string } | null)?.message ?? err ?? "clipboard error")],
      symbols: [],
      severity: "error",
    });
    if (navigator.clipboard?.writeText) {
      navigator.clipboard.writeText(text)
        .then(() => ok(`Copied ${urls.length} URL${urls.length === 1 ? "" : "s"}`))
        .catch(fail);
    } else {
      fail(new Error("clipboard API unavailable"));
    }
  }, [filtered, selectedIds]);

  const handleBulkExport = useCallback(() => {
    const picked = selectedRecords(filtered, selectedIds);
    if (picked.length === 0) return;
    try {
      downloadSelection(picked);
      pushToast({
        id: `bulk-export-${Date.now()}`,
        title: `Exported ${picked.length} record${picked.length === 1 ? "" : "s"}`,
        domain: "feed · bulk",
        score: 0.5,
        reasons: ["client-side JSON download"],
        symbols: [],
        severity: "success",
      });
    } catch (err) {
      pushToast({
        id: `bulk-export-fail-${Date.now()}`,
        title: "Export failed",
        domain: "feed · bulk",
        score: 0,
        reasons: [String((err as { message?: string } | null)?.message ?? err ?? "unknown error")],
        symbols: [],
        severity: "error",
      });
    }
  }, [filtered, selectedIds]);

  // ── keyboard shortcuts: `a`, Cmd/Ctrl+A, Esc ────────────────────────────
  // Bound at the page level so the user doesn't have to give a row keyboard
  // focus first (rows are buttons, focus is fine, but the analyst is more
  // likely reading text and just wants the modifier shortcut).
  useEffect(() => {
    function onKeyDown(e: KeyboardEvent) {
      if (hasOpenOverlays()) return;
      // Skip when the user is typing into an input/textarea/contenteditable;
      // those swallow letter keys legitimately.
      const tgt = e.target as HTMLElement | null;
      if (tgt && (
        tgt.tagName === "INPUT" ||
        tgt.tagName === "TEXTAREA" ||
        tgt.tagName === "SELECT" ||
        tgt.isContentEditable
      )) return;
      // Cmd+A (mac) / Ctrl+A (win/linux) — select all visible.
      if ((e.metaKey || e.ctrlKey) && (e.key === "a" || e.key === "A") && !e.shiftKey && !e.altKey) {
        // Only steal the shortcut when the feed actually has rows; otherwise
        // let the browser do its native "select all text" thing.
        if (filtered.length === 0) return;
        e.preventDefault();
        setSelectedIds(selectAll(filtered));
        return;
      }
      // Esc — clear selection (only when there *is* a selection; otherwise
      // let the drawer close handler win).
      if (e.key === "Escape") {
        if (selectedIds.size > 0 && !captureId) {
          e.preventDefault();
          setSelectedIds(clearSelection());
        }
        return;
      }
      // `a` (plain) — toggle the currently focused row's selection.
      if (e.key === "a" && !e.metaKey && !e.ctrlKey && !e.shiftKey && !e.altKey) {
        const idx = focusedIdxRef.current;
        if (idx >= 0 && idx < filtered.length) {
          e.preventDefault();
          toggleOne(filtered[idx].capture_id);
        }
      }
      if (e.key === "ArrowDown") {
        if (filtered.length === 0) return;
        e.preventDefault();
        setFocusedIdx((prev) => (prev >= filtered.length - 1 ? 0 : prev + 1));
      }
      if (e.key === "ArrowUp") {
        if (filtered.length === 0) return;
        e.preventDefault();
        setFocusedIdx((prev) => (prev <= 0 ? filtered.length - 1 : prev - 1));
      }
      if (e.key === "Home") {
        if (filtered.length === 0) return;
        e.preventDefault();
        setFocusedIdx(0);
      }
      if (e.key === "End") {
        if (filtered.length === 0) return;
        e.preventDefault();
        setFocusedIdx(filtered.length - 1);
      }
      if (e.key === "Enter") {
        const idx = focusedIdxRef.current;
        if (idx >= 0 && idx < filtered.length) {
          e.preventDefault();
          nav(`/feed/${encodeURIComponent(filtered[idx].capture_id)}`);
        } else if (filtered.length > 0) {
          e.preventDefault();
          setFocusedIdx(0);
          nav(`/feed/${encodeURIComponent(filtered[0].capture_id)}`);
        }
      }
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [filtered, selectedIds.size, captureId, toggleOne, nav]);

  useEffect(() => {
    if (!filtered.length) {
      setFocusedIdx(-1);
      return;
    }
    setFocusedIdx((idx) => (idx >= filtered.length ? filtered.length - 1 : idx));
  }, [filtered.length]);

  const unhealthyFeeds = news.data?.unhealthy_feeds ?? 0;
  const newsStatus = news.data?.last_error
    ? { color: "bg-bad", label: "error" }
    : unhealthyFeeds > 0
      ? { color: "bg-warn", label: "degraded" }
    : news.data?.is_polling
      ? { color: "bg-accent", label: "fetching" }
      : (news.data?.empty_ticks ?? 0) >= 5
        ? { color: "bg-warn", label: "quiet" }
        : { color: "bg-good", label: "live" };

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
            placeholder={t("feed.search.placeholder")}
            value={filters.q ?? ""}
            onChange={(e) => setFilter("q", e.target.value || null)}
          />
        </div>
        <div>
          <div className="label mb-1"><JargonTooltip term="finance relevance score">relevance</JargonTooltip></div>
          <div className="flex gap-1">
            {(["only", "all"] as const).map((v) => (
              <button
                key={v}
                onClick={() => setFilter("relevant", v)}
                className={`chip ${filters.relevant === v ? "chip-active" : ""}`}
                aria-pressed={filters.relevant === v}
              >
                {v === "only" ? t("feed.relevance.only") : t("feed.relevance.all")}
              </button>
            ))}
          </div>
        </div>
        {facets.data && (
          <>
            <FacetGroup
              label={<JargonTooltip term="asset class" />}
              items={facets.data.asset_classes}
              active={filters.ac ?? null}
              onPick={(v) => setFilter("ac", filters.ac === v ? null : v)}
            />
            <FacetGroup
              label={<JargonTooltip term="reason code" />}
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
        {topTags.data && topTags.data.items.length > 0 && (
          <FacetGroup
            label="user tags"
            items={topTags.data.items.map((t) => [t.tag, t.count] as [string, number])}
            active={filters.tag ?? null}
            onPick={(v) => setFilter("tag", filters.tag === v ? null : v)}
          />
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
        <div className="border-t border-[color:var(--border-subtle)] pt-2">
          <div className="label mb-1">export filtered</div>
          <ExportMenu
            label="download"
            formats={["csv", "json"]}
            buildUrl={(f) => api.exportRecordsUrl(f, {
              asset_class: filters.ac ?? undefined,
              reason_code: filters.rc ?? undefined,
              symbol: filters.sym ?? undefined,
            })}
            filenameHint="catchem_records"
            hint="applies current filter chips"
            testId="feed-export"
          />
        </div>
      </aside>

      {/* Results */}
      <section>
        {(news.data?.enabled || archive.data?.enabled) && (
          <section
            className="relative overflow-hidden rounded-xl border border-accent/40 hero-gradient p-6 mb-3"
            aria-live="polite"
          >
            <div aria-hidden className="pointer-events-none absolute -top-20 -left-20 h-48 w-48 rounded-full bg-accent/20 blur-3xl" />
            {news.data?.enabled && (
              <>
                <div className="relative flex flex-wrap items-start justify-between gap-3 mb-3">
                  <div className="flex items-center gap-3">
                    <span className="relative flex h-2 w-2">
                      <span className={`absolute inline-flex h-full w-full animate-ping rounded-full opacity-75 ${newsStatus.color}`} />
                      <span className={`relative inline-flex h-2 w-2 rounded-full ${newsStatus.color}`} />
                    </span>
                    <div>
                      <div className="text-[10px] uppercase tracking-[0.25em] text-accent font-semibold inline-flex items-center gap-2">
                        <span>Live Feed · news poller</span>
                        <span aria-hidden>·</span>
                        <span>{newsStatus.label}</span>
                      </div>
                      <h1 className="text-lg font-semibold mt-0.5 tracking-tight">
                        {news.data.is_polling
                          ? `Polling ${news.data.feeds} source${news.data.feeds === 1 ? "" : "s"}…`
                          : news.data.last_run_at
                            ? `Polling ${news.data.feeds} source${news.data.feeds === 1 ? "" : "s"} · ${news.data.total_ingested.toLocaleString()} ingested`
                            : "Awaiting first poll"}
                      </h1>
                      <div className="mt-1 text-[11px] text-[color:var(--fg-muted)] flex flex-wrap items-center gap-x-2 gap-y-0.5">
                        <span>
                          last fetch {fmtRel(news.data.last_run_at) || "—"}
                          {news.data.last_ingested > 0 && (
                            <span
                              key={news.data.last_run_at ?? "0"}
                              className="text-good inline-block animate-count-pulse ml-1"
                            >+{news.data.last_ingested}</span>
                          )}
                        </span>
                        <span>· every {Math.round(news.data.interval_seconds ?? 10)}s</span>
                        {news.data.next_run_at && !news.data.is_polling && (
                          <span>· next {fmtRel(news.data.next_run_at) || "soon"}</span>
                        )}
                        {unhealthyFeeds > 0 && (
                          <span className="text-warn">{unhealthyFeeds} source{unhealthyFeeds === 1 ? "" : "s"} with latest fetch issue</span>
                        )}
                        {news.data.last_error && (
                          <span className="text-bad" title={news.data.last_error}>· {news.data.last_error}</span>
                        )}
                      </div>
                    </div>
                  </div>
                  <div className="flex items-center gap-2 shrink-0">
                    <button
                      type="button"
                      className={`chip text-[11px] ${alertState === "on" ? "chip-active" : ""}`}
                      onClick={() => setAlertEnabled(alertState !== "on")}
                      title={
                        alertState === "on"
                          ? `Toasts active for arrivals with score ≥ ${alertThreshold.toFixed(2)}. Click to mute.`
                          : "Toasts muted. Click to fire a top-right notification for each high-relevance arrival."
                      }
                    >
                      <span className="inline-flex items-center gap-1">
                        <Icon name={alertState === "on" ? "bell" : "bellOff"} />
                        {alertState === "on" ? "alerts on" : "alerts off"}
                      </span>
                    </button>
                    {alertState === "on" && (
                      <button
                        type="button"
                        className="chip text-[11px]"
                        onClick={() => {
                          // Cycle through preset thresholds: 0.50 → 0.60 → 0.70 → 0.80 → back.
                          // 0.50 is permissive (~50% of items), 0.80 is aggressive
                          // (~top 2-3%). The empirical max on this scorer is ~0.80,
                          // so anything above leaves the user with no toasts at all.
                          const presets = [0.50, 0.60, 0.65, 0.70, 0.80];
                          const idx = presets.findIndex((p) => p >= alertThreshold - 0.001);
                          const next = presets[(idx + 1) % presets.length];
                          setAlertThresholdState(setAlertThreshold(next));
                        }}
                        title={`Alert threshold: ${alertThreshold.toFixed(2)} (max observed ≈ 0.80). Click to cycle through 0.50 → 0.60 → 0.65 → 0.70 → 0.80.`}
                      >
                        ≥{alertThreshold.toFixed(2)}
                      </button>
                    )}
                    <button
                      type="button"
                      className="chip text-[11px]"
                      onClick={() => pollNow.mutate()}
                      disabled={pollNow.isPending || news.data.is_polling}
                      title="Trigger an immediate poll across all sources"
                    >
                      {pollNow.isPending || news.data.is_polling ? "polling…" : "poll now"}
                    </button>
                  </div>
                </div>
                <div className="relative grid gap-2 grid-cols-1 sm:grid-cols-2 md:grid-cols-4 text-[11px]">
                  <FeedStat
                    label="ingested this session"
                    value={news.data.total_ingested.toLocaleString()}
                    hint={news.data.last_ingested > 0 ? `+${news.data.last_ingested} since last poll` : "awaiting next poll"}
                  />
                  <FeedStat
                    label="sources"
                    value={`${news.data.feeds}`}
                    hint={unhealthyFeeds > 0 ? `${unhealthyFeeds} with issues` : "all healthy"}
                    tone={unhealthyFeeds > 0 ? "warn" : "good"}
                  />
                  <FeedStat
                    label="last new"
                    value={fmtRel(news.data.last_new_at) || "—"}
                    hint={news.data.last_new_at ? "fresh signal received" : "awaiting fresh signal"}
                  />
                  <FeedStat
                    label="pub→ingest"
                    value={
                      news.data.last_median_publisher_lag_seconds != null && news.data.last_median_publisher_lag_seconds > 0
                        ? (news.data.last_median_publisher_lag_seconds < 60
                            ? `~${Math.round(news.data.last_median_publisher_lag_seconds)}s`
                            : `~${Math.round(news.data.last_median_publisher_lag_seconds / 60)}m`)
                        : "—"
                    }
                    hint="median lag"
                    tone={
                      news.data.last_median_publisher_lag_seconds != null && news.data.last_median_publisher_lag_seconds > 600
                        ? "warn"
                        : undefined
                    }
                  />
                </div>
              </>
            )}
            {archive.data?.enabled && (
              <div className={`relative flex flex-wrap items-center gap-x-3 gap-y-1 text-[10px] text-[color:var(--fg-dim)] ${news.data?.enabled ? "mt-3 pt-3 border-t border-[color:var(--border-subtle)]" : ""}`}>
                <span className="inline-flex items-center gap-1.5">
                  <span
                    className={`inline-block h-1.5 w-1.5 rounded-full ${
                      archive.data.last_error
                        ? "bg-warn"
                        : archive.data.is_archiving
                          ? "bg-accent animate-pulse-dot"
                          : "bg-good"
                    }`}
                    aria-hidden="true"
                  />
                  <span className="text-[color:var(--fg-muted)] uppercase tracking-wider">drive</span>
                </span>
                <span title={archive.data.drive_dir ?? ""}>
                  → <span className="text-[color:var(--fg)]">
                    {archive.data.current_csv_path?.split("/").slice(-1)[0]
                      ?? archive.data.drive_dir?.split("/").slice(-2).join("/")
                      ?? "—"}
                  </span>
                </span>
                <span>cap {archive.data.local_cap_rows == null ? "—" : archive.data.local_cap_rows}</span>
                <span>every {Math.round(archive.data.interval_seconds ?? 30)}s</span>
                {archive.data.last_run_at && (
                  <span>last drain <span className="text-[color:var(--fg)]">{fmtRel(archive.data.last_run_at) || "—"}</span></span>
                )}
                {archive.data.last_archived_count > 0 && (
                  <span
                    key={archive.data.last_run_at ?? "0"}
                    className="text-good inline-block animate-count-pulse"
                  >+{archive.data.last_archived_count}</span>
                )}
                <span className="ml-auto inline-flex items-center gap-3">
                  <span>{archive.data.total_archived.toLocaleString()} archived this session</span>
                  <button
                    type="button"
                    className="chip text-[10px]"
                    onClick={() => archiveNow.mutate()}
                    disabled={archiveNow.isPending || archive.data.is_archiving}
                    title={`Trigger an immediate archive sweep. Drive dir: ${archive.data.drive_dir ?? "—"}`}
                  >
                    {archiveNow.isPending || archive.data.is_archiving ? "archiving…" : "archive now"}
                  </button>
                </span>
                {archive.data.last_error && (
                  <span className="text-warn" title={archive.data.last_error}>· error</span>
                )}
              </div>
            )}
          </section>
        )}
        <div className="flex items-baseline gap-3 mb-2 text-xs text-[color:var(--fg-dim)]">
          <span>
            {filtered.length} {t("feed.records")}
            {filtered.length === 1 ? "" : ""}
          </span>
          {list.isFetching && <span>({t("feed.refreshing")})</span>}
          {filters.q && <span>· q="{filters.q}"</span>}
          {filters.ac && <span>· asset={filters.ac}</span>}
          {filters.rc && <span>· reason={filters.rc}</span>}
          {filters.sym && <span>· sym={filters.sym}</span>}
          {filters.tag && <span>· tag={filters.tag}</span>}
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
          filtered.length === 0 ? <EmptyState title="No matches" hint="Try clearing filters." action={<button type="button" className="btn" onClick={clear}>Clear filters</button>} /> : (
            <>
              {filtered.length > 0 && (
                <div className="mb-1.5 flex items-center justify-between text-[11px] text-[color:var(--fg-dim)]">
                  <label className="inline-flex items-center gap-2 cursor-pointer">
                    <input
                      type="checkbox"
                      checked={allSelected}
                      ref={(el) => {
                        // Tri-state header: indeterminate when some-but-not-all are ticked.
                        if (el) el.indeterminate = someSelected;
                      }}
                      onChange={handleSelectAllToggle}
                      aria-label={allSelected ? "Deselect all visible" : "Select all visible"}
                      data-testid="feed-select-all"
                      className="h-3.5 w-3.5 accent-accent cursor-pointer"
                    />
                    <span>
                      {selectionCount > 0
                        ? `${selectionCount} selected`
                        : "Select all"}
                    </span>
                  </label>
                  {selectionCount > 0 && (
                    <button
                      type="button"
                      className="text-[color:var(--fg-dim)] hover:text-accent"
                      onClick={clearAll}
                      data-testid="feed-bulk-clear-top"
                    >
                      clear
                    </button>
                  )}
                </div>
              )}
              <ul className="divide-y divide-[color:var(--border)] rounded-md border border-[color:var(--border)] bg-[color:var(--bg-elev)]">
                {filtered.map((r, idx) => (
                  <FeedRow
                    key={r.capture_id}
                    r={r}
                    isFresh={freshIds.has(r.capture_id)}
                    isSelected={selectedIds.has(r.capture_id)}
                    onToggleSelect={() => {
                      setFocusedIdx(idx);
                      toggleOne(r.capture_id);
                    }}
                    onFocusRow={() => setFocusedIdx(idx)}
                    onOpen={() => nav(`/feed/${encodeURIComponent(r.capture_id)}`)}
                  />
                ))}
              </ul>
            </>
          )}
      </section>

      {captureId && (
        <RecordDrawer
          captureId={captureId}
          onClose={() => nav(`/feed${window.location.search}`)}
        />
      )}

      {selectionCount > 0 && (
        <BulkActionToolbar
          count={selectionCount}
          onAddWatchlist={handleBulkWatchlist}
          onCopyUrls={handleBulkCopyUrls}
          onExport={handleBulkExport}
          onClear={clearAll}
        />
      )}
    </div>
  );
}

function BulkActionToolbar({
  count, onAddWatchlist, onCopyUrls, onExport, onClear,
}: {
  count: number;
  onAddWatchlist: () => void;
  onCopyUrls: () => void;
  onExport: () => void;
  onClear: () => void;
}) {
  return (
    <div
      role="region"
      aria-label={`Bulk actions for ${count} selected record${count === 1 ? "" : "s"}`}
      className="fixed inset-x-0 bottom-3 z-[35] flex justify-center px-3 pointer-events-none"
      data-testid="feed-bulk-toolbar"
    >
      <div
        className="pointer-events-auto flex flex-wrap items-center gap-2 rounded-full border border-accent/60 bg-[color:var(--bg-elev)]/95 px-4 py-2 text-xs shadow-lg backdrop-blur animate-toolbar-rise"
      >
        <span className="font-semibold text-accent tabular-nums">
          {count} selected
        </span>
        <span aria-hidden className="text-[color:var(--fg-dim)]">·</span>
        <button
          type="button"
          className="btn"
          onClick={onAddWatchlist}
          data-testid="feed-bulk-watchlist"
        >
          Add to watchlist
        </button>
        <button
          type="button"
          className="btn"
          onClick={onCopyUrls}
          data-testid="feed-bulk-copy"
        >
          Copy URLs
        </button>
        <button
          type="button"
          className="btn"
          onClick={onExport}
          data-testid="feed-bulk-export"
        >
          Export selected
        </button>
        <button
          type="button"
          className="btn"
          onClick={onClear}
          data-testid="feed-bulk-clear"
          title="Esc to clear"
        >
          Clear
        </button>
      </div>
    </div>
  );
}

function FacetGroup({
  label, items, active, onPick,
}: {
  label: React.ReactNode;
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

function FeedStat({ label, value, hint, tone }: { label: string; value: string; hint?: string; tone?: "good" | "warn" | "bad" }) {
  const cls = tone === "good" ? "text-good" : tone === "warn" ? "text-warn" : tone === "bad" ? "text-bad" : "";
  return (
    <div className="rounded-md border border-[color:var(--border-subtle)] bg-[color:var(--bg-elev2)]/40 px-3 py-2">
      <div className="text-[9px] uppercase tracking-wider text-[color:var(--fg-muted)]">{label}</div>
      <div className={`mt-0.5 text-sm font-semibold tabular-nums ${cls}`}>{value}</div>
      {hint && <div className="text-[10px] text-[color:var(--fg-dim)] truncate">{hint}</div>}
    </div>
  );
}

function FeedRow({
  r, onOpen, isFresh = false, isSelected = false, onToggleSelect, onFocusRow,
}: {
  r: FinancialRecord;
  onOpen: () => void;
  isFresh?: boolean;
  isSelected?: boolean;
  onToggleSelect?: () => void;
  onFocusRow?: () => void;
}) {
  const href = safeHref(r.url);
  const score = r.finance_relevance_score;
  // 3-band severity bar on the leading edge — at-a-glance scanning of a
  // long list. Matches `scoreToneClass` in lib/api.ts so the bar + number
  // always agree.
  const accentBar =
    score == null || score < 0.4
      ? "bg-[color:var(--border)]"
      : score >= 0.7
        ? "bg-good"
        : "bg-accent";
  return (
    <li
      tabIndex={0}
      className={`relative px-3 py-2.5 transition-colors ${
        isSelected
          ? "bg-accent/10 ring-1 ring-inset ring-accent/40"
          : "hover:bg-[color:var(--bg-elev2)]"
      } ${isFresh ? "animate-feed-flash" : ""} focus:outline-none focus-visible:ring-1 focus-visible:ring-accent`}
      data-fresh={isFresh ? "true" : undefined}
      data-selected={isSelected ? "true" : undefined}
      onFocus={onFocusRow}
      onMouseEnter={onFocusRow}
      onKeyDown={(e) => {
        if (e.key === " " && onToggleSelect) {
          e.preventDefault();
          onToggleSelect();
        }
      }}
    >
      <span
        aria-hidden
        className={`pointer-events-none absolute left-0 top-2 bottom-2 w-1 rounded-r ${accentBar}`}
      />
      <div className="grid grid-cols-[20px_92px_1fr_auto] gap-3 items-start">
        <div className="pl-1.5 pt-0.5">
          <input
            type="checkbox"
            checked={isSelected}
            onChange={onToggleSelect}
            onClick={(e) => e.stopPropagation()}
            aria-label={isSelected ? "Deselect record" : "Select record"}
            data-testid={`feed-row-checkbox-${r.capture_id}`}
            className="h-3.5 w-3.5 accent-accent cursor-pointer"
          />
        </div>
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
          <div className="text-sm leading-snug">
            <button onClick={onOpen} className="text-left hover:underline">
              {r.title ?? "(untitled)"}
            </button>
            {href && (
              <a href={href} target="_blank" rel="noopener noreferrer"
                 className="ml-2 inline-flex align-middle text-[color:var(--fg-dim)] hover:text-accent" aria-label="Open external">
                <Icon name="external" size={12} />
              </a>
            )}
          </div>
          <div className="flex flex-wrap gap-1 mt-1.5">
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
          <span className={`text-sm font-semibold tabular-nums ${scoreToneClass(score)}`}>
            {fmtScore(score)}
          </span>
          {!r.is_finance_relevant && <Pill variant="warn">filtered</Pill>}
          {r.diagnostic_multimodal_enabled && <Pill variant="warn" title="research diagnostic">diag</Pill>}
        </div>
      </div>
    </li>
  );
}
