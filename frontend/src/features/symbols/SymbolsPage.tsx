import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { useState } from "react";
import { api } from "@/lib/api";
import { Skeleton, ErrorBox, EmptyState } from "@/components/Skeleton";

export function SymbolsPage() {
  const top = useQuery({ queryKey: ["top-symbols"], queryFn: () => api.topSymbols(100) });
  const [q, setQ] = useState("");
  const filteredItems = (top.data?.items ?? []).filter((it) =>
    !q || it.symbol.toLowerCase().includes(q.toLowerCase()),
  );

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
          <ul className="grid gap-1 grid-cols-2 md:grid-cols-3 lg:grid-cols-4">
            {filteredItems.map((it) => (
                <li key={it.symbol}>
                  <Link
                    to={`/symbols/${encodeURIComponent(it.symbol)}`}
                    className="card flex items-center justify-between hover:bg-[color:var(--bg-elev2)] transition-colors"
                  >
                    <span className="font-semibold text-good">{it.symbol}</span>
                    <span className="text-[10px] text-[color:var(--fg-dim)]">{it.count}</span>
                  </Link>
                </li>
              ))}
          </ul>
        )}
    </div>
  );
}
