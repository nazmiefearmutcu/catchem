import { useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, fmtPct, fmtRel, safeHref } from "@/lib/api";
import { freshnessLabel, useTick } from "@/lib/freshness";
import { Skeleton, ErrorBox, EmptyState } from "@/components/Skeleton";
import { Pill } from "@/components/Pill";
import type { PortfolioEnrichedHolding } from "@/types/api";

// Query keys are shared between the enriched poll and the bare list so a
// mutation (add / remove) invalidates BOTH — the table re-fetches enriched
// rows and any list-only consumer stays in sync.
const ENRICHED_KEY = ["portfolio-enriched"];
const LIST_KEY = ["portfolio-list"];

/**
 * Portfolio — a READ-ONLY holdings tracker. Each holding the analyst adds is
 * enriched by the awareness layer: live quote, news-coverage status, recent
 * headline count + top link, sentiment, and (the headline feature) a
 * blind-spot flag when nothing referencing the symbol has arrived recently.
 *
 * There is deliberately NO trade/order surface anywhere — adding a holding is
 * a watchlist action, not a position. `shares` / `cost_basis` are optional
 * bookkeeping fields the analyst fills in for context only.
 */
export function PortfolioPage() {
  // Re-render every 30s so the hero freshness suffix + relative ages tick.
  useTick();
  const qc = useQueryClient();

  const enriched = useQuery({
    queryKey: ENRICHED_KEY,
    queryFn: api.portfolioEnriched,
    // Live-ish: re-fetch quote + coverage every 30s while the tab is open.
    refetchInterval: 30_000,
  });

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ENRICHED_KEY });
    qc.invalidateQueries({ queryKey: LIST_KEY });
  };

  const addMutation = useMutation({
    mutationFn: api.portfolioAdd,
    onSuccess: invalidate,
  });
  const removeMutation = useMutation({
    mutationFn: (id: number) => api.portfolioDelete(id),
    onSuccess: invalidate,
  });

  const holdings = enriched.data?.holdings ?? [];

  // Hero stats — count, blind-spot tally, total recent-news volume.
  const total = holdings.length;
  const blindSpots = holdings.filter((h) => !h.coverage.covered).length;
  const covered = total - blindSpots;
  const totalNews = holdings.reduce((a, h) => a + (h.recent_news_count ?? 0), 0);
  const coverageRatio = total ? covered / total : 0;

  const heroHeadline =
    total === 0
      ? "No holdings tracked yet"
      : blindSpots > 0
        ? `${blindSpots} of ${total} holding${total === 1 ? "" : "s"} in a blind spot`
        : `All ${total} holding${total === 1 ? "" : "s"} covered`;
  const heroTone: "good" | "warn" = blindSpots > 0 ? "warn" : "good";
  const dotAccent = heroTone === "good" ? "bg-good" : "bg-warn";
  const eyebrowAccent = heroTone === "good" ? "text-good" : "text-warn";

  return (
    <div className="grid gap-5">
      {/* Hero: coverage synthesis + at-a-glance KPI tiles. */}
      <section className="relative overflow-hidden rounded-xl border border-accent/40 hero-gradient p-6">
        <div
          aria-hidden
          className="pointer-events-none absolute -top-20 -left-20 h-48 w-48 rounded-full bg-accent/20 blur-3xl"
        />
        <div className="relative flex flex-wrap items-start justify-between gap-3 mb-3">
          <div className="flex items-center gap-3">
            <span className="relative flex h-2 w-2">
              <span className={`absolute inline-flex h-full w-full animate-ping rounded-full ${dotAccent} opacity-75`} />
              <span className={`relative inline-flex h-2 w-2 rounded-full ${dotAccent}`} />
            </span>
            <div>
              <div className={`text-[10px] uppercase tracking-[0.25em] ${eyebrowAccent} font-semibold`}>
                Portfolio · awareness coverage · read-only
              </div>
              <h1 className="text-lg font-semibold mt-0.5 tracking-tight" data-testid="portfolio-hero-headline">
                {heroHeadline}
              </h1>
              <div className="mt-1 text-[11px] text-[color:var(--fg-muted)]">
                {covered} covered · {blindSpots} blind spot{blindSpots === 1 ? "" : "s"} · {totalNews} recent headline{totalNews === 1 ? "" : "s"}
                <span className="text-[10px] text-[color:var(--fg-dim)]"> · {freshnessLabel(enriched.dataUpdatedAt)}</span>
              </div>
            </div>
          </div>
        </div>
        {total > 0 && (
          <div className="relative grid gap-2 grid-cols-3 text-[11px]">
            <HeroTile label="Holdings" value={total.toLocaleString()} />
            <HeroTile
              label="Covered"
              value={fmtPct(coverageRatio, 0)}
              tone={blindSpots > 0 ? "warn" : "good"}
            />
            <HeroTile label="Recent news" value={totalNews.toLocaleString()} />
          </div>
        )}
      </section>

      {/* Add-holding form — symbol required; shares / label optional. */}
      <AddHoldingForm
        onAdd={(body) => addMutation.mutate(body)}
        pending={addMutation.isPending}
        error={addMutation.error}
      />

      {/* Holdings table / states. */}
      {enriched.isLoading ? (
        <Skeleton className="h-72" />
      ) : enriched.error ? (
        <ErrorBox err={enriched.error} />
      ) : total === 0 ? (
        <EmptyState
          title="No holdings tracked yet"
          hint="Add a symbol above to start tracking its quote, news coverage, and blind-spot status. This is watch-only — Catchem never trades."
        />
      ) : (
        <section className="card overflow-x-auto" aria-label="Tracked holdings">
          <table className="w-full text-xs">
            <thead>
              <tr className="text-left text-[10px] uppercase tracking-wider text-[color:var(--fg-dim)]">
                <th className="py-2 pr-3 font-medium">Symbol</th>
                <th className="py-2 pr-3 font-medium">Quote</th>
                <th className="py-2 pr-3 font-medium">News</th>
                <th className="py-2 pr-3 font-medium">Sentiment</th>
                <th className="py-2 pr-3 font-medium">Top headline</th>
                <th className="py-2 pr-1 font-medium text-right">Remove</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-[color:var(--border)]">
              {holdings.map((h) => (
                <HoldingRow
                  key={h.id}
                  holding={h}
                  onRemove={() => removeMutation.mutate(h.id)}
                  removing={removeMutation.isPending && removeMutation.variables === h.id}
                />
              ))}
            </tbody>
          </table>
        </section>
      )}
    </div>
  );
}

function HeroTile({
  label,
  value,
  tone = "default",
}: {
  label: string;
  value: string;
  tone?: "default" | "good" | "warn";
}) {
  const valueCls =
    tone === "good" ? "text-good" : tone === "warn" ? "text-warn" : "text-[color:var(--fg)]";
  return (
    <div className="rounded-md border border-[color:var(--border-subtle)] bg-[color:var(--bg-elev2)]/40 px-3 py-2">
      <div className="text-[9px] uppercase tracking-wider text-[color:var(--fg-muted)]">{label}</div>
      <div className={`mt-0.5 text-xl font-semibold tabular-nums ${valueCls}`}>{value}</div>
    </div>
  );
}

function HoldingRow({
  holding,
  onRemove,
  removing,
}: {
  holding: PortfolioEnrichedHolding;
  onRemove: () => void;
  removing: boolean;
}) {
  const top = holding.recent_top?.[0];
  const topHref = safeHref(top?.url);
  return (
    <tr className="align-top">
      {/* Symbol + label + blind-spot badge */}
      <td className="py-2.5 pr-3">
        <div className="flex items-center gap-2">
          <span className="font-semibold text-good">{holding.symbol}</span>
          {!holding.coverage.covered && (
            <Pill variant="bad" title="No recent news references this symbol — you could miss breaking coverage">
              blind spot
            </Pill>
          )}
        </div>
        {holding.label && (
          <div className="mt-0.5 text-[10px] text-[color:var(--fg-dim)]">{holding.label}</div>
        )}
        {holding.shares != null && (
          <div className="text-[10px] text-[color:var(--fg-muted)] tabular-nums">
            {holding.shares.toLocaleString()} sh
            {holding.cost_basis != null
              ? ` @ ${holding.cost_basis.toLocaleString(undefined, { maximumFractionDigits: 2 })}`
              : ""}
          </div>
        )}
      </td>

      {/* Quote: last + colored change_pct */}
      <td className="py-2.5 pr-3">
        <QuoteCell holding={holding} />
      </td>

      {/* Recent-news count + freshest mention age */}
      <td className="py-2.5 pr-3">
        <div className="tabular-nums font-semibold">{holding.recent_news_count ?? 0}</div>
        {holding.coverage.mention_count > 0 && holding.coverage.last_seen_age_seconds != null && (
          <div className="text-[10px] text-[color:var(--fg-dim)]">
            seen {fmtRel(ageToIso(holding.coverage.last_seen_age_seconds))}
          </div>
        )}
      </td>

      {/* Sentiment chip */}
      <td className="py-2.5 pr-3">
        <SentimentChip label={holding.sentiment_label} />
      </td>

      {/* Top headline link */}
      <td className="py-2.5 pr-3 max-w-[280px]">
        {top ? (
          topHref ? (
            <a
              href={topHref}
              target="_blank"
              rel="noreferrer noopener"
              className="text-[color:var(--fg)] hover:text-accent transition-colors line-clamp-2"
              title={top.title}
            >
              {top.title}
            </a>
          ) : (
            <span className="text-[color:var(--fg-dim)] line-clamp-2" title={top.title}>
              {top.title}
            </span>
          )
        ) : (
          <span className="text-[10px] text-[color:var(--fg-muted)]">—</span>
        )}
      </td>

      {/* Remove button */}
      <td className="py-2.5 pr-1 text-right">
        <button
          type="button"
          onClick={onRemove}
          disabled={removing}
          aria-label={`Remove ${holding.symbol}`}
          title={`Remove ${holding.symbol}`}
          className="btn px-2 py-1 text-[color:var(--fg-dim)] hover:text-bad disabled:opacity-50"
        >
          ×
        </button>
      </td>
    </tr>
  );
}

function QuoteCell({ holding }: { holding: PortfolioEnrichedHolding }) {
  const q = holding.quote;
  if (!q || q.last == null) {
    return (
      <div>
        <div className="text-[color:var(--fg-dim)]">no quote</div>
        <div className="text-[10px] text-[color:var(--fg-muted)]">no market data</div>
      </div>
    );
  }
  const pct = q.change_pct;
  const pctCls =
    pct == null || !Number.isFinite(pct)
      ? "text-[color:var(--fg-dim)]"
      : pct > 0
        ? "text-good"
        : pct < 0
          ? "text-bad"
          : "text-[color:var(--fg-dim)]";
  return (
    <div>
      <div className="font-semibold tabular-nums">
        {q.last.toLocaleString(undefined, { maximumFractionDigits: 2 })}
      </div>
      <div className={`text-[10px] tabular-nums ${pctCls}`} data-testid="quote-change">
        {pct == null || !Number.isFinite(pct) ? "—" : `${pct > 0 ? "+" : ""}${fmtPct(pct, 2)}`}
      </div>
    </div>
  );
}

function SentimentChip({
  label,
}: {
  label?: "positive" | "negative" | "neutral" | "unknown" | null;
}) {
  if (!label || label === "unknown") {
    return <span className="text-[10px] text-[color:var(--fg-muted)]">—</span>;
  }
  const variant = label === "positive" ? "good" : label === "negative" ? "bad" : "warn";
  return <Pill variant={variant}>{label}</Pill>;
}

function AddHoldingForm({
  onAdd,
  pending,
  error,
}: {
  onAdd: (body: { symbol: string; shares?: number | null; label?: string | null }) => void;
  pending: boolean;
  error: unknown;
}) {
  const [symbol, setSymbol] = useState("");
  const [shares, setShares] = useState("");
  const [label, setLabel] = useState("");

  const submit = (e: FormEvent) => {
    e.preventDefault();
    const sym = symbol.trim().toUpperCase();
    if (!sym) return;
    const sharesNum = shares.trim() === "" ? null : Number(shares);
    onAdd({
      symbol: sym,
      shares: sharesNum != null && Number.isFinite(sharesNum) ? sharesNum : null,
      label: label.trim() || null,
    });
    setSymbol("");
    setShares("");
    setLabel("");
  };

  return (
    <form className="card grid gap-2 sm:grid-cols-[1fr_120px_1fr_auto] sm:items-end" onSubmit={submit}>
      <div>
        <label htmlFor="pf-symbol" className="label">symbol</label>
        <input
          id="pf-symbol"
          className="input w-full mt-1"
          placeholder="AAPL, BTC-USD, ^GSPC…"
          value={symbol}
          onChange={(e) => setSymbol(e.target.value)}
        />
      </div>
      <div>
        <label htmlFor="pf-shares" className="label">shares (optional)</label>
        <input
          id="pf-shares"
          className="input w-full mt-1"
          inputMode="decimal"
          placeholder="100"
          value={shares}
          onChange={(e) => setShares(e.target.value)}
        />
      </div>
      <div>
        <label htmlFor="pf-label" className="label">label (optional)</label>
        <input
          id="pf-label"
          className="input w-full mt-1"
          placeholder="Core tech"
          value={label}
          onChange={(e) => setLabel(e.target.value)}
        />
      </div>
      <button type="submit" className="btn" disabled={pending || !symbol.trim()}>
        {pending ? "Adding…" : "Add holding"}
      </button>
      {error ? (
        <div className="sm:col-span-4">
          <ErrorBox err={error} />
        </div>
      ) : null}
    </form>
  );
}

// The coverage age arrives as seconds-since-freshest-mention; fmtRel speaks
// ISO timestamps, so convert back to an absolute instant relative to now.
function ageToIso(ageSeconds: number): string {
  return new Date(Date.now() - ageSeconds * 1000).toISOString();
}
