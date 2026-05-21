import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { useState } from "react";
import { api, fmtDate, fmtPct } from "@/lib/api";
import { Skeleton, ErrorBox, EmptyState } from "@/components/Skeleton";
import type { MarketQuote } from "@/types/api";

export function SymbolsPage() {
  const top = useQuery({ queryKey: ["top-symbols"], queryFn: () => api.topSymbols(100) });
  const [q, setQ] = useState("");
  const filteredItems = (top.data?.items ?? []).filter((it) =>
    !q || it.symbol.toLowerCase().includes(q.toLowerCase()),
  );
  const quoteSymbols = filteredItems.slice(0, 12).map((it) => it.symbol);
  const quotes = useQuery({
    queryKey: ["market-quotes", quoteSymbols],
    queryFn: () => api.quotes(quoteSymbols),
    enabled: quoteSymbols.length > 0,
  });

  return (
    <div className="grid gap-3">
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
        !top.data || top.data.items.length === 0 ? <EmptyState title="No symbol mentions found" hint="Run a replay with finance-related news first." /> :
        filteredItems.length === 0 ? <EmptyState title="No matching symbol mentions" hint="Clear or change the symbol mention filter." /> : (
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
