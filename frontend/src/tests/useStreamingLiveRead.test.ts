import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { act, renderHook } from "@testing-library/react";

import { useStreamingLiveRead } from "@/hooks/useStreamingLiveRead";

/**
 * Tests for the streaming live-read hook.
 *
 * EventSource is mocked at the global level so each test can deterministically
 * fire `start` / `chunk` / `done` / `error` listeners and inspect the hook's
 * accumulated buffer.
 */

type Listener = (ev: unknown) => void;

class FakeEventSource {
  static instances: FakeEventSource[] = [];
  static last(): FakeEventSource {
    const inst = FakeEventSource.instances.at(-1);
    if (!inst) throw new Error("no FakeEventSource instance");
    return inst;
  }

  url: string;
  closed = false;
  listeners: Record<string, Listener[]> = {};

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

  emit(name: string, data?: unknown) {
    const evt = new MessageEvent(name, {
      data: data === undefined ? undefined : typeof data === "string" ? data : JSON.stringify(data),
    });
    for (const fn of this.listeners[name] ?? []) fn(evt);
  }

  /** Transport-level error (no payload, like a real `onerror` event). */
  emitTransportError() {
    for (const fn of this.listeners["error"] ?? []) fn(new Event("error"));
  }
}

beforeEach(() => {
  FakeEventSource.instances = [];
  // @ts-expect-error replace the JSDOM global with our stub
  globalThis.EventSource = FakeEventSource;
});

afterEach(() => {
  vi.restoreAllMocks();
  // @ts-expect-error allow next test to install its own
  delete globalThis.EventSource;
});

describe("useStreamingLiveRead", () => {
  it("starts idle and does not open a connection until start() is called", () => {
    const { result } = renderHook(() => useStreamingLiveRead(500));
    expect(result.current.state).toBe("idle");
    expect(result.current.text).toBe("");
    expect(FakeEventSource.instances).toHaveLength(0);
  });

  it("opens the stream with the right URL when start() is called", () => {
    const { result } = renderHook(() => useStreamingLiveRead(1234));
    act(() => result.current.start());
    expect(FakeEventSource.instances).toHaveLength(1);
    expect(FakeEventSource.last().url).toBe("/api/quant/live-read-stream?limit=1234");
    expect(result.current.state).toBe("streaming");
  });

  it("accumulates chunks and lands on 'done' with the right meta", () => {
    const { result } = renderHook(() => useStreamingLiveRead(1000));
    act(() => result.current.start());
    const es = FakeEventSource.last();

    act(() => {
      es.emit("start", { limit: 1000, source: "deepseek", generated_at: "2026-05-28T00:00:00Z" });
    });
    expect(result.current.meta.source).toBe("deepseek");
    expect(result.current.meta.generatedAt).toBe("2026-05-28T00:00:00Z");

    act(() => {
      es.emit("chunk", { text: "**Dominant story:** " });
      es.emit("chunk", { text: "Tech rotation underway. " });
      es.emit("chunk", { text: "Risk in mega-caps." });
    });
    expect(result.current.text).toContain("Dominant story");
    expect(result.current.text).toContain("Tech rotation");
    expect(result.current.text).toContain("Risk in mega-caps");

    act(() => {
      es.emit("done", { ok: true, source: "deepseek", usd_cost: 0.00042 });
    });
    expect(result.current.state).toBe("done");
    expect(result.current.meta.usdCost).toBeCloseTo(0.00042, 6);
    expect(es.closed).toBe(true);
  });

  it("flips to 'error' on a transport-level failure", () => {
    const { result } = renderHook(() => useStreamingLiveRead(200));
    act(() => result.current.start());
    const es = FakeEventSource.last();
    act(() => {
      es.emitTransportError();
    });
    expect(result.current.state).toBe("error");
    expect(result.current.error).toBe("Stream interrupted");
    expect(es.closed).toBe(true);
  });

  it("keeps state 'done' if a phantom transport error fires AFTER the done event", () => {
    const { result } = renderHook(() => useStreamingLiveRead(200));
    act(() => result.current.start());
    const es = FakeEventSource.last();
    act(() => {
      es.emit("chunk", { text: "hello " });
      es.emit("done", { ok: true, source: "local" });
    });
    expect(result.current.state).toBe("done");
    act(() => {
      es.emitTransportError();
    });
    expect(result.current.state).toBe("done");
    expect(result.current.error).toBeNull();
  });

  it("calling start() twice closes the previous stream and resets the buffer", () => {
    const { result } = renderHook(() => useStreamingLiveRead(500));
    act(() => result.current.start());
    const first = FakeEventSource.last();
    act(() => {
      first.emit("chunk", { text: "old narrative" });
    });
    expect(result.current.text).toContain("old narrative");

    act(() => result.current.start());
    expect(first.closed).toBe(true);
    expect(FakeEventSource.instances).toHaveLength(2);
    // Buffer cleared for the new stream.
    expect(result.current.text).toBe("");
    expect(result.current.state).toBe("streaming");
  });

  it("stop() closes the stream and returns to idle", () => {
    const { result } = renderHook(() => useStreamingLiveRead(500));
    act(() => result.current.start());
    const es = FakeEventSource.last();
    expect(result.current.state).toBe("streaming");
    act(() => result.current.stop());
    expect(result.current.state).toBe("idle");
    expect(es.closed).toBe(true);
  });

  it("ignores malformed chunk payloads without crashing", () => {
    const { result } = renderHook(() => useStreamingLiveRead(500));
    act(() => result.current.start());
    const es = FakeEventSource.last();
    act(() => {
      // Emit a "chunk" whose data is not valid JSON.
      for (const fn of es.listeners["chunk"] ?? []) fn(new MessageEvent("chunk", { data: "{not json" }));
      es.emit("chunk", { text: "good chunk" });
    });
    expect(result.current.text).toBe("good chunk");
    expect(result.current.state).toBe("streaming");
  });
});
