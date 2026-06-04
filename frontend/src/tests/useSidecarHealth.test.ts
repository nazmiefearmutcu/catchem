import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { act, renderHook } from "@testing-library/react";
import {
  SIDECAR_DEGRADED_INTERVAL_MS,
  SIDECAR_OK_INTERVAL_MS,
  useSidecarHealth,
} from "@/hooks/useSidecarHealth";
import { resetBootTokenCacheForTests } from "@/lib/bootToken";

/**
 * Pins the /healthz polling contract behind <SidecarBanner>:
 *   - Healthy 200 → state="ok", lazy 15s cadence.
 *   - Single failure → "reconnecting" (no banner upgrade yet).
 *   - Two consecutive failures → "down" (banner upgrades to bad/red).
 *   - Recovery → retryCount bumps so React-Query consumers can invalidate.
 *   - Cadence flips: 15s when ok, 3s when degraded.
 *
 * Vitest fake timers don't auto-flush Promise microtasks, so each tick
 * needs both `vi.advanceTimersByTimeAsync` (drains setInterval+timeouts)
 * and an explicit microtask flush after to let the awaited fetch
 * resolve. `flush()` does both.
 */

const fetchMock = vi.fn();

beforeEach(() => {
  fetchMock.mockReset();
  (globalThis as { fetch?: typeof fetch }).fetch = fetchMock as unknown as typeof fetch;
  window.history.replaceState({}, "", "/");
  window.name = "";
  window.sessionStorage.removeItem("catchem.boot_token");
  delete (window as Window & { __CATCHEM_BOOT_TOKEN__?: string }).__CATCHEM_BOOT_TOKEN__;
  resetBootTokenCacheForTests();
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
  window.history.replaceState({}, "", "/");
  window.name = "";
  window.sessionStorage.removeItem("catchem.boot_token");
  delete (window as Window & { __CATCHEM_BOOT_TOKEN__?: string }).__CATCHEM_BOOT_TOKEN__;
  resetBootTokenCacheForTests();
  delete (globalThis as { fetch?: typeof fetch }).fetch;
});

function okResponse(): Response {
  return new Response(JSON.stringify({ ok: true }), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

/** Advance fake timers AND drain pending microtasks (fetch.then chains). */
async function flush(ms: number) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
    // Two passes covers `await fetch` → `if (cancelled)` → setState
    // resolving across separate microtask ticks.
    await Promise.resolve();
    await Promise.resolve();
  });
}

describe("useSidecarHealth", () => {
  it("stays ok while /healthz keeps returning 200", async () => {
    fetchMock.mockResolvedValue(okResponse());
    const { result } = renderHook(() => useSidecarHealth());

    await flush(0);
    expect(result.current.state).toBe("ok");
    expect(result.current.retryCount).toBe(0);

    await flush(SIDECAR_OK_INTERVAL_MS);
    expect(result.current.state).toBe("ok");
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("includes the boot token from sessionStorage", async () => {
    window.sessionStorage.setItem("catchem.boot_token", "abc123");
    fetchMock.mockResolvedValue(okResponse());
    renderHook(() => useSidecarHealth());

    await flush(0);
    expect(fetchMock.mock.calls[0][0]).toBe("/healthz?boot_token=abc123");
  });

  it("falls back to the boot token in the current URL", async () => {
    window.history.replaceState({}, "", "/?boot_token=abc123");
    fetchMock.mockResolvedValue(okResponse());
    renderHook(() => useSidecarHealth());

    await flush(0);
    expect(fetchMock.mock.calls[0][0]).toBe("/healthz?boot_token=abc123");
    expect(window.sessionStorage.getItem("catchem.boot_token")).toBe("abc123");
  });

  it("falls back to the boot token stored in window.name", async () => {
    window.name = "catchem.boot_token=abc123";
    fetchMock.mockResolvedValue(okResponse());
    renderHook(() => useSidecarHealth());

    await flush(0);
    expect(fetchMock.mock.calls[0][0]).toBe("/healthz?boot_token=abc123");
    expect(window.sessionStorage.getItem("catchem.boot_token")).toBe("abc123");
  });

  it("prefers the current URL over stale sessionStorage and caches the token", async () => {
    window.sessionStorage.setItem("catchem.boot_token", "stale");
    window.history.replaceState({}, "", "/?boot_token=fresh");
    fetchMock.mockResolvedValue(okResponse());
    renderHook(() => useSidecarHealth());

    await flush(0);
    expect(fetchMock.mock.calls[0][0]).toBe("/healthz?boot_token=fresh");
    window.history.replaceState({}, "", "/");
    window.name = "";
    window.sessionStorage.removeItem("catchem.boot_token");
    await flush(SIDECAR_OK_INTERVAL_MS);
    expect(fetchMock.mock.calls[1][0]).toBe("/healthz?boot_token=fresh");
  });

  it("uses the bootstrap token captured before React mounts", async () => {
    (window as Window & { __CATCHEM_BOOT_TOKEN__?: string }).__CATCHEM_BOOT_TOKEN__ = "abc123";
    fetchMock.mockResolvedValue(okResponse());
    renderHook(() => useSidecarHealth());

    await flush(0);
    expect(fetchMock.mock.calls[0][0]).toBe("/healthz?boot_token=abc123");
    expect(window.sessionStorage.getItem("catchem.boot_token")).toBe("abc123");
  });

  it("upgrades to reconnecting after one failure, down after two", async () => {
    fetchMock.mockRejectedValue(new TypeError("network down"));
    const { result } = renderHook(() => useSidecarHealth());

    await flush(0);
    expect(result.current.state).toBe("reconnecting");

    await flush(SIDECAR_DEGRADED_INTERVAL_MS);
    expect(result.current.state).toBe("down");
  });

  it("uses degraded cadence (3s) once reconnecting and recovers on next 200", async () => {
    let healthy = false;
    fetchMock.mockImplementation(async () => {
      if (healthy) return okResponse();
      throw new TypeError("network down");
    });
    const { result } = renderHook(() => useSidecarHealth());

    await flush(0);
    expect(result.current.state).toBe("reconnecting");
    const callsAfterFirst = fetchMock.mock.calls.length;

    healthy = true;
    await flush(SIDECAR_DEGRADED_INTERVAL_MS);
    expect(result.current.state).toBe("ok");
    expect(result.current.retryCount).toBe(1);
    expect(fetchMock.mock.calls.length).toBeGreaterThan(callsAfterFirst);
  });

  it("treats non-2xx (500) like a network failure", async () => {
    fetchMock.mockResolvedValue(
      new Response("internal", { status: 500 }),
    );
    const { result } = renderHook(() => useSidecarHealth());
    await flush(0);
    expect(result.current.state).toBe("reconnecting");
    await flush(SIDECAR_DEGRADED_INTERVAL_MS);
    expect(result.current.state).toBe("down");
  });

  it("clears interval on unmount so no further fetch fires", async () => {
    fetchMock.mockResolvedValue(okResponse());
    const { unmount } = renderHook(() => useSidecarHealth());
    await flush(0);
    const callCount = fetchMock.mock.calls.length;
    unmount();
    await flush(SIDECAR_OK_INTERVAL_MS * 2);
    expect(fetchMock.mock.calls.length).toBe(callCount);
  });
});
