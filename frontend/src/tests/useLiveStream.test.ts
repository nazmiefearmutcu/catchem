import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { act, renderHook } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createElement, type ReactNode } from "react";
import {
  SSE_BACKOFF_MAX_MS,
  SSE_BACKOFF_MIN_MS,
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
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
  // @ts-expect-error allow next test to install its own
  delete globalThis.EventSource;
});

describe("useLiveStream", () => {
  it("connects once on mount", () => {
    renderHook(() => useLiveStream(), { wrapper });
    expect(FakeEventSource.instances).toHaveLength(1);
    expect(FakeEventSource.last().url).toBe("/ui/stream");
  });

  it("transitions to 'open' when the stream fires open", () => {
    const { result } = renderHook(() => useLiveStream(), { wrapper });
    act(() => {
      FakeEventSource.last().onopen?.();
    });
    expect(result.current.status).toBe("open");
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
});
