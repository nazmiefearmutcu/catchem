import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { act, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createElement, type ReactNode } from "react";
import { SidecarBanner } from "@/components/SidecarBanner";
import { SIDECAR_DEGRADED_INTERVAL_MS } from "@/hooks/useSidecarHealth";

const fetchMock = vi.fn();

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

beforeEach(() => {
  fetchMock.mockReset();
  (globalThis as { fetch?: typeof fetch }).fetch = fetchMock as unknown as typeof fetch;
  installLocalStorage();
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
  delete (globalThis as { fetch?: typeof fetch }).fetch;
});

function jsonOk(): Response {
  return new Response(JSON.stringify({ ok: true }), { status: 200 });
}

function withQuery(child: ReactNode): ReactNode {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return createElement(QueryClientProvider, { client: qc }, child);
}

async function flush(ms: number) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
    await Promise.resolve();
    await Promise.resolve();
  });
}

describe("<SidecarBanner>", () => {
  it("renders nothing while sidecar is healthy", async () => {
    fetchMock.mockResolvedValue(jsonOk());
    const { container } = render(withQuery(<SidecarBanner />));
    await flush(0);
    expect(container.querySelector('[data-testid="sidecar-banner"]')).toBeNull();
  });

  it("shows the warn-toned reconnecting banner on first failure", async () => {
    fetchMock.mockRejectedValue(new TypeError("network down"));
    render(withQuery(<SidecarBanner />));
    await flush(0);
    const banner = screen.getByTestId("sidecar-banner");
    expect(banner.getAttribute("data-state")).toBe("reconnecting");
    expect(banner.getAttribute("aria-live")).toBe("assertive");
    expect(screen.getByText(/Reconnecting to sidecar/i)).toBeInTheDocument();
    // Warn tone — not bad.
    expect(banner.querySelector(".border-warn\\/40")).not.toBeNull();
  });

  it("upgrades to the bad-toned offline banner after two failures", async () => {
    fetchMock.mockRejectedValue(new TypeError("network down"));
    render(withQuery(<SidecarBanner />));
    await flush(0);
    await flush(SIDECAR_DEGRADED_INTERVAL_MS);
    const banner = screen.getByTestId("sidecar-banner");
    expect(banner.getAttribute("data-state")).toBe("down");
    expect(screen.getByText(/Sidecar is offline/i)).toBeInTheDocument();
    expect(screen.getByText(/restart the app/i)).toBeInTheDocument();
  });

  it("emits a 'system' recovery notification when sidecar comes back after being down (v54)", async () => {
    // Reset notification store BEFORE rendering so we don't see leftovers from earlier tests.
    const { __resetNotificationStoreForTests, HISTORY_STORAGE_KEY } = await import(
      "@/hooks/useDesktopAlerts"
    );
    __resetNotificationStoreForTests();

    // Sequence: fail, fail (→ down), then OK (→ recovery)
    fetchMock
      .mockRejectedValueOnce(new TypeError("first failure"))
      .mockRejectedValueOnce(new TypeError("second failure"))
      .mockResolvedValueOnce(jsonOk());

    render(withQuery(<SidecarBanner />));
    await flush(0);
    await flush(SIDECAR_DEGRADED_INTERVAL_MS);
    // After two failures the state should be "down"
    expect(screen.getByTestId("sidecar-banner").getAttribute("data-state")).toBe("down");
    // Advance one more cycle so the third (OK) probe fires and recovery is recognised
    await flush(SIDECAR_DEGRADED_INTERVAL_MS);
    // Banner gone — and a recovery notification should be in history with category "system"
    expect(screen.queryByTestId("sidecar-banner")).toBeNull();
    const raw = window.localStorage.getItem(HISTORY_STORAGE_KEY) ?? "[]";
    const history = JSON.parse(raw) as Array<{ id: string; category?: string; severity?: string; title: string }>;
    const recovered = history.find((h) => h.id.startsWith("sidecar-recovered-"));
    expect(recovered).toBeDefined();
    expect(recovered?.category).toBe("system");
    expect(recovered?.severity).toBe("success");
    expect(recovered?.title).toMatch(/Sidecar reconnected/);
  });
});
