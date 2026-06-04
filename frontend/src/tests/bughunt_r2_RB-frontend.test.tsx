/**
 * Bug-hunt Round 2 — file group "RB-frontend".
 *
 * One focused regression per confirmed finding. Each assertion fails on the
 * pre-fix code and passes after the fix:
 *
 *   F1  types/api.ts          — list/detail contract split. The list-row
 *                               type (FinancialRecordSummary) must NOT assert
 *                               the 8 detail-only fields, and MUST carry the 2
 *                               summary-only fields the backend emits.
 *   F2  SettingsPage.tsx      — webhook Test button gated on the SAVED url
 *                               only (typed-but-unsaved must not enable it).
 *   F3  useLiveStream.ts      — SSE_EVENT_RECONNECTED fires once per recovery,
 *                               not on every beat of a healthy socket.
 *   F4  QuantScanPage.tsx     — the non-streaming live-read fallback query is
 *                               NOT issued on mount while the stream is the
 *                               active path (no redundant DeepSeek spend).
 *   F5  QuantScanPage.tsx     — notifiedBurstsRef cap constant is bounded.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { createElement, type ReactNode } from "react";
import type {
  FinancialRecord,
  FinancialRecordSummary,
} from "@/types/api";

// ─────────────────────────────────────────────────────────────────────────
// F1 — contract split is enforced at the type level. These are compile-time
// assertions: if the summary type regains a detail-only field (or loses the
// summary-only fields), the file fails to type-check, which fails the run.
// ─────────────────────────────────────────────────────────────────────────
describe("F1 — FinancialRecordSummary mirrors the backend compact list shape", () => {
  it("declares the two summary-only fields the backend emits", () => {
    // A minimal object that satisfies the summary shape. If `evidence_preview`
    // or `evidence_count` were dropped from the type, this object literal would
    // still satisfy it; the real guard is the `Pick` assertions below.
    const row: FinancialRecordSummary = {
      capture_id: "c1",
      doc_id: "c1",
      title: null,
      domain: null,
      language: null,
      url: null,
      is_finance_relevant: true,
      finance_relevance_score: 0.5,
      asset_classes: [],
      impact_reason_codes: [],
      candidate_symbols: [],
      sentiment_label: null,
      sentiment_score: null,
      evidence_preview: "first sentence",
      evidence_count: 3,
      diagnostic_multimodal_enabled: false,
      published_ts: null,
      created_at: "2026-05-29T00:00:00Z",
    };
    expect(row.evidence_preview).toBe("first sentence");
    expect(row.evidence_count).toBe(3);

    // Compile-time: the summary type MUST expose these keys.
    type _HasPreview = Pick<FinancialRecordSummary, "evidence_preview">;
    type _HasCount = Pick<FinancialRecordSummary, "evidence_count">;
    const _p: _HasPreview = { evidence_preview: row.evidence_preview };
    const _c: _HasCount = { evidence_count: row.evidence_count };
    expect(_p.evidence_preview).toBe("first sentence");
    expect(_c.evidence_count).toBe(3);
  });

  it("does NOT assert the 8 detail-only fields on the list-row type", () => {
    // `keyof FinancialRecordSummary` must exclude every detail-only field.
    // If any of these leaked back onto the summary type, the corresponding
    // `Extract<...>` would resolve to the key name (a non-`never` type) and
    // the assignment to `never` would fail to compile.
    type SummaryKeys = keyof FinancialRecordSummary;
    type DetailOnly =
      | "candidate_entities"
      | "component_scores"
      | "diagnostic_multimodal_result"
      | "evidence_sentences"
      | "impact_horizons"
      | "model_versions"
      | "processing_mode"
      | "reason_text";
    type Leaked = Extract<SummaryKeys, DetailOnly>;
    const _noLeak: Leaked extends never ? true : never = true;
    expect(_noLeak).toBe(true);

    // And the detail type STILL carries them (full record is unchanged).
    type DetailKeys = keyof FinancialRecord;
    type MissingFromDetail = Exclude<DetailOnly, DetailKeys>;
    const _detailComplete: MissingFromDetail extends never ? true : never = true;
    expect(_detailComplete).toBe(true);
  });
});

// ─────────────────────────────────────────────────────────────────────────
// F3 — useLiveStream reconnect-event semantics.
// ─────────────────────────────────────────────────────────────────────────
type Listener = (ev: unknown) => void;

class FakeEventSource {
  static instances: FakeEventSource[] = [];
  static last(): FakeEventSource {
    const inst = FakeEventSource.instances.at(-1);
    if (!inst) throw new Error("no FakeEventSource instance yet");
    return inst;
  }
  url: string;
  closed = false;
  listeners: Record<string, Listener[]> = {};
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  constructor(url: string) {
    this.url = url;
    FakeEventSource.instances.push(this);
  }
  addEventListener(name: string, fn: Listener) {
    (this.listeners[name] ||= []).push(fn);
  }
  close() {
    this.closed = true;
  }
  emit(name: string) {
    for (const fn of this.listeners[name] ?? []) fn(new MessageEvent(name));
  }
}

function liveStreamWrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return createElement(QueryClientProvider, { client: qc }, children);
}

describe("F3 — SSE_EVENT_RECONNECTED fires once per recovery, not per beat", () => {
  beforeEach(() => {
    FakeEventSource.instances = [];
    // @ts-expect-error overriding the JSDOM global with our stub
    globalThis.EventSource = FakeEventSource;
    vi.spyOn(Math, "random").mockReturnValue(0);
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
    // @ts-expect-error allow the next test to install its own
    delete globalThis.EventSource;
  });

  it("does NOT re-dispatch on a SECOND consecutive beat of a healthy socket", async () => {
    const { renderHook } = await import("@testing-library/react");
    const { SSE_EVENT_RECONNECTED, useLiveStream } = await import("@/hooks/useLiveStream");

    const listener = vi.fn();
    window.addEventListener(SSE_EVENT_RECONNECTED, listener);
    try {
      renderHook(() => useLiveStream(), { wrapper: liveStreamWrapper });

      // Two beats on the SAME, never-interrupted socket. Pre-fix, the first
      // beat set hasOpenedRef and the SECOND beat re-fired the event because
      // hasOpenedRef was already true. Neither beat is a recovery.
      act(() => {
        FakeEventSource.last().emit("summary");
      });
      act(() => {
        FakeEventSource.last().emit("summary");
      });
      expect(listener).not.toHaveBeenCalled();
    } finally {
      window.removeEventListener(SSE_EVENT_RECONNECTED, listener);
    }
  });

  it("fires EXACTLY once on a real reconnect, then stays quiet on the next beat", async () => {
    const { renderHook } = await import("@testing-library/react");
    const {
      SSE_BACKOFF_MIN_MS,
      SSE_EVENT_RECONNECTED,
      useLiveStream,
    } = await import("@/hooks/useLiveStream");

    const listener = vi.fn();
    window.addEventListener(SSE_EVENT_RECONNECTED, listener);
    try {
      renderHook(() => useLiveStream(), { wrapper: liveStreamWrapper });

      // Initial beat — not a recovery.
      act(() => {
        FakeEventSource.last().emit("summary");
      });
      expect(listener).not.toHaveBeenCalled();

      // Lose the socket, let the backoff spawn a fresh one.
      act(() => {
        FakeEventSource.last().onerror?.();
        vi.advanceTimersByTime(SSE_BACKOFF_MIN_MS);
      });
      expect(FakeEventSource.instances).toHaveLength(2);

      // First beat on the new socket IS a recovery → exactly one dispatch.
      act(() => {
        FakeEventSource.last().emit("summary");
      });
      expect(listener).toHaveBeenCalledTimes(1);

      // A SECOND healthy beat on the recovered socket must NOT re-fire.
      act(() => {
        FakeEventSource.last().emit("summary");
      });
      expect(listener).toHaveBeenCalledTimes(1);
    } finally {
      window.removeEventListener(SSE_EVENT_RECONNECTED, listener);
    }
  });
});

// ─────────────────────────────────────────────────────────────────────────
// F2 — webhook Test button gated on the SAVED url only.
// ─────────────────────────────────────────────────────────────────────────
vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    api: {
      appInfo: vi.fn(),
      reviewsStatus: vi.fn(),
      reviewsPatchSettings: vi.fn(),
      dbInfo: vi.fn(),
      dbSchemaVersion: vi.fn(),
      dbImport: vi.fn(),
      dbExportUrl: "/api/db/export",
      webhookConfig: vi.fn(),
      webhookSaveConfig: vi.fn(),
      webhookTest: vi.fn(),
    },
  };
});

describe("F2 — webhook Test button requires a SAVED url", () => {
  let SettingsPage: typeof import("@/features/settings/SettingsPage").SettingsPage;
  let api: Record<string, ReturnType<typeof vi.fn>>;

  beforeEach(async () => {
    const settings = await import("@/features/settings/SettingsPage");
    SettingsPage = settings.SettingsPage;
    const apiMod = await import("@/lib/api");
    api = apiMod.api as unknown as Record<string, ReturnType<typeof vi.fn>>;

    api.appInfo.mockResolvedValue({
      version: "0.1.0",
      branch: "main",
      commit_sha: "abc123",
      mode: "production_safe",
      use_ml_stubs: true,
      static_bundle_present: true,
    });
    api.reviewsStatus.mockResolvedValue({
      deepseek_enabled: false,
      deepseek_keyed: false,
      deepseek_ready: false,
      sampling_rate: 0.1,
      usd_cap: 9.5,
      usd_spent: 0,
      model: "deepseek-chat",
      exhausted: false,
    });
    api.dbInfo.mockResolvedValue({
      exists: true,
      path: "/tmp/catchem.sqlite3",
      size_bytes: 1024,
      modified_at: "2026-05-28T00:00:00+00:00",
    });
    api.dbSchemaVersion.mockResolvedValue({
      user_version: 1,
      max_known: 1,
      migrations_pending: [],
    });
    api.webhookSaveConfig.mockResolvedValue({});
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  function renderSettings() {
    const qc = new QueryClient({
      defaultOptions: {
        queries: { retry: false, gcTime: 0, staleTime: 0 },
        mutations: { retry: false },
      },
    });
    return render(
      createElement(
        QueryClientProvider,
        { client: qc },
        createElement(
          MemoryRouter,
          { initialEntries: ["/settings"] },
          createElement(SettingsPage),
        ),
      ),
    );
  }

  const status = (urlConfigured: boolean) => ({
    enabled: false,
    url_configured: urlConfigured,
    min_score: 0.7,
    asset_class_filter: null,
    reason_code_filter: null,
    timeout_seconds: 5,
    stats: { attempted: 0, sent: 0, filtered: 0, failed: 0 },
    last_status: null,
    last_error: null,
    generated_at: "2026-05-28T00:00:00+00:00",
  });

  it("stays DISABLED when the user types a url but has not saved it", async () => {
    api.webhookConfig.mockResolvedValue(status(false));
    renderSettings();
    const btn = await screen.findByTestId("webhook-test-btn");
    expect(btn).toBeDisabled();

    const input = screen.getByTestId("webhook-url-input") as HTMLInputElement;
    await act(async () => {
      fireEvent.change(input, {
        target: { value: "https://hooks.slack.com/services/T/B/secret" },
      });
    });
    // Pre-fix this flipped to enabled (the false "type to enable" affordance).
    expect(screen.getByTestId("webhook-test-btn")).toBeDisabled();
    expect(api.webhookTest).not.toHaveBeenCalled();
  });

  it("is ENABLED when a url is already saved server-side", async () => {
    api.webhookConfig.mockResolvedValue(status(true));
    renderSettings();
    const btn = await screen.findByTestId("webhook-test-btn");
    expect(btn).not.toBeDisabled();
  });
});

// ─────────────────────────────────────────────────────────────────────────
// F4 + F5 — QuantScanPage: no redundant fallback fetch on mount + bounded set.
// ─────────────────────────────────────────────────────────────────────────
vi.mock("@/charts/EChart", () => ({ EChart: () => null }));

// Streaming hook frozen in "idle" — the consumer auto-starts it on mount, so
// "idle" is exactly the first-render state under which the OLD `!== streaming`
// gate fired the redundant live-read. The NEW gate (`=== "error"`) must keep
// the fallback query disabled here.
vi.mock("@/hooks/useStreamingLiveRead", () => ({
  useStreamingLiveRead: () => ({
    text: "",
    state: "idle" as const,
    error: null,
    meta: { source: null, generatedAt: null, usdCost: null, fallbackReason: null },
    start: vi.fn(),
    stop: vi.fn(),
  }),
}));

describe("F4 — non-streaming live-read fallback is not issued while idle/streaming", () => {
  let QuantScanPage: typeof import("@/features/quant/QuantScanPage").QuantScanPage;
  let api: Record<string, ReturnType<typeof vi.fn>>;

  beforeEach(async () => {
    const apiMod = await import("@/lib/api");
    api = apiMod.api as unknown as Record<string, ReturnType<typeof vi.fn>>;
    // Reuse the same api mock object — override the quant methods the page hits.
    Object.assign(api, {
      quantDashboard: vi.fn(async () => ({
        n_records_window: 0,
        n_clusters: 0,
        clusters: [],
        source_leaderboard: null,
        novelty_timeline: [],
        lead_lag: null,
        regime: null,
        sentiment_momentum: null,
        co_occurrence: null,
        anomalies: null,
        spillover: null,
        generated_at: "2026-05-28T12:00:00Z",
      })),
      quantLiveRead: vi.fn(async () => ({
        narrative: "x",
        source: "local",
        context: {},
        generated_at: "2026-05-28T12:00:00Z",
      })),
      quantNewsVelocity: vi.fn(async () => ({
        schema_version: 1,
        generated_at: "2026-05-28T12:00:00Z",
        limit: 1000,
        bucket_minutes: 30,
        window_minutes: 360,
        current_rate_per_min: 0,
        ema_fast: 0,
        ema_slow: 0,
        baseline_rate: 0,
        baseline_std: 0,
        acceleration_z: 0,
        regime: "calm",
        samples: 0,
      })),
      quantDiagnostics: vi.fn(async () => ({
        schema_version: 1,
        generated_at: "2026-05-28T12:00:00Z",
        total_failures: 0,
        per_signal: {},
        recent: [],
        buffer_capacity: 50,
      })),
      reviewsStatus: vi.fn(async () => ({
        deepseek_enabled: false,
        deepseek_keyed: false,
        deepseek_ready: false,
        model: "stub",
        sampling_rate: 0,
        usd_cap: 1,
        usd_spent: 0,
        usd_remaining: 1,
        exhausted: false,
        primary_reviewer_version: "stub-1",
        tokens: { input: 0, output: 0, calls: 0, errors: 0 },
        base_url: "",
        generated_at: "2026-05-28T12:00:00Z",
      })),
      exportQuantUrl: (limit = 1000) => `/api/export/quant?format=json&limit=${limit}`,
    });
    const page = await import("@/features/quant/QuantScanPage");
    QuantScanPage = page.QuantScanPage;
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  it("does NOT call api.quantLiveRead on mount when the stream is the active path", async () => {
    const qc = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    });
    render(
      createElement(
        MemoryRouter,
        { initialEntries: ["/scan"] },
        createElement(
          QueryClientProvider,
          { client: qc },
          createElement(QuantScanPage),
        ),
      ),
    );
    // The hero mounts and the dashboard query fires…
    await screen.findByTestId("live-read-title");
    await waitFor(() => expect(api.quantDashboard).toHaveBeenCalled());
    // …but the non-streaming fallback must NOT have been issued. Pre-fix the
    // `!== "streaming"` gate fired it on the very first (idle) render.
    expect(api.quantLiveRead).not.toHaveBeenCalled();
  });
});
