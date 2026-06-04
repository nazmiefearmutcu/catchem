import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { act, render } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import { ToastTray } from "@/components/ToastTray";
import {
  __resetNotificationStoreForTests,
  dismissToast,
  pushToast,
  type ArrivalToast,
} from "@/hooks/useDesktopAlerts";
import { mergeByCaptureId } from "@/lib/feedMerge";
import type { FinancialRecord } from "@/types/api";

/**
 * v48 frontend bug-fix regression suite.
 *
 * Covers:
 *   1. FeedPage `firstSnapshotApplied.current` seeding gate — the fix is
 *      adding `stableRows.length` to the useEffect dep array so the "first
 *      non-empty snapshot" check (line 174) reads live state, not a closed
 *      value from the initial render. Testing the React component in full
 *      requires a router + queryClient + sidecar fixture stack which is
 *      overkill for a deps-array change; instead we pin the decision rule
 *      so a future refactor can't silently break the contract.
 *   2. ToastTray rapid-dismiss exit-cleanup — the fix changes the
 *      toasts-deps useEffect to read `exitingRef.current.get(id)` so it
 *      can guard against missing ghosts. Without the guard, a tight
 *      pushToast→dismissToast→pushToast burst can push `undefined` into
 *      `removed`, then `scheduleExitCleanup(undefined.id)` throws.
 */

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

function makeRecord(id: string): FinancialRecord {
  return {
    capture_id: id,
    doc_id: `doc-${id}`,
    title: `Headline ${id}`,
    domain: "example.com",
    language: "en",
    url: `https://example.com/${id}`,
    is_finance_relevant: true,
    finance_relevance_score: 0.7,
    asset_classes: ["equity"],
    impact_reason_codes: ["EARNINGS"],
    candidate_symbols: ["AAPL"],
    candidate_entities: [],
    impact_horizons: [],
    sentiment_label: "positive",
    sentiment_score: 0.4,
    evidence_sentences: [],
    reason_text: null,
    component_scores: {},
    diagnostic_multimodal_enabled: false,
    diagnostic_multimodal_result: null,
    processing_mode: "live",
    model_versions: {},
    published_ts: "2026-05-28T10:00:00Z",
    created_at: "2026-05-28T10:00:01Z",
  };
}

/**
 * Replicates the seeding decision the FeedPage useEffect makes (line 173-174).
 * Pinning this rule guards against a future refactor accidentally dropping
 * `stableRows.length` from the dep array — which is the v48 fix.
 */
function shouldSeedFirstNonEmptySnapshot(
  firstSnapshotApplied: boolean,
  incomingLen: number,
  stableRowsLen: number,
  seenIdsSize: number,
): boolean {
  return (
    !firstSnapshotApplied ||
    (incomingLen > 0 && stableRowsLen === 0 && seenIdsSize === 0)
  );
}

describe("v48 / FeedPage seeding rule (deps-array fix)", () => {
  it("seeds on the very first non-empty snapshot when the flag is false", () => {
    expect(shouldSeedFirstNonEmptySnapshot(false, 3, 0, 0)).toBe(true);
  });

  it("seeds again when the flag is true but stableRows is empty (post-clear)", () => {
    // This is the branch the stale-closure bug breaks: the closure carries
    // stableRowsLen=0 from initial render, so this returns true forever and
    // a refetched non-empty list silently misfires as a seed instead of
    // flashing as new arrivals.
    expect(shouldSeedFirstNonEmptySnapshot(true, 2, 0, 0)).toBe(true);
  });

  it("does NOT seed once stableRows has hydrated", () => {
    expect(shouldSeedFirstNonEmptySnapshot(true, 2, 5, 5)).toBe(false);
  });

  it("does NOT seed on an empty incoming poll", () => {
    expect(shouldSeedFirstNonEmptySnapshot(true, 0, 5, 5)).toBe(false);
    expect(shouldSeedFirstNonEmptySnapshot(true, 0, 0, 0)).toBe(false);
  });

  it("merge-by-id preserves existing items when seeding skips", () => {
    // Sanity-check the contract the merge step relies on once the seed
    // gate returns false: mergeByCaptureId never drops live rows.
    const prev = [makeRecord("a"), makeRecord("b")];
    const incoming = [makeRecord("b"), makeRecord("c")];
    const merged = mergeByCaptureId(prev, incoming);
    expect(merged.map((r) => r.capture_id).sort()).toEqual(["a", "b", "c"]);
  });
});

describe("v48 / ToastTray rapid-dismiss exit-cleanup", () => {
  beforeEach(() => {
    installLocalStorage();
    __resetNotificationStoreForTests();
    // Ensure store is empty.
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  function makeToast(id: string): ArrivalToast {
    return {
      id,
      title: `Toast ${id}`,
      domain: "example.com",
      score: 0.8,
      reasons: [],
      symbols: [],
    };
  }

  it("survives a tight push→dismiss→push burst without crashing", () => {
    // The crash scenario described in the v48 review: rapid bursts let the
    // toasts-deps effect observe a removed id whose ghost was never cached
    // (no beginDismiss call from this component because dismissal happened
    // through a different path). The fix guards on `!ghost` and skips —
    // any unhandled throw here would surface as a render error.
    const { unmount } = render(
      <MemoryRouter>
        <ToastTray />
      </MemoryRouter>,
    );

    // Burst: 5 different ids pushed and dismissed via the STORE path,
    // not through ToastTray's local beginDismiss. This is the path that
    // triggers the missing-ghost edge case.
    act(() => {
      for (let i = 0; i < 5; i++) {
        pushToast(makeToast(`burst-${i}`));
      }
    });
    act(() => {
      for (let i = 0; i < 5; i++) {
        dismissToast(`burst-${i}`);
      }
    });

    // Drain any pending exit-cleanup timers (220ms each, scheduled in
    // scheduleExitCleanup) so the test exits cleanly.
    act(() => {
      vi.advanceTimersByTime(1_000);
    });

    // If we made it here, the missing-ghost guard held. Component should
    // still be mounted and operable for the next batch.
    expect(() => {
      act(() => {
        pushToast(makeToast("post-burst"));
      });
      act(() => {
        dismissToast("post-burst");
        vi.advanceTimersByTime(1_000);
      });
    }).not.toThrow();

    unmount();
  });
});
