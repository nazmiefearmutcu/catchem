// Typed API client. Single fetch wrapper, predictable error shape, abort-able.

const BASE = ""; // same-origin

export class ApiError extends Error {
  constructor(
    public status: number,
    public url: string,
    message: string,
    public details?: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

type CancellableSignal = {
  signal: AbortSignal;
  clear: () => void;
  abortedByCaller: boolean;
};

type ApiRequestInit = RequestInit & {
  timeoutMs?: number;
  retries?: number;
  retryDelayMs?: number;
};

const DEFAULT_TIMEOUT_MS = 10_000;
const DEFAULT_RETRIES = 2;
const BASE_RETRY_DELAY_MS = 300;
const MAX_RETRY_DELAY_MS = 2_500;
const MAX_ERROR_TEXT = 500;

const inFlight = new Map<string, Promise<unknown>>();

function timeoutSignal(ms: number): CancellableSignal {
  const timeoutFn = (AbortSignal as { timeout?: (ms: number) => AbortSignal }).timeout;
  if (typeof timeoutFn === "function") {
    return {
      signal: timeoutFn.call(AbortSignal, ms),
      clear: () => undefined,
      abortedByCaller: false,
    };
  }

  const controller = new AbortController();
  const timeoutId = setTimeout(
    () => controller.abort(new DOMException(`Request timeout ${ms}ms`, "TimeoutError")),
    ms,
  );
  return {
    signal: controller.signal,
    clear: () => clearTimeout(timeoutId),
    abortedByCaller: false,
  };
}

function composeAbortSignal(ms: number, callerSignal?: AbortSignal): CancellableSignal {
  const base = timeoutSignal(ms);
  if (!callerSignal) return base;
  const controller = new AbortController();
  let abortedByCaller = false;

  const onCallerAbort = () => {
    abortedByCaller = true;
    controller.abort(callerSignal.reason);
  };
  const onTimeoutAbort = () => {
    controller.abort(base.signal.reason);
  };

  const nativeAny = (AbortSignal as { any?: (signals: AbortSignal[]) => AbortSignal }).any;
  if (typeof nativeAny === "function") {
    callerSignal.addEventListener("abort", onCallerAbort, { once: true });
    return {
      signal: nativeAny.call(AbortSignal, [callerSignal, base.signal]),
      clear: () => {
        callerSignal.removeEventListener("abort", onCallerAbort);
        base.clear();
      },
      get abortedByCaller() {
        return abortedByCaller;
      },
    };
  }

  callerSignal.addEventListener("abort", onCallerAbort, { once: true });
  base.signal.addEventListener("abort", onTimeoutAbort, { once: true });

  if (callerSignal.aborted) onCallerAbort();
  if (base.signal.aborted) onTimeoutAbort();

  return {
    signal: controller.signal,
    clear: () => {
      callerSignal.removeEventListener("abort", onCallerAbort);
      base.signal.removeEventListener("abort", onTimeoutAbort);
      base.clear();
    },
    get abortedByCaller() {
      return abortedByCaller;
    },
  };
}

function isRetryableStatus(status: number): boolean {
  return status >= 500 || status === 408 || status === 425 || status === 429;
}

function getRequestKey(method: string, path: string): string {
  return `${method.toUpperCase()} ${path}`;
}

async function parseBody(res: Response): Promise<unknown> {
  const raw = await res.text().catch(() => "");
  const trimmed = raw.trim();
  if (!trimmed) return null;
  if (trimmed.startsWith("{") || trimmed.startsWith("[") || trimmed.startsWith('"')) {
    try {
      return JSON.parse(trimmed);
    } catch {
      // fallthrough below
    }
  }

  const ct = res.headers.get("content-type") ?? "";
  if (ct.includes("application/json")) {
    try {
      return JSON.parse(trimmed);
    } catch {
      // keep raw text for message context
    }
  }
  return trimmed;
}

function isAbortError(error: unknown): error is DOMException {
  return error instanceof DOMException && error.name === "AbortError";
}

async function request<T>(
  path: string,
  init: ApiRequestInit = {},
): Promise<T> {
  const method = (init.method ?? "GET").toUpperCase();
  const timeoutMs = init.timeoutMs ?? DEFAULT_TIMEOUT_MS;
  const retries = Math.max(0, init.retries ?? DEFAULT_RETRIES);
  const retryDelayMs = init.retryDelayMs ?? BASE_RETRY_DELAY_MS;
  const headers = { Accept: "application/json", ...(init.headers ?? {}) };
  // Normalize the caller-provided signal: `RequestInit.signal` is typed
  // `AbortSignal | null | undefined`, but our `composeAbortSignal` only
  // accepts `AbortSignal | undefined`. Coerce null → undefined here so
  // the rest of the file stays free of `?? undefined` noise. (TS2345
  // from the strict null-narrowing path in TS 5.3+.)
  const requestSignal: AbortSignal | undefined = init.signal ?? undefined;
  // In-flight GET de-dup: concurrent callers for the same key share ONE
  // promise. That sharing is only safe when no caller can abort it — a
  // caller-supplied AbortSignal would otherwise let caller A's abort reject
  // caller B's (signal-less) request with A's AbortError. So only de-dup
  // signal-free GETs; any request carrying a caller signal runs standalone
  // with its own composed signal.
  const key =
    method === "GET" && !requestSignal ? getRequestKey(method, path) : null;

  if (key) {
    const existing = inFlight.get(key);
    if (existing) return existing as Promise<T>;
  }

  const run = (async () => {
    for (let attempt = 0; attempt <= retries; attempt += 1) {
      const signalState = composeAbortSignal(timeoutMs, requestSignal);
      try {
        let res: Response;
        try {
          res = await fetch(`${BASE}${path}`, {
            ...init,
            method,
            headers,
            signal: signalState.signal,
          });
        } catch (signalErr) {
          // Defensive degradation: some runtimes reject a cross-realm
          // AbortSignal with a synchronous TypeError *before* issuing the
          // request — notably the jsdom + undici test realm, where
          // AbortSignal.timeout() yields a signal that node's fetch does
          // not recognise ("Expected signal to be an instance of
          // AbortSignal"). Rather than fail an otherwise-valid request,
          // retry once without the signal. Real browsers share a single
          // realm and never take this branch, so production keeps full
          // timeout/abort behavior; only the degenerate realm loses it.
          if (
            signalErr instanceof TypeError &&
            /signal/i.test(String((signalErr as Error).message))
          ) {
            res = await fetch(`${BASE}${path}`, { ...init, method, headers });
          } else {
            throw signalErr;
          }
        }

        if (!res.ok) {
          const body = await parseBody(res);
          const details = typeof body === "string" ? body : JSON.stringify(body);
          throw new ApiError(
            res.status,
            path,
            `${path} → ${res.status} ${res.statusText}`,
            typeof details === "string" ? details.slice(0, MAX_ERROR_TEXT) : undefined,
          );
        }

        const parsed = await parseBody(res);
        return (parsed ?? null) as T;
      } catch (error) {
        if (signalState.abortedByCaller || isAbortError(error)) {
          throw error;
        }
        const status = error instanceof ApiError ? error.status : undefined;
        const retryable =
          attempt < retries &&
          (status === undefined || status === 0 || isRetryableStatus(status));

        if (!retryable) {
          throw error;
        }

        const delay = Math.min(
          retryDelayMs * 2 ** attempt + Math.random() * 500,
          MAX_RETRY_DELAY_MS,
        );
        await new Promise((r) => setTimeout(r, delay));
      } finally {
        signalState.clear();
      }
    }

    throw new ApiError(0, path, `Request retries exhausted: ${path}`);
  })();

  if (key) {
    inFlight.set(key, run);
    run.finally(() => {
      inFlight.delete(key);
    });
  }

  return run as Promise<T>;
}

import type {
  UISummary, UIFacets, UITimeline, UITrends, UIMatrix, UIBenchmark, UIBacktest, UISymbol,
  UIConfig, UIMetrics, FinancialRecord, GuardSnapshot, MarketQuote, MarketQuoteBatchResponse,
  DemoRunResponse, AppInfo, SidecarStatus, LogTail, NewsStatus, NewsPollNowResponse,
  NewsSourcesResponse, NewsAwareness,
  ArchiveStatus, ArchiveNowResponse, ReplayRunResponse,
  SymbolSentimentTrend,
} from "@/types/api";

export const api = {
  config: () => request<UIConfig>("/config"),
  metrics: () => request<UIMetrics>("/metrics"),
  stats: () =>
    request<{
      schema_version: number;
      generated_at: string;
      uptime_seconds: number;
      total_requests: number;
      request_counts: Record<string, number>;
      db: { records: number; reviews: number; dlq: number };
      reviewers: { deepseek_usd_spent: number; stub_active: boolean };
      process: {
        rss_mb: number;
        vms_mb: number;
        cpu_percent: number;
        num_threads: number;
        psutil_available: boolean;
      };
      version?: string | null;
    }>("/api/stats"),
  /** Deep health probe — suitable for readiness checks.
   *
   * The sidecar returns 200 with ``ok:true`` when every subsystem is
   * healthy, and 503 with ``ok:false`` plus a populated ``issues`` list
   * when something is wrong. The 503 case still produces a parseable
   * JSON body (``request()`` throws ApiError on non-2xx, so the caller
   * needs to handle the failure by treating an ApiError as "not ok"
   * rather than expecting the body inline). */
  healthDeep: async (): Promise<{
    ok: boolean;
    checks: Record<string, unknown>;
    issues: string[];
    generated_at: string;
    schema_version: number;
  }> => {
    const signalState = timeoutSignal(5_000);
    try {
      const res = await fetch("/api/health/deep", {
        headers: { Accept: "application/json" },
        signal: signalState.signal,
      });
      // 503 is a *legitimate* outcome here — readiness probe semantics.
      // Parse the body either way so the OpsPage pill can render the
      // issue count without a separate retry path.
      const body = await parseBody(res);
      if (
        body &&
        typeof body === "object" &&
        "ok" in body &&
        "checks" in body &&
        "issues" in body &&
        "generated_at" in body &&
        "schema_version" in body
      ) {
        return body as {
          ok: boolean;
          checks: Record<string, unknown>;
          issues: string[];
          generated_at: string;
          schema_version: number;
        };
      }
    } catch {
      /* fallthrough below */
    } finally {
      signalState.clear();
    }

    return {
      ok: false,
      checks: {},
      issues: ["malformed_response"],
      generated_at: new Date().toISOString(),
      schema_version: 1,
    };
  },
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
  backtest: (sampleSize = 200) =>
    request<UIBacktest>(`/api/backtest?sample_size=${sampleSize}`),
  quotes: (symbols: string[]) => {
    const params = new URLSearchParams({ symbols: symbols.join(",") });
    return request<MarketQuoteBatchResponse>(`/ui/quotes?${params.toString()}`);
  },
  quote: (sym: string) =>
    request<MarketQuote>(`/ui/quote/${encodeURIComponent(sym)}`),
  symbol: (sym: string, limit = 50) =>
    request<UISymbol>(`/ui/symbol/${encodeURIComponent(sym)}?limit=${limit}`),
  symbolSentimentTrend: (sym: string, days = 7) =>
    request<SymbolSentimentTrend>(
      `/api/symbols/${encodeURIComponent(sym)}/sentiment-trend?days=${days}`,
    ),

  recent: (limit = 50, relevantOnly = true) =>
    request<{ items: FinancialRecord[] }>(`/recent?limit=${limit}&relevant_only=${relevantOnly}`),
  record: (id: string) => request<FinancialRecord>(`/record/${encodeURIComponent(id)}`),
  bySymbol: (sym: string, limit = 50) =>
    request<{ items: FinancialRecord[] }>(`/records/by-symbol/${encodeURIComponent(sym)}?limit=${limit}`),
  byAssetClass: (ac: string, limit = 50) =>
    request<{ items: FinancialRecord[] }>(`/records/by-asset-class/${encodeURIComponent(ac)}?limit=${limit}`),
  byReason: (rc: string, limit = 50) =>
    request<{ items: FinancialRecord[] }>(`/records/by-reason/${encodeURIComponent(rc)}?limit=${limit}`),

  // ── User-defined record tags ──────────────────────────────────────────
  // Free-form analyst tags layered on top of pipeline-derived labels.
  // Persisted in the ``record_tags`` SQLite table (migration v2). The
  // ``addTag`` call rejects whitespace, anything > 50 chars, or characters
  // outside ``[a-zA-Z0-9_\-.]`` — keep the client guard in sync with the
  // backend regex so the user gets fast feedback before a 400.
  getTags: (captureId: string) =>
    request<{ capture_id: string; tags: string[] }>(
      `/api/records/${encodeURIComponent(captureId)}/tags`,
    ),
  addTag: (captureId: string, tag: string) =>
    request<{ ok: boolean; added: boolean; tags: string[] }>(
      `/api/records/${encodeURIComponent(captureId)}/tags`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ tag }),
      },
    ),
  removeTag: (captureId: string, tag: string) =>
    request<{ ok: boolean; removed: boolean; tags: string[] }>(
      `/api/records/${encodeURIComponent(captureId)}/tags/${encodeURIComponent(tag)}`,
      { method: "DELETE" },
    ),
  listTags: (limit = 50) =>
    request<{ items: { tag: string; count: number }[] }>(
      `/api/tags?limit=${limit}`,
    ),
  recordsByTag: (tag: string, limit = 50) =>
    request<{ items: FinancialRecord[] }>(
      `/api/tags/${encodeURIComponent(tag)}/records?limit=${limit}`,
    ),

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
  /** Per-feed source health — drives /sources page. */
  newsSources: () => request<NewsSourcesResponse>("/api/news/sources"),
  /**
   * Live "awareness window" — how fresh + how broad is awareness right now.
   * window_estimate_seconds ≈ poll_interval + median_publisher_lag. Always
   * 200 (degraded envelope when the poller is disabled). Drives the
   * "Awareness window" panel on /sources.
   */
  newsAwareness: () => request<NewsAwareness>("/api/news/awareness"),
  /**
   * Highest-scoring recent records — analyst attention triage.
   *
   * HTTP twin of the `catchem top-recent` CLI (v58-v59). Reads the last 200
   * records server-side, applies `min_score` filter + score-desc sort, returns
   * top `limit`. Cheap (<5ms) — safe to poll at 30-60s cadence from any UI
   * surface that wants a "what's hot right now" mini-feed without spinning
   * up the full feed-list query (which is heavier + paginated).
   */
  /**
   * v61: news persistence — long-running narratives.
   *
   * Returns top scopes (asset_class/symbol pairs) sorted by persistence_ratio
   * desc, where ratio = days_covered / window_days. A 7-day window with ratio
   * ≥0.7 means the scope was in the news 5+ days out of 7 — structural story.
   *
   * Pairs naturally with sentiment_dispersion and intensity for a 3-axis
   * "what's actually moving the market" lens.
   */
  quantPersistence: (windowDays = 7, minRecords = 3, topN = 20) =>
    request<{
      schema_version: number;
      generated_at: string;
      limit: number;
      window_days: number;
      min_records: number;
      buckets: Array<{
        scope: string;
        days_covered: number;
        total_records: number;
        persistence_ratio: number;
        sample_titles: string[];
      }>;
    }>(`/api/quant/persistence?window_days=${windowDays}&min_records=${minRecords}&top_n=${topN}`),
  /**
   * Recent QuantEngine signal failures — fail-soft observability (v72).
   *
   * Every signal goes through `_safe_call` which catches any exception,
   * returns None to the caller, and keeps the dashboard rendering. That
   * graceful degradation was previously invisible — only a structured
   * warning landed in the rotating sidecar log. This endpoint surfaces
   * the last 50 failures with class + message + traceback head plus
   * per-signal counts so the UI can show a "N signals degraded" pill on
   * the QuantScan hero.
   *
   * Empty payload (total_failures === 0) is the healthy steady state.
   */
  quantDiagnostics: () =>
    request<{
      schema_version: number;
      generated_at: string;
      total_failures: number;
      per_signal: Record<string, number>;
      recent: Array<{
        signal: string;
        error_class: string;
        error: string;
        traceback_head: string;
        elapsed_ms: number;
        ts: string;
      }>;
      buffer_capacity: number;
    }>("/api/quant/diagnostics"),
  /**
   * Per-table SQLite stats (rows, index list, page-count derived size).
   *
   * Drives the Ops page "Database breakdown" card (v64). Cheap COUNT(*) per
   * table on a WAL DB; safe to poll at 60s. Empty record_tags pre-v38 data
   * shows up explicitly (rows: 0) — no silent fallback.
   */
  dbStats: () =>
    request<{
      schema_version: number;
      generated_at: string;
      tables: Array<{ name: string; rows: number }>;
      indexes: Array<{ name: string; table: string }>;
      total_tables: number;
      total_indexes: number;
      page_count: number;
      page_size_bytes: number;
      estimated_size_bytes: number;
    }>("/api/db/stats"),
  newsTopRecent: (limit = 10, minScore = 0.5) =>
    request<{
      schema_version: number;
      generated_at: string;
      limit: number;
      min_score: number;
      count: number;
      items: Array<{
        capture_id: string;
        title: string | null;
        domain: string | null;
        url: string | null;
        score: number | null;
        sentiment: string | null;
        asset_classes: string[];
        symbols: string[];
        published_ts: string | null;
      }>;
    }>(`/api/news/top-recent?limit=${limit}&min_score=${minScore}`),
  newsPollNow: () =>
    request<NewsPollNowResponse>("/ui/news-poll-now", { method: "POST" }),
  /**
   * One-shot manual probe of a single feed by URL. Bypasses the
   * circuit-breaker cooldown and re-runs the regular fetch pipeline.
   * Powers the per-row "probe" button on /sources.
   */
  probeSource: (url: string) =>
    request<{
      ok: boolean;
      url: string;
      result?: Record<string, unknown>;
      error?: string;
    }>("/api/news/sources/probe", {
      method: "POST",
      body: JSON.stringify({ url }),
      headers: { "Content-Type": "application/json" },
    }),
  archiveStatus: () => request<ArchiveStatus>("/ui/archive-status"),
  archiveNow: () =>
    request<ArchiveNowResponse>("/ui/archive-now", { method: "POST" }),

  // Run one pass of the supervisor over the configured Awareness JSONL
  // directory. Used by the Replay tab on /replay so the page name no
  // longer lies about the surface it exposes.
  replay: (maxRecords: number = 50) =>
    request<ReplayRunResponse>("/replay", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ max_records: maxRecords }),
    }),

  // ── Reviews (second-opinion: DeepSeek vs in-process stub) ──────────────
  reviewsStatus: () => request<ReviewsStatus>("/api/reviews/status"),
  reviewsSpendHistory: (days = 7) =>
    request<{
      schema_version: number;
      generated_at: string;
      days: number;
      history: Array<{ day: string; call_count: number; total_cost_usd: number }>;
      totals: { calls: number; cost_usd: number };
    }>(`/api/reviews/spend-history?days=${days}`),
  reviewsCompare: (limit = 200) =>
    request<ReviewsCompareResponse>(`/api/reviews/compare?limit=${limit}`),
  reviewsRun: (captureId: string) =>
    request<{ ok: boolean; capture_id: string; review: unknown }>(
      `/api/reviews/${encodeURIComponent(captureId)}/run`,
      { method: "POST" },
    ),
  reviewsPatchSettings: (patch: Partial<ReviewsSettingsPatch>) =>
    request<ReviewsStatus>("/api/reviews/settings", {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(patch),
    }),

  // ── Awareness Quant Lens ───────────────────────────────────────────────
  quantDashboard: (limit = 500) =>
    request<QuantDashboard>(`/api/quant/dashboard?limit=${limit}`),
  quantClusters: (opts: { limit?: number; window_seconds?: number; similarity_threshold?: number; min_cluster_size?: number } = {}) => {
    const q = new URLSearchParams();
    if (opts.limit) q.set("limit", String(opts.limit));
    if (opts.window_seconds) q.set("window_seconds", String(opts.window_seconds));
    if (opts.similarity_threshold !== undefined) q.set("similarity_threshold", String(opts.similarity_threshold));
    if (opts.min_cluster_size) q.set("min_cluster_size", String(opts.min_cluster_size));
    return request<{ items: EventClusterDTO[]; total: number }>(`/api/quant/clusters?${q.toString()}`);
  },
  quantSources: (opts: { limit?: number; window_days?: number; min_records?: number } = {}) => {
    const q = new URLSearchParams();
    if (opts.limit) q.set("limit", String(opts.limit));
    if (opts.window_days) q.set("window_days", String(opts.window_days));
    if (opts.min_records) q.set("min_records", String(opts.min_records));
    return request<SourceLeaderboardDTO>(`/api/quant/sources?${q.toString()}`);
  },
  quantNovelty: (limit = 200) =>
    request<{ items: NoveltyResultDTO[]; total: number }>(`/api/quant/novelty?limit=${limit}`),
  quantLeadLag: (limit = 500) =>
    request<LeadLagReportDTO>(`/api/quant/lead-lag?limit=${limit}`),
  quantRegime: (opts: { limit?: number; bucket_minutes?: number; shift_threshold?: number } = {}) => {
    const q = new URLSearchParams();
    if (opts.limit) q.set("limit", String(opts.limit));
    if (opts.bucket_minutes) q.set("bucket_minutes", String(opts.bucket_minutes));
    if (opts.shift_threshold !== undefined) q.set("shift_threshold", String(opts.shift_threshold));
    return request<RegimeReportDTO>(`/api/quant/regime?${q.toString()}`);
  },
  quantReaction: (captureId: string) =>
    request<ReactionReportDTO>(`/api/quant/reaction/${encodeURIComponent(captureId)}`),
  quantNoveltyOne: (captureId: string) =>
    request<NoveltyResultDTO>(`/api/quant/novelty/${encodeURIComponent(captureId)}`),
  quantInvalidate: () =>
    request<{ ok: boolean }>("/api/quant/invalidate", { method: "POST" }),
  quantSentimentMomentum: (opts: { limit?: number; bucket_minutes?: number; min_mentions?: number; max_tickers?: number } = {}) => {
    const q = new URLSearchParams();
    if (opts.limit) q.set("limit", String(opts.limit));
    if (opts.bucket_minutes) q.set("bucket_minutes", String(opts.bucket_minutes));
    if (opts.min_mentions) q.set("min_mentions", String(opts.min_mentions));
    if (opts.max_tickers) q.set("max_tickers", String(opts.max_tickers));
    return request<SentimentMomentumReportDTO>(`/api/quant/sentiment-momentum?${q.toString()}`);
  },
  quantSentimentDispersion: (
    limit: number = 1000,
    scope: "overall" | "asset_classes" | "candidate_symbols" = "asset_classes",
  ) =>
    request<SentimentDispersionResponse>(
      `/api/quant/sentiment-dispersion?limit=${limit}&scope=${scope}`,
    ),
  quantIntensity: (
    limit: number = 2000,
    scope: "overall" | "asset_classes" | "candidate_symbols" = "asset_classes",
  ) =>
    request<IntensityResponse>(
      `/api/quant/intensity?limit=${limit}&scope=${scope}`,
    ),
  quantCoOccurrence: (opts: { limit?: number; min_edge_weight?: number; top_n_cells?: number; top_n_edges?: number } = {}) => {
    const q = new URLSearchParams();
    if (opts.limit) q.set("limit", String(opts.limit));
    if (opts.min_edge_weight) q.set("min_edge_weight", String(opts.min_edge_weight));
    if (opts.top_n_cells) q.set("top_n_cells", String(opts.top_n_cells));
    if (opts.top_n_edges) q.set("top_n_edges", String(opts.top_n_edges));
    return request<CoOccurrenceReportDTO>(`/api/quant/co-occurrence?${q.toString()}`);
  },
  quantAnomalies: (opts: { limit?: number; bucket_minutes?: number; window_buckets?: number; z_threshold?: number } = {}) => {
    const q = new URLSearchParams();
    if (opts.limit) q.set("limit", String(opts.limit));
    if (opts.bucket_minutes) q.set("bucket_minutes", String(opts.bucket_minutes));
    if (opts.window_buckets) q.set("window_buckets", String(opts.window_buckets));
    if (opts.z_threshold !== undefined) q.set("z_threshold", String(opts.z_threshold));
    return request<AnomalyReportDTO>(`/api/quant/anomalies?${q.toString()}`);
  },
  quantSpillover: (opts: { limit?: number; bucket_minutes?: number; lag_buckets?: number; surge_z_threshold?: number } = {}) => {
    const q = new URLSearchParams();
    if (opts.limit) q.set("limit", String(opts.limit));
    if (opts.bucket_minutes) q.set("bucket_minutes", String(opts.bucket_minutes));
    if (opts.lag_buckets) q.set("lag_buckets", String(opts.lag_buckets));
    if (opts.surge_z_threshold !== undefined) q.set("surge_z_threshold", String(opts.surge_z_threshold));
    return request<SpilloverReportDTO>(`/api/quant/spillover?${q.toString()}`);
  },
  quantSymbolCorrelation: (
    opts: { limit?: number; bucket_minutes?: number; min_mentions?: number; top_n?: number } = {},
  ) => {
    const q = new URLSearchParams();
    if (opts.limit) q.set("limit", String(opts.limit));
    if (opts.bucket_minutes) q.set("bucket_minutes", String(opts.bucket_minutes));
    if (opts.min_mentions) q.set("min_mentions", String(opts.min_mentions));
    if (opts.top_n) q.set("top_n", String(opts.top_n));
    return request<SymbolCorrelationResponse>(
      `/api/quant/symbol-correlation?${q.toString()}`,
    );
  },
  quantMarketTime: (limit = 1000) =>
    request<MarketTimeResponse>(`/api/quant/market-time?limit=${limit}`),
  quantArrivalHeatmap: (limit = 2000, timezone = "America/New_York") =>
    request<ArrivalHeatmapResponse>(
      `/api/quant/arrival-heatmap?limit=${limit}&timezone=${encodeURIComponent(timezone)}`,
    ),
  quantNewsVelocity: (
    opts: { limit?: number; bucket_minutes?: number; window_minutes?: number } = {},
  ) => {
    const q = new URLSearchParams();
    if (opts.limit) q.set("limit", String(opts.limit));
    if (opts.bucket_minutes) q.set("bucket_minutes", String(opts.bucket_minutes));
    if (opts.window_minutes) q.set("window_minutes", String(opts.window_minutes));
    return request<NewsVelocityResponse>(`/api/quant/news-velocity?${q.toString()}`);
  },
  quantClusterMembers: (clusterId: string, limit = 20) =>
    request<ClusterMembersResponse>(
      `/api/quant/cluster/${encodeURIComponent(clusterId)}/members?limit=${limit}`,
    ),
  quantHeatmapRecords: (asset: string, reason: string, limit = 20) =>
    request<HeatmapRecordsResponse>(
      `/api/quant/heatmap/records?asset=${encodeURIComponent(asset)}&reason=${encodeURIComponent(reason)}&limit=${limit}`,
    ),
  quantRecordDetail: (captureId: string) =>
    request<RecordDetailResponse>(`/api/quant/record/${encodeURIComponent(captureId)}/detail`),
  quantLiveRead: (limit = 1000) =>
    request<LiveReadResponse>(`/api/quant/live-read?limit=${limit}`),
  quantExplain: (kind: "cluster" | "regime_shift" | "anomaly" | "spillover", payload: Record<string, unknown>) =>
    request<{ kind: string; narrative: string; source: "deepseek" | "local"; usd_cost?: number; fallback_reason?: string }>(
      "/api/quant/explain",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ kind, payload }),
      },
    ),

  // ── Global content search (⌘P palette) ───────────────────────────────
  // Backs SearchPalette: substring over recent records (title + domain),
  // top symbol mentions, and fresh quant clusters. Distinct from
  // CommandPalette (⌘K), which is nav+actions, not content.
  search: (q: string, limit = 20) =>
    request<SearchResponse>(
      `/api/search?q=${encodeURIComponent(q)}&limit=${limit}`,
    ),

  // ── Analyst exports (CSV/JSON for Feed / Reviews / QuantScan) ────────
  // These return URLs (NOT request<T>) because the browser drives the
  // download via <a download href={url} />. Building the URL on the
  // client keeps query-string state — current filter chips — visible
  // in the link the analyst can also share or curl.
  exportRecordsUrl: (
    format: "csv" | "json",
    filters: {
      asset_class?: string | null;
      reason_code?: string | null;
      symbol?: string | null;
      min_score?: number | null;
      limit?: number | null;
    } = {},
  ): string => {
    const params = new URLSearchParams({ format });
    for (const [k, v] of Object.entries(filters)) {
      if (v !== null && v !== undefined && v !== "") params.set(k, String(v));
    }
    return `/api/export/records?${params.toString()}`;
  },
  exportReviewsUrl: (
    format: "csv" | "json",
    filters: {
      asset_class?: string | null;
      reason_code?: string | null;
      symbol?: string | null;
      min_score?: number | null;
      limit?: number | null;
    } = {},
  ): string => {
    const params = new URLSearchParams({ format });
    for (const [k, v] of Object.entries(filters)) {
      if (v !== null && v !== undefined && v !== "") params.set(k, String(v));
    }
    return `/api/export/reviews?${params.toString()}`;
  },
  // Quant signals export is JSON only because the structure is nested
  // (clusters carry member arrays, spillover edges are tuples).
  exportQuantUrl: (limit = 1000): string =>
    `/api/export/quant?format=json&limit=${limit}`,

  // ── SQLite truth-store backup / restore ──────────────────────────────
  // The Settings → Database section pulls these. `dbExportUrl` is a
  // direct-download endpoint — the UI uses it via <a download> so the
  // browser handles the streaming/save dialog. `dbImport` posts a
  // multipart upload of a previously saved .sqlite3 file.
  dbInfo: () =>
    request<DbInfoResponse>("/api/db/info"),
  dbSchemaVersion: () =>
    request<DbSchemaVersionResponse>("/api/db/schema_version"),
  dbExportUrl: "/api/db/export",
  dbImport: (file: File) => {
    const form = new FormData();
    form.append("file", file);
    return request<DbImportResponse>("/api/db/import", { method: "POST", body: form });
  },

  // ── Webhook output (Slack/Discord/Teams) ─────────────────────────────
  // The URL is held server-side only; GET never echoes it back. The UI
  // shows a "configured ✓" chip when `url_configured: true` and lets the
  // user replace it with a fresh POST.
  webhookConfig: () => request<WebhookStatus>("/api/webhook/config"),
  webhookSaveConfig: (patch: Partial<WebhookConfigPatch>) =>
    request<WebhookStatus>("/api/webhook/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(patch),
    }),
  webhookTest: (sample: Partial<WebhookTestSample> = {}) =>
    request<WebhookTestResult>("/api/webhook/test", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(sample),
    }),
};

// ── Additional quant DTOs (deep signals) ──────────────────────────────────

export interface SentimentBucketDTO {
  bucket_start: string;
  bucket_end: string;
  count: number;
  positive: number;
  neutral: number;
  negative: number;
  net_sentiment: number;
  mean_score: number;
  mean_relevance: number;
}

export interface TickerMomentumDTO {
  symbol: string;
  mention_count: number;
  buckets: SentimentBucketDTO[];
  overall_net_sentiment: number;
  momentum: number;
  velocity: number;
  direction: string;
  flip_detected: boolean;
  last_bucket_start: string;
}

export interface SentimentMomentumReportDTO {
  bucket_minutes: number;
  min_mentions: number;
  tickers: TickerMomentumDTO[];
}

export interface SentimentDispersionEntryDTO {
  scope: string;
  sample_size: number;
  counts: { positive: number; neutral: number; negative: number };
  entropy: number;
  max_entropy: number;
  normalized_entropy: number;
  dominant_label: "positive" | "neutral" | "negative" | "tied";
}

export interface SentimentDispersionResponse {
  schema_version: number;
  generated_at: string;
  scope: "overall" | "asset_classes" | "candidate_symbols";
  sample_window: number;
  result: SentimentDispersionEntryDTO | null;
  buckets: SentimentDispersionEntryDTO[] | null;
}

export interface IntensityTopRecordDTO {
  capture_id: string | null;
  title: string | null;
  intensity: number;
  score: number | null;
  sentiment_label: string | null;
  sentiment_score: number | null;
}

export interface IntensityBucketDTO {
  scope: string;
  sample_size: number;
  mean_intensity: number;
  max_intensity: number;
  count_high_intensity: number;
  top_records: IntensityTopRecordDTO[];
}

export interface IntensityResponse {
  schema_version: number;
  generated_at: string;
  scope: "overall" | "asset_classes" | "candidate_symbols";
  sample_window: number;
  result: IntensityBucketDTO | null;
  buckets: IntensityBucketDTO[] | null;
}

export interface AssetReasonCellDTO {
  asset_class: string;
  reason_code: string;
  count: number;
  lift: number;
  mean_relevance: number;
}

export interface SymbolEdgeDTO {
  symbol_a: string;
  symbol_b: string;
  weight: number;
  sample_capture_ids: string[];
}

export interface AssetConcentrationDTO {
  asset_class: string;
  record_count: number;
  reason_count: number;
  herfindahl_index: number;
  top_reasons: Array<[string, number]>;
}

export interface CoOccurrenceReportDTO {
  total_records: number;
  distinct_assets: number;
  distinct_reasons: number;
  distinct_symbols: number;
  asset_reason_cells: AssetReasonCellDTO[];
  strong_edges: SymbolEdgeDTO[];
  asset_concentration: AssetConcentrationDTO[];
}

export interface VolumeAnomalyDTO {
  bucket_start: string;
  bucket_end: string;
  observed: number;
  rolling_mean: number;
  rolling_std: number;
  z_score: number;
  severity: "low" | "medium" | "high";
}

export interface SentimentShockDTO {
  bucket_start: string;
  bucket_end: string;
  observed_net: number;
  rolling_mean: number;
  rolling_std: number;
  z_score: number;
  direction: "bullish_shock" | "bearish_shock" | "neutral";
}

export interface SymbolBurstDTO {
  symbol: string;
  bucket_start: string;
  observed: number;
  rolling_mean: number;
  z_score: number;
  sample_capture_ids: string[];
}

export interface AnomalyReportDTO {
  bucket_minutes: number;
  window_buckets: number;
  z_threshold: number;
  volume_anomalies: VolumeAnomalyDTO[];
  sentiment_shocks: SentimentShockDTO[];
  symbol_bursts: SymbolBurstDTO[];
}

export interface SpilloverEdgeDTO {
  source_asset: string;
  target_asset: string;
  lag_minutes: number;
  co_movements: number;
  source_only_surges: number;
  target_only_surges: number;
  spillover_score: number;
  sample_pivots: string[];
}

export interface SpilloverReportDTO {
  bucket_minutes: number;
  lag_buckets: number;
  surge_z_threshold: number;
  edges: SpilloverEdgeDTO[];
  self_loops: SpilloverEdgeDTO[];
  total_buckets: number;
}

export interface SymbolPairDTO {
  symbol_a: string;
  symbol_b: string;
  pearson_r: number;
  n_buckets: number;
  a_total: number;
  b_total: number;
}

export interface SymbolCorrelationResponse {
  schema_version: number;
  generated_at: string;
  limit: number;
  bucket_minutes: number;
  min_mentions: number;
  pairs: SymbolPairDTO[];
}

export interface MarketTimeBucketDTO {
  session: string;
  volume: number;
  avg_score: number;
  relevant_count: number;
}

export interface MarketTimeResponse {
  schema_version: number;
  generated_at: string;
  limit: number;
  total_records: number;
  sessions: string[];
  buckets: MarketTimeBucketDTO[];
  highest_score_session: string | null;
  highest_volume_session: string | null;
}

/**
 * 24h × 7day news-arrival heatmap. Dense 168-cell grid (weekday outer,
 * hour inner) so the UI can render the ECharts heatmap without
 * densifying client-side.
 */
export interface ArrivalHeatmapCell {
  weekday: number; // 0 = Monday, 6 = Sunday
  hour: number;    // 0..23 (local hour in the resolved timezone)
  count: number;
}

export interface ArrivalHeatmapResponse {
  schema_version: number;
  generated_at: string;
  limit: number;
  cells: ArrivalHeatmapCell[];
  max_count: number;
  total_samples: number;
  peak_cells: ArrivalHeatmapCell[];
  timezone: string;
  weekday_labels: string[];
}

/**
 * News velocity — how fast records are arriving and whether the rate is
 * accelerating. ``regime`` mirrors the backend classifier:
 * ``"calm" | "active" | "burst" | "quiet"``.
 */
export interface NewsVelocityResponse {
  schema_version: number;
  generated_at: string;
  limit: number;
  bucket_minutes: number;
  window_minutes: number;
  current_rate_per_min: number;
  ema_fast: number;
  ema_slow: number;
  baseline_rate: number;
  baseline_std: number;
  acceleration_z: number;
  regime: "calm" | "active" | "burst" | "quiet";
  samples: number;
}

export interface QuantMember {
  capture_id: string;
  title: string | null;
  domain: string | null;
  url: string | null;
  published_ts: string | null;
  finance_relevance_score: number | null;
  sentiment_label: "positive" | "neutral" | "negative" | null;
  asset_classes: string[];
  impact_reason_codes: string[];
  candidate_symbols: string[];
}

export interface ClusterMembersResponse {
  cluster_id: string;
  total_in_cluster: number;
  returned: number;
  members: QuantMember[];
}

export interface HeatmapRecordsResponse {
  asset_class: string;
  reason_code: string;
  total_returned: number;
  records: Omit<QuantMember, "impact_reason_codes" | "asset_classes">[];
}

export interface ReviewerRowDTO {
  capture_id: string;
  reviewer_id: string;
  reviewer_version: string;
  payload: ReviewPayload;
  input_tokens: number;
  output_tokens: number;
  usd_cost: number;
  latency_ms: number;
  created_at: string;
  error_code: string | null;
}

export interface RecordDetailResponse {
  record: Record<string, unknown>;
  reviews: ReviewerRowDTO[];
  reaction: ReactionReportDTO | null;
}

export interface LiveReadResponse {
  narrative: string;
  source: "deepseek" | "local";
  usd_cost?: number;
  fallback_reason?: string;
  context: Record<string, unknown>;
  generated_at: string;
}

// ── Quant API types (mirror src/catchem/quant/* dataclasses) ─────────────

export interface EventClusterDTO {
  cluster_id: string;
  capture_ids: string[];
  first_seen_ts: string;
  last_seen_ts: string;
  dominant_symbols: string[];
  dominant_reasons: string[];
  dominant_assets: string[];
  member_domains: string[];
  size: number;
  mean_relevance: number;
  coherence: number;
}

export interface SourceScoreDTO {
  domain: string;
  record_count: number;
  relevant_count: number;
  relevant_rate: number;
  mean_relevance_score: number;
  signal_density: number;
  sentiment_skew: number;
  asset_diversity: number;
  reason_diversity: number;
  symbol_uniqueness: number;
  composite_score: number;
}

export interface SourceLeaderboardDTO {
  window_days: number;
  total_records: number;
  total_domains: number;
  sources: SourceScoreDTO[];
}

export interface NoveltyResultDTO {
  capture_id: string;
  novelty_score: number;
  max_similarity_to_corpus: number;
  nearest_capture_id: string | null;
  nearest_title: string | null;
  matched_symbols: string[];
  explanation: string;
}

export interface PerEventLeadLagDTO {
  cluster_id: string;
  leader_domain: string | null;
  leader_capture_id: string | null;
  leader_ts: string | null;
  member_count: number;
  follower_lag_seconds: Array<[string, number]>;
}

export interface SourceLeadLagScoreDTO {
  domain: string;
  events_participated: number;
  events_led: number;
  lead_rate: number;
  mean_lag_seconds_when_following: number | null;
  mean_lead_seconds_when_leading: number | null;
  composite_score: number;
}

export interface LeadLagReportDTO {
  total_events: number;
  total_sources: number;
  per_event: PerEventLeadLagDTO[];
  per_source: SourceLeadLagScoreDTO[];
}

export interface RegimeBucketDTO {
  bucket_start: string;
  bucket_end: string;
  record_count: number;
  asset_distribution: Array<[string, number]>;
  reason_distribution: Array<[string, number]>;
  sentiment_distribution: Array<[string, number]>;
  mean_relevance: number;
  kl_divergence_from_prev: number | null;
  is_regime_shift: boolean;
}

export interface RegimeReportDTO {
  bucket_minutes: number;
  shift_threshold: number;
  buckets: RegimeBucketDTO[];
  detected_shifts: string[];
}

export interface HorizonReturnDTO {
  horizon: string;
  symbol: string;
  last_at_t0: number | null;
  last_at_t: number | null;
  return_pct: number | null;
  benchmark_return_pct: number | null;
  excess_return_pct: number | null;
}

export interface ReactionReportDTO {
  capture_id: string;
  published_ts: string | null;
  horizons: HorizonReturnDTO[];
  headline_excess_return_15m: number | null;
  benchmark_symbol: string;
  fallback_reason: string | null;
}

export interface QuantDashboard {
  n_records_window: number;
  n_clusters: number;
  clusters: EventClusterDTO[];
  source_leaderboard: SourceLeaderboardDTO | null;
  novelty_timeline: NoveltyResultDTO[];
  lead_lag: LeadLagReportDTO | null;
  regime: RegimeReportDTO | null;
  sentiment_momentum: SentimentMomentumReportDTO | null;
  co_occurrence: CoOccurrenceReportDTO | null;
  anomalies: AnomalyReportDTO | null;
  spillover: SpilloverReportDTO | null;
  generated_at: string;
}

// ── Reviews API shape (matches src/catchem/api.py @ /api/reviews/*) ────

export interface ReviewsStatus {
  deepseek_enabled: boolean;
  deepseek_keyed: boolean;
  deepseek_ready: boolean;
  model: string;
  sampling_rate: number;
  usd_cap: number;
  usd_spent: number;
  usd_remaining: number;
  exhausted: boolean;
  primary_reviewer_version: string;
  tokens: { input: number; output: number; calls: number; errors: number };
  base_url: string;
  generated_at: string;
}

export interface ReviewPayload {
  is_finance_relevant: boolean;
  finance_relevance_score: number;
  asset_classes: string[];
  impact_reason_codes: string[];
  candidate_symbols: string[];
  sentiment_label: "positive" | "neutral" | "negative" | null;
  sentiment_score: number | null;
  evidence_sentences: string[];
  reason_text: string | null;
  raw?: Record<string, unknown> | null;
}

export interface ReviewSide {
  capture_id: string;
  reviewer_id: string;
  reviewer_version: string;
  created_at: string;
  error_code: string | null;
  payload: ReviewPayload;
  // Present only on the DeepSeek side.
  input_tokens?: number;
  output_tokens?: number;
  usd_cost?: number;
  latency_ms?: number;
}

export interface Agreement {
  relevance_match: boolean;
  score_delta: number;
  asset_jaccard: number;
  reason_jaccard: number;
  symbol_jaccard: number;
  sentiment_match: boolean;
  overall: number;
}

export interface CompareItem {
  capture_id: string;
  title: string | null;
  domain: string | null;
  url: string | null;
  stub: ReviewSide;
  deepseek: ReviewSide;
  agreement: Agreement;
}

export interface CompareSummary {
  n: number;
  relevance_match_rate: number;
  sentiment_match_rate: number;
  mean_asset_jaccard: number;
  mean_reason_jaccard: number;
  mean_symbol_jaccard: number;
  mean_score_delta: number;
  mean_overall: number;
  deepseek_errors: number;
}

export interface ReviewsCompareResponse {
  items: CompareItem[];
  summary: CompareSummary;
  generated_at: string;
}

export interface SearchRecordHit {
  capture_id: string;
  title: string | null;
  domain: string | null;
  score: number | null;
  published_ts: string | null;
}

export interface SearchSymbolHit {
  symbol: string;
  count: number;
}

export interface SearchClusterHit {
  cluster_id: string;
  size: number;
  symbols: string[];
}

export interface SearchResponse {
  query: string;
  records: SearchRecordHit[];
  symbols: SearchSymbolHit[];
  clusters: SearchClusterHit[];
}

export interface DbInfoResponse {
  exists: boolean;
  size_bytes?: number;
  modified_at?: string;
  path?: string;
  generated_at?: string;
}

export interface DbImportResponse {
  ok: boolean;
  backup_path: string | null;
  imported_size_bytes: number;
  db_path?: string;
  generated_at?: string;
}

// /api/db/schema_version response. ``user_version`` is the PRAGMA value
// currently stored in the DB file; ``max_known`` is the highest version
// the running build knows about. ``migrations_pending`` lists names of
// migrations that WOULD run on the next storage init — empty when up
// to date.
export interface DbSchemaVersionResponse {
  user_version: number;
  max_known: number;
  migrations_pending: string[];
  generated_at?: string;
}

export interface ReviewsSettingsPatch {
  enabled: boolean;
  sampling_rate: number;
  usd_cap: number;
  api_key: string;
  model: string;
  base_url: string;
}

// ── Webhook output (Slack/Discord/Teams) ─────────────────────────────────
//
// `WebhookStatus` is what `/api/webhook/config` returns; note the URL is
// NEVER shipped back — only the `url_configured: bool` flag. The Settings
// panel renders that as a "configured ✓" chip with a Replace button.
export interface WebhookStatus {
  enabled: boolean;
  url_configured: boolean;
  min_score: number;
  asset_class_filter: string[] | null;
  reason_code_filter: string[] | null;
  timeout_seconds: number;
  stats: {
    attempted: number;
    sent: number;
    filtered: number;
    failed: number;
  };
  last_status: string | null;
  last_error: string | null;
  generated_at: string;
}

export interface WebhookConfigPatch {
  enabled: boolean;
  url: string;
  min_score: number;
  asset_class_filter: string[] | null;
  reason_code_filter: string[] | null;
  timeout_seconds: number;
}

export interface WebhookTestSample {
  title: string;
  url: string;
  domain: string;
  asset_classes: string[];
  impact_reason_codes: string[];
  candidate_symbols: string[];
}

export interface WebhookTestResult {
  ok: boolean;
  status: string;
  url_configured: boolean;
  generated_at: string;
}

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

/**
 * Color the score number based on the calibrated max-observed band on
 * the catchem scorer. Empirically the distribution caps ~0.80, so:
 *   - ≥ 0.70 → top-decile → text-good (green)
 *   - ≥ 0.40 → solid middle → text-fg (default)
 *   - <  0.40 → low signal → text-fg-dim
 * Used by the Overview "most recent" + Feed rows so a wall of identical
 * 0.5x numbers stops looking monochrome.
 */
export function scoreToneClass(n: number | null | undefined): string {
  if (n == null || !Number.isFinite(n)) return "text-[color:var(--fg-muted)]";
  if (n >= 0.7) return "text-good";
  if (n >= 0.4) return "text-[color:var(--fg)]";
  return "text-[color:var(--fg-dim)]";
}

/**
 * Format a byte count into a human-readable string (KB / MB / GB).
 *
 * Uses base-1024 because storage tools (Finder, df, sqlite_stat) all
 * report SQLite file sizes in KiB/MiB even though they label as KB/MB.
 * Picking IEC values keeps the displayed number identical to what an
 * operator sees in `ls -lh` on the same file.
 *
 * Returns "—" for null/undefined/non-finite so callers can pipe straight
 * into JSX without conditional guards.
 */
export function fmtBytes(n: number | null | undefined): string {
  if (n == null || !Number.isFinite(n) || n < 0) return "—";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  if (n < 1024 * 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)} MB`;
  return `${(n / 1024 / 1024 / 1024).toFixed(2)} GB`;
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
