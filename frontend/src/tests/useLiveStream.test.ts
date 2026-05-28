import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { act, renderHook } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createElement, type ReactNode } from "react";
import {
  LIVE_FRESH_SECONDS,
  LIVE_STALE_SECONDS,
  SSE_BACKOFF_JITTER_MS,
  SSE_BACKOFF_MAX_MS,
  SSE_BACKOFF_MIN_MS,
  SSE_EVENT_RECONNECTED,
  SSE_EVENT_SIDECAR_DOWN,
  SSE_EVENT_SIDECAR_RECOVERED,
  useLiveStream,
} from "@/hooks/useLiveStream";

/**
 * Round 6 Bug 2 regression: the original hook closed SSE on first error
 * and never retried. These tests pin the new behavior — periodic reconnect
 * with exponential backoff capped at SSE_BACKOFF_MAX_MS.
 *
 * We replace `globalThis.EventSource` with a controllable stub so each
 * test can deterministically simulate open/error events and inspect how
 * many EventSource instances the hook spawned.
 */

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

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return createElement(QueryClientProvider, { client: qc }, children);
}

beforeEach(() => {
  FakeEventSource.instances = [];
  // @ts-expect-error overriding the JSDOM global with our stub
  globalThis.EventSource = FakeEventSource;
  // Pin jitter to 0 so backoff cadence is deterministic. The actual
  // jitter math is independently exercised in the dedicated jitter
  // test below by toggling Math.random for that test only.
  vi.spyOn(Math, "random").mockReturnValue(0);
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
  // @ts-expect-error allow next test to install its own
  delete globalThis.EventSource;
});

describe("useLiveStream", () => {
  it("connects once on mount", () => {
    renderHook(() => useLiveStream(), { wrapper });
    expect(FakeEventSource.instances).toHaveLength(1);
    expect(FakeEventSource.last().url).toBe("/ui/stream");
  });

  it("stays connecting on socket open until the first data beat arrives", () => {
    const { result } = renderHook(() => useLiveStream(), { wrapper });
    act(() => {
      FakeEventSource.last().onopen?.();
    });
    expect(result.current.status).toBe("connecting");
    expect(result.current.lastBeatAt).toBeNull();

    act(() => {
      FakeEventSource.last().emit("summary");
    });
    expect(result.current.status).toBe("open");
    expect(result.current.lastBeatAt).not.toBeNull();
  });

  it("invalidates symbol queries when live summary or tick events arrive", () => {
    const spy = vi.spyOn(QueryClient.prototype, "invalidateQueries");

    renderHook(() => useLiveStream(), { wrapper });
    act(() => {
      FakeEventSource.last().emit("summary");
    });
    expect(spy).toHaveBeenCalledWith({ queryKey: ["top-symbols"] });
    expect(spy).toHaveBeenCalledWith(expect.objectContaining({ predicate: expect.any(Function) }));

    spy.mockClear();
    act(() => {
      FakeEventSource.last().emit("tick");
    });
    expect(spy).toHaveBeenCalledWith({ queryKey: ["top-symbols"] });
    expect(spy).toHaveBeenCalledWith(expect.objectContaining({ predicate: expect.any(Function) }));
  });

  it("switches to polling and schedules a reconnect after onerror", () => {
    const { result } = renderHook(() => useLiveStream(), { wrapper });
    act(() => {
      FakeEventSource.last().onerror?.();
    });
    expect(FakeEventSource.last().closed).toBe(true);
    expect(result.current.status).toBe("polling");

    // No reconnect yet (timer hasn't fired).
    expect(FakeEventSource.instances).toHaveLength(1);

    // Fire the first backoff window (2s) — fresh ES should be spawned.
    act(() => {
      vi.advanceTimersByTime(SSE_BACKOFF_MIN_MS);
    });
    expect(FakeEventSource.instances).toHaveLength(2);
    // Status enters 'connecting' on the new attempt.
    expect(result.current.status).toBe("connecting");
  });

  it("doubles the backoff on each consecutive failure and caps at MAX", () => {
    renderHook(() => useLiveStream(), { wrapper });

    // Sequence: 2s, 4s, 8s, 16s, 32s, 60s (cap), 60s, 60s
    const expected = [
      SSE_BACKOFF_MIN_MS,
      SSE_BACKOFF_MIN_MS * 2,
      SSE_BACKOFF_MIN_MS * 4,
      SSE_BACKOFF_MIN_MS * 8,
      SSE_BACKOFF_MIN_MS * 16,
      SSE_BACKOFF_MAX_MS, // capped (would be 64s)
      SSE_BACKOFF_MAX_MS, // still capped
    ];

    for (let i = 0; i < expected.length; i++) {
      const before = FakeEventSource.instances.length;
      // Fail the current attempt.
      act(() => {
        FakeEventSource.last().onerror?.();
      });
      // Advance one tick less than the expected delay — must not reconnect.
      act(() => {
        vi.advanceTimersByTime(expected[i] - 1);
      });
      expect(FakeEventSource.instances).toHaveLength(before);
      // Now cross the threshold — a new ES must appear.
      act(() => {
        vi.advanceTimersByTime(1);
      });
      expect(FakeEventSource.instances.length).toBe(before + 1);
    }
  });

  it("resets backoff after a successful open", () => {
    renderHook(() => useLiveStream(), { wrapper });

    // Burn three failures to push backoff to 16s (start 2 → next 4 → next 8 → next 16).
    for (let i = 0; i < 3; i++) {
      act(() => {
        FakeEventSource.last().onerror?.();
        // Trigger the scheduled reconnect.
        vi.advanceTimersByTime(SSE_BACKOFF_MAX_MS);
      });
    }
    const beforeRecovery = FakeEventSource.instances.length;

    // Successful open resets backoff.
    act(() => {
      FakeEventSource.last().onopen?.();
    });

    // Next failure must wait only the MIN window, not the previously-walked cap.
    act(() => {
      FakeEventSource.last().onerror?.();
    });
    // Just before MIN — no reconnect.
    act(() => {
      vi.advanceTimersByTime(SSE_BACKOFF_MIN_MS - 1);
    });
    expect(FakeEventSource.instances.length).toBe(beforeRecovery);
    // Cross the MIN threshold — reconnect fires.
    act(() => {
      vi.advanceTimersByTime(1);
    });
    expect(FakeEventSource.instances.length).toBe(beforeRecovery + 1);
  });

  it("cleans up timers + the active EventSource on unmount", () => {
    const { unmount } = renderHook(() => useLiveStream(), { wrapper });
    act(() => {
      FakeEventSource.last().onerror?.();
    });
    const es = FakeEventSource.last();
    unmount();
    expect(es.closed).toBe(true);
    // After unmount, the scheduled reconnect must not spawn another ES.
    act(() => {
      vi.advanceTimersByTime(SSE_BACKOFF_MAX_MS * 2);
    });
    expect(FakeEventSource.instances).toHaveLength(1);
  });

  // ── v25 regressions ────────────────────────────────────────────────────

  it("adds jitter to the scheduled backoff window", () => {
    // Pin jitter at the ceiling — reconnect must wait BACKOFF + JITTER,
    // not just BACKOFF, before spawning a fresh EventSource. The base
    // `beforeEach` already installed a Math.random spy returning 0; we
    // re-spy here so the runtime instance is fresh after `restoreAllMocks`
    // is consulted on the previous teardown.
    vi.spyOn(Math, "random").mockReturnValue(0.999);
    renderHook(() => useLiveStream(), { wrapper });

    act(() => {
      FakeEventSource.last().onerror?.();
    });
    // The plain backoff (no jitter) would have fired by now…
    act(() => {
      vi.advanceTimersByTime(SSE_BACKOFF_MIN_MS);
    });
    expect(FakeEventSource.instances).toHaveLength(1);
    // …but the full jittered window finishes within +500ms of MIN.
    act(() => {
      vi.advanceTimersByTime(SSE_BACKOFF_JITTER_MS);
    });
    expect(FakeEventSource.instances).toHaveLength(2);
  });

  it("pauses reconnect attempts while sidecar-down is signalled", () => {
    renderHook(() => useLiveStream(), { wrapper });

    // Sidecar goes down — close the current ES and stop arming retries.
    act(() => {
      window.dispatchEvent(new Event(SSE_EVENT_SIDECAR_DOWN));
    });
    // No new EventSource should appear even if many backoff windows elapse.
    act(() => {
      vi.advanceTimersByTime(SSE_BACKOFF_MAX_MS * 4);
    });
    expect(FakeEventSource.instances).toHaveLength(1);
    expect(FakeEventSource.last().closed).toBe(true);

    // Recovery event — should reconnect immediately, no waiting.
    act(() => {
      window.dispatchEvent(new Event(SSE_EVENT_SIDECAR_RECOVERED));
    });
    expect(FakeEventSource.instances).toHaveLength(2);
    expect(FakeEventSource.last().closed).toBe(false);
  });

  it("emits catchem:sse-reconnected after a recovery beat, not on the very first beat", () => {
    const listener = vi.fn();
    window.addEventListener(SSE_EVENT_RECONNECTED, listener);

    renderHook(() => useLiveStream(), { wrapper });

    // First beat is initial connect — not a reconnect.
    act(() => {
      FakeEventSource.last().emit("summary");
    });
    expect(listener).not.toHaveBeenCalled();

    // Lose the connection and let the backoff fire a fresh ES.
    act(() => {
      FakeEventSource.last().onerror?.();
      vi.advanceTimersByTime(SSE_BACKOFF_MIN_MS);
    });
    expect(FakeEventSource.instances).toHaveLength(2);

    // First beat on the new socket — this IS a recovery beat.
    act(() => {
      FakeEventSource.last().emit("summary");
    });
    expect(listener).toHaveBeenCalledTimes(1);

    window.removeEventListener(SSE_EVENT_RECONNECTED, listener);
  });

  it("exposes stalenessSeconds that grows once the last beat ages past the FRESH bar", () => {
    const { result } = renderHook(() => useLiveStream(), { wrapper });

    // Pre-beat — no staleness measurement yet.
    expect(result.current.stalenessSeconds).toBeNull();

    act(() => {
      FakeEventSource.last().emit("summary");
    });
    // First tick of the 1s staleness interval — beat is "now", so 0s.
    act(() => {
      vi.advanceTimersByTime(1_000);
    });
    expect(result.current.stalenessSeconds).toBeGreaterThanOrEqual(0);
    expect(result.current.stalenessSeconds).toBeLessThan(LIVE_FRESH_SECONDS);

    // Walk the timer past the STALE bar.
    act(() => {
      vi.advanceTimersByTime((LIVE_STALE_SECONDS + 1) * 1_000);
    });
    expect(result.current.stalenessSeconds).toBeGreaterThanOrEqual(LIVE_STALE_SECONDS);
  });
});
