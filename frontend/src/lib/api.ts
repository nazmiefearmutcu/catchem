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
  DemoRunResponse, AppInfo, SidecarStatus, LogTail, NewsStatus, NewsPollNowResponse,
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

  // ── Catchem desktop endpoints ──────────────────────────────────────────
  demoPaste: (payload: { title: string; text: string; domain?: string; url?: string }) =>
    request<DemoRunResponse>("/ui/demo/paste", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }),
  demoUpload: (file: File, opts: { title?: string; domain?: string; url?: string } = {}) => {
    const form = new FormData();
    form.append("file", file);
    if (opts.title) form.append("title", opts.title);
    form.append("domain", opts.domain ?? "demo.local");
    if (opts.url) form.append("url", opts.url);
    return request<DemoRunResponse>("/ui/demo/upload", { method: "POST", body: form });
  },
  appInfo: () => request<AppInfo>("/ui/app-info"),
  sidecarStatus: () => request<SidecarStatus>("/ui/sidecar-status"),
  logTail: (lines = 200) => request<LogTail>(`/ui/log-tail?lines=${lines}`),
  newsStatus: () => request<NewsStatus>("/ui/news-status"),
  newsPollNow: () =>
    request<NewsPollNowResponse>("/ui/news-poll-now", { method: "POST" }),
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

/**
 * Relative-time formatter: "12s ago", "5m ago", "3h ago", "2d ago".
 *
 * Behaviour:
 *  - Floors at "just now" for anything < 5s in the past.
 *  - Future timestamps render as "in Xm" (rare — happens when the user
 *    pastes an article with a published_ts ahead of system clock, or
 *    when small clock skew makes a fresh ingest read as +1s).
 *  - Beyond 14 days, falls back to an absolute YYYY-MM-DD date so the
 *    UI doesn't accumulate "364d ago" oddities.
 *
 * The {@link nowMs} parameter is for unit tests; production callers
 * omit it and we read Date.now() per call.
 */
export function fmtRel(iso: string | null | undefined, nowMs: number = Date.now()): string {
  if (!iso) return "";
  const t = Date.parse(iso);
  if (!Number.isFinite(t)) return iso;
  const delta = nowMs - t;
  const absDelta = Math.abs(delta);
  const sign = delta >= 0 ? "ago" : "in";
  const value = (n: number, unit: string) =>
    delta >= 0 ? `${n}${unit} ${sign}` : `${sign} ${n}${unit}`;

  if (absDelta < 5_000) return "just now";
  if (absDelta < 60_000) return value(Math.max(1, Math.floor(absDelta / 1_000)), "s");
  if (absDelta < 3_600_000) return value(Math.floor(absDelta / 60_000), "m");
  if (absDelta < 86_400_000) return value(Math.floor(absDelta / 3_600_000), "h");
  if (absDelta < 14 * 86_400_000) return value(Math.floor(absDelta / 86_400_000), "d");
  // Old item — show the date so the analyst doesn't see "92d ago".
  try {
    const d = new Date(t);
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
  } catch {
    return iso;
  }
}
