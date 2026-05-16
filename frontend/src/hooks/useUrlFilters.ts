import { useCallback, useMemo } from "react";
import { useSearchParams } from "react-router-dom";

/**
 * URL-state for the live feed. Keeps shareable links and back-button working.
 */
export interface Filters {
  ac?: string | null;       // asset_class
  rc?: string | null;       // reason_code
  sym?: string | null;      // symbol
  q?: string | null;        // search query
  sentiment?: string | null;
  relevant?: "all" | "only" | null;
  diagnosticOnly?: "1" | null;
}

export function useUrlFilters() {
  const [params, setParams] = useSearchParams();
  const filters: Filters = useMemo(
    () => ({
      ac: params.get("ac"),
      rc: params.get("rc"),
      sym: params.get("sym"),
      q: params.get("q"),
      sentiment: params.get("sentiment"),
      relevant: (params.get("relevant") as Filters["relevant"]) ?? "only",
      diagnosticOnly: (params.get("diagnosticOnly") as Filters["diagnosticOnly"]) ?? null,
    }),
    [params]
  );

  const setFilter = useCallback(
    (k: keyof Filters, v: string | null | undefined) => {
      const next = new URLSearchParams(params);
      if (!v) next.delete(k);
      else next.set(k, String(v));
      setParams(next, { replace: true });
    },
    [params, setParams]
  );

  const clear = useCallback(() => setParams({}, { replace: true }), [setParams]);

  return { filters, setFilter, clear };
}
