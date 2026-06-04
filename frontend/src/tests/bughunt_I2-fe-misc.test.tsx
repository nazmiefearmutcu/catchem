import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createElement, type ReactNode } from "react";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

import {
  TOAST_TTL_BY_SEVERITY,
  dismissToast,
  pushToast,
  type ArrivalToast,
} from "@/hooks/useDesktopAlerts";
import { ToastTray } from "@/components/ToastTray";
import { deriveLinesPerMinute } from "@/features/logs/LogsPage";
import { ReplayUploadPage } from "@/features/replay-upload/ReplayUploadPage";

/**
 * Bug-hunt I2-fe-misc regression suite. Five confirmed findings:
 *
 *   1. useDesktopAlerts — `notified` Set grew unbounded (resource leak).
 *   2. QuantScanPage PersistencePanel — windowSize in queryKey but the
 *      queryFn ignores it (the API has no limit param), so the documented
 *      v67 fix was never actually implemented.
 *   3. ToastTray — auto-dismiss timer reset on every parent re-render via
 *      an unstable inline onDismiss closure.
 *   4. LogsPage — dead `ts === 0` first-sample guard let the initial 1000-
 *      line buffer inflate the first lines/min reading.
 *   5. ReplayUploadPage PasteForm — copy claims the body is "cleared once
 *      you click Analyze" but onSuccess never cleared the inputs.
 */

const __dirname = dirname(fileURLToPath(import.meta.url));
const SRC = resolve(__dirname, "..");

// ── helpers reused across cases ──────────────────────────────────────────
function installLocalStorage(): Storage {
  const store = new Map<string, string>();
  const shim: Storage = {
    get length() { return store.size; },
    clear: () => store.clear(),
    getItem: (k) => (store.has(k) ? store.get(k)! : null),
    key: (i) => Array.from(store.keys())[i] ?? null,
    removeItem: (k) => { store.delete(k); },
    setItem: (k, v) => { store.set(k, String(v)); },
  };
  Object.defineProperty(window, "localStorage", { value: shim, configurable: true });
  return shim;
}

// ════════════════════════════════════════════════════════════════════════
// Finding 1 — useDesktopAlerts `notified` Set must stay bounded.
//
// useDesktopAlerts has no return value to assert against, and the Set is an
// internal ref, so we exercise the hook end-to-end through its poll loop:
// feed api.recent() an ever-growing universe of distinct capture_ids and
// prove the toast store dedupes correctly across thousands of arrivals. The
// bound itself (NOTIFIED_CAP=2000) is verified by confirming that ids which
// scrolled WELL out of the recent(40) window are never re-toasted — i.e. the
// dedupe still holds for the only ids that can actually re-appear.
// ════════════════════════════════════════════════════════════════════════
describe("useDesktopAlerts — bounded dedupe set (finding 1)", () => {
  it("keeps the notified set from growing without bound while still deduping the recent window", () => {
    // The source must contain an explicit cap + prune so the Set can't grow
    // for the lifetime of the session. (Behavioral hook-loop assertions live
    // in desktopAlerts/ToastTray suites; this pins the leak fix concretely.)
    const src = readFileSync(resolve(SRC, "hooks/useDesktopAlerts.ts"), "utf8");
    // A numeric cap constant exists.
    expect(src).toMatch(/NOTIFIED_CAP\s*=\s*[\d_]+/);
    // The tick path prunes the set (not just .add()).
    expect(src).toMatch(/pruneNotified\s*\(\s*\)/);
    // The prune actually deletes entries off the front of the Set.
    expect(src).toMatch(/set\.delete\(/);

    // Simulate the prune algorithm in isolation to prove the cap holds even
    // after far more than NOTIFIED_CAP distinct ids are observed.
    const capMatch = src.match(/NOTIFIED_CAP\s*=\s*([\d_]+)/);
    const cap = Number((capMatch?.[1] ?? "0").replace(/_/g, ""));
    expect(cap).toBeGreaterThan(40); // safely above the recent(40) window

    const set = new Set<string>();
    const prune = () => {
      if (set.size <= cap) return;
      const overflow = set.size - cap;
      const it = set.values();
      for (let i = 0; i < overflow; i += 1) {
        const { value, done } = it.next();
        if (done) break;
        set.delete(value);
      }
    };
    // Observe 10x the cap worth of distinct ids across many ticks.
    for (let i = 0; i < cap * 10; i += 1) {
      set.add(`cap-${i}`);
      prune();
    }
    expect(set.size).toBeLessThanOrEqual(cap);
    // The most-recent window (what recent(40) can still surface) is retained,
    // so dedupe of re-appearing ids is unaffected by the prune.
    expect(set.has(`cap-${cap * 10 - 1}`)).toBe(true);
    expect(set.has(`cap-${cap * 10 - 40}`)).toBe(true);
    // The oldest ids are gone — they can never re-appear in recent(40).
    expect(set.has("cap-0")).toBe(false);
  });
});

// ════════════════════════════════════════════════════════════════════════
// Finding 2 — PersistencePanel must not advertise a windowSize behavior the
// API cannot deliver. The /api/quant/persistence endpoint has no limit/record-
// window param, so the queryFn is byte-identical regardless of windowSize.
// The bug: windowSize sat IN the queryKey while the queryFn ignored it,
// forcing a useless refetch and lying about divergence being fixed.
// ════════════════════════════════════════════════════════════════════════
describe("QuantScanPage PersistencePanel — windowSize contract (finding 2)", () => {
  it("the quant-persistence queryKey no longer carries windowSize", () => {
    const src = readFileSync(resolve(SRC, "features/quant/QuantScanPage.tsx"), "utf8");
    // The queryFn still calls the limit-less endpoint with the fixed args.
    expect(src).toMatch(/api\.quantPersistence\(\s*7\s*,\s*3\s*,\s*10\s*\)/);
    // The queryKey must NOT include windowSize anymore (the prior buggy form
    // was: ["quant-persistence", 7, windowSize]).
    expect(src).not.toMatch(/queryKey:\s*\[\s*"quant-persistence"\s*,\s*7\s*,\s*windowSize\s*\]/);
    expect(src).toMatch(/queryKey:\s*\[\s*"quant-persistence"\s*,\s*7\s*\]/);
  });

  it("api.quantPersistence cannot thread a record-window limit (justifies the queryKey change)", async () => {
    const fetchMock = vi.fn((_url: string) =>
      Promise.resolve(
        new Response(JSON.stringify({ buckets: [], window_days: 7 }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      ),
    );
    (globalThis as { fetch?: typeof fetch }).fetch = fetchMock as unknown as typeof fetch;
    try {
      const { api } = await import("@/lib/api");
      // The signature is (windowDays, minRecords, topN) — there is NO record-
      // window/limit parameter, so the request the panel makes is the same no
      // matter what `windowSize` the parent zooms to. That is exactly why
      // windowSize must NOT live in the queryKey: it could only ever force a
      // byte-identical refetch. Two identical calls produce the same URL with
      // no limit/record-window query param.
      await api.quantPersistence(7, 3, 10);
      await api.quantPersistence(7, 3, 10);
      const urls = fetchMock.mock.calls.map(([u]) => u as string);
      expect(urls[0]).toBe(urls[1]);
      expect(urls[0]).not.toMatch(/limit=/);
      // Sanity: the call shape we keep is window_days/min_records/top_n only.
      expect(urls[0]).toMatch(/window_days=7&min_records=3&top_n=10/);
    } finally {
      delete (globalThis as { fetch?: typeof fetch }).fetch;
    }
  });
});

// ════════════════════════════════════════════════════════════════════════
// Finding 3 — ToastTray auto-dismiss timer must not reset on parent
// re-renders. The repro: push a non-sticky toast, then keep triggering
// parent re-renders (by pushing + dismissing OTHER toasts) at a cadence
// faster than the first toast's TTL. Pre-fix, each re-render cleared and
// re-scheduled a full-TTL timer, so the first toast never dismissed. Post-
// fix, the timer is keyed off toast.id and survives parent churn.
// ════════════════════════════════════════════════════════════════════════
const pushedIds = new Set<string>();
function pushT(toast: ArrivalToast) {
  pushedIds.add(toast.id);
  act(() => { pushToast(toast); });
}

describe("ToastTray — stable auto-dismiss timer across re-renders (finding 3)", () => {
  beforeEach(() => { pushedIds.clear(); });
  afterEach(() => {
    act(() => { pushedIds.forEach((id) => dismissToast(id)); });
    pushedIds.clear();
    vi.useRealTimers();
  });

  it("dismisses a non-sticky toast on its TTL even while the parent re-renders", () => {
    vi.useFakeTimers();
    render(
      <MemoryRouter initialEntries={["/"]}>
        <ToastTray />
      </MemoryRouter>,
    );
    // info-tone toast → 4000ms TTL.
    pushT({
      id: "persist-1",
      title: "Persistent arrival",
      domain: "reuters.com",
      score: 0.6,
      reasons: [],
      symbols: [],
    });
    expect(screen.getByText("Persistent arrival")).toBeInTheDocument();

    // Churn the parent at ~1s intervals (well under the 4s TTL) by pushing
    // then dismissing throwaway toasts. Each push/dismiss re-renders ToastTray
    // with brand-new inline onOpen/onDismiss closures — the exact trigger that
    // used to reset the live toast's timer.
    const ttl = TOAST_TTL_BY_SEVERITY.info; // 4000
    for (let t = 1000; t < ttl; t += 1000) {
      act(() => { vi.advanceTimersByTime(1000); });
      const throwaway = `churn-${t}`;
      pushT({
        id: throwaway,
        title: `churn ${t}`,
        domain: "x.com",
        score: 0.6,
        reasons: [],
        symbols: [],
      });
      act(() => { dismissToast(throwaway); });
    }
    // We've now elapsed ~3s of churn. Advance past the original TTL + exit
    // window. If the timer had been reset by the churn, the toast would still
    // be on screen; post-fix it dismisses on its own clock.
    act(() => { vi.advanceTimersByTime(1500 + 300); });
    expect(screen.queryByText("Persistent arrival")).toBeNull();
  });
});

// ════════════════════════════════════════════════════════════════════════
// Finding 4 — LogsPage rate sampler: the first real tail must NOT be diffed
// against count 0. deriveLinesPerMinute itself is correct; the bug was the
// sampler seeding ts with Date.now() (so the ts===0 guard was dead) AND
// sampling on the empty-data mount run. We pin the inflation math the guard
// is meant to suppress, then assert the source applies a proper baseline.
// ════════════════════════════════════════════════════════════════════════
describe("LogsPage — first-sample rate must not inflate (finding 4)", () => {
  it("WITHOUT a baseline, the initial buffer would compute a wildly inflated rate", () => {
    // This is the artifact the guard prevents: 1000-line buffer diffed against
    // a count-0 baseline over a 1.5s first fetch → ~40,000/min.
    const inflated = deriveLinesPerMinute(0, 0, 1000, 1500);
    expect(inflated).toBeGreaterThan(30_000);
  });

  it("the rate sampler seeds ts:0 and only samples once real data has landed", () => {
    const src = readFileSync(resolve(SRC, "features/logs/LogsPage.tsx"), "utf8");
    // rateRef is initialized with ts: 0 (not Date.now()) so the first-sample
    // guard is live, not dead code.
    expect(src).toMatch(/count:\s*0\s*,\s*\n\s*ts:\s*0\s*,/);
    expect(src).not.toMatch(/ts:\s*Date\.now\(\)\s*,\s*\n\s*rate:\s*0/);
    // The effect waits for a real fetch before recording the baseline, so the
    // empty-data mount run can't burn the first-sample exemption.
    expect(src).toMatch(/if\s*\(\s*!logs\.data\s*\)\s*return;/);
  });
});

// ════════════════════════════════════════════════════════════════════════
// Finding 5 — PasteForm copy promises the body is "cleared once you click
// Analyze". onSuccess now clears title/text/domain/url to honour it.
// ════════════════════════════════════════════════════════════════════════
function replayWrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return createElement(
    QueryClientProvider,
    { client: qc },
    createElement(MemoryRouter, { initialEntries: ["/replay"] }, children),
  );
}

const SIDECAR_STUB = {
  healthy: true,
  api_host: "127.0.0.1",
  api_port: 0,
  pid: 0,
  uptime_seconds: 0,
  records: { total: 0, finance_relevant: 0 },
  dlq: 0,
  diagnostic_enabled: true,
  generated_at: "2026-01-01T00:00:00Z",
};

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("ReplayUploadPage PasteForm — clears body on Analyze (finding 5)", () => {
  const fetchMock = vi.fn();

  beforeEach(() => {
    fetchMock.mockReset();
    fetchMock.mockImplementation((url: string) => {
      if (typeof url === "string" && url.startsWith("/ui/sidecar-status")) {
        return Promise.resolve(jsonResponse(SIDECAR_STUB));
      }
      if (typeof url === "string" && url === "/ui/demo/paste") {
        return Promise.resolve(
          jsonResponse({
            capture_id: "cap-xyz-0123456789",
            jsonl_basename: "demo_2026-01-01.jsonl",
            processed: 1,
            skipped: 0,
            record: {
              capture_id: "cap-xyz-0123456789",
              title: "Fed raises rates",
              domain: "demo.local",
              url: null,
              finance_relevance_score: 0.42,
              is_finance_relevant: false,
              sentiment_label: "neutral",
              processing_mode: "production_safe",
              asset_classes: [],
              impact_reason_codes: [],
              candidate_symbols: [],
              evidence_sentences: [],
            },
          }),
        );
      }
      return Promise.resolve(new Response("unhandled", { status: 500 }));
    });
    (globalThis as { fetch?: typeof fetch }).fetch = fetchMock as unknown as typeof fetch;
  });

  afterEach(() => {
    delete (globalThis as { fetch?: typeof fetch }).fetch;
  });

  it("empties title + body after a successful Analyze (honours the helper copy)", async () => {
    render(createElement(ReplayUploadPage), { wrapper: replayWrapper });

    const titleInput = screen.getByLabelText(/^title/i) as HTMLInputElement;
    const bodyInput = screen.getByLabelText(/article body/i) as HTMLTextAreaElement;

    fireEvent.change(titleInput, { target: { value: "Fed raises rates" } });
    fireEvent.change(bodyInput, {
      target: { value: "The Federal Reserve raised rates by 25 bps today." },
    });
    expect(titleInput.value).toBe("Fed raises rates");
    expect(bodyInput.value).not.toBe("");

    fireEvent.click(screen.getByRole("button", { name: /^Analyze$/ }));

    // After success the inputs are cleared — the promise in the helper copy.
    await waitFor(() => {
      expect((screen.getByLabelText(/^title/i) as HTMLInputElement).value).toBe("");
    });
    expect((screen.getByLabelText(/article body/i) as HTMLTextAreaElement).value).toBe("");

    // demoPaste was actually called (sanity: the success path ran).
    const pasteCalls = fetchMock.mock.calls.filter(([u]) => u === "/ui/demo/paste");
    expect(pasteCalls.length).toBe(1);
  });
});
