import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api, fmtDate, fmtScore, safeHref } from "@/lib/api";
import { Pill } from "@/components/Pill";
import { Skeleton, ErrorBox, EmptyState } from "@/components/Skeleton";
import { useUrlFilters } from "@/hooks/useUrlFilters";
import { RecordDrawer } from "@/features/record-detail/RecordDrawer";
import type { FinancialRecord } from "@/types/api";

export function FeedPage() {
  const { filters, setFilter, clear } = useUrlFilters();
  const { captureId } = useParams();
  const nav = useNavigate();

  const facets = useQuery({ queryKey: ["facets"], queryFn: () => api.facets(500), staleTime: 10_000 });

  const list = useQuery<{ items: FinancialRecord[] }>({
    queryKey: ["feed-list", filters.ac, filters.rc, filters.sym, filters.relevant],
    queryFn: async () => {
      if (filters.ac) return api.byAssetClass(filters.ac, 200);
      if (filters.rc) return api.byReason(filters.rc, 200);
      if (filters.sym) return api.bySymbol(filters.sym, 200);
      return api.recent(200, filters.relevant !== "all");
    },
    staleTime: 5_000,
  });

  // client-side filters that the backend doesn't index for
  const filtered = useMemo(() => {
    let items = list.data?.items ?? [];
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
  }, [list.data, filters]);

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
        <div className="flex items-baseline gap-3 mb-2 text-xs text-[color:var(--fg-dim)]">
          <span>{filtered.length} record{filtered.length === 1 ? "" : "s"}</span>
          {list.isFetching && <span>(refreshing…)</span>}
          {filters.q && <span>· q="{filters.q}"</span>}
          {filters.ac && <span>· asset={filters.ac}</span>}
          {filters.rc && <span>· reason={filters.rc}</span>}
          {filters.sym && <span>· sym={filters.sym}</span>}
        </div>
        {list.isLoading ? (
          <div className="grid gap-2">
            {Array.from({ length: 8 }).map((_, i) => <Skeleton key={i} className="h-12" />)}
          </div>
        ) : list.error ? <ErrorBox err={list.error} /> :
          filtered.length === 0 ? <EmptyState title="No matches" hint="Try clearing filters." /> : (
            <ul className="divide-y divide-[color:var(--border)] rounded-md border border-[color:var(--border)] bg-[color:var(--bg-elev)]">
              {filtered.map((r) => (
                <FeedRow key={r.capture_id} r={r} onOpen={() => nav(`/feed/${encodeURIComponent(r.capture_id)}`)} />
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

function FeedRow({ r, onOpen }: { r: FinancialRecord; onOpen: () => void }) {
  const href = safeHref(r.url);
  return (
    <li className="px-3 py-2 hover:bg-[color:var(--bg-elev2)]">
      <div className="grid grid-cols-[80px_1fr_auto] gap-3 items-start">
        <div className="grid">
          <span className="text-[10px] text-[color:var(--fg-dim)]">{fmtDate(r.published_ts).split(",")[0]}</span>
          <span className="text-[10px] text-[color:var(--fg-muted)] truncate" title={r.domain ?? ""}>{r.domain ?? ""}</span>
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
