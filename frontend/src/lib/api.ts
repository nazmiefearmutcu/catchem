// Typed API client. Single fetch wrapper, predictable error shape, abort-able.

const BASE = ""; // same-origin

export class ApiError extends Error {
  constructor(public status: number, public url: string, message: string) {
    super(message);
    this.name = "ApiError";
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    ...init,
    headers: { Accept: "application/json", ...(init?.headers ?? {}) },
  });
  if (!res.ok) {
    const body = await res.text().catch(() => "");
    throw new ApiError(res.status, path, `${path} → ${res.status} ${body.slice(0, 200)}`);
  }
  return (await res.json()) as T;
}

import type {
  UISummary, UIFacets, UITimeline, UITrends, UIMatrix, UIBenchmark, UISymbol,
  UIConfig, UIMetrics, FinancialRecord, GuardSnapshot,
} from "@/types/api";

export const api = {
  config: () => request<UIConfig>("/config"),
  metrics: () => request<UIMetrics>("/metrics"),
  guards: () => request<GuardSnapshot>("/ui/guards"),
  summary: () => request<UISummary>("/ui/summary"),
  facets: (limit = 500) => request<UIFacets>(`/ui/facets?limit=${limit}`),
  timeline: (bucketMinutes = 60, limit = 500) =>
    request<UITimeline>(`/ui/timeline?bucket_minutes=${bucketMinutes}&limit=${limit}`),
  trends: (limit = 500) => request<UITrends>(`/ui/trends?limit=${limit}`),
  matrix: () => request<UIMatrix>("/ui/matrix"),
  topSymbols: (limit = 20) => request<{ items: { symbol: string; count: number }[] }>(`/ui/top-symbols?limit=${limit}`),
  topReasons: (limit = 20) => request<{ items: { reason: string; count: number }[] }>(`/ui/top-reasons?limit=${limit}`),
  benchmarkLatest: () => request<UIBenchmark>("/ui/benchmark/latest"),
  benchmarkHistory: () => request<{ history: UIBenchmark[] }>("/ui/benchmark/history"),
  symbol: (sym: string, limit = 50) =>
    request<UISymbol>(`/ui/symbol/${encodeURIComponent(sym)}?limit=${limit}`),

  recent: (limit = 50, relevantOnly = true) =>
    request<{ items: FinancialRecord[] }>(`/recent?limit=${limit}&relevant_only=${relevantOnly}`),
  record: (id: string) => request<FinancialRecord>(`/record/${encodeURIComponent(id)}`),
  bySymbol: (sym: string, limit = 50) =>
    request<{ items: FinancialRecord[] }>(`/records/by-symbol/${encodeURIComponent(sym)}?limit=${limit}`),
  byAssetClass: (ac: string, limit = 50) =>
    request<{ items: FinancialRecord[] }>(`/records/by-asset-class/${encodeURIComponent(ac)}?limit=${limit}`),
  byReason: (rc: string, limit = 50) =>
    request<{ items: FinancialRecord[] }>(`/records/by-reason/${encodeURIComponent(rc)}?limit=${limit}`),
};

// Safe URL filter for outbound links. Blocks javascript:/data:/file: schemes.
export function safeHref(url: string | null | undefined): string | undefined {
  if (typeof url !== "string") return undefined;
  try {
    const u = new URL(url, "http://localhost");
    if (u.protocol === "http:" || u.protocol === "https:") return u.toString();
  } catch {
    /* fall through */
  }
  return undefined;
}

// Format helpers
export function fmtPct(n: number | null | undefined, digits = 0): string {
  if (n == null || !isFinite(n)) return "—";
  return `${(n * 100).toFixed(digits)}%`;
}

export function fmtScore(n: number | null | undefined): string {
  if (n == null) return "—";
  return n.toFixed(2);
}

export function fmtDate(iso: string | null | undefined): string {
  if (!iso) return "";
  try {
    return new Date(iso).toLocaleString(undefined, {
      year: "numeric", month: "2-digit", day: "2-digit",
      hour: "2-digit", minute: "2-digit",
    });
  } catch {
    return iso;
  }
}
