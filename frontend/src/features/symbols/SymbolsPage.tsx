import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { useState } from "react";
import { api, fmtDate, fmtPct } from "@/lib/api";
import { freshnessLabel, useTick } from "@/lib/freshness";
import { Skeleton, ErrorBox, EmptyState } from "@/components/Skeleton";
import type { MarketQuote } from "@/types/api";

export function SymbolsPage() {
  // Re-render every 30s so the hero freshness label keeps advancing.
  useTick();
  const top = useQuery({ queryKey: ["top-symbols"], queryFn: () => api.topSymbols(100) });
  const [q, setQ] = useState("");
  const allItems = top.data?.items ?? [];
  const filteredItems = allItems.filter((it) =>
    !q || it.symbol.toLowerCase().includes(q.toLowerCase()),
  );
  const quoteSymbols = filteredItems.slice(0, 12).map((it) => it.symbol);
  const quotes = useQuery({
    queryKey: ["market-quotes", quoteSymbols],
    queryFn: () => api.quotes(quoteSymbols),
    enabled: quoteSymbols.length > 0,
  });

  // Hero stats: top-3 + concentration ratio.
  const totalMentions = allItems.reduce((a, it) => a + it.count, 0);
  const top3 = allItems.slice(0, 3);
  const top3Share = totalMentions
    ? top3.reduce((a, it) => a + it.count, 0) / totalMentions
    : 0;
  const heroHeadline =
    allItems.length === 0
      ? "No symbol mentions yet"
      : top3.length === 1 || top3Share >= 0.7
        ? `${top3[0]?.symbol ?? "—"} dominates news flow`
        : `${allItems.length} distinct symbols in news flow`;

  return (
    <div className="grid gap-5">
      {/* Hero: top-3 ranked tickers + concentration ratio. */}
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
                Symbols · news mention frequency
              </div>
              <h1 className="text-lg font-semibold mt-0.5 tracking-tight">
                {heroHeadline}
              </h1>
              <div className="mt-1 text-[11px] text-[color:var(--fg-muted)]">
                {totalMentions.toLocaleString()} total mention{totalMentions === 1 ? "" : "s"} · top 3 = {fmtPct(top3Share, 0)} of flow
                <span className="text-[10px] text-[color:var(--fg-dim)]"> · {freshnessLabel(top.dataUpdatedAt)}</span>
              </div>
            </div>
          </div>
        </div>
        {top3.length > 0 && (
          <div className="relative grid gap-2 grid-cols-3 text-[11px]">
            {top3.map((it, idx) => (
              <Link
                key={it.symbol}
                to={`/symbols/${encodeURIComponent(it.symbol)}`}
                className="group rounded-md border border-[color:var(--border-subtle)] bg-[color:var(--bg-elev2)]/40 px-3 py-2 hover:border-accent/60 hover:bg-[color:var(--bg-elev2)]/70 transition-colors"
              >
                <div className="flex items-baseline justify-between gap-2">
                  <div className="text-[9px] uppercase tracking-wider text-[color:var(--fg-muted)]">
                    #{idx + 1}
                  </div>
                  <div className="text-[10px] tabular-nums text-[color:var(--fg-dim)]">
                    {totalMentions ? fmtPct(it.count / totalMentions, 0) : ""}
                  </div>
                </div>
                <div className="mt-0.5 text-xl font-semibold tabular-nums text-good group-hover:text-accent transition-colors">
                  {it.symbol}
                </div>
                <div className="text-[10px] tabular-nums text-[color:var(--fg-dim)]">
                  {it.count.toLocaleString()} mention{it.count === 1 ? "" : "s"}
                </div>
              </Link>
            ))}
          </div>
        )}
      </section>

      <div className="card">
        <label htmlFor="symq" className="label">filter symbol mentions</label>
        <input
          id="symq"
          className="input w-full mt-1"
          placeholder="AAPL mentions, BTC-USD news, ^GSPC mentions..."
          value={q}
          onChange={(e) => setQ(e.target.value)}
        />
      </div>
      {top.isLoading ? <Skeleton className="h-72" /> :
        top.error ? <ErrorBox err={top.error} /> :
        !top.data || top.data.items.length === 0 ? <EmptyState title="No symbol mentions found" hint="Run a replay with finance-related news first." action={<Link to="/replay" className="btn">Open Replay/Upload</Link>} /> :
        filteredItems.length === 0 ? <EmptyState title="No matching symbol mentions" hint="Clear or change the symbol mention filter." action={<button type="button" className="btn" onClick={() => setQ("")}>Clear filter</button>} /> : (
          <>
            <section className="card" aria-label="Market quote context">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div>
                  <h2 className="text-sm font-semibold">Market quote context</h2>
                  <p className="mt-1 text-xs text-[color:var(--fg-dim)]">
                    Separate from symbol mentions in news analysis; fixture snapshots are not current prices.
                  </p>
                </div>
                {quotes.data && (
                  <span className="chip text-[color:var(--fg-dim)]" title={fmtDate(quotes.data.generated_at)}>
                    provider {quotes.data.provider}
                  </span>
                )}
              </div>
              <QuoteContent
                isLoading={quotes.isLoading}
                error={quotes.error}
                quotes={quotes.data?.items ?? []}
              />
            </section>

            <section aria-label="Symbol mention counts">
              <div className="mb-2 flex items-center justify-between gap-2">
                <h2 className="label">symbol mention counts</h2>
                <span className="text-[10px] text-[color:var(--fg-dim)]">news-analysis context</span>
              </div>
              <ul className="grid gap-1 grid-cols-2 md:grid-cols-3 lg:grid-cols-4">
                {filteredItems.map((it) => (
                    <li key={it.symbol}>
                      <Link
                        to={`/symbols/${encodeURIComponent(it.symbol)}`}
                        className="card flex items-center justify-between hover:bg-[color:var(--bg-elev2)] transition-colors"
                      >
                        <span className="font-semibold text-good">{it.symbol}</span>
                        <span className="text-[10px] text-[color:var(--fg-dim)]">{it.count} mentions</span>
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

function QuoteContent({
  isLoading,
  error,
  quotes,
}: {
  isLoading: boolean;
  error: unknown;
  quotes: MarketQuote[];
}) {
  if (isLoading) return <Skeleton className="mt-3 h-24" />;
  if (error) return <div className="mt-3"><ErrorBox err={error} /></div>;
  if (quotes.length === 0) {
    return <p className="mt-3 text-xs text-[color:var(--fg-dim)]">No quote contract rows for this symbol set.</p>;
  }

  // The local_fixture provider returns "unavailable" for every symbol it
  // doesn't have on disk. On a fresh install that's literally every row
  // → a scroll-wall of red. Collapse the all-unavailable case into one
  // friendly explainer + a compact list of the symbols we tried so the
  // analyst still knows which ticker mentions are in play.
  const unavailableCount = quotes.filter(
    (q) => q.freshness_status === "unavailable" || q.last == null,
  ).length;
  const allUnavailable = unavailableCount === quotes.length;
  const provider = quotes[0]?.provider ?? "local_fixture";

  if (allUnavailable) {
    return (
      <div className="mt-3 rounded-md border border-warn/30 bg-warn/5 p-3">
        <div className="flex items-baseline gap-2">
          <span className="chip text-warn">quote unavailable</span>
          <span className="text-[11px] text-[color:var(--fg-dim)]">
            provider <code className="font-mono">{provider}</code> has no fixture for any of the top {quotes.length} mention{quotes.length === 1 ? "" : "s"}
          </span>
        </div>
        <p className="mt-2 text-[11px] text-[color:var(--fg-muted)]">
          Symbol mentions are extracted from news text — they don't require a quote feed.
          To attach prices, configure a market data provider in Settings → quote provider.
        </p>
        <ul className="mt-2 grid gap-1 sm:grid-cols-2 md:grid-cols-3">
          {quotes.map((q) => (
            <li
              key={q.symbol}
              className="flex items-baseline justify-between gap-2 rounded border border-[color:var(--border-subtle)] bg-[color:var(--bg-elev2)]/40 px-2 py-1 text-[11px]"
            >
              <span className="font-semibold text-good">{q.symbol}</span>
              <code
                className="font-mono text-[10px] text-[color:var(--fg-dim)]"
                title={`provider ${q.provider}`}
              >
                {q.error_code ?? "quote_unavailable"}
              </code>
            </li>
          ))}
        </ul>
      </div>
    );
  }

  return (
    <ul className="mt-3 divide-y divide-[color:var(--border)]">
      {quotes.map((quote) => (
        <li key={quote.symbol} className="grid gap-2 py-2 md:grid-cols-[minmax(80px,0.8fr)_minmax(160px,1.4fr)_minmax(180px,2fr)] md:items-center">
          <div>
            <div className="font-semibold text-good">{quote.symbol}</div>
            <div className="text-[10px] uppercase tracking-wider text-[color:var(--fg-dim)]">
              {quote.provider}
            </div>
          </div>
          <QuoteValue quote={quote} />
          <QuoteFreshness quote={quote} />
        </li>
      ))}
    </ul>
  );
}

function QuoteValue({ quote }: { quote: MarketQuote }) {
  if (quote.freshness_status === "unavailable" || quote.last == null) {
    return (
      <div>
        <div className="text-sm font-semibold text-warn">quote unavailable</div>
        <div className="text-[10px] text-[color:var(--fg-dim)]">
          {quote.error_code ?? "no market quote in contract"}
        </div>
      </div>
    );
  }

  return (
    <div>
      <div className="text-sm font-semibold">
        fixture last {quote.last.toLocaleString(undefined, { maximumFractionDigits: 2 })} {quote.currency ?? ""}
      </div>
      <div className="text-[10px] text-[color:var(--fg-dim)]">
        change {formatSigned(quote.change_abs)} ({fmtPct(quote.change_pct, 2)})
      </div>
    </div>
  );
}

function QuoteFreshness({ quote }: { quote: MarketQuote }) {
  if (quote.freshness_status === "unavailable") {
    return (
      <div className="text-xs text-[color:var(--fg-dim)]">
        <span className="chip text-warn">unavailable</span>
        <span className="ml-2">retrieved {fmtDate(quote.retrieved_at)}</span>
      </div>
    );
  }

  return (
    <div className="text-xs text-[color:var(--fg-dim)]">
      <span className="chip text-warn">stale local fixture</span>
      <span className="ml-2">as of {fmtDate(quote.as_of)}</span>
    </div>
  );
}

function formatSigned(n: number | null | undefined): string {
  if (n == null || !Number.isFinite(n)) return "-";
  const sign = n > 0 ? "+" : "";
  return `${sign}${n.toFixed(2)}`;
}
