import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api } from "@/lib/api";
import { freshnessLabel, useTick } from "@/lib/freshness";
import { Skeleton, ErrorBox, EmptyState } from "@/components/Skeleton";
import { Icon } from "@/components/Icon";

/**
 * Tags page — analyst aggregation view (v39 task #149).
 *
 * Surfaces user-defined record tags persisted by migration v2's
 * `record_tags` table. Distinct from pipeline labels (asset class,
 * reason code, symbol) — these are free-form analyst memos.
 *
 * The page is a read-only aggregation lens; the *editing* surface
 * lives in the record drawer's TagsSection (see RecordDrawer.tsx).
 * From here every chip links to `/feed?tag=<name>` so the analyst
 * can pivot from aggregate → individual records in one click.
 *
 * Refetches every 60s so a tag added on the Feed page eventually
 * appears on this page without a hard reload. `staleTime: 30_000`
 * keeps refetches cheap when the tab is re-focused inside the
 * polling window.
 */
export function TagsPage() {
  // Re-render every 30s so the hero freshness label keeps advancing.
  useTick();
  // We ask for 200 tags max — enough for every realistic analyst's
  // working vocabulary; the backend caps at 200 too (see
  // /api/tags route definition). 60s refetch as required by the spec.
  const tagsQuery = useQuery({
    queryKey: ["tags-aggregate"],
    queryFn: () => api.listTags(200),
    refetchInterval: 60_000,
    staleTime: 30_000,
  });

  const items = tagsQuery.data?.items ?? [];
  // Tag list is sorted by count desc on the server, but be defensive
  // here so a future API change can't silently break the cloud sizing
  // ramp (we lean on `items[0]` being the maximum).
  const sorted = useMemo(() => [...items].sort((a, b) => b.count - a.count), [items]);
  const totalTags = sorted.length;
  const totalTagged = sorted.reduce((sum, it) => sum + it.count, 0);
  const top3 = sorted.slice(0, 3);
  const topTag = sorted[0];
  const avgPerTag = totalTags > 0 ? totalTagged / totalTags : 0;

  const heroHeadline =
    totalTags === 0
      ? "No tags yet"
      : `${totalTags.toLocaleString()} tag${totalTags === 1 ? "" : "s"} covering ${totalTagged.toLocaleString()} record${totalTagged === 1 ? "" : "s"}`;
  const subtitle =
    top3.length === 0
      ? "Tags appear after the first analyst tag is added from any record."
      : `Top: ${top3.map((t) => t.tag).join(" · ")}`;

  return (
    <div className="grid gap-5">
      {/* Hero — matches the SymbolsPage / SettingsPage premium pattern.
          Pulse dot + eyebrow + bold headline + freshness footnote.
          The "Open Feed" chip only appears when there are tags so a
          fresh-install user doesn't get a dead link. */}
      <section
        className="relative overflow-hidden rounded-xl border border-accent/40 hero-gradient p-6"
        data-testid="tags-hero"
      >
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
                Tags · user-defined organization
              </div>
              <h1
                className="text-lg font-semibold mt-0.5 tracking-tight"
                data-testid="tags-headline"
              >
                {heroHeadline}
              </h1>
              <div className="mt-1 text-[11px] text-[color:var(--fg-muted)]">
                {subtitle}
                <span className="text-[10px] text-[color:var(--fg-dim)]">
                  {" "}
                  · {freshnessLabel(tagsQuery.dataUpdatedAt)}
                </span>
              </div>
            </div>
          </div>
          {totalTags > 0 && (
            <Link
              to="/feed"
              className="chip text-accent hover:bg-accent/10 focus:outline-none focus-visible:ring-1 focus-visible:ring-accent"
              data-testid="tags-open-feed"
            >
              Open Feed →
            </Link>
          )}
        </div>

        {/* Four stat tiles — same density as the SymbolsPage top-3 trio
            but adapted for tag aggregates. Always renders so the empty
            state still surfaces the zeroed counters honestly. */}
        <div className="relative grid gap-2 grid-cols-2 md:grid-cols-4 text-[11px]">
          <StatTile label="total tags" value={totalTags.toLocaleString()} />
          <StatTile label="tagged records" value={totalTagged.toLocaleString()} />
          <StatTile
            label="top tag"
            value={topTag?.tag ?? "—"}
            hint={
              topTag
                ? `${topTag.count.toLocaleString()} record${topTag.count === 1 ? "" : "s"}`
                : undefined
            }
            tone="good"
          />
          <StatTile
            label="avg records / tag"
            value={
              totalTags > 0
                ? avgPerTag.toLocaleString(undefined, { maximumFractionDigits: 1 })
                : "—"
            }
          />
        </div>
      </section>

      {tagsQuery.isLoading ? (
        <Skeleton className="h-72" />
      ) : tagsQuery.error ? (
        <ErrorBox err={tagsQuery.error} />
      ) : totalTags === 0 ? (
        <EmptyState
          title="No tags have been added yet"
          hint="Open any record from the Feed page, click 'add tag' in the detail drawer, and type a label (lowercase, no spaces). Tags help you organize records around analyst workflows — e.g. 'watch', 'earnings', 'fade'."
          action={
            <Link
              to="/help"
              className="btn focus:outline-none focus-visible:ring-1 focus-visible:ring-accent"
              data-testid="tags-help-link"
            >
              How to tag records →
            </Link>
          }
        />
      ) : (
        <>
          {/* Tag cloud — visual frequency map. Larger / brighter chips
              are higher-count tags. The min/max font-size ramp (11→22px)
              and the alpha ramp (0.5→1.0) are both keyed off the
              normalized count so a tag with count 1 still renders
              legibly when another tag dominates with count 200. */}
          <section className="card" aria-label="Tag cloud">
            <div className="mb-3 flex items-center gap-2">
              <Icon
                name="search"
                size={14}
                className="text-[color:var(--fg-dim)]"
              />
              <h2 className="text-sm font-semibold">Tag cloud</h2>
              <span className="text-[10px] text-[color:var(--fg-dim)]">
                sized by frequency · click to pivot to Feed
              </span>
            </div>
            <TagCloud items={sorted} />
          </section>

          {/* Detail table — every tag, sorted by frequency. Each row
              navigates to the filtered Feed view. We keep the table
              compact (single-row cells, no wrapping) so a 200-tag
              vocabulary scrolls cleanly. */}
          <section aria-label="Tag details" data-testid="tags-detail-list">
            <div className="mb-2 flex items-center justify-between gap-2">
              <h2 className="label">tag details</h2>
              <span className="text-[10px] text-[color:var(--fg-dim)]">
                sorted by count · descending
              </span>
            </div>
            <ul className="grid gap-1 grid-cols-1 md:grid-cols-2 lg:grid-cols-3">
              {sorted.map((it) => (
                <li key={it.tag}>
                  <Link
                    to={`/feed?tag=${encodeURIComponent(it.tag)}`}
                    className="card flex items-center justify-between hover:bg-[color:var(--bg-elev2)] transition-colors focus:outline-none focus-visible:ring-1 focus-visible:ring-accent"
                    data-testid={`tags-row-${it.tag}`}
                  >
                    <span className="font-semibold text-good">{it.tag}</span>
                    <span className="text-[10px] text-[color:var(--fg-dim)]">
                      {it.count.toLocaleString()} record
                      {it.count === 1 ? "" : "s"}
                    </span>
                  </Link>
                </li>
              ))}
            </ul>
          </section>
        </>
      )}
    </div>
  );
}

function StatTile({
  label,
  value,
  hint,
  tone,
}: {
  label: string;
  value: string;
  hint?: string;
  tone?: "good";
}) {
  return (
    <div className="rounded-md border border-[color:var(--border-subtle)] bg-[color:var(--bg-elev2)]/40 px-3 py-2">
      <div className="text-[9px] uppercase tracking-wider text-[color:var(--fg-muted)]">
        {label}
      </div>
      <div
        className={`mt-0.5 text-xl font-semibold tabular-nums truncate ${tone === "good" ? "text-good" : ""}`}
        title={value}
      >
        {value}
      </div>
      {hint && (
        <div className="text-[10px] tabular-nums text-[color:var(--fg-dim)]">
          {hint}
        </div>
      )}
    </div>
  );
}

/**
 * Tag-cloud renderer.
 *
 * Sizing model: every tag is a chip with `font-size` linearly
 * interpolated between MIN_PX (11) and MAX_PX (22) based on its
 * count relative to the max. The opacity ramps from 0.55 to 1.0
 * on the same curve so low-count tags read as the visual
 * background and high-count tags pop.
 *
 * We avoid a logarithmic ramp on purpose — analyst vocabularies
 * skew small (5-20 tags) and a log curve flattens the difference
 * between a count-of-2 and a count-of-50, which is the most
 * useful gradient to actually see in the cloud.
 *
 * Exported as a named component for unit-test introspection.
 */
export function TagCloud({
  items,
}: {
  items: { tag: string; count: number }[];
}) {
  // Defensive sort: callers pass a sorted-desc array today, but the
  // size/opacity ramp must not silently break if a future caller passes
  // unsorted data. Sorting inside the component decouples the visual
  // contract from the parent. useMemo keeps it cheap on re-render.
  const sortedItems = useMemo(
    () => [...items].sort((a, b) => b.count - a.count),
    [items],
  );
  const maxCount = sortedItems.reduce((m, it) => Math.max(m, it.count), 1);
  // Now that `sortedItems` is guaranteed sorted-desc, the last element is
  // the minimum — seeding the reducer with `sortedItems[0]?.count` (the
  // maximum) lets Math.min correctly narrow down. We could also seed with
  // Number.POSITIVE_INFINITY but the current shape keeps test-doubles
  // (which often pass `[]`) working without an Infinity span.
  const minCount = sortedItems.reduce(
    (m, it) => Math.min(m, it.count),
    sortedItems[0]?.count ?? 1,
  );
  const span = Math.max(1, maxCount - minCount);
  const MIN_PX = 11;
  const MAX_PX = 22;

  return (
    <div className="flex flex-wrap items-center gap-1.5" data-testid="tag-cloud">
      {sortedItems.map((it) => {
        // Normalize count → [0, 1] then interpolate both font size and
        // opacity. When every tag has the same count `span === 1` and the
        // normalized value collapses to 0; bump to 0.5 so the cloud
        // doesn't render as a wall of MIN_PX text.
        const norm = span === 1 ? 0.5 : (it.count - minCount) / span;
        const fontSizePx = MIN_PX + norm * (MAX_PX - MIN_PX);
        const opacity = 0.55 + norm * 0.45;
        // Size bucket classification — handy for theming, tests, and
        // assistive tech that wants to filter the high-signal chips.
        const sizeClass =
          norm >= 0.66
            ? "tag-cloud-chip--lg"
            : norm >= 0.33
              ? "tag-cloud-chip--md"
              : "tag-cloud-chip--sm";
        return (
          <Link
            key={it.tag}
            to={`/feed?tag=${encodeURIComponent(it.tag)}`}
            className={`tag-cloud-chip ${sizeClass} inline-flex items-baseline gap-1 rounded-md border border-accent/30 bg-accent/10 px-2 py-0.5 font-semibold text-accent hover:bg-accent/20 hover:border-accent/60 transition-colors focus:outline-none focus-visible:ring-1 focus-visible:ring-accent`}
            style={{ fontSize: `${fontSizePx.toFixed(1)}px`, opacity }}
            data-testid={`tag-cloud-chip-${it.tag}`}
            title={`${it.tag} · ${it.count.toLocaleString()} record${it.count === 1 ? "" : "s"}`}
          >
            <span>{it.tag}</span>
            <span className="text-[9px] tabular-nums text-[color:var(--fg-dim)] font-normal">
              {it.count.toLocaleString()}
            </span>
          </Link>
        );
      })}
    </div>
  );
}
