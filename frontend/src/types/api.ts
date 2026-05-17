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
  error?: string;
}

// Named FinancialRecord to avoid shadowing the built-in Record<K,V> utility type.
export interface FinancialRecord {
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
  candidate_entities: string[];
  impact_horizons: string[];
  sentiment_label: "positive" | "negative" | "neutral" | "unknown" | null;
  sentiment_score: number | null;
  evidence_sentences: string[];
  reason_text: string | null;
  component_scores: Record<string, number>;
  diagnostic_multimodal_enabled: boolean;
  diagnostic_multimodal_result: Record<string, unknown> | null;
  processing_mode: string;
  model_versions: Record<string, string>;
  published_ts: string | null;
  created_at: string;
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

export interface UISymbol {
  symbol: string;
  count: number;
  reason_distribution: Record<string, number>;
  sentiment_distribution: Record<string, number>;
  items: FinancialRecord[];
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
}

export interface NewsPollNowResponse {
  ingested: number;
  total_ingested: number;
}
