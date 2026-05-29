// Backend payload shapes. Mirrored from FastAPI responses.
// Keep these tight so the rest of the app can rely on them.

export type Mode = "production_safe" | "replay_existing" | "live_tail" | "research_diagnostic";

export interface Totals {
  total: number;
  finance_relevant: number;
}

export interface GuardSnapshot {
  ok: boolean;
  release_gate_passed?: boolean;
  quarantine_state?: string;
  fusion_verdict_class?: string;
  safe_to_publish?: boolean;
  safe_to_promote?: boolean;
  governance_index_sha256?: string;
  error_code?: string | null;
  error?: string;
}

// The backend exposes TWO record shapes (see src/catchem/contracts.py):
//
//   • FinancialImpactSummary — the COMPACT row returned by every list/feed
//     endpoint (`/recent`, `/records/by-symbol|by-asset-class|by-reason`,
//     `/api/tags/{tag}/records`). It deliberately OMITS the heavy detail
//     fields (candidate_entities, component_scores, evidence_sentences,
//     reason_text, …) and ADDS `evidence_preview` + `evidence_count`.
//   • FinancialImpactDetail — the FULL record returned only by
//     `/record/{id}`, `/ui/summary` recent_top, and `DemoRunResponse.record`.
//
// They are mirrored here as `FinancialRecordSummary` (list) and
// `FinancialRecord` (full). `FinancialRecord` extends the summary so detail
// consumers keep every field, while the summary type stops list-row readers
// from assuming detail-only fields are present (they are `undefined` at
// runtime, which previously crashed with no compiler warning).
//
// Named FinancialRecord to avoid shadowing the built-in Record<K,V> utility type.

/** Compact list/feed row — mirrors backend FinancialImpactSummary. */
export interface FinancialRecordSummary {
  capture_id: string;
  doc_id: string;
  title: string | null;
  domain: string | null;
  language: string | null;
  url: string | null;
  is_finance_relevant: boolean;
  finance_relevance_score: number;
  asset_classes: string[];
  impact_reason_codes: string[];
  candidate_symbols: string[];
  sentiment_label: "positive" | "negative" | "neutral" | "unknown" | null;
  sentiment_score: number | null;
  /** First evidence sentence (≤240 chars), if any. Summary-only field. */
  evidence_preview: string | null;
  /** Count of evidence sentences on the full record. Summary-only field. */
  evidence_count: number;
  diagnostic_multimodal_enabled: boolean;
  published_ts: string | null;
  created_at: string;
}

/**
 * Full record — mirrors backend FinancialImpactDetail. Returned only by the
 * detail/recent_top/demo paths. Extends {@link FinancialRecordSummary} so
 * detail consumers see the compact fields too. The detail-only fields below
 * (evidence_sentences, reason_text, component_scores, …) are NOT present on
 * list rows — read them only off a `/record/{id}` payload.
 *
 * `evidence_preview` / `evidence_count` are summary-only on the wire, so they
 * are widened to optional here (the detail payload doesn't carry them).
 */
export interface FinancialRecord
  extends Omit<FinancialRecordSummary, "evidence_preview" | "evidence_count"> {
  evidence_preview?: string | null;
  evidence_count?: number;
  candidate_entities: string[];
  impact_horizons: string[];
  evidence_sentences: string[];
  reason_text: string | null;
  component_scores: Record<string, number>;
  diagnostic_multimodal_result: Record<string, unknown> | null;
  processing_mode: string;
  model_versions: Record<string, string>;
}

export interface UISummary {
  mode: Mode;
  is_production_safe: boolean;
  diagnostic_allowed: boolean;
  use_ml_stubs: boolean;
  totals: Totals;
  diagnostic_count: number;
  asset_class_distribution: Record<string, number>;
  reason_code_distribution: Record<string, number>;
  sentiment_distribution: Record<string, number>;
  recent_top: FinancialRecord[];
  dlq: number;
  model_versions: Record<string, string>;
  guards: GuardSnapshot;
  generated_at: string;
}

export interface UIFacets {
  window_total: number;
  window_relevant: number;
  asset_classes: [string, number][];
  reason_codes: [string, number][];
  symbols: [string, number][];
  domains: [string, number][];
  sentiments: [string, number][];
}

export interface UITimeline {
  bucket_minutes: number;
  series: { ts: string; total: number; relevant: number }[];
}

export interface UITrends {
  buckets: string[];
  asset_classes: string[];
  series: Record<string, number[]>;
}

export interface UIMatrix {
  asset_classes: string[];
  reason_codes: string[];
  matrix: number[][];
}

export interface UIBenchmark {
  relevance: { precision: number; recall: number; f1: number };
  asset_class_f1: Record<string, number>;
  reason_code_f1: Record<string, number>;
  symbol_recall: number | null;
  sentiment_accuracy: number | null;
  n: number;
  per_item: {
    capture_id: string;
    expected_finance_relevant: boolean;
    predicted_finance_relevant: boolean;
    score: number;
    expected_asset_classes: string[];
    predicted_asset_classes: string[];
    expected_reason_codes: string[];
    predicted_reason_codes: string[];
  }[];
  ran_at: string;
}

/**
 * Backtest response (matches `GET /api/backtest`).
 *
 * `summary` is always populated (zero-valued shape on empty storage); the
 * UI never needs to branch on null. `calibration_bins` is a sparse list —
 * a quintile that holds zero predictions is omitted, NOT included with
 * count: 0, so iteration produces only meaningful rows.
 */
export interface UIBacktest {
  schema_version: number;
  ran_at: string;
  sample_size: number;
  summary: {
    items_evaluated: number;
    mean_abs_error: number;
    mean_signed_error: number;
    max_abs_error: number;
  };
  calibration_bins: {
    bin_low: number;
    bin_high: number;
    predicted_count: number;
    avg_predicted_score: number;
    avg_ground_truth_score: number;
    calibration_gap: number;
  }[];
  predictions_sample: {
    // Nullable on the wire: the backend builds this as
    // ``stub_row.get("capture_id") or ds_row.get("capture_id")`` which is
    // None when neither paired row carries one. The page already renders a
    // "—" fallback; the type now matches reality so future consumers can't
    // assume a non-null string and crash on e.g. `capture_id.slice(...)`.
    capture_id: string | null;
    predicted_score: number;
    ground_truth_score: number;
    delta: number;
  }[];
}

export interface UISymbol {
  symbol: string;
  count: number;
  reason_distribution: Record<string, number>;
  sentiment_distribution: Record<string, number>;
  items: FinancialRecord[];
}

/** Daily sentiment counts for one symbol — backs the stacked area + sparkline. */
export interface SymbolSentimentTrend {
  symbol: string;
  days: number;
  series: Array<{
    day: string; // YYYY-MM-DD (UTC)
    positive: number;
    neutral: number;
    negative: number;
  }>;
}

export interface MarketQuote {
  symbol: string;
  provider: string;
  as_of: string | null;
  retrieved_at: string;
  currency: string | null;
  last: number | null;
  prev_close: number | null;
  change_abs: number | null;
  change_pct: number | null;
  market_state: string;
  stale_after: string | null;
  freshness_status: "stale" | "unavailable" | string;
  error_code: string | null;
}

export interface MarketQuoteBatchResponse {
  items: MarketQuote[];
  provider: string;
  generated_at: string;
}

export interface UIConfig {
  mode: Mode;
  use_ml_stubs: boolean;
  newsimpact_diagnostic_enabled: boolean;
  diagnostic_allowed: boolean;
  model_versions: Record<string, string>;
}

export interface UIMetrics {
  mode: Mode;
  diagnostic_enabled: boolean;
  use_ml_stubs: boolean;
  records: Totals;
  dlq: number;
  model_versions: Record<string, string>;
}

// ── Catchem desktop additions ──────────────────────────────────────────────

export interface DemoRunResponse {
  capture_id: string;
  jsonl_basename: string;
  processed: number;
  skipped: number;
  record: FinancialRecord;
}

export interface AppInfo {
  name: string;
  version: string;
  commit_sha: string | null;
  branch: string | null;
  mode: Mode;
  use_ml_stubs: boolean;
  diagnostic_allowed: boolean;
  static_bundle_present: boolean;
  model_versions: Record<string, string>;
  generated_at: string;
}

export interface SidecarStatus {
  healthy: boolean;
  api_host: string;
  api_port: number;
  pid: number;
  uptime_seconds: number;
  records: Totals;
  dlq: number;
  diagnostic_enabled: boolean;
  generated_at: string;
}

export interface LogTail {
  lines: string[];
  truncated: boolean;
}

export interface NewsStatus {
  enabled: boolean;
  feeds: number;
  interval_seconds: number | null;
  last_run_at: string | null;
  next_run_at: string | null;
  last_ingested: number;
  total_ingested: number;
  last_error: string | null;
  is_polling: boolean;
  /** When did the poller most recently ingest at least one NEW item? */
  last_new_at: string | null;
  /** Consecutive ticks where last_ingested was 0. >0 means "publishers quiet". */
  empty_ticks: number;
  /** Avg seconds between item.published_ts and ingest time, over the last poll. */
  last_avg_publisher_lag_seconds: number | null;
  /** Median seconds. More honest than the avg when a few backfill items skew it. */
  last_median_publisher_lag_seconds: number | null;
  /** Number of configured feeds with the latest health check failing. */
  unhealthy_feeds?: number;
}

export interface NewsPollNowResponse {
  ingested: number;
  total_ingested: number;
}

/**
 * One row in the per-feed health table. ``last_status`` is one of
 *   - "ok"      — most recent poll returned 200
 *   - "error"   — most recent poll failed
 *   - "unknown" — feed registered but no poll has happened yet
 *
 * ``success_rate`` is in [0, 1]. ``items_total`` is cumulative since
 * sidecar boot (in-memory only — resets on restart).
 */
export interface NewsSourceRow {
  name: string;
  url: string;
  fallback_domain: string;
  polls: number;
  successes: number;
  failures: number;
  success_rate: number;
  items_total: number;
  item_count: number;
  last_status: "ok" | "error" | "unknown" | "backed_off";
  last_status_code: number | null;
  last_error: string | null;
  last_status_at: string | null;
  last_success_at: string | null;
  last_failure_at: string | null;
  consecutive_errors: number;
  elapsed_ms: number | null;
  /**
   * ISO timestamp of when the poller will next attempt this feed, when
   * the circuit breaker is open. Null if the feed is healthy or merely
   * degraded but not yet over the threshold.
   */
  cooldown_until?: string | null;
  /** True if the most recent tick skipped this feed due to cooldown. */
  backed_off?: boolean;
  /**
   * Adaptive poll cadence — how many poller cycles between attempts. The
   * poller stretches the interval for feeds that keep coming back empty
   * (publishers quiet) to save bandwidth. 1 (or absent) = every cycle.
   */
  adaptive_cadence?: number;
  /** Consecutive ticks this feed returned zero NEW items. */
  consecutive_empty?: number;
  /** Cumulative NEW (deduped) items ingested from this feed since boot. */
  total_new_items?: number;
}

export interface NewsSourcesResponse {
  schema_version: number;
  generated_at: string;
  /** false when the poller is disabled / not yet booted. */
  configured: boolean;
  total: number;
  healthy_count: number;
  degraded_count: number;
  /** Number of feeds currently in circuit-breaker cooldown. */
  backed_off_count?: number;
  total_items?: number;
  interval_seconds?: number;
  last_run_at?: string | null;
  sources: NewsSourceRow[];
}

/**
 * The live "awareness window" — answers "how fresh / how broad is
 * awareness right now?". ``window_estimate_seconds`` ≈
 * ``poll_interval_seconds + median_publisher_lag_seconds`` (the effective
 * span between an event happening and it surfacing in the feed).
 *
 * Degraded shape (poller disabled / not yet booted): ``configured:false``,
 * ``sources_total:0``, empty ``sources_by_parser``, null lags + window.
 */
export interface NewsAwareness {
  schema_version: number;
  generated_at: string;
  configured: boolean;
  sources_total: number;
  /** Configured feeds tallied by parser key ("rss", "gdelt", "reddit", …). */
  sources_by_parser: Record<string, number>;
  poll_interval_seconds: number | null;
  median_publisher_lag_seconds: number | null;
  avg_publisher_lag_seconds: number | null;
  last_run_at: string | null;
  last_new_at: string | null;
  total_ingested: number;
  /** poll_interval + median_publisher_lag; null when no fresh lag this tick. */
  window_estimate_seconds: number | null;
}

/**
 * One watched term that HAS been seen recently — part of the coverage map.
 * ``last_seen_age_seconds`` is how stale the freshest mention is;
 * ``mention_count`` is how many records inside the window referenced it.
 */
export interface NewsCoverageCovered {
  term: string;
  last_seen_age_seconds: number;
  mention_count: number;
}

/**
 * Blind-spot detector — answers "what am I NOT seeing?". Compares the set
 * of watched terms against what actually arrived inside ``window_seconds``.
 * ``gaps`` are watched terms with ZERO recent coverage; ``covered`` carries
 * each seen term with its freshest-mention age + mention count.
 *
 * Degraded / freshly-booted shape: empty ``gaps`` and ``covered`` — which
 * the UI renders as the benign "all covered" empty state.
 */
export interface NewsCoverageGaps {
  schema_version: number;
  generated_at: string;
  window_seconds: number;
  covered: NewsCoverageCovered[];
  gaps: string[];
}

export interface ArchiveStatus {
  enabled: boolean;
  drive_dir: string | null;
  interval_seconds: number | null;
  local_cap_rows: number | null;
  last_run_at: string | null;
  last_archived_count: number;
  total_archived: number;
  last_error: string | null;
  is_archiving: boolean;
  current_csv_path: string | null;
}

export interface ArchiveNowResponse {
  archived: number;
  csv_path: string | null;
  error: string | null;
  total_archived: number;
}

// ── Portfolio (read-only holdings tracker) ──────────────────────────────────
//
// A holding is a user-tracked instrument — NOT a trading position. There is
// no order/execution surface anywhere in catchem; the portfolio simply layers
// the awareness pipeline (live quote + coverage + recent news + blind-spot
// status) over a hand-maintained watchlist with optional bookkeeping fields
// (shares / cost_basis) the analyst fills in for context.
//
// `shares`, `weight`, and `cost_basis` are all optional: a holding can be a
// bare symbol the analyst is merely watching, or a fully-specified line item.
export interface PortfolioHolding {
  // Backend serializes this as a JSON number (storage._row_to_holding does
  // `int(r["id"])`, and the DELETE route binds `holding_id: int`).
  id: number;
  symbol: string;
  label?: string | null;
  shares?: number | null;
  weight?: number | null;
  cost_basis?: number | null;
  notes?: string | null;
  added_at: string;
}

export interface PortfolioListResponse {
  schema_version: number;
  generated_at: string;
  holdings: PortfolioHolding[];
}

/**
 * One holding enriched by the awareness layer (GET /api/portfolio/enriched).
 *
 * Every enrichment field is independently nullable/degradable so a holding
 * with no quote provider, no recent coverage, or a freshly-booted poller
 * still renders:
 *   - `quote` is null when no market data is available for the symbol.
 *   - `coverage.covered=false` is the blind-spot signal — the symbol is
 *     tracked but nothing referencing it has arrived inside the window.
 *   - `recent_top` is capped server-side (top few headlines by score).
 */
export interface PortfolioEnrichedHolding extends PortfolioHolding {
  quote: {
    last: number | null;
    prev_close: number | null;
    change_pct: number | null;
  } | null;
  coverage: {
    covered: boolean;
    last_seen_age_seconds: number | null;
    mention_count: number;
  };
  recent_news_count: number;
  recent_top: Array<{ title: string; url: string | null; score: number | null }>;
  sentiment_label?: "positive" | "negative" | "neutral" | "unknown" | null;
}

export interface PortfolioEnriched {
  schema_version: number;
  generated_at: string;
  holdings: PortfolioEnrichedHolding[];
}

/** Body for POST /api/portfolio — every field except `symbol` is optional. */
export interface PortfolioAddBody {
  symbol: string;
  label?: string | null;
  shares?: number | null;
  weight?: number | null;
  cost_basis?: number | null;
  notes?: string | null;
}

/**
 * One theme's tone summary inside {@link GlobalTone}. Mirrors
 * ``summarize_tone`` in src/catchem/quant/global_tone.py. Every tone field is
 * nullable because an empty/all-malformed GDELT timeline yields a neutral
 * all-zero summary (``n_points: 0``, ``tone_state: "stable"``).
 */
export interface GlobalToneTheme {
  latest_tone: number | null;
  mean_tone: number | null;
  min_tone: number | null;
  max_tone: number | null;
  tone_trend: number;
  tone_slope: number;
  tone_state: "improving" | "deteriorating" | "stable";
  n_points: number;
  generated_at: string;
}

/**
 * Global news tone — GDELT-derived macro sentiment (GET /api/quant/global-tone).
 *
 * ``overall_tone`` is the mean of per-theme latest tones (negative = bad press,
 * positive = good). ``overall_state`` classifies the aggregate trend. The
 * endpoint never 500s on a GDELT outage: when no theme produced a usable point
 * it returns ``degraded: true`` with each theme carrying its neutral summary.
 * Cached server-side ~120s, so the UI polls at the same cadence.
 */
export interface GlobalTone {
  schema_version: number;
  degraded: boolean;
  generated_at: string;
  overall_tone: number | null;
  overall_state: "improving" | "deteriorating" | "stable";
  by_theme: Record<string, GlobalToneTheme>;
}

/** Result of POST /replay — single pass over the awareness JSONL dir. */
export interface ReplayRunResponse {
  processed: number;
  skipped: number;
  failed: number;
  dlq: number;
  dlq_delta: number;
  records_before: Totals;
  records_after: Totals;
  inserted: number;
  replaced: number;
  net_new_records: number;
}
